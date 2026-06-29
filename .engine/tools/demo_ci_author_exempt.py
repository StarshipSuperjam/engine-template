#!/usr/bin/env python3
"""Operator-runnable demo: how the engine's own single-purpose pull requests pass the PR-body
completeness check as a DISCLOSED not-applicable pass — Dependabot by its AUTHOR (ci_author_exempt),
and a single-purpose memory-erasure proposal by its LABEL (ci_label_exempt, engine-erasure) — while
everyone else is still held to the eight-section body, and the `guardrail-ack` gate still bites the
locked-dependency change.

Run: uv run --directory .engine -- python tools/demo_ci_author_exempt.py

This exercises the REAL logic — it does not re-implement it: the REAL validator (`validate.run` over
the REAL committed pr-body-completeness rule, reading the author AND the labels from a REAL
GITHUB_EVENT_PATH event file exactly as CI does) and the REAL guardrail-weakening classifier. Read the
blocks by eye: the SAME empty body PASSES for a Dependabot author and for an engine-erasure-labelled
pull request (each with the reason stated plainly) and FAILS for a plain human-authored, unlabelled
pull request, and the guard flags the lockfile change no matter who authored it. The label case is
authored by a NON-exempt human on purpose, so its PASS proves the LABEL waived it, not the author. The
disclosure line you see under each PASS is the actual text the validator emits — not a paraphrase."""
from __future__ import annotations
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402
import weakening_guard   # noqa: E402

RULE = validate.load_json(os.path.join(validate.CHECK_DIR, "pr-body-completeness.json"))


def _run_ci_body_check(event: dict) -> int:
    """Run the REAL CI suite over JUST the real pr-body-completeness rule against `event`, reading the
    author and labels from a real GITHUB_EVENT_PATH file the way CI does. Returns the validator's exit
    code and lets it print its own operator report (including the real disclosure line)."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(event, fh)
        event_path = fh.name
    saved_env = os.environ.get("GITHUB_EVENT_PATH")
    saved_loader = validate.load_rules
    try:
        os.environ["GITHUB_EVENT_PATH"] = event_path
        validate.load_rules = lambda: [RULE]                 # focus the output on the body check
        ctx = {"pr_body": validate.get_pr_body(None),        # the REAL trusted-context reads
               "pr_author": validate.get_pr_author(),
               "pr_labels": validate.get_pr_labels()}
        return validate.run("CI", ctx)
    finally:
        validate.load_rules = saved_loader
        if saved_env is None:
            os.environ.pop("GITHUB_EVENT_PATH", None)
        else:
            os.environ["GITHUB_EVENT_PATH"] = saved_env
        os.unlink(event_path)


def _pr(author: str, *, labels: list | None = None) -> dict:
    """A minimal PR event with an EMPTY body, the given author, and optional labels."""
    return {"pull_request": {"user": {"login": author}, "body": "",
                             "labels": [{"name": n} for n in (labels or [])]}}


def main() -> int:
    print("=" * 78)
    print("The engine's own single-purpose pull requests and the eight-section PR-body check")
    print("The rule file declares:  ci_author_exempt =", RULE.get("ci_author_exempt"))
    print("                         ci_label_exempt  =", RULE.get("ci_label_exempt"))
    print("=" * 78)

    print("\n[1] A Dependabot pull request with an EMPTY body (its native changelog body is not")
    print("    the eight-section template). Expect: PASS by AUTHOR, with the reason stated plainly.")
    print("-" * 78)
    rc_bot = _run_ci_body_check(_pr("dependabot[bot]"))
    print(f"\n   -> validator exit code: {rc_bot}   (0 = the merge gate is NOT blocked)")

    print("\n[2] The SAME empty body, authored by a person ('a-human-maintainer'), NO exempt label.")
    print("    Expect: FAIL — a plain human pull request must fill the eight sections.")
    print("-" * 78)
    rc_human = _run_ci_body_check(_pr("a-human-maintainer"))
    print(f"\n   -> validator exit code: {rc_human}   (1 = the merge gate IS blocked)")

    print("\n[3] A single-purpose memory-erasure proposal: EMPTY eight-section body, authored by a")
    print("    NON-exempt human, but carrying the 'engine-erasure' label. It carries its own plain")
    print("    consent body instead. Expect: PASS by LABEL (proving the label waived it, not the author).")
    print("-" * 78)
    rc_label = _run_ci_body_check(_pr("a-human-maintainer", labels=["engine-erasure"]))
    print(f"\n   -> validator exit code: {rc_label}   (0 = the merge gate is NOT blocked)")

    print("\n[4] The guardrail-ack gate is UNCHANGED. A pull request that changes the locked dependency")
    print("    file is still flagged — neither the author nor a label makes any difference here.")
    print("-" * 78)
    flagged = weakening_guard.flagged_changes(
        [{"filename": ".engine/uv.lock", "status": "modified"}])
    print(f"   guardrail-weakening classifier on a uv.lock change: {flagged}")
    print("   -> not empty: the maintainer must still apply the `guardrail-ack` label to merge.")

    ok = (rc_bot == 0 and rc_human == 1 and rc_label == 0 and bool(flagged))
    print("\n" + "=" * 78)
    print("In plain words: the body check is GREEN for Dependabot (by its author) and for a")
    print("single-purpose erasure proposal (by its label) because the eight-section template does NOT")
    print("apply to those — each carries its own account, and it was not verified, just not-applicable.")
    print("Every other pull request still fills the eight sections, and the lockfile change is still")
    print("gated by the label you apply.")
    print("DEMO OK" if ok else "DEMO FAILED — unexpected outcome")
    print("=" * 78)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
