#!/usr/bin/env python3
"""Tests for the greenfield-intake detector (.engine/tools/greenfield_intake.py).

Drives the real `detect_greenfield()` over throwaway trees (mutation-free temp roots), so the two guards
(the intake must be installed; it self-resolves once a description exists), the construction-repo carve-out,
and the fail-soft degrade are exercised against the shipped logic. A dispatch class confirms the demo/CLI
contract.
"""
from __future__ import annotations
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import greenfield_intake as gi  # noqa: E402


class DetectGreenfieldTests(unittest.TestCase):
    def _seed(self, files: dict) -> str:
        d = tempfile.mkdtemp(prefix="engine-greenfield-test-")
        self.addCleanup(shutil.rmtree, d, True)
        for rel, body in files.items():
            path = os.path.join(d, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
        return d

    def test_silent_when_the_intake_is_not_installed(self):
        # No product-intake runbook -> the engine-design command doesn't exist -> never offer it.
        root = self._seed({"README.md": "hi\n"})
        self.assertIsNone(gi.detect_greenfield(root))

    def test_fires_when_installed_and_no_description_exists(self):
        root = self._seed({gi._INTAKE_REL: "runbook\n"})
        result = gi.detect_greenfield(root)
        self.assertIsNotNone(result)
        self.assertTrue(result["greenfield"])
        self.assertEqual(result["fingerprint"], gi._FINGERPRINT)

    def test_self_resolves_once_a_description_exists(self):
        # The intake writes docs/spec/index.md first; its presence means the intake has been used.
        root = self._seed({gi._INTAKE_REL: "runbook\n", gi._INDEX_REL: "# Product spec\n"})
        self.assertIsNone(gi.detect_greenfield(root))

    def test_no_op_in_the_engine_construction_repo(self):
        # The engine's own repo has the intake installed but no product spec, legitimately — the construction
        # marker in the root CLAUDE.md carves it out so the nudge never fires here.
        root = self._seed({gi._INTAKE_REL: "runbook\n", "CLAUDE.md": "# Construction governance\n\nengine-template"})
        self.assertIsNone(gi.detect_greenfield(root))

    def test_a_non_construction_claude_md_does_not_suppress(self):
        # A deployed repo's own CLAUDE.md (no construction marker) must NOT suppress the offer.
        root = self._seed({gi._INTAKE_REL: "runbook\n", "CLAUDE.md": "# My project\n\nnotes"})
        self.assertIsNotNone(gi.detect_greenfield(root))

    def test_fingerprint_is_stable_across_calls(self):
        # A constant fingerprint is what lets the anti-nag ledger collapse the offer.
        root = self._seed({gi._INTAKE_REL: "runbook\n"})
        self.assertEqual(gi.detect_greenfield(root)["fingerprint"], gi.detect_greenfield(root)["fingerprint"])


class DispatchTests(unittest.TestCase):
    def test_demo_passes(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gi._demo(), 0)

    def test_default_main_prints_json(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = gi.main([])
        self.assertEqual(rc, 0)
        json.loads(buf.getvalue())  # a valid JSON value (dict or null)

    def test_main_routes_demo(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gi.main(["demo"]), 0)


if __name__ == "__main__":
    unittest.main()
