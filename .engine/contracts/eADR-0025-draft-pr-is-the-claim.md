---
id: eADR-0025
title: The draft PR is the claim
status: accepted
date: 2026-06-29
---

## Decision

_Amended 2026-08-16: Issues are intake; a Build's plan is session-held, durable only through promotion (eADR-0041)._

_Amended 2026-08-17: the draft-PR-is-the-claim lifecycle is the single-Build view of the multi-writer integration lifecycle fixed by eADR-0042. "Green" authorizes merge only as an integration candidate re-proven against the current canonical tree, not as a candidate green against its starting base; the operator's merge remains the sole unbypassable wall across both views._

Build work is carried by native git/GitHub records and never by an invented artifact of its own. The draft pull request is the claim and the change surface: it holds what has been built — the opening claim, the integrated commits, the human merge gate, and the contract narrative — and it is the one record every Build has. A GitHub Issue is work intake: a problem or need ready to be taken up for planning — never the plan itself, and never created merely because a Build exists. When an originating Issue exists it remains the intent record the pull request closes at merge — nothing more.

The Build's plan is a build artifact, not a GitHub record by default: it is authored in the session, lives with that session, and is non-durable until a Build must genuinely continue cold — another session, or unattended work — when the exact approved plan is promoted to a suitable writable Issue as one bounded machine block: transport for recovery, changing nothing about what an Issue is. A "build Issue" in this canon means exactly that — an intake Issue carrying a promoted copy of a Build's plan for cold continuation. How the plan is approved, bound to the Build, and promoted proportionately is owned by the coordinator's behavioral contract (eADR-0041).

There is no separate claim artifact, no reserved slot number, and no close ritual; a build is done when its PR is submitted, and the only unbypassable wall is the operator's merge of the protected branch.

## Significance

This fixes where build state lives. Durable build state stays in native legs that git and GitHub already keep — git itself, the pull request (including the bounded handoff block its body may carry), and, when cold continuation demanded it, the promoted plan block riding a suitable Issue — never a durable engine-private ledger; the coordinator's snapshot is machine-local working evidence that carries no authority and may be absent — never a durable leg, never plan authority (eADR-0041). A same-session plan is deliberately not durable: that is why cold or unattended continuation requires promotion first, and why an unreachable GitHub bounds only cold resume — there is no durable plan to read, so the session safely does not proceed, while same-session work continues from the harness that holds its plan.

Later work must respect that an Issue is intake and never a plan container by identity — a session holding an approved plan opens its draft PR and builds, filing no Issue for the work it is doing now; promotion is the only path plan content takes to an Issue, and only for cold continuation; and nothing manufactures a close ceremony around the merge. Anything that reviews a build attaches its judgment to the PR contract (eADR-0021), never to a new artifact; the merge is the sole wall and every nudge before it is honestly a nudge.

## Rationale

A close mechanism that invents its own claim object — a reserved subject, a slot to allocate, a close-shape to police — spends real effort guarding a ritual instead of shipping the change, and that friction compounds into a spiral where closing work costs more than doing it. The native records already encode every state a build passes through: open, committed, submitted, merged. The two questions a build answers split cleanly without a second bookkeeping surface: what has been built is the PR and its commits; what is not yet built is the plan, which lives with the build and becomes a GitHub record only when recovery genuinely needs one. That keeps each record single-purpose — the PR carries the change, an Issue carries a problem awaiting work — lets a cold session reconstruct where a build stands from git plus the promoted plan when one was needed, and keeps the operator's merge the one decision that matters. The cost paid is honest degradation: when GitHub is unreachable a durable plan is unreadable, so cold continuation fails safe rather than guessing, while same-session work continues.

## Anti-choice

The rejected alternative was a dedicated claim artifact — a reserved-subject commit or allocated slot that announces and tracks the build as its own object, with a structured close ritual to retire it. It lost because that machinery is precisely the friction it claims to manage: every reserved subject needs an allocator, every close-shape needs an allowlist to police, and the apparatus grows faster than the work it wraps, turning closing a change into its own project. Rejected with it is the softer ceremony of a tracking Issue minted for every Build: it duplicates the record the PR already is, teaches that an Issue is a plan, and buys durable recovery only for builds that never needed it — promotion covers the rare Build that does. The native records carry the same states with none of the bookkeeping, so an invented artifact buys nothing the PR — and, for cold continuation, a promoted plan block — does not already give.

## Status

accepted
