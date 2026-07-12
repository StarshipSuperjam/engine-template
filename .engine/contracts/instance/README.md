# Your own engine decisions

This folder is yours. It records the decisions you make **about your engine** — "we turned on the
projects-sync module, and here's why", "we tuned this guardrail because…" — the real choices about how
your engine is set up, captured so they survive past the conversation that made them.

This is for decisions about the *engine*, not about your product. Your product's own architecture
decisions — which database, which framework — belong in whatever decision-record system your project
keeps in its own space; the engine never puts product records in its `.engine/` corner. What lives here
is the deployment-side counterpart to the engine's own founding decisions.

Two things share the `.engine/contracts/` surface, told apart by which folder they sit in:

- **The engine's own founding decisions** live one level up, directly in `.engine/contracts/`. Those are
  the engine's canon — the structural-law *why* behind how the engine itself works. They belong to the
  engine, carry the engine's own `eADR-####` names (the "e" is for engine), and an engine update overlays
  them wholesale. So leave those alone: a change to one rides an engine release, not an edit in your copy.
- **Your engine decisions** live here, in `instance/`. The engine never overwrites this folder on an
  update — your decision history is preserved across every upgrade, and these records stay yours.

You don't have to write or name these by hand. Just ask the engine to record an engine decision — describe
the choice you made and why — and it will add a file here for you, named so it won't clash with the
engine's own `eADR-####` canon in the folder above. One file per decision.
