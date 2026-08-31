#!/usr/bin/env python3
"""The Program Manager: the program surface at its own address.

A program is an ordered set of plans that carry obligations forward across several pull requests. The
verbs that create one, append and re-order its children, replace a child that turned out wrong, release
a debt whose successors are gone, record or reverse a closure, revise its objective, and record the
operator's DECIDED concurrency split — all of that lives here, reached as `program_manager.py program
<verb>`. It moved here whole from the Project Manager, argv-compatible with every caller, so the
invocation shape did not change: only its address did.

THE SEAM, AND ITS ONE HONEST IMPURITY. This surface reads the plan library freely and writes it in
exactly one place: `program supersede` retires the plan it replaces, and it does that through the
Project Manager's own close path — `project_manager.close_plan_record` — rather than a second
implementation of it, because the ORDER of that write against the program-record write is the safety
argument (retire the plan first, then mark the record; a crash between leaves a retired plan and an
unmarked record, out of play and repaired by re-running, never a marked record over a plan a Build
could still bind). That one call is the only plan-library write the program surface may perform, and a
module-scoped pin over this file enforces it. Everything else program_manager touches on the plan
library it only READS.

IMPORTS RUN ONE WAY. program_manager imports from project_manager — the shared close seam and the
library resolver — and project_manager never imports program_manager. A test pins that direction, so
the split cannot quietly become a cycle.
"""
from __future__ import annotations

import argparse
import sys

import moment
import plan_program
import plan_projection
import plan_store
import program_projection
import project_manager

ProgramManagerError = plan_store.PlanStoreError
_now = moment.utc_now


def _programs(args):
    return plan_program.ProgramLibrary(project_manager._library(args))


def _refresh_projection(programs, slug: str) -> None:
    """Regenerate the touched program's PROGRAM.md AFTER its record write, OUTSIDE the verb's failure
    path. A projection failure never fails the verb — the verb's exit code reports the RECORD's truth,
    and the record is already written — it only warns, and the stale file converges on the next program
    verb here or a `program reproject` sweep."""
    try:
        program_projection.project_program(programs, slug)
    except Exception as exc:  # noqa: BLE001 — degrade open; the record write already succeeded
        print(f"warning: {slug}'s PROGRAM.md could not be regenerated and is now stale; it converges on "
              f"the next program verb here or a `program reproject` sweep — the record itself is written "
              f"and correct: {exc}", file=sys.stderr)


def cmd_program_new(args) -> int:
    programs = _programs(args)
    slug = programs.create(args.title, args.objective)
    record = programs.read(slug)
    print(f"created program {record['program_id']} at {programs.program_dir(slug)}")
    print("Add its first child with `program add`. Order records a decision — nothing here starts, "
          "selects, or advances a child.")
    _refresh_projection(programs, slug)
    return 0


def cmd_program_reproject(args) -> int:
    """The library-wide sweep: regenerate every program's PROGRAM.md — a needs-attention file for any
    whose record will not read — and continue past the damaged ones. A pure projection; it writes no
    record, selects nothing, and starts nothing."""
    programs = _programs(args)
    written = program_projection.project_all(programs)
    print(f"regenerated PROGRAM.md for {len(written)} program(s)")
    return 0


def cmd_program_list(args) -> int:
    programs = _programs(args)
    slugs = programs.slugs()
    if not slugs:
        print(f"no programs in {programs.root}")
        return 0
    for slug in slugs:
        record = programs.read(slug)
        report = programs.obligation_report(record)
        # "unknown", never "0". This one-line summary is what an operator scans first, so it is the
        # LAST place a corrupt program should be able to read as a clean zero — which is exactly what
        # it did while the unknown rendering existed only in `show`.
        owed = (f"{len(report['obligations'])} obligation(s) outstanding" if not report["unknown"]
                else f"obligations unknown ({len(report['unknown'])} reason(s) — run `program show`)")
        status = programs.derived_status(record)
        print(f"{record['program_id']}  {status:<18} "
              f"{len(record['children'])} child(ren), {owed}  {record['title']}")
        if status == programs.CHILDREN_COMPLETE:
            # `list` is the view built for scanning many programs at once, which makes it the view
            # where a token can most easily be mistaken for a verdict. `show` carries the full
            # sentence; withholding every trace of it here would reintroduce at the list level the
            # exact misreading this change exists to remove.
            print("                    ^ every child on record is done; nobody has recorded that "
                  "the PROGRAM is. Run `program show` for what that does and does not claim.")
    print("\nEvery open program at a glance, qualitatively — `program portfolio`.")
    return 0


def cmd_program_show(args) -> int:
    programs = _programs(args)
    slug = programs.resolve(args.program)
    print(plan_program.render(programs, programs.read(slug)))
    _report_decay(programs, slug)
    print("\nEvery open program at a glance, qualitatively — `program portfolio`.")
    return 0


def cmd_program_portfolio(args) -> int:
    """The qualitative portfolio: every open program at a glance. A pure read — it renders and writes
    nothing, selects nothing, starts nothing."""
    programs = _programs(args)
    print(program_projection.render_portfolio(programs), end="")
    return 0


def cmd_program_add(args) -> int:
    programs = _programs(args)
    slug = programs.resolve(args.program)
    record = programs.add_child(slug, args.plan, predecessor=args.after)
    # THIS child's own carries, not the program-wide union. Once the chain forks, "what the program
    # still owes" and "what the next child on THIS branch must answer for" are different questions,
    # and only the second one is what an operator adding to a branch is being told. Printing the union
    # here would attribute another branch's debts to a successor that can never answer for them.
    plan_slug = programs.plans.resolve(args.plan)
    outstanding = _still_carried(programs, args.plan)
    ordinal = next((child["chain_ordinal"] for child in programs.child_view(record)
                    if child["plan_id"] == programs.plans.read_record(plan_slug)["plan_id"]),
                   len(record["children"]))
    print(f"added {args.plan} as child {ordinal} of {record['program_id']}")
    if outstanding:
        print(f"\n{len(outstanding)} obligation(s) are now carried into the next child ON THIS BRANCH:")
        for obligation in outstanding:
            print(f"  - {obligation['id']}: {obligation['statement']}")
        print("\nThe next child on this branch must answer for each — satisfied, still carried, or "
              "released with a stated reason. None of them can be dropped by saying nothing.")
    _report_decay(programs, slug)
    _refresh_projection(programs, slug)
    return 0


def cmd_program_supersede(args) -> int:
    """Replace a child that turned out wrong, keeping it visible and its place on the chain.

    The ORDER of the two writes is the safety argument, and it is enforced here because this is the
    only layer that may touch both records: plan_program never writes the plan library, which a
    mechanical allowlist pins. Refuse, then retire the plan, then mark the program record. A crash
    between the last two leaves a retired plan and an unmarked record — out of play, and repaired by
    running the verb again — never a marked record over a plan a Build could still bind.
    """
    programs = _programs(args)
    slug = programs.resolve(args.program)
    library = programs.plans
    resolved = programs.supersede_check(slug, args.plan, args.With)

    superseded_slug = library.resolve(resolved["superseded_id"])
    existing = library.read_record(superseded_slug).get("closure")
    if not existing:
        project_manager.close_plan_record(library, superseded_slug, "retired", args.reason, refuse_if_active=True)
        print(f"retired {resolved['superseded_id']}: {args.reason}")
    elif existing["state"] == "retired":
        # The half-completed state: the plan was retired by an earlier run that did not reach the
        # record. Two repairs, not one — the record write below, and the PROJECTION, which the
        # earlier run may also have died before: `_close_plan` writes the closure and then projects,
        # and a crash between the two leaves the library's rendered index still advertising a plan
        # the record says is out of play. Re-running the verb must converge the whole state, so the
        # retry re-projects rather than assuming the closure write got that far.
        print(f"{resolved['superseded_id']} was already retired; completing the supersession.")
        plan_projection.project_library(library)
    else:
        # Closed, but not the way a supersession closes a plan. An abandoned or completed target is
        # someone's DIFFERENT decision, not this verb's own crash debris, and converging over it
        # would fold that decision into a supersession nobody made of it.
        raise ProgramManagerError(
            f"{resolved['superseded_id']} is {existing['state']} ({existing['reason']}) — that is "
            "not the half-finished supersession this verb can converge, which retires its target. "
            "If this plan should indeed be replaced on the chain, that closure already took it out "
            "of play; what supersede would add is the program-record marker, and writing one over "
            "a closure someone else chose would misdescribe their decision. If the closure itself "
            "is wrong: an unsealed plan takes `reopen`; a sealed plan's closure is permanent — "
            "clone it into a new plan instead. Otherwise leave the chain as the record tells it.")

    record = programs.mark_superseded(slug, args.plan, args.With)
    print(f"{resolved['replacement_id']} supersedes {resolved['superseded_id']} "
          f"in {record['program_id']}")
    print(f"  it inherits the place after "
          f"{resolved['inherited'] or '(the start of the chain)'}, and everything that succeeded "
          f"{resolved['superseded_id']} now succeeds it")
    print("Nothing was deleted: the replaced child stays in the record, marked with what replaced "
          "it, and its plan and every revision stay on the shelf.")
    _report_decay(programs, slug)
    _refresh_projection(programs, slug)
    return 0


def cmd_program_insert(args) -> int:
    programs = _programs(args)
    slug = programs.resolve(args.program)
    record = programs.insert_child(slug, args.plan, before=args.before)
    plan_slug = programs.plans.resolve(args.plan)
    plan_id = programs.plans.read_record(plan_slug)["plan_id"]
    view = programs.child_view(record)
    ordinal = next((child["chain_ordinal"] for child in view if child["plan_id"] == plan_id), None)
    displaced_id = programs.plans.read_record(programs.plans.resolve(args.before))["plan_id"]
    print(f"inserted {plan_id} as child {ordinal} of {record['program_id']}, ahead of {displaced_id}")
    # Say which edges moved. An insertion changes what TWO plans are answerable to, and the second
    # one — the displaced child now succeeding the newcomer — is the half an operator does not
    # picture on their own, because appending has never had a second edge to think about.
    inserted = next(child for child in record["children"] if child["plan_id"] == plan_id)
    predecessor = inserted.get("predecessor_plan_id")
    print(f"  {predecessor} -> {plan_id} -> {displaced_id}" if predecessor
          else f"  {plan_id} now starts this chain, and {displaced_id} succeeds it")
    print("Nothing was renumbered: the order every reader derives comes from these edges.")
    outstanding = _still_carried(programs, args.plan)
    if outstanding:
        print(f"\n{displaced_id} now answers for {len(outstanding)} obligation(s) carried by "
              f"{plan_id}:")
        for obligation in outstanding:
            print(f"  - {obligation['id']}: {obligation['statement']}")
    _report_decay(programs, slug)
    _refresh_projection(programs, slug)
    return 0


def _still_carried(programs, plan_selector: str) -> list:
    """What a plan hands on to the next child on its branch.

    No program-level release can apply here, and the reason is worth stating rather than guarding
    against: a release is keyed to a child that is ALREADY in the program, and both doors this
    serves — `add` and `insert` — refuse a plan that is already a child. So the released set for
    the plan just joined is empty by construction.

    An earlier round subtracted it anyway, in response to a review finding that these reports could
    name a debt the gates no longer enforce. That is true of a long-standing child and not of a
    newly joined one, and the subtraction here could never fire — inert code shaped like a guard,
    which reads as protection nobody has. The one place the subtraction genuinely bites is
    `_supersession_block`, where the obligations come from a predecessor that has been a child for
    as long as the release has existed, and it is kept and tested there.
    """
    plan_slug = programs.plans.resolve(plan_selector)
    return sorted(plan_program.carried_forward(programs.plans.head(plan_slug)).values(),
                  key=lambda o: o["id"])


def _report_decay(programs, slug: str, *, plan_id: str | None = None) -> list:
    """Re-check every joined child against its predecessor's CURRENT head, and say what has decayed.

    The join-time check is a snapshot, and a predecessor revised afterwards can mint obligations its
    successor never saw. Reported wherever a program is looked at, so the decay surfaces while there
    is still a plan to revise rather than at the seal.
    """
    decay = programs.carry_forward_decay(slug, plan_id=plan_id)
    for entry in decay:
        print(f"\nwarning: {entry['plan_id']} no longer answers for "
              f"{len(entry['obligations'])} obligation(s) that {entry['predecessor_plan_id']} carries. "
              "They were minted after it joined, so the join-time check never saw them:",
              file=sys.stderr)
        for obligation in entry["obligations"]:
            print(f"  - {obligation['id']}: {obligation['statement']}", file=sys.stderr)
        # The way through depends on what the successor can still DO. "Revise it" was printed
        # unconditionally, and for a successor already sealed that is a locked door — a seal is
        # terminal — so the advice comes from the same owner every refusal uses.
        try:
            successor = programs.plans.read_record(programs.plans.resolve(entry["plan_id"]))
            advice = plan_program.way_through_for(
                entry["plan_id"], plan_store.derived_status(successor), bool(successor.get("seal")))
        except Exception:  # noqa: BLE001 — an unreadable successor is reported by other readers
            advice = ""
        if advice:
            print(" " + advice.strip(), file=sys.stderr)
        else:
            print(f"  Revise {entry['plan_id']} to answer for each — satisfied, still carried, or "
                  "released with a reason. Its seal refuses until it does.", file=sys.stderr)
    return decay


def cmd_program_close(args) -> int:
    programs = _programs(args)
    slug = programs.resolve(args.program)
    record = programs.close(slug, args.state, args.reason,
                            acknowledged_unknown=getattr(args, "acknowledge_unknown", None))
    print(f"{record['program_id']} is now {args.state}: {args.reason}")
    if record["closure"].get("acknowledged_unknown"):
        print("Closed over an unknown, on the record: "
              f"{record['closure']['acknowledged_unknown']}")
        print("That is a decision, not a resolution — what this program owed still cannot be "
              "computed from its record.")
    print("Nothing was deleted — every child plan and every revision stays on the shelf.")
    _refresh_projection(programs, slug)
    return 0


def cmd_program_complete(args) -> int:
    """The only door to a complete program. Nothing derives it, and nothing else writes it."""
    programs = _programs(args)
    slug = programs.resolve(args.program)
    record = programs.complete(slug, args.reason)
    print(f"{record['program_id']} is recorded complete: {args.reason}")
    print("Recorded, not derived — this is your judgment that the objective is met, and the record "
          "now says so with your reason attached.")
    print("It is reversible: `program reopen` undoes it, with a reason, and keeps what was undone.")
    _refresh_projection(programs, slug)
    return 0


def cmd_program_release(args) -> int:
    programs = _programs(args)
    slug = programs.resolve(args.program)
    record = programs.release(slug, args.child, args.obligation, args.reason)
    child_id = programs.plans.read_record(programs.plans.resolve(args.child))["plan_id"]
    # Print the reason the RECORD holds, not the one this invocation offered: a re-run of an
    # existing release keeps the original reason, and echoing the new text would report an audit
    # fact the store never wrote.
    stored = next(entry for entry in record.get("releases", [])
                  if entry["child_plan_id"] == child_id and entry["obligation_id"] == args.obligation)
    print(f"released {args.obligation}, carried at {child_id}, in {record['program_id']}")
    print(f"  {stored['reason']}")
    if stored["reason"] != args.reason:
        print(f"  (already released at {stored['at']} — the recorded reason above stands; this "
              "run's differing reason was not written)")
    print("Released at PROGRAM level, which is a different decision from a release written inside a "
          "successor plan: it says no successor was left to answer for this at all.")
    report = programs.obligation_report(programs.read(slug))
    if report["obligations"]:
        print(f"\n{len(report['obligations'])} obligation(s) still outstanding: "
              + ", ".join(o["id"] for o in report["obligations"]))
    _refresh_projection(programs, slug)
    return 0


def cmd_program_revise_objective(args) -> int:
    programs = _programs(args)
    slug = programs.resolve(args.program)
    before = programs.read(slug)["objective"]
    record = programs.revise_objective(slug, args.objective, args.reason)
    print(f"revised the objective of {record['program_id']}")
    print(f"  {args.reason}")
    print(f"\nPreviously: {before}")
    print(f"Now:        {record['objective']}")
    print("\nNothing was overwritten silently — `program show` lists every prior objective with "
          "when it was replaced and why.")
    _refresh_projection(programs, slug)
    return 0


def cmd_program_reopen(args) -> int:
    programs = _programs(args)
    slug = programs.resolve(args.program)
    was = programs.read(slug).get("closure") or {}
    record = programs.reopen(slug, args.reason)
    print(f"reopened {record['program_id']} — it was {was.get('state', 'closed')}")
    print(f"  {args.reason}")
    print("What was undone stays on the record: `program show` lists every closure that was "
          "reversed, with the reason for reversing it.")
    _refresh_projection(programs, slug)
    return 0


def _looks_like_plan_id(token: str) -> bool:
    return (token.startswith("pln_") and len(token) == 16
            and all(c in "0123456789abcdef" for c in token[4:]))


def _resolve_child_token(programs, token: str) -> str:
    """Turn a `--lane` member token into the plan_id the record stores.

    A plan_id passes through UNTOUCHED, and that matters: a child stored in the program but missing
    from this library is a real plan_id that will not resolve, and its honest refusal must come from
    `set_lanes` naming exactly that case — not a bare resolver error here. A non-id token (a slug or
    name) is resolved for the operator's convenience; if it will not resolve, it is passed through so
    `set_lanes` owns the message rather than this helper.
    """
    if _looks_like_plan_id(token):
        return token
    try:
        return programs.plans.read_record(programs.plans.resolve(token))["plan_id"]
    except plan_store.PlanStoreError:
        return token


def cmd_program_lanes_set(args) -> int:
    programs = _programs(args)
    slug = programs.resolve(args.program)
    lanes = []
    for spec in args.lane:
        if "=" not in spec:
            raise ProgramManagerError(
                f"--lane expects NAME=plan,plan,...; {spec!r} has no '=' to split the name from its "
                "members.")
        name, _, members = spec.partition("=")
        children = [_resolve_child_token(programs, token.strip())
                    for token in members.split(",") if token.strip()]
        lanes.append({"name": name.strip(), "children": children})
    record = programs.set_lanes(slug, lanes, args.reason)
    split = record["lanes"]
    print(f"recorded a {len(split['lanes'])}-lane split on {record['program_id']}")
    print(f"  {args.reason}")
    for lane in split["lanes"]:
        print(f"  {lane['name']}: {', '.join(lane['children'])}")
    if record.get("lanes_history"):
        print("\nThe split it replaced is kept in the lane history — `program show` lists every split "
              "that stopped standing, and whether it was replaced or cleared.")
    print("\nAdvisory only: this records which children may ride at once. It dispatches nothing, "
          "selects nothing, and gates no Build.")
    _refresh_projection(programs, slug)
    return 0


def cmd_program_lanes_clear(args) -> int:
    programs = _programs(args)
    slug = programs.resolve(args.program)
    record = programs.clear_lanes(slug, args.reason)
    print(f"cleared the lane split on {record['program_id']}")
    print(f"  {args.reason}")
    print("What was cleared is kept in the lane history — `program show` lists it, marked cleared.")
    _refresh_projection(programs, slug)
    return 0


_UNPLACEABLE_PROSE = {
    plan_program.ProgramLibrary.UNPLACEABLE_V1:
        "its plan carries a build-plan.v1 payload, which cannot express the exclusive_resources this "
        "reasons over — re-author it as build-plan.v2 to place it",
    plan_program.ProgramLibrary.UNPLACEABLE_IMPORTED:
        "its plan is an imported native plan with no authored work items, so it declares no territory "
        "to reason over yet",
    plan_program.ProgramLibrary.UNPLACEABLE_UNREADABLE:
        "its record or head document does not read, so its territory cannot be determined",
}


def cmd_program_lanes_propose(args) -> int:
    programs = _programs(args)
    slug = programs.resolve(args.program)
    proposal = programs.propose_lanes(slug, max_lanes=args.max_lanes, fresh=args.fresh)
    program_id = programs.read(slug)["program_id"]
    seal = proposal["seal_states"]
    lines = []
    if proposal["recorded_split_present"] and proposal["mode"] == "amend":
        lines.append(f"Proposing lanes for {program_id} — amending around the recorded split: its "
                     "lanes are fixed seeds and only unlaned children are placed. Nothing is written.")
    elif proposal["recorded_split_present"]:
        lines.append(f"Proposing lanes for {program_id} — --fresh: the recorded split is set aside "
                     "for this proposal only. Nothing is written.")
    else:
        lines.append(f"Proposing lanes for {program_id} — at most {proposal['max_lanes']} lane(s). "
                     "Nothing is written.")
    lines.append("")
    if proposal["lanes"]:
        lines.append("## Lanes")
        for lane in proposal["lanes"]:
            seed = " (recorded seed — membership preserved verbatim)" if lane["seed"] else ""
            members = ", ".join(f"{m} [{seal.get(m, 'recorded')}]" for m in lane["members"])
            lines.append(f"- **{lane['name']}**{seed}: {members}")
            lines.append(f"    territory: {', '.join(lane['territory']) or '(none read)'}")
    else:
        lines.append("_No child could be placed into a lane._")
    for lane in proposal["lanes"]:
        if lane.get("collides"):
            lines.append("")
            lines.append(f"**Concurrency is not recommended for the children in lane {lane['name']}.** "
                         "They collide on shared territory (" + ", ".join(lane["territory"]) + "), so "
                         "they are grouped to run in sequence rather than split into a manufactured "
                         "concurrency.")
    if proposal.get("cap_forced"):
        lines.append("")
        lines.append(f"_Note: the lane ceiling of {proposal['max_lanes']} was reached, so the following "
                     "otherwise-disjoint children were merged into an existing lane for lack of room — "
                     "NOT because of a territory collision. Raise --max-lanes to give them their own lane:_")
        for item in proposal["cap_forced"]:
            lines.append(f"- {item['plan_id']} → merged into lane {item['lane']}")
    if proposal["unplaced"]:
        lines.append("")
        lines.append("## Unplaced — contends with more than one open lane")
        for item in proposal["unplaced"]:
            lines.append(f"- {item['plan_id']} — contends with {', '.join(item['contends_with'])}; "
                         "placing it in either would leave a cross-lane collision.")
    if proposal["unplaceable"]:
        lines.append("")
        lines.append("## Unplaceable — why it could not be placed")
        for item in proposal["unplaceable"]:
            lines.append(f"- {item['plan_id']} — {_UNPLACEABLE_PROSE.get(item['class'], item['class'])}")
    if proposal["excluded"]:
        lines.append("")
        lines.append("## Excluded — with reason")
        for item in proposal["excluded"]:
            lines.append(f"- {item['plan_id']} — {item['reason']}")
    if proposal["cross_lane_edges"]:
        lines.append("")
        lines.append("## Cross-lane predecessor edges — merge-order risks")
        for edge in proposal["cross_lane_edges"]:
            lines.append(f"- {edge['child']} (lane {edge['child_lane']}) succeeds {edge['predecessor']} "
                         f"(lane {edge['predecessor_lane']}) — on different lanes, so the order they "
                         "merge in is a risk to watch.")
    if proposal["resource_cautions"]:
        lines.append("")
        lines.append("## Shared exclusive_resources — a caution, never a verdict")
        for caution in proposal["resource_cautions"]:
            lines.append(f"- `{caution['token']}` is declared by {', '.join(caution['children'])}: a "
                         "within-plan scheduling token whose cross-plan meaning is unproven. It does "
                         "not separate lanes on its own.")
    lines.append("")
    lines.append("_" + proposal["declared_paths_caveat"] + "_")
    if proposal["lanes"]:
        lane_args = " ".join(f"--lane {lane['name']}={','.join(lane['members'])}"
                             for lane in proposal["lanes"])
        lines.append("")
        lines.append("To record this recommendation (edit the reason to your own):")
        lines.append(f"  python tools/program_manager.py program lanes set {program_id} "
                     f"--reason \"<why>\" {lane_args}")
    print("\n".join(lines))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="program_manager.py",
        description="The program surface: multi-PR programs, their chains, their obligations, and the "
                    "operator's decided lane splits. Read verbs derive; nothing here auto-selects.")
    parser.add_argument("--library", help="path to the plan library (defaults to this instance's own)")
    sub = parser.add_subparsers(dest="command", required=True)

    program = sub.add_parser(
        "program", help="a multi-PR program: an ordered set of plans that carry obligations forward"
    ).add_subparsers(dest="program_command", required=True)

    program_new = program.add_parser("new", help="start a program")
    program_new.add_argument("--title", required=True)
    program_new.add_argument("--objective", required=True,
                             help="what the whole program delivers that no single child PR does")
    program_new.set_defaults(func=cmd_program_new)

    program_list = program.add_parser("list", help="every program, its children and what it still owes")
    program_list.set_defaults(func=cmd_program_list)

    program_portfolio = program.add_parser(
        "portfolio", help="every open program at a glance, qualitatively — goals, progress, what is in "
                          "flight (a pure read; nothing is selected or started)")
    program_portfolio.set_defaults(func=cmd_program_portfolio)

    program_reproject = program.add_parser(
        "reproject", help="regenerate every program's PROGRAM.md from its record (a pure projection; "
                          "writes no record) — the library-wide sweep that converges a stale file")
    program_reproject.set_defaults(func=cmd_program_reproject)

    program_show = program.add_parser("show", help="one program, its children and outstanding obligations")
    program_show.add_argument("program")
    program_show.set_defaults(func=cmd_program_show)

    program_add = program.add_parser(
        "add", help="append a plan, enforcing the carry-forward guarantee against its predecessor")
    program_add.add_argument("program")
    program_add.add_argument("plan")
    program_add.add_argument("--after", help="the plan this one succeeds; required after the first child")
    program_add.set_defaults(func=cmd_program_add)

    program_insert = program.add_parser(
        "insert", help="place a plan BEFORE an existing child, re-pointing the edge that reached it")
    program_insert.add_argument("program")
    program_insert.add_argument("plan")
    program_insert.add_argument("--before", required=True,
                                help="the child this plan is placed ahead of; it will succeed the "
                                     "inserted plan instead of what it succeeds now")
    program_insert.set_defaults(func=cmd_program_insert)

    program_supersede = program.add_parser(
        "supersede", help="replace a child that turned out wrong, keeping it and its place visible")
    program_supersede.add_argument("program")
    program_supersede.add_argument("plan", help="the child being replaced")
    program_supersede.add_argument("--with", dest="With", required=True,
                                   help="the plan that takes its place on the chain")
    program_supersede.add_argument("--reason", required=True,
                                   help="why — recorded as the replaced plan's retirement reason")
    program_supersede.set_defaults(func=cmd_program_supersede)

    program_release = program.add_parser(
        "release", help="let go of an obligation whose successors are all gone, with a reason")
    program_release.add_argument("program")
    program_release.add_argument("child", help="the child that CARRIES the obligation")
    program_release.add_argument("--obligation", required=True, dest="obligation")
    program_release.add_argument("--reason", required=True,
                                 help="why the debt is void — its whole price")
    program_release.set_defaults(func=cmd_program_release)

    for state, helptext in (("retire", "superseded, kept for the record"),
                            ("abandon", "deliberately dropped")):
        closer = program.add_parser(state, help=f"close a program: {helptext}")
        closer.add_argument("program")
        closer.add_argument("--reason", required=True)
        closer.add_argument("--acknowledge-unknown", dest="acknowledge_unknown",
                            help="close even though what this program owes cannot be computed from "
                                 "its record; the text is why, and it is recorded in the closure")
        closer.set_defaults(func=cmd_program_close,
                            state={"retire": "retired", "abandon": "abandoned"}[state])

    program_revise = program.add_parser(
        "revise-objective", help="replace the objective, keeping the text it replaced")
    program_revise.add_argument("program")
    program_revise.add_argument("--objective", required=True)
    program_revise.add_argument("--reason", required=True,
                                help="why the old wording stopped being true")
    program_revise.set_defaults(func=cmd_program_revise_objective)

    program_complete = program.add_parser(
        "complete", help="record that the objective is met — never derived, only recorded")
    program_complete.add_argument("program")
    program_complete.add_argument("--reason", required=True)
    program_complete.set_defaults(func=cmd_program_complete)

    program_reopen = program.add_parser(
        "reopen", help="undo a program closure — retired, abandoned or complete — keeping the record")
    program_reopen.add_argument("program")
    program_reopen.add_argument("--reason", required=True,
                                help="why the closure is being undone; kept in the closure history")
    program_reopen.set_defaults(func=cmd_program_reopen)

    program_lanes = program.add_parser(
        "lanes", help="record or withdraw the operator's DECIDED concurrency split (advisory only)"
    ).add_subparsers(dest="lanes_command", required=True)

    lanes_set = program_lanes.add_parser(
        "set", help="record the decided split — which children may ride which lane at once")
    lanes_set.add_argument("program")
    lanes_set.add_argument("--lane", action="append", required=True, metavar="NAME=plan,plan,...",
                           help="one lane: its name, then the plan ids riding it, comma-separated; "
                                "repeat --lane for each lane")
    lanes_set.add_argument("--reason", required=True,
                           help="why this split — stored on the split and kept when it later ends")
    lanes_set.set_defaults(func=cmd_program_lanes_set)

    lanes_clear = program_lanes.add_parser(
        "clear", help="withdraw the standing split, keeping it in the lane history")
    lanes_clear.add_argument("program")
    lanes_clear.add_argument("--reason", required=True,
                             help="why the split is being withdrawn; kept in the lane history")
    lanes_clear.set_defaults(func=cmd_program_lanes_clear)

    lanes_propose = program_lanes.add_parser(
        "propose", help="recommend a lane split on the engine's own conflict rule (a pure read)")
    lanes_propose.add_argument("program")
    lanes_propose.add_argument("--max-lanes", type=int, default=4, dest="max_lanes",
                               help="the lane ceiling (default 4, from the operator's own practice)")
    lanes_propose.add_argument("--fresh", action="store_true",
                               help="recompute from scratch, setting aside any recorded split for "
                                    "this proposal only")
    lanes_propose.set_defaults(func=cmd_program_lanes_propose)
    return parser


def main(argv: list | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ProgramManagerError as exc:
        print(f"program-manager: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
