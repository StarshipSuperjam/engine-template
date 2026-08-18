---
id: eADR-0042
title: The Engine is multi-writer; integration is validated, not inherited
status: accepted
date: 2026-08-17
---

## Decision

The Engine is a **multi-writer system whenever concurrent sessions or worktrees exist, regardless of how
many humans contribute.** One operator running two sessions, or one session plus one unattended Build, is
already multi-writer. The canonical tree — the protected branch — is the single serialization point: every
writer reaches it through the same lifecycle and the same integration proof. Solo identity (eADR-0021)
governs *who may click merge*; it does not make the system single-writer.

Work moves through five named states:

    worktree → candidate PR → reviewed PR → integration candidate → merged

  - **worktree** — session-held work; the plan is non-durable until promoted (eADR-0025, eADR-0041).
  - **candidate PR** — the draft PR is the claim (eADR-0025). It carries the change and its *candidate
    validation*: proof the change is green **against the base it started from**.
  - **reviewed PR** — a reviewed candidate. In **team** identity an approval surviving the last push marks the
    candidate reviewed for ordering; the binding code-owner requirement is enforced by GitHub's own
    `require_code_owner_review` at the merge gate (eADR-0021), not by this recognition. In **solo** identity
    there is no distinct reviewer, so "reviewed" is
    **operator-acknowledged readiness**, not code review (eADR-0021: the solo merge is informed consent, not
    review) — the state must never be presented to the operator as though an independent review gate passed.
  - **integration candidate** — the head brought up to date with the **then-current canonical tree plus all
    work already ahead of it**, carrying *integration validation*: fresh proof against that moved tree, not
    the starting base.
  - **merged** — the operator's merge; the sole unbypassable wall (eADR-0025, eADR-0021).

**Candidate validation and integration validation are distinct proofs, and neither substitutes for the
other.** A PR green against its starting base is a valid candidate; it is **not** a valid integration
candidate until it is re-proven against the current canonical tree and the work merged ahead of it. "Green"
authorizes merge only when it is green *as an integration candidate*; a stale green is false consent
(eADR-0021, freshness, reversed in place 2026-08-16).

Derived artifacts are **regenerated at integration, never hand-merged.** When two integration candidates both
touch derived-committed state, the second regenerates the derived tree from the reconciled source and
re-proves it; a derived-artifact collision is never surfaced to a human as a merge conflict. A collision in
**authored** source, by contrast, is refused for auto-reconciliation — both branches are left intact for a
human decision. The derived-state substrate (`derived_state.py`) is the single mechanism that makes derived
regeneration hand-free; it owns the set, so a new derived artifact reaches every integration boundary from
one registration.

**Serialized integration does not merge.** A provider-independent integration coordinator may order reviewed
candidates and admit one integration candidate at a time — via a native merge queue where the provider offers
one, or an Engine-controlled serialized fallback where it does not — but it prepares, proves, and orders; it
never performs the merge. That guarantee does **not** rest on the session-level merge-action hook (which sees
only the session's own tool calls, not a tool's subprocess internals): it rests on the coordinator carrying
**no merge path of its own** (asserted by its own no-merge test) and on the protected-branch ruleset, which
is what actually refuses a merge — including a stale-green one. Serialization automation may advance the
*queue*; it may never convert "ready" into "merged" by its own hand.

## Significance

This fixes the concurrency model the Engine was already living without a contract for. Later work must respect
that a green candidate is not a green integration candidate; that derived collisions regenerate while authored
collisions refuse; that cross-PR serialization is a first-class contract rather than an emergent property of a
single Build's execution DAG (eADR-0041); and that no ordering automation widens the set of things that merge
without the operator's governing policy.

## Rationale

Concurrency is intrinsic to the Engine the moment it can drive more than one session or worktree at once, so a
"solo repo means single writer" assumption was never safe — it was merely usually true. Inheriting a
candidate's earlier green as if it still holds is exactly the false consent the freshness floor (eADR-0021)
exists to prevent, lifted from one branch to the relationship *between* branches. Derived-committed state must
regenerate rather than merge because its content is a pure function of source: a textual merge of two
regenerations is not the regeneration of the merged sources, so only regeneration is correct. Authored overlap
must refuse rather than guess because there is no source-of-truth to regenerate from — only a human decision.

This is a genuinely new decision domain: eADR-0025 owns *where build state lives* (single-Build-centric), and
eADR-0021 owns the *merge gate mechanics*; neither owns concurrency semantics *across* builds. That is why this
is a new founding record rather than an overload of either, and why 0025 and 0021 are amended in place only to
cross-reference it (eADR-0014).

## Anti-choice

Rejected: treating "green once" as durable authorization (the pre-freshness non-strict floor). Rejected:
hand-merging or side-picking derived artifacts. Rejected: auto-reconciling authored conflicts. Rejected:
modeling concurrency as a per-Build execution-DAG concern only (eADR-0041) with no cross-PR serialization law.
Rejected: making a native provider merge queue the architectural contract — it is one backend behind a
provider-independent seam, available where offered and never the floor. Rejected: any coordinator merge path —
the coordinator prepares and orders; the operator merges.

## Status

accepted
