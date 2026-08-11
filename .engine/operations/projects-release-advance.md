---
title: Advance the Projects board after a release — the roadmap hand-off
---

## Purpose

After a release publishes, a roadmap board that tracks the current release should stop pointing at the
finished one and point at the next. This runbook advances it. It is entered from the release runbook
([engine-release](engine-release.md), step 8), after the released milestone is closed and renamed.
Everything here is the operator's own board — the engine touches it only because the operator asked for a
release, and every write is shown before it happens.

## Steps

1. **Find the release view.** Boards vary: this module's setup creates the engine's five fields, not
   views, so a release view exists only if the operator (or a session, at their ask) made one. List the
   board's views (GraphQL: `projectV2 { views(first: 20) { nodes { id name filter } } }`) and look for one
   filtered by the released milestone — commonly named "Current release". No such view? Say so in one line
   and stop here — there is nothing to advance.
2. **Re-point it.** Show the operator the change first — the old filter and the new one (the next open
   milestone's title, keeping the filter's own shape; a filter without `is:open` is deliberate, so a Done
   column keeps populating). Then run the GraphQL mutation `updateProjectV2View` with the view's id and
   the new `filter`. This needs the `project` permission (`gh auth refresh -s project`; the setup runbook
   explains what that grants). If the mutation is refused, hand the operator the exact filter line to
   paste into the view's filter box instead — same outcome, one manual edit.
3. **Check the next release's readiness.** If the board's own operating rubric names entry conditions for
   a release becoming current (a maintenance floor, a capacity bound), read the next milestone against
   them and flag what is missing — flag it, never fix it silently.
4. **Present the Transition checklist:** milestone closed · renamed · view re-pointed (old → new filter
   shown) · next release's readiness noted. Any line that did not happen says why.

## Done when

The board's release view points at the next open milestone (or the operator was told, in one line, that
the board carries no release view), and the Transition checklist was presented with nothing silently
skipped.

## Notes

- This is the one place the engine changes anything on the board beyond its own five fields — only at a
  release, only at the operator's ask, and shown before it happens. The setup runbook's promise names this
  exception.
- The board never advances itself: GitHub has no self-advancing milestone filter, so the advance is a
  deliberate step here, not standing automation.
