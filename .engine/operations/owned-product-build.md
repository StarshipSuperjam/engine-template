---
title: Owned-product Build — deliver directly in the product's isolated checkout
---

## Purpose

Use this path when the Engine mechanic owns a product in a separate repository. It preserves the original
direct-owned-product route: product work is isolated from the mechanic, validated as that product, and sent
straight to the product's draft pull request. It is not the unowned-upstream contribution path and is never
reflexive—the mechanic does not treat its own repository as a separate owned product.

## Steps

1. Establish one durable product checkout beside the mechanic, never inside it. Record its path in
   `.engine/mechanic/product-checkout-path`; `ENGINE_PRODUCT_CHECKOUT` is a one-session override and takes
   precedence. The checkout is a shared anchor, not a workspace: verify its git root, default branch, real
   `github.com` origin slug, and that it is not this repository. It need not be clean—a peer may be using it—
   and no Build may switch its branch or touch its tree.
2. From the mechanic run `uv run --directory .engine -- python tools/mechanic_build.py worktree
   <short-slug>` — a plain slug names both the worktree directory and the `claude/<name>` branch; prefix the
   issue number only when one exists. It fails closed unless the anchor is the configured product, fetches the target, creates
   an isolated worktree under `.engine/mechanic/worktrees/`, and emits `ENGINE_PRODUCT_WORKTREE`,
   `ENGINE_PRODUCT_BASE=origin/<default>`, and the verified `GITHUB_REPOSITORY`. The harness's mechanic
   worktree is not the product worktree. Bind the product coordinator and draft PR inside the emitted
   worktree; run every product command, test, validation, and git operation there.
3. Implement and review under the normal Build flow. Regenerate the product's indexes explicitly and run its
   registered validation. From the mechanic—not the product—run `uv run --directory .engine -- python
   tools/local_references.py scan --ref "$ENGINE_PRODUCT_BASE" --checkout "$ENGINE_PRODUCT_WORKTREE"` so the
   mechanic's private vocabulary is checked against the exact remote base. A finding is shown for judgment;
   an unavailable vocabulary or scan is disclosed, never called clean.
4. Route the completed change directly to the owned product PR. Do not use the fork/upstream submission
   helper, and do not create an intermediary mechanic PR for product code. Verify the target slug again before
   every GitHub write. Mark ready only through the normal Build submission evidence and never merge.
5. After delivery, remove the verified worktree from outside it with `git -C <shared-checkout> worktree remove
   <path>`, then `worktree prune`. Never remove it while it has unpushed commits. Keep the durable checkout.
   Stale-workspace cleanup is separate, activity-aware housekeeping: recent git-admin activity is a possibly
   live peer session; for an idle worktree or sibling clone, check both unpushed work and whether its pull
   request already merged (a squash-merged branch otherwise looks unpushed forever), then remove only with
   operator consent.
6. If a delegated worker fails or returns partial work, inspect the actual checkout and commits, retain useful
   coherent work, repair integration, and re-dispatch or complete the missing portion. Never invent progress,
   receipts, or a successful worker result to advance the coordinator.

## Done when

The owned product has one truthful, validated draft-to-ready pull request from its isolated worktree; its
indexes and local-reference checks are current; all GitHub writes targeted the verified product slug; the
mechanic repository contains no product implementation; and cleanup removed only the verified Build worktree.

## Notes

Non-reflexivity governs what the mechanic runs on, not what it may build. It may build unmerged product code
in the isolated worktree, but the mechanic itself upgrades only to human-approved released product code.
