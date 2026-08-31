from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import executor_eligibility as el  # noqa: E402


def _rec(eid, statuses, *, scope="non-production", kind="qualification", recorded="2026-08-31T00:00:00Z"):
    return {
        "record_kind": kind,
        "executor_id": eid,
        "scope": scope,
        "recorded_at": recorded,
        "gates": {gate: {"status": status,
                         "reason_category": "none" if status == "passed" else "authentication"}
                  for gate, status in zip(el.GATES, statuses)},
    }


class TestIsQualified(unittest.TestCase):
    def test_all_gates_passed(self):
        self.assertTrue(el.is_qualified(_rec("a", ["passed", "passed", "passed"])))

    def test_any_gate_not_passed_disqualifies(self):
        for statuses in (["passed", "failed", "passed"],
                         ["passed", "not-run", "passed"],
                         ["passed", "partial", "passed"]):
            self.assertFalse(el.is_qualified(_rec("a", statuses)))

    def test_witness_never_qualifies(self):
        self.assertFalse(el.is_qualified(
            _rec("a", ["passed", "passed", "passed"], kind="fail-closed-witness")))


class TestEligibleSelect(unittest.TestCase):
    def test_empty_is_no_eligible(self):
        self.assertEqual(el.eligible([], production=False), [])
        self.assertIsNone(el.select([], production=False))

    def test_all_unqualified_is_no_eligible(self):
        recs = [_rec("a", ["passed", "failed", "passed"]),
                _rec("b", ["not-run", "not-run", "not-run"])]
        self.assertEqual(el.eligible(recs, production=False), [])
        self.assertIsNone(el.select(recs, production=False))

    def test_nonproduction_excluded_from_production_query(self):
        recs = [_rec("a", ["passed", "passed", "passed"], scope="non-production")]
        self.assertEqual(el.eligible(recs, production=True), [])
        self.assertIsNone(el.select(recs, production=True))
        # ... but eligible in a non-production query
        self.assertEqual(len(el.eligible(recs, production=False)), 1)

    def test_best_qualified_is_deterministic_most_recent(self):
        recs = [_rec("a", ["passed"] * 3, recorded="2026-08-31T00:00:00Z"),
                _rec("b", ["passed"] * 3, recorded="2026-08-31T12:00:00Z")]
        self.assertEqual(el.select(recs, production=False)["executor_id"], "b")


if __name__ == "__main__":
    unittest.main()
