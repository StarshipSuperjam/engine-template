---
title: Escalation
status: accepted
date: 2026-06-03
---

## Rule

When a trigger fires, the AI stops acting on its own and surfaces the decision rather than pressing ahead. Two kinds of trigger:

- Always-fire — three things always stop the work:
  - a killswitch-class weakening of the engine's own guardrails, as the weakening guard
    (`engine/check/guardrail-weakening`) classifies it: a change that removes, disables, renames or loosens an
    enforcement gate or a protection the engine ships, changes what code its own checks run, repoints where the
    engine's code is fetched from or written to, or lowers the project's declared identity — the tier that holds
    the merge until the operator's deliberate `guardrail-ack`. The guard owns what falls in that tier; this policy
    does not re-list it;
  - a change to one of the engine's authority documents — a policy, an instruction floor (`CLAUDE.md`,
    `AGENTS.md`), the codes of conduct — that loosens a rule or a protection;
  - a head-on conflict between two hard rules.

  In an authorized Build, a change the approved plan itself names is not a fresh stop — the plan gate was that
  stop; a change the plan did not name still stops, Build or not. An ordinary edit to a protection file is
  disclosure-tier: the guard leaves a plain notice at the merge, it needs no action, it is never an escalation on
  its own, and it must not shape a design.
- Judgment — ambiguity about what is wanted, an action that is hard to reverse or reaches outside the immediate work, or a step that breaks the agreed scope. These stop the work when the outcome the operator actually cares about would change, or the action would be hard to undo.

The invariant under both: never quietly continue past a trigger.

An authorized Build is not itself an escalation trigger when it still has actionable work. Status commentary, an ordinary failed check, or an engineering obstacle stays with the Build until it is solved or becomes a real authority boundary. Assistant prose and a self-labelled state are not authority. Operator pause and cancellation have their own persistent authority and are not cleared by unrelated repository progress.

## Scope

Applies to all autonomous AI action, in two modes. In an interactive session (exploring or building with the operator present) the AI stops and asks. In a routine, unattended run it cannot ask, so it halts that line of work and records a tracked issue instead, which is brought back to the operator at the next start-up.

## Rationale

The purpose is to make sure the AI never silently makes a call that should have been the operator's — especially one that is hard to take back. When the AI stops, it explains the situation in plain language, names the decision to be made, and lays out the options, so the operator can choose. It never dumps a technical error trace in place of a clear question.

## Enforcement-tier

- **Posture** — the stop-and-surface habit is an expectation the AI follows at runtime; this policy itself does not mechanically force it.
- The hard backstops that make the posture safe are owned by other parts of the engine, not by this policy — and they cover two different things. An unescalated change **to a protected file** is caught mechanically: the locks on the engine's protected files and the protected-branch merge gate catch it regardless of whether anyone escalated — a killswitch-tier finding holds the merge for the acknowledgment, a disclosure-tier one passes with its notice. That catch makes weakening non-silent and deliberate, not impossible: in solo the operator holds admin and could bypass the ruleset. An unescalated **concern**, by contrast, is only held by the end-of-session ritual to the extent it was **recorded** — a concern never written down has no mechanical catch, at close or at merge (the operator consents on evidence, not by reading the diff), so that case rests on the stop-and-surface habit above. So a change to something protected cannot land silently; an unrecorded judgement call is guarded by posture, not by a detector.
