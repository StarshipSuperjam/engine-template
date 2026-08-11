---
title: Release — cut, review, publish, and hand over a new version
---

## Purpose

How a release happens, from the moment you ask for one to the published result — one defined path, so no
cut is improvised. What it releases depends on where it runs: in a deployed project it cuts **your
product's version** (from `product-version.json`); in the engine's own home repo it cuts **the engine's
version**. The same workflow serves both — the tools detect which. This is the *produce* side of the
version line; `/engine-upgrade` is the *consume* side (it pulls a published engine release in, it never
cuts one). Enter this runbook when the operator types `/engine-release` or asks in plain words to cut,
ship, or publish a release. Each step names the block to present, so the operator sees the same shaped
report at every cut.

## Steps

1. **Ground and preflight.** Name which release this repo cuts (product or engine). Check the way is
   clear: no release pull request already open — release branches are named `release/…`, so check
   `gh pr list --json headRefName,url --jq '.[] | select(.headRefName | startswith("release/"))'`
   (a title-word search would false-match ordinary pull requests that mention "release") — recent runs on
   the default branch green (`gh run list --branch <default branch> --limit 5`), and the one-time
   `RELEASE_PAT` secret armed (`gh secret list`; listing secrets needs repo admin — if it is refused, just
   proceed: an unarmed repo stops safely at the workflow's first step with plain recovery — nothing
   changes). Working in an engine-mechanic whose owned product is released elsewhere? Resolve the product
   checkout and slug per `build-orchestration.md`'s owned-product arm (`mechanic_build.py preflight`),
   then run every step below against that repo — the preview in its checkout, `gh --repo` pinned to the
   verified slug.
2. **Preview — read-only, before anything runs.** From the repo root:
   `uv run --directory .engine --frozen -- python tools/release_cut.py propose --json`
   (drop `--json` for the tool's own plain render; locally it authenticates through your logged-in `gh`).
   Present the **Release preview** block, translated into plain words (the JSON's field names are for you,
   never for the report): the derived next version and what forces it (`engine_floor_version` with
   `baseline_note`; a product cut floors at a patch bump), what changed since the last release (the
   merged-pull-request list, by kind), any contract-surface impacts, and the violation gates — all must be
   empty; a violation is a refusal to cut, so stop and present the tool's own reason. A first-ever cut has
   no baseline: the operator must name the starting version.
3. **Dispatch.** `gh workflow run release.yml --ref <default branch>` — leave the version blank to take the
   smallest the changes require; to name one (a first cut, or raising above the floor — never lowering),
   add `-f version=X.Y.Z`. Present the run link. The run opens a release pull request and stops: nothing
   publishes from the dispatch.
4. **Watch the cut.** If the run fails, its own step log and summary carry the plain-language reason and
   recovery — relay those words, never a paraphrase from memory (on an engine cut, a deployment-gate red
   also writes its per-transition matrix to the run summary; on a product cut the gate is inert — its
   absence there is normal, not a miss). On success, present the **Release pull
   request** block: the pull request link (also in the run summary), what it contains (the recorded
   versions and refreshed maps), its body as the evidence bundle, which checks are still running — and the
   consent line: *merging publishes; closing it means nothing happened.*
5. **Hand over the merge.** Watch the pull request's checks (`gh pr checks <number> --watch`) and tell the
   operator the moment it is green and mergeable. The merge is the operator's act and the only publish
   gate — never merge it yourself.
6. **Confirm the publish.** The publish workflow runs on the merge — confirm its conclusion
   (`gh run list --workflow release-publish.yml --limit 1`), the tag, and
   the Release (`gh release view v<version>`). Present the **Published** block: the Release link and what
   it means (instances can upgrade to it; your product is live). If the publish went red it is safe to
   re-run — it is keyed to the merged commit, so a half-done publish completes and a done one no-ops; the
   merged pull request's own comment carries the recovery.
7. **Advance the milestone — when this repo tracks releases with milestones** (list them:
   `gh api repos/{owner}/{repo}/milestones`). Close the completed one and rename it
   `<name> · released v<version>`, in one call:
   `gh api repos/{owner}/{repo}/milestones/<number> --method PATCH -f state=closed -f title="<name> · released v<version>"`.
   No milestones in use? Skip, and say so in the recap.
8. **Advance the board — when [the release-advance runbook](projects-release-advance.md) is present in
   `.engine/operations/`** (it ships with the [github-projects-sync](../modules/github-projects-sync/manifest.json)
   module, so its presence is the condition). Follow it. Absent? Skip, and say so in the recap.
9. **Recap and record.** Present the **Release recap** block: every stage above with its state — done, or
   skipped and why — so nothing is silently dropped. Then record the cut in the project's memory: the
   version, what shipped, anything learned.

## Done when

The Release is published and verified (or the cut stopped with its reason presented plainly), the recap
names every stage's state, and the cut is recorded in memory.

## Notes

- **The four blocks are the presentation contract:** *Release preview* (step 2), *Release pull request*
  (step 4), *Published* (step 6), *Release recap* (step 9). A cut that skips a block is an improvised cut.
- The operator's merge is the sole, binding publish gate; the dispatch is freely reversible.
- Between cuts, dispatching the standalone `release-gate.yml` workflow checks that an engine release still
  deploys cleanly (engine home only; it changes nothing).
- A deployed repo's first product release needs `product-version.json` (seeded at first-run) and the
  one-time `RELEASE_PAT` the release workflow's own first step explains.
