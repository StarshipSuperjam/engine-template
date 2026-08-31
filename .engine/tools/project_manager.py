#!/usr/bin/env python3
"""The Project Manager: the Build Coordinator's upstream peer.

The Build Coordinator owns execution and becomes authoritative at `plan bind`. Everything upstream of
that — grounding, deliberation, authoring the graph, presenting it, deciding it is good enough — was
convention a session was trusted to remember. This tool owns that half.

WHY THIS NAME. It shipped as the Plan Coordinator, and that named one duty out of several. The
component also manages task completion, owns the continuous-improvement workflows, and organizes work
across build phases through the program object — so the operator retitled it on 2026-08-24 to the job
it actually does. The retitle is of the COMPONENT, deliberately not of the data: the schema ids
(`engine-plan.v1`, `plan-record.v1`, `engine-program.v1`) and the `plan` verb namespace name the
artifacts rather than the component, and renaming a schema id would invalidate every record already
stored in every deployed project. Stored records and history — merged pull-request titles and an
artifact's own filename — keep the name they were written under, because that is what they were
written under. Merge history carries the reasoning.

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
import os
from pathlib import Path
import sys

import build_coordinator_core as core
import moment
import plan_contract
import plan_lifecycle
import plan_program
import plan_projection
import plan_store

ProjectManagerError = plan_store.PlanStoreError

# Review depths, offered only after a full render. ONE vocabulary across both coordinators: the depth
# chosen here is the depth the Build's deliverable review runs at, so the operator consents once, at
# plan approval, and that consent covers both gates.
#
# DEPTH SELECTS REVIEWERS AND NOTHING ELSE. It never selects the plan's FORMAT and never selects its
# GRAPH TOPOLOGY: the document is engine-plan.v1 and the payload build-plan.v2 at every depth, the
# nodes and their dependencies are whatever was authored, and both digests are byte-identical across
# depths. Stated here and pinned by test rather than left as an obvious-sounding property, because the
# tempting shortcut is real and would be quiet — letting `quick` accept a thinner document, or fold a
# graph into a chain "since nobody is reviewing it anyway", would make how carefully a plan is read
# decide what the plan IS. Depth is how much scrutiny the operator asked for; it is never a discount
# on the artifact.
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
                raise ProjectManagerError(f"more than one installed reviewer declares lens {lens}")
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
        raise ProjectManagerError(f"unknown review depth {depth!r}")
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


_EFFORT_RANK = {"low": 0, "medium": 1, "high": 2}


def _validate_findings(findings: list) -> None:
    """Each translated finding against the record's own finding definition, at read time."""
    schema = Path(__file__).resolve().parents[2] / ".engine" / "schemas" / "plan-record.v1.json"
    for finding in findings:
        core.validate_part(finding, schema, "#/$defs/finding", "plan-review finding")


def parse_delivered_efforts(values: list[str] | None, lenses: list[str]) -> dict:
    """`--delivered-effort` into a {lens: effort} map. A bare `high` applies to every lens in this
    record — the honest shape on the Claude arm, where one session spawns the whole panel at one effort
    — and `<lens>=<effort>` names one. Both forms exist because both facts are real: the fan-out has a
    single ceiling, and a reviewer may report something different from it."""
    out: dict = {}
    for raw in values or []:
        if "=" in raw:
            lens, _, effort = raw.partition("=")
            lens, effort = lens.strip(), effort.strip()
            if lens not in lenses:
                raise ProjectManagerError(
                    f"--delivered-effort names {lens}, which is not a lens in this record "
                    f"({', '.join(lenses)})")
            out[lens] = effort
        else:
            for lens in lenses:
                out.setdefault(lens, raw.strip())
    bad = sorted({effort for effort in out.values() if effort not in _EFFORT_RANK})
    if bad:
        raise ProjectManagerError("not a reasoning-effort level: " + ", ".join(bad)
                                   + " (expected low, medium or high)")
    return out


def effort_shortfalls(depth: str, delivered: dict, root: Path | None = None) -> list[str]:
    """One line per lens that reports having run below what the approved depth promises."""
    promised = resolved_efforts(root).get(depth)
    if not promised:
        return []
    return [f"{lens} reports running at {effort}, and the approved {depth} depth promises {promised}"
            for lens, effort in sorted(delivered.items())
            if _EFFORT_RANK.get(effort, -1) < _EFFORT_RANK.get(promised, -1)]


def require_delivered_effort(depth: str, lenses: list[str], delivered: dict,
                             root: Path | None = None, *, accepted: bool = False) -> None:
    """Refuse a review record whose panel came in under the approved depth's effort.

    A shortfall REFUSES but is not unrecordable: `--accept-effort-shortfall` records the honest number
    and publishes the gap. Without that escape the only way past an honest medium panel under a thorough
    approval was to type `high`, which is exactly the false record this gate exists to prevent.

    HERE, and not at the seal, because here is where the exits are still open
    (StarshipSuperjam/engine-template#1067). The approval freezes at the first review recorded, so a
    seal-time refusal would leave a plan with no way out: the panel already ran, and the depth can no
    longer be re-chosen. Refusing before the record lands keeps both honest answers available — re-run
    the lenses at the promised effort, or go back to the operator and approve the depth this panel can
    actually deliver. On the Claude arm a reviewer persona carries no effort of its own, so the depth
    reaches the lens only through the session that spawned it; the value is self-reported and nothing
    here verifies it."""
    promised = resolved_efforts(root).get(depth)
    if not promised or not lenses:
        return
    silent = [lens for lens in lenses if lens not in delivered]
    if silent:
        raise ProjectManagerError(
            f"the approved {depth} depth runs its reviewers at {promised} effort, so this record has to "
            f"say what they actually ran at. Missing for: {', '.join(silent)}. Add "
            f"`--delivered-effort {promised}` (it applies to every lens in the record) or "
            "`--delivered-effort <lens>=<effort>` per lens. It is self-reported — the point is that the "
            "claim is on the record, not that anything here can check it.")
    shortfalls = effort_shortfalls(depth, delivered, root)
    if shortfalls and not accepted:
        raise ProjectManagerError(
            "this panel came in under the depth the operator approved: " + "; ".join(shortfalls)
            + ". Three ways on, and the third is why this is a refusal rather than a wall: re-run those "
            f"lenses at {promised} and record that; take it back to the operator and re-approve at the "
            "depth this panel can actually deliver (the approval is still unfrozen — it freezes the "
            "moment a review is recorded); or record it as it stands with "
            "`--accept-effort-shortfall`, which keeps the honest number and publishes the gap in the "
            "pull request.\n\n"
            "That third exit exists deliberately. This value is self-reported and nothing here can check "
            "it, so a gate whose only unblocking path was to overstate it would manufacture the very "
            "false record it exists to prevent — the Build side has had this escape since it was built, "
            "and the asymmetry was the defect.")


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
    except ProjectManagerError:
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
    if record.get("consent"):
        print("\noperator decisions recorded:")
        for line in plan_lifecycle.consent_trail(record):
            print(f"  {line[2:]}")
    if document and not record.get("seal"):
        # ONE refusal set, derived here exactly as `seal` derives it. They used to be computed from
        # different inputs — `show` from the document's own blockers, `seal` from those PLUS the
        # gates — so a plan could read as ready here and refuse there, which is how an operator ends
        # up believing a gate is a bug. Whatever stops the seal is what is printed.
        refusals = seal_refusals(library, slug)
        if refusals:
            print("\nnot sealable yet:")
            for refusal in refusals:
                print(f"  - {refusal}")
            print(f"\nnext: {_next_step(status, record, blockers)}")
        else:
            print(f"\nready to seal: {_next_step(status, record, blockers)}")
        # Beside the refusals, never inside them: what the operator should know but which is not a
        # reason to stop. `show` prints exactly what `seal` refuses AND exactly what it discloses.
        _print_disclosures(seal_disclosures(library, slug), stream=sys.stdout)
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
    """The one thing to do next, as a COMMAND with placeholders — never as a verb to go look up.

    Every line here names the exact invocation, because a cold session reading "run the one cold plan
    review" had to reconstruct three arguments from the help text, and the reconstruction is where
    the ceremony went wrong: a packet digest guessed instead of read, a lens name spelled from
    memory. A placeholder in angle brackets is a thing to fill in; a verb name is a thing to search
    for, and this file knows the answer either way.
    """
    plan = record["plan_id"]
    if status in ("complete", "retired", "abandoned"):
        return f"nothing — this plan is {status} ({record['closure']['reason']})."
    if status == "active":
        return "nothing here — the Build this plan authorized is running."
    if status == "sealed":
        return (f"this plan is sealed and read-only, and a sealed plan is now the only thing a Build "
                f"runs on. Open a draft pull request for the work, then hand this plan to it:\n"
                f"    build_coordinator.py plan bind --plan {plan} "
                f"--repository <owner/repo> --pr <number> --operator-decision \"<what they said>\"\n"
                f"  To keep working on the idea instead:\n"
                f"    project_manager.py clone {plan} --reason \"<why a new plan>\"")
    if status == "review-recorded":
        outstanding = [f for f in (record["plan_review"] or {}).get("findings", [])
                       if not f.get("disposition")]
        if outstanding:
            first = outstanding[0]["id"]
            return (f"disposition {len(outstanding)} outstanding finding(s) — "
                    + ", ".join(f["id"] for f in outstanding) + f":\n"
                    f"    project_manager.py finding dispose {plan} --id {first} "
                    "--disposition <accepted-fixed|accepted-tracked|partially-accepted|rejected|escalated> "
                    "--rationale \"<why>\" <--blocks-this-pr|--does-not-block-this-pr>")
        if blockers:
            return (f"revise to clear what still blocks the seal, then seal:\n"
                    f"    project_manager.py revise {plan} --document <revision.json> "
                    f"--expect-revision {record['current']['revision']}")
        if not plan_lifecycle.consent_for(record, "findings-presented"):
            return (f"show the operator what the panel found and what was done about each, then record "
                    f"that you did:\n    project_manager.py present-findings {plan} "
                    "--operator-decision \"<what they said>\"")
        return (f"seal the plan — it is reviewed and nothing outstanding blocks it:\n"
                f"    project_manager.py seal {plan} --operator-decision \"<what they said>\"")
    if status == "awaiting-review":
        return (f"run the one cold plan review against the approved revision:\n"
                f"    project_manager.py review packet {plan} --output <packet.md>\n"
                f"    project_manager.py review record {plan} --packet-digest <digest from the packet> "
                f"--lens <lens> --findings <findings.json> --delivered-effort <low|medium|high>")
    if status == "awaiting-approval":
        return (f"present the full revision — and stop there. Invite the operator's questions, take their "
                f"revisions, and say nothing about approval yet:\n"
                f"    project_manager.py preview {plan}\n"
                f"  NOT in that same message, and not until the operator has read the plan and says they "
                f"are satisfied with it: the depth choice and the approval are their own separate turn, "
                f"and these two are listed here only so you know where they are —\n"
                f"    project_manager.py depths {plan}\n"
                f"    project_manager.py approve {plan} --depth <quick|standard|thorough> "
                "--operator-decision \"<what the operator said>\"")
    lead = ("revise to clear: " + "; ".join(blockers)) if blockers else "revise, then preview and approve"
    return (f"{lead}:\n    project_manager.py revise {plan} --document <revision.json> "
            f"--expect-revision {record['current']['revision']}")


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
        raise ProjectManagerError(
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
        raise ProjectManagerError("this plan is sealed; a seal is terminal and nothing about it changes")
    digest = record["current"]["plan_digest"]
    if not was_previewed(library, slug, digest):
        raise ProjectManagerError(
            f"the full plan has not been presented at this revision. Run `preview {args.plan}` first — "
            "approving a plan nobody has read is what this refusal exists to prevent.")
    if args.depth not in DEPTHS:
        raise ProjectManagerError(f"unknown review depth {args.depth!r}; choose one of "
                                   + ", ".join(DEPTHS))
    # Re-approving at a DIFFERENT depth once a review exists would leave that review in place while the
    # seal's coverage check moved to the new depth's roster. Downgrade far enough and the roster empties,
    # so a review that covered one lens of four sails through — and the pull request, which reads this
    # review live, then tells the operator a cold panel read the plan. The review cannot be dropped to
    # make room either: exactly one review per plan is what stops the re-review spiral. So the depth is
    # what holds still.
    reviewed = record.get("plan_review")
    frozen = plan_lifecycle.frozen_reason(record, "approval")
    if reviewed and args.depth != record["approval"]["depth"]:
        raise ProjectManagerError(
            f"this plan was approved at {record['approval']['depth']} depth and has already been "
            f"reviewed at it, so it cannot be re-approved at {args.depth}: {frozen}")
    # An approval that would ORPHAN a recorded review. Re-approving the same depth at a NEW revision
    # leaves the review pointing at a revision nothing approves any more — the wedge that took store
    # surgery to escape, because `review record` then refused ("one review per plan") while the seal
    # refused too ("the review covers revision N but the approval covers M"). It is refused at the
    # door now, and the refusal names the two ways out rather than leaving the operator between them.
    if reviewed and reviewed["revision"] != revision_of(record):
        raise ProjectManagerError(
            f"a cold review is recorded against revision {reviewed['revision']}, and approving revision "
            f"{revision_of(record)} would orphan it: the review would point at a revision nothing "
            "approves, which no verb can then resolve. The plan changed after it was reviewed, and "
            "that is the expected shape — do not re-approve. Seal it and let the delta judgment cover "
            f"the change:\n    project_manager.py seal {args.plan} --delta-judgment scoped "
            "--delta-rationale \"<what changed and why it is still the reviewed plan>\" "
            "--operator-decision \"<what the operator said>\"\n"
            f"  If the change is too large for that, clone: `clone {args.plan} --reason \"<why>\"`.")
    roster = installed_lenses()
    if args.depth not in available_depths(roster):
        raise ProjectManagerError(
            f"{args.depth} is not offered here: with this repository's installed reviewers it would run "
            "exactly what a lighter depth runs, so choosing it would spend consent on nothing. Run "
            f"`depths {args.plan}` to see what is actually on offer.")
    revision = record["current"]["revision"]
    consent = _require_consent(record, "approve", args)

    def approve(current):
        if current.get("seal"):          # re-asserted inside the lock, not from the copy above
            raise ProjectManagerError("this plan was sealed while you were reading it; a seal is terminal")
        current["approval"] = {"revision": revision, "plan_digest": digest,
                               "depth": args.depth, "at": _now()}
        current.setdefault("consent", []).append(consent)

    library.update_record(slug, approve, expected_revision=revision)
    covering = required_lenses(args.depth, roster)
    print(f"approved revision {revision} of {record['plan_id']} at {args.depth} depth")
    print(f"  on the operator's decision: “{consent['decision']}”")
    if covering:
        print(f"  the seal will require these lenses: {', '.join(covering)}")
    else:
        print("  no cold plan reviewers at this depth — your own read is the review")
    print("  the Build's deliverable review runs at this same depth; consent is given once, here")
    print(f"  bound to {digest}")
    print(f"\nnext: cut the packet and run the one cold plan review against this revision:\n"
          f"    project_manager.py review packet {args.plan} --output <packet.md>\n"
          f"    project_manager.py review record {args.plan} --packet-digest <digest> "
          f"--lens <lens> --findings <findings.json> --delivered-effort <low|medium|high>")
    return 0


# --- consent gates ------------------------------------------------------------

def revision_of(record: dict) -> int:
    return record["current"]["revision"]


def _require_consent(record: dict, gate: str, args) -> dict:
    """The operator's decision at one gate, or a refusal that says what they are being asked.

    Taken from the command line rather than read back from the record, because the whole point is
    that the words are the OPERATOR's from this moment — a gate satisfied by an attestation recorded
    earlier for something else would be consent laundering, and `consent` is append-only so a stale
    entry can never be edited into a fresh one.
    """
    decision = getattr(args, "operator_decision", None)
    if not decision or not decision.strip():
        raise ProjectManagerError(plan_lifecycle.missing_consent({}, gate))
    return plan_lifecycle.attestation(gate, decision, at=_now())


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
        raise ProjectManagerError("approve the plan and choose a review depth before building a packet")
    if plan_store.approval_is_stale(record):
        raise ProjectManagerError(
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
    # The seal freezes the review surface, not merely the plan text. At a depth that requires no cold
    # lenses a plan seals with no review at all, so "a review already exists" does not stand in for this
    # check: without it, a review — findings, dispositions and all — could be written onto a plan that
    # was already sealed, and the Build would read it live at compose time as though it had been there.
    if record.get("seal"):
        raise ProjectManagerError(
            "this plan is sealed, and its review is what the pull request publishes — a review recorded "
            "now would appear at merge as though it had been read before the plan was locked. Reviews "
            "belong before the seal. If this plan needs one, clone it and review the clone.")
    if record.get("plan_review"):
        existing = record["plan_review"]
        raise ProjectManagerError(
            f"a plan review is already recorded for revision {existing['revision']} of this plan, and "
            "there is exactly one per plan. Fold the fixes in as revisions and let the seal's delta "
            "judgment cover them; re-running the panel on a churning plan is the loop this refuses to "
            "rebuild. If the shape itself is wrong, that is a scrap-and-redesign decision for the "
            "operator, not another review.")
    approval = record.get("approval")
    if not approval:
        raise ProjectManagerError("a review records findings against an APPROVED revision; approve first")
    if plan_store.approval_is_stale(record):
        raise ProjectManagerError(
            f"the approval covers revision {approval['revision']} but the head is revision "
            f"{record['current']['revision']}; re-approve before recording a review")
    if not args.lens:
        raise ProjectManagerError("name at least one lens the review was run through")
    # Either shape the ceremony actually produces. The four personas emit plan-review-finding.v1,
    # which carries no id and no lens; mapping it here is what stopped a panel's whole output from
    # dying on a schema refusal at the end of the run that produced it.
    findings = plan_lifecycle.translate_findings(
        json.loads(core.input_text(args.findings)) if args.findings else [], lenses=list(args.lens))
    # Record-time verification of the packet digest, moved from the Build side with the panel. A receipt
    # that names a digest nobody can reproduce vouches for nothing; this re-renders the packet for the
    # APPROVED revision and refuses a receipt that does not match it, so the digest in the record is a
    # fact rather than a claim.
    rendered = plan_projection.render_plan(library.head(slug), record)
    expected = core.digest(rendered.encode("utf-8"))
    if args.packet_digest != expected:
        raise ProjectManagerError(
            f"this receipt names packet digest {args.packet_digest}, but the packet for the approved "
            f"revision {approval['revision']} renders to {expected}. Either the receipt came from a "
            "different packet than the one approved, or the packet was edited after it was cut — "
            "re-cut it with `review packet` and re-run the lenses against what it actually says.")
    # The coverage the approved depth demands is checked here too, not only at the seal, so the gap is
    # surfaced while the reviewers are still warm rather than at the terminal act.
    gap = coverage_gap(approval["depth"], list(args.lens))
    if gap:
        # An exact-terms warning, not a refusal, and it names the way out. The seal stays the single
        # HARD coverage gate — a second hard gate here would just move the wedge earlier — but the
        # warning has to say what to run and what command lands it, because the wedge it replaced was
        # an operator who recorded a partial panel and found the one review slot spent.
        print(f"warning: the approved {approval['depth']} depth requires "
              f"{', '.join(required_lenses(approval['depth'], installed_lenses()))}, and this record "
              f"covers only {', '.join(args.lens)}. Missing: {', '.join(gap)}. The seal will refuse "
              "until they are covered. This record is NOT spent — run the missing lenses and add them:\n"
              f"    project_manager.py review amend {args.plan} --lens <lens> "
              f"--packet-digest {args.packet_digest} --findings <findings.json> "
              "--delivered-effort <low|medium|high> "
              "--reason \"<why this is being completed now>\"\n"
              "  Amendment is possible until the first finding is dispositioned.", file=sys.stderr)
    # The findings fail on their own terms, here, before any ceremony gate: a mistyped severity should
    # be reported as a mistyped severity, not survive to the write and surface as a complaint about the
    # enclosing record — and not be pre-empted by a flag the author has not reached yet.
    _validate_findings(findings)
    delivered = parse_delivered_efforts(getattr(args, "delivered_effort", None), list(args.lens))
    accepted = bool(getattr(args, "accept_effort_shortfall", False))
    require_delivered_effort(approval["depth"], list(args.lens), delivered, accepted=accepted)
    review = {
        "revision": approval["revision"],
        "plan_digest": approval["plan_digest"],
        "packet_digest": args.packet_digest,
        "at": _now(),
        "lenses": list(args.lens),
        "findings": findings,
        "delivered_efforts": delivered,
        "effort_shortfall_accepted": bool(accepted and effort_shortfalls(approval["depth"], delivered)),
    }
    def record_review(current):
        # INSIDE the lock. Recording a review does not mint a revision, so the compare-and-swap on
        # `current.revision` cannot catch a concurrent second review — only re-checking here can, and
        # "exactly one review per plan" is worth exactly as much as this line.
        if current.get("seal"):
            raise ProjectManagerError(
                "this plan was sealed while the review was being prepared; a seal is terminal and the "
                "review it published is the one the pull request carries")
        if current.get("plan_review"):
            raise ProjectManagerError(
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


def cmd_review_amend(args) -> int:
    """Complete or correct the recorded review, until its first finding is dispositioned.

    The review slot is single-minted on purpose — a plan whose every revision re-triggered a panel
    would never converge — but single-minted was being made to mean UNFIXABLE, and those are not the
    same property. A mistyped lens name or a partial record left the operator with the one slot spent
    and no verb that could touch it; recovery meant editing the store by hand. This is the verb that
    was missing. It adds lenses and findings to the review already recorded, and it stops the moment
    the review starts being adjudicated.
    """
    library = _library(args)
    slug = _select(library, args.plan)
    record = library.read_record(slug)
    review = record.get("plan_review")
    if not review:
        raise ProjectManagerError(
            f"no plan review is recorded, so there is nothing to amend. Record one first:\n"
            f"    project_manager.py review record {args.plan} --packet-digest <digest> "
            "--lens <lens> --findings <findings.json> --delivered-effort <low|medium|high>")
    frozen = plan_lifecycle.frozen_reason(record, "plan_review")
    if frozen:
        raise ProjectManagerError("this review can no longer be amended: " + frozen)
    if args.packet_digest != review["packet_digest"]:
        raise ProjectManagerError(
            f"this amendment names packet digest {args.packet_digest}, but the recorded review read "
            f"{review['packet_digest']}. A lens that read a different packet did not review the same "
            "plan, and folding it in here would put two referents behind one receipt. Re-run it "
            f"against the recorded packet, or `review packet {args.plan}` again and check they match.")
    added_lenses = [lens for lens in (args.lens or []) if lens not in review["lenses"]]
    # An amendment adds lenses, so it carries the same obligation the record does: a lens joining the
    # review must say what it ran at, checked against the same approved depth.
    added_efforts = parse_delivered_efforts(getattr(args, "delivered_effort", None), added_lenses)
    if added_lenses:
        require_delivered_effort(record["approval"]["depth"], added_lenses, added_efforts,
                                 accepted=bool(getattr(args, "accept_effort_shortfall", False)))
    # An amendment that USED the shortfall escape has to leave the same acknowledgement the record verb
    # leaves. Passing the flag to the refusal and not writing it down produced the one state the
    # disclosure cannot describe: a gap on the record with nothing saying anyone accepted it.
    amended_shortfall = bool(added_lenses
                             and getattr(args, "accept_effort_shortfall", False)
                             and effort_shortfalls(record["approval"]["depth"], added_efforts))
    added = plan_lifecycle.translate_findings(
        json.loads(core.input_text(args.findings)) if args.findings else [],
        lenses=list(args.lens or review["lenses"]))
    _validate_findings(added)
    existing_ids = {f["id"] for f in review.get("findings", [])}
    collisions = sorted({f["id"] for f in added} & existing_ids)
    if collisions:
        raise ProjectManagerError(
            "these finding ids are already in the review: " + ", ".join(collisions)
            + ". An amendment ADDS to a review; it never rewrites a finding already recorded, because "
            "the record would then show a review that was never run in that form. Give the new "
            "findings distinct ids, or correct one in place with `finding amend`.")
    amendment = {"artifact": "plan_review", "at": _now(), "reason": args.reason}

    def amend(current):
        if plan_lifecycle.frozen_reason(current, "plan_review"):   # re-asserted inside the lock
            raise ProjectManagerError(
                "this review was sealed or began being dispositioned while the amendment was being "
                "prepared; re-read it before deciding what to do next")
        current["plan_review"]["lenses"] = current["plan_review"]["lenses"] + added_lenses
        current["plan_review"].setdefault("findings", []).extend(added)
        current["plan_review"].setdefault("delivered_efforts", {}).update(added_efforts)
        if amended_shortfall:
            current["plan_review"]["effort_shortfall_accepted"] = True
        current.setdefault("amendments", []).append(amendment)

    library.update_record(slug, amend)
    updated = library.read_record(slug)["plan_review"]
    print(f"amended the review: +{len(added_lenses)} lens(es), +{len(added)} finding(s)")
    print(f"  lenses now: {', '.join(updated['lenses'])}")
    gap = coverage_gap(record["approval"]["depth"], updated["lenses"])
    print("  coverage: " + ("complete for the approved depth" if not gap
                            else "still missing " + ", ".join(gap)))
    return 0


def cmd_finding_amend(args) -> int:
    """Correct one finding as recorded, until it is dispositioned.

    A finding's disposition is a judgment about the finding AS WRITTEN, so this stops there: amending
    afterwards would silently re-aim a judgment somebody already made.
    """
    library = _library(args)
    slug = _select(library, args.plan)
    record = library.read_record(slug)
    if not record.get("plan_review"):
        raise ProjectManagerError("no plan review is recorded, so there is no finding to amend")
    frozen = plan_lifecycle.frozen_reason(record, "finding", finding_id=args.id)
    if frozen:
        raise ProjectManagerError(f"{args.id} can no longer be amended: " + frozen)
    changes = {key: getattr(args, key) for key in ("severity", "summary", "lens", "location")
               if getattr(args, key, None)}
    if not changes:
        raise ProjectManagerError(
            "name what to correct: --severity, --summary, --lens, or --location")
    amendment = {"artifact": f"finding {args.id}", "at": _now(), "reason": args.reason}

    def amend(current):
        if plan_lifecycle.frozen_reason(current, "finding", finding_id=args.id):
            raise ProjectManagerError(
                f"{args.id} was sealed or dispositioned while the amendment was being prepared")
        for finding in current["plan_review"]["findings"]:
            if finding["id"] == args.id:
                finding.update(changes)
        current.setdefault("amendments", []).append(amendment)

    library.update_record(slug, amend)
    print(f"amended {args.id}: " + ", ".join(f"{k}={v!r}" for k, v in sorted(changes.items())))
    return 0


def cmd_present_findings(args) -> int:
    """Attest that the panel's outcome was shown to the operator. The seal refuses without this.

    Separate from `seal` on purpose, and ordered before it. The failure it answers is a session that
    ran a four-lens panel, dispositioned twenty-one findings and sealed, all without the operator
    seeing a single finding — so the attestation has to be its own act, taken after the findings
    exist and before the terminal one, rather than a flag on the command that ends the ceremony.
    """
    library = _library(args)
    slug = _select(library, args.plan)
    record = library.read_record(slug)
    review = record.get("plan_review")
    if not review:
        raise ProjectManagerError(
            "no plan review is recorded, so there is no panel outcome to present. At a depth that runs "
            "no cold lenses there is nothing to attest here and the seal does not ask for it.")
    outstanding = [f["id"] for f in review.get("findings", []) if not f.get("disposition")]
    if outstanding:
        raise ProjectManagerError(
            "present the panel's outcome once its findings have dispositions, not before — the "
            "operator is being shown what was found AND what was done about each. Outstanding: "
            + ", ".join(outstanding))
    consent = _require_consent(record, "findings-presented", args)

    def attest(current):
        if current.get("seal"):
            raise ProjectManagerError("this plan was sealed while the presentation was being recorded")
        current.setdefault("consent", []).append(consent)

    library.update_record(slug, attest)
    blocking = [f for f in review.get("findings", []) if f["severity"] == "blocking"]
    print(f"recorded that the operator was shown the panel's outcome: {len(review.get('findings', []))} "
          f"finding(s), {len(blocking)} blocking, all dispositioned")
    print(f"  their words: “{consent['decision']}”")
    print(f"\nnext: seal it:\n    project_manager.py seal {args.plan} "
          "--operator-decision \"<what the operator said>\"")
    return 0


def cmd_finding_dispose(args) -> int:
    library = _library(args)
    slug = _select(library, args.plan)
    record = library.read_record(slug)
    # A seal freezes the dispositions too, not merely the plan text. The Build reads this review LIVE from
    # the record when it composes the pull request, which is what makes a plan-review disagreement immune
    # to the Build's own receipt-supersession rule — but reading live from a record that stayed editable
    # would relocate the silent drop rather than close it: a finding left honestly blocking at seal could
    # be turned into "rejected, no issue" any time before compose, and the operator would meet the edited
    # version at merge with nothing showing it had changed.
    if record.get("seal"):
        raise ProjectManagerError(
            "this plan is sealed, and a seal freezes its review dispositions as well as its text — the "
            "pull request publishes them as they stood when you sealed. If a disposition is genuinely "
            "wrong, clone this plan into a new one and disposition it there rather than editing a "
            "sealed record.")
    review = record.get("plan_review")
    if not review:
        raise ProjectManagerError("no plan review is recorded, so there is nothing to disposition")
    match = [f for f in review.get("findings", []) if f["id"] == args.id]
    if not match:
        known = ", ".join(f["id"] for f in review.get("findings", [])) or "none"
        raise ProjectManagerError(f"no finding {args.id!r} in this review; it holds: {known}")
    stated = getattr(args, "blocks_this_pr_stated", None)
    if stated is None:
        raise ProjectManagerError(
            f"say whether {args.id} still holds the pull request this plan authorizes: "
            "`--blocks-this-pr` or `--does-not-block-this-pr`. There is no default — a gate that "
            "reads silence as 'not blocking' fails toward permitting, and the whole value of the "
            "answer is that someone gave it.")
    blocks = stated
    # The disclosure rule the Build side already enforced, arriving with the panel: a BLOCKING finding
    # that the orchestrator decides should not block needs an operator-safe sentence, because that
    # decision is a disagreement the operator meets at merge. Without one there is nothing honest to
    # publish, and "no summary recorded" on the merge surface is how a real objection disappears.
    if match[0]["severity"] == "blocking" and not blocks and not args.operator_summary:
        raise ProjectManagerError(
            f"{args.id} is a BLOCKING finding you are not leaving blocking. That is a disagreement the "
            "operator has to be able to read at merge, so it needs a safe, operator-facing sentence: "
            "pass --operator-summary.")

    def change(current):
        if current.get("seal"):          # re-asserted inside the lock, not from the copy above
            raise ProjectManagerError(
                "this plan was sealed while you were dispositioning; a seal is terminal and freezes the "
                "dispositions the pull request will publish")
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


def _print_disclosures(disclosures: list, *, stream) -> None:
    """One wording for both commands. A disclosure is not a refusal and must never read like one."""
    if not disclosures:
        return
    print("\nworth knowing, but not blocking this seal:", file=stream)
    for disclosure in disclosures:
        print(f"  - {disclosure}", file=stream)


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
    except ProjectManagerError as exc:
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
    if review and required and "delivered_efforts" in review:
        # A COHERENCE check, never a level check. The level was gated at `review record`, where the
        # exits still existed; re-gating it here would only wedge a plan whose panel already ran. What
        # the seal owes is that a record which HAS started stating delivered effort states it for every
        # lens it seals — a half-filled map would publish a depth promise as met for lenses that never
        # said so. A record with no map at all predates the field and is disclosed as unrecorded, not
        # refused: the Build's pull-request body carries that honestly
        # (StarshipSuperjam/engine-template#1067).
        efforts = review["delivered_efforts"]
        silent = [lens for lens in review.get("lenses", []) if lens not in efforts]
        if silent:
            refusals.append(
                "this review records the effort some of its lenses delivered but not all of them, so the "
                f"seal would publish an approved {depth} depth as met by lenses that never said what they "
                "ran at: " + ", ".join(silent) + ". Record them with `review amend --delivered-effort`.")
    if review:
        outstanding = [f["id"] for f in review.get("findings", []) if not f.get("disposition")]
        if outstanding:
            refusals.append("these findings have no disposition: " + ", ".join(outstanding))
        # The consent gate the silent ceremony bought. A panel whose outcome the operator never saw
        # is a panel that informed nobody, and this is where that becomes a refusal rather than a
        # hope. Only when a panel actually ran: at a depth with no cold lenses there is nothing to
        # present, and demanding it anyway would be ceremony for its own sake.
        if not plan_lifecycle.consent_for(record, "findings-presented"):
            refusals.append(
                f"the panel's outcome has not been presented to the operator. {len(review.get('findings', []))} "
                "finding(s) were recorded and dispositioned, and a seal is the last moment anyone can "
                "act on them. Show the operator what was found and what was done about each, then:\n"
                "      project_manager.py present-findings <plan> --operator-decision \"<what they said>\"")
    refusals.extend(_program_check(library, record, document)[0])
    return refusals


def _program_check(library: plan_store.PlanLibrary, record: dict, document: dict) -> tuple:
    """(refusals, disclosures) about this plan's program — the carry-forward re-check and its edges.

    The decay is re-derived from CURRENT heads rather than trusted from join time: a successor that
    joined before its predecessor minted an obligation has been claiming to answer for a set that grew
    underneath it, and the seal is the last moment that can be fixed without a clone. Advisory
    everywhere else, a refusal here.

    Split into two lists because the two halves answer to different rules. A plan whose OWN program
    cannot be re-checked must not seal — that is the guarantee. A plan that has nothing to do with a
    malformed record elsewhere in the library must not be held hostage by it, but the operator should
    still be TOLD the library has a broken record, because a silent skip is how the next one hides.
    """
    refusals, disclosures = [], []
    try:
        programs = plan_program.ProgramLibrary(library)
        claimed = (document.get("program") or {}).get("program_id")
        membership = programs.program_membership(record["plan_id"], claimed_program_id=claimed)
    except Exception as exc:  # noqa: BLE001 — the lookup itself failing is not a licence to proceed
        return ([f"the plan library's programs could not be enumerated to re-check this plan's "
                 f"carry-forward obligations ({exc}); resolve that before sealing, because an "
                 "unchecked carry-forward is the decay the program object exists to stop."], [])

    if membership["claims_unreadable"]:
        # The plan says which program it belongs to and that program's record cannot be parsed. Fail
        # CLOSED on the plan's own word: this is exactly the case the back-link exists to catch.
        refusals.append(
            f"this plan declares that it belongs to program {claimed}, and that program's record "
            "cannot be read, so the obligations it carries forward cannot be re-checked. Repair the "
            "program record before sealing — an unchecked carry-forward is the decay the program "
            "object exists to stop.")
    elif membership["slug"]:
        try:
            for entry in programs.carry_forward_decay(membership["slug"], plan_id=record["plan_id"]):
                refusals.append(
                    f"this plan no longer answers for {len(entry['obligations'])} obligation(s) its "
                    f"predecessor {entry['predecessor_plan_id']} carries — "
                    + ", ".join(o["id"] for o in entry["obligations"])
                    + ". They were minted after this plan joined the program, so the join-time check "
                      "never saw them. Revise to answer for each: satisfied, still carried, or "
                      "released with a reason.")
        except Exception as exc:  # noqa: BLE001 — this plan's OWN program: refuse, never skip
            refusals.append(f"the program this plan belongs to could not be read to re-check its "
                            f"carry-forward obligations ({exc}); resolve that before sealing, because "
                            "an unchecked carry-forward is the decay the program object exists to stop.")

    for entry in membership["unreadable"]:
        if entry["slug"] == membership["slug"] or membership["claims_unreadable"]:
            continue                      # already refused above; not disclosed twice
        # THREE cases, because the record can be broken in two ways and each supports a different
        # honest sentence. Collapsing them printed a claim about membership the inputs contradicted.
        if entry["names_this_plan"]:
            # Parseable, and its children name this plan — the lookup resolved elsewhere only because
            # a readable record claimed it first. Saying "it does not name this plan" would state as
            # fact something the code two lines up already computed to be false.
            disclosures.append(
                f"the program record at `{entry['slug']}` could not be read ({entry['error']}), AND "
                "it names this plan as one of its children — so this plan is claimed by more than "
                "one program and that claim cannot be re-checked. Repair the record before relying "
                "on this seal's carry-forward evidence.")
        elif _record_parses(library, entry["slug"]):
            # Parseable but schema-invalid: its children ARE readable, so membership is genuinely
            # settled and this plan is genuinely not in it.
            disclosures.append(
                f"the program record at `{entry['slug']}` could not be read ({entry['error']}). It "
                "does not name this plan, so it does not stand in the way of this seal — but it is "
                "broken, and no program it holds can be re-checked until it is repaired.")
        else:
            # Will not parse at all. "It does not name this plan" is an overstatement here for the
            # same reason the opposite would be: nothing can be read out of it either way.
            disclosures.append(
                f"the program record at `{entry['slug']}` could not be parsed at all "
                f"({entry['error']}), so whether it names this plan cannot be determined. This seal "
                "is proceeding on the plan's own program back-link; repair the record before relying "
                "on the library's carry-forward evidence.")
    # ONLY the unparseable class leaves a genuine gap. A record that fails its schema still yields its
    # children, so membership in it is knowable and was already settled above; saying "nothing here
    # could tell" about such a record contradicts the disclosure printed one line earlier and sends
    # the operator looking for a corrupt file that parses perfectly well.
    unknowable = [entry for entry in membership["unreadable"]
                  if not entry["names_this_plan"] and not _record_parses(library, entry["slug"])]
    if membership["slug"] is None and not claimed and unknowable:
        # The one gap two sources cannot close, stated rather than assumed away: a child added before
        # the back-link was required, under a record that will not parse, is indistinguishable from a
        # standalone plan. Named here so the operator can judge it, not silently resolved as "no
        # program" — which is precisely the fail-open this whole split exists to avoid.
        disclosures.append(
            "this plan carries no program back-link, and the library holds a program record that "
            "cannot be parsed — so if this plan were a child of THAT program, nothing here could "
            "tell. It is being treated as a standalone plan. Repair the record, or add the plan's "
            "`program.program_id` back-link, to close that gap.")
    return refusals, disclosures


def _record_parses(library: plan_store.PlanLibrary, slug: str) -> bool:
    """Whether a program record is readable JSON, regardless of whether it satisfies its schema.

    The two corruption classes differ in exactly this, and the disclosures must not conflate them: a
    schema-invalid record still says whose children it holds, so nothing about membership is unknown.
    """
    try:
        programs = plan_program.ProgramLibrary(library)
        core.json_file(programs.program_dir(slug) / plan_program.RECORD_FILENAME)
        return True
    except Exception:  # noqa: BLE001 — unreadable is the answer, not an error to raise
        return False


def seal_disclosures(library: plan_store.PlanLibrary, slug: str) -> list:
    """What the operator should know at a seal that is NOT a reason to refuse it.

    A companion to `seal_refusals` rather than a second return value from it, because that function is
    a pure predicate that thirteen call sites and a test ("show prints exactly what seal refuses")
    depend on: a refusal list that quietly grew non-refusals would break the one thing every caller
    trusts about it.
    """
    record = library.read_record(slug)
    if record.get("seal") or record.get("closure"):
        return []
    try:
        document = library.head(slug)
    except ProjectManagerError:
        return []
    return _program_check(library, record, document)[1]


def cmd_seal(args) -> int:
    """The terminal act. Nothing locks before this, and nothing changes after it.

    There is deliberately no sealed-but-failed state. A plan carrying blocking findings simply stays
    an unsealed draft that still carries them — editable, resumable, on the shelf — because a plan
    stuck in a limbo it cannot leave is worse than one that plainly is not ready.
    """
    library = _library(args)
    slug = _select(library, args.plan)
    refusals = seal_refusals(library, slug)
    disclosures = seal_disclosures(library, slug)
    if refusals:
        print(f"not sealing {library.read_record(slug)['plan_id']}; it remains an editable draft:",
              file=sys.stderr)
        for refusal in refusals:
            print(f"  - {refusal}", file=sys.stderr)
        _print_disclosures(disclosures, stream=sys.stderr)
        return 1
    # Printed BEFORE the seal is minted, because a seal is terminal: the last moment this can reach
    # the operator while it is still actionable is now.
    _print_disclosures(disclosures, stream=sys.stderr)

    record = library.read_record(slug)
    document = library.head(slug)
    # At a depth that requires no cold lenses there is no review record, and the reviewed digest IS the
    # approved one — the operator's own read at approval is what the seal records having covered.
    review = record.get("plan_review")
    reviewed_digest = review["plan_digest"] if review else record["approval"]["plan_digest"]
    sealed_digest = record["current"]["plan_digest"]
    changed = reviewed_digest != sealed_digest
    if changed and not args.delta_judgment:
        raise ProjectManagerError(
            f"the plan changed after it was read (read at revision {(review or record['approval'])['revision']}, "
            f"sealing revision {record['current']['revision']}). That is the expected shape — fixes fold "
            "in as revisions — but the delta needs one proportional judgment before it locks. Read it "
            f"with `diff {args.plan} --from {(review or record['approval'])['revision']} --to "
            f"{record['current']['revision']}`, then seal with --delta-judgment none or scoped.")
    if args.delta_judgment == "scoped" and not args.delta_rationale:
        raise ProjectManagerError("a scoped delta judgment needs a rationale saying what changed and "
                                   "why it is still the reviewed plan")
    judgment = args.delta_judgment or "none"
    consent = _require_consent(record, "seal", args)

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
            raise ProjectManagerError("another session sealed this plan while this one was reading it")
        current["seal"] = seal
        current.setdefault("consent", []).append(consent)

    library.update_record(slug, mint_seal, expected_revision=record["current"]["revision"])
    plan_projection.project_library(library)
    print(f"sealed {record['plan_id']} at revision {seal['revision']}")
    print(f"  on the operator's decision: “{consent['decision']}”")
    print(f"  reviewed  {reviewed_digest}")
    print(f"  sealed    {sealed_digest}"
          + ("  (unchanged since review)" if not changed else f"  (delta judged {judgment})"))
    print(f"  payload   {seal['build_plan_digest']}")
    if changed:
        print("\nThe PR must disclose that the sealed plan differs from the reviewed one, and by what.")
    print(seal_handback(record["plan_id"]))
    return 0


def seal_handback(plan_id: str) -> str:
    """The plan-to-build hand-back: stop, settle, offer, wait.

    SIX LINES, ADDRESSED TO THE SESSION. This prints into the session's context, not onto the
    operator's screen, so it is instructions for the assistant's next move — not operator training.
    The operator ruled the long form out: a hand-back that needs paragraphs of meta-commentary to
    explain the next step is a poorly designed step. The settle summary the session then gives the
    operator is conversational and build-specific; the readiness line at its end is the offer.

    /compact, NEVER /clear. The one build session that lost its thread — the incident this whole
    spine exists to prevent — is the one that cleared instead of compacting. A cleared session keeps
    nothing to re-ground from; a compacted one keeps the summary plus everything settled below.

    An offer, not a gate: the bind's own --operator-decision consent is the agreement to begin, and
    nothing mechanical checks any of this. The one-time /autocompact recommendation lives in the
    runbook, not here — repeating it at every seal is nagging, not guidance.
    """
    return "\n".join([
        "",
        "The plan is sealed and read-only. Stop building context here.",
        "Settle into the record anything that still lives only in this conversation, then offer",
        "the operator a /compact and their model and effort choice for the build phase. Wait.",
        "Their go begins the Build:",
        f"  build_coordinator.py plan bind --plan {plan_id} \\",
        "    --repository <owner/repo> --pr <number> --operator-decision \"<their go>\"",
    ])


def cmd_close(args) -> int:
    """retire / abandon / complete — how a plan ends. None of them deletes anything."""
    library = _library(args)
    slug = _select(library, args.plan)
    record = library.read_record(slug)
    if record.get("closure"):
        raise ProjectManagerError(
            f"this plan is already {record['closure']['state']}; reopen it before closing it differently")
    _close_plan(library, slug, args.state, args.reason)
    print(f"{record['plan_id']} is now {args.state}: {args.reason}")
    print("Nothing was deleted — the plan and every revision stay on the shelf.")
    return 0


def _close_plan(library, slug: str, state: str, reason: str, *,
                refuse_if_active: bool = False) -> None:
    """Write a plan's closure and re-project the library. THE close path, and the only one.

    Extracted so `program supersede` retires the plan it replaces through exactly this door rather
    than through a second implementation of it. A supersession that wrote a closure some other way
    would be a plan marked closed in the record while the projection still advertised it — which is
    the shape of the loaded gun supersede exists to unload.

    `refuse_if_active` re-asserts supersede's own precondition INSIDE the lock. Supersede reads the
    target's status before it writes anything, and that read is unlocked: a Build binding to the
    plan in the window between the read and this write would leave a plan retired underneath a
    running Build — the one outcome supersede calls unrecoverable, and one `derived_status` then
    hides, because it reports the closure before it looks at the binding. The store's own discipline
    is that every gate re-asserts its precondition in the mutator; this is supersede honouring it.
    """
    def close(current):
        if current.get("closure"):       # re-asserted inside the lock
            raise ProjectManagerError(
                f"another session closed this plan as {current['closure']['state']} while this one was "
                "reading it; reopen it before closing it differently")
        if refuse_if_active and current.get("build_binding"):
            raise ProjectManagerError(
                "a Build bound to this plan while the supersession was being prepared, so retiring "
                "it now would strand that Build under a retired plan — it would go on publishing "
                "from it, and its completion could never be recorded. Nothing was written. ABANDON "
                "that Build and supersede then works — or let it MERGE, after which merged history "
                "is corrected by appended work (`program add --after`), never replaced.")
        current["closure"] = {"state": state, "at": _now(), "reason": reason}

    library.update_record(slug, close)
    plan_projection.project_library(library)


def cmd_reopen(args) -> int:
    """Undo a retirement or an abandonment. Completion and a seal are terminal.

    Retiring and abandoning are bookkeeping about attention, and an operator may change their mind.
    A seal is a promise that a specific plan, at a specific digest, was reviewed and handed to a
    Build — and unsealing would let a plan be edited while something downstream still believes it
    said what it said. The way past a seal is a new plan, which is why `clone` exists.
    """
    library = _library(args)
    slug = _select(library, args.plan)
    record = library.read_record(slug)
    if not record.get("closure"):
        raise ProjectManagerError("this plan is not closed, so there is nothing to reopen")
    if record["closure"]["state"] == "complete":
        raise ProjectManagerError(
            "this plan is complete, and completed Build history is terminal. Start a new plan for new work.")
    if record.get("seal"):
        raise ProjectManagerError(
            "this plan is sealed, and a seal is terminal — reopening it would let an edited plan keep "
            "a digest a Build already trusted. Clone it into a new plan instead.")

    # The program record gets a say before a plan closure is undone, because two of its own facts
    # are built on that closure staying put: a SUPERSEDED child gave its place on the chain to its
    # replacement, and a COMPLETE program is the operator's judgment recorded over every live child
    # being settled. Three properties of this veto were each a reviewed defect in its first cut:
    #
    # - It fails CLOSED. A program record that cannot be read might carry either fact, and skipping
    #   it silently was the one fail-open on this file's governance surface — the seal path refuses
    #   on the identical condition, and this door refuses the same way. Membership is established
    #   the way `program_membership` establishes it: a broken record that still names this plan
    #   owns a veto it cannot cast, so the reopen waits for the record to be repaired.
    # - It runs UNDER THE OWNING PROGRAM'S LOCK, held across the plan write. `program complete`,
    #   `program reopen` and `mark_superseded` all write under that same lock, so the two races a
    #   reviewer traced — a completion landing between this check and the plan write, and a
    #   supersession marking the child in that same window — serialize instead of interleaving.
    #   Lock order is program-then-plan; nothing anywhere takes them in the other order.
    # - The plan mutator still re-asserts its OWN preconditions, so the lock adds ordering without
    #   this door trusting a check across a boundary.
    programs = plan_program.ProgramLibrary(library)
    # BOTH of membership's sources are consulted, exactly as the seal path consults them. The first
    # cut read only the program records, and a reviewer proved the gap: a record that parses but
    # fails schema still names its children and refused correctly — while a TRUNCATED record, the
    # strictly more damaged case, names nobody, and the veto silently passed. The plan document's
    # own back-link is the evidence that survives an unparseable record, so it is what closes that
    # hole: a claim against a record that will not read refuses the same way `_program_check` does.
    claimed = None
    try:
        claimed = (library.head(slug).get("program") or {}).get("program_id")
    except Exception:  # noqa: BLE001 — an unreadable head leaves only the record sweep below,
        pass           # and the unparseable-record check right after refuses if that is not enough
    membership = programs.program_membership(record["plan_id"], claimed_program_id=claimed)
    # Without a back-link there is no second source — and against a program record that will not
    # even PARSE, the record sweep answers nothing either: raw children were checked for every
    # record that at least parsed, so `not parseable` is precisely "membership unknowable". With
    # both sources dark, this door refuses like its neighbours instead of passing in silence —
    # the sealing gate meets the identical library state and says so out loud.
    if claimed is None:
        dark = [entry for entry in membership["unreadable"] if not entry.get("parseable", True)]
        if dark:
            raise ProjectManagerError(
                f"the program record for {dark[0]['slug']} cannot even be parsed, and this plan "
                "carries no readable program back-link — so whether some program marks it "
                "superseded, or was completed over it, cannot be told from either source. Repair "
                "that record first; a silent pass here would undo a decision nobody reversed.")
    broken = [entry for entry in membership["unreadable"] if entry["names_this_plan"]]
    if broken:
        raise ProjectManagerError(
            f"the program record for {broken[0]['slug']} names this plan but cannot be read "
            f"({broken[0]['error']}), so whether reopening is allowed cannot be told. Repair that "
            "record first: it may mark this plan superseded, or its program complete, and a silent "
            "pass here would undo a decision nobody reversed.")
    if membership["claims_unreadable"]:
        raise ProjectManagerError(
            f"this plan declares that it belongs to program {claimed}, and that program's record "
            "cannot be read — it may mark this plan superseded, or its program complete. Repair "
            "the record first; a silent pass here would undo a decision nobody reversed.")

    previous = {}

    def reopen(current):
        previous["state"] = current["closure"]["state"]   # read under the lock, not before it
        if not current.get("closure"):   # re-asserted inside the lock
            raise ProjectManagerError("another session reopened this plan already")
        if current["closure"]["state"] == "complete":
            raise ProjectManagerError(
                "this plan is complete, and completed Build history is terminal")
        if current.get("seal"):
            raise ProjectManagerError("this plan is sealed, and a seal is terminal")
        current["closure"] = None

    def veto_of(program_record):
        child = next((c for c in program_record["children"]
                      if c["plan_id"] == record["plan_id"]), None)
        if child is not None and child.get("superseded_by"):
            raise ProjectManagerError(
                f"this plan was superseded by {child['superseded_by']} in "
                f"{program_record['program_id']} — its place on the chain was given away, and "
                "reopening it would stand two plans in one position. If the supersession was "
                "wrong, supersede the replacement in turn, or clone this plan and add the copy.")
        if child is not None and (program_record.get("closure") or {}).get("state") == "complete":
            raise ProjectManagerError(
                f"{program_record['program_id']} is recorded complete, and that judgment was "
                "made over this child being settled. Reopening the child would make the "
                "program's record false without anyone deciding so — `program reopen "
                f"{program_record['program_id']} --reason \"...\"` first, then reopen the plan.")

    # Two-program membership is off-design — the join doors require a back-link and refuse a plan
    # already on a chain — but a legacy or hand-edited record can still construct it, and the
    # first cut vetoed only on the FIRST record found, quietly losing the second's say. Every
    # OTHER readable record naming this plan vetoes here, unlocked (a belt); the owning record's
    # veto runs again under its own lock below, which is the half that orders against writers.
    for program_slug in programs.slugs():
        if program_slug == membership["slug"]:
            continue
        try:
            other = programs.read(program_slug)
        except Exception:  # noqa: BLE001 — an unreadable record already refused above if it names us
            continue
        veto_of(other)

    def veto_and_reopen():
        if membership["slug"]:
            program_record = programs.read(membership["slug"])   # re-read under the program lock
            veto_of(program_record)
        library.update_record(slug, reopen)

    if membership["slug"]:
        lock_path = programs.program_dir(membership["slug"]) / (
            plan_program.RECORD_FILENAME + ".lock")
        with core.exclusive_lock(lock_path):
            veto_and_reopen()
    else:
        veto_and_reopen()
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
        raise ProjectManagerError(
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
    if getattr(args, "supersedes", None):
        document["program"] = _supersession_block(library, args.supersedes)
    new_slug = library.create(document, intake={
        "provenance": f"cloned from {source_id} at revision {library.read_record(slug)['current']['revision']}: "
                      f"{args.reason}",
        "predecessors": [f"{source_id} — {library.read_record(slug)['title']}"]})
    plan_projection.project_library(library)
    print(f"cloned {source_id} into {document['plan_id']} at {library.plan_dir(new_slug)}")
    print("It carries no approval, no review and no seal — none of that was granted for this document.")
    if getattr(args, "supersedes", None):
        program = document["program"]
        carried = program.get("carried_obligations", [])
        print(f"\nPre-filled to supersede {args.supersedes}: the back-link to "
              f"{program['program_id']}"
              + (f", and {len(carried)} obligation(s) re-declared as carried."
                 if carried else ", and nothing outstanding to inherit."))
        for obligation in carried:
            print(f"  - {obligation['id']}: {obligation['statement']}")
        print("Those come from the PREDECESSOR of the plan being replaced, which is the only honest "
              "source: the replaced plan never landed, so its own claims about what it satisfied or "
              "released describe work that does not exist.")
        print(f"Complete the supersession with `program supersede <program> {args.supersedes} "
              f"--with {document['plan_id']} --reason \"...\"`.")
    return 0


def _supersession_block(library, superseded_selector: str) -> dict:
    """The `program` block a `clone --supersedes` starts life with.

    Three things, and the third is the one worth stating. The back-link, so the clone can join its
    program at all. The replaced plan's predecessor edge, recorded as AUTHORING-TIME PROVENANCE — the
    program record's edge is the sole order authority, and this copy is a note about where the clone
    was written to fit, never a second claim about the chain. And the obligations, sourced from that
    PREDECESSOR's carried set rather than from the plan being replaced.

    That last choice is the whole point. The replaced plan's own block says what IT meant to satisfy
    or release, and none of that happened — it is being replaced precisely because it never landed.
    Copying its satisfied claims into the replacement would hand the new plan credit for work nobody
    did, which is exactly the laundering the carry-forward guarantee exists to prevent.
    """
    import plan_program
    programs = plan_program.ProgramLibrary(library)
    superseded_id = library.read_record(library.resolve(superseded_selector))["plan_id"]
    slug = programs.program_for_plan(superseded_id)
    if not slug:
        raise ProjectManagerError(
            f"{superseded_id} is not a child of any program in this library, so there is no place "
            "for a replacement to inherit. Supersession is a program-order decision; a standalone "
            "plan is simply cloned.")
    record = programs.read(slug)
    child = next(c for c in record["children"] if c["plan_id"] == superseded_id)
    block = {"program_id": record["program_id"]}
    inherited = child.get("predecessor_plan_id")
    if inherited:
        block["predecessor_plan_id"] = inherited
        # Program-level releases are subtracted here for the same reason every gate subtracts them:
        # a debt formally let go on the record must not come back. Pre-filling it as `carried` would
        # quietly reverse the operator's own decision, in the one place they are least likely to
        # re-read it — a generated block they are about to build on.
        released = programs.released_at(record, inherited)
        carried = {identifier: obligation
                   for identifier, obligation
                   in plan_program.carried_forward(library.head(library.resolve(inherited))).items()
                   if identifier not in released}
        if carried:
            block["carried_obligations"] = [
                {"id": o["id"], "statement": o["statement"], "state": "carried"}
                for o in sorted(carried.values(), key=lambda o: o["id"])]
    return block


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

    Raises ProjectManagerError when there is nothing to import. Callers running inside a hook treat
    that as no-import-and-carry-on: an acceptance that imports nothing must never cost the operator
    their turn, and a session that cannot reach its plan library is still a session that can talk.
    """
    text = text if isinstance(text, str) else ""
    if not text.strip():
        raise ProjectManagerError("there is no plan text to import")
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
        "next_command": f"python tools/project_manager.py preview --plan {document['plan_id']}",
    }


def arrival_report(arrival: dict) -> str:
    """What the session says after an import — the whole point of grading arrival, not just departure.

    An operator who accepts a plan and is then handed a write refusal has been told the engine is
    broken. So this names what happened, what did NOT happen, and the one command that moves it
    forward, and it is explicitly for relaying: unlike the stance directive it replaces, its whole
    job is to reach the operator.
    """
    return (
        f"The plan you just accepted was imported into the Project Manager as {arrival['plan_id']}, "
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
        raise ProjectManagerError(
            f"not a plan bundle (schema_version {bundle.get('schema_version')!r})")
    record, revisions = bundle["record"], bundle["revisions"]
    # Shape first. The record's own schema pattern-constrains `slug` and every `snapshot`, so this one
    # call is what stops a crafted bundle choosing where the store writes.
    core.validate(record, plan_store.RECORD_SCHEMA)
    recomputed = core.digest({"record": record, "revisions": revisions})
    if recomputed != bundle.get("bundle_digest"):
        raise ProjectManagerError(
            f"the bundle does not match its own digest (recorded {bundle.get('bundle_digest')}, found "
            f"{recomputed}); it was altered after export and is not trustworthy")
    for entry in record["ledger"]:
        if "redacted" in entry:
            continue
        body = revisions.get(str(entry["revision"]))
        if body is None:
            raise ProjectManagerError(
                f"the bundle's ledger claims revision {entry['revision']} but carries no body for it")
        actual = core.digest(body)
        if actual != entry["plan_digest"]:
            raise ProjectManagerError(
                f"revision {entry['revision']} does not match its recorded digest (recorded "
                f"{entry['plan_digest']}, found {actual})")
        plan_contract.validate_document(body)

    existing, unreadable = None, []
    for candidate in library.slugs():
        try:
            if library.read_record(candidate)["plan_id"] == record["plan_id"]:
                existing = candidate
                break
        except ProjectManagerError:
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
        raise ProjectManagerError(
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
        raise ProjectManagerError(
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
            raise ProjectManagerError(          # re-asserted inside the lock, not from the read above
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
        raise ProjectManagerError("the imported plan does not verify after writing: "
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
    except ProjectManagerError:
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
    return 0


def cmd_program_show(args) -> int:
    programs = _programs(args)
    slug = programs.resolve(args.program)
    print(plan_program.render(programs, programs.read(slug)))
    _report_decay(programs, slug)
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
        _close_plan(library, superseded_slug, "retired", args.reason, refuse_if_active=True)
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
        raise ProjectManagerError(
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
    record = programs.close(programs.resolve(args.program), args.state, args.reason,
                            acknowledged_unknown=getattr(args, "acknowledge_unknown", None))
    print(f"{record['program_id']} is now {args.state}: {args.reason}")
    if record["closure"].get("acknowledged_unknown"):
        print("Closed over an unknown, on the record: "
              f"{record['closure']['acknowledged_unknown']}")
        print("That is a decision, not a resolution — what this program owed still cannot be "
              "computed from its record.")
    print("Nothing was deleted — every child plan and every revision stays on the shelf.")
    return 0


def cmd_program_complete(args) -> int:
    """The only door to a complete program. Nothing derives it, and nothing else writes it."""
    programs = _programs(args)
    record = programs.complete(programs.resolve(args.program), args.reason)
    print(f"{record['program_id']} is recorded complete: {args.reason}")
    print("Recorded, not derived — this is your judgment that the objective is met, and the record "
          "now says so with your reason attached.")
    print("It is reversible: `program reopen` undoes it, with a reason, and keeps what was undone.")
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
            raise ProjectManagerError(
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
    return 0


def cmd_program_lanes_clear(args) -> int:
    programs = _programs(args)
    slug = programs.resolve(args.program)
    record = programs.clear_lanes(slug, args.reason)
    print(f"cleared the lane split on {record['program_id']}")
    print(f"  {args.reason}")
    print("What was cleared is kept in the lane history — `program show` lists it, marked cleared.")
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
        lines.append(f"  python tools/project_manager.py program lanes set {program_id} "
                     f"--reason \"<why>\" {lane_args}")
    print("\n".join(lines))
    return 0


def _consent_argument(command, gate: str) -> None:
    """The one flag that carries an operator decision, worded the same at every gate it guards."""
    command.add_argument(
        "--operator-decision", required=True,
        help=f"The operator's actual words consenting to {plan_lifecycle.GATES[gate]}. Published "
             "verbatim in the pull request. It is a record, not a proof.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="project_manager.py",
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
    _consent_argument(approve, "approve")
    approve.set_defaults(func=cmd_approve)

    present = sub.add_parser(
        "present-findings",
        help="record that the operator was shown the panel's outcome (the seal refuses without it)")
    present.add_argument("plan")
    _consent_argument(present, "findings-presented")
    present.set_defaults(func=cmd_present_findings)

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
    record_review.add_argument("--findings", help="a JSON array of findings, in either accepted shape: "
                                                  "the record shape (id, lens, severity, summary) or "
                                                  "plan-review-finding.v1 (severity, message, location), "
                                                  "which the reviewer personas emit and which is mapped")
    record_review.add_argument("--accept-effort-shortfall", action="store_true",
                            help="Record a panel that came in UNDER the approved depth, keeping the honest number. The gap is published in the pull request.")
    record_review.add_argument("--delivered-effort", action="append",
                            help="The reasoning effort a reviewer actually ran at, self-reported. "
                                 "A bare level (`high`) applies to every lens named here; "
                                 "`<lens>=<level>` names one. Checked against the approved depth.")
    record_review.set_defaults(func=cmd_review_record)
    amend_review = review.add_parser(
        "amend", help="complete or correct the recorded review, until its first finding is dispositioned")
    amend_review.add_argument("plan")
    amend_review.add_argument("--lens", action="append",
                              help="a lens to add; repeatable. Omit to add findings for the recorded lenses.")
    amend_review.add_argument("--packet-digest", required=True,
                              help="must equal the recorded review's packet digest — a lens that read a "
                                   "different packet did not review the same plan")
    amend_review.add_argument("--findings", help="a JSON array of findings to ADD (never to replace)")
    amend_review.add_argument("--accept-effort-shortfall", action="store_true",
                            help="Record a panel that came in UNDER the approved depth, keeping the honest number. The gap is published in the pull request.")
    amend_review.add_argument("--delivered-effort", action="append",
                            help="The reasoning effort a reviewer actually ran at, self-reported. "
                                 "A bare level (`high`) applies to every lens named here; "
                                 "`<lens>=<level>` names one. Checked against the approved depth.")
    amend_review.add_argument("--reason", required=True, help="why this review is being completed now")
    amend_review.set_defaults(func=cmd_review_amend)

    finding = sub.add_parser("finding", help="adjudicate review findings").add_subparsers(
        dest="finding_command", required=True)
    dispose = finding.add_parser("dispose", help="record how one finding was answered")
    dispose.add_argument("plan")
    dispose.add_argument("--id", required=True)
    dispose.add_argument("--disposition", required=True,
                         choices=["accepted-fixed", "accepted-tracked", "partially-accepted",
                                  "rejected", "escalated"])
    dispose.add_argument("--rationale", required=True)
    # A stated choice, never a default. An omitted flag used to resolve to False, so a session that
    # simply forgot recorded the finding as not holding the pull request — a submission gate failing
    # toward permitting, which is exactly what the Build side's own `finding record` rejects. The
    # consts make "said nothing" distinguishable from "said no".
    blocking = dispose.add_mutually_exclusive_group()
    blocking.add_argument("--blocks-this-pr", action="store_const", const=True, dest="blocks_this_pr_stated",
                          help="this finding still blocks the pull request the plan authorizes")
    blocking.add_argument("--does-not-block-this-pr", action="store_const", const=False,
                          dest="blocks_this_pr_stated",
                          help="dispositioned and not blocking")
    dispose.add_argument("--operator-summary",
                         help="The operator-safe sentence published on the merge surface. Required when a "
                              "BLOCKING finding is not left blocking.")
    dispose.set_defaults(func=cmd_finding_dispose)
    amend_finding = finding.add_parser(
        "amend", help="correct one finding as recorded, until it is dispositioned")
    amend_finding.add_argument("plan")
    amend_finding.add_argument("--id", required=True)
    amend_finding.add_argument("--severity", choices=["blocking", "serious", "nit"])
    amend_finding.add_argument("--summary")
    amend_finding.add_argument("--lens")
    amend_finding.add_argument("--location")
    amend_finding.add_argument("--reason", required=True, help="why this finding is being corrected")
    amend_finding.set_defaults(func=cmd_finding_amend)

    seal = sub.add_parser("seal", help="the terminal act — nothing locks before it")
    seal.add_argument("plan")
    seal.add_argument("--delta-judgment", choices=["none", "scoped"])
    seal.add_argument("--delta-rationale")
    _consent_argument(seal, "seal")
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

    reopen = sub.add_parser("reopen", help="undo a retirement or abandonment (never completion or a seal)")
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
    clone.add_argument("--supersedes",
                       help="the plan this copy is being written to replace. The copy starts out "
                            "already belonging to the same program, already in the replaced plan's "
                            "place, and already owing what that place owed — so you edit the work "
                            "rather than reassemble the bookkeeping")
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
    except ProjectManagerError as exc:
        print(f"project-manager: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
