---
title: Build validation and review — validate, review the deliverable, and repair proportionately
---
## Purpose
Earn candidate/final evidence, review the deliverable, adjudicate findings and judge repairs. Follow [Build orchestration](build-orchestration.md).

## Steps

### Validate the cohesive implementation
1. Run `validate --plan <payload.json>` in the live Build. It runs structural CI and self-tests selected against the merge base; non-tool changes select the complete inventory. A deployed project-only change selects the standing guard.
   The coordinator verifies the clean committed tree, inventory and run record; v2 refuses unintegrated nodes. An identical content-addressed candidate is a read-only cache hit; `--force` reruns it. Candidate evidence backs packets and repairs, not final submission by itself.
2. Push the head for `engine-ci`. Code events touching the Engine run the complete inventory; deployed product-only changes run the validator with a disclosed project-only receipt. Metadata events may reuse a proven identical-tree receipt; doubt falls back to a full run.
3. Run `validate final import` after candidate validation. It requires the pushed head current with its base and the live rollup green, importing a tree-bound receipt through platform-filtered enumeration. Final proof is never run locally; submission distinguishes absent, pending and red results.
Every accepted repair needs current-head candidate evidence and a fresh final import.

### Review the deliverable cold
Follow [Scoped native agents](scoped-agent-orchestration.md) when dispatching reviewers. Only after green validation, create `review packet --stage deliverable --plan <payload.json>` with raw intent, exact approved plan, settled criteria, commit/base and impact evidence.
Spec conformance derives requirements from the plan every Build; canonical settled criteria outrank it when present. The divergence hunter reverse-checks the change for omissions and unrequested work. Other installed lenses assess usability, integrity and release safety at the approved depth.
Reviewers run code only in copies they create and discard, never by worktree-ing or repointing an existing checkout. Packet creation snapshots checkout identity; required checkout-integrity preflight checks origin, branch and stash. Stray worktree registrations are advisory because peers may legitimately add one.
Each `review record` carries honest `--code-execution none|discarded-copy|in-place`; this is self-reported, as the PR disclosure states. Use one `build-findings-batch.v1` file for `review record --findings-from-file` and `finding record --from-file`, keeping receipt IDs and dispositions consistent; malformed batches write nothing.

### Adjudicate findings
Accepting a concern is not accepting its remedy. Verify each finding and record accepted-fixed, accepted-tracked, partially-accepted, rejected with rationale, or escalated, separately stating whether it blocks this PR. Severity alone never blocks.
Before involving the operator, synthesize one recommended call and its tradeoff. Return only for a change to design, law, authority, agreed capability, required guardrail acknowledgement or another unresolved operator-only decision.

### Judge repair proportionately
After accepted repairs, measure reviewed-to-final divergence with `repair assess` and record one engineering judgment:
- `none`: direct verification suffices; another independent pass would be disproportionate.
- `scoped`: re-read affected lenses. With no `--lens`, use the lenses that raised blocking findings; explicit lenses override that default.
- `full`: re-read all applicable lenses when architecture, authority or broad behavior changed.
The same judgment applies after target-branch reconciliation. Diff size and touched surfaces inform, never choose. Packets identify `anchor..commit`, so a repair review reads the repair. Scoped/full packets require current candidate validation; each later fix gets a new proportional judgment. There is no automatic audit recursion.

A receipt states the range a lens read. Rebinding retains covering receipts byte-for-byte and names unread authored commits for the others. Derived-only regeneration invalidates no coverage and opens no round.
`none` is a terminal judgment and clears the repair packet; if it would discard uncovered receipts, it refuses unless `--accept-receipt-loss` is explicit. Scoped/full asks for the missing read instead. Do not turn this flag into routine bookkeeping.
A behavior-changing or large repair after lighter review suggests under-chosen depth: lean scoped/full when the defect and fix warrant it. Depth remains engineering judgment, not a size threshold.

Two or more cold lenses spend a counted panel round; none or one lens spend no counted budget. The convention for a low-yield single check is a minimal model. An already-spent round is never refunded.
Three counted rounds or six total rounds trigger consultation before exceeding the limit. Summarize what keeps failing and the proposed remedy, then record the operator's answer with `--guidance`; it reaches the PR body. The trajectory classifies each increment and highlights growing code/guarded-surface churn.
These bounds cap cost, not lens coverage. One design panel freezes the plan; a changed plan follows the explicit revision path, not another automatic panel.

### Preserve receipts across a proven clean target merge
An active branch may fetch and merge its target. Commit authored resolutions and regenerate derived outputs through `sync-artifacts`; validate the actual merged HEAD and push it before assessment:
```text
build_coordinator.py <identity flags> validate --plan <payload.json>
git push origin <build-branch>
build_coordinator.py <identity flags> repair assess --judgment none --rationale "<why direct verification suffices>"
```
The coordinator observes the exact draft head and verified default-target repository/ref/tip. It requires two parents with that target second and an actual merge tree matching `git merge-tree --write-tree` in an isolated object-only repository, excluding local configuration, attributes and replacement refs.
Only proven imported target ancestry and that exact automatic merge are exempt from unread work; local commits before/after still need coverage. Original receipts/read ranges remain unchanged. The PR records target tip, merge, automatic tree and validated HEAD, not a claim that combined behavior is unchanged.
A clean catch-up alone needs no receipt-loss flag or extra counted panel. Conflicts, extra merge edits, unrelated/octopus merges, missing objects, ambiguous targets or failed proof remain ordinary authored/unverified work; use the warranted scoped/full judgment.
Failed remote verification grants no new exemption: check the clean checkout, pushed draft identity and fetched target, then retry. Intentional rebases use [Build continuity](build-continuity.md). Re-import final CI proof after later changes.

See [Executable persona result contracts](../docs/result-contracts.md): dispatch binding, observed-report validation, substitution refusal, recovery and demo.
### Reviewer contract continuity

The Build carries the approved reviewer envelope. Packet refresh preserves compatible receipts and
findings; it does not rediscover a new panel from installed files. Use `review contract-preview` and
`review contract-apply` for an operator-decided per-lens retain/adopt transition. Only changed obligations
need supplemental review, while Git read-range coverage remains independently required. A renewal or
finding change invalidates the PR contract and preflight. Historical receipt recovery uses the explicit
`review historical-preview` / `review historical-apply` route described in
[Result contracts](../docs/result-contracts.md#historical-adoption-and-its-limits), including original
backup, packet and source proof. Never replace missing observed companion evidence with historical credit.


## Done when
The final head has green candidate and imported CI evidence, every finding has a disposition, and every divergence has its proportional judgment and required receipts.
Status reports submission-preflight and names [Build submission](build-submission.md).
