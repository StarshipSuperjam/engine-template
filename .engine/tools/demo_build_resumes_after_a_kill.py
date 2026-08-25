#!/usr/bin/env python3
"""Behavioral FALSIFICATION that a Build survives being killed: its execution state is durable, and a cold
session standing in the worktree finds it again without having remembered anything.

THE FAILURE THIS CLOSES. The Build snapshot used to live in the OS temporary directory, keyed to the session
that made it, and a class refused to construct anywhere else — the code embodiment of "the Build's state is
never a durable leg". It was observed to vanish across a reboot, taking a session's planning with it
(StarshipSuperjam/engine-template#1012). When that happened after review had started, EVERY coordinator
record went with it: the plan binding, the approval, the review receipts, the finding dispositions. Recovery
meant reconstructing the snapshot by hand. The snapshot now lives in the plan library beside the sealed plan
that bound it, written owner-only through the same lock and compare-and-swap the library already uses.

FAIL-THEN-PASS on one fixture, and the two arms differ only in WHERE the snapshot was written:
  * POSITIVE (durable): a Build is bound, work is recorded, the session is killed — modelled honestly, by
    throwing away every handle to it and standing up a NEW store from nothing but the worktree path. The
    Build is found and every record is intact.
  * NEGATIVE CONTROL (the old shape): the same snapshot written into the OS temporary directory, which is
    then cleared the way a reboot clears it. The cold lookup finds nothing, and the records are gone.

Durable is still not authoritative, and this demo does not blur that: the snapshot is a record of execution,
never of what was agreed. The sealed plan remains the authority, and the cold lookup asserts the recovered
snapshot still names the plan it was bound to rather than standing in for it.

Run:  uv run --directory .engine --frozen -- python tools/demo_build_resumes_after_a_kill.py
Its companion test (`test_build_state_store.TheKillAndResumeDemo`) runs it, so it travels with the engine as
a permanent guard in every generated repository.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_coordinator_core as core   # noqa: E402
import build_state_store                # noqa: E402  (the durable store under test)
import plan_store                       # noqa: E402

_SCHEMA = (Path(__file__).resolve().parents[1] / "schemas" / "build-state.v2.json")


def _snapshot(worktree: str) -> dict:
    """A Build snapshot with real records in it — a binding, an approval, and a review receipt. Anything
    less would let the demo pass while proving only that an empty file round-trips."""
    return {
        "schema_version": "build-state.v2", "revision": 1,
        "build": {"repository": "o/r", "pr": 4242, "base_at_bind": "0" * 40,
                  "mode": "same-session", "worktree": worktree},
        "plan": {"plan_id": "pln_0123456789ab", "sealed_digest": "sha256:" + "e" * 64,
                 "diverged_from_seal": False, "digest": "sha256:" + "d" * 64,
                 "intent_digest": "sha256:" + "c" * 64, "spec_digest": None,
                 "authorizing_issue": None, "profile": "normal", "bound_head": "a" * 40},
        "approval": {"plan_digest": "sha256:" + "d" * 64, "spec_digest": None, "depth": "thorough"},
        "reviews": {"deliverable": {
            "packet_digest": "sha256:" + "1" * 64, "referent_digest": "sha256:" + "2" * 64,
            "required_lenses": ["usability"], "installed_lenses": ["usability"],
            "reviewer_contracts": [], "receipts": [
                {"lens": "usability", "packet_digest": "sha256:" + "1" * 64, "commit": "a" * 40,
                 "finding_ids": [], "code_execution": "none", "delivered_effort": "high"}],
            "reviewed_commit": "a" * 40, "base_commit": "0" * 40}},
        "findings": [], "checkpoint": None,
        "progress": {"current_item": "N1", "completed": []},
        "validation": None, "repair": None, "repair_rounds": [], "plan_change_escalations": [],
        "reconciles": [], "preflights": [], "pr_contract": None, "submission": "draft",
        "checkout_snapshot": None, "work": {},
    }


def _records_intact(state: dict) -> bool:
    return bool(state and state.get("approval") and state["reviews"]["deliverable"]["receipts"])


def main() -> int:
    failures = []
    print("=" * 78)
    print("DEMO — a killed Build resumes: its execution state is durable, and a cold session standing in")
    print("the worktree finds it again from the worktree alone.")
    print("=" * 78)

    root = tempfile.mkdtemp(prefix="build-resume-demo-")
    try:
        library = plan_store.PlanLibrary(os.path.join(root, "plans"))
        worktree = os.path.join(root, "worktree")
        os.makedirs(worktree)

        # The snapshot lives BESIDE the sealed plan that bound it, so the plan folder has to exist —
        # that adjacency is the whole addressing scheme, not an incidental detail of the fixture.
        slug = plan_store.slug_for("a sealed plan", "pln_0123456789ab")
        plan_store.ensure_dir(library.plan_dir(slug), within=library.root)
        (library.plan_dir(slug) / "record.json").write_text(
            json.dumps({"plan_id": "pln_0123456789ab"}), encoding="utf-8")

        # ---- POSITIVE: bind, record, kill, resume -------------------------------------------------
        path = build_state_store.snapshot_path(library, slug)
        store = build_state_store.DurableBuildStore(path, _SCHEMA, library_root=library.root)
        store.create(_snapshot(worktree))
        del store                                       # every handle to the session is gone

        # A COLD session: it has nothing but the worktree it is standing in.
        found = build_state_store.bound_snapshots(worktree, library=library)
        resumed = None
        if found:
            resumed = build_state_store.DurableBuildStore(found[0][1], _SCHEMA,
                                                          library_root=library.root).read()
        print("\n[POSITIVE — the durable snapshot, after the session is thrown away]")
        print(f"  a cold lookup from the worktree finds it:     {bool(found)}")
        print(f"  the approval and review receipt survived:     {_records_intact(resumed)}")
        if resumed:
            print(f"  it still names the plan it was bound to:      "
                  f"{resumed['plan']['plan_id']} (sealed {resumed['plan']['sealed_digest'][:19]}…)")
        if not found:
            failures.append("POSITIVE: a cold session standing in the worktree could not find the Build")
        if not _records_intact(resumed):
            failures.append("POSITIVE: the Build was found but its records did not survive")
        if resumed and resumed["plan"]["plan_id"] != "pln_0123456789ab":
            failures.append("POSITIVE: the recovered snapshot does not name the plan that bound it — "
                            "durable state is a record of execution, never a substitute for the plan")

        # ---- NEGATIVE CONTROL: the same snapshot in OS temp, cleared the way a reboot clears it ----
        os_temp = tempfile.mkdtemp(prefix="build-resume-demo-ostemp-")
        legacy = os.path.join(os_temp, "build-state.json")
        with open(legacy, "w", encoding="utf-8") as fh:
            json.dump(_snapshot(worktree), fh)
        shutil.rmtree(os_temp, ignore_errors=True)      # the reboot
        survived = os.path.exists(legacy)
        cold_finds_it = bool(build_state_store.bound_snapshots(
            worktree, library=plan_store.PlanLibrary(os_temp)))
        print("\n[NEGATIVE CONTROL — the old shape: OS temp, cleared by a reboot]")
        print(f"  the snapshot survived the reboot:             {survived}")
        print(f"  a cold lookup from the worktree finds it:     {cold_finds_it}")
        if survived or cold_finds_it:
            failures.append("NEGATIVE CONTROL did not reproduce the loss — the demo is not exercising the "
                            "failure it claims to close")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print("\n" + "=" * 78)
    if failures:
        print("DEMO FAILED:")
        for f in failures:
            print(f"  - {f}")
        print("=" * 78)
        return 1
    print("DEMO PASSED — the Build outlives the session that started it, and the plan stays the authority.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
