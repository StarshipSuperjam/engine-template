"""Tests for `selftest_support.py`'s two env-var markers and the pure predicate built on them.

These cases must run everywhere — home repo, nested run, or projected deployed tree — so none of them is
gated on `selftest_support.CONSTRUCTION`: gating this module on the very predicate it verifies would let a
broken predicate hide by skipping its own test. `shape_verdict` is exercised as a pure function with an
injected `is_home` stub, never by reloading the module or mutating `os.environ`, so the case stays hermetic
regardless of which repo or environment it happens to run in.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import release_gate       # noqa: E402
import selftest           # noqa: E402
import selftest_support   # noqa: E402


_HOME = "/dummy/home/repo"
_FOREIGN = "/dummy/foreign/repo"


class TestShapeVerdict(unittest.TestCase):
    def test_home_repo_bare_environ_is_true(self):
        self.assertTrue(selftest_support.shape_verdict(_HOME, {}, is_home=lambda root: root == _HOME))

    def test_home_repo_with_nested_marker_is_true(self):
        # NESTED_ENV is a recursion guard only — it must never affect the verdict.
        environ = {selftest_support.NESTED_ENV: "1"}
        self.assertTrue(selftest_support.shape_verdict(_HOME, environ, is_home=lambda root: root == _HOME))

    def test_home_repo_with_projection_marker_is_false(self):
        environ = {selftest_support.PROJECTION_ENV: "1"}
        self.assertFalse(selftest_support.shape_verdict(_HOME, environ, is_home=lambda root: root == _HOME))

    def test_foreign_repo_bare_environ_is_false(self):
        self.assertFalse(selftest_support.shape_verdict(_FOREIGN, {}, is_home=lambda root: root == _HOME))


class TestMarkerNamesArePinned(unittest.TestCase):
    def test_nested_env_is_pinned_across_its_three_homes(self):
        self.assertEqual(selftest_support.NESTED_ENV, selftest._NESTED_ENV)
        self.assertEqual(selftest_support.NESTED_ENV, release_gate._NESTED_ENV)

    def test_projection_env_is_pinned_between_support_and_release_gate(self):
        self.assertEqual(selftest_support.PROJECTION_ENV, release_gate._PROJECTION_ENV)


class TestNestedEnvSetsBothMarkers(unittest.TestCase):
    def test_nested_env_helper_carries_both_markers(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            env = release_gate._nested_env()
        self.assertEqual(env.get(selftest_support.NESTED_ENV), "1")
        self.assertEqual(env.get(selftest_support.PROJECTION_ENV), "1")


if __name__ == "__main__":
    unittest.main()
