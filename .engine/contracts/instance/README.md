# Your own engine decisions

This folder is yours. It records the decisions you make **about your engine** — "we turned on the
projects-sync module, and here's why", "we tuned this guardrail because…" — the real choices about how
your engine is set up, captured so they survive past the conversation that made them.

This is for decisions about the *engine*, not about your product. Your product's own architecture
decisions — which database, which framework — belong in whatever decision-record system your project
keeps in its own space; the engine never puts product records in its `.engine/` corner. What lives here
is the deployment-side counterpart to the engine's own founding decisions.

Two things share the `.engine/contracts/` surface, told apart by which folder they sit in:

- **The engine's own founding decisions** live one level up, directly in `.engine/contracts/`. Those are the
  *why* behind how the engine itself works. They carry the engine's own `eADR-####` names (the "e" is for
  engine — for example `eADR-0017`), where your own records here read `acme-eADR-0007` (your project's name in
  front); an engine update overlays the engine's wholesale. So leave those alone: a change to one rides an
  engine release, not an edit in your copy.
- **Your engine decisions** live here, in `instance/`. The engine never overwrites this folder on an
  update — your decision history is preserved across every upgrade, and these records stay yours.

You don't write or name these by hand. When you make an engine decision worth keeping, you can ask the engine
to record it — describe the choice and why — and it writes the file for you here. It puts your project's own
short name in front of the record's name — for example `acme-eADR-0007`, where the engine's own founding
records read `eADR-0017` — so your records read as yours at a glance and are never taken for the engine's,
even as the engine adds more of its own over time. One file per decision.
