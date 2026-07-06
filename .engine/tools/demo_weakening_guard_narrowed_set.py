#!/usr/bin/env python3
"""Demo — the safety guard now asks for your deliberate OK only when a real safety gate changes (#250 / D-268).

What this checks, in plain words: the engine has a guard that stops a merge and asks for your deliberate approval
whenever a change could turn OFF one of your safety gates. It used to fire on ANY edit to a file in the engine's
tools folder — including harmless ones (start-up, memory, status displays) — which trained clicking-through. This
shows, on REAL guard logic and your REAL check definitions, that after the fix the guard:
  - still fires on a genuine safety gate (a check's enforcement code, the file that wires the write-gate, or the
    write-gate itself), and
  - stays quiet on a harmless helper file,
so the deliberate approval stays rare and meaningful.

It runs the guard's OWN classifier (`flagged_changes` / `is_guardrail`, deriving the guarded check-scripts from
your live `.engine/check/` definitions) — not a stand-in. Nothing is changed; it feeds the classifier example
diffs and prints what it decides.

Run: uv run --directory .engine -- python tools/demo_weakening_guard_narrowed_set.py
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import weakening_guard  # noqa: E402


def _flagged(filename: str, status: str = "modified") -> bool:
    """True iff the REAL guard classifier would stop the merge for this one-file change (deriving the guarded
    check-scripts from your live check definitions, exactly as it does in CI)."""
    return bool(weakening_guard.flagged_changes([{"filename": filename, "status": status}]))


# (label, filename, status, should_be_flagged, plain-language why)
CASES = [
    ("a check's enforcement code", ".engine/tools/product_design/coverage.py", "modified", True,
     "a safety check's own logic — guarded by being named in a check definition"),
    ("the file that wires your gates", ".claude/settings.json", "modified", True,
     "wires the write-gate and other enforcement hooks — was a blind spot before #250"),
    ("the write-gate itself", ".engine/tools/modes.py", "modified", True,
     "the Explore/Build write-gate enforcement hook"),
    ("the branch-protection setup", ".engine/tools/bootstrap.py", "modified", True,
     "applies your branch ruleset — the ruleset has no file of its own, so this is its stand-in"),
    ("a harmless helper (start-up)", ".engine/tools/boot.py", "modified", False,
     "session start-up briefing — not a safety gate; used to fire before #250"),
    ("a harmless helper (memory)", ".engine/tools/memory/consolidate.py", "modified", False,
     "memory housekeeping — not a safety gate"),
    ("a harmless helper (status)", ".engine/tools/engine_status.py", "modified", False,
     "status dashboard — not a safety gate"),
    ("a brand-new safety check", ".github/workflows/new-check.yml", "added", False,
     "a pure addition strengthens protection, so it never asks for approval"),
]


def main(_argv=None) -> int:
    print("What this checks: after #250, the guard asks for your deliberate approval only when a change could")
    print("turn off a real safety gate — not for harmless helper edits. (issue #250 / D-268)\n")

    # Sanity: the guarded check-scripts really are being DERIVED from your live check definitions (not hard-coded).
    derived = weakening_guard._derive_check_scripts()
    if derived is None:
        print("Could not read your check definitions, so the guard fell back to watching the WHOLE tools folder")
        print("(the safe direction). This demo needs the real derived list to make its point — investigate why")
        print(f"`{weakening_guard._BASE_CHECK_DIR}` was unreadable. Your project was not touched.")
        return 1
    print(f"Guard derived {len(derived)} enforcement script(s) from your live check definitions "
          "(guarded by being present).\n")

    wrong = []
    for label, filename, status, expected, why in CASES:
        got = _flagged(filename, status)
        verb = {"modified": "changing", "added": "adding"}[status]
        mark = "asks for approval" if got else "stays quiet"
        ok = "OK" if got == expected else "WRONG"
        if got != expected:
            wrong.append((label, filename, expected, got))
        print(f"  [{ok:5}] {verb} {label:32} -> guard {mark}")
        print(f"          ({filename} — {why})")

    print()
    if not wrong:
        print("In plain words: every real safety gate still triggers the deliberate approval, and every harmless")
        print("helper is left alone — so the approval you give stays rare and worth reading. The four 'stays")
        print("quiet' helpers above would ALL have demanded approval before #250.\n")
        print("Vary it yourself: add a line to CASES with any file path and whether you expect it flagged, then")
        print("re-run — e.g. try `.engine/tools/telemetry.py` (quiet) or `.engine/tools/validate.py` (approval).")
        return 0
    print("This run did NOT confirm the guard's behavior — these cases came out wrong:")
    for label, filename, expected, got in wrong:
        print(f"  - {label} ({filename}): expected flagged={expected}, got flagged={got}")
    print("That is a real signal worth investigating, not a pass. Your project was not touched.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
