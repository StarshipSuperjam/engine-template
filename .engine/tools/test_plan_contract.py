#!/usr/bin/env python3
"""Tests for plan_contract — the engine-plan.v1 contract.

The behaviour worth locking is not "the schema works". It is the DELEGATION: that the payload half of
a plan is judged by the Build Coordinator's own validator and not by a second opinion living here. So
the central tests below take payloads the Build Coordinator refuses at bind — one per each of its
three layers — and prove this contract refuses them too, with the SAME error text, because it is
literally the same code. If someone later re-expresses those rules here, these tests keep passing but
the drift they exist to prevent begins; the message-identity assertions are there to make that
re-expression visibly awkward rather than quietly tempting.
"""
from __future__ import annotations

import copy
import json
import unittest

import build_coordinator_dag as dag
import plan_contract


def _payload() -> dict:
    """A minimal build-plan.v2 the Build Coordinator accepts: two nodes, one edge, no cycle."""
    return {
        "schema_version": "build-plan.v2",
        "profile": "normal",
        "intent_source": {"kind": "direct"},
        "raw_intent": "Prove the contract delegates.",
        "interpretation": "Two nodes and one edge, enough to exercise every validation layer.",
        "objective": "A payload that binds.",
        "success_obligations": [{"outcome": "It binds.", "verification": "plan bind accepts it."}],
        "evidence": [{"claim": "The Build Coordinator validates in three layers.",
                      "basis": "build_coordinator_dag.validate_plan_document", "kind": "observed"}],
        "assumptions": [],
        "scope_boundary": ["the two nodes below"],
        "non_goals": ["anything outside this fixture"],
        "risks": ["a fixture that drifts from the real schema"],
        "review_strategy": "The tests in this module are the review.",
        "parallelism": {"mode": "serial", "max_concurrency": 1},
        "spec": {"posture": "none", "selection_basis": "No specification governs a test fixture.",
                 "disclosure": "This payload exists only to exercise the validator."},
        "work_items": [
            {"id": "first", "description": "The root node.", "paths": ["a.py"],
             "depends_on": [], "exclusive_resources": [], "executor_class": "integrator",
             "verification": ["it runs"],
             "output_contract": {"deliverable": "a.py exists", "artifact_kinds": ["code"],
                                 "required_evidence": ["a green test"]}},
            {"id": "second", "description": "The dependent node.", "paths": ["b.py"],
             "depends_on": ["first"], "exclusive_resources": [], "executor_class": "integrator",
             "verification": ["it runs"],
             "output_contract": {"deliverable": "b.py exists", "artifact_kinds": ["code"],
                                 "required_evidence": ["a green test"]}},
        ],
    }


def _document(**over) -> dict:
    doc = {
        "schema_version": "engine-plan.v1",
        "plan_id": "pln_0123456789ab",
        "title": "A plan for the tests",
        "revision": 1,
        "created_at": "2026-08-23T00:00:00Z",
        "revised_at": "2026-08-23T00:00:00Z",
        "revision_note": "First revision.",
        "intent": {"raw": "make it delegate", "interpretation": "Prove payload authority is not duplicated.",
                   "source": {"kind": "direct"}},
        "deliberation": {
            "problem_frame": "Two coordinators could disagree about a valid payload.",
            "case_against": "One more schema is one more thing to keep true.",
            "alternatives": [{"option": "Re-express the payload rules here", "disposition": "rejected",
                              "reason": "Two notions of validity drift, and the drift only shows at bind."}],
            "failure_modes": ["A plan seals and then fails at bind."],
            "unresolved_decisions": [],
        },
        "build_plan": _payload(),
    }
    doc.update(over)
    return doc


class DelegatesPayloadAuthority(unittest.TestCase):
    """Each of the Build Coordinator's three layers, refused here by the same code that refuses it there."""

    def _refusal_from_both(self, payload):
        """Return (plan-side message, build-side message) for the same bad payload."""
        with self.assertRaises(plan_contract.PlanContractError) as plan_side:
            plan_contract.validate_document(_document(build_plan=payload))
        with self.assertRaises(dag.CoordinatorError) as build_side:
            dag.validate_plan_document(payload, plan_contract.BUILD_PLAN_SCHEMAS)
        return str(plan_side.exception), str(build_side.exception)

    def test_layer_one_schema_refusal_is_the_build_coordinators_own(self):
        payload = _payload()
        del payload["objective"]          # a required build-plan.v2 field
        plan_side, build_side = self._refusal_from_both(payload)
        self.assertEqual(plan_side, build_side)
        self.assertIn("build-plan.v2", plan_side)

    def test_layer_two_duplicate_work_item_id(self):
        # Uniqueness over a derived key is NOT expressible in JSON Schema, so this layer exists only
        # in code — which is exactly why a second implementation would be so easy to omit.
        payload = _payload()
        payload["work_items"][1]["id"] = "first"
        payload["work_items"][1]["depends_on"] = []
        plan_side, build_side = self._refusal_from_both(payload)
        self.assertEqual(plan_side, build_side)
        self.assertIn("work-item ids must be unique", plan_side)

    def test_layer_three_dag_closure_and_cycles(self):
        dangling = _payload()
        dangling["work_items"][1]["depends_on"] = ["nonexistent"]
        plan_side, build_side = self._refusal_from_both(dangling)
        self.assertEqual(plan_side, build_side)
        self.assertIn("depends on unknown work item", plan_side)

        cyclic = _payload()
        cyclic["work_items"][0]["depends_on"] = ["second"]
        plan_side, build_side = self._refusal_from_both(cyclic)
        self.assertEqual(plan_side, build_side)
        self.assertIn("cycle", plan_side)

    def test_a_valid_payload_passes_both_and_reports_its_version(self):
        self.assertEqual(plan_contract.validate_document(_document()), "build-plan.v2")


class StructuralDefects(unittest.TestCase):
    def test_missing_deliberation_is_refused(self):
        doc = _document()
        del doc["deliberation"]
        with self.assertRaises(plan_contract.PlanContractError):
            plan_contract.validate_document(doc)

    def test_absent_build_plan_is_refused(self):
        doc = _document()
        del doc["build_plan"]
        with self.assertRaises(plan_contract.PlanContractError):
            plan_contract.validate_document(doc)

    def test_a_hollow_deliberation_is_refused(self):
        # An empty case_against is the shape a plan takes when nobody actually deliberated.
        doc = _document()
        doc["deliberation"]["case_against"] = ""
        with self.assertRaises(plan_contract.PlanContractError):
            plan_contract.validate_document(doc)

    def test_absent_schema_version_says_so_rather_than_assuming_v1(self):
        doc = _document()
        del doc["schema_version"]
        with self.assertRaisesRegex(plan_contract.PlanContractError, "does not state a schema_version"):
            plan_contract.validate_document(doc)

    def test_an_unknown_document_version_is_refused(self):
        with self.assertRaisesRegex(plan_contract.PlanContractError, "unrecognized plan document version"):
            plan_contract.validate_document(_document(schema_version="engine-plan.v9"))

    def test_a_malformed_plan_id_is_refused(self):
        with self.assertRaises(plan_contract.PlanContractError):
            plan_contract.validate_document(_document(plan_id="plan-coordinator"))

    def test_unknown_top_level_keys_are_refused(self):
        # additionalProperties:false, so a typo'd or smuggled key cannot ride along unnoticed.
        with self.assertRaises(plan_contract.PlanContractError):
            plan_contract.validate_document(_document(deliberaton={"oops": True}))


class SealBlockers(unittest.TestCase):
    def test_an_unresolved_decision_blocks_the_seal_but_not_the_draft(self):
        doc = _document()
        doc["deliberation"]["unresolved_decisions"] = ["Who owns the retention policy?"]
        # Still a perfectly valid DRAFT — that is the point of recording the question.
        self.assertEqual(plan_contract.validate_document(doc), "build-plan.v2")
        blockers = plan_contract.seal_blockers(doc)
        self.assertTrue(any("unresolved" in b for b in blockers), blockers)
        self.assertTrue(any("Who owns the retention policy?" in b for b in blockers), blockers)

    def test_an_unresolved_assumption_blocks_the_seal(self):
        doc = _document()
        doc["build_plan"]["assumptions"] = [{"claim": "The disk is durable.", "status": "unresolved"}]
        blockers = plan_contract.seal_blockers(doc)
        self.assertTrue(any("The disk is durable." in b for b in blockers), blockers)

    def test_verified_and_accepted_risk_assumptions_do_not_block(self):
        doc = _document()
        doc["build_plan"]["assumptions"] = [{"claim": "Proven.", "status": "verified"},
                                            {"claim": "Known and accepted.", "status": "accepted-risk"}]
        self.assertEqual(plan_contract.seal_blockers(doc), [])

    def test_a_v1_payload_reads_but_cannot_seal(self):
        # A legacy payload must never be unreadable — an imported old plan should open, not explode —
        # but a seal hands the Build Coordinator a graph, and v1 has none.
        v1 = {k: v for k, v in _payload().items() if k != "parallelism"}
        v1["schema_version"] = "build-plan.v1"
        v1["work_items"] = [{k: v for k, v in item.items()
                             if k in ("id", "description", "paths", "verification")}
                            for item in v1["work_items"]]
        doc = _document(build_plan=v1)
        self.assertEqual(plan_contract.validate_document(doc), "build-plan.v1")
        blockers = plan_contract.seal_blockers(doc)
        self.assertTrue(any("only build-plan.v2 can be sealed" in b for b in blockers), blockers)

    def test_a_clean_plan_has_no_blockers(self):
        self.assertEqual(plan_contract.seal_blockers(_document()), [])

    def test_blockers_are_reported_together_not_one_at_a_time(self):
        doc = _document()
        doc["deliberation"]["unresolved_decisions"] = ["An open question."]
        doc["build_plan"]["assumptions"] = [{"claim": "An open assumption.", "status": "unresolved"}]
        self.assertEqual(len(plan_contract.seal_blockers(doc)), 2)

    def test_an_invalid_document_reports_that_rather_than_a_crash(self):
        doc = _document()
        del doc["build_plan"]
        blockers = plan_contract.seal_blockers(doc)
        self.assertEqual(len(blockers), 1)
        self.assertIn("does not validate", blockers[0])


class DigestStability(unittest.TestCase):
    def test_digest_is_stable_across_key_order(self):
        doc = _document()
        shuffled = json.loads(json.dumps(doc))
        shuffled = {k: shuffled[k] for k in reversed(list(shuffled))}
        self.assertEqual(plan_contract.document_digest(doc), plan_contract.document_digest(shuffled))

    def test_digest_is_stable_across_re_serialization(self):
        doc = _document()
        round_tripped = json.loads(json.dumps(doc, indent=4, sort_keys=True))
        self.assertEqual(plan_contract.document_digest(doc), plan_contract.document_digest(round_tripped))

    def test_digest_changes_when_content_changes(self):
        doc = _document()
        other = copy.deepcopy(doc)
        other["deliberation"]["problem_frame"] += "."
        self.assertNotEqual(plan_contract.document_digest(doc), plan_contract.document_digest(other))

    def test_build_plan_digest_is_over_the_payload_alone(self):
        doc = _document()
        # The Build side computes this over the payload it receives, knowing nothing of the wrapper —
        # so a change confined to the deliberation half must NOT move it.
        before = plan_contract.build_plan_digest(doc)
        doc["revision_note"] = "A different note."
        doc["deliberation"]["failure_modes"].append("Another way it could go wrong.")
        self.assertEqual(plan_contract.build_plan_digest(doc), before)
        self.assertEqual(before, plan_contract.digest(doc["build_plan"]))

    def test_unicode_survives_canonicalization(self):
        doc = _document()
        doc["title"] = "Plan — “quoted”, naïve, 🚂"
        again = json.loads(json.dumps(doc))
        self.assertEqual(plan_contract.document_digest(doc), plan_contract.document_digest(again))


class LoadFromDisk(unittest.TestCase):
    def test_invalid_json_is_named_as_such(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "revision.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            with self.assertRaisesRegex(plan_contract.PlanContractError, "not valid JSON"):
                plan_contract.load_document(path)

    def test_a_valid_document_round_trips_through_disk(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "revision.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(_document(), handle)
            loaded = plan_contract.load_document(path)
            self.assertEqual(plan_contract.document_digest(loaded),
                             plan_contract.document_digest(_document()))


if __name__ == "__main__":
    unittest.main()
