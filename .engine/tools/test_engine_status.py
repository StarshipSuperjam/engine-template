"""Tests for `/engine-status`'s tool (issue #83) — the operator's on-demand pull view.

Verifies: the thin reuse — `render()` is exactly `boot.render_dashboard` over `boot.gather_signals`, with
the session id passed through (so the real stance shows); the operator-facing dashboard markers are carried;
the always-answers guarantee (a renderer failure degrades to a plain line, never raises); the CLI
(`main([])` prints; `--session X` is resolved and passed through; `demo` runs and shows a clearly-labelled
made-up EXAMPLE so a real alarm is never mistaken for the operator's own); and that the strings THIS tool
adds leak no raw code identifier or exception fragment (a leaked internal would be a bug, not a word choice —
the dashboard body itself is boot's, vetted in test_boot). gather_signals (boot's I/O boundary) is faked so the tests are deterministic and offline;
the REAL render/degrade/demo logic runs ([[demo-must-exercise-real-logic]]).
"""
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_status as es  # noqa: E402
import boot  # noqa: E402
import test_boot  # noqa: E402  (reuse `_signals(**over)`, the COMPLETE signals dict render_dashboard needs)


_BACKLOG_PATCH = None


def setUpModule():
    """Keep this module off the developer's real machine.

    `_activation_state` calls `_capture_backlog`, which walks the harness home counting transcripts — so
    every test that rendered the dashboard was reading this machine's actual conversation history, at about a
    second each. The tests that care about the backlog build their own fixture states and call the renderer
    directly; nothing here needs the real one.
    """
    global _BACKLOG_PATCH
    _BACKLOG_PATCH = mock.patch.object(
        es, "_capture_backlog",
        return_value={"sessions_waiting": 0, "oldest_waiting_age_days": None, "partial": False})
    _BACKLOG_PATCH.start()


def tearDownModule():
    if _BACKLOG_PATCH is not None:
        _BACKLOG_PATCH.stop()


# A raw code identifier or exception fragment surfacing in operator text means an internal name or a traceback
# leaked there — a correctness bug, not a word choice. This guards SYMBOLS, not vocabulary, so it is not a
# banned-word list: each name below is a real internal of this tool's render path.
_RAW_CODE_IDENTIFIERS = ("gather_signals", "render_dashboard", "subscript", "keyerror")


class TestRenderReusesBootSeam(unittest.TestCase):
    def test_render_is_the_dashboard_over_gathered_signals(self):
        # The whole value of the slice: ONE renderer, two callers. render() must be byte-identical to
        # render_dashboard over the gathered signals — never a second, drifting status view.
        known = test_boot._signals()
        with mock.patch.object(boot, "gather_signals", return_value=known), \
                mock.patch.object(es, "_qualification_health", return_value=None), \
                mock.patch.object(es, "_activation_state", return_value=None):
            out = es.render()
        self.assertEqual(out, boot.render_dashboard(known))

    def test_qualification_and_coverage_are_appended_after_the_shared_dashboard(self):
        # The two memory-qualification sections are ADDITIONS below the shared dashboard, never a second
        # rendering of it: the dashboard body must still be byte-identical and come first.
        known = test_boot._signals()
        coverage = {"readable": True, "state": "blocked", "total": 3, "uncovered": 2,
                    "sample": ["main [abc]: missing (.engine/tools/accepted_hook_dispatch.py)"]}
        with mock.patch.object(boot, "gather_signals", return_value=known), \
                mock.patch.object(es, "_qualification_health", return_value=None), \
                mock.patch.object(es, "_activation_state",
                                  return_value={"activation": None, "coverage": coverage}):
            out = es.render()
        body = boot.render_dashboard(known)
        self.assertTrue(out.startswith(body))
        tail = out[len(body):]
        self.assertIn("2 of 3 registered worktrees", tail)
        self.assertIn("git worktree remove", tail)
        self.assertIn("Memory protection is not active on this machine yet", tail)

    def test_render_passes_the_session_through(self):
        # The session id must reach gather_signals so the dashboard shows the REAL stance, not a default.
        seen = {}

        def fake_gather(session_id=None):
            seen["session"] = session_id
            return test_boot._signals()

        with mock.patch.object(boot, "gather_signals", fake_gather):
            es.render("sess-abc")
        self.assertEqual(seen["session"], "sess-abc")

    def test_render_carries_the_operator_dashboard_markers(self):
        with mock.patch.object(boot, "gather_signals", return_value=test_boot._signals()):
            out = es.render()
        for marker in (f"## {boot.PRESENT_MARKER}", "What merged last", "Needs your attention", "Recently shipped"):
            self.assertIn(marker, out, f"the pulled dashboard must carry the '{marker}' section")

    def test_render_appends_degraded_qualification_health(self):
        health = {"status": "degraded", "skipped_effect_count": 2,
                  "last_failure_at": "2026-08-28T12:00:00Z",
                  "last_failure": {"reason_code": "accepted-dispatcher-absent",
                                   "effect": {"script": ".engine/tools/close.py"}},
                  "guidance": "Restore the accepted dispatcher, then retry."}
        with mock.patch.object(boot, "gather_signals", return_value=test_boot._signals()), \
                mock.patch.object(es, "_qualification_health", return_value=health):
            out = es.render()
        self.assertIn(es._QUALIFICATION_HEADING, out)
        self.assertIn("skipped 2 automatic effect(s)", out)
        self.assertIn("Canonical memory was left untouched", out)
        self.assertIn("accepted-code dispatcher is missing", out)
        self.assertIn(".engine/tools/close.py", out)
        self.assertIn("Restore the accepted dispatcher", out)

    def test_render_appends_recovered_qualification_health(self):
        health = {"status": "healthy", "last_recovery_at": "2026-08-28T12:00:00Z"}
        with mock.patch.object(boot, "gather_signals", return_value=test_boot._signals()), \
                mock.patch.object(es, "_qualification_health", return_value=health):
            out = es.render()
        self.assertIn(es._QUALIFICATION_RECOVERED, out)
        self.assertIn("2026-08-28T12:00:00Z", out)


class TestAlwaysAnswers(unittest.TestCase):
    def test_a_renderer_failure_degrades_never_raises(self):
        # If assembling the dashboard raises, the operator still gets a plain answer, not a crash.
        with mock.patch.object(boot, "render_dashboard", side_effect=RuntimeError("boom")):
            out = es.render()  # must NOT raise
        self.assertTrue(out.startswith(f"## {boot.PRESENT_MARKER}"))
        self.assertIn(es._DEGRADED, out)


class TestCLI(unittest.TestCase):
    def test_main_prints_the_status(self):
        buf = io.StringIO()
        with mock.patch.object(boot, "gather_signals", return_value=test_boot._signals()), \
                contextlib.redirect_stdout(buf):
            rc = es.main([])
        self.assertEqual(rc, 0)
        self.assertIn(f"## {boot.PRESENT_MARKER}", buf.getvalue())

    def test_main_resolves_and_passes_the_explicit_session(self):
        seen = {}

        def fake_gather(session_id=None):
            seen["session"] = session_id
            return test_boot._signals()

        with mock.patch.object(boot, "gather_signals", fake_gather), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = es.main(["--session", "X"])
        self.assertEqual(rc, 0)
        self.assertEqual(seen["session"], "X", "the --session value is resolved and passed to gather_signals")

    def test_demo_runs_and_shows_a_labelled_example(self):
        # Fake only the I/O boundary; run the REAL demo logic (the example render is pure data).
        buf = io.StringIO()
        with mock.patch.object(boot, "gather_signals", return_value=test_boot._signals()), \
                contextlib.redirect_stdout(buf):
            rc = es.main(["demo"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("/engine-status", out)                 # the real-status intro
        self.assertIn("EXAMPLE", out)                          # the made-up example is clearly banner-labelled
        self.assertIn("NOT your project", out)
        self.assertIn("safety gate is off", out)               # the example's gate-off alarm actually rendered


class TestNoRawCodeIdentifierLeak(unittest.TestCase):
    def test_the_tools_own_strings_leak_no_raw_code_identifier(self):
        # The dashboard body is boot's (vetted in test_boot); these are the strings THIS tool adds. A raw
        # identifier or exception name here would be a leaked internal (a bug), not a vocabulary choice.
        mine = "\n".join([es._DEGRADED, es._DEMO_INTRO, es._DEMO_EXAMPLE_BANNER,
                          es._DEMO_EXAMPLE_INTRO]).lower()
        for sym in _RAW_CODE_IDENTIFIERS:
            self.assertNotIn(sym, mine,
                             f"raw code identifier / exception fragment {sym!r} must not reach the operator")


class TestBacklogAndCoverageRendering(unittest.TestCase):
    """The one new operator-facing surface in this build, and it had no test at all.

    The plan's obligation is explicit — the status block carries the uncaptured backlog, count and oldest age
    — and the whole "nothing is lost while unqualified" argument rests on the operator being able to see it.
    A number computed and never rendered is the same as no number.
    """

    def _render(self, **over):
        state = {"activation": {"commit": "a" * 40}, "coverage": {"readable": True, "uncovered": 0,
                                                                  "total": 2, "sample": []}}
        state.update(over)
        return es._render_activation_state(state)

    def test_nothing_waiting_says_nothing(self):
        self.assertEqual(self._render(backlog={"sessions_waiting": 0, "oldest_waiting_age_days": None}), "")

    def test_a_backlog_is_reported_with_its_count_and_age_in_plain_words(self):
        out = self._render(backlog={"sessions_waiting": 3, "oldest_waiting_age_days": 11.3})
        self.assertIn("3 earlier conversation(s) are waiting", out)
        self.assertIn("11 days ago", out)          # whole days: "11.3 days" is not how a person says it
        self.assertIn("Nothing is lost", out)      # the reassurance is the point of showing the number
        for jargon in ("cursor", "drain", "transcript path", "ledger"):
            self.assertNotIn(jargon, out.lower())

    def test_a_backlog_from_today_does_not_say_zero_days(self):
        out = self._render(backlog={"sessions_waiting": 1, "oldest_waiting_age_days": 0.0})
        self.assertIn("from today", out)
        self.assertNotIn("0 day", out)

    def test_a_truncated_count_says_it_is_a_floor_rather_than_claiming_completeness(self):
        out = self._render(backlog={"sessions_waiting": 40, "oldest_waiting_age_days": 2.0,
                                    "partial": True})
        self.assertIn("at least 40", out)

    def test_an_unreadable_census_is_said_out_loud(self):
        out = self._render(coverage={"readable": False, "reason": "the worktree census answered ambiguous",
                                     "total": None, "uncovered": None, "sample": []})
        self.assertIn("could not be read", out)

    def test_uncovered_worktrees_say_what_the_gap_MEANS_not_just_that_it_exists(self):
        out = self._render(coverage={"readable": True, "uncovered": 2, "total": 5,
                                     "sample": ["feature-x [abc]: candidate"]})
        self.assertIn("without these checks", out)     # the consequence, like the sibling alarm
        self.assertIn("git worktree remove", out)      # …and the safe thing to do about it

    def test_an_unqualified_machine_says_reads_work_and_writes_wait(self):
        out = es._render_activation_state({"activation": None, "coverage": {"readable": True, "uncovered": 0,
                                                                            "total": 1, "sample": []}})
        self.assertIn("reads work and writes wait", out)


class TestPendingErasureReachesTheOperator(unittest.TestCase):
    """The channel half of a finding that was reported twice.

    Compaction says an approved deletion is still waiting — to `sys.stderr`, from a hook that exits 0, which
    is not a stream anyone is shown. Correcting the sentence did not correct that, so the state is reported
    here as well, where the operator asks."""

    def _render(self, pending):
        return es._render_activation_state(
            {"activation": {"commit": "a" * 40},
             "coverage": {"readable": True, "uncovered": 0, "total": 1, "sample": []},
             "pending_erasures": pending})

    def test_nothing_pending_says_nothing(self):
        self.assertEqual(self._render(0), "")

    def test_a_pending_deletion_is_named_with_what_it_means_and_when_to_worry(self):
        out = self._render(2)
        self.assertIn("2 deletion(s) you approved have not been carried out", out)
        self.assertIn("still findable", out)        # the consequence, said plainly
        self.assertIn("neither clears itself", out)  # …and when it stops being normal
        for jargon in ("compaction", "marker", "ledger", "erasure"):
            self.assertNotIn(jargon, out.lower())

    def test_an_unreadable_count_is_simply_not_reported(self):
        self.assertEqual(self._render(None), "")


if __name__ == "__main__":
    unittest.main()
