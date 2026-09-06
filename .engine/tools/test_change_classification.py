#!/usr/bin/env python3
"""Self-tests for the change classifier: the declared floor, the fail-closed vocabulary, and the git seam.

The load-bearing cases are the ones that would let a change set the Engine cares about read as the
project's: a deleted or renamed-away foundation file (absent from the live register), the root MCP wiring
file (never in it), a register that shrank, a one-parent HEAD whose diff is silently narrower, and the home
repository. Each is held here by name, and the floor is held against the Engine's own declarations so a
namespace added there cannot be missed here.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import change_classification as cc  # noqa: E402
import module_coherence as mc  # noqa: E402
import validate  # noqa: E402

REGISTER = {cc.ENGINE_MANIFEST_REL, ".engine/tools/validate.py", ".agents/skills/x/SKILL.md", "CLAUDE.md"}


def _classify(entries, *, identity=cc.IDENTITY_DEPLOYED, register=REGISTER, **kw):
    return cc.classify_paths(entries, identity=identity, register=register, **kw)


class TestFloorDeclarations(unittest.TestCase):
    """The floor is held against what the Engine itself declares elsewhere."""

    def test_every_foundation_root_file_is_a_floor_file(self):
        root_members = [m for m in mc.FOUNDATION_INFRA if "/" not in m]
        self.assertTrue(root_members, "FOUNDATION_INFRA names root files; the floor must cover them by name")
        for member in root_members:
            self.assertIn(member, cc.FLOOR_FILES, member)

    def test_every_wiring_target_is_on_the_floor(self):
        for seam, rel in mc.WIRING_TARGETS.items():
            self.assertIsNotNone(cc.floor_hit(rel), f"{seam} wires {rel}, which the floor does not cover")

    def test_every_home_travel_namespace_is_a_floor_prefix(self):
        for prefix in mc._HOME_TRAVEL_PREFIXES:
            top = prefix.split("/", 1)[0] + "/"
            self.assertIn(top, cc.FLOOR_PREFIXES, prefix)

    def test_every_register_input_lives_under_a_corner(self):
        # What makes reading the register from the pull-request HEAD safe: a change that shrinks the
        # register must itself be engine-affecting. The manifests, the source that declares
        # FOUNDATION_INFRA, and CODEOWNERS are the register's inputs.
        inputs = [rel for rel, _ in mc.discover_manifests()]
        inputs += [".engine/tools/module_coherence.py", ".github/CODEOWNERS", mc.ENGINE_MANIFEST_REL]
        for rel in inputs:
            self.assertEqual(cc.floor_hit(rel), "engine-corner-path", rel)

    def test_every_live_register_path_is_engine_affecting(self):
        register = set(mc.engine_owned_paths(mc.discover_manifests()))
        self.assertIn(cc.ENGINE_MANIFEST_REL, register)
        entries = [(p, "M") for p in sorted(register)]
        manifest = _classify(entries, register=register)
        self.assertEqual(manifest["verdict"], cc.VERDICT_ENGINE_AFFECTING)
        self.assertEqual(manifest["project_paths"], [])
        self.assertEqual(len(manifest["engine_paths"]), len(register))


class TestPureClassification(unittest.TestCase):
    def test_reason_vocabulary_is_closed_and_every_code_is_reachable(self):
        reached = {
            "git-unavailable": _classify([("src/a.py", "M")], failure="boom"),
            "not-a-merge-checkout": _classify([], shape_failure="one parent"),
            "home-repository": _classify([("src/a.py", "M")], identity=cc.IDENTITY_HOME),
            "identity-unreadable": _classify([("src/a.py", "M")], identity=cc.IDENTITY_UNREADABLE),
            "register-unreadable": _classify([("src/a.py", "M")], register=None),
            "register-degenerate": _classify([("src/a.py", "M")], register={"CLAUDE.md"}),
            "no-changed-paths": _classify([]),
            "unrecognised-status": _classify([("src/a.py", "U")]),
            "floor-path": _classify([(".mcp.json", "M")]),
            "engine-corner-path": _classify([(".claude/commands/x.md", "A")]),
            "engine-owned-path": _classify([("harness/x.py", "M")], register=REGISTER | {"harness/x.py"}),
            "project-only": _classify([("src/a.py", "M")]),
        }
        self.assertEqual(set(reached), cc.REASONS)
        for code, manifest in reached.items():
            self.assertEqual(manifest["reason"]["code"], code)
            expected = cc.VERDICT_PROJECT_ONLY if code == "project-only" else cc.VERDICT_ENGINE_AFFECTING
            self.assertEqual(manifest["verdict"], expected, code)
            self.assertEqual(manifest["schema_version"], cc.SCHEMA_VERSION)

    def test_doubts_precede_the_paths_in_the_stated_order(self):
        # A git failure outranks everything; the home repository outranks an unreadable register; a
        # degenerate register outranks an empty change set.
        self.assertEqual(_classify([], identity=cc.IDENTITY_HOME, register=None, failure="x")["reason"]["code"],
                         "git-unavailable")
        self.assertEqual(_classify([], identity=cc.IDENTITY_HOME, register=None)["reason"]["code"],
                         "home-repository")
        self.assertEqual(_classify([], register={"x"})["reason"]["code"], "register-degenerate")

    def test_deleted_and_renamed_away_floor_files_are_engine_affecting(self):
        # The live register cannot see a file that is gone; the floor matches by name.
        for entries in ([("CLAUDE.md", "D")], [("AGENTS.md", "D")], [(".gitignore", "R"), ("ignore.txt", "R")],
                        [(".mcp.json", "D")]):
            manifest = _classify(entries, register={cc.ENGINE_MANIFEST_REL})
            self.assertEqual(manifest["reason"]["code"], "floor-path", entries)

    def test_deleted_and_renamed_project_files_stay_project_only(self):
        manifest = _classify([("src/old.py", "R"), ("src/new.py", "R"), ("docs/gone.md", "D"), ("tests/t.py", "A")])
        self.assertEqual(manifest["verdict"], cc.VERDICT_PROJECT_ONLY)
        self.assertEqual(manifest["project_paths"], ["docs/gone.md", "src/new.py", "src/old.py", "tests/t.py"])

    def test_a_directory_the_register_occupies_is_engine_territory(self):
        manifest = _classify([("harness/new_file.py", "A")], register=REGISTER | {"harness/x.py"})
        self.assertEqual(manifest["reason"]["code"], "engine-owned-path")

    def test_a_mixed_change_set_names_every_engine_path_and_keeps_the_project_ones(self):
        manifest = _classify([("src/a.py", "M"), (".engine/tools/validate.py", "M"), ("README.md", "M")])
        self.assertEqual(manifest["verdict"], cc.VERDICT_ENGINE_AFFECTING)
        self.assertEqual(manifest["reason"]["code"], "engine-corner-path")
        self.assertEqual(manifest["engine_paths"], [".engine/tools/validate.py"])
        self.assertEqual(manifest["project_paths"], ["README.md", "src/a.py"])

    def test_the_root_of_a_deployed_project_is_the_projects(self):
        manifest = _classify([("README.md", "M"), ("SECURITY.md", "A"), ("product-version.json", "M")])
        self.assertEqual(manifest["verdict"], cc.VERDICT_PROJECT_ONLY)

    def test_serialization_is_canonical(self):
        a = _classify([("src/a.py", "M")])
        b = _classify([("src/a.py", "M"), ("src/a.py", "A")])
        self.assertEqual(cc.serialize(a), cc.serialize(b))
        self.assertTrue(cc.digest(a).startswith("sha256:"))


def _git(root, *args):
    subprocess.run(["git", "-C", root, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                    "-c", "commit.gpgsign=false", *args], check=True, capture_output=True, text=True)


def _write(root, rel, text="x\n"):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class TestGitSeam(unittest.TestCase):
    """A real repository with a downstream origin and a two-parent merge — the production shape."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        _git(self.root, "init", "-q", "-b", "main")
        _git(self.root, "remote", "add", "origin", "https://github.com/test/product.git")
        _write(self.root, ".engine/engine.json", json.dumps({"home_repository": "test/home"}))
        _write(self.root, ".engine/modules/core/manifest.json",
               json.dumps({"id": "core", "provides": {"tool": [".engine/tools/*.py"]}}))
        _write(self.root, ".engine/tools/validate.py", "# engine\n")
        _write(self.root, "CLAUDE.md")
        _write(self.root, ".mcp.json", "{}\n")
        _write(self.root, "src/app.py", "print(1)\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "base")

    def tearDown(self):
        self.tmp.cleanup()

    def _merge_of(self, rel, text):
        """A pull-request-shaped history: a branch that changes `rel`, merged into main with two parents."""
        _git(self.root, "checkout", "-q", "-b", "topic")
        _write(self.root, rel, text)
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "change")
        _git(self.root, "checkout", "-q", "main")
        _git(self.root, "merge", "-q", "--no-ff", "-m", "merge", "topic")

    def test_a_product_change_in_a_deployed_copy_is_project_only(self):
        self._merge_of("src/app.py", "print(2)\n")
        manifest = cc.classify_merge_checkout(self.root)
        self.assertEqual(manifest["identity"], cc.IDENTITY_DEPLOYED)
        self.assertEqual(manifest["verdict"], cc.VERDICT_PROJECT_ONLY)
        self.assertEqual(manifest["project_paths"], ["src/app.py"])

    def test_the_mcp_wiring_file_is_engine_affecting(self):
        self._merge_of(".mcp.json", '{"mcpServers": {}}\n')
        manifest = cc.classify_merge_checkout(self.root)
        self.assertEqual(manifest["reason"]["code"], "floor-path")

    def test_a_deleted_foundation_file_is_engine_affecting_on_the_merge_checkout(self):
        _git(self.root, "checkout", "-q", "-b", "topic")
        _git(self.root, "rm", "-q", "CLAUDE.md")
        _git(self.root, "commit", "-q", "-m", "delete")
        _git(self.root, "checkout", "-q", "main")
        _git(self.root, "merge", "-q", "--no-ff", "-m", "merge", "topic")
        # The live register of the merged tree no longer names CLAUDE.md; the floor does.
        self.assertNotIn("CLAUDE.md", cc.register_of(self.root))
        self.assertEqual(cc.classify_merge_checkout(self.root)["reason"]["code"], "floor-path")

    def test_a_one_parent_head_is_not_a_merge_checkout(self):
        _write(self.root, "src/app.py", "print(3)\n")
        _git(self.root, "commit", "-q", "-am", "direct")
        manifest = cc.classify_merge_checkout(self.root)
        self.assertEqual(manifest["reason"]["code"], "not-a-merge-checkout")
        self.assertEqual(manifest["verdict"], cc.VERDICT_ENGINE_AFFECTING)
        # The range form still answers for the same commits, so a caller who knows the base is not stuck.
        self.assertEqual(cc.classify_range(self.root, "HEAD~1", "HEAD")["verdict"], cc.VERDICT_PROJECT_ONLY)

    def test_an_unreadable_identity_is_a_doubt(self):
        _write(self.root, ".engine/engine.json", "{not json")
        self.assertEqual(cc.identity_of(self.root), cc.IDENTITY_UNREADABLE)
        manifest = cc.classify_range(self.root, "HEAD", "HEAD")
        self.assertEqual(manifest["reason"]["code"], "identity-unreadable")

    def test_a_home_origin_reads_as_home(self):
        _git(self.root, "remote", "set-url", "origin", "https://github.com/test/home.git")
        self.assertEqual(cc.identity_of(self.root), cc.IDENTITY_HOME)
        self.assertEqual(cc.classify_range(self.root, "HEAD", "HEAD")["reason"]["code"], "home-repository")

    def test_a_bad_revision_is_git_unavailable(self):
        manifest = cc.classify_range(self.root, "no-such-rev", "HEAD")
        self.assertEqual(manifest["reason"]["code"], "git-unavailable")

    def test_rename_rows_contribute_both_sides(self):
        _git(self.root, "checkout", "-q", "-b", "topic")
        os.makedirs(os.path.join(self.root, "docs"))
        _git(self.root, "mv", "CLAUDE.md", "docs/notes.md")
        _git(self.root, "commit", "-q", "-m", "rename")
        entries, failure = cc.diff_entries(self.root, "main", "topic")
        self.assertIsNone(failure)
        self.assertEqual(sorted(entries), [("CLAUDE.md", "R"), ("docs/notes.md", "R")])

    def test_register_read_failure_is_a_doubt_not_a_crash(self):
        with mock.patch.object(mc, "discover_manifests", side_effect=RuntimeError("boom")):
            self.assertIsNone(cc.register_of(self.root))


class TestThisRepository(unittest.TestCase):
    def test_the_engine_home_classifies_as_home(self):
        manifest = cc.classify_range(validate.ROOT, "HEAD", "HEAD")
        self.assertEqual(manifest["reason"]["code"], "home-repository")
        self.assertEqual(manifest["verdict"], cc.VERDICT_ENGINE_AFFECTING)


class TestCli(unittest.TestCase):
    def test_help_exits_zero_without_reading_the_tree(self):
        with mock.patch.object(cc, "classify_range", side_effect=AssertionError("must not run")), \
                mock.patch.object(cc, "classify_merge_checkout", side_effect=AssertionError("must not run")):
            for argv in ([], ["--help"], ["-h"]):
                out = io.StringIO()
                with mock.patch("sys.stdout", out):
                    self.assertEqual(cc.main(argv), 0, argv)
                self.assertIn("usage:", out.getvalue())

    def test_an_unknown_argument_exits_two(self):
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            self.assertEqual(cc.main(["classify", "--bogus"]), 2)
            self.assertEqual(cc.main(["frobnicate"]), 2)
        self.assertIn("usage:", err.getvalue())

    def test_classify_prints_the_manifest(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            self.assertEqual(cc.main(["classify", "--base", "HEAD", "--head", "HEAD"]), 0)
        manifest = json.loads(out.getvalue())
        self.assertEqual(manifest["schema_version"], cc.SCHEMA_VERSION)
        self.assertEqual(manifest["reason"]["code"], "home-repository")


if __name__ == "__main__":
    unittest.main()
