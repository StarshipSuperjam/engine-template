#!/usr/bin/env python3
"""Whole-engine removal: the ordering, the disclosure, and surviving its own deletion."""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import module_manager  # noqa: E402
import transaction  # noqa: E402
import transaction_adapters_remove as adapter_module  # noqa: E402
import transaction_envelope as te  # noqa: E402


class Args:
    def __init__(self, *flags):
        self.operation = "engine-remove"
        self.rest = list(flags)
        self.json = False
        self.consent_handle = ""


class TestTheStartStaysTheOperators(unittest.TestCase):
    def test_run_refuses_for_engine_remove_however_good_the_handle(self):
        """The operator's ruling: an engine deletion is a harder recovery than an upgrade."""
        self.assertIn("engine-remove", transaction._OPERATOR_TYPED_ONLY)
        adapter = adapter_module.RemoveEngine()
        with mock.patch.object(adapter, "apply") as never:
            with self.assertRaises(transaction.TransactionRefused) as caught:
                transaction.do_run(adapter, Args("--keep-protection"), "sha256:" + "0" * 64)
        self.assertEqual(caught.exception.code, "operator-typed-only")
        never.assert_not_called()


class TestTheProtectionChoiceIsTheOperators(unittest.TestCase):
    def test_planning_without_a_choice_refuses_and_names_both_options(self):
        with self.assertRaises(transaction.TransactionRefused) as caught:
            adapter_module.RemoveEngine().plan(Args(), {"fingerprints": {}})
        self.assertEqual(caught.exception.code, "protection-choice-required")
        joined = " ".join(caught.exception.next_actions)
        self.assertIn("--keep-protection", joined)
        self.assertIn("--remove-protection", joined)

    def test_the_choice_is_recorded_in_the_domain_s_own_vocabulary(self):
        keep = adapter_module.RemoveEngine().plan(Args("--keep-protection"), {"fingerprints": {}})
        drop = adapter_module.RemoveEngine().plan(Args("--remove-protection"), {"fingerprints": {}})
        self.assertEqual(keep["inputs"]["protection"], "keep")
        self.assertEqual(drop["inputs"]["protection"], "drop")
        self.assertEqual(keep["choices"][0]["options"], ["keep", "drop"])

    def test_each_choice_states_its_own_consequence(self):
        drop = adapter_module.RemoveEngine().plan(Args("--remove-protection"), {"fingerprints": {}})
        self.assertTrue(any("without review" in c for c in drop["consequences"]))
        keep = adapter_module.RemoveEngine().plan(Args("--keep-protection"), {"fingerprints": {}})
        self.assertTrue(any("rule stays" in c for c in keep["consequences"]))


class TestTheOutsideThePullRequestChangeIsDisclosed(unittest.TestCase):
    """The plan-wide claim that the merge is the only trust boundary is FALSE for this operation."""

    def test_the_plan_says_the_protection_change_happens_before_the_merge(self):
        plan = adapter_module.RemoveEngine().plan(Args("--remove-protection"), {"fingerprints": {}})
        disclosure = " ".join(plan["consequences"])
        self.assertIn("cannot ride in a pull request", disclosure)
        self.assertIn("when this runs", disclosure)

    def test_the_protection_change_is_typed_as_an_external_setting(self):
        plan = adapter_module.RemoveEngine().plan(Args("--keep-protection"), {"fingerprints": {}})
        kinds = [effect["kind"] for effect in plan["effects"]]
        self.assertIn("external-settings", kinds)


class TestVerifyNeverInventsGreen(unittest.TestCase):
    def test_a_silent_removal_reports_unverified_rather_than_passed(self):
        receipts = adapter_module.RemoveEngine().verify(Args(), {})
        by_check = {r["check"]: r for r in receipts}
        self.assertEqual(by_check["engine files removed"]["result"], "unavailable")
        self.assertEqual(by_check["branch protection change"]["result"], "unavailable")
        self.assertIn("unverified", by_check["engine files removed"]["detail"])

    def test_a_removal_with_no_pull_request_is_a_failure_not_a_silence(self):
        receipts = adapter_module.RemoveEngine().verify(
            Args(), {"deleted": [".engine/"], "de_bootstrap": {"ok": True}})
        by_check = {r["check"]: r for r in receipts}
        self.assertEqual(by_check["removal proposed for review"]["result"], "failed")

    def test_a_complete_removal_reports_each_leg_passed(self):
        receipts = adapter_module.RemoveEngine().verify(
            Args(), {"deleted": [".engine/"], "de_bootstrap": {"ok": True}, "pr": {"url": "x"}})
        self.assertTrue(all(r["result"] == "passed" for r in receipts))

    def test_verify_and_handoff_still_work_with_the_engine_tree_actually_deleted(self):
        """The behaviour, not the wording.

        This replaces a source-text check that asserted the string `open(` did not appear inside verify —
        which proves how the file is written, never what it does. Here the engine tree is genuinely
        deleted in a throwaway copy and the adapter is then asked for its receipts and its handoff, which
        is the situation whole-engine removal actually creates.
        """
        import shutil
        import subprocess
        import tempfile

        here = os.path.dirname(os.path.abspath(__file__))
        engine_dir = os.path.dirname(here)
        with tempfile.TemporaryDirectory() as tmp:
            copy_root = os.path.join(tmp, "copy")
            shutil.copytree(engine_dir, os.path.join(copy_root, ".engine"),
                            # Do NOT exclude 'memory': .engine/tools/memory is a package the adapter's
                            # import chain needs, and excluding it fails the copy for the wrong reason.
                            ignore=shutil.ignore_patterns(".venv", ".uv", "plans", "__pycache__"))
            copied_engine = os.path.join(copy_root, ".engine")
            script = (
                "import json, os, shutil, sys\n"
                "sys.path.insert(0, {tools!r})\n"
                "import transaction_adapters_remove as adapter_module\n"
                "import transaction_envelope as te\n"
                "shutil.rmtree({engine!r})\n"
                "assert not os.path.exists({engine!r})\n"
                "adapter = adapter_module.RemoveEngine()\n"
                "class A:\n"
                "    rest = ['--keep-protection']\n"
                "applied = {{'deleted': ['.engine/'], 'de_bootstrap': {{'ok': True}},\n"
                "           'pr': {{'url': 'https://example.invalid/pr/1'}}}}\n"
                "receipts = adapter.verify(A(), applied)\n"
                "handoff = adapter.handoff(A(), applied, receipts)\n"
                "env = {{'schema_version': te.SCHEMA_VERSION, 'operation': 'engine-remove',\n"
                "       'requested_phase': 'run',\n"
                "       'completed_phases': ['inspect', 'plan', 'apply', 'verify', 'handoff'],\n"
                "       'outcome': 'ok', 'verification': receipts, 'handoff': handoff}}\n"
                "te.validate(env)\n"
                "print('RECEIPT-OK' if te.render(env) else 'RENDER-FAILED')\n"
            ).format(tools=os.path.join(copied_engine, "tools"), engine=copied_engine)
            result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("RECEIPT-OK", result.stdout)


class TestHandoff(unittest.TestCase):
    def test_a_pull_request_handoff_when_the_removal_was_proposed(self):
        result = adapter_module.RemoveEngine().handoff(
            Args(), {"pr": {"url": "https://example.invalid/pr/9"}}, [])
        self.assertEqual(result["kind"], "pull-request")

    def test_a_missing_pull_request_hands_off_as_a_named_manual_step(self):
        result = adapter_module.RemoveEngine().handoff(Args(), {"pr": None}, [])
        self.assertEqual(result["kind"], "manual-follow-up")
        self.assertIn("open the pull request yourself", result["summary"].lower())


class TestTheEnvelopeIsResidentBeforeTheDelete(unittest.TestCase):
    def test_the_adapter_imports_the_envelope_at_module_scope(self):
        """Not lazily: a lazy import after the delete would find nothing to import."""
        source_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "transaction_adapters_remove.py")
        with open(source_path, encoding="utf-8") as handle:
            source = handle.read()
        header = source.split("class RemoveEngine", 1)[0]
        self.assertIn("import transaction_envelope", header)

    def test_the_schema_is_already_loaded(self):
        self.assertTrue(te.SCHEMA, "the schema must be resident before any deletion runs")


class TestConsentIsBoundAtTheFunctionSeam(unittest.TestCase):
    """A fresh whole-engine removal now applies the operator's consent to the EXACT plan they were shown:
    remove_engine() re-derives the handle at its own function seam and refuses an absent or stale one BEFORE
    it touches anything, so a non-CLI caller reaching a real removal cannot skip it. Each refusal here stops
    before de-bootstrap and deletion — proven by the result never reporting a deletion or a de_bootstrap.

    The handle is minted through the SAME shared `_refuse_stale_consent` core that serves upgrade, not a
    copy — the divergence trap this program exists to close."""

    def _minted_handle(self, *flags):
        """Mint the handle exactly as `transaction.py plan engine-remove <flags>` would, so the round-trip is
        the operator's real flow rather than a hand-built digest."""
        adapter = adapter_module.RemoveEngine()
        facts = adapter.inspect(Args(*flags))
        plan = dict(adapter.plan(Args(*flags), facts))
        plan["bound_fingerprints"] = dict((facts or {}).get("fingerprints") or {})
        return te.consent_handle(plan)

    def test_an_absent_handle_refuses_at_the_seam_before_any_mutation(self):
        result = module_manager.remove_engine(choice="keep")   # non-injected, no handle
        self.assertTrue(result["refused"])
        self.assertIn("consent handle", result["reason"])
        self.assertIn("transaction.py plan engine-remove", result["reason"])
        # Nothing was touched: no de-bootstrap ran and nothing was deleted.
        self.assertIsNone(result["de_bootstrap"])
        self.assertEqual(result["deleted"], [])

    def test_a_stale_handle_refuses_at_the_seam_before_any_mutation(self):
        result = module_manager.remove_engine(choice="keep", consent_handle="sha256:" + "0" * 64)
        self.assertTrue(result["refused"])
        self.assertIn("does not match", result["reason"])
        self.assertIsNone(result["de_bootstrap"])
        self.assertEqual(result["deleted"], [])

    def test_the_choice_is_bound_too_a_handle_for_the_other_choice_is_stale(self):
        # A handle minted for --keep-protection must not authorise a --remove-protection removal: the choice
        # is part of the plan the handle covers.
        keep_handle = self._minted_handle("--keep-protection")
        self.assertIsNone(module_manager._refuse_stale_remove_consent("keep", keep_handle))
        self.assertIsNotNone(module_manager._refuse_stale_remove_consent("drop", keep_handle))

    def test_the_plan_to_confirm_round_trip_accepts_the_carried_handle(self):
        # The handle minted from the plan matches the one re-derived at the seam — the operator's real path.
        for choice, flag in (("keep", "--keep-protection"), ("drop", "--remove-protection")):
            with self.subTest(choice=choice):
                self.assertIsNone(module_manager._refuse_stale_remove_consent(choice, self._minted_handle(flag)))

    # The injected fixture/demo seam's exemption from the consent gate is proven — safely, on a redirected
    # fixture root rather than the real tree — by the whole-engine removal fixture tests in
    # test_module_manager (which call remove_engine injected, with no handle, and still delete cleanly) and by
    # remove_engine_demo. It is deliberately NOT re-proven here: an injected remove_engine on THIS checkout
    # would delete the engine under test. The residual is disclosed in the module docstring and the PR body.


class TestTheRealConfirmPathCrossesConsent(unittest.TestCase):
    """End-to-end evidence on the REAL CLI confirm path (`remove-engine --confirm`), not the injected demo
    that bypasses it: the door carries the operator's --consent-handle to the function seam, and an absent
    one refuses there. Base currency is short-circuited so the test isolates the consent gate rather than
    d1's separate base check."""

    def test_the_confirm_door_without_a_handle_refuses_through_the_real_path(self):
        import contextlib
        import io
        captured = io.StringIO()
        with mock.patch.object(module_manager, "_door_base_currency", return_value=(False, "")), \
             contextlib.redirect_stdout(captured):
            code = module_manager.main(["remove-engine", "--confirm", "--keep-protection"])
        self.assertEqual(code, 1)
        text = captured.getvalue()
        self.assertIn("consent handle", text)
        self.assertIn("transaction.py plan engine-remove", text)

    def test_the_confirm_door_with_a_stale_handle_refuses_through_the_real_path(self):
        import contextlib
        import io
        captured = io.StringIO()
        with mock.patch.object(module_manager, "_door_base_currency", return_value=(False, "")), \
             contextlib.redirect_stdout(captured):
            code = module_manager.main(["remove-engine", "--confirm", "--keep-protection",
                                        "--consent-handle=sha256:" + "0" * 64])
        self.assertEqual(code, 1)
        self.assertIn("does not match", captured.getvalue())


if __name__ == "__main__":
    unittest.main()
