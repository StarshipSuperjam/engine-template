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
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402
import attention_rank    # noqa: E402
import attention         # noqa: E402
from attention_rank import (rank, assign_partition, intra_weight, session_condition, apply_flex,  # noqa: E402
                            budget_split, CATEGORIES, SUBSTRATES, EXPECTED_VALUE_KEYS,
                            PRECEDENCE_KEYS, TRIM_KEYS)

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


ATTENTION_STRUCTURAL = set(PRECEDENCE_KEYS) | set(TRIM_KEYS)


class TestEffectiveValues(unittest.TestCase):
    """Issue #42 — the operator policy-override read-time merge (D-167). Exercises the REAL core merge
    (validate.effective_policy_values) and the REAL consumer (attention.load_policy_values). The merge is
    static-data only, so determinism is preserved and the structural ordering cannot be out-tuned. No live
    rule exists this slice (slice 26 wires the stale-key rule on the committed override file); these fixtures
    plus the operator demo are the merge's only proof. The eligibility partition: every attention value is
    override-eligible EXCEPT the structural precedence/trim keys (Shane's flex-is-tunable leaf — flex merges)."""

    def _merge(self, override, *, structural=ATTENTION_STRUCTURAL):
        return validate.effective_policy_values(FIXTURE_POLICY, override, structural_keys=structural,
                                                tier="soft", message="")

    def test_sparse_override_merges_per_key(self):
        effective, findings = self._merge({"budget_orientation": 0.40, "weight_recency": 0.9})
        self.assertEqual(findings, [])
        self.assertEqual(effective["budget_orientation"], 0.40)
        self.assertEqual(effective["weight_recency"], 0.9)
        for k, v in FIXTURE_POLICY.items():           # every unnamed key keeps the shipped default
            if k not in ("budget_orientation", "weight_recency"):
                self.assertEqual(effective[k], v)

    def test_partial_override_leaves_unset_keys_at_default(self):
        effective, findings = self._merge({"weight_severity": 2.0})
        self.assertEqual(findings, [])
        self.assertEqual(effective["weight_severity"], 2.0)
        self.assertEqual({k: effective[k] for k in FIXTURE_POLICY if k != "weight_severity"},
                         {k: v for k, v in FIXTURE_POLICY.items() if k != "weight_severity"})

    def test_empty_override_returns_default_unchanged(self):
        effective, findings = self._merge({})
        self.assertEqual(effective, FIXTURE_POLICY)
        self.assertEqual(findings, [])

    def test_stale_key_falls_back_and_is_surfaced(self):
        effective, findings = self._merge({"budget_legacy_thing": 0.1})
        self.assertNotIn("budget_legacy_thing", effective)   # falls back: never enters the effective map
        self.assertEqual(effective, FIXTURE_POLICY)          # everything else is the default
        self.assertEqual(len(findings), 1)
        self.assertIn("budget_legacy_thing", findings[0]["message"])

    def test_ineligible_precedence_key_refused_at_the_merge_layer(self):
        # the law guard asserted at the MERGE layer (not only via the downstream rank): a structural key is
        # refused and the shipped default value stands in the effective map.
        effective, findings = self._merge({"precedence_blocking_debt": 5})
        self.assertEqual(effective["precedence_blocking_debt"], FIXTURE_POLICY["precedence_blocking_debt"])
        self.assertEqual(len(findings), 1)
        self.assertIn("precedence_blocking_debt", findings[0]["message"])

    def test_ineligible_trim_key_refused(self):
        effective, findings = self._merge({"trim_orientation": 9})
        self.assertEqual(effective["trim_orientation"], FIXTURE_POLICY["trim_orientation"])
        self.assertEqual(len(findings), 1)

    def test_flex_keys_are_eligible_and_merge(self):
        # Shane's leaf: the two flex dials ARE tunable (only precedence/trim are structural).
        effective, findings = self._merge({"flex_orientation_delta": 0.25, "flex_high_debt_count": 7})
        self.assertEqual(findings, [])
        self.assertEqual(effective["flex_orientation_delta"], 0.25)
        self.assertEqual(effective["flex_high_debt_count"], 7)

    def test_eligibility_partition_over_every_key(self):
        # comprehensive: every non-structural key merges with no finding; every structural key is refused.
        for k in FIXTURE_POLICY:
            effective, findings = self._merge({k: 99})
            if k in ATTENTION_STRUCTURAL:
                self.assertEqual(effective[k], FIXTURE_POLICY[k], f"{k} must be refused (structural)")
                self.assertEqual(len(findings), 1, f"{k} must surface a finding")
            else:
                self.assertEqual(effective[k], 99, f"{k} must merge (eligible)")
                self.assertEqual(findings, [], f"{k} must merge cleanly")

    def test_determinism_same_inputs_byte_identical(self):
        override = {"budget_orientation": 0.4, "trim_orientation": 9, "stale": 1}
        self.assertEqual(json.dumps(self._merge(override), sort_keys=True),
                         json.dumps(self._merge(override), sort_keys=True))
        # order-independent: a differently-ordered override yields the identical effective map AND the
        # identical finding order — the merge iterates `sorted(override)`, so dict insertion order never leaks.
        eff_a, find_a = self._merge(override)
        eff_b, find_b = self._merge({"stale": 1, "trim_orientation": 9, "budget_orientation": 0.4})
        self.assertEqual(eff_a, eff_b)
        self.assertEqual([f["message"] for f in find_a], [f["message"] for f in find_b])

    def test_findings_are_finding_v1_shape(self):
        _effective, findings = self._merge({"precedence_blocking_debt": 5, "stale_key": 1})
        self.assertEqual(len(findings), 2)
        for f in findings:
            self.assertEqual(set(f.keys()), {"severity", "message", "location"})
            self.assertEqual(f["severity"], "soft")

    def test_precedence_override_cannot_reorder_the_partition(self):
        # end-to-end law guard: an override that tries to demote blocking debt below in-flight is refused, so
        # rank() over the same fixture STILL leads with blocking debt — structural, not out-tunable.
        effective, findings = self._merge({"precedence_blocking_debt": 5, "precedence_in_flight": 1})
        self.assertEqual(len(findings), 2)   # both structural keys refused
        flat = _flatten(rank([FEATURE, DEBT], effective, AS_OF, set()))
        self.assertLess(flat.index("debt:overdue"), flat.index("feat:shiny"))

    def test_load_policy_values_no_override_matches_shipped_default(self):
        # the regression lock: the live path (no override) returns the committed default bit-for-bit.
        shipped = validate.frontmatter(POLICY_PATH).get("values", {})
        self.assertEqual(attention.load_policy_values(), shipped)
        self.assertEqual(attention.load_policy_values(POLICY_PATH, None), shipped)

    def test_load_policy_values_with_override_returns_effective(self):
        shipped = validate.frontmatter(POLICY_PATH).get("values", {})
        effective = attention.load_policy_values(POLICY_PATH, {"budget_orientation": 0.40})
        self.assertEqual(effective["budget_orientation"], 0.40)
        for k, v in shipped.items():
            if k != "budget_orientation":
                self.assertEqual(effective[k], v)
        # a structural override through the consumer is still refused (the default stands)
        effective2 = attention.load_policy_values(POLICY_PATH, {"trim_blocking_debt": 1})
        self.assertEqual(effective2["trim_blocking_debt"], shipped["trim_blocking_debt"])

    def test_rank_live_threads_override_to_the_merge(self):
        # The live seam (slice 26c): boot reads the operator override and hands attention's slice to
        # rank_live, which must forward it to the per-key merge — so a tuned value reaches the live ranking.
        captured = {}
        original = attention.load_policy_values

        def spy(policy_path=POLICY_PATH, override=None):
            captured["override"] = override
            return original(policy_path, override)

        attention.load_policy_values = spy
        try:
            attention.rank_live(override={"budget_orientation": 0.40})
        finally:
            attention.load_policy_values = original
        self.assertEqual(captured["override"], {"budget_orientation": 0.40},
                         "rank_live forwards the operator override to the per-key merge")


class TestDeriveFocus(unittest.TestCase):
    """derive_focus maps the in-flight changed files to their owning graph entities — the work-in-hand focus
    the orientation-time focused read keys on (#37). The git runner and the graph are injected/mocked."""

    def _patch(self, paths, entities):
        return (mock.patch.object(attention.work_record, "changed_paths", return_value=paths),
                mock.patch.object(attention.knowledge_query, "find", return_value=entities))

    def test_maps_changed_files_to_owning_entities_exactly(self):
        entities = [{"source_path": ".engine/tools/attention.py", "id": "tool:attention"},
                    {"source_path": ".engine/tools/boot.py", "id": "tool:boot"}]
        p1, p2 = self._patch([".engine/tools/attention.py", ".engine/tools/boot.py"], entities)
        with p1, p2:
            self.assertEqual(attention.derive_focus(run=lambda a: None), ["tool:attention", "tool:boot"])

    def test_skips_paths_that_own_no_entity(self):
        # a non-surface file (root README) owns no entity -> silently skipped, never guessed
        entities = [{"source_path": ".engine/tools/attention.py", "id": "tool:attention"}]
        p1, p2 = self._patch(["README.md", ".engine/tools/attention.py"], entities)
        with p1, p2:
            self.assertEqual(attention.derive_focus(run=lambda a: None), ["tool:attention"])

    def test_excludes_test_and_demo_entities(self):
        entities = [{"source_path": ".engine/tools/attention.py", "id": "tool:attention"},
                    {"source_path": ".engine/tools/test_attention.py", "id": "tool:test_attention"},
                    {"source_path": ".engine/tools/demo_x.py", "id": "tool:demo_x"}]
        paths = [e["source_path"] for e in entities]
        p1, p2 = self._patch(paths, entities)
        with p1, p2:
            self.assertEqual(attention.derive_focus(run=lambda a: None), ["tool:attention"])

    def test_distinct_and_capped_in_stable_order(self):
        entities = [{"source_path": f".engine/tools/t{i}.py", "id": f"tool:t{i}"} for i in range(10)]
        p1, p2 = self._patch([e["source_path"] for e in entities], entities)
        with p1, p2:
            self.assertEqual(attention.derive_focus(run=lambda a: None, cap=3),
                             ["tool:t0", "tool:t1", "tool:t2"])

    def test_no_changed_paths_is_empty(self):
        p1, p2 = self._patch([], [{"source_path": "x", "id": "tool:x"}])
        with p1, p2:
            self.assertEqual(attention.derive_focus(run=lambda a: None), [])

    def test_find_failure_degrades_to_empty(self):
        with mock.patch.object(attention.work_record, "changed_paths",
                               return_value=[".engine/tools/attention.py"]), \
             mock.patch.object(attention.knowledge_query, "find",
                               side_effect=Exception("knowledge unavailable")):
            self.assertEqual(attention.derive_focus(run=lambda a: None), [])

    def test_lazy_run_default_never_crashes_when_work_record_absent(self):
        # the run default is LAZY (run=None), so calling derive_focus() with the guarded work_record import
        # degraded to None returns [] cleanly — never an AttributeError at call (or import) time.
        with mock.patch.object(attention, "work_record", None):
            self.assertEqual(attention.derive_focus(), [])


class TestFocusSetWalk(unittest.TestCase):
    """assemble_candidates walks a focus SET: dedupes neighbors across members and excludes focus members
    (co-changed entities are not each other's structural neighbors). The graph + work record are mocked."""

    def test_set_focus_dedupes_neighbors_and_excludes_focus_members(self):
        def fake_neighbors(fid, edge_filter=None, depth=1):
            return {"tool:a": [{"id": "tool:b"}, {"id": "mod:x"}],   # tool:b is itself a focus member
                    "tool:b": [{"id": "tool:a"}, {"id": "mod:x"}]}.get(fid, [])
        with mock.patch.object(attention.work_record, "read_in_flight", return_value=[]), \
                mock.patch.object(attention.knowledge_query, "neighbors", side_effect=fake_neighbors):
            cands, available, _ = attention.assemble_candidates(
                FIXTURE_POLICY, state_path="/nonexistent", focus=["tool:a", "tool:b"], gh=None)
        sn = [c["id"] for c in cands if c["category"] == "structural_neighbors"]
        self.assertEqual(sn, ["mod:x"])             # mod:x once; tool:a / tool:b excluded (focus members)
        self.assertIn("knowledge", available)

    def test_single_string_focus_still_walks(self):
        with mock.patch.object(attention.work_record, "read_in_flight", return_value=[]), \
                mock.patch.object(attention.knowledge_query, "neighbors", return_value=[{"id": "mod:x"}]):
            cands, available, _ = attention.assemble_candidates(
                FIXTURE_POLICY, state_path="/nonexistent", focus="tool:a", gh=None)
        sn = [c["id"] for c in cands if c["category"] == "structural_neighbors"]
        self.assertEqual(sn, ["mod:x"])
        self.assertIn("knowledge", available)


if __name__ == "__main__":
    unittest.main()
