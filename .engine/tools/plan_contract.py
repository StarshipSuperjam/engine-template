#!/usr/bin/env python3
"""The engine-plan.v1 contract: what a plan revision is, and what makes one valid.

A plan revision has two halves that answer to two different authorities, and keeping that split
honest is this module's whole job.

The DELIBERATION half — intent, problem frame, the case against, alternatives and their
dispositions, failure modes, unresolved decisions — is governed by `.engine/schemas/engine-plan.v1.json`
and judged here.

The BUILD half — `build_plan` — is governed by the BUILD Coordinator and judged THERE, through
`build_coordinator_dag.validate_plan_document`: the same function, the same three layers, the same
code path that `plan bind` runs. It is not re-checked here and it must never be. If this module grew
its own opinion about a valid payload, the two opinions would drift, and the drift would surface as
a plan that seals cleanly and then fails at bind — precisely the disagreement a sealed handoff
exists to make impossible.

JSON is the sole authority. There is no YAML working copy to reconcile, no semantic-versus-formatting
digest distinction, and no uncheckpointed-edit state: a revision is minted whole and never edited in
place, so the digest over its canonical form IS the revision's identity. Canonicalization and digest
are the Build Coordinator's own (`build_coordinator_core.canonical` / `.digest`), not a second
implementation, so a digest computed on the plan side and one computed on the Build side agree
byte-for-byte.

This module is pure: it validates and derives, it does not read or write the plan library. Storage
is `plan_store`'s job.
"""
from __future__ import annotations

import json
from pathlib import Path

import build_coordinator_core as core
import build_coordinator_dag as dag

ROOT = Path(__file__).resolve().parents[2]
PLAN_SCHEMA = ROOT / ".engine" / "schemas" / "engine-plan.v1.json"
# schema_version -> the schema that validates a document carrying it. Mirrors the Build Coordinator's
# own map shape; it stays a map so a future engine-plan.v2 is a one-line addition rather than a fork.
PLAN_SCHEMAS = {"engine-plan.v1": PLAN_SCHEMA}

# The Build payload versions a plan may carry. v1 is accepted for READING only — a v1 payload can be
# held and shown, so an imported legacy plan is never unreadable — but it can never be sealed, since
# the whole point of the handoff is a DAG the Build Coordinator can schedule. The refusal is stated
# at seal, not at read, so the operator meets it with a plan in hand rather than at import.
BUILD_PLAN_SCHEMAS = {
    "build-plan.v1": ROOT / ".engine" / "schemas" / "build-plan.v1.json",
    "build-plan.v2": ROOT / ".engine" / "schemas" / "build-plan.v2.json",
    # The honestly-empty payload an imported native plan carries until someone actually decomposes it.
    # It is registered HERE, on the one validation path, rather than given a branch of its own: a
    # second and laxer path is how an undecomposed plan would eventually find its way to a Build. It
    # finds none — the version is not sealable, so the seal blocker below states the refusal in the
    # operator's own words, and `plan bind` (which only ever sees a sealed payload) never meets it.
    "build-plan.imported": ROOT / ".engine" / "schemas" / "build-plan.imported.json",
}
IMPORTED_BUILD_PLAN_VERSION = "build-plan.imported"
SEALABLE_BUILD_PLAN_VERSION = "build-plan.v2"

PlanContractError = core.CoordinatorError

canonical = core.canonical
digest = core.digest


def plan_version(document: dict) -> str:
    """The plan document's own schema version. Unlike the Build plan's reader there is no historical
    default: engine-plan.v1 is the first version there has ever been, so an absent `schema_version`
    is a malformed document rather than an old one, and saying so beats silently assuming v1."""
    version = document.get("schema_version")
    if not isinstance(version, str) or not version:
        raise PlanContractError(
            "the plan document does not state a schema_version; expected " + " or ".join(sorted(PLAN_SCHEMAS)))
    return version


def validate_document(document: dict) -> str:
    """Validate one plan revision whole, and return the Build payload's schema version.

    Two authorities, applied in order. First this contract's own schema over the deliberation half.
    Then — by DELEGATION, never by re-expression — the Build Coordinator's own three-layer judgment
    over `build_plan`: its schema for the declared version, work-item-id uniqueness, and DAG closure.

    Structural defects the schema itself catches (a missing deliberation, an absent build_plan, a
    malformed id) surface as this contract's error; a defect in the payload surfaces as the Build
    Coordinator's own error text, unwrapped, so the operator reads the same words at plan time that
    they would have read at bind time.
    """
    version = plan_version(document)
    schema = PLAN_SCHEMAS.get(version)
    if schema is None:
        raise PlanContractError(
            f"unrecognized plan document version {version!r}; expected " + " or ".join(sorted(PLAN_SCHEMAS)))
    core.validate(document, schema)
    return dag.validate_plan_document(document["build_plan"], BUILD_PLAN_SCHEMAS)


def load_document(path: str | Path) -> dict:
    """Read and validate a plan revision from disk. Reading is separate from judging so a caller
    holding a document in memory (an import, a freshly minted revision) validates by the same rule."""
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except ValueError as exc:
        raise PlanContractError(f"the plan document is not valid JSON: {exc}") from exc
    validate_document(document)
    return document


def build_plan_digest(document: dict) -> str:
    """The digest of the nested Build payload alone — what `plan bind` will independently compute
    over the same bytes. Recorded at seal so the handoff can be proven, on the Build side, to carry
    the exact payload the plan was sealed with, without trusting the plan side's own bookkeeping."""
    return digest(document["build_plan"])


def document_digest(document: dict) -> str:
    """The digest of the whole revision — the plan's identity at this revision."""
    return digest(document)


def seal_blockers(document: dict) -> list[str]:
    """Every reason this revision may NOT be sealed, in operator-facing language, or an empty list.

    Returned as a LIST rather than raised one at a time on purpose: an operator fixing a plan should
    see everything standing between them and a seal in one reading, not discover the next blocker
    only after clearing the last. The seal verb (N06) adds the evidence-shaped blockers it alone can
    see — a missing review, an undispositioned finding, a stale approval — on top of these, which
    are the ones derivable from the document itself.
    """
    blockers = []
    try:
        payload_version = validate_document(document)
    except PlanContractError as exc:
        return [f"the plan does not validate: {exc}"]
    unresolved = document["deliberation"]["unresolved_decisions"]
    if unresolved:
        blockers.append(
            f"{len(unresolved)} decision(s) are still unresolved and a plan cannot be sealed while the "
            "operator still owes an answer: " + "; ".join(unresolved))
    open_assumptions = [a["claim"] for a in document["build_plan"].get("assumptions", [])
                        if a.get("status") not in ("verified", "accepted-risk")]
    if open_assumptions:
        blockers.append(
            f"{len(open_assumptions)} assumption(s) are neither verified nor accepted as risk: "
            + "; ".join(open_assumptions))
    if payload_version == IMPORTED_BUILD_PLAN_VERSION:
        # Named apart from the generic version refusal because the operator meeting THIS one is not
        # holding an old plan they need to migrate — they are holding a plan nobody has decomposed
        # yet, and the remedy is work, not a conversion.
        blockers.append(
            "this plan arrived as an imported native plan and still carries the empty payload it was "
            "imported with: no work has been decomposed, so there is nothing to hand a Build. Author "
            f"a {SEALABLE_BUILD_PLAN_VERSION} payload and mint it with `revise`. Nothing will infer one "
            "from the imported text, because a decomposition nobody wrote is a decomposition nobody can "
            "be held to")
    elif payload_version != SEALABLE_BUILD_PLAN_VERSION:
        blockers.append(
            f"the Build payload is {payload_version}; only {SEALABLE_BUILD_PLAN_VERSION} can be sealed, because a "
            "sealed handoff must hand the Build Coordinator a graph it can schedule")
    return blockers
