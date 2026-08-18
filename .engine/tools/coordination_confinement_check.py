#!/usr/bin/env python3
"""coordination-confinement — prove the advisory-only law mechanically (StarshipSuperjam/engine-template#939, eADR-0043 law 3).

A coordination notice must never carry authority, and the way that is guaranteed is by CONFINING what the
coordination code may do to GitHub: it may post and edit ONE comment, and read. Nothing else. This check is a
**fail-closed whitelist** over the coordination library (`.engine/tools/coordination_*.py`, excluding this
scanner and tests): a GitHub-*mutating* call (POST/PUT/PATCH/DELETE) is allowed ONLY when it targets a
comment endpoint; and importing or calling an authority-writing surface — the merge path, a label writer, the
commit-status writer (`ack_status`), an issue-body edit, or a pull-request state change — is forbidden
outright. A violation is a HARD finding, so the blocklist-evadable "scan for known-bad calls" gap the risk
review flagged is closed: the DEFAULT is deny, and only the comment transport is allowed.

The check reads the tree at ROOT (or an `ENGINE_COORDINATION_CONFINEMENT_ROOT` override, the fixture seam),
so its negative fixture can point it at a deliberately-bad coordination file and prove it bites. It is a
static source scan — the confinement is a property of the code, provable without running it.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402

_TOOLS_REL = os.path.join(".engine", "tools")

# The single confinement message every finding carries (the fixture's message_contains anchor).
_MSG = ("coordination code may reach GitHub only through the comment transport (post/patch a comment) and "
        "read-only reads — advisory notices carry no authority (eADR-0043)")

# Forbidden authority surfaces, matched by SYNTAX (a slash-path, a call paren, or an import keyword) so a
# prose mention of the same word in a docstring is never a false positive.
_DENY = [
    (re.compile(r"gh pr merge"), "a merge command"),
    (re.compile(r"/pulls/[^\"'\s]*?/merge"), "a merge endpoint"),
    (re.compile(r"/labels\b"), "a label endpoint"),
    (re.compile(r"\b(?:add_label|remove_label|ensure_repo_label)\s*\("), "a label write"),
    (re.compile(r"\b(?:import|from)\s+issue_label_client\b"), "the label-writer module"),
    (re.compile(r"/statuses\b"), "a commit-status endpoint"),
    (re.compile(r"\b(?:import|from)\s+ack_status\b"), "the commit-status writer module"),
    (re.compile(r"\bpost_ack_status\s*\("), "a commit-status write"),
    (re.compile(r"gh issue edit"), "an issue-body edit command"),
    (re.compile(r"\b(?:set_ready|set_draft)\s*\("), "a pull-request state change"),
]

# A quoted HTTP mutating method — allowed ONLY on a line that also references a comment endpoint.
_MUTATING_METHOD = re.compile(r"\"(POST|PUT|PATCH|DELETE)\"")
_API_PATH = re.compile(r"/repos/|issues/|pulls/")


def _coordination_files(root: str) -> list:
    """The coordination library files: `.engine/tools/coordination_*.py`, EXCLUDING this scanner
    (`*_check.py`) and any test/demo (which start with `test_`/`demo_`, so the glob never matches them)."""
    pattern = os.path.join(root, _TOOLS_REL, "coordination_*.py")
    return sorted(f for f in glob.glob(pattern) if not f.endswith("_check.py"))


def _scan(path: str, rel: str) -> list:
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        # Unreadable coordination source -> fail closed: we cannot prove confinement, so flag it.
        return [validate.finding("hard", f"{_MSG} — could not read {rel}", {"file": rel, "line": None})]
    findings = []
    for lineno, line in enumerate(lines, 1):
        for rx, what in _DENY:
            if rx.search(line):
                findings.append(validate.finding(
                    "hard", f"{_MSG} — {rel}:{lineno} reaches {what}", {"file": rel, "line": lineno}))
        if _MUTATING_METHOD.search(line) and _API_PATH.search(line) and "comments" not in line:
            findings.append(validate.finding(
                "hard", f"{_MSG} — {rel}:{lineno} makes a GitHub write to a non-comment endpoint",
                {"file": rel, "line": lineno}))
    return findings


def check(root: "str | None" = None) -> list:
    """Every confinement violation in the coordination library as a `hard` finding (empty = confined)."""
    root = root or validate.ROOT
    findings = []
    for path in _coordination_files(root):
        findings.extend(_scan(path, os.path.relpath(path, root)))
    return findings


def main() -> int:
    print(json.dumps(check(validate.env_override_path("ENGINE_COORDINATION_CONFINEMENT_ROOT"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
