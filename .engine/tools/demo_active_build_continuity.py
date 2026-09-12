#!/usr/bin/env python3
"""Operator-runnable falsification of the quiet active-Build continuity surfaces."""
from __future__ import annotations

import json
import argparse
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".engine" / "tools"))

import boot  # noqa: E402
import build_coordinator  # noqa: E402
import session_economy  # noqa: E402
import session_relay  # noqa: E402


def _check(label: str, condition: bool) -> bool:
    print(f"{'PASS' if condition else 'FAIL'} — {label}")
    return condition


def presentation_demo(withhold_seal: bool = False) -> int:
    """Exercise the real lifecycle in a disposable library using fixture review evidence.

    The permanent ConsentGates tests cover these transitions. This existing
    construction demo adds an operator-readable way to vary the seal decision.
    """
    from test_plan_lifecycle import _Ceremony
    import plan_lifecycle
    fixture = _Ceremony()
    fixture.setUp()
    try:
        print("Temporary plan library; fixture review evidence, real lifecycle commands. No GitHub writes.")
        slug = fixture.reviewed()
        approved = fixture.lib.read_record(slug)["consent"]
        code, _, err = fixture.run_command("present-findings", slug)
        record = fixture.lib.read_record(slug)
        results = [
            _check("showing findings succeeds without asking for acknowledgment", code == 0),
            _check("presentation adds no operator decision", record["consent"] == approved),
            _check("the notification records the current findings", plan_lifecycle.presentation_current(record)),
        ]
        if err:
            print(err)
        code, _, _ = fixture.run_command("seal", slug)
        results.append(_check("without a seal decision, the plan stays unsealed",
                              code == 2 and fixture.lib.read_record(slug).get("seal") is None))
        if not withhold_seal:
            code, _, _ = fixture.run_command("seal", slug, "--operator-decided")
            record = fixture.lib.read_record(slug)
            results.append(_check("with a seal decision, the plan seals", code == 0))
            results.append(_check("the flow needs two decisions instead of the historical three",
                                  [c["gate"] for c in record["consent"]] == ["approve", "seal"]))
            results.append(_check("sealing did not start a Build", not record.get("build_binding")))
        return 0 if all(results) else 1
    finally:
        fixture.doCleanups()


# A plan slug that satisfies session_relay.PLAN_SELECTOR_PATTERN, so the advisory prints a runnable
# `state supersede` command that names the real Build. Edit this and the submission below and re-run:
# the before/after is variable, not hard-coded to one outcome.
_DEMO_SLUG = "fix-a-stale-binding--edbeef"
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
    """The TASK_BINDING block a booting session actually receives, produced by boot's REAL resolver
    (`resolve_task_binding` -> `_previously_submitted_advisory`) and its REAL renderer — not a
    hand-built dict handed straight to the renderer.

    A previously-submitted Build boots as a settled 'none' carrying the shared advisory; an in-flight
    Build boots as a verified binding with no advisory. Everything the #1255 fix owns is exercised for
    real: the none-vs-verified branch, the advisory gate (submission/worktree/repository/slug-grammar
    checks reading the SNAPSHOT'S OWN fields), the pr-ref formatting, and the render. Only the three
    external I/O seams a single-file, portable demo cannot stand up for real are stubbed, and each is
    named where it is stubbed: the expired live session-binding (the post-submission precondition that
    settles the ladder on 'none'), the plan-library scan, and the git origin lookup. The full
    assemble_pack wiring around this is separately bound by the guard tests in test_boot.py."""
    wt = str(ROOT)
    if submission == "ready":
        # A real ready snapshot; its OWN recorded worktree/repository/slug are what the gate reads.
        snapshot = build_coordinator._initial_state(
            "owner/repo", _DEMO_PR, "0" * 40, "pln_demo", "sha256:" + "e" * 64,
            {"raw_intent": "demo", "profile": "normal"}, None)
        snapshot["build"]["worktree"] = wt
        snapshot["submission"] = "ready"
        with tempfile.NamedTemporaryFile(prefix=".engine-demo-locator-", suffix=".json") as locator, \
                mock.patch.object(boot, "_resolve_task_binding_unguarded",  # live binding has expired
                                  return_value={"state": "none"}), \
                mock.patch.object(boot, "_binding_locator_path", return_value=locator.name), \
                mock.patch.object(boot, "_bound_snapshot",  # the library scan, stood up as one real snapshot
                                  return_value=(_DEMO_SLUG, snapshot)), \
                mock.patch.object(boot.repo_identity, "origin_slug", return_value="owner/repo"):
            result = boot.resolve_task_binding(wt)
        return session_relay._render_task_binding(result)
    # In-flight: boot's real wrapper passes a verified binding through untouched, adding no advisory.
    verified = {"state": "verified", "binding": {
        "worktree": wt, "plan_ref": _DEMO_SLUG, "coordinator_snapshot": {"revision": 15},
        "pr_contract": {"state": "open", "pr_ref": f"#{_DEMO_PR}"}}}
    with mock.patch.object(boot, "_resolve_task_binding_unguarded", return_value=verified):
        result = boot.resolve_task_binding(wt)
    return session_relay._render_task_binding(result)


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
    # The #1255 fix: a previously-submitted Build (submission=='ready') must be flagged possibly-stale
    # with the three-case new-versus-resume steer at BOTH surfaces a session grounds through — boot's
    # relay and post-compaction re-grounding — while an in-flight Build keeps its live-work framing. The
    # steer is ONE definition (session_relay.advisory_lines); here we render both surfaces in both states
    # and show that the block boot carries is byte-for-byte the block compaction carries.
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
        # #1255 us1, in full: the ready message must LEAD with the resume-versus-different DECISION, and
        # the record's authority + status-inspection ritual must come only AFTER the advisory, gated on
        # actually resuming — never as an unconditional "inspect this record before changing anything".
        # Reintroducing the old opening, or the unconditional status directive, fails THIS check.
        _check("compaction's ready message leads with the decision, gating authority/status on resume",
               "DECIDE whether to resume THIS Build or" in compact_after
               and "Only if you decide to RESUME this Build" in compact_after
               and "before changing anything" not in compact_after
               and "while a Build was running" not in compact_after
               and (compact_after.index("DECIDE whether to resume THIS Build")
                    < compact_after.index(session_relay.ADVISORY_SENTENCE)
                    < compact_after.index("Only if you decide to RESUME this Build"))),
        _check("compaction keeps the live-work tail for an in-flight Build, with no advisory",
               "continue the next planned step" in compact_before
               and session_relay.ADVISORY_SENTENCE not in compact_before),
        _check("the two surfaces carry the ONE shared advisory block byte-for-byte (cannot drift)",
               shared_advisory in boot_after and shared_advisory in compact_after),
    ]
    return 0 if all(results) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-presentation", action="store_true", help="Demonstrate notification without acknowledgment.")
    parser.add_argument("--withhold-seal", action="store_true", help="Leave the demonstrated plan awaiting its seal decision.")
    args = parser.parse_args()
    if args.withhold_seal and not args.plan_presentation:
        parser.error("--withhold-seal requires --plan-presentation")
    raise SystemExit(presentation_demo(args.withhold_seal) if args.plan_presentation else main())
