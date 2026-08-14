---
id: eADR-0041
title: Build coordinator behavioral contract
status: accepted
date: 2026-08-13
---

This contract is the acceptance referent for the Build coordinator. It is derived from the Build
orchestration runbook, finding policy, risk surface, reviewer contracts, pull-request contract,
settled-spec flow, submission preflights, and representative successful Build histories. The coordinator
preserves grounding and evidence around senior engineering judgment; it does not replace that judgment
with procedural completeness.

The historical cases are normalized in
`.engine/_fixtures/build-coordinator-scenarios/scenarios.v1.json`. They record only durable workflow facts,
not private transcripts or machine-specific paths. The failed StarshipSuperjam/engine-template#964 implementation remains available in
its preserved branch for comparison, but it is not a design input and none of its receipts are evidence for
the replacement.

## Decision

Build uses the following classified assertions as the coordinator's acceptance contract. Mechanical holds
remain limited to demonstrated failures; senior engineering and operator judgments stay outside the state
machine.

### Classified assertions

| ID | Class | Required behavior | Canonical or observed source | Failed implementation | Replacement response |
|---|---|---|---|---|---|
| BC-01 | operator authority | The operator approves the plan and review depth together before implementation. | Build steps 1–2 | Split approval from the Build gate and repeatedly sought leaf-level permission. | One approval binds plan digest and depth; later operator input is reserved for design, law, scope, or authority. |
| BC-02 | AI judgment | The orchestrator turns nascent intent into a grounded, adversarially tested plan. | Conduct plus plan-review gate | Presence checks treated a structurally complete plan as trustworthy. | The plan shape preserves the reasoning referent; cold reviewers judge its quality. |
| BC-03 | mechanical fact | The exact approved plan reaches checkpoints and reviewers. | Plan and both review gates | Receipt events multiplied copies and identities without improving the referent. | Canonical digest plus exact input verification; same-session content stays in the harness. |
| BC-04 | recovery behavior | A same-session plan may remain session-local; cold continuation requires an exact durable plan. | Build proportionality notes | Created or assumed a GitHub Issue for every Build. | Promote only when handoff is needed; reuse a suitable Issue before creating a Build Issue. |
| BC-05 | explicit non-goal | GitHub comments are not a lifecycle database. | Operator correction during StarshipSuperjam/engine-template#964 | Used append-only PR comments as an event ledger. | One local atomic snapshot; one bounded handoff block in the PR contract only for intentional handoff. |
| BC-06 | mechanical fact | The installed reviewer roster and omissions are discoverable. | Consumed-review-lenses record | Review identity became a recursive protocol. | Derive personas from committed metadata and record the exact packet and completed receipt. |
| BC-07 | AI judgment | Reviewer severity and proposed remedies are advice; the orchestrator critically adjudicates both. | Reviewer boundaries; historical cases 865, 924, 955 | Severity effectively selected the response and encouraged blind repair. | Findings record disposition, rationale, and `blocks_this_pr`; severity never decides readiness. |
| BC-08 | operator authority | Only genuine design, law, scope-boundary, authority, or unresolved blocking decisions return to the operator. | Conduct and Build gate | Routine leaves and audit nits repeatedly stopped the Build. | `escalated` is available, but ordinary engineering leaves remain the orchestrator's work. |
| BC-09 | advisory practice | Unexpected paths, assumptions, and nearby risks should be shown to the engineer. | Impact check and checkpoints | Path policing turned every surprise into an interlock. | Status and checkpoint highlight them without forbidding progress. |
| BC-10 | AI judgment | An implementation discovery revises the plan only when it changes intent, outcome, capability boundary, non-goals, settled criteria, authority, or agreed scope. | Plan settlement; historical cases 886 and 924 | Leaf changes invalidated approvals and reviews. | The orchestrator explicitly selects aligned, revision, or operator-decision posture. |
| BC-11 | submission prerequisite | The authoritative plan must be reproducible and match the approved digest. | Both review gates | Event repair attempted to reconstruct authority. | Missing or mismatched plan is a hard hold; no transcript reconstruction. |
| BC-12 | submission prerequisite | Approved reviewer coverage cannot be silently omitted. | Build review gates | Coverage was entangled with phase advancement. | A manifest names required lenses; absent required receipts hold submission and omitted installed coverage is disclosed. |
| BC-13 | submission prerequisite | Deliverable review must run against a recorded commit. | Build step 6 | Multiple packet generations obscured the actual reviewed commit. | One reviewed commit and packet digest per lens. |
| BC-14 | submission prerequisite | Findings must be dispositioned, and only findings explicitly left blocking hold submission. | Finding policy and human merge gate | `blocking`/`serious` labels automatically drove more work. | Completeness is mechanical; correctness of rationale and `blocks_this_pr` is engineering judgment. |
| BC-15 | submission prerequisite | Validation and registered preflights must be green for the final commit. | Build steps 6–7 | Receipts could be current while the substantive commit changed. | Results bind to commit and become stale on commit change. |
| BC-16 | AI judgment | Reviewed-to-final divergence receives one proportional `none`, `scoped`, or `full` re-review judgment. | Build step 6; historical cases 681, 685, 955 | Invalidation rules produced recursive cold audits. | The coordinator measures and records the choice but never selects it. |
| BC-17 | explicit non-goal | A focused re-review's repair does not automatically trigger another review. | Historical case 685; operator correction during StarshipSuperjam/engine-template#964 | Every audit-created change created another audit obligation. | A fresh proportional judgment may be `none`, terminating the loop. |
| BC-18 | recovery behavior | Local implementation remains usable during GitHub or network loss. | Same-session posture | GitHub comments were required for every transition. | Local commands use the atomic snapshot; GitHub is required only for bind verification, durable handoff, and submission. |
| BC-19 | recovery behavior | Deletion of a durable plan blocks cold continuation, not same-session work. | Plan authority rule | Event history encouraged reconstruction from partial evidence. | Restore verifies the durable plan digest and fails closed when it is gone. |
| BC-20 | AI judgment | Base advancement is reconciled according to its substance, then validated and proportionally reviewed. | Build integrate and review steps; historical case 681 | Original base became immutable lifecycle authority. | Base is disclosed evidence, not an invariant. |
| BC-21 | submission prerequisite | The PR contract is complete and truthful before readying. | Pull-request template and close-linkage preflight | Receipt completeness competed with operator-readable delivery. | Preview reports missing contract evidence; apply only marks the draft ready. |
| BC-22 | operator authority | No coordinator operation can merge. | Protected-branch human gate | Supported, but buried inside a large command protocol. | No merge command or API call exists. |
| BC-23 | advisory practice | Status distinguishes missing evidence from decisions that need engineering judgment. | Conduct and recovery goal | A single "next legal action" overruled engineering context. | It returns missing evidence, judgment items, warnings, and either one unique prerequisite or unordered available activities. |
| BC-24 | explicit non-goal | The coordinator does not certify plan quality, finding rationale, implementation coherence, or release value. | Reviewer and operator boundaries | Schema validity was treated as quality evidence. | These remain explicit judgment fields and reviewer/operator responsibilities. |

### Hard holds and demonstrated failures

Every hard hold below prevents an observed or directly demonstrated Build failure. A new hold must be added
to this table with equivalent evidence before it can become mandatory.

| Hold | Failure it prevents |
|---|---|
| Plan missing or digest mismatch | Checkpoint or review proceeds against a different plan than the one approved; StarshipSuperjam/engine-template#964 exposed this exact-artifact risk. |
| Plan/depth approval absent | Implementation spends work and review effort before the operator has approved the Build gate. |
| Required reviewer silently omitted | The operator approves coverage that never runs, creating a false review claim. |
| Deliverable review absent | A draft is submitted with only author and test-suite judgment. |
| Validation absent or stale | The final commit differs from the commit that passed the checks. |
| Reviewed commit differs and no re-review judgment exists | Repairs or base reconciliation bypass the required proportional engineering decision. |
| Finding explicitly left blocking | The orchestrator knowingly submits a concern it said prevents this PR from shipping. |
| Registered preflight absent or failed | A repository-specific submission rule, such as close linkage, is skipped or known red. |
| PR contract incomplete | The operator receives an unreadable or materially incomplete merge surface. |
| PR is not the expected open draft during construction | Evidence is attached to the wrong or already-submitted claim. |
| Operation would merge | The coordinator crosses the human-only merge boundary. |

No other condition is a hard hold. In particular, reviewer severity, unexpected paths, accepted risks,
unresolved non-blocking findings, and the size of a diff are inputs to judgment rather than mechanical stop
conditions.

### Historical replay rule

The six normalized cases are not benchmarks for reproducing one model's prose. A scenario passes when the
coordinator permits the competent workflow outcome recorded in `expected` and does not impose any behavior
listed in `must_not`. Mechanical tests may construct the minimum snapshot needed to demonstrate that result.
The fixtures must remain stable, source-linked, free of transcript text, and broad enough to cover planning
correction, remedy rejection, ordinary discoveries, operator decisions, proportional re-review, base
reconciliation, network loss, and ready delivery.

## Significance

This boundary prevents Build guidance from becoming a procedural-completeness machine that pressures an AI
to implement every review suggestion or recursively audit every repair. It also keeps exact plan, review,
validation, and submission evidence available for the operator without creating a third durable ledger.

## Rationale

The canonical runbook and successful Build histories show that quality comes from grounded plans, cold
challenge, critical finding adjudication, direct verification, and proportional judgment. The failed first
implementation instead modeled those judgments as event transitions and invalidations, producing repeated
reviews without improving the PR. A small current-evidence snapshot preserves the useful mechanical facts
while leaving the orchestrator responsible for engineering coherence.

## Anti-choice

An append-only receipt chain with immutable base authority, automatic scope interlocks, severity-driven
remedies, and audit-triggering invalidations was considered and rejected. It can prove more internal events
occurred, but it cannot prove their judgment was good and it actively encourages redundant Issues, operator
interruptions, and recursive audits.

## Status

accepted
