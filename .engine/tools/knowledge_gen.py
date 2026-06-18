#!/usr/bin/env python3
"""The knowledge graph (core slice 10) — the engine's generated, committed structural readout.

Knowledge answers "how does this world work?" — the purely STRUCTURAL, purely DERIVED layer:
what engine surfaces exist and how they relate. This tool generates ONE committed JSON file,
`.engine/knowledge/graph.json`, holding one entity per engine surface-instance file (each schema,
each check, each tool, and — as they appear — each contract, policy, operation,
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

DERIVED, NOT THE QUERY LAYER: the derived query index and the graph-query MCP server are separate,
regenerable, gitignored layers (slice 11a); the prioritized boot slice is a further gitignored layer,
DEFERRED — its producer awaits its consumer (boot), so it is not yet produced. This committed file is
the source of truth and the offline cold-start readout. Reverse traversal (who governs/enforces/provides
me) is the derived index's job — entities store OUTGOING edges only.

Library + CLI (mirrors self_map.py — plain language first; no JSON channel needed):

  uv run --directory .engine -- python tools/knowledge_gen.py show       # print the graph (live)
  uv run --directory .engine -- python tools/knowledge_gen.py generate   # (re)write .engine/knowledge/graph.json
  uv run --directory .engine -- python tools/knowledge_gen.py check       # is the committed graph in sync?
  uv run --directory .engine -- python tools/knowledge_gen.py demo        # safe fail->pass on a temp copy
  uv run --directory .engine -- python tools/knowledge_gen.py hook-demo   # show the commit-boundary regen (no writes)

REGENERATION AT THE COMMIT BOUNDARY (knowledge/README §Regeneration): the `hook` verb is the
`PreToolUse` entry the engine wires. On a `git commit` it regenerates the graph best-effort and ALWAYS
proceeds — because the hook fires BEFORE the commit, the refreshed graph lands UNSTAGED in the working
tree and is captured by a FOLLOWING commit (it is not guaranteed to ride the commit that triggered it);
the fingerprint gate above is the unbypassable CI backstop that forces capture before merge. Regeneration
is a MUTATION, not a gate: it registers no block, never blocks the commit, and on any failure proceeds
(the staleness is caught downstream at CI).

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
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402
import module_coherence  # noqa: E402
import hooks             # noqa: E402  (the run_hook harness for the commit-boundary regen hook)

# The committed graph's home: a directory (slice 11a's gitignored index lives alongside under .cache/;
# the gitignored boot slice is a deferred layer, not yet produced), owned by core's provides.knowledge
# so the ownership leg does not flag it an orphan. NOT a catalogued
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


def _instance_slug(surface_type: str, rel_path: str) -> str:
    """The id suffix for a surface INSTANCE. A skill IS its directory under .claude/skills/ — the file is
    always SKILL.md, so the file stem would collide every skill onto 'SKILL'; its slug is that parent
    directory's name (e.g. `.claude/skills/engine-help/SKILL.md` -> `engine-help`). Every other surface has a
    distinct filename, so its slug is the file stem (`_slug`); an agent is `.claude/agents/<name>.md`, whose
    stem is already its name."""
    if surface_type == "skill":
        return os.path.basename(os.path.dirname(rel_path))
    return _slug(rel_path)


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


def surface_instance_inventory(catalog: dict, claims: dict) -> list:
    """The surface-instance file relpaths the graph entitizes: every ENGINE-OWNED file (a key of `claims` —
    i.e. claimed by some module's `provides`) that lives under a catalogued surface `location`, across BOTH
    .engine/ and .claude/ (skills and agents are catalogued surfaces located under .claude/), with `.gitkeep`
    directory-placeholders excluded (a placeholder is not a ratified instance). Sorted for byte-determinism.

    This is the catalog-location-driven walk the graph needs. It deliberately does NOT reuse
    module_coherence.engine_file_inventory(), which is hard-scoped to .engine/ ('the product never owns a file
    under .engine/') for the ownership/orphan checks that depend on that scope — widening it would break that
    invariant and never reach the .claude/ surfaces. Engine/product wall: only files a module's `provides`
    claims appear in `claims`, so an operator's own un-prefixed product skill is never entitized."""
    surfaces = (catalog or {}).get("surfaces", {})
    out = []
    for rel in claims:
        if os.path.basename(rel) == ".gitkeep":
            continue                                   # a directory-placeholder is not a real instance
        if _surface_for(rel, surfaces) is None:
            continue                                   # not under any catalogued surface location
        out.append(rel)
    return sorted(out)


# ---- pure attribute harvesters (operate on already-parsed dicts; NO file IO; fixture-testable) ----
# Each takes parsed frontmatter / JSON / manifest dicts and returns a declared STATE/IDENTITY token or a
# discriminator map — never prose meaning (D-203 four-gate rule: declared, structural, not belief). The
# file IO stays in derive_entities' passes; these stay pure so they unit-test on dicts.

def _status_for(surface_type: str, frontmatter: dict, manifest: dict | None) -> str:
    """The declared lifecycle STATE TOKEN (D-203 'else active'): a module manifest's `status` and a
    contract frontmatter's `status` are harvested verbatim; EVERY other surface is `active` (a declared
    status elsewhere is not echoed). A missing value on the two declaring surfaces degrades to `active`
    (a non-conforming instance, never a crash). Never the *why* of a supersession."""
    if surface_type == "module":
        val = (manifest or {}).get("status")
        return val if isinstance(val, str) and val else "active"
    if surface_type == "contract":
        val = (frontmatter or {}).get("status")
        return val if isinstance(val, str) and val else "active"
    return "active"


def _tier_for(surface_type: str, rule: dict | None):
    """CHECKS ONLY: the check rule's own bite tier (`hard`|`soft`) from its `tier` key; None for every
    other surface and for a check whose tier is absent/non-string (a malformed check, caught by its own
    schema check). A policy's prose enforcement tier is NEVER parsed."""
    if surface_type != "check":
        return None
    val = (rule or {}).get("tier")
    return val if val in ("hard", "soft") else None


# A small, closed lexicon of leading imperative verbs marking a command/description, not an identity. It is
# a FORWARD-DRIFT tripwire with ZERO live effect: every live policy/interface title is a bare noun-phrase
# that passes, and the live excluded titles (operations) are rejected by the structural em-dash rule, not
# by this list.
_IMPERATIVE_VERBS = frozenset({
    "add", "set", "start", "stop", "show", "list", "run", "make", "create", "remove", "delete", "update",
    "shape", "adjust", "switch", "enable", "disable", "configure", "open", "close", "build", "fix", "keep",
    "use", "write", "author", "tune",
})

# The identity-title surfaces and the SINGLE declared key each harvests (D-203 ruling): never operation/
# doc/contract (purpose/decision clauses), never a description, never a slug fallback.
_TITLE_KEYS = {"policy": "title", "interface": "title", "skill": "name"}


def _is_noun_phrase_title(s: str) -> bool:
    """The noun-phrase shape-guard: accept a bare identity name; reject a purpose clause / sentence /
    imperative. The two STRUCTURAL rejections do the live work (em-dash or spaced-hyphen purpose clause;
    mid-string sentence punctuation); the imperative-verb lexicon is a forward tripwire (zero live effect)."""
    if "—" in s or " - " in s:                    # em-dash / spaced hyphen -> a purpose clause
        return False
    if re.search(r"[.:]\s+\S", s):                     # mid-string sentence punctuation ('. ' or ': ')
        return False
    parts = s.split()
    if parts and parts[0].rstrip(",").lower() in _IMPERATIVE_VERBS:   # leading imperative verb -> a command
        return False
    return True


def _title_for(surface_type: str, data: dict):
    """The verbatim IDENTITY title for policy/interface/skill ONLY (`policy.title` / `interface.title` /
    `skill.name`), harvested from the already-parsed `data` (frontmatter for policy/skill, JSON for
    interface). Returns None (OMIT the attribute — no slug fallback) when the key is absent/empty or the
    value fails the noun-phrase shape-guard."""
    key = _TITLE_KEYS.get(surface_type)
    if key is None:
        return None
    val = (data or {}).get(key)
    if not isinstance(val, str) or not val.strip():
        return None
    val = val.strip()
    return val if _is_noun_phrase_title(val) else None


def _discriminators_for(surface_type: str, frontmatter: dict, json_doc: dict, manifest: dict | None) -> dict:
    """The per-surface discriminator attributes, each from its DECLARED key (check `kind`+`suites`; agent
    `role`+`lens`+`model-tier`; skill `invocation`; interface `operations`+`fallback`; module `version`).
    Returns the {attr: value} to merge onto the entity; only non-empty members are present; all lists are
    sorted for byte-determinism."""
    out: dict = {}
    fm, jd = (frontmatter or {}), (json_doc or {})
    if surface_type == "check":
        kind = jd.get("kind")
        if isinstance(kind, str) and kind:
            out["kind"] = kind
        suites = jd.get("suites")
        if isinstance(suites, list):
            out["suites"] = sorted(s for s in suites if isinstance(s, str))
    elif surface_type == "agent":
        for k in ("role", "lens", "model-tier"):
            v = fm.get(k)
            if isinstance(v, str) and v:
                out[k] = v
    elif surface_type == "skill":
        v = fm.get("invocation")
        if isinstance(v, str) and v:
            out["invocation"] = v
    elif surface_type == "interface":
        ops = jd.get("operations")
        if isinstance(ops, list):
            names = sorted(o["name"] for o in ops
                           if isinstance(o, dict) and isinstance(o.get("name"), str))
            if names:
                out["operations"] = names
        fb = jd.get("fallback")
        handle = fb.get("handle") if isinstance(fb, dict) else None
        if isinstance(handle, str) and handle:
            out["fallback"] = handle
    elif surface_type == "module":
        ver = (manifest or {}).get("version")
        if isinstance(ver, str) and ver:
            out["version"] = ver
    return out


def _supersedes_edges(contract_entities: list, fm_by_id: dict, canon_ids) -> dict:
    """{contract_id: [superseded_contract_id]} — contract->contract, DEPLOYMENT-STREAM (non-canon) ONLY.
    `fm_by_id` maps a contract entity id to its parsed frontmatter; `canon_ids` is the set of canon
    contract entity ids (those a module's `provides` claims — per D-169, told apart by provides-membership,
    NEVER a path or content marker). An edge is emitted only when BOTH ends are non-canon and the target
    resolves in-graph by the target's declared frontmatter `id`. A canon end on either side, a dangling
    target, or a self-reference emits NOTHING — so no persisted edge ever targets a canon eADR."""
    by_eadr: dict = {}                                 # declared frontmatter `id` (eADR-NNNN) -> entity id
    for e in contract_entities:
        decl = (fm_by_id.get(e["id"]) or {}).get("id")
        if isinstance(decl, str) and decl:
            by_eadr[decl] = e["id"]
    canon = set(canon_ids or ())
    edges: dict = {}
    for e in contract_entities:
        src_id = e["id"]
        if src_id in canon:                            # a canon contract never declares/emits supersedes
            continue
        target_eadr = (fm_by_id.get(src_id) or {}).get("supersedes")
        if not isinstance(target_eadr, str):
            continue
        target_id = by_eadr.get(target_eadr)
        if target_id is None or target_id == src_id or target_id in canon:
            continue                                   # dangling / self / canon target -> emit nothing
        edges.setdefault(src_id, []).append(target_id)
    return {k: sorted(v) for k, v in edges.items()}


# ---- pure derivation layer (no committed-file IO; fixture-testable) --------------------------

def derive_entities(catalog: dict, manifests: list, inventory: list, claims: dict) -> list:
    """The whole entity set, derived from the live sources, sorted by id. `manifests` is the list of
    (relpath, manifest) pairs from discover_manifests(); `inventory` the engine file relpaths;
    `claims` the {relpath: [module-id]} ownership map. All edges are MECHANICAL and OUTGOING."""
    surfaces = (catalog or {}).get("surfaces", {})
    entities: dict = {}
    path_to_id: dict = {}
    contract_fm_by_id: dict = {}                        # contract entity id -> its parsed frontmatter (Pass 3b)

    # Pass 1 — one entity per owned engine file that lives under a catalogued surface.
    for rel in inventory:
        surface = _surface_for(rel, surfaces)
        if surface is None:
            continue                                   # foundation/infra/module-manifest/knowledge dir
        owners = claims.get(rel) or []
        if not owners:
            continue                                   # an unowned file is a coherence anomaly, caught elsewhere
        slug = _instance_slug(surface, rel)
        eid = f"{surface}:{slug}"
        rec = surfaces[surface] or {}
        preds = {"provided_by": [f"module:{owners[0]}"]}
        governing = rec.get("governing_schema")
        if governing and not governing.startswith("http"):   # an in-repo schema file, not the dialect URI
            preds["governed_by"] = [f"schema:{_slug(governing)}"]
        ent = {
            "id": eid, "type": surface, "name": rel, "slug": slug,
            "source": {"path": rel, "fingerprint": source_fingerprint(rel)},
            "owner": owners[0], "predicates": preds,
        }
        # Harvest the surface's DECLARED attributes (D-203). Parse the file ONCE by its catalog class
        # (prose -> frontmatter; structured -> JSON; code/other -> nothing). A malformed file harvests
        # nothing (its own schema check is the gate); the harvesters are pure (operate on parsed dicts).
        fm, jd = {}, {}
        try:
            if rec.get("class") == "prose":
                fm = validate.frontmatter(os.path.join(validate.ROOT, rel)) or {}
            elif rec.get("class") == "structured":
                jd = validate.load_json(os.path.join(validate.ROOT, rel))
        except Exception:
            fm, jd = {}, {}
        ent["status"] = _status_for(surface, fm, None)
        tier = _tier_for(surface, jd)
        if tier is not None:
            ent["tier"] = tier
        title = _title_for(surface, jd if rec.get("class") == "structured" else fm)
        if title is not None:
            ent["title"] = title
        ent.update(_discriminators_for(surface, fm, jd, None))
        if surface == "contract":
            contract_fm_by_id[eid] = fm
        entities[eid] = ent
        path_to_id[rel] = eid

    # Pass 2 — one entity per installed module.
    for path, m in manifests:
        mid = m.get("id")
        eid = f"module:{mid}"
        preds = {}
        deps = sorted((m.get("depends") or {}).keys())
        if deps:
            preds["depends_on"] = [f"module:{d}" for d in deps]
        ent = {
            "id": eid, "type": "module", "name": mid, "slug": mid,
            "source": {"path": path, "fingerprint": source_fingerprint(path)},
            "owner": mid, "predicates": preds,
        }
        ent["status"] = _status_for("module", {}, m)
        ent.update(_discriminators_for("module", {}, {}, m))   # version
        entities[eid] = ent
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

    # Pass 3b — `supersedes` edges (contract->contract, DEPLOYMENT-STREAM only). Canon contracts are those
    # a module's `provides` claims (D-169: told apart by provides-membership, never a path/marker). Since
    # derive_entities only entitizes OWNED files (Pass 1), every contract ENTITY is owned == canon, so this
    # emits NOTHING in v1 (provably dormant); the guard is exercised by the unit fixtures.
    contract_entities = [entities[k] for k in sorted(entities) if entities[k]["type"] == "contract"]
    canon_ids = {e["id"] for e in contract_entities}   # all entitized contracts are owned -> canon
    for src_id, targets in _supersedes_edges(contract_entities, contract_fm_by_id, canon_ids).items():
        if targets:
            entities[src_id]["predicates"]["supersedes"] = targets

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
    """The live sources: (catalog dict, [(relpath, manifest)], [surface-instance file relpaths],
    {relpath: [module-id]}). Reuses module_coherence's present-set + ownership readers so the graph and the
    module manager read the same installed set; the inventory is the catalog-location-driven surface walk
    (`surface_instance_inventory`), which spans .engine/ AND .claude/ and drops placeholders. Raises (loud)
    on a malformed source."""
    catalog = validate.load_json(validate.CATALOG_PATH)
    manifests = module_coherence.discover_manifests()
    claims = module_coherence.provides_claims(manifests)
    inventory = surface_instance_inventory(catalog, claims)
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


# ---- the commit-boundary regen hook ----------------------------------------------------------
# A `git commit` command, matched at a command-start position (line start, or just after a shell
# separator) — the same shape modes.py uses. The intercept matches on the tool NAME (Bash) and tests
# the command INSIDE the script, never via a settings `if:` matcher (hooks/README the hook-script
# contract). Bounded parity-with-modes: a missing match (`git -c k=v commit`, an alias, a `git commit`
# inside a quoted string) just leaves a slightly stale graph that the CI fingerprint gate catches at the
# PR — it never blocks, and never mis-fires on echoed text (the separator anchor).
_CMD_START = r"(?:^|[\n;&|])\s*"
_GIT_COMMIT_RE = re.compile(_CMD_START + r"git\s+commit\b")


def _is_git_commit(payload: dict) -> bool:
    """True iff this tool call is a `git commit` Bash command — the regen trigger. Degrades safe: a
    non-dict payload / non-Bash tool / absent or non-string command -> False (no regen)."""
    if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
        return False
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    return isinstance(command, str) and bool(_GIT_COMMIT_RE.search(command))


def _regen_handler(payload: dict) -> dict:
    """The `PreToolUse` regen behaviour: on a `git commit`, refresh the committed graph best-effort, then
    ALWAYS proceed. This is the one hook that legitimately mutates committed state (it writes the real
    GRAPH_PATH). It NEVER blocks and NEVER injects: a regen failure proceeds (the commit is allowed) and
    is caught downstream by the CI fingerprint check. It is a MUTATION, not a gate, so it does not promote
    a finding (that law is for a gate that goes blind) — but it is never silent on failure (a plain note
    to stderr). The regen fires even when the commit will be denied by another `PreToolUse` hook (e.g.
    modes' Explore write-gate): both hooks run and `deny` wins, so the regen only ever refreshes an
    unstaged file the denied commit never captures — harmless."""
    if not _is_git_commit(payload):
        return hooks.proceed()
    try:
        result = generate()  # best-effort: refresh the committed graph (UNSTAGED) in the working tree
    except Exception as exc:  # noqa: BLE001 — a best-effort MUTATION, never a gate: proceed, never block;
        #   the CI knowledge-coverage fingerprint check is the durable backstop for any resulting staleness.
        sys.stderr.write(
            f"(knowledge) the commit-boundary knowledge-graph refresh could not run "
            f"({type(exc).__name__}: {exc}); your commit was not affected — the merge-time check will "
            f"catch any staleness.\n")
        return hooks.proceed()
    # Not silent when it changed something: a plain best-effort note (on a proceeding `PreToolUse` this
    # reaches the debug log, not the transcript — the durable record is the working-tree change the CI gate
    # forces into a following commit). Keyed to generate()'s own "Wrote ..." message (same file).
    if (result.get("message") or "").startswith("Wrote"):
        sys.stderr.write(
            "(knowledge) refreshed the knowledge graph (.engine/knowledge/graph.json) for this commit; it "
            "is left in your working tree for the next commit — your commit was not affected.\n")
    return hooks.proceed()


# ---- CLI ------------------------------------------------------------------------------------

def _hook_demo(_argv: list) -> int:
    """Show the commit-boundary regen WITHOUT touching the committed graph: which tool calls trigger it,
    that a refresh writes the graph, and that it never blocks. The real graph.json is untouched."""
    commit = {"tool_name": "Bash", "tool_input": {"command": "git add -A && git commit -m 'x'"}}
    status = {"tool_name": "Bash", "tool_input": {"command": "git status"}}
    a_read = {"tool_name": "Read", "tool_input": {"file_path": "x"}}
    print("Which tool calls fire the commit-boundary regen (the PreToolUse hook tests this in-script):")
    for label, p in (("git add -A && git commit", commit), ("git status", status), ("a Read", a_read)):
        print(f"    {'FIRES' if _is_git_commit(p) else 'skips'} - {label}")
    with tempfile.TemporaryDirectory() as d:
        scratch = os.path.join(d, "graph.json")
        print("\nWhen it fires it refreshes the graph (shown on a throwaway copy):")
        print("    " + validate.fmt(generate(scratch)))
    print("\nThe hook ALWAYS proceeds: a commit is never blocked, and on any failure the commit still "
          "goes through (the merge-time fingerprint check catches any staleness). Your real "
          ".engine/knowledge/graph.json was never touched.")
    return 0


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
        if cmd == "hook-demo":
            return _hook_demo(argv[1:])
        if cmd == "hook":  # the PreToolUse entry the engine wires: regen at the git-commit boundary
            return hooks.run_hook("PreToolUse", _regen_handler)
        print(f"usage: knowledge_gen.py {{show|generate|check|demo|hook-demo|hook}} [path]\n"
              f"unknown command {cmd!r}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:  # a malformed source / unwritable path -> plain, no traceback
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
