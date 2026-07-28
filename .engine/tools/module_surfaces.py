#!/usr/bin/env python3
"""The module-surfaces registry — every file an engine MODULE provides, mapped to its owning module id.

Generated from ALL module manifests in the SOURCE repo (where every module is present), committed to
`.engine/provisioning/module-surfaces.json`, and shipped UNCHANGED to every deployment — it is NOT regenerated
per deployment, so it keeps listing a module's surfaces even after that module is DECLINED and its manifest is
gone. That is what lets a deployed repo recognize a path missing *because its owning optional module was
declined* as a legitimate absence rather than a broken reference: the link-integrity check consults
`declined_surface_owner` to TOLERATE a dangling link to such a path instead of failing it (#646).

`load` and `declined_surface_owner` read only the committed registry + `engine.json` (both travel) and import
nothing heavy, so `validate._coverage_links` can call them without a circular import; `derive`/`generate`
import `module_coherence` lazily and run only where the full manifest set is present (the source repo).
"""
from __future__ import annotations
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402

REGISTRY_REL = os.path.join(".engine", "provisioning", "module-surfaces.json")


def derive(root: str | None = None) -> dict:
    """{relpath: module_id} for every file the PRESENT module manifests provide. Complete only where every
    module is present (the source repo), which is why the committed file is generated there and travels."""
    import module_coherence  # lazy: module_coherence imports validate, so keep it out of import time
    surfaces: dict = {}
    for rel, owners in module_coherence.provides_claims(module_coherence.discover_manifests()).items():
        # A provided file is sole-owned in practice; if two modules glob the same file it is shared
        # infrastructure, and either owner is a fine answer for "whose optional decline removes it".
        surfaces[rel] = sorted(owners)[0]
    return surfaces


def generate(root: str | None = None) -> dict:
    surfaces = derive(root)
    path = os.path.join(root or validate.ROOT, REGISTRY_REL)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"surfaces": {k: surfaces[k] for k in sorted(surfaces)}}, fh, indent=1, ensure_ascii=False)
        fh.write("\n")
    return surfaces


def load(root: str | None = None) -> dict:
    """The committed registry {relpath: module_id}, or {} when absent/unreadable (degrade, never crash the
    link check that consults it)."""
    path = os.path.join(root or validate.ROOT, REGISTRY_REL)
    try:
        return (validate.load_json(path) or {}).get("surfaces") or {}
    except Exception:  # noqa: BLE001 — an unreadable registry degrades to no tolerance, never a crash
        return {}


def _installed_module_ids(root: str | None = None) -> set:
    try:
        eng = validate.load_json(os.path.join(root or validate.ROOT, ".engine", "engine.json"))
        return set((eng or {}).get("packages") or {})
    except Exception:  # noqa: BLE001
        return set()


def declined_surface_owner(abs_path: str, root: str | None = None) -> "str | None":
    """The owning module id if `abs_path` belongs to a module NOT installed in this deployment — so its absence
    is a legitimate decline, not a broken reference — else None. Two cases: a file under a module's own
    directory `.engine/modules/<mid>/`, and an overlaid surface the registry maps to a module. Returns None
    for a path owned by an installed module (a genuinely missing file there is a real defect the module's own
    coverage catches) or a path no module owns (a real broken link)."""
    root = root or validate.ROOT
    rel = os.path.relpath(abs_path, root)
    installed = _installed_module_ids(root)
    parts = rel.split(os.sep)
    if len(parts) >= 3 and parts[0] == ".engine" and parts[1] == "modules":
        mid = parts[2]
        if mid not in installed:
            return mid
    owner = load(root).get(rel)
    if owner and owner not in installed:
        return owner
    return None


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "generate":
        generate()
        print(f"wrote {REGISTRY_REL}")
    else:
        print("usage: module_surfaces.py generate", file=sys.stderr)
        sys.exit(2)
