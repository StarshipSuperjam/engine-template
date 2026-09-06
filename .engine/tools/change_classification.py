#!/usr/bin/env python3
"""change_classification — does a change set touch anything the Engine reads, executes, or owns?

WHY THIS EXISTS. Two gates used to spend the whole self-test inventory on change sets that could not affect
it: engine-ci in a DEPLOYED copy re-ran 4,600 tests on a product-only pull request, and the Build
Coordinator's candidate validation did the same for a project-only Build (StarshipSuperjam/engine-template#758,
StarshipSuperjam/engine-template#883). Both lacked one fact — whether every changed path lies outside the
Engine — and both now read it from here, so the two gates cannot drift apart. The verdict is one of two
words: `project-only`, or `engine-affecting`. Every doubt is `engine-affecting`.

THE FLOOR IS DECLARED, NOT DISCOVERED. The Engine's ownership register (`module_coherence.engine_owned_paths`)
enumerates the LIVE tree, so a file a pull request deletes or renames away is simply absent from it, and the
root wiring file `.mcp.json` — the commands the Engine's MCP servers launch — was never in it at all. So the
floor lives in this module by NAME: the corner prefixes the Engine occupies and the root files it wires or
owns, matched against every changed path including the deleted and rename-source sides, so presence on disk
never matters. The register can only ADD to that floor (a module providing a file outside it, and the
top-level directories the register occupies); shrinking the register cannot widen `project-only` below the
floor. Every register input — the module manifests, the module_coherence source, CODEOWNERS — lives under a
corner, so a pull request that shrinks the register is itself engine-affecting; that is what makes reading
the register from the pull-request head safe, and `test_change_classification` pins it.

WHAT RESOLVES TO ENGINE-AFFECTING, in order: a git failure; the home repository (there is nothing here that is
not the Engine's); an identity that cannot be read; a register that cannot be read, or that lacks the engine
manifest (a degenerate deployment); an empty change set; a path status this module does not recognise; then
any floor file, any corner path, any register path. Only a non-empty change set with every path outside all of
those is `project-only`. The reason is always a token from a closed vocabulary, never free text.

GUARDRAIL-CLASS. This module decides what the frozen `engine-ci` context may skip and what a Build's candidate
validation may skip, with no on-disk correlate a reviewer would notice if it were weakened — the argument that
places `ci_gatekeeper.py` in the weakening guard's hard tier. It is a member of `_FLOOR_ENFORCEMENT_HOOKS`
and of `_HARD_EXACT`: modifying it requires a deliberate acknowledgement. Any helper module this grows joins
both sets in the same change.

Imports stay light at module level (stdlib, `validate`, `repo_identity`): the gate step that calls this is
unconditioned, so an import-time failure would crash the required check instead of resolving to more work.
`module_coherence` — which drags the modes/close/hooks lifecycle machinery — is imported lazily inside the
register read, where a failure resolves to `register-unreadable`.

Run: uv run --directory .engine --frozen -- python tools/change_classification.py classify --base <rev> --head <rev>
     uv run --directory .engine --frozen -- python tools/change_classification.py classify-merge
--help prints this usage and exits 0 without reading the tree; an unknown argument exits 2.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import repo_identity  # noqa: E402
import validate  # noqa: E402

SCHEMA_VERSION = "change-classification.v1"

VERDICT_PROJECT_ONLY = "project-only"
VERDICT_ENGINE_AFFECTING = "engine-affecting"
VERDICTS = frozenset({VERDICT_PROJECT_ONLY, VERDICT_ENGINE_AFFECTING})

# THE DECLARED FLOOR. Corner prefixes: every namespace the Engine occupies in a deployed copy — its own
# directory, the Claude and Codex surfaces it wires hooks and skills into, the agent skill mirrors, and
# .github/ (a sibling workflow can upload an artifact under engine-ci's receipt name, which is why the
# gatekeeper's own threat model treats that whole directory as engine territory). Root files: the foundation
# set that is keyed-merged rather than overlay-replaced, plus the MCP wiring file no manifest names. The
# tests hold these against `module_coherence.FOUNDATION_INFRA`, `_HOME_TRAVEL_PREFIXES` and `WIRING_TARGETS`,
# so a namespace added there cannot be missed here.
FLOOR_PREFIXES = (".engine/", ".claude/", ".codex/", ".agents/", ".github/")
FLOOR_FILES = ("CLAUDE.md", "AGENTS.md", ".gitignore", ".mcp.json")

# The engine manifest, whose absence from a register means the register describes nothing.
ENGINE_MANIFEST_REL = ".engine/engine.json"

# The git `--name-status` letters this module understands. A (added), M (modified), D (deleted), R (renamed),
# C (copied), T (type changed). Anything else — U (unmerged), X, B — is a shape this module has no opinion on.
RECOGNISED_STATUSES = frozenset({"A", "M", "D", "R", "C", "T"})

IDENTITY_DEPLOYED = "deployed"
IDENTITY_HOME = "home"
IDENTITY_UNREADABLE = "unreadable"

# The complete, closed vocabulary of reasons.
ENGINE_AFFECTING_REASONS = frozenset({
    "git-unavailable",         # a git command failed, or its output could not be parsed
    "home-repository",         # the engine's own home: everything here is the Engine's
    "identity-unreadable",     # the deployed/home question could not be answered
    "register-unreadable",     # the ownership register could not be read
    "register-degenerate",     # the register read cleanly but does not name the engine manifest
    "no-changed-paths",        # nothing changed; there is no basis to narrow anything
    "not-a-merge-checkout",    # asked to classify a merge checkout whose HEAD is not a two-parent merge
    "unrecognised-status",     # a changed path carries a git status this module has no opinion on
    "floor-path",              # a changed path is a declared root file the Engine wires or owns
    "engine-corner-path",      # a changed path is under a declared Engine corner
    "engine-owned-path",       # a changed path is in the live register or under a directory it occupies
})
PROJECT_ONLY_REASON = "project-only"
REASONS = ENGINE_AFFECTING_REASONS | {PROJECT_ONLY_REASON}


class ClassificationError(RuntimeError):
    """A condition the caller must not paper over — distinct from a verdict, which is always produced."""


# --------------------------------------------------------------------------------------------------
# The pure half: given changed paths, an identity and a register, decide. No git, no clock.
# --------------------------------------------------------------------------------------------------

def floor_hit(path: str, *, floor_prefixes=FLOOR_PREFIXES, floor_files=FLOOR_FILES) -> Optional[str]:
    """The floor reason for `path`, or None. A pure string test: presence on disk never matters."""
    if path in floor_files:
        return "floor-path"
    if any(path.startswith(prefix) for prefix in floor_prefixes):
        return "engine-corner-path"
    return None


def register_corners(register) -> frozenset:
    """The top-level directories the register occupies, as `dir/` prefixes. A file inside one of them
    that no manifest names is still the Engine's territory."""
    return frozenset(p.split("/", 1)[0] + "/" for p in register if "/" in p)


def register_hit(path: str, register, corners) -> bool:
    return path in register or any(path.startswith(prefix) for prefix in corners)


def _name_paths(paths, limit: int = 3) -> str:
    shown = ", ".join(paths[:limit])
    return shown if len(paths) <= limit else f"{shown} (+{len(paths) - limit} more)"


def classify_paths(entries, *, identity: str, register, base: Optional[str] = None,
                   head: Optional[str] = None, failure: Optional[str] = None,
                   shape_failure: Optional[str] = None,
                   floor_prefixes=FLOOR_PREFIXES, floor_files=FLOOR_FILES) -> dict:
    """Decide, and return the `change-classification.v1` manifest.

    `entries` is a list of `(path, status)` pairs as `diff_entries` returns them — a rename or copy
    contributes BOTH sides. `identity` is one of the IDENTITY_* tokens. `register` is the set of
    engine-owned relpaths for the tree, or None when it could not be read. `failure` is a git diagnosis,
    or None; `shape_failure` says the checkout was not the merge shape the caller needed. Everything the
    verdict rests on is an argument, so a fixture over a synthetic tree cannot silently inherit the real
    repository's answer."""
    changed = sorted({p for p, _ in entries})

    def verdict(code: str, detail: str, engine_paths=(), project_paths=()) -> dict:
        assert code in REASONS, code
        return {
            "schema_version": SCHEMA_VERSION,
            "verdict": VERDICT_PROJECT_ONLY if code == PROJECT_ONLY_REASON else VERDICT_ENGINE_AFFECTING,
            "reason": {"code": code, "detail": detail},
            "identity": identity,
            "base": base,
            "head": head,
            "changed_paths": changed,
            "engine_paths": sorted(set(engine_paths)),
            "project_paths": sorted(set(project_paths)),
            "floor": {"prefixes": list(floor_prefixes), "files": list(floor_files)},
        }

    if failure is not None:
        return verdict("git-unavailable", failure)
    if shape_failure is not None:
        return verdict("not-a-merge-checkout", shape_failure)
    if identity == IDENTITY_HOME:
        return verdict("home-repository", "this is the engine's own home repository; every path is the Engine's")
    if identity != IDENTITY_DEPLOYED:
        return verdict("identity-unreadable", "whether this checkout is a deployed copy could not be determined")
    if register is None:
        return verdict("register-unreadable", "the engine ownership register could not be read")
    register = set(register)
    if ENGINE_MANIFEST_REL not in register:
        return verdict("register-degenerate",
                       f"the register does not name {ENGINE_MANIFEST_REL}; it describes nothing this module can trust")
    if not entries:
        return verdict("no-changed-paths", "no path differs between the two revisions")

    statuses: dict = {}
    for path, status in entries:
        statuses.setdefault(path, set()).add(status)
    odd = sorted(p for p, s in statuses.items() if not s <= RECOGNISED_STATUSES)
    if odd:
        return verdict("unrecognised-status",
                       f"{_name_paths(odd)} carry a git status this module has no opinion on", engine_paths=odd)

    corners = register_corners(register)
    engine_paths: list = []
    project_paths: list = []
    first_code: Optional[str] = None
    for path in changed:
        code = floor_hit(path, floor_prefixes=floor_prefixes, floor_files=floor_files)
        if code is None and register_hit(path, register, corners):
            code = "engine-owned-path"
        if code is None:
            project_paths.append(path)
            continue
        engine_paths.append(path)
        first_code = first_code or code
    if engine_paths:
        return verdict(first_code, f"{_name_paths(engine_paths)} touch what the Engine owns",
                       engine_paths=engine_paths, project_paths=project_paths)
    return verdict(PROJECT_ONLY_REASON,
                   f"{_name_paths(project_paths)} lie outside everything the Engine reads, executes, or owns",
                   project_paths=project_paths)


# --------------------------------------------------------------------------------------------------
# The impure half: git, identity, register. Each failure is a value the pure half turns into a verdict.
# --------------------------------------------------------------------------------------------------

def _git(root: str, *args: str):
    """Run one git command against `root`; return (ok, stdout). Never raises."""
    try:
        proc = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, check=False)
    except OSError as exc:
        return False, str(exc)
    if proc.returncode != 0:
        return False, proc.stderr.strip()
    return True, proc.stdout


def diff_entries(root: str, base: str, head: str):
    """`(entries, failure)`: the `--name-status` rows between two revisions, a rename or copy contributing
    both sides. On failure `entries` is empty and `failure` says why."""
    ok, out = _git(root, "diff", "--name-status", "-z", base, head)
    if not ok:
        return [], f"git diff {base[:12]}..{head[:12]} failed: {out}"
    fields = [f for f in out.split("\0") if f != ""]
    entries: list = []
    i = 0
    while i < len(fields):
        status = fields[i]
        letter = status[:1]
        if letter in ("R", "C"):
            if i + 2 >= len(fields):
                return [], "git diff returned a truncated rename record"
            entries.append((fields[i + 1], letter))
            entries.append((fields[i + 2], letter))
            i += 3
        else:
            if i + 1 >= len(fields):
                return [], "git diff returned a truncated record"
            entries.append((fields[i + 1], letter))
            i += 2
    return entries, None


def identity_of(root: str) -> str:
    """Deployed, home, or unreadable — through the STRICT predicate, so a malformed manifest is a doubt
    rather than a quiet 'home'. (Home is the safe answer too; the distinction is what the report says.)"""
    try:
        return IDENTITY_DEPLOYED if repo_identity.is_downstream_copy_strict(root) else IDENTITY_HOME
    except Exception:  # noqa: BLE001 — any failure here is a doubt, and doubt runs everything
        return IDENTITY_UNREADABLE


def register_of(root: str):
    """The engine-owned relpaths of the tree at `root`, or None when they cannot be read. The import is
    lazy on purpose (see the module docstring)."""
    try:
        import module_coherence
        manifests = module_coherence.discover_manifests(root)
        return set(module_coherence.engine_owned_paths(manifests, root=root))
    except Exception:  # noqa: BLE001 — an unreadable register is a doubt, never a crash of the gate
        return None


def merge_parents(root: str):
    """`(base, head)` when HEAD is a two-parent merge (a `refs/pull/N/merge` checkout records parent 1 =
    the base tip and parent 2 = the pull-request head), else None."""
    ok, out = _git(root, "log", "-1", "--format=%P")
    if not ok:
        return None
    parents = out.split()
    if len(parents) != 2:
        return None
    return parents[0], parents[1]


def classify_range(root: str, base: str, head: str) -> dict:
    """The whole answer for one tree and two revisions."""
    entries, failure = diff_entries(root, base, head)
    return classify_paths(entries, identity=identity_of(root), register=register_of(root),
                          base=base, head=head, failure=failure)


def classify_merge_checkout(root: str) -> dict:
    """The answer for a pull-request merge checkout: the diff of the merge commit against its FIRST parent,
    the base tip, so the change set is exactly the pull request's. A HEAD that is not a two-parent merge is
    a doubt — a one-parent HEAD would make the same diff succeed on a narrower change set."""
    parents = merge_parents(root)
    if parents is None:
        return classify_paths([], identity=identity_of(root), register=None, head="HEAD",
                              shape_failure="HEAD is not a two-parent merge commit, so the change set "
                                            "cannot be bounded to one pull request")
    base, _pr_head = parents
    return classify_range(root, base, "HEAD")


def serialize(manifest: dict) -> str:
    return json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def digest(manifest: dict) -> str:
    return "sha256:" + hashlib.sha256(serialize(manifest).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------------

_USAGE = (
    "usage: change_classification.py classify --base <rev> --head <rev> [--root <dir>]\n"
    "       change_classification.py classify-merge [--root <dir>]\n"
    "Run through the engine tool-runtime: uv run --directory .engine --frozen -- python tools/change_classification.py …\n"
    "Prints the change-classification.v1 manifest and exits 0; the verdict is in the JSON, never the exit status.\n"
    "--help prints this and exits 0 without reading the tree.\n"
)


def main(argv: Optional[list] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        sys.stdout.write(_USAGE)
        return 0
    parser = argparse.ArgumentParser(prog="change_classification.py", add_help=False, usage=_USAGE)
    sub = parser.add_subparsers(dest="verb")
    rng = sub.add_parser("classify", add_help=False)
    rng.add_argument("--base", required=True)
    rng.add_argument("--head", required=True)
    rng.add_argument("--root", default=validate.ROOT)
    merge = sub.add_parser("classify-merge", add_help=False)
    merge.add_argument("--root", default=validate.ROOT)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        sys.stderr.write(_USAGE)
        return 2
    if args.verb == "classify":
        manifest = classify_range(args.root, args.base, args.head)
    elif args.verb == "classify-merge":
        manifest = classify_merge_checkout(args.root)
    else:
        sys.stderr.write(_USAGE)
        return 2
    sys.stdout.write(serialize(manifest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
