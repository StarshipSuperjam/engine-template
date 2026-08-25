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
be one session. An Issue is never created merely because a Build exists — not even to track the work; an
Issue is intake, and a Build's work is carried by its draft PR. A Build that must continue cold recovers its
plan from the local plan library (see "Where the plan lives").

The plan is not authored here. It is authored, reviewed and SEALED through the Project Manager first, and a
Build binds that sealed plan's `build-plan.v2` payload. The discipline below is what that lifecycle enforces
on the way to a seal. Present a readable projection generated from that exact document; it is a view, not a
second authority, and must never be edited or translated back into JSON after approval. Keep raw intent
distinct from the AI's interpretation. Record observed evidence separately from inference, mark assumptions as
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
build_coordinator.py plan bind --plan <plan-id> \
  --repository <owner/repo> --pr <number> --operator-decision "<the operator's go>"
```

`--plan` names a SEALED plan in the local library, and nothing else enters a Build: an unsealed plan is
refused at the door with its remaining lifecycle steps named, as is one whose content moved after its seal.
For unattended work add `--issue <number>` — that Issue AUTHORIZES the work; it is never its plan.

The snapshot is durable and lives beside its sealed plan, so a killed Build resumes with its evidence
intact; a later command finds it from the worktree, and `--state <path>` still names one outright. It is one
atomically replaced, lock-protected document of current evidence, carrying no authority — not an event
ledger and not repository state. `status` derives the phase from evidence.

### Where the plan lives

In the local plan library, on this workstation. The snapshot keeps the plan's id, the digest its seal
minted, and the payload digest — never the plan's content. Every checkpoint, review packet, and submission
preview receives the payload again and refuses a mismatch.

No plan is published to GitHub: no promotion step, no plan block in an Issue or PR body. An Issue may still
AUTHORIZE a Build, which is what `--issue` records, but authorization and plan authority are two artifacts and
neither stands in for the other.

Cold continuation is anchored on the sealed plan RECORD. `handoff export --output <file>` writes the Build's
own evidence, redacted, to a file; `handoff restore --input <file>` reads it back and re-verifies the plan in
the library — same id, same sealed digest, same payload. Gone, unsealed or changed, and continuation is
blocked rather than guessed at. A Build whose executed plan was revised away from its seal cannot hand off
cold at all, since a cold session would recover the sealed payload rather than the one being built: finish it
in the session that holds it, or re-plan into a new plan.

No lifecycle event is a GitHub comment. GitHub or network loss does not stop same-session local work. Never
reconstruct an approved plan from a summary, transcript fragments, or implementation.

### 2. Assess risk and approve the Build gate

**Risk and depth are settled on the plan side, before the seal.** Run the knowledge impact check, then
`project_manager.py depths <plan>`: it lists only the depths worth offering for this repository's installed
reviewers, dropping any that would run what a lighter one does (only Quick when no reviewers,
StarshipSuperjam/engine-template#763), with each depth's resolved reviewer EFFORT.
No installed reviewer is a disclosed no-extra-review result, never a false green.
Fill `.engine/templates/risk-assessment.md` in plain language: headline, affected areas, what review and validation will run or is unavailable, suggested care level (following risk, not a prior preference; no time or cost estimate), guardrail weakening. Depth scales EFFORT, not model (see
`model-routing.md`): Claude `--effort`, Codex a `fork_turns="none"` fork at that effort, named in the Review
record.

The operator approves plan and depth together with `project_manager.py approve <plan> --depth
quick|standard|thorough --operator-decision "<their words>"`, which refuses without their recorded decision. **That one choice covers both gates**: it names the lenses the seal will require, and
it is the depth the Build's deliverable review runs at. Consent is given once, here. On the Build side,
`approve --plan <plan.json> --depth …` records the same depth against the bound payload; changing approved
depth clears review coverage, and progress prose does not. A sealed plan's revision is always the operator's
call, recorded with `--operator-change` and disclosed at merge.

The `trivial` profile is the one-entry fast path: its reduced plan needs raw intent, objective, one success obligation, one reversible work item, and no-spec disclosure—none of the normal profile's evidence, assumption, risk, scope, interpretation, or review-strategy fields. Same-session, quick depth, no cold lenses, and one commit keep one headline plus plan/depth approval as its only operator ceremony; validation and merge remain. A guarded-enforcement change, guardrail weakening, second item or commit, settled referent, or cold continuation requires revision to `normal` and renewed approval.

### 3. The plan review already happened

There is no plan review on this side. There is exactly one cold plan review per plan, run on the plan side
against the approved revision before the seal — `review packet` cuts it, `review record` files the one receipt
(re-rendering the packet and refusing a mismatched digest), `finding dispose` answers each finding, and `seal`
refuses while the recorded lenses do not cover the approved depth's roster. A bound plan is a reviewed plan by
construction, which is why this side has no plan-review gate and no waiver for one.

Recording is fallible, so it is correctable up to a named moment: `review amend` completes a partial record
until its first finding is dispositioned, `finding amend` corrects one finding until that finding is, and the
approval is fixed the moment any review is recorded against it, which pins the depth from panel time. Nothing
is correctable after a seal. And the seal refuses until the panel's outcome was SHOWN to the operator —
`present-findings <plan> --operator-decision "<their words>"` records that it was. Approve, seal and bind each
refuse without the operator's decision in their own words; the trail is published in the pull request. It is a
record of what was said, not proof that it was.

Adjudication is unchanged wherever it runs: accepting a concern is not accepting its remedy, and a finding may
be accepted and fixed, accepted and tracked, partly accepted with a bounded remedy, rejected with rationale, or
escalated — with whether it still blocks recorded separately. Severity alone never blocks. Before involving the
operator, synthesize the findings into one recommended call and state its tradeoff; never relay raw reviewer
outputs as the decision surface. Return to the operator only when the review changes design, law, authority, or
the agreed capability boundary, or leaves a genuine operator decision unresolved.

If the Build discovers the plan itself is wrong, the seal still holds: clone it, take the clone through its
own approval and panel, then `plan adopt --successor <id> --input <bound-plan.json>`. The Build keeps its
pull request and the integration evidence of every node the successor carries unchanged with unchanged
ancestry; changed nodes and their dependants reset, and the plan panel does not re-run.

What the Build still owes is DISCLOSURE. The composed PR contract renders the sealed plan review's findings,
their dispositions, and a disagreement line for any blocking finding decided not to block — read from the plan
record, so the Build's own receipt bookkeeping cannot strip them. A plan revised away from its seal has that
divergence disclosed too, with the review stated as not covering the delta.

### 4. Implement and reground

Choose an implementation strategy proportionate to the work: orchestrator-inline for small or coupled work,
isolated workers for cleanly separable work when context pressure justifies them, or the durable routine path
for unattended bulk work. Delegation returns work product to the orchestrator, which remains the single
writer and judges cohesion. Reconnaissance is not implementation: send a wide recall or impact sweep to
`engine-grounding-scout` and a broad file search to a native `Explore`. Both are cheap, neither can spawn,
and the judgment stays here.

Routine follows [Routine entry](routine-entry.md): the sealed plan in the local library supplies ordered work
items and the Issue supplies the authorization; the snapshot, handoff, and git record completed commits and
`N of M` progress, counted from `work integrate`, the only completion path. Owned product work
follows [Owned-product Build](owned-product-build.md), and work for a repository the operator does not own
follows [external contribution submission](external-contribution-submit.md). A v2 DAG Build's node lifecycle
follows [Build work dispatch](build-work-dispatch.md). If a worker fails, inspect what returned, repair
cohesion, and re-dispatch or complete the missing work without inventing workflow state.

Before each commit, run `checkpoint` with the exact plan and a short JSON note containing objective, current
work, named `work_item`, assumptions and accepted risks, non-goals, planned scope, remaining verification, and one judgment:
`aligned`, `plan_revision_required`, or `operator_decision_required`. The coordinator adds changed paths.
Unexpected paths are highlighted for judgment, not automatically forbidden. A missing or mismatched plan
blocks commit recording. A non-aligned judgment requires resolution before submission.

Write genuine deferrals at the code site with the governed [`ENGINE-TODO` marker grammar](../contracts/eADR-0035-deferred-work-marker.md)—which requires no Issue merely to record one—run `engine_todo.py list` in touched areas, and disposition covered work. Verify specifications, harness capability, and delegated findings against
first-hand authority — including a repository-state claim you would file as an engine Issue: verify it at the repo's freshly-fetched default branch (`checkout_health.claim_at_fresh_head`), and separately search that repo's open pull requests and existing Issues for the same resolution (`gh pr list`/`gh issue list --search` — a merge already shows in the default branch). Record the verified `owner/repo@sha`; if the fresh head cannot be read or the checkout is the wrong repo, report it unverified, never auto-closing an existing Issue. Iterate with focused
tests; `status` reports unordered activities and cannot know which engineering activity is best.

Reconcile the target branch before final validation; if it advanced, resolve by the substance of the change
(the original base is disclosed evidence, not immutable authority). Prepare the derived surfaces last with
`build_coordinator.py sync-artifacts` — it regenerates every registered generated surface (`.engine/knowledge/graph.json` and the rest) in dependency order from the reconciled tree, resolving a derived conflict by regeneration, never a side-pick, and validation refuses until they are current.
This reconcile is no longer the sole guarantee: the floor now requires freshness (eADR-0021), so GitHub backstops it at the merge boundary.

### 5. Validate, review the deliverable, and repair proportionately

Validation is now two evidence classes, earned in this order once the implementation is cohesive. While building, use focused tests — and run those through `engine-validation-runner`, which works in a disposable copy and returns a digest instead of thousands of log lines. It is the wrong tool for the two classes below: both bind evidence to the live tree, which a scout confined to a throwaway copy cannot produce.

1. **Candidate** — run `validate` (its preamble says so itself): the structural CI suite plus the self-tests selected as affected against the merge base, each bound to the current commit, with a run record the coordinator verifies against its own derivations (the committed tree, a clean working tree, a re-derived inventory) rather than believing. v2 adds `--plan` and refuses while any node is unintegrated. A repeat at the same content-addressed identity is a cache hit that re-runs nothing and mutates nothing; `--force` re-runs. Candidate evidence backs the build loop, the deliverable packet, and the repair gate — a disclosed narrowing from the full inventory, bounded by the imported proof below.
2. **Push the head and let `engine-ci` run.** Code events run the complete inventory; a metadata-only event (a body edit, or a label such as `guardrail-ack`) instead verifies a receipt an earlier full run left for the IDENTICAL tree, disclosing the reuse and its source run; any doubt resolves to a full run.
3. **Final** — never run locally: `validate final import` requires the pushed head current with its base and the live rollup green, then imports that run's tree-bound receipt through the CI gatekeeper's platform-filtered enumeration. `submit preview` re-reads the rollup and refuses distinctly on absent, pending, or red.

A later accepted repair must be green candidate evidence again on its new final commit, and its head's proof re-imported.

Only after green validation, create `review packet --stage deliverable --session-effort <level>`, carrying the exact raw intent, approved plan, settled criteria where present, reviewed commit and base, and impact evidence. The spec-conformance reviewer checks plan-derived success obligations every Build and settled criteria add a higher-authority comparison; the divergence hunter reverse-sweeps the diff against intent, plan, non-goals, and any settled criteria; other installed reviewers judge usability, technical integrity, and release safety at the approved depth. A pass may run the operator's code, so each shell-capable persona runs it only in a throwaway copy it makes itself (never worktree-ing or repointing a checkout it did not create); creating the deliverable packet snapshots the checkout, and the submission preflight's required `checkout-integrity` leg (`review_integrity`) refuses to report ready if the review moved its origin, branch, or stash; a companion advisory `checkout-worktrees` leg surfaces a stray worktree registration without blocking, since a concurrent peer may add one legitimately.

**State the panel's effort, and record what each lens delivered.** A depth promises an effort, and a reviewer here carries none of its own — it inherits the session's, so the session's effort is the panel's ceiling. `--session-effort` says what that is; a packet that would under-deliver the approved depth is refused (raise the session and re-cut, or re-approve at a lighter depth), and `--accept-effort-shortfall` proceeds while publishing the gap in the pull-request body. Each `review record` carries `--delivered-effort`, and `--code-execution none|discarded-copy|in-place` for what the lens did with the change's code. All of these are self-reported; the disclosures say so.

**Record a whole round from one file.** `review record --findings-from-file` takes the receipt's ids and `finding record --from-file` the dispositions, from one `build-findings-batch.v1` file — so they cannot disagree. A malformed entry records nothing. The per-finding flags still work for a one-off.

Record the reviewed commit and critically adjudicate findings. After accepted repairs, measure
reviewed-to-final divergence with `repair assess` and make one engineering judgment:

- `none`: direct verification is sufficient and another independent pass would be disproportionate.
- `scoped`: rerun only lenses materially affected by the repair.
- `full`: rerun all applicable deliverable lenses because architecture, authority, or broad behavior changed.

Diff size informs but never chooses. A focused re-review's prescribed repair receives another proportional
judgment; `none` is valid and terminates the loop — and because it also clears the repair packet, it refuses when recorded receipts would go with it, until `--accept-receipt-loss`. A receipt binds to the commit RANGE its lens read, so a re-bind keeps every receipt still covering the new divergence and asks only the lenses that owe a read, naming the commits. A commit carrying only regenerated artifacts invalidates no binding and opens no round. There is no automatic audit recursion. A scoped or full
repair packet requires validation for the repaired commit. If target-branch reconciliation happens after
review, validate it and make the same nature-based judgment.

**A large or behaviour-changing repair after a lighter depth signals the depth was under-chosen.** A Standard
review then a repair that fixes a serious-or-blocking finding *and* changes behaviour — or a large divergence —
leans `scoped`/`full` over `none`: the fix-diff is evidence the change outgrew its depth. Depth stays the orchestrator's judgment, never a mechanical threshold (eADR-0041); continuing is bounded — rounds count whatever the judgment, and after two `repair assess` stops until `--guidance` records the operator's answer. That narrows eADR-0041's "never an escalation" to a count-based stop for cost; coverage and lens count stay uncapped. One design panel per Build: a completed panel freezes the plan, and `plan revise` names the ways on.

### 6. Preflight and submit

A coordinator-driven Build composes this contract mechanically: fill the typed claim from `contract template`,
check it with `contract preview`, and write it with `contract apply` — which folds the close-linkage preflight
and binds the completeness result to the final commit. The session supplies only judgment-bearing narrative;
the manual fill described next is the human and non-coordinator fallback.

Read `.github/pull_request_template.md` in full and fill its literal contract, including the consent preamble,
Scope/Behaviors, Validation, operator-readable Review record, Demonstration, and AI involvement. The Review
record says what depth and plain-language checks ran, whether code was executed in a throwaway copy, how
findings were dispositioned, and—when final differs from reviewed—the commits, divergence, judgment, and any
focused result. Include spec-derived acceptance steps or honest no-spec line, change profile, and a genuinely
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
the claim to the operator; the Build ends there. Reach it through this gate, not a bare `gh pr ready` — `plan
bind` labels the PR `engine-coordinator-owned` and `status`/`checkpoint` carry a standing reminder.

### Coordinator status and holds

`status [--json]` returns derived phase, missing submission evidence, items needing engineering judgment,
warnings, and either one mechanically unique next prerequisite or unordered engineering activities. Its
suggestion is never more authoritative than the orchestrator's understanding of the work.

Hard holds are limited to: unavailable or mismatched plan authority; absent plan/depth approval; silently
omitted approved reviewer coverage; absent deliverable review; validation stale or red for final; post-review
change without a proportional judgment; a finding explicitly left blocking this PR; missing or failed
registered preflight; incomplete PR contract; wrong/non-draft PR during construction; and any operation that
would merge. Each is tied to a demonstrated failure in `.engine/contracts/eADR-0041-build-coordinator-behavior.md`.
Unexpected paths, reviewer severity, diff size, and non-blocking findings remain evidence or judgment inputs; an
`unresolved` assumption instead holds the `ready` phase until cleared by `assumption dispose` or `plan revise`.

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
