#!/usr/bin/env python3
"""Module coherence — the first consumer of the validation foundation's coherence legs.

After any install / uninstall / upgrade the module manager confirms the installed module
set is consistent by calling the validation foundation's coherence legs directly — a
library call, not a suite trigger (systems/grammar/module-system/README.md §Coherence).
The permanent module manager lands later (slice 25); this is the seed consumer it inherits.
It also runs as a CLI so the coherence behaviour has an operator-runnable demonstration:

  uv run --directory .engine -- python tools/module_coherence.py          # plain language
  uv run --directory .engine -- python tools/module_coherence.py --json   # finding.v1 JSON

It reads the present module manifests (.engine/modules/*/manifest.json) — "installed means
present", a directory listing, never a hand-authored registry — and the engine manifest
(.engine/engine.json), then reports four coherence legs:

  - DEPENDENCY (reused from the validation foundation, validate.coherence_findings): every
    declared dependency is installed and within its version range, and the graph is acyclic.
  - OWNERSHIP (validate.ownership_findings): every engine file under .engine/ is claimed by
    exactly one module's `provides`, or is a named foundation infrastructure artifact, or is
    a module manifest (owned by its own module by construction). An unclaimed file is an
    orphan; a doubly-claimed file is a conflict.
  - WIRING — FORWARD declared->applied (validate.wiring_findings over wiring.is_applied): every
    `wires` directive a present manifest declares is applied in its shared target file (the wiring
    library landed at slice 7, so this leg is owed now). An mcp wire is APPROVAL-BLIND — it checks
    the committed .mcp.json definition, never the operator's runtime approval (that pending state is
    surfaced at boot / the control-plane PR-Validation section, not here). This is the FORWARD
    direction ONLY: it CANNOT catch undeclared-but-applied wiring (a stale leftover after a botched
    uninstall) — that orphan-wire REVERSE leg needs a per-seam enumerator and is deferred to the
    module manager (slice 25), so wiring coherence is incomplete until then, by design. The
    foundation `.venv` .gitignore block (D-156) is outside this leg: no manifest declares it, so the
    forward leg never iterates it.
  - BLOCK-BUDGET (validate.block_budget_findings over the declared block registry): every block an
    owning system declares (hooks.BLOCK_ELIGIBLE_INVARIANTS) sits on a block-eligible event — only
    PreToolUse and Stop may hard-block (hooks/README §the block-budget law). Born at this, the first
    hook-wiring slice (slice 20), now that .claude/settings.json exists and hooks are wired; the
    registry is EMPTY in core (modes/21 + close/22 populate it), so the leg is green-but-present.

Deferred (named): the WIRING REVERSE / orphan-wire direction needs a per-seam enumerator, slice 25;
the uncatalogued-surface leg belongs to catalog coverage (validators-core).

Discovery and loading are exposed as reusable functions (discover_manifests,
load_engine_manifest) so the self-map (slice 8) and the module manager (slice 25) read the
present set from here rather than re-walking it — one present-set reader, no drift.
"""
from __future__ import annotations
import glob as _glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402
import wiring     # noqa: E402  (the wiring library: is_applied per directive for the forward wiring leg)
import hooks      # noqa: E402  (the block-eligible invariant registry the block-budget leg checks)

# The named foundation infrastructure artifacts that live under .engine/ and are owned by no
# module's `provides` — exactly the engine manifest plus the tool-runtime lockfiles
# (repository-topology/README.md; module-system/README.md §Coherence). The root CLAUDE.md
# and the engine-owned .github/ control-plane files are also named foundation artifacts, but
# they live OUTSIDE .engine/ in containers the product co-occupies, so they are not part of
# the .engine/ ownership walk (the only corner where "is this file owned?" is well-defined).
ENGINE_MANIFEST_REL = ".engine/engine.json"
NAMED_INFRA = {ENGINE_MANIFEST_REL, ".engine/pyproject.toml", ".engine/uv.lock"}

# Directories under .engine/ that are regenerable derivatives or caches — never owned files. The
# inventory's contract is "every COMMITTED engine file"; these hold gitignored regenerable artifacts
# (the uv venv, Python bytecode, and knowledge's derived `.cache/` query index, slice 11). Pruning
# them keeps the ownership leg from flagging a derived cache as an unowned orphan.
PRUNE_DIRS = {".venv", "__pycache__", ".cache"}

MODULES_GLOB = ".engine/modules/*/manifest.json"


def _rel(abs_path: str) -> str:
    return os.path.relpath(abs_path, validate.ROOT).replace(os.sep, "/")


def discover_manifests() -> list:
    """The present module manifests as (relpath, manifest) pairs — installed-means-present,
    read from the .engine/modules/ directory listing. Sorted by path for stable output."""
    out = []
    for abs_path in sorted(_glob.glob(os.path.join(validate.ROOT, MODULES_GLOB))):
        out.append((_rel(abs_path), validate.load_json(abs_path)))
    return out


def load_engine_manifest():
    """The engine manifest (.engine/engine.json) as a dict, or None if it is absent."""
    path = os.path.join(validate.ROOT, ENGINE_MANIFEST_REL)
    return validate.load_json(path) if os.path.isfile(path) else None


def engine_file_inventory() -> list:
    """Every committed engine file under .engine/ (relpaths), excluding regenerable
    derivative directories. The product never owns a file under .engine/, so this is the
    exclusively-engine corner where file ownership is well-defined."""
    out = []
    for dirpath, dirs, files in os.walk(validate.ENGINE_DIR):
        dirs[:] = [d for d in dirs if d not in PRUNE_DIRS]
        out.extend(_rel(os.path.join(dirpath, f)) for f in files)
    return sorted(out)


def provides_claims(manifests: list) -> dict:
    """{relpath: [module-id, ...]} — for each present manifest, every file its `provides`
    globs select, mapped to the owning module id. Built against the live filesystem so it
    uses real glob semantics; the pure validate.ownership_findings consumes the result."""
    claims: dict = {}
    for _path, m in manifests:
        mid = m.get("id")
        for _group, patterns in (m.get("provides") or {}).items():
            for pattern in patterns:
                for abs_path in _glob.glob(os.path.join(validate.ROOT, pattern), recursive=True):
                    if os.path.isfile(abs_path):
                        claims.setdefault(_rel(abs_path), []).append(mid)
    return claims


# The shared target file each seam's wire lands in — a plain-language LABEL for the wiring leg's
# finding only (the operative target paths are wiring.py's own constants, which is_applied actually
# reads). Derived from those constants so the label is SINGLE-HOMED and cannot drift from them.
WIRING_TARGETS = {
    "hook": _rel(wiring.SETTINGS_PATH),
    "permission": _rel(wiring.SETTINGS_PATH),
    "mcp": _rel(wiring.MCP_PATH),
    "gitignore": _rel(wiring.GITIGNORE_PATH),
    "ontology-entry": _rel(wiring.CATALOG_PATH),
}


def wiring_status(manifests: list) -> list:
    """The forward wiring-leg input: (module_id, seam_type, target_label, is_applied) for every
    `wires` directive of every present manifest, with `is_applied` computed live by the wiring
    library over the real shared files. The pure validate.wiring_findings consumes the result —
    so the live filesystem reads live here, the policy stays pure there (the ownership split)."""
    out = []
    for _path, m in manifests:
        mid = m.get("id")
        for directive in (m.get("wires") or []):
            seam = directive.get("type") if isinstance(directive, dict) else None
            target = WIRING_TARGETS.get(seam, "its shared target file")
            out.append((mid, seam, target, wiring.is_applied(directive)))
    return out


def block_eligible_registrations() -> list:
    """The block declarations the block-budget leg governs: the owning systems' declared block
    invariants (hooks.BLOCK_ELIGIBLE_INVARIANTS), each {event, name, owner}. These — NOT bare
    .claude/settings.json hook registrations — are the authoritative "this blocks" source: a wired
    hook command is opaque, so registration alone never implies a block (boot's SessionStart hook is
    wired in settings.json yet declares no block). The committed settings.json, BORN at this first
    hook-wiring slice, is the wiring CONTEXT that makes this leg live; the declared registry is what it
    checks. Empty in core today — modes' explore write-gate (slice 21) and close's findings-disposition
    block (slice 22) populate it — so the leg is green-but-present: it checks every declared block's
    event the moment one is registered."""
    return [dict(inv) for inv in hooks.BLOCK_ELIGIBLE_INVARIANTS]


def check_coherence(tier: str = "hard") -> list:
    """All four coherence legs over the present set: dependency (reused) + ownership + forward
    wiring (declared->applied) + block-budget (only PreToolUse/Stop may hard-block). Returns a flat
    list of finding.v1 dicts. The library entry the module manager (slice 25) calls. (The orphan-wire
    REVERSE wiring direction is deferred to slice 25 — see the module docstring.)"""
    manifests = discover_manifests()
    dep = validate.coherence_findings(
        [m for _path, m in manifests], tier,
        "Install the missing module, adjust the version range, or break the dependency "
        "cycle, then re-check.")
    exempt = set(NAMED_INFRA) | {path for path, _m in manifests}
    own = validate.ownership_findings(
        engine_file_inventory(), provides_claims(manifests), exempt, tier,
        "Every engine file must be owned by exactly one module.")
    wiring_leg = validate.wiring_findings(
        wiring_status(manifests), tier,
        "Declared wiring must be applied in the shared files.")
    block = validate.block_budget_findings(
        block_eligible_registrations(), tier,
        "Only PreToolUse and Stop may hard-block; move the block to an eligible event before merging.")
    return dep + own + wiring_leg + block


def _print_report(findings: list, n_modules: int, n_files: int) -> None:
    """Plain-language-first, matching the validator's report() register — a human sentence
    per issue, never raw JSON (the operator reads this; --json is the machine channel)."""
    hard = [f for f in findings if f["severity"] == "hard"]
    soft = [f for f in findings if f["severity"] != "hard"]
    if soft:
        print(f"\nnotes ({len(soft)}):")
        for f in soft:
            print("  - " + validate.fmt(f))
    if hard:
        print(f"\nModule coherence found {len(hard)} issue(s):")
        for f in hard:
            print("  - " + validate.fmt(f))
    elif not soft:
        print(f"\nOK — the module set is coherent: {n_modules} module(s) installed, "
              f"{n_files} engine file(s), all owned.")


def main(argv: list) -> int:
    try:
        manifests = discover_manifests()
        inventory = engine_file_inventory()
        findings = check_coherence()
    except Exception as exc:  # a malformed manifest/engine file halts loudly, in plain language
        print(f"\nCONFIG ERROR: cannot read the module manifests or the engine manifest: "
              f"{exc}", file=sys.stderr)
        return 2
    if "--json" in argv:
        print(json.dumps(findings, indent=2))
    else:
        _print_report(findings, len(manifests), len(inventory))
    return 1 if any(f["severity"] == "hard" for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
