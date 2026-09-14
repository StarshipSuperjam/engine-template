"""Unit tests for pins.py — durable operator intent as a record-type in the one substrate.

These exercise the real ledger and the real recall paths: a pin is only useful if `search` finds it, and the
properties that matter most (scrubbed on the way in, findable with no rebuild, removable and restorable) are
the ones a plausible-but-wrong implementation would quietly fail.
"""

from __future__ import annotations

import base64
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import forget, index, ledger, pins, records  # noqa: E402


class _Base(unittest.TestCase):
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


class PinTests(_Base):
    def test_a_pin_is_findable_by_search_with_no_rebuild_in_between(self):
        # The same trap the withhold verbs face: `ledger.append` leaves the generation stamp alone and
        # `index.extend` takes captured turns only, so an implementation that merely appended would leave the
        # operator's brand-new pin missing from a search that reports itself authoritative.
        index.rebuild()
        pins.add("Always ask before filing an issue.")
        self.assertEqual(len(index.search("filing").records), 1)
        self.assertEqual(len(index.search("filing", force_scan=True).records), 1)

    def test_dedup_returns_the_existing_pin_on_the_same_session_lane_instead_of_a_second(self):
        # Round 4, TI-2: the direct API's dedup path, outside the dispatch child. Same normalized text on the
        # same session lane is one pin; the same text from another session, or with dedup off, is a new one.
        first = pins.add("Always  ask before filing an issue.", session_id="s-1", dedup=True)
        again = pins.add("  Always ask before filing an issue. ", session_id="s-1", dedup=True)
        self.assertEqual(again[records.RECORD_ID_KEY], first[records.RECORD_ID_KEY])
        self.assertEqual(len(pins.list_pins()), 1)
        other = pins.add("Always ask before filing an issue.", session_id="s-2", dedup=True)
        self.assertNotEqual(other[records.RECORD_ID_KEY], first[records.RECORD_ID_KEY])
        self.assertEqual(len(pins.list_pins()), 2)
        pins.add("Always ask before filing an issue.", session_id="s-1")          # dedup off: appends
        self.assertEqual(len(pins.list_pins()), 3)

    def test_add_honours_a_pre_minted_id_and_emits_begin_then_committed_or_already_pinned(self):
        # Round 4, TI-2: `accepted_id` becomes the record's own id and `emit` sees "begin" before the lock
        # and "committed" after the append; a dedup hit emits "already_pinned" carrying the existing record.
        events = []
        record = pins.add("Prefer the smallest safe change.", session_id="s-1", accepted_id="acc-pin-1",
                          dedup=True, emit=lambda e, p: events.append((e, p)))
        self.assertEqual(record[records.RECORD_ID_KEY], "acc-pin-1")
        self.assertEqual([e for e, _ in events], ["begin", "committed"])
        self.assertEqual(events[0][1][records.RECORD_ID_KEY], "acc-pin-1")
        self.assertEqual(events[1][1]["record"], record)
        self.assertGreater(events[1][1]["bytes"], 0)
        events.clear()
        duplicate = pins.add("Prefer the smallest safe change.", session_id="s-1", accepted_id="acc-pin-2",
                             dedup=True, emit=lambda e, p: events.append((e, p)))
        self.assertEqual(duplicate[records.RECORD_ID_KEY], "acc-pin-1")     # the existing pin, not acc-pin-2
        self.assertEqual([e for e, _ in events], ["begin", "already_pinned"])
        self.assertEqual(events[1][1]["record"], duplicate)
        self.assertEqual(len(pins.list_pins()), 1)

    def test_a_failing_emit_never_turns_a_landed_pin_into_a_refusal(self):
        def broken(event, payload):
            if event == "committed":
                raise BrokenPipeError("parent went away")

        record = pins.add("Keep the operator informed.", emit=broken)
        self.assertEqual(len(pins.list_pins()), 1)
        self.assertEqual(pins.list_pins()[0][records.RECORD_ID_KEY], record[records.RECORD_ID_KEY])

    def test_secret_shaped_text_is_scrubbed_before_it_is_stored(self):
        # A pin does not travel through capture, so capture's scrub never sees it — and a pinned credential
        # would be read into the briefing of every future session. There must be no unscrubbed copy anywhere.
        record = pins.add("the key is sk-ant-api03-" + "A" * 32)
        self.assertNotIn("sk-ant-api03", record["text"])
        stored = [r for r in ledger.iter_records() if r.get("kind") == records.PIN_KIND]
        self.assertEqual(len(stored), 1)
        self.assertNotIn("sk-ant-api03", stored[0]["text"])

    def test_every_pin_records_the_route_it_arrived_by(self):
        # Never an authority claim — the field exists so no reader can present a pin as verified wording.
        self.assertEqual(pins.add("via the tool")[records.PIN_VIA_KEY], records.PIN_VIA_ASSISTANT)
        self.assertEqual(pins.add("typed", via=records.PIN_VIA_CLI)[records.PIN_VIA_KEY], records.PIN_VIA_CLI)
        self.assertEqual(pins.add("nonsense", via="operator-swears-it")[records.PIN_VIA_KEY],
                         records.PIN_VIA_ASSISTANT)      # an unknown route is never trusted upward

    def test_the_route_and_the_source_session_are_not_searchable_words(self):
        # "assistant" and "cli" are ordinary words: indexed, they would make every pin match a search for
        # either, and the source session is uuid hex whose fragments are real words.
        pins.add("keep the onboarding copy short", session_id="deadbeefcafe")
        index.rebuild()
        self.assertEqual(index.search("assistant").records, [])
        self.assertEqual(index.search("deadbeefcafe").records, [])
        self.assertEqual(len(index.search("onboarding").records), 1)

    def test_removing_a_pin_withholds_it_rather_than_deleting_it(self):
        record = pins.add("a preference that changed")
        rid = record[records.RECORD_ID_KEY]
        index.rebuild()
        pins.remove(rid)
        self.assertEqual(index.search("preference").records, [])
        self.assertEqual(pins.list_pins(), [])
        self.assertEqual(forget.withheld_report()["notes"][0]["id"], rid)
        # Still in the ledger, byte for byte — removal is a withhold, so restore is the operator's undo.
        self.assertIn(rid, {r.get(records.RECORD_ID_KEY) for r in ledger.iter_records()})
        forget.restore(record_id=rid)
        self.assertEqual(len(pins.list_pins()), 1)
        self.assertEqual(len(index.search("preference").records), 1)

    def test_list_reads_through_the_same_liveness_as_recall(self):
        # One definition of "live". A second one here could disagree with search and leave the operator told
        # something is pinned that no recall would ever surface.
        kept = pins.add("kept")
        gone = pins.add("gone")
        pins.remove(gone[records.RECORD_ID_KEY])
        self.assertEqual([r[records.RECORD_ID_KEY] for r in pins.list_pins()],
                         [kept[records.RECORD_ID_KEY]])

    def test_list_is_newest_first_and_honours_a_limit(self):
        first = pins.add("older", now=1000)
        second = pins.add("newer", now=2000)
        self.assertEqual([r[records.RECORD_ID_KEY] for r in pins.list_pins()],
                         [second[records.RECORD_ID_KEY], first[records.RECORD_ID_KEY]])
        self.assertEqual([r[records.RECORD_ID_KEY] for r in pins.list_pins(limit=1)],
                         [second[records.RECORD_ID_KEY]])

    def test_nothing_is_saved_when_a_pin_is_refused(self):
        # A refusal must leave no half-record behind, and must not be silent: the operator asked for something
        # to be remembered, so a quiet decline leaves them believing it was.
        for bad in ("", "   ", None, "x" * (pins.MAX_PIN_CHARS + 1)):
            with self.assertRaises(pins.PinRefused):
                pins.add(bad)
        self.assertEqual([r for r in ledger.iter_records() if r.get("kind") == records.PIN_KIND], [])

    def test_an_unexpected_write_fault_is_refused_without_its_text_and_kept_as_raw_detail(self):
        # The catch-all used to splice the underlying exception into the operator sentence (#1196).
        from unittest import mock
        raw = "disk went away at /Users/someone/.engine/memory/ledger.ndjson"
        with mock.patch.object(ledger, "append", side_effect=OSError(raw)):
            with self.assertRaises(pins.PinRefused) as caught:
                pins.add("a pin that cannot land")
        message = str(caught.exception)
        self.assertNotIn(raw, message)
        self.assertNotIn("/Users", message)
        self.assertIn("nothing was saved", message)
        self.assertIn("/engine-status", message)
        self.assertEqual(caught.exception.raw_detail, raw)

    # -- round 8 (R8 DH-1): the writer reconciles a fault raised after the bytes may have landed --------------
    def _flush_fails_after_the_bytes_land(self):
        import errno
        from unittest import mock
        return mock.patch.object(ledger.os, "fsync", side_effect=OSError(errno.EIO, "injected: the flush failed"))

    def test_an_io_error_in_the_flush_after_the_bytes_landed_returns_the_saved_pin(self):
        # The bytes are on disk when the flush fails; the pin is readable, so it is returned as saved.
        with self._flush_fails_after_the_bytes_land():
            record = pins.add("landed before the flush failed")
        self.assertEqual(record["text"], "landed before the flush failed")
        self.assertEqual([r["text"] for r in pins.list_pins()], ["landed before the flush failed"])

    def test_the_command_line_says_pinned_not_nothing_saved_when_only_the_flush_failed(self):
        import io
        from contextlib import redirect_stdout
        buffer = io.StringIO()
        with self._flush_fails_after_the_bytes_land(), redirect_stdout(buffer):
            code = pins.main(["add", "a command-line pin whose flush failed"])
        self.assertEqual(code, 0)
        self.assertIn("Pinned [", buffer.getvalue())
        self.assertNotIn("Not saved", buffer.getvalue())
        # R9 DH-1: saved, but never reported as a CLEAN save — the failed flush step is named.
        self.assertIn(pins.UNFLUSHED_NOTE, buffer.getvalue())
        self.assertEqual([r["text"] for r in pins.list_pins()], ["a command-line pin whose flush failed"])

    def test_a_clean_save_carries_no_flush_note_and_a_landed_despite_fault_save_carries_the_fault(self):
        # R9 DH-1: the disclosure travels on the committed line, so every route (the dispatched child, the
        # command line) can tell a clean save from one whose flush failed after the bytes landed.
        import io
        from contextlib import redirect_stdout
        lines = []
        pins.add("a clean pin", emit=lambda kind, payload: lines.append((kind, payload)))
        self.assertEqual([k for k, _ in lines], ["begin", "committed"])
        self.assertNotIn("fault", lines[1][1])
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(pins.main(["add", "another clean pin"]), 0)
        self.assertNotIn("not cleanly", buffer.getvalue())
        lines.clear()
        with self._flush_fails_after_the_bytes_land():
            record = pins.add("a pin whose flush failed", emit=lambda kind, payload: lines.append((kind, payload)))
        self.assertEqual([k for k, _ in lines], ["begin", "committed"])
        self.assertEqual(lines[1][1]["record"], record)
        self.assertIsNone(lines[1][1]["bytes"])                      # the byte length is unknown
        self.assertIn("injected: the flush failed", lines[1][1]["fault"])

    def test_landed_despite_answers_from_the_real_ledger_in_all_three_states(self):
        # R9 DH-3: the writer-side helper against a real ledger — a record that is there, one that is not
        # (searched, absent), and a ledger that cannot be read (None, never "absent").
        record = pins.add("a pin that is there")
        target = ledger.ledger_path()
        self.assertIs(pins._landed_despite(record, target), True)
        self.assertIs(pins._landed_despite({records.RECORD_ID_KEY: "never-written"}, target), False)
        self.assertIs(pins._landed_despite(None, target), False)
        unreadable = os.path.join(os.path.dirname(target), "a-directory")
        os.mkdir(unreadable)
        self.assertIsNone(pins._landed_despite(record, unreadable))

    def test_removing_a_pin_from_the_command_line_tells_the_truth_about_its_own_flush(self):
        # R9 DH-2: the remove headline. Only the flush failed: removed, with the flush note. The ledger cannot
        # be read back: "Not confirmed", never "Not removed" over a sentence that says it is not known.
        import io
        from contextlib import redirect_stdout
        from unittest import mock
        rid = pins.add("a pin to remove")[records.RECORD_ID_KEY]
        buffer = io.StringIO()
        with self._flush_fails_after_the_bytes_land(), redirect_stdout(buffer):
            code = pins.main(["remove", rid])
        self.assertEqual(code, 0)
        self.assertTrue(buffer.getvalue().startswith("Removed from recall."))
        self.assertIn(forget.UNFLUSHED_NOTE, buffer.getvalue())
        self.assertEqual(pins.list_pins(), [])
        other = pins.add("a pin nobody can confirm removing")[records.RECORD_ID_KEY]
        unreadable = mock.patch.object(ledger, "find_raw_record",
                                       side_effect=ledger.LedgerUnreadable("injected: cannot read back"))
        buffer = io.StringIO()
        with self._flush_fails_after_the_bytes_land(), unreadable, redirect_stdout(buffer):
            code = pins.main(["remove", other])
        self.assertEqual(code, 1)
        self.assertTrue(buffer.getvalue().startswith("Not confirmed: "))
        self.assertNotIn("Not removed", buffer.getvalue())
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(pins.main(["remove", "no-such-pin"]), 1)
        self.assertTrue(buffer.getvalue().startswith("Not removed: "))

    def test_a_flush_failure_with_an_unreadable_ledger_is_unconfirmed_never_nothing_saved(self):
        import io
        from contextlib import redirect_stdout
        from unittest import mock
        unreadable = mock.patch.object(ledger, "find_raw_record",
                                       side_effect=ledger.LedgerUnreadable("injected: cannot read back"))
        with self._flush_fails_after_the_bytes_land(), unreadable:
            with self.assertRaises(pins.PinUnconfirmed) as caught:
                pins.add("a pin nobody can confirm")
        message = str(caught.exception)
        self.assertIsInstance(caught.exception, pins.PinRefused)      # every existing handler still catches it
        self.assertIn("not confirmed", message)
        self.assertNotIn("nothing was saved", message.lower())
        self.assertIn("/engine-status", message)
        buffer = io.StringIO()
        with self._flush_fails_after_the_bytes_land(), unreadable, redirect_stdout(buffer):
            code = pins.main(["add", "a command-line pin nobody can confirm"])
        self.assertEqual(code, 1)
        self.assertTrue(buffer.getvalue().startswith("Not confirmed: "))
        self.assertNotIn("Not saved", buffer.getvalue())

    def test_a_fault_before_the_append_still_says_nothing_was_saved(self):
        # The reconciliation only ever CONFIRMS a landed write: with nothing appended, the ledger is searched,
        # holds nothing, and the plain refusal stands.
        from unittest import mock
        with mock.patch.object(ledger, "append", side_effect=OSError("disk went away")):
            with self.assertRaises(pins.PinRefused) as caught:
                pins.add("a pin that never reached the disk")
        self.assertNotIsInstance(caught.exception, pins.PinUnconfirmed)
        self.assertIn("nothing was saved", str(caught.exception))
        self.assertEqual(pins.list_pins(), [])

    def test_an_over_long_pin_is_refused_rather_than_truncated(self):
        with self.assertRaises(pins.PinRefused) as caught:
            pins.add("y" * (pins.MAX_PIN_CHARS + 1))
        self.assertIn("Nothing was saved", str(caught.exception))

    def test_shell_shaped_text_round_trips_only_through_urlsafe_base64(self):
        text = 'Keep $(touch /tmp/nope), `also-nope`, $TOKEN, and "quotes" literal.'
        encoded = base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")
        self.assertEqual(pins.main(["add-base64", encoded]), 0)
        self.assertEqual(pins.list_pins()[0]["text"], text)
        self.assertEqual(pins.main(["add-base64", encoded.rstrip("=")]), 1)

if __name__ == "__main__":
    unittest.main()
