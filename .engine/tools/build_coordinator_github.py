"""Low-level GitHub gateway and idempotent remote postconditions for Build."""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys
from typing import Callable

import build_coordinator_core as core

# One generation of markers, and one marked block. The v1 pair is gone with the v1 schemas, and with
# it the plan block entirely: B2 removed every path that WROTE a plan to GitHub, which left these
# readers able to read only an Issue body this engine can no longer produce. The handoff block is now
# the only marked block a PR body carries, and it is versioned in BOTH its begin and end tokens so a
# reader can never straddle a block of another generation.
HANDOFF_BEGIN_V2 = "<!-- engine-build-handoff:v2 "
HANDOFF_END_V2 = "<!-- /engine-build-handoff:v2 -->"
GITHUB_BODY_BUDGET_BYTES = 60_000

# The single home for the coordinator-ownership label. A coordinator-staged PR carries it as a durable,
# visible "the Build coordinator owns this workflow" tag (StarshipSuperjam/engine-template#1014) — the
# recurring reminder to finish through submit apply rather than a bare `gh pr ready`. It is engine-applied
# (not operator-applied) and backs NO guard — no check, workflow, or ruleset reads it — so it is deliberately
# NOT in bootstrap's `REQUIRED_LABELS`, which is scoped to the labels the engine's guards depend on. It is
# created on demand at bind (`tag_coordinator_owned`) instead, the right mechanism for a non-guard engine
# label: a soft aid, never a gate.
COORDINATOR_OWNED_LABEL = "engine-coordinator-owned"
COORDINATOR_OWNED_LABEL_COLOR = "5319e7"
COORDINATOR_OWNED_LABEL_DESCRIPTION = "Staged by the Build coordinator; reach ready only through submit apply."


def gh_json(root: Path, argv: list[str]):
    try:
        return json.loads(core.must_run(["gh", *argv], root=root))
    except ValueError as exc:
        raise core.CoordinatorError("GitHub returned malformed JSON") from exc


def require_body_budget(body: str, surface: str) -> None:
    size = len(body.encode("utf-8"))
    if size > GITHUB_BODY_BUDGET_BYTES:
        raise core.CoordinatorError(
            f"{surface} would be {size} bytes, above the {GITHUB_BODY_BUDGET_BYTES}-byte safe publication budget; "
            f"split the Build or reduce non-authoritative prose before publishing the {surface}"
        )


def verify_draft(root: Path, repo: str, pr: int) -> dict:
    data = pr_state(root, repo, pr)
    if data.get("number") != pr or data.get("state") != "OPEN" or data.get("isDraft") is not True:
        raise core.CoordinatorError(f"{repo}#{pr} must be the open draft claim for this Build")
    return data


def pr_state(root: Path, repo: str, pr: int) -> dict:
    return gh_json(root, ["pr", "view", str(pr), "--repo", repo,
                          "--json", "number,state,isDraft,headRefOid,baseRefOid,mergeable,body,statusCheckRollup"])


def required_check(data: dict, context: str) -> tuple:
    """`(state, entry)` for one named check in the PR's live statusCheckRollup — `state` is one of
    "absent", "pending", "success", "failure".

    Both rollup shapes are read: a CheckRun carries name/status/conclusion, a StatusContext carries
    context/state. Resolution is deliberately conservative, in two layers a reviewer attacked and
    this ordering answers. ANY matching entry still in flight makes the whole answer pending — a
    metadata event firing during a long full run produces overlapping entries, and sorting an
    in-flight run's start against a completed run's finish would read success while the decisive run
    is still executing. Among completed entries, CheckRun-shaped ones outrank StatusContext-shaped
    ones: the registered workflow's check IS a CheckRun, while a commit status sharing its name can
    be posted by any repository writer through the status API — a same-named status must never
    out-vote the platform's own check. Ties within the preferred shape go to the LATEST entry by its
    own timestamps, which is how a superseded run's lingering red loses to the newer green (observed
    on this repository's pull requests). Two residuals, both stated rather than
    flattered: a same-named CheckRun from another installed app is indistinguishable here, and where
    NO CheckRun exists at all a lone same-named commit status decides, since a preference between
    shapes can only rank the shapes that are present. Both are bounded downstream — the import
    verifies provenance by the platform-reported workflow path, and the operator's merge stands
    behind branch protection — but neither is closed by this reader, and the submission re-read
    leans on that downstream bound rather than on anything proven here."""
    entries = [x for x in (data.get("statusCheckRollup") or [])
               if x.get("name") == context or x.get("context") == context]
    if not entries:
        return "absent", None

    def stamp(entry):
        return entry.get("completedAt") or entry.get("startedAt") or entry.get("createdAt") or ""

    def in_flight(entry):
        if "state" in entry:                       # StatusContext shape
            return (entry.get("state") or "").upper() in ("EXPECTED", "PENDING")
        return (entry.get("status") or "").upper() != "COMPLETED"

    pending = [x for x in entries if in_flight(x)]
    if pending:
        return "pending", max(pending, key=stamp)
    check_runs = [x for x in entries if "state" not in x]
    latest = max(check_runs or entries, key=stamp)
    if "state" in latest:                          # StatusContext shape
        return ("success" if (latest.get("state") or "").upper() == "SUCCESS" else "failure"), latest
    conclusion = (latest.get("conclusion") or "").upper()
    return ("success" if conclusion == "SUCCESS" else "failure"), latest


def set_ready(root: Path, repo: str, pr: int) -> None:
    core.must_run(["gh", "pr", "ready", str(pr), "--repo", repo], root=root)


def set_draft(root: Path, repo: str, pr: int) -> None:
    core.must_run(["gh", "pr", "ready", str(pr), "--repo", repo, "--undo"], root=root)


def tag_coordinator_owned(root: Path, repo: str, pr: int) -> bool:
    """Best-effort: create-on-demand and apply COORDINATOR_OWNED_LABEL to a coordinator-staged PR. Two calls
    because `gh pr edit --add-label` fails if the label does not already exist; `gh label create --force`
    creates-or-updates idempotently. `core.must_run` raises on any non-zero exit, so both are caught: a
    labeling failure must never abort the Build — the caller discloses and proceeds (the tag is an aid, not a
    gate). Returns True only when both the ensure and the apply succeed."""
    try:
        core.must_run(["gh", "label", "create", COORDINATOR_OWNED_LABEL, "--repo", repo,
                       "--color", COORDINATOR_OWNED_LABEL_COLOR,
                       "--description", COORDINATOR_OWNED_LABEL_DESCRIPTION, "--force"], root=root)
        core.must_run(["gh", "pr", "edit", str(pr), "--repo", repo,
                       "--add-label", COORDINATOR_OWNED_LABEL], root=root)
        return True
    except core.CoordinatorError:
        return False


def issue_body(root: Path, repo: str, issue: int) -> str:
    data = gh_json(root, ["issue", "view", str(issue), "--repo", repo, "--json", "number,state,body"])
    if data.get("number") != issue or data.get("state") != "OPEN":
        raise core.CoordinatorError(f"{repo}#{issue} is not an open Issue suitable for durable Build scope")
    return data.get("body") or ""


# Nothing here writes or reads a plan on GitHub any more. B2 removed every WRITE path — publishing a
# plan into an Issue body, authoring or resuming the dedicated Build Issue behind a creation nonce,
# and linking that Issue to close with the PR — and the v1 sunset removes the matching READERS
# (`plan_block`, `replace_plan_block`, `durable_plan`). They were kept for one release so an old body
# could still be recognised and refused by name; with the v1 schemas deleted there is no schema left
# to validate such a block against, and a reader that cannot validate what it finds is not a refusal
# mechanism, it is a parser. A plan enters a Build from the local sealed library, and only from there.


def handoff_block(value: dict) -> str:
    rendered = json.dumps(value, indent=2, sort_keys=True)
    return f"{HANDOFF_BEGIN_V2}{core.digest(value)} -->\n```json\n{rendered}\n```\n{HANDOFF_END_V2}"


def find_handoff_block(body: str) -> tuple[str, str] | None:
    """Return (digest, json_text) for the unique handoff block, or None. Raises on a duplicate.

    One generation, so no tag argument: the v1 handoff marker retired with its schema, and probing a
    body for a version this engine can neither write nor validate would find only noise.
    """
    begin, end = HANDOFF_BEGIN_V2, HANDOFF_END_V2
    pattern = re.compile(
        re.escape(begin) + r"(sha256:[0-9a-f]{64}) -->\n```json\n(.*?)\n```\n" + re.escape(end),
        re.DOTALL,
    )
    matches = list(pattern.finditer(body))
    if len(matches) > 1:
        raise core.CoordinatorError("PR contract has more than one engine-build-handoff block")
    if not matches:
        return None
    return matches[0].group(1), matches[0].group(2)
