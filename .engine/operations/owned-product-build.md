---
title: Owned-product Build — deliver directly in the product's isolated checkout
---

## Purpose

Use this path when the Engine mechanic owns a product in a separate repository. It preserves the original
direct-owned-product route: product work is isolated from the mechanic, validated as that product, and sent
straight to the product's draft pull request. It is not the unowned-upstream contribution path and is never
reflexive—the mechanic does not treat its own repository as a separate owned product.

## Steps

1. Establish the durable sibling checkout specified by the mechanic's product configuration. Verify its git
   root, default branch, clean recoverable state, remote repository slug, and that it is not this repository.
   GitHub writes use the verified slug, never one inferred from a directory name.
2. Create one isolated worktree for the Build from the product's current target branch. Bind the coordinator
   inside that worktree to the product's open draft PR. Run every product command, test, validation, and git
   operation there; the coordinator remains repository-local.
3. Implement and review under the normal Build flow. Regenerate the product's own indexes explicitly and run
   the product's registered validation. The mechanic checkout then runs its local-reference scan across the
   outgoing product diff and PR prose so mechanic-only identifiers do not leak into the product.
4. Route the completed change directly to the owned product PR. Do not use the fork/upstream submission
   helper, and do not create an intermediary mechanic PR for product code. Verify the target slug again before
   every GitHub write. Mark ready only through the normal Build submission evidence and never merge.
5. After delivery, remove only the verified per-Build worktree. Keep the durable sibling checkout. Stale
   workspace cleanup is separate housekeeping and requires operator consent before anything recoverable is
   discarded.
6. If a delegated worker fails or returns partial work, inspect the actual checkout and commits, retain useful
   coherent work, repair integration, and re-dispatch or complete the missing portion. Never invent progress,
   receipts, or a successful worker result to advance the coordinator.

## Done when

The owned product has one truthful, validated draft-to-ready pull request from its isolated worktree; its
indexes and local-reference checks are current; all GitHub writes targeted the verified product slug; the
mechanic repository contains no product implementation; and cleanup removed only the verified Build worktree.
