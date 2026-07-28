#!/usr/bin/env python3
"""Behavioral FALSIFICATION for the release-cut deployment gate (#664) — the gate must BLOCK a release that
would not operate when deployed, and PASS one that does. This is the control the gate exists to be: a release
that regenerates its wiring map cleanly on a module-declined deployment passes; one that regresses the #663
carve-out (so the map regen fails closed on the declined shape) is caught and the cut is blocked, no release
pull request opened.

FAIL-THEN-PASS at the GATE level (a peer of `demo_663`, which falsifies the resolver one level below):
  * POSITIVE (a healthy release): the gate projects the candidate to a module-declined deployed shape — the
    exact #663 configuration — and it operates cleanly (the validator suite passes, the wiring map
    regenerates). The gate would let the cut proceed.
  * NEGATIVE CONTROL (a regressed release): the same projection built from a candidate whose optional-subtree
    carve-out has been disabled (the pre-#663 resolver) fails to regenerate its wiring map, so the gate raises
    its fail-CLOSED signal (`GateError`) — which `release_gate.main` turns into a nonzero exit that stops the
    cut before any pull request opens. A gate that could not even build the projection BLOCKS; it never waves
    the cut through.

Both arms run the REAL gate helpers (`release_gate._project_to_deployed` — real module removal, real map
regeneration) against a throwaway CLONE of this engine, so the falsification exercises the shipped gate logic,
not a stub. This demo is CONSTRUCTION EVIDENCE, retired from a generated repo at first-run (home-repo-only, it
clones the whole engine). The durable per-PR guard for the gate's orchestration is
`test_release_gate.py`; the permanent end-to-end proof is the gate's own run at each release cut. Run it
directly: `uv run --directory .engine -- python tools/demo_664_release_gate.py`.
"""
from __future__ import annotations
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate                 # noqa: E402
import release_gate as rg       # noqa: E402  (the shipped gate helpers under test)

_KNOWLEDGE_GEN_REL = os.path.join(".engine", "tools", "knowledge_gen.py")
_CARVE_OUT_LINE = '_OPTIONAL_MODULE_SUBTREES = frozenset({("memory", "semantic")})'
_CARVE_OUT_DISABLED = "_OPTIONAL_MODULE_SUBTREES = frozenset()"

_COPY_IGNORE = shutil.ignore_patterns(".venv", "__pycache__", "worktrees", "node_modules", "*.pyc", ".git")
_COPY_DIRS = (".engine", ".claude", ".codex", ".agents", ".github")
_COPY_FILES = (".mcp.json", ".gitignore", "CLAUDE.md", "AGENTS.md")


def _clone_engine(real_root: str, dest: str) -> str:
    """Copy this repo's real engine surface into `dest` — a genuine engine whose projection is clean, so the
    falsification isolates the gate's behaviour, not a broken fixture."""
    os.makedirs(dest, exist_ok=True)
    for rel in _COPY_DIRS:
        src = os.path.join(real_root, rel)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(dest, rel), ignore=_COPY_IGNORE, symlinks=True)
    for rel in _COPY_FILES:
        src = os.path.join(real_root, rel)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(dest, rel))
    return dest


def _disable_carve_out(tree: str) -> bool:
    """Regress the #663 fix in the CLONE's own `knowledge_gen.py` (the pre-#663 resolver, no optional-subtree
    carve-out), so a module-declined projection can no longer regenerate its wiring map. Returns True if the
    edit landed."""
    path = os.path.join(tree, _KNOWLEDGE_GEN_REL)
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    if _CARVE_OUT_LINE not in src:
        return False
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(src.replace(_CARVE_OUT_LINE, _CARVE_OUT_DISABLED))
    return True


def main() -> int:
    real_root = validate.ROOT
    failures = []
    print("=" * 78)
    print("DEMO #664 — the release-cut deployment gate must BLOCK a release that would not operate when")
    print("deployed (a regressed #663 carve-out) and PASS one that does. Same module-declined projection,")
    print("two arms; the only difference is whether the candidate carries the optional-subtree carve-out.")
    print("=" * 78)

    # ---- POSITIVE: a healthy candidate operates on the module-declined deployed shape ----
    with tempfile.TemporaryDirectory() as d:
        tree = _clone_engine(real_root, os.path.join(d, "healthy"))
        blocked, detail = None, ""
        try:
            declined = rg._project_to_deployed(tree, decline_optional=True)   # real remove + map regen
            result = rg._validate_in(tree, "operate/declined")
            blocked = not result["passed"]
            detail = result["detail"]
        except rg.GateError as exc:
            blocked, detail = True, str(exc)
        print("\n[POSITIVE — a healthy release]")
        print(f"  declined modules: {declined if not blocked else '(projection failed)'}")
        print(f"  the gate would let the cut proceed (operates cleanly): {not blocked}")
        if blocked:
            failures.append(f"POSITIVE: the gate blocked a healthy release: {detail[:400]}")

    # ---- NEGATIVE CONTROL: a candidate that regressed the #663 carve-out is BLOCKED ----
    with tempfile.TemporaryDirectory() as d:
        tree = _clone_engine(real_root, os.path.join(d, "regressed"))
        if not _disable_carve_out(tree):
            failures.append("NEGATIVE: could not seed the regression (carve-out line not found)")
        else:
            blocked = False
            try:
                rg._project_to_deployed(tree, decline_optional=True)   # the regressed map regen fails closed
            except rg.GateError:
                blocked = True
            print("\n[NEGATIVE CONTROL — a release that regressed the #663 carve-out]")
            print(f"  the gate blocked the cut (fail-closed GateError): {blocked}")
            if not blocked:
                failures.append("NEGATIVE: the gate did NOT block a release that cannot regenerate its wiring "
                                "map on a module-declined deployment")

    print("\n" + "=" * 78)
    if failures:
        print("DEMO #664 FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("DEMO #664 PASSED: the deployment gate lets a healthy release cut proceed (it operates on a "
          "module-declined deployment) and blocks a release that regressed the #663 carve-out (its wiring map "
          "cannot regenerate on the declined shape) — the gate fails closed, so a broken release never opens "
          "its pull request.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
