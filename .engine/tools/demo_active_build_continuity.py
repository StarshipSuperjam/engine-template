#!/usr/bin/env python3
"""Operator-runnable falsification of the quiet active-Build continuity surfaces."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".engine" / "tools"))

import build_coordinator  # noqa: E402
import session_economy  # noqa: E402


def _check(label: str, condition: bool) -> bool:
    print(f"{'PASS' if condition else 'FAIL'} — {label}")
    return condition


def main() -> int:
    required = (
        "A progress report is not a handoff",
        "continue the next actionable step",
        "do not schedule a self-wakeup",
    )
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    state = {
        "build": {"repository": "owner/repo", "pr": 17, "worktree": str(ROOT)},
        "plan": {"plan_id": "pln_demo", "profile": "normal", "bound_head": "a" * 40},
        "progress": {"current_item": "DEMO-01", "completed": []},
        "submission": "draft",
    }
    pointer = build_coordinator.reground_pointer(state)
    denial = session_economy.wakeup_denial("ScheduleWakeup") or ""
    close_source = (ROOT / ".engine" / "tools" / "close.py").read_text(encoding="utf-8")
    codex = json.loads((ROOT / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    codex_stop = [h["command"] for g in codex["hooks"]["Stop"] for h in g["hooks"]]

    results = [
        _check("Codex's re-injected root floor carries the continuity rule",
               all(text.lower() in agents.lower() for text in required)),
        _check("Claude's re-injected root floor carries the continuity rule",
               all(text.lower() in claude.lower() for text in required)),
        _check("Claude's compact Build pointer carries the same rule",
               all(text in pointer.lower() for text in (
                   "a progress report is not a handoff",
                   "continue the next planned step",
                   "do not schedule a self-wakeup",
               ))),
        _check("the recognized model self-wakeup action is denied toward real work",
               "continue the next actionable step" in denial.lower()),
        _check("the wakeup denial does not advertise operator switches",
               "ENGINE_SESSION_ECONOMY" not in denial),
        _check("the Stop owner contains no routine active-Build feedback evaluator",
               "build_continuity" not in close_source
               and len(codex_stop) == 1 and "tools/close.py" in codex_stop[0]),
    ]
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
