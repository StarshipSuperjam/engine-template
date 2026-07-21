"""test_recall_benchmark.py — unit tests for the G2 memory-recall benchmark (construction-only).

Run via the engine's CI command:
    uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py' -b

Two properties carry the weight. (1) SCORER CORRECTNESS — the frozen grading law credits a session hit, a
record-level exact-wording hit, and a nothing-relevant emptiness; it resolves a cross-session gist through its
source_ids (else a real old-path hit scores a miss and understates the baseline that gates an irreversible
deletion); and it refuses a wrong label. (2) FROZEN-SET INTEGRITY — the committed corpus + questions are
well-formed, the structural invariants the classes depend on hold, the seal matches (tamper-evident freeze),
the recorded old-path baseline reproduces exactly, and the instrument DISCRIMINATES (the old lexical path
visibly fails the paraphrase / raw-only classes). This test imports the retiring harness, so it retires in the
SAME first-run pass (the reference-closure invariant).
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # .engine/tools on path
import recall_benchmark as rb  # noqa: E402
from memory import ledger, records  # noqa: E402


class ScorerTests(unittest.TestCase):
    """The frozen pure scorer — exercised with hand-built ranked lists (no retrieval), so a failure is the
    grading law, not the index."""

    def setUp(self):
        self.corpus = rb.load_corpus()
        self.id2s = {r[rb._ID]: r.get(rb._SESSION) for r in self.corpus}

    def _rec(self, rid, session_id, **extra):
        r = {rb._ID: rid, rb._SESSION: session_id}
        r.update(extra)
        return r

    def test_session_membership_hit(self):
        q = {"content_type": "plain", "expected_sessions": ["sX"]}
        ranked = [self._rec("a", "sOther"), self._rec("b", "sX")]
        self.assertTrue(rb.score_question(ranked, q, {}, k=5))

    def test_miss_when_expected_session_absent(self):
        q = {"content_type": "plain", "expected_sessions": ["sX"]}
        ranked = [self._rec("a", "sOther"), self._rec("b", "sAlso")]
        self.assertFalse(rb.score_question(ranked, q, {}, k=5))

    def test_beyond_k_is_a_miss(self):
        q = {"content_type": "plain", "expected_sessions": ["sX"]}
        ranked = [self._rec(str(i), "sOther") for i in range(5)] + [self._rec("hit", "sX")]
        self.assertFalse(rb.score_question(ranked, q, {}, k=5))  # the hit sits at rank 6

    def test_exact_wording_needs_the_record_not_the_session(self):
        # A different record from the right session must NOT satisfy an exact-wording question.
        q = {"content_type": "exact-wording", "expected_sessions": ["sX"], "expected_record_ids": ["theRec"]}
        neighbour = [self._rec("otherRec", "sX")]
        self.assertFalse(rb.score_question(neighbour, q, {}, k=5))
        exact = [self._rec("theRec", "sX")]
        self.assertTrue(rb.score_question(exact, q, {}, k=5))

    def test_nothing_relevant_is_pure_emptiness(self):
        q = {"content_type": "nothing-relevant", "expected_sessions": []}
        self.assertTrue(rb.score_question([], q, {}, k=5))
        self.assertFalse(rb.score_question([self._rec("a", "sAny")], q, {}, k=5))

    def test_cross_session_gist_resolves_through_source_ids(self):
        # A returned gist carries a sentinel session_id; its real sessions come from source_ids.
        id2s = {"src1": "sReal"}
        gist = self._rec("g", "tag:cluster", **{records.SOURCE_IDS_KEY: ["src1"]})
        q = {"content_type": "plain", "expected_sessions": ["sReal"]}
        self.assertTrue(rb.score_question([gist], q, id2s, k=5))
        # Without the resolution the sentinel would not match, proving the resolution is load-bearing.
        q_wrong = {"content_type": "plain", "expected_sessions": ["tag:cluster"]}
        self.assertFalse(rb.score_question([gist], q_wrong, id2s, k=5))

    def test_trace_sessions_normal_record(self):
        self.assertEqual(rb.trace_sessions(self._rec("a", "s1"), {}), {"s1"})


class FrozenSetWellFormednessTests(unittest.TestCase):
    """The committed corpus + questions must be structurally sound — a malformed label silently corrupts the
    baseline that gates the deletion."""

    @classmethod
    def setUpClass(cls):
        cls.corpus = rb.load_corpus()
        cls.questions = rb.load_questions()
        cls.by_id = {r[rb._ID]: r for r in cls.corpus}
        cls.sessions = {r.get(rb._SESSION) for r in cls.corpus}

    def test_meets_the_forty_known_answer_floor(self):
        known = [q for q in self.questions if q["content_type"] != "nothing-relevant"]
        self.assertGreaterEqual(len(known), 40, "the G2 bar requires >=40 known-answer questions")

    def test_vocabulary_is_roughly_balanced(self):
        known = [q for q in self.questions if q["content_type"] != "nothing-relevant"]
        para = sum(1 for q in known if q["vocab"] == "paraphrased")
        frac = para / len(known)
        self.assertTrue(0.35 <= frac <= 0.55, "paraphrase share should be roughly half (got %.2f)" % frac)

    def test_every_class_and_vocab_is_known(self):
        for q in self.questions:
            self.assertIn(q["content_type"], rb.CONTENT_TYPES, q["qid"])
            self.assertIn(q["vocab"], rb.VOCAB, q["qid"])
            self.assertTrue(q.get("answer_key"), "%s needs a plain-language answer_key" % q["qid"])

    def test_expected_sessions_and_records_exist(self):
        for q in self.questions:
            for sid in q.get("expected_sessions", []):
                self.assertIn(sid, self.sessions, "%s points at a missing session %s" % (q["qid"], sid))
            for rid in q.get("expected_record_ids", []):
                self.assertIn(rid, self.by_id, "%s points at a missing record %s" % (q["qid"], rid))
        # nothing-relevant carries no expected source.
        for q in self.questions:
            if q["content_type"] == "nothing-relevant":
                self.assertEqual(q.get("expected_sessions", []), [], q["qid"])

    def test_qids_unique(self):
        qids = [q["qid"] for q in self.questions]
        self.assertEqual(len(qids), len(set(qids)))

    def test_raw_only_sessions_carry_no_curated_record(self):
        # The invariant the raw-only class depends on: if the answer is raw-only, the answer's session must
        # hold NO episodic/gist — else the old path reaches the session via the curated record and the
        # 'old path cannot reach it' claim is false (feasibility/architecture plan-gate finding).
        curated_sessions = {r.get(rb._SESSION) for r in self.corpus
                            if r.get("kind") != records.AMBIENT_CAPTURE_KIND}
        for q in self.questions:
            if q.get("answer_locus") == "raw-only":
                for sid in q.get("expected_sessions", []):
                    self.assertNotIn(sid, curated_sessions,
                                     "%s is raw-only but session %s has a curated record" % (q["qid"], sid))

    def test_curated_answers_live_in_a_surfaced_record(self):
        # The faithfulness floor the mechanical test CAN check: a curated answer's session must contain at
        # least one recall-surfaced (episodic/gist) record. Semantic faithfulness of that summary to the raw
        # turns is confirmed by the maintainer's plain-language fairness sample, not mechanically.
        surfaced_sessions = {r.get(rb._SESSION) for r in self.corpus
                             if r.get("kind") != records.AMBIENT_CAPTURE_KIND}
        for q in self.questions:
            if q.get("answer_locus") == "curated":
                self.assertTrue(any(s in surfaced_sessions for s in q.get("expected_sessions", []))
                                or q.get("expected_record_ids"),
                                "%s is curated but no expected session has a surfaced record" % q["qid"])


class SealAndBaselineTests(unittest.TestCase):
    """The tamper-evident freeze + the reproducible baseline + the discrimination proof."""

    def test_seal_matches_the_committed_frozen_set(self):
        seal, problems = rb.verify_seal()
        self.assertIsNotNone(seal, "seal.json is missing")
        self.assertEqual(problems, [], "the frozen set was edited without a re-seal: %s" % problems)

    def test_baseline_reproduces_the_sealed_number(self):
        seal, _ = rb.verify_seal()
        summary, _rows = rb.run_synthetic()
        sealed = seal["old_path_baseline"]["overall_known"]["recall_at_k"]
        self.assertEqual(summary["overall_known"]["recall_at_k"], sealed,
                         "the old-path baseline did not reproduce the sealed value")

    def test_run_reproduces(self):
        # Reproducibility rests on the corpus being stamped RELATIVE to the current time at every run (records
        # born minutes ago), so none drifts across the archival boundary between runs — `index.search` reads
        # real `time.time()` internally, so a far-past absolute stamp cannot be injected (nor should be). Two
        # real-now runs are byte-for-byte identical.
        a, _ = rb.run_synthetic()
        b, _ = rb.run_synthetic()
        self.assertEqual(a, b)

    def test_instrument_discriminates(self):
        # If the old path already cleared the bar there would be nothing for the new path to beat — the
        # instrument could not justify the deletion it gates (product-intent plan-gate finding).
        summary, _ = rb.run_synthetic()
        self.assertTrue(rb.discrimination_gap_shows(summary),
                        "the old path does not visibly fail the hard classes — the instrument is toothless")
        self.assertEqual(summary["by_vocab"]["paraphrased"][0], 0,
                         "the old lexical path should catch no zero-overlap paraphrase")

    def test_bar_is_pinned_in_the_seal(self):
        seal, _ = rb.verify_seal()
        self.assertEqual(seal["bar"]["top5_threshold"], 0.90)
        self.assertIn("slice6_precondition", seal["bar"])


class LeakGuardTests(unittest.TestCase):
    def test_refuses_the_live_store(self):
        with self.assertRaises(SystemExit):
            rb._assert_not_live_store(ledger.ledger_path())

    def test_allows_a_throwaway_path(self):
        rb._assert_not_live_store("/tmp/definitely-not-the-live-ledger.ndjson")  # no raise


class DemoTests(unittest.TestCase):
    def test_demo_passes(self):
        self.assertEqual(rb._demo(), 0)


if __name__ == "__main__":
    unittest.main()
