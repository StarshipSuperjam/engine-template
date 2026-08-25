#!/usr/bin/env python3
"""Tests for repair_divergence — what a repair round touched, by kind of surface.

Lock the behaviours an operator cannot read code to verify: a regenerated guarded file is still reported as
guarded (precedence fails toward the more serious kind); an exclusive derived tree owns its own members and
never a lookalike sibling directory; a dynamic member's regenerated output is described as generated rather
than as authored work; the engine's own governing prose is NOT filed under "docs" where it would look cheap;
a binary file contributes no invented line count; a rename is attributed to the file the reader will open;
the tree measured is the root the caller passed, not whatever tree this file happens to live in; and any
failure to measure REFUSES loudly instead of returning an empty diff that would read as "nothing changed".
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from unittest import mock

import derived_state
import repair_divergence
import weakening_guard

NO_GUARDS = (frozenset(), ())          # an instance that has declared no extra guarded paths
NO_SCRIPTS: set = set()                # a check dir carrying no `params.script` rules


def _row(added, deleted, path: str) -> str:
    return f"{added}\t{deleted}\t{path}\0"


def _rename_row(added, deleted, old: str, new: str) -> str:
    return f"{added}\t{deleted}\t\0{old}\0{new}\0"


def _runner(payload: str):
    """A stand-in for the git call, so classification is tested without building a repo per case."""
    def run(argv, root):
        return payload
    return run


def _classify(payload: str, **kw) -> dict:
    kw.setdefault("derived_scripts", NO_SCRIPTS)
    kw.setdefault("instance_guards", NO_GUARDS)
    return repair_divergence.classify("/nowhere", "aaa", "bbb", runner=_runner(payload), **kw)


def _git(root: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, check=False)


class TestClassification(unittest.TestCase):
    def test_a_regenerated_guarded_file_is_reported_as_guarded_not_derived(self):
        # graph.json is a derived-state output AND, here, a deployment-declared guarded path. Precedence
        # must fail toward the serious kind: an operator reading the round must see the guardrail moved.
        result = _classify(_row(4, 1, ".engine/knowledge/graph.json"),
                           instance_guards=(frozenset({".engine/knowledge/graph.json"}), ()))
        self.assertEqual(result["files"]["guarded"], [".engine/knowledge/graph.json"])
        self.assertEqual(result["files"]["derived"], [])
        self.assertEqual(result["churn"]["guarded"], 5)

    def test_the_workflow_and_check_prefixes_are_guarded(self):
        result = _classify(_row(1, 0, ".github/workflows/engine-ci.yml")
                           + _row(2, 0, ".engine/check/engine-guard.json"))
        self.assertEqual(result["files"]["guarded"],
                         [".engine/check/engine-guard.json", ".github/workflows/engine-ci.yml"])

    def test_an_exclusive_derived_tree_owns_its_members_but_never_a_lookalike_sibling(self):
        result = _classify(_row(3, 0, ".codex/agents/reviewer.md")
                           + _row(1, 1, ".codex/agents-experimental/reviewer.md"))
        self.assertEqual(result["files"]["derived"], [".codex/agents/reviewer.md"])
        self.assertEqual(result["files"]["authored"], [".codex/agents-experimental/reviewer.md"])

    def test_a_dynamic_members_regenerated_output_classifies_derived(self):
        # owner_of reads only the STATIC outputs, so without the dynamic half a regenerated setup route
        # would be described to the operator as hand-written work.
        route = ".claude/skills/engine-setup-routes/some-module/SKILL.md"
        real = derived_state._concrete_outputs

        def fake(member, root):
            if member.dynamic:
                return (derived_state.Output("file", route),)
            return real(member, root)

        with mock.patch.object(derived_state, "_concrete_outputs", fake):
            result = _classify(_row(6, 2, route))
        self.assertEqual(result["files"]["derived"], [route])
        self.assertEqual(result["churn"]["derived"], 8)

    def test_governing_prose_is_authored_and_only_real_documentation_is_docs(self):
        prose = [".engine/operations/build-orchestration.md",
                 ".engine/contracts/eADR-0041-build-coordinator-behavior.md",
                 ".engine/conduct/defaults.md",
                 "CLAUDE.md",
                 ".claude/agents/engine-qa-review-usability.md"]
        docs = ["docs/getting-started.md", ".engine/docs/upgrading.md"]
        result = _classify("".join(_row(1, 1, p) for p in prose + docs))
        self.assertEqual(result["files"]["authored"], sorted(prose))
        self.assertEqual(result["files"]["docs"], sorted(docs))

    def test_a_derived_file_that_lives_under_docs_is_still_derived(self):
        result = _classify(_row(9, 0, ".engine/docs/ci-assurance.md"))
        self.assertEqual(result["files"]["derived"], [".engine/docs/ci-assurance.md"])
        self.assertEqual(result["files"]["docs"], [])

    def test_an_unmatched_path_is_authored_never_a_silent_fifth_bucket(self):
        result = _classify(_row(10, 3, "app/main.py"))
        self.assertEqual(result["files"]["authored"], ["app/main.py"])
        self.assertEqual(set(result["files"]), set(repair_divergence.KINDS))
        self.assertEqual(result["total_churn"], 13)

    def test_a_binary_file_contributes_no_invented_line_count(self):
        result = _classify(_row("-", "-", "assets/logo.png") + _row(2, 2, "app/main.py"))
        self.assertEqual(result["files"]["authored"], ["app/main.py", "assets/logo.png"])
        self.assertEqual(result["churn"]["authored"], 4)

    def test_a_rename_is_attributed_to_the_new_path(self):
        result = _classify(_rename_row(5, 5, "app/old.py", "app/new.py"))
        self.assertEqual(result["files"]["authored"], ["app/new.py"])
        self.assertEqual(result["churn"]["authored"], 10)

    def test_a_rename_into_a_guarded_path_is_reported_as_guarded(self):
        result = _classify(_rename_row(0, 0, "scratch/ci.yml", ".github/workflows/engine-ci.yml"))
        self.assertEqual(result["files"]["guarded"], [".github/workflows/engine-ci.yml"])

    def test_an_empty_increment_reports_four_empty_kinds(self):
        result = _classify("")
        self.assertEqual(result["files"], {kind: [] for kind in repair_divergence.KINDS})
        self.assertEqual(result["total_churn"], 0)
        self.assertEqual((result["anchor"], result["head"]), ("aaa", "bbb"))


class TestGuardSetDerivation(unittest.TestCase):
    def test_the_guard_sets_are_derived_once_and_threaded_through(self):
        # One disk scan per round, not one per file — the flagged_changes pattern.
        scripts = mock.Mock(return_value=NO_SCRIPTS)
        instance = mock.Mock(return_value=NO_GUARDS)
        payload = "".join(_row(1, 1, f"app/f{n}.py") for n in range(6))
        with mock.patch.object(weakening_guard, "_derive_check_scripts", scripts), \
             mock.patch.object(weakening_guard, "_read_instance_guards", instance):
            result = repair_divergence.classify("/nowhere", "aaa", "bbb", runner=_runner(payload))
        self.assertEqual(len(result["files"]["authored"]), 6)
        self.assertEqual(scripts.call_count, 1)
        self.assertEqual(instance.call_count, 1)

    def test_the_guard_sets_are_derived_from_the_given_root(self):
        scripts = mock.Mock(return_value=NO_SCRIPTS)
        instance = mock.Mock(return_value=NO_GUARDS)
        with mock.patch.object(weakening_guard, "_derive_check_scripts", scripts), \
             mock.patch.object(weakening_guard, "_read_instance_guards", instance):
            repair_divergence.classify("/some/tree", "aaa", "bbb", runner=_runner(""))
        self.assertEqual(scripts.call_args.args[0], os.path.join("/some/tree", ".engine", "check"))
        self.assertEqual(instance.call_args.args[0],
                         os.path.join("/some/tree", weakening_guard.INSTANCE_DECL_REL))

    def test_an_unreadable_check_dir_guards_the_whole_tools_directory(self):
        # weakening_guard's fail-safe sentinel: derivation failed, so guard everything it might have named.
        result = _classify(_row(1, 1, ".engine/tools/build_coordinator.py"), derived_scripts=None)
        self.assertEqual(result["files"]["guarded"], [".engine/tools/build_coordinator.py"])


class TestRefusal(unittest.TestCase):
    def test_a_git_failure_refuses_instead_of_reporting_an_empty_diff(self):
        with tempfile.TemporaryDirectory() as root:
            _git(root, "init", "-q")
            with self.assertRaises(repair_divergence.DivergenceError) as caught:
                repair_divergence.classify(root, "deadbee", "f00ba12")
        self.assertIn("failed", str(caught.exception))

    def test_an_unparseable_record_refuses(self):
        with self.assertRaises(repair_divergence.DivergenceError):
            _classify("this is not a numstat record\0")

    def test_a_truncated_rename_record_refuses(self):
        with self.assertRaises(repair_divergence.DivergenceError):
            _classify("1\t1\t\0app/old.py\0")

    def test_a_failing_derivation_refuses_rather_than_measuring_against_a_guess(self):
        with mock.patch.object(weakening_guard, "_derive_check_scripts",
                               side_effect=RuntimeError("disk gone")):
            with self.assertRaises(repair_divergence.DivergenceError) as caught:
                repair_divergence.classify("/nowhere", "aaa", "bbb", runner=_runner(""))
        self.assertIn("classification sets", str(caught.exception))


class TestExplicitRoot(unittest.TestCase):
    def test_the_tree_measured_is_the_root_the_caller_passed(self):
        # The test process runs inside the engine's own checkout; a root-blind implementation would
        # measure THAT tree and pass this test only by accident.
        with tempfile.TemporaryDirectory() as root:
            _git(root, "init", "-q")
            _git(root, "config", "user.email", "e@x")
            _git(root, "config", "user.name", "n")
            with open(os.path.join(root, "app.py"), "w", encoding="utf-8") as fh:
                fh.write("one\n")
            _git(root, "add", "-A")
            _git(root, "commit", "-q", "-m", "first")
            base = _git(root, "rev-parse", "HEAD").stdout.strip()
            with open(os.path.join(root, "app.py"), "a", encoding="utf-8") as fh:
                fh.write("two\nthree\n")
            with open(os.path.join(root, "notes.md"), "w", encoding="utf-8") as fh:
                fh.write("note\n")
            _git(root, "add", "-A")
            _git(root, "commit", "-q", "-m", "second")
            head = _git(root, "rev-parse", "HEAD").stdout.strip()
            result = repair_divergence.classify(root, base, head,
                                                derived_scripts=NO_SCRIPTS, instance_guards=NO_GUARDS)
        self.assertEqual(result["files"]["authored"], ["app.py", "notes.md"])
        self.assertEqual(result["churn"]["authored"], 3)
        self.assertEqual((result["anchor"], result["head"]), (base, head))

    def test_the_increment_is_two_dot_so_a_sibling_branch_is_not_re_counted(self):
        # Three-dot would diff against the merge base and re-count work this round never touched.
        with tempfile.TemporaryDirectory() as root:
            _git(root, "init", "-q")
            _git(root, "config", "user.email", "e@x")
            _git(root, "config", "user.name", "n")
            for name, body in (("app.py", "one\n"),):
                with open(os.path.join(root, name), "w", encoding="utf-8") as fh:
                    fh.write(body)
            _git(root, "add", "-A")
            _git(root, "commit", "-q", "-m", "first")
            root_commit = _git(root, "rev-parse", "HEAD").stdout.strip()
            with open(os.path.join(root, "app.py"), "a", encoding="utf-8") as fh:
                fh.write("round one\n")
            _git(root, "add", "-A")
            _git(root, "commit", "-q", "-m", "round one")
            anchor = _git(root, "rev-parse", "HEAD").stdout.strip()
            with open(os.path.join(root, "later.py"), "w", encoding="utf-8") as fh:
                fh.write("round two\n")
            _git(root, "add", "-A")
            _git(root, "commit", "-q", "-m", "round two")
            head = _git(root, "rev-parse", "HEAD").stdout.strip()
            result = repair_divergence.classify(root, anchor, head,
                                                derived_scripts=NO_SCRIPTS, instance_guards=NO_GUARDS)
        self.assertEqual(result["files"]["authored"], ["later.py"])
        self.assertNotEqual(anchor, root_commit)


if __name__ == "__main__":
    unittest.main()
