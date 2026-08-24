#!/usr/bin/env python3
"""Focused tests for bounded and recoverable GitHub mutations."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest
from unittest import mock
import argparse
import contextlib
import io

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_coordinator_github as github  # noqa: E402
import build_coordinator as bc  # noqa: E402
from test_build_coordinator import BASE, HEAD_A, CoordinatorCase, plan  # noqa: E402


ROOT = Path(__file__).resolve().parents[2]


class TestRemoteBounds(unittest.TestCase):
    def test_oversized_body_is_rejected_before_remote_mutation(self):
        with self.assertRaisesRegex(github.core.CoordinatorError, "safe publication budget"):
            github.require_body_budget("x" * 60_001, "Issue body")

    def test_nothing_here_writes_a_plan_to_github_any_more(self):
        # The publication half of this module retired with the sealed-plan cutover: no plan is written
        # into an Issue body, no dedicated Build Issue is authored behind a creation nonce, and no
        # closing link is stamped onto the PR from here. Asserted against the module surface, because a
        # write path with no caller is exactly the kind of spare part that gets wired back up.
        for gone in ("publish_issue", "create_or_resume_build_issue", "ensure_pr_closes_issue",
                     "replace_handoff_block", "BUILD_MARKER"):
            self.assertFalse(hasattr(github, gone), gone)

    def test_the_readers_that_recognise_an_old_body_are_kept(self):
        # They are what lets a pre-cutover artifact be identified and refused by name rather than
        # misread; they retire with the v1 schemas in the successor plan's sunset.
        for kept in ("plan_block", "durable_plan", "handoff_block", "find_handoff_block"):
            self.assertTrue(hasattr(github, kept), kept)


class TestReadyTransitionRace(CoordinatorCase):
    def test_head_or_body_race_returns_pr_to_draft(self):
        self.seed()
        preview = {"repository": "owner/repo", "pr": 7, "commit": HEAD_A, "base": BASE,
                   "body_digest": bc._digest(b"before"), "snapshot_revision": self.state()["revision"],
                   "action": "mark-ready", "merge": False}
        raced = {"state": "OPEN", "isDraft": False, "headRefOid": "b" * 40,
                 "baseRefOid": BASE, "body": "changed"}
        draft = {**raced, "isDraft": True}
        with mock.patch.object(bc, "_submit_preview", return_value=preview), \
                mock.patch.object(bc.github, "set_ready"), mock.patch.object(bc.github, "set_draft") as redraft, \
                mock.patch.object(bc.github, "pr_state", side_effect=[raced, draft]), \
                self.assertRaisesRegex(bc.CoordinatorError, "was reversed"), \
                contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_submit_apply(argparse.Namespace(plan=str(self.plan_path)), self.store)
        redraft.assert_called_once()
        self.assertEqual(self.state()["submission"], "draft")

    def test_final_local_stability_failure_reverses_recorded_ready_state(self):
        self.seed()
        preview = {"repository": "owner/repo", "pr": 7, "commit": HEAD_A, "base": BASE,
                   "body_digest": bc._digest(b"body"), "snapshot_revision": self.state()["revision"],
                   "action": "mark-ready", "merge": False}
        ready = {"state": "OPEN", "isDraft": False, "headRefOid": HEAD_A,
                 "baseRefOid": BASE, "body": "body"}
        draft = {**ready, "isDraft": True}
        class ChangedAfter:
            def __enter__(self): return HEAD_A
            def __exit__(self, *unused): raise bc.CoordinatorError("working tree changed after ready")
        with mock.patch.object(bc.core, "StableCommit", return_value=ChangedAfter()), \
                mock.patch.object(bc, "_submit_preview", return_value=preview), \
                mock.patch.object(bc.github, "set_ready"), \
                mock.patch.object(bc.github, "set_draft") as redraft, \
                mock.patch.object(bc.github, "pr_state", side_effect=[ready, draft]), \
                self.assertRaisesRegex(bc.CoordinatorError, "changed after ready"):
            bc.cmd_submit_apply(argparse.Namespace(plan=str(self.plan_path)), self.store)
        redraft.assert_called_once()
        self.assertEqual(self.state()["submission"], "draft")


class TestCoordinatorOwnedLabel(unittest.TestCase):
    """The bind-time coordinator-ownership label helper (StarshipSuperjam/engine-template#1014)."""

    def test_tag_creates_the_label_before_adding_it_with_the_declared_attributes(self):
        with mock.patch.object(github.core, "must_run", return_value="") as remote:
            ok = github.tag_coordinator_owned(ROOT, "owner/repo", 7)
        self.assertTrue(ok)
        calls = [c.args[0] for c in remote.call_args_list]
        self.assertEqual(len(calls), 2)
        # create-before-add: `gh pr edit --add-label` fails unless the label already exists, so the helper
        # must create it (idempotently, via --force) first, then apply it.
        self.assertEqual(calls[0][:3], ["gh", "label", "create"])
        self.assertIn(github.COORDINATOR_OWNED_LABEL, calls[0])
        self.assertIn("--force", calls[0])
        self.assertIn(github.COORDINATOR_OWNED_LABEL_COLOR, calls[0])
        self.assertIn(github.COORDINATOR_OWNED_LABEL_DESCRIPTION, calls[0])
        self.assertEqual(calls[1][:4], ["gh", "pr", "edit", "7"])
        self.assertIn("--add-label", calls[1])
        self.assertIn(github.COORDINATOR_OWNED_LABEL, calls[1])
        # GitHub caps a label description at 100 chars.
        self.assertLessEqual(len(github.COORDINATOR_OWNED_LABEL_DESCRIPTION), 100)

    def test_tag_is_non_fatal_when_the_add_label_call_fails(self):
        # create succeeds, add-label fails -> caught, returns False, never raises.
        def must_run(argv, root=None):
            if "--add-label" in argv:
                raise github.core.CoordinatorError("gh pr edit failed")
            return ""
        with mock.patch.object(github.core, "must_run", side_effect=must_run):
            self.assertFalse(github.tag_coordinator_owned(ROOT, "owner/repo", 7))


if __name__ == "__main__":
    unittest.main()
