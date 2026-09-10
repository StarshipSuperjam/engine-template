#!/usr/bin/env python3
"""Attempt machinery for the DAG Build coordinator: bounded packets, claims, results, routing.

This service builds the records the ``work`` verbs write and enforces the attempt-binding and
output-contract rules. It imports only ``build_coordinator_core`` and ``build_coordinator_dag``; it
never imports the CLI, reads persona files, or touches git or GitHub. The CLI passes in the plan,
state, loaded bindings, and git facts; routing is resolved from the bindings alone, so this module
has no backward dependency on the worker-persona surfaces rendered later.
"""
from __future__ import annotations

import result_contracts

import re
import secrets

import build_coordinator_core as core
import build_coordinator_dag as dag

CoordinatorError = core.CoordinatorError

_EVIDENCE_KEYS = ("changed_paths", "verification_results", "assumptions", "unresolved_concerns")

# The plan-wide governing context a worker checks its work against. raw_intent and the plan's evidence
# array are deliberately NOT here: raw_intent under the operator's standing no-verbatim directive, the
# evidence array as reviewer grounding rather than builder context. This node's mapped spec criteria are
# added per node alongside these.
_GOVERNING_CONTEXT_KEYS = ("success_obligations", "risks", "assumptions", "scope_boundary", "interpretation")


def new_attempt_id() -> str:
    return secrets.token_hex(16)


def blocked_route(executor_class: str, provider: str, *, gap: str) -> dict:
    """An explicit BLOCKED route value — what ``resolve_route`` returns instead of silently degrading a
    declared-but-incomplete dispatched binding to integrator-inline (the exact anti-pattern issue
    StarshipSuperjam/engine-template#1138 names). It carries no model/effort and is never itself claimable: the claim path turns it into a
    persisted fail-closed dispatch attempt whose message names both the gap and the sanctioned
    ``work retry --strategy integrator-inline`` escape. ``gap`` is a machine-recognizable category
    (``declared-incomplete-binding`` here; the external-transport gaps live in the claim path).
    """
    return {"executor_class": executor_class, "provider": provider,
            "model": None, "effort": None, "inline": False, "blocked": True, "gap": gap}


def resolve_route(bindings: dict, executor_class: str, provider: str) -> dict:
    """Resolve a node's route from the implementation-class bindings, single-sourced.

    ``integrator`` is never dispatched — it is the current senior session, so it resolves to an inline
    route that inherits the session's model.

    Fail-closed, keyed on whether ``implementation_classes`` is DECLARED at all:
      - UNDECLARED (no ``implementation_classes`` key) is the documented no-worker-packs deployment —
        nothing was ever configured, so there is nothing to fail closed on and the class resolves inline
        exactly as before.
      - DECLARED but this class/provider entry is missing or incomplete is a real gap: the route BLOCKS
        rather than silently degrading to integrator-inline. The deliberate escape stays reachable as
        ``work retry --strategy integrator-inline``.

    The coordinator NEVER compensates by selecting a stronger worker, and never raises for the blocked
    case — it returns an explicit blocked route the claim path persists and reports.
    """
    if provider not in ("claude", "codex"):
        raise CoordinatorError(f"unknown provider {provider!r}; expected claude or codex")
    inline = {"executor_class": executor_class, "provider": provider,
              "model": "inherit", "effort": "inherit", "inline": True}
    if executor_class == "integrator":
        return {**inline, "executor_class": "integrator"}
    bindings = bindings or {}
    if "implementation_classes" not in bindings:
        return inline
    classes = bindings.get("implementation_classes") or {}
    binding = (classes.get(executor_class) or {}).get(provider)
    if binding and binding.get("model") and binding.get("effort"):
        return {"executor_class": executor_class, "provider": provider,
                "model": binding["model"], "effort": binding["effort"], "inline": False}
    return blocked_route(executor_class, provider, gap="declared-incomplete-binding")


def node_item(plan: dict, node_id: str) -> dict:
    for item in plan["work_items"]:
        if item["id"] == node_id:
            return item
    raise CoordinatorError(f"work item {node_id} is not in the approved plan")


def empty_node(attempt_count: int = 0) -> dict:
    return {"attempt_count": attempt_count, "claim": None, "latest_result": None,
            "integration": None, "latest_failure": None}


def new_claim(attempt_id: str, base_sha: str, worktree: str, acquired_resources, route: dict) -> dict:
    return {"attempt_id": attempt_id, "base_sha": base_sha, "worktree": worktree,
            "acquired_resources": list(acquired_resources), "requested_route": route,
            "worker_ref": None, "restored": False}


def mapped_criteria(plan: dict, node_id: str) -> list:
    """Exactly this node's mapped specification criteria — sibling-only criteria excluded, ids stripped.

    A criterion is selected only when its disposition is ``mapped`` and its ``work_item_ids`` names this
    node; a criterion mapped only to siblings is left out. ``work_item_ids`` is stripped from the
    projection so no sibling node id can ride into this node's bounded packet. A plan with no settled
    spec (posture ``none``) has no documents and yields an empty list — legitimately absent, never a
    required field defaulted away.
    """
    spec = plan.get("spec") or {}
    selected = []
    for document in spec.get("documents", []):
        for criterion in document.get("criteria", []):
            if criterion.get("disposition") != "mapped":
                continue
            if node_id not in criterion.get("work_item_ids", []):
                continue
            selected.append({
                "document_path": document["path"], "document_digest": document["digest"],
                "id": criterion["id"], "text": criterion["text"],
                "how_verified": criterion["how_verified"],
                "planned_verification": list(criterion["planned_verification"]),
            })
    return selected


def governing_context(plan: dict, node_id: str) -> dict:
    """The plan's governing context for a worker: what its work must stay true to, not its assignment.

    Carries the plan-wide success obligations, risks, assumptions, scope boundary and interpretation,
    plus only this node's mapped specification criteria. The envelope is uniform across providers. On a
    normal or routine plan every governing field must be present and non-empty; a missing one refuses
    rather than defaulting, because a worker handed an empty scope boundary or no obligations is a
    worker checking its work against nothing. Only the trivial profile — where these fields are
    legitimately absent — defaults them.
    """
    trivial = plan.get("profile") == "trivial"
    context = {"note": "This is the plan's governing context, not your assignment. Honor it, and report "
                       "any conflict with it via unresolved_concerns; your deliverable is defined by the "
                       "node's output_contract."}
    for key in _GOVERNING_CONTEXT_KEYS:
        value = plan.get(key)
        if not value:
            if trivial:
                context[key] = "" if key == "interpretation" else []
                continue
            raise CoordinatorError(
                f"plan is missing governing-context field {key!r}; a normal or routine plan must carry "
                "it, so the packet refuses rather than handing the worker an empty context")
        context[key] = value
    context["spec_criteria"] = mapped_criteria(plan, node_id)
    return context


IDENTITY_MODES = ("worker-commit", "accepted-candidate")
RECEIPT_SCHEMA_VERSION = "build-integration-receipt.v1"


def identity_mode_for_route(route: dict) -> str:
    """The Engine-selected identity mode for a route — never the result supplier's choice.

    A dispatched (non-inline) route is worker-commit; an integrator-inline route is accepted-candidate.
    """
    return "accepted-candidate" if route.get("inline") else "worker-commit"


def identity_duty(route: dict) -> dict:
    """The artifact identity a worker owes, per the Engine-selected mode for its route.

    The mode follows the route, never the worker's say-so. A dispatched worker (a non-inline route)
    owes a named commit the Engine reads the artifact tree digest FROM; an inline node is integrated by
    the senior session, which computes the digest over the staged tree, so the worker owes no commit.
    W1 states this duty; the mechanism that enforces it is built in W2.
    """
    if identity_mode_for_route(route) == "accepted-candidate":
        return {"mode": "accepted-candidate",
                "duty": "Your change is integrated inline by the senior session. Stage the candidate "
                        "(`git add`) before work result. The Engine captures the staged tree digest "
                        "at result ingress and checks it at integration. Do not supply artifact_digest "
                        "or artifact_ref in this mode; attempt and base also come from the claim."}
    return {"mode": "worker-commit",
            "duty": "Commit your candidate in this worktree and return its commit id as artifact_ref. "
                    "The Engine derives the artifact tree digest from that commit, so identity is "
                    "Engine-observed, not trusted from your report."}


def attribute_range(full_range: list, sibling_attributions: list) -> tuple:
    """The commits attributable to THIS node, given the full first-parent range and its siblings.

    ``full_range`` is the first-parent commit list reachable from the integration commit and not from
    the claim base — the git side computes it. ``sibling_attributions`` names what each INTEGRATED
    sibling already owns: ``{'node': id, 'receipt_range': [...]}`` for a sibling that carries a receipt,
    or ``{'node': id, 'fallback_commit': sha}`` for a receiptless one, whose completion commit stands in
    for its range under the defined fallback. Returns ``(attributed, degraded, degraded_reason)``.
    """
    owned_by_siblings = set()
    degraded = False
    reasons = []
    for sib in sibling_attributions:
        if sib.get("receipt_range") is not None:
            owned_by_siblings.update(sib["receipt_range"])
        else:
            owned_by_siblings.add(sib["fallback_commit"])
            degraded = True
            reasons.append(
                f"integrated sibling {sib.get('node')} had no receipt; its completion commit "
                f"{str(sib.get('fallback_commit', ''))[:12]} stood in for its attributable range")
    attributed = [c for c in full_range if c not in owned_by_siblings]
    return attributed, degraded, ("; ".join(reasons) if reasons else None)


def validate_receipt(receipt: dict) -> None:
    """Structural validation of an integration receipt — fail closed on anything malformed."""
    if not isinstance(receipt, dict):
        raise CoordinatorError("integration receipt must be an object")
    if receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        raise CoordinatorError(f"integration receipt must be {RECEIPT_SCHEMA_VERSION}")
    for key in ("claim_base", "integration_commit"):
        if not (isinstance(receipt.get(key), str) and re.fullmatch(r"[0-9a-f]{40}", receipt[key])):
            raise CoordinatorError(f"integration receipt {key} must be a 40-hex commit id")
    for key in ("patch_digest", "tree_digest"):
        if not (isinstance(receipt.get(key), str) and re.fullmatch(r"sha256:[0-9a-f]{64}", receipt[key])):
            raise CoordinatorError(f"integration receipt {key} must be a sha256 digest")
    rng = receipt.get("attributable_range")
    if not isinstance(rng, list) or any(
            not (isinstance(c, str) and re.fullmatch(r"[0-9a-f]{40}", c)) for c in rng):
        raise CoordinatorError("integration receipt attributable_range must be a list of commit ids")
    if receipt.get("identity_mode") not in IDENTITY_MODES:
        raise CoordinatorError("integration receipt identity_mode is invalid")
    if not isinstance(receipt.get("degraded"), bool):
        raise CoordinatorError("integration receipt degraded must be a boolean")
    if not isinstance(receipt.get("paths"), list):
        raise CoordinatorError("integration receipt paths must be a list")
    for entry in receipt["paths"]:
        if (not isinstance(entry, dict) or entry.get("status") not in ("A", "M", "D", "R")
                or not isinstance(entry.get("path"), str) or not entry["path"]):
            raise CoordinatorError("integration receipt path entry is malformed")


def assemble_receipt(git_facts: dict, claim_base: str, integration_commit: str,
                     identity_mode: str, sibling_attributions: list) -> dict:
    """Assemble the versioned integration receipt from Engine-gathered git facts. Pure.

    The git side gathers ``git_facts`` (range, tree/patch digests, normalized paths) through a gatherer
    taking an explicit repository root; this function applies the fixed attribution rule and records the
    identity mode proved. It touches no git and imports nothing outside core and dag.
    """
    if identity_mode not in IDENTITY_MODES:
        raise CoordinatorError(f"unknown identity mode {identity_mode!r}")
    attributed, degraded, reason = attribute_range(git_facts["range"], sibling_attributions)
    receipt = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "claim_base": claim_base,
        "integration_commit": integration_commit,
        "attributable_range": attributed,
        "patch_digest": git_facts["patch_digest"],
        "tree_digest": git_facts["tree_digest"],
        "paths": git_facts["paths"],
        "identity_mode": identity_mode,
        "degraded": degraded,
        "degraded_reason": reason,
    }
    validate_receipt(receipt)
    return receipt


def check_artifact_identity(result: dict, engine_tree_digest: str, identity_mode: str) -> None:
    """Refuse a returned result whose SUPPLIED artifact digest contradicts the Engine-derived one.

    The Engine derives the tree digest itself — from the worker's named commit in worker-commit mode,
    or over the staged candidate tree in accepted-candidate mode. A digest carried on the result is
    only ever a cross-check; when present and disagreeing, the result is refused rather than trusting
    the supplier over the Engine's own observation.
    """
    supplied = (result or {}).get("artifact_digest")
    if supplied and supplied != engine_tree_digest:
        raise CoordinatorError(
            f"supplied artifact_digest {supplied} contradicts the Engine-derived tree digest "
            f"{engine_tree_digest} for the {identity_mode} artifact; refusing rather than trusting the "
            "supplied value. Remedy: drop artifact_digest from the result (in worker-commit mode the "
            "Engine derives identity from the commit itself and needs no digest), or integrate the "
            "commit whose tree actually matches the digest you reported")


def build_packet(plan: dict, state: dict, node_id: str, route: dict, base_sha: str,
                 attempt_id: str, worktree: str) -> dict:
    """A bounded worker packet: this node's slice, plus the plan's governing context.

    It still carries no sibling node objects and no parent conversation. It DOES carry the plan's
    governing context — success obligations, risks, assumptions, scope boundary, interpretation, and
    only this node's mapped specification criteria — as context the worker checks its work against, not
    as its assignment. schema_version is build-work-packet.v2: a human-readable marker of that richer
    shape, consumed by no schema file.
    """
    item = node_item(plan, node_id)
    packet = {
        "schema_version": "build-work-packet.v2",
        "result_contract": result_contracts.resolve("worker-result.v1", role="worker"),
        "build": {"repository": state["build"]["repository"], "pr": state["build"]["pr"]},
        "node": {"id": node_id, "description": item["description"], "paths": item["paths"],
                 "verification": item["verification"], "depends_on": item.get("depends_on", []),
                 "exclusive_resources": item.get("exclusive_resources", []),
                 "executor_class": item["executor_class"], "output_contract": item["output_contract"]},
        "objective": plan["objective"], "non_goals": plan.get("non_goals", []),
        "governing_context": governing_context(plan, node_id),
        "base_sha": base_sha, "worktree": worktree, "attempt_id": attempt_id, "route": route,
        "plan_digest": core.digest(plan),
        "required_result": {
            "outcome": "returned|failed",
            "required_evidence": item["output_contract"]["required_evidence"],
            "envelope_is_context_not_deliverable": "governing_context is the plan's governing context, "
                "not this node's deliverable; your deliverable is defined by the node's output_contract.",
            "identity": identity_duty(route),
        },
    }
    packet["packet_digest"] = core.digest({k: v for k, v in packet.items() if k != "packet_digest"})
    return packet


def ingest_worker_report(raw, binding):
    """Validate the entire raw worker report before using or deriving any fields."""
    try:
        return result_contracts.compile_worker(result_contracts.ingest(
            raw, binding, contract="worker-result.v1", role="worker"))
    except result_contracts.Rejection as exc:
        raise CoordinatorError(str(exc)) from exc


def bind_result(nw: dict, item: dict, attempt_id: str, base_sha: str, payload,
                *, observed_digest=None) -> dict:
    """Bind validated semantics to Engine-owned identity; legacy records remain read-only."""
    def refuse(rule, category="semantic", detail="Worker result rejected"):
        raise CoordinatorError(str(result_contracts.Rejection(category, rule,
            contract="worker-result.v1", detail=detail)))
    claim = nw.get("claim")
    if not claim:
        refuse("missing_claim", "authority")
    if attempt_id != claim["attempt_id"]:
        refuse("attempt_mismatch", "stale-attempt")
    if base_sha != claim["base_sha"]:
        refuse("base_mismatch", "authority")
    # Dict input is an internal convenience, never a bypass of the canonical validator.
    import json
    try:
        raw = json.dumps(payload, allow_nan=False) if isinstance(payload, (dict, list)) else payload
    except (ValueError, TypeError, RecursionError):
        refuse("raw_input_required", "syntax")
    report = ingest_worker_report(raw, claim.get("result_contract"))
    outcome, evidence = report["outcome"], report["evidence"]
    artifact_digest = None
    if outcome == "returned":
        escaped = [c for c in evidence["changed_paths"] if not dag.path_within_declared(c, item.get("paths", []))]
        if escaped:
            refuse("path_scope", detail="Returned result changed paths outside the node's declared scope")
        mode = identity_mode_for_route(claim.get("requested_route") or {})
        if mode == "worker-commit":
            if report.get("artifact_ref") is None:
                refuse("artifact_ref", detail="worker-commit identity requires artifact_ref")
        else:
            if report.get("artifact_ref") is not None:
                refuse("artifact_mode", "authority")
            if not isinstance(observed_digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", observed_digest):
                refuse("observed_artifact", "authority", "accepted-candidate identity requires an Engine-observed staged digest")
            artifact_digest = observed_digest
    return {"attempt_id": claim["attempt_id"], "base_sha": claim["base_sha"], "outcome": outcome,
            "artifact_ref": report.get("artifact_ref"), "artifact_digest": artifact_digest,
            "evidence": evidence, "report": report, "result_contract": claim["result_contract"]}


FAIL_CLOSED_GAPS = ("declared-incomplete-binding", "external-transport-refused", "no-eligible-for-production")


def failure_record(attempt_id: str, failure_class: str, reason: str, disposition: str = dag.DISP_OPEN,
                   *, fail_closed: bool = False, gap: str | None = None) -> dict:
    """One node-failure record. A fail-closed dispatch BLOCK sets ``fail_closed`` and a ``gap`` category:
    the coordinator refused to dispatch rather than silently degrading to inline, and ``reason`` names both
    the gap and the ``work retry --strategy integrator-inline`` escape. It is still a ``dispatch``-class
    failure with an OPEN disposition, so the existing retry/escape machinery reopens it unchanged."""
    if failure_class not in dag.FAILURE_CLASSES:
        raise CoordinatorError(f"unknown failure class {failure_class!r}")
    if disposition not in dag.DISPOSITIONS:
        raise CoordinatorError(f"unknown failure disposition {disposition!r}")
    record = {"attempt_id": attempt_id, "class": failure_class, "reason": reason, "disposition": disposition}
    if fail_closed:
        record["fail_closed"] = True
        if gap is not None:
            if gap not in FAIL_CLOSED_GAPS:
                raise CoordinatorError(f"unknown fail-closed gap {gap!r}")
            record["gap"] = gap
    return record
