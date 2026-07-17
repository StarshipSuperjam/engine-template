"""test_memory_pointer_public_safety.py — the construction-only pointer leak guard (#224).

The guard catches a CONFIGURED memory-backup pointer accidentally committed to the PUBLIC engine-template
construction repo. It reads the COMMITTED pointer (git show HEAD), construction-gated on the root CLAUDE.md, so
the maintainer's local skip-worktree-configured pointer never trips a local floor run, and a deployed repo's
legitimately-configured pointer is never flagged.
"""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memory_pointer_public_safety_check as guard  # noqa: E402

PLACEHOLDER = json.dumps({"schema_version": 1, "configured": False})
CONFIGURED = json.dumps({"schema_version": 1, "owner": "someone", "repo": "engine-memory-vault",
                         "branch": "main", "namespace": "deadbeef" * 4, "created_at": "2026-06-23T00:00:00Z"})


class IsConfiguredTests(unittest.TestCase):
    def test_placeholder_is_not_configured(self):
        self.assertFalse(guard.is_configured_pointer(PLACEHOLDER))

    def test_a_pointer_with_coordinates_is_configured(self):
        self.assertTrue(guard.is_configured_pointer(CONFIGURED))

    def test_garbage_or_partial_is_not_configured(self):
        self.assertFalse(guard.is_configured_pointer("not json"))
        self.assertFalse(guard.is_configured_pointer("[]"))
        self.assertFalse(guard.is_configured_pointer(json.dumps({"schema_version": 1, "owner": "x"})))


class CheckTests(unittest.TestCase):
    def setUp(self):
        self._cons, self._txt = guard._is_construction_repo, guard._committed_pointer_text

    def tearDown(self):
        guard._is_construction_repo, guard._committed_pointer_text = self._cons, self._txt

    def _drive(self, *, construction, committed):
        guard._is_construction_repo = lambda: construction
        guard._committed_pointer_text = lambda pointer_rel=guard.POINTER_REL: committed
        return guard.check()

    def test_configured_pointer_in_the_construction_repo_is_a_hard_finding(self):
        f = self._drive(construction=True, committed=CONFIGURED)
        self.assertIsNotNone(f)
        self.assertEqual(f["severity"], "hard")
        self.assertIn("placeholder", f["message"])
        self.assertEqual(f["location"]["file"], guard.POINTER_REL)

    def test_placeholder_in_the_construction_repo_passes(self):
        self.assertIsNone(self._drive(construction=True, committed=PLACEHOLDER))

    def test_a_configured_pointer_in_a_deployed_repo_is_ignored(self):
        # A deployed project committing its own vault coordinates is legitimate (the read is fine on any repo;
        # only the digest CONTENT is gated) — the guard must never flag the operator's own choice.
        self.assertIsNone(self._drive(construction=False, committed=CONFIGURED))

    def test_an_unreadable_committed_state_degrades_to_a_pass(self):
        # A backstop never false-fails a build over a condition it can't read (git absent / detached HEAD).
        self.assertIsNone(self._drive(construction=True, committed=None))


if __name__ == "__main__":
    unittest.main()
