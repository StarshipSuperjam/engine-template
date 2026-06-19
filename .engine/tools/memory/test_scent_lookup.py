"""test_scent_lookup.py — the per-prompt scent's fast lexical lookup: index.scent_lookup (slice 5, PR 2).

Run via the engine's CI command:
    uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py'

scent_lookup is the scent-shaped primitive `search` is NOT (search pays an O(ledger) usage pass and is
implicit-AND). These pin its laws: OR-match (a record sharing ANY salient term is found, unlike search's
all-terms-AND); RELEVANCE-ONLY with NO usage/access pass (the latency fix — a source-scan asserts it never
reaches forget._access_index/live_records); FAST-PATH ONLY (FTS5-absent / missing / stale / broken index →
empty, never the slow scan); bm25-ranked best-first carrying records.SCORE_KEY; and SIDE-EFFECT-FREE.
"""

import inspect
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory import forget, index, ledger, records  # noqa: E402

_ID = records.RECORD_ID_KEY
_SCORE = records.SCORE_KEY


class ScentLookupTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="engine-scent-lookup-")
        self.ledger = os.path.join(self.tmp, "ledger.ndjson")
        self.index = os.path.join(self.tmp, "index.sqlite3")
        self.now = int(time.time())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add(self, text, *, role="observation", tags=()):
        rid = records.new_record_id()
        ledger.append({_ID: rid, "ts": self.now, "role": role, "tags": list(tags), "text": text},
                      path=self.ledger)
        return rid

    def rebuild(self):
        return index.rebuild(ledger_file=self.ledger, index_file=self.index)

    def lookup(self, text, **kw):
        return index.scent_lookup(text, ledger_file=self.ledger, index_file=self.index, **kw)

    def ids(self, result):
        return [r.get(_ID) for r in result.records]


class MatchTests(ScentLookupTestCase):
    def test_or_match_finds_a_record_sharing_any_term(self):
        # search's implicit-AND would need ONE record with every prompt word; scent_lookup OR-matches, so a
        # record sharing a SINGLE distinctive term is found from a multi-word prompt.
        a = self.add("the calendar sync was decided", role="decision")
        self.add("unrelated note about onboarding copy")
        self.rebuild()
        res = self.lookup("how should we handle the calendar sync today")
        self.assertIn(a, self.ids(res))

    def test_distinctive_term_outranks_common_one(self):
        strong = self.add("calendar calendar scheduling owner decided", role="decision")
        for t in ("the meeting moved", "the copy is short", "the config is set", "the release shipped"):
            self.add(t)
        self.rebuild()
        res = self.lookup("the calendar")
        self.assertTrue(res.available)
        self.assertEqual(self.ids(res)[0], strong)  # the distinctive match leads
        # and its salience clears 0.5 while the common-only matches do not
        top = res.records[0][_SCORE]
        self.assertGreaterEqual(top / (1.0 + top), 0.5)

    def test_common_term_scores_below_the_bar(self):
        for t in ("the first note", "the second note", "the third note"):
            self.add(t)
        self.rebuild()
        res = self.lookup("the")
        self.assertTrue(res.available)
        for r in res.records:
            s = r[_SCORE]
            self.assertLess(s / (1.0 + s), 0.5)  # a ubiquitous term is a weak match (bm25 IDF ~ 0)

    def test_each_record_carries_a_positive_score(self):
        self.add("calendar sync decided", role="decision")
        self.rebuild()
        res = self.lookup("calendar")
        self.assertTrue(res.records)
        for r in res.records:
            self.assertIn(_SCORE, r)
            self.assertIsInstance(r[_SCORE], float)
            self.assertGreaterEqual(r[_SCORE], 0.0)

    def test_limit_caps_the_topk(self):
        for i in range(8):
            self.add(f"calendar entry number {i}")
        self.rebuild()
        self.assertEqual(len(self.lookup("calendar", limit=3).records), 3)

    def test_empty_or_stopword_only_prompt_is_silent_not_degraded(self):
        self.add("calendar sync")
        self.rebuild()
        for q in ("", "   ", "!!", "x"):   # x is len 1 -> dropped -> no terms
            res = self.lookup(q)
            self.assertEqual(res.records, [])
            self.assertTrue(res.available)   # nothing to look up != degraded


class FastPathOnlyTests(ScentLookupTestCase):
    def test_fts5_absent_returns_unavailable_no_scan(self):
        self.add("calendar sync decided")
        self.rebuild()
        original = index.fts5_available
        index.fts5_available = lambda *a, **k: False
        try:
            res = self.lookup("calendar")
            self.assertFalse(res.available)          # the ONE degraded-latency condition
            self.assertEqual(res.records, [])        # never the slow scan
        finally:
            index.fts5_available = original

    def test_missing_index_is_silent_available(self):
        self.add("calendar sync decided")  # ledger written, index NOT built
        res = self.lookup("calendar")
        self.assertEqual(res.records, [])
        self.assertTrue(res.available)               # no index yet -> silent, not a slower-mode notice

    def test_stale_generation_is_silent(self):
        a = self.add("calendar sync decided")
        self.rebuild()
        self.assertIn(a, self.ids(self.lookup("calendar")))
        # Bump the ledger generation out from under the index (a compaction would) -> the index is stale.
        ledger.bump_generation(for_path=self.ledger)
        res = self.lookup("calendar")
        self.assertEqual(res.records, [])            # stale -> silent (never a stale answer, never a scan)
        self.assertTrue(res.available)

    def test_corrupt_index_is_silent(self):
        self.add("calendar sync decided")
        self.rebuild()
        with open(self.index, "wb") as fh:
            fh.write(b"this is not a sqlite database at all")
        res = self.lookup("calendar")
        self.assertEqual(res.records, [])
        self.assertTrue(res.available)


class NoUsagePassTests(ScentLookupTestCase):
    def test_source_has_no_usage_or_write_calls(self):
        # The latency + side-effect guard: the relevance-only path must not reach the O(ledger) usage index,
        # the live-records pass, or any ledger write.
        src = inspect.getsource(index.scent_lookup) + inspect.getsource(index._salient_terms)
        self.assertNotIn("_access_index", src)
        self.assertNotIn("live_records", src)
        self.assertNotIn("record_access", src)
        self.assertNotIn("ledger.append", src)

    def test_no_oledger_pass_behaviorally(self):
        # The latency invariant enforced BEHAVIORALLY, not just by a source name-grep: the fast lookup must
        # never walk the whole ledger. Booby-trap EVERY O(ledger) entry point to explode; scent_lookup must
        # still answer over the built FTS5 index (it reads the index + the generation sidecar, never the ledger).
        a = self.add("calendar sync decided", role="decision")
        self.rebuild()

        def boom(*a, **k):
            raise AssertionError("scent_lookup walked the ledger (an O(ledger) pass on the hot path)")

        with mock.patch.object(forget, "_access_index", side_effect=boom), \
             mock.patch.object(forget, "live_records", side_effect=boom), \
             mock.patch.object(ledger, "iter_records", side_effect=boom):
            res = self.lookup("calendar")
        self.assertTrue(res.available)
        self.assertIn(a, self.ids(res))   # answered without touching any whole-ledger pass

    def test_writes_no_ledger_bytes(self):
        self.add("calendar sync decided")
        self.rebuild()
        before = os.stat(self.ledger)
        self.lookup("calendar")
        after = os.stat(self.ledger)
        self.assertEqual((before.st_size, before.st_mtime_ns), (after.st_size, after.st_mtime_ns))


if __name__ == "__main__":
    unittest.main()
