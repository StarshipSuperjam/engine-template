#!/usr/bin/env python3
"""First-run reference-closure gate (issue #150; engine-planning D-219/D-220) — the custom/script entry for
engine/check/first-run-reference-closure.

The first-run installer removes its own setup assets at the Retire step once setup is sound (provisioning's
definition-of-record). This check enforces the locked reference-closure invariant: NO file that SURVIVES that
removal may statically reference a removed first-run asset — by `import`, `importlib`, a subprocess invocation
of its path, or a hard-coded read of its path. In a generated repo such a reference fails the adopter's first
CI run at import/collection time with a Python error a non-engineer cannot read (`unittest discover` aborts
before any test runs). A surviving file that needs that machinery must itself be a first-run asset (removed in
the same pass) or stop referencing it; there is no "guard the import" escape (a top-level try/except still
red-fails when a test body later names the absent module).

It reads the removed-asset set from the committed manifest (.engine/provisioning/first-run-assets.json) — it
NEVER imports the instantiator it is about to verify nothing references (that would make this check the next
dangler). It runs as a hard CI custom/script check: finding.v1 JSON on stdout, return 0 on a successful
evaluation (empty array = closed; one finding per surviving reference, each carrying the full plain-language
consequence + disposition the operator reads). A crash returns non-zero, which the kind turns into a hard
fail-closed finding (a guard can never silently pass).

Honest static reach (principles §7): the `import` / `importlib` / `__import__` and literal subprocess-or-path
references are caught completely; a path assembled at runtime (a computed/indirected reference) is a behavioral
residual the invariant still forbids but this static check catches only best-effort — it is not claimed
complete. (A relative import — `from . import x` — is likewise not matched; the engine's tools use a flat,
absolute-import layout, so a removed top-level module is never reachable relatively, and such an import would
not resolve here anyway.) It no-ops (passes) once the first-run machinery is already removed — i.e. when no removed Python
module is present on disk (the adopter's post-setup tree) — there is nothing left to be closed over.
"""
from __future__ import annotations
import ast
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402  (finding.v1, ROOT)

_MANIFEST_REL = os.path.join(".engine", "provisioning", "first-run-assets.json")
_TOOLS_REL = os.path.join(".engine", "tools")
_PRUNE_DIRS = {"__pycache__", ".venv", ".pytest_cache", ".cache", ".uv"}


def _load_removed(root: str):
    """The removed first-run asset set, read from the committed manifest as plain data (never by importing the
    instantiator). Returns (removed_files, removed_modules): repo-relative file paths, and the bare module names
    of the removed `.py` files (an `import <name>` of which a survivor must not carry). Returns (None, None) if
    the manifest is unreadable — the check then degrades to a pass (manifest presence is another check's job)."""
    try:
        with open(os.path.join(root, _MANIFEST_REL), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None, None
    files = [f for f in data.get("files", []) if isinstance(f, str)]
    modules = {os.path.splitext(os.path.basename(f))[0] for f in files if f.endswith(".py")}
    return set(files), modules


def _survivors(root: str, removed_files: set) -> list:
    """Every committed `.engine/tools/**/*.py` that is NOT itself a removed first-run asset, repo-relative. The
    walk is RECURSIVE — `unittest discover -s tools -p 'test_*.py'` recurses into package subdirectories (e.g.
    `.engine/tools/memory/`), so a dangling import there breaks adopter collection just the same; the scan must
    mirror that reach, not a single-level glob."""
    out = []
    tools = os.path.join(root, _TOOLS_REL)
    for cur, dirs, names in os.walk(tools):
        dirs[:] = [d for d in dirs if d not in _PRUNE_DIRS]
        for name in names:
            if not name.endswith(".py"):
                continue
            rel = os.path.relpath(os.path.join(cur, name), root)
            if rel not in removed_files:
                out.append(rel)
    return sorted(out)


def _references(tree: ast.AST, removed_modules: set, removed_files: set) -> list:
    """Static references to a removed asset within one parsed survivor. Returns (lineno, kind, target) tuples:
    kind is 'import' (an import/importlib/__import__ of a removed module) or 'path' (a string literal equal to a
    removed file's exact repo-relative path — the read/subprocess-by-path leg). Exact-path match only, so a mere
    prose mention of a name (e.g. a docstring) is never flagged."""
    refs = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in removed_modules:
                    refs.append((node.lineno, "import", alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module and node.module.split(".")[0] in removed_modules:
                refs.append((node.lineno, "import", node.module))
        elif isinstance(node, ast.Call):
            fn = node.func
            is_import_call = (isinstance(fn, ast.Name) and fn.id == "__import__") or (
                isinstance(fn, ast.Attribute) and fn.attr == "import_module")
            if is_import_call and node.args and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, str) \
                    and node.args[0].value.split(".")[0] in removed_modules:
                refs.append((node.lineno, "import", node.args[0].value))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in removed_files:
            refs.append((node.lineno, "path", node.value))
    return refs


def _message(survivor: str, kind: str, target: str) -> str:
    """The operator-facing finding: consequence + concrete file/reference + disposition, in plain words (no
    engineer shorthand). custom/script surfaces only the per-finding message, so it carries the whole story."""
    how = (f"imports `{target}`" if kind == "import" else f"reads or runs `{target}` by name")
    return (
        f"`{survivor}` {how}, which the engine removes when a project is first set up. In a new project that "
        f"reference would make the very first automated check stop before it starts, with a programmer error its "
        f"owner cannot read. Fix it one of two ways: stop pointing at the removed setup code (move what you need "
        f"into a file that stays), or add `{survivor}` to the removed-files list "
        f"(.engine/provisioning/first-run-assets.json and _FIRST_RUN_ASSET_FILES in instantiator.py) so it is "
        f"taken away in the same pass — not by hiding the reference behind an error-catch, which still fails when "
        f"the code later names the missing file.")


def check(root: str | None = None) -> list:
    """Every surviving reference to a removed first-run asset, as a list of `hard` findings (empty = closed).
    No-ops to a pass when the manifest is unreadable, or when the first-run machinery is already removed (no
    removed Python module is present on disk — the adopter's post-setup tree)."""
    root = root or validate.ROOT
    removed_files, removed_modules = _load_removed(root)
    if removed_files is None:
        return []
    removed_py = [f for f in removed_files if f.endswith(".py")]
    if removed_py and not any(os.path.isfile(os.path.join(root, f)) for f in removed_py):
        return []  # machinery already removed (post-setup tree) — nothing left to be closed over
    findings = []
    for survivor in _survivors(root, removed_files):
        try:
            with open(os.path.join(root, survivor), encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=survivor)
        except (OSError, SyntaxError):
            continue
        for lineno, kind, target in _references(tree, removed_modules, removed_files):
            findings.append(validate.finding("hard", _message(survivor, kind, target),
                                             {"file": survivor, "line": lineno}))
    return findings


def main() -> int:
    print(json.dumps(check()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
