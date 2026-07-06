#!/usr/bin/env python3
"""Regression tests for the dependency-pinning inspector (.engine/tools/dependency_discipline/pinning.py)."""
from __future__ import annotations
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # .engine/tools on sys.path
from dependency_discipline import pinning  # noqa: E402
import quiet_call  # noqa: E402  (capture a demo walkthrough's stdout so it can't bury the suite summary)
import validate  # noqa: E402


class PinningTests(unittest.TestCase):
    def _root(self, files) -> str:
        """A throwaway root seeded with the given relative file paths (each written non-empty)."""
        d = tempfile.mkdtemp(prefix="engine-pinning-test-")
        self.addCleanup(shutil.rmtree, d, True)
        for rel in files:
            path = os.path.join(d, rel)
            parent = os.path.dirname(path)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{}")
        return d

    def _severities(self, fs) -> set:
        return {f["severity"] for f in fs}

    def _snapshot(self, root) -> dict:
        out = {}
        for cur, _dirs, names in os.walk(root):
            for n in names:
                p = os.path.join(cur, n)
                out[os.path.relpath(p, root)] = os.path.getsize(p)
        return out

    # --- the disclosed no-op (empty root) ---------------------------------------------------------
    def test_empty_root_discloses_the_no_op(self):
        fs = pinning.findings("soft", root=self._root([]))
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "soft")
        self.assertIsNone(fs[0]["location"])
        self.assertIn("isn't active here yet", fs[0]["message"])
        # Marked as a disclosed no-op so the validator can collapse it away from actionable notes (#322).
        self.assertIs(fs[0].get("not_applicable"), True)

    # --- §13 wall: the engine's own .engine/ tooling is never a product dependency ----------------
    def test_engine_walled_tooling_is_not_a_product_dependency(self):
        root = self._root([".engine/pyproject.toml", ".engine/uv.lock"])
        fs = pinning.findings("soft", root=root)
        self.assertEqual(len(fs), 1, "the engine's own .engine/ tooling must not count as a product dep")
        self.assertIn("isn't active here yet", fs[0]["message"])

    # --- per-ecosystem unpinned vs pinned ---------------------------------------------------------
    def test_node_unpinned_yields_one_soft_nudge(self):
        fs = pinning.findings("soft", root=self._root(["package.json"]))
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "soft")
        self.assertIn("aren't locked", fs[0]["message"])
        self.assertEqual(fs[0]["location"], {"file": "package.json", "line": None})

    def test_node_pinned_passes_cleanly_for_each_lock_flavor(self):
        for lock in ("package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml"):
            fs = pinning.findings("soft", root=self._root(["package.json", lock]))
            self.assertEqual(fs, [], f"{lock} should count package.json as pinned")

    def test_python_pyproject_without_lock_is_unpinned(self):
        fs = pinning.findings("soft", root=self._root(["pyproject.toml"]))
        self.assertEqual(len(fs), 1)
        self.assertIn("aren't locked", fs[0]["message"])

    def test_python_requirements_txt_counts_as_its_own_pin_record(self):
        self.assertEqual(pinning.findings("soft", root=self._root(["requirements.txt"])), [])

    def test_python_pyproject_with_lock_passes(self):
        for lock in ("uv.lock", "poetry.lock", "Pipfile.lock"):
            fs = pinning.findings("soft", root=self._root(["pyproject.toml", lock]))
            self.assertEqual(fs, [], f"pyproject.toml + {lock} should pass")

    def test_other_ecosystems_unpinned(self):
        for manifest in ("Cargo.toml", "go.mod", "Gemfile", "composer.json"):
            fs = pinning.findings("soft", root=self._root([manifest]))
            self.assertEqual(len(fs), 1, f"{manifest} alone should nudge")
            self.assertIn("aren't locked", fs[0]["message"])

    def test_other_ecosystems_pinned(self):
        for manifest, lock in (("Cargo.toml", "Cargo.lock"), ("go.mod", "go.sum"),
                               ("Gemfile", "Gemfile.lock"), ("composer.json", "composer.lock")):
            fs = pinning.findings("soft", root=self._root([manifest, lock]))
            self.assertEqual(fs, [], f"{manifest} + {lock} should pass")

    def test_multiple_ecosystems_report_only_the_unpinned_one(self):
        # node unpinned, rust pinned -> exactly one nudge, naming the node manifest.
        root = self._root(["package.json", "Cargo.toml", "Cargo.lock"])
        fs = pinning.findings("soft", root=root)
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["location"]["file"], "package.json")

    # --- the tier guarantee: never hard ----------------------------------------------------------
    def test_findings_are_never_hard(self):
        for files in ([], ["package.json"], [".engine/pyproject.toml"], ["pyproject.toml"], ["Gemfile"]):
            fs = pinning.findings("soft", root=self._root(files))
            self.assertNotIn("hard", self._severities(fs))

    # --- read-only: a run never changes the tree -------------------------------------------------
    def test_inspection_is_read_only(self):
        root = self._root(["package.json"])
        before = self._snapshot(root)
        pinning.findings("soft", root=root)
        self.assertEqual(self._snapshot(root), before)

    # --- the real repo can never turn engine-ci red from this check ------------------------------
    def test_real_repo_yields_no_hard_finding(self):
        fs = pinning.findings("soft")  # defaults to validate.ROOT (engine-template itself)
        self.assertNotIn("hard", self._severities(fs))

    # --- the falsifiable demo passes on the happy path -------------------------------------------
    def test_demo_passes(self):
        self.assertEqual(quiet_call.run(pinning.demo), 0)

    # --- the no-arg dispatch emits a JSON array (the custom/script contract) ----------------------
    def test_emit_findings_prints_a_json_array_and_returns_zero(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = pinning.emit_findings()
        self.assertEqual(rc, 0)
        parsed = json.loads(buf.getvalue())
        self.assertIsInstance(parsed, list)
        for f in parsed:
            self.assertIn("severity", f)
            self.assertIn("message", f)
            self.assertIn("location", f)

    def test_main_routes_demo_and_bare_invocation(self):
        self.assertEqual(quiet_call.run(pinning.main, ["demo"]), 0)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(pinning.main([]), 0)
        self.assertIsInstance(json.loads(buf.getvalue()), list)


if __name__ == "__main__":
    unittest.main()
