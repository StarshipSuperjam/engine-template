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

    def test_unknown_session_explains_why_it_is_empty(self):
        # An empty window must never read as bare silence: a caller has to be able to tell "wrong id" from
        # "this session holds nothing readable", instead of concluding memory does not hold the answer.
        self._write(_rec("s1", 0, "user", "mine"))
        result = recall.window("nope", path=self.cabinet)
        self.assertEqual(result["turns"], [])
        self.assertIn("No stored conversation", result["note"])
        self.assertNotIn("Reconstructed", result["note"], "no completeness caveat when nothing was returned")

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

    def test_widening_the_radius_never_pushes_the_anchor_out_of_its_own_window(self):
        # The failure this guards: the cap used to truncate FORWARD from the window's start, so a radius at or
        # above max_turns returned a plausible window that did not contain the hit it was centred on — and a
        # model following "widen if the answer isn't there" would conclude memory lacked the answer.
        self._many(500)
        for radius in (6, 20, 60, 100, 400):
            turns = recall.window("s1", anchor_seq=300, radius=radius, path=self.cabinet)["turns"]
            seqs = [t["seq"] for t in turns]
            self.assertIn(300, seqs, f"the anchor fell out of its own window at radius={radius}")

    def test_anchor_past_the_end_does_not_crash(self):
        self._many(10)
        turns = recall.window("s1", anchor_seq=9999, radius=3, path=self.cabinet)["turns"]
        self.assertTrue(turns, "an anchor beyond the last turn should still return the tail, not nothing")

    def test_max_turns_caps_a_long_session_and_says_so(self):
        self._many(60)
        result = recall.window("s1", max_turns=5, path=self.cabinet)
        self.assertEqual(result["returned"], 5)
        self.assertEqual(result["total"], 60)
        self.assertTrue(result["truncated"], "a truncated window must report that it was truncated")

    def test_a_caller_cannot_raise_the_cap_without_limit(self):
        # Containment must be the implementation's, not the caller's: when a window misses, the one move
        # available is to raise the cap, so an unbounded cap turns a miss into a whole-session dump.
        self._many(400)
        result = recall.window("s1", max_turns=100_000, path=self.cabinet)
        self.assertEqual(result["returned"], recall.MAX_TURNS_CEILING)
        self.assertTrue(result["truncated"])

    def test_completeness_note_rides_a_non_empty_window(self):
        # The honest-degradation claim: chunk completeness is NOT provable, so the window says so rather
        # than implying verbatim fidelity it cannot verify.
        self._many(2)
        self.assertIn("permanently erased", recall.window("s1", path=self.cabinet)["note"])


class ClusterKeyResolutionTests(_CabinetBase):
    """A summary folded from several sessions carries a CLUSTER KEY, not a session, and its provenance is a
    list of RECORD ids. Neither exposed operation can look a record id up, and the episodes behind a completed
    roll-up are dropped from ranked recall — so without resolution here, a window on the OLDEST memories (the
    ones most likely to be folded) returns silence at exactly the moment a transcript is wanted."""

    def _folded(self):
        self._write(_rec("s-real", 0, "user", "the original conversation"),
                    {"v": 1, "kind": "episodic", records.RECORD_ID_KEY: "ep1", "session_id": "s-real",
                     "text": "an episode"},
                    {"v": 1, "kind": "gist", records.RECORD_ID_KEY: "g1", "session_id": "tag:topic",
                     "text": "a folded summary", records.SOURCE_IDS_KEY: ["ep1"]})

    def test_a_cluster_key_resolves_to_its_real_sessions(self):
        self._folded()
        self.assertEqual(recall.resolve_sessions("tag:topic", path=self.cabinet), ["s-real"])

    def test_a_window_on_a_cluster_key_returns_the_real_conversation(self):
        self._folded()
        result = recall.window("tag:topic", path=self.cabinet)
        self.assertEqual(result["sessions"], ["s-real"])
        self.assertEqual([t["text"] for t in result["turns"]], ["the original conversation"])

    def test_an_ordinary_session_id_resolves_to_itself(self):
        self.assertEqual(recall.resolve_sessions("s-real", path=self.cabinet), ["s-real"])

    def test_an_unresolvable_cluster_key_says_so_instead_of_going_silent(self):
        self._write(_rec("s-real", 0, "user", "hello"))
        result = recall.window("sim:orphan", path=self.cabinet)
        self.assertEqual(result["turns"], [])
        self.assertIn("cluster key", result["note"],
                      "an unresolvable cluster key must explain itself, not read as 'memory has nothing'")


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
