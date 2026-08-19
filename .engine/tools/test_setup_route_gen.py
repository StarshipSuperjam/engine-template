#!/usr/bin/env python3
"""Tests for setup_route_gen — the per-module setup-route generator + its derived-committed drift gate.

The `hard_check_bite` meta-check exercises the drift gate against one seeded fixture (a tree whose routes are
gone). These tests cover the rest: that derivation is deterministic, that the real committed routes are in
sync, and that both a missing and a content-drifted committed route are flagged.
"""
from __future__ import annotations
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import setup_route_gen as sr  # noqa: E402


def _hard(fs) -> list:
    return [f for f in fs if f["severity"] == "hard"]


def _seed_module(root: str, mid: str = "fixture-addon") -> None:
    """Seed one offerable module in `root` so the derivation has something to derive THERE. The check roots
    both sides, so a bare tempdir derives nothing and no drift is expressible — and a deployment that declined
    its add-ons has no offerable manifest either. Seeding keeps these cases hermetic and assertable in any
    projection, rather than borrowing whichever module the ambient tree happens to carry."""
    d = os.path.join(root, ".engine", "modules", mid)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump({"id": mid, "status": "optional",
                   "presentation": {"description": "A seeded add-on for these tests.",
                                    "category": "review",
                                    "setup_trigger": "the operator asks to set up the seeded add-on"}}, fh)


class SetupRouteGenTests(unittest.TestCase):
    def test_derive_is_deterministic(self):
        self.assertEqual(sr.derive(), sr.derive())

    def test_every_setup_route_carries_the_engine_setup_target(self):
        # ADR-0336: every route has structured targets; a setup route funnels into the engine-setup dispatcher.
        for rel, text in sr.derive().items():
            self.assertIn("kind: skill", text, f"{rel} must name a skill target")
            self.assertIn("ref: engine-setup", text, f"{rel} must target the engine-setup dispatcher")

    def test_committed_routes_are_in_sync(self):
        self.assertEqual(sr.check("hard"), [], "the committed setup routes must match the derived text")

    def test_missing_committed_route_is_flagged(self):
        with tempfile.TemporaryDirectory() as d:   # a seeded module, no committed route → it is missing
            _seed_module(d)
            fs = _hard(sr.check("hard", root=d))
            self.assertTrue(fs)
            self.assertTrue(any("is missing" in f["message"] for f in fs))

    def test_content_drifted_route_is_flagged(self):
        with tempfile.TemporaryDirectory() as d:
            _seed_module(d)
            rel, _text = sorted(sr.derive(d).items())[0]
            p = os.path.join(d, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("a hand-edited setup route the generator would never write\n")
            fs = _hard(sr.check("hard", root=d))
            self.assertTrue(any(rel in f["message"] and "out of date" in f["message"] for f in fs))


if __name__ == "__main__":
    unittest.main()
