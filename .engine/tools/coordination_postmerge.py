#!/usr/bin/env python3
"""coordination_postmerge — the DETERMINISTIC merge-reaction fan-out (StarshipSuperjam/engine-template#939, eADR-0043).

WHY A WORKFLOW, NOT A VERB. Three advisory signals are reactions to a *merge*: the base advanced, so open
candidates' greens may be stale (revalidation); a merge touched a peer's declared surface (dependency-update);
and the integration slot just freed, so the next reviewed candidate is up (next-in-queue). The base advances
the instant the merge lands — nothing engine-side runs on the operator's merge click unless a workflow fires.
Hanging these on a human remembering to run `integration_queue advance` would make them non-deterministic (and
by the documented "merge, then advance" flow the merged pull request is already closed by then, so a merge
could never even be observed from that branch). So they ride the merge EVENT: engine-coordination-postmerge.yml
runs `on: pull_request: [closed]`, gated `merged == true`, and invokes this driver once per merge.

WHAT IT DOES, given the merged pull request and the new protected head SHA:
  - revalidation/base-advanced  -> every OTHER open candidate (its green may be stale).
  - dependency-update/merged    -> each open candidate whose change domain OVERLAPS the merged one.
  - integration/next-in-queue   -> the next reviewed candidate (the slot just freed).
It reuses the coordination_emitters fan-out unchanged; only the TRIGGER moved here from the `advance` verb.

CONFINEMENT + NO-HARM. The transport is wrapped in `coordination_board.comment_only` at the boundary, so every
GitHub call this driver makes — its own reads and the emitters' comment writes — is confined to reads + the two
comment-write shapes (eADR-0043 law 3); a merge/label/status/body call cannot be reached. Every step is
best-effort and swallowed: a coordination failure NEVER reddens the post-merge run — the merge already
happened, and this is a pure advisory side-channel. Re-running for the same merge re-posts nothing (the board
dedupes on the fingerprint, and revalidation/dependency carry the observed base SHA). INERT ON SOLO: with no
other open candidate the open list is empty and nothing is posted.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import coordination_board as board  # noqa: E402
import coordination_emitters as emitters  # noqa: E402


def fan_out(transport, repo: str, merged_pr: int, *, base: str, tier: str, base_sha: str) -> dict:
    """The pure merge-reaction fan-out over an injected transport (tests hand it a fake GitHub). Returns the
    counts posted. `require_peer=False`: the merged pull request has left the open set, so the open list is the
    concurrency gate — empty posts nothing (solo-safe), a single remaining candidate is still notified. Never
    raises; a failing leg contributes 0."""
    # Confine EVERY GitHub call this driver makes (its reads + the emitters' writes) to reads + comment writes.
    guarded = board.comment_only(transport)
    result = {"revalidation": 0, "dependency": 0, "next": None}
    try:
        result["revalidation"] = emitters.emit_revalidation_scan(
            guarded, repo, base_sha=base_sha, exclude_pr=merged_pr, require_peer=False)
    except Exception:  # noqa: BLE001 — advisory; a failing leg never fails the run
        pass
    try:
        result["dependency"] = emitters.emit_dependency_merged_scan(
            guarded, repo, merged_pr, base_sha=base_sha, require_peer=False)
    except Exception:  # noqa: BLE001
        pass
    try:
        import integration_queue
        cands = integration_queue.reviewed_candidates(guarded, repo, base, tier=tier)
        if cands and emitters.emit_integration_next(guarded, repo, cands[0].pr, require_peer=False):
            result["next"] = cands[0].pr
    except Exception:  # noqa: BLE001
        pass
    return result


def _merged_pr_context(transport, repo: str, merged_pr: int) -> "dict | None":
    """Read the merged pull request and confirm it actually MERGED (a backstop under the workflow's own
    `merged == true` gate). Returns {'base': <base ref>, 'merge_sha': <merge commit sha or ''>} or None when
    the pull request is missing or was closed WITHOUT merging (then the driver is a no-op)."""
    status, pr = transport("GET", f"/repos/{repo}/pulls/{merged_pr}", None)
    if status >= 400 or not isinstance(pr, dict) or not pr.get("merged"):
        return None
    base = ((pr.get("base") or {}).get("ref")) or "main"
    return {"base": base, "merge_sha": pr.get("merge_commit_sha") or ""}


def _summary(repo: str, merged_pr: int, ctx: "dict | None", result: "dict | None") -> str:
    if ctx is None:
        return (f"**Coordination (post-merge).** PR #{merged_pr} on `{repo}` was not a merge (closed without "
                "merging, or unreadable) — no advisory notices posted.")
    nxt = result.get("next") if result else None
    nxt_line = f"next-in-queue -> PR #{nxt}" if nxt else "no next candidate waiting"
    return (f"**Coordination (post-merge) for PR #{merged_pr} on `{repo}`.** Advisory notices posted: "
            f"{result.get('revalidation', 0)} revalidation, {result.get('dependency', 0)} dependency-update; "
            f"{nxt_line}. All advisory (eADR-0043) — each is a prompt to re-verify canonical state, never a "
            "gate. Nothing here merged, labelled, or changed any authority.")


def _arg(argv: list, name: str) -> "str | None":
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


def main(argv: "list | None" = None) -> int:
    """Env + flags: --pr / PR_NUMBER, --merge-sha / MERGE_SHA (the new protected head), repo+token from the
    GitHub Actions environment. Always exits 0 — a coordination hiccup must never redden a merged-PR run."""
    argv = argv if argv is not None else sys.argv[1:]
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    pr_raw = _arg(argv, "--pr") or os.environ.get("PR_NUMBER", "")
    merge_sha = _arg(argv, "--merge-sha") or os.environ.get("MERGE_SHA", "")
    summary_out = _arg(argv, "--summary-out")

    ctx = None
    result = None
    try:
        merged_pr = int(pr_raw)
    except (TypeError, ValueError):
        merged_pr = 0
    if repo and token and merged_pr:
        try:
            import github_client
            import protection_guard
            transport = github_client.reader(repo, token, user_agent=board.USER_AGENT).transport
            ctx = _merged_pr_context(transport, repo, merged_pr)
            if ctx is not None:
                base_sha = merge_sha or ctx["merge_sha"]
                if base_sha:
                    result = fan_out(transport, repo, merged_pr, base=ctx["base"],
                                     tier=protection_guard.resolve_tier(), base_sha=base_sha)
        except Exception:  # noqa: BLE001 — advisory; never fail the post-merge run
            pass

    summary = _summary(repo, merged_pr, ctx, result)
    print(summary)
    if summary_out:
        try:
            with open(summary_out, "w", encoding="utf-8") as fh:
                fh.write(summary + "\n")
        except OSError:
            pass
    print(json.dumps(result or {}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
