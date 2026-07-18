#!/usr/bin/env python3
"""Self-tests for the provider-normalization seam (providers.py) — the laws eADR-0034/0036 rest on:
normalize is the IDENTITY for a Claude payload (the byte-stability pin); a Codex edit is rewritten
with EVERY path its batch patch names; session resolution is payload-first and the live-session
marker REFUSES on any ambiguity (stale, foreign-owned, future-stamped) rather than guessing; and the
Codex hook-command form renders through the shim while the Claude form stays byte-identical.

Run: uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py' -b
"""
from __future__ import annotations
import json
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hooks      # noqa: E402
import providers  # noqa: E402

PATCH = """*** Begin Patch
*** Update File: src/app.py
@@
-old
+new
*** Add File: docs/notes.md
+hello
*** Delete File: tmp/scratch.txt
*** End Patch"""


class TestNormalizeIdentityForClaude(unittest.TestCase):
    def test_claude_payload_is_returned_as_the_same_object(self):
        """The byte-stability law: a payload with no Codex tool name passes through UNTOUCHED — the
        same dict object, so the Claude path cannot drift by construction."""
        for tool in ("Edit", "Write", "MultiEdit", "NotebookEdit", "Bash", "Read", "ExitPlanMode"):
            payload = {"tool_name": tool, "tool_input": {"file_path": "x.py"}, "session_id": "s1"}
            self.assertIs(providers.normalize("PreToolUse", payload), payload)

    def test_non_dict_payloads_pass_through(self):
        self.assertIsNone(providers.normalize("Stop", None))
        self.assertEqual(providers.normalize("Stop", []), [])


class TestNormalizeCodexEdit(unittest.TestCase):
    def test_apply_patch_rewrites_to_edit_with_every_touched_path(self):
        payload = {"tool_name": "apply_patch", "tool_input": {"patch": PATCH}, "session_id": "s1"}
        out = providers.normalize("PreToolUse", payload)
        self.assertEqual(out["tool_name"], "Edit")
        self.assertEqual(out["tool_input"]["file_paths"],
                         ["src/app.py", "docs/notes.md", "tmp/scratch.txt"],
                         "a batch patch names MANY files; every one must be carried")
        self.assertEqual(out["tool_input"]["file_path"], "src/app.py")
        self.assertEqual(out["provider_raw"]["tool_name"], "apply_patch")
        self.assertEqual(payload["tool_name"], "apply_patch", "the input payload is never mutated")

    def test_envelope_found_under_an_unknown_key(self):
        out = providers.normalize("PreToolUse",
                                  {"tool_name": "apply_patch", "tool_input": {"weird_field": PATCH}})
        self.assertEqual(out["tool_input"]["file_paths"][0], "src/app.py")

    def test_no_envelope_still_rewrites_the_tool_name(self):
        """The deny must fire on the NAME even when the patch body cannot be found — only the
        per-file refinement is lost, never the gate decision."""
        out = providers.normalize("PreToolUse", {"tool_name": "apply_patch", "tool_input": {}})
        self.assertEqual(out["tool_name"], "Edit")
        self.assertEqual(out["tool_input"]["file_paths"], [])

    def test_codex_shell_rewrites_to_bash_with_a_joined_command(self):
        out = providers.normalize("PreToolUse", {"tool_name": "local_shell",
                                                 "tool_input": {"command": ["git", "commit", "-m", "x y"]}})
        self.assertEqual(out["tool_name"], "Bash")
        self.assertEqual(out["tool_input"]["command"], "git commit -m 'x y'")


class TestSessionResolution(unittest.TestCase):
    def test_payload_session_id_wins(self):
        with mock.patch.dict(os.environ, {"ENGINE_SESSION_ID": "env-sid"}):
            self.assertEqual(providers.resolve_session({"session_id": "payload-sid"}), "payload-sid")

    def test_env_chain_order_and_placeholder_guard(self):
        with mock.patch.dict(os.environ, {"ENGINE_SESSION_ID": "neutral",
                                          "CLAUDE_CODE_SESSION_ID": "claude"}):
            self.assertEqual(providers.session_from_env(), "neutral",
                             "the neutral override is deliberately first (an explicit knob)")
        with mock.patch.dict(os.environ, {"ENGINE_SESSION_ID": "${UNEXPANDED}",
                                          "CLAUDE_CODE_SESSION_ID": "claude"}):
            self.assertEqual(providers.session_from_env(), "claude")

    def test_explicit_flag_wins_over_everything(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "claude"}):
            self.assertEqual(providers.resolve_session(explicit="typed"), "typed")


class TestLiveSessionMarker(unittest.TestCase):
    def setUp(self):
        # Redirect the marker into a temp home so tests never touch the real per-user marker.
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(providers, "live_session_path",
                                        lambda: os.path.join(self._tmp.name, "marker.json"))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def _clear_env(self):
        return mock.patch.dict(os.environ, {}, clear=True)

    def test_write_then_read_round_trips(self):
        self.assertTrue(providers.write_live_session("sid-1", "codex"))
        record = providers.read_live_session()
        self.assertEqual(record["session_id"], "sid-1")
        self.assertEqual(record["provider"], "codex")
        mode = os.stat(providers.live_session_path()).st_mode & 0o777
        self.assertEqual(mode, 0o600, "owner-only permissions are part of the fail-safe spec")

    def test_marker_is_the_last_resort_for_a_typed_verb(self):
        providers.write_live_session("sid-2", "codex")
        with self._clear_env():
            self.assertEqual(providers.resolve_session(), "sid-2")

    def test_a_stale_marker_is_refused(self):
        providers.write_live_session("sid-3")
        path = providers.live_session_path()
        record = json.load(open(path))
        record["ts"] = time.time() - (25 * 3600)
        with open(path, "w") as fh:
            fh.write(json.dumps(record))
        with self._clear_env():
            self.assertIsNone(providers.read_live_session(), "stale → refuse, never guess")
            self.assertIsNone(providers.resolve_session())

    def test_a_future_stamped_marker_is_refused(self):
        providers.write_live_session("sid-4")
        path = providers.live_session_path()
        record = json.load(open(path))
        record["ts"] = time.time() + 3600
        with open(path, "w") as fh:
            fh.write(json.dumps(record))
        self.assertIsNone(providers.read_live_session())

    def test_a_malformed_or_absent_marker_is_refused(self):
        self.assertIsNone(providers.read_live_session())
        with open(providers.live_session_path(), "w") as fh:
            fh.write("{not json")
        self.assertIsNone(providers.read_live_session())


class TestDetect(unittest.TestCase):
    def test_env_wins(self):
        with mock.patch.dict(os.environ, {"ENGINE_PROVIDER": "codex"}):
            self.assertEqual(providers.detect(), "codex")
        with mock.patch.dict(os.environ, {"ENGINE_PROVIDER": "claude"}):
            self.assertEqual(providers.detect({"turn_id": "t"}), "claude")

    def test_payload_sniff_and_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(providers.detect({"turn_id": "t"}), "codex")
            self.assertEqual(providers.detect({"tool_name": "apply_patch"}), "codex")
            self.assertEqual(providers.detect({"tool_name": "Edit"}), "claude")
            self.assertEqual(providers.detect(), "claude")


class TestCodexHookCommandForm(unittest.TestCase):
    def test_codex_form_rides_the_shim_and_resolves_its_own_root(self):
        cmd = hooks.hook_command(".engine/tools/modes.py accept-hook", provider="codex")
        self.assertIn('cd "$(git rev-parse --show-toplevel', cmd,
                      "Codex has no project-dir token; the command must locate the root itself")
        self.assertIn('sh ".engine/tools/codex-hook-runner.sh" ".engine/tools/modes.py" accept-hook', cmd)
        self.assertNotIn("CLAUDE_PROJECT_DIR", cmd, "Claude vocabulary never leaks into the Codex form")

    def test_claude_form_is_byte_identical_to_the_default(self):
        rel = ".engine/tools/boot.py"
        self.assertEqual(hooks.hook_command(rel), hooks.hook_command(rel, provider="claude"))


if __name__ == "__main__":
    unittest.main()
