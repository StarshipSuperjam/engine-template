# Your project's own decision records

This folder is yours. It's where your project's own architecture decision records (eADRs) live — the
"we chose X over Y, and here's why" notes that capture a real design or governance call so it survives
past the conversation that made it.

Two things share the `.engine/contracts/` surface, told apart by which folder they sit in:

- **The engine's own founding decisions** live one level up, directly in `.engine/contracts/`. Those are
  the engine's canon — they belong to the engine, and an engine update overlays them wholesale (so don't
  edit them here; a change to one rides an engine release).
- **Your project's decisions** live here, in `instance/`. The engine never overwrites this folder on an
  update — your history is preserved across every upgrade, and these records stay owned by your project,
  not the engine.

You don't have to write these by hand. Just ask the engine to record a decision — describe the call you
made and why — and it will add a record here in the same form.

One file per decision. A short, stable name works well (for example, `eADR-0001-picked-postgres.md`).
