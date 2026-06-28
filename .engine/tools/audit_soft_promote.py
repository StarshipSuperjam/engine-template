#!/usr/bin/env python3
"""Promote a standing length-budget soft finding to a tracked engine Issue (issue #273 half 2, slice 2).

Slice 1 lets the weekly self-review SEE the firing soft validator findings; this closes the loop by giving
a standing one a durable home — a deduped, lane-aware engine-labelled Issue — so it reaches boot and the
issue tracker even when no one reads the digest closely. It only ever OPENS or UPDATES: it never closes an
Issue. A finding that has cleared is recommended for closure by the self-review's own backlog concern
(the audit reports and the operator decides — recommend, don't act), so this producer holds no autonomous
close.

SCOPE — length-budget nudges ONLY. The report-only `audit-prep` suite also carries other soft findings
(e.g. the audit-digest staleness warning) whose remedy is different and which already have an escalation
path; promoting them under a budget framing would misdescribe them. They are excluded by provenance: only
findings emitted by a `shape`-kind rule (the length-budget nudge) are promoted.

LANE — a budget overage on a TEMPLATE-OWNED file (machinery) cannot be durably fixed in this repo: the next
engine update replaces the engine's own files wholesale, so a local trim is overwritten. The Issue says so
plainly and points the durable fix UPSTREAM to the engine-template project — and the engine never files that
upstream report itself ("never phone home"); logging it there stays the operator's call. A budget overage on
a file THIS PROJECT owns (local state) is fixable here. Ownership is the authoritative machinery test
(module_coherence.provides_claims, the live-filesystem manifest claims). Today every budget-governed surface
is module-owned machinery, so the local lane is built and fixture-tested but not exercised live until a
deployed repo's own over-budget doc fires it.

SAFETY / HONESTY:
- source_id = "soft-budget:<file>" — keyed on the FILE, never the message. The live line-count in the
  message is per-occurrence material; keying on it would fork a new Issue every time the count changed. The
  path of a catalogued surface is a plain repo-relative path (it cannot contain the HTML-comment delimiters
  the tracking marker uses), so it is a stable, collision-free signal id — one Issue per over-budget surface.
- The finding message and file path are author-influenced text that lands in the Issue body, so both are
  defanged with the same neutraliser the slice-1 feed uses before they are embedded.
- A finding with no file location is skipped (it cannot be source-keyed); a length-budget finding always
  carries one.
- Fail-open: any error prints a visible status line and exits 0 — a transient GitHub blip must never fail
  the self-review, and the finding simply re-fires next run (and slice 1's digest still surfaces it).

CLI (the audit-prep workflow's promote step; needs GITHUB_REPOSITORY + GITHUB_TOKEN with issues:write —
the GitHub token, never the Claude token):
  uv run --directory .engine -- python tools/audit_soft_promote.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402  (the collect seam + the prompt-fence defang)
import telemetry         # noqa: E402  (the GitHub boundary + promote_finding + the benign severity class)
import issue_author      # noqa: E402  (the shared engine-Issue body contract)
import module_coherence  # noqa: E402  (provides_claims — the authoritative machinery test)

# The report-only suite the length-budget nudges join (slice 1). Read here, never the CI gate.
FEED_SUITE = "audit-prep"
# The dedup namespace for these tracked Issues. Disjoint from telemetry's own health sources
# (`rule:` / `check/`) and the other producers (`close/disposition/`, `migration/version-stamp/`), so this
# producer's Issues never collide with theirs.
SOURCE_PREFIX = "soft-budget:"


def _neutralize(text: str) -> str:
    """Render author-influenced text inertly in a GitHub issue body. The finding message and the file
    path embed an author-chosen filename, and a budget issue's body is rendered markdown — so neutralise
    both the prompt-fence rails (the slice-1 feed's defang, for when this body is later re-read into a
    persona prompt) AND the markdown/HTML a crafted filename could smuggle: HTML-escape the angle
    brackets and ampersand (no tag, no comment, no forged `<!-- engine-signal -->` tracking marker) and
    backslash-escape the markdown image/link/code characters (no beacon image, link, or code-span
    breakout). A plain repo path passes through untouched."""
    text = validate.defang_prompt_fence_markers(text or "")
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = text.replace("\\", "\\\\")
    for ch in ("`", "[", "]", "!"):
        text = text.replace(ch, "\\" + ch)
    return text


def _render(rel: str, message: str, machinery: bool) -> tuple:
    """The lane-aware (title, body_core) for one over-budget surface. `rel` is the raw repo path (the
    title is plain text, not rendered markdown) and `message` is the raw finding message; both the path
    and the message are neutralised before they enter the rendered body. body_core is prose only —
    telemetry appends its tracking trailers + signal marker."""
    where = _neutralize(rel)
    message = _neutralize(message)
    title = f"Engine length budget: {rel} is over its limit"
    if machinery:
        what_this_is = (
            f"The engine noticed one of its OWN files has grown past the length it is meant to stay "
            f"within. {message}\n\n"
            f"- **What it is:** engine machinery — a file the engine itself ships and maintains, not a "
            f"file in your project. (A shorter file is easier for a fresh AI session to read in full, "
            f"which is why the limit exists.)\n"
            f"- **Where:** `{where}`."
        )
        whats_next = (
            "Trimming it here will NOT last: the next engine update replaces the engine's own files "
            "wholesale, so a local edit to this one is overwritten on the next upgrade.\n\n"
            "- **To fix it durably,** raise it in the engine-template project this engine was created "
            "from — the project you (or whoever set up this engine) used GitHub's \"Use this template\" "
            "on. If you are not sure where that is, whoever set the engine up will know.\n"
            "- **Or leave it** — it is only a nudge and never blocks anything.\n"
            "- The engine has not sent anything to that upstream project and will not; logging it there "
            "is yours to decide.\n"
            "- This is also noted in your weekly self-review — this Issue just keeps it tracked until it "
            "is resolved or you close it."
        )
    else:
        what_this_is = (
            f"The engine noticed one of your project's files has grown past the length it is meant to stay "
            f"within. {message}\n\n"
            f"- **What it is:** a file your project owns — fixable right here.\n"
            f"- **Where:** `{where}`."
        )
        whats_next = (
            "You can:\n\n"
            "- **Trim it** in an ordinary change, or\n"
            "- **Raise its budget** with a recorded reason, or\n"
            "- **Leave it** — it is only a nudge and never blocks anything.\n"
            "- This is also noted in your weekly self-review — this Issue just keeps it tracked until it "
            "is resolved or you close it."
        )
    body_core = issue_author.render_engine_issue_body(what_this_is=what_this_is, whats_next=whats_next)
    return title, body_core


def budget_records(now: str, *, claims: dict | None = None) -> list:
    """One finding-record per firing length-budget nudge — soft, kind `shape`, with a file location —
    each carrying a lane-aware `title` + `body_core` ready for telemetry.promote_finding. Lane is the
    authoritative machinery test: a file a present module manifest claims is overlaid on every upgrade.
    `claims` (the {relpath: [owner,...]} ownership map) is injectable so the demo/tests can exercise both
    lanes on a real over-budget finding without mutating shipped files; by default it is computed live."""
    findings = validate.collect(FEED_SUITE, {}, with_source=True)
    if claims is None:
        claims = module_coherence.provides_claims(module_coherence.discover_manifests())
    records = []
    for f in findings:
        if f.get("severity") == "hard":
            continue
        if f.get("source_kind") != "shape":          # only the length-budget nudge (not e.g. staleness)
            continue
        rel = (f.get("location") or {}).get("file")
        if not rel:                                  # cannot source-key a location-less finding
            continue
        if any(c in rel for c in "<>\n\r"):
            # An anomalous path that could break the tracking marker the source_id is embedded in (a real
            # catalogued surface path never contains these). Skip it rather than corrupt dedup; defence in
            # depth alongside the body neutralisation below.
            continue
        machinery = bool(claims.get(rel))            # claimed by a manifest's provides => machinery
        title, body_core = _render(rel, f.get("message", ""), machinery)
        records.append({
            "source_id": f"{SOURCE_PREFIX}{rel}",
            "severity": telemetry.PERSISTENT_BENIGN,
            "message": _neutralize(f.get("message", "")),
            "location": {"file": rel},
            "title": title,
            "body_core": body_core,
        })
    return records


def promote(repo: str, token: str, now: str, *, transport=None, claims: dict | None = None) -> tuple:
    """Open-or-update one deduped, lane-aware tracked Issue per firing budget nudge. No close.
    Returns (tracked_count, degraded). `transport` is injectable so the demo/tests fake only the network;
    `claims` is passed through to budget_records for lane control in the demo/tests."""
    records = budget_records(now, claims=claims)
    if not records:
        return 0, False
    github = telemetry.GitHubIssues(repo, token, transport=transport)
    tracked, degraded = 0, False
    for r in records:
        result = telemetry.promote_finding(github, r, now, title=r["title"], body_core=r["body_core"])
        if result is False:
            degraded = True
        else:
            tracked += 1
    return tracked, degraded


def main(argv: list) -> int:
    repo = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        print("usage: audit_soft_promote.py   (needs GITHUB_REPOSITORY and GITHUB_TOKEN with issues:write "
              "in the environment; it uses the GitHub token, never the Claude token)", file=sys.stderr)
        return 2
    try:
        tracked, degraded = promote(repo, token, telemetry.utc_now())
    except Exception as exc:  # noqa: BLE001 — fail-open: a transient blip must never fail the self-review
        print(f"Could not track standing soft findings this run ({exc}); the self-review continues and the "
              f"finding will re-fire next run. Nothing was lost.")
        return 0
    if degraded:
        print("Could not reach GitHub to track one or more standing soft findings this run; they will "
              "re-fire next run. Nothing was lost.")
    elif tracked:
        print(f"Tracked {tracked} standing length-budget finding(s) as engine issue(s).")
    else:
        print("No standing length-budget findings are firing — nothing to track this run.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
