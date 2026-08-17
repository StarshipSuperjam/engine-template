---
status: draft
---

# Capability name

<!-- Starting shape for one capability document under docs/spec/. Copy it to docs/spec/<capability>.md, give
it a clear name above, and replace the guidance in each section with the real thing. The stage marker in the
frontmatter above moves through three steps as the work matures: `stub` (a named piece you have not described
yet — needs nothing but this marker and a row in the index), `draft` (in progress — needs the three sections
below), then `locked` (settled — the operator has accepted it as the ground to build from). The operator only
ever hears the plain words "not yet described / in progress / settled", never these markers. -->

## Summary

<In plain language, what this capability is and who it is for — one short paragraph. Someone who reads only
this should understand what the piece does and why it matters.>

## Behavior

<What the capability does, as the rules it follows or the steps a person sees — concrete enough that you could
tell whether a finished build matches it. Describe behavior the user can observe, not how it is built inside.>

## Acceptance criteria

<One row per thing that must be true for this capability to count as done. For each row, say what must be
true, how that is checked, and who checks it: write `operator` when the operator confirms it themselves (for
example by trying it on screen), or `engine` when the engine checks it automatically. Keep the first two
columns plain; only the last column's wording is fixed.>

| Criterion | How verified | Who checks it |
| --- | --- | --- |
| What must be true for this to be done | How that is confirmed | operator |
| Another thing that must be true | How that is confirmed | engine |

## Operator and automatic workflow routing

<How this capability is reached. Name the operator command(s) a person types to use it, and the automatic
routes that let the assistant recognize and run it on the operator's behalf — or say plainly that it is
internal and no command or automatic route names it. If the engine should offer or generate a route for this
capability (for example, a setup route the engine offers when the module is available), say which and when.
Every written-up capability answers this here, even if the answer is "not operator- or model-reachable".>
