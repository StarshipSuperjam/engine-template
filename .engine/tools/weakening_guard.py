#!/usr/bin/env python3
"""Guardrail-weakening classifier (stage-0 seed).

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


def main() -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    event_path = os.environ.get("GITHUB_EVENT_PATH", "")
    if not (repo and token and event_path and os.path.exists(event_path)):
        print("GUARDRAIL CHECK: could not read the pull request context; failing closed.")
        return 1
    with open(event_path, encoding="utf-8") as fh:
        event = json.loads(fh.read())
    pr = event.get("pull_request") or {}
    number = pr.get("number")
    labels = {l.get("name") for l in (pr.get("labels") or [])}
    if number is None:
        print("GUARDRAIL CHECK: no pull request number in the event; failing closed.")
        return 1

    try:
        files = api_get(f"/repos/{repo}/pulls/{number}/files?per_page=100", token)
    except Exception as e:  # fail closed — never wave a change through unjudged
        print(f"GUARDRAIL CHECK: could not read the changed files ({e}); failing closed.")
        return 1

    flagged = flagged_changes(files)

    if not flagged:
        print("GUARDRAIL CHECK: no guardrail-weakening change detected.")
        return 0

    if ACK_LABEL in labels:
        print(f"GUARDRAIL CHANGE acknowledged via the `{ACK_LABEL}` label:")
        for status, shown in flagged:
            print(f"  - {status}: {shown}")
        return 0

    print("GUARDRAIL CHANGE DETECTED — this pull request changes files that "
          "enforce your safety gates:")
    for status, shown in flagged:
        print(f"  - {status}: {shown}")
    print("\nIf merged unwatched, a safety check could be turned off, renamed, or "
          "loosened — letting future changes reach the protected branch without "
          "being checked.\n"
          f"To approve this deliberately, apply the `{ACK_LABEL}` label to this "
          "pull request (one deliberate action, distinct from the merge click). "
          "Until then, this check blocks the merge.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
