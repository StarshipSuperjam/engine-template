#!/usr/bin/env python3
"""The Plan Coordinator: the Build Coordinator's upstream peer.

The Build Coordinator owns execution and becomes authoritative at `plan bind`. Everything upstream of
that — grounding, deliberation, authoring the graph, presenting it, deciding it is good enough — was
convention a session was trusted to remember. This tool owns that half.

Its shape mirrors the Build side deliberately, because an operator should not have to learn two
vocabularies: read verbs derive and never write, governance verbs record evidence, and status is
DERIVED from that evidence rather than stored. The one structural difference is that a plan is
durable and a Build snapshot is not.

Two rules run through every verb here.

NOTHING AUTO-SELECTS. Not the newest plan, not the only plan. A shelf is not a queue, and the way the
wrong plan gets sealed is a tool helpfully picking one.

DEPTH IS OFFERED ONLY AFTER THE FULL REVISION HAS BEEN RENDERED. `depths` refuses until `preview` has
put the whole plan in front of the operator at the digest being approved. Approving a review depth
for a plan nobody has read is the failure this exists to prevent, and enforcing it in code rather
than in prose is what makes it hold in a session that has forgotten why.

This module is the read-and-derive surface (init, list, show, resume, diff, validate, preview,
reindex, doctor). The governance verbs and the terminal seal live alongside it.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import difflib
import json
from pathlib import Path
import sys

import build_coordinator_core as core
import plan_contract
import plan_projection
import plan_store

PlanCoordinatorError = plan_store.PlanStoreError

# Review depths, offered only after a full render. The names match the Build side's own vocabulary so
# an operator meets one set of words across both coordinators.
DEPTHS = {
    "light": "One architecture lens. For a plan whose shape is already settled and whose risk is low.",
    "standard": "Architecture, feasibility, product intent, risk and governance — the four-lens panel.",
    "thorough": "The four-lens panel, with security-governance and technical-integrity added at the "
                "deliverable stage. For a plan that touches secrets, data durability, or a guardrail.",
}


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _library(args) -> plan_store.PlanLibrary:
    return plan_store.PlanLibrary(args.library) if getattr(args, "library", None) else plan_store.PlanLibrary()


def _select(library: plan_store.PlanLibrary, selector: str) -> str:
    return library.resolve(selector)


# --- read and derive ---------------------------------------------------------

def status_of(library: plan_store.PlanLibrary, slug: str) -> tuple[str, dict, dict | None, list]:
    """(status, record, head document or None, seal blockers). The single place status is computed,
    so `list`, `show` and the projections can never disagree about what a plan's state is."""
    record = library.read_record(slug)
    try:
        document = library.head(slug)
    except PlanCoordinatorError:
        return plan_store.derived_status(record), record, None, []
    blockers = plan_contract.seal_blockers(document)
    return plan_store.derived_status(record, head_blockers=blockers), record, document, blockers


def cmd_init(args) -> int:
    library = _library(args)
    document = json.loads(core.input_text(args.document))
    intake = json.loads(core.input_text(args.intake)) if args.intake else None
    slug = library.create(document, intake=intake)
    plan_projection.project_library(library)
    print(f"created {document['plan_id']} at {library.plan_dir(slug)}")
    print(f"read it at {library.plan_dir(slug) / plan_projection.PLAN_MD}")
    warning = plan_store.volume_warning(library.root)
    if warning:
        print(f"\nwarning: {warning}", file=sys.stderr)
    return 0


def cmd_list(args) -> int:
    library = _library(args)
    slugs = library.slugs()
    if not slugs:
        print(f"no plans in {library.root}")
        return 0
    rows = []
    for slug in slugs:
        status, record, _, _ = status_of(library, slug)
        rows.append((record["plan_id"], status, str(record["current"]["revision"]),
                     record["ledger"][-1]["revised_at"], record["title"]))
    widths = [max(len(row[column]) for row in rows) for column in range(4)]
    for row in rows:
        print("  ".join(value.ljust(widths[column]) for column, value in enumerate(row[:4])) + "  " + row[4])
    print(f"\n{len(rows)} plan(s). Select one by id, unique id prefix, or folder name — "
          "nothing here is current by default.")
    return 0


def cmd_show(args) -> int:
    library = _library(args)
    slug = _select(library, args.plan)
    status, record, document, blockers = status_of(library, slug)
    print(f"{record['plan_id']}  {record['title']}")
    print(f"  status      {status}")
    print(f"  revision    {record['current']['revision']} of {len(record['ledger'])}")
    print(f"  digest      {record['current']['plan_digest']}")
    print(f"  payload     {record['current']['build_plan_digest']}")
    print(f"  folder      {library.plan_dir(slug)}")
    for gate, label in (("approval", "approved"), ("plan_review", "reviewed"),
                        ("seal", "sealed"), ("build_binding", "bound")):
        value = record.get(gate)
        if value:
            print(f"  {label:<11} revision {value.get('revision', '—')} at {value['at']}")
    if record.get("closure"):
        print(f"  closed      {record['closure']['state']} — {record['closure']['reason']}")
    problems = library.verify_chain(slug)
    if problems:
        print("\nintegrity problems:")
        for problem in problems:
            print(f"  - {problem}")
    if blockers:
        print("\nnot sealable yet:")
        for blocker in blockers:
            print(f"  - {blocker}")
    elif document and not record.get("seal"):
        print("\nnothing in the plan itself blocks a seal.")
    return 0


def cmd_resume(args) -> int:
    """Pick a plan back up cold: what it is, where it stands, and the one thing to do next.

    `list` answers "what is on the shelf"; this answers "I was working on this, what now" — the
    question an operator actually returns with, and the reason the next step is computed rather than
    left to be inferred from a status word.
    """
    library = _library(args)
    slug = _select(library, args.plan)
    status, record, document, blockers = status_of(library, slug)
    print(f"{record['title']}  ({record['plan_id']}, revision {record['current']['revision']}, {status})")
    print(f"  {library.plan_dir(slug) / plan_projection.PLAN_MD}")
    if document:
        print(f"\nlast revision note: {document.get('revision_note', '—')}")
    problems = library.verify_chain(slug)
    if problems:
        print("\nthis plan needs attention before anything else:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"\nnext: {_next_step(status, record, blockers)}")
    return 0


def _next_step(status: str, record: dict, blockers: list) -> str:
    if status in ("complete", "retired", "abandoned"):
        return f"nothing — this plan is {status} ({record['closure']['reason']})."
    if status == "active":
        return "nothing here — the Build this plan authorized is running."
    if status == "sealed":
        return "hand this plan to a Build; the seal is terminal and the plan is now read-only."
    if status == "review-recorded":
        outstanding = [f for f in (record["plan_review"] or {}).get("findings", [])
                       if not f.get("disposition")]
        if outstanding:
            return (f"disposition {len(outstanding)} outstanding finding(s): "
                    + ", ".join(f["id"] for f in outstanding))
        if blockers:
            return "revise to clear what still blocks the seal, then seal."
        return "seal the plan — it is reviewed and nothing outstanding blocks it."
    if status == "awaiting-review":
        return "run the one cold plan review against the approved revision."
    if status == "awaiting-approval":
        return "preview the full revision, then choose a review depth and approve."
    return ("revise: " + "; ".join(blockers)) if blockers else "revise, then preview and approve."


def cmd_preview(args) -> int:
    """Render the WHOLE current revision. This is the presentation that must precede a depth choice,
    and `depths` will not offer anything until it has happened at the current digest."""
    library = _library(args)
    slug = _select(library, args.plan)
    record = library.read_record(slug)
    document = library.head(slug)
    print(plan_projection.render_plan(document, record))
    _mark_previewed(library, slug, record["current"]["plan_digest"])
    return 0


# The preview marker is intentionally NOT part of plan-record.v1: it is session ergonomics, not plan
# evidence, and putting it in the record would make an operator's reading habits part of the document
# a Build consumes. It lives beside the record, keyed by the digest it was rendered for, so it cannot
# survive a revision and vouch for a plan nobody read.
_PREVIEW_FILENAME = ".previewed"


def _mark_previewed(library: plan_store.PlanLibrary, slug: str, digest: str) -> None:
    core.atomic_write(library.plan_dir(slug) / _PREVIEW_FILENAME, digest + "\n",
                      mode=plan_store.FILE_MODE)


def was_previewed(library: plan_store.PlanLibrary, slug: str, digest: str) -> bool:
    path = library.plan_dir(slug) / _PREVIEW_FILENAME
    try:
        return path.read_text(encoding="utf-8").strip() == digest
    except OSError:
        return False


def cmd_depths(args) -> int:
    library = _library(args)
    slug = _select(library, args.plan)
    record = library.read_record(slug)
    digest = record["current"]["plan_digest"]
    if not was_previewed(library, slug, digest):
        raise PlanCoordinatorError(
            "the full plan has not been presented at this revision, so there is nothing to choose a "
            f"review depth FOR. Run `preview {args.plan}` first. (Approving a depth for a plan nobody "
            "has read is the failure this refusal exists to prevent.)")
    blockers = plan_contract.seal_blockers(library.head(slug))
    print(f"review depths for {record['plan_id']} at revision {record['current']['revision']}:\n")
    for name, description in DEPTHS.items():
        print(f"  {name:<10} {description}")
    if blockers:
        print("\nnote: this plan is not sealable yet, so a review now would review a moving target:")
        for blocker in blockers:
            print(f"  - {blocker}")
    return 0


def cmd_validate(args) -> int:
    library = _library(args)
    slug = _select(library, args.plan)
    problems = library.verify_chain(slug)
    document = None
    try:
        document = library.head(slug)
    except PlanCoordinatorError as exc:
        problems.append(str(exc))
    if problems:
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    blockers = plan_contract.seal_blockers(document)
    print(f"{slug}: the document is valid and the revision chain is sound.")
    if blockers:
        print("It is not sealable yet:")
        for blocker in blockers:
            print(f"  - {blocker}")
    return 0


def cmd_diff(args) -> int:
    """What changed between two revisions, as a readable diff of the RENDERED plan rather than of the
    JSON. An operator deciding whether a delta is proportional is judging the plan, not its encoding,
    and a JSON diff buries a one-word change to an obligation under reformatting noise."""
    library = _library(args)
    slug = _select(library, args.plan)
    record = library.read_record(slug)
    latest = record["current"]["revision"]
    left = args.from_revision if args.from_revision is not None else max(1, latest - 1)
    right = args.to_revision if args.to_revision is not None else latest

    def render(revision: int) -> list:
        document = library.read_revision(slug, revision)
        # Render each side as if that revision were current, so the diff shows the plan changing and
        # not the header disagreeing with itself about which revision it is describing.
        entry = next(e for e in record["ledger"] if e["revision"] == revision)
        stub = dict(record, current={
            "revision": revision,
            "plan_digest": entry["plan_digest"],
            "build_plan_digest": core.digest(document["build_plan"]),
            "snapshot": entry["snapshot"],
        })
        return plan_projection.render_plan(document, stub).splitlines(keepends=True)

    if left == right:
        print(f"revision {left} is the only revision named; nothing to compare.")
        return 0
    lines = list(difflib.unified_diff(render(left), render(right),
                                      fromfile=f"revision {left}", tofile=f"revision {right}", n=2))
    if not lines:
        print(f"revisions {left} and {right} render identically.")
        return 0
    sys.stdout.writelines(lines)
    return 0


def cmd_reindex(args) -> int:
    library = _library(args)
    entries = plan_projection.project_library(library)
    unreadable = [entry for entry in entries if not entry["readable"]]
    print(f"projected {len(entries)} plan(s) into {library.root}")
    for entry in unreadable:
        print(f"  needs attention: {entry['slug']} — {entry['problem']}", file=sys.stderr)
    return 1 if unreadable else 0


def cmd_doctor(args) -> int:
    """Everything that could make this library untrustworthy, in one reading.

    Deliberately does not fix anything. Each finding here is either a decision the operator must make
    (move the library off a synced volume) or evidence of loss they need to see intact (a corrupt
    revision) — and a tool that quietly repaired either would be destroying the information that
    tells them what happened.
    """
    library = _library(args)
    findings = []
    warning = plan_store.volume_warning(library.root)
    if warning:
        findings.append(warning)
    if not library.root.exists():
        print(f"no plan library at {library.root} yet; it is created with the first plan.")
        return 0
    # Every directory, not just the root. The root is the one that actually goes wrong — mkdir's
    # `parents=True` applies its mode only to the leaf — but a check that only looked there would
    # miss a plan folder loosened by hand, and the leak is the same either way: even when the
    # revisions inside are 0600, a readable directory hands over the slug names, which carry titles.
    for directory in [library.root] + sorted(p for p in library.root.rglob("*") if p.is_dir()):
        mode = directory.stat().st_mode & 0o777
        if mode & 0o077:
            where = "library directory" if directory == library.root else "directory"
            findings.append(
                f"the {where} {directory} is mode {mode:04o} and should be 0700. It holds operator "
                "intent; other accounts on this machine can read it, or at least list what is in it.")
    for slug in library.slugs():
        for problem in library.verify_chain(slug):
            findings.append(f"{slug}: {problem}")
    for path in library.root.rglob("*.json"):
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            findings.append(f"the file {path} is mode {mode:04o} and should be 0600.")
    print(f"plan library: {library.root}")
    print(f"plans: {len(library.slugs())}")
    if not findings:
        print("\nno problems found.")
        return 0
    print(f"\n{len(findings)} problem(s):")
    for finding in findings:
        print(f"  - {finding}")
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="plan_coordinator.py",
        description="The planning half of the coordinator pair: durable, local, and nothing auto-selects.")
    parser.add_argument("--library", help="path to the plan library (defaults to this instance's own)")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="mint a plan from a validated engine-plan.v1 revision")
    init.add_argument("--document", required=True)
    init.add_argument("--intake")
    init.set_defaults(func=cmd_init)

    listing = sub.add_parser("list", help="every plan on the shelf with its derived status")
    listing.set_defaults(func=cmd_list)

    for name, function, helptext in (
            ("show", cmd_show, "one plan's identity, gates, and what blocks a seal"),
            ("resume", cmd_resume, "pick a plan back up cold and see the one next step"),
            ("preview", cmd_preview, "render the whole current revision (required before depths)"),
            ("depths", cmd_depths, "the review depths on offer, once the plan has been presented"),
            ("validate", cmd_validate, "check the document and the revision chain"),
            ("reindex", cmd_reindex, "regenerate every projection from the revisions"),
            ("doctor", cmd_doctor, "everything that could make this library untrustworthy")):
        command = sub.add_parser(name, help=helptext)
        if name not in ("reindex", "doctor"):
            command.add_argument("plan", help="full id, unique id prefix, or folder name")
        command.set_defaults(func=function)

    diff = sub.add_parser("diff", help="what changed between two revisions, as rendered prose")
    diff.add_argument("plan")
    diff.add_argument("--from", dest="from_revision", type=int)
    diff.add_argument("--to", dest="to_revision", type=int)
    diff.set_defaults(func=cmd_diff)
    return parser


def main(argv: list | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except PlanCoordinatorError as exc:
        print(f"plan-coordinator: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
