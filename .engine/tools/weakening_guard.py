#!/usr/bin/env python3
"""Guardrail-weakening classifier (stage-0 seed; re-homed onto custom/script in core slice 5b).

Runs on pull_request_target so its logic is read from the protected base branch
— a pull request cannot tamper with the guard that judges it. It READS THE DIFF
ONLY via the API and NEVER checks out or executes the pull request's head code.
This is an authoring invariant: the trigger grants the privilege, this script
enforces the restraint (it makes no use of the head ref and the workflow checks
out only the base).

It flags a change that removes, renames, or modifies a guardrail file (a CI
workflow, a check rule, an engine tool, or CODEOWNERS), AND a REPOINT of the
engine's update home in the manifest (`home_repository` in .engine/engine.json) —
which changes where executable engine code is fetched from at the next update, a
§15 supply-chain weakening (D-281/D-282, #367). A flagged change blocks the merge
until the operator applies the distinct, deliberate acknowledgment — the
`guardrail-ack` label — after reading, in plain language, what protection could
weaken (control-plane §weakening hard-gate; D-051 / D-134; principles §15).

It now runs as a frozen-named `custom/script` check rule (engine/check/guardrail-weakening),
invoked BY ID from engine-guard.yml (`validate.py --check`), NOT as part of the CI
suite — so its execution stays on the trusted-base pull_request_target workflow and
never moves into the head-checkout engine-ci context (the D-051 isolation). It emits
finding.v1 JSON on stdout (the custom/script machine channel) and returns 0 on a
successful evaluation: an empty array when nothing weakens or the `guardrail-ack`
label is present (the ack is an INPUT to this one guard, D-134); one finding at the
rule's tier (ENGINE_RULE_TIER, passed by the kind) — carrying the plain-language
ack guidance — on an unacknowledged guardrail change; and a fail-closed finding when
the pull-request context cannot be read, or when the full changed-file list cannot be
retrieved (it paginates the diff to completion and cross-checks what it read against the
pull request's authoritative `changed_files` count, failing closed on a partial view so a
weakening edit cannot hide past GitHub's file-listing cap). An internal crash returns non-zero, which
the custom/script kind turns into a hard fail-closed finding (defense in depth).

Honest bound: in solo the operator holds admin and could bypass the ruleset, so
this makes weakening NON-SILENT and DELIBERATE ("cannot weaken silently"), not
impossible ("cannot weaken at all" needs a distinct team identity).

Superseded by the control-plane weakening guard once that module lands.
"""
from __future__ import annotations
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # the sibling tools dir, for github_client
from github_client import get_json, get_page, next_link  # noqa: E402 — sibling import after the path insert

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


# The engine's update HOME lives in the manifest as a single key. A change to its VALUE (a repoint)
# redirects where executable engine code is fetched from at the next update — a §15 supply-chain weakening
# that needs the deliberate ack (D-281/D-282, #367). The manifest is deliberately NOT whole-file guarded:
# it legitimately churns on every upgrade/add (version bumps) and on first-run setup, so blanket-guarding
# it would demand an ack on routine updates. Instead the detector compares the diff against the home
# recorded in the TRUSTED BASE manifest and FAILS CLOSED — so it cannot be falsified by the change it judges.
ENGINE_MANIFEST_REL = ".engine/engine.json"
_HOME_VALUE_RE = re.compile(r'"home_repository"\s*:\s*"([^"]*)"')
# The base manifest on disk. The guard runs on pull_request_target with ONLY the trusted base checked out,
# so this reads the base value (never head) — the authoritative "what the home is now" the repoint compares
# against. `<repo>/.engine/engine.json`, three dirnames up from `<repo>/.engine/tools/weakening_guard.py`.
_BASE_MANIFEST = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".engine", "engine.json")


def _read_base_home() -> str | None:
    """The `home_repository` recorded in the BASE manifest, read from disk (the trusted base checkout, never
    head). None when absent/unreadable — i.e. no home is recorded yet, so a home appearing in the diff is a
    first recording, not a repoint."""
    try:
        with open(_BASE_MANIFEST, encoding="utf-8") as fh:
            home = json.load(fh).get("home_repository")
        return home if isinstance(home, str) and home.strip() else None
    except Exception:  # noqa: BLE001 — absent / unreadable base manifest -> treated as no home recorded
        return None


def _touches_home_key(patch: str) -> bool:
    """True iff the unified-diff `patch` adds or removes any line mentioning the `home_repository` key — a
    SUBSTRING test (not a value regex), so a duplicate-key injection (JSON's last value wins, but the added
    key line still shows), a value split across lines, and any reformatting of the home line all register as
    a touch. The `+++`/`---` file headers are excluded."""
    for line in patch.splitlines():
        plus = line.startswith("+") and not line.startswith("+++")
        minus = line.startswith("-") and not line.startswith("---")
        if (plus or minus) and "home_repository" in line:
            return True
    return False


def home_repoint(files: list, base_home: str | None) -> tuple | None:
    """A change to the engine's update home when one is ALREADY recorded (`base_home`) is a §15 repoint —
    returns (base_home, new_value_or_None) to flag, else None. FAILS CLOSED so the guard cannot be falsified
    by the change it judges: once a home exists, ANY touch of the `home_repository` key in the manifest diff,
    and a `patch` too large to be returned at all, both require the ack. This defeats a duplicate-key
    injection, a value split across lines, and a patch-suppressing bloat — the line-pair match this replaced
    missed all three (#367 security review). A first recording (no `base_home` yet) is never a repoint, so
    seeding and back-fill need no ack; a version-only bump (no home line in the patch) does not touch the key
    and does not flag. `new_value` is the added home value when parseable on one line, else None (the
    operator message then says 'a different repository')."""
    if not base_home:
        return None                        # no home recorded yet -> establishing one is not a repoint
    for f in files:
        if f.get("filename") != ENGINE_MANIFEST_REL:
            continue
        if f.get("status") not in WEAKENING_STATUS:
            continue
        patch = f.get("patch")
        if not patch:
            return (base_home, None)        # a manifest change we cannot inspect -> fail closed
        if _touches_home_key(patch):
            new = None
            for line in patch.splitlines():
                if line.startswith("+") and not line.startswith("+++"):
                    m = _HOME_VALUE_RE.search(line)
                    if m and m.group(1) != base_home:
                        new = m.group(1)
            return (base_home, new)
    return None


# A generous page bound: ~10k files at 100/page, well past GitHub's ~3000-file listing
# cap. It exists only to halt a pathological Link cycle — exceeding it raises (the caller
# fails closed), never silently truncates the file list it then judges.
MAX_PAGES = 100

# This guard's GitHub API User-Agent (was inline in its own request builder, now homed in
# github_client). The authenticated request shape + the off-host guard the §15 protection
# relies on now live in github_client; this guard reads the diff through the GET-only
# helpers below and never issues a write.
_UA = "engine-seed-weakening-guard"


def fetch_all_changed_files(repo: str, number, token: str) -> list:
    """The COMPLETE list of changed-file objects for the pull request, following Link
    pagination to exhaustion. Raises on a pathological Link cycle (more than MAX_PAGES
    pages) so the caller fails closed rather than judging a truncated set."""
    files = []
    url = f"/repos/{repo}/pulls/{number}/files?per_page=100"
    pages = 0
    while url:
        pages += 1
        if pages > MAX_PAGES:
            raise RuntimeError(f"changed-files pagination exceeded {MAX_PAGES} pages")
        page, link = get_page(url, token, user_agent=_UA)
        files.extend(page)
        url = next_link(link)
    return files


def changed_files_total(repo: str, number, token: str):
    """The pull request's authoritative changed-file count (GET /pulls/{n} -> changed_files).
    This count is the true total and is NOT subject to the files-listing cap, so it is the
    yardstick for whether the paginated listing was complete."""
    pr = get_json(f"/repos/{repo}/pulls/{number}", token, user_agent=_UA)
    return pr.get("changed_files")


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
        # Read ALL changed files (paginated to completion) AND the authoritative count —
        # both inside this fail-closed block, so any read failure, an off-host Link, or a
        # pathological Link cycle becomes the plain-language fail-closed finding below,
        # never an unhandled path.
        files = fetch_all_changed_files(repo, number, token)
        expected = changed_files_total(repo, number, token)
    except Exception as e:  # fail closed — never wave a change through unjudged
        return emit([{"severity": tier, "location": None,
                      "message": f"GUARDRAIL CHECK: could not read the changed files "
                      f"({e}); failing closed."}])

    # Completeness gate (the principles §15 non-falsifiability property): a guardrail-
    # weakening edit must not hide past GitHub's file-listing cap. If the guard could not
    # read EVERY changed file — fewer files seen than the pull request's authoritative
    # changed_files count, or no count at all — it fails closed and asks for the deliberate
    # acknowledgment; it never judges a pull request from a partial view. The cause here is
    # PR SIZE, not a detected weakening, so the message says so plainly and stays distinct
    # from the change-detected message below — the operator must never be told a guard
    # weakened when none was confirmed.
    # Count DISTINCT filenames — the same way GitHub's changed_files counts — so a
    # duplicate listing entry (or a pagination overlap) can never inflate the tally to
    # match the authoritative count while a real file goes unseen (§15: the guard must not
    # be falsifiable by the change it judges).
    seen = len({f.get("filename", "") for f in files})
    if not isinstance(expected, int) or seen < expected:
        if isinstance(expected, int):
            detail = (f"changes {expected} files — more than the safety check can read in "
                      f"one pass (it could read {seen}; GitHub limits how many files it "
                      "lists at once)")
        else:
            detail = ("did not report how many files it changes, so the safety check "
                      f"cannot confirm it read them all (it read {seen})")
        return emit([{"severity": tier, "location": None,
                      "message": "GUARDRAIL CHECK — this pull request " + detail + ".\n\n"
                      "Rather than judge your safety gates from a partial view, this check "
                      "is blocking.\n"
                      f"To approve this deliberately, apply the `{ACK_LABEL}` label to this "
                      "pull request (one deliberate action, distinct from the merge click). "
                      "Splitting the change into smaller pull requests also lets the check "
                      "read every file. Until then, this check blocks the merge."}])

    flagged = flagged_changes(files)
    repoint = home_repoint(files, _read_base_home())
    if not flagged and not repoint:
        return emit([])  # nothing weakens
    if ACK_LABEL in labels:
        return emit([])  # acknowledged via the label -> cleared (the ack is an INPUT here, D-134)

    parts = ["GUARDRAIL CHANGE DETECTED — this pull request changes protection you rely on:\n"]
    if flagged:
        listing = "\n".join(f"  - {status}: {shown}" for status, shown in flagged)
        parts.append("Files that enforce your safety gates:\n" + listing + "\n\n"
                     "If merged unwatched, a safety check could be turned off, renamed, or loosened — "
                     "letting future changes reach the protected branch without being checked.\n")
    if repoint:
        old, new = repoint
        target = new if new else "a different repository (the full change couldn't be read here)"
        parts.append(f"Your engine's update home is being changed from {old} to {target}. This changes WHERE "
                     f"your engine's own code is fetched from when it updates — a supply-chain change: a "
                     f"wrong or look-alike home could feed your engine altered code at its next update. The "
                     f"engine cannot itself tell a genuine home from a convincing look-alike — only you can "
                     f"confirm this is the home you intend.\n")
    parts.append(f"To approve this deliberately, apply the `{ACK_LABEL}` label to this pull request (one "
                 "deliberate action, distinct from the merge click). Until then, this check blocks the merge.")
    return emit([{"severity": tier, "location": None, "message": "\n".join(parts)}])


if __name__ == "__main__":
    sys.exit(main())
