---
title: Recover an Engine issue submission without sending it twice
---

## Purpose

Engine issues use the helper and an explicitly activated journal retaining content, submission ID, send claim
and confirmed issue identity. Restarts and empty searches never authorize replay. Product scope uses neither
Engine markers nor this journal.

## Steps

### 1. Check publication and permissions

The journal lives on `refs/heads/codex/engine-issue-recovery`, an orphan data branch containing only
`.engine/issue-recovery/journal.json`. Its content has the repository's visibility and remains in Git history.
Never include secrets in intended issue content. Preview performs no network write and stores nothing.

The bound credential needs Issues write and Contents write. GitHub's Contents permission is repository-wide;
it cannot be confined to the journal branch. The adapter confines its writes to the fixed data ref and its
metadata tree. The nightly report job receives these permissions; the demonstration job remains read-only.
No new credential broker, GitHub App or Actions-write permission is installed.

The operator-owned `.engine/operator-issue-recovery.json` pins the repository's numeric ID and genesis commit.
It is preserved across upgrades and excluded from upstream contributions. A missing configuration, deleted
journal, invalid record, denied ref update or unavailable service holds new Engine creation. It never selects
an older raw-create fallback. Existing report updates/closures and preview do not require activation.

### 2. Activate explicitly

After deploying the code, use an authorized isolated Build/setup worktree and the existing credential:

```text
uv run --directory .engine --frozen -- python tools/issue_author.py recovery preview --repository OWNER/REPO
uv run --directory .engine --frozen -- python tools/issue_author.py recovery init --repository OWNER/REPO --confirm
```

Initialization creates metadata only, not an issue. Review the resulting operator configuration through the
project's normal PR process before scheduled runners consume it. This code Build does not activate a live
journal. Initialization refuses an existing ref or activation and never silently recreates either.
A lost creation response is recoverable only by verifying the exact root commit that this initialization wrote.
If initialization published the ref but local configuration could not be saved, preserve the published
identity and restore explicitly with `init --genesis VERIFIED_SHA --repository-id VERIFIED_NUMERIC_ID --confirm`.
Both values must come from inspected or backed-up activation evidence; restoration verifies the whole chain.

### 3. Inspect and recover

```text
uv run --directory .engine --frozen -- python tools/issue_author.py recovery list --repository OWNER/REPO
uv run --directory .engine --frozen -- python tools/issue_author.py recovery show --repository OWNER/REPO --record RECORD
uv run --directory .engine --frozen -- python tools/issue_author.py recovery reconcile --repository OWNER/REPO --record RECORD --expect-revision N --confirm
```

Use the record key and current revision printed by list/show. Reconciliation reads open and closed issues,
including every page, using the same submission marker. One verified match is adopted; zero, multiple,
incomplete or malformed matches remain held. An adopted closed issue is returned in that invocation, never
immediately replaced. A later distinct authoritative producer observation may create a new recorded generation
under its existing recurrence policy. Clearing a symptom alone does not prove an uncertain issue was never created.
Automatic passes recover uncertain submissions even without a new failure; unverified recovery stays visibly held.

An operator who has inspected a specific matching issue can use `adopt` with the same arguments plus
`--issue NUMBER`. Target, numeric identity, Engine scope and submission marker must agree. Adoption does not
alter the issue's body or human milestone. Legacy reports without a triage record need explicit triage repair;
ambiguous historical reports are not silently chosen. The journal cannot reconstruct forgotten pre-upgrade requests.

Only after inspecting GitHub and stopping the old writer may the operator deliberately supersede a held attempt:

```text
uv run --directory .engine --frozen -- python tools/issue_author.py recovery supersede --repository OWNER/REPO --record RECORD --expect-revision N --reason "what was inspected and why another attempt is authorized" --quiescent --confirm
```

This records authorization and residual duplicate risk. It does not itself POST an issue. A later submission
uses a new linked generation. There is no timeout, lease expiry or automatic reclamation of a send claim.

## Done when

The trusted repository has its pinned activation, and each submission is confirmed or explicitly held with
its existing identity. Recovery does not replay a consumed send. A supersede is recorded only after inspection
and quiescing the prior writer; the residual duplicate risk remains visible.

## Notes

### Failure and retention boundaries

The process that positively wins a new send claim receives a single-use, nonserializable permit. It consumes
that permit before the one issue POST. Lost claim responses cannot be converted into permits by reading back
the nonce. A crash after claiming but before sending therefore stays conservatively held. A successful issue
response followed by failed metadata confirmation also remains recoverable using the same identity.

Records are limited to 1 MiB and active snapshots to 10 MiB. Exceeding a limit refuses new publication without
evicting unresolved records. History verification is bounded at 10,000 commits and refuses beyond that bound.
Reads use the same credential for GraphQL history/blob batches; writes use non-force Git-data REST updates.
Each blob batch is at most 20 blobs and 10 MiB; verified process-local prefixes save repeated reads after a
fresh remote identity/ref check. Cold reads still grow with retained history; quota or service errors hold
creation, never reset the journal. There is no automatic pruning, compaction or erasure from Git history.
Missing/corrupt state requires restoration, never initialization over the old record. A process can detect a
rewind relative to a previously observed tip; a fresh process cannot prove that a hostile repository writer
has not replaced history with a valid prefix rooted at the pinned genesis. This is recovery discipline among
cooperating writers, not universal exactly-once issue creation or protection against arbitrary repository writers.

### Roll back safely

Before deploying a version that predates this protocol, stop automatic creators and remove their write
credentials or disable their reporting workflow. Quiesce local writers too. Older binaries do not understand
the journal and cannot be made safe merely by leaving a marker behind. Preserve the data ref and activation.
Restore a protocol-aware binary and verify the journal before re-enabling creators. Never use deletion of the
journal as a way to clear an error or resume an uncertain submission.

### Supported session entry points

| Surface | Coverage and evidence |
| --- | --- |
| Codex `exec_command` with `cmd`/`workdir` | Observed native tool shape; normalized hook behavior tested offline. Session cwd anchors trust. |
| Codex shell/local-shell/unified-exec envelopes | Simulated normalization fixtures. |
| Claude Bash `gh issue create` and `gh api` POST issue collections | Simulated hook fixtures, including separate commands and explicit repositories. |
| GitHub create-issue connectors with structured owner/repo/repository fields | Simulated connector fixtures. |
| Reads, comments, edits, closes, external unlabelled creates | Negative fixtures; not routed as new trusted-repository submissions. |
| Aliases, eval, opaque JavaScript, dynamically assembled commands, credentials used outside the hook | Outside coverage. Recognized dynamic targets produce an unclassified notice; arbitrary wrappers may evade recognition. |

Every recognized trusted-repository create requires explicit Engine/product scope, regardless of its label.
An explicit Engine label also routes external requests to the helper, which refuses an untrusted target.
Human GitHub UI creation remains outside this session gate: conformance opts in only through the Engine label.
No live hook or live GitHub filing certification is claimed by these offline fixtures. App integration remains StarshipSuperjam/engine-template#914.

Run the permanent offline regression demonstration (fake Git/Issues service, real normalization and helper):

```text
uv run --directory .engine --frozen -- python tools/issue_gate.py submission-demo --failure closed-before-recovery
uv run --directory .engine --frozen -- python tools/issue_gate.py submission-demo --failure recurrence
uv run --directory .engine --frozen -- python tools/issue_gate.py submission-demo --scope product --label none
uv run --directory .engine --frozen -- python tools/issue_gate.py submission-demo --expected-posts 99
```

The last command deliberately fails. Options also cover missing assessment, external target, explicit label,
lost claim response and ordinary success. The demonstration serializes only fake remote state and reconstructs
clients and stores; separate process tests cover loss of all Python process memory.

Recovery data pushes contain no workflow files. The two shipped push-triggered code workflows select `main`,
so the fixed `codex/engine-issue-recovery` ref does not start code CI even when written with a local PAT.
This relies on checked workflow filters and the enforced metadata-only tree, not GITHUB_TOKEN event suppression.
