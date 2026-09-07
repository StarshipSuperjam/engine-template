---
title: Build continuity — resume a Build across sessions, compaction, and cold handoff
---
## Purpose

Read at one moment rather than one phase: when a Build resumes instead of starting — a new session picks up an
open Build, the context was compacted mid-Build, or the work continues cold in another session. It says where
the Build's evidence lives and how a continuation re-verifies the plan before it acts.
The surrounding flow is [Build orchestration](build-orchestration.md).

## Steps

### The durable snapshot

The Build's own snapshot is durable and lives beside that sealed plan, so a killed Build resumes with its
evidence intact — one atomically replaced, lock-protected document of current evidence, carrying no authority,
found from the worktree or named outright with `--state <path>`. It keeps the plan's id and digests, never the
plan's content; `status` derives the phase from it, and every checkpoint, review packet and submission preview
receives the payload again and refuses a mismatch.

### Cold handoff

Cold continuation is anchored on the sealed plan RECORD. `handoff export --output <file>` writes the Build's
evidence, redacted, to a file; `handoff restore --input <file>` reads it back and re-verifies the plan in the
library — same id, same sealed digest, same payload; gone, unsealed or changed, continuation is blocked rather
than guessed at. A Build whose executed plan was revised away from its seal cannot hand off cold at all: finish
it in the session that holds it, or re-plan.

### Compaction

Compaction mid-Build is survivable by design and needs no ceremony: every mutating verb re-verifies this session
against the durable snapshot and refuses on a mismatch whether or not a compaction was observed, and a
`compact`-matcher hook re-grounds the fresh context (Claude only — see the provider-exception ledger).

## Done when

The resuming session holds the same plan id, sealed digest, and payload the library records; `status` derives
the phase from the snapshot and names the runbook to read now; and the next mutating verb verified this session
against the snapshot before anything changed.
