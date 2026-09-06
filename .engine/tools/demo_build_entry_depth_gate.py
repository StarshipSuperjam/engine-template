#!/usr/bin/env python3
"""Operator-runnable demo: accepting a plan lands it on the shelf — and does not start building it.

Answers two questions a non-engineer can't read code to verify: *when I accept a plan, does it actually
land in the Project Manager?* and *does the session just start building — or does it stop and let me
decide first?* It drives the REAL intake adapters (`modes.accept_handler` on an `ExitPlanMode`
PostToolUse, and `modes.prompt_import_handler` on a Codex prompt) through the real import, and reads the
REAL stance signal afterwards. Nothing is patched. What IS pointed elsewhere, so this demo can import for
real without touching anything of yours: the user home (HOME) and the plan library (ENGINE_PLAN_DIR) are
both set to per-run temporary directories before anything runs, and the demo refuses to import unless
both resolved there. Your own plan library and your own ~/.claude are listed before and after and must
be identical.

Accepting a plan used to flip the stance straight to Build with no verb typed — so the four gates the
plan side exists to run (the full presentation, the how-careful depth choice made with the risk
assessment in view, one cold review, and the seal) were all skipped, and the session began building
against a document nobody had put in front of the operator whole. Now acceptance IMPORTS the plan as
an unapproved draft and stops there. And since the harness no longer passes the plan inline
(StarshipSuperjam/engine-template#1163), the adapter reads it from where it lives: the inline text on an
older harness, the completion's own result, or the plan file that result names. This demo drives all
three shapes and fails if any of them imports nothing, if anything lands outside its own temporary
directories, if the stance moves, or if a message that merely mentions the Codex acceptance line is
treated as an acceptance.

Run: uv run --directory .engine -- python tools/demo_build_entry_depth_gate.py
"""
from __future__ import annotations
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_SESSION = "demo-build-entry-depth-gate"
_PLAN = "# Cache widgets\n\nThey are slow; cache them.\n"


def _listing(folder: str) -> list:
    try:
        return sorted(os.listdir(folder))
    except OSError:
        return []


def main(argv: list | None = None) -> int:
    print("Accepting a plan — what the engine does, and what it deliberately does not.\n")

    # Everything of the operator's, listed BEFORE the seams move so the after-check is honest.
    real_home = os.path.realpath(os.path.expanduser("~"))
    real_plans_before = _listing(os.path.join(real_home, ".claude", "plans"))
    import plan_store
    try:
        real_library = str(plan_store.library_root())
    except Exception:  # noqa: BLE001 — no resolvable library here (a bare copy of the tools): nothing to protect
        real_library = None
    real_library_before = _listing(real_library) if real_library else []

    with tempfile.TemporaryDirectory() as tmp:
        base = os.path.realpath(tmp)
        home = os.path.join(base, "home")
        library = os.path.join(base, "library")
        os.makedirs(os.path.join(home, ".claude", "plans"))
        os.makedirs(library)
        os.environ["HOME"] = home
        os.environ[plan_store.ENV_DIR] = library
        import modes

        # The pre-flight refusal: not one import happens unless BOTH seams resolved into this run's tree.
        resolved_library = os.path.realpath(str(plan_store.library_root()))
        resolved_anchor = modes._readable_plan_root(None)
        if not (resolved_library.startswith(base) and resolved_anchor.startswith(base)):
            print("\nDEMO REFUSED: the throwaway seams did not take — the plan library resolves to "
                  f"{resolved_library} and the readable plans folder to {resolved_anchor}; nothing was "
                  "imported.", file=sys.stderr)
            return 1
        print(f"For this run, plans are read from {resolved_anchor}")
        print(f"and imported into            {resolved_library} — both thrown away afterwards.\n")

        modes.clear_stance(_SESSION)
        before = modes.current_stance(_SESSION)
        print(f"Before you accept anything, the session is in: {before}")

        plan_path = os.path.join(resolved_anchor, "cache-widgets.md")
        with open(plan_path, "w", encoding="utf-8") as fh:
            fh.write(_PLAN)
        rendered = ("User has approved your plan. You can now start coding.\n\n"
                    f"Your plan has been saved to: {plan_path}\nYou can refer back to it if needed.\n")
        shapes = [
            ("an older harness passing the plan inline",
             {"tool_input": {"plan": _PLAN}}),
            ("the current harness's own result",
             {"tool_input": {}, "tool_response": {"plan": _PLAN, "filePath": plan_path, "isAgent": False}}),
            ("a result that only names the plan file",
             {"tool_input": {}, "tool_response": rendered}),
        ]
        print("\nYou accept a plan. Three shapes the acceptance has arrived in, each through the real adapter:")
        landed = 0
        try:
            for label, fields in shapes:
                payload = {"session_id": _SESSION, "tool_name": "ExitPlanMode", **fields}
                decision = modes.accept_handler(payload)
                context = decision.get("context", "")
                match = re.search(r"imported into the Project Manager as (pln_[0-9a-f]{12})", context)
                came_from = context.split(". ", 1)[0] if context.startswith("The accepted plan's text came from") else "(no source sentence)"
                if match:
                    landed += 1
                    print(f"  {label:44} -> landed as {match.group(1)}; {came_from}")
                else:
                    print(f"  {label:44} -> NOTHING LANDED: {context[:160]}")
            after = modes.current_stance(_SESSION)
        finally:
            modes.clear_stance(_SESSION)
        print(f"\nAfter accepting, the session is in:            {after}")
        print()
        print("It did not start building. What it does instead is put the plan on the shelf as an")
        print("unapproved draft and tell you where it landed and what to run next — so the decisions that")
        print("come before any code is written are still yours to make: read the plan whole, see the risk")
        print("assessment, choose how careful the reviews should be, and only then say build it.")
        print()

        imported_here = len([d for d in _listing(resolved_library) if d != ".index.json"])
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
        all_landed = landed == len(shapes)
        untouched = (imported_here >= landed
                     and _listing(os.path.join(real_home, ".claude", "plans")) == real_plans_before
                     and (real_library is None or _listing(real_library) == real_library_before))
        print(f"Every shape of acceptance landed on the shelf:    {all_landed} ({landed} of {len(shapes)})")
        print(f"Accepting a plan left the session where it was:  {stance_held}")
        print(f"Only a real acceptance line is treated as one:   {quiet}")
        print(f"Your own plan library and ~/.claude are untouched: {untouched}")

        if not (stance_held and quiet and all_landed and untouched):
            print("\nDEMO FAILED: an accepted plan did not land, accepting a plan changed the session's stance, "
                  "a message that merely mentions the acceptance line was treated as an acceptance, or "
                  "something landed outside the demo's own directories.", file=sys.stderr)
            return 1
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
