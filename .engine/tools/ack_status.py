#!/usr/bin/env python3
"""engine-ack-status — bind the operator's guardrail acknowledgment to the exact pull-request head.

The guardrail-weakening guard (`weakening_guard.py` / `engine-guard`) no longer clears a killswitch finding
on the mere PRESENCE of the `guardrail-ack` label — a label survives a rebase/force-push and would replay a
stale acknowledgment onto a new head (StarshipSuperjam/engine-template#710; the witnessed replay was
PR StarshipSuperjam/engine-template#457). Instead the guard
reads a head-bound GitHub commit STATUS (context `engine-ack`, state `success`) pinned to the pull request's
head SHA. This companion is what POSTS that status — and only when the operator deliberately applies the
label — so the acknowledgment is bound to the exact version they reviewed.

Three actions, one job:
  - `labeled` with `label.name == guardrail-ack`: judge the LABELER'S AUTHORITY from the committed base
    manifest (StarshipSuperjam/engine-template#958), then POST to the CURRENT head SHA (the head at label
    time, read from the event). In TEAM mode a success is posted only when a DISTINCT operator identity — a
    `User` whose login is not the engine's own recorded identity — applied it; the engine's own identity, a
    bot, or a missing sender is REFUSED with `engine-ack=failure` (a fail-safe audit record, not a mechanism
    that "out-recents" a minted success — the check simply never mints one). In SOLO mode the success is
    posted and annotated `[shared credential]` — one-step consent preserved, with the "does not verify who
    applied it" limit disclosed to the operator by the guard. A later push produces a new head with no such
    status, so the guard re-blocks — the operator re-applies the single label to acknowledge the new head.
  - `unlabeled` with `label.name == guardrail-ack`: POST `engine-ack=failure` to the current head. Removing
    the label is a deliberate WITHDRAWAL of consent; a commit status cannot be deleted, so the withdrawal is
    recorded as a non-success posting, which (as the guard reads the most-recent engine-ack entry) re-blocks
    the same head. Without this, a removed label would be silently ignored until the next push.
  - `synchronize` (a new commit was pushed): REMOVE the `guardrail-ack` label. This is UX ONLY — it makes
    re-consent the same one-step "apply the label" gesture instead of a "remove-and-re-add". Correctness does
    NOT depend on it: the guard already re-blocks a new head because the head-bound status is absent there,
    whether or not the stale label was cleared. A 404 (no label present — the common case) is success.

SECURITY — the pwn-request restraint. This runs on `pull_request_target`, which hands a WRITE-capable token
(`statuses: write`, `pull-requests: write`) to a job a pull-request author can influence. It is safe only
because it NEVER checks out or executes the pull request's head code: it reads the event JSON from
`$GITHUB_EVENT_PATH` and the token from the environment, and makes at most one GitHub API call per event. It
imports no head-controlled input into any privileged call. The labeler-authority judgment
(StarshipSuperjam/engine-template#958) reads only the committed BASE manifest (`.engine/engine.json` from the
base checkout, via `protection_guard`) and the event's frozen `sender` object — never head code, and never
`github.actor` (a re-run would attribute that to the re-runner, the documented spoof vector). The workflow
checks out only the base ref (the default for `pull_request_target`) and runs this tool from the base tree.
Do not add a head checkout, and do not interpolate any `github.event.pull_request.head.*` value into a shell
line — either would turn this token into remote-code-execution-with-write.

It is deliberately import-light — stdlib + `github_client` (the shared authenticated transport) + the
`ACK_LABEL` / `ACK_CONTEXT` constants single-homed in `weakening_guard` + `protection_guard` (the
labeler-authority + identity-tier reader). That whole chain is stdlib-only AT IMPORT: `protection_guard`
pulls `repo_identity` -> `validate`, and `validate` binds its `yaml`/`jsonschema`/`tomllib` deps LAZILY
(inside functions), so importing it needs only the standard library — the workflow still
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
import protection_guard  # noqa: E402 — the labeler-authority judgment, single-homed (stdlib-only import chain)
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


def post_ack_status(repo: str, head_sha: str, token: str, number, state: str = "success",
                    detail: str | None = None) -> int:
    """POST the head-bound acknowledgment marker: `engine-ack` at `state` on `head_sha`. `state` is "success"
    when an authorized labeler applies the label and "failure" when it is REMOVED, or when the labeler's
    authority is refused (a commit status cannot be deleted, only overwritten, so a withdrawal/refusal is
    recorded as a non-success posting — the guard reads the MOST RECENT engine-ack entry, so this re-blocks the
    same head). Returns the HTTP status. `detail` is a short, non-secret audit phrase (the acking sender +
    authority marker, or a refusal reason) placed FIRST in the description so the 140-char cap can never drop
    the marker; it is advisory only — the guard keys on the status `creator`, never on this text. The
    description names the pull request, since a commit status is keyed per-SHA (two pull requests sharing a
    head SHA share the marker — acceptable, as a shared SHA is shared content)."""
    verb = "acknowledged" if state == "success" else "withdrawn"
    core = f"guardrail-ack {verb} (#{number})"
    desc = (f"{detail} — {core}" if detail else core)[:140]
    status, _ = github_client.json_request(
        "POST", f"/repos/{repo}/statuses/{head_sha}", token, user_agent=USER_AGENT,
        body={"context": ACK_CONTEXT, "state": state, "description": desc})
    return status


def remove_ack_label(repo: str, number, token: str) -> int:
    """Best-effort removal of the `guardrail-ack` label (UX only). Returns the HTTP status; a 404 (no label
    attached — the common case on a push) is treated as success by main()."""
    status, _ = github_client.json_request(
        "DELETE", f"/repos/{repo}/issues/{number}/labels/{ACK_LABEL}", token, user_agent=USER_AGENT)
    return status


def _post(repo: str, head_sha: str, token: str, number, state: str, detail: str | None) -> int:
    """POST engine-ack=`state` and translate the HTTP result into a process return code: 0 when it lands, 1 on
    a GitHub write failure (the guard then fails closed until the status is posted). Shared by the accept,
    refuse, and withdrawal paths so the error handling never drifts between them."""
    status = post_ack_status(repo, head_sha, token, number, state, detail)
    if status >= 400:
        print(f"ack-status: GitHub returned {status} posting engine-ack={state} on "
              f"{head_sha} — the guard will fail closed until it is posted.", file=sys.stderr)
        return 1
    print(f"ack-status: posted engine-ack={state} on {head_sha} for #{number}.")
    return 0


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
        if action in ("labeled", "unlabeled"):
            # Applying the ack label posts engine-ack=success ONLY when the labeler's authority is confirmed
            # (see below); REMOVING it posts engine-ack=failure, so a deliberate withdrawal re-blocks the same
            # head (a commit status cannot be deleted, and the guard keys on the head-bound status, not on the
            # label's live presence).
            label_name = (event.get("label") or {}).get("name")
            if label_name != ACK_LABEL:
                print(f"ack-status: label '{label_name}' is not the acknowledgment label — nothing to do.")
                return 0
            if not head_sha:
                print("ack-status: no head commit in the event — cannot bind the acknowledgment.",
                      file=sys.stderr)
                return 1

            if action == "unlabeled":
                # WITHDRAWAL is authority-free by design: removing consent, by ANY actor, must re-block —
                # fail-safe. An agent can DoS-withdraw a legitimate acknowledgment, but can never forge one.
                return _post(repo, head_sha, token, number, "failure", None)

            # action == "labeled": judge the LABELER'S AUTHORITY from the committed base manifest before
            # minting a success (StarshipSuperjam/engine-template#958). The decision lives ENTIRELY here in the
            # writer, where the event's `sender` is observable; the reader (weakening_guard) only verifies the
            # status came from the trusted bot and never re-judges the labeler. `sender` is the FROZEN event
            # actor (the labeler), NEVER `github.actor` — a re-run would attribute that to the re-runner.
            sender = event.get("sender") or {}
            decision, detail = protection_guard.resolve_labeler_authority(
                sender.get("login"), sender.get("type"))
            if decision == protection_guard.AUTH_REFUSE:
                # Record the refusal as a fail-safe audit posting (engine-ack=failure, by the trusted bot, so
                # the guard honors it and re-blocks). Posting the failure IS the intended outcome here, not an
                # error — only a GitHub write failure is an error.
                rc = _post(repo, head_sha, token, number, "failure", f"refused: {detail}")
                if rc == 0:
                    print(f"ack-status: REFUSED the acknowledgment on {head_sha} for #{number} — {detail}.")
                return rc
            return _post(repo, head_sha, token, number, "success", detail)

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
