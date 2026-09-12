---
title: Reader health and verified recovery
---

The scoped-evidence reader and boot assembly report health locally. Telemetry's existing SessionStart
inbox pass promotes their failures and checks recovery. These readers run on your machine; GitHub Actions
does not run them. No additional schedule is required.

An alert belongs to one local clone and one producer. All worktrees registered with that clone share an
opaque scope, including worktrees created outside the usual Codex or Claude directories. Independent clones
have different scopes. Public alerts contain no local paths or shared plan contents. Older alerts without
this attribution require manual investigation; a matching title or hook event is insufficient.

Recovery requires a successful execution recorded by each affected reader. A healthy sibling cannot clear
another reader's failure. Successful evidence expires after one hour and is invalidated by changes to its
inputs, code, interpreter or dependency metadata. Missing, corrupt, incomplete or changing evidence remains
unverified. Deleting evidence or removing a worktree does not establish recovery.

For incompatible shared records, update the affected worktree through your normal workflow and restart its
session. The reader still refuses unsupported evidence. Its compatibility diagnostic only uses a locally
available, known descendant schema that strictly validates the entire record; it does not fetch a schema,
rewrite evidence, or grant execution or review credit. Broader shared-schema migration remains separate.

Boot additionally requires successful execution from the currently accepted snapshot. A newer checkout
with an older activation is insufficient. Follow the existing accepted-activation recovery procedure; this
feature never advances activation or changes trust on your behalf.

To run the normal recovery check explicitly:

```sh
uv run --directory .engine --frozen -- python tools/telemetry.py reconcile-readers
```

The recovery pass has a ten-second total process deadline, including local reads and remote calls. Local
observation locks wait at most 50 milliseconds and never span network I/O. Contention queues bounded durable
observations. A delayed success cannot overwrite a failure; failed persistence is explicitly reported.
Offline, timed-out and unconfirmed remote writes remain unverified for the next pass. Closure is counted
only after GitHub readback and another local evidence check. GitHub has no atomic transaction with local
observations: an observed recurrence is reopened when possible, or raised again on a later pass.

An operator can retire a recorded reader after deregistering its worktree. Inspect the local
`.engine/telemetry/.cache/reader-health/state.json` in the canonical checkout for its opaque reader ID and
producer. Preview the exact decision, then confirm it with a reason after the operator approves it:

```sh
uv run --directory .engine --frozen -- python tools/telemetry.py retire-reader READER_ID scoped-reader > /tmp/reader-retirement.json
uv run --directory .engine --frozen -- python tools/telemetry.py retire-reader READER_ID scoped-reader --confirm --preview /tmp/reader-retirement.json --reason 'Operator retired this worktree'
```

The confirmation must match the current scope and generation. Registered readers and stale confirmations
are refused. A durable tombstone retains the reason and observation generation; re-registration or a new
failure supersedes the retirement. These commands never remove a worktree or accept plan evidence.

The offline regression demonstration uses disposable Git repositories, registered worktrees, the production
reader and reconciliation code, and a fake GitHub transport. It cannot change live issues:

```sh
PYTHONPATH=tools uv run --directory .engine --frozen -- python -m unittest test_telemetry.ReaderHealthRecovery test_plan_program.SharedReaderDiagnosis test_hooks.TestAcceptedAutomaticHookDispatch.test_health_identity_follows_real_dispatch_and_refuses_checkout_or_stale_activation test_hooks.TestAcceptedAutomaticHookDispatch.test_real_accepted_boot_assembly_records_failure_then_verified_success
```

The scenarios cover a failed reader, a healthy sibling, positive recovery, two independent clones, changed
or missing inputs, explicit retirement, remote outages, uncertain closure and concurrent recurrence.
