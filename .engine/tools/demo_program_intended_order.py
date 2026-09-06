#!/usr/bin/env python3
"""Demo — a program can declare what it intends to build before any of it is authored.

What this checks, in plain words. A program's chain of children answers to what has actually been
authored — but there is a second, independent thing an operator can decide well before any of that
work exists: the STEPS this program means to take next, in what order, and why one follows another.
That is the INTENDED order — declared precedence over work nobody has written a plan for yet. It is
never derived, never ranked, and never selected or advanced by this machinery; it is disclosed, and a
join that would pass it by silently is refused instead. Watch each of the following, driven through the
same command line you would type yourself, against a throwaway library this script makes and deletes.

  1. THE ORDER IS RECORDED, NOT INFERRED. Four intents are recorded on a fresh program: one with no
     precedent at all, two that each declare why they follow another, and one that follows the same
     root as a second, unrelated branch. `program show` names the one with nothing blocking it as
     next, and every declared edge's reason is rendered — never just the shape, always the "why".

  2. A JOIN CANNOT PASS A RECORDED INTENT BY IN SILENCE. The first child joins with neither door
     open. This shows the join REFUSED, naming every intent still unclaimed and BOTH doors that
     answer to it — claim one, or say the join stands outside the record.

  3. CLAIMING FREES WHAT WAITED ON IT. The same child joins again, this time claiming the intent with
     no precedent. This shows both of its dependents becoming ready AT ONCE — unranked, a partial
     order the program does not complete on anyone's behalf.

  4. CLAIMING OUT OF ORDER COSTS ITS OWN REASON. A second child claims an intent whose declared
     precedent is still unclaimed. This shows the join REFUSED, naming the intent still waited on and
     quoting the very reason that precedence was declared for — then succeeding once a reason for
     jumping it is given, with the jump itself kept in the program's history.

  5. A REPLACED CLAIM MOVES WITH THE PLAN THAT CARRIES IT. The child that claimed the first intent is
     superseded. This shows the claim TRANSFERRING to its replacement, recorded as a transfer rather
     than vanishing and reappearing unexplained.

  6. AN INTENT WITH A SEAT ON THE CHAIN CANNOT BE WITHDRAWN FROM UNDER IT — AND NEITHER CAN ONE
     SOMETHING ELSE STILL FOLLOWS. This shows both refusals: the claimed intent, and the intent
     another live intent still declares precedence on.

  7. A REVISED INTENT KEEPS WHAT IT USED TO SAY. This shows the wording changing, with the prior
     title and statement staying in the record's history rather than being overwritten silently.

  8. A CHILD CAN STAND OUTSIDE THE RECORDED ORDER, ON THE RECORD. This shows a join declaring plainly
     that it fulfils none of what was intended, kept as its own visible entry in the history.

  9. COMPLETION CANNOT DROP A RECORDED STEP SILENTLY. This shows `program complete` REFUSED while an
     intent sits recorded and unclaimed, naming it and the verb that answers to it — and completion
     going through once that intent is withdrawn, with a reason, instead.

Everything below runs the REAL command line (`program_manager.py --library <temp> ...`) against a
temporary directory. Nothing on your shelf is read or touched, and every identifier is invented for
this script. It can fail: each step asserts what it expects, and a wrong answer stops the run with a
non-zero exit.

Run: uv run --directory .engine -- python tools/demo_program_intended_order.py

Declared fate: construction evidence, walled from travel. Every behaviour shown here is covered by a
permanent regression test — IntendedSchema, IntendedOrderRecord, TheNextChildAnswersToTheIntendedOrder,
ReplacementInPlace, IntendedStandingDerivation, EndsThatSettleTheirBooks and IntendedRender in
test_plan_program.py; IntendCommands in test_program_manager.py; ThePortfolioRendersNextIntended in
test_program_projection.py; and TheIntendedRecordHasOneReader / TheIntendedStandingHasNamedCallers,
the two allowlist tripwires that keep the record's raw keys and its one derivation from growing a
second reader anywhere in this tree. This exists so the change can be WATCHED by someone who does not
read code, not to add coverage.
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
import program_manager  # noqa: E402  — the program command line under demonstration
import project_manager  # noqa: E402  — the plan command line, for `init` and `clone` below
import plan_program      # noqa: E402  — read directly, once, to prove the record itself (not just
                          # rendered text) carries what the scenes below claim it does
import plan_store       # noqa: E402  — read-only checks against what the commands wrote

from test_plan_store import _document  # noqa: E402  — the same minimal plan shape the tests use

OK = True
LIBRARY: Path | None = None
SCRATCH: Path | None = None

# Plan identifiers are how the machinery talks; titles are how a person does. Every refusal below is
# the REAL message, so it names ids — titles are substituted back in for display only, exactly as
# demo_program_order_and_honest_ends.py does; nothing about the check changes.
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
    tool = program_manager if argv and argv[0] == "program" else project_manager
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = tool.main(["--library", str(LIBRARY), *argv])
    return code, out.getvalue(), err.getvalue()


def make_plan(program_id, plan_id, title, *, predecessor=None):
    """Mint a plan through the real `init` verb, from a document written to disk first."""
    TITLES[plan_id] = title
    document = _document(plan_id=plan_id, title=title)
    program = {"program_id": program_id}
    if predecessor:
        program["predecessor_plan_id"] = predecessor
    document["program"] = program
    path = SCRATCH / f"{plan_id}.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    code, _, err = run("init", "--document", str(path))
    assert code == 0, err


def seal_plan(plan_id) -> None:
    """Scaffolding, not the surface under demonstration: stamp the gate evidence a seal needs, the
    same shortcut demo_program_order_and_honest_ends.py takes for the same reason."""
    library = plan_store.PlanLibrary(LIBRARY)
    slug = library.resolve(plan_id)
    digest = library.read_record(slug)["current"]["plan_digest"]
    library.update_record(slug, lambda r: r.update({"seal": {
        "revision": 1, "reviewed_digest": digest, "sealed_digest": digest,
        "build_plan_digest": digest, "at": "2026-08-29T03:00:00Z", "delta_judgment": "none"}}))


def program_id_from(out: str) -> str:
    return out.split("created program ")[1].split()[0]


def scene_one_record():
    print("\n1. The order is RECORDED, not inferred\n")
    code, out, _ = run("program", "new", "--title", "Shipping the ordering work",
                       "--objective", "Delivered across several pull requests, none authored yet.")
    assert code == 0, out
    program_id = program_id_from(out)

    assert run("program", "intend", "add", program_id, "--key", "C1",
              "--title", "Lay the groundwork",
              "--statement", "The base seam every later step reads from.")[0] == 0
    assert run("program", "intend", "add", program_id, "--key", "C2",
              "--title", "Reproduce the failure",
              "--statement", "Confirm the bug before touching anything.",
              "--follows", "C1=evidence gate: reproduce the failure before fixing it")[0] == 0
    assert run("program", "intend", "add", program_id, "--key", "C3",
              "--title", "Fix the seam",
              "--statement", "Land the actual fix, now that it is reproduced.",
              "--follows", "C2=builds on C2's read seam")[0] == 0
    assert run("program", "intend", "add", program_id, "--key", "D1",
              "--title", "The risky refactor",
              "--statement", "Take the riskiest slice early, on purpose.",
              "--follows", "C1=the riskier slice, taken early to learn")[0] == 0

    rendered = run("program", "show", program_id)[1]
    check("with nothing yet claimed, C1 alone is named next — nothing blocks it",
          "- **Next intended**: C1 — Lay the groundwork" in rendered)
    check("C2's edge names what it follows and WHY, not just that it follows something",
          "follows C1 — evidence gate: reproduce the failure before fixing it" in rendered)
    check("C3's edge is rendered too — a dependency two steps from the root",
          "follows C2 — builds on C2's read seam" in rendered)
    check("D1's edge, off the SAME root as C2, is rendered as its own reason",
          "follows C1 — the riskier slice, taken early to learn" in rendered)

    # Read the record itself, once, directly — not the rendered text — to prove what `show` says is
    # actually what was written, the same discipline demo_program_lanes.py holds itself to for lanes.
    programs = plan_program.ProgramLibrary(plan_store.PlanLibrary(LIBRARY))
    record = programs.read(programs.resolve(program_id))
    check("the record itself holds all four intents — not just what the render happens to show",
          sorted(i["key"] for i in record["intended"]) == ["C1", "C2", "C3", "D1"])
    return program_id


def scene_two_join_refused(program_id):
    print("\n2. A join cannot pass a recorded intent by in silence\n")
    make_plan(program_id, "pln_0000000000aa", "The child that will claim C1")
    code, _, err = run("program", "add", program_id, "pln_0000000000aa")
    check("the first join is refused outright — neither door was opened",
          code != 0, err.strip().splitlines()[0])
    check("every unclaimed intent is named, not just the one that might apply",
          all(key in err for key in ("C1", "C2", "C3", "D1")))
    check("both doors are named: claim one, or stand outside the record",
          "--fulfills" in err and "--outside-intent" in err)
    return program_id


def scene_three_claim_frees_dependents(program_id):
    print("\n3. Claiming frees what waited on it — unranked, both at once\n")
    code, out, _ = run("program", "add", program_id, "pln_0000000000aa", "--fulfills", "C1")
    check("claiming C1 through the command line succeeds",
          code == 0 and "claims intent 'C1'" in out, out.strip().splitlines()[-1] if code else "")

    rendered = run("program", "show", program_id)[1]
    check("C2 and D1 are BOTH named next now — unranked, not a priority order",
          "- **Next intended**: C2 — Reproduce the failure; D1 — The risky refactor" in rendered)
    check("C3 is not among them: it still waits on C2, which nobody has claimed",
          "C3 — Fix the seam" not in rendered.split("Next intended")[1].split("\n")[0])


def scene_four_out_of_order(program_id):
    print("\n4. Claiming out of order costs its own reason\n")
    make_plan(program_id, "pln_0000000000ab", "The child that jumps ahead for C3",
             predecessor="pln_0000000000aa")
    code, _, err = run("program", "add", program_id, "pln_0000000000ab",
                       "--after", "pln_0000000000aa", "--fulfills", "C3")
    check("claiming C3 ahead of its still-unclaimed precedent is refused",
          code != 0 and "'C3' follows 'C2'" in err, err.strip().splitlines()[0])
    check("the refusal quotes the very reason that precedence was declared for",
          "builds on C2's read seam" in err and "--out-of-order-reason" in err)

    code, out, _ = run("program", "add", program_id, "pln_0000000000ab",
                       "--after", "pln_0000000000aa", "--fulfills", "C3",
                       "--out-of-order-reason", "the fix is understood well enough to start now")
    check("the same join succeeds once the jump is priced with a reason",
          code == 0 and "out of order:" in out)
    rendered = run("program", "show", program_id)[1]
    check("the jump is kept in the program's history, not just in this run's stdout",
          "crossed C2 (builds on C2's read seam)" in rendered
          and "the fix is understood well enough to start now" in rendered)


def scene_five_claim_transfers(program_id):
    print("\n5. A replaced claim moves with the plan that carries it\n")
    seal_plan("pln_0000000000aa")
    code, out, _ = run("clone", "pln_0000000000aa", "--supersedes", "pln_0000000000aa",
                       "--reason", "the foundation needed a redo", "--title", "The redone foundation")
    assert code == 0, out
    replacement_id = out.split("into ")[1].split()[0]
    TITLES[replacement_id] = "The redone foundation"
    code, out, _ = run("program", "supersede", program_id, "pln_0000000000aa",
                       "--with", replacement_id, "--reason", "the foundation needed a redo")
    check("the replacement takes the place on the chain",
          code == 0 and f"{replacement_id} supersedes pln_0000000000aa" in out)
    rendered = run("program", "show", program_id)[1]
    check("C1's claim is recorded as TRANSFERRED, not as vanished and reappeared",
          f"C1**'s claim transferred from `pln_0000000000aa` to `{replacement_id}`" in rendered)
    return replacement_id


def scene_six_withdraw_refusals(program_id, claimant_id):
    print("\n6. Withdrawal cannot silently vacate a seat, or drop a still-followed step\n")
    code, _, err = run("program", "intend", "withdraw", program_id, "C1",
                       "--reason", "not needed after all")
    check("withdrawing a CLAIMED intent is refused — a claim already has a seat",
          code != 0 and claimant_id in err and "program supersede" in err,
          err.strip().splitlines()[0])

    code, _, err = run("program", "intend", "withdraw", program_id, "C2",
                       "--reason", "dropping this one")
    check("withdrawing an intent something else still follows is refused, naming the dependent",
          code != 0 and "C3" in err, err.strip().splitlines()[0])


def scene_seven_revise_keeps_history(program_id):
    print("\n7. A revised intent keeps what it used to say\n")
    code, out, _ = run("program", "intend", "revise", program_id, "D1",
                       "--title", "The risky refactor, rescoped",
                       "--reason", "the risk turned out narrower than first thought")
    check("the revision succeeds through the command line", code == 0, out)
    rendered = run("program", "show", program_id)[1]
    check("the NEW wording is what `show` names next",
          "D1** — The risky refactor, rescoped" in rendered)
    check("the OLD wording stays in history — nothing overwritten silently",
          "Previously: The risky refactor — Take the riskiest slice early, on purpose." in rendered)


def scene_eight_outside_intent(program_id):
    print("\n8. A child can stand outside the recorded order, on the record\n")
    make_plan(program_id, "pln_0000000000ad", "An unplanned hotfix",
             predecessor="pln_0000000000ab")
    code, out, _ = run("program", "add", program_id, "pln_0000000000ad",
                       "--after", "pln_0000000000ab",
                       "--outside-intent", "a hotfix nobody intended; not one of the four steps")
    check("the join succeeds while declaring it fulfils none of the recorded intents",
          code == 0 and "recorded outside the intended order" in out)
    rendered = run("program", "show", program_id)[1]
    check("the record keeps it as its own visible entry, with the reason",
          "recorded outside the intended order" in rendered
          and "a hotfix nobody intended; not one of the four steps" in rendered)


def scene_nine_completion_settles_its_books():
    print("\n9. Completion cannot drop a recorded step silently\n")
    code, out, _ = run("program", "new", "--title", "A small program with loose ends",
                       "--objective", "One child ships; two intended steps never get claimed.")
    program_id = program_id_from(out)
    make_plan(program_id, "pln_0000000000ba", "The one child that ships")
    assert run("program", "add", program_id, "pln_0000000000ba")[0] == 0
    assert run("complete", "pln_0000000000ba", "--reason", "merged")[0] == 0
    assert run("program", "intend", "add", program_id, "--key", "E1", "--title", "Never claimed",
              "--statement", "Meant to follow, but nobody got to it.")[0] == 0
    assert run("program", "intend", "add", program_id, "--key", "E2", "--title", "Also never claimed",
              "--statement", "Same story, a second time.")[0] == 0

    code, _, err = run("program", "complete", program_id, "--reason", "calling it done")
    check("completion is refused while recorded intents sit unclaimed",
          code != 0 and "E1" in err and "E2" in err, err.strip().splitlines()[0])
    check("the refusal names the way through: withdraw them, with a reason",
          "program intend withdraw --reason" in err)

    assert run("program", "intend", "withdraw", program_id, "E1",
              "--reason", "decided against it")[0] == 0
    assert run("program", "intend", "withdraw", program_id, "E2",
              "--reason", "decided against this one too")[0] == 0
    code, out, _ = run("program", "complete", program_id,
                       "--reason", "the one child that shipped was the whole objective")
    check("completion goes through once every recorded step is answered for",
          code == 0 and "recorded complete" in out)


def main() -> int:
    global LIBRARY, SCRATCH
    print(__doc__.split("Run:")[0].rstrip())
    with tempfile.TemporaryDirectory() as temporary:
        LIBRARY = Path(temporary) / "plans"
        SCRATCH = Path(temporary) / "scratch"
        SCRATCH.mkdir()
        program_id = scene_one_record()
        scene_two_join_refused(program_id)
        scene_three_claim_frees_dependents(program_id)
        scene_four_out_of_order(program_id)
        replacement_id = scene_five_claim_transfers(program_id)
        scene_six_withdraw_refusals(program_id, replacement_id)
        scene_seven_revise_keeps_history(program_id)
        scene_eight_outside_intent(program_id)
        scene_nine_completion_settles_its_books()
    print("\n" + ("Everything above held." if OK else "SOMETHING ABOVE DID NOT HOLD."))
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
