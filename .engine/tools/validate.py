#!/usr/bin/env python3
"""Seed validator — a thin dispatcher over check-rule data.

Stage-0 seed for engine-template. Modeled on the engine-planning workspace's own
validate.py: the check *inventory* is data (.engine/check/*.json) and the check
*logic* is a small registry of kind callables, so adding a check adds a rule file
and never edits this dispatcher. Superseded by the validators-core thin dispatcher
once that module lands (stage-0-harness §4 / §6; the supersession is a handoff —
the rule data carries over).

Each kind callable returns a Result: a pass/fail verdict plus zero or more findings
on the canonical finding.v1 base {severity, message, location}. A check finding's
severity is the rule's tier (`hard` | `soft`) (decision-log D-113). The dispatcher
routes each rule to its kind, collects results, and reports by tier.

Tier vs. context: a `hard` finding in the CI suite fails the run (exit 1) and, bound
as a required check, blocks the merge — CI is the only unbypassable gate. A rule
whose kind is unregistered, or whose callable errors, FAILS CLOSED in CI, so a
`hard` governance rule can never be silently un-enforced.

Usage:
  validate.py --suite CI                      # run the CI suite (default)
  validate.py --suite CI --pr-body-file PATH  # supply the PR body explicitly

The PR body is read from --pr-body-file, else from $GITHUB_EVENT_PATH
(.pull_request.body — the safe path: the body is never interpolated into a shell
command), else treated as unavailable (the PR-body check fails OPEN locally and
evaluates in CI).
"""
from __future__ import annotations
import json
import os
import re
import sys

THIS = os.path.abspath(__file__)
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(THIS)))  # .engine/tools/validate.py -> repo root
CHECK_DIR = os.path.join(ROOT, ".engine", "check")

LINK_RE = re.compile(r"\]\(([^)]+)\)")
HEADING_RE = re.compile(r"^##\s+(.*?)\s*$")          # a level-2 (## ) heading; ### does not match
PLACEHOLDER_LINE_RE = re.compile(r"^\s*<[^>]*>\s*$")  # a template prompt, e.g. <why this change exists>


# ---- finding.v1 ------------------------------------------------------------

def finding(severity: str, message: str, location: dict | None = None) -> dict:
    """A finding on the canonical finding.v1 base {severity, message, location}."""
    return {"severity": severity, "message": message, "location": location}


def loc(path: str, line: int | None = None) -> dict:
    return {"file": os.path.relpath(path, ROOT), "line": line}


# ---- kind callables: (rule, ctx) -> (passed: bool, findings: list) ---------

def kind_pr_body_completeness(rule, ctx):
    """The eight contract sections must each be present and non-empty. A section
    is empty if, after dropping blank lines and template placeholder lines, no
    substantive content remains — so the auto-populated template body does NOT
    pass on its own. Presence + non-emptiness are gated; truthfulness is posture
    (this cannot judge whether the content is accurate)."""
    tier = rule["tier"]
    sections = rule.get("params", {}).get("sections", [])
    body = ctx.get("pr_body")
    if body is None:
        return True, [finding("soft", "PR body not available; completeness not "
                              "evaluated here (the CI run evaluates it).")]
    blocks = section_blocks(body)
    findings = []
    for name in sections:
        if name not in blocks:
            findings.append(finding(tier, f"Required pull-request section '## {name}' "
                            f"is missing. {rule['message']}"))
        elif is_empty_section(blocks[name]):
            findings.append(finding(tier, f"Required pull-request section '## {name}' "
                            f"is empty or only contains the template placeholder. {rule['message']}"))
    return (len(findings) == 0), findings


def section_blocks(body: str) -> dict:
    """{heading_text: section_body} for each level-2 heading in the body."""
    blocks, current, buf = {}, None, []
    for line in body.splitlines():
        m = HEADING_RE.match(line)
        if m:
            if current is not None:
                blocks[current] = "\n".join(buf)
            current, buf = m.group(1), []
        elif current is not None:
            buf.append(line)
    if current is not None:
        blocks[current] = "\n".join(buf)
    return blocks


def is_empty_section(text: str) -> bool:
    for line in text.splitlines():
        if not line.strip() or PLACEHOLDER_LINE_RE.match(line):
            continue
        return False
    return True


def kind_link_integrity(rule, ctx):
    """Every relative Markdown link must resolve to an existing file. A link that
    resolves OUTSIDE the repo cannot be checked in a CI checkout, so it is a soft
    note, never a hard failure (mirrors the engine-planning validator)."""
    tier = rule["tier"]
    exclude = set(rule.get("params", {}).get("exclude_dirs", []))
    findings = []
    for path in markdown_files(exclude):
        text = read(path)
        base = os.path.dirname(path)
        for m in LINK_RE.finditer(text):
            target = m.group(1).strip()
            if not target or target.startswith(("#", "mailto:")) or "://" in target:
                continue
            target = target.split("#", 1)[0].strip()
            if not target:
                continue
            resolved = os.path.normpath(os.path.join(base, target))
            if os.path.exists(resolved):
                continue
            inside = os.path.abspath(resolved).startswith(ROOT + os.sep)
            line_no = text[:m.start()].count("\n") + 1
            sev = tier if inside else "soft"
            findings.append(finding(sev, f"Broken Markdown link to '{target}'. {rule['message']}",
                            loc(path, line_no)))
    return (not any(f["severity"] == "hard" for f in findings)), findings


# ---- helpers ---------------------------------------------------------------

def markdown_files(exclude_dirs: set) -> list:
    out = []
    for dirpath, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs
                   if d != ".git"
                   and os.path.relpath(os.path.join(dirpath, d), ROOT) not in exclude_dirs]
        out.extend(os.path.join(dirpath, f) for f in files if f.endswith(".md"))
    return out


def read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


REGISTRY = {
    "pr-body-completeness": kind_pr_body_completeness,
    "link-integrity": kind_link_integrity,
}


# ---- dispatcher ------------------------------------------------------------

def load_rules() -> list:
    if not os.path.isdir(CHECK_DIR):
        return []
    return [json.loads(read(os.path.join(CHECK_DIR, n)))
            for n in sorted(os.listdir(CHECK_DIR)) if n.endswith(".json")]


def get_pr_body(body_file: str | None) -> str | None:
    if body_file:
        return read(body_file)
    event = os.environ.get("GITHUB_EVENT_PATH")
    if event and os.path.exists(event):
        pr = (json.loads(read(event)).get("pull_request") or {})
        return pr.get("body") or ""
    return None


def run(suite: str, ctx: dict) -> int:
    findings = []
    for rule in [r for r in load_rules() if suite in r.get("suites", [])]:
        kind, tier = rule.get("kind"), rule.get("tier", "hard")
        fn = REGISTRY.get(kind)
        if fn is None:  # dangling kind: fail closed (a finding at the rule's tier)
            findings.append(finding(tier, f"Check rule '{rule.get('id')}' names "
                            f"unregistered kind '{kind}'; cannot evaluate (fails closed)."))
            continue
        try:
            _verdict, found = fn(rule, ctx)
        except Exception as exc:  # a kind that errors fails closed
            findings.append(finding("hard", f"Check rule '{rule.get('id')}' (kind "
                            f"'{kind}') errored and could not evaluate: {exc}"))
            continue
        findings.extend(found)
    report(suite, findings)
    # Gate on the authoritative signal — any hard-severity finding — so the exit
    # code and report() can never disagree. A callable's verdict flag is advisory;
    # the rule's tier (carried as the finding severity) decides where teeth land.
    hard_fired = any(f["severity"] == "hard" for f in findings)
    return 1 if (suite == "CI" and hard_fired) else 0


def fmt(f: dict) -> str:
    where = ""
    if f.get("location"):
        l = f["location"]
        where = f"  [{l.get('file')}" + (f":{l['line']}" if l.get("line") else "") + "]"
    return f["message"] + where


def report(suite: str, findings: list) -> None:
    hard = [f for f in findings if f["severity"] == "hard"]
    soft = [f for f in findings if f["severity"] != "hard"]
    if soft:
        print(f"\nnotes ({len(soft)}):")
        for f in soft:
            print("  - " + fmt(f))
    if hard:
        print(f"\nFAIL ({len(hard)} hard finding(s)) [suite: {suite}]:")
        for f in hard:
            print("  - " + fmt(f))
    else:
        print(f"\nOK — suite '{suite}' passed, no hard findings.")


def main(argv: list) -> int:
    suite, body_file, i = "CI", None, 0
    while i < len(argv):
        if argv[i] == "--suite" and i + 1 < len(argv):
            suite, i = argv[i + 1], i + 2
        elif argv[i] == "--pr-body-file" and i + 1 < len(argv):
            body_file, i = argv[i + 1], i + 2
        else:
            print(f"unknown argument: {argv[i]}", file=sys.stderr)
            return 2
    return run(suite, {"pr_body": get_pr_body(body_file)})


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
