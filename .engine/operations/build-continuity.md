---
title: Build continuity — resume a Build across sessions, compaction, and cold handoff
---
## Purpose

Read when resuming after interruption or compaction. Follow [Build orchestration](build-orchestration.md).
The sealed plan record remains authority; its private snapshot holds evidence, never plan content.

## Steps

Run `state where`, then `state continue --plan <id> --repository <owner/repo> --pr <number>` in the recorded
worktree. Keep the returned Build ID and generation. Every mutation takes `--expect-build-id`,
`--expect-generation` and current `--expect-revision` before its verb; status never grants a new identity.
Pass the exact payload to checkpoints, review packets and submission. Missing or changed seals refuse.
Snapshots and archives stay owner-only in the plan folder; external `--state` files are minimal locators.

Retry preparing binds with identical inputs. Retry adoption with the original predecessor identity, revision,
payload and successor: its journal survives archival. Successful adoption returns the new generation/address.
An adoption preparation cannot be finished as a bind or retired independently. Never delete permanent locks.
`state migrate --plan <id> --source <old-snapshot>` accepts only unique, coherent legacy evidence. Ambiguity,
missing evidence or terminal metadata refuses. External sources remain and are reported; canonical originals
become `legacy-original.json`, with a permanent directory barrier at the old slot. Use the updated Engine.
For confirmed stale ownership, `state supersede --plan <id> --reason <reason>` requires the expected tuple;
an unwritten bind preparation uses revision 0. Retirement archives before releasing ownership; retry exactly.
Bound Project Manager `abandon`/`retire` require that tuple too. `complete --completion-evidence <json>` also
requires merged=true, Build ID, generation, canonical snapshot, repository, PR and seal; CLI checks GitHub.
Matching completion is idempotent. Automatic merge reconciliation is separate.

`handoff export --output <file>` redacts private notes; `handoff restore --input <file>` verifies the seal and
matches the surviving canonical identity, revision and worktree, preserving its private evidence. Missing or
retired snapshots cannot be recreated from an export: recover the original from backup. Migrate legacy first.
Compaction re-verifies each mutation; the compact hook restores context on Claude and qualified Codex CLI hosts
(see codex-validation.md). Re-ground before continuing.
Run the disposable real-transaction demo with selectable interruptions and competitors:
```text
uv run --directory .engine --frozen -- python tools/demo_build_resumes_after_a_kill.py --interrupt retire-rename --contenders 4
```
Permanent tests vary interruptions and break fencing/exclusivity. This models interruption, not power loss.

## Done when

The verified plan and caller-held identity match; status names the next runbook and mutation verifies again.
