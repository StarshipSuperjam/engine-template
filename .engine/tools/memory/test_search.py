"""test_search.py — unit tests for ranked, filtered recall: index.search (memory-substrate-sqlite-fts5, slice 5).

Run via the engine's CI command:
    uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py' -b

Covers the slice-5 `search` laws (the search.json contract): results come back BEST-FIRST by lexical relevance
(bm25 on the fast path) with usage (frecency) breaking near-ties but NEVER overriding a clearly-stronger match
("BM25 leads"); a never-accessed match is deprioritized, never dropped (ranking, not retention); the role/tag
filters narrow; the fast and slow paths return the same SET (the availability law; the slow path ranks the FULL
matched set before slicing, not an early ledger-order truncation); and `search` is side-effect-free (it never
reinforces — that is the MCP server's job — and never writes the ledger). `query` (slice 2) stays UNRANKED.
"""

import inspect
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory import forget, index, ledger, records, score  # noqa: E402

_ID = records.RECORD_ID_KEY


class SearchTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="engine-memory-search-")
        self.ledger = os.path.join(self.tmp, "ledger.ndjson")
        self.index = os.path.join(self.tmp, "index.sqlite3")
        self.now = int(time.time())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add(self, text, *, role="observation", tags=(), ts=None):
        rid = records.new_record_id()
        ledger.append({_ID: rid, "ts": self.now if ts is None else ts, "role": role,
                       "tags": list(tags), "text": text}, path=self.ledger)
        return rid

    def rebuild(self):
        return index.rebuild(ledger_file=self.ledger, index_file=self.index)

    def search(self, text, **kw):
        return index.search(text, ledger_file=self.ledger, index_file=self.index, **kw)

    def reinforce(self, rid, times=1):
        for _ in range(times):
            forget.record_access(rid, path=self.ledger)

    def ids(self, result):
        return [r.get(_ID) for r in result.records]


class RankingTests(SearchTestCase):
    def _discriminative_corpus(self):
        # "export" is RARE (two records) so bm25's IDF is high and tf separates the strong from the weak match.
        strong = self.add("the export format and export schedule and export owner were decided", role="decision")
        weak = self.add("a passing note that export came up once")
        for t in ("onboarding copy stays short", "the nightly job rebuilds the cache",
                  "prefer dark mode everywhere", "the meeting moved to friday",
                  "snake_case for config names", "retries capped at three"):
            self.add(t)
        self.rebuild()
        return strong, weak

    def test_bm25_orders_best_match_first(self):
        strong, weak = self._discriminative_corpus()
        order = self.ids(self.search("export"))
        self.assertEqual(order[0], strong)
        self.assertIn(weak, order)

    def test_bm25_leads_a_reinforced_weak_match_does_not_overtake(self):
        # The B2 guard: hammering the weaker match with usage must NOT push it past a clearly-stronger match.
        strong, weak = self._discriminative_corpus()
        self.reinforce(weak, times=50)
        self.rebuild()
        self.assertEqual(self.ids(self.search("export"))[0], strong)

    def test_usage_breaks_a_near_tie(self):
        # Two identical-text matches (same bm25 → same relevance bucket) reorder by usage.
        a = self.add("the field almanac lists the frost dates")
        b = self.add("the field almanac lists the frost dates")
        self.rebuild()
        self.reinforce(b, times=5)
        self.rebuild()
        self.assertEqual(self.ids(self.search("almanac"))[0], b)

    def test_never_accessed_match_is_still_returned_just_lower(self):
        a = self.add("the field almanac lists the frost dates")
        b = self.add("the field almanac lists the frost dates")
        self.rebuild()
        self.reinforce(b, times=5)
        self.rebuild()
        got = set(self.ids(self.search("almanac")))
        self.assertEqual(got, {a, b})   # the un-accessed one is deprioritized, never dropped

    def test_limit_caps_the_top_k_by_ranking(self):
        strong, weak = self._discriminative_corpus()
        result = self.search("export", limit=1)
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.records[0].get(_ID), strong)   # the top-k, not an arbitrary slice

    def test_score_field_is_the_lexical_relevance(self):
        self._discriminative_corpus()
        result = self.search("export")
        for r in result.records:
            self.assertIn(records.SCORE_KEY, r)
            self.assertIsInstance(r[records.SCORE_KEY], float)
            self.assertGreaterEqual(r[records.SCORE_KEY], 0.0)
        # The primary ordering is by relevance bucket (best first): the first result's bucket is <= the last's.
        buckets = [round(-r[records.SCORE_KEY], index._REL_DECIMALS) for r in result.records]
        self.assertEqual(buckets, sorted(buckets))

    def test_empty_query_returns_empty_not_degraded(self):
        self.add("anything at all")
        self.rebuild()
        for q in ("", "   ", "!!!"):
            res = self.search(q)
            self.assertEqual(res.records, [])
            self.assertFalse(res.degraded)


class FilterTests(SearchTestCase):
    def test_unknown_role_is_rejected(self):
        self.add("a decision about export", role="decision")
        self.rebuild()
        with self.assertRaises(ValueError):
            self.search("export", roles=["banana"])

    def test_role_filter_narrows_to_the_vocabulary(self):
        d = self.add("we decided to ship export", role="decision")
        self.add("a lesson about export", role="lesson")
        self.rebuild()
        only = self.search("export", roles=["decision"])
        self.assertEqual(self.ids(only), [d])
        self.assertEqual(len(self.search("export").records), 2)   # roles=None -> all

    def test_role_accept_set_equals_score_role_weights(self):
        # Mutation tripwire: every vocabulary role is accepted, and the accept-set is exactly score.ROLE_WEIGHTS.
        for role in score.ROLE_WEIGHTS:
            self.assertEqual(index._validate_roles([role]), {role})
        self.assertEqual(index._validate_roles(list(score.ROLE_WEIGHTS)), set(score.ROLE_WEIGHTS))
        with self.assertRaises(ValueError):
            index._validate_roles(["definitely-not-a-role"])

    def test_tag_any_match(self):
        a = self.add("export plans", tags=["eADR-0007", "release"])
        self.add("export musings", tags=["scratch"])
        self.add("export with no tags")
        self.rebuild()
        got = set(self.ids(self.search("export", tags=["eADR-0007"])))
        self.assertEqual(got, {a})
        # any-match: a record sharing ANY requested tag passes
        self.assertIn(a, self.ids(self.search("export", tags=["release", "nope"])))


class ParityAndDegradeTests(SearchTestCase):
    def test_fast_and_slow_agree_on_set(self):
        self.add("export one export two", role="decision")
        self.add("export three")
        self.add("unrelated note")
        self.rebuild()
        fast = self.search("export")
        slow = self.search("export", force_scan=True)
        self.assertFalse(fast.degraded)
        self.assertTrue(slow.degraded)
        self.assertEqual(set(self.ids(fast)), set(self.ids(slow)))   # same SET (order may differ)

    def test_slow_path_ranks_the_full_set_not_a_ledger_truncation(self):
        # The S1 guard: the weak match is FIRST in the ledger, the strong match LAST. With limit=1 the slow path
        # must rank the FULL set and return the STRONG match — a first-k ledger truncation would return the weak.
        weak = self.add("alpha mentioned once")
        for t in ("beta gamma", "delta epsilon", "zeta eta", "theta iota"):
            self.add(t)
        strong = self.add("alpha alpha alpha core")
        self.rebuild()
        result = self.search("alpha", limit=1, force_scan=True)
        self.assertTrue(result.degraded)
        self.assertEqual(self.ids(result), [strong])
        self.assertNotEqual(self.ids(result), [weak])

    def test_degraded_flag(self):
        self.add("export note")
        self.rebuild()
        self.assertFalse(self.search("export").degraded)
        self.assertTrue(self.search("export", force_scan=True).degraded)

    def test_fts5_absent_falls_back_and_still_ranks(self):
        strong = self.add("export export export decided", role="decision")
        self.add("export once")
        for t in ("alpha", "beta", "gamma", "delta"):
            self.add(t)
        self.rebuild()
        original = index.fts5_available
        index.fts5_available = lambda *a, **k: False
        try:
            result = self.search("export")
            self.assertTrue(result.degraded)
            self.assertEqual(self.ids(result)[0], strong)
        finally:
            index.fts5_available = original

    def test_corrupt_index_falls_back_to_ranked_scan(self):
        a = self.add("export plans here")
        self.add("nothing relevant")
        self.rebuild()
        with open(self.index, "wb") as fh:
            fh.write(b"this is not a sqlite database at all")
        result = self.search("export")
        self.assertTrue(result.degraded)
        self.assertIn(a, self.ids(result))


class NoSideEffectTests(SearchTestCase):
    def test_search_writes_no_ledger_bytes(self):
        self.add("export decision", role="decision")
        self.rebuild()
        before = os.stat(self.ledger)
        self.search("export")
        after = os.stat(self.ledger)
        self.assertEqual((before.st_size, before.st_mtime_ns), (after.st_size, after.st_mtime_ns))

    def test_search_source_has_no_write_calls(self):
        # A source-scan: the ranked recall path must not reach the reinforcement appender or a ledger write.
        src = "".join(inspect.getsource(fn) for fn in
                       (index.search, index._ranked, index._rank_slice_score, index._usage_of))
        self.assertNotIn("record_access", src)
        self.assertNotIn("ledger.append", src)


class QueryUnchangedTests(SearchTestCase):
    def test_query_stays_ledger_order_and_carries_no_score(self):
        # `query` (slice 2) must not gain ranking or the score field — guards against accidental coupling.
        first = self.add("export alpha")
        second = self.add("export export export beta")
        self.rebuild()
        result = index.query("export", ledger_file=self.ledger, index_file=self.index)
        self.assertEqual([r.get(_ID) for r in result.records], [first, second])   # ledger order, not ranked
        for r in result.records:
            self.assertNotIn(records.SCORE_KEY, r)


if __name__ == "__main__":
    unittest.main()
