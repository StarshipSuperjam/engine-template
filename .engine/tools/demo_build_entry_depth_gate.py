#!/usr/bin/env python3
"""Operator-runnable demo: entering Build now names the depth-choice consent gate.

Answers a question a non-engineer can't read code to verify: *when I accept a plan and the session flips
into building, is it actually told to show me the risk assessment and get my how-careful depth choice
BEFORE it changes anything — or does it just start building?* It drives the REAL plan-acceptance hook
(`modes.accept_handler` on an `ExitPlanMode` PostToolUse) and reads the REAL directive the session is
handed at Build entry. Nothing is faked — the handler and its injected text are the engine's own.

Before this change the directive named only "opening a draft pull request and planning the work" — the
depth choice was silently omitted, so a cold-booting session drifted into running every review at full
depth without ever offering the operator the lighter choice. This demo fails if that gate is not named.

Run: uv run --directory .engine -- python tools/demo_build_entry_depth_gate.py
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import modes  # noqa: E402


def _injected_directive() -> str:
    """The REAL directive injected when a plan is accepted (stance flips to Build). Uses a throwaway
    session id and clears its ephemeral stance signal afterward, so the demo leaves nothing behind."""
    sid = "demo-build-entry-depth-gate"
    try:
        out = modes.accept_handler({"session_id": sid, "tool_name": "ExitPlanMode"})
    finally:
        modes.clear_stance(sid)
    return out.get("context", "") if isinstance(out, dict) else ""


def main(argv: list | None = None) -> int:
    print("Build-entry consent gate — what the session is told the moment you accept a plan.\n")
    directive = _injected_directive()
    print("The directive the session receives at Build entry:")
    for line in (directive or "(none)").split(". "):
        print(f"   {line.strip()}")
    print()

    low = directive.lower()
    names_gate = ("risk assessment" in low) and ("depth" in low)
    print(f"Names the risk-assessment + depth-choice gate before any work: {names_gate}")
    print("(Before this change it named only 'open a draft pull request and plan the work' — the depth")
    print(" choice was omitted, so a fresh session drifted to running every review at full depth.)")

    if not names_gate:
        print("\nDEMO FAILED: the Build-entry directive does not name the risk-assessment depth-choice gate.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
