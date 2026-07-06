#!/usr/bin/env python3
"""Unit tests for release_cut — the version-decision + manifest-write core.

Covered: sentinel-aware version ordering; first-cut vs diff classification (add / remove / new
migration => the mechanical floor); raise-only refusal; the atomic, shape-preserving write (only
version values change, home_repository byte-preserved); the packages<->manifest split-brain guard;
and rollback-on-validation-failure (nothing written)."""
import json
import os
import shutil
import tempfile
import unittest

import validate
import module_coherence
import release_cut as rc


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
        f.write("\n")


def _module(mid, ver="0.0.0-dev", migrations=None):
    m = {"id": mid, "version": ver, "status": "required", "provides": {}}
    if migrations:
        m["migrations"] = migrations
    return m


class _Tree:
    """A temp engine tree (engine.json + module manifests) with validate.ROOT pointed at it."""
    def __init__(self, modules, home="acme/engine-home"):
        self.root = tempfile.mkdtemp()
        engine = {"engine_release": "0.0.0-dev",
                  "packages": {mid: m["version"] for mid, m in modules.items()},
                  "identity": "solo", "home_repository": home}
        _write(os.path.join(self.root, ".engine", "engine.json"), engine)
        for mid, m in modules.items():
            _write(os.path.join(self.root, ".engine", "modules", mid, "manifest.json"), m)

    def __enter__(self):
        self._saved = (validate.ROOT, validate.ENGINE_DIR)
        validate.ROOT = self.root
        validate.ENGINE_DIR = os.path.join(self.root, ".engine")
        return self

    def __exit__(self, *exc):
        validate.ROOT, validate.ENGINE_DIR = self._saved
        shutil.rmtree(self.root, ignore_errors=True)

    def engine_text(self):
        with open(os.path.join(self.root, ".engine", "engine.json"), encoding="utf-8") as f:
            return f.read()

    def engine(self):
        return json.loads(self.engine_text())

    def module_version(self, mid):
        p = os.path.join(self.root, ".engine", "modules", mid, "manifest.json")
        return validate.load_json(p)["version"]


def _baseline_tree(modules):
    """A throwaway release-tree root carrying the given baseline module manifests."""
    root = tempfile.mkdtemp()
    for mid, m in modules.items():
        _write(os.path.join(root, ".engine", "modules", mid, "manifest.json"), m)
    return root


class VersionOrdering(unittest.TestCase):
    def test_sentinel_sorts_below_any_release(self):
        self.assertTrue(rc._strictly_greater("0.1.0", "0.0.0-dev"))
        self.assertTrue(rc._strictly_greater("1.0.0", "0.0.0-dev"))
        self.assertTrue(rc._strictly_greater("0.0.0", "0.0.0-dev"))  # a real release outranks the dev sentinel

    def test_equal_and_lower_refused(self):
        self.assertFalse(rc._strictly_greater("0.1.0", "0.1.0"))
        self.assertFalse(rc._strictly_greater("0.0.9", "0.1.0"))
        self.assertFalse(rc._strictly_greater("0.0.0-dev", "0.0.0-dev"))  # sentinel is not > itself

    def test_normal_increments(self):
        self.assertTrue(rc._strictly_greater("0.1.1", "0.1.0"))
        self.assertTrue(rc._strictly_greater("1.0.0", "0.9.9"))

    def test_prerelease_sorts_below_its_release(self):
        # a pre-release must NOT be taken as greater than its own release (else raise-only accepts a downgrade)
        self.assertFalse(rc._strictly_greater("1.0.0-rc1", "1.0.0"))
        self.assertTrue(rc._strictly_greater("1.0.0", "1.0.0-rc1"))
        self.assertTrue(rc._strictly_greater("1.0.0-rc1", "0.9.0"))       # higher numbers still win
        self.assertFalse(rc._strictly_greater("1.0.0-rc2", "1.0.0-rc1"))  # conservative: rc progression refused

    def test_valid_version_grammar(self):
        self.assertTrue(rc._valid_version("1.2.0"))
        self.assertTrue(rc._valid_version("1.0.0-rc1"))
        self.assertTrue(rc._valid_version("0.0.0-dev"))
        self.assertFalse(rc._valid_version("99999.total-garbage;rm -rf ~"))
        self.assertFalse(rc._valid_version("v1.2.0"))
        self.assertFalse(rc._valid_version(""))


class Classify(unittest.TestCase):
    def test_first_cut_derives_no_floor(self):
        with _Tree({"core": _module("core"), "qa-review": _module("qa-review")}):
            p = rc.classify(rc.Baseline(None, True, "first cut"), None)
        self.assertEqual(p["mode"], "first-cut")
        self.assertEqual(p["engine_floor_level"], "none")
        self.assertIn("First release", p["change_inventory"][0])

    def test_diff_add_remove_migration_floor(self):
        # live tree: core (with a new migration) + product-design (new); baseline: core (no migration) + legacy
        live = {"core": _module("core", migrations={"0.2.0": {"description": "d", "run": "r", "kind": "config"}}),
                "product-design": _module("product-design")}
        base = _baseline_tree({"core": _module("core"), "legacy": _module("legacy")})
        try:
            with _Tree(live):
                p = rc.classify(rc.Baseline("v0.0.9", False, "diff"), base)
            inv = " ".join(p["change_inventory"])
            self.assertEqual(p["mode"], "diff")
            self.assertEqual(p["engine_floor_level"], "major")           # a removal => major
            self.assertIn("Added the 'product-design'", inv)
            self.assertIn("Removed the 'legacy'", inv)
            self.assertIn("core", p["package_floor"])                    # new migration => package floor
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_diff_no_signal_notes_patch_and_contract_silent_caveat(self):
        mods = {"core": _module("core")}
        base = _baseline_tree({"core": _module("core")})
        try:
            with _Tree(mods):
                p = rc.classify(rc.Baseline("v0.0.9", False, "diff"), base)
            self.assertEqual(p["engine_floor_level"], "none")
            self.assertIn("no structural signal", " ".join(p["change_inventory"]))
        finally:
            shutil.rmtree(base, ignore_errors=True)


class Apply(unittest.TestCase):
    def test_raise_only_refuses_non_increase(self):
        with _Tree({"core": _module("core")}):
            r = rc.apply("0.0.0-dev", "0.0.0-dev", {}, None, dry_run=True)
        self.assertFalse(r["applied"])
        self.assertEqual(r["reason"], "raise-only")

    def test_apply_writes_versions_and_preserves_home_repository(self):
        with _Tree({"core": _module("core"), "qa-review": _module("qa-review")}) as t:
            before_home_line = [ln for ln in t.engine_text().splitlines() if "home_repository" in ln][0]
            r = rc.apply("0.1.0", "0.1.0", {}, None, dry_run=False)
            self.assertTrue(r["applied"])
            self.assertEqual(t.engine()["engine_release"], "0.1.0")
            self.assertEqual(t.engine()["packages"]["core"], "0.1.0")
            self.assertEqual(t.module_version("core"), "0.1.0")
            self.assertEqual(t.module_version("qa-review"), "0.1.0")
            # home_repository line is byte-identical (would otherwise trip weakening_guard, D-281/D-282)
            after_home_line = [ln for ln in t.engine_text().splitlines() if "home_repository" in ln][0]
            self.assertEqual(before_home_line, after_home_line)
            self.assertEqual(t.engine()["identity"], "solo")           # unrelated keys preserved

    def test_apply_dry_run_writes_nothing(self):
        with _Tree({"core": _module("core")}) as t:
            rc.apply("0.1.0", "0.1.0", {}, None, dry_run=True)
            self.assertEqual(t.engine()["engine_release"], "0.0.0-dev")
            self.assertEqual(t.module_version("core"), "0.0.0-dev")

    def test_below_confirmed_floor_refused(self):
        # target 0.1.5 is ABOVE the current 0.1.0 (so raise-only passes) but BELOW the confirmed floor
        # 0.2.0 — this must be caught by the below-floor guard specifically, not raise-only.
        with _Tree({"core": _module("core", ver="0.1.0")}):
            proposal = {"package_floor": {"core": "0.2.0"}}
            r = rc.apply("0.2.0", None, {"core": "0.1.5"}, proposal, dry_run=True)
        self.assertFalse(r["applied"])
        self.assertEqual(r["reason"], "below-confirmed-floor")

    def test_at_or_above_confirmed_floor_passes(self):
        with _Tree({"core": _module("core", ver="0.1.0")}):
            proposal = {"package_floor": {"core": "0.2.0"}}
            r = rc.apply("0.2.0", None, {"core": "0.2.0"}, proposal, dry_run=True)   # meets the floor
        self.assertEqual(r["reason"], "dry-run")   # would apply

    def test_invalid_version_refused(self):
        with _Tree({"core": _module("core")}):
            r = rc.apply("99999.total-garbage;rm -rf ~", "99999.total-garbage;rm -rf ~", {}, None, dry_run=True)
        self.assertFalse(r["applied"])
        self.assertEqual(r["reason"], "invalid-version")

    def test_pre_write_validation_failure_writes_nothing(self):
        # a validation error fires BEFORE any file is staged — the pre-write refusal path
        with _Tree({"core": _module("core")}) as t:
            orig = rc._schema_ok
            rc._schema_ok = lambda inst, path: ["forced error"]
            try:
                r = rc.apply("0.1.0", "0.1.0", {}, None, dry_run=False)
            finally:
                rc._schema_ok = orig
            self.assertFalse(r["applied"])
            self.assertEqual(r["reason"], "validation")
            self.assertEqual(t.engine()["engine_release"], "0.0.0-dev")   # untouched
            self.assertEqual(t.module_version("core"), "0.0.0-dev")

    def test_swap_failure_rolls_back_all_files(self):
        # a write error mid-swap must roll back the files already swapped — no split-brain left on disk
        with _Tree({"core": _module("core"), "qa-review": _module("qa-review")}) as t:
            real_replace = rc.os.replace
            calls = {"n": 0}

            def flaky(src, dst):
                calls["n"] += 1
                if calls["n"] == 2:            # engine.json swaps (1), the first manifest swap (2) fails
                    raise OSError("disk full")
                return real_replace(src, dst)

            rc.os.replace = flaky
            try:
                with self.assertRaises(RuntimeError):
                    rc.apply("0.1.0", "0.1.0", {}, None, dry_run=False)
            finally:
                rc.os.replace = real_replace
            # everything is back at the sentinel — engine.json was restored, no manifest half-written
            self.assertEqual(t.engine()["engine_release"], "0.0.0-dev")
            self.assertEqual(t.engine()["packages"]["core"], "0.0.0-dev")
            self.assertEqual(t.module_version("core"), "0.0.0-dev")
            self.assertEqual(t.module_version("qa-review"), "0.0.0-dev")


if __name__ == "__main__":
    unittest.main()
