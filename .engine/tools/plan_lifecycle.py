#!/usr/bin/env python3
"""When a plan's recorded artifacts stop being correctable, and what the operator must attest to.

Two rules live here, and they are here rather than scattered through the verbs because both are
answers to the same question — "may this be changed now?" — and two implementations of that question
drift into disagreeing about a record an operator is relying on.

FREEZE MOMENTS. Recording is a fallible act. A lens set is mistyped, a receipt is recorded against
the wrong packet, a finding's severity is entered wrong. Until 2026-08-25 every one of those was
permanent: the review slot is single-minted, so a partial record wasted it, and recovery meant
hand-editing the store. That made ordinary typing mistakes seal-grade, which is a far worse property
than the correction risk it was avoiding. So each artifact is correctable until a specific, named
moment, and the moments are chosen so that a correction can never rewrite something a later decision
was made ON TOP OF:

  - a FINDING freezes when it is dispositioned — the disposition is a judgment about that finding as
    written, so changing the finding afterwards would silently re-aim the judgment;
  - the REVIEW RECORD freezes when its first finding is dispositioned — from then on the review is
    being adjudicated, and its lens set and packet digest are what the adjudication assumed;
  - the APPROVAL freezes when any review is recorded against it, which is what PINS THE DEPTH from
    panel time: the panel ran the roster the depth demanded, and a depth amended afterwards would
    move the seal's coverage question away from what actually ran;
  - and NOTHING is correctable once the plan is sealed. The seal is terminal; that is not softened
    here, and the amendment path deliberately stops at the recording layer.

CONSENT POINTS. On 2026-08-25 a session drove a plan from authoring to a bound pull request —
approval, a four-lens panel, twenty-one findings, dispositions, a fix revision and a seal — in
thirty-two minutes with no operator input at all. Nothing malfunctioned. The consent points were
prose in a runbook, and prose is advice a session may follow.

So approve, seal and bind each refuse without a recorded operator decision at that gate, and the
seal additionally refuses until the panel's outcome was presented to the operator and that
presentation recorded. Each gate also checks that the gate before it left its record: the seal
looks for the approval's, the bind for the seal's. The record is an EVENT — the gate and the
moment — never the operator's words. Until 2026-09-05 each gate demanded the operator's actual
words and the pull request republished them; a quotation typed by the session proves nothing, so
that ceremony was retired (StarshipSuperjam/engine-template#1125) and every refusal kept. Records
written before then may still carry the text; nothing reads or renders it.

What this is NOT: proof. A recorded decision is a session's record that the operator was asked, and
a session that would fabricate one could. What it changes is the shape of the failure: a gate that
was never crossed is a missing record rather than an omission nobody can see. Un-forgeability needs
an identity the session cannot mint, which is issue 914's residual and is not claimed here. The
trail stays in the plan record, where `plan show` lists it; it is not published in the pull request.
"""
from __future__ import annotations

import build_coordinator_core as core
import plan_store

PlanLifecycleError = core.CoordinatorError

# The statuses after which a review depth can no longer be chosen. Enumerated NEGATIVELY on purpose:
# a status this list does not know is treated as pre-seal, which fails open to one harmless extra
# preview marker, where a positive pre-seal list would fail closed to an approve nobody can reach.
_AFTER_THE_SEAL = ("sealed", "active", "complete", "retired", "abandoned")


def depth_choice_closed(record: dict) -> str | None:
    """Why a review depth can no longer be chosen for this plan, or None while it still can.

    ONE predicate for the preview marker and BOTH of its readers (StarshipSuperjam/engine-template#1108).
    `preview` drops a marker to prove the operator was shown the revision a depth is about to be chosen
    for; `depths` and `approve` refuse without it and tell the session to run preview. That remedy is
    only honest while a preview can still mark — and it must not mark once the plan is sealed, bound to
    a Build, or closed, because inspecting terminal history is a read, not a lifecycle step, and a
    sandbox that cannot write the library must still be able to render it. The boundary is the SEAL
    (plus binding and closure), not the approval: an approved-but-unreviewed plan may still be
    re-approved at another depth, and its marker can be lost in transport (a bundle carries the record
    and revisions, never the marker), so preview has to be able to re-mark it. Every verb that reads
    the marker asks this predicate FIRST, so a post-boundary plan is refused with its real state and
    the real way on — never with an instruction to run a preview that will never mark again. Derived
    from the store's own status so it cannot drift from what `list` and `show` say.
    """
    status = plan_store.derived_status(record)
    if status not in _AFTER_THE_SEAL:
        return None
    if status == "sealed":
        return (f"this plan is sealed at revision {record['seal']['revision']}, and a seal is terminal: a "
                "review depth is chosen before the seal, never after it. Clone the plan to choose again.")
    if status == "active":
        return ("a Build is bound to this plan, so a review depth can no longer be chosen for it — the "
                "depth approved before the seal is the one that Build runs at. Let that Build run, or "
                "clone the plan to plan the work differently.")
    if status == "complete":
        return ("this plan is complete, and completed Build history is terminal: nothing about it can be "
                "approved again. Start a new plan for new work.")
    reason = record["closure"].get("reason", "")
    return (f"this plan is {status}" + (f" ({reason})" if reason else "") + ", so a review depth cannot be "
            "chosen for it as it stands. Reopen it first (`reopen`); the choice starts over from there.")

# The gates that require an operator attestation, and what each one is consent TO.
GATES = {
    "approve": "approving this plan and choosing the review depth its panel will run",
    "findings-presented": "being shown the panel's outcome — its findings and their dispositions — "
                          "before the plan locks",
    "seal": "sealing this plan, which is terminal, and authorising the Build it enters",
    "bind": "starting the Build that executes this sealed plan",
}


# --- freeze moments -----------------------------------------------------------

def _sealed_reason(record: dict) -> str | None:
    seal = record.get("seal")
    if not seal:
        return None
    return (f"this plan was sealed at revision {seal['revision']}, and a seal is terminal — it freezes "
            "the plan's text, its review, and its dispositions together, because the pull request "
            "publishes them as they stood when you sealed. Clone the plan to work on it further.")


def frozen_reason(record: dict, artifact: str, *, finding_id: str | None = None) -> str | None:
    """Why `artifact` may no longer be corrected, or None if it still may be.

    `artifact` is one of "approval", "plan_review", "finding". Returning a SENTENCE rather than a
    boolean is deliberate: every caller of this refuses with it, and a shared boolean would leave
    each verb inventing its own explanation of a rule it does not own.
    """
    sealed = _sealed_reason(record)
    if sealed:
        return sealed
    review = record.get("plan_review") or {}
    findings = review.get("findings", [])
    dispositioned = [f for f in findings if f.get("disposition")]

    if artifact == "approval":
        if review:
            return (f"a cold review was recorded against this approval on {review['at']}, which pins the "
                    f"approved depth from the moment the panel ran. The panel read the roster "
                    f"{record['approval']['depth']} demanded; amending the depth now would move the "
                    "seal's coverage question away from what actually ran. Clone the plan to choose a "
                    "different depth.")
        return None

    if artifact == "plan_review":
        if dispositioned:
            first = dispositioned[0]
            return (f"{first['id']} has already been dispositioned, so this review is being adjudicated "
                    "and its lens set and packet digest are what that adjudication assumed. Amend a "
                    "review before dispositioning any of its findings.")
        return None

    if artifact == "finding":
        match = [f for f in findings if f["id"] == finding_id]
        if not match:
            known = ", ".join(f["id"] for f in findings) or "none"
            raise PlanLifecycleError(f"no finding {finding_id!r} in this review; it holds: {known}")
        if match[0].get("disposition"):
            return (f"{finding_id} was dispositioned as {match[0]['disposition']}, and that judgment was "
                    "made about the finding as written. Changing the finding now would silently re-aim "
                    "it. Record a fresh finding if the review missed something.")
        return None

    raise PlanLifecycleError(f"unknown plan artifact {artifact!r}")


# --- consent attestations -----------------------------------------------------

def attestation(gate: str, *, at: str) -> dict:
    """One recorded operator decision at one gate: the gate and the moment, nothing else."""
    if gate not in GATES:
        raise PlanLifecycleError(f"unknown consent gate {gate!r}; expected " + ", ".join(sorted(GATES)))
    if not (at or "").strip():
        raise PlanLifecycleError(f"the {gate} gate's record needs the moment it was crossed")
    return {"gate": gate, "at": at}


def consent_for(record: dict, gate: str) -> dict | None:
    """The most recent attestation at `gate`, or None."""
    matches = [c for c in record.get("consent", []) if c["gate"] == gate]
    return matches[-1] if matches else None


def missing_consent(record: dict, gate: str) -> str | None:
    """The refusal for a gate with no attestation, or None when one is recorded.

    Named separately from `attestation` because the refusal is what a reader of a verb needs to
    understand, and it should say what the operator is being asked rather than name a missing field.
    """
    if consent_for(record, gate):
        return None
    return (f"there is no recorded operator decision for this gate, so nothing shows the operator was "
            f"asked. This gate is consent to: {GATES[gate]}. Ask, then record their answer with "
            "--operator-decided. The record is the gate and the moment — a record, not a proof — and "
            "it stays in the plan record, where `plan show` lists it.")


def consent_lines(record: dict) -> list[str]:
    """The recorded decisions, one plain line per gate and moment, for `plan show`.

    Nothing here is rendered into the pull request any more, and nothing the operator typed is
    rendered anywhere: the entry is an event. A pre-2026-09-05 entry may carry a `decision` text; it
    is not shown, because a quotation the session typed is not evidence of anything."""
    return [f"{entry['gate']} at {entry['at']}" for entry in record.get("consent", [])]


# The gate each gate looks back to: a seal without an approval's record, or a bind without a
# seal's, is a chain with a link missing — a record a tool did not write, whatever else it says.
PRIOR_GATE = {"seal": "approve", "bind": "seal"}


def missing_prior_consent(record: dict, gate: str) -> str | None:
    """The refusal when the gate before `gate` left no record, or None."""
    prior = PRIOR_GATE.get(gate)
    if not prior or consent_for(record, prior):
        return None
    return (f"the {gate} gate needs the {prior} gate's recorded decision first, and this plan carries "
            f"none: nothing shows the operator was asked to consent to {GATES[prior]}. The tool that "
            "owns that gate did not write its record; run it again before this one.")


# One sentence, rendered by the seal hand-back and the bind alike, so the rule that no Issue is filed
# for work already in flight arrives from the ceremony itself rather than from anyone's memory
# (StarshipSuperjam/engine-template#1091). An Issue is intake; a Build's work is carried by its draft
# pull request.
CARRIER_RULE = "The Build's work rides its draft pull request; no Issue is filed for it."


# --- the two finding shapes ---------------------------------------------------

_RECORD_SHAPE = ("id", "lens", "severity", "summary")
_PERSONA_SHAPE = ("severity", "message", "location")


def translate_findings(raw, *, lenses: list) -> list:
    """Accept either finding shape the ceremony actually produces, and map the reviewer's one.

    There are two, and until this existed the mismatch was silent-then-fatal: the four plan-review
    personas report on `plan-review-finding.v1` — a severity, a message and a location, with no id
    and no lens, because a reviewer does not name itself — while the plan record stores an id, a
    lens, a severity and a summary. A session that piped one into the other got a schema refusal at
    the end of a panel it had just paid for.

    So the mapping is made explicit and mechanical: `message` becomes the summary, `location` is
    preserved rather than dropped (it is the most useful part of a finding and the record now has a
    field for it), the lens is the ONE lens being recorded — a persona finding cannot carry a lens
    it never states — and the id is minted from that lens plus its position. A mixed or unrecognised
    batch is refused naming BOTH contracts and the mapping, rather than reported as a schema error
    about a field the author never chose.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PlanLifecycleError("findings must be a JSON array")
    if not raw:
        return []

    def shape_of(entry):
        if not isinstance(entry, dict):
            return None
        if all(key in entry for key in _RECORD_SHAPE):
            return "record"
        if all(key in entry for key in _PERSONA_SHAPE):
            return "persona"
        return None

    shapes = {shape_of(entry) for entry in raw}
    if None in shapes or len(shapes) > 1:
        raise PlanLifecycleError(
            "these findings are not in one recognised shape. Two are accepted, and a batch must be "
            "entirely one of them:\n"
            "  - the record shape: id, lens, severity, summary (plus optional disposition, "
            "rationale, operator_summary, blocks_this_pr, location)\n"
            "  - the reviewer shape (plan-review-finding.v1), which the four plan-review personas "
            "emit: severity, message, location\n"
            "The reviewer shape is mapped for you — message becomes the summary, location is kept, "
            "the lens is the one lens being recorded, and the id is minted as <LENS>-<n>. Fix the "
            "batch so every entry is one shape, or record each lens's findings in its own call.")
    if shapes == {"record"}:
        return [dict(entry) for entry in raw]

    if len(lenses) != 1:
        raise PlanLifecycleError(
            "reviewer-shaped findings (plan-review-finding.v1) carry no lens of their own, so they can "
            f"only be recorded for ONE lens at a time — this call names {len(lenses)}: "
            + ", ".join(lenses) + ". Record each lens's findings with its own --lens, or convert the "
            "batch to the record shape, which states each finding's lens outright.")
    lens = lenses[0]
    prefix = "".join(word[0] for word in lens.replace("_", "-").split("-") if word).upper() or "F"
    translated = []
    for index, entry in enumerate(raw, start=1):
        translated.append({
            "id": f"{prefix}-{index}",
            "lens": lens,
            "severity": entry["severity"],
            "summary": entry["message"],
            "location": entry["location"],
        })
    return translated
