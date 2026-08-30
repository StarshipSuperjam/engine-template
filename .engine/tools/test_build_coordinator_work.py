#!/usr/bin/env python3
"""Tests for the DAG work verbs: packets, claims, results, and provider routing."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_coordinator as bc  # noqa: E402
import build_coordinator_work as work  # noqa: E402
from test_build_coordinator import plan_v1, plan_v2, _work_item_v2, HEAD_A, BASE, PLAN_ID, SEALED  # noqa: E402
import build_coordinator_dag as dag  # noqa: E402
import build_coordinator_github as ghub  # noqa: E402


class WorkCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        head = mock.patch.object(bc, "_head", return_value=HEAD_A)
        head.start()
        self.addCleanup(head.stop)
        self.state_path = str(Path(self.temp.name) / "build.json")
        self.store = bc.StateStore(self.state_path)
        self.plan_value = plan_v2()
        self.plan_path = Path(self.temp.name) / "plan.json"
        self.write_plan(self.plan_value)
        state = bc._initial_state("owner/repo", 7, BASE, PLAN_ID, SEALED, self.plan_value, None)
        state["approval"] = {"plan_digest": bc._digest(self.plan_value), "spec_digest": None, "depth": "thorough"}
        self.store.create(state)

    def write_plan(self, value):
        self.plan_value = value
        self.plan_path.write_text(json.dumps(value), encoding="utf-8")

    def claim(self, item, provider="claude", worktree="/tmp/wt"):
        args = argparse.Namespace(item=item, provider=provider, plan=str(self.plan_path), worktree=worktree)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            bc.cmd_work_claim(args, self.store)
        return json.loads(out.getvalue())

    def result(self, item, attempt, payload):
        path = Path(self.temp.name) / "result.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        args = argparse.Namespace(item=item, attempt=attempt, plan=str(self.plan_path), input=str(path))
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_work_result(args, self.store)

    def state(self):
        return self.store.read()


class TestWorkClaims(WorkCase):
    def test_claim_emits_bounded_packet_and_records_the_attempt(self):
        packet = self.claim("shared")
        self.assertEqual(packet["node"]["id"], "shared")
        self.assertNotIn("adapter", json.dumps(packet["node"]))  # no sibling nodes
        self.assertEqual(packet["route"], {"executor_class": "builder", "provider": "claude",
                                           "model": "sonnet", "effort": "medium", "inline": False})
        nw = self.state()["work"]["shared"]
        self.assertEqual(nw["attempt_count"], 1)
        self.assertEqual(nw["claim"]["attempt_id"], packet["attempt_id"])

    def test_claim_of_a_blocked_node_is_refused(self):
        with self.assertRaisesRegex(bc.CoordinatorError, "not claimable"):
            self.claim("adapter")

    def test_serial_policy_refuses_a_second_concurrent_claim(self):
        self.write_plan(plan_v2(items=[_work_item_v2("a", []), _work_item_v2("b", [])]))
        state = bc._initial_state("owner/repo", 7, BASE, PLAN_ID, SEALED, self.plan_value, None)
        state["approval"] = {"plan_digest": bc._digest(self.plan_value), "spec_digest": None, "depth": "thorough"}
        os.remove(self.state_path)
        self.store.create(state)
        self.claim("a")
        with self.assertRaisesRegex(bc.CoordinatorError, "not claimable"):
            self.claim("b")

    def test_result_binds_to_attempt_and_rejects_a_stale_attempt(self):
        packet = self.claim("shared")
        attempt = packet["attempt_id"]
        evidence = {"changed_paths": [".engine/tools/shared.py"], "verification_results": ["ok"]}
        self.result("shared", attempt, {"outcome": "returned", "base_sha": HEAD_A, "evidence": evidence})
        self.assertEqual(self.state()["work"]["shared"]["latest_result"]["outcome"], "returned")
        with self.assertRaisesRegex(bc.CoordinatorError, "does not match the active claim"):
            self.result("shared", "f" * 32, {"outcome": "returned", "base_sha": HEAD_A, "evidence": evidence})

    def test_result_from_the_wrong_base_is_rejected(self):
        packet = self.claim("shared")
        with self.assertRaisesRegex(bc.CoordinatorError, "does not match the claimed base"):
            self.result("shared", packet["attempt_id"],
                        {"outcome": "returned", "base_sha": "b" * 40,
                         "evidence": {"changed_paths": ["x"], "verification_results": ["ok"]}})

    def test_returned_result_missing_contract_evidence_is_rejected(self):
        packet = self.claim("shared")
        with self.assertRaisesRegex(bc.CoordinatorError, "missing output-contract evidence"):
            self.result("shared", packet["attempt_id"],
                        {"outcome": "returned", "base_sha": HEAD_A, "evidence": {"changed_paths": ["x"]}})

    def test_claim_refusal_names_the_cause(self):
        # The refusal now carries the typed deferral kind and its detail, read out of the same
        # admission derivation that `status` and `work frontier` render — so a refusal and the
        # deferral reason a session was just shown can never disagree.
        with self.assertRaisesRegex(bc.CoordinatorError, "not claimable now: dependency — waiting on shared"):
            self.claim("adapter")  # blocked on shared

    def test_packet_preview_reports_claimability(self):
        args = argparse.Namespace(item="adapter", provider="claude", plan=str(self.plan_path), worktree="/tmp/wt")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            bc.cmd_work_packet(args, self.store)
        preview = json.loads(out.getvalue())["preview"]
        self.assertFalse(preview["claimable_now"])
        self.assertEqual(preview["state"], "blocked")
        self.assertIn("dependency", preview["refusal_reason"])



    def test_result_verb_guards_with_compare_and_swap(self):
        packet = self.claim("shared")   # revision advances to 2
        path = Path(self.temp.name) / "r.json"
        path.write_text(json.dumps({"outcome": "returned", "base_sha": HEAD_A,
                                    "evidence": {"changed_paths": [".engine/tools/shared.py"], "verification_results": ["ok"]}}))
        stale = bc.StateStore(self.state_path, expected_revision=1)
        args = argparse.Namespace(item="shared", attempt=packet["attempt_id"], plan=str(self.plan_path), input=str(path))
        with self.assertRaisesRegex(bc.CoordinatorError, "reload status"):
            bc.cmd_work_result(args, stale)

    def test_attach_records_the_worker_reference(self):
        packet = self.claim("shared")
        args = argparse.Namespace(item="shared", attempt=packet["attempt_id"], worker_ref="task-123")
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_work_attach(args, self.store)
        self.assertEqual(self.state()["work"]["shared"]["claim"]["worker_ref"], "task-123")

    def test_packet_preview_reports_the_unapproved_gate(self):
        # The preview honors the same approval gate the claim checks first, so a clean preview is
        # never followed by a surprise "gate is not approved" refusal.
        self.store.mutate(lambda s: s.update({"approval": None}))
        args = argparse.Namespace(item="shared", provider="claude", plan=str(self.plan_path), worktree="/tmp/wt")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            bc.cmd_work_packet(args, self.store)
        preview = json.loads(out.getvalue())["preview"]
        self.assertFalse(preview["claimable_now"])
        self.assertIn("not approved", preview["refusal_reason"])

    def test_every_work_verb_rejects_a_stale_snapshot_revision(self):
        # SC-9: the compare-and-swap guard is demonstrated for each verb, not just result/attach.
        self.claim("shared")  # revision advances past 1
        for invoke in (
            lambda s: bc.cmd_work_claim(argparse.Namespace(item="shared", provider="claude",
                                                           plan=str(self.plan_path), worktree="/tmp/wt"), s),
            lambda s: bc.cmd_work_reject(argparse.Namespace(item="shared", attempt="0" * 32,
                                                            rejection_class="worker", reason="x"), s),
            lambda s: bc.cmd_work_retry(argparse.Namespace(item="shared", strategy="redispatch", reason="x"), s),
            lambda s: bc.cmd_work_abandon(argparse.Namespace(item="shared", attempt="0" * 32, reason="x"), s),
            lambda s: bc.cmd_work_integrate(argparse.Namespace(item="shared", attempt="0" * 32,
                                                               commit=HEAD_A, verification_input="v"), s),
        ):
            stale = bc.StateStore(self.state_path, expected_revision=1)
            with self.assertRaisesRegex(bc.CoordinatorError, "reload status"):
                with mock.patch.object(bc, "_commit_on_branch", return_value=True):
                    invoke(stale)

    def test_a_snapshot_without_a_work_map_names_the_actual_cause(self):
        # The v1 arm of this case is gone with v1 entry itself — no snapshot without a work map can be
        # WRITTEN any more. The guard still earns its place as the reader-side floor, and is pinned
        # directly rather than through a snapshot the schemas now refuse to hold.
        state = bc._initial_state("owner/repo", 7, BASE, PLAN_ID, SEALED, plan_v2(), None)
        state.pop("work")
        with self.assertRaisesRegex(bc.CoordinatorError, "require a build-plan.v2 Build"):
            bc._node_work(state, "W1")

    def test_each_work_verb_guards_with_compare_and_swap(self):
        self.claim("shared")  # revision advances to 2
        stale = bc.StateStore(self.state_path, expected_revision=1)
        args = argparse.Namespace(item="shared", attempt="0" * 32, worker_ref="x")
        with self.assertRaisesRegex(bc.CoordinatorError, "reload status"):
            bc.cmd_work_attach(args, stale)


class TestFrontierProjection(WorkCase):
    """`work frontier` explains the admission decision and changes nothing."""

    def frontier(self, as_json=True):
        args = argparse.Namespace(plan=str(self.plan_path), json=as_json)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            bc.cmd_work_frontier(args, self.store)
        return json.loads(out.getvalue()) if as_json else out.getvalue()

    def test_frontier_writes_nothing(self):
        before_revision = self.state()["revision"]
        before_bytes = Path(self.state_path).read_bytes()
        self.frontier()
        self.assertEqual(self.state()["revision"], before_revision)
        self.assertEqual(Path(self.state_path).read_bytes(), before_bytes)

    def test_frontier_names_the_admitted_node_and_every_deferral(self):
        projection = self.frontier()
        self.assertEqual(projection["admitted"], ["shared"])
        self.assertEqual(projection["next_ready"], "shared")
        self.assertEqual([(d["id"], d["kind"]) for d in projection["deferred"]],
                         [("adapter", dag.DEFER_DEPENDENCY)])
        self.assertEqual(projection["critical_path"], {"shared": 2, "adapter": 1})
        self.assertEqual(projection["admission_rank"], ["shared", "adapter"])

    def test_frontier_refuses_a_plan_that_is_not_the_approved_one(self):
        other = plan_v2(objective="A different graph entirely")
        path = Path(self.temp.name) / "other.json"
        path.write_text(json.dumps(other), encoding="utf-8")
        args = argparse.Namespace(plan=str(path), json=True)
        with self.assertRaisesRegex(bc.CoordinatorError, "does not match"):
            bc.cmd_work_frontier(args, self.store)

    def test_frontier_human_render_carries_the_deferral_reason(self):
        text = self.frontier(as_json=False)
        self.assertIn("deferred adapter: dependency", text)
        self.assertIn("shared[2]", text)

    def test_frontier_render_reconciles_a_node_that_is_both_claimable_and_deferred(self):
        # This verb is where a session is SENT to understand the admission decision, so the one place
        # the two lists can look contradictory must resolve it on the line itself. Two independent
        # roots under a serial plan: both claimable, the lower-ranked one deferred on capacity.
        self.write_plan(plan_v2(items=[_work_item_v2("a", []), _work_item_v2("b", [])]))
        os.remove(self.state_path)
        self.store = bc.StateStore(self.state_path)
        state = bc._initial_state("owner/repo", 7, BASE, PLAN_ID, SEALED, self.plan_value, None)
        state["approval"] = {"plan_digest": bc._digest(self.plan_value), "spec_digest": None, "depth": "thorough"}
        self.store.create(state)
        projection = self.frontier()
        self.assertEqual(projection["admitted"], ["a"])
        self.assertIn("b", projection["claimable"])
        self.assertEqual([d["kind"] for d in projection["deferred"]], [dag.DEFER_CAPACITY])
        text = self.frontier(as_json=False)
        self.assertIn("deferred b: capacity", text)
        self.assertIn("(still claimable directly)", text)
        # ...and the reason must not claim an occupancy the slot line above it contradicts.
        self.assertIn("Frontier: 0 of 1 worker slot(s) in use", text)
        self.assertNotIn("all 1 worker slot(s) are in use", text)

    def test_the_claim_verb_recomputes_the_frontier_under_the_lock(self):
        # The frontier the claim enforces is derived INSIDE store.mutate — from the state re-read
        # under the lock, not from anything the caller sampled earlier. Proved by moving the graph
        # underneath a claim that was admissible at call time: a sibling claim lands first (taking
        # the only serial slot), and the second claim is refused on the re-read state.
        self.write_plan(plan_v2(items=[_work_item_v2("a", []), _work_item_v2("b", [])]))
        os.remove(self.state_path)
        self.store = bc.StateStore(self.state_path)
        state = bc._initial_state("owner/repo", 7, BASE, PLAN_ID, SEALED, self.plan_value, None)
        state["approval"] = {"plan_digest": bc._digest(self.plan_value), "spec_digest": None, "depth": "thorough"}
        self.store.create(state)
        self.assertEqual(dag.claimable_set(self.plan_value, self.state()), ["a", "b"])
        self.claim("a")
        with self.assertRaisesRegex(bc.CoordinatorError, "capacity"):
            self.claim("b")

    def test_no_new_constraint_reaches_the_plan_validator_or_the_v2_schema(self):
        # Ranking and deferrals are DERIVED; they add no rule about what a valid plan is. A
        # conditional plan at max_concurrency 1 was sealable before this change and still validates —
        # narrowing it would invalidate already-sealed records on read (plan_contract validates
        # through this same single-homed function).
        value = plan_v2(items=[_work_item_v2("a", []), _work_item_v2("b", ["a"])],
                        mode="conditional", max_concurrency=1)
        dag.validate_plan_document(value, bc.PLAN_SCHEMAS)
        path = Path(self.temp.name) / "conditional.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(bc._plan(str(path))["parallelism"], {"mode": "conditional", "max_concurrency": 1})
class TestResultEdges(WorkCase):
    def test_worker_failed_report_records_failure_and_derives_failed(self):
        packet = self.claim("shared")
        self.result("shared", packet["attempt_id"],
                    {"outcome": "failed", "base_sha": HEAD_A, "class": "worker", "reason": "boom", "evidence": {}})
        nw = self.state()["work"]["shared"]
        self.assertEqual(nw["latest_failure"]["disposition"], "open")
        self.assertEqual(dag.derive_lifecycle(self.plan_value, self.state())["shared"]["state"], dag.FAILED)

    def test_returned_after_failed_clears_the_stale_failure(self):
        packet = self.claim("shared"); a = packet["attempt_id"]
        self.result("shared", a, {"outcome": "failed", "base_sha": HEAD_A, "reason": "x", "evidence": {}})
        self.result("shared", a, {"outcome": "returned", "base_sha": HEAD_A,
                    "evidence": {"changed_paths": [".engine/tools/shared.py"], "verification_results": ["ok"]}})
        nw = self.state()["work"]["shared"]
        self.assertIsNone(nw["latest_failure"])
        self.assertEqual(dag.derive_lifecycle(self.plan_value, self.state())["shared"]["state"], dag.RETURNED)

    def test_non_object_payload_fails_closed_not_crashes(self):
        # A JSON array (or any non-object) at the top level must refuse, never AttributeError.
        with self.assertRaisesRegex(bc.CoordinatorError, "payload must be a JSON object"):
            work.bind_result({"claim": {"attempt_id": "a", "base_sha": "s"}},
                             {"id": "n", "paths": [], "output_contract": {"required_evidence": []}},
                             "a", "s", ["not", "a", "dict"])

    def test_malformed_evidence_fails_closed_not_crashes(self):
        packet = self.claim("shared")
        with self.assertRaisesRegex(bc.CoordinatorError, "evidence must be an object"):
            self.result("shared", packet["attempt_id"],
                        {"outcome": "failed", "base_sha": HEAD_A, "evidence": ["not-a-dict"]})

    def test_returned_paths_outside_declared_scope_are_rejected(self):
        packet = self.claim("shared")
        with self.assertRaisesRegex(bc.CoordinatorError, "outside the node's declared scope"):
            self.result("shared", packet["attempt_id"],
                        {"outcome": "returned", "base_sha": HEAD_A,
                         "evidence": {"changed_paths": ["etc/passwd"], "verification_results": ["ok"]}})

    def test_null_or_nonstring_evidence_entries_fail_closed_not_crash(self):
        # A worker's JSON may carry null or non-string values where lists of strings belong; every
        # such shape must be refused with a CoordinatorError, never a TypeError or a silent char-split.
        packet = self.claim("shared")
        for evidence in ({"changed_paths": [None], "verification_results": ["ok"]},
                         {"changed_paths": [".engine/tools/shared.py"], "verification_results": [7]},
                         {"changed_paths": [".engine/tools/shared.py"], "verification_results": ["ok"],
                          "assumptions": "no concerns"},
                         {"changed_paths": [".engine/tools/shared.py"], "verification_results": ["ok"],
                          "unresolved_concerns": {"nested": True}}):
            with self.assertRaisesRegex(bc.CoordinatorError, "must be a list of strings"):
                self.result("shared", packet["attempt_id"],
                            {"outcome": "returned", "base_sha": HEAD_A, "evidence": evidence})

    def test_null_on_a_required_evidence_key_is_missing_not_empty(self):
        # Repair-review regression: an explicit null must not satisfy a REQUIRED evidence kind by
        # silently laundering into [] — the contract completeness check treats it as missing.
        packet = self.claim("shared")
        with self.assertRaisesRegex(bc.CoordinatorError, "missing output-contract evidence"):
            self.result("shared", packet["attempt_id"],
                        {"outcome": "returned", "base_sha": HEAD_A,
                         "evidence": {"changed_paths": [".engine/tools/shared.py"],
                                      "verification_results": None}})

    def test_explicit_null_evidence_field_reads_as_empty(self):
        # null for a NON-required key is an ordinary way to say "nothing here" and must not crash.
        packet = self.claim("shared")
        self.result("shared", packet["attempt_id"],
                    {"outcome": "returned", "base_sha": HEAD_A,
                     "evidence": {"changed_paths": [".engine/tools/shared.py"],
                                  "verification_results": ["ok"], "assumptions": None}})
        self.assertEqual(self.state()["work"]["shared"]["latest_result"]["evidence"]["assumptions"], [])

    def test_returned_paths_using_traversal_are_rejected(self):
        # a self-reported changed path that escapes declared scope via ../ must be refused
        packet = self.claim("shared")
        with self.assertRaisesRegex(bc.CoordinatorError, "outside the node's declared scope"):
            self.result("shared", packet["attempt_id"],
                        {"outcome": "returned", "base_sha": HEAD_A,
                         "evidence": {"changed_paths": [".engine/tools/../../../.github/workflows/ci.yml"],
                                      "verification_results": ["ok"]}})


class TestWorkDispositions(WorkCase):
    def _return(self, item):
        packet = self.claim(item)
        self.result(item, packet["attempt_id"],
                    {"outcome": "returned", "base_sha": HEAD_A,
                     "evidence": {"changed_paths": [f".engine/tools/{item}.py"], "verification_results": ["ok"]}})
        return packet["attempt_id"]

    def _reject(self, item, attempt, cls="worker"):
        args = argparse.Namespace(item=item, attempt=attempt, rejection_class=cls, reason="not cohesive")
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_work_reject(args, self.store)

    def _integrate(self, item, attempt, commit=HEAD_A, verification="focused tests pass"):
        args = argparse.Namespace(item=item, attempt=attempt, commit=commit, verification_input=verification)
        with mock.patch.object(bc, "_commit_on_branch", return_value=True), contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_work_integrate(args, self.store)

    def test_reject_releases_resources_and_marks_failed(self):
        attempt = self._return("shared")
        self._reject("shared", attempt)
        nw = self.state()["work"]["shared"]
        self.assertIsNone(nw["claim"])
        self.assertEqual(nw["latest_failure"]["disposition"], "open")

    def test_explicit_retry_reopens_the_node_and_increments_attempt_count(self):
        attempt = self._return("shared")
        self._reject("shared", attempt)
        args = argparse.Namespace(item="shared", strategy="redispatch", reason="try again")
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_work_retry(args, self.store)
        packet2 = self.claim("shared")
        self.assertNotEqual(packet2["attempt_id"], attempt)
        self.assertEqual(self.state()["work"]["shared"]["attempt_count"], 2)

    def test_abandon_releases_resources_and_blocks_the_node(self):
        packet = self.claim("shared")
        args = argparse.Namespace(item="shared", attempt=packet["attempt_id"], reason="give up")
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_work_abandon(args, self.store)
        nw = self.state()["work"]["shared"]
        self.assertIsNone(nw["claim"])
        self.assertEqual(nw["latest_failure"]["disposition"], "abandoned")

    def test_retry_reopens_an_abandoned_node(self):
        # Abandonment is never a permanent dead end: the deliberate fresh start its blocked-state
        # reason promises is exactly `work retry`, after which the node claims again.
        packet = self.claim("shared")
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_work_abandon(argparse.Namespace(item="shared", attempt=packet["attempt_id"],
                                                   reason="give up"), self.store)
        self.assertEqual(dag.derive_lifecycle(self.plan_value, self.state())["shared"]["state"], dag.BLOCKED)
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_work_retry(argparse.Namespace(item="shared", strategy="redispatch",
                                                 reason="fresh approach"), self.store)
        packet2 = self.claim("shared")
        self.assertNotEqual(packet2["attempt_id"], packet["attempt_id"])
        self.assertEqual(self.state()["work"]["shared"]["attempt_count"], 2)

    def test_integrator_inline_retry_routes_the_next_claim_inline(self):
        # The integrator-inline strategy is a real disposition, not a label: the next claim resolves
        # to the inline route (current senior session) instead of re-dispatching the class's worker.
        attempt = self._return("shared")
        self._reject("shared", attempt)
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_work_retry(argparse.Namespace(item="shared", strategy="integrator-inline",
                                                 reason="I'll take it"), self.store)
        packet = self.claim("shared")
        self.assertTrue(packet["route"]["inline"])
        self.assertEqual(packet["route"]["model"], "inherit")
        self.assertEqual(packet["route"]["executor_class"], "builder")  # the class is unchanged; only the route is
        self.assertTrue(self.state()["work"]["shared"]["claim"]["requested_route"]["inline"])

    def test_integrate_records_completion_and_mirrors_into_progress(self):
        attempt = self._return("shared")
        self._integrate("shared", attempt)
        nw = self.state()["work"]["shared"]
        self.assertEqual(nw["integration"]["commit"], HEAD_A)
        self.assertIsNone(nw["claim"])
        self.assertIn({"id": "shared", "commit": HEAD_A}, self.state()["progress"]["completed"])

    def test_no_completion_without_a_returned_result(self):
        packet = self.claim("shared")  # claimed, no result yet
        with self.assertRaisesRegex(bc.CoordinatorError, "no returned result"):
            self._integrate("shared", packet["attempt_id"])

    def test_integration_off_branch_commit_is_refused(self):
        attempt = self._return("shared")
        args = argparse.Namespace(item="shared", attempt=attempt, commit="c" * 40, verification_input="v")
        with mock.patch.object(bc, "_commit_on_branch", return_value=False):
            with self.assertRaisesRegex(bc.CoordinatorError, "not on the PR branch"):
                bc.cmd_work_integrate(args, self.store)

    def test_completed_dependency_unblocks_its_successor(self):
        attempt = self._return("shared")
        self._integrate("shared", attempt)
        lc = dag.derive_lifecycle(self.plan_value, self.state())
        self.assertEqual(lc["shared"]["state"], dag.COMPLETE)
        self.assertEqual(lc["adapter"]["state"], dag.READY)

    def test_reject_names_the_current_attempt_on_mismatch(self):
        attempt = self._return("shared")
        args = argparse.Namespace(item="shared", attempt="f" * 32, rejection_class="worker", reason="x")
        with self.assertRaisesRegex(bc.CoordinatorError, f"current attempt \\({attempt}\\)"):
            bc.cmd_work_reject(args, self.store)

    def test_integrate_requires_a_focused_verification_summary(self):
        attempt = self._return("shared")
        args = argparse.Namespace(item="shared", attempt=attempt, commit=HEAD_A, verification_input="   ")
        with mock.patch.object(bc, "_commit_on_branch", return_value=True):
            with self.assertRaisesRegex(bc.CoordinatorError, "focused-verification"):
                bc.cmd_work_integrate(args, self.store)

    def test_stale_result_after_an_explicit_retry_is_rejected(self):
        old = self._return("shared")
        self._reject("shared", old)
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_work_retry(argparse.Namespace(item="shared", strategy="redispatch", reason="again"), self.store)
        self.claim("shared")  # a fresh attempt supersedes the old one
        with self.assertRaisesRegex(bc.CoordinatorError, "does not match the active claim"):
            self.result("shared", old, {"outcome": "returned", "base_sha": HEAD_A,
                        "evidence": {"changed_paths": [".engine/tools/shared.py"], "verification_results": ["ok"]}})


class TestStatusV2(WorkCase):
    def test_status_exposes_the_work_section(self):
        self.claim("shared")
        result = bc._status(self.state(), self.plan_value)
        self.assertIn("work", result)
        w = result["work"]
        self.assertEqual(w["slots_in_use"], 1)
        self.assertEqual(w["max_concurrency"], 1)
        self.assertEqual(w["nodes"]["shared"]["state"], "claimed")
        self.assertEqual(w["nodes"]["shared"]["route"]["model"], "sonnet")
        self.assertEqual(w["claimable"], [])  # serial slot busy

    def test_status_and_checkpoint_share_one_next_derivation(self):
        state = self.state()
        self.assertEqual(bc._next_incomplete(self.plan_value, state), dag.ready_set(self.plan_value, state)[0])
        self.assertEqual(bc._status(self.plan_value and state, self.plan_value)["progress"]["next"],
                         bc._next_incomplete(self.plan_value, state))

    def test_a_snapshot_without_a_work_map_has_no_work_section(self):
        value = plan_v2()
        state = bc._initial_state("owner/repo", 7, BASE, PLAN_ID, SEALED, value, None)
        state["approval"] = {"plan_digest": bc._digest(value), "spec_digest": None, "depth": "thorough"}
        state["schema_version"] = "build-state.v1"
        state.pop("work")
        result = bc._status(state, value)
        self.assertNotIn("work", result)

    def test_status_surfaces_the_failure_reason(self):
        packet = self.claim("shared")
        path = Path(self.temp.name) / "f.json"
        path.write_text(json.dumps({"outcome": "failed", "base_sha": HEAD_A, "class": "worker",
                                    "reason": "hit a permission error on X", "evidence": {}}))
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_work_result(argparse.Namespace(item="shared", attempt=packet["attempt_id"],
                                                  plan=str(self.plan_path), input=str(path)), self.store)
        node = bc._status(self.state(), self.plan_value)["work"]["nodes"]["shared"]
        self.assertEqual(node["failure"]["reason"], "hit a permission error on X")

    def test_v2_routine_refusal_says_next_ready(self):
        # RSC-4: the v2 Routine refusal names dependency READINESS — the concept the doc and the
        # status render use — while the v1 wording stays byte-identical (pinned elsewhere).
        value = plan_v2()
        value["profile"] = "routine"
        value["intent_source"] = {"kind": "issue", "issue": 11}
        self.write_plan(value)
        state = bc._initial_state("owner/repo", 7, BASE, PLAN_ID, SEALED, value, 11, "unattended")
        state["approval"] = {"plan_digest": state["plan"]["digest"], "spec_digest": None, "depth": "quick"}
        os.remove(self.state_path)
        self.store.create(state)
        note = {"objective": "x", "current_work": "later item", "work_item": "adapter", "assumptions": [],
                "non_goals": [], "planned_scope": [], "remaining_verification": [], "judgment": "aligned"}
        note_path = Path(self.temp.name) / "note.json"
        note_path.write_text(json.dumps(note))
        with mock.patch.object(bc, "_assert_spec_boundary", return_value={}),                 self.assertRaisesRegex(bc.CoordinatorError, "next ready work item shared"):
            bc.cmd_checkpoint(argparse.Namespace(plan=str(self.plan_path), input=str(note_path),
                                                 complete_item="adapter", json=False), self.store)

    def test_human_render_collapses_a_multiline_failure_reason(self):
        # RSC-5a: an untrusted multi-line reason (a pasted trace) must not break the one-line-per-
        # node render; it is collapsed and capped, with the full text still in --json.
        canned = {"phase": "implementation", "head_commit": HEAD_A, "snapshot_revision": 3,
                  "required_evidence": [], "engineering_judgment": [], "warnings": [],
                  "suggested_next": None, "available_activities": [],
                  "progress": {"completed": [], "total": 2, "current": None, "next": "shared"},
                  "work": {"slots_in_use": 0, "max_concurrency": 1, "ready": ["shared"],
                           "claimable": ["shared"], "resource_holders": {},
                           "nodes": {"shared": {"state": "failed", "reasons": [], "attempt_count": 1,
                                     "route": None, "integration_commit": None,
                                     "focused_verification": None, "artifact_digest": None,
                                     "failure": {"class": "worker", "disposition": "open",
                                                 "reason": "Traceback (most recent call last):\n  File x\n" + "x" * 300}}}}}
        with mock.patch.object(bc, "_status", return_value=canned),                 contextlib.redirect_stdout(io.StringIO()) as out:
            bc.cmd_status(argparse.Namespace(plan=None, json=False), self.store)
        lines = [l for l in out.getvalue().splitlines() if "[failure:" in l]
        self.assertEqual(len(lines), 1)  # collapsed to one physical line
        self.assertLess(len(lines[0]), 250)
        self.assertIn("...", lines[0])

    def test_reset_after_revision_clears_the_work_map(self):
        self.claim("shared")
        state = self.state()
        bc._reset_after_revision(state, self.plan_value)
        self.assertEqual(state["work"], {})


class TestMarkerVersioning(unittest.TestCase):
    """One marked block, one generation. The plan block and the v1 handoff marker are gone."""

    def test_the_handoff_block_reads_back_exactly_what_it_wrote(self):
        handoff = {"schema_version": "build-handoff.v2", "x": 1}
        body = "prose\n\n" + ghub.handoff_block(handoff) + "\nmore prose\n"
        found = ghub.find_handoff_block(body)
        self.assertIsNotNone(found)
        self.assertEqual(bc._digest(handoff), found[0])

    def test_two_handoff_blocks_in_one_body_are_refused(self):
        handoff = {"schema_version": "build-handoff.v2", "x": 1}
        body = ghub.handoff_block(handoff) + "\n\n" + ghub.handoff_block(handoff)
        with self.assertRaisesRegex(bc.CoordinatorError, "more than one engine-build-handoff"):
            ghub.find_handoff_block(body)

    def test_a_body_with_no_handoff_block_reads_as_none_rather_than_failing(self):
        self.assertIsNone(ghub.find_handoff_block("prose with no markers at all\n"))


class TestHandoffV2(WorkCase):
    def test_v2_handoff_carries_the_work_projection_and_validates(self):
        self.claim("shared")
        state = self.state()
        state["plan"]["authorizing_issue"] = 11
        value = bc._handoff(state)
        self.assertEqual(value["schema_version"], "build-handoff.v2")
        self.assertIn("shared", value["work"])

    def test_handoff_work_projection_is_bounded_and_redacted(self):
        # The handoff reaches the public PR body: local paths and unreviewed worker free-text are
        # redacted; identifiers, digests, outcomes, and repo-relative changed paths travel.
        packet = self.claim("shared")
        self.result("shared", packet["attempt_id"],
                    {"outcome": "returned", "base_sha": HEAD_A, "artifact_ref": "/Users/someone/bundle.git",
                     "evidence": {"changed_paths": [".engine/tools/shared.py"],
                                  "verification_results": ["ran the suite: 3 passed"],
                                  "assumptions": ["assumed the flag stays default"]}})
        state = self.state()
        state["plan"]["authorizing_issue"] = 11
        value = bc._handoff(state)
        nw = value["work"]["shared"]
        self.assertEqual(nw["claim"]["worktree"], "redacted from durable handoff")
        self.assertEqual(nw["latest_result"]["artifact_ref"], "redacted from durable handoff")
        self.assertEqual(nw["latest_result"]["evidence"]["verification_results"], ["redacted from durable handoff"])
        self.assertEqual(nw["latest_result"]["evidence"]["assumptions"], ["redacted from durable handoff"])
        self.assertEqual(nw["latest_result"]["evidence"]["changed_paths"], [".engine/tools/shared.py"])
        self.assertEqual(nw["claim"]["attempt_id"], packet["attempt_id"])
        # RSC-3: the integrator's free-text verification summary is redacted like every other
        # unreviewed free-text field.
        probe = bc._bounded_work({"n": {"integration": {"attempt_id": "a", "commit": HEAD_A,
                                                        "focused_verification": "token=/Users/x secret"}}})
        self.assertEqual(probe["n"]["integration"]["focused_verification"], "redacted from durable handoff")
        # the LOCAL snapshot keeps its unredacted evidence — only the published projection is bounded
        self.assertEqual(self.state()["work"]["shared"]["claim"]["worktree"], "/tmp/wt")

    def test_restore_keeps_a_returned_result_as_returned(self):
        # A restored claim whose attempt already returned is not uncertain: it derives returned
        # (awaiting integrator inspection), never recovery_required masking complete evidence.
        work_map = {"shared": {"attempt_count": 1, "integration": None, "latest_failure": None,
                               "latest_result": {"attempt_id": "0" * 32, "base_sha": HEAD_A,
                                                 "outcome": "returned", "artifact_ref": None,
                                                 "artifact_digest": None,
                                                 "evidence": {"changed_paths": [], "verification_results": [],
                                                              "assumptions": [], "unresolved_concerns": []}},
                               "claim": {"attempt_id": "0" * 32, "base_sha": HEAD_A, "worktree": "/tmp/wt",
                                         "acquired_resources": [], "restored": False, "worker_ref": None,
                                         "requested_route": {"executor_class": "builder", "provider": "claude",
                                                             "model": "sonnet", "effort": "medium", "inline": False}}}}
        restored = bc._restore_work(work_map)
        self.assertFalse(restored["shared"]["claim"]["restored"])
        lc = dag.derive_lifecycle(self.plan_value, {"work": restored})
        self.assertEqual(lc["shared"]["state"], dag.RETURNED)

    def test_restore_marks_an_unfinished_claim_recovery_required(self):
        work_map = {"shared": {"attempt_count": 1, "latest_result": None, "integration": None,
                               "latest_failure": None,
                               "claim": {"attempt_id": "0" * 32, "base_sha": HEAD_A, "worktree": "/tmp/wt",
                                         "acquired_resources": [], "restored": False, "worker_ref": None,
                                         "requested_route": {"executor_class": "builder", "provider": "claude",
                                                             "model": "sonnet", "effort": "medium", "inline": False}}}}
        restored = bc._restore_work(work_map)
        self.assertTrue(restored["shared"]["claim"]["restored"])
        lc = dag.derive_lifecycle(self.plan_value, {"work": restored})
        self.assertEqual(lc["shared"]["state"], dag.RECOVERY_REQUIRED)


class TestWorkRouting(unittest.TestCase):
    def setUp(self):
        self.bindings = bc._bindings()

    def test_builder_and_bounded_render_per_provider(self):
        self.assertEqual(work.resolve_route(self.bindings, "builder", "claude"),
                         {"executor_class": "builder", "provider": "claude", "model": "sonnet", "effort": "medium", "inline": False})
        self.assertEqual(work.resolve_route(self.bindings, "bounded", "codex"),
                         {"executor_class": "bounded", "provider": "codex", "model": "gpt-5.6-luna", "effort": "low", "inline": False})

    def test_integrator_is_inline_and_inherits(self):
        route = work.resolve_route(self.bindings, "integrator", "claude")
        self.assertTrue(route["inline"])
        self.assertEqual(route["model"], "inherit")

    def test_missing_binding_falls_back_to_integrator_inline_never_stronger(self):
        route = work.resolve_route({"implementation_classes": {}}, "builder", "codex")
        self.assertTrue(route["inline"])
        self.assertEqual(route["model"], "inherit")
        self.assertEqual(route["executor_class"], "builder")


if __name__ == "__main__":
    unittest.main()


class MidBuildRevision(WorkCase):
    """OB-MIDBUILD-REVISION: a Build in flight consumes a SEALED successor without restarting.

    The shape this answers. A seal is terminal, so a plan discovered mid-Build to be wrong cannot be
    edited — the way past a seal is a clone. Until now adopting that clone meant abandoning the Build
    and rebuilding everything the old plan got right, which is a strong incentive to keep building
    against a plan you already believe is flawed.
    """

    SUCCESSOR = "pln_fedcba987654"

    def _successor(self, *, change_adapter=True):
        """A sealed successor: `shared` byte-identical, `adapter` changed (or not)."""
        items = [json.loads(json.dumps(item)) for item in self.plan_value["work_items"]]
        if change_adapter:
            items[1]["description"] = "Build adapter, corrected"
        return plan_v2(items=items)

    def _library(self, *, predecessors=(PLAN_ID,), depth="thorough"):
        record = {"intake": {"predecessors": [f"{p} — a plan" for p in predecessors]},
                  "approval": {"revision": 1, "plan_digest": "sha256:" + "a" * 64,
                               "depth": depth, "at": "2026-08-25T00:00:00Z"}}
        library = mock.MagicMock()
        library.resolve.return_value = "successor-slug"
        library.read_record.return_value = record
        return mock.patch.object(bc, "_library", return_value=library)

    def _adopt(self, successor, **over):
        args = argparse.Namespace(successor=self.SUCCESSOR, input=str(self.plan_path),
                                  operator_decision="Yes, continue on the corrected plan.")
        for key, value in over.items():
            setattr(args, key, value)
        with mock.patch.object(bc, "_sealed_plan",
                               return_value=(self.SUCCESSOR, "sha256:" + "f" * 64, successor)), \
                self._library(), mock.patch.object(bc, "_record_build_binding"), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            bc.cmd_plan_adopt(args, self.store)
        return out.getvalue()

    def _through_integration(self, item):
        claim = self.claim(item)
        self.result(item, claim["attempt_id"], {
            "outcome": "returned", "base_sha": claim["base_sha"],
            "evidence": {"changed_paths": [f".engine/tools/{item}.py"],
                         "verification_results": ["green"]}})
        args = argparse.Namespace(item=item, attempt=claim["attempt_id"], commit=HEAD_A,
                                  verification_input="focused tests pass")
        with mock.patch.object(bc, "_commit_on_branch", return_value=True), \
                contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_work_integrate(args, self.store)

    def test_a_refused_mutate_restores_the_successors_binding_and_consent(self):
        """Adoption touches TWO records, and a `mutate` that refuses after the binding write must
        not leave the successor marked bound — or carrying a consent attestation — for an adoption
        that never happened. This drives the REAL `_record_build_binding` (every other case here
        mocks it, which is how the call site shipped with zero coverage)."""
        successor = self._successor()
        record = {"intake": {"predecessors": [f"{PLAN_ID} — a plan"]},
                  "approval": {"revision": 1, "plan_digest": "sha256:" + "a" * 64,
                               "depth": "thorough", "at": "2026-08-25T00:00:00Z"},
                  "current": {"revision": 1}}
        library = mock.MagicMock()
        library.resolve.return_value = "successor-slug"
        library.read_record.side_effect = lambda slug: record

        def apply_mutator(slug, change, expected_revision=None):
            change(record)
            return record
        library.update_record.side_effect = apply_mutator

        args = argparse.Namespace(successor=self.SUCCESSOR, input=str(self.plan_path),
                                  operator_decision="Yes, continue on the corrected plan.")
        with mock.patch.object(bc, "_sealed_plan",
                               return_value=(self.SUCCESSOR, "sha256:" + "f" * 64, successor)), \
                mock.patch.object(bc, "_library", return_value=library), \
                mock.patch.object(self.store, "mutate",
                                  side_effect=bc.CoordinatorError("revision race")), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(bc.CoordinatorError, "revision race"):
                bc.cmd_plan_adopt(args, self.store)
        self.assertIsNone(record.get("build_binding"),
                          "the successor must not stay marked bound to a Build that never switched")
        self.assertFalse(record.get("consent"),
                         "the trail must not attest an adoption that was refused")

    def test_an_interrupt_during_the_rollback_still_discloses_and_reraises(self):
        """The claim shipped one round on reading alone: the inner handler's breadth (BaseException)
        is what lets a mid-rollback interrupt still print the repair instructions and let the
        original refusal propagate, instead of escaping past both."""
        successor = self._successor()
        record = {"intake": {"predecessors": [f"{PLAN_ID} — a plan"]},
                  "approval": {"revision": 1, "plan_digest": "sha256:" + "a" * 64,
                               "depth": "thorough", "at": "2026-08-25T00:00:00Z"},
                  "current": {"revision": 1}}
        library = mock.MagicMock()
        library.resolve.return_value = "successor-slug"
        library.read_record.side_effect = lambda slug: record
        calls = {"n": 0}

        def update(slug, change, expected_revision=None):
            calls["n"] += 1
            if calls["n"] == 1:      # the binding write lands
                change(record)
                return record
            raise KeyboardInterrupt   # the operator's second Ctrl-C hits the rollback write

        library.update_record.side_effect = update
        args = argparse.Namespace(successor=self.SUCCESSOR, input=str(self.plan_path),
                                  operator_decision="Yes, continue on the corrected plan.")
        err = io.StringIO()
        with mock.patch.object(bc, "_sealed_plan",
                               return_value=(self.SUCCESSOR, "sha256:" + "f" * 64, successor)), \
                mock.patch.object(bc, "_library", return_value=library), \
                mock.patch.object(self.store, "mutate",
                                  side_effect=bc.CoordinatorError("revision race")), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            with self.assertRaisesRegex(bc.CoordinatorError, "revision race"):
                bc.cmd_plan_adopt(args, self.store)
        self.assertIn("could not be restored", err.getvalue())

    def test_adoption_carries_the_settled_specification_forward_with_the_approval(self):
        """A project WITH a settled specification is the case nothing covered, and the case that broke.

        `approve` is what normally resolves the specification and records its fingerprint. Adoption
        inherits the approval without passing through approve, so leaving the fingerprint absent meant
        the next command compared the live specification against nothing, refused with 'settled
        specification changed since approval' — which had not happened — and pointed at a plan revision
        that would have undone the adoption's whole purpose. Invisible here until a specification exists,
        which is exactly why it shipped."""
        successor = self._successor()
        spec = {"digest": "sha256:" + "5" * 64, "posture": "settled"}
        with mock.patch.object(bc, "_canonical_spec", return_value=spec):
            self._adopt(successor)
        state = self.state()
        self.assertEqual(state["approval"]["spec_digest"], spec["digest"])
        self.assertEqual(state["plan"]["spec_digest"], spec["digest"],
                         "the plan's own fingerprint is what _assert_spec_current compares against")
        # And the proof that it is coherent: the very next assertion passes instead of refusing.
        with mock.patch.object(bc, "_canonical_spec", return_value=spec):
            bc._assert_spec_current(state, successor)

    def test_an_unresolvable_specification_refuses_the_adoption_rather_than_half_applying_it(self):
        successor = self._successor()
        before = self.state()["plan"]["digest"]
        with mock.patch.object(bc, "_canonical_spec", side_effect=bc.CoordinatorError("no spec")), \
                self.assertRaises(bc.CoordinatorError):
            self._adopt(successor)
        self.assertEqual(self.state()["plan"]["digest"], before,
                         "the snapshot must be untouched when the adoption refuses")

    # -- the node comparison, which is where the safety lives --

    def test_an_unchanged_node_with_unchanged_ancestry_is_preserved(self):
        successor = self._successor()
        self.assertEqual(bc._unchanged_nodes(self.plan_value, successor), {"shared"})

    def test_a_node_whose_dependency_changed_is_not_preserved_however_identical_it_is(self):
        items = [json.loads(json.dumps(item)) for item in self.plan_value["work_items"]]
        items[0]["description"] = "Build shared, corrected"     # the ROOT moved
        successor = plan_v2(items=items)
        # `adapter` is byte-identical, and it is still dropped: its integration was verified against a
        # predecessor that no longer exists.
        self.assertEqual(bc._unchanged_nodes(self.plan_value, successor), set())

    def test_a_wholly_unchanged_successor_preserves_everything(self):
        self.assertEqual(bc._unchanged_nodes(self.plan_value, plan_v2()), {"shared", "adapter"})

    # -- the verb --

    def test_adoption_preserves_the_binding_and_the_unchanged_node_s_evidence(self):
        self._through_integration("shared")
        self._through_integration("adapter")
        before = self.state()
        self._adopt(self._successor())
        after = self.state()
        self.assertEqual(after["build"], before["build"], "the binding must survive adoption")
        self.assertEqual(after["plan"]["plan_id"], self.SUCCESSOR)
        self.assertFalse(after["plan"]["diverged_from_seal"],
                         "the Build is executing a different SEAL, not diverging from one")
        self.assertEqual(sorted(after["work"]), ["shared"])
        self.assertEqual([e["id"] for e in after["progress"]["completed"]], ["shared"])

    def test_the_changed_node_and_its_dependants_are_reset(self):
        self._through_integration("shared")
        self._through_integration("adapter")
        self._adopt(self._successor())
        self.assertNotIn("adapter", self.state()["work"])

    def test_the_plan_panel_never_re_runs_the_successor_s_own_approval_is_inherited(self):
        self._through_integration("shared")
        self._adopt(self._successor())
        approval = self.state()["approval"]
        self.assertEqual(approval["depth"], "thorough")
        self.assertEqual(approval["plan_digest"], self.state()["plan"]["digest"])

    def test_the_change_is_recorded_for_the_merge_surface(self):
        self._through_integration("shared")
        self._adopt(self._successor())
        escalation = self.state()["plan_change_escalations"][-1]
        self.assertIn(self.SUCCESSOR, escalation["operator_change"])
        self.assertIn("Yes, continue on the corrected plan.", escalation["operator_change"])

    # -- the refusals --

    def test_a_successor_that_does_not_name_the_bound_plan_is_refused(self):
        args = argparse.Namespace(successor=self.SUCCESSOR, input=str(self.plan_path),
                                  operator_decision="Go.")
        with mock.patch.object(bc, "_sealed_plan",
                               return_value=(self.SUCCESSOR, "sha256:" + "f" * 64, self._successor())), \
                self._library(predecessors=("pln_999999999999",)), \
                self.assertRaises(bc.CoordinatorError) as caught:
            bc.cmd_plan_adopt(args, self.store)
        self.assertIn("does not name", str(caught.exception))
        self.assertEqual(self.state()["plan"]["plan_id"], PLAN_ID)

    def test_adoption_without_the_operator_s_decision_is_refused(self):
        args = argparse.Namespace(successor=self.SUCCESSOR, input=str(self.plan_path),
                                  operator_decision=None)
        with self.assertRaises(bc.CoordinatorError) as caught:
            bc.cmd_plan_adopt(args, self.store)
        self.assertIn("no recorded operator decision", str(caught.exception))

    def test_adopting_the_plan_already_bound_is_refused(self):
        args = argparse.Namespace(successor=PLAN_ID, input=str(self.plan_path),
                                  operator_decision="Go.")
        with mock.patch.object(bc, "_sealed_plan",
                               return_value=(PLAN_ID, SEALED, self.plan_value)), \
                self.assertRaises(bc.CoordinatorError) as caught:
            bc.cmd_plan_adopt(args, self.store)
        self.assertIn("already bound to", str(caught.exception))

    def test_an_input_that_is_not_the_executing_plan_is_refused(self):
        other = Path(self.temp.name) / "other.json"
        other.write_text(json.dumps(plan_v2(objective="Something else")), encoding="utf-8")
        args = argparse.Namespace(successor=self.SUCCESSOR, input=str(other),
                                  operator_decision="Go.")
        with mock.patch.object(bc, "_sealed_plan",
                               return_value=(self.SUCCESSOR, "sha256:" + "f" * 64, self._successor())), \
                self._library(), self.assertRaises(bc.CoordinatorError) as caught:
            bc.cmd_plan_adopt(args, self.store)
        self.assertIn("must be the plan this Build is currently executing", str(caught.exception))
