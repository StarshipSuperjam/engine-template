#!/usr/bin/env python3
"""Focused mechanical tests for the Build coordinator instrument panel."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_coordinator as bc  # noqa: E402

HEAD_A = "a" * 40
HEAD_B = "b" * 40
BASE = "0" * 40


def plan(objective="Ship a small instrument panel"):
    return {
        "schema_version": "build-plan.v1",
        "raw_intent": "Coordinate Build mechanics without replacing engineering judgment.",
        "interpretation": "Preserve exact plan and commit-bound evidence around a senior engineer's choices.",
        "evidence": [{"claim": "The runbook already requires proportional review.", "basis": ".engine/operations/build-orchestration.md", "kind": "observed"}],
        "assumptions": [{"claim": "The harness can reproduce this JSON document.", "status": "verified"}],
        "objective": objective,
        "success_obligations": [{"outcome": "A final commit is validated before submission.", "verification": "Validation receipt matches HEAD."}],
        "scope_boundary": ["Build workflow evidence"],
        "non_goals": ["Choosing reviewer remedies", "Merging the PR"],
        "risks": ["A missing session plan blocks cold recovery."],
        "implementation_outline": ["Bind", "Review", "Build", "Validate", "Submit"],
        "review_strategy": "Use the operator-approved depth and one proportional repair judgment.",
        "spec": {"posture": "none", "disclosure": "No settled spec; plan obligations remain the conformance referent."},
    }


class CoordinatorCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_path = str(Path(self.temp.name) / "build.json")
        self.store = bc.StateStore(self.state_path)
        self.plan_path = Path(self.temp.name) / "plan.json"
        self.write_plan(plan())

    def write_plan(self, value):
        self.plan_path.write_text(json.dumps(value), encoding="utf-8")

    def seed(self, source="session"):
        value = plan()
        state = bc._initial_state("owner/repo", 7, BASE, source, value, 11 if source == "issue" else None)
        self.store.create(state)
        return state

    def approve(self, depth="thorough"):
        args = argparse.Namespace(plan=str(self.plan_path), depth=depth)
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_approve(args, self.store)

    def state(self):
        return self.store.read()


class TestPlanAndSnapshot(CoordinatorCase):
    def test_bind_initializes_only_for_the_matching_draft_pr_head(self):
        args = argparse.Namespace(input=str(self.plan_path), source="session", repository="owner/repo", pr=7, issue=None)
        pr = {"number": 7, "state": "OPEN", "isDraft": True, "headRefOid": HEAD_A, "baseRefOid": BASE}
        with mock.patch.object(bc, "_verify_draft", return_value=pr), mock.patch.object(bc, "_head", return_value=HEAD_A), contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_plan_bind(args, self.store)
        self.assertEqual(self.state()["build"], {"repository": "owner/repo", "pr": 7, "base_at_bind": BASE})

    def test_bind_rejects_a_draft_pr_at_a_different_head(self):
        args = argparse.Namespace(input=str(self.plan_path), source="session", repository="owner/repo", pr=7, issue=None)
        with mock.patch.object(bc, "_verify_draft", return_value={"headRefOid": HEAD_B}), mock.patch.object(bc, "_head", return_value=HEAD_A), self.assertRaisesRegex(bc.CoordinatorError, "does not match"):
            bc.cmd_plan_bind(args, self.store)

    def test_plan_digest_is_canonical_and_exact_content_is_not_stored(self):
        value = plan()
        state = bc._initial_state("owner/repo", 7, BASE, "session", value, None)
        rendered = json.dumps(state)
        self.assertEqual(state["plan"]["digest"], bc._digest(value))
        self.assertNotIn(value["raw_intent"], rendered)
        self.assertNotIn(value["objective"], rendered)

    def test_mismatched_plan_is_rejected(self):
        self.seed()
        changed = plan("Do something else")
        with self.assertRaisesRegex(bc.CoordinatorError, "does not match"):
            bc._assert_plan(self.state(), changed)

    def test_atomic_snapshot_revision_and_compare_and_swap(self):
        self.seed()
        bc.StateStore(self.state_path, expected_revision=1).mutate(lambda s: s.update({"submission": "draft"}))
        self.assertEqual(self.state()["revision"], 2)
        with self.assertRaisesRegex(bc.CoordinatorError, "not expected"):
            bc.StateStore(self.state_path, expected_revision=1).mutate(lambda s: None)
        self.assertTrue(Path(self.state_path).exists())

    def test_snapshot_outside_os_temp_is_refused(self):
        with self.assertRaisesRegex(bc.CoordinatorError, "OS temporary"):
            bc.StateStore(str(bc.ROOT / "state.json"))

    def test_plan_revision_invalidates_approval_and_reviews_but_not_build_identity(self):
        self.seed(); self.approve()
        before = self.state()["build"]
        revised = plan("A genuinely changed outcome")
        bc._reset_after_revision(self.state(), revised)  # pure-shape smoke first
        self.write_plan(revised)
        args = argparse.Namespace(input=str(self.plan_path), ack_visibility=False)
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_plan_revise(args, self.store)
        state = self.state()
        self.assertEqual(state["build"], before)
        self.assertIsNone(state["approval"])
        self.assertIsNone(state["reviews"]["plan"]["packet_digest"])

    def test_unchanged_revision_preserves_evidence(self):
        self.seed(); self.approve()
        revision = self.state()["revision"]
        args = argparse.Namespace(input=str(self.plan_path), ack_visibility=False)
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_plan_revise(args, self.store)
        self.assertEqual(self.state()["revision"], revision)

    def test_changing_approved_depth_clears_review_evidence(self):
        self.seed(); self.approve("standard")
        self.store.mutate(lambda s: s["reviews"]["plan"].update({"packet_digest": "sha256:" + "1" * 64}))
        self.approve("thorough")
        self.assertIsNone(self.state()["reviews"]["plan"]["packet_digest"])


class TestIssueDurability(CoordinatorCase):
    def test_plan_promotion_preserves_human_body_and_verifies_write(self):
        self.seed()
        bodies = iter(["Human issue body\n", "Human issue body\n", None])
        written = {}

        def issue_body(repo, issue):
            value = next(bodies)
            return written.get("body") if value is None else value

        def must_run(argv, input_text=None):
            written["body"] = input_text
            return ""

        args = argparse.Namespace(input=str(self.plan_path), issue=11, ack_visibility=True)
        with mock.patch.object(bc, "_issue_body", side_effect=issue_body), mock.patch.object(bc, "_must_run", side_effect=must_run), contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_plan_promote(args, self.store)
        self.assertTrue(written["body"].startswith("Human issue body\n"))
        self.assertIn(bc.PLAN_BEGIN + bc._digest(plan()), written["body"])
        self.assertEqual(self.state()["plan"]["source"], "issue")

    def test_concurrent_issue_edit_aborts_before_write(self):
        self.seed()
        with mock.patch.object(bc, "_issue_body", side_effect=["first", "changed"]), mock.patch.object(bc, "_must_run") as write:
            with self.assertRaisesRegex(bc.CoordinatorError, "changed"):
                bc._publish_issue("owner/repo", 11, plan())
        write.assert_not_called()

    def test_deleted_durable_plan_blocks_restore(self):
        self.seed("issue")
        handoff = bc._handoff(self.state())
        handoff_path = Path(self.temp.name) / "handoff.json"
        handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
        restored = bc.StateStore(str(Path(self.temp.name) / "restored.json"))
        with mock.patch.object(bc, "_issue_body", return_value="human body only"), self.assertRaisesRegex(bc.CoordinatorError, "no unique"):
            bc.cmd_handoff_restore(argparse.Namespace(input=str(handoff_path)), restored)

    def test_legacy_handoff_requires_fresh_bind(self):
        path = Path(self.temp.name) / "legacy.json"
        path.write_text('{"schema_version":"build-receipt.v1"}', encoding="utf-8")
        with self.assertRaisesRegex(bc.CoordinatorError, "fresh plan bind"):
            bc.cmd_handoff_restore(argparse.Namespace(input=str(path)), bc.StateStore(str(Path(self.temp.name) / "new.json")))


class TestReviewAndFindings(CoordinatorCase):
    def setUp(self):
        super().setUp()
        self.seed(); self.approve("thorough")

    def packet(self, stage="plan", head=HEAD_A):
        args = argparse.Namespace(stage=stage, plan=str(self.plan_path), impact=None)
        out = io.StringIO()
        lenses = ["product-intent", "architecture", "feasibility", "risk-governance"] if stage == "plan" else ["spec-conformance", "divergence-hunter", "usability", "technical-integrity", "security-governance"]
        with mock.patch.object(bc, "_installed", return_value=lenses), mock.patch.object(bc, "_head", return_value=head), mock.patch.object(bc, "_base", return_value=BASE), contextlib.redirect_stdout(out):
            bc._packet(args, self.store)
        return json.loads(out.getvalue())

    def test_packet_contains_exact_plan_raw_intent_and_digest(self):
        packet = self.packet()
        self.assertEqual(packet["plan"], plan())
        self.assertEqual(packet["raw_intent"], plan()["raw_intent"])
        self.assertEqual(packet["plan_digest"], bc._digest(plan()))
        self.assertIn("protocol_digest", packet)

    def test_no_formal_plan_feature_is_needed(self):
        packet = self.packet()
        self.assertEqual(packet["schema_version"], "build-review-packet.v1")
        self.assertEqual(packet["plan"]["spec"]["posture"], "none")

    def test_review_receipt_inventory_drives_disposition_completeness(self):
        packet = self.packet()
        args = argparse.Namespace(stage="plan", lens="product-intent", packet_digest=packet["packet_digest"], finding=["PI-1"])
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_review_record(args, self.store)
        with mock.patch.object(bc, "_head", return_value=HEAD_A):
            result = bc._status(self.state())
        self.assertIn("finding disposition: PI-1", result["required_evidence"])

    def test_wrong_lens_disposition_does_not_satisfy_receipt(self):
        packet = self.packet()
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_review_record(argparse.Namespace(stage="plan", lens="product-intent", packet_digest=packet["packet_digest"], finding=["PI-1"]), self.store)
            bc.cmd_finding_record(argparse.Namespace(id="PI-1", stage="plan", lens="architecture", severity="nit", summary="Different finding", disposition="rejected", rationale="Not the declared finding.", blocks_this_pr=False, handoff_summary=None), self.store)
        self.assertEqual(bc._missing_findings(self.state()), ["PI-1"])

    def test_severity_does_not_choose_remedy_or_blocking_posture(self):
        packet = self.packet()
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_review_record(argparse.Namespace(stage="plan", lens="product-intent", packet_digest=packet["packet_digest"], finding=["PI-1"]), self.store)
            bc.cmd_finding_record(argparse.Namespace(id="PI-1", stage="plan", lens="product-intent", severity="blocking", summary="Reviewer concern", disposition="rejected", rationale="The evidence disproves it.", blocks_this_pr=False, handoff_summary=None), self.store)
        finding = self.state()["findings"][0]
        self.assertEqual(finding["severity"], "blocking")
        self.assertEqual(finding["disposition"], "rejected")
        self.assertFalse(finding["blocks_this_pr"])

    def test_partial_acceptance_keeps_bounded_remedy(self):
        packet = self.packet()
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_finding_record(argparse.Namespace(id="A-1", stage="plan", lens="architecture", severity="serious", summary="Concern", disposition="partially-accepted", rationale="Accept the failure case, reject the proposed new subsystem.", blocks_this_pr=False, handoff_summary="Bounded remedy chosen."), self.store)
        self.assertEqual(self.state()["findings"][0]["disposition"], "partially-accepted")


class TestValidationRepairAndStatus(CoordinatorCase):
    def setUp(self):
        super().setUp()
        self.seed(); self.approve("quick")
        state = self.state()
        state["reviews"]["plan"].update({"packet_digest": "sha256:" + "1" * 64, "required_lenses": [], "installed_lenses": [], "receipts": []})
        self.store.mutate(lambda s: s.update(state))

    def test_validation_records_every_result_against_head(self):
        commands = Path(self.temp.name) / "commands.json"
        commands.write_text(json.dumps([{"id": "one", "command": ["one"]}, {"id": "two", "command": ["two"]}]), encoding="utf-8")
        with mock.patch.object(bc, "_head", return_value=HEAD_A), mock.patch.object(bc, "_run", side_effect=[subprocess.CompletedProcess([], 0, "ok", ""), subprocess.CompletedProcess([], 0, "ok", "")]), contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_validate(argparse.Namespace(commands=str(commands)), self.store)
        self.assertEqual({r["commit"] for r in self.state()["validation"]["results"]}, {HEAD_A})

    def test_validation_becomes_stale_when_head_changes(self):
        self.store.mutate(lambda s: s.update({"validation": {"commit": HEAD_A, "results": [{"id": "ci", "commit": HEAD_A, "passed": True, "summary": "ok"}]}}))
        with mock.patch.object(bc, "_head", return_value=HEAD_B):
            status = bc._status(self.state())
        self.assertIn("green validation for the final commit", status["required_evidence"])

    def test_none_repair_judgment_is_valid_for_small_change(self):
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"packet_digest": "sha256:" + "2" * 64, "reviewed_commit": HEAD_A}))
        with mock.patch.object(bc, "_head", return_value=HEAD_B), mock.patch.object(bc, "_must_run", return_value="1 file changed, 1 insertion(+), 1 deletion(-)"), contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_repair_assess(argparse.Namespace(judgment="none", rationale="Direct verification covers the prescribed wording repair.", lens=None), self.store)
        self.assertEqual(self.state()["repair"]["judgment"], "none")

    def test_scoped_repair_requires_named_lens(self):
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"reviewed_commit": HEAD_A}))
        with mock.patch.object(bc, "_head", return_value=HEAD_B), mock.patch.object(bc, "_must_run", return_value="1 file changed"), self.assertRaisesRegex(bc.CoordinatorError, "at least one"):
            bc.cmd_repair_assess(argparse.Namespace(judgment="scoped", rationale="Logic changed.", lens=None), self.store)

    def test_repair_review_requires_validation_for_repaired_commit(self):
        self.store.mutate(lambda s: s.update({"repair": {"reviewed_commit": HEAD_A, "final_commit": HEAD_B, "summary": "1 file", "judgment": "scoped", "rationale": "Logic changed", "lenses": ["usability"], "packet_digest": None, "receipts": []}}))
        args = argparse.Namespace(stage="repair", plan=str(self.plan_path), impact=None)
        with mock.patch.object(bc, "_installed", return_value=["usability"]), self.assertRaisesRegex(bc.CoordinatorError, "green validation"):
            bc._packet(args, self.store)

    def test_non_aligned_checkpoint_prevents_ready_phase(self):
        self.store.mutate(lambda s: s.update({"checkpoint": {"plan_digest": s["plan"]["digest"], "objective": "x", "current_work": "x", "assumptions": [], "non_goals": [], "planned_scope": [], "changed_paths": [], "remaining_verification": [], "judgment": "operator_decision_required"}}))
        with mock.patch.object(bc, "_head", return_value=HEAD_A):
            status = bc._status(self.state())
        self.assertEqual(status["phase"], "engineering-decision")

    def test_implementation_status_offers_unordered_engineering_activities(self):
        with mock.patch.object(bc, "_head", return_value=HEAD_A):
            status = bc._status(self.state())
        self.assertEqual(status["phase"], "implementation")
        self.assertIsNone(status["suggested_next"])
        self.assertIn("continue implementation", status["available_activities"])


class TestPreflightHandoffAndSubmission(CoordinatorCase):
    def test_handoff_redacts_private_rationale(self):
        self.seed("issue")
        self.store.mutate(lambda s: s["findings"].append({"id": "F-1", "stage": "plan", "lens": "product-intent", "packet_digest": s["plan"]["digest"], "commit": None, "severity": "serious", "summary": "private detail", "disposition": "rejected", "rationale": "sensitive local basis", "blocks_this_pr": False, "handoff_summary": "Concern rejected because the durable evidence contradicted it."}))
        rendered = json.dumps(bc._handoff(self.state()))
        self.assertNotIn("sensitive local basis", rendered)
        self.assertNotIn("private detail", rendered)
        self.assertIn("durable evidence", rendered)

    def test_handoff_requires_summary_for_every_finding(self):
        self.seed("issue")
        self.store.mutate(lambda s: s["findings"].append({"id": "F-1", "stage": "plan", "lens": "x", "packet_digest": s["plan"]["digest"], "commit": None, "severity": "nit", "summary": "x", "disposition": "rejected", "rationale": "x", "blocks_this_pr": False, "handoff_summary": None}))
        with self.assertRaisesRegex(bc.CoordinatorError, "handoff-summary"):
            bc._handoff(self.state())

    def test_preflight_binds_contract_and_results_to_head(self):
        self.seed()
        args = argparse.Namespace(pr_body=None)
        pr = {"body": "complete", "baseRefOid": BASE}
        close = subprocess.CompletedProcess([], 0, json.dumps({"lines": [], "defang": None}), "")
        with mock.patch.object(bc, "_head", return_value=HEAD_A), mock.patch.object(bc, "_verify_draft", return_value=pr), mock.patch.object(bc, "_run", return_value=close), mock.patch.object(bc, "_pr_contract", return_value=(True, "complete")), contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_preflight(args, self.store)
        self.assertEqual(self.state()["pr_contract"], {"commit": HEAD_A, "complete": True})
        self.assertEqual({x["commit"] for x in self.state()["preflights"]}, {HEAD_A})

    def test_submit_apply_can_only_mark_ready(self):
        self.seed()
        preview = {"repository": "owner/repo", "pr": 7, "commit": HEAD_A, "action": "mark-ready", "merge": False}
        with mock.patch.object(bc, "_submit_preview", return_value=preview), mock.patch.object(bc, "_must_run", return_value="") as run, contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_submit_apply(argparse.Namespace(plan=str(self.plan_path)), self.store)
        argv = run.call_args.args[0]
        self.assertEqual(argv[:3], ["gh", "pr", "ready"])
        self.assertNotIn("merge", argv)

    def test_cli_has_no_merge_command(self):
        command_action = next(action for action in bc.parser()._actions if getattr(action, "choices", None))
        self.assertNotIn("merge", command_action.choices)


class TestHistoricalScenarioCorpus(unittest.TestCase):
    def test_six_stable_source_linked_scenarios_cover_recovery_behaviors(self):
        fixture = json.loads((bc.ROOT / ".engine" / "_fixtures" / "build-coordinator-scenarios" / "scenarios.v1.json").read_text())
        scenarios = fixture["scenarios"]
        self.assertEqual(len(scenarios), 6)
        self.assertEqual(len({x["id"] for x in scenarios}), 6)
        self.assertTrue(all(x["source"]["pull_request"] and x["source"]["memory_session"] for x in scenarios))
        expected = {item for scenario in scenarios for item in scenario["expected"]}
        forbidden = {item for scenario in scenarios for item in scenario["must_not"]}
        self.assertIn("none-judgment-is-valid", expected)
        self.assertIn("review-loop-terminates", expected)
        self.assertIn("recursive-audit", forbidden)
        self.assertIn("blindly-apply-reviewer-remedy", forbidden)

    def test_short_runbook_preserves_the_quality_and_authority_gates(self):
        text = (bc.ROOT / ".engine" / "operations" / "build-orchestration.md").read_text()
        self.assertLessEqual(len(text.split()), 3063)
        for phrase in ("operator-approved plan", "one cold plan review", "reviewed-to-final divergence",
                       "no automatic audit recursion", "operator alone merges"):
            self.assertIn(phrase, text)

    def test_every_reviewer_receives_the_exact_approved_plan(self):
        agents = list((bc.ROOT / ".claude" / "agents").glob("engine-design-review-*.md"))
        agents += list((bc.ROOT / ".claude" / "agents").glob("engine-qa-review-*.md"))
        self.assertEqual(len(agents), 9)
        for agent in agents:
            self.assertIn("exact operator-approved Build plan", agent.read_text(), agent.name)

    def test_no_spec_keeps_both_plan_derived_conformance_lenses(self):
        for name in ("engine-qa-review-spec-conformance.md", "engine-qa-review-divergence-hunter.md"):
            text = (bc.ROOT / ".claude" / "agents" / name).read_text()
            self.assertIn("no-spec is not a no-op" if "divergence" in name else "It is not a no-op", text)
            self.assertIn("operator-approved Build plan", text)


if __name__ == "__main__":
    unittest.main()
