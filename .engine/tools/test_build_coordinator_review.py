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
import build_coordinator as bc  # noqa: E402
import build_coordinator_review as review  # noqa: E402
from test_build_coordinator import BASE, HEAD_A, CoordinatorCase, plan  # noqa: E402


def _needs_design_review(case) -> None:
    """Skip when design-review is not installed: this case reads a reviewer prompt that module DELIVERS, so a
    deployment that declined it has no subject to assert over — the absence is the module's contract."""
    import module_coherence
    ids = {m.get("id") for _p, m in module_coherence.discover_manifests() if isinstance(m, dict)}
    if "design-review" not in ids:
        case.skipTest("design-review is not installed in this repository, so the reviewer prompt this "
                      "case reads is legitimately absent here")


class TestPlanReviewOrdering(CoordinatorCase):
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

    def test_checkpoint_requires_completed_plan_review(self):
        with self.assertRaisesRegex(bc.CoordinatorError, "before plan review"):
            bc.cmd_checkpoint(argparse.Namespace(plan=str(self.plan_path), input=str(self.note_path),
                                                 complete_item=None, json=False), self.store)

    def test_final_validation_cannot_precede_plan_review(self):
        with self.assertRaisesRegex(bc.CoordinatorError, "before plan review"):
            bc.cmd_validate(argparse.Namespace(), self.store)

    def test_changed_installed_plan_contract_blocks_checkpoint_until_refreshed(self):
        self.approve("standard")
        args = argparse.Namespace(stage="plan", plan=str(self.plan_path), impact=None,
                                  standalone=False, output=None, json=True)
        reviewer = {"lens": "product-intent", "path": "reviewer.md",
                    "digest": "sha256:" + "1" * 64}
        with mock.patch.object(bc, "_installed", return_value=[reviewer]), \
                contextlib.redirect_stdout(io.StringIO()):
            bc._packet(args, self.store)
        packet = self.state()["reviews"]["plan"]["packet_digest"]
        contract = self.state()["reviews"]["plan"]["reviewer_contracts"][0]
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_review_record(argparse.Namespace(stage="plan", lens="product-intent",
                                                    packet_digest=packet,
                                                    lens_packet_digest=contract["lens_packet_digest"],
                                                    finding=[], code_execution="none"), self.store)
        changed = {**reviewer, "digest": "sha256:" + "2" * 64}
        with mock.patch.object(bc, "_installed", return_value=[changed]), \
                self.assertRaisesRegex(bc.CoordinatorError, "refresh plan-review contract"):
            bc.cmd_checkpoint(argparse.Namespace(plan=str(self.plan_path), input=str(self.note_path),
                                                 complete_item=None, json=False), self.store)

    def test_routine_cannot_use_retrospective_plan_review_waiver(self):
        value = plan()
        value["profile"] = "routine"
        value["intent_source"] = {"kind": "issue", "issue": 7}
        self.write_plan(value)
        state = bc._initial_state("owner/repo", 7, BASE, "issue", value, 7, "unattended")
        state["approval"] = {"plan_digest": state["plan"]["digest"], "spec_digest": None, "depth": "quick"}
        routine = bc.StateStore(str(Path(self.temp.name) / "routine.json"))
        routine.create(state)
        with self.assertRaisesRegex(bc.CoordinatorError, "same-session normal"):
            bc.cmd_review_waive(argparse.Namespace(stage="plan", reason="not eligible",
                                                   adopted_commit=state["plan"]["bound_head"]), routine)

    def test_operator_approved_normal_quick_still_uses_zero_cold_lenses(self):
        args = argparse.Namespace(stage="plan", plan=str(self.plan_path), impact=None,
                                  standalone=False, output=None, json=True)
        with mock.patch.object(bc, "_installed", return_value=[]), contextlib.redirect_stdout(io.StringIO()):
            bc._packet(args, self.store)
        with mock.patch.object(bc, "_assert_spec_boundary", return_value={}), \
                mock.patch.object(bc, "_head", return_value=HEAD_A), \
                mock.patch.object(bc, "_changed_paths", return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_checkpoint(argparse.Namespace(plan=str(self.plan_path), input=str(self.note_path),
                                                 complete_item=None, json=False), self.store)


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
        _needs_design_review(self)
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
    """#677 decision 2: the committed protocol runs all four plan lenses at standard depth (coverage over
    per-lens depth at the plan gate — plan-stage misses are unrecoverable downstream)."""
    def _protocol(self):
        here = os.path.dirname(os.path.abspath(__file__))
        return json.load(open(os.path.join(here, "..", "build-protocol.json"), encoding="utf-8"))

    def test_standard_plan_review_runs_all_four_lenses(self):
        proto = self._protocol()
        self.assertEqual(set(proto["plan_review"]["standard"]),
                         {"product-intent", "architecture", "feasibility", "risk-governance"})
        roster = [{"lens": l} for l in ("product-intent", "architecture", "feasibility", "risk-governance")]
        self.assertEqual(len(review.required(proto, "plan", "standard", roster)), 4)


class TestAvailableDepths(unittest.TestCase):
    """#763/#677: the consent surface offers only depths that add something — keyed on lens-set AND effort."""
    def _protocol(self):
        here = os.path.dirname(os.path.abspath(__file__))
        return json.load(open(os.path.join(here, "..", "build-protocol.json"), encoding="utf-8"))

    _EFFORTS = {"quick": None, "standard": "medium", "thorough": "high"}

    def _roster(self, *lenses):
        return [{"lens": l} for l in lenses]

    def test_zero_lenses_collapses_to_quick_only(self):
        got = review.available_depths(self._protocol(), self._roster(), self._roster(), self._EFFORTS)
        self.assertEqual(got, ["quick"])

    def test_full_roster_offers_all_three(self):
        plan = self._roster("product-intent", "architecture", "feasibility", "risk-governance")
        deliverable = self._roster("spec-conformance", "divergence-hunter", "usability",
                                   "technical-integrity", "security-governance")
        got = review.available_depths(self._protocol(), plan, deliverable, self._EFFORTS)
        self.assertEqual(got, ["quick", "standard", "thorough"])

    def test_effort_only_difference_still_offers_the_heavier_depth(self):
        # Partial roster where standard's and thorough's lens-sets COINCIDE (full plan lenses + only the
        # standard-subset deliverable lenses): the depths differ ONLY by effort, and the heavier depth is
        # still offered because effort distinguishes them (architecture F1's genuine effort-only case).
        plan = self._roster("product-intent", "architecture", "feasibility", "risk-governance")
        deliverable = self._roster("spec-conformance", "divergence-hunter", "usability")
        got = review.available_depths(self._protocol(), plan, deliverable, self._EFFORTS)
        self.assertEqual(got, ["quick", "standard", "thorough"])

    def test_equal_effort_and_equal_lenses_collapses(self):
        flat = {"quick": None, "standard": "medium", "thorough": "medium"}
        plan = self._roster("product-intent", "architecture", "feasibility", "risk-governance")
        deliverable = self._roster("spec-conformance", "divergence-hunter", "usability")
        got = review.available_depths(self._protocol(), plan, deliverable, flat)
        self.assertEqual(got, ["quick", "standard"])   # thorough adds neither lenses nor effort -> collapsed

    def test_one_standard_table_lens_offers_all_three(self):
        # A single installed reviewer whose lens IS in the standard table: standard adds that lens over quick,
        # and thorough adds effort over standard (same lens-set, higher effort) -> all three are distinct.
        got = review.available_depths(self._protocol(), self._roster(),
                                      self._roster("spec-conformance"), self._EFFORTS)
        self.assertEqual(got, ["quick", "standard", "thorough"])

    def test_one_thorough_only_lens_skips_standard(self):
        # A single installed reviewer whose lens runs ONLY at thorough (security-governance is not in the
        # standard deliverable table): standard would run nothing the quick floor doesn't, so it collapses,
        # yet thorough still adds that lens -> the offer skips the middle depth entirely.
        got = review.available_depths(self._protocol(), self._roster(),
                                      self._roster("security-governance"), self._EFFORTS)
        self.assertEqual(got, ["quick", "thorough"])

    def test_non_monotonic_tables_still_offer_a_depth_with_unique_coverage(self):
        # Robustness (keyed on set-DIFFERENCE, not strict superset): if the per-depth lens tables are ever
        # non-monotonic — a lighter depth naming a lens a heavier depth's table omits — a depth that runs
        # genuinely unique coverage must still be OFFERED, never silently hidden (which would invert the
        # feature's purpose). Inert with the shipped monotonic tables; this guards a future table edit.
        protocol = {"plan_review": {"quick": ["risk-governance"], "standard": ["product-intent"], "thorough": []},
                    "deliverable_review": {"quick": [], "standard": [], "thorough": []}}
        roster = self._roster("risk-governance", "product-intent")
        got = review.available_depths(protocol, roster, self._roster(), self._EFFORTS)
        # standard runs product-intent, which quick (risk-governance) does not -> it must be offered, not hidden.
        self.assertIn("standard", got)
        self.assertEqual(got[0], "quick")   # quick is always the floor


if __name__ == "__main__":
    unittest.main()
