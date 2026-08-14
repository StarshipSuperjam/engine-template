---
title: Build submission evidence — make the ready PR truthful and reproducible
---

## Purpose

Submission is the last factual reconciliation before a draft becomes ready. This procedure preserves the
original Build runbook's disclosure obligations while keeping truthfulness-dependent claims in prose written
by the orchestrator. The coordinator records deterministic results and checks completeness; it does not
write a flattering story about the change.

## Steps

1. Reconcile the target branch, run final validation, and retain complete stdout and stderr in OS-temporary
   logs. Relay live progress or a heartbeat while commands run. Local state records each log path and digest;
   status stays concise. Durable handoff carries results and digests but never machine-local paths.
2. Regenerate the knowledge graph and self-map last. State how many generated index files changed and confirm
   whether regeneration lost any authored work. If regeneration had nothing to change, say that plainly.
3. Fill the pull-request contract with the delivered scope and behavior, frozen validation commands and
   results, change/scope profile, exact spec-derived review steps or the honest no-spec disclosure, and one
   operator-runnable demonstration (or the real reason no observable demonstration exists).
4. The Review record names the approved depth and checks performed in operator language. Disclose whether a
   reviewer executed code and that execution was confined to a discarded copy. Include reviewed and final
   commits, measured divergence, the proportional re-review judgment, and any focused result.
5. Include the standing unresolved-conversation notice and report each relevant submission condition:
   recognized automation that did not apply; canonical hard-check not-applicable declarations; open fail-open
   findings; unavailable live helper or MCP checks; and any submission check that could not obtain evidence.
   “Could not check” is never rendered as “clean.”
6. Run the registered preflights against the live PR body and final commit: close linkage, PR-contract
   completeness, scope-profile evidence, spec review-step projection, declaration inventory, automation and
   fail-open disclosures, and local-reference checks where applicable. Deterministic facts are recorded;
   prose claims receive completeness checks only.
7. Preview submission and inspect the resulting action. Apply may mark the draft ready. No coordinator path
   may merge, approve on behalf of the operator, or weaken protected-branch review.

## Done when

The final commit has complete retained diagnostics, fresh validation and preflight evidence, an honest PR
contract covering every applicable disclosure, a runnable demonstration, and a truthful review narrative.
The PR may be marked ready for the operator and remains unmerged.
