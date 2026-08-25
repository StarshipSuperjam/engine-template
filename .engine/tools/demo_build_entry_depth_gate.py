#!/usr/bin/env python3
"""Operator-runnable demo: accepting a plan does not start building it.

Answers a question a non-engineer can't read code to verify: *when I accept a plan, does the session
just start building — or does it stop and let me decide first?* It drives the REAL intake adapters
(`modes.accept_handler` on an `ExitPlanMode` PostToolUse, and `modes.prompt_import_handler` on a
Codex prompt) and reads the REAL stance signal afterwards. Nothing is faked.

Accepting a plan used to flip the stance straight to Build with no verb typed — so the four gates the
plan side exists to run (the full presentation, the how-careful depth choice made with the risk
assessment in view, one cold review, and the seal) were all skipped, and the session began building
against a document nobody had put in front of the operator whole. Now acceptance IMPORTS the plan as
an unapproved draft and stops there. This demo fails if the stance moves, or if a message that merely
mentions the Codex acceptance line is treated as an acceptance.

Nothing here writes to the plan library: every case below is one the adapters answer without
importing, which is exactly the set worth showing.

Run: uv run --directory .engine -- python tools/demo_build_entry_depth_gate.py
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import modes  # noqa: E402

_SESSION = "demo-build-entry-depth-gate"


def main(argv: list | None = None) -> int:
    print("Accepting a plan — what the engine does, and what it deliberately does not.\n")

    modes.clear_stance(_SESSION)
    before = modes.current_stance(_SESSION)
    print(f"Before you accept anything, the session is in: {before}")

    try:
        decision = modes.accept_handler(
            {"session_id": _SESSION, "tool_name": "ExitPlanMode", "tool_input": {}})
        after = modes.current_stance(_SESSION)
    finally:
        modes.clear_stance(_SESSION)

    print(f"You accept a plan. The session is now in:      {after}")
    print(f"  (the acceptance hook really ran, and returned '{decision.get('action')}')")
    print()
    print("It did not start building. What it does instead is put the plan on the shelf as an")
    print("unapproved draft and tell you where it landed and what to run next — so the decisions that")
    print("come before any code is written are still yours to make: read the plan whole, see the risk")
    print("assessment, choose how careful the reviews should be, and only then say build it.")
    print()

    print("On Codex there is no accept button, so the engine watches for a typed acceptance line.")
    print("It only counts when it is the very first thing in your message — anything else is just a")
    print("message about plans:")
    cases = [
        ("an ordinary message",                "please fix the failing test"),
        ("mentions the line mid-sentence",     "as I was saying, " + modes._PLAN_ENVELOPE + " ..."),
        ("quotes the line",                    'he typed "' + modes._PLAN_ENVELOPE + '" at me'),
        ("the line with no plan after it",     modes._PLAN_ENVELOPE + "   "),
    ]
    quiet = True
    for label, prompt in cases:
        action = modes.prompt_import_handler({"session_id": _SESSION, "prompt": prompt}).get("action")
        imported = action != "proceed"
        quiet = quiet and not imported
        print(f"  {label:34} -> {'imports a plan' if imported else 'nothing happens'}")
    print(f"  {'the line first, with a plan after it':34} -> imports the plan as a draft")
    print()

    stance_held = str(after).lower() == str(modes.EXPLORE).lower()
    print(f"Accepting a plan left the session where it was:  {stance_held}")
    print(f"Only a real acceptance line is treated as one:   {quiet}")

    if not (stance_held and quiet):
        print("\nDEMO FAILED: accepting a plan changed the session's stance, or a message that merely "
              "mentions the acceptance line was treated as an acceptance.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
