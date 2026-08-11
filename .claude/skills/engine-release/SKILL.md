---
name: engine-release
description: Cut and publish a new release of this project — your product's version in a deployed repo, the engine's own in its home repo. Previews what the release would be, opens it as a pull request you review, and publishes only on your merge.
invocation: operator-typed
disable-model-invocation: true
allowed-tools: Bash(uv run *), Bash(gh *)
---

## Steps

1. Enter and follow the procedure in `.engine/operations/engine-release.md`. In short: preview what the
   next release would be (read-only), dispatch the release workflow, watch it open the release pull
   request, hand the merge to the operator, confirm the publish, advance the milestone and board where
   they exist, and close with a recap of every stage.

## Notes

This *produces* a release; `/engine-upgrade` *consumes* one (it pulls a published engine release into this
repo). Nothing publishes until the operator merges the release pull request. Typing the verb is the wired
entry; a plain-words ask ("cut a release") reaches the same runbook through the engine's knowledge map.
