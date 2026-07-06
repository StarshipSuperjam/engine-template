#!/usr/bin/env python3
"""Self-tests for the hooks contract substrate (core slice 17): the closed event inventory, the block
budget + block cap, the per-OS interpreter-path resolver, the fail-open-and-flag harness, and the pure
block-budget coherence leg (validate.block_budget_findings).

Run: uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py' -b

These lock the laws hooks owns (systems/infrastructure/hooks/README.md):
  - the event inventory is the engine's chosen subset, with PostToolUse three-owner (validation·telemetry·
    modes), SessionEnd hooks-owned and non-blocking, UserPromptSubmit boot-owned injection; only PreToolUse
    and Stop are block-eligible;
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
import shutil
import subprocess
import sys
import tempfile
import threading
import time
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

    def test_posttooluse_enumerates_its_three_owners(self):
        # validation's touched-file run + telemetry's ambient capture + modes' plan-acceptance
        # Build-entry trigger coexist on one event (D-180 owner inventory).
        self.assertEqual(hooks.EVENT_INVENTORY["PostToolUse"]["owners"],
                         ("validation", "telemetry", "modes"))

    def test_posttooluse_may_inject_and_stays_non_blocking(self):
        # D-270/D-271: modes' acceptance trigger injects an assistant-internal stance directive
        # (additionalContext) on Build entry, so PostToolUse may inject — but it never blocks.
        self.assertTrue(hooks.EVENT_INVENTORY["PostToolUse"]["injects"])
        self.assertFalse(hooks.EVENT_INVENTORY["PostToolUse"]["blocks"])

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

    def test_hook_command_calls_the_launcher_with_the_explicit_interpreter(self):
        # The form is now a call to the hook launcher (.engine/tools/hook-runner.sh) with the explicit
        # ${CLAUDE_PROJECT_DIR}-rooted venv interpreter named as its first argument, then the
        # ${CLAUDE_PROJECT_DIR}-rooted script. The wait/exec mechanics live in the launcher; the command
        # stays legible. Byte-exact so a drift is caught.
        # The script PATH token is double-quoted (#390) so a spaced project dir does not word-split; the
        # interpreter and launcher tokens have always been quoted. Hand-derived to the intended form.
        self.assertEqual(
            hooks.hook_command("tools/some_hook.py", "posix"),
            'sh "${CLAUDE_PROJECT_DIR}/.engine/tools/hook-runner.sh" '
            '"${CLAUDE_PROJECT_DIR}/.engine/.venv/bin/python" "${CLAUDE_PROJECT_DIR}/tools/some_hook.py"')


class TestHookCommandWaitWrapper(unittest.TestCase):
    """The wait/exec mechanics moved from the inline command into the committed launcher
    (.engine/tools/hook-runner.sh) so the displayed command is legible, NOT a wall of shell. The launcher
    keeps exactly the fresh-worktree-race behaviour (issue #83): bounded wait, exec-only-the-given-venv-
    interpreter, never a system-Python fallback, args preserved, and the live wait/degrade behaviour."""

    WRAPPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hook-runner.sh")

    def test_the_command_is_legible_not_a_wall_of_shell(self):
        # the presentation fix: the displayed command no longer carries shell control-flow — it just calls
        # the launcher. The wall (while/done/exec/sleep/loop-arithmetic) lives in hook-runner.sh now.
        cmd = hooks.hook_command(".engine/tools/boot.py", "posix")
        self.assertIn("hook-runner.sh", cmd)
        for control in ("while", "done", "exec", "sleep", "n=$(("):
            self.assertNotIn(control, cmd)

    def test_command_names_the_explicit_venv_interpreter_never_system_python(self):
        # the conformance witness (D-156): the explicit ${CLAUDE_PROJECT_DIR}-rooted venv interpreter is
        # named IN the command (the launcher's first arg), never a bare/system interpreter or `uv run`.
        cmd = hooks.hook_command(".engine/tools/boot.py", "posix")
        self.assertIn(f'"{hooks.interpreter_path("posix")}"', cmd)
        self.assertNotIn("exec python", cmd)
        self.assertNotIn("uv ", cmd)
        self.assertNotIn("/usr/bin/", cmd)
        self.assertNotIn("/usr/local/bin/", cmd)

    def test_the_launcher_waits_bounded_and_execs_only_the_given_interpreter(self):
        # the launcher source: a bounded (not infinite) wait, then a single exec of ONLY the passed
        # interpreter with the forwarded args — never a bare/system Python fallback.
        with open(self.WRAPPER) as fh:
            src = fh.read()
        self.assertIn("while", src)
        self.assertIn("-lt", src)                       # a numeric cap, never an unbounded loop
        self.assertIn("shift", src)                     # the interpreter arg is consumed, so "$@" = script+args
        self.assertIn('exec "$interp" "$@"', src)       # one exec, of the passed interpreter, args forwarded
        self.assertEqual(src.count("exec "), 1)
        for forbidden in ("uv ", "/usr/bin/", "/usr/local/bin/", "exec python"):
            self.assertNotIn(forbidden, src)

    def test_per_os_form_carries_its_own_venv_interpreter(self):
        self.assertIn(".engine/.venv/bin/python",
                      hooks.hook_command(".engine/tools/boot.py", "posix"))
        self.assertIn(".engine/.venv/Scripts/python.exe",
                      hooks.hook_command(".engine/tools/boot.py", "nt"))

    def test_trailing_args_stay_bare_words_after_the_quoted_path(self):
        # the footgun guard, post-#390: the script PATH is now double-quoted, but the arg word (` hook` /
        # ` accept-hook`) stays OUTSIDE the quotes as the final, word-splittable token — so it still reaches
        # the launcher as its own positional param. The two conditions together (quoted path, bare arg) are
        # exactly what makes both a spaced project dir AND arg-passing work.
        kg = hooks.hook_command(".engine/tools/knowledge_gen.py hook", "posix")
        self.assertTrue(kg.rstrip().endswith('knowledge_gen.py" hook'), kg)   # path quoted, arg bare
        modes = hooks.hook_command(".engine/tools/modes.py accept-hook", "posix")
        self.assertTrue(modes.rstrip().endswith('modes.py" accept-hook'), modes)

    def test_spaced_project_dir_delivers_the_intact_script_path_and_arg(self):
        # #390 regression, driven through the REAL committed launcher and the REAL `sh -c` substitution:
        # a project directory whose path contains a space used to word-split the UNQUOTED script tail, so the
        # launcher forwarded a truncated path, python exited 2, and the platform read that exit-2 as a
        # fail-CLOSED BLOCK on every tool call and turn-end. This runs the rendered command under `sh -c`
        # with a spaced ${CLAUDE_PROJECT_DIR} and an ARG-BEARING wire, and asserts the interpreter receives
        # the WHOLE spaced path as ONE argument plus the arg. It FAILS on the pre-#390 unquoted form (the
        # path would split into two args, yielding three output lines), which is what makes it a real
        # falsification rather than a string-shape assertion.
        with tempfile.TemporaryDirectory() as base:
            proj = os.path.join(base, "my project")                     # the space is the whole point
            tools_dir = os.path.join(proj, ".engine", "tools")
            venv_bin = os.path.join(proj, ".engine", ".venv", "bin")
            os.makedirs(tools_dir)
            os.makedirs(venv_bin)
            shutil.copy(self.WRAPPER, os.path.join(tools_dir, "hook-runner.sh"))   # the real launcher
            interp = os.path.join(venv_bin, "python")                   # a stub that echoes each argv word
            with open(interp, "w") as fh:
                fh.write('#!/bin/sh\nfor a in "$@"; do printf \'%s\\n\' "$a"; done\n')
            os.chmod(interp, 0o755)
            open(os.path.join(tools_dir, "modes.py"), "w").close()      # a stub script so the path exists

            cmd = hooks.hook_command(".engine/tools/modes.py accept-hook", "posix")
            r = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, timeout=10,
                               env={**os.environ, "CLAUDE_PROJECT_DIR": proj,
                                    "ENGINE_HOOK_WAIT_POLLS": "3", "ENGINE_HOOK_WAIT_INTERVAL": "0.05"})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(
                r.stdout.splitlines(),
                [os.path.join(proj, ".engine/tools/modes.py"), "accept-hook"],
                "the interpreter must receive the intact spaced script path as ONE arg, then the bare arg")

    def test_launcher_waits_then_execs_when_the_interpreter_appears_late(self):
        # the race, simulated deterministically against the REAL committed launcher: the interpreter is
        # created AFTER the launcher starts; it must wait, then exec it, forwarding the script + its arg.
        with tempfile.TemporaryDirectory() as td:
            interp = os.path.join(td, "python")
            script = os.path.join(td, "boot.py")

            def _provision_late():
                time.sleep(0.3)
                with open(interp, "w") as fh:               # write fully, THEN chmod +x — mirrors uv's
                    fh.write('#!/bin/sh\necho "STUB-RAN $@"\n')   # executable-on-create order
                os.chmod(interp, 0o755)

            t = threading.Thread(target=_provision_late)
            t.start()
            r = subprocess.run(["sh", self.WRAPPER, interp, script, "hook"],
                               capture_output=True, text=True, timeout=10)
            t.join()
            self.assertIn("STUB-RAN", r.stdout)             # the interpreter ran after the wait
            self.assertIn(script, r.stdout)                 # the script path passed through
            self.assertIn("hook", r.stdout)                 # the trailing arg passed through

    def test_launcher_runs_nothing_and_never_falls_back_when_interpreter_never_appears(self):
        with tempfile.TemporaryDirectory() as td:
            interp = os.path.join(td, "python")             # never created
            script = os.path.join(td, "boot.py")
            r = subprocess.run(["sh", self.WRAPPER, interp, script],
                               capture_output=True, text=True, timeout=10,
                               env={**os.environ, "ENGINE_HOOK_WAIT_POLLS": "3",
                                    "ENGINE_HOOK_WAIT_INTERVAL": "0.05"})       # ~0.15 s bound, fast
            self.assertEqual(r.stdout, "")                  # nothing ran — no system-Python fallback
            self.assertNotEqual(r.returncode, 0)            # the falsy `[ -x ]` short-circuits the exec


class TestHookCommandMatchesWiredLiterals(unittest.TestCase):
    """The wired hook commands ARE `hook_command`'s output, so the form and the literals can never drift:
    a command-form change must update `hooks.py`, the core manifest, AND `.claude/settings.json` in
    lockstep, or this reds (the architect-A1 / adversarial-S1 drift guard for issue #83)."""

    # every engine hook wire's script-relpath-with-args. Core wires boot on three SessionStart matchers;
    # the per-prompt scent on UserPromptSubmit (slice 5, PR 2); the commit-boundary regen for the knowledge
    # graph AND the self-map (the #136 self-map/graph-asymmetry close) on PreToolUse; memory-substrate
    # (slice 3b) wires its consolidation sweep on the same three SessionStart matchers + a PreCompact hook
    # (the compaction trigger, slice 5 PR 3), (slice 4e-ii/iii) the cross-session erasure OBSERVER and the
    # earned-erasure PROPOSER, and (slice 6a) the backup-vault push, each on the same three SessionStart matchers.
    CORE_RELPATHS = (".engine/tools/boot.py", ".engine/tools/modes.py", ".engine/tools/knowledge_gen.py hook",
                     ".engine/tools/self_map.py hook", ".engine/tools/modes.py accept-hook",
                     ".engine/tools/close.py", ".engine/tools/scent.py")
    MEMORY_RELPATHS = (".engine/tools/memory/consolidate.py session-start",
                       ".engine/tools/memory/consolidate.py pre-compact",
                       ".engine/tools/memory/erasure_observer.py session-start",
                       ".engine/tools/memory/erasure_proposer.py session-start",
                       ".engine/tools/memory/backup_vault.py session-start")
    # github-projects-sync (optional) wires its board refresh on two SessionStart matchers (startup + resume),
    # the same command, so the SET has one entry while the registration COUNT is two.
    PROJECTS_SYNC_RELPATHS = (".engine/tools/projects_sync/projects_sync.py session-start",)

    def _venv_hook_commands(self, commands):
        return [c for c in commands if ".venv/bin/python" in c]

    def _hook_cmds(self, manifest):
        return self._venv_hook_commands(
            w.get("hook", {}).get("command", "") for w in manifest["wires"] if w.get("type") == "hook")

    def test_manifest_and_settings_hook_commands_are_hook_command_output(self):
        expected_core = {hooks.hook_command(r, "posix") for r in self.CORE_RELPATHS}
        expected_memory = {hooks.hook_command(r, "posix") for r in self.MEMORY_RELPATHS}
        expected_projects = {hooks.hook_command(r, "posix") for r in self.PROJECTS_SYNC_RELPATHS}

        core = validate.load_json(os.path.join(validate.ROOT, ".engine/modules/core/manifest.json"))
        c_cmds = self._hook_cmds(core)
        self.assertEqual(len(c_cmds), 9, "the nine venv-rooted core hook wires (boot ×3 + 6)")
        self.assertEqual(set(c_cmds), expected_core, "every core manifest hook command is hook_command's output")

        memory = validate.load_json(
            os.path.join(validate.ROOT, ".engine/modules/memory-substrate-sqlite-fts5/manifest.json"))
        m_cmds = self._hook_cmds(memory)
        self.assertEqual(len(m_cmds), 13, "memory's three consolidation SessionStart sweeps + one PreCompact "
                                          "compaction trigger + three erasure-observer SessionStart sweeps + three "
                                          "erasure-proposer SessionStart sweeps + three backup-vault SessionStart pushes")
        self.assertEqual(set(m_cmds), expected_memory, "every memory manifest hook command is hook_command's output")

        projects = validate.load_json(
            os.path.join(validate.ROOT, ".engine/modules/github-projects-sync/manifest.json"))
        p_cmds = self._hook_cmds(projects)
        self.assertEqual(len(p_cmds), 2, "the board refresh on two SessionStart matchers (startup + resume)")
        self.assertEqual(set(p_cmds), expected_projects, "every board-sync manifest hook command is hook_command's output")

        # settings.json registers all installed modules' hooks: 9 core + 13 memory + 2 board-sync venv-rooted.
        settings = validate.load_json(os.path.join(validate.ROOT, ".claude", "settings.json"))
        s_cmds = self._venv_hook_commands(
            h.get("command", "") for groups in settings["hooks"].values()
            for grp in groups for h in grp.get("hooks", []))
        self.assertEqual(len(s_cmds), 24,
                         "the twenty-four venv-rooted hook commands in settings (9 core + 13 memory + 2 board-sync)")
        self.assertEqual(set(s_cmds), expected_core | expected_memory | expected_projects,
                         "settings matches the form (and so all three manifests) exactly")


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
