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

It shows four honest cases, each rendered through boot's REAL per-source block:
  * a POLICY file  -> the bidirectional read GAINS the checks that target (validate) it (the real win);
  * a bare TOOL    -> stays module-only either way (the honest residual D-224 names: a leaf with no inbound
                      structural edge has no reverse tissue to surface);
  * a MODULE manifest -> a highly-connected hub; the render DISCLOSES the true count ("provides N, showing 4
                      examples"), so an arbitrary sample never masquerades as the whole or the salient set;
  * SEVEN files at once -> the FOCUS itself is capped at five, and the header DISCLOSES the true count too
                      ("showing 5 of 7 you've changed"), so the shown focus never masquerades as the whole (#165).

Vary it yourself: pass a path to pretend you're editing, e.g.
    uv run --directory .engine -- python tools/demo_reverse_adjacency.py .engine/policies/escalation.md
(Pass a path the project doesn't track — say a top-level README — and it calmly reports "no focused read";
that is the engine degrading correctly, not an error.)

Run: uv run --directory .engine -- python tools/demo_reverse_adjacency.py
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import attention          # noqa: E402
import boot               # noqa: E402
import knowledge_query    # noqa: E402


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
    focus members, slugify. The 'before' baseline — what a leaf collapsed to before D-224."""
    focus_set, seen, adj = set(focus_ids), set(), []
    for fid in focus_ids:
        for n in knowledge_query.neighbors(fid, direction="out"):    # the OLD default
            nid = n["id"]
            if nid in focus_set or nid in seen:
                continue
            seen.add(nid)
            adj.append(_slug(nid))
    return adj


def _show(title: str, changed: list):
    """Run the REAL focused read for `changed` and print before/after. Returns (focus, summary, rendered) so
    the caller's self-checks read the real STRUCTURE, not the printout."""
    print(title)
    print(f"   pretending you've touched: {', '.join(changed)}")
    focus, focus_total = attention.derive_focus(run=_fake_changed(changed), with_total=True)  # real mapping
    if not focus:
        print("   (these files own no graph surface -> no focused read)\n")
        return None, None, ""
    before = _forward_only(focus)
    summary = attention.neighborhood_of(focus)                       # the REAL bidirectional summary
    if summary:
        summary["focus_total"] = focus_total                         # mirror boot: the true count behind the cap
    rendered = boot.render_neighborhood(summary)                     # boot's REAL honest render
    print(f"   the work maps to: {', '.join(_slug(f) for f in focus)}")
    print(f"   forward-only (before): {', '.join(before) or '(only its module / nothing)'}")
    print("   what the model now sees in its briefing (bidirectional; any truncation disclosed by count):")
    for line in rendered:
        if line:
            print(f"     {line}")
    print()
    return focus, summary, "\n".join(rendered)


def _has_reverse(summary) -> bool:
    """True if the focus gained connective tissue a forward-only walk could not see (a `direction:in` edge)."""
    return bool(summary) and any(g["direction"] == "in" for g in summary.get("groups", []))


def _has_disclosed_truncation(summary, rendered: str) -> bool:
    """True if a relationship floods past the sample AND the render DISCLOSES the true count (not a bare few)."""
    flooded = bool(summary) and any(g["total"] > len(g["sample"]) for g in summary.get("groups", []))
    return flooded and "(showing " in rendered


def _has_disclosed_focus_truncation(summary, rendered: str) -> bool:
    """True if the FOCUS itself was capped (more changed than shown) AND the header discloses the true count (#165)."""
    capped = bool(summary) and summary.get("focus_total", 0) > len(summary.get("focus", []))
    return capped and "you've changed)" in rendered


def main(argv: list | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    print("Bidirectional orientation walk — the reverse connective tissue of the work in hand (#37 / D-224).\n")

    if argv:
        focus, _summary, rendered = _show("Your scenario:", argv)
        # A custom path that owns no graph surface degrading to "no focused read" is CORRECT behavior, not a
        # failure — so the only thing to assert for an arbitrary path is that what DID render carries no jargon.
        ok = True
        all_rendered = [rendered]
    else:
        # 1) a CONNECTIVE focus (a policy) -> the bidirectional read GAINS the checks that target it.
        f1, s1, r1 = _show("1) Editing a POLICY -> the bidirectional read gains the checks that target it:",
                           [".engine/policies/attention.md"])
        # 2) a bare TOOL -> honestly stays module-only (no inbound structural edge to reverse).
        f2, s2, r2 = _show("2) Editing a bare TOOL -> honestly stays module-only (no inbound edges to reverse):",
                           [".engine/tools/attention.py"])
        # 3) a MODULE manifest -> a hub; the render must DISCLOSE the true count ("provides N, showing 4"),
        #    never an arbitrary capped few passed off as the whole (the maintainer's honesty correction).
        f3, s3, r3 = _show("3) Editing a MODULE manifest -> a hub; the render DISCLOSES the true count:",
                           [".engine/modules/core/manifest.json"])
        # 4) MORE than FOCUS_CAP files at once -> the FOCUS itself is capped; the header DISCLOSES the true count
        #    too (#165), so the shown focus is never passed off as the whole change.
        many = [f".engine/tools/{t}.py" for t in
                ("attention", "boot", "close", "modes", "hooks", "telemetry", "validate")]
        f4, s4, r4 = _show("4) Editing 7 files at once -> the focus is capped at 5, and the header says so:", many)
        ok = (_has_reverse(s1)                            # the policy gained reverse tissue forward-only can't see
              and bool(f2) and not _has_reverse(s2)       # the bare tool honestly stays module-only
              and _has_disclosed_truncation(s3, r3)       # the hub floods AND the render discloses the true count
              and _has_disclosed_focus_truncation(s4, r4))  # the over-cap focus discloses its true count too
        all_rendered = [r1, r2, r3, r4]

    blob = "\n".join(all_rendered)
    # §12: the AI block names plain components + relationship VERBS, never raw ids or internal type/predicate
    # vocabulary. The tokens below are exactly what a leak would look like.
    jargon_free = not any(t in blob for t in ("tool:", "module:", "policy:", "check:", "schema:",
                                              "provided_by", "governed_by", "depends_on", "targets"))

    print("Only the 'which files changed' git read was faked; the entity mapping, the bidirectional neighbour")
    print("walk, and the render are the engine's real logic over the real committed knowledge graph. The block")
    print("names plain components (never raw ids), is orientation context (not an alarm), and discloses any")
    print("truncation by its true count.")

    if not (ok and jargon_free):
        print("\nDEMO UNEXPECTED: the focused read did not behave as described for the built-in scenarios "
              "(the policy should gain reverse tissue, a bare tool should not, the hub should DISCLOSE its "
              "true count, an over-cap focus should disclose ITS true count, and the block must be "
              "jargon-free).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
