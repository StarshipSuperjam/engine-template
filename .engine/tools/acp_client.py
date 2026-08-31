#!/usr/bin/env python3
"""A provider-neutral build-execution runner seam, and its first implementation: an ACP v1 client.

``BuildExecutionRunner`` is the abstract seam the Engine's build-execution contract dispatches through — a
small surface (start a session, send a prompt, read the ordered transcript of streamed updates, cancel, and
close) that any coding-agent bridge can implement without the orchestrator caring which protocol or vendor
sits underneath it.

``AcpClient`` is the first (and, at this Build, only) implementation: it speaks the Agent Client Protocol
(ACP) v1 — JSON-RPC 2.0 over newline-delimited JSON on a subprocess's stdio — to a locally-launched coding
agent. It supervises that subprocess ONLY through ``execution_env_policy`` (``launch`` to start it in its own
process group with an explicit allowlisted environment, ``terminate_tree`` to reap the whole tree on close);
it never touches ``os.environ`` or bare ``subprocess`` itself, and it carries no provider-specific token or
credential.

The exact ACP v1 method vocabulary this client is allowed to use is pinned as data — ``ACP_V1_VOCABULARY`` —
with a disclosure that names where it came from and states plainly that it was recorded at plan time, not
re-verified against the upstream spec this session. ``vocabulary_witness()`` proves at runtime that every
method the client actually emits is a member of the pinned ``client_sends`` set, so no method can silently
escape the pin.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Callable, Optional

import execution_env_policy


# ---------------------------------------------------------------------------------------------------------
# Pinned ACP v1 vocabulary — recorded as data so drift from the pin is a diffable, honest fact, not prose.
# ---------------------------------------------------------------------------------------------------------

def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


ACP_V1_VOCABULARY = {
    "client_sends": [
        "initialize",
        "session/new",
        "session/prompt",
        "session/cancel",
        "session/set_mode",
    ],
    "client_receives": [
        "session/update",
        "session/request_permission",
    ],
    "source": (
        "Agent Client Protocol v1 specification (agentclientprotocol.com) and the maintained "
        "@agentclientprotocol/* bridge packages."
    ),
    "disclosure": (
        "Recorded from the ACP v1 grounding at plan time; NOT independently re-verified against the "
        "upstream spec this session."
    ),
}

ACP_V1_VOCABULARY_DIGEST = "sha256:" + hashlib.sha256(
    _canonical_json(ACP_V1_VOCABULARY).encode()
).hexdigest()

# Pinned expected value — a FROZEN LITERAL, deliberately NOT `ACP_V1_VOCABULARY_DIGEST`. A literal is what
# makes the pin bite: any edit to ACP_V1_VOCABULARY changes the recomputed digest, which then no longer equals
# this frozen string, so vocabulary_witness() reports a mismatch. Binding the pin to the live computation
# instead would be a tautology that can never fail. When the vocabulary changes on purpose, update this literal
# in the same commit — that edit is the reviewable record that the protocol surface moved.
ACP_V1_VOCABULARY_PINNED = "sha256:8b432ab6873f5195c98af54cd78317ba16d1614109e8f7bd8f972bbdd754a163"


def vocabulary_witness() -> dict:
    """Runtime proof that the pinned vocabulary is self-consistent and that every JSON-RPC method
    ``AcpClient`` can emit is a member of ``ACP_V1_VOCABULARY['client_sends']`` — no method escapes the pin."""
    digest = ACP_V1_VOCABULARY_DIGEST
    matches_pin = digest == ACP_V1_VOCABULARY_PINNED
    methods_used = list(AcpClient.METHODS_EMITTED)
    escaped = [m for m in methods_used if m not in ACP_V1_VOCABULARY["client_sends"]]
    assert not escaped, f"AcpClient emits method(s) outside the pinned vocabulary: {escaped}"
    return {"digest": digest, "matches_pin": matches_pin, "methods_used": methods_used}


# ---------------------------------------------------------------------------------------------------------
# Abstract runner seam
# ---------------------------------------------------------------------------------------------------------

class BuildExecutionRunner(ABC):
    """The provider-neutral seam a build-execution contract dispatches through. An implementation supervises
    one coding-agent session: negotiate/open it, send prompt turns, expose the ordered transcript of streamed
    updates, request cancellation (and report honestly whether the agent acknowledged it), detect process
    loss, and tear down cleanly. Kept deliberately small — this is a seam, not a feature surface."""

    @abstractmethod
    def start_session(self) -> str:
        """Negotiate/initialize and open a session. Returns a session id, or raises."""

    @abstractmethod
    def prompt(self, text: str) -> None:
        """Send one prompt turn to the open session."""

    @abstractmethod
    def updates(self) -> list:
        """The ordered transcript of streamed updates captured so far — a list of structured dicts, each with
        at least a ``kind`` key. Never raises on a malformed/partial update; such an event is itself recorded
        as an update of kind ``malformed`` or ``partial``."""

    @abstractmethod
    def cancel(self) -> bool:
        """Request cancellation of the in-flight turn. Returns/tracks whether the agent ACKNOWLEDGED it —
        callers must not assume acknowledgement just because cancellation was requested."""

    @abstractmethod
    def close(self) -> dict:
        """Tear down the session, reaping the underlying process tree via
        ``execution_env_policy.terminate_tree``. Returns the tree-reap witness dict."""

    @abstractmethod
    def process_lost(self) -> bool:
        """Whether the supervised agent subprocess has died (crashed, was killed, or exited) since it was
        launched, independent of whether ``close()`` has been called."""


# ---------------------------------------------------------------------------------------------------------
# AcpClient — ACP v1 over JSON-RPC on stdio
# ---------------------------------------------------------------------------------------------------------

class AcpClient(BuildExecutionRunner):
    """Speaks ACP v1 (JSON-RPC 2.0, newline-delimited JSON on stdio) to a locally-launched coding-agent
    subprocess. Only the pinned vocabulary is ever emitted: ``initialize``, ``session/new``,
    ``session/prompt``, ``session/cancel``, ``session/set_mode``. Inbound ``session/update`` notifications are
    captured into an ordered transcript; inbound ``session/request_permission`` requests are answered
    according to a configurable, explicit, recorded policy (default: deny). No ACP v2 surface, no
    filesystem/terminal client-side APIs, no custom methods, and no provider-specific token or credential ever
    appears here.

    The subprocess is launched via ``execution_env_policy.launch`` with an env the CALLER supplies (this
    module never reads ``os.environ`` itself), and reaped via ``execution_env_policy.terminate_tree``.
    """

    METHODS_EMITTED = (
        "initialize",
        "session/new",
        "session/prompt",
        "session/cancel",
        "session/set_mode",
    )

    def __init__(
        self,
        argv,
        *,
        env: dict,
        cwd: Optional[str] = None,
        permission_policy: str = "deny",
        timeout_seconds: float = 30.0,
        client_info: Optional[dict] = None,
    ):
        if permission_policy not in ("allow", "deny"):
            raise ValueError("permission_policy must be 'allow' or 'deny'")
        self._argv = list(argv)
        self._env = dict(env)
        self._cwd = cwd
        self._permission_policy = permission_policy
        self.timeout_seconds = timeout_seconds
        self._client_info = client_info or {"name": "engine-acp-client", "version": "1"}

        self._proc = None
        self._next_id = 1
        self._lock = threading.Lock()
        self._pending: dict = {}
        self._updates: list = []
        self._session_id: Optional[str] = None
        self._cancel_ack = False
        self._process_lost = False
        self._reader_thread: Optional[threading.Thread] = None
        self._stop_reader = threading.Event()
        # A prompt turn's honest completion signal, captured from the session/prompt reply. Cancellation
        # acknowledgement is the in-flight turn ENDING as 'cancelled' — never a reply to the cancel send,
        # which ACP defines as a fire-and-forget notification with no reply of its own.
        self._prompt_in_flight = False
        self._last_stop_reason: Optional[str] = None
        # What the agent reported about itself during negotiation, captured so a caller (the qualification
        # harness) can record configuration_as_reported without reaching into the JSON-RPC plumbing. Recorded
        # AS REPORTED — never independently verified here.
        self.initialize_result: Optional[dict] = None
        self.session_modes: Optional[dict] = None

    # -- BuildExecutionRunner ------------------------------------------------------------------------------

    def start_session(self) -> str:
        env = execution_env_policy.allowlist_environment(self._env.keys(), source=self._env)
        self._proc = execution_env_policy.launch(self._argv, env=env, cwd=self._cwd)
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

        self.initialize_result = self._request(
            "initialize", {"clientInfo": self._client_info, "protocolVersion": 1})
        # ACP v1 session/new requires `mcpServers`; an empty list exposes NO MCP servers to the agent, which
        # is also the containment-consistent default (the qualified executor is handed no extra tools).
        result = self._request("session/new", {"cwd": self._cwd or ".", "mcpServers": []})
        session_id = None
        if isinstance(result, dict):
            session_id = result.get("sessionId") or result.get("session_id")
            self.session_modes = result.get("modes")
        if not session_id:
            session_id = f"session-{self._next_id}"
        self._session_id = session_id
        return session_id

    def prompt(self, text: str) -> None:
        if self._session_id is None:
            raise RuntimeError("start_session() must be called before prompt()")
        # ACP v1 `prompt` is an ARRAY of content blocks, not a bare string. A single text block is the
        # minimal well-formed turn.
        content = [{"type": "text", "text": text}]
        with self._lock:
            self._prompt_in_flight = True
            self._last_stop_reason = None
        try:
            result = self._request("session/prompt", {"sessionId": self._session_id, "prompt": content})
            stop = result.get("stopReason") if isinstance(result, dict) else None
            with self._lock:
                self._last_stop_reason = stop
        finally:
            with self._lock:
                self._prompt_in_flight = False

    def set_mode(self, mode: str) -> Any:
        if self._session_id is None:
            raise RuntimeError("start_session() must be called before set_mode()")
        # ACP v1 names the session mode parameter `modeId`, not `mode`.
        return self._request("session/set_mode", {"sessionId": self._session_id, "modeId": mode})

    def updates(self) -> list:
        with self._lock:
            return list(self._updates)

    def cancel(self) -> bool:
        if self._session_id is None:
            raise RuntimeError("start_session() must be called before cancel()")
        self._cancel_ack = False
        # ACP v1 cancellation is a fire-and-forget NOTIFICATION (no id, no reply). The agent acknowledges
        # by ENDING the in-flight turn with a 'cancelled' stop reason — never by replying to this send. So
        # the send itself is not acknowledgement, and we report ack only on observing that stop reason.
        try:
            self._send("session/cancel", {"sessionId": self._session_id}, None)
        except AcpProcessLost:
            return False
        # Wait, bounded, only while a turn is actually in flight; with nothing in flight there is nothing to
        # acknowledge, so we do not block for the timeout.
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            with self._lock:
                stopped = self._last_stop_reason == "cancelled"
                in_flight = self._prompt_in_flight
            if stopped or not in_flight or self.process_lost():
                break
            time.sleep(0.02)
        with self._lock:
            self._cancel_ack = self._last_stop_reason == "cancelled"
        return self._cancel_ack

    @property
    def cancel_acknowledged(self) -> bool:
        return self._cancel_ack

    def process_lost(self) -> bool:
        if self._process_lost:
            return True
        if self._proc is not None and self._proc.poll() is not None:
            self._process_lost = True
            return True
        return False

    def close(self) -> dict:
        self._stop_reader.set()
        witness = {"pid": None, "pgid": None, "leader_exited": True,
                   "escalated_to_kill": False, "group_reaped": True}
        if self._proc is not None:
            witness = execution_env_policy.terminate_tree(self._proc)
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=self.timeout_seconds)
        return witness

    # -- JSON-RPC plumbing ---------------------------------------------------------------------------------

    def _record(self, kind: str, payload: Any) -> None:
        with self._lock:
            self._updates.append({"kind": kind, "payload": payload})

    def _send(self, method: str, params: dict, msg_id: Optional[int]) -> None:
        if method not in self.METHODS_EMITTED:
            raise ValueError(f"method {method!r} is not in the pinned ACP v1 client_sends vocabulary")
        if self._proc is None or self._proc.stdin is None:
            raise AcpProcessLost(f"cannot send {method!r}: agent process is not running")
        message: dict = {"jsonrpc": "2.0", "method": method, "params": params}
        if msg_id is not None:
            message["id"] = msg_id
        line = _canonical_json(message) + "\n"
        try:
            self._proc.stdin.write(line.encode("utf-8"))
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            self._process_lost = True
            raise AcpProcessLost(f"failed to send {method!r}: {exc}") from exc

    def _request(self, method: str, params: dict) -> Any:
        with self._lock:
            msg_id = self._next_id
            self._next_id += 1
            event = threading.Event()
            self._pending[msg_id] = {"event": event, "result": None, "error": None}
        self._send(method, params, msg_id)

        deadline = time.monotonic() + self.timeout_seconds
        while True:
            if event.wait(timeout=0.05):
                break
            if self.process_lost():
                with self._lock:
                    self._pending.pop(msg_id, None)
                raise AcpProcessLost(f"agent process died while waiting for reply to {method!r}")
            if time.monotonic() > deadline:
                with self._lock:
                    self._pending.pop(msg_id, None)
                raise AcpTimeout(f"timed out waiting for reply to {method!r}")

        with self._lock:
            slot = self._pending.pop(msg_id, {"result": None, "error": None})
        if slot.get("error") is not None:
            raise AcpAgentError(f"{method} error: {slot['error']}")
        return slot.get("result")

    def _respond(self, msg_id, result=None, error=None) -> None:
        if self._proc is None or self._proc.stdin is None:
            return
        message: dict = {"jsonrpc": "2.0", "id": msg_id}
        if error is not None:
            message["error"] = error
        else:
            message["result"] = result
        line = _canonical_json(message) + "\n"
        try:
            self._proc.stdin.write(line.encode("utf-8"))
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError):
            self._process_lost = True

    def _handle_incoming(self, obj: dict) -> None:
        # A reply to one of our own requests.
        if "id" in obj and ("result" in obj or "error" in obj) and "method" not in obj:
            msg_id = obj["id"]
            with self._lock:
                slot = self._pending.get(msg_id)
                if slot is not None:
                    slot["result"] = obj.get("result")
                    slot["error"] = obj.get("error")
                    slot["event"].set()
            return

        method = obj.get("method")
        if method == "session/update":
            self._record("session/update", obj.get("params"))
            return
        if method == "session/request_permission":
            self._record("session/request_permission", obj.get("params"))
            allow = self._permission_policy == "allow"
            self._record("permission_decision", {"policy": self._permission_policy, "allow": allow})
            if "id" in obj:
                self._respond(obj["id"], result={"outcome": "allow" if allow else "deny"})
            return
        # Any other inbound method/notification we don't recognize — record, never crash.
        self._record("partial", obj)

    def _read_loop(self) -> None:
        stdout = self._proc.stdout if self._proc is not None else None
        if stdout is None:
            return
        while not self._stop_reader.is_set():
            try:
                raw = stdout.readline()
            except (ValueError, OSError):
                self._process_lost = True
                break
            if raw == b"":
                # EOF: the agent's stdout closed — the process is gone or going.
                self._process_lost = True
                break
            text = raw.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                self._record("malformed", {"raw": text})
                continue
            if not isinstance(obj, dict):
                self._record("malformed", {"raw": text})
                continue
            try:
                self._handle_incoming(obj)
            except Exception as exc:  # never let a malformed/unexpected event kill the reader
                self._record("malformed", {"raw": text, "error": str(exc)})


class AcpProcessLost(RuntimeError):
    """Raised when the agent subprocess died while a request was outstanding."""


class AcpTimeout(RuntimeError):
    """Raised when a request did not receive a reply within ``timeout_seconds``."""


class AcpAgentError(RuntimeError):
    """Raised when the agent replied to a request with a JSON-RPC error."""
