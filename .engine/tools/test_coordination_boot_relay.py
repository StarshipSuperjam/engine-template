#!/usr/bin/env python3
"""Tests for boot's coordination relay (StarshipSuperjam/engine-template#939): it renders unread notices from the LOCAL ledger only
(no network), carries only enum kinds + counts, and is silent when there is nothing unread or no ledger."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import boot  # noqa: E402
import coordination_ledger as cl  # noqa: E402


class TestBootRelay(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.mkdtemp()
        env = mock.patch.dict(os.environ, {"ENGINE_COORDINATION_CACHE_DIR": self.cache})
        env.start()
        self.addCleanup(env.stop)
        self.ledger = os.path.join(self.cache, "coordination.json")

    def test_silent_when_nothing_unread(self):
        self.assertEqual(boot.render_coordination(), "")

    def test_renders_unread_with_kinds_and_counts_only(self):
        cl.sync_board(7, [{"notice_id": "a" * 32, "kind": "integration-notice"},
                          {"notice_id": "b" * 32, "kind": "revalidation-notice"}], path=self.ledger)
        # boot reads the default ledger path (env-pointed at our tmp dir)
        block = boot.render_coordination()
        self.assertIn("pull request #7", block)
        self.assertIn("integration-notice", block)
        self.assertIn("2 unread", block)

    def test_seen_notices_drop_out(self):
        cl.sync_board(7, [{"notice_id": "a" * 32, "kind": "integration-notice"}], path=self.ledger)
        cl.mark_seen(7, ["a" * 32], path=self.ledger)
        self.assertEqual(boot.render_coordination(), "")

    def test_broken_ledger_is_silent(self):
        with open(self.ledger, "w") as fh:
            fh.write("{not json")
        self.assertEqual(boot.render_coordination(), "")


if __name__ == "__main__":
    unittest.main()
