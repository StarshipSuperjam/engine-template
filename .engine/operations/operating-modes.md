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

The mechanism is `.engine/tools/modes.py` — the ephemeral, session-keyed stance signal, the `PreToolUse`
write-gate, and the `PostToolUse` plan-acceptance Build-entry trigger, wired as hooks in
`.claude/settings.json`. The stance lifecycle:

1. **Every session boots in explore.** At session start, boot clears the stance signal first
   (`modes.clear_stance`), so even a resumed session never inherits a prior build stance. When the
   signal is absent, stale, or unreadable, the stance is explore — the safe default is the floor, never
   the ceiling.
2. **While exploring, the gate denies the building actions and allows everything else.** It denies the
   small enumerated set that begins building — editing files (Edit / Write / MultiEdit / NotebookEdit),
   creating a branch, committing, and opening a pull request (via `gh pr create` or the GitHub MCP
   create-pull-request tool) — with a plain sentence that names what was blocked and the way forward. That
   denial rides the platform's `PreToolUse` reason channel, which does not reliably reach the operator's
   screen, so the assistant relays it to the operator in plain words — never a silent refusal. One denial
   carries a memory-specific relay: a hand-edit of a memory store (the engine's own `.engine/memory/` or
   the harness auto-memory notebook) is most often the operator asking to be remembered, so instead of the
   build-set "open a pull request" line it confirms a competent "noted" and that the engine records it
   automatically — readable back on request — never a code-change refusal (D-251 / #257). It
   allows reading, running read-only commands and tests, greps, spawning subagents, and logging issues.
   It also allows Claude Code's own plan file — that is planning, not building — recognized by the
   platform's plan-mode marker, not a path, so it holds even if the plan folder is moved into the repo; a
   write to the operator's own `~/.claude/` config carries no such marker and stays denied. An action it
   cannot classify is allowed: there is no default-deny, because exploring must stay the comfortable place
   to work.
3. **To start building, the operator either types `/engine-start` or accepts a plan.** `/engine-start` is
   an operator-only command the model cannot invoke itself (it carries the platform's operator-only flag,
   and the skill-coherence check holds that flag in place); accepting a plan the model proposed also enters
   build (the model cannot accept its own plan). Either way the stance flips to build, the gate permits the
   writes, and entering build is announced as the work begins. Neither path is silent or self-elected — the
   model never flips its own stance.
4. **Routine is unattended, scope-locked build work** entered by an operator-authored scheduled fire; it
   never merges the protected branch (authored later). It is the same workflow, constrained.

To see what the gate decides for any action without Claude Desktop (the operator demo): `python
tools/modes.py demo` (which also shows the plan-file carve-out and plan-acceptance entering build), or
`python tools/modes.py classify <Tool> [command] [--session S] [--pm MODE]`.

## Done when

The session's stance is legible and enforced as a nudge: in explore, a building action is refused with a
plain sentence while a read or a test runs unimpeded; setting the build stance permits the same action;
clearing the signal returns the session to explore. The current stance is named in the status block the
assistant renders first each session ("Exploring — I won't change files…"), and a denied action is relayed
to the operator in plain words rather than refused in silence.

## Notes

The gate is a **deliberate-effort nudge, not a wall** — stated honestly, never overstated. The current
platform honors the gate's deny (emitted as the exit-0 + `hookSpecificOutput` form, across built-in and
GitHub-MCP tools); it is still fallible for two durable reasons: a crashing gate fails open (the action
proceeds, by design — a gate must never strand the operator), and detecting a build verb in a shell
string is best-effort (an alias, `eval`, substitution, or chaining evades it). The only unbypassable
guarantee is the **protected-branch merge** — any write that ever slips the gate (a crash, an evaded
verb, or a `permissions.allow` entry that outranks the hook, which is why the engine never allow-lists a
gated tool) still cannot reach the protected branch unreviewed. Never dress the local gate as the wall.
