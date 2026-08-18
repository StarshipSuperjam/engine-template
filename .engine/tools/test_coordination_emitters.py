#!/usr/bin/env python3
"""Tests for coordination_emitters — the no-harm guarantee (an emit never raises and never affects the
caller), the solo-repo inertness gate, and one happy path through mocked internals
(StarshipSuperjam/engine-template#939)."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import coordination_emitters as ce  # noqa: E402

_ALL_EMITTERS = [
    lambda: ce.emit_integration_admitted(5),
    lambda: ce.emit_integration_blocked(5),
    lambda: ce.emit_integration_next(5),
    lambda: ce.emit_handoff_slot_released(5),
    lambda: ce.emit_revalidation_base_advanced(5, base_sha="a" * 40),
    lambda: ce.emit_bounded_status(5, "work-declared", paths=["a.py"]),
    lambda: ce.emit_overlap(5, 6, paths=["a.py"]),
]


class TestNoHarm(unittest.TestCase):
    def test_forced_raise_is_swallowed_for_every_emitter(self):
        with mock.patch.object(ce, "_FORCE_RAISE", True):
            for emit in _ALL_EMITTERS:
                self.assertIsNone(emit())  # never raises, always None

    def test_no_token_is_a_silent_noop(self):
        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "", "GITHUB_TOKEN": ""}, clear=False):
            with mock.patch.object(ce, "_repo_token", return_value=(None, None)):
                for emit in _ALL_EMITTERS:
                    self.assertIsNone(emit())


class TestSoloInert(unittest.TestCase):
    def test_no_peer_writes_nothing(self):
        with mock.patch.object(ce, "_repo_token", return_value=("o/r", "tok")), \
             mock.patch.object(ce, "_peer_present", return_value=False), \
             mock.patch("coordination_board.post_notice") as post:
            self.assertIsNone(ce.emit_integration_admitted(5))
            post.assert_not_called()


class TestHappyPath(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.mkdtemp()
        self._env = mock.patch.dict(os.environ, {"ENGINE_COORDINATION_CACHE_DIR": self.cache})
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_posts_a_valid_notice_and_records_event(self):
        posted = {}

        def _fake_post(client, number, notice):
            posted["notice"] = notice
            posted["number"] = number
            return "posted"

        with mock.patch.object(ce, "_repo_token", return_value=("o/r", "tok")), \
             mock.patch.object(ce, "_peer_present", return_value=True), \
             mock.patch("coordination_board.post_notice", _fake_post):
            outcome = ce.emit_integration_admitted(7)

        self.assertEqual(outcome, "posted")
        self.assertEqual(posted["number"], 7)
        self.assertEqual(posted["notice"]["kind"], "integration-notice")
        self.assertEqual(posted["notice"]["event"], "admitted")
        # a measurement event landed in the tmp ledger
        import coordination_ledger as cl
        evs = cl.events(path=os.path.join(self.cache, "coordination.json"))
        self.assertTrue(any(e["t"] == "posted" and e.get("pr") == 7 for e in evs))

    def test_blocked_records_late_conflict(self):
        with mock.patch.object(ce, "_repo_token", return_value=("o/r", "tok")), \
             mock.patch.object(ce, "_peer_present", return_value=True), \
             mock.patch("coordination_board.post_notice", return_value="posted"):
            ce.emit_integration_blocked(7)
        import coordination_ledger as cl
        evs = cl.events(path=os.path.join(self.cache, "coordination.json"))
        self.assertTrue(any(e["t"] == "late-conflict" for e in evs))


if __name__ == "__main__":
    unittest.main()
