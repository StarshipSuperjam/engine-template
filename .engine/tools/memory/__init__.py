"""The engine's memory substrate package (memory-substrate-sqlite-fts5).

The public import surface the rest of the engine binds to as ``memory`` — e.g. the close turn-hook's
ambient-capture relay does ``import memory; memory.capture_turn_delta(payload)``. That relay is wrapped
so that, until a later slice defines ``capture_turn_delta`` here, the call degrades to a safe no-op
(``AttributeError`` is swallowed) — the ledger-before-hooks invariant in action.

This module is deliberately import-side-effect-free: importing ``memory`` does no filesystem work and
binds no submodule, so it cannot fail or do work on a live session's turn. Callers reach the ledger
primitives explicitly with ``from memory import ledger``.

Build slice 1 ships only the ledger (``memory.ledger``); the derived index, capture, active-forgetting,
the search interface + MCP server, and backup/restore land in later slices.
"""
