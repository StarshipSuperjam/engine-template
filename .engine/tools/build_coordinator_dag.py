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
