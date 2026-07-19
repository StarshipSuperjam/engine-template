#!/usr/bin/env python3
"""Operator-runnable demo of boot's anti-habituation collapse — the standing-alarm presentation ledger.

It answers, in plain words, a question a non-engineer can't read code to verify: *when the same problem is
still here next time I open a session, does the engine stop re-reading me the whole paragraph — while still
telling me a NEW or WORSE problem in full, and never going silent when something is actually wrong?*

It runs the REAL logic end-to-end — boot's own `_relay_lines` over `boot_alarm_ledger.decide` — in an
ISOLATED temp ledger (via the env override), so it never touches your real `.engine/boot/.cache/` and needs
no network, no token, no edits. Only the boundary is faked: the signal values (gate / findings) a live boot
would have read. Each scenario is CHECKED, so this demo can FAIL if the behaviour is wrong (it is a
falsification, not a showcase).

It shows four honest things:
  * COLLAPSE — the same standing problem, seen twice, relays full the first time and a terse "still …"
    reminder the second time — and the reminder STILL names the risk and STILL offers the fix;
  * ESCALATION — when the problem gets WORSE, it relays in full again (never a quiet reminder);
  * GROUNDING SURVIVES — the one-line `Project status` marker, and the all-clear, render every session and
    NEVER collapse (your at-a-glance proof the engine grounded);
  * FAIL-TOWARD-FULL — corrupt the ledger and the next session relays in FULL (repetition is the tolerable
    failure; a hidden alarm is not).

Vary it yourself: change the numbers below and re-run; or delete/scramble the temp ledger it prints and watch
the next read relay in full.

Run: uv run --directory .engine -- python tools/demo_boot_alarm_collapse.py
"""
from __future__ import annotations
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import boot               # noqa: E402
import boot_alarm_ledger  # noqa: E402

# A complete, valid signals dict (the boundary we fake — what a live boot would have READ). The collapse
# logic under test is real; only these inputs are synthetic.
_BASE = {"state": {"schema_version": 1}, "refused": False, "gate": "on", "reason": None,
         "finding_count": 0, "register": "", "total_open": None, "counts_state": "offline",
         "all_open_register": None, "blocking_findings": [], "blocking_finding_fingerprint": None,
         "debt_count": 0, "debt_as_of": None, "att_lines": [],
         "att_degraded": [], "shipped": [], "stance": "Exploring", "strand": None, "pr_conflict": None,
         "restore_offer": None, "migration_revert": None, "audit_stale": None, "live_standing": None,
         "neighborhood": None}


def _signals(**over):
    s = dict(_BASE)
    s.update(over)
    # Derive the BLOCKING finding fingerprint from any blocking_findings a caller set (the never-shed relay's
    # collapse key), unless it was set explicitly — mirrors gather_signals.
    if "blocking_finding_fingerprint" not in over:
        s["blocking_finding_fingerprint"] = (
            sorted(f"#{b['number']}" for b in (s.get("blocking_findings") or [])) or None)
    return s


def _blocking(n):
    """n blocking-finding rows ({number, title}) — what the never-shed relay counts."""
    return [{"number": str(i), "title": f"broken thing {i}"} for i in range(1, n + 1)]


def _findings_line(lines):
    return next((l for l in lines if "BLOCKING" in l), "(no findings relay)")


def main() -> int:
    failures = []
    register = "https://github.com/StarshipSuperjam/engine-template/issues?q=is:open+is:issue+label:engine"
    findings = _signals(blocking_findings=_blocking(20), register=register)

    with tempfile.TemporaryDirectory() as d:
        os.environ[boot_alarm_ledger.ENV_DIR] = d
        ledger = boot_alarm_ledger.ledger_path()
        print(f"(isolated demo ledger: {ledger})\n")

        print("=== Session 1 — 20 BLOCKING findings, seen for the FIRST time ===")
        first = _findings_line(boot._relay_lines(findings))
        print(f"  {first}\n")
        if "still" in first.lower():
            failures.append("session 1 should relay FULL, not a 'still' reminder")

        print("=== Session 2 — same 20 blocking findings, UNCHANGED (the resume Shane complained about) ===")
        second = _findings_line(boot._relay_lines(findings))
        print(f"  {second}\n")
        if "still" not in second.lower():
            failures.append("session 2 (unchanged) should COLLAPSE to a 'still …' reminder")
        if "blocking" not in second.lower() or "issues" not in second:
            failures.append("the terse reminder must STILL name the blocking findings and keep the link")

        print("=== Session 3 — blocking findings rose to 25, this got WORSE ===")
        worse = _findings_line(boot._relay_lines(_signals(blocking_findings=_blocking(25), register=register)))
        print(f"  {worse}\n")
        if "still" in worse.lower() or "grown" not in worse.lower():
            failures.append("a worsened condition must relay FULL with the 'grown' label, never collapse")

        print("=== Safety-gate alarm — same behaviour (the other standing item Shane sees repeat) ===")
        gate = _signals(gate="off", reason="branch protection has no required checks")
        g1 = next(l for l in boot._relay_lines(gate) if "gate" in l.lower())
        g2 = next(l for l in boot._relay_lines(gate) if "gate" in l.lower())
        print(f"  first sight: {g1}")
        print(f"  unchanged:   {g2}\n")
        if "still" in g1.lower():
            failures.append("gate first-sight should relay FULL, not a 'still' reminder")
        if "still off" not in g2.lower() or "turn my safety gate back on" not in g2.lower():
            failures.append("gate unchanged should collapse to a 'still off …' reminder that keeps the fix")

        print("=== Grounding survives — the one-line marker + all-clear never collapse ===")
        # Findings no longer drive the marker (a routine count is a quiet fact); a governance alarm does, and
        # names itself every session even as its relay collapses. All-clear renders calmly with the ▸ marker.
        marker = boot.present_marker_line(_signals(gate="off", reason="x"))
        clear_relay = boot._relay_lines(_signals(gate="on"))
        clear_marker = boot.present_marker_line(_signals(gate="on"))
        print(f"  marker while alarmed: {marker}")
        print(f"  all-clear relay (empty list = nothing pushed): {clear_relay}")
        print(f"  all-clear marker: {clear_marker}\n")
        if marker != "⚠ Your safety gate is off":
            failures.append("the present-marker must name the alarm every session (never collapses)")
        if clear_relay != [] or clear_marker != f"▸ {boot.PRESENT_MARKER}: all clear":
            failures.append("a healthy session must render all-clear, never a collapsed/odd marker")

        print("=== Fail-toward-full — a corrupt ledger relays in FULL, never silent ===")
        boot._relay_lines(findings)                       # seed a clean, collapsible state
        with open(ledger, "w", encoding="utf-8") as fh:   # scramble it
            fh.write("}{ not json")
        after = _findings_line(boot._relay_lines(findings))
        print(f"  {after}\n")
        if "still" in after.lower():
            failures.append("a corrupt ledger must FAIL-TOWARD-FULL, never collapse")

        os.environ.pop(boot_alarm_ledger.ENV_DIR, None)

    if failures:
        print("DEMO FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All checks passed: collapse on unchanged, full on new/worse, grounding marker always present, "
          "fail-toward-full on a corrupt ledger.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
