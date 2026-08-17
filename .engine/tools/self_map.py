#!/usr/bin/env python3
"""The self-map — the engine's generated, committed "what am I made of" readout.

A non-engineer needs a lay of the land. This tool generates ONE committed Markdown file,
`.engine/self-map.md`, that answers "what is my engine made of": the engine release, the kinds
of file the engine governs (surfaces), and the packages it is assembled from (modules). It is
DERIVED from the declarations the engine already requires — the surface catalog and the module
manifests, plus (StarshipSuperjam/engine-template#513) the first-run retirement census cross-checked against what actually
exists on disk, so a deployed repo's map never advertises a file the retire step deleted. It is
never hand-authored (it would drift) and never boot-only (a human opening the repo could not read it).

The map is kept honest by a FINGERPRINT GATE: the committed file is checked against its canonical
derivation. The committed content IS the fingerprint of its sources — the checker regenerates the
map in memory from the current catalog + manifests and compares; any difference is drift. This
catches both a hand-edit of the map and a source change with no regenerate, with one mechanism
(the standard generated-file check, like `gofmt -l` / `prettier --check`). The gate runs in CI as
the `custom/script` rule engine/check/self-map-drift (its thin entry is self_map_check.py).

REGENERATION AT THE COMMIT BOUNDARY (resolves the StarshipSuperjam/engine-template#136 self-map/graph asymmetry): the `hook` verb is the
`PreToolUse` entry the engine wires (beside knowledge_gen's graph hook). On a `git commit` it refreshes the
committed self-map best-effort and ALWAYS proceeds — the refresh lands UNSTAGED in the working tree and is
captured by the following commit. It is a MUTATION, not a gate: it never blocks the commit, and on any failure
proceeds (the CI drift gate is the durable backstop). This gives the self-map the same commit-boundary refresh
the knowledge graph already had — closing the asymmetry the StarshipSuperjam/engine-template#136 resolution noted (graph.json had a hook, the
self-map did not). Both are subordinate to `integrate`'s authoritative regenerate-last and the unbypassable CI
drift gate (best-effort hook -> integrate regenerate-last -> CI gate).

Library + CLI (mirrors module_coherence.py / wiring.py — plain language first, --no JSON channel):

  uv run --directory .engine -- python tools/self_map.py show       # print the readout (live)
  uv run --directory .engine -- python tools/self_map.py generate   # (re)write .engine/self-map.md
  uv run --directory .engine -- python tools/self_map.py check       # is the committed map in sync?
  uv run --directory .engine -- python tools/self_map.py demo        # safe fail->pass on a temp copy
  uv run --directory .engine -- python tools/self_map.py hook-demo   # show the commit-boundary regen (no writes)
  uv run --directory .engine -- python tools/self_map.py hook        # the PreToolUse entry the engine wires

Reuse: the present-set readers discover_manifests()/load_engine_manifest() are reused from
module_coherence.py (exposed for exactly this — one present-set reader, no drift), and
finding.v1 + path helpers from validate.py via the sibling-import precedent. The per-module render
is exposed as render_module() so the permanent module manager reuses the operator-facing
module prose rather than diverge into a second renderer.

The wiring-graph portion renders the module dependency graph in TOPOLOGICAL order (each module after
the ones it depends on) with an explicit dependency-edge view; the surface portion renders EVERY governed field of the locked surface
record, so the fingerprint covers the whole record (a repointed governing_schema/template trips the gate).

Scope (named): the map renders module `wires` as the directive TYPE list only — the closed seam
vocabulary (hook/mcp/ontology-entry/permission/gitignore), the part locked in module.v1.json; the
per-type directive BODY is rendered for manifests that carry wires. The
operator-reachable access path is the `/engine-parts` command (`.claude/skills/engine-parts/`), the
plain-language "what is my engine made of" readout — it runs `show`, is auto-advertised by /engine-help,
and is pointed at from getting-started.md and the root CLAUDE.md floor; `show` and the directly-openable
committed file remain the readout it renders (StarshipSuperjam/engine-template#400).
"""
from __future__ import annotations
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402
import hooks             # noqa: E402  (the run_hook harness for the commit-boundary regen hook)
import module_coherence  # noqa: E402

# The committed map's home. A top-level .engine/ file (beside engine.json / suites.json), claimed
# by core's provides.foundation so the ownership leg does not flag it an orphan; a top-level FILE
# (not a new .engine/<dir>/) so it is invisible to the catalog-coverage orphan-directory check.
SELF_MAP_PATH = os.path.join(validate.ENGINE_DIR, "self-map.md")

REGEN_CMD = "uv run --directory .engine -- python tools/self_map.py generate"


# ---- small shared helpers --------------------------------------------------------------------

def _cell(value) -> str:
    """A value rendered into a Markdown table cell: pipes escaped so they cannot forge a column,
    and any stray newline flattened — defensive, though catalog values are single-line prose."""
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def _code(value) -> str:
    """A path/identifier as an inline code span. Code spans contain no `](` sequence, so the
    rendered map never trips link-integrity's LINK_RE (which matches the raw `](` byte-sequence
    anywhere, not just real Markdown links)."""
    return f"`{value}`"


def _opt_code(value) -> str:
    """An OPTIONAL governed field (governing_schema / template) as a table cell: an inline code span
    when present, else the ASCII sentinel `(none)` — consistent with render_module's empty-list
    rendering, and ASCII-stable in the byte-compared fingerprinted map (no non-ASCII sentinel). A
    null/empty value is the catalog's own "no schema/template governs this surface"."""
    return _code(value) if value else "(none)"


def _display(path: str) -> str:
    """A path for human messages: repo-relative when inside the repo (e.g. `.engine/self-map.md`),
    else an absolute path — never a `../../..` chain, which reads to a non-engineer like a bug
    (matters for the demo's throwaway copy outside the repo)."""
    rel = os.path.relpath(path, validate.ROOT)
    return rel.replace(os.sep, "/") if not rel.startswith("..") else os.path.abspath(path)


def _loc_opt(path: str):
    """A finding.v1 location (repo-relative, per the schema) — or None when the path is outside the
    repo, where a repo-relative location would be meaningless (mirrors wiring.py's _loc_opt)."""
    rel = os.path.relpath(path, validate.ROOT)
    return None if rel.startswith("..") else {"file": rel.replace(os.sep, "/"), "line": None}


# ---- pure render layer (no IO; fixture-testable) ---------------------------------------------

def render_header(engine: dict) -> list:
    """The banner + the engine-level line. `engine` is the engine.json dict (or None)."""
    release = (engine or {}).get("engine_release", "unknown")
    identity = (engine or {}).get("identity", "unknown")
    return [
        "# What this engine is made of",
        "",
        "> **Generated file — do not edit by hand.** This map is derived from the engine's surface",
        "> catalog and module manifests, so it always matches them. To update it, change those and",
        f"> regenerate with {_code(REGEN_CMD)}, then commit the result.",
        "",
        "> **What this shows — and what it does not.** This map shows your engine's structural makeup:",
        "> the kinds of file it governs and the packages it is built from, derived to match those sources.",
        "> It does not show whether each part *works* or is well designed — that is your review and each",
        "> module's own checks, never something this map attests.",
        "",
        f"Engine release {_code(release)} · identity {_code(identity)}",
    ]


def render_surfaces(surfaces: dict) -> list:
    """The surface-level portion: one table row per catalogued surface, sorted by name, carrying
    EVERY governed field of the locked surface record (name, purpose, home/location,
    authority, lifecycle, class, governing_schema, template). Rendering
    the whole record is what makes the fingerprint total — a repointed/nulled governing_schema or
    template now changes the map and trips the drift gate, so the "cannot diverge" guarantee holds for
    the whole record, not a subset. `surfaces` is the catalog's `surfaces` map {name: record}."""
    out = [
        "## Surfaces",
        "",
        f"Every kind of file the engine governs — its home and authority, and the schema and template "
        f"that govern it ({len(surfaces)} surfaces).",
        "",
        "| surface | purpose | home | authority | lifecycle | class | governing schema | template |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name in sorted(surfaces):
        rec = surfaces[name] or {}
        out.append(
            f"| {_code(name)} | {_cell(rec.get('purpose', ''))} | "
            f"{_code(rec.get('location', ''))} | {_cell(rec.get('authority', ''))} | "
            f"{_cell(rec.get('lifecycle', ''))} | {_cell(rec.get('class', ''))} | "
            f"{_opt_code(rec.get('governing_schema'))} | {_opt_code(rec.get('template'))} |")
    return out


def _retired_absent() -> "tuple[set, tuple]":
    """The first-run retirement census entries that are ALSO absent on disk — the post-retire state of a
    deployed repo (StarshipSuperjam/engine-template#513), as (absent files, absent directories). Without this filter the map keeps
    advertising the retired first-run operation file, the deleted setup-skill trees, and their census
    siblings after first-run removed them, pointing a session at files that don't exist. Read as plain
    data from the committed census (never by importing the retiring instantiator — the reference-closure
    check's own discipline); resolved at call time so tests can redirect the root. A missing or unreadable
    census reads as empty — the map renders un-filtered rather than failing (a corrupted census is caught
    by its own surviving CI shape check, never by this renderer). In the construction repo every census
    entry still exists, so both sets are empty and the map is unchanged."""
    census = os.path.join(validate.ENGINE_DIR, "provisioning", "first-run-assets.json")
    try:
        data = validate.load_json(census)
        files = list(data.get("files") or [])
        dirs = list(data.get("directories") or [])
    except Exception:  # noqa: BLE001 — no census, no filter; never fail the render over it
        return set(), ()
    absent_files = {rel for rel in files
                    if not os.path.exists(os.path.join(validate.ROOT, rel))}
    absent_dirs = tuple(rel for rel in dirs
                        if not os.path.isdir(os.path.join(validate.ROOT, rel)))
    return absent_files, absent_dirs


def _is_retired_absent(path: str, absent_files: set, absent_dirs: tuple) -> bool:
    """True iff a provides entry is retired-and-absent: an exact census-file match, or a path AT or UNDER a
    census directory that retire removed wholesale (the engine-setup skill trees) — the two ways the retire
    step deletes things. Prefix matching is directory-boundary-safe (`d + "/"`), never substring."""
    if path in absent_files:
        return True
    return any(path == d or path.startswith(d + "/") for d in absent_dirs)


def render_module(manifest: dict) -> list:
    """One module's block (the reusable per-module render the module manager inherits).
    Renders id, version (from the manifest's own `version`), status, depends, provides, and the
    `wires` directive TYPE list (the locked closed seam vocabulary; per-type bodies land later).
    Provides entries that first-run has retired AND that are absent on disk are filtered out
    (StarshipSuperjam/engine-template#513, see _retired_absent) — the map never advertises a file the retire step deleted."""
    mid = manifest.get("id", "?")
    version = manifest.get("version", "?")
    status = manifest.get("status", "?")
    out = [f"### {_code(mid)} — version {_code(version)} ({_cell(status)})", ""]

    depends = manifest.get("depends") or {}
    if depends:
        edges = ", ".join(
            (f"{_code(dep)} {_cell(rng)}" if rng else _code(dep))
            for dep, rng in sorted(depends.items()))
        out.append(f"- depends on: {edges}")
    else:
        out.append("- depends on: nothing")

    provides = manifest.get("provides") or {}
    if provides:
        absent_files, absent_dirs = _retired_absent()
        out.append("- provides:")
        for group in sorted(provides):
            patterns = ", ".join(_code(p) for p in sorted(provides[group] or [])
                                 if not _is_retired_absent(p, absent_files, absent_dirs))
            out.append(f"  - {_cell(group)}: {patterns or '(none)'}")
    else:
        out.append("- provides: nothing")

    types = sorted({(w or {}).get("type", "?") for w in (manifest.get("wires") or [])})
    if types:
        out.append(f"- wires: {', '.join(_cell(t) for t in types)}")
    else:
        out.append("- wires: none (this module adds no shared-state edits)")
    return out


def render_wiring_graph(ordered: list) -> list:
    """The explicit dependency-edge view: the modules in topological order, each shown with the
    modules it depends on (`→`), so the wiring reads as a graph rather than a flat block. Edges are
    the manifest `depends` ids (sorted); a root module shows "(no dependencies)". `ordered` is the
    already-topologically-sorted manifest list. Rendered with code spans (no `](` sequence), so
    link-integrity passes; deterministic, so it is part of the byte-compared fingerprint."""
    out = [
        "The dependency graph — each module is listed after the ones it builds on "
        "(`→` means \"depends on\"):",
        "",
    ]
    for m in ordered:
        mid = m.get("id", "?")
        deps = sorted((m.get("depends") or {}).keys())
        if deps:
            out.append(f"- {_code(mid)} → " + ", ".join(_code(d) for d in deps))
        else:
            out.append(f"- {_code(mid)} (no dependencies)")
    return out


def render_modules(manifests: list) -> list:
    """The wiring-graph portion: the module dependency graph rendered in TOPOLOGICAL order (each
    module after the ones it `depends` on, via validate.topological_order) with an explicit edge view,
    then one detail block per installed module in that same order — so the section reads as a graph,
    not a flat alphabetical block. `manifests` is a list of manifest dicts (the values from
    module_coherence.discover_manifests())."""
    ordered = validate.topological_order(manifests)
    out = [
        "## Modules",
        "",
        f"The packages your engine is assembled from, and how they wire together "
        f"({len(manifests)} installed).",
        "",
    ]
    out.extend(render_wiring_graph(ordered))
    out.append("")
    for m in ordered:
        out.extend(render_module(m))
        out.append("")
    if out and out[-1] == "":
        out.pop()
    return out


def _first_sentence(text: str) -> str:
    """The first sentence of a description. Splits on the first sentence-ending period followed by a space OR a
    newline (matching engine_help._first_sentence, so the two operator-facing readouts render a description's
    first sentence identically); falls back to the whole (single-sentence) text."""
    text = (text or "").strip()
    for sep in (". ", ".\n"):
        idx = text.find(sep)
        if idx != -1:
            return text[:idx + 1]
    return text


def _render_targets(targets) -> str:
    """A route's canonical targets rendered for a table cell: each as `kind \\`ref\\` (availability)`, joined
    by `; `. An absent/empty target list renders the ASCII sentinel `(none)` (a route that points nowhere)."""
    if not isinstance(targets, list) or not targets:
        return "(none)"
    parts = []
    for t in targets:
        if not isinstance(t, dict):
            continue
        parts.append(f"{_cell(t.get('kind', '?'))} {_code(t.get('ref', '?'))} "
                     f"({_cell(t.get('availability', '?'))})")
    return "; ".join(parts) if parts else "(none)"


def render_routes(rows: list) -> list:
    """The commands-and-routes portion: how a person reaches the engine. Two tables derived from the engine
    skills — the OPERATOR COMMANDS a person types (user-invocable skills), and the AUTOMATIC ROUTES the
    assistant may follow on its own (model-reachable skills), each row carrying its owning module, and each
    route its canonical targets with their active/module-conditional/home-only state. `rows` is the gathered
    route-row list (from _gather_route_rows); an empty list renders the section with empty tables (the shape a
    synthetic fixture with no skills yields), so the render stays pure and deterministic."""
    operator = sorted((r for r in rows if r.get("typeable")), key=lambda r: r["slug"])
    routes = sorted((r for r in rows if r.get("reachable")), key=lambda r: r["slug"])
    out = [
        "## Commands and routes",
        "",
        f"How you reach your engine: the commands a person types, and the automatic routes the assistant "
        f"may follow on your behalf ({len(operator)} operator commands, {len(routes)} automatic routes).",
        "",
        "### Operator commands",
        "",
        "| command | what it does | module |",
        "| --- | --- | --- |",
    ]
    for r in operator:
        out.append(f"| {_code(r['slug'])} | {_cell(_first_sentence(r['description']))} | {_code(r['owner'])} |")
    out += [
        "",
        "### Automatic routes",
        "",
        "Routes the assistant may follow on its own to reach an engine workflow, with what each points at "
        "and whether that target is always present, module-conditional, or home-only.",
        "",
        "A note on runtimes: on Claude these routes are hidden from the operator's typed menu (they are "
        "model-only, so `engine-help` never lists them); on Codex, which has no hidden-route selector, the "
        "same routes are also explicitly visible and typeable. That is the one deliberate provider asymmetry, "
        "and this map is where it is disclosed.",
        "",
        "| route | reachable as | points at | module |",
        "| --- | --- | --- | --- |",
    ]
    for r in routes:
        out.append(f"| {_code(r['slug'])} | {_cell(r['invocation'])} | {_render_targets(r['targets'])} | "
                   f"{_code(r['owner'])} |")
    return out


def render_map(catalog: dict, manifests: list, engine: dict, routes: list = ()) -> str:
    """The whole deterministic Markdown map. Sections joined by a blank line; LF newlines; no
    trailing whitespace; exactly one final newline — so regenerate-and-compare is a valid equality
    test. Contains no `](` sequence (paths are code spans), so link-integrity passes. `routes` is the
    gathered route rows (empty by default, so a fixture render stays deterministic)."""
    surfaces = (catalog or {}).get("surfaces", {})
    sections = [render_header(engine), render_surfaces(surfaces), render_modules(manifests),
                render_routes(list(routes))]
    lines = []
    for i, sec in enumerate(sections):
        if i:
            lines.append("")
        lines.extend(sec)
    body = "\n".join(ln.rstrip() for ln in lines)
    return body + "\n"


# ---- pure drift logic (no IO; fixture-testable) ---------------------------------------------

def drift_finding(canonical: str, committed: str | None, path: str) -> dict:
    """The fingerprint gate as a pure function. `note` when the committed text equals the canonical
    derivation; `hard` when it drifted; `hard` when the committed file is absent (committed=None).
    The hard finding names the one fix (regenerate + commit) and the file — never a stack trace."""
    name, where = _display(path), _loc_opt(path)
    if committed is None:
        return validate.finding(
            "hard",
            f"The self-map ({name}) has not been generated yet. Create it with "
            f"`{REGEN_CMD}` and commit the result.",
            where)
    if committed != canonical:
        return validate.finding(
            "hard",
            f"The self-map ({name}) is out of date — it no longer matches the surfaces and "
            f"modules it is generated from. Regenerate it with `{REGEN_CMD}` and commit the result.",
            where)
    return validate.finding(
        "note",
        f"The self-map ({name}) is in sync with the surfaces and modules it is generated from.",
        where)


# ---- IO / source layer ----------------------------------------------------------------------

def load_sources():
    """The three declaration sources, read from disk: (catalog dict, [manifest dicts], engine dict).
    Reuses module_coherence's present-set readers so the self-map and the module manager read the
    same installed set. Raises (loud) on a malformed source — the engine's own files fail closed."""
    catalog = validate.load_json(validate.CATALOG_PATH)
    manifests = [m for _path, m in module_coherence.discover_manifests()]
    engine = module_coherence.load_engine_manifest()
    return catalog, manifests, engine


def _skill_owner_map(manifests: list) -> dict:
    """{skill-slug: owning-module-id} from each manifest's `provides.skill` (a list of
    `.claude/skills/<slug>/SKILL.md` paths). The first owner wins (a skill is owned once)."""
    owner: dict = {}
    for m in manifests:
        mid = m.get("id", "?")
        for entry in ((m.get("provides") or {}).get("skill") or []):
            slug = os.path.basename(os.path.dirname(str(entry)))
            owner.setdefault(slug, mid)
    return owner


def _gather_route_rows(manifests: list) -> list:
    """The commands-and-routes rows for the map, gathered from the on-disk engine skills (the PRESENT set —
    a route first-run retired is simply absent, so the deployed map never advertises it). Each row is
    {slug, invocation, reachable, typeable, description, targets, owner}. Sorted by slug (deterministic).
    Reachability is imported from codex_gen._MODEL_REACHABLE (the single home)."""
    import skill_discovery  # lazy: the shared skill-discovery helper (IO)
    import codex_gen        # lazy: single home of _MODEL_REACHABLE
    owner = _skill_owner_map(manifests)
    rows = []
    for rec in skill_discovery.records("claude"):
        fm = rec["frontmatter"]
        inv = fm.get("invocation")
        rows.append({
            "slug": rec["slug"],
            "invocation": inv if isinstance(inv, str) else "(unset)",
            # Platform-truth reachability (an omitted invocation is model-auto = reachable), the same notion the
            # route checks enforce — so the readout classifies an invocation-less model-auto skill as a route.
            "reachable": codex_gen.is_platform_reachable(inv),
            # Typeable = the operator can type it: every invocation class EXCEPT model-only, which alone sets
            # user-invocable: false. operator-typed and model-auto are both typeable (engine-recall is both a
            # typed command and an automatic route), so it appears under Operator commands and Automatic routes.
            "typeable": inv != "model-only",
            "description": fm.get("description") or "",
            "targets": fm.get("engine-targets") or [],
            "owner": owner.get(rec["slug"], "(unowned)"),
        })
    return sorted(rows, key=lambda r: r["slug"])


def canonical_map() -> str:
    """The canonical map rendered from the live sources (surfaces, modules, and the commands-and-routes rows)."""
    catalog, manifests, engine = load_sources()
    return render_map(catalog, manifests, engine, _gather_route_rows(manifests))


def read_committed(path: str):
    """The committed map's exact bytes-as-text, or None if it does not exist. Read with newline=''
    so universal-newline translation cannot mask a CRLF-vs-LF difference in the equality test."""
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def write_map(text: str, path: str) -> None:
    """Write the map verbatim (newline='' so the LF content is not platform-translated)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def generate(path: str | None = None) -> dict:
    """(Re)write the committed map from the live sources. Returns a `note` finding stating whether
    the file changed or was already current. `path` defaults to the live SELF_MAP_PATH (resolved at
    call time, so a test may redirect it)."""
    path = SELF_MAP_PATH if path is None else path
    canonical = canonical_map()
    changed = read_committed(path) != canonical
    write_map(canonical, path)
    name = _display(path)
    msg = (f"Wrote the self-map ({name})." if changed
           else f"The self-map ({name}) was already up to date.")
    return validate.finding("note", msg, _loc_opt(path))


def check(path: str | None = None) -> dict:
    """The fingerprint gate over the live sources + the committed file at `path` (defaults to the
    live SELF_MAP_PATH, resolved at call time so a test may redirect it)."""
    path = SELF_MAP_PATH if path is None else path
    return drift_finding(canonical_map(), read_committed(path), path)


# ---- the commit-boundary regen hook ----------------------------------------------------------
# Fires at the `git commit` boundary — the classifier is hooks._is_git_commit, shared with the other
# commit-boundary hooks (knowledge_gen's graph regen, validation's pre-commit nudge) rather than copied.
def _regen_handler(payload: dict) -> dict:
    """The `PreToolUse` regen behaviour: on a `git commit`, refresh the committed self-map best-effort,
    then ALWAYS proceed. Like knowledge_gen's graph hook, this is a MUTATION that legitimately writes the
    real SELF_MAP_PATH (UNSTAGED). It NEVER blocks and NEVER injects: a regen failure proceeds (the commit
    is allowed) and is caught downstream by the CI drift gate. It is a MUTATION, not a gate, so it does not
    promote a finding — but it is never silent on failure (a plain note to stderr). The regen fires even
    when the commit will be denied by another `PreToolUse` hook (e.g. modes' Explore write-gate): every
    hook runs and `deny` wins, so the regen only ever refreshes an unstaged file the denied commit never
    captures — harmless."""
    if not hooks._is_git_commit(payload):
        return hooks.proceed()
    try:
        result = generate()  # best-effort: refresh the committed self-map (UNSTAGED) in the working tree
    except Exception as exc:  # noqa: BLE001 — a best-effort MUTATION, never a gate: proceed, never block;
        #   the CI self-map-drift check is the durable backstop for any resulting staleness.
        sys.stderr.write(
            f"(ontology) the commit-boundary self-map refresh could not run "
            f"({type(exc).__name__}: {exc}); your commit was not affected — the merge-time check will "
            f"catch any staleness.\n")
        return hooks.proceed()
    # Not silent when it changed something: a plain best-effort note (on a proceeding `PreToolUse` this
    # reaches the debug log, not the transcript — the durable record is the working-tree change the CI gate
    # forces into a following commit). Keyed to generate()'s own "Wrote ..." message (same file).
    if (result.get("message") or "").startswith("Wrote"):
        sys.stderr.write(
            "(ontology) refreshed the self-map (.engine/self-map.md) for this commit; it is left in your "
            "working tree for the next commit — your commit was not affected.\n")
    return hooks.proceed()


# ---- CLI ------------------------------------------------------------------------------------

def _hook_demo(_argv: list) -> int:
    """Show the commit-boundary regen WITHOUT touching the committed map: which tool calls trigger it,
    that a refresh writes the map, and that it never blocks. The real self-map.md is untouched."""
    commit = {"tool_name": "Bash", "tool_input": {"command": "git add -A && git commit -m 'x'"}}
    status = {"tool_name": "Bash", "tool_input": {"command": "git status"}}
    a_read = {"tool_name": "Read", "tool_input": {"file_path": "x"}}
    print("Which tool calls fire the commit-boundary regen (the PreToolUse hook tests this in-script):")
    ok = True
    for label, p, expected in (("git add -A && git commit", commit, True), ("git status", status, False),
                               ("a Read", a_read, False)):
        fired = hooks._is_git_commit(p)
        ok = ok and fired == expected
        print(f"    {'FIRES' if fired else 'skips'} - {label}")
    with tempfile.TemporaryDirectory() as d:
        scratch = os.path.join(d, "self-map.md")
        print("\nWhen it fires it refreshes the self-map (shown on a throwaway copy):")
        gen = generate(scratch)
        print("    " + validate.fmt(gen))
        ok = ok and (gen.get("message") or "").startswith("Wrote")
    print("\nThe hook ALWAYS proceeds: a commit is never blocked, and on any failure the commit still "
          "goes through (the merge-time drift check catches any staleness). Your real "
          ".engine/self-map.md was never touched.")
    if not ok:
        print("\nDEMO UNEXPECTED: a `git commit` must fire the regen (a status/read must not) and the "
              "refresh must write the file.", file=sys.stderr)
        return 1
    return 0


def _demo(_argv: list) -> int:
    """A safe, scripted fail->pass on a THROWAWAY COPY — never touches the committed map."""
    with tempfile.TemporaryDirectory() as d:
        scratch = os.path.join(d, "self-map.md")
        print("Generating the self-map onto a throwaway copy (your committed file is untouched)...")
        print("    " + validate.fmt(generate(scratch)))
        print("(i) Checking it — should be in sync...")
        c1 = check(scratch)
        print("    " + validate.fmt(c1))
        print("(ii) Now hand-editing the copy to simulate drift...")
        with open(scratch, "a", encoding="utf-8", newline="") as fh:
            fh.write("a hand-edited line the generator would never write\n")
        c2 = check(scratch)
        print("    " + validate.fmt(c2))
        print("(iii) Regenerating to heal it...")
        print("    " + validate.fmt(generate(scratch)))
        c3 = check(scratch)
        print("    " + validate.fmt(c3))
        print("Done — a hand-edit was caught (drift) and regeneration restored the file (in sync). "
              "Your real .engine/self-map.md was never touched.")
        ok = c1["severity"] != "hard" and c2["severity"] == "hard" and c3["severity"] != "hard"
    if not ok:
        print("\nDEMO UNEXPECTED: expected in-sync, then drift caught, then in-sync after regen.",
              file=sys.stderr)
        return 1
    return 0


def main(argv: list) -> int:
    cmd = argv[0] if argv else "show"
    try:
        if cmd == "show":
            sys.stdout.write(canonical_map())
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
        print(f"usage: self_map.py {{show|generate|check|demo|hook-demo|hook}} [path]\n"
              f"unknown command {cmd!r}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:  # a malformed source / unwritable path -> plain, no traceback
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
