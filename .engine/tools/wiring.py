#!/usr/bin/env python3
"""The wiring library — applies and reverses module `wires` over a CLOSED seam vocabulary.

A module touches shared state (Claude settings, MCP registration, the ontology catalog,
`.gitignore`) only through a small, reviewed set of directive types, each with a *guaranteed
reverser*. This library is the permanent home of those applier/reverser pairs — both
provisioning subsystems (the one-time instantiator and the permanent module manager) and the
CODEOWNERS renderer call it, so the wiring logic does not die with the self-deleting
instantiator (systems/grammar/module-system/README.md §"The wiring library").

THE R5 FIREWALL. The seam vocabulary is closed to five types — hook, mcp, ontology-entry,
permission, gitignore — and there is **no `custom/script` escape hatch**: an arbitrary
shared-state mutation with no guaranteed reverser *is* the R5 failure (module-system 99-103).
A new seam is a reviewed change to this file (a new applier/reverser pair), not a runtime
directive. The dispatch is reject-by-default: an unknown type mutates nothing.

REVERSAL KEYS ON ENGINE-NAMESPACED IDENTITY, never bare content (module-system 105-115):
  - hook        -> .claude/settings.json, keyed on {event, matcher, type, command}; command -> .engine/
  - mcp         -> root .mcp.json, keyed on the engine-prefixed server name; never writes approval
  - ontology-entry -> the engine-owned catalog (.engine/schemas/surface-catalog.json), keyed on surface name
  - permission  -> .claude/settings.json; a bare string is NOT namespaceable, so reverse "errs toward
                   leaving it" (a documented no-op) — the worst case is a tolerated residual, never
                   removing one the operator wanted (module-system 110-113)
  - gitignore   -> root .gitignore, engine lines inside a comment-fenced engine-managed block

Apply **inserts iff absent**; reverse **removes only the engine-identified entry** (an operator's
or product's identical-looking entry is left untouched). Every apply and reverse is **idempotent**,
so a crashed half-install is safe to re-run (module-system 117-119).

MUTATOR POSTURE — fail open and flag (the inverse of validate.py's fail-closed checker). On a file
it cannot safely parse, the library does NOT mutate (no blind overwrite of operator data) and does
NOT crash (no traceback to a non-engineer); it returns a plain-language finding.v1 and is re-runnable
once the file is fixed (hooks fail-open-and-flag).

The comment-fenced-block helper (fence_apply/fence_reverse) is a *library helper*, NOT a module
`wires` seam: the `gitignore` seam calls it, and so will the foundation `.engine/.venv/` block and the
CODEOWNERS renderer at provisioning (provisioning 189-195, 254-267).

CLI (the operator-runnable demo — the `gitignore` seam is the only one with a live target today):
  uv run --directory .engine -- python tools/wiring.py demo-gitignore /tmp/demo.gitignore
  uv run --directory .engine -- python tools/wiring.py gitignore-apply   /tmp/mine.gitignore ".engine/.venv/"
  uv run --directory .engine -- python tools/wiring.py gitignore-reverse /tmp/mine.gitignore
"""
from __future__ import annotations
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402


# ---- engine-identity constants (one declared convention; module-system 105-115) ------------
# The fence marker (build-spec leaf b, decided with the maintainer): the conventional BEGIN/END
# form plus a plain-language cue, so a non-engineer who opens the file is never confused. `{id}`
# distinguishes coexisting fences (a module id, "tool-runtime-venv", "codeowners", ...). No
# checksum/provenance tag lives in the file — the manifest `wires` block is the complete record
# (module-system 114).
FENCE_BEGIN = "# BEGIN engine-managed block: {id} - do not edit inside"
FENCE_END = "# END engine-managed block: {id}"
# Stable prefixes used to detect a forged marker in a body line, regardless of id.
_FENCE_BEGIN_TOKEN = "# BEGIN engine-managed block:"
_FENCE_END_TOKEN = "# END engine-managed block:"

MCP_NAME_PREFIX = "engine-"               # an engine MCP server name is engine-prefixed (module-system 106)
CLAUDE_PROJECT_DIR = "${CLAUDE_PROJECT_DIR:-.}"  # the literal MCP path form (module-system 155-156); never locally expanded
ENGINE_DIR_MARKER = ".engine/"            # an engine hook command resolves under here (module-system 106)

# Identity-token patterns reused from the repo's existing grammar — the path/marker-injection
# firewall. Anchored with \A...\Z (NOT ^...$): in Python `$` also matches just before a trailing
# newline, so "^[a-z]+$" would admit "core\n" — a token that forges a split fence marker on disk.
# \Z matches only the true end of string, so '/', '\n', '\r', '..', and marker fragments are all rejected.
_ID_RE = re.compile(r"\A[a-z][a-z0-9-]*\Z")        # module ids / fence ids / mcp name suffix (module.v1 id pattern)
_SURFACE_NAME_RE = re.compile(r"\A[a-z][a-z-]*\Z")  # surface names (surface-catalog.schema.json $defs.surfaceName)

# Target files are HARDCODED — never derived from directive content (path-escape firewall).
# Module-level so tests can redirect them to a temp dir.
SETTINGS_PATH = os.path.join(validate.ROOT, ".claude", "settings.json")   # hook, permission
MCP_PATH = os.path.join(validate.ROOT, ".mcp.json")                       # mcp
GITIGNORE_PATH = os.path.join(validate.ROOT, ".gitignore")               # gitignore
CATALOG_PATH = validate.CATALOG_PATH                                      # ontology-entry
CATALOG_SCHEMA_PATH = os.path.join(validate.SCHEMAS_DIR, "surface-catalog.schema.json")


class WiringError(Exception):
    """A refusable directive or an unsafe target — caught and turned into a hard finding;
    the library mutates nothing on a WiringError (fail-open)."""


# ---- small shared helpers --------------------------------------------------------------------

def _rel(path: str) -> str:
    rel = os.path.relpath(path, validate.ROOT)
    return path if rel.startswith("..") else rel.replace(os.sep, "/")


def _loc_opt(path: str):
    rel = os.path.relpath(path, validate.ROOT)
    return None if rel.startswith("..") else {"file": rel.replace(os.sep, "/"), "line": None}


def _ok(msg: str, path: str | None = None) -> dict:
    """A non-blocking 'note' finding (success / no-op / intentional-leave)."""
    return validate.finding("note", msg, _loc_opt(path) if path else None)


def _fail(msg: str, path: str | None = None) -> dict:
    """A 'hard' finding — the flag half of fail-open. report() never renders this as OK."""
    return validate.finding("hard", msg, _loc_opt(path) if path else None)


def _check_id(fence_id: str) -> None:
    if not isinstance(fence_id, str) or not _ID_RE.match(fence_id):
        raise WiringError(f"refused: {fence_id!r} is not a valid engine identity token "
                          f"(lowercase letters, digits and hyphens; must start with a letter).")


# ---- the comment-fenced-block helper (library helper, NOT a wires seam) -----------------------

def _find_fence(lines: list, fence_id: str):
    """Locate the single well-formed begin..end pair for `fence_id`. Returns (begin_idx, end_idx),
    or None if absent. Raises WiringError if the fence is malformed (begin-without-end,
    orphan-end, duplicate-begin, begin-after-end, nesting) — the caller then leaves the file
    UNCHANGED and flags, never guessing a boundary and never deleting to EOF."""
    begin = FENCE_BEGIN.format(id=fence_id)
    end = FENCE_END.format(id=fence_id)
    begins = [i for i, ln in enumerate(lines) if ln == begin]
    ends = [i for i, ln in enumerate(lines) if ln == end]
    if not begins and not ends:
        return None
    if len(begins) == 1 and len(ends) == 1 and begins[0] < ends[0]:
        return begins[0], ends[0]
    raise WiringError(
        f"the engine-managed block '{fence_id}' in this file is malformed "
        f"(found {len(begins)} begin and {len(ends)} end marker(s)); the engine left the file "
        f"unchanged. Remove the stray marker line(s) and re-run.")


def fence_apply(text: str, fence_id: str, body_lines: list) -> str:
    """Insert-iff-absent / replace-only-as-a-block. If the keyed fence is absent, append a fresh
    block; if present, replace only its body between its own markers. Bytes OUTSIDE the fence —
    including an operator line identical to a body line — are never touched. Idempotent: an
    identical re-apply returns identical text. (module-system 108-109, 117; provisioning 254.)"""
    _check_id(fence_id)
    body = list(body_lines)
    for bl in body:
        if not isinstance(bl, str):
            raise WiringError("refused: a line to add is not text.")
        if "\n" in bl or "\r" in bl:
            raise WiringError("refused: a line to add contains a line break.")
        stripped = bl.strip()
        if stripped.startswith(_FENCE_BEGIN_TOKEN) or stripped.startswith(_FENCE_END_TOKEN):
            raise WiringError("refused: a line to add would forge an engine fence marker.")
    lines = text.split("\n")
    span = _find_fence(lines, fence_id)
    block = [FENCE_BEGIN.format(id=fence_id)] + body + [FENCE_END.format(id=fence_id)]
    if span is not None:
        b, e = span
        return "\n".join(lines[:b] + block + lines[e + 1:])
    if text == "":
        return "\n".join(block + [""])
    if lines[-1] == "":                      # text already ends with a newline
        return "\n".join(lines[:-1] + block + [""])
    return "\n".join(lines + block + [""])   # terminate the final line, then append (content preserved)


def fence_reverse(text: str, fence_id: str) -> str:
    """Remove ONLY the named fence's begin..end span; leave every other line byte-identical.
    No-op if the fence is absent. Raises WiringError (→ leave unchanged + flag) if malformed —
    NEVER deletes to EOF on an unterminated fence."""
    _check_id(fence_id)
    lines = text.split("\n")
    span = _find_fence(lines, fence_id)
    if span is None:
        return text
    b, e = span
    return "\n".join(lines[:b] + lines[e + 1:])


# ---- tolerant IO (the mutator posture: fail open, never clobber, never crash) -----------------

def _read_json_tolerant(path: str, create: bool):
    """Returns (data_dict, None) or (None, hard_finding). Absent → {} when create else a hard
    'missing' finding; empty/whitespace → {}; malformed JSON or non-object top level → a hard
    finding and NO mutation (the file is left exactly as the operator left it)."""
    if not os.path.exists(path):
        if create:
            return {}, None
        return None, _fail(f"cannot apply: the expected file {_rel(path)} is missing — the "
                           f"repository looks malformed. The engine made no change.", path)
    try:
        text = validate.read(path)
    except OSError as exc:
        return None, _fail(f"could not read {_rel(path)}: {exc}. The engine made no change.", path)
    if text.strip() == "":
        return {}, None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, _fail(f"could not safely update {_rel(path)} because it is not valid JSON "
                           f"({exc.msg}, line {exc.lineno}); the engine left it unchanged. "
                           f"Fix the file and re-run.", path)
    if not isinstance(data, dict):
        return None, _fail(f"could not safely update {_rel(path)}: its top level is not a JSON "
                           f"object; the engine left it unchanged.", path)
    return data, None


def _write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")


def _read_text(path: str) -> str:
    return validate.read(path) if os.path.exists(path) else ""


def _write_text(path: str, text: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _json_apply(path: str, transform, success: str, noop: str, create: bool) -> dict:
    """Tolerant read → pure transform → write-iff-changed. transform(data) -> (data, changed)
    and may raise WiringError. All error paths fail open with a hard finding; success/no-op
    return a note."""
    try:
        data, err = _read_json_tolerant(path, create=create)
        if err is not None:
            return err
        data, changed = transform(data)
    except WiringError as exc:
        return _fail(str(exc), path)
    if changed:
        _write_json(path, data)
        return _ok(success, path)
    return _ok(noop, path)


def _text_fence_apply(path: str, fence_id: str, body_lines: list, create: bool) -> dict:
    try:
        text = _read_text(path)
        if not os.path.exists(path) and not create:
            return _fail(f"cannot apply: {_rel(path)} is missing. The engine made no change.", path)
        new = fence_apply(text, fence_id, body_lines)
    except WiringError as exc:
        return _fail(str(exc), path)
    if new != text:
        _write_text(path, new)
        return _ok(f"Added the engine-managed block '{fence_id}' to {_rel(path)}.", path)
    return _ok(f"Nothing to change - the engine-managed block '{fence_id}' is already present "
               f"in {_rel(path)}.", path)


def _text_fence_reverse(path: str, fence_id: str) -> dict:
    try:
        text = _read_text(path)
        new = fence_reverse(text, fence_id)
    except WiringError as exc:
        return _fail(str(exc), path)
    if new != text:
        _write_text(path, new)
        return _ok(f"Removed only the engine-managed block '{fence_id}' from {_rel(path)}; "
                   f"your own lines are untouched.", path)
    return _ok(f"Nothing to remove - no engine-managed block '{fence_id}' in {_rel(path)}.", path)


# ---- pure per-seam transforms (data in, (data, changed) out) ----------------------------------

def settings_add_hook(data: dict, directive: dict):
    event = directive["event"]
    matcher = directive["matcher"]
    entry = directive["hook"]
    command = entry.get("command", "")
    if ENGINE_DIR_MARKER not in command:
        raise WiringError("refused: a hook command must point into .engine/ to be an engine-owned "
                          f"hook (got {command!r}).")
    groups = data.setdefault("hooks", {}).setdefault(event, [])
    group = next((g for g in groups if g.get("matcher") == matcher), None)
    if group is None:
        group = {"matcher": matcher, "hooks": []}
        groups.append(group)
    hooks = group.setdefault("hooks", [])
    if any(h.get("type") == entry.get("type") and h.get("command") == entry.get("command")
           for h in hooks):
        return data, False
    hooks.append(dict(entry))
    return data, True


def settings_remove_hook(data: dict, directive: dict):
    event = directive["event"]
    matcher = directive["matcher"]
    entry = directive["hook"]
    groups = (data.get("hooks") or {}).get(event)
    if not groups:
        return data, False
    changed = False
    for group in list(groups):
        if group.get("matcher") != matcher:
            continue
        existing = group.get("hooks") or []
        kept = [h for h in existing
                if not (h.get("type") == entry.get("type")
                        and h.get("command") == entry.get("command"))]
        if len(kept) != len(existing):
            changed = True
            if kept:
                group["hooks"] = kept
            else:
                groups.remove(group)
    if changed and not groups:
        del data["hooks"][event]
    if changed and not data.get("hooks"):   # prune the engine-created container iff now empty
        data.pop("hooks", None)
    return data, changed


def settings_add_permission(data: dict, directive: dict):
    value = directive["value"]
    allow = data.setdefault("permissions", {}).setdefault("allow", [])
    if value in allow:
        return data, False
    allow.append(value)
    return data, True


def _validate_mcp_name(name: str) -> None:
    if not isinstance(name, str) or not name.startswith(MCP_NAME_PREFIX) \
            or not _ID_RE.match(name[len(MCP_NAME_PREFIX):]):
        raise WiringError(f"refused: an MCP server name must be engine-prefixed "
                          f"({MCP_NAME_PREFIX!r} + a valid id); got {name!r}.")


def mcp_add(data: dict, directive: dict):
    name = directive["name"]
    definition = directive["definition"]
    _validate_mcp_name(name)
    servers = data.setdefault("mcpServers", {})
    if servers.get(name) == definition:
        return data, False
    servers[name] = definition
    return data, True


def mcp_remove(data: dict, directive: dict):
    name = directive["name"]
    _validate_mcp_name(name)
    servers = data.get("mcpServers") or {}
    if name in servers:
        del servers[name]
        if not servers:                      # prune the engine-created container iff now empty
            data.pop("mcpServers", None)
        return data, True
    return data, False


def catalog_add(data: dict, directive: dict, schema: dict):
    name = directive["name"]
    record = directive["record"]
    if not isinstance(name, str) or not _SURFACE_NAME_RE.match(name):
        raise WiringError(f"refused: {name!r} is not a valid surface name (lowercase letters and "
                          f"hyphens).")
    surfaces = data.setdefault("surfaces", {})
    if surfaces.get(name) == record:
        return data, False
    surfaces[name] = record
    errors = list(validate.Draft202012Validator(schema).iter_errors(data))
    if errors:
        raise WiringError(f"refused to add the ontology-entry '{name}': the resulting catalog is "
                          f"not schema-valid ({errors[0].message}). The engine made no change.")
    return data, True


def catalog_remove(data: dict, directive: dict):
    name = directive["name"]
    surfaces = data.get("surfaces") or {}
    if name in surfaces:
        del surfaces[name]
        return data, True
    return data, False


# ---- the five directive-level applier/reverser pairs -----------------------------------------

def hook_apply(directive: dict) -> dict:
    return _json_apply(SETTINGS_PATH, lambda d: settings_add_hook(d, directive),
                       "Wired the hook into .claude/settings.json.",
                       "Nothing to change - the hook is already wired.", create=True)


def hook_reverse(directive: dict) -> dict:
    return _json_apply(SETTINGS_PATH, lambda d: settings_remove_hook(d, directive),
                       "Removed the engine hook from .claude/settings.json; other hooks were left "
                       "untouched.",
                       "Nothing to remove - the engine hook is not present.", create=True)


def permission_apply(directive: dict) -> dict:
    return _json_apply(SETTINGS_PATH, lambda d: settings_add_permission(d, directive),
                       "Added the permission to .claude/settings.json.",
                       "Nothing to change - the permission is already present.", create=True)


def permission_reverse(directive: dict) -> dict:
    # Deliberate no-op — "errs toward leaving it" (module-system 110-113). A bare permission cannot
    # be proven engine-only, so the engine never auto-removes it; the residual is surfaced, not silent.
    return _ok(f"Left the permission {directive.get('value')!r} in place by design: a bare "
               f"permission cannot be proven engine-only, so the engine never removes it (you or "
               f"another module may still need it).", SETTINGS_PATH)


def mcp_apply(directive: dict) -> dict:
    return _json_apply(MCP_PATH, lambda d: mcp_add(d, directive),
                       "Registered the MCP server in .mcp.json.",
                       "Nothing to change - the MCP server is already registered.", create=True)


def mcp_reverse(directive: dict) -> dict:
    return _json_apply(MCP_PATH, lambda d: mcp_remove(d, directive),
                       "Removed the engine MCP server from .mcp.json; any operator approval was "
                       "left untouched.",
                       "Nothing to remove - the engine MCP server is not registered.", create=True)


def ontology_entry_apply(directive: dict) -> dict:
    try:
        schema = validate.load_json(CATALOG_SCHEMA_PATH)
    except (OSError, json.JSONDecodeError) as exc:
        return _fail(f"could not load the catalog schema to validate the new record: {exc}.")
    return _json_apply(CATALOG_PATH, lambda d: catalog_add(d, directive, schema),
                       "Added the surface record to the ontology catalog.",
                       "Nothing to change - the surface record is already present.", create=False)


def ontology_entry_reverse(directive: dict) -> dict:
    return _json_apply(CATALOG_PATH, lambda d: catalog_remove(d, directive),
                       "Removed the engine surface record from the ontology catalog.",
                       "Nothing to remove - the surface record is not present.", create=True)


def gitignore_apply(directive: dict) -> dict:
    if "key" not in directive or "lines" not in directive:
        return _fail("refused: a gitignore directive needs 'key' and 'lines'.", GITIGNORE_PATH)
    return _text_fence_apply(GITIGNORE_PATH, directive["key"], directive["lines"], create=True)


def gitignore_reverse(directive: dict) -> dict:
    if "key" not in directive:
        return _fail("refused: a gitignore directive needs 'key'.", GITIGNORE_PATH)
    return _text_fence_reverse(GITIGNORE_PATH, directive["key"])


# ---- the closed dispatch (the R5 firewall as code) -------------------------------------------

_APPLIERS = {
    "hook": hook_apply,
    "mcp": mcp_apply,
    "ontology-entry": ontology_entry_apply,
    "permission": permission_apply,
    "gitignore": gitignore_apply,
}
_REVERSERS = {
    "hook": hook_reverse,
    "mcp": mcp_reverse,
    "ontology-entry": ontology_entry_reverse,
    "permission": permission_reverse,
    "gitignore": gitignore_reverse,
}
SEAMS = frozenset(_APPLIERS)  # the closed seam vocabulary (must equal the module.v1 wires.type enum)


def _dispatch(table: dict, directive: dict, verb: str) -> dict:
    if not isinstance(directive, dict):
        return _fail(f"refused to {verb} wiring: a directive must be an object.")
    seam = directive.get("type")
    try:
        fn = table.get(seam)              # guard an unhashable type (a list/dict) — no traceback
    except TypeError:
        fn = None
    if fn is None:
        return _fail(f"refused to {verb} wiring of unknown seam type {seam!r}. The seam vocabulary "
                     f"is closed to {sorted(table)}; a new seam is a reviewed change to the wiring "
                     f"library, never a runtime directive.")
    try:
        return fn(directive)
    except WiringError as exc:
        return _fail(str(exc))
    except (KeyError, TypeError) as exc:
        return _fail(f"refused to {verb} the {seam} directive: malformed directive ({exc}).")


def apply(directive: dict) -> dict:
    """Apply one wiring directive. Returns a finding.v1 (note on success/no-op, hard on refusal)."""
    return _dispatch(_APPLIERS, directive, "apply")


def reverse(directive: dict) -> dict:
    """Reverse one wiring directive. Returns a finding.v1."""
    return _dispatch(_REVERSERS, directive, "reverse")


def apply_all(directives: list) -> list:
    """Apply each directive independently (idempotent ⇒ safe to re-run); collect findings."""
    return [apply(d) for d in directives]


def reverse_all(directives: list) -> list:
    return [reverse(d) for d in directives]


def is_applied(directive: dict) -> bool:
    """Is this directive's engine entry currently applied in its target file — by FULL CONTENT, not
    just by name? Mirrors each applier's insert-iff-absent test exactly (hook: would-be-no-op;
    gitignore: fence presence; permission: value membership; mcp / ontology-entry: definition /
    record EQUALITY), so a same-name-but-DRIFTED entry reads as NOT applied — an apply would rewrite
    it. Exposed as a reusable predicate so the coherence wiring leg (module_coherence) and the module
    manager (slice 25) build the declared→applied direction without re-deriving. (The orphan-wire
    REVERSE direction — nothing engine-identified applied that no manifest declares — needs a per-seam
    enumerator and remains slice 25.)"""
    seam = directive.get("type") if isinstance(directive, dict) else None
    try:
        if seam == "gitignore":
            return _find_fence(_read_text(GITIGNORE_PATH).split("\n"), directive["key"]) is not None
        if seam in ("hook", "permission"):
            data, err = _read_json_tolerant(SETTINGS_PATH, create=True)
            if err is not None:
                return False
            if seam == "permission":
                return directive["value"] in (data.get("permissions", {}).get("allow") or [])
            _, changed = settings_add_hook(json.loads(json.dumps(data)), directive)
            return not changed  # would-be-no-op ⇒ already present
        if seam == "mcp":
            data, err = _read_json_tolerant(MCP_PATH, create=True)
            # FULL-CONTENT, mirroring mcp_add's `servers.get(name) == definition` (a drifted
            # same-name definition reads as NOT applied, exactly as the applier would rewrite it).
            return err is None and (data.get("mcpServers") or {}).get(directive["name"]) == directive["definition"]
        if seam == "ontology-entry":
            data, err = _read_json_tolerant(CATALOG_PATH, create=True)
            # FULL-CONTENT, mirroring catalog_add's `surfaces.get(name) == record`.
            return err is None and (data.get("surfaces") or {}).get(directive["name"]) == directive["record"]
    except (WiringError, KeyError, TypeError):
        return False
    return False


# ---- CLI (the operator-runnable gitignore demo, on a scratch file) ---------------------------

def _indent(text: str) -> str:
    return "".join("        " + ln + "\n" for ln in text.split("\n") if ln != "") or "        (empty)\n"


def _demo_gitignore(path: str) -> int:
    sample = "build/\n*.log\n"
    _write_text(path, sample)
    print(f"Starting from a sample file ({_rel(path)}) with two of your own lines:")
    print(_indent(sample), end="")
    print("(i) Applying the engine's gitignore directive...")
    print("    " + validate.fmt(_text_fence_apply(path, "demo", [".engine/.venv/"], create=True)))
    print(_indent(_read_text(path)), end="")
    print("(ii) Applying the SAME directive again (should change nothing)...")
    print("    " + validate.fmt(_text_fence_apply(path, "demo", [".engine/.venv/"], create=True)))
    print("(iii) Now add your OWN identical-looking line OUTSIDE the engine block...")
    _write_text(path, _read_text(path) + ".engine/.venv/\n")
    print(_indent(_read_text(path)), end="")
    print("    Reversing the engine directive...")
    print("    " + validate.fmt(_text_fence_reverse(path, "demo")))
    print(_indent(_read_text(path)), end="")
    print(f"Done - your own '.engine/.venv/' line survived; the engine block is gone. "
          f"Delete {path} when finished.")
    return 0


def main(argv: list) -> int:
    if not argv:
        print("usage: wiring.py {demo-gitignore|gitignore-apply|gitignore-reverse} <file> [lines...]",
              file=sys.stderr)
        return 2
    cmd = argv[0]
    try:
        if cmd == "demo-gitignore":
            return _demo_gitignore(argv[1])
        if cmd == "gitignore-apply":
            path, lines = argv[1], argv[2:]
            finding = _text_fence_apply(path, "demo", lines, create=True)
            print(validate.fmt(finding))
            return 1 if finding["severity"] == "hard" else 0
        if cmd == "gitignore-reverse":
            path = argv[1]
            finding = _text_fence_reverse(path, "demo")
            print(validate.fmt(finding))
            return 1 if finding["severity"] == "hard" else 0
        print(f"unknown command {cmd!r}", file=sys.stderr)
        return 2
    except IndexError:
        print("CONFIG ERROR: missing the <file> argument.", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
