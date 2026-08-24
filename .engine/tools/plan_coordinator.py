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

# Review depths, offered only after a full render. ONE vocabulary across both coordinators: the depth
# chosen here is the depth the Build's deliverable review runs at, so the operator consents once, at
# plan approval, and that consent covers both gates.
DEPTHS = {
    "quick": "No cold reviewers on either side — your own read of the plan plus the automatic checks.",
    "standard": "Architecture, feasibility, product intent, risk and governance — the four-lens panel "
                "before the seal, and three deliverable lenses after the build.",
    "thorough": "The same four-lens panel at higher effort, and five deliverable lenses after the "
                "build. For a plan that touches secrets, data durability, or a guardrail.",
}
DEPTH_ORDER = ("quick", "standard", "thorough")

# Which lenses each depth requires, homed HERE because the panel is here. It left `.engine/build-protocol.json`
# with the panel itself: a Build protocol declaring a review the Build does not run is a table nobody reads
# and everybody can misread. The two coordinators must still speak one depth vocabulary, and that is pinned
# by test rather than by sharing a file — `quick` is the floor on both sides, and `thorough` widens to the
# whole installed roster on both sides.
PLAN_REVIEW_LENSES = {
    "quick": [],
    "standard": ["product-intent", "architecture", "feasibility", "risk-governance"],
    "thorough": ["product-intent", "architecture", "feasibility", "risk-governance"],
}


_now = moment.utc_now


def installed_lenses(root: Path | None = None) -> list[dict]:
    """The plan-review reviewers actually installed here, by lens.

    Moved from the Build side with the panel. What matters is that the roster is DISCOVERED — a depth
    cannot require a lens nobody installed, and the coverage gate below cannot demand one either.
    """
    base = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    found: dict[str, dict] = {}
    for path in sorted((base / ".claude" / "agents").glob("*.md")):
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            continue
        front = text.split("---\n", 2)[1]
        fields = {}
        for line in front.splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                fields[key.strip()] = value.strip()
        if fields.get("role") == "plan-review" and fields.get("lens"):
            lens = fields["lens"]
            if lens in found:
                raise PlanCoordinatorError(f"more than one installed reviewer declares lens {lens}")
            found[lens] = {"lens": lens, "path": str(path.relative_to(base)),
                           "digest": core.digest(path.read_bytes()),
                           "effort": fields.get("effort")}
    return [found[lens] for lens in sorted(found)]


def required_lenses(depth: str, roster: list[dict], protocol: dict | None = None) -> list[str]:
    """The lenses a review at this depth must cover. Intersected with what is installed, never invented.

    This is the rule BC-12 protected on the Build side — approved reviewer coverage cannot be silently
    omitted — and it comes across with the panel as a SEAL gate rather than a submission gate.
    """
    table = protocol or PLAN_REVIEW_LENSES
    if depth not in table:
        raise PlanCoordinatorError(f"unknown review depth {depth!r}")
    allowed = {item["lens"] for item in roster} if depth == "thorough" else set(table[depth])
    return [item["lens"] for item in roster if item["lens"] in allowed]


_EFFORT_RANK = {None: -1, "low": 0, "medium": 1, "high": 2}
BINDINGS_PATH = Path(__file__).resolve().parents[2] / ".engine" / "policies" / "model-bindings.json"


def resolved_efforts(root: Path | None = None) -> dict:
    """Each depth's RESOLVED reviewer effort — the deployment's override over the shipped default.

    Not a second table. This is the same single depth dial the Build side reads, so an operator asked
    to choose a depth here is told the effort they will actually get.
    """
    import agent_bindings
    base = str(Path(root) if root is not None else Path(__file__).resolve().parents[2])
    bindings = core.json_file(Path(base) / ".engine" / "policies" / "model-bindings.json")
    return {depth: agent_bindings.depth_effort(depth, bindings, root=base) for depth in DEPTH_ORDER}


def available_depths(roster: list[dict], protocol: dict | None = None,
                     efforts: dict | None = None) -> list[str]:
    """Which depths to OFFER, so the operator is never asked to choose one that buys nothing.

    The 763/677 protection, moved with the consent surface. A depth is offered when, against the last
    offered lighter one, it covers at least one lens the lighter one does not, OR the same non-empty
    lens set at higher reviewer effort. Empty-versus-empty never distinguishes, so with no reviewers
    installed only `quick` is offered. `quick` is always offered: it is the floor.
    """
    protocol = protocol or PLAN_REVIEW_LENSES
    efforts = resolved_efforts() if efforts is None else efforts
    offered: list[str] = []
    last: tuple[frozenset, str | None] | None = None
    for depth in DEPTH_ORDER:
        lenses = frozenset(required_lenses(depth, roster, protocol))
        effort = efforts.get(depth)
        if last is None:
            offered.append(depth)
            last = (lenses, effort)
            continue
        last_lenses, last_effort = last
        if (lenses - last_lenses) or (lenses == last_lenses and lenses
                                      and _EFFORT_RANK.get(effort, -1) > _EFFORT_RANK.get(last_effort, -1)):
            offered.append(depth)
            last = (lenses, effort)
    return offered


def coverage_gap(depth: str, recorded_lenses: list[str], roster: list[dict] | None = None,
                 protocol: dict | None = None) -> list[str]:
    """Lenses the approved depth requires that the recorded review did not run.

    A non-empty answer is what makes "a sealed plan is a reviewed one" TRUE rather than assumed. The
    one-lens-seals-at-thorough hole is exactly what this closes.
    """
    roster = installed_lenses() if roster is None else roster
    return [lens for lens in required_lenses(depth, roster, protocol) if lens not in set(recorded_lenses)]


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
        return (f"this plan is sealed and read-only, and a sealed plan is now the only thing a Build "
                f"runs on. Open a draft pull request for the work, then hand this plan to it:\n"
                f"    build_coordinator.py --state <snapshot> plan bind --plan {record['plan_id']} "
                f"--repository <owner/repo> --pr <number>\n"
                f"  To keep working on the idea instead, `clone` it into a new plan — a seal is terminal.")
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
    roster = installed_lenses()
    efforts = resolved_efforts()
    offered = available_depths(roster, efforts=efforts)
    print(f"review depths for {record['plan_id']} at revision {record['current']['revision']}")
    print("(only those that add coverage or effort over a lighter one):\n")
    for name in offered:
        lenses = required_lenses(name, roster)
        print(f"  {name:<10} {DEPTHS[name]}")
        effort = efforts.get(name)
        if lenses:
            print(f"             lenses: {', '.join(lenses)}"
                  + (f" — reviewer effort {effort}" if effort else ""))
        else:
            print("             no cold plan reviewers; your own read is the review")
    suppressed = [d for d in DEPTH_ORDER if d not in offered]
    if suppressed:
        print("\nnot offered, because it would run what a lighter depth already runs: "
              + ", ".join(suppressed))
    if not roster:
        print("no plan-review reviewers are installed here, so only your own read is available")
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
    roster = installed_lenses()
    if args.depth not in available_depths(roster):
        raise PlanCoordinatorError(
            f"{args.depth} is not offered here: with this repository's installed reviewers it would run "
            "exactly what a lighter depth runs, so choosing it would spend consent on nothing. Run "
            f"`depths {args.plan}` to see what is actually on offer.")
    revision = record["current"]["revision"]

    def approve(current):
        if current.get("seal"):          # re-asserted inside the lock, not from the copy above
            raise PlanCoordinatorError("this plan was sealed while you were reading it; a seal is terminal")
        current["approval"] = {"revision": revision, "plan_digest": digest,
                               "depth": args.depth, "at": _now()}

    library.update_record(slug, approve, expected_revision=revision)
    covering = required_lenses(args.depth, roster)
    print(f"approved revision {revision} of {record['plan_id']} at {args.depth} depth")
    if covering:
        print(f"  the seal will require these lenses: {', '.join(covering)}")
    else:
        print("  no cold plan reviewers at this depth — your own read is the review")
    print("  the Build's deliverable review runs at this same depth; consent is given once, here")
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
    covering = required_lenses(approval["depth"], installed_lenses())
    header = (f"Plan review packet — {record['plan_id']} revision {record['current']['revision']}\n"
              f"Plan digest: {record['current']['plan_digest']}\n"
              f"Packet digest: {packet_digest}\n"
              f"Required lenses: {', '.join(covering) or 'none at this depth'}\n"
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
    # Record-time verification of the packet digest, moved from the Build side with the panel. A receipt
    # that names a digest nobody can reproduce vouches for nothing; this re-renders the packet for the
    # APPROVED revision and refuses a receipt that does not match it, so the digest in the record is a
    # fact rather than a claim.
    rendered = plan_projection.render_plan(library.head(slug), record)
    expected = core.digest(rendered.encode("utf-8"))
    if args.packet_digest != expected:
        raise PlanCoordinatorError(
            f"this receipt names packet digest {args.packet_digest}, but the packet for the approved "
            f"revision {approval['revision']} renders to {expected}. Either the receipt came from a "
            "different packet than the one approved, or the packet was edited after it was cut — "
            "re-cut it with `review packet` and re-run the lenses against what it actually says.")
    # The coverage the approved depth demands is checked here too, not only at the seal, so the gap is
    # surfaced while the reviewers are still warm rather than at the terminal act.
    gap = coverage_gap(approval["depth"], list(args.lens))
    if gap:
        print(f"note: the approved {approval['depth']} depth also requires {', '.join(gap)}; the seal "
              "will refuse until those lenses are covered.", file=sys.stderr)
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
    blocks = bool(args.blocks_this_pr)
    # The disclosure rule the Build side already enforced, arriving with the panel: a BLOCKING finding
    # that the orchestrator decides should not block needs an operator-safe sentence, because that
    # decision is a disagreement the operator meets at merge. Without one there is nothing honest to
    # publish, and "no summary recorded" on the merge surface is how a real objection disappears.
    if match[0]["severity"] == "blocking" and not blocks and not args.operator_summary:
        raise PlanCoordinatorError(
            f"{args.id} is a BLOCKING finding you are not leaving blocking. That is a disagreement the "
            "operator has to be able to read at merge, so it needs a safe, operator-facing sentence: "
            "pass --operator-summary.")

    def change(current):
        for finding in current["plan_review"]["findings"]:
            if finding["id"] == args.id:
                finding["disposition"] = args.disposition
                finding["rationale"] = args.rationale
                finding["blocks_this_pr"] = blocks
                if args.operator_summary:
                    finding["operator_summary"] = args.operator_summary
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
    depth = (approval or {}).get("depth")
    # The coverage rule moves here WITH the panel. On the Build side this was BC-12's "approved reviewer
    # coverage cannot be silently omitted", enforced at submission; here it is a seal gate, which is what
    # makes "a sealed plan is by definition a reviewed one" true rather than assumed. At `quick` the
    # roster is empty by the operator's own choice at approval, and their read IS the review — so the
    # demand for a recorded review is keyed on the roster, not asserted regardless of the depth chosen.
    required = required_lenses(depth, installed_lenses()) if depth in DEPTHS else []
    if not review:
        if required:
            refusals.append("no cold plan review has been recorded, and the approved depth requires "
                            + ", ".join(required))
    elif approval and review["revision"] != approval["revision"]:
        refusals.append(f"the review covers revision {review['revision']} but the approval covers "
                        f"revision {approval['revision']}")
    if review and required:
        gap = coverage_gap(depth, review.get("lenses", []))
        if gap:
            refusals.append(
                f"the review covers {', '.join(review.get('lenses', [])) or 'no lenses'}, but the "
                f"approved {depth} depth requires {', '.join(required)}: missing {', '.join(gap)}. "
                "Run the missing lenses and record them, or re-approve at a depth that matches what "
                "you actually intend to run.")
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
    # At a depth that requires no cold lenses there is no review record, and the reviewed digest IS the
    # approved one — the operator's own read at approval is what the seal records having covered.
    review = record.get("plan_review")
    reviewed_digest = review["plan_digest"] if review else record["approval"]["plan_digest"]
    sealed_digest = record["current"]["plan_digest"]
    changed = reviewed_digest != sealed_digest
    if changed and not args.delta_judgment:
        raise PlanCoordinatorError(
            f"the plan changed after it was read (read at revision {(review or record['approval'])['revision']}, "
            f"sealing revision {record['current']['revision']}). That is the expected shape — fixes fold "
            "in as revisions — but the delta needs one proportional judgment before it locks. Read it "
            f"with `diff {args.plan} --from {(review or record['approval'])['revision']} --to "
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


# --- importing an accepted native plan --------------------------------------
#
# Both runtimes end up here. On Claude the operator accepts a plan and the plan-exit PostToolUse hook
# calls this with the accepted document; on Codex the operator prefixes a prompt with the acceptance
# envelope and the UserPromptSubmit hook calls this with the rest of the prompt. One import, one
# result, one arrival report — the adapters differ only in where the text came from.
#
# GROUNDWORK, NOT BYPASS AND NOT RESTART. What lands is an unapproved draft revision 1: no approval,
# no review, no seal, and no Build authority of any kind. The native text is kept verbatim as the raw
# intent, because the gap between what was said and what anyone made of it is the thing a reviewer
# needs; nothing here interprets it, decomposes it, or writes deliberation prose on its behalf. The
# payload is `build-plan.imported`, whose schema forbids a non-empty decomposition, and the four
# things an import cannot know are written down as unresolved decisions, which the seal refuses while
# any remain. So an import moves a plan onto the shelf and moves nobody any closer to building it —
# which is the honest position, and the reason this is safe to run from a hook.

NATIVE_PLAN_ENVELOPE = "PLEASE IMPLEMENT THIS PLAN:"

_IMPORTED_INTERPRETATION = (
    "Not interpreted. This plan arrived as an accepted native plan and was imported verbatim; no one "
    "has yet restated what it is asking for, and this sentence is a placeholder for that work, not a "
    "summary of it.")

_IMPORTED_GAPS = (
    "What is this plan actually asking for? The imported text is the raw intent; nothing has restated "
    "it as an interpretation the operator can confirm or correct.",
    "What problem does it solve, and for whom? The import wrote no problem frame.",
    "What is the strongest honest case AGAINST doing this? The import wrote none, and a placeholder "
    "would be worse than the gap.",
    "How does it decompose into work? The imported payload is empty by construction; a real "
    "build-plan.v2 payload has to be authored before this plan can be sealed.",
)


def _imported_title(text: str) -> str:
    """A title lifted from the imported text, never invented. The first markdown heading if there is
    one, else the first non-blank line; a document with neither gets a plainly generic name rather
    than a guess dressed up as a title."""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            line = line.lstrip("#").strip()
        if not line:
            continue
        return line[:120].rstrip() if len(line) > 120 else line
    return "Imported plan"


def import_native_plan(text: str, *, provenance: str,
                       library: plan_store.PlanLibrary | None = None) -> dict:
    """Import an accepted native plan as an unapproved draft. Returns the arrival facts.

    Raises PlanCoordinatorError when there is nothing to import. Callers running inside a hook treat
    that as no-import-and-carry-on: an acceptance that imports nothing must never cost the operator
    their turn, and a session that cannot reach its plan library is still a session that can talk.
    """
    text = text if isinstance(text, str) else ""
    if not text.strip():
        raise PlanCoordinatorError("there is no plan text to import")
    library = library or plan_store.PlanLibrary()
    now = _now()
    document = {
        "schema_version": "engine-plan.v1",
        "plan_id": plan_store.mint_plan_id(),
        "title": _imported_title(text),
        "revision": 1,
        "created_at": now,
        "revised_at": now,
        "revision_note": "Imported verbatim from an accepted native plan. Nothing interpreted, "
                         "nothing decomposed, nothing deliberated.",
        "intent": {"raw": text, "interpretation": _IMPORTED_INTERPRETATION, "source": {"kind": "direct"}},
        "deliberation": {
            "problem_frame": "",
            "case_against": "",
            "alternatives": [],
            "failure_modes": [],
            "unresolved_decisions": list(_IMPORTED_GAPS),
        },
        "intake": {"provenance": provenance},
        "build_plan": {"schema_version": plan_contract.IMPORTED_BUILD_PLAN_VERSION, "work_items": []},
    }
    slug = library.create(document, intake={"provenance": provenance})
    plan_projection.project_library(library)
    return {
        "plan_id": document["plan_id"],
        "revision": 1,
        "slug": slug,
        "title": document["title"],
        "folder": str(library.plan_dir(slug)),
        "next_command": f"python tools/plan_coordinator.py preview --plan {document['plan_id']}",
    }


def arrival_report(arrival: dict) -> str:
    """What the session says after an import — the whole point of grading arrival, not just departure.

    An operator who accepts a plan and is then handed a write refusal has been told the engine is
    broken. So this names what happened, what did NOT happen, and the one command that moves it
    forward, and it is explicitly for relaying: unlike the stance directive it replaces, its whole
    job is to reach the operator.
    """
    return (
        f"The plan you just accepted was imported into the Plan Coordinator as {arrival['plan_id']}, "
        f"revision {arrival['revision']} — an unapproved draft titled \"{arrival['title']}\". "
        "Your stance did not change and nothing was built: an imported draft carries no approval, no "
        "review, no seal and no Build authority. Tell the operator this in your own words now, "
        "including the next command, so they are not left guessing why nothing happened. "
        f"The next command is: {arrival['next_command']} — reading the plan whole is what unlocks the "
        "depth choice (`depths`, then `approve --depth ...`), which is where the operator sees the "
        "risk assessment and says how careful the reviews should be. The imported payload is empty "
        "by construction, so a real build-plan.v2 payload still has to be authored before this plan "
        "can be sealed and handed to a Build.")


def cmd_import_native(args) -> int:
    """The typed recovery path. The hooks are the ordinary door; this exists for the two ways a hook
    can be unavailable — an acceptance envelope the handler no longer recognizes, and a Codex
    registration the operator has not re-trusted — so a drifted platform costs an extra command
    rather than the whole capability."""
    library = _library(args)
    arrival = import_native_plan(core.input_text(args.input),
                                 provenance=args.provenance, library=library)
    print(f"imported {arrival['plan_id']} revision {arrival['revision']} at {arrival['folder']}")
    print(f"  title       {arrival['title']}")
    print("  status      draft — unapproved, unreviewed, unsealed, and carrying an empty payload")
    print(f"  next        {arrival['next_command']}")
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

    existing, unreadable = None, []
    for candidate in library.slugs():
        try:
            if library.read_record(candidate)["plan_id"] == record["plan_id"]:
                existing = candidate
                break
        except PlanCoordinatorError:
            # A bit-rotted neighbour must not block an import that has nothing to do with it. But the
            # collision check is now incomplete, so say so instead of quietly proceeding as though it
            # had passed.
            unreadable.append(candidate)
    if unreadable:
        print(f"warning: could not read {len(unreadable)} plan record(s) while checking for an id "
              f"collision ({', '.join(unreadable)}), so that check is incomplete. Run `doctor` to see "
              "what is wrong with them.", file=sys.stderr)
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
    blocking = dispose.add_mutually_exclusive_group()
    blocking.add_argument("--blocks-this-pr", action="store_true",
                          help="this finding still blocks the pull request the plan authorizes")
    blocking.add_argument("--does-not-block-this-pr", action="store_false", dest="blocks_this_pr",
                          help="the default: dispositioned and not blocking")
    dispose.add_argument("--operator-summary",
                         help="The operator-safe sentence published on the merge surface. Required when a "
                              "BLOCKING finding is not left blocking.")
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

    import_native = sub.add_parser(
        "import-native",
        help="import an accepted native plan as an unapproved draft — the recovery path when a hook cannot")
    import_native.add_argument("--input", required=True, help="the native plan text, or - for stdin")
    import_native.add_argument("--provenance", required=True,
                               help="where this text came from, in plain words — an import with no "
                                    "provenance is a plan nobody can trace")
    import_native.set_defaults(func=cmd_import_native)

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
