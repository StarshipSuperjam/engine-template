#!/usr/bin/env python3
"""Self-tests for the hooks contract substrate: the closed event inventory, the block
budget + block cap, the per-OS interpreter-path resolver, the fail-open-and-flag harness, and the pure
block-budget coherence leg (validate.block_budget_findings).

Run: uv run --directory .engine --frozen -- python tools/selftest.py

These lock the laws hooks owns:
  - the event inventory is the engine's chosen subset of six events (SessionEnd is NOT governed — nothing
    ever ran on it on either runtime, so its never-bound row was retracted), every row naming the systems
    whose behaviour runs on the event: SessionStart five-owner (boot·memory·github-projects-sync·telemetry·
    build-coordinator), PreToolUse six-owner (its actually-bound systems, not a placeholder), PostToolUse
    three-owner (validation·telemetry·modes — telemetry a DECLARED delegated owner riding validation's
    accept-hook), UserPromptSubmit boot-then-modes; owners are asserted as sets, order only where it is
    load-bearing; only PreToolUse and Stop are block-eligible; the block-eligible invariant set ships EMPTY.
  - the inventory is kept true by two pure drift checkers over the runtimes' registration documents, judging
    ENGINE-owned entries only (an operator's own hook is never a finding): the forward leg reds an engine
    command on an uninventoried event or one whose script maps to no owner named on its event; the reverse
    leg reds an inventoried event with no engine binding, or a named owner nothing satisfies (a delegated
    owner needs its delegate bound; an optional module's owner is skipped when the module is absent); both
    fail loud on an empty extraction. Committed negative fixtures prove each leg still bites; what the
    owner table proves is that every engine command has a NAMED owner, not that the name is the right one.
  - the block cap is 8, overridable via CLAUDE_CODE_STOP_HOOK_BLOCK_CAP (verified on the live platform).
  - the interpreter path is ${CLAUDE_PROJECT_DIR}-rooted, per-OS (POSIX bin/python, Windows Scripts/
    python.exe), never bare python / uv run.
  - the harness FAILS OPEN: a crashing handler, a malformed event payload, or a block requested on a
    non-eligible event all PROCEED (a non-2 exit) and emit a plain-language finding — never a hard block;
    only a handler that returns block() on PreToolUse/Stop exits 2; on a repeated Stop
    (stop_hook_active) the handler STILL runs and its decision is preserved. The registered owner,
    close, owns the finite log-and-proceed rule; the shared harness does not guess another owner's budget.
  - the static block-budget leg flags a block declared on a non-eligible event, is silent on an empty set,
    and agrees with the runtime BLOCK_ELIGIBLE_EVENTS (a drift guard). The leg is built + fixture-tested
    with no live rule (the interface_resolution_findings / agent_coherence_findings precedent); the live
    rule wires at the first hook-wiring slice.
"""
from __future__ import annotations
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import concurrent.futures
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hooks     # noqa: E402
import modes     # noqa: E402  (the canonical stance vocabulary the block-registry leg validates against)
import validate  # noqa: E402


def _run(event, handler, payload=None, stdin_text=None):
    """Drive the real run_hook with captured streams. Returns (exit_code, stdout, stderr)."""
    if stdin_text is None:
        stdin_text = json.dumps(payload or {})
    out, err = io.StringIO(), io.StringIO()
    code = hooks.run_hook(event, handler, stdin=io.StringIO(stdin_text), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


class TestEventInventory(unittest.TestCase):
    def test_the_six_governed_events(self):
        self.assertEqual(hooks.EVENTS, {
            "SessionStart", "PreToolUse", "PostToolUse", "PreCompact",
            "Stop", "UserPromptSubmit"})

    def test_sessionend_is_not_governed(self):
        # Retracted (StarshipSuperjam/engine-template#816, migration M2): the row claimed a hooks-owned
        # cleanup duty while nothing ever ran on SessionEnd on either runtime — no registration, no
        # delegated call — and hooks ships no handler by its own law. A never-bound row is a claim, not a
        # reservation; a real duty re-adds the row with its owner and a binding, in one change.
        self.assertNotIn("SessionEnd", hooks.EVENTS)
        self.assertNotIn("SessionEnd", hooks.DELEGATED_OWNERS)

    def test_only_pretooluse_and_stop_are_block_eligible(self):
        self.assertEqual(hooks.BLOCK_ELIGIBLE_EVENTS, {"PreToolUse", "Stop"})
        for ev, meta in hooks.EVENT_INVENTORY.items():
            self.assertEqual(meta["blocks"], ev in {"PreToolUse", "Stop"},
                             f"{ev} block-eligibility")

    def test_sessionstart_names_its_five_owners_as_a_set(self):
        # boot's pack + memory's session-start work + the optional board refresh + telemetry's ambient
        # triage and inbox drain (StarshipSuperjam/engine-template#784 — four bound hooks that had no
        # owner slot) + build-coordinator's compact-matcher re-grounding. A SET: the two runtimes register
        # these in different orders, so no single registration order exists to pin.
        self.assertEqual(set(hooks.EVENT_INVENTORY["SessionStart"]["owners"]),
                         {"boot", "memory", "github-projects-sync", "telemetry", "build-coordinator"})
        self.assertTrue(hooks.EVENT_INVENTORY["SessionStart"]["injects"])
        self.assertFalse(hooks.EVENT_INVENTORY["SessionStart"]["blocks"])

    def test_pretooluse_names_the_six_systems_actually_bound_there(self):
        # The row used to name a placeholder ("invariant-owner") — the same under-report as StarshipSuperjam/engine-template#784 on the
        # busiest event. These are the systems whose commands are bound on PreToolUse.
        self.assertEqual(set(hooks.EVENT_INVENTORY["PreToolUse"]["owners"]),
                         {"modes", "knowledge", "self-map", "validation", "product-design", "session-economy"})
        self.assertNotIn("invariant-owner", hooks.EVENT_INVENTORY["PreToolUse"]["owners"])

    def test_posttooluse_enumerates_its_three_owners(self):
        # validation's touched-file run + telemetry's ambient capture + modes' Claude native-plan
        # intake adapter coexist on one event (the owner inventory).
        self.assertEqual(set(hooks.EVENT_INVENTORY["PostToolUse"]["owners"]),
                         {"validation", "telemetry", "modes"})

    def test_telemetry_is_a_declared_delegated_owner_on_posttooluse(self):
        # telemetry registers no PostToolUse hook of its own: validate's accept-hook relays each edit into
        # telemetry.capture_touched_fires. "Owner" is a behaviour relation, and a delegated owner is
        # DECLARED data — the checkers honour it, they cannot detect it.
        self.assertEqual(hooks.DELEGATED_OWNERS, {"PostToolUse": {"telemetry": "validation"},
                                                  "Stop": {"telemetry": "close"}})
        for event, delegations in hooks.DELEGATED_OWNERS.items():
            self.assertIn(event, hooks.EVENTS)
            for owner, delegate in delegations.items():
                self.assertIn(owner, hooks.EVENT_INVENTORY[event]["owners"])
                self.assertIn(delegate, hooks.EVENT_INVENTORY[event]["owners"])
                self.assertNotEqual(owner, delegate)

    def test_stop_names_close_and_a_delegated_telemetry(self):
        # close's Stop handler promotes a logged finding through telemetry.promote_finding — the same
        # delegated shape as PostToolUse, on the turn-close event.
        self.assertEqual(set(hooks.EVENT_INVENTORY["Stop"]["owners"]), {"close", "telemetry"})
        self.assertEqual(hooks.DELEGATED_OWNERS["Stop"], {"telemetry": "close"})

    def test_every_owner_by_script_target_is_an_inventoried_owner(self):
        named = {o for meta in hooks.EVENT_INVENTORY.values() for o in meta["owners"]}
        for prefix, owner in hooks.OWNER_BY_SCRIPT:
            self.assertTrue(prefix.startswith(".engine/tools/"), prefix)
            self.assertIn(owner, named, f"{prefix} maps to {owner}, which no inventory row names")
        self.assertLessEqual(set(hooks.OWNER_MODULE), named)
        # A mistyped module id would make the reverse leg skip that owner forever, silently. The roster
        # is the module CATALOG — every module the engine knows, kept whether or not this deployment
        # installed it — not the modules present on disk, so a project that declined an optional add-on
        # (a supported setup) does not red its own self-test.
        import module_catalog
        catalog_ids = {entry["id"] for entry in module_catalog.entries()}
        self.assertTrue(catalog_ids, "the module catalog is empty or unreadable")
        self.assertLessEqual(set(hooks.OWNER_MODULE.values()), catalog_ids)

    def test_posttooluse_may_inject_and_stays_non_blocking(self):
        # modes' intake adapter injects the arrival report (additionalContext) after importing an
        # accepted plan, so PostToolUse may inject — but it never blocks.
        self.assertTrue(hooks.EVENT_INVENTORY["PostToolUse"]["injects"])
        self.assertFalse(hooks.EVENT_INVENTORY["PostToolUse"]["blocks"])

    def test_userpromptsubmit_carries_boot_then_modes_in_a_defined_order(self):
        """AMENDED, deliberately, when the Codex native-plan intake adapter landed. This event was
        single-owner (`("boot",)`) from the day the table was written until then.

        What makes a second owner admissible here, and why the amendment is not the drift the table
        exists to catch. The hook table refuses writers of UNDEFINED order — writers that race, whose
        relative sequence nothing states — not registered owners; PostToolUse has carried three owners,
        added one at a time, since it was written (asserted as a set), and this is that shape, not a new one. The two owners
        cannot contend: boot's per-prompt scent injects a constant orientation cue without reading the
        prompt's content, while modes reads the prompt's opening bytes and acts only on an acceptance
        envelope at byte zero. They share no file and no signal — modes writes no stance — so neither
        can overwrite the other's work, and the order below is stated so it can never become
        incidental. The adapter is registered on this event only on Codex, which has no plan-exit
        completion to key on; on Claude the same import rides PostToolUse. The order is pinned in the
        Codex registration itself by test_modes, and this pins the table that authorizes it.
        """
        self.assertEqual(hooks.EVENT_INVENTORY["UserPromptSubmit"]["owners"], ("boot", "modes"))
        self.assertTrue(hooks.EVENT_INVENTORY["UserPromptSubmit"]["injects"])
        self.assertFalse(hooks.EVENT_INVENTORY["UserPromptSubmit"]["blocks"],
                         "the second owner must not have made this event block-eligible")

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
        # the conformance witness: the explicit ${CLAUDE_PROJECT_DIR}-rooted venv interpreter is
        # named IN the command (the launcher's first arg), never a bare/system interpreter or `uv run`.
        cmd = hooks.hook_command(".engine/tools/boot.py", "posix")
        self.assertIn(f'"{hooks.interpreter_path("posix")}"', cmd)
        self.assertNotIn("exec python", cmd)
        self.assertNotIn("uv ", cmd)
        self.assertNotIn("/usr/bin/", cmd)
        self.assertNotIn("/usr/local/bin/", cmd)

    def test_the_launcher_waits_bounded_and_execs_only_the_given_interpreter(self):
        # the launcher source: a bounded (not infinite) wait, then a SINGLE exec of the resolved venv
        # interpreter (the named POSIX bin/python, or its Windows Scripts/python.exe sibling under the same
        # venv root — #407) with the forwarded args — never a bare/system Python fallback. There are two
        # mutually exclusive exec sites now: the normal non-memory path and the qualification-health
        # telemetry fallback when its owner-only transient stderr file cannot be created.
        with open(self.WRAPPER) as fh:
            src = fh.read()
        self.assertIn("while", src)
        self.assertIn("-lt", src)                       # a numeric cap, never an unbounded loop
        self.assertIn("shift", src)                     # the interpreter arg is consumed, so "$@" = script+args
        self.assertIn('exec "$interp" "$@"', src)       # one exec, of the passed interpreter, args forwarded
        self.assertEqual(src.count('exec "$interp" "$@"'), 2)
        self.assertEqual(src.count("exec "), 2)
        for forbidden in ("uv ", "/usr/bin/", "/usr/local/bin/", "exec python"):
            self.assertNotIn(forbidden, src)

    def test_memory_bearing_automatic_targets_enter_the_exact_accepted_dispatcher(self):
        with open(self.WRAPPER) as fh:
            src = fh.read()
        self.assertIn("ENGINE_ACCEPTED_HOOK_DISPATCH=1", src)
        self.assertIn("accepted_hook_dispatch.py", src)
        self.assertIn('set -- -I -S "$dispatcher" run --root "$project" --script "$script" -- "$@"', src)
        for target in (
            ".engine/tools/boot.py",
            ".engine/tools/close.py",
            ".engine/tools/memory/compact.py",
            ".engine/tools/memory/erasure_observer.py",
            ".engine/tools/memory/backup_vault.py",
        ):
            self.assertIn(target, src)

    def test_automatic_roster_matches_dispatcher_and_both_provider_documents_are_closed(self):
        import accepted_hook_dispatch
        self.assertEqual(hooks.ACCEPTED_AUTOMATIC_SCRIPTS,
                         accepted_hook_dispatch.AUTOMATIC_MUTATORS)
        for provider, rel in (("claude", ".claude/settings.json"), ("codex", ".codex/hooks.json")):
            with self.subTest(provider=provider):
                document = validate.load_json(os.path.join(validate.ROOT, rel))
                self.assertEqual(hooks.automatic_hook_wiring_failures(document, provider), [])

    def test_direct_dispatch_mutator_sibling_and_altered_effect_fail_mechanically(self):
        unsafe = (
            "python .engine/tools/accepted_hook_dispatch.py activate",
            "python .engine/tools/memory/new_mutator.py",
            hooks.hook_command(".engine/tools/memory/mutation_authority.py", provider="codex"),
            hooks.hook_command(".engine/tools/memory/compact.py activate", provider="claude"),
        )
        for provider in ("claude", "codex"):
            for command in unsafe:
                with self.subTest(provider=provider, command=command):
                    document = {"hooks": {"PreCompact": [{"hooks": [
                        {"type": "command", "command": command},
                    ]}]}}
                    self.assertTrue(hooks.automatic_hook_wiring_failures(document, provider))

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
            self.assertNotEqual(r.returncode, 0)            # neither venv layout exists → no exec → fail-open
            self.assertNotEqual(r.returncode, 2)            # and NEVER the platform's block code (#390 stranding)

    def test_launcher_fails_open_when_the_interpreter_is_present_but_not_executable(self):
        # a corrupt/partial venv: the named interpreter file EXISTS but is not runnable. The launcher must
        # still reach the plain-language fail-open readout (exit 1, non-blocking) — NOT exec the file and
        # surface a raw shell error (126). This pins the fix for the `-f`-guard regression the gate found:
        # the exec is gated on `-x`, so a non-executable interpreter waits out the bound and fails open.
        with tempfile.TemporaryDirectory() as td:
            interp = os.path.join(td, ".venv", "bin", "python")
            os.makedirs(os.path.dirname(interp))
            with open(interp, "w") as fh:                   # present as a regular file...
                fh.write("#!/bin/sh\necho SHOULD-NOT-RUN\n")
            os.chmod(interp, 0o644)                         # ...but NOT executable
            r = subprocess.run(["sh", self.WRAPPER, interp, os.path.join(td, "boot.py")],
                               capture_output=True, text=True, timeout=10,
                               env={**os.environ, "ENGINE_HOOK_WAIT_POLLS": "3",
                                    "ENGINE_HOOK_WAIT_INTERVAL": "0.05"})
            self.assertEqual(r.stdout, "")                  # did not exec the non-executable file
            self.assertNotEqual(r.returncode, 0)            # fail-open readout path
            self.assertNotEqual(r.returncode, 2)            # never the block code
            self.assertIn("not a block", r.stderr)          # the friendly readout, not a raw exec error

    def test_launcher_resolves_the_windows_sibling_when_the_posix_layout_is_absent(self):
        # the #407 fix, exercised on a POSIX host with a STUB at the Windows layout path: the committed
        # command always names the POSIX bin/python; when only the Windows Scripts/python.exe layout exists
        # (a Windows adopter, or a mixed-OS teammate on a repo whose committed command names the other OS),
        # the launcher resolves and runs THAT sibling under the same venv root — so one committed repo boots
        # on every OS. (The real .exe under Git Bash is unverifiable off Windows; the stub proves the
        # branch-selection + exec dispatch, which is the falsifiable part on a POSIX host.)
        with tempfile.TemporaryDirectory() as td:
            venv = os.path.join(td, ".venv")
            posix = os.path.join(venv, "bin", "python")          # NAMED in the command, but absent here
            win = os.path.join(venv, "Scripts", "python.exe")    # the only layout present on this "machine"
            os.makedirs(os.path.dirname(win))
            with open(win, "w") as fh:
                fh.write('#!/bin/sh\necho "WIN-STUB-RAN $@"\n')
            os.chmod(win, 0o755)                                 # executable so exec succeeds on the POSIX host
            script = os.path.join(td, "boot.py")
            r = subprocess.run(["sh", self.WRAPPER, posix, script, "hook"],
                               capture_output=True, text=True, timeout=10,
                               env={**os.environ, "ENGINE_HOOK_WAIT_POLLS": "3",
                                    "ENGINE_HOOK_WAIT_INTERVAL": "0.05"})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("WIN-STUB-RAN", r.stdout)              # the Windows-layout interpreter ran
            self.assertIn(script, r.stdout)                      # the script path forwarded
            self.assertIn("hook", r.stdout)                      # the trailing arg forwarded

    def test_launcher_prefers_the_named_posix_interpreter_when_both_layouts_exist(self):
        # determinism: on the normal POSIX host the named bin/python is used and the Windows sibling is
        # never consulted, even if both happen to be present.
        with tempfile.TemporaryDirectory() as td:
            venv = os.path.join(td, ".venv")
            posix = os.path.join(venv, "bin", "python")
            win = os.path.join(venv, "Scripts", "python.exe")
            os.makedirs(os.path.dirname(posix))
            os.makedirs(os.path.dirname(win))
            for p, tag in ((posix, "POSIX"), (win, "WIN")):
                with open(p, "w") as fh:
                    fh.write(f'#!/bin/sh\necho "{tag}-RAN"\n')
                os.chmod(p, 0o755)
            r = subprocess.run(["sh", self.WRAPPER, posix, os.path.join(td, "boot.py")],
                               capture_output=True, text=True, timeout=10)
            self.assertIn("POSIX-RAN", r.stdout)
            self.assertNotIn("WIN-RAN", r.stdout)

    def test_launcher_os_literals_match_the_resolver_single_source(self):
        # F2 / single-source-of-truth: the per-OS layout fact (bin/python vs Scripts/python.exe) is DEFINED
        # once in hooks.interpreter_path; the launcher necessarily restates the two subpaths in shell. Pin
        # them to the resolver's forms — checking the EXECUTABLE lines only (the comments carry the literals
        # too, so a whole-file match would not catch a mangled code line) — so the two homes cannot diverge.
        with open(self.WRAPPER) as fh:
            code = "\n".join(ln for ln in fh.read().splitlines() if not ln.lstrip().startswith("#"))
        self.assertTrue(hooks.interpreter_path("posix").endswith("/bin/python"))
        self.assertTrue(hooks.interpreter_path("nt").endswith("/Scripts/python.exe"))
        self.assertIn("/bin/python", code)                       # the POSIX layout, in the executable body
        self.assertIn("/Scripts/python.exe", code)               # the Windows layout, in the executable body

    def test_launcher_drops_the_runtime_marker_when_no_interpreter_appears(self):
        # #412: a missing tool-runtime cannot reach Python, so the launcher leaves a PRESENCE marker for the
        # drain-inbox driver to promote next session. It is best-effort, EMPTY, and never changes the exit code.
        with tempfile.TemporaryDirectory() as td:
            interp = os.path.join(td, ".engine", ".venv", "bin", "python")   # named, never created
            os.makedirs(os.path.dirname(interp))
            r = subprocess.run(["sh", self.WRAPPER, interp, os.path.join(td, ".engine", "tools", "boot.py")],
                               capture_output=True, text=True, timeout=10,
                               env={**os.environ, "ENGINE_HOOK_WAIT_POLLS": "3", "ENGINE_HOOK_WAIT_INTERVAL": "0.05"})
            self.assertNotEqual(r.returncode, 0)                 # fail-open readout path
            self.assertNotEqual(r.returncode, 2)                 # never the block code (#390 stranding)
            marker = os.path.join(td, ".engine", "telemetry", ".cache", "runtime-health.marker")
            self.assertTrue(os.path.exists(marker), "the launcher drops the runtime-health marker")
            self.assertEqual(os.path.getsize(marker), 0)         # presence-only — no bytes can enter the finding

    def test_launcher_marker_path_matches_the_python_constant(self):
        # #412 single-source: the shell builds the marker path from the venv root; pin its two components to
        # telemetry.RUNTIME_HEALTH_MARKER_PATH so the shell↔Python contract cannot drift.
        import telemetry
        engine = os.path.join(validate.ROOT, ".engine")
        dir_tail = os.path.relpath(os.path.dirname(telemetry.RUNTIME_HEALTH_MARKER_PATH), engine)  # telemetry/.cache
        base = os.path.basename(telemetry.RUNTIME_HEALTH_MARKER_PATH)                               # runtime-health.marker
        with open(self.WRAPPER) as fh:
            code = "\n".join(ln for ln in fh.read().splitlines() if not ln.lstrip().startswith("#"))
        self.assertIn(dir_tail, code)
        self.assertIn(base, code)


class TestHookCommandMatchesWiredLiterals(unittest.TestCase):
    """The wired hook commands ARE `hook_command`'s output, so the form and the literals can never drift:
    a command-form change must update `hooks.py`, the core manifest, AND `.claude/settings.json` in
    lockstep, or this reds (the architect-A1 / adversarial-S1 drift guard for issue #83)."""

    # every engine hook wire's script-relpath-with-args. Core wires boot on three SessionStart matchers;
    # the per-prompt scent on UserPromptSubmit; the commit-boundary regen for the knowledge
    # graph AND the self-map (the #136 self-map/graph-asymmetry close) on PreToolUse; memory-substrate
    # wires a PreCompact hook (the compaction trigger — the one thing that physically carries out an erasure
    # the operator merged), plus the cross-session erasure OBSERVER and the backup-vault push, each on three
    # SessionStart matchers. telemetry's ambient triage runs on two SessionStart matchers (startup + resume),
    # the same command, so it adds ONE entry to the SET while adding TWO to the registration COUNT (like
    # github-projects-sync).
    CORE_RELPATHS = (".engine/tools/boot.py", ".engine/tools/modes.py", ".engine/tools/knowledge_gen.py hook",
                     ".engine/tools/self_map.py hook", ".engine/tools/validate.py hook",
                     ".engine/tools/session_economy.py hook",
                     ".engine/tools/modes.py accept-hook", ".engine/tools/validate.py accept-hook",
                     ".engine/tools/close.py", ".engine/tools/scent.py",
                     ".engine/tools/telemetry.py run-ambient",
                     ".engine/tools/telemetry.py drain-inbox",
                     # The post-compaction re-grounding owner: the ONLY wire on the `compact` matcher,
                     # so it adds one to the set and one to the count.
                     ".engine/tools/build_coordinator.py reground-hook")
    MEMORY_RELPATHS = (".engine/tools/memory/compact.py pre-compact",
                       ".engine/tools/memory/erasure_observer.py session-start",
                       ".engine/tools/memory/backup_vault.py session-start")
    # github-projects-sync (optional) wires its board refresh on two SessionStart matchers (startup + resume),
    # the same command, so the SET has one entry while the registration COUNT is two.
    PROJECTS_SYNC_RELPATHS = (".engine/tools/projects_sync/projects_sync.py session-start",)
    # product-design (optional) wires ONE PreToolUse regen hook: its obligation-matrix commit-boundary refresh
    # (mirrors core's graph/self-map regen hooks) — product-design's first and only hook wire.
    PRODUCT_DESIGN_RELPATHS = (".engine/tools/product_design/obligation_matrix.py hook",)

    def _venv_hook_commands(self, commands):
        return [c for c in commands if ".venv/bin/python" in c]

    def _hook_cmds(self, manifest):
        return self._venv_hook_commands(
            w.get("hook", {}).get("command", "") for w in manifest["wires"] if w.get("type") == "hook")

    def test_manifest_and_settings_hook_commands_are_hook_command_output(self):
        expected_core = {hooks.hook_command(r, "posix") for r in self.CORE_RELPATHS}
        expected_memory = {hooks.hook_command(r, "posix") for r in self.MEMORY_RELPATHS}
        expected_projects = {hooks.hook_command(r, "posix") for r in self.PROJECTS_SYNC_RELPATHS}
        expected_product_design = {hooks.hook_command(r, "posix") for r in self.PRODUCT_DESIGN_RELPATHS}

        core = validate.load_json(os.path.join(validate.ROOT, ".engine/modules/core/manifest.json"))
        c_cmds = self._hook_cmds(core)
        self.assertEqual(len(c_cmds), 17, "the seventeen venv-rooted core hook wires (boot ×3 + 9: modes, "
                         "knowledge_gen, self_map, validate pre-commit, session_economy, modes accept, "
                         "validate accept, close, "
                         "scent + telemetry run-ambient ×2 + telemetry drain-inbox ×2: startup + resume "
                         "+ build_coordinator reground-hook on the compact matcher)")
        self.assertEqual(set(c_cmds), expected_core, "every core manifest hook command is hook_command's output")

        memory = validate.load_json(
            os.path.join(validate.ROOT, ".engine/modules/memory-substrate-sqlite-fts5/manifest.json"))
        m_cmds = self._hook_cmds(memory)
        self.assertEqual(len(m_cmds), 7, "memory's one PreCompact compaction trigger + three erasure-observer "
                                         "SessionStart sweeps + three backup-vault SessionStart pushes")
        self.assertEqual(set(m_cmds), expected_memory, "every memory manifest hook command is hook_command's output")

        # Both modules below are OPTIONAL — declined at setup or removed later, their manifests are simply
        # gone. Reading one unconditionally raises a bare FileNotFoundError in a deployment that made a
        # supported choice, which reds its required self-tests over an add-on it never wanted.
        projects_path = os.path.join(validate.ROOT, ".engine/modules/github-projects-sync/manifest.json")
        pd_path = os.path.join(validate.ROOT, ".engine/modules/product-design/manifest.json")
        if not (os.path.exists(projects_path) and os.path.exists(pd_path)):
            self.skipTest("board-sync and/or product-design are not installed in this repository")
        projects = validate.load_json(projects_path)
        p_cmds = self._hook_cmds(projects)
        self.assertEqual(len(p_cmds), 2, "the board refresh on two SessionStart matchers (startup + resume)")
        self.assertEqual(set(p_cmds), expected_projects, "every board-sync manifest hook command is hook_command's output")

        product_design = validate.load_json(pd_path)
        pd_cmds = self._hook_cmds(product_design)
        self.assertEqual(len(pd_cmds), 1, "product-design's one obligation-matrix commit-boundary regen hook")
        self.assertEqual(set(pd_cmds), expected_product_design,
                         "product-design's manifest hook command is hook_command's output")

        # settings.json registers all installed modules' hooks: 17 core + 7 memory + 2 board-sync + 1 product-design venv-rooted.
        settings = validate.load_json(os.path.join(validate.ROOT, ".claude", "settings.json"))
        s_cmds = self._venv_hook_commands(
            h.get("command", "") for groups in settings["hooks"].values()
            for grp in groups for h in grp.get("hooks", []))
        self.assertEqual(len(s_cmds), 27,
                         "the twenty-seven venv-rooted hook commands in settings "
                         "(17 core + 7 memory + 2 board-sync + 1 product-design)")
        self.assertEqual(set(s_cmds), expected_core | expected_memory | expected_projects | expected_product_design,
                         "settings matches the form (and so all four manifests) exactly")


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

    def test_fail_open_notice_overrides_the_operator_crash_line(self):
        # U08c (#412): a gate can supply its OWN operator-facing crash sentence (close passes the spec's
        # "I couldn't run the check that confirms nothing was dropped"). fail_open_notice replaces ONLY the
        # operator message; the promote path + non-blocking exit are unchanged.
        seen = {}
        def promote(event, kind, message):
            seen["message"] = message
            return True
        def boom(_payload):
            raise RuntimeError("kaboom")
        out, err = io.StringIO(), io.StringIO()
        code = hooks.run_hook("Stop", boom, stdin=io.StringIO("{}"), stdout=out, stderr=err,
                              promote=promote, fail_open_notice="MY-OWN-CRASH-LINE")
        self.assertEqual(code, hooks.EXIT_NONBLOCKING)
        self.assertIn("MY-OWN-CRASH-LINE", err.getvalue())                 # operator hears the gate's own line
        self.assertNotIn("a safety check on the stop step", err.getvalue().lower())  # not the generic wording
        self.assertIn("MY-OWN-CRASH-LINE", seen["message"])               # and it rides the promoted Issue too

    def test_without_a_fail_open_notice_the_generic_crash_line_is_used(self):
        def boom(_payload):
            raise RuntimeError("kaboom")
        code, _out, err = _run("PreToolUse", boom)
        self.assertEqual(code, hooks.EXIT_NONBLOCKING)
        self.assertIn("could not run", err.lower())                       # the generic fallback still applies

    def test_a_crash_records_a_locator_to_the_engine_only_file_not_the_operator_channel(self):
        # A fail-open crash records only the exception TYPE on the operator-facing surfaces (the plain
        # stderr finding the platform shows on exit 1, and the promoted Issue). The exception MESSAGE + a
        # file:line locator go ONLY to a gitignored engine-only FILE — never stderr, never the Issue — so a
        # transient crash is diagnosable WITHOUT putting backstage detail in front of a non-engineer.
        recorded = {}
        def promote(event, kind, message):
            recorded["message"] = message
            return True
        def boom(_payload):
            raise NameError("name 'wibble' is not defined")
        with tempfile.TemporaryDirectory() as d:
            logpath = os.path.join(d, ".cache", "hook-crash-debug.log")
            # point the recorder at a temp path (its `path` arg) so the test never writes the real cache
            real = hooks._record_crash_debug
            try:
                hooks._record_crash_debug = lambda ev, ex: real(ev, ex, logpath)
                out, err = io.StringIO(), io.StringIO()
                code = hooks.run_hook("PreToolUse", boom, stdin=io.StringIO("{}"),
                                      stdout=out, stderr=err, promote=promote)
            finally:
                hooks._record_crash_debug = real
            errtext = err.getvalue()
            with open(logpath, encoding="utf-8") as fh:
                filetext = fh.read()
        self.assertEqual(code, hooks.EXIT_NONBLOCKING)
        # the engine-only FILE carries the exception message AND a file:line locator
        self.assertIn("name 'wibble' is not defined", filetext)
        self.assertRegex(filetext, r"@ \S+\.py:\d+")
        # stderr (operator-visible on the non-blocking exit) stays plain: no raw message, no locator
        self.assertNotIn("wibble", errtext)
        self.assertNotRegex(errtext, r"\.py:\d+")
        self.assertIn("NameError", errtext)                            # the plain finding still names the type
        # the promoted (operator Issue) message names only the type
        self.assertIn("NameError", recorded["message"])
        self.assertNotIn("wibble", recorded["message"])
        self.assertNotRegex(recorded["message"], r"\.py:\d+")

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
    def test_repeated_stop_runs_the_owner_and_preserves_its_decision(self):
        # The shared harness cannot infer one owner's finite budget from the provider flag. It delivers the
        # event and preserves the owner's decision; test_close drives the registered Stop owner and proves
        # that owner logs then proceeds instead of looping.
        called = []

        def would_block(_payload):
            called.append(True)
            return hooks.block("disposition still open")
        code, _out, err = _run("Stop", would_block, payload={"stop_hook_active": True})
        self.assertEqual(code, hooks.EXIT_BLOCK)     # the owner decides whether its own finite budget permits it
        self.assertEqual(called, [True])             # ...but the handler DID run (its side effects fire)
        self.assertIn("disposition still open", err) # the owner's pushback remains visible

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
    STANCES = modes.STANCES  # the canonical vocabulary the mode-dimension rule validates against

    def test_empty_set_is_silent(self):
        self.assertEqual(validate.block_budget_findings([], "hard", self.MSG, stances=self.STANCES), [])

    def test_eligible_events_with_declared_modes_pass(self):
        blocks = [{"event": "Stop", "name": "findings-disposition", "owner": "close",
                   "modes": ["explore", "build", "routine"]},
                  {"event": "PreToolUse", "name": "explore-write-gate", "owner": "modes",
                   "modes": ["explore"]}]
        self.assertEqual(validate.block_budget_findings(blocks, "hard", self.MSG, stances=self.STANCES), [])

    def test_non_eligible_event_is_flagged(self):
        # A declared `modes` isolates the event rule so only it fires (len 1).
        found = validate.block_budget_findings(
            [{"event": "PostToolUse", "name": "bad", "modes": ["explore"]}], "hard", self.MSG,
            stances=self.STANCES)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["severity"], "hard")
        self.assertIn("PostToolUse", found[0]["message"])
        self.assertIn(self.MSG, found[0]["message"])

    def test_owner_is_used_when_name_absent(self):
        found = validate.block_budget_findings(
            [{"event": "PreCompact", "owner": "memory", "modes": ["explore"]}], "soft", self.MSG,
            stances=self.STANCES)
        self.assertEqual(len(found), 1)
        self.assertIn("memory", found[0]["message"])
        self.assertEqual(found[0]["severity"], "soft")

    def test_missing_modes_is_flagged(self):
        # The mode dimension is declared data: a block that names no stances it is active in fires.
        found = validate.block_budget_findings(
            [{"event": "PreToolUse", "name": "no-modes", "owner": "modes"}], "hard", self.MSG,
            stances=self.STANCES)
        self.assertEqual(len(found), 1)
        self.assertIn("does not declare the modes it is active in", found[0]["message"])

    def test_empty_modes_is_flagged(self):
        found = validate.block_budget_findings(
            [{"event": "Stop", "name": "empty", "owner": "close", "modes": []}], "hard", self.MSG,
            stances=self.STANCES)
        self.assertEqual(len(found), 1)
        self.assertIn("does not declare the modes", found[0]["message"])

    def test_unknown_mode_is_flagged(self):
        found = validate.block_budget_findings(
            [{"event": "Stop", "name": "typo", "owner": "close", "modes": ["explor"]}], "hard", self.MSG,
            stances=self.STANCES)
        self.assertEqual(len(found), 1)
        self.assertIn("unknown mode", found[0]["message"])
        self.assertIn("explor", found[0]["message"])

    def test_agrees_with_runtime_block_eligible_events(self):
        """Drift guard: for every governed event, the static leg flags a block on it iff the event is
        outside the runtime's BLOCK_ELIGIBLE_EVENTS constant — the leg's own {PreToolUse, Stop} literal
        and the harness's eligibility constant cannot drift. A declared `modes` isolates the event rule.
        (The runtime _translate gate itself is exercised in
        TestHarnessBlock.test_block_on_every_non_eligible_event_fails_open.)"""
        for ev in hooks.EVENTS:
            findings = validate.block_budget_findings(
                [{"event": ev, "name": ev, "modes": ["explore"]}], "hard", "", stances=self.STANCES)
            flagged = bool(findings)
            self.assertEqual(flagged, ev not in hooks.BLOCK_ELIGIBLE_EVENTS, f"{ev} agreement")


class TestDemoRuns(unittest.TestCase):
    def test_demo_executes_cleanly(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = hooks.main(["demo"])
        self.assertEqual(code, 0)
        self.assertIn("fail-open", buf.getvalue())


class TestFailOpenPromotion(unittest.TestCase):
    """#391: a fail-open finding is PROMOTED to a tracked engine Issue (best-effort, fail-safe), and the
    in-session copy is HONEST about whether that landed — the old unconditional 'this was recorded as a
    problem to fix' (which recorded nothing) is gone."""

    @staticmethod
    def _crash(_payload):
        raise RuntimeError("boom")

    def _run(self, promote):
        out, err = io.StringIO(), io.StringIO()
        code = hooks.run_hook("PreToolUse", self._crash, stdin=io.StringIO("{}"),
                              stdout=out, stderr=err, promote=promote)
        return code, err.getvalue()

    def test_promoted_finding_says_recorded(self):
        code, err = self._run(promote=lambda *a: 4242)      # a landed promotion returns the Issue number
        self.assertEqual(code, hooks.EXIT_NONBLOCKING)
        self.assertIn("recorded as a tracked item", err)
        self.assertNotIn("not durably", err)

    def test_offline_finding_says_not_recorded_and_never_lies(self):
        code, err = self._run(promote=lambda *a: False)     # offline / unreachable -> not durably tracked
        self.assertEqual(code, hooks.EXIT_NONBLOCKING)
        self.assertIn("not durably", err)
        self.assertNotIn("recorded as a tracked item", err)
        self.assertNotIn("recorded as a problem to fix", err)   # the old false copy is gone entirely

    def test_a_promoter_that_itself_throws_never_fails_the_gate_closed(self):
        # belt-and-suspenders: recording the crash must NEVER re-break the fail-open path into a block/crash.
        def boom_promote(*_a):
            raise OSError("disk full")
        code, err = self._run(promote=boom_promote)
        self.assertEqual(code, hooks.EXIT_NONBLOCKING)      # still non-blocking, never exit 2
        self.assertIn("not durably", err)                   # degrades to surfaced-not-recorded

    def test_source_id_is_coarse_and_marker_safe(self):
        a = hooks._fail_open_source_id("PreToolUse", "crash")
        self.assertEqual(a, hooks._fail_open_source_id("PreToolUse", "crash"))   # recurrences -> ONE Issue
        self.assertEqual(a, "hooks/fail-open/PreToolUse/crash")
        self.assertNotEqual(a, hooks._fail_open_source_id("Stop", "crash"))
        for bad in ("<!--", "-->", "\n"):                   # cannot forge telemetry's dedup marker
            self.assertNotIn(bad, a)

    def test_real_promoter_refuses_live_github_under_a_test_harness(self):
        # the SAFETY BACKSTOP: the real promoter must never open a live Issue from a test run.
        self.assertIn("unittest", sys.modules)
        self.assertFalse(hooks._promote_fail_open("PreToolUse", "crash", "msg"))

    def test_do_promote_degrades_to_false_when_emit_does(self):
        # After the un-inversion the hook no longer resolves the GitHub boundary — telemetry.emit_finding
        # does (and owns the no-token/offline -> False behaviour, covered in test_telemetry). Here the hook's
        # job is only to relay emit_finding's verdict: a False emit -> a False promote (surfaced-not-recorded).
        import telemetry
        with mock.patch.object(telemetry, "emit_finding", return_value=False):
            self.assertFalse(hooks._do_promote_fail_open("Stop", "crash", "m"))

    def test_do_promote_emits_a_trust_critical_sourced_record(self):
        # The hook builds the coarse, marker-safe, trust-critical record and hands it to the emit-and-done
        # seam; it NO LONGER holds telemetry's GitHub boundary (that reach-in was the inverted seam that was fixed).
        import telemetry
        captured = {}

        def fake_emit(record, *, gh=None):
            captured["record"] = record
            captured["gh"] = gh
            return 77
        with mock.patch.object(telemetry, "emit_finding", side_effect=fake_emit):
            got = hooks._do_promote_fail_open("PreToolUse", "input", "could not read input")
        self.assertTrue(got)                                # a landed promotion -> truthy
        self.assertIsNone(captured["gh"])                   # telemetry resolves the boundary, not the hook
        self.assertEqual(captured["record"]["source_id"], "hooks/fail-open/PreToolUse/input")
        self.assertEqual(captured["record"]["severity"], telemetry.TRUST_CRITICAL)
        self.assertEqual(captured["record"]["message"], "could not read input")


class TestMissingRuntimeReadout(unittest.TestCase):
    """#391: when the venv interpreter never appears, the launcher NAMES the absent runtime on stderr and
    stays NON-blocking, instead of exiting silently (the missing-runtime variant of fail-open-and-flag)."""

    WRAPPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hook-runner.sh")

    def test_names_the_absent_runtime_and_stays_non_blocking(self):
        with tempfile.TemporaryDirectory() as td:
            interp = os.path.join(td, "python")             # never created -> never appears
            r = subprocess.run(["sh", self.WRAPPER, interp, os.path.join(td, "boot.py")],
                               capture_output=True, text=True, timeout=10,
                               env={**os.environ, "ENGINE_HOOK_WAIT_POLLS": "3",
                                    "ENGINE_HOOK_WAIT_INTERVAL": "0.05"})
            self.assertEqual(r.stdout, "")                  # still ran nothing (no system-Python fallback)
            self.assertNotEqual(r.returncode, 2)            # NON-blocking (never the platform's block code)
            self.assertNotEqual(r.returncode, 0)            # and did not silently succeed
            self.assertIn("private Python runtime is not ready", r.stderr)   # names the absent runtime
            self.assertIn(interp, r.stderr)                 # the concrete path, for a literate operator
            self.assertIn("not a block", r.stderr)          # tells the operator it did not block


class TestIsGitCommit(unittest.TestCase):
    """The shared `git commit` classifier the commit-boundary hooks agree on (factored out of the
    per-consumer copies in self_map / knowledge_gen). Command-start anchored; degrades safe."""

    def test_true_on_commit_amend_and_compound(self):
        for cmd in ("git commit -m 'x'", "git commit --amend", "git add -A && git commit -m y",
                    "git status\ngit commit -m z"):
            p = {"tool_name": "Bash", "tool_input": {"command": cmd}}
            self.assertTrue(hooks._is_git_commit(p), cmd)

    def test_false_on_non_commit_non_bash_and_malformed(self):
        self.assertFalse(hooks._is_git_commit(
            {"tool_name": "Bash", "tool_input": {"command": "git status"}}))
        self.assertFalse(hooks._is_git_commit(
            {"tool_name": "Bash", "tool_input": {"command": "git log --oneline"}}))
        # a non-Bash tool never fires, even if its input text contains the words
        self.assertFalse(hooks._is_git_commit(
            {"tool_name": "Read", "tool_input": {"file_path": "git commit"}}))
        # an echoed / quoted occurrence is not at a command-start position -> no fire
        self.assertFalse(hooks._is_git_commit(
            {"tool_name": "Bash", "tool_input": {"command": "echo 'git commit'"}}))
        self.assertFalse(hooks._is_git_commit({"tool_name": "Bash"}))   # no tool_input
        self.assertFalse(hooks._is_git_commit(None))                    # malformed
        self.assertFalse(hooks._is_git_commit({}))
        # a non-string command degrades safe (no TypeError, no spurious match)
        for bad in (["git", "commit"], 123, {"x": 1}, None):
            self.assertFalse(hooks._is_git_commit(
                {"tool_name": "Bash", "tool_input": {"command": bad}}), repr(bad))


class TestCapShed(unittest.TestCase):
    """The #495 measure-and-shed contract, at the helper's own seam: order preserved, pinned never shed,
    notice counted — and content always beats the label (a class is never shed to fit the notice)."""

    def _blocks(self, p0="G" * 100, p1="D" * 100, p2="O" * 100):
        return [(0, "governance", p0), (2, "orientation", p2), (1, "dashboard", p1)]

    def test_wide_cap_keeps_all_in_original_order(self):
        text, shed = hooks.cap_shed(self._blocks("AAA", "BBB", "CCC"), cap=1000,
                                    notice=lambda names: "N:" + ",".join(names))
        self.assertEqual(text, "AAA\nCCC\nBBB")     # original order, priorities never reorder
        self.assertEqual(shed, [])

    def test_sheds_highest_priority_first_with_counted_notice(self):
        notice = lambda names: "shed:" + ",".join(names)
        text, shed = hooks.cap_shed(self._blocks(), cap=250, notice=notice)
        self.assertEqual(shed, ["orientation"])
        self.assertIn("shed:orientation", text)
        self.assertLessEqual(len(text), 250)

    def test_content_beats_the_label_notice_shrinks_not_another_class(self):
        # Governance+dashboard fit; the FULL notice would tip it over — the compact one must be used
        # and the dashboard KEPT (the converged #495 review finding: never shed 4.9k for a 390-char note).
        big_notice = lambda names: "X" * 60
        compact = lambda names: "c"
        text, shed = hooks.cap_shed(self._blocks(), cap=210, notice=big_notice, compact_notice=compact)
        self.assertEqual(shed, ["orientation"])
        self.assertIn("D" * 100, text)               # dashboard kept
        self.assertIn("c", text)
        self.assertLessEqual(len(text), 210)

    def test_notice_drops_entirely_before_content_does(self):
        big_notice = lambda names: "X" * 60
        compact = lambda names: "y" * 50
        text, shed = hooks.cap_shed(self._blocks(), cap=201, notice=big_notice, compact_notice=compact)
        self.assertEqual(shed, ["orientation"])
        self.assertIn("D" * 100, text)               # dashboard still kept
        self.assertNotIn("X", text)
        self.assertNotIn("y", text)                  # even the compact notice gave way to content
        self.assertLessEqual(len(text), 201)

    def test_pinned_alone_oversize_is_emitted_whole(self):
        text, shed = hooks.cap_shed([(0, "governance", "G" * 500)], cap=100)
        self.assertEqual(text, "G" * 500)            # never truncated; documented tradeoff
        self.assertEqual(shed, [])


class TestCrashDebugHermeticGuard(unittest.TestCase):
    """#495's rider: under the test harness, _record_crash_debug with the DEFAULT path writes nothing
    (production crash-log stays clean); an explicit path (the unit tests' own temp file) still writes."""

    def test_default_path_is_a_noop_under_unittest(self):
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "crash.log")
            class _T:                                 # stands in for the lazy telemetry import's constant
                HOOK_CRASH_DEBUG_PATH = target
            with mock.patch.dict(sys.modules, {"telemetry": _T}):
                wrote = hooks._record_crash_debug("PreToolUse", RuntimeError("boom"))
            self.assertFalse(os.path.exists(target))
            self.assertIs(wrote, False)          # the hermetic no-op reports it wrote nothing

    def test_explicit_path_still_writes(self):
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "crash.log")
            wrote = hooks._record_crash_debug("PreToolUse", RuntimeError("boom"), path=target)
            with open(target, encoding="utf-8") as fh:
                self.assertIn("RuntimeError: boom", fh.read())
            self.assertIs(wrote, True)           # a real append reports it wrote




class TestPostCompactionOwnerCoexistsWithMemory(unittest.TestCase):
    """The compact lifecycle now has two engine owners. They must not collide, and memory's
    single-fire housekeeping must be exactly as it was.

    The hazard this guards is a plausible alternative design: putting re-grounding on PreCompact
    beside memory's compaction trigger, or adding the memory sweeps to the compact matcher. Either
    would make memory's once-per-compaction work fire twice, or ask an event that cannot inject to
    inject. These assert the split that avoids both.
    """

    def _settings(self):
        return validate.load_json(os.path.join(validate.ROOT, ".claude/settings.json"))

    def _groups(self, event):
        return self._settings().get("hooks", {}).get(event, [])

    def test_precompact_is_still_memory_only(self):
        self.assertEqual(hooks.EVENT_INVENTORY["PreCompact"]["owners"], ("memory",))
        self.assertFalse(hooks.EVENT_INVENTORY["PreCompact"]["injects"],
                         "PreCompact cannot inject, which is why re-grounding could never live there")
        commands = [h["command"] for g in self._groups("PreCompact") for h in g["hooks"]]
        self.assertEqual(len(commands), 1, "memory's compaction trigger, and nothing else")
        self.assertIn("memory/compact.py", commands[0])

    def test_the_regrounding_owner_is_registered_on_sessionstart(self):
        self.assertIn("build-coordinator", hooks.EVENT_INVENTORY["SessionStart"]["owners"])
        self.assertTrue(hooks.EVENT_INVENTORY["SessionStart"]["injects"])

    def test_the_compact_matcher_carries_only_the_regrounding_owner(self):
        compact = [g for g in self._groups("SessionStart") if g.get("matcher") == "compact"]
        self.assertEqual(len(compact), 1)
        commands = [h["command"] for h in compact[0]["hooks"]]
        self.assertEqual(len(commands), 1)
        self.assertIn("build_coordinator.py", commands[0])
        self.assertIn("reground-hook", commands[0])

    def test_memory_does_not_also_run_on_the_compact_matcher(self):
        # If it did, one compaction would run memory's session-start work twice: once on PreCompact
        # and again here. The whole point of the matcher split is that it does not.
        compact = [g for g in self._groups("SessionStart") if g.get("matcher") == "compact"]
        commands = " ".join(h["command"] for g in compact for h in g["hooks"])
        self.assertNotIn("memory/", commands)

    def test_boot_does_not_run_on_the_compact_matcher(self):
        # Boot deliberately never runs its full pack after a compaction — that absence is what leaves
        # a compacted session unoriented, and what the narrow pointer exists to fill.
        compact = [g for g in self._groups("SessionStart") if g.get("matcher") == "compact"]
        commands = " ".join(h["command"] for g in compact for h in g["hooks"])
        self.assertNotIn("boot.py", commands)

    def test_one_compaction_lifecycle_fires_each_owner_exactly_once(self):
        # The lifecycle as the platform runs it: PreCompact, then SessionStart(compact).
        pre = [h["command"] for g in self._groups("PreCompact") for h in g["hooks"]]
        post = [h["command"] for g in self._groups("SessionStart")
                if g.get("matcher") == "compact" for h in g["hooks"]]
        self.assertEqual(len(pre), 1, "memory acts once")
        self.assertEqual(len(post), 1, "re-grounding acts once")
        self.assertEqual(len(set(pre) & set(post)), 0, "and never the same command twice")


# --- Accepted automatic-hook dispatch ---------------------------------------------------------------

_ACCEPTED_TOOLS = Path(__file__).resolve().parent


def _accepted_call(*args, cwd=None, env=None, check=True):
    proc = subprocess.run(list(args), cwd=cwd, env=env, capture_output=True, text=True, timeout=30)
    if check and proc.returncode != 0:
        raise AssertionError(f"command failed ({proc.returncode}): {args!r}\n{proc.stdout}\n{proc.stderr}")
    return proc


class _AcceptedDispatchRepo:
    """A real clone + linked worktree fixture for the issue StarshipSuperjam/engine-template#1151 split-brain topology."""

    def __init__(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "main"
        self.worktree = Path(self.temp.name) / "candidate"
        self.poison = Path(self.temp.name) / "poison"
        self.marker = Path(self.temp.name) / "startup-ran"
        self.root.mkdir()
        _accepted_call("git", "init", "-b", "main", str(self.root))
        _accepted_call("git", "-C", str(self.root), "config", "user.email", "fixture@example.test")
        _accepted_call("git", "-C", str(self.root), "config", "user.name", "Fixture")
        _accepted_call("git", "-C", str(self.root), "remote", "add", "origin",
                       "https://github.com/owner/project.git")
        self._accepted_tree()
        _accepted_call("git", "-C", str(self.root), "add", ".")
        _accepted_call("git", "-C", str(self.root), "commit", "-m", "accepted")
        self.commit = self.git("rev-parse", "HEAD")
        self.tree = self.git("rev-parse", "HEAD^{tree}")
        self.fake_bin = Path(self.temp.name) / "bin"
        self.fake_bin.mkdir()
        fake_gh = self.fake_bin / "gh"
        fake_gh.write_text(textwrap.dedent("""\
            #!/usr/bin/env python3
            import json, os, sys
            endpoint = sys.argv[-1]
            commit = os.environ["ENGINE_TEST_ACCEPTED_COMMIT"]
            if endpoint == "repos/owner/project":
                print(json.dumps({"default_branch": os.environ.get("ENGINE_TEST_GH_DEFAULT", "main")}))
            elif endpoint.endswith("/pulls"):
                if os.environ.get("ENGINE_TEST_GH_REFUSE") == "1":
                    print("[]")
                    raise SystemExit(0)
                print(json.dumps([{"number": 42, "merged_at": "2026-01-01T00:00:00Z",
                    "merge_commit_sha": commit,
                    "base": {"ref": os.environ.get("ENGINE_TEST_GH_DEFAULT", "main")}}]))
            elif "/releases/tags/" in endpoint:
                print(json.dumps({"id": 77, "tag_name": endpoint.rsplit("/", 1)[-1]}))
            elif "/git/ref/tags/" in endpoint:
                print(json.dumps({"object": {"type": "commit",
                    "sha": os.environ.get("ENGINE_TEST_GH_TAG_SHA", commit)}}))
            elif "/actions/workflows/release-publish.yml/runs?" in endpoint:
                print(json.dumps({"workflow_runs": [{"id": 88, "head_sha": commit,
                    "conclusion": "success"}]}))
            else:
                raise SystemExit(1)
            """), encoding="utf-8")
        fake_gh.chmod(0o755)
        _accepted_call("git", "-C", str(self.root), "worktree", "add", "-b", "candidate",
                       str(self.worktree), self.commit)
        self._refresh_paths()

    def cleanup(self):
        self.temp.cleanup()

    def git(self, *args):
        return _accepted_call("git", "-C", str(self.root), *args).stdout.strip()

    def _put(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _accepted_tree(self):
        for name in ("accepted_hook_dispatch.py", "release_source.py", "moment.py", "mutation_guards.py",
                     "hook-runner.sh", "codex-hook-runner.sh", "providers.py", "hooks_path_health.py"):
            self._put(f".engine/tools/{name}", (_ACCEPTED_TOOLS / name).read_text(encoding="utf-8"))
        for rel in (".claude/settings.json", ".codex/hooks.json"):
            self._put(rel, (_ACCEPTED_TOOLS.parents[1] / rel).read_text(encoding="utf-8"))
        self._put(".engine/tools/validate.py",
                  "from pathlib import Path\nROOT = str(Path(__file__).resolve().parents[2])\n")
        self._put(".engine/tools/helper.py",
                  "from pathlib import Path\nVALUE = 'accepted'\nORIGIN = str(Path(__file__).resolve())\n")
        self._put(".engine/tools/close.py", textwrap.dedent("""\
            import json, os
            import helper, validate
            context = json.loads(os.environ["ENGINE_ACCEPTED_HOOK_CONTEXT"])
            print(json.dumps({"value": helper.VALUE, "helper_origin": helper.ORIGIN,
                "validate_origin": validate.__file__, "root": validate.ROOT,
                "memory_dir": os.environ.get("ENGINE_MEMORY_DIR"),
                "provider": os.environ.get("ENGINE_PROVIDER"), "context": context}, sort_keys=True))
            """))
        self._put(".engine/tools/boot.py", "raise SystemExit(0)\n")
        self._put(".engine/tools/memory/__init__.py", "")
        for name in ("execution_context.py", "mutation_contract.py", "mutation_authority.py",
                     "candidate_invocation.py", "qualification_health.py"):
            self._put(f".engine/tools/memory/{name}",
                      (_ACCEPTED_TOOLS / "memory" / name).read_text(encoding="utf-8"))
        for name in ("compact.py", "erasure_observer.py", "backup_vault.py"):
            self._put(f".engine/tools/memory/{name}", "raise SystemExit(0)\n")
        self._put(".engine/tools/memory/mcp_server.py", textwrap.dedent("""\
            import json, os
            from memory import execution_context
            context = execution_context.current_context().to_document()
            print(json.dumps({"operation": context["operation"],
                              "memory_dir": os.environ.get("ENGINE_MEMORY_DIR")}, sort_keys=True))
            """))
        # Shape-matched to the committed `.engine/engine.json` — see the fixture-shape guard test, which now
        # checks BOTH directions, so this carries every key the real manifest carries and no others. The
        # dispatcher reads GitHub for the default branch, so a `default_branch` key here would be a fiction.
        # Values are fixture values; only the SHAPE is copied.
        _real_manifest = json.loads(
            (_ACCEPTED_TOOLS.parents[1] / ".engine/engine.json").read_text(encoding="utf-8"))
        self._put(".engine/engine.json",
                  json.dumps({**_real_manifest, "engine_release": "9.9.9"}) + "\n")
        self._put(".engine/memory-backup/pointer.json", json.dumps({
            "schema_version": 1, "owner": "vault-owner", "repo": "vault", "branch": "main",
            "namespace": "project-id"}) + "\n")

    def _refresh_paths(self):
        # Prefer the linked candidate worktree (that split-brain is the whole point of the fixture), but fall
        # back to the main checkout once a test has retired the worktree.
        self.home = self.worktree if (self.worktree / ".engine/tools").is_dir() else self.root
        self.dispatcher = self.home / ".engine/tools/accepted_hook_dispatch.py"
        self.script = self.home / ".engine/tools/close.py"

    def activate(self, *, source="reviewed-merge", source_ref="refs/heads/main", expected_epoch=0,
                 commit=None, accepted_proof=True, default_branch="main", tag_commit=None):
        selected = commit or self.commit
        env = dict(os.environ)
        env["PATH"] = str(self.fake_bin) + os.pathsep + env.get("PATH", "")
        env["ENGINE_TEST_ACCEPTED_COMMIT"] = selected
        if not accepted_proof:
            env["ENGINE_TEST_GH_REFUSE"] = "1"
        env["ENGINE_TEST_GH_DEFAULT"] = default_branch
        if tag_commit is not None:
            env["ENGINE_TEST_GH_TAG_SHA"] = tag_commit
        return _accepted_call(
            sys.executable, str(self.dispatcher), "activate", "--root", str(self.home),
            "--repository", "owner/project", "--commit", selected, "--source", source,
            "--source-ref", source_ref, "--engine-release", "9.9.9", "--expected-epoch",
            str(expected_epoch), check=False, env=env)

    def ensure(self, *, accepted_proof=True, ambient=False, commit=None, extra_env=None):
        env = dict(os.environ)
        env["PATH"] = str(self.fake_bin) + os.pathsep + env.get("PATH", "")
        env["ENGINE_TEST_ACCEPTED_COMMIT"] = commit or self.git("rev-parse", "main")
        if not accepted_proof:
            env["ENGINE_TEST_GH_REFUSE"] = "1"
        env.update(extra_env or {})
        argv = [sys.executable, str(self.dispatcher), "ensure", "--root", str(self.home)]
        if ambient:
            argv.append("--ambient")
        return _accepted_call(*argv, check=False, env=env)

    def coverage(self) -> dict:
        """The worktree-coverage disclosure that replaced StarshipSuperjam/engine-template#1153's activation barrier."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            f"_fixture_dispatch_{id(self)}", str(self.root / ".engine/tools/accepted_hook_dispatch.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.uncovered_worktrees(str(self.root))

    def common_dir(self):
        raw = self.git("rev-parse", "--git-common-dir")
        return Path(raw) if os.path.isabs(raw) else self.root / raw

    def dirty(self):
        (self.worktree / ".engine/tools/helper.py").write_text(
            "from pathlib import Path\nVALUE = 'candidate'\nORIGIN = str(Path(__file__).resolve())\n",
            encoding="utf-8")
        self.script.write_text(
            f"from pathlib import Path\nPath({str(self.marker)!r}).write_text('candidate-ran')\n",
            encoding="utf-8")

    def poison_env(self):
        self.poison.mkdir(exist_ok=True)
        (self.poison / "helper.py").write_text("VALUE='environment'\nORIGIN=__file__\n", encoding="utf-8")
        for name in ("sitecustomize.py", "usercustomize.py", "startup.py"):
            (self.poison / name).write_text(
                f"from pathlib import Path\nPath({str(self.marker)!r}).write_text({name!r})\n",
                encoding="utf-8")
        return {**os.environ, "PYTHONPATH": str(self.poison), "PYTHONUSERBASE": str(self.poison),
                "PYTHONSTARTUP": str(self.poison / "startup.py"),
                "ENGINE_MEMORY_DIR": str(self.poison / "memory"), "ENGINE_PROVIDER": "codex"}

    def run_direct(self, env=None):
        return _accepted_call(sys.executable, "-I", "-S", str(self.dispatcher), "run", "--root",
                              str(self.worktree), "--script", str(self.script), "--", env=env, check=False)

    def run_attended(self, operation="attended-memory-mcp", script=None, *target_args):
        target = script or ".engine/tools/memory/mcp_server.py"
        return _accepted_call(
            sys.executable, "-I", "-S", str(self.dispatcher), "attended", "--root", str(self.worktree),
            "--script", target, "--operation", operation, "--", *target_args, check=False,
        )

    def _provision(self):
        bindir = self.worktree / ".engine/.venv/bin"
        bindir.mkdir(parents=True, exist_ok=True)
        python = bindir / "python"
        if not python.exists():
            python.symlink_to(sys.executable)

    def run_launcher(self, provider, env):
        return self.run_launcher_script(provider, ".engine/tools/close.py", env)

    def run_launcher_script(self, provider, rel, env, *args):
        self._provision()
        if provider == "codex":
            return _accepted_call("sh", ".engine/tools/codex-hook-runner.sh", rel, *args,
                                  cwd=str(self.worktree), env=env, check=False)
        clean = dict(env)
        clean.pop("ENGINE_PROVIDER", None)
        return _accepted_call("sh", str(self.worktree / ".engine/tools/hook-runner.sh"),
                              str(self.worktree / ".engine/.venv/bin/python"),
                              str(self.worktree / rel), *args,
                              cwd=str(self.worktree), env=clean, check=False)

    def canonical_inventory(self):
        inventory = {}
        for base in (self.root / ".engine/memory", self.root / ".engine/memory-backup"):
            if not base.exists():
                continue
            for path in sorted(item for item in base.rglob("*") if item.is_file()):
                inventory[str(path.relative_to(self.root))] = hashlib.sha256(path.read_bytes()).hexdigest()
        return inventory

    def qualification_health(self):
        path = self.common_dir() / "engine/accepted-hooks/qualification-health.json"
        return json.loads(path.read_text(encoding="utf-8"))


class TestAcceptedAutomaticHookDispatch(unittest.TestCase):
    def setUp(self):
        self.repo = _AcceptedDispatchRepo()

    def tearDown(self):
        self.repo.cleanup()

    def test_activation_schema_exact_objects_epoch_cas_and_legacy_barrier(self):
        import jsonschema
        schema_path = _ACCEPTED_TOOLS.parent / "schemas/accepted-hook-activation.v1.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        self.assertFalse(schema["additionalProperties"])
        activated = self.repo.activate()
        self.assertEqual(activated.returncode, 0, activated.stderr)
        record = json.loads(activated.stdout)
        self.assertEqual((record["commit"], record["tree"], record["epoch"]),
                         (self.repo.commit, self.repo.tree, 1))
        path = self.repo.common_dir() / "engine/accepted-hooks/activation.json"
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), record)
        stale = self.repo.activate(expected_epoch=0)
        self.assertEqual(stale.returncode, 1)
        self.assertNotEqual(stale.returncode, 2)
        self.assertIn("compare-and-set", stale.stderr)
        # A pre-fix linked worktree used to REFUSE this activation. It no longer does (StarshipSuperjam/engine-template#1158): that worktree
        # runs its own old wiring whether or not this machine's activation advances, so refusing only stripped
        # protection from the sessions that could have had it. It is now counted and named instead.
        (self.repo.worktree / ".engine/tools/hook-runner.sh").write_text("#!/bin/sh\nexit 0\n",
                                                                         encoding="utf-8")
        legacy = self.repo.activate(expected_epoch=1)
        self.assertEqual(legacy.returncode, 0, legacy.stderr)
        self.assertEqual(json.loads(legacy.stdout)["epoch"], 2)
        coverage = self.repo.coverage()
        self.assertTrue(coverage["readable"])
        self.assertEqual(coverage["uncovered"], 1)
        self.assertTrue(any("candidate" in item for item in coverage["sample"]))

    def test_activation_requires_independent_github_acceptance_proof(self):
        refused = self.repo.activate(accepted_proof=False)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("no merged GitHub pull request", refused.stderr)
        self.assertNotIn("did not block the host action", refused.stderr)
        self.assertFalse((self.repo.common_dir() / "engine/accepted-hooks/activation.json").exists())

    def test_candidate_manifest_cannot_redefine_the_github_default_branch(self):
        self.repo.git("branch", "staging", self.repo.commit)
        refused = self.repo.activate(source_ref="refs/heads/staging")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("GitHub's current default branch", refused.stderr)

    def test_github_default_branch_may_be_a_valid_slash_name(self):
        self.repo.git("branch", "release/stable", self.repo.commit)
        activated = self.repo.activate(
            source_ref="refs/heads/release/stable", default_branch="release/stable")
        self.assertEqual(activated.returncode, 0, activated.stderr)
        self.assertEqual(json.loads(activated.stdout)["source_ref"], "refs/heads/release/stable")

    def test_release_proof_binds_the_github_tag_ref_to_the_selected_commit(self):
        self.repo.git("tag", "v9.9.9", self.repo.commit)
        refused = self.repo.activate(
            source="published-release", source_ref="v9.9.9", tag_commit="f" * 40)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("release tag does not name", refused.stderr)

    def test_attended_ensure_bootstraps_once_and_keeps_the_exact_activation_offline(self):
        first = self.repo.ensure()
        self.assertEqual(first.returncode, 0, first.stderr)
        record = json.loads(first.stdout)
        self.assertEqual(record["commit"], self.repo.commit)
        second = self.repo.ensure(accepted_proof=False)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout), record)

    def test_attended_ensure_preserves_an_older_rollback_epoch_after_canonical_advances_offline(self):
        first = self.repo.ensure()
        self.assertEqual(first.returncode, 0, first.stderr)
        rollback = json.loads(first.stdout)
        rollback["epoch"] = 3
        activation = self.repo.common_dir() / "engine/accepted-hooks/activation.json"
        activation.write_text(json.dumps(rollback, sort_keys=True) + "\n", encoding="utf-8")
        (self.repo.root / "product.txt").write_text("newer canonical product commit\n", encoding="utf-8")
        self.repo.git("add", "product.txt")
        self.repo.git("commit", "-m", "advance canonical checkout")
        (self.repo.fake_bin / "gh").write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
        preserved = self.repo.ensure()
        self.assertEqual(preserved.returncode, 0, preserved.stderr)
        self.assertEqual(json.loads(preserved.stdout), rollback)

    def test_attended_maintenance_reenters_exact_accepted_code_with_one_registered_operation(self):
        self.assertEqual(self.repo.activate().returncode, 0)
        result = self.repo.run_attended()
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["operation"]["registry_id"], "attended-memory-mcp")
        self.assertEqual(payload["operation"]["invocation_mode"], "attended")
        self.assertEqual(payload["memory_dir"], os.path.realpath(self.repo.root / ".engine/memory"))
        mismatch = self.repo.run_attended("attended-pin-add")
        self.assertEqual(mismatch.returncode, 1)
        self.assertIn("does not belong", mismatch.stderr)

    def test_unqualified_generations_are_disclosed_and_activation_still_succeeds(self):
        mutations = {
            "dirty": lambda path: path.write_text(path.read_text(encoding="utf-8") + "\n# dirty\n",
                                                   encoding="utf-8"),
            "ambiguous": lambda path: path.write_text(
                path.read_text(encoding="utf-8") + "\n# ENGINE_ACCEPTED_HOOK_DISPATCH=1\n",
                encoding="utf-8"),
            "missing": lambda path: path.unlink(),
            "unreadable": lambda path: (path.unlink(), path.symlink_to(os.devnull)),
        }
        for expected, mutate in mutations.items():
            fixture = _AcceptedDispatchRepo()
            try:
                runner = fixture.worktree / ".engine/tools/hook-runner.sh"
                mutate(runner)
                activated = fixture.activate()
                self.assertEqual(activated.returncode, 0, (expected, activated.stderr))
                self.assertTrue((fixture.common_dir() / "engine/accepted-hooks/activation.json").exists())
                coverage = fixture.coverage()
                self.assertEqual(coverage["uncovered"], 1, (expected, coverage))
                self.assertTrue(any(expected in item for item in coverage["sample"]),
                                (expected, coverage["sample"]))
            finally:
                fixture.cleanup()

    def test_worktree_coverage_is_reported_and_never_gates_activation(self):
        """The barrier's replacement. Activation must not depend on worktree topology at all — including a
        topology that changes underneath it — and the coverage gap must be legible instead."""
        import accepted_hook_dispatch
        self.assertFalse(hasattr(accepted_hook_dispatch, "_verify_activation_barrier"))
        self.assertFalse(hasattr(accepted_hook_dispatch, "_verify_unchanged_activation_barrier"))
        clean = self.repo.coverage()
        self.assertEqual((clean["readable"], clean["uncovered"], clean["total"]), (True, 0, 2))
        (self.repo.worktree / ".engine/tools/hook-runner.sh").write_text("#!/bin/sh\nexit 0\n",
                                                                         encoding="utf-8")
        activated = self.repo.activate()
        self.assertEqual(activated.returncode, 0, activated.stderr)
        degraded = self.repo.coverage()
        self.assertEqual(degraded["uncovered"], 1)
        self.assertEqual(len(degraded["sample"]), 1)
        # The disclosure names the state and the branch, and still exposes no filesystem path.
        self.assertNotIn(str(self.repo.worktree), repr(degraded))

    def test_a_census_that_cannot_answer_reports_unreadable_instead_of_reporting_clean(self):
        """The disclosure is the WHOLE of what replaced the refusal, so the one thing it must never do is go
        quiet in the states where the machine cannot tell whether it is covered.

        Three census verdicts — `unreadable` (git could not list), `ambiguous` (no paths, or duplicates) and
        `concurrent-change` (the list moved mid-read) — return with an EMPTY worktree list rather than
        raising. That counted as zero offenders and rendered nothing at all: silence that reads exactly like
        "everything is covered".
        """
        import accepted_hook_dispatch
        import engine_status
        for state in ("unreadable", "ambiguous", "concurrent-change"):
            with self.subTest(state=state):
                with mock.patch.object(accepted_hook_dispatch, "_activation_topology",
                                       return_value={"state": state, "qualified": False, "worktrees": []}):
                    coverage = accepted_hook_dispatch.uncovered_worktrees(str(self.repo.root))
                self.assertFalse(coverage["readable"], coverage)
                rendered = engine_status._render_activation_state(
                    {"activation": {"commit": "a" * 40}, "coverage": coverage})
                self.assertIn("could not be read", rendered)

    def test_retiring_a_legacy_worktree_clears_the_disclosure_without_rewriting_payload(self):
        """Cleanup is what closes the coverage gap — and the gap never blocked activation to begin with."""
        runner = self.repo.worktree / ".engine/tools/hook-runner.sh"
        runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        before = self.repo.canonical_inventory()
        activated = self.repo.activate()
        self.assertEqual(activated.returncode, 0, activated.stderr)
        self.assertEqual(json.loads(activated.stdout)["epoch"], 1)
        self.assertEqual(self.repo.coverage()["uncovered"], 1)

        _accepted_call("git", "-C", str(self.repo.root), "worktree", "remove", "--force",
                       str(self.repo.worktree))
        self.repo.git("branch", "-D", "candidate")
        self.repo._refresh_paths()
        self.assertEqual(self.repo.coverage()["uncovered"], 0)
        stale = self.repo.activate(expected_epoch=0)  # the epoch CAS still holds after the cleanup
        self.assertEqual(stale.returncode, 1)
        self.assertIn("compare-and-set", stale.stderr)
        self.assertEqual(self.repo.canonical_inventory(), before)

    def test_rollback_is_a_new_epoch_to_a_prior_safe_acceptance_and_legacy_never_reactivates(self):
        before = self.repo.canonical_inventory()
        first = self.repo.activate()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.repo.git("tag", "safe-a", self.repo.commit)

        (self.repo.root / "reviewed.txt").write_text("reviewed successor\n", encoding="utf-8")
        self.repo.git("add", "reviewed.txt")
        self.repo.git("commit", "-m", "reviewed successor")
        successor = self.repo.git("rev-parse", "HEAD")
        advanced = self.repo.activate(commit=successor, expected_epoch=1)
        self.assertEqual(advanced.returncode, 0, advanced.stderr)
        self.assertEqual(json.loads(advanced.stdout)["epoch"], 2)

        rolled_back = self.repo.activate(
            source="published-release", source_ref="safe-a", commit=self.repo.commit, expected_epoch=2)
        self.assertEqual(rolled_back.returncode, 0, rolled_back.stderr)
        rollback_record = json.loads(rolled_back.stdout)
        self.assertEqual((rollback_record["commit"], rollback_record["epoch"]), (self.repo.commit, 3))
        self.assertEqual(self.repo.canonical_inventory(), before)

        # A legacy worktree appearing after the rollback is disclosed, not a veto on the next epoch; and the
        # rollback itself remains a forward epoch, never a rewind of the counter.
        (self.repo.worktree / ".engine/tools/hook-runner.sh").write_text(
            "#!/bin/sh\nexit 0\n", encoding="utf-8")
        onward = self.repo.activate(commit=successor, expected_epoch=3)
        self.assertEqual(onward.returncode, 0, onward.stderr)
        current = json.loads(
            (self.repo.common_dir() / "engine/accepted-hooks/activation.json").read_text(encoding="utf-8"))
        self.assertEqual((current["commit"], current["epoch"]), (successor, 4))
        self.assertEqual(self.repo.coverage()["uncovered"], 1)
        self.assertEqual(self.repo.canonical_inventory(), before)

    def test_dirty_worktree_and_python_poison_run_only_accepted_code_and_canonical_state(self):
        self.assertEqual(self.repo.activate().returncode, 0)
        self.repo.dirty()
        result = self.repo.run_direct(self.repo.poison_env())
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["value"], "accepted")
        self.assertIn("accepted-hooks/trees", receipt["helper_origin"])
        self.assertIn("accepted-hooks/trees", receipt["validate_origin"])
        self.assertEqual(receipt["root"], str(self.repo.root.resolve()))
        self.assertEqual(receipt["memory_dir"], str((self.repo.root / ".engine/memory").resolve()))
        self.assertEqual(receipt["context"]["canonical"]["backup_pointer_identity"]["namespace"],
                         "project-id")
        self.assertFalse(self.repo.marker.exists())
        (self.repo.root / ".engine/memory-backup/pointer.json").write_text("{}\n", encoding="utf-8")
        refused = self.repo.run_direct(self.repo.poison_env())
        self.assertEqual(refused.returncode, 1)
        self.assertIn("canonical backup pointer differs", refused.stderr)
        self.assertFalse(self.repo.marker.exists())

    def _home_shape(self, *, committed_pointer):
        """Re-commit the fixture as the engine's OWN home repo (origin == the accepted manifest's
        `home_repository`) with the given committed pointer, and activate that commit. The subprocess
        dispatcher judges home from the fixture's real origin and the ACCEPTED tree's manifest, so this —
        not an in-process monkeypatch it would never see — is what genuinely reaches the carve-out."""
        manifest = json.loads((self.repo.root / ".engine/engine.json").read_text(encoding="utf-8"))
        manifest["home_repository"] = "owner/project"
        (self.repo.root / ".engine/engine.json").write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        (self.repo.root / ".engine/memory-backup/pointer.json").write_text(
            json.dumps(committed_pointer) + "\n", encoding="utf-8")
        self.repo.git("add", ".")
        self.repo.git("commit", "-m", "home-shaped")
        commit = self.repo.git("rev-parse", "HEAD")
        result = self.repo.activate(commit=commit)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_home_repo_mandated_pointer_split_qualifies_and_binds_the_live_pointer(self):
        # The home repo's mandated state — committed placeholder (the public-safety check forbids
        # committing the configured pointer there, #224) with the live configured pointer on disk —
        # qualifies instead of refusing, and the context binds the LIVE pointer's identity.
        self._home_shape(committed_pointer={"schema_version": 1, "configured": False})
        (self.repo.root / ".engine/memory-backup/pointer.json").write_text(json.dumps({
            "schema_version": 1, "owner": "vault-owner", "repo": "vault", "branch": "main",
            "namespace": "live-project-id"}) + "\n", encoding="utf-8")
        result = self.repo.run_direct(self.repo.poison_env())
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["value"], "accepted")
        self.assertEqual(receipt["context"]["canonical"]["backup_pointer_identity"]["namespace"],
                         "live-project-id")
        # A live pointer that does NOT parse as configured is not the mandated split — garbage on disk
        # still refuses even in the home repo, exactly as before the carve-out.
        (self.repo.root / ".engine/memory-backup/pointer.json").write_text("{}\n", encoding="utf-8")
        refused = self.repo.run_direct(self.repo.poison_env())
        self.assertEqual(refused.returncode, 1)
        self.assertIn("canonical backup pointer differs", refused.stderr)

    def test_home_repo_with_committed_configured_pointer_keeps_the_parity_refusal(self):
        # Home shape alone never excuses parity: with a CONFIGURED pointer committed, a differing live
        # pointer is genuine drift from operator-accepted state — the tamper the binding exists to catch.
        self._home_shape(committed_pointer={
            "schema_version": 1, "owner": "vault-owner", "repo": "vault", "branch": "main",
            "namespace": "project-id"})
        (self.repo.root / ".engine/memory-backup/pointer.json").write_text(json.dumps({
            "schema_version": 1, "owner": "vault-owner", "repo": "vault", "branch": "main",
            "namespace": "somewhere-else"}) + "\n", encoding="utf-8")
        refused = self.repo.run_direct(self.repo.poison_env())
        self.assertEqual(refused.returncode, 1)
        self.assertIn("canonical backup pointer differs", refused.stderr)

    def test_real_claude_and_codex_launchers_preserve_provider_and_closed_origins(self):
        self.assertEqual(self.repo.activate().returncode, 0)
        self.repo.dirty()
        env = self.repo.poison_env()
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                self.repo.marker.unlink(missing_ok=True)
                result = self.repo.run_launcher(provider, env)
                self.assertEqual(result.returncode, 0, result.stderr)
                receipt = json.loads(result.stdout)
                self.assertEqual((receipt["provider"], receipt["value"]), (provider, "accepted"))
                self.assertIn("accepted-hooks/trees", receipt["helper_origin"])
                self.assertFalse(self.repo.marker.exists())

    def test_repeated_precompact_refusal_is_bounded_nonblocking_and_recovers(self):
        self.repo.dirty()
        env = self.repo.poison_env()
        before = self.repo.canonical_inventory()
        outcomes = [
            self.repo.run_launcher_script(provider, ".engine/tools/memory/compact.py", env, "pre-compact")
            for provider in ("claude", "codex")
        ]
        self.assertTrue(all(result.returncode == 1 for result in outcomes))
        self.assertTrue(all(result.returncode != 2 for result in outcomes))
        self.assertEqual(self.repo.canonical_inventory(), before)
        self.assertFalse(self.repo.marker.exists())
        health = self.repo.qualification_health()
        self.assertEqual((health["status"], health["skipped_effect_count"]), ("degraded", 2))
        self.assertIn("Automatic memory work was skipped because", outcomes[0].stderr)
        self.assertNotIn("Automatic memory work is being skipped", outcomes[1].stderr)
        self.assertNotIn(self.repo.temp.name, json.dumps(health))

        activated = self.repo.activate()
        self.assertEqual(activated.returncode, 0, activated.stderr)
        recovered = self.repo.run_launcher_script(
            "codex", ".engine/tools/memory/compact.py", env, "pre-compact")
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        health = self.repo.qualification_health()
        self.assertEqual((health["status"], health["skipped_effect_count"]), ("healthy", 2))
        self.assertIsNotNone(health["last_recovery_at"])
        self.assertFalse(self.repo.marker.exists())

    def test_dirty_candidate_precompact_uses_both_real_provider_paths_without_canonical_drift(self):
        self.assertEqual(self.repo.activate().returncode, 0)
        initialized = self.repo.run_direct()
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        compact = self.repo.worktree / ".engine/tools/memory/compact.py"
        compact.write_text(
            f"from pathlib import Path\nPath({str(self.repo.marker)!r}).write_text('candidate-compact')\n",
            encoding="utf-8")
        env = self.repo.poison_env()
        before = self.repo.canonical_inventory()
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                result = self.repo.run_launcher_script(
                    provider, ".engine/tools/memory/compact.py", env, "pre-compact")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.repo.canonical_inventory(), before)
                self.assertFalse(self.repo.marker.exists())
        health = self.repo.qualification_health()
        self.assertEqual(health["status"], "healthy")
        self.assertEqual(health["last_receipt"]["effect"]["operation_id"], "automatic-compaction")

    def test_missing_and_corrupt_authority_never_fall_back_or_exit_two(self):
        self.repo.dirty()
        missing = self.repo.run_direct(self.repo.poison_env())
        self.assertEqual(missing.returncode, 1)
        self.assertNotEqual(missing.returncode, 2)
        self.assertFalse(self.repo.marker.exists())
        path = self.repo.common_dir() / "engine/accepted-hooks/activation.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not-json", encoding="utf-8")
        corrupt = self.repo.run_direct(self.repo.poison_env())
        self.assertEqual(corrupt.returncode, 1)
        self.assertNotEqual(corrupt.returncode, 2)
        self.assertFalse(self.repo.marker.exists())

    def test_published_tag_is_resolved_once_and_accepted_exit_two_is_preserved(self):
        self.repo.git("tag", "v9.9.9", self.repo.commit)
        activated = self.repo.activate(source="published-release", source_ref="v9.9.9")
        self.assertEqual(activated.returncode, 0, activated.stderr)
        self.repo.dirty()
        (self.repo.root / "later.txt").write_text("later\n", encoding="utf-8")
        self.repo.git("add", "later.txt")
        self.repo.git("commit", "-m", "later")
        self.repo.git("tag", "-f", "v9.9.9", "HEAD")
        still_exact = self.repo.run_direct(self.repo.poison_env())
        self.assertEqual(still_exact.returncode, 0, still_exact.stderr)
        self.assertEqual(json.loads(still_exact.stdout)["context"]["activation"]["commit"], self.repo.commit)

        # Legitimate target exit 2 is transparent; only qualification failures are normalized away from 2.
        (self.repo.root / ".engine/tools/close.py").write_text("raise SystemExit(2)\n", encoding="utf-8")
        self.repo.git("add", ".engine/tools/close.py")
        self.repo.git("commit", "-m", "accepted block")
        commit = self.repo.git("rev-parse", "HEAD")
        _accepted_call("git", "-C", str(self.repo.root), "worktree", "remove", "--force",
                       str(self.repo.worktree))
        self.repo.git("branch", "-D", "candidate")
        _accepted_call("git", "-C", str(self.repo.root), "worktree", "add", "-b", "candidate",
                       str(self.repo.worktree), commit)
        self.repo._refresh_paths()
        advanced = self.repo.activate(commit=commit, expected_epoch=1)
        self.assertEqual(advanced.returncode, 0, advanced.stderr)
        self.assertEqual(self.repo.run_direct().returncode, 2)


class TestAmbientActivationLifecycle(unittest.TestCase):
    """The lifecycle StarshipSuperjam/engine-template#1153 could not reach: activation that bootstraps and advances on its own, bounded,
    non-interactive, forward-only, and degrading to a notice rather than an exception (issue StarshipSuperjam/engine-template#1158)."""

    def setUp(self):
        self.repo = _AcceptedDispatchRepo()

    def tearDown(self):
        self.repo.cleanup()

    def _ambient(self, **kwargs) -> dict:
        result = self.repo.ensure(ambient=True, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def _advance_canonical(self) -> str:
        (self.repo.root / "product.txt").write_text("a merged change\n", encoding="utf-8")
        self.repo.git("add", "product.txt")
        self.repo.git("commit", "-m", "merged change")
        return self.repo.git("rev-parse", "HEAD")

    def test_fixture_manifests_match_the_shape_of_the_committed_manifest(self):
        """The guard for the defect class that broke StarshipSuperjam/engine-template#1153: the dispatcher read `engine_version` while the
        real `.engine/engine.json` has always carried `engine_release`, and the fixture invented the same
        wrong key — so the suite agreed with the bug.

        Both directions, because the guard was one-directional and the mirror-image failure is the same
        defect class: a fixture that INVENTS a key lets the suite agree with code reading a name that does not
        exist, and a fixture MISSING a key the real manifest carries lets the suite agree with code that never
        has to handle it. Neither is a byte-shape copy."""
        real = json.loads((_ACCEPTED_TOOLS.parents[1] / ".engine/engine.json").read_text(encoding="utf-8"))
        fixture = json.loads((self.repo.root / ".engine/engine.json").read_text(encoding="utf-8"))
        self.assertEqual(set(fixture) - set(real), set(),
                         "fixture manifest invents keys the committed manifest does not have")
        self.assertEqual(set(real) - set(fixture), set(),
                         "fixture manifest is missing keys the committed manifest carries")
        self.assertIn("engine_release", fixture)
        self.assertIn("engine_release", real)

    def test_ambient_bootstraps_when_absent_and_says_so(self):
        result = self._ambient()
        self.assertEqual(result["activation"]["commit"], self.repo.commit)
        self.assertEqual(result["activation"]["epoch"], 1)
        self.assertEqual(len(result["notices"]), 1)
        notice = result["notices"][0]
        # Asserted on MEANING, not on a phrase. The wording changed once already, when the deliverable review
        # found it was telling a non-developer that "the code that may write memory is now the tree of that
        # merge". What has to hold is that the operator is told writing works now and which merged commit it
        # is running — with none of the internal vocabulary.
        self.assertIn("can now write", notice)
        self.assertIn(self.repo.commit[:12], notice)
        for jargon in ("tree", "epoch", "activation"):
            self.assertNotIn(jargon, notice.lower())

    def test_ambient_advances_when_the_default_branch_moves_and_discloses_the_advance(self):
        self._ambient()
        successor = self._advance_canonical()
        result = self._ambient(commit=successor)
        self.assertEqual(result["activation"]["commit"], successor)
        self.assertEqual(result["activation"]["epoch"], 2)
        self.assertEqual(len(result["notices"]), 1)
        notice = result["notices"][0]
        self.assertIn(successor[:12], notice)
        self.assertIn(self.repo.commit[:12], notice)      # says what it moved FROM as well as to
        for jargon in ("tree", "epoch"):
            self.assertNotIn(jargon, notice.lower())

    def test_ambient_is_silent_when_nothing_changed(self):
        self._ambient()
        self.assertEqual(self._ambient()["notices"], [])

    def test_advance_refuses_a_commit_that_does_not_descend_from_the_activated_one(self):
        """Forward-only. A force-push, a rollback, or a swapped branch cannot walk qualification backwards
        onto code the current activation never descended from."""
        self._ambient()
        divergent = self.repo.git("commit-tree", self.repo.tree, "-m", "unrelated root")
        self.repo.git("reset", "--hard", divergent)   # a force-push landing on an unrelated history
        result = self._ambient(commit=divergent)
        self.assertEqual(result["activation"]["commit"], self.repo.commit)   # unchanged
        self.assertEqual(result["activation"]["epoch"], 1)
        self.assertTrue(any("does not descend" in notice or "no longer descends" in notice
                            for notice in result["notices"]), result["notices"])

    def test_a_failed_advance_never_costs_the_working_activation(self):
        self._ambient()
        self._advance_canonical()
        (self.repo.fake_bin / "gh").write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
        result = self._ambient()
        self.assertEqual(result["activation"]["commit"], self.repo.commit)
        self.assertEqual(result["activation"]["epoch"], 1)
        self.assertTrue(any("kept working" in notice for notice in result["notices"]), result["notices"])
        # A failed advance is not an alarm: it has to say plainly that nothing is blocked by it.
        self.assertTrue(any("Nothing is blocked" in notice for notice in result["notices"]),
                        result["notices"])

    def test_an_unprovable_commit_leaves_no_activation_and_reports_why(self):
        result = self._ambient(accepted_proof=False)
        self.assertIsNone(result["activation"])
        self.assertFalse((self.repo.common_dir() / "engine/accepted-hooks/activation.json").exists())
        # The degraded line leads with what still WORKS and where the conversation goes meanwhile, because
        # the reassuring half used to be printed to a stream the operator never sees while the raw internal
        # error was what reached them. The detail is kept, but last and labelled.
        self.assertTrue(any("not able to write to memory" in notice for notice in result["notices"]),
                        result["notices"])
        degraded = next(n for n in result["notices"] if "not able to write to memory" in n)
        self.assertIn("Reading and recall work", degraded)
        self.assertIn("transcript", degraded)
        # The detail is kept, but framed as a diagnostic. The strings it can carry say things like
        # "topology authority is unsafe", which a non-engineer reads as a security alarm — so the label has
        # to say, in the sentence itself, that this is for a bug report and not something to act on.
        self.assertIn("for a bug report rather than for you to act on", degraded)
        self.assertLess(degraded.index("Reading and recall work"),
                        degraded.index("for a bug report"),
                        "the reassurance must come before the internal detail, not after it")

    def test_an_activation_belonging_to_another_repository_is_refused(self):
        self._ambient()
        path = self.repo.common_dir() / "engine/accepted-hooks/activation.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["repository"] = "someone-else/other"
        path.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
        result = self._ambient()
        self.assertIsNone(result["activation"])
        self.assertTrue(any("different repository" in notice for notice in result["notices"]),
                        result["notices"])

    def test_a_hanging_github_read_is_abandoned_inside_the_boot_budget(self):
        """The seam that matters at session start is not a slow answer but NO answer. A hook has no terminal,
        so a hang is indistinguishable from a broken session; ambient activation must give up and degrade."""
        (self.repo.fake_bin / "gh").write_text("#!/bin/sh\nsleep 120\n", encoding="utf-8")
        started = time.monotonic()
        result = self._ambient()
        elapsed = time.monotonic() - started
        self.assertIsNone(result["activation"])
        self.assertLess(elapsed, 30, "ambient activation did not abandon a hanging GitHub read")
        self.assertTrue(any("not able to write to memory" in notice for notice in result["notices"]),
                        "a hang must be disclosed, not silently degraded")

    def test_an_authentication_prompt_cannot_block_the_session(self):
        """`gh` reading from stdin must see EOF, not a session that waits forever for an answer nobody can
        type. Without the DEVNULL stdin this script blocks until the timeout."""
        (self.repo.fake_bin / "gh").write_text(
            "#!/usr/bin/env python3\nimport sys\nsys.stdin.read()\nraise SystemExit(1)\n", encoding="utf-8")
        started = time.monotonic()
        result = self._ambient()
        self.assertIsNone(result["activation"])
        self.assertLess(time.monotonic() - started, 10, "an interactive prompt stalled activation")

    def test_concurrent_activations_produce_exactly_one_winner_per_epoch(self):
        self._ambient()
        successor = self._advance_canonical()
        outcomes = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            for result in pool.map(lambda _n: self.repo.activate(commit=successor, expected_epoch=1),
                                   range(4)):
                outcomes.append(result.returncode)
        self.assertEqual(sorted(outcomes), [0, 1, 1, 1])
        record = json.loads(
            (self.repo.common_dir() / "engine/accepted-hooks/activation.json").read_text(encoding="utf-8"))
        self.assertEqual((record["commit"], record["epoch"]), (successor, 2))

    def test_an_unqualified_precompact_rewrites_nothing_and_says_so_once(self):
        """The negative witness for the availability-first shape: with NO activation at all — the state every
        clone is in before it converges — the PreCompact hook must leave canonical memory byte-identical and
        report the skip in one line, not mutate and not go quiet."""
        before = self.repo.canonical_inventory()
        result = self.repo.run_launcher_script(
            "claude", ".engine/tools/memory/compact.py", self.repo.poison_env(), "pre-compact")
        # The dispatcher reports the skip with exit 1; the hook runner above it fail-opens, which is what
        # keeps PreCompact from ever blocking the squash. What matters here is that NOTHING was written.
        self.assertEqual(result.returncode, 1)
        self.assertIn("did not block the host action", result.stderr)
        self.assertEqual(self.repo.canonical_inventory(), before)
        self.assertFalse(self.repo.marker.exists())
        health = self.repo.qualification_health()
        self.assertEqual(health["status"], "degraded")
        self.assertEqual(health["skipped_effect_count"], 1)
        self.assertIn("converges", health["guidance"])

    def test_a_reworded_guidance_sentence_neither_wedges_the_record_nor_reaches_the_operator(self):
        """Guidance is derived, so a record written by an older wording must still read, and the stale
        sentence must not survive into what the operator is shown.

        This is a real incident, not a hypothetical: an earlier commit on this very branch reworded one
        `GUIDANCE_BY_REASON` entry, and every record written before it became permanently unreadable --
        `_read_path` refused, so `record()` could no longer update, so the one channel that reports
        skipped memory work went silent and stayed silent. Exact-match validation made a cosmetic edit a
        latent outage on every machine holding a record, and the LEGACY_GUIDANCE escape hatch only works
        for whoever remembers to use it. Nobody did, in-house, within days."""
        from memory import qualification_health as qh  # noqa: PLC0415 — tools/ is on sys.path above

        self.repo.run_launcher_script(
            "claude", ".engine/tools/memory/compact.py", self.repo.poison_env(), "pre-compact")
        path = self.repo.common_dir() / "engine/accepted-hooks/qualification-health.json"
        stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(stored["skipped_effect_count"], 1)
        obsolete = "Some earlier release phrased this differently, and said to go and repair a worktree."
        stored["guidance"] = obsolete
        path.write_text(json.dumps(stored), encoding="utf-8")

        value = qh.read(str(self.repo.root))
        self.assertEqual(value["guidance"], qh.GUIDANCE_BY_REASON[value["last_failure"]["reason_code"]])
        self.assertNotEqual(value["guidance"], obsolete)

        # The write path is the half that actually wedged: prove the next skip still lands.
        self.repo.run_launcher_script(
            "claude", ".engine/tools/memory/compact.py", self.repo.poison_env(), "pre-compact")
        self.assertEqual(self.repo.qualification_health()["skipped_effect_count"], 2)

    def test_activation_succeeds_with_a_pre_fix_worktree_present_and_discloses_it(self):
        (self.repo.worktree / ".engine/tools/hook-runner.sh").write_text("#!/bin/sh\nexit 0\n",
                                                                         encoding="utf-8")
        result = self._ambient()
        self.assertEqual(result["activation"]["epoch"], 1)
        self.assertEqual(result["coverage"]["uncovered"], 1)
        self.assertEqual(result["coverage"]["total"], 2)


class TestSitePathDerivation(unittest.TestCase):
    """W1: under -I -S the accepted interpreter must find the project venv's packages, not the base
    interpreter's, or boot's grounding envelope cannot import jsonschema and crashes every session."""

    def _module(self):
        import accepted_hook_dispatch
        return accepted_hook_dispatch

    def _make_venv(self, root, *, cfg=True, posix=True, version=(3, 12), make_site=True):
        root = Path(root)
        bindir = root / ("bin" if posix else "Scripts")
        bindir.mkdir(parents=True, exist_ok=True)
        exe = bindir / ("python" if posix else "python.exe")
        exe.write_text("", encoding="utf-8")
        if cfg:
            (root / "pyvenv.cfg").write_text("home = /usr\n", encoding="utf-8")
        site = None
        if make_site:
            if posix:
                site = root / "lib" / f"python{version[0]}.{version[1]}" / "site-packages"
            else:
                site = root / "Lib" / "site-packages"
            site.mkdir(parents=True, exist_ok=True)
            site = str(site.resolve())
        return str(exe), site

    def test_posix_layout_is_derived_from_the_interpreter_alone(self):
        d = self._module()
        with tempfile.TemporaryDirectory() as td:
            exe, site = self._make_venv(Path(td) / "venv", posix=True, version=(3, 11))
            self.assertEqual(d._venv_site_packages(exe, version_info=(3, 11), os_name="posix"), site)

    def test_windows_layout_uses_Lib_site_packages(self):
        d = self._module()
        with tempfile.TemporaryDirectory() as td:
            exe, site = self._make_venv(Path(td) / "venv", posix=False)
            self.assertEqual(d._venv_site_packages(exe, version_info=(3, 12), os_name="nt"), site)

    def test_no_pyvenv_cfg_is_not_a_venv_and_returns_none(self):
        d = self._module()
        with tempfile.TemporaryDirectory() as td:
            exe, _ = self._make_venv(Path(td) / "venv", cfg=False, posix=True)
            self.assertIsNone(d._venv_site_packages(exe, version_info=(3, 12), os_name="posix"))

    def test_missing_site_packages_dir_returns_none_to_force_fallback(self):
        d = self._module()
        with tempfile.TemporaryDirectory() as td:
            exe, _ = self._make_venv(Path(td) / "venv", make_site=False, posix=True)
            self.assertIsNone(d._venv_site_packages(exe, version_info=(3, 12), os_name="posix"))

    def test_site_paths_returns_the_venv_alone_and_drops_the_fallback_scan(self):
        d = self._module()
        with mock.patch.object(d, "_venv_site_packages", return_value="/x/site-packages"):
            self.assertEqual(d._site_paths(), ["/x/site-packages"])

    def test_site_paths_falls_back_to_the_scan_only_when_not_in_a_venv(self):
        d = self._module()
        with mock.patch.object(d, "_venv_site_packages", return_value=None):
            result = d._site_paths()
            self.assertIsInstance(result, list)
            self.assertEqual(result, sorted(set(result)))

    def test_live_venv_makes_jsonschema_importable_under_isolated_flags(self):
        venv_python = _ACCEPTED_TOOLS.parent / ".venv" / "bin" / "python"
        if not venv_python.exists():
            self.skipTest(".engine/.venv absent on this machine")
        program = (
            "import sys; sys.path.insert(0, %r);"
            "import accepted_hook_dispatch as d;"
            "sp = d._site_paths(); sys.path[:0] = sp;"
            "import jsonschema;"
            "print('OK', len(sp) == 1 and any('site-packages' in p for p in sp))" % str(_ACCEPTED_TOOLS)
        )
        proc = subprocess.run([str(venv_python), "-I", "-S", "-c", program],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("OK True", proc.stdout)


class TestAcceptedTreeBytecodeHygiene(unittest.TestCase):
    """W2: no lane descended from a dispatch may write bytecode into the self-attesting accepted tree, or
    its content digest no longer matches and every later dispatch rebuilds it, refusing concurrent starts."""

    def setUp(self):
        self.repo = _AcceptedDispatchRepo()
        self.repo.activate()

    def tearDown(self):
        self.repo.cleanup()

    def _tree_path(self):
        cache_root = self.repo.common_dir() / "engine" / "accepted-hooks" / "trees"
        return cache_root / f"{self.repo.commit}-{self.repo.tree}"

    def _marker_path(self):
        cache_root = self.repo.common_dir() / "engine" / "accepted-hooks" / "trees"
        return cache_root / f"{self.repo.commit}-{self.repo.tree}.json"

    def _pycache_dirs(self, tree):
        found = []
        for cur, dirs, _files in os.walk(tree):
            for name in dirs:
                if name == "__pycache__":
                    found.append(os.path.relpath(os.path.join(cur, name), tree))
        return sorted(found)

    def _valid(self):
        import accepted_hook_dispatch as d
        return d._valid_materialization(
            str(self.repo.home), {"commit": self.repo.commit, "tree": self.repo.tree})

    def test_automatic_lane_leaves_no_bytecode_and_the_tree_stays_valid(self):
        self.assertEqual(self.repo.run_direct().returncode, 0)
        self.assertEqual(self._pycache_dirs(self._tree_path()), [],
                         "the automatic dispatch wrote __pycache__ into the trusted tree")
        self.assertIsNotNone(self._valid(), "the tree is no longer a valid materialization")

    def test_attended_lane_leaves_no_bytecode_and_the_tree_stays_valid(self):
        self.assertEqual(self.repo.run_attended().returncode, 0)
        self.assertEqual(self._pycache_dirs(self._tree_path()), [])
        self.assertIsNotNone(self._valid())

    def test_a_polluted_tree_is_rebuilt_once_then_stays_stable(self):
        self.assertEqual(self.repo.run_direct().returncode, 0)
        marker = self._marker_path()
        seeded = self._tree_path() / ".engine/tools/__pycache__"
        seeded.mkdir(parents=True, exist_ok=True)
        (seeded / "poison.cpython-000.pyc").write_bytes(b"\x00\x01poison")
        self.assertIsNone(self._valid(), "a tree polluted with __pycache__ must be judged invalid")
        # The next dispatch rebuilds it clean once.
        self.assertEqual(self.repo.run_direct().returncode, 0)
        self.assertEqual(self._pycache_dirs(self._tree_path()), [])
        self.assertIsNotNone(self._valid())
        rebuilt = os.stat(marker)
        # A following dispatch finds a valid tree and does not rebuild: the marker file is untouched
        # (an atomic rewrite would replace the inode).
        self.assertEqual(self.repo.run_direct().returncode, 0)
        after = os.stat(marker)
        self.assertEqual((after.st_ino, after.st_mtime_ns), (rebuilt.st_ino, rebuilt.st_mtime_ns),
                         "the tree was rebuilt again, so pollution recurred")

    def test_two_concurrent_attended_launches_against_a_clean_tree_both_succeed(self):
        self.assertEqual(self.repo.run_attended().returncode, 0)
        self.assertEqual(self._pycache_dirs(self._tree_path()), [])
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.repo.run_attended) for _ in range(2)]
            for future in concurrent.futures.as_completed(futures):
                results.append(future.result())
        for result in results:
            self.assertEqual(result.returncode, 0, result.stderr)


class TestBytecodeBeltScope(unittest.TestCase):
    """W2 repair: the bytecode belt is scoped to the INNER accepted-dispatch process. Importing this module
    as a library — as boot.py does at session start to reach ensure_activation_ambient — must NOT flip the
    process-global sys.dont_write_bytecode, or boot's own later imports lose caching on every session start.
    The inner-run commands still arm the belt so an OLD outer (no -B) launching this NEW inner writes no
    bytecode into the digest-attested accepted tree."""

    def _flag_after(self, body):
        program = ("import sys; sys.path.insert(0, %r)\n" % str(_ACCEPTED_TOOLS)) + body + \
                  "\nprint('FLAG', sys.dont_write_bytecode)"
        env = {key: value for key, value in os.environ.items() if key != "PYTHONDONTWRITEBYTECODE"}
        proc = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True,
                              env=env, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip().rsplit("FLAG", 1)[1].strip()

    def test_importing_as_a_library_leaves_bytecode_writing_enabled(self):
        self.assertEqual(self._flag_after("import accepted_hook_dispatch"), "False")

    def test_the_inner_run_command_arms_the_belt_even_without_dash_b(self):
        body = ("import accepted_hook_dispatch as d\n"
                "d.main(['_run-accepted', '--tree', '/nonexistent-tree', '--script', 'foo.py',"
                " '--site-path', '/tmp', '--'])")
        self.assertEqual(self._flag_after(body), "True")

    def test_the_belt_is_not_a_module_top_global(self):
        source = (_ACCEPTED_TOOLS / "accepted_hook_dispatch.py").read_text(encoding="utf-8")
        self.assertIn('if args.command in ("_run-accepted", "_run-attended", "_run-candidate"):', source)
        self.assertNotIn("from urllib.parse import quote\n\nsys.dont_write_bytecode = True", source)


# The unittest runner MUST stay the last statement in this file. Under the engine's canonical
# `unittest discover` run a mid-file runner is harmless — discovery imports the module and collects every
# TestCase regardless of position — but a developer running this file DIRECTLY (`python tools/test_hooks.py`)
# executes `unittest.main()` at that line, before the classes beneath it are defined, and gets a green that
# silently skipped them. Runner-last keeps the direct run honest. `test_launch_contract.py` fails any test
# module that defines a TestCase after its runner. (An earlier comment here claimed #1153's activation suite
# "ran never" because it sat below the runner; that was wrong — it was collected and ran under discovery, and
# #1153 shipped broken because its tests never exercised the fresh-clone path.)


class TestInventoryDriftCheckers(unittest.TestCase):
    """The two-direction drift checkers keep EVENT_INVENTORY true mechanically (the #784 / #816 close).
    Positive: both live registration files pass both legs. Negative: committed fixtures prove each leg
    still bites — and that a FOREIGN (operator's own) hook is never a finding, because the registration
    files are operator-shared."""

    _LIVE = (("claude", ".claude/settings.json"), ("codex", ".codex/hooks.json"))

    @classmethod
    def _live(cls):
        return {provider: validate.load_json(os.path.join(validate.ROOT, rel)) for provider, rel in cls._LIVE}

    @staticmethod
    def _doc(event, *commands, matcher=""):
        return {"hooks": {event: [{"matcher": matcher,
                                   "hooks": [{"type": "command", "command": c} for c in commands]}]}}

    def test_marker_is_the_wiring_libraries_engine_identity(self):
        import wiring
        self.assertEqual(hooks.ENGINE_COMMAND_MARKER, wiring.ENGINE_DIR_MARKER)

    def test_engine_script_extraction_returns_the_script_not_the_launcher(self):
        for provider in ("claude", "codex"):
            cmd = hooks.hook_command(".engine/tools/telemetry.py run-ambient", provider=provider)
            self.assertEqual(hooks._engine_script(cmd), ".engine/tools/telemetry.py")
        self.assertIsNone(hooks._engine_script("echo hi"))
        self.assertIsNone(hooks._engine_script(None))

    def test_both_live_registrations_pass_the_forward_leg(self):
        for provider, document in self._live().items():
            with self.subTest(provider=provider):
                self.assertEqual(hooks.inventory_forward_failures(document, provider), [])

    def test_the_union_of_live_registrations_passes_the_reverse_leg(self):
        installed = hooks.installed_modules()
        self.assertIn("core", installed)
        self.assertEqual(hooks.inventory_reverse_failures(self._live(), installed), [])

    def test_reverse_leg_stays_green_with_both_optional_modules_absent(self):
        # The deployment-variance case: board-sync and product-design declined, so their commands are gone
        # from both files and their owners are skipped — no false red for a supported choice.
        docs = {}
        for provider, document in self._live().items():
            stripped = json.loads(json.dumps(document))
            for groups in stripped["hooks"].values():
                for group in groups:
                    group["hooks"] = [h for h in group["hooks"]
                                      if "projects_sync/" not in h["command"] and "product_design/" not in h["command"]]
            docs[provider] = stripped
        installed = hooks.installed_modules() - {"github-projects-sync", "product-design"}
        self.assertEqual(hooks.inventory_reverse_failures(docs, installed), [])
        for provider, document in docs.items():
            self.assertEqual(hooks.inventory_forward_failures(document, provider), [])

    def test_forward_leg_reds_an_engine_hook_on_an_uninventoried_event(self):
        # The planted drift: an engine hook bound on SessionEnd (or any ungoverned event) with no row.
        for provider in ("claude", "codex"):
            doc = self._doc("SessionEnd", hooks.hook_command(".engine/tools/close.py", provider=provider))
            failures = hooks.inventory_forward_failures(doc, provider)
            self.assertEqual(len(failures), 1, failures)
            self.assertIn("does not govern", failures[0])

    def test_forward_leg_reds_an_engine_command_mapping_to_no_owner(self):
        # A REAL engine script (moment.py exists) that OWNER_BY_SCRIPT does not map.
        doc = self._doc("SessionStart", hooks.hook_command(".engine/tools/moment.py", provider="claude"),
                        matcher="startup")
        failures = hooks.inventory_forward_failures(doc, "claude")
        self.assertEqual(len(failures), 1, failures)
        self.assertIn("no owning system", failures[0])

    def test_a_foreign_command_merely_mentioning_an_engine_path_is_not_judged(self):
        # The operator's own script that happens to name a non-existent path under .engine/tools/ is
        # not the engine's — off the launcher, only a command running an EXISTING engine script is judged.
        doc = self._doc("SessionEnd", "sh /Users/me/mytidy.sh --log .engine/tools/mylog.txt")
        doc["hooks"]["Stop"] = [{"hooks": [{"type": "command",
                                            "command": hooks.hook_command(".engine/tools/close.py", provider="claude")}]}]
        self.assertEqual(hooks.inventory_forward_failures(doc, "claude"), [])
        self.assertIsNone(hooks._engine_script("sh /Users/me/mytidy.sh --log .engine/tools/mylog.txt"))

    def test_forward_leg_reds_a_launcher_entry_pointing_at_a_missing_script(self):
        # The dangling registration: an engine launcher entry left behind by a deleted or renamed script.
        # It is still the engine's (identity rides the launcher), and the missing file is its own finding
        # — a file-must-exist identity test would make the leg look away from exactly this case.
        for provider in ("claude", "codex"):
            cmd = hooks.hook_command(".engine/tools/gone_since_a_rename.py", provider=provider)
            self.assertEqual(hooks._engine_script(cmd), ".engine/tools/gone_since_a_rename.py")
            doc = self._doc("Stop", cmd)
            failures = hooks.inventory_forward_failures(doc, provider)
            self.assertEqual(len(failures), 1, failures)
            self.assertIn("no such file", failures[0])
        # And it satisfies nothing on the reverse leg: close's Stop hook replaced by the dangling entry
        # leaves the Stop row a claim with nothing behind it.
        docs = {}
        for provider, document in self._live().items():
            swapped = json.loads(json.dumps(document))
            for group in swapped["hooks"]["Stop"]:
                for h in group["hooks"]:
                    h["command"] = h["command"].replace("close.py", "gone_since_a_rename.py")
            docs[provider] = swapped
        failures = hooks.inventory_reverse_failures(docs, hooks.installed_modules())
        self.assertEqual(len(failures), 1, failures)
        self.assertIn("governs Stop", failures[0])

    def test_root_points_both_legs_at_another_checkout(self):
        # `root` is a real seam, not decoration: a non-launcher command is the engine's only where its
        # script exists, and the reverse leg reads that checkout's installed modules by default.
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, ".engine", "tools"))
            os.makedirs(os.path.join(tmp, ".engine", "modules", "core"))
            Path(tmp, ".engine", "tools", "only_here.py").write_text("", encoding="utf-8")
            Path(tmp, ".engine", "modules", "core", "manifest.json").write_text("{}", encoding="utf-8")
            cmd = "python3 .engine/tools/only_here.py"
            self.assertEqual(hooks._engine_script(cmd, root=tmp), ".engine/tools/only_here.py")
            self.assertIsNone(hooks._engine_script(cmd))
            doc = self._doc("SessionStart", cmd, matcher="startup")
            failures = hooks.inventory_forward_failures(doc, "claude", root=tmp)
            self.assertEqual(len(failures), 1, failures)
            self.assertIn("no owning system", failures[0])
            self.assertIn("nothing to judge", hooks.inventory_forward_failures(doc, "claude")[0])
            self.assertEqual(hooks.installed_modules(root=tmp), {"core"})
            # The live registrations judged against the temporary root: every launcher entry is still the
            # engine's, and every script is missing there — the reverse leg has nothing behind any row.
            failures = hooks.inventory_reverse_failures(self._live(), root=tmp)
            self.assertTrue(all("nothing to judge" in f for f in failures), failures)

    def test_forward_leg_reds_an_owner_its_event_does_not_name(self):
        # The #784 shape exactly: a known engine script bound on an event whose row omits its owner.
        doc = self._doc("PreCompact", hooks.hook_command(".engine/tools/telemetry.py run-ambient", provider="claude"))
        failures = hooks.inventory_forward_failures(doc, "claude")
        self.assertEqual(len(failures), 1, failures)
        self.assertIn("under-reports", failures[0])

    def test_a_foreign_hook_is_never_a_finding(self):
        # The operator's own hook on an event the engine does not govern, beside one engine hook so the
        # extraction is non-empty: the foreign entry passes unjudged.
        doc = self._doc("SessionEnd", "echo goodbye", "/usr/local/bin/my-own-tidy --quiet")
        doc["hooks"]["Stop"] = [{"hooks": [{"type": "command",
                                            "command": hooks.hook_command(".engine/tools/close.py", provider="claude")}]}]
        self.assertEqual(hooks.inventory_forward_failures(doc, "claude"), [])

    def test_both_legs_fail_loud_on_an_empty_extraction(self):
        empty = {"hooks": {"SessionEnd": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}}
        self.assertTrue(hooks.inventory_forward_failures(empty, "claude"))
        self.assertTrue(hooks.inventory_forward_failures({}, "codex"))
        self.assertTrue(hooks.inventory_reverse_failures({"claude": empty, "codex": {}}, {"core"}))
        with self.assertRaises(ValueError):
            hooks.inventory_forward_failures(empty, "gemini")

    def test_reverse_leg_reds_an_inventoried_owner_nothing_satisfies(self):
        # The #816 shape: a named owner with nothing behind it (telemetry stripped from SessionStart).
        docs = {}
        for provider, document in self._live().items():
            stripped = json.loads(json.dumps(document))
            for group in stripped["hooks"]["SessionStart"]:
                group["hooks"] = [h for h in group["hooks"] if "telemetry.py" not in h["command"]]
            docs[provider] = stripped
        failures = hooks.inventory_reverse_failures(docs, hooks.installed_modules())
        self.assertEqual(len(failures), 1, failures)
        self.assertIn("names telemetry on SessionStart", failures[0])
        self.assertIn("over-reports", failures[0])

    def test_reverse_leg_reds_an_inventoried_event_with_no_binding(self):
        docs = {}
        for provider, document in self._live().items():
            stripped = json.loads(json.dumps(document))
            stripped["hooks"].pop("PreCompact", None)
            docs[provider] = stripped
        failures = hooks.inventory_reverse_failures(docs, hooks.installed_modules())
        self.assertEqual(len(failures), 1, failures)
        self.assertIn("governs PreCompact", failures[0])

    def test_reverse_leg_requires_a_delegated_owners_delegate_to_be_bound(self):
        docs = {}
        for provider, document in self._live().items():
            stripped = json.loads(json.dumps(document))
            for group in stripped["hooks"]["PostToolUse"]:
                group["hooks"] = [h for h in group["hooks"] if "validate.py" not in h["command"]]
            docs[provider] = stripped
        failures = hooks.inventory_reverse_failures(docs, hooks.installed_modules())
        self.assertTrue(any("telemetry rides validation" in f for f in failures), failures)
        self.assertTrue(any("names validation on PostToolUse" in f for f in failures), failures)

    def test_provider_only_bindings_satisfy_their_owners_only_across_the_union(self):
        # Provider-only owners are an established, ledgered shape: build-coordinator's compact-matcher
        # re-grounding and session-economy's spend gate are Claude-only; modes' native-plan importer on
        # UserPromptSubmit is Codex-only. Read alone, EACH runtime's file reds the other's owners — which
        # is exactly why the reverse leg reads the union (green above), never one file.
        live = self._live()
        installed = hooks.installed_modules()
        codex_only = hooks.inventory_reverse_failures({"codex": live["codex"]}, installed)
        self.assertTrue(any("build-coordinator on SessionStart" in f for f in codex_only), codex_only)
        self.assertTrue(any("session-economy on PreToolUse" in f for f in codex_only), codex_only)
        claude_only = hooks.inventory_reverse_failures({"claude": live["claude"]}, installed)
        self.assertEqual([f for f in claude_only if "over-reports" in f],
                         ["the inventory names modes on UserPromptSubmit, but no engine command mapped to modes "
                          "is bound there in any runtime — the row over-reports"])

if __name__ == "__main__":
    unittest.main()
