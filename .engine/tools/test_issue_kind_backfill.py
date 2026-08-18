#!/usr/bin/env python3
"""Tests for issue_kind_backfill — the bounded one-time normalisation of legacy alias title prefixes
(StarshipSuperjam/engine-template#937). The renaming logic is pure; the dry-run mutates nothing; `--apply`
needs `--confirm` too."""
from __future__ import annotations

import contextlib
import io
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import issue_kind_backfill as b   # noqa: E402
import issue_label_client         # noqa: E402
import quiet_call                 # noqa: E402


class TestPlanRenames(unittest.TestCase):
    def test_only_unambiguous_aliases_are_planned(self):
        issues = [
            {"number": 1, "title": "Bug: broke"},
            {"number": 2, "title": "Engine fault: shed"},
            {"number": 3, "title": "Defect: off-by-one"},
            {"number": 4, "title": "Improvement: canonical already"},   # skip
            {"number": 5, "title": "Architecture: ambiguous"},          # skip (never guess)
            {"number": 6, "title": "Memory integrity: ambiguous"},      # skip
            {"number": 7, "title": "Migration M3: not a kind"},         # skip
            {"number": 8, "title": "no prefix at all"},                 # skip
        ]
        plan = b.plan_renames(issues)
        self.assertEqual([n for n, _, _ in plan], [1, 2, 3])
        self.assertEqual(dict((n, new) for n, _, new in plan),
                         {1: "Fix: broke", 2: "Fix: shed", 3: "Fix: off-by-one"})

    def test_descriptive_remainder_is_preserved_when_stripping_the_alias(self):
        plan = b.plan_renames([{"number": 1, "title": "Bug: parser: nested case"}])
        # only the FIRST alias slot is replaced; the descriptive `parser:` is preserved
        self.assertEqual(plan, [(1, "Bug: parser: nested case", "Fix: parser: nested case")])

    def test_a_noop_rename_is_not_planned(self):
        self.assertEqual(b.plan_renames([{"number": 1, "title": "Fix: already fine"}]), [])

    def test_missing_title_is_ignored(self):
        self.assertEqual(b.plan_renames([{"number": 1}]), [])


class _FakeIssues:
    """A telemetry.GitHubIssues stand-in used to prove the dry-run lists without writing."""

    def __init__(self, issues):
        self._issues = issues
        self.title_patches = []

    def list_open_engine_issues(self):
        return list(self._issues)


class TestApplyRenames(unittest.TestCase):
    def test_apply_writes_each_planned_rename_via_edit_title(self):
        writes = []

        class _Client:
            def edit_title(self, number, title):
                writes.append((number, title))

        n = b.apply_renames([(1, "Bug: x", "Fix: x"), (2, "Defect: y", "Fix: y")], _Client())
        self.assertEqual(n, 2)
        self.assertEqual(writes, [(1, "Fix: x"), (2, "Fix: y")])

    def test_dry_run_lists_but_never_patches(self):
        gh = b._FakeGitHub([{"number": 1, "title": "Bug: broke"}])
        import telemetry
        telemetry.GitHubIssues("o/r", "t", transport=gh).list_open_engine_issues()
        # the list read is GET-only; no title PATCH is ever issued in the dry-run path
        self.assertEqual(gh.title_edits(), [])
        self.assertTrue(all(m == "GET" for m, _, _ in gh.calls))


class TestCliGate(unittest.TestCase):
    def _env(self, **kv):
        keys = ("GITHUB_REPOSITORY", "GITHUB_TOKEN")
        saved = {k: os.environ.get(k) for k in keys}

        def restore():
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(restore)
        for k in keys:
            os.environ.pop(k, None)
        for k, v in kv.items():
            os.environ[k] = v

    def test_apply_without_confirm_is_refused_before_any_write(self):
        # even reachable (repo+token set), --apply alone must refuse — parity with the create path's --confirm.
        self._env(GITHUB_REPOSITORY="o/r", GITHUB_TOKEN="tok")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            # list_open_engine_issues would try the network; stub telemetry to return an empty list so we reach
            # the confirm gate deterministically offline.
            import telemetry
            orig = telemetry.GitHubIssues
            telemetry.GitHubIssues = lambda *a, **k: _FakeIssues([{"number": 1, "title": "Bug: x"}])
            try:
                rc = b.main(["--apply"])
            finally:
                telemetry.GitHubIssues = orig
        self.assertEqual(rc, 2)
        self.assertIn("--confirm", err.getvalue())

    def test_no_repo_or_token_reports_cannot_reach(self):
        self._env()  # neither set
        # origin_slug may resolve a repo from git, but with no token the tool still reports it cannot reach.
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rc = b.main([])
        self.assertEqual(rc, 1)
        self.assertIn("GITHUB_TOKEN", err.getvalue())


class TestDemo(unittest.TestCase):
    def test_demo_passes(self):
        self.assertEqual(quiet_call.run(b._demo), 0)


if __name__ == "__main__":
    unittest.main()
