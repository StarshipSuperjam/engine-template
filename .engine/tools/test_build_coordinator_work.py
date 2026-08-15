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
from test_build_coordinator import plan_v2, _work_item_v2, HEAD_A, BASE  # noqa: E402


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
        state = bc._initial_state("owner/repo", 7, BASE, "session", self.plan_value, None)
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
        state = bc._initial_state("owner/repo", 7, BASE, "session", self.plan_value, None)
        state["approval"] = {"plan_digest": bc._digest(self.plan_value), "spec_digest": None, "depth": "thorough"}
        os.remove(self.state_path)
        self.store.create(state)
        self.claim("a")
        with self.assertRaisesRegex(bc.CoordinatorError, "not claimable"):
            self.claim("b")

    def test_result_binds_to_attempt_and_rejects_a_stale_attempt(self):
        packet = self.claim("shared")
        attempt = packet["attempt_id"]
        evidence = {"changed_paths": ["x"], "verification_results": ["ok"]}
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

    def test_attach_records_the_worker_reference(self):
        packet = self.claim("shared")
        args = argparse.Namespace(item="shared", attempt=packet["attempt_id"], worker_ref="task-123")
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_work_attach(args, self.store)
        self.assertEqual(self.state()["work"]["shared"]["claim"]["worker_ref"], "task-123")

    def test_each_work_verb_guards_with_compare_and_swap(self):
        self.claim("shared")  # revision advances to 2
        stale = bc.StateStore(self.state_path, expected_revision=1)
        args = argparse.Namespace(item="shared", attempt="0" * 32, worker_ref="x")
        with self.assertRaisesRegex(bc.CoordinatorError, "reload status"):
            bc.cmd_work_attach(args, stale)


class TestWorkDispositions(WorkCase):
    def _return(self, item):
        packet = self.claim(item)
        self.result(item, packet["attempt_id"],
                    {"outcome": "returned", "base_sha": HEAD_A,
                     "evidence": {"changed_paths": ["x"], "verification_results": ["ok"]}})
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
        import build_coordinator_dag as dag
        lc = dag.derive_lifecycle(self.plan_value, self.state())
        self.assertEqual(lc["shared"]["state"], dag.COMPLETE)
        self.assertEqual(lc["adapter"]["state"], dag.READY)


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
