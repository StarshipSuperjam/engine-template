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
