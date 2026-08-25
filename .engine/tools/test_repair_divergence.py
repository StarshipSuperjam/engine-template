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

    def test_a_rename_AWAY_from_a_guarded_path_is_still_reported_as_guarded(self):
        # Classifying only the destination would report a guarded file renamed to an ordinary name as
        # ordinary authored work -- renaming a guard away is at least as serious as regenerating one.
        result = _classify(_rename_row(0, 0, ".github/workflows/engine-ci.yml", "scratch/ci.yml"))
        self.assertEqual(result["files"]["guarded"], ["scratch/ci.yml"])
        self.assertEqual(result["files"]["authored"], [])

    def test_a_rename_away_from_a_derived_path_is_still_reported_as_derived(self):
        result = _classify(_rename_row(0, 0, ".engine/knowledge/graph.json", "notes/graph.json"))
        self.assertEqual(result["files"]["derived"], ["notes/graph.json"])

    def test_a_rename_away_from_a_DYNAMIC_output_is_still_reported_as_derived(self):
        # The dynamic set is resolved from the HEAD tree, so a renamed-away route is no longer in it and
        # owner_of does not cover dynamic members either -- the old name has to be checked against both.
        route = ".claude/skills/engine-setup-routes/some-module/SKILL.md"
        real = derived_state._concrete_outputs

        def fake(member, root):
            return (derived_state.Output("file", route),) if member.dynamic else real(member, root)

        with mock.patch.object(derived_state, "_concrete_outputs", fake):
            result = _classify(_rename_row(0, 0, route, "notes/old-route.md"))
        self.assertEqual(result["files"]["derived"], ["notes/old-route.md"])
        self.assertEqual(result["files"]["authored"], [])

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


class TestGuardSetsAcrossTheIncrement(unittest.TestCase):
    """The two halves of a classification come from different points in time: the file list is the
    anchor..head diff, but the guard sets would otherwise be read only at head. A round that de-registers a
    guard must not be able to make the NEXT round's churn on that file look like ordinary authored work."""

    def repo(self, stack, anchor_rule, head_rule):
        root = stack.enter_context(tempfile.TemporaryDirectory())
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "e@x")
        _git(root, "config", "user.name", "n")
        os.makedirs(os.path.join(root, ".engine/check"))
        os.makedirs(os.path.join(root, ".engine/tools"))
        for rel, body in ((".engine/check/rule.json", anchor_rule),
                          (".engine/tools/enforcer.py", "one\n")):
            with open(os.path.join(root, rel), "w", encoding="utf-8") as fh:
                fh.write(body)
        _git(root, "add", "-A"); _git(root, "commit", "-q", "-m", "first")
        anchor = _git(root, "rev-parse", "HEAD").stdout.strip()
        with open(os.path.join(root, ".engine/check/rule.json"), "w", encoding="utf-8") as fh:
            fh.write(head_rule)
        with open(os.path.join(root, ".engine/tools/enforcer.py"), "w", encoding="utf-8") as fh:
            fh.write("one\ntwo\nthree\n")
        _git(root, "add", "-A"); _git(root, "commit", "-q", "-m", "second")
        return root, anchor, _git(root, "rev-parse", "HEAD").stdout.strip()

    def test_a_round_that_de_registers_a_guard_still_reports_the_guarded_file(self):
        import contextlib
        registered = '{"params": {"script": ".engine/tools/enforcer.py"}}'
        with contextlib.ExitStack() as stack:
            root, anchor, head = self.repo(stack, registered, '{"params": {}}')
            result = repair_divergence.classify(root, anchor, head)
        self.assertIn(".engine/tools/enforcer.py", result["files"]["guarded"])
        self.assertEqual(result["files"]["authored"], [])

    def test_a_guard_added_by_this_very_round_is_reported_too(self):
        import contextlib
        registered = '{"params": {"script": ".engine/tools/enforcer.py"}}'
        with contextlib.ExitStack() as stack:
            root, anchor, head = self.repo(stack, '{"params": {}}', registered)
            result = repair_divergence.classify(root, anchor, head)
        self.assertIn(".engine/tools/enforcer.py", result["files"]["guarded"])


class TestTwoDotIsNotThreeDot(unittest.TestCase):
    def test_the_increment_is_the_two_dot_span_between_the_two_commits(self):
        # The guarantee only has teeth where the two forms DIFFER, which needs an anchor that is not an
        # ancestor of head -- a repair anchor genuinely can leave the branch (the `rewritten` case). On a
        # linear history, and on a merge measured from its own first parent, two-dot and three-dot are
        # identical by definition and the test cannot fail. This fixture was checked by mutation: swapping
        # `base..head` for `base...head` in numstat_rows reddens it.
        with tempfile.TemporaryDirectory() as root:
            _git(root, "init", "-q", "-b", "main")
            _git(root, "config", "user.email", "e@x")
            _git(root, "config", "user.name", "n")
            with open(os.path.join(root, "shared.py"), "w", encoding="utf-8") as fh:
                fh.write("shared\n")
            _git(root, "add", "-A"); _git(root, "commit", "-q", "-m", "root")
            _git(root, "checkout", "-q", "-b", "abandoned")
            with open(os.path.join(root, "abandoned.py"), "w", encoding="utf-8") as fh:
                fh.write("abandoned work\n")
            _git(root, "add", "-A"); _git(root, "commit", "-q", "-m", "the anchor, later left behind")
            anchor = _git(root, "rev-parse", "HEAD").stdout.strip()
            _git(root, "checkout", "-q", "main")
            with open(os.path.join(root, "fix.py"), "w", encoding="utf-8") as fh:
                fh.write("the fix\n")
            _git(root, "add", "-A"); _git(root, "commit", "-q", "-m", "the repair")
            head = _git(root, "rev-parse", "HEAD").stdout.strip()
            result = repair_divergence.classify(root, anchor, head,
                                                derived_scripts=NO_SCRIPTS, instance_guards=NO_GUARDS)
        # Two-dot compares the two TREES: fix.py appears, and abandoned.py appears as a deletion. Three-dot
        # would diff from the merge base (the root commit) and would NOT mention abandoned.py at all.
        self.assertEqual(result["files"]["authored"], ["abandoned.py", "fix.py"])


class TestGuardFloor(unittest.TestCase):
    def test_a_guard_de_registered_in_an_earlier_round_still_reports_guarded(self):
        # The anchor-side union reaches back exactly one round. The reference floor -- the commit the
        # deliverable review stood on -- carries the property across the whole repair loop.
        with tempfile.TemporaryDirectory() as root:
            _git(root, "init", "-q")
            _git(root, "config", "user.email", "e@x")
            _git(root, "config", "user.name", "n")
            os.makedirs(os.path.join(root, ".engine/check"))
            os.makedirs(os.path.join(root, ".engine/tools"))
            def write(rel, body):
                with open(os.path.join(root, rel), "w", encoding="utf-8") as fh:
                    fh.write(body)
            write(".engine/check/rule.json", '{"params": {"script": ".engine/tools/enforcer.py"}}')
            write(".engine/tools/enforcer.py", "one\n")
            _git(root, "add", "-A"); _git(root, "commit", "-q", "-m", "reviewed")
            reviewed = _git(root, "rev-parse", "HEAD").stdout.strip()
            write(".engine/check/rule.json", '{"params": {}}')          # round 1 de-registers the guard
            _git(root, "add", "-A"); _git(root, "commit", "-q", "-m", "round one")
            round_one = _git(root, "rev-parse", "HEAD").stdout.strip()
            write(".engine/tools/enforcer.py", "one\ntwo\nthree\n")   # round 2 rewrites the enforcer
            _git(root, "add", "-A"); _git(root, "commit", "-q", "-m", "round two")
            head = _git(root, "rev-parse", "HEAD").stdout.strip()
            without = repair_divergence.classify(root, round_one, head)
            with_floor = repair_divergence.classify(root, round_one, head, guard_reference=reviewed)
        # Without the floor the guard is gone at both ends of round 2's span, so the enforcer reads as
        # ordinary authored work -- the operator is told no guarded surface moved.
        self.assertEqual(without["files"]["authored"], [".engine/tools/enforcer.py"])
        self.assertEqual(with_floor["files"]["guarded"], [".engine/tools/enforcer.py"])
        self.assertEqual(with_floor["files"]["authored"], [])


class TestDegradedGuardRead(unittest.TestCase):
    def test_an_unreadable_instance_declaration_is_disclosed_not_silently_empty(self):
        payload = _row(1, 1, "app/main.py")
        real = repair_divergence._guard_sets_at
        with mock.patch.object(repair_divergence, "_guard_sets_at",
                               return_value=(NO_SCRIPTS, (set(), ()), False)):
            result = repair_divergence.classify("/nowhere", "aaa", "bbb", runner=_runner(payload))
        self.assertFalse(result["guards_read"])
        self.assertIs(repair_divergence._guard_sets_at, real)

    def test_a_clean_read_reports_complete(self):
        with mock.patch.object(repair_divergence, "_guard_sets_at",
                               return_value=(NO_SCRIPTS, (set(), ()), True)):
            result = repair_divergence.classify("/nowhere", "aaa", "bbb", runner=_runner(""))
        self.assertTrue(result["guards_read"])


class TestSchemaAgreement(unittest.TestCase):
    def test_every_key_classify_returns_is_declared_in_the_build_state_schema(self):
        """The two halves drift silently and expensively: a new key here is written straight into the
        Build snapshot, whose schema forbids unknown properties, so the FIRST real `repair assess` after
        such a drift refuses -- while a unit suite whose fixtures are hand-built stays green. Bind the
        shapes together instead of trusting them to stay in step."""
        import json
        here = os.path.dirname(os.path.abspath(__file__))

        def shape(name):
            """The classification shape each schema declares — by $ref where the file has $defs, and
            inline in the handoff schemas, which have none. All four are bound: a Build restored from a
            legacy v1 snapshot hits the same unknown-property refusal, and only checking v2 left three
            files free to drift."""
            doc = json.load(open(os.path.join(here, "..", "schemas", f"{name}.json"), encoding="utf-8"))
            if "$defs" in doc and "divergence_classification" in doc["$defs"]:
                return doc["$defs"]["divergence_classification"]
            entry = doc["properties"]["repair_rounds"]["items"]["properties"]["classification"]
            return entry

        schemas = {name: shape(name) for name in ("build-state.v1", "build-state.v2",
                                                  "build-handoff.v1", "build-handoff.v2")}
        declared = set.intersection(*(set(sc["properties"]) for sc in schemas.values()))
        with tempfile.TemporaryDirectory() as root:
            _git(root, "init", "-q")
            _git(root, "config", "user.email", "e@x")
            _git(root, "config", "user.name", "n")
            with open(os.path.join(root, "app.py"), "w", encoding="utf-8") as fh:
                fh.write("one\n")
            _git(root, "add", "-A"); _git(root, "commit", "-q", "-m", "first")
            base = _git(root, "rev-parse", "HEAD").stdout.strip()
            with open(os.path.join(root, "app.py"), "a", encoding="utf-8") as fh:
                fh.write("two\n")
            _git(root, "add", "-A"); _git(root, "commit", "-q", "-m", "second")
            head = _git(root, "rev-parse", "HEAD").stdout.strip()
            produced = repair_divergence.classify(root, base, head,
                                                  derived_scripts=NO_SCRIPTS, instance_guards=NO_GUARDS)
        for name, sc in schemas.items():
            self.assertEqual(set(produced) - set(sc["properties"]), set(),
                             f"classify() returns a key {name} would reject")
            self.assertEqual(set(sc["required"]) - set(produced), set(), name)
        self.assertEqual(set(produced) - declared, set())


if __name__ == "__main__":
    unittest.main()
