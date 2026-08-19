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

_Amended 2026-08-15 for the build-plan.v2 execution-DAG capability: BC-25–BC-27 add the new coordinator
integrity guarantees (attempt/base result binding, compare-and-swap claim writes, and completion only through
integration), each demonstrated by a focused test cited inline. The capability introduced no new submission
hard hold — the graph's resource and slot admission is a derived refusal, not a merge-blocking condition._

_Amended 2026-08-16 for coordinator-owned PR composition: the `contract` verb family (`template`, `preview`,
`apply`) composes the complete PR body from one typed claim (`pr-body-claim.v1`, judgment-bearing narrative and
session-only observations) plus coordinator-computed evidence, replacing the hand-pasted template. This does not
change BC-21: the coordinator now mechanically assembles and gate-checks the contract's completeness, while the
truthfulness of its narrative remains AI-and-operator judgment (BC-24). It introduces no new hard hold — a
`contract apply` refusal (a stale source-body digest, a concurrent edit, or non-convergence) is a derived
refusal that restores the prior body, never a merge wall; the existing `pr-contract` hard hold is unchanged, and
`apply` never marks the draft ready. The close-linkage posture is unchanged — advisory lines are folded, the
uniquely bounded defang is applied, contradictions stay visible without becoming a wall._

_Amended 2026-08-18 for reviewer-finding privacy (StarshipSuperjam/engine-template#981): a finding's
`private_reference` is reviewer-internal detail and is **never published** to the public PR body. The
`pr-body-claim.v1` finding-summary lane already enforced this (it carries only the operator-safe
`operator_summary` plus an optional deliberately-public `public_reference`); this amendment binds the two lanes
that predated and bypassed it — the reviewer-disagreement line (`build_coordinator_review.disagreement_line`,
which BC-21's `pr-contract` preflight requires present in the body) and the durable handoff's published
`finding_summaries` (`_handoff`) — to the same rule. The redaction lives inside `disagreement_line`, the single
source both the composed body and the preflight derive from, so they cannot drift; `operator_summary` is
required on exactly these downgraded-blocking findings, so the disclosure is never emptied. The
`build-handoff.v1/v2` schemas now **forbid** `private_reference` (removed from `properties` under
`additionalProperties: false`) — a load-bearing structural guard: any future re-introduction into the published
handoff fails validation rather than silently re-leaking. `private_reference` is retained as local reviewer
state in `build-state` (written by `cmd_finding_record`, read by no publish path); a legacy handoff still
carrying the field is stripped on restore, not failed closed. This strengthens BC-21 (a truthful, non-leaking
contract) and introduces no new hard hold._

_Amended 2026-08-19 for low-friction assumption resolution (StarshipSuperjam/engine-template#1014). An
assumption authored `unresolved` pins the Build at the `engineering-decision` phase, which — because `submit
apply` requires the `ready` phase — holds submission until it is resolved. Before this amendment the only way
to resolve one was `plan revise`, which mints a new plan digest and so invalidates approval and every review
receipt: an assumption the review had already settled could be cleared only by re-running the whole review for
an identical result, which manufactured a real incentive to reach `ready` by a bare `gh pr ready` outside the
submit gate (the failure StarshipSuperjam/engine-template#1014 records). The `assumption dispose` verb resolves an assumption authored
`unresolved` to `verified` or `accepted-risk` in the **receipt layer** (a new `assumption_dispositions` field,
written by `store.mutate` exactly as finding dispositions are), bound to a required `--basis`, WITHOUT editing
the plan — so the plan digest, approval, and review receipts survive and no re-review is forced. `_status`
computes each assumption's **effective** status (authored status overlaid with any disposition) once, feeding
both the judgment lines and the phase gate, so only an *effectively*-unresolved assumption still holds the
phase. This is a self-clearable engineering-judgment hold, never a mechanical hard hold: it introduces no new
row in the hard-holds table, forces no re-review, and requires no operator escalation. Its honesty rests on
disclosure, not tamper-proofing — a disposition is a self-attested judgment, so every post-approval resolution
(both `verified` and `accepted-risk`) is surfaced durably in `status` and in the PR Review record the operator
reads at merge, distinct from a plan-authored status; the operator's merge stays the binding gate. A `plan
revise` clears `assumption_dispositions` (cycle-bound evidence, like findings and receipts), so a revision
re-opens its premises; a depth-only change keeps the plan digest and so keeps the dispositions. This does not
close the residual that an honest session could still bypass the gate — a durable independent build identity
(StarshipSuperjam/engine-template#914) is that fix; this amendment removes the manufactured *reason* to bypass
and adds a soft, recurring reminder (a bind-time `engine-coordinator-owned` PR label and a standing line in
`status`/`checkpoint`) that a coordinator-staged PR must reach `ready` through the submit gate._

### Classified assertions

| ID | Class | Required behavior | Canonical or observed source | Failed implementation | Replacement response |
|---|---|---|---|---|---|
| BC-01 | operator authority | The operator approves the plan and review depth together before implementation. | Build steps 1–2 | Split approval from the Build gate and repeatedly sought leaf-level permission. | One approval binds plan digest and depth; later operator input is reserved for design, law, scope, or authority. |
| BC-02 | AI judgment | The orchestrator turns nascent intent into a grounded, adversarially tested plan. | Conduct plus plan-review gate | Presence checks treated a structurally complete plan as trustworthy. | The plan shape preserves the reasoning referent; cold reviewers judge its quality. |
| BC-03 | mechanical fact | The exact approved plan reaches checkpoints and reviewers. | Plan and both review gates | Receipt events multiplied copies and identities without improving the referent. | Canonical digest plus exact input verification; same-session content stays in the harness. |
| BC-04 | recovery behavior | A same-session plan may remain session-local; cold continuation requires an exact durable plan. | Build proportionality notes | Created or assumed a GitHub Issue for every Build. | Promote only when handoff is needed; reuse a suitable Issue before creating a Build Issue. |
| BC-05 | explicit non-goal | GitHub comments are not a lifecycle database. | Operator correction during StarshipSuperjam/engine-template#964 | Used append-only PR comments as an event ledger. | One local atomic snapshot; one bounded handoff block in the PR contract only for intentional handoff. |
| BC-06 | mechanical fact | The installed reviewer roster and omissions are discoverable, and receipts stay bound to the reviewer contract that produced them. | Consumed-review-lenses record | Review identity became a recursive protocol. | Derive personas from committed metadata; bind each receipt to the shared referent plus that lens's source path and digest, so one prompt change invalidates only its dependent receipt. |
| BC-07 | AI judgment | Reviewer severity and proposed remedies are advice; the orchestrator critically adjudicates both. | Reviewer boundaries; historical cases 865, 924, 955 | Severity effectively selected the response and encouraged blind repair. | Findings record disposition, rationale, and `blocks_this_pr`; severity never decides readiness. |
| BC-08 | operator authority | Only genuine design, law, scope-boundary, authority, or unresolved blocking decisions return to the operator. | Conduct and Build gate | Routine leaves and audit nits repeatedly stopped the Build. | `escalated` is available, but ordinary engineering leaves remain the orchestrator's work. |
| BC-09 | advisory practice | Unexpected paths, assumptions, and nearby risks should be shown to the engineer. | Impact check and checkpoints | Path policing turned every surprise into an interlock. | Status and checkpoint highlight unexpected paths and nearby risks without forbidding progress; an assumption authored `unresolved` additionally holds the `ready` phase until the orchestrator resolves it — low-friction via `assumption dispose` (receipt-layer, review-preserving) or `plan revise` — a self-clearable judgment hold, never a mechanical hard hold. |
| BC-10 | AI judgment | An implementation discovery revises the plan only when it changes intent, outcome, capability boundary, non-goals, settled criteria, authority, or agreed scope. | Plan settlement; historical cases 886 and 924 | Leaf changes invalidated approvals and reviews. | The orchestrator explicitly selects aligned, revision, or operator-decision posture. |
| BC-11 | submission prerequisite | The authoritative plan must be reproducible and match the approved digest. | Both review gates | Event repair attempted to reconstruct authority. | Missing or mismatched plan is a hard hold; no transcript reconstruction. |
| BC-12 | submission prerequisite | Approved reviewer coverage cannot be silently omitted. | Build review gates | Coverage was entangled with phase advancement. | A manifest names required lenses; absent receipts hold submission unless the operator explicitly waives a now-retrospective plan review with a disclosed reason. Deliverable review cannot be waived. |
| BC-13 | submission prerequisite | Deliverable review must run against a recorded commit. | Build step 6 | Multiple packet generations obscured the actual reviewed commit. | One reviewed commit, shared referent digest, and contract-specific lens-packet digest per receipt. |
| BC-14 | submission prerequisite | Findings must be dispositioned, and only findings explicitly left blocking hold submission. | Finding policy and human merge gate | `blocking`/`serious` labels automatically drove more work. | Completeness is mechanical; correctness of rationale and `blocks_this_pr` is engineering judgment. |
| BC-15 | submission prerequisite | Validation and required registered preflights must be green for the final commit; advisory checks remain visible. | Build steps 6–7 | Receipts could be current while the substantive commit changed. | Results bind to commit and PR-body digest as applicable; advisory close-linkage evidence cannot masquerade as a merge wall. |
| BC-16 | AI judgment | Reviewed-to-final divergence receives one proportional `none`, `scoped`, or `full` re-review judgment. | Build step 6; historical cases 681, 685, 955 | Invalidation rules produced recursive cold audits. | The coordinator measures and records the choice but never selects it. |
| BC-17 | explicit non-goal | A focused re-review's repair does not automatically trigger another review. | Historical case 685; operator correction during StarshipSuperjam/engine-template#964 | Every audit-created change created another audit obligation. | A fresh proportional judgment may be `none`, terminating the loop. |
| BC-18 | recovery behavior | Local implementation remains usable during GitHub or network loss. | Same-session posture | GitHub comments were required for every transition. | Local commands use the atomic snapshot; GitHub is required only for bind verification, durable handoff, and submission. |
| BC-19 | recovery behavior | Deletion of a durable plan blocks cold continuation, not same-session work. | Plan authority rule | Event history encouraged reconstruction from partial evidence. | Restore verifies the durable plan digest and fails closed when it is gone. |
| BC-20 | AI judgment | Base advancement is reconciled according to its substance, then validated and proportionally reviewed. | Build integrate and review steps; historical case 681 | Original base became immutable lifecycle authority. | Base is disclosed evidence, not an invariant. |
| BC-21 | submission prerequisite | The PR contract is complete and truthful before readying. | Pull-request template and close-linkage preflight | Receipt completeness competed with operator-readable delivery. | Preview reports missing contract evidence; apply only marks the draft ready. |
| BC-22 | operator authority | No coordinator operation can merge. | Protected-branch human gate | Supported, but buried inside a large command protocol. | No merge command or API call exists. |
| BC-23 | advisory practice | Status distinguishes missing evidence from decisions that need engineering judgment. | Conduct and recovery goal | A single "next legal action" overruled engineering context. | It returns missing evidence, judgment items, warnings, and either one unique prerequisite or unordered available activities. |
| BC-24 | explicit non-goal | The coordinator does not certify plan quality, finding rationale, implementation coherence, or release value. | Reviewer and operator boundaries | Schema validity was treated as quality evidence. | These remain explicit judgment fields and reviewer/operator responsibilities. |
| BC-25 | mechanical fact | A DAG worker result binds only to its claim's attempt id and base SHA. | build-plan.v2 work verbs; `test_build_coordinator_work.TestWorkClaims` | Without binding, a superseded worker's result could satisfy a replacement attempt or a wrong base and corrupt completion evidence. | The result is rejected unless its attempt id and reported base match the active claim. |
| BC-26 | mechanical fact | Every claim and result write compare-and-swaps against the current snapshot revision. | Work-verb `_work_mutate`; `test_build_coordinator_work.TestWorkClaims` | Without it, a concurrent or stale write could overwrite a sibling claim or lose a snapshot update. | Each work verb writes under an explicit from-revision guard and refuses a stale write. |
| BC-27 | mechanical fact | A DAG node is complete only with a recorded integration commit on the PR branch and focused verification. | Work integrate; `test_build_coordinator_work.TestWorkDispositions` | Without it, a returned worker artifact could be treated as completion without the orchestrator integrating and verifying it. | Completion requires an integration commit proven on the PR branch plus recorded focused verification; worker commits are transport, never completion. |

### Hard holds and demonstrated failures

Every hard hold below prevents an observed or directly demonstrated Build failure. A new hold must be added
to this table with equivalent evidence before it can become mandatory.

| Hold | Failure it prevents |
|---|---|
| Plan missing or digest mismatch | Checkpoint or review proceeds against a different plan than the one approved; StarshipSuperjam/engine-template#964 exposed this exact-artifact risk. |
| Plan/depth approval absent | Implementation spends work and review effort before the operator has approved the Build gate. |
| Required reviewer silently omitted | The operator approves coverage that never runs or carries no explicit plan-review waiver, creating a false review claim. |
| Deliverable review absent | A draft is submitted with only author and test-suite judgment. |
| Validation absent or stale | The final commit differs from the commit that passed the checks. |
| Reviewed commit differs and no re-review judgment exists | Repairs or base reconciliation bypass the required proportional engineering decision. |
| Finding explicitly left blocking | The orchestrator knowingly submits a concern it said prevents this PR from shipping. |
| Required registered preflight absent or failed | A repository-specific hard submission rule is skipped or known red; advisory close-linkage output is still recorded but does not block. |
| PR contract incomplete | The operator receives an unreadable or materially incomplete merge surface. |
| PR is not the expected open draft during construction | Evidence is attached to the wrong or already-submitted claim. |
| Operation would merge | The coordinator crosses the human-only merge boundary. |

No other condition is a hard hold. In particular, reviewer severity, unexpected paths, accepted risks,
unresolved non-blocking findings, and the size of a diff are inputs to judgment rather than mechanical stop
conditions. An assumption authored `unresolved` holds the `ready` phase (and so submission) until the
orchestrator resolves it, but this is a self-clearable engineering-judgment hold — cleared by `assumption
dispose` (receipt-layer, review-preserving) or `plan revise`, never forcing re-review or operator escalation —
not a hard hold in the sense of this table, which enumerates missing evidence and authority.

### Historical scenario traceability

The eight normalized cases are not benchmarks for reproducing one model's prose, and their presence is not
itself a behavioral replay. They trace observed competent outcomes into named expectations and prohibitions.
Only an expectation actually exercised by a focused test is behavioral evidence; otherwise it remains
scenario traceability for independent source-contract review.
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
