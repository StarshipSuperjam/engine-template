#!/usr/bin/env python3
"""Executor eligibility — best-qualified selection strictly over explicit qualification records.

Selection policy ONLY. Given the loaded executor-qualification.v1 records, this module decides which
executors are eligible to receive a dispatched build-execution attempt and picks the best-qualified one. It
never reads a package registry, discovery metadata, or a coordinator binding — NONE of those is a
qualification. Registry presence is discovery, not certification.

Fail closed. An empty record set, or a set in which no executor clears every one of the three distinct gates,
yields NO eligible executor and the caller must fail closed — there is no implicit fallback here.

'best-qualified', never 'best-certified': eligibility rests only on an explicit, versioned Engine
qualification record. In THIS Build every real record carries scope 'non-production', so a PRODUCTION
eligibility query always returns no-eligible — the spike never makes a production eligibility claim.
"""
from __future__ import annotations

# The three distinct gates every eligible executor must clear. Kept in step with the schema's gates object.
GATES = ("protocol_conformance", "governance_containment", "coding_capability")


def _gate_passed(record: dict, gate: str) -> bool:
    entry = (record.get("gates") or {}).get(gate) or {}
    return entry.get("status") == "passed"


def is_qualified(record: dict) -> bool:
    """True only when this is a qualification record AND every one of the three distinct gates passed. A
    partial, failed, not-run, or blocked gate disqualifies — there is no partial eligibility, and a
    fail-closed-witness record never qualifies anything."""
    if not isinstance(record, dict) or record.get("record_kind") != "qualification":
        return False
    return all(_gate_passed(record, gate) for gate in GATES)


def eligible(records, *, production: bool) -> list:
    """The eligible records for a query, unordered-but-filtered. With ``production=True`` a non-production
    record is additionally excluded, so a spike record can never satisfy a production query. Returns ``[]``
    when nothing qualifies — that empty list is the fail-closed signal the caller acts on."""
    out = []
    for record in records or []:
        if not is_qualified(record):
            continue
        if production and record.get("scope") != "production":
            continue
        out.append(record)
    return out


def select(records, *, production: bool):
    """The single best-qualified executor for a query, or ``None`` when none is eligible (fail closed).

    Qualification is binary — every gate passed — so 'best-qualified' resolves ties deterministically: among
    fully-qualified records, the most recently recorded wins, and ``executor_id`` breaks an exact tie so the
    choice is stable across runs."""
    candidates = eligible(records, production=production)
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda r: (r.get("recorded_at", ""), r.get("executor_id", "")),
        reverse=True,
    )[0]
