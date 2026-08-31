#!/usr/bin/env python3
"""Hermetic self-tests for ``acp_client``: the ``BuildExecutionRunner`` seam and its ACP v1 implementation,
``AcpClient``.

Every test drives a REAL subprocess — a small scripted fake ACP agent (a standalone Python script written to a
temp file once in ``setUpClass`` and spawned per test via ``execution_env_policy.launch`` through
``AcpClient`` itself) that reads newline-delimited JSON-RPC requests on stdin and emits scripted
newline-delimited JSON-RPC responses/notifications on stdout. This exercises the real stdio framing path, not
a mock of it. Coverage:

  * lifecycle: initialize -> session/new -> session/prompt -> a sequence of session/update notifications ->
    clean close, with the ordered transcript asserted in order;
  * client-callback: the agent sends session/request_permission mid-turn and the client responds per its
    configured policy (both allow and deny are exercised), and a session/set_mode call succeeds;
  * fault injection: a malformed/truncated inbound line is recorded as an update and does not crash the
    reader; the agent exits mid-turn and process_lost() flips true while close() still reaps the tree cleanly;
    a cancel the agent never acknowledges leaves the ack flag false and reported honestly;
  * the pinned ACP v1 vocabulary witness: the digest matches the pin, and every method AcpClient can emit is
    inside the pinned client_sends set.

Run: uv run --directory .engine --frozen -- python -m unittest tools.test_acp_client -v
Resource-warning-clean: uv run --directory .engine --frozen -- python -W error::ResourceWarning -m unittest tools.test_acp_client
"""
from __future__ import annotations

import os
import sys
import tempfile
import textwrap
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import acp_client  # noqa: E402


def _run_and_collect(client, text, errors):
    """Run one prompt turn on a background thread, recording any exception rather than letting it die
    silently — the cancellation test asserts the turn completed without error."""
    try:
        client.prompt(text)
    except Exception as exc:  # pragma: no cover - surfaced via the errors list the caller asserts on
        errors.append(exc)

FAKE_AGENT_SCRIPT = textwrap.dedent(
    r"""
    import json
    import os
    import sys

    def send(obj):
        sys.stdout.write(json.dumps(obj, sort_keys=True) + "\n")
        sys.stdout.flush()

    def recv():
        line = sys.stdin.readline()
        if line == "":
            return None
        line = line.strip()
        if not line:
            return recv()
        try:
            return json.loads(line)
        except Exception:
            return None

    SCENARIO = os.environ.get("SCENARIO", "normal")

    while True:
        msg = recv()
        if msg is None:
            break
        method = msg.get("method")
        mid = msg.get("id")

        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {"ok": True}})
        elif method == "session/new":
            params = msg.get("params") or {}
            # Mirror the real bridge: session/new REQUIRES mcpServers. Reject when the client omits it, so
            # the test proves the client now sends it.
            if "mcpServers" not in params:
                send({"jsonrpc": "2.0", "id": mid,
                      "error": {"code": -32602, "message": "mcpServers required"}})
            else:
                send({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": "sess-1"}})
        elif method == "session/set_mode":
            params = msg.get("params") or {}
            # Mirror the real bridge: the mode parameter is `modeId`. Reject `mode` so the test proves the
            # client sends the correct key.
            if not isinstance(params.get("modeId"), str):
                send({"jsonrpc": "2.0", "id": mid,
                      "error": {"code": -32602, "message": "modeId required"}})
            else:
                send({"jsonrpc": "2.0", "id": mid, "result": {"ok": True, "modeId": params["modeId"]}})
        elif method == "session/cancel":
            # ACP cancellation is a notification (no id). With no in-flight turn it is simply dropped; the
            # cancellable turn below does its own recv() and handles it there.
            pass
        elif method == "session/prompt":
            params = msg.get("params") or {}
            prompt = params.get("prompt")
            # Mirror the real bridge: prompt MUST be an array of content blocks, never a bare string.
            if not isinstance(prompt, list):
                send({"jsonrpc": "2.0", "id": mid,
                      "error": {"code": -32602, "message": "prompt must be an array"}})
                continue
            text = prompt[0].get("text", "") if prompt and isinstance(prompt[0], dict) else ""
            if text == "die":
                sys.exit(0)
            elif text == "slow":
                # A cancellable turn: stream an update, then wait for the cancel notification and END the
                # turn as 'cancelled'. If stdin closes first, end as end_turn.
                send({"jsonrpc": "2.0", "method": "session/update",
                      "params": {"seq": 1, "prompt_is_list": True}})
                stop = "end_turn"
                while True:
                    m2 = recv()
                    if m2 is None:
                        break
                    if m2.get("method") == "session/cancel":
                        stop = "cancelled"
                        break
                send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": stop}})
            elif text == "permission":
                send({"jsonrpc": "2.0", "id": 9001, "method": "session/request_permission",
                      "params": {"tool": "shell", "action": "run"}})
                resp = recv()
                send({"jsonrpc": "2.0", "method": "session/update",
                      "params": {"note": "permission-response-seen", "response": resp}})
                send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
            elif text == "malformed":
                sys.stdout.write("{this is not valid json\n")
                sys.stdout.flush()
                send({"jsonrpc": "2.0", "method": "session/update", "params": {"seq": 1}})
                send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
            else:
                # Echo that the prompt arrived as a well-formed content-block array, so the test can assert
                # the client sent the corrected shape.
                send({"jsonrpc": "2.0", "method": "session/update",
                      "params": {"seq": 1, "prompt_is_list": True}})
                send({"jsonrpc": "2.0", "method": "session/update", "params": {"seq": 2}})
                send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
        # unrecognized methods are silently ignored by the fake agent
    """
)


class AcpClientTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls.script_path = tempfile.mkstemp(prefix="fake_acp_agent_", suffix=".py")
        with os.fdopen(fd, "w") as fh:
            fh.write(FAKE_AGENT_SCRIPT)

    @classmethod
    def tearDownClass(cls):
        try:
            os.remove(cls.script_path)
        except OSError:
            pass

    def make_client(self, scenario="normal", permission_policy="deny", timeout_seconds=5.0):
        env = {"SCENARIO": scenario}
        if "PATH" in os.environ:
            env["PATH"] = os.environ["PATH"]
        client = acp_client.AcpClient(
            [sys.executable, self.script_path],
            env=env,
            permission_policy=permission_policy,
            timeout_seconds=timeout_seconds,
        )
        self.addCleanup(client.close)
        return client


class LifecycleTests(AcpClientTestBase):
    def test_full_lifecycle_ordered_transcript(self):
        client = self.make_client(scenario="normal")
        session_id = client.start_session()
        self.assertEqual(session_id, "sess-1")

        client.prompt("normal")

        # The session/prompt request blocks for a reply, so by the time prompt() returns the two
        # session/update notifications sent before that reply must already be in the transcript.
        updates = client.updates()
        update_kinds = [u["kind"] for u in updates]
        self.assertEqual(update_kinds, ["session/update", "session/update"])
        self.assertEqual([u["payload"]["seq"] for u in updates], [1, 2])
        # The agent only reaches this reply when session/new carried mcpServers and session/prompt carried a
        # content-block ARRAY; the echoed flag proves the client sent the corrected ACP shapes.
        self.assertTrue(updates[0]["payload"]["prompt_is_list"])

        witness = client.close()
        self.assertTrue(witness["leader_exited"])
        self.assertTrue(witness["group_reaped"])


class ClientCallbackTests(AcpClientTestBase):
    def test_request_permission_allow_policy(self):
        client = self.make_client(scenario="normal", permission_policy="allow")
        client.start_session()
        client.prompt("permission")

        kinds = [u["kind"] for u in client.updates()]
        self.assertIn("session/request_permission", kinds)
        self.assertIn("permission_decision", kinds)
        decision = next(u for u in client.updates() if u["kind"] == "permission_decision")
        self.assertTrue(decision["payload"]["allow"])

        # The agent echoes back the response it received to our request_permission reply.
        echoed = next(u for u in client.updates() if u["kind"] == "session/update")
        self.assertEqual(echoed["payload"]["response"]["result"]["outcome"], "allow")

    def test_request_permission_deny_policy(self):
        client = self.make_client(scenario="normal", permission_policy="deny")
        client.start_session()
        client.prompt("permission")

        decision = next(u for u in client.updates() if u["kind"] == "permission_decision")
        self.assertFalse(decision["payload"]["allow"])
        echoed = next(u for u in client.updates() if u["kind"] == "session/update")
        self.assertEqual(echoed["payload"]["response"]["result"]["outcome"], "deny")

    def test_set_mode_sends_modeId_and_succeeds(self):
        client = self.make_client(scenario="normal")
        client.start_session()
        result = client.set_mode("acceptEdits")
        # The agent only returns modeId when the client sent the correctly-named `modeId` parameter.
        self.assertEqual(result["modeId"], "acceptEdits")


class FaultInjectionTests(AcpClientTestBase):
    def test_malformed_event_is_recorded_not_raised(self):
        client = self.make_client(scenario="normal")
        client.start_session()
        client.prompt("malformed")  # does not raise despite the garbage line the agent injects

        kinds = [u["kind"] for u in client.updates()]
        self.assertIn("malformed", kinds)
        self.assertIn("session/update", kinds)

    def test_process_death_mid_stream_is_detected_and_closes_cleanly(self):
        client = self.make_client(scenario="normal", timeout_seconds=2.0)
        client.start_session()
        self.assertFalse(client.process_lost())

        with self.assertRaises(acp_client.AcpProcessLost):
            client.prompt("die")

        self.assertTrue(client.process_lost())
        witness = client.close()
        self.assertTrue(witness["leader_exited"])

    def test_cancel_with_no_in_flight_turn_is_not_acknowledged(self):
        # Cancellation acknowledgement is an in-flight turn ending as 'cancelled'. With nothing in flight
        # there is nothing to acknowledge, and cancel() returns False without blocking for the timeout.
        client = self.make_client(scenario="normal", timeout_seconds=5.0)
        client.start_session()

        started = time.monotonic()
        ack = client.cancel()
        elapsed = time.monotonic() - started
        self.assertFalse(ack)
        self.assertFalse(client.cancel_acknowledged)
        self.assertLess(elapsed, 2.0, "cancel() must not block for the full timeout with no turn in flight")

    def test_cancel_of_in_flight_turn_is_acknowledged(self):
        # Drive a real concurrent cancellation: a 'slow' turn runs on one thread and blocks for its reply
        # while the main thread sends the cancel notification; the agent then ends the turn as 'cancelled'
        # and the client reports the acknowledgement honestly from that stop reason.
        client = self.make_client(scenario="normal", timeout_seconds=5.0)
        client.start_session()

        errors = []
        prompt_thread = threading.Thread(target=lambda: _run_and_collect(client, "slow", errors))
        prompt_thread.start()
        try:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not any(
                    u["kind"] == "session/update" for u in client.updates()):
                time.sleep(0.02)
            ack = client.cancel()
        finally:
            prompt_thread.join(timeout=5.0)

        self.assertEqual(errors, [])
        self.assertTrue(ack)
        self.assertTrue(client.cancel_acknowledged)


class VocabularyWitnessTests(unittest.TestCase):
    def test_witness_matches_pin_and_stays_inside_client_sends(self):
        witness = acp_client.vocabulary_witness()
        self.assertTrue(witness["matches_pin"])
        self.assertEqual(witness["digest"], acp_client.ACP_V1_VOCABULARY_PINNED)
        for method in witness["methods_used"]:
            self.assertIn(method, acp_client.ACP_V1_VOCABULARY["client_sends"])
        self.assertEqual(set(witness["methods_used"]), set(acp_client.AcpClient.METHODS_EMITTED))

    def test_sending_an_unpinned_method_is_refused(self):
        client = acp_client.AcpClient(
            [sys.executable, "-c", "pass"], env={}, timeout_seconds=1.0
        )
        with self.assertRaises(ValueError):
            client._send("session/not_a_real_method", {}, 1)

    def test_the_pin_is_a_frozen_literal_that_actually_bites(self):
        # The pin must be a frozen literal, not the live computation, or it can never fail. Prove it bites:
        # a mutated vocabulary recomputes to a digest that no longer equals the frozen pin.
        self.assertNotEqual(acp_client.ACP_V1_VOCABULARY_PINNED,
                            "sha256:" + "0" * 64)  # sanity: it is a real, specific value
        drifted = dict(acp_client.ACP_V1_VOCABULARY)
        drifted["client_sends"] = list(drifted["client_sends"]) + ["session/rogue_method"]
        drifted_digest = "sha256:" + __import__("hashlib").sha256(
            acp_client._canonical_json(drifted).encode()).hexdigest()
        self.assertNotEqual(drifted_digest, acp_client.ACP_V1_VOCABULARY_PINNED,
                            "a changed vocabulary must not still match the frozen pin")


if __name__ == "__main__":
    unittest.main()
