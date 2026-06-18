"""Unit tests for the stable, content-free record id minted at capture (memory slice 4b).

The id (`records.RECORD_ID_KEY`, a `uuid4().hex`) is stamped in every record factory — turn-deltas
(`capture._make_record`), episodics and markers (`consolidate._make_episodic` / `_make_marker`). It is a
durable, content-free NAME for a record: unique per record, never derived from content, kept OUT of the search
body (`index._NON_BODY_KEYS`), and stable across an index rebuild and a ledger re-append (the compaction
precursor). These tests exercise the real minter, the real factories, and the real recall paths through them,
plus the back-compat tolerance of pre-4b records that carry no id, and the folded-in `SESSION_ENV` fix.
"""

from __future__ import annotations

import os
import string
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import capture, consolidate, index, ledger, records  # noqa: E402

_HEX = set(string.hexdigits.lower())


class _Base(unittest.TestCase):
    """A throwaway temp cabinet via ENGINE_MEMORY_DIR, mirroring test_forget._Base."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._prev = os.environ.get(ledger.ENV_DIR)
        os.environ[ledger.ENV_DIR] = self._tmp.name

    def tearDown(self):
        if self._prev is None:
            os.environ.pop(ledger.ENV_DIR, None)
        else:
            os.environ[ledger.ENV_DIR] = self._prev
        self._tmp.cleanup()


class MinterTests(unittest.TestCase):
    def test_new_record_id_is_32_char_lowercase_hex(self):
        rid = records.new_record_id()
        self.assertEqual(len(rid), 32)
        self.assertTrue(set(rid) <= _HEX, rid)              # content-free: just hex

    def test_two_mints_differ(self):
        self.assertNotEqual(records.new_record_id(), records.new_record_id())


class FactoryTests(unittest.TestCase):
    def test_every_factory_stamps_a_unique_nonempty_id(self):
        td = capture._make_record("S", 0, "user", "a turn note")
        ep = consolidate._make_episodic("S", {"role": "decision", "text": "an episode"}, "batch-x")
        mk = consolidate._make_marker("S", "batch-x")           # markers carry an id too (uniform invariant)
        ids = [r[records.RECORD_ID_KEY] for r in (td, ep, mk)]
        for rid in ids:
            self.assertIsInstance(rid, str)
            self.assertEqual(len(rid), 32)
        self.assertEqual(len(set(ids)), 3)                     # all three distinct

    def test_identical_text_gets_different_ids(self):
        a = capture._make_record("S", 0, "user", "exactly the same words")
        b = capture._make_record("S", 1, "user", "exactly the same words")
        self.assertNotEqual(a[records.RECORD_ID_KEY], b[records.RECORD_ID_KEY])  # not derived from content


class StabilityTests(_Base):
    def test_the_id_survives_an_index_rebuild_byte_for_byte(self):
        rec = capture._make_record("S", 0, "user", "the quokka decision")
        ledger.append(rec)
        index.rebuild()
        hits = [r for r in index.query("quokka").records if r.get("kind") == capture.RECORD_KIND]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][records.RECORD_ID_KEY], rec[records.RECORD_ID_KEY])  # index read it, not re-mint

    def test_re_appending_a_record_preserves_its_id(self):
        rec = capture._make_record("S", 0, "user", "a re-filed note")
        ledger.append(rec)
        ledger.append(rec)                                     # the move the future compaction will make
        ids = [r[records.RECORD_ID_KEY] for r in ledger.iter_records()
               if r.get("kind") == capture.RECORD_KIND]
        self.assertEqual(ids, [rec[records.RECORD_ID_KEY], rec[records.RECORD_ID_KEY]])  # preserved, not lost


class NonSearchableTests(_Base):
    def test_the_id_is_not_a_search_term(self):
        rec = capture._make_record("S", 0, "user", "findable by its words")
        ledger.append(rec)
        index.rebuild()
        self.assertTrue(index.query("findable").records)              # the words ARE findable
        self.assertEqual(index.query(rec[records.RECORD_ID_KEY]).records, [])  # the id is NOT


class BackCompatTests(_Base):
    def test_a_record_with_no_id_still_appends_indexes_and_queries(self):
        # a pre-4b turn-delta — NO "id" field — must read/index/query fine (no reader requires an id)
        ledger.append({"v": 1, "kind": capture.RECORD_KIND, "session_id": "S", "ts": 1, "seq": 0,
                       "speaker": "user", "text": "an old note about pelicans", "tags": ["stop"]})
        index.rebuild()
        hits = index.query("pelicans").records
        self.assertEqual(len(hits), 1)
        self.assertIsNone(hits[0].get(records.RECORD_ID_KEY))         # absent id tolerated, no crash

    def test_no_id_episodics_recall_and_retire_correctly(self):
        # both episodics are id-free (pre-4b): the no-batch one is live; the orphan-batch one still retires
        ledger.append({"v": 1, "kind": records.EPISODIC_KIND, "session_id": "S",
                       "text": "kept summary", "tags": ["episodic"]})
        ledger.append({"v": 1, "kind": records.EPISODIC_KIND, "session_id": "S",
                       "text": "orphan summary", "tags": ["episodic"], records.BATCH_KEY: "never-closed"})
        index.rebuild()
        texts = sorted(r["text"] for r in index.query("summary").records
                       if r.get("kind") == records.EPISODIC_KIND)
        self.assertEqual(texts, ["kept summary"])                    # orphan retired; no-batch kept — both id-free


class SessionEnvFixTests(unittest.TestCase):
    def test_capture_session_env_is_the_live_platform_var(self):
        # the slice-4b folded-in fix: capture's env fallback must name the live var (matches consolidate's)
        self.assertEqual(capture.SESSION_ENV, "CLAUDE_CODE_SESSION_ID")
        self.assertEqual(capture.SESSION_ENV, consolidate.SESSION_ENV)


if __name__ == "__main__":
    unittest.main()
