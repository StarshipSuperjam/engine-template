#!/usr/bin/env python3
"""Self-tests for the hooks contract substrate (core slice 17): the closed event inventory, the block
budget + block cap, the per-OS interpreter-path resolver, the fail-open-and-flag harness, and the pure
block-budget coherence leg (validate.block_budget_findings).

Run: uv run --directory .engine -- python -m unittest discover -s tools -p 'test_*.py'

These lock the laws hooks owns (systems/infrastructure/hooks/README.md):
  - the event inventory is the engine's chosen subset, with PostToolUse two-owner, SessionEnd hooks-owned
    and non-blocking, UserPromptSubmit boot-owned injection; only PreToolUse and Stop are block-eligible;
    the block-eligible invariant set ships EMPTY.
  - the block cap is 8, overridable via CLAUDE_CODE_STOP_HOOK_BLOCK_CAP (verified on the live platform).
  - the interpreter path is ${CLAUDE_PROJECT_DIR}-rooted, per-OS (POSIX bin/python, Windows Scripts/
    python.exe), never bare python / uv run.
  - the harness FAILS OPEN: a crashing handler, a malformed event payload, or a block requested on a
    non-eligible event all PROCEED (a non-2 exit) and emit a plain-language finding — never a hard block;
    only a handler that returns block() on PreToolUse/Stop exits 2; on a forced Stop continuation
    (stop_hook_active) the handler STILL runs but its block is downgraded to proceed, so it can never
    re-block and loop the cap (slice 22 — close needs the give-up moment to log; the guarantee is the
    harness's, by construction, not the handler's).
  - the static block-budget leg flags a block declared on a non-eligible event, is silent on an empty set,
    and agrees with the runtime BLOCK_ELIGIBLE_EVENTS (a drift guard). The leg is built + fixture-tested
    with no live rule (the interface_resolution_findings / agent_coherence_findings precedent); the live
    rule wires at the first hook-wiring slice (20).
"""
from __future__ import annotations
import contextlib
import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hooks     # noqa: E402
import validate  # noqa: E402


def _run(event, handler, payload=None, stdin_text=None):
    """Drive the real run_hook with captured streams. Returns (exit_code, stdout, stderr)."""
    if stdin_text is None:
        stdin_text = json.dumps(payload or {})
    out, err = io.StringIO(), io.StringIO()
    code = hooks.run_hook(event, handler, stdin=io.StringIO(stdin_text), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


class TestEventInventory(unittest.TestCase):
    def test_the_seven_governed_events(self):
        self.assertEqual(hooks.EVENTS, {
            "SessionStart", "PreToolUse", "PostToolUse", "PreCompact",
            "Stop", "SessionEnd", "UserPromptSubmit"})

    def test_only_pretooluse_and_stop_are_block_eligible(self):
        self.assertEqual(hooks.BLOCK_ELIGIBLE_EVENTS, {"PreToolUse", "Stop"})
        for ev, meta in hooks.EVENT_INVENTORY.items():
            self.assertEqual(meta["blocks"], ev in {"PreToolUse", "Stop"},
                             f"{ev} block-eligibility")

    def test_posttooluse_has_two_owners(self):
        self.assertEqual(hooks.EVENT_INVENTORY["PostToolUse"]["owners"], ("validation", "telemetry"))

    def test_sessionend_is_hooks_owned_and_cannot_block(self):
        self.assertEqual(hooks.EVENT_INVENTORY["SessionEnd"]["owners"], ("hooks",))
        self.assertFalse(hooks.EVENT_INVENTORY["SessionEnd"]["blocks"])

    def test_userpromptsubmit_is_boot_owned_injection(self):
        self.assertEqual(hooks.EVENT_INVENTORY["UserPromptSubmit"]["owners"], ("boot",))
        self.assertTrue(hooks.EVENT_INVENTORY["UserPromptSubmit"]["injects"])

    def test_block_eligible_invariant_set_starts_empty(self):
        self.assertEqual(hooks.BLOCK_ELIGIBLE_INVARIANTS, ())


class TestBlockCap(unittest.TestCase):
    def test_cap_is_eight_with_the_platform_env_override(self):
        self.assertEqual(hooks.STOP_HOOK_BLOCK_CAP, 8)
        self.assertEqual(hooks.STOP_HOOK_BLOCK_CAP_ENV, "CLAUDE_CODE_STOP_HOOK_BLOCK_CAP")


class TestInterpreterPath(unittest.TestCase):
    def test_posix_form(self):
        self.assertEqual(hooks.interpreter_path("posix"),
                         "${CLAUDE_PROJECT_DIR}/.engine/.venv/bin/python")

    def test_windows_form(self):
        self.assertEqual(hooks.interpreter_path("nt"),
                         "${CLAUDE_PROJECT_DIR}/.engine/.venv/Scripts/python.exe")

    def test_is_project_dir_rooted_and_never_bare(self):
        for name in ("posix", "nt"):
            p = hooks.interpreter_path(name)
            self.assertTrue(p.startswith("${CLAUDE_PROJECT_DIR}/.engine/.venv/"))
            self.assertNotIn("uv ", p)
            self.assertNotEqual(p, "python")

    def test_hook_command_joins_interpreter_and_rooted_script(self):
        cmd = hooks.hook_command("tools/some_hook.py", "posix")
        self.assertEqual(
            cmd, "${CLAUDE_PROJECT_DIR}/.engine/.venv/bin/python ${CLAUDE_PROJECT_DIR}/tools/some_hook.py")


class TestHarnessBlock(unittest.TestCase):
    def test_block_on_pretooluse_exits_two_with_reason_on_stderr(self):
        code, out, err = _run("PreToolUse", lambda p: hooks.block("finish first"))
        self.assertEqual(code, hooks.EXIT_BLOCK)
        self.assertEqual(code, 2)
        self.assertIn("finish first", err)
        self.assertEqual(out, "")

    def test_block_on_stop_exits_two(self):
        code, _out, _err = _run("Stop", lambda p: hooks.block("not done"))
        self.assertEqual(code, 2)

    def test_block_on_non_eligible_event_fails_open_and_flags(self):
        code, _out, err = _run("PostToolUse", lambda p: hooks.block("I cannot block here"))
        self.assertNotEqual(code, hooks.EXIT_BLOCK)
        self.assertEqual(code, hooks.EXIT_NONBLOCKING)
        self.assertIn("only", err)
        self.assertIn("PostToolUse", err)

    def test_block_on_every_non_eligible_event_fails_open(self):
        """The runtime gate (not just the static leg) must refuse a block on EVERY non-eligible event,
        so a _translate bug on an event other than PostToolUse cannot fail-closed."""
        for ev in sorted(hooks.EVENTS - hooks.BLOCK_ELIGIBLE_EVENTS):
            code, _out, _err = _run(ev, lambda p: hooks.block("should not block"))
            self.assertNotEqual(code, hooks.EXIT_BLOCK, f"{ev} must not honor a block")
            self.assertEqual(code, hooks.EXIT_NONBLOCKING, f"{ev} should fail open")


class TestHarnessFailOpen(unittest.TestCase):
    def test_a_crashing_handler_proceeds_and_flags_never_blocks(self):
        def boom(_payload):
            raise RuntimeError("kaboom")
        code, _out, err = _run("PreToolUse", boom)
        self.assertNotEqual(code, hooks.EXIT_BLOCK)
        self.assertEqual(code, hooks.EXIT_NONBLOCKING)
        self.assertTrue(err.strip(), "a fail-open crash must emit a plain-language finding")
        self.assertNotIn("Traceback", err)

    def test_no_handler_proceeds(self):
        code, out, err = _run("Stop", None)
        self.assertEqual(code, hooks.EXIT_PROCEED)
        self.assertEqual(out, "")
        self.assertEqual(err, "")

    def test_handler_calling_sys_exit_fails_open_never_fails_closed(self):
        """A handler that reaches past the decision protocol and calls sys.exit(2) must STILL fail
        open — the harness owns the exit code, so a handler bug can never fail-closed."""
        def rogue(_payload):
            sys.exit(2)
        for ev in ("PostToolUse", "PreToolUse", "Stop"):
            code, _out, err = _run(ev, rogue)
            self.assertNotEqual(code, hooks.EXIT_BLOCK, f"{ev}: sys.exit(2) must not become a block")
            self.assertEqual(code, hooks.EXIT_NONBLOCKING, f"{ev}: should fail open")
            self.assertTrue(err.strip())


class TestStopHookActive(unittest.TestCase):
    def test_forced_continuation_runs_handler_but_never_reblocks(self):
        # Slice 22 (deliberate law change from slice 17's skip-the-handler): on a forced continuation the
        # handler STILL runs — close uses the give-up moment to log a still-undispositioned finding — but
        # its block is downgraded to proceed in run_hook, so the no-re-block / no-loop guarantee holds by
        # construction (the harness owns it, not the handler).
        called = []

        def would_block(_payload):
            called.append(True)
            return hooks.block("disposition still open")
        code, _out, err = _run("Stop", would_block, payload={"stop_hook_active": True})
        self.assertEqual(code, hooks.EXIT_PROCEED)   # downgraded to proceed — never re-blocks
        self.assertEqual(called, [True])             # ...but the handler DID run (its side effects fire)
        self.assertEqual(err, "")                    # and no block reason was emitted

    def test_forced_continuation_proceed_passes_through(self):
        code, _out, _err = _run("Stop", lambda p: hooks.proceed(), payload={"stop_hook_active": True})
        self.assertEqual(code, hooks.EXIT_PROCEED)

    def test_forced_continuation_handler_crash_fails_open(self):
        # The give-up handler itself crashing must fail open (the turn ends) and flag — never block.
        def boom(_payload):
            raise RuntimeError("give-up handler crashed")
        code, _out, err = _run("Stop", boom, payload={"stop_hook_active": True})
        self.assertEqual(code, hooks.EXIT_NONBLOCKING)   # non-blocking → the turn ends, never strands
        self.assertIn("could not run", err)              # ...and the failure is surfaced as a finding

    def test_normal_stop_still_blocks(self):
        code, _out, _err = _run("Stop", lambda p: hooks.block("nope"),
                                payload={"stop_hook_active": False})
        self.assertEqual(code, 2)


class TestMalformedAndEmptyPayload(unittest.TestCase):
    def test_malformed_stdin_fails_open(self):
        code, _out, err = _run("PreToolUse", lambda p: hooks.block("x"), stdin_text="{not json")
        self.assertNotEqual(code, hooks.EXIT_BLOCK)
        self.assertEqual(code, hooks.EXIT_NONBLOCKING)
        self.assertTrue(err.strip())

    def test_empty_stdin_is_an_empty_payload(self):
        code, _out, _err = _run("PreToolUse", lambda p: hooks.proceed(), stdin_text="")
        self.assertEqual(code, hooks.EXIT_PROCEED)

    def test_non_object_json_is_treated_as_empty(self):
        seen = {}

        def handler(payload):
            seen["payload"] = payload
            return hooks.proceed()
        code, _out, _err = _run("PreToolUse", handler, stdin_text="[1, 2, 3]")
        self.assertEqual(code, hooks.EXIT_PROCEED)
        self.assertEqual(seen["payload"], {})


class TestInjectAndDecide(unittest.TestCase):
    def test_inject_emits_additional_context(self):
        code, out, _err = _run("SessionStart", lambda p: hooks.inject("orientation pack"))
        self.assertEqual(code, hooks.EXIT_PROCEED)
        payload = json.loads(out)
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertEqual(payload["hookSpecificOutput"]["additionalContext"], "orientation pack")

    def test_pretooluse_permission_decision(self):
        code, out, _err = _run("PreToolUse", lambda p: hooks.decide("deny", "blocked by gate"))
        self.assertEqual(code, hooks.EXIT_PROCEED)
        hso = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(hso["permissionDecision"], "deny")
        self.assertEqual(hso["permissionDecisionReason"], "blocked by gate")

    def test_permission_decision_without_reason_omits_the_reason_key(self):
        code, out, _err = _run("PreToolUse", lambda p: hooks.decide("allow"))
        self.assertEqual(code, hooks.EXIT_PROCEED)
        hso = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(hso["permissionDecision"], "allow")
        self.assertNotIn("permissionDecisionReason", hso)

    def test_permission_decision_on_non_pretooluse_is_flagged(self):
        code, _out, err = _run("Stop", lambda p: hooks.decide("deny"))
        self.assertEqual(code, hooks.EXIT_NONBLOCKING)
        self.assertTrue(err.strip())

    def test_invalid_permission_value_is_flagged(self):
        code, _out, err = _run("PreToolUse", lambda p: hooks.decide("maybe"))
        self.assertEqual(code, hooks.EXIT_NONBLOCKING)
        self.assertTrue(err.strip())

    def test_proceed_is_silent(self):
        code, out, err = _run("PostToolUse", lambda p: hooks.proceed())
        self.assertEqual(code, hooks.EXIT_PROCEED)
        self.assertEqual(out, "")
        self.assertEqual(err, "")


class TestBlockBudgetFindings(unittest.TestCase):
    MSG = "Register the block with its owning system on an eligible event."

    def test_empty_set_is_silent(self):
        self.assertEqual(validate.block_budget_findings([], "hard", self.MSG), [])

    def test_eligible_events_pass(self):
        blocks = [{"event": "Stop", "name": "findings-disposition", "owner": "close"},
                  {"event": "PreToolUse", "name": "explore-write-gate", "owner": "modes"}]
        self.assertEqual(validate.block_budget_findings(blocks, "hard", self.MSG), [])

    def test_non_eligible_event_is_flagged(self):
        found = validate.block_budget_findings(
            [{"event": "PostToolUse", "name": "bad"}], "hard", self.MSG)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["severity"], "hard")
        self.assertIn("PostToolUse", found[0]["message"])
        self.assertIn(self.MSG, found[0]["message"])

    def test_owner_is_used_when_name_absent(self):
        found = validate.block_budget_findings(
            [{"event": "PreCompact", "owner": "memory"}], "soft", self.MSG)
        self.assertEqual(len(found), 1)
        self.assertIn("memory", found[0]["message"])
        self.assertEqual(found[0]["severity"], "soft")

    def test_agrees_with_runtime_block_eligible_events(self):
        """Drift guard: for every governed event, the static leg flags a block on it iff the event is
        outside the runtime's BLOCK_ELIGIBLE_EVENTS constant — the leg's own {PreToolUse, Stop} literal
        and the harness's eligibility constant cannot drift. (The runtime _translate gate itself is
        exercised in TestHarnessBlock.test_block_on_every_non_eligible_event_fails_open.)"""
        for ev in hooks.EVENTS:
            findings = validate.block_budget_findings([{"event": ev, "name": ev}], "hard", "")
            flagged = bool(findings)
            self.assertEqual(flagged, ev not in hooks.BLOCK_ELIGIBLE_EVENTS, f"{ev} agreement")


class TestDemoRuns(unittest.TestCase):
    def test_demo_executes_cleanly(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = hooks.main(["demo"])
        self.assertEqual(code, 0)
        self.assertIn("fail-open", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
