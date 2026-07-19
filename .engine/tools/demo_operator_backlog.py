#!/usr/bin/env python3
"""Operator-runnable demo of the "Your open issues" line — the count of YOUR own open issues (your product
backlog) on the session status card, shown next to the engine's own self-health findings.

It answers, in plain words, a question a non-engineer can't read code to verify: *does the status card show
MY open issues separately from the engine's own findings — and does it keep the two apart (the engine never
counting my product work as its own, and never counting its own items as mine)?*

It runs the REAL logic end-to-end — telemetry's own `count_open_operator_issues` (one Search-API call) and
`list_open_engine_issues`, boot's own `open_operator_count` / `open_findings` relays, and boot's own
`render_dashboard` — with only the network boundary faked (a transport that serves a Search count for the
operator backlog and an engine-labelled issue list for the engine findings). No network, no token, no edits.

It shows, and CHECKS (so this demo can FAIL — it is a falsification, not a showcase):
  * TWO SEPARATE COUNTS — an engine read finding 2 self-health items and an operator read finding 3 backlog
    items render as distinct lines ("Engine findings: 2" and "Your open issues: 3"), never summed;
  * THE BACKLOG IS ACTIONABLE — the operator line carries its own clickable filtered register (the exact
    `-label:` list the count came from), so a bare number is something you can click through to;
  * AN HONEST FAILURE, NOT A SILENT VANISH — when only the backlog read fails, the line says it couldn't be
    read this session rather than disappearing (and never shows a false 0);
  * NO ACCESS STAYS SILENT — with no GitHub access the line is suppressed entirely, like every other
    GitHub-derived line, so it never claims "0" it did not check;
  * NEVER AN ALARM — a routine backlog never reaches the ⚠ status marker, which stays engine-scoped.

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


def _render(finding_count, operator_count, operator_register, operator_degraded):
    """Render boot's REAL dashboard carrying these two counts as a live SessionStart would — every OTHER
    status signal is the only thing faked (mirrors the sibling boot-card demos)."""
    s = {"state": {"schema_version": 1, "standing_situation": {}, "integration_debt": {}},
         "refused": False, "gate": "on", "reason": None,
         "finding_count": finding_count, "unrated_count": None, "register": "", "finding_fingerprint": None,
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
        finding_count, _reg, _fp, _low, _find = boot.open_findings(_REPO, _TOKEN)
        operator_count, register = boot.open_operator_count(_REPO, _TOKEN)
        return finding_count, operator_count, register
    finally:
        telemetry.GitHubIssues = real


def main():
    failures = []

    print("=== Two separate counts — engine findings vs your own backlog ===")
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
    if "**Engine findings:** 2" not in dash:
        failures.append("the card must show the engine findings line")
    if "**Your open issues:** 3" not in dash:
        failures.append("the card must show the operator backlog as a distinct line")
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

    print("=== Never an alarm — the backlog never reaches the ⚠ status marker ===")
    marker = boot.present_marker_line(_marker_signals(operator_count=40))
    print(f"  marker: {marker!r}\n")
    if "open issues" in marker or "40" in marker:
        failures.append("a routine backlog must never appear in the ⚠ status marker")

    if failures:
        print("DEMO FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All checks passed: your backlog and the engine's findings are two distinct counts, the backlog is "
          "clickable, a solo read failure is stated (never a false 0), no access stays silent, and a routine "
          "backlog never becomes a ⚠ alarm.")
    return 0


def _marker_signals(operator_count):
    """A minimal all-clear signals dict carrying an operator backlog, to prove the marker ignores it."""
    return {"gate": "on", "reason": None, "refused": False, "finding_count": 0, "strand": None,
            "behind_origin": None, "off_main": None, "pr_conflict": None, "migration_revert": None,
            "restore_offer": None, "absent_home": None, "operator_backlog_count": operator_count}


if __name__ == "__main__":
    sys.exit(main())
