#!/usr/bin/env python3
"""engine-ack-status — bind the operator's guardrail acknowledgment to the exact pull-request head.

The guardrail-weakening guard (`weakening_guard.py` / `engine-guard`) no longer clears a killswitch finding
on the mere PRESENCE of the `guardrail-ack` label — a label survives a rebase/force-push and would replay a
stale acknowledgment onto a new head (StarshipSuperjam/engine-template#710; the witnessed replay was
PR StarshipSuperjam/engine-template#457). Instead the guard
reads a head-bound GitHub commit STATUS (context `engine-ack`, state `success`) pinned to the pull request's
head SHA. This companion is what POSTS that status — and only when the operator deliberately applies the
label — so the acknowledgment is bound to the exact version they reviewed.

Two events, one job:
  - `labeled` with `label.name == guardrail-ack`: POST `engine-ack=success` to the CURRENT head SHA (the
    head at label time, read from the event). A later push produces a new head with no such status, so the
    guard re-blocks — the operator re-applies the single label to acknowledge the new head.
  - `synchronize` (a new commit was pushed): REMOVE the `guardrail-ack` label. This is UX ONLY — it makes
    re-consent the same one-step "apply the label" gesture instead of a "remove-and-re-add". Correctness does
    NOT depend on it: the guard already re-blocks a new head because the head-bound status is absent there,
    whether or not the stale label was cleared. A 404 (no label present — the common case) is success.

SECURITY — the pwn-request restraint. This runs on `pull_request_target`, which hands a WRITE-capable token
(`statuses: write`, `pull-requests: write`) to a job a pull-request author can influence. It is safe only
because it NEVER checks out or executes the pull request's head code: it reads the event JSON from
`$GITHUB_EVENT_PATH` and the token from the environment, and makes exactly two GitHub API calls. It imports
no head-controlled input into any privileged call. The workflow checks out only the base ref (the default
for `pull_request_target`) and runs this tool from the base tree. Do not add a head checkout, and do not
interpolate any `github.event.pull_request.head.*` value into a shell line — either would turn this token
into remote-code-execution-with-write.

It is deliberately import-light — stdlib + `github_client` (the shared authenticated transport) + the
`ACK_LABEL` / `ACK_CONTEXT` constants single-homed in `weakening_guard`, all stdlib-only — so its workflow
runs WITHOUT the `uv sync` the guard and CI pay, and reliably posts the status before the guard reads it
(the labeled-event race resolves in the safe direction — a lost race is a transient BLOCK the guard's bounded
retry and a re-run clear, never a false clear). The `engine-ack` status is an INPUT the guard reads; it must
never be elevated to a required branch-ruleset check (a label-satisfiable required gate would defeat itself).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # the sibling tools dir, for the imports below
import github_client  # noqa: E402 — sibling import after the path insert
from weakening_guard import ACK_CONTEXT, ACK_LABEL  # noqa: E402 — the ack contract, single-homed there

USER_AGENT = "engine-ack-status"


def _load_event() -> dict:
    """The GitHub event payload (read-only). Returns {} if it cannot be read — main() then no-ops safely."""
    path = os.environ.get("GITHUB_EVENT_PATH", "")
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def post_ack_status(repo: str, head_sha: str, token: str, number) -> int:
    """POST the head-bound acknowledgment marker: `engine-ack=success` on `head_sha`. Returns the HTTP
    status. The description names the pull request, since a commit status is keyed per-SHA (two pull requests
    sharing a head SHA share the marker — acceptable, as a shared SHA is shared content)."""
    desc = f"guardrail-ack acknowledged for this head (#{number})"[:140]
    status, _ = github_client.json_request(
        "POST", f"/repos/{repo}/statuses/{head_sha}", token, user_agent=USER_AGENT,
        body={"context": ACK_CONTEXT, "state": "success", "description": desc})
    return status


def remove_ack_label(repo: str, number, token: str) -> int:
    """Best-effort removal of the `guardrail-ack` label (UX only). Returns the HTTP status; a 404 (no label
    attached — the common case on a push) is treated as success by main()."""
    status, _ = github_client.json_request(
        "DELETE", f"/repos/{repo}/issues/{number}/labels/{ACK_LABEL}", token, user_agent=USER_AGENT)
    return status


def main() -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    event = _load_event()
    action = event.get("action") or ""
    pr = event.get("pull_request") or {}
    number = pr.get("number")
    head_sha = ((pr.get("head") or {}).get("sha")) or ""

    if not (repo and token) or number is None:
        print("ack-status: no pull request context in the event — nothing to do.")
        return 0

    try:
        if action == "labeled":
            label_name = (event.get("label") or {}).get("name")
            if label_name != ACK_LABEL:
                print(f"ack-status: label '{label_name}' is not the acknowledgment label — nothing to do.")
                return 0
            if not head_sha:
                print("ack-status: no head commit in the event — cannot bind the acknowledgment.",
                      file=sys.stderr)
                return 1
            status = post_ack_status(repo, head_sha, token, number)
            if status >= 400:
                print(f"ack-status: GitHub returned {status} posting the acknowledgment status on "
                      f"{head_sha} — the guard will fail closed until it is posted.", file=sys.stderr)
                return 1
            print(f"ack-status: posted engine-ack=success on {head_sha} for #{number}.")
            return 0

        if action == "synchronize":
            # UX-only: clear the now-stale label so re-consent is a one-step re-apply. Correctness rests on
            # the guard's head-bound status read, not on this removal.
            status = remove_ack_label(repo, number, token)
            if status == 404:
                print(f"ack-status: no acknowledgment label on #{number} to clear after the push.")
                return 0
            if status >= 400:
                print(f"ack-status: GitHub returned {status} clearing the stale label on #{number} "
                      "(UX only — the guard is unaffected).", file=sys.stderr)
                return 1
            print(f"ack-status: cleared the stale acknowledgment label on #{number} after the push.")
            return 0

        print(f"ack-status: action '{action}' needs no acknowledgment handling — nothing to do.")
        return 0
    except urllib.error.URLError as exc:  # network unreachable — a real, visible failure (non-blocking check)
        print(f"ack-status: GitHub is unreachable ({exc}); the acknowledgment could not be recorded.",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
