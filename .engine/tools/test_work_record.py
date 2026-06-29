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

Run: uv run --directory .engine -- python -m unittest discover -s tools -p 'test_*.py' -b
"""
from __future__ import annotations
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import work_record as wr  # noqa: E402


def _run(*, in_repo=True, current="claude/my-feature", default="main", tip="2026-06-19T10:00:00Z", merged=False):
    """A fake git runner answering exactly the reads the floor makes. `current` may be 'HEAD' (detached),
    the default branch name, or None (rev-parse fails). `in_repo=False` makes the floor itself unavailable.
    `merged` answers `merge-base --is-ancestor` the way real git does: "" (exit 0 = HEAD is an ancestor of the
    default = merged) when True, else None (exit != 0 = not an ancestor)."""
    def run(args):
        if args[:2] == ["rev-parse", "--is-inside-work-tree"]:
            return "true" if in_repo else None
        if args[:1] == ["symbolic-ref"]:
            return f"refs/remotes/origin/{default}" if default else None
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return current
        if args[:2] == ["merge-base", "--is-ancestor"]:
            return "" if merged else None
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

    def test_a_merged_branch_is_not_surfaced_as_in_flight(self):
        # A merged-but-not-deleted working branch is FINISHED work, not "unmerged work in flight": boot must not
        # tell the operator they have unmerged work on a branch whose pull request already landed.
        recs = wr.read_in_flight(None, run=_run(current="claude/already-merged", merged=True))
        self.assertEqual(recs, [])

    def test_an_unmerged_branch_is_still_surfaced_the_paired_control(self):
        # The control for the test above: the SAME path with merged=False MUST still surface the branch, so real
        # in-flight work is never hidden (and a future fake-default flip to merged can't silently start hiding it).
        recs = wr.read_in_flight(None, run=_run(current="claude/still-working", merged=False))
        self.assertEqual([r["id"] for r in recs], ["branch:claude/still-working"])

    def test_a_merged_branch_drops_but_other_open_prs_remain(self):
        # Online: a merged current branch (with no PR of its own) drops, while open PRs for OTHER branches stay.
        recs = wr.read_in_flight(_gh(_prs(_pr(161, head="claude/other"))),
                                 run=_run(current="claude/already-merged", merged=True))
        ids = [r["id"] for r in recs]
        self.assertIn("pr:161", ids)
        self.assertNotIn("branch:claude/already-merged", ids)

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


def _run_paths(*, in_repo=True, current="claude/feature", default="main",
               committed=None, working=None, staged=None):
    """A fake git runner for changed_paths: answers the branch-resolution reads (_current_branch /
    _default_branch) plus the three diff legs (committed `<base>...HEAD`, working-tree `HEAD`, staged
    `--cached`). `in_repo=False` makes every read fail (not a git checkout)."""
    def run(args):
        if not in_repo:
            return None
        if args[:2] == ["rev-parse", "--is-inside-work-tree"]:
            return "true"
        if args[:1] == ["symbolic-ref"]:
            return f"refs/remotes/origin/{default}" if default else None
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return current
        if args[:1] == ["log"]:
            return "2026-06-19T10:00:00Z"
        if args[:2] == ["diff", "--name-only"]:
            spec = args[2] if len(args) > 2 else None
            if spec == "HEAD":
                return "\n".join(working) if working else None
            if spec == "--cached":
                return "\n".join(staged) if staged else None
            if spec and "..." in spec:
                return "\n".join(committed) if committed else None
        return None
    return run


class TestChangedPaths(unittest.TestCase):
    def test_committed_and_uncommitted_and_staged_union_deduped_sorted(self):
        recs = wr.changed_paths(run=_run_paths(
            committed=["b.py", "a.py"], working=["c.py"], staged=["b.py", "d.py"]))
        self.assertEqual(recs, ["a.py", "b.py", "c.py", "d.py"])  # union, deduped (b.py once), sorted

    def test_default_branch_skips_the_committed_leg_but_keeps_local_edits(self):
        # On the default branch _current_branch is None -> the committed leg is NOT run (its merge-base diff
        # would be empty/meaningless), but uncommitted/staged local work is still in-flight work in hand.
        recs = wr.changed_paths(run=_run_paths(
            current="main", default="main", committed=["should_not_appear.py"], working=["w.py"]))
        self.assertEqual(recs, ["w.py"])

    def test_default_branch_clean_is_empty(self):
        self.assertEqual(wr.changed_paths(run=_run_paths(current="main", default="main")), [])

    def test_detached_head_skips_the_committed_leg(self):
        recs = wr.changed_paths(run=_run_paths(current="HEAD", committed=["x.py"], staged=["s.py"]))
        self.assertEqual(recs, ["s.py"])

    def test_not_a_repo_is_empty(self):
        self.assertEqual(wr.changed_paths(run=_run_paths(in_repo=False)), [])

    def test_cap_bounds_the_list(self):
        many = [f"f{i:02d}.py" for i in range(40)]
        recs = wr.changed_paths(run=_run_paths(committed=many), cap=5)
        self.assertEqual(len(recs), 5)
        self.assertEqual(recs, ["f00.py", "f01.py", "f02.py", "f03.py", "f04.py"])  # the sorted prefix


if __name__ == "__main__":
    unittest.main()
