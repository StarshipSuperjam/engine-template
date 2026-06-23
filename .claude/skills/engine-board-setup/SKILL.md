---
name: engine-board-setup
description: Set up a GitHub Projects board that shows, at a glance, what the engine is building.
invocation: operator-typed
disable-model-invocation: true
allowed-tools: Bash(gh *), Bash(uv run *)
---

## Steps

1. Read the runbook `.engine/operations/projects-sync-setup.md` and follow it with the operator, one step
   at a time: grant the one-time `project` permission, create the board, link it to this repo, create the
   engine's five fields, connect the board with
   `uv run --directory .engine -- python tools/projects_sync/projects_sync.py resolve <project-id>`, then
   optionally turn on auto-add and read it back with `… projects_sync.py check`.
2. Before the permission step, make sure the operator hears it plainly: the `project` permission lets the
   engine read and change **every** GitHub Project on their account, not just this repo's board — it is
   optional and revocable (`gh auth refresh --remove-scopes project`). Do not run it for them without that.
3. When done, confirm with `… projects_sync.py check` and tell the operator the board now refreshes at the
   start of each session.

## Notes

This is a command you type to connect a progress board. The board is a one-way mirror: the engine keeps
only its own fields in step and never touches your Status or card moves. You can skip it entirely — the
engine works the same from your issues and pull requests — and you can delete the board later without
losing anything. Removing the board or the permission is something you do on GitHub yourself; the engine
cannot reach back out to undo those.
