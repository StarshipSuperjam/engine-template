---
title: Build orchestration — from approved intent to a ready pull request
---
## Purpose
Build turns an operator-approved plan into one coherent pull request. The orchestrating AI acts as the senior
engineer: it frames the problem, challenges assumptions, chooses the implementation, adjudicates review
findings, and decides whether repairs deserve more review. The Build coordinator is its instrument panel. It
preserves the exact plan and current evidence, prepares cold-review packets, records what commit was checked,
runs validation and preflights, and reports what remains. It never decides whether the work is good, and it
never merges: the protected-branch merge remains the only binding gate, the coordinator may change an open draft
pull request to ready only after the evidence described here is complete, and the operator alone merges it.

## Steps
### Responsibility boundary
The coordinator owns mechanical facts: Build and draft-PR identity, plan source and digest, approved review
depth, installed reviewer discovery, exact packets and receipts, finding-disposition completeness, reviewed
and final commits, validation and preflight freshness, recorded repair judgment, PR-contract completeness,
and draft-to-ready submission.

The orchestrator owns judgment: plan quality, risk, implementation strategy, whether a discovery is an
ordinary engineering leaf or a changed agreement, whether a reviewer is correct, which remedy fits, whether
operator input is genuinely needed, whether a repair needs no/scoped/full re-review, and whether the result
is coherent and worth presenting.

The operator owns approval of the plan and review depth as one Build gate, decisions about design, law,
authority, or the agreed capability boundary, guardrail acknowledgements, and merge.

### The phase map

A Build moves through the coordinator's phases, and each phase has one runbook. Read this spine, then the runbook
`status` names for the current phase — printed as `Read now: <runbook>` beside the phase, and carried as `runbook`
in `status --json` — and nothing else until the phase changes; the verbs that move a Build between phases print
the same line. The pointer keys on the furthest stage the Build has entered, so a mid-repair commit still reads
validation and review, never implementation again. The table mirrors `phase_runbooks` in
`.engine/build-protocol.json`, which the coordinator reads.

| Coordinator phase | Read now |
| --- | --- |
| `planning` | [Build kickoff](build-kickoff.md) — the claim, the bind, and the one approval that covers both gates |
| `implementation` | [Build implementation](build-implementation.md) — strategy, node integration, checkpoints, reconcile |
| `engineering-decision` | [Build plan correction](build-plan-correction.md) — the plan is wrong, or an assumption or checkpoint is unresolved |
| `finding-disposition`, `deliverable-review`, `repair-assessment`, `final-validation` | [Build validation and review](build-validation-and-review.md) — the two evidence classes, the deliverable review, adjudication, proportional repair |
| `submission-preflight`, `ready` | [Build submission](build-submission.md) — the contract, the disclosure lanes, preflight, marking ready |

[Build continuity](build-continuity.md) is read at a moment rather than a phase: when a Build resumes — a new
session, a compaction, or a cold handoff — before any mutating verb.

Runbooks layer in two tiers. A PHASE runbook is named by the coordinator's pointer and read whole. A SIDE runbook —
work dispatch, execution, product grounding, owned-product, serialized integration, routine entry, external
contribution — is named by a phase runbook for the specific situation that needs it and is read only then; this
spine names no side runbook.

### Coordinator status and holds

`status [--json]` returns derived phase, missing submission evidence, items needing engineering judgment,
warnings, and either one mechanically unique next prerequisite or unordered engineering activities. Its
suggestion is never more authoritative than the orchestrator's understanding of the work.

Hard holds are limited to: unavailable or mismatched plan authority; absent plan/depth approval; silently
omitted approved reviewer coverage; absent deliverable review; validation stale or red for final; post-review
change without a proportional judgment; a finding explicitly left blocking this PR; missing or failed
registered preflight; incomplete PR contract; wrong/non-draft PR during construction; and any operation that
would merge. Each has demonstrated-failure tests and merge history.
Unexpected paths, reviewer severity, diff size, and non-blocking findings remain evidence or judgment inputs, as does a repair's surface classification — round accounting bounds how many rounds run, never which lenses run or how deep; an
`unresolved` assumption instead holds the `ready` phase until cleared by `assumption dispose` or `plan revise`.

## Done when

The exact approved plan and depth are recorded; required plan and deliverable review ran; every reported
finding has an orchestrator disposition; final validation and registered preflights are green; any
reviewed-to-final change has a recorded proportional judgment and required focused receipts; the PR contract
truthfully explains the delivered behavior and evidence; the reconciled PR is mergeable; and the coordinator
has marked the draft ready for the operator. Nothing merged automatically.

## Notes

<!-- generated: build-protocol review-consumers (build_protocol.py render; never hand-edit) -->
Review lenses each Build stage consumes (`review_consumers` in `.engine/build-protocol.json`):

- **plan-review gate** — product-intent, architecture, feasibility, risk-governance
- **product-design spec lock** — product-intent, architecture, feasibility, risk-governance
- **pre-submission gate** — spec-conformance, divergence-hunter, usability, technical-integrity, security-governance
<!-- /generated: build-protocol review-consumers -->
