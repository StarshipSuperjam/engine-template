"""Unit tests for write_dispatch.py — the one seam every memory write crosses.

The write happens in a child launched from the accepted tree on disk, not on the server. These tests pin the
two halves of that seam. The CHILD half (`run_child`, `main`) is exercised for real against the ledger — the
same in-process path the memory-server tests drive it through — so the record it mints, the id it pre-mints
before the lock, and the response it hands back are the genuine ones. The PARENT half (`dispatch`,
`_classify_outcome`, `_spawn_accepted_child`) is exercised with an injected runner, hand-built line streams,
and a mocked `subprocess.Popen`, because its whole job is to classify ONE outcome without ever writing or
retrying: a memory write is never retried, since a retry cannot tell a lost confirmation from a lost write and
would risk doubling it.

The real cross-process launcher (`_spawn_accepted_child`) launches the accepted materialization of this file,
which a pre-merge worktree does not yet contain — so its process handling (the timeout kill-and-reap, the
read-back after the reap, the not-attempted class, the forensic recording of the fault classes) is pinned here
against a FAKE process rather than a live launch. Write authority follows the merge; the cross-process launch
begins working once this change is on an accepted commit, and until then the seam is measured exactly as the
module docstring says.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import capture, ledger, pins, records, refusals, stranding_log, write_dispatch  # noqa: E402


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

    def test_the_committed_line_carries_the_record_byte_length(self):
        # SC-4: the committed line reports how many bytes landed, so a lost-confirmation reply can state what
        # was written rather than guess.
        lines = []
        write_dispatch.run_child({"verb": "pin", "text": "measure me"}, emit=lines.append)
        committed = next(json.loads(line) for line in lines if json.loads(line)["event"] == "committed")
        self.assertIsInstance(committed["bytes"], int)
        self.assertGreater(committed["bytes"], 0)

    def test_a_duplicate_pin_returns_the_existing_record_and_writes_no_second_record(self):
        first = write_dispatch.run_child({"verb": "pin", "text": "say it once", "session_id": "s1"})
        lines = []
        second = write_dispatch.run_child({"verb": "pin", "text": "  say   it once ", "session_id": "s1"},
                                          emit=lines.append)
        self.assertEqual(second["id"], first["id"])                     # the same record, not a new one
        self.assertEqual(len(self._pins()), 1)                          # nothing appended the second time
        self.assertIn("already_pinned", [json.loads(line)["event"] for line in lines])
        # ...and the reply SAYS so: the existing record's id and scrubbed text, never a fresh "Saved."
        self.assertTrue(second["already_pinned"])
        self.assertIn(first["id"], second["note"])
        self.assertIn("say it once", second["note"])
        self.assertNotIn("Saved.", second["note"])
        self.assertNotIn("already_pinned", first)                       # a fresh commit carries no such flag

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


class ClassifyOutcomeTests(unittest.TestCase):
    """`_classify_outcome` folds one child's stdout into exactly one of the five SR2 outcomes, keyed only on
    what the child PROVED and whether it could still be writing. Pure: every liveness and read-back fact
    arrives as an argument, so a real launch and a synthetic stream classify identically. `not_attempted` is
    the launcher's own call and never reaches here."""

    def _stdout(self, *events):
        return "\n".join(json.dumps(e, sort_keys=True, separators=(",", ":")) for e in events)

    def _classify(self, stdout, *, returncode=0, verb="pin", request=None, read_back=None, child_alive=False):
        return write_dispatch._classify_outcome(
            stdout, returncode=returncode, verb=verb, request=request or {"verb": verb},
            read_back=read_back, child_alive=child_alive)

    def _forbidden_read_back(self, record_id):
        raise AssertionError("read-back must not be consulted once a receipt is trusted")

    # -- the authoritative response line wins ---------------------------------------------------------------
    def test_a_response_line_is_the_committed_outcome(self):
        out = self._classify(
            self._stdout({"event": "begin", "id": "r1"},
                         {"event": "committed", "record": {"id": "r1", "text": "t"}},
                         {"event": "response", "response": {"id": "r1", "text": "t", "total": 1}}),
            read_back=self._forbidden_read_back)
        self.assertEqual(out, {"outcome": "committed", "response": {"id": "r1", "text": "t", "total": 1}})

    def test_a_response_carrying_a_refusal_is_the_refused_outcome(self):
        out = self._classify(
            self._stdout({"event": "response", "response": {"refused": "writing is held"}}),
            read_back=self._forbidden_read_back)
        self.assertEqual(out, {"outcome": "refused", "sentence": "writing is held"})

    # -- a well-formed receipt is a landed write (DH-1) ----------------------------------------------------
    def test_a_wellformed_committed_receipt_is_trusted_without_a_read_back(self):
        # DH-1: a committed receipt whose confirmation line was lost is a LANDED write — rebuilt from the
        # receipt, never read back, and never reported as nothing-saved.
        out = self._classify(
            self._stdout({"event": "begin", "id": "r1"},
                         {"event": "committed",
                          "record": {"id": "r1", "text": "t", records.PIN_VIA_KEY: records.PIN_VIA_ASSISTANT},
                          "bytes": 128}),
            verb="pin", read_back=self._forbidden_read_back)
        self.assertEqual(out["outcome"], "committed")
        self.assertEqual(out["response"]["id"], "r1")
        self.assertEqual(out["response"]["text"], "t")
        self.assertIn("unconfirmed", out["response"])                  # honest it was rebuilt
        self.assertNotIn("nothing was saved", json.dumps(out).lower())

    def test_an_already_pinned_receipt_counts_as_a_landed_write(self):
        out = self._classify(
            self._stdout({"event": "already_pinned", "record": {"id": "r9", "text": "dup"}}),
            verb="pin", read_back=self._forbidden_read_back)
        self.assertEqual(out["outcome"], "committed")
        self.assertEqual(out["response"]["id"], "r9")
        self.assertIn("unconfirmed", out["response"])
        self.assertTrue(out["response"]["already_pinned"])              # rebuilt as the duplicate it was
        self.assertIn("r9", out["response"]["note"])
        self.assertIn("dup", out["response"]["note"])

    # -- a forged or corrupt receipt cannot mint a success (SC-3) ------------------------------------------
    def test_a_receipt_without_a_record_id_is_not_trusted_and_falls_through(self):
        # SC-3: a fabricated or corrupt committed line cannot by itself mint a success. With no id on the
        # receipt, nothing on disk and the child dead, it is an honest fault — not a forged commit.
        out = self._classify(
            self._stdout({"event": "begin", "id": "r1"},
                         {"event": "committed", "record": {"text": "no id here"}}),
            returncode=1, verb="pin", read_back=lambda rid: None, child_alive=False)
        self.assertEqual(out, {"outcome": "faulted", "returncode": 1})

    def test_a_receipt_whose_record_is_not_a_dict_is_overruled_by_the_disk(self):
        # SC-3: a committed line is trusted only when it carries a dict record with an id; anything else falls
        # to the read-back of the pre-minted begin id, so only real disk state can decide the success.
        seen = []

        def read_back(rid):
            seen.append(rid)
            return {"id": rid, "text": "actually on disk"}

        out = self._classify(
            self._stdout({"event": "begin", "id": "r1"},
                         {"event": "committed", "record": "not-a-dict"}),
            verb="pin", read_back=read_back, child_alive=False)
        self.assertEqual(seen, ["r1"])                                 # the disk, not the forged line, decided
        self.assertEqual(out["outcome"], "committed")
        self.assertEqual(out["response"]["id"], "r1")

    # -- a begin line: decide on disk first, then on liveness (DH-1, DH-2) ---------------------------------
    def test_a_begin_whose_id_is_on_disk_is_committed_even_while_the_child_is_alive(self):
        # DH-1: the disk is authoritative. A pre-minted id found on the ledger is a landed write whether or not
        # the child is still around — a lost confirmation, never a fault and never nothing-saved.
        for child_alive in (True, False):
            with self.subTest(child_alive=child_alive):
                out = self._classify(
                    self._stdout({"event": "begin", "id": "r1"}),
                    verb="pin", read_back=lambda rid: {"id": rid, "text": "on disk"},
                    child_alive=child_alive)
                self.assertEqual(out["outcome"], "committed")
                self.assertEqual(out["response"]["id"], "r1")
                self.assertIn("unconfirmed", out["response"])

    def test_a_begin_absent_from_disk_with_a_live_child_is_unconfirmed_never_nothing_saved(self):
        # DH-2: a begin-only child that is still alive may yet commit; it is held open as unconfirmed, never
        # collapsed to a fault and never reported as nothing saved.
        out = self._classify(
            self._stdout({"event": "begin", "id": "r1"}),
            verb="pin", read_back=lambda rid: None, child_alive=True)
        self.assertEqual(out["outcome"], "unconfirmed")
        self.assertEqual(out["response"]["id"], "r1")
        self.assertEqual(out["response"]["unconfirmed"], write_dispatch._STILL_UNCONFIRMED_NOTE)
        self.assertNotIn("nothing was saved", json.dumps(out).lower())

    def test_a_begin_absent_from_disk_with_a_dead_child_is_faulted(self):
        # DH-2: confirmed dead, nothing on disk, no receipt — a genuine fault carrying the child's exit.
        out = self._classify(
            self._stdout({"event": "begin", "id": "r1"}),
            returncode=-9, verb="pin", read_back=lambda rid: None, child_alive=False)
        self.assertEqual(out, {"outcome": "faulted", "returncode": -9})

    def test_no_begin_line_at_all_is_faulted_and_carries_the_returncode(self):
        # DH-3: the child never reached the write body. Every no-begin stream faults and threads the returncode.
        streams = ("", "   \n\n", "not json\n{also not",
                   self._stdout({"event": "committed", "record": {"text": "no id"}}))
        for stdout in streams:
            with self.subTest(stdout=stdout):
                out = self._classify(stdout, returncode=3, read_back=lambda rid: None, child_alive=False)
                self.assertEqual(out, {"outcome": "faulted", "returncode": 3})

    # -- a lost confirmation reads as the verb that actually ran (US-2) ------------------------------------
    def test_a_lost_confirmation_reads_as_a_withhold_not_a_pin(self):
        # US-2: a withhold that committed but lost its confirmation returns the withheld message, not a
        # pin-shaped null response.
        out = self._classify(
            self._stdout({"event": "begin", "id": "r1"},
                         {"event": "committed", "record": {"id": "r1"}, "bytes": 64}),
            verb="withhold", request={"verb": "withhold", "record_id": "r1"},
            read_back=self._forbidden_read_back)
        self.assertEqual(out["outcome"], "committed")
        self.assertIn("withheld", out["response"])
        self.assertIn("out of recall", out["response"]["withheld"])
        self.assertIn("unconfirmed", out["response"])
        self.assertNotIn("id", out["response"])                        # not a pin-shaped reply

    def test_a_lost_confirmation_reads_as_a_restore(self):
        out = self._classify(
            self._stdout({"event": "begin", "id": "r1"}),
            verb="restore", request={"verb": "restore", "record_id": "r1"},
            read_back=lambda rid: {"id": rid}, child_alive=False)
        self.assertEqual(out["outcome"], "committed")
        self.assertIn("restored", out["response"])
        self.assertIn("back in recall", out["response"]["restored"])
        self.assertIn("unconfirmed", out["response"])

    def test_a_lost_withhold_of_a_whole_conversation_names_the_conversation(self):
        out = self._classify(
            self._stdout({"event": "committed", "record": {"id": "r1"}, "bytes": 64}),
            verb="withhold", request={"verb": "withhold", "session_id": "s-gone"},
            read_back=self._forbidden_read_back)
        self.assertIn("that conversation", out["response"]["withheld"])

    # -- round 3, DH-5: a response or receipt is believed only when it is COMPLETE for the verb ------------
    def test_a_response_line_with_a_null_or_empty_response_is_not_a_committed_outcome(self):
        # DH-5: a `response` event carrying null, a non-dict, or an empty object used to be minted as
        # committed with `{}` — an empty reply for a write nothing proves. Now it is not believed: the
        # decision falls through to the receipts and the disk.
        for payload in (None, "", [], {}, 7):
            with self.subTest(payload=payload):
                out = self._classify(
                    self._stdout({"event": "begin", "id": "r1"}, {"event": "response", "response": payload}),
                    returncode=1, verb="pin", read_back=lambda rid: None, child_alive=False)
                self.assertEqual(out, {"outcome": "faulted", "returncode": 1})
        # ...and a stream with no begin at all is the plain no-begin fault, never `{"response": {}}`.
        out = self._classify(self._stdout({"event": "response"}), returncode=2,
                             read_back=lambda rid: None, child_alive=False)
        self.assertEqual(out, {"outcome": "faulted", "returncode": 2})

    def test_an_incomplete_response_for_the_verb_falls_through_to_the_disk(self):
        # DH-5: the response must be the whole operator-facing reply for the verb — a pin without its stored
        # text, a withhold/restore whose sentence is missing or not a string, a refusal that is not a
        # sentence. Each falls through; here the disk then decides the outcome.
        cases = [
            ("pin", {"id": "r1"}),                       # no stored text
            ("pin", {"id": "", "text": "t"}),            # empty id
            ("pin", {"text": "t"}),                      # no id at all
            ("withhold", {"withheld": None}),
            ("withhold", {"restored": "wrong verb's sentence"}),
            ("restore", {"restored": ""}),
            ("pin", {"refused": ""}),
            ("pin", {"refused": ["not", "a", "sentence"]}),
        ]
        for verb, payload in cases:
            with self.subTest(verb=verb, payload=payload):
                seen = []

                def read_back(rid, seen=seen):
                    seen.append(rid)
                    return {"id": rid, "text": "on disk"}

                out = self._classify(
                    self._stdout({"event": "begin", "id": "r1"}, {"event": "response", "response": payload}),
                    verb=verb, request={"verb": verb, "record_id": "r1"}, read_back=read_back)
                self.assertEqual(seen, ["r1"])           # the disk, not the malformed line, decided
                self.assertEqual(out["outcome"], "committed")
                self.assertIn("unconfirmed", out["response"])   # honest it was rebuilt, not relayed

    def test_a_complete_response_is_relayed_for_every_verb(self):
        for verb, payload in (("pin", {"id": "r1", "text": "t", "via": "assistant", "total": 3}),
                              ("withhold", {"withheld": "Out of recall."}),
                              ("restore", {"restored": "Back in recall."})):
            with self.subTest(verb=verb):
                out = self._classify(
                    self._stdout({"event": "begin", "id": "r1"}, {"event": "response", "response": payload}),
                    verb=verb, request={"verb": verb, "record_id": "r1"}, read_back=self._forbidden_read_back)
                self.assertEqual(out, {"outcome": "committed", "response": payload})

    def test_a_committed_receipt_must_agree_with_the_begin_id_and_carry_a_positive_byte_length(self):
        # DH-5: a `committed` receipt is a receipt for THIS write only when it names the pre-minted id and
        # carries the byte length the child prints after the append lands. A receipt for some other id, or
        # with no/zero/negative/boolean/non-integer bytes, is not a receipt — the disk decides.
        bad_receipts = [
            {"event": "committed", "record": {"id": "other", "text": "t"}, "bytes": 64},   # foreign id
            {"event": "committed", "record": {"id": "r1", "text": "t"}},                   # no bytes
            {"event": "committed", "record": {"id": "r1", "text": "t"}, "bytes": 0},
            {"event": "committed", "record": {"id": "r1", "text": "t"}, "bytes": -5},
            {"event": "committed", "record": {"id": "r1", "text": "t"}, "bytes": True},
            {"event": "committed", "record": {"id": "r1", "text": "t"}, "bytes": "64"},
            {"event": "committed", "record": {"id": "r1"}, "bytes": 64},                   # pin without text
            {"event": "committed", "record": {"id": ""}, "bytes": 64},                     # empty id
        ]
        for receipt in bad_receipts:
            with self.subTest(receipt=receipt):
                out = self._classify(
                    self._stdout({"event": "begin", "id": "r1"}, receipt),
                    returncode=1, verb="pin", read_back=lambda rid: None, child_alive=False)
                self.assertEqual(out, {"outcome": "faulted", "returncode": 1})
                # ...and never a success minted from the bad line when the disk does have the write:
                out = self._classify(
                    self._stdout({"event": "begin", "id": "r1"}, receipt),
                    verb="pin", read_back=lambda rid: {"id": rid, "text": "on disk"})
                self.assertEqual(out["response"]["id"], "r1")
                self.assertEqual(out["response"]["text"], "on disk")

    def test_an_already_pinned_receipt_needs_the_existing_pins_text_and_only_exists_for_pin(self):
        # DH-5: already_pinned names the EXISTING pin (a different id from the begin line, by design) and
        # must carry that record's scrubbed text; it is meaningless for withhold/restore.
        out = self._classify(
            self._stdout({"event": "begin", "id": "r1"},
                         {"event": "already_pinned", "record": {"id": "r9", "text": "dup"}}),
            verb="pin", read_back=self._forbidden_read_back)
        self.assertEqual(out["outcome"], "committed")
        self.assertEqual(out["response"]["id"], "r9")                 # the existing pin, not the begin id
        for verb, receipt in (("pin", {"event": "already_pinned", "record": {"id": "r9"}}),
                              ("withhold", {"event": "already_pinned", "record": {"id": "r9", "text": "d"}})):
            with self.subTest(verb=verb):
                out = self._classify(self._stdout({"event": "begin", "id": "r1"}, receipt),
                                     returncode=1, verb=verb, request={"verb": verb, "record_id": "r1"},
                                     read_back=lambda rid: None, child_alive=False)
                self.assertEqual(out, {"outcome": "faulted", "returncode": 1})

    # -- round 3, DH-6: a read-back that could not be PERFORMED is not evidence of absence -----------------
    def test_an_unreadable_ledger_keeps_a_begun_write_unconfirmed_never_faulted(self):
        # DH-6: the child is confirmed dead, it printed a begin line, and the ledger could not be read back.
        # Absence was never established, so this is `unconfirmed` — never `faulted`, never nothing-saved —
        # whether or not the child is alive.
        def unreadable(rid):
            raise write_dispatch.ReadBackUnavailable("permission denied")

        for child_alive in (False, True):
            with self.subTest(child_alive=child_alive):
                out = self._classify(
                    self._stdout({"event": "begin", "id": "r1"}),
                    returncode=1, verb="pin", read_back=unreadable, child_alive=child_alive)
                self.assertEqual(out["outcome"], "unconfirmed")
                self.assertEqual(out["response"]["id"], "r1")
                self.assertEqual(out["response"]["unconfirmed"], write_dispatch._UNRESOLVED_NOTE)
                self.assertIn("could not be read back", out["response"]["unconfirmed"])
                self.assertNotIn("returncode", out)
                self.assertNotIn("nothing was saved", json.dumps(out).lower())

    def test_the_ledger_read_back_is_three_state(self):
        # DH-6: found -> the record; searched and absent -> None; could not read -> ReadBackUnavailable.
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {ledger.ENV_DIR: tmp}):
            self.assertIsNone(write_dispatch._ledger_read_back("nothing-yet"))        # empty cabinet: absent
            with mock.patch.object(write_dispatch.ledger, "read", side_effect=PermissionError("denied")):
                with self.assertRaises(write_dispatch.ReadBackUnavailable):
                    write_dispatch._ledger_read_back("r1")
        self.assertIsNone(write_dispatch._ledger_read_back(""))                       # no id: nothing to read


class DispatchRelayTests(unittest.TestCase):
    """`dispatch` runs the runner exactly ONCE and normalizes its outcome. It speaks two dialects: the new
    classified envelope (`{"outcome": ...}`) from the cross-process launcher, and the in-process test seam
    where `run_child` hands back the operator response directly (or `{"refused": ...}`)."""

    def _once(self, outcome):
        calls = []

        def run(request):
            calls.append(request)
            return outcome

        return run, calls

    # -- the classified envelope ---------------------------------------------------------------------------
    def test_a_committed_envelope_returns_its_response_and_runs_the_child_once(self):
        run, calls = self._once({"outcome": "committed", "response": {"id": "r1", "text": "t"}})
        out = write_dispatch.dispatch({"verb": "pin", "text": "t"}, run=run)
        self.assertEqual(out, {"id": "r1", "text": "t"})
        self.assertEqual(len(calls), 1)                                # never retried

    def test_an_unconfirmed_envelope_returns_its_response_never_an_error(self):
        run, _ = self._once({"outcome": "unconfirmed", "response": {"unconfirmed": "may still be completing"}})
        out = write_dispatch.dispatch({"verb": "pin"}, run=run)
        self.assertEqual(out, {"unconfirmed": "may still be completing"})

    def test_a_refused_envelope_becomes_a_dispatch_refused_that_is_an_engine_refusal(self):
        run, _ = self._once({"outcome": "refused", "sentence": "writing is held and nothing was changed"})
        with self.assertRaises(write_dispatch.DispatchRefused) as caught:
            write_dispatch.dispatch({"verb": "pin"}, run=run)
        # It derives from EngineRefusal, so the server's tool boundary forwards its sentence verbatim.
        self.assertIsInstance(caught.exception, refusals.EngineRefusal)
        self.assertEqual(str(caught.exception), "writing is held and nothing was changed")

    def test_a_faulted_envelope_becomes_a_dispatch_faulted_that_is_not_a_refusal(self):
        run, _ = self._once({"outcome": "faulted", "returncode": 1})
        with self.assertRaises(write_dispatch.DispatchFaulted) as caught:
            write_dispatch.dispatch({"verb": "pin"}, run=run)
        # A fault stays the masked crash it is; it is deliberately NOT dressed up as a polished refusal.
        self.assertNotIsInstance(caught.exception, refusals.EngineRefusal)

    def test_a_not_attempted_envelope_becomes_a_dispatch_faulted(self):
        run, _ = self._once({"outcome": "not_attempted"})
        with self.assertRaises(write_dispatch.DispatchFaulted):
            write_dispatch.dispatch({"verb": "pin"}, run=run)

    def test_an_unknown_outcome_faults(self):
        run, _ = self._once({"outcome": "surprise"})
        with self.assertRaises(write_dispatch.DispatchFaulted):
            write_dispatch.dispatch({"verb": "pin"}, run=run)

    # -- the in-process legacy seam (run=run_child) --------------------------------------------------------
    def test_the_in_process_seam_returns_a_plain_operator_response(self):
        run, calls = self._once({"id": "r1", "text": "t", "total": 1})
        self.assertEqual(write_dispatch.dispatch({"verb": "pin"}, run=run),
                         {"id": "r1", "text": "t", "total": 1})
        self.assertEqual(len(calls), 1)

    def test_the_in_process_seam_refusal_becomes_a_dispatch_refused(self):
        run, _ = self._once({"refused": "held for a reason"})
        with self.assertRaises(write_dispatch.DispatchRefused) as caught:
            write_dispatch.dispatch({"verb": "pin"}, run=run)
        self.assertEqual(str(caught.exception), "held for a reason")

    def test_a_non_dict_outcome_faults(self):
        run, _ = self._once("not a dict")
        with self.assertRaises(write_dispatch.DispatchFaulted):
            write_dispatch.dispatch({"verb": "pin"}, run=run)


class _FakeProc:
    """A stand-in for the launched child process. `communicate` returns the child's stdout on the first call
    (or raises TimeoutExpired to model a stuck/cancelled child), and the salvaged stdout on the post-kill reap
    call. `kill` records that the parent reaped before reading."""

    def __init__(self, *, stdout="", returncode=0, timeout_first=False, reap_stdout="", timeout_reap=False):
        self._stdout = stdout
        self._reap_stdout = reap_stdout
        self.returncode = returncode
        self._timeout_first = timeout_first
        self._timeout_reap = timeout_reap
        self.killed = False
        self.calls = 0
        self.input_seen = None

    def communicate(self, input=None, timeout=None):
        self.calls += 1
        if self.calls == 1:
            self.input_seen = input
            if self._timeout_first:
                raise subprocess.TimeoutExpired(cmd="child", timeout=timeout)
            return (self._stdout, "")
        if self._timeout_reap:                                         # the kill did not land in time
            raise subprocess.TimeoutExpired(cmd="child", timeout=timeout)
        return (self._reap_stdout, "")                                 # the reap after kill()

    def kill(self):
        self.killed = True


class SpawnReapTests(_Base):
    """The real parent-side launcher `_spawn_accepted_child`, with `subprocess.Popen` mocked. This pins the
    process handling the in-process seam cannot: the payload rides on stdin (never argv), a stuck child is
    killed and reaped before the read-back, a landed write is recovered from disk after the reap, and the two
    fault classes are recorded forensically."""

    def _patch_popen(self, proc, captured):
        def fake_popen(argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            return proc

        return mock.patch.object(subprocess, "Popen", fake_popen)

    def _capture_recording(self):
        recorded = []
        patch = mock.patch.object(
            write_dispatch.stranding_log, "record_dispatch_outcome",
            lambda *a, **k: recorded.append((a, k)) or True)
        return recorded, patch

    def test_a_clean_child_commits_and_the_payload_never_rides_on_argv(self):
        # DH-4 argv-absence: the whole request crosses on STDIN; nothing a `ps` could read carries it.
        request = {"verb": "pin", "text": "a secret standing note", "session_id": "s1"}
        stdout = "\n".join(json.dumps(e, sort_keys=True, separators=(",", ":")) for e in (
            {"event": "begin", "id": "r1"},
            {"event": "committed", "record": {"id": "r1", "text": "a secret standing note"}},
            {"event": "response", "response": {"id": "r1", "text": "a secret standing note", "total": 1}},
        ))
        proc = _FakeProc(stdout=stdout, returncode=0)
        captured = {}
        recorded, recpatch = self._capture_recording()
        with self._patch_popen(proc, captured), recpatch:
            outcome = write_dispatch._spawn_accepted_child(request)
        self.assertEqual(outcome["outcome"], "committed")
        self.assertEqual(outcome["response"]["id"], "r1")
        self.assertNotIn("a secret standing note", " ".join(captured["argv"]))
        self.assertIn(write_dispatch.OPERATION, captured["argv"])       # launched under the dispatch operation
        self.assertEqual(json.loads(proc.input_seen), request)         # the payload went to stdin
        self.assertEqual(recorded, [])                                  # a committed write is not stranded

    def test_a_stuck_child_is_killed_and_reaped_then_a_read_back_recovers_a_landed_write(self):
        # DH-1 under a kill: the child pre-minted an id, went stuck, was killed and reaped, and left only its
        # begin line — but the record IS on disk. The read-back after the reap recovers it: a landed write.
        seeded = write_dispatch.run_child({"verb": "pin", "text": "it did land"})
        rid = seeded["id"]
        begin_only = json.dumps({"event": "begin", "id": rid}, sort_keys=True, separators=(",", ":"))
        proc = _FakeProc(timeout_first=True, reap_stdout=begin_only, returncode=-9)
        captured = {}
        recorded, recpatch = self._capture_recording()
        with self._patch_popen(proc, captured), recpatch:
            outcome = write_dispatch._spawn_accepted_child({"verb": "pin", "text": "unused here"})
        self.assertTrue(proc.killed)                                    # reaped before the read-back
        self.assertEqual(outcome["outcome"], "committed")
        self.assertEqual(outcome["response"]["id"], rid)
        self.assertEqual(recorded, [])                                  # a recovered write is not stranded

    def test_a_stuck_child_with_nothing_on_disk_is_faulted_and_recorded(self):
        # DH-2/DH-3: begin-only, killed and reaped, and nothing on disk — a genuine fault, recorded to the
        # stranding log with the child's signalled exit threaded through.
        begin_only = json.dumps({"event": "begin", "id": "never-written"}, sort_keys=True,
                                separators=(",", ":"))
        proc = _FakeProc(timeout_first=True, reap_stdout=begin_only, returncode=-9)
        captured = {}
        recorded, recpatch = self._capture_recording()
        with self._patch_popen(proc, captured), recpatch:
            outcome = write_dispatch._spawn_accepted_child({"verb": "pin", "text": "x"})
        self.assertTrue(proc.killed)
        self.assertEqual(outcome["outcome"], "faulted")
        self.assertEqual(outcome["returncode"], -9)
        self.assertEqual(len(recorded), 1)
        (args, _kwargs) = recorded[0]
        self.assertEqual(args[0], stranding_log.DispatchOutcome.FAULTED)
        self.assertEqual(args[1], -9)                                  # the child's signalled exit, threaded

    def test_a_cancelled_child_that_left_no_output_is_faulted_and_recorded(self):
        # SC-3 client cancellation: the reap salvages nothing, so there is no begin line at all — a fault.
        proc = _FakeProc(timeout_first=True, reap_stdout="", returncode=-15)
        captured = {}
        recorded, recpatch = self._capture_recording()
        with self._patch_popen(proc, captured), recpatch:
            outcome = write_dispatch._spawn_accepted_child({"verb": "pin", "text": "x"})
        self.assertTrue(proc.killed)
        self.assertEqual(outcome["outcome"], "faulted")
        self.assertEqual(recorded[0][0][0], stranding_log.DispatchOutcome.FAULTED)
        self.assertEqual(recorded[0][0][1], -15)

    def test_a_child_that_survives_the_reap_window_is_unconfirmed_never_faulted_and_not_recorded(self):
        # DH2-1: kill() was sent but the reap timed out too, so the child is NOT confirmed dead and may still
        # be holding the ledger lock or committing. LOSE NOTHING: that is `unconfirmed`, never `faulted`;
        # nothing is written to the stranding log, and the never-launched exit sentinel is not reused for a
        # child that did run.
        proc = _FakeProc(timeout_first=True, timeout_reap=True, returncode=None)
        captured = {}
        recorded, recpatch = self._capture_recording()
        with self._patch_popen(proc, captured), recpatch:
            outcome = write_dispatch._spawn_accepted_child({"verb": "pin", "text": "x"})
        self.assertTrue(proc.killed)
        self.assertEqual(proc.calls, 2)                                 # the reap WAS attempted
        self.assertEqual(outcome["outcome"], "unconfirmed")
        self.assertEqual(outcome["response"]["unconfirmed"], write_dispatch._STILL_UNCONFIRMED_NOTE)
        self.assertNotIn("returncode", outcome)                        # no exit status exists yet
        self.assertNotIn(str(stranding_log.EXIT_NOT_LAUNCHED), json.dumps(outcome))
        self.assertEqual(recorded, [])                                  # never stranded: it may still land

    def test_a_dead_child_whose_ledger_cannot_be_read_back_is_unconfirmed_and_not_recorded(self):
        # Round 3, DH-6: begin-only, killed and reaped, and the LEDGER COULD NOT BE READ (a permission
        # fault, a missing directory). That is not evidence the write is absent, so it is `unconfirmed` —
        # never `faulted`, and nothing is written to the stranding log as if the write were known lost.
        begin_only = json.dumps({"event": "begin", "id": "maybe-landed"}, sort_keys=True,
                                separators=(",", ":"))
        proc = _FakeProc(timeout_first=True, reap_stdout=begin_only, returncode=-9)
        captured = {}
        recorded, recpatch = self._capture_recording()
        with self._patch_popen(proc, captured), recpatch, \
                mock.patch.object(write_dispatch.ledger, "read", side_effect=PermissionError("denied")):
            outcome = write_dispatch._spawn_accepted_child({"verb": "pin", "text": "x"})
        self.assertTrue(proc.killed)
        self.assertEqual(outcome["outcome"], "unconfirmed")
        self.assertEqual(outcome["response"]["id"], "maybe-landed")
        self.assertEqual(outcome["response"]["unconfirmed"], write_dispatch._UNRESOLVED_NOTE)
        self.assertNotIn("returncode", outcome)
        self.assertEqual(recorded, [])                                  # never stranded: it may have landed

    def test_an_interrupted_wait_kills_and_reaps_a_real_child_before_propagating(self):
        # Round 3, TI-1 (DH-7 folded in): the parent's wait is interrupted by a REAL cancellation — a
        # KeyboardInterrupt delivered to the waiting thread while a REAL disposable child is blocked — not a
        # TimeoutExpired from a fake. The child must be dead and reaped (its exit status collected) before
        # the interruption leaves `_spawn_accepted_child`, and no outcome is classified or recorded.
        import _thread
        launched = {}

        def real_popen(argv, **kwargs):
            # The same pipes and text mode the launcher asked for, on a disposable child that would block
            # for a minute if nothing killed it.
            proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
            launched["proc"] = proc
            return proc

        recorded, recpatch = self._capture_recording()
        timer = threading.Timer(0.5, _thread.interrupt_main)
        timer.daemon = True
        try:
            with mock.patch.object(subprocess, "Popen", real_popen), recpatch:
                timer.start()
                with self.assertRaises(KeyboardInterrupt):
                    write_dispatch._spawn_accepted_child({"verb": "pin", "text": "x"})
        finally:
            timer.cancel()
            proc = launched.get("proc")
            if proc is not None and proc.poll() is None:                # never leave a child behind a test
                proc.kill()
                proc.wait()
        self.assertIn("proc", launched)
        self.assertIsNotNone(proc.returncode)                          # reaped before the raise propagated
        self.assertNotEqual(proc.returncode, 0)                        # ...by the kill, not a natural exit
        self.assertEqual(recorded, [])                                  # nothing classified, nothing stranded

    def test_a_child_that_never_launches_is_not_attempted_and_recorded(self):
        # not_attempted: Popen itself fails, so no process ever ran — a class distinct from a child that ran
        # and faulted, recorded with the never-launched exit sentinel.
        def boom(argv, **kwargs):
            raise OSError("cannot fork")

        recorded, recpatch = self._capture_recording()
        with mock.patch.object(subprocess, "Popen", boom), recpatch:
            outcome = write_dispatch._spawn_accepted_child({"verb": "pin", "text": "x"})
        self.assertEqual(outcome, {"outcome": "not_attempted"})
        self.assertEqual(len(recorded), 1)
        (args, _kwargs) = recorded[0]
        self.assertEqual(args[0], stranding_log.DispatchOutcome.NOT_ATTEMPTED)
        self.assertEqual(args[1], stranding_log.EXIT_NOT_LAUNCHED)


class StrandingForensicContractTests(unittest.TestCase):
    """DH-3: the forensic record a dispatch fault writes carries ONLY a closed-enum outcome and the child's
    integer exit status — no message, no argv, no path, no record text — and the export contract carries
    exactly those two new fields through `sanitize`."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._path = os.path.join(self._tmp.name, "strand.log")

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_faulted_outcome_is_recorded_with_its_enum_and_integer_exit(self):
        self.assertTrue(stranding_log.record_dispatch_outcome(
            stranding_log.DispatchOutcome.FAULTED, -9, path=self._path))
        rows = stranding_log.read_records(path=self._path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event"], stranding_log.Event.WRITE_DISPATCH.value)
        self.assertEqual(rows[0]["dispatch_outcome"], "faulted")
        self.assertEqual(rows[0]["exit_status"], -9)

    def test_a_not_attempted_outcome_carries_the_never_launched_sentinel(self):
        stranding_log.record_dispatch_outcome(
            stranding_log.DispatchOutcome.NOT_ATTEMPTED, stranding_log.EXIT_NOT_LAUNCHED, path=self._path)
        row = stranding_log.read_records(path=self._path)[0]
        self.assertEqual(row["dispatch_outcome"], "not-attempted")
        self.assertEqual(row["exit_status"], stranding_log.EXIT_NOT_LAUNCHED)

    def test_a_free_text_outcome_or_non_integer_exit_is_refused_not_recorded(self):
        # The record is a closed enum and a true integer: a string outcome, a bool, or a float is refused, so
        # a caller cannot smuggle free text or a truthy non-integer into the forensic record.
        self.assertFalse(stranding_log.record_dispatch_outcome("faulted", 1, path=self._path))
        self.assertFalse(stranding_log.record_dispatch_outcome(
            stranding_log.DispatchOutcome.FAULTED, True, path=self._path))
        self.assertFalse(stranding_log.record_dispatch_outcome(
            stranding_log.DispatchOutcome.FAULTED, 1.5, path=self._path))
        self.assertEqual(stranding_log.read_records(path=self._path), [])

    def test_the_export_contract_keeps_the_two_new_fields_and_no_content(self):
        stranding_log.record_dispatch_outcome(stranding_log.DispatchOutcome.FAULTED, 7, path=self._path)
        row = stranding_log.read_records(path=self._path)[0]
        exported = stranding_log.sanitize(row)
        self.assertEqual(exported["dispatch_outcome"], "faulted")
        self.assertEqual(exported["exit_status"], 7)
        self.assertIn("dispatch_outcome", stranding_log._EXPORT_KEYS)
        self.assertIn("exit_status", stranding_log._EXPORT_KEYS)
        # Nothing the child produced rides along: no message, argv, path, or record text.
        for forbidden in ("message", "argv", "path", "text"):
            self.assertNotIn(forbidden, row)


class ConcurrencyAndCollisionTests(_Base):
    """DH-4: two writers never interleave — a concurrent write finds the single-writer lock held and refuses
    cleanly (it is not queued), and the dedup key includes the session, so two conversations pinning the same
    words are two records, not a collision. DH2-5: the decisive duplicate check runs under that lock, so two
    overlapping identical pins land exactly one record."""

    def test_a_second_writer_refuses_while_the_single_writer_lock_is_held(self):
        # Hold the capture transaction lock exactly as a concurrent writer would, then attempt a dispatched
        # write: it must find the lock held and refuse cleanly with nothing saved, never tearing a half-write
        # in under contention.
        data_dir = os.path.dirname(ledger.ledger_path())
        os.makedirs(data_dir, exist_ok=True)
        held = capture._acquire_lock(os.path.join(data_dir, capture.LOCK_FILENAME))
        self.assertIsNotNone(held)                                     # we hold it; the writer must now wait
        try:
            out = write_dispatch.run_child({"verb": "pin", "text": "cannot get in"})
        finally:
            capture._release_lock(held)
        self.assertIn("refused", out)
        self.assertIn("another memory write is in progress", out["refused"])
        self.assertEqual(self._pins(), [])                             # nothing torn in under contention

    def test_two_sessions_pinning_the_same_words_are_not_a_collision(self):
        a = write_dispatch.run_child({"verb": "pin", "text": "same words", "session_id": "s1"})
        b = write_dispatch.run_child({"verb": "pin", "text": "same words", "session_id": "s2"})
        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual(len(self._pins()), 2)

    def test_two_overlapping_identical_pins_land_one_record_and_the_decisive_check_fires_under_the_lock(self):
        """Two children race for the same pin. A parent-side-only check would let BOTH through: before either
        child runs, the parent's view of the ledger holds no duplicate for either request. The decisive check
        is the one the child runs while it HOLDS the write lock: the first child is held open at that check
        while the second arrives, contends for the lock, and — once the first has committed — finds the
        duplicate and commits nothing. Exactly one record, exactly one 'already pinned' answer."""
        target = ledger.ledger_path()
        requests = [{"verb": "pin", "text": "race me", "session_id": "s1"} for _ in range(2)]
        # The parent-side-only check: nothing is pinned yet, so it clears BOTH requests. It cannot serialize.
        for request in requests:
            self.assertIsNone(pins._find_duplicate_pin(request["text"], request["session_id"], path=target))

        real_check = pins._find_duplicate_pin
        entered = threading.Event()
        release = threading.Event()
        check_threads = []

        def gated_check(cleaned, session_identity, *, path):
            check_threads.append(threading.get_ident())
            if len(check_threads) == 1:
                entered.set()                                          # first child: inside the lock, at the check
                self.assertTrue(release.wait(10))                     # ...held there until the second is queued
            return real_check(cleaned, session_identity, path=path)

        results = []

        def run(request):
            results.append(write_dispatch.run_child(request))

        with mock.patch.object(pins, "_find_duplicate_pin", gated_check):
            first = threading.Thread(target=run, args=(requests[0],))
            first.start()
            self.assertTrue(entered.wait(10))                          # first child holds the lock
            second = threading.Thread(target=run, args=(requests[1],))
            second.start()
            time.sleep(0.25)                                           # second child is now contending
            self.assertEqual(len(check_threads), 1)                    # ...and has NOT reached the check
            self.assertEqual(self._pins(), [])                         # nothing committed while both are open
            release.set()
            first.join(30)
            second.join(30)

        self.assertEqual(len(results), 2)
        stored = self._pins()
        self.assertEqual(len(stored), 1)                               # exactly one record
        self.assertEqual({r["id"] for r in results}, {stored[0][records.RECORD_ID_KEY]})
        self.assertEqual([bool(r.get("already_pinned")) for r in sorted(results, key=lambda r: bool(r.get("already_pinned")))],
                         [False, True])                                # one fresh commit, one already-pinned
        self.assertEqual(len(check_threads), 2)                        # both decisive checks ran under the lock
        self.assertNotEqual(check_threads[0], check_threads[1])        # ...each in its own child, in turn

    def test_two_identical_pins_that_omit_session_id_collide(self):
        # An omitted session_id normalizes to ONE explicit no-session identity, so two no-session pins of the
        # same text are the same pin — not two records that merely share no session.
        a = write_dispatch.run_child({"verb": "pin", "text": "no session here"})
        b = write_dispatch.run_child({"verb": "pin", "text": "no session here"})
        c = write_dispatch.run_child({"verb": "pin", "text": "no session here", "session_id": None})
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(a["id"], c["id"])
        self.assertTrue(b["already_pinned"] and c["already_pinned"])
        self.assertEqual(len(self._pins()), 1)
        self.assertEqual(pins._session_identity(None), pins._session_identity(""))

    def test_the_normalization_rule_has_exactly_three_equivalence_classes(self):
        # Unicode NFC, trailing/leading whitespace stripped, internal whitespace runs collapsed to one space —
        # each on its own and all together — land on the SAME pin; a change to the words does not.
        composed = "caf\u00e9 au lait"                                 # é precomposed
        decomposed = "cafe\u0301 au lait"                              # e + combining acute (NFD)
        self.assertNotEqual(composed, decomposed)
        first = write_dispatch.run_child({"verb": "pin", "text": composed, "session_id": "s1"})
        for variant in (decomposed,                                    # NFC folding
                        "  " + composed + "\t\n",                     # outer whitespace stripped
                        "caf\u00e9   au\t\tlait",                     # internal runs collapsed
                        " \n cafe\u0301\t au  \n lait "):             # all three at once
            with self.subTest(variant=variant):
                out = write_dispatch.run_child({"verb": "pin", "text": variant, "session_id": "s1"})
                self.assertEqual(out["id"], first["id"])
                self.assertTrue(out["already_pinned"])
        self.assertEqual(len(self._pins()), 1)
        other = write_dispatch.run_child({"verb": "pin", "text": "caf\u00e9 au laid", "session_id": "s1"})
        self.assertNotEqual(other["id"], first["id"])                  # different words are a different pin
        self.assertEqual(len(self._pins()), 2)
        self.assertEqual(pins._normalized_pin_text(" \n cafe\u0301\t au  \n lait "), composed)


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
        # The parent classifies exactly this stdout, and gets the child's own response back.
        outcome = write_dispatch._classify_outcome(
            printed, returncode=code, verb="pin", request={"verb": "pin", "text": "a round trip"},
            read_back=write_dispatch._ledger_read_back, child_alive=False)
        self.assertEqual(outcome["outcome"], "committed")
        self.assertEqual(outcome["response"]["text"], "a round trip")
        self.assertEqual([r[records.RECORD_ID_KEY] for r in self._pins()], [outcome["response"]["id"]])

    def test_empty_stdin_is_read_as_an_empty_request_and_faults_on_the_missing_verb(self):
        with mock.patch.object(sys, "stdin", io.StringIO("")):
            with self.assertRaises(write_dispatch.DispatchFaulted):
                write_dispatch.main([])


if __name__ == "__main__":
    unittest.main()
