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


def dropped_obligations(predecessor: dict, successor: dict) -> list:
    """Obligations the predecessor was carrying that the successor does not mention at all.

    Not mentioning one is the failure this object exists for — it is indistinguishable, from the
    outside, from having decided it no longer matters, except that nobody decided anything.
    """
    successor_program = successor.get("program") or {}
    named = {o["id"] for o in successor_program.get("carried_obligations", [])}
    return [obligation for identifier, obligation in sorted(carried_forward(predecessor).items())
            if identifier not in named]


DEAD_BRANCH_STATES = ("retired", "abandoned")


def chain_analysis(record: dict) -> dict:
    """Order the children by their DECLARED predecessor edges, and name every anomaly found.

    `position` is NOT consulted. It is display-only — assigned at add, printed in the table, and read
    by nothing else in the engine — so ordering by it made the stored array's numbering authoritative
    over the edges that actually record the decision. The edges are the decision; the number is a
    label. Siblings of one fork tie-break on (added_at, plan_id), never on position, so a record whose
    numbering has been permuted or duplicated still renders in the order its edges declare.

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
                    self.plans.head(plan_slug))
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
                    self.plans.head(self.plans.resolve(inherited)), inserted_head)
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
            dropped = dropped_obligations(inserted_head,
                                          self.plans.head(self.plans.resolve(displaced_id)))
            if dropped:
                sealed = bool(displaced_record.get("seal"))
                raise ProgramError(
                    f"{displaced_id} would succeed {plan_id} once this insertion lands, and it does "
                    f"not answer for {len(dropped)} obligation(s) that {plan_id} declares it is "
                    f"carrying:\n"
                    + "\n".join(f"  - {o['id']}: {o['statement']}" for o in dropped)
                    + ("\nThat plan is SEALED, and a seal is terminal, so it cannot be revised to "
                       f"answer for them. The way through is to replace it: `program supersede "
                       f"{displaced_id}` with a plan that does answer, which inherits its place on "
                       "the chain and keeps it visible in the record."
                       if sealed else
                       f"\nRevise {displaced_id} so each appears in its carried_obligations as "
                       "satisfied, still carried, or released with a reason, then insert."))

            child = {"plan_id": plan_id, "added_at": _now()}
            if inherited:
                child["predecessor_plan_id"] = inherited
            displaced["predecessor_plan_id"] = plan_id
            record["children"].append(child)
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

    SUPERSEDE_REFUSED_STATUSES = ("complete", "active")

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
                "and its completion could never be recorded afterwards. Finish that Build and let it "
                "merge, or abandon it, then supersede.")

        # The replacement inherits the replaced child's predecessor edge, so it inherits the
        # answerability that came with it. Checked here, before anything is retired, because a
        # refusal after step 2 would leave a plan out of play for a supersession that never landed.
        inherited = child.get("predecessor_plan_id")
        if inherited and not already:
            declared = (self.plans.head(replacement_slug).get("program") or {}).get("program_id")
            if declared == record["program_id"]:
                dropped = dropped_obligations(
                    self.plans.head(self.plans.resolve(inherited)),
                    self.plans.head(replacement_slug))
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
        record = self.read(slug)
        decay = []
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
            if child.get("superseded_by"):
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
                    self.plans.head(self.plans.resolve(child["plan_id"])))
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
                names_this_plan = False
                try:
                    raw = core.json_file(self._record_path(slug))
                    names_this_plan = any(
                        isinstance(child, dict) and child.get("plan_id") == plan_id
                        for child in (raw.get("children") or []))
                except Exception:  # noqa: BLE001 — unparseable: membership genuinely unknowable here
                    names_this_plan = False
                unreadable.append({"slug": slug, "error": str(exc), "names_this_plan": names_this_plan})
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

    def close(self, slug: str, state: str, reason: str) -> dict:
        with core.exclusive_lock(self.program_dir(slug) / (RECORD_FILENAME + ".lock")):
            record = self.read(slug)
            if record.get("closure"):
                raise ProgramError(f"this program is already {record['closure']['state']}")
            record["closure"] = {"state": state, "at": _now(), "reason": reason}
            self._write(slug, record)
            return record

    def reopen(self, slug: str) -> dict:
        with core.exclusive_lock(self.program_dir(slug) / (RECORD_FILENAME + ".lock")):
            record = self.read(slug)
            if not record.get("closure"):
                raise ProgramError("this program is not closed")
            record["closure"] = None
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
            plan_record = self.plans.read_record(plan_slug)
            try:
                document = self.plans.head(plan_slug)
                outstanding = sorted(carried_forward(document).values(), key=lambda o: o["id"])
                status = plan_store.derived_status(plan_record)
            except ProgramError:
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
        for child in view.values():
            if child["status"] in ("missing", "unreadable"):
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
                "by_leaf": by_leaf, "unknown": unknown, "analysis": analysis}

    def outstanding_obligations(self, record: dict) -> list:
        """The union of every OPEN leaf's carried obligations. See `obligation_report`, which also
        says whose debt each one is and when the answer is not computable."""
        return self.obligation_report(record)["obligations"]

    def derived_status(self, record: dict) -> str:
        """Programs have no seal and no separate completion act.

        Sealing a child seals that child. Completion is derived from every child being complete, so a
        program can never be marked finished while a child is not — the one thing a human summary of
        a multi-PR effort reliably gets wrong.
        """
        if record.get("closure"):
            return record["closure"]["state"]
        view = self.child_view(record)
        if not view:
            return "empty"
        statuses = [child["status"] for child in view]
        if all(status == "complete" for status in statuses):
            return "complete"
        if any(status in ("missing", "unreadable") for status in statuses):
            return "needs-attention"
        if any(status == "active" for status in statuses):
            return "active"
        return "in-progress"


def render(library: ProgramLibrary, record: dict) -> str:
    """The program as an operator reads it: what it is for, its children and where each stands, every
    obligation still owed and whose it is, and anything about the record that does not add up."""
    view = library.child_view(record)
    report = library.obligation_report(record)
    analysis = report["analysis"]
    out = [f"# {record['title']}", "",
           "<!-- generated from the program record and its children; edits here are overwritten -->", "",
           f"- **Program**: `{record['program_id']}`",
           f"- **Status**: {library.derived_status(record)} — derived from the children, never stored",
           f"- **Children**: {len(view)}", ""]
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
        out += ["", "Each must be answered by the next child on ITS OWN branch — satisfied, still "
                    "carried, or released with a reason. None of them can be dropped by saying nothing."]
    elif not report["unknown"]:
        out.append("_None outstanding._")
    released = _released(library, record)
    if released:
        out += ["", "## Obligations released along the way", ""]
        for obligation, plan_id in released:
            out.append(f"- **{obligation['id']}** — {obligation['statement']}")
            # No fallback string. There used to be a "(no reason given)" here, and printing it was the
            # projection quietly accepting the one shape the guarantee forbids. A release now carries
            # its reason by schema and by add_child, so a missing one is a corrupt record, and the
            # KeyError that follows is the honest report of that — not a hole to paper over.
            out.append(f"  - Released in `{plan_id}`: {obligation['reason']}")
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
