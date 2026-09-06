---
title: Operating modes — the session stance and the Explore write-gate
---

## Purpose

Keep a session honest about what it may do. Every session runs in one of three stances — **explore**
(the default), **build**, or **routine** — and this runbook is the operating guide to that stance: how
exploring gates the building actions, how a session deliberately enters build, and why the gate is a
deliberate-effort nudge rather than a wall. Enter it whenever you need to understand or explain why a
building action was refused while exploring, why a session's attempt to merge the protected branch is
refused in any stance, or what changes when a session starts building.

## Steps

The mechanism is `.engine/tools/modes.py` — the ephemeral, session-keyed stance signal, the `PreToolUse`
write-gate, and the two native-plan intake adapters (Claude's `PostToolUse` plan-exit adapter, Codex's
`UserPromptSubmit` envelope adapter), wired as hooks in `.claude/settings.json` and `.codex/hooks.json`.
The stance lifecycle:

1. **Every session boots in explore.** At session start, boot clears the stance signal first
   (`modes.clear_stance`), so a resumed session does not inherit a prior build stance. When the signal is
   absent, unreadable, or unrecognized, the stance is explore — the safe default is the floor, never the
   ceiling. (Any absent, unreadable, or unrecognized signal reliably resolves to explore; the boot clear
   that removes a prior marker is best-effort, and the protected-branch merge is the absolute backstop.)
2. **While exploring, the gate denies the building actions and allows everything else.** It denies the
   small enumerated set that begins building — editing files (Edit / Write / MultiEdit / NotebookEdit),
   creating a branch, committing, and opening a pull request (via `gh pr create` or the GitHub MCP
   create-pull-request tool) — with a plain sentence that names what was blocked and the way forward. That
   denial rides the platform's `PreToolUse` reason channel, which does not reliably reach the operator's
   screen, so the assistant relays it to the operator in plain words — never a silent refusal. One denial
   carries a memory-specific relay: a hand-edit that targets a memory store but stays denied — the
   engine's own `.engine/memory/` (never hand-written; its CLI is the only safe door) or a memory-looking
   path that is not really the session's own notebook — earns an honest memory line instead of the
   build-set "open a pull request" wording: it says plainly that nothing was saved by the blocked write
   and names the doors that do work — the pin verb for what the operator asked to keep, the assistant's
   own notebook for its own notes — never a code-change refusal, and never a false "saved" (StarshipSuperjam/engine-template#257, StarshipSuperjam/engine-template#766).
   It allows reading, running read-only commands and tests, greps, spawning subagents, and logging issues.
   It also allows Claude Code's own plan file — that is planning, not building — recognized by the
   platform's plan-mode marker together with where the write lands: inside the plans folder the platform
   is configured to use (`plansDirectory`, read from the managed, user and workspace settings in that
   order, else `~/.claude/plans`), so it holds even if the plan folder is moved into the repo, while a
   folder that would make the checkout, the home or the engine's own directories "the plan folder" is
   refused (StarshipSuperjam/engine-template#775). That is one of two path-anchored exceptions; the other (StarshipSuperjam/engine-template#766) is a Write/Edit whose every
   path resolves inside the harness's own auto-memory notebook for **this** project
   (`~/.claude/projects/<this project>/memory/`) — the session's own notebook, not the project. Both are
   judged on the real filesystem (symlinks and `..` resolved, the project bound to the session's working
   directory, anything undecidable denied). A write to the operator's own `~/.claude/` config lands in
   neither place and stays denied — even during plan mode, when the denial names the plans folder the
   engine resolved and the setting that moves it. An action it cannot classify is allowed: there is no
   default-deny, because exploring must stay the comfortable place to work.
3. **To start building, the operator types `/engine-start` — and only that.** It is an operator-only
   command the model cannot invoke itself (it carries the platform's operator-only flag, and the
   skill-coherence check holds that flag in place). The stance flips to build, the gate permits the writes,
   and the operator meets build-entry exactly once, through the build-orchestration kickoff ("opening a
   draft pull request and planning the work"). The stance signal has exactly two writers — this verb and
   `set-routine` — and **no hook writes it**, so the model can never flip its own stance.

4. **Accepting a plan imports it; it does not start building it.** This changed: accepting a plan used to
   flip the stance straight to build, which skipped every gate the plan side exists to run. Now the
   acceptance is caught by an intake adapter — Claude's plan-exit completion, or on Codex a message whose
   very first characters are `PLEASE IMPLEMENT THIS PLAN:` — and the accepted document lands in the Project
   Manager as an **unapproved draft**: no approval, no review, no seal, no build authority. On Claude the
   adapter reads the plan from where the harness now puts it: the inline text on an older harness, else
   the completion's own result, else the plan file that result names — read only from the platform's
   plans folder (or a `plansDirectory` set in managed or your own settings, never a project's file). The
   session then reports where the text came from, the plan's id, the revision it created, and the exact
   next command, so an acceptance is never followed by an unexplained refusal. Nothing is invented on the way in: the text is kept verbatim,
   the payload is empty, and the things an import cannot know — what this is asking for, the problem, the
   case against, the decomposition — are recorded as open decisions the plan cannot be sealed with.
   Where a hook cannot run (a Codex hook trust the operator has not re-approved, or an acceptance line the
   platform has since reworded), `python tools/project_manager.py import-native --input - --provenance
   "<where this plan came from, in your words>"` performs the identical import from the plan text on stdin.
5. **Routine is unattended, scope-locked build work** entered by an operator-authored scheduled fire: a
   Claude Desktop routine runs the routine command, which enters
   the Routine write-stance through `set-routine` — a **mechanical** gate that grants the stance only in a
   proven-isolated worktree, never the operator's checkout — and which the run additionally declines to enter
   when its start-of-session hooks did not fire (an honest-tier check the run follows). It never merges the protected branch. It is the same workflow,
   constrained.

To check the live stance, `python tools/modes.py stance` — it resolves the session from `--session` or
`$CLAUDE_CODE_SESSION_ID`, and says `unknown` (non-zero) rather than a misleading `explore` when it cannot
resolve one. To see what the gate decides for any action without Claude Desktop (the operator demo): `python
tools/modes.py demo` (which also shows the plan-file carve-out, and that accepting a plan leaves the
session where it was), or `python tools/modes.py classify <Tool> [command] [--session S] [--pm MODE] [--file PATH] [--cwd DIR]` (for example `classify Write --pm plan --file ~/.claude/plans/x.md` shows the plan-mode allow, and the same call with `--file ~/.claude/settings.json` the denial that names the plans folder).

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
gated tool) still cannot reach the protected branch without the operator's own merge. That merge is the
operator's **informed consent**, not a review of the code — in solo it clears with zero required approvals
(team adds a code-owner review) — and the session never performs it in any stance (a best-effort nudge
refuses a session `gh pr merge`; the wall is the merge itself). Never dress the local gate as the wall.

Intake is fail-safe too, in every direction. If an intake adapter never fires — including accepting a plan
with the context cleared, which does not fire it (claude-code#20397) — nothing is imported and the typed
`import-native` verb is the recovery. If the adapter fires but finds no plan text anywhere it may read, it
says so rather than staying silent: the session is handed a notice naming what the hook saw, what it
tried, and the `import-native` recovery, and is told to relay it to you — an empty acceptance is never the
benign case it once was. A message that merely mentions or quotes the Codex acceptance line is
not an acceptance, because the line only counts at the very start of the message. And none of it can reach
the stance: no hook writes the signal, so a miss, a misfire, or a failed import all leave the session in
explore, never falsely in build.
