#!/usr/bin/env python3
"""Demo — the safety guard now asks for your deliberate OK only when a real safety gate changes (#250).

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


_BASE_HOME = "acme/engine-home"   # a stand-in "current home" so the demo needs no real manifest

def _home_flagged(patch: str) -> bool:
    """True iff the REAL repoint detector would stop the merge for this manifest diff, given a home already
    recorded. Drives `home_repoint` directly (the manifest-content leg the file-set cases above don't reach)."""
    return weakening_guard.home_repoint(
        [{"filename": ".engine/engine.json", "status": "modified", "patch": patch}], _BASE_HOME) is not None


# (label, unified-diff patch, should_ask_approval, plain-language why) — the manifest's update-home line.
_HOME_CASES = [
    ("a harmless reformat (trailing comma)",
     '@@ -1,3 +1,4 @@\n-  "home_repository": "acme/engine-home"\n'
     '+  "home_repository": "acme/engine-home",\n+  "control_plane": {"ruleset_id": 901}\n }\n',
     False,
     "first-run appends a block after the home line, so it gains a comma — the VALUE is unchanged, so no "
     "approval is asked (this is the #515 false alarm the fix removes)"),
    ("a real repoint (new value)",
     '@@ -1,3 +1,3 @@\n-  "home_repository": "acme/engine-home"\n'
     '+  "home_repository": "evil/look-alike"\n }\n',
     True,
     "the home value changes — where your engine fetches its own code from — so it asks for your approval"),
    ("removing the home line",
     '@@ -1,4 +1,3 @@\n   "identity": "solo",\n-  "home_repository": "acme/engine-home"\n }\n',
     True,
     "removing the recorded home would let a LATER change set a new one unchecked, so the removal itself "
     "asks for approval (#550 — this used to slip through)"),
]


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
    ("a validation gate's schema", ".engine/schemas/contract.v1.json", "modified", True,
     "the shape a decision record must match — the teeth of a hard merge check, so loosening it loosens that "
     "gate (#467); an internal report format checked only by a test, e.g. plan-review-finding.v1.json, stays quiet"),
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
    print("turn off a real safety gate — not for harmless helper edits. (issue #250)\n")

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

    # The manifest-content leg (#515/#550): the guard also asks for approval when a change repoints the
    # engine's update home — the repository it fetches its own code from — but stays quiet on a harmless
    # reformat of an unchanged home. This drives the REAL repoint detector against example manifest diffs.
    print()
    print("And the engine's update home (the repository it fetches its own code from), against example")
    print(f"manifest changes with a home already recorded ({_BASE_HOME}):\n")
    for label, patch, expect, why in _HOME_CASES:
        got = _home_flagged(patch)
        mark = "asks for approval" if got else "stays quiet"
        ok = "OK" if got == expect else "WRONG"
        if got != expect:
            wrong.append((label, ".engine/engine.json", expect, got))
        print(f"  [{ok:5}] {label:34} -> guard {mark}")
        print(f"          ({why})")

    print()
    if not wrong:
        print("In plain words: every real safety gate still triggers the deliberate approval, and every harmless")
        print("helper is left alone — so the approval you give stays rare and worth reading. The four 'stays")
        print("quiet' helpers above would ALL have demanded approval before #250; the home leg stays quiet only")
        print("for a reformat that leaves the value unchanged, and asks whenever the home is repointed OR removed.\n")
        print("Vary it yourself: add a line to CASES with any file path and whether you expect it flagged, or a")
        print("line to _HOME_CASES with a manifest diff — then re-run.")
        return 0
    print("This run did NOT confirm the guard's behavior — these cases came out wrong:")
    for label, filename, expected, got in wrong:
        print(f"  - {label} ({filename}): expected flagged={expected}, got flagged={got}")
    print("That is a real signal worth investigating, not a pass. Your project was not touched.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
