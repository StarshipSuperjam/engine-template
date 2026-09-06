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

    def test_a_directory_a_provides_pattern_names_stays_a_corner_after_its_last_file_is_deleted(self):
        # The register enumerates files that exist; the pattern's directory does not depend on one.
        deleted = [("harness/run.py", "D")]
        self.assertEqual(_classify(deleted)["verdict"], cc.VERDICT_PROJECT_ONLY,
                         "without the pattern the deletion is invisible to the register — the gap")
        manifest = _classify(deleted, provides_patterns=("harness/*.py",))
        self.assertEqual(manifest["reason"]["code"], "engine-owned-path")
        self.assertEqual(cc.register_corners(set(), ("harness/*.py", "root-file.md")), frozenset({"harness/"}))

    def test_a_mixed_change_sets_reason_code_follows_precedence_not_sort_order(self):
        register = REGISTER | {"harness/x.py"}
        floor_last = [("harness/x.py", "M"), (".claude/x.md", "M"), ("zz-CLAUDE.md", "M"), ("CLAUDE.md", "M")]
        self.assertEqual(_classify(floor_last, register=register)["reason"]["code"], "floor-path")
        corner_after_owned = [("harness/x.py", "M"), (".claude/x.md", "M")]
        self.assertEqual(_classify(corner_after_owned, register=register)["reason"]["code"], "engine-corner-path")
        self.assertEqual(_classify([("harness/x.py", "M"), ("src/a.py", "M")], register=register)["reason"]["code"],
                         "engine-owned-path")

    def test_floor_and_corner_matching_is_case_folded(self):
        self.assertEqual(_classify([(".Engine/x.py", "M")])["reason"]["code"], "engine-corner-path")
        self.assertEqual(_classify([("claude.md", "M")])["reason"]["code"], "floor-path")
        self.assertEqual(_classify([("Harness/y.py", "A")], register=REGISTER | {"harness/x.py"})["reason"]["code"],
                         "engine-owned-path")

    def test_the_per_path_rule_is_one_function_and_the_loop_agrees_with_it(self):
        register = REGISTER | {"harness/x.py"}
        corners = cc.register_corners(register, ("vendor/*",))
        paths = ["src/a.py", "CLAUDE.md", ".github/x.yml", "harness/z.py", "vendor/lib.js", "README.md"]
        manifest = _classify([(p, "M") for p in paths], register=register, provides_patterns=("vendor/*",))
        self.assertEqual(manifest["project_paths"],
                         sorted(p for p in paths if cc.is_project_owned(p, register, corners)))
        self.assertEqual(manifest["engine_paths"],
                         sorted(p for p in paths if cc.engine_reason(p, register, corners) is not None))

    def test_the_detail_reads_grammatically_for_one_path_and_for_many(self):
        self.assertIn("src/a.py lies outside everything the Engine owns", _classify([("src/a.py", "M")])["reason"]["detail"])
        many = _classify([("src/a.py", "M"), ("src/b.py", "M")])["reason"]["detail"]
        self.assertIn("src/a.py, src/b.py lie outside everything the Engine owns", many)
        self.assertNotIn("reads, executes", many)
        self.assertIn("CLAUDE.md touches what the Engine owns", _classify([("CLAUDE.md", "M")])["reason"]["detail"])
        self.assertEqual(cc.name_paths(["a", "b", "c", "d", "e"]), "a, b, c (+2 more)")
        self.assertEqual((cc.count_paths(["a"]), cc.count_paths(["a", "b"])), ("1 changed path", "2 changed paths"))


class TestReceiptAdmission(unittest.TestCase):
    """Which engine-ci receipt a merge proof may stand on — the rule lives here, in the guarded module."""

    _PROJECT_ONLY = {"verdict": "project-only", "reason": {"code": "project-only"}}
    _ENGINE = {"verdict": "engine-affecting", "reason": {"code": "engine-corner-path"}}

    def test_a_full_receipt_stands_alone(self):
        self.assertEqual(cc.admit_receipt({"mode": "full"}, None), "full")
        self.assertEqual(cc.admit_receipt({"mode": "full"}, self._ENGINE), "full")

    def test_a_project_only_receipt_stands_only_on_an_agreeing_re_derived_verdict(self):
        self.assertEqual(cc.admit_receipt({"mode": "project-only"}, self._PROJECT_ONLY), "project-only")
        for disagreeing in (self._ENGINE, None, {}):
            with self.assertRaisesRegex(cc.ClassificationError, "narrower than the change set warrants"):
                cc.admit_receipt({"mode": "project-only"}, disagreeing)

    def test_any_other_mode_names_no_arm(self):
        for receipt in ({}, None, {"mode": "reuse"}, {"mode": "FULL"}, "full"):
            with self.assertRaisesRegex(cc.ClassificationError, "names no arm"):
                cc.admit_receipt(receipt, self._PROJECT_ONLY)

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

    def test_deleting_the_last_file_under_a_provided_directory_keeps_the_corner(self):
        _write(self.root, ".engine/modules/core/manifest.json",
               json.dumps({"id": "core", "provides": {"tool": [".engine/tools/*.py", "harness/*.py"]}}))
        _write(self.root, "harness/run.py", "x\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "harness")
        _git(self.root, "checkout", "-q", "-b", "topic")
        _git(self.root, "rm", "-q", "harness/run.py")
        _git(self.root, "commit", "-q", "-m", "delete the harness")
        _git(self.root, "checkout", "-q", "main")
        _git(self.root, "merge", "-q", "--no-ff", "-m", "merge", "topic")
        register, patterns = cc.register_and_patterns_of(self.root)
        self.assertNotIn("harness/run.py", register)
        self.assertIn("harness/*.py", patterns)
        self.assertIn("harness/", cc.register_corners(register, patterns))
        self.assertEqual(cc.classify_merge_checkout(self.root)["reason"]["code"], "engine-owned-path")

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
            # One read yields both halves, and a failure yields neither: no pattern set survives a register
            # that could not be read, so no caller can build fewer corners out of a failed read.
            self.assertEqual(cc.register_and_patterns_of(self.root), (None, ()))
            self.assertEqual(cc.classify_range(self.root, "HEAD", "HEAD")["reason"]["code"], "register-unreadable")

    def test_the_manifests_are_read_once_per_classification(self):
        with mock.patch.object(mc, "discover_manifests", wraps=mc.discover_manifests) as discover:
            cc.classify_range(self.root, "HEAD", "HEAD")
        self.assertEqual(discover.call_count, 1)


# What an empty range (HEAD against HEAD) must say about THIS checkout, by its identity. The tests below
# read the identity first rather than assuming the home: run from a deployed copy — a throwaway clone with a
# foreign origin, or a real downstream project — the same code is right to answer differently, and a test
# that hard-coded `home-repository` would fail there while proving nothing (a reviewer ran exactly that).
_EMPTY_RANGE_BY_IDENTITY = {cc.IDENTITY_HOME: "home-repository",
                            cc.IDENTITY_DEPLOYED: "no-changed-paths",
                            cc.IDENTITY_UNREADABLE: "identity-unreadable"}


class TestThisRepository(unittest.TestCase):
    def test_this_checkout_classifies_by_its_identity_never_project_only(self):
        identity = cc.identity_of(validate.ROOT)
        manifest = cc.classify_range(validate.ROOT, "HEAD", "HEAD")
        self.assertEqual(manifest["identity"], identity)
        self.assertEqual(manifest["reason"]["code"], _EMPTY_RANGE_BY_IDENTITY[identity])
        self.assertEqual(manifest["verdict"], cc.VERDICT_ENGINE_AFFECTING)


class TestOnAThrowawayDeployedClone(unittest.TestCase):
    """The identity-dependent tests, run again on a clone of this very tree whose origin is foreign — the
    shape of a deployed copy, with the REAL manifests and register. This is the case a home-only assertion
    silently never covered."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = os.path.join(cls.tmp.name, "clone")
        # `--shared` borrows this checkout's object store, so the clone costs a checkout and nothing more.
        subprocess.run(["git", "clone", "-q", "--shared", validate.ROOT, cls.root],
                       check=True, capture_output=True, text=True)
        _git(cls.root, "remote", "set-url", "origin", "https://github.com/test/downstream-product.git")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_the_clone_reads_as_deployed_with_a_live_register(self):
        self.assertEqual(cc.identity_of(self.root), cc.IDENTITY_DEPLOYED)
        register = cc.register_of(self.root)
        self.assertIsNotNone(register)
        self.assertIn(cc.ENGINE_MANIFEST_REL, register)
        manifest = cc.classify_range(self.root, "HEAD", "HEAD")
        self.assertEqual(manifest["reason"]["code"], _EMPTY_RANGE_BY_IDENTITY[cc.IDENTITY_DEPLOYED])
        self.assertEqual(manifest["verdict"], cc.VERDICT_ENGINE_AFFECTING)

    def test_a_product_commit_on_the_clone_is_project_only_and_an_engine_commit_is_not(self):
        _git(self.root, "checkout", "-q", "-b", "product")
        _write(self.root, "src/app.py", "print(1)\n")
        _git(self.root, "add", "src/app.py")
        _git(self.root, "commit", "-q", "-m", "product change")
        product = cc.classify_range(self.root, "HEAD~1", "HEAD")
        self.assertEqual((product["verdict"], product["project_paths"]), (cc.VERDICT_PROJECT_ONLY, ["src/app.py"]))
        _write(self.root, ".engine/tools/validate.py", "# touched\n")
        _git(self.root, "add", ".engine/tools/validate.py")
        _git(self.root, "commit", "-q", "-m", "engine change")
        engine = cc.classify_range(self.root, "HEAD~1", "HEAD")
        self.assertEqual(engine["reason"]["code"], "engine-corner-path")

    def test_the_cli_answers_for_the_clone_through_root(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            self.assertEqual(cc.main(["classify", "--base", "HEAD", "--head", "HEAD", "--root", self.root]), 0)
        manifest = json.loads(out.getvalue())
        self.assertEqual(manifest["identity"], cc.IDENTITY_DEPLOYED)
        self.assertEqual(manifest["reason"]["code"], "no-changed-paths")


class TestCli(unittest.TestCase):
    def test_help_exits_zero_without_reading_the_tree(self):
        with mock.patch.object(cc, "classify_range", side_effect=AssertionError("must not run")), \
                mock.patch.object(cc, "classify_merge_checkout", side_effect=AssertionError("must not run")):
            for argv in ([], ["--help"], ["-h"]):
                out = io.StringIO()
                with mock.patch("sys.stdout", out):
                    self.assertEqual(cc.main(argv), 0, argv)
                self.assertIn("usage:", out.getvalue())

    def test_an_unknown_argument_exits_two_and_names_the_problem_once(self):
        # One usage block, preceded by the specific complaint — not argparse's own usage AND ours.
        for argv, named in ((["classify", "--base", "x", "--head", "y", "--bogus"], "--bogus"),
                            (["frobnicate"], "frobnicate"), (["classify", "--base", "x"], "--head")):
            err = io.StringIO()
            with mock.patch("sys.stderr", err):
                self.assertEqual(cc.main(argv), 2, argv)
            text = err.getvalue()
            self.assertEqual(text.count("usage:"), 1, text)
            self.assertIn(named, text.splitlines()[0], text)
            self.assertTrue(text.startswith("change_classification.py: "), text)

    def test_classify_prints_the_manifest(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            self.assertEqual(cc.main(["classify", "--base", "HEAD", "--head", "HEAD"]), 0)
        manifest = json.loads(out.getvalue())
        self.assertEqual(manifest["schema_version"], cc.SCHEMA_VERSION)
        self.assertEqual(manifest["reason"]["code"], _EMPTY_RANGE_BY_IDENTITY[cc.identity_of(validate.ROOT)])


if __name__ == "__main__":
    unittest.main()
