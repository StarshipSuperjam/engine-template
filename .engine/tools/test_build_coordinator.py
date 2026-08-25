#!/usr/bin/env python3
"""Focused mechanical tests for the Build coordinator instrument panel."""
from __future__ import annotations

import argparse
import contextlib
import types
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
import repair_divergence  # noqa: E402

HEAD_A = "a" * 40
HEAD_B = "b" * 40
HEAD_C = "c" * 40
HEAD_D = "d" * 40
HEAD_E = "e" * 40
HEAD_F = "f" * 40
BASE = "0" * 40
PLAN_ID = "pln_0123456789ab"
SEALED = "sha256:" + "e" * 64


def classification(guarded=(), derived=(), docs=(), authored=(), churn=None):
    """A repair_divergence.classify() result, as the coordinator consumes it. The commits are fake
    throughout these tests, so the real classifier (which shells out to git) is stood in for — what is
    under test here is what the COORDINATOR does with a classification, not how one is measured."""
    files = {"guarded": list(guarded), "derived": list(derived),
             "docs": list(docs), "authored": list(authored)}
    counts = dict(churn) if churn else {kind: len(paths) for kind, paths in files.items()}
    return {"anchor": HEAD_A, "head": HEAD_B, "files": files, "churn": counts,
            "total_churn": sum(counts.values()), "guards_read": True}


def plan_v1(objective="Ship a small instrument panel"):
    """A build-plan.v1 document. No longer a Build entry shape — v1 is refused at `plan bind` — but
    still the fixture for the version dispatch and the migration verb, which the v1 sunset removes."""
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


def plan(objective="Ship a small instrument panel"):
    """The ordinary Build plan for these cases: build-plan.v2, because that is now the only shape that
    enters a Build at all. Deliberately one integrator-executed item, so cases about the panel's general
    mechanics are not also cases about the DAG."""
    value = plan_v1(objective)
    value["schema_version"] = "build-plan.v2"
    value["work_items"] = [{
        **value["work_items"][0],
        "depends_on": [], "exclusive_resources": [], "executor_class": "integrator",
        "output_contract": {"deliverable": "The instrument panel and its tests",
                            "artifact_kinds": ["integrated-commit"],
                            "required_evidence": ["changed_paths", "verification_results"]},
    }]
    value["parallelism"] = {"mode": "serial", "max_concurrency": 1}
    return value


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

    def seed(self, issue=None):
        """Seed a bound Build. The plan of record is a sealed library plan named by id; `issue` is the
        Issue that AUTHORIZED the work, which after the cutover is never where the plan lives."""
        value = plan()
        state = bc._initial_state("owner/repo", 7, BASE, PLAN_ID, SEALED, value, issue)
        self.store.create(state)
        return state

    def approve(self, depth="thorough"):
        args = argparse.Namespace(plan=str(self.plan_path), depth=depth)
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_approve(args, self.store)

    def state(self):
        return self.store.read()

    def bind_args(self, **over):
        args = {"plan": PLAN_ID, "mode": "same-session", "repository": "owner/repo", "pr": 7,
                "issue": None, "operator_decision": "yes, start the Build"}
        args.update(over)
        return argparse.Namespace(**args)

    def sealed(self, value=None, plan_id=PLAN_ID, sealed_digest=SEALED):
        """Stand in for the library lookup. The lookup itself has its own cases against a real
        library (TestSealedPlanEntry); everything else is about what bind DOES with a sealed plan."""
        return mock.patch.object(bc, "_sealed_plan",
                                 return_value=(plan_id, sealed_digest, value or plan()))

    def integrate_all(self, value=None):
        """Mark every node of the bound plan integrated, the way `work integrate` would.

        Needed wherever a case's real subject is downstream of the graph — validation, drift, status —
        because the completion gate now holds final validation until every node is integrated."""
        value = value or plan()
        work = {}
        for index, item in enumerate(value["work_items"]):
            attempt = "%032x" % (index + 1)
            work[item["id"]] = {
                "attempt_count": 1,
                "claim": None,
                "latest_result": None,
                "latest_failure": None,
                "integration": {"attempt_id": attempt, "commit": HEAD_A,
                                "focused_verification": "node tests"},
            }
        completed = [{"id": item["id"], "commit": HEAD_A} for item in value["work_items"]]

        def change(state):
            state["work"] = work
            state["progress"] = {"current_item": None, "completed": completed}
        self.store.mutate(change)

    def revise_args(self, **over):
        args = {"input": str(self.plan_path), "operator_change": "The operator authorized this change."}
        args.update(over)
        return argparse.Namespace(**args)


class TestPlanAndSnapshot(CoordinatorCase):
    def test_plan_work_items_are_ordered_and_unique(self):
        value = plan()
        value["work_items"].append({**value["work_items"][0], "description": "duplicate"})
        self.write_plan(value)
        with self.assertRaisesRegex(bc.CoordinatorError, "must be unique"):
            bc._plan(str(self.plan_path))

    def test_bind_initializes_only_for_the_matching_draft_pr_head(self):
        pr = {"number": 7, "state": "OPEN", "isDraft": True, "headRefOid": HEAD_A, "baseRefOid": BASE}
        with self.sealed(), mock.patch.object(bc, "_verify_draft", return_value=pr), mock.patch.object(bc, "_head", return_value=HEAD_A), mock.patch.object(bc.github, "tag_coordinator_owned", return_value=True), mock.patch.object(bc, "_record_build_binding"), contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_plan_bind(self.bind_args(), self.store)
        self.assertEqual(self.state()["build"], {"repository": "owner/repo", "pr": 7, "base_at_bind": BASE,
                                                 "mode": "same-session", "worktree": str(bc.ROOT)})

    def test_bind_names_the_sealed_plan_it_entered_on(self):
        pr = {"number": 7, "state": "OPEN", "isDraft": True, "headRefOid": HEAD_A, "baseRefOid": BASE}
        with self.sealed(), mock.patch.object(bc, "_verify_draft", return_value=pr), mock.patch.object(bc, "_head", return_value=HEAD_A), mock.patch.object(bc.github, "tag_coordinator_owned", return_value=True), mock.patch.object(bc, "_record_build_binding") as binding, contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_plan_bind(self.bind_args(), self.store)
        recorded = self.state()["plan"]
        self.assertEqual(recorded["plan_id"], PLAN_ID)
        self.assertEqual(recorded["sealed_digest"], SEALED)
        self.assertFalse(recorded["diverged_from_seal"])
        self.assertIsNone(recorded["authorizing_issue"])
        binding.assert_called_once()

    def test_unattended_bind_requires_an_authorizing_issue(self):
        with self.sealed(), self.assertRaisesRegex(bc.CoordinatorError, "durable Issue for authorization"):
            bc.cmd_plan_bind(self.bind_args(mode="unattended"), self.store)

    @staticmethod
    def _from_issue(number=770, profile=None):
        value = plan()
        value["intent_source"] = {"kind": "issue", "issue": number}
        if profile:
            value["profile"] = profile
        return value

    def _bind_ok(self, value, **over):
        pr = {"number": 7, "state": "OPEN", "isDraft": True, "headRefOid": HEAD_A, "baseRefOid": BASE}
        with self.sealed(value=value), mock.patch.object(bc, "_verify_draft", return_value=pr), \
                mock.patch.object(bc, "_head", return_value=HEAD_A), \
                mock.patch.object(bc.github, "tag_coordinator_owned", return_value=True), \
                mock.patch.object(bc, "_record_build_binding"), contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_plan_bind(self.bind_args(**over), self.store)

    def test_an_unattended_bind_refuses_an_issue_the_sealed_plan_does_not_name(self):
        """The replacement for the old Issue-carries-the-plan digest equality. With the plan held
        locally the Issue and the plan are two artifacts, so an unrelated open Issue paired with an
        arbitrary sealed plan must authorize nothing — and the refusal names both numbers, because the
        operator's next move is deciding which of the two was the mistake."""
        with self.sealed(value=self._from_issue(770, "routine")), \
                self.assertRaisesRegex(bc.CoordinatorError, r"Issue #999 does not authorize this plan"):
            bc.cmd_plan_bind(self.bind_args(mode="unattended", issue=999), self.store)

    def test_an_unattended_bind_refuses_a_plan_that_names_no_issue_at_all(self):
        # The other half of the same hole: an Issue supplied against a plan with direct intent has
        # nothing to correspond to, so supplying it proved nothing.
        with self.sealed(), self.assertRaisesRegex(bc.CoordinatorError, "names no authorizing Issue"):
            bc.cmd_plan_bind(self.bind_args(mode="unattended", issue=770), self.store)

    def test_the_matching_issue_authorizes_an_unattended_bind_and_is_recorded(self):
        self._bind_ok(self._from_issue(770, "routine"), mode="unattended", issue=770)
        self.assertEqual(self.state()["plan"]["authorizing_issue"], 770)
        self.assertEqual(self.state()["build"]["mode"], "unattended")

    def test_neither_artifact_substitutes_for_the_other(self):
        """Stated as the property rather than as two more incidental cases: supplying the Issue does
        not excuse a plan that never named it, and holding a sealed plan does not excuse a missing
        Issue. Each half is refused in its own words, so a session cannot satisfy one by producing the
        other."""
        with self.sealed(value=self._from_issue(770, "routine")), \
                self.assertRaisesRegex(bc.CoordinatorError, "durable Issue for authorization"):
            bc.cmd_plan_bind(self.bind_args(mode="unattended"), self.store)     # the plan alone
        with self.sealed(), self.assertRaisesRegex(bc.CoordinatorError, "names no authorizing Issue"):
            bc.cmd_plan_bind(self.bind_args(mode="unattended", issue=770), self.store)   # the Issue alone

    def test_a_mismatched_issue_is_refused_in_an_interactive_bind_too(self):
        # --issue stays optional same-session (the operator is present), but one supplied in ANY mode
        # must still correspond: a mismatch is a mistake worth catching wherever it is made.
        with self.sealed(value=self._from_issue(770)), \
                self.assertRaisesRegex(bc.CoordinatorError, r"sealed against Issue #770"):
            bc.cmd_plan_bind(self.bind_args(issue=999), self.store)

    def test_an_interactive_issue_the_plan_never_named_is_refused(self):
        """The other half of "in ANY mode": correspondence fails when the plan names no Issue at all.

        Same-session Builds are authorized by the operator being present, so --issue is optional here —
        but a number supplied anyway does not simply sit unused. It is stored as the authorizing Issue
        and composed into the pull request as a Closes link, so accepting it unchecked would put an
        unverified claim on the merge surface: a PR asserting it closes work nothing tied it to.
        """
        with self.sealed(), self.assertRaises(bc.CoordinatorError) as caught:
            bc.cmd_plan_bind(self.bind_args(issue=999), self.store)
        self.assertIn("names no Issue", str(caught.exception))
        self.assertIn("Bind without --issue", str(caught.exception))

    def test_an_interactive_bind_still_needs_no_issue(self):
        self._bind_ok(self._from_issue(770))
        self.assertIsNone(self.state()["plan"]["authorizing_issue"])

    def test_the_profile_rule_is_reported_before_the_authorization_rule(self):
        # A trivial plan bound unattended breaks both rules at once. The profile one is the root cause
        # — no Issue could have made this bind legal — so reporting the other would send the operator
        # hunting for the right Issue number for a Build that was never going to be unattended.
        value = plan(); value["profile"] = "trivial"
        with self.sealed(value=value), self.assertRaisesRegex(bc.CoordinatorError, "same-session only"):
            bc.cmd_plan_bind(self.bind_args(mode="unattended", issue=None), self.store)

    def test_bind_refuses_a_v1_payload_and_names_the_way_forward(self):
        with self.sealed(value=plan_v1()), self.assertRaisesRegex(bc.CoordinatorError, "v1 no longer enters a Build"):
            bc.cmd_plan_bind(self.bind_args(), self.store)

    def test_bind_rejects_a_draft_pr_at_a_different_head(self):
        with self.sealed(), mock.patch.object(bc, "_verify_draft", return_value={"headRefOid": HEAD_B}), mock.patch.object(bc, "_head", return_value=HEAD_A), self.assertRaisesRegex(bc.CoordinatorError, "does not match"):
            bc.cmd_plan_bind(self.bind_args(), self.store)

    def test_plan_digest_is_canonical_and_exact_content_is_not_stored(self):
        value = plan()
        state = bc._initial_state("owner/repo", 7, BASE, PLAN_ID, SEALED, value, None)
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

    def test_a_snapshot_outside_os_temp_is_accepted_now_that_state_is_durable(self):
        # The inverse of the assertion this replaces. Until 2026-08-25 a snapshot outside the OS
        # temporary directory was REFUSED, and that refusal is exactly what made a killed Build lose
        # its evidence. It is deleted with the eADR-0025 and eADR-0041 amendments, and durability has
        # a proper home in build_state_store. Asserted here, where the old guarantee was made, so the
        # deletion is visible at the class that carried it rather than only in the new module.
        self.assertEqual(bc.StateStore(str(Path(os.sep) / "state.json")).path,
                         Path(os.sep) / "state.json")

    def test_plan_revision_invalidates_approval_and_reviews_but_not_build_identity(self):
        self.seed(); self.approve()
        before = self.state()["build"]
        revised = plan("A genuinely changed outcome")
        bc._reset_after_revision(self.state(), revised)  # pure-shape smoke first
        self.write_plan(revised)
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_plan_revise(self.revise_args(), self.store)
        state = self.state()
        self.assertEqual(state["build"], before)
        self.assertIsNone(state["approval"])
        self.assertIsNone(state["reviews"]["deliverable"]["packet_digest"])

    def test_unchanged_revision_preserves_evidence(self):
        self.seed(); self.approve()
        revision = self.state()["revision"]
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_plan_revise(self.revise_args(), self.store)
        self.assertEqual(self.state()["revision"], revision)

    def test_changing_approved_depth_clears_review_evidence(self):
        self.seed(); self.approve("standard")
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"packet_digest": "sha256:" + "1" * 64}))
        self.approve("thorough")
        self.assertIsNone(self.state()["reviews"]["deliverable"]["packet_digest"])

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
        state = bc._initial_state("owner/repo", 7, BASE, PLAN_ID, SEALED, value, None)
        state["approval"] = {"plan_digest": state["plan"]["digest"], "spec_digest": None, "depth": "quick"}
        with mock.patch.object(bc, "_head", return_value=HEAD_A), mock.patch.object(bc, "_changed_paths", return_value=[]), mock.patch.object(bc, "_must_run", return_value="1"):
            status = bc._status(state, value)
        self.assertNotIn("plan-review packet", status["required_evidence"])
        self.assertNotIn("deliverable-review packet", status["required_evidence"])

    def test_trivial_plan_uses_the_reduced_document_shape(self):
        value = {
            "schema_version": "build-plan.v2", "profile": "trivial",
            "intent_source": {"kind": "direct"}, "raw_intent": "Correct one typo.",
            "objective": "Correct the typo.",
            "success_obligations": [{"outcome": "Text is correct.", "verification": "Read it."}],
            "parallelism": {"mode": "serial", "max_concurrency": 1},
            "work_items": [{"id": "W1", "description": "Correct it", "paths": ["README.md"],
                            "verification": ["Read the changed line"], "depends_on": [],
                            "exclusive_resources": [], "executor_class": "integrator",
                            "output_contract": {"deliverable": "the corrected line",
                                                "artifact_kinds": ["integrated-commit"],
                                                "required_evidence": ["changed_paths"]}}],
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
        state = bc._initial_state("owner/repo", 7, BASE, PLAN_ID, SEALED, value, None)
        state["approval"] = {"plan_digest": state["plan"]["digest"], "spec_digest": None, "depth": "quick"}
        with mock.patch.object(bc, "_head", return_value=HEAD_A), mock.patch.object(bc, "_changed_paths", return_value=[".engine/schemas/x.json"]), mock.patch.object(bc, "_must_run", return_value="1"):
            status = bc._status(state, value)
        self.assertTrue(any("raise the trivial Build" in item for item in status["engineering_judgment"]))

    def test_trivial_uses_the_canonical_guarded_file_classifier(self):
        value = plan(); value["profile"] = "trivial"
        state = bc._initial_state("owner/repo", 7, BASE, PLAN_ID, SEALED, value, None)
        state["approval"] = {"plan_digest": state["plan"]["digest"], "spec_digest": None, "depth": "quick"}
        guarded = [".engine/uv.lock", ".engine/suites.json", ".claude/settings.json",
                   ".codex/hooks.json", ".github/dependabot.yml", ".engine/schemas/new.json"]
        for path in guarded:
            with self.subTest(path=path), mock.patch.object(bc, "_head", return_value=HEAD_A), \
                    mock.patch.object(bc, "_changed_paths", return_value=[path]), \
                    mock.patch.object(bc, "_must_run", return_value="1"):
                status = bc._status(state, value)
                self.assertTrue(any("raise the trivial Build" in item for item in status["engineering_judgment"]))

    def test_trivial_cannot_bind_unattended(self):
        value = plan(); value["profile"] = "trivial"; self.write_plan(value)
        with self.sealed(value=value), self.assertRaisesRegex(bc.CoordinatorError, "same-session only"):
            bc.cmd_plan_bind(self.bind_args(mode="unattended", issue=11), self.store)

    def test_same_session_status_survives_github_loss(self):
        value = plan(); value["intent_source"] = {"kind": "issue", "issue": 770}
        state = bc._initial_state("owner/repo", 7, BASE, PLAN_ID, SEALED, value, None)
        state["approval"] = {"plan_digest": state["plan"]["digest"], "spec_digest": None, "depth": "quick"}
        with mock.patch.object(bc, "_issue_body", side_effect=bc.CoordinatorError("offline")), mock.patch.object(bc, "_head", return_value=HEAD_A):
            status = bc._status(state, value)
        self.assertIn("phase", status)


class TestSealedPlanEntry(CoordinatorCase):
    """The one door. These run against a REAL plan library, because the whole point of the cutover is
    that entry is decided by a stored seal rather than by whatever a session hands the coordinator."""

    def setUp(self):
        super().setUp()
        import plan_store
        from test_plan_store import _document
        self.library_root = Path(self.temp.name) / "plans"
        self.library = plan_store.PlanLibrary(self.library_root)
        self.document = _document(build_plan=plan())
        self.slug = self.library.create(self.document)
        patched = mock.patch.object(bc, "_library", return_value=self.library)
        patched.start()
        self.addCleanup(patched.stop)

    def seal_it(self, payload=None):
        import plan_contract
        document = self.library.head(self.slug)
        record = self.library.read_record(self.slug)
        seal = {"revision": record["current"]["revision"],
                "reviewed_digest": record["current"]["plan_digest"],
                "sealed_digest": record["current"]["plan_digest"],
                "build_plan_digest": plan_contract.build_plan_digest(document),
                "at": "2026-08-24T00:00:00Z", "delta_judgment": "none"}
        self.library.update_record(self.slug, lambda current: current.update({"seal": seal}),
                                   expected_revision=record["current"]["revision"])
        return seal

    def test_an_unsealed_plan_cannot_start_a_build_and_the_refusal_names_the_lifecycle(self):
        with self.assertRaisesRegex(bc.CoordinatorError, "is not sealed"):
            bc._sealed_plan(self.document["plan_id"])

    def test_a_sealed_plan_yields_its_id_digest_and_payload(self):
        seal = self.seal_it()
        plan_id, sealed_digest, payload = bc._sealed_plan(self.document["plan_id"])
        self.assertEqual(plan_id, self.document["plan_id"])
        self.assertEqual(sealed_digest, seal["sealed_digest"])
        self.assertEqual(bc._digest(payload), bc._digest(plan()))

    def test_a_plan_that_is_not_in_the_library_is_refused_by_name(self):
        with self.assertRaisesRegex(bc.CoordinatorError, "no plan in the local library matches"):
            bc._sealed_plan("pln_ffffffffffff")

    def test_a_plan_that_moved_after_its_seal_is_refused(self):
        self.seal_it()
        record = self.library.read_record(self.slug)
        moved = dict(record["seal"], sealed_digest="sha256:" + "9" * 64)
        self.library.update_record(self.slug, lambda current: current.update({"seal": moved}),
                                   expected_revision=record["current"]["revision"])
        with self.assertRaisesRegex(bc.CoordinatorError, "moved since it was sealed"):
            bc._sealed_plan(self.document["plan_id"])

    def test_binding_records_the_binding_on_the_plan_itself(self):
        seal = self.seal_it()
        bc._record_build_binding(self.document["plan_id"], "owner/repo", 7, seal["sealed_digest"],
                                 seal["build_plan_digest"])
        binding = self.library.read_record(self.slug)["build_binding"]
        self.assertEqual(binding["pull_request"], 7)
        self.assertEqual(binding["repository"], "owner/repo")
        self.assertEqual(binding["sealed_digest"], seal["sealed_digest"])

    def test_a_library_that_cannot_be_written_does_not_strand_the_build(self):
        seal = self.seal_it()
        with mock.patch.object(self.library, "update_record", side_effect=OSError("read-only")), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            bc._record_build_binding(self.document["plan_id"], "owner/repo", 7, seal["sealed_digest"],
                                     seal["build_plan_digest"])
        self.assertIn("could not record the Build binding", err.getvalue())

    def test_cold_restore_is_blocked_when_the_sealed_plan_is_gone(self):
        self.seal_it()
        state = bc._initial_state("owner/repo", 7, BASE, self.document["plan_id"],
                                  self.library.read_record(self.slug)["seal"]["sealed_digest"],
                                  plan(), None)
        self.store.create(state)
        handoff = bc._handoff(self.state())
        handoff_path = Path(self.temp.name) / "handoff.json"
        handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
        restored = bc.StateStore(str(Path(self.temp.name) / "restored.json"))
        with mock.patch.object(bc, "_sealed_plan", side_effect=bc.CoordinatorError("no such plan")), \
                mock.patch.object(bc.repo_identity, "origin_slug", return_value="owner/repo"), \
                mock.patch.object(bc.github, "pr_state", return_value={"number": 7, "state": "OPEN", "headRefOid": bc._head()}), \
                self.assertRaisesRegex(bc.CoordinatorError, "cold continuation is blocked"):
            bc.cmd_handoff_restore(argparse.Namespace(input=str(handoff_path)), restored)

    def test_cold_restore_is_blocked_when_the_seal_changed(self):
        self.seal_it()
        sealed_digest = self.library.read_record(self.slug)["seal"]["sealed_digest"]
        state = bc._initial_state("owner/repo", 7, BASE, self.document["plan_id"], sealed_digest, plan(), None)
        self.store.create(state)
        handoff = bc._handoff(self.state())
        handoff_path = Path(self.temp.name) / "handoff.json"
        handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
        restored = bc.StateStore(str(Path(self.temp.name) / "restored.json"))
        other = "sha256:" + "7" * 64
        with mock.patch.object(bc, "_sealed_plan", return_value=(self.document["plan_id"], other, plan())), \
                mock.patch.object(bc.repo_identity, "origin_slug", return_value="owner/repo"), \
                mock.patch.object(bc.github, "pr_state", return_value={"number": 7, "state": "OPEN", "headRefOid": bc._head()}), \
                self.assertRaises(bc.CoordinatorError) as caught:
            bc.cmd_handoff_restore(argparse.Namespace(input=str(handoff_path)), restored)
        # The refusal names the fault AND the way out. A blocked cold continuation with no remedy is a
        # session stuck mid-Build with nothing to read but source, which is how a refusal becomes a trap.
        self.assertIn("sealed plan changed since this Build was bound", str(caught.exception))
        self.assertIn("bind a fresh Build", str(caught.exception))

    def test_cold_restore_is_blocked_with_its_remedy_when_the_payload_moved(self):
        """The seal this snapshot names is the one in the library, but the work inside it is not."""
        self.seal_it()
        sealed_digest = self.library.read_record(self.slug)["seal"]["sealed_digest"]
        state = bc._initial_state("owner/repo", 7, BASE, self.document["plan_id"], sealed_digest, plan(), None)
        self.store.create(state)
        handoff = bc._handoff(self.state())
        handoff_path = Path(self.temp.name) / "handoff.json"
        handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
        restored = bc.StateStore(str(Path(self.temp.name) / "restored.json"))
        moved = plan()
        moved["objective"] = moved["objective"] + " And one sentence more."
        with mock.patch.object(bc, "_sealed_plan", return_value=(self.document["plan_id"], sealed_digest, moved)), \
                mock.patch.object(bc.repo_identity, "origin_slug", return_value="owner/repo"), \
                mock.patch.object(bc.github, "pr_state", return_value={"number": 7, "state": "OPEN", "headRefOid": bc._head()}), \
                self.assertRaises(bc.CoordinatorError) as caught:
            bc.cmd_handoff_restore(argparse.Namespace(input=str(handoff_path)), restored)
        self.assertIn("build payload no longer matches this Build", str(caught.exception))
        self.assertIn("Export the handoff again", str(caught.exception))

    def test_a_pre_cutover_handoff_is_refused_with_its_remedy(self):
        path = Path(self.temp.name) / "legacy.json"
        path.write_text('{"schema_version":"build-handoff.v1"}', encoding="utf-8")
        with self.assertRaisesRegex(bc.CoordinatorError, "predates the sealed-plan cutover"):
            bc.cmd_handoff_restore(argparse.Namespace(input=str(path)), bc.StateStore(str(Path(self.temp.name) / "new.json")))

    def test_restore_without_input_no_longer_reads_the_pr_contract(self):
        with self.assertRaisesRegex(bc.CoordinatorError, "no longer published"):
            bc.cmd_handoff_restore(argparse.Namespace(input=None, repository="owner/repo", pr=7),
                                   bc.StateStore(str(Path(self.temp.name) / "new.json")))

    def test_a_diverged_build_cannot_hand_itself_off_cold(self):
        self.seal_it()
        sealed_digest = self.library.read_record(self.slug)["seal"]["sealed_digest"]
        state = bc._initial_state("owner/repo", 7, BASE, self.document["plan_id"], sealed_digest, plan(), None)
        state["plan"]["diverged_from_seal"] = True
        self.store.create(state)
        with self.assertRaisesRegex(bc.CoordinatorError, "diverged from sealed plan"):
            bc.cmd_handoff_export(argparse.Namespace(output="-"), self.store)


class TestTwoRootTopologyIsConsumedNotReDerived(unittest.TestCase):
    """Where a plan LIVES is a topology question. The resolution shipped with the library; this node's
    job was to CONSUME it, and the two ways that goes wrong are re-deriving it and gating it."""

    def test_the_coordinator_delegates_to_the_library_and_hand_rolls_nothing(self):
        import plan_store
        with mock.patch.object(plan_store, "PlanLibrary") as library:
            bc._library()
        library.assert_called_once_with()   # no root argument: the library resolves its own home
        source = Path(bc.__file__).read_text(encoding="utf-8")
        self.assertNotIn("--git-common-dir", source)
        self.assertNotIn("git_common_dir", source)

    def test_a_mechanic_session_resolves_the_products_own_checkout(self):
        import checkout_health
        import plan_store
        with mock.patch.object(checkout_health, "resolve_product_checkout",
                               return_value=("/product/checkout", "owned")) as resolve:
            root = plan_store.library_root("/mechanic/session")
        resolve.assert_called_once_with("/mechanic/session")
        self.assertTrue(str(root).startswith("/product/checkout"))

    def test_an_ambiguous_root_refuses_rather_than_choosing(self):
        import checkout_health
        import plan_store
        with mock.patch.object(checkout_health, "resolve_product_checkout", return_value=(None, "ambiguous")), \
                self.assertRaises(Exception):
            plan_store.library_root("/mechanic/session")

    def test_resolution_never_consults_the_write_authorization_gate(self):
        # A permission check answering a topology question is how a resolution silently picks the wrong
        # root — and it is the polarity mistake the retired home-repo carve-out made.
        source = Path(bc.__file__).read_text(encoding="utf-8")
        self.assertNotIn("_confidently_home", source)
        library_source = Path(bc._library.__code__.co_filename).read_text(encoding="utf-8")
        for gate in ("engine_write", "is_home_repo"):
            self.assertNotIn(gate, library_source.split("def _library(")[1].split("def _sealed_plan(")[0])


class TestNoPlanReachesGitHub(unittest.TestCase):
    """The negative half of the cutover, asserted against the surface rather than assumed."""

    def _parser_verbs(self):
        parser = bc.parser()
        actions = {a.dest: a for a in parser._actions if hasattr(a, "choices") and a.choices}
        verbs = {}
        for name, action in actions.items():
            for verb, sub in (action.choices or {}).items():
                verbs[verb] = sub
        return verbs

    def test_plan_promote_is_gone(self):
        self.assertFalse(hasattr(bc, "cmd_plan_promote"))
        parser = bc.parser()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["plan", "promote", "--input", "x"])

    def test_bind_has_no_source_flag_and_takes_a_sealed_plan(self):
        parser = bc.parser()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["plan", "bind", "--input", "x", "--source", "issue",
                               "--repository", "owner/repo", "--pr", "7"])
        args = parser.parse_args(["plan", "bind", "--plan", PLAN_ID, "--repository", "owner/repo", "--pr", "7"])
        self.assertEqual(args.plan, PLAN_ID)
        self.assertFalse(hasattr(args, "source"))

    def test_handoff_export_cannot_publish(self):
        parser = bc.parser()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["handoff", "export", "--publish"])

    def test_the_coordinator_no_longer_writes_or_reads_a_plan_on_github(self):
        source = Path(bc.__file__).read_text(encoding="utf-8")
        for gone in ("_publish_issue", "_create_build_issue", "_durable_plan(",
                     "replace_handoff_block", "find_handoff_block"):
            self.assertNotIn(gone, source, gone)

    def test_no_shipped_operator_instruction_names_a_retired_mechanic(self):
        # The runbooks are where a retired verb survives longest, because nothing breaks when they lie.
        # Every .md under .engine/operations/ is checked, not a hand-listed few.
        retired = ("plan promote", "--source issue", "--source session", "handoff export --publish",
                   "promoted Issue plan", "promote the exact plan")
        offenders = []
        for path in sorted((bc.ROOT / ".engine" / "operations").rglob("*.md")):
            text = path.read_text(encoding="utf-8")
            for phrase in retired:
                if phrase in text:
                    offenders.append(f"{path.name}: {phrase}")
        self.assertEqual(offenders, [])

    def test_the_derived_next_step_for_a_sealed_plan_states_the_bind_command(self):
        # The in-tool guidance is shipped instruction too, and it was the loudest stale line of all:
        # it used to tell the operator that handing a sealed plan to a Build was not wired up.
        import project_manager
        record = {"plan_id": PLAN_ID, "current": {"revision": 1, "plan_digest": SEALED}}
        step = project_manager._next_step("sealed", record, [])
        self.assertIn(f"plan bind --plan {PLAN_ID}", step)
        self.assertNotIn("not wired up", step)

    def test_the_dispatch_runbook_documents_the_frontier_the_scheduler_shipped(self):
        # Issue 1064: B1 shipped `work frontier`, the four typed deferral kinds and the admission order
        # with no operator-facing documentation at all.
        text = (bc.ROOT / ".engine" / "operations" / "build-work-dispatch.md").read_text(encoding="utf-8")
        self.assertIn("work frontier --plan", text)
        for kind in ("dependency", "held-resource", "selected-node-conflict", "capacity"):
            self.assertIn(f"`{kind}`", text)
        self.assertIn("critical-path descending", text)
        self.assertIn("Eligibility is not selection", text)


class TestReviewAndFindings(CoordinatorCase):
    """One review on this side, and it is the deliverable review.

    The plan panel and everything that governed it — the panel ledger, the cadence cap, the retrospective
    waiver, the plan stage on `review packet`/`review record` — moved to the plan side with the panel. What
    stays here is the deliverable review, unchanged, and the DISCLOSURE of a plan that was revised away from
    the seal it entered on.
    """

    def setUp(self):
        super().setUp()
        self.seed(); self.approve("thorough")
        self.integrate_all()
        self.store.mutate(lambda s: s.update({"validation": {"commit": HEAD_A, "results": [
            {"id": "self-test", "commit": HEAD_A, "passed": True, "summary": "green"}]}}))

    DELIVERABLE_LENSES = ["spec-conformance", "divergence-hunter", "usability",
                          "technical-integrity", "security-governance"]

    def packet(self, stage="deliverable", head=HEAD_A, roster=None):
        args = argparse.Namespace(stage=stage, plan=str(self.plan_path), impact=None, session_effort="high")
        out = io.StringIO()
        lenses = roster if roster is not None else self.DELIVERABLE_LENSES
        with mock.patch.object(bc, "_installed", return_value=lenses), mock.patch.object(bc, "_head", return_value=head), mock.patch.object(bc, "_base", return_value=BASE), contextlib.redirect_stdout(out):
            bc._packet(args, self.store)
        return json.loads(out.getvalue())

    def receipt_args(self, packet, lens, findings):
        contract = next(item for item in packet["reviewer_contracts"] if item["lens"] == lens)
        return argparse.Namespace(stage=packet["stage"], lens=lens,
                                  packet_digest=packet["packet_digest"],
                                  lens_packet_digest=contract["lens_packet_digest"], finding=findings,
                                  findings_from_file=None, code_execution="none",
                                  delivered_effort="high")

    def complete_panel(self):
        """Run the deliverable panel and receipt every lens, as a real Build does."""
        pkt = self.packet()
        for lens in self.DELIVERABLE_LENSES:
            with contextlib.redirect_stdout(io.StringIO()):
                bc.cmd_review_record(self.receipt_args(pkt, lens, ["F-" + lens]), self.store)
        return pkt

    # --- the plan stage is gone from this side ----------------------------------------

    def test_a_plan_review_packet_is_refused_and_names_where_plan_review_lives(self):
        args = argparse.Namespace(stage="plan", plan=str(self.plan_path), impact=None, session_effort="high")
        with self.assertRaisesRegex(bc.CoordinatorError, "runs one review"):
            bc._packet(args, self.store)

    def test_the_parser_offers_no_plan_stage_and_no_waive_verb(self):
        parser = bc.parser()
        for argv in (["review", "packet", "--stage", "plan", "--plan", "x"],
                     ["review", "record", "--stage", "plan", "--lens", "architecture",
                      "--packet-digest", "x", "--lens-packet-digest", "y", "--code-execution", "none"],
                     ["review", "waive", "--stage", "plan", "--reason", "r", "--adopted-commit", "c"],
                     ["finding", "record", "--id", "F", "--stage", "plan", "--lens", "architecture",
                      "--severity", "nit", "--summary", "s", "--disposition", "rejected",
                      "--rationale", "r", "--does-not-block-this-pr"]):
            with self.subTest(argv=argv[:3]), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit):
                parser.parse_args(argv)
        self.assertFalse(hasattr(bc, "cmd_review_waive"))
        self.assertFalse(hasattr(bc, "_record_plan_panel"))
        self.assertFalse(hasattr(bc, "_plan_review_ready"))

    def test_no_waiver_survives_anywhere_for_any_review(self):
        # BC-12's waiver went with the gate it excused. The state's own shape is the assertion, because a
        # field nothing writes is a field something can start writing again.
        self.assertNotIn("waiver", bc._empty_review())
        self.assertNotIn("waiver", json.dumps(self.state()["reviews"]))
        schema = json.loads((bc.ROOT / ".engine" / "schemas" / "build-state.v2.json").read_text())
        self.assertNotIn("waiver", json.dumps(schema["$defs"]["review_stage"]["properties"]))

    def test_the_build_snapshot_carries_exactly_one_review_stage(self):
        self.assertEqual(set(self.state()["reviews"]), {"deliverable"})
        self.assertNotIn("plan_panels", self.state())

    def test_the_build_protocol_no_longer_declares_a_plan_review_roster(self):
        protocol = json.loads((bc.ROOT / ".engine" / "build-protocol.json").read_text())
        self.assertNotIn("plan_review", protocol)
        self.assertIn("deliverable_review", protocol)

    # --- divergence from the seal, which is what survives of the escalation path -------

    def test_an_authorized_revision_records_the_escalation_and_demands_no_plan_review(self):
        reviewed = self.state()["plan"]["digest"]
        self.write_plan(plan("A materially different intent."))
        with mock.patch.object(bc, "_head", return_value=HEAD_A), contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_plan_revise(self.revise_args(
                operator_change="Operator: the API shape changed; adjust and ship without re-review."),
                self.store)
        state = self.state()
        escalations = state["plan_change_escalations"]
        self.assertEqual(len(escalations), 1)
        self.assertEqual(escalations[0]["reviewed_plan_digest"], reviewed)
        self.assertEqual(escalations[0]["plan_digest"], state["plan"]["digest"])
        with mock.patch.object(bc, "_head", return_value=HEAD_A):
            status = bc._status(state)
        self.assertFalse([x for x in status["required_evidence"] if "plan-review" in x],
                         status["required_evidence"])
        self.assertTrue(any("not re-reviewed" in w and "API shape changed" in w for w in status["warnings"]))

    def test_revising_always_needs_recorded_operator_authority(self):
        # There is no free-iteration window left on the Build side. Every Build enters on a plan that was
        # reviewed and sealed before any code was written, so changing it is always the operator's call.
        self.write_plan(plan("A materially different intent."))
        with mock.patch.object(bc, "_head", return_value=HEAD_A), \
                self.assertRaisesRegex(bc.CoordinatorError, "entered on a sealed plan"):
            bc.cmd_plan_revise(argparse.Namespace(input=str(self.plan_path), operator_change=None), self.store)

    def test_an_authorized_revision_is_recorded_as_a_divergence_from_the_seal(self):
        self.write_plan(plan("A materially different intent."))
        with mock.patch.object(bc, "_head", return_value=HEAD_A), contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_plan_revise(self.revise_args(), self.store)
        state = self.state()
        self.assertTrue(state["plan"]["diverged_from_seal"])
        self.assertEqual(state["plan"]["plan_id"], PLAN_ID)
        self.assertEqual(state["plan"]["sealed_digest"], SEALED)
        self.assertEqual([e["operator_change"] for e in state["plan_change_escalations"]],
                         ["The operator authorized this change."])

    def test_a_revision_clears_the_deliverable_receipts_it_invalidates(self):
        # The escalation authorizes shipping an unreviewed delta; it must not forge review OF that delta.
        self.complete_panel()
        self.assertTrue(self.state()["reviews"]["deliverable"]["receipts"])
        self.write_plan(plan("A materially different intent."))
        with mock.patch.object(bc, "_head", return_value=HEAD_A), contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_plan_revise(self.revise_args(), self.store)
        self.assertEqual(self.state()["reviews"]["deliverable"]["receipts"], [])

    def test_a_divergence_does_not_relax_deliverable_coverage(self):
        self.write_plan(plan("A materially different intent."))
        with mock.patch.object(bc, "_head", return_value=HEAD_A), contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_plan_revise(self.revise_args(), self.store)
        with mock.patch.object(bc, "_head", return_value=HEAD_A):
            status = bc._status(self.state())
        self.assertIn("deliverable-review packet", status["required_evidence"])

    # --- the PR body's plan-review sentence, now read from the sealed plan record ------

    def _with_plan_review(self, review, problem=None):
        """The clause reads the whole RECORD now, not just the review, so that it can tell a plan it
        could not read from a plan that was sealed without one."""
        record = None if review is None else {"plan_review": review, "approval": {"depth": "standard"}}
        return mock.patch.object(bc, "_sealed_plan_record", return_value=(record, problem))

    def test_the_pr_body_states_honestly_what_the_plan_review_was(self):
        state = self.state()
        recorded = {"lenses": ["architecture", "risk-governance"], "findings": []}
        with self._with_plan_review(recorded):
            self.assertIn("Plan review ran before any code", bc._plan_review_clause(state))
            self.assertIn("architecture, risk-governance", bc._plan_review_clause(state))
        with self._with_plan_review(None):
            self.assertIn("No cold plan review is recorded", bc._plan_review_clause(state))
        diverged = json.loads(json.dumps(state))
        diverged["plan"]["diverged_from_seal"] = True
        with self._with_plan_review(recorded):
            clause = bc._plan_review_clause(diverged)
            self.assertIn("what was BUILT differs from it", clause)
            self.assertIn("does not cover the delta", clause)
        with self._with_plan_review(None):
            self.assertIn("no cold plan review is recorded for either",
                          bc._plan_review_clause(diverged))

    def test_a_plan_it_could_not_read_is_never_reported_as_a_plan_with_no_review(self):
        """Two different facts that used to render as one sentence, in the flattering direction. And the
        failure is NAMED, never quoted: the library's read errors carry sibling plan slugs, which are
        private working titles for unrelated work and must not travel to a pull request."""
        clause = None
        with self._with_plan_review(None, problem="no plan matches 'pln_x'; the library holds: secret-slug"):
            clause = bc._plan_review_clause(self.state())
        self.assertIn("could NOT be established", clause)
        self.assertNotIn("No cold plan review is recorded", clause)
        self.assertNotIn("secret-slug", clause, "a private plan slug must never reach the merge surface")

    def test_the_body_cannot_claim_a_plan_was_both_reviewed_and_not_reviewed(self):
        # The contradiction is now impossible by construction rather than by a conditional: a seal is
        # terminal, so a mid-Build change can never be followed by a re-review of the sealed plan. Every
        # escalation line therefore states the un-reviewed delta, with no re-reviewed arm to disagree with.
        self.write_plan(plan("A materially different intent."))
        with mock.patch.object(bc, "_head", return_value=HEAD_A), contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_plan_revise(self.revise_args(), self.store)
        source = Path(bc.__file__).read_text(encoding="utf-8")
        self.assertNotIn("subsequently re-reviewed", source)
        self.assertIn("without re-review", source)

    # --- The Review section is an ACCOUNT, not a ledger (engine-template#1083) ---------------------
    SEVERITIES = ("blocking", "serious", "nit")
    DISPOSITIONS = ("accepted-fixed", "accepted-tracked", "partially-accepted", "rejected", "escalated")

    def test_every_finding_lands_in_exactly_one_of_rendered_or_counted(self):
        """Exhaustively, over the whole space the record permits — not over the cases I thought of.

        The partition is the whole safety argument: a finding that falls through both arms is one the
        operator never sees, and one that lands in both is double-reported. The plan record constrains
        neither severity against disposition nor either against the blocking flag, so the space is the
        full cross-product and the test walks all of it.
        """
        for severity in self.SEVERITIES:
            for disposition in self.DISPOSITIONS:
                for blocks in (True, False):
                    for deferred in (True, False):
                        finding = {"id": "F", "lens": "l", "severity": severity,
                                   "disposition": disposition, "blocks_this_pr": blocks,
                                   "summary": "s"}
                        full, counted = bc.partition_findings([finding], deferred_counts=deferred)
                        self.assertEqual(len(full) + len(counted), 1,
                                         f"{severity}/{disposition}/blocks={blocks} landed in "
                                         f"{len(full)} rendered and {len(counted)} counted")

    def test_what_the_operator_must_still_act_on_is_never_counted(self):
        """Each arm of the union named separately, so removing any one of them fails HERE.

        The plan record permits an escalated finding recorded as non-blocking and a rejected one
        carrying the flag — the plan-side disposer enforces no coherence between the two fields, unlike
        the Build side — so keying on either field alone silently compacts a live operator decision.
        """
        def rendered(**over):
            finding = {"id": "F", "lens": "l", "severity": "nit", "disposition": "accepted-fixed",
                       "blocks_this_pr": False, "summary": "s"}
            finding.update(over)
            return bool(bc.partition_findings([finding])[0])
        self.assertTrue(rendered(blocks_this_pr=True), "a still-blocking finding must render")
        self.assertTrue(rendered(disposition="escalated"), "an escalated finding is an open decision")
        self.assertTrue(rendered(disposition="rejected"), "a rejection is a disagreement to meet")
        self.assertTrue(rendered(disposition="partially-accepted"), "part of it still stands")
        self.assertFalse(rendered(), "a settled nit is counted")
        self.assertFalse(rendered(disposition="accepted-tracked"),
                         "plan-side, tracked work was answered before the code existed")
        self.assertTrue(bc.partition_findings(
            [{"id": "F", "lens": "l", "severity": "nit", "disposition": "accepted-tracked",
              "blocks_this_pr": False, "summary": "s"}], deferred_counts=True)[0],
            "Build-side, deferred work is live risk a remediating session needs named")

    def test_an_unreadable_plan_record_is_not_rendered_as_zero_findings(self):
        state = self.state()
        with self._with_plan_review(None, problem="/Users/someone/.engine/plans/layoffs-q3 is corrupt"):
            lines = bc._plan_finding_lines(state)
        rendered = "\n".join(lines)
        self.assertIn("could not be read", rendered)
        self.assertNotIn("0 finding", rendered, "unreadable is not empty")
        # And never quotes the failure: the library's errors name private plan titles and local paths.
        self.assertNotIn("layoffs-q3", rendered)
        self.assertNotIn("/Users/", rendered)

    def test_the_counts_are_projections_of_what_was_rendered(self):
        findings = [{"id": f"F{i}", "lens": "architecture" if i % 2 else "risk-governance",
                     "severity": "nit" if i % 3 else "serious", "disposition": "accepted-fixed",
                     "blocks_this_pr": False, "summary": "s"} for i in range(9)]
        findings.append({"id": "OPEN", "lens": "l", "severity": "serious", "disposition": "escalated",
                         "blocks_this_pr": True, "summary": "s", "operator_summary": "still open"})
        state = self.state()
        with self._with_plan_review({"lenses": ["l"], "findings": findings}):
            rendered = "\n".join(bc._plan_finding_lines(state))
        self.assertIn("10 finding(s), of which 1 render above", rendered)
        self.assertIn("The other 9 were settled", rendered)
        self.assertIn("still open", rendered)

    def test_the_digest_is_bounded_by_what_is_open_not_by_how_many_were_raised(self):
        """The durable property behind the byte fix: a thorough panel must not be able to crowd the
        author's own account out of the body. Two records, same open findings, wildly different
        settled tails — the rendered size must barely move."""
        def render(settled):
            findings = [{"id": f"S{i}", "lens": "l", "severity": "nit",
                         "disposition": "accepted-fixed", "blocks_this_pr": False,
                         "summary": "x" * 400} for i in range(settled)]
            findings.append({"id": "OPEN", "lens": "l", "severity": "blocking",
                             "disposition": "escalated", "blocks_this_pr": True,
                             "summary": "s", "operator_summary": "the one that matters"})
            state = self.state()
            with self._with_plan_review({"lenses": ["l"], "findings": findings}):
                return "\n".join(bc._plan_finding_lines(state))
        small, huge = render(3), render(200)
        self.assertIn("the one that matters", huge, "the open finding survives at any scale")
        self.assertLess(len(huge.encode()), 1200, "the digest is bounded")
        self.assertLess(len(huge.encode()) - len(small.encode()), 40,
                        "200 settled findings cost no more than a longer number")

    def test_the_qa_ledger_renders_what_is_open_and_counts_the_rest(self):
        """The Build-side half, at the composer. The CLAIM still carries a summary for every finding —
        that contract is untouched — and this only decides which of them reach the body."""
        import build_coordinator_contract as bcc
        from test_build_coordinator_contract import _good_claim, _good_evidence
        claim, evidence = _good_claim(), _good_evidence()
        claim["review"]["finding_summaries"] = [
            {"id": "OPEN-1", "operator_summary": "an unmet objection"},
            {"id": "DEFER-1", "operator_summary": "deferred and still live"},
            {"id": "SETTLED-1", "operator_summary": "settled quietly"},
            {"id": "SETTLED-2", "operator_summary": "also settled"},
        ]
        evidence["qa_finding_split"] = {
            "full_ids": ["OPEN-1", "DEFER-1"], "counted_total": 2,
            "counted_by_stage": {"deliverable": {"count": 2, "severities": "2 nit"}}}
        body = bcc.compose(claim, evidence)
        self.assertIn("an unmet objection", body)
        self.assertIn("deferred and still live", body)
        self.assertNotIn("settled quietly", body, "a settled finding is counted, not transcribed")
        self.assertIn("A further 2 finding(s)", body)
        self.assertIn("2 in deliverable", body)

    def test_an_absent_split_still_renders_every_summary(self):
        """Fail toward disclosure: a composer handed no partition (an older evidence dict, a handoff
        restored across the change) renders the whole ledger rather than silently dropping it."""
        import build_coordinator_contract as bcc
        from test_build_coordinator_contract import _good_claim, _good_evidence
        claim, evidence = _good_claim(), _good_evidence()
        claim["review"]["finding_summaries"] = [{"id": "A", "operator_summary": "first"},
                                                {"id": "B", "operator_summary": "second"}]
        evidence.pop("qa_finding_split", None)
        body = bcc.compose(claim, evidence)
        self.assertIn("first", body)
        self.assertIn("second", body)

    def test_the_build_that_could_not_publish_now_composes_well_inside_the_budget(self):
        """The instance behind the property: StarshipSuperjam/engine-template#1080 refused at 64,313
        bytes with its author's account already cut three times. Composed here from that Build's own
        saved claim, snapshot and sealed plan record — skipped rather than faked if they are not on
        this machine, because a fixture invented for this test would prove nothing about that body."""
        import json
        base = Path("/Users/shanekidd/Developer/engine-template/.engine/plans"
                    "/diff-classified-repair-rounds-surface-evidence-s--8ded7b")
        saved = {n: base / p for n, p in (("state", "builds/pr-1080-state.json"),
                                          ("claim", "builds/pr-1080-claim.json"),
                                          ("plan", "builds/pr-1080-build-plan.json"),
                                          ("record", "record.json"))}
        if not all(f.is_file() for f in saved.values()):
            self.skipTest("PR#1080's saved build artifacts are not on this machine")
        loaded = {n: json.loads(f.read_text()) for n, f in saved.items()}
        import build_coordinator_contract as bcc
        import build_coordinator_github as github
        with mock.patch.object(bc, "_sealed_plan_record", return_value=(loaded["record"], None)):
            evidence = bc._assemble_evidence(loaded["state"], loaded["plan"], loaded["claim"],
                                             loaded["state"]["repair"]["final_commit"], {})
            body = bcc.compose(loaded["claim"], evidence)
        size = len(body.encode())
        self.assertLess(size, github.GITHUB_BODY_BUDGET_BYTES, f"still over budget at {size}")
        # Not merely under: back to the size the 520 hand-written merges ran at, with the account whole.
        self.assertLess(size, 30_000, f"{size} bytes is still the ledger genre, not an account")

    def test_a_plan_reviews_findings_and_disagreements_reach_the_merge_surface(self):
        # The disclosure the panel move must not drop: what the plan review found, how it was answered, and
        # any blocking finding that was decided not to block.
        recorded = {"lenses": ["risk-governance"], "findings": [
            {"id": "RISK-1", "lens": "risk-governance", "severity": "blocking",
             "summary": "internal detail", "disposition": "accepted-tracked",
             "rationale": "private", "blocks_this_pr": False,
             "operator_summary": "The store is writable by anything on this workstation."},
            {"id": "ARCH-2", "lens": "architecture", "severity": "serious",
             "summary": "a seam is wrong", "disposition": "accepted-fixed", "rationale": "fixed",
             "blocks_this_pr": False},
        ]}
        state = self.state()
        with self._with_plan_review(recorded):
            lines = bc._plan_finding_lines(state)
            disagreements = bc._plan_disagreement_lines(state)
        # The disclosure is what must not drop, not the transcript. Neither of these is something the
        # operator must still act on: RISK-1 was blocking and was answered (so it is a DISAGREEMENT,
        # and reaches the surface in full through its own renderer); ARCH-2 was fixed. So the finding
        # lines carry the account -- both counted, neither transcribed -- and the disagreement line
        # still carries RISK-1's operator-safe text verbatim, which is the guarantee that matters.
        rendered = "\n".join(lines)
        self.assertNotIn("`ARCH-2`", rendered, "a settled finding is counted, not transcribed")
        self.assertIn("2 finding(s), of which 0 render above", rendered)
        self.assertIn("The other 2 were settled", rendered)
        self.assertIn("never published", rendered, "the plan library's locality is disclosed, not implied")
        self.assertEqual(len(disagreements), 1)
        self.assertIn("RISK-1", disagreements[0])
        self.assertIn("writable by anything", disagreements[0])
        # ...and the internal summary never travels when an operator-safe one exists.
        self.assertNotIn("internal detail", "\n".join(lines + disagreements))

    def test_a_plan_finding_is_immune_to_build_side_receipt_supersession(self):
        # Structural immunity, not a flag: plan findings never enter state["findings"], so the rule that
        # strips a finding no live receipt demands cannot reach them. This is the silent-drop the review
        # located, asserted by driving the mechanism that used to do the dropping.
        recorded = {"lenses": ["risk-governance"], "findings": [
            {"id": "RISK-1", "lens": "risk-governance", "severity": "blocking", "summary": "s",
             "disposition": "accepted-tracked", "rationale": "r", "blocks_this_pr": False,
             "operator_summary": "A residual the operator must weigh."}]}
        pkt = self.complete_panel()
        for lens in self.DELIVERABLE_LENSES:
            with contextlib.redirect_stdout(io.StringIO()):
                bc.cmd_finding_record(argparse.Namespace(
                    id="F-" + lens, stage="deliverable", lens=lens, severity="nit", summary="s",
                    disposition="rejected", rationale="r", escalation_kind=None, blocks_this_pr_stated=False,
                    handoff_summary="s", operator_summary=None, private_reference=None), self.store)
        # Regenerate the deliverable packet against MOVED reviewer contracts, which is what supersedes
        # Build-side findings — the mechanism whose reach over plan findings is the subject here.
        moved = [{"lens": lens, "path": f"test-reviewer/{lens}.md", "digest": "sha256:" + "9" * 64}
                 for lens in self.DELIVERABLE_LENSES]
        regenerated = self.packet(roster=moved)
        self.assertNotEqual(regenerated["packet_digest"], pkt["packet_digest"])
        state = self.state()
        self.assertTrue(any(f.get("superseded") for f in state["findings"]),
                        "fixture sanity: regeneration does supersede Build-side findings")
        with self._with_plan_review(recorded):
            self.assertEqual(len(bc._plan_disagreement_lines(state)), 1)
            self.assertEqual(len(bc._plan_finding_lines(state)), 1)

    def _with_plan_document(self, document):
        library = mock.Mock()
        library.resolve.return_value = "a-plan--abc123"
        library.head.return_value = document
        return mock.patch.object(bc, "_library", return_value=library)

    def test_carried_obligations_render_from_the_sealed_plan_record(self):
        """The carry-forward guarantee reaches the operator who approves the merge.

        It is enforced where plans are written — a place that operator never looks — so the same
        record is rendered onto the merge surface: a release states its reason there, and a carried
        obligation names where it went.
        """
        document = {"program": {"program_id": "prg_aaaaaaaaaaaa", "carried_obligations": [
            {"id": "OB-CANON", "state": "carried",
             "statement": "Amend eADR-0025 and eADR-0041 on plan authority."},
            {"id": "OB-SPEC-REACCEPT", "state": "released",
             "statement": "Re-accept the settled specification documents.",
             "reason": "The corpus is stale; re-accepting it would record assent to text that no "
                       "longer describes the engine."},
        ]}}
        with self._with_plan_document(document):
            lines = bc._plan_obligation_lines(self.state())
        self.assertEqual(len(lines), 2)
        self.assertIn("**OB-CANON**", lines[0])
        self.assertIn("_carried_", lines[0])
        self.assertIn("_released_", lines[1])
        self.assertIn("The corpus is stale", lines[1])

    def test_a_standalone_plan_carries_no_obligations(self):
        # A plan belongs to no program unless someone deliberately put it in one, so the ordinary case
        # is an empty list — never an invented heading over nothing.
        for document in ({}, {"program": {"program_id": "prg_aaaaaaaaaaaa"}},
                         {"program": {"program_id": "prg_aaaaaaaaaaaa", "carried_obligations": []}}):
            with self._with_plan_document(document):
                self.assertEqual(bc._plan_obligation_lines(self.state()), [])

    def test_an_unreadable_library_does_not_block_composing_the_pr_body(self):
        # Same rule as the plan review beside it: the obligations are worth disclosing, and none of
        # them is worth stranding a finished Build at the moment it tries to describe itself.
        broken = mock.Mock()
        broken.resolve.side_effect = RuntimeError("the library is gone")
        with mock.patch.object(bc, "_library", return_value=broken):
            self.assertEqual(bc._plan_obligation_lines(self.state()), [])

    def test_obligations_are_immune_to_build_side_receipt_supersession(self):
        # Structural, like the plan findings: they are read from the plan record at compose time and
        # never enter build-state, so nothing in the Build's own receipt bookkeeping can strip them.
        state = self.state()
        self.assertNotIn("obligations", state)
        self.assertNotIn("carried_obligations", json.dumps(state))

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
        self.store.mutate(lambda s: s.update({"validation": None}))
        args = argparse.Namespace(stage="deliverable", plan=str(self.plan_path), impact=None, session_effort="high")
        with mock.patch.object(bc, "_installed", return_value=["spec-conformance"]), mock.patch.object(bc, "_head", return_value=HEAD_A), self.assertRaisesRegex(bc.CoordinatorError, "green candidate validation"):
            bc._packet(args, self.store)

    def test_deliverable_packet_captures_a_checkout_baseline(self):
        # the deliverable review packet snapshots the checkout so the submission preflight can verify the
        # review fan-out did not mutate it (StarshipSuperjam/engine-template#947).
        self.store.mutate(lambda s: s.update({"checkout_snapshot": None}))
        self.assertIsNone(self.state()["checkout_snapshot"])
        self.packet(stage="deliverable")
        snap = self.state()["checkout_snapshot"]
        self.assertIsNotNone(snap, "the deliverable packet captures a checkout baseline")
        self.assertEqual(snap["checkout"], str(bc.ROOT))

    def test_unchanged_packet_reissue_refreshes_checkout_baseline(self):
        # re-issuing an identical deliverable packet marks a fresh review fan-out, so the baseline must be
        # re-captured even though receipts/findings are preserved (the unchanged path).
        self.store.mutate(lambda s: s.update({"validation": {"commit": HEAD_A, "results": [
            {"id": "ci", "commit": HEAD_A, "passed": True, "summary": "ok"}]}}))
        self.packet(stage="deliverable")
        self.store.mutate(lambda s: s.update({"checkout_snapshot": {
            "checkout": "stale", "origin": None, "branch": None, "head": None,
            "stash_count": None, "worktrees": None}}))
        self.packet(stage="deliverable")  # identical digest -> unchanged path
        self.assertEqual(self.state()["checkout_snapshot"]["checkout"], str(bc.ROOT),
                         "the unchanged re-issue refreshed the checkout baseline")

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
        args = argparse.Namespace(stage="deliverable", plan=str(self.plan_path), impact=None, session_effort="high",
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

    def test_standalone_packet_needs_no_pr_or_snapshot(self):
        args = argparse.Namespace(stage="deliverable", plan=str(self.plan_path), impact=None, session_effort="high", standalone=True,
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
        args = argparse.Namespace(stage="deliverable", plan=str(self.plan_path), impact=None, session_effort="high",
                                  standalone=False, output=None, json=False)
        pins = lambda: (mock.patch.object(bc, "_installed", return_value=[]),
                        mock.patch.object(bc, "_head", return_value=HEAD_A),
                        mock.patch.object(bc, "_base", return_value=BASE))
        concise = io.StringIO()
        with contextlib.ExitStack() as es:
            for pin in pins():
                es.enter_context(pin)
            es.enter_context(contextlib.redirect_stdout(concise))
            bc._packet(args, self.store)
        self.assertEqual(len(concise.getvalue().splitlines()), 1)
        args.json = True
        verbose = io.StringIO()
        with contextlib.ExitStack() as es:
            for pin in pins():
                es.enter_context(pin)
            es.enter_context(contextlib.redirect_stdout(verbose))
            bc._packet(args, self.store)
        self.assertIn('"raw_intent"', verbose.getvalue())

    def test_retrying_identical_packet_preserves_receipts_and_findings(self):
        packet = self.packet()
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_review_record(self.receipt_args(packet, "spec-conformance", ["PI-1"]), self.store)
            bc.cmd_finding_record(argparse.Namespace(id="PI-1", stage="deliverable", lens="spec-conformance", severity="nit", summary="Concern", disposition="rejected", rationale="Evidence disproves it.", escalation_kind=None, blocks_this_pr_stated=False, handoff_summary=None), self.store)
        before = self.state()
        retried = self.packet()
        after = self.state()
        self.assertEqual(retried["packet_digest"], packet["packet_digest"])
        self.assertEqual(after["reviews"]["deliverable"]["receipts"], before["reviews"]["deliverable"]["receipts"])
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
        args = self.receipt_args(packet, "spec-conformance", [])
        args.lens_packet_digest = "sha256:" + "f" * 64
        with self.assertRaisesRegex(bc.CoordinatorError, "attest"):
            bc.cmd_review_record(args, self.store)

    def test_review_receipt_inventory_drives_disposition_completeness(self):
        packet = self.packet()
        args = self.receipt_args(packet, "spec-conformance", ["PI-1"])
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_review_record(args, self.store)
        with mock.patch.object(bc, "_head", return_value=HEAD_A):
            result = bc._status(self.state())
        self.assertIn("finding disposition: PI-1", result["required_evidence"])

    def test_wrong_lens_disposition_does_not_satisfy_receipt(self):
        packet = self.packet()
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_review_record(self.receipt_args(packet, "spec-conformance", ["PI-1"]), self.store)
            bc.cmd_finding_record(argparse.Namespace(id="PI-1", stage="deliverable", lens="divergence-hunter", severity="nit", summary="Different finding", disposition="rejected", rationale="Not the declared finding.", escalation_kind=None, blocks_this_pr_stated=False, handoff_summary=None), self.store)
        self.assertEqual(bc._missing_findings(self.state()), ["PI-1"])

    def test_severity_does_not_choose_remedy_or_blocking_posture(self):
        packet = self.packet()
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_review_record(self.receipt_args(packet, "spec-conformance", ["PI-1"]), self.store)
            bc.cmd_finding_record(argparse.Namespace(id="PI-1", stage="deliverable", lens="spec-conformance", severity="blocking", summary="Reviewer concern", disposition="rejected", rationale="The evidence disproves it.", escalation_kind=None, blocks_this_pr_stated=False, handoff_summary=None, operator_summary="The concern was rejected because the cited evidence does not support it.", private_reference=None), self.store)
        finding = self.state()["findings"][0]
        self.assertEqual(finding["severity"], "blocking")
        self.assertEqual(finding["disposition"], "rejected")
        self.assertFalse(finding["blocks_this_pr"])

    def test_partial_acceptance_keeps_bounded_remedy(self):
        packet = self.packet()
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_finding_record(argparse.Namespace(id="A-1", stage="deliverable", lens="divergence-hunter", severity="serious", summary="Concern", disposition="partially-accepted", rationale="Accept the failure case, reject the proposed new subsystem.", escalation_kind=None, blocks_this_pr_stated=False, handoff_summary="Bounded remedy chosen."), self.store)
        self.assertEqual(self.state()["findings"][0]["disposition"], "partially-accepted")

    def test_escalation_names_an_operator_owned_boundary(self):
        self.packet()
        args = argparse.Namespace(id="A-2", stage="deliverable", lens="divergence-hunter", severity="serious",
                                  summary="Boundary", disposition="escalated", rationale="Changes authority.",
                                  escalation_kind=None, blocks_this_pr_stated=True, handoff_summary=None)
        with self.assertRaisesRegex(bc.CoordinatorError, "operator-owned"):
            bc.cmd_finding_record(args, self.store)

    def _batch_file(self, findings, stage="deliverable"):
        path = Path(tempfile.mkdtemp()) / "batch.json"
        path.write_text(json.dumps({"schema_version": "build-findings-batch.v1", "stage": stage,
                                    "findings": findings}), encoding="utf-8")
        return str(path)

    def test_one_file_records_the_receipt_ids_and_their_dispositions_together(self):
        """The end of the failure that started this: the receipt's ids and the findings recorded against
        them come from the same file, so they cannot disagree
        (StarshipSuperjam/engine-template#1060)."""
        packet = self.packet()
        batch = self._batch_file([
            {"id": "B-1", "lens": "usability", "severity": "nit", "summary": "wording",
             "disposition": "accepted-fixed", "rationale": "fixed", "blocks_this_pr": False},
            {"id": "B-2", "lens": "usability", "severity": "serious", "summary": "friction",
             "disposition": "accepted-tracked", "rationale": "tracked", "blocks_this_pr": False}])
        args = self.receipt_args(packet, "usability", None)
        args.findings_from_file = batch
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            bc.cmd_review_record(args, self.store)
            bc.cmd_finding_record(argparse.Namespace(stage="deliverable", id=None, from_file=batch),
                                  self.store)
        receipt = next(r for r in self.state()["reviews"]["deliverable"]["receipts"]
                       if r["lens"] == "usability")
        self.assertEqual(receipt["finding_ids"], ["B-1", "B-2"])
        self.assertEqual(sorted(f["id"] for f in self.state()["findings"]), ["B-1", "B-2"])
        self.assertEqual(bc._missing_findings(self.state()), [])

    def test_a_batch_with_one_bad_entry_leaves_the_snapshot_untouched(self):
        packet = self.packet()
        good = {"id": "B-1", "lens": "usability", "severity": "nit", "summary": "wording",
                "disposition": "accepted-fixed", "rationale": "fixed", "blocks_this_pr": False}
        bad = {**good, "id": "B-2", "disposition": "accepted-fixed", "blocks_this_pr": True}
        args = self.receipt_args(packet, "usability", ["B-1", "B-2"])
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            bc.cmd_review_record(args, self.store)
        before = self.state()["findings"]
        with self.assertRaises(bc.CoordinatorError):
            bc.cmd_finding_record(argparse.Namespace(
                stage="deliverable", id=None, from_file=self._batch_file([good, bad])), self.store)
        self.assertEqual(self.state()["findings"], before,
                         "a batch is validated whole before anything is written, so a bad entry at the "
                         "end must not leave the good ones half-recorded")

    def test_a_panel_that_would_under_deliver_the_approved_depth_is_refused_at_spawn(self):
        """The B1 shape (StarshipSuperjam/engine-template#1067): a sealed `thorough` promises high-effort
        reviewers, but on this runtime an un-pinned reviewer inherits the SESSION's effort — so a medium
        session cannot deliver it. Refused here, where the session still holds both exits: raise its own
        effort and re-cut, or take the depth back to the operator."""
        args = argparse.Namespace(stage="deliverable", plan=str(self.plan_path), impact=None,
                                  session_effort="medium")
        with mock.patch.object(bc, "_installed", return_value=self.DELIVERABLE_LENSES), \
                mock.patch.object(bc, "_head", return_value=HEAD_A), \
                mock.patch.object(bc, "_base", return_value=BASE), \
                self.assertRaises(bc.CoordinatorError) as caught:
            bc._packet(args, self.store)
        message = str(caught.exception)
        self.assertIn("reports running at medium", message)
        self.assertIn("re-approve at the depth", message)
        self.assertIn("--accept-effort-shortfall", message)

    def test_a_packet_that_does_not_state_its_effort_is_refused_rather_than_assumed(self):
        """The assumption this replaces was that a session defaults to `high`. It stopped being true the
        moment running a build at `medium` became a rational choice, and nothing noticed."""
        args = argparse.Namespace(stage="deliverable", plan=str(self.plan_path), impact=None)
        with mock.patch.object(bc, "_installed", return_value=self.DELIVERABLE_LENSES), \
                mock.patch.object(bc, "_head", return_value=HEAD_A), \
                mock.patch.object(bc, "_base", return_value=BASE), \
                self.assertRaises(bc.CoordinatorError) as caught:
            bc._packet(args, self.store)
        self.assertIn("--session-effort", str(caught.exception))

    def test_proceeding_under_a_known_shortfall_is_recorded_for_the_merge_surface(self):
        """The other exit. It is not free: the gap is kept on the stage and published as a hard line in
        the pull-request body, where the operator meets it at merge."""
        args = argparse.Namespace(stage="deliverable", plan=str(self.plan_path), impact=None,
                                  session_effort="medium", accept_effort_shortfall=True)
        with mock.patch.object(bc, "_installed", return_value=self.DELIVERABLE_LENSES), \
                mock.patch.object(bc, "_head", return_value=HEAD_A), \
                mock.patch.object(bc, "_base", return_value=BASE), \
                contextlib.redirect_stdout(io.StringIO()):
            bc._packet(args, self.store)
        stage = self.state()["reviews"]["deliverable"]
        self.assertEqual(stage["session_effort"], "medium")
        self.assertTrue(stage["effort_shortfall_accepted"])

    def test_a_receipt_records_what_its_lens_read_and_what_it_ran_at(self):
        packet = self.packet()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            bc.cmd_review_record(self.receipt_args(packet, "usability", []), self.store)
        receipt = next(r for r in self.state()["reviews"]["deliverable"]["receipts"]
                       if r["lens"] == "usability")
        self.assertEqual(receipt["delivered_effort"], "high")
        self.assertEqual(receipt["reviewed_range"], {"base": BASE, "tip": HEAD_A})

    def test_engineering_blocker_remains_orchestrator_work(self):
        packet = self.packet()
        with contextlib.redirect_stdout(io.StringIO()):
            # `partially-accepted`, not `accepted-fixed`: a finding that still BLOCKS cannot also be
            # dispositioned as settled, and that contradiction is now refused at record time rather than
            # interpreted at the gate (StarshipSuperjam/engine-template#1012). The property under test is
            # unchanged — an engineering blocker is the orchestrator's work, never an operator decision.
            bc.cmd_finding_record(argparse.Namespace(id="A-3", stage="deliverable", lens="divergence-hunter", severity="blocking",
                summary="Engineering repair", disposition="partially-accepted", rationale="Repair stays in approved design.",
                escalation_kind=None, blocks_this_pr_stated=True, handoff_summary=None), self.store)
        with mock.patch.object(bc, "_head", return_value=HEAD_A):
            status = bc._status(self.state())
        self.assertTrue(any("resolve" in item for item in status["engineering_judgment"]))
        self.assertFalse(any("operator decision" in item for item in status["engineering_judgment"]))


class TestArtifactSync(CoordinatorCase):
    """The E4 artifact-preparation transaction and the read-only validation pre-gate."""

    def setUp(self):
        super().setUp()
        self.seed(); self.approve("quick")
        self.integrate_all()

    def test_validate_pre_gate_refuses_on_drift_naming_the_sync_command(self):
        import derived_state
        drift = [derived_state.DriftResult(".engine/self-map.md", "r", "drift", "stale")]
        with mock.patch.object(bc, "_derived_drift", return_value=drift):
            with self.assertRaisesRegex(bc.CoordinatorError, "sync-artifacts"):
                bc.cmd_validate(argparse.Namespace(plan=str(self.plan_path)), self.store)
        self.assertIsNone(self.state()["validation"])   # nothing recorded — refused before StableCommit

    def test_sync_refuses_a_dirty_tree(self):
        with mock.patch.object(bc.core, "dirty_paths", return_value=[" M .engine/tools/x.py"]):
            with self.assertRaisesRegex(bc.CoordinatorError, "clean working tree"):
                bc.cmd_sync_artifacts(argparse.Namespace(), self.store)

    def test_sync_refuses_and_restores_on_an_undeclared_write(self):
        import derived_state
        ok = [derived_state.MemberResult(".engine/self-map.md", "regenerated", True, "wrote")]
        # clean at entry; after regeneration a declared member changed AND an undeclared path leaked.
        dirty = mock.patch.object(bc.core, "dirty_paths",
                                  side_effect=[[], [" M .engine/self-map.md", "?? src/leaked.py"]])
        run = mock.patch.object(bc.core, "run",
                                return_value=types.SimpleNamespace(returncode=0, stdout="", stderr=""))
        with dirty, run as run_mock, \
                mock.patch.object(derived_state, "regenerate", return_value=ok), \
                mock.patch.object(derived_state, "MEMBERS", ()), \
                mock.patch.object(derived_state, "owner_of",
                                  side_effect=lambda p: object() if p == ".engine/self-map.md" else None):
            with self.assertRaisesRegex(bc.CoordinatorError, "outside its declared outputs"):
                bc.cmd_sync_artifacts(argparse.Namespace(), self.store)
        # the restore is SCOPED to this sync's tracked footprint — a checkout of the declared member only,
        # never a whole-tree `git checkout -- .` that would revert a peer's unrelated edit.
        checkouts = [c.args[0] for c in run_mock.call_args_list if c.args[0][:3] == ["git", "checkout", "--"]]
        self.assertTrue(checkouts, "the tracked declared output was not restored")
        for argv in checkouts:
            self.assertNotIn(".", argv[3:], "restore used a whole-tree checkout instead of a scoped one")
            self.assertIn(".engine/self-map.md", argv)
        self.assertIsNone(self.state().get("artifact_sync"))   # no receipt on a refused sync

    def test_sync_records_a_receipt_bound_to_the_sync_commit(self):
        import derived_state
        ok = [derived_state.MemberResult(".engine/self-map.md", "regenerated", True, "wrote")]
        with mock.patch.object(bc.core, "dirty_paths", side_effect=[[], [" M .engine/self-map.md"]]), \
                mock.patch.object(bc.core, "run",
                                  return_value=types.SimpleNamespace(returncode=0, stdout="", stderr="")), \
                mock.patch.object(bc.core, "head", return_value=HEAD_B), \
                mock.patch.object(derived_state, "regenerate", return_value=ok), \
                mock.patch.object(derived_state, "MEMBERS", ()), \
                mock.patch.object(derived_state, "owner_of",
                                  side_effect=lambda p: object() if p == ".engine/self-map.md" else None), \
                contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_sync_artifacts(argparse.Namespace(), self.store)
        receipt = self.state()["artifact_sync"]
        self.assertEqual(receipt["commit"], HEAD_B)
        self.assertEqual(receipt["results"][0]["path"], ".engine/self-map.md")

    def test_a_snapshot_without_the_sync_receipt_is_valid(self):
        # old-snapshot compatibility: the field is optional; a state that never ran sync validates and a
        # plan revision that clears it does not crash.
        self.assertNotIn("artifact_sync", self.state())
        self.store.read()   # re-validates on read; must not raise

    def _real_repo_with_seeded_output(self):
        import derived_state  # noqa: F401
        tmp = Path(tempfile.mkdtemp()); self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        subprocess.run(["git", "init", "-q", str(tmp)], check=True)
        subprocess.run(["git", "-C", str(tmp), "config", "user.email", "e@x"], check=True)
        subprocess.run(["git", "-C", str(tmp), "config", "user.name", "n"], check=True)
        derived = tmp / ".engine" / "self-map.md"
        derived.parent.mkdir(parents=True); derived.write_text("old\n")
        subprocess.run(["git", "-C", str(tmp), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(tmp), "commit", "-qm", "seed"], check=True)
        return tmp, derived

    def _porcelain(self, tmp):
        return subprocess.run(["git", "-C", str(tmp), "status", "--porcelain"],
                              capture_output=True, text=True).stdout

    def test_sync_commits_exactly_the_declared_output_against_real_git(self):
        import derived_state
        tmp, derived = self._real_repo_with_seeded_output()

        def fake_regen(*_a, **_k):
            derived.write_text("new\n")                      # a REAL write to a declared output
            return [derived_state.MemberResult(".engine/self-map.md", "regenerated", True, "wrote")]

        with mock.patch.object(bc, "ROOT", tmp), \
                mock.patch.object(derived_state, "regenerate", side_effect=fake_regen), \
                mock.patch.object(derived_state, "MEMBERS", ()), \
                mock.patch.object(derived_state, "owner_of",
                                  side_effect=lambda p: object() if p == ".engine/self-map.md" else None), \
                contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_sync_artifacts(argparse.Namespace(), self.store)
        self.assertEqual(derived.read_text(), "new\n")        # the regenerated bytes
        self.assertEqual(self._porcelain(tmp), "")            # committed → tree clean
        subject = subprocess.run(["git", "-C", str(tmp), "log", "-1", "--format=%s"],
                                 capture_output=True, text=True).stdout.strip()
        self.assertEqual(subject, "Regenerate derived artifacts")

    def test_sync_restores_exactly_after_an_undeclared_write_against_real_git(self):
        import derived_state
        tmp, derived = self._real_repo_with_seeded_output()
        leaked = tmp / "src" / "leaked.py"

        def fake_regen(*_a, **_k):
            derived.write_text("new\n")                       # declared output changed
            leaked.parent.mkdir(parents=True); leaked.write_text("boom\n")   # AND an undeclared write
            return [derived_state.MemberResult(".engine/self-map.md", "regenerated", True, "wrote")]

        with mock.patch.object(bc, "ROOT", tmp), \
                mock.patch.object(derived_state, "regenerate", side_effect=fake_regen), \
                mock.patch.object(derived_state, "MEMBERS", ()), \
                mock.patch.object(derived_state, "owner_of",
                                  side_effect=lambda p: object() if p == ".engine/self-map.md" else None), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(bc.CoordinatorError, "outside its declared outputs"):
                bc.cmd_sync_artifacts(argparse.Namespace(), self.store)
        # restored EXACTLY: the declared output back to its committed bytes, the leaked file and its empty
        # parent dir gone, the tree clean, and no receipt recorded.
        self.assertEqual(derived.read_text(), "old\n")
        self.assertFalse(leaked.exists())
        self.assertFalse(leaked.parent.exists(), "the empty parent dir of the leaked file was not cleaned")
        self.assertEqual(self._porcelain(tmp), "")
        self.assertIsNone(self.state().get("artifact_sync"))


def _candidate_validation_fake(payload="complete validation output\n", rc=0, record_mutator=None):
    """A fake `_run_validation` for the candidate protocol: writes the log, and — for the command
    carrying `--run-record-path` — a run record consistent with everything the coordinator checks
    against its own derivations (tree, cleanliness, inventory count, log digest). `record_mutator`
    lets a test corrupt exactly one field to prove the corresponding refusal is real."""
    def validation(command, path):
        path.write_text(payload, encoding="utf-8")
        if "--run-record-path" in command:
            record_path = command[command.index("--run-record-path") + 1]
            tree = bc._run(["git", "rev-parse", "HEAD^{tree}"]).stdout.strip()
            count, _ = bc.ci_gatekeeper.inventory_digest(str(bc.ROOT))
            record = {"attests": "engine-selftest", "scope": "full", "tree": tree,
                      "worktree_dirty": False, "inventory": {"module_count": count},
                      "log": {"path": str(path), "sha256": bc._digest(payload.encode())}}
            if record_mutator:
                record_mutator(record)
            Path(record_path).write_text(json.dumps(record), encoding="utf-8")
        return rc
    return validation


class TestValidationRepairAndStatus(CoordinatorCase):
    def setUp(self):
        super().setUp()
        self.seed(); self.approve("quick")
        self.integrate_all()

    def test_validation_records_every_result_against_head(self):
        with mock.patch.object(bc, "_head", return_value=HEAD_A), mock.patch.object(bc, "_derived_drift", return_value=[]), mock.patch.object(bc, "_run_validation", side_effect=_candidate_validation_fake()), contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_validate(argparse.Namespace(plan=str(self.plan_path)), self.store)
        candidate = self.state()["validation"]["candidate"]
        self.assertEqual({r["commit"] for r in candidate["results"]}, {HEAD_A})
        self.assertTrue(all(Path(r["log_path"]).read_text() == "complete validation output\n" for r in candidate["results"]))

    def test_validation_runs_only_registered_commands(self):
        seen = []
        inner = _candidate_validation_fake()
        def validation(command, path):
            seen.append(command); return inner(command, path)
        with mock.patch.object(bc, "_head", return_value=HEAD_A), mock.patch.object(bc, "_derived_drift", return_value=[]), mock.patch.object(bc, "_run_validation", side_effect=validation), contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_validate(argparse.Namespace(plan=str(self.plan_path)), self.store)
        registered = bc._protocol()["validation_commands"]["candidate"]
        self.assertEqual(len(seen), len(registered))
        for ran, item in zip(seen, registered):
            self.assertEqual(len(ran), len(item["command"]))
            for ran_token, registered_token in zip(ran, item["command"]):
                if registered_token in ("{merge_base}", "{run_record_path}"):
                    self.assertNotIn("{", ran_token, "placeholders must be substituted, never passed through")
                else:
                    self.assertEqual(ran_token, registered_token, "only registered tokens may run")

    def test_validation_preserves_complete_logs(self):
        payload = "x" * 5000
        with mock.patch.object(bc, "_head", return_value=HEAD_A), mock.patch.object(bc, "_derived_drift", return_value=[]), mock.patch.object(bc, "_run_validation", side_effect=_candidate_validation_fake(payload=payload)), contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_validate(argparse.Namespace(plan=str(self.plan_path)), self.store)
        for result in self.state()["validation"]["candidate"]["results"]:
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
        self.assertIn("green candidate validation for the final commit", status["required_evidence"])

    def test_status_requires_validation_for_current_head(self):
        self.test_validation_becomes_stale_when_head_changes()

    # --- repair-round escalation (never a cap on review coverage) ----------------------

    def assess(self, judgment, head, lens=None, guidance=None, reviewed=None, classified=None,
               lenses=("usability", "technical-integrity")):
        ns = argparse.Namespace(judgment=judgment, rationale="Round rationale.", lens=lens, guidance=guidance)
        out = io.StringIO()
        with mock.patch.object(bc, "_head", return_value=head), \
             mock.patch.object(bc, "_must_run", return_value="1 file changed"), \
             mock.patch.object(bc, "_required", return_value=[{"lens": x} for x in lenses]), \
             mock.patch.object(bc, "_installed", return_value=list(lenses)), \
             mock.patch.object(repair_divergence, "classify",
                               return_value=classified or classification(authored=["app/main.py"])), \
             mock.patch.object(bc, "_commit_present", return_value=True), \
             mock.patch.object(bc, "_is_ancestor", return_value=True), \
             contextlib.redirect_stdout(out):
            bc.cmd_repair_assess(ns, self.store)
        return out.getvalue()

    def refused_assess(self, judgment, head, lens=None, guidance=None, classified=None):
        """Drive an assess expected to refuse, returning the refusal text."""
        with self.assertRaises(bc.CoordinatorError) as caught:
            self.assess(judgment, head, lens=lens, guidance=guidance, classified=classified)
        return str(caught.exception)

    def receipt_round(self, head):
        """Finish the current round's lenses so the next assess advances to a new commit pair. The receipts
        are schema-faithful on purpose: a thin fake would pass here while the real shape failed."""
        def change(s):
            digest = "sha256:" + "3" * 64
            s["repair"]["receipts"] = [
                {"lens": x, "packet_digest": digest, "referent_digest": digest,
                 "lens_packet_digest": digest, "commit": head, "finding_ids": [],
                 "code_execution": "none"}
                for x in s["repair"]["lenses"]]
        self.store.mutate(change)

    PANEL = ["usability", "technical-integrity"]

    def panel_round(self, head, **kw):
        """A COUNTED round: it dispatches a panel (two or more cold lenses), and finishes it."""
        self.assess("scoped", head, lens=list(self.PANEL), **kw)
        self.receipt_round(head)

    def record_findings(self, rows):
        """Seed schema-faithful findings: (id, stage, commit, lens, severity, blocks_this_pr)."""
        digest = "sha256:" + "4" * 64
        def change(s):
            s["findings"] = [
                {"id": fid, "stage": stage, "lens": lens, "packet_digest": digest,
                 "lens_packet_digest": digest, "commit": commit, "severity": severity,
                 "summary": f"{fid} summary", "disposition": "accepted-fixed",
                 "rationale": "Fixed.", "escalation_kind": None, "blocks_this_pr": blocks,
                 "handoff_summary": None, "operator_summary": None, "private_reference": None}
                for fid, stage, commit, lens, severity, blocks in rows]
        self.store.mutate(change)

    def test_a_none_judgment_is_recorded_but_spends_no_counted_budget(self):
        # A round costs what it SPENDS. `none` dispatches nobody, so it does not consume the counted
        # budget -- but it is still a recorded round, and the absolute ceiling still bounds it, so
        # "no re-review needed" is not the frictionless infinite exit it would otherwise be.
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"reviewed_commit": HEAD_A}))
        self.assess("none", HEAD_B)
        rounds = self.state()["repair_rounds"]
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0]["judgment"], "none")
        self.assertFalse(rounds[0]["counted"])

    def test_a_single_lens_cold_check_spends_no_counted_budget(self):
        # The cheap check the operator asked for: one cold context on a low-yield change. "Minimal" is
        # DERIVED from the recorded roster, never declared by the session that benefits from declaring it.
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"reviewed_commit": HEAD_A}))
        self.assess("scoped", HEAD_B, lens=["usability"])
        self.assertFalse(self.state()["repair_rounds"][0]["counted"])

    def test_a_panel_round_spends_counted_budget(self):
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"reviewed_commit": HEAD_A}))
        self.assess("scoped", HEAD_B, lens=list(self.PANEL))
        self.assertTrue(self.state()["repair_rounds"][0]["counted"])

    def test_the_fourth_counted_round_stops_for_operator_guidance(self):
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"reviewed_commit": HEAD_A}))
        self.panel_round(HEAD_B); self.panel_round(HEAD_C); self.panel_round(HEAD_D)
        self.assertEqual(sum(1 for r in self.state()["repair_rounds"] if r["counted"]), 3)
        message = self.refused_assess("scoped", HEAD_E, lens=list(self.PANEL))
        self.assertIn("--guidance", message)
        self.assertIn("counted budget", message)
        self.assertIn("How the rounds have gone", message)

    def test_an_uncounted_round_after_the_counted_budget_still_runs(self):
        # The point of counting by spend: a cheap cold check on a low-yield change is not what runs an
        # unattended session into the ground, so it is not what the counted cap should stop.
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"reviewed_commit": HEAD_A}))
        self.panel_round(HEAD_B); self.panel_round(HEAD_C); self.panel_round(HEAD_D)
        self.assess("scoped", HEAD_E, lens=["usability"])
        self.assertEqual(len(self.state()["repair_rounds"]), 4)

    def test_the_absolute_ceiling_stops_a_mixed_run_of_cheap_rounds(self):
        # Six rounds of ANY kind is the wall the operator asked for: "I can't go afk and come back to 21
        # rounds." Uncounted rounds are cheaper, not free.
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"reviewed_commit": HEAD_A}))
        for head in (HEAD_B, HEAD_C, HEAD_D, HEAD_E, HEAD_F, "1" * 40):
            self.assess("none", head)
        self.assertEqual(len(self.state()["repair_rounds"]), 6)
        self.assertEqual(sum(1 for r in self.state()["repair_rounds"] if r["counted"]), 0)
        message = self.refused_assess("none", "2" * 40)
        self.assertIn("--guidance", message)
        self.assertIn("absolute ceiling", message)

    def test_each_round_is_measured_from_the_previous_ROUND_not_the_reviewed_commit(self):
        # The measurement trap this closes: the reviewed commit advances only past a COMPLETED round, so
        # after a `none` round it stays put. Anchoring there would measure every later round cumulatively
        # from it, and each one would look bigger than the last -- a growth signal manufactured by the
        # anchor rather than by the work.
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"reviewed_commit": HEAD_A}))
        self.assess("none", HEAD_B)
        self.assess("none", HEAD_C)
        self.assertEqual(self.state()["repair_rounds"][0]["anchor"], HEAD_A)
        self.assertEqual(self.state()["repair_rounds"][-1]["anchor"], HEAD_B)
        self.assertEqual(self.state()["reviews"]["deliverable"]["reviewed_commit"], HEAD_A)

    def test_a_round_after_a_base_change_is_marked_and_suppresses_the_growth_signal(self):
        # A rebase must never be able to masquerade as a fix that grew.
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"reviewed_commit": HEAD_A}))
        self.panel_round(HEAD_B, classified=classification(authored=["a.py"], churn={"authored": 4, "guarded": 0, "derived": 0, "docs": 0}))
        self.store.mutate(lambda s: s.update({"reconciles": [
            {"from_commit": HEAD_B, "to_commit": HEAD_C, "base_before": BASE, "base_after": HEAD_C,
             "contribution_identical": True, "divergent_paths": [], "anchored_to": HEAD_C}]}))
        out = self.assess("scoped", HEAD_D, lens=list(self.PANEL),
                          classified=classification(authored=["a.py", "b.py"],
                                                    churn={"authored": 400, "guarded": 0, "derived": 0, "docs": 0}))
        latest = self.state()["repair_rounds"][-1]
        self.assertEqual(latest["anchor_note"], "reconcile")
        self.assertEqual(latest["anchor"], HEAD_A)
        # The widening claim is suppressed -- but the suppression is SAID, not silent, so a reader who
        # sees no widening warning knows whether that means "it did not" or "we could not tell".
        self.assertNotIn("widening rather than settling", out)
        self.assertIn("could not be judged", out)

    def test_a_widening_repair_is_highlighted_at_every_assess(self):
        # A highlight, never a stop: the round is recorded and the Build continues. The operator asked for
        # this signal to travel with the escalation rather than to become a second bespoke wall.
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"reviewed_commit": HEAD_A}))
        small = classification(authored=["a.py"], churn={"authored": 6, "guarded": 0, "derived": 0, "docs": 0})
        large = classification(authored=["a.py", "b.py"], churn={"authored": 60, "guarded": 0, "derived": 0, "docs": 0})
        self.panel_round(HEAD_B, classified=small)
        out = self.assess("scoped", HEAD_C, lens=list(self.PANEL), classified=large)
        self.assertIn("HIGHLIGHT", out)
        self.assertIn("6 -> 60 lines", out)
        self.assertEqual(len(self.state()["repair_rounds"]), 2)   # highlighted, not stopped

    def test_a_shrinking_repair_raises_no_highlight(self):
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"reviewed_commit": HEAD_A}))
        large = classification(authored=["a.py", "b.py"], churn={"authored": 60, "guarded": 0, "derived": 0, "docs": 0})
        small = classification(authored=["a.py"], churn={"authored": 6, "guarded": 0, "derived": 0, "docs": 0})
        self.panel_round(HEAD_B, classified=large)
        out = self.assess("scoped", HEAD_C, lens=list(self.PANEL), classified=small)
        self.assertNotIn("HIGHLIGHT", out)

    def test_documentation_and_regeneration_do_not_read_as_a_widening_repair(self):
        # The headline the growth signal watches is code and guarded surface. A round that regenerated a
        # large derived artifact is disclosed, but it is not what a fix breaking something looks like.
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"reviewed_commit": HEAD_A}))
        code = classification(authored=["a.py"], churn={"authored": 6, "guarded": 0, "derived": 0, "docs": 0})
        regen = classification(authored=["a.py"], derived=[".engine/knowledge/graph.json"],
                               churn={"authored": 5, "guarded": 0, "derived": 9000, "docs": 0})
        self.panel_round(HEAD_B, classified=code)
        out = self.assess("scoped", HEAD_C, lens=list(self.PANEL), classified=regen)
        self.assertNotIn("HIGHLIGHT", out)
        self.assertIn("1 derived", out)

    def test_an_unmeasurable_increment_refuses_the_round(self):
        # Fail toward refusal. A defaulted classification would be published to the operator as "this round
        # touched nothing" -- a measurement nobody made.
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"reviewed_commit": HEAD_A}))
        with mock.patch.object(bc, "_head", return_value=HEAD_B), \
             mock.patch.object(bc, "_must_run", return_value="1 file changed"), \
             mock.patch.object(repair_divergence, "classify",
                               side_effect=repair_divergence.DivergenceError("git exploded")), \
             self.assertRaisesRegex(bc.CoordinatorError, "could not be classified"):
            bc.cmd_repair_assess(argparse.Namespace(judgment="none", rationale="r", lens=None,
                                                    guidance=None), self.store)
        self.assertEqual(self.state()["repair_rounds"], [])

    def test_the_classification_never_chooses_the_judgment(self):
        # eADR-0041 BC-16 stands: the coordinator measures and records, the orchestrator judges. A round
        # over guarded surface still records exactly the judgment it was given.
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"reviewed_commit": HEAD_A}))
        self.assess("none", HEAD_B, classified=classification(
            guarded=[".github/workflows/engine-ci.yml"],
            churn={"guarded": 900, "authored": 0, "derived": 0, "docs": 0}))
        latest = self.state()["repair_rounds"][-1]
        self.assertEqual(latest["judgment"], "none")
        self.assertEqual(latest["lenses"], [])
        self.assertEqual(latest["classification"]["files"]["guarded"], [".github/workflows/engine-ci.yml"])

    def test_upgrading_a_free_round_to_a_panel_cannot_slip_past_the_counted_budget(self):
        # The bypass this closes: the replacement path skipped BOTH stops, so a session with its budget
        # spent could record a cheap single-lens round and immediately re-assess the same commit pair as
        # a panel -- a fourth counted round with no operator guidance anywhere.
        # The rounds are deliberately left un-receipted: that keeps the effective reviewed commit stable,
        # which is what makes the same-commit-pair replacement path reachable at all.
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"reviewed_commit": HEAD_A}))
        for head in (HEAD_B, HEAD_C, HEAD_D):
            self.assess("scoped", head, lens=list(self.PANEL))
        self.assess("scoped", HEAD_E, lens=["usability"])            # free, and legal
        message = self.refused_assess("scoped", HEAD_E, lens=list(self.PANEL))
        self.assertIn("counted budget", message)
        self.assertEqual(sum(1 for r in self.state()["repair_rounds"] if r["counted"]), 3)

    def test_re_judging_a_round_that_already_spent_is_still_free(self):
        # ...but a replacement that was ALREADY counted is the same spend judged again, not a new one.
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"reviewed_commit": HEAD_A}))
        for head in (HEAD_B, HEAD_C, HEAD_D):
            self.assess("scoped", head, lens=list(self.PANEL))
        self.assess("full", HEAD_D)
        rounds = self.state()["repair_rounds"]
        self.assertEqual(len(rounds), 3)
        self.assertEqual(rounds[-1]["judgment"], "full")

    def test_a_legacy_round_written_before_spend_counting_counts(self):
        # Fail toward "already spent": an upgrade may hand a Build a stop it did not need, never a budget
        # it already used. Nothing else pins this default.
        legacy = {"reviewed_commit": HEAD_A, "final_commit": HEAD_B, "judgment": "scoped",
                  "lenses": ["usability"], "guidance": None}
        def seed(s):
            s["reviews"]["deliverable"].update({"reviewed_commit": HEAD_A})
            s["repair_rounds"] = [dict(legacy), dict(legacy), dict(legacy)]
        self.store.mutate(seed)
        self.assertTrue(all(bc._round_counted(r) for r in self.state()["repair_rounds"]))
        message = self.refused_assess("scoped", HEAD_C, lens=list(self.PANEL))
        self.assertIn("counted budget", message)

    def test_the_absolute_ceiling_counts_rounds_of_every_kind_together(self):
        # A genuinely MIXED run: panels, a cheap check, and none-judgments reaching the ceiling together.
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"reviewed_commit": HEAD_A}))
        self.panel_round(HEAD_B); self.panel_round(HEAD_C)
        self.assess("scoped", HEAD_D, lens=["usability"]); self.receipt_round(HEAD_D)
        self.assess("none", HEAD_E)
        self.assess("none", HEAD_F)
        self.assess("none", "1" * 40)
        self.assertEqual(len(self.state()["repair_rounds"]), 6)
        self.assertEqual(sum(1 for r in self.state()["repair_rounds"] if r["counted"]), 2)
        message = self.refused_assess("none", "2" * 40)
        self.assertIn("absolute ceiling", message)

    def test_an_amended_commit_is_not_reported_to_the_operator_as_a_base_change(self):
        # Telling an operator "the base moved" when a session merely amended a commit is a false statement
        # about their branch -- and the two facts suppress the growth comparison for different reasons.
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"reviewed_commit": HEAD_A}))
        self.panel_round(HEAD_B)
        args = argparse.Namespace(judgment="scoped", rationale="r", lens=list(self.PANEL), guidance=None)
        out = io.StringIO()
        with mock.patch.object(bc, "_head", return_value=HEAD_C), \
             mock.patch.object(bc, "_must_run", return_value="1 file changed"), \
             mock.patch.object(bc, "_commit_present", return_value=True), \
             mock.patch.object(bc, "_is_ancestor", return_value=False), \
             mock.patch.object(repair_divergence, "classify",
                               return_value=classification(authored=["a.py"])), \
             contextlib.redirect_stdout(out):
            bc.cmd_repair_assess(args, self.store)
        latest = self.state()["repair_rounds"][-1]
        self.assertEqual(latest["anchor_note"], "rewritten")
        self.assertIn("no longer on this branch", out.getvalue())
        self.assertNotIn("the base moved", out.getvalue())
        # ...and the suppressed comparison is SAID, not passed over in silence.
        self.assertIn("could not be judged", out.getvalue())

    def reconcile(self, identical, anchored_to=HEAD_A):
        def seed(s):
            s["reviews"]["deliverable"].update({"reviewed_commit": HEAD_A})
            s["reconciles"] = [{"from_commit": HEAD_C, "to_commit": HEAD_D, "base_before": BASE,
                                "base_after": anchored_to, "contribution_identical": identical,
                                "divergent_paths": [] if identical else ["a.py"],
                                "anchored_to": anchored_to}]
        self.store.mutate(seed)

    def test_a_first_round_after_a_DIVERGENT_reconcile_is_marked_as_spanning_the_rebase(self):
        # The divergent path anchors the review short of the branch tip, so the round after it spans the
        # rebase. The whole rebased branch must not be presented as the size of one fix.
        self.reconcile(identical=False)
        self.assess("scoped", HEAD_B, lens=list(self.PANEL))
        self.assertEqual(self.state()["repair_rounds"][0]["anchor_note"], "reconcile")
        self.assertIn("the base moved", "\n".join(bc._repair_round_lines(self.state())))

    def test_a_first_round_after_a_CLEAN_reconcile_is_not_marked(self):
        # A clean reconcile re-anchors the review onto the post-rebase head, so the round after it measures
        # one fix. Marking it would tell the operator their base moved across a span that crossed no rebase.
        self.reconcile(identical=True)
        self.assess("scoped", HEAD_B, lens=list(self.PANEL))
        self.assertIsNone(self.state()["repair_rounds"][0]["anchor_note"])
        self.assertNotIn("the base moved", "\n".join(bc._repair_round_lines(self.state())))

    def test_a_reconcile_a_later_review_superseded_does_not_mark(self):
        # The reconcile anchored an older review; a fresh deliverable review has since moved the anchor
        # somewhere that reconcile says nothing about.
        self.reconcile(identical=False, anchored_to=HEAD_F)
        self.assess("scoped", HEAD_B, lens=list(self.PANEL))
        self.assertIsNone(self.state()["repair_rounds"][0]["anchor_note"])

    def test_a_branch_reset_cannot_delete_a_dispatched_round_and_refund_its_slot(self):
        # Matching ANY round with this commit pair let a reset back to an older round's head erase that
        # round -- panel, guidance and all -- and hand its counted slot back with both stops skipped.
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"reviewed_commit": HEAD_A}))
        for head in (HEAD_B, HEAD_C, HEAD_D):
            self.assess("scoped", head, lens=list(self.PANEL))
        message = self.refused_assess("scoped", HEAD_B, lens=list(self.PANEL))   # reset back to round 1
        self.assertIn("counted budget", message)
        self.assertEqual(len(self.state()["repair_rounds"]), 3)

    def test_re_judging_a_round_carries_the_operators_answer_forward(self):
        # Dropping it would remove the escalation from the PR body -- and with it the preflight's
        # requirement to carry it -- while the headline went on asserting guidance had been disclosed.
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"reviewed_commit": HEAD_A}))
        for head in (HEAD_B, HEAD_C, HEAD_D):
            self.assess("scoped", head, lens=list(self.PANEL))
        self.assess("scoped", HEAD_E, lens=list(self.PANEL), guidance="Operator: one more, then ship.")
        self.assess("full", HEAD_E)
        self.assertEqual(self.state()["repair_rounds"][-1]["guidance"], "Operator: one more, then ship.")
        self.assertTrue(bc._round_guidance_lines(self.state()))

    def test_recorded_guidance_cannot_hide_the_rest_of_the_pr_body(self):
        # An unterminated comment opener blanks every line rendered after it on GitHub, while a
        # presence-only preflight still finds each required string in the source.
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"reviewed_commit": HEAD_A}))
        self.assess("none", HEAD_B, guidance="operator says continue <!--")
        line = bc._round_guidance_lines(self.state())[0]
        self.assertNotIn("<!--", line)
        self.assertIn("operator says continue", line)

    def test_a_branch_wide_round_is_never_compared_against_a_fix_sized_one(self):
        # The suppression window has to include the EARLIER round: its own number is the branch's, so
        # comparing it either raises a false alarm or reports a false calm.
        self.reconcile(identical=False)
        big = classification(authored=["a.py"], churn={"authored": 400, "guarded": 0, "derived": 0, "docs": 0})
        small = classification(authored=["a.py"], churn={"authored": 20, "guarded": 0, "derived": 0, "docs": 0})
        self.panel_round(HEAD_B, classified=big)      # branch-wide: marked reconcile
        out = self.assess("scoped", HEAD_C, lens=list(self.PANEL), classified=small)
        self.assertIn("could not be judged", out)
        self.assertNotIn("widening rather than settling", out)

    def test_the_growth_highlight_names_only_what_actually_grew(self):
        # Saying "code and guarded surface" when no guarded file moved tells the operator protected
        # surface was touched when it was not -- in the one sentence written to catch their eye.
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"reviewed_commit": HEAD_A}))
        small = classification(authored=["a.py"], churn={"authored": 6, "guarded": 0, "derived": 0, "docs": 0})
        bigger = classification(authored=["a.py", "b.py"],
                                churn={"authored": 60, "guarded": 0, "derived": 0, "docs": 0})
        self.panel_round(HEAD_B, classified=small)
        out = self.assess("scoped", HEAD_C, lens=list(self.PANEL), classified=bigger)
        self.assertIn("moved more code than", out)
        self.assertNotIn("guarded surface", out)

    def test_the_growth_highlight_names_guarded_surface_when_that_is_what_grew(self):
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"reviewed_commit": HEAD_A}))
        small = classification(guarded=[".github/workflows/engine-ci.yml"],
                               churn={"authored": 6, "guarded": 1, "derived": 0, "docs": 0})
        bigger = classification(guarded=[".github/workflows/engine-ci.yml"],
                                churn={"authored": 6, "guarded": 90, "derived": 0, "docs": 0})
        self.panel_round(HEAD_B, classified=small)
        out = self.assess("scoped", HEAD_C, lens=list(self.PANEL), classified=bigger)
        self.assertIn("moved more guarded surface than", out)

    def test_the_rounds_record_says_where_the_lens_roster_came_from(self):
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"reviewed_commit": HEAD_A}))
        self.record_findings([("F1", "deliverable", HEAD_A, "usability", "blocking", False),
                              ("F2", "deliverable", HEAD_A, "technical-integrity", "serious", True)])
        self.assess("scoped", HEAD_B)
        self.assertIn("defaulted to the lenses that raised blockers last time",
                      "\n".join(bc._repair_round_lines(self.state())))
        self.receipt_round(HEAD_B)
        self.assess("scoped", HEAD_C, lens=["usability"])
        self.assertIn("lenses named deliberately", "\n".join(bc._repair_round_lines(self.state())))

    def test_a_carried_answer_never_satisfies_a_stop_it_was_not_given_for(self):
        # The regression this closes: carrying a replaced round's guidance forward put it in front of the
        # stop check, so an answer recorded at the ceiling was inherited by a re-judgment that turned a
        # cheap round into a panel -- spending a counted round the operator was never asked about.
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"reviewed_commit": HEAD_A}))
        for head in (HEAD_B, HEAD_C, HEAD_D):
            self.assess("scoped", head, lens=list(self.PANEL))
        for head in (HEAD_E, HEAD_F, "1" * 40):
            self.assess("scoped", head, lens=["usability"])
        self.assess("scoped", "2" * 40, lens=["usability"],
                    guidance="Operator: one cheap check, then ship.")   # answers the CEILING
        message = self.refused_assess("scoped", "2" * 40, lens=list(self.PANEL))
        self.assertIn("counted budget", message)
        # ...and the answer is still on the record, because it was really given.
        self.assertEqual(self.state()["repair_rounds"][-1]["guidance"],
                         "Operator: one cheap check, then ship.")

    def test_two_identical_rounds_are_never_collapsed_into_one(self):
        # Removing the replaced round by VALUE would drop both of a dict-identical pair, erasing a round
        # from the ledger and refunding its counted slot.
        # Three assesses at the SAME pair, a packet cut between each so every one appends rather than
        # replaces. Rounds 2 and 3 then share every field -- same commits, same anchor (round 1's final
        # commit IS this pair's head), same roster, same classification -- and are dict-identical.
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"reviewed_commit": HEAD_A}))
        for _ in range(3):
            self.assess("scoped", HEAD_B, lens=list(self.PANEL))
            self.store.mutate(lambda s: s["repair"].update({"packet_digest": "sha256:" + "5" * 64}))
        rounds = self.state()["repair_rounds"]
        self.assertEqual(len(rounds), 3)
        self.assertEqual(rounds[1], rounds[2], "the fixture no longer builds an identical pair")
        self.store.mutate(lambda s: s["repair"].update({"packet_digest": None}))
        self.assess("full", HEAD_B)                                     # replaces only the LAST
        self.assertEqual(len(self.state()["repair_rounds"]), 3)

    def test_the_operator_is_told_when_a_guard_registration_could_not_be_read(self):
        # The one operator-facing consequence of the whole guards_read mechanism, and it survived deletion
        # with the suite green.
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"reviewed_commit": HEAD_A}))
        degraded = classification(authored=["a.py"])
        degraded["guards_read"] = False
        self.assess("scoped", HEAD_B, lens=list(self.PANEL), classified=degraded)
        self.assertIn("could not be read", "\n".join(bc._repair_round_lines(self.state())))

    def test_a_classification_with_no_honesty_flag_reads_as_unknown_not_as_clean(self):
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"reviewed_commit": HEAD_A}))
        silent = classification(authored=["a.py"])
        del silent["guards_read"]
        self.assess("scoped", HEAD_B, lens=list(self.PANEL), classified=silent)
        rendered = "\n".join(bc._repair_round_lines(self.state()))
        self.assertIn("whether its guarded count is complete is unknown", rendered)
        # ...and NOT the stronger claim that a read failed, which did not happen.
        self.assertNotIn("could not be read", rendered)

    def test_the_rounds_headline_says_what_widening_is_judged_on(self):
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"reviewed_commit": HEAD_A}))
        self.panel_round(HEAD_B)
        headline = bc._repair_round_lines(self.state())[0]
        self.assertIn("judged on code and guarded surface only", headline)
        self.assertIn("protected at ANY point since the deliverable review", headline)

    def test_free_text_cannot_restructure_the_page_it_is_rendered_into(self):
        # Every construct, not a two-string blacklist: an opener, a bidirectional override and a raw
        # newline all have to come out neutralised.
        hostile = "before <!-- <![CDATA[ \u202e after\nnext line"
        rendered = bc._plain(hostile)
        self.assertNotIn("<", rendered)
        self.assertNotIn("\u202e", rendered)
        self.assertNotIn("\n", rendered)
        self.assertIn("before", rendered)
        self.assertIn("after", rendered)

    def test_a_hostile_path_cannot_break_out_of_its_code_span(self):
        self.assertNotIn("<", bc._fenced("app/<!--evil.py"))
        self.assertNotIn("\n", bc._fenced("app/two\nlines.py"))

    def test_a_plan_change_escalation_cannot_hide_the_rounds_record_below_it(self):
        # It renders one bullet ABOVE the rounds record in the same section, so leaving it raw left the
        # attack open one line away from where it was defended.
        self.store.mutate(lambda s: s.update({"plan_change_escalations": [
            {"reviewed_plan_digest": "sha256:" + "7" * 64, "plan_digest": "sha256:" + "8" * 64,
             "operator_change": "operator said go <!--"}]}))
        rendered = "\n".join(
            f"the executed plan differs from the sealed plan its panel read, on recorded operator "
            f"authority and without re-review: {bc._plain(item['operator_change'])}"
            for item in self.state()["plan_change_escalations"])
        self.assertNotIn("<!--", rendered)
        self.assertIn("operator said go", rendered)

    def test_recorded_guidance_allows_the_next_round_and_is_kept(self):
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"reviewed_commit": HEAD_A}))
        self.panel_round(HEAD_B); self.panel_round(HEAD_C); self.panel_round(HEAD_D)
        self.assess("scoped", HEAD_E, lens=list(self.PANEL),
                    guidance="Operator: narrow to usability and ship.")
        rounds = self.state()["repair_rounds"]
        self.assertEqual(len(rounds), 4)
        self.assertEqual(rounds[-1]["guidance"], "Operator: narrow to usability and ship.")

    def test_an_abandoned_fan_out_counts_as_its_own_round(self):
        # The dedup key (reviewed, final) alone could not tell "upgrading my judgment before anything ran"
        # from "the fan-out was abandoned and restarted": both share the commit pair, so repeated abandoned
        # full fan-outs collapsed into ONE round while costing full price each time.
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"reviewed_commit": HEAD_A}))
        self.assess("scoped", HEAD_B, lens=["usability"])
        # a repair packet was cut: the lenses were dispatched and that round cost real money
        self.store.mutate(lambda s: s["repair"].update({"packet_digest": "sha256:" + "5" * 64}))
        self.assess("full", HEAD_B)
        self.assertEqual(len(self.state()["repair_rounds"]), 2)

    def test_the_ledgers_survive_a_handoff_round_trip_and_a_legacy_restore(self):
        # Unauthorized work is also unverified work: carrying the ledgers across handoff reversed a stated
        # non-goal, so it needs its own coverage in both directions.
        rounds = [{"reviewed_commit": HEAD_A, "final_commit": HEAD_B, "judgment": "scoped",
                   "lenses": ["usability", "technical-integrity"], "guidance": None, "counted": True,
                   "anchor": HEAD_A, "anchor_note": None, "roster_provenance": "blocker-union",
                   "classification": classification(authored=["app/main.py"])}]
        escalations = [{"reviewed_plan_digest": "sha256:" + "7" * 64,
                        "plan_digest": "sha256:" + "8" * 64, "operator_change": "Operator: proceed."}]
        restored = bc._restore_base_state(
            {"build": {}, "plan": {}, "approval": None, "reviews": {}, "finding_summaries": [],
             "progress": {}, "validation": None, "repair": None, "preflights": [], "pr_contract": None,
             "repair_rounds": rounds, "plan_change_escalations": escalations},
            "build-state.v1")
        self.assertNotIn("plan_panels", restored)
        self.assertEqual(restored["repair_rounds"], rounds)
        self.assertEqual(restored["plan_change_escalations"], escalations)
        # A handoff exported BEFORE this change carries none of these keys and must still restore, reading
        # as empty ledgers (a free first panel) rather than raising.
        legacy = bc._restore_base_state(
            {"build": {}, "plan": {}, "approval": None, "reviews": {}, "finding_summaries": [],
             "progress": {}, "validation": None, "repair": None, "preflights": [], "pr_contract": None},
            "build-state.v1")
        self.assertNotIn("plan_panels", legacy)
        self.assertEqual(legacy["repair_rounds"], [])
        self.assertEqual(legacy["plan_change_escalations"], [])

    def test_a_classified_round_survives_a_real_handoff_export(self):
        # Both handoff schemas close their round entries to unknown keys, so adding round evidence without
        # widening them would break cold resumption -- the export refusing on the very state it must carry.
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"reviewed_commit": HEAD_A}))
        self.panel_round(HEAD_B)
        exported = bc._handoff(self.state())
        bc.core.validate(exported, bc.HANDOFF_SCHEMAS[exported["schema_version"]])
        entry = exported["repair_rounds"][0]
        self.assertTrue(entry["counted"])
        self.assertEqual(entry["anchor"], HEAD_A)
        self.assertEqual(entry["roster_provenance"], "explicit")
        self.assertEqual(entry["classification"]["files"]["authored"], ["app/main.py"])
        # ...and the round comes back intact on the other side.
        restored = bc._restore_base_state(exported, "build-state.v2")
        self.assertEqual(restored["repair_rounds"][0], entry)

    def test_reassessing_the_same_divergence_replaces_its_round(self):
        # Upgrading a scoped judgment to full on the SAME reviewed/final pair is one round, not two.
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"reviewed_commit": HEAD_A}))
        self.assess("scoped", HEAD_B, lens=["usability"])
        self.assess("full", HEAD_B)
        rounds = self.state()["repair_rounds"]
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0]["judgment"], "full")

    def test_none_repair_judgment_is_valid_for_small_change(self):
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"packet_digest": "sha256:" + "2" * 64, "reviewed_commit": HEAD_A}))
        self.assess("none", HEAD_B, guidance=None)
        self.assertEqual(self.state()["repair"]["judgment"], "none")

    def test_repair_assessment_records_diff_and_judgment(self):
        self.test_none_repair_judgment_is_valid_for_small_change()
        repair = self.state()["repair"]
        self.assertEqual(repair["summary"], "1 file changed")
        self.assertEqual(repair["classification"]["files"]["authored"], ["app/main.py"])
        self.assertEqual(repair["anchor"], HEAD_A)

    def test_a_scoped_round_defaults_to_the_lenses_that_found_blockers(self):
        # The operator's requirement made mechanical: the next round goes back to the lenses that found
        # blockers, and the UNION is what makes it work -- a blocker that was FIXED is recorded
        # accepted-fixed with blocks_this_pr false, so an intersection would select nobody at all.
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"reviewed_commit": HEAD_A}))
        self.record_findings([
            ("F1", "deliverable", HEAD_A, "usability", "blocking", False),          # raised, then fixed
            ("F2", "deliverable", HEAD_A, "technical-integrity", "serious", True),  # left blocking
            ("F3", "deliverable", HEAD_A, "security-governance", "nit", False),     # neither
        ])
        self.assess("scoped", HEAD_B)
        round_one = self.state()["repair_rounds"][0]
        self.assertEqual(round_one["lenses"], ["technical-integrity", "usability"])
        self.assertEqual(round_one["roster_provenance"], "blocker-union")

    def test_an_explicit_lens_overrides_the_default_and_is_recorded_as_explicit(self):
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"reviewed_commit": HEAD_A}))
        self.record_findings([("F1", "deliverable", HEAD_A, "usability", "blocking", True)])
        self.assess("scoped", HEAD_B, lens=["technical-integrity"])
        round_one = self.state()["repair_rounds"][0]
        self.assertEqual(round_one["lenses"], ["technical-integrity"])
        self.assertEqual(round_one["roster_provenance"], "explicit")

    def test_the_default_roster_reads_the_last_round_that_actually_REVIEWED(self):
        # A `none` round raised no findings. Reading the roster from it would find nothing and refuse,
        # so the lookup walks back to the last round that dispatched lenses.
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"reviewed_commit": HEAD_A}))
        self.record_findings([("F1", "deliverable", HEAD_A, "usability", "blocking", True)])
        self.assess("none", HEAD_B)
        self.assess("scoped", HEAD_C)
        self.assertEqual(self.state()["repair_rounds"][-1]["lenses"], ["usability"])

    def test_scoped_repair_requires_named_lens(self):
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"reviewed_commit": HEAD_A}))
        message = self.refused_assess("scoped", HEAD_B)
        self.assertIn("at least one --lens", message)
        self.assertIn("no finding that was blocking", message)

    def test_repair_review_requires_validation_for_repaired_commit(self):
        self.store.mutate(lambda s: s.update({"repair": {"reviewed_commit": HEAD_A, "final_commit": HEAD_B, "summary": "1 file", "judgment": "scoped", "rationale": "Logic changed", "lenses": ["usability"], "packet_digest": None, "receipts": []}}))
        args = argparse.Namespace(stage="repair", plan=str(self.plan_path), impact=None, session_effort="high")
        with mock.patch.object(bc, "_installed", return_value=["usability"]), self.assertRaisesRegex(bc.CoordinatorError, "green candidate validation"):
            bc._packet(args, self.store)

    def repair_packet(self, anchor=HEAD_A, classified=None):
        self.store.mutate(lambda s: s.update({
            "validation": {"commit": HEAD_B, "results": [{"id": "ci", "commit": HEAD_B, "passed": True, "summary": "ok"}]},
            "repair": {"reviewed_commit": HEAD_A, "final_commit": HEAD_B, "summary": "1 file",
                       "judgment": "scoped", "rationale": "Logic changed", "lenses": ["usability"],
                       "packet_digest": None, "receipts": [], "anchor": anchor,
                       "classification": classified or classification(authored=["app/main.py"])},
        }))
        args = argparse.Namespace(stage="repair", plan=str(self.plan_path), impact=None)
        output = io.StringIO()
        with mock.patch.object(bc, "_installed", return_value=["usability"]), \
             mock.patch.object(bc, "_head", return_value=HEAD_B), \
             mock.patch.object(bc, "_base", return_value=BASE), contextlib.redirect_stdout(output):
            bc._packet(args, self.store)
        return json.loads(output.getvalue())

    def test_the_repair_packet_points_the_reviewer_at_the_repair_not_the_whole_pr(self):
        # base_commit is the MERGE BASE -- the whole pull request. Without this a repair reviewer is
        # handed the entire change again and left to work out which part of it is new.
        packet = self.repair_packet(anchor=HEAD_C)
        self.assertEqual(packet["repair_anchor"], HEAD_C)
        self.assertEqual(packet["base_commit"], BASE)
        self.assertIn(f"{HEAD_C[:12]}..{HEAD_B[:12]}", packet["review_subject"])
        self.assertIn("Review the repair, not the whole pull request", packet["review_subject"])
        self.assertEqual(packet["repair_classification"]["files"]["authored"], ["app/main.py"])

    def test_a_deliverable_packet_carries_no_repair_anchor(self):
        self.store.mutate(lambda s: s.update({
            "validation": {"commit": HEAD_B, "results": [{"id": "ci", "commit": HEAD_B, "passed": True, "summary": "ok"}]}}))
        args = argparse.Namespace(stage="deliverable", plan=str(self.plan_path), impact=None)
        output = io.StringIO()
        with mock.patch.object(bc, "_installed", return_value=["usability"]), \
             mock.patch.object(bc, "_head", return_value=HEAD_B), \
             mock.patch.object(bc, "_base", return_value=BASE), contextlib.redirect_stdout(output):
            bc._packet(args, self.store)
        packet = json.loads(output.getvalue())
        self.assertNotIn("repair_anchor", packet)
        self.assertNotIn("review_subject", packet)

    def test_the_rounds_disclosure_reads_plainly_and_samples_its_paths(self):
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"reviewed_commit": HEAD_A}))
        many = classification(authored=[f"app/f{n}.py" for n in range(9)],
                              derived=[".engine/knowledge/graph.json"],
                              churn={"authored": 12, "derived": 900, "guarded": 0, "docs": 0})
        self.panel_round(HEAD_B, classified=many)
        lines = bc._repair_round_lines(self.state())
        self.assertIn("**Repair rounds.**", lines[0])
        self.assertIn("1 of which dispatched a review panel", lines[0])
        self.assertTrue(lines[1].startswith("  - round 1: counted"))
        sample = next(line for line in lines if line.strip().startswith("- authored:"))
        self.assertEqual(sample.count("`app/f"), bc._ROUND_PATH_SAMPLE)
        self.assertIn("and 5 more", sample)
        self.assertTrue(any("derived: `.engine/knowledge/graph.json`" in line for line in lines))

    def test_no_rounds_means_no_rounds_disclosure(self):
        self.assertEqual(bc._repair_round_lines(self.state()), [])

    def contract_leg(self, body):
        with mock.patch.object(bc, "_pr_contract", return_value=(True, "all sections filled")), \
             mock.patch.object(bc, "_run", return_value=types.SimpleNamespace(returncode=0, stdout="ok", stderr="")), \
             mock.patch.object(bc, "_hard_check_declarations", return_value=[]), \
             mock.patch.object(bc, "_write_json_artifact", return_value=("/dev/null", "sha256:" + "0" * 64)):
            legs = bc._compute_preflight_legs(self.state(), HEAD_B, {"baseRefOid": BASE}, body)
        return next(r for r in legs["results"] if r["id"] == "pr-contract")

    def test_the_pr_contract_leg_refuses_a_body_missing_the_rounds_disclosure(self):
        # Disclosure and gate are wired together on purpose: an otherwise-complete body that has dropped
        # what the repair loop cost must not reach the operator's merge surface.
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"reviewed_commit": HEAD_A}))
        self.panel_round(HEAD_B)
        lines = bc._repair_round_lines(self.state())
        without = self.contract_leg("## Review\n\n- everything else is here\n")
        self.assertFalse(without["passed"])
        self.assertIn("repair-rounds disclosure", without["summary"])
        # The HEADLINE alone is not enough: the per-round detail is the part a reader acts on, so a body
        # that keeps the summary sentence while dropping the rounds beneath it must still be refused.
        headline_only = self.contract_leg(f"## Review\n\n{lines[0]}\n")
        self.assertFalse(headline_only["passed"])
        with_it = self.contract_leg("## Review\n\n" + "\n".join(lines) + "\n")
        self.assertTrue(with_it["passed"])

    def test_the_pr_contract_leg_refuses_a_body_missing_the_growth_highlight(self):
        # The one sentence saying "this repair is not converging" is exactly what a partial recompose
        # would drop, and exactly what the operator most needs at the merge gate.
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"reviewed_commit": HEAD_A}))
        small = classification(authored=["a.py"], churn={"authored": 6, "guarded": 0, "derived": 0, "docs": 0})
        big = classification(authored=["a.py"], churn={"authored": 60, "guarded": 0, "derived": 0, "docs": 0})
        self.panel_round(HEAD_B, classified=small)
        self.panel_round(HEAD_C, classified=big)
        lines = bc._repair_round_lines(self.state())
        highlight = next(line for line in lines if "Worth a look" in line)
        stripped = self.contract_leg("## Review\n\n" + "\n".join(l for l in lines if l != highlight))
        self.assertFalse(stripped["passed"])
        self.assertTrue(self.contract_leg("## Review\n\n" + "\n".join(lines))["passed"])

    def test_the_pr_contract_leg_requires_the_recorded_operator_guidance(self):
        # The rounds headline asserts that guidance was disclosed; the assertion and the thing it asserts
        # have to be gated together, or the body can claim a disclosure it does not carry.
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"reviewed_commit": HEAD_A}))
        self.panel_round(HEAD_B); self.panel_round(HEAD_C); self.panel_round(HEAD_D)
        self.assess("scoped", HEAD_E, lens=list(self.PANEL), guidance="Operator: one more pass, then ship.")
        lines = bc._repair_round_lines(self.state())
        without = self.contract_leg("## Review\n\n" + "\n".join(lines))
        self.assertFalse(without["passed"])
        full = lines + [f"- **Escalation recorded.** {g}" for g in bc._round_guidance_lines(self.state())]
        self.assertTrue(self.contract_leg("## Review\n\n" + "\n".join(full))["passed"])

    def test_repair_findings_are_dispositioned_in_the_repair_stage(self):
        self.store.mutate(lambda s: s.update({
            "validation": {"commit": HEAD_B, "results": [{"id": "ci", "commit": HEAD_B, "passed": True, "summary": "ok"}]},
            "repair": {"reviewed_commit": HEAD_A, "final_commit": HEAD_B, "summary": "1 file", "judgment": "scoped", "rationale": "Logic changed", "lenses": ["usability"], "packet_digest": None, "receipts": []},
        }))
        args = argparse.Namespace(stage="repair", plan=str(self.plan_path), impact=None, session_effort="high")
        output = io.StringIO()
        with mock.patch.object(bc, "_installed", return_value=["usability"]), mock.patch.object(bc, "_head", return_value=HEAD_B), mock.patch.object(bc, "_base", return_value=BASE), contextlib.redirect_stdout(output):
            bc._packet(args, self.store)
        packet = json.loads(output.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            contract = next(item for item in packet["reviewer_contracts"] if item["lens"] == "usability")
            bc.cmd_review_record(argparse.Namespace(stage="repair", lens="usability",
                                                    packet_digest=packet["packet_digest"],
                                                    lens_packet_digest=contract["lens_packet_digest"],
                                                    finding=["R-1"], code_execution="none"), self.store)
            bc.cmd_finding_record(argparse.Namespace(id="R-1", stage="repair", lens="usability", severity="serious", summary="Repair concern", disposition="accepted-fixed", rationale="Directly fixed.", escalation_kind=None, blocks_this_pr_stated=False, handoff_summary=None), self.store)
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
                mock.patch.object(bc, "_derived_drift", return_value=[]), \
                mock.patch.object(bc, "_run_validation", side_effect=validation), \
                self.assertRaisesRegex(bc.CoordinatorError, "changed after"), \
                contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_validate(argparse.Namespace(plan=str(self.plan_path)), self.store)
        self.assertIsNone(self.state()["validation"])

    def test_prescribed_change_after_re_review_uses_latest_reviewed_commit(self):
        def completed_re_review(state):
            state["reviews"]["deliverable"]["reviewed_commit"] = HEAD_A
            state["repair"] = {"reviewed_commit": HEAD_A, "final_commit": HEAD_B, "summary": "material repair", "judgment": "scoped", "rationale": "Usability changed", "lenses": ["usability"], "packet_digest": "sha256:" + "3" * 64,
                               "receipts": [{"lens": "usability", "packet_digest": "sha256:" + "3" * 64, "commit": HEAD_B, "finding_ids": []}]}
        self.store.mutate(completed_re_review)
        with mock.patch.object(bc, "_head", return_value=HEAD_C), mock.patch.object(bc, "_must_run", return_value="1 file changed, 1 insertion(+)"), mock.patch.object(repair_divergence, "classify", return_value=classification(authored=["app/main.py"])), contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_repair_assess(argparse.Namespace(judgment="none", rationale="Direct verification covers the prescribed repair.", lens=None, accept_receipt_loss=True), self.store)
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

    def test_implementation_status_routes_delegation_to_the_scouts(self):
        """The runtime projection names both scouts — and names the runner on FOCUSED verification only.

        The routing's whole claim is that a session meets it at the moment it acts rather than in a
        runbook it may never open, so the runbook's phrase list is not enough on its own: it guards the
        prose, not this output. The second half matters as much as the first. Both coordinator
        validation classes bind evidence to the live tree, and the runner is confined by its own
        containment recipe to a disposable copy, so routing final validation through it would yield a
        readable summary and no admissible evidence. A later edit that moves the runner onto the final
        line would reintroduce exactly that, and this is what catches it.
        """
        with mock.patch.object(bc, "_head", return_value=HEAD_A):
            status = bc._status(self.state())
        activities = status["available_activities"]
        grounding = [a for a in activities if "engine-grounding-scout" in a]
        self.assertEqual(len(grounding), 1, "the grounding scout is named exactly once")
        runner = [a for a in activities if "engine-validation-runner" in a]
        self.assertEqual(len(runner), 1, "the validation runner is named exactly once")
        self.assertIn("focused verification", runner[0],
                      "the runner belongs to focused verification, never to a validation class whose "
                      "evidence binds to the live tree")
        self.assertNotIn("engine-validation-runner",
                         next(a for a in activities if a.startswith("run final validation")))

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

    def test_recording_progress_does_not_change_the_plan_digest(self):
        before = self.state()["plan"]["digest"]
        note = {"objective": "x", "current_work": "x", "work_item": "W1", "assumptions": [],
                "non_goals": [], "planned_scope": [".engine/tools/build_coordinator.py"],
                "remaining_verification": ["tests"], "judgment": "aligned"}
        path = Path(self.temp.name) / "checkpoint.json"; path.write_text(json.dumps(note))
        with mock.patch.object(bc, "_head", return_value=HEAD_A), mock.patch.object(bc, "_changed_paths", return_value=[]), contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_checkpoint(argparse.Namespace(plan=str(self.plan_path), input=str(path), complete_item=None), self.store)
        self.assertEqual(self.state()["plan"]["digest"], before)
        self.assertEqual(self.state()["checkpoint"]["work_item"], "W1")

    def test_routine_enforces_order_and_reports_n_of_m(self):
        value = plan(); value["profile"] = "routine"; value["intent_source"] = {"kind": "issue", "issue": 11}
        value["work_items"].append({"id": "W2", "description": "Second", "paths": ["README.md"],
                                    "verification": ["Read it"], "depends_on": ["W1"],
                                    "exclusive_resources": [], "executor_class": "integrator",
                                    "output_contract": {"deliverable": "The second item",
                                                        "artifact_kinds": ["integrated-commit"],
                                                        "required_evidence": ["changed_paths", "verification_results"]}})
        self.write_plan(value)
        state = bc._initial_state("owner/repo", 7, BASE, PLAN_ID, SEALED, value, 11, "unattended")
        state["approval"] = {"plan_digest": state["plan"]["digest"], "spec_digest": None, "depth": "quick"}
        self.store = bc.StateStore(str(Path(self.temp.name) / "routine.json")); self.store.create(state)
        note = {"objective": "x", "current_work": "second", "work_item": "W2", "assumptions": [],
                "non_goals": [], "planned_scope": ["README.md"], "remaining_verification": [], "judgment": "aligned"}
        note_path = Path(self.temp.name) / "routine-note.json"; note_path.write_text(json.dumps(note))
        with mock.patch.object(bc, "_assert_spec_boundary", return_value={}), self.assertRaisesRegex(bc.CoordinatorError, "next ready work item W1"):
            bc.cmd_checkpoint(argparse.Namespace(plan=str(self.plan_path), input=str(note_path), complete_item="W2", json=False), self.store)
        with mock.patch.object(bc, "_head", return_value=HEAD_A), mock.patch.object(bc, "_installed", return_value=[]):
            status = bc._status(self.state(), value)
        self.assertEqual(status["progress"], {"completed": [], "total": 2, "current": None, "next": "W1"})


class TestCandidateEvidenceAndCache(CoordinatorCase):
    """The split evidence model: the cache's content-addressed identity, the downgrade seams that can
    never mint final evidence, and the run record's refusal paths — each corruption driven singly."""

    def setUp(self):
        super().setUp()
        self.seed(); self.approve("quick")
        self.integrate_all()

    def _validate(self, fake=None, **over):
        calls = []
        inner = fake or _candidate_validation_fake()
        def counting(command, path):
            calls.append(command); return inner(command, path)
        args = {"plan": str(self.plan_path)}
        args.update(over)
        out = io.StringIO()
        with mock.patch.object(bc, "_head", return_value=HEAD_A), \
                mock.patch.object(bc, "_derived_drift", return_value=[]), \
                mock.patch.object(bc, "_run_validation", side_effect=counting), \
                contextlib.redirect_stdout(out):
            bc.cmd_validate(argparse.Namespace(**args), self.store)
        return calls, out.getvalue()

    def test_a_repeat_at_the_same_identity_is_a_cache_hit_that_mutates_nothing(self):
        self._validate()
        before = Path(self.state_path).read_bytes()
        calls, out = self._validate()
        self.assertEqual(calls, [], "a cache hit must re-run nothing")
        self.assertIn('"cached": true', out)
        self.assertEqual(Path(self.state_path).read_bytes(), before,
                         "a cache hit performs no state mutation at all")

    def test_force_always_re_runs(self):
        self._validate()
        calls, _ = self._validate(force=True)
        self.assertEqual(len(calls), 2, "--force must re-run every registered candidate command")

    def test_every_identity_component_is_load_bearing(self):
        """Corrupting any single stored component — commit, merge base, protocol digest, an argv
        digest, the inventory digest — must miss the cache and re-run."""
        self._validate()
        components = ["commit", "merge_base", "protocol_digest", "argv_digests", "inventory_digest"]
        for component in components:
            with self.subTest(component=component):
                def corrupt(state, key=component):
                    candidate = state["validation"]["candidate"]
                    if key == "argv_digests":
                        candidate[key] = {list(candidate[key])[0]: bc._digest(b"other")}
                    elif key in ("commit", "merge_base"):
                        candidate[key] = HEAD_B
                    else:
                        candidate[key] = bc._digest(b"other")
                self.store.mutate(corrupt)
                calls, _ = self._validate()
                self.assertEqual(len(calls), 2, f"a changed {component} must be a cache miss")

    def test_a_red_candidate_never_hits_the_cache(self):
        with self.assertRaises(bc.CoordinatorError):
            self._validate(fake=_candidate_validation_fake(rc=1))
        calls, _ = self._validate()
        self.assertEqual(len(calls), 2, "only a green candidate may satisfy a repeat")

    def test_a_legacy_slot_normalizes_to_null_identity_and_never_hits(self):
        """The downgrade seam: an old single-slot snapshot stays readable, stands behind packets at
        its commit, and can never be a cache hit or final evidence."""
        legacy = {"commit": HEAD_A, "results": [{"id": "engine-ci", "commit": HEAD_A,
                                                 "passed": True, "summary": "ok"}]}
        self.store.mutate(lambda s: s.update({"validation": legacy}))
        state = self.state()
        split = bc._split_validation(state)
        self.assertIsNone(split["candidate"]["protocol_digest"])
        self.assertIsNone(split["final"])
        self.assertTrue(bc._candidate_ok(state, HEAD_A))
        self.assertFalse(bc._final_ok(state, HEAD_A))
        calls, _ = self._validate()
        self.assertEqual(len(calls), 2, "null identity describes nothing and must never hit")

    def test_no_coordinator_write_path_mints_final_evidence(self):
        """`validate` preserves a same-head final and DROPS a different-head one; it never creates
        one. The only writer of `final` is the import verb — held here at the state level, and by
        the source audit below."""
        final = {"commit": HEAD_A, "source": "ci-import", "run_id": 5,
                 "context": "engine-ci", "tree": "b" * 40}
        self._validate()
        self.store.mutate(lambda s: s["validation"].update({"final": final}))
        self._validate(force=True)
        self.assertEqual(bc._split_validation(self.state())["final"], final,
                         "a same-head candidate re-run preserves valid final evidence")
        self.store.mutate(lambda s: s["validation"]["final"].update({"commit": HEAD_B}))
        self._validate(force=True)
        self.assertIsNone(bc._split_validation(self.state())["final"],
                          "final evidence for another head must be dropped, never inherited")

    def test_every_reader_routes_through_the_accessor(self):
        """Mechanical: raw `state["validation"]` subscripts may appear only in the accessor itself
        and in the writers (cmd_validate's store, the clears, and state seeding) — a reader that
        bypasses `_split_validation` re-opens the shape drift this closes."""
        import inspect
        source = inspect.getsource(bc)
        readers = [line.strip() for line in source.splitlines()
                   if 'state["validation"]["' in line.replace(" ", "")]
        self.assertEqual(readers, [], f"raw validation readers bypass the accessor: {readers}")

    def test_each_run_record_corruption_is_refused_singly(self):
        cases = {
            "foreign tree": lambda r: r.update({"tree": "c" * 40}),
            "null tree": lambda r: r.update({"tree": None, "worktree_dirty": None}),
            "dirty tree": lambda r: r.update({"worktree_dirty": True}),
            "inventory count": lambda r: r["inventory"].update({"module_count": 1}),
            "log digest": lambda r: r["log"].update({"sha256": bc._digest(b"other")}),
            "foreign attestation": lambda r: r.update({"attests": "engine-ci"}),
            "missing scope": lambda r: r.pop("scope", None),
            "unrecognised scope": lambda r: r.update({"scope": "whatever"}),
            "malformed log": lambda r: r.update({"log": "not-a-dict"}),
            "log without a path": lambda r: r.update({"log": {"sha256": bc._digest(b"x")}}),
            "unreadable log with no digest to hide behind": lambda r: r.update(
                {"log": {"path": "/nonexistent/run.log", "sha256": "sha256:absent"}}),
            # A reviewer got a traceback out of the log read with an embedded null byte, which
            # `except OSError` does not catch, and noted the unbounded read of an
            # attacker-named path beside it.
            "log path with an embedded null byte": lambda r: r["log"].update({"path": "a\x00b"}),
            "log path that is a directory, not a file": lambda r: r["log"].update({"path": "/tmp"}),
        }
        for name, mutator in cases.items():
            with self.subTest(corruption=name):
                self.store.mutate(lambda s: s.update({"validation": None}))
                with self.assertRaisesRegex(bc.CoordinatorError, "failed"):
                    self._validate(fake=_candidate_validation_fake(record_mutator=mutator))
                candidate = bc._split_validation(self.state())["candidate"]
                refused = [r for r in candidate["results"] if not r["passed"]]
                self.assertTrue(refused, f"{name}: the corrupted record must fail its result")
                self.assertIn("run record refused", refused[0]["summary"])

    def test_a_missing_run_record_is_refused(self):
        def no_record(command, path):
            path.write_text("ok\n", encoding="utf-8"); return 0
        with self.assertRaisesRegex(bc.CoordinatorError, "failed"):
            self._validate(fake=no_record)
        candidate = bc._split_validation(self.state())["candidate"]
        self.assertIn("missing or unreadable",
                      [r for r in candidate["results"] if not r["passed"]][0]["summary"])

    def test_bare_validate_says_it_is_the_candidate_variant(self):
        _, out = self._validate()
        self.assertIn("validate candidate", out.splitlines()[0])
        self.assertIn("{", out, "the JSON consumers parse from the first brace still follows")

    def test_handoff_restore_downgrades_to_candidate_without_final(self):
        """A cold continuation must re-import against the live rollup: the handoff carries candidate
        evidence in the legacy shape, and a restored state holds no final."""
        self._validate()
        final = {"commit": HEAD_A, "source": "ci-import", "run_id": 5,
                 "context": "engine-ci", "tree": "b" * 40}
        self.store.mutate(lambda s: s["validation"].update({"final": final}))
        out = io.StringIO()
        handoff_path = Path(self.temp.name) / "handoff.json"
        with self.sealed(), mock.patch.object(bc, "_head", return_value=HEAD_A), \
                contextlib.redirect_stdout(out):
            bc.cmd_handoff_export(argparse.Namespace(output=str(handoff_path)), self.store)
        value = json.loads(handoff_path.read_text())
        self.assertEqual(set(value["validation"]), {"commit", "results"},
                         "the handoff carries candidate evidence in the legacy shape only")
        restored = bc._restore_result_set(value["validation"])
        state_like = {"validation": restored}
        self.assertIsNone(bc._split_validation(state_like)["final"])
        self.assertIsNone(bc._split_validation(state_like)["candidate"]["protocol_digest"],
                          "a restored candidate has null identity and can never be a cache hit")


class TestFinalImportAndRollupGate(CoordinatorCase):
    """`validate final import` — the only writer of final evidence — and the live rollup wall at
    submission. Every refusal is driven singly, with its distinct, actionable message."""

    TREE = "d" * 40

    def setUp(self):
        super().setUp()
        self.seed(); self.approve("quick")
        self.integrate_all()
        with mock.patch.object(bc, "_head", return_value=HEAD_A), \
                mock.patch.object(bc, "_derived_drift", return_value=[]), \
                mock.patch.object(bc, "_run_validation", side_effect=_candidate_validation_fake()), \
                contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_validate(argparse.Namespace(plan=str(self.plan_path)), self.store)

    def _pr(self, **over):
        pr = {"number": 7, "state": "OPEN", "isDraft": True, "headRefOid": HEAD_A,
              "baseRefOid": BASE, "mergeable": "MERGEABLE", "body": "body",
              "statusCheckRollup": [{"name": "engine-ci", "status": "COMPLETED",
                                     "conclusion": "SUCCESS", "completedAt": "2026-08-25T01:00:00Z"}]}
        pr.update(over)
        return pr

    def _import(self, pr=None, found=None, token="tok", merge_base=None):
        """Drive the import with the live seams stubbed; git plumbing answers with fixture values."""
        real_run = bc._run
        def fake_run(argv, **kw):
            if argv[:2] == ["git", "merge-base"]:
                out = (merge_base if merge_base is not None else BASE) + "\n"
                return subprocess.CompletedProcess(argv, 0, out, "")
            if argv == ["git", "rev-parse", "HEAD^{tree}"]:
                return subprocess.CompletedProcess(argv, 0, self.TREE + "\n", "")
            return real_run(argv, **kw)
        found = found if found is not None else (True, {"run_id": 42, "run_attempt": 1,
                                                        "receipt": {"schema": "engine-ci-receipt/v1"}})
        out = io.StringIO()
        with mock.patch.object(bc, "_head", return_value=HEAD_A), \
                mock.patch.object(bc, "_run", side_effect=fake_run), \
                mock.patch.object(bc.github, "pr_state", return_value=pr or self._pr()), \
                mock.patch("boot.gh_token", return_value=token), \
                mock.patch.object(bc.ci_gatekeeper, "find_reusable_receipt",
                                  return_value=found) as finder, \
                contextlib.redirect_stdout(out):
            bc.cmd_validate(argparse.Namespace(plan=str(self.plan_path), mode="final",
                                               action="import"), self.store)
        return finder, out.getvalue()

    def test_a_verified_import_records_final_and_only_then_is_the_head_final_ok(self):
        state = self.state()
        self.assertFalse(bc._final_ok(state, HEAD_A))
        finder, out = self._import()
        state = self.state()
        final = bc._split_validation(state)["final"]
        self.assertEqual((final["commit"], final["run_id"], final["tree"], final["source"]),
                         (HEAD_A, 42, self.TREE, "ci-import"))
        self.assertTrue(bc._final_ok(state, HEAD_A))
        kwargs = finder.call_args.kwargs
        self.assertEqual((kwargs["pr_number"], kwargs["head_sha"], kwargs["expected_tree"]),
                         (7, HEAD_A, self.TREE),
                         "provenance must go through the platform-filtered enumeration, bound to the "
                         "local head and its tree — never a run picked out of rollup fields")

    @staticmethod
    def _final_slot_writers(source):
        """Every function in `source` that could put a value under the `final` key, BY SYNTAX TREE.

        Two reviewers independently defeated the literal-pin version of this audit: each planted a
        laundering promotion into the coordinator — one reaching the slot through `.update({...})`,
        one through a differently-named dict literal — and watched all 278 tests pass. A grep for
        chosen spellings catches only the spellings someone thought of, which is the same blindness
        the bare minting-literal count had. This finds subscript stores, `final=` keywords, and any
        dict literal carrying a "final" key, and reports the enclosing function of each."""
        import ast
        tree = ast.parse(source)
        enclosing = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for child in ast.walk(node):
                    enclosing.setdefault(id(child), node.name)
        writers = set()
        for node in ast.walk(tree):
            hit = False
            if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
                hit = getattr(node.slice, "value", None) == "final"
            elif isinstance(node, ast.Dict):
                hit = any(isinstance(k, ast.Constant) and k.value == "final" for k in node.keys)
            elif isinstance(node, ast.keyword):
                hit = node.arg == "final"
            if hit:
                writers.add(enclosing.get(id(node)))
        return writers

    def test_the_import_verb_is_the_only_writer_of_final_evidence(self):
        """Only two functions may reach the final slot: the import verb's record callback, and
        cmd_validate's head-guarded preserve-or-drop."""
        import inspect
        source = inspect.getsource(bc)
        self.assertEqual(
            self._final_slot_writers(source),
            {"_final_import", "cmd_validate", "_split_validation"},
            "exactly three functions may touch the final key: the import verb (whose nested record "
            "callback performs the only mint), cmd_validate's head-guarded preserve-or-drop, and the "
            "read-side normalizer that CONSTRUCTS a view and never persists one — pinned here too, so "
            "the normalizer's role cannot quietly become a writer either. Any fourth is laundering")
        self.assertEqual(source.count('"source": "ci-import"'), 1,
                         "final evidence is minted in exactly one place")
        self.assertIn('split["final"].get("commit") == head', source,
                      "the preserved value is head-guarded, so preservation can never re-stamp")

    def test_the_write_side_audit_catches_a_laundering_promotion(self):
        """The audit's own positive control: the exact exploit two reviewers used to defeat the
        previous version — a promotion reaching the slot through `.update()`, with no subscript
        assignment, no minting literal, and a differently-named dict — must now be seen."""
        import inspect
        laundered = inspect.getsource(bc) + (
            "\n\ndef cmd_promote_evidence(args, store):\n"
            "    promoted = {\"commit\": \"x\", \"source\": \"local\"}\n"
            "    store.mutate(lambda s: s[\"validation\"].update({\"final\": promoted}))\n")
        self.assertIn("cmd_promote_evidence", self._final_slot_writers(laundered),
                      "the audit must see a laundering promotion no literal pin would catch")

    def test_head_drift_refuses_with_the_push_or_pull_remedy(self):
        with self.assertRaisesRegex(bc.CoordinatorError, "push this head"):
            self._import(pr=self._pr(headRefOid=HEAD_B))

    def test_a_base_advance_is_a_reconcile_message_never_a_forgery_message(self):
        with self.assertRaisesRegex(bc.CoordinatorError,
                                    "routine base advance, not an integrity failure"):
            self._import(merge_base=HEAD_B)

    def test_each_rollup_state_has_its_own_refusal(self):
        cases = {
            "absent": ({"statusCheckRollup": []}, "may not have started"),
            "pending": ({"statusCheckRollup": [{"name": "engine-ci", "status": "IN_PROGRESS",
                                                "startedAt": "2026-08-25T01:00:00Z"}]},
                        "still running"),
            "red": ({"statusCheckRollup": [{"name": "engine-ci", "status": "COMPLETED",
                                            "conclusion": "FAILURE",
                                            "completedAt": "2026-08-25T01:00:00Z"}]},
                    "red for this head"),
        }
        for name, (over, message) in cases.items():
            with self.subTest(rollup=name):
                with self.assertRaisesRegex(bc.CoordinatorError, message):
                    self._import(pr=self._pr(**over))

    def test_an_overlapping_in_flight_run_reads_as_pending_not_success(self):
        """A metadata event firing during a long full run: the in-flight entry must win as pending
        even though the completed entry's finish time out-sorts its start time."""
        rollup = [{"name": "engine-ci", "status": "COMPLETED", "conclusion": "SUCCESS",
                   "startedAt": "2026-08-25T01:00:00Z", "completedAt": "2026-08-25T01:40:00Z"},
                  {"name": "engine-ci", "status": "IN_PROGRESS",
                   "startedAt": "2026-08-25T01:05:00Z"}]
        state, _ = bc.github.required_check({"statusCheckRollup": rollup}, "engine-ci")
        self.assertEqual(state, "pending")

    def test_a_same_named_commit_status_cannot_outvote_the_platforms_check_run(self):
        """Any repository writer can post a commit status through the status API; the registered
        workflow's check is a CheckRun, and the CheckRun-shaped entry decides."""
        rollup = [{"name": "engine-ci", "status": "COMPLETED", "conclusion": "FAILURE",
                   "completedAt": "2026-08-25T01:00:00Z"},
                  {"context": "engine-ci", "state": "SUCCESS",
                   "createdAt": "2026-08-25T02:00:00Z"}]
        state, entry = bc.github.required_check({"statusCheckRollup": rollup}, "engine-ci")
        self.assertEqual(state, "failure")
        self.assertNotIn("state", entry)

    def test_a_superseded_red_beside_a_newer_green_reads_as_green(self):
        """The observed shape on this repository: the pre-contract run's failure lingers in the
        rollup beside the later success; the LATEST entry decides."""
        rollup = [{"name": "engine-ci", "status": "COMPLETED", "conclusion": "FAILURE",
                   "completedAt": "2026-08-25T01:00:00Z"},
                  {"name": "engine-ci", "status": "COMPLETED", "conclusion": "SUCCESS",
                   "completedAt": "2026-08-25T01:02:00Z"}]
        self._import(pr=self._pr(statusCheckRollup=rollup))
        self.assertTrue(bc._final_ok(self.state(), HEAD_A))

    def test_no_token_refuses_with_the_single_homed_note(self):
        import boot
        with self.assertRaisesRegex(bc.CoordinatorError, "no GitHub token is available"):
            self._import(token=None)

    def test_no_verifiable_receipt_refuses_with_the_enumeration_detail(self):
        with self.assertRaisesRegex(bc.CoordinatorError, "no run of the registered workflow"):
            self._import(found=(False, {"reason": "no-receipt-for-this-tree", "refusals": []}))

    def test_import_requires_green_candidate_at_the_exact_head(self):
        self.store.mutate(lambda s: s.update({"validation": None}))
        with self.assertRaisesRegex(bc.CoordinatorError, "stands on top of candidate evidence"):
            self._import()

    def test_import_refuses_a_pre_split_candidate(self):
        legacy = {"commit": HEAD_A, "results": [{"id": "engine-ci", "commit": HEAD_A,
                                                 "passed": True, "summary": "ok"}]}
        self.store.mutate(lambda s: s.update({"validation": legacy}))
        with self.assertRaisesRegex(bc.CoordinatorError, "re-run `validate` at this head"):
            self._import()

    def test_import_refuses_when_the_registry_and_gatekeeper_disagree(self):
        protocol = bc._protocol()
        protocol["validation_commands"]["final"]["context"] = "someone-elses-check"
        with mock.patch.object(bc, "_protocol", return_value=protocol), \
                self.assertRaisesRegex(bc.CoordinatorError, "no longer matches what the CI gatekeeper"):
            self._import()

    def test_registry_final_descriptor_matches_the_gatekeeper_constants(self):
        final = bc._protocol()["validation_commands"]["final"]
        self.assertEqual(
            (final["context"], final["workflow"], final["receipt_artifact"], final["max_age_days"]),
            (bc.ci_gatekeeper.CHECK_CONTEXT, bc.ci_gatekeeper.WORKFLOW_PATH,
             bc.ci_gatekeeper.RECEIPT_ARTIFACT_NAME, bc.ci_gatekeeper.MAX_RECEIPT_AGE_DAYS))

    def test_the_forward_surfaces_name_the_import_verb_before_the_wall(self):
        """A session reads status BEFORE it acts: the required-evidence line and, at the
        final-validation phase, the next/available lines all name `validate final import`."""
        with mock.patch.object(bc, "_head", return_value=HEAD_A):
            status = bc._status(self.state())
        self.assertTrue(any("validate final import" in line
                            for line in status["required_evidence"]),
                        status["required_evidence"])

    def _seed_everything_but_final(self):
        """Every rung below final-validation satisfied: quick depth needs no deliverable receipts,
        so seeding green preflights and a complete contract at HEAD_A walks the ladder to the rung
        this class exists to test."""
        def seed(s):
            s["reviews"]["deliverable"].update({
                "packet_digest": bc._digest(b"packet"), "referent_digest": bc._digest(b"referent"),
                "required_lenses": [], "reviewed_commit": HEAD_A, "base_commit": BASE})
            s.update({
                "preflights": [{"id": "pr-contract", "commit": HEAD_A, "passed": True, "summary": "ok"},
                               {"id": "checkout-integrity", "commit": HEAD_A, "passed": True,
                                "summary": "ok"}],
                "pr_contract": {"commit": HEAD_A, "body_digest": bc._digest(b"body"),
                                "complete": True}})
        self.store.mutate(seed)

    def test_the_final_validation_phase_names_the_verb_in_next_and_available(self):
        """The rung is actually ENTERED — a reviewer proved the first version of this test never
        reached its named branch and asserted only the sibling's line. Unconditional now."""
        self._seed_everything_but_final()
        with mock.patch.object(bc, "_head", return_value=HEAD_A):
            status = bc._status(self.state())
        self.assertEqual(status["phase"], "final-validation")
        self.assertIn("validate final import", status["suggested_next"])
        self.assertTrue(any("validate final import" in x
                            for x in status["available_activities"]))
        self._import()
        with mock.patch.object(bc, "_head", return_value=HEAD_A):
            status = bc._status(self.state())
        self.assertEqual(status["phase"], "ready",
                         "with the proof imported the same state reaches ready")

    def test_submit_preview_re_reads_the_live_rollup_with_three_distinct_messages(self):
        self.store.mutate(lambda s: s.update(
            {"pr_contract": {"commit": HEAD_A, "body_digest": bc._digest(b"body"), "complete": True}}))
        ready = {"phase": "ready", "head_commit": HEAD_A,
                 "required_evidence": [], "engineering_judgment": []}
        ancestor = subprocess.CompletedProcess([], 0, "", "")
        cases = {
            "absent": ([], "push the head and wait"),
            "pending": ([{"name": "engine-ci", "status": "IN_PROGRESS",
                          "startedAt": "2026-08-25T01:00:00Z"}], "still running"),
            "red": ([{"name": "engine-ci", "status": "COMPLETED", "conclusion": "FAILURE",
                      "completedAt": "2026-08-25T01:00:00Z"}], "red for this head"),
        }
        for name, (rollup, message) in cases.items():
            with self.subTest(rollup=name):
                pr = self._pr(statusCheckRollup=rollup)
                with mock.patch.object(bc, "_status", return_value=ready), \
                        mock.patch.object(bc.github, "pr_state", return_value=pr), \
                        mock.patch.object(bc, "_run", return_value=ancestor), \
                        self.assertRaisesRegex(bc.CoordinatorError, message):
                    bc._submit_preview(self.store, str(self.plan_path))
        pr = self._pr()
        with mock.patch.object(bc, "_status", return_value=ready), \
                mock.patch.object(bc, "_head", return_value=HEAD_A), \
                mock.patch.object(bc.github, "pr_state", return_value=pr), \
                mock.patch.object(bc, "_run", return_value=ancestor):
            preview = bc._submit_preview(self.store, str(self.plan_path))
        self.assertEqual(preview["action"], "mark-ready")


class TestPreflightHandoffAndSubmission(CoordinatorCase):
    def test_handoff_redacts_private_rationale(self):
        self.seed(issue=11)
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
        self.seed(issue=11)
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
                mock.patch.object(bc, "_sealed_plan", return_value=(PLAN_ID, SEALED, plan())):
            bc.cmd_handoff_restore(argparse.Namespace(input=str(path), repository="owner/repo", pr=7), restored)
        self.assertEqual(restored.read()["findings"][0]["severity"], "blocking")
        self.assertEqual(restored.read()["validation"]["results"][0]["log_digest"], digest)

    def _blocking_finding_with_private(self, private_reference):
        digest = "sha256:" + "b" * 64
        return {"id": "F-9", "stage": "deliverable", "lens": "security-governance",
                "packet_digest": digest, "lens_packet_digest": digest, "commit": HEAD_A,
                "severity": "blocking", "summary": "private", "disposition": "rejected",
                "rationale": "private", "escalation_kind": None, "blocks_this_pr": False,
                "handoff_summary": "Safe concern summary.", "operator_summary": "Safe disagreement.",
                "private_reference": private_reference}

    def test_handoff_restore_rejects_malformed_summary_cleanly(self):
        # The strip-on-restore loop must not assume each finding_summaries entry is a dict: a malformed
        # block (a non-dict summary) has to reach _validate and fail with the tool's clean CoordinatorError,
        # not a raw AttributeError from an unconditional .pop() (StarshipSuperjam/engine-template#981).
        self.seed(issue=11)
        self.store.mutate(lambda s: s["findings"].append(self._blocking_finding_with_private(None)))
        handoff = bc._handoff(self.state())
        handoff["finding_summaries"] = ["not-a-dict"]
        path = Path(self.temp.name) / "malformed-handoff-981.json"
        path.write_text(json.dumps(handoff))
        restored = bc.StateStore(str(Path(self.temp.name) / "restored-malformed-981.json"))
        with self.assertRaises(bc.CoordinatorError):
            bc.cmd_handoff_restore(argparse.Namespace(input=str(path), repository="owner/repo", pr=7), restored)

    def test_handoff_never_publishes_private_reference(self):
        # StarshipSuperjam/engine-template#981: `handoff export --publish` writes finding summaries into
        # the public PR body, so a populated private_reference must not appear in the rendered handoff,
        # and the round-trip must yield None for it (schema-valid restore = the field is dropped).
        self.seed(issue=11)
        self.store.mutate(lambda s: s["findings"].append(
            self._blocking_finding_with_private("LEAKME-private-reference-XYZ")))
        handoff = bc._handoff(self.state())
        self.assertNotIn("private_reference", handoff["finding_summaries"][0])
        self.assertNotIn("LEAKME-private-reference-XYZ", json.dumps(handoff))
        path = Path(self.temp.name) / "handoff-981.json"
        path.write_text(json.dumps(handoff))
        restored = bc.StateStore(str(Path(self.temp.name) / "restored-981.json"))
        pr = {"number": 7, "state": "OPEN", "headRefOid": HEAD_A}
        with mock.patch.object(bc.repo_identity, "origin_slug", return_value="owner/repo"), \
                mock.patch.object(bc.github, "pr_state", return_value=pr), \
                mock.patch.object(bc, "_head", return_value=HEAD_A), \
                mock.patch.object(bc, "_sealed_plan", return_value=(PLAN_ID, SEALED, plan())):
            bc.cmd_handoff_restore(argparse.Namespace(input=str(path), repository="owner/repo", pr=7), restored)
        # A successful restore means the state passed build-state validation; the field is dropped to None.
        self.assertIsNone(restored.read()["findings"][0]["private_reference"])

    def test_handoff_restore_tolerates_legacy_private_reference(self):
        # A handoff exported by an OLDER engine still carries private_reference; the tightened schema
        # forbids it, but restore must strip the stray copy and continue rather than fail closed
        # (StarshipSuperjam/engine-template#981) — the field never survives the round-trip anyway.
        self.seed(issue=11)
        self.store.mutate(lambda s: s["findings"].append(self._blocking_finding_with_private(None)))
        handoff = bc._handoff(self.state())
        handoff["finding_summaries"][0]["private_reference"] = "legacy private text that must not survive"
        path = Path(self.temp.name) / "legacy-handoff-981.json"
        path.write_text(json.dumps(handoff))
        restored = bc.StateStore(str(Path(self.temp.name) / "restored-legacy-981.json"))
        pr = {"number": 7, "state": "OPEN", "headRefOid": HEAD_A}
        with mock.patch.object(bc.repo_identity, "origin_slug", return_value="owner/repo"), \
                mock.patch.object(bc.github, "pr_state", return_value=pr), \
                mock.patch.object(bc, "_head", return_value=HEAD_A), \
                mock.patch.object(bc, "_sealed_plan", return_value=(PLAN_ID, SEALED, plan())):
            bc.cmd_handoff_restore(argparse.Namespace(input=str(path), repository="owner/repo", pr=7), restored)
        self.assertIsNone(restored.read()["findings"][0]["private_reference"])

    def test_handoff_schema_forbids_private_reference(self):
        # Defense in depth: the schema itself must REJECT a finding summary carrying private_reference,
        # so a future re-introduction fails validation instead of silently leaking.
        self.seed(issue=11)
        self.store.mutate(lambda s: s["findings"].append(self._blocking_finding_with_private(None)))
        handoff = bc._handoff(self.state())
        handoff["finding_summaries"][0]["private_reference"] = "should be forbidden by the schema"
        with self.assertRaises(bc.CoordinatorError):
            bc._validate(handoff, bc.HANDOFF_SCHEMA_V2)

    def test_v2_handoff_never_publishes_private_reference(self):
        # The v2 (execution-DAG) handoff path is symmetric to v1 but needs its own witness: the v2
        # schema edit and the is_v2 branch must both drop and forbid private_reference (#981).
        state = bc._initial_state("owner/repo", 7, BASE, PLAN_ID, SEALED, plan_v2(), 11)
        self.store.create(state)
        self.store.mutate(lambda s: s["findings"].append(
            self._blocking_finding_with_private("V2-LEAKME-private-XYZ")))
        handoff = bc._handoff(self.state())
        self.assertEqual(handoff["schema_version"], "build-handoff.v2")
        self.assertNotIn("private_reference", handoff["finding_summaries"][0])
        self.assertNotIn("V2-LEAKME-private-XYZ", json.dumps(handoff))
        handoff["finding_summaries"][0]["private_reference"] = "should be forbidden by the v2 schema"
        with self.assertRaises(bc.CoordinatorError):
            bc._validate(handoff, bc.HANDOFF_SCHEMA_V2)

    def test_handoff_requires_summary_for_every_finding(self):
        self.seed(issue=11)
        self.store.mutate(lambda s: s["findings"].append({"id": "F-1", "stage": "plan", "lens": "x", "packet_digest": s["plan"]["digest"], "commit": None, "severity": "nit", "summary": "x", "disposition": "rejected", "rationale": "x", "escalation_kind": None, "blocks_this_pr": False, "handoff_summary": None}))
        with self.assertRaisesRegex(bc.CoordinatorError, "handoff-summary"):
            bc._handoff(self.state())

    def test_handoff_export_writes_a_file_and_touches_no_pr(self):
        self.seed(issue=11)
        out = Path(self.temp.name) / "exported.json"
        with mock.patch.object(bc, "_sealed_plan", return_value=(PLAN_ID, SEALED, plan())), \
                mock.patch.object(bc, "_assert_spec_boundary", return_value={}), \
                mock.patch.object(bc, "_must_run") as write, \
                contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_handoff_export(argparse.Namespace(output=str(out)), self.store)
        write.assert_not_called()
        self.assertEqual(json.loads(out.read_text())["plan"]["plan_id"], PLAN_ID)

    def test_routine_progress_restores_from_handoff(self):
        self.seed(issue=11)
        self.store.mutate(lambda s: s["progress"].update({"current_item": "W1", "completed": [{"id": "W1", "commit": HEAD_A}]}))
        handoff = bc._handoff(self.state())
        self.assertEqual(handoff["progress"]["completed"], [{"id": "W1", "commit": HEAD_A}])

    def test_restore_rebinds_identity_and_rejects_uncontained_progress(self):
        self.seed(issue=11)
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

    def test_checkout_integrity_preflight_passes_when_checkout_unchanged(self):
        self.seed()
        # a deliverable review packet would capture this baseline of the real build checkout
        self.store.mutate(lambda s: s.update({"checkout_snapshot": bc.review_integrity.snapshot(str(bc.ROOT))}))
        pr = {"body": "complete", "baseRefOid": BASE}
        ok = subprocess.CompletedProcess([], 0, "ok", "")
        with mock.patch.object(bc, "_head", return_value=HEAD_A), mock.patch.object(bc, "_verify_draft", return_value=pr), \
                mock.patch.object(bc, "_run", return_value=ok), \
                mock.patch.object(bc, "_pr_contract", return_value=(True, "complete")), contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_preflight(argparse.Namespace(pr_body=None, json=False), self.store)
        result = {row["id"]: row for row in self.state()["preflights"]}
        self.assertIn("checkout-integrity", result)
        self.assertTrue(result["checkout-integrity"]["passed"])

    def test_checkout_integrity_preflight_fails_and_blocks_on_origin_repoint(self):
        self.seed()
        # simulate a review that repointed the checkout's origin: the captured baseline names the real
        # origin, the live re-read at preflight would too, so inject a mismatch into the baseline to model it
        tampered = {**bc.review_integrity.snapshot(str(bc.ROOT)), "origin": "https://github.com/attacker/fake.git"}
        self.store.mutate(lambda s: s.update({"checkout_snapshot": tampered}))
        pr = {"body": "complete", "baseRefOid": BASE}
        ok = subprocess.CompletedProcess([], 0, "ok", "")
        with mock.patch.object(bc, "_head", return_value=HEAD_A), mock.patch.object(bc, "_verify_draft", return_value=pr), \
                mock.patch.object(bc, "_run", return_value=ok), \
                mock.patch.object(bc, "_pr_contract", return_value=(True, "complete")), contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaisesRegex(bc.CoordinatorError, "checkout-integrity"):
            bc.cmd_preflight(argparse.Namespace(pr_body=None, json=False), self.store)
        # the failing result is recorded before the raise, so the readiness gate blocks on it
        result = {row["id"]: row for row in self.state()["preflights"]}
        self.assertFalse(result["checkout-integrity"]["passed"])
        self.assertIn("origin", result["checkout-integrity"]["summary"])

    def test_checkout_worktrees_leg_is_advisory_not_blocking(self):
        self.seed()
        # baseline claims zero worktrees; the real checkout has at least this one -> worktree drift,
        # but origin/branch/stash are unchanged, so the required leg passes and preflight does NOT raise.
        baseline = {**bc.review_integrity.snapshot(str(bc.ROOT)), "worktrees": []}
        self.store.mutate(lambda s: s.update({"checkout_snapshot": baseline}))
        pr = {"body": "complete", "baseRefOid": BASE}
        ok = subprocess.CompletedProcess([], 0, "ok", "")
        with mock.patch.object(bc, "_head", return_value=HEAD_A), mock.patch.object(bc, "_verify_draft", return_value=pr), \
                mock.patch.object(bc, "_run", return_value=ok), \
                mock.patch.object(bc, "_pr_contract", return_value=(True, "complete")), contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_preflight(argparse.Namespace(pr_body=None, json=False), self.store)  # must NOT raise
        result = {row["id"]: row for row in self.state()["preflights"]}
        self.assertIn("checkout-worktrees", result)
        self.assertFalse(result["checkout-worktrees"]["passed"], "the stray worktree is surfaced")
        self.assertTrue(result["checkout-integrity"]["passed"], "the required leg is unaffected by worktrees")

    def test_readiness_requires_the_checkout_integrity_preflight(self):
        self.seed()
        status = bc._status(self.state())
        self.assertIn("green preflight: checkout-integrity", status["required_evidence"],
                      "checkout-integrity is a required preflight the readiness gate blocks on")
        self.assertNotIn("green preflight: checkout-worktrees", status["required_evidence"],
                         "checkout-worktrees is advisory and never blocks readiness")

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

    def test_submit_preview_refuses_a_pr_that_is_not_open(self):
        # A finalize transition on a merged/closed/missing claim must refuse, never mark ready
        # (StarshipSuperjam/engine-template#959 names "missing PRs" among the failure cases).
        self.seed()
        self.store.mutate(lambda s: s.update({"pr_contract": {"commit": HEAD_A, "body_digest": bc._digest(b"complete"), "complete": True}}))
        ready = {"phase": "ready", "head_commit": HEAD_A, "required_evidence": [], "engineering_judgment": []}
        not_open = {"number": 7, "state": "CLOSED", "isDraft": False, "headRefOid": HEAD_A,
                    "baseRefOid": BASE, "mergeable": "MERGEABLE", "body": "complete"}
        with mock.patch.object(bc, "_status", return_value=ready), mock.patch.object(bc.github, "pr_state", return_value=not_open), self.assertRaisesRegex(bc.CoordinatorError, "not open"):
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
        # The message now names the field and the constraint rather than reporting the whole object
        # as "not valid under any of the given schemas": a `oneOf` error is descended into, so an
        # author reading this refusal is told which value to fix.
        with self.assertRaisesRegex(bc.CoordinatorError, r"criteria\.0\.reason: '' should be non-empty"):
            bc._validate(value, bc.PLAN_SCHEMA_V2)

    def test_changed_criterion_invalidates_approval(self):
        root = Path(self.temp.name) / "repo"
        value = self.settled(root)
        with mock.patch.object(bc, "ROOT", root), mock.patch.object(bc, "_head", return_value=HEAD_A):
            canonical = bc._canonical_spec(value)
            state = bc._initial_state("owner/repo", 7, BASE, PLAN_ID, SEALED, value, None)
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
        args = argparse.Namespace(stage="deliverable", plan=str(self.plan_path), impact=None, session_effort="high")
        out = io.StringIO()
        self.integrate_all()
        self.store.mutate(lambda s: s.update({"validation": {"commit": HEAD_A, "results": [
            {"id": "ci", "commit": HEAD_A, "passed": True, "summary": "ok"}]}}))
        with mock.patch.object(bc, "_assert_spec_current", return_value=canonical), mock.patch.object(bc, "_installed", return_value=[]), mock.patch.object(bc, "_head", return_value=HEAD_A), mock.patch.object(bc, "_base", return_value=BASE), contextlib.redirect_stdout(out):
            bc._packet(args, self.store)
        self.assertEqual(json.loads(out.getvalue())["spec"], canonical)


def _installed_module_ids() -> set:
    """The module ids present in this tree. Mirrors the helper of the same name in test_seed.py."""
    import module_coherence
    return {m.get("id") for _p, m in module_coherence.discover_manifests() if isinstance(m, dict)}


def _needs_modules(case, *ids) -> None:
    """Skip when a named module is not installed here. These cases read files the module DELIVERS, so in a
    deployment that declined it there is no subject to assert over — the absence is the module's contract."""
    missing = sorted(set(ids) - _installed_module_ids())
    if missing:
        case.skipTest(f"{', '.join(missing)} is not installed in this repository, so the file this case reads "
                      f"is legitimately absent here")


class TestHistoricalScenarioCorpus(unittest.TestCase):
    def test_consumed_review_lenses_remain_connected(self):
        text = (bc.ROOT / ".engine" / "operations" / "build-orchestration.md").read_text()
        for lens in ("product-intent", "architecture", "feasibility", "risk-governance",
                     "spec-conformance", "divergence-hunter", "usability", "technical-integrity", "security-governance"):
            self.assertIn(lens, text)

    def test_every_mapped_obligation_has_one_live_disposition(self):
        obligations = json.loads((bc.ROOT / ".engine/build-orchestration-obligations.json").read_text())
        self.assertEqual(len(obligations["obligations"]), 73)
        self.assertEqual(len({row["id"] for row in obligations["obligations"]}), 73)

    def test_special_delivery_and_submission_disclosures_remain_reachable(self):
        # The two core-owned runbooks are present in EVERY projection, so they are asserted unconditionally;
        # only the external-contribution line is conditional on that optional module being installed.
        owned = (bc.ROOT / ".engine/operations/owned-product-build.md").read_text()
        evidence = (bc.ROOT / ".engine/operations/build-submission-evidence.md").read_text()
        for phrase in ("mechanic_build.py worktree", "tools/local_references.py scan", "unpushed commits", "worker fails"):
            self.assertIn(phrase, owned)
        for phrase in ("recognized automation", "fail-open", "mcp_availability_check", "unresolved-conversation", "operator-runnable demonstration"):
            self.assertIn(phrase, evidence)
        if "external-contribution" in _installed_module_ids():
            external = (bc.ROOT / ".engine/operations/external-contribution-submit.md").read_text()
            self.assertIn("no draft PR is", external)

    def test_runbook_stays_within_its_line_cap(self):
        # A ratchet against bloat, kept with no slack: it sat at exactly 250, moved to 251 when the v2
        # completion path had to be taught (a Routine session following the old text would reach for a verb
        # the coordinator now refuses), and to 261 for the correction and consent paths — a session that
        # does not know `present-findings` exists meets a seal refusal it cannot resolve, and one that does
        # not know `plan adopt` exists abandons a Build to correct a plan. 261 -> 265 for the review
        # economy: `review packet` now REFUSES without `--session-effort`, so a session working from the
        # old text cannot cut a panel at all. Raise it only for instruction a session cannot work without,
        # and only by what that instruction actually costs.
        # In parallel on main, 253 -> 255 for the delegation routing: step 4 says where reconnaissance
        # goes and step 5 how the suite is run, and a session following the runbook without them does that
        # work inline — the cost the routing exists to remove.
        # 273 is those two independently justified raises meeting in a merge, not a new budget: the file
        # measures exactly 273 lines, and neither side's additions were trimmed to fit the other's cap.
        text = (bc.ROOT / ".engine/operations/build-orchestration.md").read_text()
        self.assertLessEqual(len(text.splitlines()), 273)

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
        # Same ratchet as the line cap. On this branch: 3063 -> 3081 for the v2 completion path, then
        # 3081 -> 3294, the measured cost of four things a session cannot work without — the snapshot is
        # durable and no longer in OS temp, so the old instruction sends a session to the wrong place;
        # approve, seal and bind refuse without the operator's recorded decision; the seal refuses until
        # the panel's outcome was presented; and correcting a plan mid-Build no longer means abandoning
        # it. Then 3294 -> 3504 for the review economy, three of them hard refusals the old text cannot
        # get a session past: `review packet` without `--session-effort`, `review record` with a
        # `--code-execution` value the old text does not list, and `repair assess --judgment none` when it
        # would discard recorded receipts.
        # In parallel on main: -> 3147 for the candidate/final validation split, -> 3152 restoring the
        # focused-tests anchor BO-21 binds, -> 3215 for the delegation routing, -> 3233 when a reviewer
        # showed that routing had been attached to the WRONG verb and the correction had to say which
        # runs route to the runner and which do not.
        # 3657 is those two ratchets meeting in a merge, not a new budget: the file measures exactly that,
        # and neither side's instruction was trimmed to fit the other's cap. The preservation-source ratio
        # (448/6296) is unchanged.
        # 3657 -> 3850 for spend-counted repair rounds, measured at +193 words. That cost buys four
        # things a session cannot infer from the verbs: what makes a round COUNT (a panel, not a
        # judgment), the two bounds and that both are guidance stops rather than walls, the blocker-union
        # default and the minimal-cold-check convention, and that each round is measured from the
        # previous round's end. Honesty first: thinning this passage to hold the old number would leave
        # the weakening -- two flat rounds to three counted -- disclosed nowhere a session reads.
        # 3850 -> 3877: the last 27 of those words are the honest worst case the two bounds allow, added
        # because a reviewer showed the passage otherwise left an impression of cheapness it could not
        # back. The preservation-source ratio (448/6296) is unchanged.
        self.assertLessEqual(len(text.split()), 3877)
        for phrase in ("operator-approved plan", "one cold plan review", "reviewed-to-final divergence",
                       "no automatic audit recursion", "operator alone merges",
                       # The routing targets are load-bearing prose, not decoration: a runbook that
                       # stops naming them teaches the inline behaviour again by omission.
                       "engine-grounding-scout", "engine-validation-runner",
                       # And the CORRECTION is load-bearing too. Pinning only the persona name would
                       # leave a future edit free to reattach the runner to the coordinator's own
                       # validation and stay green — the exact defect a deliverable reviewer caught,
                       # where a scout confined to a copy was told to produce evidence that binds to
                       # the live tree. This sentence is the half that says which runs are not its job.
                       "It is the wrong tool for the two classes below"):
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
        # Each reviewer module delivers a fixed set, so pin the count PER MODULE against what is installed —
        # a deployment that declined one still proves the other's set is complete, which a single all-or-
        # nothing count would drop entirely.
        ids = _installed_module_ids()
        expected = (4 if "design-review" in ids else 0) + (5 if "qa-review" in ids else 0)
        self.assertEqual(len(agents), expected)
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
        _needs_modules(self, "qa-review")
        for name in ("engine-qa-review-spec-conformance.md", "engine-qa-review-divergence-hunter.md"):
            text = (bc.ROOT / ".claude" / "agents" / name).read_text()
            self.assertIn("no-spec is not a no-op" if "divergence" in name else "It is not a no-op", text)
            self.assertIn("operator-approved Build plan", text)


class TestPlanV2Ingest(CoordinatorCase):
    def _bind(self, value, *, issue=None):
        self.write_plan(value)
        pr = {"number": 7, "state": "OPEN", "isDraft": True, "headRefOid": HEAD_A, "baseRefOid": BASE,
              "body": ""}
        with self.sealed(value=value), \
                mock.patch.object(bc, "_verify_draft", return_value=pr), \
                mock.patch.object(bc, "_head", return_value=HEAD_A), \
                mock.patch.object(bc.github, "tag_coordinator_owned", return_value=True), \
                mock.patch.object(bc, "_record_build_binding"), \
                contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_plan_bind(self.bind_args(issue=issue), self.store)

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

    def test_a_v1_payload_never_binds_however_it_arrives(self):
        # The home-repo carve-out and the in-flight Issue exemption are both gone: v1 is unreachable at
        # entry, full stop. What replaces the carve-out is a refusal that names the way forward.
        self.write_plan(plan_v1())
        with self.sealed(value=plan_v1()), self.assertRaisesRegex(bc.CoordinatorError, "v1 no longer enters a Build"):
            bc.cmd_plan_bind(self.bind_args(), self.store)


class TestV2CompletionGate(CoordinatorCase):
    """A v2 completion can only be earned at `work integrate` (BC-27).

    The second writer this closes — `checkpoint --complete-item` — appended the same completion entry
    with no integration evidence, so the graph's completion rule had a published bypass. These cases
    pin the refusal, the untouched v1 island, the four readers of `progress.completed`, and the
    mid-flight snapshot that already carries an unearned entry.
    """

    def setUp(self):
        super().setUp()
        self.v2 = plan_v2()
        self.write_plan(self.v2)
        state = bc._initial_state("owner/repo", 7, BASE, PLAN_ID, SEALED, self.v2, 11)
        state["approval"] = {"plan_digest": state["plan"]["digest"], "spec_digest": None, "depth": "quick"}
        self.store = bc.StateStore(str(Path(self.temp.name) / "v2-gate.json"))
        self.store.create(state)

    def _note(self, work_item="shared"):
        note = {"objective": "x", "current_work": "x", "work_item": work_item, "assumptions": [],
                "non_goals": [], "planned_scope": [".engine/tools/shared.py"],
                "remaining_verification": ["tests"], "judgment": "aligned"}
        path = Path(self.temp.name) / f"note-{work_item}.json"
        path.write_text(json.dumps(note), encoding="utf-8")
        return str(path)

    def _checkpoint(self, work_item="shared"):
        args = argparse.Namespace(plan=str(self.plan_path), input=self._note(work_item), json=False)
        with mock.patch.object(bc, "_head", return_value=HEAD_A), \
                mock.patch.object(bc, "_changed_paths", return_value=[]), \
                mock.patch.object(bc, "_assert_spec_boundary", return_value={}), \
                contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_checkpoint(args, self.store)

    def _inject_unearned(self, node_id="shared"):
        """Write the completion the removed writer used to write: an id and a commit, no integration."""
        self.store.mutate(lambda s: s["progress"]["completed"].append({"id": node_id, "commit": HEAD_A}))

    def test_checkpoint_can_no_longer_be_asked_to_complete_anything(self):
        # The refusal became a removal with the v1 sunset. `--complete-item` wrote a completion with
        # no integration evidence and survived only for v1, which had no other completion path; with
        # v1 gone its only remaining use would be to bypass the graph's own rule, so the flag is not
        # there to pass. The CLI is the assertion: argparse rejects it outright.
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            bc.parser().parse_args(["checkpoint", "--plan", "p.json", "--input", "n.json",
                                    "--complete-item", "shared"])
        self.assertEqual(self.state()["progress"]["completed"], [])

    def test_checkpoint_without_the_flag_still_records_the_note(self):
        self._checkpoint()
        state = self.state()
        self.assertEqual(state["checkpoint"]["work_item"], "shared")
        self.assertEqual(state["progress"]["completed"], [])

    def test_a_bound_build_is_always_a_v2_snapshot(self):
        # The v1 island is gone rather than maintained: a bound Build is always a v2 snapshot, which
        # is what left `checkpoint --complete-item` with nothing to complete anything on.
        state = bc._initial_state("owner/repo", 7, BASE, PLAN_ID, SEALED, plan_v1(), None)
        self.assertEqual(state["schema_version"], "build-state.v2")
        self.assertEqual(state["work"], {})

    def test_an_injected_completion_derives_no_completion_and_holds_validation(self):
        self._inject_unearned()
        lifecycle = bc.dag.derive_lifecycle(self.v2, self.state())
        self.assertNotEqual(lifecycle["shared"]["state"], bc.dag.COMPLETE)
        with self.assertRaises(bc.CoordinatorError) as caught:
            bc.cmd_validate(argparse.Namespace(plan=str(self.plan_path)), self.store)
        message = str(caught.exception)
        self.assertIn("no integration earned", message)
        self.assertIn("work integrate", message)
        self.assertIn("Do NOT rebind", message)

    def test_an_injected_completion_also_holds_the_checkpoint_gate(self):
        self._inject_unearned()
        with self.assertRaisesRegex(bc.CoordinatorError, "no integration earned"):
            self._checkpoint()

    def test_the_four_readers_of_completed_progress_are_unchanged(self):
        # progress.completed keeps its field, its schema requirement, and all four readers; only the
        # second WRITER is gone. Each reader is exercised against an injected entry.
        self._inject_unearned()
        state = self.state()
        # 1. the handoff ancestry check still refuses a completing commit the live PR head does not contain.
        handoff = bc._handoff(state)
        path = Path(self.temp.name) / "v2-handoff.json"
        path.write_text(json.dumps(handoff), encoding="utf-8")
        restored = bc.StateStore(str(Path(self.temp.name) / "v2-restored.json"))
        failed = types.SimpleNamespace(returncode=1, stdout="", stderr="")
        with mock.patch.object(bc.repo_identity, "origin_slug", return_value="owner/repo"), \
                mock.patch.object(bc.github, "pr_state", return_value={"number": 7, "state": "OPEN", "headRefOid": HEAD_A}), \
                mock.patch.object(bc, "_head", return_value=HEAD_A), \
                mock.patch.object(bc, "_run", return_value=failed), \
                self.assertRaisesRegex(bc.CoordinatorError, "not contained by the live PR head"):
            bc.cmd_handoff_restore(argparse.Namespace(input=str(path), repository="owner/repo", pr=7), restored)
        # 2. the retrospective plan-review waiver still treats progress as prospective work — driven
        # through the real reader in its own case below, since the waiver refuses earlier on this
        # fixture's recorded plan-review evidence.
        # 3. the status render still reports it.
        with mock.patch.object(bc, "_head", return_value=HEAD_A), mock.patch.object(bc, "_installed", return_value=[]):
            status = bc._status(state, self.v2)
        self.assertEqual(status["progress"]["completed"], ["shared"])
        self.assertTrue(any("no integration" in j or "without integration evidence" in j
                            for j in status["engineering_judgment"]))
        # 4. handoff publication still carries it.
        self.assertIn("shared", json.dumps(bc._handoff(state)))

    def test_the_retrospective_waiver_still_reads_injected_progress_as_prospective_work(self):
        # Reader 2 of progress.completed, exercised directly now that the waiver is gone. The
        # earlier guards refuse a Build that already has plan-review evidence, so this case builds a
        # snapshot that reaches the progress condition and nothing else.
        with mock.patch.object(bc, "_head", return_value=HEAD_A):
            state = bc._initial_state("owner/repo", 7, BASE, PLAN_ID, SEALED, self.v2, None)
            state["approval"] = {"plan_digest": state["plan"]["digest"], "spec_digest": None, "depth": "quick"}
            store = bc.StateStore(str(Path(self.temp.name) / "waiver.json"))
            store.create(state)
            store.mutate(lambda s: s["progress"]["completed"].append({"id": "shared", "commit": HEAD_A}))
            # The waiver this used to exercise is gone; what still matters is that an injected
            # completion is read as unearned wherever progress is inspected.
            self.assertEqual(bc._unearned_completions(store.read()), ["shared"])

    def test_the_documented_remedy_actually_clears_the_gate(self):
        # The refusal is only half the requirement: the remedy it names must WORK. An unearned
        # completion is repaired by integrating the node for real, and the gate must then clear —
        # otherwise the mid-flight refusal is a permanent deadlock on exactly the snapshots it exists
        # to rescue, which is what shipped before this case was written.
        self._inject_unearned()
        self.assertEqual(bc._unearned_completions(self.state()), ["shared"])
        claim = bc.work.new_claim("1" * 32, HEAD_A, "/tmp/wt", [], {"executor_class": "builder",
                                 "provider": "claude", "model": "sonnet", "effort": "medium", "inline": False})
        item = next(i for i in self.v2["work_items"] if i["id"] == "shared")
        payload = {"outcome": "returned", "base_sha": HEAD_A,
                   "evidence": {"changed_paths": [".engine/tools/shared.py"],
                                "verification_results": ["focused tests green"]}}
        def stage_returned_attempt(state):
            nw = state["work"].setdefault("shared", bc.work.empty_node())
            nw["attempt_count"] = 1
            nw["claim"] = claim
            nw["latest_result"] = bc.work.bind_result(nw, item, "1" * 32, HEAD_A, payload)
        self.store.mutate(stage_returned_attempt)
        args = argparse.Namespace(item="shared", attempt="1" * 32, commit=HEAD_B,
                                  verification_input="focused tests green")
        with mock.patch.object(bc, "_commit_on_branch", return_value=True), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            bc.cmd_work_integrate(args, self.store)
        # The correction is announced: an operator recovering from the refusal must see that a stale
        # completion was rewritten, not just that an integration happened.
        self.assertIn("corrected the recorded completion for shared", out.getvalue())
        self.assertIn(HEAD_A[:12], out.getvalue())
        state = self.state()
        # The stale entry is corrected in place, not left beside the new integration evidence.
        self.assertEqual(state["progress"]["completed"], [{"id": "shared", "commit": HEAD_B}])
        self.assertEqual(bc._unearned_completions(state), [])

    def test_validation_is_refused_while_a_node_is_unintegrated(self):
        with self.assertRaises(bc.CoordinatorError) as caught:
            bc.cmd_validate(argparse.Namespace(plan=str(self.plan_path)), self.store)
        message = str(caught.exception)
        self.assertIn("unintegrated", message)
        self.assertIn("shared", message)
        self.assertIn("adapter", message)

    def test_v2_validation_without_the_plan_names_the_flag(self):
        with self.assertRaisesRegex(bc.CoordinatorError, r"validate --plan"):
            bc.cmd_validate(argparse.Namespace(plan=None), self.store)

    def test_the_governance_record_registers_the_hold(self):
        text = (bc.ROOT / ".engine" / "contracts" / "eADR-0041-build-coordinator-behavior.md").read_text(encoding="utf-8")
        self.assertIn("| A v2 work item is unintegrated, or is recorded complete without its integration commit |", text)
        self.assertIn("TestV2CompletionGate", text)

    def test_no_runbook_instructs_the_removed_mechanic(self):
        operations = bc.ROOT / ".engine" / "operations"
        routine = (operations / "routine-entry.md").read_text(encoding="utf-8")
        orchestration = (operations / "build-orchestration.md").read_text(encoding="utf-8")
        # The flag was refused for v2 and kept for v1; with the v1 sunset it is REMOVED, so every
        # surviving mention has to be a sentence saying so. A bare instruction to run it fails here,
        # and so does a sentence that still describes it as merely refused — a runbook telling an
        # operator a gone flag is "refused" sends them looking for a flag that is not there.
        for line in routine.splitlines() + orchestration.splitlines():
            if "--complete-item" in line:
                self.assertIn("removed", line,
                              "a runbook still describes the removed flag as available or refused: "
                              + line.strip())
        self.assertIn("work integrate", routine)
        self.assertIn("work integrate", orchestration)


class TestTheCanonSaysOneThing(unittest.TestCase):
    """The canon and the code must not disagree about where plan authority lives.

    eADR-0025 and eADR-0041 both described a world where a Build's plan was session-held and made
    durable by promotion to an Issue. That world is gone. A merged tree in which the code enters on a
    seal while the canon still says the session hands over a plan is a tree whose contracts cannot be
    trusted to mean anything — so the amendments are pinned here, by assertion id, at the same gate
    the behaviour is.

    These are text assertions on purpose. What they protect is not a mechanism but a promise about
    what a reader of the canon will find, and the failure they catch is a future change that edits the
    behaviour and leaves the record describing the old one.
    """

    @staticmethod
    def _contract(name):
        return (bc.ROOT / ".engine" / "contracts" / name).read_text(encoding="utf-8")

    def setUp(self):
        self.behavior = self._contract("eADR-0041-build-coordinator-behavior.md")
        self.claim = self._contract("eADR-0025-draft-pr-is-the-claim.md")

    def _row(self, identifier):
        rows = [line for line in self.behavior.splitlines() if line.startswith(f"| {identifier} |")]
        self.assertEqual(len(rows), 1, f"{identifier} must appear exactly once")
        return rows[0]

    def test_the_review_sections_genre_is_recorded_as_a_decision(self):
        """The canon test above asserts a FIXED list of rows, so a new one is invisible to it — a row
        could be missing or malformed and the suite would stay green. This reads BC-31 itself.

        It is asserted here because the change it records is a REDUCTION in what the merge surface
        discloses. A weakening that is not written where the behaviour is governed is a weakening
        nobody can find later, and the honesty of the record is the only thing standing behind it.
        """
        row = self._row("BC-31")
        self.assertIn("account", row.lower())
        # The measurement, not just the claim: a reader must be able to see what it cost and why.
        self.assertIn("60,000", row, "the budget the surface actually broke against")
        self.assertIn("1080", row, "the build that could not publish")
        # What survives, named — these are the guarantee, not the transcript that was dropped.
        for kept in ("still-blocking", "escalated", "rejected", "partially-accepted",
                     "accepted-tracked", "disagreement"):
            self.assertIn(kept, row, f"BC-31 must name {kept} among what still renders in full")
        # And the cost, stated rather than implied.
        self.assertIn("gitignored", row, "the plan library's locality is part of the decision")
        amendments = [l for l in self.behavior.splitlines() if l.startswith("_Amended 2026-08-25")]
        self.assertTrue(any("BC-31" in l for l in amendments),
                        "the dated amendment log must carry the row it introduced")

    def test_the_moved_assertions_are_corrected_in_place_not_left_standing(self):
        # Each of the five names the cutover as a source, so a reader can see WHEN it changed and why
        # — rather than finding a silently rewritten row with no history.
        for identifier in ("BC-04", "BC-05", "BC-12", "BC-18", "BC-19"):
            self.assertIn("2026-08-24", self._row(identifier), identifier)

    def test_no_amended_assertion_still_describes_the_retired_mechanics(self):
        for identifier in ("BC-04", "BC-05", "BC-12", "BC-18", "BC-19"):
            row = self._row(identifier)
            for retired in ("Promote only when", "promoted copy", "waives a now-retrospective"):
                self.assertNotIn(retired, row, f"{identifier} still describes a retired mechanic")

    def test_the_two_added_assertions_state_what_replaced_the_plan_gate(self):
        entry, coverage = self._row("BC-28"), self._row("BC-29")
        self.assertIn("only through a seal", entry)
        self.assertIn("build-plan.v1", entry)              # names what is refused at entry
        self.assertIn("approved depth", coverage)
        self.assertIn("roster", coverage)                  # the demand is keyed on the roster, not the name

    def test_the_weakened_hold_is_disclosed_as_a_weakening(self):
        """The honest half. The table states a bar for ADDING a hold and none for weakening one, so
        the only thing stopping a quiet edit is a session choosing to say so."""
        row = next(line for line in self.behavior.splitlines()
                   if line.startswith("| Required reviewer silently omitted |"))
        self.assertIn("WEAKENED", row)
        self.assertIn("2026-08-24", row)
        self.assertIn("states a bar for ADDING a hold and states none", row)
        self.assertNotIn("plan-review waiver, creating", row)   # the old escape clause is gone

    def test_the_new_hold_carries_its_demonstrated_failure(self):
        row = next(line for line in self.behavior.splitlines()
                   if line.startswith("| An unattended bind whose Issue does not correspond"))
        self.assertIn("authority nobody granted", row)
        self.assertIn("digest equality", row)              # what the guarantee used to be
        self.assertIn("intent_source", row)                # what replaced it

    def test_the_amendment_revises_the_2026_08_15_position_by_name(self):
        self.assertIn("introduced no new submission hard hold", self.behavior)
        self.assertIn("revised by name again here", self.behavior)

    def test_the_claim_record_no_longer_says_a_plan_is_session_held(self):
        self.assertIn("The Build's plan is not a GitHub record at all", self.claim)
        self.assertIn("2026-08-24", self.claim)
        for retired in ("promoted to a suitable writable Issue", "non-durable until a Build must"):
            self.assertNotIn(retired, self.claim, "eADR-0025 still describes promotion")

    def test_the_claim_record_states_what_the_change_costs(self):
        # The residual is the point of amending rather than deleting: recovery is workstation-only,
        # and the operator's read of the PR is what stands behind a merge.
        self.assertIn("workstation-only", self.claim)

    def test_never_a_durable_leg_is_separated_and_then_narrowed_rather_than_deleted(self):
        # One sentence about two things, taken apart one at a time rather than dropped. 2026-08-24
        # separated them; 2026-08-25 dropped the durability half for the Build's own state, because
        # a killed Build losing its evidence refuted it. What survives is the claim eADR-0025
        # actually rests on, and it must still be there in those words.
        self.assertIn("never plan authority", self.claim)
        self.assertIn("one sentence about two different things", self.claim)
        self.assertIn("carries no authority", self.claim)
        # The BOLDED phrase is the live claim; the earlier amendments quote the old wording as
        # history, which is the record working as intended and must not be rewritten.
        self.assertNotIn("**never a durable leg, never plan authority**", self.claim,
                         "eADR-0025 still claims the Build's own state is never durable")

    def test_the_claim_record_states_what_durable_build_state_costs(self):
        # Amending rather than deleting means the residuals are named: evidence now persists as long
        # as its plan folder, and durability is not portability.
        self.assertIn("Amended 2026-08-25", self.claim)
        self.assertIn("private notes", self.claim)
        self.assertIn("does not make a Build resumable on another machine", self.claim)


class TestV1Sunset(unittest.TestCase):
    """The v1 generation is gone, and this is the search that PROVES it — with its one exclusion.

    A removal is only as good as the evidence that nothing survived it, and a grep the author ran
    once while deleting is not that evidence. This class is the completeness search, run every time
    the suite runs, and its one exclusion is named on purpose: an exclusion nobody can find is
    indistinguishable from an oversight.

    What the search looks for is deliberately NOT the string "v1". A refusal has to name what it
    refuses — `plan bind` says the words "build-plan.v1" precisely because a message that would not
    name the version would leave an operator guessing — so banning the string would push the engine
    toward vaguer refusals in the name of tidiness. What must not survive is v1 as a LOADABLE
    SURFACE: a schema file, a path built to one, or a version key something dispatches on. Those are
    the two structural forms below, and they are what a survival actually looks like.
    """

    # A path built to a v1 schema file. That is what the FILE scan can identify without guessing:
    # a version key is spelled the same in a dispatch map and in an `if version == ...` refusal, so
    # dispatch is asserted directly against the real maps below instead of grepped for.
    SCHEMA_PATHS = ('"build-state.v1.json"', '"build-handoff.v1.json"', '"build-plan.v1.json"')

    # THE single exclusion, and the only one. `plan_contract` keeps a read-only build-plan.v1 payload
    # map, with its schema file, by the operator's decision of 2026-08-25: a plan library may still
    # hold a stored plan whose payload is v1, and a stored plan that cannot be READ is an operator's
    # own deliberation made unreachable by an engine upgrade. It is refused at every door and its
    # removal is tracked as issue 1070 in R05.
    EXCLUDED_CODE = "plan_contract.py"
    EXCLUDED_SCHEMA = "build-plan.v1.json"

    # Not exclusions — prose that must MENTION what was removed to be honest about removing it: a
    # decision record amends by dated paragraph and quotes what it corrected, a v2 schema says what
    # it extended, and a test names the thing whose absence it asserts.
    HISTORY = ("build-state.v2.json", "build-handoff.v2.json", "build-plan.v2.json")

    def _shipped_code_and_schemas(self):
        for directory in ("tools", "schemas"):
            for path in sorted((bc.ROOT / ".engine" / directory).rglob("*")):
                if not path.is_file() or path.suffix not in (".py", ".json"):
                    continue
                if path.name.startswith("test_") or path.name in self.HISTORY:
                    continue
                yield path

    def test_the_v1_state_and_handoff_schemas_are_gone(self):
        for gone in ("build-state.v1.json", "build-handoff.v1.json"):
            self.assertFalse((bc.ROOT / ".engine" / "schemas" / gone).exists(),
                             f"{gone} still ships after the v1 sunset")

    def test_the_kept_payload_map_is_the_single_code_exclusion(self):
        import plan_contract
        self.assertTrue((bc.ROOT / ".engine" / "schemas" / self.EXCLUDED_SCHEMA).exists())
        self.assertIn("build-plan.v1", plan_contract.BUILD_PLAN_SCHEMAS)
        source = (bc.ROOT / ".engine" / "tools" / self.EXCLUDED_CODE).read_text(encoding="utf-8")
        self.assertIn("issue 1070", source,
                      "the kept v1 map does not name the issue that retires it")

    def test_the_coordinator_dispatches_one_generation_only(self):
        self.assertEqual(sorted(bc.PLAN_SCHEMAS), ["build-plan.v2"])
        self.assertEqual(sorted(bc.STATE_SCHEMAS), ["build-state.v2"])
        self.assertEqual(sorted(bc.HANDOFF_SCHEMAS), ["build-handoff.v2"])
        for gone in ("PLAN_V1_REMOVE_AT_MAJOR", "cmd_plan_migrate_v1", "_migrate_v1_to_v2",
                     "PLAN_BEGIN", "HANDOFF_BEGIN"):
            self.assertFalse(hasattr(bc, gone), f"build_coordinator.{gone} survives the v1 sunset")

    def test_the_plan_marker_readers_retired_with_their_schemas(self):
        for gone in ("plan_block", "replace_plan_block", "durable_plan", "_plan_markers",
                     "_version_tag", "PLAN_BEGIN", "HANDOFF_BEGIN"):
            self.assertFalse(hasattr(bc.github, gone), f"github.{gone} survives the v1 sunset")

    def test_an_absent_schema_version_is_refused_never_defaulted(self):
        with self.assertRaisesRegex(bc.CoordinatorError, "does not state a schema_version"):
            bc._state_schema_for({"revision": 1})
        with self.assertRaisesRegex(bc.CoordinatorError, "does not state a schema_version"):
            bc.dag.plan_version({"work_items": []})

    def test_the_migrate_verb_is_gone_and_the_refusal_names_the_path(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            bc.parser().parse_args(["plan", "migrate-v1", "--input", "p.json"])
        source = (bc.ROOT / ".engine" / "tools" / "build_coordinator.py").read_text(encoding="utf-8")
        self.assertIn("is no converter", source)
        self.assertIn("Re-author this work as a fresh plan through the Project Manager", source)

    def test_no_v1_schema_path_or_dispatch_key_survives_outside_the_one_exclusion(self):
        strays = []
        for path in self._shipped_code_and_schemas():
            if path.name in (self.EXCLUDED_CODE, self.EXCLUDED_SCHEMA):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            strays += [f"{path.name}: {token}" for token in self.SCHEMA_PATHS if token in text]
        self.assertEqual(strays, [], "the v1 sunset left a loadable v1 surface behind: "
                         + ", ".join(strays))


class TestDepthsVerb(unittest.TestCase):
    """The `depths` advisory verb — the runnable form of the #763 chooser collapse. Stateless: it reads the
    committed protocol, the installed roster, and the shipped/operator per-depth effort, and offers only the
    depths that add coverage or effort over a lighter one."""

    @staticmethod
    def _roster(*lenses):
        return [{"lens": lens, "path": f".claude/agents/{lens}.md", "digest": "d"} for lens in lenses]

    def _run(self, deliverable_roster, as_json=False):
        out = io.StringIO()
        with mock.patch.object(bc, "_installed", return_value=deliverable_roster), contextlib.redirect_stdout(out):
            bc.cmd_depths(argparse.Namespace(json=as_json), None)
        return out.getvalue()

    def test_full_roster_offers_all_three_with_stepped_effort(self):
        deliverable_roster = self._roster("spec-conformance", "divergence-hunter", "usability",
                                          "technical-integrity", "security-governance")
        result = json.loads(self._run(deliverable_roster, as_json=True))
        self.assertEqual(result["available"], ["quick", "standard", "thorough"])
        self.assertIsNone(result["depths"]["quick"]["effort"])
        # Depth scales reviewer effort off the shipped review_depths defaults (standard steps down, thorough
        # holds the anchor); no operator override in the tree, so these are the shipped values.
        self.assertEqual(result["depths"]["standard"]["effort"], "medium")
        self.assertEqual(result["depths"]["thorough"]["effort"], "high")
        # Standard's deliverable gate runs its three lenses; the plan roster is no longer this verb's
        # business, and the plan side offers its own depths from its own roster.
        self.assertEqual(len(result["depths"]["standard"]["deliverable_lenses"]), 3)
        self.assertNotIn("plan_lenses", result["depths"]["standard"])

    def test_zero_reviewers_collapse_to_quick_alone(self):
        # The #763 heart: with no installed reviewers every heavier depth buys nothing, so only quick is offered.
        text = self._run([])
        self.assertIn("quick: no cold reviewers", text)
        self.assertIn("Collapsed", text)
        result = json.loads(self._run([], as_json=True))
        self.assertEqual(result["available"], ["quick"])

    def test_depths_needs_no_state(self):
        # The verb must run before approval, so main() must not demand --state for it.
        out = io.StringIO()
        with mock.patch.object(bc, "_installed", return_value=[]), contextlib.redirect_stdout(out):
            rc = bc.main(["depths", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue())["available"], ["quick"])


class TestAssumptionDisposition(CoordinatorCase):
    """The receipt-layer assumption-resolution mechanism (StarshipSuperjam/engine-template#1014)."""

    CLAIM = "eADR-0043 has no dependents"

    def _plan_unresolved(self, objective="Ship a small instrument panel"):
        value = plan(objective)
        value["assumptions"] = [{"claim": self.CLAIM, "status": "unresolved"}]
        return value

    def _seed_unresolved(self, depth="standard"):
        value = self._plan_unresolved()
        self.write_plan(value)
        self.store.create(bc._initial_state("owner/repo", 7, BASE, PLAN_ID, SEALED, value, None))
        self.approve(depth)
        return value

    def _status_now(self, value):
        with mock.patch.object(bc, "_head", return_value=HEAD_A), \
                mock.patch.object(bc, "_changed_paths", return_value=[]), \
                mock.patch.object(bc, "_must_run", return_value="1"):
            return bc._status(self.store.read(), value)

    def _dispose(self, resolved_as="verified", basis="the risk-governance lens verified it", claim=None):
        args = argparse.Namespace(plan=str(self.plan_path), claim=claim or self.CLAIM,
                                  resolved_as=resolved_as, basis=basis)
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_assumption_dispose(args, self.store)

    def test_unresolved_assumption_walls(self):
        value = self._seed_unresolved()
        status = self._status_now(value)
        self.assertTrue(any("investigate unresolved assumption" in j for j in status["engineering_judgment"]))
        # An in-flight state carries no dispositions key at all — the field is materialized lazily.
        self.assertNotIn("assumption_dispositions", self.state())

    def test_dispose_clears_wall_without_touching_plan_or_review(self):
        value = self._seed_unresolved()
        digest_before = self.state()["plan"]["digest"]
        approval_before = self.state()["approval"]
        self._dispose("verified")
        state = self.state()
        # The plan digest, approval, and review receipts are untouched — no re-review is forced.
        self.assertEqual(state["plan"]["digest"], digest_before)
        self.assertEqual(state["approval"], approval_before)
        self.assertEqual(state["assumption_dispositions"],
                         [{"claim": self.CLAIM, "resolved_as": "verified",
                           "basis": "the risk-governance lens verified it"}])
        status = self._status_now(value)
        self.assertFalse(any("investigate unresolved assumption" in j for j in status["engineering_judgment"]))

    def test_render_is_contradiction_free(self):
        # Feasibility lens: the overlay must feed BOTH sites, so a disposed assumption never yields a walling
        # judgment line while the phase clears (or vice versa).
        value = self._seed_unresolved()
        self._dispose("verified")
        status = self._status_now(value)
        judgment_names_it = any("investigate unresolved assumption" in j for j in status["engineering_judgment"])
        disclosed = any("resolved after approval" in w for w in status["warnings"])
        self.assertFalse(judgment_names_it)
        self.assertTrue(disclosed)

    def test_disclosure_present_for_verified_and_accepted_risk(self):
        for resolved_as in ("verified", "accepted-risk"):
            with self.subTest(resolved_as=resolved_as):
                self.setUp()
                value = self._seed_unresolved()
                self._dispose(resolved_as, basis="a stated basis")
                warnings = self._status_now(value)["warnings"]
                notes = [w for w in warnings if "resolved after approval" in w]
                self.assertEqual(len(notes), 1)
                self.assertIn(self.CLAIM, notes[0])
                self.assertIn(resolved_as, notes[0])
                self.assertIn("a stated basis", notes[0])

    def test_dispose_requires_a_basis(self):
        self._seed_unresolved()
        with self.assertRaisesRegex(bc.CoordinatorError, "basis"):
            self._dispose("verified", basis="   ")

    def test_dispose_refuses_unknown_claim(self):
        self._seed_unresolved()
        with self.assertRaisesRegex(bc.CoordinatorError, "no assumption with that exact claim"):
            self._dispose("verified", claim="a claim not in the plan")

    def test_dispose_refuses_an_already_authored_status(self):
        # plan()'s default assumption is authored 'verified' — it cannot be dispositioned.
        self.seed(); self.approve("standard")
        args = argparse.Namespace(plan=str(self.plan_path), claim="The harness can reproduce this JSON document.",
                                  resolved_as="verified", basis="x")
        with self.assertRaisesRegex(bc.CoordinatorError, "authored 'unresolved'"):
            bc.cmd_assumption_dispose(args, self.store)

    def test_plan_revision_clears_dispositions_even_when_the_claim_is_unchanged(self):
        # RG/architecture lens: a revision that keeps the assumption's claim byte-identical must still re-open
        # it — a stale disposition may not survive to silently re-clear the wall.
        value = self._seed_unresolved()
        self._dispose("verified")
        revised = self._plan_unresolved("A genuinely changed outcome")  # same unresolved claim, new digest
        self.write_plan(revised)
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_plan_revise(self.revise_args(), self.store)
        self.assertNotIn("assumption_dispositions", self.state())
        self.approve("standard")
        status = self._status_now(revised)
        self.assertTrue(any("investigate unresolved assumption" in j for j in status["engineering_judgment"]))

    def test_depth_change_preserves_dispositions(self):
        # A depth change keeps the plan digest, so a disposition rightly survives it (no needless re-open).
        value = self._seed_unresolved(depth="standard")
        self._dispose("verified")
        self.approve("thorough")
        self.assertEqual(len(self.state().get("assumption_dispositions", [])), 1)

    def test_state_without_dispositions_validates_and_dispose_materializes_lazily(self):
        # RG/feasibility lens back-compat: a state carrying no dispositions key is valid (schema optional),
        # and the key appears only once a disposition is recorded.
        self._seed_unresolved()
        self.assertNotIn("assumption_dispositions", self.state())  # store.create validated it
        self._dispose("verified")
        self.assertIn("assumption_dispositions", self.state())      # store.mutate re-validated it


class TestCoordinatorOwnedTag(CoordinatorCase):
    """The bind-time coordinator-ownership tag and the recurring reminder (StarshipSuperjam/engine-template#1014)."""

    def _bind(self):
        pr = {"number": 7, "state": "OPEN", "isDraft": True, "headRefOid": HEAD_A, "baseRefOid": BASE}
        with self.sealed(), mock.patch.object(bc, "_verify_draft", return_value=pr), \
                mock.patch.object(bc, "_head", return_value=HEAD_A), \
                mock.patch.object(bc, "_record_build_binding"), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            bc.cmd_plan_bind(self.bind_args(), self.store)
        return err.getvalue()

    def test_bind_tags_the_pr_coordinator_owned(self):
        with mock.patch.object(bc.github, "tag_coordinator_owned", return_value=True) as tag:
            self._bind()
        tag.assert_called_once_with(bc.ROOT, "owner/repo", 7)

    def test_bind_is_non_fatal_when_tagging_fails(self):
        with mock.patch.object(bc.github, "tag_coordinator_owned", return_value=False):
            err = self._bind()
        # The Build still bound (state created), and the failure is disclosed on stderr, not stdout.
        self.assertEqual(self.state()["build"]["pr"], 7)
        self.assertIn("coordinator-owned", err)

    def test_tag_helper_is_non_fatal_on_gh_failure(self):
        with mock.patch.object(bc.github.core, "must_run", side_effect=bc.core.CoordinatorError("gh boom")):
            self.assertFalse(bc.github.tag_coordinator_owned(bc.ROOT, "owner/repo", 7))

    def test_status_carries_the_reminder(self):
        self.seed(); self.approve("standard")
        out = io.StringIO()
        with mock.patch.object(bc, "_head", return_value=HEAD_A), \
                mock.patch.object(bc, "_changed_paths", return_value=[]), \
                mock.patch.object(bc, "_must_run", return_value="1"), contextlib.redirect_stdout(out):
            bc.cmd_status(argparse.Namespace(plan=str(self.plan_path), json=False), self.store)
        self.assertIn("submit apply", out.getvalue())
        self.assertIn("gh pr ready", out.getvalue())


if __name__ == "__main__":
    unittest.main()


class TestEvidenceDurability(CoordinatorCase):
    """A finding lives exactly as long as a receipt demands it, and review bindings survive a rebase.

    StarshipSuperjam/engine-template#1051 and #1000. Both defects are the same geometry: recorded review
    evidence bound to state the workflow itself later rewrites.
    """

    def setUp(self):
        super().setUp()
        self.seed(); self.approve("thorough")

    # --- shared scaffolding -------------------------------------------------------------

    def _deliverable_reviewed(self, lenses=("usability", "spec-conformance"), head=HEAD_A):
        """Land a completed deliverable review at `head`, the way a real Build reaches repair. The roster is
        passed as bare lens names to `_installed` and coerced by `_packet`, mirroring the established helper
        -- a hand-built roster of `{"lens": x}` dicts is not a real reviewer contract and is rejected."""
        args = argparse.Namespace(stage="deliverable", plan=str(self.plan_path), impact=None, session_effort="high")
        out = io.StringIO()
        self.store.mutate(lambda s: s.update({"validation": {
            "commit": head, "results": [{"id": "x", "commit": head, "passed": True, "summary": "ok"}]}}))
        with mock.patch.object(bc, "_installed", return_value=list(lenses)), \
                mock.patch.object(bc, "_head", return_value=head), \
                mock.patch.object(bc, "_base", return_value=BASE), \
                contextlib.redirect_stdout(out):
            bc._packet(args, self.store)
        pkt = json.loads(out.getvalue())
        for item in pkt["reviewer_contracts"]:
            with contextlib.redirect_stdout(io.StringIO()):
                bc.cmd_review_record(self.receipt_args(pkt, item["lens"], []), self.store)
        return pkt

    def receipt_args(self, packet, lens, findings):
        contract = next(item for item in packet["reviewer_contracts"] if item["lens"] == lens)
        return argparse.Namespace(stage=packet["stage"], lens=lens,
                                  packet_digest=packet["packet_digest"],
                                  lens_packet_digest=contract["lens_packet_digest"], finding=findings,
                                  findings_from_file=None, code_execution="none",
                                  delivered_effort="high")

    def _plan_reviewed(self):
        """Complete the deliverable panel so the phase driver can reach the later gates."""
        self.integrate_all()
        self.store.mutate(lambda s: s.update({"validation": {"commit": HEAD_A, "results": [
            {"id": "ci", "commit": HEAD_A, "passed": True, "summary": "ok"}]}}))
        args = argparse.Namespace(stage="deliverable", plan=str(self.plan_path), impact=None, session_effort="high")
        out = io.StringIO()
        roster = ["spec-conformance", "divergence-hunter", "usability"]
        with mock.patch.object(bc, "_installed", return_value=roster), \
                mock.patch.object(bc, "_head", return_value=HEAD_A), \
                mock.patch.object(bc, "_base", return_value=BASE), contextlib.redirect_stdout(out):
            bc._packet(args, self.store)
        pkt = json.loads(out.getvalue())
        for item in pkt["reviewer_contracts"]:
            with contextlib.redirect_stdout(io.StringIO()):
                bc.cmd_review_record(self.receipt_args(pkt, item["lens"], []), self.store)

    def _repair_packet(self, lenses, final=HEAD_B, reviewed=HEAD_A):
        self.store.mutate(lambda s: s.update({
            "repair": {"reviewed_commit": reviewed, "final_commit": final, "summary": "1 file",
                       "judgment": "scoped", "rationale": "Logic changed.", "lenses": list(lenses),
                       "packet_digest": None, "referent_digest": None, "reviewer_contracts": [],
                       "receipts": []},
            "validation": {"commit": final, "results": [{"id": "x", "commit": final, "passed": True, "summary": "ok"}]}}))
        args = argparse.Namespace(stage="repair", plan=str(self.plan_path), impact=None, session_effort="high")
        out = io.StringIO()
        with mock.patch.object(bc, "_installed", return_value=list(lenses)), \
                mock.patch.object(bc, "_head", return_value=final), \
                mock.patch.object(bc, "_base", return_value=BASE), contextlib.redirect_stdout(out):
            bc._packet(args, self.store)
        return json.loads(out.getvalue())

    # --- #1051: the wedge -----------------------------------------------------------------

    def test_a_regenerated_repair_packet_never_strands_the_findings_it_still_demands(self):
        """The wedge, end to end: a repair receipt is spliced into the deliverable stage, the repair packet
        is then re-cut for a different lens, and the spliced receipt survives carrying its OLD digest.
        Before the fix its findings were deleted while it kept demanding them, and `finding record` -- which
        could only ever write against the LIVE packet -- had no way to satisfy the demand. The Build could
        not leave finding-disposition, and the only escape found in practice was a full cold fan-out spent
        on bookkeeping."""
        self._deliverable_reviewed()
        pkt = self._repair_packet(["usability"])
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_review_record(self.receipt_args(pkt, "usability", ["R-1"]), self.store)
        # regenerate the repair packet for a DIFFERENT lens: the spliced receipt survives in the
        # deliverable stage carrying the old packet digest, and still demands R-1.
        self._repair_packet(["spec-conformance"], final=HEAD_B)
        self.assertEqual(bc.review.missing_findings(self.state()), ["R-1"])
        # the demand must be satisfiable -- this is the exact call that could not be made before.
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_finding_record(argparse.Namespace(
                id="R-1", stage="repair", lens="usability", severity="serious", summary="Repair concern",
                disposition="accepted-fixed", rationale="Fixed directly.", escalation_kind=None,
                blocks_this_pr_stated=False, handoff_summary="Repair concern"), self.store)
        self.assertEqual(bc.review.missing_findings(self.state()), [])

    def test_a_none_judgment_after_a_spliced_round_leaves_no_unrecordable_demand(self):
        """`none` clears the repair packet entirely, so before the fix there was no current packet to
        record against at all -- the same wedge in its worst form."""
        self._deliverable_reviewed()
        pkt = self._repair_packet(["usability"])
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_review_record(self.receipt_args(pkt, "usability", ["R-2"]), self.store)
        with mock.patch.object(bc, "_head", return_value=HEAD_C), \
                mock.patch.object(bc, "_must_run", return_value="1 file changed"), \
                mock.patch.object(repair_divergence, "classify",
                                  return_value=classification(authored=["app/main.py"])), \
                contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_repair_assess(argparse.Namespace(judgment="none", rationale="Verified directly.",
                                                    lens=None, accept_receipt_loss=True, guidance=None), self.store)
        self.assertIsNone(self.state()["repair"]["packet_digest"])
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_finding_record(argparse.Namespace(
                id="R-2", stage="repair", lens="usability", severity="nit", summary="Minor.",
                disposition="accepted-fixed", rationale="Fixed.", escalation_kind=None,
                blocks_this_pr_stated=False, handoff_summary="Minor."), self.store)
        self.assertEqual(bc.review.missing_findings(self.state()), [])

    def test_a_deliverable_regeneration_never_orphans_a_repair_finding(self):
        """The complementary leak, which the widen-the-filter approach would have left open: a DELIVERABLE
        re-cut drops the spliced repair receipt from the preserved set, but the old per-branch filter keyed
        on `f["stage"] != stage` and so left the repair finding behind. An orphan no receipt demands still
        counted toward `blocks_this_pr` and still rendered a disagreement line into the PR body."""
        self._deliverable_reviewed()
        pkt = self._repair_packet(["usability"])
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_review_record(self.receipt_args(pkt, "usability", ["R-3"]), self.store)
            bc.cmd_finding_record(argparse.Namespace(
                id="R-3", stage="repair", lens="usability", severity="blocking", summary="Serious.",
                disposition="accepted-fixed", rationale="Fixed.", escalation_kind=None,
                blocks_this_pr_stated=False, handoff_summary="Serious.",
                operator_summary="A repair-stage concern, fixed."), self.store)
        self.assertTrue(any(f["id"] == "R-3" for f in self.state()["findings"]))
        self.assertTrue(bc.review.required_disagreement_lines(self.state()))
        # A `none` judgment replaces the repair slot, so the ONLY receipt still demanding R-3 is the copy
        # spliced into the deliverable stage. Re-cutting the deliverable packet drops that copy.
        with mock.patch.object(bc, "_head", return_value=HEAD_C), \
                mock.patch.object(bc, "_must_run", return_value="1 file changed"), \
                mock.patch.object(repair_divergence, "classify",
                                  return_value=classification(authored=["app/main.py"])), \
                contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_repair_assess(argparse.Namespace(judgment="none", accept_receipt_loss=True, rationale="Verified.", lens=None,
                                                    guidance=None), self.store)
        self._deliverable_reviewed(lenses=("technical-integrity",), head=HEAD_C)
        kept = [f for f in self.state()["findings"] if f["id"] == "R-3"]
        self.assertEqual(len(kept), 1, "the finding was erased instead of being marked superseded")
        self.assertTrue(kept[0]["superseded"], "it outlived every receipt that demanded it, with weight")
        self.assertFalse(kept[0]["blocks_this_pr"])
        self.assertEqual(bc.review.missing_findings(self.state()), [])
        self.assertEqual(bc.review.required_disagreement_lines(self.state()), [])

    def test_finding_record_refuses_cleanly_when_no_live_receipt_names_the_lens(self):
        """Not a stack trace: `--stage repair` against an empty repair slot used to raise a bare KeyError,
        because state["reviews"] holds only plan and deliverable."""
        self._deliverable_reviewed()
        with self.assertRaises(bc.CoordinatorError):
            bc.cmd_finding_record(argparse.Namespace(
                id="R-9", stage="repair", lens="usability", severity="nit", summary="x",
                disposition="accepted-fixed", rationale="y", escalation_kind=None,
                blocks_this_pr_stated=False, handoff_summary="x"), self.store)

    # --- #1000: reconcile ------------------------------------------------------------------

    def _rebase_repo(self, diverge=False, repair_commit=False):
        """A real repository, a real branch, and a real rebase onto an advanced base."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        def git(*a, check=True):
            return subprocess.run(["git", "-C", str(tmp), *a], check=check,
                                  capture_output=True, text=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(tmp)], check=True)
        git("config", "user.email", "e@x"); git("config", "user.name", "n")
        # `_status` and `_packet` read the engine's own schemas and protocol from ROOT, which the probes
        # repoint at this scratch repo. Link them in read-only rather than mocking every reader.
        (tmp / ".engine").mkdir(exist_ok=True)
        for name in ("schemas", "check", "policies", "modules"):
            source = Path(bc.__file__).resolve().parents[1] / name
            if source.exists():
                (tmp / ".engine" / name).symlink_to(source)
        (tmp / "upstream.txt").write_text("one\n")
        git("add", "-A"); git("commit", "-qm", "base")
        base_before = git("rev-parse", "HEAD").stdout.strip()
        git("checkout", "-q", "-b", "work")
        (tmp / "mine.py").write_text("x = 1\n")
        git("add", "-A"); git("commit", "-qm", "mine")
        reviewed = git("rev-parse", "HEAD").stdout.strip()
        if repair_commit:
            (tmp / "repairfix.py").write_text("y = 2\n")
            git("add", "-A"); git("commit", "-qm", "repair round output")
            reviewed = git("rev-parse", "HEAD").stdout.strip()
        git("checkout", "-q", "main")
        (tmp / "upstream.txt").write_text("one\ntwo\n")
        git("add", "-A"); git("commit", "-qm", "upstream moved")
        base_after = git("rev-parse", "HEAD").stdout.strip()
        git("checkout", "-q", "work")
        git("rebase", "-q", "main")
        if diverge:
            # a real content change carried in with the rebase -- exactly the shape patch-id could not see
            (tmp / "mine.py").write_text("    x = 1\n")
            git("add", "-A"); git("commit", "-qm", "reindented during the rebase")
        head = git("rev-parse", "HEAD").stdout.strip()
        return tmp, base_before, reviewed, base_after, head

    def _reconcile(self, tmp, base_before, reviewed, base_after, head, deliverable_reviewed=None):
        anchor = deliverable_reviewed or reviewed
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update(
            {"reviewed_commit": anchor, "base_commit": base_before,
             "packet_digest": "sha256:" + "7" * 64}))
        self.store.mutate(lambda s: s.update(
            {"pr_contract": {"commit": head, "body_digest": "sha256:" + "8" * 64, "complete": True}}))
        out = io.StringIO()
        with mock.patch.object(bc, "ROOT", tmp), \
                mock.patch.object(bc, "_head", return_value=head), \
                mock.patch.object(bc, "_base", return_value=base_after), \
                mock.patch.object(bc.review_integrity, "snapshot", return_value=None), \
                contextlib.redirect_stdout(out):
            bc.cmd_reconcile(argparse.Namespace(plan=str(self.plan_path)), self.store)
        return out.getvalue()

    def test_an_unchanged_contribution_re_anchors_against_real_git(self):
        """A real rebase onto a real advanced base: the branch's own contribution is untouched, so the
        bindings move to the new head with no operator involvement and no repair round spent."""
        tmp, base_before, reviewed, base_after, head = self._rebase_repo()
        message = self._reconcile(tmp, base_before, reviewed, base_after, head)
        state = self.state()
        self.assertEqual(state["reviews"]["deliverable"]["reviewed_commit"], head)
        self.assertEqual(state["reviews"]["deliverable"]["base_commit"], base_after)
        self.assertEqual(len(state["reconciles"]), 1)
        self.assertTrue(state["reconciles"][0]["contribution_identical"])
        self.assertEqual(state["reconciles"][0]["divergent_paths"], [])
        self.assertEqual(state["repair_rounds"], [], "a clean re-anchor must not spend a repair round")
        self.assertIn("unchanged", message)
        # a body composed before the reconcile must not carry into readiness
        self.assertIsNone(state["pr_contract"])

    def test_a_reindented_line_is_divergent_even_though_patch_id_calls_it_identical(self):
        """The finding that replaced the original comparator. `git patch-id --stable` strips whitespace
        before hashing, so `x = 1` and `    x = 1` share an id -- in a Python tree a full semantic change
        measuring as identical, and it was the sole safeguard on a free re-anchor. Exact tree entries see
        it, and the divergent path routes back to `repair assess` rather than re-anchoring to head."""
        tmp, base_before, reviewed, base_after, head = self._rebase_repo(diverge=True)
        message = self._reconcile(tmp, base_before, reviewed, base_after, head)
        state = self.state()
        entry = state["reconciles"][0]
        self.assertFalse(entry["contribution_identical"])
        self.assertEqual(entry["divergent_paths"], ["mine.py"])
        # the weaker outcome carries MORE scrutiny: reviewed != head, so a repair judgment is still owed.
        self.assertEqual(state["reviews"]["deliverable"]["reviewed_commit"], base_after)
        self.assertNotEqual(state["reviews"]["deliverable"]["reviewed_commit"], head)
        self.assertIn("repair assess", message)

    def test_patch_id_would_have_called_the_reindent_identical(self):
        """Pins the reason the comparator is what it is, so nobody re-introduces patch-id as a
        simplification. If git ever changes this, this test says so."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        subprocess.run(["git", "init", "-q", str(tmp)], check=True)
        subprocess.run(["git", "-C", str(tmp), "config", "user.email", "e@x"], check=True)
        subprocess.run(["git", "-C", str(tmp), "config", "user.name", "n"], check=True)
        (tmp / "f.py").write_text("a\nb\nc\n")
        subprocess.run(["git", "-C", str(tmp), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(tmp), "commit", "-qm", "s"], check=True)
        def pid(text):
            (tmp / "f.py").write_text(text)
            diff = subprocess.run(["git", "-C", str(tmp), "diff"], capture_output=True, text=True).stdout
            return subprocess.run(["git", "-C", str(tmp), "patch-id", "--stable"],
                                  input=diff, capture_output=True, text=True).stdout.split()[0]
        self.assertEqual(pid("a\nb\nx = 1\nc\n"), pid("a\nb\n    x = 1\nc\n"))

    def test_reconcile_refuses_what_belongs_to_repair_assess(self):
        tmp, base_before, reviewed, base_after, head = self._rebase_repo()
        # still on the branch: ordinary forward divergence
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update(
            {"reviewed_commit": base_after, "base_commit": base_before}))
        with mock.patch.object(bc, "ROOT", tmp), mock.patch.object(bc, "_head", return_value=head), \
                mock.patch.object(bc, "_base", return_value=base_after), \
                self.assertRaisesRegex(bc.CoordinatorError, "repair assess"):
            bc.cmd_reconcile(argparse.Namespace(plan=str(self.plan_path)), self.store)
        # orphaned but the base never moved: an amend, which is a real content change
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update(
            {"reviewed_commit": reviewed, "base_commit": base_after}))
        with mock.patch.object(bc, "ROOT", tmp), mock.patch.object(bc, "_head", return_value=head), \
                mock.patch.object(bc, "_base", return_value=base_after), \
                self.assertRaisesRegex(bc.CoordinatorError, "repair assess"):
            bc.cmd_reconcile(argparse.Namespace(plan=str(self.plan_path)), self.store)

    def test_repair_assess_routes_a_rewritten_history_to_the_verb(self):
        tmp, base_before, reviewed, base_after, head = self._rebase_repo()
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update(
            {"reviewed_commit": reviewed, "base_commit": base_before}))
        with mock.patch.object(bc, "ROOT", tmp), mock.patch.object(bc, "_head", return_value=head), \
                mock.patch.object(bc, "_base", return_value=base_after), \
                self.assertRaisesRegex(bc.CoordinatorError, "reconcile"):
            bc.cmd_repair_assess(argparse.Namespace(judgment="none", rationale="r", lens=None,
                                                    guidance=None), self.store)

    def test_an_unmeasurable_rewrite_refuses_the_free_path(self):
        """A garbage-collected orphan cannot be compared. Fail closed: onto the base, not onto head."""
        tmp, base_before, reviewed, base_after, head = self._rebase_repo()
        self._reconcile(tmp, base_before, "f" * 40, base_after, head)
        entry = self.state()["reconciles"][0]
        self.assertFalse(entry["contribution_identical"])
        self.assertTrue(entry["unmeasurable"])
        self.assertEqual(self.state()["reviews"]["deliverable"]["reviewed_commit"], base_after)

    def test_a_second_rewrite_can_still_reach_the_clean_path(self):
        """`base_commit` is re-anchored too, so the two sides of the measurement stay the same width. Two
        occurrences in one Build is the recorded case (#994 and #1049), not a hypothetical."""
        tmp, base_before, reviewed, base_after, head = self._rebase_repo()
        self._reconcile(tmp, base_before, reviewed, base_after, head)
        self.assertEqual(self.state()["reviews"]["deliverable"]["base_commit"], base_after)
        tmp2, b2, r2, a2, h2 = self._rebase_repo()
        self._reconcile(tmp2, b2, r2, a2, h2)
        self.assertTrue(self.state()["reconciles"][-1]["contribution_identical"])

    def test_the_reconcile_reaches_the_operator_in_the_pr_body(self):
        """The disclosure must SAY what happened, not merely exist -- and the pre-existing repair line must
        not be inverted into a false 'no repair was needed' claim."""
        tmp, base_before, reviewed, base_after, head = self._rebase_repo()
        self.store.mutate(lambda s: s.update({"repair": {
            "reviewed_commit": base_before, "final_commit": reviewed, "summary": "3 files changed",
            "judgment": "scoped", "rationale": "r", "lenses": ["usability"], "packet_digest": None,
            "receipts": []}}))
        self._reconcile(tmp, base_before, reviewed, base_after, head)
        state = self.state()
        entry = state["reconciles"][0]
        # the repair record survives, so its truthful line survives with it
        self.assertIsNotNone(state["repair"])
        self.assertEqual(state["repair"]["summary"], "3 files changed")
        self.assertTrue(entry["contribution_identical"])
        self.assertEqual(entry["from_commit"], reviewed)
        self.assertEqual(entry["to_commit"], head)

    def test_a_repair_round_before_the_rewrite_does_not_burn_a_fabricated_round(self):
        """A completed repair round leaves commits that a later rebase orphans. Anchoring on them made
        `repair assess` measure `orphan..head` -- a span carrying the upstream commits the rebase pulled in
        -- and write a record whose reviewed_commit could never satisfy `repair_ready`, so the session
        assessed twice and one fabricated round counted toward the escalation threshold."""
        tmp, base_before, reviewed, base_after, head = self._rebase_repo()
        self.store.mutate(lambda s: s.update({"repair": {
            "reviewed_commit": base_before, "final_commit": reviewed, "summary": "1 file changed",
            "judgment": "scoped", "rationale": "r", "lenses": [], "packet_digest": None, "receipts": []}}))
        self._reconcile(tmp, base_before, reviewed, base_after, head)
        with mock.patch.object(bc, "ROOT", tmp), mock.patch.object(bc, "_head", return_value=head), \
                mock.patch.object(bc, "_base", return_value=base_after):
            # the orphaned repair record must not act as the live anchor once it is off the branch
            self.assertEqual(bc._effective_reviewed(self.state()), head)
            # ...so no re-review judgment is owed, and `repair assess` is not driven at an orphan
            status = bc._status(self.state())
            self.assertFalse(any("re-review" in j for j in status["engineering_judgment"]))
            with self.assertRaisesRegex(bc.CoordinatorError, "already the current head"):
                bc.cmd_reconcile(argparse.Namespace(plan=str(self.plan_path)), self.store)
        self.assertEqual(self.state()["repair_rounds"], [], "no fabricated round was burned")

    def test_status_never_crashes_where_the_merge_base_cannot_be_resolved(self):
        """`status` is the read-only command a stuck session runs FIRST; it must report, not raise."""
        tmp, base_before, reviewed, base_after, head = self._rebase_repo()
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update(
            {"reviewed_commit": reviewed, "base_commit": base_before}))
        with mock.patch.object(bc, "ROOT", tmp), mock.patch.object(bc, "_head", return_value=head), \
                mock.patch.object(bc, "_base", side_effect=bc.CoordinatorError("no origin/HEAD")):
            self.assertIsInstance(bc._status(self.state())["phase"], str)

    def test_status_routes_a_rewritten_history_before_the_session_is_stuck(self):
        """The point-of-action home: a session reading `status` must be told to reconcile BEFORE it tries
        `repair assess` and meets the refusal."""
        self._plan_reviewed()
        tmp, base_before, reviewed, base_after, head = self._rebase_repo()
        self._deliverable_reviewed(head=reviewed)
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update({"base_commit": base_before}))
        self.store.mutate(lambda s: s.update({"validation": {
            "commit": head, "results": [{"id": "x", "commit": head, "passed": True, "summary": "ok"}]}}))
        with mock.patch.object(bc, "ROOT", tmp), mock.patch.object(bc, "_head", return_value=head), \
                mock.patch.object(bc, "_base", return_value=base_after):
            status = bc._status(self.state())
        self.assertEqual(status["phase"], "repair-assessment")
        self.assertTrue(any("reconcile" in j for j in status["engineering_judgment"]))
        self.assertTrue(any("reconcile" in a for a in status["available_activities"]))
        # and it must NOT offer the activity that is a guaranteed refusal in this state
        self.assertFalse(any("proportional re-review" in a for a in status["available_activities"]))

    def test_no_private_plan_title_or_local_path_reaches_the_composed_body(self):
        """THE MERGE SURFACE, not one function at a time.

        Three separate composers read the same plan library and render its failure into the same
        pull-request body, and that body is pushed to a public repository. The library's own errors
        enumerate sibling plan slugs — the operator's private working titles for unrelated work — and
        absolute filesystem paths. Two of the three were fixed with per-function assertions, and the
        third, written a round earlier, went on quoting the exception until a lens found it.

        So this asserts on the RENDERED BODY: point the library at a real one holding plans with
        recognisable names, name a plan that does not resolve, compose, and require that nothing
        private survives anywhere in the output. A composer added later is covered without anyone
        remembering to add it here."""
        import build_coordinator_contract as bcc
        from test_build_coordinator_contract import _good_claim, _good_evidence
        import plan_store
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library = plan_store.PlanLibrary(root)
            # Real slugs, in the library's OWN shape. With anything else `resolve` reports "the library
            # is empty", carries no titles, and this test passes without ever exercising the leak —
            # which is exactly what it did the first time it was written.
            secrets = ["layoff-tooling-q3--aa11bb", "acquisition-terms-draft--cc22dd"]
            for index, slug in enumerate(secrets):
                folder = root / slug
                folder.mkdir(parents=True, exist_ok=True)
                (folder / "record.json").write_text(
                    json.dumps({"plan_id": f"pln_{index:012d}", "current": {"revision": 1}}),
                    encoding="utf-8")
            self.assertEqual(library.slugs(), sorted(secrets),
                             "the fixture must populate the library, or this test proves nothing")
            state = json.loads(json.dumps(self.state()))
            state["plan"]["plan_id"] = "pln_deadbeefdead"
            claim = _good_claim()
            claim["review"]["finding_summaries"] = []
            # THE REAL COMPOSER, not a hand-picked list of functions. The previous version of this test
            # called three composers by name and passed their output in as `consent_lines`, so it proved
            # only that those three behave — which is the property the finding said was missing. Three
            # further composers were leaking at the time it was green.
            with mock.patch.object(bc, "_library", return_value=library), \
                    mock.patch.object(bc, "_head", return_value=HEAD_A), \
                    mock.patch.object(bc, "_depth_effort", side_effect=OSError(f"{tmp}/policies/bindings.json")), \
                    mock.patch.object(bc, "_run", return_value=types.SimpleNamespace(
                        returncode=129, stdout="", stderr=f"fatal: not a git repository: '{tmp}/.git'")):
                evidence = bc._assemble_evidence(state, bc._plan(str(self.plan_path)), claim, HEAD_A,
                                                 {"body": "", "isDraft": True})
            body = bcc.compose(claim, evidence)
            self.assertNotIn(tmp, body, "an absolute local path reached the merge surface")
        # Silence is the other way to get this wrong: the body must still SAY each thing failed.
        for expected in ("could NOT be established", "could not be read", "could not be determined"):
            self.assertIn(expected, body, f"a failure was hidden rather than named: {expected}")
        for slug in secrets:
            self.assertNotIn(slug, body, f"a private plan title reached the merge surface: {slug}")
        self.assertNotIn("the library holds", body)
        self.assertNotIn("not a git repository", body, "a git error reached the merge surface verbatim")

    def test_the_reconcile_disclosure_reaches_the_operator_in_the_composed_body(self):
        """Drives the REAL composer: state -> drift line -> rendered pull-request body. The two rules this
        sentence must never break are that it cannot claim no repair happened when one did, and that the
        commit it names as submitted must be the one actually in the pull request."""
        import build_coordinator_contract as bcc
        from test_build_coordinator_contract import _good_claim, _good_evidence
        state = {"repair": {"reviewed_commit": BASE, "final_commit": HEAD_A, "summary": "3 files changed",
                            "judgment": "scoped", "rationale": "r", "lenses": [], "packet_digest": None,
                            "receipts": []},
                 "reconciles": [{"from_commit": HEAD_A, "to_commit": HEAD_B, "base_before": BASE,
                                 "base_after": HEAD_C, "contribution_identical": True,
                                 "divergent_paths": [], "unmeasurable": None}]}
        line = bc._drift_line(state, HEAD_B)
        # the submitted commit is the one in the PR, not the orphan the repair round ended on
        self.assertIn(f"submitted `{HEAD_B[:12]}`", line)
        self.assertNotIn(f"submitted `{HEAD_A[:12]}`", line)
        # a repair round that really happened is never denied
        self.assertIn("3 files changed", line)
        self.assertNotIn("no post-review repair was needed", line)
        self.assertIn("history was rewritten", line.lower())
        self.assertIn("verified unchanged", line)
        body = bcc.compose(_good_claim(), {**_good_evidence(), "drift_line": line})
        self.assertIn("Reviewed vs submitted", body)
        self.assertIn(f"submitted `{HEAD_B[:12]}`", body)

    def test_a_divergent_reconcile_names_the_paths_in_the_composed_body(self):
        state = {"repair": None,
                 "reconciles": [{"from_commit": HEAD_A, "to_commit": HEAD_B, "base_before": BASE,
                                 "base_after": HEAD_C, "contribution_identical": False,
                                 "divergent_paths": ["mine.py"], "unmeasurable": None}]}
        line = bc._drift_line(state, HEAD_B)
        self.assertIn("mine.py", line)
        self.assertNotIn("no post-review repair was needed", line)

    def test_two_receipts_naming_one_finding_id_keep_both_demands(self):
        """A single-key map let the last receipt iterated win, silently dropping the other demand and
        deleting an already-recorded disposition at the next regeneration."""
        state = {"reviews": {"deliverable": {"packet_digest": "sha256:" + "1" * 64, "receipts": [
                     {"lens": "architecture", "packet_digest": "sha256:" + "1" * 64,
                      "lens_packet_digest": "sha256:" + "a" * 64, "commit": None, "finding_ids": ["X-1"]},
                     {"lens": "usability", "packet_digest": "sha256:" + "1" * 64,
                      "lens_packet_digest": "sha256:" + "b" * 64, "commit": None, "finding_ids": ["X-1"]}]}},
                 "repair": None, "findings": []}
        demanded = bc.review.demanded_findings(state)
        self.assertEqual(len(demanded["X-1"]), 2)
        # Recorded under the FIRST receipt iterated, not the last: the old last-wins map kept the LAST
        # one's key, so a fixture using `usability` here passes against the defect and proves nothing.
        state["findings"] = [{"id": "X-1", "stage": "deliverable", "lens": "architecture",
                              "packet_digest": "sha256:" + "1" * 64,
                              "lens_packet_digest": "sha256:" + "a" * 64, "commit": None}]
        self.assertEqual(bc.review.missing_findings(state), [])
        self.assertEqual(len(bc.review.surviving_findings(state)), 1)

    def test_a_repair_round_before_the_rewrite_still_reaches_the_clean_path(self):
        """The over-correction the repair review caught: demoting the round's final commit merely because
        a rebase orphaned it made `reconcile` measure against a PRE-repair commit, so the round's own files
        read as divergent and the Build that had repaired before rebasing was denied the clean re-anchor --
        the very cost the demotion was introduced to prevent."""
        tmp, base_before, reviewed, base_after, head = self._rebase_repo(repair_commit=True)
        self.store.mutate(lambda s: s.update({"repair": {
            "reviewed_commit": base_before, "final_commit": reviewed, "summary": "1 file changed",
            "judgment": "scoped", "rationale": "r", "lenses": [], "packet_digest": None, "receipts": []}}))
        # the deliverable binding is the PRE-repair commit, as it is until a round's receipts land
        self._reconcile(tmp, base_before, base_before, base_after, head, deliverable_reviewed=base_before)
        entry = self.state()["reconciles"][0]
        self.assertEqual(entry["from_commit"], reviewed, "the round's output is what was last reviewed")
        self.assertTrue(entry["contribution_identical"],
                        f"the repair round's own files read as divergent: {entry['divergent_paths']}")
        self.assertEqual(self.state()["repair_rounds"], [])

    def _R(self, frm, to, bb, ba, identical, paths, anchored, unmeasurable=None):
        return {"from_commit": frm, "to_commit": to, "base_before": bb, "base_after": ba,
                "contribution_identical": identical, "divergent_paths": paths,
                "unmeasurable": unmeasurable, "anchored_to": anchored}

    def _P(self, reviewed, final, summary, judgment):
        return {"reviewed_commit": reviewed, "final_commit": final, "summary": summary,
                "judgment": judgment, "rationale": "r", "lenses": [], "packet_digest": None,
                "receipts": []}

    def test_the_disclosure_claims_no_ordering_it_cannot_support(self):
        """Three successive attempts to lead with "the reviewed commit" and to say whether a repair ran
        before or after a rewrite each produced a sentence that was wrong on some reachable flow — the last
        contradicting itself inside one line. Neither fact is recoverable once history is rewritten, so the
        line states neither. What it must never do is name a commit as the review's starting point."""
        after = {"repair": self._P(HEAD_C, HEAD_B, "2 files", "none"),
                 "reconciles": [self._R(HEAD_A, HEAD_B, BASE, HEAD_C, False, ["x.py"], HEAD_C)]}
        line = bc._drift_line(after, HEAD_B)
        # the contradiction that survived two repairs: "reviewed C" beside "re-anchored from A"
        self.assertNotIn(f"reviewed `{HEAD_C[:12]}`", line)
        self.assertNotIn("ran before it", line)
        self.assertNotIn("ran afterwards", line)
        # what it does say is true and checkable
        self.assertIn(f"submitted `{HEAD_B[:12]}`", line)
        self.assertIn(f"from `{HEAD_A[:12]}` to `{HEAD_C[:12]}`", line)
        self.assertIn("judged not to need re-review", line)
        self.assertIn("x.py", line)

    def test_a_repair_before_a_rewrite_is_reported_without_an_order_claim(self):
        state = {"repair": self._P(BASE, HEAD_A, "3 files changed", "scoped"),
                 "reconciles": [self._R(HEAD_A, HEAD_B, BASE, HEAD_C, True, [], HEAD_B)]}
        line = bc._drift_line(state, HEAD_B)
        self.assertIn(f"carried `{BASE[:12]}` to `{HEAD_A[:12]}`", line)
        self.assertIn("3 files changed", line)
        self.assertNotIn("ran before it", line)
        self.assertIn("order relative to one another is not recorded", line)

    def test_a_none_judgment_is_never_rendered_as_a_completed_re_review(self):
        """Including on the no-rewrite path, which the earlier fix left untouched: a shortstat alone read
        identically whether lenses had been dispatched or not."""
        line = bc._drift_line({"repair": self._P(BASE, HEAD_A, "2 files changed", "none"),
                               "reconciles": []}, HEAD_A)
        self.assertIn("no re-review was judged necessary", line)
        scoped = bc._drift_line({"repair": self._P(BASE, HEAD_A, "2 files changed", "scoped"),
                                 "reconciles": []}, HEAD_A)
        self.assertNotIn("no re-review", scoped)

    def test_the_reconcile_disclosure_reaches_the_operator_in_the_composed_body(self):
        """Drives the REAL composer: state -> drift line -> rendered pull-request body."""
        import build_coordinator_contract as bcc
        from test_build_coordinator_contract import _good_claim, _good_evidence
        state = {"repair": self._P(BASE, HEAD_A, "3 files changed", "scoped"),
                 "reconciles": [self._R(HEAD_A, HEAD_B, BASE, HEAD_C, True, [], HEAD_B)]}
        line = bc._drift_line(state, HEAD_B)
        self.assertIn(f"submitted `{HEAD_B[:12]}`", line)
        self.assertIn("3 files changed", line)
        self.assertNotIn("no post-review repair was needed", line)
        self.assertIn("verified unchanged", line)
        body = bcc.compose(_good_claim(), {**_good_evidence(), "drift_line": line})
        self.assertIn("Reviewed vs submitted", body)
        self.assertIn(f"submitted `{HEAD_B[:12]}`", body)

    def test_a_divergent_reconcile_names_the_paths_in_the_composed_body(self):
        line = bc._drift_line({"repair": None,
                               "reconciles": [self._R(HEAD_A, HEAD_B, BASE, HEAD_C, False,
                                                      ["mine.py"], HEAD_C)]}, HEAD_B)
        self.assertIn("mine.py", line)
        self.assertNotIn("no post-review repair was needed", line)
        # the binding stopped at the new base; the line must not claim it reached head
        self.assertIn(f"to `{HEAD_C[:12]}`", line)

    def test_two_rewrites_are_listed_separately(self):
        line = bc._drift_line({"repair": None, "reconciles": [
            self._R(HEAD_A, HEAD_B, BASE, HEAD_C, True, [], HEAD_B),
            self._R(HEAD_B, HEAD_C, HEAD_C, HEAD_A, False, ["foo.py"], HEAD_A)]}, HEAD_C)
        self.assertEqual(line.count("history was rewritten"), 2)
        self.assertIn("; history was rewritten", line)
        self.assertIn("order relative to one another is not recorded", line)

    def test_a_superseded_finding_keeps_its_record_but_loses_its_weight(self):
        """When a lens is re-run, its repair receipt replaces the deliverable receipt, so the earlier
        findings are demanded by nothing. Deleting them dropped the first review round out of the
        pull-request body entirely — an operator saw only whichever lenses happened not to be re-run."""
        self._deliverable_reviewed()
        pkt = self._repair_packet(["usability"])
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_review_record(self.receipt_args(pkt, "usability", ["R-8"]), self.store)
            bc.cmd_finding_record(argparse.Namespace(
                id="R-8", stage="repair", lens="usability", severity="blocking", summary="Earlier concern",
                disposition="accepted-fixed", rationale="Fixed.", escalation_kind=None,
                blocks_this_pr_stated=False, handoff_summary="Earlier concern",
                operator_summary="An earlier concern, fixed."), self.store)
        self.assertTrue(bc.review.required_disagreement_lines(self.state()))
        # Close the round, then re-run a different lens at a newer commit: the repair slot's own receipt
        # goes with the round, and the deliverable re-cut drops the copy spliced into that stage, so
        # nothing demands R-8 any more.
        with mock.patch.object(bc, "_head", return_value=HEAD_C), \
                mock.patch.object(bc, "_must_run", return_value="1 file changed"), \
                mock.patch.object(repair_divergence, "classify",
                                  return_value=classification(authored=["app/main.py"])), \
                contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_repair_assess(argparse.Namespace(judgment="none", accept_receipt_loss=True, rationale="Verified.", lens=None,
                                                    guidance=None), self.store)
        self._deliverable_reviewed(lenses=("technical-integrity",), head=HEAD_C)
        kept = [f for f in self.state()["findings"] if f["id"] == "R-8"]
        self.assertEqual(len(kept), 1, "the earlier round's finding was erased from the record")
        self.assertTrue(kept[0]["superseded"])
        self.assertFalse(kept[0]["blocks_this_pr"])
        # ...it no longer carries weight...
        self.assertEqual(bc.review.required_disagreement_lines(self.state()), [])
        self.assertEqual(bc.review.missing_findings(self.state()), [])
        # ...but the record an operator reads still carries it, with its disposition intact
        self.assertEqual(kept[0]["disposition"], "accepted-fixed")
        self.assertEqual(kept[0]["operator_summary"], "An earlier concern, fixed.")

    def test_the_assembler_passes_the_real_head_to_the_disclosure(self):
        """The wiring half of the seam: the pure function is correct, but nothing asserted the assembler
        hands it the CURRENT head rather than a stale commit. Pinned as source text, which is honest about
        what it checks — patching the function and then calling it from the test would assert a mock
        against itself and prove nothing about production."""
        import inspect
        self.assertIn("_drift_line(state, head)", inspect.getsource(bc._assemble_evidence))

    def test_repair_assess_refuses_legibly_when_the_anchor_is_unreadable(self):
        """A garbage-collected anchor produced a raw `Invalid revision range` from git."""
        tmp, base_before, reviewed, base_after, head = self._rebase_repo()
        self.store.mutate(lambda s: s["reviews"]["deliverable"].update(
            {"reviewed_commit": "f" * 40, "base_commit": base_after}))
        with mock.patch.object(bc, "ROOT", tmp), mock.patch.object(bc, "_head", return_value=head), \
                mock.patch.object(bc, "_base", return_value=base_after), \
                self.assertRaisesRegex(bc.CoordinatorError, "no longer readable"):
            bc.cmd_repair_assess(argparse.Namespace(judgment="none", rationale="r", lens=None,
                                                    guidance=None), self.store)

    def test_reconciles_survive_a_handoff_round_trip_and_a_legacy_restore(self):
        entry = {"from_commit": HEAD_A, "to_commit": HEAD_B, "base_before": BASE, "base_after": HEAD_C,
                 "contribution_identical": True, "divergent_paths": [], "unmeasurable": None}
        value = {"build": {"repository": "owner/repo", "pr": 7, "base_at_bind": BASE, "mode": "same-session"},
                 "plan": self.state()["plan"], "approval": self.state()["approval"],
                 "reviews": self.state()["reviews"], "finding_summaries": [],
                 "progress": {}, "validation": None, "repair": None, "preflights": [], "pr_contract": None,
                 "repair_rounds": [], "plan_change_escalations": [],
                 "reconciles": [entry]}
        restored = bc._restore_base_state(value, "build-state.v1")
        self.assertEqual(restored["reconciles"], [entry])
        legacy = bc._restore_base_state({k: v for k, v in value.items() if k != "reconciles"},
                                        "build-state.v1")
        self.assertEqual(legacy["reconciles"], [])
