"""test_semantic.py — the guarantees meaning-based recall makes, and the ones it must never break.

Three groups. The tokenizer tests freeze behaviour that was verified against the reference implementation
over 3,012 strings of real captured conversation: if an edit changes what a word splits into, every stored
vector silently stops matching the table, so these pin exact ids rather than properties. The loader tests
prove a tampered table refuses loudly instead of returning plausible nonsense. The store tests prove the
erasure guarantees actually bite — each is written so that removing the guard it covers makes it fail.
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

_PARENT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from memory import ledger, records                       # noqa: E402
from memory.semantic import embed, store, wordpiece      # noqa: E402


class WordPieceTests(unittest.TestCase):
    """The split must match the table's publisher exactly, or every lookup is off by a row."""

    @classmethod
    def setUpClass(cls):
        cls.vocab = wordpiece.load_vocab(embed.VOCAB_FILE)

    def test_the_vocabulary_is_the_one_the_table_was_keyed_by(self):
        # The table has one row per token; a vocabulary of a different size means they are not a pair.
        self.assertEqual(len(self.vocab), 63091)
        self.assertEqual(embed.dimensions(), 512)

    def test_known_strings_split_exactly_as_the_reference_tokenizer_splits_them(self):
        """Frozen against the reference implementation. A change here silently invalidates every vector."""
        for text, expected in [
            ("the check that goes red", [1002, 3644, 1014, 2638, 1423]),
            ("eADR-0038", [18419, 12632, 17, 36228, 1626]),       # punctuation splits, so ids survive as pieces
            ("café NAÏVE", [6674, 14749]),                        # accents stripped, then lowercased
            ("__init__.py", [41, 41, 36350, 41, 41, 18, 36542]),
            ("C++ & C#", [45, 15, 15, 10, 45, 7]),
        ]:
            with self.subTest(text=text):
                self.assertEqual(wordpiece.encode(text, self.vocab), expected)

    def test_an_unknown_word_becomes_pieces_rather_than_nothing(self):
        """Project jargon is imprecise, never invisible — the property the whole capability leans on."""
        ids = wordpiece.encode("zzzqqxunknownword", self.vocab)
        self.assertGreater(len(ids), 1)
        self.assertNotIn(self.vocab[wordpiece.UNK_TOKEN], ids)

    def test_a_pathological_run_is_one_unknown_token_rather_than_a_quadratic_scan(self):
        ids = wordpiece.encode("a" * (wordpiece.MAX_WORD_CHARS + 1), self.vocab)
        self.assertEqual(ids, [self.vocab[wordpiece.UNK_TOKEN]])

    def test_whitespace_shape_does_not_change_the_split(self):
        """A tab-indented code block and the same line re-indented must tokenize alike."""
        self.assertEqual(wordpiece.encode("def  main( ):", self.vocab),
                         wordpiece.encode("def\tmain(\n):", self.vocab))

    def test_text_with_no_words_yields_no_tokens(self):
        self.assertEqual(wordpiece.encode("", self.vocab), [])
        self.assertEqual(wordpiece.encode("   \n\t ", self.vocab), [])


class TableIntegrityTests(unittest.TestCase):
    """A wrong table returns plausible nonsense, and nothing else in the engine could notice."""

    def setUp(self):
        self._cache = embed._CACHE
        embed._CACHE = None
        self.addCleanup(lambda: setattr(embed, "_CACHE", self._cache))

    def test_a_table_that_does_not_match_its_recorded_hash_is_refused_in_plain_words(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(embed.CHECKSUMS_FILE, encoding="utf-8") as fh:
            recorded = json.load(fh)
        recorded["files"][os.path.basename(embed.TABLE_FILE)]["sha256"] = "0" * 64
        forged = os.path.join(tmp, "checksums.json")
        with open(forged, "w", encoding="utf-8") as fh:
            json.dump(recorded, fh)
        real = embed.CHECKSUMS_FILE
        embed.CHECKSUMS_FILE = forged
        self.addCleanup(lambda: setattr(embed, "CHECKSUMS_FILE", real))

        with self.assertRaises(embed.TableUnavailable) as caught:
            embed._load()
        self.assertIn("does not match its recorded checksum", str(caught.exception))
        # available() must answer the same question without raising — it is a presence probe.
        self.assertFalse(embed.available())
        self.assertTrue(embed.unavailable_reason())

    def test_a_missing_table_says_which_file_is_missing(self):
        real = embed.TABLE_FILE
        embed.TABLE_FILE = os.path.join(tempfile.gettempdir(), "no-such-table.npz")
        self.addCleanup(lambda: setattr(embed, "TABLE_FILE", real))
        with self.assertRaises(embed.TableUnavailable) as caught:
            embed._load()
        self.assertIn("no-such-table.npz", str(caught.exception))


class EmbeddingTests(unittest.TestCase):
    def test_text_with_no_recognizable_words_is_a_zero_vector(self):
        """A zero vector scores zero against every question — absent, never spuriously close to everything."""
        import numpy

        self.assertEqual(float(numpy.linalg.norm(embed.embed("   "))), 0.0)

    def test_a_paraphrase_scores_far_above_unrelated_text(self):
        """The property the capability exists for, asserted as a margin rather than an absolute."""
        import numpy

        vectors = embed.embed_many(["should we use a cron job",
                                    "scheduled task that runs on a timer",
                                    "sourdough bread with rye flour"])
        paraphrase = float(numpy.dot(vectors[0], vectors[1]))
        unrelated = float(numpy.dot(vectors[0], vectors[2]))
        # A wide table's absolute cosines run lower than a narrow one's; the separation is what matters, and
        # it is asserted as a multiple so this cannot pass on two numbers that are merely both small.
        self.assertGreater(paraphrase, 0.15)
        self.assertGreater(paraphrase, unrelated * 3)


class PassageTests(unittest.TestCase):
    def test_a_record_is_split_on_sentence_boundaries(self):
        found = store.passages("First thing here. Second thing here. Third thing here.")
        self.assertTrue(found)
        self.assertTrue(all(len(p) <= store.PASSAGE_CHARS + 40 for p in found))

    def test_text_with_no_sentence_punctuation_is_still_reachable(self):
        """Returning nothing here would make the record permanently unfindable."""
        self.assertTrue(store.passages("a" * 900))

    def test_empty_text_yields_no_passages(self):
        self.assertEqual(store.passages("   "), [])

    def test_one_enormous_record_cannot_flood_the_store(self):
        self.assertLessEqual(len(store.passages("Sentence here. " * 500)), store.MAX_PASSAGES)


class _Cabinet(unittest.TestCase):
    """A throwaway store: a temp ledger and a temp vector file, never the operator's own."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.ledger = os.path.join(self.dir, "ledger.ndjson")
        self.vectors = os.path.join(self.dir, "vectors.sqlite3")

    def _write(self, *texts):
        for text in texts:
            ledger.append({records.RECORD_ID_KEY: records.new_record_id(), "ts": int(time.time()),
                           "role": "decision", "tags": [], "text": text}, path=self.ledger)

    def _search(self, query, **kw):
        return store.search(query, ledger_file=self.ledger, store_file=self.vectors, **kw)


class ErasureTests(_Cabinet):
    """A memory the operator removed must not be findable by meaning after its text is gone."""

    def test_a_removed_record_is_dropped_from_the_store_and_never_returned(self):
        self._write("We decided to keep the ledger append-only forever.",
                    "The onboarding copy should stay short and direct.")
        first = self._search("append only ledger")
        self.assertTrue(first["records"])

        # Remove one record from the ledger entirely, as an enacted erasure leaves it.
        kept = [line for line in open(self.ledger, encoding="utf-8").read().splitlines()
                if "append-only" not in line]
        with open(self.ledger, "w", encoding="utf-8") as fh:
            fh.write("\n".join(kept) + ("\n" if kept else ""))

        after = self._search("append only ledger")
        texts = " ".join(r.get("text", "") for r in after["records"])
        self.assertNotIn("append-only", texts)

        # And it is gone from the store itself, not merely filtered out of the answer.
        import sqlite3

        conn = sqlite3.connect(self.vectors)
        try:
            held = conn.execute("SELECT COUNT(DISTINCT record_id) FROM passages").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(held, 1)

    def test_reconciling_twice_embeds_nothing_the_second_time(self):
        self._write("A settled decision about the release cut.")
        first = store.sync(ledger_file=self.ledger, store_file=self.vectors)
        second = store.sync(ledger_file=self.ledger, store_file=self.vectors)
        self.assertEqual(first["embedded"], 1)
        self.assertEqual(second["embedded"], 0)


class StoreBehaviourTests(_Cabinet):
    def test_plainly_unrelated_text_is_not_offered_at_all(self):
        """The floor's whole job. It cuts obvious noise; it deliberately does not adjudicate relevance."""
        self._write("We chose NDJSON so the ledger degrades to plain git.")
        self.assertEqual(self._search("sourdough bread with rye flour")["records"], [])

    def test_a_paraphrase_sharing_no_words_with_the_record_is_still_found(self):
        """The case the capability exists for, and the one a higher floor would silently cut.

        Measured at 0.244 — below where an irrelevant near-miss can land on a large store, which is why the
        score is returned for the caller to weigh rather than used as a verdict here.
        """
        self._write("We ruled out a cron job and hooked the calendar instead.")
        found = self._search("did we consider running it on a timer")
        self.assertTrue(found["records"], "a zero-overlap paraphrase must survive the floor")
        self.assertIn("cron job", found["records"][0]["text"])

    def test_a_result_carries_the_passage_that_matched(self):
        """Without it a caller judges relevance from the record's opening, which need not be the hit."""
        self._write("Some unrelated preamble about scheduling. " * 3
                    + "We ruled out a cron job and hooked the calendar instead.")
        found = self._search("did we consider running it on a timer")
        self.assertTrue(found["records"], "expected a meaning-based hit")
        self.assertTrue(found["passages"][0])
        self.assertEqual(len(found["passages"]), len(found["records"]))

    def test_an_empty_store_reports_that_nothing_was_searched(self):
        """Zero searched is a different answer from nothing being close, and callers must tell them apart."""
        open(self.ledger, "a").close()
        found = self._search("anything at all")
        self.assertEqual(found["records"], [])
        self.assertEqual(found["searched"], 0)

    def test_a_different_word_table_discards_every_stored_vector(self):
        """Vectors from two tables are not comparable; mixing them degrades ranking with no error."""
        import sqlite3

        self._write("A decision worth recalling later.")
        store.sync(ledger_file=self.ledger, store_file=self.vectors)
        conn = sqlite3.connect(self.vectors)
        try:
            conn.execute("UPDATE meta SET table_fingerprint = 'a-different-table' WHERE rowid = 1")
            conn.commit()
        finally:
            conn.close()
        reopened = store._connect(self.vectors)
        try:
            remaining = reopened.execute("SELECT COUNT(*) FROM passages").fetchone()[0]
        finally:
            reopened.close()
        self.assertEqual(remaining, 0)


if __name__ == "__main__":
    unittest.main()
