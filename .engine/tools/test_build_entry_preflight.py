#!/usr/bin/env python3
"""Fresh Build admission against real isolated git checkouts and a local bare origin."""
from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import build_entry_preflight as entry
import build_coordinator_core as core
from test_build_coordinator import ScrubbedGitRepo


class TestFreshBuildEntry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.operator = ScrubbedGitRepo(Path(self.tmp.name) / "operator")
        self.base = self.operator.commit_file("a.txt", "base\n", "base")
        self.remote = str(Path(self.tmp.name) / "origin.git")
        self.operator.git("clone", "--bare", self.operator.path, self.remote)
        self.operator.git("remote", "add", "origin", self.remote)
        self.repo = ScrubbedGitRepo(Path(self.tmp.name) / "isolated")
        self.repo.git("remote", "add", "origin", self.remote)
        self.repo.git("fetch", "origin")
        self.repo.git("checkout", "-q", "-b", "codex/build", "origin/main")
        self.head = self.repo.commit_file("build.txt", "build\n", "work")
        self.pr = {"number": 7, "state": "OPEN", "isDraft": True,
                   "headRefOid": self.head, "headRefName": "codex/build",
                   "headRepository": {"nameWithOwner": "owner/repo"},
                   "baseRefName": "main", "baseRefOid": self.base}
        patch = mock.patch.object(entry.repo_identity, "origin_slug", return_value="owner/repo")
        patch.start()
        self.addCleanup(patch.stop)
        self.operator_before = self._checkout(self.operator)
        self.addCleanup(self._assert_operator_unchanged)

    def _checkout(self, repo):
        return (repo.git("rev-parse", "HEAD"), repo.git("symbolic-ref", "HEAD"),
                repo.git("status", "--porcelain"), repo.git("reflog", "--format=%H %gs"))

    def _assert_operator_unchanged(self):
        self.assertEqual(self._checkout(self.operator), self.operator_before)

    def observe(self, pr=None):
        return entry.observe_fresh(self.repo.path, "owner/repo", 7, self.pr if pr is None else pr)

    def advance(self):
        # Separate publisher; the operator checkout is never updated by an admission operation.
        publisher = ScrubbedGitRepo(Path(self.tmp.name) / "publisher")
        publisher.git("remote", "add", "origin", self.remote)
        publisher.git("fetch", "origin")
        publisher.git("checkout", "-q", "-B", "main", "origin/main")
        tip = publisher.commit_file("upstream.txt", "advance\n", "advance")
        publisher.git("push", "origin", "main")
        return tip

    def test_fresh_success_records_exact_refs_without_changing_either_checkout(self):
        before = self._checkout(self.repo)
        observed = self.observe()
        self.assertEqual(observed["material"]["head"], self.head)
        self.assertEqual(observed["material"]["target_tip"], self.base)
        self.assertEqual(observed["material"]["target_ref"], "main")
        self.assertTrue(observed["observed_at"])
        entry.verify_frozen(self.repo.path, observed)
        self.assertEqual(self._checkout(self.repo), before)
        later = dict(observed, observed_at="later observation")
        self.assertEqual(entry.material_digest(later), entry.material_digest(observed))

    def test_target_advance_after_planning_refuses_stale_branch_without_rebasing(self):
        self.pr["baseRefOid"] = self.advance()
        before = self._checkout(self.repo)
        with self.assertRaisesRegex(core.CoordinatorError, "not contained|rebase"):
            self.observe()
        self.assertEqual(self._checkout(self.repo), before)

    def test_target_advance_after_pr_observation_refuses_stale_metadata(self):
        self.advance()
        with self.assertRaisesRegex(core.CoordinatorError, "target and PR base differ"):
            self.observe()

    def test_dirty_checkout_refuses_without_cleaning_it(self):
        self.repo.write("build.txt", "uncommitted\n")
        before = self._checkout(self.repo)
        with self.assertRaisesRegex(core.CoordinatorError, "clean isolated worktree"):
            self.observe()
        self.assertEqual(self._checkout(self.repo), before)

    def test_real_conflicted_merge_refuses_without_aborting_it(self):
        self.repo.git("checkout", "-q", "-b", "conflicting", self.base)
        self.repo.commit_file("a.txt", "other\n", "other")
        self.repo.git("checkout", "-q", "codex/build")
        self.repo.commit_file("a.txt", "ours\n", "ours")
        merge = subprocess.run(["git", "-C", self.repo.path, "merge", "conflicting"],
                               env=self.repo.env, capture_output=True, text=True)
        self.assertNotEqual(merge.returncode, 0)
        self.assertIn("CONFLICT", merge.stdout)
        before = self._checkout(self.repo)
        with self.assertRaisesRegex(core.CoordinatorError, "clean|git operation"):
            self.observe()
        self.assertEqual(self._checkout(self.repo), before)
        self.assertTrue((Path(self.repo.path) / ".git" / "MERGE_HEAD").exists())

    def test_failed_fetch_does_not_accept_cached_target(self):
        self.repo.git("remote", "set-url", "origin", str(Path(self.tmp.name) / "missing.git"))
        before = self._checkout(self.repo)
        with self.assertRaisesRegex(core.CoordinatorError, "git"):
            self.observe()
        self.assertEqual(self._checkout(self.repo), before)

    def test_wrong_pr_identity_or_refs_refuse(self):
        changes = [{"headRefOid": "f" * 40}, {"headRefName": "other"},
                   {"headRepository": {"nameWithOwner": "other/repo"}},
                   {"number": 8}, {"state": "CLOSED"}, {"isDraft": False},
                   {"baseRefName": "-bad"}, {"baseRefName": "missing"},
                   {"baseRefOid": "f" * 40}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(core.CoordinatorError):
                self.observe(dict(self.pr, **change))

    def test_existing_nondefault_target_ref_refuses(self):
        self.repo.git("push", "origin", f"{self.base}:refs/heads/other-base")
        with self.assertRaises(core.CoordinatorError):
            self.observe(dict(self.pr, baseRefName="other-base"))

    def test_fetch_timeout_is_a_bounded_refusal(self):
        run = subprocess.run
        def timeout_fetch(argv, **kwargs):
            if "fetch" in argv:
                self.assertEqual(kwargs["timeout"], 45)
                self.assertEqual(kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            return run(argv, **kwargs)
        before = self._checkout(self.repo)
        with mock.patch.object(entry.subprocess, "run", side_effect=timeout_fetch), \
                self.assertRaises(core.CoordinatorError):
            self.observe()
        self.assertEqual(self._checkout(self.repo), before)

    def test_wrong_origin_repository_refuses(self):
        with mock.patch.object(entry.repo_identity, "origin_slug", return_value="other/repo"), \
                self.assertRaisesRegex(core.CoordinatorError, "repository.*origin"):
            self.observe()

    def test_changed_head_after_observation_fails_local_recheck(self):
        observed = self.observe()
        self.repo.commit_file("later.txt", "later\n", "later")
        with self.assertRaisesRegex(core.CoordinatorError, "inputs changed"):
            entry.verify_frozen(self.repo.path, observed)

    def test_changed_target_after_observation_fails_local_recheck(self):
        observed = self.observe()
        self.advance()
        self.repo.git("fetch", "origin")
        with self.assertRaisesRegex(core.CoordinatorError, "inputs changed"):
            entry.verify_frozen(self.repo.path, observed)


if __name__ == "__main__":
    unittest.main()
