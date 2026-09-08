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

The mutable plan record owns a stable Build ID, a monotonically advancing lease generation and the canonical
snapshot address. Carry the ID and generation received from bind or authorized adoption into each mutation;
do not replace them with whatever an old locator happens to resolve to now. Every mutation also supplies the
current revision. The store compares all three under the plan and permanent snapshot locks.

For an intentional cold continuation in the recorded worktree:

```text
build_coordinator.py state where
build_coordinator.py state continue --plan <plan-id> --repository <owner/repo> --pr <number>
build_coordinator.py --expect-build-id <returned-id> --expect-generation <returned-generation> \
  --expect-revision <current-revision> checkpoint --plan <payload.json> --input <checkpoint.json>
```

An external `--state` selector contains only the plan ID, Build identity and canonical address. It cannot
recreate evidence after the plan folder is deleted. Full snapshots, retired archives and private reviewer
notes remain owner-only inside that folder.

### Cold handoff

Cold continuation is anchored on the sealed plan RECORD. `handoff export --output <file>` writes the Build's
evidence, redacted, to a file; `handoff restore --input <file>` reads it back and re-verifies the plan in the
library — same id, same sealed digest, same payload; gone, unsealed or changed, continuation is blocked rather
than guessed at. A Build whose executed plan was revised away from its seal cannot hand off cold at all: finish
it in the session that holds it, or re-plan.

The export carries its Build identity, generation, canonical address and revision. Restore compares its bounded
evidence to the surviving canonical snapshot, re-derives integration receipts, and preserves private notes
from that snapshot. A stale export, another worktree or a missing/retired canonical snapshot refuses. Recover
the original private snapshot from backup before restoring; a portable export cannot prove it is the newest
copy and cannot create another active Build. Legacy exports must be migrated and re-exported from their owner.

### Interrupted transitions and legacy evidence

Use `state where` first. Retry a preparing bind with the same inputs. Retry adoption with the original
predecessor ID, generation, revision, payload and successor; its journal preserves the destination even after
the predecessor snapshot moves to its archive. Successful adoption returns the same Build ID and the new
generation/address, preserves eligible progress, and retires the predecessor claim. An adoption preparation
cannot be completed as an ordinary bind or independently retired.

Explicit legacy migration is `state migrate --plan <plan-id> --source <old-snapshot>`. It requires an
unambiguous snapshot agreeing with the existing binding. Missing, conflicting or terminal legacy metadata
refuses with the source preserved. Migration keeps external sources and reports them; canonical legacy
evidence moves to `legacy-original.json` only after the new copy is durable. The old `builds/snapshot.json`
slot becomes a permanent directory barrier, and its old lock remains. Continue upgraded Builds with the
updated Engine: pre-upgrade file operations cannot replace that directory slot.

For a confirmed-stale claim, `state supersede --plan <plan-id> --reason <reason>` also requires the global
expected ID, generation and revision flags. An unwritten bind preparation retires at revision 0; otherwise
use the snapshot's actual revision. Retirement journals its reason, preserves a generation-specific archive,
then releases ownership; retry the same decision after interruption. Never remove either permanent lock.

Project Manager `abandon` and `retire` require the same expected tuple for a bound Build. `complete` additionally
takes `--completion-evidence <json>` containing `merged: true`, `build_id`, `generation`, `snapshot`,
`repository`, `pull_request` and `sealed_digest`; the CLI independently checks GitHub reports that PR merged.
The store matches the entire tuple under lock, including the canonical address, and repeated matching
completion is idempotent. This is the seam for future reconciliation; automatic merge reconciliation is separate.

Run and vary the disposable demonstration:

```text
uv run --directory .engine --frozen -- python tools/demo_build_resumes_after_a_kill.py \
  --interrupt retire-rename --contenders 4
```

It exercises the real transaction logic without touching the live library or GitHub. All selectable
interruptions and deliberately broken exclusivity/fencing checks have permanent regression coverage in
`test_build_state_store.py`. Fault injection models interruptions; it does not certify physical power-loss behavior.

### Compaction

Compaction mid-Build is survivable by design and needs no ceremony: every mutating verb re-verifies this session
against the durable snapshot and refuses on a mismatch whether or not a compaction was observed, and a
`compact` hook restores context on Claude and qualified Codex CLI hosts (see `codex-validation.md`).

## Done when

The resuming session holds the same plan id, sealed digest, and payload the library records; `status` derives
the phase from the snapshot and names the runbook to read now; and the next mutating verb verified this session
against the snapshot before anything changed.
