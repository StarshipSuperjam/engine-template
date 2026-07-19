#!/usr/bin/env python3
"""Operator-runnable demo of boot's reversible-forgetting readout — what memory has set aside from recall.

It answers, in plain words, a question a non-engineer can't read code to verify: *the engine quietly stops
searching notes that have gone unused, and folds old ones into summaries — does it TELL me when it does, never
delete anything, and actually bring a note back when I ask?*

It runs the REAL logic end-to-end — memory's own `forget.set_aside` / `restore_to_recall` / `recorded_text`,
and boot's own `render_set_aside` + `_relay_lines` collapse — in an ISOLATED temp store and temp boot cache
(via env overrides), so it never touches your real memory and needs no network, no token, no edits. Only the
boundary is faked: the other status signals a live boot would have read alongside the set-aside report.

It shows, and CHECKS (so this demo can FAIL — it is a falsification, not a showcase):
  * QUIET WHEN TIDY — three fresh notes set nothing aside; the readout renders nothing (why it is invisible on
    a young project);
  * SET ASIDE, NOT LOST — an old, unused note is set aside; the readout names it, says nothing was deleted,
    offers to bring it back, and shows no internal id;
  * ANTI-HABITUATION — seen again unchanged, it collapses to one terse line that STILL offers the handles;
  * WHAT CHANGED — a second note going aside relays full again, naming how many are new since last seen;
  * A REAL UNDO — bringing a demoted note back makes it searchable again (proven by re-reading recall);
  * PER-CLASS HONESTY — a note folded into a summary is offered "show the original wording", NEVER "bring it
    back" (there is no un-fold), and its original words are still recoverable;
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


def _fresh_note(text, *, age_days, session):
    """Append one batchless episodic (never a crash orphan — only demoted by age) at a chosen age."""
    rec = consolidate._make_episodic(session, {"role": "decision", "text": text}, "b")
    rec.pop(records.BATCH_KEY, None)
    rec["ts"] = int(time.time()) - age_days * _DAY
    ledger.append(rec)
    return rec[records.RECORD_ID_KEY]


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
            _fresh_note(f"a fresh decision {i}", age_days=0, session=f"F{i}")
        block = boot.render_set_aside(forget.set_aside())
        print(f"  readout: {block!r}\n")
        if block != []:
            failures.append("a young store with only fresh notes must render NO set-aside block")

        print("=== Set aside, not lost — an old unused note ===")
        demoted = _fresh_note("the sourdough starter is fed daily at 8am", age_days=40, session="D1")
        first = _relayed_readout(forget.set_aside())
        text = "\n".join(first)
        print(text + "\n")
        low = text.lower()
        if "nothing was deleted" not in low:
            failures.append("the readout must say nothing was deleted")
        if "bring it back into search" not in low:
            failures.append("a demoted note must offer the bring-back handle")
        if demoted in text:
            failures.append("the internal record id must never reach the operator readout")
        if "forgot" in low or "deleted the" in low:
            failures.append("the readout must never claim a note was forgotten/deleted")

        print("=== Anti-habituation — seen again unchanged, it collapses ===")
        second = _relayed_readout(forget.set_aside())
        text2 = "\n".join(second).lower()
        print("\n".join(second) + "\n")
        if "unchanged since last session" not in text2:
            failures.append("an unchanged readout must collapse to the terse 'unchanged' line")
        if "bring one back" not in text2:             # the terse form STILL carries the offer
            failures.append("the collapsed readout must still carry the bring-back offer")

        print("=== What changed — a second note goes aside ===")
        _fresh_note("early idea: try a rye levain", age_days=40, session="D2")
        third = _relayed_readout(forget.set_aside())
        text3 = "\n".join(third).lower()
        print("\n".join(third) + "\n")
        if "since you last saw this" not in text3:
            failures.append("a newly set-aside note must relay full and name what changed since last seen")

        print("=== A real undo — bring the demoted note back ===")
        was_out = not _in_recall(demoted)
        brought_back = forget.restore_to_recall(demoted)
        now_in = _in_recall(demoted)
        print(f"  was out of recall: {was_out}; restore returned: {brought_back}; back in recall: {now_in}\n")
        if not (was_out and brought_back and now_in):
            failures.append("a demoted note must be out of recall, then brought back into it by restore")

        print("=== Per-class honesty — a note folded into a summary ===")
        raw = _fresh_note("raw note: the oven runs 15C hot on the fan setting", age_days=25, session="R1")
        rollup.store_gist("R1", [{"role": "lesson", "text": "kitchen quirks summary",
                                  records.SOURCE_IDS_KEY: [raw]}])
        summ = _relayed_readout(forget.set_aside())
        stext = "\n".join(summ).lower()
        print("\n".join(summ) + "\n")
        cannot_undo = forget.restore_to_recall(raw)                     # must be False — no un-fold exists
        original = forget.recorded_text(raw)                            # but the wording is still recoverable
        print(f"  restore of a folded note returned: {cannot_undo}")
        print(f"  its original wording is still readable: {(original or {}).get('text')!r}\n")
        if "exact wording" not in stext:
            failures.append("a summarised note must offer to show its original wording")
        if cannot_undo:
            failures.append("a summarised (folded) note must NOT be restorable — there is no un-fold")
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
    print("All checks passed: quiet when tidy, set-aside-not-lost with a real undo, anti-habituation collapse, "
          "per-class honesty (show vs bring-back), and nothing ever erased.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
