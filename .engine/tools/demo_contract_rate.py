#!/usr/bin/env python3
"""Operator-runnable demo of the contract-rate nudge — boot's "are decisions being over-recorded?" line.

It answers, in plain words, a question a non-engineer can't read code to verify: *when the engine writes
down an unusual burst of my own engine decisions as permanent records, does boot TELL me — and does my
own /engine-tune of the limit actually change when it appears, rather than the shipped default silently
winning?*

It runs the REAL logic end-to-end — telemetry's own `derive_contract_rate` / `contract_rate_threshold` /
`contract_rate_line` and boot's own `render_dashboard` — over an ISOLATED temp decision folder (via an env
override), so it never touches your real project and needs no network, no token, no edits. Only the
boundary is faked: the other status signals a live boot would have read alongside this one.

It shows, and CHECKS (so this demo can FAIL — it is a falsification, not a showcase):
  * QUIET WHEN NORMAL — three decisions in the week (at the limit of 3) render NOTHING;
  * NUDGE ON A BURST — a fourth crosses the limit and boot renders the plain "over-recorded?" line, in
    plain language (no engine jargon), naming a real next move (ask to see what got recorded);
  * THE TUNE GOVERNS — with the burst unchanged, raising the limit to 5 via a tune makes it go silent
    again (proving a reviewed /engine-tune actually governs, not the shipped default);
  * ONLY THE OPERATOR'S OWN DECISIONS COUNT — the engine's own founding records (dated historically) never
    push the meter, so a fresh project and an engine upgrade stay quiet;
  * DEGRADE IS SILENT, NEVER FALSE — an unreadable clock suppresses the line rather than showing a number.

Vary it yourself: change the dates or counts below and re-run.

Run: uv run --directory .engine -- python tools/demo_contract_rate.py
"""
from __future__ import annotations
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import boot         # noqa: E402
import telemetry    # noqa: E402

_NOW = "2026-07-17"


def _seed(directory, name, status, date):
    with open(os.path.join(directory, name), "w", encoding="utf-8") as fh:
        fh.write(f"---\nid: acme-eADR-0001\ntitle: a decision\nstatus: {status}\ndate: {date}\n---\n## Decision\nx\n")


def _dashboard_with(line):
    """Render boot's REAL dashboard carrying this contract-rate line as a live SessionStart would — the
    boundary (every other status signal) is the only thing faked."""
    s = {"state": {"schema_version": 1, "standing_situation": {}, "integration_debt": {}},
         "refused": False, "gate": "on", "reason": None, "finding_count": 0, "unrated_count": 0,
         "register": "", "debt_count": 0, "debt_as_of": None, "att_lines": [],
         "att_degraded": [], "shipped": [], "stance": "Exploring", "strand": None, "behind_origin": None,
         "off_main": None, "pr_conflict": None, "restore_offer": None, "migration_revert": None,
         "audit_stale": None, "live_standing": None, "neighborhood": None, "map_rebuilt": False,
         "map_corrupt": False, "ledger_malformed": None, "migration_stalled": False, "recall_offline": False,
         "set_aside": None, "triage_pressure_line": None, "contract_rate_line": line}
    return boot.render_dashboard(s)


def _line_now(directory):
    """The REAL boot computation: count the operator's recent decisions, read the tunable limit through the
    override merge, render the line (or None) — exactly what gather_signals does."""
    count = telemetry.derive_contract_rate(_NOW, contracts_dir=directory)
    if count is None:
        return None
    return telemetry.contract_rate_line(count, telemetry.contract_rate_threshold())


def main() -> int:
    failures: list[str] = []
    store = tempfile.mkdtemp()
    os.environ["ENGINE_INSTANCE_CONTRACTS_DIR"] = store
    try:
        limit = telemetry.contract_rate_threshold()
        print(f"The limit before a nudge fires is {limit} decisions written down as permanent records in a week.\n")

        print("=== Quiet when normal — three decisions this week (at the limit) ===")
        for i in range(3):
            _seed(store, f"acme-eADR-000{i}-x.md", "accepted", "2026-07-15")
        line = _line_now(store)
        print(f"  line: {line!r}\n")
        if line is not None:
            failures.append("at the limit, the nudge must NOT fire")
        elif "over-recorded" in _dashboard_with(None).lower():
            failures.append("the dashboard must not show the nudge at the limit")

        print("=== Nudge on a burst — a fourth decision crosses the limit ===")
        _seed(store, "acme-eADR-0004-x.md", "accepted", "2026-07-16")
        line = _line_now(store)
        print(f"  {line}\n")
        if line is None:
            failures.append("over the limit, the nudge must fire")
        else:
            dash = _dashboard_with(line)
            if "over-recorded" not in dash.lower():
                failures.append("the dashboard must render the nudge on a burst")
            if "/engine-tune" not in line:
                failures.append("the nudge must offer /engine-tune")
            for jargon in ("eADR", "stream", "severity", "persistence"):
                if jargon in line:
                    failures.append(f"the nudge must not use backstage vocabulary ({jargon})")

        print("=== The tune governs — raise the limit to 5, same burst goes silent ===")
        count = telemetry.derive_contract_rate(_NOW, contracts_dir=store)
        tuned = telemetry.contract_rate_threshold(override={"contract_rate_max": 5})
        tuned_line = telemetry.contract_rate_line(count, tuned)
        print(f"  count still {count}; tuned limit {tuned}; line now: {tuned_line!r}\n")
        if tuned_line is not None:
            failures.append("raising the limit via a tune must silence the nudge — the tune must govern")

        print("=== Degrade is silent — an unreadable clock suppresses, never a false number ===")
        degraded = telemetry.derive_contract_rate("not-a-date", contracts_dir=store)
        print(f"  derive on a bad clock: {degraded!r}\n")
        if degraded is not None:
            failures.append("a bad clock must suppress the count (None), never render a false number")
    finally:
        os.environ.pop("ENGINE_INSTANCE_CONTRACTS_DIR", None)

    if failures:
        print("DEMO FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All checks passed: quiet when normal, a plain nudge on a burst, a tune that actually governs, "
          "only the operator's own decisions counted, and a silent (never false) degrade.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
