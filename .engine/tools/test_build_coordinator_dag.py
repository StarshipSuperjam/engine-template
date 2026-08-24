#!/usr/bin/env python3
"""Pure-function tests for the DAG derivation and resource-admission service."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_coordinator_dag as dag  # noqa: E402

SHA = "a" * 40
ATTEMPT = "0" * 32


def item(node_id, deps=(), *, paths=None, resources=None):
    return {"id": node_id, "depends_on": list(deps),
            "paths": list(paths) if paths is not None else [f"src/{node_id}.py"],
            "exclusive_resources": list(resources or [])}


def plan(items, mode="serial", max_concurrency=1):
    return {"work_items": items, "parallelism": {"mode": mode, "max_concurrency": max_concurrency}}


def node(*, claim=None, result=None, integration=None, failure=None, attempts=1):
    return {"attempt_count": attempts, "claim": claim, "latest_result": result,
            "integration": integration, "latest_failure": failure}


def claim(resources=(), *, restored=False, attempt=ATTEMPT):
    return {"attempt_id": attempt, "base_sha": SHA, "worktree": "/tmp/wt",
            "acquired_resources": list(resources), "restored": restored,
            "requested_route": {"executor_class": "builder", "provider": "claude",
                                "model": "sonnet", "effort": "medium", "inline": False},
            "worker_ref": None}


def state(work):
    return {"work": work}


class TestValidateDag(unittest.TestCase):
    def test_valid_graph_passes(self):
        dag.validate_dag(plan([item("a"), item("b", ["a"])]))

    def test_cycle_refused(self):
        with self.assertRaisesRegex(dag.CoordinatorError, "cycle"):
            dag.validate_dag(plan([item("a", ["b"]), item("b", ["a"])]))

    def test_unknown_dependency_refused(self):
        with self.assertRaisesRegex(dag.CoordinatorError, "unknown work item ghost"):
            dag.validate_dag(plan([item("a", ["ghost"])]))

    def test_self_dependency_refused(self):
        with self.assertRaisesRegex(dag.CoordinatorError, "depend on itself"):
            dag.validate_dag(plan([item("a", ["a"])]))


class TestLifecycle(unittest.TestCase):
    def test_root_ready_dependent_blocked(self):
        lc = dag.derive_lifecycle(plan([item("a"), item("b", ["a"])]), state({}))
        self.assertEqual(lc["a"]["state"], dag.READY)
        self.assertEqual(lc["b"]["state"], dag.BLOCKED)

    def test_dependent_ready_after_dependency_integrated(self):
        work = {"a": node(integration={"attempt_id": ATTEMPT, "commit": SHA, "focused_verification": "ok"})}
        lc = dag.derive_lifecycle(plan([item("a"), item("b", ["a"])]), state(work))
        self.assertEqual(lc["a"]["state"], dag.COMPLETE)
        self.assertEqual(lc["b"]["state"], dag.READY)

    def test_claimed_returned_failed_recovery(self):
        p = plan([item("a")])
        self.assertEqual(dag.derive_lifecycle(p, state({"a": node(claim=claim())}))["a"]["state"], dag.CLAIMED)
        returned = node(claim=claim(), result={"attempt_id": ATTEMPT, "base_sha": SHA, "outcome": "returned"})
        self.assertEqual(dag.derive_lifecycle(p, state({"a": returned}))["a"]["state"], dag.RETURNED)
        failed = node(claim=claim(), failure={"attempt_id": ATTEMPT, "class": "worker", "reason": "x", "disposition": "open"})
        self.assertEqual(dag.derive_lifecycle(p, state({"a": failed}))["a"]["state"], dag.FAILED)
        recov = node(claim=claim(restored=True))
        self.assertEqual(dag.derive_lifecycle(p, state({"a": recov}))["a"]["state"], dag.RECOVERY_REQUIRED)

    def test_independent_roots_both_ready_without_priority(self):
        rs = dag.ready_set(plan([item("a"), item("b")]), state({}))
        self.assertEqual(rs, ["a", "b"])


class TestResourceAdmission(unittest.TestCase):
    def test_prefix_extraction(self):
        self.assertEqual(dag.resource_prefix(".claude/**"), ".claude/")
        self.assertEqual(dag.resource_prefix(".engine/tools/*.py"), ".engine/tools/")
        self.assertEqual(dag.resource_prefix("foo/bar.py"), "foo/bar.py")
        self.assertIsNone(dag.resource_prefix("*.py"))

    def test_equal_and_ancestor_prefixes_conflict(self):
        self.assertTrue(dag.paths_conflict([".claude/**"], [".claude/agents/x.md"]))
        self.assertTrue(dag.paths_conflict(["a/b.py"], ["a/b.py"]))

    def test_distinct_files_and_prefixes_do_not_conflict(self):
        self.assertFalse(dag.paths_conflict([".engine/tools/a.py"], [".engine/tools/b.py"]))
        self.assertFalse(dag.paths_conflict([".engine/tools/"], [".engine/schemas/"]))
        self.assertFalse(dag.paths_conflict(["foo/bar"], ["foo/barbaz"]))

    def test_metachar_leading_pattern_conflicts_with_everything(self):
        self.assertTrue(dag.paths_conflict(["*.py"], ["totally/unrelated.txt"]))

    def test_glob_mid_component_conflicts_with_a_matching_file(self):
        # a glob that falls INSIDE a filename component must still conflict with a file it matches
        self.assertTrue(dag.paths_conflict(["src/report_*.py"], ["src/report_final.py"]))
        self.assertTrue(dag.paths_conflict(["a*.py"], ["axyz.py"]))
        # but a genuinely non-matching file stays disjoint
        self.assertFalse(dag.paths_conflict(["src/report_*.py"], ["src/summary.py"]))

    def test_path_within_declared(self):
        self.assertTrue(dag.path_within_declared(".claude/agents/x.md", [".claude/**"]))
        self.assertTrue(dag.path_within_declared(".engine/tools/a.py", [".engine/tools/a.py"]))
        self.assertTrue(dag.path_within_declared(".engine/tools/a.py", [".engine/tools/*.py"]))
        self.assertTrue(dag.path_within_declared(".engine/tools/a.py", [".engine/tools/"]))
        self.assertTrue(dag.path_within_declared(".engine/tools/sub/../a.py", [".engine/tools/*.py"]))  # normalizes in-scope
        self.assertFalse(dag.path_within_declared("etc/passwd", [".engine/tools/*.py"]))

    def test_path_within_declared_rejects_traversal_and_absolute(self):
        # the untrusted self-reported path must not escape declared scope via traversal or absoluteness
        self.assertFalse(dag.path_within_declared(".engine/tools/../../../etc/passwd", [".engine/tools/"]))
        self.assertFalse(dag.path_within_declared(".engine/tools/../../../etc/passwd.py", [".engine/tools/*.py"]))
        self.assertFalse(dag.path_within_declared("/etc/passwd", [".engine/tools/*.py"]))
        self.assertFalse(dag.path_within_declared("../secrets/prod.env", [".engine/tools/"]))

    def test_named_resources_conflict(self):
        self.assertTrue(dag.resources_conflict(item("a", paths=["x/a"], resources=["db"]),
                                                item("b", paths=["y/b"], resources=["db"])))

    def test_serial_admits_one_conditional_respects_max(self):
        p_serial = plan([item("a", paths=["a/x"]), item("b", paths=["b/y"])], "serial", 1)
        self.assertEqual(dag.claimable_set(p_serial, state({})), ["a", "b"])
        # once one slot is in use, serial admits none
        busy = state({"a": node(claim=claim())})
        self.assertEqual(dag.claimable_set(p_serial, busy), [])
        p_cond = plan([item("a", paths=["a/x"]), item("b", paths=["b/y"]), item("c", paths=["c/z"])], "conditional", 2)
        self.assertEqual(dag.slots_in_use(p_cond, state({"a": node(claim=claim())})), 1)
        self.assertEqual(dag.claimable_set(p_cond, state({"a": node(claim=claim())})), ["b", "c"])

    def test_resource_conflict_excludes_a_ready_node(self):
        p = plan([item("a", paths=["shared/x"]), item("b", paths=["shared/x"])], "conditional", 2)
        held = state({"a": node(claim=claim(resources=[]))})
        self.assertEqual(dag.claimable_set(p, held), [])  # b conflicts with a's held path

    def test_returned_holds_resources_but_frees_the_slot(self):
        p = plan([item("a", paths=["a/x"]), item("b", paths=["b/y"])], "conditional", 2)
        returned = node(claim=claim(), result={"attempt_id": ATTEMPT, "base_sha": SHA, "outcome": "returned"})
        st = state({"a": returned})
        self.assertEqual(dag.slots_in_use(p, st), 0)  # slot freed
        self.assertIn("a", dag.resource_holders(p, st))  # resources retained
        self.assertEqual(dag.claimable_set(p, st), ["b"])

    def test_failed_node_with_active_claim_still_admits_a_self_retry(self):
        # ARCH-5: a node's own retained resources never exclude it from itself.
        p = plan([item("a", paths=["a/x"])], "serial", 1)
        failed = node(claim=claim(resources=["a/x"]),
                      failure={"attempt_id": ATTEMPT, "class": "worker", "reason": "x", "disposition": "retry"},
                      attempts=1)
        # disposition retry clears the failed state; the node is ready and self-resources don't block it.
        st = state({"a": {**failed, "claim": None}})
        self.assertEqual(dag.claimable_set(p, st), ["a"])



class TestGlobDisjointness(unittest.TestCase):
    """The tightened conflict math: provable disjointness admits concurrency; doubt still conflicts."""

    def test_disjoint_glob_suffixes_do_not_conflict(self):
        # Two globs over one directory with incompatible literal tails can never touch the same file.
        self.assertFalse(dag._pair_conflict("docs/*.md", "docs/*.json"))
        self.assertFalse(dag._pair_conflict("src/a*.py", "src/a*.txt"))

    def test_glob_vs_literal_stays_conservative_over_subtrees(self):
        # A declared literal covers its whole SUBTREE and * crosses /, so a*.txt genuinely reaches
        # axyz.py/notes.txt — compatible prefixes must conflict; only true divergence is disjoint.
        self.assertTrue(dag._pair_conflict("a*.txt", "axyz.py"))
        self.assertTrue(dag._pair_conflict("src/api*.json", "src/apiary/notes.md"))
        self.assertTrue(dag._pair_conflict("a*.py", "axyz.py"))         # the literal matches the glob
        self.assertTrue(dag._pair_conflict("docs/*.md", "docs/readme.md"))
        self.assertFalse(dag._pair_conflict("docs/*.md", "src/readme.md"))  # divergent prefixes

    def test_mid_component_wildcard_against_a_directory_literal_conflicts(self):
        # Repair-review regression (found independently by two lenses): a glob whose wildcard is
        # fused mid-component reaches into a directory literal sharing only a partial-component
        # prefix — src/module*/sub/x.py genuinely collides with src/module_a/sub/x.py.
        self.assertTrue(dag._pair_conflict("src/module*/sub/x.py", "src/module_a/"))
        self.assertTrue(dag._pair_conflict("src/module*/sub/x.py", "src/module_a"))
        self.assertTrue(dag._pair_conflict("a*b/x.py", "axxxb/y.py"))
        self.assertTrue(dag._pair_conflict("foo*bar/x.py", "foo_qux_bar/"))

    def test_mid_component_wildcard_with_later_slash_still_conflicts(self):
        # Repair-review regression: a wildcard fused mid-component can absorb text so a LATER "/"
        # still lands inside the literal's subtree — this pair genuinely collides on
        # docs/api_stable_v2/readme.md and must never be judged disjoint.
        self.assertTrue(dag._pair_conflict("docs/api_*_v2/*.md", "docs/api_stable_v2"))

    def test_containment_still_conflicts(self):
        self.assertTrue(dag._pair_conflict("docs", "docs/*.md"))        # literal dir holds the pattern
        self.assertTrue(dag._pair_conflict("docs/sub", "docs/*.md"))    # a match could live under it
        self.assertTrue(dag._pair_conflict("docs/*.md", "docs/*.md"))   # identical patterns

    def test_compatible_suffix_globs_still_conflict(self):
        self.assertTrue(dag._pair_conflict("a*.py", "ab*.py"))          # a*.py genuinely reaches abz.py
        self.assertTrue(dag._pair_conflict("docs/*", "docs/*.md"))      # a bare-star tail proves nothing

    def test_metachar_leading_pattern_conflicts_with_everything(self):
        self.assertTrue(dag._pair_conflict("*", "docs/readme.md"))


class TestAdmissionRankingAndDeferrals(unittest.TestCase):
    """Ranking is computed from the graph, and every omission carries one of four reasons."""

    def test_critical_path_counts_the_longest_downstream_chain(self):
        # long -> mid -> tail is a chain of three; short unblocks nothing.
        p = plan([item("long"), item("mid", ["long"]), item("tail", ["mid"]), item("short")])
        self.assertEqual(dag.critical_path_lengths(p),
                         {"long": 3, "mid": 2, "tail": 1, "short": 1})

    def test_rank_is_critical_path_descending_then_lexical(self):
        p = plan([item("zeta"), item("alpha"), item("deep"), item("under", ["deep"])])
        # deep unblocks one node, so it outranks the three sinks; the sinks tie and sort by id.
        self.assertEqual(dag.admission_rank(p), ["deep", "alpha", "under", "zeta"])

    def test_array_order_changes_no_scheduling_outcome(self):
        forward = [item("deep"), item("under", ["deep"]), item("alpha"), item("zeta")]
        p_forward, p_reversed = plan(forward), plan(list(reversed(forward)))
        empty = state({})
        self.assertEqual(dag.admission_rank(p_forward), dag.admission_rank(p_reversed))
        self.assertEqual(dag.critical_path_lengths(p_forward), dag.critical_path_lengths(p_reversed))
        self.assertEqual(dag.claimable_set(p_forward, empty), dag.claimable_set(p_reversed, empty))
        self.assertEqual(dag.admission_plan(p_forward, empty), dag.admission_plan(p_reversed, empty))
        self.assertEqual(dag.next_ready(p_forward, empty), dag.next_ready(p_reversed, empty))

    def test_dependency_deferral(self):
        p = plan([item("a"), item("b", ["a"])])
        deferred = dag.admission_plan(p, state({}))["deferred"]
        self.assertEqual([(d["id"], d["kind"]) for d in deferred], [("b", dag.DEFER_DEPENDENCY)])
        self.assertIn("waiting on a", deferred[0]["reason"])

    def test_held_resource_deferral(self):
        p = plan([item("a", resources=["db"]), item("b", resources=["db"])],
                 mode="conditional", max_concurrency=2)
        admission = dag.admission_plan(p, state({"a": node(claim=claim(["db"]))}))
        self.assertEqual(admission["admitted"], [])
        self.assertEqual([(d["id"], d["kind"]) for d in admission["deferred"]],
                         [("b", dag.DEFER_HELD_RESOURCE)])
        self.assertIn("node a holds", admission["deferred"][0]["reason"])

    def test_selected_node_conflict_deferral(self):
        # Two free slots and nothing held: the pass admits the higher-ranked node and defers its
        # same-pass rival on the conflict, rather than pretending both could run at once.
        p = plan([item("a", resources=["db"]), item("b", resources=["db"])],
                 mode="conditional", max_concurrency=2)
        admission = dag.admission_plan(p, state({}))
        self.assertEqual(admission["admitted"], ["a"])
        self.assertEqual([(d["id"], d["kind"]) for d in admission["deferred"]],
                         [("b", dag.DEFER_SELECTED_CONFLICT)])
        self.assertIn("admitted earlier in this pass", admission["deferred"][0]["reason"])

    def test_capacity_deferral(self):
        p = plan([item("a"), item("b")])          # serial: one slot for two independent roots
        admission = dag.admission_plan(p, state({}))
        self.assertEqual(admission["admitted"], ["a"])
        self.assertEqual([(d["id"], d["kind"]) for d in admission["deferred"]],
                         [("b", dag.DEFER_CAPACITY)])

    def test_capacity_reason_distinguishes_real_occupancy_from_pass_exhaustion(self):
        # With no slot actually occupied, saying "all slots are in use" contradicts the slot count
        # status prints directly above the deferral line. The two situations get different sentences.
        p = plan([item("a"), item("b")])
        empty = state({})
        self.assertEqual(dag.slots_in_use(p, empty), 0)
        pass_exhausted = dag.admission_plan(p, empty)["deferred"][0]["reason"]
        self.assertIn("this pass filled the last", pass_exhausted)
        self.assertIn("a", pass_exhausted)                      # names who took the slot
        self.assertNotIn("are in use", pass_exhausted)
        busy = state({"a": node(claim=claim())})
        really_busy = dag.admission_plan(p, busy)["deferred"][0]["reason"]
        self.assertEqual(dag.slots_in_use(p, busy), 1)
        self.assertIn("all 1 worker slot(s) are in use", really_busy)

    def test_a_capacity_deferred_node_is_still_claimable(self):
        # Eligibility is not selection: the scheduler would advance "a", but a direct claim on "b"
        # stays permitted while a slot is free. Ranking orders the frontier; it never seizes the
        # orchestrator's choice of what to work on.
        p = plan([item("a"), item("b")])
        self.assertEqual(dag.claimable_set(p, state({})), ["a", "b"])

    def test_returned_but_unintegrated_frees_the_slot_and_keeps_the_resources(self):
        p = plan([item("a", resources=["db"]), item("b", resources=["db"]), item("c")],
                 mode="conditional", max_concurrency=2)
        returned = {"a": node(claim=claim(["db"]),
                              result={"attempt_id": ATTEMPT, "outcome": "returned"})}
        # The slot is free (a returned node occupies no worker), so c is admitted...
        self.assertEqual(dag.slots_in_use(p, state(returned)), 0)
        admission = dag.admission_plan(p, state(returned))
        self.assertIn("c", admission["admitted"])
        # ...while a's resources stay reserved, so b is still held off.
        self.assertEqual([(d["id"], d["kind"]) for d in admission["deferred"]],
                         [("b", dag.DEFER_HELD_RESOURCE)])

    def test_integration_releases_successors_in_critical_path_order(self):
        p = plan([item("root"), item("deep", ["root"]), item("under", ["deep"]), item("flat", ["root"])])
        integrated = {"root": node(integration={"attempt_id": ATTEMPT, "commit": SHA})}
        # Both successors become ready; the one carrying the longer tail is advanced first.
        self.assertEqual(dag.ready_set(p, state(integrated)), ["deep", "flat"])
        self.assertEqual(dag.next_ready(p, state(integrated)), "deep")
        self.assertEqual(dag.admission_plan(p, state(integrated))["admitted"], ["deep"])

    def test_a_deep_chain_does_not_exhaust_the_python_stack(self):
        # critical_path_lengths walks a topological order rather than recursing, so a chain far longer
        # than the interpreter's recursion limit answers instead of raising RecursionError.
        depth = 2000
        items = [item("n0")] + [item(f"n{i}", [f"n{i - 1}"]) for i in range(1, depth)]
        lengths = dag.critical_path_lengths(plan(items))
        self.assertEqual(lengths["n0"], depth)
        self.assertEqual(lengths[f"n{depth - 1}"], 1)

    def test_next_ready_ignores_capacity_and_held_resources(self):
        # "Next" answers which item to advance, so a busy slot must not change it.
        p = plan([item("a"), item("b"), item("c")])
        busy = {"c": node(claim=claim())}
        self.assertEqual(dag.next_ready(p, state({})), "a")
        self.assertEqual(dag.next_ready(p, state(busy)), "a")


class RoutineShapeRule(unittest.TestCase):
    """The schema rule the unattended correspondence check leans on.

    A routine plan's intent must have come from an Issue. That used to be the authority itself — the
    durable Issue CARRIED the plan, so an Issue-derived intent was how an unattended Build proved it
    held the right one, and bind compared digests. It is a SHAPE rule now, and the real work is done
    by build_coordinator's correspondence check. What it still guarantees is that there IS a reference
    for that check to compare against: without it a routine plan could be sealed naming no Issue, and
    every unattended bind of it would then have nothing to check and would have to either refuse
    always or trust anything.
    """

    @staticmethod
    def _plan(intent_source, profile=None):
        from test_build_coordinator import plan as build_plan
        value = build_plan()
        value["intent_source"] = dict(intent_source)
        if profile:
            value["profile"] = profile
        return value

    @staticmethod
    def _schemas():
        import build_coordinator as bc
        return bc.PLAN_SCHEMAS

    def test_a_routine_plan_whose_intent_is_direct_is_refused(self):
        with self.assertRaises(dag.CoordinatorError):
            dag.validate_plan_document(self._plan({"kind": "direct"}, "routine"), self._schemas())

    def test_a_routine_plan_that_names_its_issue_validates(self):
        value = self._plan({"kind": "issue", "issue": 770}, "routine")
        self.assertEqual(dag.validate_plan_document(value, self._schemas()), "build-plan.v2")

    def test_the_rule_binds_only_the_routine_profile(self):
        # A normal plan may be authored from an Issue or from a direct instruction — the interactive
        # operator is the authorization there, so nothing about its shape is forced.
        for intent in ({"kind": "direct"}, {"kind": "issue", "issue": 770}):
            self.assertEqual(dag.validate_plan_document(self._plan(intent), self._schemas()),
                             "build-plan.v2")


if __name__ == "__main__":
    unittest.main()
