#!/usr/bin/env python3
"""Self-tests for the module manager — slice 25b `remove` + the group-scoped uv-sync derivation, slice 25c
PR-1 `add` (fetch/overlay), and slice 25c PR-2 the engine `upgrade`/updater + the migrations machinery
(select/run, the no-backup guard, the version-stamp check, and the frozen-check-name invariant).

Run: uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py' -b

Pure policy (refusals, the derivation, migration select/order, version-stamp detection, the tightened
migrations schema) is tested directly on fixture data — no disk mutation; the live mutation glue (`remove`,
`add`, `upgrade`) is exercised end-to-end by the shipped fail-then-pass demos against throwaway fixture
engines, with ONLY the side-effect boundaries faked (release fetch, git/PR open, data backup) and the real
overlay / migration / coherence logic run. The deliverable-gate cold review attests each test's assertion
matches its name.
"""
from __future__ import annotations
import contextlib
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import module_manager  # noqa: E402
import module_coherence  # noqa: E402
import wiring  # noqa: E402


def _m(mid, status="optional", depends=None, version="0.0.0"):
    return (f".engine/modules/{mid}/manifest.json",
            {"id": mid, "version": version, "status": status, "provides": {},
             "depends": depends or {}})


def _cand(mid, version="0.1.0", status="optional", depends=None, provides=None, wires=None):
    """A fetched-module candidate manifest (the shape plan_add receives from the release tree)."""
    return {"id": mid, "version": version, "status": status,
            "provides": provides or {}, "depends": depends or {}, "wires": wires or []}


class TestRemoveRefusals(unittest.TestCase):
    """Pure refusal policy over an injected manifest list — no disk."""

    def test_absent_module_is_refused_plainly(self):
        plan = module_manager.plan_remove("nope", [_m("a")])
        self.assertTrue(plan["refused"])
        self.assertIn("no module named 'nope'", plan["reason"])

    def test_depended_on_module_is_refused_naming_the_dependent(self):
        manifests = [_m("a"), _m("b", depends={"a": ""})]
        plan = module_manager.plan_remove("a", manifests)
        self.assertTrue(plan["refused"])
        self.assertIn("'b'", plan["reason"])          # names the dependent
        self.assertIn("needs it", plan["reason"])     # singular verb agreement

    def test_two_dependents_are_both_named_with_plural_grammar(self):
        manifests = [_m("a"), _m("b", depends={"a": ""}), _m("c", depends={"a": ""})]
        plan = module_manager.plan_remove("a", manifests)
        self.assertTrue(plan["refused"])
        self.assertIn("'b'", plan["reason"])
        self.assertIn("'c'", plan["reason"])
        self.assertIn("need it", plan["reason"])      # plural verb

    def test_required_module_is_refused(self):
        plan = module_manager.plan_remove("base", [_m("base", status="required")])
        self.assertTrue(plan["refused"])
        self.assertIn("required", plan["reason"])

    def test_dependency_refusal_takes_precedence_over_required(self):
        # A module both required AND depended-on surfaces the dependency reason (the actionable one).
        manifests = [_m("base", status="required"), _m("ext", depends={"base": ""})]
        plan = module_manager.plan_remove("base", manifests)
        self.assertTrue(plan["refused"])
        self.assertIn("'ext'", plan["reason"])
        self.assertNotIn("required part", plan["reason"])

    def test_optional_with_no_dependents_is_allowed(self):
        plan = module_manager.plan_remove("a", [_m("a"), _m("b")])
        self.assertFalse(plan["refused"])
        self.assertIsNone(plan["reason"])


class TestRealRepoRefusals(unittest.TestCase):
    """The real required modules — read-only, no mutation."""

    def test_remove_core_is_refused_naming_validators_core(self):
        plan = module_manager.plan_remove("core")
        self.assertTrue(plan["refused"])
        self.assertIn("validators-core", plan["reason"])   # reverse-dependency refusal

    def test_remove_validators_core_is_refused_naming_audit_library(self):
        # audit-library is the first module to depend on validators-core, so its removal is now a
        # reverse-dependency refusal (audit-library needs it), ahead of its required-foundation status.
        plan = module_manager.plan_remove("validators-core")
        self.assertTrue(plan["refused"])
        self.assertIn("audit-library", plan["reason"])     # reverse-dependency refusal

    def test_remove_a_required_leaf_is_refused_as_required(self):
        # a required module that nothing depends on falls through to the required-foundation refusal.
        plan = module_manager.plan_remove("routine-mode")
        self.assertTrue(plan["refused"])
        self.assertIn("required", plan["reason"])          # required-foundation refusal


class TestUvGroupDerivation(unittest.TestCase):

    def test_pep735_normalization(self):
        self.assertEqual(module_manager.normalize_pep735("a_b.c"), "a-b-c")
        self.assertEqual(module_manager.normalize_pep735("Core"), "core")
        self.assertEqual(module_manager.normalize_pep735("validators-core"), "validators-core")
        self.assertEqual(module_manager.normalize_pep735("my__group..x"), "my-group-x")

    def test_derive_matches_committed_default_groups_on_the_real_repo(self):
        # The drift gate: the committed [tool.uv] default-groups equals what the present set derives.
        self.assertEqual(module_manager.derive_uv_groups(), module_manager.committed_default_groups())
        self.assertEqual(module_manager.committed_default_groups(), ["core"])

    def test_a_module_with_no_dependency_group_is_excluded(self):
        with tempfile.TemporaryDirectory() as d:
            py = os.path.join(d, "pyproject.toml")
            with open(py, "w") as fh:
                fh.write('[dependency-groups]\na = ["x"]\n[tool.uv]\ndefault-groups = ["a"]\n')
            # present ids {a, b, c}; only `a` declares a group -> b and c contribute nothing
            manifests = [_m("a"), _m("b"), _m("c")]
            self.assertEqual(module_manager.derive_uv_groups(manifests=manifests, pyproject_path=py), ["a"])

    def test_rewrite_default_groups_is_minimal_and_validates_shape(self):
        text = ('# a comment\n[project]\nname = "x"\n\n[dependency-groups]\n'
                'base = ["p"]\noptx = ["q"]\n\n[tool.uv]\ndefault-groups = ["base", "optx"]\n')
        new, changed = module_manager.rewrite_default_groups_text(text, ["base"])
        self.assertTrue(changed)
        self.assertIn('default-groups = ["base"]', new)
        self.assertIn("[dependency-groups]", new)            # the rest is preserved byte-for-byte
        self.assertIn('optx = ["q"]', new)
        # idempotent: rewriting to the same value reports no change
        _, changed2 = module_manager.rewrite_default_groups_text(new, ["base"])
        self.assertFalse(changed2)
        # a missing, duplicated, or MULTI-LINE array fails loud (the caller fails open, never blind-writes
        # nor silently reformats the operator's pyproject)
        with self.assertRaises(ValueError):
            module_manager.rewrite_default_groups_text("[tool.uv]\n", ["x"])
        with self.assertRaises(ValueError):
            module_manager.rewrite_default_groups_text(text + 'default-groups = ["base"]\n', ["base"])
        with self.assertRaises(ValueError):
            module_manager.rewrite_default_groups_text(
                '[tool.uv]\ndefault-groups = [\n  "base",\n]\n', ["base"])


class TestRemoveEndToEnd(unittest.TestCase):
    """The live `remove` mutation glue — reversal, deletion, engine.json update, group re-derivation,
    idempotence, and post-removal coherence — exercised by the shipped fail-then-pass demo against a
    throwaway fixture engine (real logic, fixture boundary)."""

    def test_run_demo_passes(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(module_manager.run_demo())


class TestRemoveDeletesProvidesAtAnyPath(unittest.TestCase):
    """#409 U16: per-module remove() deletes a module's sole-owned provides regardless of path — so a removed
    module's .claude/ personas + skills do not orphan on disk (before U16 the deletion was gated to .engine/,
    leaving e.g. a removed /engine-design skill live and erroring). The manifest-derived reversal law
    (provisioning README L546-551 / L773-777) deletes the engine-identified files a module provides, wherever
    they live — matching what whole-engine remove_engine already does."""

    def test_remove_deletes_a_claude_skill_provide_not_only_engine_files(self):
        with tempfile.TemporaryDirectory() as d:
            with module_manager._redirect_root(d):
                module_manager._build_fixture(d)
                # give optx a .claude/ skill provide (sole-owned) alongside its .engine/ tool
                mpath = os.path.join(d, ".engine", "modules", "optx", "manifest.json")
                module_manager._write_json(mpath, {
                    "id": "optx", "version": "0.0.0", "status": "optional",
                    "provides": {"tool": [".engine/tools/optx_tool.py"],
                                 "skill": [".claude/skills/optx/SKILL.md"]},
                    "wires": [{"type": "gitignore", "key": "optx-cache", "lines": [".engine/optx/.cache/"]},
                              {"type": "permission", "value": "Bash(optx-tool:*)"}],
                    "depends": {}})
                skill_abs = os.path.join(d, ".claude", "skills", "optx", "SKILL.md")
                os.makedirs(os.path.dirname(skill_abs), exist_ok=True)
                with open(skill_abs, "w", encoding="utf-8") as fh:
                    fh.write("# optx skill\n")
                res = module_manager.remove("optx")
                skill_gone = not os.path.exists(skill_abs)
                tool_gone = not os.path.exists(os.path.join(d, ".engine", "tools", "optx_tool.py"))
        self.assertFalse(res["refused"])
        self.assertTrue(skill_gone, "the removed module's .claude/ skill must be deleted, not orphaned")
        self.assertTrue(tool_gone, "the module's .engine/ tool is still deleted (the path-agnostic sweep)")
        self.assertIn(".claude/skills/optx/SKILL.md", res["deleted"])


class TestAddRefusals(unittest.TestCase):
    """Pure plan_add refusal policy over an injected present set + a fetched candidate — no disk, no fetch."""

    def test_already_installed_is_refused(self):
        plan = module_manager.plan_add("a", _cand("a"), [_m("a")])
        self.assertTrue(plan["refused"])
        self.assertIn("already installed", plan["reason"])

    def test_manifest_id_mismatch_is_refused(self):
        # the fetched files carry a different id than requested (a wrong/corrupt fetch)
        plan = module_manager.plan_add("a", _cand("b"), [_m("base", "required")])
        self.assertTrue(plan["refused"])
        self.assertIn("'a'", plan["reason"])
        self.assertIn("'b'", plan["reason"])          # names what was actually found

    def test_missing_dependency_is_refused_naming_it(self):
        plan = module_manager.plan_add("a", _cand("a", depends={"ghost": ""}), [_m("base", "required")])
        self.assertTrue(plan["refused"])
        self.assertIn("ghost", plan["reason"])         # the absent dependency is named

    def test_out_of_range_dependency_is_refused(self):
        present = [_m("base", "required", version="1.0.0")]
        plan = module_manager.plan_add("a", _cand("a", depends={"base": ">=2.0.0"}), present)
        self.assertTrue(plan["refused"])
        self.assertIn("base", plan["reason"])          # the range rule (reused from coherence) fires

    def test_satisfiable_add_is_allowed(self):
        present = [_m("base", "required", version="1.0.0")]
        plan = module_manager.plan_add("a", _cand("a", version="0.1.0", depends={"base": ">=1.0.0"}), present)
        self.assertFalse(plan["refused"])
        self.assertIsNone(plan["reason"])
        self.assertEqual(plan["version"], "0.1.0")


class TestAddEndToEnd(unittest.TestCase):
    """The live `add` overlay glue — fetch (faked, injected release tree), copy, wire, engine.json record,
    group re-derivation, coherence, and the missing-dependency / already-installed refusals — exercised by
    the shipped add demo against a throwaway fixture (real logic, faked fetch boundary)."""

    def test_add_demo_passes(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(module_manager.add_demo())


class TestAddSafety(unittest.TestCase):
    """The add path's defense-in-depth: a malformed id, a release that places files outside the engine
    (the topology-wall containment guard), and an unreachable release all refuse cleanly, changing nothing."""

    def test_invalid_module_id_is_refused_without_touching_disk(self):
        # refused at the id check, before any discover/fetch — safe on the real repo, no network
        res = module_manager.add("../evil")
        self.assertTrue(res["refused"])
        self.assertIn("not a valid module id", res["reason"])

    def test_escaping_provides_pattern_is_refused_before_any_copy(self):
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            release = os.path.join(d, "release")
            os.makedirs(os.path.join(release, ".engine", "modules", "evil"))
            module_manager._write_json(
                os.path.join(release, ".engine", "modules", "evil", "manifest.json"),
                {"id": "evil", "version": "0.1.0", "status": "optional",
                 "provides": {"tool": ["../sneak.py"]}, "depends": {}})   # climbs out of the engine tree
            with open(os.path.join(d, "sneak.py"), "w", encoding="utf-8") as fh:
                fh.write("# a file the escaping glob would match, OUTSIDE ROOT\n")
            with module_manager._redirect_root(live):
                module_manager._build_add_fixture(live)
                res = module_manager.add("evil", release_tree=release)
            self.assertTrue(res["refused"])
            self.assertIn("outside the engine", res["reason"])
            # nothing was written: the module folder was never created in the live tree
            self.assertFalse(os.path.isdir(os.path.join(live, ".engine", "modules", "evil")))

    def test_unreachable_release_is_a_plain_refusal_changing_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            with module_manager._redirect_root(live):
                module_manager._build_add_fixture(live)                  # engine_release "0.0.0"
                saved = module_manager._fetch_release_tree
                module_manager._fetch_release_tree = lambda *a, **k: (_ for _ in ()).throw(
                    RuntimeError("HTTP Error 404: Not Found"))
                try:
                    res = module_manager.add("feat")                    # real-fetch path -> raises
                finally:
                    module_manager._fetch_release_tree = saved
                engine = module_manager.module_coherence.load_engine_manifest()
            self.assertTrue(res["refused"])
            self.assertIn("Couldn't reach", res["reason"])              # plain, not a raw urllib error
            self.assertIn("Nothing was changed", res["reason"])
            self.assertNotIn("feat", (engine or {}).get("packages", {}))

    def test_add_fetches_from_the_recorded_home_never_origin(self):
        # A module's files come from the engine's recorded HOME, never this repo's own origin (#367, D-281).
        seen = {}
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            with module_manager._redirect_root(live):
                module_manager._build_add_fixture(live)                 # records home "acme/engine-home"
                saved = module_manager._fetch_release_tree

                def _spy(ref, dest, repo=None, token=None):
                    seen["repo"] = repo
                    raise RuntimeError("stop after capturing the source")
                module_manager._fetch_release_tree = _spy
                try:
                    module_manager.add("feat")
                finally:
                    module_manager._fetch_release_tree = saved
        self.assertEqual(seen.get("repo"), "acme/engine-home")          # the HOME, not boot.repo_slug()/origin

    def test_add_with_no_recorded_home_refuses_with_a_remedy_never_origin(self):
        called = {"n": 0}
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            with module_manager._redirect_root(live):
                module_manager._build_add_fixture(live)
                p = os.path.join(live, ".engine", "engine.json")        # strip the home -> a pre-field engine
                m = module_manager.validate.load_json(p)
                m.pop("home_repository", None)
                module_manager._write_json(p, m)
                saved = module_manager._fetch_release_tree
                module_manager._fetch_release_tree = lambda *a, **k: called.__setitem__("n", called["n"] + 1)
                try:
                    res = module_manager.add("feat")
                finally:
                    module_manager._fetch_release_tree = saved
        self.assertTrue(res["refused"])
        self.assertIn("no update home recorded", res["reason"])
        self.assertEqual(called["n"], 0)                                # never reached a fetch -> never origin

    def test_add_release_missing_at_the_home_is_refused_naming_the_home(self):
        import urllib.error
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            with module_manager._redirect_root(live):
                module_manager._build_add_fixture(live)
                saved = module_manager._fetch_release_tree
                module_manager._fetch_release_tree = lambda *a, **k: (_ for _ in ()).throw(
                    urllib.error.HTTPError("u", 404, "Not Found", {}, None))   # a REAL 404 at the home
                try:
                    res = module_manager.add("feat")
                finally:
                    module_manager._fetch_release_tree = saved
        self.assertTrue(res["refused"])
        self.assertIn("acme/engine-home", res["reason"])               # NAMES the home so the operator can check
        self.assertIn("Nothing was changed", res["reason"])


class TestCli(unittest.TestCase):

    def test_status_exits_zero(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(module_manager.main(["status"]), 0)

    def test_plan_remove_required_exits_one(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(module_manager.main(["plan-remove", "validators-core"]), 1)

    def test_demo_exits_zero(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(module_manager.main(["demo"]), 0)

    def test_sync_groups_exits_zero(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(module_manager.main(["sync-groups"]), 0)

    def test_add_already_installed_exits_one_without_fetching(self):
        # `core` is already installed, so add refuses BEFORE any release fetch (no network in this test).
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(module_manager.main(["add", "core"]), 1)


def _man(mid, version="0.0.0", migrations=None, depends=None):
    """A manifest DICT (the shape select_migrations / topological_order consume)."""
    return {"id": mid, "version": version, "status": "required",
            "provides": {}, "depends": depends or {}, "migrations": migrations or {}}


class TestSelectMigrations(unittest.TestCase):
    """PURE migration selection + ordering — no disk, no network."""

    def test_only_in_range_migrations_are_selected(self):
        m = _man("a", migrations={
            "0.1.0": {"description": "x", "run": "migrations/a1.py", "kind": "config"},
            "0.2.0": {"description": "y", "run": "migrations/a2.py", "kind": "data"},
            "0.3.0": {"description": "z", "run": "migrations/a3.py", "kind": "config"}})
        sel = module_manager.select_migrations({"a": "0.1.0"}, {"a": "0.2.0"}, [m])
        self.assertEqual([s["version"] for s in sel], ["0.2.0"])   # > from(0.1.0), <= target(0.2.0)

    def test_within_a_module_versions_order_numerically_not_lexically(self):
        # the "0.10.0" < "0.9.0" string-sort bug guard: 0.9.0 must precede 0.10.0
        m = _man("a", migrations={
            "0.10.0": {"description": "ten", "run": "migrations/x.py", "kind": "config"},
            "0.9.0": {"description": "nine", "run": "migrations/y.py", "kind": "config"}})
        sel = module_manager.select_migrations({"a": "0.0.0"}, {"a": "0.10.0"}, [m])
        self.assertEqual([s["version"] for s in sel], ["0.9.0", "0.10.0"])

    def test_modules_run_in_dependency_order(self):
        base = _man("base", migrations={"1.0.0": {"description": "b", "run": "migrations/b.py",
                                                  "kind": "config"}})
        ext = _man("ext", depends={"base": ""},
                   migrations={"1.0.0": {"description": "e", "run": "migrations/e.py", "kind": "config"}})
        # input order puts ext BEFORE base; topological order must still emit base first (ext needs base)
        sel = module_manager.select_migrations({"base": "0.0.0", "ext": "0.0.0"},
                                               {"base": "1.0.0", "ext": "1.0.0"}, [ext, base])
        self.assertEqual([s["module_id"] for s in sel], ["base", "ext"])

    def test_empty_migrations_select_nothing(self):
        self.assertEqual(
            module_manager.select_migrations({"a": "0.0.0"}, {"a": "1.0.0"}, [_man("a")]), [])


class TestRunMigrations(unittest.TestCase):
    """The runner's execution + the no-backup guard. A migration .py is loaded by path and run; only the
    backup seam (a side-effect boundary) is faked."""

    def setUp(self):
        # A data migration now raises an in-flight marker under memory's dir (#396 U26); isolate ENGINE_MEMORY_DIR
        # to a throwaway so the window never touches the real store (nor flakes on a concurrent session's lock).
        self._memtmp = tempfile.TemporaryDirectory()
        self._prev_mem = os.environ.get("ENGINE_MEMORY_DIR")
        os.environ["ENGINE_MEMORY_DIR"] = self._memtmp.name

    def tearDown(self):
        if self._prev_mem is None:
            os.environ.pop("ENGINE_MEMORY_DIR", None)
        else:
            os.environ["ENGINE_MEMORY_DIR"] = self._prev_mem
        self._memtmp.cleanup()

    def _module_dir(self, d, fname, body):
        md = os.path.join(d, ".engine", "modules", "m")
        os.makedirs(os.path.join(md, "migrations"))
        with open(os.path.join(md, "migrations", fname), "w", encoding="utf-8") as fh:
            fh.write(body)
        return lambda mid: os.path.join(d, ".engine", "modules", mid)

    def test_config_migration_runs_directly(self):
        with tempfile.TemporaryDirectory() as d:
            marker = os.path.join(d, "out.txt")
            mdir = self._module_dir(d, "c.py",
                                    "def migrate(context):\n"
                                    f"    with open({marker!r}, 'w') as fh:\n"
                                    "        fh.write(context['kind'])\n")
            sel = [{"module_id": "m", "version": "0.1.0", "run": "migrations/c.py", "kind": "config"}]
            res = module_manager.run_migrations(sel, {"m": "0.0.0"}, "v1", module_dir=mdir)
            self.assertEqual(res["ran"], ["m -> 0.1.0 (config)"])
            with open(marker) as fh:
                self.assertEqual(fh.read(), "config")

    def test_data_migration_refused_without_a_backup_seam_and_never_runs(self):
        # Force the no-backup-available condition so the test is deterministic regardless of the developer's
        # ambient state: with backup=None, _resolve_backup_seam falls back to the live memory seam, which is
        # available iff memory.migration_backup_available() (a configured vault pointer). On a dev machine with a
        # real vault configured that is True, the seam resolves, and the data migration would RUN — so pin it
        # False here (the same isolation TestBackupSeamResolution uses) to exercise the refusal path this asserts.
        import memory
        orig = memory.migration_backup_available
        memory.migration_backup_available = lambda: False
        try:
            with tempfile.TemporaryDirectory() as d:
                marker = os.path.join(d, "out.txt")
                mdir = self._module_dir(d, "dd.py",
                                        "def migrate(context):\n"
                                        f"    with open({marker!r}, 'w') as fh:\n"
                                        "        fh.write('RAN')\n")
                sel = [{"module_id": "m", "version": "0.2.0", "run": "migrations/dd.py", "kind": "data"}]
                res = module_manager.run_migrations(sel, {"m": "0.0.0"}, "v1", module_dir=mdir, backup=None)
                self.assertEqual(res["ran"], [])
                self.assertEqual(len(res["refused"]), 1)
                self.assertIn("no data backup", res["refused"][0])
                self.assertFalse(os.path.exists(marker))          # the migration body never ran
        finally:
            memory.migration_backup_available = orig

    def test_data_migration_runs_after_backup_and_is_stamped(self):
        with tempfile.TemporaryDirectory() as d:
            marker = os.path.join(d, "out.txt")
            mdir = self._module_dir(d, "dd.py",
                                    "def migrate(context):\n"
                                    "    handle = context['backup']('store', context['engine_version'])\n"
                                    "    assert handle\n"
                                    f"    with open({marker!r}, 'w') as fh:\n"
                                    "        fh.write(context['engine_version'])\n")
            calls = []
            seam = lambda store, ver, migration_id=None, **kw: (
                calls.append((store, ver, migration_id, kw.get("reversibility_floor"))) or {"ok": True})
            sel = [{"module_id": "m", "version": "0.2.0", "run": "migrations/dd.py", "kind": "data"}]
            res = module_manager.run_migrations(sel, {"m": "0.0.0"}, "v2", module_dir=mdir, backup=seam)
            self.assertEqual(res["ran"], ["m -> 0.2.0 (data)"])
            # the backup was taken (before the body ran), with the migration id bound in for collision-free naming, and
            # the lone data migration is the upgrade's reversibility floor (#303)
            self.assertEqual(calls, [("store", "v2", "m@0.2.0", True)])
            with open(marker) as fh:
                self.assertEqual(fh.read(), "v2")              # the migration stamped the engine version

    def test_run_migrations_flags_an_unlockable_snapshot_for_the_operator(self):
        # The snapshot handle carries whether the retained pre-update copy could be locked; run_migrations reduces
        # it to a single flag so the upgrade can tell the operator to keep an unlockable copy. Only a handle that
        # plainly reports it could NOT be locked (hardened is False) sets the flag.
        with tempfile.TemporaryDirectory() as d:
            mdir = self._module_dir(d, "dd.py",
                                    "def migrate(context):\n"
                                    "    assert context['backup']('store', context['engine_version'])\n")
            sel = [{"module_id": "m", "version": "0.2.0", "run": "migrations/dd.py", "kind": "data"}]
            unprot = module_manager.run_migrations(sel, {"m": "0.0.0"}, "v2", module_dir=mdir,
                                                   backup=lambda *a, **k: {"backed-up": True, "hardened": False})
            self.assertTrue(unprot["backup_unprotected"])
            prot = module_manager.run_migrations(sel, {"m": "0.0.0"}, "v2", module_dir=mdir,
                                                 backup=lambda *a, **k: {"backed-up": True, "hardened": True})
            self.assertFalse(prot["backup_unprotected"])
            empty = module_manager.run_migrations([], {}, "v2", module_dir=mdir)
            self.assertFalse(empty["backup_unprotected"])       # no data migration -> never flagged

    def test_only_the_first_data_migration_of_the_upgrade_is_the_reversibility_floor(self):
        # #303: one run_migrations call == one upgrade. reversibility_floor is True for the FIRST data migration only;
        # config migrations take no backup, and later data migrations of the same upgrade are NOT the floor.
        with tempfile.TemporaryDirectory() as d:
            md = os.path.join(d, ".engine", "modules", "m", "migrations")
            os.makedirs(md)
            body = ("def migrate(context):\n"
                    "    if context['kind'] == 'data':\n"
                    "        assert context['backup']('store', context['engine_version'])\n")
            for fn in ("c.py", "d0.py", "d1.py"):
                with open(os.path.join(md, fn), "w", encoding="utf-8") as fh:
                    fh.write(body)
            mdir = lambda mid: os.path.join(d, ".engine", "modules", mid)
            calls = []
            seam = lambda store, ver, migration_id=None, **kw: (
                calls.append((migration_id, kw.get("reversibility_floor"))) or {"ok": True})
            sel = [{"module_id": "m", "version": "0.1.0", "run": "migrations/c.py", "kind": "config"},
                   {"module_id": "m", "version": "0.2.0", "run": "migrations/d0.py", "kind": "data"},
                   {"module_id": "m", "version": "0.3.0", "run": "migrations/d1.py", "kind": "data"}]
            res = module_manager.run_migrations(sel, {"m": "0.0.0"}, "v2", module_dir=mdir, backup=seam)
            self.assertEqual(len(res["ran"]), 3)
            self.assertEqual(calls, [("m@0.2.0", True), ("m@0.3.0", False)])   # floor = first data migration only

    def test_a_data_migration_runs_inside_an_in_flight_window_that_clears_after(self):
        # U26 (#396): the marker is present DURING the migration (the body reads it) and lowered AFTER.
        from memory import capture, ledger
        with tempfile.TemporaryDirectory() as d:
            seen = os.path.join(d, "seen.txt")
            mdir = self._module_dir(d, "dd.py",
                                    "def migrate(context):\n"
                                    "    context['backup']('store', context['engine_version'])\n"
                                    "    from memory import capture, ledger\n"
                                    "    inflight = capture.migration_in_flight(ledger.ledger_dir())\n"
                                    f"    open({seen!r}, 'w').write('1' if inflight else '0')\n")
            seam = lambda store, ver, **kw: {"ok": True}
            sel = [{"module_id": "m", "version": "0.2.0", "run": "migrations/dd.py", "kind": "data"}]
            res = module_manager.run_migrations(sel, {"m": "0.0.0"}, "v2", module_dir=mdir, backup=seam)
            self.assertEqual(res["ran"], ["m -> 0.2.0 (data)"])
            with open(seen) as fh:
                self.assertEqual(fh.read(), "1")               # the window was open while the body ran
            self.assertFalse(capture.migration_in_flight(ledger.ledger_dir()))   # lowered after

    def test_a_data_migration_is_refused_when_the_window_cannot_open(self):
        # Fail CLOSED: a held single-writer lock => the marker can't be raised => the migration is REFUSED, never
        # run marker-less (the exact interleave U26 prevents). The body must not run.
        from memory import capture, ledger
        with tempfile.TemporaryDirectory() as d:
            ran = os.path.join(d, "ran.txt")
            mdir = self._module_dir(d, "dd.py",
                                    "def migrate(context):\n"
                                    "    context['backup']('store', context['engine_version'])\n"
                                    f"    open({ran!r}, 'w').write('RAN')\n")
            data_dir = ledger.ledger_dir()
            os.makedirs(data_dir, exist_ok=True)
            lock_fd = capture._acquire_lock(os.path.join(data_dir, capture.LOCK_FILENAME))
            self.addCleanup(capture._release_lock, lock_fd)
            seam = lambda store, ver, **kw: {"ok": True}
            sel = [{"module_id": "m", "version": "0.2.0", "run": "migrations/dd.py", "kind": "data"}]
            res = module_manager.run_migrations(sel, {"m": "0.0.0"}, "v2", module_dir=mdir, backup=seam)
            self.assertEqual(res["ran"], [])
            self.assertEqual(len(res["refused"]), 1)
            self.assertIn("couldn't start safely", res["refused"][0])
            self.assertFalse(os.path.exists(ran))              # the body never ran

    def test_data_migration_whose_backup_fails_at_runtime_refuses_cleanly_without_mutating(self):
        # A seam resolved LIVE but that returns a falsy handle at call time (a vault reachable at pre-flight,
        # gone at the snapshot): the migration's backup-first assert fires; run_migrations must DEGRADE LOUD
        # (a refusal, not a raw traceback) and the body must not have mutated.
        with tempfile.TemporaryDirectory() as d:
            marker = os.path.join(d, "out.txt")
            mdir = self._module_dir(d, "dd.py",
                                    "def migrate(context):\n"
                                    "    handle = context['backup']('store', context['engine_version'])\n"
                                    "    assert handle, 'backup-first: a data migration must snapshot before mutating'\n"
                                    f"    open({marker!r}, 'w').write('RAN')\n")
            sel = [{"module_id": "m", "version": "0.2.0", "run": "migrations/dd.py", "kind": "data"}]
            failing = lambda store, ver, **kw: None            # the backup fails at the moment of the snapshot
            res = module_manager.run_migrations(sel, {"m": "0.0.0"}, "v2", module_dir=mdir, backup=failing)
            self.assertEqual(res["ran"], [])                   # not counted as run
            self.assertEqual(len(res["refused"]), 1)
            self.assertIn("backup could not be completed", res["refused"][0])
            self.assertIn("Ask me to", res["refused"][0])      # carries a recovery action
            self.assertFalse(os.path.exists(marker))           # the body bailed before mutating


class TestVersionStamp(unittest.TestCase):
    """The post-revert version-stamp check: pure detection + the read-only promote_finding surfacing."""

    def test_mismatch_detected_when_running_code_is_older_than_the_data(self):
        f = module_manager.stamp_mismatch_finding("ledger", "0.2.0", "0.1.0", "engine restore ledger")
        self.assertIsNotNone(f)
        self.assertEqual(f["severity"], "hard")
        self.assertIn("restore", f["message"].lower())

    def test_no_mismatch_when_code_is_at_or_ahead_of_the_data(self):
        self.assertIsNone(module_manager.stamp_mismatch_finding("ledger", "0.2.0", "0.2.0", "cmd"))
        self.assertIsNone(module_manager.stamp_mismatch_finding("ledger", "0.2.0", "0.3.0", "cmd"))

    def test_the_finding_message_is_plain_peer_voice_and_carries_no_raw_ref(self):
        # the promoted Issue body is operator-facing (boot.open_findings renders it): plain peer voice, never a
        # tag/ref/version-machinery (D-265 S1 — the operator meets a plain handle, not the mechanism).
        f = module_manager.stamp_mismatch_finding(
            "recall-ledger", "2.0.0", "1.0.0", "ask me to restore the copy saved before the last update")
        msg = f["message"]
        for banned in ("refs/", "engine-snapshot/", "@", "stored data"):
            self.assertNotIn(banned, msg)
        self.assertIn("saved memory", msg.lower())             # peer voice

    def test_surface_promotes_exactly_one_finding_via_a_faked_github(self):
        import telemetry
        gh = telemetry.GitHubIssues("you/proj", "tok", transport=telemetry._FakeGitHub().transport)
        num = module_manager.surface_stamp_mismatch(
            "ledger", "0.2.0", "0.1.0", "engine restore ledger",
            now="2026-01-01T00:00:00Z", github=gh)
        self.assertTrue(num)                                   # an Issue number was opened
        # no mismatch -> nothing surfaced
        self.assertIsNone(module_manager.surface_stamp_mismatch(
            "ledger", "0.2.0", "0.2.0", "cmd", now="2026-01-01T00:00:00Z", github=gh))


class TestEngineReleaseNormalization(unittest.TestCase):
    """The stored engine_release stays BARE even when the resolved release ref is v-prefixed, so the manifest
    never carries the engine as `v0.1.0` while the packages read `0.1.0` (the tag-grammar round-trip fix)."""

    def test_a_v_prefixed_tag_is_stored_bare_and_consistent_with_the_packages(self):
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)
                module_manager._bump_engine_manifest({"base": "0.2.0"}, "v0.2.0")   # v-prefixed ref in
                engine = module_manager.module_coherence.load_engine_manifest()
        self.assertEqual((engine or {}).get("engine_release"), "0.2.0")             # stored bare, not "v0.2.0"
        self.assertEqual((engine or {}).get("packages", {}).get("base"), "0.2.0")   # matches the package form

    def test_a_bare_ref_is_stored_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)
                module_manager._bump_engine_manifest({"base": "0.2.0"}, "0.2.0")    # already bare -> no double-strip
                engine = module_manager.module_coherence.load_engine_manifest()
        self.assertEqual((engine or {}).get("engine_release"), "0.2.0")


class TestMigrationsSchema(unittest.TestCase):
    """The tightened module.v1.json `migrations` shape: a well-formed entry passes, a malformed one fails
    the same schema the hard/CI module-manifest check enforces."""

    def _validator(self):
        from jsonschema import Draft202012Validator
        schema = module_manager.validate.load_json(
            os.path.join(module_manager.validate.ROOT, ".engine", "schemas", "module.v1.json"))
        return Draft202012Validator(schema)

    def test_wellformed_migrations_entry_validates(self):
        man = {"id": "a", "version": "1.0.0", "status": "optional", "provides": {},
               "migrations": {"1.0.0": {"description": "reshape", "run": "migrations/x.py", "kind": "data"}}}
        self.assertEqual(list(self._validator().iter_errors(man)), [])

    def test_migrations_entry_missing_kind_is_rejected(self):
        man = {"id": "a", "version": "1.0.0", "status": "optional", "provides": {},
               "migrations": {"1.0.0": {"description": "reshape", "run": "migrations/x.py"}}}
        self.assertTrue(list(self._validator().iter_errors(man)))    # `kind` is required

    def test_migrations_entry_with_bad_kind_is_rejected(self):
        man = {"id": "a", "version": "1.0.0", "status": "optional", "provides": {},
               "migrations": {"1.0.0": {"description": "x", "run": "migrations/x.py", "kind": "sideways"}}}
        self.assertTrue(list(self._validator().iter_errors(man)))    # kind must be data|config

    def test_present_manifests_carry_no_migrations_field_so_stay_valid(self):
        # the tightening is zero-breakage today: neither shipped manifest declares migrations
        v = self._validator()
        for rel, m in module_manager.module_coherence.discover_manifests():
            self.assertEqual(list(v.iter_errors(m)), [], f"{rel} must validate against module.v1.json")


class TestUpgradeEndToEnd(unittest.TestCase):
    """The live upgrade glue — faked fetch, overlay off the present set, wiring deltas, the migration runner
    (config + backup-first data), the engine-manifest bump with identity preserved, coherence, and the
    faked PR open — plus the degrade and no-backup paths — exercised by the shipped upgrade demo."""

    def test_upgrade_demo_passes(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(module_manager.upgrade_demo())


class TestUpgradeSafety(unittest.TestCase):
    """Upgrade's defense-in-depth: degrade on an unreachable release, the data-migration pre-flight refusal
    (before any overlay), and the overlay containment guard — each changes nothing."""

    def test_unreachable_release_degrades_to_the_current_version(self):
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)
                saved = module_manager._fetch_release_tree
                module_manager._fetch_release_tree = lambda *a, **k: (_ for _ in ()).throw(
                    RuntimeError("HTTP Error 404: Not Found"))
                try:
                    res = module_manager.upgrade(ref="v9.9.9")
                finally:
                    module_manager._fetch_release_tree = saved
                engine = module_manager.module_coherence.load_engine_manifest()
            self.assertTrue(res["refused"])
            self.assertIn("Couldn't reach", res["reason"])
            self.assertEqual((engine or {}).get("packages", {}).get("base"), "0.0.0")   # unchanged

    def test_upgrade_resolves_and_fetches_from_the_recorded_home_never_origin(self):
        # BOTH the latest-ref resolution and the release fetch target the engine's recorded HOME (#367, D-281).
        seen = {}
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)             # records home "acme/engine-home"
                sr, sf = module_manager._resolve_release_ref, module_manager._fetch_release_tree
                module_manager._resolve_release_ref = lambda ref, repo=None, token=None: (
                    seen.__setitem__("resolve_repo", repo) or "v0.2.0")

                def _spy(ref, dest, repo=None, token=None):
                    seen["fetch_repo"] = repo
                    raise RuntimeError("stop after capturing the source")
                module_manager._fetch_release_tree = _spy
                try:
                    module_manager.upgrade()                            # ref=None -> resolve latest FROM THE HOME
                finally:
                    module_manager._resolve_release_ref, module_manager._fetch_release_tree = sr, sf
        self.assertEqual(seen.get("resolve_repo"), "acme/engine-home")
        self.assertEqual(seen.get("fetch_repo"), "acme/engine-home")

    def test_upgrade_with_no_recorded_home_refuses_with_a_remedy_never_origin(self):
        called = {"n": 0}
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)
                p = os.path.join(live, ".engine", "engine.json")        # strip the home -> a pre-field engine
                m = module_manager.validate.load_json(p)
                m.pop("home_repository", None)
                module_manager._write_json(p, m)
                sf = module_manager._fetch_release_tree
                module_manager._fetch_release_tree = lambda *a, **k: called.__setitem__("n", called["n"] + 1)
                try:
                    res = module_manager.upgrade()
                finally:
                    module_manager._fetch_release_tree = sf
                engine = module_manager.module_coherence.load_engine_manifest()
        self.assertTrue(res["refused"])
        self.assertIn("no update home recorded", res["reason"])
        self.assertEqual(called["n"], 0)                                # never fell through to a fetch (never origin)
        self.assertEqual((engine or {}).get("packages", {}).get("base"), "0.0.0")   # nothing changed

    def test_upgrade_release_missing_at_the_home_refuses_naming_it_distinct_from_transport(self):
        import urllib.error
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)
                sf = module_manager._fetch_release_tree
                module_manager._fetch_release_tree = lambda *a, **k: (_ for _ in ()).throw(
                    urllib.error.HTTPError("u", 404, "Not Found", {}, None))   # a REAL 404 at the home
                try:
                    missing = module_manager.upgrade(ref="v9.9.9")
                finally:
                    module_manager._fetch_release_tree = sf
        self.assertTrue(missing["refused"])
        self.assertIn("acme/engine-home", missing["reason"])            # names the home (not just "the release")
        self.assertNotIn("network", missing["reason"].lower())          # distinct from the transport-degrade wording

    def test_no_published_release_at_the_home_refuses_naming_it_not_transport(self):
        # A reachable home with NO published release (releases API 200, null tag -> _NoPublishedRelease) is a
        # missing-release: refused naming the home, not mis-degraded as a network failure (#367 tech review).
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)
                sr = module_manager._resolve_release_ref
                module_manager._resolve_release_ref = lambda *a, **k: (_ for _ in ()).throw(
                    module_manager._NoPublishedRelease("no published release"))
                try:
                    res = module_manager.upgrade()   # ref=None -> resolve latest -> no published release
                finally:
                    module_manager._resolve_release_ref = sr
        self.assertTrue(res["refused"])
        self.assertIn("acme/engine-home", res["reason"])               # names the home
        self.assertNotIn("network", res["reason"].lower())             # not the transport-degrade wording

    def test_upgrade_preserves_the_recorded_home_across_the_version_bump(self):
        # Law 1 (D-281): engine.json is preserved-not-overlaid, so a successful upgrade keeps the home while
        # the module versions DO bump.
        opened = []
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            release = module_manager._build_upgrade_release(os.path.join(d, "release"))
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)
                module_manager.upgrade(ref="v0.2.0", release_tree=release,
                                       opener=lambda **k: opened.append(k) or {"number": 1},
                                       backup=lambda *a, **k: {"ok": 1})
                engine = module_manager.module_coherence.load_engine_manifest()
        self.assertEqual((engine or {}).get("home_repository"), "acme/engine-home")   # preserved
        self.assertEqual((engine or {}).get("packages", {}).get("base"), "0.2.0")     # but versions bumped

    def test_upgrade_reasserts_the_foundation_gitignore_fence_and_keeps_operator_lines(self):
        # #409 U14: the foundation fence is release-evolvable — an upgrade re-applies it (like the CODEOWNERS
        # re-render / CLAUDE.md floor merge), so a repo provisioned before/without it converges, and an
        # operator's own ignore lines are preserved (block-scoped apply, never a wholesale overlay).
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            release = module_manager._build_upgrade_release(os.path.join(d, "release"))
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)
                with open(os.path.join(live, ".gitignore"), "w", encoding="utf-8") as fh:
                    fh.write("# mine\nnode_modules/\n")               # operator content, no engine fence yet
                res = module_manager.upgrade(ref="v0.2.0", release_tree=release,
                                             opener=lambda **k: {"number": 1}, backup=lambda *a, **k: {"ok": 1})
                after = module_manager.validate.read(os.path.join(live, ".gitignore"))
            self.assertEqual(res["foundation_ignores"]["status"], "written")
            self.assertIn("BEGIN engine-managed block: foundation-ignores", after)
            self.assertIn(".engine/.venv/", after)
            self.assertIn("node_modules/", after, "the operator's own ignore lines are preserved on upgrade")

    def test_data_migration_without_backup_refuses_the_whole_upgrade_before_overlay(self):
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            release = module_manager._build_upgrade_release(os.path.join(d, "release"))
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)
                res = module_manager.upgrade(ref="v0.2.0", release_tree=release,
                                             opener=lambda **k: {"number": 1}, backup=None)
                engine = module_manager.module_coherence.load_engine_manifest()
                tool = module_manager.validate.read(os.path.join(live, ".engine/tools/base_tool.py"))
            self.assertTrue(res["refused"])
            self.assertIn("data", res["reason"])
            self.assertEqual((engine or {}).get("packages", {}).get("base"), "0.0.0")   # NOT bumped
            self.assertIn("v0", tool)        # base tool NOT overlaid: the pre-flight refuses before any write

    def test_data_migration_whose_backup_fails_at_runtime_is_declined_not_crashed(self):
        # A backup available at pre-flight but that fails at the snapshot must NOT crash upgrade() with a raw
        # traceback: the migration refuses (degrade loud), upgrade declines to open the change for review, and
        # nothing is merged. (opener must never fire.)
        opened = []
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            release = module_manager._build_upgrade_release(os.path.join(d, "release"))
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)
                res = module_manager.upgrade(
                    ref="v0.2.0", release_tree=release,
                    opener=lambda **k: opened.append(k) or {"number": 1},
                    backup=lambda *a, **k: None)               # resolved live, but the snapshot fails at run time
        self.assertFalse(res.get("refused"))                   # not a pre-flight refusal — it got past the overlay
        self.assertIn("backup did not succeed", res["reason"])
        self.assertIn("NOT opened for review", res["reason"])
        self.assertEqual(opened, [])                           # the change was never opened for review

    def test_a_successful_data_migration_upgrade_discloses_the_saved_copy_once(self):
        # Floor (c) (D-264): a successful data-migration upgrade tells the operator a pre-update copy was saved —
        # ONCE per upgrade, plainly, as reassurance for the later restore offer.
        opened = []
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            release = module_manager._build_upgrade_release(os.path.join(d, "release"))
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)
                res = module_manager.upgrade(
                    ref="v0.2.0", release_tree=release,
                    opener=lambda **k: opened.append(k) or {"number": 1},
                    backup=lambda *a, **k: {"ok": 1})          # the pre-update snapshot succeeds
        self.assertTrue(opened)                                # the upgrade actually opened for review
        disclosures = [n for n in res.get("notes", []) if "saved a copy of it from right before this update" in n]
        self.assertEqual(len(disclosures), 1)                  # exactly one disclosure per upgrade
        self.assertIn("nothing for you to do now", disclosures[0])
        # the seam here reports nothing about locking, so no keep-it heads-up is added
        self.assertNotIn("couldn't confirm", disclosures[0])

    def test_an_unlockable_saved_copy_adds_a_keep_it_heads_up(self):
        # When the retained copy could not be locked against hand-deletion (hardened is False), the reversibility
        # disclosure carries a plain heads-up to keep it — so the operator does not delete their own undo.
        opened = []
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            release = module_manager._build_upgrade_release(os.path.join(d, "release"))
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)
                res = module_manager.upgrade(
                    ref="v0.2.0", release_tree=release,
                    opener=lambda **k: opened.append(k) or {"number": 1},
                    backup=lambda *a, **k: {"backed-up": True, "hardened": False})   # can't be locked on this plan
        disclosures = [n for n in res.get("notes", []) if "saved a copy of it from right before this update" in n]
        self.assertEqual(len(disclosures), 1)
        self.assertIn("couldn't confirm", disclosures[0])       # states what the engine knows, not a guess about why
        self.assertIn("deleted by hand", disclosures[0])
        self.assertIn("keep it in place", disclosures[0])

    def test_a_lockable_saved_copy_has_no_keep_it_heads_up(self):
        opened = []
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            release = module_manager._build_upgrade_release(os.path.join(d, "release"))
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)
                res = module_manager.upgrade(
                    ref="v0.2.0", release_tree=release,
                    opener=lambda **k: opened.append(k) or {"number": 1},
                    backup=lambda *a, **k: {"backed-up": True, "hardened": True})    # locked -> nothing to warn
        disclosures = [n for n in res.get("notes", []) if "saved a copy of it from right before this update" in n]
        self.assertEqual(len(disclosures), 1)
        self.assertNotIn("couldn't confirm", disclosures[0])

    def test_escaping_provides_in_a_release_is_refused_before_any_write(self):
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            release = os.path.join(d, "release")
            os.makedirs(os.path.join(release, ".engine", "modules", "base"))
            module_manager._write_json(
                os.path.join(release, ".engine", "modules", "base", "manifest.json"),
                {"id": "base", "version": "0.2.0", "status": "required",
                 "provides": {"tool": ["../sneak.py"]}, "depends": {}, "migrations": {}})
            with open(os.path.join(d, "sneak.py"), "w", encoding="utf-8") as fh:
                fh.write("# outside ROOT\n")
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)
                res = module_manager.upgrade(ref="v0.2.0", release_tree=release,
                                             opener=lambda **k: {"number": 1}, backup=lambda *a, **k: {"ok": 1})
            self.assertTrue(res["refused"])
            self.assertIn("outside the engine", res["reason"])

    def test_upgrade_refreshes_codeowners_with_new_engine_files_keeping_operator_rules(self):
        # The design's upgrade re-render (provisioning §Token substitution; engine.json `handle`): a release
        # whose `base` ADDS an engine file must land in the CODEOWNERS wall so the new file still routes to
        # the operator — and an operator's OWN rule must survive (fence-scoped).
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            release = os.path.join(d, "release")
            os.makedirs(os.path.join(release, ".engine", "modules", "base"))
            os.makedirs(os.path.join(release, ".engine", "tools"))
            module_manager._write_json(
                os.path.join(release, ".engine", "modules", "base", "manifest.json"),
                {"id": "base", "version": "0.2.0", "status": "required",
                 "provides": {"tool": [".engine/tools/base_tool.py", ".engine/tools/base_helper.py"]},
                 "depends": {}, "migrations": {}})
            for rel, txt in ((".engine/tools/base_tool.py", "# base v2\n"),
                             (".engine/tools/base_helper.py", "# a NEW engine file shipped in v2\n")):
                with open(os.path.join(release, rel), "w") as fh:
                    fh.write(txt)
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)
                eng = module_coherence.load_engine_manifest()
                eng["handle"] = "@me"            # the preserved-identity owner first-run would have recorded
                module_manager._write_json(os.path.join(live, ".engine", "engine.json"), eng)
                co_path = os.path.join(live, ".github", "CODEOWNERS")
                os.makedirs(os.path.dirname(co_path), exist_ok=True)
                # seed an operator's OWN rule + the OLD engine block (pre-upgrade path set, no base_helper)
                with open(co_path, "w") as fh:
                    fh.write(wiring.render_codeowners("# mine\n/src/ @me\n",
                                                      module_coherence.codeowners_path_set(), "@me"))
                self.assertNotIn("base_helper.py", module_manager.validate.read(co_path))   # not engine-owned yet
                res = module_manager.upgrade(ref="v0.2.0", release_tree=release,
                                             opener=lambda **k: {"number": 1})
                after = module_manager.validate.read(co_path)
            self.assertFalse(res["refused"])
            self.assertEqual(res["codeowners"], "written")
            self.assertIn("/.engine/tools/base_helper.py @me", after)   # the new file now routes for review
            self.assertIn("/src/ @me", after)                           # the operator's own rule survived

    def test_upgrade_without_a_handle_degrades_codeowners_leaving_it_unchanged(self):
        # No operator handle on record (the construction repo / a pre-handle manifest) -> the re-render
        # DEGRADES: nothing written, no crash, the operator's CODEOWNERS left exactly as it was.
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            release = module_manager._build_upgrade_release(os.path.join(d, "release"))
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)              # engine.json carries NO handle
                co_path = os.path.join(live, ".github", "CODEOWNERS")
                os.makedirs(os.path.dirname(co_path), exist_ok=True)
                with open(co_path, "w") as fh:
                    fh.write("# operator only\n/src/ @me\n")
                before = module_manager.validate.read(co_path)
                res = module_manager.upgrade(ref="v0.2.0", release_tree=release,
                                             opener=lambda **k: {"number": 1}, backup=lambda *a, **k: {"ok": 1})
                after = module_manager.validate.read(co_path)
            self.assertFalse(res["refused"])
            self.assertEqual(res["codeowners"], "degraded")
            self.assertEqual(after, before)                             # untouched on degrade


class TestMergeClaudeFloor(unittest.TestCase):
    """`_merge_claude_floor` keyed-merges the engine floor from a release's CLAUDE.deployed.md into the local
    CLAUDE.md — replacing only the `floor` block, preserving operator content, never appending a duplicate or
    crashing, and never letting the release's construction CLAUDE.md overlay the floor (#234 6a)."""
    FENCE = module_manager._FLOOR_FENCE
    STYLE = wiring.MD_FENCE

    def _release(self, d, floor_text="# New floor\n\nProject status v2.\n", construction=True):
        rel = os.path.join(d, "release")
        os.makedirs(rel, exist_ok=True)
        if floor_text is not None:
            with open(os.path.join(rel, "CLAUDE.deployed.md"), "w", encoding="utf-8") as fh:
                fh.write(floor_text)
        if construction:
            with open(os.path.join(rel, "CLAUDE.md"), "w", encoding="utf-8") as fh:
                fh.write("# engine-template — construction governance\n\nbuild notes\n")
        return rel

    def _write_local(self, live, text):
        with open(os.path.join(live, "CLAUDE.md"), "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_replaces_only_the_block_and_preserves_operator_content(self):
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live"); os.makedirs(live)
            rel = self._release(d)
            top, bottom = "# My product\n\nintro\n\n", "\n## More\n\ntail\n"
            with module_manager._redirect_root(live):
                self._write_local(
                    live, top + wiring.fence_apply("", self.FENCE, ["old"], style=self.STYLE) + bottom)
                out = module_manager._merge_claude_floor(rel)
                after = module_manager.validate.read(os.path.join(live, "CLAUDE.md"))
        self.assertEqual(out, "merged")
        self.assertIn(top, after)
        self.assertIn(bottom, after)
        self.assertIn("Project status v2.", after)
        self.assertNotIn("old", after)
        self.assertNotIn("construction governance", after)     # the release construction file never overlays

    def test_no_local_fence_is_skipped_not_appended(self):
        # A pre-6a raw-floor (or any fence-less) CLAUDE.md is LEFT UNTOUCHED — never a duplicate floor.
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live"); os.makedirs(live)
            rel = self._release(d)
            raw = "# Your project runs on an Engine\n\nProject status (raw, unfenced).\n"
            with module_manager._redirect_root(live):
                self._write_local(live, raw)
                out = module_manager._merge_claude_floor(rel)
                after = module_manager.validate.read(os.path.join(live, "CLAUDE.md"))
        self.assertEqual(out, "skipped-no-section")
        self.assertEqual(after, raw)                           # untouched, no append
        self.assertNotIn("Project status v2.", after)

    def test_release_without_a_floor_source_is_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live"); os.makedirs(live)
            rel = self._release(d, floor_text=None)            # no CLAUDE.deployed.md in the release
            fenced = wiring.fence_apply("", self.FENCE, ["keep"], style=self.STYLE)
            with module_manager._redirect_root(live):
                self._write_local(live, fenced)
                out = module_manager._merge_claude_floor(rel)
                after = module_manager.validate.read(os.path.join(live, "CLAUDE.md"))
        self.assertEqual(out, "skipped")
        self.assertEqual(after, fenced)                        # untouched

    def test_malformed_local_fence_degrades_without_crashing(self):
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live"); os.makedirs(live)
            rel = self._release(d)
            dup = (wiring.fence_apply("", self.FENCE, ["a"], style=self.STYLE)
                   + wiring.fence_apply("", self.FENCE, ["b"], style=self.STYLE))   # two blocks → malformed
            with module_manager._redirect_root(live):
                self._write_local(live, dup)
                out = module_manager._merge_claude_floor(rel)
                after = module_manager.validate.read(os.path.join(live, "CLAUDE.md"))
        self.assertEqual(out, "degraded")
        self.assertEqual(after, dup)                           # left unchanged, no crash


class TestRemoveReversesClaudeFloor(unittest.TestCase):
    """Clean engine removal block-reverses the root CLAUDE.md `floor` fence: a brownfield operator's own
    content is KEPT (only the engine block is removed), not clobbered wholesale — the data-loss guard the
    `outside`-set carve-out + the reversal provide together (#234 6a). The all-engine-delete case is covered
    by the shipped removal demo."""

    def _fakes(self):
        import bootstrap
        def opener(branch, title, body):
            return {"number": 0, "html_url": "(fixture)"}
        def transport(method, path, body=None):
            if method == "GET" and path.endswith("/rulesets"):
                return (200, [{"id": 1, "name": bootstrap.ENGINE_RULESET_NAME}], {})
            return (200 if method == "PUT" else 204 if method == "DELETE" else 200, None, {})
        return opener, transport

    def test_brownfield_claude_keeps_operator_content_on_remove(self):
        opener, transport = self._fakes()
        top, bottom = "# My product\n\nintro\n\n", "\n## More\n\ntail\n"
        with tempfile.TemporaryDirectory() as d:
            with module_manager._redirect_root(d):
                module_manager._build_remove_fixture(d)
                block = wiring.fence_apply("", module_manager._FLOOR_FENCE, ["# engine floor"],
                                           style=wiring.MD_FENCE)
                with open(os.path.join(d, "CLAUDE.md"), "w", encoding="utf-8") as fh:
                    fh.write(top + block + bottom)
                module_manager.remove_engine(opener=opener, transport=transport, choice="keep",
                                             announce=lambda m: None)
                after = module_manager.validate.read(os.path.join(d, "CLAUDE.md"))
        self.assertIn(top, after)                              # operator content kept
        self.assertIn(bottom, after)
        self.assertNotIn("# engine floor", after)             # the engine block body is gone
        self.assertNotIn("BEGIN engine-managed block: floor", after)


class TestRemoveReversesFoundationIgnores(unittest.TestCase):
    """#409 U14: clean engine removal block-reverses the root `.gitignore` foundation fence — the operator's
    own ignore lines are KEPT (only the engine `foundation-ignores` block is removed), never wholesale-deleted
    (`.gitignore` is excluded from remove_engine's delete set + block-reversed, like CODEOWNERS/CLAUDE.md)."""

    def _fakes(self):
        import bootstrap
        def opener(branch, title, body):
            return {"number": 0, "html_url": "(fixture)"}
        def transport(method, path, body=None):
            if method == "GET" and path.endswith("/rulesets"):
                return (200, [{"id": 1, "name": bootstrap.ENGINE_RULESET_NAME}], {})
            return (200 if method == "PUT" else 204 if method == "DELETE" else 200, None, {})
        return opener, transport

    def test_remove_keeps_operator_ignore_lines_and_drops_only_the_engine_block(self):
        opener, transport = self._fakes()
        with tempfile.TemporaryDirectory() as d:
            with module_manager._redirect_root(d):
                module_manager._build_remove_fixture(d)
                # an operator's own ignore line + the engine foundation fence in one .gitignore
                with open(os.path.join(d, ".gitignore"), "w", encoding="utf-8") as fh:
                    fh.write("# mine\nnode_modules/\n")
                wiring.apply_foundation_ignores(os.path.join(d, ".gitignore"))
                module_manager.remove_engine(opener=opener, transport=transport, choice="keep",
                                             announce=lambda m: None)
                after = module_manager.validate.read(os.path.join(d, ".gitignore"))
        self.assertIn("node_modules/", after)                      # operator content kept
        self.assertNotIn("foundation-ignores", after)              # the engine block is gone
        self.assertNotIn(".engine/.venv/", after)
        self.assertNotIn("BEGIN engine-managed block: foundation-ignores", after)


class TestUpgradeSurfacesClaudeFloor(unittest.TestCase):
    """The CLAUDE.md merge outcome must reach the operator's consent surface — the upgrade PR body AND the
    console render — exactly as the CODEOWNERS outcome does. A degraded/skipped floor merge is an engine edit
    (or a silent non-update) of a file the operator co-owns and must never be invisible at the merge gate."""

    def _body(self, cf):
        return module_manager._upgrade_pr_body({"base": "0.1.0"}, {"base": "0.2.0"}, {"claude_floor": cf})

    def test_pr_body_names_the_merged_outcome(self):
        self.assertIn("working guide", self._body("merged").lower())

    def test_pr_body_names_the_not_updated_outcomes(self):
        self.assertIn("looked damaged", self._body("degraded"))
        self.assertIn("no engine marked block", self._body("skipped-no-section"))

    def test_render_prints_the_degraded_outcome(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            module_manager._render_upgrade({"from": {"base": "0.1.0"}, "to": {"base": "0.2.0"},
                                            "claude_floor": "degraded"})
        self.assertIn("working guide", buf.getvalue().lower())

    def test_pr_body_surfaces_the_foundation_ignores_reassertion(self):
        # #409 U14 (deliverable-gate nit 2): the .gitignore fence re-assert gets an operator-facing line on
        # upgrade, like its CODEOWNERS / CLAUDE.md siblings — not just a raw git diff.
        body = module_manager._upgrade_pr_body({"base": "0.1.0"}, {"base": "0.2.0"},
                                               {"foundation_ignores": {"status": "written"}})
        self.assertIn("ignore list", body.lower())
        # an unchanged ("already") re-assert stays silent — nothing changed to disclose
        quiet = module_manager._upgrade_pr_body({"base": "0.1.0"}, {"base": "0.2.0"},
                                                {"foundation_ignores": {"status": "already"}})
        self.assertNotIn("ignore list", quiet.lower())


class TestLifecycleRendersCarryCoherenceWarrant(unittest.TestCase):
    """#400 F5: the add/remove/upgrade renders must carry the structural-not-fitness coherence warrant, so an
    operator never misreads a bare "consistent" line as "the module works." The warrant is single-homed in
    module_coherence.COHERENCE_WARRANT (reused, not re-typed) and, matching the standalone CLI's _print_report,
    prints on EVERY non-refused report — including the upgrade path that opens a review PR (the dominant case,
    which prints neither the hard-findings nor the staged-consistent line)."""

    _WARRANT_TELL = "not a fitness check"  # a distinctive phrase from COHERENCE_WARRANT

    def _render(self, fn, result):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fn(result)
        return buf.getvalue()

    def test_the_tell_is_actually_in_the_single_homed_warrant(self):
        # guards against the warrant text drifting out from under the phrase these tests assert on
        self.assertIn(self._WARRANT_TELL, module_coherence.COHERENCE_WARRANT)

    def test_remove_render_carries_the_warrant(self):
        out = self._render(module_manager._render_remove, {"module_id": "demo"})
        self.assertIn("consistent", out)
        self.assertIn(self._WARRANT_TELL, out)

    def test_add_render_carries_the_warrant(self):
        out = self._render(module_manager._render_add, {"module_id": "demo", "version": "1.0.0"})
        self.assertIn(self._WARRANT_TELL, out)

    def test_upgrade_staged_path_carries_the_warrant(self):
        out = self._render(module_manager._render_upgrade,
                           {"from": {"base": "0.1.0"}, "to": {"base": "0.2.0"}})
        self.assertIn("staged and consistent", out)
        self.assertIn(self._WARRANT_TELL, out)

    def test_upgrade_PR_path_carries_the_warrant(self):
        # the dominant upgrade case opens a PR and prints neither branch — the warrant must still appear
        out = self._render(module_manager._render_upgrade,
                           {"from": {"base": "0.1.0"}, "to": {"base": "0.2.0"}, "pr": {"number": 42}})
        self.assertIn("#42", out)
        self.assertIn(self._WARRANT_TELL, out)

    def test_refused_render_does_not_carry_the_warrant(self):
        # a refused op checked nothing — no "consistent" claim, so no warrant to qualify
        out = self._render(module_manager._render_remove,
                           {"module_id": "demo", "refused": True, "reason": "does not exist"})
        self.assertNotIn(self._WARRANT_TELL, out)


class TestFrozenCheckNames(unittest.TestCase):
    """The engine CI check names are a FROZEN contract across versions — an upgrade/migration may never
    rename them (a renamed required check 'waits forever' and deadlocks every pull request; provisioning
    §"The engine CI check's status name is a frozen contract"). Changing this is a guardrail-weakening
    change, caught here and at the guard."""

    def test_required_check_names_are_frozen(self):
        import protection_guard
        self.assertEqual(protection_guard.REQUIRED_CHECKS, ["engine-ci", "engine-guard"])

    def test_the_overlay_replaces_the_engine_owned_workflow_files(self):
        # the overlay replaces the engine-owned CI workflows wholesale (a new version ships its own), so the
        # frozen names live in those files; the invariant is the release must keep the names unchanged
        self.assertIn(".github/workflows/engine-ci.yml", module_manager.FOUNDATION_CODE)
        self.assertIn(".github/workflows/engine-guard.yml", module_manager.FOUNDATION_CODE)


class TestFoundationInfra(unittest.TestCase):
    """FOUNDATION_INFRA is the single source; NAMED_INFRA + FOUNDATION_CODE derive from it (core 25c PR-3)."""

    def test_named_infra_is_the_engine_subset_a_pure_refactor(self):
        self.assertEqual(module_coherence.NAMED_INFRA,
                         {p for p in module_coherence.FOUNDATION_INFRA if p.startswith(".engine/")})
        # identical to the historical literal — no membership change to the ownership-walk carve-out
        self.assertEqual(module_coherence.NAMED_INFRA,
                         {".engine/engine.json", ".engine/pyproject.toml", ".engine/uv.lock"})

    def test_foundation_code_is_foundation_infra_minus_manifest_codeowners_claude_and_gitignore(self):
        expected = tuple(p for p in module_coherence.FOUNDATION_INFRA
                         if p not in (module_coherence.ENGINE_MANIFEST_REL, ".github/CODEOWNERS",
                                      "CLAUDE.md", ".gitignore"))
        self.assertEqual(module_manager.FOUNDATION_CODE, expected)
        # the issue templates are now in the overlay set; the manifest, CODEOWNERS, root CLAUDE.md, and root
        # .gitignore are excluded — CLAUDE.md/.gitignore carry a keyed engine fence re-asserted locally
        # (_merge_claude_floor / apply_foundation_ignores), not fetched-and-replaced wholesale (#234 6a /
        # #409 U14), so a release's file never overlays an adopter's own content
        self.assertIn(".github/ISSUE_TEMPLATE/*.md", module_manager.FOUNDATION_CODE)
        self.assertNotIn(".engine/engine.json", module_manager.FOUNDATION_CODE)
        self.assertNotIn(".github/CODEOWNERS", module_manager.FOUNDATION_CODE)
        self.assertNotIn("CLAUDE.md", module_manager.FOUNDATION_CODE)
        self.assertNotIn(".gitignore", module_manager.FOUNDATION_CODE)

    def test_engine_owned_paths_unions_provides_and_foundation_concretely(self):
        with tempfile.TemporaryDirectory() as d:
            with module_manager._redirect_root(d):
                module_manager._build_fixture(d)             # base + optx, provides two tool files
                os.makedirs(os.path.join(d, ".github"))
                with open(os.path.join(d, "CLAUDE.md"), "w") as fh:
                    fh.write("# floor\n")
                paths = module_coherence.engine_owned_paths(module_coherence.discover_manifests())
                self.assertIn(".engine/tools/base_tool.py", paths)        # a provides-claimed file
                self.assertIn(".engine/engine.json", paths)               # a foundation member
                self.assertIn("CLAUDE.md", paths)                         # a non-.engine foundation member
                # no glob literal leaks in — paths are concrete
                self.assertFalse(any("*" in p for p in paths))

    def test_codeowners_path_set_adds_self_only_in_the_codeowners_helper(self):
        # codeowners_path_set = engine_owned_paths + the CODEOWNERS self-add; the self-add lives ONLY here,
        # never in engine_owned_paths (whose other consumers must not carry CODEOWNERS' self-ownership).
        with tempfile.TemporaryDirectory() as d:
            with module_manager._redirect_root(d):
                module_manager._build_fixture(d)
                base_paths = module_coherence.engine_owned_paths(module_coherence.discover_manifests())
                co_paths = module_coherence.codeowners_path_set()
                self.assertNotIn(".github/CODEOWNERS", base_paths)        # NOT in the bare engine set
                self.assertIn(".github/CODEOWNERS", co_paths)             # self-owned in the CODEOWNERS set
                self.assertIn(".engine/tools/base_tool.py", co_paths)     # still unions the provides set
                self.assertEqual(co_paths, sorted(set(base_paths) | {".github/CODEOWNERS"}))


class TestIssueTemplateOverlay(unittest.TestCase):
    """tech plan-gate SERIOUS: the overlay must COPY the issue templates, not merely list a glob string
    (the foundation loop globs the release tree; a literal isfile on '*.md' would silently drop them)."""

    def test_overlay_copies_issue_templates_from_a_release(self):
        with tempfile.TemporaryDirectory() as d:
            release, live = os.path.join(d, "release"), os.path.join(d, "live")
            os.makedirs(os.path.join(release, ".engine", "modules", "base"))
            os.makedirs(os.path.join(release, ".github", "ISSUE_TEMPLATE"))
            module_manager._write_json(
                os.path.join(release, ".engine", "modules", "base", "manifest.json"),
                {"id": "base", "version": "0.0.0", "status": "required", "provides": {}, "depends": {}})
            for name in ("bug.md", "feature.md"):
                with open(os.path.join(release, ".github", "ISSUE_TEMPLATE", name), "w") as fh:
                    fh.write("template\n")
            os.makedirs(live)
            with module_manager._redirect_root(live):
                copied, _ = module_manager._overlay_engine_code(release, ["base"])
            self.assertIn(".github/ISSUE_TEMPLATE/bug.md", copied)
            self.assertIn(".github/ISSUE_TEMPLATE/feature.md", copied)
            self.assertTrue(os.path.isfile(os.path.join(live, ".github", "ISSUE_TEMPLATE", "bug.md")))


class TestOverlayExclude(unittest.TestCase):
    """#234 6b: the brownfield arrival passes `exclude` so an engine-exclusive path the operator chose to keep
    (a class-1 'leave-as-is') is NOT overwritten by the incoming engine file — the engine coexists around it."""

    def _release(self, release):
        os.makedirs(os.path.join(release, ".engine", "modules", "base"))
        os.makedirs(os.path.join(release, ".engine", "tools"))
        module_manager._write_json(
            os.path.join(release, ".engine", "modules", "base", "manifest.json"),
            {"id": "base", "version": "0.0.0", "status": "required",
             "provides": {"tool": [".engine/tools/keep.py", ".engine/tools/other.py"]}, "depends": {}})
        for name in ("keep.py", "other.py"):
            with open(os.path.join(release, ".engine", "tools", name), "w") as fh:
                fh.write("# engine version\n")

    def test_excluded_path_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            release, live = os.path.join(d, "release"), os.path.join(d, "live")
            self._release(release)
            os.makedirs(os.path.join(live, ".engine", "tools"))
            with open(os.path.join(live, ".engine", "tools", "keep.py"), "w") as fh:
                fh.write("# the product's own file\n")    # an operator file at an engine path (class-1)
            with module_manager._redirect_root(live):
                copied, _ = module_manager._overlay_engine_code(
                    release, ["base"], exclude={".engine/tools/keep.py"})
            self.assertNotIn(".engine/tools/keep.py", copied)            # kept, not overwritten
            self.assertIn(".engine/tools/other.py", copied)             # the rest still overlaid
            with open(os.path.join(live, ".engine", "tools", "keep.py")) as fh:
                self.assertEqual(fh.read(), "# the product's own file\n")


class TestRemoveEngine(unittest.TestCase):
    """Clean whole-engine removal (core 25c PR-3): de-bootstrap first, reverse all wires, delete every engine
    file (including the .github/ ones, unlike per-module remove), open a reviewed pull request. All four
    boundaries injected; the REAL reversal / delete-set / de-bootstrap-decision logic runs."""

    def _fakes(self, ruleset_present=True):
        import bootstrap
        prs, methods = [], []

        def opener(branch, title, body):
            prs.append((branch, title, body))
            return {"number": 5, "html_url": "http://x/5"}

        def transport(method, path, body=None):
            methods.append(method)
            if method == "GET" and path.endswith("/rulesets"):
                return (200, ([{"id": 3, "name": bootstrap.ENGINE_RULESET_NAME}]
                              if ruleset_present else []), {})
            if method == "PUT":
                return (200, {"id": 3}, {})
            if method == "DELETE":
                return (204, None, {})
            return (200, None, {})
        return opener, transport, prs, methods

    def _fixture_with_github(self, d):
        module_manager._build_fixture(d)                  # base + optx (+ optx wires applied)
        os.makedirs(os.path.join(d, ".github", "workflows"))
        with open(os.path.join(d, ".github", "workflows", "engine-ci.yml"), "w") as fh:
            fh.write("ci\n")
        with open(os.path.join(d, "CLAUDE.md"), "w", encoding="utf-8") as fh:
            # An all-engine greenfield CLAUDE.md is the floor wrapped in the engine fence (6a); removal
            # block-reverses it → whitespace-only → the file is deleted (nothing operator-owned to keep).
            fh.write(wiring.fence_apply("", module_manager._FLOOR_FENCE, ["# floor"], style=wiring.MD_FENCE))

    def test_reverses_wires_deletes_all_engine_files_and_opens_a_pr(self):
        opener, transport, prs, _ = self._fakes(True)
        with tempfile.TemporaryDirectory() as d:
            with module_manager._redirect_root(d):
                self._fixture_with_github(d)
                r = module_manager.remove_engine(opener=opener, transport=transport,
                                                 choice="keep", announce=lambda m: None)
                engine_gone = not os.path.isdir(os.path.join(d, ".engine"))
                ci_gone = not os.path.isfile(os.path.join(d, ".github", "workflows", "engine-ci.yml"))
                claude_gone = not os.path.isfile(os.path.join(d, "CLAUDE.md"))
        self.assertEqual(r["de_bootstrap"]["status"], "kept")
        self.assertTrue(r["reversed"], "optx's wires should be reversed")
        self.assertTrue(engine_gone and ci_gone and claude_gone)
        self.assertEqual(len(prs), 1)
        self.assertIsNotNone(r["pr"])

    def test_github_member_is_in_the_delete_set_unlike_per_module_remove(self):
        # whole-engine removal deletes the .github/ foundation files + root CLAUDE.md too — these are
        # foundation infra, NOT any module's `provides`, so per-module remove() (which deletes only the
        # sole-owned files a module provides, at any path since #409 U16) never touches them
        opener, transport, _, _ = self._fakes(True)
        with tempfile.TemporaryDirectory() as d:
            with module_manager._redirect_root(d):
                self._fixture_with_github(d)
                r = module_manager.remove_engine(opener=opener, transport=transport,
                                                 choice="keep", announce=lambda m: None)
        self.assertIn(".github/workflows/engine-ci.yml", r["deleted"])
        self.assertIn("CLAUDE.md", r["deleted"])
        self.assertIn(".engine/", r["deleted"])

    def test_codeowners_engine_block_removed_operator_rules_kept(self):
        opener, transport, _, _ = self._fakes(True)
        with tempfile.TemporaryDirectory() as d:
            with module_manager._redirect_root(d):
                self._fixture_with_github(d)
                co = wiring.render_codeowners("# mine\n/src/ @me\n", [".engine/engine.json"], "@me")
                with open(os.path.join(d, ".github", "CODEOWNERS"), "w") as fh:
                    fh.write(co)
                module_manager.remove_engine(opener=opener, transport=transport,
                                             choice="keep", announce=lambda m: None)
                with open(os.path.join(d, ".github", "CODEOWNERS")) as fh:
                    text = fh.read()
        self.assertIn("/src/ @me", text)             # operator rule kept
        self.assertNotIn("engine.json", text)        # engine block removed

    def test_drop_choice_deletes_the_safety_rule(self):
        opener, transport, _, methods = self._fakes(True)
        with tempfile.TemporaryDirectory() as d:
            with module_manager._redirect_root(d):
                self._fixture_with_github(d)
                r = module_manager.remove_engine(opener=opener, transport=transport,
                                                 choice="drop", announce=lambda m: None)
        self.assertEqual(r["de_bootstrap"]["status"], "dropped")
        self.assertIn("DELETE", methods)

    def test_frozen_check_names_untouched_by_removal(self):
        import protection_guard
        before = list(protection_guard.REQUIRED_CHECKS)
        opener, transport, _, _ = self._fakes(True)
        with tempfile.TemporaryDirectory() as d:
            with module_manager._redirect_root(d):
                self._fixture_with_github(d)
                module_manager.remove_engine(opener=opener, transport=transport,
                                             choice="keep", announce=lambda m: None)
        self.assertEqual(protection_guard.REQUIRED_CHECKS, before)

    def test_remove_engine_demo_passes(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(module_manager.remove_engine_demo())


class TestBackupSeamResolution(unittest.TestCase):
    """_resolve_backup_seam: now that memory ships the seam, "a backup is available" means the mechanism is
    installed AND a vault is configured — so memory-installed-but-no-vault resolves to None (refuse the data
    migration cleanly) rather than handing back a seam that fails mid-snapshot. An injected backup always wins."""

    def test_no_vault_configured_resolves_to_no_seam(self):
        import memory
        orig = memory.migration_backup_available
        memory.migration_backup_available = lambda: False
        try:
            self.assertIsNone(module_manager._resolve_backup_seam(None))
        finally:
            memory.migration_backup_available = orig

    def test_vault_configured_resolves_to_the_live_seam(self):
        import memory
        orig = memory.migration_backup_available
        memory.migration_backup_available = lambda: True
        try:
            self.assertIs(module_manager._resolve_backup_seam(None), memory.snapshot_for_migration)
        finally:
            memory.migration_backup_available = orig

    def test_an_injected_backup_wins_over_resolution(self):
        sentinel = lambda store, ver: {"ok": True}
        self.assertIs(module_manager._resolve_backup_seam(sentinel), sentinel)


if __name__ == "__main__":
    unittest.main()
