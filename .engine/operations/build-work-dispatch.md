---
title: Build work dispatch — driving a v2 DAG Build's nodes
---

## Purpose

A v2 Build plan is a small dependency graph of work items rather than a flat ordered list. This procedure drives
one node through its life — claiming work, attaching a worker, recording a result, integrating it — and reads what
the coordinator refuses and why; the orchestrator remains the single writer to the PR branch, and the operator
owns approval and merge.

## Steps

### See what is ready

`status --plan <plan> --json` (or the human render) shows, for a v2 Build, a `work` section: the graph-ready
nodes, the ones claimable right now, worker slots in use against the plan's `max_concurrency`, and each node's
derived state — `blocked`, `ready`, `claimed`, `returned`, `failed`, `recovery_required`, or `complete` — with its
reason. Readiness is derived from the graph and the recorded evidence every time, never a stored status you can
edit; a serial plan exposes one claimable node at a time, a conditional plan up to its approved limit.

### Ask what the scheduler would do, before spending a claim

`work frontier --plan <plan>` (add `--json` for the machine shape) is a read-only projection of the admission
decision — no snapshot mutation, no GitHub call — so you can ask what the scheduler would choose, and why it
passed something over, without spending a claim. It keeps three questions apart: **ready** (the graph says the
dependencies are integrated), **admitted** (the scheduler would hand the node out now, in the printed order), and
**deferred** (considered in this pass and passed over, with a typed reason).

**Eligibility is not selection.** A deferred node may still be perfectly claimable by name: the frontier reports
what the scheduler would CHOOSE, not the limit of what you may claim. `work claim` remains the authority: it
re-derives the frontier under the state lock and refuses an out-of-frontier claim there. The four deferral kinds,
each recorded per node:

| Kind | What it means |
| --- | --- |
| `dependency` | A predecessor is not integrated yet. The graph is holding it, not the scheduler. |
| `held-resource` | An exclusive resource or declared path is held by a node already running. |
| `selected-node-conflict` | It conflicts with another node admitted in this same pass, not with a running one. |
| `capacity` | Nothing is wrong with it; every worker slot was already taken. |

**Admission order is critical-path descending** (longest remaining chain first, lexical tie-break), so two runs of
one graph admit alike; plan array order is never priority.

### Preview and claim a node

`work packet --item <id> --provider claude|codex --plan <plan>` previews the bounded packet a worker would
receive — that node's objective, paths, output contract, base commit, and resolved route (the per-provider model
and effort from the bindings); no sibling nodes, no parent conversation.

`work claim --item <id> --provider <p> --plan <plan> --worktree <path>` atomically admits and acquires the node —
one locked step checks it is claimable (dependencies integrated, a free slot, no path or named-resource conflict
with a node that already holds them, the serial or concurrency limit not reached), records the claim, and emits the
definitive packet. `work attach --item <id> --attempt <id> --worker-ref <ref>` then records an observable worker
reference on the claim when the runtime can give one.

### Record and integrate the result

`work result --item <id> --attempt <id> --plan <plan> --input <result.json>` binds a worker's result to its
claim. The result must report the base it built from and, when it returns work, every evidence kind its output
contract requires; a stale attempt, a wrong base, or missing contract evidence is refused. A returned result
frees the worker slot but keeps the node's resources reserved.

`work integrate --item <id> --attempt <id> --commit <sha> --verification-input <summary>` is how a node reaches
`complete`: the orchestrator inspected the returned artifact and applied it on the single PR branch, and the
named commit is there. Integration records the commit and its focused verification, releases the node's
resources, and mirrors the commit into the Build's progress; no node completes without both.

### Handle a failure or recovery

A worker failure or a rejected artifact leaves the node `failed`, awaiting your disposition — no automatic
retries, timers, or claim expiry. Choose one explicitly:

- `work reject --item <id> --attempt <id> --class <class> --reason <text>` rejects a returned artifact and releases
  its resources.
- `work retry --item <id> --strategy redispatch|integrator-inline --reason <text>` reopens the node for a fresh
  attempt (a new attempt id, an incremented attempt count on the next claim), or hands it to inline work.
- `work abandon --item <id> --attempt <id> --reason <text>` gives up the node's current line of work and frees
  its resources.

A Build restored from a cold handoff with a claim that never returned derives that node `recovery_required`:
inspect its worktree and the provider's state, then supply a matching result or abandon and retry. A claim never
expires on its own; an unreachable worker is never marked failed from elapsed time alone.

### A dispatch that cannot be honored fails closed

When a route cannot be honored the coordinator does not silently degrade to inline — it FAILS CLOSED, recording a
blocked `dispatch` attempt (persisted, awaiting disposition) and refusing the claim, so a missing capability is a
visible gap, not a quiet demotion to the senior session (StarshipSuperjam/engine-template#1138). The decision keys
on whether worker packs are declared:

- **Undeclared** `implementation_classes` (no worker packs): nothing to fail closed on; the class resolves
  inline exactly as before.
- **Declared but incomplete** — the class/provider entry is missing or lacks model/effort: the claim BLOCKS,
  and the failure message names both the gap and the escape.
- **External transport** (an ACP executor) — refused default-on and unreachable from any ordinary `work claim`; where
  the seam is reached deliberately, a real eligibility consult over explicit qualification records governs it, and no
  eligible production executor is itself blocked.

**Recovery** is the operator's explicit choice: complete or remove the class/provider entry in
`model-bindings.json`'s `implementation_classes` and re-claim, or take the deliberate escape, `work retry --item
<id> --strategy integrator-inline --reason <why>`, honored ahead of any block on the next claim. An unattended
(Routine) Build stops at the block the same way — no timer or fallback advances it.

### A plan carrying a v1 payload

There is no converter, by decision: a v1 payload now exists only inside a SEALED plan, and a migration would
invalidate the seal that makes it bindable. `plan bind` refuses it and names the path — re-author the work through
the Project Manager and seal that; the stored v1 plan stays readable.

## Done when

Every node the plan requires is `complete` (an integration commit on the PR branch plus recorded focused
verification), no node is left `failed` or `recovery_required` without a disposition, and the Build has passed
candidate validation, imported engine-ci proof, deliverable review, and preflight. Only the operator merges.

## Notes

Routing is resolved from one binding source and rendered for both providers; the coordinator never compensates for a
pack it cannot honor with a stronger or costlier worker. The surrounding flow is [Build
orchestration](build-orchestration.md).
