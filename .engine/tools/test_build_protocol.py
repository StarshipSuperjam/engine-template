#!/usr/bin/env python3
"""Self-tests for the Build protocol loader (build_protocol.py) and its merge check (build_protocol_check.py):
the protocol loads and resolves every consumed lens; a missing or off-schema protocol raises (fail-closed);
the runbook's generated review-consumers region is current, a drift is caught and `render` restores it;
the registered check is green on the live tree, bites its negative fixture, and names a revived
Markdown sentinel.

Run: uv run --directory .engine --frozen -- python tools/selftest.py
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
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402
import build_protocol as bp  # noqa: E402
import build_protocol_check as bpc  # noqa: E402

EXPECTED = {"product-intent", "architecture", "feasibility", "risk-governance",
            "spec-conformance", "divergence-hunter", "usability", "technical-integrity", "security-governance"}
FIXTURE = ".engine/_fixtures/build-protocol/malformed-protocol.json"


def _scratch_tree() -> str:
    """A throwaway root carrying copies of the protocol, its schema and the runbook, so a test can corrupt
    them without touching the committed tree."""
    tmp = tempfile.mkdtemp(prefix="build-protocol-")
    for rel in (bp.PROTOCOL_REL, bp.SCHEMA_REL, bp.RUNBOOK_REL):
        dst = os.path.join(tmp, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(os.path.join(validate.ROOT, rel), dst)
    return tmp


class TestLoader(unittest.TestCase):
    def test_live_protocol_loads_and_every_stage_resolves(self):
        protocol = bp.load()
        stages = {c["stage"]: c["lenses"] for c in bp.consumers(protocol)}
        self.assertEqual(set(stages), {"plan-review gate", "product-design spec lock", "pre-submission gate"})
        self.assertEqual(bp.consumed_lenses(protocol), EXPECTED)

    def test_the_two_rosters_are_not_copies(self):
        """The protocol names rosters; the lens lists live once — with the Project Manager and in
        deliverable_review — so no consumer entry carries a lens list of its own."""
        for entry in bp.load()["review_consumers"]:
            self.assertEqual(set(entry), {"stage", "roster"})

    def test_missing_protocol_fails_closed(self):
        with mock.patch.dict(os.environ, {bp.ENV_OVERRIDE: "/nonexistent/build-protocol.json"}):
            with self.assertRaises(bp.ProtocolError):
                bp.load()

    def test_off_schema_protocol_fails_closed_naming_the_path(self):
        with mock.patch.dict(os.environ, {bp.ENV_OVERRIDE: FIXTURE}):
            with self.assertRaises(bp.ProtocolError) as ctx:
                bp.load()
        self.assertIn("review_consumers", str(ctx.exception))

    def test_unreadable_json_fails_closed(self):
        tmp = _scratch_tree()
        try:
            with open(os.path.join(tmp, bp.PROTOCOL_REL), "w", encoding="utf-8") as fh:
                fh.write("{not json")
            with self.assertRaises(bp.ProtocolError):
                bp.load(tmp)
        finally:
            shutil.rmtree(tmp)

    def test_an_unknown_roster_is_refused_even_past_the_schema(self):
        protocol = bp.load()
        with self.assertRaises(bp.ProtocolError):
            bp.roster_lenses(protocol, "nobody")


class TestProjection(unittest.TestCase):
    def test_committed_runbook_region_is_current(self):
        expected, actual = bp.projection_status()
        self.assertEqual(actual, expected, "run `build_protocol.py render` and commit the runbook")

    def test_drift_is_caught_and_render_restores(self):
        tmp = _scratch_tree()
        try:
            path = os.path.join(tmp, bp.RUNBOOK_REL)
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            corrupted = text.replace("pre-submission gate", "pre-submission gate (hand-edited)")
            self.assertNotEqual(corrupted, text)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(corrupted)
            expected, actual = bp.projection_status(tmp)
            self.assertNotEqual(actual, expected)
            self.assertTrue(bp.apply(tmp))
            expected, actual = bp.projection_status(tmp)
            self.assertEqual(actual, expected)
            self.assertFalse(bp.apply(tmp), "a second render is a no-op")
        finally:
            shutil.rmtree(tmp)

    def test_a_runbook_without_the_region_is_refused_by_render(self):
        tmp = _scratch_tree()
        try:
            path = os.path.join(tmp, bp.RUNBOOK_REL)
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            before, region, after = bp._split_runbook(text)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(before + after)
            with self.assertRaises(bp.ProtocolError):
                bp.apply(tmp)
            self.assertIsNone(bp.projection_status(tmp)[1])
        finally:
            shutil.rmtree(tmp)

    def test_a_second_copy_of_the_region_is_drift_not_a_match(self):
        # A duplicate marker pair would be a place to hide prose behind the name this check guards (repair
        # round 2 of typed-lifecycle part C): the split refuses it, so the check reports the runbook as drifted.
        tmp = _scratch_tree()
        try:
            path = os.path.join(tmp, bp.RUNBOOK_REL)
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            before, region, after = bp._split_runbook(text)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(before + region + after + "\n" + region.replace("\n", "\nhidden prose\n", 1) + "\n")
            self.assertIsNone(bp.projection_status(tmp)[1])
        finally:
            shutil.rmtree(tmp)


class TestCheck(unittest.TestCase):
    def _run(self, env: dict | None = None) -> list:
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env or {}), contextlib.redirect_stdout(buf):
            rc = bpc.main([])
        self.assertEqual(rc, 0)
        return json.loads(buf.getvalue())

    def test_green_on_the_live_tree(self):
        self.assertEqual(self._run(), [])

    def test_bites_its_negative_fixture(self):
        found = self._run({bp.ENV_OVERRIDE: FIXTURE})
        self.assertTrue(any(f["severity"] == "hard" and "does not load as build-protocol.v1" in f["message"]
                            for f in found), found)

    def test_a_revived_markdown_sentinel_is_a_finding(self):
        tmp = _scratch_tree()
        try:
            os.makedirs(os.path.join(tmp, ".engine/policies"), exist_ok=True)
            stray = os.path.join(tmp, ".engine/policies/stray.md")
            with open(stray, "w", encoding="utf-8") as fh:
                fh.write("# stray\n\n```text\nconsumed-review-lenses:\n  x: y\n```\n")
            found = bpc.findings("hard", tmp)
            self.assertTrue(any("consumed-review-lenses:" in f["message"] and "stray.md" in f["message"]
                                for f in found), found)
        finally:
            shutil.rmtree(tmp)

    def test_drift_in_the_runbook_is_a_finding(self):
        tmp = _scratch_tree()
        try:
            path = os.path.join(tmp, bp.RUNBOOK_REL)
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text.replace("pre-submission gate", "pre-submission gate (edited)"))
            found = bpc.findings("hard", tmp)
            self.assertTrue(any("has drifted" in f["message"] for f in found), found)
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
