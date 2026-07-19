---
name: engine-setup
description: Set up your project for the first time — I'll walk you through a few choices, then get everything ready.
invocation: operator-typed
disable-model-invocation: true
allowed-tools: Bash(python3 .engine/tools/instantiator.py *), Bash(uv run --directory .engine -- python tools/memory/backup_vault.py disclosure*), Bash(uv run --directory .engine -- python tools/memory/backup_vault.py setup*)
---

## Steps

1. Follow the procedure in `.engine/operations/first-run.md`. In short: run
   `python3 .engine/tools/instantiator.py show` to welcome the new operator — a plain-language orientation to
   what's already running (the essentials that come with every Engine, described not chosen) — and present the
   choices: who reviews changes here, and which optional add-ons to include (each addable later or removable).
   Take the operator's answers, then confirm to save their choices. From there
   the engine continues: it installs the choices and turns on the review gate, checks that everything fits
   together (pausing in plain words if something needs fixing — the operator's choices are never lost), offers the
   operator a private off-computer backup of the project's memory (creating one only on a clear yes to a named
   destination), and finally tidies away the one-time setup files.

## Notes

This is the command you type once, in a brand-new project, to set the engine up. It walks you through who
reviews changes and which optional add-ons to include, then gets everything ready. On a project that is
already set up, it has nothing to do and says so.

Setup is launched with plain `python3` — not the engine's own tool runner — on purpose: it is the one step
that has to run *before* it installs that runner, so it cannot depend on it. Every other engine command runs
through the installed runner; this one alone runs on the system's Python, and only until setup has installed
the runner. Do not switch this launch to the tool runner — that would stop setup from being able to start on a
brand-new project.
