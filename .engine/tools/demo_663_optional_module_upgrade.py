#!/usr/bin/env python3
"""Behavioral FALSIFICATION for issue #663 — the engine REGENERATES its wiring map on a deployment that
DECLINED an optional module, and that regen must NOT mistake the declined module's legitimately-absent subtree
for a dangling (rename-residue) import. This is the exact step that failed in the field: a clean upgrade to
0.4.0 on a deployment WITHOUT the optional meaning-based-recall module applied its files, then the graph regen
raised `DanglingImportError` on the always-present substrate's `from memory.semantic import ...` imports, the
`knowledge-coverage` structural gate failed closed, and the upgrade half-applied with no pull request opened.

FAIL-THEN-PASS on the SAME module-absent fixture; the only difference between the two arms is whether the
optional-subtree carve-out is active:
  * POSITIVE (the fix): the resolver drops the runtime-guarded `from memory.semantic import …` imports whose
    WHOLE subtree is absent, so the graph regenerates cleanly and no dangling-import finding is raised — the
    upgrade's regen step (the one that fails closed in #663) succeeds.
  * NEGATIVE CONTROL (the bug): with the carve-out disabled the resolver raises `DanglingImportError` on those
    exact imports — reproducing #663 — and the graph cannot regenerate.

The fixture is a throwaway COPY of this engine with the OPTIONAL module REMOVED (its `memory/semantic` subtree
AND its manifest deleted) — the precise shape a deployment that opted out of it carries: the substrate's
`mcp_server.py`/`rescrub.py` still import `memory.semantic`, but the subtree is gone. It exercises the REAL
graph generator (`knowledge_gen.generate`, the same call the upgrade tail's `_regen_indexes` makes) against
that tree, isolating the resolver behaviour, not a hand-built stub.

This demo is CONSTRUCTION EVIDENCE, retired from a generated repo at first-run (it clones the whole engine and
is home-repo-only) — the durable CI guard for the resolver logic is `test_knowledge.TestOptionalModuleSubtree*`,
and the permanent end-to-end deployed-upgrade belt is the release-cut gate (#664). Run it directly:
`uv run --directory .engine -- python tools/demo_663_optional_module_upgrade.py`.
"""
from __future__ import annotations
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate                 # noqa: E402
import module_manager as mm     # noqa: E402  (its _redirect_root points the generators at the fixture tree)
import knowledge_gen            # noqa: E402  (the real graph generator under test)

# The optional module a deployment can decline — its subtree and its manifest, both removed to model opt-out.
_SEMANTIC_SUBTREE_REL = os.path.join(".engine", "tools", "memory", "semantic")
_SEMANTIC_MANIFEST_REL = os.path.join(".engine", "modules", "memory-semantic-recall", "manifest.json")

_COPY_IGNORE = shutil.ignore_patterns(".venv", "__pycache__", "worktrees", "node_modules", "*.pyc", ".git")
_COPY_DIRS = (".engine", ".claude", ".codex", ".agents", ".github")
_COPY_FILES = (".mcp.json", ".gitignore", "CLAUDE.md", "AGENTS.md")


def _clone_engine(real_root: str, dest: str) -> str:
    """Copy this repo's real engine surface into `dest` — a genuine engine whose graph generates cleanly, so
    the falsification isolates the optional-subtree behaviour, not a broken fixture."""
    os.makedirs(dest, exist_ok=True)
    for rel in _COPY_DIRS:
        src = os.path.join(real_root, rel)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(dest, rel), ignore=_COPY_IGNORE, symlinks=True)
    for rel in _COPY_FILES:
        src = os.path.join(real_root, rel)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(dest, rel))
    return dest


def _decline_optional_module(tree: str) -> None:
    """Remove the optional meaning-based-recall module from the clone — its tool subtree AND its manifest — so
    the tree is exactly what a deployment that opted out of it carries: the substrate importers remain, the
    subtree is gone, and no manifest claims the now-absent files."""
    shutil.rmtree(os.path.join(tree, _SEMANTIC_SUBTREE_REL))
    os.remove(os.path.join(tree, _SEMANTIC_MANIFEST_REL))


def _mcp_server_imports_semantic(graph_path: str) -> bool:
    """True iff the regenerated graph records an edge from tool:mcp_server into the (absent) semantic subtree —
    which it must NOT, since the module is declined here."""
    with open(graph_path, encoding="utf-8") as fh:
        graph = json.load(fh)
    for entity in graph.get("entities", []):
        if entity.get("id") == "tool:mcp_server":
            targets = (entity.get("predicates", {}) or {}).get("imports", []) or []
            return any("semantic" in str(t) or t in ("tool:store", "tool:embed") for t in targets)
    return False


def main() -> int:
    real_root = validate.ROOT   # capture the REAL repo before any redirect
    failures = []
    print("=" * 78)
    print("DEMO #663 — regenerating the wiring map on a deployment that DECLINED the optional recall module")
    print("must not mistake that module's absent subtree for a dangling import. Same module-absent fixture,")
    print("two arms; the only difference is whether the optional-subtree carve-out runs.")
    print("=" * 78)

    # ---- POSITIVE: the carve-out drops the absent-subtree imports; the graph regenerates cleanly ----
    with tempfile.TemporaryDirectory() as d:
        tree = _clone_engine(real_root, os.path.join(d, "declined"))
        _decline_optional_module(tree)
        graph_path = os.path.join(tree, ".engine", "knowledge", "graph.json")
        raised = None
        with mm._redirect_root(tree):
            try:
                knowledge_gen.generate(path=graph_path)
            except knowledge_gen.DanglingImportError as exc:
                raised = str(exc)
        regen_clean = raised is None
        edge_dropped = regen_clean and not _mcp_server_imports_semantic(graph_path)
        print("\n[POSITIVE — the optional-subtree carve-out runs]")
        print(f"  graph regenerated without a dangling-import raise:  {regen_clean}")
        print(f"  the absent-subtree import was dropped (no edge):    {edge_dropped}")
        if not regen_clean:
            failures.append(f"POSITIVE: the graph regen raised on the declined module: {raised}")
        if regen_clean and not edge_dropped:
            failures.append("POSITIVE: an edge into the absent semantic subtree was recorded (not dropped)")

    # ---- NEGATIVE CONTROL: disable the carve-out — the resolver raises exactly as #663 did ----
    with tempfile.TemporaryDirectory() as d:
        tree = _clone_engine(real_root, os.path.join(d, "declined"))
        _decline_optional_module(tree)
        graph_path = os.path.join(tree, ".engine", "knowledge", "graph.json")
        original = knowledge_gen._OPTIONAL_MODULE_SUBTREES
        knowledge_gen._OPTIONAL_MODULE_SUBTREES = frozenset()   # the pre-#663 resolver: no carve-out
        raised = None
        try:
            with mm._redirect_root(tree):
                try:
                    knowledge_gen.generate(path=graph_path)
                except knowledge_gen.DanglingImportError as exc:
                    raised = str(exc)
        finally:
            knowledge_gen._OPTIONAL_MODULE_SUBTREES = original
        reproduced = raised is not None and "memory.semantic" in raised
        print("\n[NEGATIVE CONTROL — carve-out disabled (the pre-#663 resolver)]")
        print(f"  graph regen raised DanglingImportError on memory.semantic:  {reproduced}")
        if not reproduced:
            failures.append("NEGATIVE: disabling the carve-out did NOT reproduce #663's dangling-import raise")

    print("\n" + "=" * 78)
    if failures:
        print("DEMO #663 FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("DEMO #663 PASSED: on a deployment that declined the optional module, the carve-out lets the wiring "
          "map regenerate cleanly (the step that fails closed in #663); disabling it reproduces the exact "
          "dangling-import raise. The carve-out is load-bearing for a clean deployed upgrade.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
