---
id: eADR-0038
title: The product-design module produces a structured description by default
status: accepted
date: 2026-07-18
---

## Decision

The product-design intake produces a **fixed structural backbone by default** — the `docs/spec/` description
corpus plus a guiding-principles document and an architecture overview — and the operator opts *out* to a
lighter, corpus-only description rather than opting in to the fuller one. The depth choice is **recorded** (a
depth marker the intake writes), not inferred, so the default has a checkable correlate. This decision also
**establishes** the spec-structure-integrity standing rule (the policy of the same name): loosening how
tightly a description's prose pins its details is never license to dismantle the description's structure.

## Significance

This locks in that a real, structured design is the module's **default outcome**, not an opt-in a
non-engineer would have to know to ask for. Future work on the module must keep the backbone the default and
may not quietly reduce the default to a thin corpus; may not treat the recorded depth marker as advisory; and
may not read a "keep the prose loose" instruction as authority to remove the structural apparatus. It fixes
the *shape* that is owed by default — the backbone and a recorded depth — while leaving the concrete document
set enumerated in one home (the module's scaffold), not re-listed here, so this record does not become a
filename ledger that drifts.

## Rationale

The module's whole value is that a non-engineer who does not know the vocabulary (and will never request the
artifacts by name) can still trust the engine to produce a genuine, structured design. An opt-in default hands
that exact operator the thin result — the opposite of the confidence the module promises. Making the full
write-up the default, with a recorded opt-out, inverts that. Recording the depth as a committed marker gives
the "full by default" promise a non-AI correlate a check can enforce, which the trust model values over a
prose-only intention. Separating "loosen the prose" from "delete the structure" closes the specific
degradation path this decision responds to.

## Anti-choice

The strongest alternative was to **leave document structure discretionary per project** — let each session
author whatever structure it judges the product needs, with no mandated backbone. It lost because that
discretion is the status quo that produced the failure this fixes: a session collapsed the structured
artifacts into free prose and justified it as "keeping the description at the level of durable laws." When the
structural default depends on the drafting session's judgment, the default outcome for a non-engineer becomes
unsupervisable by the very person it is meant to serve. A mandated backbone with a recorded, deliberate
opt-out keeps the operator in control without making structure something they must know to demand. (User
guides remain genuinely discretionary — "only the ones the product needs" — because their membership is
elastic per product; it is the *backbone* that is mandated, not an exhaustive document list.)

## Status

accepted
