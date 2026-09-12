---
title: Reader health and verified recovery
---

## What this covers

The scoped-evidence reader and boot assembly report health locally. Telemetry's existing SessionStart
inbox pass promotes their failures and checks recovery. These readers run on your machine; GitHub Actions
does not run them. No additional schedule is required.

## What you need to know

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
feature never advances activation or changes trust on your behalf. Because boot consumes changing signals,
session authority and task bindings, its cached success never suffices by itself: reconciliation requires
fresh assembly with the identical trusted producer and runtime. A different running producer, failed fresh
assembly or exhausted budget leaves recovery unverified.

To run the normal recovery check explicitly:

```sh
uv run --directory .engine --frozen -- python tools/telemetry.py reconcile-readers
```

An explicit check exits nonzero when recovery is unverified; the background SessionStart pass remains
fail-open. A checkout command cannot substitute for the accepted runtime when verifying boot recovery;
restart through the accepted hooks when that execution context is required.

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

For an older incident, an operator may explicitly confirm attribution after inspecting its original issue
evidence and the exact original reader's retained local crash diagnostic. The preview supports a single
original observation with matching first/last timestamps and a known scoped-reader or boot-assembly source.
It refuses aggregated incidents, unrelated hook sources, missing diagnostics and ambiguous evidence.
The operator must establish that this clone and reader account for the incident's original scope; a matching
event or title alone is insufficient. Issues without that proof remain manual.
Enrollment remains available after that reader recovers; retained original attribution is still required.
Confirmation invalidates earlier success before updating the issue, so the reader must verify again after
enrollment before closure. An older verification already in progress cannot satisfy that requirement.

```sh
uv run --directory .engine --frozen -- python tools/telemetry.py enroll-reader-incident ISSUE_NUMBER READER_ID scoped-reader --observed-at ORIGINAL_TIMESTAMP > /tmp/reader-enrollment.json
uv run --directory .engine --frozen -- python tools/telemetry.py enroll-reader-incident ISSUE_NUMBER READER_ID scoped-reader --observed-at ORIGINAL_TIMESTAMP --confirm --preview /tmp/reader-enrollment.json --reason 'Operator verified the original incident belongs to this clone and reader'
```

Confirmation rechecks the original issue, diagnostic and local observation generation. It retains a
content-safe attribution receipt and human issue text, changes the source to the reader's scoped owner,
and leaves the issue open. Normal positive recovery checks must still succeed before closure. An uncertain
write requires inspection before retrying. This operation never guesses which legacy issues to enroll.

The offline regression demonstration uses disposable Git repositories, registered worktrees, the production
reader and reconciliation code, and a fake GitHub transport. It cannot change live issues:

```sh
PYTHONPATH=tools uv run --directory .engine --frozen -- python -m unittest test_telemetry.ReaderHealthRecovery test_plan_program.SharedReaderDiagnosis test_hooks.TestAcceptedAutomaticHookDispatch.test_health_identity_follows_real_dispatch_and_refuses_checkout_or_stale_activation test_hooks.TestAcceptedAutomaticHookDispatch.test_real_accepted_boot_assembly_records_failure_then_verified_success
```

The scenarios cover actual historical scoped/program schemas against current writer records, a failing
historical-schema reader, a healthy sibling, updating the affected fixture reader, positive recovery, two
independent clones, changed or missing inputs, retirement, legacy attribution, outages and concurrent recurrence.
