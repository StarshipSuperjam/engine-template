---
title: Describing what you want built
---

## What this covers

This is for the person who wants to tell the engine what to build and have it written down clearly enough that
the work which follows stays true to it. By the end you will know what the `engine-design` command does, how
to start it, where what you write is kept, and what "settling" a description means.

## What you need to know

Before building anything, it helps to write down what you actually want — in plain language, organized so
nothing important is forgotten. The engine does this with you through one command, `engine-design`. You
describe what you want; it lays out the pieces, helps you write each one up, checks that every part is present
and well-formed, and keeps the result as ordinary files in your project under a folder called `docs/spec/`.

You stay in control of how much detail to capture. You can write just enough to get moving, or take more time
up front to think each piece through so there are fewer surprises later — the engine names that trade-off and
lets you choose, and it leans toward keeping things light unless you ask for more.

Each piece of your description moves through three plain stages as it matures: **not yet described** (a piece
you have named but not written up yet), **in progress** (you are writing it), and **settled** (you have looked
it over and accepted it as the description to build from). Settling something is your decision — the engine
never settles a description on its own. A settled description is not frozen forever: you can change or reopen it
later, but not quietly. When a change touches a settled part of your description, the engine asks you to confirm
it on the pull request — by applying the `guardrail-ack` label — before it can merge, so the record always shows
the change was deliberate, never a silent edit.

One thing the engine is careful about: when it checks your description, it is checking that every part is
*present and well-formed* — not whether the design is *right*. Whether the idea is a good one is your call (and
your reviewers'); the check only makes sure nothing is missing or malformed.

Everything lives in your own project as readable files, so you can open `docs/spec/` any time and see exactly
what was written. If your project's GitHub connection is not reachable, the engine still writes and checks your
description from files and tells you so, rather than stopping.

## Finding the commands

You start this by typing **`/engine-design`**. To see this and everything else you can ask the engine for, run
**`/engine-help`**, or type **`/engine`** to narrow your assistant's command menu to just the engine's
commands. You can also simply ask, in plain words — "help me describe what I want to build."

## Where to go next

When you are ready, type `/engine-design` and describe what you have in mind — the engine takes it from there,
one step at a time. If you would rather get oriented first, run `/engine-help` to see everything the engine can
do.
