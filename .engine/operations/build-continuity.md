---
title: Build continuity — resume a Build across sessions, compaction, and cold handoff
---
## Purpose

Resume from the authoritative sealed plan and its evidence-only snapshot. Follow [Build orchestration](build-orchestration.md).

## Steps

Run `state where`, then `state continue --plan <id> --repository <owner/repo> --pr <number>` in the recorded
worktree. Keep the returned Build ID and generation. Every mutation takes `--expect-build-id`,
`--expect-generation` and current `--expect-revision` before its verb; status never grants a new identity.
Pass the exact payload to checkpoints, reviews and submission; missing/changed seals refuse. Snapshots and
archives stay owner-only in the plan folder; external `--state` files are minimal locators.

Retry binds with identical inputs; adoption retries use the original predecessor identity, revision, payload
and successor. Its journal survives archival; success returns the new generation/address.
An adoption preparation cannot be finished as a bind or retired independently. Never delete permanent locks.
Pause affected old-code sessions and wait for running Engine commands to exit; update their worktrees before
`state migrate --plan <id> --source <old-snapshot> --legacy-clients-stopped`, then resume the same unfinished tasks.
Migration locks and rechecks source evidence; ambiguity, changed/missing evidence or terminal metadata refuses.
External originals remain reported evidence only; canonical originals become `legacy-original.json` and the old
slot becomes a permanent directory barrier. Protection from old file operations starts after cutover.
For confirmed stale ownership, `state supersede --plan <id> --reason <reason>` requires the expected tuple;
an unwritten bind preparation uses revision 0. Retirement archives before releasing ownership; retry exactly.
Bound Project Manager `abandon`/`retire` require that tuple too. `complete --completion-evidence <json>` also
requires merged=true, Build ID, generation, canonical snapshot, repository, PR and seal; CLI checks GitHub.
Matching completion is idempotent. Automatic merge reconciliation is separate.

`handoff export --output <new-file-outside-library>` redacts private notes; `handoff restore --input <file>` verifies the seal and
matches the surviving canonical identity, revision and worktree, preserving its private evidence. Missing or
retired snapshots cannot be recreated from an export: recover the original from backup. Migrate legacy first.
Compaction re-verifies each mutation; the compact hook restores context on Claude and qualified Codex CLI hosts
(see codex-validation.md). Re-ground before continuing. Disposable transaction demo:
```text
uv run --directory .engine --frozen -- python tools/demo_build_resumes_after_a_kill.py --interrupt retire-rename --contenders 4
```
Permanent tests vary interruptions and break fencing/exclusivity. This models interruption, not power loss.

### Catch up or deliberately rewrite history

An active Build can catch up by fetching and merging its recorded target, then regenerating derived files
through `sync-artifacts` after committing any authored resolution. The live protect-main policy permits
merge, squash and rebase; it has no `required_linear_history` rule. Do not manufacture a rebase requirement.
Honor an explicit operator request to rebase. A merge preserves history; an intentional rewrite uses the
recovery procedure below instead. All coordinator mutations carry the caller-held Build ID, generation and
current revision described above, plus the exact bound payload where shown.

For an **unreviewed** Build, finish or abandon outstanding node claims, commit and push the current head,
then prepare *before* rewriting:

```text
build_coordinator.py <identity flags> reconcile --plan <payload.json> --prepare
git rebase <prepared-target-tip>
build_coordinator.py <identity flags> reconcile --plan <payload.json>
```

Preparation fetches and pins the target, records the source HEAD, plan and ownership tuple, and retains source
objects under `refs/engine/build-recovery/`. Rebase onto that exact pinned target; do not refetch and substitute
a later target mid-preparation. Resolve any conflicts and finish the rebase before applying recovery. Keep the
PR's pre-rewrite source head or push the recovered head as instructed by the identity check. The apply checks
completed rebase provenance and the original contribution before advancing the same Build's history anchor.

Clean recovery preserves the original work ledger and receipts. Divergent unreviewed recovery preserves the
resolved work and archives the original evidence, but marks affected nodes and their dependent closure as
needing verification. It creates no review receipts. Re-run the appropriate checks and, in dependency order,
record each original attempt at the recovered HEAD:

```text
build_coordinator.py <identity flags> work integrate --item <node> --attempt <original-attempt> --plan <payload.json> --commit <recovered-head> --recovery --verification-input "<fresh check and result>"
```

The old integration receipt remains historical evidence; a separate recovery verification records what was
checked now. No fresh claim or invented completion substitutes for this transition. Complete reverification
before another rewrite, then earn current-head validation and the normally approved deliverable review.
`reconcile --plan <payload.json> --cancel-preparation` cancels only on the original, unchanged history;
retained source objects remain available. An interrupted apply can be retried against matching canonical
identity/revision. Missing preparation, retained objects or unambiguous rebase provenance refuses: recover the
original evidence/history, never manually move the Build anchor or replace its plan.

After **reviewed** history is rewritten, the existing `reconcile --plan <payload.json>` path compares the
branch contribution. An identical contribution re-anchors the review bindings; a differing or unmeasurable
contribution requires the existing proportional repair judgment against the new base. Original receipts are
not fabricated or restamped.

A recovered Build can export and restore a handoff only against the surviving canonical snapshot. Push the
recovered PR head first, using an explicit lease on the recorded remote source when replacing rewritten history:

```text
git push --force-with-lease=refs/heads/<build-branch>:<recorded-source-head> origin HEAD:refs/heads/<build-branch>
build_coordinator.py <identity flags> handoff export --output <new-file-outside-library>
build_coordinator.py <identity flags> handoff restore --input <export-file>
```

If that lease refuses, inspect the competing remote change; do not broaden the force push. Restoration re-derives the original integration receipts from retained objects and
verifies identity-bound recovery provenance; a portable export cannot create that exception by itself. Keep
source objects and canonical private evidence available through completion.

## Done when

The verified plan and caller-held identity match; status names the next runbook and mutation verifies again.
