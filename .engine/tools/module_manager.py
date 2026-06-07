#!/usr/bin/env python3
"""Module manager — the permanent provisioning primitive that adds and removes engine modules
over a repo's life (systems/infrastructure/provisioning/README.md §"The module manager").

Slice 25b shipped **remove** + the **group-scoped uv-sync derivation**. Slice 25c (this change) adds
**add** (install a module at the current release) and its shared **fetch/overlay** primitive, plus the
standalone **sync-groups** fixer (the one-command fix the uv-group-drift check points at). The engine
updater/upgrade, migrations, and de-bootstrap-on-clean-removal — the rest of the fetch-and-overlay
machinery — land in the following 25c pull requests.

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
step a later slice owns). It touches **no** control-plane ruleset: an ordinary remove changes only
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
  python tools/module_manager.py demo                # mutation-free fail-then-pass (remove + add; fixtures)
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

    # (2) delete the engine-identified files the module owns — sole-owner, under .engine/ only
    target_claims = module_coherence.provides_claims([(manifest_path, target)])
    others = [(p, m) for p, m in manifests if m.get("id") != module_id]
    other_claims = module_coherence.provides_claims(others)
    for rel in sorted(target_claims):
        if rel not in other_claims and rel.startswith(".engine/"):
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
#      it in a later slice) ----------------------------------------------------------------------

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
            tmp = tempfile.mkdtemp(prefix="engine-add-")
            try:
                release_tree = _fetch_release_tree(target_ref, tmp)
            except Exception as exc:   # offline / missing release / transport — degrade loud + plain (§5)
                return {"module_id": module_id, "refused": True, "applied": False,
                        "reason": f"Couldn't reach the engine's release '{target_ref}' to add "
                                  f"'{module_id}' — the release may not exist yet, or the network is "
                                  f"unavailable. Nothing was changed. ({exc})"}
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
    ok = ok and vc["refused"] and "required" in vc["reason"]              # required-foundation refusal

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
                {"engine_release": "0.0.0", "packages": {"base": "0.0.0"}, "identity": "solo"})
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


def main(argv: list) -> int:
    if not argv:
        print("usage: module_manager.py {status | sync-groups | add <id> [--json] | "
              "plan-remove <id> | remove <id> [--json] | demo}", file=sys.stderr)
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
            return 0 if (ok_remove and ok_add) else 1
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
        print(f"unknown command {cmd!r}", file=sys.stderr)
        return 2
    except Exception as exc:  # a malformed manifest / engine.json halts loudly, never a traceback
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
