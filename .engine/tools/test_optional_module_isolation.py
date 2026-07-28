#!/usr/bin/env python3
"""Regression: an ALWAYS-PRESENT engine tool must never hard-import a DECLINABLE module at module top level.

Motivating bug: `.engine/tools/test_conformance_sweep.py` (core-owned, so it travels to every deployment) did
an unconditional `from product_design import obligation_matrix`. product-design is an OPTIONAL module, so on a
deployment that declined it the import raised ModuleNotFoundError at unittest COLLECTION time and aborted the
whole self-test suite before any test ran. This locks the class so no always-present tool can reintroduce it
for ANY declinable module — the tool package of an `optional` or `default-on` module (e.g. `product_design`
or `memory.semantic`).

Distinct in lifecycle from `engine/check/first-run-reference-closure`, which guards references to first-run-
REMOVED assets; this guards module-top-level imports of DECLINED-optional-module packages (and it subtracts the
first-run-removed set below, since those files do not ship to a deployment). The invariant is manifest-driven
and PURE AST — it parses source and matches import NAMES, never importing the packages it reasons about, so it
stays safe on a declined deployment. It is primarily a home-repo introduction guard (fully powered where every
module manifest is present); on a declined deployment the collection-time crash itself remains the backstop.
"""
from __future__ import annotations

import ast
import glob
import json
import os
import subprocess
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))          # .engine/tools
_ENGINE = os.path.dirname(_HERE)                            # .engine
_ROOT = os.path.dirname(_ENGINE)                            # repo root
_MODULES = os.path.join(_ENGINE, "modules")

# Module statuses whose tool packages can be DECLINED at setup — absent on some deployments.
_DECLINABLE_STATUS = {"optional", "default-on"}


def _module_manifests() -> list:
    """Parsed module manifests; one that cannot be read/parsed is skipped — a malformed manifest must degrade
    this guard to checking fewer packages, never crash the suite it exists to protect."""
    out = []
    for path in sorted(glob.glob(os.path.join(_MODULES, "*", "manifest.json"))):
        try:
            with open(path, encoding="utf-8") as fh:
                out.append(json.load(fh))
        except (OSError, ValueError):
            continue
    return out


def _py_tool_globs(manifest: dict) -> list:
    return [g for g in manifest.get("provides", {}).get("tool", [])
            if isinstance(g, str) and g.startswith(".engine/tools/") and g.endswith(".py")]


def _glob_to_package(tool_glob: str) -> "str | None":
    """`.engine/tools/product_design/*.py` -> `product_design`; `.engine/tools/memory/semantic/*.py` ->
    `memory.semantic`; a top-level file glob `.engine/tools/*.py` owns no importable subpackage -> None."""
    parts = tool_glob[len(".engine/tools/"):].split("/")
    pkg_parts = parts[:-1]                       # drop the trailing filename-glob component
    return ".".join(pkg_parts) if pkg_parts else None


def _declinable_packages(manifests: list) -> set:
    pkgs = set()
    for m in manifests:
        if m.get("status") in _DECLINABLE_STATUS:
            for g in _py_tool_globs(m):
                pkg = _glob_to_package(g)
                if pkg:
                    pkgs.add(pkg)
    return pkgs


def _first_run_removed() -> tuple:
    """The files and directories the instantiator removes at first-run (they do NOT ship to a deployment), read
    from the same committed manifest `engine/check/first-run-reference-closure` uses. Absolute paths; a missing
    or malformed manifest degrades to removing nothing (loud-nothing, never a crash)."""
    manifest = os.path.join(_ENGINE, "provisioning", "first-run-assets.json")
    try:
        with open(manifest, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return set(), []
    files = {os.path.abspath(os.path.join(_ROOT, f)) for f in data.get("files", []) if isinstance(f, str)}
    dirs = [os.path.abspath(os.path.join(_ROOT, d)) for d in data.get("directories", []) if isinstance(d, str)]
    return files, dirs


def _always_present_tool_files(manifests: list) -> list:
    """Every `.py` tool file owned by a module that CANNOT be declined (status not declinable), MINUS the
    first-run-removed set — i.e. the tool files that actually ship on every deployment and are imported when a
    deployment runs its self-tests. A declinable module's own package dir is excluded wholesale (it retires as a
    unit and legitimately imports within itself)."""
    removed_files, removed_dirs = _first_run_removed()

    def _is_removed(path: str) -> bool:
        return path in removed_files or any(path == d or path.startswith(d + os.sep) for d in removed_dirs)

    files = set()
    for m in manifests:
        if m.get("status") in _DECLINABLE_STATUS:
            continue
        for g in _py_tool_globs(m):
            for f in glob.glob(os.path.join(_ROOT, g)):
                ap = os.path.abspath(f)
                if ap.endswith(".py") and not _is_removed(ap):
                    files.add(ap)
    return sorted(files)


def _pkg_match(module_name: "str | None", declinable: set) -> "str | None":
    """The declinable package `module_name` imports, matched COMPONENT-WISE by dotted prefix — so `memory.semantic`
    matches `memory.semantic` and `memory.semantic.store` but NOT the REQUIRED substrate `memory`. Else None."""
    if not module_name:
        return None
    parts = module_name.split(".")
    for pkg in declinable:
        p = pkg.split(".")
        if parts[:len(p)] == p:
            return pkg
    return None


def _top_level_declinable_imports(source: str, declinable: set) -> list:
    """(lineno, package) for every declinable-package import that is a DIRECT child of the module body — i.e.
    unconditional, not nested under if/try/with/def/class. That scoping is load-bearing: it distinguishes a
    naked top-level import (which runs at import time and aborts collection) from a guarded `if _HAVE_...:`
    import or a deferred in-function import, which do not run at import time and so cannot crash the suite."""
    hits = []
    for node in ast.parse(source).body:          # module-level statements only; deliberately never descend
        if isinstance(node, ast.Import):
            for alias in node.names:
                pkg = _pkg_match(alias.name, declinable)
                if pkg:
                    hits.append((node.lineno, pkg))
        elif isinstance(node, ast.ImportFrom) and node.level == 0:   # absolute import only (a relative one
            pkg = _pkg_match(node.module, declinable)                # can't name a top-level package)
            if pkg:
                hits.append((node.lineno, pkg))
    return hits


class TestNoAlwaysPresentToolImportsDeclinable(unittest.TestCase):
    """The core invariant: nothing that ships to every deployment may hard-import a module the operator can decline."""

    def test_no_unconditional_declinable_import(self):
        manifests = _module_manifests()
        declinable = _declinable_packages(manifests)
        self.assertTrue(declinable, "expected to derive at least one declinable package from the module manifests")
        offenders = []
        for path in _always_present_tool_files(manifests):
            try:
                with open(path, encoding="utf-8") as fh:
                    hits = _top_level_declinable_imports(fh.read(), declinable)
            except (OSError, SyntaxError):
                continue                         # unreadable / unparseable: skip, never crash the guard
            rel = os.path.relpath(path, _ROOT)
            offenders += [f"{rel}:{ln} imports declinable module {pkg!r} at module top level" for ln, pkg in hits]
        self.assertEqual(offenders, [], "always-present tools must guard optional-module imports (nest under a "
                         "find_spec guard, or import lazily inside the test that needs it):\n" + "\n".join(offenders))


class TestDeclinedDeploymentImportsCleanly(unittest.TestCase):
    """Behavioral proof for the motivating bug: with product-design blocked, the core conformance test still
    IMPORTS — the exact condition that, unfixed, aborted the whole suite at collection time."""

    def test_conformance_sweep_imports_with_product_design_absent(self):
        # A subprocess keeps the block isolated from this interpreter's already-imported modules. The meta-path
        # finder raises for `product_design*` — the shape a declined deployment presents at import time.
        script = (
            "import sys, importlib.abc\n"
            "class _Block(importlib.abc.MetaPathFinder):\n"
            "    def find_spec(self, name, path, target=None):\n"
            "        if name == 'product_design' or name.startswith('product_design.'):\n"
            "            raise ModuleNotFoundError(name)\n"
            "        return None\n"
            "sys.meta_path.insert(0, _Block())\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import test_conformance_sweep\n"
        )
        proc = subprocess.run([sys.executable, "-c", script, _HERE], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0,
                         "test_conformance_sweep must import cleanly with product-design absent; stderr:\n" + proc.stderr)


class TestGuardDoesNotSilentlyMask(unittest.TestCase):
    """Anti-masking: where product-design IS installed, the conformance suite's own guard must report it present
    — else a `find_spec` wrongly returning None would silently skip the seam test and hide a real regression."""

    def test_guard_matches_installed_state(self):
        pkg_dir = os.path.join(_HERE, "product_design")
        installed = os.path.isdir(pkg_dir) and bool(glob.glob(os.path.join(pkg_dir, "*.py")))
        if not installed:
            self.skipTest("product-design is not installed in this checkout")
        sys.path.insert(0, _HERE)
        import test_conformance_sweep as tcs  # noqa: E402 — the guard under test; core module, always present
        self.assertTrue(tcs._HAVE_PRODUCT_DESIGN,
                        "product-design has module files on disk but the conformance suite's guard reports it "
                        "absent — the seam test would silently skip and hide a regression")


if __name__ == "__main__":
    unittest.main()
