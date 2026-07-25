"""test_recall.py — unit tests for the transcript-window reader.

Run via the engine's CI command:
    uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py' -b

Four properties carry the weight, because a window is presented to a model (and an operator) as "what was
actually said": (1) CONVERSATION FIDELITY — turns come back in the order they happened, a >4KB message split
across records is rejoined whole, and one session's words never leak into another's; (2) THE GENUINE-TURN
FILTER — a harness-injected pseudo-turn is never shown as the operator's own words; (3) READ-ONLY — a window
appends nothing, so reading memory cannot change it; (4) THE LEAK GUARD — a throwaway path that resolves to
the live store is refused loudly, because this reader's output is verbatim conversation and a demo's stdout
can be a public log. Legacy-record tolerance is pinned too: real ledgers hold turn-deltas missing envelope
fields, and a window must skip them rather than crash.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # .engine/tools on path
import quiet_call  # noqa: E402  (capture a demo walkthrough's stdout so it can't bury the suite summary)
from memory import ledger, recall, records  # noqa: E402


def _rec(session_id, seq, speaker, text, *, injected=False, kind=None, **extra):
    tags = ["transcript", "stop"] + ([records.INJECTED_TAG] if injected else [])
    out = {"v": 1, "kind": kind or records.AMBIENT_CAPTURE_KIND,
           records.RECORD_ID_KEY: records.new_record_id(), "session_id": session_id,
           "ts": 1, "seq": seq, "speaker": speaker, "text": text, "tags": tags}
    out.update(extra)
    return out


class _CabinetBase(unittest.TestCase):
    """Every test writes to a THROWAWAY cabinet — never the real ledger."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="engine-recall-test-")
        self.cabinet = os.path.join(self._tmp.name, "ledger.ndjson")
        self.addCleanup(self._tmp.cleanup)

    def _write(self, *recs):
        for r in recs:
            ledger.append(r, path=self.cabinet)


class WindowFidelityTests(_CabinetBase):
    def test_turns_come_back_in_conversation_order(self):
        # Written out of order on purpose: `seq` is the authority, not append position.
        self._write(_rec("s1", 2, "user", "third"),
                    _rec("s1", 0, "user", "first"),
                    _rec("s1", 1, "assistant", "second"))
        texts = [t["text"] for t in recall.window("s1", path=self.cabinet)["turns"]]
        self.assertEqual(texts, ["first", "second", "third"])

    def test_a_split_message_is_rejoined_whole(self):
        # Capture splits a >4KB message into records sharing ONE seq; they must come back as one turn.
        self._write(_rec("s1", 0, "user", "part-one "),
                    _rec("s1", 0, "user", "part-two "),
                    _rec("s1", 0, "user", "part-three"))
        turns = recall.window("s1", path=self.cabinet)["turns"]
        self.assertEqual(len(turns), 1, "the chunks of one message must present as ONE turn")
        self.assertEqual(turns[0]["text"], "part-one part-two part-three")
        self.assertEqual(turns[0]["chunks"], 3)

    def test_same_seq_different_speaker_is_not_merged(self):
        # A defensive boundary: only chunks of the SAME message (same seq AND speaker) concatenate.
        self._write(_rec("s1", 0, "user", "asked"), _rec("s1", 0, "assistant", "answered"))
        turns = recall.window("s1", path=self.cabinet)["turns"]
        self.assertEqual([t["speaker"] for t in turns], ["user", "assistant"])

    def test_another_session_never_leaks_in(self):
        self._write(_rec("s1", 0, "user", "mine"), _rec("s2", 0, "user", "theirs"))
        texts = [t["text"] for t in recall.window("s1", path=self.cabinet)["turns"]]
        self.assertEqual(texts, ["mine"])

    def test_unknown_session_is_quietly_empty(self):
        self._write(_rec("s1", 0, "user", "mine"))
        result = recall.window("nope", path=self.cabinet)
        self.assertEqual(result["turns"], [])
        self.assertEqual(result["note"], "", "no completeness note when there is nothing to caveat")

    def test_blank_session_id_returns_nothing(self):
        self._write(_rec("s1", 0, "user", "mine"))
        self.assertEqual(recall.session_turns("", path=self.cabinet), [])
        self.assertEqual(recall.session_turns(None, path=self.cabinet), [])


class GenuineTurnFilterTests(_CabinetBase):
    def test_injected_pseudo_turn_is_never_shown_as_the_operators_words(self):
        # The load-bearing filter: a /compact continuation summary or task-notification is machine
        # scaffolding. Showing it as conversation would misattribute it to the operator.
        self._write(_rec("s1", 0, "user", "real words"),
                    _rec("s1", 1, "user", "scaffolding", injected=True))
        texts = [t["text"] for t in recall.window("s1", path=self.cabinet)["turns"]]
        self.assertEqual(texts, ["real words"])

    def test_non_turn_delta_records_are_ignored(self):
        # The ledger is shared: episodics, gists and markers live beside raw turns and are not conversation.
        self._write(_rec("s1", 0, "user", "real words"),
                    _rec("s1", 1, "user", "a summary", kind="episodic"))
        texts = [t["text"] for t in recall.window("s1", path=self.cabinet)["turns"]]
        self.assertEqual(texts, ["real words"])

    def test_is_genuine_turn_rejects_non_dicts(self):
        self.assertFalse(recall.is_genuine_turn(None))
        self.assertFalse(recall.is_genuine_turn("a string"))


class LegacyToleranceTests(_CabinetBase):
    def test_a_malformed_legacy_record_is_skipped_not_crashed(self):
        # The real store holds a turn-delta with no id/session_id/seq (an old demo run). Skipping it must
        # never cost the records after it.
        self._write({"v": 1, "kind": records.AMBIENT_CAPTURE_KIND, "text": "orphan with no session"},
                    _rec("s1", 0, "user", "good record"))
        texts = [t["text"] for t in recall.window("s1", path=self.cabinet)["turns"]]
        self.assertEqual(texts, ["good record"])

    def test_missing_seq_and_speaker_do_not_crash(self):
        self._write({"v": 1, "kind": records.AMBIENT_CAPTURE_KIND, "session_id": "s1", "text": "bare"})
        turns = recall.window("s1", path=self.cabinet)["turns"]
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["speaker"], "unknown")


class WindowingTests(_CabinetBase):
    def _many(self, n):
        self._write(*[_rec("s1", i, "user", f"turn-{i}") for i in range(n)])

    def test_anchor_centres_the_window_on_the_hit(self):
        self._many(20)
        turns = recall.window("s1", anchor_seq=10, radius=2, path=self.cabinet)["turns"]
        self.assertEqual([t["text"] for t in turns],
                         ["turn-8", "turn-9", "turn-10", "turn-11", "turn-12"])

    def test_anchor_near_the_start_does_not_underflow(self):
        self._many(10)
        turns = recall.window("s1", anchor_seq=0, radius=3, path=self.cabinet)["turns"]
        self.assertEqual(turns[0]["text"], "turn-0", "a window at the start must not wrap or crash")

    def test_max_turns_caps_a_long_session_and_says_so(self):
        self._many(60)
        result = recall.window("s1", max_turns=5, path=self.cabinet)
        self.assertEqual(result["returned"], 5)
        self.assertEqual(result["total"], 60)
        self.assertTrue(result["truncated"], "a truncated window must report that it was truncated")

    def test_completeness_note_rides_a_non_empty_window(self):
        # The honest-degradation claim: chunk completeness is NOT provable, so the window says so rather
        # than implying verbatim fidelity it cannot verify.
        self._many(2)
        self.assertIn("permanently erased", recall.window("s1", path=self.cabinet)["note"])


class ReadOnlyTests(_CabinetBase):
    def test_reading_a_window_appends_nothing(self):
        # eADR-0038: a search writes nothing on a read. The same must hold for a window.
        self._write(*[_rec("s1", i, "user", f"turn-{i}") for i in range(3)])
        before = open(self.cabinet, "rb").read()
        recall.window("s1", path=self.cabinet)
        recall.session_turns("s1", path=self.cabinet)
        self.assertEqual(open(self.cabinet, "rb").read(), before,
                         "reading a transcript window must not mutate the ledger")

    def test_module_source_contains_no_ledger_write(self):
        # A source-scan invariant (the pattern test_search.py uses): the reader must never gain a write.
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "recall.py")).read()
        body = src.split("# --- Operator demonstration")[0]     # the demo legitimately seeds a cabinet
        for forbidden in ("ledger.append", "record_access", "replace_ledger"):
            self.assertNotIn(forbidden, body, f"the reader must not call {forbidden}")


class LeakGuardTests(unittest.TestCase):
    def test_refuses_the_live_store(self):
        with self.assertRaises(SystemExit):
            recall.assert_not_live_store(ledger.ledger_path())

    def test_allows_a_throwaway_path(self):
        recall.assert_not_live_store("/tmp/definitely-not-the-live-ledger.ndjson")  # no raise


class DemoTests(unittest.TestCase):
    def test_demo_passes(self):
        self.assertEqual(quiet_call.run(recall._demo), 0)

    def test_demo_can_fail(self):
        # Prove the demo is a real falsification, not a happy-path showcase: break the genuine-turn filter
        # so injected scaffolding leaks into the window, and the demo must exit non-zero.
        import unittest.mock as mock
        with mock.patch.object(recall, "is_genuine_turn",
                               lambda r: isinstance(r, dict) and r.get("kind") == records.AMBIENT_CAPTURE_KIND):
            self.assertEqual(quiet_call.run(recall._demo), 1)


if __name__ == "__main__":
    unittest.main()
