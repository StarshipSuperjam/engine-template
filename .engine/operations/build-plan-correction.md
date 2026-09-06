---
title: Build plan correction — when the Build finds its plan wrong
---
## Purpose

Read when the coordinator reports `engineering-decision`: a checkpoint judged `plan_revision_required` or
`operator_decision_required`, an assumption is still `unresolved`, or a `trivial` profile met a condition it
cannot carry. The seal holds; this runbook says how a Build corrects its plan without losing its evidence.
The surrounding flow is [Build orchestration](build-orchestration.md).

## Steps

1. Decide what kind of discovery this is. The responsibility boundary in the spine assigns that judgment to the
   orchestrator: an ordinary engineering leaf inside the approved design and scope is solved in place and the next
   checkpoint records `aligned`; a changed agreement needs the plan corrected, and a question of design, law,
   authority, or the agreed capability boundary returns to the operator.
2. If the Build discovers the plan itself is wrong, the seal still holds: clone it, take the clone through its own
   approval and panel, then `plan adopt --successor <id> --input <bound-plan.json>`. The Build keeps its pull
   request and the integration evidence of nodes the successor carries with unchanged ancestry; changed nodes and
   their dependants reset, and the plan panel does not re-run.
3. A sealed plan's revision is always the operator's call, recorded with `--operator-change` and disclosed at
   merge. A plan revised away from its seal has that divergence disclosed too, with the review stated as not
   covering the delta.
4. An `unresolved` assumption holds the `ready` phase: clear it with `assumption dispose`, or with `plan revise`
   when the answer changes the plan. A `trivial` profile that meets a condition it cannot carry is revised to
   `normal` and re-approved, as [Build kickoff](build-kickoff.md) says.
5. Return to [Build implementation](build-implementation.md) once the checkpoint records `aligned` or the
   successor is adopted; `status` names the runbook again from the phase it derives.

## Done when

The Build's plan authority is intact — the original seal, or an adopted successor that passed its own approval and
panel — every assumption is disposed, the latest checkpoint is `aligned`, and any divergence from the reviewed
plan is on record for the contract to disclose.
