#!/usr/bin/env python3
"""Module manager — the permanent provisioning primitive that adds and removes engine modules
over a repo's life (systems/infrastructure/provisioning/README.md §"The module manager").

This slice (25b) ships the **remove** verb, the **group-scoped uv-sync derivation**, and the CLI
that surfaces both. `add`, the engine updater/upgrade, migrations, and de-bootstrap-on-clean-
removal share the fetch-tagged-release machinery and land in a later slice (25c).

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
  python tools/module_manager.py plan-remove <id>    # read-only: refusal reasons / what remove would do
  python tools/module_manager.py remove <id> [--json]
  python tools/module_manager.py demo                # mutation-free fail-then-pass (real logic, fixture)
"""
from __future__ import annotations
import contextlib
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


def main(argv: list) -> int:
    if not argv:
        print("usage: module_manager.py {status | plan-remove <id> | remove <id> [--json] | demo}",
              file=sys.stderr)
        return 2
    cmd = argv[0]
    try:
        if cmd == "status":
            return _status()
        if cmd == "demo":
            return 0 if run_demo() else 1
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
        print(f"unknown command {cmd!r}", file=sys.stderr)
        return 2
    except Exception as exc:  # a malformed manifest / engine.json halts loudly, never a traceback
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
