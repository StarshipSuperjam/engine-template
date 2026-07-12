#!/usr/bin/env python3
"""Module manager — the permanent provisioning primitive that adds and removes engine modules
over a repo's life (systems/infrastructure/provisioning/README.md §"The module manager").

Slice 25b shipped **remove** + the **group-scoped uv-sync derivation**. Slice 25c PR-1 added **add**
(install a module at the current release) + its shared **fetch/overlay** primitive + the **sync-groups**
fixer. Slice 25c PR-2 (this change) added the **engine updater** — `upgrade` (the whole-engine vX -> vY
version move) and the **migrations** machinery it runs. CODEOWNERS rendering + de-bootstrap +
clean whole-engine removal shipped in 25c PR-3.

`upgrade` is the engine updater (provisioning §"Upgrading the engine"): fetch the tagged release (reusing
`_fetch_release_tree`), overlay the engine CODE of the present packages (driven off the present set, so a
deselected module is never resurrected; operator config + gitignored data preserved; `_within_root` fails a
containment escape closed BEFORE any write), apply/reverse wiring deltas, re-render the CODEOWNERS ownership
wall for the new release's engine paths (the design's upgrade re-render — `_refresh_codeowners`), re-sync the
tool-runtime, run the packages' `migrations` in dependency order, run coherence, and land it as a reviewed PR.
A `data` migration is **backup-first**: it is refused (pre-flight, before any overlay) unless a backup seam
is available (memory owns the mechanism, live via `memory.snapshot_for_migration`), so the engine never
changes un-backed-up data. It DEGRADES to the current version on an unreachable release (§5 / R7).
FIXTURE-DEMOED: the real release fetch, the `uv sync` re-sync, the git/PR open, and a real data migration
are exercised by fixtures, not by a live release in this template repo (which cuts no releases of itself)
— the named inductive gaps.

`add` is the mirror of `remove` (provisioning §"The module manager: add"): fetch the module's files from
the tagged release, copy its `provides` into their surface homes, copy in its manifest, apply its `wires`,
record it in the engine manifest at its version, re-derive the dependency-group selection, and re-run
coherence. It refuses — in plain language — an already-installed module, a fetch whose manifest id does
not match, or a declared dependency that is absent / outside its range (plan_add, reusing the coherence
range rule so it stays single-homed). The release FETCH is one injectable boundary (_fetch_release_tree —
the tag's source archive) so the tests and the demo run the REAL overlay/wire/coherence on a local tree
and never touch the network; the concrete fetch is the named inductive gap (the construction repo has no
releases to exercise it).

`remove` is **manifest-derived reversal** (module-system §Lifecycle "Uninstall"): reverse the
module's declared `wires` (via the wiring library), delete the engine-identified files it
`provides`, drop it from the engine manifest, re-derive the tool-runtime dependency groups, and
re-run coherence. It is **reverse-dependency-aware** — it refuses, in plain language naming the
dependents, to remove a module another present module still `depends` on — and it declines a
**required** module (the permanent spine; removing the whole engine is a separate clean-removal
step — remove_engine). It touches **no** control-plane ruleset: an ordinary remove changes only
what runs INSIDE the stable engine CI check, not the bound check name, so it needs no operator-
privileged step (provisioning §"The ruleset is the exception"). A `permission` a module added is
**left in place** and disclosed — a bare permission is not engine-identifiable, so reversal errs
toward leaving it (module-system §"The wiring library").

The **uv-sync derivation** (provisioning §"Tool-runtime bootstrap"): each dep-carrying module
declares a [dependency-group] in .engine/pyproject.toml NAMED BY ITS `id`; the sync selection is
those group names that match a PRESENT manifest id, under PEP 735 name normalization. It reuses the
id the manifest already carries — it adds no manifest field. `remove` re-derives and rewrites
`[tool.uv] default-groups` so the CI/local `uv sync` selection stays correct without hand-
maintenance (the seam the pyproject comment cedes to "slice 25's module manager").

Read-only discovery is reused from module_coherence (one present-set reader, no drift):
discover_manifests / load_engine_manifest / provides_claims / check_coherence.

CLI:
  python tools/module_manager.py status              # present modules, reverse-deps, group sync
  python tools/module_manager.py sync-groups         # re-derive + rewrite [tool.uv] default-groups
  python tools/module_manager.py add <id> [--json]   # fetch + install a module at the current release
  python tools/module_manager.py plan-remove <id>    # read-only: refusal reasons / what remove would do
  python tools/module_manager.py remove <id> [--json]
  python tools/module_manager.py upgrade [ref] [--json]  # the engine updater: whole-engine vX -> vY
  python tools/module_manager.py demo                # mutation-free fail-then-pass (remove + add + upgrade; fixtures)
"""
from __future__ import annotations
import contextlib
import glob
import io
import json
import os
import re
import shutil
import sys
import tempfile

try:
    import tomllib  # stdlib, Python >=3.11 (the runtime's requires-python)
except ModuleNotFoundError:  # pragma: no cover - the runtime guarantees >=3.11
    tomllib = None

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402  (finding.v1 + ROOT + read)
import wiring            # noqa: E402  (the wiring library: reverse_all, apply, the shared-file constants)
import module_coherence  # noqa: E402  (the present-set reader + the coherence legs)
import bootstrap         # noqa: E402  (ControlPlane.de_bootstrap — the clean-removal control-plane leg; one-way)


# ---- paths (computed from validate.ROOT at CALL time so a test/demo can redirect ROOT) --------

def _engine_manifest_path() -> str:
    return os.path.join(validate.ROOT, module_coherence.ENGINE_MANIFEST_REL)


def _pyproject_path() -> str:
    return os.path.join(validate.ROOT, ".engine", "pyproject.toml")


def _modules_dir(module_id: str) -> str:
    return os.path.join(validate.ROOT, ".engine", "modules", module_id)


def _write_json(path: str, data) -> None:
    """2-space-indent + trailing-newline JSON writer (mirrors wiring._write_json) so an
    operator's later diff of engine.json stays minimal."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")


# ---- group-scoped uv-sync derivation (pure where it counts) -----------------------------------

def normalize_pep735(name: str) -> str:
    """PEP 735 dependency-group name normalization (the PEP 503 rule it references): lowercase, and
    collapse every run of [-_.] to a single '-'. A module id (^[a-z][a-z0-9-]*$) already normalizes
    to itself, so id<->group matching is exact for well-formed ids."""
    return re.sub(r"[-_.]+", "-", name or "").lower()


def declared_dependency_groups(pyproject_path: str | None = None) -> set:
    """The [dependency-groups] names declared in pyproject.toml, PEP 735-normalized. Read-only
    (tomllib — exactly its remit)."""
    path = pyproject_path or _pyproject_path()
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    return {normalize_pep735(g) for g in (data.get("dependency-groups") or {})}


def committed_default_groups(pyproject_path: str | None = None) -> list:
    """The [tool.uv] default-groups currently committed in pyproject.toml, PEP 735-normalized."""
    path = pyproject_path or _pyproject_path()
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    groups = ((data.get("tool") or {}).get("uv") or {}).get("default-groups") or []
    return [normalize_pep735(g) for g in groups]


def derive_uv_groups(manifests: list | None = None, pyproject_path: str | None = None) -> list:
    """The uv-sync group selection: the [dependency-groups] names that match a PRESENT manifest id,
    under PEP 735 normalization, sorted. A module with no Python dependencies declares no group, so it
    simply isn't in the intersection and contributes nothing to the sync (installed-means-present).
    Adds no manifest field — it reuses the id the manifest already carries."""
    if manifests is None:
        manifests = module_coherence.discover_manifests()
    present = {normalize_pep735(m.get("id", "")) for _p, m in manifests}
    return sorted(present & declared_dependency_groups(pyproject_path))


# Anchored to a SINGLE line ([^\]\n]* never crosses a newline), so a multi-line default-groups array
# does not match -> the caller fails open (a plain note, no write) rather than silently collapsing the
# operator's formatting. The committed selection is single-line, so normal operation is unaffected.
_DEFAULT_GROUPS_RE = re.compile(r"(?m)^(?P<pre>[ \t]*default-groups[ \t]*=[ \t]*)\[[^\]\n]*\][ \t]*$")


def rewrite_default_groups_text(text: str, new_groups: list) -> tuple:
    """Pure minimal-diff rewrite: replace the single-line `default-groups = [...]` array literal with
    `new_groups`, preserving every other byte (the comment block, [project], [dependency-groups]).
    Returns (new_text, changed). Raises ValueError if the line is absent, appears more than once, or is
    written as a multi-line array (the regex matches only a single line) — the caller fails open and
    never blind-writes or silently reformats (the wiring-library mutator posture). No TOML writer
    library is used (none is a dependency); tomllib reads, this rewrites the one line."""
    matches = list(_DEFAULT_GROUPS_RE.finditer(text))
    if len(matches) != 1:
        raise ValueError(f"expected exactly one tool-runtime dependency-group selection line to "
                         f"update, found {len(matches)}; left the configuration unchanged.")
    rendered = "[" + ", ".join(f'"{g}"' for g in new_groups) + "]"
    m = matches[0]
    new_text = text[:m.start()] + m.group("pre") + rendered + text[m.end():]
    return new_text, (new_text != text)


def _maybe_rewrite_default_groups(new_groups: list, pyproject_path: str | None = None) -> bool:
    path = pyproject_path or _pyproject_path()
    if not os.path.exists(path):
        return False
    text = validate.read(path)
    new_text, changed = rewrite_default_groups_text(text, new_groups)
    if changed:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(new_text)
    return changed


def sync_groups(pyproject_path: str | None = None) -> dict:
    """Re-derive the tool-runtime dependency-group selection from the present module set and rewrite
    `[tool.uv] default-groups` to match. This is the standalone fixer the `uv-group-drift` check points at
    — `add`/`remove` already keep the selection derived as a side effect of changing the set, so this is
    for the rare drift (a hand-edit, a botched merge). Returns {groups, changed}."""
    groups = derive_uv_groups(pyproject_path=pyproject_path)
    changed = _maybe_rewrite_default_groups(groups, pyproject_path)
    return {"groups": groups, "changed": changed}


# ---- remove (pure refusal policy + live mutation glue) ----------------------------------------

def plan_remove(module_id: str, manifests: list | None = None) -> dict:
    """READ-ONLY: would removing `module_id` be refused, and why — or what would remove do? Pure given
    the manifest list (defaults to the live present set), so every refusal path is fixture-testable
    without touching disk. Refusals (plain language), in order:
      - the module is not installed;
      - another present module still `depends` on it (named) — the spec's reverse-dependency refusal,
        checked first so it is the surfaced, actionable reason for a depended-on module;
      - it is `required` (the permanent spine — removing the whole engine is a separate step)."""
    if manifests is None:
        manifests = module_coherence.discover_manifests()
    by_id = {m.get("id"): (p, m) for p, m in manifests}
    if module_id not in by_id:
        return {"module_id": module_id, "refused": True,
                "reason": f"There is no module named '{module_id}' installed."}
    _path, target = by_id[module_id]
    dependents = sorted(m.get("id") for _p, m in manifests
                        if m.get("id") != module_id and module_id in (m.get("depends") or {}))
    if dependents:
        names = ", ".join(f"'{d}'" for d in dependents)
        many = len(dependents) > 1
        word, verb, those = ("modules", "need", "those") if many else ("module", "needs", "that one")
        return {"module_id": module_id, "refused": True,
                "reason": f"Can't remove '{module_id}' — the {names} {word} still {verb} it. Remove "
                          f"{those} first, or keep '{module_id}'."}
    if target.get("status") == "required":
        return {"module_id": module_id, "refused": True,
                "reason": f"'{module_id}' is a required part of the engine and can't be removed on its "
                          f"own — removing it would break the engine. (Removing the engine entirely is a "
                          f"separate step.)"}
    return {"module_id": module_id, "refused": False, "reason": None,
            "status": target.get("status"), "wires": list(target.get("wires") or [])}


def _permission_residue(target: dict) -> list:
    """The plain-language disclosure for every `permission` the module added that remove leaves behind —
    names the value, the file, the reason, and that it is safe to remove by hand (F6)."""
    out = []
    for d in (target.get("wires") or []):
        if isinstance(d, dict) and d.get("type") == "permission":
            v = d.get("value")
            out.append(f'The permission "{v}" in .claude/settings.json was left in place. The engine '
                       f"can't be sure it belongs only to this module and not also to your own setup, so "
                       f"it never removes a shared permission. If it was only for this module, you can "
                       f"remove it yourself.")
    return out


def remove(module_id: str) -> dict:
    """Remove one installed module (manifest-derived reversal). Returns a structured result; the CLI
    renders it in plain language. Refuses (no mutation) per plan_remove; otherwise reverses wiring,
    deletes the engine-identified files it owns + its manifest folder, drops it from engine.json,
    re-derives the tool-runtime dependency groups, and re-runs coherence."""
    manifests = module_coherence.discover_manifests()
    plan = plan_remove(module_id, manifests)
    if plan["refused"]:
        plan["applied"] = False
        return plan
    by_id = {m.get("id"): (p, m) for p, m in manifests}
    manifest_path, target = by_id[module_id]
    result = {"module_id": module_id, "refused": False, "applied": True,
              "reversed": [], "left_in_place": _permission_residue(target),
              "deleted": [], "groups_after": None, "findings": []}

    # (1) reverse the module's wiring (idempotent; permission no-op leaves honest residue)
    for f in wiring.reverse_all(target.get("wires") or []):
        result["reversed"].append(validate.fmt(f))

    # (2) delete the engine-identified files the module owns — sole-owner, at ANY path (the reversal law
    #     deletes the engine-identified files a module provides regardless of where they live; whole-engine
    #     remove_engine already does this, so a per-module remove that stopped at .engine/ left a removed
    #     module's .claude/ personas + skills orphaned on disk — #409 U16). A module's `provides` are always
    #     wholly engine-owned files; anything shared with the operator (a settings.json hook, a permission)
    #     arrives via `wires` and is reversed in step (1), so the sole-owner guard is the only gate needed.
    target_claims = module_coherence.provides_claims([(manifest_path, target)])
    others = [(p, m) for p, m in manifests if m.get("id") != module_id]
    other_claims = module_coherence.provides_claims(others)
    for rel in sorted(target_claims):
        if rel not in other_claims:
            try:
                os.remove(os.path.join(validate.ROOT, rel))
                result["deleted"].append(rel)
            except OSError as exc:
                result["left_in_place"].append(f"Could not delete {rel} ({exc}); remove it by hand.")
    mod_dir = _modules_dir(module_id)
    if os.path.isdir(mod_dir):
        shutil.rmtree(mod_dir)
        result["deleted"].append(f".engine/modules/{module_id}/")

    # (3) drop the module from the engine manifest
    engine = module_coherence.load_engine_manifest()
    if engine and module_id in (engine.get("packages") or {}):
        del engine["packages"][module_id]
        _write_json(_engine_manifest_path(), engine)

    # (4) re-derive + rewrite the tool-runtime dependency-group selection for the remaining set
    try:
        new_groups = derive_uv_groups(manifests=others)
        result["groups_after"] = new_groups
        _maybe_rewrite_default_groups(new_groups)
    except (OSError, ValueError) as exc:
        result["left_in_place"].append(f"(Could not update the tool-runtime dependency groups: {exc})")
    except Exception as exc:  # tomllib decode / unexpected — fail open, never crash the removal
        result["left_in_place"].append(f"(Could not update the tool-runtime dependency groups: {exc})")

    # (5) confirm the remaining set is consistent
    result["findings"] = module_coherence.check_coherence()
    return result


# ---- fetch / overlay (the shared release machinery: add uses it here; the engine updater reuses
#      it in `upgrade`) ----------------------------------------------------------------------

class _NoPublishedRelease(RuntimeError):
    """The home is reachable but has NO release to resolve (the releases API returned 200 with no
    `tag_name`) — a genuine missing-release condition, distinct from a transport failure, so the caller
    refuses LOUDLY naming the home rather than degrading it as a network problem (#367)."""


def _fetch_release_tree(ref: str, dest_dir: str, repo: str | None = None,
                        token: str | None = None) -> str:
    """Download the engine's SOURCE archive at the tagged release `ref`, extract it under `dest_dir`, and
    return the path to the extracted tree root (the directory that contains `.engine/`). THIS IS THE
    NETWORK BOUNDARY — `add` (and the later updater) accept an injected local `release_tree`, so the tests
    and the demo never reach the network: they pass a local tree and exercise the REAL overlay/wire/
    coherence logic. The concrete download-and-extract below is therefore the named inductive gap a fixture
    cannot discharge (it never runs in the construction repo — there are no releases to fetch).

    Build-spec leaf (recorded): the artifact is the tag's GitHub SOURCE archive (the `tarball` endpoint),
    NOT a curated release asset — the engine ships from one tagged release as one tree
    (module-system §versioning), so the source archive carries every module's files and resolves their
    `provides` globs, and no separate asset-build pipeline exists. `ref` is a TAG, pinned, never a moving
    branch (provisioning §"Upgrading the engine" step 1; supply-chain Risk R7)."""
    import tarfile                # local: only the real network path needs these
    import urllib.request
    import boot                   # lazy: only the real fetch needs the repo slug + token
    slug = repo or boot.repo_slug()
    if not slug:
        raise RuntimeError("could not determine the engine repository to fetch the release from.")
    tok = token if token is not None else boot.gh_token()
    url = f"https://api.github.com/repos/{slug}/tarball/{ref}"
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "engine-module-manager"}
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as resp:
        payload = resp.read()
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tf:
        tops = {n.split("/", 1)[0] for n in tf.getnames() if n}
        if len(tops) != 1:
            raise RuntimeError(f"unexpected release archive layout (top-level entries: {sorted(tops)[:3]}).")
        tf.extractall(dest_dir, filter="data")   # filter='data' blocks path traversal / device entries (py3.12)
    return os.path.join(dest_dir, tops.pop())


def _resolve_release_ref(ref: str | None, repo: str | None = None, token: str | None = None) -> str:
    """Resolve a target release ref to a CONCRETE tag. A pinned tag/sha passes through unchanged; None or
    "latest" is resolved to the repository's latest published release tag via the GitHub releases API — so
    the engine never fetches, runs, or RECORDS a moving ref (the tag-pin is the supply-chain control, R7;
    provisioning §"Upgrading the engine" step 1). THE NETWORK BOUNDARY for ref resolution — only the real
    upgrade path reaches it (the injected release_tree path passes a concrete ref), so it is part of the
    same named inductive gap as the release fetch (never run in the construction repo)."""
    if ref and ref != "latest":
        return ref
    import urllib.request, json as _json, boot   # local: only the real resolve needs these
    slug = repo or boot.repo_slug()
    if not slug:
        raise RuntimeError("could not determine the engine repository to resolve the latest release.")
    tok = token if token is not None else boot.gh_token()
    url = f"https://api.github.com/repos/{slug}/releases/latest"
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "engine-module-manager"}
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as resp:
        tag = (_json.loads(resp.read()) or {}).get("tag_name")
    if not tag:
        raise _NoPublishedRelease("the engine repository has no published release to update to.")
    return tag


def _home_repository() -> str | None:
    """The engine's HOME repository slug (`owner/repo`) recorded in the manifest — the single source of
    truth for where engine updates are fetched from (D-281/D-282; issue #367). None when the manifest
    records no home (a repo generated before this coordinate shipped). The release-fetch callers pass this
    as `repo=` so they resolve the HOME, never the deployed repo's own `origin` (which `boot.repo_slug()`
    returns and which has no engine releases). On a None home the caller REFUSES with a plain remedy and
    never falls back to origin — the engine does not guess a home."""
    engine = module_coherence.load_engine_manifest() or {}
    home = engine.get("home_repository")
    return home if isinstance(home, str) and home.strip() else None


def _release_is_missing(exc: BaseException) -> bool:
    """Split a release-fetch failure into its two operator-distinct outcomes (three-state resolution,
    D-281/D-282). True → the home is recorded but UNRESOLVABLE: the release/repo does not exist (HTTP 404
    — release-less, renamed, or removed home) OR the home is reachable but has no published release at all
    (`_NoPublishedRelease`, a 200 with no tag) — both refused LOUDLY naming the home. False → a transport
    failure (offline / DNS / timeout / other status), which DEGRADES to the current version (§5 / R7).
    urllib raises HTTPError (a URLError subclass) carrying a numeric `.code` for an HTTP status; a bare
    URLError or socket error carries none."""
    import urllib.error
    if isinstance(exc, _NoPublishedRelease):
        return True
    return isinstance(exc, urllib.error.HTTPError) and getattr(exc, "code", None) == 404


def _within_root(rel: str) -> bool:
    """True iff repo-relative `rel` resolves INSIDE validate.ROOT — the overlay containment guard (the
    topology wall: an overlay places only engine-namespaced paths). A `provides` pattern that is absolute or
    climbs out with `..` would otherwise escape the repo; this fails it closed."""
    root = os.path.realpath(validate.ROOT)
    dst = os.path.realpath(os.path.join(validate.ROOT, rel))
    return dst == root or dst.startswith(root + os.sep)


# ---- add (pure refusal policy + live overlay glue) --------------------------------------------

def plan_add(module_id: str, candidate: dict, manifests: list | None = None) -> dict:
    """READ-ONLY: would adding `module_id` (whose fetched manifest is `candidate`) be refused, and why?
    Pure given the candidate manifest + the present set, so every refusal path is fixture-testable. Refusals
    (plain language), in order:
      - the module is already installed;
      - the fetched files do not contain a module whose id matches (a wrong/corrupt fetch);
      - a declared dependency is absent from the present set, or the present version is outside the declared
        range — surfaced by reusing validate.coherence_findings over the PROSPECTIVE set (present + candidate)
        and diffing against the present set, so the range rule stays single-homed with the coherence leg."""
    if manifests is None:
        manifests = module_coherence.discover_manifests()
    by_id = {m.get("id"): m for _p, m in manifests}
    if module_id in by_id:
        return {"module_id": module_id, "refused": True,
                "reason": f"'{module_id}' is already installed."}
    if not isinstance(candidate, dict) or candidate.get("id") != module_id:
        got = candidate.get("id") if isinstance(candidate, dict) else None
        return {"module_id": module_id, "refused": True,
                "reason": f"The fetched files don't contain a module named '{module_id}' "
                          f"(found {got!r} instead); nothing was changed."}
    present = [m for _p, m in manifests]
    base = validate.coherence_findings(present, "hard", "")
    after = validate.coherence_findings(present + [candidate], "hard", "")
    new = [f for f in after if f not in base]
    if new:
        reasons = " ".join(f.get("message", "").strip() for f in new)
        return {"module_id": module_id, "refused": True,
                "reason": f"Can't add '{module_id}' yet — {reasons}"}
    return {"module_id": module_id, "refused": False, "reason": None,
            "version": candidate.get("version"), "wires": list(candidate.get("wires") or [])}


def add(module_id: str, release_tree: str | None = None, ref: str | None = None) -> dict:
    """Add (install) one module at the current engine release (provisioning §"add"): fetch the module's
    files from the tagged release, copy its `provides` into their surface homes, copy in its manifest, apply
    its `wires`, record it in the engine manifest, re-derive the tool-runtime dependency-group selection,
    and re-run coherence. Re-adding a module deselected at first run is this same path (its files were
    deleted). `release_tree` injects a local extracted release tree (the fetch boundary) for tests/the demo;
    None fetches the current release for real. Returns a structured result; the CLI renders it in plain
    language. Refuses (no mutation) per plan_add."""
    if not re.fullmatch(r"[a-z][a-z0-9-]*", module_id or ""):
        # bound a CLI-supplied id before it is ever path-joined (defense in depth; the manifest schema's
        # id pattern governs committed manifests, not this argument)
        return {"module_id": module_id, "refused": True, "applied": False,
                "reason": f"'{module_id}' is not a valid module id (lower-case letters, digits and hyphens, "
                          f"starting with a letter); nothing was changed."}
    manifests = module_coherence.discover_manifests()
    if module_id in {m.get("id") for _p, m in manifests}:
        return {"module_id": module_id, "refused": True, "applied": False,
                "reason": f"'{module_id}' is already installed."}
    result = {"module_id": module_id, "refused": False, "applied": False, "version": None,
              "copied": [], "applied_wires": [], "groups_after": None, "notes": [], "findings": []}
    tmp = None
    try:
        if release_tree is None:
            engine = module_coherence.load_engine_manifest()
            target_ref = ref or (engine or {}).get("engine_release")
            if not target_ref:
                return {"module_id": module_id, "refused": True, "applied": False,
                        "reason": "could not determine which engine release to fetch the module from."}
            # A module's files come from the engine's HOME release too, never this repo's own origin
            # (D-281/D-282; #367). Absent home -> refuse with a remedy; never fall back to origin.
            home = _home_repository()
            if not home:
                return {"module_id": module_id, "refused": True, "applied": False,
                        "reason": f"This engine has no update home recorded, so it can't fetch '{module_id}'. "
                                  f"Tell me the repository your engine updates from (for example your-org/your-engine) and I'll "
                                  f"record it, then you can add the module again. Nothing was changed."}
            tmp = tempfile.mkdtemp(prefix="engine-add-")
            try:
                release_tree = _fetch_release_tree(target_ref, tmp, repo=home)
            except Exception as exc:
                if _release_is_missing(exc):   # recorded home, but no such release/repo -> refuse, NAME it
                    return {"module_id": module_id, "refused": True, "applied": False,
                            "reason": f"Couldn't find release '{target_ref}' at your engine's update home, "
                                      f"{home}, to add '{module_id}' — that home may have no such release, or "
                                      f"it may have been renamed or removed. Nothing was changed. If the home "
                                      f"is wrong, update the recorded home and try again."}
                return {"module_id": module_id, "refused": True, "applied": False,   # transport -> degrade
                        "reason": f"Couldn't reach your engine's update home, {home}, to add '{module_id}' — "
                                  f"the network may be down, or the home may not be reachable right now. "
                                  f"Nothing was changed. ({exc})"}
        candidate_path = os.path.join(release_tree, ".engine", "modules", module_id, "manifest.json")
        if not os.path.isfile(candidate_path):
            return {"module_id": module_id, "refused": True, "applied": False,
                    "reason": f"The engine release does not contain a module named '{module_id}'."}
        candidate = validate.load_json(candidate_path)
        plan = plan_add(module_id, candidate, manifests)
        if plan["refused"]:
            plan["applied"] = False
            return plan

        # (1) collect the module's provided files from the release tree (same relpaths). The `provides`
        #     contract scopes a module's globs to its own files (the ownership leg enforces non-overlap).
        #     CONTAINMENT GUARD (the topology wall): every destination must resolve INSIDE the engine tree —
        #     an absolute or `..`-climbing pattern is refused before anything is copied, never written
        #     outside ROOT (the spec's "overlay only engine-namespaced paths" law, enforced not assumed).
        result["version"] = candidate.get("version")
        to_copy = []
        for _group, patterns in (candidate.get("provides") or {}).items():
            for pattern in patterns:
                for src in sorted(glob.glob(os.path.join(release_tree, pattern), recursive=True)):
                    if os.path.isfile(src):
                        to_copy.append((src, os.path.relpath(src, release_tree).replace(os.sep, "/")))
        escapes = [rel for _src, rel in to_copy if not _within_root(rel)]
        if escapes:
            shown = ", ".join(escapes[:3]) + ("…" if len(escapes) > 3 else "")
            return {"module_id": module_id, "refused": True, "applied": False,
                    "reason": f"Refused to add '{module_id}': it tried to place files outside the engine "
                              f"({shown}). Nothing was changed."}
        for src, rel in to_copy:
            dst = os.path.join(validate.ROOT, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
            result["copied"].append(rel)
        # (2) copy in the module's manifest
        dst_manifest = os.path.join(_modules_dir(module_id), "manifest.json")
        os.makedirs(os.path.dirname(dst_manifest), exist_ok=True)
        shutil.copyfile(candidate_path, dst_manifest)
        result["copied"].append(f".engine/modules/{module_id}/manifest.json")
        # (3) apply the module's wiring (the real appliers)
        for f in wiring.apply_all(candidate.get("wires") or []):
            result["applied_wires"].append(validate.fmt(f))
        # (4) record it in the engine manifest at its version
        engine = module_coherence.load_engine_manifest() or {"packages": {}}
        engine.setdefault("packages", {})[module_id] = candidate.get("version")
        _write_json(_engine_manifest_path(), engine)
        # (5) re-derive + rewrite the dependency-group selection now that module_id is present. (The
        #     module's [dependency-groups] declaration + its uv.lock entries ship with the engine, so add
        #     flips only the SELECTION; an engine upgrade is what introduces a wholly new declaration.)
        try:
            new_groups = derive_uv_groups()
            result["groups_after"] = new_groups
            _maybe_rewrite_default_groups(new_groups)
        except Exception as exc:  # OSError / ValueError / tomllib decode — fail open, never crash the add
            result["notes"].append(f"(Could not update the tool-runtime dependency groups: {exc})")
        # (6) confirm the resulting set is consistent
        result["applied"] = True
        result["findings"] = module_coherence.check_coherence()
        return result
    finally:
        if tmp and os.path.isdir(tmp):
            shutil.rmtree(tmp, ignore_errors=True)


# ---- engine upgrade + migrations (the engine updater: provisioning §"Upgrading the engine" +
#      §"Migration and reversibility"). FIXTURE-DEMOED — four boundaries never run in the construction
#      repo: (1) the real release FETCH (no releases), (2) the `uv sync` RE-SYNC from the overlaid lock,
#      (3) the git/PR OPEN, (4) a real DATA migration + its backup (the live `memory.snapshot_for_migration`
#      seam). Each is
#      injectable/skipped so tests + the demo run the REAL overlay / runner / coherence logic; "works on
#      the fixture ⇒ works for a real adopter" is the inductive step the fixture cannot discharge. ------

_UNSET = object()   # sentinel: "no GitHub boundary passed (resolve close._github)" vs "offline (None)"

# Root CLAUDE.md is keyed-MERGED on upgrade, not wholesale-overlaid: it carries the engine's `floor` as a
# comment-fenced section so a brownfield adopter's own CLAUDE.md co-exists with the engine's entries rather
# than being seized (repository-topology law 1; the #234/#272 coexistence obligation). The floor is sourced
# from the release's CLAUDE.deployed.md by `_merge_claude_floor`.
_ROOT_CLAUDE_REL = "CLAUDE.md"
_DEPLOYED_FLOOR_REL = "CLAUDE.deployed.md"
_FLOOR_FENCE = "floor"
_GITIGNORE_REL = ".gitignore"           # the foundation-ignores fence lives here (#409 U14) — a shared keyed
#                                         file, so it is block-reversed like CODEOWNERS/CLAUDE.md, never
#                                         overlay-replaced (FOUNDATION_CODE) or wholesale-deleted (remove_engine)

# Engine CODE owned by no module's `provides` but replaced wholesale on upgrade (provisioning L289/L356).
# DERIVED from module_coherence.FOUNDATION_INFRA (the single source of the foundation-artifact set) minus
# the members the overlay must NOT fetch-and-replace: the engine manifest (engine.json — operator config
# whose package versions upgrade bumps in place, identity preserved); CODEOWNERS (re-rendered locally from
# the post-overlay engine path set by upgrade step (2d) / `_refresh_codeowners`, never fetched from a
# release — a release's block would carry the wrong owner + paths); and root CLAUDE.md (keyed-merged by
# `_merge_claude_floor` from the release's CLAUDE.deployed.md so operator content is preserved and the
# release's construction-governance CLAUDE.md never overlays an adopter's floor); and root `.gitignore`
# (the foundation-ignores fence is re-asserted locally by apply_foundation_ignores on upgrade — step (2f)
# below — never fetched, since a release's file would clobber the adopter's own ignore lines + module
# fences). Gitignored data and the deployment's per-instance eADR stream are in no `provides`/FOUNDATION_CODE,
# so the overlay leaves them untouched (config + data preserved). A member may be a glob (the issue
# templates); the overlay loop below expands it against the release tree, so the issue templates are now
# refreshed on update (they were silently omitted before — single-homing closed that gap; forward-only).
FOUNDATION_CODE = tuple(
    p for p in module_coherence.FOUNDATION_INFRA
    if p not in (module_coherence.ENGINE_MANIFEST_REL, ".github/CODEOWNERS", _ROOT_CLAUDE_REL, _GITIGNORE_REL)
)


class _UpgradeRefused(Exception):
    """A clean upgrade refusal carrying a plain-language reason — caught by upgrade() so a refusal returns
    a structured result (no traceback), with nothing applied."""
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# ---- migrations: the backup seam, the loader, the runner, the version-stamp check -----

def _resolve_backup_seam(backup):
    """The pre-migration backup seam a `data` migration uses. An injected callable (tests/demo) wins;
    otherwise MEMORY's snapshot mechanism if memory-substrate is installed AND a backup destination is
    configured, else None. The seam is a callable `seam(store, engine_version) -> a truthy snapshot
    handle`; **None means NO backup is available**, so the no-backup guard refuses every data migration
    (degrade loud, never silently mutate un-backed-up data). "Available" means a backup can actually be
    taken (mechanism installed + a vault set up) — NOT merely that the callable exists — so a repo with
    memory installed but no vault refuses cleanly instead of running a migration that fails mid-snapshot.
    Live via `memory.snapshot_for_migration` (+ `memory.migration_backup_available`): memory owns the
    mechanism AND the restore contract and may not be widened here. The handle's concrete shape is memory's
    leaf (the close._trigger_ambient_capture precedent). The snapshot lands as a distinct, retained git tag the
    routine backup never overwrites — memory's point-in-time pre-migration snapshot (D-264/D-265, resolving #287);
    the restore command targets that tag. This consumer widens nothing of that mechanism."""
    if backup is not None:
        return backup
    try:
        import memory  # noqa: F401 — ImportError (memory not installed) -> no seam
        fn = getattr(memory, "snapshot_for_migration", None)
        if not callable(fn):
            return None
        available = getattr(memory, "migration_backup_available", None)
        if callable(available) and not available():
            return None        # mechanism installed but no backup destination configured -> no backup available
        return fn
    except Exception:  # noqa: BLE001 — any failure obtaining the seam -> treat as "no backup available"
        return None


def _load_migration(module_dir: str, run_rel: str):
    """Load the migration at <module_dir>/<run_rel> and return its migrate(context) callable. Loaded under
    a UNIQUE synthetic module name (so two modules' migration files never collide in sys.modules) via the
    importlib spec loader — no sys.path mutation."""
    import importlib.util   # local: only the migration path needs it
    path = os.path.join(module_dir, run_rel)
    if not os.path.isfile(path):
        raise RuntimeError(f"migration file '{run_rel}' is missing")
    uniq = re.sub(r"[^a-z0-9]+", "_", os.path.relpath(path, validate.ROOT).lower())
    spec = importlib.util.spec_from_file_location(f"engine_migration_{uniq}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fn = getattr(mod, "migrate", None)
    if not callable(fn):
        raise RuntimeError(f"migration '{run_rel}' does not define a migrate(context) function")
    return fn


def select_migrations(from_versions: dict, target_versions: dict, manifests: list) -> list:
    """PURE: the migration entries an upgrade must run, in execution order. For each present module pick
    the `migrations` keys strictly ABOVE its from-version and AT-OR-BELOW its target-version; order modules
    by dependency (validate.topological_order) and, within a module, by ASCENDING version using
    validate._ver_tuple (NEVER string order — '0.10.0' must sort AFTER '0.9.0'). `manifests` is a list of
    manifest dicts; `from_versions`/`target_versions` are {module_id: version}. Returns a list of
    {module_id, version, description, run, kind} — fixture-testable with no disk/network."""
    out = []
    for m in validate.topological_order(list(manifests)):
        mid = m.get("id")
        frm = validate._ver_tuple(from_versions.get(mid, "0"))
        tgt = validate._ver_tuple(target_versions.get(mid, from_versions.get(mid, "0")))
        for ver in sorted((m.get("migrations") or {}), key=validate._ver_tuple):
            if frm < validate._ver_tuple(ver) <= tgt:
                e = (m.get("migrations") or {})[ver] or {}
                out.append({"module_id": mid, "version": ver, "description": e.get("description"),
                            "run": e.get("run"), "kind": e.get("kind")})
    return out


def _bind_migration_id(seam, module_id: str, version: str, reversibility_floor: bool = False):
    """Bind the migration's identity into the backup seam so memory names the pre-migration snapshot collision-free
    by it (the retained-tag mechanism, D-264 law 3). The migration calls `context['backup'](store, engine_version)`
    exactly as before — the migration id rides along, so migration authors need not know about it and module_manager
    stays a pure consumer that knows nothing of the snapshot's tag mechanism. `reversibility_floor` (True only for the
    first data migration of the upgrade — #303) likewise rides along so memory records THAT snapshot as the undo floor;
    module_manager passes only this boolean and never learns the snapshot's tag. Passing the extra kwargs is forward-
    compatible: a seam that ignores them still works (memory falls back to engine-version + generation)."""
    if seam is None:
        return None
    migration_id = f"{module_id}@{version}"

    def _seam(store, engine_version):
        return seam(store, engine_version, migration_id=migration_id, reversibility_floor=reversibility_floor)
    return _seam


def run_migrations(selected: list, from_versions: dict, engine_version: str,
                   module_dir=None, backup=None) -> dict:
    """Run the SELECTED migrations (from select_migrations) in order. `module_dir(module_id)` returns that
    module's directory so `run` resolves (defaults to the live layout). `engine_version` is handed to each
    migration (a data migration stamps its snapshot with it). `backup` injects the seam (tests/demo); None
    resolves the real one. Returns {ran:[...], refused:[...]}.

    `config` migration -> runs directly (a reverted upgrade restores a committed file on its own).
    `data` migration  -> the NO-BACKUP GUARD: with no backup available it is REFUSED (degrade loud,
    nothing run); else the seam is handed to the migration in `context` so it snapshots its OWN store
    BEFORE mutating + stamps it with `engine_version` (backup-first reversibility). The guard is
    belt-and-suspenders with upgrade()'s pre-flight (which refuses the whole upgrade before overlaying if a
    data migration has no backup available), so run_migrations is also safe to call on its own. A data
    migration whose backup FAILS at run time (the seam returns a falsy handle, so its backup-first assert
    fires) is also caught and recorded as a refusal — never a raw traceback; upgrade() then declines to
    open the change for review (see the refused-result check there)."""
    if module_dir is None:
        module_dir = _modules_dir
    seam = _resolve_backup_seam(backup)
    result = {"ran": [], "refused": []}
    floor_taken = False                                  # #303: the FIRST data migration of this upgrade is the
    #                                                      reversibility floor — one run_migrations call == one upgrade
    #                                                      == one reversibility unit (true for the sole caller upgrade()).
    for item in selected:
        mid, ver, kind = item["module_id"], item["version"], item.get("kind")
        if kind == "data" and seam is None:
            result["refused"].append(
                f"Did not update stored data for '{mid}' to {ver}: no data backup is set up yet, and the "
                f"engine never changes stored data it can't first back up. Nothing was changed. Ask me to "
                f"set up a backup, then update again.")
            continue
        is_floor = kind == "data" and not floor_taken
        if kind == "data":
            floor_taken = True
        ctx = {"module_id": mid, "from_version": from_versions.get(mid), "to_version": ver,
               "engine_version": engine_version, "kind": kind,
               "backup": _bind_migration_id(seam, mid, ver, reversibility_floor=is_floor) if kind == "data" else None}
        if kind == "data":
            # Raise the in-flight-migration marker for the snapshot+mutate window so a concurrent compaction
            # refuses within it (the compaction↔migration ordering law, memory/README §269-283). Lazy import (the
            # memory←boot back-edge, as the backup seam already is). FAIL CLOSED: if the marker can't be raised
            # (another memory write holds the single-writer lock), REFUSE the migration rather than run it
            # marker-less — an unguarded snapshot+mutate is exactly the interleave the marker prevents.
            from memory import capture as _capture, ledger as _ledger   # lazy back-edge
            _mig_dir = _ledger.ledger_dir()
            if not _capture.open_migration_window(_mig_dir):
                result["refused"].append(
                    f"Did not update stored data for '{mid}' to {ver}: another memory task was busy, so the "
                    f"update couldn't start safely. Nothing was changed. Try again in a moment.")
                continue
            # A data migration snapshots BEFORE mutating; if that backup can't be taken at run time (the seam
            # returns a falsy handle, so the migration's own backup-first assert fires) it must DEGRADE LOUD —
            # a clean refusal, never a raw traceback to the operator. The pre-flight + readiness probe catch
            # the common "no vault configured" case earlier; this catches a backup that fails at the moment of
            # the snapshot (a vault that went unreachable/public between pre-flight and run). The `finally` lowers
            # the marker whether the migration finished or refused.
            try:
                _load_migration(module_dir(mid), item["run"])(ctx)
            except Exception:  # noqa: BLE001 — backup-first means the failure is before mutating; degrade loud
                result["refused"].append(
                    f"Did not finish updating stored data for '{mid}' to {ver}: its backup could not be "
                    f"completed, so the update was stopped. Ask me to set up or check your backup, then "
                    f"update again.")
                continue
            finally:
                _capture.close_migration_window(_mig_dir)
        else:
            _load_migration(module_dir(mid), item["run"])(ctx)
        result["ran"].append(f"{mid} -> {ver} ({kind})")
    return result


def stamp_mismatch_finding(store_label: str, stamped_version: str, running_version: str,
                           restore_command: str):
    """PURE: the post-revert data-integrity check a data migration owns. After an upgrade pull request is
    reverted, the engine CODE returns to the older version, but a data migration that already reshaped a
    gitignored store is NOT reverted with it (the store is gitignored, outside the pull request). Each data
    migration stamps its snapshot with the engine-code version it ran at; if the running engine code is now
    OLDER than that stamp, the store is ahead of the code. Returns a hard finding.v1 carrying the plain-handle
    restore action, or None when there is no mismatch (running >= stamped). DETECTION is the migration system's
    logic (memory's `restore_vault.detect_migration_revert` is the live caller); SURFACING is boot's existing
    read-only open-findings path (boot needs no change). `restore_command` is a plain-handle action phrase, never
    a raw tag/ref — the finding message is operator-facing (boot.open_findings renders it)."""
    if validate._ver_tuple(running_version) >= validate._ver_tuple(stamped_version):
        return None
    return validate.finding(
        "hard",
        f"Your saved memory was changed by an engine update that isn't in place, so right now your "
        f"memory and the engine don't match. To line them up again, {restore_command}.")


def surface_stamp_mismatch(store_label: str, stamped_version: str, running_version: str,
                           restore_command: str, now: str, github=_UNSET):
    """Surface a detected version-stamp mismatch as ONE tracked engine finding via
    telemetry.promote_finding (NO auto-resolve — never closes other open Issues), which boot then renders
    through its read-only open-findings path. Reuses close's GitHub boundary + finding-record shape.
    Returns the Issue number, or None when there is no mismatch / GitHub is unreachable (the in-session
    surfacing + the merge wall remain). This is a READ-ONLY check — it calls promote_finding, NEVER runs
    migrate() (migration is never triggered at boot). Its live caller is memory's `restore_vault.detect_migration_revert`,
    which runs the offline code-older-than-data check and, when online, calls this to open the durable tracked Issue."""
    f = stamp_mismatch_finding(store_label, stamped_version, running_version, restore_command)
    if f is None:
        return None
    import hashlib            # lazy: this rare path keeps module_manager's common imports lean
    import close              # close owns the GitHub boundary + the finding-record shape (reuse, no copy)
    import telemetry
    gh = close._github() if github is _UNSET else github
    if gh is None:            # offline -> surfaced-in-session-not-tracked; the merge wall is the backstop
        return None
    digest = hashlib.sha1((f.get("message") or "").encode("utf-8")).hexdigest()[:12]
    record = {"source_id": f"migration/version-stamp/{digest}", "severity": telemetry.TRUST_CRITICAL,
              "message": f.get("message"), "location": f.get("location"),
              "first_seen": now, "last_seen": now}
    return telemetry.promote_finding(gh, record, now)


# ---- upgrade: overlay (off the PRESENT set) + wiring deltas + re-sync + migrations + coherence + PR ----

def _overlay_engine_code(release_tree: str, present_ids: list, exclude=None) -> tuple:
    """Overlay the engine CODE of the PRESENT packages from `release_tree`: each present module's
    `provides` files + its manifest, plus the FOUNDATION_CODE infra the release ships. Driven off the
    PRESENT set (never the release tree's modules/*), so a deselected module is NEVER resurrected
    (provisioning L352-356). Operator config (engine.json identity, the policy-override) and gitignored
    data + the per-instance eADR stream are in no `provides`/FOUNDATION_CODE, so they are untouched.
    CONTAINMENT GUARD (the topology wall): every destination must resolve INSIDE ROOT — fail closed BEFORE
    any write (the PR-1 pattern). `exclude` (a set of repo-relative paths) is NOT overwritten — the brownfield
    arrival passes the engine-exclusive paths an operator chose to keep ('leave-as-is', a class-1 collision),
    so the engine coexists around them rather than replacing them. Returns (copied_relpaths,
    {module_id: release_manifest})."""
    skip = set(exclude or ())
    to_copy: dict = {}   # rel -> src (dedup; a manifest also matched by a glob resolves to one entry)
    candidates: dict = {}
    for mid in present_ids:
        man_src = os.path.join(release_tree, ".engine", "modules", mid, "manifest.json")
        if not os.path.isfile(man_src):
            raise _UpgradeRefused(f"the engine release does not contain the installed module '{mid}', so "
                                  f"the update was stopped and nothing was changed.")
        cand = validate.load_json(man_src)
        candidates[mid] = cand
        for _group, patterns in (cand.get("provides") or {}).items():
            for pattern in patterns:
                for src in glob.glob(os.path.join(release_tree, pattern), recursive=True):
                    if os.path.isfile(src):
                        to_copy[os.path.relpath(src, release_tree).replace(os.sep, "/")] = src
        to_copy[f".engine/modules/{mid}/manifest.json"] = man_src
    for member in FOUNDATION_CODE:
        # Glob-expand each foundation member against the release tree (a member may be a glob, e.g.
        # .github/ISSUE_TEMPLATE/*.md; glob.glob on a literal path returns it iff it exists). A literal
        # os.path.isfile on a glob string would silently drop the issue templates.
        for src in glob.glob(os.path.join(release_tree, member), recursive=True):
            if os.path.isfile(src):
                to_copy[os.path.relpath(src, release_tree).replace(os.sep, "/")] = src
    escapes = sorted(rel for rel in to_copy if not _within_root(rel))
    if escapes:
        shown = ", ".join(escapes[:3]) + ("…" if len(escapes) > 3 else "")
        raise _UpgradeRefused(f"the update was stopped because it tried to place files outside the engine "
                              f"({shown}); nothing was changed.")
    copied = []
    for rel, src in sorted(to_copy.items()):
        if rel in skip:                      # an operator file the arrival is keeping (class-1 leave-as-is)
            continue
        dst = os.path.join(validate.ROOT, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
        copied.append(rel)
    return copied, candidates


def _apply_wiring_deltas(old_by_id: dict, new_by_id: dict) -> list:
    """Reverse the wires a module no longer declares and (re)apply the wires it declares now (the
    scenario's 'apply/reverse wiring deltas'). For an unchanged version the delta is empty (apply_all is
    idempotent). A removed engine-identifiable wire is reversed so it does not linger; a same-identity
    content change is re-applied, and if a seam cannot update in place the forward coherence leg (step 5)
    catches the drift. Returns plain-language lines."""
    lines = []
    for mid, new_m in new_by_id.items():
        new_list = new_m.get("wires") or []
        new_ids = {wiring.declared_wire_identity(w) for w in new_list} - {None}
        for w in (old_by_id.get(mid) or {}).get("wires") or []:
            k = wiring.declared_wire_identity(w)
            if k is not None and k not in new_ids:     # wire removed in the new version -> reverse it
                lines.append(validate.fmt(wiring.reverse(w)))
        for f in wiring.apply_all(new_list):           # apply the new version's wires (idempotent)
            lines.append(validate.fmt(f))
    return lines


def _bump_engine_manifest(target_versions: dict, engine_release: str) -> dict:
    """Update the engine manifest in place: set engine_release + each present package's version to the
    release's, PRESERVING identity and any other operator-owned keys (engine.json is operator config, not
    overlaid). Returns the new manifest."""
    engine = module_coherence.load_engine_manifest() or {"packages": {}}
    # Release tags are v-prefixed (`v0.1.0` — the git/GitHub convention and what the maintainer sees), but
    # `engine_release` is stored BARE so it matches the bare per-package versions below; otherwise the
    # manifest would carry the engine version as `v0.1.0` and every package as `0.1.0` — a self-inconsistent
    # store the next cut's floor derivation would then oscillate. Strip a single leading `v` (comparison
    # already v-strips, so nothing behavioural changes — this only keeps the stored form consistent).
    engine["engine_release"] = engine_release[1:] if engine_release.startswith("v") else engine_release
    pkgs = engine.setdefault("packages", {})
    for mid, ver in target_versions.items():
        if mid in pkgs:
            pkgs[mid] = ver
    _write_json(_engine_manifest_path(), engine)
    return engine


def _resync_tool_runtime() -> bool:
    """Group-scoped `uv sync` rebuilds the tool-runtime from the overlaid lockfile BEFORE migrations run
    in it (provisioning step 3) — shelled via subprocess (the bootstrap.py pattern). It materializes the
    runtime only and never mutates a gitignored data store. Returns True on success. NEVER runs in tests /
    the demo (the injected-release path skips it) — one of the four named inductive gaps."""
    import subprocess   # local: only the real re-sync needs it
    try:
        subprocess.run(["uv", "sync"], cwd=os.path.join(validate.ROOT, ".engine"),
                       check=True, capture_output=True, timeout=300)
        return True
    except Exception:   # noqa: BLE001 — degrade: the caller surfaces a re-sync failure, never crashes
        return False


def _upgrade_pr_body(from_versions: dict, target_versions: dict, result: dict) -> str:
    """A plain-language body for the upgrade's own pull request (operator-facing). Lists the version move
    and the data/config changes that ran, so the reviewer sees what an approval lands. (The deployed PR
    template fill is a later refinement; this is a readable, structured summary.)"""
    lines = ["This pull request updates the engine to a new released version.", "",
             "What changed:"]
    for mid in sorted(target_versions):
        frm, to = from_versions.get(mid, "—"), target_versions.get(mid)
        lines.append(f"- {mid}: {frm} -> {to}")
    ran = result.get("migrations", {}).get("ran") or []
    if ran:
        lines += ["", "Data/settings updates that ran:"] + [f"- {r}" for r in ran]
    co = result.get("codeowners")
    if co == "written":
        lines += ["", "Refreshed the list of engine files that route to you for review, so this version's "
                  "new files are covered. Any review rules you added yourself are untouched."]
    elif co == "degraded":
        lines += ["", "Could not refresh the engine-file review list (no account handle on record); your "
                  "existing CODEOWNERS file was left unchanged."]
    cf = result.get("claude_floor")
    if cf == "merged":
        lines += ["", "Updated your project's working guide (the engine's marked block in CLAUDE.md) to this "
                  "version. Anything you wrote outside that block is untouched."]
    elif cf == "degraded":
        lines += ["", "Could not update your project's working guide — the engine's marked block in CLAUDE.md "
                  "looked damaged, so I left the file unchanged. Check the marker lines, then update again."]
    elif cf == "skipped-no-section":
        lines += ["", "Did not update your project's working guide — I found no engine marked block in "
                  "CLAUDE.md, so I left the file unchanged."]
    fi = (result.get("foundation_ignores") or {}).get("status")
    if fi == "written":
        lines += ["", "Updated the engine's ignore list (the marked block in .gitignore that keeps the "
                  "engine's own tool files and per-session folders out of git) to this version. Any ignore "
                  "lines you added yourself are untouched."]
    elif fi == "degraded":
        lines += ["", "Could not update the engine's ignore list — the marked block in .gitignore looked "
                  "damaged, so I left the file unchanged. Check the marker lines, then update again."]
    lines += ["", "The engine's own consistency check passed. Merging this is your review and consent; "
              "reverting this pull request undoes the update."]
    return "\n".join(lines)


def _open_upgrade_pr(branch: str, title: str, body: str, repo=None, token=None) -> dict:
    """THE GIT+PR BOUNDARY (provisioning step 6): stage the overlaid change on a new branch, commit, push,
    and open a pull request so an upgrade is reviewed + reversible like any change. NET-NEW (no
    git-automation helper existed) — branch/commit/push via subprocess (the bootstrap.py pattern), the PR
    via POST /repos/{slug}/pulls (the telemetry.open_issue pattern). INJECTED for tests + the demo
    (upgrade(opener=...)), so this real path NEVER runs in the construction repo — one of the four named
    inductive gaps (no release to upgrade to, no PR to open)."""
    import subprocess, urllib.request, json as _json, boot   # local: only the real open needs these
    slug = repo or boot.repo_slug()
    tok = token if token is not None else boot.gh_token()
    if not slug or not tok:
        raise RuntimeError("could not determine the engine repository / credentials to open the update "
                           "pull request.")
    base = getattr(boot, "PROTECTED_BRANCH", "main")
    for args in (["git", "checkout", "-b", branch], ["git", "add", "-A"],
                 ["git", "commit", "-m", title], ["git", "push", "-u", "origin", branch]):
        subprocess.run(args, cwd=validate.ROOT, check=True, capture_output=True)
    url = f"https://api.github.com/repos/{slug}/pulls"
    payload = _json.dumps({"title": title, "head": branch, "base": base, "body": body}).encode("utf-8")
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "engine-module-manager", "Authorization": f"Bearer {tok}",
               "Content-Type": "application/json"}
    with urllib.request.urlopen(urllib.request.Request(url, data=payload, headers=headers),
                                timeout=60) as resp:
        return _json.loads(resp.read())


def _refresh_codeowners(handle) -> str:
    """Re-render the CODEOWNERS ownership wall for the POST-overlay engine path set, so a release that
    adds/removes engine files keeps the wall complete and every engine file still routes to the operator
    for review — the design's upgrade re-render (provisioning §Identity and tokens; the engine.json
    `handle` field). Operator-added rules are preserved (fence-scoped). Single-sources the path set +
    render with first-run via module_coherence.codeowners_path_set + wiring.apply_codeowners, so the two
    render sites can't drift. Returns 'written' | 'already' | 'degraded'. DEGRADES (no change) when no
    operator handle is on record (the construction repo / a pre-handle manifest) or the render refuses —
    never crashes (Q7)."""
    if not handle:
        return "degraded"
    co_path = os.path.join(validate.ROOT, ".github", "CODEOWNERS")
    try:
        return wiring.apply_codeowners(co_path, module_coherence.codeowners_path_set(), handle)["status"]
    except wiring.WiringError:
        return "degraded"


def _merge_claude_floor(release_tree: str) -> str:
    """Keyed-merge the engine's root-CLAUDE.md floor from the release's CLAUDE.deployed.md into the local
    CLAUDE.md, replacing ONLY the engine `floor` fence and preserving any operator content outside it
    (repository-topology law 1 — keyed, reversible entries; the #234/#272 coexistence obligation). The
    floor is sourced from the release's CLAUDE.deployed.md, NEVER its CLAUDE.md (the maintainer
    construction-governance file) — which also closes the latent bug where CLAUDE.md ∈ FOUNDATION_CODE
    would copy the construction file over an adopter's floor on every upgrade.

    Returns: 'merged' (the engine block was replaced); 'skipped' (the release ships no floor source);
    'skipped-no-section' (the local CLAUDE.md carries no engine `floor` fence — leave it untouched, NEVER
    append a duplicate floor: the pre-keyed-merge raw-floor case); 'degraded' (a malformed local fence —
    leave it untouched, never a mid-upgrade crash). Structural sibling of `_refresh_codeowners`, but with
    no handle dependency."""
    src = os.path.join(release_tree, _DEPLOYED_FLOOR_REL)
    if not os.path.isfile(src):
        return "skipped"
    floor_lines = validate.read(src).split("\n")
    if floor_lines and floor_lines[-1] == "":
        floor_lines = floor_lines[:-1]   # drop the trailing-newline empty element; fence_apply re-terminates
    local_path = os.path.join(validate.ROOT, _ROOT_CLAUDE_REL)
    local = validate.read(local_path) if os.path.isfile(local_path) else ""
    try:
        if not wiring.fence_present(local, _FLOOR_FENCE, style=wiring.MD_FENCE):
            return "skipped-no-section"
        merged = wiring.fence_apply(local, _FLOOR_FENCE, floor_lines, style=wiring.MD_FENCE)
    except wiring.WiringError:
        return "degraded"
    if merged != local:
        with open(local_path, "w", encoding="utf-8") as fh:
            fh.write(merged)
    return "merged"


def upgrade(ref: str | None = None, release_tree: str | None = None, opener=None, backup=None) -> dict:
    """Upgrade the whole engine vX -> vY (provisioning §"Upgrading the engine"). Steps: fetch the tagged
    release, overlay engine code and re-render the CODEOWNERS ownership wall for the new release's engine
    files (operator config + gitignored data preserved), re-sync the tool-runtime, run migrations in
    dependency order, run coherence, and land the change as a reviewed pull request.

    Injectable boundaries (so tests + the demo run the REAL overlay/runner/coherence and never touch the
    network or open a real PR): `release_tree` injects a local extracted release AND marks a practice run
    (the real `uv sync` is skipped); `opener` injects the git+PR boundary; `backup` injects the migration
    backup seam (None = memory if installed, else none -> data migrations refuse). Returns a structured
    result the CLI renders in plain language. Refuses cleanly (nothing applied) on an unreachable release,
    a containment escape, or a data migration with no backup seam (the pre-flight). Degrades to the
    current version on an unreachable release (§5 / R7). The change lands ONLY as a reviewed pull request,
    so an abort at any step leaves it UN-MERGED — no half-state is ever the operating baseline. A mid-step
    abort (a paused coherence finding, a failed re-sync) can leave the working copy changed-but-unmerged,
    which the operator discards or fixes; the engine does not attempt in-place rollback."""
    injected = release_tree is not None
    result = {"refused": False, "applied": False, "reason": None, "from": None, "to": None,
              "copied": [], "wiring": [], "synced": None, "migrations": {"ran": [], "refused": []},
              "findings": [], "pr": None, "notes": [], "codeowners": None, "claude_floor": None}
    tmp = None
    try:
        engine = module_coherence.load_engine_manifest() or {"packages": {}}
        from_versions = dict(engine.get("packages") or {})
        present_ids = sorted(from_versions)
        result["from"] = from_versions
        target_ref = ref or "latest"
        # (1) FETCH the tagged release (reuse the PR-1 boundary + its plain-failure handler; degrade §5).
        # On the real path, resolve None/"latest" to a CONCRETE tag FIRST, so the engine fetches, runs, and
        # records a pinned ref — never a moving one (R7). The injected path passes a concrete ref already.
        if release_tree is None:
            if not present_ids:
                return {**result, "refused": True, "reason": "There are no installed modules to update."}
            # Resolve the engine's HOME from the manifest and fetch the release FROM THE HOME, never from
            # this repo's own origin (D-281/D-282; #367). Absent home -> refuse with a remedy (three-state).
            home = _home_repository()
            if not home:
                return {**result, "refused": True,
                        "reason": "This engine has no update home recorded, so it can't check for updates. "
                                  "Tell me the repository your engine updates from (for example your-org/your-engine) and I'll "
                                  "record it, then you can update again. The engine is unchanged."}
            tmp = tempfile.mkdtemp(prefix="engine-upgrade-")
            try:
                target_ref = _resolve_release_ref(ref, repo=home)   # None/"latest" -> concrete latest tag
                release_tree = _fetch_release_tree(target_ref, tmp, repo=home)
            except Exception as exc:
                if _release_is_missing(exc):   # recorded home, but no such release/repo -> refuse, NAME it
                    return {**result, "refused": True,
                            "reason": f"Couldn't find a release to update to at your engine's update home, "
                                      f"{home} (looked for '{ref or 'latest'}'). That home may have no "
                                      f"published releases yet, or it may have been renamed or removed. The "
                                      f"engine is unchanged. If the home is wrong, update the recorded home "
                                      f"and try again."}
                return {**result, "refused": True,   # transport/offline -> DEGRADE to the current version
                        "reason": f"Couldn't reach your engine's update home, {home}, to check for updates — "
                                  f"the network may be down, or the home may not be reachable right now. The "
                                  f"engine is unchanged and still working. ({exc})"}
        # read target versions + capture the CURRENTLY-installed manifests (for wiring deltas) BEFORE the
        # overlay overwrites them
        target_versions, old_by_id = {}, {}
        for mid in present_ids:
            man_src = os.path.join(release_tree, ".engine", "modules", mid, "manifest.json")
            if not os.path.isfile(man_src):
                return {**result, "refused": True,
                        "reason": f"The engine release does not contain the installed module '{mid}', so "
                                  f"the update was stopped and nothing was changed."}
            target_versions[mid] = validate.load_json(man_src).get("version")
            cur = os.path.join(_modules_dir(mid), "manifest.json")
            old_by_id[mid] = validate.load_json(cur) if os.path.isfile(cur) else {}
        result["to"] = target_versions
        # PRE-FLIGHT the data-migration backup guard BEFORE any overlay (the half-state law): refuse the
        # WHOLE upgrade if a data migration in range has no backup seam — nothing is applied.
        selected = select_migrations(
            from_versions, target_versions,
            [validate.load_json(os.path.join(release_tree, ".engine", "modules", mid, "manifest.json"))
             for mid in present_ids])
        seam = _resolve_backup_seam(backup)
        data_no_seam = sorted({s["module_id"] for s in selected
                               if s.get("kind") == "data" and seam is None})
        if data_no_seam:
            return {**result, "refused": True,
                    "reason": f"This update needs to change stored data for {', '.join(data_no_seam)}, but "
                              f"no data backup is set up yet — and the engine never changes stored data it "
                              f"can't first back up. The engine is unchanged. Ask me to set up a backup, then "
                              f"update again."}
        # (2) OVERLAY engine code (driven off the present set; containment fail-closed)
        try:
            result["copied"], candidates = _overlay_engine_code(release_tree, present_ids)
        except _UpgradeRefused as ur:
            return {**result, "refused": True, "reason": ur.reason}
        # (2b) wiring deltas, (2c) bump the engine manifest (preserve identity)
        result["wiring"] = _apply_wiring_deltas(old_by_id, candidates)
        _bump_engine_manifest(target_versions, target_ref)
        # (2d) RE-RENDER the CODEOWNERS ownership wall against the new release's engine path set (the
        # design's upgrade re-render). Runs AFTER the overlay (so the path set sees the release's files)
        # and the manifest bump, BEFORE coherence/PR (so the landed diff carries a complete wall). The
        # handle is the preserved-identity owner from the engine manifest; absent -> degrade (no change).
        result["codeowners"] = _refresh_codeowners(engine.get("handle"))
        # (2e) KEYED-MERGE the root CLAUDE.md floor from the release's CLAUDE.deployed.md (replace only the
        # engine `floor` fence; preserve operator content; never overlay the construction CLAUDE.md). Same
        # placement rationale as the CODEOWNERS re-render: after the overlay, before coherence/PR.
        result["claude_floor"] = _merge_claude_floor(release_tree)
        # (2f) RE-ASSERT the foundation `.gitignore` fence — release-evolvable, like the CODEOWNERS/CLAUDE.md
        # re-renders above: a new release's FOUNDATION_IGNORE_LINES reach a provisioned repo here (the block
        # is excluded from the overlay-replace set, so this is the ONLY path that evolves it). Idempotent and
        # block-scoped — the operator's own ignore lines + any module `gitignore` fences are untouched.
        result["foundation_ignores"] = wiring.apply_foundation_ignores(wiring.GITIGNORE_PATH)
        # (3) RE-SYNC the tool-runtime (real path only; the injected/practice run skips it — no real venv).
        # Migrations are Python that runs IN the runtime, so a FAILED re-sync ABORTS before step 4 rather
        # than run migrations against a stale runtime — staged but not opened, no saved data touched.
        if injected:
            result["synced"] = None
            result["notes"].append("(skipped re-building the tool-runtime — this is a practice run)")
        else:
            result["synced"] = _resync_tool_runtime()
            if not result["synced"]:
                result["applied"] = True
                result["reason"] = ("The update was applied to the working copy but the engine's tools "
                                    "could not be rebuilt from the new version, so it was NOT opened for "
                                    "review and no saved data was changed. Fix the problem and update "
                                    "again, or discard the change.")
                return result
        # (4) RUN migrations (selected, dependency-ordered; the no-backup guard already pre-flighted)
        result["migrations"] = run_migrations(selected, from_versions, target_ref, backup=seam)
        # A data migration whose backup FAILED at run time (vault reachable at pre-flight, gone at snapshot)
        # comes back as a refusal, not a crash — decline to open the change for review (nothing is merged),
        # the same degrade-loud pattern as a failed re-sync / a coherence break below.
        if result["migrations"].get("refused"):
            result["applied"] = True
            result["reason"] = ("The update was applied to the working copy but a stored-data update could "
                                "not be completed (its backup did not succeed), so it was NOT opened for "
                                "review and nothing was merged. Ask me to set up or check your backup, then "
                                "update again.")
            return result
        # Floor (c) (D-264): when a data migration ran, a copy of the saved memory was taken before it changed —
        # disclose it ONCE per upgrade (structured kind check, not a per-step or string-match), plainly, so the later
        # restore offer is reassurance rather than a mystery.
        if any(item.get("kind") == "data" for item in selected):
            result["notes"].append(
                "Before changing your saved memory, I automatically saved a copy of it from right before this "
                "update — there's nothing for you to do now. If this update is ever undone, I can bring that copy back.")
        # (5) COHERENCE — a hard finding pauses (the change is staged in the working copy, not landed)
        result["applied"] = True
        result["findings"] = module_coherence.check_coherence()
        if any(f.get("severity") == "hard" for f in result["findings"]):
            result["reason"] = ("The update was applied to the working copy but a consistency problem "
                                "remains, so it was NOT opened for review. Fix the problem and update "
                                "again, or discard the change.")
            return result
        # (6) LAND as a reviewed pull request (injected opener in tests/demo; real opener otherwise). An
        # injected practice run with no opener never reaches the real git/PR boundary (the footgun guard).
        title = f"Update the engine to {target_ref}"
        body = _upgrade_pr_body(from_versions, target_versions, result)
        branch = "engine-update-" + re.sub(r"[^a-zA-Z0-9._-]+", "-", target_ref)
        open_fn = opener or (None if injected else _open_upgrade_pr)
        if open_fn is None:
            result["notes"].append("(practice run — the pull request was not opened)")
            return result
        try:
            result["pr"] = open_fn(branch=branch, title=title, body=body)
        except Exception as exc:   # noqa: BLE001 — staged but not opened; surfaced, never a traceback
            result["notes"].append(f"(the update is staged but the pull request could not be opened: {exc})")
        return result
    finally:
        if tmp and os.path.isdir(tmp):
            shutil.rmtree(tmp, ignore_errors=True)


# ---- clean whole-engine removal (provisioning §clean removal / Design commitments L446-449) ----

def _remove_engine_pr_body(result: dict) -> str:
    """A plain-language body for the whole-engine removal pull request (operator-facing). States what the
    removal does, the safety-rule outcome, and that it is reviewed + reversible."""
    lines = ["This pull request removes the engine from this repository, leaving an operable, "
             "engine-free product.", "", "What this does:",
             "- Deletes the engine's own files (its tools, checks, schemas, and configuration).",
             "- Removes the engine's entries from shared setup files. Anything there that might also be "
             "yours is left in place for you to review and remove if you don't need it."]
    db = result.get("de_bootstrap") or {}
    if db.get("status") == "kept":
        lines.append("- Keeps the safety rule on your main branch, with the engine's own checks removed "
                     "from it.")
    elif db.get("status") == "dropped":
        lines.append("- Removes the safety rule on your main branch entirely (you chose to remove it).")
    elif db.get("status") == "unaugmented":
        lines.append("- Takes the engine's checks — and any force-push/deletion/pull-request protection the "
                     "engine had added — back out of your own branch-protection rule, leaving the rest of "
                     "that rule exactly as it was. (The rule is yours, so it is not removed.)")
    lines += ["", "Reviewed and reversible: reverting this pull request restores the engine's files. The "
              "main-branch safety rule is turned back on by running the engine setup again.", "",
              "Merging this is your review and consent."]
    return "\n".join(lines)


def remove_engine(opener=None, transport=None, choice: str | None = None, announce=None,
                  repo=None, token=None) -> dict:
    """Remove the WHOLE engine cleanly (provisioning §clean removal / Design commitments L446-449) — the
    'separate step' that per-module remove() points a required module toward, leaving an operable,
    engine-free product. The order is what safety demands:
      (1) DE-BOOTSTRAP FIRST (operator-privileged): remove the engine's required checks from its own
          safety rule (keep the floor remainder, or drop the rule per the operator's `choice`). This runs
          BEFORE the deletion pull request, because that PR deletes the engine workflows and a required
          check whose workflow is gone would 'wait forever' and deadlock the PR (provisioning L332-335).
      (2) REVERSE ALL WIRES across every installed module (the engine's entries in shared files), leaving
          honest residue for anything it can't key to the engine alone (a permission the operator may
          also hold — the reversal firewall).
      (3) DELETE every engine file — UNLIKE per-module remove() (which deletes only under .engine/), the
          whole-engine removal also deletes the engine-owned files OUTSIDE .engine/: the foundation
          infrastructure artifacts (the .github/ control-plane files) and the root CLAUDE.md. CODEOWNERS
          loses only the engine block (the operator's own rules are kept; the file is removed iff nothing
          else remains). The .engine/ tree goes wholesale.
      (4) LAND the deletions as a reviewed pull request via the injectable opener (reuse _open_upgrade_pr).

    Reviewed + reversible: reverting the pull request restores the files; the safety rule is re-created by
    re-running the engine setup (de_bootstrap and bootstrap.apply are the reversal pair, both idempotent).
    FIXTURE-DEMOED — never run on the construction repo (it would delete the engine being built). The four
    boundaries (the de-bootstrap GitHub API, the git/PR open, the real working tree, the operator's real
    keep/drop choice) are injected/faked so tests + the demo run the REAL reversal / delete-set / de-
    bootstrap-decision logic; 'works on the fixture ⇒ works for a real adopter' is the inductive gap."""
    injected = opener is not None or transport is not None
    say = announce if announce is not None else (lambda text: print(text))
    result = {"de_bootstrap": None, "reversed": [], "left_in_place": [], "deleted": [],
              "pr": None, "reversal_note": None, "refused": False, "reason": None, "notes": []}
    manifests = module_coherence.discover_manifests()

    # (1) DE-BOOTSTRAP FIRST — drop the engine required checks so the deletion PR can't deadlock.
    import boot  # lazy: the shared GitHub-context helpers (matches _fetch_release_tree / _open_upgrade_pr)
    slug = repo or boot.repo_slug()
    tok = token if token is not None else boot.gh_token()
    cp = bootstrap.ControlPlane(slug or "", tok or "", transport=transport)
    # The control-plane marker the arrival recorded — whether the engine created its OWN ruleset or AUGMENTED
    # a pre-existing PRODUCT one, and the exact pieces it added — so de-bootstrap reverses precisely that and
    # nothing of the operator's. Absent on an older install or when none was recorded; de_bootstrap then falls
    # back to a bounded, name-only strip that still never deletes a product rule.
    marker = (module_coherence.load_engine_manifest() or {}).get("control_plane")
    try:
        result["de_bootstrap"] = cp.de_bootstrap(choice=choice, marker=marker, announce=say)
    except bootstrap.BootstrapError as exc:
        return {**result, "refused": True,
                "reason": f"Couldn't reach GitHub to remove the engine's branch protection ({exc}); "
                          f"nothing was changed. Try again when you're back online."}

    # (2) REVERSE ALL WIRES across every module + disclose honest permission residue.
    for _path, m in manifests:
        for f in wiring.reverse_all(m.get("wires") or []):
            result["reversed"].append(validate.fmt(f))
        result["left_in_place"].extend(_permission_residue(m))

    # (3) DELETE the engine file set. Compute it BEFORE any deletion (the live globs need the files).
    co_rel = ".github/CODEOWNERS"
    foundation = module_coherence.foundation_infra_paths()
    provides = set(module_coherence.provides_claims(manifests).keys())
    # engine-owned files OUTSIDE .engine/: provides-claimed (e.g. .claude/*/.gitkeep) + the non-.engine
    # foundation members (the .github/ artifacts), minus the three SHARED files handled specially below —
    # CODEOWNERS, root CLAUDE.md, and root .gitignore — which carry the engine as a keyed fenced block, so
    # they are block-reversed (operator content kept) rather than deleted wholesale.
    outside = sorted({r for r in (provides | set(foundation))
                      if not r.startswith(".engine/") and r not in (co_rel, _ROOT_CLAUDE_REL, _GITIGNORE_REL)})
    deleted = []
    for rel in outside:
        p = os.path.join(validate.ROOT, rel)
        if os.path.isfile(p):
            try:
                os.remove(p)
                deleted.append(rel)
            except OSError as exc:
                result["left_in_place"].append(f"Could not delete {rel} ({exc}); remove it by hand.")
    # CODEOWNERS: remove ONLY the engine block; delete the file iff nothing but whitespace remains, else
    # keep the operator's own rules (the engine never clobbers operator content in a shared file).
    co_path = os.path.join(validate.ROOT, co_rel)
    if os.path.isfile(co_path):
        text = validate.read(co_path)
        remainder = wiring.fence_reverse(text, wiring.CODEOWNERS_FENCE)
        if remainder.strip() == "":
            os.remove(co_path)
            deleted.append(co_rel)
        elif remainder != text:
            with open(co_path, "w", encoding="utf-8") as fh:
                fh.write(remainder)
            deleted.append(f"{co_rel} (engine block removed; your own rules kept)")
    # Root CLAUDE.md: the SAME block-reversal as CODEOWNERS — remove only the engine `floor` fence and
    # delete the file iff nothing but whitespace remains (an all-engine greenfield CLAUDE.md), else keep the
    # operator's own content (a brownfield CLAUDE.md). Wrapped so a malformed local fence degrades (leaves
    # the file untouched) rather than crashing the uninstall in front of a non-engineer.
    claude_path = os.path.join(validate.ROOT, _ROOT_CLAUDE_REL)
    if os.path.isfile(claude_path):
        text = validate.read(claude_path)
        try:
            remainder = wiring.fence_reverse(text, _FLOOR_FENCE, style=wiring.MD_FENCE)
        except wiring.WiringError as exc:
            remainder = text
            result["left_in_place"].append(
                f"Left {_ROOT_CLAUDE_REL} as it is — its engine section looked damaged ({exc}).")
        if remainder.strip() == "":
            os.remove(claude_path)
            deleted.append(_ROOT_CLAUDE_REL)
        elif remainder != text:
            with open(claude_path, "w", encoding="utf-8") as fh:
                fh.write(remainder)
            deleted.append(f"{_ROOT_CLAUDE_REL} (engine block removed; your own content kept)")
    # Root .gitignore: the SAME block-reversal — remove only the engine `foundation-ignores` fence and keep
    # the operator's own ignore lines (delete the file only if nothing but whitespace remains, which never
    # happens in practice — the generic dev-ignores survive). The module `gitignore` fences were already
    # reversed per-module in step (1) of each removal, so this leaves an engine-free .gitignore. Wrapped so a
    # malformed hand-edited fence degrades (file untouched) rather than crashing the uninstall — .gitignore
    # is the file operators edit most, so this fail-safe matters more here than for CODEOWNERS.
    gi_path = os.path.join(validate.ROOT, _GITIGNORE_REL)
    if os.path.isfile(gi_path):
        text = validate.read(gi_path)
        try:
            remainder = wiring.fence_reverse(text, wiring.FOUNDATION_IGNORES_FENCE)
        except wiring.WiringError as exc:
            remainder = text
            result["left_in_place"].append(
                f"Left {_GITIGNORE_REL} as it is — its engine section looked damaged ({exc}).")
        if remainder.strip() == "":
            os.remove(gi_path)
            deleted.append(_GITIGNORE_REL)
        elif remainder != text:
            with open(gi_path, "w", encoding="utf-8") as fh:
                fh.write(remainder)
            deleted.append(f"{_GITIGNORE_REL} (engine block removed; your own lines kept)")
    # the whole .engine/ tree (tools, checks, schemas, manifests, generated maps — everything). The
    # running tool keeps executing from memory, so the source being gone on disk before the opener stages
    # it (git add -A) is safe; any process needing .engine again would be a fresh process.
    if os.path.isdir(validate.ENGINE_DIR):
        shutil.rmtree(validate.ENGINE_DIR)
        deleted.append(".engine/")
    result["deleted"] = sorted(deleted)

    # (4) LAND the deletions as a reviewed pull request (reuse the upgrade opener; the opener's `git add
    #     -A` stages the deletions + the wire reversals). INJECTED in tests + the demo; the real path runs
    #     only on a deployed repo, never the construction repo. The opener should run on an otherwise-clean
    #     tree so the removal PR carries only the removal.
    body = _remove_engine_pr_body(result)
    open_fn = opener or (None if injected else _open_upgrade_pr)
    if open_fn is None:
        result["notes"].append("(practice run — the removal pull request was not opened)")
    else:
        try:
            result["pr"] = open_fn(branch="engine-remove", title="Remove the engine", body=body)
        except Exception as exc:  # noqa: BLE001 — staged but not opened; surfaced, never a traceback
            result["notes"].append(f"(removal is staged but the pull request could not be opened: {exc})")

    # The sharpened reversal disclosure (names the unprotected window + the drop case explicitly).
    db = result["de_bootstrap"] or {}
    if db.get("status") == "dropped":
        protection_state = ("off — you removed the safety rule, so re-running the engine setup re-creates "
                            "it from scratch")
    elif db.get("status") == "kept":
        protection_state = ("still in place but without the engine's checks; re-running the engine setup "
                            "restores them")
    else:
        protection_state = "unchanged"
    result["reversal_note"] = (
        "To undo this removal: revert the pull request to bring the engine's files back. Until you then "
        f"run the engine setup again, your main branch's safety rule is {protection_state}.")
    return result


# ---- CLI rendering ----------------------------------------------------------------------------

def _render_remove(result: dict) -> None:
    mid = result.get("module_id")
    if result.get("refused"):
        print(f"Did not remove '{mid}': {result['reason']}")
        return
    print(f"Removed the module '{mid}'.")
    for line in result.get("reversed", []):
        print("  - " + line)
    for rel in result.get("deleted", []):
        print(f"  - deleted {rel}")
    if result.get("groups_after") is not None:
        print(f"  - tool-runtime dependency groups are now: {result['groups_after'] or '(none)'}")
    if result.get("left_in_place"):
        print("\nLeft in place (on purpose):")
        for line in result["left_in_place"]:
            print("  - " + line)
    hard = [f for f in result.get("findings", []) if f.get("severity") == "hard"]
    if hard:
        print(f"\nAfter removing '{mid}', a problem remains:")
        for f in hard:
            print("  - " + validate.fmt(f))
    else:
        print("\nThe remaining modules are consistent.")


def _render_add(result: dict) -> None:
    mid = result.get("module_id")
    if result.get("refused"):
        print(f"Did not add '{mid}': {result['reason']}")
        return
    print(f"Added the module '{mid}' (version {result.get('version')}).")
    for rel in result.get("copied", []):
        print(f"  - added {rel}")
    for line in result.get("applied_wires", []):
        print("  - " + line)
    if result.get("groups_after") is not None:
        print(f"  - tool-runtime dependency groups are now: {result['groups_after'] or '(none)'}")
    for line in result.get("notes", []):
        print("  - " + line)
    hard = [f for f in result.get("findings", []) if f.get("severity") == "hard"]
    if hard:
        print(f"\nAfter adding '{mid}', a problem remains:")
        for f in hard:
            print("  - " + validate.fmt(f))
    else:
        print("\nThe installed modules are consistent.")


def _render_upgrade(result: dict) -> None:
    if result.get("refused"):
        print(f"Did not update the engine: {result['reason']}")
        return
    frm, to = result.get("from") or {}, result.get("to") or {}
    moved = [f"{mid} {frm.get(mid, '—')} -> {to.get(mid)}" for mid in sorted(to)]
    print("Updated the engine" + (f": {'; '.join(moved)}." if moved else "."))
    copied = result.get("copied", [])
    for rel in copied[:8]:
        print(f"  - replaced {rel}")
    if len(copied) > 8:
        print(f"  - … and {len(copied) - 8} more engine file(s)")
    co = result.get("codeowners")
    if co == "written":
        print("  - refreshed the list of engine files that route to you for review "
              "(this version's new files are covered; your own rules untouched)")
    elif co == "degraded":
        print("  - could not refresh the engine-file review list (no account handle on record); "
              "left it unchanged")
    cf = result.get("claude_floor")
    if cf == "merged":
        print("  - updated your project's working guide (the engine's marked block in CLAUDE.md; "
              "your own content kept)")
    elif cf == "degraded":
        print("  - could not update your project's working guide — the engine's marked block in CLAUDE.md "
              "looked damaged; left the file unchanged (check the marker lines and update again)")
    elif cf == "skipped-no-section":
        print("  - did not update your project's working guide — no engine marked block found in CLAUDE.md; "
              "left the file unchanged")
    for r in result.get("migrations", {}).get("ran", []):
        print(f"  - ran update: {r}")
    for r in result.get("migrations", {}).get("refused", []):
        print(f"  - {r}")
    for line in result.get("notes", []):
        print("  - " + line)
    pr = result.get("pr")
    if pr:
        num = pr.get("number") if isinstance(pr, dict) else None
        print(f"\nOpened a pull request{f' #{num}' if num else ''} for review — merging it is your consent; "
              f"reverting it undoes the update.")
    hard = [f for f in result.get("findings", []) if f.get("severity") == "hard"]
    if hard:
        print(f"\n{result.get('reason') or 'A problem remains:'}")
        for f in hard:
            print("  - " + validate.fmt(f))
    elif not pr:
        print("\nThe update is staged and consistent.")


def _render_remove_engine(result: dict) -> None:
    if result.get("refused"):
        print(f"Did not remove the engine: {result['reason']}")
        return
    db = result.get("de_bootstrap") or {}
    state = {"kept": "kept your main-branch safety rule (the engine's checks removed from it)",
             "dropped": "removed your main-branch safety rule entirely",
             "no-rule": "found no engine safety rule to remove"}.get(db.get("status"), "")
    print("Removed the engine." + (f" Safety rule: {state}." if state else ""))
    for rel in result.get("deleted", []):
        print(f"  - deleted {rel}")
    for line in result.get("reversed", []):
        print("  - " + line)
    for line in result.get("left_in_place", []):
        print("  - left in place: " + line)
    for line in result.get("notes", []):
        print("  - " + line)
    pr = result.get("pr")
    if pr:
        num = pr.get("number") if isinstance(pr, dict) else None
        print(f"\nOpened a pull request{f' #{num}' if num else ''} with the deletions — merging it is your "
              f"consent; reverting it brings the engine's files back.")
    if result.get("reversal_note"):
        print(f"\n{result['reversal_note']}")


def _status() -> int:
    manifests = module_coherence.discover_manifests()
    print(f"Installed modules ({len(manifests)}):")
    for _p, m in manifests:
        mid = m.get("id")
        deps = sorted((m.get("depends") or {}).keys())
        dependents = sorted(o.get("id") for _q, o in manifests
                            if o.get("id") != mid and mid in (o.get("depends") or {}))
        line = f"  - {mid} ({m.get('status')})"
        if deps:
            line += f"; needs: {', '.join(deps)}"
        if dependents:
            line += f"; needed by: {', '.join(dependents)}"
        print(line)
    try:
        derived = derive_uv_groups(manifests=manifests)
        committed = committed_default_groups()
        synced = derived == committed
        print(f"\nTool-runtime dependency groups: {derived or '(none)'} "
              f"({'in sync' if synced else f'OUT OF SYNC — committed: {committed}'}).")
    except Exception as exc:
        print(f"\nTool-runtime dependency groups: could not read the tool-runtime configuration ({exc}).")
    return 0


# ---- demo (mutation-free, real logic, fixture boundary) ---------------------------------------

@contextlib.contextmanager
def _redirect_root(root: str):
    """Point every ROOT-derived path at a throwaway fixture tree, restore on exit. The wiring-library
    path constants are bound at import, so they are redirected explicitly (the same discipline the
    coherence tests use)."""
    saved = (validate.ROOT, validate.ENGINE_DIR, wiring.SETTINGS_PATH, wiring.MCP_PATH,
             wiring.GITIGNORE_PATH, wiring.CATALOG_PATH)
    validate.ROOT = root
    validate.ENGINE_DIR = os.path.join(root, ".engine")
    wiring.SETTINGS_PATH = os.path.join(root, ".claude", "settings.json")
    wiring.MCP_PATH = os.path.join(root, ".mcp.json")
    wiring.GITIGNORE_PATH = os.path.join(root, ".gitignore")
    wiring.CATALOG_PATH = os.path.join(root, ".engine", "schemas", "surface-catalog.json")
    try:
        yield
    finally:
        (validate.ROOT, validate.ENGINE_DIR, wiring.SETTINGS_PATH, wiring.MCP_PATH,
         wiring.GITIGNORE_PATH, wiring.CATALOG_PATH) = saved


def _build_fixture(root: str) -> None:
    """A minimal COHERENT fixture engine: a required `base` module + an optional `optx` module
    (one provided file, one gitignore wire, one declared dependency group). Every .engine/ file is
    claimed or named-infra, so coherence is clean before remove."""
    eng = os.path.join(root, ".engine")
    os.makedirs(os.path.join(eng, "modules", "base"))
    os.makedirs(os.path.join(eng, "modules", "optx"))
    os.makedirs(os.path.join(eng, "tools"))
    os.makedirs(os.path.join(root, ".claude"))
    _write_json(os.path.join(eng, "modules", "base", "manifest.json"),
                {"id": "base", "version": "0.0.0", "status": "required",
                 "provides": {"tool": [".engine/tools/base_tool.py"]}, "depends": {}})
    _write_json(os.path.join(eng, "modules", "optx", "manifest.json"),
                {"id": "optx", "version": "0.0.0", "status": "optional",
                 "provides": {"tool": [".engine/tools/optx_tool.py"]},
                 "wires": [{"type": "gitignore", "key": "optx-cache",
                            "lines": [".engine/optx/.cache/"]},
                           {"type": "permission", "value": "Bash(optx-tool:*)"}],
                 "depends": {}})
    _write_json(os.path.join(eng, "engine.json"),
                {"engine_release": "0.0.0", "packages": {"base": "0.0.0", "optx": "0.0.0"},
                 "identity": "solo"})
    with open(os.path.join(eng, "tools", "base_tool.py"), "w") as fh:
        fh.write("# base\n")
    with open(os.path.join(eng, "tools", "optx_tool.py"), "w") as fh:
        fh.write("# optx\n")
    with open(os.path.join(eng, "uv.lock"), "w") as fh:
        fh.write("")
    with open(os.path.join(eng, "pyproject.toml"), "w") as fh:
        fh.write('[project]\nname = "x"\nversion = "0"\n\n[dependency-groups]\n'
                 'base = ["pkg-a"]\noptx = ["pkg-b"]\n\n[tool.uv]\ndefault-groups = ["base", "optx"]\n')
    for name in (".mcp.json", os.path.join(".claude", "settings.json")):
        with open(os.path.join(root, name), "w") as fh:
            fh.write("{}\n")
    with open(os.path.join(root, ".gitignore"), "w") as fh:
        fh.write("# a foundation plain line\n.engine/.venv/\n")
    # apply optx's declared wires so the forward leg sees them applied (the real appliers)
    wiring.apply_all([{"type": "gitignore", "key": "optx-cache", "lines": [".engine/optx/.cache/"]},
                      {"type": "permission", "value": "Bash(optx-tool:*)"}])


def run_demo() -> bool:
    """The fail-then-pass behavioral demonstration, returning True iff every step behaved. Real
    plan_remove / remove / derive logic runs; only the tree it touches is a throwaway. Part A shows the
    two refusals on the REAL repo (read-only); Part B removes an optional module end-to-end on a
    fixture; Part C shows the idempotent re-run."""
    ok = True
    print("Part A — refusals on your real repository (nothing is changed):")
    core = plan_remove("core")
    print("  remove core            -> " + ("REFUSED: " + core["reason"] if core["refused"] else "NOT refused?!"))
    ok = ok and core["refused"] and "validators-core" in core["reason"]   # reverse-dependency refusal
    vc = plan_remove("validators-core")
    print("  remove validators-core -> " + ("REFUSED: " + vc["reason"] if vc["refused"] else "NOT refused?!"))
    ok = ok and vc["refused"] and "audit-library" in vc["reason"]        # reverse-dependency refusal (audit-library needs it)
    leaf = plan_remove("routine-mode")
    print("  remove routine-mode    -> " + ("REFUSED: " + leaf["reason"] if leaf["refused"] else "NOT refused?!"))
    ok = ok and leaf["refused"] and "required" in leaf["reason"]          # required-foundation refusal (a required leaf)

    print("\nPart B — removing an optional module end-to-end on a throwaway fixture:")
    with tempfile.TemporaryDirectory() as d:
        with _redirect_root(d):
            _build_fixture(d)
            before = [f for f in module_coherence.check_coherence() if f["severity"] == "hard"]
            print("  fixture coherent before removal: " + ("yes" if not before else f"NO: {before}"))
            ok = ok and not before
            res = remove("optx")
            for line in [f"removed '{res['module_id']}'"] + res["reversed"] + \
                    [f"deleted {x}" for x in res["deleted"]] + [f"groups now {res['groups_after']}"]:
                print("    - " + line)
            engine = module_coherence.load_engine_manifest()
            checks = {
                "optx file deleted": not os.path.exists(os.path.join(d, ".engine/tools/optx_tool.py")),
                "optx module folder gone": not os.path.isdir(os.path.join(d, ".engine/modules/optx")),
                "engine.json drops optx": "optx" not in (engine or {}).get("packages", {}),
                "base survives": "base" in (engine or {}).get("packages", {}),
                "groups re-derived to [base]": res["groups_after"] == ["base"],
                "default-groups rewritten": committed_default_groups() == ["base"],
                "coherent after removal": not [f for f in res["findings"] if f["severity"] == "hard"],
            }
            for label, good in checks.items():
                print(f"    [{'ok' if good else 'FAIL'}] {label}")
                ok = ok and good

            print("\nPart C — removing it again is a clean refusal (safe to re-run):")
            again = remove("optx")
            print("    -> " + (again["reason"] if again.get("refused") else "NOT refused?!"))
            ok = ok and again.get("refused")
    print("\n" + ("DEMO PASSED: refusals hold, a real removal reversed cleanly, and a re-run is safe."
                  if ok else "DEMO DID NOT BEHAVE AS EXPECTED — see above."))
    return ok


# ---- add demo (mutation-free, real logic, faked fetch boundary) -------------------------------

def _build_add_fixture(root: str) -> None:
    """A minimal COHERENT live fixture engine for the add demo: just a required `base` module present, the
    tool-runtime pyproject declaring BOTH base's and feat's dependency-groups (so feat's group becomes
    selectable the moment feat is added — the shipped engine declares every module's group, deselected or
    not), default-groups selecting only base."""
    eng = os.path.join(root, ".engine")
    os.makedirs(os.path.join(eng, "modules", "base"))
    os.makedirs(os.path.join(eng, "tools"))
    os.makedirs(os.path.join(root, ".claude"))
    _write_json(os.path.join(eng, "modules", "base", "manifest.json"),
                {"id": "base", "version": "0.0.0", "status": "required",
                 "provides": {"tool": [".engine/tools/base_tool.py"]}, "depends": {}})
    _write_json(os.path.join(eng, "engine.json"),
                {"engine_release": "0.0.0", "packages": {"base": "0.0.0"}, "identity": "solo",
                 "home_repository": "acme/engine-home"})   # the update home the fetch resolves + upgrade preserves
    with open(os.path.join(eng, "tools", "base_tool.py"), "w") as fh:
        fh.write("# base\n")
    with open(os.path.join(eng, "uv.lock"), "w") as fh:
        fh.write("")
    with open(os.path.join(eng, "pyproject.toml"), "w") as fh:
        fh.write('[project]\nname = "x"\nversion = "0"\n\n[dependency-groups]\n'
                 'base = ["pkg-a"]\nfeat = ["pkg-c"]\n\n[tool.uv]\ndefault-groups = ["base"]\n')
    for name in (".mcp.json", os.path.join(".claude", "settings.json")):
        with open(os.path.join(root, name), "w") as fh:
            fh.write("{}\n")
    with open(os.path.join(root, ".gitignore"), "w") as fh:
        fh.write("# a foundation plain line\n.engine/.venv/\n")


def _build_release_tree(root: str) -> str:
    """A throwaway extracted release tree (what _fetch_release_tree would return) holding two addable
    modules: `feat` (optional, depends the present `base`, brings one tool + a gitignore wire) and `needy`
    (optional, depends an ABSENT `ghost`). Returns the tree root (the directory that contains `.engine/`)."""
    eng = os.path.join(root, ".engine")
    os.makedirs(os.path.join(eng, "modules", "feat"))
    os.makedirs(os.path.join(eng, "modules", "needy"))
    os.makedirs(os.path.join(eng, "tools"))
    _write_json(os.path.join(eng, "modules", "feat", "manifest.json"),
                {"id": "feat", "version": "0.1.0", "status": "optional",
                 "provides": {"tool": [".engine/tools/feat_tool.py"]},
                 "wires": [{"type": "gitignore", "key": "feat-cache",
                            "lines": [".engine/feat/.cache/"]}],
                 "depends": {"base": ""}})
    _write_json(os.path.join(eng, "modules", "needy", "manifest.json"),
                {"id": "needy", "version": "0.1.0", "status": "optional",
                 "provides": {"tool": [".engine/tools/needy_tool.py"]}, "depends": {"ghost": ""}})
    with open(os.path.join(eng, "tools", "feat_tool.py"), "w") as fh:
        fh.write("# feat\n")
    with open(os.path.join(eng, "tools", "needy_tool.py"), "w") as fh:
        fh.write("# needy\n")
    return root


def add_demo() -> bool:
    """Fail-then-pass demonstration of `add`, returning True iff every step behaved. Real plan_add / add /
    derive / coherence logic runs against a throwaway fixture; only the release FETCH is faked (an injected
    local release tree — exactly the boundary _fetch_release_tree owns). Honest limit: a real release fetch
    is never exercised in the construction repo (no releases exist), so "works on the fixture ⇒ works for a
    real adopter" is the inductive step the fixture cannot discharge."""
    ok = True
    print("Part D — adding an optional module end-to-end on a throwaway fixture (the release fetch is "
          "faked; the copy / wire / coherence logic is real):")
    with tempfile.TemporaryDirectory() as d:
        live = os.path.join(d, "live")
        os.makedirs(live)
        release = _build_release_tree(os.path.join(d, "release"))
        with _redirect_root(live):
            _build_add_fixture(live)
            before = [f for f in module_coherence.check_coherence() if f["severity"] == "hard"]
            print("  fixture coherent before add: " + ("yes" if not before else f"NO: {before}"))
            ok = ok and not before
            res = add("feat", release_tree=release)
            for line in [f"added '{res.get('module_id')}' v{res.get('version')}"] + \
                    [f"copied {x}" for x in res.get("copied", [])] + \
                    res.get("applied_wires", []) + [f"groups now {res.get('groups_after')}"]:
                print("    - " + line)
            engine = module_coherence.load_engine_manifest()
            checks = {
                "feat tool copied in": os.path.exists(os.path.join(live, ".engine/tools/feat_tool.py")),
                "feat manifest copied in": os.path.isfile(
                    os.path.join(live, ".engine/modules/feat/manifest.json")),
                "engine.json records feat 0.1.0": (engine or {}).get("packages", {}).get("feat") == "0.1.0",
                "base survives": "base" in (engine or {}).get("packages", {}),
                "groups re-derived to [base, feat]": res.get("groups_after") == ["base", "feat"],
                "default-groups rewritten": committed_default_groups() == ["base", "feat"],
                "feat wire applied (gitignore fence present)":
                    "feat-cache" in validate.read(os.path.join(live, ".gitignore")),
                "coherent after add": not [f for f in res.get("findings", []) if f["severity"] == "hard"],
            }
            for label, good in checks.items():
                print(f"    [{'ok' if good else 'FAIL'}] {label}")
                ok = ok and good

            print("\nPart E — adding a module whose dependency is missing is refused (nothing changed):")
            needy = add("needy", release_tree=release)
            print("    -> " + (needy["reason"] if needy.get("refused") else "NOT refused?!"))
            unchanged = (not os.path.exists(os.path.join(live, ".engine/tools/needy_tool.py"))
                         and "needy" not in (module_coherence.load_engine_manifest() or {}).get("packages", {}))
            print(f"    [{'ok' if unchanged else 'FAIL'}] the refused add changed nothing")
            ok = ok and needy.get("refused") and "ghost" in (needy.get("reason") or "") and unchanged

            print("\nPart F — adding a module that is already installed is refused (safe to re-run):")
            again = add("feat", release_tree=release)
            print("    -> " + (again["reason"] if again.get("refused") else "NOT refused?!"))
            ok = ok and again.get("refused")
    print("\n" + ("ADD DEMO PASSED: a module was fetched-and-installed cleanly on the fixture, and the "
                  "missing-dependency and already-installed cases were refused."
                  if ok else "ADD DEMO DID NOT BEHAVE AS EXPECTED — see above."))
    return ok


# ---- upgrade demo (mutation-free, real logic, ALL FOUR boundaries faked) ----------------------

def _build_upgrade_fixture(root: str) -> None:
    """A minimal COHERENT live fixture engine at version 0.0.0: a required `base` module (one tool, no
    migrations yet), the engine manifest recording base 0.0.0 + a `solo` identity (operator config the
    upgrade must preserve), and the foundation code files an overlay replaces."""
    eng = os.path.join(root, ".engine")
    os.makedirs(os.path.join(eng, "modules", "base"))
    os.makedirs(os.path.join(eng, "tools"))
    os.makedirs(os.path.join(root, ".claude"))
    _write_json(os.path.join(eng, "modules", "base", "manifest.json"),
                {"id": "base", "version": "0.0.0", "status": "required",
                 "provides": {"tool": [".engine/tools/base_tool.py"]}, "depends": {}, "migrations": {},
                 "wires": [{"type": "gitignore", "key": "oldcache",
                            "lines": [".engine/base/.oldcache/"]}]})
    _write_json(os.path.join(eng, "engine.json"),
                {"engine_release": "0.0.0", "packages": {"base": "0.0.0"}, "identity": "solo",
                 "home_repository": "acme/engine-home"})   # the update home the fetch resolves + upgrade preserves
    with open(os.path.join(eng, "tools", "base_tool.py"), "w") as fh:
        fh.write("# base v0\n")
    with open(os.path.join(eng, "uv.lock"), "w") as fh:
        fh.write("# lock v0\n")
    with open(os.path.join(eng, "pyproject.toml"), "w") as fh:
        fh.write('[project]\nname = "x"\nversion = "0"\n\n[dependency-groups]\nbase = ["pkg-a"]\n\n'
                 '[tool.uv]\ndefault-groups = ["base"]\n')
    for name in (".mcp.json", os.path.join(".claude", "settings.json")):
        with open(os.path.join(root, name), "w") as fh:
            fh.write("{}\n")
    with open(os.path.join(root, ".gitignore"), "w") as fh:
        fh.write("# foundation\n.engine/.venv/\n")
    # apply vX's declared wire so the upgrade has an OLD wire to REVERSE (the delta's reverse leg)
    wiring.apply_all([{"type": "gitignore", "key": "oldcache", "lines": [".engine/base/.oldcache/"]}])


def _build_upgrade_release(root: str) -> str:
    """A throwaway extracted release tree (what _fetch_release_tree would return) at version 0.2.0: `base`
    bumped, its tool updated, and TWO migrations declared — a `config` transform (0.1.0, runs directly) and
    a `data` transform (0.2.0, backup-first). The migration `.py` files are in `base`'s `provides` (so the
    overlay copies them and the ownership leg claims them); each migrate(context) leaves an observable
    marker under .engine/state/ (claimed by base's state glob). Returns the tree root."""
    eng = os.path.join(root, ".engine")
    os.makedirs(os.path.join(eng, "modules", "base", "migrations"))
    os.makedirs(os.path.join(eng, "tools"))
    _write_json(os.path.join(eng, "modules", "base", "manifest.json"),
                {"id": "base", "version": "0.2.0", "status": "required",
                 "provides": {"tool": [".engine/tools/base_tool.py"],
                              "migration": [".engine/modules/base/migrations/*.py"],
                              "state": [".engine/state/*.json"]},
                 "depends": {},
                 "wires": [{"type": "gitignore", "key": "newcache",
                            "lines": [".engine/base/.newcache/"]}],
                 "migrations": {
                     "0.1.0": {"description": "Tidy a committed settings file for the new layout.",
                               "run": "migrations/config_010.py", "kind": "config"},
                     "0.2.0": {"description": "Reshape the stored data for the new format.",
                               "run": "migrations/data_020.py", "kind": "data"}}})
    with open(os.path.join(eng, "tools", "base_tool.py"), "w") as fh:
        fh.write("# base v2 (updated)\n")
    # the migration code runs IN the tool-runtime; it imports validate (module_manager already put the
    # tools dir on sys.path) to find the redirected ROOT — exactly how a real migration locates its store.
    cfg = ("import os, json, validate\n"
           "def migrate(context):\n"
           "    assert context['kind'] == 'config'\n"
           "    p = os.path.join(validate.ROOT, '.engine', 'state', 'config_marker.json')\n"
           "    os.makedirs(os.path.dirname(p), exist_ok=True)\n"
           "    with open(p, 'w') as fh:\n"
           "        json.dump({'ran': 'config', 'to': context['to_version']}, fh)\n")
    data = ("import os, json, validate\n"
            "def migrate(context):\n"
            "    assert context['kind'] == 'data'\n"
            "    handle = context['backup']('recall-ledger', context['engine_version'])\n"
            "    assert handle, 'backup-first: a data migration must snapshot before mutating'\n"
            "    p = os.path.join(validate.ROOT, '.engine', 'state', 'data_marker.json')\n"
            "    os.makedirs(os.path.dirname(p), exist_ok=True)\n"
            "    with open(p, 'w') as fh:\n"
            "        json.dump({'ran': 'data', 'stamp': context['engine_version']}, fh)\n")
    with open(os.path.join(eng, "modules", "base", "migrations", "config_010.py"), "w") as fh:
        fh.write(cfg)
    with open(os.path.join(eng, "modules", "base", "migrations", "data_020.py"), "w") as fh:
        fh.write(data)
    with open(os.path.join(eng, "uv.lock"), "w") as fh:           # foundation code the overlay replaces
        fh.write("# lock v2\n")
    with open(os.path.join(eng, "pyproject.toml"), "w") as fh:
        fh.write('[project]\nname = "x"\nversion = "0"\n\n[dependency-groups]\nbase = ["pkg-a"]\n\n'
                 '[tool.uv]\ndefault-groups = ["base"]\n')
    # The release ships BOTH the floor (CLAUDE.deployed.md — what the keyed-merge reads) and the maintainer
    # construction CLAUDE.md (which must NEVER overlay an adopter's floor — the latent-bug regression Part L
    # checks). The floor body carries a v2 marker so the merge is observable.
    with open(os.path.join(root, "CLAUDE.deployed.md"), "w", encoding="utf-8") as fh:
        fh.write("# Your project runs on an Engine (v2)\n\nProject status block, refreshed in v2.\n")
    with open(os.path.join(root, "CLAUDE.md"), "w", encoding="utf-8") as fh:
        fh.write("# engine-template — construction governance (v2 release)\n\nbuild scaffolding\n")
    return root


def upgrade_demo() -> bool:
    """Fail-then-pass demonstration of `upgrade`, returning True iff every step behaved. Real overlay /
    migration runner / coherence logic runs against a throwaway fixture; ALL FOUR side-effect boundaries
    are faked — the release fetch (injected release tree), the tool-runtime rebuild (skipped on a practice
    run), the git/PR open (injected fake opener), and the data backup (injected fake seam). Honest limit:
    none of those four ever runs against a live release in this template repo (which cuts no releases of
    itself), so "works on the fixture ⇒ works for a real adopter" is the inductive step the fixture cannot
    discharge."""
    ok = True
    print("Part G — updating the whole engine on a throwaway fixture. FAKED: the release fetch, the "
          "tool-runtime rebuild, the pull-request open, and the data backup. REAL: the overlay, the "
          "migration runner, and the consistency check. (None of those four ever runs for real here.)")
    pulls = []
    def fake_opener(branch, title, body):
        pulls.append({"branch": branch, "title": title})
        return {"number": 0, "title": title}
    snapshots = []
    def fake_backup(store, engine_version, **kw):                # **kw absorbs the migration_id run_migrations binds in
        snapshots.append((store, engine_version))
        return {"store": store, "engine_version": engine_version}

    with tempfile.TemporaryDirectory() as d:
        live = os.path.join(d, "live")
        os.makedirs(live)
        release = _build_upgrade_release(os.path.join(d, "release"))
        with _redirect_root(live):
            _build_upgrade_fixture(live)
            before = [f for f in module_coherence.check_coherence() if f["severity"] == "hard"]
            print("  fixture consistent before update: " + ("yes" if not before else f"NO: {before}"))
            ok = ok and not before

            print("\nPart H — an unreachable release leaves the engine on its current version (it degrades):")
            saved_fetch = globals().get("_fetch_release_tree")
            globals()["_fetch_release_tree"] = lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError("no such release"))
            try:
                degraded = upgrade(ref="v9.9.9")
            finally:
                globals()["_fetch_release_tree"] = saved_fetch
            still0 = (module_coherence.load_engine_manifest() or {}).get("packages", {}).get("base")
            print("    -> " + (degraded["reason"] if degraded.get("refused") else "NOT refused?!"))
            ok = ok and degraded.get("refused") and still0 == "0.0.0"

            print("\nPart I — an update that changes stored data is REFUSED with no backup set up "
                  "(nothing changes):")
            no_seam = upgrade(ref="v0.2.0", release_tree=release, opener=fake_opener, backup=None)
            still1 = (module_coherence.load_engine_manifest() or {}).get("packages", {}).get("base")
            print("    -> " + (no_seam["reason"] if no_seam.get("refused") else "NOT refused?!"))
            ok = ok and no_seam.get("refused") and still1 == "0.0.0" and not pulls and not snapshots

            print("\nPart J — the same update with a backup available runs end-to-end:")
            res = upgrade(ref="v0.2.0", release_tree=release, opener=fake_opener, backup=fake_backup)
            for line in [f"base {res['from'].get('base')} -> {res['to'].get('base')}"] + \
                    [f"replaced {x}" for x in res.get("copied", [])] + \
                    [f"ran {r}" for r in res.get("migrations", {}).get("ran", [])]:
                print("    - " + line)
            engine = module_coherence.load_engine_manifest()
            cfg_marker = os.path.join(live, ".engine", "state", "config_marker.json")
            data_marker = os.path.join(live, ".engine", "state", "data_marker.json")
            stamp = None
            if os.path.isfile(data_marker):
                with open(data_marker) as fh:
                    stamp = json.load(fh).get("stamp")
            checks = {
                "engine.json records base 0.2.0": (engine or {}).get("packages", {}).get("base") == "0.2.0",
                "operator identity preserved": (engine or {}).get("identity") == "solo",
                "base tool replaced with v2":
                    "v2" in validate.read(os.path.join(live, ".engine/tools/base_tool.py")),
                "config migration ran": os.path.isfile(cfg_marker),
                "data migration ran (after a backup)": os.path.isfile(data_marker),
                "backup taken before the data migration": snapshots == [("recall-ledger", "v0.2.0")],
                "data snapshot stamped with the engine version": stamp == "v0.2.0",
                "old wire reversed (oldcache gone from .gitignore)":
                    "oldcache" not in validate.read(os.path.join(live, ".gitignore")),
                "new wire applied (newcache present in .gitignore)":
                    "newcache" in validate.read(os.path.join(live, ".gitignore")),
                "consistent after the update":
                    not [f for f in res.get("findings", []) if f["severity"] == "hard"],
                "opened a pull request for review": bool(res.get("pr")) and len(pulls) == 1,
            }
            for label, good in checks.items():
                print(f"    [{'ok' if good else 'FAIL'}] {label}")
                ok = ok and good

            print("\nPart J2 — the update is fetched from the engine's recorded HOME, never this repo's own "
                  "origin (#367); and with NO home recorded it refuses with a remedy rather than guess a home:")
            seen = {}
            saved_fetch2 = globals().get("_fetch_release_tree")
            globals()["_fetch_release_tree"] = lambda ref, dest, repo=None, token=None: (
                seen.__setitem__("repo", repo) or (_ for _ in ()).throw(RuntimeError("stop after capture")))
            try:
                upgrade(ref="v0.2.0")            # real fetch path -> captures the SOURCE repo, then stops
            finally:
                globals()["_fetch_release_tree"] = saved_fetch2
            from_home = seen.get("repo") == "acme/engine-home"
            print(f"    [{'ok' if from_home else 'FAIL'}] fetched from the recorded home 'acme/engine-home' "
                  f"(saw {seen.get('repo')!r}), not this repo's own origin")
            ok = ok and from_home
            eng2 = module_coherence.load_engine_manifest()
            eng2.pop("home_repository", None)    # a repo generated BEFORE the home coordinate shipped
            _write_json(os.path.join(live, ".engine", "engine.json"), eng2)
            no_home = upgrade(ref="v0.2.0")
            asks_to_record = (no_home.get("refused")
                              and "no update home recorded" in (no_home.get("reason") or ""))
            print("    -> " + (no_home["reason"] if no_home.get("refused") else "NOT refused?!"))
            print(f"    [{'ok' if asks_to_record else 'FAIL'}] absent home refuses with a plain remedy, "
                  f"never falling back to origin")
            ok = ok and asks_to_record

    print("\nPart K — the update RE-RENDERS the code-ownership wall for the new version's engine files, so "
          "a file the new release adds still routes to the operator for review — and the operator's OWN "
          "rules are kept (the design's upgrade re-render):")
    with tempfile.TemporaryDirectory() as d:
        live = os.path.join(d, "live")
        os.makedirs(live)
        release = _build_upgrade_release(os.path.join(d, "release"))   # v0.2.0 ADDS migration .py files
        with _redirect_root(live):
            _build_upgrade_fixture(live)
            eng = module_coherence.load_engine_manifest()
            eng["handle"] = "@operator"        # the preserved-identity owner first-run records
            _write_json(os.path.join(live, ".engine", "engine.json"), eng)
            co_path = os.path.join(live, ".github", "CODEOWNERS")
            os.makedirs(os.path.dirname(co_path), exist_ok=True)
            with open(co_path, "w", encoding="utf-8") as fh:        # an operator rule + the OLD wall
                fh.write(wiring.render_codeowners("# my rules\n/src/ @team\n",
                                                  module_coherence.codeowners_path_set(), "@operator"))
            new_file = "/.engine/modules/base/migrations/config_010.py @operator"
            covered_before = new_file in validate.read(co_path)
            res = upgrade(ref="v0.2.0", release_tree=release,
                          opener=lambda **k: {"number": 0}, backup=lambda *a, **k: {"ok": 1})
            co_after = validate.read(co_path)
            co_checks = {
                "the wall did NOT cover the new file before the update": not covered_before,
                "the update re-rendered the wall": res.get("codeowners") == "written",
                "the new version's engine file now routes for review": new_file in co_after,
                "the operator's own rule survived untouched": "/src/ @team" in co_after,
            }
            for label, good in co_checks.items():
                print(f"    [{'ok' if good else 'FAIL'}] {label}")
                ok = ok and good

    print("\nPart L — the update KEYED-MERGES the root CLAUDE.md floor: it replaces ONLY the engine's marked "
          "block and keeps the operator's own content byte-for-byte, and the release's construction file "
          "never overlays the floor (the #234/#272 coexistence obligation + the latent-bug fix):")
    with tempfile.TemporaryDirectory() as d:
        live = os.path.join(d, "live")
        os.makedirs(live)
        release = _build_upgrade_release(os.path.join(d, "release"))   # ships a v2 floor + a construction file
        with _redirect_root(live):
            _build_upgrade_fixture(live)
            claude_path = os.path.join(live, "CLAUDE.md")
            top = "# My product\n\nHow we work here.\n\n"
            bottom = "\n## Contributing\n\nOpen a PR.\n"
            old_floor = wiring.fence_apply(
                "", _FLOOR_FENCE, ["# Old engine floor (v1)", "", "Project status block."],
                style=wiring.MD_FENCE)
            with open(claude_path, "w", encoding="utf-8") as fh:   # operator prose AROUND the engine block
                fh.write(top + old_floor + bottom)
            res = upgrade(ref="v0.2.0", release_tree=release,
                          opener=lambda **k: {"number": 0}, backup=lambda *a: {"ok": 1})
            after = validate.read(claude_path)
            cl_checks = {
                "the floor was keyed-merged": res.get("claude_floor") == "merged",
                "the operator's content above the block survived": top in after,
                "the operator's content below the block survived": bottom in after,
                "the new engine floor replaced the block": "Project status block, refreshed in v2." in after,
                "the old engine floor is gone": "Old engine floor (v1)" not in after,
                "the construction file did NOT overlay CLAUDE.md": "construction governance" not in after,
            }
            for label, good in cl_checks.items():
                print(f"    [{'ok' if good else 'FAIL'}] {label}")
                ok = ok and good

    print("\n" + ("UPGRADE DEMO PASSED: an unreachable release degraded, a data update with no backup was "
                  "refused, a backed-up update overlaid + migrated + opened a pull request cleanly, the "
                  "update re-rendered the code-ownership wall for the new files while keeping operator rules, "
                  "and it keyed-merged the CLAUDE.md floor while keeping the operator's own content."
                  if ok else "UPGRADE DEMO DID NOT BEHAVE AS EXPECTED — see above."))
    return ok


# ---- removal demo (CODEOWNERS render + clean whole-engine removal; ALL boundaries faked) -------

def _build_remove_fixture(root: str) -> None:
    """A coherent live fixture engine with engine-owned files BOTH under .engine/ and in .github/, plus a
    CODEOWNERS carrying an engine block after an operator rule — so the removal demo exercises every leg."""
    _build_fixture(root)                                  # base + optx (+ optx's shared-file edits applied)
    os.makedirs(os.path.join(root, ".github", "workflows"))
    with open(os.path.join(root, ".github", "workflows", "engine-ci.yml"), "w") as fh:
        fh.write("name: engine-ci\n")
    with open(os.path.join(root, "CLAUDE.md"), "w", encoding="utf-8") as fh:
        # An all-engine greenfield CLAUDE.md: the floor wrapped in the engine fence (what 6a's first-run seed
        # writes), so the removal demo exercises the real block-reversal (→ whitespace-only → file deleted).
        fh.write(wiring.fence_apply("", _FLOOR_FENCE, ["# engine floor"], style=wiring.MD_FENCE))
    co = wiring.render_codeowners("# product rules\n/src/ @team\n",
                                  [".engine/engine.json", ".github/workflows/engine-ci.yml"], "@operator")
    with open(os.path.join(root, ".github", "CODEOWNERS"), "w") as fh:
        fh.write(co)


def remove_engine_demo() -> bool:
    """Fail-then-pass demonstration of the CODEOWNERS renderer and clean whole-engine removal, returning
    True iff every leg behaved. The REAL render / shared-file reversal / delete-set / safety-rule-decision
    logic runs; FOUR boundaries are faked because none can run in the construction repo: (1) the GitHub
    branch-protection API, (2) the git/pull-request open, (3) a real deployed working tree, (4) the
    operator's real keep/remove choice. 'Works on the fixture ⇒ works for a real adopter' is the inductive
    gap the fixture cannot discharge."""
    ok = True
    prs = []

    def fake_opener(branch, title, body):
        prs.append((branch, title))
        return {"number": 0, "html_url": "(fixture)"}

    def fake_transport(method, path, body=None):
        if method == "GET" and path.endswith("/rulesets"):
            return (200, [{"id": 1, "name": bootstrap.ENGINE_RULESET_NAME}], {})
        return (200 if method == "PUT" else 204 if method == "DELETE" else 200, None, {})

    print("=" * 70)
    print("REMOVAL DEMO — CODEOWNERS ownership block + clean whole-engine removal, on a FIXTURE engine.\n"
          "The branch-protection setting, the pull-request open, and the operator's keep/remove choice are\n"
          "all faked; the real render / reversal / delete logic runs. None of this runs on the real engine.")

    print("\nPart K — the CODEOWNERS ownership block renders one file-precise line per engine file:")
    green = wiring.render_codeowners("", [".engine/engine.json", "CLAUDE.md"], "@operator")
    brown = wiring.render_codeowners("# product rules\n/src/ @team\n", [".engine/engine.json"], "@operator")
    k_ok = ("/.engine/engine.json @operator" in green and brown.startswith("# product rules")
            and brown.index("engine.json") > brown.index("/src/ @team"))
    print("    [{}] greenfield seeds a block; brownfield appends AFTER the product's rules (last wins)"
          .format("ok" if k_ok else "FAIL"))
    ok = ok and k_ok

    print("\nPart L — clean removal, KEEPING the main-branch safety rule (the engine's checks removed):")
    with tempfile.TemporaryDirectory() as d:
        with _redirect_root(d):
            _build_remove_fixture(d)
            r = remove_engine(opener=fake_opener, transport=fake_transport, choice="keep",
                              announce=lambda m: None)
            co_text = validate.read(os.path.join(d, ".github", "CODEOWNERS"))
            checks = {
                "the main-branch safety rule was kept, the engine's checks removed":
                    (r["de_bootstrap"] or {}).get("status") == "kept",
                "the module's shared-file edits were undone": bool(r["reversed"]),
                "a permission the operator also holds was left in place and disclosed":
                    bool(r["left_in_place"]),
                "the whole .engine/ tree was deleted": not os.path.isdir(os.path.join(d, ".engine")),
                "the engine's .github/ file was deleted (per-module remove never touches .github/)":
                    not os.path.isfile(os.path.join(d, ".github", "workflows", "engine-ci.yml")),
                "the all-engine CLAUDE.md (only the engine block) was removed":
                    not os.path.isfile(os.path.join(d, "CLAUDE.md")),
                "CODEOWNERS kept the product rule and dropped the engine block":
                    "/src/ @team" in co_text and "engine.json" not in co_text,
                "the deletions were opened as a (fixture) pull request for review": r["pr"] is not None,
            }
        for label, good in checks.items():
            print(f"    [{'ok' if good else 'FAIL'}] {label}")
            ok = ok and good
    print("    reversal note -> " + (r.get("reversal_note") or ""))

    print("\nPart M — clean removal, REMOVING the safety rule entirely (the operator's other choice):")
    deletes = []

    def drop_transport(method, path, body=None):
        if method == "DELETE":
            deletes.append(path)
        return fake_transport(method, path, body)
    with tempfile.TemporaryDirectory() as d:
        with _redirect_root(d):
            _build_remove_fixture(d)
            r2 = remove_engine(opener=fake_opener, transport=drop_transport, choice="drop",
                               announce=lambda m: None)
        m_ok = (r2["de_bootstrap"] or {}).get("status") == "dropped" and bool(deletes)
        print(f"    [{'ok' if m_ok else 'FAIL'}] the safety rule was removed entirely (a delete was issued)")
        ok = ok and m_ok

    print("\nPart N — clean removal on a BROWNFIELD repo whose OWN rule the engine augmented:")
    # The arrival added the engine's two checks (and a non_fast_forward rule) INTO the product's own ruleset
    # and recorded that in engine.json. Removal must reverse EXACTLY that and leave the product's rule —
    # never delete it, and never offer a keep/drop choice (the rule is the operator's, not the engine's).
    product_detail = {
        "id": 9, "name": "team protections", "target": "branch", "enforcement": "active",
        "node_id": "RRS_x", "_links": {"self": {"href": "x"}}, "created_at": "2026-01-01T00:00:00Z",
        "source": "owner/repo", "source_type": "Repository", "current_user_can_bypass": "always",
        "bypass_actors": [{"actor_id": 5, "actor_type": "Team", "bypass_mode": "always"}],
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "rules": [
            {"type": "pull_request", "parameters": {"required_approving_review_count": 1},
             "ruleset_source_type": "Repository", "ruleset_id": 9},
            {"type": "required_status_checks", "parameters": {
                "required_status_checks": [{"context": "product-ci"}, {"context": "engine-ci"},
                                           {"context": "engine-guard"}],
                "strict_required_status_checks_policy": False}, "ruleset_id": 9},
            {"type": "non_fast_forward", "ruleset_id": 9},   # engine-added (recorded in the marker)
        ],
    }
    n_puts, n_deletes = [], []

    def aug_transport(method, path, body=None):
        if method == "GET" and path.endswith("/rulesets"):
            return (200, [{"id": 9, "name": "team protections"}], {})   # NO engine-named ruleset present
        if method == "GET" and path.endswith("/rulesets/9"):
            return (200, product_detail, {})
        if method == "PUT" and path.endswith("/rulesets/9"):
            n_puts.append(body)
            return (200, {"id": 9}, {})
        if method == "DELETE":
            n_deletes.append(path)
            return (204, None, {})
        return (200, None, {})

    with tempfile.TemporaryDirectory() as d:
        with _redirect_root(d):
            _build_remove_fixture(d)
            # Record the augment marker the arrival would have written.
            eng_path = os.path.join(d, ".engine", "engine.json")
            eng = json.loads(validate.read(eng_path))
            eng["control_plane"] = {"ruleset_mode": "augmented", "augmented_ruleset_id": 9,
                                    "added": {"checks": ["engine-ci", "engine-guard"],
                                              "rules": ["non_fast_forward"]}}
            _write_json(eng_path, eng)
            rN = remove_engine(opener=fake_opener, transport=aug_transport, choice="keep",
                               announce=lambda m: None)
        put_body = n_puts[-1] if n_puts else {}
        put_rules = put_body.get("rules", [])
        put_types = {r.get("type") for r in put_rules}
        put_checks = bootstrap._bound_checks(put_rules)
        n_checks = {
            "the engine's checks were taken out of the product's rule (status 'unaugmented')":
                (rN["de_bootstrap"] or {}).get("status") == "unaugmented",
            "the product's rule was NOT deleted (it is the operator's)": not n_deletes,
            "exactly one update was written back to the product's rule": len(n_puts) == 1,
            "the engine checks are gone, the product's own check remains":
                put_checks == {"product-ci"},
            "the engine-added force-push rule was removed": "non_fast_forward" not in put_types,
            "the product's own pull-request rule was left untouched":
                {"type": "pull_request", "parameters": {"required_approving_review_count": 1}} in put_rules,
            "the operator's bypass list was preserved verbatim":
                put_body.get("bypass_actors") == product_detail["bypass_actors"],
        }
        for label, good in n_checks.items():
            print(f"    [{'ok' if good else 'FAIL'}] {label}")
            ok = ok and good

    print("\n" + ("REMOVAL DEMO PASSED: the ownership block rendered file-precisely, and the engine removed\n"
                  "itself cleanly on the fixture — it took its checks off the safety rule first, undid its\n"
                  "shared-file edits, deleted its files, and opened a reviewed pull request — for the engine's\n"
                  "own keep AND remove choices, AND for a brownfield repo whose own rule it had only augmented\n"
                  "(reversing exactly what it added, never deleting the operator's rule). The four real\n"
                  "boundaries named above are the inductive gap a fixture cannot discharge."
                  if ok else "REMOVAL DEMO DID NOT BEHAVE AS EXPECTED — see above."))
    return ok


def main(argv: list) -> int:
    if not argv:
        print("usage: module_manager.py {status | sync-groups | add <id> [--json] | "
              "plan-remove <id> | remove <id> [--json] | upgrade [ref] [--json] | "
              "remove-engine [--confirm] [--keep-protection|--remove-protection] [--json] | demo}",
              file=sys.stderr)
        return 2
    cmd = argv[0]
    try:
        if cmd == "status":
            return _status()
        if cmd == "sync-groups":
            res = sync_groups()
            tail = f"{res['groups'] or '(none)'}."
            print((f"Updated the tool-runtime dependency groups to match the installed modules: {tail}")
                  if res["changed"] else
                  (f"The tool-runtime dependency groups already match the installed modules: {tail}"))
            return 0
        if cmd == "demo":
            ok_remove = run_demo()
            print("\n" + ("-" * 70) + "\n")
            ok_add = add_demo()
            print("\n" + ("-" * 70) + "\n")
            ok_upgrade = upgrade_demo()
            print("\n" + ("-" * 70) + "\n")
            ok_remove_engine = remove_engine_demo()
            return 0 if (ok_remove and ok_add and ok_upgrade and ok_remove_engine) else 1
        if cmd == "plan-remove":
            if len(argv) < 2:
                print("CONFIG ERROR: plan-remove needs a module id.", file=sys.stderr)
                return 2
            plan = plan_remove(argv[1])
            if plan["refused"]:
                print(f"Removing '{argv[1]}' would be refused: {plan['reason']}")
                return 1
            print(f"'{argv[1]}' can be removed. It would undo {len(plan['wires'])} setting "
                  f"change(s), delete its files, and re-check that what remains is consistent.")
            return 0
        if cmd == "remove":
            if len(argv) < 2:
                print("CONFIG ERROR: remove needs a module id.", file=sys.stderr)
                return 2
            result = remove(argv[1])
            if "--json" in argv:
                print(json.dumps(result, indent=2))
            else:
                _render_remove(result)
            if result.get("refused"):
                return 1
            return 1 if any(f.get("severity") == "hard" for f in result.get("findings", [])) else 0
        if cmd == "add":
            if len(argv) < 2:
                print("CONFIG ERROR: add needs a module id.", file=sys.stderr)
                return 2
            result = add(argv[1])
            if "--json" in argv:
                print(json.dumps(result, indent=2))
            else:
                _render_add(result)
            if result.get("refused"):
                return 1
            return 1 if any(f.get("severity") == "hard" for f in result.get("findings", [])) else 0
        if cmd == "upgrade":
            ref = next((a for a in argv[1:] if not a.startswith("-")), None)
            result = upgrade(ref)
            if "--json" in argv:
                print(json.dumps(result, indent=2))
            else:
                _render_upgrade(result)
            # 0 only when the update actually landed a pull request; a refusal, a paused coherence
            # finding, a failed re-sync, or a PR that could not be opened all leave it un-landed -> 1.
            if result.get("refused"):
                return 1
            return 0 if result.get("pr") else 1
        if cmd == "remove-engine":
            # Destructive + operator-privileged: without --confirm this only PREVIEWS (changes nothing).
            if "--confirm" not in argv:
                print("Removing the WHOLE engine is a deliberate step. It takes the engine's checks off "
                      "your main branch's safety rule, removes the engine's entries from your shared setup "
                      "files, deletes all the engine's files, and opens a pull request with the deletions "
                      "for your review. Nothing has changed.\n\nTo proceed, re-run with --confirm and ONE "
                      "of:\n  --keep-protection    keep your main-branch safety rule (engine's checks "
                      "removed)\n  --remove-protection  remove your main-branch safety rule entirely")
                return 1
            keep_f, drop_f = "--keep-protection" in argv, "--remove-protection" in argv
            if keep_f == drop_f:   # neither, or BOTH (ambiguous) — never silently pick the destructive one
                print("CONFIG ERROR: remove-engine --confirm needs EXACTLY ONE of --keep-protection or "
                      "--remove-protection (your choice for the main-branch safety rule).", file=sys.stderr)
                return 2
            choice = "drop" if drop_f else "keep"
            result = remove_engine(choice=choice)
            if "--json" in argv:
                print(json.dumps(result, indent=2))
            else:
                _render_remove_engine(result)
            if result.get("refused"):
                return 1
            return 0 if result.get("pr") else 1
        print(f"unknown command {cmd!r}", file=sys.stderr)
        return 2
    except Exception as exc:  # a malformed manifest / engine.json halts loudly, never a traceback
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
