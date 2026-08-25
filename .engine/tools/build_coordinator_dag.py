#!/usr/bin/env python3
"""Pure derivation over a (build-plan.v2, build-state.v2) pair for the Build coordinator.

This module holds the graph and admission logic with no I/O: it validates a plan document, validates
the static implementation DAG, derives each node's lifecycle from evidence, and computes the ready
and claimable sets and the resource-occupancy view. It imports only ``build_coordinator_core`` (for
the shared error type and the schema validator); it never imports the CLI, touches git or GitHub, or
writes state. Both ``build_coordinator`` (the CLI) and later coordinator services consume it, so the
ready-set/refusal logic lives in exactly one place.
"""
from __future__ import annotations

import fnmatch
import graphlib
import posixpath

import build_coordinator_core as core

CoordinatorError = core.CoordinatorError


def _work_items(plan: dict) -> list[dict]:
    return plan["work_items"]


def plan_version(plan: dict) -> str:
    """The plan document's schema version. An unstated version is REFUSED, never defaulted.

    It used to default to v1, which was reasonable while v1 existed: an old document missing the
    field genuinely was v1. With v1 deleted the same default would name a schema that is not there,
    so a versionless document now fails by saying exactly what it is missing.
    """
    version = plan.get("schema_version")
    if not version:
        raise CoordinatorError(
            "this Build plan does not state a schema_version, so there is no way to know what it is. "
            "Nothing is assumed — the version was never optional, only defaulted, and the generation "
            "it defaulted to no longer exists.")
    return version


def validate_plan_document(value: dict, plan_schemas: dict) -> str:
    """THE single authority on whether a Build plan document is valid. Returns its schema version.

    Three layers, in this order, because each presupposes the last: the schema for the declared
    version, work-item-id uniqueness (which no schema expresses — JSON Schema cannot state
    uniqueness over a derived key), and, for v2, graph closure and acyclicity.

    It lives HERE, in the pure layer, rather than in the CLI, because it now has two callers with a
    hard reason to agree: ``build_coordinator._plan`` applies it at ``plan bind``, and
    ``plan_contract`` applies it to the payload nested inside an ``engine-plan.v1`` revision before
    a plan may be sealed. A second, re-expressed copy would let a plan seal cleanly and then fail at
    bind — the two coordinators holding different notions of a valid payload, which is exactly the
    failure the sealed handoff exists to make impossible. `plan_schemas` is passed in (the same
    dependency-injection shape ``github.durable_plan`` already uses) so this layer stays free of
    filesystem constants while the version->schema map keeps its single home in the CLI module.
    """
    version = plan_version(value)
    schema = plan_schemas.get(version)
    if schema is None:
        raise CoordinatorError(
            f"unrecognized Build plan version {version!r}; expected " + " or ".join(sorted(plan_schemas)))
    core.validate(value, schema)
    ids = [item["id"] for item in value["work_items"]]
    if len(ids) != len(set(ids)):
        raise CoordinatorError("Build plan work-item ids must be unique")
    if version == "build-plan.v2":
        validate_dag(value)
    return version


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

# The failure classes and the orchestrator's dispositions of a failed attempt — one shared home for
# these machine-decidable sets (mirroring the state constants above) so a producer and the deriver
# below cannot drift apart on a bare string literal. Kept in step with the enums in build-state.v2.json.
FAILURE_CLASSES = ("dispatch", "worker", "contract", "verification", "integration")
DISP_OPEN = "open"
DISP_RETRY = "retry"
DISP_ABANDONED = "abandoned"
DISP_INLINE = "integrator-inline"
DISPOSITIONS = (DISP_OPEN, DISP_RETRY, DISP_ABANDONED, DISP_INLINE)

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
    if failure and failure.get("disposition") == DISP_OPEN:
        reasons.append(f"attempt failed ({failure.get('class')}) awaiting disposition")
        return FAILED, reasons
    if claim and result and result.get("outcome") == "returned" and result.get("attempt_id") == claim.get("attempt_id"):
        return RETURNED, ["worker result awaits integrator inspection"]
    if claim:
        return CLAIMED, ["an attempt is dispatched"]
    if failure and failure.get("disposition") == DISP_ABANDONED:
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


def _has_glob(pattern: str) -> bool:
    return any(m in pattern for m in _GLOB_META)


def _literal_suffix(pattern: str) -> str:
    """The literal tail after a pattern's last glob metacharacter (``""`` when none is provable).

    Every path matching the pattern must end with this tail, so two patterns whose tails cannot
    coexist (neither is a suffix of the other) are provably disjoint. A ``[`` class is bounded by its
    closing ``]``, so the cut falls after the last of ``*``/``?``/``]``.
    """
    cut = max(pattern.rfind("*"), pattern.rfind("?"), pattern.rfind("]"))
    return pattern if cut == -1 else pattern[cut + 1:]


def _pair_conflict(pa: str, pb: str) -> bool:
    """Whether two path patterns are NOT provably disjoint.

    A None prefix (a metacharacter-leading pattern with no safe literal) reaches anywhere, so it
    conflicts with everything. Two COMPLETE literal paths (no glob on either side) compare
    COMPONENT-wise, so distinct subtrees never collide (``foo/bar.py`` vs ``foo/barbaz.py`` do not,
    while ``foo/`` vs ``foo/bar.py`` do as ancestor/descendant). A glob against a complete literal
    is conservative on PURPOSE: a declared literal covers its whole subtree and a glob ``*``
    crosses ``/``, so ``a*.txt`` genuinely reaches ``axyz.py/notes.txt`` — only genuine prefix
    divergence proves disjointness there (a match must start with the pattern's literal prefix and
    lie inside the literal's subtree, so compatible prefixes always leave a reachable overlap).
    Two globs declare exact match-sets, so a suffix proof applies: they conflict only when their
    literal prefixes overlap AND their literal tails could coexist — ``docs/*.md`` vs
    ``docs/*.json`` share a prefix but no single path can end with both tails, so they are provably
    disjoint and may run concurrently. Anything not provably disjoint stays a conflict (the safe
    direction: over-serializing never admits a real collision).
    """
    la, lb = resource_prefix(pa), resource_prefix(pb)
    if la is None or lb is None:
        return True
    ga, gb = _has_glob(pa), _has_glob(pb)
    if not ga and not gb:
        ca, cb = _components(la), _components(lb)
        shorter, longer = (ca, cb) if len(ca) <= len(cb) else (cb, ca)
        return longer[: len(shorter)] == shorter
    if ga != gb:
        pattern, literal = (pa, lb) if ga else (pb, la)
        lit = literal.rstrip("/") + "/"
        root = resource_prefix(pattern)
        # A pattern match starts with root; the literal's subtree is everything under lit. When
        # either string prefixes the other, a glob metacharacter can absorb the remainder either
        # way, so an overlapping path is constructible; only true divergence is disjoint.
        return (fnmatch.fnmatch(literal.rstrip("/"), pattern)
                or root.startswith(lit) or lit.startswith(root))
    if not (la.startswith(lb) or lb.startswith(la)):
        return False
    sa, sb = _literal_suffix(pa), _literal_suffix(pb)
    if sa and sb and not (sa.endswith(sb) or sb.endswith(sa)):
        return False
    return True


def paths_conflict(paths_a: list[str], paths_b: list[str]) -> bool:
    return any(_pair_conflict(pa, pb) for pa in paths_a for pb in paths_b)


def path_within_declared(changed: str, declared: list[str]) -> bool:
    """Whether one changed path falls within a node's declared path patterns.

    The changed path is UNTRUSTED (a worker's self-report), so it is normalized and any path that
    escapes the tree is rejected BEFORE matching: an absolute path, or one that normalizes to a
    leading ``..`` traversal (``.engine/tools/../../etc/passwd`` -> ``../etc/passwd``), is never
    within scope. Otherwise it is covered when the normalized change equals or glob-matches a declared
    pattern, or sits beneath a declared literal prefix. Gives a worker's scoped-write posture teeth at
    the evidence layer (the orchestrator's own integration inspection is the deeper backstop).
    """
    if not changed or changed.startswith("/") or "\x00" in changed:
        return False
    norm = posixpath.normpath(changed)
    if norm == ".." or norm.startswith("../"):
        return False
    for pattern in declared:
        if norm == pattern or fnmatch.fnmatch(norm, pattern):
            return True
        prefix = resource_prefix(pattern)
        if prefix:
            root = posixpath.normpath(prefix.rstrip("/"))
            if root not in ("", ".") and (norm == root or norm.startswith(root + "/")):
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
            # Prefer the named resources the claim actually recorded when it was acquired; fall back
            # to the plan item. The paths axis is not stored on the claim, so it comes from the item.
            acquired = (nw.get("claim") or {}).get("acquired_resources")
            holders[node_id] = {"exclusive_resources": acquired if acquired is not None else item.get("exclusive_resources", []),
                                "paths": item.get("paths", [])}
    return holders


def ready_set(plan: dict, state: dict) -> list[str]:
    """The dependency-ready nodes, sorted for stable rendering (sort implies no priority)."""
    lifecycle = derive_lifecycle(plan, state)
    return sorted(node_id for node_id, node in lifecycle.items() if node["state"] == READY)


# The four reasons a candidate node can be left out of an admission pass. Every omission carries one,
# so `status` and `work frontier` can say why a node was passed over instead of leaving a session to
# infer it from an empty set.
DEFER_DEPENDENCY = "dependency"
DEFER_HELD_RESOURCE = "held-resource"
DEFER_SELECTED_CONFLICT = "selected-node-conflict"
DEFER_CAPACITY = "capacity"


def critical_path_lengths(plan: dict) -> dict:
    """node id -> the longest chain of work reachable from it, counted in nodes and including itself.

    Computed from the plan graph ALONE — never from state and never from array position — so it is the
    same number for the same graph however the work_items array happens to be ordered, and a node that
    unblocks a long tail outranks one that unblocks nothing.
    """
    items = _work_items(plan)
    dependents: dict[str, list[str]] = {item["id"]: [] for item in items}
    for item in items:
        for dep in item.get("depends_on", []):
            dependents[dep].append(item["id"])
    # Iterative, not recursive: a deep chain would otherwise cost one stack frame per node and raise
    # RecursionError on a long enough plan instead of answering. graphlib gives a topological order of
    # the dependency graph; walking it in REVERSE visits every node after all of its dependents, so
    # each length is final when it is read. A sink is length 1.
    order = list(graphlib.TopologicalSorter(
        {item["id"]: set(item.get("depends_on", [])) for item in items}).static_order())
    lengths: dict[str, int] = {}
    for node_id in reversed(order):
        lengths[node_id] = 1 + max((lengths[child] for child in dependents[node_id]), default=0)
    return lengths


def admission_rank(plan: dict, lengths: dict | None = None) -> list[str]:
    """Every node id in deterministic admission order: critical path descending, then id ascending.

    Total and deterministic — a lexical tie-break means two graphs that differ only in array order
    produce the identical ranking. `lengths` lets one render compute the critical paths once and
    share them across the several derivations that need the same ranking.
    """
    lengths = critical_path_lengths(plan) if lengths is None else lengths
    return sorted(lengths, key=lambda node_id: (-lengths[node_id], node_id))


def admission_plan(plan: dict, state: dict, rank: list | None = None) -> dict:
    """The one admission derivation: which nodes a fresh claim is permitted on, and why the rest wait.

    Greedy over `admission_rank`: each candidate is admitted unless a DIFFERENT node already holds
    conflicting resources, a node admitted earlier in this same pass conflicts with it, or no worker
    slot is left. Dependency-blocked nodes are candidates too — they are reported as deferred on
    dependency grounds rather than silently omitted.

    The holder_id == node_id guard is defensive: a node in ready_set has no active claim (an active
    claim derives claimed/returned/failed/recovery_required, never ready), so it is never its own
    holder today; the guard keeps the intent explicit if the state machine ever lets a node be ready
    while a claim of its own persists (an explicit retry that reserved resources across the boundary).
    """
    parallelism = plan.get("parallelism", {"mode": "serial", "max_concurrency": 1})
    max_concurrency = parallelism.get("max_concurrency", 1)
    free_slots = max_concurrency - slots_in_use(plan, state)
    lifecycle = derive_lifecycle(plan, state)
    by_id = {item["id"]: item for item in _work_items(plan)}
    holders = resource_holders(plan, state)
    admitted: list[str] = []
    deferred: list[dict] = []
    for node_id in (admission_rank(plan) if rank is None else rank):
        node = lifecycle[node_id]
        if node["state"] == BLOCKED:
            deferred.append({"id": node_id, "kind": DEFER_DEPENDENCY,
                             "reason": "; ".join(node["reasons"]) or "dependencies are not integrated"})
            continue
        if node["state"] != READY:
            continue  # in flight or complete: not a candidate this pass, and visible in the node map
        item = by_id[node_id]
        blocker = next((holder_id for holder_id, held in holders.items()
                        if holder_id != node_id and resources_conflict(item, held)), None)
        if blocker:
            deferred.append({"id": node_id, "kind": DEFER_HELD_RESOURCE,
                             "reason": f"node {blocker} holds conflicting paths or resources"})
            continue
        selected = next((other for other in admitted if resources_conflict(item, by_id[other])), None)
        if selected:
            deferred.append({"id": node_id, "kind": DEFER_SELECTED_CONFLICT,
                             "reason": f"conflicts with {selected}, admitted earlier in this pass"})
            continue
        if free_slots <= 0:
            # Two different situations reach here and a session must be able to tell them apart:
            # every slot is genuinely occupied by a dispatched claim, or this pass filled the last
            # free slot with higher-ranked siblings. Saying "all slots are in use" for the second
            # contradicts the slot count status prints directly above it.
            in_use = slots_in_use(plan, state)
            reason = (f"all {max_concurrency} worker slot(s) are in use"
                      if in_use >= max_concurrency else
                      f"this pass filled the last of {max_concurrency} worker slot(s) with "
                      f"higher-ranked nodes ({', '.join(admitted)})")
            deferred.append({"id": node_id, "kind": DEFER_CAPACITY, "reason": reason})
            continue
        admitted.append(node_id)
        free_slots -= 1
    return {"admitted": admitted, "deferred": deferred}


def next_ready(plan: dict, state: dict, rank: list | None = None) -> str | None:
    """The single ready node the scheduler would advance next.

    Ranked, but deliberately NOT filtered by capacity or resource holds: a busy slot or a held
    resource must not change WHICH item is next to advance — only dependency readiness and rank do.
    """
    ready = set(ready_set(plan, state))
    order = admission_rank(plan) if rank is None else rank
    return next((node_id for node_id in order if node_id in ready), None)


def claimable_set(plan: dict, state: dict, rank: list | None = None) -> list[str]:
    """The ready nodes a fresh claim is PERMITTED on right now, in admission order.

    Eligibility, not selection — and the distinction is deliberate. Membership is exactly the
    pre-ranking rule (a worker slot free under max_concurrency, and no conflict with a resource a
    different node holds), so ranking never takes away the orchestrator's freedom to claim any ready,
    non-conflicting node. What ranking changed is the ORDER: the first entry is the node the scheduler
    would advance, where the order used to be lexical-for-stability and carried no priority.

    `admission_plan()["admitted"]` is the narrower question — which nodes this pass would actually
    select, capped by the free slots and with same-pass conflicts resolved. A node can be claimable
    and not admitted; claiming it is still allowed.
    """
    parallelism = plan.get("parallelism", {"mode": "serial", "max_concurrency": 1})
    if slots_in_use(plan, state) >= parallelism.get("max_concurrency", 1):
        return []
    by_id = {item["id"]: item for item in _work_items(plan)}
    holders = resource_holders(plan, state)
    ready = set(ready_set(plan, state))
    return [node_id for node_id in (admission_rank(plan) if rank is None else rank) if node_id in ready
            and not any(holder_id != node_id and resources_conflict(by_id[node_id], held)
                        for holder_id, held in holders.items())]
