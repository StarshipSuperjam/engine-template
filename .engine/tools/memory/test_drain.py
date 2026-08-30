#!/usr/bin/env python3
"""Tests for the session-start transcript drain (issue StarshipSuperjam/engine-template#1158).

The claim under test is the one the whole availability-first design rests on: an unqualified session loses
nothing, because it writes nothing AND leaves its cursor where it found it, and a later qualified session
picks the tail up out of the harness's own transcript.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import capture, drain, ledger, mutation_authority  # noqa: E402


def _transcript(path: str, turns) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        for speaker, text in turns:
            handle.write(json.dumps({"type": speaker, "message": {"role": speaker, "content": text}}) + "\n")


class _DrainBase(unittest.TestCase):
    """A private store plus a private transcript home, both inside one temp tree."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self._tmp.name)
        self.project = os.path.join(self.root, "project")
        self.home = os.path.join(self.root, "home")
        # The harness names a project's transcript directory after its path with separators flattened.
        self.sessions_dir = os.path.join(self.home, "projects", drain._slug(self.project))
        os.makedirs(self.sessions_dir)
        os.makedirs(os.path.join(self.project, ".engine", "memory"))
        self._patches = [
            mock.patch.dict(os.environ, {capture.TRANSCRIPT_DIR_ENV: self.home}, clear=False),
            mock.patch.object(ledger, "_git_common_root", return_value=self.project),
            mock.patch.object(ledger, "ledger_dir",
                              side_effect=lambda cwd=None: os.path.join(self.project, ".engine", "memory")),
            mock.patch.object(ledger, "ledger_path",
                              side_effect=lambda cwd=None: os.path.join(
                                  self.project, ".engine", "memory", "ledger.ndjson")),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self._patches):
            patch.stop()
        self._tmp.cleanup()

    def write_session(self, session_id: str, turns) -> str:
        path = os.path.join(self.sessions_dir, f"{session_id}.jsonl")
        _transcript(path, turns)
        return path

    def ledger_texts(self):
        try:
            return [r.get("text", "") for r in ledger.read(path=ledger.ledger_path()).records]
        except Exception:  # noqa: BLE001
            return []

    def cursor(self, session_id: str) -> int:
        return drain._cursor_state(ledger.ledger_dir()).get(session_id, 0)


class UnqualifiedSessionWritesNothing(_DrainBase):
    def test_a_refused_capture_leaves_both_the_ledger_and_the_cursor_untouched(self):
        """The load-bearing half. If the cursor moved without the append, the tail would look captured and
        the drain would never come back for it — silent, permanent loss.

        This drives the REAL refusal, not a stand-in for one. It used to mock `ledger.append` into raising an
        arbitrary error, which only re-proved capture's generic fail-soft behaviour; the deliverable review
        was right that the test's name promised more than it did. Turning the source-bound test adapter off
        is what makes a checked-in test module unqualified like any other caller, so the guard refuses for
        the actual reason the shipped code refuses.
        """
        path = self.write_session("sess-unqualified", [("user", "a decision worth keeping")])
        with mock.patch.object(mutation_authority, "_test_adapter_allowed", return_value=False):
            appended = capture.capture_turn_delta(
                {"session_id": "sess-unqualified", "transcript_path": path})
        self.assertEqual(appended, 0)
        self.assertEqual(self.ledger_texts(), [])
        self.assertEqual(self.cursor("sess-unqualified"), 0)

    def test_the_refusal_is_a_no_op_return_not_an_exception_out_of_the_leaf(self):
        """`capture_turn_delta` documents itself as never raising into its caller, and both live callers rely
        on that. Guarding the leaf's OUTER boundary broke it: a qualification refusal came out as an
        exception, and the only thing keeping that from surfacing was defensive `try` blocks in the two
        callers — external, incidental, and not the contract the docstring states. The guard belongs one
        frame in, on `_capture`, where the refusal lands inside the leaf's own fail-soft body."""
        path = self.write_session("sess-noraise", [("user", "must not raise")])
        with mock.patch.object(mutation_authority, "_test_adapter_allowed", return_value=False):
            try:
                appended = capture.capture_turn_delta(
                    {"session_id": "sess-noraise", "transcript_path": path})
            except Exception as exc:  # noqa: BLE001 — the assertion IS that this does not happen
                self.fail(f"capture_turn_delta raised {type(exc).__name__}: {exc}")
        self.assertEqual(appended, 0)

    def test_the_tail_survives_in_the_transcript_and_the_drain_finds_it(self):
        path = self.write_session("sess-later", [("user", "the marmalade migration decision")])
        with mock.patch.object(capture.ledger, "append", side_effect=RuntimeError("unqualified")):
            capture.capture_turn_delta({"session_id": "sess-later", "transcript_path": path})
        self.assertEqual(drain.backlog()["sessions_waiting"], 1)
        receipt = drain.drain()
        self.assertEqual(receipt["sessions_drained"], 1)
        self.assertTrue(any("marmalade migration" in text for text in self.ledger_texts()))


class DrainBehaviour(_DrainBase):
    def test_it_captures_exactly_the_uncaptured_tail(self):
        path = self.write_session("sess-tail", [("user", "first turn alpha"),
                                                ("assistant", "second turn beta")])
        capture.capture_turn_delta({"session_id": "sess-tail", "transcript_path": path})
        _transcript(path, [("user", "first turn alpha"), ("assistant", "second turn beta"),
                           ("user", "third turn gamma")])
        before = len(self.ledger_texts())
        drain.drain()
        after = self.ledger_texts()
        self.assertEqual(len(after) - before, 1)
        self.assertTrue(any("third turn gamma" in text for text in after))
        self.assertEqual(sum("first turn alpha" in text for text in after), 1)   # not re-captured

    def test_it_is_idempotent(self):
        self.write_session("sess-idem", [("user", "only once please")])
        drain.drain()
        first = self.ledger_texts()
        second_receipt = drain.drain()
        self.assertEqual(second_receipt["sessions_drained"], 0)
        self.assertEqual(self.ledger_texts(), first)

    def test_drained_records_carry_their_late_origin(self):
        self.write_session("sess-origin", [("user", "recovered afterwards")])
        drain.drain()
        rows = [r for r in ledger.read(path=ledger.ledger_path()).records
                if "recovered afterwards" in (r.get("text") or "")]
        self.assertTrue(rows)
        self.assertIn(drain.ORIGIN_DRAIN, rows[0].get("tags", []))

    def test_a_live_session_is_left_to_capture_its_own_turns(self):
        self.write_session("sess-live", [("user", "still talking")])
        with mock.patch.dict(os.environ, {capture.SESSION_ENV: "sess-live"}, clear=False):
            receipt = drain.drain()
        self.assertEqual(receipt["sessions_drained"], 0)
        self.assertEqual(self.ledger_texts(), [])

    def test_a_cleaned_up_transcript_for_a_captured_session_is_not_called_a_loss(self):
        """Caught by running the drain against this machine's real store: 188 cursors had no surviving
        transcript, and every one was an already-captured session. Reporting those as permanent gaps would
        have announced a false alarm at every session start."""
        path = self.write_session("sess-gone", [("user", "words already captured")])
        capture.capture_turn_delta({"session_id": "sess-gone", "transcript_path": path})
        os.remove(path)
        receipt = drain.drain()
        self.assertEqual(receipt["gaps"], [])

    def test_a_present_but_unreadable_transcript_is_a_reported_gap(self):
        path = os.path.join(self.sessions_dir, "sess-broken.jsonl")
        with open(path, "wb") as handle:
            handle.write(b"\xff\xfe not json at all\n")
        with mock.patch.object(drain, "_message_count", return_value=None):
            receipt = drain.drain()
        self.assertIn("sess-broken", [g["session_id"] for g in receipt["gaps"]])
        self.assertEqual(receipt["gaps"][0]["reason"], "transcript-unreadable")

    def test_another_projects_transcripts_are_never_swept_in(self):
        other = os.path.join(self.home, "projects", "-Users-someone-else-other-project")
        os.makedirs(other)
        _transcript(os.path.join(other, "sess-foreign.jsonl"), [("user", "another project's secret")])
        self.write_session("sess-ours", [("user", "our own note")])
        drain.drain()
        texts = self.ledger_texts()
        self.assertTrue(any("our own note" in text for text in texts))
        self.assertFalse(any("another project" in text for text in texts))

    def test_an_unrecognisable_transcript_directory_is_excluded(self):
        self.assertFalse(drain._belongs_to_this_project(
            os.path.join(self.home, "projects", "not-a-slug"), self.project))
        self.assertFalse(drain._belongs_to_this_project(
            os.path.join(self.home, "projects", "-Users-shanekidd-elsewhere"), self.project))
        self.assertTrue(drain._belongs_to_this_project(self.sessions_dir, self.project))

    def test_a_worktree_of_this_project_is_included(self):
        worktree = os.path.join(self.project, ".claude", "worktrees", "feature")
        self.assertTrue(drain._belongs_to_this_project(
            os.path.join(self.home, "projects", drain._slug(worktree)), self.project))

    def test_a_sibling_checkout_whose_name_merely_extends_this_one_is_excluded(self):
        """The defect the deliverable review caught, pinned in both directions.

        The slug turns a real hyphen into a separator, so the old flatten-and-prefix test could not tell
        `<project>-lane-c` (a SIBLING) from `<project>/lane/c` (a subdirectory) and admitted both. Run
        against this machine it matched a separate checkout with a live 4.8 MB transcript, which the first
        qualified session start would have filed into this project's ledger under this project's ids.
        """
        for suffix in ("-lane-c", "-fork", "-2", "-clientwork"):
            with self.subTest(suffix=suffix):
                self.assertFalse(drain._belongs_to_this_project(
                    os.path.join(self.home, "projects", drain._slug(self.project) + suffix), self.project))

    def test_a_sibling_checkouts_transcripts_are_never_swept_in(self):
        """The same thing end to end: not just the predicate, but the drain that uses it."""
        sibling = os.path.join(self.home, "projects", drain._slug(self.project) + "-lane-c")
        os.makedirs(sibling)
        _transcript(os.path.join(sibling, "sess-sibling.jsonl"),
                    [("user", "the sibling checkout's private note")])
        self.write_session("sess-ours", [("user", "our own note")])
        drain.drain()
        texts = self.ledger_texts()
        self.assertTrue(any("our own note" in text for text in texts))
        self.assertFalse(any("sibling checkout" in text for text in texts))

    def test_a_project_path_containing_a_dot_still_matches_its_own_transcripts(self):
        """The harness turns a dot into a hyphen too. Without that substitution such a project matched
        nothing — never drained its own tails, and reported an empty backlog while doing it, which is the
        one combination that hides the loss the backlog exists to reveal."""
        dotted = os.path.join(self.root, "proj.v2")
        self.assertTrue(drain._belongs_to_this_project(
            os.path.join(self.home, "projects", drain._slug(dotted)), dotted))

    def test_the_backlog_reports_count_and_age(self):
        self.write_session("sess-a", [("user", "one")])
        self.write_session("sess-b", [("user", "two")])
        report = drain.backlog()
        self.assertEqual(report["sessions_waiting"], 2)
        self.assertIsNotNone(report["oldest_waiting_age_days"])
        drain.drain()
        self.assertEqual(drain.backlog()["sessions_waiting"], 0)

    def test_erased_content_is_out_of_the_drains_reach_by_construction(self):
        """An erasure can only target something already captured — at or below the cursor — and the drain
        only ever reads above it. Proven here rather than argued: capture a turn, then re-run the drain over
        the same transcript and check the ledger did not gain a second copy for it to erase twice."""
        path = self.write_session("sess-erase", [("user", "sensitive thing")])
        capture.capture_turn_delta({"session_id": "sess-erase", "transcript_path": path})
        captured = [t for t in self.ledger_texts() if "sensitive thing" in t]
        self.assertEqual(len(captured), 1)
        drain.drain()
        self.assertEqual(len([t for t in self.ledger_texts() if "sensitive thing" in t]), 1)
        separation = drain.erasure_is_out_of_reach()
        self.assertTrue(separation["readable"])
        self.assertTrue(separation["drain_reads_only_above_the_cursor"])

    def test_a_drain_and_a_live_capture_interleave_without_duplication(self):
        """The mixed case: one session captures its own turn while the drain catches up another. They share
        capture's single advisory lock, so they serialise at the transaction boundary rather than inside it."""
        import concurrent.futures
        stale = self.write_session("sess-stale", [("user", "older unsaved turn")])
        live = self.write_session("sess-fresh", [("user", "current turn")])
        self.assertTrue(os.path.exists(stale))
        # Both sides are submitted as functions DEFINED HERE, so each worker thread's stack carries a frame
        # from this checked-in test module — which is what the mutation authority's source-bound adapter
        # looks for. Submitting the library functions directly would leave the workers unqualified and this
        # would test the refusal path instead of the interleaving.
        def run_drain():
            return drain.drain()

        def run_live_capture():
            return capture.capture_turn_delta({"session_id": "sess-fresh", "transcript_path": live})

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run_drain), pool.submit(run_live_capture)]
            [future.result() for future in futures]
        texts = self.ledger_texts()
        self.assertEqual(sum("older unsaved turn" in text for text in texts), 1)
        self.assertEqual(sum("current turn" in text for text in texts), 1)


class DrainIsBounded(_DrainBase):
    """A session start may not be held open indefinitely.

    Everything else on this boot path is wall-clock bounded — ambient qualification carries an explicit
    two-second budget — and the drain was bounded only by COUNT. Measured against this machine's real
    history, scrubbing and chunking 49 transcripts took over a second before any fsync'd write; at the
    500-transcript cap with large files that is minutes of a session refusing to begin, with no abort path.
    """

    def test_it_stops_at_the_deadline_and_leaves_the_rest_for_next_time(self):
        for n in range(4):
            self.write_session(f"sess-{n}", [("user", f"note number {n}")])
        with mock.patch.object(drain, "MAX_DRAIN_SECONDS", 0.0):
            receipt = drain.drain()
        self.assertTrue(receipt["stopped_early"])
        self.assertEqual(receipt["sessions_drained"], 0)
        self.assertEqual(self.ledger_texts(), [])
        # …and nothing is lost: the next pass, unbounded, still finds every one of them.
        again = drain.drain()
        self.assertEqual(again["sessions_drained"], 4)
        self.assertFalse(again["stopped_early"])


class QualificationGate(_DrainBase):
    def test_an_unqualified_session_does_not_drain(self):
        self.write_session("sess-x", [("user", "waiting")])
        with mock.patch.object(drain, "is_qualified", return_value=False):
            self.assertIsNone(drain.drain_if_qualified())
        self.assertEqual(self.ledger_texts(), [])

    def test_a_qualified_session_drains(self):
        self.write_session("sess-y", [("user", "caught up now")])
        with mock.patch.object(drain, "is_qualified", return_value=True):
            receipt = drain.drain_if_qualified()
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["sessions_drained"], 1)

    def test_a_fault_inside_the_drain_is_a_receipt_not_an_exception(self):
        with mock.patch.object(drain, "is_qualified", return_value=True), \
                mock.patch.object(drain, "drain", side_effect=RuntimeError("boom")):
            receipt = drain.drain_if_qualified()
        self.assertEqual(receipt["error"], "RuntimeError")
        self.assertEqual(receipt["records_appended"], 0)


if __name__ == "__main__":
    unittest.main()
