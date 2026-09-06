#!/usr/bin/env python3
"""Focused tests for review ordering, contracts, and disagreement evidence."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import selftest_support  # noqa: E402  (the suite's single-homed guard helpers, #940)
import build_coordinator as bc  # noqa: E402
import build_coordinator_review as review  # noqa: E402
from test_build_coordinator import BASE, HEAD_A, CoordinatorCase, plan, PLAN_ID, SEALED  # noqa: E402


class TestThePlanGateIsGoneFromThisSide(CoordinatorCase):
    """The whole plan-review ordering suite retires with the gate it protected.

    Its subject was: a Build must not proceed until its plan review is complete and its reviewer
    contracts are fresh. That precondition now holds by construction — a Build binds only a SEALED plan,
    and a seal refuses a review that does not cover its approved depth — so there is nothing left to
    order, refresh, or waive on this side. What is worth pinning is that the gate really is unreachable
    rather than merely unused.
    """

    def setUp(self):
        super().setUp()
        self.seed()
        self.approve("quick")
        self.note_path = Path(self.temp.name) / "checkpoint.json"
        self.note_path.write_text(json.dumps({
            "objective": "x", "current_work": "implementation", "work_item": "W1",
            "assumptions": [], "non_goals": [], "planned_scope": [],
            "remaining_verification": [], "judgment": "aligned",
        }), encoding="utf-8")

    def test_checkpoint_no_longer_waits_on_a_plan_review(self):
        with mock.patch.object(bc, "_assert_spec_boundary", return_value={}), \
                mock.patch.object(bc, "_head", return_value=HEAD_A), \
                mock.patch.object(bc, "_changed_paths", return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_checkpoint(argparse.Namespace(plan=str(self.plan_path), input=str(self.note_path),
                                                 complete_item=None, json=False), self.store)
        self.assertEqual(self.state()["checkpoint"]["work_item"], "W1")

    def test_the_plan_gate_and_its_waiver_are_both_unreachable(self):
        for gone in ("_plan_review_ready", "cmd_review_waive", "_record_plan_panel"):
            self.assertFalse(hasattr(bc, gone), gone)
        self.assertFalse(hasattr(review, "plan_review_ready"))

    def test_the_review_module_no_longer_threads_a_stage(self):
        # `installed` and `required` answered for two stages; one of them is gone, and a parameter that
        # can take only one value is an invitation to reintroduce the other.
        import inspect
        self.assertNotIn("stage", inspect.signature(review.installed).parameters)
        self.assertNotIn("stage", inspect.signature(review.required).parameters)


class TestReviewerContractFreshness(unittest.TestCase):
    def test_one_changed_contract_invalidates_only_its_receipt(self):
        referent = "sha256:" + "a" * 64
        first = {"lens": "architecture", "path": "a.md", "digest": "sha256:" + "1" * 64}
        second = {"lens": "feasibility", "path": "f.md", "digest": "sha256:" + "2" * 64}
        contracts = review.lens_packets(referent, [first, second])
        stage = {"reviewer_contracts": contracts, "receipts": [
            {"lens": item["lens"], "lens_packet_digest": item["lens_packet_digest"]}
            for item in contracts
        ]}
        changed = review.lens_packets(referent, [{**first, "digest": "sha256:" + "3" * 64}, second])
        stage["reviewer_contracts"] = changed
        self.assertEqual(review.current_receipt_lenses(stage), {"feasibility"})

    def test_downgraded_blocking_finding_line_publishes_only_operator_summary(self):
        # StarshipSuperjam/engine-template#981: the disagreement line is published verbatim to the
        # public PR body, so it must carry ONLY the operator-safe summary — never `private_reference`.
        finding = {"id": "SEC-1", "severity": "blocking", "blocks_this_pr": False,
                   "operator_summary": "The public concern and rejection rationale.",
                   "private_reference": "private security note S-1"}
        line = review.disagreement_line(finding)
        self.assertIn("Reviewer disagreement `SEC-1`", line)
        self.assertIn("The public concern and rejection rationale.", line)
        self.assertNotIn("private security note S-1", line)
        self.assertNotIn("Private details", line)

    def test_disagreement_line_breaks_a_quoted_closing_keyword(self):
        # PR #1229: a reviewer's summary may quote a past accident ("auto-closed #1210"); published verbatim
        # it would close that issue on merge. Broken at this single source, so the body and the preflight
        # that asserts the line present agree on one text.
        import close_linkage_preflight
        finding = {"id": "QA-9", "severity": "blocking", "blocks_this_pr": False,
                   "operator_summary": "C1's wording auto-closed #1210 on merge; Fixes #1167 was meant."}
        line = review.disagreement_line(finding)
        self.assertEqual(line, "- Reviewer disagreement `QA-9`: C1's wording auto-closed issue #1210 on merge; "
                               "Fixes issue #1167 was meant.")
        self.assertIsNone(close_linkage_preflight._CLOSE_LIST_RE.search(line))

    def test_disagreement_line_without_operator_summary_is_still_safe(self):
        # A missing operator_summary must never fall back to private text; the line renders a legible
        # placeholder, not the private note and not a dangling colon. (cmd_finding_record requires
        # operator_summary on these findings, so this is defense in depth against a malformed or legacy
        # finding reaching the renderer.)
        finding = {"id": "SEC-2", "severity": "blocking", "blocks_this_pr": False,
                   "operator_summary": None, "private_reference": "private note that must never leak"}
        line = review.disagreement_line(finding)
        self.assertNotIn("private note that must never leak", line)
        self.assertNotIn("Private details", line)
        self.assertEqual(line, "- Reviewer disagreement `SEC-2`: [no operator-safe summary recorded]")

    def test_product_intent_challenges_no_spec_and_selected_document_judgment(self):
        # This case reads a reviewer prompt the design-review module DELIVERS, so a deployment that declined it
        # has no subject to assert over — the absence is the module's contract.
        selftest_support.needs_modules(self, "design-review", reason=(
            "design-review is not installed in this repository, so the reviewer prompt this case reads is "
            "legitimately absent here"))
        prompt = (bc.ROOT / ".claude/agents/engine-design-review-product-intent.md").read_text()
        self.assertIn("For a `no-spec` plan", prompt)
        self.assertIn("every semantically affected document", prompt)
        self.assertIn("only inside the documents the orchestrator selected", prompt)


class TestDisagreementPreflight(CoordinatorCase):
    def test_downgraded_blocking_finding_must_appear_in_pr_review_record(self):
        self.seed()
        self.store.mutate(lambda state: state["findings"].append({
            "id": "SEC-1", "stage": "plan", "lens": "risk-governance",
            "packet_digest": state["plan"]["digest"], "lens_packet_digest": state["plan"]["digest"],
            "commit": None, "severity": "blocking", "summary": "private",
            "disposition": "rejected", "rationale": "private rationale", "escalation_kind": None,
            "blocks_this_pr": False, "handoff_summary": "bounded",
            "operator_summary": "The concern was rejected on verified evidence.", "private_reference": None,
        }))
        pr = {"body": "complete PR without disagreement", "baseRefOid": BASE}
        command = subprocess.CompletedProcess([], 0, "ok", "")
        with mock.patch.object(bc, "_head", return_value=HEAD_A), \
                mock.patch.object(bc, "_verify_draft", return_value=pr), \
                mock.patch.object(bc, "_run", return_value=command), \
                mock.patch.object(bc, "_pr_contract", return_value=(True, "complete")), \
                self.assertRaisesRegex(bc.CoordinatorError, "PR-contract"):
            bc.cmd_preflight(argparse.Namespace(pr_body=None, json=False), self.store)
        self.assertFalse(self.state()["pr_contract"]["complete"])


class TestStandardPlanRow(unittest.TestCase):
    """#677 decision 2 survives the panel move: standard depth still runs all four plan lenses (coverage
    over per-lens depth at the plan gate — plan-stage misses are unrecoverable downstream). It is now
    declared where the panel lives, and the Build protocol no longer mentions plan review at all."""
    def _protocol(self):
        here = os.path.dirname(os.path.abspath(__file__))
        return json.load(open(os.path.join(here, "..", "build-protocol.json"), encoding="utf-8"))

    def test_standard_plan_review_runs_all_four_lenses(self):
        import project_manager
        self.assertEqual(set(project_manager.PLAN_REVIEW_LENSES["standard"]),
                         {"product-intent", "architecture", "feasibility", "risk-governance"})
        self.assertNotIn("plan_review", self._protocol())

    def test_both_coordinators_speak_one_depth_vocabulary(self):
        # The shared file that used to guarantee this is gone, so the guarantee is pinned instead. A depth
        # approved on the plan side IS the depth the Build's deliverable review runs at, and a vocabulary
        # that drifted would silently break that single consent.
        import project_manager
        self.assertEqual(set(project_manager.PLAN_REVIEW_LENSES), set(review.DEPTH_ORDER))
        self.assertEqual(project_manager.DEPTH_ORDER, review.DEPTH_ORDER)
        self.assertEqual(set(project_manager.DEPTHS), set(self._protocol()["deliverable_review"]))
        self.assertEqual(set(project_manager.PLAN_REVIEW_LENSES),
                         set(self._protocol()["deliverable_review"]))
        # `quick` is the floor on both sides: it runs nobody.
        self.assertEqual(project_manager.PLAN_REVIEW_LENSES["quick"], [])
        self.assertEqual(self._protocol()["deliverable_review"]["quick"], [])
        roster = [{"lens": l} for l in ("product-intent", "architecture", "feasibility", "risk-governance")]
        self.assertEqual(len(project_manager.required_lenses("standard", roster)), 4)


class TestAvailableDepths(unittest.TestCase):
    """#763/#677: the consent surface offers only depths that add something — keyed on the lens-set alone."""
    def _protocol(self):
        here = os.path.dirname(os.path.abspath(__file__))
        return json.load(open(os.path.join(here, "..", "build-protocol.json"), encoding="utf-8"))

    def _roster(self, *lenses):
        return [{"lens": l} for l in lenses]

    def test_zero_lenses_collapses_to_quick_only(self):
        got = review.available_depths(self._protocol(), self._roster())
        self.assertEqual(got, ["quick"])

    def test_full_roster_offers_all_three(self):
        deliverable = self._roster("spec-conformance", "divergence-hunter", "usability",
                                   "technical-integrity", "security-governance")
        got = review.available_depths(self._protocol(), deliverable)
        self.assertEqual(got, ["quick", "standard", "thorough"])

    def test_a_roster_where_thorough_adds_no_lens_does_not_offer_it(self):
        # Only the standard-subset lenses are installed (an optional review pack declined): thorough would run
        # exactly what standard runs, so it is honestly not offered — the roster is the whole difference.
        deliverable = self._roster("spec-conformance", "divergence-hunter", "usability")
        got = review.available_depths(self._protocol(), deliverable)
        self.assertEqual(got, ["quick", "standard"])

    def test_one_standard_table_lens_offers_quick_and_standard(self):
        # A single installed reviewer whose lens IS in the standard table: standard adds that lens over quick,
        # and thorough adds nothing over standard -> two depths.
        got = review.available_depths(self._protocol(), self._roster("spec-conformance"))
        self.assertEqual(got, ["quick", "standard"])

    def test_one_thorough_only_lens_skips_standard(self):
        # A single installed reviewer whose lens runs ONLY at thorough (security-governance is not in the
        # standard deliverable table): standard would run nothing the quick floor doesn't, so it collapses,
        # yet thorough still adds that lens -> the offer skips the middle depth entirely.
        got = review.available_depths(self._protocol(), self._roster("security-governance"))
        self.assertEqual(got, ["quick", "thorough"])

    def test_non_monotonic_tables_still_offer_a_depth_with_unique_coverage(self):
        # Robustness (keyed on set-DIFFERENCE, not strict superset): if the per-depth lens tables are ever
        # non-monotonic — a lighter depth naming a lens a heavier depth's table omits — a depth that runs
        # genuinely unique coverage must still be OFFERED, never silently hidden (which would invert the
        # feature's purpose). Inert with the shipped monotonic tables; this guards a future table edit.
        protocol = {"deliverable_review": {"quick": ["risk-governance"], "standard": ["product-intent"],
                                          "thorough": []}}
        roster = self._roster("risk-governance", "product-intent")
        got = review.available_depths(protocol, roster)
        # standard runs product-intent, which quick (risk-governance) does not -> it must be offered, not hidden.
        self.assertIn("standard", got)
        self.assertEqual(got[0], "quick")   # quick is always the floor

    def test_the_offer_rule_takes_no_effort_argument(self):
        with self.assertRaises(TypeError):
            review.available_depths(self._protocol(), self._roster("spec-conformance"),
                                    {"quick": None, "standard": "medium", "thorough": "high"})


if __name__ == "__main__":
    unittest.main()
