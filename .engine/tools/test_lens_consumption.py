#!/usr/bin/env python3
"""Self-tests for the lens-consumption consumer (lens_consumption_check.py): the custom/script guard
that diffs the installed review lenses against the consumed set build orchestration records.

Run: uv run --directory .engine --frozen -- python tools/selftest.py

These lock the CONSUMER's contract (the pure diff leg validate.dangling_lens_findings is locked in
test_agent.py): the Build protocol's review_consumers resolve to exactly the nine installed lens tokens;
a MISSING or OFF-SCHEMA protocol fails CLOSED (the loader raises → the custom/script kind's hard
finding) rather than passing an unjudged roster as "nothing dangling"; the live repository is clean
(every installed review is consumed); and the demo runs its real fail-then-pass.
"""
from __future__ import annotations
import contextlib
import io
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402
import build_protocol  # noqa: E402
import lens_consumption_check as lc  # noqa: E402

EXPECTED = {"product-intent", "architecture", "feasibility", "risk-governance",
            "spec-conformance", "divergence-hunter", "usability", "technical-integrity", "security-governance"}


class TestConsumedRecord(unittest.TestCase):
    """The consumed set is the Build protocol's review_consumers, resolved by the shared loader; a missing
    or off-schema protocol fails CLOSED (the loader raises → the custom/script kind's hard finding)."""

    def test_the_protocol_yields_exactly_the_nine_tokens(self):
        self.assertEqual(lc.consumed_lenses(), EXPECTED)

    def test_missing_protocol_fails_closed(self):
        with mock.patch.dict(os.environ, {build_protocol.ENV_OVERRIDE: "/nonexistent/build-protocol.json"}):
            with self.assertRaises(build_protocol.ProtocolError):
                lc.consumed_lenses()

    def test_off_schema_protocol_fails_closed(self):
        with mock.patch.dict(os.environ, {build_protocol.ENV_OVERRIDE: ".engine/_fixtures/build-protocol/malformed-protocol.json"}):
            with self.assertRaises(build_protocol.ProtocolError):
                lc.consumed_lenses()


class TestLiveRepository(unittest.TestCase):
    def test_consumed_lenses_reads_the_committed_record(self):
        self.assertEqual(lc.consumed_lenses(), EXPECTED)

    def test_check_is_green_on_the_live_roster(self):
        """Every installed review lens is consumed today, so the check emits an empty finding array."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = lc.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue()), [])

    def test_demo_runs_its_real_fail_then_pass(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = lc.main(["demo"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        # The fail-then-pass narration assumes review packs are installed. A deployment that DECLINED both
        # review packs has no reviews to consume, so the demo prints the empty-roster line and returns before
        # the fail/pass halves — a legitimate state, not a failure (#646). Key the assertion on whether any
        # review persona is actually installed.
        import agent_coherence_check
        reviews_installed = any(a.get("role") in {"plan-review", "pre-submission-review"} and a.get("lens")
                                for a in agent_coherence_check.engine_agents())
        if reviews_installed:
            self.assertIn("all clear", out)
            self.assertIn("turns RED", out)
        else:
            self.assertIn("no review packs are installed", out)


if __name__ == "__main__":
    unittest.main()
