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

if __name__ == "__main__":
    unittest.main()
