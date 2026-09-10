#!/usr/bin/env python3
"""Fresh Build admission against real isolated git checkouts and a local bare origin."""
from __future__ import annotations

from pathlib import Path
import os
import sys
import json
import subprocess
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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


class _ClaimLibrary:
    """Small local claim catalogue; no mocked overlap decisions."""
    def __init__(self, records=None):
        self.records = records or {}

    def slugs(self):
        return list(self.records)

    def read_record(self, slug):
        value = self.records[slug]
        if isinstance(value, Exception):
            raise value
        return value

    def head(self, slug):
        return {"build_plan": {"intent_source": self.records[slug].get("source", {"kind": "direct"})}}


class TestIssueOverlapAdmission(unittest.TestCase):
    def setUp(self):
        self.library = _ClaimLibrary()
        self.candidate = {"repository": "owner/repo", "pr": 7, "head_repository": "owner/repo",
                          "head_ref": "codex/12-work", "head": "a" * 40}

    def claim(self, state="active", **over):
        return dict({"state": state, "repository": "owner/repo", "pull_request": 99,
                     "build_id": "bld_" + "1" * 32, "generation": 1, "worktree": "/tmp/other-build",
                     "authorizing_issue": 12}, **over)

    def pr(self, number=7, ref="codex/12-work", repository="owner/repo", body="Fixes #12"):
        return {"number": number, "title": "work", "body": body,
                "head": {"ref": ref, "sha": "a" * 40, "repo": {"full_name": repository}}}

    def observe(self, *, issues=None, rows=None, branches="", candidate=True):
        with mock.patch.object(entry, "_open_prs", return_value=[] if rows is None else rows), \
                mock.patch.object(entry, "_git", return_value=branches):
            return entry.overlap_observation("/tmp/entry", self.library, "owner/repo",
                [12] if issues is None else issues, candidate=self.candidate if candidate else None,
                worktree="/tmp/entry")

    def test_structured_issue_sources_are_unioned_without_prose_or_foreign_numbers(self):
        plan = {"intent_source": {"kind": "issue", "issue": 12}, "raw_intent": "also #999"}
        pr = {"body": "Fixes #888", "closingIssuesReferences": [
            {"number": 13, "url": "https://github.com/owner/repo/issues/13"},
            {"number": 12, "url": "https://github.com/OWNER/REPO/issues/12"},
            {"number": 14, "url": "https://github.com/foreign/repo/issues/14"}]}
        self.assertEqual(entry.issue_numbers(plan, 11, pr, "owner/repo"), [11, 12, 13])
        self.assertEqual(entry.issue_numbers({}, True, {"closingIssuesReferences": []}, "owner/repo"), [])

    def test_missing_or_malformed_structured_pr_issue_metadata_refuses(self):
        for pr in ({}, {"closingIssuesReferences": None}, {"closingIssuesReferences": {}},
                   {"closingIssuesReferences": [None]}, {"closingIssuesReferences": [{"number": 12}]},
                   {"closingIssuesReferences": [{"url": "not-an-issue-url"}]},
                   {"closingIssuesReferences": [{"number": 13,
                       "url": "https://github.com/owner/repo/issues/12"}]}):
            with self.subTest(pr=pr), self.assertRaises(core.CoordinatorError):
                entry.issue_numbers({"intent_source": {"kind": "issue", "issue": 12}}, 12, pr, "owner/repo")

    def test_markdown_and_qualified_foreign_references_do_not_collide(self):
        text = ("[#12](https://github.com/foreign/repo/issues/12) foreign/repo#12 "
                "https://github.com/foreign/repo/issues/12 "
                "[local](https://github.com/owner/repo/issues/13) OWNER/REPO#14 plain #15 #123")
        self.assertEqual(entry.mentioned_issues(text, "owner/repo"), {13, 14, 15, 123})

    def test_pr_transport_slurps_every_page_and_later_competitor_is_observed(self):
        pages = [[self.pr()], [self.pr(number=8, ref="other")]]
        result = subprocess.CompletedProcess([], 0, stdout=json.dumps(pages), stderr="")
        with mock.patch.object(entry.subprocess, "run", return_value=result) as run, \
                mock.patch.object(entry, "_git", return_value=""):
            observed = entry.overlap_observation("/tmp/entry", self.library, "owner/repo", [12],
                                                   candidate=self.candidate)
        argv = run.call_args.args[0]
        self.assertIn("--paginate", argv)
        self.assertIn("--slurp", argv)
        self.assertIn("per_page=100", argv[-1])
        self.assertEqual(observed["matches"], ["pr:owner/repo#8:issue#12"])
        with self.assertRaises(core.CoordinatorError):
            entry.accept_overlap("owner/repo", [12], observed)

    def test_only_exact_verified_candidate_pr_is_excluded(self):
        self.assertEqual(self.observe(rows=[self.pr()])["matches"], [])
        moved = self.pr()
        moved["head"]["sha"] = "b" * 40
        variants = [self.pr(number=8), self.pr(ref="different"), self.pr(repository="foreign/repo"), moved]
        for pr in variants:
            with self.subTest(pr=pr):
                self.assertTrue(self.observe(rows=[pr])["matches"])
        self.assertTrue(self.observe(rows=[self.pr()], candidate=False)["matches"])

    def test_remote_branch_issue_boundaries_cover_both_providers(self):
        names = ["codex/12-work", "codex/123-work", "claude/issue-12-fix", "claude/123-fix"]
        lines = "\n".join("a" * 40 + "\trefs/heads/" + name for name in names)
        matches = self.observe(branches=lines, candidate=False)["matches"]
        self.assertEqual(len(matches), 2)
        self.assertTrue(any("codex/12-work" in value for value in matches))
        self.assertTrue(any("claude/issue-12-fix" in value for value in matches))
        self.assertFalse(any("123" in value for value in matches))

    def test_candidate_branch_self_exclusion_requires_unchanged_head(self):
        ref = "\trefs/heads/" + self.candidate["head_ref"]
        self.assertEqual(self.observe(branches="a" * 40 + ref)["matches"], [])
        self.assertEqual(len(self.observe(branches="b" * 40 + ref)["matches"]), 1)

    def test_nonterminal_local_claims_are_occupied_without_snapshots(self):
        for state in ("preparing", "active", "transferring", "retiring"):
            with self.subTest(state=state):
                self.library.records = {"plan": {"build_lease": {"current": self.claim(state)}}}
                self.assertEqual(len(self.observe()["matches"]), 1)
        for state in ("superseded", "abandoned", "complete"):
            with self.subTest(state=state):
                self.library.records = {"plan": {"build_lease": {"current": self.claim(state)}}}
                self.assertEqual(self.observe()["matches"], [])

    def test_local_issue_sources_include_admission_and_plan_intent(self):
        claim = self.claim(authorizing_issue=11, admission={"material": {"issues": [12]}})
        self.library.records = {"plan": {"build_lease": {"current": claim},
                                         "source": {"kind": "issue", "issue": 13}}}
        matches, errors = entry.local_overlap(self.library, "owner/repo", [11, 12, 13])
        self.assertEqual(len(matches), 3)
        self.assertEqual(errors, [])

    def test_local_self_exclusion_requires_exact_build_id_and_generation(self):
        claim = self.claim()
        self.library.records = {"plan": {"build_lease": {"current": claim}}}
        identity = {"build_id": claim["build_id"], "generation": 1}
        self.assertEqual(entry.local_overlap(self.library, "owner/repo", [12], identity=identity), ([], []))
        for changed in ({"build_id": "bld_" + "2" * 32, "generation": 1},
                        {"build_id": claim["build_id"], "generation": 2}):
            with self.subTest(identity=changed):
                self.assertTrue(entry.local_overlap(self.library, "owner/repo", [12], identity=changed)[0])

    def test_other_owner_of_same_pr_or_worktree_is_never_an_overlap_override(self):
        for change in ({"pull_request": 7}, {"worktree": "/tmp/entry"}):
            self.library.records = {"plan": {"build_lease": {"current": self.claim(**change)}}}
            with self.subTest(change=change), self.assertRaisesRegex(core.CoordinatorError, "owns this PR or worktree"):
                self.observe(issues=[])

    def test_remote_and_local_errors_are_incomplete_and_override_is_scoped(self):
        self.library.records = {"broken": OSError("unreadable")}
        with mock.patch.object(entry, "_open_prs", side_effect=core.CoordinatorError("remote unavailable")), \
                mock.patch.object(entry, "_git", return_value=""):
            observed = entry.overlap_observation("/tmp/entry", self.library, "owner/repo", [12])
        self.assertEqual(observed["coverage"], "incomplete")
        self.assertEqual(len(observed["errors"]), 2)
        scope = core.digest({"repository": "owner/repo", "issues": [12], "observation": observed})
        for reason in (None, "", "   "):
            with self.subTest(reason=reason), self.assertRaises(core.CoordinatorError):
                entry.accept_overlap("owner/repo", [12], observed, override=scope, reason=reason)
        accepted = entry.accept_overlap("owner/repo", [12], observed, override=scope, reason="  proceed deliberately  ")
        self.assertEqual(accepted, {"observation_digest": scope, "reason": "proceed deliberately"})
        for repo, issues, obs in (("other/repo", [12], observed), ("owner/repo", [13], observed),
                                 ("owner/repo", [12], dict(observed, matches=["new competitor"]))):
            with self.subTest(repo=repo, issues=issues, observed=obs), self.assertRaises(core.CoordinatorError):
                entry.accept_overlap(repo, issues, obs, override=scope, reason="old decision")
        with self.assertRaises(core.CoordinatorError):
            entry.accept_overlap("owner/repo", [12], {"coverage": "complete", "matches": [],
                "errors": [], "local_digest": "changed"}, override=scope, reason="old decision")

    def test_no_issue_overlap_is_not_applicable_and_never_calls_remote(self):
        with mock.patch.object(entry, "_open_prs", side_effect=AssertionError("no remote issue check")), \
                mock.patch.object(entry, "_git", side_effect=AssertionError("no branch issue check")):
            observed = entry.overlap_observation("/tmp/entry", self.library, "owner/repo", [])
        self.assertEqual(observed["coverage"], "not-applicable")
        self.assertIsNone(entry.accept_overlap("owner/repo", [], observed))

    def test_local_recheck_detects_new_competing_claim(self):
        observed = self.observe()
        material = dict(self.candidate, issues=[12], overlap=observed)
        entry.verify_local_overlap(self.library, material, worktree="/tmp/entry")
        self.library.records["new"] = {"build_lease": {"current": self.claim("preparing")}}
        with self.assertRaisesRegex(core.CoordinatorError, "claims changed"):
            entry.verify_local_overlap(self.library, material, worktree="/tmp/entry")

    def test_malformed_paginated_remote_response_never_becomes_complete(self):
        for data in ({"message": "error"}, [{"number": 1}], [[{"number": 1}]]):
            result = subprocess.CompletedProcess([], 0, stdout=json.dumps(data), stderr="")
            with self.subTest(data=data), mock.patch.object(entry.subprocess, "run", return_value=result), \
                    self.assertRaisesRegex(core.CoordinatorError, "completely observed"):
                entry._open_prs("/tmp/entry", "owner/repo")


if __name__ == "__main__":
    unittest.main()
