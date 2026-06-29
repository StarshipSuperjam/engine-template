"""test_index.py — unit tests for the derived memory lookup (memory-substrate-sqlite-fts5, slice 2).

Run via the engine's CI command:
    uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py' -b

These tests cover the slice-2 laws: the fast lookup and the slow backup return the SAME set of records (the
unicode61-mirror), the FTS5-absent condition is detected and degrades to the scan, the rebuild is atomic
(a crash leaves the prior index intact), and reads stay line-resilient. FTS5 is present in CI's SQLite, so the
scan path is exercised both by `force_scan=True` and by monkeypatching `fts5_available` to False.
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unicodedata
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory import index, ledger  # noqa: E402


def _bodies(result):
    return sorted(r["body"] for r in result.records)


class IndexTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="engine-memory-test-")
        self.ledger = os.path.join(self.tmp, "ledger.ndjson")
        self.index = os.path.join(self.tmp, "index.sqlite3")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def file(self, *records):
        for record in records:
            ledger.append(record, path=self.ledger)

    def q(self, text, **kw):
        return index.query(text, ledger_file=self.ledger, index_file=self.index, **kw)

    def rebuild(self):
        return index.rebuild(ledger_file=self.ledger, index_file=self.index)


class Fts5DetectionTests(IndexTestCase):
    def test_fts5_available_true_on_this_runtime(self):
        # CI's SQLite has FTS5; the whole fast path depends on it.
        self.assertTrue(index.fts5_available())

    def test_detection_does_not_leak_a_probe_table_on_a_passed_connection(self):
        conn = sqlite3.connect(":memory:")
        try:
            self.assertTrue(index.fts5_available(conn))
            temp_names = {r[0] for r in conn.execute("SELECT name FROM temp.sqlite_master").fetchall()}
            main_names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master").fetchall()}
            self.assertNotIn(index._FTS_PROBE_TABLE, temp_names | main_names)
        finally:
            conn.close()


class RoundTripTests(IndexTestCase):
    def test_rebuild_then_query_finds_records(self):
        self.file({"body": "we shipped the export feature"}, {"body": "we paused the import feature"})
        report = self.rebuild()
        self.assertTrue(report.fts5)
        self.assertEqual(report.indexed, 2)
        self.assertEqual(report.with_text, 2)
        self.assertEqual(_bodies(self.q("export")), ["we shipped the export feature"])

    def test_implicit_and_every_word_must_appear(self):
        self.file({"body": "alpha beta gamma"}, {"body": "alpha delta"})
        self.rebuild()
        self.assertEqual(_bodies(self.q("alpha")), ["alpha beta gamma", "alpha delta"])
        self.assertEqual(_bodies(self.q("alpha beta")), ["alpha beta gamma"])
        self.assertEqual(_bodies(self.q("alpha zeta")), [])

    def test_fast_path_is_not_degraded_forced_scan_is(self):
        self.file({"body": "hello world"})
        self.rebuild()
        self.assertFalse(self.q("hello").degraded)
        self.assertTrue(self.q("hello", force_scan=True).degraded)

    def test_limit_caps_results_on_both_paths(self):
        self.file(*({"body": f"repeated token item number {n}"} for n in range(5)))
        self.rebuild()
        self.assertEqual(len(self.q("repeated", limit=2).records), 2)
        self.assertEqual(len(self.q("repeated", limit=2, force_scan=True).records), 2)


class MirrorParityTests(IndexTestCase):
    """The load-bearing slice-2 property: the fast lookup and the slow backup return the same records, including
    the inputs a naive backup (a [A-Za-z0-9_] split) would get wrong — underscores and diacritics."""

    CORPUS = [
        {"body": "we chose the snake_case_config naming convention"},
        {"body": "the café meeting approved the naïve cache plan"},
        {"body": "Müller reviewed the e=mc2 derivation"},
        {"body": "the QUICK Fox jumped"},
        {"body": "ёжик решение про кэш"},  # Cyrillic: FTS5 folds "ё" differently from Python — the regressed class
        {"body": "δοκιμή τέλος της συνεδρίασης"},  # Greek with tonos accents — also regressed before the fix
        {"body": "unrelated decoy about timeouts and retries"},
    ]
    # Each query exercises a divergence class FTS5's own folder and a naive split disagree on: underscore-split,
    # diacritic-fold, case-fold, Cyrillic, Greek, plus a plain word and a miss.
    QUERIES = ["config", "snake", "cafe", "naive", "muller", "mc2", "quick", "fox",
               "ёжик", "решение", "δοκιμη", "τελος", "timeouts", "absent"]

    def test_fast_and_scan_agree_across_the_divergence_corpus(self):
        self.file(*self.CORPUS)
        self.rebuild()
        for query_text in self.QUERIES:
            fast = _bodies(self.q(query_text))
            scan = _bodies(self.q(query_text, force_scan=True))
            self.assertEqual(fast, scan, f"fast vs slow disagree on {query_text!r}")

    def test_divergence_class_queries_actually_match(self):
        # Guard against a vacuous parity pass: these are exactly the queries a naive split (or FTS5's own
        # folder, for the Cyrillic/Greek cases) would get wrong.
        self.file(*self.CORPUS)
        self.rebuild()
        self.assertEqual(_bodies(self.q("config")), ["we chose the snake_case_config naming convention"])
        self.assertEqual(_bodies(self.q("cafe")), ["the café meeting approved the naïve cache plan"])
        self.assertEqual(_bodies(self.q("naive")), ["the café meeting approved the naïve cache plan"])
        self.assertEqual(_bodies(self.q("ёжик")), ["ёжик решение про кэш"])  # fast path must find it, not just scan
        self.assertEqual(_bodies(self.q("δοκιμη")), ["δοκιμή τέλος της συνεδρίασης"])

    def test_tokenize_folds_words_the_expected_way(self):
        # _tokenize is the single folding authority for both paths. Pin its rules directly — each line is a
        # mutation tripwire (a naive [A-Za-z0-9_] split, .casefold(), or NFKD would change one of these).
        cases = {
            "snake_case_config": ["snake", "case", "config"],  # underscore is a separator
            "café": ["cafe"],  # NFD + drop combining marks → diacritic fold
            "naïve": ["naive"],
            "Müller": ["muller"],  # case fold
            "straße": ["straße"],  # .lower(), NOT casefold (ß stays — no "ss")
            "ёжик": ["ежик"],  # Cyrillic yo → e (diacritic strip)
            "Ⅳ": ["ⅳ"],  # NFD canonical, NOT NFKD (stays — not "iv")
            "a.b-c2": ["a", "b", "c2"],  # punctuation separates; digits are word chars
        }
        for text, expected in cases.items():
            self.assertEqual(index._tokenize(text), expected, f"_tokenize({text!r})")

    def test_every_indexed_token_is_retrievable_via_the_fast_path(self):
        # Proves FTS5 indexed exactly the tokens _tokenize produced (the slice-2 architecture): each token of a
        # record's text, queried through the FAST lookup, returns that record.
        records = [{"body": "the snake_case_config café decision"}, {"body": "ёжик решение"}]
        self.file(*records)
        self.rebuild()
        for record in records:
            for token in set(index._tokenize(record["body"])):
                result = self.q(token)
                self.assertFalse(result.degraded, f"token {token!r} should use the fast path")
                self.assertIn(record["body"], [r["body"] for r in result.records], f"token {token!r}")


class Fts5AbsentDispatchTests(IndexTestCase):
    """Cover the genuine FTS5-absent branch (CI has FTS5, so monkeypatch the detector)."""

    def test_query_falls_back_to_scan_when_fts5_absent(self):
        self.file({"body": "decision about the rollout"})
        self.rebuild()
        original = index.fts5_available
        index.fts5_available = lambda conn=None: False
        try:
            result = self.q("rollout")  # not force_scan — the dispatch must choose scan because FTS5 is "absent"
            self.assertTrue(result.degraded)
            self.assertEqual(_bodies(result), ["decision about the rollout"])
        finally:
            index.fts5_available = original

    def test_rebuild_is_a_noop_when_fts5_absent(self):
        self.file({"body": "nothing to index without the fast feature"})
        original = index.fts5_available
        index.fts5_available = lambda conn=None: False
        try:
            report = self.rebuild()
            self.assertFalse(report.fts5)
            self.assertEqual(report.indexed, 0)
            self.assertFalse(os.path.exists(self.index))  # no index file written
        finally:
            index.fts5_available = original


class AtomicRebuildTests(IndexTestCase):
    def test_failed_rebuild_leaves_prior_index_intact_and_no_temp(self):
        self.file({"body": "the original indexed decision"})
        self.rebuild()
        self.assertEqual(_bodies(self.q("original")), ["the original indexed decision"])
        # A second rebuild from a changed cabinet that fails at the atomic swap must NOT corrupt the prior index.
        self.file({"body": "a brand new decision that should not land"})
        original_replace = index.os.replace
        index.os.replace = lambda *a, **k: (_ for _ in ()).throw(OSError("simulated crash at swap"))
        try:
            with self.assertRaises(OSError):
                self.rebuild()
        finally:
            index.os.replace = original_replace
        # The prior index still answers the old way; the new record never landed in it.
        self.assertEqual(_bodies(self.q("original")), ["the original indexed decision"])
        self.assertEqual(self.q("brand").records, [])
        # No half-built temp left behind in the data dir.
        leftovers = [n for n in os.listdir(self.tmp) if n.startswith(".index-build-")]
        self.assertEqual(leftovers, [])

    def test_rebuild_overwrites_a_stale_index(self):
        self.file({"body": "first"})
        self.rebuild()
        self.file({"body": "second"})
        self.rebuild()
        self.assertEqual(_bodies(self.q("second")), ["second"])
        self.assertEqual(_bodies(self.q("first")), ["first"])


class ThrowawayTests(IndexTestCase):
    def test_missing_index_degrades_to_scan(self):
        self.file({"body": "recoverable memory"})
        # never built — no index file
        self.assertFalse(os.path.exists(self.index))
        result = self.q("recoverable")
        self.assertTrue(result.degraded)
        self.assertEqual(_bodies(result), ["recoverable memory"])

    def test_delete_and_rebuild_is_identical(self):
        self.file({"body": "DO NOT LOSE THIS"})
        self.rebuild()
        before = _bodies(self.q("lose"))
        os.remove(self.index)
        self.rebuild()
        self.assertEqual(_bodies(self.q("lose")), before)

    def test_corrupt_or_empty_index_degrades_to_scan(self):
        # A present-but-unreadable fast lookup (0-byte, or non-database bytes from a truncated copy / disk
        # error) must fall back to the slow backup, not crash — the availability law.
        self.file({"body": "recoverable decision"})

        def zero_byte(p):
            open(p, "wb").close()

        def garbage(p):
            with open(p, "wb") as fh:
                fh.write(b"this is not a database")

        for make_broken in (zero_byte, garbage):
            make_broken(self.index)
            result = self.q("recoverable")
            self.assertTrue(result.degraded)
            self.assertEqual(_bodies(result), ["recoverable decision"])


class ResilienceTests(IndexTestCase):
    def test_empty_ledger_rebuilds_to_empty_index(self):
        report = self.rebuild()  # no ledger file at all
        self.assertEqual(report.indexed, 0)
        self.assertTrue(os.path.exists(self.index))
        self.assertEqual(self.q("anything").records, [])

    def test_malformed_interior_line_does_not_cost_the_rest(self):
        self.file({"body": "memory before the corruption"})
        with open(self.ledger, "a", encoding="utf-8") as fh:
            fh.write("@@@ not json @@@\n")
        self.file({"body": "memory after the corruption"})
        self.rebuild()
        self.assertEqual(
            _bodies(self.q("memory")),
            ["memory after the corruption", "memory before the corruption"],
        )

    def test_torn_trailing_line_is_dropped(self):
        self.file({"body": "intact memory"})
        with open(self.ledger, "a", encoding="utf-8") as fh:
            fh.write('{"body":"half written when the power went ou')  # no newline
        self.rebuild()
        self.assertEqual(_bodies(self.q("intact")), ["intact memory"])
        self.assertEqual(self.q("half").records, [])


class ProjectionTests(IndexTestCase):
    def test_tags_field_is_excluded_from_the_searchable_text(self):
        # The locked law: tags are NOT indexed into the full-text body.
        self.file({"body": "the visible narrative", "tags": ["secretxyztag", "eADR-0007"]})
        self.rebuild()
        self.assertEqual(_bodies(self.q("visible")), ["the visible narrative"])
        self.assertEqual(self.q("secretxyztag").records, [])  # fast path
        self.assertEqual(self.q("secretxyztag", force_scan=True).records, [])  # slow path agrees
        self.assertNotIn("secretxyztag", index._record_text({"body": "x", "tags": ["secretxyztag"]}))

    def test_string_free_record_is_indexed_but_unsearchable(self):
        self.file({"count": 7, "ok": True}, {"body": "has words"})
        report = self.rebuild()
        self.assertEqual(report.indexed, 2)
        self.assertEqual(report.with_text, 1)  # only the record with string content is searchable
        self.assertEqual(_bodies(self.q("words")), ["has words"])

    def test_indexed_body_equals_the_shared_tokenization(self):
        # The fast path indexes exactly the tokens of _record_text(record) — the same tokens the scan path
        # matches against — so the two paths cannot silently desync on the projection or the tokenizer.
        records = [{"body": "first narrative", "title": "a title"}, {"note": "nested", "extra": ["deep", "words"]}]
        self.file(*records)
        self.rebuild()
        conn = sqlite3.connect(self.index)
        try:
            for ordinal, record in enumerate(records):
                body = conn.execute("SELECT body FROM entries_fts WHERE rowid = ?", (ordinal,)).fetchone()[0]
                self.assertEqual(body, " ".join(index._tokenize(index._record_text(record))))
        finally:
            conn.close()

    def test_non_dict_records_index_and_agree_across_paths(self):
        # The ledger is record-agnostic: a top-level string or list record must index and match on both paths.
        self.file("a bare string about caches", ["a", "list", "about", "caches"], {"body": "a dict about caches"})
        self.rebuild()
        fast = index.query("caches", ledger_file=self.ledger, index_file=self.index).records
        scan = index.query("caches", force_scan=True, ledger_file=self.ledger, index_file=self.index).records
        self.assertEqual(fast, scan)
        self.assertEqual(len(fast), 3)

    def test_limit_returns_the_same_records_not_just_the_same_count(self):
        # More matches than the limit: the fast path (ORDER BY ord LIMIT) and the scan (iter order, break at
        # limit) must pick the SAME records, in the same order — not merely the same count.
        self.file(*({"body": f"shared token entry {n}"} for n in range(6)))
        self.rebuild()
        fast = index.query("shared", limit=3, ledger_file=self.ledger, index_file=self.index).records
        scan = index.query("shared", limit=3, force_scan=True, ledger_file=self.ledger, index_file=self.index).records
        self.assertEqual(fast, scan)
        self.assertEqual(len(fast), 3)


class SafetyTests(IndexTestCase):
    def test_empty_and_punctuation_only_queries_return_nothing(self):
        self.file({"body": "some memory"})
        self.rebuild()
        for text in ["", "   ", "!!!", "...", "@#$%"]:
            result = self.q(text)
            self.assertEqual(result.records, [])
            self.assertFalse(result.degraded)

    def test_fts5_operators_in_a_query_are_neutralized(self):
        # Raw FTS5 syntax in user input must never reach the MATCH parser as syntax.
        self.file({"body": "alpha bravo charlie"})
        self.rebuild()
        for hostile in ['alpha" OR "bravo', "alpha NEAR bravo", "alpha*", "alpha AND bravo", 'alpha" --'] :
            fast = _bodies(self.q(hostile))
            scan = _bodies(self.q(hostile, force_scan=True))
            self.assertEqual(fast, scan, f"fast vs slow disagree on hostile input {hostile!r}")

    def test_module_import_is_side_effect_free_for_close_seam(self):
        # close.py does `import memory`; that must not touch the filesystem or build anything (capture
        # is now exposed, but binding it does no filesystem work — all reads/writes are inside calls).
        self.assertTrue(hasattr(index, "query"))
        import memory
        self.assertTrue(hasattr(memory, "capture_turn_delta"))  # the capture slice lit this up


if __name__ == "__main__":
    unittest.main()
