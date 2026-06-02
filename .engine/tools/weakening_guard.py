#!/usr/bin/env python3
"""Guardrail-weakening classifier (stage-0 seed; re-homed onto custom/script in core slice 5b).

Runs on pull_request_target so its logic is read from the protected base branch
— a pull request cannot tamper with the guard that judges it. It READS THE DIFF
ONLY via the API and NEVER checks out or executes the pull request's head code.
This is an authoring invariant: the trigger grants the privilege, this script
enforces the restraint (it makes no use of the head ref and the workflow checks
out only the base).

It flags a change that removes, renames, or modifies a guardrail file (a CI
workflow, a check rule, an engine tool, or CODEOWNERS). A flagged change blocks
the merge until the operator applies the distinct, deliberate acknowledgment —
the `guardrail-ack` label — after reading, in plain language, what protection
could weaken (control-plane §weakening hard-gate; D-051 / D-134; principles §15).

It now runs as a frozen-named `custom/script` check rule (engine/check/guardrail-weakening),
invoked BY ID from engine-guard.yml (`validate.py --check`), NOT as part of the CI
suite — so its execution stays on the trusted-base pull_request_target workflow and
never moves into the head-checkout engine-ci context (the D-051 isolation). It emits
finding.v1 JSON on stdout (the custom/script machine channel) and returns 0 on a
successful evaluation: an empty array when nothing weakens or the `guardrail-ack`
label is present (the ack is an INPUT to this one guard, D-134); one finding at the
rule's tier (ENGINE_RULE_TIER, passed by the kind) — carrying the plain-language
ack guidance — on an unacknowledged guardrail change; and a fail-closed finding when
the pull-request context cannot be read. An internal crash returns non-zero, which
the custom/script kind turns into a hard fail-closed finding (defense in depth).

Honest bound: in solo the operator holds admin and could bypass the ruleset, so
this makes weakening NON-SILENT and DELIBERATE ("cannot weaken silently"), not
impossible ("cannot weaken at all" needs a distinct team identity).

Superseded by the control-plane weakening guard once that module lands.
"""
from __future__ import annotations
import json
import os
import sys
import urllib.error
import urllib.request

ACK_LABEL = "guardrail-ack"
# Path prefixes whose files enforce the safety gates.
GUARDRAIL_PREFIXES = (".github/workflows/", ".engine/check/", ".engine/tools/")
# Exact-path guardrails: CODEOWNERS (reserved); the tool-runtime lockfiles, which
# define the runtime every guard and the validator execute in (foundation artifacts);
# and the suite declarations, which decide WHICH suite blocks the merge — a loosened
# context there (e.g. CI -> local-nudge) would silently un-gate the CI check, so a
# change to it must be acknowledged like any other guardrail weakening (core slice 4).
GUARDRAIL_EXACT = (".github/CODEOWNERS", ".engine/pyproject.toml", ".engine/uv.lock",
                   ".engine/suites.json")
# A pure addition strengthens; removal/rename/modification/copy can weaken.
# 'copied' is in GitHub's file-status enum — without it, a weakened *copy* of a
# guardrail file would slip through ungated.
WEAKENING_STATUS = {"removed", "renamed", "modified", "changed", "copied"}


def is_guardrail(path: str) -> bool:
    return path.startswith(GUARDRAIL_PREFIXES) or path in GUARDRAIL_EXACT


def flagged_changes(files: list) -> list:
    """Pure classifier: the guardrail files this diff removes, renames, modifies,
    or copies. Returns a list of (status, shown_path)."""
    flagged = []
    for f in files:
        name = f.get("filename", "")
        status = f.get("status", "")
        prev = f.get("previous_filename", "")
        if status in WEAKENING_STATUS and (is_guardrail(name) or (prev and is_guardrail(prev))):
            flagged.append((status, name if not prev else f"{prev} -> {name}"))
    return flagged


def api_get(path: str, token: str):
    req = urllib.request.Request(
        "https://api.github.com" + path,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "engine-seed-weakening-guard",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def emit(findings: list) -> int:
    """Write the finding.v1 array to stdout (the custom/script machine channel) and return
    0 — a successful evaluation, whatever it found. Each finding carries its own severity;
    the dispatcher's custom/script kind decides where the teeth land. Human-readable prose
    — including the deliberate guardrail-ack guidance — lives inside each finding's
    `message`, so stdout stays pure JSON."""
    print(json.dumps(findings))
    return 0


def main() -> int:
    tier = os.environ.get("ENGINE_RULE_TIER", "hard")  # the rule's tier, passed by the kind
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    event_path = os.environ.get("GITHUB_EVENT_PATH", "")
    if not (repo and token and event_path and os.path.exists(event_path)):
        # Fail closed: a required check that cannot read the PR context blocks until it can.
        return emit([{"severity": tier, "location": None,
                      "message": "GUARDRAIL CHECK: could not read the pull request "
                      "context; failing closed."}])
    with open(event_path, encoding="utf-8") as fh:
        event = json.loads(fh.read())
    pr = event.get("pull_request") or {}
    number = pr.get("number")
    labels = {l.get("name") for l in (pr.get("labels") or [])}
    if number is None:
        return emit([{"severity": tier, "location": None,
                      "message": "GUARDRAIL CHECK: no pull request number in the "
                      "event; failing closed."}])
    try:
        files = api_get(f"/repos/{repo}/pulls/{number}/files?per_page=100", token)
    except Exception as e:  # fail closed — never wave a change through unjudged
        return emit([{"severity": tier, "location": None,
                      "message": f"GUARDRAIL CHECK: could not read the changed files "
                      f"({e}); failing closed."}])

    flagged = flagged_changes(files)
    if not flagged:
        return emit([])  # no guardrail-weakening change
    if ACK_LABEL in labels:
        return emit([])  # acknowledged via the label -> cleared (the ack is an INPUT here, D-134)

    listing = "\n".join(f"  - {status}: {shown}" for status, shown in flagged)
    return emit([{"severity": tier, "location": None,
                  "message": "GUARDRAIL CHANGE DETECTED — this pull request changes "
                  "files that enforce your safety gates:\n" + listing + "\n\n"
                  "If merged unwatched, a safety check could be turned off, renamed, or "
                  "loosened — letting future changes reach the protected branch without "
                  "being checked.\n"
                  f"To approve this deliberately, apply the `{ACK_LABEL}` label to this "
                  "pull request (one deliberate action, distinct from the merge click). "
                  "Until then, this check blocks the merge."}])


if __name__ == "__main__":
    sys.exit(main())
