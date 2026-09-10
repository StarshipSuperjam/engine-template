---
title: Build continuity — resume a Build across sessions, compaction, and cold handoff
---
## Purpose
Resume the canonical sealed plan and evidence. Follow [Build orchestration](build-orchestration.md).

## Steps
Run `state where` and `state continue --plan <id> --repository <owner/repo> --pr <number>` in the recorded worktree.
Every mutation supplies the returned `--expect-build-id`, `--expect-generation` and current `--expect-revision` before its verb.
Use the exact payload. Snapshots/archives stay private in the plan folder; external `--state` files are locators. Status grants no identity.
Matching retries retain original admission/consent; changed preparing inputs refuse. Restore them or explicitly retire the preparation.
Adoption retries retain predecessor identity/revision, payload and successor; their journal survives archival. Never delete permanent locks.
An adoption preparation cannot finish as bind or retire independently. Active continuation gains no new entry certificate.

Pause old-code sessions, wait for Engine commands to exit and update their worktrees before migration. Ambiguous evidence refuses.
Migration retains canonical `legacy-original.json` behind a permanent directory barrier; external originals remain evidence only.
Retirement archives before releasing ownership; retry exactly (revision 0 for unwritten preparation). Bound Project Manager retirement requires the tuple too.
```text
build_coordinator.py <identity flags> state migrate --plan <id> --source <old-snapshot> --legacy-clients-stopped
build_coordinator.py <identity flags> state supersede --plan <id> --reason <confirmed-stale-ownership>
```
An active branch may merge its fetched target, then `sync-artifacts` after committing authored resolutions. Protect-main permits merge/squash/rebase, with no `required_linear_history` rule. Honor explicit rebase requests.
For an **unreviewed rewrite**, finish/abandon node claims, commit and push, then prepare before rebasing onto the pinned tip:
```text
build_coordinator.py <identity flags> reconcile --plan <payload.json> --prepare
git rebase <prepared-target-tip>
build_coordinator.py <identity flags> reconcile --plan <payload.json>
```
Preparation retains source objects under `refs/engine/build-recovery/` and pins identity, plan, revision and target. Finish conflicts before apply.
Apply verifies completed-rebase provenance and contribution; the PR must retain source or recovered HEAD. Missing/ambiguous proof refuses; recover evidence, never hand-edit anchors or replace plans.
Clean recovery preserves the ledger. Divergence archives original receipts and invalidates affected nodes/dependents without inventing review evidence.
Re-run checks and reverify original attempts at recovered HEAD in dependency order; reconcile and `status` print each exact attempt/commit and command. Finish before another rewrite, validation and approved review:
```text
build_coordinator.py <identity flags> work integrate --item <node> --attempt <original-attempt> --plan <payload.json> --commit <recovered-head> --recovery --verification-input "<fresh check and result>"
```
This adds separate verification, not replacement receipts or claims. `reconcile --cancel-preparation --plan <payload.json>` requires unchanged original history; retained objects remain. Interrupted apply retries keep canonical identity/revision.
For **reviewed rewrites**, ordinary reconcile re-anchors identical contribution; differing/unmeasurable contribution returns to proportional repair against the new base. Receipts are never fabricated or restamped.

Push recovered history with an exact source lease before handoff. If refused, inspect the remote change; never broaden the force push.
```text
git push --force-with-lease=refs/heads/<build-branch>:<recorded-source-head> origin HEAD:refs/heads/<build-branch>
build_coordinator.py <identity flags> handoff export --output <new-file-outside-library>
build_coordinator.py <identity flags> handoff restore --input <export-file>
```
Export redacts notes. Restore matches the surviving canonical identity/revision/worktree/seal, preserving private provenance and re-deriving original receipts from retained objects. Keep both through completion.
An export cannot recreate missing/retired state: recover the original from backup; migrate legacy first. Complete requires merged=true, identity, snapshot, repository, PR and seal in `--completion-evidence`; the CLI verifies GitHub. Matching completion is idempotent; automatic reconciliation is separate.
Compaction re-verifies mutations; re-ground before continuing. Hooks help on Claude/qualified Codex CLI hosts (codex-validation.md). `demo_build_resumes_after_a_kill.py --interrupt retire-rename --contenders 4` models interruption, not power loss.

## Done when
The seal and caller-held identity match; status names the next runbook and mutation verifies again.
