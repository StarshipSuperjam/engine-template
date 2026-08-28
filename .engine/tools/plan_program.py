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

            child = {"plan_id": plan_id, "position": len(record["children"]) + 1, "added_at": _now()}
            if predecessor_id:
                child["predecessor_plan_id"] = predecessor_id
            record["children"].append(child)
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
        """What this program still owes, per LEAF, and honestly when it cannot tell.

        Three corrections to reading the last array element, each of which was wrong on a real shelf:

        1. A leaf, not the tail. `view[-1]` is the last child by stored position; once the chain forks
           the other branch's debts vanished from the only number an operator reads. The answer is the
           union over every branch end.
        2. OPEN leaves only. A retired or abandoned branch is a decision to stop, and its carries died
           with it — a live program on this workstation has exactly that shape, with a debt the
           surviving branch deliberately RELEASED still sitting on the dead leaf. Unioning it back in
           would resurrect an obligation someone consciously let go, which is a new wrong answer, not
           a fix.
        3. Unknown is never zero. A missing or unreadable child, or a cycle leaving no leaf at all,
           means the debt cannot be computed — and printing `0 outstanding` for exactly the corrupted
           programs this reporting exists to expose is the same silent decay one level up. Those cases
           populate `unknown`, and every caller renders it instead of a count.
        """
        analysis = chain_analysis(record)
        view = {child["plan_id"]: child for child in self.child_view(record)}
        unknown, by_leaf, obligations = [], {}, {}
        for child in view.values():
            if child["status"] in ("missing", "unreadable"):
                unknown.append(f"{child['plan_id']} is {child['status']}")
        for plan_id in analysis["leaves"]:
            child = view.get(plan_id)
            if child is None or child["status"] in ("missing", "unreadable"):
                continue
            if child["status"] in DEAD_BRANCH_STATES:
                continue          # a branch someone stopped; its carries stopped with it
            if child["outstanding"]:
                by_leaf[plan_id] = child["outstanding"]
                for obligation in child["outstanding"]:
                    obligations[obligation["id"]] = obligation
        if not analysis["leaves"] and record["children"]:
            unknown.append("no branch of this chain ends — every child is succeeded by another, "
                           "which means the predecessor edges form a cycle")
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

    if analysis["dangling"] or analysis["unreachable"] or len(analysis["roots"]) > 1:
        out += ["## What does not add up in this record", ""]
        for entry in analysis["dangling"]:
            out.append(f"- `{entry['plan_id']}` declares `{entry['predecessor_plan_id']}` as its "
                       "predecessor, and no such child is in this program.")
        for plan_id in analysis["unreachable"]:
            if any(entry["plan_id"] == plan_id for entry in analysis["dangling"]):
                continue
            out.append(f"- `{plan_id}` cannot be reached from the start of the chain; its predecessor "
                       "edges lead in a circle.")
        if len(analysis["roots"]) > 1:
            out.append("- More than one child declares no predecessor: "
                       + ", ".join(f"`{name}`" for name in analysis["roots"])
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
        for leaf, obligations in sorted(report["by_leaf"].items()):
            # Attributed per leaf, because on a forked chain "what is still owed" and "who owes it"
            # are different questions, and only the second one can be answered by a successor.
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
