#!/usr/bin/env python3
"""Demo — a program's order can be re-decided, and its ends stop claiming more than they know.

What this checks, in plain words. A program is a set of plans that together deliver one thing across
several pull requests. You can watch each of the following being put right here, driven through the
same command line you would type yourself, against a throwaway library this script makes and deletes.

  1. ORDER WAS APPEND-ONLY. Work that turned out to belong BEFORE work already on the list had no way
     in. This shows a plan being INSERTED ahead of another, with both of the connections that move
     being re-checked — including the one that is easy to forget, where the displaced plan now comes
     after the newcomer and has to answer for what it carries.

  2. CLOSING A PROGRAM SETTLED NOTHING. You could retire a program while it still owed things. This
     shows the close being REFUSED and naming each debt, the debt being let go on the record with a
     stated reason, and only then the close going through — including the late-minted debt that used
     to slip past both gates entirely.

  3. "COMPLETE" WAS A GUESS. A program reported itself complete as soon as every plan ON RECORD was
     complete — "nothing left written down" read as "done". This shows the headline stopping at what
     the record can actually claim, and completion arriving only when someone records it, with their
     reason attached, undoable but never silently.

  4. A PLAN THAT TURNED OUT WRONG COULD ONLY BE PILED ON TOP OF. This shows one being REPLACED in
     place: the supersede that refuses a still-revisable draft, the clone that pre-fills the debts
     the place owes, the replaced plan staying visible but no longer bindable — and staying put,
     because reopening it is refused too.

  5. THE OBJECTIVE WAS FROZEN AT ITS LEAST-INFORMED MOMENT. This shows it being revised with the old
     text kept, never overwritten silently.

  6. A WRECKED RECORD COULD NOT BE PUT DOWN HONESTLY. This shows a program whose books cannot be
     computed refusing a quiet close, then closing on an explicit, recorded acknowledgement.

Everything below runs the REAL command line (`project_manager.py --library <temp> ...`) against a
temporary directory. Nothing on your shelf is read or touched, and every identifier is invented for
this script. It can fail: each step asserts what it expects, and a wrong answer stops the run with a
non-zero exit.

Run: uv run --directory .engine -- python tools/demo_program_order_and_honest_ends.py

Declared fate: construction evidence, walled from travel. Every behaviour shown here is covered by a
permanent regression test — TheOrderCanBeReDecided, ReplacementInPlace, EndsThatSettleTheirBooks and
TheObjectiveCanFollowTheEvidence in test_plan_program.py, plus the ProgramVerbs cases in
test_project_manager.py. This exists so the change can be WATCHED by someone who does not read code,
not to add coverage.
"""
from __future__ import annotations
import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import project_manager  # noqa: E402  — the public command line under demonstration
import plan_store       # noqa: E402  — read-only checks against what the commands wrote

from test_plan_store import _document  # noqa: E402  — the same minimal plan shape the tests use

OK = True
LIBRARY: Path | None = None
SCRATCH: Path | None = None

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


def run(*argv) -> tuple[int, str, str]:
    """One real command-line invocation: the same argv you would type, minus the temp --library."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = project_manager.main(["--library", str(LIBRARY), *argv])
    return code, out.getvalue(), err.getvalue()


def obligation(identifier, statement, state="carried"):
    return {"id": identifier, "statement": statement, "state": state}


def make_plan(program_id, plan_id, title, *obligations, predecessor=None):
    """Mint a plan through the real `init` verb, from a document written to disk first."""
    TITLES[plan_id] = title
    document = _document(plan_id=plan_id, title=title)
    program = {"program_id": program_id}
    if obligations:
        program["carried_obligations"] = list(obligations)
    if predecessor:
        program["predecessor_plan_id"] = predecessor
    document["program"] = program
    path = SCRATCH / f"{plan_id}.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    code, _, err = run("init", "--document", str(path))
    assert code == 0, err


def seal_plan(plan_id) -> None:
    """Scaffolding, not the surface under demonstration: stamp the gate evidence a seal needs.

    The real seal ceremony (present, approve with a depth, the one cold review) is the PLAN
    lifecycle's demonstration, not this one's; writing its evidence directly is how the program
    scenes get a sealed plan without re-demonstrating another feature's doors.
    """
    library = plan_store.PlanLibrary(LIBRARY)
    slug = library.resolve(plan_id)
    digest = library.read_record(slug)["current"]["plan_digest"]
    library.update_record(slug, lambda r: r.update({"seal": {
        "revision": 1, "reviewed_digest": digest, "sealed_digest": digest,
        "build_plan_digest": digest, "at": "2026-08-29T03:00:00Z", "delta_judgment": "none"}}))


def program_id_from(out: str) -> str:
    return out.split("created program ")[1].split()[0]


def scene_one_insert():
    print("\n1. Work that belongs BEFORE work already on the list\n")
    code, out, _ = run("program", "new", "--title", "Shipping a thing",
                       "--objective", "Delivered across three pull requests.")
    assert code == 0
    program_id = program_id_from(out)
    for plan_id, title, predecessor in (
            ("pln_0000000000a1", "First: the foundation", None),
            ("pln_0000000000a2", "Second: the feature", "pln_0000000000a1"),
            ("pln_0000000000a3", "Third: the polish", "pln_0000000000a2")):
        make_plan(program_id, plan_id, title, predecessor=predecessor)
        argv = ["program", "add", program_id, plan_id]
        if predecessor:
            argv += ["--after", predecessor]
        assert run(*argv)[0] == 0

    rendered = run("program", "show", program_id)[1]
    make_plan(program_id, "pln_0000000000a9", "Actually first: a safety fix")
    code, out, _ = run("program", "insert", program_id, "pln_0000000000a9",
                       "--before", "pln_0000000000a2")
    check("the new plan takes its place ahead of the second one, through the command line",
          code == 0 and "pln_0000000000a1 -> pln_0000000000a9 -> pln_0000000000a2" in out)
    rendered = run("program", "show", program_id)[1]
    check("and nothing was deleted to do it: all four plans still render, in the re-decided order",
          rendered.index("Actually first") < rendered.index("Second: the feature")
          and "Third: the polish" in rendered)

    # The connection that is easy to forget: the displaced plan now comes AFTER the newcomer, so it
    # has to answer for whatever the newcomer is carrying. Here the newcomer carries something the
    # displaced plan has never heard of, and the insertion is refused rather than silently creating
    # a promise nobody downstream ever agreed to.
    make_plan(program_id, "pln_0000000000b1", "A newcomer with a debt",
              obligation("SEC-1", "Whoever comes after this must finish the hardening."))
    code, _, err = run("program", "insert", program_id, "pln_0000000000b1",
                       "--before", "pln_0000000000a3")
    check("an insertion that would create an unanswered promise is refused",
          code != 0 and "SEC-1" in err, err.strip().splitlines()[0])


def scene_two_closing():
    print("\n2. Closing a program used to leave its debts hanging\n")
    code, out, _ = run("program", "new", "--title", "A program that stopped",
                       "--objective", "Delivered across two pull requests.")
    program_id = program_id_from(out)
    make_plan(program_id, "pln_0000000000c1", "The one that landed",
              obligation("DOC-1", "The next one was going to finish the documentation."))
    assert run("program", "add", program_id, "pln_0000000000c1")[0] == 0
    make_plan(program_id, "pln_0000000000c2", "The one that was dropped",
              obligation("DOC-1", "Still to do."), predecessor="pln_0000000000c1")
    assert run("program", "add", program_id, "pln_0000000000c2",
               "--after", "pln_0000000000c1")[0] == 0
    assert run("complete", "pln_0000000000c1", "--reason", "merged")[0] == 0
    assert run("abandon", "pln_0000000000c2", "--reason", "the approach was wrong")[0] == 0

    code, _, err = run("program", "retire", program_id, "--reason", "setting this down")
    check("retiring a program that still owes something is refused, and each debt is named",
          code != 0 and "DOC-1" in err and "program release" in err, err.strip().splitlines()[0])

    code, out, _ = run("program", "release", program_id, "pln_0000000000c1",
                       "--obligation", "DOC-1",
                       "--reason", "the pull request that would have done it was dropped")
    check("the debt is let go on the record, with the reason attached", code == 0 and "DOC-1" in out)
    code, _, _ = run("program", "retire", program_id, "--reason", "setting this down")
    check("once the debt is answered for, the program closes", code == 0)
    rendered = run("program", "show", program_id)[1]
    check("the reason stays visible to anyone who reads the program afterwards",
          "would have done it was dropped" in rendered)

    # The debt that used to escape entirely: minted AFTER its successor sealed, it sat mid-chain
    # where the report could not place it, and both closure gates passed over it.
    print("\n   ...and the late-minted debt that used to slip past both gates\n")
    code, out, _ = run("program", "new", "--title", "A late debt",
                       "--objective", "The predecessor learned something after the successor sealed.")
    program_id = program_id_from(out)
    make_plan(program_id, "pln_0000000000c5", "The one that learned late")
    assert run("program", "add", program_id, "pln_0000000000c5")[0] == 0
    make_plan(program_id, "pln_0000000000c6", "The one already sealed",
              predecessor="pln_0000000000c5")
    assert run("program", "add", program_id, "pln_0000000000c6",
               "--after", "pln_0000000000c5")[0] == 0
    seal_plan("pln_0000000000c6")
    # Revise the predecessor to mint a debt the sealed successor never saw — the real `revise` verb.
    library = plan_store.PlanLibrary(LIBRARY)
    revised = dict(library.head(library.resolve("pln_0000000000c5")))
    revised["program"] = {"program_id": program_id, "carried_obligations": [
        obligation("LATE-1", "Discovered after the successor sealed.")]}
    revised["revision"] = 2
    revised["revision_note"] = "mint an obligation the sealed successor can never answer"
    path = SCRATCH / "late-revision.json"
    path.write_text(json.dumps(revised), encoding="utf-8")
    assert run("revise", "pln_0000000000c5", "--document", str(path))[0] == 0

    code, _, err = run("program", "retire", program_id, "--reason", "setting this down")
    check("the close now sees the late debt and refuses, naming it",
          code != 0 and "LATE-1" in err, err.strip().splitlines()[0])
    code, _, err = run("program", "release", program_id, "pln_0000000000c5",
                       "--obligation", "LATE-1",
                       "--reason", "the successor sealed first; nothing can answer this")
    check("release opens for exactly this shape — the sealed successor cannot take a revision",
          code == 0, err.strip().splitlines()[0] if code != 0 else "")
    check("and with the debt on the record, the close goes through",
          run("program", "retire", program_id, "--reason", "setting this down")[0] == 0)


def scene_three_completion():
    print("\n3. \"Complete\" used to mean \"nothing left written down\"\n")
    code, out, _ = run("program", "new", "--title", "Five planned, one written",
                       "--objective", "Delivered across five pull requests.")
    program_id = program_id_from(out)
    make_plan(program_id, "pln_0000000000d1", "The first of five")
    assert run("program", "add", program_id, "pln_0000000000d1")[0] == 0
    assert run("complete", "pln_0000000000d1", "--reason", "merged")[0] == 0

    listed = next(line for line in run("program", "list")[1].splitlines() if program_id in line)
    status = listed.split()[1]
    print(f"  One of five landed. The headline reads:  {status}")
    check("the headline does NOT say the program is complete", status != "complete",
          "against the previous code this read `complete`, with four pull requests unwritten")
    check("and the listing qualifies the word on the spot, pointing at the full sentence",
          "nobody has recorded that the PROGRAM is" in run("program", "list")[1])
    check("while `show` carries the claim in full",
          "not the same as this program being complete"
          in run("program", "show", program_id)[1])

    make_plan(program_id, "pln_0000000000d2", "The second of five",
              predecessor="pln_0000000000d1")
    assert run("program", "add", program_id, "pln_0000000000d2",
               "--after", "pln_0000000000d1")[0] == 0
    code, _, err = run("program", "complete", program_id, "--reason", "calling it done")
    check("recording completion over unfinished work is refused",
          code != 0 and "pln_0000000000d2" in err, err.strip().splitlines()[0])

    assert run("complete", "pln_0000000000d2", "--reason", "merged")[0] == 0
    code, out, _ = run("program", "complete", program_id,
                       "--reason", "both pull requests landed and the objective is met")
    check("completion arrives only when someone records it, with their reason attached",
          code == 0 and "recorded complete" in out)
    code, out, _ = run("program", "reopen", program_id, "--reason", "a defect turned up after all")
    rendered = run("program", "show", program_id)[1]
    check("it can be undone — but never silently: what was undone is kept, with why",
          code == 0 and "a defect turned up after all" in rendered)


def scene_four_replacement():
    print("\n4. A plan that turned out wrong is replaced in place\n")
    code, out, _ = run("program", "new", "--title", "A wrong turn",
                       "--objective", "The second pull request took the wrong shape.")
    program_id = program_id_from(out)
    make_plan(program_id, "pln_0000000000e1", "The foundation",
              obligation("OB-1", "The next plan finishes the cut-over."))
    assert run("program", "add", program_id, "pln_0000000000e1")[0] == 0
    make_plan(program_id, "pln_0000000000e2", "The wrong shape",
              obligation("OB-1", "Still carried."), predecessor="pln_0000000000e1")
    assert run("program", "add", program_id, "pln_0000000000e2",
               "--after", "pln_0000000000e1")[0] == 0

    code, _, err = run("program", "supersede", program_id, "pln_0000000000e2",
                       "--with", "pln_0000000000e1", "--reason", "premature")
    check("superseding a plan that is still a draft is refused — a draft is revised, not replaced",
          code != 0 and "not sealed" in err, err.strip().splitlines()[0])

    seal_plan("pln_0000000000e2")     # now it is terminal, and replacement is the only way past it
    code, out, _ = run("clone", "pln_0000000000e2", "--supersedes", "pln_0000000000e2",
                       "--reason", "the shape was wrong", "--title", "The right shape")
    check("clone pre-fills the replacement with exactly what the place owes",
          code == 0 and "OB-1" in out)
    clone_id = out.split("into ")[1].split()[0]
    TITLES[clone_id] = "The right shape"
    code, out, _ = run("program", "supersede", program_id, "pln_0000000000e2",
                       "--with", clone_id, "--reason", "the shape was wrong")
    check("the replacement takes the place on the chain, through the command line",
          code == 0 and f"{clone_id} supersedes pln_0000000000e2" in out)
    rendered = run("program", "show", program_id)[1]
    check("the replaced plan stays visible in the record — history is never deleted",
          "The wrong shape" in rendered and f"superseded by `{clone_id}`" in rendered)
    code, _, err = run("reopen", "pln_0000000000e2")
    check("and it cannot be brought back into play: reopening a superseded plan is refused",
          code != 0 and "superseded" in err, err.strip().splitlines()[0])


def scene_five_objective():
    print("\n5. The objective can follow the evidence\n")
    code, out, _ = run("program", "new", "--title", "A stale headline",
                       "--objective", "Deliver the coordinator over sealed handoffs.")
    program_id = program_id_from(out)
    code, out, _ = run("program", "revise-objective", program_id,
                       "--objective", "Deliver the coordinator over sealed handoffs, and the "
                                      "order tools the first attempt showed were missing.",
                       "--reason", "the first pull request surfaced the ordering work")
    check("the objective is revised through the command line", code == 0)
    rendered = run("program", "show", program_id)[1]
    check("the old text is kept beside the new — nothing is overwritten silently",
          "order tools" in rendered and "surfaced the ordering work" in rendered)


def scene_six_unknown():
    print("\n6. A wrecked record can be put down — but only on the record\n")
    code, out, _ = run("program", "new", "--title", "A wrecked one",
                       "--objective", "Its second child's plan is gone from the library.")
    program_id = program_id_from(out)
    make_plan(program_id, "pln_0000000000f1", "The child that reads")
    assert run("program", "add", program_id, "pln_0000000000f1")[0] == 0
    # Construct the wreckage: the record names a child the library does not hold. This is the one
    # direct write in this scene, because a broken record is the PRECONDITION here, not a verb.
    library = plan_store.PlanLibrary(LIBRARY)
    programs_dir = LIBRARY / "programs"
    slug = next(p.name for p in programs_dir.iterdir()
                if (p / "record.json").is_file()
                and json.loads((p / "record.json").read_text())["program_id"] == program_id)
    record_path = programs_dir / slug / "record.json"
    record = json.loads(record_path.read_text())
    record["children"].append({"plan_id": "pln_0000000000f9",
                               "added_at": "2026-01-01T00:00:00Z",
                               "predecessor_plan_id": "pln_0000000000f1"})
    record_path.write_text(json.dumps(record), encoding="utf-8")
    TITLES["pln_0000000000f9"] = "The child that is gone"

    code, _, err = run("program", "abandon", program_id, "--reason", "wrecked")
    check("a quiet close over uncomputable books is refused",
          code != 0 and "cannot be computed" in err, err.strip().splitlines()[0])
    code, out, _ = run("program", "abandon", program_id, "--reason", "wrecked",
                       "--acknowledge-unknown",
                       "the missing child predates the library move; its debts died with it")
    check("closing takes an explicit acknowledgement instead, recorded in the closure",
          code == 0 and "on the record" in out)
    rendered = run("program", "show", program_id)[1]
    check("and the acknowledgement stays visible afterwards",
          "predates the library move" in rendered)


def main() -> int:
    global LIBRARY, SCRATCH
    print(__doc__.split("Run:")[0].rstrip())
    with tempfile.TemporaryDirectory() as temporary:
        LIBRARY = Path(temporary) / "plans"
        SCRATCH = Path(temporary) / "scratch"
        SCRATCH.mkdir()
        scene_one_insert()
        scene_two_closing()
        scene_three_completion()
        scene_four_replacement()
        scene_five_objective()
        scene_six_unknown()
    print("\n" + ("Everything above held." if OK else "SOMETHING ABOVE DID NOT HOLD."))
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
