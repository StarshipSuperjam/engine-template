#!/usr/bin/env python3
"""Focused tests for commit evidence and snapshot transactions."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_coordinator_core as core  # noqa: E402


class TestExactCommitEvidence(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "Build Test"], cwd=self.root, check=True)
        (self.root / "tracked.txt").write_text("committed\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "initial"], cwd=self.root, check=True)

    def test_dirty_initial_tree_cannot_be_used_as_commit_evidence(self):
        (self.root / "tracked.txt").write_text("dirty fix\n", encoding="utf-8")
        with self.assertRaisesRegex(core.CoordinatorError, "clean working tree"):
            with core.StableCommit(self.root, "validation"):
                pass

    def test_tracked_change_produced_during_validation_invalidates_evidence(self):
        with self.assertRaisesRegex(core.CoordinatorError, "working tree changed"):
            with core.StableCommit(self.root, "validation"):
                (self.root / "tracked.txt").write_text("generated\n", encoding="utf-8")

    def test_head_change_during_commit_bound_activity_invalidates_evidence(self):
        with self.assertRaisesRegex(core.CoordinatorError, "HEAD changed"):
            with core.StableCommit(self.root, "review packet"):
                (self.root / "second.txt").write_text("second\n", encoding="utf-8")
                subprocess.run(["git", "add", "second.txt"], cwd=self.root, check=True)
                subprocess.run(["git", "commit", "-qm", "second"], cwd=self.root, check=True)


class TestSnapshotCAS(unittest.TestCase):
    def test_internal_revision_guard_rejects_stale_result(self):
        with tempfile.TemporaryDirectory() as directory:
            schema = Path(directory) / "schema.json"
            schema.write_text(json.dumps({
                "type": "object", "additionalProperties": False,
                "required": ["revision", "value"],
                "properties": {"revision": {"type": "integer"}, "value": {"type": "integer"}},
            }), encoding="utf-8")
            path = Path(directory) / "state.json"
            store = core.StateStore(str(path), schema)
            store.create({"revision": 1, "value": 0})
            observed = store.read()["revision"]
            store.mutate(lambda state: state.update({"value": 1}))
            with self.assertRaisesRegex(core.CoordinatorError, "reload status"):
                store.mutate(lambda state: state.update({"value": 2}), from_revision=observed)


class TestModuleBoundaries(unittest.TestCase):
    def test_extracted_services_do_not_import_the_public_cli(self):
        tools = Path(__file__).resolve().parent
        for name in ("build_coordinator_core.py", "build_coordinator_spec.py",
                     "build_coordinator_review.py", "build_coordinator_github.py",
                     "build_coordinator_dag.py", "build_coordinator_work.py"):
            with self.subTest(name=name):
                self.assertNotIn("import build_coordinator\n", (tools / name).read_text())


if __name__ == "__main__":
    unittest.main()
