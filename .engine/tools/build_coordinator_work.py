#!/usr/bin/env python3
"""Attempt machinery for the DAG Build coordinator: bounded packets, claims, results, routing.

This service builds the records the ``work`` verbs write and enforces the attempt-binding and
output-contract rules. It imports only ``build_coordinator_core`` and ``build_coordinator_dag``; it
never imports the CLI, reads persona files, or touches git or GitHub. The CLI passes in the plan,
state, loaded bindings, and git facts; routing is resolved from the bindings alone, so this module
has no backward dependency on the worker-persona surfaces rendered later.
"""
from __future__ import annotations

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


def resolve_route(bindings: dict, executor_class: str, provider: str) -> dict:
    """Resolve a node's route from the implementation-class bindings, single-sourced.

    ``integrator`` is never dispatched — it is the current senior session, so it resolves to an inline
    route that inherits the session's model. A missing or unqualified binding for a dispatched class
    falls back to integrator-inline; the coordinator NEVER compensates by selecting a stronger worker.
    """
    if provider not in ("claude", "codex"):
        raise CoordinatorError(f"unknown provider {provider!r}; expected claude or codex")
    inline = {"executor_class": executor_class, "provider": provider,
              "model": "inherit", "effort": "inherit", "inline": True}
    if executor_class == "integrator":
        return {**inline, "executor_class": "integrator"}
    classes = (bindings or {}).get("implementation_classes", {})
    binding = (classes.get(executor_class) or {}).get(provider)
    if not binding or not binding.get("model") or not binding.get("effort"):
        return inline
    return {"executor_class": executor_class, "provider": provider,
            "model": binding["model"], "effort": binding["effort"], "inline": False}


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


def identity_duty(route: dict) -> dict:
    """The artifact identity a worker owes, per the Engine-selected mode for its route.

    The mode follows the route, never the worker's say-so. A dispatched worker (a non-inline route)
    owes a named commit the Engine reads the artifact tree digest FROM; an inline node is integrated by
    the senior session, which computes the digest over the staged tree, so the worker owes no commit.
    W1 states this duty; the mechanism that enforces it is built in W2.
    """
    if route.get("inline"):
        return {"mode": "accepted-candidate",
                "duty": "Your change is integrated inline by the senior session, which computes the "
                        "artifact tree digest over the staged tree. You owe no worker commit id."}
    return {"mode": "worker-commit",
            "duty": "Commit your candidate in this worktree and return its commit id as artifact_ref. "
                    "The Engine derives the artifact tree digest from that commit, so identity is "
                    "Engine-observed, not trusted from your report."}


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


def bind_result(nw: dict, item: dict, attempt_id: str, base_sha: str, payload: dict) -> dict:
    """Bind a worker result to the active claim's attempt id and base SHA; reject a stale attempt.

    A returned result must carry every evidence kind its node's output_contract requires; a missing
    kind is a contract failure, so the output_contract is enforced, not merely declared.
    """
    if not isinstance(payload, dict):
        raise CoordinatorError("work result payload must be a JSON object")   # fail closed, never crash
    claim = nw.get("claim")
    if not claim:
        raise CoordinatorError("no active claim to bind a result to")
    if attempt_id != claim["attempt_id"]:
        raise CoordinatorError(
            f"result attempt {attempt_id} does not match the active claim attempt {claim['attempt_id']}")
    if base_sha != claim["base_sha"]:
        raise CoordinatorError(
            f"result base {base_sha} does not match the claimed base {claim['base_sha']}")
    outcome = payload.get("outcome")
    if outcome not in ("returned", "failed"):
        raise CoordinatorError("result outcome must be 'returned' or 'failed'")
    supplied = payload.get("evidence") or {}
    if not isinstance(supplied, dict):
        raise CoordinatorError("result evidence must be an object")   # fail closed, never crash
    evidence = {}
    for key in _EVIDENCE_KEYS:
        value = supplied.get(key)
        value = [] if value is None else value
        # The whole payload is a worker's UNTRUSTED self-report, so every field fails closed with a
        # refusal, never a crash or a silent coercion (a bare string must not become a char list).
        if not isinstance(value, list) or any(not isinstance(entry, str) for entry in value):
            raise CoordinatorError(f"result evidence {key} must be a list of strings")
        evidence[key] = list(value)
    if outcome == "returned":
        # A required key satisfied by an explicit null is MISSING, not empty: the contract demands
        # the evidence kind be carried, and a null must not silently launder it into [].
        missing = [k for k in item["output_contract"]["required_evidence"] if supplied.get(k) is None]
        if missing:
            raise CoordinatorError(
                "returned result is missing output-contract evidence: " + ", ".join(sorted(missing)))
        # Scoped-write teeth: a returned result whose reported changed paths escape the node's
        # declared paths is a contract failure — the worker wrote outside the scope it was given.
        declared = item.get("paths", [])
        escaped = [c for c in evidence["changed_paths"] if not dag.path_within_declared(c, declared)]
        if escaped:
            raise CoordinatorError(
                "returned result changed paths outside the node's declared scope: " + ", ".join(sorted(escaped)))
    return {"attempt_id": attempt_id, "base_sha": base_sha, "outcome": outcome,
            "artifact_ref": payload.get("artifact_ref"), "artifact_digest": payload.get("artifact_digest"),
            "evidence": evidence}


def failure_record(attempt_id: str, failure_class: str, reason: str, disposition: str = dag.DISP_OPEN) -> dict:
    if failure_class not in dag.FAILURE_CLASSES:
        raise CoordinatorError(f"unknown failure class {failure_class!r}")
    if disposition not in dag.DISPOSITIONS:
        raise CoordinatorError(f"unknown failure disposition {disposition!r}")
    return {"attempt_id": attempt_id, "class": failure_class, "reason": reason, "disposition": disposition}
