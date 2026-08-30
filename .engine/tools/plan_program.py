#!/usr/bin/env python3
"""Multi-PR programs: an ordered set of plans, and one mechanical guarantee about what they owe.

A plan is per-Build — one plan, one seal, one pull request. Work that genuinely spans several PRs was,
before this, a shelf of related plans held together by whoever remembered. The failure that produces
is obligation decay: PR A promises that PR B will finish something, PR B never mentions it, and
nothing anywhere notices. Three successive drafts of the plan that produced THIS module decayed
exactly that way, which is the evidence the object rests on.

The guarantee is deliberately narrow, and stating it precisely matters more than making it sound
impressive:

    An obligation a plan declares it is CARRYING cannot vanish from its successor.
    It is satisfied, re-declared as carried, or released with a stated reason.

That is the whole of it. This module does not judge whether the decomposition into PRs was wise,
whether the order is right, or whether a release was justified. Those are judgment, and a mechanism
that pretended to check them would launder them — an operator would read a passing check as approval
of a decision nothing actually examined. What it does instead is make dropping an obligation require
someone to say so, in writing, which is the smallest thing that turns a silent failure into a visible
one.

It never auto-selects a current child either. A program is a sequence someone decided on, not a queue
something pops from.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import secrets

import build_coordinator_core as core
import moment
import plan_store

ProgramError = plan_store.PlanStoreError

ROOT = Path(__file__).resolve().parents[2]
PROGRAM_SCHEMA = ROOT / ".engine" / "schemas" / "engine-program.v1.json"

PROGRAMS_DIRNAME = "programs"
RECORD_FILENAME = "record.json"

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*--[0-9a-f]{6}$")


_now = moment.utc_now


def mint_program_id() -> str:
    return "prg_" + secrets.token_hex(6)


def carried_forward(document: dict) -> dict:
    """The obligations this plan hands to its successor: id -> obligation, `carried` state only.

    `satisfied` stops here by definition, and `released` was deliberately let go with a reason on the
    record. Only `carried` creates a debt the next plan must answer for.
    """
    program = document.get("program") or {}
    return {o["id"]: o for o in program.get("carried_obligations", []) if o["state"] == "carried"}


def unexplained_releases(document: dict) -> list:
    """Every obligation this plan RELEASES without saying why.

    The carry-forward guarantee has exactly one escape hatch — release it, and state a reason — so an
    unexplained release is the guarantee failing in the only way it can. `released` is also the state
    a plan reaches for when an obligation is inconvenient, which is precisely when the reason matters
    most and is likeliest to be skipped, and the projection used to print `(no reason given)` and
    carry on. The schema refuses this shape too; this is the same rule where the program can see it,
    so a release that never went through validation still cannot enter the chain at add_child.
    """
    program = document.get("program") or {}
    return [o for o in program.get("carried_obligations", [])
            if o["state"] == "released" and not (o.get("reason") or "").strip()]


def dropped_obligations(predecessor: dict, successor: dict, *, released=()) -> list:
    """Obligations the predecessor was carrying that the successor does not mention at all.

    Not mentioning one is the failure this object exists for — it is indistinguishable, from the
    outside, from having decided it no longer matters, except that nobody decided anything.

    `released` is the set of obligation ids let go at PROGRAM level AT THE PREDECESSOR. It is an
    answer, not an exemption: someone stated a reason on the record, which is the same price the
    in-plan release has always cost. Passed in rather than read here because this function sees two
    documents and no program, and the keying — which CHILD the release was granted at — is exactly
    the part that must not be lost.
    """
    successor_program = successor.get("program") or {}
    named = {o["id"] for o in successor_program.get("carried_obligations", [])}
    released = set(released)
    return [obligation for identifier, obligation in sorted(carried_forward(predecessor).items())
            if identifier not in named and identifier not in released]


DEAD_BRANCH_STATES = ("retired", "abandoned")


def superseded_children(record: dict) -> set:
    """Child ids whose supersession marker names a child that actually exists on this record.

    Every suppression keyed on `superseded_by` goes through here, because the marker is trusted to
    mean "a replacement stands in this child's place" — and a dangling marker, one naming a plan
    that is not a child, carries no replacement to stand anywhere. Trusting it suppressed both the
    needs-attention alarm and the unknown entry for a missing child while nothing anywhere named
    the dangle. Only reachable through legacy or hand-edited records — `mark_superseded` always
    adds the replacement — which is exactly where the alarms matter most.
    """
    ids = {child["plan_id"] for child in record["children"]}
    return {child["plan_id"] for child in record["children"]
            if child.get("superseded_by") in ids}


def way_through_for(plan_id: str, status: str, sealed: bool) -> str:
    """What the operator can actually DO about a child that cannot answer for an obligation.

    One owner, because two callers each worked it out for themselves and both got the same case
    wrong: they tested `bool(record["seal"])` and named supersede, but an ACTIVE plan carries a seal
    too — a Build is bound to it — and supersede refuses an active target. So the refusal named a
    real verb that would refuse the moment it was run, which is the dead-end shape this change
    exists to close, arriving a third time.

    A plan that is still open can simply be revised. A sealed one cannot — a seal is terminal — so
    it is replaced. An active one cannot be replaced either until its Build stops, and saying so is
    the difference between a way through and a door that opens onto a wall.
    """
    if status == "active":
        # The two outcomes of that Build lead to DIFFERENT doors, and saying only "let it merge, or
        # abandon it, then supersede" was this function's own defect arriving inside the fix for it:
        # a merged Build makes the plan complete, and supersede refuses a complete target flat. So
        # each route names the door that is actually open at the end of it.
        return (f"\nA Build is bound to {plan_id} right now, so it can be neither revised (its seal "
                "is terminal) nor superseded (superseding a plan with a Build running would strand "
                "it). Two ways on, and they end somewhere different: ABANDON that Build, and "
                f"`program supersede {plan_id}` then works. Or let it MERGE — after which the plan "
                "is complete and merged history is not replaced but CORRECTED, so the way through "
                "becomes appended work: `program add --after` a new plan that answers.")
    if status == "complete":
        # A complete plan carries a seal, so it fell into the sealed branch below and was told to
        # supersede — which refuses a complete target flat. Unlike the sealed case there is no
        # precondition that opens supersede here: merged history is never replaced. The door that
        # does open is the one the complete-refusal already names, and now this names it too.
        return (f"\n{plan_id} is complete — its pull request has merged, and merged history is not "
                "replaced but CORRECTED. Nothing can make it answer retrospectively. Append the "
                "work instead: `program add --after` a new plan that answers for these, placed "
                "after the last child on that branch.")
    if sealed:
        return (f"\nThat plan is SEALED, and a seal is terminal, so it cannot be revised to answer "
                f"for them. Replace it: `program supersede {plan_id}` with a plan that does.")
    return (f"\nRevise {plan_id} so each appears in its carried_obligations as satisfied, still "
            "carried, or released with a reason, then try again.")


def chain_analysis(record: dict) -> dict:
    """Order the children by their DECLARED predecessor edges, and name every anomaly found.

    `position` is NOT consulted, and is no longer written either. It was display-only — a stored
    label nothing read — so ordering by it made the stored numbering authoritative over the edges
    that actually record the decision. The edges are the decision; the number was a label, and
    insert is the verb that would have had to invent one, so it died instead of being renumbered.
    Legacy records still carrying it stay valid. Siblings of one fork tie-break on (added_at,
    plan_id), never on position, so a record whose numbering has been permuted or duplicated still
    renders in the order its edges declare.

    EVERY stored child appears in `order` exactly once. A child unreachable from a root — because its
    predecessor edge dangles, or because it sits in a cycle — is appended and named in `unreachable`
    rather than dropped, holding the invariant `child_view` was written for: quietly omitting a child
    would make the program look shorter than it is, and a corrupt record is the case where that lie
    would matter most.
    """
    children = record["children"]
    by_id = {child["plan_id"]: child for child in children}

    def key(child):
        return (child.get("added_at", ""), child["plan_id"])

    ordered_children = sorted(children, key=key)
    successors: dict = {}
    dangling = []
    for child in ordered_children:
        predecessor = child.get("predecessor_plan_id")
        if predecessor is None:
            continue
        if predecessor not in by_id:
            dangling.append({"plan_id": child["plan_id"], "predecessor_plan_id": predecessor})
            continue
        successors.setdefault(predecessor, []).append(child["plan_id"])

    roots = [child["plan_id"] for child in ordered_children if not child.get("predecessor_plan_id")]
    order, seen = [], set()
    stack = list(reversed(roots))
    while stack:                      # iterative depth-first: a deep chain cannot blow the stack,
        plan_id = stack.pop()         # and each branch renders contiguously rather than interleaved
        if plan_id in seen:
            continue
        seen.add(plan_id)
        order.append(plan_id)
        for successor in reversed(successors.get(plan_id, [])):
            stack.append(successor)
    unreachable = [child["plan_id"] for child in ordered_children if child["plan_id"] not in seen]
    order.extend(unreachable)

    return {
        "order": order,
        "roots": roots,
        "forks": [{"predecessor_plan_id": predecessor, "successors": list(names)}
                  for predecessor, names in sorted(successors.items()) if len(names) > 1],
        "dangling": dangling,
        "unreachable": unreachable,
        # A leaf is a child no other child declares as its predecessor: where a branch of the chain
        # currently ends, and therefore where an unanswered obligation still sits.
        "leaves": [child["plan_id"] for child in ordered_children
                   if child["plan_id"] not in successors],
    }


class ProgramLibrary:
    """Programs live beside plans, in the same durable library and under the same lock discipline."""

    def __init__(self, plans: plan_store.PlanLibrary):
        self.plans = plans
        self.root = plans.root / PROGRAMS_DIRNAME

    def program_dir(self, slug: str) -> Path:
        return self.root / slug

    def _record_path(self, slug: str) -> Path:
        return self.program_dir(slug) / RECORD_FILENAME

    def slugs(self) -> list:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir()
                      if p.is_dir() and _SLUG_RE.match(p.name) and (p / RECORD_FILENAME).is_file())

    def resolve(self, selector: str) -> str:
        """Same rule as plans: full id, unique prefix, or slug, and nothing auto-selects."""
        selector = (selector or "").strip()
        if not selector:
            raise ProgramError("name a program by id, unique id prefix, or slug; "
                               "nothing is selected by default")
        available = self.slugs()
        if selector in available:
            return selector
        records = {slug: core.json_file(self._record_path(slug)) for slug in available}
        exact = [s for s, r in records.items() if r["program_id"] == selector]
        if exact:
            return exact[0]
        prefix = [s for s, r in records.items() if r["program_id"].startswith(selector)]
        if len(prefix) == 1:
            return prefix[0]
        if len(prefix) > 1:
            raise ProgramError(f"{selector!r} matches {len(prefix)} programs: "
                               + ", ".join(sorted(records[s]["program_id"] for s in prefix)))
        raise ProgramError(f"no program matches {selector!r}"
                           + ("; the library holds no programs." if not available
                              else "; it holds: " + ", ".join(available)))

    def read(self, slug: str) -> dict:
        record = core.json_file(self._record_path(slug))
        core.validate(record, PROGRAM_SCHEMA)
        return record

    def _write(self, slug: str, record: dict) -> None:
        core.validate(record, PROGRAM_SCHEMA)
        core.atomic_write(self._record_path(slug),
                          json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                          durable=True, mode=plan_store.FILE_MODE)

    def create(self, title: str, objective: str) -> str:
        program_id = mint_program_id()
        slug = plan_store.slug_for(title, program_id)
        plan_store.ensure_dir(self.program_dir(slug), within=self.plans.root)
        # Under the lock, like every other mutating path here. This method previously took none at
        # all while the class docstring claimed it did — and checked existence before writing, which
        # is a check-then-act race even once a lock exists. Both halves are fixed: the lock is held,
        # and the check happens inside it.
        with core.exclusive_lock(self.program_dir(slug) / (RECORD_FILENAME + ".lock")):
            if self._record_path(slug).exists():
                raise ProgramError(f"a program already exists at {self.program_dir(slug)}")
            self._write(slug, {
                "schema_version": "engine-program.v1",
                "program_id": program_id,
                "slug": slug,
                "title": title,
                "objective": objective,
                "created_at": _now(),
                "children": [],
                "closure": None,
            })
        return slug

    def add_child(self, slug: str, plan_selector: str, *, predecessor: str | None = None) -> dict:
        """Append a plan to the program, enforcing the carry-forward guarantee against its predecessor.

        The predecessor is DECLARED, not inferred from array position, so re-ordering the record can
        never silently re-point what a successor is answerable to.
        """
        with core.exclusive_lock(self.program_dir(slug) / (RECORD_FILENAME + ".lock")):
            record = self.read(slug)
            if record.get("closure"):
                raise ProgramError(f"this program is {record['closure']['state']}; reopen it first")
            plan_slug, plan_id = self._joinable(record, plan_selector)

            predecessor_id = None
            if record["children"]:
                if predecessor is None:
                    raise ProgramError(
                        "this program already has children, so a new one must declare which plan it "
                        "succeeds — that declaration is what the carry-forward check reads. Name it "
                        "with --after.")
                predecessor_id = self.plans.read_record(self.plans.resolve(predecessor))["plan_id"]
                if not any(child["plan_id"] == predecessor_id for child in record["children"]):
                    raise ProgramError(f"{predecessor_id} is not a child of this program")
                dropped = dropped_obligations(
                    self.plans.head(self.plans.resolve(predecessor_id)),
                    self.plans.head(plan_slug),
                    released=self.released_at(record, predecessor_id))
                if dropped:
                    raise ProgramError(
                        f"{plan_id} does not answer for {len(dropped)} obligation(s) that "
                        f"{predecessor_id} declared it was carrying:\n"
                        + "\n".join(f"  - {o['id']}: {o['statement']}" for o in dropped)
                        + "\nEach must appear in the successor's carried_obligations as satisfied, "
                          "still carried, or released with a reason. Dropping one silently is the "
                          "decay this program object exists to prevent.")
            elif predecessor is not None:
                raise ProgramError("the first child of a program has no predecessor to declare")

            child = {"plan_id": plan_id, "added_at": _now()}
            if predecessor_id:
                child["predecessor_plan_id"] = predecessor_id
            record["children"].append(child)
            self._write(slug, record)
            return record

    def _joinable(self, record: dict, plan_selector: str) -> tuple:
        """The checks every join makes, whichever door it came through: `add` or `insert`.

        Extracted when `insert` arrived rather than copied into it. These three rules — not already a
        child, the back-link is present, no release goes unexplained — are properties of JOINING a
        program, not of appending to it, and a second copy of them is a second place for them to drift.
        The carry-forward comparison is deliberately NOT here: `add` checks one edge and `insert`
        checks two, and which edges exist is exactly what differs between the doors.
        """
        plan_slug = self.plans.resolve(plan_selector)
        plan_id = self.plans.read_record(plan_slug)["plan_id"]
        if any(child["plan_id"] == plan_id for child in record["children"]):
            raise ProgramError(f"{plan_id} is already a child of this program")

        # The back-link is load-bearing, so it is required at the join rather than hoped for. It
        # is the ONLY evidence of membership that survives a program record which will not parse:
        # without it, a plan whose own program is corrupt is indistinguishable from a standalone
        # plan, and its seal would skip the carry-forward re-check entirely. Required going
        # forward only — children added before this cannot be retro-fitted without revising
        # sealed plans, and that residual gap is disclosed at their seals instead.
        declared = (self.plans.head(plan_slug).get("program") or {}).get("program_id")
        if declared != record["program_id"]:
            sealed = bool(self.plans.read_record(plan_slug).get("seal"))
            raise ProgramError(
                f"{plan_id} does not declare that it belongs to this program. Its document must "
                f"carry `program.program_id` = {record['program_id']}"
                + (f", and it currently says {declared}." if declared else ".")
                + " That back-link is what lets this plan's seal find its program even when the "
                  "program record cannot be read, so it is required before the plan can join."
                + (" This plan is already SEALED, and a seal is terminal, so the back-link can "
                   "no longer be added to it. The way through is three steps, and the middle one "
                   "is easy to miss: `clone` it, then `revise` the CLONE to add "
                   f"`program.program_id` = {record['program_id']} — a clone deliberately carries "
                   "no program block, because it carries none of the original's evidence either — "
                   "then add the clone here. A plan is normally added to its program before it is "
                   "sealed, which is when this is a one-line revision."
                   if sealed else
                   " Revise the plan to add it, then add it here."))

        # Checked for EVERY child, including the first, and before the carry-forward comparison.
        # A release is a decision to stop answering for something, and it costs a reason wherever
        # it is made — the first plan in a program can release an obligation it inherited from
        # outside the program just as a later one can, and there is no predecessor to catch it.
        unexplained = unexplained_releases(self.plans.head(plan_slug))
        if unexplained:
            raise ProgramError(
                f"{plan_id} releases {len(unexplained)} obligation(s) without saying why:\n"
                + "\n".join(f"  - {o['id']}: {o['statement']}" for o in unexplained)
                + "\nReleasing is allowed and sometimes right, but the stated reason is its whole "
                  "price: it is what lets a later reader tell a decision from an omission. Record "
                  "why each was let go, or carry it.")
        return plan_slug, plan_id

    def insert_child(self, slug: str, plan_selector: str, *, before: str) -> dict:
        """Place a plan AHEAD of an existing child, re-pointing the edge that used to reach it.

        This is the door the program object was missing. `add_child` can only append after an
        existing child, so work that turns out to belong BEFORE work already on the chain had no
        honest way in: the only route was to abandon the tail and re-add it in the new order, which
        destroys the decision record the chain exists to keep and orphans every obligation the
        abandoned children were carrying. That is the hostage problem, and this is its answer.

        Exactly two edges move, and both are re-checked for carry-forward, because an insertion
        creates two new answerabilities rather than one:

            before:   predecessor -> displaced
            after:    predecessor -> inserted -> displaced

        The inserted plan must answer for what the predecessor carries (it now stands between them),
        and the displaced plan must answer for what the INSERTED plan carries (it no longer succeeds
        what it used to). Nothing is renumbered: `position` is not written, and the order every reader
        derives comes from these edges.

        Two refusals, and each names a way through that is itself open:

        - The displaced child's plan is COMPLETE. That is merged history, and history is corrected by
          appended work, never by inserting ahead of it. Refused flat.
        - The displaced child cannot answer for what the inserted plan carries. If it is still open,
          revise it. If it is SEALED, a seal is terminal and revision is not available — the way
          through is to supersede it, which is a different verb and a different decision.
        """
        with core.exclusive_lock(self.program_dir(slug) / (RECORD_FILENAME + ".lock")):
            record = self.read(slug)
            if record.get("closure"):
                raise ProgramError(f"this program is {record['closure']['state']}; reopen it first")
            plan_slug, plan_id = self._joinable(record, plan_selector)

            displaced_id = self.plans.read_record(self.plans.resolve(before))["plan_id"]
            displaced = next((child for child in record["children"]
                              if child["plan_id"] == displaced_id), None)
            if displaced is None:
                raise ProgramError(f"{displaced_id} is not a child of this program")
            if displaced_id == plan_id:
                raise ProgramError("a plan cannot be inserted before itself")

            displaced_record = self.plans.read_record(self.plans.resolve(displaced_id))
            displaced_status = plan_store.derived_status(displaced_record)
            if displaced_status == "complete":
                raise ProgramError(
                    f"{displaced_id} is complete — its pull request is merged, and inserting ahead "
                    "of merged history would claim work landed in an order it did not. History is "
                    "corrected by APPENDED work: add the new plan after the last child on this "
                    "branch with `program add --after`, and let it say what it changes about what "
                    "already shipped.")

            inherited = displaced.get("predecessor_plan_id")
            inserted_head = self.plans.head(plan_slug)

            # Edge one: the inserted plan now stands between the predecessor and the displaced child,
            # so it answers for what the predecessor carries. Same comparison `add_child` makes, on
            # the edge this verb creates rather than on an appended one.
            if inherited:
                dropped = dropped_obligations(
                    self.plans.head(self.plans.resolve(inherited)), inserted_head,
                    released=self.released_at(record, inherited))
                if dropped:
                    raise ProgramError(
                        f"{plan_id} does not answer for {len(dropped)} obligation(s) that "
                        f"{inherited} declared it was carrying, and inserting it here makes it the "
                        f"plan that must:\n"
                        + "\n".join(f"  - {o['id']}: {o['statement']}" for o in dropped)
                        + f"\nRevise {plan_id} so each appears in its carried_obligations as "
                          "satisfied, still carried, or released with a reason, then insert it.")

            # Edge two: the displaced child stops succeeding what it used to and starts succeeding
            # the inserted plan, so it answers for what the INSERTED plan carries. This is the edge
            # an append never creates, and skipping it would let an insertion mint a debt nothing
            # downstream ever had to answer for — the decay this object exists to prevent, arriving
            # through the new door.
            # A retired or abandoned displaced child answers for nothing and is owed nothing —
            # the same call supersede's downstream check makes, and made here too so the two doors
            # do not demand different things of the same dead plan.
            dropped = ([] if displaced_status in DEAD_BRANCH_STATES else
                       dropped_obligations(inserted_head,
                                           self.plans.head(self.plans.resolve(displaced_id))))
            if dropped:
                raise ProgramError(
                    f"{displaced_id} would succeed {plan_id} once this insertion lands, and it does "
                    f"not answer for {len(dropped)} obligation(s) that {plan_id} declares it is "
                    f"carrying:\n"
                    + "\n".join(f"  - {o['id']}: {o['statement']}" for o in dropped)
                    + way_through_for(displaced_id, displaced_status,
                                      bool(displaced_record.get("seal"))))

            child = {"plan_id": plan_id, "added_at": _now()}
            if inherited:
                child["predecessor_plan_id"] = inherited
            displaced["predecessor_plan_id"] = plan_id
            record["children"].append(child)
            self._write(slug, record)
            return record

    # -- program-level releases --
    #
    # An obligation is normally released INSIDE a successor plan, with a reason, and that stays the
    # ordinary door. This one exists for the shape that has no successor left to revise: a completed
    # child carrying debts whose successors were every one of them abandoned. Seals are terminal and
    # abandoned plans are closed, so there is nothing left to write the release into — and the debt
    # sits outstanding forever against work someone consciously decided was void. The only honest
    # surface remaining is this record.
    #
    # It is keyed to (child, obligation) rather than released program-wide. On a forked chain the same
    # obligation id can be owed on more than one branch, and a release granted because ONE branch died
    # must not clear the debt the live branch still owes. That keying is the whole difference between
    # a decision and a silent drop.

    @staticmethod
    def released_at(record: dict, child_plan_id: str) -> set:
        """The obligation ids let go at program level AT this child. Empty for every other child."""
        return {entry["obligation_id"] for entry in record.get("releases", [])
                if entry["child_plan_id"] == child_plan_id}

    def release(self, slug: str, child_selector: str, obligation_id: str, reason: str) -> dict:
        """Let go of one obligation at one child, on a stated reason. Works on a closed program.

        A closed program may still be CORRECTED — that is what this is, and refusing it would leave
        a retired program's books permanently wrong with no door. What a closed program may not take
        is new structure, and the structural verbs still refuse.
        """
        if not (reason or "").strip():
            raise ProgramError(
                "a release costs a reason — that is its whole price, and what lets a later reader "
                "tell a decision from an omission.")
        with core.exclusive_lock(self.program_dir(slug) / (RECORD_FILENAME + ".lock")):
            record = self.read(slug)
            child_id = self.plans.read_record(self.plans.resolve(child_selector))["plan_id"]
            if not any(child["plan_id"] == child_id for child in record["children"]):
                raise ProgramError(f"{child_id} is not a child of this program")
            if obligation_id in self.released_at(record, child_id):
                return record                       # idempotent: already let go, with its reason
            if not any(o["id"] == obligation_id
                       for o in carried_forward(self.plans.head(self.plans.resolve(child_id))).values()):
                raise ProgramError(
                    f"{child_id} does not carry an obligation called {obligation_id}. Program-level "
                    "release answers for what a child DECLARES it is carrying; run `program show` to "
                    "see what each child actually owes.")

            # The precondition that keeps this from becoming the easy door around answering. If a
            # live child succeeds the carrier, the ordinary route is open: that successor answers for
            # the debt in its own document, where the decision is read alongside the work. This verb
            # is for the case where no such successor can exist.
            view = {child["plan_id"]: child for child in self.child_view(record)}
            marked = superseded_children(record)
            successors = [child["plan_id"] for child in record["children"]
                          if child.get("predecessor_plan_id") == child_id
                          and child["plan_id"] not in marked]
            # Fail CLOSED on a successor whose state cannot be told. "Missing or unreadable" is
            # not "unable to answer" — it may be a perfectly revisable draft behind a broken
            # record — and this verb exists to be the hard door. The closure verbs keep their
            # acknowledged-unknown exit if the record stays broken; this one waits for a repair.
            untellable = [plan_id for plan_id in successors
                          if view.get(plan_id, {}).get("status") in ("missing", "unreadable")]
            if untellable:
                raise ProgramError(
                    f"{', '.join(untellable)} succeed(s) {child_id}, and whether they could still "
                    f"answer {obligation_id} cannot be told: their records are missing or will not "
                    "read. Repair is the door: a damaged record can be hand-fixed, and a missing "
                    "plan restored with `project_manager.py import` (from a bundle) or re-minted "
                    "with `init --document` — after which this release can tell what it is "
                    "releasing over, and either refuses honestly or proceeds.")
            live_successors = [
                plan_id for plan_id in successors
                if view.get(plan_id, {}).get("status") not in DEAD_BRANCH_STATES]
            # A successor that can still be REVISED must answer instead — but a sealed successor
            # cannot: a seal is terminal, so a debt its predecessor minted after that seal has no
            # revision left to land in. Treating a sealed successor as "somewhere to be answered"
            # was the wedge the operator ruled on: the close gate refused over the decayed debt,
            # this refusal pointed at a successor that could never take it, and the program was
            # permanently unclosable. So only a successor still open to revision blocks this door.
            revisable = []
            for successor_id in live_successors:
                try:
                    if not self.plans.read_record(self.plans.resolve(successor_id)).get("seal"):
                        revisable.append(successor_id)
                except Exception as exc:  # noqa: BLE001 — fail CLOSED: unreadable is not "unable"
                    # A successor whose record will not read might be a perfectly revisable draft,
                    # and this verb exists to be the hard door. Refusing names the actual problem;
                    # the closure verbs still have their acknowledged-unknown exit if the record
                    # stays broken.
                    raise ProgramError(
                        f"{successor_id} succeeds {child_id}, and whether it could still answer "
                        f"{obligation_id} cannot be told: its record does not read ({exc}). Repair "
                        "that record before releasing over it.")
            if revisable:
                raise ProgramError(
                    f"{child_id} still has a live successor that can be revised — "
                    + ", ".join(revisable)
                    + f" — so {obligation_id} has somewhere to be answered. Release it there, in that "
                      "plan's own carried_obligations with a reason, where the decision is read "
                      "beside the work. This verb is for a debt no successor can answer any more.")

            record.setdefault("releases", []).append({
                "child_plan_id": child_id, "obligation_id": obligation_id,
                "at": _now(), "reason": reason})
            self._write(slug, record)
            return record

    # Supersede is TWO records — the replaced plan's, and this program's — and two records cannot be
    # written atomically: two files, two locks. So the two halves are split deliberately and ordered,
    # and the order is the whole safety argument rather than an implementation detail:
    #
    #   1. `supersede_check` refuses, reading only.
    #   2. The command layer RETIRES the replaced plan through the Project Manager's own close path.
    #   3. `mark_superseded` re-checks under the program lock and writes the program record.
    #
    # Step 2 is what stops the replaced plan being bindable, and it happens FIRST. Every crash window
    # therefore leaves `retired-but-unmarked` — a plan that is out of play and a program record that
    # has not yet noticed — and never `marked-but-bindable`, which would be a loaded gun on the shelf
    # under a record claiming it had been put away. Re-running converges from the half state.
    #
    # The program lock is NEVER held across step 2. plan_program does not write the plan library at
    # all (a mechanical AST allowlist pins that seam), and holding this lock across a call that takes
    # the plan lock would invert the two locks' order and invite a deadlock with any session going the
    # other way. Nothing may reverse this.

    def supersede_check(self, slug: str, superseded_selector: str,
                        replacement_selector: str) -> dict:
        """Everything supersede refuses on, decided before ANY record is written. Reads only.

        Returns the resolved ids and, in `already`, whether this supersession is already recorded —
        the caller re-runs a half-completed one rather than treating it as an error.
        """
        record = self.read(slug)
        if record.get("closure"):
            raise ProgramError(f"this program is {record['closure']['state']}; reopen it first")

        superseded_id = self.plans.read_record(self.plans.resolve(superseded_selector))["plan_id"]
        child = next((c for c in record["children"] if c["plan_id"] == superseded_id), None)
        if child is None:
            raise ProgramError(f"{superseded_id} is not a child of this program")
        replacement_slug = self.plans.resolve(replacement_selector)
        replacement_id = self.plans.read_record(replacement_slug)["plan_id"]
        if replacement_id == superseded_id:
            raise ProgramError("a plan cannot supersede itself")

        already = child.get("superseded_by") == replacement_id
        if child.get("superseded_by") and not already:
            raise ProgramError(
                f"{superseded_id} was already superseded by {child['superseded_by']}. Supersede that "
                "plan instead, so the chain records one replacement after another rather than two "
                "plans claiming the same place.")

        superseded_record = self.plans.read_record(self.plans.resolve(superseded_id))
        status = plan_store.derived_status(superseded_record)
        if status == "complete":
            raise ProgramError(
                f"{superseded_id} is complete — its pull request is merged. Merged history is not "
                "replaced, it is CORRECTED by appended work: add a new plan after the last child on "
                "this branch with `program add --after`, and let it say what it changes about what "
                "already shipped.")
        if status == "active":
            raise ProgramError(
                f"{superseded_id} is ACTIVE — a Build is bound to it right now, and retiring the plan "
                "underneath a running Build strands it: it would go on publishing from a retired plan, "
                "and its completion could never be recorded afterwards. ABANDON that Build and this "
                "supersede then works — or let it MERGE, after which the plan is complete and merged "
                "history is corrected by appended work (`program add --after`), never replaced.")
        if not superseded_record.get("seal") and not already:
            # The requirement binds every target not yet MARKED, closed or not. The first cut
            # exempted any closed target, reasoning that supersede's own crash debris had to
            # converge — but under this very check a genuine half-state's target is always sealed
            # (the first run refused it otherwise), so the exemption never served convergence. What
            # it actually admitted, a cold reviewer proved: retire an unsealed draft for unrelated
            # reasons, and supersede would then mark it replaced — a plan that was never terminal
            # recorded as superseded, and `reopen` refusing it forever after.
            raise ProgramError(
                f"{superseded_id} is not sealed — supersede exists for a plan a seal has made "
                "terminal, and this one never was. An open draft is revised: edit the plan itself. "
                f"A closed draft is reopened or left closed: `reopen {superseded_id}` if its "
                "closure was wrong, or add the replacement with `program add`/`program insert` "
                "and let this one stand as the record tells it.")

        inherited = child.get("predecessor_plan_id")
        if not already:
            # Supersede is a JOIN. It was the only door that did not say so, and the omission was a
            # silent drop: the carry-forward comparison below used to be nested inside a test for
            # the replacement's back-link, so a replacement declaring no program — or a different
            # one — skipped the comparison entirely and was joined anyway, taking its predecessor's
            # debt out of the program's books with it. Reproduced before this line existed: the
            # obligation vanished from `obligation_report` and the program then closed clean.
            #
            # So the same checks every other door runs, run here, and the back-link case refuses
            # with the message that actually explains it rather than passing vacuously.
            if any(c["plan_id"] == replacement_id for c in record["children"]):
                raise ProgramError(
                    f"{replacement_id} is already a child of this program. A replacement JOINS at "
                    f"the place {superseded_id} is giving up; a plan already on the chain cannot "
                    "also take another's place, because it would then sit in two positions at once. "
                    "Clone it into a new plan if the same work is meant to stand in both places.")
            self._joinable(record, replacement_selector)

            # The replacement inherits the replaced child's predecessor edge, so it inherits the
            # answerability that came with it. Checked here, before anything is retired, because a
            # refusal after step 2 would leave a plan out of play for a supersession that never
            # landed. Unconditional: there is no shape of replacement this may be skipped for.
            if inherited:
                dropped = dropped_obligations(
                    self.plans.head(self.plans.resolve(inherited)),
                    self.plans.head(replacement_slug),
                    released=self.released_at(record, inherited))
                if dropped:
                    raise ProgramError(
                        f"{replacement_id} would take {superseded_id}'s place after {inherited}, and "
                        f"it does not answer for {len(dropped)} obligation(s) that {inherited} "
                        f"declares it is carrying:\n"
                        + "\n".join(f"  - {o['id']}: {o['statement']}" for o in dropped)
                        + f"\nA replacement inherits the place AND the debt. `clone --supersedes "
                          f"{superseded_id}` pre-fills exactly this set from the predecessor, which is "
                          "the honest source: the plan being replaced never landed, so its own claims "
                          "about what it satisfied describe work that does not exist.")
            # Edge two, and supersede owes it for exactly the reason insert does: everything that
            # succeeded the replaced child is about to succeed the REPLACEMENT instead, so each of
            # them must answer for what the replacement carries. Checking only the inherited edge
            # was the round-1 defect wearing its other face — a replacement that declares a new
            # obligation had it vanish from the report the moment the downstream edge moved, with
            # `program show` reading "None outstanding" over a live, unanswered debt. Reproduced
            # before this existed. The decay sweep did notice, but decay is a warning printed once
            # to a terminal; the report is the surface a close and a completion gate on.
            replacement_head = self.plans.head(replacement_slug)
            for other in record["children"]:
                if other["plan_id"] in (superseded_id, replacement_id):
                    continue
                if other.get("predecessor_plan_id") != superseded_id \
                        or other["plan_id"] in superseded_children(record):
                    continue
                try:
                    downstream_record = self.plans.read_record(self.plans.resolve(other["plan_id"]))
                    downstream_status = plan_store.derived_status(downstream_record)
                    downstream_head = self.plans.head(self.plans.resolve(other["plan_id"]))
                except Exception:  # noqa: BLE001 — an unreadable child is reported by other readers
                    continue
                if downstream_status in DEAD_BRANCH_STATES:
                    continue      # a dead successor answers for nothing, and is owed nothing
                # No `released=` here, and the omission is deliberate: a release is refused
                # unless its target is already a child, and supersede refuses a replacement that
                # already is one, so the set would be empty on every path reaching this line.
                # Passing it anyway would be inert code shaped like a guard — the same thing removed
                # from the display reports a round ago, and the same argument applies.
                dropped = dropped_obligations(replacement_head, downstream_head)
                if not dropped:
                    continue
                raise ProgramError(
                    f"{other['plan_id']} succeeds {superseded_id} today, so this supersession would "
                    f"move it onto {replacement_id} — and it does not answer for "
                    f"{len(dropped)} obligation(s) that {replacement_id} declares it is carrying:\n"
                    + "\n".join(f"  - {o['id']}: {o['statement']}" for o in dropped)
                    + way_through_for(other["plan_id"], downstream_status,
                                      bool(downstream_record.get("seal"))))

        return {"superseded_id": superseded_id, "replacement_id": replacement_id,
                "replacement_slug": replacement_slug, "inherited": inherited, "already": already}

    def mark_superseded(self, slug: str, superseded_selector: str,
                        replacement_selector: str) -> dict:
        """The program record's half of a supersession. Step 3; the plan is already retired.

        Idempotent: re-running a supersession that is already recorded returns the record unchanged,
        which is what lets a crash between the two writes be repaired by simply running the verb again.
        """
        with core.exclusive_lock(self.program_dir(slug) / (RECORD_FILENAME + ".lock")):
            # Re-derived under the lock. The pre-check ran outside it and against a record another
            # session may have moved since; a check whose result is trusted across a lock boundary is
            # not a check.
            resolved = self.supersede_check(slug, superseded_selector, replacement_selector)
            if resolved["already"]:
                return self.read(slug)
            record = self.read(slug)
            superseded_id, replacement_id = resolved["superseded_id"], resolved["replacement_id"]
            child = next(c for c in record["children"] if c["plan_id"] == superseded_id)

            child["superseded_by"] = replacement_id
            entry = next((c for c in record["children"] if c["plan_id"] == replacement_id), None)
            if entry is None:
                entry = {"plan_id": replacement_id, "added_at": _now()}
                record["children"].append(entry)
            if resolved["inherited"]:
                entry["predecessor_plan_id"] = resolved["inherited"]
            else:
                entry.pop("predecessor_plan_id", None)
            # The replaced child's OWN predecessor edge is left exactly as it was. It is a true
            # statement about the plan that was authored — it did succeed that plan — and re-pointing
            # it at the replacement would assert an answerability that never existed: the replaced
            # plan never saw the replacement's obligations and was never checked against them. What
            # moves is everything DOWNSTREAM: its former successors succeed the replacement now.
            # The result is a fork with one dead branch, which is what superseding already looks like
            # in this record and what `render` already describes as needing no fixing.
            for other in record["children"]:
                if other["plan_id"] in (superseded_id, replacement_id):
                    continue
                if other.get("predecessor_plan_id") == superseded_id:
                    other["predecessor_plan_id"] = replacement_id
            self._write(slug, record)
            return record

    def carry_forward_decay(self, slug: str, *, plan_id: str | None = None) -> list:
        """Carry-forward obligations a successor no longer answers for, re-checked against CURRENT heads.

        `add_child` checks once, at join time, against the predecessor as it stood THEN. That was the
        whole guarantee, and it decays: a predecessor revised afterwards can mint obligations its
        successor never saw, and nothing looked again. Observed live — the second pull request of this
        very program gained obligations after its successor had already joined, and the successor's
        record went on claiming to answer for a set that had grown underneath it.

        So the same comparison is re-derivable at any time, from the heads as they are now. It is
        surfaced rather than refused at `add`, because the decay is usually the predecessor's author
        doing exactly the right thing; what must not happen is the successor SEALING while unaware.

        Returns one entry per affected successor: its id, its predecessor's, and the obligations it
        does not answer for. `plan_id` narrows the sweep to one successor.
        """
        return self._decay_entries(self.read(slug), plan_id=plan_id)

    def _decay_entries(self, record: dict, *, plan_id: str | None = None) -> list:
        """`carry_forward_decay` from an already-read record — the shape `obligation_report` needs."""
        decay = []
        superseded = superseded_children(record)
        for child in record["children"]:
            predecessor_id = child.get("predecessor_plan_id")
            if not predecessor_id:
                continue
            if plan_id and child["plan_id"] != plan_id:
                continue
            # Liveness. A superseded, retired or abandoned child is not going to be revised — a
            # superseded one CANNOT be, since supersede retires it — so complaining that it no longer
            # answers for something is a demand with no door behind it. Left unfiltered, every
            # deliberate supersession would mint a permanent warning on `program show` that no action
            # could ever clear, which trains an operator to read past warnings. The complaint is only
            # useful where a plan can still act on it.
            if child["plan_id"] in superseded:
                continue
            try:
                if plan_store.derived_status(
                        self.plans.read_record(self.plans.resolve(child["plan_id"]))) \
                        in DEAD_BRANCH_STATES:
                    continue
            except Exception:  # noqa: BLE001 — an unreadable child is reported by other readers
                continue
            try:
                dropped = dropped_obligations(
                    self.plans.head(self.plans.resolve(predecessor_id)),
                    self.plans.head(self.plans.resolve(child["plan_id"])),
                    released=self.released_at(record, predecessor_id))
            except Exception:  # noqa: BLE001 — an unreadable sibling must not hide the rest
                continue
            if dropped:
                decay.append({"plan_id": child["plan_id"], "predecessor_plan_id": predecessor_id,
                              "obligations": dropped})
        return decay

    def program_membership(self, plan_id: str, *, claimed_program_id: str | None = None) -> dict:
        """Which program this plan belongs to, read from TWO sources, and what could not be read.

        Ownership used to be answered by validating every record in the library and letting any
        failure escape. One malformed record then refused the seal of EVERY plan on the shelf,
        including plans in no program at all — and `show`, which renders the same refusals, went with
        it. The obvious repair is the except-continue discipline `carry_forward_decay` already uses,
        and taken alone it is a fail-OPEN: a plan whose OWN program record is unreadable would look
        exactly like a plan in no program, the carry-forward re-check would be skipped, and a debt
        would slip past the one gate that catches it.

        So membership is established from two independent sources, and neither alone is trusted:

        1. **The program records.** A record that parses but fails its schema can still say whose it
           is — the children array is readable — so it OWNS its children even while broken, and their
           seals refuse. Only a record that will not parse at all hides its membership.
        2. **The plan's own back-link.** `program.program_id` lives in the sealed plan document and
           survives a program record that cannot be read. A slug carries its program id's last six
           characters, so a claim can be matched against an unreadable record without parsing it.

        What the two sources cannot settle between them is the one honest gap: a LEGACY child added
        before the back-link was required, under a record that will not parse. It is reported as an
        unreadable record rather than silently resolved, so the caller discloses it instead of
        pretending the question was answered.

        Returns `slug` (the owning program, or None), `unreadable` (every record that could not be
        validated, each saying whether it still names this plan), and `claims_unreadable` (this plan's
        own back-link names a program whose record cannot be parsed).
        """
        found, unreadable = None, []
        for slug in self.slugs():
            try:
                record = self.read(slug)
            except Exception as exc:  # noqa: BLE001 — one bad record must not hide the rest
                names_this_plan, parseable = False, True
                try:
                    raw = core.json_file(self._record_path(slug))
                    names_this_plan = any(
                        isinstance(child, dict) and child.get("plan_id") == plan_id
                        for child in (raw.get("children") or []))
                except Exception:  # noqa: BLE001 — unparseable: membership genuinely unknowable here
                    names_this_plan, parseable = False, False
                # `parseable` is the difference between "this broken record does NOT name the plan"
                # (raw JSON read, children checked) and "nobody can tell" (it would not even parse).
                # A caller deciding whether membership is knowable needs that distinction; without
                # it, the strictly more damaged record looked SAFER than the mildly damaged one.
                unreadable.append({"slug": slug, "error": str(exc),
                                   "names_this_plan": names_this_plan, "parseable": parseable})
                if names_this_plan and found is None:
                    found = slug      # fail CLOSED: a broken record that names this plan still owns it
                continue
            if found is None and any(child["plan_id"] == plan_id for child in record["children"]):
                found = slug
        claims_unreadable = False
        if claimed_program_id:
            suffix = f"--{claimed_program_id[-6:]}"
            claims_unreadable = any(entry["slug"].endswith(suffix) for entry in unreadable)
        return {"slug": found, "unreadable": unreadable, "claims_unreadable": claims_unreadable}

    def program_for_plan(self, plan_id: str) -> str | None:
        """The program slug this plan is a child of, or None. Nothing auto-selects; this is a lookup.

        Guarded: see `program_membership`, which this delegates to. A caller that needs to know what
        could not be read must ask that instead — this shape cannot say."""
        return self.program_membership(plan_id)["slug"]

    def close(self, slug: str, state: str, reason: str, *,
              acknowledged_unknown: str | None = None) -> dict:
        """End a program — retired or abandoned — after settling its books. NOT completion.

        Closing used to write the closure without consulting the obligation report, so a program
        could be retired while its own `show` went on listing debts as outstanding under a closed
        status. That is the carry-forward guarantee failing at the one moment nobody looks again:
        the debts do not stop existing because the program stopped.

        The two kinds of debt are answered differently, and the difference is not fussiness:

        - READABLE debts refuse. Each is named, and the refusal names `program release`, which is a
          door that opens. This refusal IS carry-forward, arriving at the end of a program's life.
        - UNKNOWN entries take an explicit acknowledgement instead. They are sentences about a broken
          record — a missing child, an unreadable one, a cycle — not obligations, and nothing keyed to
          an obligation id can clear them. Refusing over them would point at a door that cannot open
          and would permanently wedge exactly the wrecked programs `abandon` exists for. So they close
          on a recorded decision: not a wall, and not a silent pass.
        """
        if state == "complete":
            # `complete` has exactly one door, and this is not it. The signature admits the state
            # because the schema does, but reaching completion here would skip `completion_blockers`
            # entirely — a second entrance standing open beside the one this change exists to build.
            raise ProgramError(
                "completion is not written through `close`. It has one door — `complete` — because "
                "it is the only closure with a gate of its own: every live child complete, something "
                "actually shipped, and nothing owed or unknown.")
        with core.exclusive_lock(self.program_dir(slug) / (RECORD_FILENAME + ".lock")):
            record = self.read(slug)
            if record.get("closure"):
                raise ProgramError(f"this program is already {record['closure']['state']}")
            report = self.obligation_report(record)
            # The report now includes decayed mid-chain debts — an obligation minted after its
            # successor sealed — so this gate refuses over them too. The wedge that once made
            # gating on decay unshippable is gone: the operator ruled that a sealed successor is
            # not somewhere a debt can still be answered, so `program release` opens for exactly
            # this shape, and the refusal below names a door that opens.
            if report["obligations"]:
                raise ProgramError(
                    f"this program still owes {len(report['obligations'])} obligation(s), and "
                    f"closing it as {state} would leave them reporting as outstanding under a closed "
                    "status — owed by nobody, answerable by nothing:\n"
                    + "\n".join(
                        f"  - {obligation['id']} (carried at {leaf}): {obligation['statement']}"
                        for leaf, obligations in sorted(report["by_leaf"].items())
                        for obligation in obligations)
                    + "\nAnswer each before closing. If the work they awaited is genuinely void — "
                      "its successors abandoned, nothing left to revise — let it go on the record "
                      "with `program release <program> <child> --obligation <id> --reason \"...\"`.")
            if report["unknown"] and not (acknowledged_unknown or "").strip():
                raise ProgramError(
                    "what this program owes cannot be computed from its record, so closing it would "
                    "claim its books are settled when nobody can tell:\n"
                    + "\n".join(f"  - {reason_text}" for reason_text in report["unknown"])
                    + "\nThese are not obligations and no release can clear them — they are what a "
                      "broken record looks like. Repair the record if you can. If you cannot, close "
                      "with --acknowledge-unknown \"<why this is being accepted>\", which records the "
                      "decision rather than hiding it.")
            closure = {"state": state, "at": _now(), "reason": reason}
            if report["unknown"]:
                closure["acknowledged_unknown"] = acknowledged_unknown
            elif (acknowledged_unknown or "").strip():
                # Passed defensively when there was nothing to acknowledge. Silently dropping it
                # would leave the operator believing the record holds something it does not.
                raise ProgramError(
                    "nothing about this program's books is unknown, so there is nothing to "
                    "acknowledge — and recording an acknowledgement of nothing would put a claim on "
                    "the record that misdescribes it. Close without --acknowledge-unknown.")
            record["closure"] = closure
            self._write(slug, record)
            return record

    def complete(self, slug: str, reason: str) -> dict:
        """Record that the operator judged this program's objective met. Never derived.

        Completion used to be derived: every STORED child complete meant the program was complete.
        That makes 'complete' mean 'nothing left recorded' — and the absence of authored successors
        is UNKNOWN, not done. It was observed live, on this object's own program, reading complete
        with one of five pull requests landed. The same rule the obligation count already follows:
        unknown must never render as finished.

        So completion takes an explicit act. It carries no attestation of the operator's words — no
        local field can prove someone was present, and one implying it would be false confidence.
        What makes it trustworthy is simply that no derivation can reach it.
        """
        if not (reason or "").strip():
            raise ProgramError("completion costs a reason — it records a judgment, and a blank one "
                               "records nothing a later reader can weigh.")
        with core.exclusive_lock(self.program_dir(slug) / (RECORD_FILENAME + ".lock")):
            record = self.read(slug)
            if record.get("closure"):
                raise ProgramError(f"this program is already {record['closure']['state']}")
            blockers = self.completion_blockers(record)
            if blockers:
                raise ProgramError(
                    "this program cannot be recorded complete yet:\n"
                    + "\n".join(f"  - {blocker}" for blocker in blockers)
                    + "\nCompletion says the objective is MET. Recording it over unfinished work "
                      "would be the same lie in a different place. If the program is being set "
                      "down rather than finished, `program retire` is the verb — it refuses over "
                      "outstanding debts too, so settle or release those either way.")
            record["closure"] = {"state": "complete", "at": _now(), "reason": reason}
            self._write(slug, record)
            return record

    def completion_blockers(self, record: dict) -> list:
        """Why this program may not be recorded complete yet, in plain sentences.

        The live-children refusal here is a deliberate, recorded WIDENING of this object's boundary,
        which is otherwise 'refuse only on obligation carry-forward'. It is widened because a stored
        `complete` sitting over incomplete live children is a record that lies, which is the exact
        defect class this program exists to kill — and a disclosure that the record contradicts
        itself, printed beside the contradiction, is not an answer to it.
        """
        blockers = []
        view = self.child_view(record)
        statuses = {child["plan_id"]: child["status"] for child in view}
        superseded = superseded_children(record)
        # Superseded, retired and abandoned children do not bar completion: they are decisions
        # someone made, not work left undone.
        live = [plan_id for plan_id, status in statuses.items()
                if plan_id not in superseded and status not in DEAD_BRANCH_STATES]
        incomplete = [plan_id for plan_id in live if statuses[plan_id] != "complete"]
        if incomplete:
            blockers.append(
                "these children are not complete: "
                + ", ".join(f"{plan_id} ({statuses[plan_id]})" for plan_id in sorted(incomplete)))
        if not any(status == "complete" for status in statuses.values()):
            blockers.append("no child of this program is complete, so nothing has actually shipped")
        report = self.obligation_report(record)
        if report["obligations"]:
            blockers.append(f"{len(report['obligations'])} obligation(s) are still outstanding: "
                            + ", ".join(o["id"] for o in report["obligations"]))
        if report["unknown"]:
            blockers.append("what this program owes cannot be computed from its record: "
                            + "; ".join(report["unknown"]))
        return blockers

    def revise_objective(self, slug: str, objective: str, reason: str) -> dict:
        """Replace the objective, keeping the text it replaced. Works on a closed program.

        An objective is written before the work begins, which is exactly when the least is known
        about it. This program object's OWN objective went stale within days of being written — it
        still described an ordering the children's chain had already superseded — and there was no
        verb to correct it, so the stale wording stood as the program's headline while the record
        underneath it said something else.

        Permitted on a closed program for the same reason a release is: this is a CORRECTION to the
        record, not new structure, and a closed program whose objective is wrong should not have to
        be reopened — a reversal of the operator's decision — merely to fix a sentence.
        """
        if not (objective or "").strip():
            raise ProgramError("an objective cannot be empty — that is the one thing a program has "
                               "to say for itself.")
        if not (reason or "").strip():
            raise ProgramError(
                "revising the objective costs a reason. The old text is kept either way; what the "
                "reason adds is why it stopped being true, which is the part a later reader needs "
                "and the part nobody can reconstruct.")
        with core.exclusive_lock(self.program_dir(slug) / (RECORD_FILENAME + ".lock")):
            record = self.read(slug)
            if record["objective"] == objective:
                raise ProgramError("that is the objective this program already carries; nothing "
                                   "was written, and no history entry was minted for a no-op.")
            record.setdefault("objective_history", []).append({
                "objective": record["objective"], "replaced_at": _now(), "reason": reason})
            record["objective"] = objective
            self._write(slug, record)
            return record

    def reopen(self, slug: str, reason: str) -> dict:
        """Undo a closure — any of the three — keeping what was undone on the record.

        A plan's completion is terminal because it records MERGED HISTORY, which does not become
        untrue. A program's records the operator's judgment that the objective is met, and a judgment
        may be revisited as evidence arrives. The divergence from the plan-level rule is deliberate
        and is the reason a reason is required: reversible, never silent.
        """
        if not (reason or "").strip():
            raise ProgramError(
                "reopening costs a reason. A closure that can be undone without saying why is a "
                "record that changes with nothing to show for it — and completion in particular is "
                "the operator's judgment, so reversing it is a second judgment worth reading.")
        with core.exclusive_lock(self.program_dir(slug) / (RECORD_FILENAME + ".lock")):
            record = self.read(slug)
            if not record.get("closure"):
                raise ProgramError("this program is not closed")
            record.setdefault("closure_history", []).append({
                "closure": record["closure"], "reopened_at": _now(), "reason": reason})
            record["closure"] = None
            self._write(slug, record)
            return record

    def set_lanes(self, slug: str, lanes: list, reason: str) -> dict:
        """Record the concurrency split the operator DECIDED, keeping any split it replaces.

        Advisory only: a lane split is a record of which children may ride at once, never a schedule
        anything executes. Its ONLY refusal surface is input validity, and that surface is enumerated
        exhaustively below — liveness, ordering, and disagreement with what `propose` recommended are
        never refused, so a split that lanes a superseded child, or contradicts the recommendation,
        records cleanly. The read → mutate → write is one hold on the record lock, mirroring
        revise_objective, so a concurrent record write cannot be lost; and an identical re-set is a
        no-op that mints no history, exactly as revise_objective refuses an identical objective.

        `lanes` is the decided split as a list of {"name", "children"} — children are plan_ids in the
        order the operator wants them read. The reason is stored ON the split; when a later set or
        clear ends this split, that reason travels with it into lanes_history.
        """
        if not (reason or "").strip():
            raise ProgramError(
                "recording a lane split costs a reason. The split is the operator's decision about "
                "concurrency, and a decision with nothing to show for why it was made is the silent "
                "record this object exists to prevent.")
        # Structural validity of the proposed split — pure checks on the input, before the lock.
        # Enumerated, each with its own message, because input validation is the whole refusal
        # surface here and a caught-together error would blur which rule an operator tripped.
        if not lanes:
            raise ProgramError(
                "a lane split needs at least one lane; an empty split records no decision and is "
                "refused. To withdraw a split, use `program lanes clear`.")
        proposed: list = []
        seen_names: set = set()
        seen_children: dict = {}   # plan_id -> the lane name that first claimed it, in input order
        for lane in lanes:
            name = lane.get("name") or ""
            children = list(lane.get("children") or [])
            if not name.strip():
                raise ProgramError("a lane name cannot be empty; every lane is named.")
            if name in seen_names:
                raise ProgramError(f"lane name {name!r} is used twice; lane names must be unique.")
            seen_names.add(name)
            if not children:
                raise ProgramError(
                    f"lane {name!r} has no members; a lane with no children records nothing and is "
                    "refused.")
            for child in children:
                if child in seen_children:
                    if seen_children[child] == name:
                        raise ProgramError(f"child {child} appears twice in lane {name!r}.")
                    raise ProgramError(
                        f"child {child} is placed in two lanes ({seen_children[child]!r} and "
                        f"{name!r}); a child rides at most one lane.")
                seen_children[child] = name
            proposed.append({"name": name, "children": children})
        with core.exclusive_lock(self.program_dir(slug) / (RECORD_FILENAME + ".lock")):
            record = self.read(slug)
            member_ids = {child["plan_id"] for child in record["children"]}
            for child in seen_children:   # insertion order == input order, so the message is stable
                if child not in member_ids:
                    raise ProgramError(
                        f"child {child} is not stored in this program, so it cannot be laned. Add it "
                        "to the program first, or correct the plan id.")
                try:
                    self.plans.resolve(child)
                except plan_store.PlanStoreError:
                    raise ProgramError(
                        f"child {child} is stored in this program but missing from this library, so "
                        "its territory cannot be read; laning it would record a decision over a plan "
                        "this workstation cannot see.") from None
            standing = record.get("lanes")
            if standing and standing["lanes"] == proposed:
                raise ProgramError(
                    "that is the split this program already carries; nothing was written, and no "
                    "history entry was minted for a no-op.")
            now = _now()
            if standing:
                record.setdefault("lanes_history", []).append(
                    {"split": standing, "ended_at": now, "ended_by": "replaced", "reason": reason})
            record["lanes"] = {"decided_at": now, "reason": reason, "lanes": proposed}
            self._write(slug, record)
            return record

    def clear_lanes(self, slug: str, reason: str) -> dict:
        """Withdraw the standing lane split, keeping it in a discriminated history.

        The withdrawal is recorded as `ended_by: cleared`, distinct from the `replaced` a set writes,
        so a later reader of set → set → clear → set can tell a withdrawal from a replacement and
        reconstruct when no split stood at all. Same lock discipline as set_lanes.
        """
        if not (reason or "").strip():
            raise ProgramError(
                "withdrawing a lane split costs a reason. Clearing a decided split without saying "
                "why is a record that changes with nothing to show for it.")
        with core.exclusive_lock(self.program_dir(slug) / (RECORD_FILENAME + ".lock")):
            record = self.read(slug)
            standing = record.get("lanes")
            if not standing:
                raise ProgramError("this program has no decided lane split to clear.")
            record.setdefault("lanes_history", []).append(
                {"split": standing, "ended_at": _now(), "ended_by": "cleared", "reason": reason})
            record.pop("lanes", None)
            self._write(slug, record)
            return record

    # -- derivation --
    def child_view(self, record: dict) -> list:
        """Each child with its plan's derived status, in CHAIN order, every stored child exactly once.

        Missing plans are reported as missing rather than skipped: a child that is not in this library
        is a fact about the program, and quietly omitting it would make the program look shorter than
        it is. Each entry carries `chain_ordinal` — its computed place in the declared order — and,
        where the record is malformed, an `anomaly` naming why it could not be reached.
        """
        analysis = chain_analysis(record)
        by_id = {child["plan_id"]: child for child in record["children"]}
        anomaly_of = {entry["plan_id"]: "dangling-predecessor" for entry in analysis["dangling"]}
        for plan_id in analysis["unreachable"]:
            anomaly_of.setdefault(plan_id, "unreachable")
        view = []
        for ordinal, plan_id in enumerate(analysis["order"], start=1):
            child = by_id[plan_id]
            entry = {**child, "chain_ordinal": ordinal}
            if plan_id in anomaly_of:
                entry["anomaly"] = anomaly_of[plan_id]
            try:
                plan_slug = self.plans.resolve(child["plan_id"])
            except ProgramError:
                view.append({**entry, "slug": None, "title": "(not in this library)",
                             "status": "missing", "outstanding": []})
                continue
            try:
                plan_record = self.plans.read_record(plan_slug)
            except Exception:  # noqa: BLE001 — a schema-invalid record is present but will not read
                # Uncaught, this crashed every reader stacked on this view — report, render, and
                # BOTH closure gates — so one child whose record fails schema validation made its
                # whole program permanently unclosable through every verb, defeating the exact
                # "always an exit" guarantee the acknowledged-unknown path exists to keep. It is an
                # `unreadable` row instead: disclosed by the report, closable on the record.
                view.append({**entry, "slug": plan_slug, "title": "(record does not read)",
                             "status": "unreadable", "outstanding": []})
                continue
            try:
                document = self.plans.head(plan_slug)
                # Program-level releases are subtracted HERE, at the one place every reader of a
                # child's debts goes through, rather than at each of them. A release honored in some
                # readers and not others would be the worst of both: a debt that disappears from the
                # count an operator scans and reappears at the gate that refuses their next action.
                released = self.released_at(record, child["plan_id"])
                outstanding = sorted(
                    (o for o in carried_forward(document).values() if o["id"] not in released),
                    key=lambda o: o["id"])
                status = plan_store.derived_status(plan_record)
            except Exception:  # noqa: BLE001 — same rule: an unreadable head is a row, not a crash
                outstanding, status = [], "unreadable"
            view.append({**entry, "slug": plan_slug, "title": plan_record["title"],
                         "status": status, "outstanding": outstanding})
        return view

    def obligation_report(self, record: dict) -> dict:
        """What this program still owes, per branch end, and honestly when it cannot tell.

        ONE RULE, stated whole, because answering it in pieces produced four wrong answers in a row:

            A program owes what its LIVE branch ends still carry.
            A child is LIVE if its plan reads and it is not retired or abandoned.
            A live child is a BRANCH END if no live child declares it as predecessor.
            A live child carrying a debt that is not a branch end and cannot be placed on the chain
            is UNKNOWN — the debt is real, its position is not.

        Each clause was a defect found by review, and each is here rather than in a caller:

        - `view[-1]` answered "what does the last row carry". Once the chain forked, the other
          branch's debts left the only number an operator reads.
        - Retired and abandoned branches are not ends: a live shelf holds exactly that shape, with a
          debt the surviving branch deliberately RELEASED still sitting on the dead leaf. Unioning it
          back would resurrect an obligation someone consciously let go.
        - But a dead END does not kill its live ANCESTOR's debt. An open child whose only successor
          was retired is itself the live end of that branch, and reporting `None outstanding` while it
          carries an unanswered obligation is the same silent drop wearing a different mask.
        - A child off the chain still carries what it carries. If nothing live succeeds it, it IS an
          end and its debt is owed there; the broken edge is disclosed separately. Only when it can be
          neither placed nor ended — a cycle member — is the debt unknown rather than owed.
        - Unknown is never zero, and never also a count: a debt is reported once, in one place.
        """
        analysis = chain_analysis(record)
        view = {child["plan_id"]: child for child in self.child_view(record)}

        def live(plan_id: str) -> bool:
            child = view.get(plan_id)
            return bool(child) and child["status"] not in DEAD_BRANCH_STATES \
                and child["status"] not in ("missing", "unreadable")

        # A live child that no LIVE child succeeds. Not `analysis["leaves"]`, which is structural and
        # cannot see that a branch's only successor is retired.
        live_successors: dict = {}
        for child in record["children"]:
            predecessor = child.get("predecessor_plan_id")
            if predecessor and live(child["plan_id"]):
                live_successors.setdefault(predecessor, []).append(child["plan_id"])
        ends = [plan_id for plan_id in analysis["order"]
                if live(plan_id) and plan_id not in live_successors]

        def reaches_an_end(start: str) -> bool:
            """Whether following LIVE successors from here arrives at a branch end.

            The rule says a debt is unknown when it can be neither placed nor ended — and "not itself
            an end" is not that test. A child whose own predecessor edge is broken but which still has
            a live successor is fine: its carries flow forward exactly as they always did, and the end
            that receives them already answers for them. Testing only for end-ness printed such a debt
            twice, and resurrected one the end had already SATISFIED. Cycle-safe by the seen set: a
            child that can only ever revisit itself reaches nothing, which is precisely the case that
            is genuinely unknown.
            """
            seen, stack = set(), [start]
            while stack:
                plan_id = stack.pop()
                if plan_id in seen:
                    continue
                seen.add(plan_id)
                if plan_id in ends:
                    return True
                stack.extend(live_successors.get(plan_id, []))
            return False

        unknown, by_leaf, obligations = [], {}, {}
        superseded = superseded_children(record)
        for child in view.values():
            if child["status"] in ("missing", "unreadable"):
                # A superseded child is a decision on the record, not work whose books are open:
                # its debts moved to the replacement at supersede time, where the join checks ran.
                # Counting its unreadable plan as unknown made a deliberate supersession block
                # completion forever — the inconsistency the dead-branch filters exist to avoid.
                if child["plan_id"] in superseded:
                    continue
                unknown.append(f"{child['plan_id']} is {child['status']}")
        for plan_id in ends:
            child = view[plan_id]
            if child["outstanding"]:
                by_leaf[plan_id] = child["outstanding"]
                for obligation in child["outstanding"]:
                    obligations[obligation["id"]] = obligation
        for plan_id, child in view.items():
            # Carries a debt, is live, is not an end, and cannot be placed: a cycle member. Its debt
            # is real and belongs nowhere the chain reaches, so it is named rather than dropped — and
            # named ONCE, since it was not attributed above.
            if (child.get("anomaly") and live(plan_id) and child["outstanding"]
                    and not reaches_an_end(plan_id)):
                unknown.append(
                    f"{plan_id} carries {len(child['outstanding'])} obligation(s) but is "
                    f"{child['anomaly']}, so they sit on no branch and cannot be attributed: "
                    + ", ".join(o["id"] for o in child["outstanding"]))
        # THE THIRD PATH, closed on the operator's ruling. A debt a predecessor mints AFTER its
        # successor has sealed sits mid-chain: the successor answered everything the join-time check
        # saw, so the branch end never inherits this one, and the report used to miss it — the decay
        # sweep warned on a terminal while both closure gates read this report and passed. A warning
        # is not a gate. So decay is folded in here, attributed to the child that CARRIES the debt,
        # which makes `show` display it, and close and complete refuse over it. The door that then
        # opens is `program release` — the sealed successor can never take a revision, so it no
        # longer counts as somewhere the debt could be answered.
        decayed: dict = {}
        decayed_awaiting: dict = {}
        for entry in self._decay_entries(record):
            carrier = entry["predecessor_plan_id"]
            if not live(carrier):
                continue          # a dead carrier's debts died with the decision that closed it
            # Which door opens depends on what the carrier's LIVE successors can still DO. If any
            # is unsealed, a revision can answer and `release` refuses — so the honest advice is
            # revise, not release. Only when every live successor is sealed is the debt truly
            # unanswerable and the program-level release the door. The first cut keyed the render
            # sentence on decay alone and told a draft's operator that "no revision can answer" —
            # a false sentence pointing at a door (`release`) that refuses exactly that shape.
            revisable_successors = []
            for child in record["children"]:
                if child.get("predecessor_plan_id") != carrier or not live(child["plan_id"]):
                    continue
                try:
                    if not self.plans.read_record(
                            self.plans.resolve(child["plan_id"])).get("seal"):
                        revisable_successors.append(child["plan_id"])
                except Exception:  # noqa: BLE001 — an unreadable successor is the report's unknown
                    continue
            for obligation in entry["obligations"]:
                if obligation["id"] in obligations:
                    continue      # already owed at a branch end; one debt, reported once
                by_leaf.setdefault(carrier, []).append(obligation)
                obligations[obligation["id"]] = obligation
                if revisable_successors:
                    bucket = decayed_awaiting.setdefault(
                        carrier, {"ids": set(), "successors": sorted(revisable_successors)})
                    bucket["ids"].add(obligation["id"])
                else:
                    decayed.setdefault(carrier, set()).add(obligation["id"])
        if record["children"] and not ends and not unknown and any(
                live(child["plan_id"]) for child in record["children"]):
            # ONLY when live children exist and yet none of them ends anything: that is a cycle. A
            # program whose every child was retired or abandoned has no live end either, and it is
            # not corrupt — it is finished. Keying the message on "no ends" alone made a normal fully
            # closed program report as damaged, which is the false alarm side of the same coin as the
            # silent zero: both tell the operator something the record does not say.
            unknown.append("no live branch of this chain ends — every open child is succeeded by "
                           "another, which means the predecessor edges form a cycle")
        return {"obligations": sorted(obligations.values(), key=lambda o: o["id"]),
                "by_leaf": by_leaf, "unknown": unknown, "analysis": analysis,
                # Which of the by_leaf attributions are mid-chain decay carriers, not branch ends.
                # The render reads this to say the true sentence: pre-fold, every by_leaf key WAS
                # an end, and the two end-shaped sentences it had were both false for a carrier
                # whose sealed successor can never answer — a lie printed three lines under the
                # table that contradicted it, with a door ("the next child answers") that refuses.
                "decayed": {carrier: sorted(ids) for carrier, ids in decayed.items()},
                # Same shape, other door: mid-chain debts whose successor can still be REVISED.
                "decayed_awaiting": {
                    carrier: {"ids": sorted(entry["ids"]), "successors": entry["successors"]}
                    for carrier, entry in decayed_awaiting.items()}}

    def outstanding_obligations(self, record: dict) -> list:
        """The union of every OPEN leaf's carried obligations. See `obligation_report`, which also
        says whose debt each one is and when the answer is not computable."""
        return self.obligation_report(record)["obligations"]

    CHILDREN_COMPLETE = "children-complete"

    #: What the children-complete token does NOT claim, said in full wherever there is room for a
    #: sentence. The token itself is deliberately not the word `complete`: an operator scanning a
    #: list reads one word, and that word must not be one they will take for a finished program.
    CHILDREN_COMPLETE_SENTENCE = (
        "every live child is complete — superseded, retired and abandoned children are recorded "
        "decisions, not work left undone — which is not the same as this program being complete: "
        "successors that were never authored are UNKNOWN, not done, and only an explicit "
        "`program complete` records that the objective was met")

    def derived_status(self, record: dict) -> str:
        """What can be told from the record. Programs have no seal, and completion is NOT derivable.

        Sealing a child seals that child. Completion used to be derived here — every stored child
        complete meant the program was complete — and that was a defect, not a design choice. It
        makes `complete` mean "nothing left recorded", so a program with one of five planned pull
        requests landed reads as finished, because the four unwritten successors derive as done.
        Absent work is unknown, and unknown must never render as finished: the same rule the
        obligation count follows, applied to the headline an operator actually reads.

        So the ceiling here is `children-complete` — a token that claims only what the record shows.
        The word `complete` appears for a program in exactly one circumstance: the operator recorded
        it with an explicit verb, in which case it comes from the stored closure above, not from here.
        """
        if record.get("closure"):
            return record["closure"]["state"]
        view = self.child_view(record)
        if not view:
            return "empty"
        statuses = [child["status"] for child in view]
        superseded = superseded_children(record)
        # A missing SUPERSEDED child is damaged history, not open books: its debts moved to the
        # replacement when the join checks ran, so nothing about the program's future turns on
        # reading it. Flagging it forced `needs-attention` — and blocked completion — forever, on
        # a record whose every live child was fine, which trains an operator to read past the one
        # word that must always mean "something here needs you". The row still renders as missing
        # in `show`; it just stops being an alarm.
        if any(child["status"] in ("missing", "unreadable") for child in view
               if child["plan_id"] not in superseded):
            return "needs-attention"
        if any(status == "active" for status in statuses):
            return "active"
        live = [child["status"] for child in view
                if child["plan_id"] not in superseded
                and child["status"] not in DEAD_BRANCH_STATES]
        if live and all(status == "complete" for status in live):
            return self.CHILDREN_COMPLETE
        return "in-progress"

    def status_is_recorded(self, record: dict) -> bool:
        """Whether the status word came from the operator's stored closure or from a derivation.

        The distinction is the whole point of the caption: `retired` read off a closure is a decision
        somebody made, and `in-progress` read off the children is a computation. Labelling both the
        same way would let an operator's recorded judgment be mistaken for something the engine
        worked out, and the reverse.
        """
        return bool(record.get("closure"))


def render(library: ProgramLibrary, record: dict) -> str:
    """The program as an operator reads it: what it is for, its children and where each stands, every
    obligation still owed and whose it is, and anything about the record that does not add up."""
    view = library.child_view(record)
    report = library.obligation_report(record)
    analysis = report["analysis"]
    out = [f"# {record['title']}", "",
           "<!-- generated from the program record and its children; edits here are overwritten -->", "",
           f"- **Program**: `{record['program_id']}`",
           f"- **Status**: {library.derived_status(record)} — "
           + ("recorded by an explicit close, not derived"
              if library.status_is_recorded(record)
              else "derived from the children, never stored"),
           f"- **Children**: {len(view)}", ""]
    if library.derived_status(record) == library.CHILDREN_COMPLETE:
        # The token is short enough to be misread on its own, so wherever there is room for a
        # sentence it gets one. This is the render an operator reads when deciding whether the work
        # is done, which is exactly the moment the old derived `complete` misled them.
        out += [f"> **This program is not recorded as complete.** {library.CHILDREN_COMPLETE_SENTENCE}.",
                ""]
    if record.get("closure", {}) and record["closure"].get("acknowledged_unknown"):
        out += ["> **Closed over an unknown.** What this program owed could not be computed from its "
                "record when it was closed, and that was accepted deliberately rather than resolved: "
                f"{record['closure']['acknowledged_unknown']}", ""]
    out += ["## Objective", "", record["objective"], ""]
    out += ["## Children, in the order their predecessor edges declare", "",
            "| # | Plan | Status | Succeeds |", "|---:|---|---|---|"]
    for child in view:
        title = child["title"].replace("|", "\\|")
        succeeds = f"`{child['predecessor_plan_id']}`" if child.get("predecessor_plan_id") else "—"
        flag = f" ⚠ {child['anomaly']}" if child.get("anomaly") else ""
        # A replaced child stays in the table rather than vanishing — history is not deleted from
        # this record — and says what replaced it, so its retired status reads as a decision someone
        # made rather than as a plan that quietly died.
        if child.get("superseded_by"):
            flag += f" — superseded by `{child['superseded_by']}`"
        # The ordinal is COMPUTED from the chain, not the stored `position` field: a record whose
        # numbering was permuted or duplicated still reads in the order its edges actually declare.
        out.append(f"| {child['chain_ordinal']} | {title} (`{child['plan_id']}`){flag} | "
                   f"{child['status']} | {succeeds} |")
    out += ["", "_Order records a decision. Nothing here selects, starts, or advances a child._", ""]

    if analysis["forks"]:
        # Its own section, deliberately NOT filed under the corruption heading below. A fork is how a
        # branch gets superseded or abandoned — the ordinary shape of a program that changed its mind
        # — and reporting it as something that "does not add up" made the one genuinely forked program
        # on this shelf read as damaged every time an operator looked at it.
        status_of = {child["plan_id"]: child["status"] for child in view}
        out += ["## Where the chain branches", ""]
        for fork in analysis["forks"]:
            live = [name for name in fork["successors"]
                    if status_of.get(name) not in DEAD_BRANCH_STATES]
            out.append(f"- `{fork['predecessor_plan_id']}` is the declared predecessor of "
                       + ", ".join(f"`{name}` ({status_of.get(name, 'unknown')})"
                                   for name in fork["successors"])
                       + ("." if len(live) <= 1 else
                          " — more than one of these branches is still open, so the program has more "
                          "than one end and more than one set of obligations still owed."))
        if all(len([name for name in fork["successors"]
                    if status_of.get(name) not in DEAD_BRANCH_STATES]) <= 1
               for fork in analysis["forks"]):
            out.append("")
            out.append("_Nothing here needs fixing: every branch but one has been retired or "
                       "abandoned, which is what superseding a plan looks like in the record._")
        out.append("")

    status_of_all = {child["plan_id"]: child["status"] for child in view}
    if analysis["dangling"] or analysis["unreachable"] or len(
            [name for name in analysis["roots"]
             if status_of_all.get(name) not in DEAD_BRANCH_STATES]) > 1:
        out += ["## What does not add up in this record", ""]
        for entry in analysis["dangling"]:
            out.append(f"- `{entry['plan_id']}` declares `{entry['predecessor_plan_id']}` as its "
                       "predecessor, and no such child is in this program.")
        for plan_id in analysis["unreachable"]:
            if any(entry["plan_id"] == plan_id for entry in analysis["dangling"]):
                continue
            out.append(f"- `{plan_id}` cannot be reached from the start of the chain; its predecessor "
                       "edges lead in a circle.")
        live_roots = [name for name in analysis["roots"]
                      if status_of_all.get(name) not in DEAD_BRANCH_STATES]
        if len(live_roots) > 1:
            # Counted over LIVE roots only, for the same reason the fork section is: superseding the
            # FIRST child of a program leaves its replacement as a second root, and the replaced one
            # is retired. Counting both would report the ordinary shape of a changed mind as a record
            # that holds several disconnected chains — a false alarm about the very verb that made it.
            out.append("- More than one child declares no predecessor: "
                       + ", ".join(f"`{name}`" for name in live_roots)
                       + " — so this record holds several disconnected chains rather than one.")
        out.append("")

    out += ["## Obligations still carried", ""]
    if report["unknown"]:
        out.append("_Cannot be computed from this record._ Nothing here should be read as a debt of "
                   "zero — what is owed is unknown until these are resolved:")
        out += [f"- {reason}" for reason in report["unknown"]]
        if report["obligations"]:
            out += ["", "What the readable branches still owe:"]
    if report["obligations"]:
        status_of = {child["plan_id"]: child["status"] for child in view}
        successors_of: dict = {}
        for child in record["children"]:
            predecessor = child.get("predecessor_plan_id")
            if predecessor:
                successors_of.setdefault(predecessor, []).append(child["plan_id"])

        def dead_chain_after(plan_id: str) -> list:
            """Every stopped plan downstream of here, not merely the first one.

            A branch usually dies more than one plan deep — the live shelf's own case is B abandoned
            and then C abandoned after it — and naming only the immediate successor left an operator
            reading about one stopped plan while the table showed two, with nothing connecting them.
            """
            found, seen, stack = [], set(), list(successors_of.get(plan_id, []))
            while stack:
                successor = stack.pop(0)
                if successor in seen or status_of.get(successor) not in DEAD_BRANCH_STATES:
                    continue
                seen.add(successor)
                found.append(successor)
                stack.extend(successors_of.get(successor, []))
            return found

        stopped_after = {plan_id: dead_chain_after(plan_id) for plan_id in report["by_leaf"]}
        for leaf, obligations in sorted(report["by_leaf"].items()):
            # Attributed per leaf, because on a forked chain "what is still owed" and "who owes it"
            # are different questions, and only the second one can be answered by a successor.
            #
            # And when this plan is an end only BECAUSE its successors stopped, say so. The operator
            # otherwise meets a debt that appeared from nowhere and has to infer the reason from a
            # status column two sections up — the reasoning lived only in this module's docstring,
            # which is not somewhere they read.
            dead = stopped_after.get(leaf) or []
            awaiting = report.get("decayed_awaiting", {}).get(leaf)
            if awaiting and all(o["id"] in awaiting["ids"] for o in obligations):
                names = ", ".join(f"`{plan_id}`" for plan_id in awaiting["successors"])
                out.append(
                    f"- Carried at `{leaf}`, MID-CHAIN: these were minted after {names} joined, "
                    "so the join-time check never saw them. Revise "
                    f"{names} to answer for each — satisfied, still carried, or released with a "
                    "reason; its seal refuses until it does:")
                for obligation in obligations:
                    out.append(f"  - **{obligation['id']}** — {obligation['statement']}")
                continue
            decayed_here = set(report.get("decayed", {}).get(leaf, []))
            if decayed_here and all(o["id"] in decayed_here for o in obligations):
                out.append(
                    f"- Carried at `{leaf}`, MID-CHAIN: these were minted after its successor "
                    "sealed, so the join-time check never saw them and no revision can answer "
                    "them now. The door is `program release " + record.get("slug", "<program>")
                    + f" {leaf} --obligation <id> --reason \"...\"`:")
                for obligation in obligations:
                    out.append(f"  - **{obligation['id']}** — {obligation['statement']}")
                continue
            if dead:
                out.append(
                    f"- Carried at `{leaf}`, which is where that branch now ends — "
                    + ", ".join(f"`{name}` ({status_of.get(name, 'closed')})" for name in dead)
                    + (" was" if len(dead) == 1 else " were")
                    + " meant to answer for these and stopped without doing so:")
            else:
                out.append(f"- Carried at `{leaf}`, where that branch currently ends:")
            for obligation in obligations:
                out.append(f"  - **{obligation['id']}** — {obligation['statement']}")
        out += ["", "Each debt carried at a branch END must be answered by the next child on ITS "
                    "OWN branch — satisfied, still carried, or released with a reason; a mid-chain "
                    "debt above already names its own door. None can be dropped by saying nothing."]
    elif not report["unknown"]:
        out.append("_None outstanding._")
    released = _released(library, record)
    program_releases = record.get("releases", [])
    if released or program_releases:
        out += ["", "## Obligations released along the way", ""]
        for entry in sorted(program_releases,
                            key=lambda e: (e["child_plan_id"], e["obligation_id"])):
            # Attributed at PROGRAM level, and said so. A release granted here was granted because
            # no successor was left to answer — a different decision from one written inside a plan,
            # and an operator should not have to work out which they are looking at.
            out.append(f"- **{entry['obligation_id']}** — carried at `{entry['child_plan_id']}`, "
                       "released at PROGRAM level because no successor was left to answer for it")
            out.append(f"  - Released {entry['at']}: {entry['reason']}")
        for obligation, plan_id in released:
            out.append(f"- **{obligation['id']}** — {obligation['statement']}")
            # No fallback string. There used to be a "(no reason given)" here, and printing it was the
            # projection quietly accepting the one shape the guarantee forbids. A release now carries
            # its reason by schema and by add_child, so a missing one is a corrupt record, and the
            # KeyError that follows is the honest report of that — not a hole to paper over.
            out.append(f"  - Released in `{plan_id}`: {obligation['reason']}")
    if record.get("closure_history"):
        out += ["", "## Closures that were undone", ""]
        for entry in record["closure_history"]:
            closure = entry["closure"]
            out.append(f"- **{closure['state']}** ({closure['at']}): {closure['reason']}")
            out.append(f"  - Reopened {entry['reopened_at']}: {entry['reason']}")
        out.append("")
        out.append("_Nothing was erased. A program's completion records a judgment, and a judgment "
                   "may be revisited — but never silently._")
    if record.get("objective_history"):
        out += ["", "## How the objective has been revised", ""]
        for entry in record["objective_history"]:
            out.append(f"- Replaced {entry['replaced_at']}: {entry['reason']}")
            out.append(f"  - Previously: {entry['objective']}")
    return "\n".join(out).rstrip() + "\n"


def _released(library: ProgramLibrary, record: dict) -> list:
    """Every obligation deliberately let go, with where and why. Kept visible on purpose: a release
    is a decision, and a decision nobody can find again is not much better than a silent drop."""
    out = []
    for child in library.child_view(record):
        if not child["slug"]:
            continue
        try:
            document = library.plans.head(child["slug"])
        except ProgramError:
            continue
        for obligation in (document.get("program") or {}).get("carried_obligations", []):
            if obligation["state"] == "released":
                out.append((obligation, child["plan_id"]))
    return out
