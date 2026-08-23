---
title: Session economy
status: accepted
date: 2026-08-22
---

## Rule

Two ways a session spends heavily are refused mechanically, at the moment they are attempted.

- **A search or planning subagent runs on a cheap model.** `Explore` and `Plan` are the platform's own
  general-purpose helpers; unlike the engine's review personas, which carry a model stamped from
  `.engine/policies/model-bindings.json`, they carry no binding of their own and so inherit whatever the
  orchestrating session is running. They may name only a cheap model — the mechanical tier's, or `sonnet`.
  A strong model is never valid here: an expensive search agent is spin-up cost for work the orchestrator
  should have done inline, and if a task genuinely needs stronger judgment the orchestrator should do it
  itself rather than delegate it.
- **A session does not schedule its own wake-up.** Nothing in the engine ever instructed this. Unattended
  work is fired by the platform's scheduler, not arranged from inside a session, and observed builds spent
  consecutive wake-ups re-reading their whole context to report that nothing had changed.

Both are enforced by `.engine/tools/session_economy.py`, a PreToolUse gate registered under its own narrow
matcher so it never costs a subprocess on unrelated tool calls.

## Scope

Governs which model realizes an **unbound** subagent, and whether a session may schedule itself. It does not
touch the engine's own review personas, whose models come from the bindings file; it does not touch
`general-purpose`, which does judgment work; and it never modulates a review gate — which lenses must run
and pass is untouched.

**This is not a cost router and does not meter spend.** The engine cannot see its own token use and does not
own the model-invocation loop; `model-routing.md` rejects that scaffolding and that rejection stands. This
gate acts only at the seam the engine genuinely observes — the spawn itself. Being honest about what it
buys: it changes price per token, not the volume of context re-reads, and the measured cost driver in the
builds that motivated it was re-read volume. It is worth having, and it is not the whole answer.

## Rationale

The gate fails toward **allow**. The payload shape it reads is the platform's contract, not the engine's,
and that contract has changed before — the subagent tool has been named both `Task` and `Agent` — so any
shape the gate does not recognize is allowed rather than blocked. A wrong deny is not caught by the hooks
fail-open harness, which covers only crashes, so the escape is explicit: set `ENGINE_SESSION_ECONOMY=off`.

That escape is an environment variable rather than a tunable, deliberately. The tunables surface holds
operator preferences and never enforcement switches, and its override file sits outside the weakening guard
on the strength of exactly that invariant; putting a gate's on/off switch there would falsify it. The honest
cost of the choice: hooks inherit the launching process's environment, so the switch cannot be thrown from
inside the session a wrong deny has just stopped — it governs sessions started after it is set.

A residual worth naming rather than hiding: a session denied an expensive `Explore` could spawn
`general-purpose` instead, which is not gated. That is left open because `general-purpose` does judgment
work a cheap model would do badly; the gate is friction against the common case, not a wall.

## Enforcement-tier

- **Mechanical** for the two refusals: they are code at the point of action, not prose a session may forget.
- **Fail-open** by construction, and disableable by the operator, so it can never strand a build.
- The operator's merge remains the binding gate, as everywhere else. This gate spends nothing on their
  behalf and decides nothing about whether a change is right.
