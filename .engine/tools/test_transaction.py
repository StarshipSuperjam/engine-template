#!/usr/bin/env python3
"""The protocol core's own properties, proven against a recording fake adapter.

The load-bearing test is `TestNothingMutatesBeforeConsent`: the fake records every apply, and a stale or
absent handle must leave that record empty. Everything else in this protocol rests on that.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import transaction  # noqa: E402
import transaction_envelope as te  # noqa: E402


class RecordingAdapter(transaction.Adapter):
    """A fake that records what it was asked to do, and lets a test move the world underneath."""

    operation = "module-add"

    def __init__(self):
        self.applied = []
        self.verified = []
        self.handed_off = []
        self.world = "unchanged"
        self.domain_answer = "the dependency rules said yes"

    def inspect(self, args):
        return {"summary": "module 'design-review' is available",
                "fingerprints": {"world": self.world}}

    def plan(self, args, facts):
        # A thin adapter: the CONSEQUENCE text comes from the domain answer, so stubbing the domain
        # visibly changes the envelope. That is the deference property, made checkable.
        return {
            "inputs": {"module": "design-review"},
            "consequences": ["Adds the design-review capability. " + self.domain_answer],
            "effects": [{"kind": "capability", "description": "design-review becomes available"}],
            "reversibility": "local-recovery",
        }

    def apply(self, args, plan):
        self.applied.append(plan["consent_handle"])
        return {"committed": "abc1234"}

    def verify(self, args, applied):
        self.verified.append(applied)
        return [{"check": "wiring coherence", "result": "passed"}]

    def handoff(self, args, applied, receipts):
        self.handed_off.append(applied)
        return {"kind": "in-tree-commit", "summary": "Added as one labelled commit.",
                "reference": applied["committed"]}


class Args:
    def __init__(self, **kw):
        self.operation = kw.pop("operation", "module-add")
        self.rest = []
        self.json = False
        self.consent_handle = kw.pop("consent_handle", "")
        for key, value in kw.items():
            setattr(self, key, value)


class ProtocolTestCase(unittest.TestCase):
    def setUp(self):
        self._saved = dict(transaction._REGISTRY)
        self.adapter = RecordingAdapter()
        transaction.register(self.adapter)

    def tearDown(self):
        transaction._REGISTRY.clear()
        transaction._REGISTRY.update(self._saved)


class TestPhases(ProtocolTestCase):
    def test_inspect_changes_nothing_and_produces_no_plan(self):
        result = transaction.do_inspect(self.adapter, Args())
        te.validate(result)
        self.assertEqual(result["outcome"], "ok")
        self.assertNotIn("plan", result)
        self.assertEqual(self.adapter.applied, [])

    def test_plan_mints_a_handle_and_still_changes_nothing(self):
        result = transaction.do_plan(self.adapter, Args())
        te.validate(result)
        self.assertTrue(result["plan"]["consent_handle"].startswith("sha256:"))
        self.assertEqual(self.adapter.applied, [])

    def test_planning_twice_against_an_unchanged_world_yields_the_same_handle(self):
        first = transaction.do_plan(self.adapter, Args())["plan"]["consent_handle"]
        second = transaction.do_plan(self.adapter, Args())["plan"]["consent_handle"]
        self.assertEqual(first, second)

    def test_run_applies_verifies_and_hands_off_in_one_process(self):
        handle = transaction.do_plan(self.adapter, Args())["plan"]["consent_handle"]
        result = transaction.do_run(self.adapter, Args(), handle)
        te.validate(result)
        self.assertEqual(result["completed_phases"], ["inspect", "plan", "apply", "verify", "handoff"])
        self.assertEqual(self.adapter.applied, [handle])
        self.assertEqual(len(self.adapter.verified), 1)
        self.assertEqual(len(self.adapter.handed_off), 1)


class TestNothingMutatesBeforeConsent(ProtocolTestCase):
    """The property everything else rests on."""

    def test_an_absent_handle_refuses_and_applies_nothing(self):
        with self.assertRaises(transaction.TransactionRefused) as caught:
            transaction.do_run(self.adapter, Args(), "")
        self.assertEqual(caught.exception.code, "consent-handle-missing")
        self.assertEqual(self.adapter.applied, [])
        self.assertEqual(self.adapter.verified, [])
        self.assertEqual(self.adapter.handed_off, [])

    def test_a_stale_handle_refuses_and_applies_nothing(self):
        handle = transaction.do_plan(self.adapter, Args())["plan"]["consent_handle"]
        self.adapter.world = "moved"          # the world changes after the operator saw the plan
        with self.assertRaises(transaction.StalePlan) as caught:
            transaction.do_run(self.adapter, Args(), handle)
        self.assertEqual(self.adapter.applied, [])
        stale = caught.exception.envelope
        te.validate(stale)
        self.assertEqual(stale["outcome"], "refused")
        self.assertEqual(stale["refusal"]["code"], "consent-handle-stale")

    def test_a_stale_refusal_hands_back_the_fresh_plan_not_merely_a_complaint(self):
        handle = transaction.do_plan(self.adapter, Args())["plan"]["consent_handle"]
        self.adapter.world = "moved"
        with self.assertRaises(transaction.StalePlan) as caught:
            transaction.do_run(self.adapter, Args(), handle)
        stale = caught.exception.envelope
        self.assertIn("plan", stale, "the operator must see WHAT moved, not only that it did")
        self.assertNotEqual(stale["plan"]["consent_handle"], handle)
        self.assertTrue(stale["refusal"]["retryable"])

    def test_a_moved_world_invalidates_the_handle_even_when_the_plan_reads_identically(self):
        """The regression this suite caught during the build.

        The handle was first taken over the plan's own fields only. The world-state fingerprints live in
        the facts, so a repository that moved underneath an unchanged-looking plan kept a VALID handle —
        the staleness guarantee was decorative. The plan now binds the fingerprints it was derived
        against, so state is part of what the operator consented to.
        """
        first = transaction.do_plan(self.adapter, Args())["plan"]
        self.adapter.world = "moved"          # only the fingerprint changes; every word stays the same
        second = transaction.do_plan(self.adapter, Args())["plan"]
        self.assertEqual(first["consequences"], second["consequences"],
                         "precondition: the plan's prose is identical")
        self.assertEqual(first["effects"], second["effects"])
        self.assertNotEqual(first["consent_handle"], second["consent_handle"],
                            "a moved world must invalidate consent even when the wording did not change")

    def test_a_forged_handle_refuses(self):
        with self.assertRaises(transaction.StalePlan):
            transaction.do_run(self.adapter, Args(), "sha256:" + "f" * 64)
        self.assertEqual(self.adapter.applied, [])


class TestAdapterDefersToTheDomain(ProtocolTestCase):
    """An adapter that duplicates a domain rule instead of wrapping it would pass its own suite.

    Stubbing the domain must visibly change the envelope — that is what proves deference.
    """

    def test_stubbing_the_domain_answer_changes_the_envelope(self):
        before = transaction.do_plan(self.adapter, Args())["plan"]["consequences"]
        self.adapter.domain_answer = "the dependency rules refused"
        after = transaction.do_plan(self.adapter, Args())["plan"]["consequences"]
        self.assertNotEqual(before, after)

    def test_stubbing_the_domain_answer_changes_the_handle(self):
        before = transaction.do_plan(self.adapter, Args())["plan"]["consent_handle"]
        self.adapter.domain_answer = "the dependency rules refused"
        after = transaction.do_plan(self.adapter, Args())["plan"]["consent_handle"]
        self.assertNotEqual(before, after, "a different domain answer is a different change")


class TestOperatorTypedOnlyOperations(ProtocolTestCase):
    """Whole-engine removal refuses `run` outright: a deletion is a harder recovery than an upgrade."""

    def test_engine_remove_run_refuses_unconditionally_and_names_the_door(self):
        class Removal(RecordingAdapter):
            operation = "engine-remove"

        removal = Removal()
        transaction.register(removal)
        # Even with a perfectly good handle, run refuses.
        handle = transaction.do_plan(removal, Args(operation="engine-remove"))["plan"]["consent_handle"]
        with self.assertRaises(transaction.TransactionRefused) as caught:
            transaction.do_run(removal, Args(operation="engine-remove"), handle)
        self.assertEqual(caught.exception.code, "operator-typed-only")
        self.assertTrue(any("module_manager.py remove-engine" in action
                            for action in caught.exception.next_actions + [caught.exception.explanation]))
        self.assertEqual(removal.applied, [], "a refused run must apply nothing")

    def test_the_refusal_is_not_a_judgement_the_protocol_invents(self):
        self.assertIn("engine-remove", transaction._OPERATOR_TYPED_ONLY)
        self.assertNotIn("module-add", transaction._OPERATOR_TYPED_ONLY)
        self.assertNotIn("engine-upgrade", transaction._OPERATOR_TYPED_ONLY,
                         "upgrade's start protections are the harness-gated skill and the merge, and "
                         "its consent is the digest handle — not a refusal here")

    def test_part_b_adapters_are_on_the_cli_load_list(self):
        # typed-lifecycle Part B: the three external-state operations join the CLI load list so
        # `transaction.py plan <op>` resolves them rather than answering unknown-operation.
        for mod in ("transaction_adapters_controlplane", "transaction_adapters_arrival"):
            self.assertIn(mod, transaction._ADAPTER_MODULES)
        failed = transaction.load_adapters()
        for mod in ("transaction_adapters_controlplane", "transaction_adapters_arrival"):
            self.assertNotIn(mod, failed, f"{mod} must import cleanly in the engine's home repo")
        for op in ("control-plane-bootstrap", "control-plane-finalize", "engine-arrival"):
            self.assertIn(op, transaction._REGISTRY, f"load_adapters must register {op}")

    def test_part_b_external_state_operations_are_not_operator_typed_only(self):
        # bootstrap/finalize/arrival are additive and reversible (protection augments, an arrival is
        # reverted by reverting its pull request), so — unlike engine-remove, whose recovery is harder —
        # they are NOT operator-typed-only: the protocol resolves them for `plan`, and `run` is a
        # consent-verified apply (Part A's upgrade/module-add pattern) rather than a refusal.
        for op in ("control-plane-bootstrap", "control-plane-finalize", "engine-arrival"):
            self.assertNotIn(op, transaction._OPERATOR_TYPED_ONLY)


class TestResume(ProtocolTestCase):
    def test_resume_without_a_progress_marker_replans_and_says_so(self):
        result = transaction.do_resume(self.adapter, Args())
        te.validate(result)
        self.assertIn("plan", result)
        unavailable = [r for r in result["verification"] if r["result"] == "unavailable"]
        self.assertTrue(unavailable, "an adapter with no marker must say prior progress is unreadable")
        self.assertIn("not a continuation", unavailable[0]["detail"])

    def test_resume_applies_nothing_by_itself(self):
        transaction.do_resume(self.adapter, Args())
        self.assertEqual(self.adapter.applied, [])

    def test_an_adapter_with_its_own_resume_is_used(self):
        marker = {"schema_version": te.SCHEMA_VERSION, "operation": "module-add",
                  "requested_phase": "resume", "completed_phases": ["inspect", "plan", "apply"],
                  "outcome": "partial"}
        self.adapter.resume = lambda args: marker
        self.assertEqual(transaction.do_resume(self.adapter, Args())["outcome"], "partial")


class TestUnknownOperation(ProtocolTestCase):
    def test_an_unimplemented_operation_raises_its_own_kind_not_a_refusal(self):
        """Deliberately not a TransactionRefused: a refusal rides inside an envelope, and an envelope
        must name a REAL operation. Reporting a typo under some default operation told the caller their
        mistake was about a transaction they never asked for."""
        with self.assertRaises(transaction.UnknownOperation) as caught:
            transaction._adapter_for("engine-do-whatever")
        self.assertEqual(caught.exception.operation, "engine-do-whatever")

    def test_an_adapter_that_cannot_load_is_reported_rather_than_called_a_typo(self):
        with self.assertRaises(transaction.UnknownOperation) as caught:
            transaction._adapter_for("never-registered-here",
                                     {"transaction_adapters_remove": "ImportError: boom"})
        self.assertIn("transaction_adapters_remove", caught.exception.load_failures)


class TestLoadAdaptersReportsAnImportFailure(unittest.TestCase):
    """The REAL load_adapters() import path under a broken adapter — not the hand-fed load_failures dict
    the sibling test above uses. A synthetic adapter module that raises on import is REPORTED by name with
    its typed error, never swallowed; every other adapter still registers and dispatches; and a caller
    asking for the broken adapter's operation meets a load-failure notice, not a "did you mean" for a typo
    that does not exist. `load_adapters` exists precisely so one broken adapter does not take the CLI down,
    and this proves each half of that promise instead of trusting the comment."""

    _BROKEN = "transaction_adapters_synthetic_boom"

    def setUp(self):
        self._saved_modules = transaction._ADAPTER_MODULES
        self._saved_registry = dict(transaction._REGISTRY)
        self._saved_syspath = list(sys.path)
        self._saved_sysmodules = set(sys.modules)
        self._tmp = tempfile.TemporaryDirectory()
        with open(os.path.join(self._tmp.name, self._BROKEN + ".py"), "w", encoding="utf-8") as handle:
            handle.write("raise RuntimeError('synthetic adapter import failure')\n")
        sys.path.insert(0, self._tmp.name)
        transaction._ADAPTER_MODULES = tuple(self._saved_modules) + (self._BROKEN,)

    def tearDown(self):
        transaction._ADAPTER_MODULES = self._saved_modules
        transaction._REGISTRY.clear()
        transaction._REGISTRY.update(self._saved_registry)
        sys.path[:] = self._saved_syspath
        for name in set(sys.modules) - self._saved_sysmodules:
            sys.modules.pop(name, None)
        self._tmp.cleanup()

    def test_the_broken_adapter_is_named_with_its_typed_error(self):
        failed = transaction.load_adapters()
        self.assertIn(self._BROKEN, failed)
        self.assertIn("RuntimeError", failed[self._BROKEN])
        self.assertIn("synthetic adapter import failure", failed[self._BROKEN])

    def test_the_other_adapters_still_register_and_dispatch(self):
        failed = transaction.load_adapters()
        # The broken one is the ONLY failure; every real operation still registered and reachable.
        self.assertEqual(set(failed), {self._BROKEN})
        for operation in ("engine-upgrade", "engine-upgrade-rollback", "module-add", "module-remove",
                          "engine-remove"):
            self.assertEqual(transaction._adapter_for(operation).operation, operation)

    def test_a_caller_asking_for_the_broken_adapters_operation_meets_the_load_failure_not_a_typo(self):
        import contextlib
        import io
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            code = transaction.main(["inspect", "synthetic-op"])
        self.assertEqual(code, 2)
        text = captured.getvalue()
        self.assertIn(self._BROKEN, text)
        self.assertIn("could not be loaded", text)
        self.assertIn("synthetic adapter import failure", text)


class TestStaysOnTheArrivalFloor(unittest.TestCase):
    def test_core_is_standard_library_only_with_the_future_import(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "transaction.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("from __future__ import annotations", source)
        for third_party in ("jsonschema", "yaml", "requests"):
            self.assertNotIn("import {0}".format(third_party), source)
        self.assertNotIn("import tomllib", source)
class TestTheRealCommandLineWorks(unittest.TestCase):
    """The gap that let a dead CLI ship green.

    Every other test in this file registers its adapter in-process — either by installing a stub or by
    importing the adapter module, which registers as a side effect. The shipped entry point reproduces
    neither: run as a script, `transaction.py` is `__main__`, while each adapter does `import
    transaction` and loads a SECOND copy of it, registering into a registry `__main__` never reads. So
    94 tests passed over a command that answered "no adapter implements ..." for every operation it
    ships. These tests drive the real command as a subprocess, which is the only arrangement that would
    have caught it.
    """

    TOOLS = os.path.dirname(os.path.abspath(__file__))
    ENGINE = os.path.dirname(TOOLS)

    def _run(self, *argv):
        return subprocess.run([sys.executable, os.path.join(self.TOOLS, "transaction.py")] + list(argv),
                              cwd=self.ENGINE, capture_output=True, text=True)

    def test_every_shipped_operation_is_reachable_from_the_command_line(self):
        for operation in ("engine-upgrade", "engine-upgrade-rollback", "module-add", "module-remove",
                          "engine-remove"):
            result = self._run("inspect", operation)
            said = result.stdout + result.stderr
            # Assert on the OUTCOME, not on the absence of one string. Checking only for "No adapter
            # implements" passes over a command that dies with a traceback and prints nothing -- a
            # reviewer hit exactly that under a different interpreter and the test stayed green.
            #
            # "Reachable" is not "exits 0": `inspect module-add` with no module id REFUSES, and a refusal
            # is a real answer. What must never happen is silence, a crash, or the CLI disowning its own
            # operation.
            self.assertTrue(said.strip(), "{0} produced no output at all".format(operation))
            self.assertNotIn("Traceback", said, "{0} crashed".format(operation))
            self.assertNotIn("No adapter implements", said,
                             "{0} is unreachable from the real CLI".format(operation))
            self.assertIn(result.returncode, (0, 2),
                          "{0} exited {1}: {2}".format(operation, result.returncode, said))

    def test_an_operation_needing_no_argument_succeeds_outright(self):
        """The stricter half: engine-upgrade takes no operand, so anything but a clean answer is a defect."""
        result = self._run("inspect", "engine-upgrade")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(result.stdout.strip())

    def test_an_operand_protected_by_the_end_of_options_marker_is_not_lifted_as_a_flag(self):
        """`--` must mean what it means everywhere else. argparse strips the marker before REMAINDER sees
        it, so this is handled before parsing -- and the first attempt was dead code that could never
        fire, which is the same "reads as coverage, is not" defect this round removed elsewhere."""
        result = self._run("plan", "module-add", "--", "--json")
        said = result.stdout + result.stderr
        # A command that died printing nothing would have passed the old assertion, which only checked
        # that stdout did not begin with "{". Establish that it ANSWERED, and answered as prose.
        self.assertTrue(result.stdout.strip(), "the command produced no stdout at all")
        self.assertNotIn("Traceback", said)
        self.assertNotEqual(result.stdout.strip()[:1], "{",
                            "a protected --json was still lifted as this CLI's own flag")
        self.assertIn("module-add", result.stdout)

    def test_the_end_of_options_boundary_never_goes_negative(self):
        """`--` before the operation makes the boundary arithmetic negative, and negative used to read as
        "no protection at all" -- dropping the guard where the operator asked for it most loudly.

        Drives the REAL command. The previous version re-implemented `max(0, ...)` inside the test, so
        reverting the production line left it green -- the third time this build wrote a test that could
        not fail, and the reason its siblings here run as subprocesses.
        """
        # `--json` is the discriminator, because its effect is VISIBLE. A protected `--json` must stay an
        # operand and leave the output as prose; if the boundary collapses to "no protection", it is
        # lifted and the output becomes JSON. A protected `--consent-handle` would not do: both the
        # protected and the unprotected case end in a refusal, so the test could not tell them apart --
        # which is exactly how the first two versions of this test managed to pass either way.
        result = self._run("plan", "--", "engine-upgrade", "--json")
        said = result.stdout + result.stderr
        self.assertTrue(said.strip(), "the command produced no output at all")
        self.assertNotIn("Traceback", said)
        self.assertNotEqual(result.stdout.strip()[:1], "{",
                            "the protected --json was lifted, so the boundary collapsed: " + said)

    def test_an_unknown_operation_still_answers_in_json_when_json_was_asked_for(self):
        """No envelope -- an envelope must name a real operation -- but a caller that asked for
        machine-readable output must not be handed an unparseable stream with no signal."""
        result = self._run("plan", "definitely-not-an-operation", "--json")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["unknown_operation"], "definitely-not-an-operation")
        self.assertIn("module-add", payload["available_operations"])
        self.assertEqual(result.returncode, 2)

    def test_the_documented_flag_position_actually_produces_json(self):
        """`--json` after the operation was swallowed by REMAINDER and silently printed prose."""
        result = self._run("plan", "module-add", "some-module", "--json")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["operation"], "module-add")

    def test_an_unknown_operation_is_not_reported_as_a_real_one(self):
        result = self._run("plan", "module-frobnicate")
        combined = result.stdout + result.stderr
        self.assertIn("module-frobnicate", combined)
        self.assertNotIn("engine-upgrade — plan", combined,
                         "a typo must not be reported as the most sensitive transaction")
        self.assertIn("Available here:", combined)

    def test_the_module_stays_importable_as_a_library_on_the_arrival_floor(self):
        """Adapters load at CLI entry, not at import: arrival reaches this module before the domain."""
        script = ("import sys; sys.path.insert(0, {tools!r});"
                  " import transaction;"
                  " print('imported-clean')").format(tools=self.TOOLS)
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("imported-clean", result.stdout)


class TestTheModuleManagerCommandIsAlsoOneModule(unittest.TestCase):
    """The same dual-module hazard, one level down, caught only because a reviewer went looking for the
    pattern rather than the symptom.

    `module_manager.py` run as a script is `__main__`, while the upgrade adapter it reaches through the
    consent check does `import module_manager` — a second copy. It is harmless only while that module
    holds no shared mutable state, which is a fact about today's code, not a guarantee. These run the real
    command as a subprocess, which is the check that was missing when the identical defect shipped green.
    """

    def _run(self, *args):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return subprocess.run([sys.executable, os.path.join("tools", "module_manager.py")] + list(args),
                              cwd=root, capture_output=True, text=True)

    def test_running_the_script_delegates_to_the_imported_module(self):
        """A REAL discriminator. The previous version of this test printed
        `sys.modules['module_manager'] is module_manager` after an ordinary import, which is True
        whatever the `__main__` block does -- a reviewer ran it against a reverted copy and got True
        there too. Empty coverage for the very fix it names.

        This one imports the module, replaces `main` on THAT object, then executes the file as
        `__main__`. With delegation the sentinel runs, because the script hands off to the imported
        copy. Without it, the script's own `main` runs and the sentinel never fires.
        """
        probe = (
            "import sys, os, runpy;"
            "sys.path.insert(0, os.path.join(os.getcwd(), 'tools'));"
            "import module_manager;"
            "module_manager.main = lambda argv: (print('DELEGATED'), 0)[1];"
            "sys.argv = ['module_manager.py', 'status'];"
            "runpy.run_path(os.path.join('tools', 'module_manager.py'), run_name='__main__')"
        )
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        result = subprocess.run([sys.executable, "-c", probe], cwd=root, capture_output=True, text=True)
        self.assertIn("DELEGATED", result.stdout,
                      "the script ran its own main instead of the imported module's:\n" + result.stderr)

    def test_the_real_command_answers_rather_than_crashing(self):
        result = self._run("upgrade", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.strip(), "the command produced no output at all")

    def test_a_fresh_apply_without_a_handle_refuses_through_the_real_command(self):
        """Drives the gate as a subprocess -- but ONLY in a throwaway copy.

        The previous version ran `module_manager.py upgrade --confirm` unmocked in the repository root.
        Its comment enumerated two refusal branches and missed a third: with a marker present that
        carries a `target_ref`, control falls through to a genuine `upgrade()` against the tree. Whether
        it fired depended on machine state, which is exactly what makes it unacceptable in a discovered
        suite -- a test must not be able to update the operator's engine.
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with tempfile.TemporaryDirectory() as tmp:
            copy = os.path.join(tmp, "engine")
            shutil.copytree(root, copy,
                            ignore=shutil.ignore_patterns(".venv", ".uv", "plans", "__pycache__"))
            result = subprocess.run(
                [sys.executable, os.path.join("tools", "module_manager.py"), "upgrade", "--confirm"],
                cwd=copy, capture_output=True, text=True)
        said = (result.stdout + result.stderr).lower()
        self.assertEqual(result.returncode, 2, said)
        # WHICH refusal fires depends on the copy's state; every branch must name a runnable next step.
        self.assertTrue(any(step in said for step in ("transaction.py plan", "rollback --confirm",
                                                      "transaction.py resume")),
                        "the refusal named no next command: " + said)
        self.assertNotIn("traceback", said)


# Kept LAST on purpose: this block used to sit mid-file, so every test class below it was
# invisible to anyone running the file directly -- 19 of this build's own tests among them. CI
# uses discovery and ran them, which is the same "green over a gap" shape as the defect repaired
# here.
if __name__ == "__main__":
    unittest.main()
