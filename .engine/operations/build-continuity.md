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

## Done when

The verified plan and caller-held identity match; status names the next runbook and mutation verifies again.
