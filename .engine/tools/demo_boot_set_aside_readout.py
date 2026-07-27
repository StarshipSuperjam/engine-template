#!/usr/bin/env python3
"""Operator-runnable demo of boot's set-aside readout — what memory has set aside from recall.

It answers, in plain words, a question a non-engineer can't read code to verify: *the engine folds old notes
into summaries — does it TELL me when it does, never delete anything, and can I still get the original words
back? And does it quietly stop searching a note just because I haven't used it in a while?*

That last question used to have the opposite answer. A note nobody had come back to was set aside from search
after about a month, and asking for it brought it back. That age-out is gone for every kind of note, so the
engine no longer sets anything aside by time. What this demo shows is a summary being written over a note, which is
never reversible, so the readout must never pretend otherwise.

It runs the REAL logic end-to-end — memory's own `forget.set_aside` / `recorded_text`, and boot's own
`render_set_aside` + `_relay_lines` collapse — in an ISOLATED temp store and temp boot cache (via env
overrides), so it never touches your real memory and needs no network, no token, no edits. Only the boundary is
faked: the other status signals a live boot would have read alongside the set-aside report.

It shows, and CHECKS (so this demo can FAIL — it is a falsification, not a showcase):
  * QUIET WHEN TIDY — three fresh notes set nothing aside; the readout renders nothing (why it is invisible on
    a young project);
  * AGE ALONE SETS NOTHING ASIDE — a note untouched for months is still searchable, and still not in the
    readout;
  * SET ASIDE, NOT LOST — a note folded into a summary is set aside; the readout names it, says nothing was
    deleted, offers its original wording, and shows no internal id;
  * ANTI-HABITUATION — seen again unchanged, it collapses to one terse line that STILL offers the handle;
  * WHAT CHANGED — a second note going aside relays full again, naming how many are new since last seen;
  * THE HONEST HANDLE — a folded note is out of search and stays out (there is no un-fold), yet its original
    words are still recoverable exactly as they were written;
  * NOTHING ERASED — the store's record count only ever grows across the whole run.

Vary it yourself: change the ages or counts below and re-run.

Run: uv run --directory .engine -- python tools/demo_boot_set_aside_readout.py
"""
from __future__ import annotations
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import boot                # noqa: E402
import boot_alarm_ledger   # noqa: E402
from memory import consolidate, forget, ledger, records, rollup  # noqa: E402

_DAY = 86400
_ANCIENT_DAYS = 400        # far past every threshold the retired age-out ever used


def _note(text, *, age_days, session):
    """Append one batchless episodic (never a crash orphan) at a chosen age."""
    rec = consolidate._make_episodic(session, {"role": "decision", "text": text}, "b")
    rec.pop(records.BATCH_KEY, None)
    rec["ts"] = int(time.time()) - age_days * _DAY
    ledger.append(rec)
    return rec[records.RECORD_ID_KEY]


def _fold_into_summary(session, raw_id, summary_text):
    """Write a real roll-up summary over one note — the irreversible way a note leaves search.

    Not the only way: the operator can also WITHHOLD a note or a whole conversation, which the same readout
    reports and which `forget.restore` reverses. This demo covers the fold because that is the class with no
    undo, and so the one whose readout must never overstate what it can offer."""
    rollup.store_gist(session, [{"role": "lesson", "text": summary_text,
                                 records.SOURCE_IDS_KEY: [raw_id]}])


def _shown(block):
    """What the operator would actually see — the rendered block, or a plain sentence when there is none. An
    empty Python list on screen is not evidence a non-engineer can read."""
    return "\n" + "\n".join(block) if block else "nothing — no block at all"


def _record_count():
    return sum(1 for _ in ledger.iter_records())


def _in_recall(rid):
    return rid in {r.get(records.RECORD_ID_KEY) for r in forget.live_records()}


def _signals_with(report):
    """A complete, valid signals dict — the boundary we fake — carrying the REAL set-aside report under test."""
    return {"state": {"schema_version": 1}, "refused": False, "gate": "on", "reason": None,
            "finding_count": 0, "unrated_count": 0, "register": "",
            "debt_count": 0, "debt_as_of": None, "att_lines": [], "att_degraded": [], "shipped": [],
            "stance": "Exploring", "strand": None, "behind_origin": None, "off_main": None,
            "pr_conflict": None, "restore_offer": None, "migration_revert": None, "audit_stale": None,
            "live_standing": None, "neighborhood": None, "map_rebuilt": False, "map_corrupt": False,
            "ledger_malformed": None, "migration_stalled": False, "recall_offline": False,
            "set_aside": report}


def _relayed_readout(report):
    """Drive boot's REAL collapse pass (`_relay_lines` over `decide`) so the readout's collapsed/newly flags
    are stamped the way a live SessionStart would, then render it — returns the rendered lines."""
    s = _signals_with(report)
    boot._relay_lines(s)                       # stamps s["set_aside"]["collapsed"] / ["newly"] via decide()
    return boot.render_set_aside(s["set_aside"])


def main() -> int:
    failures: list[str] = []
    store = tempfile.mkdtemp()
    cache = tempfile.mkdtemp()
    os.environ[ledger.ENV_DIR] = store
    os.environ[boot_alarm_ledger.ENV_DIR] = cache
    try:
        start_count = _record_count()

        print("=== Quiet when tidy — three fresh notes set nothing aside ===")
        for i in range(3):
            _note(f"a fresh decision {i}", age_days=0, session=f"F{i}")
        block = boot.render_set_aside(forget.set_aside())
        print(f"  what the session start would show: {_shown(block)}\n")
        if block != []:
            failures.append("a young store with only fresh notes must render NO set-aside block")

        print(f"=== Age alone sets nothing aside — a note untouched for {_ANCIENT_DAYS} days ===")
        ancient = _note("the sourdough starter is fed daily at 8am", age_days=_ANCIENT_DAYS, session="A1")
        still_searchable = _in_recall(ancient)
        still_quiet = boot.render_set_aside(forget.set_aside())
        print(f"  a note nobody has touched in over a year is still searchable: {still_searchable}")
        print(f"  what the session start would show: {_shown(still_quiet)}\n")
        if not still_searchable:
            failures.append("a note must never leave search just because time passed")
        if still_quiet != []:
            failures.append("an old note is not set aside, so it must not appear in the readout")

        print("=== Set aside, not lost — a note folded into a summary ===")
        folded = _note("raw note: the oven runs 15C hot on the fan setting", age_days=25, session="R1")
        _fold_into_summary("R1", folded, "kitchen quirks summary")
        first = _relayed_readout(forget.set_aside())
        text = "\n".join(first)
        print(text + "\n")
        low = text.lower()
        if "nothing was deleted" not in low:
            failures.append("the readout must say nothing was deleted")
        if "exact wording" not in low:
            failures.append("a folded note must offer the show-the-original-wording handle")
        if "bring" in low and "back" in low:
            failures.append("the readout must never offer to bring a folded note back — there is no un-fold")
        if folded in text:
            failures.append("the internal record id must never reach the operator readout")
        if "forgot" in low or "deleted the" in low:
            failures.append("the readout must never claim a note was forgotten/deleted")

        print("=== Anti-habituation — seen again unchanged, it collapses ===")
        second = _relayed_readout(forget.set_aside())
        text2 = "\n".join(second).lower()
        print("\n".join(second) + "\n")
        if "unchanged since last session" not in text2:
            failures.append("an unchanged readout must collapse to the terse 'unchanged' line")
        if "original wording" not in text2:          # the terse form STILL carries the offer
            failures.append("the collapsed readout must still carry the show-the-wording offer")

        print("=== What changed — a second note goes aside ===")
        second_folded = _note("early idea: try a rye levain", age_days=25, session="R2")
        _fold_into_summary("R2", second_folded, "bread experiments summary")
        third = _relayed_readout(forget.set_aside())
        text3 = "\n".join(third).lower()
        print("\n".join(third) + "\n")
        if "since you last saw this" not in text3:
            failures.append("a newly set-aside note must relay full and name what changed since last seen")

        print("=== The honest handle — out of search, but not lost ===")
        out_of_recall = not _in_recall(folded)
        original = forget.recorded_text(folded)      # the wording is still recoverable, word for word
        print(f"  the folded note is out of search: {out_of_recall}")
        print(f"  its original wording is still readable: {(original or {}).get('text')!r}\n")
        if not out_of_recall:
            failures.append("a folded note must be out of recall — the summary is what search returns now")
        if not original or "oven runs 15C hot" not in original.get("text", ""):
            failures.append("a folded note's original wording must still be recoverable word-for-word")

        print("=== Nothing erased — the store only grew ===")
        end_count = _record_count()
        print(f"  records at start: {start_count}; at end: {end_count}\n")
        if end_count < start_count:
            failures.append("the append-only store must never shrink — nothing is ever erased here")
    finally:
        os.environ.pop(ledger.ENV_DIR, None)
        os.environ.pop(boot_alarm_ledger.ENV_DIR, None)

    if failures:
        print("DEMO FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All checks passed: quiet when tidy, age alone sets nothing aside, set-aside-not-lost with the "
          "original wording still readable, anti-habituation collapse, and nothing ever erased.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
