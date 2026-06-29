# `.engine/_fixtures/` — the negative-fixture namespace (test data, not a surface)

This directory holds **negative fixtures**: deliberately-broken inputs the engine runs its own `hard` checks
against to prove each one actually *bites* (it catches a known-bad example, rather than passing green while doing
nothing). It is the standing, CI-enforced "checker-of-checkers" the negative-fixture meta-check uses.

Design of record: engine-planning decision log **D-256…D-260** (the meta-check law, the by-presence fixture
grammar, and this reserved namespace as a Tier-2 leaf under `.engine/`).

## What lives here

Each in-scope hard check-logic unit gets one subdirectory, discovered by name:

- a **check-kind callable** (the closed core kinds, plus any module-added kind) → `kind-<kind>/`
  (e.g. `kind-schema/`, `kind-coverage/`)
- a **`custom/script` check instance** → `<check-id-stem>/`, the rule id minus `engine/check/`
  (e.g. `disposition-issue-resolution/`)

Each unit directory holds the seeded bad input (a single bad file; or, for the repo-global `coverage`/`coherence`
kinds, a malformed mini-tree or a `manifests.json` data literal) **plus an `expect.json`** naming the
`(finding-id, severity)` the meta-check asserts by set-membership — or a reviewed `not-applicable` disclosure for a
unit with no statically-decidable CI failure path.

## Why it is invisible to the real checks

These files are **test data, not a governed surface**. They are intentionally excluded from the live validation
suite so a committed bad input neither reds the real checks nor reads as an orphan/uncatalogued surface:

- `catalog-coverage` lists `.engine/_fixtures/` as infrastructure (not a catalogued surface).
- `link-integrity` excludes it (a fixture's deliberately-broken Markdown link must not fail CI).
- the module-coherence ownership walk prunes it (no module `provides` a fixture).

The knowledge graph never fingerprints these files (it entitizes only catalogued, module-provided surfaces), so
nothing here needs a `graph.json` regen.
