#!/usr/bin/env python3
"""One-time backfill — normalise a legacy engine Issue's title kind prefix to the canonical vocabulary.

WHAT THIS IS. Before the kind reconciler existed, engine Issues were filed with prose prefixes that drifted
(`Bug`, `Defect`, `Engine fault`). This bounded, re-runnable tool maps the UNAMBIGUOUS legacy aliases
(issue_kind.ALIASES — Bug/Defect/Engine fault → Fix) to the canonical prefix on OPEN engine-labelled Issues,
leaving ambiguous or already-canonical titles untouched. It never guesses a classification: an ambiguous
historical prefix (`Architecture`, `Memory integrity`) and a bare descriptive title are both left alone.

DRY-RUN BY DEFAULT; --apply --confirm TO WRITE. With no flags (or `--dry-run`) it prints the exact rename plan
and mutates NOTHING (GET-only — it only lists Issues). `--apply --confirm` — BOTH, matching the create path's
confirmation gate — edits each planned title. A mass retitle of live Issues is outward-facing and hard to
reverse, so the two-flag gate is deliberate, weightier than the single-issue write.

RETITLE ONLY, NOT SELF-HEALING (disclosed). This repairs the visible prefix; it does NOT stamp the
`<!-- engine-kind: … -->` marker (that would require re-authoring each body and would mass-fire `edited` across
every touched Issue). A backfilled legacy Issue therefore carries a canonical prefix but is not self-healing —
re-author it through the issue helper (issue_author) to give it a marker. This is a bounded normalisation, not
a migration onto the marker.

CLI:
  uv run --directory .engine -- python tools/issue_kind_backfill.py                     # dry-run: print the plan
  uv run --directory .engine -- python tools/issue_kind_backfill.py --apply --confirm   # write the renames
  uv run --directory .engine -- python tools/issue_kind_backfill.py demo                # scripted, fake GitHub
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import issue_kind          # noqa: E402  (the canonical vocabulary + the single-source alias mapping)
import issue_label_client  # noqa: E402  (the light client whose edit_title writes the renames)

USER_AGENT = "engine-issue-kind-backfill"


def plan_renames(issues: list) -> list:
    """Pure: the (number, old_title, new_title) renames an UNAMBIGUOUS legacy alias prefix implies over the
    given Issues (each a dict with `number` and `title`). Skips a title with no unambiguous alias (already
    canonical, ambiguous, or unprefixed) and any whose rename would be a no-op — never a guess."""
    plan = []
    for issue in issues:
        title = issue.get("title") or ""
        target = issue_kind.alias_target(title)
        if target is None:
            continue
        new_title = issue_kind.render_title(target, title)
        if new_title != title:
            plan.append((issue["number"], title, new_title))
    return plan


def apply_renames(plan: list, client) -> int:
    """Write each planned rename via the client's edit_title, printing each. Returns the count written. Any
    GitHub failure propagates as DegradedWriteError (the caller reports it) — never a silent partial success
    claimed as done."""
    for number, old_title, new_title in plan:
        client.edit_title(number, new_title)
        print(f"  #{number}: {old_title!r} -> {new_title!r}")
    return len(plan)


def _resolve_repo_token() -> "tuple[str | None, str | None]":
    """The target repo + token from trusted config: GITHUB_REPOSITORY (else the git origin slug) and
    GITHUB_TOKEN. A backfill runs against the engine's own Issue list, so the target is the checkout's own
    identity, never an argument."""
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        try:
            import repo_identity  # lazy: only needed at CLI runtime
            repo = repo_identity.origin_slug(None)
        except Exception:  # noqa: BLE001 — no origin resolvable is reported as "cannot reach" below
            repo = None
    return (repo or None), (os.environ.get("GITHUB_TOKEN") or None)


def _print_plan(plan: list, apply: bool) -> None:
    if not plan:
        print("issue-kind-backfill: no legacy alias prefixes to normalise — nothing to do.")
        return
    print(f"issue-kind-backfill: {len(plan)} title(s) with an unambiguous legacy alias prefix "
          f"{'to rewrite' if apply else 'that WOULD be rewritten (dry run — nothing is filed)'}:")
    for number, old_title, new_title in plan:
        print(f"  #{number}: {old_title!r} -> {new_title!r}")


def _run(argv: list) -> int:
    apply = "--apply" in argv
    confirm = "--confirm" in argv
    repo, token = _resolve_repo_token()
    if not repo or not token:
        print("issue-kind-backfill: set GITHUB_REPOSITORY and GITHUB_TOKEN to reach GitHub "
              "(a backfill lists the engine's own open Issues).", file=sys.stderr)
        return 1
    import telemetry  # lazy: the list read only; heavy import kept off module scope
    try:
        issues = telemetry.GitHubIssues(repo, token).list_open_engine_issues()
    except Exception as exc:  # noqa: BLE001 — a degraded read is reported plainly, never a false empty plan
        print(f"issue-kind-backfill: could not list open engine issues — {exc}", file=sys.stderr)
        return 1
    plan = plan_renames(issues)
    _print_plan(plan, apply)
    if not apply:
        if confirm:
            print("(Note: --confirm has no effect without --apply — this is still a dry run.)")
        if plan:
            print("\nThis was a DRY RUN — nothing was filed. Re-run with `--apply --confirm` to write these.")
        return 0
    if not confirm:
        print("Refused — `--apply` rewrites live Issue titles, so it needs `--confirm` too "
              "(run without flags first to see exactly what would change).", file=sys.stderr)
        return 2
    client = issue_label_client.IssueLabelClient(repo, token, user_agent=USER_AGENT)
    try:
        written = apply_renames(plan, client)
    except issue_label_client.DegradedWriteError as exc:
        print(f"issue-kind-backfill: a title rewrite failed — {exc}", file=sys.stderr)
        return 1
    print(f"issue-kind-backfill: rewrote {written} title(s).")
    return 0


# ---- the operator-runnable demo (real logic, fake GitHub) -------------------------------------

class _FakeGitHub:
    """A scripted GitHub for the demo/tests: records every (method, path, body) and serves a canned open-issue
    list, so the REAL plan_renames / apply_renames logic runs with no network."""

    def __init__(self, issues):
        self.calls = []
        self._issues = issues

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if "/issues?" in path:                 # the list read (page 1; fewer than 100 → single page)
            return 200, [{"number": i["number"], "title": i["title"], "body": i.get("body", "")}
                         for i in self._issues]
        return 200, {}                          # a PATCH title write

    def title_edits(self):
        import re
        return [c for c in self.calls if c[0] == "PATCH" and re.search(r"/issues/\d+$", c[1])]


def _demo() -> int:
    """Runs the REAL plan_renames / apply_renames over synthetic Issues against a fake GitHub, printing the
    plan and self-checking. Returns 1 on any unexpected result (the in_tool_demo_failure_path floor's path)."""
    import telemetry
    ok = True

    def check(desc: str, cond: bool) -> None:
        nonlocal ok
        if not cond:
            ok = False
        print(f"  {desc:72} -> {'OK' if cond else 'UNEXPECTED'}")

    issues = [
        {"number": 1, "title": "Bug: the parser drops a token"},          # alias -> Fix
        {"number": 2, "title": "Engine fault: boot shed continuity"},     # alias -> Fix
        {"number": 3, "title": "Defect: off-by-one"},                     # alias -> Fix
        {"number": 4, "title": "Improvement: already canonical"},         # canonical -> skip
        {"number": 5, "title": "Architecture: ambiguous, never guessed"}, # ambiguous -> skip
        {"number": 6, "title": "Migration M3: not a kind"},               # no alias -> skip
    ]
    print("One-time kind backfill — what it would rewrite (real logic, fake GitHub):\n")

    plan = plan_renames(issues)
    check("only the three unambiguous aliases are planned", [n for n, _, _ in plan] == [1, 2, 3])
    check("Bug: -> Fix:", ("Bug: the parser drops a token", "Fix: the parser drops a token")
          in [(o, n) for _, o, n in plan])
    check("ambiguous 'Architecture:' is left untouched", 5 not in [n for n, _, _ in plan])
    check("already-canonical 'Improvement:' is left untouched", 4 not in [n for n, _, _ in plan])

    _print_plan(plan, apply=False)

    # dry run: listing an issue makes only GET calls, never a PATCH
    gh = _FakeGitHub(issues)
    telemetry.GitHubIssues("o/r", "t", transport=gh).list_open_engine_issues()
    check("dry-run path issues no title writes", gh.title_edits() == [])

    # apply: each planned rename is written once
    gh2 = _FakeGitHub(issues)
    client = issue_label_client.IssueLabelClient("o/r", "t", user_agent=USER_AGENT, transport=gh2)
    written = apply_renames(plan, client)
    check("apply writes exactly the planned renames", written == 3 and len(gh2.title_edits()) == 3)
    check("the write carries the canonical title", gh2.title_edits()[0][2] == {"title": "Fix: the parser drops a token"})

    if not ok:
        print("\nDEMO UNEXPECTED: a backfill outcome did not match the contract.", file=sys.stderr)
        return 1
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    return _run(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
