#!/usr/bin/env python3
"""Derive the project's standing-situation ("where we are") LIVE from native GitHub sources (engine-template
#100; engine-planning D-198 -> D-199, superseding D-196/D-197).

The corrected design: "where we are" is a **read-only projection of native sources**, assembled live at
orientation — never a stored marker any session *advances* (that was the rejected category error). This module
is that projection:

  - **phase** <- the most-recently-merged **tracked build Issue**, formatted "<title> (issue #N)". GitHub's REST
    API exposes no "closing issue references" (that field is GraphQL-only), so we read it the way GitHub itself
    derives it: take the recent closed PRs, order the merged ones by `merged_at` (newest first), parse each PR
    body for a closing keyword (`Closes/Fixes/Resolves #N`, the common forms), and take the first whose
    referenced Issue carries the engine label. A PR that closes several Issues contributes its first reference.
  - **milestone** <- the project's active open GitHub Milestone (the earliest-due one), or None when the project
    keeps none ("none set" — the honest normal state, not an error).

This is the strong/best-effort split the design names: `phase` is engine-derivable (strong, like the debt
count); `milestone` is operator-plan-derivable (best-effort — None when no Milestone exists).

**All-or-nothing degradation.** A *read failure* (any HTTP >= 400, an unreachable host, an unexpected response
shape) is NEVER swallowed as "nothing here" — it RAISES `DeriveUnavailable`, so [boot] falls back to the
committed offline cache (rendered stale-labelled) rather than presenting a confident, wrong live answer (the
failure mode state/README forbids). A *successful* read that simply finds nothing returns None for that field,
which boot renders as the honest live "none set" / "—".

This module is a **pure leaf**: it imports only the standard library and takes an injected GitHub reader
(`gh`, duck-typed: `gh.repo`, `gh.label`, `gh._transport(method, path, body) -> (status, json)` — the seam
[telemetry].GitHubIssues exposes). It imports neither boot nor telemetry, so there is no import cycle: boot
calls it for the live display, telemetry calls it to refresh the offline cache on its GitHub pass. It performs
**no writes** — it never touches state.json, never advances a marker, never hand-types a value.

Run the demo: `uv run --directory .engine -- python tools/standing_situation.py demo`
"""
from __future__ import annotations
import re

# GitHub's own issue-closing keywords (https://docs.github.com/issues — closing keywords). Matching these in a
# merged PR's body approximates, over REST, the linkage GitHub records as `closingIssuesReferences` for the
# common forms ("Closes #80", "Closes: #80", "Fixed #80"). The leading \b keeps it from matching inside other
# words (e.g. "discloses #80", "unfixed #80"); the separator allows a space and/or a colon.
_CLOSES_RE = re.compile(r"\b(?:close[sd]?|fix(?:es|ed)?|resolve[sd]?)\b[:\s]+#(\d+)", re.IGNORECASE)

# How many recent closed PRs to scan for the latest tracked build before giving up (returning None for phase).
_PR_WINDOW = 30


class DeriveUnavailable(Exception):
    """Raised when GitHub cannot be read while deriving the standing-situation (an outage, or a 4xx/5xx
    auth/scope error, or an unexpected response shape). It is NEVER swallowed as "no milestone / no phase":
    a read failure that read as genuine-absence would silently present a confident, wrong live card — the
    exact #100-class trust failure this correction exists to remove. Boot catches it and falls back to the
    committed offline cache, rendered stale-labelled."""


def _read(gh, path: str):
    """One read through the injected transport. Raises DeriveUnavailable on any failure — an HTTP error
    status (>= 400) or a null body — so a read failure can never be mistaken for genuine absence."""
    status, data = gh._transport("GET", path, None)
    if status >= 400 or data is None:
        raise DeriveUnavailable(f"GitHub returned {status} reading {path}")
    return data


def derive_milestone(gh) -> str | None:
    """The project's active open Milestone title, or None when there are none ("none set"). "Active" = the
    earliest-due open Milestone (GitHub's `sort=due_on&direction=asc`); ties / no due date fall to the first
    returned. A read failure raises DeriveUnavailable (never read as "none set")."""
    data = _read(gh, f"/repos/{gh.repo}/milestones?state=open&sort=due_on&direction=asc&per_page=100")
    if not isinstance(data, list):
        raise DeriveUnavailable("milestones response was not a list")
    if not data:
        return None                          # genuinely no open milestone -> "none set" (honest normal)
    return (data[0].get("title") or "").strip() or None


def derive_phase(gh, *, window: int = _PR_WINDOW) -> str | None:
    """The most-recently-merged tracked build Issue, as "<title> (issue #N)", or None when no tracked build
    is found in the recent window. Walks closed PRs newest-first; for each *merged* PR, parses its body for a
    closing keyword and, if the referenced Issue carries the engine label, returns that Issue's title. A read
    failure raises DeriveUnavailable (never read as "no phase")."""
    pulls = _read(gh, f"/repos/{gh.repo}/pulls?state=closed&sort=updated&direction=desc&per_page={window}")
    if not isinstance(pulls, list):
        raise DeriveUnavailable("pulls response was not a list")
    # The REST API has no `sort=merged`, and a post-merge edit can bump `updated`; so among the closed PRs we
    # keep only the merged ones and order them by `merged_at` ourselves — truly "most-recently-merged" first.
    merged = sorted((pr for pr in pulls if isinstance(pr, dict) and pr.get("merged_at")),
                    key=lambda pr: pr["merged_at"], reverse=True)
    for pr in merged:
        match = _CLOSES_RE.search(pr.get("body") or "")
        if not match:
            continue                         # this PR closed no Issue (e.g. a standalone slice) — skip
        number = int(match.group(1))
        issue = _read(gh, f"/repos/{gh.repo}/issues/{number}")
        if not isinstance(issue, dict):
            raise DeriveUnavailable(f"issue #{number} response was not an object")
        names = {lab.get("name") for lab in (issue.get("labels") or []) if isinstance(lab, dict)}
        if gh.label not in names:
            continue                         # closed a non-engine Issue (not a tracked build) — keep walking
        title = (issue.get("title") or "").strip()
        if title:
            return f"{title} (issue #{number})"
    return None                              # no tracked build in the window -> phase is "—" (honest)


def derive_standing_situation(gh) -> dict:
    """Assemble {"milestone", "phase"} live from native sources, read-only. ALL-OR-NOTHING: if either read
    fails, DeriveUnavailable propagates (boot then shows the cached line); on success, a None field means
    genuine absence (boot renders "none set" / "—"). Milestone is read first so a read failure short-circuits
    before the phase walk."""
    milestone = derive_milestone(gh)
    phase = derive_phase(gh)
    return {"milestone": milestone, "phase": phase}


# ---- operator-runnable demo (real derive logic; only the GitHub network is faked) -----------------

class _FakeGH:
    """A stand-in GitHub reader for the demo: a fixed repo/label and an injected transport. Lets the demo run
    the REAL derive + render logic fully offline ([[demo-must-exercise-real-logic]]) — only the network is faked."""

    def __init__(self, transport, *, repo="your-org/your-project", label="engine"):
        self.repo = repo
        self.label = label
        self._transport = transport


def _canned(milestones, pulls, issues):
    """Build a transport answering the three GETs derive makes, from canned fixtures. `milestones` is the
    milestones-list payload; `pulls` the closed-PRs payload; `issues` maps issue number -> issue object."""
    def transport(method, path, body):
        if "/milestones" in path:
            return 200, milestones
        if "/pulls" in path:
            return 200, pulls
        m = re.search(r"/issues/(\d+)", path)
        if m:
            return 200, issues.get(int(m.group(1)))
        return 404, None
    return transport


def _fail_transport(method, path, body):
    """A transport that always reports a read failure (here, an auth error), to demonstrate the all-or-nothing
    fall-back to the cached line — a failure must NEVER read as a confident live 'none set'."""
    return 403, None


def _where_lines(boot, *, live, state) -> list:
    """Render the REAL boot card over a complete signals dict and return its standing block — the
    'Where we are' line, the 'Milestone' line, and the cached-staleness sub-line when present — so the
    operator sees the actual card text, not a Python structure."""
    signals = {"state": state, "refused": False, "gate": "on", "reason": None,
               "finding_count": 0, "register": "", "findings_unavailable": False,
               "debt_count": 0, "debt_as_of": None, "att_lines": [], "att_degraded": False,
               "shipped": [], "stance": "Exploring", "strand": None, "pr_conflict": None,
               "audit_stale": None, "live_standing": live}
    lines = boot.render_dashboard(signals).splitlines()
    out = []
    for i, ln in enumerate(lines):
        if ln.startswith("**Where we are"):
            out.append(ln)
            for nxt in lines[i + 1:i + 3]:          # the Milestone line + an optional staleness sub-line
                if nxt.startswith("**Milestone:**") or nxt.startswith("_("):
                    out.append(nxt)
                else:
                    break
            break
    return out


def _demo() -> int:
    import boot  # lazy (boot imports this module at top; importing it here avoids a load-time cycle)

    print("What boot shows for 'where we are' — derived live from your GitHub each session:\n")

    # (1) A project that DOES keep a milestone, with a recent tracked build.
    gh1 = _FakeGH(_canned(
        milestones=[{"title": "Ship the beta", "due_on": "2026-09-01T00:00:00Z"}],
        pulls=[{"number": 42, "merged_at": "2026-06-10T00:00:00Z", "body": "Closes #40\n\nthe checkout page"}],
        issues={40: {"number": 40, "title": "Build the checkout page", "labels": [{"name": "engine"}]}}))
    print("1) A project with an active milestone and a recent tracked build:")
    for ln in _where_lines(boot, live=derive_standing_situation(gh1), state=None):
        print("   " + ln)
    print()

    # (2) A project that keeps NO milestone (this repo's real state) — "No milestone is open" is honest-normal.
    gh2 = _FakeGH(_canned(
        milestones=[],
        pulls=[{"number": 99, "merged_at": "2026-06-13T00:00:00Z", "body": "Closes #80\n\nthe un-stranding fix"}],
        issues={80: {"number": 80, "title": "Operator checkout can silently drift", "labels": [{"name": "engine"}]}}))
    print('2) A project that keeps NO GitHub milestone — "No milestone is open" is a normal state, not an error:')
    for ln in _where_lines(boot, live=derive_standing_situation(gh2), state=None):
        print("   " + ln)
    print()

    # (3) GitHub unreachable / auth failed -> the derive RAISES, so boot shows the cached copy, stale-labelled.
    #     A read failure must NEVER read as a confident live 'none set'.
    try:
        live3 = derive_standing_situation(_FakeGH(_fail_transport))
    except DeriveUnavailable:
        live3 = None
    cached_state = {"schema_version": 1,
                    "standing_situation": {"milestone": None, "phase": "Building the checkout page (issue #40)",
                                           "as_of": "2026-06-10T12:00:00Z"},
                    "integration_debt": {}}
    print("3) When GitHub can't be read, boot falls back to the last cached copy and says so plainly:")
    for ln in _where_lines(boot, live=live3, state=cached_state):
        print("   " + ln)
    print()

    # (4) Before/after — the stale committed marker (shown as if current) vs the live-derived lines.
    stale_phase = "Making the Engine's status and alarms reach you (issue #83)"
    print("4) Issue #100 in one view:")
    print(f'   BEFORE — a stored marker nothing updated, shown as if current:  "{stale_phase}"')
    print("   AFTER  — derived live from GitHub each session:")
    for ln in _where_lines(boot, live=derive_standing_situation(gh2), state=None):
        print("            " + ln)
    print()
    print("Note: in THIS construction repo the phase line shows a maintainer-framed issue title verbatim")
    print("(e.g. #80's '...silently drift'); in a generated project, build-issue titles are written to read")
    print("cleanly for you. No real GitHub call was made, and your saved status was not modified.")
    return 0


def main(argv: list | None = None) -> int:
    import sys
    argv = list(argv if argv is not None else sys.argv[1:])
    if argv and argv[0] == "demo":
        return _demo()
    print("usage: standing_situation.py demo")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
