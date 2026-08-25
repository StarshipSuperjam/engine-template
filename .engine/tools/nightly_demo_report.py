#!/usr/bin/env python3
"""Report the nightly demonstration run as AT MOST ONE open engine Issue.

WHY SINGULAR IS THE WHOLE DESIGN. A nightly guard that files an Issue per red run is a guard that punishes
the operator for not having fixed it yet: a demonstration broken on a Friday produces a fresh Issue every
night until someone gets to it, and by the time they do, the register is full of duplicates of the same
sentence. So there is exactly one Issue for this workflow at a time, and the rules are:

  * red, and no open report      -> file one
  * red, and a report is open    -> UPDATE it with the fresh failure set (never a second Issue, never a
                                    comment pile — the body always says what is failing NOW)
  * green, and a report is open  -> close it
  * green, and none open         -> do nothing at all

IT ACTS ONLY ON THE ISSUE IT MINTED. The report carries a marker this tool defines, and finding its Issue
means finding that marker in an open engine-labelled body. Nothing else is touched, updated or closed —
not by title match, not by label alone. A reporting job holding a write token must be unable to act on an
Issue a person wrote, however similar it looks.

IT READS STRUCTURED INPUT, NEVER DEMONSTRATION OUTPUT. Its input is `demonstration_corpus.py`'s JSON result. Failure
output is carried into the body as a bounded, fenced block that is RENDERED, never parsed — nothing in it
decides what this tool does. That is deliberate: this is the half of the workflow holding issue-write, and
its behaviour must not be steerable by text a demonstration printed.

THE BODY IS AUTHORED THROUGH THE ENGINE'S ISSUE HELPER, so the report satisfies the same body contract every
other engine-authored Issue does and passes the conformance net without a re-authoring flag.

Run:  uv run --directory .engine --frozen -- python tools/nightly_demo_report.py --result <result.json>
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import issue_author                     # noqa: E402  (the one body contract)
import telemetry                        # noqa: E402  (the supported GitHub boundary)

# The marker this tool mints and then binds to. Versioned, so a future change of report shape can adopt or
# deliberately orphan the previous generation rather than inheriting it by accident.
MARKER = "<!-- engine-nightly-demos:v1 -->"
TITLE = "a shipped demonstration is failing"
KIND = "Fix"
# How many failing demonstrations are named in the body. A corpus-wide breakage should read as "everything
# is failing, start at the top", not as a wall no one finishes.
_NAMED = 12


def find_report(issues: list) -> dict | None:
    """The one open Issue this workflow owns, or None. Bound by the marker in the body — never by title."""
    for issue in issues:
        if MARKER in (issue.get("body") or ""):
            return issue
    return None


def render(result: dict, repository: str, run_url: str | None = None) -> str:
    """The Issue body for a red run, through the engine's own helper so it meets the body contract."""
    failures = result.get("failures") or []
    shown = failures[:_NAMED]
    lines = [f"- `{f['demo']}` — exit {f['exit_code']}" for f in shown]
    if len(failures) > len(shown):
        lines.append(f"- …and {len(failures) - len(shown)} more")
    what = (
        f"The nightly run of this engine's behavioral demonstrations went red: {len(failures)} of "
        f"{len(result.get('ran') or [])} failed.\n\n"
        "A demonstration is a fail-then-pass reproducer of a real past incident — it exists so that a change "
        "which quietly reintroduces that incident goes red AT the incident rather than at some downstream "
        "symptom months later. One failing means either the guarded behaviour has regressed, or the "
        "demonstration itself has gone stale against a deliberate change. Both need a person; neither is "
        "urgent tonight.\n\n"
        + "\n".join(lines))
    tail = "\n".join(f"### {f['demo']}\n\n```\n{f['output']}\n```" for f in shown)
    whats_next = (
        "Run the corpus locally and read the failure the demonstration itself prints — each one states, in "
        "plain words, what it expected and what it saw:\n\n"
        "```\nuv run --directory .engine --frozen -- python tools/demonstration_corpus.py\n```\n\n"
        "Then either fix the regression the demonstration caught, or — if the behaviour changed on purpose "
        "— update the demonstration in the same change that changed it, so the reproducer still describes "
        "something true.\n\n"
        "This Issue is the ONLY one this workflow keeps open. While it stays red, each night updates this "
        "body with the current failure set rather than filing another; the night it goes green, this closes "
        "itself.\n\n"
        f"The failing output, as the demonstrations printed it:\n\n{tail}")
    references = [("the nightly run that reported this", run_url)] if run_url else None
    return (issue_author.render_engine_issue_body(
        what_this_is=what, whats_next=whats_next, references=references, kind=KIND)
        + "\n" + MARKER + "\n")


def report(result: dict, issues_api, repository: str, run_url: str | None = None) -> dict:
    """Apply the singular-Issue rules. Returns what was done, for the workflow's step summary."""
    open_report = find_report(issues_api.list_open_engine_issues())
    if result.get("ok"):
        if open_report:
            issues_api.close_issue(open_report["number"])
            return {"action": "closed", "issue": open_report["number"]}
        return {"action": "none"}
    body = render(result, repository, run_url)
    if open_report:
        issues_api.update_issue(open_report["number"], body)
        return {"action": "updated", "issue": open_report["number"]}
    issues_api.ensure_label()
    opened = issues_api.open_issue(f"{KIND}: {TITLE}", body)
    return {"action": "filed", "issue": opened.get("number")}


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nightly_demo_report.py",
                                     description="file, update or close the one nightly demo Issue")
    parser.add_argument("--result", required=True, help="demonstration_corpus.py's JSON result — the ONLY input")
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--run-url", default=None)
    parser.add_argument("--dry-run", action="store_true", help="render and decide, but call no API")
    args = parser.parse_args(argv)
    result = json.loads(Path(args.result).read_text(encoding="utf-8"))
    if not args.repository:
        print("nightly-demo-report: no repository to report to", file=sys.stderr)
        return 2
    if args.dry_run:
        print(json.dumps({"would_file": not result.get("ok"),
                          "failures": len(result.get("failures") or [])}, indent=2))
        return 0
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("nightly-demo-report: no token, so nothing was reported", file=sys.stderr)
        return 2
    outcome = report(result, telemetry.GitHubIssues(args.repository, token), args.repository, args.run_url)
    print(json.dumps(outcome, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
