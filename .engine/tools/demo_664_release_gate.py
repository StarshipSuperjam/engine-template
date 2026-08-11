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
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_fixture           # noqa: E402  (the shared tracked-only fixture clone)
import validate                 # noqa: E402
import release_gate as rg       # noqa: E402  (the shipped gate helpers under test)

_KNOWLEDGE_GEN_REL = os.path.join(".engine", "tools", "knowledge_gen.py")
_CARVE_OUT_LINE = '_OPTIONAL_MODULE_SUBTREES = frozenset({("memory", "semantic")})'
_CARVE_OUT_DISABLED = "_OPTIONAL_MODULE_SUBTREES = frozenset()"
_MODULE_MANAGER_REL = os.path.join(".engine", "tools", "module_manager.py")
_SWITCH_BACK_LINE = '    if _git(root, "checkout", branch) is None:'
_SWITCH_BACK_BROKEN = '    if True:  # seeded rollback regression (demo #664 negative control): switch-back neutralized'


def _seed_rollback_regression(tree: str) -> bool:
    """Break the CLONE's own `rollback` switch-back (the pre-#599-safe shape) so a staged update cannot be
    cleanly undone — the discard reports `partial` instead of `undone`. Because the overlay installs the
    candidate's `module_manager.py` into the projection, this edit reaches the rollback child the gate spawns
    after the practice upgrade — the only mechanism that crosses that process boundary (an in-process patch
    like demo_594's cannot). The UPGRADE leg is untouched (it never calls the discard), so this isolates the
    ROLLBACK leg: the upgrade still passes, the undo fails, and the gate blocks. Returns True if seeded."""
    path = os.path.join(tree, _MODULE_MANAGER_REL)
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    if _SWITCH_BACK_LINE not in src:
        return False
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(src.replace(_SWITCH_BACK_LINE, _SWITCH_BACK_BROKEN, 1))
    return True

def _seed_rename_residue(tree: str) -> bool:
    """Introduce a genuine rename-residue import into the always-present memory substrate — a
    `from memory.<gone> import ...` whose target does not exist and is NOT an optional-subtree carve-out — so
    the candidate's wiring map cannot regenerate during an upgrade. This is the #663 *class* (a reconcile that
    reds because the map won't regenerate) via a real dangling import rather than a declined module — the
    failure Arm B (upgrade-when-deployed) exists to catch. Returns True if seeded."""
    path = os.path.join(tree, ".engine", "tools", "memory", "mcp_server.py")
    if not os.path.isfile(path):
        return False
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("from memory.renamed_gone_by_a_bad_refactor import nothing  # seeded rename residue\n" + src)
    return True


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
        tree = engine_fixture.clone_engine(real_root, os.path.join(d, "healthy"))
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
        tree = engine_fixture.clone_engine(real_root, os.path.join(d, "regressed"))
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

    # ---- NEGATIVE CONTROL (upgrade arm): a candidate that cannot regenerate its wiring map is BLOCKED ----
    with tempfile.TemporaryDirectory() as d:
        tree = engine_fixture.clone_engine(real_root, os.path.join(d, "residue"))
        if not _seed_rename_residue(tree):
            failures.append("NEGATIVE(upgrade): could not seed the rename residue (mcp_server.py not found)")
        else:
            res = rg._upgrade_from("v0.4.0", tree)   # practice-upgrade a real past release TO the seeded candidate
            print("\n[NEGATIVE CONTROL — upgrade arm, a candidate whose wiring map cannot regenerate]")
            print(f"  the gate's upgrade arm blocked the cut: {not res['passed']}")
            if res["passed"]:
                failures.append("NEGATIVE(upgrade): the gate did NOT block a candidate whose wiring map cannot "
                                "regenerate during an upgrade")

    # ---- POSITIVE (upgrade + rollback arm): a healthy candidate upgrades AND cleanly rolls back ----
    with tempfile.TemporaryDirectory() as d:
        tree = engine_fixture.clone_engine(real_root, os.path.join(d, "healthy-tx"))
        res = rg._upgrade_from("v0.4.0", tree)   # a real past release -> practice upgrade -> undo, all clean
        up_ok = (res.get("upgrade") or {}).get("passed")
        rb_ok = (res.get("rollback") or {}).get("passed")
        print("\n[POSITIVE — upgrade + rollback arm, a healthy candidate]")
        print(f"  upgrade passed: {up_ok}; rollback passed: {rb_ok}; transition passed: {res['passed']}")
        if not (res["passed"] and up_ok and rb_ok is True):
            failures.append(f"POSITIVE(rollback): a healthy candidate did not upgrade-then-cleanly-roll-back "
                            f"(upgrade={up_ok}, rollback={rb_ok}, detail={(res.get('rollback') or {}).get('detail','')[:300]})")

    # ---- NEGATIVE CONTROL (rollback arm): a candidate whose undo cannot restore the copy is BLOCKED ----
    with tempfile.TemporaryDirectory() as d:
        tree = engine_fixture.clone_engine(real_root, os.path.join(d, "rollback-regressed"))
        if not _seed_rollback_regression(tree):
            failures.append("NEGATIVE(rollback): could not seed the regression (switch-back line not found)")
        else:
            res = rg._upgrade_from("v0.4.0", tree)   # upgrade still clean; the seeded undo cannot restore
            up_ok = (res.get("upgrade") or {}).get("passed")
            rb_ok = (res.get("rollback") or {}).get("passed")
            print("\n[NEGATIVE CONTROL — rollback arm, a candidate whose undo cannot restore the copy]")
            print(f"  upgrade still passed: {up_ok}; the gate's rollback arm blocked the cut: {rb_ok is False}")
            if not (up_ok and rb_ok is False):
                failures.append("NEGATIVE(rollback): the gate did NOT block a candidate whose staged update "
                                f"could not be cleanly undone (upgrade={up_ok}, rollback={rb_ok})")

    print("\n" + "=" * 78)
    if failures:
        print("DEMO #664 FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("DEMO #664 PASSED: the deployment gate lets a healthy release cut proceed (it operates on a "
          "module-declined deployment, and upgrades then cleanly rolls back from a real past release), blocks a "
          "release that regressed the #663 carve-out (its wiring map cannot regenerate on the declined shape), "
          "blocks — on the upgrade arm — a release whose wiring map cannot regenerate during an upgrade, AND "
          "blocks — on the rollback arm — a release whose staged update cannot be cleanly undone (the #703 "
          "matrix's rollback leg). The gate fails closed, so a broken release never opens its pull request.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
