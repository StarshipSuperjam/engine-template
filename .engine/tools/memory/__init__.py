"""The engine's memory substrate package (memory-substrate-sqlite-fts5).

The public import surface the rest of the engine binds to as ``memory`` — e.g. the close turn-hook's
ambient-capture relay does ``import memory; memory.capture_turn_delta(payload)``. As of the capture
slice that relay is LIVE: ``capture_turn_delta`` is exposed here, so the previously-dormant seam now
appends the completed turn's delta to the ledger instead of degrading to a no-op. The function is
fail-soft (any fault is a clean no-op return, never a raise), so close is still never gated by capture.

Importing ``memory`` binds the ``capture`` + ``ledger`` submodules but does **no filesystem work** —
all reads/writes happen inside the called functions — so the import itself cannot fail or do work on a
live session's turn. Callers reach the ledger/index primitives explicitly with ``from memory import
ledger`` / ``from memory import index``.

Shipped so far: the ledger (``memory.ledger``), the derived index + plain-scan fallback
(``memory.index``), turn-delta capture (``memory.capture`` / ``memory.capture_turn_delta``), AI-judged
episodic consolidation — the closed role vocabulary + the abandoned-session ``SessionStart`` sweep
(``memory.consolidate``), and active forgetting (Layer 1): logical retirement + scored demotion
(``memory.forget``), crash-safe ledger compaction (``memory.compact``), and gist roll-up
(``memory.rollup``). The public search interface + MCP server (with the live recall/reinforcement caller and
the live maintenance triggers) land in later slices, as does backup/restore (with its resurrection-surfacing).
Layer-2 audit-gated physical erasure has shipped its enactment core (the gated removal + sole minter in
``memory.compact``, slice 4e-i) and its cross-session observer (``memory.erasure_observer``, slice 4e-ii).
"""

from memory.capture import capture_turn_delta  # noqa: F401 — the public capture entry close's relay calls

__all__ = ["capture_turn_delta"]
