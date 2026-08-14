---
title: Build product grounding — consume settled intent without inventing it
---

## Purpose

Build begins from product intent that is either settled or honestly absent. This procedure preserves the
product-grounding work formerly embedded in Build orchestration: milestone context, readiness evidence,
canonical specification resolution, complete criterion coverage, and review-step projection. These signals
inform engineering judgment; they do not become a second merge gate.

## Steps

1. When `docs/spec/build-plan.md` exists, run `.engine/tools/milestone_emit.py emit`: it consumes the product
   module's build order, creates one native GitHub milestone per named phase idempotently, and never invents
   Engine/review vocabulary for those plain-language names. Assign each open work Issue with `gh issue edit
   <number> --milestone <phase>`. Before starting a phase, run `.engine/tools/build_readiness.py check --phase
   <phase>` and retain any unsettled scheduled work in the risk evidence. Both signals are advisory. Without a
   committed build order there is nothing to emit; the Build plans its own phase and does not fabricate one.
2. Resolve any settled-description pointer through `spec_referent`. Distinguish three outcomes:
   - no authority is declared, so the plan carries an explicit no-spec disclosure and local obligations;
   - one settled document resolves, so it and every semantically affected settled document are selected;
   - the read fails, is ambiguous, or resolves unexpectedly unsettled authority, which is an authority failure
     and never silently degrades to no-spec.
3. Build consumes settled intent. If the needed product description or criteria do not exist, route that work
   through product intake and settle it before Build; do not author an informal substitute inside the Build.
4. For every selected document, record its canonical path, digest, and why it applies. Map every canonical
   criterion to named work items and planned verification, or mark it not applicable with an operator-visible
   reason. The coordinator re-derives the full denominator and refuses approval when a row is omitted,
   duplicated, or stale.
5. Use the same resolution to project `spec_referent review-steps`. Carry exact criteria—not just digests or
   paraphrases—into plan review, deliverable review, the PR Review record, and submission evidence.
6. Where a hard check has an authorized not-applicable, construction-scoped, or dependency declaration,
   inventory the canonical declaration. Deliverable review receives it and independently checks each bound;
   the declaration is evidence, never a blanket exemption.

## Done when

The plan truthfully names the product authority posture; every selected settled criterion has one complete
mapping or not-applicable reason; failed reads have not been hidden; advisory product signals are available
to the orchestrator; and the exact criteria and derived review steps can reach both review gates and the PR.
