#!/usr/bin/env python3
"""Demo — guard attention distinguishes disclosure from hard acknowledgement (#250).

What this checks, in plain words: the engine has a guard that notices changes to its safety gates. Ordinary
enforcement modifications get a soft disclosure for review; removals, renames, and hard-floor changes require a
deliberate acknowledgement. It used to fire on ANY edit to a file in the engine's tools folder — including harmless
ones (start-up, memory, status displays) — which trained clicking-through. This shows, on REAL guard logic and your
REAL check definitions, that after the fix the guard:
  - classifies a genuine safety-gate change at the correct disclosure or acknowledgement tier, and
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


def _tier(filename: str, status: str = "modified") -> str | None:
    """The real guard tier: hard acknowledgement, soft disclosure, or no attention."""
    flagged = weakening_guard.flagged_changes([{"filename": filename, "status": status}])
    return weakening_guard.classify(filename, status) if flagged else None


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


# (label, filename, status, expected tier (or None), plain-language why)
CASES = [
    ("an audit library modification", ".engine/tools/audit_digest.py", "modified", "soft",
     "an active hard check's declared enforcement library — disclosed for review with no acknowledgement"),
    ("an audit library removal", ".engine/tools/audit_digest.py", "removed", "hard",
     "removing a guarded file is a hard weakening and requires acknowledgement"),
    ("the file that wires your gates", ".claude/settings.json", "modified", "soft",
     "wires the write-gate and other enforcement hooks, so an ordinary edit is disclosed"),
    ("the write-gate itself", ".engine/tools/modes.py", "modified", "soft",
     "the Explore/Build write-gate hook; its ordinary edits are disclosed"),
    ("the hard gate configuration", ".engine/suites.json", "modified", "hard",
     "the hard floor defining suites; changing it requires acknowledgement"),
    ("the branch-protection setup", ".engine/tools/bootstrap.py", "modified", "soft",
     "applies the branch ruleset, so an ordinary edit is disclosed"),
    ("a validation gate's schema", ".engine/schemas/policy.v1.json", "modified", "soft",
     "the shape a standing hard rule must match; its ordinary edit is disclosed"),
    ("a harmless helper (start-up)", ".engine/tools/boot.py", "modified", None,
     "session start-up briefing — not a safety gate; used to fire before #250"),
    ("a harmless helper (memory)", ".engine/tools/memory/compact.py", "modified", None,
     "memory housekeeping — not a safety gate"),
    ("a harmless helper (status)", ".engine/tools/engine_status.py", "modified", None,
     "status dashboard — not a safety gate"),
    ("a brand-new safety check", ".github/workflows/new-check.yml", "added", None,
     "a pure addition strengthens protection, so it needs no guard attention"),
]


def main(_argv=None) -> int:
    print("What this checks: the guard separates soft disclosure from hard acknowledgement, and leaves")
    print("harmless helper edits alone. (issue #250)\n")

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
        got = _tier(filename, status)
        verb = {"modified": "changing", "added": "adding", "removed": "removing"}[status]
        mark = {"hard": "requires hard acknowledgement", "soft": "makes a soft disclosure", None: "stays quiet"}[got]
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
        print("In plain words: ordinary audit-library changes are disclosed, removals and hard floor changes")
        print("require acknowledgement, and harmless helpers stay quiet. The home leg stays quiet only for a")
        print("reformat that leaves the value unchanged, and requires acknowledgement when the home changes or is removed.\n")
        print("Vary it yourself: add a line to CASES with any file path and whether you expect it flagged, or a")
        print("line to _HOME_CASES with a manifest diff — then re-run.")
        return 0
    print("This run did NOT confirm the guard's behavior — these cases came out wrong:")
    for label, filename, expected, got in wrong:
        print(f"  - {label} ({filename}): expected tier={expected}, got tier={got}")
    print("That is a real signal worth investigating, not a pass. Your project was not touched.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
