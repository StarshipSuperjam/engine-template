---
title: Build execution — the executor-qualification contract and how dispatch fails closed
---

## Purpose

The Engine can dispatch a Build node's implementation to an executor other than the bespoke worker
framework. This runbook is the judgment around that contract: what it governs, how an executor earns
eligibility, how dispatch refuses when it cannot honour a request, and what a qualification record does
and does not establish. The machinery — the schema, the records and eligibility modules, the
environment policy, the ACP client, and the qualification harness — lives in code and its tests; this
page is how you reason about it, not a restatement of its verbs.

The contract has two halves. The **records half** is a governed home for versioned
`executor-qualification.v1` records under `.engine/executors/`, a catalogued first-level surface owned
by the core manifest and guarded by a hard check. The **dispatch half** is the coordinator's claim
path: it consults eligibility, and it fails closed — refusing rather than guessing — when a binding is
declared but incomplete, when an external transport is requested, or when no record is eligible for
production.

## Steps

### Read where an executor stands

An executor's standing is a committed `executor-qualification.v1` record under `.engine/executors/`,
one file per executor, each validated at merge by `engine/check/executor-record`. A record carries
three DISTINCT gates — protocol conformance, governance containment, coding capability — each with a
status and a reason category that separates an environmental or authentication BLOCK from a protocol or
capability FINDING, so a blocked gate is never read as a capability verdict. It also carries the
separate identities of the bridge and the vendored agent it wraps, the transport and ACP version, a
non-production scope marker, its own decision boundary, and a staleness rule. Registry presence,
discovery metadata, and a mere binding are never qualification; only an explicit record is.

### Let eligibility answer, never assume

`executor_eligibility` answers best-qualified strictly over explicit records. For a production question
it excludes every non-production record — which, in this Build, is all of them — so it answers
no-eligible-for-production throughout. Consult it; do not infer eligibility from a package being present
or a binding existing.

### Expect dispatch to fail closed

`resolve_route` returns an explicit blocked route and `cmd_work_claim` decides only after consulting the
retry disposition, so the deliberate `work retry --strategy integrator-inline` escape stays reachable.
The decision is keyed on whether `implementation_classes` is declared at all: an undeclared map — the
documented no-worker-packs deployment — resolves inline exactly as before; only a declared-but-missing
or incomplete entry blocks. A blocked attempt is persisted through the `build-state.v2` vocabulary and
its message names both the gap and the sanctioned escape. External-transport routing is refused
default-on and is unreachable from ordinary coordinator entry points.

### Qualify an executor honestly

A qualification run is attended and consent-gated: a recorded operator go-ahead before any third-party
execution, then the harness drives an executor over the provider-neutral `BuildExecutionRunner` seam
(the ACP v1 client is its first implementation) on a replayed sealed node in a throwaway workspace.
Containment is OBSERVED self-containment — detection instruments, bounded inert probes, an allowlist
environment witness, verified process-tree reaping — never Engine enforcement. Each run produces a
schema-valid `executor-attempt-receipt.v1` (which composes the existing `build-integration-receipt.v1`)
and updates the record's gates from what was observed. An honest not-qualified or cannot-authenticate
outcome is a valid completion.

## Done when

The contract exists as machinery with a governed home, its guard is green over every record, eligibility
answers best-qualified over explicit records only, and dispatch is witnessed to fail closed on the
declared-incomplete, external-transport, and no-eligible paths while present bindings resolve unchanged.
Any qualification run that occurred left a schema-valid record and receipt whose evidence references are
digest-checkable after the workspace is gone.

## Notes

These records are NOT the runtime-environment store. `.engine/state/execution.json`
(`execution-state.v1`) records whether the Engine's OWN runtime floors (the claude/codex senior
sessions) are qualified against the current release; it is a frozen operator-judgment snapshot written
only by `execution_environment.record_qualification`. The `.engine/executors/` records instead qualify a
THIRD-PARTY executor artifact by observed behaviour, are derived-observational, and carry a
non-production scope. The two stores share no subject and must never be conflated.

**Non-production scope and staleness.** Every record this contract mints carries `scope:
non-production`, and the hard check refuses any other value — this Build never lifts the marker. A
record freezes against the bridge and vendored-agent versions and the ACP version it names; when any of
those is superseded the record is stale and must be re-qualified before reuse. No gate here proves
genuine tool execution (that ground is open, issue StarshipSuperjam/engine-template#1021); a gate
records what was OBSERVED.

**Retirement.** If the ACP thesis is abandoned, the Phase B machinery (the ACP client, the acquisition
and qualification tools, their schemas, and the committed records) routes to a named removal follow-up;
the records' staleness rule is what keeps a frozen record from being read as current in the meantime.

**The door to production.** Lifting the non-production marker is a separate later plan's decision,
gated on the pre-stated thresholds AND the operator's two proof-of-concept preconditions: real
containment enforcement (an OS-level sandbox, not observed-only), and a settled headless
subscription-authentication story with no Engine token handling. A bridge-backed same-engine result
establishes nothing about a non-incumbent executor, and the native-executor arm is a recorded gap —
so the production question stays open until that plan answers it.
