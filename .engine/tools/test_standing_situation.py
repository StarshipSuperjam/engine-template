#!/usr/bin/env python3
"""Self-tests for the read-only "where we are" derivation (engine-template #100; D-198 -> D-199).

These lock the load-bearing behaviours a non-engineer cannot read code to verify:
  - `milestone` is the active open Milestone's title, or None when there are none ("none set" is the honest
    normal state, never an error); the active one is the earliest-due (first returned by the API sort);
  - `phase` is derived from the most-recently-merged TRACKED BUILD Issue — a merged PR whose body closes an
    *engine-labelled* Issue — formatted "<title> (issue #N)"; PRs that merged nothing, that closed nothing,
    or that closed a non-engine Issue are skipped; no tracked build in the window -> None;
  - a READ FAILURE (HTTP >= 400 / null body) RAISES `DeriveUnavailable` and is NEVER swallowed as
    genuine-absence — so boot falls back to the cached line rather than presenting a confident, wrong one;
  - the module performs NO writes (it is a pure read-only projection).
Only the network is faked (an injected transport); the derive logic is the REAL one.

Run: uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py' -b
"""
from __future__ import annotations
import os
import re
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import standing_situation as ss  # noqa: E402


def _gh(transport, *, label="engine", repo="owner/repo"):
    """A duck-typed GitHub reader: just the attributes derive uses (repo, label, _transport)."""
    return SimpleNamespace(repo=repo, label=label, _transport=transport)


def _transport(*, milestones=(200, []), pulls=(200, []), issues=None):
    """A fake transport answering the three GETs derive makes. Each arg is a (status, json) tuple;
    `issues` maps an issue number to its (status, json). Fakes ONLY the network."""
    issues = issues or {}

    def t(method, path, body):
        if "/milestones" in path:
            return milestones
        if "/pulls" in path:
            return pulls
        m = re.search(r"/issues/(\d+)", path)
        if m:
            return issues.get(int(m.group(1)), (404, None))
        return (404, None)
    return t


def _merged_pr(number, body):
    return {"number": number, "merged_at": "2026-06-13T00:00:00Z", "body": body}


def _engine_issue(number, title):
    return {"number": number, "title": title, "labels": [{"name": "engine"}]}


class TestMilestone(unittest.TestCase):
    def test_active_milestone_title(self):
        gh = _gh(_transport(milestones=(200, [{"title": "Ship the beta", "due_on": "2026-09-01T00:00:00Z"}])))
        self.assertEqual(ss.derive_milestone(gh), "Ship the beta")

    def test_zero_milestones_is_none_not_an_error(self):
        gh = _gh(_transport(milestones=(200, [])))
        self.assertIsNone(ss.derive_milestone(gh))

    def test_active_is_the_first_returned_earliest_due(self):
        # derive takes the API's earliest-due-first ordering and uses the first — the recorded "active" rule.
        gh = _gh(_transport(milestones=(200, [{"title": "Earliest"}, {"title": "Later"}])))
        self.assertEqual(ss.derive_milestone(gh), "Earliest")

    def test_read_failure_raises_never_reads_as_none(self):
        gh = _gh(_transport(milestones=(403, None)))
        with self.assertRaises(ss.DeriveUnavailable):
            ss.derive_milestone(gh)


class TestPhase(unittest.TestCase):
    def test_phase_from_merged_pr_closing_an_engine_issue(self):
        gh = _gh(_transport(
            pulls=(200, [_merged_pr(99, "Adds the fix.\n\nCloses #80")]),
            issues={80: (200, _engine_issue(80, "Operator checkout can silently drift"))}))
        self.assertEqual(ss.derive_phase(gh), "Operator checkout can silently drift (issue #80)")

    def test_skips_a_pr_that_closes_nothing_then_finds_the_next(self):
        gh = _gh(_transport(
            pulls=(200, [_merged_pr(101, "A standalone slice, closes nothing."),
                         _merged_pr(99, "Closes #80")]),
            issues={80: (200, _engine_issue(80, "The drift fix"))}))
        self.assertEqual(ss.derive_phase(gh), "The drift fix (issue #80)")

    def test_skips_a_pr_closing_a_non_engine_issue(self):
        gh = _gh(_transport(
            pulls=(200, [_merged_pr(50, "Closes #5")]),
            issues={5: (200, {"number": 5, "title": "A product bug", "labels": [{"name": "bug"}]})}))
        self.assertIsNone(ss.derive_phase(gh))

    def test_skips_a_closed_but_unmerged_pr(self):
        gh = _gh(_transport(
            pulls=(200, [{"number": 7, "merged_at": None, "body": "Closes #80"}]),
            issues={80: (200, _engine_issue(80, "Should not be reached"))}))
        self.assertIsNone(ss.derive_phase(gh))

    def test_no_tracked_build_in_window_is_none(self):
        self.assertIsNone(ss.derive_phase(_gh(_transport(pulls=(200, [])))))

    def test_pulls_read_failure_raises(self):
        with self.assertRaises(ss.DeriveUnavailable):
            ss.derive_phase(_gh(_transport(pulls=(503, None))))

    def test_issue_read_failure_raises_never_swallowed(self):
        # a read failure on the referenced Issue must RAISE (fall to cache), not silently skip to "no phase"
        gh = _gh(_transport(pulls=(200, [_merged_pr(99, "Closes #80")]), issues={80: (500, None)}))
        with self.assertRaises(ss.DeriveUnavailable):
            ss.derive_phase(gh)

    def test_recognises_all_closing_keywords_and_the_colon_form(self):
        for body in ("Closes #80", "closed #80", "Fixes #80", "fixed #80", "Resolves #80",
                     "resolved #80", "Closes: #80"):
            gh = _gh(_transport(
                pulls=(200, [_merged_pr(99, body)]),
                issues={80: (200, _engine_issue(80, "T"))}))
            self.assertEqual(ss.derive_phase(gh), "T (issue #80)", f"body {body!r} should link")

    def test_keyword_inside_another_word_does_not_falsely_match(self):
        # "discloses"/"unfixed" contain close/fix as substrings — the word boundary must stop a false link.
        for body in ("This discloses #80 indirectly.", "An unfixed #80 reference."):
            gh = _gh(_transport(
                pulls=(200, [_merged_pr(99, body)]),
                issues={80: (200, _engine_issue(80, "Should not be reached"))}))
            self.assertIsNone(ss.derive_phase(gh), f"body {body!r} must not be read as a closing reference")

    def test_picks_the_most_recently_merged_not_the_api_list_order(self):
        # API list order (sort=updated) is NOT merge order: PR #99 comes first in the list but #100 merged
        # LATER, so #100's tracked build is the answer — the derive must order by merged_at, not list order.
        pulls = (200, [{"number": 99, "merged_at": "2026-06-01T00:00:00Z", "body": "Closes #80"},
                       {"number": 100, "merged_at": "2026-06-10T00:00:00Z", "body": "Closes #90"}])
        gh = _gh(_transport(pulls=pulls, issues={80: (200, _engine_issue(80, "Older build")),
                                                  90: (200, _engine_issue(90, "Newer build"))}))
        self.assertEqual(ss.derive_phase(gh), "Newer build (issue #90)")


class TestDeriveStandingSituation(unittest.TestCase):
    def test_returns_both_fields_on_success(self):
        gh = _gh(_transport(
            milestones=(200, [{"title": "Ship the beta"}]),
            pulls=(200, [_merged_pr(99, "Closes #80")]),
            issues={80: (200, _engine_issue(80, "The drift fix"))}))
        self.assertEqual(ss.derive_standing_situation(gh),
                         {"milestone": "Ship the beta", "phase": "The drift fix (issue #80)"})

    def test_genuine_absence_returns_none_fields(self):
        gh = _gh(_transport(milestones=(200, []), pulls=(200, [])))
        self.assertEqual(ss.derive_standing_situation(gh), {"milestone": None, "phase": None})

    def test_all_or_nothing_a_read_failure_raises_for_the_whole_derive(self):
        # milestone read fails -> the whole derive raises (boot then shows the cached line), never a
        # half-live "{none set, <phase>}" that masquerades as a current answer.
        gh = _gh(_transport(milestones=(403, None), pulls=(200, [_merged_pr(99, "Closes #80")]),
                            issues={80: (200, _engine_issue(80, "T"))}))
        with self.assertRaises(ss.DeriveUnavailable):
            ss.derive_standing_situation(gh)


class TestNoWrites(unittest.TestCase):
    def test_module_source_performs_no_file_writes(self):
        # The projection is read-only: no file-write code at all (scan for write CODE tokens, not prose —
        # the docstring legitimately says it "never touches state.json").
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "standing_situation.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        for forbidden in ('open(', '.write(', 'json.dump'):
            self.assertNotIn(forbidden, src,
                             f"the read-only derive module must contain no '{forbidden}' (no writes)")


class TestWhereLinesRendersTheRealCard(unittest.TestCase):
    """`_where_lines` renders the REAL boot dashboard over a hand-built signals dict to extract the
    'Where we are' block. render_dashboard reads its signals by HARD SUBSCRIPT, so this guards that the
    hand-built dict stays complete: a new boot signal not added here would KeyError this operator-runnable
    demo path — caught here in the suite, not only when the operator runs the demo. (audit-library 3c added
    `audit_stale`; this is the test that would have caught the missing key.)"""

    def test_renders_without_keyerror_and_returns_the_standing_block(self):
        import boot  # lazy: boot imports this module at top — importing here mirrors the demo and avoids a cycle
        live = {"milestone": "Ship the beta", "phase": "Building the checkout page"}
        lines = ss._where_lines(boot, live=live, state=None)
        self.assertTrue(any(ln.startswith("**Where we are:**") for ln in lines),
                        "the standing block must carry the 'Where we are' line")
        self.assertTrue(any("Ship the beta" in ln for ln in lines),
                        "and the milestone the live source named")


if __name__ == "__main__":
    unittest.main()
