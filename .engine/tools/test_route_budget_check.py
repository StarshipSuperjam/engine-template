#!/usr/bin/env python3
"""Unit tests for route_budget_check — the model-route projection budget guard (ADR 0336).

The `hard_check_bite` meta-check only exercises one leg (an over-length description) against one fixture. These
tests cover the legs it doesn't: the total-budget ceiling, the no-omission-from-Codex twin-parity leg, the
platform-truth reachability (an OMITTED invocation is model-auto = reachable, so it is still held to the
budget), and the guard's fail-closed posture on a malformed skill.
"""
from __future__ import annotations
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import route_budget_check as rbc  # noqa: E402


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _skill(root: str, slug: str, *, invocation="model-only", description="A route.",
           twin=True, twin_startable=True) -> None:
    """Seed a Claude skill (and, by default, a model-startable Codex twin) under `root`."""
    inv = f"invocation: {invocation}\n" if invocation is not None else ""
    ui = "user-invocable: false\n" if invocation == "model-only" else ""
    _write(os.path.join(root, ".claude", "skills", slug, "SKILL.md"),
           f"---\nname: {slug}\ndescription: {description}\n{inv}{ui}---\n\n## Steps\n\n1. Go.\n")
    if twin:
        val = "true" if twin_startable else "false"
        _write(os.path.join(root, ".agents", "skills", slug, "agents", "openai.yaml"),
               f"policy:\n  allow_implicit_invocation: {val}\n")


def _hard(fs) -> list:
    return [f for f in fs if f["severity"] == "hard"]


class RouteBudgetCheckTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="rbc-test-")
        self.addCleanup(__import__("shutil").rmtree, self.root, True)

    def test_a_clean_route_produces_no_finding(self):
        _skill(self.root, "engine-clean", description="A short, clean route description.")
        self.assertEqual(rbc.findings("hard", root=self.root), [])

    def test_over_length_description_fires_leg_A(self):
        _skill(self.root, "engine-long", description="x" * 121)
        fs = _hard(rbc.findings("hard", root=self.root))
        self.assertTrue(any("120-character" in f["message"] for f in fs))

    def test_omitted_invocation_route_is_still_held_to_the_budget(self):
        # BLOCKER regression: a route that omits the invocation line (model-auto by the platform default) is
        # model-reachable and must still be subject to the per-description ceiling — not silently exempt.
        _skill(self.root, "engine-omitted", invocation=None, description="y" * 121)
        fs = _hard(rbc.findings("hard", root=self.root))
        self.assertTrue(any("engine-omitted" in f["message"] and "120-character" in f["message"] for f in fs),
                        "an invocation-less (model-auto) route must be counted and its long description flagged")

    def test_total_budget_ceiling_fires_leg_B(self):
        # Isolate leg B: one clean route (description ≤120, valid twin) with the total ceiling forced tiny.
        _skill(self.root, "engine-b", description="A short route.")
        with mock.patch.object(rbc, "_TOTAL_CEILING", 5):
            fs = _hard(rbc.findings("hard", root=self.root))
        self.assertTrue(any("budget" in f["message"].lower() for f in fs))

    def test_missing_model_startable_twin_fires_leg_C(self):
        # No Codex twin at all → the route is reachable on Claude but stranded on Codex (the no-omission leg).
        _skill(self.root, "engine-notwin", description="A short route.", twin=False)
        fs = _hard(rbc.findings("hard", root=self.root))
        self.assertTrue(any("engine-notwin" in f["message"] and "Codex twin" in f["message"] for f in fs))

    def test_typed_only_twin_fires_leg_C(self):
        # A twin that exists but is NOT model-startable (allow_implicit_invocation: false) is still a strand.
        _skill(self.root, "engine-typedtwin", description="A short route.", twin_startable=False)
        fs = _hard(rbc.findings("hard", root=self.root))
        self.assertTrue(any("engine-typedtwin" in f["message"] and "Codex twin" in f["message"] for f in fs))

    def test_operator_typed_skill_is_not_projected(self):
        # An operator-typed command is not a model route: no description ceiling, no twin-parity requirement.
        _skill(self.root, "engine-op", invocation="operator-typed", description="z" * 200, twin=False)
        self.assertEqual(rbc.findings("hard", root=self.root), [])

    def test_malformed_frontmatter_fails_closed(self):
        # strict=True guard posture: an unparseable skill RAISES rather than silently vanishing from the scan.
        _write(os.path.join(self.root, ".claude", "skills", "engine-bad", "SKILL.md"),
               "---\ndescription: [unterminated\ninvocation: model-only\n---\n\n## Steps\n\n1. Go.\n")
        with self.assertRaises(Exception):
            rbc.findings("hard", root=self.root)


if __name__ == "__main__":
    unittest.main()
