#!/usr/bin/env python3
"""Structural checks for the historical Build preservation artifact."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_coordinator_preservation as preservation  # noqa: E402
import build_coordinator as coordinator  # noqa: E402


ROOT = Path(__file__).resolve().parents[2]


class TestPreservationTraceability(unittest.TestCase):
    def test_every_mapped_obligation_has_one_live_structural_disposition(self):
        value = preservation.validate_map(ROOT)
        self.assertEqual(len(value["obligations"]), 68)
        self.assertEqual(len({row["id"] for row in value["obligations"]}), 68)

    def test_preservation_assurance_does_not_claim_semantic_proof(self):
        assurance = preservation.validate_map(ROOT)["preservation_source"]["assurance"].lower()
        self.assertIn("not proof of semantic equivalence", assurance)
        self.assertIn("independent qa", assurance)

    def test_runtime_protocol_does_not_scan_historical_preservation_anchors(self):
        value = coordinator._protocol()
        self.assertNotIn("obligations", value)
        self.assertNotIn("preservation_source", value)


if __name__ == "__main__":
    unittest.main()
