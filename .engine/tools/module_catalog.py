#!/usr/bin/env python3
"""Shared reader for the optional-module catalog — the single home that reads
`.engine/provisioning/module-catalog.json`.

Provisioning owns the catalog (it ships empty and grows as optional modules are built); two readers RELAY
it and must never drift in how they parse it, so both go through this one function:
- the first-run setup walkthrough (`instantiator.py`) — groups the entries by discipline and presents them
  as opt-out-able choices;
- the `/engine-help` command index (`engine_help.py`) — lists an uninstalled module's command under
  "available if you install it".

This is the shared skill/command-discovery helper the `/engine-help` work recorded as owed once a
second reader appeared — that second reader is the instantiator, so it lands here. The reader DEGRADES and
never raises (degrade-to-git-native): a missing, unreadable, malformed, or wrong-shaped catalog
narrows the relay to nothing rather than breaking either caller. It only relays — it decides nothing about
what is installed, and validates nothing (the shape is governed by `provisioning-catalog.v1.json`, enforced
by the `engine/check/provisioning-catalog` schema check, not this read path). Every well-formed entry is
relayed, including a command-less optional module (one with no `verb`): the setup walkthrough offers it by
its description, while `/engine-help` — which lists only typeable commands — filters it out at that reader.
"""
from __future__ import annotations
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402

CATALOG_PATH = os.path.join(validate.ENGINE_DIR, "provisioning", "module-catalog.json")

# The fields a relayed entry carries. `description` + `category` + `status` feed both readers: the setup
# walkthrough groups by category and offers by description; /engine-help presents each offerable add-on
# under "available through engine-setup" by its description. There is no per-module `verb`: offerable
# modules are reached through natural-language setup routes and the permanent engine-setup dispatcher, not a
# typed per-module command.
_FIELDS = ("id", "description", "category", "status")

# The offerable statuses — the opt-out-able set the catalog covers. A required or internal module is never
# offered and carries no catalog entry (and no manifest `presentation`).
_OFFERABLE = ("optional", "default-on", "experimental")


def _normalize(entry: dict) -> dict:
    """One catalog record coerced to the relayed shape. Missing fields become empty strings; nothing raises.
    The reader relays every well-formed entry and decides nothing — the shape is enforced by the
    `engine/check/provisioning-catalog` schema check."""
    return {field: str(entry.get(field) or "") for field in _FIELDS}


def entries(path: str | None = None) -> list:
    """The optional-module catalog as a list of normalized records (each a dict with `id`, `description`,
    `category`, `status`), sorted by `id`. Returns `[]` when there is no catalog or it cannot be read as the
    expected top-level array — a missing or damaged catalog narrows the relay, never raises. `path` is
    injectable for tests/demo; the committed catalog is read by default."""
    target = path or CATALOG_PATH
    if not os.path.isfile(target):
        return []
    try:
        with open(target, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out = [_normalize(entry) for entry in data if isinstance(entry, dict)]
    return sorted(out, key=lambda e: e["id"])


def _raw_prior(path: str) -> list:
    """The prior committed catalog as a raw list (for merge-preserving declined entries), or [] when absent
    or malformed."""
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return []
    return data if isinstance(data, list) else []


def derive(path: str | None = None, root: str | None = None) -> list:
    """The catalog derived from the PRESENT offerable module manifests' `presentation` records, MERGE-PRESERVING
    declined entries from the prior committed catalog. An offerable module (status optional / default-on /
    experimental) that carries a `presentation` produces one entry {id, description, category, status}; the
    manifest is the single source, never a hand-authored catalog. A prior-catalog entry whose module has NO
    present manifest is a DECLINED module: its entry is retained so a later upgrade neither resurrects nor
    forgets it. This keeps the catalog a merge-preserving generation (prior catalog + manifests), not a pure
    from-manifests derivation — declined-module memory is not a function of the present source tree."""
    import module_coherence  # lazy: it imports validate; keep it out of import time
    target = path or CATALOG_PATH
    present_ids: set = set()
    derived: dict = {}
    for _rel, manifest in module_coherence.discover_manifests(root):
        if not isinstance(manifest, dict):
            continue
        mid = manifest.get("id")
        if not mid:
            continue
        present_ids.add(mid)
        pres = manifest.get("presentation")
        status = manifest.get("status")
        if isinstance(pres, dict) and status in _OFFERABLE and pres.get("description") and pres.get("category"):
            derived[mid] = {
                "id": mid,
                "description": pres["description"],
                "category": pres["category"],
                "status": status,
            }
    # Merge-preserve: retain a prior entry only for a module with no present manifest (a declined module).
    for entry in _raw_prior(target):
        if not isinstance(entry, dict):
            continue
        eid = entry.get("id")
        if eid and eid not in present_ids and eid not in derived:
            derived[eid] = {k: entry[k] for k in _FIELDS if k in entry}
    return [derived[k] for k in sorted(derived)]


def _serialize(catalog: list) -> str:
    """The catalog's canonical committed text. The SINGLE serialization both `generate` (which writes it) and
    `check` (which compares against it) use, so a drift check cannot disagree with what generate would write."""
    return json.dumps(catalog, indent=2, ensure_ascii=False) + "\n"


def generate(path: str | None = None, root: str | None = None) -> list:
    """Write the derived, merge-preserving catalog to `.engine/provisioning/module-catalog.json` and return it.
    Run in the SOURCE repo where every module is present (so it lists all offerable modules); shipped unchanged
    to deployments. Where it is regenerated with a module absent, the declined entry is preserved by `derive`."""
    target = path or CATALOG_PATH
    catalog = derive(target, root)
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(_serialize(catalog))
    return catalog


def check(tier: str = "hard", path: str | None = None, root: str | None = None) -> list:
    """Drift gate: the committed catalog must equal the canonical serialization of the derived catalog, so a
    hand-edit to a present module's entry — or a stale catalog left after a manifest `presentation` change —
    turns the check red until it is regenerated. A DECLINED-module entry (no present manifest) is preserved by
    `derive` and so is legitimately not flagged; it has no manifest source of truth. Returns [] when in sync,
    one hard finding (carrying the plain-language regenerate guidance) on drift or an absent catalog. `path`
    overrides the committed-side read and `root` the tree the derivation reads — together the seam the
    negative-fixture meta-check uses to witness the gate biting a stale catalog. The derivation is rooted
    because a deployment that declined its add-ons has no offerable manifests, so a real-tree derivation there
    merge-preserves the seeded stale catalog unchanged and the aimed bite could never be witnessed."""
    target = path or CATALOG_PATH
    canonical = _serialize(derive(target, root))
    try:
        with open(target, encoding="utf-8") as fh:
            committed = fh.read()
    except OSError:
        committed = None
    if committed == canonical:
        return []
    return [validate.finding(
        tier,
        "The module catalog (.engine/provisioning/module-catalog.json) is out of date — it no longer matches "
        "the module manifests' presentation records it is generated from (it must never be hand-authored). "
        "Regenerate it with `uv run --directory .engine -- python tools/module_catalog.py generate` and commit "
        "the result.",
        {"file": ".engine/provisioning/module-catalog.json", "line": None})]


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "generate":
        result = generate()
        print(f"wrote {os.path.relpath(CATALOG_PATH, validate.ROOT)} ({len(result)} entries)")
    else:
        print("usage: module_catalog.py generate", file=sys.stderr)
        sys.exit(2)
