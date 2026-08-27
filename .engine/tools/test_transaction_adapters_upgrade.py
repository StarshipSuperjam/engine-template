#!/usr/bin/env python3
"""The upgrade transaction: what its consent binds, and what it deliberately does not gate."""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import module_manager  # noqa: E402
import transaction  # noqa: E402
import transaction_adapters_upgrade as adapters  # noqa: E402
import transaction_envelope as te  # noqa: E402


class Args:
    def __init__(self, *rest):
        self.operation = "engine-upgrade"
        self.rest = list(rest)
        self.json = False
        self.consent_handle = ""


PREVIEW = {"refused": False, "current": "1.0.0", "available": "1.1.0",
           "capabilities_removed": [], "modules_added": [], "data_migration": False}


class TestTheStartIsNotGatedHere(unittest.TestCase):
    """The operator's ruling: digest-only. An upgrade rolls back, so no new start machinery."""

    def test_engine_upgrade_run_is_not_refused_outright(self):
        self.assertNotIn("engine-upgrade", transaction._OPERATOR_TYPED_ONLY)

    def test_the_plan_names_the_typed_command_as_a_manual_step_rather_than_pretending_to_gate_it(self):
        with mock.patch.object(module_manager, "upgrade_preview", return_value=PREVIEW):
            plan = adapters.UpgradeEngine().plan(Args(), {"fingerprints": {}})
        joined = " ".join(plan["manual_steps"])
        self.assertIn("/engine-upgrade", joined)
        self.assertIn("cannot start an update on its own", joined)


class TestConsentBindsWhatIsApplied(unittest.TestCase):
    def test_a_matching_handle_passes(self):
        with mock.patch.object(module_manager, "upgrade_preview", return_value=PREVIEW), \
                mock.patch.object(module_manager, "_git", return_value="abc123\n"):
            adapter = transaction._REGISTRY["engine-upgrade"]
            facts = adapter.inspect(Args())
            plan = dict(adapter.plan(Args(), facts))
            plan["bound_fingerprints"] = dict(facts["fingerprints"])
            handle = te.consent_handle(plan)
            self.assertIsNone(module_manager._refuse_stale_consent(None, handle))

    def test_a_handle_from_a_different_update_refuses_and_says_what_moved(self):
        with mock.patch.object(module_manager, "upgrade_preview", return_value=PREVIEW):
            message = module_manager._refuse_stale_consent(None, "sha256:" + "9" * 64)
        self.assertIsNotNone(message)
        self.assertIn("Nothing was changed", message)
        self.assertIn("different change", message)

    def test_an_unverifiable_handle_refuses_rather_than_passing_silently(self):
        with mock.patch.object(module_manager, "upgrade_preview", side_effect=RuntimeError("no network")):
            message = module_manager._refuse_stale_consent(None, "sha256:" + "0" * 64)
        self.assertIsNotNone(message)
        self.assertIn("NOT applied", message)

    def test_an_absent_handle_is_not_itself_a_refusal(self):
        """Making it mandatory would be the start gate that was ruled against."""
        self.assertIsNone(module_manager._consent_handle_arg(["upgrade", "--confirm"]))
        self.assertEqual(module_manager._consent_handle_arg(
            ["upgrade", "--confirm", "--consent-handle", "sha256:abc"]), "sha256:abc")
        self.assertEqual(module_manager._consent_handle_arg(
            ["upgrade", "--confirm", "--consent-handle=sha256:xyz"]), "sha256:xyz")

    def test_a_moved_release_moves_the_handle(self):
        with mock.patch.object(module_manager, "upgrade_preview", return_value=PREVIEW), \
                mock.patch.object(module_manager, "_git", return_value="abc123\n"):
            adapter = transaction._REGISTRY["engine-upgrade"]
            facts = adapter.inspect(Args())
            plan = dict(adapter.plan(Args(), facts))
            plan["bound_fingerprints"] = dict(facts["fingerprints"])
            before = te.consent_handle(plan)
        moved = dict(PREVIEW, available="2.0.0")
        with mock.patch.object(module_manager, "upgrade_preview", return_value=moved), \
                mock.patch.object(module_manager, "_git", return_value="abc123\n"):
            facts = adapter.inspect(Args())
            plan = dict(adapter.plan(Args(), facts))
            plan["bound_fingerprints"] = dict(facts["fingerprints"])
            after = te.consent_handle(plan)
        self.assertNotEqual(before, after)


class TestPlanReportsWhatTheDomainSaid(unittest.TestCase):
    def test_a_retired_capability_reaches_the_consequences(self):
        with mock.patch.object(module_manager, "upgrade_preview",
                               return_value=dict(PREVIEW, capabilities_removed=["design-review"])):
            plan = adapters.UpgradeEngine().plan(Args(), {"fingerprints": {}})
        self.assertTrue(any("design-review" in c for c in plan["consequences"]))

    def test_a_data_migration_is_disclosed_with_its_backup_refusal(self):
        with mock.patch.object(module_manager, "upgrade_preview",
                               return_value=dict(PREVIEW, data_migration=True)):
            plan = adapters.UpgradeEngine().plan(Args(), {"fingerprints": {}})
        self.assertTrue(any("backed up first" in c for c in plan["consequences"]))
        self.assertIn("saved-data", [e["kind"] for e in plan["effects"]])

    def test_being_current_refuses_rather_than_planning_a_no_op(self):
        with mock.patch.object(module_manager, "upgrade_preview",
                               return_value=dict(PREVIEW, available=None, target=None)):
            with self.assertRaises(transaction.TransactionRefused) as caught:
                adapters.UpgradeEngine().plan(Args(), {"fingerprints": {}})
        self.assertEqual(caught.exception.code, "already-current")


class TestVerifyNeverInventsGreen(unittest.TestCase):
    def test_a_silent_result_is_unavailable(self):
        receipts = adapters.UpgradeEngine().verify(Args(), {})
        self.assertEqual(receipts[0]["result"], "unavailable")

    def test_a_staged_update_with_no_pull_request_is_a_failure_with_a_way_forward(self):
        receipts = adapters.UpgradeEngine().verify(Args(), {"applied": True, "pr": None})
        by_check = {r["check"]: r for r in receipts}
        self.assertEqual(by_check["update proposed for review"]["result"], "failed")
        self.assertIn("finished or undone", by_check["update proposed for review"]["detail"])


class TestRollbackIsHonestAboutWhatItCannotDo(unittest.TestCase):
    def test_nothing_to_undo_points_at_the_reviewed_revert_instead(self):
        with mock.patch.object(module_manager, "rollback", return_value={"state": "none"}):
            with self.assertRaises(transaction.TransactionRefused) as caught:
                adapters.RollbackUpgrade().plan(Args(), {"fingerprints": {}})
        self.assertEqual(caught.exception.code, "nothing-to-undo")
        self.assertIn("reverting its pull request", caught.exception.explanation)

    def test_a_staged_undo_states_the_recovery_point_and_the_refusal_guard(self):
        with mock.patch.object(module_manager, "rollback", return_value={"state": "staged"}):
            plan = adapters.RollbackUpgrade().plan(Args(), {"fingerprints": {}})
        joined = " ".join(plan["consequences"])
        self.assertIn("recovery point", joined)
        self.assertIn("unsaved work of your own", joined)

    def test_an_undo_that_did_nothing_hands_off_to_the_reviewed_revert(self):
        result = adapters.RollbackUpgrade().handoff(Args(), {"undone": False}, [])
        self.assertEqual(result["kind"], "manual-follow-up")
        self.assertIn("never rewrites your main line", result["summary"])


if __name__ == "__main__":
    unittest.main()
