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

### Delegation routing

The gate says which model an unbound subagent may run on. It says nothing about *which* agent to reach
for, and until now nothing did — so the choice was left to each session's judgment and the mechanical
work stayed inline, in the orchestrator's own window, which is where the measured context pressure
actually came from. This is the standing answer.

- **`Explore`** — a mechanical search fan-out across many files or naming conventions, when you want the
  conclusion and not the file dumps. Runs on a cheap model (the gate above enforces it) and cannot spawn
  anything: the platform gives it no subagent tool, so a delegation to `Explore` is a leaf by construction.
- **`Plan`** — drafting an implementation strategy, under the same cheap-model gate. The orchestrator
  still judges what comes back; a plan agent's output is a draft, never a decision.
- **`general-purpose`** — judgment work that needs tools, where a cheap tier would do badly. Not gated,
  and leaf-only **by convention only** — see the residuals below.
- **`engine-grounding-scout`** — the engine's own cheap scout for the ceremony sweeps: the memory-recall
  fan-out and the knowledge-graph impact traversal. Returns a cited shortlist, never a conclusion. Alone
  on the roster it carries an **allowlist**: it names the reading tools it may use and holds nothing else,
  so it has no shell, no editor, no subagent tool, and no way to write.
- **`engine-validation-runner`** — the focused verification you run *while building*: the self-tests, the
  structural checks, or a named subset, in a disposable copy of the working tree, returning a digest of
  what failed and why instead of thousands of log lines. Not the Build coordinator's own validation —
  those runs bind their evidence to the live checkout, which a copy cannot produce. Denies `Agent` and
  `Task`.
- **DAG workers (`engine-worker-bounded`, `engine-worker-builder`)** — implementation nodes of an approved
  Build graph, dispatched with a bounded packet. Both now deny `Agent` and `Task` too.

The engine's judgment personas — the review lenses and the audit persona — keep the ability to dispatch,
but only downward: cheap reconnaissance (`engine-grounding-scout`, or `Explore` on a cheap model) and
nothing else, never a judgment-tier or spawn-capable agent, and never ending a review on work deferred to
another agent. Each of those personas carries that mandate in its own text.

Four residuals, named rather than hidden:

- **`general-purpose` is convention-only.** It inherits every tool, including the subagent tool, and the
  platform exposes no per-agent configuration for it. Nothing mechanical stops it spawning; only the
  prompt it is given.
- **A user-level persona wrapper would be out of reach.** An agent defined in an operator's own
  `~/.claude/agents/` directory is not part of this repository and cannot be constrained from here. If one
  of yours runs engine personas by reading their files at runtime, it treats a persona's denylist as prose
  rather than as a lock, and the leaf-lock above does not bind it. Whether such a wrapper exists is a
  property of your machine, not of this project; closing it is a change to your own file.
- **The scouts are Claude-only.** They have no Codex twin; see `provider-exceptions.json` for why.
- **A denylist does not bound the tools a session connects.** A persona that names tools to block inherits
  everything else the session can reach, including whatever MCP servers happen to be connected — so a
  write-capable server the engine has never heard of is reachable by any persona that only denies by name.
  The grounding scout is exempt because it carries an allowlist instead: its whole tool set is enumerated,
  so a newly connected server adds nothing to it. Every other persona, the validation runner included,
  denies by name, because a reviewer's or a shell-capable scout's reach cannot honestly be enumerated in
  advance. For those, what bounds the surface is which servers you connect.

  One honest limit inside that. The platform documents the allowlist *model* and whole-server grants
  (`mcp__<server>`), but not granting one tool from a server while withholding its others — which is what
  the scout does to read the memory server without reaching its three write operations. That is exactly
  why it is on an allowlist: if the per-tool form is not honored the scout gets *less* than intended and
  loses those reads, which shows the first time it runs, where the same unsettled form in a denylist would
  fail the other way and leave the writes reachable. The graph server is granted whole, by the documented
  server-level form, because every tool on it is a read.

**This is not a cost router and does not meter spend.** The engine cannot see its own token use and does not
own the model-invocation loop; `model-routing.md` rejects that scaffolding and that rejection stands. This
gate acts only at the seam the engine genuinely observes — the spawn itself. Being honest about what it
buys: it changes price per token, not the volume of context re-reads, and the measured cost driver in the
builds that motivated it was re-read volume. It is worth having, and it is not the whole answer.

## Rationale

The gate fails toward **allow**. The payload shape it reads is the platform's contract, not the engine's,
and that contract has changed before — the subagent tool has been named both `Task` and `Agent` — so any
shape the gate does not recognize is allowed rather than blocked. A wrong deny is not caught by the hooks
fail-open harness, which covers only crashes, so the escape is explicit.

**Each rule has its own switch**, because they are unrelated behaviours and one combined switch meant that
turning off a self-scheduling deny also silently un-gated expensive subagent spawns:
`ENGINE_SESSION_ECONOMY_MODEL=off` for the cheap-model rule, `ENGINE_SESSION_ECONOMY_WAKEUP=off` for the
self-scheduling rule, and `ENGINE_SESSION_ECONOMY=off` for both. Each deny names its own switch in the
refusal text, so the escape is discoverable at the moment it is needed.

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
