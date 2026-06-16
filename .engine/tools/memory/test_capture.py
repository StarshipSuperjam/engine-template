"""Unit tests for memory.capture — ambient turn-delta capture (slice 3a).

Run via the engine test suite: `uv run --directory .engine --frozen -- python -m unittest discover -s
tools -p 'test_*.py'`. These exercise the REAL capture path against throwaway temp ledgers/transcripts;
ENGINE_MEMORY_DIR points the ledger at a temp dir and ENGINE_MEMORY_TRANSCRIPT_DIR allow-lists the temp
transcript so the path-safety gate does not reject the fixture.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # .engine/tools on path
from memory import capture, index, ledger  # noqa: E402


def _msg(role, text):
    """A Claude Code transcript message line (top-level `type` + nested `message.role/content`)."""
    return {"type": role, "message": {"role": role, "content": text}}


class CaptureTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="engine-capture-test-")
        self.mem = os.path.join(self.tmp, "mem")
        self._saved = {k: os.environ.get(k) for k in (
            "ENGINE_MEMORY_DIR", "ENGINE_MEMORY_TRANSCRIPT_DIR", "CLAUDE_SESSION_ID", "CLAUDE_TRANSCRIPT_PATH",
        )}
        os.environ["ENGINE_MEMORY_DIR"] = self.mem
        os.environ["ENGINE_MEMORY_TRANSCRIPT_DIR"] = self.tmp
        os.environ.pop("CLAUDE_SESSION_ID", None)
        os.environ.pop("CLAUDE_TRANSCRIPT_PATH", None)
        self.ledger = os.path.join(self.mem, "ledger.ndjson")
        self.data_dir = self.mem

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    # helpers -----------------------------------------------------------------
    def transcript(self, name, lines):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            for line in lines:
                fh.write(json.dumps(line) + "\n")
        return path

    def payload(self, transcript_path, session_id="sess-A"):
        return {"session_id": session_id, "transcript_path": transcript_path}

    def records(self):
        return ledger.read(path=self.ledger).records

    def texts(self):
        return [r.get("text") for r in self.records()]


class RoundTripTests(CaptureTestCase):
    def test_capture_files_one_record_per_message(self):
        t = self.transcript("s.jsonl", [
            _msg("user", "redesign the export to write a manifest first"),
            _msg("assistant", "added a manifest step and a configurable schedule"),
        ])
        n = capture.capture_turn_delta(self.payload(t))
        self.assertEqual(n, 2)
        recs = self.records()
        self.assertEqual([r["speaker"] for r in recs], ["user", "assistant"])
        self.assertEqual([r["kind"] for r in recs], ["turn-delta", "turn-delta"])

    def test_captured_notes_are_findable_by_their_words(self):
        t = self.transcript("s.jsonl", [_msg("user", "the login page logs people out after thirty minutes")])
        capture.capture_turn_delta(self.payload(t))
        hits = index.query("logs out").records  # no index built -> plain scan over the ledger
        self.assertTrue(any("login page" in r.get("text", "") for r in hits))

    def test_record_shape_and_version_envelope(self):
        t = self.transcript("s.jsonl", [_msg("user", "hello there")])
        capture.capture_turn_delta(self.payload(t, session_id="sess-XYZ"))
        rec = self.records()[0]
        self.assertEqual(rec["v"], capture.RECORD_VERSION)
        self.assertEqual(rec["kind"], "turn-delta")
        self.assertEqual(rec["session_id"], "sess-XYZ")
        self.assertEqual(rec["seq"], 0)
        self.assertEqual(rec["text"], "hello there")
        self.assertEqual(rec["tags"], ["transcript", "stop"])
        self.assertIsInstance(rec["ts"], int)   # integers stay out of the FTS body (see ProjectionTests)
        self.assertIsInstance(rec["seq"], int)


class CursorTests(CaptureTestCase):
    def test_recapture_over_the_same_finished_turns_adds_nothing(self):
        t = self.transcript("s.jsonl", [_msg("user", "alpha"), _msg("assistant", "bravo")])
        first = capture.capture_turn_delta(self.payload(t))
        before = self.records()
        second = capture.capture_turn_delta(self.payload(t))   # identical re-trigger
        after = self.records()
        self.assertEqual((first, second), (2, 0))
        self.assertEqual(before, after)   # not just same count — same records

    def test_only_the_new_delta_is_captured_when_the_transcript_grows(self):
        t = self.transcript("s.jsonl", [_msg("user", "first turn")])
        capture.capture_turn_delta(self.payload(t))
        # the session continues: a second turn lands in the same transcript
        self.transcript("s.jsonl", [_msg("user", "first turn"), _msg("assistant", "second turn")])
        n = capture.capture_turn_delta(self.payload(t))
        self.assertEqual(n, 1)
        self.assertEqual(self.texts(), ["first turn", "second turn"])

    def test_distinct_sessions_keep_distinct_cursors(self):
        ta = self.transcript("a.jsonl", [_msg("user", "from session A")])
        tb = self.transcript("b.jsonl", [_msg("user", "from session B")])
        capture.capture_turn_delta(self.payload(ta, session_id="A"))
        capture.capture_turn_delta(self.payload(tb, session_id="B"))
        self.assertEqual(sorted(self.texts()), ["from session A", "from session B"])

    def test_corrupt_cursor_file_is_treated_as_zero(self):
        t = self.transcript("s.jsonl", [_msg("user", "only turn")])
        capture.capture_turn_delta(self.payload(t))
        with open(os.path.join(self.data_dir, capture.CURSOR_FILENAME), "w", encoding="utf-8") as fh:
            fh.write("{not json at all")
        n = capture.capture_turn_delta(self.payload(t))   # cursor unreadable -> re-capture from 0
        self.assertEqual(n, 1)              # re-captured (duplicate-over-loss), did not crash
        self.assertEqual(self.texts(), ["only turn", "only turn"])

    def test_deleted_cursor_file_is_treated_as_zero(self):
        t = self.transcript("s.jsonl", [_msg("user", "only turn")])
        capture.capture_turn_delta(self.payload(t))
        os.remove(os.path.join(self.data_dir, capture.CURSOR_FILENAME))
        n = capture.capture_turn_delta(self.payload(t))
        self.assertEqual(n, 1)
        self.assertEqual(len(self.records()), 2)

    def test_cursor_is_monotonic_never_rewinds(self):
        t = self.transcript("s.jsonl", [_msg("user", "a"), _msg("assistant", "b")])
        capture.capture_turn_delta(self.payload(t))
        # force a smaller stored count, then recapture: a monotonic write must NOT lower it
        capture._write_cursor(self.data_dir, "sess-A", 1)
        with open(os.path.join(self.data_dir, capture.CURSOR_FILENAME), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["sess-A"], 2)


class FailSoftTests(CaptureTestCase):
    def test_non_dict_payload_is_a_noop(self):
        for bad in (None, "x", 5, [1, 2]):
            self.assertEqual(capture.capture_turn_delta(bad), 0)
        self.assertEqual(self.records(), [])

    def test_missing_transcript_path_is_a_noop(self):
        self.assertEqual(capture.capture_turn_delta({"session_id": "x"}), 0)

    def test_missing_session_id_is_a_noop(self):
        t = self.transcript("s.jsonl", [_msg("user", "hi")])
        self.assertEqual(capture.capture_turn_delta({"transcript_path": t}), 0)

    def test_absent_transcript_file_is_a_noop(self):
        path = os.path.join(self.tmp, "does-not-exist.jsonl")
        self.assertEqual(capture.capture_turn_delta(self.payload(path)), 0)

    def test_malformed_transcript_lines_are_skipped(self):
        path = os.path.join(self.tmp, "s.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(_msg("user", "before garbage")) + "\n")
            fh.write("@@@ not json @@@\n")
            fh.write(json.dumps(_msg("assistant", "after garbage")) + "\n")
        n = capture.capture_turn_delta(self.payload(path))
        self.assertEqual(n, 2)
        self.assertEqual(self.texts(), ["before garbage", "after garbage"])

    def test_lock_contention_is_a_clean_noop(self):
        import fcntl
        t = self.transcript("s.jsonl", [_msg("user", "contended turn")])
        os.makedirs(self.data_dir, exist_ok=True)
        held = os.open(os.path.join(self.data_dir, capture.LOCK_FILENAME), os.O_WRONLY | os.O_CREAT, 0o644)
        fcntl.flock(held, fcntl.LOCK_EX)
        saved = capture._LOCK_ATTEMPTS
        capture._LOCK_ATTEMPTS = 2  # keep the test fast; the bound is what matters, not the count
        try:
            n = capture.capture_turn_delta(self.payload(t))   # cannot get the lock -> gives up cleanly
        finally:
            capture._LOCK_ATTEMPTS = saved
            fcntl.flock(held, fcntl.LOCK_UN)
            os.close(held)
        self.assertEqual(n, 0)
        self.assertEqual(self.records(), [])
        # and once the lock is free, the same delta is caught
        self.assertEqual(capture.capture_turn_delta(self.payload(t)), 1)


class PathSafetyTests(CaptureTestCase):
    def test_traversal_path_is_rejected_even_when_it_resolves_in_scope(self):
        # a real, in-scope transcript reached via a '..' path: the suffix/root/exists checks would ACCEPT
        # it, so ONLY the raw-path '..' guard can reject it. This makes the test actually exercise the guard.
        self.transcript("real.jsonl", [_msg("user", "in scope but reached via dot-dot")])
        sneaky = os.path.join(self.tmp, "sub", "..", "real.jsonl")   # realpath -> <tmp>/real.jsonl (in scope)
        self.assertEqual(capture.capture_turn_delta(self.payload(sneaky)), 0)
        self.assertEqual(self.records(), [])   # nothing captured -> the '..' guard fired

    def test_wrong_suffix_is_rejected(self):
        path = os.path.join(self.tmp, "s.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(_msg("user", "hi")) + "\n")
        self.assertEqual(capture.capture_turn_delta(self.payload(path)), 0)

    def test_out_of_scope_path_is_rejected(self):
        other = tempfile.mkdtemp(prefix="engine-capture-outside-")
        try:
            path = os.path.join(other, "s.jsonl")   # NOT under ~/.claude, the clone root, or the env root
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(_msg("user", "hi")) + "\n")
            self.assertEqual(capture.capture_turn_delta(self.payload(path)), 0)
        finally:
            shutil.rmtree(other, ignore_errors=True)

    def test_oversized_transcript_is_rejected(self):
        t = self.transcript("s.jsonl", [_msg("user", "hi")])
        saved = capture.MAX_TRANSCRIPT_BYTES
        capture.MAX_TRANSCRIPT_BYTES = 1
        try:
            self.assertEqual(capture.capture_turn_delta(self.payload(t)), 0)
        finally:
            capture.MAX_TRANSCRIPT_BYTES = saved


class ContentTests(CaptureTestCase):
    def test_long_message_is_chunked_losslessly_never_elided(self):
        tokens = [f"tok{i}" for i in range(2000)]
        big = "\n".join(tokens)                       # ~ 14k chars, well over the 4k chunk cap
        self.assertGreater(len(big), capture.CHUNK_MAX_CHARS * 2)
        t = self.transcript("s.jsonl", [_msg("user", big)])
        n = capture.capture_turn_delta(self.payload(t))
        self.assertGreater(n, 1)                       # the one message became several records
        joined = " ".join(self.texts())
        for tok in ("tok0", "tok1000", "tok1999"):     # head, MIDDLE, and tail all survive — no elision
            self.assertIn(tok, joined)

    def test_assistant_list_content_blocks_are_joined(self):
        line = {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "first thought"},
            {"type": "tool_use", "name": "x", "input": {}},   # tool args are not text -> skipped
            {"type": "text", "text": "second thought"},
        ]}}
        capture.capture_turn_delta(self.payload(self.transcript("s.jsonl", [line])))
        self.assertEqual(self.texts(), ["first thought\nsecond thought"])

    def test_non_message_lines_are_skipped(self):
        lines = [
            _msg("user", "a real message"),
            {"type": "queue-operation", "op": "noop"},     # not a conversation message
            _msg("assistant", "another real message"),
        ]
        n = capture.capture_turn_delta(self.payload(self.transcript("s.jsonl", lines)))
        self.assertEqual(n, 2)
        self.assertEqual(self.texts(), ["a real message", "another real message"])

    def test_empty_text_message_is_counted_but_files_nothing(self):
        lines = [_msg("user", "   "), _msg("assistant", "real content")]
        n = capture.capture_turn_delta(self.payload(self.transcript("s.jsonl", lines)))
        self.assertEqual(n, 1)                         # the blank user turn files no record...
        self.assertEqual(self.texts(), ["real content"])
        # ...but the cursor still advanced past it, so a re-trigger adds nothing
        self.assertEqual(capture.capture_turn_delta(self.payload(self.transcript("s.jsonl", lines))), 0)

    def test_all_chunks_of_one_message_share_one_seq(self):
        big = "\n".join(f"word{i}" for i in range(2000))   # one message, many chunks
        capture.capture_turn_delta(self.payload(self.transcript("s.jsonl", [_msg("user", big)])))
        recs = self.records()
        self.assertGreater(len(recs), 1)
        self.assertEqual({r["seq"] for r in recs}, {0})   # all chunks of message 0 carry seq 0

    def test_boundary_free_message_chunks_losslessly(self):
        # the worst case for the chunker: a long message with NO whitespace boundary (the hard-cut path).
        big = "x" * (capture.CHUNK_MAX_CHARS * 5)
        chunks = capture.chunk_text(big)
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), big)            # every character preserved, nothing dropped
        self.assertTrue(all(len(c) <= capture.CHUNK_MAX_CHARS for c in chunks))


class EnvAndShapeTests(CaptureTestCase):
    def test_session_and_transcript_env_fallbacks_are_used(self):
        t = self.transcript("s.jsonl", [_msg("user", "from the environment")])
        os.environ["CLAUDE_SESSION_ID"] = "env-session"
        os.environ["CLAUDE_TRANSCRIPT_PATH"] = t
        try:
            n = capture.capture_turn_delta({})   # empty payload -> both come from the env
        finally:
            os.environ.pop("CLAUDE_SESSION_ID", None)
            os.environ.pop("CLAUDE_TRANSCRIPT_PATH", None)
        self.assertEqual(n, 1)
        self.assertEqual(self.records()[0]["session_id"], "env-session")

    def test_speaker_falls_back_to_top_level_role_then_unknown(self):
        lines = [
            {"type": "user", "role": "human", "content": "top-level role only"},  # no message dict
            {"message": {"content": "no role anywhere"}},                          # message, but no role -> unknown
        ]
        capture.capture_turn_delta(self.payload(self.transcript("s.jsonl", lines)))
        self.assertEqual([r["speaker"] for r in self.records()], ["human", "unknown"])


class ProjectionTests(CaptureTestCase):
    def test_only_the_content_text_enters_the_search_body(self):
        # Envelope metadata (session_id UUID, kind, speaker) and integers/tags must NOT be searchable, or
        # query("user")/query("delta")/UUID-hex-words would match every record and bury real recall.
        rec = capture._make_record("dadface", 7, "user", "the meeting timeout was half an hour")
        body = index._record_text(rec)
        self.assertIn("timeout", body)                # the narrative text IS searchable
        self.assertIn("meeting", body)
        self.assertNotIn("turn-delta", body)          # kind is provenance, not content
        self.assertNotIn("dadface", body)             # session_id (a UUID — hex fragments are real words)
        self.assertNotIn("user", body)                # speaker is provenance, not content
        self.assertNotIn("7", body)                   # seq (int) is skipped
        self.assertNotIn(str(rec["ts"]), body)        # ts (int) is skipped
        self.assertNotIn("transcript", body)          # top-level tags are excluded

    def test_captured_records_are_not_retrievable_by_their_provenance(self):
        # the end-to-end consequence: searching the provenance words must not flood with every record
        self.transcript("s.jsonl", [_msg("user", "deploy the new pricing page")])
        capture.capture_turn_delta(self.payload("%s" % os.path.join(self.tmp, "s.jsonl")))
        self.assertEqual(index.query("user").records, [])          # speaker is not a search term
        self.assertEqual(index.query("delta").records, [])         # kind is not a search term
        self.assertTrue(index.query("pricing").records)            # the real content still is


class CloseSeamTests(CaptureTestCase):
    def test_close_relay_now_lands_a_record_the_fail_then_pass(self):
        import close   # the real turn-close tool; its ambient-capture relay was inert until this slice
        t = self.transcript("h.jsonl", [_msg("user", "the spare key is under the blue pot")])
        self.assertEqual(self.records(), [])                      # inert: nothing yet
        close._trigger_ambient_capture({"session_id": "S", "transcript_path": t})
        hits = index.query("blue pot").records
        self.assertTrue(any("spare key" in r.get("text", "") for r in hits))   # the relay really captured

    def test_close_relay_swallows_a_real_capture_exception(self):
        import close
        # force a genuine exception INSIDE capture (not a graceful no-op) and prove neither capture nor the
        # close relay raises — capture can never gate a turn.
        t = self.transcript("h.jsonl", [_msg("user", "hi")])
        saved = ledger.append
        ledger.append = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
        try:
            self.assertEqual(capture.capture_turn_delta(self.payload(t)), 0)   # swallowed -> clean 0
            try:
                close._trigger_ambient_capture({"session_id": "S", "transcript_path": t})
            except Exception as e:  # noqa: BLE001
                self.fail(f"close relay raised on a real capture failure: {e!r}")
        finally:
            ledger.append = saved


if __name__ == "__main__":
    unittest.main()
