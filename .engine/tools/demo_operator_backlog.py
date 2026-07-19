#!/usr/bin/env python3
"""Operator-runnable demo of the whole-backlog status card — YOUR own open issues (your product backlog) and
the engine's own self-health findings, folded into one plain total the card leads with and kept as two
distinct lines below.

It answers, in plain words, a question a non-engineer can't read code to verify: *does the status card lead
with my whole open backlog, keep my own work and the engine's own findings as distinct lines, and never turn a
routine backlog into an alarm?*

It runs the REAL logic end-to-end — telemetry's own `count_open_operator_issues` (one Search-API call) and
`list_open_engine_issues`, boot's own `open_operator_count` / `open_findings` relays, and boot's own
`render_dashboard` / `present_marker_line` — with only the network boundary faked (a transport that serves a
Search count for the operator backlog and an engine-labelled issue list for the engine findings). No network,
no token, no edits.

It shows, and CHECKS (so this demo can FAIL — it is a falsification, not a showcase):
  * ONE HEADLINE TOTAL — an engine read finding 2 self-health items and an operator read finding 3 backlog
    items lead the card as "5 open issues (2 are engine-health)" — the whole backlog, the engine share named;
  * TWO DISTINCT LINES BELOW — the same two counts render as their own lines ("Your open issues: 3" above
    "Engine findings: 2"), your own work first, each with its own clickable filtered register;
  * AN HONEST FAILURE, NOT A SILENT VANISH — when only the backlog read fails, the line says it couldn't be
    read this session rather than disappearing (and never shows a false 0);
  * NO ACCESS STAYS SILENT — with no GitHub access the line is suppressed entirely, like every other
    GitHub-derived line, so it never claims "0" it did not check;
  * A CALM MARKER, NEVER A ⚠ ALARM — the whole-backlog total leads the top marker with a calm `▸`, never a ⚠:
    a backlog is work to see, not a governance alarm.

Vary it yourself: change the counts below and re-run.

Run: uv run --directory .engine -- python tools/demo_operator_backlog.py
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import boot         # noqa: E402
import telemetry    # noqa: E402

_REPO, _TOKEN = "acme/store", "tok"
_ALL_OPEN = f"https://github.com/{_REPO}/issues?q=is:open+is:issue"


class _FakeNetwork:
    """The only faked thing: the GitHub boundary. Serves a Search `total_count` for the operator backlog and
    an engine-labelled issue list for the engine findings. `fail` makes ONLY the Search (operator) read fail,
    to exercise the solo-failure path. Everything above it is the real GitHubIssues + boot logic."""

    def __init__(self, operator_total, engine_issues, *, fail=False):
        self.operator_total = operator_total
        self.engine_issues = engine_issues
        self.fail = fail

    def transport(self, method, path, body):
        base = path.split("?")[0]
        if base == "/search/issues":                       # the operator backlog count (one call)
            if self.fail:
                return 503, None
            return 200, {"total_count": self.operator_total, "items": []}
        if base.endswith("/issues") and method == "GET":   # the engine-labelled findings (open_findings)
            return 200, self.engine_issues
        return 404, None


def _engine_issue(number, title):
    # A minimal open engine-labelled issue; the body carries no source_id/severity marker, which is fine —
    # list_open_engine_issues still counts it (an unrated finding), which is all this demo needs.
    return {"number": number, "title": title, "body": "The engine flagged this about its own health."}


def _counts_state(finding_count, operator_count):
    """Derive the whole-backlog total + degraded state the way gather_signals does, so the demo renders the
    real headline the operator sees rather than a hand-seeded one."""
    have_e = finding_count is not None
    have_o = operator_count is not None
    if have_e and have_o:
        return "both", finding_count + operator_count
    if have_e or have_o:
        return "partial", None
    return "offline", None


def _render(finding_count, operator_count, operator_register, operator_degraded):
    """Render boot's REAL dashboard carrying these two counts as a live SessionStart would — every OTHER
    status signal is the only thing faked (mirrors the sibling boot-card demos)."""
    counts_state, total_open = _counts_state(finding_count, operator_count)
    s = {"state": {"schema_version": 1, "standing_situation": {}, "integration_debt": {}},
         "refused": False, "gate": "on", "reason": None,
         "finding_count": finding_count, "unrated_count": None, "register": "",
         "total_open": total_open, "counts_state": counts_state, "all_open_register": _ALL_OPEN,
         "blocking_findings": [], "blocking_finding_fingerprint": None,
         "debt_count": 0, "debt_as_of": None, "att_lines": [], "att_degraded": [], "shipped": [],
         "stance": "Exploring", "strand": None, "behind_origin": None, "off_main": None, "pr_conflict": None,
         "restore_offer": None, "migration_revert": None, "audit_stale": None, "live_standing": None,
         "neighborhood": None, "map_rebuilt": False, "map_corrupt": False, "ledger_malformed": None,
         "migration_stalled": False, "recall_offline": False, "set_aside": None, "foreign_license": None,
         "triage_pressure_line": None, "contract_rate_line": None,
         "operator_backlog_count": operator_count, "operator_backlog_register": operator_register,
         "operator_backlog_degraded": operator_degraded}
    return boot.render_dashboard(s)


def _read_through(fake):
    """Run boot's REAL relays (open_findings + open_operator_count) against the faked boundary, by binding the
    fake transport onto the GitHubIssues the relays construct. Returns (finding_count, operator_count,
    register)."""
    real = telemetry.GitHubIssues

    def _bound(repo, token, **kw):
        return real(repo, token, transport=fake.transport)

    telemetry.GitHubIssues = _bound
    try:
        finding_count, _reg, _low, _find = boot.open_findings(_REPO, _TOKEN)
        operator_count, register = boot.open_operator_count(_REPO, _TOKEN)
        return finding_count, operator_count, register
    finally:
        telemetry.GitHubIssues = real


def main():
    failures = []

    print("=== One headline total + two distinct lines — your backlog and the engine's findings ===")
    fake = _FakeNetwork(operator_total=3, engine_issues=[_engine_issue(1, "CI keeps flaking"),
                                                         _engine_issue(2, "a hook crashed")])
    finding_count, operator_count, register = _read_through(fake)
    print(f"  engine read -> {finding_count} finding(s); operator read -> {operator_count} backlog issue(s)")
    dash = _render(finding_count, operator_count, register, operator_degraded=False)
    print(dash + "\n")
    if finding_count != 2:
        failures.append("the engine read must count its 2 engine-labelled findings")
    if operator_count != 3:
        failures.append("the operator read must count 3 backlog issues from the Search total_count")
    if "**5 open issues** (2 are engine-health)" not in dash:
        failures.append("the card must lead with the whole-backlog total, the engine share named")
    if "**Engine findings:** 2" not in dash:
        failures.append("the card must keep the engine findings as its own line")
    if "**Your open issues:** 3" not in dash:
        failures.append("the card must keep the operator backlog as its own distinct line")
    # your own work leads the two subset lines (the engine's own findings are the lower priority)
    if dash.index("**Your open issues:**") > dash.index("**Engine findings:**"):
        failures.append("your own open issues must render ABOVE the engine findings")
    if register is None or register not in dash:
        failures.append("the backlog line must carry its clickable filtered register")
    if "-label:engine" not in (register or ""):
        failures.append("the register must be the operator's own (non-engine) filtered list")

    print("=== An honest failure — only the backlog read fails, the line says so (never a false 0) ===")
    failed = _FakeNetwork(operator_total=3, engine_issues=[_engine_issue(1, "CI keeps flaking")], fail=True)
    fcount, ocount, oreg = _read_through(failed)
    degraded = bool(_REPO and _TOKEN) and ocount is None
    dash2 = _render(fcount, ocount, oreg, operator_degraded=degraded)
    print(dash2 + "\n")
    if ocount is not None:
        failures.append("a failed backlog read must degrade to None, never a number")
    if "couldn't read your issue backlog" not in dash2:
        failures.append("a failed backlog read must SAY so, not silently vanish")
    if "Your open issues:** 0" in dash2:
        failures.append("a failed read must never render a false 0")
    if "**Engine findings:** 1" not in dash2:
        failures.append("the engine line must still render when only the backlog read failed (independent)")

    print("=== No GitHub access — the line is suppressed entirely, never a claimed 0 ===")
    ncount, nreg = boot.open_operator_count(None, None)
    # With no access the engine read has nothing either, so render its line as unread too (finding_count
    # None) — otherwise the card would show a fresh-looking "Engine findings: 0" that no-access can't produce.
    dash3 = _render(None, ncount, nreg, operator_degraded=False)
    print(dash3 + "\n")
    if ncount is not None:
        failures.append("no repo/token must return None (no read attempted)")
    if "Your open issues" in dash3:
        failures.append("with no access the backlog line must be suppressed entirely")

    print("=== A calm marker, never a ⚠ alarm — the whole-backlog total leads the top marker ===")
    marker = boot.present_marker_line(_marker_signals(finding_count=2, operator_count=40))
    print(f"  marker: {marker!r}\n")
    if marker != f"▸ {boot.PRESENT_MARKER}: 42 open issues (2 are engine-health)":
        failures.append("the marker must lead with the calm whole-backlog total")
    if "⚠" in marker:
        failures.append("a routine backlog total must never be a ⚠ alarm")

    if failures:
        print("DEMO FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All checks passed: the card leads with your whole open backlog (the engine share named), keeps your "
          "own work and the engine's findings as two distinct lines, states a solo read failure (never a false "
          "0), stays silent with no access, and leads the marker with a calm total — never a ⚠ alarm.")
    return 0


def _marker_signals(finding_count, operator_count):
    """A minimal calm signals dict carrying both counts, to show the marker leads with the whole-backlog total
    (a calm ▸ line, never a ⚠)."""
    counts_state, total_open = _counts_state(finding_count, operator_count)
    return {"gate": "on", "reason": None, "refused": False, "finding_count": finding_count, "strand": None,
            "behind_origin": None, "off_main": None, "pr_conflict": None, "migration_revert": None,
            "restore_offer": None, "absent_home": None, "operator_backlog_count": operator_count,
            "counts_state": counts_state, "total_open": total_open}


if __name__ == "__main__":
    sys.exit(main())
