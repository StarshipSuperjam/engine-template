#!/usr/bin/env python3
"""Self-tests for the in-flight git/GitHub work-record reader (engine-template #37, the work-record slice).

These lock the load-bearing behaviours a non-engineer cannot read code to verify:
  - the reader surfaces the IN-FLIGHT work as `in_flight` candidates: open pull requests (the GitHub layer)
    and the current working branch (the local-git floor), freshest first and bounded;
  - a pull request for the CURRENT branch SUBSUMES its branch record (no double-listing);
  - it answers OFFLINE from the local-git floor alone (no GitHub reader), and a GitHub read FAILURE degrades
    to the floor rather than failing ("local git stands in" — never a crash);
  - it RAISES WorkRecordUnavailable only when nothing can be consulted (git unrunnable AND no GitHub read),
    and NEVER swallows that as "no in-flight work"; an empty-but-consulted record returns [] (git available);
  - recency is normalised to a trailing-Z UTC moment (a git offset / a GitHub Z), or omitted when malformed —
    so a bad timestamp can never reach the ranking math and crash the whole ranking.
Only git (an injected `run`) and the network (an injected `gh._transport`) are faked; the reader logic is real.

Run: uv run --directory .engine -- python -m unittest discover -s tools -p 'test_*.py'
"""
from __future__ import annotations
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import work_record as wr  # noqa: E402


def _run(*, in_repo=True, current="claude/my-feature", default="main", tip="2026-06-19T10:00:00Z"):
    """A fake git runner answering exactly the reads the floor makes. `current` may be 'HEAD' (detached),
    the default branch name, or None (rev-parse fails). `in_repo=False` makes the floor itself unavailable."""
    def run(args):
        if args[:2] == ["rev-parse", "--is-inside-work-tree"]:
            return "true" if in_repo else None
        if args[:1] == ["symbolic-ref"]:
            return f"refs/remotes/origin/{default}" if default else None
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return current
        if args[:1] == ["log"]:
            return tip
        return None
    return run


def _gh(transport, *, repo="owner/repo"):
    """A duck-typed GitHub reader (just .repo + ._transport) over a canned transport."""
    return SimpleNamespace(repo=repo, _transport=transport)


def _prs(*pulls, status=200):
    """A transport answering the open-PRs GET from canned PR objects; everything else 404s."""
    def t(method, path, body):
        if "/pulls" in path:
            return status, list(pulls)
        return 404, None
    return t


def _pr(number, *, title="a PR", updated="2026-06-18T00:00:00Z", head="some/branch"):
    return {"number": number, "title": title, "updated_at": updated, "head": {"ref": head}}


class TestInFlight(unittest.TestCase):
    def test_open_prs_and_current_branch(self):
        recs = wr.read_in_flight(_gh(_prs(_pr(161, head="x"), _pr(158, head="y"))),
                                 run=_run(current="claude/my-feature"))
        ids = [r["id"] for r in recs]
        self.assertIn("pr:161", ids)
        self.assertIn("pr:158", ids)
        self.assertIn("branch:claude/my-feature", ids)
        self.assertTrue(all(r["category"] == "in_flight" and r["source"] == "git" for r in recs))

    def test_pr_for_current_branch_subsumes_the_branch_record(self):
        # The open PR's head ref IS the current branch -> the branch must NOT also be listed.
        recs = wr.read_in_flight(_gh(_prs(_pr(161, head="claude/work"))), run=_run(current="claude/work"))
        ids = [r["id"] for r in recs]
        self.assertEqual(ids, ["pr:161"])
        self.assertNotIn("branch:claude/work", ids)

    def test_freshest_first_and_capped(self):
        pulls = [_pr(n, updated=f"2026-06-{n:02d}T00:00:00Z", head=f"h{n}") for n in (5, 20, 12)]
        recs = wr.read_in_flight(_gh(_prs(*pulls)), run=_run(current="main", default="main"), cap=2)
        self.assertEqual([r["id"] for r in recs], ["pr:20", "pr:12"])  # newest two, branch omitted (on default)


class TestFloorAndDegrade(unittest.TestCase):
    def test_offline_floor_is_the_working_branch(self):
        recs = wr.read_in_flight(None, run=_run(current="claude/my-feature"))
        self.assertEqual([r["id"] for r in recs], ["branch:claude/my-feature"])

    def test_on_default_branch_offline_is_empty_but_available(self):
        # On the default branch with no reader: nothing in flight, but git WAS consulted -> [] (not a raise).
        self.assertEqual(wr.read_in_flight(None, run=_run(current="main", default="main")), [])

    def test_detached_head_yields_no_branch_record(self):
        self.assertEqual(wr.read_in_flight(None, run=_run(current="HEAD")), [])

    def test_github_failure_degrades_to_floor(self):
        # A 403 on the PR read must fall back to the local-git floor, never raise.
        recs = wr.read_in_flight(_gh(_prs(status=403)), run=_run(current="claude/my-feature"))
        self.assertEqual([r["id"] for r in recs], ["branch:claude/my-feature"])

    def test_unavailable_when_no_git_and_no_github(self):
        with self.assertRaises(wr.WorkRecordUnavailable):
            wr.read_in_flight(None, run=_run(in_repo=False))

    def test_unavailable_when_no_git_and_github_fails(self):
        with self.assertRaises(wr.WorkRecordUnavailable):
            wr.read_in_flight(_gh(_prs(status=500)), run=_run(in_repo=False))

    def test_no_git_but_github_ok_returns_prs(self):
        # Floor unavailable (not a git checkout) but the GitHub read succeeds -> the PRs still answer.
        recs = wr.read_in_flight(_gh(_prs(_pr(161, head="x"))), run=_run(in_repo=False))
        self.assertEqual([r["id"] for r in recs], ["pr:161"])


class TestRecencyNormalisation(unittest.TestCase):
    def test_offset_and_z_normalise_to_trailing_z(self):
        self.assertEqual(wr._z("2026-06-19T10:00:00-07:00"), "2026-06-19T17:00:00Z")
        self.assertEqual(wr._z("2026-06-19T17:00:00Z"), "2026-06-19T17:00:00Z")

    def test_malformed_or_absent_recency_is_dropped_not_crashed(self):
        self.assertIsNone(wr._z("not-a-timestamp"))
        self.assertIsNone(wr._z(None))
        self.assertIsNone(wr._z(""))

    def test_branch_recency_is_normalised_in_the_record(self):
        rec = wr.read_in_flight(None, run=_run(current="b", tip="2026-06-19T10:00:00-07:00"))[0]
        self.assertEqual(rec["recency"], "2026-06-19T17:00:00Z")

    def test_record_recency_is_epoch_parseable(self):
        # The contract attention relies on: every emitted recency is parseable by the ranking math
        # (attention_rank._epoch), so a live work-record read never crashes the ranking.
        import attention_rank
        for rec in wr.read_in_flight(_gh(_prs(_pr(1, updated="2026-06-18T00:00:00Z", head="z"))),
                                     run=_run(current="b")):
            if rec["recency"] is not None:
                attention_rank._epoch(rec["recency"])  # must not raise


class TestAttentionIntegration(unittest.TestCase):
    """The seam attention relies on: a successful read marks `git` available and adds in_flight candidates;
    a raising read leaves `git` degraded (never a crash)."""

    def setUp(self):
        import attention
        self.attention = attention

    def test_records_become_in_flight_candidates_and_mark_git_available(self):
        from unittest import mock
        recs = [{"id": "pr:9", "category": "in_flight", "recency": None, "title": "t", "source": "git"}]
        with mock.patch.object(self.attention.work_record, "read_in_flight", return_value=recs):
            cands, available, _ = self.attention.assemble_candidates({}, state_path="/nonexistent", gh=object())
        self.assertIn("git", available)
        self.assertIn("pr:9", [c["id"] for c in cands if c.get("source") == "git"])

    def test_unavailable_read_leaves_git_degraded(self):
        from unittest import mock
        with mock.patch.object(self.attention.work_record, "read_in_flight",
                               side_effect=wr.WorkRecordUnavailable("down")):
            _, available, _ = self.attention.assemble_candidates({}, state_path="/nonexistent", gh=object())
        self.assertNotIn("git", available)


if __name__ == "__main__":
    unittest.main()
