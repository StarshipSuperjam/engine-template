#!/usr/bin/env python3
"""Operator-runnable falsification of the Program Manager lanes surface, end to end.

It drives the whole surface in-process through program_manager.main — propose reads the evidence and
recommends a split; the operator decides DIFFERENTLY and records it; `program show` renders the decided
split truthfully; a laned child is superseded and a newcomer added; `program show` marks the dead member
and lists the newcomer; and amend-mode propose places the newcomer without touching the recorded lanes.
Advisory throughout: nothing here dispatches, selects, or gates a Build.
"""
from __future__ import annotations

import contextlib
import io
import tempfile
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".engine" / "tools"))

import plan_program  # noqa: E402
import plan_store  # noqa: E402
import program_manager  # noqa: E402


def _check(label: str, condition: bool) -> bool:
    print(f"{'PASS' if condition else 'FAIL'} — {label}")
    return condition


def _document(plan_id: str, title: str, paths: list, predecessor: str | None, program_id: str) -> dict:
    """A minimal, valid engine-plan.v1 document whose one work item declares `paths` as its territory."""
    program = {"program_id": program_id}
    if predecessor:
        program["predecessor_plan_id"] = predecessor
    return {
        "schema_version": "engine-plan.v1", "plan_id": plan_id, "title": title, "revision": 1,
        "created_at": "2026-08-31T00:00:00Z", "revised_at": "2026-08-31T00:00:00Z",
        "revision_note": "Revision 1.",
        "intent": {"raw": "ride in parallel", "interpretation": "By territory.",
                   "source": {"kind": "direct"}},
        "deliberation": {"problem_frame": "Concurrency is decided by hand.",
                         "case_against": "One more thing to keep true.",
                         "alternatives": [{"option": "Keep it in chat", "disposition": "rejected",
                                           "reason": "It dies with the conversation."}],
                         "failure_modes": ["The split lies after the chain moves."],
                         "unresolved_decisions": []},
        "program": program,
        "build_plan": {
            "schema_version": "build-plan.v2", "profile": "normal",
            "intent_source": {"kind": "direct"}, "raw_intent": "ride in parallel",
            "interpretation": "By territory.", "objective": "Exercise lanes.",
            "success_obligations": [{"outcome": "it runs", "verification": "this demo"}],
            "evidence": [{"claim": "it stores", "basis": "this demo", "kind": "observed"}],
            "assumptions": [], "scope_boundary": ["one node"], "non_goals": ["everything else"],
            "risks": ["none"], "review_strategy": "this demo",
            "spec": {"posture": "none", "selection_basis": "a demo", "disclosure": "a demo"},
            "parallelism": {"mode": "serial", "max_concurrency": 1},
            "work_items": [{"id": "w", "description": title, "paths": list(paths), "depends_on": [],
                            "exclusive_resources": [], "executor_class": "builder",
                            "verification": ["it runs"],
                            "output_contract": {"deliverable": "x", "artifact_kinds": ["code"],
                                                "required_evidence": ["a test"]}}],
        },
    }


def main() -> int:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name) / "plans"
    library = plan_store.PlanLibrary(root)
    programs = plan_program.ProgramLibrary(library)

    def run(*argv) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = program_manager.main(["--library", str(root), *argv])
        return code, out.getvalue() + err.getvalue()

    slug = programs.create("Lanes demo", "Ride the children in parallel where territory allows.")
    program_id = programs.read(slug)["program_id"]
    # A(x.py) -> B(y.py, disjoint) -> C(x.py, conflicts A): territory groups A and C, B rides apart.
    chain = [("pln_a00000000001", ["x.py"], None), ("pln_b00000000002", ["y.py"], "pln_a00000000001"),
             ("pln_c00000000003", ["x.py"], "pln_b00000000002")]
    for plan_id, paths, predecessor in chain:
        library.create(_document(plan_id, plan_id[-3:], paths, predecessor, program_id))
        programs.add_child(slug, plan_id, predecessor=predecessor)

    results = []

    _, proposed = run("program", "lanes", "propose", program_id)
    results.append(_check("propose recommends grouping the shared-territory children and splitting B off",
                          "## Lanes" in proposed and "lane-1" in proposed and "lane-2" in proposed
                          and "program lanes set" in proposed))

    # The operator decides DIFFERENTLY from the recommendation: everything in one lane.
    code, _ = run("program", "lanes", "set", program_id,
                  "--lane", "solo=pln_a00000000001,pln_b00000000002,pln_c00000000003",
                  "--reason", "run them one at a time this cycle")
    results.append(_check("the operator's own split records cleanly, even against the recommendation",
                          code == 0
                          and programs.read(slug)["lanes"]["lanes"]
                          == [{"name": "solo", "children": ["pln_a00000000001", "pln_b00000000002",
                                                            "pln_c00000000003"]}]))

    _, shown = run("program", "show", program_id)
    results.append(_check("show renders the decided split with its reason",
                          "## Lanes" in shown and "run them one at a time this cycle" in shown))

    # Supersede a laned child (retire it, mark it), and add a newcomer after the split.
    library.update_record(library.resolve("pln_a00000000001"), lambda cur: cur.__setitem__(
        "closure", {"state": "retired", "at": "2026-08-31T00:00:00Z", "reason": "superseded"}))
    record = programs.read(slug)
    for child in record["children"]:
        if child["plan_id"] == "pln_a00000000001":
            child["superseded_by"] = "pln_c00000000003"
    programs._write(slug, record)
    library.create(_document("pln_d00000000004", "004", ["z.py"], "pln_c00000000003", program_id))
    programs.add_child(slug, "pln_d00000000004", predecessor="pln_c00000000003")

    _, shown_again = run("program", "show", program_id)
    results.append(_check("show marks the superseded laned member in place, never hidden",
                          "pln_a00000000001" in shown_again
                          and "superseded by `pln_c00000000003`" in shown_again))
    results.append(_check("show lists the post-split newcomer as unlaned",
                          "not in any lane" in shown_again
                          and "pln_d00000000004" in shown_again.split("not in any lane")[1]))

    _, amended = run("program", "lanes", "propose", program_id)
    results.append(_check("amend-mode propose keeps the recorded lane a seed and places the newcomer",
                          "amending around the recorded split" in amended
                          and "pln_d00000000004" in amended))

    tmp.cleanup()
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
