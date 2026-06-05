---
title: Operating modes — the session stance and the Explore write-gate
---

## Purpose

Keep a session honest about what it may do. Every session runs in one of three stances — **explore**
(the default), **build**, or **routine** — and this runbook is the operating guide to that stance: how
exploring gates the building actions, how a session deliberately enters build, and why the gate is a
deliberate-effort nudge rather than a wall. Enter it whenever you need to understand or explain why a
building action was refused while exploring, or what changes when a session starts building.

## Steps

The mechanism is `.engine/tools/modes.py` — the ephemeral, session-keyed stance signal plus the
`PreToolUse` write-gate, wired as a `PreToolUse` hook in `.claude/settings.json`. The stance lifecycle:

1. **Every session boots in explore.** At session start, boot clears the stance signal first
   (`modes.clear_stance`), so even a resumed session never inherits a prior build stance. When the
   signal is absent, stale, or unreadable, the stance is explore — the safe default is the floor, never
   the ceiling.
2. **While exploring, the gate denies the building actions and allows everything else.** It denies the
   small enumerated set that begins building — editing files (Edit / Write / MultiEdit / NotebookEdit),
   creating a branch, committing, and opening a pull request (via `gh pr create` or the GitHub MCP
   create-pull-request tool) — with a plain sentence that names what was blocked and the way forward. It
   allows reading, running read-only commands and tests, greps, spawning subagents, and logging issues.
   An action it cannot classify is allowed: there is no default-deny, because exploring must stay the
   comfortable place to work.
3. **To start building, the operator types the Build-entry verb** — an operator-only command the model
   cannot invoke itself (authored in a later slice). Only then does the stance flip to build and the
   gate permit the writes, and entering build is announced. There is no automatic or self-elected entry.
4. **Routine is unattended, scope-locked build work** entered by an operator-authored scheduled fire; it
   never merges the protected branch (authored later). It is the same workflow, constrained.

To see what the gate decides for any action without Claude Desktop (the operator demo): `python
tools/modes.py demo`, or `python tools/modes.py classify <Tool> [command] [--session S]`.

## Done when

The session's stance is legible and enforced as a nudge: in explore, a building action is refused with a
plain sentence while a read or a test runs unimpeded; setting the build stance permits the same action;
clearing the signal returns the session to explore. The current stance is named in the boot orientation
card ("Exploring — I won't change files…").

## Notes

The gate is a **deliberate-effort nudge, not a wall** — stated honestly, never overstated. The current
platform honors the gate's deny (emitted as the exit-0 + `hookSpecificOutput` form, across built-in and
GitHub-MCP tools); it is still fallible for two durable reasons: a crashing gate fails open (the action
proceeds, by design — a gate must never strand the operator), and detecting a build verb in a shell
string is best-effort (an alias, `eval`, substitution, or chaining evades it). The only unbypassable
guarantee is the **protected-branch merge** — any write that ever slips the gate (a crash, an evaded
verb, or a `permissions.allow` entry that outranks the hook, which is why the engine never allow-lists a
gated tool) still cannot reach the protected branch unreviewed. Never dress the local gate as the wall.
