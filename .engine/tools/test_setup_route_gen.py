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
        # Every route has structured targets; a setup route funnels into the engine-setup dispatcher.
        for rel, text in sr.derive().items():
            self.assertIn("kind: skill", text, f"{rel} must name a skill target")
            self.assertIn("ref: engine-setup", text, f"{rel} must target the engine-setup dispatcher")

    def test_committed_routes_are_in_sync(self):
        # HARD findings only: a checkout that declined an add-on legitimately carries its kept route, which
        # the gate discloses as a SOFT note rather than a drift. Comparing the whole list would read that
        # expected disclosure as a failure.
        self.assertEqual(_hard(sr.check("hard")), [],
                         "the committed setup routes must match the derived text")

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


class StrayRouteToleranceTests(unittest.TestCase):
    """The stray-route branch's BOTH arms. The tolerance is the only part of this gate with no negative
    fixture behind it (the bite harness pins one message per check), so these are its only mechanical trace."""

    def _tree(self, d, *, installed, known, routes):
        """A checkout shape: `installed` is engine.json's package roster, `known` the modules the committed
        module-surfaces registry names, `routes` the engine-setup-* directories on disk."""
        os.makedirs(os.path.join(d, ".engine", "provisioning"), exist_ok=True)
        with open(os.path.join(d, ".engine", "engine.json"), "w", encoding="utf-8") as fh:
            json.dump({"packages": {m: "1.0.0" for m in installed}}, fh)
        with open(os.path.join(d, ".engine", "provisioning", "module-surfaces.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"surfaces": {f".engine/modules/{m}/manifest.json": [m] for m in known}}, fh)
        for name in routes:
            p = os.path.join(d, ".claude", "skills", name)
            os.makedirs(p, exist_ok=True)
            with open(os.path.join(p, "SKILL.md"), "w", encoding="utf-8") as fh:
                fh.write("a surviving route\n")

    def test_a_declined_modules_route_is_tolerated_and_disclosed(self):
        with tempfile.TemporaryDirectory() as d:
            self._tree(d, installed=["core"], known=["core", "qa-review"],
                       routes=["engine-setup-qa-review"])
            fs = sr.check("hard", root=d)
            self.assertEqual(_hard(fs), [], "a declined module's kept route is not a hard finding")
            self.assertTrue(any(f["severity"] == "soft" and "qa-review" in f["message"] for f in fs),
                            "the tolerance is disclosed, never silent")

    def test_a_route_no_real_module_owns_is_still_flagged(self):
        with tempfile.TemporaryDirectory() as d:
            self._tree(d, installed=["core"], known=["core", "qa-review"],
                       routes=["engine-setup-qa-revue"])   # a typo / renamed / retired module
            fs = _hard(sr.check("hard", root=d))
            self.assertTrue(any("engine-setup-qa-revue" in f["message"] and "stale" in f["message"]
                                for f in fs), "a route the registry does not know stays a hard finding")

    def test_an_unreadable_installed_roster_tolerates_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            self._tree(d, installed=["core"], known=["core", "qa-review"],
                       routes=["engine-setup-qa-review"])
            os.remove(os.path.join(d, ".engine", "engine.json"))   # roster unreadable → fail closed
            fs = _hard(sr.check("hard", root=d))
            self.assertTrue(any("engine-setup-qa-review" in f["message"] for f in fs),
                            "an undecidable roster must soften nothing")

    def test_a_declined_modules_deleted_route_is_flagged(self):
        with tempfile.TemporaryDirectory() as d:
            self._tree(d, installed=["core"], known=["core", "qa-review"], routes=[])
            fs = _hard(sr.check("hard", root=d))
            self.assertTrue(any("engine-setup-qa-review" in f["message"] and "missing" in f["message"]
                                for f in fs), "a declined module's route must still be PRESENT")


if __name__ == "__main__":
    unittest.main()
