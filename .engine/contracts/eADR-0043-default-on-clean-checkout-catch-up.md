---
id: eADR-0043
title: A clean default operator checkout catches up at session start by default
status: accepted
date: 2026-08-20
---

## Decision

At each Engine SessionStart source (`startup`, `resume`, and `clear`), the Engine automatically
fast-forwards the operator's project folder only when all of these facts hold at mutation time:

- it is already on the freshly verified remote default branch;
- its assessed HEAD has a strict fast-forward path to the exact remote target;
- it has no uncommitted work, stash, off-branch commit, or paused Git operation; and
- the remote identity, branch, default ref, HEAD, and target still match the fresh assessment.

The mechanism uses the existing named-ref compare-and-swap, index-locked materialization, rollback, and
postcondition verification. A competing session that completed the same update is normalized to current. A
changed target, a late edit, unavailable remote facts, or any other failed predicate leaves the operator
checkout's branch, HEAD, index, and working tree untouched.

This is a deliberately narrow exception to boot's usual read-only orientation posture. It changes the local
operator checkout only. It never pushes, merges, changes GitHub, changes an isolated session worktree, or
bypasses the protected-branch merge gate.

The new committed operator preference `.engine/operator-checkout.json` is preserved across upgrades and
outside Engine overlay ownership. Its absence is the shipped enabled default; exact JSON boolean `false` opts
out. A present malformed or unreadable preference fails closed, leaves automatic catch-up disabled, and tells
the operator to repair it through `/engine-setup`. Setting changes are atomic and travel in a reviewed pull
request. Opting out retains fresh drift detection and the existing consented **bring it up to date** path.

The successful update is disclosed once in the same boot invocation and points to `/engine-setup` as the
opt-out. An already-current checkout is silent. Unsafe and unavailable states retain a plain reason and the
manual recovery offer.

## Significance

This decision deliberately supersedes the consent-only default established in
StarshipSuperjam/engine-template#619: every operator-checkout update formerly required consent. That work
correctly established the fresh-target, identity, losslessness, atomic advancement, and rollback safeguards;
this decision keeps them and changes only the default authority for the strictly clean,
already-default-branch case. The broader repair authorities from that work remain consent-only.

## Rationale

The operator's project folder and session worktrees have separate checked-out branches. A merged pull request
therefore does not itself advance the folder an operator opens. Session start is the cross-platform moment
where the Engine already sees a fresh checkout-health snapshot, without introducing a watcher, webhook,
daemon, self-hosted runner, or exact-on-merge dependency. Restricting automation to a no-branch-switch clean
fast-forward lets the Engine remove ordinary drift while preserving all work in ambiguous or exceptional states.

## Anti-choice

Rejected: automatic branch switching, rescue-branch creation, dirty-but-subsumed reconciliation, strand
repair, divergence repair, or any automatic conflict resolution. Those paths carry wider authority and remain
visible, consented recovery. Rejected: a machine-local unreviewed toggle, because an upgrade could lose it or
make its provenance unclear. Rejected: a background watcher or GitHub-event mechanism, because session-start
synchronization is the agreed boundary. Rejected: treating malformed preferences as absent/default-on, because
uncertain operator intent must disable mutation.

## Status

accepted
