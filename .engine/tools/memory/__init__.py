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
(``memory.index``), and turn-delta capture (``memory.capture`` / ``memory.capture_turn_delta``). The
AI-judged episodic consolidation + role typing, the abandoned-session sweep, the search interface + MCP
server, active-forgetting, and backup/restore land in later slices.
"""

from memory.capture import capture_turn_delta  # noqa: F401 — the public capture entry close's relay calls

__all__ = ["capture_turn_delta"]
