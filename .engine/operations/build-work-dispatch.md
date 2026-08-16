---
title: Build work dispatch — driving a v2 DAG Build's nodes
---

## Purpose

A v2 Build plan is a small dependency graph of work items rather than a flat ordered list. This is the
operator-facing procedure for driving one node through its life: previewing and claiming work, attaching a
worker, recording a result, and integrating it — and for reading what the coordinator refuses and why. The
mechanics live in the coordinator and its tests; this doc is how you use them. The orchestrator remains the
single writer to the PR branch, and the operator still owns approval and merge.

## Steps

### See what is ready

`status --plan <plan> --json` (or the human render) shows, for a v2 Build, a `work` section: the graph-ready
nodes, the ones claimable right now, how many worker slots are in use against the plan's `max_concurrency`, and
each node's derived state — `blocked`, `ready`, `claimed`, `returned`, `failed`, `recovery_required`, or
`complete` — with its reason. Readiness is derived from the graph and the recorded evidence every time; nothing
is a stored status you can edit. A serial plan exposes one claimable node at a time; a conditional plan exposes
up to its approved limit.

### Preview and claim a node

`work packet --item <id> --provider claude|codex --plan <plan>` previews the bounded packet a worker would
receive: just that node's objective, paths, output contract, base commit, and its resolved route (the explicit
per-provider model and effort from the bindings). It carries no sibling nodes and no parent conversation.

`work claim --item <id> --provider <p> --plan <plan> --worktree <path>` atomically admits and acquires the node
— it checks in one locked step that the node is claimable (dependencies integrated, a free slot, no resource
conflict) and records the claim, then emits the definitive packet. A claim is refused when the node is blocked,
when the serial or concurrency limit is reached, or when its paths or named resources conflict with a node that
already holds them.

`work attach --item <id> --attempt <id> --worker-ref <ref>` records an observable worker reference on the claim
when the runtime can give one; if it cannot, the claim simply carries none.

### Record and integrate the result

`work result --item <id> --attempt <id> --plan <plan> --input <result.json>` binds a worker's result to its
claim. The result must report the base it built from and, when it returns work, every evidence kind its output
contract requires; a stale attempt, a wrong base, or missing contract evidence is refused. A returned result
frees the worker slot but keeps the node's resources reserved.

`work integrate --item <id> --attempt <id> --commit <sha> --verification-input <summary>` is how a node reaches
`complete`: the orchestrator has inspected the returned artifact, applied it on the single PR branch, and the
named commit is on that branch. Integration records the commit and its focused verification, releases the
node's resources, and mirrors the commit into the Build's progress. A node never completes without an
integration commit and recorded verification.

### Handle a failure or recovery

A worker failure or a rejected artifact leaves the node `failed`, awaiting your disposition — there are no
automatic retries, timers, or claim expiry. Choose one explicitly:

- `work reject --item <id> --attempt <id> --class <class> --reason <text>` rejects a returned artifact and
  releases its resources.
- `work retry --item <id> --strategy redispatch|integrator-inline --reason <text>` reopens the node for a fresh
  attempt (a new attempt id and an incremented attempt count on the next claim), or hands it to inline work.
- `work abandon --item <id> --attempt <id> --reason <text>` gives up the node's current line of work and
  releases its resources.

If a Build is restored from a cold handoff with a claim that never returned a result, that node derives
`recovery_required`: inspect its worktree and the provider's state, then supply a matching result or abandon and
retry. A claim never expires on its own, and a timed-out or unreachable worker is never marked failed from
elapsed time alone.

### Migrate a v1 plan

`plan migrate-v1 --input <v1.json>` rewrites an old v1 plan as an equivalent v2 linear chain. The rewrite
changes the plan, so it needs renewed approval before it is bound.

## Done when

Every node the plan requires is `complete` — each with an integration commit on the PR branch and recorded
focused verification — no node is left `failed` or `recovery_required` without a disposition, and the Build has
passed the full validation, deliverable review, and preflight the runbook requires. Only the operator merges.

## Notes

Routing is resolved from one binding source and rendered for both providers; when a route cannot be honored, the
node falls back to integrator-inline execution and never to a stronger or more expensive worker. See
[Build orchestration](build-orchestration.md) for the surrounding Build flow.
