---
title: Accepted automatic memory hooks
---

# Accepted automatic memory hooks

## What this covers

Automatic memory work can fire from any registered Git worktree, while canonical project memory is shared
across those worktrees. This page describes how the Engine keeps a stale or in-progress checkout from choosing
the implementation that mutates that shared state, how an accepted implementation is changed, and what an
operator sees when qualification fails.

## What you need to know

### Automatic execution

Claude and Codex register the same five memory-bearing entry points: boot, close, compaction, erasure
observation, and backup. Their provider-specific commands enter one shared launcher. The launcher admits only
that closed roster and starts the accepted dispatcher with isolated Python startup; it never selects the hook
script from the firing worktree as the implementation to run.

The dispatcher reads one activation record from the repository's Git common directory. That record binds the
repository, exact commit and tree, Engine release, reviewed source, and monotonically increasing epoch. The
exact tree is materialized into an owner-local cache and inventoried. Accepted code then reconstructs an
immutable execution context that binds one project, canonical memory store, recovery generation, provider,
run identity, operation, writer, target, effect, and cardinality before it grants one consumable capability.
Any mismatch is a no-mutation outcome.

This preserves shared recall: linked worktrees still read and update the same canonical project memory. The
worktrees do not receive separate memory stores, and memory is not disabled merely because more than one
worktree exists.

### Changing the accepted implementation

Activation is attended. It accepts only an exact commit GitHub proves was merged into the repository's live
default branch, or an exact commit named by GitHub's published release and exact tag ref, and advances the
epoch with compare-and-set. A candidate manifest cannot redefine the default branch, and a release object,
tag, and successful publisher run must converge on the selected commit.

Normal attended workflows bootstrap this state with `uv run --directory .engine --frozen -- python
tools/accepted_hook_dispatch.py ensure --root ..`. The command preserves any valid existing activation without
network access, even when the canonical checkout has advanced or an explicit rollback selected an older safe
commit. Only when activation is absent does `ensure` select the canonical checkout's local
GitHub-default-branch HEAD and require the independent merge proof. Advancing or rolling back an existing epoch
always uses the explicit attended activation/upgrade path. Automatic hooks and unattended Routines never call
`ensure` and cannot advance the epoch. If a fresh clone reports that activation is absent, update its canonical default branch cleanly,
retire or recreate any legacy worktree named by the topology diagnostic, then run Engine Start or `ensure` in
an attended session.

Before and during that advance, the Engine inventories every Git-registered worktree twice. Each worktree must
have the exact tracked, clean launcher generation whose per-file digests are reviewed by the topology
authority; marker strings or a clean commit alone are not qualification. A pre-fix, altered, dirty, missing, symlinked, unreadable,
ambiguous, duplicated, or concurrently changing topology refuses activation. The Engine does not rewrite or
delete those worktrees. Retire the legacy worktree or recreate it from the qualified generation, then retry the
attended activation. Activation writes only accepted metadata and materialization; it does not rewrite ledger,
index, cursor, sidecar, pointer, vault, or credential payloads.

Rollback follows the same rule: select a previously reviewed safe commit or published release through a new
compare-and-set activation. Never restore an old activation record by copying it over the current one, because
that would bypass the worktree census and epoch check.

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
