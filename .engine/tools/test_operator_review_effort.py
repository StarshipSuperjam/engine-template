#!/usr/bin/env python3
"""Self-tests for the operator per-depth review-effort override reader (StarshipSuperjam/engine-template#677).

Run: uv run --directory .engine --frozen -- python tools/selftest.py

These lock: a missing/malformed file degrades to {} (shipped defaults, the safe direction); a valid slice is
read; an unrecognised depth or effort is DROPPED (degrade-up) and surfaced by stale_slices; set/forget round-
trip; and the file is registered as preserved operator config in module_coherence.OPERATOR_CONFIG so it
survives an engine update.
"""
from __future__ import annotations
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import operator_review_effort as ore  # noqa: E402
import module_coherence  # noqa: E402


def _write(d, data):
    path = os.path.join(d, "operator-review-effort.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    return path


class TestLoad(unittest.TestCase):
    def test_missing_file_degrades_to_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(ore.load(os.path.join(d, "nope.json")), {})

    def test_malformed_file_degrades_to_empty(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "operator-review-effort.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{not json")
            self.assertEqual(ore.load(path), {})

    def test_valid_slice_is_read(self):
        with tempfile.TemporaryDirectory() as d:
            path = _write(d, {"standard": {"effort": "low"}, "thorough": {"effort": "high"}})
            self.assertEqual(ore.load(path), {"standard": {"effort": "low"}, "thorough": {"effort": "high"}})

    def test_unrecognised_depth_or_effort_is_dropped_safe_direction(self):
        with tempfile.TemporaryDirectory() as d:
            path = _write(d, {"quick": {"effort": "low"},          # quick runs no reviewers -> dropped
                              "standard": {"effort": "maximum"},   # not a real level -> dropped
                              "thorough": {"effort": "medium"}})   # valid -> kept
            self.assertEqual(ore.load(path), {"thorough": {"effort": "medium"}})

    def test_stale_slices_surfaces_dropped_entries(self):
        with tempfile.TemporaryDirectory() as d:
            path = _write(d, {"quick": {"effort": "low"}, "standard": {"effort": "maximum"},
                              "thorough": {"effort": "high"}})
            stale = ore.stale_slices(path)
            self.assertTrue(any(s.startswith("quick:") for s in stale))
            self.assertTrue(any(s.startswith("standard:") for s in stale))
            self.assertFalse(any(s.startswith("thorough:") for s in stale))   # the valid one is not stale


class TestSetForget(unittest.TestCase):
    def test_set_then_load_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "operator-review-effort.json")
            ore.set_effort("standard", "low", path)
            self.assertEqual(ore.load(path), {"standard": {"effort": "low"}})

    def test_set_refuses_unknown_depth_and_effort(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "operator-review-effort.json")
            with self.assertRaises(ValueError):
                ore.set_effort("quick", "low", path)
            with self.assertRaises(ValueError):
                ore.set_effort("standard", "maximum", path)

    def test_forget_reverts_and_removes_empty_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "operator-review-effort.json")
            ore.set_effort("standard", "low", path)
            ore.set_effort("thorough", "medium", path)
            ore.forget("standard", path)
            self.assertEqual(ore.load(path), {"thorough": {"effort": "medium"}})
            ore.forget("thorough", path)
            self.assertFalse(os.path.isfile(path))   # last override cleared -> file removed


class TestPreservedAcrossUpgrade(unittest.TestCase):
    def test_file_is_registered_operator_config(self):
        # OPERATOR_CONFIG membership is what preserves the file across the upgrade overlay and exempts it from
        # the ownership orphan leg — the whole survival guarantee (#677).
        self.assertIn(".engine/operator-review-effort.json", module_coherence.OPERATOR_CONFIG)


if __name__ == "__main__":
    unittest.main()
