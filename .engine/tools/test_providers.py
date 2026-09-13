#!/usr/bin/env python3
"""Self-tests for the provider-normalization seam (providers.py) — the invariants the adapters rest on:
normalize is the IDENTITY for a Claude payload (the byte-stability pin); a Codex edit is rewritten
with EVERY path its batch patch names; session resolution is payload-first and the live-session
marker REFUSES on any ambiguity (stale, foreign-owned, future-stamped) rather than guessing; and the
Codex hook-command form renders through the shim while the Claude form stays byte-identical.

Run: uv run --directory .engine --frozen -- python tools/selftest.py
"""
from __future__ import annotations
import json
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import glob as _glob  # noqa: E402
import hooks      # noqa: E402
import providers  # noqa: E402
import validate   # noqa: E402


def _codex_wire_count(module_ids=None) -> int:
    """Count the committed codex-hook wires across the module manifests on disk (installed-means-present). A
    deployment that DECLINED an optional module removes its manifest, so its wires drop out of this count — the
    roster-aware replacement for a hardcoded floor (#646). Pass `module_ids` to count only those modules."""
    total = 0
    for mpath in _glob.glob(os.path.join(validate.ROOT, ".engine", "modules", "*", "manifest.json")):
        if module_ids is not None and os.path.basename(os.path.dirname(mpath)) not in module_ids:
            continue
        for wire in (validate.load_json(mpath).get("wires") or []):
            if wire.get("type") == "codex-hook":
                total += 1
    return total



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

    def test_actual_exec_command_preserves_session_cwd_and_retains_command_workdir(self):
        out = providers.normalize("PreToolUse", {"tool_name": "exec_command", "tool_input": {
            "cmd": "gh issue create -t x", "workdir": "/external/project"}, "cwd": "/session/project"})
        self.assertEqual(out["tool_name"], "Bash")
        self.assertEqual(out["tool_input"]["command"], "gh issue create -t x")
        self.assertEqual(out["tool_input"]["workdir"], "/external/project")
        self.assertEqual(out["cwd"], "/session/project")


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


class TestDetectSignal(unittest.TestCase):
    """detect_signal reports WHICH signal decides detect's answer, content-free, and never changes that
    answer for anyone — it exists only so a capture marker can record why a transcript was routed."""

    def test_env_is_reported_when_the_provider_variable_is_set(self):
        with mock.patch.dict(os.environ, {"ENGINE_PROVIDER": "codex"}):
            self.assertEqual(providers.detect_signal({"turn_id": "t"}), "env")
        with mock.patch.dict(os.environ, {"ENGINE_PROVIDER": "claude"}):
            self.assertEqual(providers.detect_signal(), "env")

    def test_payload_signals_and_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(providers.detect_signal({"turn_id": "t"}), "turn_id")
            self.assertEqual(providers.detect_signal({"tool_name": "apply_patch"}), "tool_name")
            self.assertEqual(providers.detect_signal({"tool_name": "Edit"}), "default")
            self.assertEqual(providers.detect_signal({}), "default")
            self.assertEqual(providers.detect_signal(None), "default")

    def test_the_signal_never_disagrees_with_detects_verdict(self):
        cases = [{}, {"turn_id": "t"}, {"tool_name": "apply_patch"}, {"tool_name": "Edit"}, None]
        for env in ({}, {"ENGINE_PROVIDER": "codex"}, {"ENGINE_PROVIDER": "claude"}):
            for payload in cases:
                with mock.patch.dict(os.environ, env, clear=True):
                    signal, verdict = providers.detect_signal(payload), providers.detect(payload)
                    if signal == "env":
                        self.assertIn(env.get("ENGINE_PROVIDER"), ("codex", "claude"))
                    elif signal in ("turn_id", "tool_name"):
                        self.assertEqual(verdict, "codex")
                    else:
                        self.assertEqual(verdict, "claude")


class TestMarkerProviderConfinement(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(providers, "live_session_path",
                                        lambda: os.path.join(self._tmp.name, "marker.json"))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_a_claude_marker_never_resolves_a_session(self):
        """The Claude fail-safe stays historical: a Claude session always exports its env var, so
        the marker leg is CODEX-ONLY — a Claude-provider marker must resolve nothing (the mis-grant
        the review gate confined)."""
        providers.write_live_session("claude-sid", "claude")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(providers.resolve_session())

    def test_a_codex_marker_still_resolves(self):
        providers.write_live_session("codex-sid", "codex")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(providers.resolve_session(), "codex-sid")


class TestPlanCarveoutProviderConfined(unittest.TestCase):
    def test_plan_mode_opens_nothing_on_codex(self):
        """Plan mode is Claude Code's feature: a Codex payload reporting permission_mode "plan"
        (its vocabulary is unverified) must NOT open the Explore write-gate — inert BY RULE."""
        import modes
        # The Claude case carries what a real plan save carries (StarshipSuperjam/engine-template#775): a path inside the plans folder
        # (the default, with no settings file consulted, so the developer's own configuration cannot
        # steer this test). The Codex denials are the point: the same call opens nothing there.
        with mock.patch.object(modes, "_plans_settings_files", return_value=[]):
            plan_path = os.path.join(modes._plans_directory(None), "p.md")   # resolved INSIDE the seam
            self.assertTrue(modes.is_plan_artifact("Edit", {"file_path": plan_path}, "plan", None, provider="claude"))
            self.assertFalse(modes.is_plan_artifact("Edit", {"file_path": plan_path}, "plan", None, provider="codex"))
        self.assertFalse(modes.is_plan_artifact("Edit", {"is_plan_file": True}, None,
                                                provider="codex"))

    def test_the_gate_denies_a_codex_plan_mode_edit_in_explore(self):
        import contextlib
        import tempfile
        import modes
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(modes.tempfile, "gettempdir", return_value=tmp), \
                mock.patch.dict(os.environ, {"ENGINE_PROVIDER": "codex"}):
            decision = modes.handler({"session_id": "s-codex", "tool_name": "Edit",
                                      "tool_input": {}, "permission_mode": "plan"})
            self.assertEqual(decision.get("permissionDecision"), "deny",
                             "Codex has no plan mode; the carve-out must not open the gate")
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(modes.tempfile, "gettempdir", return_value=tmp), \
                mock.patch.object(modes, "_plans_settings_files", return_value=[]), \
                mock.patch.dict(os.environ, {}, clear=True):
            plan_path = os.path.join(modes._plans_directory(None), "p.md")
            decision = modes.handler({"session_id": "s-claude", "tool_name": "Edit",
                                      "tool_input": {"file_path": plan_path}, "permission_mode": "plan"})
            self.assertEqual(decision.get("action"), "proceed",
                             "the Claude plan-artifact carve-out admits the plan file itself")


class TestCodexRegistrationDrift(unittest.TestCase):
    """The renderer↔committed-literals pin the Claude side has and the Codex side was missing:
    every codex-hook command in the manifests AND in the committed .codex/hooks.json must be
    byte-identical to hooks.hook_command(provider="codex") over its own script."""

    _SHIM_RE = None

    @classmethod
    def setUpClass(cls):
        import re
        cls._SHIM_RE = re.compile(r'sh "\.engine/tools/codex-hook-runner\.sh" "([^"]+)"(.*)$')

    def _assert_rendered(self, command: str, where: str):
        m = self._SHIM_RE.search(command)
        self.assertIsNotNone(m, f"{where}: not the shim form: {command}")
        script = (m.group(1) + m.group(2)).strip()
        self.assertEqual(command, hooks.hook_command(script, provider="codex"),
                         f"{where}: committed literal drifted from the renderer")

    def test_every_manifest_codex_hook_matches_the_renderer(self):
        import glob as _glob
        import validate
        total = 0
        for mpath in sorted(_glob.glob(os.path.join(validate.ROOT, ".engine", "modules", "*",
                                                    "manifest.json"))):
            manifest = validate.load_json(mpath)
            for wire in manifest.get("wires", []):
                if wire.get("type") == "codex-hook":
                    total += 1
                    self._assert_rendered(wire["hook"]["command"], os.path.basename(os.path.dirname(mpath)))
        # The per-wire renderer-parity above is the real check and runs for whatever is installed. A `>= 0`
        # floor would be tautological (total sums the same manifests it iterates), so assert the meaningful,
        # roster-safe thing: the wiring did not vanish entirely — at least one codex-hook wire exists (core
        # always ships some), which holds in any deployment however many optional modules were declined (#646).
        self.assertGreater(total, 0, "codex-hook wires exist and were all checked")

    def test_every_committed_hooks_json_command_matches_the_renderer(self):
        import validate
        data = validate.load_json(os.path.join(validate.ROOT, ".codex", "hooks.json"))
        commands = [h["command"] for groups in data["hooks"].values()
                    for g in groups for h in g["hooks"]]
        # Roster-aware cross-check: the committed .codex/hooks.json must carry exactly the codex-hook wires the
        # INSTALLED module manifests declare — so a deployment that declined a module (fewer manifests) has a
        # correspondingly smaller hooks.json, and a drift in either direction still fails (#646).
        self.assertEqual(len(commands), _codex_wire_count(),
                         "the committed hooks.json commands must match the installed manifests' codex-hook wires")
        for command in commands:
            self._assert_rendered(command, ".codex/hooks.json")

    def test_codex_wire_count_is_installed_aware(self):
        # Proof that _codex_wire_count filters by installed module (the home repo has every module installed, so
        # the declined count is otherwise never seen): counting core alone is a strict subset of counting all
        # installed modules, so declining optional modules genuinely lowers the total (#646).
        core_only = _codex_wire_count({"core"})
        all_installed = _codex_wire_count()
        self.assertGreater(all_installed, core_only, "optional modules contribute codex-hook wires")
        self.assertGreater(core_only, 0, "core itself ships codex-hook wires — the total can never be empty")

    @staticmethod
    def _codex_commands():
        import validate
        data = validate.load_json(os.path.join(validate.ROOT, ".codex", "hooks.json"))
        return [h["command"] for groups in data["hooks"].values() for g in groups for h in g["hooks"]]

    def test_the_modes_accept_hook_is_deliberately_absent_on_codex(self):
        """Codex has no plan-exit completion to key on, so this hook cannot be registered there, and a
        future mirror-everything cleanup must trip here rather than ship it. The capability is NOT
        absent on Codex — the same import runs through the acceptance envelope on UserPromptSubmit
        (below) — so this asymmetry is one of signal, not of function. Build entry stays the typed
        verb on both runtimes either way."""
        self.assertFalse(any("modes.py" in c and "accept-hook" in c for c in self._codex_commands()),
                         "the plan-exit adapter has no Codex registration by design")

    def test_the_codex_envelope_adapter_is_registered_and_its_claude_counterpart_is_not(self):
        """The mirror of the rule above, pinned in both directions so neither side can quietly grow a
        second importer of the same accepted document."""
        import validate
        self.assertTrue(any("modes.py" in c and "plan-import-hook" in c for c in self._codex_commands()),
                        "Codex must carry the envelope adapter — it is how a plan is imported there")
        claude = validate.load_json(os.path.join(validate.ROOT, ".claude", "settings.json"))
        claude_commands = [h["command"] for groups in claude.get("hooks", {}).values()
                           for g in groups for h in g.get("hooks", [])]
        self.assertFalse(any("plan-import-hook" in c for c in claude_commands),
                         "Claude imports at plan-exit; a second importer would mint a second plan id")

    def test_the_intake_asymmetries_are_each_recorded_once_in_the_ledger(self):
        """Revised in place, not duplicated. Two entries — one per direction — and no
        third entry restating either: a ledger that accumulates near-copies of one exception stops
        being readable as a list of the differences that exist."""
        import validate
        ledger = validate.load_json(
            os.path.join(validate.ROOT, ".engine", "policies", "provider-exceptions.json"))["exceptions"]
        for identity in (".engine/tools/modes.py accept-hook", ".engine/tools/modes.py plan-import-hook"):
            matching = [e for e in ledger if e.get("kind") == "hook" and e.get("id") == identity]
            self.assertEqual(len(matching), 1, f"{identity} must appear exactly once")
            self.assertIn("import", matching[0]["reason"].lower(),
                          f"{identity}'s reason must describe what the hook now does — import a draft")

    def test_wire_apply_leaves_the_claude_files_byte_identical(self):
        """The Claude byte-stability regression the PR body cites: applying EVERY manifest wire is
        a no-op over the committed .claude/settings.json and .mcp.json."""
        import glob as _glob
        import validate
        import wiring
        settings = validate.read(wiring.SETTINGS_PATH)
        mcp = validate.read(wiring.MCP_PATH)
        directives = []
        for mpath in sorted(_glob.glob(os.path.join(validate.ROOT, ".engine", "modules", "*",
                                                    "manifest.json"))):
            directives += validate.load_json(mpath).get("wires", [])
        for f in wiring.apply_all(directives):
            self.assertNotEqual(f.get("severity"), "hard", f)
        self.assertEqual(validate.read(wiring.SETTINGS_PATH), settings)
        self.assertEqual(validate.read(wiring.MCP_PATH), mcp)


class TestParityCheckSelfIntegrity(unittest.TestCase):
    def test_hook_identity_parses_both_live_command_forms(self):
        """The parity check's private grammar is bound to the one renderer, both forms — the
        blindness canary's static half."""
        import provider_parity_check as ppc
        claude_cmd = hooks.hook_command(".engine/tools/modes.py")
        codex_cmd = hooks.hook_command(".engine/tools/modes.py", provider="codex")
        self.assertEqual(ppc._hook_identity(claude_cmd), ".engine/tools/modes.py")
        self.assertEqual(ppc._hook_identity(codex_cmd), ".engine/tools/modes.py")
        tail_claude = hooks.hook_command(".engine/tools/telemetry.py run-ambient")
        self.assertEqual(ppc._hook_identity(tail_claude), ".engine/tools/telemetry.py run-ambient")

    def test_blind_extraction_goes_loud_not_green(self):
        """A registration whose commands the grammar cannot parse must produce a broken-check
        finding, never an empty (green) comparison set."""
        import json as _json
        import tempfile
        import provider_parity_check as ppc
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, ".claude"))
            with open(os.path.join(root, ".claude", "settings.json"), "w") as fh:
                _json.dump({"hooks": {"PreToolUse": [{"matcher": "", "hooks": [
                    {"type": "command", "command": "run .engine/tools/modes.py somehow"}]}]}}, fh)
            finds = ppc.findings("hard", root=root)
            self.assertTrue(any("the check's command grammar recognized none" in f["message"]
                                for f in finds), finds)

    def test_the_exception_ledger_is_not_in_the_retirement_set(self):
        """The #411 trap, pinned: a standing check's data file must never ride the first-run
        retirement set."""
        import validate
        assets = validate.load_json(os.path.join(validate.ROOT, ".engine", "provisioning",
                                                 "first-run-assets.json"))
        everything = list(assets.get("files", [])) + list(assets.get("directories", []))
        self.assertFalse(any("provider-exceptions" in entry for entry in everything))


class TestProviderExceptionLedgerSchema(unittest.TestCase):
    def setUp(self):
        self.schema = validate.load_json(
            os.path.join(validate.ROOT, ".engine", "schemas", "provider-exceptions.v1.json"))
        self.ledger = validate.load_json(
            os.path.join(validate.ROOT, ".engine", "policies", "provider-exceptions.json"))

    def _errors(self, instance):
        return list(validate.Draft202012Validator(self.schema).iter_errors(instance))

    def test_schema_is_well_formed_and_committed_ledger_conforms(self):
        validate.Draft202012Validator.check_schema(self.schema)
        self.assertEqual(self._errors(self.ledger), [])

    def test_reason_remains_required(self):
        legacy = json.loads(json.dumps(self.ledger))
        legacy["exceptions"][0].pop("reason")
        self.assertNotEqual(self._errors(legacy), [])

    def test_retired_decision_pointer_is_rejected(self):
        legacy = json.loads(json.dumps(self.ledger))
        legacy["exceptions"][0]["contract_ref"] = "legacy policy reference"
        self.assertNotEqual(self._errors(legacy), [],
                            "the closed ledger shape no longer accepts decorative pointers")


class TestCaptureStatusPathSingleHomed(unittest.TestCase):
    def test_writer_and_readers_spell_the_same_path(self):
        """The marker is written by capture and read by boot and telemetry; three spellings, one
        path — pinned so a move cannot silently sever the disclosure chain."""
        import boot
        import telemetry
        import validate
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory"))
        from memory import capture
        expected = os.path.realpath(os.path.join(validate.ROOT, ".engine", "telemetry", ".cache",
                                                 "memory-capture.status"))
        self.assertEqual(os.path.realpath(capture.CAPTURE_STATUS_PATH), expected)
        self.assertEqual(os.path.realpath(telemetry.CAPTURE_STATUS_PATH), expected)
        # boot reads it inline; pin the literal by rendering the joined path the same way
        self.assertEqual(os.path.realpath(os.path.join(validate.ROOT, ".engine", "telemetry",
                                                       ".cache", "memory-capture.status")), expected)


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


class TestLaunchNormalization(unittest.TestCase):
    def test_captured_provider_launch_shapes_keep_their_provenance_and_unknowns(self):
        # Claude: actual project transcript tool_use, line 85, 2026-08-31T04:40:05.884Z,
        # CLI 2.1.247; source line SHA256 a5164db8db8300e05b6392e4d1decfb2ea1e63860071adaa7eb0ba21997a77e8.
        # This is a transcript projection, NOT an observed hook envelope. Session/tool fields
        # are retained; task prose and description are removed. No fresh Claude hook is claimed.
        claude = {"session_id": "57d75b64-73b9-4cf5-b477-0f3df0e307fc", "tool_name": "Agent",
                  "tool_input": {"subagent_type": "Explore", "model": "sonnet"}}
        # Codex: captured PreToolUse in the 2026-09-08 CLI 0.153.4/macOS qualification,
        # h1-hooks.jsonl, parent below; bounded original event is retained in Build evidence.
        # The top-level model is the parent model, and must never be promoted to child evidence.
        codex = {"hook_event_name": "PreToolUse", "session_id": "01a08374-15d6-7da1-9fa8-851b5701f101",
                 "model": "gpt-5.6-terra", "tool_name": "collaborationspawn_agent", "tool_input": {
                     "task_name": "child_created", "agent_type": "explorer", "fork_turns": "none",
                     "model": "gpt-5.6-luna", "reasoning_effort": "low"}}
        for provider, payload, model in (("claude", claude, "sonnet"), ("codex", codex, "gpt-5.6-luna")):
            with self.subTest(provider=provider):
                launch = providers.launch_record(payload, provider)
                self.assertEqual(launch["semantic_role"], "search")
                self.assertEqual(launch["session_id"], payload["session_id"])
                self.assertEqual(launch["requested_model"], model)
                self.assertEqual(launch["model_source"], "tool_input.model")
                self.assertIsNone(launch["effective_model"])
                self.assertIsNone(launch["effective_sandbox"])
        with mock.patch.dict(os.environ, {providers.PROVIDER_ENV: "claude"}):
            self.assertIs(providers.normalize("PreToolUse", claude), claude)
        self.assertEqual(providers.normalize("PreToolUse", codex)["provider_launch"]["requested_effort"], "low")

    def test_observed_spawn_preserves_identity_and_never_borrows_parent_model(self):
        payload = {"tool_name": "collaborationspawn_agent", "model": "gpt-6-astra",
                   "session_id": "parent:exact/id", "tool_input": {
                       "agent_type": "explorer", "fork_turns": "none", "message": "private task"}}
        out = providers.normalize("PreToolUse", payload)
        self.assertEqual(out["tool_name"], "Agent")
        self.assertEqual(out["tool_input"]["subagent_type"], "Explore")
        launch = out["provider_launch"]
        self.assertEqual(launch["semantic_role"], "search")
        self.assertEqual(launch["session_id"], "parent:exact/id")
        self.assertIsNone(launch["requested_model"])
        self.assertIsNone(launch["effective_model"])
        self.assertIn("requested_model", launch["unknown_fields"])
        self.assertNotIn("private task", json.dumps(out["provider_raw"]))
        self.assertNotIn("subagent_type", payload["tool_input"])

    def test_explicit_settings_have_provenance_not_effective_claims(self):
        payload = {"tool_name": "spawn_agent", "session_id": "s", "tool_input": {
            "agent_type": "explorer", "model": "gpt-5.6-luna", "reasoning_effort": "low",
            "sandbox_mode": "read-only", "fork_turns": "none"}}
        launch = providers.launch_record(payload, "codex")
        self.assertEqual(launch["model_source"], "tool_input.model")
        self.assertEqual(launch["requested_model"], "gpt-5.6-luna")
        self.assertEqual(launch["requested_effort"], "low")
        self.assertEqual(launch["sandbox_intent"], "read-only")
        self.assertIsNone(launch["effective_sandbox"])
        self.assertIsNone(launch["recursion_limit"])

    def test_prose_and_task_name_do_not_classify_an_unknown_agent(self):
        payload = {"tool_name": "spawn_agent", "tool_input": {
            "task_name": "Explore", "message": "Use model gpt-5.6-luna to plan.", "agent_type": "custom"}}
        launch = providers.launch_record(payload, "codex")
        self.assertEqual(launch["semantic_role"], "unclassified")
        self.assertIsNone(launch["requested_model"])
        self.assertIsNone(providers.launch_record({"tool_name": "future_spawn"}, "codex"))

    def test_claude_launch_remains_identity_and_has_a_separate_record(self):
        payload = {"tool_name": "Agent", "tool_input": {"subagent_type": "Plan", "model": "haiku"}}
        with mock.patch.dict(os.environ, {providers.PROVIDER_ENV: "claude"}):
            self.assertIs(providers.normalize("PreToolUse", payload), payload)
            launch = providers.launch_record(payload)
        self.assertEqual(launch["semantic_role"], "plan")
        self.assertEqual(launch["requested_model"], "haiku")

    def test_malformed_tool_fields_stay_unknown(self):
        for value in (None, [], 1):
            payload = {"tool_name": value}
            self.assertIs(providers.normalize("PreToolUse", payload), payload)
            self.assertIsNone(providers.launch_record(payload, "codex"))


class TestScopedAgentBaseline(unittest.TestCase):
    """Sanitized September 9 Desktop envelopes, distinct from Claude documentation fixtures.

    These exercise the real normalization boundary, not a simulated native allocator. Replaced
    identities and paths preserve the observed parent/child split; no private task bodies are kept.
    The local probe's packets/witness flags were instrumentation, not native payload fields.
    """

    def test_desktop_launch_preserves_correlation_fields_on_both_tool_events(self):
        for event in ("PreToolUse", "PostToolUse"):
            payload = {"hook_event_name": event, "session_id": "parent", "turn_id": "turn",
                       "tool_use_id": "call-launch", "tool_name": "collaborationspawn_agent",
                       "tool_input": {"agent_type": "default", "task_name": "review_a",
                                      "fork_turns": "none", "message": "synthetic packet path"}}
            result = providers.normalize(event, payload)
            self.assertEqual(result["tool_name"], "Agent")
            self.assertEqual(result["session_id"], "parent")
            self.assertEqual(result["tool_use_id"], "call-launch")
            self.assertEqual(result["provider_launch"]["fork_context"], "none")
            self.assertNotIn("agent_id", result)  # a request does not establish the actual child

    def test_desktop_child_read_does_not_replace_parent_session_with_child_id(self):
        payload = {"session_id": "parent", "agent_id": "child-a", "agent_type": "default",
                   "turn_id": "child-turn", "tool_name": "Bash", "tool_use_id": "exec-read",
                   "tool_input": {"command": "cat /fixture/packets/A.json"},
                   "tool_response": "synthetic packet content"}
        result = providers.normalize("PostToolUse", payload)
        self.assertEqual((result["session_id"], result["agent_id"]), ("parent", "child-a"))
        self.assertEqual(result["tool_response"], "synthetic packet content")
        self.assertEqual(result["tool_use_id"], "exec-read")

    def test_desktop_control_tools_are_distinct_from_launches(self):
        for tool in ("collaborationsend_message", "collaborationfollowup_task"):
            payload = {"turn_id": "turn", "session_id": "parent", "tool_name": tool,
                       "tool_input": {"target": "review_a", "message": "synthetic"}}
            self.assertEqual(providers.detect(payload), "codex")
            self.assertIsNone(providers.launch_record(payload, "codex"))
            result = providers.normalize("PreToolUse", payload)
            self.assertEqual(result["tool_input"]["target"], "review_a")

    def test_claude_documented_child_fields_remain_identity_without_codex_turn_id(self):
        # Official hooks reference: common session_id plus agent_id/agent_type on child tools.
        # Documentation contract only; no live Claude run is represented by this fixture.
        with mock.patch.dict(os.environ, {providers.PROVIDER_ENV: "claude"}):
            for event in ("PreToolUse", "PostToolUse", "SubagentStart", "SubagentStop"):
                payload = {"hook_event_name": event, "session_id": "parent", "agent_id": "child",
                           "agent_type": "engine-design-review-architecture"}
                self.assertIs(providers.normalize(event, payload), payload)
                self.assertEqual(providers.detect(payload), "claude")
            message = {"tool_name": "SendMessage", "tool_input": {
                "type": "message", "recipient": "child", "content": "synthetic clarification"}}
            self.assertIs(providers.normalize("PreToolUse", message), message)
            self.assertIsNone(providers.launch_record(message))


class TestReviewReaderEvidence(unittest.TestCase):
    def test_77696_byte_packet_requires_more_than_a_capped_response(self):
        # A reported incident shape, not a claim about any universal provider cap.
        packet = "x" * 77695 + "\n"
        for cap in (16384, 66000, 70000):
            payload = {"tool_name": "Read", "tool_response": packet[:cap]}
            self.assertFalse(providers.scoped_read_succeeded(payload, packet))
        self.assertTrue(providers.scoped_read_succeeded(
            {"tool_name": "Read", "tool_response": packet}, packet))

    def test_only_exact_complete_successful_reader_output_counts(self):
        import copy
        import hashlib
        text = "frozen packet\n"
        result = {"file_path": "/packets/a.md", "content": text, "complete": True,
                  "offset": 0, "sha256": "sha256:" + hashlib.sha256(text.encode()).hexdigest()}
        payload = {"tool_name": "mcp__engine-review-reader__read_file",
                   "tool_input": {"path": "/packets/a.md"},
                   "tool_response": {"content": [{"type": "text", "text": json.dumps(result)}],
                                     "isError": False}}
        self.assertTrue(providers.scoped_reads_path(payload, "/packets/a.md"))
        self.assertTrue(providers.scoped_read_succeeded(payload, text))
        for key, value in (("file_path", "/other"), ("complete", False), ("offset", 1),
                           ("sha256", "wrong"), ("content", "partial")):
            bad = copy.deepcopy(payload)
            bad["tool_response"]["content"][0]["text"] = json.dumps({**result, key: value})
            self.assertFalse(providers.scoped_read_succeeded(bad, text), key)
        bad = copy.deepcopy(payload)
        bad["tool_response"]["isError"] = True
        self.assertFalse(providers.scoped_read_succeeded(bad, text))
        bad = copy.deepcopy(payload)
        bad["tool_name"] = "mcp__unrelated__read_file"
        self.assertFalse(providers.scoped_read_succeeded(bad, text))
        self.assertFalse(providers.scoped_reads_path(payload, "/packets/b.md"))


class ScopedControlReconciliation(unittest.TestCase):
    def setUp(self):
        self.initial = self.envelope("initial opaque")
        self.transcript = {"child": "child", "root": "root", "name": "/root/assignment", "messages": [self.initial]}
        self.continuation = {"sender": "root", "recipient": "child", "content": "opaque", "dispatched": True}
        self.message = self.envelope("opaque")

    @staticmethod
    def envelope(content):
        return {"author": "/root", "recipient": "/root/assignment", "content": [
            {"type": "input_text", "text": "Same native header for initial and continuation"},
            {"type": "encrypted_content", "encrypted_content": content}]}

    def verified(self, continuations=()):
        return providers.scoped_control_verified(self.transcript, root="root", child="child",
            name="assignment", launch_digest=providers.scoped_control_digest("initial opaque"),
            continuations=list(continuations))

    def test_initial_launch_and_one_exact_continuation(self):
        self.assertTrue(self.verified())
        self.transcript["messages"] = [self.initial, self.message]
        self.assertTrue(self.verified([self.continuation]))
        self.assertFalse(self.verified())
        self.transcript["messages"] = [self.initial]
        self.assertFalse(self.verified([self.continuation]))
        self.transcript["messages"] = []
        self.assertFalse(self.verified())
        self.transcript["messages"] = [self.message]
        self.assertFalse(self.verified())  # arbitrary first message cannot stand in for launch

    def test_duplicate_unknown_wrong_actor_and_missing_send_fail(self):
        import copy
        for change in (lambda m: m.update(author="/root/peer"),
                       lambda m: m.update(recipient="/root/other"),
                       lambda m: m.update(content=[]),
                       lambda m: m.update(content=[{"type": "unknown", "text": "opaque"}]),
                       lambda m: m.update(content=[*m["content"], *m["content"]])):
            message = copy.deepcopy(self.message)
            change(message)
            self.transcript["messages"] = [self.initial, message]
            self.assertFalse(self.verified([self.continuation]))
        self.transcript["messages"] = [self.initial, self.message, self.message]
        self.assertFalse(self.verified([self.continuation, self.continuation]))
        self.transcript["messages"] = [self.message, self.initial]
        self.assertFalse(self.verified([self.continuation]))
        self.transcript["messages"] = [self.initial, self.message]
        self.assertFalse(self.verified([{**self.continuation, "dispatched": False}]))
        self.assertFalse(self.verified([{**self.continuation, "sender": "other"}]))
        self.transcript["child"] = "wrong"
        self.assertFalse(self.verified([self.continuation]))

    def test_parser_refuses_duplicate_actor_metadata_and_malformed_payload(self):
        import tempfile
        from pathlib import Path
        meta = {"type": "session_meta", "payload": {"id": "child", "source": {"subagent": {
            "thread_spawn": {"parent_thread_id": "root", "agent_path": "/root/assignment"}}}}}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "child.jsonl"
            for rows in ([meta, meta], [meta, {"type": "response_item", "payload": []}],
                         [{"type": "session_meta", "payload": {"source": {"subagent": "bad"}}}]):
                path.write_text("\n".join(json.dumps(row) for row in rows))
                self.assertEqual(providers.scoped_transcript({"transcript_path": str(path)}, providers.CODEX), {})

    def test_metadata_only_reads_bounded_first_header(self):
        import tempfile
        from pathlib import Path
        meta = {"type": "session_meta", "payload": {"id": "child", "source": {"subagent": {
            "thread_spawn": {"parent_thread_id": "root", "agent_path": "/root/assignment"}}}}}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "huge.jsonl"
            path.write_text(json.dumps(meta) + "\n" + "not json\n" + ("x" * (4 * 1024 * 1024)))
            self.assertEqual(providers.scoped_transcript({"transcript_path": str(path)}, providers.CODEX,
                             metadata_only=True)["name"], "/root/assignment")
            path.write_bytes(json.dumps(meta).encode() + b'\n\xff\xfe')
            self.assertEqual(providers.scoped_transcript({"transcript_path": str(path)}, providers.CODEX,
                             metadata_only=True)["child"], "child")
            self.assertEqual(providers.scoped_transcript({"transcript_path": str(path)}, providers.CLAUDE,
                             metadata_only=True), {})
            meta['payload']['id'] = ' '
            path.write_text(json.dumps(meta) + '\n')
            self.assertEqual(providers.scoped_transcript({"transcript_path": str(path)}, providers.CODEX,
                             metadata_only=True), {})

    def test_metadata_only_rejects_oversized_or_malformed_header_and_full_rejects_contradiction(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bad.jsonl"
            path.write_text("{" + "x" * (64 * 1024) + "\n")
            self.assertEqual(providers.scoped_transcript({"transcript_path": str(path)}, providers.CODEX,
                             metadata_only=True), {})
            path.write_text(json.dumps({'padding': 'é' * 40000}, ensure_ascii=False) + '\n')
            self.assertEqual(providers.scoped_transcript({"transcript_path": str(path)}, providers.CODEX,
                             metadata_only=True), {})
            meta = {"type": "session_meta", "payload": {"id": "child", "source": {"subagent": {
                "thread_spawn": {"parent_thread_id": "root", "agent_path": "/root/assignment"}}}}}
            other = {"type": "session_meta", "payload": {"id": "other", "source": {"subagent": {
                "thread_spawn": {"parent_thread_id": "root", "agent_path": "/root/other"}}}}}
            path.write_text("\n".join(json.dumps(x) for x in (meta, other)))
            self.assertEqual(providers.scoped_transcript({"transcript_path": str(path)}, providers.CODEX), {})

    def test_full_transcript_stream_closes_on_early_refusal(self):
        import io
        from pathlib import Path
        from unittest import mock
        stream = io.StringIO('[]\n')
        with mock.patch.object(Path, 'open', return_value=stream):
            self.assertEqual(providers.scoped_transcript({'transcript_path':'unused'}, providers.CODEX), {})
        self.assertTrue(stream.closed)

    def test_capacity_error_requires_exact_native_parent_call_and_result(self):
        import tempfile
        from pathlib import Path
        import copy
        args = {'task_name':'assignment', 'message':'opaque', 'agent_type':'role', 'fork_turns':'none'}
        rows = [
            {'type':'session_meta','payload':{'id':'root'}},
            {'type':'response_item','payload':{'type':'function_call','name':'spawn_agent',
                'namespace':'collaboration','call_id':'failed','arguments':json.dumps(args)}},
            {'type':'response_item','payload':{'type':'function_call_output','call_id':'failed',
                'output':'collab spawn failed: agent thread limit reached'}}]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'parent.jsonl'
            payload = {'session_id':'root','transcript_path':str(path)}
            def check(values):
                path.write_text('\n'.join(json.dumps(r) for r in values)+'\n')
                return providers.scoped_capacity_rejection_from_transcript(payload,'failed')
            self.assertEqual(check(rows)['input'],args)
            self.assertEqual(check(rows)['response'],rows[-1]['payload']['output'])
            for index,key,value in ((0,'id','other'),(1,'name','send_message'),(1,'namespace','other'),
                                    (2,'output','transport failed'),(2,'call_id','other')):
                bad=copy.deepcopy(rows);bad[index]['payload'][key]=value
                self.assertEqual(check(bad),{})
            for bad in (rows[:-1], rows+[rows[-1]], [rows[0],rows[2],rows[1]], rows+[rows[1]]):
                self.assertEqual(check(bad),{})


if __name__ == "__main__":
    unittest.main()
