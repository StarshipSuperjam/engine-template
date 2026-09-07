---
name: engine-start
description: Start building — switch from looking around to making changes, which I'll put up for your approval.
invocation: operator-typed
disable-model-invocation: true
allowed-tools: Bash(uv run *)
---

## Steps

1. Switch this session into building by running:
   `uv run --directory .engine --frozen -- python tools/modes.py set-build --session "${CLAUDE_CODE_SESSION_ID}"`
   (the engine works out this session's identity automatically. If the command reports it could not
   identify the session, say so plainly: the stance stays as it was, and building has not started.)
   This step needs no network and no accepted activation — entering Build is the operator's own control, and
   it must work whether or not memory-write qualification has converged on this machine.
2. Tell the operator, in plain words, that the session is now building — say: "Building — I'll make changes
   and submit them as a pull request for your approval."
3. Begin the work by following `.engine/operations/build-orchestration.md`, the spine that names one runbook per
   phase: start with `.engine/operations/build-kickoff.md` — open the draft pull request and bind the sealed
   plan — and read each later runbook only when the coordinator's `status` names it.
4. Once the Build is authorized, keep moving through its actionable work. A progress report is not a
   handoff: continue the next planned step unless the Build is submission-ready or a real authority
   boundary requires the operator's decision. Do not schedule a self-wakeup instead of working.

## Notes

This is a command you type to begin building. I won't start building on my own — that is your call: type
`/engine-start` and the work begins. Approving a plan I've shown you imports it as a draft for the Engine's
planning — it does not start building; the typed command is the only way in, on either runtime.

Memory-write qualification is not part of this command. It converges by itself at session start, and when it
has not, the session simply runs unqualified: memory still reads, and writing waits. If that matters to what
you are about to do, `/engine-status` says so in plain words.
