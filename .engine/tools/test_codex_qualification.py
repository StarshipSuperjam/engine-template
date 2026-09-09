"""Adversarial completeness checks for the live Codex qualification record."""
from copy import deepcopy
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import codex_qualification as qualification


def complete_record():
    return {
        "schema_version": "codex-qualification.v1",
        "recorded_at": "2026-09-08T15:00:00Z",
        "base_commit": "a" * 40, "head_commit": "b" * 40,
        "host": {key: {"value": value} for key, value in (
            ("kind", "CLI"), ("os", "macOS"), ("cli_version", "0.153.4"), ("host_version", "0.153.4"))},
        "official_sources": ["https://learn.chatgpt.com/docs/hooks"],
        "reproduction_commands": [["codex", "exec", "fixture prompt"]],
        "cells": [{"parent_mode": mode, "capability": capability,
                   "status": "not-verified", "requested": {"probe": capability},
                   "reason": "No live run in this synthetic test."}
                  for mode in ("read-only", "workspace-write")
                  for capability in ("custom-agent-model", "reasoning-effort", "parent-child-sandbox",
                                     "shell-availability", "file-writes", "hook-session-identity",
                                     "compact-context", "agent-observation", "spawn-payload")],
    }


class TestQualificationRecord(unittest.TestCase):
    def test_complete_unknown_record_does_not_claim_live_success(self):
        record = complete_record()
        self.assertEqual(qualification.validate_record(record), [])
        self.assertTrue(all(cell["status"] == "not-verified" for cell in record["cells"]))

    def test_rejects_incomplete_or_duplicate_matrix(self):
        for mutate in (lambda r: r["cells"].pop(), lambda r: r["cells"].append(deepcopy(r["cells"][0]))):
            with self.subTest(mutate=mutate):
                record = complete_record()
                mutate(record)
                self.assertTrue(qualification.validate_record(record))

    def test_rejects_invalid_status_or_missing_reason(self):
        for field, value in (("status", "skipped"), ("reason", " "), ("parent_mode", [])):
            with self.subTest(field=field):
                record = complete_record()
                record["cells"][0][field] = value
                self.assertTrue(qualification.validate_record(record))

    def test_observed_outcomes_require_settings_and_evidence(self):
        for status in ("pass", "fail"):
            record = complete_record()
            cell = record["cells"][0]
            cell.update(status=status, observed={"result": "fixture observation"}, evidence_refs=["run.jsonl:1"])
            self.assertEqual(qualification.validate_record(record), [])
            for field in ("requested", "observed", "evidence_refs"):
                broken = deepcopy(record)
                del broken["cells"][0][field]
                self.assertTrue(qualification.validate_record(broken), field)

    def test_host_unavailability_is_explicit(self):
        for field in ("kind", "os", "cli_version", "host_version"):
            record = complete_record()
            del record["host"][field]
            self.assertTrue(qualification.validate_record(record), field)
            record["host"][field] = {"value": None, "unavailable_reason": "Not exposed by this fixture."}
            self.assertEqual(qualification.validate_record(record), [])

    def test_rejects_missing_run_provenance(self):
        for field in ("recorded_at", "base_commit", "head_commit", "official_sources", "reproduction_commands"):
            record = complete_record()
            del record[field]
            self.assertTrue(qualification.validate_record(record), field)

    def test_timestamps_require_a_real_explicit_zone(self):
        for value in (None, [], "", "2026-09-08T15:00:00", "not-a-date"):
            record = complete_record()
            record["recorded_at"] = value
            self.assertTrue(qualification.validate_record(record), repr(value))
        for value in ("2026-09-08T15:00:00Z", "2026-09-08T08:00:00-07:00"):
            record = complete_record()
            record["recorded_at"] = value
            self.assertEqual(qualification.validate_record(record), [])

    def test_cli_is_read_only_and_honest_about_unknowns(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "record.json"
            original = json.dumps(complete_record())
            path.write_text(original)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(qualification.main([str(path)]), 0)
            self.assertEqual(path.read_text(), original)
            self.assertIn("Completeness only", json.loads(output.getvalue())["meaning"])
            path.write_text('{')
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(qualification.main([str(path)]), 1)


if __name__ == "__main__":
    unittest.main()
