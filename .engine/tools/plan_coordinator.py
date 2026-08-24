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


# --- governance --------------------------------------------------------------

def cmd_approve(args) -> int:
    library = _library(args)
    slug = _select(library, args.plan)
    record = library.read_record(slug)
    if record.get("seal"):
        raise PlanCoordinatorError("this plan is sealed; a seal is terminal and nothing about it changes")
    digest = record["current"]["plan_digest"]
    if not was_previewed(library, slug, digest):
        raise PlanCoordinatorError(
            f"the full plan has not been presented at this revision. Run `preview {args.plan}` first — "
            "approving a plan nobody has read is what this refusal exists to prevent.")
    if args.depth not in DEPTHS:
        raise PlanCoordinatorError(f"unknown review depth {args.depth!r}; choose one of "
                                   + ", ".join(DEPTHS))
    revision = record["current"]["revision"]
    library.update_record(slug, lambda r: r.update({"approval": {
        "revision": revision, "plan_digest": digest, "depth": args.depth, "at": _now()}}),
        expected_revision=revision)
    print(f"approved revision {revision} of {record['plan_id']} at {args.depth} depth")
    print(f"  bound to {digest}")
    print("\nnext: run the one cold plan review against this revision.")
    return 0


def cmd_review_packet(args) -> int:
    """Emit the packet the cold lenses read: the whole plan, at a named digest.

    The digest is what makes a receipt mean anything later — a lens receipt that did not name what it
    read could be replayed against any revision.
    """
    library = _library(args)
    slug = _select(library, args.plan)
    record = library.read_record(slug)
    approval = record.get("approval")
    if not approval:
        raise PlanCoordinatorError("approve the plan and choose a review depth before building a packet")
    if plan_store.approval_is_stale(record):
        raise PlanCoordinatorError(
            f"the approval covers revision {approval['revision']}, but the plan has been revised since "
            f"and never reviewed. Re-preview and re-approve at revision {record['current']['revision']} "
            "so the review reads what the operator actually approved.")
    document = library.head(slug)
    packet = plan_projection.render_plan(document, record)
    packet_digest = core.digest(packet.encode("utf-8"))
    header = (f"Plan review packet — {record['plan_id']} revision {record['current']['revision']}\n"
              f"Plan digest: {record['current']['plan_digest']}\n"
              f"Packet digest: {packet_digest}\n"
              f"Depth: {approval['depth']} — {DEPTHS[approval['depth']]}\n"
              + "=" * 78 + "\n\n")
    if args.output:
        Path(args.output).write_text(header + packet, encoding="utf-8")
        print(f"packet written to {args.output}")
    else:
        print(header + packet)
    print(f"\npacket digest: {packet_digest}", file=sys.stderr)
    return 0


def cmd_review_record(args) -> int:
    """Record the ONE cold review for this approved revision.

    Single-minted on purpose. A plan whose every revision re-triggered a panel would never converge,
    which is the failure the Build side already removed; folding fixes in afterwards is covered by the
    seal's proportional delta judgment, not by another panel.
    """
    library = _library(args)
    slug = _select(library, args.plan)
    record = library.read_record(slug)
    if record.get("plan_review"):
        existing = record["plan_review"]
        raise PlanCoordinatorError(
            f"a plan review is already recorded for revision {existing['revision']} of this plan, and "
            "there is exactly one per plan. Fold the fixes in as revisions and let the seal's delta "
            "judgment cover them; re-running the panel on a churning plan is the loop this refuses to "
            "rebuild. If the shape itself is wrong, that is a scrap-and-redesign decision for the "
            "operator, not another review.")
    approval = record.get("approval")
    if not approval:
        raise PlanCoordinatorError("a review records findings against an APPROVED revision; approve first")
    if plan_store.approval_is_stale(record):
        raise PlanCoordinatorError(
            f"the approval covers revision {approval['revision']} but the head is revision "
            f"{record['current']['revision']}; re-approve before recording a review")
    findings = json.loads(core.input_text(args.findings)) if args.findings else []
    if not args.lens:
        raise PlanCoordinatorError("name at least one lens the review was run through")
    review = {
        "revision": approval["revision"],
        "plan_digest": approval["plan_digest"],
        "packet_digest": args.packet_digest,
        "at": _now(),
        "lenses": list(args.lens),
        "findings": findings,
    }
    library.update_record(slug, lambda r: r.update({"plan_review": review}))
    blocking = [f for f in findings if f["severity"] == "blocking"]
    print(f"recorded a {len(args.lens)}-lens review of revision {approval['revision']}: "
          f"{len(findings)} finding(s), {len(blocking)} blocking")
    if findings:
        print("\nnext: disposition every finding, then fold fixes in as revisions.")
    return 0


def cmd_finding_dispose(args) -> int:
    library = _library(args)
    slug = _select(library, args.plan)
    record = library.read_record(slug)
    review = record.get("plan_review")
    if not review:
        raise PlanCoordinatorError("no plan review is recorded, so there is nothing to disposition")
    match = [f for f in review.get("findings", []) if f["id"] == args.id]
    if not match:
        known = ", ".join(f["id"] for f in review.get("findings", [])) or "none"
        raise PlanCoordinatorError(f"no finding {args.id!r} in this review; it holds: {known}")

    def change(current):
        for finding in current["plan_review"]["findings"]:
            if finding["id"] == args.id:
                finding["disposition"] = args.disposition
                finding["rationale"] = args.rationale
    library.update_record(slug, change)
    outstanding = [f["id"] for f in library.read_record(slug)["plan_review"]["findings"]
                   if not f.get("disposition")]
    print(f"{args.id}: {args.disposition}")
    print(f"outstanding: {', '.join(outstanding) if outstanding else 'none'}")
    return 0


def seal_refusals(library: plan_store.PlanLibrary, slug: str) -> list:
    """Every reason this plan may not be sealed, together, in operator-facing language.

    All at once rather than one at a time: someone getting a plan to a seal should see everything in
    the way in a single reading, not discover the next obstacle only after clearing the last.
    """
    record = library.read_record(slug)
    refusals = []
    if record.get("seal"):
        return [f"this plan was already sealed at revision {record['seal']['revision']}. A seal is "
                "terminal and single-minted: to change the plan now, clone it into a new one."]
    if record.get("closure"):
        return [f"this plan is {record['closure']['state']} ({record['closure']['reason']}); reopen it "
                "before sealing."]
    refusals.extend(library.verify_chain(slug))
    try:
        document = library.head(slug)
    except PlanCoordinatorError as exc:
        return refusals + [str(exc)]
    # Unresolved decisions, unresolved assumptions, and a payload the Build Coordinator would refuse
    # all arrive from the contract — delegated, not re-expressed here.
    refusals.extend(plan_contract.seal_blockers(document))
    approval = record.get("approval")
    if not approval:
        refusals.append("the plan has not been approved at any revision")
    elif plan_store.approval_is_stale(record):
        refusals.append(
            f"the approval covers revision {approval['revision']} but the plan changed before it was "
            "ever reviewed, so nothing reviewed reflects what was approved; re-preview and re-approve")
    review = record.get("plan_review")
    if not review:
        refusals.append("no cold plan review has been recorded; a sealed plan is by definition a "
                        "reviewed one")
    elif approval and review["revision"] != approval["revision"]:
        refusals.append(f"the review covers revision {review['revision']} but the approval covers "
                        f"revision {approval['revision']}")
    if review:
        outstanding = [f["id"] for f in review.get("findings", []) if not f.get("disposition")]
        if outstanding:
            refusals.append("these findings have no disposition: " + ", ".join(outstanding))
    return refusals


def cmd_seal(args) -> int:
    """The terminal act. Nothing locks before this, and nothing changes after it.

    There is deliberately no sealed-but-failed state. A plan carrying blocking findings simply stays
    an unsealed draft that still carries them — editable, resumable, on the shelf — because a plan
    stuck in a limbo it cannot leave is worse than one that plainly is not ready.
    """
    library = _library(args)
    slug = _select(library, args.plan)
    refusals = seal_refusals(library, slug)
    if refusals:
        print(f"not sealing {slug}; it remains an editable draft:", file=sys.stderr)
        for refusal in refusals:
            print(f"  - {refusal}", file=sys.stderr)
        return 1

    record = library.read_record(slug)
    document = library.head(slug)
    reviewed_digest = record["plan_review"]["plan_digest"]
    sealed_digest = record["current"]["plan_digest"]
    changed = reviewed_digest != sealed_digest
    if changed and not args.delta_judgment:
        raise PlanCoordinatorError(
            f"the plan changed after its review (reviewed revision {record['plan_review']['revision']}, "
            f"sealing revision {record['current']['revision']}). That is the expected shape — fixes fold "
            "in as revisions — but the delta needs one proportional judgment before it locks. Read it "
            f"with `diff {args.plan} --from {record['plan_review']['revision']} --to "
            f"{record['current']['revision']}`, then seal with --delta-judgment none or scoped.")
    if args.delta_judgment == "scoped" and not args.delta_rationale:
        raise PlanCoordinatorError("a scoped delta judgment needs a rationale saying what changed and "
                                   "why it is still the reviewed plan")
    judgment = args.delta_judgment or "none"

    seal = {
        "revision": record["current"]["revision"],
        "reviewed_digest": reviewed_digest,
        "sealed_digest": sealed_digest,
        "build_plan_digest": plan_contract.build_plan_digest(document),
        "at": _now(),
        "delta_judgment": judgment,
    }
    if args.delta_rationale:
        seal["delta_rationale"] = args.delta_rationale
    library.update_record(slug, lambda r: r.update({"seal": seal}),
                          expected_revision=record["current"]["revision"])
    plan_projection.project_library(library)
    print(f"sealed {record['plan_id']} at revision {seal['revision']}")
    print(f"  reviewed  {reviewed_digest}")
    print(f"  sealed    {sealed_digest}"
          + ("  (unchanged since review)" if not changed else f"  (delta judged {judgment})"))
    print(f"  payload   {seal['build_plan_digest']}")
    if changed:
        print("\nThe PR must disclose that the sealed plan differs from the reviewed one, and by what.")
    return 0


def cmd_close(args) -> int:
    """retire / abandon / complete — how a plan ends. None of them deletes anything."""
    library = _library(args)
    slug = _select(library, args.plan)
    record = library.read_record(slug)
    if record.get("closure"):
        raise PlanCoordinatorError(
            f"this plan is already {record['closure']['state']}; reopen it before closing it differently")
    library.update_record(slug, lambda r: r.update({"closure": {
        "state": args.state, "at": _now(), "reason": args.reason}}))
    plan_projection.project_library(library)
    print(f"{record['plan_id']} is now {args.state}: {args.reason}")
    print("Nothing was deleted — the plan and every revision stay on the shelf.")
    return 0


def cmd_reopen(args) -> int:
    """Undo a retirement or an abandonment. Deliberately CANNOT undo a seal.

    Retiring and abandoning are bookkeeping about attention, and an operator may change their mind.
    A seal is a promise that a specific plan, at a specific digest, was reviewed and handed to a
    Build — and unsealing would let a plan be edited while something downstream still believes it
    said what it said. The way past a seal is a new plan, which is why `clone` exists.
    """
    library = _library(args)
    slug = _select(library, args.plan)
    record = library.read_record(slug)
    if not record.get("closure"):
        raise PlanCoordinatorError("this plan is not closed, so there is nothing to reopen")
    if record.get("seal"):
        raise PlanCoordinatorError(
            "this plan is sealed, and a seal is terminal — reopening it would let an edited plan keep "
            "a digest a Build already trusted. Clone it into a new plan instead.")
    previous = record["closure"]["state"]
    library.update_record(slug, lambda r: r.update({"closure": None}))
    plan_projection.project_library(library)
    print(f"reopened {record['plan_id']} (was {previous})")
    return 0


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

    approve = sub.add_parser("approve", help="bind a review depth to this revision's digest")
    approve.add_argument("plan")
    approve.add_argument("--depth", required=True, choices=sorted(DEPTHS))
    approve.set_defaults(func=cmd_approve)

    review = sub.add_parser("review", help="the one cold plan review").add_subparsers(
        dest="review_command", required=True)
    packet = review.add_parser("packet", help="the plan as the cold lenses read it, at a named digest")
    packet.add_argument("plan")
    packet.add_argument("--output")
    packet.set_defaults(func=cmd_review_packet)
    record_review = review.add_parser("record", help="record the review — once per plan")
    record_review.add_argument("plan")
    record_review.add_argument("--lens", action="append", required=True)
    record_review.add_argument("--packet-digest", required=True)
    record_review.add_argument("--findings", help="a JSON array of findings")
    record_review.set_defaults(func=cmd_review_record)

    finding = sub.add_parser("finding", help="adjudicate review findings").add_subparsers(
        dest="finding_command", required=True)
    dispose = finding.add_parser("dispose", help="record how one finding was answered")
    dispose.add_argument("plan")
    dispose.add_argument("--id", required=True)
    dispose.add_argument("--disposition", required=True,
                         choices=["accepted-fixed", "accepted-tracked", "partially-accepted",
                                  "rejected", "escalated"])
    dispose.add_argument("--rationale", required=True)
    dispose.set_defaults(func=cmd_finding_dispose)

    seal = sub.add_parser("seal", help="the terminal act — nothing locks before it")
    seal.add_argument("plan")
    seal.add_argument("--delta-judgment", choices=["none", "scoped"])
    seal.add_argument("--delta-rationale")
    seal.set_defaults(func=cmd_seal)

    for state, helptext in (("retire", "superseded by a later plan, kept for the record"),
                            ("abandon", "deliberately dropped"),
                            ("complete", "the Build it authorized was merged")):
        closer = sub.add_parser(state, help=helptext)
        closer.add_argument("plan")
        closer.add_argument("--reason", required=True)
        closer.set_defaults(func=cmd_close,
                            state={"retire": "retired", "abandon": "abandoned",
                                   "complete": "complete"}[state])

    reopen = sub.add_parser("reopen", help="undo a retirement or abandonment (never a seal)")
    reopen.add_argument("plan")
    reopen.set_defaults(func=cmd_reopen)
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
