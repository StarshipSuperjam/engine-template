---
title: Advance the Projects board after a release — the roadmap hand-off
---

## Purpose

After a release publishes, a roadmap board that tracks the current release should stop pointing at the
finished one and point at the next. This runbook advances it. It is entered from the release runbook
([engine-release](engine-release.md), step 8), after the released milestone is closed and renamed.
Everything here is your own board — the engine touches it only because you asked for a release, and every
write is shown to you before it happens.

## Steps

1. **Find the release view.** Boards vary: this module's setup creates the engine's five fields, not
   views, so a release view exists only if you (or a session, at your ask) made one. The board's id is in
   the module's local settings (the gitignored `.engine/projects-sync/` config, written at setup). List
   the views and look for one filtered by the released milestone — commonly named "Current release":
   `gh api graphql -f query='query($id: ID!) { node(id: $id) { ... on ProjectV2 { views(first: 20) { nodes { id name filter } } } } }' -f id=<project-id>`
   No such view? Say so in one line and stop here — there is nothing to advance.
2. **Re-point it.** Show the change first — the old filter and the new one (the next open milestone's
   title, keeping the filter's own shape; a filter without `is:open` is deliberate, so your Done column
   keeps populating). Then run:
   `gh api graphql -f query='mutation($view: ID!, $filter: String!) { updateProjectV2View(input: {viewId: $view, filter: $filter}) { projectV2View { name filter } } }' -f view=<view-id> -f filter='<new filter>'`
   This needs the `project` permission (`gh auth refresh -s project`; the setup runbook explains what that
   grants). If the mutation is refused, you get the exact filter line to paste into the view's filter box
   yourself instead — same outcome, one manual edit.
3. **Check the next release's readiness.** If the board's own operating rubric names entry conditions for
   a release becoming current (a maintenance floor, a capacity bound), read the next milestone against
   them and flag what is missing — flag it, never fix it silently.
4. **Present the Transition checklist:** milestone closed · renamed · view re-pointed (old → new filter
   shown) · the next release's readiness noted. Any line that did not happen says why.

## Done when

The board's release view points at the next open milestone (or you were told, in one line, that the board
carries no release view), and the Transition checklist was presented with nothing silently skipped.

## Notes

- This is the one place the engine changes anything on the board beyond its own five fields — only at a
  release, only at your ask, and shown to you before it happens. The setup runbook's promise names this
  exception.
- The board never advances itself: GitHub has no self-advancing milestone filter, so the advance is a
  deliberate step here, not standing automation.
