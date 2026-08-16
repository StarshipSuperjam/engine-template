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
from test_build_coordinator import plan as plan_v1, plan_v2, _work_item_v2, HEAD_A, BASE  # noqa: E402
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
        with self.assertRaisesRegex(bc.CoordinatorError, "not claimable now: it is blocked"):
            self.claim("adapter")  # blocked on shared

    def test_packet_preview_reports_claimability(self):
        args = argparse.Namespace(item="adapter", provider="claude", plan=str(self.plan_path), worktree="/tmp/wt")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            bc.cmd_work_packet(args, self.store)
        preview = json.loads(out.getvalue())["preview"]
        self.assertFalse(preview["claimable_now"])
        self.assertIn("blocked", preview["refusal_reason"])

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

    def test_work_verbs_on_a_v1_build_name_the_actual_cause(self):
        # A v1 snapshot has no work map: the verbs without a --plan argument refuse with the same
        # actionable message their packet/claim/result siblings give, not "no recorded work".
        v1 = plan_v1()
        state = bc._initial_state("owner/repo", 7, BASE, "session", v1, None)
        os.remove(self.state_path)
        self.store.create(state)
        with self.assertRaisesRegex(bc.CoordinatorError, "require a build-plan.v2 Build"):
            bc.cmd_work_reject(argparse.Namespace(item="W1", attempt="0" * 32,
                                                  rejection_class="worker", reason="x"), self.store)

    def test_each_work_verb_guards_with_compare_and_swap(self):
        self.claim("shared")  # revision advances to 2
        stale = bc.StateStore(self.state_path, expected_revision=1)
        args = argparse.Namespace(item="shared", attempt="0" * 32, worker_ref="x")
        with self.assertRaisesRegex(bc.CoordinatorError, "reload status"):
            bc.cmd_work_attach(args, stale)


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

    def test_v1_status_has_no_work_section(self):
        v1 = plan_v1()
        state = bc._initial_state("owner/repo", 7, BASE, "session", v1, None)
        state["approval"] = {"plan_digest": bc._digest(v1), "spec_digest": None, "depth": "thorough"}
        result = bc._status(state, v1)
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
        state = bc._initial_state("owner/repo", 7, BASE, "issue", value, 11, "unattended")
        state["approval"] = {"plan_digest": state["plan"]["digest"], "spec_digest": None, "depth": "quick"}
        state["reviews"]["plan"].update({"packet_digest": "sha256:" + "1" * 64,
                                         "referent_digest": "sha256:" + "2" * 64})
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
    def test_v2_plan_block_reads_back_and_does_not_cross_match_v1(self):
        v2 = plan_v2()
        body = "prose\n\n" + ghub.plan_block(v2) + "\nmore prose\n"
        got = ghub.durable_plan(body, plan_schema=bc.PLAN_SCHEMAS)
        self.assertEqual(bc._digest(got), bc._digest(v2))
        with self.assertRaisesRegex(bc.CoordinatorError, "no unique"):
            ghub.durable_plan(body, plan_schema=bc.PLAN_SCHEMA)  # a legacy v1-only reader sees no block

    def test_v1_and_v2_plan_blocks_together_are_rejected(self):
        body = ghub.plan_block(plan_v1()) + "\n\n" + ghub.plan_block(plan_v2())
        with self.assertRaisesRegex(bc.CoordinatorError, "no unique"):
            ghub.durable_plan(body, plan_schema=bc.PLAN_SCHEMAS)

    def test_handoff_markers_do_not_cross_match(self):
        v2_handoff = {"schema_version": "build-handoff.v2", "x": 1}
        body = ghub.handoff_block(v2_handoff)
        self.assertIsNotNone(ghub.find_handoff_block(body, "v2"))
        self.assertIsNone(ghub.find_handoff_block(body, "v1"))


class TestHandoffV2(WorkCase):
    def test_v2_handoff_carries_the_work_projection_and_validates(self):
        self.claim("shared")
        state = self.state()
        state["plan"]["source"] = "issue"
        state["plan"]["durable_issue"] = 11
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
        state["plan"]["source"] = "issue"
        state["plan"]["durable_issue"] = 11
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
