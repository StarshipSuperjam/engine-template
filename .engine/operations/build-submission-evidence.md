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
5. Report each applicable conditional lane exactly (the unresolved-conversation guidance is no longer folded
   into every pull request — it is surfaced only when a merge is actually blocked; see the blocked-merge
   recovery path in [boot-session-start.md](boot-session-start.md)):
   - A recognized automation PR says its body-completeness result was **not applicable**, not verified. Keep
     that distinct from any separately required `guardrail-ack`; automation never supplies the operator's
     acknowledgement.
   - The local pre-submission run lists any credential- or pull-request-witnessed check on a distinct **not
     verified in this run — enforces in CI** line: it did not run here for lack of a local witness, so its
     result is *not yet verified*, not *not applicable anywhere*. Read that line before marking ready — those
     checks bite when CI runs on the real pull request.
   - An open fail-open finding goes in Validation as: “a safety check could not run on this change: what it
     would have checked; this work was not verified for X.” It informs consent and does not become a new gate.
   - Reuse boot's provider-specific `.engine/tools/boot.py` `mcp_availability_check` result. If it did not run,
     run that canonical procedure; never infer health from the visible tool list. Distinguish an undiscovered
     helper (trust/approval and restart) from one registered but not answering (diagnosis). Say nothing when
     helpers are healthy.
   - Carry exact spec review steps, applicable hard-check declarations, and any owned/unowned local-reference
     result. “Could not check” is never rendered as “clean.”
6. Run registered preflights against the live PR body and final commit. PR-contract completeness is the hard
   mechanical prerequisite. Close linkage is detect-and-surface posture: record its lines and bounded defang
   advice but do not turn a contradiction into a readiness wall. Record the scope profile and applicable
   declaration inventory. Truthfulness-dependent conditional lanes remain orchestrator prose checked by cold
   review; mechanics must not claim they semantically proved the prose.
7. Preview submission and inspect the resulting action. Apply may mark the draft ready. No coordinator path
   may merge, approve on behalf of the operator, or weaken protected-branch review.

## Done when

The final commit has complete retained diagnostics, fresh validation and preflight evidence, an honest PR
contract covering every applicable disclosure, a runnable demonstration, and a truthful review narrative.
The PR may be marked ready for the operator and remains unmerged.
