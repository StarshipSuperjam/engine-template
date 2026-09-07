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
import session_relay  # noqa: E402


def _check(label: str, condition: bool) -> bool:
    print(f"{'PASS' if condition else 'FAIL'} — {label}")
    return condition


# A plan slug that satisfies session_relay.PLAN_SELECTOR_PATTERN, so the advisory prints a runnable
# `state supersede` command that names the real Build. Edit this and the submission below and re-run:
# the before/after is variable, not hard-coded to one outcome.
_DEMO_SLUG = "fix-a-finished-build--edbeef"
_DEMO_PR = 1259


def _reground(submission: str) -> str:
    """The compaction re-grounding pointer for a Build in the given submission state (build_coordinator)."""
    state = {
        "build": {"repository": "owner/repo", "pr": _DEMO_PR, "worktree": str(ROOT)},
        "plan": {"plan_id": "pln_demo", "profile": "normal", "bound_head": "a" * 40},
        "progress": {"current_item": "DEMO-07", "completed": [{"id": "DEMO-06", "commit": "b" * 40}]},
        "submission": submission,
    }
    return build_coordinator.reground_pointer(state, _DEMO_SLUG)


def _boot_task_binding(submission: str) -> str:
    """The TASK_BINDING block boot's relay renders (session_relay) for the given submission state.

    A previously-submitted Build boots as a settled 'none' carrying the shared advisory; an in-flight
    Build boots as a verified binding with no advisory. This is the same renderer boot uses; the
    end-to-end path (gate + assemble_pack) is bound by the guard test in test_boot.py."""
    if submission == "ready":
        advisory = {"submission": "ready", "pr_ref": f"#{_DEMO_PR}", "plan_selector": _DEMO_SLUG}
        return session_relay._render_task_binding({"state": "none", "advisory": advisory})
    return session_relay._render_task_binding({
        "state": "verified",
        "binding": {
            "worktree": str(ROOT),
            "plan_ref": _DEMO_SLUG,
            "coordinator_snapshot": {"revision": 15},
            "pr_contract": {"state": "draft", "pr_ref": f"#{_DEMO_PR}"},
        },
    })


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

    # ---- A previously-submitted Build is surfaced as a resume aid, not live work ----------------
    # The #1255 fix: a finished Build (submission=='ready') must be flagged possibly-stale with the
    # three-case new-versus-resume steer at BOTH surfaces a session grounds through — boot's relay and
    # post-compaction re-grounding — while an in-flight Build keeps its live-work framing. The steer is
    # ONE definition (session_relay.advisory_lines); here we render both surfaces in both states and
    # show that the block boot carries is byte-for-byte the block compaction carries.
    boot_before, boot_after = _boot_task_binding("draft"), _boot_task_binding("ready")
    compact_before, compact_after = _reground("draft"), _reground("ready")
    shared_advisory = "\n".join(session_relay.advisory_lines(
        {"submission": "ready", "pr_ref": f"#{_DEMO_PR}", "plan_selector": _DEMO_SLUG}))
    for title, before, after in (
        ("BOOT relay — TASK_BINDING", boot_before, boot_after),
        ("COMPACTION — reground pointer", compact_before, compact_after),
    ):
        print(f"\n----- {title} — BEFORE (in-flight Build) -----\n{before}")
        print(f"\n----- {title} — AFTER (previously-submitted Build) -----\n{after}")
    print()

    results += [
        _check("boot's relay flags a previously-submitted Build possibly-stale with the three-case steer",
               session_relay.ADVISORY_SENTENCE in boot_after
               and "continue THIS Build" in boot_after
               and "start a DIFFERENT Build" in boot_after
               and f"state supersede --plan {_DEMO_SLUG}" in boot_after),
        _check("boot leaves an in-flight Build as live work, with no previously-submitted advisory",
               "state=verified" in boot_before
               and session_relay.ADVISORY_SENTENCE not in boot_before),
        _check("compaction flags a previously-submitted Build with the same steer, not the live-work tail",
               session_relay.ADVISORY_SENTENCE in compact_after
               and "continue the next planned step" not in compact_after),
        _check("compaction keeps the live-work tail for an in-flight Build, with no advisory",
               "continue the next planned step" in compact_before
               and session_relay.ADVISORY_SENTENCE not in compact_before),
        _check("the two surfaces carry the ONE shared advisory block byte-for-byte (cannot drift)",
               shared_advisory in boot_after and shared_advisory in compact_after),
    ]
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
