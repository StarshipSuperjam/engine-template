"""Regression fixture for the affected-test selector (selftest_select.py).

Run: uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py' -b

The load-bearing cases are the ones that would let the selector run TOO FEW tests, because that is the
only failure here that can make a run look green when it is not. Two of them carry positive controls,
because a test that proves a conservative selector works can pass just as well when the selector does
nothing but fall back to running everything:

  * `test_a_test_reached_only_through_a_chain_of_imports_is_selected` asserts the classification is
    focused with no fallback reason, requires a control module to be ABSENT from the selection, and is
    shown by `test_the_transitive_case_fails_when_the_graph_is_narrowed_to_direct_importers` to go red
    when the traversal is narrowed. Without something that must be excluded, "expanded correctly" and
    "ran everything" are indistinguishable.
  * `TestSurfaceCatalogueTotality` walks the engine's own surface catalogue and fails if any registered
    surface kind other than `tool` ever becomes positively classifiable. That is what stops the partition
    silently ceasing to be total as the engine grows a new kind of governed file.

The classification half is exercised against plain directory trees with a hand-built import index, never
a constructed git repository — that separation is why these cases are fast and why the git boundary has
its own small set of cases instead of contaminating every other one.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

import selftest_select as S
import validate

_TOOLS = S.TOOLS_ROOT_REL


def _p(name: str) -> str:
    """A repo-relative tools path for a bare module name (`boot` -> `.engine/tools/boot.py`)."""
    return f"{_TOOLS}/{name}.py"


def _no_guard(_importers):
    """No derived-artifact guard, for the cases that exercise classification over a synthetic tree.

    The real guard is derived from the engine's own register of generated artifacts, which describes THIS
    repository — applying it to a fabricated three-module tree would (correctly) fail closed and tell us
    nothing about the logic under test. `DerivedArtifactGuard` below exercises the real thing."""
    return set(), None


def _index(edges: dict) -> dict:
    """A reverse import index from `{importer: [imported, ...]}`, using bare module names."""
    importers: dict = {}
    for importer, targets in edges.items():
        for target in targets:
            importers.setdefault(_p(target), set()).add(_p(importer))
    return importers


class Classification(unittest.TestCase):
    """The pure half: given changed paths and a graph, what runs."""

    def _classify(self, changed, edges=None):
        return S.classify(changed, lambda: _index(edges or {}), guard_factory=_no_guard,
                          changed_from="base")

    def test_a_changed_test_module_selects_itself(self):
        m = self._classify([(_p("test_alpha"), "M")])
        self.assertEqual(m["classification"], "focused")
        self.assertEqual([e["module"] for e in m["selected"]], ["test_alpha"])
        self.assertEqual(m["selected"][0]["reason"]["code"], "changed-test-module")

    def test_a_test_that_imports_the_changed_tool_directly_is_selected(self):
        m = self._classify([(_p("widget"), "M")], {"test_widget": ["widget"]})
        self.assertEqual(m["classification"], "focused")
        self.assertEqual([e["module"] for e in m["selected"]], ["test_widget"])
        self.assertEqual(m["selected"][0]["reason"]["code"], "direct-import")

    def test_a_test_reached_only_through_a_chain_of_imports_is_selected(self):
        """The headline correctness case, with the control that stops it being vacuous.

        `test_far` imports nothing changed; it reaches `deep` only through `middle`. `test_unrelated`
        must be ABSENT — without a module required to be excluded, a selector that simply fell back to
        the full inventory would satisfy this case exactly as well as a correct one."""
        m = self._classify(
            [(_p("deep"), "M")],
            {"middle": ["deep"], "test_far": ["middle"], "test_unrelated": ["elsewhere"]})
        self.assertEqual(m["classification"], "focused")
        self.assertIsNone(m["full_reason"], "a full fallback would satisfy the rest of this case")
        picked = {e["module"]: e["reason"]["code"] for e in m["selected"]}
        self.assertEqual(picked, {"test_far": "transitive-import"})
        self.assertNotIn("test_unrelated", picked)

    def test_the_transitive_case_fails_when_the_graph_is_narrowed_to_direct_importers(self):
        """The positive control: prove the case above is not vacuous.

        Narrow the traversal to direct importers only — the exact defect the case exists to catch — and
        the same input must stop selecting the transitively-reached test."""
        importers = _index({"middle": ["deep"], "test_far": ["middle"]})
        direct_only = {
            path: {imp for imp in imps if S.is_test_module(imp)}
            for path, imps in importers.items()
        }
        narrowed = S.classify([(_p("deep"), "M")], lambda: direct_only, guard_factory=_no_guard,
                              changed_from="base")
        self.assertNotEqual(
            narrowed["classification"], "focused",
            "a direct-importers-only graph must NOT reproduce the transitive selection")
        self.assertEqual(narrowed["full_reason"]["code"], "unreached-tool")

    def test_a_realistic_mid_loop_change_narrows_rather_than_running_everything(self):
        """The obligation that the feature must actually be useful, not merely safe.

        A session iterating on one tool and its test is the shape this exists for. Asserted as a
        classification and a proper subset — no arithmetic over real runs, no projected saving."""
        edges = {f"test_other{i}": [f"other{i}"] for i in range(12)}
        edges["test_widget"] = ["widget"]
        m = self._classify([(_p("widget"), "M"), (_p("test_widget"), "M")], edges)
        self.assertEqual(m["classification"], "focused")
        self.assertEqual([e["module"] for e in m["selected"]], ["test_widget"])
        self.assertLess(len(m["selected"]), len(edges),
                        "a focused run must select a proper subset of the tests that exist")

    def test_every_selected_entry_carries_its_own_reason_from_the_closed_vocabulary(self):
        m = self._classify([(_p("widget"), "M")], {"test_widget": ["widget"], "mid": ["widget"],
                                                   "test_via_mid": ["mid"]})
        self.assertEqual(m["classification"], "focused")
        self.assertTrue(m["selected"])
        for entry in m["selected"]:
            self.assertIn(entry["reason"]["code"], S.SELECTION_REASONS)
            self.assertTrue(entry["reason"]["detail"])

    def test_a_packaged_test_module_gets_its_dotted_discovery_name(self):
        """Discovery names a packaged module `memory.test_ledger`; the manifest must match, or the
        runner would need a second naming convention and could silently match nothing."""
        self.assertEqual(S.module_name(f"{_TOOLS}/memory/test_ledger.py"), "memory.test_ledger")
        self.assertEqual(S.module_name(_p("test_flat")), "test_flat")


class FullFallbacks(unittest.TestCase):
    """Every way of not knowing runs everything, and says which way it was."""

    def _full(self, changed, edges=None, git_failure=None):
        m = S.classify(changed, lambda: _index(edges or {}), guard_factory=_no_guard,
                       changed_from="base", git_failure=git_failure)
        self.assertEqual(m["classification"], "full")
        self.assertEqual(m["selected"], [])
        self.assertIn(m["full_reason"]["code"], S.FULL_REASONS)
        self.assertTrue(m["full_reason"]["detail"])
        return m["full_reason"]["code"]

    def test_a_failing_git_command_runs_everything(self):
        self.assertEqual(self._full([], git_failure="git exploded"), "git-unavailable")

    def test_nothing_changed_runs_everything(self):
        self.assertEqual(self._full([]), "no-changed-paths")

    def test_a_deleted_file_runs_everything(self):
        self.assertEqual(self._full([(_p("gone"), "D")]), "deleted-or-renamed")

    def test_a_renamed_file_runs_everything(self):
        self.assertEqual(self._full([(_p("moved"), "R")]), "deleted-or-renamed")

    def test_a_changed_tool_no_test_reaches_runs_everything(self):
        self.assertEqual(self._full([(_p("orphan"), "M")], {"test_x": ["y"]}), "unreached-tool")

    def test_an_unparseable_tool_runs_everything(self):
        def boom():
            raise S.SelectionError(("unparseable-python", "tools/broken.py will not parse"))
        m = S.classify([(_p("broken"), "M")], boom, guard_factory=_no_guard, changed_from="base")
        self.assertEqual(m["full_reason"]["code"], "unparseable-python")

    def test_a_dangling_import_runs_everything(self):
        def boom():
            raise S.SelectionError(("dangling-import", "tools/a.py imports a module that is gone"))
        m = S.classify([(_p("a"), "M")], boom, guard_factory=_no_guard, changed_from="base")
        self.assertEqual(m["full_reason"]["code"], "dangling-import")

    def test_documentation_and_governed_data_run_everything(self):
        """The category that used to be a shortcut. A documentation-only change now runs the whole
        inventory: the earlier design skipped everything for it, which would have skipped the very test
        that pins the repository's own top-level instruction file."""
        for path in ("CLAUDE.md", "README.md", ".engine/docs/getting-started.md",
                     ".engine/check/link-integrity.json", ".engine/operations/build-orchestration.md",
                     ".engine/conduct/defaults.md", ".engine/schemas/build-plan.v2.json",
                     ".github/workflows/engine-ci.yml", ".engine/uv.lock", "pyproject.toml",
                     ".claude/skills/engine-setup/SKILL.md", ".engine/suites.json"):
            with self.subTest(path=path):
                self.assertEqual(self._full([(path, "M")]), "path-not-classifiable")

    def test_one_unclassifiable_path_forces_everything_even_beside_tool_code(self):
        """The partition is total, not per-path: a batch containing anything unrecognised runs
        everything, rather than narrowing on the part that happened to be recognisable."""
        self.assertEqual(
            self._full([(_p("widget"), "M"), (".engine/docs/getting-started.md", "M")],
                       {"test_widget": ["widget"]}),
            "path-not-classifiable")


class SurfaceCatalogueTotality(unittest.TestCase):
    """The rule that keeps the partition total as the engine grows.

    A hand-maintained list of significant paths cannot fail when it goes stale — that was the defect in
    the design this replaced. This can: it reads the engine's own register of governed surface kinds and
    asserts that `tool` is the only one positively classified. Adding a fourteenth surface kind, or
    quietly teaching the selector to narrow on prose, breaks this test rather than silently
    under-selecting."""

    def _catalogue(self) -> dict:
        with open(os.path.join(validate.ENGINE_DIR, "schemas", "surface-catalog.json"),
                  encoding="utf-8") as fh:
            return json.load(fh)["surfaces"]

    @staticmethod
    def _sample(kind: str, entry: dict):
        """A representative path for one registered surface kind, from its declared home and CLASS.

        The extension comes from the catalogue's own `class` field, never from the kind's name. Hardcoding
        `.py` for the literal string "tool" and `.md` for everything else made this test unable to do the
        one job it exists for: a reviewer added a hypothetical fourteenth kind of class `code` homed under
        the tools directory — precisely the shape that WOULD become positively classifiable — and the test
        still passed, because it could never construct a `.py` sample for a kind not named "tool"."""
        if not isinstance(entry, dict):
            return None
        home = entry.get("location")
        if not isinstance(home, str) or not home:
            return None
        suffix = ".py" if entry.get("class") == "code" else ".md"
        return f"{home.rstrip('/')}/representative-sample{suffix}"

    def test_tool_is_the_only_positively_classified_surface_kind(self):
        catalogue = self._catalogue()
        self.assertGreaterEqual(len(catalogue), 10, "the catalogue looks empty; the read is wrong")
        positively = set()
        for kind, entry in catalogue.items():
            sample = self._sample(kind, entry)
            if sample and S.is_tool_python(sample):
                positively.add(kind)
        self.assertEqual(
            positively, {"tool"},  # noqa: the message below is the point of this assertion
            "exactly one governed surface kind may be positively classified; anything else must fall "
            "to the full inventory. If this failed because the engine grew a surface kind, decide "
            "deliberately whether the selector should narrow on it.")

    def test_every_catalogued_surface_kind_reaches_a_named_rule(self):
        """Totality itself: no registered surface kind lands outside both rules."""
        for kind, entry in self._catalogue().items():
            sample = self._sample(kind, entry)
            self.assertIsNotNone(sample, f"{kind} declares no home; totality cannot be judged")
            with self.subTest(kind=kind):
                if S.is_tool_python(sample):
                    continue
                m = S.classify([(sample, "M")], lambda: {}, guard_factory=_no_guard,
                               changed_from="base")
                self.assertEqual(m["classification"], "full")
                self.assertEqual(m["full_reason"]["code"], "path-not-classifiable")


class DerivedArtifactGuard(unittest.TestCase):
    """The guard that closes the last silent-miss path, exercised against the real register.

    Editing any tracked file restates that file's recorded fingerprint and stales the engine's generated
    maps. The tests that police that staleness import nothing the edited file touches, so no import graph
    can ever reach them — which is why they are added structurally instead."""

    def test_a_focused_run_always_includes_the_generated_map_drift_tests(self):
        importers = S.build_importer_index(validate.ROOT)
        guard, unreachable = S.derived_artifact_guard(importers)
        self.assertIsNone(unreachable)
        modules = {S.module_name(p) for p in guard}
        for expected in ("test_knowledge", "test_ci_assurance", "test_self_map",
                         "test_module_surfaces", "test_module_catalog"):
            self.assertIn(expected, modules)

    def test_editing_one_leaf_tool_still_selects_the_drift_tests(self):
        """The concrete case a reviewer proved: append a comment to a tool, and without the guard the
        selection excludes the very test that would have caught the resulting stale map."""
        importers = S.build_importer_index(validate.ROOT)
        m = S.classify([(f"{_TOOLS}/quiet_call.py", "M")], lambda: importers, changed_from="base")
        self.assertEqual(m["classification"], "focused")
        self.assertIn("test_knowledge", {e["module"] for e in m["selected"]})

    def test_the_guard_is_far_smaller_than_the_transitive_closure_of_the_generators(self):
        """Direct test importers only. The transitive closure of those generators is most of the tree,
        which would erase the feature; the direct set leaves a leaf edit selecting a small fraction."""
        importers = S.build_importer_index(validate.ROOT)
        guard, _ = S.derived_artifact_guard(importers)
        every_test = {p for p in importers if S.is_test_module(p)}
        every_test |= {imp for imps in importers.values() for imp in imps if S.is_test_module(imp)}
        self.assertLess(len(guard), len(every_test) / 3,
                        "the guard must stay a small fraction of the inventory or it defeats the feature")

    def test_a_generator_no_test_imports_forces_the_full_inventory(self):
        """Fail closed: an incomplete guard set is exactly the silent miss the guard exists to prevent."""
        m = S.classify([(_p("widget"), "M")], lambda: _index({"test_widget": ["widget"]}),
                       guard_factory=lambda _i: (set(), f"{_TOOLS}/some_generator.py"),
                       changed_from="base")
        self.assertEqual(m["classification"], "full")
        self.assertEqual(m["full_reason"]["code"], "derived-guard-unreachable")


class ManifestHonesty(unittest.TestCase):
    """The manifest must be true, not merely present — every one of these was a live wrong statement."""

    def test_each_changed_file_is_described_by_its_own_relationship(self):
        """A test can import one changed file directly and reach another only through a chain. Lumping
        them under the single strongest code made the manifest assert a direct import that was not one
        — a false claim about the graph, in the artifact whose whole job is explaining the graph."""
        m = S.classify([(_p("near"), "M"), (_p("far"), "M")],
                       lambda: _index({"mid": ["far"], "test_x": ["mid", "near"]}),
                       guard_factory=_no_guard, changed_from="base")
        detail = m["selected"][0]["reason"]["detail"]
        self.assertIn(f"imports {_p('near')} directly", detail)
        self.assertIn(f"reaches {_p('far')} through a chain", detail)
        self.assertNotIn(f"{_p('far')} directly", detail)

    def test_a_path_reported_by_two_sources_is_counted_once(self):
        """The three changed-set sources overlap by design, so a file edited but not yet committed
        arrives twice. Counting the raw list named a file twice and overstated how many had changed —
        in the commonest situation there is, uncommitted mid-build work."""
        dupes = [("README.md", "M"), ("README.md", "M"), ("docs/a.md", "M"), ("docs/a.md", "M")]
        m = S.classify(dupes, lambda: {}, guard_factory=_no_guard, changed_from="base")
        detail = m["full_reason"]["detail"]
        self.assertEqual(detail.count("README.md"), 1)
        self.assertNotIn("more)", detail, "two distinct paths need no overflow count")

    def test_every_single_reason_code_names_the_whole_batch(self):
        """Naming only the first offender sends a reader round the loop once per file."""
        gone = S.classify([(_p("a"), "D"), (_p("b"), "D")], lambda: {},
                          guard_factory=_no_guard, changed_from="base")
        self.assertIn(_p("b"), gone["full_reason"]["detail"])
        orphans = S.classify([(_p("x"), "M"), (_p("y"), "M")], lambda: _index({"test_z": ["z"]}),
                             guard_factory=_no_guard, changed_from="base")
        self.assertIn(_p("y"), orphans["full_reason"]["detail"])

    def test_a_changed_test_module_says_it_changed_even_when_it_guards_a_map(self):
        """The guard seeds the selection first, and a setdefault could not displace it — so a test
        module that BOTH changed and guards a generated map reported only that it guards one."""
        target = _p("test_knowledge")
        m = S.classify([(target, "M")], lambda: {},
                       guard_factory=lambda _i: ({target}, None), changed_from="base")
        entry = next(e for e in m["selected"] if e["path"] == target)
        self.assertEqual(entry["reason"]["code"], "changed-test-module")

    def test_a_real_dangling_import_keeps_its_whole_explanation(self):
        """Drives the REAL code path, which the first version of this test did not.

        The detail was taken as everything before the first period — which, in a message opening with a
        repo-relative path, is the period inside `.engine`, so the file, the bad import and the whole
        explanation were lost in the failure a session would most need explained. The first version of
        this test rebuilt the fixed transformation inline in its own body and never called
        `build_importer_index`, where the truncation lived, so it passed against the buggy commit."""
        tmp = tempfile.mkdtemp(prefix="selftest-dangling-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        tools = os.path.join(tmp, _TOOLS)
        pkg = os.path.join(tools, "anchorpkg")
        os.makedirs(pkg)
        # A PACKAGE, because that is the shape the resolver refuses: `from <in-repo package> import
        # <name>` where the name is neither a submodule nor something the package declares.
        with open(os.path.join(pkg, "__init__.py"), "w") as fh:
            fh.write("")
        with open(os.path.join(pkg, "present.py"), "w") as fh:
            fh.write("VALUE = 1\n")
        with open(os.path.join(tools, "broken.py"), "w") as fh:
            fh.write("from anchorpkg import definitely_not_a_real_name\n")
        with self.assertRaises(S.SelectionError) as caught:
            S.build_importer_index(tmp)
        code, detail = caught.exception.args[0]
        self.assertEqual(code, "dangling-import")
        self.assertIn("broken.py", detail)
        self.assertIn("definitely_not_a_real_name", detail)

    def test_every_exempt_output_is_policed_by_a_hard_check(self):
        """The exemption's real bound, held mechanically rather than asserted in a comment.

        A path exempt from classification is one the selector deliberately stops reasoning about, so
        something else must police it. That something is each derived member's own drift check, hard and
        in the validator suite — the other registered validation command, which is never narrowed. A
        reviewer showed the first rationale named the wrong mechanism, claiming the guard's test modules
        covered all of them when three only exercise synthetic trees. This asserts the bound that is
        actually true, so a derived member added later cannot become exempt without a hard check."""
        import derived_state
        exempt = S.derived_output_paths()
        self.assertTrue(exempt, "an empty exempt set would make this test vacuous")
        for member in derived_state.members():
            with self.subTest(member=member.path):
                self.assertTrue(member.check_rules,
                                f"{member.path} is exempt but declares no drift check")
                for rule_id in member.check_rules:
                    rule_path = os.path.join(validate.ENGINE_DIR, "check",
                                             rule_id.split("/")[-1] + ".json")
                    with open(rule_path, encoding="utf-8") as fh:
                        rule = json.load(fh)
                    self.assertEqual(rule["tier"], "hard", f"{rule_id} must be a hard check")
                    self.assertIn("CI", rule["suites"], f"{rule_id} must run in the validator suite")


class TheIterationLoopIsReachable(unittest.TestCase):
    """The test that would have caught the deadlock, and the reason it exists.

    The guard closed a real silent miss and opened a worse hole: editing any tool stales the knowledge
    map, the guard then always selects the test that catches that, so the focused run goes RED — and
    regenerating to clear it put the regenerated map, a non-Python path, into the changed set, so the
    next run classified `full`. No state in an ordinary build iteration was left where a focused run
    could be green. Every unit fixture passed throughout; only walking the actual loop shows it."""

    def test_a_generated_artifact_is_exempt_so_regenerating_does_not_force_a_full_run(self):
        importers = S.build_importer_index(validate.ROOT)
        exempt = S.derived_output_paths()
        self.assertIn(".engine/knowledge/graph.json", exempt,
                      "the register must name the generated map, or the exemption is inert")
        after_regenerate = S.classify(
            [(f"{_TOOLS}/quiet_call.py", "M"), (".engine/knowledge/graph.json", "M")],
            lambda: importers, changed_from="base")
        self.assertEqual(after_regenerate["classification"], "focused",
                         "regenerating a stale map must not force the complete inventory")
        self.assertIn("test_knowledge", {e["module"] for e in after_regenerate["selected"]})

    def test_a_generated_artifact_alone_still_runs_its_guard_tests(self):
        """A hand-edited generated map selects nothing by import, but the guard covers it."""
        importers = S.build_importer_index(validate.ROOT)
        m = S.classify([(".engine/knowledge/graph.json", "M")], lambda: importers, changed_from="base")
        self.assertEqual(m["classification"], "focused")
        self.assertIn("test_knowledge", {e["module"] for e in m["selected"]})


class ExemptionIsInjectableAndRecorded(unittest.TestCase):
    """The exemption removes a path from consideration entirely, so it must be both testable in
    isolation and visible in the artifact — a reviewer found it was neither."""

    def test_the_exemption_can_be_injected_so_a_synthetic_tree_does_not_inherit_the_real_one(self):
        m = S.classify([(_p("widget"), "M"), ("generated/thing.json", "M")],
                       lambda: _index({"test_widget": ["widget"]}),
                       guard_factory=_no_guard,
                       exempt_factory=lambda: frozenset({"generated/thing.json"}),
                       changed_from="base")
        self.assertEqual(m["classification"], "focused")
        self.assertEqual([e["module"] for e in m["selected"]], ["test_widget"])

    def test_an_exempted_path_is_named_in_the_manifest(self):
        m = S.classify([(_p("widget"), "M"), ("generated/thing.json", "M")],
                       lambda: _index({"test_widget": ["widget"]}),
                       guard_factory=_no_guard,
                       exempt_factory=lambda: frozenset({"generated/thing.json"}),
                       changed_from="base")
        self.assertEqual(m["exempt_paths"], ["generated/thing.json"],
                         "a waived path must be visible, not silently dropped")

    def test_nothing_is_exempt_when_the_register_cannot_be_read(self):
        m = S.classify([("generated/thing.json", "M")], lambda: {}, guard_factory=_no_guard,
                       exempt_factory=frozenset, changed_from="base")
        self.assertEqual(m["classification"], "full")
        self.assertEqual(m["exempt_paths"], [])


class ProjectOwnedPaths(unittest.TestCase):
    """Category 4: in a deployed copy, a project's own path contributes no tests.

    The predicate is injected, so these cases decide by name what is the project's; `select()` wires the
    real classifier, which `TheLivePredicate` below exercises against this repository (where nothing is
    the project's) and a synthetic deployed tree."""

    def _classify(self, changed, project, edges=None, guard=_no_guard):
        return S.classify(changed, lambda: _index(edges or {}), guard_factory=guard,
                          project_owned_factory=lambda: (lambda p: p in project), changed_from="base")

    def test_a_project_only_change_set_selects_the_guard_alone_and_says_so(self):
        m = self._classify([("src/app.py", "M"), ("docs/x.md", "A")], {"src/app.py", "docs/x.md"},
                           guard=lambda _i: ({_p("test_map")}, None))
        self.assertEqual(m["classification"], "project-only")
        self.assertEqual(m["project_paths"], ["docs/x.md", "src/app.py"])
        self.assertEqual([e["module"] for e in m["selected"]], ["test_map"])
        self.assertEqual({e["reason"]["code"] for e in m["selected"]}, {"derived-artifact-guard"})

    def test_deleted_and_renamed_project_files_do_not_force_the_full_inventory(self):
        m = self._classify([("src/old.py", "R"), ("src/new.py", "R"), ("docs/gone.md", "D")],
                           {"src/old.py", "src/new.py", "docs/gone.md"},
                           guard=lambda _i: ({_p("test_map")}, None))
        self.assertEqual(m["classification"], "project-only")

    def test_a_project_path_beside_an_exempted_engine_path_is_a_focused_run_not_project_only(self):
        # An exempted path is an Engine path the inventory need not re-run for; it is still the Engine's,
        # and the classifier engine-ci runs would call this change set engine-affecting. The two gates agree.
        m = S.classify([("src/app.py", "M"), ("generated/thing.json", "M")], lambda: {},
                       guard_factory=lambda _i: ({_p("test_map")}, None),
                       exempt_factory=lambda: frozenset({"generated/thing.json"}),
                       project_owned_factory=lambda: (lambda p: p == "src/app.py"), changed_from="base")
        self.assertEqual(m["classification"], "focused")
        self.assertEqual((m["project_paths"], m["exempt_paths"]), (["src/app.py"], ["generated/thing.json"]))

    def test_a_project_path_beside_tool_code_is_an_ordinary_focused_run(self):
        m = self._classify([("src/app.py", "M"), (_p("widget"), "M")], {"src/app.py"},
                           {"test_widget": ["widget"]})
        self.assertEqual(m["classification"], "focused")
        self.assertEqual([e["module"] for e in m["selected"]], ["test_widget"])
        self.assertEqual(m["project_paths"], ["src/app.py"])

    def test_a_project_path_beside_engine_prose_still_runs_everything(self):
        m = self._classify([("src/app.py", "M"), ("CLAUDE.md", "M")], {"src/app.py"})
        self.assertEqual(m["classification"], "full")
        self.assertEqual(m["full_reason"]["code"], "path-not-classifiable")
        self.assertEqual(m["project_paths"], ["src/app.py"])

    def test_a_deleted_engine_file_the_predicate_does_not_claim_still_runs_everything(self):
        m = self._classify([("CLAUDE.md", "D"), ("src/app.py", "M")], {"src/app.py"})
        self.assertEqual(m["full_reason"]["code"], "deleted-or-renamed")

    def test_the_default_predicate_claims_nothing_and_the_manifest_keeps_one_shape(self):
        edges = {"test_widget": ["widget"]}
        m = S.classify([(_p("widget"), "M")], lambda: _index(edges), guard_factory=_no_guard, changed_from="base")
        self.assertEqual(m["classification"], "focused")
        self.assertEqual(m["project_paths"], [])
        full = S.classify([("README.md", "M")], lambda: {}, guard_factory=_no_guard, changed_from="base")
        self.assertEqual(full["classification"], "full")
        self.assertEqual(full["project_paths"], [])

    def test_a_predicate_that_cannot_run_still_selects_as_before(self):
        # `select()` degrades the live predicate to nothing-is-the-project's on any failure; the pure
        # half must never see an exception from the factory as anything but that.
        m = S.classify([("README.md", "M")], lambda: {}, guard_factory=_no_guard,
                       project_owned_factory=S.no_project_owned_paths, changed_from="base")
        self.assertEqual(m["full_reason"]["code"], "path-not-classifiable")


class TheLivePredicate(unittest.TestCase):
    def test_this_checkout_claims_what_its_identity_allows(self):
        # Identity-aware, not home-assuming: in the home nothing is the project's; in a deployed copy (a
        # downstream project running its own inventory) the Engine's paths are still never the project's.
        import change_classification as cc
        is_project = S.project_owned_predicate(validate.ROOT)
        for path in ("CLAUDE.md", ".engine/tools/validate.py", ".mcp.json", ".github/x.yml"):
            self.assertFalse(is_project(path), path)
        at_home = cc.identity_of(validate.ROOT) == cc.IDENTITY_HOME
        for path in ("src/app.py", "README.md"):
            self.assertEqual(is_project(path), not at_home, path)

    def test_a_deployed_tree_owns_only_what_lies_outside_the_engine(self):
        tmp = tempfile.mkdtemp(prefix="selftest-select-deployed-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        subprocess.run(["git", "init", "-q", tmp], check=True)
        subprocess.run(["git", "-C", tmp, "remote", "add", "origin", "https://github.com/test/product.git"],
                       check=True)
        os.makedirs(os.path.join(tmp, ".engine", "modules", "core"))
        with open(os.path.join(tmp, ".engine", "engine.json"), "w", encoding="utf-8") as fh:
            json.dump({"home_repository": "test/home"}, fh)
        with open(os.path.join(tmp, ".engine", "modules", "core", "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump({"id": "core", "provides": {"tool": ["harness/*.py"]}}, fh)
        os.makedirs(os.path.join(tmp, "harness"))
        with open(os.path.join(tmp, "harness", "run.py"), "w", encoding="utf-8") as fh:
            fh.write("x\n")
        is_project = S.project_owned_predicate(tmp)
        self.assertTrue(is_project("src/app.py"))
        self.assertTrue(is_project("README.md"))
        for path in ("CLAUDE.md", ".mcp.json", ".engine/tools/x.py", ".claude/settings.json",
                     "harness/run.py", "harness/other.py", "Harness/other.py"):
            self.assertFalse(is_project(path), path)
        # The predicate IS the classifier's per-path rule: the two gates cannot disagree on a path.
        import change_classification as cc
        register, patterns = cc.register_and_patterns_of(tmp)
        corners = cc.register_corners(register, patterns)
        for path in ("src/app.py", "README.md", "CLAUDE.md", "harness/other.py", ".engine/tools/x.py"):
            self.assertEqual(is_project(path), cc.is_project_owned(path, register, corners), path)
        # Deleting the last file under the provided directory does not hand the directory to the project.
        os.remove(os.path.join(tmp, "harness", "run.py"))
        self.assertFalse(S.project_owned_predicate(tmp)("harness/other.py"))
        # Building the predicate discovers the manifests exactly ONCE — the register and the corners
        # come from one read, never a validated read and then an unchecked second one.
        import module_coherence as mc
        with mock.patch.object(mc, "discover_manifests", wraps=mc.discover_manifests) as discover:
            S.project_owned_predicate(tmp)
        self.assertEqual(discover.call_count, 1)

    def test_a_degenerate_register_makes_nothing_the_projects(self):
        tmp = tempfile.mkdtemp(prefix="selftest-select-degenerate-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        subprocess.run(["git", "init", "-q", tmp], check=True)
        subprocess.run(["git", "-C", tmp, "remote", "add", "origin", "https://github.com/test/product.git"],
                       check=True)
        # A deployed origin but no engine.json: identity is unreadable, so nothing is the project's.
        self.assertFalse(S.project_owned_predicate(tmp)("src/app.py"))


class SelectOnADeployedClone(unittest.TestCase):
    """`select()` end to end — the four-line join that hands the live predicate to the classifier — on a
    `--shared` clone of this checkout with a foreign origin: the real manifests, the real importer index,
    the real derived-artifact guard. A divergence reviewer found this join untested while everything on
    either side of it was; this is the test the earlier repair record claimed."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = os.path.join(cls.tmp.name, "clone")
        subprocess.run(["git", "clone", "-q", "--shared", validate.ROOT, cls.root], check=True,
                       capture_output=True, text=True)
        cls._git("remote", "set-url", "origin", "https://github.com/test/downstream-product.git")
        cls._git("checkout", "-q", "-b", "product")
        cls.base = subprocess.run(["git", "-C", cls.root, "rev-parse", "HEAD"], check=True,
                                  capture_output=True, text=True).stdout.strip()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def _git(cls, *args):
        subprocess.run(["git", "-C", cls.root, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                        "-c", "commit.gpgsign=false", *args], check=True, capture_output=True, text=True)

    def _commit(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        self._git("add", rel)
        self._git("commit", "-q", "-m", f"change {rel}")

    def test_a_product_change_selects_the_guard_alone_and_an_engine_change_does_not(self):
        self._commit("src/app.py", "print(1)\n")
        m = S.select(self.root, self.base)
        self.assertEqual(m["classification"], "project-only", m.get("full_reason"))
        self.assertEqual(m["project_paths"], ["src/app.py"])
        self.assertTrue(m["selected"], "the derived-artifact guard is never empty on the real tree")
        self.assertEqual({e["reason"]["code"] for e in m["selected"]}, {"derived-artifact-guard"})
        self._commit(".engine/tools/zz_new_tool.py", "VALUE = 1\n")
        m = S.select(self.root, self.base)
        self.assertNotEqual(m["classification"], "project-only")
        self.assertEqual(m["project_paths"], ["src/app.py"])


class ThePartitionHasExactlyFourCategories(unittest.TestCase):
    """The exemption is a real third category and the deployed project-owned path a fourth; this is
    where that is stated and checked.

    A conformance reviewer caught the source asserting a two-category partition it no longer had — the
    module docstring and the schema description both still said "there is no third category" after the
    guard had forced one. The categories are: tool code narrows, a registered generated output is
    exempt, everything else runs the full inventory, and — in a deployed copy only — a project's own
    path contributes nothing. Totality still holds because the third is a catch-all."""

    def test_a_registered_generated_output_neither_narrows_nor_forces_the_full_inventory(self):
        importers = S.build_importer_index(validate.ROOT)
        for output in (".engine/knowledge/graph.json", ".engine/self-map.md",
                       ".engine/docs/ci-assurance.md"):
            with self.subTest(output=output):
                self.assertIn(output, S.derived_output_paths())
                m = S.classify([(output, "M")], lambda: importers, changed_from="base")
                self.assertEqual(m["classification"], "focused",
                                 "a regenerated map must not force the complete inventory")
                self.assertEqual({e["reason"]["code"] for e in m["selected"]},
                                 {"derived-artifact-guard"},
                                 "it contributes nothing of its own; the guard is the whole selection")

    def test_deleting_a_registered_generated_output_is_exempt_too(self):
        """Stated explicitly because it is the surprising half: deletions otherwise always force full."""
        importers = S.build_importer_index(validate.ROOT)
        m = S.classify([(".engine/knowledge/graph.json", "D")], lambda: importers, changed_from="base")
        self.assertEqual(m["classification"], "focused")
        self.assertIn("test_knowledge", {e["module"] for e in m["selected"]},
                      "the drift test must still run — that is what makes the exemption safe")

    def test_a_governed_file_that_is_not_a_generated_output_still_forces_the_full_inventory(self):
        """The exemption is exactly the register's contents, never a general softening."""
        importers = S.build_importer_index(validate.ROOT)
        for path in (".engine/check/link-integrity.json", ".engine/operations/build-orchestration.md",
                     ".engine/suites.json"):
            with self.subTest(path=path):
                self.assertNotIn(path, S.derived_output_paths())
                m = S.classify([(path, "M")], lambda: importers, changed_from="base")
                self.assertEqual(m["classification"], "full")
                self.assertEqual(m["full_reason"]["code"], "path-not-classifiable")


class RealManifestMatchesItsSchema(unittest.TestCase):
    """The gap that let production output violate its own schema: every fixture that validated a
    manifest either bypassed the guard or hand-built the selection, so the two reason codes the guard
    introduced were never seen by a validator."""

    def test_a_manifest_from_the_real_guarded_path_validates(self):
        import jsonschema
        with open(os.path.join(validate.ENGINE_DIR, "schemas",
                               "selftest-selection.v1.json"), encoding="utf-8") as fh:
            schema = json.load(fh)
        importers = S.build_importer_index(validate.ROOT)
        produced = S.classify([(f"{_TOOLS}/quiet_call.py", "M")], lambda: importers,
                              changed_from="base")
        self.assertEqual(produced["classification"], "focused")
        self.assertIn("derived-artifact-guard",
                      {e["reason"]["code"] for e in produced["selected"]},
                      "this case is only meaningful if it exercises the guard's own reason code")
        jsonschema.validate(produced, schema)

    def test_every_reason_the_module_can_emit_is_in_the_published_schema(self):
        """Mechanical, so the two vocabularies cannot drift apart again."""
        with open(os.path.join(validate.ENGINE_DIR, "schemas",
                               "selftest-selection.v1.json"), encoding="utf-8") as fh:
            schema = json.load(fh)
        published_full = set(
            schema["properties"]["full_reason"]["oneOf"][1]["properties"]["code"]["enum"])
        published_sel = set(
            schema["properties"]["selected"]["items"]["properties"]["reason"]["properties"]["code"]["enum"])
        self.assertEqual(S.FULL_REASONS, published_full)
        self.assertEqual(S.SELECTION_REASONS, published_sel)
        self.assertEqual(set(schema["properties"]["classification"]["enum"]),
                         {"focused", "full", "project-only"})
        self.assertIn("project_paths", schema["properties"])


class TheProtocolDeclaresExactlyWhatCandidateRuns(unittest.TestCase):
    """The successor to the bare-argv pin, superseded deliberately by the candidate/final split.

    The old claim — 'a focused run cannot become merge evidence because the protocol registers the
    runner bare' — moved house: merge evidence is now the IMPORTED engine-ci proof (the protocol's
    `final` descriptor), and the registered candidate command legitimately narrows. What still needs
    a mechanical hold is the same shape as before: the registered argv is EXACTLY what the registry
    declares — an allowlist, never a denylist of flag names, because `--changed-from=origin/main` is
    one token and `--pattern` narrows too. Any addition, removal or respelling, fails here."""

    def _protocol(self):
        with open(os.path.join(validate.ENGINE_DIR, "build-protocol.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def test_the_registered_candidate_self_test_argv_is_exact(self):
        candidates = self._protocol()["validation_commands"]["candidate"]
        argv = [c for c in candidates if c.get("id") == "engine-selftest"]
        self.assertEqual(len(argv), 1, "the self-test command must be registered exactly once")
        self.assertEqual(
            argv[0]["command"],
            ["uv", "run", "--directory", ".engine", "--frozen", "--", "python", "tools/selftest.py",
             "--changed-from", "{merge_base}", "--run-record-path", "{run_record_path}"],
            "the registered candidate command must be EXACTLY this argv: the affected-selection flag "
            "against the coordinator-substituted merge base, and the run-record path the coordinator "
            "mints. Anything else — one more narrowing flag, one changed spelling — fails here.")

    def test_the_run_record_attests_the_registered_candidate_command_id(self):
        """The coupling a reviewer asked to be pinned: `attests` is the single field that stops a
        record for one registered command standing for another, and it is a const in both the schema
        and the runner — so if the registered id ever moves, this fails rather than the binding
        silently becoming a constant that matches nothing the coordinator ran."""
        import selftest
        candidates = self._protocol()["validation_commands"]["candidate"]
        self_test_ids = [c["id"] for c in candidates if "selftest.py" in " ".join(c["command"])]
        self.assertEqual(self_test_ids, [selftest.RECORD_ATTESTS])
        with open(os.path.join(validate.ENGINE_DIR, "schemas",
                               "selftest-run-record.v1.json"), encoding="utf-8") as fh:
            schema = json.load(fh)
        self.assertEqual(schema["properties"]["attests"]["const"], selftest.RECORD_ATTESTS)

    def test_candidate_ids_are_unique(self):
        """The schema cannot express cross-item uniqueness, so the registry's own test holds it."""
        ids = [c["id"] for c in self._protocol()["validation_commands"]["candidate"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_the_schema_rejects_an_unknown_placeholder(self):
        """A placeholder can never be invented in the registry and silently substituted downstream:
        the only tokens a command may carry are the two coordinator-owned ones."""
        import jsonschema
        with open(os.path.join(validate.ENGINE_DIR, "schemas",
                               "build-protocol.v1.json"), encoding="utf-8") as fh:
            schema = json.load(fh)
        protocol = self._protocol()
        jsonschema.validate(protocol, schema)     # the shipped registry is valid
        for token in ("{arbitrary_token}", "uv{arbitrary_token}", "--flag={bogus_token}",
                      "prefix{run_record_path}", "{merge_base}{arbitrary_token}"):
            with self.subTest(token=token):
                forged = json.loads(json.dumps(protocol))
                forged["validation_commands"]["candidate"][1]["command"].append(token)
                with self.assertRaises(jsonschema.ValidationError):
                    jsonschema.validate(forged, schema)

    def test_the_final_descriptor_names_the_imported_proof(self):
        """`final` is a descriptor of the CI proof to import, not a command to run — a command here
        would be a local path to merge evidence, which is the exact thing the split removes."""
        final = self._protocol()["validation_commands"]["final"]
        self.assertEqual(final["mode"], "ci-import")
        self.assertEqual(final["context"], "engine-ci")
        self.assertEqual(final["workflow"], ".github/workflows/engine-ci.yml")
        self.assertEqual(final["receipt_artifact"], "engine-ci-receipt")
        self.assertNotIn("command", final)


class Determinism(unittest.TestCase):

    def test_the_same_tree_and_changed_set_serialize_byte_identically(self):
        edges = {"test_a": ["core"], "mid": ["core"], "test_b": ["mid"]}
        first = S.classify([(_p("core"), "M")], lambda: _index(edges), guard_factory=_no_guard,
                           changed_from="base")
        second = S.classify([(_p("core"), "M")], lambda: _index(edges), guard_factory=_no_guard,
                            changed_from="base")
        self.assertEqual(S.serialize(first), S.serialize(second))
        self.assertEqual(S.digest(first), S.digest(second))

    def test_the_manifest_validates_against_its_own_schema(self):
        schema_path = os.path.join(validate.ENGINE_DIR, "schemas", "selftest-selection.v1.json")
        with open(schema_path, encoding="utf-8") as fh:
            schema = json.load(fh)
        import jsonschema
        for manifest in (
            S.classify([(_p("test_a"), "M")], lambda: {}, guard_factory=_no_guard, changed_from="base"),
            S.classify([("README.md", "M")], lambda: {}, guard_factory=_no_guard, changed_from="base"),
            S.classify([], lambda: {}, guard_factory=_no_guard, changed_from="base",
                       git_failure="git exploded"),
        ):
            jsonschema.validate(manifest, schema)


class GitBoundary(unittest.TestCase):
    """The impure half, against real throwaway repositories."""

    def _repo(self) -> str:
        tmp = tempfile.mkdtemp(prefix="selftest-select-git-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
        def git(*args):
            subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True, env=env)
        git("init", "-q", "-b", "main")
        os.makedirs(os.path.join(tmp, _TOOLS), exist_ok=True)
        with open(os.path.join(tmp, "seed.txt"), "w") as fh:
            fh.write("seed\n")
        git("add", "-A")
        git("commit", "-q", "-m", "seed")
        self._git = git
        return tmp

    def test_a_brand_new_untracked_test_module_is_part_of_the_changed_set(self):
        """A session's own newly written test must not be invisible to the selector meant to run it.
        A plain diff does not report untracked files, so without the third source this returns nothing."""
        tmp = self._repo()
        new = os.path.join(tmp, _TOOLS, "test_brand_new.py")
        with open(new, "w") as fh:
            fh.write("import unittest\n")
        entries, failure = S.changed_paths(tmp, "HEAD")
        self.assertIsNone(failure)
        self.assertIn((f"{_TOOLS}/test_brand_new.py", "?"), entries)

    def test_a_deletion_is_reported_so_it_can_force_the_full_inventory(self):
        tmp = self._repo()
        os.remove(os.path.join(tmp, "seed.txt"))
        entries, failure = S.changed_paths(tmp, "HEAD")
        self.assertIsNone(failure)
        self.assertIn(("seed.txt", "D"), entries)

    def test_an_unknown_base_is_a_recorded_failure_not_a_crash(self):
        tmp = self._repo()
        entries, failure = S.changed_paths(tmp, "no-such-ref-anywhere")
        self.assertEqual(entries, [])
        self.assertIsNotNone(failure)

    def test_the_command_line_entry_point_emits_a_manifest(self):
        tmp = self._repo()
        proc = subprocess.run(
            [sys.executable, os.path.join(validate.ENGINE_DIR, "tools", "selftest_select.py"),
             "--changed-from", "HEAD", "--root", tmp],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        manifest = json.loads(proc.stdout)
        self.assertEqual(manifest["schema_version"], S.SCHEMA_VERSION)
        self.assertIn(manifest["classification"], ("focused", "full"))


class LiveTree(unittest.TestCase):
    """One case against the real repository, so the graph builder is exercised on real code."""

    def test_the_real_import_graph_builds_and_reaches_this_module(self):
        importers = S.build_importer_index(validate.ROOT)
        self.assertGreater(len(importers), 50, "the real tool graph should be substantial")
        reached = S.reaching_tests(f"{_TOOLS}/selftest_select.py", importers)
        self.assertIn(f"{_TOOLS}/test_selftest_select.py", reached,
                      "this fixture imports the selector, so it must be reached by it")


if __name__ == "__main__":
    unittest.main()
