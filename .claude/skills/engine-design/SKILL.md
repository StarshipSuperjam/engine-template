---
name: engine-design
description: Describe what you want to build, in plain words — I'll help you write it down clearly, check it holds together, and settle it as the description to build from.
invocation: operator-typed
disable-model-invocation: true
allowed-tools: Bash(uv run *), Bash(gh *)
---

## Steps

1. Read the runbook `.engine/operations/product-intake.md` and follow it with the operator, one step at a
   time: lay out the pieces of what they want and confirm the shape together, agree on how much detail to
   capture now, then ask them to type `/engine-start` — the write-up under `docs/spec/`, its checks and the
   operator's go-ahead all happen in Build, and reach the project through the pull request they merge. Keep
   the procedure in the runbook — this command is just the way the operator starts it.

## Notes

This is a command the operator types to describe what they want built — it is never started on the engine's
own initiative. Everything it produces is plain, readable files inside the operator's own project, written in
Build and landing through a pull request; while exploring, the engine proposes and nothing is written. Nothing
is treated as settled until they say so, and a settled description can always be changed later — the engine
just asks the operator to confirm the change on the pull request first (by applying the `guardrail-ack` label),
so it is never a quiet edit. If the project's GitHub connection isn't reachable, Build cannot open, so the intake
proposes and waits and says so — what the operator said is kept in the conversation. Once a description is
settled, the same command can turn it into a build order and a list of things to build — the work a build picks
up from.
