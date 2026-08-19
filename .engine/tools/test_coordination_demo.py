#!/usr/bin/env python3
"""The coordination two-session demo passes, and can genuinely FAIL (StarshipSuperjam/engine-template#939) — a demo that can only
succeed is not evidence."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import demo_coordination_two_sessions as demo  # noqa: E402
import quiet_call  # noqa: E402  (capture the demo walkthrough's stdout so it can't bury the suite summary)


class TestCoordinationDemo(unittest.TestCase):
    def test_demo_passes(self):
        self.assertEqual(quiet_call.run(demo.main), 0)

    def test_demo_fails_when_the_forged_skip_control_breaks(self):
        # If the reader stopped skipping a tampered block (returned it as a real notice), control 2 must bite.
        real = demo.board.read_board

        def _leaky(client, number):
            out = real(client, number)
            return out or [{"kind": "integration-notice", "event": "admitted"}]  # pretend tamper survived

        with mock.patch("coordination_board.read_board", _leaky):
            self.assertEqual(quiet_call.run(demo.main), 1)


if __name__ == "__main__":
    unittest.main()
