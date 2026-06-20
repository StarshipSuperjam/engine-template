#!/usr/bin/env python3
"""Operator-runnable demo of the BIDIRECTIONAL orientation walk (engine-template #37 / D-224).

It answers, in plain words, a question a non-engineer can't read code to verify: *when the engine reads the
knowledge neighborhood of the work in hand, does it now see the connective tissue around an edited file —
the rules that govern it, the checks that target it — not just the module it lives in?*

Before this change (PR 2) the walk was forward-only, so an edited file usually collapsed to ONLY its module.
D-224 makes the walk bidirectional: it also follows edges that point AT the focus. This demo runs the REAL
logic end-to-end over the REAL committed knowledge graph — `attention.derive_focus` (changed files -> owning
entities), the REAL bidirectional ranking walk, and boot's REAL capped neighborhood render. ONLY the local
git "what files changed" read is faked, so it is deterministic and needs no network, no token, no edits.

It shows three honest cases side by side (forward-only vs bidirectional), each capped exactly the way boot
caps the operator-facing view:
  * a POLICY file  -> the bidirectional read GAINS the checks that govern it (the real win);
  * a bare TOOL    -> stays module-only either way (the honest residual D-224 names: a leaf with no inbound
                      structural edge has no reverse tissue to surface);
  * a MODULE manifest -> a highly-connected focus; the view stays BOUNDED (capped), it never floods.

Vary it yourself: pass a path to pretend you're editing, e.g.
    uv run --directory .engine -- python tools/demo_reverse_adjacency.py .engine/policies/memory.md

Run: uv run --directory .engine -- python tools/demo_reverse_adjacency.py
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import attention          # noqa: E402
import boot               # noqa: E402
import knowledge_query    # noqa: E402

CAP = boot.NEEDS_ATTENTION_CAP   # the same bound boot puts on the operator-facing neighborhood


def _fake_changed(paths):
    """A fake git runner answering only what changed_paths asks: a real non-default branch whose committed
    diff is `paths`. Everything else returns None, so ONLY the injected changed-file list is faked."""
    def run(args):
        if args[:1] == ["symbolic-ref"]:
            return "refs/remotes/origin/main"
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return "claude/your-feature"            # a real branch other than the default
        if args[:1] == ["log"]:
            return "2026-06-19T10:00:00Z"
        if args[:2] == ["diff", "--name-only"]:
            spec = args[2] if len(args) > 2 else None
            if spec and "..." in spec:
                return "\n".join(paths)
            return None
        return None
    return run


def _slug(eid: str) -> str:
    return eid.split(":", 1)[-1]


def _forward_only(focus_ids: list) -> list:
    """The PR-2 (forward-only) neighbourhood: walk each focus member with direction='out', dedupe, exclude
    focus members, slugify, cap exactly as boot would. The 'before' baseline."""
    focus_set, seen, adj = set(focus_ids), set(), []
    for fid in focus_ids:
        for n in knowledge_query.neighbors(fid, direction="out"):    # the OLD default
            nid = n["id"]
            if nid in focus_set or nid in seen:
                continue
            seen.add(nid)
            adj.append(_slug(nid))
    return adj[:CAP]


def _bidirectional(focus_ids: list) -> list:
    """The D-224 (bidirectional) neighbourhood, via the REAL rank_live walk, capped the way boot caps it."""
    result = attention.rank_live(focus=focus_ids or None)            # real walk; offline, gh=None
    adj: list = []
    for entry in result.get("partition", []):
        if entry.get("category") == "structural_neighbors":
            for m in entry.get("members", [])[:CAP]:                 # boot's NEEDS_ATTENTION_CAP bound
                s = _slug(m.get("id", ""))
                if s and s not in adj:
                    adj.append(s)
    return adj


def _show(title: str, changed: list):
    print(title)
    print(f"   pretending you've touched: {', '.join(changed)}")
    focus = attention.derive_focus(run=_fake_changed(changed))       # real path -> entity mapping
    if not focus:
        print("   (these files own no graph surface -> no focused read)\n")
        return [], [], []
    before = _forward_only(focus)
    after = _bidirectional(focus)
    nb = {"focus": [_slug(f) for f in focus], "adjacent": after}
    print(f"   the work maps to: {', '.join(_slug(f) for f in focus)}")
    print(f"   forward-only (before): {', '.join(before) or '(only its module / nothing)'}")
    print(f"   bidirectional (after): {', '.join(after) or '(only its module / nothing)'}")
    gained = [a for a in after if a not in before]
    if gained:
        print(f"   NEW reverse tissue surfaced: {', '.join(gained)}")
    print("   what the model now sees in its briefing:")
    for line in boot.render_neighborhood(nb):
        if line:
            print(f"     {line}")
    print()
    return focus, before, after


def main(argv: list | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    print("Bidirectional orientation walk — the reverse connective tissue of the work in hand (#37 / D-224).\n")

    if argv:
        focus, before, after = _show("Your scenario:", argv)
        ok = bool(focus)
        scenario_after = after
    else:
        # Scenario 1 leads with a CONNECTIVE focus so the win is visible on a no-args run.
        f1, b1, a1 = _show("1) Editing a POLICY -> the bidirectional read gains the checks that govern it:",
                           [".engine/policies/attention.md"])
        f2, b2, a2 = _show("2) Editing a bare TOOL -> honestly stays module-only (no inbound edges to reverse):",
                           [".engine/tools/attention.py"])
        f3, b3, a3 = _show("3) Editing a MODULE manifest -> highly connected, yet the view stays BOUNDED:",
                           [".engine/modules/core/manifest.json"])
        # Self-checks: the policy GAINS tissue forward-only couldn't see; the tool does NOT; the hub is bounded.
        ok = (bool(f1) and len(a1) > len(b1)
              and bool(f2) and a2 == b2
              and bool(f3) and len(a3) <= CAP)
        scenario_after = a1

    rendered = "\n".join(boot.render_neighborhood({"focus": ["x"], "adjacent": scenario_after}))
    jargon_free = "tool:" not in rendered and "module:" not in rendered and "governed_by" not in rendered

    print("Only the 'which files changed' git read was faked; the entity mapping, the bidirectional neighbor")
    print("walk, and the render are the engine's real logic over the real committed knowledge graph. The block")
    print("names plain components (never raw ids), is orientation context (not an alarm), and stays bounded.")

    if not (ok and jargon_free):
        print("\nDEMO UNEXPECTED: the bidirectional read did not behave as described for the built-in "
              "scenarios (policy should gain tissue, a bare tool should not, the hub should stay bounded, "
              "and the block must be jargon-free).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
