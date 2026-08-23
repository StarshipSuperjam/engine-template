"""Reviewer discovery, selective freshness, and finding disclosure for Build."""
from __future__ import annotations

from pathlib import Path

import build_coordinator_core as core


def installed(root: Path, stage: str) -> list[dict]:
    role = "plan-review" if stage == "plan" else "pre-submission-review"
    found: dict[str, dict] = {}
    for path in sorted((root / ".claude" / "agents").glob("*.md")):
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            continue
        front = text.split("---\n", 2)[1]
        fields = {}
        for line in front.splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                fields[key.strip()] = value.strip()
        if fields.get("role") == role and fields.get("lens"):
            lens = fields["lens"]
            if lens in found:
                raise core.CoordinatorError(f"more than one installed reviewer declares lens {lens}")
            found[lens] = {
                "lens": lens,
                "path": str(path.relative_to(root)),
                "digest": core.digest(path.read_bytes()),
            }
    return [found[lens] for lens in sorted(found)]


def required(protocol: dict, stage: str, depth: str, roster: list[dict]) -> list[dict]:
    table = protocol["plan_review" if stage == "plan" else "deliverable_review"]
    allowed = {item["lens"] for item in roster} if depth == "thorough" else set(table[depth])
    return [item for item in roster if item["lens"] in allowed]


DEPTH_ORDER = ("quick", "standard", "thorough")
_EFFORT_RANK = {None: -1, "low": 0, "medium": 1, "high": 2}


def available_depths(protocol: dict, plan_roster: list[dict], deliverable_roster: list[dict],
                     efforts: dict) -> list[str]:
    """Which review depths the consent surface should OFFER, so the operator is never asked to choose a depth
    that buys nothing (StarshipSuperjam/engine-template#763, generalized under StarshipSuperjam/engine-template#677). A depth is offered when,
    versus the last offered lighter depth, it runs AT LEAST ONE lens the lighter one does not, OR the SAME
    non-empty lens-set at HIGHER effort; empty-vs-empty never distinguishes, so with no installed reviewers only
    `quick` is offered (the operator's own read plus the automatic checks). Keyed on the set DIFFERENCE, not a
    strict superset, so a depth that runs genuinely unique coverage is offered even if the lens tables are ever
    non-monotonic (a heavier depth that both adds and drops a lens still surfaces its addition, rather than being
    silently hidden). `efforts` maps each depth to its resolved effort (None where the depth runs no reviewers).
    `quick` is always offered — it is the floor. Advisory only: this shapes what the operator is shown;
    `required()` remains the sole mechanical lens authority, and a collapsed depth, if bound anyway, still
    resolves to the same empty roster as quick."""
    offered: list[str] = []
    last: tuple[frozenset, str | None] | None = None
    for depth in DEPTH_ORDER:
        lenses = (frozenset(i["lens"] for i in required(protocol, "plan", depth, plan_roster))
                  | frozenset(i["lens"] for i in required(protocol, "deliverable", depth, deliverable_roster)))
        effort = efforts.get(depth)
        if last is None:
            offered.append(depth)
            last = (lenses, effort)
            continue
        last_lenses, last_effort = last
        adds_lenses = bool(lenses - last_lenses)
        same_nonempty_higher_effort = (
            lenses == last_lenses and bool(lenses)
            and _EFFORT_RANK.get(effort, -1) > _EFFORT_RANK.get(last_effort, -1))
        if adds_lenses or same_nonempty_higher_effort:
            offered.append(depth)
            last = (lenses, effort)
    return offered


def lens_packet_digest(referent_digest: str, contract: dict) -> str:
    return core.digest({"referent_digest": referent_digest, "reviewer_contract": contract})


def lens_packets(referent_digest: str, contracts: list[dict]) -> list[dict]:
    return [
        {**contract, "lens_packet_digest": lens_packet_digest(referent_digest, contract)}
        for contract in contracts
    ]


def current_receipt_lenses(stage: dict) -> set[str]:
    expected = {item["lens"]: item["lens_packet_digest"] for item in stage.get("reviewer_contracts", [])}
    return {
        receipt["lens"]
        for receipt in stage["receipts"]
        if receipt.get("lens_packet_digest") == expected.get(receipt["lens"])
    }


def missing_receipts(stage: dict) -> list[str]:
    done = current_receipt_lenses(stage)
    return [item["lens"] for item in stage.get("reviewer_contracts", []) if item["lens"] not in done]


def live_receipts(state: dict) -> list[tuple[str, dict]]:
    """Every review receipt currently live anywhere in the Build, each paired with the stage that PRODUCED
    it -- the one home for that classification.

    A receipt does not record its own producing stage, so it has to be inferred: a receipt sitting in the
    deliverable stage whose packet digest is not the deliverable packet's was spliced there by a repair
    review (`cmd_review_record`). That inference used to live only inside `missing_findings`, while the
    complementary rule -- which findings SURVIVE a packet regeneration -- was reconstructed separately
    inside `_packet`'s closures, in another module. The two had to stay exact complements forever and
    nothing enforced it, so a deliverable regeneration dropped a spliced repair receipt while leaving its
    findings behind: orphaned findings no receipt demanded, still counting toward `blocks_this_pr` and
    still rendering disagreement lines into the PR body. Deriving both the demand and the survival set
    from this one function is what keeps them from drifting apart
    (StarshipSuperjam/engine-template#1051)."""
    found = []
    for stage_name, stage in state["reviews"].items():
        for receipt in stage["receipts"]:
            produced_by = ("repair" if stage_name == "deliverable"
                           and receipt["packet_digest"] != stage["packet_digest"] else stage_name)
            found.append((produced_by, receipt))
    if state["repair"]:
        for receipt in state["repair"]["receipts"]:
            found.append(("repair", receipt))
    return found


def _finding_key(stage: str, lens: str, packet_digest: str, lens_packet_digest, commit) -> tuple:
    return (stage, lens, packet_digest, lens_packet_digest, commit)


def demanded_findings(state: dict) -> dict[str, set]:
    """finding id -> EVERY key that would satisfy it, across all live receipts naming that id.

    A set rather than one key: `state["findings"]` holds at most one record per id, so if two live receipts
    name the same id a single-key map would let the last one iterated win -- silently dropping the other
    receipt's demand and, because the survival set reads the same map, deleting an already-recorded
    disposition at the next packet regeneration. Matching ANY live demand keeps one honest record able to
    satisfy every receipt that asked for it, instead of trading a loud wedge for quiet evidence loss."""
    demanded: dict[str, set] = {}
    for produced_by, receipt in live_receipts(state):
        key = _finding_key(produced_by, receipt["lens"], receipt["packet_digest"],
                           receipt.get("lens_packet_digest"), receipt["commit"])
        for finding_id in receipt["finding_ids"]:
            demanded.setdefault(finding_id, set()).add(key)
    return demanded


def finding_is_demanded(finding: dict, demanded: dict[str, set]) -> bool:
    keys = demanded.get(finding["id"])
    return bool(keys) and _finding_key(
        finding["stage"], finding["lens"], finding["packet_digest"],
        finding.get("lens_packet_digest"), finding["commit"]) in keys


def surviving_findings(state: dict) -> list[dict]:
    """The findings a live receipt still demands. Applied after a packet regeneration so a finding lives
    exactly as long as the receipt referencing it -- never orphaned, never stranded."""
    demanded = demanded_findings(state)
    return [f for f in state["findings"] if finding_is_demanded(f, demanded)]


def missing_findings(state: dict) -> list[str]:
    demanded = demanded_findings(state)
    actual = state["findings"]
    return sorted(finding_id for finding_id in demanded if not any(
        finding["id"] == finding_id and finding_is_demanded(finding, demanded) for finding in actual))


def plan_change_escalation(state: dict) -> dict | None:
    """The operator's recorded authorization to ship the CURRENT plan digest without re-reviewing the delta,
    or None. Single-homed because two readers enforce on it -- the status render and the checkpoint/validate
    gate -- and when only one of them knew, status reported plan review satisfied while the gate refused,
    wedging the Build into a forced re-panel of a plan the operator had already settled."""
    for item in state.get("plan_change_escalations", []):
        if item["plan_digest"] == state["plan"]["digest"]:
            return item
    return None


def plan_review_ready(state: dict, plan: dict) -> tuple[bool, list[str]]:
    if plan["profile"] == "trivial" and (state.get("approval") or {}).get("depth") == "quick":
        return True, []
    stage = state["reviews"]["plan"]
    waiver = stage.get("waiver")
    if waiver and state.get("approval") and waiver["plan_digest"] == state["plan"]["digest"] \
            and waiver["depth"] == state["approval"]["depth"]:
        return True, []
    if plan_change_escalation(state):
        return True, []
    missing = []
    if not stage.get("referent_digest") and not stage.get("packet_digest"):
        missing.append("plan-review packet")
    missing.extend(f"plan-review receipt: {lens}" for lens in missing_receipts(stage))
    plan_receipts = {receipt["lens"]: receipt for receipt in stage["receipts"]}
    for receipt in plan_receipts.values():
        for finding_id in receipt["finding_ids"]:
            if not any(f["id"] == finding_id and f["stage"] == "plan" and f["lens"] == receipt["lens"]
                       and f["packet_digest"] == receipt["packet_digest"]
                       and f.get("lens_packet_digest") == receipt.get("lens_packet_digest")
                       for f in state["findings"]):
                missing.append(f"finding disposition: {finding_id}")
    blocking = [f["id"] for f in state["findings"] if f["stage"] == "plan" and f["blocks_this_pr"]]
    missing.extend(f"plan finding still blocks: {finding_id}" for finding_id in blocking)
    return not missing, missing


def disagreement_line(finding: dict) -> str:
    # Only the operator-safe summary reaches this line — never `private_reference`, which is
    # reviewer-internal detail (StarshipSuperjam/engine-template#981). `operator_summary` is required
    # on exactly these downgraded-blocking findings (see cmd_finding_record), so on the normal path it
    # is present. This line is published verbatim into the PR body AND asserted present by the
    # pr-contract preflight, so the redaction lives here — the single source both derive from. A legacy
    # or hand-edited finding could still carry a null summary; render a legible placeholder rather than
    # a dangling colon, and never fall back to the private text.
    summary = finding.get("operator_summary") or "[no operator-safe summary recorded]"
    return f"- Reviewer disagreement `{finding['id']}`: {summary}"


def required_disagreement_lines(state: dict) -> list[str]:
    return [
        disagreement_line(finding)
        for finding in state["findings"]
        if finding["severity"] == "blocking" and not finding["blocks_this_pr"]
    ]
