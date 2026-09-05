# `operation-shape/` — the hardened length tier's negative fixture

The `kind-shape/` fixture proves the shape kind bites on a BROKEN SECTION ORDER. This fixture proves the other
thing the operation-shape rule enforces: an operation whose sections are all present and in order but whose
prose body is OVER ITS LINE BUDGET fails at the **hard** tier once a rule opts in with `length_tier: "hard"`
(the flip typed-lifecycle part C made for `.engine/check/operation-shape.json`, StarshipSuperjam/engine-template#821).

`rule.json` is a transient shape rule that opts in, with an inlined six-line budget so the seeded `input.md` —
well-formed, sections in order, prose past six lines — bites for the length reason alone (`expect.json` pins
the `over its 6-line budget` token). The input also carries a fenced block and a generated region, which the
count leaves out: they pad the file without changing the number the finding reports.

The checker-of-checkers covers each closed kind once, by `kind-<kind>/`; a per-rule tier of a closed kind is
not in its roster, so this fixture is driven through the same unit builder and bite predicate by
`test_hard_check_bite.py` (`TestOperationShapeLengthTierBites`), not by the live meta-check.
