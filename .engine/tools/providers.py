#!/usr/bin/env python3
"""Provider normalization — the ONE seam where AI-runtime differences are absorbed.

The engine's gates, hooks, and tools are written against one canonical payload vocabulary (the
Claude Code hook shapes: tool_name Edit/Write/Bash, tool_input.file_path/command, session_id).
This module translates every other runtime's payloads INTO that vocabulary at the hook boundary
(hooks.run_hook calls normalize() immediately after reading the event), so everything downstream
stays provider-blind. Provider-specific names — the env vars, the Codex tool names, the patch
envelope — live HERE and in the narrow adapter code (hooks.py's command renderer, memory/capture.py's
transcript recognizer), never scattered through gate logic; a standing check holds that confinement.

Three laws:
  1. NORMALIZE IS THE IDENTITY FOR CLAUDE. A payload that carries no Codex tool name is returned
     as the SAME object, untouched — the Claude path's byte-stability is pinned by test.
  2. REWRITES ARE NAME-KEYED, NOT DETECTION-KEYED. `apply_patch` and the Codex shell tools are
     rewritten wherever they appear, because Claude Code never emits those names — so an edit is
     recognized even if provider detection itself fails (defense in depth; modes.py additionally
     carries `apply_patch` in its own denied set as the second belt).
  3. SESSION RESOLUTION IS PAYLOAD-FIRST, FAIL-SAFE. The hook payload's session_id always wins;
     the env chain is the CLI fallback; the live-session marker is the last resort for a typed
     Codex verb — and on any ambiguity (stale, foreign-owned, unreadable) it resolves NOTHING, so
     a stance change degrades to "could not identify the session" and the stance stays Explore,
     never a silent flip of the wrong session.
"""
from __future__ import annotations
import hashlib
import json
import os
import re
import shlex
import sys
import tempfile
import time

SCOPED_READ_MAX_BYTES = 1024 * 1024

CLAUDE = "claude"
CODEX = "codex"

# The engine's own provider tag, exported by each runtime's hook launcher (codex-hook-runner.sh sets
# codex; the Claude launcher sets nothing and claude is the default). Env-first so detection never
# depends on payload heuristics on the live path.
PROVIDER_ENV = "ENGINE_PROVIDER"

# The session-id env fallback chain, in order. ENGINE_SESSION_ID is DELIBERATELY first: it is the
# engine's own neutral, explicit override knob (an operator or a test sets it on purpose to pin the
# session identity), so when set it outranks the platform vars — the same explicit-beats-ambient rule
# as a --session flag. The platform vars follow; a runtime that exports no session var falls through
# to the live-session marker (typed-verb path only).
SESSION_ENV_CHAIN = ("ENGINE_SESSION_ID", "CLAUDE_CODE_SESSION_ID")

# Codex's canonical edit tool (its payloads report apply_patch even when a matcher aliases Edit/Write)
# and its shell tool names. "Bash" itself needs no entry — Codex reports simple shell as Bash; these
# are the sibling names that may appear on other shell paths, mapped defensively.
CODEX_EDIT_TOOL = "apply_patch"
CODEX_SHELL_TOOLS = frozenset({"shell", "local_shell", "unified_exec"})
# The first spelling is documented; the second was observed in CLI 0.153.4.
# Keep the matcher here with the names the adapter understands, not in gate logic.
CODEX_SPAWN_TOOLS = frozenset({"spawn_agent", "collaborationspawn_agent"})
CODEX_SPAWN_MATCHER = "^(Agent|spawn_agent|collaborationspawn_agent)$"
CODEX_QUEUE_TOOLS = frozenset({"send_message", "collaborationsend_message"})
CODEX_CONTINUE_TOOLS = frozenset({"followup_task", "collaborationfollowup_task"})
CODEX_CONTROL_TOOLS = CODEX_QUEUE_TOOLS | CODEX_CONTINUE_TOOLS
REVIEW_READ_TOOLS = frozenset({"mcp__engine-review-reader__read_file",
                              "mcp__engine_review_reader__read_file"})

# The apply_patch envelope: one call may create/edit/delete MANY files, each named on a marker line.
_PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File:\s*(.+?)\s*$", re.MULTILINE)
_PATCH_MARKERS = ("*** Begin Patch", "*** Update File:", "*** Add File:", "*** Delete File:")
_PATCH_INPUT_KEYS = ("patch", "input", "content", "changes")   # likeliest field names, tried first


def detect(payload: dict | None = None) -> str:
    """Which runtime this process is serving: the launcher-exported ENGINE_PROVIDER wins; a payload
    that carries a Codex-only shape (turn_id, or a Codex tool name) reads as codex; default claude."""
    env = (os.environ.get(PROVIDER_ENV) or "").strip().lower()
    if env in (CLAUDE, CODEX):
        return env
    if isinstance(payload, dict):
        if "turn_id" in payload:
            return CODEX
        tool = payload.get("tool_name")
        if isinstance(tool, str) and (tool == CODEX_EDIT_TOOL or tool in CODEX_SHELL_TOOLS or tool in CODEX_SPAWN_TOOLS or tool in CODEX_CONTROL_TOOLS):
            return CODEX
    return CLAUDE


def detect_signal(payload: dict | None = None) -> str:
    """Which SIGNAL decides `detect`'s answer, as a content-free token: `env` (ENGINE_PROVIDER is set),
    `turn_id` (a Codex-only payload field), `tool_name` (a Codex tool name), or `default` (none of those
    — `detect` returns claude). A PURE READ that mirrors `detect`'s precedence exactly and changes its
    answer for no one; it exists so a capture marker can record WHY a transcript was routed, making a
    future provider misroute diagnosable rather than silent. It is deliberately NOT consulted on any
    gate path — only `detect` is."""
    env = (os.environ.get(PROVIDER_ENV) or "").strip().lower()
    if env in (CLAUDE, CODEX):
        return "env"
    if isinstance(payload, dict):
        if "turn_id" in payload:
            return "turn_id"
        tool = payload.get("tool_name")
        if isinstance(tool, str) and (tool == CODEX_EDIT_TOOL or tool in CODEX_SHELL_TOOLS or tool in CODEX_SPAWN_TOOLS or tool in CODEX_CONTROL_TOOLS):
            return "tool_name"
    return "default"


def _patch_file_paths(tool_input) -> list:
    """Every file path named in an apply_patch envelope, in order, de-duplicated. The payload field
    carrying the envelope is not a stable contract, so the envelope is FOUND by its own markers: the
    likeliest keys are tried first, then any string value. No envelope found → [] (the deny still
    fires on the tool name; only the per-file message/relay refinement is lost)."""
    candidates = []
    if isinstance(tool_input, str):
        candidates = [tool_input]
    elif isinstance(tool_input, dict):
        candidates = [tool_input[k] for k in _PATCH_INPUT_KEYS
                      if isinstance(tool_input.get(k), str)]
        candidates += [v for k, v in sorted(tool_input.items())
                       if isinstance(v, str) and k not in _PATCH_INPUT_KEYS]
    for text in candidates:
        if any(marker in text for marker in _PATCH_MARKERS):
            seen, out = set(), []
            for p in _PATCH_FILE_RE.findall(text):
                if p not in seen:
                    seen.add(p)
                    out.append(p)
            return out
    return []


def _shell_command(tool_input) -> str:
    """The one command string a Codex shell payload carries — joined shell-safely when the runtime
    reports an argv list instead of a string."""
    if isinstance(tool_input, dict):
        cmd = tool_input.get("command")
        if isinstance(cmd, str):
            return cmd
        if isinstance(cmd, list):
            try:
                return shlex.join(str(c) for c in cmd)
            except (TypeError, ValueError):
                return ""
    return ""


def launch_record(payload, provider: str | None = None):
    """Provider-neutral requested launch facts, with provenance and explicit unknowns.

    The common hook ``model`` describes the parent and is NEVER a child-model
    fallback. Agent-file/default resolution and actual child settings are not in
    the observed spawn input, so absence stays unknown. No task prose is parsed.
    Runtime qualification supplies effective-setting evidence separately.
    """
    if not isinstance(payload, dict):
        return None
    provider = provider or detect(payload)
    if provider not in (CLAUDE, CODEX):
        return None
    tool = payload.get("tool_name")
    known = ("Agent", "Task") if provider == CLAUDE else ("Agent", *CODEX_SPAWN_TOOLS)
    if not isinstance(tool, str) or tool not in known:
        return None
    raw = payload.get("tool_input")
    if not isinstance(raw, dict):
        raw = {}

    def text_value(key):
        value = raw.get(key)
        return value if isinstance(value, str) and value.strip() else None

    kind = text_value("subagent_type") if provider == CLAUDE else (
        text_value("agent_type") or text_value("subagent_type"))
    roles = {"Explore": "search", "Plan": "plan", "general-purpose": "judgment"}
    if provider == CODEX:
        roles = {"explorer": "search", "default": "judgment", "worker": "execution"}
    role = roles.get(kind, "unclassified")
    model = text_value("model")
    effort = text_value("reasoning_effort") or text_value("model_reasoning_effort")
    sandbox = text_value("sandbox_mode")
    fork = raw.get("fork_turns") if provider == CODEX else raw.get("fork_context")
    if not isinstance(fork, (str, bool, int)):
        fork = None
    unknown = ["effective_model", "effective_effort", "effective_sandbox", "recursion_limit"]
    for field, value in (("requested_model", model), ("requested_effort", effort),
                         ("sandbox_intent", sandbox), ("fork_context", fork)):
        if value is None:
            unknown.append(field)
    if role == "unclassified":
        unknown.append("semantic_role")
    return {
        "provider": provider, "agent_type": kind, "semantic_role": role,
        "requested_model": model, "model_source": "tool_input.model" if model else None,
        "effective_model": None, "requested_effort": effort,
        "effort_source": "tool_input" if effort else None, "effective_effort": None,
        "sandbox_intent": sandbox, "effective_sandbox": None,
        "fork_context": fork, "recursion_limit": None,
        "session_id": payload.get("session_id") if isinstance(payload.get("session_id"), str) else None,
        "unknown_fields": unknown,
    }


def scoped_call(payload: dict) -> dict:
    """Native assignment facts. Unknowns remain unknown; no inference from task prose.

    Call after normalization or on the original envelope. Exact launch aliases live here. The
    controller registers the task name (Codex) or unique packet path (Claude) before dispatch.
    A tool's successful return is not child delivery or assignment completion.
    """
    raw_name = (payload.get("provider_raw") or {}).get("tool_name", payload.get("tool_name"))
    provider = (payload.get("provider_launch") or {}).get("provider") or detect(payload)
    inp = payload.get("tool_input")
    inp = inp if isinstance(inp, dict) else {}
    result = {"provider": provider, "kind": "other", "call_id": payload.get("tool_use_id"),
              "root": payload.get("session_id"), "child": payload.get("agent_id"),
              "role": payload.get("agent_type"), "input": inp}
    launch = launch_record(payload, provider)
    if launch:
        fork = launch["fork_context"]
        fresh = (fork == "none") if provider == CODEX else (
            fork in (None, False) and not inp.get("resume") and launch["agent_type"] != "fork")
        result.update(kind="launch", role=launch["agent_type"], fresh=fresh,
                      name=inp.get("task_name") if provider == CODEX else None,
                      prompt=inp.get("prompt") if provider == CLAUDE else None)
    elif provider == CODEX and raw_name in CODEX_CONTROL_TOOLS:
        result.update(kind="queue" if raw_name in CODEX_QUEUE_TOOLS else "continue",
                      target=inp.get("target"), content=inp.get("message"))
    elif provider == CLAUDE and raw_name == "SendMessage":
        result.update(kind="continue", target=inp.get("recipient"), content=inp.get("content"))
    return result


def scoped_transcript(payload: dict, provider: str, *, metadata_only: bool = False) -> dict:
    """Read observed child identity, delivered control payloads and final output.

    Deliberately bounded to the two qualified native formats. Never convert malformed or missing
    data into successful evidence. This is local operational provenance, not same-user isolation.
    """
    from pathlib import Path
    path = payload.get("agent_transcript_path") or payload.get("transcript_path")
    if not isinstance(path, str) or not path:
        return {}
    result = {"path": path, "messages": [], "final": None}
    try:
        if metadata_only:
            if provider != CODEX:
                return {}
            with Path(path).open("rb") as stream:
                header = stream.readline(64 * 1024 + 1)
            if len(header) > 64 * 1024 or not header.endswith(b"\n"):
                return {}
            line = header.decode("utf-8")
            row = json.loads(line)
            if not isinstance(row, dict) or row.get("type") != "session_meta":
                return {}
            data = row.get("payload")
            source = data.get("source") if isinstance(data, dict) else None
            spawn = (source.get("subagent") or {}).get("thread_spawn") if isinstance(source, dict) else None
            if (not isinstance(data, dict) or not isinstance(data.get("id"), str) or
                    not isinstance(spawn, dict) or not isinstance(spawn.get("parent_thread_id"), str) or
                    not isinstance(spawn.get("agent_path"), str)):
                return {}
            if not all(x.strip() for x in (data["id"], spawn["parent_thread_id"], spawn["agent_path"])):
                return {}
            return {"path": path, "child": data["id"], "root": spawn["parent_thread_id"],
                    "name": spawn["agent_path"], "messages": [], "final": None}
        with Path(path).open("r", encoding="utf-8") as stream:
            for line in stream:
                row = json.loads(line)
                if not isinstance(row, dict):
                    return {}
                data = row.get("payload", {})
                if not isinstance(data, dict):
                    return {}
                if provider == CODEX:
                    if row.get("type") == "event_msg" and data.get("type") == "task_started":
                        result["final"] = None
                    elif row.get("type") == "session_meta":
                        source = data.get("source") or {}
                        spawn = (source.get("subagent") or {}).get("thread_spawn") if isinstance(source, dict) else None
                        if "child" in result:
                            return {}  # duplicate/contradictory session metadata is not one actor
                        if isinstance(spawn, dict):
                            result.update(child=data.get("id"), root=spawn.get("parent_thread_id"),
                                          name=spawn.get("agent_path"))
                    elif row.get("type") == "response_item" and data.get("type") == "agent_message":
                        result["messages"].append(data)
                    elif row.get("type") == "response_item" and data.get("type") == "message" and data.get("role") == "assistant" and data.get("phase") in ("final", "final_answer"):
                        result["final"] = "".join(x.get("text", "") for x in data.get("content", []) if isinstance(x, dict))
                else:
                    if row.get("type") == "assistant":
                        content = (row.get("message") or {}).get("content", [])
                        texts = [x.get("text", "") for x in content if isinstance(x, dict) and x.get("type") == "text"]
                        if texts:
                            result["final"] = "".join(texts)
                    elif row.get("type") == "user":
                        result["final"] = None
                        result["messages"].append(row)
    except (OSError, ValueError, TypeError, AttributeError):
        return {}
    return result


def scoped_launch_capacity_rejected(payload: dict) -> bool:
    """One qualified native failure proves the spawn was rejected before creating a child.

    Measured native Codex function-call error, not a generic transport exception. Unknown failure
    prose and structured shapes are deliberately not promoted to definite nonexecution.
    """
    return (payload.get("is_error") is True and
            payload.get("tool_response") == "collab spawn failed: agent thread limit reached")


def scoped_control_digest(content) -> str | None:
    """Digest the exact observed opaque native message, never its interpreted meaning."""
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest() if isinstance(content, str) and content else None


def scoped_control_verified(transcript: dict, *, root: str, child: str, name: str,
                            launch_digest: str | None, continuations: list[dict]) -> bool:
    """Join one initial native launch envelope, then each continuation in dispatch order.

    Native initial and followup headers are identical. Only the actual observed opaque payload
    differentiates them; metadata prose or message position alone never proves the initial launch.
    """
    if not launch_digest or (transcript.get("root"), transcript.get("child"), transcript.get("name")) != (
            root, child, "/root/" + name):
        return False
    messages = transcript.get("messages")
    if not isinstance(messages, list) or len(messages) != 1 + len(continuations):
        return False
    expected = [launch_digest]
    for c in continuations:
        if c.get("sender") != root or c.get("recipient") != child or c.get("dispatched") is not True:
            return False
        expected.append(scoped_control_digest(c.get("content")))
    if None in expected or len(set(expected)) != len(expected):
        return False  # indistinguishable control payloads cannot establish separate deliveries
    for message, digest in zip(messages, expected):
        if (not isinstance(message, dict) or message.get("author") != "/root" or
                message.get("recipient") != "/root/" + name):
            return False
        parts = message.get("content")
        if (not isinstance(parts, list) or len(parts) != 2 or
                not all(isinstance(p, dict) for p in parts) or
                parts[0].get("type") != "input_text" or not isinstance(parts[0].get("text"), str) or
                parts[1].get("type") != "encrypted_content"):
            return False
        if scoped_control_digest(parts[1].get("encrypted_content")) != digest:
            return False
    return True


def scoped_launch_child(response):
    """Claude's documented Agent output identifies the child independently of start ordering."""
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except ValueError:
            return None
    if isinstance(response, dict):
        child = response.get("agentId")
        if isinstance(child, str) and child:
            return child
    return None


def _codex_command_completion(payload: dict) -> dict | None:
    """Join plain hook output to an exact native completion; stdout never supplies its own status.

    Qualified on Desktop 26.901.51231 / Codex 0.153.4. The native completion was visible during
    PostToolUse. A missing, truncated, changed or unfamiliar transcript leaves execution unverified.
    """
    from pathlib import Path
    actor = payload.get("agent_id") or payload.get("session_id")
    root, turn, call = (payload.get(k) for k in ("session_id", "turn_id", "tool_use_id"))
    path = payload.get("agent_transcript_path") or payload.get("transcript_path")
    if not all(isinstance(x, str) and x for x in (actor, root, turn, call, path)):
        return None
    try:
        with Path(path).open("rb") as stream:
            meta = json.loads(stream.readline())
            stream.seek(0, 2)
            offset = max(0, stream.tell() - 4 * 1024 * 1024)
            stream.seek(offset)
            tail = stream.read()
        if offset:
            tail = tail.split(b"\n", 1)[-1]
        if meta.get("type") != "session_meta" or meta.get("payload", {}).get("id") != actor:
            return None
        if payload.get("agent_id"):
            spawn = meta["payload"].get("source", {}).get("subagent", {}).get("thread_spawn", {})
            if spawn.get("parent_thread_id") != root:
                return None
        matches = []
        for line in tail.splitlines():
            row = json.loads(line)
            data = row.get("payload", {})
            item = data.get("item", {})
            if row.get("type") == "event_msg" and data.get("type") == "item_completed" and item.get("id") == call:
                matches.append((data, item))
        if len(matches) != 1:
            return None
        data, item = matches[0]
        if item.get("type") == "CommandExecution" and data.get("thread_id") == actor and data.get("turn_id") == turn:
            return item
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        pass
    return None


def _codex_shell_read_succeeded(payload: dict, content: str, response: str) -> bool:
    item = _codex_command_completion(payload)
    return bool(item and content and content in response and item.get("status") == "completed"
                and type(item.get("exit_code")) is int and item["exit_code"] == 0
                and item.get("aggregated_output") == response
                and isinstance(item.get("stdout"), str) and content in item["stdout"])


def scoped_reads_path(payload: dict, path: str) -> bool:
    """Recognize the immutable path, including a simple relative native cat or Read.

    Arbitrary shell directory changes and computed paths remain unsupported; do not guess their meaning.
    Successful full-content evidence is checked separately before a read can be recorded.
    """
    from pathlib import Path
    from urllib.parse import urlparse, unquote
    inp = payload.get("tool_input") or {}
    if payload.get("tool_name") in REVIEW_READ_TOOLS:
        return isinstance(inp, dict) and inp.get("path") == path
    if path in json.dumps(inp):
        return True
    if not isinstance(inp, dict):
        return False
    cwd = payload.get("cwd")
    if payload.get("tool_name") == "Read":
        target = inp.get("file_path")
    elif payload.get("tool_name") == "Bash":
        try:
            words = shlex.split(_shell_command(inp))
        except ValueError:
            return False
        if not words or Path(words[0]).name != "cat":
            return False
        operands = words[1:]
        if operands[:1] == ["--"]:
            operands = operands[1:]
        if len(operands) != 1 or operands[0].startswith("-"):
            return False
        target = operands[0]
        if detect(payload) == CODEX:
            item = _codex_command_completion(payload)
            if not item:
                return False
            cwd = item.get("cwd")
            if isinstance(cwd, str) and cwd.startswith("file:"):
                parsed = urlparse(cwd)
                if parsed.netloc not in ("", "localhost"):
                    return False
                cwd = unquote(parsed.path)
    else:
        return False
    if not isinstance(target, str) or not target:
        return False
    candidate = Path(target)
    if not candidate.is_absolute():
        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
            return False
        candidate = Path(cwd) / candidate
    return candidate.resolve() == Path(path).resolve()


def scoped_read_succeeded(payload: dict, content: str) -> bool:
    """Successful Read/Bash response containing the whole immutable packet, never just its name."""
    if payload.get("tool_name") in REVIEW_READ_TOOLS:
        return _review_reader_succeeded(payload, content)
    if payload.get("is_error") or payload.get("tool_name") not in ("Read", "Bash"):
        return False
    response = payload.get("tool_response")
    if isinstance(response, str):
        if payload.get("tool_name") == "Bash" and detect(payload) == CODEX:
            return _codex_shell_read_succeeded(payload, content, response)
        # Structured output from other qualified provider surfaces.
        try:
            decoded = json.loads(response)
        except ValueError:
            decoded = None
        if isinstance(decoded, dict):
            response = decoded
        else:
            return payload.get("tool_name") == "Read" and content in response
    if not isinstance(response, dict) or response.get("isError") or response.get("is_error"):
        return False
    if payload.get("tool_name") == "Bash" and response.get("exit_code", response.get("exitCode")) != 0:
        return False
    values = [response.get(k) for k in ("stdout", "output", "content")]
    file = response.get("file")
    if isinstance(file, dict):
        values.append(file.get("content"))
    return any(isinstance(value, str) and content in value for value in values)


def _review_reader_succeeded(payload: dict, content: str) -> bool:
    """Only a complete successful result from the named reader earns packet-read evidence."""
    response = payload.get("tool_response")
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except ValueError:
            return False
    if payload.get("is_error") or not isinstance(response, dict):
        return False
    if response.get("isError") or response.get("is_error"):
        return False
    blocks = response.get("content")
    if not isinstance(blocks, list) or len(blocks) != 1:
        return False
    block = blocks[0]
    if not isinstance(block, dict) or block.get("type") != "text":
        return False
    try:
        result = json.loads(block["text"])
    except (KeyError, TypeError, ValueError):
        return False
    return (isinstance(result, dict) and result.get("complete") is True
            and type(result.get("offset")) is int and result["offset"] == 0
            and result.get("file_path") == (payload.get("tool_input") or {}).get("path")
            and result.get("content") == content
            and result.get("sha256") == "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest())


def scoped_deliveries(transcript: dict, content: str, name: str) -> int:
    """Count exact native delivered payloads with controller/recipient attribution.

    Codex encrypted content is retained privately and compared as opaque transport bytes. It cannot
    establish the meaning of clarification. Unknown Claude envelope shapes remain unverified.
    """
    count = 0
    for message in transcript.get("messages", []):
        if message.get("author") != "/root" or message.get("recipient") != "/root/" + name:
            continue
        for part in message.get("content", []):
            if not isinstance(part, dict):
                continue
            if content in (part.get("encrypted_content"), part.get("text")):
                count += 1
    return count


def normalize(event: str, payload):
    """Canonicalize a hook payload. Claude payloads pass through as the SAME object (identity —
    test-pinned); a Codex edit becomes tool_name "Edit" with tool_input.file_paths = EVERY path the
    patch envelope names (+ file_path = the first, for single-path readers), and a Codex shell tool
    becomes tool_name "Bash" with tool_input.command. The raw payload fields are preserved under
    provider_raw for diagnostics; nothing else in the payload is touched."""
    if not isinstance(payload, dict):
        return payload
    tool = payload.get("tool_name")
    if not isinstance(tool, str):
        return payload
    if tool in CODEX_SPAWN_TOOLS or (tool == "Agent" and detect(payload) == CODEX):
        launch = launch_record(payload, CODEX)
        out = dict(payload)
        raw = payload.get("tool_input")
        out["tool_name"] = "Agent"
        out["tool_input"] = dict(raw) if isinstance(raw, dict) else {}
        if launch["semantic_role"] == "search":
            # Keep the native role when the compatibility field supplied it: downstream
            # readers re-derive classification and must not mistake canonical Explore for unknown.
            out["tool_input"]["agent_type"] = launch["agent_type"]
            out["tool_input"]["subagent_type"] = "Explore"
        out["provider_launch"] = launch
        # Diagnostics are bounded and omit the potentially private task message.
        out["provider_raw"] = {"tool_name": tool, "input_keys": sorted(
            str(key)[:80] for key in raw)[:32] if isinstance(raw, dict) else []}
        return out
    if tool == CODEX_EDIT_TOOL:
        raw = payload.get("tool_input")
        paths = _patch_file_paths(raw)
        tool_input = {"file_paths": paths}
        if paths:
            tool_input["file_path"] = paths[0]
        out = dict(payload)
        out["tool_name"] = "Edit"
        out["tool_input"] = tool_input
        out["provider_raw"] = {"tool_name": tool, "tool_input": raw}
        return out
    if tool in CODEX_SHELL_TOOLS:
        out = dict(payload)
        out["tool_name"] = "Bash"
        out["tool_input"] = {"command": _shell_command(payload.get("tool_input"))}
        out["provider_raw"] = {"tool_name": tool, "tool_input": payload.get("tool_input")}
        return out
    return payload


def session_from_env() -> str | None:
    """The first present, non-empty, actually-expanded session id in the env chain (an unexpanded
    `${...}` literal — a shell that passed the token through — is skipped, mirroring the CLI flag
    guard)."""
    for var in SESSION_ENV_CHAIN:
        value = os.environ.get(var) or ""
        if value and "${" not in value:
            return value
    return None


# ---- the live-session marker (the typed-verb fallback for a runtime with no session env var) ----
# boot writes it at every SessionStart; a typed `$engine-start` on Codex resolves through it when the
# payloadless CLI has no env var to read. FAIL-SAFE BY CONSTRUCTION: per-user temp scope, owner-only
# permissions, owner + freshness checked on read, and any ambiguity resolves to None — the caller's
# stance change then reports failure instead of flipping an unidentified session. KNOWN LIMIT
# (disclosed): two CONCURRENT sessions of the same user in one repo share the marker
# last-writer-wins, so the typed verb can only be trusted to address the most recently started
# session; the stance readout (`$engine-status`) is the check.
_MARKER_MAX_AGE = 24 * 3600      # a marker older than one session-day is stale — refuse, never guess
_MARKER_FUTURE_SKEW = 300        # a timestamp from the future beyond clock skew is forged/broken — refuse


MARKER_ENV = "ENGINE_LIVE_SESSION_MARKER"   # explicit path override (tests / unusual temp setups)


def live_session_path() -> str:
    override = os.environ.get(MARKER_ENV)
    if override:
        return override
    if "unittest" in sys.modules:
        # Hermetic under a test harness (the emit_finding precedent): a test that exercises boot's
        # heartbeat must NEVER write the developer's real per-user marker — a stale test session id
        # there would leak into real session resolution.
        return os.path.join(tempfile.gettempdir(), f"engine-live-session-test-{os.getpid()}.json")
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    digest = hashlib.sha1(root.encode("utf-8")).hexdigest()[:16]
    # The uid in the name scopes the marker per-user even on a SHARED system temp dir (a pre-created
    # same-name file by another user then simply isn't ours — the owner check refuses it — and can no
    # longer squat the one name every user would compute).
    uid = os.getuid() if hasattr(os, "getuid") else 0
    return os.path.join(tempfile.gettempdir(), f"engine-live-session-{uid}-{digest}.json")


def write_live_session(session_id, provider: str | None = None) -> bool:
    """Record the live session (called by boot). Owner-only permissions; best-effort — a failure
    never disturbs the hook that called it."""
    if not isinstance(session_id, str) or not session_id:
        return False
    record = {"session_id": session_id, "provider": provider or detect(), "ts": time.time()}
    path = live_session_path()
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW      # a planted symlink must never make this write land elsewhere
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(record))
        try:
            os.chmod(path, 0o600)     # O_CREAT's mode only applies to a NEW file; re-assert on reuse
        except OSError:
            pass
        return True
    except OSError:
        return False


def read_live_session(max_age: float = _MARKER_MAX_AGE) -> dict | None:
    """The live-session record, or None on ANY ambiguity: absent, unreadable, malformed, owned by
    another user, stale, or timestamped in the future. Refusal is the safety property — a caller
    must treat None as 'could not identify the session', never guess."""
    path = live_session_path()
    try:
        st = os.stat(path)
        if hasattr(os, "getuid") and st.st_uid != os.getuid():
            return None
        with open(path, encoding="utf-8") as fh:
            record = json.load(fh)
        if not isinstance(record, dict):
            return None
        sid, ts = record.get("session_id"), record.get("ts")
        if not isinstance(sid, str) or not sid or not isinstance(ts, (int, float)):
            return None
        age = time.time() - ts
        if age > max_age or age < -_MARKER_FUTURE_SKEW:
            return None
        return record
    except (OSError, ValueError):
        return None


def resolve_session(payload: dict | None = None, explicit: str | None = None) -> str | None:
    """The session id for the current action: an explicit value (a --session flag) wins; then the
    hook payload's session_id; then the env chain; then the live-session marker. None means 'could
    not identify the session' — every caller degrades safe on it (a stance change reports failure
    and the stance stays Explore)."""
    if isinstance(explicit, str) and explicit and "${" not in explicit:
        return explicit
    if isinstance(payload, dict):
        sid = payload.get("session_id")
        if isinstance(sid, str) and sid:
            return sid
    sid = session_from_env()
    if sid:
        return sid
    record = read_live_session()
    # PROVIDER-CONFINED: the marker resolves a session ONLY when the session it records is a Codex
    # one — the runtime with no session env var, the fallback's whole reason to exist. A Claude
    # session always exports its env var, so reaching this point on Claude means something is off,
    # and the safe answer is the historical one: resolve nothing (stance changes report failure)
    # rather than adopt whichever session most recently booted.
    if record and record.get("provider") == CODEX:
        return record["session_id"]
    return None


# Provider normalization sits on every hook's hot path, including content-free hooks that must not import the
# optional memory package. Keep the registered writer visible but resolve the common authority only if it runs.
# The accepted dispatcher also loads this file under a private, read-only bootstrap name before its tools root
# is installed; that use exposes no writer and therefore needs no adapter import at all.
if __name__ != "_engine_accepted_provider_authority":
    import mutation_guards as _mutation_guards  # noqa: E402
    _mutation_guards.install(globals(), {"write_live_session": "automatic-live-session"})
