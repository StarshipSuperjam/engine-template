#!/usr/bin/env python3
"""Self-tests for the module manager — slice 25b `remove` + the group-scoped uv-sync derivation, slice 25c
PR-1 `add` (fetch/overlay), and slice 25c PR-2 the engine `upgrade`/updater + the migrations machinery
(select/run, the no-backup guard, the version-stamp check, and the frozen-check-name invariant).

Run: uv run --directory .engine -- python -m unittest discover -s tools -p 'test_*.py'

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
            seam = lambda store, ver: (calls.append((store, ver)) or {"ok": True})
            sel = [{"module_id": "m", "version": "0.2.0", "run": "migrations/dd.py", "kind": "data"}]
            res = module_manager.run_migrations(sel, {"m": "0.0.0"}, "v2", module_dir=mdir, backup=seam)
            self.assertEqual(res["ran"], ["m -> 0.2.0 (data)"])
            self.assertEqual(calls, [("store", "v2")])         # the backup was taken (before the body ran)
            with open(marker) as fh:
                self.assertEqual(fh.read(), "v2")              # the migration stamped the engine version


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
                                             opener=lambda **k: {"number": 1}, backup=lambda *a: {"ok": 1})
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
                                             opener=lambda **k: {"number": 1}, backup=lambda *a: {"ok": 1})
                after = module_manager.validate.read(co_path)
            self.assertFalse(res["refused"])
            self.assertEqual(res["codeowners"], "degraded")
            self.assertEqual(after, before)                             # untouched on degrade


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

    def test_foundation_code_is_foundation_infra_minus_manifest_and_codeowners(self):
        expected = tuple(p for p in module_coherence.FOUNDATION_INFRA
                         if p not in (module_coherence.ENGINE_MANIFEST_REL, ".github/CODEOWNERS"))
        self.assertEqual(module_manager.FOUNDATION_CODE, expected)
        # the issue templates are now in the overlay set; the manifest + CODEOWNERS are excluded
        self.assertIn(".github/ISSUE_TEMPLATE/*.md", module_manager.FOUNDATION_CODE)
        self.assertNotIn(".engine/engine.json", module_manager.FOUNDATION_CODE)
        self.assertNotIn(".github/CODEOWNERS", module_manager.FOUNDATION_CODE)

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
        with open(os.path.join(d, "CLAUDE.md"), "w") as fh:
            fh.write("# floor\n")

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
        # per-module remove() deletes only under .engine/; whole-engine removal deletes the .github/ files too
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
