#!/usr/bin/env python3
"""Self-tests for the attention ranking function (slice 12): the pure deterministic core (attention_rank),
its versioned output contract (attention-result.v1.json), and the policy it reads (.engine/policies/attention.md).

Run: uv run --directory .engine -- python -m unittest discover -s tools -p 'test_*.py'

These lock the FORM, not the calibration (D-052/D-113 — the values are uncalibrated starting values, so
ranking *quality* is deliberately NOT asserted here):
  - partition ASSIGNMENT — a candidate lands in the category its source labels, and the ONE membership call
    attention owns (open debt is blocking iff severity reaches the policy bar) fires correctly; an unknown
    label is refused (the source must speak the locked vocabulary);
  - the HEADLINE structural guarantee — a low-weight blocking-debt item leads a high-weight feature WITH
    precedence, the feature floats up when precedence is removed, and the debt returns when it is restored:
    precedence is structural, never weight-driven;
  - DETERMINISM — same inputs yield byte-identical output; input order does not matter; ties break on id; the
    absolute (non-range-normalized) scoring yields no NaN even for a single-member or all-equal partition;
  - the budget FLEX — a clean session widens orientation, a high-debt session compresses it, fractions still
    sum to one (attention owns the flex, D-062/D-063);
  - DEGRADE — the result records absent substrates and never narrates; every category is present even empty;
  - the result CONFORMS to attention-result.v1 and the schema has teeth (this is the ONLY well-formedness lock
    on that schema — no live rule targets .engine/schemas/*.json);
  - the committed policy carries EXACTLY the value keys the tool reads (a drift guard), with precedence/trim a
    permutation of 1..5 and the budget fractions summing to one, and conforms to policy.v1.
"""
from __future__ import annotations
import copy
import json
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402
import attention_rank    # noqa: E402
from attention_rank import (rank, assign_partition, intra_weight, session_condition, apply_flex,  # noqa: E402
                            budget_split, CATEGORIES, SUBSTRATES, EXPECTED_VALUE_KEYS)

RESULT_SCHEMA = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "attention-result.v1.json"))
POLICY_SCHEMA = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "policy.v1.json"))
POLICY_PATH = os.path.join(validate.ENGINE_DIR, "policies", "attention.md")
AS_OF = "2026-06-04T00:00:00Z"

# A self-contained fixture policy so the FORM tests do not depend on the real (tunable) numbers: precedence
# in canonical order, trim its reverse, budgets summing to one, a high debt-blocking bar of 2.
FIXTURE_POLICY = {
    **{f"budget_{c}": v for c, v in zip(CATEGORIES, [0.30, 0.25, 0.15, 0.15, 0.15])},
    **{f"precedence_{c}": i + 1 for i, c in enumerate(CATEGORIES)},
    **{f"trim_{c}": i + 1 for i, c in enumerate(reversed(CATEGORIES))},
    "weight_recency": 0.5, "weight_severity": 1.0, "weight_proximity": 0.5,
    "flex_high_debt_count": 3, "flex_orientation_delta": 0.10,
    "debt_blocking_threshold": 2, "scent_strong_match_threshold": 0.5,
}

# The headline pair: a blocking-debt item that is OLD and just-at-the-bar (low weight) and an in-flight
# feature that is CURRENT and close (high weight).
DEBT = {"id": "debt:overdue", "category": "blocking_debt", "severity": 2,
        "recency": "2026-04-01T00:00:00Z", "source": "telemetry"}
FEATURE = {"id": "feat:shiny", "category": "in_flight", "proximity": 1.0,
           "recency": "2026-06-04T00:00:00Z", "source": "git"}


def _errors(schema, instance):
    return list(validate.Draft202012Validator(schema).iter_errors(instance))


def _flatten(result):
    """Member ids across the whole partition in result order (partition array order, then member order)."""
    return [m["id"] for entry in result["partition"] for m in entry["members"]]


class TestAssignment(unittest.TestCase):
    def test_each_candidate_lands_in_its_category(self):
        cases = [
            ({"id": "d", "category": "blocking_debt", "severity": 3}, "blocking_debt"),
            ({"id": "b", "category": "in_flight"}, "in_flight"),
            ({"id": "p", "category": "recent_decisions"}, "recent_decisions"),
            ({"id": "n", "category": "structural_neighbors"}, "structural_neighbors"),
            ({"id": "o", "category": "orientation"}, "orientation"),
        ]
        for cand, expected in cases:
            self.assertEqual(assign_partition(cand, FIXTURE_POLICY), expected)

    def test_open_debt_below_threshold_is_not_blocking(self):
        # severity 1 is below the bar of 2 -> a deferral, not surfaced as blocking (returns None)
        self.assertIsNone(assign_partition({"id": "d", "category": "blocking_debt", "severity": 1}, FIXTURE_POLICY))
        # missing severity is likewise not provably blocking
        self.assertIsNone(assign_partition({"id": "d", "category": "blocking_debt"}, FIXTURE_POLICY))

    def test_open_debt_at_or_above_threshold_is_blocking(self):
        self.assertEqual(assign_partition({"id": "d", "category": "blocking_debt", "severity": 2}, FIXTURE_POLICY),
                         "blocking_debt")
        self.assertEqual(assign_partition({"id": "d", "category": "blocking_debt", "severity": 9}, FIXTURE_POLICY),
                         "blocking_debt")

    def test_unknown_category_label_raises(self):
        with self.assertRaises(ValueError):
            assign_partition({"id": "x", "category": "backlog"}, FIXTURE_POLICY)

    def test_non_finite_severity_is_not_blocking(self):
        # a malformed (NaN/Infinity) severity is not a PROVABLE severity -> not surfaced as blocking
        for bad in (float("nan"), float("inf")):
            self.assertIsNone(assign_partition({"id": "d", "category": "blocking_debt", "severity": bad},
                                               FIXTURE_POLICY))


class TestPrecedenceIsStructural(unittest.TestCase):
    """The slice's whole point: precedence is structural, not a weight anything can out-tune."""

    def test_blocking_debt_outranks_higher_weighted_feature(self):
        self.assertGreater(intra_weight(FEATURE, FIXTURE_POLICY, AS_OF),
                           intra_weight(DEBT, FIXTURE_POLICY, AS_OF),
                           "the fixture must give the feature the HIGHER weight for the test to be meaningful")
        flat = _flatten(rank([FEATURE, DEBT], FIXTURE_POLICY, AS_OF, set()))
        self.assertLess(flat.index("debt:overdue"), flat.index("feat:shiny"),
                        "with precedence, blocking debt leads despite its lower weight")

    def test_remove_precedence_lets_the_feature_float_up(self):
        result = rank([FEATURE, DEBT], FIXTURE_POLICY, AS_OF, set(), apply_precedence=False)
        flat = _flatten(result)
        self.assertLess(flat.index("feat:shiny"), flat.index("debt:overdue"),
                        "with precedence removed, the higher-weighted feature floats above the debt")
        self.assertTrue(all(e["precedence_rank"] == 1 for e in result["partition"]),
                        "precedence-removed is the visible diagnostic state: every category at rank 1")

    def test_restore_precedence_returns_debt_to_the_top(self):
        flat = _flatten(rank([FEATURE, DEBT], FIXTURE_POLICY, AS_OF, set(), apply_precedence=True))
        self.assertLess(flat.index("debt:overdue"), flat.index("feat:shiny"))


class TestDeterminism(unittest.TestCase):
    def test_same_inputs_same_output(self):
        a = rank([DEBT, FEATURE], FIXTURE_POLICY, AS_OF, {"state"}, budget_total=20)
        b = rank([DEBT, FEATURE], FIXTURE_POLICY, AS_OF, {"state"}, budget_total=20)
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))

    def test_input_order_does_not_matter(self):
        a = rank([DEBT, FEATURE], FIXTURE_POLICY, AS_OF, set())
        b = rank([FEATURE, DEBT], FIXTURE_POLICY, AS_OF, set())
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))

    def test_ties_break_on_id(self):
        # two identical-weight in-flight items (no signals) -> ordered by id ascending
        x = {"id": "zzz", "category": "in_flight"}
        y = {"id": "aaa", "category": "in_flight"}
        members = rank([x, y], FIXTURE_POLICY, AS_OF, set())["partition"]
        in_flight = next(e for e in members if e["category"] == "in_flight")
        self.assertEqual([m["id"] for m in in_flight["members"]], ["aaa", "zzz"])

    def test_single_member_and_all_equal_partitions_yield_no_nan(self):
        # the absolute (non-range-normalized) scoring cannot divide by a zero range; assert a finite,
        # JSON-strict result (json with allow_nan=False raises on a NaN/Infinity) for these edge fixtures.
        same_ts = [{"id": f"n{i}", "category": "structural_neighbors", "recency": AS_OF, "proximity": 0.5}
                   for i in range(3)]
        lone = [{"id": "solo", "category": "orientation"}]
        for fixture in (same_ts, lone, []):
            result = rank(fixture, FIXTURE_POLICY, AS_OF, set())
            json.dumps(result, allow_nan=False)  # raises if any NaN/Infinity leaked into the result

    def test_as_of_is_echoed_never_a_fresh_clock_read(self):
        result = rank([FEATURE], FIXTURE_POLICY, AS_OF, set())
        self.assertEqual(result["as_of"], AS_OF)

    def test_non_finite_signal_is_coerced_not_leaked(self):
        # a malformed NaN/Infinity signal must never poison the math or leak a non-JSON NaN into the result;
        # it is coerced to the weakest position (the degrade posture), and the result is strict-valid JSON.
        for bad in (float("nan"), float("inf"), float("-inf")):
            result = rank([{"id": "n", "category": "structural_neighbors", "proximity": bad, "recency": None}],
                          FIXTURE_POLICY, AS_OF, set())
            json.dumps(result, allow_nan=False)  # raises if any NaN/Infinity leaked
            members = [m for e in result["partition"] for m in e["members"]]
            self.assertTrue(all(math.isfinite(m["signals"]["proximity"]) for m in members if "signals" in m))


class TestFlex(unittest.TestCase):
    def _orientation_fraction(self, result):
        return next(e["budget_fraction"] for e in result["partition"] if e["category"] == "orientation")

    def test_clean_session_widens_orientation(self):
        result = rank([FEATURE], FIXTURE_POLICY, AS_OF, set())  # no blocking debt -> clean
        self.assertEqual(result["session_condition"], "clean")
        self.assertGreater(self._orientation_fraction(result), FIXTURE_POLICY["budget_orientation"])

    def test_high_debt_session_compresses_orientation(self):
        debts = [{"id": f"d{i}", "category": "blocking_debt", "severity": 3} for i in range(3)]  # >= flex count
        result = rank(debts, FIXTURE_POLICY, AS_OF, set())
        self.assertEqual(result["session_condition"], "high_debt")
        self.assertLess(self._orientation_fraction(result), FIXTURE_POLICY["budget_orientation"])

    def test_fractions_still_sum_to_one(self):
        for fixture in ([FEATURE], [{"id": f"d{i}", "category": "blocking_debt", "severity": 3} for i in range(4)]):
            result = rank(fixture, FIXTURE_POLICY, AS_OF, set())
            self.assertAlmostEqual(sum(e["budget_fraction"] for e in result["partition"]), 1.0, places=5)

    def test_apply_flex_helper_renormalizes(self):
        base = {c: FIXTURE_POLICY[f"budget_{c}"] for c in CATEGORIES}
        for condition in ("clean", "high_debt"):
            flexed = apply_flex(base, condition, FIXTURE_POLICY)
            self.assertAlmostEqual(sum(flexed.values()), 1.0, places=9)


class TestDegrade(unittest.TestCase):
    def test_absent_substrates_are_recorded_sorted(self):
        result = rank([FEATURE], FIXTURE_POLICY, AS_OF, {"state"})
        self.assertEqual(result["degraded_inputs"], ["git", "knowledge", "telemetry"])

    def test_telemetry_and_git_are_degraded_when_only_state_knowledge_present(self):
        result = rank([], FIXTURE_POLICY, AS_OF, {"state", "knowledge"})
        self.assertIn("telemetry", result["degraded_inputs"])
        self.assertIn("git", result["degraded_inputs"])

    def test_every_category_is_present_even_when_empty(self):
        result = rank([], FIXTURE_POLICY, AS_OF, set())
        self.assertEqual([e["category"] for e in result["partition"]], list(CATEGORIES))
        self.assertTrue(all(e["members"] == [] for e in result["partition"]))

    def test_result_carries_no_narration_field(self):
        result = rank([FEATURE], FIXTURE_POLICY, AS_OF, set())
        for forbidden in ("message", "warning", "narration", "note"):
            self.assertNotIn(forbidden, result)


class TestResultSchema(unittest.TestCase):
    def test_result_schema_is_well_formed(self):
        # the ONLY well-formedness lock on attention-result.v1 — no live rule targets .engine/schemas/*.json
        validate.Draft202012Validator.check_schema(RESULT_SCHEMA)

    def test_rank_output_conforms(self):
        fixtures = [
            rank([DEBT, FEATURE], FIXTURE_POLICY, AS_OF, {"state"}, budget_total=20),   # headline + sizing
            rank([], FIXTURE_POLICY, AS_OF, set()),                                     # all-empty
            rank([{"id": f"d{i}", "category": "blocking_debt", "severity": 3} for i in range(3)],
                 FIXTURE_POLICY, AS_OF, {"state"}),                                     # flexed high-debt
        ]
        for result in fixtures:
            self.assertEqual(_errors(RESULT_SCHEMA, result), [])

    def test_signals_slot_is_carried_when_present(self):
        result = rank([DEBT, FEATURE], FIXTURE_POLICY, AS_OF, set())
        members = [m for e in result["partition"] for m in e["members"]]
        self.assertTrue(any("signals" in m for m in members), "the optional per-member signals slot is populated")
        self.assertEqual(_errors(RESULT_SCHEMA, result), [])

    def test_schema_has_teeth(self):
        good = rank([FEATURE], FIXTURE_POLICY, AS_OF, set())
        # drop a required top-level field
        bad = copy.deepcopy(good); del bad["degraded_inputs"]
        self.assertNotEqual(_errors(RESULT_SCHEMA, bad), [])
        # a partition that is not exactly five categories
        bad = copy.deepcopy(good); bad["partition"] = bad["partition"][:4]
        self.assertNotEqual(_errors(RESULT_SCHEMA, bad), [])
        # an out-of-vocabulary category
        bad = copy.deepcopy(good); bad["partition"][0]["category"] = "backlog"
        self.assertNotEqual(_errors(RESULT_SCHEMA, bad), [])
        # a malformed as_of
        bad = copy.deepcopy(good); bad["as_of"] = "June 4"
        self.assertNotEqual(_errors(RESULT_SCHEMA, bad), [])
        # a degraded input outside the closed substrate vocabulary
        bad = copy.deepcopy(good); bad["degraded_inputs"] = ["memory"]
        self.assertNotEqual(_errors(RESULT_SCHEMA, bad), [])


class TestPolicyValues(unittest.TestCase):
    """Drift guard: the committed policy carries EXACTLY the value keys the tool reads, structurally well-formed."""

    def setUp(self):
        self.values = validate.frontmatter(POLICY_PATH).get("values", {})

    def test_policy_carries_exactly_the_expected_value_keys(self):
        self.assertEqual(set(self.values.keys()), set(EXPECTED_VALUE_KEYS))

    def test_precedence_ranks_are_a_permutation_of_one_to_five(self):
        self.assertEqual(sorted(self.values[k] for k in attention_rank.PRECEDENCE_KEYS), [1, 2, 3, 4, 5])

    def test_trim_ranks_are_a_permutation_of_one_to_five(self):
        self.assertEqual(sorted(self.values[k] for k in attention_rank.TRIM_KEYS), [1, 2, 3, 4, 5])

    def test_budget_fractions_sum_to_one(self):
        self.assertAlmostEqual(sum(self.values[k] for k in attention_rank.BUDGET_KEYS), 1.0, places=6)

    def test_policy_frontmatter_conforms_to_policy_v1(self):
        self.assertEqual(_errors(POLICY_SCHEMA, validate.frontmatter(POLICY_PATH)), [])


class TestReferenceTime(unittest.TestCase):
    def test_more_recent_ranks_first_within_a_partition(self):
        recent = {"id": "recent", "category": "in_flight", "recency": "2026-06-04T00:00:00Z"}
        old = {"id": "old", "category": "in_flight", "recency": "2026-01-01T00:00:00Z"}
        result = rank([old, recent], FIXTURE_POLICY, AS_OF, set())
        in_flight = next(e for e in result["partition"] if e["category"] == "in_flight")
        self.assertEqual([m["id"] for m in in_flight["members"]], ["recent", "old"])

    def test_as_of_is_an_explicit_reproducible_input(self):
        a = rank([FEATURE], FIXTURE_POLICY, "2026-06-04T00:00:00Z", set())
        b = rank([FEATURE], FIXTURE_POLICY, "2026-06-04T00:00:00Z", set())
        self.assertEqual(a, b)
        self.assertEqual(a["as_of"], "2026-06-04T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
