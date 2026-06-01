#!/usr/bin/env python3
"""Protection-detection guard (stage-0 seed).

Reads the EVALUATED per-branch rules for the protected branch and fails loud
until the protected-branch ruleset AND its required-check bindings are actually
in force. The evaluated-rules endpoint omits rules left in 'evaluate' or
'disabled' mode, so a ruleset that protects the branch but does not actually
bite reads as absent here — "is protection on?" is answered by what bites, not
by configuration (control-plane bootstrap contract; stage-0-harness §5).

Runs as a step in the `engine-ci` job, so an unprotected branch turns engine-ci
red. The default GITHUB_TOKEN (Metadata: read) can read this endpoint; it never
reads the admin-gated ruleset-configuration endpoints.

Superseded by the control-plane bootstrap guard once that module lands.
"""
from __future__ import annotations
import json
import os
import sys
import urllib.error
import urllib.request

# Frozen required-check names this guard expects the ruleset to bind. These are
# the literal job names of the seed's two required checks; renaming either one,
# anywhere, is a guardrail-weakening change.
REQUIRED_CHECKS = ["engine-ci", "engine-guard"]


def missing_floor(rules: list, required_checks: list) -> list:
    """Pure evaluation of the protection floor against the EVALUATED per-branch
    rules (which already omit rules in evaluate/disabled mode). Returns the list
    of floor pieces not in force — empty means the gate fully bites."""
    types = {r.get("type") for r in rules}
    bound: set[str] = set()
    pr_thread_resolution = False
    for r in rules:
        p = r.get("parameters") or {}
        if r.get("type") == "required_status_checks":
            for c in p.get("required_status_checks", []):
                if c.get("context"):
                    bound.add(c["context"])
        elif r.get("type") == "pull_request":
            pr_thread_resolution = bool(p.get("required_review_thread_resolution"))

    missing: list[str] = []
    if "pull_request" not in types:
        missing.append("a pull request is not required before merging")
    if "required_status_checks" not in types:
        missing.append("status checks are not required to pass")
    else:
        for name in required_checks:
            if name not in bound:
                missing.append(f"the required check '{name}' is not bound")
    if not pr_thread_resolution:
        missing.append("unresolved review conversations do not block merging")
    if "non_fast_forward" not in types:
        missing.append("force-pushes are not blocked")
    if "deletion" not in types:
        missing.append("branch deletion is not restricted")
    return missing


def api_get(path: str, token: str):
    req = urllib.request.Request(
        "https://api.github.com" + path,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "engine-seed-protection-guard",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    branch = os.environ.get("PROTECTED_BRANCH", "main")
    token = os.environ.get("GITHUB_TOKEN", "")
    if not repo or not token:
        print("SAFETY GATE: UNKNOWN — missing GITHUB_REPOSITORY or GITHUB_TOKEN; "
              "cannot check protection (failing closed).")
        return 1
    try:
        rules = api_get(f"/repos/{repo}/rules/branches/{branch}", token)
    except urllib.error.HTTPError as e:
        print(f"SAFETY GATE: UNKNOWN — could not read the evaluated rules for "
              f"'{branch}' (HTTP {e.code}); failing closed.")
        return 1
    except Exception as e:  # network/parse — fail closed, never assume protected
        print(f"SAFETY GATE: UNKNOWN — could not read the evaluated rules for "
              f"'{branch}' ({e}); failing closed.")
        return 1

    missing = missing_floor(rules, REQUIRED_CHECKS)

    if missing:
        print(f"SAFETY GATE: OFF — the protected-branch ruleset on '{branch}' is "
              "not fully in force.\nWhat is missing:")
        for m in missing:
            print("  - " + m)
        print("\nUntil this is on, an unreviewed change could reach the protected "
              "branch. Apply the ruleset using the setup recipe you were handed, "
              "then re-run this check.")
        return 1
    print(f"SAFETY GATE: ON — '{branch}' requires a pull request and the checks "
          f"{', '.join(REQUIRED_CHECKS)}; unresolved conversations block merging; "
          "force-pushes and deletion are blocked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
