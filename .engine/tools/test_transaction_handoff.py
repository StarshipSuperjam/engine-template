#!/usr/bin/env python3
"""The pre-mutation state matrix and the two handoff shapes.

Every test here runs against a throwaway git repository, never this checkout: these paths commit and
mutate, and a test that succeeds in doing the wrong thing has then done it for real.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import transaction  # noqa: E402
import transaction_handoff as th  # noqa: E402


def git(root, *args):
    return subprocess.run(["git"] + list(args), cwd=root, capture_output=True, text=True)


class ThrowawayRepo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.email", "test@example.invalid")
        git(self.root, "config", "user.name", "Test")
        os.makedirs(os.path.join(self.root, ".engine", "modules"), exist_ok=True)
        self._write(".engine/modules/seed.json", "{}\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "seed")

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return rel


class TestPreMutationRefusals(ThrowawayRepo):
    def test_a_clean_tree_on_a_named_branch_is_ready(self):
        state = th.refuse_unless_ready([".engine/modules/design-review.json"], root=self.root)
        self.assertEqual(state["branch"], "main")

    def test_uncommitted_work_in_the_transaction_s_own_paths_refuses(self):
        self._write(".engine/modules/design-review.json", "{}\n")
        with self.assertRaises(transaction.TransactionRefused) as caught:
            th.refuse_unless_ready([".engine/modules/design-review.json"], root=self.root)
        self.assertEqual(caught.exception.code, "uncommitted-changes-in-scope")
        self.assertTrue(caught.exception.next_actions)

    def test_unrelated_uncommitted_work_does_not_refuse(self):
        """The operator's own work in progress is their business, not something to sweep in or block on."""
        self._write("README.md", "my own notes\n")
        state = th.refuse_unless_ready([".engine/modules/design-review.json"], root=self.root)
        self.assertIn("README.md", state["dirty_paths"])

    def test_a_detached_head_refuses(self):
        head = git(self.root, "rev-parse", "HEAD").stdout.strip()
        git(self.root, "checkout", "-q", head)
        with self.assertRaises(transaction.TransactionRefused) as caught:
            th.refuse_unless_ready([".engine/modules/x.json"], root=self.root)
        self.assertEqual(caught.exception.code, "detached-head")

    def test_a_transaction_may_not_claim_the_plan_library_or_the_operator_s_memory(self):
        for forbidden in (".engine/plans/x.json", ".engine/memory/y.json"):
            with self.assertRaises(transaction.TransactionRefused) as caught:
                th.refuse_unless_ready([forbidden], root=self.root)
            self.assertEqual(caught.exception.code, "path-not-claimable")

    def test_every_refusal_carries_a_stable_code_and_a_way_forward(self):
        head = git(self.root, "rev-parse", "HEAD").stdout.strip()
        git(self.root, "checkout", "-q", head)
        with self.assertRaises(transaction.TransactionRefused) as caught:
            th.refuse_unless_ready([".engine/modules/x.json"], root=self.root)
        code = caught.exception.code
        self.assertTrue(code.islower() and " " not in code)
        self.assertTrue(caught.exception.next_actions)
        self.assertTrue(caught.exception.explanation)


class TestSelectiveCommit(ThrowawayRepo):
    def test_only_the_declared_paths_are_committed(self):
        """The property that makes the change revertable as a unit."""
        self._write(".engine/modules/design-review.json", '{"id": "design-review"}\n')
        self._write("product-code.py", "print('the operator's own work')\n".replace("'s", "s"))
        result = th.commit_in_tree([".engine/modules/design-review.json"],
                                   "Add the design-review add-on", root=self.root)
        self.assertTrue(result["committed"])
        listed = git(self.root, "show", "--name-only", "--format=", "HEAD").stdout.split()
        self.assertEqual(listed, [".engine/modules/design-review.json"])
        # The operator's unrelated work is still uncommitted, exactly where they left it.
        self.assertIn("product-code.py", git(self.root, "status", "--porcelain").stdout)

    def test_the_commit_is_revertable_as_a_unit(self):
        self._write(".engine/modules/design-review.json", '{"id": "design-review"}\n')
        th.commit_in_tree([".engine/modules/design-review.json"], "Add it", root=self.root)
        self.assertTrue(os.path.exists(os.path.join(self.root, ".engine/modules/design-review.json")))
        git(self.root, "revert", "--no-edit", "HEAD")
        self.assertFalse(os.path.exists(os.path.join(self.root, ".engine/modules/design-review.json")))

    def test_a_deletion_is_staged_too(self):
        target = ".engine/modules/seed.json"
        os.remove(os.path.join(self.root, target))
        result = th.commit_in_tree([target], "Remove the seed module", root=self.root)
        self.assertTrue(result["committed"])
        self.assertNotIn(target, git(self.root, "ls-files").stdout)

    def test_nothing_to_commit_is_reported_rather_than_faked(self):
        result = th.commit_in_tree([".engine/modules/seed.json"], "No change", root=self.root)
        self.assertIsNone(result["committed"])
        self.assertIn("already in place", result["note"])


class TestHandoffShapes(unittest.TestCase):
    def test_an_in_tree_handoff_tells_the_operator_how_to_undo_it(self):
        handoff = th.in_tree_handoff({"committed": "abc1234"}, "Added the design-review add-on.")
        self.assertEqual(handoff["kind"], "in-tree-commit")
        self.assertIn("reverting that commit", handoff["summary"])
        self.assertEqual(handoff["reference"], "abc1234")

    def test_a_pull_request_handoff_says_nothing_changes_until_merge(self):
        handoff = th.pull_request_handoff({"url": "https://example.invalid/pr/1"}, "The update is ready.")
        self.assertEqual(handoff["kind"], "pull-request")
        self.assertIn("until you merge", handoff["summary"])


class TestStaysOnTheArrivalFloor(unittest.TestCase):
    def test_standard_library_only_with_the_future_import(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "transaction_handoff.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("from __future__ import annotations", source)
        for third_party in ("jsonschema", "yaml", "requests"):
            self.assertNotIn("import {0}".format(third_party), source)
        self.assertNotIn("import tomllib", source)


class TestTheOpenerStagesSelectively(unittest.TestCase):
    """The pull-request path must respect declared paths too, not sweep the tree."""

    def test_module_manager_stages_declared_paths_when_given_them(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "module_manager.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('stage_step = ["git", "add", "-A"] if not paths else ["git", "add", "--"] + list(paths)',
                      source)


if __name__ == "__main__":
    unittest.main()
