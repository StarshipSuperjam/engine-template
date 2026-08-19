#!/usr/bin/env python3
"""A small instrument panel for one PR-shaped Build.

The coordinator stores current mechanical evidence in one atomic local snapshot. It never chooses a plan,
finding remedy, operator escalation, or re-review depth, and it has no merge operation.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
from typing import Any

import build_coordinator_contract as composer  # aliased 'composer', not 'contract': this file uses the bare
# name 'contract' as a local for the reviewer-contract dict and the pr-contract state, and a module alias would
# be a shadowing landmine (a future use before the local assignment would raise UnboundLocalError).
import build_coordinator_core as core
import build_coordinator_dag as dag
import build_coordinator_github as github
import build_coordinator_review as review
import build_coordinator_spec as spec_service
import build_coordinator_work as work
import repo_identity
import review_integrity

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_PATH = ROOT / ".engine" / "build-protocol.json"
BINDINGS_PATH = ROOT / ".engine" / "policies" / "model-bindings.json"
PLAN_SCHEMA = ROOT / ".engine" / "schemas" / "build-plan.v1.json"
STATE_SCHEMA = ROOT / ".engine" / "schemas" / "build-state.v1.json"
HANDOFF_SCHEMA = ROOT / ".engine" / "schemas" / "build-handoff.v1.json"
PLAN_SCHEMA_V2 = ROOT / ".engine" / "schemas" / "build-plan.v2.json"
STATE_SCHEMA_V2 = ROOT / ".engine" / "schemas" / "build-state.v2.json"
HANDOFF_SCHEMA_V2 = ROOT / ".engine" / "schemas" / "build-handoff.v2.json"
# schema_version -> the schema file that validates a document carrying it.
PLAN_SCHEMAS = {"build-plan.v1": PLAN_SCHEMA, "build-plan.v2": PLAN_SCHEMA_V2}
STATE_SCHEMAS = {"build-state.v1": STATE_SCHEMA, "build-state.v2": STATE_SCHEMA_V2}
HANDOFF_SCHEMAS = {"build-handoff.v1": HANDOFF_SCHEMA, "build-handoff.v2": HANDOFF_SCHEMA_V2}
# The Engine major at which the v1 Build reader is removed. Until then v1 stays readable and existing
# v1 Builds run; new v1 binds are refused in deployed Engines (see cmd_plan_bind). A self-test fails
# closed once the Engine major reaches this while the v1 reader still ships — the mechanical removal
# trigger, so the legacy reader cannot become an indefinite disconnected artifact.
PLAN_V1_REMOVE_AT_MAJOR = 1
PLAN_BEGIN = "<!-- engine-build-plan:v1 "
PLAN_END = "<!-- /engine-build-plan -->"
HANDOFF_BEGIN = "<!-- engine-build-handoff:v1 "
HANDOFF_END = "<!-- /engine-build-handoff -->"
# The registered validation commands (id, operator label, argv) are declared in build-protocol.json, so
# both execution (cmd_validate) and PR rendering (the contract composer's Validation section) read one source.


CoordinatorError = core.CoordinatorError
_json = core.json_file
_input = core.input_text
_validate = core.validate

# The recurring reminder shown on every coordinator command a session runs mid-Build (status, checkpoint):
# the coordinator owns this PR's workflow, so it must reach ready THROUGH the submit gate, not a bare
# `gh pr ready` (StarshipSuperjam/engine-template#1014). It is a soft nudge — the operator's merge stays the
# binding gate — pairing with the durable 'engine-coordinator-owned' PR label applied at bind.
_COORDINATOR_OWNED_REMINDER = ("Coordinator-owned: reach ready only through 'submit apply' (never a bare "
                               "'gh pr ready'); the tail is contract apply -> preflight -> submit apply.")
_canonical = core.canonical
_digest = core.digest


def _plan_version(plan: dict) -> str:
    """The plan document's schema version, or v1 when unstated (the historical default)."""
    return plan.get("schema_version", "build-plan.v1")


def _plan(path: str) -> dict:
    try:
        value = json.loads(_input(path))
    except ValueError as exc:
        raise CoordinatorError(f"the Build plan is not valid JSON: {exc}") from exc
    version = _plan_version(value)
    schema = PLAN_SCHEMAS.get(version)
    if schema is None:
        raise CoordinatorError(f"unrecognized Build plan version {version!r}; expected build-plan.v1 or build-plan.v2")
    _validate(value, schema)
    ids = [item["id"] for item in value["work_items"]]
    if len(ids) != len(set(ids)):
        raise CoordinatorError("Build plan work-item ids must be unique")
    if version == "build-plan.v2":
        dag.validate_dag(value)
    return value


_criterion = spec_service.criterion


def _canonical_spec(plan: dict, *, repository: str | None = None, check_issue: bool = True) -> dict:
    return spec_service.canonical_spec(
        ROOT, plan, repository=repository, check_issue=check_issue,
        issue_body=_issue_body,
    )


def _assert_spec_current(state: dict, plan: dict, *, check_issue: bool = False) -> dict:
    canonical = _canonical_spec(plan, repository=state["build"]["repository"], check_issue=check_issue)
    approved = state["plan"].get("spec_digest")
    if state.get("approval") and canonical["digest"] != approved:
        raise CoordinatorError("settled specification changed since approval; revise and reapprove the plan")
    return canonical


def _assert_spec_boundary(state: dict, plan: dict, *, allow_same_session_offline: bool = False) -> dict:
    try:
        return _assert_spec_current(state, plan, check_issue=True)
    except CoordinatorError as exc:
        message = str(exc).lower()
        offline = any(token in message for token in ("network", "offline", "timed out", "could not resolve host", "gh issue view failed"))
        if allow_same_session_offline and state["build"]["mode"] == "same-session" and offline:
            return _assert_spec_current(state, plan, check_issue=False)
        raise


def _hard_check_declarations() -> list[dict]:
    return spec_service.hard_check_declarations(ROOT)


def _protocol() -> dict:
    """Load only the current runtime protocol.

    Historical preservation traceability is validated by Engine-home checks, not by
    normal Build commands.
    """
    value = _json(PROTOCOL_PATH)
    _validate(value, ROOT / ".engine" / "schemas" / "build-protocol.v1.json")
    return value


def _run(argv: list[str], *, cwd: Path = ROOT, input_text: str | None = None) -> subprocess.CompletedProcess:
    return core.run(argv, root=cwd, input_value=input_text)


def _run_validation(command: list[str], log_path: Path) -> int:
    return core.run_validation(command, log_path, root=ROOT)


def _must_run(argv: list[str], *, input_text: str | None = None) -> str:
    return core.must_run(argv, root=ROOT, input_value=input_text)


def _gh_json(argv: list[str]) -> Any:
    return github.gh_json(ROOT, argv)


def _head() -> str:
    return core.head(ROOT)


def _base() -> str:
    return core.base(ROOT)


def _verify_draft(repo: str, pr: int) -> dict:
    return github.verify_draft(ROOT, repo, pr)


def _state_schema_for(state: dict) -> Path:
    """Select the snapshot schema from the document's own version (defaulting to v1)."""
    version = state.get("schema_version", "build-state.v1")
    schema = STATE_SCHEMAS.get(version)
    if schema is None:
        raise CoordinatorError(f"unrecognized Build snapshot version {version!r}")
    return schema


class StateStore(core.StateStore):
    def __init__(self, path: str, expected_revision: int | None = None):
        super().__init__(path, _state_schema_for, expected_revision)


def _empty_review() -> dict:
    return {"packet_digest": None, "referent_digest": None, "required_lenses": [], "installed_lenses": [],
            "reviewer_contracts": [], "receipts": [], "reviewed_commit": None, "base_commit": None,
            "waiver": None}


def _initial_state(repo: str, pr: int, base: str, source: str, plan: dict, issue: int | None,
                   mode: str = "same-session") -> dict:
    state = {
        "schema_version": "build-state.v1", "revision": 1,
        "build": {"repository": repo, "pr": pr, "base_at_bind": base, "mode": mode},
        "plan": {"source": source, "digest": _digest(plan), "intent_digest": _digest(plan["raw_intent"].encode()),
                 "spec_digest": None, "durable_issue": issue, "profile": plan["profile"],
                 "bound_head": _head(), "promotion_nonce": None},
        "approval": None, "reviews": {"plan": _empty_review(), "deliverable": _empty_review()},
        "findings": [], "checkpoint": None, "progress": {"current_item": None, "completed": []},
        "validation": None, "repair": None,
        "preflights": [], "pr_contract": None, "submission": "draft",
        "checkout_snapshot": None
    }
    if _plan_version(plan) == "build-plan.v2":
        state["schema_version"] = "build-state.v2"
        state["work"] = {}
    return state


def _assert_plan(state: dict, plan: dict) -> None:
    actual = _digest(plan)
    if actual != state["plan"]["digest"]:
        raise CoordinatorError(f"supplied plan digest {actual} does not match approved Build plan {state['plan']['digest']}")


def _issue_body(repo: str, issue: int) -> str:
    return github.issue_body(ROOT, repo, issue)


def _plan_block(plan: dict) -> str:
    return github.plan_block(plan)


def _replace_plan_block(body: str, plan: dict) -> str:
    return github.replace_plan_block(body, plan)


def _durable_plan(body: str) -> dict:
    return github.durable_plan(body, plan_schema=PLAN_SCHEMAS)


def _publish_issue(repo: str, issue: int, plan: dict) -> None:
    github.publish_issue(ROOT, repo, issue, plan, plan_schema=PLAN_SCHEMAS)


def _create_build_issue(repo: str, pr: int, title: str, plan: dict, nonce: str) -> int:
    return github.create_or_resume_build_issue(
        ROOT, repo, pr, title, plan, nonce, plan_schema=PLAN_SCHEMAS,
    )


def _ensure_pr_closes_issue(repo: str, pr: int, issue: int) -> None:
    github.ensure_pr_closes_issue(ROOT, repo, pr, issue)


def _installed(stage: str) -> list[dict]:
    return review.installed(ROOT, stage)


def _required(protocol: dict, stage: str, depth: str, installed: list[dict]) -> list[dict]:
    return review.required(protocol, stage, depth, installed)


def _missing_findings(state: dict) -> list[str]:
    return review.missing_findings(state)


def _missing_receipts(stage: dict) -> list[str]:
    return review.missing_receipts(stage)


def _plan_review_ready(state: dict, plan: dict) -> tuple[bool, list[str]]:
    ready, missing = review.plan_review_ready(state, plan)
    stage = state["reviews"]["plan"]
    waiver = stage.get("waiver")
    trivial = plan["profile"] == "trivial" and (state.get("approval") or {}).get("depth") == "quick"
    if waiver or trivial or not state.get("approval"):
        return ready, missing
    current = _required(_protocol(), "plan", state["approval"]["depth"], _installed("plan"))
    recorded = {item["lens"]: (item["path"], item["digest"])
                for item in stage.get("reviewer_contracts", [])}
    changed = [item["lens"] for item in current
               if recorded.get(item["lens"]) != (item["path"], item["digest"])]
    missing.extend(f"refresh plan-review contract: {lens}" for lens in changed)
    return not missing, missing


def _trivial_violations(state: dict, plan: dict) -> list[str]:
    if plan["profile"] != "trivial":
        return []
    violations = []
    if state["build"]["mode"] != "same-session" or state["plan"]["source"] != "session":
        violations.append("the Build is no longer same-session and session-local")
    if len(plan["work_items"]) != 1:
        violations.append("the Build no longer has exactly one work item")
    paths = _changed_paths(state["build"]["base_at_bind"])
    sys.path.insert(0, str(ROOT / ".engine" / "tools"))
    import weakening_guard
    guarded = [path for path in paths if path.startswith(".engine/schemas/") or weakening_guard.is_guardrail(path)]
    if guarded:
        violations.append("guarded enforcement or schema surfaces changed: " + ", ".join(guarded))
    commit_count = int(_must_run(["git", "rev-list", "--count", f"{state['build']['base_at_bind']}..HEAD"]).strip())
    if commit_count > 1:
        violations.append(f"the Build has {commit_count} commits, not the one-commit fast path")
    return violations


def _next_incomplete(plan: dict, state: dict) -> str | None:
    """The single next work item the linear v1 order or the v2 DAG readiness would advance.

    v1 keeps its byte-identical linear scan; a v2 plan derives the next item from the graph's READY
    set, so status and checkpoint read one shared derivation rather than duplicating the scan.
    Deliberately ready_set, not claimable_set: a checkpoint records completion and reserves no worker
    slot, so a busy slot or a resource hold (which claimable_set subtracts) must not change which item
    is "next" to advance — only dependency readiness does.
    """
    if _plan_version(plan) == "build-plan.v2":
        ready = dag.ready_set(plan, state)
        return ready[0] if ready else None
    ordered = [item["id"] for item in plan["work_items"]]
    completed = {item["id"] for item in state["progress"]["completed"]}
    return next((item for item in ordered if item not in completed), None)


def _work_projection(plan: dict, state: dict) -> dict:
    """The DAG status section for a v2 Build: ready/claimable sets, per-node state, capacity, holders."""
    lifecycle = dag.derive_lifecycle(plan, state)
    parallelism = plan.get("parallelism", {"mode": "serial", "max_concurrency": 1})
    nodes = {}
    for node_id, node in lifecycle.items():
        nw = (state.get("work") or {}).get(node_id) or {}
        claim = nw.get("claim") or {}
        integration = nw.get("integration") or {}
        result = nw.get("latest_result") or {}
        failure = nw.get("latest_failure") or {}
        nodes[node_id] = {
            "state": node["state"], "reasons": node["reasons"],
            "attempt_count": nw.get("attempt_count", 0),
            "route": claim.get("requested_route"),
            "integration_commit": integration.get("commit"),
            "focused_verification": integration.get("focused_verification"),
            "artifact_digest": result.get("artifact_digest"),
            "failure": {"class": failure.get("class"), "disposition": failure.get("disposition"),
                        "reason": failure.get("reason")} if failure else None,
        }
    return {
        "ready": dag.ready_set(plan, state),
        "claimable": dag.claimable_set(plan, state),
        "slots_in_use": dag.slots_in_use(plan, state),
        "max_concurrency": parallelism.get("max_concurrency", 1),
        "resource_holders": dag.resource_holders(plan, state),
        "nodes": nodes,
    }


def _status(state: dict, plan: dict | None = None) -> dict:
    head = _head()
    required_evidence, judgments, warnings = [], [], []
    unresolved_assumptions: list[str] = []
    plan_stage, delivery = state["reviews"]["plan"], state["reviews"]["deliverable"]
    missing_findings = _missing_findings(state)
    blocking = [f["id"] for f in state["findings"] if f["blocks_this_pr"]]

    if state["approval"] is None or state["approval"].get("plan_digest") != state["plan"]["digest"]:
        required_evidence.append("operator approval of this plan digest and review depth")
    fast_path = bool(plan and plan["profile"] == "trivial" and (state.get("approval") or {}).get("depth") == "quick")
    trivial_violations = _trivial_violations(state, plan) if plan else []
    if plan and state["approval"]:
        _assert_spec_current(state, plan)
    plan_waived = bool(plan_stage.get("waiver") and state.get("approval")
                        and plan_stage["waiver"]["plan_digest"] == state["plan"]["digest"]
                        and plan_stage["waiver"]["depth"] == state["approval"]["depth"])
    if plan_stage["packet_digest"] is None and not fast_path and not plan_waived:
        required_evidence.append("plan-review packet")
    elif not plan_waived:
        required_evidence.extend(f"plan-review receipt: {x}" for x in _missing_receipts(plan_stage))
    required_evidence.extend(f"finding disposition: {x}" for x in missing_findings)
    if blocking:
        judgments.append("resolve or deliberately re-disposition findings blocking this PR: " + ", ".join(blocking))
    if state["checkpoint"] and state["checkpoint"]["judgment"] != "aligned":
        judgments.append(state["checkpoint"]["judgment"])
    if state["validation"] is None or state["validation"]["commit"] != head or not all(x["passed"] for x in (state["validation"] or {}).get("results", [])):
        required_evidence.append("green validation for the final commit")
    if delivery["packet_digest"] is None and not fast_path:
        required_evidence.append("deliverable-review packet")
    else:
        required_evidence.extend(f"deliverable-review receipt: {x}" for x in _missing_receipts(delivery))
    if delivery["reviewed_commit"] and delivery["reviewed_commit"] != head:
        repair = state["repair"]
        if not repair or repair["reviewed_commit"] != delivery["reviewed_commit"] or repair["final_commit"] != head:
            judgments.append("choose none, scoped, or full re-review for reviewed-to-final divergence")
        elif repair["judgment"] != "none":
            done = {r["lens"] for r in repair["receipts"]}
            required_evidence.extend(f"repair-review receipt: {x}" for x in repair["lenses"] if x not in done)
    protocol = _protocol()
    if state["approval"]:
        depth = state["approval"]["depth"]
        current_plan = _required(protocol, "plan", depth, _installed("plan"))
        current_delivery = _required(protocol, "deliverable", depth, _installed("deliverable"))
        def contracts_current(current, recorded):
            actual = {item["lens"]: (item["path"], item["digest"])
                      for item in recorded.get("reviewer_contracts", [])}
            return all(actual.get(item["lens"]) == (item["path"], item["digest"]) for item in current)
        plan_coverage_current = contracts_current(current_plan, plan_stage)
        delivery_coverage_current = contracts_current(current_delivery, delivery)
        if not plan_coverage_current and not plan_waived:
            required_evidence.append("refresh plan-review coverage for the currently installed reviewers")
        if delivery["packet_digest"] and not delivery_coverage_current:
            required_evidence.append("refresh deliverable-review coverage for the currently installed reviewers")
    else:
        plan_coverage_current = delivery_coverage_current = True
    passed = {x["id"] for x in state["preflights"] if x["commit"] == head and x["passed"]}
    required_preflights = [x for x in protocol["preflights"] if x["required"]]
    required_evidence.extend(f"green preflight: {x['id']}" for x in required_preflights if x["id"] not in passed)
    if not state["pr_contract"] or state["pr_contract"]["commit"] != head or not state["pr_contract"]["complete"]:
        required_evidence.append("complete PR contract for the final commit")
    if state["plan"]["source"] == "session":
        warnings.append("plan is session-local; promote it before intentional cold-session handoff")
    if plan_waived:
        warnings.append("plan review explicitly waived by the operator: " + plan_stage["waiver"]["reason"])
    if plan:
        # An assumption's EFFECTIVE status is its authored plan status overlaid with any receipt-layer
        # disposition (StarshipSuperjam/engine-template#1014). A disposition never edits the plan, so the
        # plan digest, approval, and review receipts survive — but a genuinely-open premise the review
        # verified no longer forces a full review re-run to clear its engineering-decision hold. Computed
        # ONCE and fed to BOTH the judgment/warning lines AND the phase gate below, so a disposed assumption
        # can never wall while still printing "investigate …" (a contradictory render).
        dispositions = {d["claim"]: d for d in state.get("assumption_dispositions", [])}
        unresolved_assumptions, accepted, resolved_notes = [], [], []
        for item in plan.get("assumptions", []):
            claim, authored = item["claim"], item["status"]
            disposition = dispositions.get(claim)
            effective = disposition["resolved_as"] if disposition else authored
            if effective == "unresolved":
                unresolved_assumptions.append(claim)
            elif effective == "accepted-risk":
                accepted.append(claim)
            # A disposition can only touch an assumption authored 'unresolved', so its presence IS the
            # "resolved after approval" signal — no timestamp needed. Disclosed for BOTH verified and
            # accepted-risk (a 'verified' disposition must NOT vanish the way a plan-authored 'verified'
            # does), so the operator meets a self-attested post-hoc resolution at merge, never a silent skip.
            if disposition and authored == "unresolved":
                resolved_notes.append(
                    f"assumption resolved after approval (self-attested, not re-reviewed): {claim} "
                    f"-> {disposition['resolved_as']} — basis: {disposition['basis']}")
        judgments.extend("investigate unresolved assumption: " + value for value in unresolved_assumptions)
        warnings.extend("accepted plan risk: " + value for value in accepted)
        warnings.extend(resolved_notes)
    if trivial_violations:
        judgments.append("promote the trivial Build to the normal profile and renew approval: " + "; ".join(trivial_violations))

    approval_ready = state["approval"] is not None and state["approval"].get("plan_digest") == state["plan"]["digest"]
    plan_ready = fast_path or plan_waived or ((plan_stage.get("referent_digest") or plan_stage["packet_digest"])
                                               is not None and not _missing_receipts(plan_stage)
                                               and plan_coverage_current)
    dispositions_ready = not missing_findings and not blocking
    valid = state["validation"] is not None and state["validation"]["commit"] == head and all(x["passed"] for x in state["validation"]["results"])
    delivery_ready = fast_path or (delivery["packet_digest"] is not None and not _missing_receipts(delivery) and delivery_coverage_current)
    repair_ready = not delivery["reviewed_commit"] or delivery["reviewed_commit"] == head or (
        state["repair"] is not None and state["repair"]["reviewed_commit"] == delivery["reviewed_commit"]
        and state["repair"]["final_commit"] == head and (state["repair"]["judgment"] == "none" or
        not [x for x in state["repair"]["lenses"] if x not in {r["lens"] for r in state["repair"]["receipts"]}]))
    preflight_ready = not [x for x in required_preflights if x["id"] not in passed]
    contract_ready = bool(state["pr_contract"] and state["pr_contract"]["commit"] == head and state["pr_contract"]["complete"])

    if not approval_ready:
        phase, next_one, available = "planning", "approve the plan and review depth", []
    elif not plan_ready:
        phase, next_one, available = "plan-review", "prepare or complete the plan review", []
    elif not dispositions_ready:
        phase, next_one, available = "finding-disposition", None, ["critically adjudicate outstanding findings", "revise the plan if the agreed design changed"]
    elif trivial_violations or unresolved_assumptions or (state["checkpoint"] and state["checkpoint"]["judgment"] != "aligned"):
        phase, next_one, available = "engineering-decision", None, ["investigate unresolved assumptions", "revise the plan if the agreed design changed", "obtain a genuine operator decision only when required"]
    elif not valid:
        phase, next_one, available = "implementation", None, ["continue implementation", "run focused verification", "run final validation when the change is cohesive"]
    elif not delivery_ready:
        phase, next_one, available = "deliverable-review", "prepare or complete the deliverable review", []
    elif not repair_ready:
        phase, next_one, available = "repair-assessment", "record the proportional re-review judgment", []
    elif not preflight_ready or not contract_ready:
        phase, next_one, available = "submission-preflight", "run submission preflights", []
    else:
        phase, next_one, available = "ready", "preview submission", []
    ordered_items = [] if not plan else [item["id"] for item in plan["work_items"]]
    completed_items = [item["id"] for item in state["progress"]["completed"]]
    next_item = _next_incomplete(plan, state) if plan else None
    result = {"phase": phase, "head_commit": head, "snapshot_revision": state["revision"],
              "required_evidence": required_evidence, "engineering_judgment": judgments,
              "warnings": warnings, "suggested_next": next_one, "available_activities": available,
              "progress": {"completed": completed_items, "total": len(ordered_items),
                           "current": state["progress"]["current_item"], "next": next_item}}
    if plan is not None and state.get("schema_version") == "build-state.v2":
        result["work"] = _work_projection(plan, state)
    return result


def _confidently_home() -> bool:
    """True only when this checkout can be CONFIDENTLY placed as the Engine's own home repo.

    is_home_repo fails TOWARD home when the origin or manifest cannot be read — the safe direction for
    a check that RUNS, but the wrong direction for a governance carve-out whose quiet verdict SKIPS a
    refusal. So this requires both the on-disk origin and the recorded home to be readable AND equal;
    an unreadable or malformed either side is NOT confidently home, so the v1-bind refusal fails toward
    enforcing rather than being silently bypassed in a deployed repo.
    """
    own = repo_identity.origin_slug(str(ROOT))
    try:
        home = repo_identity.home_repository(str(ROOT))
    except Exception:  # noqa: BLE001 — a malformed manifest cannot confirm home; not confidently home
        home = None
    return own is not None and home is not None and repo_identity.slug_eq(own, home)


def cmd_plan_bind(args, store: StateStore) -> None:
    plan = _plan(args.input)
    mode = getattr(args, "mode", "same-session")
    # Once build-plan.v2 exists, a deployed Engine refuses a NEW session-sourced v1 bind and directs
    # the operator to migrate. An issue-sourced bind is the exempt path: it resumes an in-flight v1
    # Build from its durable Issue plan. The refusal no-ops ONLY in the Engine's own home repo, where
    # v1 is still dogfooded to build v2; an uncertain checkout fails toward enforcing the refusal.
    if _plan_version(plan) == "build-plan.v1" and args.source == "session" and not _confidently_home():
        raise CoordinatorError(
            "new session-sourced build-plan.v1 binds are refused now that build-plan.v2 is available; "
            "migrate this plan with 'plan migrate-v1' or resume an existing v1 Build from its durable "
            "Issue with --source issue")
    if mode == "unattended" and args.source != "issue":
        raise CoordinatorError("unattended Builds require an exact durable Issue plan")
    if plan["profile"] == "trivial" and (mode != "same-session" or args.source != "session"):
        raise CoordinatorError("trivial Builds are same-session and session-local only")
    if plan["profile"] == "routine" and (mode != "unattended" or args.source != "issue"):
        raise CoordinatorError("routine plans require unattended mode and durable Issue authority")
    pr = _verify_draft(args.repository, args.pr)
    if pr.get("headRefOid") != _head():
        raise CoordinatorError("the draft PR head does not match this worktree")
    issue = args.issue if args.source == "issue" else None
    if args.source == "issue":
        if issue is None:
            raise CoordinatorError("--issue is required for an Issue plan")
        durable = _durable_plan(_issue_body(args.repository, issue))
        if _digest(durable) != _digest(plan):
            raise CoordinatorError("supplied plan does not match the durable Issue plan")
        # The v1 Issue carve-out resumes an IN-FLIGHT Build, so it demands pre-existing continuation
        # evidence, not just marker text: a freshly authored Issue can carry a v1 plan block, but only
        # a Build that actually ran has published its v1 handoff into the PR contract. The evidence
        # must be a WELL-FORMED handoff block whose digest matches its content (the same bar restore
        # holds it to) — a quoted marker fragment in prose is not evidence. Without it, a deployed
        # Engine treats the bind as a new v1 Build and refuses toward migration.
        if _plan_version(plan) == "build-plan.v1" and not _confidently_home():
            block = github.find_handoff_block(pr.get("body") or "", "v1")
            valid = False
            if block:
                digest, rendered = block
                try:
                    valid = _digest(json.loads(rendered)) == digest
                except ValueError:
                    valid = False
            if not valid:
                raise CoordinatorError(
                    "a v1 Issue re-bind resumes an in-flight Build, so the draft PR must already carry "
                    "published v1 handoff evidence; new Builds use build-plan.v2 — migrate this plan "
                    "with 'plan migrate-v1'")
    state = _initial_state(args.repository, args.pr, pr.get("baseRefOid") or _base(), args.source, plan, issue, mode)
    store.create(state)
    # Tag the PR the coordinator just adopted, so it carries a durable "coordinator owns this workflow"
    # marker (StarshipSuperjam/engine-template#1014). Best-effort and non-fatal: a labeling failure is
    # disclosed on stderr and the Build proceeds — the stdout below stays a clean machine-readable line.
    if not github.tag_coordinator_owned(ROOT, args.repository, args.pr):
        print("build-coordinator: could not tag this PR 'engine-coordinator-owned' (a non-blocking aid); "
              "the Build proceeds — reach ready only through 'submit apply', never a bare 'gh pr ready'.",
              file=sys.stderr)
    print(json.dumps({"plan_digest": state["plan"]["digest"], "state": str(store.path)}))


def _migrate_v1_to_v2(v1: dict) -> dict:
    """Transform a v1 plan into a v2 linear-chain DAG, preserving item order.

    Each item depends on its predecessor (the linear chain reproduces v1's array-order execution),
    every node is integrator-executed with a default output contract, and the plan is serial. The
    result has a NEW digest, so it requires renewed approval and affected review — the migration is
    never a silent receipt-preserving rename.
    """
    items = v1["work_items"]
    migrated = []
    for index, item in enumerate(items):
        migrated.append({
            "id": item["id"], "description": item["description"], "paths": item["paths"],
            "verification": item["verification"],
            "depends_on": [items[index - 1]["id"]] if index else [],
            "exclusive_resources": [],
            "executor_class": "integrator",
            "output_contract": {"deliverable": item["description"],
                                "artifact_kinds": ["integrated-commit"],
                                "required_evidence": ["changed_paths", "verification_results"]},
        })
    v2 = {k: v for k, v in v1.items() if k != "work_items"}
    v2["schema_version"] = "build-plan.v2"
    v2["work_items"] = migrated
    v2["parallelism"] = {"mode": "serial", "max_concurrency": 1}
    return v2


def cmd_plan_migrate_v1(args, store: StateStore | None) -> None:
    v1 = _plan(args.input)
    if _plan_version(v1) != "build-plan.v1":
        raise CoordinatorError("plan migrate-v1 requires a build-plan.v1 document")
    v2 = _migrate_v1_to_v2(v1)
    _validate(v2, PLAN_SCHEMA_V2)
    dag.validate_dag(v2)
    rendered = json.dumps(v2, indent=2, sort_keys=True) + "\n"
    if args.output and args.output != "-":
        Path(args.output).write_text(rendered, encoding="utf-8")
        target = args.output
    else:
        sys.stdout.write(rendered)
        target = "stdout"
    print(f"migrated to build-plan.v2 ({_digest(v2)}) at {target}; the new digest requires renewed "
          f"operator approval and affected review before it can be bound", file=sys.stderr)


def cmd_plan_promote(args, store: StateStore) -> None:
    if not args.ack_visibility:
        raise CoordinatorError("promotion publishes the exact plan in an Issue body; pass --ack-visibility")
    plan = _plan(args.input)
    state = store.read()
    _assert_plan(state, plan)
    if plan["profile"] == "trivial":
        raise CoordinatorError("revise a trivial Build to the normal profile and renew approval before durable continuation")
    issue = args.issue
    if issue is None:
        if not state["plan"].get("promotion_nonce"):
            nonce = secrets.token_hex(16)
            store.mutate(lambda s: s["plan"].update({"promotion_nonce": nonce}),
                         from_revision=state["revision"])
            state = store.read()
        issue = _create_build_issue(state["build"]["repository"], state["build"]["pr"],
                                    args.create_issue, plan, state["plan"]["promotion_nonce"])
    else:
        _publish_issue(state["build"]["repository"], issue, plan)
    _ensure_pr_closes_issue(state["build"]["repository"], state["build"]["pr"], issue)
    store.mutate(lambda s: s["plan"].update({"source": "issue", "durable_issue": issue,
                                              "promotion_nonce": None}),
                 from_revision=state["revision"])
    print(f"promoted exact plan {state['plan']['digest']} to Issue #{issue}")


def _reset_after_revision(state: dict, plan: dict) -> None:
    state["plan"].update({"digest": _digest(plan), "intent_digest": _digest(plan["raw_intent"].encode()),
                          "spec_digest": None, "profile": plan["profile"], "bound_head": _head(),
                          "promotion_nonce": None})
    state["approval"] = None
    state["reviews"] = {"plan": _empty_review(), "deliverable": _empty_review()}
    state["findings"] = []
    # Assumption dispositions are cycle-bound evidence, like findings and review receipts: a plan revision
    # re-authors the premises and re-reviews from scratch, so a stale disposition must not survive to silently
    # re-clear a wall the freshly-authored plan re-opens (StarshipSuperjam/engine-template#1014). A depth-only
    # change keeps the plan digest and does NOT reach this reset, so a disposition rightly survives it.
    state.pop("assumption_dispositions", None)
    state["progress"] = {"current_item": None, "completed": []}
    if "work" in state:
        state["work"] = {}
    state["checkpoint"] = state["validation"] = state["repair"] = state["pr_contract"] = None
    state["preflights"] = []
    state["checkout_snapshot"] = None


def cmd_plan_revise(args, store: StateStore) -> None:
    plan = _plan(args.input)
    state = store.read()
    if _digest(plan) == state["plan"]["digest"]:
        print("plan content is unchanged; existing evidence remains current")
        return
    durable = state["plan"]["source"] == "issue"
    if durable:
        if not args.ack_visibility:
            raise CoordinatorError("revising a durable plan updates the Issue body; pass --ack-visibility")
    def change(current):
        if durable:
            _publish_issue(current["build"]["repository"], current["plan"]["durable_issue"], plan)
        _reset_after_revision(current, plan)
    store.mutate(change, from_revision=state["revision"])
    print(f"revised plan to {_digest(plan)}; approval and review evidence were cleared")


def cmd_approve(args, store: StateStore) -> None:
    plan = _plan(args.plan)
    state = store.read()
    _assert_plan(state, plan)
    if plan["profile"] == "trivial" and args.depth != "quick":
        raise CoordinatorError("the trivial one-glance profile requires quick depth")
    canonical_spec = _canonical_spec(plan, repository=state["build"]["repository"], check_issue=True)
    def change(state):
        _assert_plan(state, plan)
        if state["approval"] and state["approval"]["depth"] != args.depth:
            state["reviews"] = {"plan": _empty_review(), "deliverable": _empty_review()}
            state["findings"] = []
            state["validation"] = state["repair"] = state["pr_contract"] = None
            state["preflights"] = []
            state["checkout_snapshot"] = None
        state["plan"]["spec_digest"] = canonical_spec["digest"]
        state["approval"] = {"plan_digest": state["plan"]["digest"], "spec_digest": canonical_spec["digest"], "depth": args.depth}
    store.mutate(change, from_revision=state["revision"])
    print(f"approved plan and {args.depth} review depth")


def cmd_status(args, store: StateStore) -> None:
    state = store.read()
    plan = None
    if args.plan:
        plan = _plan(args.plan)
        _assert_plan(state, plan)
    result = _status(state, plan)
    if args.json:
        # The reminder rides in the JSON payload too, so a session consuming --json (a documented status
        # path) still meets the submit-gate nudge (StarshipSuperjam/engine-template#1014).
        result["reminder"] = _COORDINATOR_OWNED_REMINDER
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    print(f"Phase: {result['phase']} (snapshot r{result['snapshot_revision']})")
    print(_COORDINATOR_OWNED_REMINDER)
    progress = result["progress"]
    if progress["total"]:
        print(f"Progress: {len(progress['completed'])} of {progress['total']} complete"
              + (f"; next {progress['next']}" if progress["next"] else "; all planned items complete"))
    for label, key in (("Missing evidence", "required_evidence"), ("Engineering judgment", "engineering_judgment"), ("Warnings", "warnings")):
        values = result[key]
        if values:
            print(label + ":")
            for value in values:
                print(f"  - {value}")
    if result["suggested_next"]:
        print("Suggested next: " + result["suggested_next"])
    if result["available_activities"]:
        print("Available activities (unordered):")
        for value in result["available_activities"]:
            print(f"  - {value}")
    if "work" in result:
        w = result["work"]
        print(f"Work graph: {w['slots_in_use']} of {w['max_concurrency']} worker slot(s) in use")
        print("  ready (unordered): " + (", ".join(w["ready"]) or "none"))
        print("  claimable now: " + (", ".join(w["claimable"]) or "none"))
        for node_id in sorted(w["nodes"]):
            node = w["nodes"][node_id]
            line = f"  {node_id}: {node['state']} (attempt {node['attempt_count']})"
            if node["reasons"]:
                line += " — " + "; ".join(node["reasons"])
            route = node.get("route")
            if node["state"] == "claimed" and route:
                line += f" [route {route.get('provider')}/{route.get('model')}]"
            if node["state"] == "complete" and node.get("integration_commit"):
                line += f" [integrated {node['integration_commit'][:12]}]"
            failure = node.get("failure")
            if failure and failure.get("reason"):
                # The reason is untrusted free text (a worker's self-report or a pasted trace):
                # collapse it to one bounded line so the per-node render stays legible; the full
                # text is always available via --json.
                reason = " ".join(str(failure["reason"]).split())
                if len(reason) > 160:
                    reason = reason[:157] + "..."
                line += f" [failure: {reason}]"
            print(line)
        if w["resource_holders"]:
            print("  resources held by: " + ", ".join(sorted(w["resource_holders"])))


def cmd_depths(args, store: "StateStore | None") -> None:
    """Advisory, read-only: which review depths are worth OFFERING for this repo's installed reviewer roster,
    and the reviewer effort each resolves to. Consulted before `approve` so the operator is never asked to pick
    a depth that buys nothing over a lighter one (StarshipSuperjam/engine-template#763). The exact offer rule
    (a depth runs at least one lens the last lighter offered depth does not, or the same non-empty lens-set at
    higher effort) is single-homed in `build_coordinator_review.available_depths`, which this delegates to;
    `quick` is always offered (the floor). This shapes the consent surface only — `required()`/`approve`
    remain the sole mechanical lens authority, so a collapsed depth bound anyway still resolves to quick's roster.
    Needs no Build state (it reads the protocol, the installed personas, and the shipped/operator effort)."""
    import agent_bindings  # lazy: keep the coordinator's common path import-light, as cmd_validate does
    protocol = _protocol()
    plan_roster = _installed("plan")
    deliverable_roster = _installed("deliverable")
    bindings = _bindings()
    efforts = {depth: agent_bindings.depth_effort(depth, bindings, root=str(ROOT))
               for depth in review.DEPTH_ORDER}
    offered = review.available_depths(protocol, plan_roster, deliverable_roster, efforts)
    detail = {}
    for depth in review.DEPTH_ORDER:
        detail[depth] = {
            "offered": depth in offered,
            "effort": efforts[depth],
            "plan_lenses": [item["lens"] for item in _required(protocol, "plan", depth, plan_roster)],
            "deliverable_lenses": [item["lens"] for item in _required(protocol, "deliverable", depth, deliverable_roster)],
        }
    if args.json:
        print(json.dumps({"available": offered, "depths": detail}, indent=2, sort_keys=True))
        return
    print("Available review depths (only those that add coverage or effort over a lighter one):")
    for depth in offered:
        d = detail[depth]
        if not d["plan_lenses"] and not d["deliverable_lenses"]:
            print(f"  {depth}: no cold reviewers — your own read plus the automatic checks")
        else:
            effort = d["effort"] or "session default"
            # Name the lenses, not just their count, so the operator can see WHICH reviewer a heavier depth adds.
            print(f"  {depth}: reviewer effort {effort}")
            print(f"      plan lenses: {', '.join(d['plan_lenses']) or 'none'}")
            print(f"      deliverable lenses: {', '.join(d['deliverable_lenses']) or 'none'}")
    collapsed = [depth for depth in review.DEPTH_ORDER if depth not in offered]
    if collapsed:
        print("Collapsed (adds nothing over a lighter depth, so not offered): " + ", ".join(collapsed))


def _write_json_artifact(prefix: str, value: Any) -> tuple[str, str]:
    return core.write_json_artifact(prefix, value)


def _emit_packet(packet: dict, args) -> None:
    if getattr(args, "json", None) is True or not hasattr(args, "output"):
        print(json.dumps(packet, indent=2, sort_keys=True))
        return
    path = getattr(args, "output", None)
    if path:
        core.write_private_path(Path(path), json.dumps(packet, indent=2, sort_keys=True) + "\n")
    else:
        path, _ = _write_json_artifact("build-review-packet", packet)
    print(f"review packet {packet['packet_digest']} written to {path}; "
          f"{len(packet['required_lenses'])} required lens(es), commit {packet.get('commit') or 'plan'}")


def _packet(args, store: StateStore | None) -> None:
    plan = _plan(args.plan)
    impact = json.loads(_input(args.impact)) if args.impact else {}
    protocol = _protocol()
    stage = args.stage
    roster_stage = "plan" if stage == "plan" else "deliverable"
    installed = [item if isinstance(item, dict) else {
        "lens": item, "path": f"test-reviewer/{item}.md", "digest": _digest(item.encode())
    } for item in _installed(roster_stage)]
    installed_names = [item["lens"] for item in installed]
    if getattr(args, "standalone", False):
        if stage == "repair":
            raise CoordinatorError("standalone packets support plan or deliverable review, not repair state")
        canonical_spec = _canonical_spec(plan, repository=args.repository, check_issue=True)
        required_contracts = _required(protocol, stage, args.depth, installed)
        required = [item["lens"] for item in required_contracts]
        commit = None if stage == "plan" else args.commit
        if stage == "deliverable" and (not commit or not args.base):
            raise CoordinatorError("standalone deliverable packets require --commit and --base")
        packet_root = Path.cwd().resolve()
        stable = core.StableCommit(packet_root, "standalone deliverable review-packet construction") if commit else None
        if stable:
            stable_head = stable.__enter__()
            try:
                if stable_head != commit:
                    raise CoordinatorError("standalone deliverable packet commit must equal the clean worktree HEAD")
                if core.run(["git", "cat-file", "-e", f"{args.base}^{{commit}}"], root=packet_root).returncode:
                    raise CoordinatorError("standalone deliverable packet base is not a commit in this worktree")
            except BaseException as exc:
                stable.__exit__(type(exc), exc, exc.__traceback__)
                raise
        referent = {"schema_version": "build-review-packet.v1", "stage": stage,
                  "raw_intent": plan["raw_intent"], "plan": plan, "plan_digest": _digest(plan),
                  "intent_digest": _digest(plan["raw_intent"].encode()), "spec": canonical_spec,
                  "commit": commit, "base_commit": args.base if commit else None, "impact": impact,
                  "protocol_digest": _digest(protocol), "installed_lenses": installed_names,
                  "required_lenses": required, "standalone": True}
        if stage == "deliverable":
            declarations = _hard_check_declarations()
            path, digest = _write_json_artifact("build-hard-check-declarations", declarations)
            referent["hard_check_declarations"] = {"digest": digest, "count": len(declarations),
                                                    "for_lens": "spec-conformance"}
        referent_digest = _digest(referent)
        packet = {**referent, "referent_digest": referent_digest,
                  "reviewer_contracts": review.lens_packets(referent_digest, required_contracts)}
        packet["packet_digest"] = _digest(packet)
        if stage == "deliverable":
            packet["artifacts"] = {"hard_check_declarations": {"path": path}}
        if stable:
            stable.__exit__(None, None, None)
        _emit_packet(packet, args)
        return
    if store is None:
        raise CoordinatorError("--state is required unless --standalone constructs a pre-PR review packet")
    state = store.read()
    revision = state["revision"]
    _assert_plan(state, plan)
    canonical_spec = _assert_spec_current(state, plan, check_issue=True)
    if not state["approval"]:
        raise CoordinatorError("approve the plan and depth before preparing review packets")
    if stage == "repair":
        repair = state["repair"]
        if not repair or repair["judgment"] == "none":
            raise CoordinatorError("a scoped or full repair assessment is required before a repair packet")
        required = repair["lenses"]
        required_contracts = [item for item in installed if item["lens"] in required]
        commit = repair["final_commit"]
        if not state["validation"] or state["validation"]["commit"] != commit or not all(x["passed"] for x in state["validation"]["results"]):
            raise CoordinatorError("green validation for the repaired commit is required before re-review")
    else:
        required_contracts = _required(protocol, stage, state["approval"]["depth"], installed)
        required = [item["lens"] for item in required_contracts]
        commit = None if stage == "plan" else _head()
        if stage == "deliverable" and (not state["validation"] or state["validation"]["commit"] != commit or not all(x["passed"] for x in state["validation"]["results"])):
            raise CoordinatorError("green validation for the current commit is required before deliverable review")
    stable = core.StableCommit(ROOT, f"{stage} review-packet construction") if commit else None
    if stable:
        stable_head = stable.__enter__()
        if stable_head != commit:
            raise CoordinatorError(f"{stage} review packet is not bound to the current clean commit")
    missing = [lens for lens in required if lens not in installed_names]
    if missing:
        raise CoordinatorError("required reviewers are not installed: " + ", ".join(missing))
    referent = {"schema_version": "build-review-packet.v1", "stage": stage, "raw_intent": plan["raw_intent"],
              "plan": plan, "plan_digest": state["plan"]["digest"], "intent_digest": state["plan"]["intent_digest"],
              "spec": canonical_spec, "commit": commit, "base_commit": _base() if commit else None,
              "impact": impact, "protocol_digest": _digest(protocol),
              "installed_lenses": installed_names, "required_lenses": required}
    if stage != "plan":
        declarations = _hard_check_declarations()
        path, digest = _write_json_artifact("build-hard-check-declarations", declarations)
        referent["hard_check_declarations"] = {"digest": digest, "count": len(declarations),
                                                "for_lens": "spec-conformance"}
    referent_digest = _digest(referent)
    contracts = review.lens_packets(referent_digest, required_contracts)
    packet = {**referent, "referent_digest": referent_digest, "reviewer_contracts": contracts}
    packet["packet_digest"] = _digest(packet)
    if stage != "plan":
        packet["artifacts"] = {"hard_check_declarations": {"path": path}}
    current = state["repair"] if stage == "repair" else state["reviews"][stage]
    unchanged = bool(current and current.get("packet_digest") == packet["packet_digest"])
    # Capture the checkout's git state as the review fan-out begins, so the submission preflight can
    # verify the deliverable/repair review did not mutate it (StarshipSuperjam/engine-template#947). Plan review runs before
    # implementation and drives no throwaway-copy execution, so it needs no baseline.
    checkout_baseline = review_integrity.snapshot(str(ROOT)) if stage != "plan" else None
    def change(s):
        old = s["repair"] if stage == "repair" else s["reviews"][stage]
        expected = {item["lens"]: item["lens_packet_digest"] for item in contracts}
        preserved_receipts = [receipt for receipt in (old or {}).get("receipts", [])
                              if receipt["lens"] in expected
                              and receipt.get("lens_packet_digest") == expected[receipt["lens"]]]
        preserved_packets = {receipt["packet_digest"] for receipt in preserved_receipts}
        if stage == "repair":
            s["repair"]["packet_digest"] = packet["packet_digest"]
            s["repair"]["referent_digest"] = referent_digest
            s["repair"]["reviewer_contracts"] = contracts
            s["repair"]["receipts"] = preserved_receipts
            s["findings"] = [f for f in s["findings"] if f["stage"] != "repair"
                             or f["packet_digest"] in preserved_packets]
        else:
            target = s["reviews"][stage]
            target.update({"packet_digest": packet["packet_digest"], "referent_digest": referent_digest,
                           "required_lenses": required, "installed_lenses": installed_names,
                           "reviewer_contracts": contracts, "receipts": preserved_receipts, "reviewed_commit": commit,
                           "base_commit": packet["base_commit"], "waiver": None})
            s["findings"] = [f for f in s["findings"] if f["stage"] != stage
                             or f["packet_digest"] in preserved_packets]
        if checkout_baseline is not None:
            s["checkout_snapshot"] = checkout_baseline
    if stable:
        stable.__exit__(None, None, None)
    if not unchanged:
        store.mutate(change, from_revision=revision)
    elif checkout_baseline is not None:
        # The packet is unchanged, so receipts and findings stand — but a re-issue marks a fresh review
        # fan-out, so refresh the checkout baseline to now (the documented "re-captured at the next review
        # packet"); otherwise the preflight would compare against a stale, arbitrarily-old baseline.
        store.mutate(lambda s: s.update({"checkout_snapshot": checkout_baseline}), from_revision=revision)
    _emit_packet(packet, args)


def cmd_review_waive(args, store: StateStore) -> None:
    if args.stage != "plan":
        raise CoordinatorError("only retrospective plan review may be waived; deliverable review remains required")
    def change(state):
        if not state["approval"]:
            raise CoordinatorError("approve the Build gate before recording an operator review waiver")
        stage = state["reviews"]["plan"]
        if stage["packet_digest"] or stage["receipts"] or any(f["stage"] == "plan" for f in state["findings"]):
            raise CoordinatorError("plan review already started; disposition its findings instead of erasing evidence with a waiver")
        if state["build"]["mode"] != "same-session" or state["plan"].get("profile") != "normal":
            raise CoordinatorError("retrospective plan-review waiver is limited to same-session normal adoption")
        if args.adopted_commit != state["plan"].get("bound_head") or args.adopted_commit != _head():
            raise CoordinatorError("the waiver must bind the plan-bound adopted commit at current HEAD")
        if state["progress"]["completed"] or state["checkpoint"]:
            raise CoordinatorError("the Build has prospective coordinator progress; retrospective waiver is not applicable")
        adoption_diff = _run(["git", "diff", "--quiet", f"{state['build']['base_at_bind']}..{args.adopted_commit}"])
        if adoption_diff.returncode == 0:
            raise CoordinatorError("retrospective waiver requires already-existing implementation to adopt")
        if adoption_diff.returncode != 1:
            raise CoordinatorError("could not verify the retrospective adoption diff")
        stage.update({"packet_digest": None, "referent_digest": None,
                      "required_lenses": [], "installed_lenses": [], "reviewer_contracts": [],
                      "receipts": [], "reviewed_commit": None, "base_commit": None,
                      "waiver": {"plan_digest": state["plan"]["digest"],
                                 "depth": state["approval"]["depth"], "reason": args.reason,
                                 "adopted_commit": args.adopted_commit}})
        state["findings"] = [f for f in state["findings"] if f["stage"] != "plan"]
    store.mutate(change)
    print("recorded explicit operator waiver of retrospective plan review; no review receipt was synthesized")


def cmd_review_record(args, store: StateStore) -> None:
    finding_ids = sorted(set(args.finding or []))
    def change(state):
        if args.stage == "repair":
            target = state["repair"]
            if not target or target["packet_digest"] != args.packet_digest:
                raise CoordinatorError("repair receipt does not match the current repair packet")
            if args.lens not in target["lenses"]:
                raise CoordinatorError(f"{args.lens} was not requested for this repair review")
            contract = next((item for item in target["reviewer_contracts"] if item["lens"] == args.lens), None)
            if not contract:
                raise CoordinatorError(f"no current reviewer contract for {args.lens}")
            if getattr(args, "lens_packet_digest", None) != contract["lens_packet_digest"]:
                raise CoordinatorError("review receipt does not attest the current reviewer contract packet")
            receipt = {"lens": args.lens, "packet_digest": args.packet_digest,
                       "referent_digest": target["referent_digest"],
                       "lens_packet_digest": contract["lens_packet_digest"],
                       "commit": target["final_commit"], "finding_ids": finding_ids,
                       "code_execution": args.code_execution}
            target["receipts"] = [r for r in target["receipts"] if r["lens"] != args.lens] + [receipt]
            delivery = state["reviews"]["deliverable"]
            delivery["receipts"] = [r for r in delivery["receipts"] if r["lens"] != args.lens] + [receipt]
            delivery["reviewer_contracts"] = [
                item for item in delivery["reviewer_contracts"] if item["lens"] != args.lens
            ] + [contract]
            if not [lens for lens in target["lenses"] if lens not in {r["lens"] for r in target["receipts"]}]:
                delivery["reviewed_commit"] = target["final_commit"]
        else:
            target = state["reviews"][args.stage]
            if target["packet_digest"] != args.packet_digest:
                raise CoordinatorError("receipt does not match the current review packet")
            if args.lens not in target["required_lenses"]:
                raise CoordinatorError(f"{args.lens} was not required by the approved depth")
            contract = next((item for item in target["reviewer_contracts"] if item["lens"] == args.lens), None)
            if not contract:
                raise CoordinatorError(f"no current reviewer contract for {args.lens}")
            if getattr(args, "lens_packet_digest", None) != contract["lens_packet_digest"]:
                raise CoordinatorError("review receipt does not attest the current reviewer contract packet")
            receipt = {"lens": args.lens, "packet_digest": args.packet_digest,
                       "referent_digest": target["referent_digest"],
                       "lens_packet_digest": contract["lens_packet_digest"],
                       "commit": target["reviewed_commit"], "finding_ids": finding_ids,
                       "code_execution": args.code_execution}
            target["receipts"] = [r for r in target["receipts"] if r["lens"] != args.lens] + [receipt]
    store.mutate(change)
    print(f"recorded {args.stage} review from {args.lens} with {len(finding_ids)} finding(s)")


def cmd_finding_record(args, store: StateStore) -> None:
    if args.disposition == "escalated" and not args.escalation_kind:
        raise CoordinatorError("an escalated finding must name the operator-owned decision boundary")
    if args.disposition != "escalated" and args.escalation_kind:
        raise CoordinatorError("only an escalated finding may name an escalation boundary")
    operator_summary = getattr(args, "operator_summary", None)
    private_reference = getattr(args, "private_reference", None)
    if args.severity == "blocking" and not args.blocks_this_pr and not operator_summary:
        raise CoordinatorError("a downgraded blocking finding needs a safe operator-facing --operator-summary")
    def change(state):
        target = state["repair"] if args.stage == "repair" and state["repair"] else state["reviews"][args.stage]
        packet = target["packet_digest"]
        if not packet:
            raise CoordinatorError(f"no current {args.stage} review packet")
        requested = target["lenses"] if args.stage == "repair" else target["required_lenses"]
        if args.lens not in requested:
            raise CoordinatorError(f"{args.lens} was not requested by the current {args.stage} packet")
        contract = next((item for item in target["reviewer_contracts"] if item["lens"] == args.lens), None)
        if not contract:
            raise CoordinatorError(f"no current reviewer contract for {args.lens}")
        commit = None if args.stage == "plan" else (target["final_commit"] if args.stage == "repair" else target["reviewed_commit"])
        finding = {"id": args.id, "stage": args.stage, "lens": args.lens, "packet_digest": packet,
                   "lens_packet_digest": contract["lens_packet_digest"],
                   "commit": commit, "severity": args.severity,
                   "summary": args.summary, "disposition": args.disposition, "rationale": args.rationale,
                   "escalation_kind": args.escalation_kind,
                   "blocks_this_pr": args.blocks_this_pr, "handoff_summary": args.handoff_summary,
                   "operator_summary": operator_summary, "private_reference": private_reference}
        state["findings"] = [f for f in state["findings"] if f["id"] != args.id] + [finding]
    store.mutate(change)
    print(f"recorded disposition for {args.id}; reviewer severity did not choose the remedy")


def cmd_assumption_dispose(args, store: StateStore) -> None:
    """Resolve a plan assumption authored 'unresolved' to 'verified' or 'accepted-risk' in the receipt layer,
    bound to a --basis, WITHOUT editing the plan (StarshipSuperjam/engine-template#1014). Mirrors
    cmd_finding_record: store.mutate never touches state['plan']['digest'], so approval and every review
    receipt survive — the honest submit path no longer forces a full review re-run to clear a wall the review
    already settled. The disposition is disclosed at merge (see _status); it is a self-attested judgment, not a
    silent skip, and the operator's merge stays the binding gate."""
    plan = _plan(args.plan)
    state = store.read()
    _assert_plan(state, plan)
    claim = args.claim.strip()
    if not claim:
        raise CoordinatorError("an assumption disposition needs a non-empty --claim")
    if not args.basis.strip():
        raise CoordinatorError("a disposition needs a --basis stating how the assumption was resolved")
    authored = {item["claim"]: item["status"] for item in plan.get("assumptions", [])}
    if claim not in authored:
        raise CoordinatorError(
            "no assumption with that exact claim in the approved plan (match the claim text verbatim): " + claim)
    if authored[claim] != "unresolved":
        raise CoordinatorError(
            f"this assumption is already authored '{authored[claim]}' in the approved plan — it is not "
            f"unresolved, so there is nothing to dispose and no action is needed. (Disposition only clears an "
            f"assumption authored 'unresolved'. To change a premise the plan already settled is a real plan "
            f"change — 'plan revise' — which correctly re-opens approval and review.)")

    def change(state):
        entry = {"claim": claim, "resolved_as": args.resolved_as, "basis": args.basis.strip()}
        kept = [d for d in state.get("assumption_dispositions", []) if d["claim"] != claim]
        state["assumption_dispositions"] = kept + [entry]

    store.mutate(change)
    print(f"resolved assumption after approval: {claim} -> {args.resolved_as}; it clears the "
          "engineering-decision hold without re-running review, and is disclosed at merge")


def _changed_paths(base: str) -> list[str]:
    paths = set(_must_run(["git", "diff", "--name-only", f"{base}..HEAD"]).splitlines())
    paths.update(_must_run(["git", "diff", "--name-only"]).splitlines())
    paths.update(_must_run(["git", "diff", "--cached", "--name-only"]).splitlines())
    return sorted(x for x in paths if x)


def cmd_checkpoint(args, store: StateStore) -> None:
    plan = _plan(args.plan)
    try:
        note = json.loads(_input(args.input))
    except ValueError as exc:
        raise CoordinatorError(f"checkpoint input is not JSON: {exc}") from exc
    required = {"current_work", "work_item", "remaining_verification", "judgment"}
    if plan["profile"] != "trivial":
        required.update({"objective", "assumptions", "non_goals", "planned_scope"})
    if not required.issubset(note):
        raise CoordinatorError("checkpoint is missing: " + ", ".join(sorted(required - set(note))))
    def change(state):
        _assert_plan(state, plan)
        _assert_spec_boundary(state, plan, allow_same_session_offline=True)
        if not state["approval"]:
            raise CoordinatorError("the Build gate is not approved")
        plan_ready, missing_review = _plan_review_ready(state, plan)
        if not plan_ready:
            raise CoordinatorError("implementation cannot begin before plan review: " + "; ".join(missing_review))
        items = {item["id"]: item for item in plan["work_items"]}
        if note["work_item"] not in items:
            raise CoordinatorError(f"checkpoint work item {note['work_item']} is not in the approved plan")
        completed = {item["id"] for item in state["progress"]["completed"]}
        next_item = _next_incomplete(plan, state)
        # v1 keeps its exact historical wording; a v2 graph's "next" is dependency READINESS, so the
        # refusal names the same concept the operation doc and status render use.
        noun = "ready" if _plan_version(plan) == "build-plan.v2" else "incomplete"
        if plan["profile"] == "routine" and next_item and note["work_item"] != next_item:
            raise CoordinatorError(f"Routine must advance the next {noun} work item {next_item}")
        if args.complete_item:
            if args.complete_item not in items:
                raise CoordinatorError(f"completed work item {args.complete_item} is not in the approved plan")
            if args.complete_item not in completed:
                if plan["profile"] == "routine" and args.complete_item != next_item:
                    raise CoordinatorError(f"Routine must complete the next {noun} work item {next_item}")
                state["progress"]["completed"].append({"id": args.complete_item, "commit": _head()})
                completed.add(args.complete_item)
        state["progress"]["current_item"] = note["work_item"]
        note.setdefault("objective", plan["objective"])
        note.setdefault("assumptions", [])
        note.setdefault("non_goals", [])
        note.setdefault("planned_scope", items[note["work_item"]]["paths"])
        note.update({"plan_digest": state["plan"]["digest"], "changed_paths": _changed_paths(state["build"]["base_at_bind"]),
                     "progress": f"{len(completed)} of {len(items)} planned work items complete"})
        state["checkpoint"] = note
    store.mutate(change)
    if getattr(args, "json", False):
        # Carry the reminder in the JSON payload too (see cmd_status); the stored checkpoint note is
        # unchanged — this is a display-only field for a --json consumer.
        print(json.dumps({**note, "coordinator_reminder": _COORDINATOR_OWNED_REMINDER}, indent=2, sort_keys=True))
    else:
        print(f"checkpoint {note['judgment']}: {note['work_item']}; {note['progress']}; "
              f"{len(note['changed_paths'])} changed path(s), {len(note['remaining_verification'])} verification item(s) remain")
        print(_COORDINATOR_OWNED_REMINDER)


def cmd_validate(args, store: StateStore) -> None:
    state = store.read()
    revision = state["revision"]
    plan_ready, missing_review = _plan_review_ready(state, {"profile": state["plan"]["profile"]})
    if not plan_ready:
        raise CoordinatorError("final validation cannot become evidence before plan review: " + "; ".join(missing_review))
    results = []
    with core.StableCommit(ROOT, "validation") as head:
        for item in _protocol()["validation_commands"]:
            stamp = f"{int(time.time())}-{item['id']}-{head[:12]}-{secrets.token_hex(6)}.log"
            log_path = Path(__import__("tempfile").gettempdir()) / stamp
            returncode = _run_validation(item["command"], log_path)
            log_digest = _digest(log_path.read_bytes())
            summary = f"exit {returncode}; complete log at {log_path} ({log_digest})"
            results.append({"id": item["id"], "commit": head, "passed": returncode == 0,
                            "summary": summary, "log_path": str(log_path), "log_digest": log_digest})
    store.mutate(lambda s: s.update({"validation": {"commit": head, "results": results}}),
                 from_revision=revision)
    print(json.dumps({"commit": head, "results": results}, indent=2, sort_keys=True))
    if not all(x["passed"] for x in results):
        raise CoordinatorError("validation failed; the failed results remain recorded")


def cmd_repair_assess(args, store: StateStore) -> None:
    head = _head()
    state = store.read()
    revision = state["revision"]
    reviewed = state["reviews"]["deliverable"]["reviewed_commit"]
    prior = state["repair"]
    if prior and prior["judgment"] != "none" and not [
        lens for lens in prior["lenses"] if lens not in {receipt["lens"] for receipt in prior["receipts"]}
    ]:
        reviewed = prior["final_commit"]
    if not reviewed:
        raise CoordinatorError("deliverable review has not recorded a reviewed commit")
    summary = _must_run(["git", "diff", "--shortstat", f"{reviewed}..{head}"]).strip() or "no textual diff"
    lenses = sorted(set(args.lens or []))
    if args.judgment == "none" and lenses:
        raise CoordinatorError("a none judgment cannot request review lenses")
    if args.judgment == "scoped" and not lenses:
        raise CoordinatorError("a scoped judgment must name at least one --lens")
    if args.judgment == "full":
        lenses = [item["lens"] for item in _required(_protocol(), "deliverable", "thorough", _installed("deliverable"))]
    repair = {"reviewed_commit": reviewed, "final_commit": head, "summary": summary, "judgment": args.judgment,
              "rationale": args.rationale, "lenses": lenses, "packet_digest": None,
              "referent_digest": None, "reviewer_contracts": [], "receipts": []}
    store.mutate(lambda s: s.update({"repair": repair}), from_revision=revision)
    print(json.dumps(repair, indent=2, sort_keys=True))


def _pr_contract(body: str) -> tuple[bool, str]:
    sys.path.insert(0, str(ROOT / ".engine" / "tools"))
    import validate
    rule = _json(ROOT / ".engine" / "check" / "pr-body-completeness.json")
    verdict, findings = validate.kind_presence(rule, {"pr_body": body})
    return verdict, "; ".join(f["message"] for f in findings) or "all required sections and consent anchors are filled"


def _compute_preflight_legs(state: dict, head: str, pr_data: dict, body: str) -> dict:
    """The six submission-preflight legs over a candidate body, as pure computation that never raises.

    Single-homed so the two callers cannot drift: `cmd_preflight` records the results and raises on a
    failed required leg (`pr-contract`, `checkout-integrity`); `contract apply` loops on the results while
    it drives the body to a fixed point (where an intermediate pass may legitimately fail `pr-contract`
    before the folded advisory lines and defang settle). Returns the results list, the required-leg
    verdicts and their summaries, and the applicable hard-check declarations."""
    repo, pr = state["build"]["repository"], state["build"]["pr"]
    base = pr_data.get("baseRefOid") or state["build"]["base_at_bind"]
    close = _run([sys.executable, str(ROOT / ".engine" / "tools" / "close_linkage_preflight.py"), "check", "--pr", str(pr), "--base", base, "--head", head])
    close_passed = close.returncode == 0
    close_summary = (close.stdout or close.stderr or "no close-linkage output").strip()
    contract_passed, contract_summary = _pr_contract(body)
    missing_disagreements = [line for line in review.required_disagreement_lines(state) if line not in body]
    if missing_disagreements:
        contract_passed = False
        ids = [re.search(r"`([^`]+)`", line).group(1) for line in missing_disagreements]
        contract_summary += "; missing reviewer disagreement disclosure: " + ", ".join(ids)
    profile = _run([sys.executable, str(ROOT / ".engine" / "tools" / "scope_profile.py"), base])
    profile_summary = (profile.stdout or profile.stderr or "no scope-profile output").strip()
    declarations = _hard_check_declarations()
    declaration_path, declaration_digest = _write_json_artifact("build-hard-check-declarations", declarations)
    # Checkout-integrity (StarshipSuperjam/engine-template#947): a required preflight that verifies the review fan-out did
    # not mutate the build checkout's git state, comparing against the snapshot captured when the
    # deliverable/repair review packet was created. `head` and `worktrees` are ignored — repair commits
    # legitimately advance HEAD and a concurrent peer may add a worktree — leaving origin, branch, and
    # stash, none of which a review ever legitimately changes. Inert (passes) until a baseline exists.
    ci_snap = state.get("checkout_snapshot")
    if ci_snap:
        ci = review_integrity.verify(str(ROOT), ci_snap, ignore={"head", "worktrees"})
        ci_passed = not ci["mutated"]
        ci_summary = ("checkout origin, branch, and stash unchanged since the review packet"
                      if ci_passed else "; ".join(ci["changes"]))
        # Advisory (non-blocking): worktree-registry drift is the other half of incident 2, but a
        # concurrent peer session may legitimately add a worktree to the shared checkout, so it is
        # SURFACED here, never used to block — the required leg above stays free of that false positive.
        wt_changes = review_integrity.compare(ci_snap, ci["after"],
                                              ignore={"origin", "branch", "head", "stash"})
        wt_passed = not wt_changes
        wt_summary = ("worktree registry unchanged since the review packet"
                      if wt_passed else "; ".join(wt_changes))
    else:
        ci_passed = wt_passed = True
        ci_summary = wt_summary = "no review-packet checkout snapshot captured (nothing to verify)"
    results = [
        {"id": "close-linkage", "commit": head, "passed": close_passed, "summary": close_summary},
        {"id": "pr-contract", "commit": head, "passed": contract_passed, "summary": contract_summary},
        {"id": "scope-profile", "commit": head,
         "passed": profile.returncode == 0 and "could not read the diff" not in profile_summary.lower(),
         "summary": profile_summary},
        {"id": "hard-check-declarations", "commit": head, "passed": True,
         "summary": (f"{len(declarations)} applicable declaration(s) at {declaration_path} "
                     f"({declaration_digest})" if declarations else "no hard-check declarations apply")},
        {"id": "checkout-integrity", "commit": head, "passed": ci_passed, "summary": ci_summary},
        {"id": "checkout-worktrees", "commit": head, "passed": wt_passed, "summary": wt_summary},
    ]
    return {"results": results, "contract_passed": contract_passed, "contract_summary": contract_summary,
            "ci_passed": ci_passed, "ci_summary": ci_summary, "declarations": declarations}


def cmd_preflight(args, store: StateStore) -> None:
    state = store.read()
    revision = state["revision"]
    repo, pr = state["build"]["repository"], state["build"]["pr"]
    with core.StableCommit(ROOT, "submission preflight") as head:
        pr_data = _verify_draft(repo, pr)
        body = pr_data.get("body") or ""
        if args.pr_body and _input(args.pr_body) != body:
            raise CoordinatorError("the supplied PR body is not the body currently on GitHub")
        legs = _compute_preflight_legs(state, head, pr_data, body)
        results = legs["results"]
        contract_passed, ci_passed, ci_summary = legs["contract_passed"], legs["ci_passed"], legs["ci_summary"]
        declarations = legs["declarations"]

        def change(s):
            s["preflights"] = results
            s["pr_contract"] = {"commit": head, "body_digest": _digest(body.encode()), "complete": contract_passed}
    store.mutate(change, from_revision=revision)
    if getattr(args, "json", False):
        print(json.dumps(results, indent=2, sort_keys=True))
    else:
        required_ids = {"pr-contract", "checkout-integrity"}
        required_failures = [item["id"] for item in results if item["id"] in required_ids and not item["passed"]]
        advisory = [item["id"] for item in results if item["id"] not in required_ids and not item["passed"]]
        print(f"preflight recorded for {head[:12]}: required failures {len(required_failures)}, "
              f"advisory findings {len(advisory)}, {len(declarations)} applicable hard-check declaration(s)")
    if not contract_passed:
        raise CoordinatorError("the required PR-contract preflight needs attention")
    if not ci_passed:
        raise CoordinatorError("the checkout-integrity preflight failed — a review pass appears to have "
                               "mutated the build checkout: " + ci_summary)


def _bounded_work(work_map: dict) -> dict:
    """The BOUNDED work projection for a durable handoff, published into the PR body.

    Identifiers, digests, outcomes, and repo-relative changed paths travel — they are what a cold
    resume needs to re-derive every node's state. Local filesystem paths and unreviewed worker
    free-text (verification output, assumptions, concerns, failure reasons) are redacted with the
    same discipline the repair rationale and finding summaries already get: the PR body is a public
    surface, and evidence prose reaches it only through an explicitly reviewed summary.
    """
    redacted = "redacted from durable handoff"
    bounded = {}
    for node_id, nw in (work_map or {}).items():
        nw = json.loads(json.dumps(nw))
        claim = nw.get("claim")
        if claim:
            claim["worktree"] = redacted
        result = nw.get("latest_result")
        if result:
            if result.get("artifact_ref"):
                result["artifact_ref"] = redacted
            evidence = result.get("evidence") or {}
            for key in ("verification_results", "assumptions", "unresolved_concerns"):
                if evidence.get(key):
                    evidence[key] = [redacted]
        failure = nw.get("latest_failure")
        if failure and failure.get("reason"):
            failure["reason"] = redacted
        integration = nw.get("integration")
        if integration and integration.get("focused_verification"):
            # Also free text (typed at `work integrate`); like the repair rationale, it reaches the
            # PR body only through an explicitly authored summary, never verbatim.
            integration["focused_verification"] = redacted
        bounded[node_id] = nw
    return bounded


def _handoff(state: dict) -> dict:
    if state["plan"]["source"] != "issue" or not state["plan"]["durable_issue"]:
        raise CoordinatorError("promote the exact plan to a suitable Issue before cold-session handoff")
    summaries = []
    for finding in state["findings"]:
        if not finding["handoff_summary"]:
            raise CoordinatorError(f"finding {finding['id']} needs a non-sensitive --handoff-summary")
        # `private_reference` is deliberately NOT carried here: it is reviewer-internal detail
        # (StarshipSuperjam/engine-template#981) and the handoff schema now forbids it. It stays local
        # in build-state; this handoff is a redacted, publishable projection — like the stripped repair
        # rationale and worker free-text — so only the operator-safe summaries reach the PR body.
        summaries.append({"id": finding["id"], "stage": finding["stage"], "lens": finding["lens"],
                          "packet_digest": finding["packet_digest"],
                          "lens_packet_digest": finding.get("lens_packet_digest", finding["packet_digest"]), "commit": finding["commit"],
                          "severity": finding["severity"], "disposition": finding["disposition"], "escalation_kind": finding["escalation_kind"],
                          "blocks_this_pr": finding["blocks_this_pr"], "summary": finding["handoff_summary"],
                          "operator_summary": finding.get("operator_summary")})
    validation = None if not state["validation"] else {
        "commit": state["validation"]["commit"],
        "results": [{"id": x["id"], "commit": x["commit"], "passed": x["passed"],
                     **({"log_digest": x["log_digest"]} if x.get("log_digest") else {})}
                    for x in state["validation"]["results"]],
    }
    repair = None if not state["repair"] else {k: v for k, v in state["repair"].items() if k != "rationale"}
    preflights = [{"id": x["id"], "commit": x["commit"], "passed": x["passed"]} for x in state["preflights"]]
    is_v2 = state.get("schema_version") == "build-state.v2"
    value = {"schema_version": "build-handoff.v2" if is_v2 else "build-handoff.v1",
             "build": state["build"], "plan": state["plan"],
             "approval": state["approval"], "reviews": state["reviews"], "finding_summaries": summaries,
             "progress": state["progress"], "validation": validation, "repair": repair, "preflights": preflights,
             "pr_contract": state["pr_contract"]}
    if is_v2:
        value["work"] = _bounded_work(state.get("work", {}))
    _validate(value, HANDOFF_SCHEMA_V2 if is_v2 else HANDOFF_SCHEMA)
    return value


def cmd_handoff_export(args, store: StateStore) -> None:
    state = store.read()
    revision = state["revision"]
    if state["plan"]["source"] != "issue" or not state["plan"]["durable_issue"]:
        raise CoordinatorError("promote the exact plan to a suitable Issue before cold-session handoff")
    durable = _durable_plan(_issue_body(state["build"]["repository"], state["plan"]["durable_issue"]))
    _assert_plan(state, durable)
    _assert_spec_boundary(state, durable)
    value = _handoff(state)
    rendered = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if args.publish:
        if not args.ack_visibility:
            raise CoordinatorError("handoff publication places redacted evidence in the PR contract; pass --ack-visibility")
        repo, pr = value["build"]["repository"], value["build"]["pr"]
        before = _verify_draft(repo, pr).get("body") or ""
        after = github.replace_handoff_block(before, value)
        if store.read()["revision"] != revision:
            raise CoordinatorError("Build evidence changed while preparing handoff; rerun export")
        if (_verify_draft(repo, pr).get("body") or "") != before:
            raise CoordinatorError("PR contract changed while preparing handoff; no write was made")
        _must_run(["gh", "pr", "edit", str(pr), "--repo", repo, "--body-file", "-"], input_text=after)
        confirmed = _verify_draft(repo, pr).get("body") or ""
        if confirmed != after:
            raise CoordinatorError("GitHub did not preserve the exact handoff block")
        if store.read()["revision"] != revision:
            latest = _verify_draft(repo, pr).get("body") or ""
            if latest == after:
                _must_run(["gh", "pr", "edit", str(pr), "--repo", repo, "--body-file", "-"], input_text=before)
                if (_verify_draft(repo, pr).get("body") or "") != before:
                    raise CoordinatorError("Build evidence changed during handoff publication and the stale block could not be rolled back")
            raise CoordinatorError("Build evidence changed during handoff publication; the stale block was rolled back")
        print(f"published bounded handoff snapshot in {repo}#{pr}")
        return
    if args.output == "-":
        print(rendered, end="")
    else:
        core.write_private_path(Path(args.output), rendered)
        print(f"wrote bounded handoff snapshot to {args.output}")


def _restore_results(results: list[dict]) -> list[dict]:
    return [{**item, "summary": "details redacted from durable handoff"} for item in results]


def _restore_result_set(result_set: dict | None) -> dict | None:
    if not result_set:
        return None
    return {"commit": result_set["commit"], "results": _restore_results(result_set["results"])}


def _restore_repair(repair: dict | None) -> dict | None:
    if not repair:
        return None
    return {**repair, "rationale": "private rationale redacted from durable handoff"}


def _restore_base_state(value: dict, schema_version: str) -> dict:
    return {"schema_version": schema_version, "revision": 1, "build": value["build"], "plan": value["plan"],
            "approval": value["approval"], "reviews": value["reviews"],
            "findings": [{"id": f["id"], "stage": f["stage"], "lens": f["lens"], "packet_digest": f["packet_digest"],
                          "lens_packet_digest": f["lens_packet_digest"],
                          "commit": f["commit"], "severity": f["severity"], "summary": f["summary"], "disposition": f["disposition"],
                          "rationale": f["summary"], "escalation_kind": f["escalation_kind"],
                          "blocks_this_pr": f["blocks_this_pr"], "handoff_summary": f["summary"],
                          "operator_summary": f["operator_summary"], "private_reference": f.get("private_reference")}
                         for f in value["finding_summaries"]],
            "checkpoint": None, "progress": value["progress"], "validation": _restore_result_set(value["validation"]),
            "repair": _restore_repair(value["repair"]), "preflights": _restore_results(value["preflights"]),
            "pr_contract": value["pr_contract"], "submission": "draft", "checkout_snapshot": None}


def _restore_work(work_map: dict) -> dict:
    """Reconstruct the per-node work map, marking any genuinely unfinished claim as restored.

    A claim present without an integration is uncertain after a cold resume, so it derives
    recovery_required and is never treated as still-running or auto-expired. A claim whose attempt
    already has a bound RETURNED result is not uncertain — the worker finished and the node awaits
    integrator inspection exactly as before the handoff, so it keeps deriving returned rather than
    masking complete evidence behind a recovery flag.
    """
    restored = {}
    for node_id, nw in (work_map or {}).items():
        nw = dict(nw)
        claim = nw.get("claim")
        if claim and not nw.get("integration"):
            result = nw.get("latest_result")
            returned = bool(result and result.get("outcome") == "returned"
                            and result.get("attempt_id") == claim.get("attempt_id"))
            if not returned:
                claim = dict(claim)
                claim["restored"] = True
                nw["claim"] = claim
        restored[node_id] = nw
    return restored


def cmd_handoff_restore(args, store: StateStore) -> None:
    if args.input:
        rendered = _input(args.input)
        value = json.loads(rendered)
    else:
        if not args.repository or not args.pr:
            raise CoordinatorError("restore needs --input or both --repository and --pr")
        body = _gh_json(["pr", "view", str(args.pr), "--repo", args.repository, "--json", "body"]).get("body") or ""
        present = [(block, sv) for block, sv in
                   ((github.find_handoff_block(body, "v1"), "build-handoff.v1"),
                    (github.find_handoff_block(body, "v2"), "build-handoff.v2")) if block]
        if len(present) != 1:
            raise CoordinatorError("PR contract has no unique engine-build-handoff block")
        (digest, rendered), _sv = present[0]
        if _digest(json.loads(rendered)) != digest:
            raise CoordinatorError("PR handoff content does not match its marker digest")
        value = json.loads(rendered)
    version = value.get("schema_version")
    if version not in ("build-handoff.v1", "build-handoff.v2"):
        raise CoordinatorError("legacy Build handoff is unsupported; verify the PR and start with a fresh plan bind")
    # A handoff exported by an older engine carried `private_reference` in its finding summaries; the
    # field is no longer published and the schema now forbids it (StarshipSuperjam/engine-template#981),
    # so drop any stray copy before validation — an old block still restores cleanly instead of failing
    # closed on the forbidden key. The value is reviewer-internal and never survives the round-trip
    # (restore already yields None for it), so dropping it here loses nothing an up-to-date export keeps.
    stripped_private = False
    for _summary in value.get("finding_summaries") or []:
        # Guard the shape: a malformed block (a non-dict summary) must still reach _validate and fail
        # with the tool's clean CoordinatorError, not an AttributeError from an unconditional .pop().
        if isinstance(_summary, dict) and _summary.pop("private_reference", None) is not None:
            stripped_private = True
    _validate(value, HANDOFF_SCHEMA_V2 if version == "build-handoff.v2" else HANDOFF_SCHEMA)
    if stripped_private:
        # Match the codebase's visible-redaction convention (repair rationale, bounded work): say
        # plainly that a legacy private note was dropped rather than stripping it silently.
        print("dropped a legacy private_reference from the restored handoff (no longer published)")
    repo, issue = value["build"]["repository"], value["plan"]["durable_issue"]
    if getattr(args, "repository", None) and not repo_identity.slug_eq(args.repository, repo):
        raise CoordinatorError("handoff repository does not match the selected repository")
    if getattr(args, "pr", None) and args.pr != value["build"]["pr"]:
        raise CoordinatorError("handoff PR does not match the selected pull request")
    if not repo_identity.slug_eq(repo_identity.origin_slug(str(ROOT)), repo):
        raise CoordinatorError("handoff repository does not match this worktree's origin")
    pr = github.pr_state(ROOT, repo, value["build"]["pr"])
    if pr.get("number") != value["build"]["pr"] or pr.get("state") != "OPEN" or pr.get("headRefOid") != _head():
        raise CoordinatorError("handoff PR is not the open claim at this worktree's current HEAD")
    for completed in value["progress"].get("completed", []):
        commit = completed.get("commit")
        if (not isinstance(commit, str)
                or _run(["git", "cat-file", "-e", f"{commit}^{{commit}}"]).returncode
                or _run(["git", "merge-base", "--is-ancestor", commit, pr["headRefOid"]]).returncode):
            raise CoordinatorError(f"handoff progress commit for {completed.get('id', 'unknown item')} is not contained by the live PR head")
    plan = _durable_plan(_issue_body(repo, issue))
    if _digest(plan) != value["plan"]["digest"]:
        raise CoordinatorError("durable plan is missing or changed; cold continuation is blocked")
    if version == "build-handoff.v2":
        state = _restore_base_state(value, "build-state.v2")
        state["work"] = _restore_work(value.get("work", {}))
    else:
        state = _restore_base_state(value, "build-state.v1")
    store.create(state)
    print(f"restored Build snapshot from durable Issue #{issue}")


def _submit_preview(store: StateStore, plan_path: str) -> dict:
    state = store.read()
    revision = state["revision"]
    plan = _plan(plan_path)
    _assert_plan(state, plan)
    _assert_spec_current(state, plan, check_issue=True)
    repo, pr_number = state["build"]["repository"], state["build"]["pr"]
    if state.get("submission") == "unknown":
        observed = github.pr_state(ROOT, repo, pr_number)
        if observed.get("isDraft") is False:
            github.set_draft(ROOT, repo, pr_number)
        confirmed = github.pr_state(ROOT, repo, pr_number)
        if confirmed.get("isDraft") is not True:
            raise CoordinatorError("ready-state recovery remains unknown; GitHub did not confirm the PR returned to draft")
        store.mutate(lambda s: s.update({"submission": "draft"}), from_revision=revision)
        raise CoordinatorError("recovered an uncertain prior ready transition by returning the PR to draft; rerun preview")
    with core.StableCommit(ROOT, "submission") as stable_head:
        status = _status(state, plan)
        pr = github.pr_state(ROOT, repo, pr_number)
    if pr.get("number") != state["build"]["pr"] or pr.get("state") != "OPEN":
        raise CoordinatorError("the expected pull request is not open")
    if pr.get("headRefOid") != status["head_commit"]:
        raise CoordinatorError("the PR head does not match the local final commit")
    contract = state.get("pr_contract")
    if not contract or contract.get("body_digest") != _digest((pr.get("body") or "").encode()):
        raise CoordinatorError("the PR body changed after preflight; rerun preflight against the current contract")
    if pr.get("mergeable") != "MERGEABLE":
        raise CoordinatorError("the PR is not currently confirmed mergeable; reconcile or retry the live check before submission")
    base = pr.get("baseRefOid")
    if not base or _run(["git", "merge-base", "--is-ancestor", base, status["head_commit"]]).returncode:
        raise CoordinatorError("the final commit does not contain the live target-branch base; reconcile, validate, and assess review proportionally")
    if status["phase"] != "ready":
        raise CoordinatorError("submission evidence is incomplete: " + "; ".join(status["required_evidence"] + status["engineering_judgment"]))
    action = "mark-ready" if pr.get("isDraft") else "record-ready"
    if stable_head != status["head_commit"]:
        raise CoordinatorError("submission status was not derived from the stable final commit")
    return {"repository": repo, "pr": pr_number, "commit": status["head_commit"],
            "base": pr.get("baseRefOid"), "body_digest": _digest((pr.get("body") or "").encode()),
            "snapshot_revision": revision, "action": action, "merge": False}


def cmd_submit_preview(args, store: StateStore) -> None:
    with core.StableCommit(ROOT, "submission preview"):
        preview = _submit_preview(store, args.plan)
    print(json.dumps(preview, indent=2, sort_keys=True))


def cmd_submit_apply(args, store: StateStore) -> None:
    preview = None
    try:
        with core.StableCommit(ROOT, "ready transition"):
            preview = _submit_preview(store, args.plan)
            if preview["action"] == "mark-ready":
                github.set_ready(ROOT, preview["repository"], preview["pr"])
            try:
                after = github.pr_state(ROOT, preview["repository"], preview["pr"])
            except Exception as exc:
                try:
                    store.mutate(lambda s: s.update({"submission": "unknown"}),
                                 from_revision=preview["snapshot_revision"])
                except CoordinatorError:
                    pass
                raise CoordinatorError(f"ready transition state is unknown after GitHub verification failed: {exc}") from exc
            try:
                unchanged = (after.get("state") == "OPEN" and after.get("isDraft") is False
                             and after.get("headRefOid") == preview["commit"]
                             and after.get("baseRefOid") == preview["base"]
                             and _digest((after.get("body") or "").encode()) == preview["body_digest"])
                if not unchanged:
                    raise CoordinatorError("pull-request evidence changed during the ready transition")
                store.mutate(lambda s: s.update({"submission": "ready"}),
                             from_revision=preview["snapshot_revision"])
            except CoordinatorError as exc:
                try:
                    github.set_draft(ROOT, preview["repository"], preview["pr"])
                    confirmed = github.pr_state(ROOT, preview["repository"], preview["pr"])
                    if confirmed.get("isDraft") is not True:
                        raise RuntimeError("GitHub did not confirm draft recovery")
                    store.mutate(lambda s: s.update({"submission": "draft"}),
                                 from_revision=preview["snapshot_revision"])
                except Exception as recovery_exc:
                    try:
                        store.mutate(lambda s: s.update({"submission": "unknown"}),
                                     from_revision=preview["snapshot_revision"])
                    except CoordinatorError:
                        pass
                    raise CoordinatorError(f"ready transition could not be reversed: {exc}; recovery: {recovery_exc}") from exc
                raise CoordinatorError(f"ready transition was reversed: {exc}") from exc
    except CoordinatorError:
        if preview and store.read().get("submission") != "draft":
            try:
                github.set_draft(ROOT, preview["repository"], preview["pr"])
                confirmed = github.pr_state(ROOT, preview["repository"], preview["pr"])
                recovered = confirmed.get("isDraft") is True
                current_revision = store.read()["revision"]
                store.mutate(lambda s: s.update({"submission": "draft" if recovered else "unknown"}),
                             from_revision=current_revision)
            except Exception:
                try:
                    current_revision = store.read()["revision"]
                    store.mutate(lambda s: s.update({"submission": "unknown"}), from_revision=current_revision)
                except CoordinatorError:
                    pass
        raise
    print(f"marked {preview['repository']}#{preview['pr']} ready for the operator; no merge was attempted")


def _bindings() -> dict:
    return _json(BINDINGS_PATH)


def _work_mutate(store: StateStore, change) -> Any:
    """Every work verb reads the current revision and mutates under an explicit compare-and-swap.

    A shared helper so no work verb repeats the legacy pattern of mutating without a from_revision;
    a concurrent snapshot advance rejects the write rather than silently overwriting a sibling claim.
    """
    state = store.read()
    return store.mutate(change, from_revision=state["revision"])


def _require_dag_plan(plan: dict) -> None:
    if _plan_version(plan) != "build-plan.v2":
        raise CoordinatorError("work verbs require a build-plan.v2 Build")


def _node_work(state: dict, node_id: str) -> dict:
    # A v1 snapshot has no work map at all: name the actual cause (wrong plan generation), matching
    # the refusal the packet/claim/result verbs give, instead of a misleading "no recorded work".
    if "work" not in state:
        raise CoordinatorError("work verbs require a build-plan.v2 Build")
    nw = (state.get("work") or {}).get(node_id)
    if not nw:
        raise CoordinatorError(f"work item {node_id} has no recorded work")
    return nw


def _claim_refusal_reason(plan: dict, state: dict, node_id: str, node: dict) -> str:
    """The specific reason a ready-or-not node is not claimable, so the refusal is actionable."""
    st = node.get("state")
    if st != dag.READY:
        reasons = "; ".join(node.get("reasons") or [])
        return f"it is {st}" + (f" ({reasons})" if reasons else "")
    max_concurrency = plan.get("parallelism", {}).get("max_concurrency", 1)
    if dag.slots_in_use(plan, state) >= max_concurrency:
        return f"all {max_concurrency} worker slot(s) are in use — free one by integrating, rejecting, or abandoning a claim"
    item = work.node_item(plan, node_id)
    for holder_id, held in dag.resource_holders(plan, state).items():
        if holder_id != node_id and dag.resources_conflict(item, held):
            return f"its paths or resources conflict with node {holder_id}, which currently holds them"
    return "admission is currently blocked"


def cmd_work_packet(args, store: StateStore) -> None:
    plan = _plan(args.plan)
    _require_dag_plan(plan)
    state = store.read()
    # The preview enforces the same plan-digest bar the claim does, so a stale --plan file fails
    # HERE with the digest-mismatch message rather than previewing clean and surprising the claim.
    _assert_plan(state, plan)
    item = work.node_item(plan, args.item)
    route = work.resolve_route(_bindings(), item["executor_class"], args.provider)
    packet = work.build_packet(plan, state, args.item, route, _head(), "preview", args.worktree or "<worktree>")
    node = dag.derive_lifecycle(plan, state).get(args.item, {})
    # A preview says nothing about the digest; it reports whether a claim would actually succeed now
    # — INCLUDING the approval gate a claim checks first — so a clean preview is never followed by a
    # surprise refusal.
    approved = bool(state["approval"])
    claimable = approved and args.item in dag.claimable_set(plan, state)
    if claimable:
        refusal = None
    elif not approved:
        refusal = "the Build gate is not approved"
    else:
        refusal = _claim_refusal_reason(plan, state, args.item, node)
    packet["preview"] = {"state": node.get("state"), "reasons": node.get("reasons", []),
                         "claimable_now": claimable, "refusal_reason": refusal}
    print(json.dumps(packet))


def cmd_work_claim(args, store: StateStore) -> None:
    plan = _plan(args.plan)
    _require_dag_plan(plan)
    item = work.node_item(plan, args.item)
    route = work.resolve_route(_bindings(), item["executor_class"], args.provider)
    base_sha = _head()
    attempt_id = work.new_attempt_id()
    emitted: dict = {}

    def change(state):
        _assert_plan(state, plan)
        if not state["approval"]:
            raise CoordinatorError("the Build gate is not approved")
        if args.item not in dag.claimable_set(plan, state):
            node = dag.derive_lifecycle(plan, state).get(args.item, {})
            raise CoordinatorError(
                f"work item {args.item} is not claimable now: {_claim_refusal_reason(plan, state, args.item, node)}")
        nw = state["work"].get(args.item) or work.empty_node()
        # An integrator-inline retry disposition is honored HERE: the next attempt runs inline in the
        # current senior session and is never re-dispatched, whatever the node's executor class.
        effective_route = route
        if (nw.get("latest_failure") or {}).get("disposition") == dag.DISP_INLINE:
            effective_route = {**route, "model": "inherit", "effort": "inherit", "inline": True}
        nw["attempt_count"] = nw.get("attempt_count", 0) + 1
        nw["claim"] = work.new_claim(attempt_id, base_sha, args.worktree,
                                     item.get("exclusive_resources", []), effective_route)
        nw["latest_result"] = None
        nw["latest_failure"] = None
        state["work"][args.item] = nw
        emitted["packet"] = work.build_packet(plan, state, args.item, effective_route, base_sha, attempt_id, args.worktree)

    _work_mutate(store, change)
    print(json.dumps(emitted["packet"]))


def cmd_work_attach(args, store: StateStore) -> None:
    def change(state):
        nw = _node_work(state, args.item)
        claim = nw.get("claim")
        if not claim:
            raise CoordinatorError(f"work item {args.item} has no active claim")
        if claim["attempt_id"] != args.attempt:
            raise CoordinatorError(f"attempt {args.attempt} does not match the active claim {claim['attempt_id']}")
        claim["worker_ref"] = args.worker_ref

    _work_mutate(store, change)
    print(f"attached worker reference to {args.item} attempt {args.attempt}")


def cmd_work_result(args, store: StateStore) -> None:
    plan = _plan(args.plan)
    _require_dag_plan(plan)
    item = work.node_item(plan, args.item)
    try:
        payload = json.loads(_input(args.input))
    except ValueError as exc:
        raise CoordinatorError(f"work result input is not JSON: {exc}") from exc
    base_sha = payload.get("base_sha")
    if not base_sha:
        raise CoordinatorError("work result must report the base_sha the worker built from")

    def change(state):
        _assert_plan(state, plan)
        nw = _node_work(state, args.item)
        result = work.bind_result(nw, item, args.attempt, base_sha, payload)
        nw["latest_result"] = result
        if result["outcome"] == "failed":
            nw["latest_failure"] = work.failure_record(
                args.attempt, payload.get("class", "worker"),
                payload.get("reason", "worker reported a failure"))
        else:
            # A returned result supersedes any open failure for this attempt, so the node never
            # derives as failed while holding a complete, contract-satisfying returned result.
            nw["latest_failure"] = None

    _work_mutate(store, change)
    print(f"recorded {args.item} result for attempt {args.attempt}")


def _commit_on_branch(commit: str) -> bool:
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        return False
    return _run(["git", "merge-base", "--is-ancestor", commit, "HEAD"]).returncode == 0


def cmd_work_reject(args, store: StateStore) -> None:
    def change(state):
        nw = _node_work(state, args.item)
        claim = nw.get("claim")
        result = nw.get("latest_result")
        attempt = claim["attempt_id"] if claim else (result or {}).get("attempt_id")
        if attempt != args.attempt:
            raise CoordinatorError(f"attempt {args.attempt} is not the node's current attempt ({attempt})")
        nw["latest_failure"] = work.failure_record(args.attempt, args.rejection_class, args.reason, dag.DISP_OPEN)
        nw["claim"] = None  # rejection releases the reserved resources

    _work_mutate(store, change)
    print(f"rejected {args.item} attempt {args.attempt} ({args.rejection_class}); resources released")


def cmd_work_retry(args, store: StateStore) -> None:
    def change(state):
        nw = _node_work(state, args.item)
        failure = nw.get("latest_failure")
        # An open failure awaits its first disposition; an abandoned node is reopened by exactly this
        # verb — the deliberate fresh start its blocked-state reason promises. Any other disposition
        # has no pending decision to take.
        if not failure or failure.get("disposition") not in (dag.DISP_OPEN, dag.DISP_ABANDONED):
            raise CoordinatorError(f"work item {args.item} has no failure awaiting a retry decision")
        disposition = dag.DISP_INLINE if args.strategy == "integrator-inline" else dag.DISP_RETRY
        failure["disposition"] = disposition
        nw["claim"] = None  # a fresh attempt id is minted on the next claim; attempt_count increments there

    _work_mutate(store, change)
    consequence = ("the next claim will run integrator-inline in the current session"
                   if args.strategy == "integrator-inline" else "the next claim mints a fresh attempt")
    print(f"retry recorded for {args.item} via {args.strategy} ({consequence}): {args.reason}")


def cmd_work_abandon(args, store: StateStore) -> None:
    def change(state):
        nw = _node_work(state, args.item)
        claim = nw.get("claim")
        failure = nw.get("latest_failure")
        attempt = (claim or {}).get("attempt_id") or (failure or {}).get("attempt_id")
        if attempt != args.attempt:
            raise CoordinatorError(f"attempt {args.attempt} is not the node's current attempt ({attempt})")
        nw["latest_failure"] = work.failure_record(args.attempt, (failure or {}).get("class", "worker"),
                                                   args.reason, dag.DISP_ABANDONED)
        nw["claim"] = None  # abandonment releases the reserved resources

    _work_mutate(store, change)
    print(f"abandoned {args.item} attempt {args.attempt}; resources released")


def cmd_work_integrate(args, store: StateStore) -> None:
    if not args.verification_input.strip():
        raise CoordinatorError("integration requires a focused-verification summary")
    if not _commit_on_branch(args.commit):
        raise CoordinatorError(f"integration commit {args.commit} is not on the PR branch")

    def change(state):
        nw = _node_work(state, args.item)
        result = nw.get("latest_result")
        if not result or result.get("outcome") != "returned" or result.get("attempt_id") != args.attempt:
            raise CoordinatorError(f"work item {args.item} has no returned result for attempt {args.attempt} to integrate")
        nw["integration"] = {"attempt_id": args.attempt, "commit": args.commit,
                             "focused_verification": args.verification_input.strip()}
        nw["claim"] = None  # integration releases the reserved resources
        completed = {entry["id"] for entry in state["progress"]["completed"]}
        if args.item not in completed:
            state["progress"]["completed"].append({"id": args.item, "commit": args.commit})

    _work_mutate(store, change)
    print(f"integrated {args.item} at {args.commit}; focused verification recorded")


_CLAIM_FILL_GUIDANCE = (
    "Fill each field per its `description` in .engine/schemas/pr-body-claim.v1.json — that is where the content "
    "rules live (e.g. Scope must not restate size/counts, Risk flags the single most safety-sensitive edit, "
    "Files of interest is a curated selection not the whole diff, Review's loop_narrative is one entry per "
    "round). The renderer owns all structure; every value is a single-line Markdown fragment.")


def cmd_contract_template(args, store) -> None:
    """Emit the fillable `pr-body-claim.v1` skeleton (stateless — no Build snapshot needed). Every judgment
    slot is null and every list empty, so validating the filled-in result names any slot still unfilled;
    the shape is the instruction. Written to --output, or stdout by default."""
    rendered = json.dumps(composer.fillable_template(), indent=2, sort_keys=True) + "\n"
    if getattr(args, "output", None) and args.output != "-":
        Path(args.output).write_text(rendered, encoding="utf-8")
        print(f"wrote the fillable pr-body-claim.v1 template to {args.output}")
        print(_CLAIM_FILL_GUIDANCE, file=sys.stderr)
    else:
        print(rendered, end="")
        print("\n" + _CLAIM_FILL_GUIDANCE, file=sys.stderr)


def _assert_claim_findings(state: dict, claim: dict) -> None:
    """The claim's finding summaries must match the current coordinator finding set EXACTLY — no stale,
    missing, or unknown ids — so the composed body neither drops a real finding nor invents one, and never
    publishes raw finding text implicitly (only the claim's operator-safe summary reaches the body)."""
    state_ids = {f["id"] for f in state.get("findings", [])}
    claim_ids = {fs["id"] for fs in claim["review"]["finding_summaries"]}
    if state_ids != claim_ids:
        parts = []
        missing = sorted(state_ids - claim_ids)
        unknown = sorted(claim_ids - state_ids)
        if missing:
            parts.append("missing an operator-safe summary for: " + ", ".join(missing))
        if unknown:
            parts.append("carries a summary for unknown finding(s): " + ", ".join(unknown))
        raise CoordinatorError(
            "the claim's finding summaries must match the current finding set exactly — " + "; ".join(parts)
            + " (re-run `contract template` against current state, or reconcile the finding ids)")


def _assemble_evidence(state: dict, plan: dict, claim: dict, head: str, pr_data: dict) -> dict:
    """Compute the coordinator-owned evidence a composed body carries — everything deterministic that the
    claim deliberately does not hold. Read-only: it runs the same report-only tools the preflight uses and
    reads recorded Build state; it never writes. `contract preview` and `contract apply` share it so the
    previewed body and the applied body are assembled identically."""
    repo = state["build"]["repository"]
    base = pr_data.get("baseRefOid") or state["build"]["base_at_bind"]

    # Closing linkage: the claim's closes plus the durable Build Issue promotion linked (mechanically added,
    # never inferred). Part-of comes straight from the claim inside the composer.
    closes = list(claim["linkage"]["closes"])
    durable = state["plan"].get("durable_issue")
    if durable and durable not in closes and durable not in claim["linkage"]["part_of"]:
        closes.append(durable)

    # Report-only change profile, over the live base — the same invocation the preflight records.
    profile = _run([sys.executable, str(ROOT / ".engine" / "tools" / "scope_profile.py"), base])
    change_profile = (profile.stdout or "").strip()

    # Validation receipts, stripped of machine-local log paths (mirrors the handoff strip), rendered with the
    # operator labels from the one build-protocol declaration that also drives execution.
    val = state.get("validation")
    if val and val.get("results"):
        labels = {c["id"]: c["label"] for c in _protocol().get("validation_commands", [])}
        vlines = []
        for r in val["results"]:
            status = "passed" if r["passed"] else "**FAILED**"
            tail = f" (log {r['log_digest']})" if r.get("log_digest") else ""
            vlines.append(f"- **{labels.get(r['id'], r['id'])}** — {status} at `{r['commit'][:12]}`{tail}")
        validation_results = "\n".join(vlines)
    else:
        validation_results = "- The full CI suite and self-tests are run green against the final commit and recorded before the draft is marked ready."

    # Spec-derived acceptance steps: the canonical resolution, rendered (multi-document merge) or its honest
    # no-spec disclosure — never re-authored here.
    cs = spec_service.canonical_spec(ROOT, plan, repository=repo, issue_body=_issue_body)
    if cs["posture"] == "none":
        spec_steps = cs["review_steps"]
    else:
        import spec_referent
        spec_steps = spec_referent.render_review_steps_multi(cs["review_steps"])

    depth = state["approval"]["depth"] if state.get("approval") else "unapproved"
    # Coverage must state what ACTUALLY ran, not what is installed. At quick depth (and any depth that
    # recorded no cold-review receipts) no lens ran, so naming the installed lenses as having "ran after"
    # would be a false claim in the PR body — the honesty defect this line must not commit. Key the sentence
    # on the recorded receipts, not on the installed set.
    cold_review_ran = any(
        state.get("reviews", {}).get(stage, {}).get("receipts", [])
        for stage in ("plan", "deliverable"))
    if cold_review_ran:
        lenses = ", ".join(sorted(x["lens"] for x in _installed("deliverable"))) or "no installed deliverable lenses"
        review_coverage = f"{depth} depth. Plan review ran before any code; the deliverable review ({lenses}) ran after."
    else:
        review_coverage = (f"{depth} depth — no cold reviewers ran; the coverage is your own read of the change "
                           "plus the automatic checks (the full CI suite and self-tests).")

    # Code-execution disclosure (BO-41): every current review receipt must carry it. An older snapshot whose
    # receipts predate the field cannot be composed until they are re-recorded — a precise remediation, never a
    # fabricated "no code ran". The disclosure's PRESENCE is mechanical; its truth stays the reviewer's report.
    receipts = [r for stage in ("plan", "deliverable")
                for r in state.get("reviews", {}).get(stage, {}).get("receipts", [])]
    missing = sorted({r["lens"] for r in receipts if "code_execution" not in r})
    if missing:
        raise CoordinatorError(
            "these review receipts predate the code-execution disclosure and must be re-recorded before "
            f"composing: {', '.join(missing)} — re-run `review record … --code-execution none|discarded-copy`")
    ran_code = any(r.get("code_execution") == "discarded-copy" for r in receipts)
    code_execution_line = ("a reviewer ran the change's code in a throwaway copy to judge it — it never touched "
                           "your project" if ran_code else "no reviewer executed the change's code")

    repair = state.get("repair")
    if repair and repair.get("final_commit"):
        drift_line = (f"reviewed `{repair['reviewed_commit'][:12]}`, submitted `{repair['final_commit'][:12]}` — "
                      f"{repair['summary']}")
    else:
        drift_line = "no post-review repair was needed; the reviewed and submitted commits are the same."

    # Index-regeneration disclosure (BO-24): which of the engine's generated index files this PR changed,
    # computed from the diff so the operator sees regeneration happened over generated paths only.
    generated = [".engine/knowledge/graph.json", ".engine/self-map.md", ".engine/provisioning/module-surfaces.json"]
    changed = set(_run(["git", "diff", "--name-only", f"{base}...HEAD"]).stdout.splitlines())
    regen = [g for g in generated if g in changed]
    index_regen = (f"Regeneration updated {len(regen)} of the engine's generated index files "
                   f"({', '.join(regen)}) from the final tree — generated paths only."
                   if regen else "")

    marker = f"<!-- engine-pr-contract:v1 {_digest(claim)} commit={head} -->"

    # Post-approval assumption resolutions, rendered for the operator's merge surface (the PR Review record),
    # not just `status` (StarshipSuperjam/engine-template#1014). Only an assumption authored 'unresolved' can
    # carry a disposition, so its presence is the "after approval" signal; both verified and accepted-risk are
    # disclosed so a self-attested post-hoc resolution can never vanish before the operator sees it.
    authored_unresolved = {a["claim"] for a in plan.get("assumptions", []) if a["status"] == "unresolved"}
    assumption_resolutions = [
        f"{d['claim']} -> {d['resolved_as']} (self-attested, not re-reviewed) — basis: {d['basis']}"
        for d in state.get("assumption_dispositions", []) if d["claim"] in authored_unresolved]

    return {
        "closes": closes,
        "change_profile": change_profile,
        "validation_results": validation_results,
        "index_regen": index_regen,
        "spec_steps": spec_steps,
        "review_coverage": review_coverage,
        "code_execution_line": code_execution_line,
        "disagreement_lines": review.required_disagreement_lines(state),
        "assumption_resolutions": assumption_resolutions,
        "drift_line": drift_line,
        "composition_marker": marker,
        # preserved marker blocks are extracted from the live body at apply time, where the write happens.
        "preserved_blocks": [],
    }


def cmd_contract_preview(args, store: StateStore) -> None:
    """Read-only: validate the claim and current evidence, render the candidate body, run the real
    completeness rule locally, and report the source/claim/commit/candidate digests. No GitHub write."""
    state = store.read()
    plan = _plan(args.plan)
    _assert_plan(state, plan)
    try:
        claim = composer.load_claim(args.claim)
    except composer.ContractError as exc:
        raise CoordinatorError(str(exc)) from exc
    _assert_claim_findings(state, claim)
    repo, pr = state["build"]["repository"], state["build"]["pr"]
    with core.StableCommit(ROOT, "contract preview") as head:
        pr_data = _verify_draft(repo, pr)
        source_body = pr_data.get("body") or ""
        evidence = _assemble_evidence(state, plan, claim, head, pr_data)
        try:
            body = composer.compose(claim, evidence)
        except composer.ContractError as exc:
            raise CoordinatorError(str(exc)) from exc
        github.require_body_budget(body, "composed PR contract")
        contract_passed, contract_summary = _pr_contract(body)
    if getattr(args, "output", None):
        Path(args.output).write_text(body, encoding="utf-8")
    result = {"source_body_digest": _digest(source_body.encode()), "claim_digest": _digest(claim),
              "commit": head, "candidate_body_digest": _digest(body.encode()),
              "complete": contract_passed, "summary": contract_summary}
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        state_word = "complete" if contract_passed else "incomplete"
        print(f"composed a candidate PR contract for {head[:12]}: {state_word} — {contract_summary}")


def _extract_marker_blocks(body: str) -> list:
    """The valid engine marker blocks already on the draft that a fresh compose must carry through unchanged:
    a published handoff block (v2 or v1) and the build-id marker. The pr-contract composition marker is NOT
    preserved — the composer mints a fresh one bound to the current claim digest and commit."""
    blocks = []
    for begin, end in ((github.HANDOFF_BEGIN_V2, github.HANDOFF_END_V2),
                       (github.HANDOFF_BEGIN, github.HANDOFF_END)):
        m = re.search(re.escape(begin) + r".*?" + re.escape(end), body, re.DOTALL)
        if m:
            blocks.append(m.group(0))
    m = re.search(r"<!-- engine-build-id:v1 [^\n]*?-->", body)
    if m:
        blocks.append(m.group(0))
    return blocks


def _apply_body(repo: str, pr: int, *, expected_before: str, new_body: str, revision: int, store: StateStore) -> str:
    """One safe body write: confirm the live body is still what we last saw and Build evidence has not moved,
    write, then read back and require byte-equality. Mirrors cmd_handoff_export's proven idiom. Returns the
    written body (now the confirmed live body). Never clobbers an external edit — a mismatch refuses."""
    before = _verify_draft(repo, pr).get("body") or ""
    if before != expected_before:
        raise CoordinatorError("the PR body changed under the composer mid-apply (a concurrent edit); "
                               "no further write was made — rerun `contract preview` and apply with the new digest")
    if store.read()["revision"] != revision:
        raise CoordinatorError("Build evidence changed mid-apply; rerun `contract preview`")
    _must_run(["gh", "pr", "edit", str(pr), "--repo", repo, "--body-file", "-"], input_text=new_body)
    confirmed = _verify_draft(repo, pr).get("body") or ""
    if confirmed != new_body:
        # GitHub did not echo our exact bytes (a normalization, partial write, or transport hiccup). The
        # mangled body would not be in the caller's `wrote` set — the loop-level restore cannot recognise it —
        # so THIS write undoes itself here: roll the body back to what was there before this write, then raise.
        # (The far likelier cause is a GitHub-side transform of OUR content, not an external edit landing in the
        # sub-second read-after-write window, so undoing our own unconfirmed write is the correct default.)
        if (_verify_draft(repo, pr).get("body") or "") != expected_before:
            _must_run(["gh", "pr", "edit", str(pr), "--repo", repo, "--body-file", "-"], input_text=expected_before)
        raise CoordinatorError("GitHub did not preserve the composed body exactly; the unconfirmed write was "
                               "rolled back")
    return new_body


def _restore_after_failure(repo: str, pr: int, wrote: set, original: str) -> bool:
    """After a failed apply, restore the original body IFF the live body is one the coordinator itself wrote
    this run — so a coordinator-authored intermediate is never left live, while a genuine external edit (not in
    `wrote`) is preserved, never clobbered. No-op when nothing was written. Returns True iff it restored."""
    if not wrote:
        return False
    live = _verify_draft(repo, pr).get("body") or ""
    if live != original and live in wrote:
        _must_run(["gh", "pr", "edit", str(pr), "--repo", repo, "--body-file", "-"], input_text=original)
        return True
    return False


def _close_linkage_result(repo: str, pr: int, base: str, head: str) -> dict:
    """Run the close-linkage preflight against the (already applied) live body and parse its JSON. On a CRASH
    (non-zero exit, no parseable JSON — distinct from the tool's own fail-closed 'could not read' line, which
    it prints with exit 0) fold an explicit fail-closed disclosure so the human-facing body never silently
    omits the close-linkage check; the final recorded preflight leg also captures the failure independently."""
    close = _run([sys.executable, str(ROOT / ".engine" / "tools" / "close_linkage_preflight.py"),
                  "check", "--pr", str(pr), "--base", base, "--head", head])
    try:
        parsed = json.loads(close.stdout) if (close.stdout or "").strip() else None
    except ValueError:
        parsed = None
    if parsed is None:
        if close.returncode != 0:
            return {"lines": ["I couldn't run the close-linkage check before submitting — open the PR on GitHub "
                              "and confirm its “will close” list before you merge."], "defang": None}
        return {"lines": [], "defang": None}
    return {"lines": parsed.get("lines", []), "defang": parsed.get("defang")}


def cmd_contract_apply(args, store: StateStore) -> None:
    """Compose the body and apply it to the still-draft PR under a source-digest compare-and-swap, folding
    close-linkage advisory lines to a fixed point (max three passes), then record the full preflight set and
    bind pr_contract to the stable body. Never marks ready and never merges."""
    if not args.ack_visibility:
        raise CoordinatorError("contract apply writes the composed body to the public PR contract; pass --ack-visibility")
    state = store.read()
    revision = state["revision"]
    plan = _plan(args.plan)
    _assert_plan(state, plan)
    try:
        claim = composer.load_claim(args.claim)
    except composer.ContractError as exc:
        raise CoordinatorError(str(exc)) from exc
    _assert_claim_findings(state, claim)
    repo, pr = state["build"]["repository"], state["build"]["pr"]
    with core.StableCommit(ROOT, "contract apply") as head:
        pr_data = _verify_draft(repo, pr)
        source_body = pr_data.get("body") or ""
        if _digest(source_body.encode()) != args.source_body_digest:
            raise CoordinatorError("the live PR body does not match --source-body-digest; a concurrent edit "
                                   "occurred — rerun `contract preview` and apply with the reported digest")
        base = pr_data.get("baseRefOid") or state["build"]["base_at_bind"]
        preserved = _extract_marker_blocks(source_body)
        # Everything but the folded close-linkage lines is invariant across passes, so assemble the evidence
        # ONCE (a fresh scope_profile subprocess and a possible live spec read are not worth repeating).
        base_evidence = _assemble_evidence(state, plan, claim, head, pr_data)
        base_evidence["preserved_blocks"] = preserved
        close_lines: list = []
        written = source_body
        wrote: set = set()          # every candidate we sent to GitHub this run — the restore set
        converged = False
        # Rollback is two-layered so no coordinator-authored body is ever left live on a failure. Layer one:
        # _apply_body undoes its OWN unconfirmed write on a GitHub echo mismatch (that mangled body is not a
        # recognisable candidate, so only the writer can clean it). Layer two, here: on ANY failure after an
        # earlier CONFIRMED write (a mid-loop revision bump, an armed close, or non-convergence) the live body
        # is a candidate we wrote, so _restore_after_failure puts back the original — while a genuine external
        # edit (not in `wrote`) is preserved, never clobbered.
        try:
            for _ in range(3):
                evidence = {**base_evidence, "close_linkage_lines": close_lines}
                candidate = composer.compose(claim, evidence)
                github.require_body_budget(candidate, "composed PR contract")
                if candidate == written:
                    converged = True
                    break
                wrote.add(candidate)
                written = _apply_body(repo, pr, expected_before=written, new_body=candidate,
                                      revision=revision, store=store)
                result = _close_linkage_result(repo, pr, base, head)
                if result["defang"]:
                    raise CoordinatorError(
                        f"the composed body armed an accidental close of #{result['defang']['number']} — that is "
                        "a composer defect, not an author edit; nothing was submitted")
                close_lines = list(result["lines"])
            if not converged:
                raise CoordinatorError("the composed body did not reach a fixed point in three passes; "
                                       "nothing was recorded")
        except composer.ContractError as exc:
            _restore_after_failure(repo, pr, wrote, source_body)
            raise CoordinatorError(str(exc)) from exc
        except Exception:
            _restore_after_failure(repo, pr, wrote, source_body)
            raise
        legs = _compute_preflight_legs(state, head, _verify_draft(repo, pr), written)
        body_digest = _digest(written.encode())

        def change(s):
            s["preflights"] = legs["results"]
            s["pr_contract"] = {"commit": head, "body_digest": body_digest, "complete": legs["contract_passed"]}
    store.mutate(change, from_revision=revision)
    result = {"commit": head, "body_digest": body_digest, "complete": legs["contract_passed"],
              "summary": legs["contract_summary"], "ready": False, "merge": False}
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        state_word = "complete" if legs["contract_passed"] else "INCOMPLETE"
        print(f"applied the composed PR contract for {head[:12]} ({state_word}); preflights recorded. "
              f"Run `submit preview` when ready — apply never marks ready.")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state", help="path to the harness-owned local Build snapshot; omitted only for standalone pre-PR packets")
    p.add_argument("--expect-revision", type=int, help="optional compare-and-swap guard")
    sub = p.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan").add_subparsers(dest="plan_command", required=True)
    bind = plan.add_parser("bind"); bind.add_argument("--input", required=True); bind.add_argument("--source", choices=["session", "issue"], required=True); bind.add_argument("--mode", choices=["same-session", "unattended"], default="same-session"); bind.add_argument("--repository", required=True); bind.add_argument("--pr", type=int, required=True); bind.add_argument("--issue", type=int); bind.set_defaults(func=cmd_plan_bind)
    promote = plan.add_parser("promote"); promote.add_argument("--input", required=True); destination = promote.add_mutually_exclusive_group(required=True); destination.add_argument("--issue", type=int); destination.add_argument("--create-issue"); promote.add_argument("--ack-visibility", action="store_true"); promote.set_defaults(func=cmd_plan_promote)
    revise = plan.add_parser("revise"); revise.add_argument("--input", required=True); revise.add_argument("--ack-visibility", action="store_true"); revise.set_defaults(func=cmd_plan_revise)
    migrate = plan.add_parser("migrate-v1"); migrate.add_argument("--input", required=True); migrate.add_argument("--output", default="-"); migrate.set_defaults(func=cmd_plan_migrate_v1)
    approve = sub.add_parser("approve"); approve.add_argument("--plan", required=True); approve.add_argument("--depth", choices=["quick", "standard", "thorough"], required=True); approve.set_defaults(func=cmd_approve)
    status = sub.add_parser("status"); status.add_argument("--plan"); status.add_argument("--json", action="store_true"); status.set_defaults(func=cmd_status)
    depths = sub.add_parser("depths"); depths.add_argument("--json", action="store_true"); depths.set_defaults(func=cmd_depths)
    review = sub.add_parser("review").add_subparsers(dest="review_command", required=True)
    packet = review.add_parser("packet"); packet.add_argument("--stage", choices=["plan", "deliverable", "repair"], required=True); packet.add_argument("--plan", required=True); packet.add_argument("--impact"); packet.add_argument("--output"); packet.add_argument("--json", action="store_true"); packet.add_argument("--standalone", action="store_true"); packet.add_argument("--repository"); packet.add_argument("--commit"); packet.add_argument("--base"); packet.add_argument("--depth", choices=["quick", "standard", "thorough"]); packet.set_defaults(func=_packet)
    record = review.add_parser("record"); record.add_argument("--stage", choices=["plan", "deliverable", "repair"], required=True); record.add_argument("--lens", required=True); record.add_argument("--packet-digest", required=True); record.add_argument("--lens-packet-digest", required=True); record.add_argument("--finding", action="append"); record.add_argument("--code-execution", choices=["none", "discarded-copy"], required=True); record.set_defaults(func=cmd_review_record)
    waive = review.add_parser("waive"); waive.add_argument("--stage", choices=["plan"], required=True); waive.add_argument("--reason", required=True); waive.add_argument("--adopted-commit", required=True); waive.set_defaults(func=cmd_review_waive)
    finding = sub.add_parser("finding").add_subparsers(dest="finding_command", required=True)
    frecord = finding.add_parser("record"); frecord.add_argument("--id", required=True); frecord.add_argument("--stage", choices=["plan", "deliverable", "repair"], required=True); frecord.add_argument("--lens", required=True); frecord.add_argument("--severity", choices=["blocking", "serious", "nit"], required=True); frecord.add_argument("--summary", required=True); frecord.add_argument("--disposition", choices=["accepted-fixed", "accepted-tracked", "partially-accepted", "rejected", "escalated"], required=True); frecord.add_argument("--rationale", required=True); frecord.add_argument("--escalation-kind", choices=["design", "law", "authority", "capability-boundary", "guardrail-ack", "operator-only"]); block = frecord.add_mutually_exclusive_group(required=True); block.add_argument("--blocks-this-pr", action="store_true"); block.add_argument("--does-not-block-this-pr", action="store_false", dest="blocks_this_pr"); frecord.add_argument("--handoff-summary"); frecord.add_argument("--operator-summary"); frecord.add_argument("--private-reference", help="Local-only reviewer note; kept in build-state, never published to the PR body and not read back by any verb."); frecord.set_defaults(func=cmd_finding_record)
    assumption = sub.add_parser("assumption").add_subparsers(dest="assumption_command", required=True)
    adispose = assumption.add_parser("dispose"); adispose.add_argument("--plan", required=True); adispose.add_argument("--claim", required=True); adispose.add_argument("--as", dest="resolved_as", choices=["verified", "accepted-risk"], required=True); adispose.add_argument("--basis", required=True); adispose.set_defaults(func=cmd_assumption_dispose)
    checkpoint = sub.add_parser("checkpoint"); checkpoint.add_argument("--plan", required=True); checkpoint.add_argument("--input", required=True); checkpoint.add_argument("--complete-item"); checkpoint.add_argument("--json", action="store_true"); checkpoint.set_defaults(func=cmd_checkpoint)
    validate = sub.add_parser("validate"); validate.set_defaults(func=cmd_validate)
    repair = sub.add_parser("repair").add_subparsers(dest="repair_command", required=True)
    assess = repair.add_parser("assess"); assess.add_argument("--judgment", choices=["none", "scoped", "full"], required=True); assess.add_argument("--rationale", required=True); assess.add_argument("--lens", action="append"); assess.set_defaults(func=cmd_repair_assess)
    preflight = sub.add_parser("preflight"); preflight.add_argument("--pr-body"); preflight.add_argument("--json", action="store_true"); preflight.set_defaults(func=cmd_preflight)
    handoff = sub.add_parser("handoff").add_subparsers(dest="handoff_command", required=True)
    export = handoff.add_parser("export"); export.add_argument("--output", default="-"); export.add_argument("--publish", action="store_true"); export.add_argument("--ack-visibility", action="store_true"); export.set_defaults(func=cmd_handoff_export)
    restore = handoff.add_parser("restore"); restore.add_argument("--input"); restore.add_argument("--repository"); restore.add_argument("--pr", type=int); restore.set_defaults(func=cmd_handoff_restore)
    submit = sub.add_parser("submit").add_subparsers(dest="submit_command", required=True)
    preview = submit.add_parser("preview"); preview.add_argument("--plan", required=True); preview.set_defaults(func=cmd_submit_preview)
    apply = submit.add_parser("apply"); apply.add_argument("--plan", required=True); apply.set_defaults(func=cmd_submit_apply)
    contract_p = sub.add_parser("contract").add_subparsers(dest="contract_command", required=True)
    ctemplate = contract_p.add_parser("template"); ctemplate.add_argument("--output", default="-"); ctemplate.set_defaults(func=cmd_contract_template)
    cpreview = contract_p.add_parser("preview"); cpreview.add_argument("--plan", required=True); cpreview.add_argument("--claim", required=True); cpreview.add_argument("--output"); cpreview.add_argument("--json", action="store_true"); cpreview.set_defaults(func=cmd_contract_preview)
    capply = contract_p.add_parser("apply"); capply.add_argument("--plan", required=True); capply.add_argument("--claim", required=True); capply.add_argument("--source-body-digest", required=True); capply.add_argument("--ack-visibility", action="store_true"); capply.add_argument("--json", action="store_true"); capply.set_defaults(func=cmd_contract_apply)
    work_p = sub.add_parser("work").add_subparsers(dest="work_command", required=True)
    wpacket = work_p.add_parser("packet"); wpacket.add_argument("--item", required=True); wpacket.add_argument("--provider", choices=["claude", "codex"], required=True); wpacket.add_argument("--plan", required=True); wpacket.add_argument("--worktree"); wpacket.set_defaults(func=cmd_work_packet)
    wclaim = work_p.add_parser("claim"); wclaim.add_argument("--item", required=True); wclaim.add_argument("--provider", choices=["claude", "codex"], required=True); wclaim.add_argument("--plan", required=True); wclaim.add_argument("--worktree", required=True); wclaim.set_defaults(func=cmd_work_claim)
    wattach = work_p.add_parser("attach"); wattach.add_argument("--item", required=True); wattach.add_argument("--attempt", required=True); wattach.add_argument("--worker-ref", required=True); wattach.set_defaults(func=cmd_work_attach)
    wresult = work_p.add_parser("result"); wresult.add_argument("--item", required=True); wresult.add_argument("--attempt", required=True); wresult.add_argument("--plan", required=True); wresult.add_argument("--input", required=True); wresult.set_defaults(func=cmd_work_result)
    wreject = work_p.add_parser("reject"); wreject.add_argument("--item", required=True); wreject.add_argument("--attempt", required=True); wreject.add_argument("--class", dest="rejection_class", choices=["dispatch", "worker", "contract", "verification", "integration"], required=True); wreject.add_argument("--reason", required=True); wreject.set_defaults(func=cmd_work_reject)
    wretry = work_p.add_parser("retry"); wretry.add_argument("--item", required=True); wretry.add_argument("--strategy", choices=["redispatch", "integrator-inline"], required=True); wretry.add_argument("--reason", required=True); wretry.set_defaults(func=cmd_work_retry)
    wabandon = work_p.add_parser("abandon"); wabandon.add_argument("--item", required=True); wabandon.add_argument("--attempt", required=True); wabandon.add_argument("--reason", required=True); wabandon.set_defaults(func=cmd_work_abandon)
    wintegrate = work_p.add_parser("integrate"); wintegrate.add_argument("--item", required=True); wintegrate.add_argument("--attempt", required=True); wintegrate.add_argument("--commit", required=True); wintegrate.add_argument("--verification-input", required=True); wintegrate.set_defaults(func=cmd_work_integrate)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        standalone = args.command == "review" and args.review_command == "packet" and args.standalone
        stateless = (args.command == "depths"
                     or (args.command == "plan" and getattr(args, "plan_command", None) == "migrate-v1")
                     or (args.command == "contract" and getattr(args, "contract_command", None) == "template"))
        if not args.state and not standalone and not stateless:
            raise CoordinatorError("--state is required for this command")
        if standalone and (not args.repository or not args.depth):
            raise CoordinatorError("standalone review packets require --repository and --depth")
        store = None if (standalone or stateless) else StateStore(args.state, args.expect_revision)
        args.func(args, store)
        return 0
    except CoordinatorError as exc:
        print(f"build-coordinator: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
