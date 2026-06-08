---
name: engine-setup
description: Set up your project for the first time — I'll walk you through a few choices, then get everything ready.
invocation: operator-typed
disable-model-invocation: true
allowed-tools: Bash(uv run *)
---

## Steps

1. Follow the procedure in `.engine/operations/first-run.md`. In short: run
   `uv run --directory .engine -- python tools/instantiator.py show` to present the choices — who reviews
   changes here, and which optional add-ons to include — take the operator's answers, then confirm to save
   their choices. From there the engine continues setting things up and turning on the review gate.

## Notes

This is the command you type once, in a brand-new project, to set the engine up. It walks you through who
reviews changes and which optional add-ons to include, then gets everything ready. On a project that is
already set up, it has nothing to do and says so.
