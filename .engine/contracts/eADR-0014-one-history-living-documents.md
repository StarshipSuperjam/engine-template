---
id: eADR-0014
title: One history; living documents everywhere else
status: accepted
date: 2026-06-29
---

## Decision

Change history lives in exactly one place: the structured pull-request body, which the pull request carries as the durable record of what happened and why. Two surfaces are governed by exception. Decision records are append-only — a standing decision is never edited; it is superseded by a newer record, and a rejected option survives as a named anti-choice inside the prevailing record rather than as its own state. Every other document — specifications, guidance, catalogs, narrative — is rewritten in place to its current truth and carries no inline change history: no "previously," no version banners, no diff-against-the-past.

## Significance

This locks in that any document other than a decision record reads as authored-complete-today, and that the single trustworthy account of how state reached its current shape is the pull-request body — below the decision-record threshold, narrative is recorded there or is simply done. Later work must respect three things: no second history store may be invented (a bespoke log, a changelog file, a per-doc revision section) — narrative routes to the PR body or to a decision record, never to a third place; decision records may only grow and be superseded, never edited in place; and the decision-record threshold (eADR-0029) governs what is heavy enough to earn its own record versus what stays below the bar. The append-only carve-out is what lets a decision record truthfully cite a document that a later rewrite has since changed.

## Rationale

An operator who does not read code weighs change on its written record, so the record must be singular and trustworthy. Two failure modes are in tension. If history is scattered — inline "used to" notes, parallel changelogs, per-document revision logs — no document is reliably current and the operator must reconcile contradictory accounts to know what is true now. If history is erased everywhere, the chain of why is lost. The resolution splits the two needs onto two substrates: living documents stay current by being rewritten in place and holding zero history, while the why is preserved in exactly one durable channel. Decision records are append-only because a decision is a commitment others built on; editing it would silently rewrite the past those commitments were made against, whereas superseding it keeps the chain auditable. The pull-request body is the one history because the platform already carries it as the durable artifact of every change — reusing it avoids standing up a store that would itself need maintaining and could drift from reality.

## Anti-choice

The strongest rejected alternative was to keep a dedicated narrative store — a changelog surface, separate from the pull request — as the home for below-threshold session history. It lost because it duplicates what the pull request already carries: every change arrives as a pull request whose body is the natural, durable, structured record, so a parallel store is a second history that must be kept in sync and inevitably diverges from the one the platform already maintains. It also reintroduces exactly the scatter this law exists to prevent — two places an operator must consult to reconstruct what happened. Folding narrative into the pull-request body keeps history singular and removes a surface that earned its keep only by restating what the pull request already says.

## Status

accepted
