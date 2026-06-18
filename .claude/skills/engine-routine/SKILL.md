---
name: engine-routine
description: Set up unattended work — let me advance a planned build on a schedule while you're away, adding each change to a pull request for your approval.
invocation: operator-typed
disable-model-invocation: true
---

## Steps

1. Enter and follow the procedure in `.engine/operations/routine-entry.md`. It confirms this run can work
   on its own and in an isolated copy of your repo, finds the next planned piece of work and the files it's
   allowed to touch, does that piece and adds the commit(s) to the open pull request, and leaves a GitHub
   Issue for anything it can't handle alone — never merging anything.

## Notes

This is a command you put inside a Claude Desktop **routine** so I can keep a planned build moving while
you're away — not something I start on my own. To set one up:

1. In Claude Desktop, create a routine (in the sidebar) and choose when it should run.
2. Put `/engine-routine` in the routine's Instructions.
3. Turn on **"Work in an isolated copy of the repo"** (worktree mode), so each run works in its own copy,
   not your main one. A scheduled run won't do this on its own — this is the step that keeps it off your
   working checkout.
4. In the routine's settings, set it to handle permissions automatically (turn on **Auto mode**) so it can
   work without stopping to ask. If your plan can't turn Auto mode on, the run will stop and ask instead of
   working unattended — and you'll know, because it leaves a note (a GitHub Issue) rather than silently
   stalling.
5. Make sure your computer won't go to sleep during the scheduled time — a local routine only runs while
   your machine is awake.

While it works on its own it stays inside the planned files and **never merges** — every change still waits
for your approval as a pull request. It adds its work to that pull request but doesn't finish it: when
you're back, open a normal session and ask me to wrap up and submit it for your approval. Staying in scope
is me following the plan, not a lock I can't break; your merge is the real gate. You'll see progress on the
pull request, and anything I can't do alone — including not finding the work I was pointed at — I leave as a
GitHub Issue so you can pick it up when you're back.
