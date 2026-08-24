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
import difflib
import json
from pathlib import Path
import sys

import build_coordinator_core as core
import moment
import plan_contract
import plan_program
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


_now = moment.utc_now


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
        # Two different sentences, because there are two different situations and the earlier single
        # template collapsed them into "but the gates still do: nothing outstanding blocks it".
        step = _next_step(status, record, blockers)
        if status == "review-recorded" and not blockers:
            print(f"\nready to seal: {step}")
        else:
            print(f"\nnothing in the plan document itself blocks a seal, but the gates do — {step}")
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
        return ("this plan is sealed and read-only. Handing a sealed plan to a Build is not wired up "
                "yet — the Build Coordinator still takes whatever plan a session hands it — so for now the "
                "seal is the record that this plan was reviewed and settled. To keep working on the "
                "idea, `clone` it into a new plan.")
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
    # verify_chain already walks the head as one of its ledger entries, so reading it again here
    # would report a damaged head twice and read as two faults where there is one.
    problems = library.verify_chain(slug)
    document = None
    if not problems:
        document = library.head(slug)
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
    # force=True: reindex is the verb that exists to rebuild everything from the revisions, including
    # the closed plans the incremental path skips. If it inherited that skip it would stop being the
    # answer to "regenerate the library" and become a slightly cheaper no-op.
    library = _library(args)
    entries = plan_projection.project_library(library, force=True)
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
                "intent; other accounts on this machine can read it, or at least list what is in it. "
                f"Fix: chmod 700 {directory}")
    for slug in library.slugs():
        for problem in library.verify_chain(slug):
            findings.append(f"{slug}: {problem}")
    for path in library.root.rglob("*.json"):
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            findings.append(f"the file {path} is mode {mode:04o} and should be 0600. "
                            f"Fix: chmod 600 {path}")
    print(f"plan library: {library.root}")
    print(f"plans: {len(library.slugs())}")
    if not plan_store.volume_determined(library.root):
        # Said plainly rather than folded into "no problems found": a check that could not run and a
        # check that passed are different facts, and only one of them is reassuring.
        print("\nnote: this platform would not tell me what filesystem the library sits on, so the "
              "network-volume check did not run. If it is on a network share, the store's file lock "
              "is not reliable across hosts.")
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

    def approve(current):
        if current.get("seal"):          # re-asserted inside the lock, not from the copy above
            raise PlanCoordinatorError("this plan was sealed while you were reading it; a seal is terminal")
        current["approval"] = {"revision": revision, "plan_digest": digest,
                               "depth": args.depth, "at": _now()}

    library.update_record(slug, approve, expected_revision=revision)
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
    def record_review(current):
        # INSIDE the lock. Recording a review does not mint a revision, so the compare-and-swap on
        # `current.revision` cannot catch a concurrent second review — only re-checking here can, and
        # "exactly one review per plan" is worth exactly as much as this line.
        if current.get("plan_review"):
            raise PlanCoordinatorError(
                "another session recorded a plan review while this one was being prepared, and there "
                "is exactly one per plan. Re-read the plan before deciding what to do next.")
        current["plan_review"] = review

    library.update_record(slug, record_review)
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
        print(f"not sealing {library.read_record(slug)['plan_id']}; it remains an editable draft:",
              file=sys.stderr)
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
    def mint_seal(current):
        if current.get("seal"):          # re-asserted inside the lock; a seal is minted once
            raise PlanCoordinatorError("another session sealed this plan while this one was reading it")
        current["seal"] = seal

    library.update_record(slug, mint_seal, expected_revision=record["current"]["revision"])
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
    def close(current):
        if current.get("closure"):       # re-asserted inside the lock
            raise PlanCoordinatorError(
                f"another session closed this plan as {current['closure']['state']} while this one was "
                "reading it; reopen it before closing it differently")
        current["closure"] = {"state": args.state, "at": _now(), "reason": args.reason}

    library.update_record(slug, close)
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
    previous = {}

    def reopen(current):
        previous["state"] = current["closure"]["state"]   # read under the lock, not before it
        if not current.get("closure"):   # re-asserted inside the lock
            raise PlanCoordinatorError("another session reopened this plan already")
        if current.get("seal"):
            raise PlanCoordinatorError("this plan is sealed, and a seal is terminal")
        current["closure"] = None

    library.update_record(slug, reopen)
    plan_projection.project_library(library)
    print(f"reopened {record['plan_id']} (was {previous['state']})")
    return 0


# --- revision and transport ---------------------------------------------------

def cmd_revise(args) -> int:
    """The ONE verb that mints a revision. Nothing else writes a revision file, ever.

    A single minting path is what makes the ledger trustworthy: if a second route existed, a revision
    could reach disk without a compare-and-swap, without validation, or without a ledger entry, and
    the chain would be sound-looking and wrong.
    """
    library = _library(args)
    slug = _select(library, args.plan)
    document = json.loads(core.input_text(args.document))
    record = library.read_record(slug)
    if record.get("seal"):
        raise PlanCoordinatorError(
            "this plan is sealed and a seal is terminal; `clone` it into a new plan to keep working")
    expected = args.expect_revision if args.expect_revision is not None else record["current"]["revision"]
    updated = library.append_revision(slug, document, expected_revision=expected)
    plan_projection.project_library(library)
    print(f"revision {updated['current']['revision']} of {updated['plan_id']}")
    print(f"  digest {updated['current']['plan_digest']}")
    if plan_store.approval_is_stale(updated):
        print("\nthe approval covered an earlier revision and this plan was never reviewed, so the "
              "approval no longer speaks for the head: preview and approve again.")
    elif updated.get("plan_review"):
        print("\nthis revision folds a fix in after the review. The panel does NOT re-run; the seal "
              "will ask for one proportional judgment of the delta.")
    return 0


def cmd_clone(args) -> int:
    """Start a new plan from an existing one. The way past a seal, and the only way.

    A clone mints a NEW id and carries NO approvals, no review, no seal — because none of that
    evidence was granted for this document. Carrying it forward would let a fresh plan inherit a
    reviewed-ness nobody granted it, which is precisely the laundering the seal exists to prevent.
    """
    library = _library(args)
    slug = _select(library, args.plan)
    document = dict(library.head(slug))
    source_id = document["plan_id"]
    document["plan_id"] = plan_store.mint_plan_id()
    document["revision"] = 1
    document["title"] = args.title or f"{document['title']} (continued)"
    document["created_at"] = document["revised_at"] = _now()
    document["revision_note"] = args.reason
    document.pop("program", None)
    new_slug = library.create(document, intake={
        "provenance": f"cloned from {source_id} at revision {library.read_record(slug)['current']['revision']}: "
                      f"{args.reason}",
        "predecessors": [f"{source_id} — {library.read_record(slug)['title']}"]})
    plan_projection.project_library(library)
    print(f"cloned {source_id} into {document['plan_id']} at {library.plan_dir(new_slug)}")
    print("It carries no approval, no review and no seal — none of that was granted for this document.")
    return 0


def build_bundle(library: plan_store.PlanLibrary, slug: str) -> dict:
    """A plan as a self-contained, self-verifying local bundle.

    Carries the record and every readable revision. A redacted revision travels as its ledger entry
    only — the body was excised on purpose and an export that resurrected it would defeat the
    redaction, which is the one thing a transport format must not do.
    """
    record = library.read_record(slug)
    revisions = {}
    for entry in record["ledger"]:
        if "redacted" in entry:
            continue
        revisions[str(entry["revision"])] = library.read_revision(slug, entry["revision"])
    bundle = {"schema_version": "plan-bundle.v1", "record": record, "revisions": revisions}
    bundle["bundle_digest"] = core.digest({"record": record, "revisions": revisions})
    return bundle


def cmd_export(args) -> int:
    library = _library(args)
    slug = _select(library, args.plan)
    bundle = build_bundle(library, slug)
    path = Path(args.output)
    # The bundle holds the plan in full, so its destination gets the same owner-only treatment the
    # library itself has. No `within` here: the operator chose this path deliberately and it is
    # SUPPOSED to be outside the library — that is what an export is for.
    plan_store.ensure_dir(path.parent)
    core.atomic_write(path, json.dumps(bundle, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                      mode=plan_store.FILE_MODE)
    redacted = [e["revision"] for e in bundle["record"]["ledger"] if "redacted" in e]
    print(f"exported {bundle['record']['plan_id']} to {path} ({len(bundle['revisions'])} revision(s))")
    print(f"  bundle digest {bundle['bundle_digest']}")
    if redacted:
        print(f"  revision(s) {', '.join(str(r) for r in redacted)} travel as ledger entries only; "
              "their bodies were redacted and are not resurrected here.")
    print("\nThis is a local file. Nothing was uploaded, and moving it is your decision — it holds the "
          "plan in full.")
    return 0


def cmd_import(args) -> int:
    """Read a bundle back, verifying every digest AND every path before anything reaches the library.

    A bundle is the one way a plan crosses a trust boundary — a backup, another machine, a colleague —
    so it is the one place where bytes claiming to be a plan have not already been proven to be one.
    Two distinct things therefore have to be checked, and checking only the first is the trap:

      CONTENT is proven by digest. But every digest in a bundle is computed by whoever built it, so
      self-consistency proves only that the file was not corrupted in transit — never that its author
      was honest.

      SHAPE is proven by the schema, and it has to be proven BEFORE anything is written. `slug` and
      each `snapshot` become filesystem paths; an absolute value silently discards the library root
      it was joined to, and `..` walks out. Validating afterwards (which `verify_chain` does) is too
      late: by then the writes have landed wherever the bundle asked.
    """
    library = _library(args)
    bundle = json.loads(core.input_text(args.bundle))
    if bundle.get("schema_version") != "plan-bundle.v1":
        raise PlanCoordinatorError(
            f"not a plan bundle (schema_version {bundle.get('schema_version')!r})")
    record, revisions = bundle["record"], bundle["revisions"]
    # Shape first. The record's own schema pattern-constrains `slug` and every `snapshot`, so this one
    # call is what stops a crafted bundle choosing where the store writes.
    core.validate(record, plan_store.RECORD_SCHEMA)
    recomputed = core.digest({"record": record, "revisions": revisions})
    if recomputed != bundle.get("bundle_digest"):
        raise PlanCoordinatorError(
            f"the bundle does not match its own digest (recorded {bundle.get('bundle_digest')}, found "
            f"{recomputed}); it was altered after export and is not trustworthy")
    for entry in record["ledger"]:
        if "redacted" in entry:
            continue
        body = revisions.get(str(entry["revision"]))
        if body is None:
            raise PlanCoordinatorError(
                f"the bundle's ledger claims revision {entry['revision']} but carries no body for it")
        actual = core.digest(body)
        if actual != entry["plan_digest"]:
            raise PlanCoordinatorError(
                f"revision {entry['revision']} does not match its recorded digest (recorded "
                f"{entry['plan_digest']}, found {actual})")
        plan_contract.validate_document(body)

    existing = next((s for s in library.slugs()
                     if library.read_record(s)["plan_id"] == record["plan_id"]), None)
    if existing:
        # A collision is only benign when the content is genuinely identical. Otherwise two different
        # plans share an id, and every later reference to that id becomes ambiguous.
        if build_bundle(library, existing)["bundle_digest"] == bundle["bundle_digest"]:
            print(f"{record['plan_id']} is already here and identical; nothing to do.")
            return 0
        raise PlanCoordinatorError(
            f"a DIFFERENT plan with id {record['plan_id']} is already in this library ({existing}). "
            "Importing would leave two plans sharing an id and make every later reference to it "
            "ambiguous. Clone the incoming plan under a new id, or remove the local one first.")

    slug = record["slug"]
    # A DIFFERENT plan already living at this folder is refused. Constraining `slug` to a safe SHAPE
    # stopped a bundle escaping the library; it does nothing to stop one landing on top of a
    # neighbour, because a slug is not secret — it is printed by `list`, it is the folder name, and
    # anyone who has seen it once can put it in a bundle. `create()` has always had this guard; the
    # first version of this function reimplemented directory creation instead of reusing it and lost
    # the check along the way, which is the whole reason the write path now goes through the store.
    if slug in library.slugs() and library.read_record(slug)["plan_id"] != record["plan_id"]:
        raise PlanCoordinatorError(
            f"a different plan already occupies {library.plan_dir(slug)}: "
            f"{library.read_record(slug)['plan_id']}, not {record['plan_id']}. Importing would "
            "destroy it. Move or remove the local plan first if you meant to replace it.")
    plan_dir = library.plan_dir(slug)
    # Belt and braces over the schema check above: containment is asserted again at the point of
    # every join, so a record written before the pattern existed — or a future edit that relaxes it —
    # still cannot escape. This is the layer that must not be removed for being redundant.
    plan_store.contain(plan_dir, library.root, "the imported plan folder")
    targets = [(plan_store.contain(plan_dir / entry["snapshot"], library.root,
                                   f"revision {entry['revision']}"), entry)
               for entry in record["ledger"] if "redacted" not in entry]
    record_path = plan_store.contain(plan_dir / plan_store.RECORD_FILENAME, library.root,
                                     "the imported plan record")

    plan_store.ensure_dir(plan_dir, within=library.root)
    plan_store.ensure_dir(plan_dir / plan_store.REVISIONS_DIRNAME, within=library.root)
    # Under the plan's own lock, like every other writer. Without it an import racing a `revise` on
    # the same plan has no interlock at all — the compare-and-swap those verbs rely on assumes this
    # lock is the thing serialising them, and an unlocked writer silently opts out of it.
    with plan_store.exclusive_lock_for(library, slug):
        if slug in library.slugs() and library.read_record(slug)["plan_id"] != record["plan_id"]:
            raise PlanCoordinatorError(          # re-asserted inside the lock, not from the read above
                f"another session created a different plan at {plan_dir} while this import was being "
                "verified; nothing was written.")
        for path, entry in targets:
            core.atomic_write(path,
                              json.dumps(revisions[str(entry["revision"])], indent=2, sort_keys=True,
                                         ensure_ascii=False) + "\n",
                              durable=True, mode=plan_store.FILE_MODE)
        core.atomic_write(record_path,
                          json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                          durable=True, mode=plan_store.FILE_MODE)
    problems = library.verify_chain(slug)
    if problems:
        raise PlanCoordinatorError("the imported plan does not verify after writing: "
                                   + "; ".join(problems))
    plan_projection.project_library(library)
    print(f"imported {record['plan_id']} as {slug} "
          f"({len(revisions)} revision(s), every digest verified)")
    return 0


# --- repair and redaction -----------------------------------------------------

def cmd_redact(args) -> int:
    """Excise one revision's body. The operator's remedy when something got written down that must not
    have been — and the reason the store has a redaction path at all."""
    library = _library(args)
    slug = _select(library, args.plan)
    record = library.redact_revision(slug, args.revision, reason=args.reason)
    entry = next(e for e in record["ledger"] if e["revision"] == args.revision)
    plan_projection.project_library(library, force=True)
    print(f"redacted revision {args.revision} of {record['plan_id']}: {entry['redacted']['reason']}")
    print(f"  the ledger entry and its digest ({entry['plan_digest']}) remain, so the chain still "
          "verifies and the excision reads as deliberate.")
    print("\nThis is a logical deletion: the file is gone and the store cannot reach it, but the disk "
          "blocks are not overwritten and any backup or filesystem snapshot taken earlier still holds "
          "it. If what you redacted was a credential, rotate it — redacting is not the remedy.")
    return 0


def cmd_recover(args) -> int:
    """Find the newest intact revision when the head will not read, and say what was passed over.

    Reports; it does not repair. A damaged head is a fact the operator has to decide about — resume
    from the intact ancestor, or restore the damaged file from a backup — and quietly rewriting the
    record would take that decision away and destroy the evidence of what happened.
    """
    library = _library(args)
    slug = _select(library, args.plan)
    problems = library.verify_chain(slug)
    if not problems:
        print(f"{slug} is sound; every revision matches its recorded digest. Nothing to recover.")
        return 0
    print(f"{len(problems)} problem(s) in {slug}:")
    for problem in problems:
        print(f"  - {problem}")
    try:
        revision, skipped = library.recover_head(slug)
    except PlanCoordinatorError:
        # Total loss. This is the case `recover` most exists for, and the guidance below used to be
        # unreachable here — the refusal propagated to the generic handler and the operator got a
        # bare exception instead of the advice the command promises.
        print("\nNo revision of this plan is intact — every one is missing, redacted, or altered.")
        print("Nothing was changed, and nothing here can be recovered from the library itself.")
        print("\nWhat is left, in the order worth trying:")
        print(f"  - restore {library.plan_dir(slug)} from a backup or filesystem snapshot, then "
              f"re-run `validate {args.plan}`;")
        print("  - if you exported a bundle of this plan, `import` it into a clean library;")
        print("  - otherwise the plan is gone and re-authoring it is the only way forward. The "
              "record's own ledger still shows how many revisions there were and when.")
        return 1
    record = library.read_record(slug)
    print(f"\nThe newest intact revision is {revision}; the record's head is "
          f"{record['current']['revision']}.")
    if skipped:
        print(f"Passed over {len(skipped)} damaged revision(s) to get there.")
    print("\nNothing was changed. Two ways forward, and which is right is your call:")
    print(f"  - restore the damaged file from a backup, then re-run `validate {args.plan}`; or")
    print(f"  - carry the plan forward from revision {revision} as a new revision with `revise`.")
    return 1


# --- programs -----------------------------------------------------------------

def _programs(args):
    return plan_program.ProgramLibrary(_library(args))


def cmd_program_new(args) -> int:
    programs = _programs(args)
    slug = programs.create(args.title, args.objective)
    record = programs.read(slug)
    print(f"created program {record['program_id']} at {programs.program_dir(slug)}")
    print("Add its first child with `program add`. Order records a decision — nothing here starts, "
          "selects, or advances a child.")
    return 0


def cmd_program_list(args) -> int:
    programs = _programs(args)
    slugs = programs.slugs()
    if not slugs:
        print(f"no programs in {programs.root}")
        return 0
    for slug in slugs:
        record = programs.read(slug)
        outstanding = programs.outstanding_obligations(record)
        print(f"{record['program_id']}  {programs.derived_status(record):<15} "
              f"{len(record['children'])} child(ren), {len(outstanding)} obligation(s) outstanding  "
              f"{record['title']}")
    return 0


def cmd_program_show(args) -> int:
    programs = _programs(args)
    print(plan_program.render(programs, programs.read(programs.resolve(args.program))))
    return 0


def cmd_program_add(args) -> int:
    programs = _programs(args)
    slug = programs.resolve(args.program)
    record = programs.add_child(slug, args.plan, predecessor=args.after)
    outstanding = programs.outstanding_obligations(record)
    print(f"added {args.plan} as child {len(record['children'])} of {record['program_id']}")
    if outstanding:
        print(f"\n{len(outstanding)} obligation(s) are now carried into the next child:")
        for obligation in outstanding:
            print(f"  - {obligation['id']}: {obligation['statement']}")
        print("\nThe next child must answer for each — satisfied, still carried, or released with a "
              "stated reason. None of them can be dropped by saying nothing.")
    return 0


def cmd_program_close(args) -> int:
    programs = _programs(args)
    record = programs.close(programs.resolve(args.program), args.state, args.reason)
    print(f"{record['program_id']} is now {args.state}: {args.reason}")
    print("Nothing was deleted — every child plan and every revision stays on the shelf.")
    return 0


def cmd_program_reopen(args) -> int:
    programs = _programs(args)
    record = programs.reopen(programs.resolve(args.program))
    print(f"reopened {record['program_id']}")
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

    revise = sub.add_parser("revise", help="mint the next revision — the only verb that writes one")
    revise.add_argument("plan")
    revise.add_argument("--document", required=True)
    revise.add_argument("--expect-revision", type=int,
                        help="the head you believe you are building on; refused if it moved")
    revise.set_defaults(func=cmd_revise)

    clone = sub.add_parser("clone", help="start a new plan from this one — the way past a seal")
    clone.add_argument("plan")
    clone.add_argument("--reason", required=True)
    clone.add_argument("--title")
    clone.set_defaults(func=cmd_clone)

    export = sub.add_parser("export", help="write a self-verifying local bundle (uploads nothing)")
    export.add_argument("plan")
    export.add_argument("--output", required=True)
    export.set_defaults(func=cmd_export)

    importer = sub.add_parser("import", help="read a bundle back, verifying every digest first")
    importer.add_argument("--bundle", required=True)
    importer.set_defaults(func=cmd_import)

    redact = sub.add_parser("redact", help="excise one revision's body, keeping the chain honest")
    redact.add_argument("plan")
    redact.add_argument("--revision", type=int, required=True)
    redact.add_argument("--reason", required=True,
                        help="why — an unexplained hole is indistinguishable from damage")
    redact.set_defaults(func=cmd_redact)

    recover = sub.add_parser("recover", help="find the newest intact revision when the head will not read")
    recover.add_argument("plan")
    recover.set_defaults(func=cmd_recover)

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

    program_show = program.add_parser("show", help="one program, its children and outstanding obligations")
    program_show.add_argument("program")
    program_show.set_defaults(func=cmd_program_show)

    program_add = program.add_parser(
        "add", help="append a plan, enforcing the carry-forward guarantee against its predecessor")
    program_add.add_argument("program")
    program_add.add_argument("plan")
    program_add.add_argument("--after", help="the plan this one succeeds; required after the first child")
    program_add.set_defaults(func=cmd_program_add)

    for state, helptext in (("retire", "superseded, kept for the record"),
                            ("abandon", "deliberately dropped")):
        closer = program.add_parser(state, help=f"close a program: {helptext}")
        closer.add_argument("program")
        closer.add_argument("--reason", required=True)
        closer.set_defaults(func=cmd_program_close,
                            state={"retire": "retired", "abandon": "abandoned"}[state])

    program_reopen = program.add_parser("reopen", help="undo a program retirement or abandonment")
    program_reopen.add_argument("program")
    program_reopen.set_defaults(func=cmd_program_reopen)
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
