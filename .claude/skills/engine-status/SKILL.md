---
name: engine-status
description: Show where your project stands — what's next, what recently shipped, and anything that needs your attention.
invocation: model-auto
allowed-tools: Bash(uv run *)
---

## Steps

1. Show where the project stands by running:
   `uv run --directory .engine -- python tools/engine_status.py --session "${CLAUDE_SESSION_ID}"`
   (`${CLAUDE_SESSION_ID}` is filled in with this session's id before this runs; if it ever comes through
   empty, the status still renders — only the building-or-looking-around line may be left off.)
2. Show the operator the status exactly as it is printed — where the project stands, what recently shipped,
   and anything needing their attention. Do not summarize or reword it.

## Notes

This is a command you can type any time — and one I may run myself when you ask where things stand — to see
your project's status. It only reads; it never changes anything.
