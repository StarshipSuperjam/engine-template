---
title: Build validation and review — validate, review the deliverable, and repair proportionately
---
## Purpose

Read from candidate validation through the last repair judgment — the coordinator's `finding-disposition`,
`deliverable-review`, `repair-assessment`, and `final-validation` phases: earn the two validation evidence
classes, run the deliverable review, adjudicate its findings, and judge each repair proportionately.
The surrounding flow is [Build orchestration](build-orchestration.md).

## Steps

### Validate

Validation is now two evidence classes, earned in this order once the implementation is cohesive.

1. **Candidate** — run `validate` (its preamble says so itself): the structural CI suite plus the self-tests selected as affected against the merge base (in a deployed copy, a change set outside everything the Engine owns selects only the standing guard, recorded as scope `project-only`), each bound to the current commit, with a run record the coordinator verifies against its own derivations (the committed tree, a clean working tree, a re-derived inventory) rather than believing. v2 adds `--plan` and refuses while any node is unintegrated. A repeat at the same content-addressed identity is a cache hit that re-runs nothing and mutates nothing; `--force` re-runs. Candidate evidence backs the build loop, the deliverable packet, and the repair gate — a disclosed narrowing from the full inventory, bounded by the imported proof below for any change that touches the Engine.
2. **Push the head and let `engine-ci` run.** Code events that touch anything the Engine owns run the complete inventory; a deployed copy's product-only change set runs the validator alone, disclosed, with a project-only receipt the final import accepts only after re-deriving that verdict; a metadata-only event (a body edit, or a label such as `guardrail-ack`) instead verifies a receipt an earlier full run left for the IDENTICAL tree, disclosing the reuse and its source run; any doubt resolves to a full run.
3. **Final** — never run locally: `validate final import` requires the pushed head current with its base and the live rollup green, then imports that run's tree-bound receipt through the CI gatekeeper's platform-filtered enumeration. `submit preview` re-reads the rollup and refuses distinctly on absent, pending, or red.

A later accepted repair must be green candidate evidence again on its new final commit, and its head's proof re-imported.

### Review the deliverable

Only after green validation, create `review packet --stage deliverable`, carrying the exact raw intent, approved plan, settled criteria where present, reviewed commit and base, and impact evidence. The spec-conformance reviewer checks plan-derived success obligations every Build and settled criteria add a higher-authority comparison; the divergence hunter reverse-sweeps the diff against intent, plan, non-goals, and any settled criteria; other installed reviewers judge usability, technical integrity, and release safety at the approved depth. A pass may run the operator's code, so each shell-capable persona runs it only in a throwaway copy it makes itself (never worktree-ing or repointing a checkout it did not create); creating the deliverable packet snapshots the checkout, and the submission preflight's required `checkout-integrity` leg (`review_integrity`) refuses to report ready if the review moved its origin, branch, or stash; a companion advisory `checkout-worktrees` leg surfaces a stray worktree registration without blocking, since a concurrent peer may add one legitimately.

**Record what each lens did with the code.** Each `review record` carries `--code-execution none|discarded-copy|in-place` for what the lens did with the change's code; it is self-reported, and the pull-request body's disclosure says so.

**Record a whole round from one file.** `review record --findings-from-file` takes the receipt's ids and `finding record --from-file` the dispositions, from one `build-findings-batch.v1` file — so they cannot disagree. A malformed entry records nothing. The per-finding flags still work for a one-off.

### Adjudicate

Adjudication is unchanged wherever it runs: accepting a concern is not accepting its remedy, and a finding may
be accepted and fixed, accepted and tracked, partly accepted with a bounded remedy, rejected with rationale, or
escalated — with whether it still blocks recorded separately. Severity alone never blocks. Before involving the
operator, synthesize the findings into one recommended call and state its tradeoff; never relay raw reviewer
outputs as the decision surface. Return to the operator only when the review changes design, law, authority, or
the agreed capability boundary, or leaves a genuine operator decision unresolved.

### Repair proportionately

Record the reviewed commit and critically adjudicate findings. After accepted repairs, measure
reviewed-to-final divergence with `repair assess` and make one engineering judgment:

- `none`: direct verification is sufficient and another independent pass would be disproportionate.
- `scoped`: rerun only lenses materially affected by the repair. With no `--lens` the coordinator defaults to the lenses that raised a blocking or blocking-this-PR finding at the review being repaired; naming lenses overrides it either way.
- `full`: rerun all applicable deliverable lenses because architecture, authority, or broad behavior changed.

Diff size and the surfaces a repair touched inform but never choose. The repair packet names `anchor..commit` as the review's subject, so a re-review reads the repair rather than the whole pull request. A focused re-review's prescribed repair receives another proportional
judgment; `none` is valid and terminates the loop — and because it also clears the repair packet, it refuses when recorded receipts would go with it, until `--accept-receipt-loss`. A receipt binds to the commit RANGE its lens read, so a re-bind keeps every receipt still covering the new divergence and asks only the lenses that owe a read, naming the commits. A commit carrying only regenerated artifacts invalidates no binding and opens no round. There is no automatic audit recursion. A scoped or full
repair packet requires validation for the repaired commit. If target-branch reconciliation happens after
review, validate it and make the same nature-based judgment.

**A large or behaviour-changing repair after a lighter depth signals the depth was under-chosen.** A Standard
review then a repair that fixes a serious-or-blocking finding *and* changes behaviour — or a large divergence —
leans `scoped`/`full` over `none`: the fix-diff is evidence the change outgrew its depth. Depth stays the orchestrator's judgment, never a mechanical threshold; continuing is bounded by what rounds SPEND. A round that dispatches two or more cold lenses is counted; three counted rounds is the budget, and six rounds of any kind is the absolute ceiling. Either stop refuses until `--guidance` records the operator's answer, and prints the trajectory — including a highlight when a round moved more code and guarded surface than the one before it, the sign a fix broke something past the finding it answered. A `none` judgment and a single cold check on a low-yield change are recorded and disclosed but spend no counted budget; by convention that cheap check is one lens on a minimal model, because its whole value is the cold context — a convention, not a mechanism, so the honest worst case these two bounds allow is three full panels plus three further single-lens rounds on any model. Each round is classified from the previous round's end, not from the reviewed commit, and the rounds record reaches the operator at merge. The spend-based stops bound cost; coverage and lens count stay uncapped. One design panel per Build: a completed panel freezes the plan, and `plan revise` names the ways on.

## Done when

Candidate validation is green for the final commit and its engine-ci proof is imported; every finding the
deliverable review reported carries a disposition; every reviewed-to-final change has a recorded proportional
judgment and its required focused receipts; and `status` reports `submission-preflight`, naming
[Build submission](build-submission.md).
