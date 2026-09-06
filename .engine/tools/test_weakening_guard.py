#!/usr/bin/env python3
"""Self-tests for the weakening guard's hard-tier membership of the engine-ci gate and its helper.

The gatekeeper's docstring binds any helper module it grows to join both of the guard's sets in the same
change; `change_classification.py` is that helper. The broader tier tests live in test_seed.py (every
_HARD_EXACT member is asserted guarded there); this module pins the binding itself, so a later change that
drops the helper from either set is caught by name rather than noticed in review.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import weakening_guard  # noqa: E402

GATE = ".engine/tools/ci_gatekeeper.py"
HELPER = ".engine/tools/change_classification.py"


class TestGateAndHelperMembership(unittest.TestCase):
    def test_the_gate_and_its_helper_are_in_both_hard_tier_sets(self):
        for path in (GATE, HELPER):
            self.assertIn(path, weakening_guard._FLOOR_ENFORCEMENT_HOOKS, path)
            self.assertIn(path, weakening_guard._HARD_EXACT, path)

    def test_a_modification_to_either_is_hard(self):
        for path in (GATE, HELPER):
            self.assertEqual(weakening_guard.classify(path, "modified", instance_guards=(frozenset(), ())),
                             "hard", path)
            self.assertTrue(weakening_guard.is_guardrail(path, derived_scripts=frozenset()), path)

    def test_the_helper_docstring_restates_the_binding(self):
        # The binding is one level deep unless each helper repeats it; the classifier does.
        import change_classification
        self.assertIn("Any helper module this grows joins both sets in the same change",
                      " ".join(change_classification.__doc__.split()))


if __name__ == "__main__":
    unittest.main()
