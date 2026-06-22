#!/usr/bin/env python3
"""Read the in-flight git/GitHub work record — the "work in hand" attention ranks at orientation
(engine-template #37, the work-record-reader slice; PR 1 of the chain to the gitignored knowledge boot-slice
producer). Builder B, post-M1.

attention's ranking reads the native git/GitHub work record (attention/README "Work prioritization":
"the native git/GitHub record — in-flight branches and pull requests, open Issues as deferrals/backlog,
Milestones as the plan"). This module supplies the IN-FLIGHT half — open pull requests and the current
working branch — as `in_flight` candidates. Open Issues (backlog) and Milestones (the plan) are a later
slice; this reads only the work actually in flight, which is also the "work in hand" the #37 knowledge focus
keys on. Membership is the recorded build-spec leaf (D-052/D-113): in-flight = the repo's open pull requests
(capped) + the current working branch (HEAD, when it is not the default branch), a PR for the current branch
SUBSUMING its branch record (no double-listing). Deliberately NOT "every local branch" — a worktree repo
accrues many stale `claude/*` branches that are not in-flight work.

Two layers, so it answers offline and degrades to git-native (principle §5):
  - the LOCAL-GIT FLOOR (no network, no token): the current branch (HEAD) when not the default — the one
    piece of in-flight work always knowable from the tracked repo alone;
  - the GITHUB LAYER (best-effort, when a reader is injected): the repo's open pull requests, capped. A
    GitHub read FAILURE falls back to the floor rather than failing (the design's "local git stands in for
    the live register") — so a failed PR read degrades WITHIN the git substrate, not to a crash.

Pure leaf — imports only the standard library and takes injected seams (a `run` for git, a duck-typed `gh`
reader: `gh.repo` + `gh._transport(method, path, body) -> (status, json)`, the seam telemetry.GitHubIssues
exposes). It imports neither attention nor boot, so attention imports IT with no cycle (mirrors
standing_situation.py). It performs NO writes.

Availability vs emptiness (the degraded-input contract): `read_in_flight` RAISES WorkRecordUnavailable only
when the work record cannot be consulted AT ALL — git is not runnable here AND no GitHub read succeeded. A
successful consult that simply finds no in-flight work returns [] — git is AVAILABLE, there is just nothing
in flight (attention records `git` as available, not degraded; boot shows "Nothing is blocking").

Run the demo: uv run --directory .engine -- python tools/work_record.py demo
"""
from __future__ import annotations
import datetime
import subprocess

# How many open PRs to read before stopping — a bound so a busy public repo never hangs orientation.
_PR_WINDOW = 20
# How many in-flight records to surface in all (PRs + the working branch), freshest first.
_CAP = 12
# How many in-flight changed paths to surface — a bound so a huge branch never floods focus derivation.
_PATHS_CAP = 50


class WorkRecordUnavailable(Exception):
    """Raised when the in-flight work record cannot be consulted at all — git is not runnable here AND no
    GitHub read succeeded. attention catches it and records `git` in degraded_inputs (boot says so loudly);
    it is NEVER swallowed as "no in-flight work", which would read as a confident-but-blind all-clear."""


def _run_git(args: list[str]) -> str | None:
    """Run a local read-only git command; stripped stdout, or None on any failure (missing binary, not a
    repo, non-zero exit, timeout). Never raises — the floor degrades rather than stranding orientation."""
    try:
        out = subprocess.run(["git", *args], capture_output=True, text=True, timeout=10, check=False)
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:  # noqa: BLE001 — missing git / OS error / timeout all degrade to "floor unavailable"
        return None


def _z(ts: str | None) -> str | None:
    """Normalise a git/GitHub timestamp to a trailing-Z UTC moment, or None when absent/unparseable.
    Defensive: a malformed recency must never reach the ranking math (attention_rank._epoch would raise) —
    omit it instead, so the candidate scores 0 on recency rather than crashing the whole ranking."""
    if not ts:
        return None
    try:
        dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_branch(run) -> str:
    """The repo's default branch short name (from origin/HEAD), falling back to 'main' when it can't be read."""
    head = run(["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"])  # e.g. "refs/remotes/origin/main"
    return head.rsplit("/", 1)[-1] if head else "main"


def _current_branch(run) -> tuple[str, str | None] | None:
    """The current branch as (name, recency) when it is a real branch other than the default; else None
    (detached HEAD, or sitting on the default branch — no in-flight branch to surface from the floor)."""
    name = run(["rev-parse", "--abbrev-ref", "HEAD"])
    if not name or name == "HEAD":             # no branch / detached -> no floor branch record
        return None
    if name == _default_branch(run):           # on the default branch -> not in-flight work
        return None
    return name, _z(run(["log", "-1", "--format=%cI"]))  # the tip commit's committer date, ISO-8601 strict


def _open_prs(gh, *, window: int) -> list[tuple]:
    """The repo's open pull requests via the injected reader, as (number, title, updated_at, head_ref) tuples.
    Raises WorkRecordUnavailable on a read failure (HTTP >= 400 / non-list body) — never read as "no PRs"."""
    status, data = gh._transport(
        "GET", f"/repos/{gh.repo}/pulls?state=open&sort=updated&direction=desc&per_page={window}", None)
    if status >= 400 or not isinstance(data, list):
        raise WorkRecordUnavailable(f"GitHub returned {status} listing open pull requests")
    out: list[tuple] = []
    for pr in data:
        if not isinstance(pr, dict) or not isinstance(pr.get("number"), int):
            continue
        head = pr.get("head")
        head_ref = head.get("ref") if isinstance(head, dict) else None
        out.append((pr["number"], (pr.get("title") or "").strip(), pr.get("updated_at"), head_ref))
    return out


def read_in_flight(gh=None, *, run=_run_git, window: int = _PR_WINDOW, cap: int = _CAP) -> list[dict]:
    """The in-flight work record as `in_flight` attention candidates, freshest first, capped.

    `gh` is the GitHub reader (None -> the local-git floor only, the offline path). `run` is the git runner
    (injected for tests). Each record: {"id": "pr:<n>"|"branch:<name>", "category": "in_flight",
    "recency": <UTC-Z|None>, "title": <str>, "source": "git"}.

    Degradation: returns [] when the record was consulted but holds no in-flight work (git AVAILABLE). A
    GitHub read failure falls back to the floor (git stays available — "local git stands in"). RAISES
    WorkRecordUnavailable only when nothing could be consulted (git unrunnable AND no gh / the gh read failed
    AND no floor)."""
    floor_ok = run(["rev-parse", "--is-inside-work-tree"]) == "true"
    current = _current_branch(run) if floor_ok else None

    records: list[dict] = []
    pr_head_refs: set = set()
    gh_ok = False
    if gh is not None:
        try:
            for num, title, updated, head in _open_prs(gh, window=window):
                if head:
                    pr_head_refs.add(head)
                records.append({"id": f"pr:{num}", "category": "in_flight",
                                "recency": _z(updated), "title": title or f"#{num}", "source": "git"})
            gh_ok = True
        except WorkRecordUnavailable:
            gh_ok = False  # GitHub unreadable -> fall back to the local-git floor (degrade WITHIN git)

    # The working branch — unless an open PR already represents it (no double-listing).
    if current is not None and current[0] not in pr_head_refs:
        records.append({"id": f"branch:{current[0]}", "category": "in_flight",
                        "recency": current[1], "title": current[0], "source": "git"})

    if not floor_ok and not gh_ok:
        raise WorkRecordUnavailable("git is not runnable here and no GitHub work-record read succeeded")

    # Freshest first (a missing recency sorts last), then bounded so orientation never floods.
    records.sort(key=lambda r: (r["recency"] is not None, r["recency"] or ""), reverse=True)
    return records[:cap]


def changed_paths(*, run=_run_git, cap: int = _PATHS_CAP) -> list[str]:
    """The repo-relative paths the current branch's in-flight work touches — the "work in hand" the focused
    knowledge read keys on (engine-template #37, PR 2). The union of:
      - the COMMITTED diff vs the default branch, but ONLY on a real non-default branch (so the default branch
        itself contributes no committed diff): `git diff --name-only <default>...HEAD` (the three-dot form
        diffs against the merge-base of the two — "what this branch added");
      - the UNCOMMITTED working-tree diff and the STAGED diff (always — local edits are in-flight work in
        hand even on the default branch).
    Each leg is independently fail-open (a None from `run` contributes nothing); the union is deduped, sorted,
    and bounded. Returns [] on a clean default branch, a detached HEAD, or outside a repo. A PURE stdlib leaf —
    imports no knowledge_query, so attention (which maps these paths to graph entities) imports IT with no
    cycle; the path -> entity mapping is attention's job, not this reader's."""
    paths: set = set()
    if _current_branch(run) is not None:        # a real branch other than the default -> committed leg applies
        base = _default_branch(run)
        committed = run(["diff", "--name-only", f"{base}...HEAD"])
        if committed:
            paths.update(committed.splitlines())
    for leg in (["diff", "--name-only", "HEAD"], ["diff", "--name-only", "--cached"]):
        out = run(leg)
        if out:
            paths.update(out.splitlines())
    return sorted(p for p in paths if p)[:cap]


# ---- operator-runnable demo (real read_in_flight + the real boot rendering; only git/network faked) ------

def _fake_run(*, current="claude/my-feature", default="main", tip="2026-06-19T10:00:00Z", in_repo=True):
    """A fake git runner answering the calls _current_branch / read_in_flight make — lets the demo run the
    REAL reader fully offline ([[demo-must-exercise-real-logic]]); only the git subprocess is faked."""
    def run(args):
        if args[:2] == ["rev-parse", "--is-inside-work-tree"]:
            return "true" if in_repo else None
        if args[:1] == ["symbolic-ref"]:
            return f"refs/remotes/origin/{default}"
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return current
        if args[:1] == ["log"]:
            return tip
        return None
    return run


def _gh(transport, *, repo="your-org/your-project"):
    """A duck-typed GitHub reader (just .repo + ._transport) over a canned transport — only the network is faked."""
    from types import SimpleNamespace
    return SimpleNamespace(repo=repo, _transport=transport)


def _canned_prs(*prs):
    """A transport answering the open-PRs GET from canned PR objects; everything else 404s."""
    def t(method, path, body):
        if "/pulls" in path:
            return 200, list(prs)
        return 404, None
    return t


def _fail_prs(method, path, body):
    """A transport that fails the PR read (auth error), to show the fall-back to the local-git floor."""
    return 403, None


def _demo() -> int:
    import boot  # lazy: boot has no top-level dependence on this module, so this avoids any load-time cycle

    def show(title, records):
        print(title)
        if not records:
            print("   (nothing in flight — boot shows \"Nothing is blocking right now.\")")
        for r in records:
            print(f"   • {boot._resolve_member(r['id'], None)}")
        print()

    print("What attention reads as your IN-FLIGHT work, and how boot renders it — derived live each session:\n")

    # (1) Online: open PRs + the current branch (which has no PR yet), freshest first.
    gh1 = _gh(_canned_prs(
        {"number": 161, "title": "Wire the work-record reader", "updated_at": "2026-06-19T12:00:00Z",
         "head": {"ref": "claude/work-record"}},
        {"number": 158, "title": "Polish the landing", "updated_at": "2026-06-17T09:00:00Z",
         "head": {"ref": "claude/landing"}}))
    r1 = read_in_flight(gh1, run=_fake_run(current="claude/my-feature"))
    show("1) Online — your open pull requests plus the branch you're on:", r1)

    # (2) The current branch already HAS an open PR -> the PR subsumes it (no double-listing).
    gh2 = _gh(_canned_prs(
        {"number": 161, "title": "Wire the work-record reader", "updated_at": "2026-06-19T12:00:00Z",
         "head": {"ref": "claude/work-record"}}))
    r2 = read_in_flight(gh2, run=_fake_run(current="claude/work-record"))
    show("2) On a branch that already has an open PR — the PR represents it, the branch isn't listed twice:", r2)

    # (3) Offline floor: no GitHub reader -> just the working branch, from the tracked repo alone.
    r3 = read_in_flight(None, run=_fake_run(current="claude/my-feature"))
    show("3) Offline (no GitHub token) — the local-git floor still shows the branch you're on:", r3)

    # (4) GitHub read FAILS but git works -> degrade to the floor, never a crash.
    gh4 = _gh(_fail_prs)
    r4 = read_in_flight(gh4, run=_fake_run(current="claude/my-feature"))
    show("4) GitHub unreachable / auth expired — it degrades to the local-git floor rather than failing:", r4)

    print("No real GitHub call was made and nothing was written. When GitHub can't be read at all and git")
    print("isn't runnable either, the reader reports `git` as a degraded input and boot says so plainly.")
    # Self-check: online reads the PRs; a branch already covered by a PR isn't double-listed; and both the
    # offline and the GitHub-failure cases still surface the working branch (the local-git floor).
    ok = len(r1) >= 2 and len(r2) == 1 and bool(r3) and bool(r4)
    if not ok:
        print("\nDEMO UNEXPECTED: in-flight reading, the no-double-list rule, or the local-git floor did not "
              "behave as expected.", file=sys.stderr)
        return 1
    return 0


def main(argv: list | None = None) -> int:
    import sys
    argv = list(argv if argv is not None else sys.argv[1:])
    if argv and argv[0] == "demo":
        return _demo()
    print("usage: work_record.py demo")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
