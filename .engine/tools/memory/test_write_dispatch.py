"""Unit tests for write_dispatch.py — the one seam every memory write crosses.

The write happens in a child launched from the accepted tree on disk, not on the server. These tests pin the
two halves of that seam. The CHILD half (`run_child`, `main`) is exercised for real against the ledger — the
same in-process path the memory-server tests drive it through — so the record it mints, the id it pre-mints
before the lock, and the response it hands back are the genuine ones. The PARENT half (`dispatch`,
`_read_outcome`) is exercised with an injected runner and hand-built line streams, because its whole job is to
classify ONE outcome without ever writing or retrying: a memory write is never retried, since a retry cannot
tell a lost confirmation from a lost write and would risk doubling it.

The real cross-process launcher (`_spawn_accepted_child`) is deliberately NOT exercised: it launches the
accepted materialization of this file, which a pre-merge worktree does not yet contain. Write authority
follows the merge; the cross-process launch begins working once this change is on an accepted commit, and
until then the seam is measured in-process exactly as the module docstring says.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import ledger, pins, records, refusals, write_dispatch  # noqa: E402


class _Base(unittest.TestCase):
    """A throwaway ledger cabinet; the child's writes land there. No context is installed — the committed
    test module's own tracked-HEAD adapter authorizes the direct write, the same way test_pins does."""

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

    def _pins(self):
        return [r for r in ledger.iter_records() if r.get("kind") == records.PIN_KIND]


class RunChildTests(_Base):
    def test_a_pin_is_written_by_the_child_and_the_response_carries_the_route_and_total(self):
        out = write_dispatch.run_child({"verb": "pin", "text": "the child does the write"})
        self.assertTrue(out["id"])
        self.assertEqual(out["text"], "the child does the write")
        self.assertEqual(out[records.PIN_VIA_KEY], records.PIN_VIA_ASSISTANT)  # never a claim of authority
        self.assertEqual(out["total"], 1)
        stored = self._pins()
        self.assertEqual([r[records.RECORD_ID_KEY] for r in stored], [out["id"]])  # returned id IS the stored id

    def test_the_preminted_id_on_the_begin_line_is_the_id_that_gets_committed(self):
        # The child pre-mints ONE id, announces it on a `begin` line BEFORE the write lock, and commits under
        # exactly that id — so a child that dies mid-write still left the parent the real record id.
        lines = []
        out = write_dispatch.run_child({"verb": "pin", "text": "announce then commit"}, emit=lines.append)
        events = [json.loads(line) for line in lines]
        kinds = [e["event"] for e in events]
        self.assertEqual(kinds, ["begin", "committed"])                 # begin strictly before committed
        begin = next(e for e in events if e["event"] == "begin")
        committed = next(e for e in events if e["event"] == "committed")
        self.assertEqual(begin["id"], out["id"])
        self.assertEqual(committed["record"][records.RECORD_ID_KEY], out["id"])

    def test_a_duplicate_pin_returns_the_existing_record_and_writes_no_second_record(self):
        first = write_dispatch.run_child({"verb": "pin", "text": "say it once", "session_id": "s1"})
        lines = []
        second = write_dispatch.run_child({"verb": "pin", "text": "  say   it once ", "session_id": "s1"},
                                          emit=lines.append)
        self.assertEqual(second["id"], first["id"])                     # the same record, not a new one
        self.assertEqual(len(self._pins()), 1)                          # nothing appended the second time
        self.assertIn("already_pinned", [json.loads(line)["event"] for line in lines])

    def test_a_pin_that_differs_only_by_session_is_not_a_duplicate(self):
        a = write_dispatch.run_child({"verb": "pin", "text": "same words", "session_id": "s1"})
        b = write_dispatch.run_child({"verb": "pin", "text": "same words", "session_id": "s2"})
        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual(len(self._pins()), 2)

    def test_a_pin_is_scrubbed_before_it_is_stored(self):
        out = write_dispatch.run_child({"verb": "pin", "text": "key sk-ant-api03-" + "A" * 32})
        self.assertNotIn("sk-ant-api03", out["text"])
        self.assertNotIn("sk-ant-api03", json.dumps(self._pins()))

    def test_a_long_list_carries_a_prune_note_and_a_short_one_does_not(self):
        for i in range(pins.PIN_PRUNE_HINT_AT - 1):
            out = write_dispatch.run_child({"verb": "pin", "text": f"standing note {i}"})
            self.assertNotIn("note", out)
        out = write_dispatch.run_child({"verb": "pin", "text": "one more"})
        self.assertEqual(out["total"], pins.PIN_PRUNE_HINT_AT)
        self.assertIn("prune", out["note"].lower())

    def test_withhold_and_restore_round_trip_through_the_child(self):
        pinned = write_dispatch.run_child({"verb": "pin", "text": "a note to take out of recall"})
        rid = pinned["id"]
        withheld = write_dispatch.run_child({"verb": "withhold", "record_id": rid})
        self.assertIn("still saved", withheld["withheld"])              # never reads as erasure
        self.assertEqual(pins.list_pins(), [])                          # gone from recall
        restored = write_dispatch.run_child({"verb": "restore", "record_id": rid})
        self.assertIn("back in recall", restored["restored"])
        self.assertEqual([p["id"] for p in pins.list_pins()], [rid])    # and back again

    def test_a_withhold_of_a_whole_conversation_names_the_conversation(self):
        import time
        ledger.append({"v": 1, "kind": records.AMBIENT_CAPTURE_KIND,
                       records.RECORD_ID_KEY: records.new_record_id(), "session_id": "s-gone",
                       "seq": 1, "speaker": "user", "ts": int(time.time()), "text": "a passing remark"})
        withheld = write_dispatch.run_child({"verb": "withhold", "session_id": "s-gone"})
        self.assertIn("that conversation", withheld["withheld"])   # names the conversation, not a note

    def test_an_unknown_verb_faults_rather_than_refusing(self):
        # A malformed request is our bug, not an operator-facing refusal, so it is a fault, not a refusal.
        with self.assertRaises(write_dispatch.DispatchFaulted):
            write_dispatch.run_child({"verb": "obliterate", "text": "x"})

    def test_a_refused_inner_write_is_returned_as_a_refusal_never_raised(self):
        # run_child catches an EngineRefusal from the inner write and hands it back as {"refused": ...} so the
        # real cross-process child and the in-process seam classify identically.
        with mock.patch.object(pins, "add", side_effect=refusals.EngineRefusal("held for a reason")):
            out = write_dispatch.run_child({"verb": "pin", "text": "x"})
        self.assertEqual(out, {"refused": "held for a reason"})


class DispatchClassifyTests(unittest.TestCase):
    """The parent-side relay: run the runner exactly once and classify the single outcome. No ledger, no
    context — the runner is injected."""

    def _once(self, outcome):
        calls = []

        def run(request):
            calls.append(request)
            return outcome

        return run, calls

    def test_a_response_is_returned_and_the_runner_is_called_exactly_once(self):
        run, calls = self._once({"id": "r1", "text": "t"})
        out = write_dispatch.dispatch({"verb": "pin", "text": "t"}, run=run)
        self.assertEqual(out, {"id": "r1", "text": "t"})
        self.assertEqual(len(calls), 1)                                 # never retried

    def test_a_refusal_becomes_a_dispatch_refused_that_is_an_engine_refusal(self):
        run, _ = self._once({"refused": "writing is held and nothing was changed"})
        with self.assertRaises(write_dispatch.DispatchRefused) as caught:
            write_dispatch.dispatch({"verb": "pin"}, run=run)
        # It derives from EngineRefusal, so the server's tool boundary forwards its sentence verbatim.
        self.assertIsInstance(caught.exception, refusals.EngineRefusal)
        self.assertEqual(str(caught.exception), "writing is held and nothing was changed")

    def test_a_fault_becomes_a_dispatch_faulted_that_is_not_an_engine_refusal(self):
        run, _ = self._once({"faulted": True})
        with self.assertRaises(write_dispatch.DispatchFaulted) as caught:
            write_dispatch.dispatch({"verb": "pin"}, run=run)
        # A fault stays the masked crash it is; it is deliberately NOT dressed up as a polished refusal.
        self.assertNotIsInstance(caught.exception, refusals.EngineRefusal)

    def test_a_non_dict_outcome_faults(self):
        run, _ = self._once("not a dict")
        with self.assertRaises(write_dispatch.DispatchFaulted):
            write_dispatch.dispatch({"verb": "pin"}, run=run)


class ReadOutcomeTests(unittest.TestCase):
    """Fold a child's stdout line stream into one outcome. A response line wins; failing that, evidence of a
    commit reconstructs a success rather than ever claiming nothing was saved; only with no evidence is it a
    fault."""

    def _line(self, **payload):
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def test_a_response_line_is_authoritative(self):
        stdout = "\n".join([
            self._line(event="begin", id="r1"),
            self._line(event="committed", record={"id": "r1", "text": "t"}),
            self._line(event="response", response={"id": "r1", "text": "t", "total": 1}),
        ])
        self.assertEqual(write_dispatch._read_outcome(stdout, 0), {"id": "r1", "text": "t", "total": 1})

    def test_a_committed_line_without_a_response_reconstructs_success_marked_unconfirmed(self):
        stdout = "\n".join([
            self._line(event="begin", id="r1"),
            self._line(event="committed",
                       record={"id": "r1", "text": "t", records.PIN_VIA_KEY: records.PIN_VIA_ASSISTANT}),
        ])
        out = write_dispatch._read_outcome(stdout, 0)
        self.assertEqual(out["id"], "r1")
        self.assertEqual(out["text"], "t")
        self.assertIn("unconfirmed", out)                               # NEVER "nothing saved" — it may have
        self.assertNotIn("faulted", out)

    def test_an_already_pinned_line_also_counts_as_a_landed_write(self):
        stdout = self._line(event="already_pinned", record={"id": "r9", "text": "dup"})
        out = write_dispatch._read_outcome(stdout, 0)
        self.assertEqual(out["id"], "r9")
        self.assertIn("unconfirmed", out)

    def test_empty_and_garbage_output_faults(self):
        for stdout in ("", "   \n\n", "not json\n{also not", self._line(event="begin", id="r1")):
            with self.subTest(stdout=stdout):
                self.assertEqual(write_dispatch._read_outcome(stdout, 0), {"faulted": True})


class MainRoundTripTests(_Base):
    """The child entry: read one request on stdin, run it, print the line stream then a response line."""

    def _run_main(self, request):
        buffer = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(request))), redirect_stdout(buffer):
            code = write_dispatch.main([])
        return code, buffer.getvalue()

    def test_main_prints_the_lines_then_the_response_and_the_parent_reads_it_back(self):
        code, printed = self._run_main({"verb": "pin", "text": "a round trip"})
        self.assertEqual(code, 0)
        events = [json.loads(line)["event"] for line in printed.splitlines() if line.strip()]
        self.assertEqual(events, ["begin", "committed", "response"])    # order the parent depends on
        # The parent reads exactly this stdout, and gets the child's own response back.
        outcome = write_dispatch._read_outcome(printed, code)
        self.assertEqual(outcome["text"], "a round trip")
        self.assertEqual([r[records.RECORD_ID_KEY] for r in self._pins()], [outcome["id"]])

    def test_empty_stdin_is_read_as_an_empty_request_and_faults_on_the_missing_verb(self):
        with mock.patch.object(sys, "stdin", io.StringIO("")):
            with self.assertRaises(write_dispatch.DispatchFaulted):
                write_dispatch.main([])


if __name__ == "__main__":
    unittest.main()
