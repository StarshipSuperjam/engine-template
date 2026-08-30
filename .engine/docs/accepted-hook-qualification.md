---
title: Accepted automatic memory hooks
---

# Accepted automatic memory hooks

## What this covers

Automatic memory work can fire from any registered Git worktree, while canonical project memory is shared
across those worktrees. This page describes how the Engine keeps a stale or in-progress checkout from choosing
the implementation that AUTHORS that shared state — and, just as importantly, what stays working when it
cannot: reading, recall, health, diagnostics, capture into the transcript, and entering Build.

The distinction between "never authors canonical memory" and "touches nothing" is the whole design. The second
reading, shipped once, removed memory from every session on the machine.

## What you need to know

### Automatic execution

Claude and Codex register the same five memory-bearing entry points: boot, close, compaction, erasure
observation, and backup. Their provider-specific commands enter one shared launcher. The launcher admits only
that closed roster and starts the accepted dispatcher with isolated Python startup; it never selects the hook
script from the firing worktree as the implementation to run.

The dispatcher reads one activation record from the repository's Git common directory. When there is none it
does NOT decline to start: it runs the tool from the live checkout with no execution context, which is exactly
the signal the tier below reads. That record binds the
repository, exact commit and tree, Engine release, reviewed source, and monotonically increasing epoch. The
exact tree is materialized into an owner-local cache and inventoried. Accepted code then reconstructs an
immutable execution context that binds one project, canonical memory store, recovery generation, provider,
run identity, operation, writer, target, effect, and cardinality before it grants one consumable capability.
Any mismatch is a no-mutation outcome.

This preserves shared recall: linked worktrees still read and update the same canonical project memory. The
worktrees do not receive separate memory stores, and memory is not disabled merely because more than one
worktree exists.

### Qualifying happens by itself

Qualification is **ambient**. Every session start attempts it, with no command for the operator to run and no
prompt to answer. Three states, one entry point:

* **absent** — bootstrap to the canonical checkout's current GitHub default-branch tip;
* **stale** — the default branch has moved ahead of the activated commit, so advance to it;
* **current** — verify the recorded object and keep it.

An advance must be FORWARD: the new commit has to descend from the activated one, so a force-push, a rollback,
or a swapped branch cannot walk qualification backwards. Every advance still needs the same proof a first
activation does — a pull request the operator merged, whose merge commit IS this commit, on GitHub's own
default branch — so a direct push can never qualify; it simply stalls advancement until the next merged pull
request. The epoch moves forward only, by compare-and-set, and every first qualification and every advance is
disclosed in the session's own status lines, because "the code allowed to write your memory just changed" is
not something to do quietly.

The whole attempt shares a two-second wall-clock budget, runs with stdin closed and prompts disabled, and
gives up rather than delaying a session. **A failed advance never costs a working activation**: no network, no
GitHub CLI, a rolled-back branch — each leaves the machine qualified where it already was, and says why.

`uv run --directory .engine --frozen -- python tools/accepted_hook_dispatch.py ensure --root .. --ambient`
reports the current state and what, if anything, is holding it back. It is a diagnostic, not a step anyone has
to remember.

**Rollback** is a new compare-and-set activation onto a previously reviewed safe commit or published release,
and a pinned published release is never auto-advanced — it is an explicit operator choice. Never restore an old
activation record by copying it over the current one: that bypasses the epoch check.

### Worktrees this does not cover

An earlier version of this mechanism REFUSED to activate while any registered worktree carried a pre-fix,
dirty, or unreadable launcher generation. That protected nothing. A pre-fix worktree runs its own pre-fix
wiring whether or not this machine's activation advances, so refusing only stripped protection from the
sessions that could have had it — and on a machine with accumulated worktrees, that was every session.

So it is a **disclosure** now. `/engine-status` reports how many registered worktrees are not covered, names
them, and gives the command that clears one (`git worktree remove <path>`, or `git worktree prune`).
Activation proceeds regardless. The mechanism that would genuinely cover those worktrees is a store-side
cutover — locking pre-fix code out of canonical state rather than declining to protect the rest — and that is
deliberately deferred to its own change.

### What an unqualified session can still do

This is the correction at the heart of the current design. The rule is that candidate code never **authors**
canonical memory. It is not "candidate code touches nothing" — an earlier implementation read it that way, and
took memory reads, automatic capture, health reporting, crash diagnostics and the ability to enter Build down
with the thing it was protecting.

Effects are tiered, as data, over the registry that already describes them
(`mutation_contract.degraded_disposition`):

| Tier | Targets | What an unqualified session does |
| --- | --- | --- |
| Allowed | diagnostics and status records, tracked findings, session markers, ephemeral staging, the keyword and semantic indexes | Proceeds, returning an unqualified receipt. Each is regenerable or costless to lose, and the engine must be able to report that it is degraded. |
| Refused | the ledger, its metadata and generation stamp, the capture cursor, restore journals, the backup pointer and remote vault, erasure proposals, exports, the project repository | Refuses without mutating, with a reply that says why, that nothing changed, and what makes it stick. |

A destructive effect stays refused even on an allowed target, unless it is clearing a diagnostic. A nested
writer is tiered on its own entry, so an allowed diagnostic is never a door to the ledger beneath it.

Three operator verbs — pin, set aside, restore — refuse in the unqualified window and say so plainly. Setting
a note aside names the consequence explicitly: the note stays recallable until qualification converges, and if
the intent was erasure, that chain has not started.

**Entering Build is not part of any of this.** The session stance marker is per-session temporary state, not
persistent memory, and it left the mutation registry entirely; its integrity is carried by a hardened write
(owner-only, refuses a planted symlink, lands atomically). Governing it as a persistent mutation is what
locked the operator out of their own project.

### Capture loses nothing

Decisions get made in ordinary sessions, not only during Builds, so a design where an unqualified session
cannot record anything would remove a pillar of the system rather than protect it.

It does not, because **the transcript is the durable record**. An unqualified session writes nothing AND
leaves its capture cursor exactly where it found it — the cursor advance and the ledger append are one
transaction under one lock, so a refused append cannot leave a tail marked as captured. The tail stays in the
harness's own transcript, still marked uncaptured.

At the next qualified session start, `memory/drain.py` collects it: it walks this project's transcripts, finds
the cursors that are behind, and captures those tails through the ordinary capture path — so chunking,
scrubbing, id-minting, tagging, sequencing and the cursor advance all happen in reviewed code. Nothing an
unqualified session produced is trusted as input; the input is the transcript. Drained records carry a
`session-start-drain` tag so a reader can tell a note recovered afterwards from one filed live.

Two bounds on where it looks: only under capture's own allowed roots, and only transcript directories this
project's path names — a harness keeps every project's transcripts under one home, and a drain that swept the
whole home would file another project's conversations into this project's memory.

The residual risk, accepted and disclosed: a transcript cleaned up by the harness before qualification ever
converged is a loss, and one that leaves nothing behind to detect it by. The defence is the backlog — how many
sessions are waiting and how old the oldest is — reported long before retention could reach them.

### Rewriting the record needs a person

Compaction is the one effect that rewrites canonical memory, and in the incident that prompted all of this a
background lifecycle hook classified 99.9% of live records as retired. The code ran, the state was consistent,
the effect was registered. What was missing was attendance.

So a record-destroying effect whose declared recovery story IS a person — an operator merge, or a snapshot
taken first — runs only from an attended invocation, **even in a fully qualified session**. PreCompact
therefore proceeds without mutating and says so in one sentence. Appends are deliberately outside this rule
(capture must keep working unattended), as are journal-driven restore recovery and index rebuilds, each for a
reason a test pins.

Recovery readiness is checked before any destructive pass on the real store: where a backup vault is
configured, a successful push has to happen first, and a failed push stops the compaction rather than
proceeding without a net. A machine whose operator declined a vault is not blocked over a backup they chose
not to have.

One setup-era presentation marker is deliberately outside this normal activation path because the project's
first reviewed activation cannot exist until its first setup pull request lands. Before deleting any first-run
asset, `instantiator.retire` preflights one source-verified, target-bound, one-use attended exception for
`.engine/boot/.cache/first-run-landing.json`. That path is checkout-local, gitignored, and presentation-only;
the issuer requires the actual tracked `instantiator.py` source, keeps consumption in an opaque registry, and
opens every marker path component without following symlinks. Losing the marker can only suppress the single
post-landing “Setup is now complete” message. It cannot authorize shared
memory, recovery, repository, remote, or arbitrary lifecycle writes. This narrow boundary is operator-approved;
the accepted automatic cleanup that later consumes the marker still uses the normal qualified capability path.

The context-free unit-test adapter requires a live frame matching a regular `test_*.py` file under the Engine
tools tree whose byte snapshot equals its tracked `HEAD` blob. Dirty, staged-only, untracked, fabricated,
borrowed-path, and same-named sources receive no test authority. This is harness identity, not accepted-code
identity: candidate tests must exercise newly changed writers before activation, and existing suites bind those
writes to disposable fixtures. Mechanically removing that remaining tracked-test exception requires a separate
harness migration; it is not part of the production mutation boundary claimed here.

### Candidate maintenance

Candidate code has no automatic path to canonical memory. An attended candidate command re-enters accepted
code, which creates a new absent disposable directory directly under the operating-system temporary directory.
Only an operation whose complete registered transitive boundary is attended-capable may run there. The receipt
binds accepted code, candidate code and dirtiness, immutable context, private target and store, operation,
provider, run/task identities, before/after inventories, outputs, and exit status. Canonical and remote targets,
aliases, pre-existing directories, automatic-only operations, and unknown future authorization fields refuse.

### Degraded health and recovery

A qualification refusal never becomes a canonical-memory repair. The host action continues with the launcher's
normal non-blocking exit, including PreCompact. Outside canonical memory, under an atomic Git-common lock, the
Engine records a closed bounded health document: skipped-effect count, first and latest failure, last recovery,
freshness, fixed guidance, and a non-sensitive receipt. It stores hashes instead of session identities and
never stores raw hook stderr, paths, ledger content, credentials, or vault material. Repeated guidance is
rate-limited; every skipped effect is still counted. A later qualified success records recovery, and the status
dashboard reports the current degraded or recovered state.

### Enforcement limit

This is operational provenance, not protection from malicious code running as the same operating-system user.
It closes the supported Engine call graph against accidental candidate or stale-code mutation and detects
ordinary topology races. Same-user code can still rewrite launchers, Git common metadata, caches, or process
state, and a candidate-controlled shell runs before the accepted boundary. Stronger enforcement requires a
separately permissioned mediator or operating-system isolation; this mechanism does not claim either.
