#!/usr/bin/env python3
"""Self-tests for slice 25b — the module manager: `remove` (refusals + manifest-derived reversal),
the group-scoped uv-sync derivation, and the reverse-dependency / required-foundation guards.

Run: uv run --directory .engine -- python -m unittest discover -s tools -p 'test_*.py'

The pure refusal policy and the derivation are tested directly (fixture manifest lists / a temp
pyproject — no disk mutation); the live `remove` mutation glue is exercised end-to-end by run_demo()
against a throwaway fixture engine (real reversal, real coherence, real re-derivation). The
deliverable-gate cold review attests each test's assertion matches its name.
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
    """The two real modules (both required) — read-only, no mutation."""

    def test_remove_core_is_refused_naming_validators_core(self):
        plan = module_manager.plan_remove("core")
        self.assertTrue(plan["refused"])
        self.assertIn("validators-core", plan["reason"])   # reverse-dependency refusal

    def test_remove_validators_core_is_refused_as_required(self):
        plan = module_manager.plan_remove("validators-core")
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


if __name__ == "__main__":
    unittest.main()
