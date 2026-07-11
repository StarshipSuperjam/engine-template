"""ledger_health.py — memory's boot-surfaced health detectors.

`ledger.py` is a leaf: it reads the ledger and RETURNS a read-health report, but never surfaces
anything itself. This module is the concern layer that turns that report into a boot signal — the
same detect→relay split the other memory→boot detectors use (restore_vault's restore/migration
offers). Boot lazily imports and gathers it; render decides the operator-facing line.

`detect_ledger_malformed` reports a ROTTING ledger — one or more complete-but-unparseable lines that
the resilient read skips (and compaction skips-and-reports). Left unsurfaced, a ledger could lose
recall line by line with no signal; this is that signal, at cold start.

It deliberately does NOT report a torn TRAILING line: that is the normal, self-healing state after any
crash mid-append (the very next append heals it, ledger.append), so surfacing it would be a standing
false alarm rather than a real degradation.

`detect_stalled_migration` reports an in-flight-migration marker whose migration didn't finish (its
process died, or it is far past any real migration's span) — automatic tidying (compaction) is paused
until it clears. The clear itself is compaction's job (it self-heals a stale marker under the lock); this
detector is read-only, for boot's heads-up. A LIVE marker (a migration genuinely in progress) is normal
and draws no signal.
"""

from __future__ import annotations


def detect_ledger_malformed(cwd: "str | None" = None) -> "int | None":
    """The count of unreadable (malformed) lines in the live ledger, or None on any read fault.

    0 on a clean (or empty, or torn-only) ledger — a falsy no-signal boot treats as nothing to say.
    A positive count is a genuine-corruption signal boot surfaces. None (a read/import fault) also
    surfaces nothing: this is a best-effort health readout, never a gate, so it degrades to silence.
    """
    try:
        from memory import ledger
        report = ledger.read(path=ledger.ledger_path(cwd))
        return report.malformed
    except Exception:  # noqa: BLE001 — a health readout degrades to no-signal, never breaks boot
        return None


def detect_stalled_migration(cwd: "str | None" = None) -> bool:
    """True iff a memory migration didn't finish and left an ORPHANED in-flight marker (dead process / past the
    wall-clock ceiling), so compaction is paused until the marker clears. False on a clean state, a genuinely
    LIVE migration (normal, not a stall), or any read/import fault — a best-effort readout that degrades to
    silence, never a gate.
    """
    try:
        from memory import capture, ledger
        return capture.detect_orphaned_migration(ledger.ledger_dir(cwd)) is not None
    except Exception:  # noqa: BLE001 — a health readout degrades to no-signal, never breaks boot
        return False
