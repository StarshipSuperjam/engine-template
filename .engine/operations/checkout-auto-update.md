---
title: Automatic project-folder updates — reviewed opt-out preference
---

## Purpose

The Engine normally brings the operator's project folder up to date at session start only when it is already
on the verified remote default branch and is clean, strictly fast-forwardable, and unchanged through the
final safety check. This procedure lets the operator inspect, disable, or re-enable that bounded automation.
It never changes GitHub, pushes, merges, moves a session worktree, or broadens the automatic recovery path.

## Steps

1. On `/engine-setup`, show the current setting with:

   ```text
   uv run --directory .engine -- python tools/checkout_auto_update.py show
   ```

   A missing setting means automatic catch-up is enabled. An explicit `false` setting means it is disabled;
   the Engine still detects drift and offers **bring it up to date** as the existing consented action.
2. Only after the operator clearly asks to change the setting, prepare the matching reviewed change:

   ```text
   uv run --directory .engine -- python tools/checkout_auto_update.py disable
   uv run --directory .engine -- python tools/checkout_auto_update.py enable
   ```

   The command atomically writes `.engine/operator-checkout.json`, creates a pull request, and leaves the
   choice pending until the operator merges it. This file is operator configuration: Engine upgrades preserve
   it and Engine overlays do not own it.
3. If `show` reports malformed or unreadable preference data, automatic catch-up is already paused. Explain
   the named reason and use `enable` or `disable` to write a valid replacement when the filesystem permits.
   If that write cannot complete, report the filesystem error and leave the existing file untouched.

## Done when

The operator can see whether automatic updates are on, off, or paused for repair; any change has a reviewed
pull request; and an opt-out project continues receiving drift detection and the manual recovery offer.
