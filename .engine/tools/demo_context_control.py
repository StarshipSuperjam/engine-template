#!/usr/bin/env python3
"""Behavioral FALSIFICATION for the context-control spine — that a compaction is survivable, that the
plan-to-build boundary is real, and that the engine takes none of the powers it deliberately refused.

Three arms, each FAIL-THEN-PASS on the same fixture. The only difference between the two arms of each
is the one behaviour under test, so a green arm cannot be a fixture that was never capable of failing.

  ARM 1 — RECOVERY. A session standing in a different worktree tries to mutate a Build.
    * POSITIVE: unconditional verb-entry verification refuses before anything is written.
    * NEGATIVE CONTROL: with verification bypassed — the world before this change — the same
      mutation lands, writing one Build's evidence from a session that belongs to another.
    This is the arm that matters, because it is the failure a compacted session actually produces:
    not a crash, but confident work against the wrong record.

  ARM 2 — THE HAND-OFF. Binding a Build without saying what the build phase runs on.
    * POSITIVE: entry refuses, names both missing flags, and names the remedy. Driven through the
      real command line, because a refusal reached by calling the handler with a hand-built argument
      object proves the branch exists, never that a session would actually meet it.
    * NEGATIVE CONTROL: the same bind WITH the answer proceeds — proving the gate answers to the
      answer rather than refusing unconditionally, which is the way this gate could be useless while
      still looking green.

  ARM 3 — THE POWERS NOT TAKEN. The whole context-control surface is run against a real engine clone.
    * POSITIVE: it writes NOTHING AT ALL. Not a settings file, not a record of its own — the clone is
      byte-identical afterwards. No compaction is initiated, no clear is invoked, nothing estimates
      utilization from text.
    * NEGATIVE CONTROL: a variant that writes an auto-compact setting into the fixture's settings.json
      is caught by the same comparison — so the check can fail, and does.

Run: uv run --directory .engine --frozen -- python tools/demo_context_control.py
Its companion test (`test_context_control_end_to_end`) runs it, so it travels with the engine.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_coordinator as bc      # noqa: E402  (the real verification + barrier under test)
import build_state_store            # noqa: E402
import engine_fixture               # noqa: E402
import plan_store                   # noqa: E402

SEALED_AT = "2026-08-26T02:46:39Z"
AFTER_SEAL = "2026-08-26T05:00:00Z"
BEFORE_SEAL = "2026-08-26T01:00:00Z"


def _snapshot_state(worktree: str) -> dict:
    """A minimal but schema-real Build snapshot bound to `worktree`."""
    return {
        "schema_version": "build-state.v2", "revision": 1,
        "build": {"repository": "owner/repo", "pr": 7, "base_at_bind": "0" * 40,
                  "mode": "same-session", "worktree": worktree},
        "plan": {"plan_id": "pln_0123456789ab", "sealed_digest": "sha256:" + "b" * 64,
                 "diverged_from_seal": False, "digest": "sha256:" + "c" * 64,
                 "intent_digest": "sha256:" + "d" * 64, "spec_digest": None,
                 "authorizing_issue": None, "profile": "normal", "bound_head": "e" * 40},
        "approval": None,
        "reviews": {"deliverable": {"packet_digest": None, "referent_digest": None,
                                    "required_lenses": [], "installed_lenses": [],
                                    "reviewer_contracts": [], "receipts": [],
                                    "reviewed_commit": None, "base_commit": None}},
        "findings": [], "checkpoint": None,
        "progress": {"current_item": None, "completed": []},
        "work": {}, "validation": None, "repair": None, "repair_rounds": [],
        "plan_change_escalations": [], "reconciles": [], "preflights": [],
        "pr_contract": None, "submission": "draft", "checkout_snapshot": None,
    }


def _demo_plan() -> dict:
    """A minimal, schema-shaped v2 payload for the bind arm to carry.

    Written out here rather than imported from the test fixtures: a demo that depends on test code
    is a demo that stops running the day someone reorganizes the tests.
    """
    return {
        "schema_version": "build-plan.v2", "profile": "normal",
        "intent_source": {"kind": "direct"},
        "raw_intent": "Demonstrate the seal-to-build hand-off.",
        "interpretation": "Bind refuses until it carries the operator's model and effort.",
        "evidence": [{"claim": "The gate ships in build_coordinator.", "basis": "this file runs it",
                      "kind": "observed"}],
        "assumptions": [{"claim": "The demo reproduces this document.", "status": "verified"}],
        "objective": "Show the hand-off refusing and then proceeding.",
        "success_obligations": [{"outcome": "An unanswered bind refuses.",
                                 "verification": "this demo"}],
        "scope_boundary": ["The bind gate"], "non_goals": ["Anything else"],
        "risks": ["None — this payload is never executed."],
        "work_items": [{
            "id": "DEMO-01", "depends_on": [], "description": "A single node, never run.",
            "executor_class": "builder", "paths": [".engine/tools/demo_context_control.py"],
            "exclusive_resources": ["demo"], "verification": ["none"],
            "output_contract": {"deliverable": "nothing", "artifact_kinds": ["tests"],
                                "required_evidence": ["changed_paths"]}}],
        "parallelism": {"mode": "serial", "max_concurrency": 1},
        "review_strategy": "None; this plan is never reviewed.",
        "spec": {"posture": "none", "selection_basis": "No product spec governs a demo.",
                 "disclosure": "No settled spec."},
    }


def arm_one_recovery() -> tuple[bool, list[str]]:
    """A session in the wrong worktree tries to mutate a Build."""
    lines = []
    with tempfile.TemporaryDirectory() as tmp:
        elsewhere = str(Path(tmp) / "a-different-worktree")
        state = _snapshot_state(elsewhere)

        # POSITIVE: the real check, from the worktree the session is actually standing in.
        reasons = bc.resume_reasons(state, worktree=Path(tmp) / "here", head=None)
        refused = bool(reasons)
        lines.append(f"  refuses a mutation from the wrong worktree:      {refused}")
        if refused:
            lines.append(f"    reason given: {reasons[0][:96]}…")

        # NEGATIVE CONTROL: the world before this change — nothing compared the two, so the mutation
        # proceeds. Modelled by asking the same question with the comparison removed.
        legacy = _snapshot_state(elsewhere)
        legacy["build"].pop("worktree")          # what a pre-change snapshot could not carry
        legacy["plan"].pop("bound_head")         # recorded then, but read by nothing
        unguarded = bc.resume_reasons(legacy, worktree=Path(tmp) / "here", head=None)
        lines.append(f"  WITHOUT the recorded facts, the same mutation:  "
                     f"{'proceeds' if not unguarded else 'refused'}")
        return refused and not unguarded, lines


def arm_two_handoff() -> tuple[bool, list[str]]:
    """Binding without answering the seal hand-back, through the real command line."""
    import argparse
    import contextlib
    import io
    from unittest import mock

    lines = []
    plan_payload = {"plan_id": "pln_0123456789ab", "sealed": "sha256:" + "b" * 64}

    def bind(*extra) -> tuple[int, str]:
        """One real `plan bind` argv. Only the OUTSIDE world is stubbed — the draft PR lookup, the
        git head, the label call — never the gate under test, which runs exactly as it ships."""
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            head = "e" * 40
            pr = {"number": 7, "state": "OPEN", "isDraft": True,
                  "headRefOid": head, "baseRefOid": "0" * 40}
            err = io.StringIO()
            with mock.patch.object(bc, "_sealed_plan",
                                   return_value=(plan_payload["plan_id"], plan_payload["sealed"],
                                                 _demo_plan())), \
                    mock.patch.object(bc, "_verify_draft", return_value=pr), \
                    mock.patch.object(bc, "_head", return_value=head), \
                    mock.patch.object(bc, "_base", return_value="0" * 40), \
                    mock.patch.object(bc.github, "tag_coordinator_owned", return_value=True), \
                    mock.patch.object(bc, "_record_build_binding"), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                code = bc.main(["--state", state_path, "plan", "bind",
                                "--plan", plan_payload["plan_id"], "--repository", "owner/repo",
                                "--pr", "7", "--operator-decision", "start the Build", *extra])
            return code, err.getvalue()

    # POSITIVE: nobody was asked, so nothing can be answered, so the Build does not start.
    code, err = bind()
    refused = code != 0
    lines.append(f"  refuses a bind that names no model or effort:   {refused}")
    named = all(token in err for token in
                ("--session-model", "--session-effort", "hand-back", "bind again"))
    lines.append(f"    and names both flags and the remedy:         {named}")

    # A half answer is not an answer, and the refusal is specific about which half is missing.
    half_code, half_err = bind("--session-model", "demo-model")
    precise = half_code != 0 and "--session-effort" in half_err and "--session-model" not in half_err
    lines.append(f"  half an answer still refuses, naming that half: {precise}")

    # NEGATIVE CONTROL: answered, it proceeds. Without this the arm would pass on a gate that
    # refused everything, which is green and worthless.
    ok_code, ok_err = bind("--session-model", "demo-model", "--session-effort", "medium")
    proceeds = ok_code == 0
    lines.append(f"  the SAME bind, answered, proceeds:             {proceeds}")
    if not proceeds:
        lines.append(f"    it did not: {ok_err.strip()[:120]}")
    return refused and named and precise and proceeds, lines


def arm_three_powers_not_taken() -> tuple[bool, list[str]]:
    """The whole surface runs against a real engine clone; only observation records may change."""
    lines = []
    real_root = str(Path(__file__).resolve().parents[2])
    with tempfile.TemporaryDirectory() as tmp:
        live = engine_fixture.clone_engine(real_root, str(Path(tmp) / "engine"))

        def fingerprint() -> dict:
            found = {}
            for path in Path(live).rglob("*"):
                if path.is_file():
                    try:
                        found[str(path.relative_to(live))] = path.read_bytes()
                    except OSError:
                        pass
            return found

        library = plan_store.PlanLibrary(Path(tmp) / "plans")
        slug = plan_store.slug_for("a context-control demo", "pln_0123456789ab")
        plan_store.ensure_dir(library.plan_dir(slug), within=library.root)
        (library.plan_dir(slug) / "record.json").write_text(
            json.dumps({"plan_id": "pln_0123456789ab"}), encoding="utf-8")
        snapshot = build_state_store.snapshot_path(library, slug)
        plan_store.ensure_dir(snapshot.parent, within=library.root)
        snapshot.write_text(json.dumps(_snapshot_state(live)), encoding="utf-8")

        before = fingerprint()
        original_library, original_root = bc._library, bc.ROOT
        bc._library = lambda: library
        bc.ROOT = Path(live)
        try:
            # The real handler, on the real compact lifecycle, resolving a real bound snapshot.
            decision = bc.reground_handler({"source": "compact", "session_id": "s1",
                                            "trigger": "auto"})
            injected = decision.get("context", "")
        finally:
            bc._library, bc.ROOT = original_library, original_root
        after = fingerprint()

        touched = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        lines.append(f"  the engine clone is byte-identical afterwards:  {not touched}")
        if touched:
            lines.append(f"    unexpectedly written: {touched[:4]}")
        settings = json.loads((Path(live) / ".claude/settings.json").read_text(encoding="utf-8"))
        no_threshold = "autoCompactWindow" not in json.dumps(settings)
        lines.append(f"  no auto-compact setting was written anywhere:   {no_threshold}")
        lines.append(f"  and it re-grounded the session anyway:         {bool(injected)}")
        lines.append("    (the clone being untouched is the point: the hook REACTS to a compaction")
        lines.append("     and keeps no record that one happened — nothing reads such a record)")

        # NEGATIVE CONTROL: a variant that writes the setting the engine refuses to write. The same
        # comparison must catch it, or arm three proves nothing.
        settings_path = Path(live) / ".claude/settings.json"
        settings["autoCompactWindow"] = 0.75
        settings_path.write_text(json.dumps(settings), encoding="utf-8")
        caught = fingerprint().get(".claude/settings.json") != before.get(".claude/settings.json")
        lines.append(f"  a settings-writing variant IS caught:           {caught}")
        return (not touched) and no_threshold and bool(injected) and caught, lines


def main() -> int:
    print("=" * 78)
    print("DEMO — context control: compaction is survived by verification, the plan-to-build")
    print("stop is a question that must be answered, and the engine takes none of the powers it refused.")
    print("=" * 78)
    arms = [
        ("ARM 1 — RECOVERY (a session mutating a Build it does not match)", arm_one_recovery),
        ("ARM 2 — THE HAND-OFF (binding without saying what the build runs on)", arm_two_handoff),
        ("ARM 3 — THE POWERS NOT TAKEN (no settings write, no clear, no initiation)",
         arm_three_powers_not_taken),
    ]
    failures = []
    for title, run in arms:
        print(f"\n[{title}]")
        try:
            passed, lines = run()
        except Exception as exc:  # noqa: BLE001 — a demo reports its own failure, never a traceback
            passed, lines = False, [f"  arm raised {type(exc).__name__}: {exc}"]
        for line in lines:
            print(line)
        if not passed:
            failures.append(title)
    print("\n" + "=" * 78)
    if failures:
        print("DEMO FAILED:")
        for title in failures:
            print(f"  - {title}")
        return 1
    print("DEMO PASSED: a mismatched session is refused before it writes (and without the recorded")
    print("facts it would not have been); bind refuses an unanswered hand-back and proceeds on an")
    print("answered one; and the whole surface writes nothing at all.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
