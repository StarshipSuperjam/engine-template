---
title: Build orchestration — from approved intent to a ready pull request
---
## Purpose
Build turns an operator-approved plan into one coherent pull request. The orchestrating AI acts as the
senior engineer: it frames the problem, challenges assumptions, chooses the implementation, adjudicates
review findings, and decides whether repairs deserve more review. The Build coordinator is its instrument
panel. It preserves the exact plan and current evidence, prepares cold-review packets, records what commit
was checked, runs validation and preflights, and reports what remains. It never decides whether the work is
good, and it never merges.
The protected-branch merge remains the only binding gate. The coordinator may change an open draft pull
request to ready only after the evidence described here is complete. The operator alone merges it.
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

Reviewer severity is advice. It never selects a remedy and never makes a finding block automatically.

### 1. Plan and open the claim

Open one draft pull request for the Build and keep it draft throughout construction. Title it `Kind: what
changed`, using the kinds in `.github/pull_request_template.md`. A Build is one PR-shaped change; it need not
be one session.

Turn the initiating request or Issue into a structured JSON `build-plan.v1` document. Present a readable
harness projection generated directly from that exact document; it is a view, not a second authority, and
must never be independently edited or translated back into JSON after approval. Keep raw intent distinct from
the AI's interpretation. Record observed evidence separately from inference, mark assumptions as
verified, accepted risk, or unresolved, and state the objective, checkable success obligations, scope,
non-goals, important risks, implementation outline, and review strategy. Include settled-spec mapping when
one exists. Otherwise disclose that there is no settled spec; the plan's success obligations still govern
conformance review.

Follow [Build product grounding](build-product-grounding.md): retain advisory milestone and readiness
evidence, resolve settled descriptions without degrading failed reads to no-spec, map every selected
canonical criterion, and derive its review steps. Build consumes settled intent; missing product description
work returns through product intake instead of being improvised here.

Planning is deliberative, not form filling. Check the strongest case against the change, smaller or no-build
alternatives, likely failure modes, and whether the plan quietly turns uncertainty into certainty. Use the
active codes of conduct: preserve intent, ground claims, prefer the smallest safe change, deliver the full
agreed capability, and do not expand scope without authority. The coordinator can prove only that this
reasoning is present and that later work uses the same plan. It cannot prove the reasoning is sound.

Bind the plan once:

```text
build_coordinator.py --state <OS-temp-path> plan bind \
  --source session --input <plan.json> --repository <owner/repo> --pr <number>
```

The snapshot must live in the OS temporary directory. It is one atomically replaced, lock-protected document
of current evidence, not an append-only event ledger and not repository state. There is no editable phase;
`status` derives the phase from evidence.

### Where the plan lives

For an interactive Build expected to finish in this harness session, the harness is the content store. The
snapshot keeps only the canonical digest and source facts. Every checkpoint, review packet, and submission
preview receives the plan again and refuses a mismatch. This works whether or not the runtime has a formal
Plan feature: the orchestrator may author and present the same `build-plan.v1` JSON conversationally and pass
that exact document by file or stdin. The JSON document is the harness plan, not a second plan authority.

A GitHub Issue is not created merely because a Build exists. If a same-session Build began from a suitable
Issue, that Issue remains the intent record but need not duplicate the plan. A direct request needs only the
draft PR.

Before intentional cold-session or unattended continuation, promote the exact plan to a suitable writable
Issue. Promotion appends or replaces one bounded machine block in the Issue body while preserving the
human-authored text and GitHub's edit history. It requires an explicit visibility acknowledgement, compares
the Issue body again immediately before writing, aborts on concurrent edits, and verifies the written bytes.
Reuse an originating Issue that represents exactly this Build. When none is suitable, `plan promote --create-issue
<title>` uses `.engine/tools/issue_author.py`, applies the `engine` label, states ordered scope and recovery purpose, then publishes the bounded plan. A broad epic,
read-only external Issue, or Issue spanning independent PRs is not suitable authority.

No lifecycle event is a GitHub comment. GitHub or network loss does not stop same-session local work. A
deleted durable plan blocks only cold continuation; never reconstruct an approved plan from a summary,
transcript fragments, or implementation. `handoff export --publish --ack-visibility` places one bounded,
redacted snapshot block in the PR contract with an optimistic-concurrency check; it never creates a comment.
`handoff restore --repository <owner/repo> --pr <number>` reads that block and verifies the durable Issue plan.
File/stdin export and restore remain available for a harness that transports the same bytes itself.

### 2. Assess risk and approve the Build gate

Run the knowledge impact check and inspect installed review personas. Fill
`.engine/templates/risk-assessment.md` in plain language: headline, affected areas, exactly what review and
validation will run or is unavailable, suggested care level, and any guardrail weakening. Never invent a
time or cost estimate. The suggested depth follows risk, not a prior preference.

The operator iterates the plan to solid and approves the plan and review depth together. Record that approval
with `approve --plan <plan.json> --depth quick|standard|thorough`. Changing the plan content changes its
digest and clears approval and applicable review evidence. Changing approved depth also clears review
coverage. Progress prose does neither.

An ordinary implementation leaf does not revise the plan. Revision is warranted only when intent, outcome,
capability boundary, non-goals, settled criteria, authority, or agreed scope changes.

The `trivial` profile is the one-entry fast path: its reduced plan needs raw intent, objective, one success
obligation, one reversible work item, and no-spec disclosure—none of the normal profile's evidence, assumption, risk, scope, interpretation, or review-strategy fields. Same-session, quick depth, no cold lenses, and one
commit keep one headline plus plan/depth approval as its only operator ceremony; validation and merge remain. A guarded-enforcement change,
guardrail weakening, second item or commit, settled referent, or cold continuation requires revision to
`normal` and renewed approval.

### 3. Run one cold plan review

`review packet --stage plan` constructs one exact packet containing raw initiating intent, the approved
plan, cited evidence supplied as impact input, settled criteria when present, installed and required lenses,
protocol digest, and each required reviewer's source path and content digest. The approved depth determines
required coverage; Thorough runs every installed lens. A changed reviewer contract invalidates only that lens's receipt; unchanged lenses remain current against the same referent.
No installed reviewer is a disclosed no-extra-review result, never a false green.

Cold reviewers judge product intent, architecture, feasibility, and risk/governance within their independent
mandates. Record each receipt and its finding IDs. Then critically adjudicate every finding under the finding
policy. Accepting a concern does not mean accepting its remedy. A finding may be accepted and fixed, accepted
and tracked, partly accepted with a bounded remedy, rejected with rationale, or escalated for a genuine
operator decision. Record separately whether it still blocks this PR. Severity alone never blocks.
Before involving the operator, synthesize the plan-review findings into one recommended call and state its
tradeoff; never relay a stack of raw reviewer outputs as the decision surface.
Return to the operator only if the review changes design, law, authority, the agreed capability boundary, or
leaves a genuine operator decision unresolved. Engineering leaves are the orchestrator's responsibility.
Normal and Routine implementation checkpoints remain closed until this review's required receipts and
finding dispositions are complete and no plan finding remains explicitly blocking.
When a completed implementation is adopted after this before-code gate, the operator may explicitly waive the
now-retrospective plan review only for a same-session normal Build bound to that already-implemented commit;
record the commit and reason, disclose the waiver, and never fabricate receipts. Routine and prospective work
cannot use the waiver.

### 4. Implement and reground

Choose an implementation strategy proportionate to the work: orchestrator-inline for small or coupled work,
isolated workers for cleanly separable work when context pressure justifies them, or the durable routine path
for unattended bulk work. Delegation returns work product to the orchestrator, which remains the single
writer and judges cohesion.

Routine follows [Routine entry](routine-entry.md): the immutable Issue plan supplies ordered work items while
the snapshot, bounded PR handoff, and git record completed commits and `N of M` progress. Owned product work
follows [Owned-product Build](owned-product-build.md). Work for a repository the operator does not own follows
[external contribution submission](external-contribution-submit.md), where exact review packets may run
before an upstream PR exists. If a worker fails, inspect what returned, repair cohesion, and re-dispatch or
complete the missing work without inventing workflow state.

Before each commit, run `checkpoint` with the exact plan and a short JSON note containing objective, current
work, named `work_item`, assumptions and accepted risks, non-goals, planned scope, remaining verification, and one judgment:
`aligned`, `plan_revision_required`, or `operator_decision_required`. The coordinator adds changed paths.
Unexpected paths are highlighted for judgment, not automatically forbidden. A missing or mismatched plan
blocks commit recording. A non-aligned judgment requires resolution before submission.

Write genuine deferrals at the code site with the governed [`ENGINE-TODO` marker grammar](../contracts/eADR-0035-deferred-work-marker.md)—which requires no Issue merely to record one—run `engine_todo.py list` in touched areas, and disposition covered work. Verify specifications, harness capability, and delegated findings against
first-hand authority. Iterate with focused tests; `status` reports unordered activities and cannot know which
engineering activity is best.

Reconcile the target branch before final validation. If it advanced, resolve according to the substance of
the change. Regenerate `.engine/knowledge/graph.json` and `.engine/self-map.md` last from the reconciled tree;
their textual conflicts are resolved by regeneration, never by choosing a side. The original base is
disclosed evidence, not immutable authority.

### 5. Validate, review the deliverable, and repair proportionately

When the implementation is cohesive, run `validate` (the CI suite and self-tests, each bound to the current commit). Use focused tests while building and run full validation once on the cohesive candidate; a later accepted repair must be green again on its new final commit.

Only after green validation, create `review packet --stage deliverable`, carrying the exact raw intent, approved plan, settled criteria where present, reviewed commit and base, and impact evidence. The spec-conformance reviewer checks plan-derived success obligations every Build and settled criteria add a higher-authority comparison; the divergence hunter reverse-sweeps the diff against intent, plan, non-goals, and any settled criteria; other installed reviewers judge usability, technical integrity, and release safety at the approved depth. A pass may run the operator's code, so each shell-capable persona runs it only in a throwaway copy it makes itself (never worktree-ing or repointing a checkout it did not create); creating the deliverable packet snapshots the checkout, and the submission preflight's required `checkout-integrity` leg (`review_integrity`) refuses to report ready if the review moved its origin, branch, or stash; a companion advisory `checkout-worktrees` leg surfaces a stray worktree registration without blocking, since a concurrent peer may add one legitimately.

Record the reviewed commit and critically adjudicate findings. After accepted repairs, measure
reviewed-to-final divergence with `repair assess` and make one engineering judgment:

- `none`: direct verification is sufficient and another independent pass would be disproportionate.
- `scoped`: rerun only lenses materially affected by the repair.
- `full`: rerun all applicable deliverable lenses because architecture, authority, or broad behavior changed.

Diff size informs but never chooses. A focused re-review's prescribed repair receives another proportional
judgment; `none` is valid and terminates the loop. There is no automatic audit recursion. A scoped or full
repair packet requires validation for the repaired commit. If target-branch reconciliation happens after
review, validate it and make the same nature-based judgment.

### 6. Preflight and submit

Read `.github/pull_request_template.md` in full and fill its literal contract, including the consent preamble,
Scope/Behaviors, Validation, operator-readable Review record, Demonstration, and AI involvement. The Review
record says what depth and plain-language checks ran, whether code was executed in a throwaway copy, how
findings were dispositioned, and—when final differs from reviewed—the commits, divergence, judgment, and any
focused result. Keep reviewer-internal names out of operator-facing prose. Include the standing unresolved
conversation notice, spec-derived acceptance steps or honest no-spec line, change profile, and a genuinely
operator-runnable demonstration (or the real reason none is observable).

Follow [Build submission evidence](build-submission-evidence.md) for complete logs, index and scope-profile
disclosure, spec-derived review steps, hard-check declarations, unresolved conversations, recognized
automation, fail-open findings, unavailable live helpers, reviewer code execution, and the demonstration.
Deterministic results are coordinator evidence; truthfulness remains the orchestrator's responsibility.

Run `preflight`. It reads the live draft body, evaluates the existing PR-body completeness rule, and runs the
close-linkage preflight. Results and PR-contract completeness bind to the final commit. Resolve any emitted
defang or failed check, update the PR body, and rerun. `submit preview --plan <plan.json>` then verifies the
exact plan, current local/remote head, confirmed mergeability, complete review and dispositions, fresh green
validation and preflights, proportional repair judgment, and complete PR contract.

`submit apply` can invoke only `gh pr ready`. It has no merge command or merge API path. Marking ready submits
the claim to the operator; the Build ends there.

### Coordinator status and holds

`status [--json]` returns derived phase, missing submission evidence, items needing engineering judgment,
warnings, and either one mechanically unique next prerequisite or unordered engineering activities. Its
suggestion is never more authoritative than the orchestrator's understanding of the work.

Hard holds are limited to: unavailable or mismatched plan authority; absent plan/depth approval; silently
omitted approved reviewer coverage; absent deliverable review; validation stale or red for final; post-review
change without a proportional judgment; a finding explicitly left blocking this PR; missing or failed
registered preflight; incomplete PR contract; wrong/non-draft PR during construction; and any operation that
would merge. Each is tied to a demonstrated failure in `.engine/contracts/eADR-0041-build-coordinator-behavior.md`.
Assumptions, unexpected paths, reviewer severity, diff size, and non-blocking findings remain evidence or
judgment inputs.

## Done when

The exact approved plan and depth are recorded; required plan and deliverable review ran; every reported
finding has an orchestrator disposition; final validation and registered preflights are green; any
reviewed-to-final change has a recorded proportional judgment and required focused receipts; the PR contract
truthfully explains the delivered behavior and evidence; the reconciled PR is mergeable; and the coordinator
has marked the draft ready for the operator. Nothing merged automatically.

## Notes

The consumed-review-lenses record below is machine-read. It keeps installed personas connected to a Build
stage; the coordinator still derives actual coverage from the installed roster and approved depth.

```text
consumed-review-lenses:
  plan-review gate: product-intent, architecture, feasibility, risk-governance
  product-design spec-lock ceremony: product-intent, architecture, feasibility, risk-governance
  pre-submission gate: spec-conformance, divergence-hunter, usability, technical-integrity, security-governance
```
