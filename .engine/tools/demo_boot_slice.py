#!/usr/bin/env python3
"""Operator-runnable demo of boot's rung-1 knowledge cache — the gitignored boot slice (#37).

It answers, in plain words, a question a non-engineer can't read code to verify: *when the engine caches the
knowledge it reads at orientation, does the cache say EXACTLY what a fresh live read would — and does the
engine still orient when the cache is stale, missing, or its source is gone?* It runs the REAL logic
end-to-end over the REAL committed knowledge graph — boot_slice's build + read-shim, attention's bidirectional
walk, and boot's REAL render — in an ISOLATED temp cache (and, for the freshness scenario, an isolated COPY of
the graph), so it never touches your live `.cache/` and needs no network, no token, no edits.

It shows three honest things:
  * FAITHFUL — a focus member's rendered neighbourhood, read from the CACHE, is BYTE-IDENTICAL to the same
    block read from a fresh LIVE WALK: the cache never invents or drops a relationship, and never re-samples a
    hub's truncated list differently;
  * HONEST FRESHNESS — changing the graph's CONTENT (one byte) flips the cache stale, then it rebuilds; a bare
    file `touch` (which leaves the content unchanged) does NOT, because the freshness key is the content
    fingerprint, not the clock;
  * NEVER BLOCKS — delete the cache and it self-heals (rebuilds on the next read); take the committed graph
    away entirely and it still orients from a live walk of the surfaces (degrade, not failure).

Vary it yourself: run it, then in a scratch copy change a byte of graph.json and re-run `boot_slice.py status`,
or delete `.engine/knowledge/.cache/boot-slice.json` and watch the next boot rebuild it.

Run: uv run --directory .engine -- python tools/demo_boot_slice.py
"""
from __future__ import annotations
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import attention          # noqa: E402
import boot               # noqa: E402
import boot_slice         # noqa: E402
import knowledge_gen      # noqa: E402

FOCUS = ["module:core"]   # a hub: its neighbourhood truncates, so this also exercises the honest-count render


def _render(source) -> list:
    """boot's REAL orientation block for FOCUS, read through `source` (None -> the live walk; a Slice -> the
    cache). Returns the rendered lines (with the focus count attached, exactly as boot does)."""
    nb = attention.neighborhood_of(FOCUS, source=source)
    if nb is None:
        return []
    nb["focus_total"] = len(FOCUS)
    return boot.render_neighborhood(nb)


def _print(title: str, lines: list) -> None:
    print(title)
    for ln in lines:
        if ln:
            print(f"     {ln}")
    print()


def main(argv: list | None = None) -> int:
    print("Boot's rung-1 knowledge cache — does the cache match the live read, and never block orientation? "
          "(#37)\n")
    work = tempfile.mkdtemp()
    try:
        slice_path = os.path.join(work, "boot-slice.json")

        # 1) FAITHFUL: the cache render vs the live-walk render, byte for byte.
        boot_slice.build(slice_path=slice_path)                  # from the real committed graph
        shim = boot_slice.read(slice_path=slice_path)
        cached = _render(shim)
        live = _render(None)                                     # the live walk (knowledge_query)
        _print("1) The orientation block, read from the CACHE:", cached)
        faithful = (cached == live and bool(cached))
        print(f"   -> byte-identical to a fresh LIVE WALK of the same graph? {'YES' if faithful else 'NO'}\n")

        # 2) HONEST FRESHNESS: content moves the fingerprint; a bare touch does not.
        gdir = os.path.join(work, "graph")
        os.makedirs(gdir)
        graph_copy = os.path.join(gdir, "graph.json")
        with open(knowledge_gen.GRAPH_PATH, encoding="utf-8") as fh:
            original = fh.read()
        with open(graph_copy, "w", encoding="utf-8") as fh:
            fh.write(original)
        sp2 = os.path.join(gdir, "boot-slice.json")
        boot_slice.build(slice_path=sp2, graph_path=graph_copy)
        fresh0 = boot_slice.is_fresh(slice_path=sp2, graph_path=graph_copy)
        os.utime(graph_copy, None)                               # a bare touch: content unchanged
        fresh_after_touch = boot_slice.is_fresh(slice_path=sp2, graph_path=graph_copy)
        with open(graph_copy, "w", encoding="utf-8") as fh:      # a real content change
            fh.write(original.replace("}", "} ", 1))
        stale_after_edit = not boot_slice.is_fresh(slice_path=sp2, graph_path=graph_copy)
        boot_slice.ensure(slice_path=sp2, graph_path=graph_copy)  # the next read rebuilds it
        fresh_again = boot_slice.is_fresh(slice_path=sp2, graph_path=graph_copy)
        print("2) Freshness is keyed on the graph's CONTENT, not the clock:")
        print(f"     just built ......... fresh? {fresh0}")
        print(f"     after a bare touch . fresh? {fresh_after_touch}   (a touch must NOT invalidate)")
        print(f"     after a byte change  stale? {stale_after_edit}    (a content change MUST invalidate)")
        print(f"     after the rebuild .. fresh? {fresh_again}\n")
        freshness_ok = fresh0 and fresh_after_touch and stale_after_edit and fresh_again

        # 3) NEVER BLOCKS: (a) a deleted cache self-heals; (b) an absent committed graph still orients (live walk).
        os.remove(slice_path)
        healed = boot_slice.read(slice_path=slice_path)          # rebuilds transparently on the next read
        self_heal_ok = (healed is not None and _render(healed) == live)
        absent_graph = os.path.join(work, "no-such-graph.json")
        sp3 = os.path.join(work, "live-slice.json")
        _path, source = boot_slice.build(slice_path=sp3, graph_path=absent_graph)
        live_shim = boot_slice.read(slice_path=sp3, graph_path=absent_graph)
        degrade_render = _render(live_shim)
        degrade_ok = (source == "live" and bool(degrade_render))
        print("3) The cache never blocks orientation:")
        print(f"     deleted cache -> rebuilds on next read, same block? {'YES' if self_heal_ok else 'NO'}")
        print(f"     committed graph absent -> still orients from a live walk? {'YES' if degrade_ok else 'NO'}\n")

        blob = "\n".join(cached + degrade_render)
        # §12: the rendered block names plain components + relationship VERBS, never raw ids or predicate nouns.
        jargon_free = not any(t in blob for t in ("module:", "tool:", "policy:", "check:", "schema:",
                                                  "provided_by", "governed_by", "depends_on", "targets"))
        print("Only an isolated temp cache + a temp copy of the graph were written; the build, the read-shim,")
        print("the bidirectional walk, and the render are the engine's real logic over the real committed")
        print("knowledge graph. The cache is a faithful reprojection, honest about its freshness, and never")
        print("blocks boot.")

        if not (faithful and freshness_ok and self_heal_ok and degrade_ok and jargon_free):
            print("\nDEMO UNEXPECTED: the boot slice did not behave as described (the cache should match the "
                  "live walk byte-for-byte, invalidate on a content change but not a touch, self-heal when "
                  "deleted, still orient from a live walk when the committed graph is absent, and render "
                  "jargon-free).", file=sys.stderr)
            return 1
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
