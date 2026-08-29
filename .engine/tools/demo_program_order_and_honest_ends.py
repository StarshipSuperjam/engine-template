#!/usr/bin/env python3
"""Demo — a program's order can be re-decided, and its ends stop claiming more than they know.

What this checks, in plain words. A program is a set of plans that together deliver one thing across
several pull requests. Three things used to go wrong with one, and you can watch each of them being
put right here, on the real program object, against a throwaway library this script makes and deletes.

  1. ORDER WAS APPEND-ONLY. Work that turned out to belong BEFORE work already on the list had no way
     in. You could only add after something. This shows a plan being INSERTED ahead of another, with
     both of the connections that move being re-checked — including the one that is easy to forget,
     where the displaced plan now comes after the newcomer and has to answer for what it carries.

  2. CLOSING A PROGRAM SETTLED NOTHING. You could retire a program while it still owed things, and
     afterwards it went on listing those debts as outstanding under a closed status — owed by nobody
     and answerable by nothing. This shows the close being REFUSED and naming each debt, then the
     debt being let go on the record with a stated reason, and only then the close going through.

  3. "COMPLETE" WAS A GUESS. A program reported itself complete as soon as every plan it had ON
     RECORD was complete. That means "nothing left written down" — so a program with one of five
     planned pull requests landed read as finished, because the four nobody had written yet counted
     as done. This shows the headline now stopping at a word that claims only what the record shows,
     and completion arriving only when someone says so, with their reason attached.

Everything below runs the REAL verbs against a REAL plan library in a temporary directory. Nothing on
your shelf is read or touched, and every identifier is invented for this script. It can fail: each
step asserts what it expects, and a wrong answer stops the run with a non-zero exit.

Run: uv run --directory .engine -- python tools/demo_program_order_and_honest_ends.py

Declared fate: construction evidence, walled from travel. Every behaviour shown here is covered by a
permanent regression test — TheOrderCanBeReDecided, ReplacementInPlace, EndsThatSettleTheirBooks and
TheObjectiveCanFollowTheEvidence in test_plan_program.py, plus the ProgramVerbs cases in
test_project_manager.py. This exists so the change can be WATCHED by someone who does not read code,
not to add coverage.
"""
from __future__ import annotations
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plan_program     # noqa: E402  — the real program object under demonstration
import plan_store       # noqa: E402  — the real plan library it reads

from test_plan_store import _document  # noqa: E402  — the same minimal plan shape the tests use

OK = True

# Plan identifiers are how the machinery talks; titles are how a person does. Every refusal below is
# the REAL message, so it names ids — and a reader who has been following "First: the foundation"
# should not suddenly be asked to track `pln_0000000000a1`. Titles are substituted back in for
# display only; nothing about the check changes.
TITLES: dict = {}


def check(claim: str, condition: bool, detail: str = "") -> None:
    global OK
    OK = OK and bool(condition)
    print(f"  {'PASS' if condition else 'FAIL'}  {claim}")
    if detail:
        for plan_id, title in TITLES.items():
            detail = detail.replace(plan_id, f'"{title}"')
        print(f"        {detail}")


def obligation(identifier, statement, state="carried"):
    return {"id": identifier, "statement": statement, "state": state}


def make_plan(library, program_id, plan_id, title, *obligations, predecessor=None):
    TITLES[plan_id] = title
    document = _document(plan_id=plan_id, title=title)
    program = {"program_id": program_id}
    if obligations:
        program["carried_obligations"] = list(obligations)
    if predecessor:
        program["predecessor_plan_id"] = predecessor
    document["program"] = program
    library.create(document)


def close_plan(library, plan_id, state, reason="for the demo"):
    library.update_record(library.resolve(plan_id), lambda record: record.update({"closure": {
        "state": state, "at": "2026-08-29T09:00:00Z", "reason": reason}}))


def scene_one_insert(programs, library):
    print("\n1. Work that belongs BEFORE work already on the list\n")
    slug = programs.create("Shipping a thing", "Delivered across three pull requests.")
    program_id = programs.read(slug)["program_id"]
    for plan_id, title, predecessor in (
            ("pln_0000000000a1", "First: the foundation", None),
            ("pln_0000000000a2", "Second: the feature", "pln_0000000000a1"),
            ("pln_0000000000a3", "Third: the polish", "pln_0000000000a2")):
        make_plan(library, program_id, plan_id, title, predecessor=predecessor)
        programs.add_child(slug, plan_id, predecessor=predecessor)

    order = [child["title"] for child in programs.child_view(programs.read(slug))]
    print(f"  The order as decided:  {' -> '.join(order)}")

    make_plan(library, program_id, "pln_0000000000a9", "Actually first: a safety fix")
    programs.insert_child(slug, "pln_0000000000a9", before="pln_0000000000a2")
    after = [child["title"] for child in programs.child_view(programs.read(slug))]
    print(f"  After inserting:       {' -> '.join(after)}")
    check("the new plan took its place ahead of the second one, and nothing was deleted to do it",
          after == ["First: the foundation", "Actually first: a safety fix",
                    "Second: the feature", "Third: the polish"])

    # The connection that is easy to forget: the displaced plan now comes AFTER the newcomer, so it
    # has to answer for whatever the newcomer is carrying. Here the newcomer carries something the
    # displaced plan has never heard of, and the insertion is refused rather than silently creating
    # a promise nobody downstream ever agreed to.
    make_plan(library, program_id, "pln_0000000000b1", "A newcomer with a debt",
              obligation("SEC-1", "Whoever comes after this must finish the hardening."))
    try:
        programs.insert_child(slug, "pln_0000000000b1", before="pln_0000000000a3")
        check("an insertion that would create an unanswered promise is refused", False)
    except plan_program.ProgramError as refusal:
        check("an insertion that would create an unanswered promise is refused",
              "SEC-1" in str(refusal), str(refusal).splitlines()[0])
    return slug, program_id


def scene_two_closing(programs, library):
    print("\n2. Closing a program used to leave its debts hanging\n")
    slug = programs.create("A program that stopped", "Delivered across two pull requests.")
    program_id = programs.read(slug)["program_id"]
    make_plan(library, program_id, "pln_0000000000c1", "The one that landed",
              obligation("DOC-1", "The next one was going to finish the documentation."))
    programs.add_child(slug, "pln_0000000000c1")
    make_plan(library, program_id, "pln_0000000000c2", "The one that was dropped",
              obligation("DOC-1", "Still to do."), predecessor="pln_0000000000c1")
    programs.add_child(slug, "pln_0000000000c2", predecessor="pln_0000000000c1")
    close_plan(library, "pln_0000000000c1", "complete", "merged")
    close_plan(library, "pln_0000000000c2", "abandoned", "the approach was wrong")

    owed = programs.obligation_report(programs.read(slug))["obligations"]
    print(f"  What it still owes:    {', '.join(o['id'] for o in owed)}")
    try:
        programs.close(slug, "retired", "setting this down")
        check("retiring a program that still owes something is refused", False)
    except plan_program.ProgramError as refusal:
        check("retiring a program that still owes something is refused, and each debt is named",
              "DOC-1" in str(refusal) and "program release" in str(refusal),
              str(refusal).splitlines()[0])

    programs.release(slug, "pln_0000000000c1", "DOC-1",
                     "the pull request that would have done it was dropped, so the work is void")
    print("  Let go on the record:  DOC-1, with the reason above")
    programs.close(slug, "retired", "setting this down")
    check("once the debt is answered for, the program closes",
          programs.derived_status(programs.read(slug)) == "retired")
    rendered = plan_program.render(programs, programs.read(slug))
    check("the reason stays visible to anyone who reads the program afterwards",
          "the work is void" in rendered)


def scene_three_completion(programs, library):
    print("\n3. \"Complete\" used to mean \"nothing left written down\"\n")
    slug = programs.create("Five planned, one written", "Delivered across five pull requests.")
    program_id = programs.read(slug)["program_id"]
    make_plan(library, program_id, "pln_0000000000d1", "The first of five")
    programs.add_child(slug, "pln_0000000000d1")
    close_plan(library, "pln_0000000000d1", "complete", "merged")

    status = programs.derived_status(programs.read(slug))
    print(f"  One of five landed. The headline reads:  {status}")
    check("the headline does NOT say the program is complete", status != "complete",
          "against the previous code this read `complete`, with four pull requests unwritten")
    rendered = plan_program.render(programs, programs.read(slug))
    check("and it says in full what it is not claiming",
          "This program is not recorded as complete" in rendered)

    make_plan(library, program_id, "pln_0000000000d2", "The second of five",
              predecessor="pln_0000000000d1")
    programs.add_child(slug, "pln_0000000000d2", predecessor="pln_0000000000d1")
    try:
        programs.complete(slug, "calling it done")
        check("recording completion over unfinished work is refused", False)
    except plan_program.ProgramError as refusal:
        check("recording completion over unfinished work is refused",
              "pln_0000000000d2" in str(refusal), str(refusal).splitlines()[1].strip())

    close_plan(library, "pln_0000000000d2", "complete", "merged")
    programs.complete(slug, "both pull requests landed and the objective is met")
    record = programs.read(slug)
    check("completion arrives only when someone records it, with their reason attached",
          record["closure"]["state"] == "complete"
          and record["closure"]["reason"] == "both pull requests landed and the objective is met")
    check("and the program says this was recorded rather than worked out",
          "recorded by an explicit close, not derived" in plan_program.render(programs, record))

    programs.reopen(slug, "a defect turned up after all")
    reopened = programs.read(slug)
    check("it can be undone — but never silently: what was undone is kept, with why",
          reopened["closure"] is None
          and reopened["closure_history"][0]["closure"]["state"] == "complete"
          and reopened["closure_history"][0]["reason"] == "a defect turned up after all")


def main() -> int:
    print(__doc__.split("Run:")[0].rstrip())
    with tempfile.TemporaryDirectory() as temporary:
        library = plan_store.PlanLibrary(Path(temporary) / "plans")
        programs = plan_program.ProgramLibrary(library)
        scene_one_insert(programs, library)
        scene_two_closing(programs, library)
        scene_three_completion(programs, library)
    print("\n" + ("Everything above held." if OK else "SOMETHING ABOVE DID NOT HOLD."))
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
