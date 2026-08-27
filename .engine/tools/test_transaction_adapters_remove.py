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

    def test_verify_reads_no_file(self):
        """After apply there is no .engine to read; the receipts come from what apply returned."""
        source_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "transaction_adapters_remove.py")
        with open(source_path, encoding="utf-8") as handle:
            source = handle.read()
        verify_body = source.split("def verify(", 1)[1].split("def handoff(", 1)[0]
        for forbidden in ("open(", "load_json", "os.path.isfile"):
            self.assertNotIn(forbidden, verify_body,
                             "{0} in verify would read a tree that has just been deleted".format(forbidden))


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


if __name__ == "__main__":
    unittest.main()
