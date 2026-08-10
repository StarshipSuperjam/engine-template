#!/usr/bin/env python3
"""Manifest-write funnel floor (engine-template #923) — the custom/script entry for
engine/check/manifest-write-funnel.

The engine must never write its own deployed manifest (`.engine/engine.json`) THROUGH a symlink or to a
path escaping the tree. #862 and #923 homed that invariant in `engine_write` and routed every known writer
through it — but that convergence rested on discipline: a NEW writer inherits the guard only if its author
remembers to. Eight writers were discovered across seven review rounds spanning two pull requests, and the
pattern kept recurring — so this check makes convergence MECHANICAL rather than disciplinary.

It statically scans `.engine/tools/**/*.py` and flags any function that WRITES the deployed manifest slot
without routing through the guarded funnel. A function is a violation when all three hold in its own
(non-nested) body:
  1. a raw write primitive — `os.replace(...)`, an `open(...)`/`os.fdopen(...)` in a write mode, a
     `pathlib` `.write_text`/`.write_bytes`, or a call to a GENERIC unguarded writer (`_write_json` /
     `_write_text` — the primitives whose real job is writing fixtures/release trees, deliberately left
     unguarded);
  2. a reference to the DEPLOYED manifest slot — the path helpers `_engine_manifest_path` /
     `_engine_json_path`, the `ENGINE_MANIFEST_REL` constant, or a literal `engine.json` joined onto
     `validate.ROOT` (the bare-literal-against-the-real-root shape); and
  3. NO reference to the funnel — `engine_write`, `write_through_symlink_reason` (incl. the aliased
     `_write_through_symlink_reason`), `_write_engine_manifest`, `_manifest_write_reason`, or the
     `EngineWriteRefused` exception.

By construction this catches the deployed slot while EXCLUDING fixture/demo writers, which target a literal
`engine.json` joined onto a LOCAL temp root (`os.path.join(eng, "engine.json")`), not `validate.ROOT` and not
a path helper — so they never satisfy (2), and no allowlist is needed. It would have caught the original
bug shape (`_write_json(_engine_manifest_path(), engine)`): generic writer + path helper + no funnel.

Scope: the DEPLOYED `.engine/engine.json` slot specifically — the clearest, best-defined invariant. It does
not police the other engine-owned slots (`.engine/pyproject.toml`, the sealed audit digest, `.engine/state/`),
which have their own path vocabularies and their own guards; extending the funnel check to them is a separate,
larger design. Read opens of the manifest (no write mode) and the generic writers themselves (no manifest
reference) are correctly untouched.

Runs as a hard CI custom/script check: finding.v1 JSON on stdout, return 0 on a successful evaluation (empty
array = every manifest writer routes through the funnel). A crash returns non-zero, which the kind turns into
a hard fail-closed finding.
"""
from __future__ import annotations
import ast
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402  (finding.v1, ROOT)

_TOOLS_REL = os.path.join(".engine", "tools")
_PRUNE_DIRS = {"__pycache__", ".venv", ".pytest_cache", ".cache", ".uv"}

# (2) DEPLOYED-manifest path helpers — a reference to one of these names means "the real slot".
_MANIFEST_PATH_NAMES = ("_engine_manifest_path", "_engine_json_path", "ENGINE_MANIFEST_REL")
# (3) FUNNEL markers — an identifier containing any of these means the write is guarded (substring match
# so the instantiator aliases `_write_through_symlink_reason` / `_EngineWriteRefused` are recognised too).
_FUNNEL_MARKERS = ("engine_write", "write_through_symlink_reason", "write_engine_manifest",
                   "manifest_write_reason", "EngineWriteRefused")
# (1) GENERIC unguarded writers — a call to one of these is a raw write for this check's purposes; the
# guarded `engine_write.write_json` (attr `write_json`, no leading underscore) is deliberately NOT here.
_GENERIC_WRITERS = ("_write_json", "_write_text")
_PATHLIB_WRITES = ("write_text", "write_bytes")


def _tool_files(root: str) -> list:
    """Every committed `.engine/tools/**/*.py` that is a shipped tool — excluding `test_*.py` (they plant
    deliberate violations as fixtures). Recursive, to reach the `memory/` and `product_design/` packages."""
    out = []
    tools = os.path.join(root, _TOOLS_REL)
    for cur, dirs, names in os.walk(tools):
        dirs[:] = [d for d in dirs if d not in _PRUNE_DIRS]
        for name in names:
            if not name.endswith(".py") or name.startswith("test_"):
                continue
            out.append(os.path.relpath(os.path.join(cur, name), root))
    return sorted(out)


def _own_body(node):
    """Yield every node lexically within a function's body WITHOUT descending into a nested function/lambda
    scope — so a nested closure is analysed as its own unit, never folded into its parent (mirrors
    in_tool_demo_failure_path_check._walk_no_scope). The nested def node itself is yielded (so a call TO it is
    visible), but its body is not."""
    stack = list(node.body)
    while stack:
        cur = stack.pop()
        yield cur
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(cur))


def _mode_is_write(call: ast.Call) -> bool:
    """True if an `open`/`os.fdopen` call's mode argument admits writing (contains w/a/x/+). A missing mode
    is a read (both default to 'r'); a NON-literal mode is treated as a write (fail-closed — can't prove a
    read). `open`'s mode is positional arg 1; `os.fdopen`'s is positional arg 1 too (after the fd)."""
    mode = None
    if len(call.args) >= 2:
        mode = call.args[1]
    for kw in call.keywords:
        if kw.arg == "mode":
            mode = kw.value
    if mode is None:
        return False
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return any(c in mode.value for c in "wax+")
    return True  # a computed mode — cannot prove it is read-only, so treat as a write


def _is_write_primitive(n) -> bool:
    if not isinstance(n, ast.Call):
        return False
    f = n.func
    # os.replace(src, dst) — an atomic rename, always a write of dst
    if isinstance(f, ast.Attribute) and f.attr == "replace" \
            and isinstance(f.value, ast.Name) and f.value.id == "os":
        return True
    # open(...) / os.fdopen(...) in a write mode
    if isinstance(f, ast.Name) and f.id == "open":
        return _mode_is_write(n)
    if isinstance(f, ast.Attribute) and f.attr == "fdopen" \
            and isinstance(f.value, ast.Name) and f.value.id == "os":
        return _mode_is_write(n)
    # pathlib .write_text / .write_bytes
    if isinstance(f, ast.Attribute) and f.attr in _PATHLIB_WRITES:
        return True
    # a GENERIC unguarded writer (_write_json / _write_text), by Name or Attribute (e.g. wiring._write_json)
    if isinstance(f, ast.Name) and f.id in _GENERIC_WRITERS:
        return True
    if isinstance(f, ast.Attribute) and f.attr in _GENERIC_WRITERS:
        return True
    return False


def _identifiers(nodes) -> set:
    """Every Name.id and Attribute.attr appearing in `nodes` — the vocabulary a substring match reads."""
    out = set()
    for n in nodes:
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
    return out


def _references_validate_root(nodes) -> bool:
    return any(isinstance(n, ast.Attribute) and n.attr in ("ROOT", "ENGINE_DIR")
               and isinstance(n.value, ast.Name) and n.value.id == "validate" for n in nodes)


def _has_manifest_literal(nodes) -> bool:
    return any(isinstance(n, ast.Constant) and isinstance(n.value, str) and "engine.json" in n.value
              for n in nodes)


# A fixture/demo context writes a throwaway `engine.json` under a REDIRECTED root (a tempdir), which is
# statically indistinguishable from the real slot by the bare literal alone — so the literal rule is
# suppressed here. The path-helper rule still applies (a fixture never calls the deployed-slot helper).
_FIXTURE_MARKERS = ("tempfile", "TemporaryDirectory", "mkdtemp", "_redirect_root")


def _reassigns_validate_root(nodes) -> bool:
    for n in nodes:
        if isinstance(n, (ast.Assign, ast.AnnAssign)):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            for t in targets:
                for sub in ast.walk(t):
                    if isinstance(sub, ast.Attribute) and sub.attr in ("ROOT", "ENGINE_DIR") \
                            and isinstance(sub.value, ast.Name) and sub.value.id == "validate":
                        return True
    return False


def _fixture_context(idents: set, nodes: list) -> bool:
    return bool(idents & set(_FIXTURE_MARKERS)) or _reassigns_validate_root(nodes)


def _funnel_ref(idents: set) -> bool:
    return any(any(m in ident for m in _FUNNEL_MARKERS) for ident in idents)


def _manifest_ref(idents: set, nodes: list) -> bool:
    # (a) the deployed-slot PATH HELPERS — the way every real writer names the slot.
    if idents & set(_MANIFEST_PATH_NAMES):
        return True
    # (b) the bare-literal-against-the-real-root shape — a literal "engine.json" joined onto
    # validate.ROOT/ENGINE_DIR — but NOT in a fixture context, which writes the same literal under a
    # redirected temp root (an unavoidable static ambiguity; the helper rule still covers real writers).
    return (_has_manifest_literal(nodes) and _references_validate_root(nodes)
            and not _fixture_context(idents, nodes))


def _message(rel: str, func: str) -> str:
    return (
        f"`{func}` in `{rel}` writes the engine's deployed manifest (.engine/engine.json) with a raw write "
        f"that does not route through the guarded write funnel (engine_write / _write_engine_manifest / "
        f"_manifest_write_reason). A manifest writer that bypasses the funnel can follow a planted shortcut "
        f"(symlink) and place the engine's own file outside the repository — the out-of-tree-write class "
        f"#862/#923 exist to close. Route this write through `engine_write.write_json` (or pre-flight the "
        f"destination with `engine_write.write_through_symlink_reason`) before writing, as every other "
        f"manifest writer does. If this is a fixture/demo writing a throwaway tree, target a local temp "
        f"root, not `validate.ROOT` or a path helper, so it is not the deployed slot.")


def _funcs(tree):
    """Every function/async-function scope in the module, plus a synthetic module-scope unit for top-level
    statements (so a bare module-level manifest write is caught too)."""
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield n.name, n, list(_own_body(n))
    module_stmts = [s for s in tree.body
                    if not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    if module_stmts:
        nodes = []
        for s in module_stmts:
            nodes.extend(ast.walk(s))
        yield "<module>", tree, nodes


def check(root: str | None = None) -> list:
    """Every function that writes the deployed manifest outside the funnel, as `hard` findings (empty = every
    manifest writer routes through the funnel)."""
    root = root or validate.ROOT
    findings = []
    for rel in _tool_files(root):
        try:
            with open(os.path.join(root, rel), encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=rel)
        except (OSError, SyntaxError):
            continue
        for fname, node, nodes in _funcs(tree):
            if not any(_is_write_primitive(n) for n in nodes):
                continue
            idents = _identifiers(nodes)
            if not _manifest_ref(idents, nodes):
                continue
            if _funnel_ref(idents):
                continue
            findings.append(validate.finding("hard", _message(rel, fname),
                                             {"file": rel, "line": getattr(node, "lineno", None)}))
    return findings


def main() -> int:
    # ENGINE_ROOT (unset in production) lets the negative-fixture meta-check point the scan at a seeded
    # mini-tree carrying a manifest writer that bypasses the funnel, so the gate is witnessed biting a real
    # bad input (#286 fixture seam).
    print(json.dumps(check(validate.env_override_path("ENGINE_ROOT"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
