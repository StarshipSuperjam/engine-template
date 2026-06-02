#!/usr/bin/env python3
"""The knowledge graph (core slice 10) — the engine's generated, committed structural readout.

Knowledge answers "how does this world work?" — the purely STRUCTURAL, purely DERIVED layer:
what engine surfaces exist and how they relate. This tool generates ONE committed JSON file,
`.engine/knowledge/graph.json`, holding one entity per engine surface-instance file (each schema,
each check, each tool, the state cursor, and — as they appear — each contract, policy, operation,
skill, agent, interface, and doc) plus one entity per installed module, with the mechanical edges
between them. It is DERIVED from the declarations the engine already requires — the surface catalog,
the module manifests, and the check rules — so it cannot diverge from them, and it is never
hand-authored (it would drift) and never boot-only (latency while building is tolerable; latency
while using is not). Entity beliefs are forbidden: every field is read from the catalog, a manifest,
a check's target, or the file's own bytes — never "why" a choice was made (that is memory's, behind
the structure/belief wall).

The graph is kept honest by a FINGERPRINT GATE: the committed file is checked against its canonical
derivation. The committed content IS the fingerprint of its sources (each entity also carries a
sha256 of its own source file, so a changed source flips a hash), and the checker regenerates the
graph in memory and compares; any difference is drift — a surface changed, was added, or was removed
without a regenerate. The gate runs in CI as the `coverage`-kind rule engine/check/knowledge-coverage
(mode: fingerprint), which RELAYS to check() here — knowledge owns the detection, the rule relays it.

DERIVED, NOT THE QUERY LAYER: the derived query index, the graph-query MCP server, and the boot slice
are separate, regenerable, gitignored layers shipped by a later slice; this committed file is the
source of truth and the offline cold-start readout. Reverse traversal (who governs/enforces/provides
me) is the derived index's job — entities store OUTGOING edges only.

Library + CLI (mirrors self_map.py — plain language first; no JSON channel needed):

  uv run --directory .engine -- python tools/knowledge_gen.py show       # print the graph (live)
  uv run --directory .engine -- python tools/knowledge_gen.py generate   # (re)write .engine/knowledge/graph.json
  uv run --directory .engine -- python tools/knowledge_gen.py check       # is the committed graph in sync?
  uv run --directory .engine -- python tools/knowledge_gen.py demo        # safe fail->pass on a temp copy

Reuse: the present-set + ownership readers (discover_manifests / engine_file_inventory /
provides_claims) come from module_coherence.py; finding.v1, the catalog, and path/glob helpers from
validate.py, via the sibling-import precedent. The committed-artifact + drift-gate shape mirrors
self_map.py. The generic catalog-driven walk and the JSON edge vocabulary are new to this slice
(informed by the Engine_Prototype KG, not ported from it).
"""
from __future__ import annotations
import glob as _glob
import hashlib
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402
import module_coherence  # noqa: E402

# The committed graph's home: a directory (slice 11's gitignored index + boot slice live alongside),
# owned by core's provides.knowledge so the ownership leg does not flag it an orphan. NOT a catalogued
# surface (the knowledge map is derived-observational, excluded from the catalog by design), so it
# never becomes an entity of itself.
KNOWLEDGE_DIR = os.path.join(validate.ENGINE_DIR, "knowledge")
GRAPH_PATH = os.path.join(KNOWLEDGE_DIR, "graph.json")
SCHEMA_VERSION = 1
REGEN_CMD = "uv run --directory .engine -- python tools/knowledge_gen.py generate"


# ---- small shared helpers --------------------------------------------------------------------

def _rel(abs_path: str) -> str:
    """A repo-relative path with forward slashes (so committed bytes are identical on any host)."""
    return os.path.relpath(abs_path, validate.ROOT).replace(os.sep, "/")


def _display(path: str) -> str:
    """A path for human messages: repo-relative inside the repo, else absolute — never a `../` chain
    (matters for the demo's throwaway copy outside the repo)."""
    rel = os.path.relpath(path, validate.ROOT)
    return rel.replace(os.sep, "/") if not rel.startswith("..") else os.path.abspath(path)


def _loc_opt(path: str):
    """A finding.v1 location (repo-relative) — or None when the path is outside the repo."""
    rel = os.path.relpath(path, validate.ROOT)
    return None if rel.startswith("..") else {"file": rel.replace(os.sep, "/"), "line": None}


def source_fingerprint(rel_path: str) -> str:
    """A sha256 of a source file's RAW bytes (read with no newline translation), prefixed 'sha256:'.
    The per-entity provenance hash — a changed source flips it, so drift is caught and pinpointed."""
    with open(os.path.join(validate.ROOT, rel_path), "rb") as fh:
        return "sha256:" + hashlib.sha256(fh.read()).hexdigest()


def _slug(rel_path: str) -> str:
    """The id suffix: the file's stem (basename without its final extension), e.g.
    `.engine/check/state-cursor.json` -> `state-cursor`, `check.v1.json` -> `check.v1`."""
    return os.path.splitext(os.path.basename(rel_path))[0]


def _surface_for(rel_path: str, surfaces: dict):
    """The catalogued surface NAME whose `location` is the longest directory-prefix of rel_path,
    or None when the file lives under no catalogued surface (foundation/infra/module-manifest, or
    the derived knowledge dir itself)."""
    best_name, best_len = None, -1
    for name, rec in surfaces.items():
        location = (rec or {}).get("location", "")
        if location and rel_path.startswith(location) and len(location) > best_len:
            best_name, best_len = name, len(location)
    return best_name


# ---- pure derivation layer (no committed-file IO; fixture-testable) --------------------------

def derive_entities(catalog: dict, manifests: list, inventory: list, claims: dict) -> list:
    """The whole entity set, derived from the live sources, sorted by id. `manifests` is the list of
    (relpath, manifest) pairs from discover_manifests(); `inventory` the engine file relpaths;
    `claims` the {relpath: [module-id]} ownership map. All edges are MECHANICAL and OUTGOING."""
    surfaces = (catalog or {}).get("surfaces", {})
    entities: dict = {}
    path_to_id: dict = {}

    # Pass 1 — one entity per owned engine file that lives under a catalogued surface.
    for rel in inventory:
        surface = _surface_for(rel, surfaces)
        if surface is None:
            continue                                   # foundation/infra/module-manifest/knowledge dir
        owners = claims.get(rel) or []
        if not owners:
            continue                                   # an unowned file is a coherence anomaly, caught elsewhere
        eid = f"{surface}:{_slug(rel)}"
        preds = {"provided_by": [f"module:{owners[0]}"]}
        governing = (surfaces[surface] or {}).get("governing_schema")
        if governing and not governing.startswith("http"):   # an in-repo schema file, not the dialect URI
            preds["governed_by"] = [f"schema:{_slug(governing)}"]
        entities[eid] = {
            "id": eid, "type": surface, "name": rel, "slug": _slug(rel),
            "source": {"path": rel, "fingerprint": source_fingerprint(rel)},
            "owner": owners[0], "predicates": preds,
        }
        path_to_id[rel] = eid

    # Pass 2 — one entity per installed module.
    for path, m in manifests:
        mid = m.get("id")
        eid = f"module:{mid}"
        preds = {}
        deps = sorted((m.get("depends") or {}).keys())
        if deps:
            preds["depends_on"] = [f"module:{d}" for d in deps]
        entities[eid] = {
            "id": eid, "type": "module", "name": mid, "slug": mid,
            "source": {"path": path, "fingerprint": source_fingerprint(path)},
            "owner": mid, "predicates": preds,
        }
        path_to_id[path] = eid

    # Pass 3 — `targets` edges for check entities (needs the full path->id map).
    for rel, eid in path_to_id.items():
        if entities[eid]["type"] != "check":
            continue
        try:
            rule = validate.load_json(os.path.join(validate.ROOT, rel))
        except Exception:
            continue                                   # a malformed check is caught by its schema check
        matched = [_rel(p) for p in validate.target_files(rule)]
        targets = sorted({path_to_id[mp] for mp in matched if mp in path_to_id})
        if targets:
            entities[eid]["predicates"]["targets"] = targets

    return [entities[k] for k in sorted(entities)]


def render_graph(entities: list) -> str:
    """The whole deterministic graph JSON: sorted keys, 2-space indent, LF, exactly one final newline
    — so regenerate-and-compare is a valid byte-equality test."""
    graph = {"schema_version": SCHEMA_VERSION, "entities": entities}
    return json.dumps(graph, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


# ---- pure drift logic (no IO; fixture-testable) ---------------------------------------------

def drift_finding(canonical: str, committed: str | None, path: str, tier: str = "hard") -> dict:
    """The fingerprint gate as a pure function. `note` when the committed text equals the canonical
    derivation; the rule's `tier` (hard) when it drifted or is absent. The hard finding names the one
    fix (regenerate + commit) and the file — never a stack trace."""
    name, where = _display(path), _loc_opt(path)
    if committed is None:
        return validate.finding(
            tier,
            f"The knowledge graph ({name}) has not been generated yet. Create it with "
            f"`{REGEN_CMD}` and commit the result.",
            where)
    if committed != canonical:
        return validate.finding(
            tier,
            f"The knowledge graph ({name}) is out of date — it no longer matches the surfaces it is "
            f"generated from (a surface changed, was added, or was removed without regenerating). "
            f"Regenerate it with `{REGEN_CMD}` and commit the result.",
            where)
    return validate.finding(
        "note",
        f"The knowledge graph ({name}) is in sync with the surfaces it is generated from.",
        where)


# ---- IO / source layer ----------------------------------------------------------------------

def load_sources():
    """The live sources: (catalog dict, [(relpath, manifest)], [engine file relpaths],
    {relpath: [module-id]}). Reuses module_coherence's present-set + ownership readers so the graph
    and the module manager read the same installed set. Raises (loud) on a malformed source."""
    catalog = validate.load_json(validate.CATALOG_PATH)
    manifests = module_coherence.discover_manifests()
    inventory = module_coherence.engine_file_inventory()
    claims = module_coherence.provides_claims(manifests)
    return catalog, manifests, inventory, claims


def canonical_graph() -> str:
    """The canonical graph rendered from the live sources."""
    return render_graph(derive_entities(*load_sources()))


def read_committed(path: str):
    """The committed graph's exact bytes-as-text, or None if absent. newline='' so universal-newline
    translation cannot mask a CRLF-vs-LF difference in the equality test."""
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def write_graph(text: str, path: str) -> None:
    """Write the graph verbatim (newline='' so the LF content is not platform-translated)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def generate(path: str | None = None) -> dict:
    """(Re)write the committed graph from the live sources. Returns a `note` finding stating whether
    the file changed. `path` defaults to the live GRAPH_PATH (resolved at call time so a test may
    redirect it)."""
    path = GRAPH_PATH if path is None else path
    canonical = canonical_graph()
    changed = read_committed(path) != canonical
    write_graph(canonical, path)
    name = _display(path)
    msg = (f"Wrote the knowledge graph ({name})." if changed
           else f"The knowledge graph ({name}) was already up to date.")
    return validate.finding("note", msg, _loc_opt(path))


def check(path: str | None = None, tier: str = "hard") -> dict:
    """The fingerprint gate over the live sources + the committed file at `path` (defaults to the live
    GRAPH_PATH, resolved at call time so a test may redirect it). The drift severity is the caller's
    tier (the relaying rule's tier); in-sync is a `note`."""
    path = GRAPH_PATH if path is None else path
    return drift_finding(canonical_graph(), read_committed(path), path, tier)


# ---- CLI ------------------------------------------------------------------------------------

def _demo(_argv: list) -> int:
    """A safe, scripted fail->pass on a THROWAWAY COPY — never touches the committed graph."""
    with tempfile.TemporaryDirectory() as d:
        scratch = os.path.join(d, "graph.json")
        print("Generating the knowledge graph onto a throwaway copy (your committed file is untouched)...")
        print("    " + validate.fmt(generate(scratch)))
        print("(i) Checking it — should be in sync...")
        print("    " + validate.fmt(check(scratch)))
        print("(ii) Now hand-editing the copy to simulate drift...")
        with open(scratch, "a", encoding="utf-8", newline="") as fh:
            fh.write("a hand-edited line the generator would never write\n")
        print("    " + validate.fmt(check(scratch)))
        print("(iii) Regenerating to heal it...")
        print("    " + validate.fmt(generate(scratch)))
        print("    " + validate.fmt(check(scratch)))
        print("Done — a hand-edit was caught (drift) and regeneration restored the file (in sync). "
              "Your real .engine/knowledge/graph.json was never touched.")
    return 0


def main(argv: list) -> int:
    cmd = argv[0] if argv else "show"
    try:
        if cmd == "show":
            sys.stdout.write(canonical_graph())
            return 0
        if cmd == "generate":
            path = argv[1] if len(argv) > 1 else None
            print(validate.fmt(generate(path)))
            return 0
        if cmd == "check":
            path = argv[1] if len(argv) > 1 else None
            f = check(path)
            print(validate.fmt(f))
            return 1 if f["severity"] == "hard" else 0
        if cmd == "demo":
            return _demo(argv[1:])
        print(f"usage: knowledge_gen.py {{show|generate|check|demo}} [path]\nunknown command {cmd!r}",
              file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:  # a malformed source / unwritable path -> plain, no traceback
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
