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


def build_packet(plan: dict, state: dict, node_id: str, route: dict, base_sha: str,
                 attempt_id: str, worktree: str) -> dict:
    """A bounded worker packet: only this node's slice and the governing plan context.

    It carries no sibling nodes and no parent conversation — just the node's objective, paths,
    output contract, base, route, and the required result shape.
    """
    item = node_item(plan, node_id)
    packet = {
        "schema_version": "build-work-packet.v1",
        "build": {"repository": state["build"]["repository"], "pr": state["build"]["pr"]},
        "node": {"id": node_id, "description": item["description"], "paths": item["paths"],
                 "verification": item["verification"], "depends_on": item.get("depends_on", []),
                 "exclusive_resources": item.get("exclusive_resources", []),
                 "executor_class": item["executor_class"], "output_contract": item["output_contract"]},
        "objective": plan["objective"], "non_goals": plan.get("non_goals", []),
        "base_sha": base_sha, "worktree": worktree, "attempt_id": attempt_id, "route": route,
        "plan_digest": core.digest(plan),
        "required_result": {"outcome": "returned|failed",
                            "required_evidence": item["output_contract"]["required_evidence"]},
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
