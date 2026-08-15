#!/usr/bin/env python3
"""Pure derivation over a (build-plan.v2, build-state.v2) pair for the Build coordinator.

This module holds the graph and admission logic with no I/O: it validates the static implementation
DAG, derives each node's lifecycle from evidence, and computes the ready and claimable sets and the
resource-occupancy view. It imports only ``build_coordinator_core`` (for the shared error type); it
never imports the CLI, touches git or GitHub, or writes state. Both ``build_coordinator`` (the CLI)
and later coordinator services consume it, so the ready-set/refusal logic lives in exactly one place.
"""
from __future__ import annotations

import graphlib

import build_coordinator_core as core

CoordinatorError = core.CoordinatorError


def _work_items(plan: dict) -> list[dict]:
    return plan["work_items"]


def validate_dag(plan: dict) -> None:
    """Validate the static implementation graph of a build-plan.v2 document.

    Refuses a plan whose dependencies name a work item that does not exist, whose dependencies point
    at their own node, or whose graph contains a cycle. Unique ids are enforced by the plan-ingest
    chokepoint; this re-checks them so the function is safe to call in isolation.
    """
    items = _work_items(plan)
    ids = [item["id"] for item in items]
    if len(ids) != len(set(ids)):
        raise CoordinatorError("Build plan work-item ids must be unique")
    id_set = set(ids)
    graph: dict[str, set[str]] = {}
    for item in items:
        node = item["id"]
        deps = item.get("depends_on", [])
        for dep in deps:
            if dep == node:
                raise CoordinatorError(f"work item {node} cannot depend on itself")
            if dep not in id_set:
                raise CoordinatorError(f"work item {node} depends on unknown work item {dep}")
        graph[node] = set(deps)
    try:
        graphlib.TopologicalSorter(graph).prepare()
    except graphlib.CycleError as exc:
        cycle = " -> ".join(exc.args[1]) if len(exc.args) > 1 else "unknown"
        raise CoordinatorError(f"Build plan dependencies form a cycle: {cycle}") from exc


# --- Derived node lifecycle -------------------------------------------------
#
# The seven derived states. They are DERIVED from evidence each time, never stored, so the snapshot
# stays one atomic record of current facts rather than a lifecycle ledger.
BLOCKED = "blocked"
READY = "ready"
CLAIMED = "claimed"
RETURNED = "returned"
FAILED = "failed"
RECOVERY_REQUIRED = "recovery_required"
COMPLETE = "complete"

_GLOB_META = ("*", "?", "[")


def _node_work(state: dict, node_id: str) -> dict | None:
    return (state.get("work") or {}).get(node_id)


def _holds_claim(nw: dict | None) -> bool:
    """A node holds its acquired resources while a claim record is present.

    The claim is cleared only by integrate, reject, or abandon; it survives a returned or failed
    result (the worker slot is released but the resources stay reserved), which is exactly the
    execution-capacity vs logical-occupancy distinction the design borrows.
    """
    return bool(nw and nw.get("claim"))


def _node_state(nw: dict | None, deps_complete: bool) -> tuple[str, list[str]]:
    """Derive one node's lifecycle state and its reason codes from its work evidence."""
    reasons: list[str] = []
    if nw and nw.get("integration"):
        return COMPLETE, reasons
    claim = nw.get("claim") if nw else None
    failure = nw.get("latest_failure") if nw else None
    result = nw.get("latest_result") if nw else None
    if claim and claim.get("restored"):
        return RECOVERY_REQUIRED, ["restored claim awaits inspection"]
    if failure and failure.get("disposition") == "open":
        reasons.append(f"attempt failed ({failure.get('class')}) awaiting disposition")
        return FAILED, reasons
    if claim and result and result.get("outcome") == "returned" and result.get("attempt_id") == claim.get("attempt_id"):
        return RETURNED, ["worker result awaits integrator inspection"]
    if claim:
        return CLAIMED, ["an attempt is dispatched"]
    if failure and failure.get("disposition") == "abandoned":
        return BLOCKED, ["node abandoned; a fresh attempt must be started deliberately"]
    if not deps_complete:
        return BLOCKED, ["dependencies are not integrated"]
    return READY, reasons


def derive_lifecycle(plan: dict, state: dict) -> dict:
    """Map every work-item id to its derived {state, reasons}.

    A node is complete only when its integration is recorded; readiness requires every dependency
    complete. The result is the single source both status rendering and checkpoint admission read.
    """
    items = _work_items(plan)
    by_id = {item["id"]: item for item in items}
    lifecycle: dict[str, dict] = {}

    def resolve(node_id: str) -> dict:
        if node_id in lifecycle:
            return lifecycle[node_id]
        item = by_id[node_id]
        deps = item.get("depends_on", [])
        deps_complete = all(resolve(dep)["state"] == COMPLETE for dep in deps)
        st, reasons = _node_state(_node_work(state, node_id), deps_complete)
        if st == BLOCKED and not deps_complete:
            missing = [dep for dep in deps if resolve(dep)["state"] != COMPLETE]
            reasons = [f"waiting on {', '.join(missing)}"] + [r for r in reasons if "dependencies" not in r]
        lifecycle[node_id] = {"state": st, "reasons": reasons}
        return lifecycle[node_id]

    for item in items:
        resolve(item["id"])
    return lifecycle


# --- Resource admission -----------------------------------------------------

def resource_prefix(pattern: str) -> str | None:
    """The literal path prefix before a pattern's first glob metacharacter.

    Returns None when the pattern begins with a metacharacter and so has no safe literal prefix — such
    a pattern is treated as conflicting with everything, because its reach cannot be bounded.
    """
    idx = min((pattern.find(m) for m in _GLOB_META if m in pattern), default=-1)
    literal = pattern if idx == -1 else pattern[:idx]
    if not literal:
        return None
    return literal


def _components(prefix: str) -> list[str]:
    return [c for c in prefix.strip("/").split("/") if c]


def _prefixes_conflict(a: str | None, b: str | None) -> bool:
    """Two path prefixes conflict when equal, or when one is an ancestor of the other.

    Component-wise comparison, so foo/bar does not falsely contain foo/barbaz. A None prefix (an
    unbounded metacharacter-leading pattern) conflicts with everything.
    """
    if a is None or b is None:
        return True
    ca, cb = _components(a), _components(b)
    shorter, longer = (ca, cb) if len(ca) <= len(cb) else (cb, ca)
    return longer[: len(shorter)] == shorter


def paths_conflict(paths_a: list[str], paths_b: list[str]) -> bool:
    for pa in paths_a:
        for pb in paths_b:
            if _prefixes_conflict(resource_prefix(pa), resource_prefix(pb)):
                return True
    return False


def resources_conflict(item_a: dict, item_b: dict) -> bool:
    """Whether two work items cannot be claimed concurrently on resource grounds.

    They conflict when their explicit exclusive_resources intersect or their intended paths are not
    provably disjoint. Resource conflict restricts concurrency only; it never creates a dependency.
    """
    if set(item_a.get("exclusive_resources", [])) & set(item_b.get("exclusive_resources", [])):
        return True
    return paths_conflict(item_a.get("paths", []), item_b.get("paths", []))


def slots_in_use(plan: dict, state: dict) -> int:
    """How many worker slots are occupied — the count of nodes with a dispatched, unreturned claim."""
    lifecycle = derive_lifecycle(plan, state)
    return sum(1 for node in lifecycle.values() if node["state"] == CLAIMED)


def resource_holders(plan: dict, state: dict) -> dict:
    """node id -> the resources it currently reserves (explicit + intended paths).

    A node reserves while it holds a claim (claimed, returned, failed-with-active-claim, or
    recovery_required); integrate/reject/abandon release it.
    """
    by_id = {item["id"]: item for item in _work_items(plan)}
    holders: dict[str, dict] = {}
    for node_id, item in by_id.items():
        nw = _node_work(state, node_id)
        if _holds_claim(nw):
            holders[node_id] = {"exclusive_resources": item.get("exclusive_resources", []),
                                "paths": item.get("paths", [])}
    return holders


def ready_set(plan: dict, state: dict) -> list[str]:
    """The dependency-ready nodes, sorted for stable rendering (sort implies no priority)."""
    lifecycle = derive_lifecycle(plan, state)
    return sorted(node_id for node_id, node in lifecycle.items() if node["state"] == READY)


def claimable_set(plan: dict, state: dict) -> list[str]:
    """The ready nodes that admission currently permits a fresh claim on.

    A ready node is claimable only when a worker slot is free under the plan's max_concurrency AND its
    resources do not conflict with any resources a DIFFERENT node currently holds. A node's own held
    resources never exclude it from itself, so an explicit retry can re-claim a node that still
    reserves its resources.
    """
    parallelism = plan.get("parallelism", {"mode": "serial", "max_concurrency": 1})
    max_concurrency = parallelism.get("max_concurrency", 1)
    if slots_in_use(plan, state) >= max_concurrency:
        return []
    by_id = {item["id"]: item for item in _work_items(plan)}
    holders = resource_holders(plan, state)
    claimable = []
    for node_id in ready_set(plan, state):
        item = by_id[node_id]
        conflict = False
        for holder_id, held in holders.items():
            if holder_id == node_id:
                continue
            if set(item.get("exclusive_resources", [])) & set(held["exclusive_resources"]) \
                    or paths_conflict(item.get("paths", []), held["paths"]):
                conflict = True
                break
        if not conflict:
            claimable.append(node_id)
    return sorted(claimable)
