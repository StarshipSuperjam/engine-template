"""Low-level GitHub gateway and idempotent remote postconditions for Build."""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys
from typing import Callable

import build_coordinator_core as core

PLAN_BEGIN = "<!-- engine-build-plan:v1 "
PLAN_END = "<!-- /engine-build-plan -->"
HANDOFF_BEGIN = "<!-- engine-build-handoff:v1 "
HANDOFF_END = "<!-- /engine-build-handoff -->"
# v2 markers version BOTH the begin and end tokens (defence in depth: the v1 end token is not a
# substring of the v2 end token, so a v1 reader can never straddle a v2 block and vice versa).
PLAN_BEGIN_V2 = "<!-- engine-build-plan:v2 "
PLAN_END_V2 = "<!-- /engine-build-plan:v2 -->"
HANDOFF_BEGIN_V2 = "<!-- engine-build-handoff:v2 "
HANDOFF_END_V2 = "<!-- /engine-build-handoff:v2 -->"
BUILD_MARKER = "<!-- engine-build-id:v1 nonce={nonce} repo={repo} pr={pr} plan={plan_digest} -->"
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


def _version_tag(schema_version: str) -> str:
    """'build-plan.v2' -> 'v2'. The tag that selects a document's marker pair."""
    return (schema_version or "build-plan.v1").rsplit(".", 1)[-1]


def _plan_markers(tag: str) -> tuple[str, str]:
    return (PLAN_BEGIN_V2, PLAN_END_V2) if tag == "v2" else (PLAN_BEGIN, PLAN_END)


def _handoff_markers(tag: str) -> tuple[str, str]:
    return (HANDOFF_BEGIN_V2, HANDOFF_END_V2) if tag == "v2" else (HANDOFF_BEGIN, HANDOFF_END)


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
                          "--json", "number,state,isDraft,headRefOid,baseRefOid,mergeable,body"])


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


def plan_block(plan: dict) -> str:
    begin, end = _plan_markers(_version_tag(plan.get("schema_version", "build-plan.v1")))
    plan_digest = core.digest(plan)
    rendered = json.dumps(plan, indent=2, sort_keys=True, ensure_ascii=False)
    return f"{begin}{plan_digest} -->\n```json\n{rendered}\n```\n{end}"


def replace_plan_block(body: str, plan: dict) -> str:
    begin, end = _plan_markers(_version_tag(plan.get("schema_version", "build-plan.v1")))
    pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end), re.DOTALL)
    block = plan_block(plan)
    matches = list(pattern.finditer(body))
    if len(matches) > 1:
        raise core.CoordinatorError("Issue contains more than one Build plan block; resolve it manually")
    after = body[:matches[0].start()] + block + body[matches[0].end():] if matches else \
        body.rstrip() + ("\n\n" if body.strip() else "") + block + "\n"
    require_body_budget(after, "durable Issue body")
    return after


def durable_plan(body: str, *, plan_schema) -> dict:
    """Read the exact durable plan block. ``plan_schema`` may be a single Path (legacy v1-only) or a
    map of schema_version -> Path, in which case the block's own version is detected and validated
    against the matching schema. Exactly one plan block, of one version, may be present."""
    schemas = plan_schema if isinstance(plan_schema, dict) else {"build-plan.v1": plan_schema}
    found = []
    for schema_version, schema in schemas.items():
        begin, end = _plan_markers(_version_tag(schema_version))
        pattern = re.compile(
            re.escape(begin) + r"(sha256:[0-9a-f]{64}) -->\n```json\n(.*?)\n```\n" + re.escape(end),
            re.DOTALL,
        )
        matches = list(pattern.finditer(body))
        if len(matches) > 1:
            raise core.CoordinatorError(f"durable Issue has more than one engine-build-plan:{_version_tag(schema_version)} block")
        if matches:
            found.append((schema, matches[0]))
    if len(found) != 1:
        raise core.CoordinatorError("durable Issue has no unique engine-build-plan block")
    schema, match = found[0]
    try:
        plan = json.loads(match.group(2))
    except ValueError as exc:
        raise core.CoordinatorError("durable Issue plan block is malformed") from exc
    core.validate(plan, schema)
    if core.digest(plan) != match.group(1):
        raise core.CoordinatorError("durable Issue plan content does not match its marker digest")
    return plan


def publish_issue(root: Path, repo: str, issue: int, plan: dict, *, plan_schema: Path) -> None:
    before = issue_body(root, repo, issue)
    after = replace_plan_block(before, plan)
    if issue_body(root, repo, issue) != before:
        raise core.CoordinatorError("Issue changed while preparing the plan; no write was made")
    core.must_run(["gh", "issue", "edit", str(issue), "--repo", repo, "--body-file", "-"],
                  root=root, input_value=after)
    confirmed = issue_body(root, repo, issue)
    if confirmed != after or core.digest(durable_plan(confirmed, plan_schema=plan_schema)) != core.digest(plan):
        raise core.CoordinatorError("GitHub did not preserve the exact durable plan; cold handoff is not safe")


def _current_login(root: Path) -> str:
    return core.must_run(["gh", "api", "user", "--jq", ".login"], root=root).strip()


def _marked_issue(root: Path, repo: str, marker: str, expected_login: str) -> int | None:
    rows = gh_json(root, ["issue", "list", "--repo", repo, "--state", "open", "--label", "engine",
                          "--limit", "100", "--json", "number,body,author"])
    matches = [row for row in rows if marker in (row.get("body") or "")]
    if len(matches) > 1:
        raise core.CoordinatorError("more than one open Issue carries this Build creation nonce; resolve it manually")
    if not matches:
        return None
    author = (matches[0].get("author") or {}).get("login")
    if author != expected_login:
        raise core.CoordinatorError("the matching Build marker was not created by the authenticated GitHub identity")
    return int(matches[0]["number"])


def create_or_resume_build_issue(
    root: Path,
    repo: str,
    pr: int,
    title: str,
    plan: dict,
    nonce: str,
    *,
    plan_schema: Path,
) -> int:
    sys.path.insert(0, str(root / ".engine" / "tools"))
    import issue_author

    marker = BUILD_MARKER.format(nonce=nonce, repo=repo, pr=pr, plan_digest=core.digest(plan))
    login = _current_login(root)
    issue = _marked_issue(root, repo, marker, login)
    if issue is None:
        ordered = "\n".join(f"- {index + 1}. `{item['id']}` — {item['description']}"
                             for index, item in enumerate(plan["work_items"]))
        body = issue_author.render_engine_issue_body(
            what_this_is=(f"This is the durable coordination surface for one Build: {plan['objective']}\n\n"
                          f"**Approved ordered scope**\n{ordered}"),
            whats_next=("The Build follows the exact machine-marked plan below. Progress stays in git and the "
                        "pull-request handoff; this Issue preserves authority for a cold or unattended session."),
        ).rstrip() + "\n\n" + marker + "\n"
        require_body_budget(replace_plan_block(body, plan), "new durable Build Issue body")
        created = core.must_run(["gh", "issue", "create", "--repo", repo, "--title", title,
                                 "--label", "engine", "--body-file", "-"], root=root, input_value=body).strip()
        match = re.search(r"/issues/([0-9]+)(?:\s*)$", created)
        if not match:
            # The response may have been lost after creation. Rediscovery is the recovery path.
            issue = _marked_issue(root, repo, marker, login)
            if issue is None:
                raise core.CoordinatorError("GitHub returned no Issue identity and the nonce could not be rediscovered")
        else:
            issue = int(match.group(1))
    current = issue_body(root, repo, issue)
    if marker not in current:
        raise core.CoordinatorError("the recovered Issue does not match the persisted Build creation identity")
    publish_issue(root, repo, issue, plan, plan_schema=plan_schema)
    return issue


def ensure_pr_closes_issue(root: Path, repo: str, pr: int, issue: int) -> None:
    before = verify_draft(root, repo, pr).get("body") or ""
    line = f"Closes #{issue}"
    if re.search(rf"(?im)^\s*closes\s+#{issue}\s*$", before):
        return
    after = before.rstrip() + ("\n\n" if before.strip() else "") + line + "\n"
    require_body_budget(after, "pull-request body")
    if (verify_draft(root, repo, pr).get("body") or "") != before:
        raise core.CoordinatorError("PR contract changed while linking the durable Build Issue; no write was made")
    core.must_run(["gh", "pr", "edit", str(pr), "--repo", repo, "--body-file", "-"], root=root, input_value=after)
    if (verify_draft(root, repo, pr).get("body") or "") != after:
        raise core.CoordinatorError("GitHub did not preserve the durable Build Issue closing link")


def handoff_block(value: dict) -> str:
    begin, end = _handoff_markers(_version_tag(value.get("schema_version", "build-handoff.v1")))
    rendered = json.dumps(value, indent=2, sort_keys=True)
    return f"{begin}{core.digest(value)} -->\n```json\n{rendered}\n```\n{end}"


def replace_handoff_block(body: str, value: dict) -> str:
    begin, end = _handoff_markers(_version_tag(value.get("schema_version", "build-handoff.v1")))
    block = handoff_block(value)
    pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end), re.DOTALL)
    after = pattern.sub(block, body) if pattern.search(body) else body.rstrip() + "\n\n" + block + "\n"
    require_body_budget(after, "pull-request handoff body")
    return after


def find_handoff_block(body: str, tag: str) -> tuple[str, str] | None:
    """Return (digest, json_text) for the unique handoff block of one version tag, or None.

    Raises on a duplicated block. The distinct v1/v2 markers guarantee the two versions never
    cross-match, so a body may safely be probed for each version in turn.
    """
    begin, end = _handoff_markers(tag)
    pattern = re.compile(
        re.escape(begin) + r"(sha256:[0-9a-f]{64}) -->\n```json\n(.*?)\n```\n" + re.escape(end),
        re.DOTALL,
    )
    matches = list(pattern.finditer(body))
    if len(matches) > 1:
        raise core.CoordinatorError(f"PR contract has more than one engine-build-handoff:{tag} block")
    if not matches:
        return None
    return matches[0].group(1), matches[0].group(2)
