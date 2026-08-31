#!/usr/bin/env python3
"""The upgrade transaction: what its consent binds, and what it deliberately does not gate."""
from __future__ import annotations

import contextlib
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import module_manager  # noqa: E402
import transaction  # noqa: E402
import transaction_adapters_upgrade as adapters  # noqa: E402
import transaction_envelope as te  # noqa: E402
import transaction_handoff as handoff  # noqa: E402

# The base-currency gate (a separate obligation from consent) refuses a wrong/stale base before the
# upgrade apply and inside the operator-typed door. These tests predate it and exercise consent and
# release-resolution against this in-place checkout, which is legitimately a wrong base — so they hold
# the currency gate open to isolate the behavior each one is actually about. Its own refusals are
# covered in test_transaction_handoff.
_CURRENCY_OK = {"verified": False, "note": "base currency held open for this test"}


def _pass_currency():
    return mock.patch.object(handoff, "refuse_if_stale_base", return_value=_CURRENCY_OK)


def _pass_door_currency():
    return mock.patch.object(module_manager, "_door_base_currency", return_value=(False, ""))


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

    def test_the_parser_reports_an_absent_handle_as_none_and_decides_nothing(self):
        """Parsing only. Whether absent REFUSES is `main`'s decision, covered separately below -- it does."""
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
        """Uses the shape the domain really returns for an up-to-date engine. This test previously
        passed `available=None, target=None`, which `upgrade_preview` never produces -- it encoded the
        same wrong belief as the code, so the two agreed with each other and not with reality."""
        with mock.patch.object(module_manager, "upgrade_preview",
                               return_value=dict(PREVIEW, status="up-to-date",
                                                 available="1.0.0", target_ref="1.0.0")):
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
class TestConsentIsEnforcedOnTheCommandTheOperatorTypes(unittest.TestCase):
    """The obligation names the OPERATOR-TYPED apply path, not the helper under it.

    `_refuse_stale_consent` being correct proves nothing about whether `main` ever calls it: a dispatch
    that read the flag and fell through would pass every helper-level test while applying a stale plan.
    These drive `module_manager.main([...])` — the exact argv the skill types — and assert on whether the
    mutating `upgrade()` was reached at all.
    """

    def _main(self, *argv, stale):
        refusal = "This is not the update you read." if stale else None
        with mock.patch.object(module_manager, "_refuse_stale_consent", return_value=refusal), \
             _pass_door_currency(), \
             mock.patch.object(module_manager, "upgrade") as applied:
            code = module_manager.main(list(argv))
        return code, applied

    def test_a_stale_handle_refuses_without_ever_reaching_the_apply(self):
        code, applied = self._main("upgrade", "--confirm", "--consent-handle", "sha256:" + "9" * 64,
                                   stale=True)
        self.assertEqual(code, 2)
        applied.assert_not_called()

    def test_a_matching_handle_reaches_the_apply(self):
        code, applied = self._main("upgrade", "--confirm", "--consent-handle", "sha256:" + "0" * 64,
                                   stale=False)
        applied.assert_called_once()
        self.assertNotEqual(code, 2)

    def test_the_equals_form_the_skill_may_emit_is_enforced_the_same_way(self):
        code, applied = self._main("upgrade", "--confirm", "--consent-handle=sha256:" + "9" * 64,
                                   stale=True)
        self.assertEqual(code, 2)
        applied.assert_not_called()


class TestUpgradeResumesFromWhatItRecorded(unittest.TestCase):
    """Upgrade is the one adapter with durable progress, so its resume must NOT be the generic re-plan."""

    def _resume(self, state, announced):
        with mock.patch.object(module_manager, "_diagnose_undo",
                               return_value={"state": state, "current": "1.0.0"}), \
             mock.patch.object(module_manager, "staged_upgrade_announced", return_value=announced):
            return adapters.UpgradeEngine().resume(Args())

    def test_a_recorded_staged_update_is_reported_rather_than_re_planned(self):
        resumed = self._resume("staged", True)
        self.assertIsNotNone(resumed)
        te.validate(resumed)
        self.assertEqual(resumed["handoff"]["kind"], "local-recovery")
        self.assertEqual(resumed["verification"][0]["result"], "passed")

    def test_nothing_recorded_falls_through_to_the_honest_fresh_plan(self):
        self.assertIsNone(self._resume("none", False))

    def test_a_merely_dirty_tree_is_not_mistaken_for_a_staged_update(self):
        """StarshipSuperjam/engine-template#948's failure shape, in resume's clothing.

        `_diagnose_undo` answers `staged` for ANY dirty overlay tree on purpose — generosity is right
        where it offers an undo. Resume withholds the fresh plan when it claims progress, so inheriting
        that generosity would tell an operator with nothing staged not to apply.
        """
        self.assertIsNone(self._resume("staged", False))


class TestInspectSurvivesHavingNoTargetRelease(unittest.TestCase):
    """`inspect engine-upgrade` died with an unhandled EnvelopeError whenever no target release existed --
    already current, offline, inconsistent, mid-transaction. The summary line beside the fingerprints
    composes prose for exactly that state, so the method contradicted itself; it passed locally only
    because this machine happened to have an update available."""

    def test_no_available_release_is_an_answer_not_a_crash(self):
        for preview in ({"current": "1.0.0"},
                        {"status": "unreachable", "current": "1.0.0"},
                        {"current": None},
                        {"status": "transaction-incomplete", "current": "1.0.0"}):
            with mock.patch.object(module_manager, "upgrade_preview", return_value=preview):
                facts = adapters.UpgradeEngine().inspect(Args())
            for key, value in facts["fingerprints"].items():
                self.assertTrue(str(value).strip(), "{0} was blank for {1}".format(key, preview))


class TestApplyUsesTheReleaseThePlanNamed(unittest.TestCase):
    """The handle is checked against the concretely resolved tag the plan recorded -- then apply used to
    hand `upgrade()` the raw operand (None for the ordinary case), which resolved "latest" a SECOND time.
    A release landing between those moments meant consent for X applied Y, on the route the runbook
    points at."""

    def test_the_planned_release_is_what_gets_applied(self):
        plan = {"inputs": {"release": "v9.9.9"}}
        with _pass_currency(), \
             mock.patch.object(module_manager, "upgrade", return_value={"pr": {"url": "u"}}) as applied:
            adapters.UpgradeEngine().apply(Args(), plan)
        applied.assert_called_once_with("v9.9.9")

    def test_a_plan_naming_no_release_still_falls_back_to_the_operand(self):
        with _pass_currency(), \
             mock.patch.object(module_manager, "upgrade", return_value={"pr": {"url": "u"}}) as applied:
            adapters.UpgradeEngine().apply(Args("v1"), {"inputs": {}})
        applied.assert_called_once_with("v1")


class TestTheTypedCommandAppliesTheReleaseItsHandleApproved(unittest.TestCase):
    """The path the SKILL documents, and the one the obligation names.

    `_refuse_stale_consent` resolved "latest" to a concrete tag, compared the handle against it, and threw
    the plan away -- then `main` ran `upgrade(None)`, resolving latest a SECOND time. So the handle was
    verified against release X while release Y could be applied, on the route whose own notes promise the
    handle guarantees the thing applied is the thing you read. The adapter-level fix did not reach here.
    """

    def test_the_verified_plans_release_is_what_main_applies(self):
        def fake_check(ref, handle, on_release=None):
            if on_release is not None:
                on_release("v7.7.7")
            return None

        with mock.patch.object(module_manager, "_refuse_stale_consent", side_effect=fake_check), \
             _pass_door_currency(), \
             mock.patch.object(module_manager, "upgrade") as applied:
            module_manager.main(["upgrade", "--confirm", "--consent-handle", "sha256:" + "0" * 64])
        applied.assert_called_once_with("v7.7.7")

    def test_a_stale_handle_still_refuses_before_anything_is_applied(self):
        with mock.patch.object(module_manager, "_refuse_stale_consent", return_value="no match"), \
             _pass_door_currency(), \
             mock.patch.object(module_manager, "upgrade") as applied:
            code = module_manager.main(["upgrade", "--confirm", "--consent-handle", "sha256:" + "9" * 64])
        self.assertEqual(code, 2)
        applied.assert_not_called()


class TestPlanNamesEveryStateItCannotPlanFrom(unittest.TestCase):
    """Driven off the shapes `upgrade_preview` and `plan_upgrade` ACTUALLY return.

    The previous version asserted the already-current path with `{"current": "1.0.0"}` -- a shape the
    domain never produces. The real `up-to-date` preview sets `available = target_ref`, so the branch
    under test could not fire for a genuinely current engine at all, and that operator was handed a real
    plan and a consent handle whose first consequence read "Moves this engine to" the version it was
    already on. A test built on an invented shape proves the code handles the invention.
    """

    def _refusal(self, preview):
        with mock.patch.object(module_manager, "upgrade_preview", return_value=preview):
            try:
                adapters.UpgradeEngine().plan(Args(), {"fingerprints": {}})
            except transaction.TransactionRefused as refused:
                return refused
        self.fail("expected a refusal for " + repr(preview))

    def test_a_genuinely_current_engine_is_refused_not_handed_consent_for_a_no_op(self):
        refused = self._refusal({"status": "up-to-date", "current": "1.0.0",
                                 "available": "1.0.0", "target_ref": "1.0.0"})
        self.assertEqual(refused.code, "already-current")
        self.assertFalse(refused.retryable)

    def test_no_recorded_update_home_is_named_rather_than_called_up_to_date(self):
        """The fifth state -- missed when this was repaired for the four a reviewer happened to name.
        The operator was told they were on the newest version their update home offers, while having no
        update home at all, and the domain's own actionable reason was discarded."""
        refused = self._refusal({"status": "no-home", "current": "1.0.0",
                                 "reason": "This engine has no update home recorded."})
        self.assertEqual(refused.code, "no-update-home")
        self.assertIn("no update home recorded", refused.explanation)

    def test_an_interrupted_update_is_named_and_leads_somewhere(self):
        refused = self._refusal({"status": "transaction-incomplete", "current": "1.0.0",
                                 "reason": "An earlier update is in progress."})
        self.assertEqual(refused.code, "unfinished-update")
        self.assertTrue(any("resume" in step or "rollback" in step for step in refused.next_actions))

    def test_being_offline_is_the_only_retryable_case(self):
        self.assertTrue(self._refusal({"status": "unreachable", "current": "1.0.0",
                                       "reason": "No network."}).retryable)
        for status in ("no-home", "inconsistent", "missing-release", "transaction-incomplete"):
            self.assertFalse(self._refusal({"status": status, "current": "1.0.0", "reason": "x"}).retryable,
                             status)

    def test_an_inconsistent_engine_is_named_as_one(self):
        self.assertEqual(self._refusal({"status": "inconsistent", "current": "1.0.0",
                                        "reason": "Half-built."}).code, "engine-inconsistent")

    def test_a_shape_this_version_cannot_read_refuses_as_unknown_rather_than_claiming_current(self):
        self.assertEqual(self._refusal({"current": "1.0.0"}).code, "preview-unrecognised")


class TestTheRealConsentCheckHandsBackTheRealRelease(unittest.TestCase):
    """The JOIN, not each half. A reviewer's exact point: this seam was proven across two mocks -- one
    test faked `_refuse_stale_consent` to hand back a release, and the tests driving the real function
    passed no callback. "Each half tested, the join not" is the shape that let the substitution defect
    survive three rounds, so it is worth one test that drives the real function against a real plan."""

    def test_a_matching_handle_yields_the_plans_resolved_release(self):
        seen = {}
        with mock.patch.object(module_manager, "upgrade_preview", return_value=dict(PREVIEW)):
            adapter = adapters.UpgradeEngine()
            facts = adapter.inspect(Args())
            plan = dict(adapter.plan(Args(), facts))
            plan["bound_fingerprints"] = dict((facts or {}).get("fingerprints") or {})
            handle = te.consent_handle(plan)
            message = module_manager._refuse_stale_consent(
                None, handle, on_release=lambda r: seen.update(release=r))
        self.assertIsNone(message, message)
        self.assertEqual(seen.get("release"), PREVIEW["available"],
                         "the real check did not hand back the release its own plan named")

    def test_a_stale_handle_hands_back_nothing(self):
        seen = {}
        with mock.patch.object(module_manager, "upgrade_preview", return_value=dict(PREVIEW)):
            message = module_manager._refuse_stale_consent(
                None, "sha256:" + "9" * 64, on_release=lambda r: seen.update(release=r))
        self.assertIsNotNone(message)
        self.assertEqual(seen, {}, "a release was handed back despite the handle not matching")


class TestTheRecordedTargetIsShapeChecked(unittest.TestCase):
    """The recorded tag flows into `/repos/{slug}/tarball/{ref}`, deciding what code is overlaid, on the
    one path that skips the consent handle."""

    def _detail(self, value):
        import json as _json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "marker.json")
            with open(path, "w", encoding="utf-8") as handle:
                _json.dump({"target_ref": value}, handle)
            with mock.patch.object(module_manager, "_staged_upgrade_marker_path", return_value=path):
                return module_manager.staged_upgrade_detail()

    def test_an_ordinary_tag_is_read_back(self):
        self.assertEqual(self._detail("v1.2.3"), {"target_ref": "v1.2.3"})

    def test_a_traversal_shaped_ref_is_refused_rather_than_fetched(self):
        for hostile in ("../other/repo", "v1/../x", "a b", ""):
            self.assertEqual(self._detail(hostile), {}, hostile)


class TestResumeNeverRendersAnUnreadStateGreen(unittest.TestCase):
    """The envelope's core rule, applied to the one place the repair had broken it."""

    def _resume(self, diagnosis):
        with mock.patch.object(module_manager, "_diagnose_undo", return_value=diagnosis), \
             mock.patch.object(module_manager, "staged_upgrade_announced", return_value=True):
            return adapters.UpgradeEngine().resume(Args())

    def test_a_state_this_version_cannot_read_is_unavailable_not_passed(self):
        resumed = self._resume({"state": "something-a-later-version-writes", "current": "1.0.0"})
        te.validate(resumed)
        self.assertEqual(resumed["verification"][0]["result"], "unavailable")

    def test_a_completed_update_is_not_reported_as_mid_flight(self):
        """`upgrade` tolerates the transaction record surviving after the pull request is open. Reading
        that as 'mid-flight' tells an operator whose update SUCCEEDED not to touch it."""
        resumed = self._resume({"state": "transaction", "current": "1.0.0",
                                "transaction": {"record": {"phase": "pr-opened",
                                                           "pull_request": {"url": "https://x/1"}}}})
        te.validate(resumed)
        self.assertEqual(resumed["handoff"]["kind"], "pull-request")
        self.assertIn("nothing to resume", resumed["verification"][0]["detail"])

    def test_a_genuinely_mid_flight_transaction_still_says_so(self):
        resumed = self._resume({"state": "transaction", "current": "1.0.0",
                                "transaction": {"record": {"phase": "mutating"}}})
        te.validate(resumed)
        self.assertEqual(resumed["handoff"]["kind"], "local-recovery")
        self.assertIn("compound", resumed["verification"][0]["detail"])

    def test_memory_ahead_is_reported_on_its_own_terms(self):
        resumed = self._resume({"state": "memory-ahead", "current": "1.0.0", "tag": "t"})
        te.validate(resumed)
        self.assertIn("saved data", resumed["verification"][0]["detail"])


class TestTheOtherAdaptersReplanRatherThanPretend(unittest.TestCase):
    """The differentiation is the point: only upgrade records progress, so only upgrade continues."""

    def test_add_remove_and_engine_removal_have_no_progress_to_read_back(self):
        import transaction_adapters_module as module_adapters
        import transaction_adapters_remove as remove_adapters
        for adapter in (module_adapters.AddModule(), module_adapters.RemoveModule(),
                        remove_adapters.RemoveEngine()):
            self.assertIsNone(adapter.resume(Args()), adapter.operation)


class TestAnAbsentHandleIsARefusalNotAPass(unittest.TestCase):
    """The plan said a stale OR ABSENT handle refuses. An optional gate is the same as no gate: a session
    that wants to apply just omits the flag. But the refusal must not swallow the documented recovery."""

    def _main(self, *argv, staged, detail=None, undo="none"):
        with mock.patch.object(module_manager, "staged_upgrade_announced", return_value=staged), \
             mock.patch.object(module_manager, "staged_upgrade_detail",
                               return_value=(detail if detail is not None else {"target_ref": "v9"})), \
             mock.patch.object(module_manager, "_diagnose_undo", return_value={"state": undo}), \
             _pass_door_currency(), \
             mock.patch.object(module_manager, "upgrade") as applied:
            code = module_manager.main(list(argv))
        return code, applied

    def test_a_fresh_apply_with_no_handle_refuses_without_mutating(self):
        code, applied = self._main("upgrade", "--confirm", staged=False)
        self.assertEqual(code, 2)
        applied.assert_not_called()

    def test_finishing_a_staged_update_applies_the_release_that_was_actually_staged(self):
        """The defect this replaces: both branches fell through to `upgrade(ref)`, which re-resolves
        "latest" against whatever the home publishes NOW. A release published during the interruption
        would have been applied under the earlier release's consent -- the exact drift the handle exists
        to catch, switched off where drift is most likely."""
        code, applied = self._main("upgrade", "--confirm", staged=True, detail={"target_ref": "v9"})
        applied.assert_called_once_with("v9")
        self.assertNotEqual(code, 2)

    def test_a_staged_copy_that_recorded_no_target_refuses_rather_than_resolving_afresh(self):
        code, applied = self._main("upgrade", "--confirm", staged=True, detail={}, undo="staged")
        self.assertEqual(code, 2)
        applied.assert_not_called()

    def test_finishing_cannot_be_redirected_to_a_different_release(self):
        """`upgrade --confirm some-other-tag` with no handle must not ride the staged exception."""
        code, applied = self._main("upgrade", "--confirm", "v10", staged=True, detail={"target_ref": "v9"})
        self.assertEqual(code, 2)
        applied.assert_not_called()

    def test_a_dirty_tree_with_no_marker_is_told_the_truth_not_that_nothing_is_staged(self):
        """An update staged by a version predating the marker leaves none at all. The old message told
        that operator "there is none staged" while `_diagnose_undo` said otherwise, and pointed them at a
        fresh plan -- which re-applies the overlay, the one thing resume warns against."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code, applied = self._main("upgrade", "--confirm", staged=False, undo="staged")
        self.assertEqual(code, 2)
        applied.assert_not_called()
        said = buf.getvalue()
        self.assertIn("interrupted update is present", said)
        self.assertIn("rollback --confirm", said)
        self.assertNotIn("none staged", said)

    def test_the_recovery_opening_asks_the_narrow_question_not_the_dirty_tree_one(self):
        """Reusing the generous predicate here would let any dirty checkout skip the gate entirely.

        Behavioural, not a source-text check: with the narrow predicate false, a fresh apply must refuse
        even though the generous one would have said `staged`."""
        code, applied = self._main("upgrade", "--confirm", staged=False, undo="staged")
        self.assertEqual(code, 2)
        applied.assert_not_called()


# Kept LAST on purpose: this block used to sit mid-file, so every test class below it was
# invisible to anyone running the file directly -- 19 of this build's own tests among them. CI
# uses discovery and ran them, which is the same "green over a gap" shape as the defect repaired
# here.
if __name__ == "__main__":
    unittest.main()
