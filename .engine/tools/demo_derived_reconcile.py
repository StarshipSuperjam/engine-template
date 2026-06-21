#!/usr/bin/env python3
"""Demo — why a collision on the engine's internal index files is never your problem (#136 / principle 19).

What this checks, in plain words: when two pieces of work are in flight at once, they can both rewrite the
engine's internal index files — the knowledge graph and the self-map — and "collide" on them. This shows, on
a THROWAWAY COPY of your project, that such a collision loses NOTHING: those files are rebuilt entirely from
your real source files, so resolving the collision is just rebuilding them — every contribution from both
pieces of work is still there, and you are never handed the conflict to sort out.

It runs the REAL generator (the same code an engine session runs at "integrate"), not a stand-in. Nothing real
is touched — it works on a temporary copy and prints what it finds.

Run: uv run --directory .engine -- python tools/demo_derived_reconcile.py
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402
import knowledge_gen     # noqa: E402

A_TOOL, B_TOOL = "reconcile_demo_widget_a.py", "reconcile_demo_widget_b.py"
A_ID, B_ID = "tool:reconcile_demo_widget_a", "tool:reconcile_demo_widget_b"


def _entity_ids(graph_text: str) -> set:
    return {e["id"] for e in json.loads(graph_text)["entities"]}


def _generate_in(tree_root: str) -> str:
    """Run the REAL generator inside a copied tree and return the graph it produces. The copied tool
    resolves its repo root from its own file location, so it reads `tree_root`, not your real project."""
    tool = os.path.join(tree_root, ".engine", "tools", "knowledge_gen.py")
    proc = subprocess.run([sys.executable, tool, "show"], capture_output=True, text=True, check=True)
    return proc.stdout


def _add_tool(tree_root: str, name: str, title: str) -> None:
    with open(os.path.join(tree_root, ".engine", "tools", name), "w", encoding="utf-8") as fh:
        fh.write(f'"""{title}"""\n')


def _rm_tool(tree_root: str, name: str) -> None:
    os.remove(os.path.join(tree_root, ".engine", "tools", name))


def main(_argv=None) -> int:
    print("What this checks: a collision on the engine's internal index files loses no work — they are")
    print("rebuilt from your real source files, so rebuilding recovers everything. (#136 / principle 19)\n")

    # Part 1 — rebuilding is reproducible: the same sources always rebuild the same index, so "rebuild to
    # resolve" has one well-defined answer and the file carries nothing extra a rebuild could drop.
    with tempfile.TemporaryDirectory() as d:
        one, two = os.path.join(d, "graph1.json"), os.path.join(d, "graph2.json")
        knowledge_gen.generate(one)      # rebuild from your REAL sources, onto throwaway files
        knowledge_gen.generate(two)
        same = knowledge_gen.read_committed(one) == knowledge_gen.read_committed(two)
        verdict = "byte-for-byte identical" if same else "DIFFERS (unexpected!)"
        print(f"(1) Rebuilt your knowledge graph from your real sources twice: {verdict}.")
        print("    -> rebuilding is reproducible, so 'rebuild to resolve a collision' has one well-defined")
        print("       answer; the file holds nothing a rebuild could lose.\n")

    # Part 2 — two pieces of work, each adding its own source, both survive a rebuild from the merged tree.
    holder = tempfile.mkdtemp(prefix="reconcile-demo-")
    base = os.path.join(holder, "repo")
    try:
        shutil.copytree(validate.ROOT, base, symlinks=True,
                        ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", "*.pyc"))

        _add_tool(base, A_TOOL, "Widget A — work from one branch")
        ids_a = _entity_ids(_generate_in(base))               # "branch A" rebuilt the index

        _rm_tool(base, A_TOOL)
        _add_tool(base, B_TOOL, "Widget B — work from another branch")
        ids_b = _entity_ids(_generate_in(base))               # "branch B" rebuilt the index

        _add_tool(base, A_TOOL, "Widget A — work from one branch")   # the reconciled tree carries BOTH
        ids_both = _entity_ids(_generate_in(base))

        both = A_ID in ids_both and B_ID in ids_both
        caption = "both survived" if both else "MISMATCH — a contribution was dropped; investigate"
        print("(2) Two pieces of work, each rebuilding the index its own way (they would 'collide'):")
        print(f"      branch A's index has A={A_ID in ids_a}, B={B_ID in ids_a}")
        print(f"      branch B's index has A={A_ID in ids_b}, B={B_ID in ids_b}")
        print("    Resolve the collision by rebuilding from the merged sources:")
        print(f"      rebuilt index has A={A_ID in ids_both} AND B={B_ID in ids_both}   <- {caption}\n")
        ok = same and A_ID in ids_a and B_ID in ids_b and both
    finally:
        shutil.rmtree(holder, ignore_errors=True)

    if ok:
        print("In plain words: the collision was 'spurious' — both sides were only rebuilds of the same")
        print("sources, so rebuilding from the merged sources recovered every contribution. No work lost,")
        print("and you were never handed a conflict to resolve.\n")
        print("Vary it yourself: rename the two widgets at the top of this file, or add a third, and re-run —")
        print("watch each one survive the rebuild. Your real project is never touched.")
    else:
        print("This run did NOT confirm 'no work lost' — the rebuilt index above is missing a contribution.")
        print("That is a real signal worth investigating, not a pass. Your real project was not touched.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
