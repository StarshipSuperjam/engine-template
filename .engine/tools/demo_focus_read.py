#!/usr/bin/env python3
"""Operator-runnable demo of the orientation-time focused knowledge read (engine-template #37, PR 2).

It answers, in plain words, a question a non-engineer can't read code to verify: *when I cold-boot mid-task,
does the engine actually notice which parts of the project I'm working on, and read their knowledge
neighborhood for me?* It runs the REAL logic end-to-end — `attention.derive_focus` (changed files -> owning
graph entities), the REAL ranking walk over the REAL committed knowledge graph, and the REAL boot rendering
of the AI-facing neighborhood block. ONLY the local git "what files changed" read is faked, so the demo is
deterministic and needs no network, no token, and no edits to your working tree.

Vary it yourself: pass paths to pretend you're editing, e.g.
    uv run --directory .engine -- python tools/demo_focus_read.py .engine/tools/boot.py .engine/policies/attention.md
With no args it uses a built-in two-file scenario plus the clean-branch (no work in hand) case.

Run: uv run --directory .engine -- python tools/demo_focus_read.py
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import attention   # noqa: E402
import boot        # noqa: E402


def _fake_changed(paths):
    """A fake git runner answering only what changed_paths asks: a real non-default branch with `paths` as
    its committed diff. Everything else returns None, so only the injected changed-file list is faked."""
    def run(args):
        if args[:1] == ["symbolic-ref"]:
            return "refs/remotes/origin/main"
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return "claude/your-feature"           # a real branch other than the default -> committed leg runs
        if args[:1] == ["log"]:
            return "2026-06-19T10:00:00Z"
        if args[:2] == ["diff", "--name-only"]:
            spec = args[2] if len(args) > 2 else None
            if spec and "..." in spec:              # the committed-vs-default leg
                return "\n".join(paths)
            return None                             # nothing uncommitted / staged in the demo
        return None
    return run


def _neighborhood(changed):
    """The REAL focused read for a faked changed-file set: derive the focus from the files (over the real
    graph), rank live (the real neighbor walk; offline, gh=None), and assemble the neighborhood the way boot
    does. Returns (focus_ids, neighborhood_dict_or_None)."""
    focus = attention.derive_focus(run=_fake_changed(changed))      # real path -> entity mapping
    result = attention.rank_live(focus=focus or None)               # real walk over the real graph, offline
    adjacent: list = []
    for entry in result.get("partition", []):
        if entry.get("category") == "structural_neighbors":
            for m in entry.get("members", []):
                slug = m.get("id", "").split(":", 1)[-1]
                if slug and slug not in adjacent:
                    adjacent.append(slug)
    nb = {"focus": [f.split(":", 1)[-1] for f in focus], "adjacent": adjacent} if focus else None
    return focus, nb


def _show(title, changed):
    print(title)
    print(f"   pretending you've touched: {', '.join(changed) if changed else '(nothing — a clean branch)'}")
    focus, nb = _neighborhood(changed)
    block = boot.render_neighborhood(nb)
    if block:
        for line in block:
            if line:
                print(f"   {line}")
    else:
        print("   (no work in hand -> no focused read, no neighborhood block — boot degrades cleanly)")
    print()
    return focus, nb


def main(argv: list | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    print("Focused knowledge read at orientation — what the engine reads about the work in hand (#37).\n")

    scenario = argv or [".engine/tools/attention.py", ".engine/tools/boot.py"]
    focus, nb = _show("1) Mid-task: the files you're editing map to their components, and their knowledge "
                      "neighborhood is read:", scenario)

    _show("2) On a clean / default branch: no work in hand, so no focused read fires:", [])

    print("Only the 'which files changed' git read was faked; the entity mapping, the neighbor walk, and the")
    print("rendering are the engine's real logic over the real committed knowledge graph. No network, no token,")
    print("nothing written. The block is AI-orientation context (it names plain components, never raw ids); it")
    print("is separate from the work-priority ranking and does not change that ranking's degraded notice.")

    # Self-check: scenario 1 produced a focus (the mapped files are real surfaces) and a jargon-free block.
    rendered = "\n".join(boot.render_neighborhood(nb))
    ok = bool(focus) and "tool:" not in rendered and "module:" not in rendered
    if not ok:
        print("\nDEMO UNEXPECTED: the focused read did not produce a clean neighborhood block for the "
              "built-in scenario.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
