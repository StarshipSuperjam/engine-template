---
title: Session coordination — how active worker sessions warn and hand off to each other, advisory-only
---

## Purpose

How concurrent Engine worker sessions coordinate while work is still active — breaking-change warnings,
overlap warnings, prerequisite handoffs, bounded status, integration and revalidation notices — without the
operator becoming the message bus, and without any message ever carrying authority. The governing law
(eADR-0043): **live communication may cut latency and rework, but correctness, authority, consent, and
future-session truth never depend on a message being delivered.** Enter this runbook to understand how a
notice is emitted, carried, read, and acted on, and why every step is advisory.

## Steps

1. **A notice is emitted at a bounded lifecycle point, never on a timer.** The Engine emits a typed
   coordination notice only at a semantically meaningful moment — an integration slot opening or releasing,
   an authored-conflict block, a build declaring or completing its change domain, a prerequisite reaching a
   durable state. Emission is best-effort: it can never block, fail, or change the behaviour of the step it
   rides. On a single-session repository (no peer candidate) nothing is written at all.
2. **The payload is durable; the doorbell is live.** The notice is stored as a typed, digest-verified block
   in ONE Engine-maintained comment on the pull request or issue it concerns (`coordination_board`) — a
   best-effort durable cache, quietly updated in place, never a stream of timeline comments. A peer's live
   messaging carries only a one-line poke pointing at that comment; a held, refused, dropped, forged, or
   stale message changes nothing.
3. **A receiver acts only by re-verifying canonical state.** Every notice names a `verify` action — re-check
   the queue, the base, the plan, the overlap, or the pull request. A receiving session treats the notice
   (and any poke) as an untrusted prompt to run that check against Git, GitHub, the specification, or the
   integration coordinator. A message that a review passed, a PR merged, or "you're next" is never trusted;
   the authoritative surface is. A notice can never satisfy consent, a guardrail-ack, a review, merge
   eligibility, or freshness.
4. **The doorbell is passive by default.** On Claude, a poke lands in a peer's inbox it reads when it next
   looks (`SendMessage`). Other runtimes are first-class through the durable lane alone: an idle Codex peer
   receives the durable notice only — the Engine never starts a turn on it (`turn/start` is never a
   doorbell); an already-active Codex peer may receive a bounded `turn/steer`. The Engine drives no peer
   runtime — the doorbell is a skill the sending session's own agent performs.
5. **A session reads the board at bounded points, never by polling.** Session start relays the unseen count
   from local state; the integration-queue status/prepare steps read the
   live board for the current pull request. There is no background poll.

## Done when

An active session can discover a peer's consequential change, hand off a prerequisite, and surface overlap or
status early — and the same repository stays correct when live messaging is unavailable or fails, because the
durable state and the serialized-integration path remain the source of truth.

## Notes

**Advisory-only is enforced mechanically.** The coordination code reaches GitHub through one comment
transport plus read-only reads and nothing else — no merge, no label, no commit status, no issue-body edit —
checked fail-closed by `engine/check/coordination-confinement`. The overlap warning prompts a human decision;
it is never an admission gate or a lock (serialization stays the queue's job, freshness the ruleset's).

**Auditability without a corpus.** The pull-request comment carries the notices with the platform's own
actor and timestamp; local read/unread cursors and a bounded measurement ring live in a gitignored cache. No
peer conversation becomes a permanent knowledge store — a future session reconstructs truth from GitHub
alone.

**The live cross-session demonstration is local-only.** The durable lane and its negative controls
(delivery-independence, digest-forged-skip, coordination-absent) run in CI; the real two-session doorbell
needs live runtimes CI does not have, so it is run locally once and recorded in the pull request's
Demonstration section — the same honest ceiling the live queue and Codex arms carry.
