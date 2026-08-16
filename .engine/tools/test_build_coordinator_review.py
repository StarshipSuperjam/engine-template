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

    def test_downgraded_blocking_finding_has_stable_operator_line(self):
        finding = {"id": "SEC-1", "severity": "blocking", "blocks_this_pr": False,
                   "operator_summary": "The public concern and rejection rationale.",
                   "private_reference": "private security note S-1"}
        line = review.disagreement_line(finding)
        self.assertIn("Reviewer disagreement `SEC-1`", line)
        self.assertIn("private security note S-1", line)

    def test_product_intent_challenges_no_spec_and_selected_document_judgment(self):
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


if __name__ == "__main__":
    unittest.main()
