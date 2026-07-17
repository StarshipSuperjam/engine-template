#!/usr/bin/env python3
"""Demonstration: the engine's priority dials now actually govern what you see first (#394).

Run it:  uv run --directory .engine --frozen -- python tools/demo_attention_live_dials.py

WHAT THIS IS FOR. The engine keeps a short, reviewable list of dials that are supposed to decide what it shows
you at the start of a session — how bad a problem has to be before it stops you, how many open problems make a
session "busy", how strong a word-match has to be before it volunteers a hint. Those dials read as promises.
This shows they are now kept: change a dial, and what the engine surfaces changes. Before this change every one
of them was inert — the engine lumped every open problem into a single item pinned exactly at the bar, so the
bar was forever measuring itself, "busy" could never be reached, and a tuned word-match bar never reached the
part that uses it.

Everything below runs the REAL ranking, the REAL grading, and the REAL boot rendering. Only the outside world
is faked — GitHub, local git, and the saved-memory store — so nothing is read from or written to your project.

VARY IT YOURSELF: edit `_BAR` (the bar an open problem must clear to stop you) or `_BUSY_AT` (how many blocking
problems make a session busy) near the top and run it again. The point is that the outcomes move when you move
them — that is what "the dial governs" means.
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import attention          # noqa: E402  (the REAL assembler)
import attention_rank     # noqa: E402  (the REAL ranking core)
import boot               # noqa: E402  (the REAL render)
import scent              # noqa: E402  (the REAL per-prompt hint threshold)
import telemetry          # noqa: E402  (the REAL severity grading)
from unittest import mock  # noqa: E402

# --- the two dials this demo varies (edit these and re-run) -------------------------------------------------
_BAR = 2        # `debt_blocking_threshold`: how severe an open problem must be to STOP you
_BUSY_AT = 3    # `flex_high_debt_count`: how many blocking problems make the session "busy"

_CATEGORIES = attention_rank.CATEGORIES
_POLICY = {
    **{f"budget_{c}": v for c, v in zip(_CATEGORIES, [0.30, 0.25, 0.15, 0.15, 0.15])},
    **{f"precedence_{c}": i + 1 for i, c in enumerate(_CATEGORIES)},
    **{f"trim_{c}": i + 1 for i, c in enumerate(reversed(_CATEGORIES))},
    "weight_recency": 0.5, "weight_severity": 1.0, "weight_proximity": 0.5,
    "flex_high_debt_count": _BUSY_AT, "flex_orientation_delta": 0.10,
    "debt_blocking_threshold": _BAR, "scent_strong_match_threshold": 0.5,
}
_AS_OF = "2026-06-04T00:00:00Z"
# The saved marker deliberately LAGS the merges below, which is the ordinary situation: it is refreshed on
# its own schedule, so work lands after it. Age is measured back from a reference moment and floored at
# zero, so everything newer than a lagging marker used to score the same, tie, and fall through to the
# pull-request number — which is how "recently shipped" came out back to front. Part 3 uses the marker the
# live path resolves, not this raw one.
_CURSOR = "2026-04-01T00:00:00Z"
_STATE = {"standing_situation": {"phase": "x"},
          "integration_debt": {"open_count": 0, "as_of": _CURSOR}}


def _finding(number, severity=None):
    """One row of the live debt register, exactly as boot's open_findings projects it."""
    return {"number": number, "source_id": None, "severity": severity}


def _assemble(findings, *, policy=None, recall=(), shipped=()):
    """The REAL assembler over faked substrates — only the outside world is stubbed."""
    with mock.patch.object(attention.validate, "load_json", return_value=dict(_STATE)), \
            mock.patch.object(attention.knowledge_query, "find", return_value=[]), \
            mock.patch.object(attention.work_record, "read_in_flight", return_value=[]), \
            mock.patch.object(attention.work_record, "read_recent_decisions", return_value=list(shipped)):
        return attention.assemble_candidates(policy or _POLICY, live_findings=findings,
                                             memory_recall=list(recall), gh=object())


def _blocking(cands, policy=None):
    """The open problems that actually STOP work — the ones clearing the bar."""
    pol = policy or _POLICY
    return [c["id"] for c in cands if attention_rank.assign_partition(c, pol) == "blocking_debt"]


def _named(ids) -> str:
    """Candidate ids in the operator's words. `finding:11` / `shipped:42` / `memory:r1` are how the engine
    keys these internally; a demonstration for a non-engineer says what they ARE. Nothing is shown that only
    makes sense with the code open."""
    words = {"finding": "engine finding #", "shipped": "pull request #", "memory": "a decision from your notes"}
    out = []
    for i in ids:
        kind, _, slug = str(i).partition(":")
        out.append(words["memory"] if kind == "memory" else f"{words.get(kind, kind + ' ')}{slug}")
    return ", ".join(out) if out else "nothing"


def _plainly(condition: str) -> str:
    """The session's condition in the operator's words. The engine's own names for these ("clean", "high_debt")
    are backstage vocabulary; a demonstration for a non-engineer says what it MEANS."""
    return {"clean": "a normal session", "high_debt": "a busy session"}.get(condition, condition)


def _demo() -> int:
    ok = True
    print("=" * 88)
    print("PART 1 — how bad a problem must be before it stops you (the bar is now a real bar)")
    print("=" * 88)
    # Two open problems: one where a safety check could not run, one recurring-but-low-impact.
    cands, _, _ = _assemble([_finding(11, telemetry.TRUST_CRITICAL), _finding(12, telemetry.PERSISTENT_BENIGN)])
    stops = _blocking(cands)
    print(f"  Two problems are open: #11 (a safety check could not run) and #12 (recurring, low impact).")
    print(f"  With the bar at {_BAR}, what actually stops you: {_named(stops)}")
    print("  => the serious one stops you; the low-impact one waits its turn instead of crying wolf.")
    ok_1 = stops == ["finding:11"]
    ok = ok and ok_1

    print()
    print("  Now move ONLY the bar — same two problems:")
    lax = {**_POLICY, "debt_blocking_threshold": 1}
    lax_cands, _, _ = _assemble([_finding(12, telemetry.PERSISTENT_BENIGN)], policy=lax)
    strict_cands, _, _ = _assemble([_finding(12, telemetry.PERSISTENT_BENIGN)])
    print(f"    bar {_BAR} -> the low-impact problem stops you? {bool(_blocking(strict_cands))}")
    print(f"    bar 1 -> the low-impact problem stops you? {bool(_blocking(lax_cands, lax))}")
    print("  => the dial GOVERNS. Before this change, moving it changed nothing at all: every problem was")
    print("     lumped into one item pinned exactly AT the bar, so the bar only ever measured itself.")
    ok_2 = not _blocking(strict_cands) and _blocking(lax_cands, lax) == ["finding:12"]
    ok = ok and ok_2

    print()
    print("  And the one thing the dial must NEVER be able to switch off:")
    absurd = {**_POLICY, "debt_blocking_threshold": 10_000}
    hard, _, _ = _assemble([_finding(11, telemetry.TRUST_CRITICAL)], policy=absurd)
    print(f"    bar cranked to 10000 -> does '#11 a safety check could not run' still stop you? "
          f"{'yes' if _blocking(hard, absurd) else 'NO'}")
    print("  => yes. A check that could not run always stops you, however the bar is tuned — you can defer")
    print("     ordinary problems, never the engine telling you it could not check something.")
    ok_3 = _blocking(hard, absurd) == ["finding:11"]
    ok = ok and ok_3

    print()
    print("  A problem nobody has rated yet:")
    ungraded, _, _ = _assemble([_finding(13)])
    print(f"    does '#13, severity never established' stop you? {bool(_blocking(ungraded))}")
    print("  => no. The engine doesn't guess a severity nobody set — you still see it counted among your open")
    print("     problems, it just doesn't stop you. Claiming a rating it was never given is how the bar ended")
    print("     up compared against itself, which is what made it meaningless in the first place.")
    ok_2b = not _blocking(ungraded)
    ok = ok and ok_2b

    print()
    print("=" * 88)
    print("PART 2 — when the session counts as busy (the count is now a real count)")
    print("=" * 88)
    few, _, _ = _assemble([_finding(i, telemetry.TRUST_CRITICAL) for i in range(1, _BUSY_AT)])
    many, _, _ = _assemble([_finding(i, telemetry.TRUST_CRITICAL) for i in range(1, _BUSY_AT + 3)])
    c_few = attention_rank.session_condition(few, _POLICY)
    c_many = attention_rank.session_condition(many, _POLICY)
    print(f"  {_BUSY_AT - 1} problems that stop you -> the session reads: {_plainly(c_few)}")
    print(f"  {_BUSY_AT + 2} problems that stop you -> the session reads: {_plainly(c_many)}")
    print(f"  => 'busy' can now be reached, and when it is, the engine spends less of the space on background")
    print(f"     orientation and more on the problems. Before, every problem collapsed into ONE item, so the")
    print(f"     count was stuck at 1 and '{_BUSY_AT} or more' could never happen whatever the dial said.")
    ok_4 = c_few == "clean" and c_many == "high_debt"
    ok = ok and ok_4

    print()
    print("  A deep pile of LOW-IMPACT problems must not fake a busy session:")
    benign, _, _ = _assemble([_finding(i, telemetry.PERSISTENT_BENIGN) for i in range(1, 9)])
    c_benign = attention_rank.session_condition(benign, _POLICY)
    print(f"    8 low-impact problems open -> the session reads: {_plainly(c_benign)}")
    print("  => waiting work is not blocking work.")
    ok_5 = c_benign == "clean"
    ok = ok and ok_5

    print()
    print("=" * 88)
    print("PART 3 — 'decisions made recently' is a real kind now, from BOTH of its sources")
    print("=" * 88)
    shipped = [{"id": "shipped:42", "category": "recent_decisions", "recency": "2026-06-03T00:00:00Z",
                "title": "Add the sign-in page", "source": "git"},
               {"id": "shipped:17", "category": "recent_decisions", "recency": "2026-05-01T00:00:00Z",
                "title": "Set up the database", "source": "git"}]
    recall = [{"id": "r1", "text": "we decided to keep the onboarding copy short", "recency": "2026-06-02T00:00:00Z"}]
    cands, _, _ = _assemble([], recall=recall, shipped=shipped)
    moment = attention._reference_moment(cands, _CURSOR)   # what the live path resolves, from a lagging marker
    result = attention_rank.rank(cands, _POLICY, moment, {"state", "knowledge", "telemetry", "git"},
                                 budget_total=20)
    recent = next(e for e in result["partition"] if e["category"] == "recent_decisions")
    kinds = [m["id"].partition(":")[0] for m in recent["members"]]
    print(f"  What the engine treats as a recent decision: {_named(m['id'] for m in recent['members'])}")
    print("  => both halves the design names: a merged pull request AND a decision recorded in saved memory.")
    share = _POLICY["budget_recent_decisions"]
    print(f"  How many it shows is the share of the session's opening space you set aside for this — "
          f"{share:.0%} of it, which works out here as {recent['budget_size']} slot(s). Not a number buried "
          f"in the code.")
    ok_6 = "shipped" in kinds and "memory" in kinds
    ok = ok and ok_6

    print()
    print("  What a session actually reads (the real boot rendering of the above):")
    for line in boot._shipped_lines(result, read=lambda: shipped):
        print(f"    recently shipped: {line}")
    for line in boot.render_recalled_decisions(boot._recalled_entries(result, recall))[:3]:
        print(f"    {line}")
    # The defect that started this: the newest merge must lead. It rendered OLDEST-first, because age was
    # measured back from a marker that lags behind the merges themselves — so everything newer than it tied,
    # and the tie fell through to the pull-request number. #42 is newer than #17 and must come first.
    lines = boot._shipped_lines(result, read=lambda: shipped)
    print("  (newest first — that is the fix: this list used to come out back to front)")
    ok_8 = [l.split()[0] for l in lines] == ["#42", "#17"]
    ok = ok and ok_8

    print()
    print("=" * 88)
    print("PART 4 — the bar for volunteering a hint mid-conversation (the third dial)")
    print("=" * 88)
    # The scent reads its bar from the SAME reviewable policy. It used to read only the shipped default, so
    # tuning it saved a number nothing ever read. Only the override FILE is faked here; the read is real.
    with mock.patch.object(scent.operator_overrides, "slice_for", return_value={}):
        shipped_bar = scent._threshold()
    with mock.patch.object(scent.operator_overrides, "slice_for",
                           return_value={"scent_strong_match_threshold": 0.93}):
        tuned_bar = scent._threshold()
    print(f"  the shipped bar for volunteering a hint: {shipped_bar}")
    print(f"  after you tune it to 0.93, what the hint actually uses: {tuned_bar}")
    print("  => it takes effect. Before this change it still used the shipped number, so tuning this one")
    print("     changed nothing at all and nothing told you so.")
    ok_9 = shipped_bar != tuned_bar and tuned_bar == 0.93
    ok = ok and ok_9

    print()
    print("-" * 88)
    print("Nothing here touched your project: no GitHub call, no git read, no saved-memory read, nothing")
    print("written. Vary `_BAR` or `_BUSY_AT` at the top of this file and run it again — the outcomes move")
    print("with them. That is the whole point: a dial the engine shows you is a dial that governs.")

    if not ok:
        print("\nDEMO UNEXPECTED: a dial did not govern as claimed "
              f"(bar={ok_1 and ok_2}, ungraded-defers={ok_2b}, never-tuned-out={ok_3}, busy={ok_4 and ok_5}, "
              f"recent-decisions={ok_6 and ok_8}, hint-bar={ok_9}).", file=sys.stderr)
        return 1
    return 0


def demo() -> int:
    return _demo()


if __name__ == "__main__":
    sys.exit(demo())
