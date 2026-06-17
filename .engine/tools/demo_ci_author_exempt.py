#!/usr/bin/env python3
"""Operator-runnable demo: how Dependabot's own pull requests pass the PR-body completeness
check as a DISCLOSED not-applicable pass — while everyone else is still held to the eight-section
body, and the `guardrail-ack` gate still bites the locked-dependency change.

Run: uv run --directory .engine -- python tools/demo_ci_author_exempt.py

This exercises the REAL logic — it does not re-implement it: the REAL validator (`validate.run`
over the REAL committed pr-body-completeness rule, reading the author from a REAL GITHUB_EVENT_PATH
event file exactly as CI does) and the REAL guardrail-weakening classifier. Read the three blocks by
eye: the SAME empty body PASSES for Dependabot (with the reason stated plainly) and FAILS for a human
author, and the guard flags the lockfile change no matter who authored it. The disclosure line you
see under each PASS is the actual text the validator emits — not a paraphrase typed into this demo."""
from __future__ import annotations
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402
import weakening_guard   # noqa: E402

RULE = validate.load_json(os.path.join(validate.CHECK_DIR, "pr-body-completeness.json"))


def _run_ci_body_check_as(author: str) -> int:
    """Run the REAL CI suite over JUST the real pr-body-completeness rule, with an EMPTY PR body,
    reading `author` from a real event file the way CI does. Returns the validator's exit code and
    lets it print its own operator report (including the real disclosure line)."""
    event = {"pull_request": {"user": {"login": author}, "body": ""}}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(event, fh)
        event_path = fh.name
    saved_env = os.environ.get("GITHUB_EVENT_PATH")
    saved_loader = validate.load_rules
    try:
        os.environ["GITHUB_EVENT_PATH"] = event_path
        validate.load_rules = lambda: [RULE]                 # focus the output on the body check
        ctx = {"pr_body": validate.get_pr_body(None),        # the REAL trusted-context reads
               "pr_author": validate.get_pr_author()}
        return validate.run("CI", ctx)
    finally:
        validate.load_rules = saved_loader
        if saved_env is None:
            os.environ.pop("GITHUB_EVENT_PATH", None)
        else:
            os.environ["GITHUB_EVENT_PATH"] = saved_env
        os.unlink(event_path)


def main() -> int:
    print("=" * 78)
    print("Dependabot's own pull requests and the eight-section PR-body check")
    print("The rule file declares:  ci_author_exempt =", RULE.get("ci_author_exempt"))
    print("=" * 78)

    print("\n[1] A Dependabot pull request with an EMPTY body (its native changelog body is not")
    print("    the eight-section template). Expect: PASS, with the reason stated plainly.")
    print("-" * 78)
    rc_bot = _run_ci_body_check_as("dependabot[bot]")
    print(f"\n   -> validator exit code: {rc_bot}   (0 = the merge gate is NOT blocked)")

    print("\n[2] The SAME empty body, but authored by a person ('a-human-maintainer').")
    print("    Expect: FAIL — a human must fill the eight sections.")
    print("-" * 78)
    rc_human = _run_ci_body_check_as("a-human-maintainer")
    print(f"\n   -> validator exit code: {rc_human}   (1 = the merge gate IS blocked)")

    print("\n[3] The guardrail-ack gate is UNCHANGED. A Dependabot pull request that changes the")
    print("    locked dependency file is still flagged — the author makes no difference here.")
    print("-" * 78)
    flagged = weakening_guard.flagged_changes(
        [{"filename": ".engine/uv.lock", "status": "modified"}])
    print(f"   guardrail-weakening classifier on a dependabot[bot] uv.lock change: {flagged}")
    print("   -> not empty: the maintainer must still apply the `guardrail-ack` label to merge.")

    ok = (rc_bot == 0 and rc_human == 1 and bool(flagged))
    print("\n" + "=" * 78)
    print("In plain words: the body check is GREEN for Dependabot because it does NOT apply to")
    print("Dependabot's own updates — it was not verified, not 'checked and passed'. Every other")
    print("check still runs, and the lockfile change is still gated by the label you apply.")
    print("DEMO OK" if ok else "DEMO FAILED — unexpected outcome")
    print("=" * 78)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
