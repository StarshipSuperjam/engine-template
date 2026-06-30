#!/usr/bin/env python3
"""Protection-detection guard (stage-0 seed).

Reads the EVALUATED per-branch rules for the protected branch and fails loud
until the protected-branch ruleset AND its required-check bindings are actually
in force. The evaluated-rules endpoint omits rules left in 'evaluate' or
'disabled' mode, so a ruleset that protects the branch but does not actually
bite reads as absent here — "is protection on?" is answered by what bites, not
by configuration (control-plane bootstrap contract; stage-0-harness §5).

Runs as a `custom/script` check rule in the CI suite (re-homed in core slice 5a),
so an unprotected branch turns engine-ci red. It emits finding.v1 JSON on stdout
(the custom/script machine channel): a hard finding when the gate is not in force,
and a soft "not checked here" note when no token is available (locally — fail open;
the CI run, which has a token, performs the real check). The default GITHUB_TOKEN
(Metadata: read) can read this endpoint; it never reads the admin-gated
ruleset-configuration endpoints.

Superseded by the control-plane bootstrap guard once that module lands.
"""
from __future__ import annotations
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # the sibling tools dir, for github_client
from github_client import get_json  # noqa: E402 — sibling import after the path insert

# Frozen required-check names this guard expects the ruleset to bind. These are
# the literal job names of the seed's two required checks; renaming either one,
# anywhere, is a guardrail-weakening change.
REQUIRED_CHECKS = ["engine-ci", "engine-guard"]

UA = "engine-seed-protection-guard"  # this guard's GitHub API User-Agent; boot reuses it for the same protected-branch probe


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


def emit(findings: list) -> int:
    """Write the finding.v1 array to stdout (the custom/script machine channel) and return
    0 — a successful evaluation, whatever it found. Each finding carries its own severity;
    the dispatcher's custom/script kind decides where the teeth land. Human-readable prose
    lives inside each finding's `message`, so stdout stays pure JSON."""
    print(json.dumps(findings))
    return 0


def main() -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    branch = os.environ.get("PROTECTED_BRANCH", "main")
    token = os.environ.get("GITHUB_TOKEN", "")
    tier = os.environ.get("ENGINE_RULE_TIER", "hard")  # the rule's tier, passed by the kind
    if not repo or not token:
        # Local / no credentials: FAIL OPEN with a soft note — a soft finding never blocks,
        # and the CI run (which has a token) performs the real check. Mirrors the presence
        # kind's fail-open-locally posture; never a false local block.
        return emit([{"severity": "soft", "location": None,
                      "message": "Branch protection was not checked here — no repository "
                      "access token is available, which is normal on your own machine. The "
                      "check that can actually block a bad merge runs in CI."}])
    try:
        rules = get_json(f"/repos/{repo}/rules/branches/{branch}", token, user_agent=UA)
    except Exception as e:  # token present but the API could not be read -> fail closed in CI
        return emit([{"severity": tier, "location": None,
                      "message": f"Branch protection could not be verified for '{branch}' "
                      f"({e}); treating it as not in force until confirmed."}])
    missing = missing_floor(rules, REQUIRED_CHECKS)
    if missing:
        return emit([{"severity": tier, "location": None,
                      "message": f"The protected-branch safety gate on '{branch}' is not fully "
                      "in force: " + "; ".join(missing) + ". Until this is on, an unreviewed "
                      "change could reach the protected branch. Apply the ruleset using the "
                      "setup recipe you were handed, then re-run."}])
    return emit([])  # protection is fully in force


if __name__ == "__main__":
    sys.exit(main())
