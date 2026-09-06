---
title: Build implementation — implement and reground
---
## Purpose

Read when the coordinator reports `implementation`: the plan is bound and approved, and the work is to make the
change cohesive, commit it through checkpoints, and bring the tree to a validated candidate.
The surrounding flow is [Build orchestration](build-orchestration.md).

## Steps

### Choose the strategy

Choose an implementation strategy proportionate to the work: orchestrator-inline for small or coupled work,
isolated workers for cleanly separable work when context pressure justifies them, or the durable routine path
for unattended bulk work. Delegation returns work product to the orchestrator, which remains the single
writer and judges cohesion. Reconnaissance is not implementation: send a wide recall or impact sweep to
`engine-grounding-scout` and a broad file search to a native `Explore`. Both are cheap, neither can spawn,
and the judgment stays here.

### Follow the path the work takes

Routine follows [Routine entry](routine-entry.md): the sealed plan in the local library supplies ordered work
items and the Issue supplies the authorization; the snapshot, handoff, and git record completed commits and
`N of M` progress, counted from `work integrate`, the only completion path. Owned product work
follows [Owned-product Build](owned-product-build.md), and work for a repository the operator does not own
follows [external contribution submission](external-contribution-submit.md). A v2 DAG Build's node lifecycle
follows [Build work dispatch](build-work-dispatch.md). If a worker fails, inspect what returned, repair
cohesion, and re-dispatch or complete the missing work without inventing workflow state.

### Integrate nodes and recover

`work integrate` completes a v2 node only against an Engine-computed integration receipt (base descent, HEAD
reachability, Engine-selected identity, an attributable range, and no path outside the node's admissible set);
a failed check refuses with a named remedy, recorded durably as an integration-class node failure with no
lifecycle advance, so the node stays incomplete rather than the Build wedging. Recovering a wrongly refused
integration needs no plan revision: read the refusal, re-integrate a corrected commit, and a clean
integration clears the failure. Only a genuinely too-narrow declared scope needs the operator — the session
cannot self-grant an exemption (the plan is digest-checked), so the operator revises the sealed plan.

### Checkpoint before each commit

Before each commit, run `checkpoint` with the exact plan and a short JSON note containing objective, current
work, named `work_item`, assumptions and accepted risks, non-goals, planned scope, remaining verification, and one judgment:
`aligned`, `plan_revision_required`, or `operator_decision_required`. The coordinator adds changed paths.
Unexpected paths are highlighted for judgment, not automatically forbidden. A missing or mismatched plan
blocks commit recording. A non-aligned judgment requires resolution before submission.

### Defer, verify, iterate

Write genuine deferrals at the code site with the `ENGINE-TODO` marker grammar, owned by `engine_todo.py`; it requires no Issue merely to record one. Run `engine_todo.py list` in touched areas and disposition covered work. Verify specifications, harness capability, and delegated findings against
first-hand authority — including a repository-state claim you would file as an engine Issue: verify it at the repo's freshly-fetched default branch (`checkout_health.claim_at_fresh_head`), and separately search that repo's open pull requests and existing Issues for the same resolution (`gh pr list`/`gh issue list --search` — a merge already shows in the default branch). Record the verified `owner/repo@sha`; if the fresh head cannot be read or the checkout is the wrong repo, report it unverified, never auto-closing an existing Issue. Iterate with focused
tests; `status` reports unordered activities and cannot know which engineering activity is best.

While building, use focused tests — and run those through `engine-validation-runner`, which works in a disposable
copy and returns a digest instead of thousands of log lines. It is the wrong tool for the two classes
[Build validation and review](build-validation-and-review.md) earns: both bind evidence to the live tree, which a
scout confined to a throwaway copy cannot produce.

### Reconcile and prepare the derived surfaces

Reconcile the target branch before final validation; if it advanced, resolve by the substance of the change
(the original base is disclosed evidence, not immutable authority). Prepare the derived surfaces last with
`build_coordinator.py sync-artifacts` — it regenerates every registered generated surface (`.engine/knowledge/graph.json` and the rest) in dependency order from the reconciled tree, resolving a derived conflict by regeneration, never a side-pick, and validation refuses until they are current.
This reconcile is no longer the sole guarantee: the merge floor requires freshness, so GitHub backstops it at the merge boundary.

## Done when

Every node is integrated, each commit carries an `aligned` checkpoint, the target branch is reconciled and the
derived surfaces are current, and the tree is ready for candidate validation in
[Build validation and review](build-validation-and-review.md).
