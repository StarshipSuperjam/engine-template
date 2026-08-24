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


def dropped_obligations(predecessor: dict, successor: dict) -> list:
    """Obligations the predecessor was carrying that the successor does not mention at all.

    Not mentioning one is the failure this object exists for — it is indistinguishable, from the
    outside, from having decided it no longer matters, except that nobody decided anything.
    """
    successor_program = successor.get("program") or {}
    named = {o["id"] for o in successor_program.get("carried_obligations", [])}
    return [obligation for identifier, obligation in sorted(carried_forward(predecessor).items())
            if identifier not in named]


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
        """Each child with its plan's derived status, in declared order. Missing plans are reported
        as missing rather than skipped: a child that is not in this library is a fact about the
        program, and quietly omitting it would make the program look shorter than it is."""
        view = []
        for child in sorted(record["children"], key=lambda c: c["position"]):
            try:
                plan_slug = self.plans.resolve(child["plan_id"])
            except ProgramError:
                view.append({**child, "slug": None, "title": "(not in this library)",
                             "status": "missing", "outstanding": []})
                continue
            plan_record = self.plans.read_record(plan_slug)
            try:
                document = self.plans.head(plan_slug)
                outstanding = sorted(carried_forward(document).values(), key=lambda o: o["id"])
                status = plan_store.derived_status(plan_record)
            except ProgramError:
                outstanding, status = [], "unreadable"
            view.append({**child, "slug": plan_slug, "title": plan_record["title"],
                         "status": status, "outstanding": outstanding})
        return view

    def outstanding_obligations(self, record: dict) -> list:
        """Every obligation still declared as carried by the LAST child, which is where the chain
        currently ends. Earlier children's carries were already answered for by their successors —
        that is exactly what add_child enforced — so reporting them again would double-count debts
        that have in fact moved forward."""
        view = self.child_view(record)
        return view[-1]["outstanding"] if view else []

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
    """The program as an operator reads it: what it is for, its children and where each stands, and
    every obligation still owed."""
    view = library.child_view(record)
    out = [f"# {record['title']}", "",
           "<!-- generated from the program record and its children; edits here are overwritten -->", "",
           f"- **Program**: `{record['program_id']}`",
           f"- **Status**: {library.derived_status(record)} — derived from the children, never stored",
           f"- **Children**: {len(view)}", ""]
    out += ["## Objective", "", record["objective"], ""]
    out += ["## Children, in the order they were decided", "",
            "| # | Plan | Status | Succeeds |", "|---:|---|---|---|"]
    for child in view:
        title = child["title"].replace("|", "\\|")
        succeeds = f"`{child['predecessor_plan_id']}`" if child.get("predecessor_plan_id") else "—"
        out.append(f"| {child['position']} | {title} (`{child['plan_id']}`) | {child['status']} "
                   f"| {succeeds} |")
    out += ["", "_Order records a decision. Nothing here selects, starts, or advances a child._", ""]

    outstanding = library.outstanding_obligations(record)
    out += ["## Obligations still carried", ""]
    if outstanding:
        for obligation in outstanding:
            out.append(f"- **{obligation['id']}** — {obligation['statement']}")
        out += ["", "Each must appear in the next child as satisfied, still carried, or released with "
                    "a reason. None of them can be dropped by saying nothing."]
    else:
        out.append("_None outstanding._")
    released = _released(library, record)
    if released:
        out += ["", "## Obligations released along the way", ""]
        for obligation, plan_id in released:
            out.append(f"- **{obligation['id']}** — {obligation['statement']}")
            out.append(f"  - Released in `{plan_id}`: {obligation.get('reason', '(no reason given)')}")
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
