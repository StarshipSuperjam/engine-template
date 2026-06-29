# `.engine/_fixtures/` — the negative-fixture namespace (test data, not a surface)

This directory holds **negative fixtures**: deliberately-broken inputs the engine runs its own `hard` checks
against to prove each one actually *bites* (it catches a known-bad example, rather than passing green while doing
nothing). It is the standing, CI-enforced "checker-of-checkers" the negative-fixture meta-check uses.

Design of record: engine-planning decision log **D-256…D-260** (the meta-check law, the by-presence fixture
grammar, and this reserved namespace as a Tier-2 leaf under `.engine/`).

## What lives here

A negative fixture is **bound to its logic unit by presence** (a naming/location convention, not a rule field —
the check schema is unchanged). Each in-scope hard check-logic unit gets one subdirectory, discovered by name:

- a **check-kind callable** (the closed core kinds, plus any module-added kind) → `kind-<kind>/`
  (e.g. `kind-schema/`, `kind-coverage/`)
- a **`custom/script` check instance** → `<check-id-stem>/`, the rule id minus `engine/check/`
  (e.g. `disposition-issue-resolution/`)

Each unit directory holds a `rule.json` (the transient rule the meta-check runs), the seeded bad input (a single
bad file; or, for the repo-global `coverage`/`coherence` kinds, a malformed mini-tree or a `manifests.json` data
literal), and an `expect.json` sidecar. The sidecar declares **`{"severity": ..., "message_contains": ...}`** — the
meta-check asserts by **set-membership** that a finding of that severity carrying that message token is present
(never order/count). The token is required, and it is what distinguishes the unit's *intended* finding from an
unrelated bite: a fixture that fail-closes for the wrong reason fires a hard finding but not the expected one, so it
does not satisfy the assertion. A unit with no statically-decidable CI failure path instead carries a
`not-applicable.json` whose `property` is exactly `"no statically-decidable failure path in the CI environment"`
(verbatim — a compressed slug would reopen the self-classification escape D-257/D-258 closed); the meta-check lists
every such carve-out loudly and it is re-derived at the review gate.

**Every fixture must live under `.engine/_fixtures/` and nowhere else.** The exclusions that shield these files
are anchored on this exact path, so a fixture placed outside the namespace (or under a near-miss sibling like
`_fixtures-schema/`) would get none of the shielding and red the real suite.

## Why it is invisible to the real checks

These files are **test data, not a governed surface**. They are intentionally excluded from the live validation
suite so a committed bad input neither reds the real checks nor reads as an orphan/uncatalogued surface:

- `catalog-coverage` lists `.engine/_fixtures/` as infrastructure (not a catalogued surface).
- `link-integrity` excludes it (a fixture's deliberately-broken Markdown link must not fail CI).
- the module-coherence ownership walk prunes it (no module `provides` a fixture).

The knowledge graph never fingerprints these files (it entitizes only files that are *both* claimed by a module's
`provides` *and* under a catalogued surface location — fixtures are neither), so nothing here needs a `graph.json`
regen.
