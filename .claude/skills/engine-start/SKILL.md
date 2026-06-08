---
name: engine-start
description: Start building — switch from looking around to making changes, which I'll put up for your approval.
invocation: operator-typed
disable-model-invocation: true
allowed-tools: Bash(uv run *)
---

## Steps

1. Switch this session into building by running:
   `uv run --directory .engine -- python tools/modes.py set-build --session "${CLAUDE_SESSION_ID}"`
   (`${CLAUDE_SESSION_ID}` is filled in with this session's id before this runs; if it ever comes through
   empty, the command falls back to the session's own environment value, so building still starts.)
2. Tell the operator, in plain words, that the session is now building — say: "Building — I'll make changes
   and submit them as a pull request for your approval."
3. Begin the work by following the build procedure in `.engine/operations/build-orchestration.md` — open
   the draft pull request and plan the work before changing anything.

## Notes

This is a command you type to begin building. I won't start building on my own — that is your call: type
`/engine-start`, or approve a plan I've shown you, and either one begins the work.
