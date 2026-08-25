#!/usr/bin/env python3
"""A small instrument panel for one PR-shaped Build.

The coordinator stores current mechanical evidence in one atomic local snapshot. It never chooses a plan,
finding remedy, operator escalation, or re-review depth, and it has no merge operation.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
from typing import Any

import build_coordinator_contract as composer  # aliased 'composer', not 'contract': this file uses the bare
# name 'contract' as a local for the reviewer-contract dict and the pr-contract state, and a module alias would
# be a shadowing landmine (a future use before the local assignment would raise UnboundLocalError).
import build_coordinator_core as core
import build_coordinator_dag as dag
import build_coordinator_github as github
import build_coordinator_review as review
import build_coordinator_spec as spec_service
import build_coordinator_work as work
import build_review_range as ranges
import build_state_store
import repo_identity
import review_integrity

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_PATH = ROOT / ".engine" / "build-protocol.json"
BINDINGS_PATH = ROOT / ".engine" / "policies" / "model-bindings.json"
PLAN_SCHEMA_V2 = ROOT / ".engine" / "schemas" / "build-plan.v2.json"
STATE_SCHEMA_V2 = ROOT / ".engine" / "schemas" / "build-state.v2.json"
HANDOFF_SCHEMA_V2 = ROOT / ".engine" / "schemas" / "build-handoff.v2.json"
# schema_version -> the schema file that validates a document carrying it. ONE generation. The maps
# stay maps because a future v3 should be a one-line addition rather than a fork, but there is no
# longer a v1 entry to fall back to, and nothing here defaults an absent version: a document that
# does not say what it is is refused by name (`_state_schema_for`, `dag.plan_version`), because
# guessing v1 for a versionless document is how an unreadable file became a silently-misread one.
PLAN_SCHEMAS = {"build-plan.v2": PLAN_SCHEMA_V2}
STATE_SCHEMAS = {"build-state.v2": STATE_SCHEMA_V2}
HANDOFF_SCHEMAS = {"build-handoff.v2": HANDOFF_SCHEMA_V2}
# The registered validation commands (id, operator label, argv) are declared in build-protocol.json, so
# both execution (cmd_validate) and PR rendering (the contract composer's Validation section) read one source.


CoordinatorError = core.CoordinatorError
_json = core.json_file
_input = core.input_text
_validate = core.validate

# The recurring reminder shown on every coordinator command a session runs mid-Build (status, checkpoint):
# the coordinator owns this PR's workflow, so it must reach ready THROUGH the submit gate, not a bare
# `gh pr ready` (StarshipSuperjam/engine-template#1014). It is a soft nudge — the operator's merge stays the
# binding gate — pairing with the durable 'engine-coordinator-owned' PR label applied at bind.
_COORDINATOR_OWNED_REMINDER = ("Coordinator-owned: reach ready only through 'submit apply' (never a bare "
                               "'gh pr ready'); the tail is contract apply -> preflight -> submit apply.")
_canonical = core.canonical
_digest = core.digest


# The plan document's schema version. Re-exported from the pure layer so the version rule has one
# home now that the Project Manager reads it too.
_plan_version = dag.plan_version

# `checkpoint --complete-item` is GONE with the v1 sunset. A node's completion is earned at `work
# integrate`, which records the integration commit BC-27 requires; the flag wrote the same progress
# entry with no such evidence, and it survived only because v1 — which had no other completion path —
# still existed. With v1 deleted it has no honest caller left, and a flag whose only remaining use
# would be to bypass the graph's completion rule is removed rather than guarded.

# The mid-flight refusal. A snapshot written before this change can carry v2 completions that no
# integration ever earned; every gate reading them would be reading a bypass. The remedy is to earn or
# withdraw them — never to rebind, which on a live draft PR discards the Build's whole evidence trail.
_UNEARNED_COMPLETION_REMEDY = (
    "earn each one with `work integrate --item <id> --attempt <attempt> --commit <sha>` (claim the node "
    "first if no attempt returned), or `work reject`/`work abandon` the node and let the graph re-derive "
    "it. Do NOT rebind the plan: a rebind on a live Build discards the recorded evidence rather than "
    "repairing it.")


def _unearned_completions(state: dict) -> list[str]:
    """Work-item ids recorded complete on a v2 snapshot without the integration evidence BC-27 requires.

    Read-only and plan-free, so every caller — the two gates and the status render — asks the same
    question of the same field. Empty for a v1 snapshot: there `progress.completed` IS the completion
    record and has no integration to disagree with.
    """
    if state.get("schema_version") != "build-state.v2":
        return []
    unearned = []
    for entry in state["progress"]["completed"]:
        integration = ((state.get("work") or {}).get(entry["id"]) or {}).get("integration") or {}
        if integration.get("commit") != entry.get("commit"):
            unearned.append(entry["id"])
    return unearned


def _plan(path: str) -> dict:
    """Read a Build plan document from `path` and validate it.

    The reading is this module's job; the JUDGING is not. Every rule about what makes a payload
    valid lives in dag.validate_plan_document, which plan_contract also calls before a plan may be
    sealed — so a sealed plan and the bind that consumes it can never disagree about validity.
    """
    try:
        value = json.loads(_input(path))
    except ValueError as exc:
        raise CoordinatorError(f"the Build plan is not valid JSON: {exc}") from exc
    dag.validate_plan_document(value, PLAN_SCHEMAS)
    return value


_criterion = spec_service.criterion


def _canonical_spec(plan: dict, *, repository: str | None = None, check_issue: bool = True) -> dict:
    return spec_service.canonical_spec(
        ROOT, plan, repository=repository, check_issue=check_issue,
        issue_body=_issue_body,
    )


def _assert_spec_current(state: dict, plan: dict, *, check_issue: bool = False) -> dict:
    canonical = _canonical_spec(plan, repository=state["build"]["repository"], check_issue=check_issue)
    approved = state["plan"].get("spec_digest")
    if state.get("approval") and canonical["digest"] != approved:
        raise CoordinatorError("settled specification changed since approval; revise and reapprove the plan")
    return canonical


def _assert_spec_boundary(state: dict, plan: dict, *, allow_same_session_offline: bool = False) -> dict:
    try:
        return _assert_spec_current(state, plan, check_issue=True)
    except CoordinatorError as exc:
        message = str(exc).lower()
        offline = any(token in message for token in ("network", "offline", "timed out", "could not resolve host", "gh issue view failed"))
        if allow_same_session_offline and state["build"]["mode"] == "same-session" and offline:
            return _assert_spec_current(state, plan, check_issue=False)
        raise


def _hard_check_declarations() -> list[dict]:
    return spec_service.hard_check_declarations(ROOT)


def _protocol() -> dict:
    """Load only the current runtime protocol.

    Historical preservation traceability is validated by Engine-home checks, not by
    normal Build commands.
    """
    value = _json(PROTOCOL_PATH)
    _validate(value, ROOT / ".engine" / "schemas" / "build-protocol.v1.json")
    return value


def _run(argv: list[str], *, cwd: Path = ROOT, input_text: str | None = None) -> subprocess.CompletedProcess:
    return core.run(argv, root=cwd, input_value=input_text)


def _run_validation(command: list[str], log_path: Path) -> int:
    return core.run_validation(command, log_path, root=ROOT)


def _must_run(argv: list[str], *, input_text: str | None = None) -> str:
    return core.must_run(argv, root=ROOT, input_value=input_text)


def _gh_json(argv: list[str]) -> Any:
    return github.gh_json(ROOT, argv)


def _head() -> str:
    return core.head(ROOT)


def _base() -> str:
    return core.base(ROOT)


def _verify_draft(repo: str, pr: int) -> dict:
    return github.verify_draft(ROOT, repo, pr)


def _state_schema_for(state: dict) -> Path:
    """Select the snapshot schema from the document's own version. Absent is REFUSED, never defaulted.

    Defaulting was the v1-era behaviour and it is exactly what the sunset removes. A snapshot that
    does not say what it is would have been read as v1 and validated against a schema it was never
    written to — quietly, and against a schema that no longer exists.
    """
    version = state.get("schema_version")
    if not version:
        raise CoordinatorError(
            "this Build snapshot does not state a schema_version, so there is no way to know what it "
            "is. Nothing is assumed: expected " + " or ".join(sorted(STATE_SCHEMAS)) + ".")
    schema = STATE_SCHEMAS.get(version)
    if schema is None:
        raise CoordinatorError(
            f"unrecognized Build snapshot version {version!r}; expected "
            + " or ".join(sorted(STATE_SCHEMAS)))
    return schema


class StateStore(core.StateStore):
    """A snapshot at a path the caller named with --state. The escape hatch and the tests' store."""

    def __init__(self, path: str, expected_revision: int | None = None):
        super().__init__(path, _state_schema_for, expected_revision)


# Either store, wherever a command only needs "the snapshot for this Build". They are peers with one
# shared discipline (core.RevisionedStore) and a genuinely different home, and no command below cares
# which one it got — which is the point of the split being in addressing rather than in behaviour.
Snapshot = core.RevisionedStore


def _resolve_store(args) -> Snapshot:
    """Which snapshot this command acts on: the one named, or the one bound to this worktree.

    `--state` still means exactly what it always meant — this file, no lookup — and it wins, because
    a caller who named a path is answering the question this function otherwise has to infer. What
    changed is the default. Before, omitting `--state` was an error, because there was nowhere else a
    snapshot could be; now the durable snapshot lives with the plan that bound it, and the worktree
    the session is standing in is what names it. That is the whole restart story: a session that
    comes back to the same worktree finds the same evidence, with nothing to have remembered.
    """
    if args.state:
        return StateStore(args.state, args.expect_revision)
    library = _library()
    return build_state_store.resolve_for_worktree(
        ROOT, _state_schema_for, args.expect_revision, library=library)


def _empty_review() -> dict:
    return {"packet_digest": None, "referent_digest": None, "required_lenses": [], "installed_lenses": [],
            "reviewer_contracts": [], "receipts": [], "reviewed_commit": None, "base_commit": None}


# After this many repair rounds on one deliverable, the next round stops for operator guidance. Two is
# the operator's chosen threshold: enough for an honest fix-and-verify cycle plus one follow-up, before
# a loop that is not converging keeps spending. It caps neither review coverage nor lens count.
_REPAIR_ROUND_ESCALATION = 2


def _initial_state(repo: str, pr: int, base: str, plan_id: str, sealed_digest: str, plan: dict,
                   issue: int | None, mode: str = "same-session") -> dict:
    state = {
        "schema_version": "build-state.v2", "revision": 1,
        # `worktree` is what lets a session that restarted find this snapshot again without having
        # remembered anything: it is standing in the worktree, and the durable store looks the Build
        # up by it. Recorded at bind because bind is the moment the two are genuinely bound.
        "build": {"repository": repo, "pr": pr, "base_at_bind": base, "mode": mode,
                  "worktree": str(ROOT)},
        # The plan of record is the sealed library plan, named by id and by the digest its seal minted.
        # `digest` remains the executed build payload's digest: equal to the sealed payload at bind, and
        # the thing every later `--plan` assertion is checked against.
        "plan": {"plan_id": plan_id, "sealed_digest": sealed_digest, "diverged_from_seal": False,
                 "digest": _digest(plan), "intent_digest": _digest(plan["raw_intent"].encode()),
                 "spec_digest": None, "authorizing_issue": issue, "profile": plan["profile"],
                 "bound_head": _head()},
        "approval": None, "reviews": {"deliverable": _empty_review()},
        "findings": [], "checkpoint": None, "progress": {"current_item": None, "completed": []},
        "validation": None, "repair": None,
        # Cost-cadence ledgers. Both are cross-revision by design and are carried through handoff:
        # a cap a cold resume silently zeroes is a cap with a published bypass.
        "repair_rounds": [], "plan_change_escalations": [], "reconciles": [],
        "preflights": [], "pr_contract": None, "submission": "draft",
        "checkout_snapshot": None
    }
    state["work"] = {}
    return state


def _assert_plan(state: dict, plan: dict) -> None:
    actual = _digest(plan)
    if actual != state["plan"]["digest"]:
        raise CoordinatorError(f"supplied plan digest {actual} does not match approved Build plan {state['plan']['digest']}")


def _issue_body(repo: str, issue: int) -> str:
    return github.issue_body(ROOT, repo, issue)


# The Issue-body plan block and its publication helpers are gone from this coordinator's surface: a plan
# now enters a Build only through a sealed plan in the local library, so nothing here writes a plan to
# GitHub or reads one back. The marker helpers themselves still live in build_coordinator_github, where
# the v1 sunset in the successor plan removes them together with the v1 schemas they serve.


def _installed() -> list[dict]:
    return review.installed(ROOT)


def _required(protocol: dict, depth: str, installed: list[dict]) -> list[dict]:
    return review.required(protocol, depth, installed)


def _missing_findings(state: dict) -> list[str]:
    return review.missing_findings(state)


def _stage_range(stage: dict, kind: str) -> tuple[str | None, str | None]:
    """The (base, tip) a stage's reviewers are being asked to read. The deliverable review reads the
    branch — base to reviewed commit; a repair review reads only the divergence the repair named."""
    if kind == "repair":
        return stage.get("reviewed_commit"), stage.get("final_commit")
    return stage.get("base_commit"), stage.get("reviewed_commit")


def _coverage(stage: dict, kind: str):
    """The carry-forward predicate for one stage: does a receipt's recorded range already contain every
    authored commit this stage asks about? Built here because it needs git and ROOT; consumed by the pure
    `build_coordinator_review` through injection, so that module keeps no repository knowledge."""
    base, tip = _stage_range(stage, kind)
    return lambda receipt: ranges.receipt_covers(ROOT, receipt, base, tip)


def _missing_receipts(stage: dict, kind: str = "deliverable") -> list[str]:
    return review.missing_receipts(stage, _coverage(stage, kind))


def _outstanding_repair_lenses(repair: dict | None) -> list[str]:
    """The repair lenses that still owe a read — the requested lenses minus those with a receipt that
    stands, whether it attests this packet or carries forward from a range that already covered it.
    Single-homed because the status render, the readiness predicate and `_repair_round_complete` must
    agree; when they disagreed, `status` reported a repair satisfied that the gate then refused."""
    if not repair:
        return []
    covers = _coverage(repair, "repair")
    standing = {r["lens"] for r in repair.get("receipts", []) if
                r.get("packet_digest") == repair.get("packet_digest") or covers(r)}
    return [lens for lens in repair.get("lenses", []) if lens not in standing]


_EFFORT_RANK = {"low": 0, "medium": 1, "high": 2}


def _depth_effort(depth: str) -> str | None:
    """The reasoning effort the approved review depth PROMISES its reviewers will run at, or None where
    the depth pins none. Read from the one bindings file through `agent_bindings`, never re-derived."""
    sys.path.insert(0, str(ROOT / ".engine" / "tools"))
    import agent_bindings
    return agent_bindings.depth_effort(depth, agent_bindings.load_bindings(str(ROOT)), str(ROOT))


def effort_shortfall(delivered: str | None, promised: str | None) -> bool:
    """True when a self-reported delivered effort is below what the depth promised.

    An UNRECORDED effort is not a shortfall — it is an unknown, and the disclosure says so rather than
    inventing a number. The measurement is self-reported throughout: the spawning session knows its own
    effort at panel time and the reviewer reports its own, and nothing here can independently verify
    either. Commit-bound reviewer attestations (StarshipSuperjam/engine-template#916) are the named
    residual; until they exist, every place this value is disclosed says who reported it."""
    if delivered is None or promised is None:
        return False
    return _EFFORT_RANK.get(delivered, -1) < _EFFORT_RANK.get(promised, -1)


CODE_EXECUTION_KINDS = ("none", "discarded-copy", "in-place")


def code_execution_disclosure(kinds: set) -> str:
    """The pull-request body's sentence about what reviewers did with the change's code.

    Three real behaviours, three values. The disclosure used to carry only two, so a lens that ran the
    suite IN THE OPERATOR'S OWN CHECKOUT was published as having used a throwaway copy — a materially
    different claim about what touched their project, and the reason B2 carried finding CO-1. A mixed
    panel says both rather than picking whichever is more comfortable."""
    ran_in_place = "in-place" in kinds
    ran_in_copy = "discarded-copy" in kinds
    if ran_in_place and ran_in_copy:
        return ("reviewers ran the change's code to judge it — one or more in a throwaway copy that never "
                "touched your project, and one or more directly in this checkout")
    if ran_in_place:
        return "a reviewer ran the change's code directly in this checkout to judge it"
    if ran_in_copy:
        return ("a reviewer ran the change's code in a throwaway copy to judge it — it never touched "
                "your project")
    return "no reviewer executed the change's code"


def _depth_effort_or_none(state: dict) -> str | None:
    """The approved depth's promised effort, or None where there is no depth or no promise to caveat."""
    depth = (state.get("approval") or {}).get("depth")
    try:
        return _depth_effort(depth) if depth else None
    except Exception:                                   # noqa: BLE001 — the disclosure above says so itself
        return None


def _plan_effort_lines(state: dict) -> list[str]:
    """What to say about the PLAN panel's effort, read from the sealed plan record.

    The plan side's `--accept-effort-shortfall` tells the session, in its own refusal text, that the gap
    it is accepting "publishes the gap in the pull request". Nothing published it. `delivered_efforts`
    and `effort_shortfall_accepted` were written onto the plan record and the only reader in the tree
    was the seal's own completeness check, which asserts the map is filled in and never that the level
    was met. So the honest exit built FOR the plan panel produced a body claiming the approved depth
    without qualification — the same failure the Build side closed, re-opened one component over by the
    escape valve added to close it.

    The depth is the plan record's own, not the Build's: the two are normally the same value through the
    sealed handoff, but the claim being qualified here is the plan panel's, and it is answerable to the
    approval the plan panel actually ran under."""
    record = _sealed_plan_record(state)
    review = (record or {}).get("plan_review") or {}
    delivered = review.get("delivered_efforts") or {}
    depth = ((record or {}).get("approval") or {}).get("depth")
    if not depth or not delivered:
        return []
    try:
        promised = _depth_effort(depth)
    except Exception:                                   # noqa: BLE001
        return [f"the effort the plan's approved `{depth}` depth promises could not be read, so nothing "
                "here checked what its panel delivered against it"]
    if not promised:
        return []
    under = sorted(f"{lens} ({effort})" for lens, effort in delivered.items()
                   if effort_shortfall(effort, promised))
    if not under:
        return []
    # An unacknowledged shortfall should be unreachable — the gate refuses one — but `review amend` can
    # bypass the refusal without setting the flag, so the two cases are told apart rather than assumed.
    acknowledged = ("and the session recorded that it proceeded knowing it"
                    if review.get("effort_shortfall_accepted")
                    else "and NO acknowledgement of that gap is recorded against the plan")
    return [f"the PLAN panel came in under the `{depth}` depth it was approved at, which promises "
            f"{promised}, {acknowledged}: " + ", ".join(under)
            + " (self-reported, and nothing here verifies it)"]


def _effort_shortfall_lines(state: dict) -> list[str]:
    """What to say about reviewer effort, from every panel this Build paid for.

    Reading only the receipts was a hole big enough to swallow the whole mechanism. A session can accept a
    known shortfall at panel spawn — the operator's own recorded choice — and every reviewer can then
    self-report meeting the depth, at which point the gap the operator asked to publish disappeared
    entirely. That is not a corner case: it happened on the first Build to use this, where the session
    spawned at `medium` and all five lenses reported `high`.

    The two claims can also disagree, and one of them must be wrong: on this runtime a reviewer inherits
    the spawning session's effort and cannot exceed it. Resolving that silently in the reviewer's favour
    is the more flattering reading, so it is the one not taken — the disagreement is stated and the
    operator decides what it is worth (StarshipSuperjam/engine-template#1067).

    EVERY PANEL, NOT JUST THE FIRST. A repair round is spawned with its own session effort onto the
    repair stage, and its receipts are then SPLICED into the deliverable stage — so reading the session
    from `reviews.deliverable` alone dropped the repair panel's accepted gap entirely and compared each
    spliced repair receipt against the wrong session's number. Each receipt is attributed to the panel
    that actually spawned it, which the repair stage's own receipt list still records."""
    lines = _plan_effort_lines(state)
    approval = state.get("approval") or {}
    depth = approval.get("depth")
    if not depth:
        return lines
    stage = state.get("reviews", {}).get("deliverable", {})
    repair = state.get("repair") or {}
    receipts = stage.get("receipts", [])
    try:
        promised = _depth_effort(depth)
    except Exception as exc:                            # noqa: BLE001
        # NOT silence. This function is the sole source of the merge-surface disclosure as well as of the
        # status line, and a status render that degrades quietly is a different thing from a pull-request
        # body that quietly drops a line this engine calls hard.
        return lines + [f"the effort the approved `{depth}` depth promises could not be read, so nothing "
                        f"here checked what the panel delivered against it: {exc}"]
    if not promised:
        return lines
    silent, overclaim = [], []
    session = stage.get("session_effort")
    repair_session = repair.get("session_effort")
    # A repair receipt lives in BOTH lists — spliced into the deliverable stage, and kept on the repair
    # stage that spawned it. That second list is what makes the attribution readable without stamping a
    # new field onto every receipt.
    repair_lenses = {receipt["lens"] for receipt in repair.get("receipts", [])}
    for label, panel_session, panel in (("this panel", session, stage),
                                        ("the repair panel", repair_session, repair)):
        if panel.get("effort_shortfall_accepted") or effort_shortfall(panel_session, promised):
            lines.append(
                f"{label} was spawned from a session reporting {panel_session or 'an unrecorded'} effort "
                f"against an approved `{depth}` depth that promises {promised}, and the session proceeded "
                "knowing it (self-reported, and nothing here verifies it)")
    for receipt in receipts:
        delivered = receipt.get("delivered_effort")
        spawning = repair_session if receipt["lens"] in repair_lenses else session
        if delivered is None:
            silent.append(receipt["lens"])
        elif effort_shortfall(delivered, promised):
            lines.append(f"reviewer effort below the approved depth (self-reported): {receipt['lens']} ran at "
                         f"{delivered}, and `{depth}` promises {promised}")
        elif spawning and effort_shortfall(spawning, delivered):
            # Collected, not repeated per lens: five near-identical sentences in a pull-request body is
            # how a real disclosure gets skimmed past. Grouped by the session each answers to, because a
            # repair round and the deliverable panel can report different ones.
            overclaim.append((spawning, f"{receipt['lens']} ({delivered})"))
    for spawning in sorted({entry[0] for entry in overclaim}):
        named = sorted(entry[1] for entry in overclaim if entry[0] == spawning)
        lines.append(
            "these reviewers report running ABOVE the session that spawned them, which is not possible "
            "here — a reviewer inherits the session's effort and cannot exceed it, so one of the two "
            "self-reports is wrong and neither is verified: " + ", ".join(named)
            + f" against a session reporting {spawning}")
    if silent:
        lines.append("these review receipts recorded no delivered effort, so the approved "
                     f"`{depth}` depth's promise of {promised} is unverified for them: " + ", ".join(sorted(silent)))
    return lines


# `_plan_review_ready` is gone, and with it the checkpoint and validate gates that called it. A Build
# can no longer begin against an unreviewed plan, because it can no longer BEGIN against an unsealed one:
# `plan bind` refuses anything without a seal, and a seal refuses a review that does not cover its
# approved depth. The gate did not weaken; it moved upstream of the Build entirely.


def _trivial_violations(state: dict, plan: dict) -> list[str]:
    if plan["profile"] != "trivial":
        return []
    violations = []
    if state["build"]["mode"] != "same-session":
        violations.append("the Build is no longer same-session")
    if len(plan["work_items"]) != 1:
        violations.append("the Build no longer has exactly one work item")
    paths = _changed_paths(state["build"]["base_at_bind"])
    sys.path.insert(0, str(ROOT / ".engine" / "tools"))
    import weakening_guard
    guarded = [path for path in paths if path.startswith(".engine/schemas/") or weakening_guard.is_guardrail(path)]
    if guarded:
        violations.append("guarded enforcement or schema surfaces changed: " + ", ".join(guarded))
    commit_count = int(_must_run(["git", "rev-list", "--count", f"{state['build']['base_at_bind']}..HEAD"]).strip())
    if commit_count > 1:
        violations.append(f"the Build has {commit_count} commits, not the one-commit fast path")
    return violations


def _next_incomplete(plan: dict, state: dict) -> str | None:
    """The single next work item the linear v1 order or the v2 DAG readiness would advance.

    v1 keeps its byte-identical linear scan; a v2 plan derives the next item from the graph's READY
    set, so status and checkpoint read one shared derivation rather than duplicating the scan.
    Deliberately ready_set, not claimable_set: a checkpoint records completion and reserves no worker
    slot, so a busy slot or a resource hold (which claimable_set subtracts) must not change which item
    is "next" to advance — only dependency readiness does.
    """
    if _plan_version(plan) == "build-plan.v2":
        return dag.next_ready(plan, state)
    ordered = [item["id"] for item in plan["work_items"]]
    completed = {item["id"] for item in state["progress"]["completed"]}
    return next((item for item in ordered if item not in completed), None)


def _work_projection(plan: dict, state: dict) -> dict:
    """The DAG status section for a v2 Build: ready/claimable sets, per-node state, capacity, holders."""
    lifecycle = dag.derive_lifecycle(plan, state)
    parallelism = plan.get("parallelism", {"mode": "serial", "max_concurrency": 1})
    nodes = {}
    for node_id, node in lifecycle.items():
        nw = (state.get("work") or {}).get(node_id) or {}
        claim = nw.get("claim") or {}
        integration = nw.get("integration") or {}
        result = nw.get("latest_result") or {}
        failure = nw.get("latest_failure") or {}
        nodes[node_id] = {
            "state": node["state"], "reasons": node["reasons"],
            "attempt_count": nw.get("attempt_count", 0),
            "route": claim.get("requested_route"),
            "integration_commit": integration.get("commit"),
            "focused_verification": integration.get("focused_verification"),
            "artifact_digest": result.get("artifact_digest"),
            "failure": {"class": failure.get("class"), "disposition": failure.get("disposition"),
                        "reason": failure.get("reason")} if failure else None,
        }
    # Computed once and threaded: the ranking is a pure function of the graph, and four derivations
    # below want the same answer.
    lengths = dag.critical_path_lengths(plan)
    rank = dag.admission_rank(plan, lengths)
    admission = dag.admission_plan(plan, state, rank)
    return {
        "ready": dag.ready_set(plan, state),
        "claimable": dag.claimable_set(plan, state, rank),
        # What this pass would actually select, capped by free slots — a subset of claimable.
        "admitted": admission["admitted"],
        # Every node the pass passed over, with which of the four reasons applied. Read-only detail: a
        # session should be able to see why the scheduler chose as it did without running a verb that
        # refuses to find out.
        "deferred": admission["deferred"],
        "admission_rank": rank,
        "critical_path": lengths,
        "slots_in_use": dag.slots_in_use(plan, state),
        "max_concurrency": parallelism.get("max_concurrency", 1),
        "resource_holders": dag.resource_holders(plan, state),
        "nodes": nodes,
    }


def _status(state: dict, plan: dict | None = None) -> dict:
    head = _head()
    required_evidence, judgments, warnings = [], [], []
    unresolved_assumptions: list[str] = []
    delivery = state["reviews"]["deliverable"]
    missing_findings = _missing_findings(state)
    blocking = [f["id"] for f in review.live_findings(state) if review.blocks_submission(f)]

    if state["approval"] is None or state["approval"].get("plan_digest") != state["plan"]["digest"]:
        required_evidence.append("operator approval of this plan digest and review depth")
    fast_path = bool(plan and plan["profile"] == "trivial" and (state.get("approval") or {}).get("depth") == "quick")
    trivial_violations = _trivial_violations(state, plan) if plan else []
    if plan and state["approval"]:
        _assert_spec_current(state, plan)
    # Plan review is no longer a Build-side gate at all. It happened before this Build existed, on the plan
    # side, and the seal is the receipt: a Build cannot bind an unsealed plan, and a seal refuses a review
    # that does not cover its approved depth. What survives here is the DISCLOSURE of an operator-authorized
    # mid-flight divergence from that seal, because that is the one case where the sealed record no longer
    # describes what is being built.
    plan_escalation = review.plan_change_escalation(state)
    required_evidence.extend(f"finding disposition: {x}" for x in missing_findings)
    if blocking:
        judgments.append("resolve or deliberately re-disposition findings blocking this PR: " + ", ".join(blocking))
    if state["checkpoint"] and state["checkpoint"]["judgment"] != "aligned":
        judgments.append(state["checkpoint"]["judgment"])
    if state["validation"] is None or state["validation"]["commit"] != head or not all(x["passed"] for x in (state["validation"] or {}).get("results", [])):
        required_evidence.append("green validation for the final commit")
    if delivery["packet_digest"] is None and not fast_path:
        required_evidence.append("deliverable-review packet")
    else:
        required_evidence.extend(f"deliverable-review receipt: {x}" for x in _missing_receipts(delivery))
    rewritten = _history_was_rewritten(state, head)
    if delivery["reviewed_commit"] and delivery["reviewed_commit"] != head:
        repair = state["repair"]
        if not repair or repair["reviewed_commit"] != delivery["reviewed_commit"] or repair["final_commit"] != head:
            judgments.append(
                "re-anchor the review bindings with `reconcile`: this branch's history was rewritten and the "
                "reviewed commit is no longer on it" if rewritten else
                "the branch has moved past the reviewed commit: judge how much of that divergence needs "
                "re-reading and record it with `repair assess`. `none` is a real judgment, not a skip — "
                "it ends the repair loop without a re-review and clears the repair packet, so reach for "
                "it when the divergence genuinely carries nothing a lens would find.")
        elif repair["judgment"] != "none":
            outstanding = _outstanding_repair_lenses(repair)
            # Name the DELTA each outstanding lens still owes, never a bare "run it again". The wall this
            # replaces was a session told to re-run two lenses with no way to see that one of them had
            # already read everything in the range.
            base, tip = _stage_range(repair, "repair")
            by_lens = {r["lens"]: r for r in repair.get("receipts", [])}
            for lens in outstanding:
                detail = (" — " + ranges.coverage_report(ROOT, by_lens[lens], base, tip)
                          if lens in by_lens else "")
                required_evidence.append(f"repair-review receipt: {lens}{detail}")
    protocol = _protocol()
    if state["approval"]:
        depth = state["approval"]["depth"]
        current_delivery = _required(protocol, depth, _installed())
        def contracts_current(current, recorded):
            actual = {item["lens"]: (item["path"], item["digest"])
                      for item in recorded.get("reviewer_contracts", [])}
            return all(actual.get(item["lens"]) == (item["path"], item["digest"]) for item in current)
        delivery_coverage_current = contracts_current(current_delivery, delivery)
        if delivery["packet_digest"] and not delivery_coverage_current:
            required_evidence.append("refresh deliverable-review coverage for the currently installed reviewers")
    else:
        delivery_coverage_current = True
    passed = {x["id"] for x in state["preflights"] if x["commit"] == head and x["passed"]}
    required_preflights = [x for x in protocol["preflights"] if x["required"]]
    required_evidence.extend(f"green preflight: {x['id']}" for x in required_preflights if x["id"] not in passed)
    if not state["pr_contract"] or state["pr_contract"]["commit"] != head or not state["pr_contract"]["complete"]:
        required_evidence.append("complete PR contract for the final commit")
    if state["plan"].get("diverged_from_seal"):
        warnings.append(f"the executed plan differs from the sealed plan {state['plan']['plan_id']}; the "
                        "seal records what was reviewed, not what is being built")
    if plan_escalation:
        warnings.append("plan changed after review (operator-authorized, not re-reviewed): "
                        + plan_escalation["operator_change"])
    if plan:
        # An assumption's EFFECTIVE status is its authored plan status overlaid with any receipt-layer
        # disposition (StarshipSuperjam/engine-template#1014). A disposition never edits the plan, so the
        # plan digest, approval, and review receipts survive — but a genuinely-open premise the review
        # verified no longer forces a full review re-run to clear its engineering-decision hold. Computed
        # ONCE and fed to BOTH the judgment/warning lines AND the phase gate below, so a disposed assumption
        # can never wall while still printing "investigate …" (a contradictory render).
        dispositions = {d["claim"]: d for d in state.get("assumption_dispositions", [])}
        unresolved_assumptions, accepted, resolved_notes = [], [], []
        for item in plan.get("assumptions", []):
            claim, authored = item["claim"], item["status"]
            disposition = dispositions.get(claim)
            effective = disposition["resolved_as"] if disposition else authored
            if effective == "unresolved":
                unresolved_assumptions.append(claim)
            elif effective == "accepted-risk":
                accepted.append(claim)
            # A disposition can only touch an assumption authored 'unresolved', so its presence IS the
            # "resolved after approval" signal — no timestamp needed. Disclosed for BOTH verified and
            # accepted-risk (a 'verified' disposition must NOT vanish the way a plan-authored 'verified'
            # does), so the operator meets a self-attested post-hoc resolution at merge, never a silent skip.
            if disposition and authored == "unresolved":
                resolved_notes.append(
                    f"assumption resolved after approval (self-attested, not re-reviewed): {claim} "
                    f"-> {disposition['resolved_as']} — basis: {disposition['basis']}")
        judgments.extend("investigate unresolved assumption: " + value for value in unresolved_assumptions)
        warnings.extend("accepted plan risk: " + value for value in accepted)
        warnings.extend(resolved_notes)
    if trivial_violations:
        judgments.append("raise the trivial Build to the normal profile and renew approval: " + "; ".join(trivial_violations))
    # Surfaced rather than refused: `status` is what a session runs to find out WHY it is stuck, so the
    # snapshot that the two gates will refuse must still be readable and must name its own remedy here.
    unearned = _unearned_completions(state)
    if unearned:
        judgments.append("completions recorded without integration evidence (" + ", ".join(unearned)
                         + "); " + _UNEARNED_COMPLETION_REMEDY)
    # Advisory artifact-preparation line: a recorded sync receipt whose commit is no longer HEAD means the
    # tree moved since the last sync, so derived artifacts may be stale. Advisory only — validate's read-only
    # pre-gate and CI's drift checks are the authority; this just tells a session to re-sync before validating.
    sync_receipt = state.get("artifact_sync")
    if sync_receipt and sync_receipt.get("commit") != _head():
        warnings.append("derived artifacts were synced at " + sync_receipt["commit"][:12]
                        + ", but HEAD has since moved — run `sync-artifacts` again if sources changed")
    # Delivered reviewer effort against the depth that was approved
    # (StarshipSuperjam/engine-template#1067). A warning here and a hard line in the pull-request body:
    # the operator approved a depth that promises an effort, and until this existed nothing recorded
    # whether the panel delivered it — a sealed `thorough` whose lenses actually ran at `medium`
    # published its promise unchallenged. Both halves are self-reported, and every rendering says so.
    for line in _effort_shortfall_lines(state):
        warnings.append(line)

    approval_ready = state["approval"] is not None and state["approval"].get("plan_digest") == state["plan"]["digest"]
    dispositions_ready = not missing_findings and not blocking
    valid = state["validation"] is not None and state["validation"]["commit"] == head and all(x["passed"] for x in state["validation"]["results"])
    delivery_ready = fast_path or (delivery["packet_digest"] is not None and not _missing_receipts(delivery) and delivery_coverage_current)
    repair_ready = not delivery["reviewed_commit"] or delivery["reviewed_commit"] == head or (
        state["repair"] is not None and state["repair"]["reviewed_commit"] == delivery["reviewed_commit"]
        and state["repair"]["final_commit"] == head and (state["repair"]["judgment"] == "none" or
        not _outstanding_repair_lenses(state["repair"])))
    preflight_ready = not [x for x in required_preflights if x["id"] not in passed]
    contract_ready = bool(state["pr_contract"] and state["pr_contract"]["commit"] == head and state["pr_contract"]["complete"])

    if not approval_ready:
        phase, next_one, available = "planning", "approve the plan and review depth", []
    elif not dispositions_ready:
        phase, next_one, available = "finding-disposition", None, ["critically adjudicate outstanding findings", "revise the plan if the agreed design changed"]
    elif trivial_violations or unresolved_assumptions or (state["checkpoint"] and state["checkpoint"]["judgment"] != "aligned"):
        phase, next_one, available = "engineering-decision", None, ["investigate unresolved assumptions", "revise the plan if the agreed design changed", "obtain a genuine operator decision only when required"]
    elif not valid:
        phase, next_one, available = "implementation", None, ["continue implementation", "run focused verification", "run final validation when the change is cohesive"]
    elif not delivery_ready:
        phase, next_one, available = "deliverable-review", "prepare or complete the deliverable review", []
    elif not repair_ready:
        phase, next_one, available = ("repair-assessment",
                                       "re-anchor the review bindings with `reconcile`" if rewritten
                                       else "record the proportional re-review judgment",
                                       # A session reads this list BEFORE it acts. Naming the verb only in
                                       # the refusals would mean a session met it only after being stuck.
                                       ["re-anchor the bindings with `reconcile` after a rebase or other "
                                        "history rewrite"] if rewritten else
                                       ["record the proportional re-review judgment"])
    elif not preflight_ready or not contract_ready:
        phase, next_one, available = "submission-preflight", "run submission preflights", []
    else:
        phase, next_one, available = "ready", "preview submission", []
    ordered_items = [] if not plan else [item["id"] for item in plan["work_items"]]
    completed_items = [item["id"] for item in state["progress"]["completed"]]
    next_item = _next_incomplete(plan, state) if plan else None
    result = {"phase": phase, "head_commit": head, "snapshot_revision": state["revision"],
              "required_evidence": required_evidence, "engineering_judgment": judgments,
              "warnings": warnings, "suggested_next": next_one, "available_activities": available,
              "progress": {"completed": completed_items, "total": len(ordered_items),
                           "current": state["progress"]["current_item"], "next": next_item}}
    if plan is not None and state.get("schema_version") == "build-state.v2":
        result["work"] = _work_projection(plan, state)
    return result


def _library() -> "plan_store.PlanLibrary":
    """The plan library for THIS Build's product checkout.

    The two-root resolution is not re-derived here. `plan_store.library_root` already resolves it
    through `checkout_health.resolve_product_checkout` and fails closed on both ambiguous cases, so a
    Build planned in a mechanic session against the owned product finds its plans in the product's own
    canonical checkout. Deliberately unrelated to any write-authorization gate: where a plan LIVES is a
    topology question, and answering it with a permission check is how a resolution silently picks the
    wrong root.
    """
    import plan_store
    return plan_store.PlanLibrary()


def _sealed_plan(selector: str) -> tuple[str, str, dict]:
    """Resolve a sealed plan in the local library: (plan_id, sealed_digest, build payload).

    This is the ONLY door a plan comes through. Anything unsealed is refused here rather than at some
    later gate, because a Build that has already started against an unreviewed plan is exactly the
    failure the seal exists to prevent.
    """
    import plan_contract
    library = _library()
    try:
        slug = library.resolve(selector)
    except Exception as exc:  # noqa: BLE001 — the library speaks its own error language
        raise CoordinatorError(f"no plan in the local library matches {selector!r}: {exc}") from exc
    problems = library.verify_chain(slug)
    if problems:
        raise CoordinatorError(
            f"the plan record for {selector!r} does not verify, so it cannot authorize a Build: "
            + "; ".join(problems))
    record = library.read_record(slug)
    seal = record.get("seal")
    if not seal:
        raise CoordinatorError(
            f"{record['plan_id']} is not sealed, and only a sealed plan enters a Build. Finish its "
            "lifecycle first — preview, approve with a depth, record the one cold plan review, "
            f"disposition its findings, then `project_manager.py seal {record['plan_id']}`.")
    document = library.head(slug)
    if record["current"]["plan_digest"] != seal["sealed_digest"]:
        raise CoordinatorError(
            f"{record['plan_id']} has moved since it was sealed (sealed {seal['sealed_digest']}, "
            f"current {record['current']['plan_digest']}); a seal is terminal, so clone it into a new "
            "plan rather than binding a changed one")
    payload = document.get("build_plan")
    if not isinstance(payload, dict):
        raise CoordinatorError(f"{record['plan_id']} carries no build payload to execute")
    if plan_contract.build_plan_digest(document) != seal["build_plan_digest"]:
        raise CoordinatorError(
            f"the build payload inside {record['plan_id']} does not match the digest its seal recorded")
    return record["plan_id"], seal["sealed_digest"], payload


def _record_build_binding(plan_id: str, repository: str, pr: int, sealed_digest: str,
                          build_plan_digest: str, consent: dict | None = None) -> None:
    """Mark the sealed plan as the one now driving a Build. Best-effort and non-fatal.

    The binding is a record, not a lock: the plan library is the plan's home and the Build snapshot is
    the Build's, and a library that cannot be written must not strand a Build that is otherwise sound.

    The bind ATTESTATION rides along here rather than into build-state, because the plan record is
    where the other three gates' attestations already live and a consent trail split across two
    stores is a trail with a seam to lose things in. The refusal that demands it is in `cmd_plan_bind`
    and is NOT best-effort — this write is what records it, not what enforces it.
    """
    import moment
    library = _library()
    try:
        slug = library.resolve(plan_id)
        record = library.read_record(slug)
        binding = {"sealed_digest": sealed_digest, "build_plan_digest": build_plan_digest,
                   "at": moment.utc_now(), "pull_request": pr, "repository": repository}

        def mark(current):
            current["build_binding"] = binding
            if consent:
                current.setdefault("consent", []).append(consent)

        library.update_record(slug, mark, expected_revision=record["current"]["revision"])
    except Exception as exc:  # noqa: BLE001 — disclosed, never fatal
        print(f"build-coordinator: could not record the Build binding on {plan_id} ({exc}); the Build "
              "proceeds and the plan's record simply does not name this PR.", file=sys.stderr)


def _check_authorization(plan: dict, issue: int | None, mode: str) -> None:
    """Two artifacts, two authorities, and a check that they are about the same work.

    Until the plan moved into the local library these were one thing: the durable Issue CARRIED the
    plan, so an unattended Build that had the Issue necessarily had the plan, and `plan bind` proved it
    by comparing digests. That equality is what the split removes, and deleting it without replacing it
    would have quietly widened unattended authority — any open Issue plus any sealed plan would have
    started a Build on work nobody connected the two to.

    So the guarantee is rebuilt rather than dropped, in the only form two separate artifacts can carry
    it: CORRESPONDENCE. The Issue remains the AUTHORIZATION — the durable, operator-visible record that
    this work may run unattended — and the sealed plan remains the PLAN AUTHORITY, the reviewed document
    that says what the work is. Neither substitutes for the other, and an unattended bind must show that
    the one names the other. A stale Issue, an unrelated Issue, or an Issue the plan has never heard of
    authorizes nothing.

    The plan's own `intent_source` is the reference, because it was fixed when the plan was sealed: the
    Issue number in it was reviewed and then locked, so it cannot be chosen at bind time to match
    whatever Issue is convenient. Only the `--issue` argument is free to vary, and this is what pins it.

    Interactive Builds keep their shape — `--issue` stays optional there, since a same-session Build is
    authorized by the operator being present — but an Issue supplied in ANY mode must still correspond,
    because a mismatch is a mistake worth catching wherever it happens.
    """
    intent = plan["intent_source"]
    reference = intent["issue"] if intent["kind"] == "issue" else None
    if mode == "unattended":
        if issue is None:
            raise CoordinatorError(
                "unattended Builds require a durable Issue for authorization. The sealed plan is the "
                "PLAN — what the work is — and it does not authorize its own unattended execution; the "
                "Issue is the durable, operator-visible record that this work may run with nobody "
                "watching. Pass it with --issue.")
        if reference is None:
            raise CoordinatorError(
                "this sealed plan names no authorizing Issue, so there is nothing for --issue to "
                f"correspond to and no way to show that Issue #{issue} is about this work. An "
                "unattended Build needs both halves: a plan whose recorded intent came from an Issue, "
                "and that same Issue supplied here. Bind it same-session instead, or plan the "
                "unattended work from the Issue that authorizes it.")
    elif issue is not None and reference is None:
        # Same-session, --issue supplied, and the plan names no Issue at all. The Build is authorized
        # either way — the operator is present — but the number does not vanish: it is stored as the
        # authorizing Issue and composed into the pull request as a Closes link. An unverified Closes is
        # a false claim on the merge surface, so the correspondence rule holds in EVERY mode, which is
        # what this function's contract says and what this arm makes true.
        raise CoordinatorError(
            f"this sealed plan names no Issue, so there is nothing for --issue {issue} to correspond to "
            "and no way to show that the two are about the same work. The pull request would claim to "
            f"close #{issue} on nothing but your say-so at bind time. Bind without --issue, or bind a "
            "plan whose recorded intent came from that Issue.")
    if issue is not None and reference is not None and issue != reference:
        raise CoordinatorError(
            f"Issue #{issue} does not authorize this plan: the plan was sealed against Issue "
            f"#{reference}, and an Issue that is not the one the plan was written from cannot vouch "
            "for it. This is the check that replaced the old Issue-carries-the-plan equality — with "
            "the plan held locally the two are separate artifacts, so bind proves they are about the "
            "same work rather than assuming it. Supply the Issue the plan names, or bind a plan "
            "written from the Issue you meant.")


def cmd_plan_bind(args, store: Snapshot) -> None:
    mode = getattr(args, "mode", "same-session")
    plan_id, sealed_digest, plan = _sealed_plan(args.plan)
    # The closed door. B2 made v1 unreachable at entry; the sunset removed the schemas and the
    # converter, so this refusal is now terminal rather than a wait. Deliberately so: migrating a
    # SEALED plan would invalidate the seal that is the only thing making it a plan a Build may enter,
    # and a converter that produced a plan nobody approved would have been a way around the seal
    # wearing the costume of a migration. Re-authoring is the path, and the refusal names it.
    if _plan_version(plan) == "build-plan.v1":
        raise CoordinatorError(
            "this sealed plan carries a build-plan.v1 payload, and v1 no longer enters a Build. There "
            "is no converter: a migration would invalidate the seal, and an unapproved plan is not a "
            "plan. Re-author this work as a fresh plan through the Project Manager — its deliberation "
            "can be imported from the old one — and seal that. If a v1 Build is already in flight, "
            "finish it on the engine it started on.")
    issue = args.issue
    # Profile first, then authorization. Both can be true of one bad bind — a trivial plan handed an
    # Issue and unattended mode breaks two rules at once — and the profile rule is the root cause: it
    # says this plan may not run in this mode AT ALL, so no Issue could have fixed it. Reporting the
    # authorization failure there would send the operator hunting for the right Issue number for a
    # Build that was never going to be unattended.
    if plan["profile"] == "trivial" and mode != "same-session":
        raise CoordinatorError("trivial Builds are same-session only")
    if plan["profile"] == "routine" and mode != "unattended":
        raise CoordinatorError("routine plans require unattended mode and durable Issue authority")
    _check_authorization(plan, issue, mode)
    # The last consent gate, and the one the silent ceremony reached: a sealed plan bound to a fresh
    # pull request in the same unattended breath as the seal that produced it. The operator's go for
    # the BUILD to begin is its own decision, distinct from their go to seal the plan, and it is
    # taken here where the Build actually starts. Recorded, not proven (issue 914's residual).
    import moment
    import plan_lifecycle
    if not (getattr(args, "operator_decision", None) or "").strip():
        raise CoordinatorError(plan_lifecycle.missing_consent({}, "bind"))
    consent = plan_lifecycle.attestation("bind", args.operator_decision, at=moment.utc_now())
    pr = _verify_draft(args.repository, args.pr)
    if pr.get("headRefOid") != _head():
        raise CoordinatorError("the draft PR head does not match this worktree")
    state = _initial_state(args.repository, args.pr, pr.get("baseRefOid") or _base(), plan_id,
                           sealed_digest, plan, issue, mode)
    # Where this Build's evidence lands. With no --state it goes to the durable store beside its own
    # sealed plan, which is the default because the alternative is what actually happened: a killed
    # Build whose approval, receipts, findings and progress were reconstructed by hand.
    if store is None:
        store = build_state_store.store_for_plan(plan_id, _state_schema_for, library=_library())
    store.create(state)
    _record_build_binding(plan_id, args.repository, args.pr, sealed_digest, state["plan"]["digest"],
                          consent)
    # Tag the PR the coordinator just adopted, so it carries a durable "coordinator owns this workflow"
    # marker (StarshipSuperjam/engine-template#1014). Best-effort and non-fatal: a labeling failure is
    # disclosed on stderr and the Build proceeds — the stdout below stays a clean machine-readable line.
    if not github.tag_coordinator_owned(ROOT, args.repository, args.pr):
        print("build-coordinator: could not tag this PR 'engine-coordinator-owned' (a non-blocking aid); "
              "the Build proceeds — reach ready only through 'submit apply', never a bare 'gh pr ready'.",
              file=sys.stderr)
    print(json.dumps({"plan_digest": state["plan"]["digest"], "state": str(store.path)}))


def cmd_state_where(args, store: "Snapshot | None") -> None:
    """Say where this Build's evidence actually is. The first question a resuming session asks."""
    library = _library()
    found = build_state_store.bound_snapshots(ROOT, library=library)
    warning = plan_store_module().volume_warning(library.root)
    print(f"plan library: {library.root}")
    if warning:
        print(f"WARNING: {warning}")
    elif not plan_store_module().volume_determined(library.root):
        print("note: this platform could not determine the library's filesystem type, so the "
              "network-filesystem check did not run. That is a gap, not a pass.")
    if not found:
        print(f"no durable Build snapshot is bound to {ROOT}")
        return
    for slug, path in found:
        state = core.json_file(path)
        print(f"{slug}: {path} (revision {state.get('revision')}, "
              f"PR {state['build']['pr']}, {state['build']['repository']})")


def cmd_state_migrate(args, store: "Snapshot | None") -> None:
    """Move one OS-temp snapshot into the durable library, or refuse and leave it untouched."""
    destination = build_state_store.migrate(args.source, args.plan, _state_schema_for,
                                            library=_library(), worktree=ROOT)
    print(json.dumps({"migrated": str(destination), "source": str(Path(args.source).resolve()),
                      "source_kept": True}))


def cmd_state_supersede(args, store: "Snapshot | None") -> None:
    """Set this plan's durable snapshot aside so a second Build of it may start. Never implicit."""
    library = _library()
    slug = library.resolve(args.plan)
    retired = build_state_store.supersede(library, slug, reason=args.reason)
    if retired is None:
        raise CoordinatorError(
            f"{slug} holds no durable Build snapshot, so there is nothing to supersede.")
    print(json.dumps({"superseded": str(retired)}))


def plan_store_module():
    import plan_store
    return plan_store


# The v1 converter is GONE, and its absence is the operator's decision of 2026-08-25 rather than an
# oversight. A converter can only produce a document nobody approved, and the only place a v1 payload
# still exists is inside a SEALED plan, whose seal a migration would invalidate — so the "migrate then
# bind" path was never a path, only a longer way to the same refusal. `plan bind` names re-authoring
# through the Project Manager instead, which is the thing that actually gets the work built.


def _reset_after_revision(state: dict, plan: dict) -> None:
    # `plan_id` and `sealed_digest` deliberately survive a revision: the sealed plan is still the plan of
    # record and the authority this Build entered on. What changes is that the EXECUTED payload no longer
    # equals the sealed one, and that is recorded rather than inferred — a seal whose divergence is only
    # visible in prose is a seal whose meaning leaks. The composed PR contract reads this flag.
    state["plan"].update({"digest": _digest(plan), "intent_digest": _digest(plan["raw_intent"].encode()),
                          "spec_digest": None, "profile": plan["profile"], "bound_head": _head(),
                          "diverged_from_seal": True})
    state["approval"] = None
    state["reviews"] = {"deliverable": _empty_review()}
    state["findings"] = []
    # Assumption dispositions are cycle-bound evidence, like findings and review receipts: a plan revision
    # re-authors the premises and re-reviews from scratch, so a stale disposition must not survive to silently
    # re-clear a wall the freshly-authored plan re-opens (StarshipSuperjam/engine-template#1014). A depth-only
    # change keeps the plan digest and does NOT reach this reset, so a disposition rightly survives it.
    state.pop("assumption_dispositions", None)
    state["progress"] = {"current_item": None, "completed": []}
    if "work" in state:
        state["work"] = {}
    state["checkpoint"] = state["validation"] = state["repair"] = state["pr_contract"] = None
    state["preflights"] = []
    state["checkout_snapshot"] = None
    # The artifact-sync receipt is cycle-bound like validation: a revised plan may change what is built, so a
    # stale sync receipt must not stand in for a fresh preparation of the re-authored change.
    state.pop("artifact_sync", None)


def _unchanged_nodes(old: dict, new: dict) -> set:
    """Node ids the successor plan carries UNCHANGED, and whose ancestors are all unchanged too.

    Two rules, and the second is the one that matters. A node is a candidate when the successor's
    item is byte-identical to the bound plan's item of the same id — same description, same paths,
    same verification, same output contract — because anything less means the work asked for moved.

    Then the ancestry rule prunes it: a node whose dependency changed is NOT unchanged, however
    identical its own text, because its integration was verified against a predecessor that no longer
    exists. Keeping such a node would carry forward evidence about a graph that was never built,
    which is exactly the laundering a mid-Build revision must not perform.
    """
    old_items = {item["id"]: item for item in old.get("work_items", [])}
    new_items = {item["id"]: item for item in new.get("work_items", [])}
    identical = {node_id for node_id, item in new_items.items()
                 if node_id in old_items and _canonical(old_items[node_id]) == _canonical(item)}
    # Prune by ancestry, to a fixed point: dropping a node can drop its dependants in turn.
    while True:
        pruned = {node_id for node_id in identical
                  if all(dep in identical for dep in new_items[node_id].get("depends_on", []))}
        if pruned == identical:
            return identical
        identical = pruned


def cmd_plan_adopt(args, store: Snapshot) -> None:
    """Consume a SEALED successor plan without restarting the Build. OB-MIDBUILD-REVISION.

    The problem this answers. A seal is terminal, so a plan discovered mid-Build to be wrong cannot
    be edited — the way past a seal is a clone. But until now adopting that clone meant abandoning
    the Build: a fresh bind on a fresh snapshot, throwing away every node already integrated and
    verified, because `plan revise` resets the whole graph. The cost of correcting a plan was
    therefore the cost of rebuilding everything it got right, which is a strong incentive to keep
    building against a plan you already believe is flawed.

    What is preserved, and why each is safe. The BINDING — same pull request, same snapshot, same
    branch — because the work is the same work. The APPROVAL and its depth, taken from the successor's
    OWN plan-side approval: the successor was approved and its panel ran on the plan side, so the
    Build inherits consent that was actually granted for THIS document rather than carrying over the
    predecessor's. And the integration evidence of nodes the successor carries unchanged with
    unchanged ancestry (see `_unchanged_nodes`).

    What is reset: every changed or new node, everything downstream of one, and all cycle-bound
    Build evidence — deliverable receipts, findings, validation, preflights, the composed contract.
    The deliverable panel re-runs on what was actually built; the PLAN panel never re-runs, because
    it already ran on the plan side, against the successor, before this verb could be reached.

    What is refused: a successor that is not sealed, one that does not name this plan as its
    predecessor, and one adopted without the operator's recorded decision. Lineage is the load-bearing
    check — without it this verb would let any sealed plan take over any Build in flight, which is a
    plan swap wearing the costume of a correction.
    """
    import moment
    import plan_lifecycle
    if not (getattr(args, "operator_decision", None) or "").strip():
        raise CoordinatorError(plan_lifecycle.missing_consent({}, "bind"))
    state = store.read()
    bound_id = state["plan"]["plan_id"]
    successor_id, sealed_digest, successor = _sealed_plan(args.successor)
    if successor_id == bound_id:
        raise CoordinatorError(
            f"{successor_id} is the plan this Build is already bound to. A sealed plan cannot be "
            "revised, so adopting it again would change nothing.")
    library = _library()
    record = library.read_record(library.resolve(successor_id))
    lineage = " ".join((record.get("intake") or {}).get("predecessors", []))
    if bound_id not in lineage:
        raise CoordinatorError(
            f"{successor_id} does not name {bound_id} among its predecessors, so nothing shows it is a "
            f"correction of the plan this Build is executing rather than an unrelated plan. Adopt only a "
            f"clone of the bound plan:\n    project_manager.py clone {bound_id} "
            "--reason \"<what the Build discovered>\"\n  then approve, review and seal that clone.")
    bound_plan = _plan(args.input)
    if _digest(bound_plan) != state["plan"]["digest"]:
        raise CoordinatorError(
            "--input must be the plan this Build is currently executing, so the two can be compared "
            f"node by node; this one digests to {_digest(bound_plan)}, not {state['plan']['digest']}")
    consent = plan_lifecycle.attestation("bind", args.operator_decision, at=moment.utc_now())
    keep = _unchanged_nodes(bound_plan, successor)

    # Resolved BEFORE the mutation, so a specification that cannot be resolved refuses the adoption
    # rather than half-applying it — the same fail-before-write discipline the store keeps everywhere.
    adopted_spec_digest = None
    if record.get("approval"):
        adopted_spec_digest = _canonical_spec(
            successor, repository=state["build"]["repository"], check_issue=False)["digest"]

    def change(current):
        preserved_work = {node_id: entry for node_id, entry in (current.get("work") or {}).items()
                          if node_id in keep}
        preserved_progress = [entry for entry in current["progress"]["completed"]
                              if entry["id"] in keep]
        previous_digest = current["plan"]["digest"]
        _reset_after_revision(current, successor)
        # The successor is a SEALED plan in its own right, so the Build is not diverging from a seal —
        # it is executing a different one. Re-pointing rather than flagging divergence is the whole
        # difference between this verb and `plan revise`.
        current["plan"].update({"plan_id": successor_id, "sealed_digest": sealed_digest,
                                "diverged_from_seal": False})
        current["work"] = preserved_work
        current["progress"] = {"current_item": None, "completed": preserved_progress}
        # Consent that was granted for THIS document, on the plan side, at the depth its panel ran.
        #
        # AND ITS SETTLED-SPECIFICATION FINGERPRINT, resolved here rather than left absent. `approve` is
        # what normally resolves the specification and records its digest; adoption inherits the approval
        # WITHOUT passing through approve, so leaving the fingerprint None meant the next command compared
        # the live specification against nothing and refused with "settled specification changed since
        # approval" — which had not happened — and pointed at a plan revision that would have undone the
        # adoption's whole purpose. Invisible in a project with no settled specification, and a wall in
        # every project that has one.
        approval = record.get("approval")
        if approval:
            current["approval"] = {"plan_digest": current["plan"]["digest"],
                                   "spec_digest": adopted_spec_digest, "depth": approval["depth"]}
            current["plan"]["spec_digest"] = adopted_spec_digest
        current.setdefault("plan_change_escalations", []).append(
            {"reviewed_plan_digest": previous_digest, "plan_digest": _digest(successor),
             "operator_change": f"adopted sealed successor {successor_id}: {args.operator_decision}"})

    store.mutate(change, from_revision=state["revision"])
    _record_build_binding(successor_id, state["build"]["repository"], state["build"]["pr"],
                          sealed_digest, _digest(successor), consent)
    preserved = sorted(keep)
    print(f"adopted sealed successor {successor_id}; the Build continues on PR "
          f"{state['build']['pr']} with its binding intact")
    print(f"  preserved unchanged, with integration evidence: {', '.join(preserved) or 'no nodes'}")
    print("  reset: every changed or new node, everything downstream of one, and all Build-side "
          "review, validation and preflight evidence")
    print("  the plan panel does NOT re-run — it ran on the plan side, against this successor")


def cmd_plan_revise(args, store: Snapshot) -> None:
    plan = _plan(args.input)
    state = store.read()
    if _digest(plan) == state["plan"]["digest"]:
        print("plan content is unchanged; existing evidence remains current")
        return
    # One design-review panel per Build. A completed panel FREEZES the plan: revising it here would
    # invalidate all four receipts and force a second cold panel, which is the single largest source of
    # Build spend and is never the right answer. Panel findings are dispositioned and fixed during
    # implementation; a plan the panel reveals as not-ready is scrapped and re-planned, not re-reviewed.
    # Iterating the plan freely BEFORE the first packet is the intended path and is untouched by this.
    escalation = getattr(args, "operator_change", None)
    # Every Build now enters on a SEALED plan, which means every Build's plan has already been reviewed
    # before a single line was written. So revising it here is never a free edit and never a re-review:
    # it is the operator authorizing execution of a plan that differs from the one the panel read. The
    # old condition keyed this on a completed Build-side plan panel; with the panel on the plan side
    # that ledger is always empty, and keying on it would have quietly retired the wall.
    if not escalation:
        raise CoordinatorError(
            "this Build entered on a sealed plan, so the plan you are revising has already been reviewed "
            "and settled. Re-reviewing it here is not one of the ways forward. If you believe the PLAN "
            "itself is wrong, that is the operator's call, not yours: stop now, put the change and its "
            "consequences to them in plain words, and record their decision with --operator-change. If "
            "the plan is wrong at its root, abandon this Build and re-plan from intent — a seal is "
            "terminal, so that means a new plan, not an edited one. Only if the discovery is an "
            "implementation matter — the plan still stands and the fix belongs in the code — carry on "
            "without touching the plan. Do not take that last path to avoid the first: never keep "
            "building against a plan you believe is flawed.")

    def change(current):
        reviewed_digest = current["plan"]["digest"]
        _reset_after_revision(current, plan)
        # The receipt-layer escalation record, following the assumption-disposition precedent
        # (StarshipSuperjam/engine-template#1014): a legitimate mid-flight correction must not be
        # payable only by nuking the review chain, because that manufactures an incentive to carry on
        # against a plan the session already doubts. The revision CLEARS the Build's own receipts; they
        # are neither carried forward nor re-pointed at the new digest, which would forge review of a
        # delta nobody read. The sealed plan stays on record in the library against the digest its panel
        # actually read. What this records is that the operator authorized executing a CHANGED plan
        # without re-review; that gap is disclosed in status AND published in the PR body.
        current.setdefault("plan_change_escalations", []).append(
            {"reviewed_plan_digest": reviewed_digest, "plan_digest": _digest(plan),
             "operator_change": escalation})
    store.mutate(change, from_revision=state["revision"])
    print(f"revised plan to {_digest(plan)} on recorded operator authority; the sealed plan is unchanged "
          "and the divergence is disclosed at merge")


def cmd_approve(args, store: Snapshot) -> None:
    plan = _plan(args.plan)
    state = store.read()
    _assert_plan(state, plan)
    if plan["profile"] == "trivial" and args.depth != "quick":
        raise CoordinatorError("the trivial one-glance profile requires quick depth")
    canonical_spec = _canonical_spec(plan, repository=state["build"]["repository"], check_issue=True)
    def change(state):
        _assert_plan(state, plan)
        if state["approval"] and state["approval"]["depth"] != args.depth:
            state["reviews"] = {"deliverable": _empty_review()}
            state["findings"] = []
            state["validation"] = state["repair"] = state["pr_contract"] = None
            state["preflights"] = []
            state["checkout_snapshot"] = None
        state["plan"]["spec_digest"] = canonical_spec["digest"]
        state["approval"] = {"plan_digest": state["plan"]["digest"], "spec_digest": canonical_spec["digest"], "depth": args.depth}
    store.mutate(change, from_revision=state["revision"])
    print(f"approved plan and {args.depth} review depth")


def cmd_status(args, store: Snapshot) -> None:
    state = store.read()
    plan = None
    if args.plan:
        plan = _plan(args.plan)
        _assert_plan(state, plan)
    result = _status(state, plan)
    if args.json:
        # The reminder rides in the JSON payload too, so a session consuming --json (a documented status
        # path) still meets the submit-gate nudge (StarshipSuperjam/engine-template#1014).
        result["reminder"] = _COORDINATOR_OWNED_REMINDER
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    print(f"Phase: {result['phase']} (snapshot r{result['snapshot_revision']})")
    print(_COORDINATOR_OWNED_REMINDER)
    progress = result["progress"]
    if progress["total"]:
        print(f"Progress: {len(progress['completed'])} of {progress['total']} complete"
              + (f"; next {progress['next']}" if progress["next"] else "; all planned items complete"))
    # The three buckets say what they ARE, not just what they are called. A session reading this while
    # stuck could not tell a hard gate fact from a prompt for its own judgment, and read
    # "choose none, scoped or full" as an instruction to pick one now — which is how a destructive
    # `--judgment none` got run mid-stream as though it were a step (StarshipSuperjam/engine-template#1012).
    for label, key in (
            ("Missing evidence (the gate refuses to submit until each of these exists)", "required_evidence"),
            ("Engineering judgment (your call — the coordinator reports these, it does not make them)",
             "engineering_judgment"),
            ("Warnings (disclosed at merge; no action is demanded here)", "warnings")):
        values = result[key]
        if values:
            print(label + ":")
            for value in values:
                print(f"  - {value}")
    if result["suggested_next"]:
        print("Suggested next: " + result["suggested_next"])
    if result["available_activities"]:
        print("Available activities (unordered):")
        for value in result["available_activities"]:
            print(f"  - {value}")
    if "work" in result:
        w = result["work"]
        print(f"Work graph: {w['slots_in_use']} of {w['max_concurrency']} worker slot(s) in use")
        print("  ready (unordered): " + (", ".join(w["ready"]) or "none"))
        print("  claimable now (a direct claim is permitted, in admission order): "
              + (", ".join(w["claimable"]) or "none"))
        if "admitted" in w:
            print("  this pass would admit: " + (", ".join(w["admitted"]) or "none"))
        deferred = w.get("deferred", [])
        for entry in deferred:
            # A deferred node can also be claimable: deferral describes what the scheduler's own pass
            # would pick, never what a direct claim is allowed to do. Saying so on the line itself,
            # because a session reading two lists that both name the same node cannot otherwise tell.
            note = " (still claimable directly)" if entry["id"] in w["claimable"] else ""
            print(f"  deferred {entry['id']}: {entry['kind']} — {entry['reason']}{note}")
        if deferred:
            print("  why a node was passed over, in full: `work frontier --plan <plan.json>`")
        for node_id in sorted(w["nodes"]):
            node = w["nodes"][node_id]
            line = f"  {node_id}: {node['state']} (attempt {node['attempt_count']})"
            if node["reasons"]:
                line += " — " + "; ".join(node["reasons"])
            route = node.get("route")
            if node["state"] == "claimed" and route:
                line += f" [route {route.get('provider')}/{route.get('model')}]"
            if node["state"] == "complete" and node.get("integration_commit"):
                line += f" [integrated {node['integration_commit'][:12]}]"
            failure = node.get("failure")
            if failure and failure.get("reason"):
                # The reason is untrusted free text (a worker's self-report or a pasted trace):
                # collapse it to one bounded line so the per-node render stays legible; the full
                # text is always available via --json.
                reason = " ".join(str(failure["reason"]).split())
                if len(reason) > 160:
                    reason = reason[:157] + "..."
                line += f" [failure: {reason}]"
            print(line)
        if w["resource_holders"]:
            print("  resources held by: " + ", ".join(sorted(w["resource_holders"])))


def cmd_depths(args, store: "Snapshot | None") -> None:
    """Advisory, read-only: which review depths are worth OFFERING for this repo's installed reviewer roster,
    and the reviewer effort each resolves to. Consulted before `approve` so the operator is never asked to pick
    a depth that buys nothing over a lighter one (StarshipSuperjam/engine-template#763). The exact offer rule
    (a depth runs at least one lens the last lighter offered depth does not, or the same non-empty lens-set at
    higher effort) is single-homed in `build_coordinator_review.available_depths`, which this delegates to;
    `quick` is always offered (the floor). This shapes the consent surface only — `required()`/`approve`
    remain the sole mechanical lens authority, so a collapsed depth bound anyway still resolves to quick's roster.
    Needs no Build state (it reads the protocol, the installed personas, and the shipped/operator effort)."""
    import agent_bindings  # lazy: keep the coordinator's common path import-light, as cmd_validate does
    protocol = _protocol()
    deliverable_roster = _installed()
    bindings = _bindings()
    efforts = {depth: agent_bindings.depth_effort(depth, bindings, root=str(ROOT))
               for depth in review.DEPTH_ORDER}
    offered = review.available_depths(protocol, deliverable_roster, efforts)
    detail = {}
    for depth in review.DEPTH_ORDER:
        detail[depth] = {
            "offered": depth in offered,
            "effort": efforts[depth],
            "deliverable_lenses": [item["lens"] for item in _required(protocol, depth, deliverable_roster)],
        }
    if args.json:
        print(json.dumps({"available": offered, "depths": detail}, indent=2, sort_keys=True))
        return
    print("Available review depths (only those that add coverage or effort over a lighter one):")
    for depth in offered:
        d = detail[depth]
        if not d["deliverable_lenses"]:
            print(f"  {depth}: no cold reviewers — your own read plus the automatic checks")
        else:
            effort = d["effort"] or "session default"
            # Name the lenses, not just their count, so the operator can see WHICH reviewer a heavier depth adds.
            print(f"  {depth}: reviewer effort {effort}")
            print(f"      deliverable lenses: {', '.join(d['deliverable_lenses']) or 'none'}")
    collapsed = [depth for depth in review.DEPTH_ORDER if depth not in offered]
    if collapsed:
        print("Collapsed (adds nothing over a lighter depth, so not offered): " + ", ".join(collapsed))


def _write_json_artifact(prefix: str, value: Any) -> tuple[str, str]:
    return core.write_json_artifact(prefix, value)


def _emit_packet(packet: dict, args) -> None:
    if getattr(args, "json", None) is True or not hasattr(args, "output"):
        print(json.dumps(packet, indent=2, sort_keys=True))
        return
    path = getattr(args, "output", None)
    if path:
        core.write_private_path(Path(path), json.dumps(packet, indent=2, sort_keys=True) + "\n")
    else:
        path, _ = _write_json_artifact("build-review-packet", packet)
    print(f"review packet {packet['packet_digest']} written to {path}; "
          f"{len(packet['required_lenses'])} required lens(es), commit {packet.get('commit') or 'plan'}")


def _packet(args, store: Snapshot | None) -> None:
    plan = _plan(args.plan)
    impact = json.loads(_input(args.impact)) if args.impact else {}
    protocol = _protocol()
    stage = args.stage
    if stage == "plan":
        raise CoordinatorError(
            "the Build Coordinator runs one review, and it is the deliverable review. Plan review "
            "happens on the plan side before the seal — `project_manager.py review packet` — and a "
            "Build cannot start on a plan that has not been through it.")
    installed = [item if isinstance(item, dict) else {
        "lens": item, "path": f"test-reviewer/{item}.md", "digest": _digest(item.encode())
    } for item in _installed()]
    installed_names = [item["lens"] for item in installed]
    if getattr(args, "standalone", False):
        if stage == "repair":
            raise CoordinatorError("standalone packets support deliverable review, not repair state")
        canonical_spec = _canonical_spec(plan, repository=args.repository, check_issue=True)
        required_contracts = _required(protocol, args.depth, installed)
        required = [item["lens"] for item in required_contracts]
        commit = args.commit
        if not commit or not args.base:
            raise CoordinatorError("standalone deliverable packets require --commit and --base")
        packet_root = Path.cwd().resolve()
        stable = core.StableCommit(packet_root, "standalone deliverable review-packet construction") if commit else None
        if stable:
            stable_head = stable.__enter__()
            try:
                if stable_head != commit:
                    raise CoordinatorError("standalone deliverable packet commit must equal the clean worktree HEAD")
                if core.run(["git", "cat-file", "-e", f"{args.base}^{{commit}}"], root=packet_root).returncode:
                    raise CoordinatorError("standalone deliverable packet base is not a commit in this worktree")
            except BaseException as exc:
                stable.__exit__(type(exc), exc, exc.__traceback__)
                raise
        referent = {"schema_version": "build-review-packet.v1", "stage": stage,
                  "raw_intent": plan["raw_intent"], "plan": plan, "plan_digest": _digest(plan),
                  "intent_digest": _digest(plan["raw_intent"].encode()), "spec": canonical_spec,
                  "commit": commit, "base_commit": args.base if commit else None, "impact": impact,
                  "protocol_digest": _digest(protocol), "installed_lenses": installed_names,
                  "required_lenses": required, "standalone": True}
        if stage == "deliverable":
            declarations = _hard_check_declarations()
            path, digest = _write_json_artifact("build-hard-check-declarations", declarations)
            referent["hard_check_declarations"] = {"digest": digest, "count": len(declarations),
                                                    "for_lens": "spec-conformance"}
        referent_digest = _digest(referent)
        packet = {**referent, "referent_digest": referent_digest,
                  "reviewer_contracts": review.lens_packets(referent_digest, required_contracts)}
        packet["packet_digest"] = _digest(packet)
        if stage == "deliverable":
            packet["artifacts"] = {"hard_check_declarations": {"path": path}}
        if stable:
            stable.__exit__(None, None, None)
        _emit_packet(packet, args)
        return
    if store is None:
        raise CoordinatorError("--state is required unless --standalone constructs a pre-PR review packet")
    state = store.read()
    revision = state["revision"]
    _assert_plan(state, plan)
    canonical_spec = _assert_spec_current(state, plan, check_issue=True)
    if not state["approval"]:
        raise CoordinatorError("approve the plan and depth before preparing review packets")
    if stage == "repair":
        repair = state["repair"]
        if not repair or repair["judgment"] == "none":
            raise CoordinatorError("a scoped or full repair assessment is required before a repair packet")
        required = repair["lenses"]
        required_contracts = [item for item in installed if item["lens"] in required]
        commit = repair["final_commit"]
        if not state["validation"] or state["validation"]["commit"] != commit or not all(x["passed"] for x in state["validation"]["results"]):
            raise CoordinatorError("green validation for the repaired commit is required before re-review")
    else:
        required_contracts = _required(protocol, state["approval"]["depth"], installed)
        required = [item["lens"] for item in required_contracts]
        commit = _head()
        if not state["validation"] or state["validation"]["commit"] != commit or not all(x["passed"] for x in state["validation"]["results"]):
            raise CoordinatorError("green validation for the current commit is required before deliverable review")
    # THE EFFORT GATE, at panel spawn (StarshipSuperjam/engine-template#1067). This is the moment the
    # session still holds every exit: it can raise its own effort and re-cut, or go back to the operator
    # for a lighter depth. Once the lenses have run, the only honest options left are re-running them or
    # publishing a shortfall, so the refusal belongs here. On the Claude arm a reviewer persona carries no
    # effort of its own by design — the depth reaches the lens ONLY through the spawning session's effort
    # — which is why the session has to state it. The value is self-reported and nothing here can check
    # it; commit-bound reviewer attestations (StarshipSuperjam/engine-template#916) are the residual.
    promised_effort = _depth_effort(state["approval"]["depth"])
    session_effort = getattr(args, "session_effort", None)
    if promised_effort and required:
        if not session_effort:
            raise CoordinatorError(
                f"the approved `{state['approval']['depth']}` depth runs its reviewers at {promised_effort} "
                "effort, and on this runtime an un-pinned reviewer inherits the SPAWNING SESSION's effort — "
                "so the packet has to state what that is. Re-run with `--session-effort "
                f"{promised_effort}` once this session is actually running at it.")
        if effort_shortfall(session_effort, promised_effort) and not getattr(args, "accept_effort_shortfall", False):
            raise CoordinatorError(
                f"this session reports running at {session_effort} effort, but the approved "
                f"`{state['approval']['depth']}` depth promises reviewers at {promised_effort}, and an "
                "un-pinned reviewer cannot exceed the session that spawns it — so this panel would "
                "under-deliver the depth the operator approved. Either raise this session's effort to "
                f"{promised_effort} and re-cut the packet, or ask the operator to re-approve at the depth "
                "this panel can actually deliver. To proceed anyway, pass --accept-effort-shortfall: the "
                "gap is then published as a hard disclosure in the pull-request body, where the operator "
                "meets it at merge.")
    stable = core.StableCommit(ROOT, f"{stage} review-packet construction") if commit else None
    if stable:
        stable_head = stable.__enter__()
        if stable_head != commit:
            raise CoordinatorError(f"{stage} review packet is not bound to the current clean commit")
    missing = [lens for lens in required if lens not in installed_names]
    if missing:
        raise CoordinatorError("required reviewers are not installed: " + ", ".join(missing))
    referent = {"schema_version": "build-review-packet.v1", "stage": stage, "raw_intent": plan["raw_intent"],
              "plan": plan, "plan_digest": state["plan"]["digest"], "intent_digest": state["plan"]["intent_digest"],
              "spec": canonical_spec, "commit": commit, "base_commit": _base() if commit else None,
              "impact": impact, "protocol_digest": _digest(protocol),
              "installed_lenses": installed_names, "required_lenses": required}
    declarations = _hard_check_declarations()
    path, digest = _write_json_artifact("build-hard-check-declarations", declarations)
    referent["hard_check_declarations"] = {"digest": digest, "count": len(declarations),
                                            "for_lens": "spec-conformance"}
    referent_digest = _digest(referent)
    contracts = review.lens_packets(referent_digest, required_contracts)
    packet = {**referent, "referent_digest": referent_digest, "reviewer_contracts": contracts}
    packet["packet_digest"] = _digest(packet)
    packet["artifacts"] = {"hard_check_declarations": {"path": path}}
    current = state["repair"] if stage == "repair" else state["reviews"][stage]
    unchanged = bool(current and current.get("packet_digest") == packet["packet_digest"])
    # NOT gated here, by operator decision after a packet-level cap wedged the Build twice: it refused the
    # re-cut that a depth change or a lens install legitimately needs, leaving no exit but faking a plan
    # edit -- a laundered consent record as the escape from a cost cap. The freeze in cmd_plan_revise is
    # the enforcement, and it sits where the measured cost actually came from: every observed second panel
    # followed a plan revision invalidating its receipts. A session voluntarily re-cutting an unchanged
    # packet was never the failure mode, and the operator's merge remains the wall.
    # Capture the checkout's git state as the review fan-out begins, so the submission preflight can
    # verify the deliverable/repair review did not mutate it (StarshipSuperjam/engine-template#947).
    checkout_baseline = review_integrity.snapshot(str(ROOT))
    # What this new packet asks its lenses to read, so a receipt already covering that range survives the
    # re-cut instead of being thrown away and re-run for nothing.
    new_base = (state["repair"]["reviewed_commit"] if stage == "repair" else packet["base_commit"])
    new_tip = commit
    covers = lambda receipt: ranges.receipt_covers(ROOT, receipt, new_base, new_tip)   # noqa: E731

    def change(s):
        old = s["repair"] if stage == "repair" else s["reviews"][stage]
        expected = {item["lens"]: item["lens_packet_digest"] for item in contracts}
        # A receipt survives on either ground: it attests THIS packet, or the range it recorded reading
        # already contains every authored commit this packet asks about. It is kept byte-identical
        # either way — never restamped onto the new packet, because the finding keys hang off its
        # digests and restamping would supersede every disposition recorded against it
        # (StarshipSuperjam/engine-template#1065, and
        # StarshipSuperjam/engine-template#1051 for why that loss matters).
        preserved_receipts = [receipt for receipt in (old or {}).get("receipts", [])
                              if receipt["lens"] in expected
                              and (receipt.get("lens_packet_digest") == expected[receipt["lens"]]
                                   or covers(receipt))]
        # The session's own effort at the moment the panel was spawned, recorded on the stage rather than
        # only on each receipt: it is a fact about the fan-out, and it is the ceiling every un-pinned
        # reviewer in that fan-out inherits. `effort_shortfall_accepted` is the operator-facing half —
        # a session that proceeded under a known gap, kept so the disclosure cannot be forgotten.
        spawn = {"session_effort": session_effort,
                 "effort_shortfall_accepted": bool(getattr(args, "accept_effort_shortfall", False))}
        if stage == "repair":
            s["repair"]["packet_digest"] = packet["packet_digest"]
            s["repair"]["referent_digest"] = referent_digest
            s["repair"]["base_commit"] = packet["base_commit"]
            s["repair"]["reviewer_contracts"] = contracts
            s["repair"]["receipts"] = preserved_receipts
            s["repair"].update(spawn)
        else:
            target = s["reviews"][stage]
            target.update({"packet_digest": packet["packet_digest"], "referent_digest": referent_digest,
                           "required_lenses": required, "installed_lenses": installed_names,
                           "reviewer_contracts": contracts, "receipts": preserved_receipts, "reviewed_commit": commit,
                           "base_commit": packet["base_commit"], **spawn})
        # A finding lives exactly as long as a receipt still demands it -- derived, for BOTH branches, from
        # the single home in `review` rather than from a per-branch stage filter. The two old filters keyed
        # on `f["stage"] != stage`, which cut both ways: a repair regeneration deleted findings whose
        # spliced receipt survived in the deliverable stage (leaving a demand `finding record` could never
        # satisfy, wedging the Build out of submission), and a DELIVERABLE regeneration dropped a spliced
        # repair receipt while leaving its findings behind (orphaned findings no receipt demanded, still
        # counting toward `blocks_this_pr` and still rendering disagreement lines into the PR body).
        # StarshipSuperjam/engine-template#1051.
        s["findings"] = review.surviving_findings(s)
        if checkout_baseline is not None:
            s["checkout_snapshot"] = checkout_baseline
    if stable:
        stable.__exit__(None, None, None)
    if not unchanged:
        store.mutate(change, from_revision=revision)
    elif checkout_baseline is not None:
        # The packet is unchanged, so receipts and findings stand — but a re-issue marks a fresh review
        # fan-out, so refresh the checkout baseline to now (the documented "re-captured at the next review
        # packet"); otherwise the preflight would compare against a stale, arbitrarily-old baseline.
        store.mutate(lambda s: s.update({"checkout_snapshot": checkout_baseline}), from_revision=revision)
    _emit_packet(packet, args)


# `review waive` is gone. Its precondition — a Build that started before its plan was reviewed — became
# unreachable when the seal became the only door: a sealed plan HAS been reviewed to its approved depth,
# and an unsealed one cannot bind at all. A waiver for a gate that cannot fail is not a safety valve; it
# is a bypass looking for a reason, so it leaves with the gate. Deliverable review was never waivable and
# still is not. `_record_plan_panel` and the `plan_panels` ledger go with it: a Build-side panel ledger
# that can never gain an entry is a field nobody can read anything true out of.


FINDINGS_BATCH_SCHEMA = ROOT / ".engine" / "schemas" / "build-findings-batch.v1.json"


def _findings_batch(source: str, stage: str, lens: str | None = None) -> list[dict]:
    """Read and validate a findings batch — the ONE file that drives both recording verbs.

    WHY A FILE (StarshipSuperjam/engine-template#1060). Recording a review round meant one invocation
    per finding, each carrying about a dozen flags, and a session assembling thirty of those in shell
    loops. In an observed build a repeated `--finding` built from a shell array collapsed into a single
    string, so the receipt demanded one bogus id and the whole step had to be redone. This is not a
    token saving — the summaries and rationales are the payload and cost the same either way — it is
    the removal of a class of quoting and ordering mistakes.

    WHY ONE FILE FOR BOTH VERBS. `review record` needs the finding IDS for its receipt and
    `finding record` needs the full dispositions; cutting them separately is how the two came to
    disagree. Naming the same file at both verbs makes the receipt's ids and the recorded findings
    the same list by construction, so they cannot drift.

    ALL OR NOTHING. Every entry is validated — schema first, then the same cross-field refusals the
    per-flag form applies — BEFORE anything is written, and the write is a single mutation. A malformed
    entry anywhere records nothing, so a half-applied batch is not a state a session can land in.
    """
    try:
        document = json.loads(_input(source))
    except ValueError as exc:
        raise CoordinatorError(f"findings batch is not JSON: {exc}") from exc
    _validate(document, FINDINGS_BATCH_SCHEMA)
    if document["stage"] != stage:
        raise CoordinatorError(
            f"this findings batch is authored for the {document['stage']} stage, but the command names "
            f"{stage}. A batch names its own stage so a file cut for one review cannot be replayed "
            "against another; re-cut it, or run the verb against the stage it was written for.")
    entries = document["findings"]
    ids = [entry["id"] for entry in entries]
    duplicates = sorted({x for x in ids if ids.count(x) > 1})
    if duplicates:
        raise CoordinatorError("a findings batch may not carry the same id twice: " + ", ".join(duplicates))
    if lens is not None:
        foreign = sorted({entry["lens"] for entry in entries if entry["lens"] != lens})
        if foreign:
            raise CoordinatorError(
                f"this batch carries findings from {', '.join(foreign)}, but the receipt being recorded is "
                f"{lens}'s. A receipt names what ITS lens found; cut one batch per lens, or record the "
                "receipt with the ids that belong to it.")
    for entry in entries:
        problem = _finding_entry_problem(entry)
        if problem:
            raise CoordinatorError(f"findings batch entry {entry['id']}: {problem} — nothing was recorded")
    return entries


def _finding_entry_problem(entry: dict) -> str | None:
    """Every cross-field refusal a single finding must survive, in one place, so the batch form and the
    per-flag form cannot enforce different rules on the same data."""
    disposition, severity = entry["disposition"], entry["severity"]
    blocks = bool(entry["blocks_this_pr"])
    if disposition == "escalated" and not entry.get("escalation_kind"):
        return "an escalated finding must name the operator-owned decision boundary (escalation_kind)"
    if disposition != "escalated" and entry.get("escalation_kind"):
        return "only an escalated finding may name an escalation boundary"
    conflict = review.disposition_conflict(disposition, blocks)
    if conflict:
        return conflict
    if severity == "blocking" and not review.blocks_submission(
            {"disposition": disposition, "blocks_this_pr": blocks}) and not entry.get("operator_summary"):
        return ("a blocking finding that no longer blocks this pull request needs a safe operator-facing "
                "operator_summary — that text is what the disagreement line published at merge is built from")
    return None


def _receipt_finding_ids(args) -> list[str]:
    """The ids a receipt demands: from the batch file when one is named, else the repeatable flag.

    Refusing BOTH is deliberate. Two sources for one list is exactly the ambiguity this change removes;
    a session that has a batch file should name it and nothing else."""
    batch = getattr(args, "findings_from_file", None)
    if batch and args.finding:
        raise CoordinatorError(
            "name the ids with --findings-from-file or with --finding, not both — two sources for one "
            "list is the ambiguity the batch form exists to remove")
    if batch:
        return [entry["id"] for entry in _findings_batch(batch, args.stage, args.lens)]
    return list(args.finding or [])


def cmd_review_record(args, store: Snapshot) -> None:
    finding_ids = sorted(set(_receipt_finding_ids(args)))
    delivered_effort = getattr(args, "delivered_effort", None)

    def change(state):
        if args.stage == "repair":
            target = state["repair"]
            if not target or target["packet_digest"] != args.packet_digest:
                raise CoordinatorError("repair receipt does not match the current repair packet")
            if args.lens not in target["lenses"]:
                raise CoordinatorError(f"{args.lens} was not requested for this repair review")
            contract = next((item for item in target["reviewer_contracts"] if item["lens"] == args.lens), None)
            if not contract:
                raise CoordinatorError(f"no current reviewer contract for {args.lens}")
            if getattr(args, "lens_packet_digest", None) != contract["lens_packet_digest"]:
                raise CoordinatorError("review receipt does not attest the current reviewer contract packet")
            receipt = {"lens": args.lens, "packet_digest": args.packet_digest,
                       "referent_digest": target["referent_digest"],
                       "lens_packet_digest": contract["lens_packet_digest"],
                       "commit": target["final_commit"], "finding_ids": finding_ids,
                       "code_execution": args.code_execution,
                       # What this lens actually READ, so a later re-bind can ask whether anything in the
                       # new range is new to it instead of assuming everything is.
                       "reviewed_range": {"base": target["reviewed_commit"], "tip": target["final_commit"]},
                       "delivered_effort": delivered_effort}
            target["receipts"] = [r for r in target["receipts"] if r["lens"] != args.lens] + [receipt]
            delivery = state["reviews"]["deliverable"]
            delivery["receipts"] = [r for r in delivery["receipts"] if r["lens"] != args.lens] + [receipt]
            delivery["reviewer_contracts"] = [
                item for item in delivery["reviewer_contracts"] if item["lens"] != args.lens
            ] + [contract]
            if not _outstanding_repair_lenses(target):
                # `base_commit` advances WITH `reviewed_commit`, never behind it. Advancing only the
                # reviewed commit left the pair naming two different points in history, so any later
                # measurement across `base_commit..reviewed_commit` spanned a wider range than the branch
                # actually contributed and swept in upstream commits on one side only.
                delivery["reviewed_commit"] = target["final_commit"]
                if target.get("base_commit"):
                    delivery["base_commit"] = target["base_commit"]
        else:
            if args.stage != "deliverable":
                raise CoordinatorError(
                    "the Build Coordinator records one review, and it is the deliverable review; plan "
                    "review is recorded on the plan side, before the seal")
            target = state["reviews"]["deliverable"]
            if target["packet_digest"] != args.packet_digest:
                raise CoordinatorError("receipt does not match the current review packet")
            if args.lens not in target["required_lenses"]:
                raise CoordinatorError(f"{args.lens} was not required by the approved depth")
            contract = next((item for item in target["reviewer_contracts"] if item["lens"] == args.lens), None)
            if not contract:
                raise CoordinatorError(f"no current reviewer contract for {args.lens}")
            if getattr(args, "lens_packet_digest", None) != contract["lens_packet_digest"]:
                raise CoordinatorError("review receipt does not attest the current reviewer contract packet")
            receipt = {"lens": args.lens, "packet_digest": args.packet_digest,
                       "referent_digest": target["referent_digest"],
                       "lens_packet_digest": contract["lens_packet_digest"],
                       "commit": target["reviewed_commit"], "finding_ids": finding_ids,
                       "code_execution": args.code_execution,
                       "reviewed_range": {"base": target["base_commit"], "tip": target["reviewed_commit"]},
                       "delivered_effort": delivered_effort}
            target["receipts"] = [r for r in target["receipts"] if r["lens"] != args.lens] + [receipt]
    store.mutate(change)
    shortfall = _effort_shortfall_lines(store.read())
    for line in shortfall:
        print("disclosure: " + line, file=sys.stderr)
    print(f"recorded {args.stage} review from {args.lens} with {len(finding_ids)} finding(s)")


def _finding_entry_from_args(args) -> dict:
    return {"id": args.id, "lens": args.lens, "severity": args.severity, "summary": args.summary,
            "disposition": args.disposition, "rationale": args.rationale,
            "escalation_kind": args.escalation_kind,
            "blocks_this_pr": bool(getattr(args, "blocks_this_pr_stated", None)),
            "handoff_summary": args.handoff_summary,
            "operator_summary": getattr(args, "operator_summary", None),
            "private_reference": getattr(args, "private_reference", None)}


def cmd_finding_record(args, store: Snapshot) -> None:
    if getattr(args, "from_file", None):
        if args.id:
            raise CoordinatorError(
                "record a batch with --from-file or a single finding with the per-finding flags, not "
                "both — a batch names its own findings")
        entries = _findings_batch(args.from_file, args.stage)
    else:
        if not args.id:
            raise CoordinatorError("record a finding with --id … or a whole round with --from-file <file|->")
        # STATED, never defaulted. The blocking choice stopped being an argparse-required group so the
        # batch form could run without it, and argparse resolves an omitted store_true/store_false pair to
        # False — so a session that simply forgot the flag recorded the finding as NOT holding the pull
        # request, silently. A submission gate must not fail toward permitting, and argparse cannot express
        # "required unless --from-file", so the requirement lives here where it can
        # (StarshipSuperjam/engine-template#1060's batch form must not cost
        # StarshipSuperjam/engine-template#1012's gate).
        if getattr(args, "blocks_this_pr_stated", None) is None:
            raise CoordinatorError(
                "say whether this finding holds the pull request: --blocks-this-pr or "
                "--does-not-block-this-pr. It is not defaulted, because the default that argparse would "
                "pick is 'does not block' — and a finding recorded as not blocking because nobody said "
                "otherwise is exactly the silence the submission gate exists to break.")
        missing = [name for name in ("lens", "severity", "summary", "disposition", "rationale")
                   if not getattr(args, name, None)]
        if missing:
            raise CoordinatorError(
                "a single finding needs " + ", ".join("--" + m for m in missing)
                + ". (Recording a whole round? `--from-file <file|->` takes them all from one "
                  "build-findings-batch.v1 file and needs none of these flags.)")
        entries = [_finding_entry_from_args(args)]
        problem = _finding_entry_problem(entries[0])
        if problem:
            raise CoordinatorError(problem)

    def change(state):
        # A disposition must be recordable against the packet ITS OWN RECEIPT names, not only against the
        # live one. A receipt can outlive the packet that produced it -- a repair receipt is spliced into
        # the deliverable stage and survives a repair-packet regeneration, and a `none` judgment clears the
        # repair packet entirely -- and `missing_findings` keeps demanding that receipt's ids at its own
        # digest. Deriving the target from the live packet alone left those ids permanently unrecordable
        # and the Build wedged out of submission; the same lookup also replaces a bare KeyError on
        # `--stage repair` with an empty repair slot. StarshipSuperjam/engine-template#1051.
        demanded = review.demanded_findings(state)
        live = state["repair"] if args.stage == "repair" and state["repair"] else state["reviews"].get(args.stage)
        recorded = []
        for entry in entries:
            lens, finding_id = entry["lens"], entry["id"]
            by_receipt = next((
                (produced_by, receipt) for produced_by, receipt in review.live_receipts(state)
                if produced_by == args.stage and receipt["lens"] == lens
                and finding_id in receipt["finding_ids"]), None)
            contract = None if not live else next(
                (item for item in live["reviewer_contracts"] if item["lens"] == lens), None)
            live_offers = bool(live and live["packet_digest"] and contract and lens in (
                live["lenses"] if args.stage == "repair" else live["required_lenses"]))
            # THE RECEIPT THAT DEMANDED IT WINS. A finding belongs to the review that raised it, so its
            # key is the key of that receipt — even when a live packet would also offer the lens. Reading
            # the live packet first meant that anything advancing the stage's reviewed commit between the
            # receipt and the disposition (a completed repair round splicing forward, a packet re-cut)
            # stamped the finding with a NEWER commit than its own receipt, so `missing_findings` demanded
            # it forever and no amount of re-recording the finding could satisfy it — the fix was to
            # re-record the RECEIPT and its siblings together, which no message ever said
            # (StarshipSuperjam/engine-template#1012).
            if by_receipt:
                receipt = by_receipt[1]
                packet = receipt["packet_digest"]
                lens_packet_digest = receipt.get("lens_packet_digest")
                commit = receipt["commit"]
            elif live_offers:
                packet = live["packet_digest"]
                lens_packet_digest = contract["lens_packet_digest"]
                commit = live["final_commit"] if args.stage == "repair" else live["reviewed_commit"]
            elif finding_id in demanded:
                raise CoordinatorError(
                    f"no live {args.stage} receipt from {lens} demands {finding_id} — the id is demanded by "
                    "a different lens or stage; record it against the one whose receipt names it")
            elif not live or not live["packet_digest"]:
                raise CoordinatorError(f"no current {args.stage} review packet")
            else:
                raise CoordinatorError(f"{lens} was not requested by the current {args.stage} packet")
            recorded.append({"id": finding_id, "stage": args.stage, "lens": lens, "packet_digest": packet,
                             "lens_packet_digest": lens_packet_digest, "commit": commit,
                             "severity": entry["severity"], "summary": entry["summary"],
                             "disposition": entry["disposition"], "rationale": entry["rationale"],
                             "escalation_kind": entry.get("escalation_kind"),
                             "blocks_this_pr": bool(entry["blocks_this_pr"]),
                             "handoff_summary": entry.get("handoff_summary"),
                             "operator_summary": entry.get("operator_summary"),
                             "private_reference": entry.get("private_reference")})
        # One assignment for the whole batch: every entry was validated and resolved above, so nothing
        # can land half-written.
        replaced = {finding["id"] for finding in recorded}
        state["findings"] = [f for f in state["findings"] if f["id"] not in replaced] + recorded
    store.mutate(change)
    print(f"recorded {len(entries)} disposition(s) ({', '.join(e['id'] for e in entries)}); "
          "reviewer severity did not choose the remedy")


def cmd_assumption_dispose(args, store: Snapshot) -> None:
    """Resolve a plan assumption authored 'unresolved' to 'verified' or 'accepted-risk' in the receipt layer,
    bound to a --basis, WITHOUT editing the plan (StarshipSuperjam/engine-template#1014). Mirrors
    cmd_finding_record: store.mutate never touches state['plan']['digest'], so approval and every review
    receipt survive — the honest submit path no longer forces a full review re-run to clear a wall the review
    already settled. The disposition is disclosed at merge (see _status); it is a self-attested judgment, not a
    silent skip, and the operator's merge stays the binding gate."""
    plan = _plan(args.plan)
    state = store.read()
    _assert_plan(state, plan)
    claim = args.claim.strip()
    if not claim:
        raise CoordinatorError("an assumption disposition needs a non-empty --claim")
    if not args.basis.strip():
        raise CoordinatorError("a disposition needs a --basis stating how the assumption was resolved")
    authored = {item["claim"]: item["status"] for item in plan.get("assumptions", [])}
    if claim not in authored:
        raise CoordinatorError(
            "no assumption with that exact claim in the approved plan (match the claim text verbatim): " + claim)
    if authored[claim] != "unresolved":
        raise CoordinatorError(
            f"this assumption is already authored '{authored[claim]}' in the approved plan — it is not "
            f"unresolved, so there is nothing to dispose and no action is needed. (Disposition only clears an "
            f"assumption authored 'unresolved'. To change a premise the plan already settled is a real plan "
            f"change — 'plan revise' — which correctly re-opens approval and review.)")

    def change(state):
        entry = {"claim": claim, "resolved_as": args.resolved_as, "basis": args.basis.strip()}
        kept = [d for d in state.get("assumption_dispositions", []) if d["claim"] != claim]
        state["assumption_dispositions"] = kept + [entry]

    store.mutate(change)
    print(f"resolved assumption after approval: {claim} -> {args.resolved_as}; it clears the "
          "engineering-decision hold without re-running review, and is disclosed at merge")


def _changed_paths(base: str) -> list[str]:
    paths = set(_must_run(["git", "diff", "--name-only", f"{base}..HEAD"]).splitlines())
    paths.update(_must_run(["git", "diff", "--name-only"]).splitlines())
    paths.update(_must_run(["git", "diff", "--cached", "--name-only"]).splitlines())
    return sorted(x for x in paths if x)


def cmd_checkpoint(args, store: Snapshot) -> None:
    plan = _plan(args.plan)
    try:
        note = json.loads(_input(args.input))
    except ValueError as exc:
        raise CoordinatorError(f"checkpoint input is not JSON: {exc}") from exc
    required = {"current_work", "work_item", "remaining_verification", "judgment"}
    if plan["profile"] != "trivial":
        required.update({"objective", "assumptions", "non_goals", "planned_scope"})
    if not required.issubset(note):
        raise CoordinatorError("checkpoint is missing: " + ", ".join(sorted(required - set(note))))
    def change(state):
        _assert_plan(state, plan)
        _assert_spec_boundary(state, plan, allow_same_session_offline=True)
        if not state["approval"]:
            raise CoordinatorError("the Build gate is not approved")
        unearned = _unearned_completions(state)
        if unearned:
            raise CoordinatorError(
                "this v2 snapshot records completions no integration earned (" + ", ".join(unearned)
                + "); " + _UNEARNED_COMPLETION_REMEDY)
        items = {item["id"]: item for item in plan["work_items"]}
        if note["work_item"] not in items:
            raise CoordinatorError(f"checkpoint work item {note['work_item']} is not in the approved plan")
        completed = {item["id"] for item in state["progress"]["completed"]}
        next_item = _next_incomplete(plan, state)
        # A graph's "next" is dependency READINESS, and this names the same concept the operation doc
        # and the status render use.
        if plan["profile"] == "routine" and next_item and note["work_item"] != next_item:
            raise CoordinatorError(f"Routine must advance the next ready work item {next_item}")
        state["progress"]["current_item"] = note["work_item"]
        note.setdefault("objective", plan["objective"])
        note.setdefault("assumptions", [])
        note.setdefault("non_goals", [])
        note.setdefault("planned_scope", items[note["work_item"]]["paths"])
        note.update({"plan_digest": state["plan"]["digest"], "changed_paths": _changed_paths(state["build"]["base_at_bind"]),
                     "progress": f"{len(completed)} of {len(items)} planned work items complete"})
        state["checkpoint"] = note
    store.mutate(change)
    if getattr(args, "json", False):
        # Carry the reminder in the JSON payload too (see cmd_status); the stored checkpoint note is
        # unchanged — this is a display-only field for a --json consumer.
        print(json.dumps({**note, "coordinator_reminder": _COORDINATOR_OWNED_REMINDER}, indent=2, sort_keys=True))
    else:
        print(f"checkpoint {note['judgment']}: {note['work_item']}; {note['progress']}; "
              f"{len(note['changed_paths'])} changed path(s), {len(note['remaining_verification'])} verification item(s) remain")
        print(_COORDINATOR_OWNED_REMINDER)


def _derived_drift() -> list:
    """Read-only: the derived members whose committed output is stale against source (drift or a
    fail-closed error). The one seam the validation pre-gate consults — it never regenerates."""
    sys.path.insert(0, str(ROOT / ".engine" / "tools"))
    import derived_state
    return [d for d in derived_state.verify() if d.status in ("drift", "error")]


def cmd_validate(args, store: Snapshot) -> None:
    state = store.read()
    revision = state["revision"]
    unearned = _unearned_completions(state)
    if unearned:
        raise CoordinatorError(
            "this v2 snapshot records completions no integration earned (" + ", ".join(unearned)
            + "); " + _UNEARNED_COMPLETION_REMEDY)
    # A DAG Build's validation is evidence about the WHOLE graph, so it cannot be earned while a node is
    # still outstanding: a green suite over a half-built graph reads at merge as though the plan were
    # done. The roster lives only in the plan, so a v2 snapshot must be handed the approved plan here —
    # the same exact-artifact discipline `checkpoint` and `status` already keep (BC-11).
    if state.get("schema_version") == "build-state.v2":
        if not getattr(args, "plan", None):
            raise CoordinatorError(
                "final validation of a build-plan.v2 Build needs the approved plan to know its node "
                "roster — re-run with `validate --plan <plan.json>`")
        plan = _plan(args.plan)
        _assert_plan(state, plan)
        outstanding = [f"{node_id} ({node['state']})"
                       for node_id, node in sorted(dag.derive_lifecycle(plan, state).items())
                       if node["state"] != dag.COMPLETE]
        if outstanding:
            raise CoordinatorError(
                "final validation cannot become evidence while a work item is unintegrated: "
                + "; ".join(outstanding)
                + ". Integrate every node first — focused verification is the tool for a partial graph.")
    # Fail-fast pre-gate (read-only), BEFORE the expensive StableCommit run: if a derived artifact is stale,
    # refuse naming the exact remedy rather than spending the full CI suite + self-tests only to go red on a
    # drift check. This is NOT a new hard hold (eADR-0041) — it is a cheap early refusal; CI's drift checks
    # remain the authority, and the sync-artifacts step is what a session runs to clear it.
    drift = _derived_drift()
    if drift:
        detail = "; ".join(f"{d.path} ({d.status})" for d in drift[:6])
        # A `drift` status means the committed output is stale — sync-artifacts fixes it. An `error` status
        # means the drift check itself could not evaluate (a broken generator/check); sync may not clear that,
        # so name the distinct remedy rather than send a session in a loop.
        if any(d.status == "error" for d in drift):
            raise CoordinatorError(
                "a derived-artifact drift check could not evaluate (an error, not plain drift) — investigate "
                "the named generator/check; `sync-artifacts` may not clear it: " + detail)
        raise CoordinatorError(
            "derived artifacts are stale, so validation would fail its drift checks — run "
            "`build_coordinator.py sync-artifacts` first, then re-run validate: " + detail)
    results = []
    with core.StableCommit(ROOT, "validation") as head:
        for item in _protocol()["validation_commands"]:
            stamp = f"{int(time.time())}-{item['id']}-{head[:12]}-{secrets.token_hex(6)}.log"
            log_path = Path(__import__("tempfile").gettempdir()) / stamp
            returncode = _run_validation(item["command"], log_path)
            log_digest = _digest(log_path.read_bytes())
            summary = f"exit {returncode}; complete log at {log_path} ({log_digest})"
            results.append({"id": item["id"], "commit": head, "passed": returncode == 0,
                            "summary": summary, "log_path": str(log_path), "log_digest": log_digest})
    store.mutate(lambda s: s.update({"validation": {"commit": head, "results": results}}),
                 from_revision=revision)
    print(json.dumps({"commit": head, "results": results}, indent=2, sort_keys=True))
    if not all(x["passed"] for x in results):
        raise CoordinatorError("validation failed; the failed results remain recorded")


def _sync_changed_paths() -> list:
    """(status_code, repo-relative path) for every path git sees as changed — modified (` M`), deleted
    (` D`), or untracked (`??`). A rename's destination is taken. The porcelain lines come from the same
    `git status --porcelain=v1 --untracked-files=all` the clean-tree guard reads."""
    out = []
    for line in core.dirty_paths(ROOT):
        xy, path = line[:2], line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        out.append(("??" if xy == "??" else xy.strip(), path))
    return out


def _rmdir_empty_parents(start: Path) -> None:
    """Remove `start` and its ancestors while they are empty directories strictly under ROOT — tidies the
    dir a pruned untracked file left behind, so a failed sync leaves no empty-dir debris the clean-tree guard
    cannot see (git does not track empty dirs). Stops at the first non-empty dir, at ROOT, or on any error."""
    current = start
    try:
        root_resolved = ROOT.resolve()
        while current.resolve() != root_resolved and root_resolved in current.resolve().parents:
            if any(current.iterdir()):
                return
            current.rmdir()
            current = current.parent
    except OSError:
        return


def cmd_sync_artifacts(args, store: Snapshot) -> None:
    """Transactional artifact preparation: regenerate the built-in derived members in declared order and
    commit them, so the read-only `validate` (which refuses a dirty or moved tree) then runs against a
    current tree. Refuses a dirty tree at entry (the proven executor precondition that makes restore
    trivially correct); refuses AND restores exactly if a generator writes outside its declared outputs;
    records a receipt bound to the resulting sync commit. Never a blanket `git clean` or a whole-tree
    checkout — the restore is scoped to THIS sync's own enumerated footprint (so a concurrent peer's
    unrelated tracked edit on a shared checkout is never reverted), and the enumerated new untracked paths
    are removed by name with their now-empty parent dirs."""
    import shutil
    sys.path.insert(0, str(ROOT / ".engine" / "tools"))
    import derived_state
    state = store.read()
    revision = state["revision"]
    dirty = core.dirty_paths(ROOT)
    if dirty:
        raise CoordinatorError("sync-artifacts requires a clean working tree; commit or remove: "
                               + ", ".join(d[3:] for d in dirty[:8]))
    # The declared-output guard: a changed path is legitimate iff a registry member owns it (exact file, or
    # an EXCLUSIVE tree by directory-boundary prefix) OR it is a dynamic member's concrete output.
    dynamic_files = {o.path for m in derived_state.MEMBERS if m.dynamic
                     for o in derived_state._concrete_outputs(m, str(ROOT))}

    def _declared(path: str) -> bool:
        return derived_state.owner_of(path) is not None or path in dynamic_files

    results = derived_state.regenerate()            # in declared order, scope-aware, import dispatch
    failed = [r for r in results if r.status == "failed"]
    changed = _sync_changed_paths()
    undeclared = [p for _code, p in changed if not _declared(p)]

    def _restore() -> None:
        # Undo only the tracked mods/deletions THIS sync produced — NOT `git checkout -- .`, which would also
        # revert a concurrent peer's unrelated tracked edit on a shared checkout (SG-F1). The tree was clean
        # at entry, so `changed` is exactly this sync's footprint.
        tracked = [p for code, p in changed if code != "??"]
        if tracked:
            core.run(["git", "checkout", "--", *tracked], root=ROOT)
        for code, p in changed:
            if code == "??":                                    # remove exactly the generator's new files
                fp = ROOT / p
                if fp.is_dir():
                    shutil.rmtree(fp, ignore_errors=True)
                elif fp.exists():
                    fp.unlink()
                _rmdir_empty_parents(fp.parent)                 # and the dir it left empty (no debris)

    if failed or undeclared:
        _restore()
        why = ("a generator failed: " + "; ".join(f"{r.path}: {r.error}" for r in failed)) if failed else \
              ("a generator wrote outside its declared outputs: " + ", ".join(sorted(set(undeclared))[:8]))
        raise CoordinatorError("sync-artifacts refused and restored the tree — " + why)

    committed = bool(changed)
    if committed:
        paths = sorted({p for _code, p in changed})
        if core.run(["git", "add", "--", *paths], root=ROOT).returncode:
            _restore()
            raise CoordinatorError("sync-artifacts could not stage the regenerated outputs; tree restored")
        core.run(["git", "-c", "user.email=engine@local", "-c", "user.name=engine",
                  "commit", "-m", "Regenerate derived artifacts"], root=ROOT)
    head = core.head(ROOT)
    receipt = {"commit": head,
               "results": [{"path": r.path, "status": r.status, "changed": r.changed} for r in results]}
    store.mutate(lambda s: s.update({"artifact_sync": receipt}), from_revision=revision)
    print(json.dumps({"commit": head, "committed": committed,
                      "regenerated": [r.path for r in results if r.changed]}, indent=2, sort_keys=True))


def _repair_round_complete(repair: dict | None) -> bool:
    """Whether a repair round finished the re-review it asked for. A `none` judgment requests no lenses
    and so never satisfies this -- it terminates the loop without re-review rather than completing one.
    Single-homed because two readers need it and a second copy could drift: the reviewed-commit advance
    below, and the round ledger. A drift would surface only on a third round, which is exactly the path
    the escalation gate creates."""
    if not repair or repair["judgment"] == "none":
        return False
    return not _outstanding_repair_lenses(repair)


class _Unmeasurable(Exception):
    """The reconcile could not establish what the branch contributed. Never a free re-anchor."""


def _commit_present(sha: str) -> bool:
    """True when the object is still readable. Routed through core.run with the module-level ROOT read at
    CALL time -- `_run`'s `cwd` default binds ROOT at import, so a probe written that way would answer from
    the engine's own checkout under test and certify a measurement it never made."""
    return core.run(["git", "cat-file", "-e", sha + "^{commit}"], root=ROOT).returncode == 0


def _is_ancestor(candidate: str, of: str) -> bool:
    return core.run(["git", "merge-base", "--is-ancestor", candidate, of], root=ROOT).returncode == 0


def _base_or_none() -> str | None:
    """The merge base, or None where it cannot be resolved. `_base` raises, and `_status` -- the read-only
    command a stuck session runs FIRST -- must report where the Build stands rather than crash in a
    checkout with no `origin/HEAD` (a disposable demo repo, a bare-ish clone)."""
    try:
        return _base()
    except CoordinatorError:
        return None


def _tree_entry(commit: str, path: str) -> tuple[str, str] | None:
    """(mode, blob-or-tree oid) for one path at one commit, or None when absent. Exact: no diff, no
    normalization, no textconv, no rename heuristics -- so a whitespace-only edit, a swapped binary, and a
    mode flip are all visible, none of which `git patch-id` can see."""
    out = core.run(["git", "ls-tree", "-z", commit, "--", path], root=ROOT)
    if out.returncode != 0:
        raise _Unmeasurable(f"could not read `{path}` at `{commit[:12]}`")
    line = (out.stdout or "").split("\0")[0].strip()
    if not line:
        return None
    meta = line.split("\t", 1)[0].split()
    if len(meta) < 3:
        raise _Unmeasurable(f"unreadable tree entry for `{path}` at `{commit[:12]}`")
    return meta[0], meta[2]


def _touched_paths(base: str, tip: str) -> list[str]:
    out = core.run(["git", "diff", "--name-only", "-z", f"{base}..{tip}"], root=ROOT)
    if out.returncode != 0:
        raise _Unmeasurable(f"could not list the paths between `{base[:12]}` and `{tip[:12]}`")
    return [x for x in (out.stdout or "").split("\0") if x]


def _contribution_divergence(base_before: str, from_commit: str, base_after: str, to_commit: str) -> list[str]:
    """The paths where the branch's own contribution is NOT provably unchanged across a history rewrite.

    For every path either side touches, the contribution is unchanged only when the upstream side is
    identical (`base_before` and `base_after` agree on it) AND the result is identical (`from_commit` and
    `to_commit` agree on it). Both comparisons are on exact tree entries, so nothing is normalized away.
    Where upstream itself moved under a path the branch touched, the rebase produced real content that no
    reviewer has read, and this reports the path as divergent rather than guessing.

    An earlier design compared `git patch-id --stable` sets here. It was replaced before implementation:
    patch-id strips whitespace before hashing, so `x = 1` and an indented `x = 1` share an id -- in a
    Python tree that is a semantic change measuring as identical, and it was the sole safeguard on a free
    re-anchor of review evidence. It is also blind to binary content and mode changes, has no per-file
    form, and cannot be combined with `--verbatim`. Exact tree entries have none of those properties.
    """
    for sha in (base_before, from_commit, base_after, to_commit):
        if not sha or not _commit_present(sha):
            raise _Unmeasurable(f"commit `{(sha or '?')[:12]}` is no longer readable")
    paths = sorted(set(_touched_paths(base_before, from_commit)) | set(_touched_paths(base_after, to_commit)))
    if not paths:
        # Nothing measured is not the same as nothing changed. Never a free pass.
        raise _Unmeasurable("neither side touches any path, so there is no contribution to compare")
    divergent = []
    for path in paths:
        if (_tree_entry(base_before, path) != _tree_entry(base_after, path)
                or _tree_entry(from_commit, path) != _tree_entry(to_commit, path)):
            divergent.append(path)
    return divergent


def _effective_reviewed(state: dict) -> str | None:
    """The commit the deliverable review currently stands on: the reviewed commit, advanced to a completed
    repair round's final commit. Single-homed -- `cmd_repair_assess` and `cmd_reconcile` must agree.

    The advance holds only while that final commit is still ON the branch. A repair record is history: it
    describes a round that happened, and a rewrite does not un-happen it, so its commits are deliberately
    left as recorded for the audit trail. But once they name orphans they are no longer a live anchor.
    Preferring an orphan here made `repair assess` measure `orphan..head` -- a span carrying the upstream
    commits the rebase pulled in, exactly the diff its own refusal text calls meaningless -- and write a
    repair record whose `reviewed_commit` could never satisfy `repair_ready`, so the session had to assess
    twice and one fabricated round counted against the escalation threshold."""
    reviewed = state["reviews"]["deliverable"]["reviewed_commit"]
    prior = state["repair"]
    if _repair_round_complete(prior):
        final = prior["final_commit"]
        # SUPERSESSION retires a repair anchor, not orphanhood. A rebase orphans the round's final commit,
        # but the commit stays readable and is still exactly what was last reviewed, so it remains the
        # right thing to measure a rewrite FROM -- demoting it merely because it left the branch made
        # `reconcile` compare against a pre-repair commit, reporting the round's own files as divergent
        # and denying the clean re-anchor to precisely the Build that had repaired before rebasing.
        # Once a reconcile has re-anchored past that commit, the deliverable binding it wrote is newer and
        # the repair record is history; anchoring on it there made `repair assess` measure `orphan..head`,
        # a span carrying the upstream commits the rebase pulled in, and burn a fabricated round.
        superseded = any(item["from_commit"] == final for item in state.get("reconciles", []))
        if not superseded:
            return final
    return reviewed


def _history_was_rewritten(state: dict, head: str) -> bool:
    """The reviewed commit is no longer on the branch AND the branch sits on a different base -- the
    signature of a rebase, as distinct from ordinary forward progress or an amend in place."""
    reviewed = _effective_reviewed(state)
    if not reviewed or reviewed == head:
        return False
    # Both ends must be READABLE before any conclusion is drawn. `merge-base --is-ancestor` exits non-zero
    # for "not an ancestor" and for "no such object" alike, so an unreadable commit would otherwise look
    # exactly like a rewrite and hijack the ordinary repair path on a failed probe.
    if not _commit_present(reviewed) or not _commit_present(head):
        return False
    if _is_ancestor(reviewed, head):
        return False
    recorded_base = state["reviews"]["deliverable"].get("base_commit")
    current_base = _base_or_none()
    return bool(recorded_base) and bool(current_base) and recorded_base != current_base


def cmd_reconcile(args, store: Snapshot) -> None:
    """Re-anchor the deliverable review's commit bindings after a diff-preserving history rewrite.

    The merge-freshness floor and a linear-history ruleset together make a rebase the required reconcile
    for an already-reviewed branch, and a rebase rewrites the very SHAs the review evidence is bound to
    (StarshipSuperjam/engine-template#1000). The receipts themselves survive -- they bind to packet
    digests -- so what breaks is narrower than the audit trail: the coordinator can no longer tell what
    the branch contributed, and demands a fresh judgment for a diff nobody changed.

    There are exactly two outcomes and no operator-typed escape between them. When the branch's own
    contribution is provably identical, the bindings move to the new head and the reconcile is published.
    When it is not -- or cannot be measured -- the bindings move only as far as the NEW BASE, which leaves
    `reviewed_commit != head` and hands the session straight back to `repair assess`, now against a
    meaningful `base_after..head` diff instead of an orphaned one. That path consumes a repair round, arms
    the escalation gate, and publishes the reviewed-vs-submitted line, so the weaker outcome is the one
    carrying MORE scrutiny, not less. A session cannot spend a string to skip re-review here."""
    head = _head()
    state = store.read()
    revision = state["revision"]
    plan = _plan(args.plan)
    _assert_plan(state, plan)
    delivery = state["reviews"]["deliverable"]
    reviewed = _effective_reviewed(state)
    if not reviewed:
        raise CoordinatorError("deliverable review has not recorded a reviewed commit; there is nothing to re-anchor")
    if reviewed == head:
        raise CoordinatorError("the reviewed commit is already the current head; nothing was rewritten")
    if _is_ancestor(reviewed, head):
        raise CoordinatorError(
            "the reviewed commit is still on this branch, so this is ordinary post-review divergence, not a "
            "history rewrite — record a proportional judgment with `repair assess` instead")
    base_before = delivery.get("base_commit")
    base_after = _base()
    if not base_before:
        raise CoordinatorError("the deliverable review recorded no base commit, so a rewrite cannot be measured")
    if base_before == base_after:
        raise CoordinatorError(
            "the branch still sits on the same base, so the reviewed commit was amended rather than rebased — "
            "that is a real content change and belongs to `repair assess`")
    try:
        divergent = _contribution_divergence(base_before, reviewed, base_after, head)
        unmeasurable = None
    except _Unmeasurable as exc:
        divergent, unmeasurable = [], str(exc)
    identical = unmeasurable is None and not divergent
    entry = {"from_commit": reviewed, "to_commit": head, "base_before": base_before,
             "base_after": base_after, "contribution_identical": identical,
             "divergent_paths": divergent, "unmeasurable": unmeasurable,
             # Where the binding ACTUALLY landed. On the divergent path it moves only as far as the new
             # base, so a disclosure that assumed `to_commit` would overstate what was re-anchored.
             "anchored_to": head if identical else base_after}
    # Re-capture the checkout baseline: the rewrite this verb has just recorded as legitimate would
    # otherwise surface as advisory integrity drift against a snapshot taken before it.
    checkout_baseline = review_integrity.snapshot(str(ROOT))

    def change(s):
        d = s["reviews"]["deliverable"]
        # The base moves on BOTH paths, so the pair (reviewed_commit, base_commit) never names two
        # different points in history -- otherwise a second rewrite in one Build measures a wider span on
        # one side and can never reach the identical path.
        d["base_commit"] = base_after
        d["reviewed_commit"] = head if identical else base_after
        s["reconciles"] = list(s.get("reconciles", [])) + [entry]
        # `state["repair"]` is deliberately NOT cleared. It is the sole producer of the PR body's
        # reviewed-vs-submitted line, and clearing it made a Build that HAD run a repair round publish
        # "no post-review repair was needed; the reviewed and submitted commits are the same" -- false on
        # both halves, at the operator's only consent surface.
        # A body composed before this reconcile must not carry into readiness: the contract's freshness
        # keys on head, and a reconcile does not move head, so nothing else would invalidate it.
        s["pr_contract"] = None
        s["checkout_snapshot"] = checkout_baseline

    store.mutate(change, from_revision=revision)
    if identical:
        print(f"re-anchored the deliverable review from {reviewed[:12]} to {head[:12]}: the branch's own "
              f"contribution is unchanged across the rewrite, verified on exact tree entries for every "
              f"path either side touches. Recompose the PR body before submitting.")
    else:
        why = unmeasurable or ("the branch's contribution differs at: " + ", ".join(divergent))
        print(f"re-anchored the deliverable review onto the new base {base_after[:12]} — {why}. The reviewed "
              f"commit is not the head, so record a proportional judgment with `repair assess`; its diff now "
              f"spans the branch as it actually stands. Recompose the PR body before submitting.")


def cmd_repair_assess(args, store: Snapshot) -> None:
    head = _head()
    state = store.read()
    revision = state["revision"]
    prior = state["repair"]
    reviewed = _effective_reviewed(state)
    if not reviewed:
        raise CoordinatorError("deliverable review has not recorded a reviewed commit")
    if _history_was_rewritten(state, head):
        # Refused HERE rather than measured: the reviewed commit is off the branch and the base has moved,
        # so `reviewed..head` describes upstream history rather than this Build's work. Judging that diff
        # spends a repair round on a summary that means nothing.
        raise CoordinatorError(
            "the reviewed commit is no longer on this branch and the base has moved, so this branch's "
            "history was rewritten — `reviewed..head` would summarise upstream commits, not your work. "
            "Run `reconcile` first to re-anchor the review bindings; it will hand this back to you with a "
            "diff that spans the branch as it actually stands.")
    try:
        summary = _must_run(["git", "diff", "--shortstat", f"{reviewed}..{head}"]).strip() or "no textual diff"
    except CoordinatorError as exc:
        # Derived from the real failure rather than a pre-check, so this cannot disagree with what git
        # actually did. A garbage-collected anchor otherwise surfaced as a raw "Invalid revision range".
        if not _commit_present(reviewed):
            raise CoordinatorError(
                f"the commit this review stands on (`{reviewed[:12]}`) is no longer readable in this "
                "checkout, so the reviewed-to-final divergence cannot be measured. Recover it (fetch the "
                "branch, or restore it from a reflog) and re-run; if it is genuinely gone, re-run the "
                "deliverable review against the current head rather than recording a judgment on a span "
                "that cannot be computed.") from exc
        raise
    lenses = sorted(set(args.lens or []))
    if args.judgment == "none" and lenses:
        raise CoordinatorError("a none judgment cannot request review lenses")
    if args.judgment == "scoped" and not lenses:
        raise CoordinatorError("a scoped judgment must name at least one --lens")
    if args.judgment == "full":
        lenses = [item["lens"] for item in _required(_protocol(), "thorough", _installed())]
    # A round is counted when it STARTS, and counted irrespective of judgment. Counting only completed
    # scoped/full rounds would leave two holes: an abandoned fan-out costs full price yet would count
    # zero, and gating only the reviewed path would make `none` -- "no re-review needed" -- the
    # frictionless exit at the exact moment cost pressure peaks, which is the "accept the breaks and
    # merge" outcome this gate exists to prevent. Re-assessing the SAME divergence (upgrading a scoped
    # judgment to full, say) replaces its entry in place rather than counting twice.
    rounds = list(state.get("repair_rounds", []))

    def _authored_between(base: str | None, tip: str) -> bool:
        """Did anything a REVIEWER would read land between these two commits? `sync-artifacts` commits
        machine output the engine generated itself, and no reviewer would read it. An unmeasurable range
        answers yes: never a free pass (StarshipSuperjam/engine-template#1065)."""
        try:
            return bool(ranges.authored_between(ROOT, base, tip))
        except ranges.RangeUnreadable:
            return True

    # A round that was FANNED OUT AND NEVER COMPLETED is paid for and can never be absorbed into a later
    # assess. Keying that on commit identity was the defect: an abandoned fan-out counted only while the
    # tip had not moved, so a single `sync-artifacts` commit landing before the next assessment ERASED an
    # already-paid round and dropped the count. What actually distinguishes the two cases is not where
    # HEAD is, it is whether the lenses came back — so that is what gets recorded.
    if prior and prior.get("packet_digest") and not _repair_round_complete(prior):
        rounds = [{**r, "spent": True}
                  if (r["reviewed_commit"] == prior["reviewed_commit"]
                      and r["final_commit"] == prior["final_commit"]) else r
                  for r in rounds]

    def _same_episode(entry: dict) -> bool:
        """Whether this assess is the round `entry` already recorded, re-pointed rather than repeated.

        Two things have to hold. The head must have moved onto nothing authored — otherwise real work
        landed that the round's lenses have not read, and judging it is a genuinely new round. And the
        divergence now being judged must start where that round already stood: at its reviewed commit
        (nothing completed yet) or at its final commit (the round completed and the anchor advanced with
        it). That second case is the observed one — a completed round, then a `sync-artifacts` commit,
        which put the repair's final commit behind HEAD and forced a re-bind that the counter charged as
        a third round in a build that had run two (StarshipSuperjam/engine-template#1063)."""
        if entry.get("spent"):
            return False                    # its lenses were dispatched and never returned; it counts
        if _authored_between(entry["final_commit"], head):
            return False
        return reviewed in (entry["reviewed_commit"], entry["final_commit"])

    same = [r for r in rounds if _same_episode(r)]
    prior_rounds = len(rounds) - len(same)
    guidance = getattr(args, "guidance", None)
    if not same and prior_rounds >= _REPAIR_ROUND_ESCALATION and not guidance:
        raise CoordinatorError(
            f"{prior_rounds} repair rounds have already run on this deliverable. A third is the point to stop "
            "and bring the operator in: summarise plainly what keeps failing and what you propose (narrow the "
            "re-review, accept-track the residual findings, or keep going), then record their answer with "
            "--guidance. That text is published in the PR body, so the operator sees at merge whether they "
            "were actually consulted. This is a discipline prompt backed by their merge, not a wall.")
    # CARRY-FORWARD. A receipt is a fact about what a lens read, and that fact does not stop being true
    # because the binding moved. Every prior repair receipt whose recorded range already covers the new
    # divergence survives, byte-identical; the rest are named, with the delta they still owe, so the
    # session is told precisely which lenses owe a read of which commits instead of facing the
    # all-or-nothing wall that cost two true receipts in StarshipSuperjam/engine-template#1063.
    carried, dropped = [], []
    for receipt in (prior or {}).get("receipts", []):
        (carried if ranges.receipt_covers(ROOT, receipt, reviewed, head) else dropped).append(receipt)
    # A dropped receipt is always NAMED. It is only REFUSED on a `none` judgment, and the difference is
    # what each path costs. A scoped or full round drops a receipt and then asks that lens to read the new
    # range, so the evidence is replaced rather than lost — naming it is enough, and walling every ordinary
    # second round behind a flag would turn a safety valve into a rubber stamp. `none` is the destructive
    # one StarshipSuperjam/engine-template#1012 named: it discards the receipt AND ends the repair loop
    # with no re-review, mid-stream, prompted by a status line that used to read like a step to take.
    if dropped:
        detail = "; ".join(ranges.coverage_report(ROOT, r, reviewed, head) for r in dropped)
        also = f" {len(carried)} receipt(s) DO still cover it and are kept." if carried else ""
        if args.judgment == "none" and not getattr(args, "accept_receipt_loss", False):
            raise CoordinatorError(
                f"a `none` judgment here would discard {len(dropped)} recorded repair receipt(s) that do "
                f"not cover this new divergence, AND end the repair loop without re-reviewing it: {detail}."
                + also + " That is real cold-review evidence and a terminal judgment in one step, so this "
                "stops rather than doing it quietly. If the divergence genuinely carries nothing a lens "
                "would find, pass --accept-receipt-loss and it proceeds; if it does carry something, judge "
                "it `scoped` and name the lenses that must re-read it.")
        print(f"warning: {len(dropped)} repair receipt(s) do not cover this new divergence and are being "
              f"dropped: {detail}." + also + " Those lenses owe a read of the new range.", file=sys.stderr)
    entry = {"reviewed_commit": reviewed, "final_commit": head, "judgment": args.judgment,
             "lenses": lenses, "guidance": guidance}
    rounds = [r for r in rounds if r not in same] + [entry]
    repair = {"reviewed_commit": reviewed, "final_commit": head, "summary": summary, "judgment": args.judgment,
              "rationale": args.rationale, "lenses": lenses, "packet_digest": None,
              "referent_digest": None, "reviewer_contracts": [], "receipts": carried,
              "session_effort": (prior or {}).get("session_effort"),
              "effort_shortfall_accepted": bool((prior or {}).get("effort_shortfall_accepted"))}
    store.mutate(lambda s: s.update({"repair": repair, "repair_rounds": rounds}), from_revision=revision)
    print(json.dumps(repair, indent=2, sort_keys=True))
    if carried:
        print(f"carried {len(carried)} repair receipt(s) forward — "
              + "; ".join(ranges.coverage_report(ROOT, r, reviewed, head) for r in carried), file=sys.stderr)
    if same:
        print("this re-points the repair round already recorded at "
              f"{reviewed[:12]} rather than opening a new one against the escalation gate", file=sys.stderr)


def _pr_contract(body: str) -> tuple[bool, str]:
    sys.path.insert(0, str(ROOT / ".engine" / "tools"))
    import validate
    rule = _json(ROOT / ".engine" / "check" / "pr-body-completeness.json")
    verdict, findings = validate.kind_presence(rule, {"pr_body": body})
    return verdict, "; ".join(f["message"] for f in findings) or "all required sections and consent anchors are filled"


def _compute_preflight_legs(state: dict, head: str, pr_data: dict, body: str) -> dict:
    """The six submission-preflight legs over a candidate body, as pure computation that never raises.

    Single-homed so the two callers cannot drift: `cmd_preflight` records the results and raises on a
    failed required leg (`pr-contract`, `checkout-integrity`); `contract apply` loops on the results while
    it drives the body to a fixed point (where an intermediate pass may legitimately fail `pr-contract`
    before the folded advisory lines and defang settle). Returns the results list, the required-leg
    verdicts and their summaries, and the applicable hard-check declarations."""
    repo, pr = state["build"]["repository"], state["build"]["pr"]
    base = pr_data.get("baseRefOid") or state["build"]["base_at_bind"]
    close = _run([sys.executable, str(ROOT / ".engine" / "tools" / "close_linkage_preflight.py"), "check", "--pr", str(pr), "--base", base, "--head", head])
    close_passed = close.returncode == 0
    close_summary = (close.stdout or close.stderr or "no close-linkage output").strip()
    contract_passed, contract_summary = _pr_contract(body)
    missing_disagreements = [line for line in review.required_disagreement_lines(state) if line not in body]
    if missing_disagreements:
        contract_passed = False
        ids = [re.search(r"`([^`]+)`", line).group(1) for line in missing_disagreements]
        contract_summary += "; missing reviewer disagreement disclosure: " + ", ".join(ids)
    profile = _run([sys.executable, str(ROOT / ".engine" / "tools" / "scope_profile.py"), base])
    profile_summary = (profile.stdout or profile.stderr or "no scope-profile output").strip()
    declarations = _hard_check_declarations()
    declaration_path, declaration_digest = _write_json_artifact("build-hard-check-declarations", declarations)
    # Checkout-integrity (StarshipSuperjam/engine-template#947): a required preflight that verifies the review fan-out did
    # not mutate the build checkout's git state, comparing against the snapshot captured when the
    # deliverable/repair review packet was created. `head` and `worktrees` are ignored — repair commits
    # legitimately advance HEAD and a concurrent peer may add a worktree — leaving origin, branch, and
    # stash, none of which a review ever legitimately changes. Inert (passes) until a baseline exists.
    ci_snap = state.get("checkout_snapshot")
    if ci_snap:
        ci = review_integrity.verify(str(ROOT), ci_snap, ignore={"head", "worktrees"})
        ci_passed = not ci["mutated"]
        ci_summary = ("checkout origin, branch, and stash unchanged since the review packet"
                      if ci_passed else "; ".join(ci["changes"]))
        # Advisory (non-blocking): worktree-registry drift is the other half of incident 2, but a
        # concurrent peer session may legitimately add a worktree to the shared checkout, so it is
        # SURFACED here, never used to block — the required leg above stays free of that false positive.
        wt_changes = review_integrity.compare(ci_snap, ci["after"],
                                              ignore={"origin", "branch", "head", "stash"})
        wt_passed = not wt_changes
        wt_summary = ("worktree registry unchanged since the review packet"
                      if wt_passed else "; ".join(wt_changes))
    else:
        ci_passed = wt_passed = True
        ci_summary = wt_summary = "no review-packet checkout snapshot captured (nothing to verify)"
    results = [
        {"id": "close-linkage", "commit": head, "passed": close_passed, "summary": close_summary},
        {"id": "pr-contract", "commit": head, "passed": contract_passed, "summary": contract_summary},
        {"id": "scope-profile", "commit": head,
         "passed": profile.returncode == 0 and "could not read the diff" not in profile_summary.lower(),
         "summary": profile_summary},
        {"id": "hard-check-declarations", "commit": head, "passed": True,
         "summary": (f"{len(declarations)} applicable declaration(s) at {declaration_path} "
                     f"({declaration_digest})" if declarations else "no hard-check declarations apply")},
        {"id": "checkout-integrity", "commit": head, "passed": ci_passed, "summary": ci_summary},
        {"id": "checkout-worktrees", "commit": head, "passed": wt_passed, "summary": wt_summary},
    ]
    return {"results": results, "contract_passed": contract_passed, "contract_summary": contract_summary,
            "ci_passed": ci_passed, "ci_summary": ci_summary, "declarations": declarations}


def cmd_preflight(args, store: Snapshot) -> None:
    state = store.read()
    revision = state["revision"]
    repo, pr = state["build"]["repository"], state["build"]["pr"]
    with core.StableCommit(ROOT, "submission preflight") as head:
        pr_data = _verify_draft(repo, pr)
        body = pr_data.get("body") or ""
        if args.pr_body and _input(args.pr_body) != body:
            raise CoordinatorError("the supplied PR body is not the body currently on GitHub")
        legs = _compute_preflight_legs(state, head, pr_data, body)
        results = legs["results"]
        contract_passed, ci_passed, ci_summary = legs["contract_passed"], legs["ci_passed"], legs["ci_summary"]
        declarations = legs["declarations"]

        def change(s):
            s["preflights"] = results
            s["pr_contract"] = {"commit": head, "body_digest": _digest(body.encode()), "complete": contract_passed}
    store.mutate(change, from_revision=revision)
    if getattr(args, "json", False):
        print(json.dumps(results, indent=2, sort_keys=True))
    else:
        required_ids = {"pr-contract", "checkout-integrity"}
        required_failures = [item["id"] for item in results if item["id"] in required_ids and not item["passed"]]
        advisory = [item["id"] for item in results if item["id"] not in required_ids and not item["passed"]]
        print(f"preflight recorded for {head[:12]}: required failures {len(required_failures)}, "
              f"advisory findings {len(advisory)}, {len(declarations)} applicable hard-check declaration(s)")
    if not contract_passed:
        raise CoordinatorError("the required PR-contract preflight needs attention")
    if not ci_passed:
        raise CoordinatorError("the checkout-integrity preflight failed — a review pass appears to have "
                               "mutated the build checkout: " + ci_summary)


def _bounded_work(work_map: dict) -> dict:
    """The BOUNDED work projection for a durable handoff, published into the PR body.

    Identifiers, digests, outcomes, and repo-relative changed paths travel — they are what a cold
    resume needs to re-derive every node's state. Local filesystem paths and unreviewed worker
    free-text (verification output, assumptions, concerns, failure reasons) are redacted with the
    same discipline the repair rationale and finding summaries already get: the PR body is a public
    surface, and evidence prose reaches it only through an explicitly reviewed summary.
    """
    redacted = "redacted from durable handoff"
    bounded = {}
    for node_id, nw in (work_map or {}).items():
        nw = json.loads(json.dumps(nw))
        claim = nw.get("claim")
        if claim:
            claim["worktree"] = redacted
        result = nw.get("latest_result")
        if result:
            if result.get("artifact_ref"):
                result["artifact_ref"] = redacted
            evidence = result.get("evidence") or {}
            for key in ("verification_results", "assumptions", "unresolved_concerns"):
                if evidence.get(key):
                    evidence[key] = [redacted]
        failure = nw.get("latest_failure")
        if failure and failure.get("reason"):
            failure["reason"] = redacted
        integration = nw.get("integration")
        if integration and integration.get("focused_verification"):
            # Also free text (typed at `work integrate`); like the repair rationale, it reaches the
            # PR body only through an explicitly authored summary, never verbatim.
            integration["focused_verification"] = redacted
        bounded[node_id] = nw
    return bounded


def _handoff(state: dict) -> dict:
    # No precondition about promotion any more. The plan a cold session must recover is durable by
    # construction — it is sealed in the local plan library — so the handoff's job narrows to carrying
    # the Build's own evidence, and its plan reference is a name plus the digest the seal minted.
    summaries = []
    for finding in state["findings"]:
        if not finding["handoff_summary"]:
            raise CoordinatorError(f"finding {finding['id']} needs a non-sensitive --handoff-summary")
        # `private_reference` is deliberately NOT carried here: it is reviewer-internal detail
        # (StarshipSuperjam/engine-template#981) and the handoff schema now forbids it. It stays local
        # in build-state; this handoff is a redacted, publishable projection — like the stripped repair
        # rationale and worker free-text — so only the operator-safe summaries reach the PR body.
        summaries.append({"id": finding["id"], "stage": finding["stage"], "lens": finding["lens"],
                          "packet_digest": finding["packet_digest"],
                          "lens_packet_digest": finding.get("lens_packet_digest", finding["packet_digest"]), "commit": finding["commit"],
                          "severity": finding["severity"], "disposition": finding["disposition"], "escalation_kind": finding["escalation_kind"],
                          "blocks_this_pr": finding["blocks_this_pr"], "summary": finding["handoff_summary"],
                          "operator_summary": finding.get("operator_summary")})
    validation = None if not state["validation"] else {
        "commit": state["validation"]["commit"],
        "results": [{"id": x["id"], "commit": x["commit"], "passed": x["passed"],
                     **({"log_digest": x["log_digest"]} if x.get("log_digest") else {})}
                    for x in state["validation"]["results"]],
    }
    repair = None if not state["repair"] else {k: v for k, v in state["repair"].items() if k != "rationale"}
    preflights = [{"id": x["id"], "commit": x["commit"], "passed": x["passed"]} for x in state["preflights"]]
    value = {"schema_version": "build-handoff.v2",
             "build": state["build"], "plan": state["plan"],
             "approval": state["approval"], "reviews": state["reviews"], "finding_summaries": summaries,
             "progress": state["progress"], "validation": validation, "repair": repair, "preflights": preflights,
             "pr_contract": state["pr_contract"],
             # Cadence ledgers cross the handoff boundary: a cold resume that zeroed them would hand the
             # continuing session a fresh free panel and fresh repair rounds. They carry digests, lens
             # names, finding ids and counts only -- nothing the redaction discipline above applies to.
             "repair_rounds": state.get("repair_rounds", []),
             "plan_change_escalations": state.get("plan_change_escalations", []),
             "reconciles": state.get("reconciles", [])}
    value["work"] = _bounded_work(state.get("work", {}))
    _validate(value, HANDOFF_SCHEMA_V2)
    return value


def cmd_handoff_export(args, store: Snapshot) -> None:
    """Write the Build's own evidence out for a cold resume. Nothing is published to GitHub.

    The plan half of a handoff is gone: the plan is not re-derived from an Issue body, because it is
    not IN an Issue body — it is sealed in the local library, and the handoff names it. What travels is
    the Build's evidence, which is why this writes a file (or stdout) and never a PR contract.
    """
    state = store.read()
    if state["plan"].get("diverged_from_seal"):
        # Fail closed, and say why. A cold session recovers the plan from the library, where the SEALED
        # payload lives; a Build whose executed payload no longer equals it would resume against a plan
        # nobody is building. Better to refuse than to hand a continuation the wrong authority.
        raise CoordinatorError(
            f"the executed plan diverged from sealed plan {state['plan']['plan_id']} on recorded "
            "operator authority, and a cold resume would recover the SEALED payload rather than the "
            "one being built. Finish this Build in the session that holds the revised plan, or re-plan "
            "from intent into a new plan and start a fresh Build.")
    _, _, sealed = _sealed_plan(state["plan"]["plan_id"])
    _assert_plan(state, sealed)
    _assert_spec_boundary(state, sealed)
    value = _handoff(state)
    rendered = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if args.output == "-":
        print(rendered, end="")
    else:
        core.write_private_path(Path(args.output), rendered)
        print(f"wrote bounded handoff snapshot to {args.output}")


def _restore_results(results: list[dict]) -> list[dict]:
    return [{**item, "summary": "details redacted from durable handoff"} for item in results]


def _restore_result_set(result_set: dict | None) -> dict | None:
    if not result_set:
        return None
    return {"commit": result_set["commit"], "results": _restore_results(result_set["results"])}


def _restore_repair(repair: dict | None) -> dict | None:
    if not repair:
        return None
    return {**repair, "rationale": "private rationale redacted from durable handoff"}


def _restore_base_state(value: dict, schema_version: str) -> dict:
    return {"schema_version": schema_version, "revision": 1, "build": value["build"], "plan": value["plan"],
            "approval": value["approval"], "reviews": value["reviews"],
            "findings": [{"id": f["id"], "stage": f["stage"], "lens": f["lens"], "packet_digest": f["packet_digest"],
                          "lens_packet_digest": f["lens_packet_digest"],
                          "commit": f["commit"], "severity": f["severity"], "summary": f["summary"], "disposition": f["disposition"],
                          "rationale": f["summary"], "escalation_kind": f["escalation_kind"],
                          "blocks_this_pr": f["blocks_this_pr"], "handoff_summary": f["summary"],
                          "operator_summary": f["operator_summary"], "private_reference": f.get("private_reference")}
                         for f in value["finding_summaries"]],
            "checkpoint": None, "progress": value["progress"], "validation": _restore_result_set(value["validation"]),
            "repair": _restore_repair(value["repair"]), "preflights": _restore_results(value["preflights"]),
            "pr_contract": value["pr_contract"], "submission": "draft", "checkout_snapshot": None,
            "repair_rounds": value.get("repair_rounds", []),
            "plan_change_escalations": value.get("plan_change_escalations", []),
            "reconciles": value.get("reconciles", [])}


def _restore_work(work_map: dict) -> dict:
    """Reconstruct the per-node work map, marking any genuinely unfinished claim as restored.

    A claim present without an integration is uncertain after a cold resume, so it derives
    recovery_required and is never treated as still-running or auto-expired. A claim whose attempt
    already has a bound RETURNED result is not uncertain — the worker finished and the node awaits
    integrator inspection exactly as before the handoff, so it keeps deriving returned rather than
    masking complete evidence behind a recovery flag.
    """
    restored = {}
    for node_id, nw in (work_map or {}).items():
        nw = dict(nw)
        claim = nw.get("claim")
        if claim and not nw.get("integration"):
            result = nw.get("latest_result")
            returned = bool(result and result.get("outcome") == "returned"
                            and result.get("attempt_id") == claim.get("attempt_id"))
            if not returned:
                claim = dict(claim)
                claim["restored"] = True
                nw["claim"] = claim
        restored[node_id] = nw
    return restored


def cmd_handoff_restore(args, store: Snapshot) -> None:
    if not args.input:
        raise CoordinatorError(
            "restore reads an exported handoff file: pass --input. Handoffs are no longer published "
            "into the PR contract, because the plan they continue is sealed in the local plan library "
            "rather than in a GitHub body.")
    rendered = _input(args.input)
    value = json.loads(rendered)
    version = value.get("schema_version")
    if version != "build-handoff.v2":
        raise CoordinatorError(
            "this handoff predates the sealed-plan cutover, so its plan authority is an Issue body that "
            "is no longer read. Finish that Build on the engine it started on; a new Build starts from "
            "a sealed plan with 'plan bind'.")
    # A handoff exported by an older engine carried `private_reference` in its finding summaries; the
    # field is no longer published and the schema now forbids it (StarshipSuperjam/engine-template#981),
    # so drop any stray copy before validation — an old block still restores cleanly instead of failing
    # closed on the forbidden key. The value is reviewer-internal and never survives the round-trip
    # (restore already yields None for it), so dropping it here loses nothing an up-to-date export keeps.
    stripped_private = False
    for _summary in value.get("finding_summaries") or []:
        # Guard the shape: a malformed block (a non-dict summary) must still reach _validate and fail
        # with the tool's clean CoordinatorError, not an AttributeError from an unconditional .pop().
        if isinstance(_summary, dict) and _summary.pop("private_reference", None) is not None:
            stripped_private = True
    _validate(value, HANDOFF_SCHEMA_V2)
    if stripped_private:
        # Match the codebase's visible-redaction convention (repair rationale, bounded work): say
        # plainly that a legacy private note was dropped rather than stripping it silently.
        print("dropped a legacy private_reference from the restored handoff (no longer published)")
    repo = value["build"]["repository"]
    if getattr(args, "repository", None) and not repo_identity.slug_eq(args.repository, repo):
        raise CoordinatorError("handoff repository does not match the selected repository")
    if getattr(args, "pr", None) and args.pr != value["build"]["pr"]:
        raise CoordinatorError("handoff PR does not match the selected pull request")
    if not repo_identity.slug_eq(repo_identity.origin_slug(str(ROOT)), repo):
        raise CoordinatorError("handoff repository does not match this worktree's origin")
    pr = github.pr_state(ROOT, repo, value["build"]["pr"])
    if pr.get("number") != value["build"]["pr"] or pr.get("state") != "OPEN" or pr.get("headRefOid") != _head():
        raise CoordinatorError("handoff PR is not the open claim at this worktree's current HEAD")
    for completed in value["progress"].get("completed", []):
        commit = completed.get("commit")
        if (not isinstance(commit, str)
                or _run(["git", "cat-file", "-e", f"{commit}^{{commit}}"]).returncode
                or _run(["git", "merge-base", "--is-ancestor", commit, pr["headRefOid"]]).returncode):
            raise CoordinatorError(f"handoff progress commit for {completed.get('id', 'unknown item')} is not contained by the live PR head")
    # The replacement anchor for cold continuation: the sealed plan RECORD, not an Issue body. The plan
    # must still be in the library, still sealed, still sealed to the same digest, and still carrying
    # the payload this Build was bound to. Any of those missing or changed and continuation is blocked —
    # this is BC-19's amended form, and it fails closed in every direction.
    plan_id = value["plan"]["plan_id"]
    try:
        recorded_id, sealed_digest, plan = _sealed_plan(plan_id)
    except CoordinatorError as exc:
        raise CoordinatorError(f"the sealed plan this Build was bound to is unusable, so cold "
                               f"continuation is blocked: {exc}") from exc
    if recorded_id != plan_id or sealed_digest != value["plan"]["sealed_digest"]:
        raise CoordinatorError(
            "the sealed plan changed since this Build was bound, so cold continuation is blocked: this "
            "snapshot names a seal the library no longer holds. A seal is terminal, so nothing re-seals "
            "it — bind a fresh Build to the plan that is sealed now, or restore the plan revision this "
            "Build was bound to and continue against that.")
    if _digest(plan) != value["plan"]["digest"]:
        raise CoordinatorError(
            "the sealed plan's build payload no longer matches this Build, so cold continuation is "
            "blocked: the seal is the one this snapshot names, but the work it describes is not. Export "
            "the handoff again from the session that owns this Build, or bind a fresh Build to the "
            "sealed plan rather than continuing against a payload this snapshot was never built from.")
    state = _restore_base_state(value, "build-state.v2")
    state["work"] = _restore_work(value.get("work", {}))
    store.create(state)
    print(f"restored Build snapshot against sealed plan {plan_id}")


def _submit_preview(store: Snapshot, plan_path: str) -> dict:
    state = store.read()
    revision = state["revision"]
    plan = _plan(plan_path)
    _assert_plan(state, plan)
    _assert_spec_current(state, plan, check_issue=True)
    repo, pr_number = state["build"]["repository"], state["build"]["pr"]
    if state.get("submission") == "unknown":
        observed = github.pr_state(ROOT, repo, pr_number)
        if observed.get("isDraft") is False:
            github.set_draft(ROOT, repo, pr_number)
        confirmed = github.pr_state(ROOT, repo, pr_number)
        if confirmed.get("isDraft") is not True:
            raise CoordinatorError("ready-state recovery remains unknown; GitHub did not confirm the PR returned to draft")
        store.mutate(lambda s: s.update({"submission": "draft"}), from_revision=revision)
        raise CoordinatorError("recovered an uncertain prior ready transition by returning the PR to draft; rerun preview")
    with core.StableCommit(ROOT, "submission") as stable_head:
        status = _status(state, plan)
        pr = github.pr_state(ROOT, repo, pr_number)
    if pr.get("number") != state["build"]["pr"] or pr.get("state") != "OPEN":
        raise CoordinatorError("the expected pull request is not open")
    if pr.get("headRefOid") != status["head_commit"]:
        raise CoordinatorError("the PR head does not match the local final commit")
    contract = state.get("pr_contract")
    if not contract or contract.get("body_digest") != _digest((pr.get("body") or "").encode()):
        raise CoordinatorError("the PR body changed after preflight; rerun preflight against the current contract")
    if pr.get("mergeable") != "MERGEABLE":
        raise CoordinatorError("the PR is not currently confirmed mergeable; reconcile or retry the live check before submission")
    base = pr.get("baseRefOid")
    if not base or _run(["git", "merge-base", "--is-ancestor", base, status["head_commit"]]).returncode:
        raise CoordinatorError("the final commit does not contain the live target-branch base; reconcile, validate, and assess review proportionally")
    if status["phase"] != "ready":
        raise CoordinatorError("submission evidence is incomplete: " + "; ".join(status["required_evidence"] + status["engineering_judgment"]))
    action = "mark-ready" if pr.get("isDraft") else "record-ready"
    if stable_head != status["head_commit"]:
        raise CoordinatorError("submission status was not derived from the stable final commit")
    return {"repository": repo, "pr": pr_number, "commit": status["head_commit"],
            "base": pr.get("baseRefOid"), "body_digest": _digest((pr.get("body") or "").encode()),
            "snapshot_revision": revision, "action": action, "merge": False}


def cmd_submit_preview(args, store: Snapshot) -> None:
    with core.StableCommit(ROOT, "submission preview"):
        preview = _submit_preview(store, args.plan)
    print(json.dumps(preview, indent=2, sort_keys=True))


def cmd_submit_apply(args, store: Snapshot) -> None:
    preview = None
    try:
        with core.StableCommit(ROOT, "ready transition"):
            preview = _submit_preview(store, args.plan)
            if preview["action"] == "mark-ready":
                github.set_ready(ROOT, preview["repository"], preview["pr"])
            try:
                after = github.pr_state(ROOT, preview["repository"], preview["pr"])
            except Exception as exc:
                try:
                    store.mutate(lambda s: s.update({"submission": "unknown"}),
                                 from_revision=preview["snapshot_revision"])
                except CoordinatorError:
                    pass
                raise CoordinatorError(f"ready transition state is unknown after GitHub verification failed: {exc}") from exc
            try:
                unchanged = (after.get("state") == "OPEN" and after.get("isDraft") is False
                             and after.get("headRefOid") == preview["commit"]
                             and after.get("baseRefOid") == preview["base"]
                             and _digest((after.get("body") or "").encode()) == preview["body_digest"])
                if not unchanged:
                    raise CoordinatorError("pull-request evidence changed during the ready transition")
                store.mutate(lambda s: s.update({"submission": "ready"}),
                             from_revision=preview["snapshot_revision"])
            except CoordinatorError as exc:
                try:
                    github.set_draft(ROOT, preview["repository"], preview["pr"])
                    confirmed = github.pr_state(ROOT, preview["repository"], preview["pr"])
                    if confirmed.get("isDraft") is not True:
                        raise RuntimeError("GitHub did not confirm draft recovery")
                    store.mutate(lambda s: s.update({"submission": "draft"}),
                                 from_revision=preview["snapshot_revision"])
                except Exception as recovery_exc:
                    try:
                        store.mutate(lambda s: s.update({"submission": "unknown"}),
                                     from_revision=preview["snapshot_revision"])
                    except CoordinatorError:
                        pass
                    raise CoordinatorError(f"ready transition could not be reversed: {exc}; recovery: {recovery_exc}") from exc
                raise CoordinatorError(f"ready transition was reversed: {exc}") from exc
    except CoordinatorError:
        if preview and store.read().get("submission") != "draft":
            try:
                github.set_draft(ROOT, preview["repository"], preview["pr"])
                confirmed = github.pr_state(ROOT, preview["repository"], preview["pr"])
                recovered = confirmed.get("isDraft") is True
                current_revision = store.read()["revision"]
                store.mutate(lambda s: s.update({"submission": "draft" if recovered else "unknown"}),
                             from_revision=current_revision)
            except Exception:
                try:
                    current_revision = store.read()["revision"]
                    store.mutate(lambda s: s.update({"submission": "unknown"}), from_revision=current_revision)
                except CoordinatorError:
                    pass
        raise
    print(f"marked {preview['repository']}#{preview['pr']} ready for the operator; no merge was attempted")


def _bindings() -> dict:
    return _json(BINDINGS_PATH)


def _work_mutate(store: Snapshot, change) -> Any:
    """Every work verb reads the current revision and mutates under an explicit compare-and-swap.

    A shared helper so no work verb repeats the legacy pattern of mutating without a from_revision;
    a concurrent snapshot advance rejects the write rather than silently overwriting a sibling claim.
    """
    state = store.read()
    return store.mutate(change, from_revision=state["revision"])


def _require_dag_plan(plan: dict) -> None:
    if _plan_version(plan) != "build-plan.v2":
        raise CoordinatorError("work verbs require a build-plan.v2 Build")


def _node_work(state: dict, node_id: str) -> dict:
    # A v1 snapshot has no work map at all: name the actual cause (wrong plan generation), matching
    # the refusal the packet/claim/result verbs give, instead of a misleading "no recorded work".
    if "work" not in state:
        raise CoordinatorError("work verbs require a build-plan.v2 Build")
    nw = (state.get("work") or {}).get(node_id)
    if not nw:
        raise CoordinatorError(f"work item {node_id} has no recorded work")
    return nw


def _claim_refusal_reason(plan: dict, state: dict, node_id: str, node: dict) -> str:
    """The specific reason a node is not claimable, so the refusal is actionable.

    Read out of the SAME admission derivation `status` and `work frontier` render, so a refusal can
    never disagree with the deferral reason a session was just shown. Only a node that is neither a
    candidate nor deferred falls back to its derived state.
    """
    deferral = next((entry for entry in dag.admission_plan(plan, state)["deferred"]
                     if entry["id"] == node_id), None)
    if deferral:
        detail = f"{deferral['kind']} — {deferral['reason']}"
        if deferral["kind"] == dag.DEFER_CAPACITY:
            detail += "; free one by integrating, rejecting, or abandoning a claim"
        return detail + " (see `work frontier --plan <plan.json>` for the whole admission picture)"
    st = node.get("state")
    reasons = "; ".join(node.get("reasons") or [])
    return f"it is {st}" + (f" ({reasons})" if reasons else "")


def cmd_work_packet(args, store: Snapshot) -> None:
    plan = _plan(args.plan)
    _require_dag_plan(plan)
    state = store.read()
    # The preview enforces the same plan-digest bar the claim does, so a stale --plan file fails
    # HERE with the digest-mismatch message rather than previewing clean and surprising the claim.
    _assert_plan(state, plan)
    item = work.node_item(plan, args.item)
    route = work.resolve_route(_bindings(), item["executor_class"], args.provider)
    packet = work.build_packet(plan, state, args.item, route, _head(), "preview", args.worktree or "<worktree>")
    node = dag.derive_lifecycle(plan, state).get(args.item, {})
    # A preview says nothing about the digest; it reports whether a claim would actually succeed now
    # — INCLUDING the approval gate a claim checks first — so a clean preview is never followed by a
    # surprise refusal.
    approved = bool(state["approval"])
    claimable = approved and args.item in dag.claimable_set(plan, state)
    if claimable:
        refusal = None
    elif not approved:
        refusal = "the Build gate is not approved"
    else:
        refusal = _claim_refusal_reason(plan, state, args.item, node)
    packet["preview"] = {"state": node.get("state"), "reasons": node.get("reasons", []),
                         "claimable_now": claimable, "refusal_reason": refusal}
    print(json.dumps(packet))


def cmd_work_frontier(args, store: Snapshot) -> None:
    """Read-only projection of the admission decision: what is admitted, what waits, and why.

    Writes NOTHING — no mutate, no store write, no GitHub call. It exists so a session can ask what
    the scheduler would do, and why it passed a node over, without spending a claim to find out.
    """
    plan = _plan(args.plan)
    _require_dag_plan(plan)
    state = store.read()
    _assert_plan(state, plan)
    lengths = dag.critical_path_lengths(plan)
    rank = dag.admission_rank(plan, lengths)
    admission = dag.admission_plan(plan, state, rank)
    parallelism = plan.get("parallelism", {"mode": "serial", "max_concurrency": 1})
    projection = {
        "admitted": admission["admitted"],
        "claimable": dag.claimable_set(plan, state, rank),
        "deferred": admission["deferred"],
        "admission_rank": rank,
        "critical_path": lengths,
        "ready": dag.ready_set(plan, state),
        "next_ready": dag.next_ready(plan, state, rank),
        "slots_in_use": dag.slots_in_use(plan, state),
        "max_concurrency": parallelism.get("max_concurrency", 1),
        "resource_holders": dag.resource_holders(plan, state),
    }
    if getattr(args, "json", False):
        print(json.dumps(projection, indent=2, sort_keys=True))
        return
    print(f"Frontier: {projection['slots_in_use']} of {projection['max_concurrency']} worker slot(s) in use")
    print("  admitted (admission order): " + (", ".join(projection["admitted"]) or "none"))
    print("  claimable (a direct claim is permitted): " + (", ".join(projection["claimable"]) or "none"))
    for entry in projection["deferred"]:
        # Same reconciliation the status render makes, and it matters more here: this verb is where a
        # session is sent to understand the admission decision, so a node named on both the claimable
        # line and a deferral line must say on the spot that the two do not disagree.
        note = " (still claimable directly)" if entry["id"] in projection["claimable"] else ""
        print(f"  deferred {entry['id']}: {entry['kind']} — {entry['reason']}{note}")
    print("  rank (critical path desc, then id): "
          + ", ".join(f"{node_id}[{projection['critical_path'][node_id]}]"
                      for node_id in projection["admission_rank"]))


def cmd_work_claim(args, store: Snapshot) -> None:
    plan = _plan(args.plan)
    _require_dag_plan(plan)
    item = work.node_item(plan, args.item)
    route = work.resolve_route(_bindings(), item["executor_class"], args.provider)
    base_sha = _head()
    attempt_id = work.new_attempt_id()
    emitted: dict = {}

    def change(state):
        _assert_plan(state, plan)
        if not state["approval"]:
            raise CoordinatorError("the Build gate is not approved")
        if args.item not in dag.claimable_set(plan, state):
            node = dag.derive_lifecycle(plan, state).get(args.item, {})
            raise CoordinatorError(
                f"work item {args.item} is not claimable now: {_claim_refusal_reason(plan, state, args.item, node)}")
        nw = state["work"].get(args.item) or work.empty_node()
        # An integrator-inline retry disposition is honored HERE: the next attempt runs inline in the
        # current senior session and is never re-dispatched, whatever the node's executor class.
        effective_route = route
        if (nw.get("latest_failure") or {}).get("disposition") == dag.DISP_INLINE:
            effective_route = {**route, "model": "inherit", "effort": "inherit", "inline": True}
        nw["attempt_count"] = nw.get("attempt_count", 0) + 1
        nw["claim"] = work.new_claim(attempt_id, base_sha, args.worktree,
                                     item.get("exclusive_resources", []), effective_route)
        nw["latest_result"] = None
        nw["latest_failure"] = None
        state["work"][args.item] = nw
        emitted["packet"] = work.build_packet(plan, state, args.item, effective_route, base_sha, attempt_id, args.worktree)

    _work_mutate(store, change)
    print(json.dumps(emitted["packet"]))


def cmd_work_attach(args, store: Snapshot) -> None:
    def change(state):
        nw = _node_work(state, args.item)
        claim = nw.get("claim")
        if not claim:
            raise CoordinatorError(f"work item {args.item} has no active claim")
        if claim["attempt_id"] != args.attempt:
            raise CoordinatorError(f"attempt {args.attempt} does not match the active claim {claim['attempt_id']}")
        claim["worker_ref"] = args.worker_ref

    _work_mutate(store, change)
    print(f"attached worker reference to {args.item} attempt {args.attempt}")


def cmd_work_result(args, store: Snapshot) -> None:
    plan = _plan(args.plan)
    _require_dag_plan(plan)
    item = work.node_item(plan, args.item)
    try:
        payload = json.loads(_input(args.input))
    except ValueError as exc:
        raise CoordinatorError(f"work result input is not JSON: {exc}") from exc
    base_sha = payload.get("base_sha")
    if not base_sha:
        raise CoordinatorError("work result must report the base_sha the worker built from")

    def change(state):
        _assert_plan(state, plan)
        nw = _node_work(state, args.item)
        result = work.bind_result(nw, item, args.attempt, base_sha, payload)
        nw["latest_result"] = result
        if result["outcome"] == "failed":
            nw["latest_failure"] = work.failure_record(
                args.attempt, payload.get("class", "worker"),
                payload.get("reason", "worker reported a failure"))
        else:
            # A returned result supersedes any open failure for this attempt, so the node never
            # derives as failed while holding a complete, contract-satisfying returned result.
            nw["latest_failure"] = None

    _work_mutate(store, change)
    print(f"recorded {args.item} result for attempt {args.attempt}")


def _commit_on_branch(commit: str) -> bool:
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        return False
    return _run(["git", "merge-base", "--is-ancestor", commit, "HEAD"]).returncode == 0


def cmd_work_reject(args, store: Snapshot) -> None:
    def change(state):
        nw = _node_work(state, args.item)
        claim = nw.get("claim")
        result = nw.get("latest_result")
        attempt = claim["attempt_id"] if claim else (result or {}).get("attempt_id")
        if attempt != args.attempt:
            raise CoordinatorError(f"attempt {args.attempt} is not the node's current attempt ({attempt})")
        nw["latest_failure"] = work.failure_record(args.attempt, args.rejection_class, args.reason, dag.DISP_OPEN)
        nw["claim"] = None  # rejection releases the reserved resources

    _work_mutate(store, change)
    print(f"rejected {args.item} attempt {args.attempt} ({args.rejection_class}); resources released")


def cmd_work_retry(args, store: Snapshot) -> None:
    def change(state):
        nw = _node_work(state, args.item)
        failure = nw.get("latest_failure")
        # An open failure awaits its first disposition; an abandoned node is reopened by exactly this
        # verb — the deliberate fresh start its blocked-state reason promises. Any other disposition
        # has no pending decision to take.
        if not failure or failure.get("disposition") not in (dag.DISP_OPEN, dag.DISP_ABANDONED):
            raise CoordinatorError(f"work item {args.item} has no failure awaiting a retry decision")
        disposition = dag.DISP_INLINE if args.strategy == "integrator-inline" else dag.DISP_RETRY
        failure["disposition"] = disposition
        nw["claim"] = None  # a fresh attempt id is minted on the next claim; attempt_count increments there

    _work_mutate(store, change)
    consequence = ("the next claim will run integrator-inline in the current session"
                   if args.strategy == "integrator-inline" else "the next claim mints a fresh attempt")
    print(f"retry recorded for {args.item} via {args.strategy} ({consequence}): {args.reason}")


def cmd_work_abandon(args, store: Snapshot) -> None:
    def change(state):
        nw = _node_work(state, args.item)
        claim = nw.get("claim")
        failure = nw.get("latest_failure")
        attempt = (claim or {}).get("attempt_id") or (failure or {}).get("attempt_id")
        if attempt != args.attempt:
            raise CoordinatorError(f"attempt {args.attempt} is not the node's current attempt ({attempt})")
        nw["latest_failure"] = work.failure_record(args.attempt, (failure or {}).get("class", "worker"),
                                                   args.reason, dag.DISP_ABANDONED)
        nw["claim"] = None  # abandonment releases the reserved resources

    _work_mutate(store, change)
    print(f"abandoned {args.item} attempt {args.attempt}; resources released")


def cmd_work_integrate(args, store: Snapshot) -> None:
    if not args.verification_input.strip():
        raise CoordinatorError("integration requires a focused-verification summary")
    if not _commit_on_branch(args.commit):
        raise CoordinatorError(f"integration commit {args.commit} is not on the PR branch")

    def change(state):
        nw = _node_work(state, args.item)
        result = nw.get("latest_result")
        if not result or result.get("outcome") != "returned" or result.get("attempt_id") != args.attempt:
            raise CoordinatorError(f"work item {args.item} has no returned result for attempt {args.attempt} to integrate")
        nw["integration"] = {"attempt_id": args.attempt, "commit": args.commit,
                             "focused_verification": args.verification_input.strip()}
        nw["claim"] = None  # integration releases the reserved resources
        # `work integrate` is the SOLE writer of a v2 completion, so it owns this entry outright: a
        # pre-existing one is corrected to the integration commit rather than skipped. Skipping was
        # what made the mid-flight refusal unrecoverable — a snapshot carrying an unearned completion
        # (the shape `checkpoint --complete-item` used to write) kept its stale commit through the
        # entire documented remedy, so the gate never cleared and the Build deadlocked.
        existing = next((entry for entry in state["progress"]["completed"]
                         if entry["id"] == args.item), None)
        if existing is None:
            state["progress"]["completed"].append({"id": args.item, "commit": args.commit})
            return None
        corrected = existing["commit"]
        existing["commit"] = args.commit
        return corrected

    corrected = _work_mutate(store, change)
    print(f"integrated {args.item} at {args.commit}; focused verification recorded")
    if corrected and corrected != args.commit:
        # An operator running this to escape the unearned-completion refusal should see that the
        # correction happened, not just that an integration did.
        print(f"corrected the recorded completion for {args.item}: was {corrected[:12]}, "
              f"now the integration commit {args.commit[:12]}")


_CLAIM_FILL_GUIDANCE = (
    "Fill each field per its `description` in .engine/schemas/pr-body-claim.v1.json — that is where the content "
    "rules live (e.g. Scope must not restate size/counts, Risk flags the single most safety-sensitive edit, "
    "Files of interest is a curated selection not the whole diff, Review's loop_narrative is one entry per "
    "round). The renderer owns all structure; every value is a single-line Markdown fragment.")


def cmd_contract_template(args, store) -> None:
    """Emit the fillable `pr-body-claim.v1` skeleton (stateless — no Build snapshot needed). Every judgment
    slot is null and every list empty, so validating the filled-in result names any slot still unfilled;
    the shape is the instruction. Written to --output, or stdout by default."""
    rendered = json.dumps(composer.fillable_template(), indent=2, sort_keys=True) + "\n"
    if getattr(args, "output", None) and args.output != "-":
        Path(args.output).write_text(rendered, encoding="utf-8")
        print(f"wrote the fillable pr-body-claim.v1 template to {args.output}")
        print(_CLAIM_FILL_GUIDANCE, file=sys.stderr)
    else:
        print(rendered, end="")
        print("\n" + _CLAIM_FILL_GUIDANCE, file=sys.stderr)


def _assert_claim_findings(state: dict, claim: dict) -> None:
    """The claim's finding summaries must match the current coordinator finding set EXACTLY — no stale,
    missing, or unknown ids — so the composed body neither drops a real finding nor invents one, and never
    publishes raw finding text implicitly (only the claim's operator-safe summary reaches the body)."""
    state_ids = {f["id"] for f in state.get("findings", [])}
    claim_ids = {fs["id"] for fs in claim["review"]["finding_summaries"]}
    if state_ids != claim_ids:
        parts = []
        missing = sorted(state_ids - claim_ids)
        unknown = sorted(claim_ids - state_ids)
        if missing:
            parts.append("missing an operator-safe summary for: " + ", ".join(missing))
        if unknown:
            parts.append("carries a summary for unknown finding(s): " + ", ".join(unknown))
        raise CoordinatorError(
            "the claim's finding summaries must match the current finding set exactly — " + "; ".join(parts)
            + " (re-run `contract template` against current state, or reconcile the finding ids)")


def _sealed_plan_record(state: dict) -> dict | None:
    """The whole record of the SEALED PLAN this Build was bound to, read from the library at compose time.

    Its own seam because more than one disclosure needs more than one field off it — the review and its
    findings, the carried obligations, and the depth the plan panel was approved at, which is the plan
    record's own and not the Build's.
    """
    plan_id = state.get("plan", {}).get("plan_id")
    if not plan_id:
        return None
    try:
        library = _library()
        return library.read_record(library.resolve(plan_id))
    except Exception:  # noqa: BLE001 — an unreadable library must not block composing the PR body
        return None


def _sealed_plan_review(state: dict) -> dict | None:
    """The plan review recorded on the SEALED PLAN, read from the library at compose time.

    This is where plan-review evidence lives now, and reading it here rather than mirroring it into
    build-state is what makes the disclosure immune to the supersession rule: a finding that no live
    Build receipt demands loses its weight in `surviving_findings`, and a plan finding has no Build
    receipt at all. Structural immunity rather than a flag to remember to set.
    """
    return (_sealed_plan_record(state) or {}).get("plan_review")


def _plan_obligation_lines(state: dict) -> list[str]:
    """What this plan owed a predecessor, and what it did about each — for the merge surface.

    A program's carry-forward guarantee is that an obligation can be satisfied, re-carried, or
    released with a reason, but never dropped silently. That guarantee is enforced where plans are
    written, which is a place the operator approving the merge never looks. So the same record is
    rendered here, at the one surface they do read: a release states its reason in the operator's
    view, and an obligation carried to a successor names where it went.

    Read from the sealed plan RECORD at compose time, like the plan review above and for the same
    reason — it cannot be edited by the Build, mirrored stale into build-state, or stripped by the
    Build's own receipt bookkeeping.
    """
    plan_id = state.get("plan", {}).get("plan_id")
    if not plan_id:
        return []
    try:
        library = _library()
        slug = library.resolve(plan_id)
        document = library.head(slug)
    except Exception:  # noqa: BLE001 — an unreadable library must not block composing the PR body
        return []
    lines = []
    for obligation in ((document.get("program") or {}).get("carried_obligations") or []):
        reason = (obligation.get("reason") or "").strip()
        tail = f" {reason}" if reason else ""
        lines.append(f"- **{obligation['id']}** — _{obligation['state']}_. "
                     f"{obligation['statement']}{tail}")
    return lines


def _added_workflow_disclosure(base: str) -> str:
    """For every GitHub workflow this change ADDS: what fires it and what it is allowed to do.

    Read out of the file rather than written by hand. An added workflow is invisible to the weakening
    guard, which inspects modifications to files that already exist — so nothing mechanical reviews the
    triggers or the token of a workflow that arrives whole. This is the disclosure that stands in for
    that missing review, and a disclosure a session has to remember to write is one that will eventually
    not be written.

    A FAILED DIFF IS NOT AN EMPTY ONE. `_run` does not raise, so an unresolvable base — a
    garbage-collected anchor, a shallow clone, a branch rewritten under us — used to produce no output,
    an empty `added`, and a body that read as "this change adds no workflows". At the one surface where
    an added workflow gets any review at all, a git failure and a genuine absence must not look alike."""
    listed = _run(["git", "diff", "--diff-filter=A", "--name-only", f"{base}...HEAD"])
    if listed.returncode != 0:
        return ("**Whether this change adds GitHub workflows could not be determined** — listing the "
                f"files added since `{base}` failed ({(listed.stderr or '').strip() or 'no detail'}). "
                "Nothing automatic reviews an added workflow's triggers or permissions, so read "
                "`.github/workflows/` yourself before merging.")
    added = [p for p in listed.stdout.splitlines() if p.startswith(".github/workflows/")]
    if not added:
        return ""
    lines = ["**This change adds GitHub workflows.** Nothing automatic reviews an added workflow's "
             "triggers or permissions, so they are stated here, read from the files themselves:"]
    for rel in sorted(added):
        try:
            import yaml
            doc = yaml.safe_load((ROOT / rel).read_text(encoding="utf-8")) or {}
        except Exception as exc:                        # noqa: BLE001
            lines.append(f"- `{rel}` — could not be read to disclose its triggers and permissions ({exc}); "
                         "read the file before merging.")
            continue
        triggers = doc.get(True, doc.get("on")) or {}
        names = ", ".join(sorted(triggers)) if isinstance(triggers, dict) else str(triggers)
        # `{}` and absent are DIFFERENT and the difference is the point: an empty mapping grants nothing
        # by default (the least-privilege posture), while an absent key inherits the repository default,
        # which may be write. Reporting both as "not declared" would flatten the safer one into the
        # riskier one at the exact surface where the operator judges it.
        top = ("grants nothing by default" if doc.get("permissions") == {}
               else ", ".join(f"{k}={v}" for k, v in sorted((doc.get("permissions") or {}).items()))
               or "NOT DECLARED — inherits the repository default, which may include write")
        per_job = {job: spec.get("permissions") for job, spec in (doc.get("jobs") or {}).items()
                   if isinstance(spec, dict) and spec.get("permissions")}
        grants = "; ".join(f"`{job}`: " + ", ".join(f"{k}={v}" for k, v in sorted(perm.items()))
                           for job, perm in sorted(per_job.items())) or "none declared per job"
        lines.append(f"- `{rel}` — fires on: {names or 'nothing declared'}. Workflow-level permissions: "
                     f"{top}. Per job: {grants}.")
    return "\n".join(lines)


def _plan_consent_lines(state: dict) -> list[str]:
    """What the operator was asked at each plan consent gate, and what they answered — verbatim.

    Read from the sealed plan RECORD at compose time, like the plan review and the obligations above
    and for the same reason: the Build cannot edit it, and nothing here can mirror it stale.

    It is published because publishing is the only thing the mechanism actually buys. The
    attestations are a session's record of what the operator said; a session that would fabricate one
    could. Putting them in front of that same operator at merge is what turns a fabrication into a
    discrete, visible lie — a decision they do not recognise, in their own supposed words — rather
    than a silence nobody can see. Un-forgeability needs an identity the session cannot mint, which
    is issue 914's residual and is not claimed here.
    """
    plan_id = state.get("plan", {}).get("plan_id")
    if not plan_id:
        return []
    try:
        import plan_lifecycle
        library = _library()
        record = library.read_record(library.resolve(plan_id))
    except Exception as exc:  # noqa: BLE001
        # SAYS SO, rather than returning empty. Returning [] made an unreadable library indistinguishable
        # from a Build that had no consent points at all — the body simply omitted the trail, and nothing
        # in the completeness check demanded it. The headline safeguard of this whole program would have
        # degraded into silence at exactly the surface it exists to reach. A body that cannot show the
        # trail must say it could not, so the operator knows to ask.
        return [f"**The operator-consent trail could not be read from the plan library, so it is NOT "
                f"published here.** That is not the same as there having been no consent gates — it means "
                f"this body cannot show you what you were recorded as deciding for plan {plan_id}, and you "
                f"should ask before merging. ({exc})"]
    trail = plan_lifecycle.consent_trail(record)
    if not trail:
        return ["**No operator-consent attestations are recorded for this plan.** Approve, seal and bind "
                "each require one, so an empty trail on a Build that reached here is itself worth asking "
                "about."]
    return trail


def _plan_review_clause(state: dict) -> str:
    """The PR body's one sentence about whether the SHIPPED plan was reviewed.

    Its own named seam because distinct situations reach it and each needs a different true sentence.
    """
    plan_review = _sealed_plan_review(state)
    diverged = state.get("plan", {}).get("diverged_from_seal")
    depth = (state.get("approval") or {}).get("depth", "the approved")
    if plan_review and diverged:
        return (f"The sealed plan was reviewed at {depth} depth by "
                + ", ".join(plan_review.get("lenses", [])) +
                ", but what was BUILT differs from it on recorded operator authority (see the "
                "escalation above), so the review does not cover the delta")
    if plan_review:
        return ("Plan review ran before any code, on the plan side: "
                + ", ".join(plan_review.get("lenses", [])) + " read the sealed plan")
    if diverged:
        return ("The shipped plan differs from the sealed plan on recorded operator authority, and no "
                "cold plan review is recorded for either")
    return ("No cold plan review is recorded — the plan was sealed at a depth that runs no plan lenses, "
            "so the operator's own read is its review")


def _plan_finding_lines(state: dict) -> list[str]:
    """The sealed plan review's findings and their dispositions, for the merge surface.

    A plan review that found blocking problems must be VISIBLE at merge, whatever was decided about
    them. Rendered from the plan record, so nothing in the Build's own receipt bookkeeping can strip
    them.
    """
    plan_review = _sealed_plan_review(state)
    lines = []
    for finding in (plan_review or {}).get("findings", []):
        disposition = finding.get("disposition") or "undispositioned"
        summary = finding.get("operator_summary") or finding["summary"]
        blocks = " — **blocks this PR**" if finding.get("blocks_this_pr") else ""
        lines.append(f"- **Plan finding `{finding['id']}`** ({finding['lens']}, {finding['severity']}, "
                     f"{disposition}){blocks}. {summary}")
    return lines


def _plan_disagreement_lines(state: dict) -> list[str]:
    """A blocking plan finding that was NOT left blocking is a disagreement the operator must meet."""
    plan_review = _sealed_plan_review(state)
    lines = []
    for finding in (plan_review or {}).get("findings", []):
        if finding["severity"] == "blocking" and not finding.get("blocks_this_pr"):
            summary = finding.get("operator_summary") or "[no operator-safe summary recorded]"
            lines.append(f"- Plan-review disagreement `{finding['id']}`: {summary}")
    return lines


def _drift_line(state: dict, head: str) -> str:
    """The PR body's "Reviewed vs submitted" disclosure, composed from recorded state.

    Pure and single-homed so it can be driven end to end by a test: the operator's consent surface is the
    one place a wrong sentence does real damage, and this line has twice been the thing that went wrong.
    Two rules it must never break. It must not claim no repair happened when one did -- clearing the repair
    record used to make it say exactly that. And the commit it names as "submitted" must be the commit
    actually in the pull request: after a history rewrite, a completed repair round's final commit is an
    orphan, and leading with it put a rewritten SHA under the bold label an operator reads first, with the
    correction two clauses later."""
    repair = state.get("repair")
    reconciles = state.get("reconciles", [])

    def repair_clause():
        if not (repair and repair.get("final_commit")):
            return None
        if repair.get("judgment") == "none":
            return (f"a post-review repair was assessed at `{repair['final_commit'][:12]}` and judged not "
                    f"to need re-review ({repair['summary']})")
        return (f"a post-review repair carried `{repair['reviewed_commit'][:12]}` to "
                f"`{repair['final_commit'][:12]}` ({repair['summary']})")

    if not reconciles:
        if repair and repair.get("final_commit"):
            tail = (f"{repair['summary']}; no re-review was judged necessary"
                    if repair.get("judgment") == "none" else repair["summary"])
            return (f"reviewed `{repair['reviewed_commit'][:12]}`, submitted "
                    f"`{repair['final_commit'][:12]}` — {tail}")
        return "no post-review repair was needed; the reviewed and submitted commits are the same."

    # Every event states its OWN commits and nothing else. Three successive attempts to lead with "the
    # reviewed commit" and to say whether a repair ran before or after a rewrite each produced a sentence
    # that was wrong on some reachable flow -- the last one contradicting itself inside a single line --
    # because neither the original review point nor the ordering is reliably recoverable once history has
    # been rewritten. So this asserts neither. The only claim about the whole is the commit actually in the
    # pull request, which is always known. This is the operator's consent surface: a line that says less
    # and is always true beats a narrative that reads well and is sometimes false.
    events = []
    for item in reconciles:
        detail = ("the branch's own contribution was verified unchanged on exact tree entries"
                  if item["contribution_identical"] else
                  (item["unmeasurable"] or "the contribution differs at: "
                   + ", ".join(item["divergent_paths"])))
        events.append(f"history was rewritten and the review bindings were re-anchored from "
                      f"`{item['from_commit'][:12]}` to `{item['anchored_to'][:12]}` "
                      f"(base `{(item['base_before'] or '?')[:12]}` → `{item['base_after'][:12]}`, {detail})")
    clause = repair_clause()
    if clause:
        events.append(clause)
    ordering = (" These are listed by kind; their order relative to one another is not recorded."
                if len(events) > 1 else "")
    return f"submitted `{head[:12]}`, after: " + "; ".join(events) + "." + ordering


def _assemble_evidence(state: dict, plan: dict, claim: dict, head: str, pr_data: dict) -> dict:
    """Compute the coordinator-owned evidence a composed body carries — everything deterministic that the
    claim deliberately does not hold. Read-only: it runs the same report-only tools the preflight uses and
    reads recorded Build state; it never writes. `contract preview` and `contract apply` share it so the
    previewed body and the applied body are assembled identically."""
    repo = state["build"]["repository"]
    base = pr_data.get("baseRefOid") or state["build"]["base_at_bind"]

    # Closing linkage: the claim's closes plus the Issue that AUTHORIZED this Build (mechanically added,
    # never inferred). Part-of comes straight from the claim inside the composer.
    closes = list(claim["linkage"]["closes"])
    authorizing = state["plan"].get("authorizing_issue")
    if authorizing and authorizing not in closes and authorizing not in claim["linkage"]["part_of"]:
        closes.append(authorizing)

    # Report-only change profile, over the live base — the same invocation the preflight records.
    profile = _run([sys.executable, str(ROOT / ".engine" / "tools" / "scope_profile.py"), base])
    change_profile = (profile.stdout or "").strip()
    # An ADDED workflow discloses itself. The weakening guard only inspects files that already existed, so
    # a change that ADDS a workflow — with whatever triggers and whatever token — passes no automatic
    # review at all; the plan named the pull-request disclosure as the compensating control precisely for
    # that reason. Leaving it to the submitting session to remember would make the control exactly as
    # reliable as memory, so it is composed from the file itself.
    added_workflows = _added_workflow_disclosure(base)
    if added_workflows:
        change_profile = (change_profile + "\n\n" + added_workflows).strip()

    # Validation receipts, stripped of machine-local log paths (mirrors the handoff strip), rendered with the
    # operator labels from the one build-protocol declaration that also drives execution.
    val = state.get("validation")
    if val and val.get("results"):
        labels = {c["id"]: c["label"] for c in _protocol().get("validation_commands", [])}
        vlines = []
        for r in val["results"]:
            status = "passed" if r["passed"] else "**FAILED**"
            tail = f" (log {r['log_digest']})" if r.get("log_digest") else ""
            vlines.append(f"- **{labels.get(r['id'], r['id'])}** — {status} at `{r['commit'][:12]}`{tail}")
        validation_results = "\n".join(vlines)
    else:
        validation_results = "- The full CI suite and self-tests are run green against the final commit and recorded before the draft is marked ready."

    # Spec-derived acceptance steps: the canonical resolution, rendered (multi-document merge) or its honest
    # no-spec disclosure — never re-authored here.
    cs = spec_service.canonical_spec(ROOT, plan, repository=repo, issue_body=_issue_body)
    if cs["posture"] == "none":
        spec_steps = cs["review_steps"]
    else:
        import spec_referent
        spec_steps = spec_referent.render_review_steps_multi(cs["review_steps"])

    depth = state["approval"]["depth"] if state.get("approval") else "unapproved"
    # Coverage must state what ACTUALLY ran, not what is installed. At quick depth (and any depth that
    # recorded no cold-review receipts) no lens ran, so naming the installed lenses as having "ran after"
    # would be a false claim in the PR body — the honesty defect this line must not commit. Key the sentence
    # on the recorded receipts, not on the installed set.
    plan_review_ran = bool(_sealed_plan_review(state))
    cold_review_ran = plan_review_ran or bool(state.get("reviews", {}).get("deliverable", {}).get("receipts", []))
    if cold_review_ran:
        lenses = ", ".join(sorted(x["lens"] for x in _installed())) or "no installed deliverable lenses"
        plan_clause = _plan_review_clause(state)
        review_coverage = f"{depth} depth. {plan_clause}; the deliverable review ({lenses}) ran after."
    else:
        review_coverage = (f"{depth} depth — no cold reviewers ran; the coverage is your own read of the change "
                           "plus the automatic checks (the full CI suite and self-tests).")

    # Code-execution disclosure (BO-41): every current review receipt must carry it. An older snapshot whose
    # receipts predate the field cannot be composed until they are re-recorded — a precise remediation, never a
    # fabricated "no code ran". The disclosure's PRESENCE is mechanical; its truth stays the reviewer's report.
    receipts = list(state.get("reviews", {}).get("deliverable", {}).get("receipts", []))
    missing = sorted({r["lens"] for r in receipts if "code_execution" not in r})
    if missing:
        raise CoordinatorError(
            "these review receipts predate the code-execution disclosure and must be re-recorded before "
            f"composing: {', '.join(missing)} — re-run `review record … --code-execution "
            "none|discarded-copy|in-place`")
    # Three behaviours, three words. Reviewers do one of three things with the change's code, and the
    # disclosure used to carry only two — so a lens that ran the suite IN THE OPERATOR'S OWN CHECKOUT
    # was recorded as though it had used a throwaway copy, which is a materially different claim about
    # what touched their project. B2's carried finding CO-1; the third value is the fix.
    code_execution_line = code_execution_disclosure({r.get("code_execution") for r in receipts})
    # The effort the panel actually delivered against the depth the operator approved
    # (StarshipSuperjam/engine-template#1067). A HARD line, not a warning: a sealed `thorough` whose
    # lenses ran at `medium` published its promise unchallenged, and the operator's merge is the only
    # place that can be met. Self-reported on both halves, and the sentence says so.
    # The caveat rides the DEPTH CLAIM itself, not only its exceptions. Stating "self-reported and
    # unverified" only when there is a shortfall to admit means the exact failure this closes — a session
    # claiming an effort it did not deliver — produces a body that reads as an unqualified assurance
    # (StarshipSuperjam/engine-template#1067).
    if cold_review_ran and _depth_effort_or_none(state):
        review_coverage += (" What effort those reviewers ran at is self-reported by the spawning session "
                            "and by the reviewers themselves; nothing in this engine verifies it.")
    effort_lines = _effort_shortfall_lines(state)
    if effort_lines:
        review_coverage += " " + "; ".join(effort_lines) + "."

    drift_line = _drift_line(state, head)

    # Index-regeneration disclosure (BO-24): which of the engine's generated surfaces this PR changed,
    # computed from the diff so the operator sees regeneration happened over generated paths only. Driven
    # from the derived-state REGISTRY (owner_of) rather than a hand-maintained literal, so it can never
    # drift from what the engine actually regenerates: a changed file inside a Codex render tree is
    # attributed to its member, and ci-assurance / the spec matrix / the catalogs appear when they change.
    sys.path.insert(0, str(ROOT / ".engine" / "tools"))
    import derived_state
    changed = set(_run(["git", "diff", "--name-only", f"{base}...HEAD"]).stdout.splitlines())
    regen = sorted({owner.path for f in changed if (owner := derived_state.owner_of(f)) is not None})
    index_regen = (f"Regeneration updated {len(regen)} of the engine's generated surfaces "
                   f"({', '.join(regen)}) from the final tree — generated paths only."
                   if regen else "")

    marker = f"<!-- engine-pr-contract:v1 {_digest(claim)} commit={head} -->"

    # Post-approval assumption resolutions, rendered for the operator's merge surface (the PR Review record),
    # not just `status` (StarshipSuperjam/engine-template#1014). Only an assumption authored 'unresolved' can
    # carry a disposition, so its presence is the "after approval" signal; both verified and accepted-risk are
    # disclosed so a self-attested post-hoc resolution can never vanish before the operator sees it.
    authored_unresolved = {a["claim"] for a in plan.get("assumptions", []) if a["status"] == "unresolved"}
    assumption_resolutions = [
        f"{d['claim']} -> {d['resolved_as']} (self-attested, not re-reviewed) — basis: {d['basis']}"
        for d in state.get("assumption_dispositions", []) if d["claim"] in authored_unresolved]

    # The two cost-cadence escalations, published because an escalation the operator cannot see at merge is
    # an escalation to nobody -- the whole argument for these being real rather than a self-tick. Only rounds
    # carrying guidance are emitted: rounds one and two are free by design and have nothing to disclose. The
    # "round N of M" phrasing carries the total, so a PR after three rounds cannot read like one after one.
    cadence_escalations = []
    # The "not re-reviewed" half is only true while the plan stage still HAS no receipts. Once the cap at
    # packet level was removed, a session could escalate and then cut a fresh panel on the new plan, which
    # made the body assert both "Plan review ran before any code" and "was NOT re-reviewed" about the same
    # plan. The authorization is disclosed either way; only the claim about review is conditional.
    for item in state.get("plan_change_escalations", []):
        # A plan is sealed before a Build starts and a seal is terminal, so a mid-Build change is ALWAYS
        # a change the panel did not read. There is no re-reviewed arm any more, because there is no way
        # to re-review a sealed plan.
        cadence_escalations.append(
            f"the executed plan differs from the sealed plan its panel read, on recorded operator "
            f"authority and without re-review: {item['operator_change']}")
    rounds = state.get("repair_rounds", [])
    for index, item in enumerate(rounds, start=1):
        if item.get("guidance"):
            cadence_escalations.append(
                f"repair round {index} of {len(rounds)} proceeded past the escalation point on recorded "
                f"operator guidance: {item['guidance']}")

    return {
        "closes": closes,
        "change_profile": change_profile,
        "validation_results": validation_results,
        "index_regen": index_regen,
        "spec_steps": spec_steps,
        "review_coverage": review_coverage,
        "code_execution_line": code_execution_line,
        "disagreement_lines": _plan_disagreement_lines(state) + review.required_disagreement_lines(state),
        "plan_finding_lines": _plan_finding_lines(state),
        "consent_lines": _plan_consent_lines(state),
        "obligation_lines": _plan_obligation_lines(state),
        "assumption_resolutions": assumption_resolutions,
        "cadence_escalations": cadence_escalations,
        "drift_line": drift_line,
        "composition_marker": marker,
        # preserved marker blocks are extracted from the live body at apply time, where the write happens.
        "preserved_blocks": [],
    }


def cmd_contract_preview(args, store: Snapshot) -> None:
    """Read-only: validate the claim and current evidence, render the candidate body, run the real
    completeness rule locally, and report the source/claim/commit/candidate digests. No GitHub write."""
    state = store.read()
    plan = _plan(args.plan)
    _assert_plan(state, plan)
    try:
        claim = composer.load_claim(args.claim)
    except composer.ContractError as exc:
        raise CoordinatorError(str(exc)) from exc
    _assert_claim_findings(state, claim)
    repo, pr = state["build"]["repository"], state["build"]["pr"]
    with core.StableCommit(ROOT, "contract preview") as head:
        pr_data = _verify_draft(repo, pr)
        source_body = pr_data.get("body") or ""
        evidence = _assemble_evidence(state, plan, claim, head, pr_data)
        try:
            body = composer.compose(claim, evidence)
        except composer.ContractError as exc:
            raise CoordinatorError(str(exc)) from exc
        github.require_body_budget(body, "composed PR contract")
        contract_passed, contract_summary = _pr_contract(body)
    if getattr(args, "output", None):
        Path(args.output).write_text(body, encoding="utf-8")
    result = {"source_body_digest": _digest(source_body.encode()), "claim_digest": _digest(claim),
              "commit": head, "candidate_body_digest": _digest(body.encode()),
              "complete": contract_passed, "summary": contract_summary}
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        state_word = "complete" if contract_passed else "incomplete"
        print(f"composed a candidate PR contract for {head[:12]}: {state_word} — {contract_summary}")


def _extract_marker_blocks(body: str) -> list:
    """The valid engine marker blocks already on the draft that a fresh compose must carry through unchanged:
    a published handoff block and the build-id marker. The pr-contract composition marker is NOT
    preserved — the composer mints a fresh one bound to the current claim digest and commit.

    One handoff generation now: the v1 marker retired with its schema, so a body carrying one holds a
    block this engine can neither validate nor rewrite, and carrying it through a fresh compose would
    republish content nothing here can vouch for."""
    blocks = []
    m = re.search(re.escape(github.HANDOFF_BEGIN_V2) + r".*?" + re.escape(github.HANDOFF_END_V2),
                  body, re.DOTALL)
    if m:
        blocks.append(m.group(0))
    m = re.search(r"<!-- engine-build-id:v1 [^\n]*?-->", body)
    if m:
        blocks.append(m.group(0))
    return blocks


def _apply_body(repo: str, pr: int, *, expected_before: str, new_body: str, revision: int, store: Snapshot) -> str:
    """One safe body write: confirm the live body is still what we last saw and Build evidence has not moved,
    write, then read back and require byte-equality. Mirrors cmd_handoff_export's proven idiom. Returns the
    written body (now the confirmed live body). Never clobbers an external edit — a mismatch refuses."""
    before = _verify_draft(repo, pr).get("body") or ""
    if before != expected_before:
        raise CoordinatorError("the PR body changed under the composer mid-apply (a concurrent edit); "
                               "no further write was made — rerun `contract preview` and apply with the new digest")
    if store.read()["revision"] != revision:
        raise CoordinatorError("Build evidence changed mid-apply; rerun `contract preview`")
    _must_run(["gh", "pr", "edit", str(pr), "--repo", repo, "--body-file", "-"], input_text=new_body)
    confirmed = _verify_draft(repo, pr).get("body") or ""
    if confirmed != new_body:
        # GitHub did not echo our exact bytes (a normalization, partial write, or transport hiccup). The
        # mangled body would not be in the caller's `wrote` set — the loop-level restore cannot recognise it —
        # so THIS write undoes itself here: roll the body back to what was there before this write, then raise.
        # (The far likelier cause is a GitHub-side transform of OUR content, not an external edit landing in the
        # sub-second read-after-write window, so undoing our own unconfirmed write is the correct default.)
        if (_verify_draft(repo, pr).get("body") or "") != expected_before:
            _must_run(["gh", "pr", "edit", str(pr), "--repo", repo, "--body-file", "-"], input_text=expected_before)
        raise CoordinatorError("GitHub did not preserve the composed body exactly; the unconfirmed write was "
                               "rolled back")
    return new_body


def _restore_after_failure(repo: str, pr: int, wrote: set, original: str) -> bool:
    """After a failed apply, restore the original body IFF the live body is one the coordinator itself wrote
    this run — so a coordinator-authored intermediate is never left live, while a genuine external edit (not in
    `wrote`) is preserved, never clobbered. No-op when nothing was written. Returns True iff it restored."""
    if not wrote:
        return False
    live = _verify_draft(repo, pr).get("body") or ""
    if live != original and live in wrote:
        _must_run(["gh", "pr", "edit", str(pr), "--repo", repo, "--body-file", "-"], input_text=original)
        return True
    return False


def _close_linkage_result(repo: str, pr: int, base: str, head: str) -> dict:
    """Run the close-linkage preflight against the (already applied) live body and parse its JSON. On a CRASH
    (non-zero exit, no parseable JSON — distinct from the tool's own fail-closed 'could not read' line, which
    it prints with exit 0) fold an explicit fail-closed disclosure so the human-facing body never silently
    omits the close-linkage check; the final recorded preflight leg also captures the failure independently."""
    close = _run([sys.executable, str(ROOT / ".engine" / "tools" / "close_linkage_preflight.py"),
                  "check", "--pr", str(pr), "--base", base, "--head", head])
    try:
        parsed = json.loads(close.stdout) if (close.stdout or "").strip() else None
    except ValueError:
        parsed = None
    if parsed is None:
        if close.returncode != 0:
            return {"lines": ["I couldn't run the close-linkage check before submitting — open the PR on GitHub "
                              "and confirm its “will close” list before you merge."], "defang": None}
        return {"lines": [], "defang": None}
    return {"lines": parsed.get("lines", []), "defang": parsed.get("defang")}


def cmd_contract_apply(args, store: Snapshot) -> None:
    """Compose the body and apply it to the still-draft PR under a source-digest compare-and-swap, folding
    close-linkage advisory lines to a fixed point (max three passes), then record the full preflight set and
    bind pr_contract to the stable body. Never marks ready and never merges."""
    if not args.ack_visibility:
        raise CoordinatorError("contract apply writes the composed body to the public PR contract; pass --ack-visibility")
    state = store.read()
    revision = state["revision"]
    plan = _plan(args.plan)
    _assert_plan(state, plan)
    try:
        claim = composer.load_claim(args.claim)
    except composer.ContractError as exc:
        raise CoordinatorError(str(exc)) from exc
    _assert_claim_findings(state, claim)
    repo, pr = state["build"]["repository"], state["build"]["pr"]
    with core.StableCommit(ROOT, "contract apply") as head:
        pr_data = _verify_draft(repo, pr)
        source_body = pr_data.get("body") or ""
        if _digest(source_body.encode()) != args.source_body_digest:
            raise CoordinatorError("the live PR body does not match --source-body-digest; a concurrent edit "
                                   "occurred — rerun `contract preview` and apply with the reported digest")
        base = pr_data.get("baseRefOid") or state["build"]["base_at_bind"]
        preserved = _extract_marker_blocks(source_body)
        # Everything but the folded close-linkage lines is invariant across passes, so assemble the evidence
        # ONCE (a fresh scope_profile subprocess and a possible live spec read are not worth repeating).
        base_evidence = _assemble_evidence(state, plan, claim, head, pr_data)
        base_evidence["preserved_blocks"] = preserved
        close_lines: list = []
        written = source_body
        wrote: set = set()          # every candidate we sent to GitHub this run — the restore set
        converged = False
        # Rollback is two-layered so no coordinator-authored body is ever left live on a failure. Layer one:
        # _apply_body undoes its OWN unconfirmed write on a GitHub echo mismatch (that mangled body is not a
        # recognisable candidate, so only the writer can clean it). Layer two, here: on ANY failure after an
        # earlier CONFIRMED write (a mid-loop revision bump, an armed close, or non-convergence) the live body
        # is a candidate we wrote, so _restore_after_failure puts back the original — while a genuine external
        # edit (not in `wrote`) is preserved, never clobbered.
        try:
            for _ in range(3):
                evidence = {**base_evidence, "close_linkage_lines": close_lines}
                candidate = composer.compose(claim, evidence)
                github.require_body_budget(candidate, "composed PR contract")
                if candidate == written:
                    converged = True
                    break
                wrote.add(candidate)
                written = _apply_body(repo, pr, expected_before=written, new_body=candidate,
                                      revision=revision, store=store)
                result = _close_linkage_result(repo, pr, base, head)
                if result["defang"]:
                    raise CoordinatorError(
                        f"the composed body armed an accidental close of #{result['defang']['number']} — that is "
                        "a composer defect, not an author edit; nothing was submitted")
                close_lines = list(result["lines"])
            if not converged:
                raise CoordinatorError("the composed body did not reach a fixed point in three passes; "
                                       "nothing was recorded")
        except composer.ContractError as exc:
            _restore_after_failure(repo, pr, wrote, source_body)
            raise CoordinatorError(str(exc)) from exc
        except Exception:
            _restore_after_failure(repo, pr, wrote, source_body)
            raise
        legs = _compute_preflight_legs(state, head, _verify_draft(repo, pr), written)
        body_digest = _digest(written.encode())

        def change(s):
            s["preflights"] = legs["results"]
            s["pr_contract"] = {"commit": head, "body_digest": body_digest, "complete": legs["contract_passed"]}
    store.mutate(change, from_revision=revision)
    result = {"commit": head, "body_digest": body_digest, "complete": legs["contract_passed"],
              "summary": legs["contract_summary"], "ready": False, "merge": False}
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        state_word = "complete" if legs["contract_passed"] else "INCOMPLETE"
        print(f"applied the composed PR contract for {head[:12]} ({state_word}); preflights recorded. "
              f"Run `submit preview` when ready — apply never marks ready.")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state", help="path to the harness-owned local Build snapshot; omitted only for standalone pre-PR packets")
    p.add_argument("--expect-revision", type=int, help="optional compare-and-swap guard")
    sub = p.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan").add_subparsers(dest="plan_command", required=True)
    bind = plan.add_parser("bind"); bind.add_argument("--plan", required=True, help="a SEALED plan in the local library, by id or by name"); bind.add_argument("--mode", choices=["same-session", "unattended"], default="same-session"); bind.add_argument("--repository", required=True); bind.add_argument("--pr", type=int, required=True); bind.add_argument("--issue", type=int, help="the Issue that AUTHORIZES this work; never its plan"); bind.add_argument("--operator-decision", help="The operator's actual words giving the go for the Build to begin. Published verbatim in the pull request's consent trail; a record, not a proof."); bind.set_defaults(func=cmd_plan_bind)
    adopt = plan.add_parser("adopt", help="consume a SEALED successor plan without restarting the Build"); adopt.add_argument("--successor", required=True, help="a sealed plan in the library that names the bound plan as its predecessor"); adopt.add_argument("--input", required=True, help="the plan this Build is currently executing, for the node-by-node comparison"); adopt.add_argument("--operator-decision", help="The operator's actual words authorising the Build to continue on the successor."); adopt.set_defaults(func=cmd_plan_adopt)
    revise = plan.add_parser("revise"); revise.add_argument("--input", required=True); revise.add_argument("--operator-change", help="The operator's decision authorizing execution of a plan that differs from the sealed one. The sealed plan is unchanged; the divergence is disclosed at merge."); revise.set_defaults(func=cmd_plan_revise)
    approve = sub.add_parser("approve"); approve.add_argument("--plan", required=True); approve.add_argument("--depth", choices=["quick", "standard", "thorough"], required=True); approve.set_defaults(func=cmd_approve)
    status = sub.add_parser("status"); status.add_argument("--plan"); status.add_argument("--json", action="store_true"); status.set_defaults(func=cmd_status)
    depths = sub.add_parser("depths"); depths.add_argument("--json", action="store_true"); depths.set_defaults(func=cmd_depths)
    review = sub.add_parser("review").add_subparsers(dest="review_command", required=True)
    packet = review.add_parser("packet"); packet.add_argument("--stage", choices=["deliverable", "repair"], required=True); packet.add_argument("--plan", required=True); packet.add_argument("--impact"); packet.add_argument("--output"); packet.add_argument("--json", action="store_true"); packet.add_argument("--standalone", action="store_true"); packet.add_argument("--repository"); packet.add_argument("--commit"); packet.add_argument("--base"); packet.add_argument("--depth", choices=["quick", "standard", "thorough"]); packet.add_argument("--session-effort", choices=["low", "medium", "high"], help="The reasoning effort THIS session is running at as it spawns the panel. On the Claude arm an un-pinned reviewer inherits it, so it is the ceiling every lens in the fan-out runs at; self-reported."); packet.add_argument("--accept-effort-shortfall", action="store_true", help="Spawn the panel knowing it runs below the approved depth's effort. The gap becomes a hard disclosure in the PR body."); packet.set_defaults(func=_packet)
    record = review.add_parser("record"); record.add_argument("--stage", choices=["deliverable", "repair"], required=True); record.add_argument("--lens", required=True); record.add_argument("--packet-digest", required=True); record.add_argument("--lens-packet-digest", required=True); record.add_argument("--finding", action="append"); record.add_argument("--findings-from-file", help="A build-findings-batch.v1 file (or -) whose ids this receipt demands. The SAME file `finding record --from-file` reads, so a receipt and its findings cannot disagree; mutually exclusive with --finding."); record.add_argument("--code-execution", choices=["none", "discarded-copy", "in-place"], required=True); record.add_argument("--delivered-effort", choices=["low", "medium", "high"], help="The reasoning effort this reviewer actually ran at, self-reported. Compared against the approved depth's effort; a shortfall is disclosed in the PR body."); record.set_defaults(func=cmd_review_record)
    finding = sub.add_parser("finding").add_subparsers(dest="finding_command", required=True)
    frecord = finding.add_parser("record"); frecord.add_argument("--id"); frecord.add_argument("--stage", choices=["deliverable", "repair"], required=True); frecord.add_argument("--lens"); frecord.add_argument("--severity", choices=["blocking", "serious", "nit"]); frecord.add_argument("--summary"); frecord.add_argument("--disposition", choices=["accepted-fixed", "accepted-tracked", "partially-accepted", "rejected", "escalated"]); frecord.add_argument("--rationale"); frecord.add_argument("--escalation-kind", choices=["design", "law", "authority", "capability-boundary", "guardrail-ack", "operator-only"]); block = frecord.add_mutually_exclusive_group(); block.add_argument("--blocks-this-pr", action="store_const", const=True, dest="blocks_this_pr_stated"); block.add_argument("--does-not-block-this-pr", action="store_const", const=False, dest="blocks_this_pr_stated"); frecord.add_argument("--handoff-summary"); frecord.add_argument("--operator-summary"); frecord.add_argument("--private-reference", help="Local-only reviewer note; kept in build-state, never published to the PR body and not read back by any verb."); frecord.add_argument("--findings-from-file", "--from-file", dest="from_file", help="A build-findings-batch.v1 file (or -) carrying a whole round's dispositions. The SAME flag name and the SAME file `review record` takes, so one cut file feeds both verbs and their ids cannot drift; --from-file remains as an alias. Validated entirely before anything is written, then recorded in one mutation: a malformed entry records nothing."); frecord.set_defaults(func=cmd_finding_record)
    assumption = sub.add_parser("assumption").add_subparsers(dest="assumption_command", required=True)
    adispose = assumption.add_parser("dispose"); adispose.add_argument("--plan", required=True); adispose.add_argument("--claim", required=True); adispose.add_argument("--as", dest="resolved_as", choices=["verified", "accepted-risk"], required=True); adispose.add_argument("--basis", required=True); adispose.set_defaults(func=cmd_assumption_dispose)
    checkpoint = sub.add_parser("checkpoint"); checkpoint.add_argument("--plan", required=True); checkpoint.add_argument("--input", required=True); checkpoint.add_argument("--json", action="store_true"); checkpoint.set_defaults(func=cmd_checkpoint)
    state_p = sub.add_parser("state").add_subparsers(dest="state_command", required=True)
    swhere = state_p.add_parser("where"); swhere.set_defaults(func=cmd_state_where)
    smigrate = state_p.add_parser("migrate"); smigrate.add_argument("--source", required=True, help="an existing OS-temp Build snapshot"); smigrate.add_argument("--plan", required=True, help="the sealed plan whose library folder receives it"); smigrate.set_defaults(func=cmd_state_migrate)
    ssupersede = state_p.add_parser("supersede"); ssupersede.add_argument("--plan", required=True); ssupersede.add_argument("--reason", required=True); ssupersede.set_defaults(func=cmd_state_supersede)
    validate = sub.add_parser("validate"); validate.add_argument("--plan", help="the approved plan; REQUIRED for a build-plan.v2 Build, whose node roster lives only there"); validate.set_defaults(func=cmd_validate)
    sync_artifacts = sub.add_parser("sync-artifacts"); sync_artifacts.set_defaults(func=cmd_sync_artifacts)
    repair = sub.add_parser("repair").add_subparsers(dest="repair_command", required=True)
    assess = repair.add_parser("assess"); assess.add_argument("--judgment", choices=["none", "scoped", "full"], required=True); assess.add_argument("--rationale", required=True); assess.add_argument("--guidance", help="The operator's answer when a third or later repair round is proposed; published in the PR body."); assess.add_argument("--lens", action="append"); assess.add_argument("--accept-receipt-loss", action="store_true", help="Re-bind even though recorded repair receipts do not cover the new divergence and will be dropped. Without it the re-bind refuses and names what each lens still owes."); assess.set_defaults(func=cmd_repair_assess)
    reconcile = sub.add_parser("reconcile"); reconcile.add_argument("--plan", required=True); reconcile.set_defaults(func=cmd_reconcile)
    preflight = sub.add_parser("preflight"); preflight.add_argument("--pr-body"); preflight.add_argument("--json", action="store_true"); preflight.set_defaults(func=cmd_preflight)
    handoff = sub.add_parser("handoff").add_subparsers(dest="handoff_command", required=True)
    export = handoff.add_parser("export"); export.add_argument("--output", default="-"); export.set_defaults(func=cmd_handoff_export)
    restore = handoff.add_parser("restore"); restore.add_argument("--input"); restore.add_argument("--repository"); restore.add_argument("--pr", type=int); restore.set_defaults(func=cmd_handoff_restore)
    submit = sub.add_parser("submit").add_subparsers(dest="submit_command", required=True)
    preview = submit.add_parser("preview"); preview.add_argument("--plan", required=True); preview.set_defaults(func=cmd_submit_preview)
    apply = submit.add_parser("apply"); apply.add_argument("--plan", required=True); apply.set_defaults(func=cmd_submit_apply)
    contract_p = sub.add_parser("contract").add_subparsers(dest="contract_command", required=True)
    ctemplate = contract_p.add_parser("template"); ctemplate.add_argument("--output", default="-"); ctemplate.set_defaults(func=cmd_contract_template)
    cpreview = contract_p.add_parser("preview"); cpreview.add_argument("--plan", required=True); cpreview.add_argument("--claim", required=True); cpreview.add_argument("--output"); cpreview.add_argument("--json", action="store_true"); cpreview.set_defaults(func=cmd_contract_preview)
    capply = contract_p.add_parser("apply"); capply.add_argument("--plan", required=True); capply.add_argument("--claim", required=True); capply.add_argument("--source-body-digest", required=True); capply.add_argument("--ack-visibility", action="store_true"); capply.add_argument("--json", action="store_true"); capply.set_defaults(func=cmd_contract_apply)
    work_p = sub.add_parser("work").add_subparsers(dest="work_command", required=True)
    wfrontier = work_p.add_parser("frontier"); wfrontier.add_argument("--plan", required=True); wfrontier.add_argument("--json", action="store_true"); wfrontier.set_defaults(func=cmd_work_frontier)
    wpacket = work_p.add_parser("packet"); wpacket.add_argument("--item", required=True); wpacket.add_argument("--provider", choices=["claude", "codex"], required=True); wpacket.add_argument("--plan", required=True); wpacket.add_argument("--worktree"); wpacket.set_defaults(func=cmd_work_packet)
    wclaim = work_p.add_parser("claim"); wclaim.add_argument("--item", required=True); wclaim.add_argument("--provider", choices=["claude", "codex"], required=True); wclaim.add_argument("--plan", required=True); wclaim.add_argument("--worktree", required=True); wclaim.set_defaults(func=cmd_work_claim)
    wattach = work_p.add_parser("attach"); wattach.add_argument("--item", required=True); wattach.add_argument("--attempt", required=True); wattach.add_argument("--worker-ref", required=True); wattach.set_defaults(func=cmd_work_attach)
    wresult = work_p.add_parser("result"); wresult.add_argument("--item", required=True); wresult.add_argument("--attempt", required=True); wresult.add_argument("--plan", required=True); wresult.add_argument("--input", required=True); wresult.set_defaults(func=cmd_work_result)
    wreject = work_p.add_parser("reject"); wreject.add_argument("--item", required=True); wreject.add_argument("--attempt", required=True); wreject.add_argument("--class", dest="rejection_class", choices=["dispatch", "worker", "contract", "verification", "integration"], required=True); wreject.add_argument("--reason", required=True); wreject.set_defaults(func=cmd_work_reject)
    wretry = work_p.add_parser("retry"); wretry.add_argument("--item", required=True); wretry.add_argument("--strategy", choices=["redispatch", "integrator-inline"], required=True); wretry.add_argument("--reason", required=True); wretry.set_defaults(func=cmd_work_retry)
    wabandon = work_p.add_parser("abandon"); wabandon.add_argument("--item", required=True); wabandon.add_argument("--attempt", required=True); wabandon.add_argument("--reason", required=True); wabandon.set_defaults(func=cmd_work_abandon)
    wintegrate = work_p.add_parser("integrate"); wintegrate.add_argument("--item", required=True); wintegrate.add_argument("--attempt", required=True); wintegrate.add_argument("--commit", required=True); wintegrate.add_argument("--verification-input", required=True); wintegrate.set_defaults(func=cmd_work_integrate)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        standalone = args.command == "review" and args.review_command == "packet" and args.standalone
        stateless = (args.command == "depths"
                     or args.command == "state"
                     or (args.command == "contract" and getattr(args, "contract_command", None) == "template"))
        # `plan bind` is the one command that has no snapshot to resolve: it is the command that
        # creates one. Without --state it chooses the durable address itself, from the plan it binds.
        binding = args.command == "plan" and getattr(args, "plan_command", None) == "bind"
        if standalone and (not args.repository or not args.depth):
            raise CoordinatorError("standalone review packets require --repository and --depth")
        deferred = binding and not args.state
        store = None if (standalone or stateless or deferred) else _resolve_store(args)
        args.func(args, store)
        return 0
    except CoordinatorError as exc:
        print(f"build-coordinator: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
