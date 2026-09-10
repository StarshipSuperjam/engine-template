"""Real canonical ingress witnesses, including the deliberately invalid inputs it must reject."""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import quiet_call

from jsonschema import Draft202012Validator
import result_contracts as rc


class ResultContracts(unittest.TestCase):
    def test_disposable_demo_succeeds_and_deliberate_bypass_fails(self):
        import contextlib
        import demo_result_contracts
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(quiet_call.run(demo_result_contracts.main, []), 0)
            self.assertEqual(quiet_call.run(demo_result_contracts.main, ["--break-ingress"]), 1)

    def setUp(self):
        self.review = rc.resolve("plan-review-finding.v1", role="plan-review")
        self.finding = {"severity": "blocking", "message": "A real failure", "location": None}

    def ingest(self, value):
        return rc.ingest(json.dumps(value), self.review)

    def refusal(self, raw, rule=None):
        with self.assertRaises(rc.Rejection) as caught:
            rc.ingest(raw, self.review)
        envelope = caught.exception.envelope
        schema = json.loads((rc.ROOT / ".engine/schemas/result-rejection.v1.json").read_text())
        Draft202012Validator(schema).validate(envelope)
        if rule:
            self.assertEqual(envelope["rule"], rule)
        return envelope

    def test_empty_is_complete_but_null_and_control_messages_are_not(self):
        self.assertEqual(self.ingest([]), [])
        for value in [None, {}, {"status": "needs_clarification"}, "[]", 7]:
            self.refusal(json.dumps(value), "type")

    def test_complete_closed_raw_report_before_compilation(self):
        self.assertEqual(self.ingest([self.finding]), [self.finding])
        for key in ["id", "lens", "disposition", "rationale", "blocks_this_pr", "surprise"]:
            self.refusal(json.dumps([{**self.finding, key: "cannot disappear"}]), "additionalProperties")
        self.refusal(json.dumps([{**self.finding, "location": {"file": "a", "unknown": 1}}]))
        self.refusal(json.dumps([self.finding, {"summary": "wrong shape"}]))

    def test_strict_json(self):
        for raw in [b"\xff", "[", '{"x":1,"x":2}', "NaN", "1e999", "Infinity", '"\\ud800"']:
            self.refusal(raw)

    def test_observed_output_cannot_be_replaced_or_reordered(self):
        observed = json.dumps([self.finding, {**self.finding, "message": "second"}])
        for replacement in [[], [self.finding], list(reversed(json.loads(observed)))]:
            with self.assertRaises(rc.Rejection) as caught:
                rc.require_observed_report(self.ingest(replacement), self.ingest(json.loads(observed)))
            self.assertEqual(caught.exception.envelope["rule"], "observed_report_mismatch")
        self.assertIsNone(rc.require_observed_report(self.ingest(json.loads(observed)),
                                                     self.ingest(json.loads(observed))))

    def test_no_false_authority_from_prose_or_changed_binding(self):
        for contract in [None, [], {}, "unknown"]:
            with self.assertRaises(rc.Rejection):
                rc.resolve(contract)
        for contract in ["grounding-brief.v1", "validation-digest.v1", "audit-finding.v1"]:
            bound = rc.resolve(contract)
            self.assertIsNone(bound["schema"])
            with self.assertRaises(rc.Rejection):
                rc.ingest("[]", bound)
        for field in ["schema_digest", "enforcement", "schema", "limits"]:
            bad = copy.deepcopy(self.review); bad[field] = None
            with self.assertRaises(rc.Rejection):
                rc.ingest("[]", bad)
        with self.assertRaises(rc.Rejection):
            rc.resolve("plan-review-finding.v1", role="worker")

    def test_binding_comparison_preserves_json_types(self):
        bad = copy.deepcopy(self.review)
        self.assertIs(bad["schema"]["items"]["additionalProperties"], False)
        bad["schema"]["items"]["additionalProperties"] = 0
        self.assertEqual(bad, self.review)  # Python equality masks this schema tampering.
        with self.assertRaises(rc.Rejection) as caught:
            rc.ingest("[]", bad)
        self.assertEqual(caught.exception.envelope["rule"], "changed_binding")

    def test_bounded_bytes_depth_values_arrays_and_strings(self):
        self.refusal(" " * (rc.LIMITS["bytes"] + 1), "maxBytes")
        self.refusal("[" * 65 + "]" * 65, "maxDepth")
        self.refusal("[" + ",".join(["0"] * 10001) + "]", "maxValues")
        self.refusal(json.dumps([self.finding] * 1001), "maxItems")
        self.ingest([{**self.finding, "message": "a" * 65536}])
        self.refusal(json.dumps([{**self.finding, "message": "a" * 65537}]), "maxStringBytes")
        stream = io.BytesIO(b" " * (rc.LIMITS["bytes"] + 200))
        self.assertEqual(len(rc.read_input("-", stream=stream)), rc.LIMITS["bytes"] + 1)
        self.assertEqual(stream.tell(), rc.LIMITS["bytes"] + 1)

    def test_safe_error_does_not_echo_bad_value(self):
        secret = "secret-canary-never-echo"
        error = self.refusal(json.dumps([{**self.finding, "severity": secret}]))
        self.assertNotIn(secret, json.dumps(error))
        self.assertEqual(error["path"], "/0/severity")
        for value in [{secret: [0] * 1001}, {secret: "x" * 65537},
                      {"location": {secret: [0] * 1001}}]:
            error = self.refusal(json.dumps(value))
            self.assertNotIn(secret, json.dumps(error))

    def test_lossless_review_compiler_retains_location_semantics(self):
        for loc in [None, {"file": "a:b"}, {"file": "a", "line": None}, {"file": "a", "line": 3}]:
            original = self.ingest([{**self.finding, "location": loc}])
            compiled = rc.compile_review(original, lens="architecture")
            self.assertEqual(compiled["report"], original)
            self.assertEqual(compiled["findings"][0]["id"], "A-1")

    def test_worker_failure_evidence_survives_canonical_conversion(self):
        bound = rc.resolve("worker-result.v1", role="worker")
        value = {"outcome": "failed", "reason": "Cannot complete", "evidence": {
            "changed_paths": [], "verification_results": [
                {"command": "focused", "outcome": "passed", "detail": "3 passed"},
                {"command": "full", "outcome": "failed", "detail": "13 failures, 3 errors", "exit_code": 1}],
            "assumptions": [], "unresolved_concerns": ["The full suite is red"]}}
        report = rc.ingest(json.dumps(value), bound)
        self.assertEqual(rc.compile_worker(report), value)
        for key in ["attempt_id", "base_sha", "artifact_digest", "receipt"]:
            with self.assertRaises(rc.Rejection):
                rc.ingest(json.dumps({**value, key: "forged"}), bound)

    def test_schema_closure_changes_and_unsafe_references_refuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / ".engine/schemas"; directory.mkdir(parents=True)
            schema = directory / "plan-review-finding.v1.json"
            schema.write_text('{"$ref":"leaf.json"}')
            leaf = directory / "leaf.json"; leaf.write_text('{"type":"string"}')
            first = rc.resolve("plan-review-finding.v1", root=tmp)
            leaf.write_text('{"type":"integer"}')
            second = rc.resolve("plan-review-finding.v1", root=tmp)
            self.assertNotEqual(first["schema_digest"], second["schema_digest"])
            for ref in [None, [], "plan-review-finding.v1.json", "https://example.org/schema", "../../outside.json", "missing.json"]:
                leaf.write_text(json.dumps({"$ref": ref}))
                with self.assertRaises(rc.Rejection):
                    rc.resolve("plan-review-finding.v1", root=tmp)


if __name__ == "__main__":
    unittest.main()
