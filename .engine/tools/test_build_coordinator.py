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
HEAD_C = "c" * 40
BASE = "0" * 40


def plan(objective="Ship a small instrument panel"):
    return {
        "schema_version": "build-plan.v1",
        "profile": "normal",
        "intent_source": {"kind": "direct"},
        "raw_intent": "Coordinate Build mechanics without replacing engineering judgment.",
        "interpretation": "Preserve exact plan and commit-bound evidence around a senior engineer's choices.",
        "evidence": [{"claim": "The runbook already requires proportional review.", "basis": ".engine/operations/build-orchestration.md", "kind": "observed"}],
        "assumptions": [{"claim": "The harness can reproduce this JSON document.", "status": "verified"}],
        "objective": objective,
        "success_obligations": [{"outcome": "A final commit is validated before submission.", "verification": "Validation receipt matches HEAD."}],
        "scope_boundary": ["Build workflow evidence"],
        "non_goals": ["Choosing reviewer remedies", "Merging the PR"],
        "risks": ["A missing session plan blocks cold recovery."],
        "work_items": [{"id": "W1", "description": "Build the instrument panel", "paths": [".engine/tools/build_coordinator.py"], "verification": ["Run focused coordinator tests"]}],
        "review_strategy": "Use the operator-approved depth and one proportional repair judgment.",
        "spec": {"posture": "none", "selection_basis": "No product specification governs this workflow-only change.", "disclosure": "No settled spec; plan obligations remain the conformance referent."},
    }


def _work_item_v2(node_id, deps, *, resources=None, executor="builder"):
    return {
        "id": node_id, "description": f"Build {node_id}",
        "paths": [f".engine/tools/{node_id}.py"], "verification": [f"Run {node_id} tests"],
        "depends_on": list(deps), "exclusive_resources": list(resources or []),
        "executor_class": executor,
        "output_contract": {"deliverable": f"{node_id} and its tests",
                            "artifact_kinds": ["worker-commit", "integrated-commit"],
                            "required_evidence": ["changed_paths", "verification_results"]},
    }


def plan_v2(objective="Ship a dependency-ordered Build", items=None, mode="serial", max_concurrency=1):
    if items is None:
        items = [_work_item_v2("shared", []), _work_item_v2("adapter", ["shared"])]
    return {
        "schema_version": "build-plan.v2",
        "profile": "normal",
        "intent_source": {"kind": "direct"},
        "raw_intent": "Coordinate Build work as a static implementation DAG.",
        "interpretation": "Derive readiness from a validated acyclic graph while keeping one integrator.",
        "evidence": [{"claim": "graphlib is stdlib.", "basis": "Python standard library", "kind": "observed"}],
        "assumptions": [{"claim": "The harness reproduces this JSON document.", "status": "verified"}],
        "objective": objective,
        "success_obligations": [{"outcome": "A cycle fails validation.", "verification": "TestPlanV2Ingest"}],
        "scope_boundary": ["Build workflow evidence"],
        "non_goals": ["A scheduler daemon"],
        "risks": ["Version dispatch could regress v1."],
        "work_items": items,
        "parallelism": {"mode": mode, "max_concurrency": max_concurrency},
        "review_strategy": "Operator-approved depth with one proportional repair judgment.",
        "spec": {"posture": "none", "selection_basis": "No product spec governs the coordinator.", "disclosure": "No settled spec; plan obligations are the referent."},
    }


class CoordinatorCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        stable = mock.patch.object(
            bc.core, "StableCommit",
            side_effect=lambda root, activity: contextlib.nullcontext(bc._head()),
        )
        stable.start()
        self.addCleanup(stable.stop)
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
    def test_plan_work_items_are_ordered_and_unique(self):
        value = plan()
        value["work_items"].append({**value["work_items"][0], "description": "duplicate"})
        self.write_plan(value)
        with self.assertRaisesRegex(bc.CoordinatorError, "must be unique"):
            bc._plan(str(self.plan_path))

    def test_bind_initializes_only_for_the_matching_draft_pr_head(self):
        args = argparse.Namespace(input=str(self.plan_path), source="session", repository="owner/repo", pr=7, issue=None)
        pr = {"number": 7, "state": "OPEN", "isDraft": True, "headRefOid": HEAD_A, "baseRefOid": BASE}
        with mock.patch.object(bc, "_verify_draft", return_value=pr), mock.patch.object(bc, "_head", return_value=HEAD_A), contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_plan_bind(args, self.store)
        self.assertEqual(self.state()["build"], {"repository": "owner/repo", "pr": 7, "base_at_bind": BASE, "mode": "same-session"})

    def test_unattended_bind_requires_durable_issue_plan(self):
        args = argparse.Namespace(input=str(self.plan_path), source="session", mode="unattended", repository="owner/repo", pr=7, issue=None)
        with self.assertRaisesRegex(bc.CoordinatorError, "durable Issue"):
            bc.cmd_plan_bind(args, self.store)

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

    def test_checkpoint_requires_approval(self):
        self.seed()
        note = {"objective": "x", "current_work": "x", "work_item": "W1", "assumptions": [],
                "non_goals": [], "planned_scope": [], "remaining_verification": [], "judgment": "aligned"}
        path = Path(self.temp.name) / "checkpoint.json"
        path.write_text(json.dumps(note))
        with self.assertRaisesRegex(bc.CoordinatorError, "not approved"):
            bc.cmd_checkpoint(argparse.Namespace(plan=str(self.plan_path), input=str(path), complete_item=None), self.store)

    def test_trivial_profile_preserves_one_glance_floor(self):
        value = plan(); value["profile"] = "trivial"
        state = bc._initial_state("owner/repo", 7, BASE, "session", value, None)
        state["approval"] = {"plan_digest": state["plan"]["digest"], "spec_digest": None, "depth": "quick"}
        with mock.patch.object(bc, "_head", return_value=HEAD_A), mock.patch.object(bc, "_changed_paths", return_value=[]), mock.patch.object(bc, "_must_run", return_value="1"):
            status = bc._status(state, value)
        self.assertNotIn("plan-review packet", status["required_evidence"])
        self.assertNotIn("deliverable-review packet", status["required_evidence"])

    def test_trivial_plan_uses_the_reduced_document_shape(self):
        value = {
            "schema_version": "build-plan.v1", "profile": "trivial",
            "intent_source": {"kind": "direct"}, "raw_intent": "Correct one typo.",
            "objective": "Correct the typo.",
            "success_obligations": [{"outcome": "Text is correct.", "verification": "Read it."}],
            "work_items": [{"id": "W1", "description": "Correct it", "paths": ["README.md"],
                            "verification": ["Read the changed line"]}],
            "spec": {"posture": "none", "selection_basis": "Copy-only change.",
                     "disclosure": "No settled specification applies."},
        }
        self.write_plan(value)
        self.assertEqual(bc._plan(str(self.plan_path))["profile"], "trivial")
        value["profile"] = "normal"
        self.write_plan(value)
        with self.assertRaisesRegex(bc.CoordinatorError, "interpretation.*required"):
            bc._plan(str(self.plan_path))

    def test_trivial_guarded_change_requires_normal_promotion(self):
        value = plan(); value["profile"] = "trivial"
        state = bc._initial_state("owner/repo", 7, BASE, "session", value, None)
        state["approval"] = {"plan_digest": state["plan"]["digest"], "spec_digest": None, "depth": "quick"}
        with mock.patch.object(bc, "_head", return_value=HEAD_A), mock.patch.object(bc, "_changed_paths", return_value=[".engine/schemas/x.json"]), mock.patch.object(bc, "_must_run", return_value="1"):
            status = bc._status(state, value)
        self.assertTrue(any("promote the trivial Build" in item for item in status["engineering_judgment"]))

    def test_trivial_uses_the_canonical_guarded_file_classifier(self):
        value = plan(); value["profile"] = "trivial"
        state = bc._initial_state("owner/repo", 7, BASE, "session", value, None)
        state["approval"] = {"plan_digest": state["plan"]["digest"], "spec_digest": None, "depth": "quick"}
        guarded = [".engine/uv.lock", ".engine/suites.json", ".claude/settings.json",
                   ".codex/hooks.json", ".github/dependabot.yml", ".engine/schemas/new.json"]
        for path in guarded:
            with self.subTest(path=path), mock.patch.object(bc, "_head", return_value=HEAD_A), \
                    mock.patch.object(bc, "_changed_paths", return_value=[path]), \
                    mock.patch.object(bc, "_must_run", return_value="1"):
                status = bc._status(state, value)
                self.assertTrue(any("promote the trivial Build" in item for item in status["engineering_judgment"]))

    def test_trivial_cannot_promote_to_cold_continuation(self):
        value = plan(); value["profile"] = "trivial"; self.write_plan(value)
        self.store.create(bc._initial_state("owner/repo", 7, BASE, "session", value, None))
        with self.assertRaisesRegex(bc.CoordinatorError, "normal profile"):
            bc.cmd_plan_promote(argparse.Namespace(input=str(self.plan_path), issue=11, ack_visibility=True), self.store)

    def test_same_session_status_survives_github_loss(self):
        value = plan(); value["intent_source"] = {"kind": "issue", "issue": 770}
        state = bc._initial_state("owner/repo", 7, BASE, "session", value, None)
        state["approval"] = {"plan_digest": state["plan"]["digest"], "spec_digest": None, "depth": "quick"}
        with mock.patch.object(bc, "_issue_body", side_effect=bc.CoordinatorError("offline")), mock.patch.object(bc, "_head", return_value=HEAD_A):
            status = bc._status(state, value)
        self.assertIn("phase", status)


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
        with mock.patch.object(bc.github, "issue_body", side_effect=lambda root, repo, issue: issue_body(repo, issue)), \
                mock.patch.object(bc.github.core, "must_run", side_effect=lambda argv, root, input_value=None: must_run(argv, input_value)), \
                mock.patch.object(bc, "_ensure_pr_closes_issue"), contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_plan_promote(args, self.store)
        self.assertTrue(written["body"].startswith("Human issue body\n"))
        self.assertIn(bc.PLAN_BEGIN + bc._digest(plan()), written["body"])
        self.assertEqual(self.state()["plan"]["source"], "issue")

    def test_concurrent_issue_edit_aborts_before_write(self):
        self.seed()
        with mock.patch.object(bc.github, "issue_body", side_effect=["first", "changed"]), mock.patch.object(bc.github.core, "must_run") as write:
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
            with mock.patch.object(bc.repo_identity, "origin_slug", return_value="owner/repo"), \
                    mock.patch.object(bc.github, "pr_state", return_value={"number": 7, "state": "OPEN", "headRefOid": bc._head()}):
                bc.cmd_handoff_restore(argparse.Namespace(input=str(handoff_path)), restored)

    def test_legacy_handoff_requires_fresh_bind(self):
        path = Path(self.temp.name) / "legacy.json"
        path.write_text('{"schema_version":"build-receipt.v1"}', encoding="utf-8")
        with self.assertRaisesRegex(bc.CoordinatorError, "fresh plan bind"):
            bc.cmd_handoff_restore(argparse.Namespace(input=str(path)), bc.StateStore(str(Path(self.temp.name) / "new.json")))

    def test_dedicated_build_issue_is_authored_and_then_receives_the_plan(self):
        self.seed()
        args = argparse.Namespace(input=str(self.plan_path), issue=None, create_issue="Durable coordinator work", ack_visibility=True)
        with mock.patch.object(bc, "_create_build_issue", return_value=42) as create, \
                mock.patch.object(bc, "_ensure_pr_closes_issue") as link, contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_plan_promote(args, self.store)
        call = create.call_args.args
        self.assertEqual(call[:4], ("owner/repo", 7, "Durable coordinator work", plan()))
        self.assertRegex(call[4], "^[0-9a-f]{32}$")
        link.assert_called_once_with("owner/repo", 7, 42)
        self.assertEqual(self.state()["plan"]["durable_issue"], 42)

    def test_durable_build_issue_is_linked_to_close_with_the_pr(self):
        bodies = iter(["PR body", "PR body", "PR body\n\nCloses #42\n"])
        written = {}
        def draft(repo, pr):
            return {"body": next(bodies)}
        def write(argv, input_text=None):
            written["body"] = input_text
            return ""
        with mock.patch.object(bc.github, "verify_draft", side_effect=lambda root, repo, pr: draft(repo, pr)), \
                mock.patch.object(bc.github.core, "must_run", side_effect=lambda argv, root, input_value=None: write(argv, input_value)):
            bc._ensure_pr_closes_issue("owner/repo", 7, 42)
        self.assertEqual(written["body"], "PR body\n\nCloses #42\n")


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

    def receipt_args(self, packet, lens, findings):
        contract = next(item for item in packet["reviewer_contracts"] if item["lens"] == lens)
        return argparse.Namespace(stage=packet["stage"], lens=lens,
                                  packet_digest=packet["packet_digest"],
                                  lens_packet_digest=contract["lens_packet_digest"], finding=findings)

    def test_packet_contains_exact_plan_raw_intent_and_digest(self):
        packet = self.packet()
        self.assertEqual(packet["plan"], plan())
        self.assertEqual(packet["raw_intent"], plan()["raw_intent"])
        self.assertEqual(packet["plan_digest"], bc._digest(plan()))
        self.assertIn("protocol_digest", packet)

    def test_plan_packet_contains_exact_referents(self):
        packet = self.packet()
        self.assertEqual((packet["raw_intent"], packet["plan"]), (plan()["raw_intent"], plan()))

    def test_thorough_requires_every_installed_lens(self):
        packet = self.packet()
        self.assertEqual(set(packet["required_lenses"]), set(packet["installed_lenses"]))

    def test_deliverable_packet_requires_green_validation(self):
        args = argparse.Namespace(stage="deliverable", plan=str(self.plan_path), impact=None)
        with mock.patch.object(bc, "_installed", return_value=["spec-conformance"]), mock.patch.object(bc, "_head", return_value=HEAD_A), self.assertRaisesRegex(bc.CoordinatorError, "green validation"):
            bc._packet(args, self.store)

    def test_deliverable_packet_records_reviewed_commit(self):
        self.store.mutate(lambda s: s.update({"validation": {"commit": HEAD_A, "results": [{"id": "ci", "commit": HEAD_A, "passed": True, "summary": "ok"}]}}))
        packet = self.packet("deliverable", HEAD_A)
        self.assertEqual(packet["commit"], HEAD_A)
        self.assertEqual(self.state()["reviews"]["deliverable"]["reviewed_commit"], HEAD_A)

    def test_failed_final_stability_check_does_not_record_review_packet(self):
        self.store.mutate(lambda s: s.update({"validation": {"commit": HEAD_A, "results": [
            {"id": "ci", "commit": HEAD_A, "passed": True, "summary": "ok"}]}}))
        class ChangedAfter:
            def __enter__(self): return HEAD_A
            def __exit__(self, *unused): raise bc.CoordinatorError("changed after packet")
        args = argparse.Namespace(stage="deliverable", plan=str(self.plan_path), impact=None,
                                  output=None, json=True)
        with mock.patch.object(bc.core, "StableCommit", return_value=ChangedAfter()), \
                mock.patch.object(bc, "_installed", return_value=["spec-conformance"]), \
                mock.patch.object(bc, "_head", return_value=HEAD_A), \
                mock.patch.object(bc, "_base", return_value=BASE), \
                self.assertRaisesRegex(bc.CoordinatorError, "changed after"):
            bc._packet(args, self.store)
        self.assertIsNone(self.state()["reviews"]["deliverable"]["packet_digest"])

    def test_hard_check_carveouts_reach_deliverable_packet(self):
        self.store.mutate(lambda s: s.update({"validation": {"commit": HEAD_A, "results": [{"id": "ci", "commit": HEAD_A, "passed": True, "summary": "ok"}]}}))
        packet = self.packet("deliverable", HEAD_A)
        self.assertIn("hard_check_declarations", packet)

    def test_no_formal_plan_feature_is_needed(self):
        packet = self.packet()
        self.assertEqual(packet["schema_version"], "build-review-packet.v1")
        self.assertEqual(packet["plan"]["spec"]["posture"], "none")

    def test_operator_can_waive_only_retrospective_plan_review(self):
        adopted = self.state()["plan"]["bound_head"]
        changed = subprocess.CompletedProcess([], 1, "", "")
        with mock.patch.object(bc, "_head", return_value=adopted), mock.patch.object(bc, "_run", return_value=changed), contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_review_waive(argparse.Namespace(stage="plan", reason="Implementation preceded the coordinator.", adopted_commit=adopted), self.store)
        state = self.state()
        self.assertEqual(state["reviews"]["plan"]["waiver"]["plan_digest"], state["plan"]["digest"])
        with self.assertRaisesRegex(bc.CoordinatorError, "only retrospective plan review"):
            bc.cmd_review_waive(argparse.Namespace(stage="deliverable", reason="not allowed", adopted_commit=adopted), self.store)

    def test_plan_review_waiver_cannot_erase_started_review(self):
        self.packet()
        with self.assertRaisesRegex(bc.CoordinatorError, "already started"):
            bc.cmd_review_waive(argparse.Namespace(stage="plan", reason="too late", adopted_commit=self.state()["plan"]["bound_head"]), self.store)

    def test_standalone_packet_needs_no_pr_or_snapshot(self):
        args = argparse.Namespace(stage="deliverable", plan=str(self.plan_path), impact=None, standalone=True,
                                  repository="owner/repo", commit=HEAD_A, base=BASE, depth="thorough",
                                  output=None, json=False)
        output = io.StringIO()
        with mock.patch.object(bc, "_installed", return_value=["spec-conformance"]), \
                mock.patch.object(bc, "_hard_check_declarations", return_value=[]), \
                mock.patch.object(bc, "_head", return_value=HEAD_A), \
                mock.patch.object(bc.core, "run", return_value=subprocess.CompletedProcess([], 0, "", "")), \
                contextlib.redirect_stdout(output):
            bc._packet(args, None)
        self.assertIn("review packet sha256:", output.getvalue())
        self.assertNotIn('"raw_intent"', output.getvalue())

    def test_default_packet_output_is_concise_and_json_is_explicit(self):
        args = argparse.Namespace(stage="plan", plan=str(self.plan_path), impact=None, standalone=False,
                                  output=None, json=False)
        concise = io.StringIO()
        with mock.patch.object(bc, "_installed", return_value=[]), contextlib.redirect_stdout(concise):
            bc._packet(args, self.store)
        self.assertEqual(len(concise.getvalue().splitlines()), 1)
        args.json = True
        verbose = io.StringIO()
        with mock.patch.object(bc, "_installed", return_value=[]), contextlib.redirect_stdout(verbose):
            bc._packet(args, self.store)
        self.assertIn('"raw_intent"', verbose.getvalue())

    def test_retrying_identical_packet_preserves_receipts_and_findings(self):
        packet = self.packet()
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_review_record(self.receipt_args(packet, "product-intent", ["PI-1"]), self.store)
            bc.cmd_finding_record(argparse.Namespace(id="PI-1", stage="plan", lens="product-intent", severity="nit", summary="Concern", disposition="rejected", rationale="Evidence disproves it.", escalation_kind=None, blocks_this_pr=False, handoff_summary=None), self.store)
        before = self.state()
        retried = self.packet()
        after = self.state()
        self.assertEqual(retried["packet_digest"], packet["packet_digest"])
        self.assertEqual(after["reviews"]["plan"]["receipts"], before["reviews"]["plan"]["receipts"])
        self.assertEqual(after["findings"], before["findings"])

    def test_retrying_deliverable_packet_ignores_random_artifact_transport_path(self):
        self.store.mutate(lambda s: s.update({"validation": {"commit": HEAD_A, "results": [
            {"id": "ci", "commit": HEAD_A, "passed": True, "summary": "ok"}]}}))
        first = self.packet("deliverable", HEAD_A)
        second = self.packet("deliverable", HEAD_A)
        self.assertEqual(first["packet_digest"], second["packet_digest"])
        self.assertEqual(first["referent_digest"], second["referent_digest"])
        self.assertNotEqual(first["artifacts"]["hard_check_declarations"]["path"],
                            second["artifacts"]["hard_check_declarations"]["path"])

    def test_review_receipt_must_attest_the_lens_packet_contract(self):
        packet = self.packet()
        args = self.receipt_args(packet, "product-intent", [])
        args.lens_packet_digest = "sha256:" + "f" * 64
        with self.assertRaisesRegex(bc.CoordinatorError, "attest"):
            bc.cmd_review_record(args, self.store)

    def test_review_receipt_inventory_drives_disposition_completeness(self):
        packet = self.packet()
        args = self.receipt_args(packet, "product-intent", ["PI-1"])
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_review_record(args, self.store)
        with mock.patch.object(bc, "_head", return_value=HEAD_A):
            result = bc._status(self.state())
        self.assertIn("finding disposition: PI-1", result["required_evidence"])

    def test_wrong_lens_disposition_does_not_satisfy_receipt(self):
        packet = self.packet()
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_review_record(self.receipt_args(packet, "product-intent", ["PI-1"]), self.store)
            bc.cmd_finding_record(argparse.Namespace(id="PI-1", stage="plan", lens="architecture", severity="nit", summary="Different finding", disposition="rejected", rationale="Not the declared finding.", escalation_kind=None, blocks_this_pr=False, handoff_summary=None), self.store)
        self.assertEqual(bc._missing_findings(self.state()), ["PI-1"])

    def test_severity_does_not_choose_remedy_or_blocking_posture(self):
        packet = self.packet()
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_review_record(self.receipt_args(packet, "product-intent", ["PI-1"]), self.store)
            bc.cmd_finding_record(argparse.Namespace(id="PI-1", stage="plan", lens="product-intent", severity="blocking", summary="Reviewer concern", disposition="rejected", rationale="The evidence disproves it.", escalation_kind=None, blocks_this_pr=False, handoff_summary=None, operator_summary="The concern was rejected because the cited evidence does not support it.", private_reference=None), self.store)
        finding = self.state()["findings"][0]
        self.assertEqual(finding["severity"], "blocking")
        self.assertEqual(finding["disposition"], "rejected")
        self.assertFalse(finding["blocks_this_pr"])

    def test_partial_acceptance_keeps_bounded_remedy(self):
        packet = self.packet()
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_finding_record(argparse.Namespace(id="A-1", stage="plan", lens="architecture", severity="serious", summary="Concern", disposition="partially-accepted", rationale="Accept the failure case, reject the proposed new subsystem.", escalation_kind=None, blocks_this_pr=False, handoff_summary="Bounded remedy chosen."), self.store)
        self.assertEqual(self.state()["findings"][0]["disposition"], "partially-accepted")

    def test_escalation_names_an_operator_owned_boundary(self):
        self.packet()
        args = argparse.Namespace(id="A-2", stage="plan", lens="architecture", severity="serious",
                                  summary="Boundary", disposition="escalated", rationale="Changes authority.",
                                  escalation_kind=None, blocks_this_pr=True, handoff_summary=None)
        with self.assertRaisesRegex(bc.CoordinatorError, "operator-owned"):
            bc.cmd_finding_record(args, self.store)

    def test_engineering_blocker_remains_orchestrator_work(self):
        packet = self.packet()
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_finding_record(argparse.Namespace(id="A-3", stage="plan", lens="architecture", severity="blocking",
                summary="Engineering repair", disposition="accepted-fixed", rationale="Repair stays in approved design.",
                escalation_kind=None, blocks_this_pr=True, handoff_summary=None), self.store)
        with mock.patch.object(bc, "_head", return_value=HEAD_A):
            status = bc._status(self.state())
        self.assertTrue(any("resolve" in item for item in status["engineering_judgment"]))
        self.assertFalse(any("operator decision" in item for item in status["engineering_judgment"]))


class TestValidationRepairAndStatus(CoordinatorCase):
    def setUp(self):
        super().setUp()
        self.seed(); self.approve("quick")
        state = self.state()
        state["reviews"]["plan"].update({"packet_digest": "sha256:" + "1" * 64, "required_lenses": [], "installed_lenses": [], "receipts": []})
        self.store.mutate(lambda s: s.update(state))

    def test_validation_records_every_result_against_head(self):
        def validation(command, path):
            path.write_text("complete validation output\n", encoding="utf-8")
            return 0
        with mock.patch.object(bc, "_head", return_value=HEAD_A), mock.patch.object(bc, "_run_validation", side_effect=validation), contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_validate(argparse.Namespace(), self.store)
        self.assertEqual({r["commit"] for r in self.state()["validation"]["results"]}, {HEAD_A})
        self.assertTrue(all(Path(r["log_path"]).read_text() == "complete validation output\n" for r in self.state()["validation"]["results"]))

    def test_validation_runs_only_registered_commands(self):
        seen = []
        def validation(command, path):
            seen.append(command); path.write_text("ok\n"); return 0
        with mock.patch.object(bc, "_head", return_value=HEAD_A), mock.patch.object(bc, "_run_validation", side_effect=validation), contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_validate(argparse.Namespace(), self.store)
        self.assertEqual(seen, [item["command"] for item in bc.VALIDATION_COMMANDS])

    def test_validation_preserves_complete_logs(self):
        payload = "x" * 5000
        def validation(command, path):
            path.write_text(payload); return 0
        with mock.patch.object(bc, "_head", return_value=HEAD_A), mock.patch.object(bc, "_run_validation", side_effect=validation), contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_validate(argparse.Namespace(), self.store)
        for result in self.state()["validation"]["results"]:
            self.assertEqual(Path(result["log_path"]).read_text(), payload)
            self.assertEqual(result["log_digest"], bc._digest(payload.encode()))

    def test_validation_log_is_exclusive_and_owner_only(self):
        path = Path(self.temp.name) / "validation.log"
        self.assertEqual(bc._run_validation(["/usr/bin/true"], path), 0)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        link = Path(self.temp.name) / "link.log"; target = Path(self.temp.name) / "target"
        target.write_text("unchanged"); link.symlink_to(target)
        with self.assertRaisesRegex(bc.CoordinatorError, "private validation log"):
            bc._run_validation(["true"], link)
        self.assertEqual(target.read_text(), "unchanged")

    def test_validate_cli_has_no_arbitrary_command_input(self):
        parsed = bc.parser().parse_args(["--state", self.state_path, "validate"])
        self.assertFalse(hasattr(parsed, "commands"))

    def test_validation_becomes_stale_when_head_changes(self):
        self.store.mutate(lambda s: s.update({"validation": {"commit": HEAD_A, "results": [{"id": "ci", "commit": HEAD_A, "passed": True, "summary": "ok"}]}}))
        with mock.patch.object(bc, "_head", return_value=HEAD_B):
            status = bc._status(self.state())
        self.assertIn("green validation for the final commit", status["required_evidence"])

    def test_status_requires_validation_for_current_head(self):
        self.test_validation_becomes_stale_when_head_changes()

    def test_none_repair_judgment_is_valid_for_small_change(self):
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"packet_digest": "sha256:" + "2" * 64, "reviewed_commit": HEAD_A}))
        with mock.patch.object(bc, "_head", return_value=HEAD_B), mock.patch.object(bc, "_must_run", return_value="1 file changed, 1 insertion(+), 1 deletion(-)"), contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_repair_assess(argparse.Namespace(judgment="none", rationale="Direct verification covers the prescribed wording repair.", lens=None), self.store)
        self.assertEqual(self.state()["repair"]["judgment"], "none")

    def test_repair_assessment_records_diff_and_judgment(self):
        self.test_none_repair_judgment_is_valid_for_small_change()

    def test_scoped_repair_requires_named_lens(self):
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"reviewed_commit": HEAD_A}))
        with mock.patch.object(bc, "_head", return_value=HEAD_B), mock.patch.object(bc, "_must_run", return_value="1 file changed"), self.assertRaisesRegex(bc.CoordinatorError, "at least one"):
            bc.cmd_repair_assess(argparse.Namespace(judgment="scoped", rationale="Logic changed.", lens=None), self.store)

    def test_repair_review_requires_validation_for_repaired_commit(self):
        self.store.mutate(lambda s: s.update({"repair": {"reviewed_commit": HEAD_A, "final_commit": HEAD_B, "summary": "1 file", "judgment": "scoped", "rationale": "Logic changed", "lenses": ["usability"], "packet_digest": None, "receipts": []}}))
        args = argparse.Namespace(stage="repair", plan=str(self.plan_path), impact=None)
        with mock.patch.object(bc, "_installed", return_value=["usability"]), self.assertRaisesRegex(bc.CoordinatorError, "green validation"):
            bc._packet(args, self.store)

    def test_repair_findings_are_dispositioned_in_the_repair_stage(self):
        self.store.mutate(lambda s: s.update({
            "validation": {"commit": HEAD_B, "results": [{"id": "ci", "commit": HEAD_B, "passed": True, "summary": "ok"}]},
            "repair": {"reviewed_commit": HEAD_A, "final_commit": HEAD_B, "summary": "1 file", "judgment": "scoped", "rationale": "Logic changed", "lenses": ["usability"], "packet_digest": None, "receipts": []},
        }))
        args = argparse.Namespace(stage="repair", plan=str(self.plan_path), impact=None)
        output = io.StringIO()
        with mock.patch.object(bc, "_installed", return_value=["usability"]), mock.patch.object(bc, "_head", return_value=HEAD_B), mock.patch.object(bc, "_base", return_value=BASE), contextlib.redirect_stdout(output):
            bc._packet(args, self.store)
        packet = json.loads(output.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            contract = next(item for item in packet["reviewer_contracts"] if item["lens"] == "usability")
            bc.cmd_review_record(argparse.Namespace(stage="repair", lens="usability",
                                                    packet_digest=packet["packet_digest"],
                                                    lens_packet_digest=contract["lens_packet_digest"],
                                                    finding=["R-1"]), self.store)
            bc.cmd_finding_record(argparse.Namespace(id="R-1", stage="repair", lens="usability", severity="serious", summary="Repair concern", disposition="accepted-fixed", rationale="Directly fixed.", escalation_kind=None, blocks_this_pr=False, handoff_summary=None), self.store)
        self.assertEqual(bc._missing_findings(self.state()), [])
        self.assertEqual(self.state()["reviews"]["deliverable"]["reviewed_commit"], HEAD_B)
        self.assertEqual(self.state()["reviews"]["deliverable"]["receipts"][0]["packet_digest"], packet["packet_digest"])
        self.assertEqual(bc.review.missing_receipts(self.state()["reviews"]["deliverable"]), [])

    def test_failed_final_stability_check_does_not_record_validation(self):
        class ChangedAfter:
            def __enter__(self): return HEAD_A
            def __exit__(self, *unused): raise bc.CoordinatorError("changed after validation")
        def validation(command, path):
            path.write_text("ok\n"); return 0
        with mock.patch.object(bc.core, "StableCommit", return_value=ChangedAfter()), \
                mock.patch.object(bc, "_run_validation", side_effect=validation), \
                self.assertRaisesRegex(bc.CoordinatorError, "changed after"), \
                contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_validate(argparse.Namespace(), self.store)
        self.assertIsNone(self.state()["validation"])

    def test_prescribed_change_after_re_review_uses_latest_reviewed_commit(self):
        def completed_re_review(state):
            state["reviews"]["deliverable"]["reviewed_commit"] = HEAD_A
            state["repair"] = {"reviewed_commit": HEAD_A, "final_commit": HEAD_B, "summary": "material repair", "judgment": "scoped", "rationale": "Usability changed", "lenses": ["usability"], "packet_digest": "sha256:" + "3" * 64,
                               "receipts": [{"lens": "usability", "packet_digest": "sha256:" + "3" * 64, "commit": HEAD_B, "finding_ids": []}]}
        self.store.mutate(completed_re_review)
        with mock.patch.object(bc, "_head", return_value=HEAD_C), mock.patch.object(bc, "_must_run", return_value="1 file changed, 1 insertion(+)"), contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_repair_assess(argparse.Namespace(judgment="none", rationale="Direct verification covers the prescribed repair.", lens=None), self.store)
        self.assertEqual(self.state()["repair"]["reviewed_commit"], HEAD_B)

    def test_none_after_completed_scoped_review_terminates_the_chain(self):
        def reviewed(state):
            state["reviews"]["deliverable"].update({"packet_digest": "sha256:" + "2" * 64,
                "reviewed_commit": HEAD_B, "required_lenses": [], "installed_lenses": [], "receipts": []})
            state["validation"] = {"commit": HEAD_C, "results": [{"id": "ci", "commit": HEAD_C, "passed": True, "summary": "ok"}]}
            state["repair"] = {"reviewed_commit": HEAD_B, "final_commit": HEAD_C, "summary": "small repair",
                "judgment": "none", "rationale": "Direct verification is sufficient.", "lenses": [],
                "packet_digest": None, "receipts": []}
        self.store.mutate(reviewed)
        with mock.patch.object(bc, "_head", return_value=HEAD_C), mock.patch.object(bc, "_installed", return_value=[]):
            status = bc._status(self.state())
        self.assertNotIn("choose none, scoped, or full re-review", status["engineering_judgment"])

    def test_non_aligned_checkpoint_prevents_ready_phase(self):
        self.store.mutate(lambda s: s.update({"checkpoint": {"plan_digest": s["plan"]["digest"], "objective": "x", "current_work": "x", "work_item": "W1", "assumptions": [], "non_goals": [], "planned_scope": [], "changed_paths": [], "remaining_verification": [], "judgment": "operator_decision_required", "progress": "0 of 1 planned work items complete"}}))
        with mock.patch.object(bc, "_head", return_value=HEAD_A):
            status = bc._status(self.state())
        self.assertEqual(status["phase"], "engineering-decision")

    def test_implementation_status_offers_unordered_engineering_activities(self):
        with mock.patch.object(bc, "_head", return_value=HEAD_A):
            status = bc._status(self.state())
        self.assertEqual(status["phase"], "implementation")
        self.assertIsNone(status["suggested_next"])
        self.assertIn("continue implementation", status["available_activities"])

    def test_status_surfaces_unresolved_and_accepted_plan_assumptions(self):
        value = plan()
        value["assumptions"] = [
            {"claim": "API behavior is unknown", "status": "unresolved"},
            {"claim": "A rare timeout is acceptable", "status": "accepted-risk"},
        ]
        def update_plan(state):
            state["plan"]["digest"] = bc._digest(value)
            state["approval"]["plan_digest"] = bc._digest(value)
        self.store.mutate(update_plan)
        with mock.patch.object(bc, "_head", return_value=HEAD_A):
            status = bc._status(self.state(), value)
        self.assertEqual(status["phase"], "engineering-decision")
        self.assertIn("investigate unresolved assumption: API behavior is unknown", status["engineering_judgment"])
        self.assertIn("accepted plan risk: A rare timeout is acceptable", status["warnings"])

    def test_routine_progress_does_not_change_plan_digest(self):
        before = self.state()["plan"]["digest"]
        note = {"objective": "x", "current_work": "x", "work_item": "W1", "assumptions": [],
                "non_goals": [], "planned_scope": [".engine/tools/build_coordinator.py"],
                "remaining_verification": ["tests"], "judgment": "aligned"}
        path = Path(self.temp.name) / "checkpoint.json"; path.write_text(json.dumps(note))
        with mock.patch.object(bc, "_head", return_value=HEAD_A), mock.patch.object(bc, "_changed_paths", return_value=[]), contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_checkpoint(argparse.Namespace(plan=str(self.plan_path), input=str(path), complete_item="W1"), self.store)
        self.assertEqual(self.state()["plan"]["digest"], before)
        self.assertEqual(self.state()["progress"]["completed"], [{"id": "W1", "commit": HEAD_A}])

    def test_routine_enforces_order_and_reports_n_of_m(self):
        value = plan(); value["profile"] = "routine"; value["intent_source"] = {"kind": "issue", "issue": 11}
        value["work_items"].append({"id": "W2", "description": "Second", "paths": ["README.md"],
                                    "verification": ["Read it"]})
        self.write_plan(value)
        state = bc._initial_state("owner/repo", 7, BASE, "issue", value, 11, "unattended")
        state["approval"] = {"plan_digest": state["plan"]["digest"], "spec_digest": None, "depth": "quick"}
        state["reviews"]["plan"].update({"packet_digest": "sha256:" + "1" * 64,
                                           "referent_digest": "sha256:" + "2" * 64})
        self.store = bc.StateStore(str(Path(self.temp.name) / "routine.json")); self.store.create(state)
        note = {"objective": "x", "current_work": "second", "work_item": "W2", "assumptions": [],
                "non_goals": [], "planned_scope": ["README.md"], "remaining_verification": [], "judgment": "aligned"}
        note_path = Path(self.temp.name) / "routine-note.json"; note_path.write_text(json.dumps(note))
        with mock.patch.object(bc, "_assert_spec_boundary", return_value={}), self.assertRaisesRegex(bc.CoordinatorError, "next incomplete work item W1"):
            bc.cmd_checkpoint(argparse.Namespace(plan=str(self.plan_path), input=str(note_path), complete_item="W2", json=False), self.store)
        with mock.patch.object(bc, "_head", return_value=HEAD_A), mock.patch.object(bc, "_installed", return_value=[]):
            status = bc._status(self.state(), value)
        self.assertEqual(status["progress"], {"completed": [], "total": 2, "current": None, "next": "W1"})


class TestPreflightHandoffAndSubmission(CoordinatorCase):
    def test_handoff_redacts_private_rationale(self):
        self.seed("issue")
        def add_private(state):
            state["findings"].append({"id": "F-1", "stage": "plan", "lens": "product-intent", "packet_digest": state["plan"]["digest"], "commit": None, "severity": "serious", "summary": "private detail", "disposition": "rejected", "rationale": "sensitive local basis", "escalation_kind": None, "blocks_this_pr": False, "handoff_summary": "Concern rejected because the durable evidence contradicted it."})
            state["validation"] = {"commit": HEAD_A, "results": [{"id": "ci", "commit": HEAD_A, "passed": True, "summary": "token=/private/path"}]}
            state["repair"] = {"reviewed_commit": HEAD_A, "final_commit": HEAD_A, "summary": "no textual diff", "judgment": "none", "rationale": "private repair reasoning", "lenses": [], "packet_digest": None, "receipts": []}
            state["preflights"] = [{"id": "close-linkage", "commit": HEAD_A, "passed": True, "summary": "private preflight output"}]
        self.store.mutate(add_private)
        rendered = json.dumps(bc._handoff(self.state()))
        self.assertNotIn("sensitive local basis", rendered)
        self.assertNotIn("private detail", rendered)
        self.assertNotIn("token=/private/path", rendered)
        self.assertNotIn("private repair reasoning", rendered)
        self.assertNotIn("private preflight output", rendered)
        self.assertIn("durable evidence", rendered)

    def test_handoff_preserves_finding_severity_and_validation_digest(self):
        self.seed("issue")
        digest = "sha256:" + "a" * 64
        def evidence(state):
            state["findings"].append({"id": "F-1", "stage": "deliverable", "lens": "security-governance",
                "packet_digest": digest, "lens_packet_digest": digest, "commit": HEAD_A,
                "severity": "blocking", "summary": "private", "disposition": "rejected",
                "rationale": "private", "escalation_kind": None, "blocks_this_pr": False,
                "handoff_summary": "Safe concern summary.", "operator_summary": "Safe disagreement.",
                "private_reference": None})
            state["validation"] = {"commit": HEAD_A, "results": [{"id": "ci", "commit": HEAD_A,
                "passed": True, "summary": "private path", "log_digest": digest}]}
        self.store.mutate(evidence)
        handoff = bc._handoff(self.state())
        self.assertEqual(handoff["finding_summaries"][0]["severity"], "blocking")
        self.assertEqual(handoff["validation"]["results"][0]["log_digest"], digest)
        path = Path(self.temp.name) / "handoff-evidence.json"
        path.write_text(json.dumps(handoff))
        restored = bc.StateStore(str(Path(self.temp.name) / "restored-evidence.json"))
        pr = {"number": 7, "state": "OPEN", "headRefOid": HEAD_A}
        with mock.patch.object(bc.repo_identity, "origin_slug", return_value="owner/repo"), \
                mock.patch.object(bc.github, "pr_state", return_value=pr), \
                mock.patch.object(bc, "_head", return_value=HEAD_A), \
                mock.patch.object(bc, "_issue_body", return_value="durable"), \
                mock.patch.object(bc, "_durable_plan", return_value=plan()):
            bc.cmd_handoff_restore(argparse.Namespace(input=str(path), repository="owner/repo", pr=7), restored)
        self.assertEqual(restored.read()["findings"][0]["severity"], "blocking")
        self.assertEqual(restored.read()["validation"]["results"][0]["log_digest"], digest)

    def test_handoff_requires_summary_for_every_finding(self):
        self.seed("issue")
        self.store.mutate(lambda s: s["findings"].append({"id": "F-1", "stage": "plan", "lens": "x", "packet_digest": s["plan"]["digest"], "commit": None, "severity": "nit", "summary": "x", "disposition": "rejected", "rationale": "x", "escalation_kind": None, "blocks_this_pr": False, "handoff_summary": None}))
        with self.assertRaisesRegex(bc.CoordinatorError, "handoff-summary"):
            bc._handoff(self.state())

    def test_handoff_publish_rejects_a_snapshot_revision_race(self):
        self.seed("issue")
        changed = False
        def verify(repo, pr):
            nonlocal changed
            if not changed:
                changed = True
                self.store.mutate(lambda s: s.update({"submission": "draft"}))
            return {"body": "current"}
        args = argparse.Namespace(output="-", publish=True, ack_visibility=True)
        with mock.patch.object(bc, "_issue_body", return_value="durable"), \
                mock.patch.object(bc, "_durable_plan", return_value=plan()), \
                mock.patch.object(bc, "_assert_spec_boundary", return_value={}), \
                mock.patch.object(bc, "_verify_draft", side_effect=verify), \
                mock.patch.object(bc.github, "replace_handoff_block", return_value="updated"), \
                mock.patch.object(bc, "_must_run") as write, \
                self.assertRaisesRegex(bc.CoordinatorError, "evidence changed"):
            bc.cmd_handoff_export(args, self.store)
        write.assert_not_called()

    def test_handoff_publish_rolls_back_when_snapshot_changes_after_write(self):
        self.seed("issue")
        calls = 0
        def verify(repo, pr):
            nonlocal calls
            calls += 1
            if calls == 3:
                self.store.mutate(lambda s: s.update({"submission": "draft"}))
            return {"body": "updated" if calls >= 3 and calls < 5 else "current"}
        args = argparse.Namespace(output="-", publish=True, ack_visibility=True)
        with mock.patch.object(bc, "_issue_body", return_value="durable"), \
                mock.patch.object(bc, "_durable_plan", return_value=plan()), \
                mock.patch.object(bc, "_assert_spec_boundary", return_value={}), \
                mock.patch.object(bc, "_verify_draft", side_effect=verify), \
                mock.patch.object(bc.github, "replace_handoff_block", return_value="updated"), \
                mock.patch.object(bc, "_must_run") as write, \
                self.assertRaisesRegex(bc.CoordinatorError, "stale block was rolled back"):
            bc.cmd_handoff_export(args, self.store)
        self.assertEqual([call.kwargs["input_text"] for call in write.call_args_list], ["updated", "current"])

    def test_routine_progress_restores_from_handoff(self):
        self.seed("issue")
        self.store.mutate(lambda s: s["progress"].update({"current_item": "W1", "completed": [{"id": "W1", "commit": HEAD_A}]}))
        handoff = bc._handoff(self.state())
        self.assertEqual(handoff["progress"]["completed"], [{"id": "W1", "commit": HEAD_A}])

    def test_restore_rebinds_identity_and_rejects_uncontained_progress(self):
        self.seed("issue")
        self.store.mutate(lambda s: s["progress"].update({"current_item": "W1",
                                                          "completed": [{"id": "W1", "commit": HEAD_B}]}))
        path = Path(self.temp.name) / "handoff.json"
        path.write_text(json.dumps(bc._handoff(self.state())))
        restored = bc.StateStore(str(Path(self.temp.name) / "restored.json"))
        pr = {"number": 7, "state": "OPEN", "headRefOid": HEAD_A}
        missing = subprocess.CompletedProcess([], 1, "", "")
        with mock.patch.object(bc.repo_identity, "origin_slug", return_value="owner/repo"), \
                mock.patch.object(bc.github, "pr_state", return_value=pr), \
                mock.patch.object(bc, "_head", return_value=HEAD_A), \
                mock.patch.object(bc, "_run", return_value=missing), \
                self.assertRaisesRegex(bc.CoordinatorError, "not contained"):
            bc.cmd_handoff_restore(argparse.Namespace(input=str(path), repository="owner/repo", pr=7), restored)

    def test_preflight_binds_contract_and_results_to_head(self):
        self.seed()
        args = argparse.Namespace(pr_body=None)
        pr = {"body": "complete", "baseRefOid": BASE}
        close = subprocess.CompletedProcess([], 0, json.dumps({"lines": [], "defang": None}), "")
        with mock.patch.object(bc, "_head", return_value=HEAD_A), mock.patch.object(bc, "_verify_draft", return_value=pr), mock.patch.object(bc, "_run", return_value=close), mock.patch.object(bc, "_pr_contract", return_value=(True, "complete")), contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_preflight(args, self.store)
        self.assertEqual(self.state()["pr_contract"], {"commit": HEAD_A, "body_digest": bc._digest(b"complete"), "complete": True})
        self.assertEqual({x["commit"] for x in self.state()["preflights"]}, {HEAD_A})
        self.assertIn("scope-profile", {x["id"] for x in self.state()["preflights"]})
        self.assertIn("hard-check-declarations", {x["id"] for x in self.state()["preflights"]})

    def test_failed_final_stability_check_does_not_record_preflight(self):
        self.seed()
        class ChangedAfter:
            def __enter__(self): return HEAD_A
            def __exit__(self, *unused): raise bc.CoordinatorError("changed after preflight")
        pr = {"body": "complete", "baseRefOid": BASE}
        ok = subprocess.CompletedProcess([], 0, "ok", "")
        with mock.patch.object(bc.core, "StableCommit", return_value=ChangedAfter()), \
                mock.patch.object(bc, "_verify_draft", return_value=pr), \
                mock.patch.object(bc, "_run", return_value=ok), \
                mock.patch.object(bc, "_pr_contract", return_value=(True, "complete")), \
                self.assertRaisesRegex(bc.CoordinatorError, "changed after"):
            bc.cmd_preflight(argparse.Namespace(pr_body=None, json=False), self.store)
        self.assertEqual(self.state()["preflights"], [])
        self.assertIsNone(self.state()["pr_contract"])

    def test_preflight_runs_close_linkage_and_contract(self):
        self.test_preflight_binds_contract_and_results_to_head()

    def test_close_linkage_is_advisory_but_pr_contract_is_required(self):
        self.seed()
        args = argparse.Namespace(pr_body=None, json=False)
        pr = {"body": "complete", "baseRefOid": BASE}
        close = subprocess.CompletedProcess([], 1, "conflicting close line", "")
        profile = subprocess.CompletedProcess([], 0, "scope", "")
        with mock.patch.object(bc, "_head", return_value=HEAD_A), mock.patch.object(bc, "_verify_draft", return_value=pr), \
                mock.patch.object(bc, "_run", side_effect=[close, profile]), \
                mock.patch.object(bc, "_pr_contract", return_value=(True, "complete")), contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_preflight(args, self.store)
        result = {row["id"]: row for row in self.state()["preflights"]}
        self.assertFalse(result["close-linkage"]["passed"])
        self.assertTrue(self.state()["pr_contract"]["complete"])

    def test_pr_body_change_after_preflight_blocks_submission(self):
        self.seed()
        self.store.mutate(lambda s: s.update({"pr_contract": {"commit": HEAD_A, "body_digest": bc._digest(b"old"), "complete": True}}))
        ready = {"phase": "ready", "head_commit": HEAD_A, "required_evidence": [], "engineering_judgment": []}
        pr = {"number": 7, "state": "OPEN", "isDraft": True, "headRefOid": HEAD_A,
              "baseRefOid": BASE, "mergeable": "MERGEABLE", "body": "new"}
        with mock.patch.object(bc, "_status", return_value=ready), mock.patch.object(bc.github, "pr_state", return_value=pr), \
                self.assertRaisesRegex(bc.CoordinatorError, "body changed after preflight"):
            bc._submit_preview(self.store, str(self.plan_path))

    def test_submit_apply_can_only_mark_ready(self):
        self.seed()
        preview = {"repository": "owner/repo", "pr": 7, "commit": HEAD_A, "base": BASE,
                   "body_digest": bc._digest(b"body"), "snapshot_revision": self.state()["revision"],
                   "action": "mark-ready", "merge": False}
        after = {"state": "OPEN", "isDraft": False, "headRefOid": HEAD_A, "baseRefOid": BASE, "body": "body"}
        with mock.patch.object(bc, "_submit_preview", return_value=preview), \
                mock.patch.object(bc.github, "set_ready") as ready, \
                mock.patch.object(bc.github, "pr_state", return_value=after), contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_submit_apply(argparse.Namespace(plan=str(self.plan_path)), self.store)
        ready.assert_called_once_with(bc.ROOT, "owner/repo", 7)

    def test_submit_apply_recovers_when_matching_pr_is_already_ready(self):
        self.seed()
        preview = {"repository": "owner/repo", "pr": 7, "commit": HEAD_A, "base": BASE,
                   "body_digest": bc._digest(b"body"), "snapshot_revision": self.state()["revision"],
                   "action": "record-ready", "merge": False}
        after = {"state": "OPEN", "isDraft": False, "headRefOid": HEAD_A, "baseRefOid": BASE, "body": "body"}
        with mock.patch.object(bc, "_submit_preview", return_value=preview), \
                mock.patch.object(bc.github, "set_ready") as run, \
                mock.patch.object(bc.github, "pr_state", return_value=after), contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_submit_apply(argparse.Namespace(plan=str(self.plan_path)), self.store)
        run.assert_not_called()
        self.assertEqual(self.state()["submission"], "ready")

    def test_submit_preview_requires_live_base_to_be_ancestor_of_final_commit(self):
        self.seed()
        self.store.mutate(lambda s: s.update({"pr_contract": {"commit": HEAD_A, "body_digest": bc._digest(b"complete"), "complete": True}}))
        ready = {"phase": "ready", "head_commit": HEAD_A, "required_evidence": [], "engineering_judgment": []}
        pr = {"number": 7, "state": "OPEN", "isDraft": True, "headRefOid": HEAD_A, "baseRefOid": BASE, "mergeable": "MERGEABLE", "body": "complete"}
        not_ancestor = subprocess.CompletedProcess([], 1, "", "")
        with mock.patch.object(bc, "_status", return_value=ready), mock.patch.object(bc.github, "pr_state", return_value=pr), mock.patch.object(bc, "_run", return_value=not_ancestor), self.assertRaisesRegex(bc.CoordinatorError, "live target-branch base"):
            bc._submit_preview(self.store, str(self.plan_path))

    def test_submission_requires_live_base_containment(self):
        self.test_submit_preview_requires_live_base_to_be_ancestor_of_final_commit()

    def test_submit_preview_requires_complete_current_evidence(self):
        self.seed()
        self.store.mutate(lambda s: s.update({"pr_contract": {"commit": HEAD_A, "body_digest": bc._digest(b"complete"), "complete": True}}))
        incomplete = {"phase": "implementation", "head_commit": HEAD_A,
                      "required_evidence": ["green validation"], "engineering_judgment": []}
        pr = {"number": 7, "state": "OPEN", "isDraft": True, "headRefOid": HEAD_A,
              "baseRefOid": BASE, "mergeable": "MERGEABLE", "body": "complete"}
        ancestor = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(bc, "_status", return_value=incomplete), mock.patch.object(bc.github, "pr_state", return_value=pr), mock.patch.object(bc, "_run", return_value=ancestor), self.assertRaisesRegex(bc.CoordinatorError, "incomplete"):
            bc._submit_preview(self.store, str(self.plan_path))

    def test_cli_has_no_merge_command(self):
        command_action = next(action for action in bc.parser()._actions if getattr(action, "choices", None))
        self.assertNotIn("merge", command_action.choices)


class TestSettledCriterionGate(CoordinatorCase):
    SPEC_TEXT = """---
status: locked
---

## Acceptance criteria

| Criterion | How verified | Who checks it |
| --- | --- | --- |
| First outcome | Open the result | operator |
| Second outcome | `uv run test` | engine |
"""

    def settled(self, root: Path) -> dict:
        path = root / "docs" / "spec" / "example.md"
        path.parent.mkdir(parents=True)
        path.write_text(self.SPEC_TEXT)
        sys.path.insert(0, str(bc.ROOT / ".engine" / "tools"))
        import spec_referent
        resolved = spec_referent.resolve_doc(str(root), "docs/spec/example.md")
        value = plan()
        value["spec"] = {
            "posture": "settled", "selection_basis": "This Build implements the example capability.",
            "documents": [{
                "path": "docs/spec/example.md", "selection_reason": "The change implements both outcomes.",
                "digest": bc._digest(path.read_bytes()),
                "criteria": [{**{k: v for k, v in bc._criterion("docs/spec/example.md", i, row).items() if k != "who"}, "disposition": "mapped",
                              "work_item_ids": ["W1"], "planned_verification": ["Run the focused test"]}
                             for i, row in enumerate(resolved["criteria"])]
            }]
        }
        return value

    def test_omitted_settled_criterion_prevents_approval(self):
        root = Path(self.temp.name) / "repo"
        value = self.settled(root)
        value["spec"]["documents"][0]["criteria"].pop()
        with mock.patch.object(bc, "ROOT", root), self.assertRaisesRegex(bc.CoordinatorError, "omitted settled criterion"):
            bc._canonical_spec(value)

    def test_adding_missing_mapping_settles_the_plan(self):
        root = Path(self.temp.name) / "repo"
        value = self.settled(root)
        with mock.patch.object(bc, "ROOT", root), mock.patch.object(bc, "_head", return_value=HEAD_A):
            canonical = bc._canonical_spec(value)
        self.assertEqual(len(canonical["documents"][0]["criteria"]), 2)

    def test_missing_not_applicable_reason_fails_schema(self):
        root = Path(self.temp.name) / "repo"
        value = self.settled(root)
        row = value["spec"]["documents"][0]["criteria"][0]
        value["spec"]["documents"][0]["criteria"][0] = {
            "id": row["id"], "digest": row["digest"], "text": row["text"],
            "how_verified": row["how_verified"], "disposition": "not_applicable", "reason": ""
        }
        with self.assertRaisesRegex(bc.CoordinatorError, "not valid"):
            bc._validate(value, bc.PLAN_SCHEMA)

    def test_changed_criterion_invalidates_approval(self):
        root = Path(self.temp.name) / "repo"
        value = self.settled(root)
        with mock.patch.object(bc, "ROOT", root), mock.patch.object(bc, "_head", return_value=HEAD_A):
            canonical = bc._canonical_spec(value)
            state = bc._initial_state("owner/repo", 7, BASE, "session", value, None)
            state["approval"] = {"plan_digest": state["plan"]["digest"], "spec_digest": canonical["digest"], "depth": "thorough"}
            state["plan"]["spec_digest"] = canonical["digest"]
            (root / "docs/spec/example.md").write_text(self.SPEC_TEXT.replace("Second outcome", "Changed outcome"))
            with self.assertRaisesRegex(bc.CoordinatorError, "stale"):
                bc._assert_spec_current(state, value)

    def test_deleted_issue_pointer_invalidates_a_settled_plan(self):
        root = Path(self.temp.name) / "repo"
        value = self.settled(root); value["intent_source"] = {"kind": "issue", "issue": 770}
        no_pointer = {"ok": False, "no_op_reason": "no-issue-pointer", "detail": "no settled pointer"}
        sys.path.insert(0, str(bc.ROOT / ".engine" / "tools"))
        import spec_referent
        with mock.patch.object(bc, "ROOT", root), mock.patch.object(bc, "_issue_body", return_value="body"), \
                mock.patch.object(spec_referent, "resolve_from_body", return_value=no_pointer), \
                self.assertRaisesRegex(bc.CoordinatorError, "authority is unusable"):
            bc._canonical_spec(value, repository="owner/repo", check_issue=True)

    def test_issue_linked_settled_authority_cannot_hide_behind_no_spec(self):
        root = Path(self.temp.name) / "repo"
        self.settled(root)
        value = plan(); value["intent_source"] = {"kind": "issue", "issue": 770}
        body = "See [settled description](docs/spec/example.md)."
        with mock.patch.object(bc, "ROOT", root), mock.patch.object(bc, "_issue_body", return_value=body), self.assertRaisesRegex(bc.CoordinatorError, "cannot declare no spec"):
            bc._canonical_spec(value, repository="owner/repo", check_issue=True)

    def test_unrelated_markdown_link_does_not_invent_spec_authority(self):
        value = plan(); value["intent_source"] = {"kind": "issue", "issue": 770}
        unrelated = {"ok": False, "no_op_reason": "pointer-not-under-docs-spec",
                     "detail": "this change is not linked to a settled description"}
        sys.path.insert(0, str(bc.ROOT / ".engine" / "tools"))
        import spec_referent
        with mock.patch.object(bc, "_issue_body", return_value="[operation](.engine/operations/build-orchestration.md)"), \
                mock.patch.object(spec_referent, "resolve_from_body", return_value=unrelated):
            canonical = bc._canonical_spec(value, repository="owner/repo", check_issue=True)
        self.assertEqual(canonical["posture"], "none")

    def test_failed_spec_read_never_degrades_to_no_spec(self):
        value = plan(); value["intent_source"] = {"kind": "issue", "issue": 770}
        with mock.patch.object(bc, "_issue_body", side_effect=bc.CoordinatorError("network unavailable")), self.assertRaisesRegex(bc.CoordinatorError, "network unavailable"):
            bc._canonical_spec(value, repository="owner/repo", check_issue=True)

    def test_unsettled_issue_authority_never_degrades_to_no_spec(self):
        value = plan(); value["intent_source"] = {"kind": "issue", "issue": 770}
        unresolved = {"ok": False, "no_op_reason": "doc-not-locked", "detail": "the linked description is not settled"}
        sys.path.insert(0, str(bc.ROOT / ".engine" / "tools"))
        import spec_referent
        with mock.patch.object(bc, "_issue_body", return_value="body"), mock.patch.object(spec_referent, "resolve_from_body", return_value=unresolved), self.assertRaisesRegex(bc.CoordinatorError, "authority is unusable"):
            bc._canonical_spec(value, repository="owner/repo", check_issue=True)

    def test_packets_and_review_steps_share_canonical_spec(self):
        self.seed(); self.approve("thorough")
        canonical = {"posture": "settled", "documents": [{"criteria": [{"text": "exact criterion"}]}],
                     "review_steps": [{"operator_steps": ["exact criterion"]}], "digest": "sha256:" + "1" * 64}
        args = argparse.Namespace(stage="plan", plan=str(self.plan_path), impact=None)
        out = io.StringIO()
        with mock.patch.object(bc, "_assert_spec_current", return_value=canonical), mock.patch.object(bc, "_installed", return_value=[]), contextlib.redirect_stdout(out):
            bc._packet(args, self.store)
        self.assertEqual(json.loads(out.getvalue())["spec"], canonical)


class TestHistoricalScenarioCorpus(unittest.TestCase):
    def test_consumed_review_lenses_remain_connected(self):
        text = (bc.ROOT / ".engine" / "operations" / "build-orchestration.md").read_text()
        for lens in ("product-intent", "architecture", "feasibility", "risk-governance",
                     "spec-conformance", "divergence-hunter", "usability", "technical-integrity", "security-governance"):
            self.assertIn(lens, text)

    def test_every_mapped_obligation_has_one_live_disposition(self):
        obligations = json.loads((bc.ROOT / ".engine/build-orchestration-obligations.json").read_text())
        self.assertEqual(len(obligations["obligations"]), 65)
        self.assertEqual(len({row["id"] for row in obligations["obligations"]}), 65)

    def test_special_delivery_and_submission_disclosures_remain_reachable(self):
        owned = (bc.ROOT / ".engine/operations/owned-product-build.md").read_text()
        external = (bc.ROOT / ".engine/operations/external-contribution-submit.md").read_text()
        evidence = (bc.ROOT / ".engine/operations/build-submission-evidence.md").read_text()
        for phrase in ("mechanic_build.py worktree", "tools/local_references.py scan", "unpushed commits", "worker fails"):
            self.assertIn(phrase, owned)
        self.assertIn("no draft PR is", external)
        for phrase in ("recognized automation", "fail-open", "mcp_availability_check", "unresolved-conversation", "operator-runnable demonstration"):
            self.assertIn(phrase, evidence)

    def test_runbook_stays_within_the_250_line_cap(self):
        text = (bc.ROOT / ".engine/operations/build-orchestration.md").read_text()
        self.assertLessEqual(len(text.splitlines()), 250)

    def test_preservation_map_records_the_exact_historical_source_identity(self):
        source = json.loads((bc.ROOT / ".engine/build-orchestration-obligations.json").read_text())["preservation_source"]
        self.assertEqual(source["lines"], 448)
        self.assertEqual(source["words"], 6296)
        self.assertIn("structural", source["assurance"])

    def test_home_only_obligations_are_explicitly_scoped(self):
        obligations = json.loads((bc.ROOT / ".engine/build-orchestration-obligations.json").read_text())
        rows = [row for row in obligations["obligations"] if row.get("scope") == "home-only"]
        self.assertEqual({row["id"] for row in rows}, {"BO-06"})

    def test_json_artifacts_are_owner_only_and_not_reused(self):
        first, digest = bc._write_json_artifact("packet-test", {"secret": "plan"})
        second, second_digest = bc._write_json_artifact("packet-test", {"secret": "plan"})
        self.assertEqual(digest, second_digest)
        self.assertNotEqual(first, second)
        self.assertEqual(Path(first).stat().st_mode & 0o777, 0o600)

    def test_source_linked_scenarios_cover_recovery_behaviors(self):
        fixture = json.loads((bc.ROOT / ".engine" / "_fixtures" / "build-coordinator-scenarios" / "scenarios.v1.json").read_text())
        scenarios = fixture["scenarios"]
        self.assertEqual(len(scenarios), 8)
        self.assertEqual(len({x["id"] for x in scenarios}), 8)
        self.assertTrue(all(x["source"]["pull_request"] and x["source"]["memory_session"] for x in scenarios))
        expected = {item for scenario in scenarios for item in scenario["expected"]}
        forbidden = {item for scenario in scenarios for item in scenario["must_not"]}
        self.assertIn("none-judgment-is-valid", expected)
        self.assertIn("review-loop-terminates", expected)
        self.assertIn("recursive-audit", forbidden)
        self.assertIn("blindly-apply-reviewer-remedy", forbidden)
        self.assertIn("local-status-remains-usable", expected)
        self.assertIn("ready-pr-is-human-merge-surface", expected)

    def test_every_scenario_is_bound_to_mechanical_tests_that_run_in_this_suite(self):
        fixture = json.loads((bc.ROOT / ".engine" / "_fixtures" / "build-coordinator-scenarios" / "scenarios.v1.json").read_text())
        classes = {cls.__name__: cls for cls in (
            TestPlanAndSnapshot, TestReviewAndFindings, TestValidationRepairAndStatus,
            TestPreflightHandoffAndSubmission,
        )}
        for scenario in fixture["scenarios"]:
            self.assertTrue(scenario["mechanical_tests"], scenario["id"])
            for reference in scenario["mechanical_tests"]:
                class_name, method_name = reference.split(".", 1)
                self.assertIn(class_name, classes, reference)
                self.assertTrue(callable(getattr(classes[class_name], method_name, None)), reference)

    def test_short_runbook_preserves_the_quality_and_authority_gates(self):
        text = (bc.ROOT / ".engine" / "operations" / "build-orchestration.md").read_text()
        self.assertLessEqual(len(text.split()), 3063)
        for phrase in ("operator-approved plan", "one cold plan review", "reviewed-to-final divergence",
                       "no automatic audit recursion", "operator alone merges"):
            self.assertIn(phrase, text)

    def test_runbook_keeps_review_synthesis_marker_grammar_and_routine_authority_boundary(self):
        runbook = (bc.ROOT / ".engine/operations/build-orchestration.md").read_text()
        routine = (bc.ROOT / ".engine/operations/routine-entry.md").read_text()
        self.assertIn("one recommended call", runbook)
        self.assertIn("ENGINE-TODO` marker grammar", runbook)
        self.assertIn("requires no Issue", runbook)
        self.assertIn("engineering blocker inside the approved design and scope is solved", routine)
        self.assertNotIn("a genuine blocker or a decision needing a human", routine)

    def test_every_reviewer_receives_the_exact_approved_plan(self):
        agents = list((bc.ROOT / ".claude" / "agents").glob("engine-design-review-*.md"))
        agents += list((bc.ROOT / ".claude" / "agents").glob("engine-qa-review-*.md"))
        self.assertEqual(len(agents), 9)
        for agent in agents:
            self.assertIn("exact operator-approved Build plan", agent.read_text(), agent.name)

    def test_reviewers_do_not_assign_finding_adjudication_to_the_operator(self):
        agents = list((bc.ROOT / ".claude" / "agents").glob("engine-design-review-*.md"))
        agents += list((bc.ROOT / ".claude" / "agents").glob("engine-qa-review-*.md"))
        for agent in agents:
            text = agent.read_text()
            self.assertNotIn("You report; the operator decides", text, agent.name)
            self.assertNotIn("the build process collects them and the operator decides", text, agent.name)
            self.assertIn("orchestrator", text, agent.name)

    def test_no_spec_keeps_both_plan_derived_conformance_lenses(self):
        for name in ("engine-qa-review-spec-conformance.md", "engine-qa-review-divergence-hunter.md"):
            text = (bc.ROOT / ".claude" / "agents" / name).read_text()
            self.assertIn("no-spec is not a no-op" if "divergence" in name else "It is not a no-op", text)
            self.assertIn("operator-approved Build plan", text)


class TestPlanV2Ingest(CoordinatorCase):
    def _bind(self, value, *, source="session", issue=None, home=True):
        self.write_plan(value)
        args = argparse.Namespace(input=str(self.plan_path), source=source, repository="owner/repo",
                                  pr=7, issue=issue, mode="same-session")
        pr = {"number": 7, "state": "OPEN", "isDraft": True, "headRefOid": HEAD_A, "baseRefOid": BASE}
        stack = [
            mock.patch.object(bc, "_verify_draft", return_value=pr),
            mock.patch.object(bc, "_head", return_value=HEAD_A),
            mock.patch.object(bc, "_confidently_home", return_value=home),
        ]
        if source == "issue":
            stack.append(mock.patch.object(bc, "_durable_plan", return_value=value))
            stack.append(mock.patch.object(bc, "_issue_body", return_value="body"))
        with contextlib.ExitStack() as es:
            for p in stack:
                es.enter_context(p)
            es.enter_context(contextlib.redirect_stdout(io.StringIO()))
            bc.cmd_plan_bind(args, self.store)

    def test_v2_plan_binds_with_a_versioned_snapshot_and_empty_work_map(self):
        self._bind(plan_v2())
        state = self.state()
        self.assertEqual(state["schema_version"], "build-state.v2")
        self.assertEqual(state["work"], {})

    def test_v2_cycle_is_refused(self):
        value = plan_v2(items=[_work_item_v2("a", ["b"]), _work_item_v2("b", ["a"])])
        self.write_plan(value)
        with self.assertRaisesRegex(bc.CoordinatorError, "cycle"):
            bc._plan(str(self.plan_path))

    def test_v2_unknown_dependency_is_refused(self):
        value = plan_v2(items=[_work_item_v2("a", ["ghost"])])
        self.write_plan(value)
        with self.assertRaisesRegex(bc.CoordinatorError, "unknown work item ghost"):
            bc._plan(str(self.plan_path))

    def test_v2_self_dependency_is_refused(self):
        value = plan_v2(items=[_work_item_v2("a", ["a"])])
        self.write_plan(value)
        with self.assertRaisesRegex(bc.CoordinatorError, "cannot depend on itself"):
            bc._plan(str(self.plan_path))

    def test_v2_duplicate_ids_are_refused(self):
        value = plan_v2(items=[_work_item_v2("a", []), _work_item_v2("a", [])])
        self.write_plan(value)
        with self.assertRaisesRegex(bc.CoordinatorError, "must be unique"):
            bc._plan(str(self.plan_path))

    def test_independent_roots_carry_no_ordering(self):
        value = plan_v2(items=[_work_item_v2("a", []), _work_item_v2("b", [])])
        self.write_plan(value)
        loaded = bc._plan(str(self.plan_path))
        self.assertEqual([i["id"] for i in loaded["work_items"]], ["a", "b"])

    def test_unrecognized_plan_version_is_refused(self):
        value = plan_v2()
        value["schema_version"] = "build-plan.v9"
        self.write_plan(value)
        with self.assertRaisesRegex(bc.CoordinatorError, "unrecognized Build plan version"):
            bc._plan(str(self.plan_path))

    def test_new_session_v1_bind_is_refused_in_a_deployed_repo(self):
        with self.assertRaisesRegex(bc.CoordinatorError, "refused now that build-plan.v2"):
            self._bind(plan(), home=False)

    def test_session_v1_bind_is_permitted_in_the_home_repo(self):
        self._bind(plan(), home=True)
        self.assertEqual(self.state()["schema_version"], "build-state.v1")

    def test_confidently_home_requires_a_readable_matching_origin(self):
        # The governance polarity fix: an unreadable/mismatched origin is NOT confidently home, so the
        # v1-bind refusal fails toward ENFORCING rather than being silently skipped in a deployed repo.
        with mock.patch.object(bc.repo_identity, "origin_slug", return_value=None):
            self.assertFalse(bc._confidently_home())
        with mock.patch.object(bc.repo_identity, "origin_slug", return_value="o/r"), \
                mock.patch.object(bc.repo_identity, "home_repository", return_value="other/repo"):
            self.assertFalse(bc._confidently_home())
        with mock.patch.object(bc.repo_identity, "origin_slug", return_value="o/r"), \
                mock.patch.object(bc.repo_identity, "home_repository", return_value="o/r"):
            self.assertTrue(bc._confidently_home())

    def test_issue_sourced_v1_rebind_is_not_walled_by_the_refusal(self):
        # An issue-sourced bind is the exempt continuation path even in a deployed repo.
        issue_plan = plan()
        try:
            self._bind(issue_plan, source="issue", issue=11, home=False)
        except bc.CoordinatorError as exc:  # noqa: BLE001 — only the v1-refusal message must not appear
            self.assertNotIn("refused now that build-plan.v2", str(exc))
        self.assertEqual(self.state()["schema_version"], "build-state.v1")


class TestV1Migration(CoordinatorCase):
    def _v1_two_items(self):
        value = plan()
        value["work_items"] = [
            {"id": "one", "description": "First", "paths": ["a/x.py"], "verification": ["run one"]},
            {"id": "two", "description": "Second", "paths": ["a/y.py"], "verification": ["run two"]},
        ]
        return value

    def test_migrate_produces_a_linear_chain_with_a_new_digest(self):
        v1 = self._v1_two_items()
        v2 = bc._migrate_v1_to_v2(v1)
        self.assertEqual(v2["schema_version"], "build-plan.v2")
        self.assertEqual(v2["work_items"][0]["depends_on"], [])
        self.assertEqual(v2["work_items"][1]["depends_on"], ["one"])
        self.assertTrue(all(i["executor_class"] == "integrator" for i in v2["work_items"]))
        self.assertEqual(v2["parallelism"], {"mode": "serial", "max_concurrency": 1})
        self.assertNotEqual(bc._digest(v1), bc._digest(v2))
        bc.dag.validate_dag(v2)  # the chain is acyclic

    def test_migrate_cli_emits_v2_and_requires_v1_input(self):
        self.write_plan(self._v1_two_items())
        args = argparse.Namespace(input=str(self.plan_path), output="-")
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            bc.cmd_plan_migrate_v1(args, None)
        emitted = json.loads(out.getvalue())
        self.assertEqual(emitted["schema_version"], "build-plan.v2")
        # a v2 input is refused
        self.write_plan(plan_v2())
        args = argparse.Namespace(input=str(self.plan_path), output="-")
        with self.assertRaisesRegex(bc.CoordinatorError, "requires a build-plan.v1"):
            bc.cmd_plan_migrate_v1(args, None)

    def test_v1_reader_sunsets_at_the_removal_major(self):
        # Fails closed once the Engine major reaches the sunset while the v1 reader still ships — the
        # mechanical removal trigger. A no-op below the sunset (and at the 0.0.0 construction sentinel).
        release = json.loads((bc.ROOT / ".engine" / "engine.json").read_text()).get("engine_release", "0.0.0")
        if release == "0.0.0":
            return
        major = int(release.split(".")[0])
        v1_reader_present = (bc.ROOT / ".engine" / "schemas" / "build-plan.v1.json").exists()
        if major >= bc.PLAN_V1_REMOVE_AT_MAJOR and v1_reader_present:
            self.fail(f"Engine major {major} has reached the v1 sunset ({bc.PLAN_V1_REMOVE_AT_MAJOR}) but the "
                      f"build-plan.v1 reader still ships; remove the v1 reader and its ordered path.")


if __name__ == "__main__":
    unittest.main()
