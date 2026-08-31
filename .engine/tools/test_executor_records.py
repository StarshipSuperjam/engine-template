from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import executor_records as er  # noqa: E402
import validate  # noqa: E402


def _good_qual(**over):
    rec = {
        "schema_version": "executor-qualification.v1",
        "record_kind": "qualification",
        "executor_id": "claude-agent-acp",
        "recorded_at": "2026-08-31T00:00:00Z",
        "transport": "acp/v1",
        "engine_revision": "abc123",
        "scope": "non-production",
        "bridge_identity": {"name": "@agentclientprotocol/claude-agent-acp", "version": "0.1.0",
                            "digest": "sha256:" + "0" * 64},
        "vendored_agent_identity": {"name": "@anthropic-ai/claude-agent-sdk", "version": "0.1.0",
                                    "digest": "sha256:" + "1" * 64},
        "gates": {
            "protocol_conformance": {"status": "passed", "reason_category": "none"},
            "governance_containment": {"status": "passed", "reason_category": "none"},
            "coding_capability": {"status": "passed", "reason_category": "none"},
        },
        "decision_boundary": "A bridge-backed same-engine result establishes nothing about a non-incumbent executor.",
        "staleness_rule": "Re-qualify when the bridge or ACP version is superseded.",
    }
    rec.update(over)
    return rec


def _witness(**over):
    rec = {
        "schema_version": "executor-qualification.v1",
        "record_kind": "fail-closed-witness",
        "executor_id": "witness-external-refused",
        "recorded_at": "2026-08-31T00:00:00Z",
        "witness": {"scenario": "external-transport-refused",
                    "observed": "the coordinator refused the external transport by default"},
    }
    rec.update(over)
    return rec


class TestValidateRecord(unittest.TestCase):
    def test_good_qualification_validates(self):
        er.validate_record(_good_qual())  # no raise

    def test_witness_validates(self):
        er.validate_record(_witness())

    def test_non_dict_raises(self):
        with self.assertRaises(er.ExecutorRecordError):
            er.validate_record(["not", "a", "record"])

    def test_bad_scope_raises(self):
        with self.assertRaises(er.ExecutorRecordError):
            er.validate_record(_good_qual(scope="production"))

    def test_missing_gates_raises(self):
        rec = _good_qual()
        del rec["gates"]
        with self.assertRaises(er.ExecutorRecordError):
            er.validate_record(rec)

    def test_missing_vendored_identity_raises(self):
        rec = _good_qual()
        del rec["vendored_agent_identity"]
        with self.assertRaises(er.ExecutorRecordError):
            er.validate_record(rec)

    def test_witness_without_scenario_raises(self):
        rec = _witness()
        del rec["witness"]
        with self.assertRaises(er.ExecutorRecordError):
            er.validate_record(rec)


class TestLoad(unittest.TestCase):
    def test_load_missing_file_raises(self):
        with self.assertRaises(er.ExecutorRecordError):
            er.load_record("/no/such/record.json")

    def test_load_bad_json_raises(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write("{ not json ")
            path = fh.name
        try:
            with self.assertRaises(er.ExecutorRecordError):
                er.load_record(path)
        finally:
            os.unlink(path)

    def test_load_records_skips_gitkeep_and_subdirs(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, ".gitkeep"), "w").close()
            os.mkdir(os.path.join(d, "evidence"))
            with open(os.path.join(d, "evidence", "x.json"), "w") as fh:
                json.dump({"not": "a record"}, fh)  # under a subdir, must be ignored
            with open(os.path.join(d, "claude-agent-acp.json"), "w") as fh:
                json.dump(_good_qual(), fh)
            recs = er.load_records(d)
            self.assertEqual(len(recs), 1)
            self.assertEqual(recs[0]["executor_id"], "claude-agent-acp")

    def test_load_records_refuses_malformed_loudly(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "bad.json"), "w") as fh:
                json.dump(_good_qual(scope="production"), fh)
            with self.assertRaises(er.ExecutorRecordError):
                er.load_records(d)

    def test_qualification_records_excludes_witness(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "q.json"), "w") as fh:
                json.dump(_good_qual(), fh)
            with open(os.path.join(d, "w.json"), "w") as fh:
                json.dump(_witness(executor_id="w"), fh)
            self.assertEqual(len(er.load_records(d)), 2)
            self.assertEqual(len(er.qualification_records(d)), 1)


class TestCheckRuleBites(unittest.TestCase):
    """The plan's check-rule bite witness: the REAL engine/check/executor-record rule fires on a tampered record."""

    def test_real_check_rule_fires_on_tampered_record(self):
        rule = validate.load_json(os.path.join(validate.CHECK_DIR, "executor-record.json"))
        tampered = _good_qual(scope="production")  # a scope the non-production marker forbids
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(tampered, fh)
            path = fh.name
        try:
            _passed, findings = validate.run_unit(rule, {"path": path}, {})
            self.assertTrue(any(f.get("severity") == "hard" for f in findings),
                            f"the executor-record check did not bite a tampered record: {findings}")
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
