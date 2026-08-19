#!/usr/bin/env python3
"""Tests for coordination_ledger — the local board-snapshot/seen bookkeeping and the bounded measurement ring
(StarshipSuperjam/engine-template#939). Every test points the ledger at a tmp file via path=, so no git layout or shared cache is
touched."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import coordination_ledger as cl  # noqa: E402


def _entry(nid, kind="integration-notice"):
    return {"notice_id": nid, "kind": kind, "event": "admitted"}


class LedgerTmp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "coordination.json")


class TestSnapshotAndSeen(LedgerTmp):
    def test_sync_returns_unseen_then_seen_clears(self):
        notices = [_entry("1" * 32), _entry("2" * 32)]
        unseen = cl.sync_board(5, notices, path=self.path)
        self.assertEqual({n["notice_id"] for n in unseen}, {"1" * 32, "2" * 32})
        cl.mark_seen(5, ["1" * 32], path=self.path)
        self.assertEqual(list(cl.pending(path=self.path).keys()), [5])
        self.assertEqual([e["notice_id"] for e in cl.pending(path=self.path)[5]], ["2" * 32])

    def test_pending_omits_fully_seen_pr(self):
        cl.sync_board(9, [_entry("a" * 32)], path=self.path)
        cl.mark_seen(9, ["a" * 32], path=self.path)
        self.assertEqual(cl.pending(path=self.path), {})

    def test_pending_spans_multiple_prs(self):
        cl.sync_board(1, [_entry("1" * 32)], path=self.path)
        cl.sync_board(2, [_entry("2" * 32)], path=self.path)
        self.assertEqual(set(cl.pending(path=self.path).keys()), {1, 2})

    def test_snapshot_refresh_reflects_current_board(self):
        cl.sync_board(3, [_entry("1" * 32), _entry("2" * 32)], path=self.path)
        # the board later shrinks to one notice: pending reflects the new snapshot, not the old
        cl.sync_board(3, [_entry("2" * 32)], path=self.path)
        self.assertEqual([e["notice_id"] for e in cl.pending(path=self.path)[3]], ["2" * 32])


class TestMeasurementRing(LedgerTmp):
    def test_records_and_reads(self):
        cl.record_event("posted", at="2026-08-18T00:00:00Z", pr=5, kind="integration-notice", path=self.path)
        evs = cl.events(path=self.path)
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["t"], "posted")
        self.assertEqual(evs[0]["pr"], 5)

    def test_unknown_event_type_ignored(self):
        cl.record_event("gossip", at="2026-08-18T00:00:00Z", path=self.path)
        self.assertEqual(cl.events(path=self.path), [])

    def test_ring_caps_at_500(self):
        for i in range(520):
            cl.record_event("queue-poll", at="2026-08-18T00:00:00Z", n=i, path=self.path)
        evs = cl.events(path=self.path)
        self.assertEqual(len(evs), 500)
        # oldest evicted: the first surviving event is n=20
        self.assertEqual(evs[0]["n"], 20)

    def test_long_string_field_is_dropped(self):
        cl.record_event("read", at="2026-08-18T00:00:00Z", blob="x" * 200, ok="short", path=self.path)
        ev = cl.events(path=self.path)[0]
        self.assertNotIn("blob", ev)   # a long string cannot leak content into the ring
        self.assertEqual(ev["ok"], "short")


class TestResilience(LedgerTmp):
    def test_missing_file_reads_empty(self):
        self.assertEqual(cl.pending(path=self.path), {})
        self.assertEqual(cl.events(path=self.path), [])

    def test_malformed_file_reads_empty(self):
        with open(self.path, "w") as fh:
            fh.write("{not json")
        self.assertEqual(cl.pending(path=self.path), {})


if __name__ == "__main__":
    unittest.main()
