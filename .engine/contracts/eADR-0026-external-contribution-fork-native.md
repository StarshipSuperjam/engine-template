---
id: eADR-0026
title: External contribution is fork-native, with the engine-mechanic as its non-reflexive special case
status: accepted
date: 2026-06-29
---

## Decision

Contributing to a product repository the operator does not own is a first-class operating arrangement, realized fork-native: the operator forks the upstream (and so owns the fork), the Engine is brownfield-installed into that fork as an ordinary same-repo deployment, and product changes reach the un-owned upstream as a cross-fork pull request carrying only product-path changes. No new grammar is added — the existing delivery, topology, and lifecycle machinery is reused. What changes is only where the merge gate lives and the duty to keep the Engine off the contribution. The engine-mechanic — a deployed instance whose product is the engine repository itself — is the special case of this arrangement: a separate-workspace variant, non-reflexive (the dependency runs mechanic → product, never the reverse), trusting an independently-held human review gate on the product repo rather than any self-vouching.

## Significance

This establishes that an un-owned product is reached by owning a fork of it, not by relocating the Engine's substrate or inventing a cross-repo mode. The full cognitive substrate keeps its committed home in the fork exactly as a same-repo deployment; later work must not re-home state or knowledge to make cross-repo work. The unbypassable merge wall moves from the operator's own merge to the upstream's: for a governed upstream that is its own checks plus maintainer review (a real human gate whose human is simply not the operator); for an ungoverned one, the honest position is that the operator's fork-side checks are the only real gate — an unreviewed merge must never be dressed as a trust gate. The Engine stays off the contribution by posture (an engine-clean-by-origin product branch plus a local cleanliness nudge), backstopped by the upstream's review, never claimed as a mechanical guarantee. The mechanic must remain non-reflexive and may upgrade only to human-approved releases of its own output — that is the only thing that keeps self-improvement honest, and it is a human-review-grade rule, not a machine proof.

## Rationale

The north star is to cold-start work on any project, and treating the Engine as a contributor rather than a part of the product is the purest reading of that: a contributor knows the product, the product does not know its contributor, and the substrate is the contributor's own — so it belongs in the operator's fork, not in a repo the operator cannot own. Building this in from the start avoids a future system refactor: a capability deferred to a v2 becomes one. Reusing the already-settled machinery keeps the contagious core minimal while still carrying the one genuinely new obligation honestly — that the hard gate is now someone else's, which forces plain narration that submitted is not accepted and that an ungoverned upstream has no real acceptance wall. The mechanic is folded in as a special case rather than a bespoke path because it runs the same machinery; its only distinguishing constraint is that the building instance never self-upgrades to its own unapproved output, which is what dissolves the trusting-trust and reflexive-upgrade hazards.

## Anti-choice

The strongest rejected alternative was to make the separate-workspace arrangement — Engine in one repo, product checked out elsewhere — the general realization for all external contribution, with fork-native as a mere variant. It lost because it would re-open the settled committed homes of state and knowledge for more churn and a weaker degradation story, all to generalize a shape only the mechanic actually needs. Fork-native keeps the substrate committed in the fork unchanged and degrades to a working owned fork on any upstream failure; the separate-workspace form is reserved for the mechanic alone, where fork-native would degenerate to installing the Engine into a repo that already is the Engine. Two weaker alternatives were also rejected: deferring cross-repo to a post-v1 capability (it would become the exact future refactor this design exists to prevent), and making it a core capability (an own-product deployment never contributes to a repo it does not own, so the machinery is a genuine opt-in extension, not core).

## Status

accepted
