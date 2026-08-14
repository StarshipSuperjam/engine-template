#!/usr/bin/env python3
"""A small instrument panel for one PR-shaped Build.

The coordinator stores current mechanical evidence in one atomic local snapshot. It never chooses a plan,
finding remedy, operator escalation, or re-review depth, and it has no merge operation.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_PATH = ROOT / ".engine" / "build-protocol.json"
PLAN_SCHEMA = ROOT / ".engine" / "schemas" / "build-plan.v1.json"
STATE_SCHEMA = ROOT / ".engine" / "schemas" / "build-state.v1.json"
HANDOFF_SCHEMA = ROOT / ".engine" / "schemas" / "build-handoff.v1.json"
PLAN_BEGIN = "<!-- engine-build-plan:v1 "
PLAN_END = "<!-- /engine-build-plan -->"
HANDOFF_BEGIN = "<!-- engine-build-handoff:v1 "
HANDOFF_END = "<!-- /engine-build-handoff -->"
VALIDATION_COMMANDS = [
    {"id": "engine-ci", "command": ["uv", "run", "--directory", ".engine", "--frozen", "--", "python", "tools/validate.py", "--suite", "CI"]},
    {"id": "engine-selftest", "command": ["uv", "run", "--directory", ".engine", "--frozen", "--", "python", "tools/selftest.py"]},
]


class CoordinatorError(Exception):
    pass


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CoordinatorError(f"could not read JSON from {path}: {exc}") from exc


def _input(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise CoordinatorError(f"could not read {path}: {exc}") from exc


def _validate(instance: Any, schema_path: Path) -> None:
    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:
        raise CoordinatorError("the Engine runtime is missing jsonschema; run this tool through uv") from exc
    errors = sorted(Draft202012Validator(_json(schema_path)).iter_errors(instance), key=lambda e: list(e.path))
    if errors:
        where = ".".join(str(p) for p in errors[0].path) or "document"
        raise CoordinatorError(f"{schema_path.stem} rejected {where}: {errors[0].message}")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _digest(value: Any) -> str:
    payload = value if isinstance(value, bytes) else _canonical(value)
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _plan(path: str) -> dict:
    try:
        value = json.loads(_input(path))
    except ValueError as exc:
        raise CoordinatorError(f"the Build plan is not valid JSON: {exc}") from exc
    _validate(value, PLAN_SCHEMA)
    return value


def _run(argv: list[str], *, cwd: Path = ROOT, input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=cwd, input=input_text, text=True, capture_output=True, check=False)


def _must_run(argv: list[str], *, input_text: str | None = None) -> str:
    result = _run(argv, input_text=input_text)
    if result.returncode:
        detail = (result.stderr or result.stdout or "no diagnostic").strip()
        raise CoordinatorError(f"{' '.join(argv[:3])} failed: {detail}")
    return result.stdout


def _gh_json(argv: list[str]) -> Any:
    try:
        return json.loads(_must_run(["gh", *argv]))
    except ValueError as exc:
        raise CoordinatorError("GitHub returned malformed JSON") from exc


def _head() -> str:
    value = _must_run(["git", "rev-parse", "HEAD"]).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise CoordinatorError("git did not return a full commit id")
    return value


def _base() -> str:
    return _must_run(["git", "merge-base", "HEAD", "origin/HEAD"]).strip()


def _verify_draft(repo: str, pr: int) -> dict:
    data = _gh_json(["pr", "view", str(pr), "--repo", repo, "--json", "number,state,isDraft,headRefOid,baseRefOid,mergeable,body"])
    if data.get("number") != pr or data.get("state") != "OPEN" or data.get("isDraft") is not True:
        raise CoordinatorError(f"{repo}#{pr} must be the open draft claim for this Build")
    return data


class StateStore:
    def __init__(self, path: str, expected_revision: int | None = None):
        self.path = Path(path).resolve()
        temp_root = Path(tempfile.gettempdir()).resolve()
        if os.path.commonpath((self.path, temp_root)) != str(temp_root):
            raise CoordinatorError(f"Build snapshots belong in the OS temporary directory ({temp_root})")
        self.lock = self.path.with_name(self.path.name + ".lock")
        self.expected_revision = expected_revision

    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock.open("a+", encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle

    def read(self) -> dict:
        with self._locked():
            if not self.path.exists():
                raise CoordinatorError(f"no Build snapshot at {self.path}; use 'plan bind' first")
            state = _json(self.path)
            _validate(state, STATE_SCHEMA)
            return state

    def create(self, state: dict) -> None:
        with self._locked():
            if self.path.exists():
                raise CoordinatorError(f"Build snapshot already exists at {self.path}")
            self._write(state)

    def mutate(self, change: Callable[[dict], Any]) -> Any:
        with self._locked():
            if not self.path.exists():
                raise CoordinatorError(f"no Build snapshot at {self.path}; use 'plan bind' first")
            state = _json(self.path)
            _validate(state, STATE_SCHEMA)
            if self.expected_revision is not None and state["revision"] != self.expected_revision:
                raise CoordinatorError(f"snapshot revision is {state['revision']}, not expected {self.expected_revision}; reload status")
            result = change(state)
            state["revision"] += 1
            self._write(state)
            return result

    def _write(self, state: dict) -> None:
        _validate(state, STATE_SCHEMA)
        fd, temp_name = tempfile.mkstemp(prefix=self.path.name + ".", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)


def _empty_review() -> dict:
    return {"packet_digest": None, "required_lenses": [], "installed_lenses": [], "receipts": [], "reviewed_commit": None, "base_commit": None}


def _initial_state(repo: str, pr: int, base: str, source: str, plan: dict, issue: int | None,
                   mode: str = "same-session") -> dict:
    return {
        "schema_version": "build-state.v1", "revision": 1,
        "build": {"repository": repo, "pr": pr, "base_at_bind": base, "mode": mode},
        "plan": {"source": source, "digest": _digest(plan), "intent_digest": _digest(plan["raw_intent"].encode()),
                 "spec_digest": plan["spec"].get("digest"), "durable_issue": issue},
        "approval": None, "reviews": {"plan": _empty_review(), "deliverable": _empty_review()},
        "findings": [], "checkpoint": None, "validation": None, "repair": None,
        "preflights": [], "pr_contract": None, "submission": "draft"
    }


def _assert_plan(state: dict, plan: dict) -> None:
    actual = _digest(plan)
    if actual != state["plan"]["digest"]:
        raise CoordinatorError(f"supplied plan digest {actual} does not match approved Build plan {state['plan']['digest']}")


def _issue_body(repo: str, issue: int) -> str:
    data = _gh_json(["issue", "view", str(issue), "--repo", repo, "--json", "number,state,body"])
    if data.get("number") != issue or data.get("state") != "OPEN":
        raise CoordinatorError(f"{repo}#{issue} is not an open Issue suitable for durable Build scope")
    return data.get("body") or ""


def _plan_block(plan: dict) -> str:
    digest = _digest(plan)
    rendered = json.dumps(plan, indent=2, sort_keys=True, ensure_ascii=False)
    return f"{PLAN_BEGIN}{digest} -->\n```json\n{rendered}\n```\n{PLAN_END}"


def _replace_plan_block(body: str, plan: dict) -> str:
    pattern = re.compile(re.escape(PLAN_BEGIN) + r".*?" + re.escape(PLAN_END), re.DOTALL)
    block = _plan_block(plan)
    matches = list(pattern.finditer(body))
    if len(matches) > 1:
        raise CoordinatorError("Issue contains more than one Build plan block; resolve it manually")
    if matches:
        return body[:matches[0].start()] + block + body[matches[0].end():]
    return body.rstrip() + ("\n\n" if body.strip() else "") + block + "\n"


def _durable_plan(body: str) -> dict:
    pattern = re.compile(re.escape(PLAN_BEGIN) + r"(sha256:[0-9a-f]{64}) -->\n```json\n(.*?)\n```\n" + re.escape(PLAN_END), re.DOTALL)
    matches = list(pattern.finditer(body))
    if len(matches) != 1:
        raise CoordinatorError("durable Issue has no unique engine-build-plan:v1 block")
    try:
        plan = json.loads(matches[0].group(2))
    except ValueError as exc:
        raise CoordinatorError("durable Issue plan block is malformed") from exc
    _validate(plan, PLAN_SCHEMA)
    if _digest(plan) != matches[0].group(1):
        raise CoordinatorError("durable Issue plan content does not match its marker digest")
    return plan


def _publish_issue(repo: str, issue: int, plan: dict) -> None:
    before = _issue_body(repo, issue)
    after = _replace_plan_block(before, plan)
    # Re-read before writing: a concurrent human edit aborts instead of being overwritten.
    if _issue_body(repo, issue) != before:
        raise CoordinatorError("Issue changed while preparing the plan; no write was made")
    _must_run(["gh", "issue", "edit", str(issue), "--repo", repo, "--body-file", "-"], input_text=after)
    confirmed = _issue_body(repo, issue)
    if confirmed != after or _digest(_durable_plan(confirmed)) != _digest(plan):
        raise CoordinatorError("GitHub did not preserve the exact durable plan; cold handoff is not safe")


def _installed(stage: str) -> list[str]:
    role = "plan-review" if stage == "plan" else "pre-submission-review"
    found = []
    for path in sorted((ROOT / ".claude" / "agents").glob("*.md")):
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
            found.append(fields["lens"])
    return sorted(set(found))


def _required(protocol: dict, stage: str, depth: str, installed: list[str]) -> list[str]:
    table = protocol["plan_review" if stage == "plan" else "deliverable_review"]
    return installed if depth == "thorough" else [lens for lens in table[depth] if lens in installed]


def _missing_findings(state: dict) -> list[str]:
    expected = []
    for stage_name, stage in state["reviews"].items():
        for receipt in stage["receipts"]:
            expected.extend((finding_id, stage_name, receipt) for finding_id in receipt["finding_ids"])
    if state["repair"]:
        for receipt in state["repair"]["receipts"]:
            expected.extend((finding_id, "repair", receipt) for finding_id in receipt["finding_ids"])
    actual = state["findings"]
    return sorted(finding_id for finding_id, stage, receipt in expected if not any(
        finding["id"] == finding_id and finding["stage"] == stage and finding["lens"] == receipt["lens"]
        and finding["packet_digest"] == receipt["packet_digest"] and finding["commit"] == receipt["commit"]
        for finding in actual))


def _missing_receipts(stage: dict) -> list[str]:
    done = {r["lens"] for r in stage["receipts"]}
    return [lens for lens in stage["required_lenses"] if lens not in done]


def _status(state: dict, plan: dict | None = None) -> dict:
    head = _head()
    required_evidence, judgments, warnings = [], [], []
    unresolved_assumptions: list[str] = []
    plan_stage, delivery = state["reviews"]["plan"], state["reviews"]["deliverable"]
    missing_findings = _missing_findings(state)
    blocking = [f["id"] for f in state["findings"] if f["blocks_this_pr"]]

    if state["approval"] is None or state["approval"].get("plan_digest") != state["plan"]["digest"]:
        required_evidence.append("operator approval of this plan digest and review depth")
    if plan_stage["packet_digest"] is None:
        required_evidence.append("plan-review packet")
    else:
        required_evidence.extend(f"plan-review receipt: {x}" for x in _missing_receipts(plan_stage))
    required_evidence.extend(f"finding disposition: {x}" for x in missing_findings)
    if blocking:
        judgments.append("resolve or deliberately re-disposition findings blocking this PR: " + ", ".join(blocking))
    if state["checkpoint"] and state["checkpoint"]["judgment"] != "aligned":
        judgments.append(state["checkpoint"]["judgment"])
    if state["validation"] is None or state["validation"]["commit"] != head or not all(x["passed"] for x in (state["validation"] or {}).get("results", [])):
        required_evidence.append("green validation for the final commit")
    if delivery["packet_digest"] is None:
        required_evidence.append("deliverable-review packet")
    else:
        required_evidence.extend(f"deliverable-review receipt: {x}" for x in _missing_receipts(delivery))
    if delivery["reviewed_commit"] and delivery["reviewed_commit"] != head:
        repair = state["repair"]
        if not repair or repair["reviewed_commit"] != delivery["reviewed_commit"] or repair["final_commit"] != head:
            judgments.append("choose none, scoped, or full re-review for reviewed-to-final divergence")
        elif repair["judgment"] != "none":
            done = {r["lens"] for r in repair["receipts"]}
            required_evidence.extend(f"repair-review receipt: {x}" for x in repair["lenses"] if x not in done)
    protocol = _json(PROTOCOL_PATH)
    if state["approval"]:
        depth = state["approval"]["depth"]
        current_plan = _required(protocol, "plan", depth, _installed("plan"))
        current_delivery = _required(protocol, "deliverable", depth, _installed("deliverable"))
        plan_coverage_current = not (set(current_plan) - set(plan_stage["required_lenses"]))
        delivery_coverage_current = not (set(current_delivery) - set(delivery["required_lenses"]))
        if not plan_coverage_current:
            required_evidence.append("refresh plan-review coverage for the currently installed reviewers")
        if delivery["packet_digest"] and not delivery_coverage_current:
            required_evidence.append("refresh deliverable-review coverage for the currently installed reviewers")
    else:
        plan_coverage_current = delivery_coverage_current = True
    passed = {x["id"] for x in state["preflights"] if x["commit"] == head and x["passed"]}
    required_evidence.extend(f"green preflight: {x['id']}" for x in protocol["preflights"] if x["id"] not in passed)
    if not state["pr_contract"] or state["pr_contract"]["commit"] != head or not state["pr_contract"]["complete"]:
        required_evidence.append("complete PR contract for the final commit")
    if state["plan"]["source"] == "session":
        warnings.append("plan is session-local; promote it before intentional cold-session handoff")
    if plan:
        unresolved_assumptions = [x["claim"] for x in plan["assumptions"] if x["status"] == "unresolved"]
        accepted = [x["claim"] for x in plan["assumptions"] if x["status"] == "accepted-risk"]
        judgments.extend("investigate unresolved assumption: " + value for value in unresolved_assumptions)
        warnings.extend("accepted plan risk: " + value for value in accepted)

    approval_ready = state["approval"] is not None and state["approval"].get("plan_digest") == state["plan"]["digest"]
    plan_ready = plan_stage["packet_digest"] is not None and not _missing_receipts(plan_stage) and plan_coverage_current
    dispositions_ready = not missing_findings and not blocking
    valid = state["validation"] is not None and state["validation"]["commit"] == head and all(x["passed"] for x in state["validation"]["results"])
    delivery_ready = delivery["packet_digest"] is not None and not _missing_receipts(delivery) and delivery_coverage_current
    repair_ready = not delivery["reviewed_commit"] or delivery["reviewed_commit"] == head or (
        state["repair"] is not None and state["repair"]["final_commit"] == head and (state["repair"]["judgment"] == "none" or
        not [x for x in state["repair"]["lenses"] if x not in {r["lens"] for r in state["repair"]["receipts"]}]))
    preflight_ready = not [x for x in protocol["preflights"] if x["id"] not in passed]
    contract_ready = bool(state["pr_contract"] and state["pr_contract"]["commit"] == head and state["pr_contract"]["complete"])

    if not approval_ready:
        phase, next_one, available = "planning", "approve the plan and review depth", []
    elif not plan_ready:
        phase, next_one, available = "plan-review", "prepare or complete the plan review", []
    elif not dispositions_ready:
        phase, next_one, available = "finding-disposition", None, ["critically adjudicate outstanding findings", "revise the plan if the agreed design changed"]
    elif unresolved_assumptions or (state["checkpoint"] and state["checkpoint"]["judgment"] != "aligned"):
        phase, next_one, available = "engineering-decision", None, ["investigate unresolved assumptions", "revise the plan if the agreed design changed", "obtain a genuine operator decision only when required"]
    elif not valid:
        phase, next_one, available = "implementation", None, ["continue implementation", "run focused verification", "run final validation when the change is cohesive"]
    elif not delivery_ready:
        phase, next_one, available = "deliverable-review", "prepare or complete the deliverable review", []
    elif not repair_ready:
        phase, next_one, available = "repair-assessment", "record the proportional re-review judgment", []
    elif not preflight_ready or not contract_ready:
        phase, next_one, available = "submission-preflight", "run submission preflights", []
    else:
        phase, next_one, available = "ready", "preview submission", []
    return {"phase": phase, "head_commit": head, "snapshot_revision": state["revision"],
            "required_evidence": required_evidence, "engineering_judgment": judgments,
            "warnings": warnings, "suggested_next": next_one, "available_activities": available}


def cmd_plan_bind(args, store: StateStore) -> None:
    plan = _plan(args.input)
    mode = getattr(args, "mode", "same-session")
    if mode == "unattended" and args.source != "issue":
        raise CoordinatorError("unattended Builds require an exact durable Issue plan")
    pr = _verify_draft(args.repository, args.pr)
    if pr.get("headRefOid") != _head():
        raise CoordinatorError("the draft PR head does not match this worktree")
    issue = args.issue if args.source == "issue" else None
    if args.source == "issue":
        if issue is None:
            raise CoordinatorError("--issue is required for an Issue plan")
        durable = _durable_plan(_issue_body(args.repository, issue))
        if _digest(durable) != _digest(plan):
            raise CoordinatorError("supplied plan does not match the durable Issue plan")
    state = _initial_state(args.repository, args.pr, pr.get("baseRefOid") or _base(), args.source, plan, issue, mode)
    store.create(state)
    print(json.dumps({"plan_digest": state["plan"]["digest"], "state": str(store.path)}))


def cmd_plan_promote(args, store: StateStore) -> None:
    if not args.ack_visibility:
        raise CoordinatorError("promotion publishes the exact plan in an Issue body; pass --ack-visibility")
    plan = _plan(args.input)
    state = store.read()
    _assert_plan(state, plan)
    _publish_issue(state["build"]["repository"], args.issue, plan)
    store.mutate(lambda s: s["plan"].update({"source": "issue", "durable_issue": args.issue}))
    print(f"promoted exact plan {state['plan']['digest']} to Issue #{args.issue}")


def _reset_after_revision(state: dict, plan: dict) -> None:
    state["plan"].update({"digest": _digest(plan), "intent_digest": _digest(plan["raw_intent"].encode()), "spec_digest": plan["spec"].get("digest")})
    state["approval"] = None
    state["reviews"] = {"plan": _empty_review(), "deliverable": _empty_review()}
    state["findings"] = []
    state["checkpoint"] = state["validation"] = state["repair"] = state["pr_contract"] = None
    state["preflights"] = []


def cmd_plan_revise(args, store: StateStore) -> None:
    plan = _plan(args.input)
    state = store.read()
    if _digest(plan) == state["plan"]["digest"]:
        print("plan content is unchanged; existing evidence remains current")
        return
    if state["plan"]["source"] == "issue":
        if not args.ack_visibility:
            raise CoordinatorError("revising a durable plan updates the Issue body; pass --ack-visibility")
        _publish_issue(state["build"]["repository"], state["plan"]["durable_issue"], plan)
    store.mutate(lambda s: _reset_after_revision(s, plan))
    print(f"revised plan to {_digest(plan)}; approval and review evidence were cleared")


def cmd_approve(args, store: StateStore) -> None:
    plan = _plan(args.plan)
    def change(state):
        _assert_plan(state, plan)
        if state["approval"] and state["approval"]["depth"] != args.depth:
            state["reviews"] = {"plan": _empty_review(), "deliverable": _empty_review()}
            state["findings"] = []
            state["validation"] = state["repair"] = state["pr_contract"] = None
            state["preflights"] = []
        state["approval"] = {"plan_digest": state["plan"]["digest"], "depth": args.depth}
    store.mutate(change)
    print(f"approved plan and {args.depth} review depth")


def cmd_status(args, store: StateStore) -> None:
    state = store.read()
    plan = None
    if args.plan:
        plan = _plan(args.plan)
        _assert_plan(state, plan)
    result = _status(state, plan)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    print(f"Phase: {result['phase']} (snapshot r{result['snapshot_revision']})")
    for label, key in (("Missing evidence", "required_evidence"), ("Engineering judgment", "engineering_judgment"), ("Warnings", "warnings")):
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


def _packet(args, store: StateStore) -> None:
    plan = _plan(args.plan)
    impact = json.loads(_input(args.impact)) if args.impact else {}
    state = store.read()
    _assert_plan(state, plan)
    if not state["approval"]:
        raise CoordinatorError("approve the plan and depth before preparing review packets")
    protocol = _json(PROTOCOL_PATH)
    stage = args.stage
    roster_stage = "plan" if stage == "plan" else "deliverable"
    installed = _installed(roster_stage)
    if stage == "repair":
        repair = state["repair"]
        if not repair or repair["judgment"] == "none":
            raise CoordinatorError("a scoped or full repair assessment is required before a repair packet")
        required = repair["lenses"]
        commit = repair["final_commit"]
        if not state["validation"] or state["validation"]["commit"] != commit or not all(x["passed"] for x in state["validation"]["results"]):
            raise CoordinatorError("green validation for the repaired commit is required before re-review")
    else:
        required = _required(protocol, stage, state["approval"]["depth"], installed)
        commit = None if stage == "plan" else _head()
        if stage == "deliverable" and (not state["validation"] or state["validation"]["commit"] != commit or not all(x["passed"] for x in state["validation"]["results"])):
            raise CoordinatorError("green validation for the current commit is required before deliverable review")
    missing = [lens for lens in required if lens not in installed]
    if missing:
        raise CoordinatorError("required reviewers are not installed: " + ", ".join(missing))
    packet = {"schema_version": "build-review-packet.v1", "stage": stage, "raw_intent": plan["raw_intent"],
              "plan": plan, "plan_digest": state["plan"]["digest"], "intent_digest": state["plan"]["intent_digest"],
              "spec": plan["spec"], "commit": commit, "base_commit": _base() if commit else None,
              "impact": impact, "protocol_digest": _digest(protocol),
              "installed_lenses": installed, "required_lenses": required}
    packet["packet_digest"] = _digest(packet)
    current = state["repair"] if stage == "repair" else state["reviews"][stage]
    if current and current.get("packet_digest") == packet["packet_digest"]:
        print(json.dumps(packet, indent=2, sort_keys=True))
        return
    def change(s):
        if stage == "repair":
            s["repair"]["packet_digest"] = packet["packet_digest"]
            s["repair"]["receipts"] = []
            s["findings"] = [f for f in s["findings"] if f["stage"] != "repair"]
        else:
            target = s["reviews"][stage]
            target.update({"packet_digest": packet["packet_digest"], "required_lenses": required,
                           "installed_lenses": installed, "receipts": [], "reviewed_commit": commit,
                           "base_commit": packet["base_commit"]})
            s["findings"] = [f for f in s["findings"] if f["stage"] != stage]
    store.mutate(change)
    print(json.dumps(packet, indent=2, sort_keys=True))


def cmd_review_record(args, store: StateStore) -> None:
    finding_ids = sorted(set(args.finding or []))
    def change(state):
        if args.stage == "repair":
            target = state["repair"]
            if not target or target["packet_digest"] != args.packet_digest:
                raise CoordinatorError("repair receipt does not match the current repair packet")
            if args.lens not in target["lenses"]:
                raise CoordinatorError(f"{args.lens} was not requested for this repair review")
            receipt = {"lens": args.lens, "packet_digest": args.packet_digest, "commit": target["final_commit"], "finding_ids": finding_ids}
            target["receipts"] = [r for r in target["receipts"] if r["lens"] != args.lens] + [receipt]
        else:
            target = state["reviews"][args.stage]
            if target["packet_digest"] != args.packet_digest:
                raise CoordinatorError("receipt does not match the current review packet")
            if args.lens not in target["required_lenses"]:
                raise CoordinatorError(f"{args.lens} was not required by the approved depth")
            receipt = {"lens": args.lens, "packet_digest": args.packet_digest,
                       "commit": target["reviewed_commit"], "finding_ids": finding_ids}
            target["receipts"] = [r for r in target["receipts"] if r["lens"] != args.lens] + [receipt]
    store.mutate(change)
    print(f"recorded {args.stage} review from {args.lens} with {len(finding_ids)} finding(s)")


def cmd_finding_record(args, store: StateStore) -> None:
    def change(state):
        target = state["repair"] if args.stage == "repair" and state["repair"] else state["reviews"][args.stage]
        packet = target["packet_digest"]
        if not packet:
            raise CoordinatorError(f"no current {args.stage} review packet")
        requested = target["lenses"] if args.stage == "repair" else target["required_lenses"]
        if args.lens not in requested:
            raise CoordinatorError(f"{args.lens} was not requested by the current {args.stage} packet")
        commit = None if args.stage == "plan" else (target["final_commit"] if args.stage == "repair" else target["reviewed_commit"])
        finding = {"id": args.id, "stage": args.stage, "lens": args.lens, "packet_digest": packet,
                   "commit": commit, "severity": args.severity,
                   "summary": args.summary, "disposition": args.disposition, "rationale": args.rationale,
                   "blocks_this_pr": args.blocks_this_pr, "handoff_summary": args.handoff_summary}
        state["findings"] = [f for f in state["findings"] if f["id"] != args.id] + [finding]
    store.mutate(change)
    print(f"recorded disposition for {args.id}; reviewer severity did not choose the remedy")


def _changed_paths(base: str) -> list[str]:
    paths = set(_must_run(["git", "diff", "--name-only", f"{base}..HEAD"]).splitlines())
    paths.update(_must_run(["git", "diff", "--name-only"]).splitlines())
    paths.update(_must_run(["git", "diff", "--cached", "--name-only"]).splitlines())
    return sorted(x for x in paths if x)


def cmd_checkpoint(args, store: StateStore) -> None:
    plan = _plan(args.plan)
    try:
        note = json.loads(_input(args.input))
    except ValueError as exc:
        raise CoordinatorError(f"checkpoint input is not JSON: {exc}") from exc
    required = {"objective", "current_work", "assumptions", "non_goals", "planned_scope", "remaining_verification", "judgment"}
    if not required.issubset(note):
        raise CoordinatorError("checkpoint is missing: " + ", ".join(sorted(required - set(note))))
    def change(state):
        _assert_plan(state, plan)
        if not state["approval"]:
            raise CoordinatorError("the Build gate is not approved")
        note.update({"plan_digest": state["plan"]["digest"], "changed_paths": _changed_paths(state["build"]["base_at_bind"])})
        state["checkpoint"] = note
    store.mutate(change)
    print(json.dumps(note, indent=2, sort_keys=True))


def cmd_validate(args, store: StateStore) -> None:
    store.read()
    head = _head()
    results = []
    for item in VALIDATION_COMMANDS:
        result = _run(item["command"])
        detail = (result.stdout + "\n" + result.stderr).strip()
        summary = (detail[-1000:] if detail else f"exit {result.returncode}")
        results.append({"id": item["id"], "commit": head, "passed": result.returncode == 0, "summary": summary})
    store.mutate(lambda s: s.update({"validation": {"commit": head, "results": results}}))
    print(json.dumps({"commit": head, "results": results}, indent=2, sort_keys=True))
    if not all(x["passed"] for x in results):
        raise CoordinatorError("validation failed; the failed results remain recorded")


def cmd_repair_assess(args, store: StateStore) -> None:
    head = _head()
    state = store.read()
    reviewed = state["reviews"]["deliverable"]["reviewed_commit"]
    prior = state["repair"]
    if prior and prior["judgment"] != "none" and not [
        lens for lens in prior["lenses"] if lens not in {receipt["lens"] for receipt in prior["receipts"]}
    ]:
        reviewed = prior["final_commit"]
    if not reviewed:
        raise CoordinatorError("deliverable review has not recorded a reviewed commit")
    summary = _must_run(["git", "diff", "--shortstat", f"{reviewed}..{head}"]).strip() or "no textual diff"
    lenses = sorted(set(args.lens or []))
    if args.judgment == "none" and lenses:
        raise CoordinatorError("a none judgment cannot request review lenses")
    if args.judgment == "scoped" and not lenses:
        raise CoordinatorError("a scoped judgment must name at least one --lens")
    if args.judgment == "full":
        lenses = _required(_json(PROTOCOL_PATH), "deliverable", "thorough", _installed("deliverable"))
    repair = {"reviewed_commit": reviewed, "final_commit": head, "summary": summary, "judgment": args.judgment,
              "rationale": args.rationale, "lenses": lenses, "packet_digest": None, "receipts": []}
    store.mutate(lambda s: s.update({"repair": repair}))
    print(json.dumps(repair, indent=2, sort_keys=True))


def _pr_contract(body: str) -> tuple[bool, str]:
    sys.path.insert(0, str(ROOT / ".engine" / "tools"))
    import validate
    rule = _json(ROOT / ".engine" / "check" / "pr-body-completeness.json")
    verdict, findings = validate.kind_presence(rule, {"pr_body": body})
    return verdict, "; ".join(f["message"] for f in findings) or "all required sections and consent anchors are filled"


def cmd_preflight(args, store: StateStore) -> None:
    state = store.read()
    head = _head()
    repo, pr = state["build"]["repository"], state["build"]["pr"]
    pr_data = _verify_draft(repo, pr)
    body = pr_data.get("body") or ""
    if args.pr_body and _input(args.pr_body) != body:
        raise CoordinatorError("the supplied PR body is not the body currently on GitHub")
    close = _run([sys.executable, str(ROOT / ".engine" / "tools" / "close_linkage_preflight.py"), "check", "--pr", str(pr), "--base", pr_data.get("baseRefOid") or state["build"]["base_at_bind"], "--head", head])
    close_passed = close.returncode == 0
    close_summary = (close.stdout or close.stderr or "no close-linkage output").strip()
    if close_passed:
        try:
            close_payload = json.loads(close.stdout)
            close_passed = close_payload.get("defang") is None
        except ValueError:
            close_passed = False
    contract_passed, contract_summary = _pr_contract(body)
    results = [
        {"id": "close-linkage", "commit": head, "passed": close_passed, "summary": close_summary},
        {"id": "pr-contract", "commit": head, "passed": contract_passed, "summary": contract_summary},
    ]
    def change(s):
        s["preflights"] = results
        s["pr_contract"] = {"commit": head, "complete": contract_passed}
    store.mutate(change)
    print(json.dumps(results, indent=2, sort_keys=True))
    if not all(x["passed"] for x in results):
        raise CoordinatorError("one or more submission preflights need attention")


def _handoff(state: dict) -> dict:
    if state["plan"]["source"] != "issue" or not state["plan"]["durable_issue"]:
        raise CoordinatorError("promote the exact plan to a suitable Issue before cold-session handoff")
    summaries = []
    for finding in state["findings"]:
        if not finding["handoff_summary"]:
            raise CoordinatorError(f"finding {finding['id']} needs a non-sensitive --handoff-summary")
        summaries.append({"id": finding["id"], "stage": finding["stage"], "lens": finding["lens"],
                          "packet_digest": finding["packet_digest"], "commit": finding["commit"],
                          "disposition": finding["disposition"],
                          "blocks_this_pr": finding["blocks_this_pr"], "summary": finding["handoff_summary"]})
    validation = None if not state["validation"] else {
        "commit": state["validation"]["commit"],
        "results": [{"id": x["id"], "commit": x["commit"], "passed": x["passed"]} for x in state["validation"]["results"]],
    }
    repair = None if not state["repair"] else {k: v for k, v in state["repair"].items() if k != "rationale"}
    preflights = [{"id": x["id"], "commit": x["commit"], "passed": x["passed"]} for x in state["preflights"]]
    value = {"schema_version": "build-handoff.v1", "build": state["build"], "plan": state["plan"],
             "approval": state["approval"], "reviews": state["reviews"], "finding_summaries": summaries,
             "validation": validation, "repair": repair, "preflights": preflights,
             "pr_contract": state["pr_contract"]}
    _validate(value, HANDOFF_SCHEMA)
    return value


def cmd_handoff_export(args, store: StateStore) -> None:
    value = _handoff(store.read())
    rendered = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if args.publish:
        if not args.ack_visibility:
            raise CoordinatorError("handoff publication places redacted evidence in the PR contract; pass --ack-visibility")
        repo, pr = value["build"]["repository"], value["build"]["pr"]
        before = _verify_draft(repo, pr).get("body") or ""
        block = f"{HANDOFF_BEGIN}{_digest(value)} -->\n```json\n{rendered.rstrip()}\n```\n{HANDOFF_END}"
        pattern = re.compile(re.escape(HANDOFF_BEGIN) + r".*?" + re.escape(HANDOFF_END), re.DOTALL)
        after = pattern.sub(block, before) if pattern.search(before) else before.rstrip() + "\n\n" + block + "\n"
        if (_verify_draft(repo, pr).get("body") or "") != before:
            raise CoordinatorError("PR contract changed while preparing handoff; no write was made")
        _must_run(["gh", "pr", "edit", str(pr), "--repo", repo, "--body-file", "-"], input_text=after)
        confirmed = _verify_draft(repo, pr).get("body") or ""
        if confirmed != after:
            raise CoordinatorError("GitHub did not preserve the exact handoff block")
        print(f"published bounded handoff snapshot in {repo}#{pr}")
        return
    if args.output == "-":
        print(rendered, end="")
    else:
        Path(args.output).write_text(rendered, encoding="utf-8")
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


def cmd_handoff_restore(args, store: StateStore) -> None:
    if args.input:
        rendered = _input(args.input)
    else:
        if not args.repository or not args.pr:
            raise CoordinatorError("restore needs --input or both --repository and --pr")
        body = _gh_json(["pr", "view", str(args.pr), "--repo", args.repository, "--json", "body"]).get("body") or ""
        pattern = re.compile(re.escape(HANDOFF_BEGIN) + r"(sha256:[0-9a-f]{64}) -->\n```json\n(.*?)\n```\n" + re.escape(HANDOFF_END), re.DOTALL)
        matches = list(pattern.finditer(body))
        if len(matches) != 1:
            raise CoordinatorError("PR contract has no unique engine-build-handoff:v1 block")
        rendered = matches[0].group(2)
        if _digest(json.loads(rendered)) != matches[0].group(1):
            raise CoordinatorError("PR handoff content does not match its marker digest")
    value = json.loads(rendered)
    if value.get("schema_version") != "build-handoff.v1":
        raise CoordinatorError("legacy Build handoff is unsupported; verify the PR and start with a fresh plan bind")
    _validate(value, HANDOFF_SCHEMA)
    repo, issue = value["build"]["repository"], value["plan"]["durable_issue"]
    plan = _durable_plan(_issue_body(repo, issue))
    if _digest(plan) != value["plan"]["digest"]:
        raise CoordinatorError("durable plan is missing or changed; cold continuation is blocked")
    state = {"schema_version": "build-state.v1", "revision": 1, "build": value["build"], "plan": value["plan"],
             "approval": value["approval"], "reviews": value["reviews"],
             "findings": [{"id": f["id"], "stage": f["stage"], "lens": f["lens"], "packet_digest": f["packet_digest"],
                           "commit": f["commit"], "severity": "nit", "summary": f["summary"], "disposition": f["disposition"],
                           "rationale": f["summary"], "blocks_this_pr": f["blocks_this_pr"], "handoff_summary": f["summary"]}
                          for f in value["finding_summaries"]],
             "checkpoint": None, "validation": _restore_result_set(value["validation"]),
             "repair": _restore_repair(value["repair"]), "preflights": _restore_results(value["preflights"]),
             "pr_contract": value["pr_contract"], "submission": "draft"}
    store.create(state)
    print(f"restored Build snapshot from durable Issue #{issue}")


def _submit_preview(store: StateStore, plan_path: str) -> dict:
    state = store.read()
    plan = _plan(plan_path)
    _assert_plan(state, plan)
    status = _status(state, plan)
    pr = _gh_json(["pr", "view", str(state["build"]["pr"]), "--repo", state["build"]["repository"],
                   "--json", "number,state,isDraft,headRefOid,baseRefOid,mergeable,body"])
    if pr.get("number") != state["build"]["pr"] or pr.get("state") != "OPEN":
        raise CoordinatorError("the expected pull request is not open")
    if pr.get("headRefOid") != status["head_commit"]:
        raise CoordinatorError("the PR head does not match the local final commit")
    if pr.get("mergeable") != "MERGEABLE":
        raise CoordinatorError("the PR is not currently confirmed mergeable; reconcile or retry the live check before submission")
    base = pr.get("baseRefOid")
    if not base or _run(["git", "merge-base", "--is-ancestor", base, status["head_commit"]]).returncode:
        raise CoordinatorError("the final commit does not contain the live target-branch base; reconcile, validate, and assess review proportionally")
    if status["phase"] != "ready":
        raise CoordinatorError("submission evidence is incomplete: " + "; ".join(status["required_evidence"] + status["engineering_judgment"]))
    action = "mark-ready" if pr.get("isDraft") else "record-ready"
    return {"repository": state["build"]["repository"], "pr": state["build"]["pr"], "commit": status["head_commit"], "action": action, "merge": False}


def cmd_submit_preview(args, store: StateStore) -> None:
    print(json.dumps(_submit_preview(store, args.plan), indent=2, sort_keys=True))


def cmd_submit_apply(args, store: StateStore) -> None:
    preview = _submit_preview(store, args.plan)
    if preview["action"] == "mark-ready":
        _must_run(["gh", "pr", "ready", str(preview["pr"]), "--repo", preview["repository"]])
    store.mutate(lambda s: s.update({"submission": "ready"}))
    print(f"marked {preview['repository']}#{preview['pr']} ready for the operator; no merge was attempted")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state", required=True, help="path to the harness-owned local Build snapshot")
    p.add_argument("--expect-revision", type=int, help="optional compare-and-swap guard")
    sub = p.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan").add_subparsers(dest="plan_command", required=True)
    bind = plan.add_parser("bind"); bind.add_argument("--input", required=True); bind.add_argument("--source", choices=["session", "issue"], required=True); bind.add_argument("--mode", choices=["same-session", "unattended"], default="same-session"); bind.add_argument("--repository", required=True); bind.add_argument("--pr", type=int, required=True); bind.add_argument("--issue", type=int); bind.set_defaults(func=cmd_plan_bind)
    promote = plan.add_parser("promote"); promote.add_argument("--input", required=True); promote.add_argument("--issue", type=int, required=True); promote.add_argument("--ack-visibility", action="store_true"); promote.set_defaults(func=cmd_plan_promote)
    revise = plan.add_parser("revise"); revise.add_argument("--input", required=True); revise.add_argument("--ack-visibility", action="store_true"); revise.set_defaults(func=cmd_plan_revise)
    approve = sub.add_parser("approve"); approve.add_argument("--plan", required=True); approve.add_argument("--depth", choices=["quick", "standard", "thorough"], required=True); approve.set_defaults(func=cmd_approve)
    status = sub.add_parser("status"); status.add_argument("--plan"); status.add_argument("--json", action="store_true"); status.set_defaults(func=cmd_status)
    review = sub.add_parser("review").add_subparsers(dest="review_command", required=True)
    packet = review.add_parser("packet"); packet.add_argument("--stage", choices=["plan", "deliverable", "repair"], required=True); packet.add_argument("--plan", required=True); packet.add_argument("--impact"); packet.set_defaults(func=_packet)
    record = review.add_parser("record"); record.add_argument("--stage", choices=["plan", "deliverable", "repair"], required=True); record.add_argument("--lens", required=True); record.add_argument("--packet-digest", required=True); record.add_argument("--finding", action="append"); record.set_defaults(func=cmd_review_record)
    finding = sub.add_parser("finding").add_subparsers(dest="finding_command", required=True)
    frecord = finding.add_parser("record"); frecord.add_argument("--id", required=True); frecord.add_argument("--stage", choices=["plan", "deliverable", "repair"], required=True); frecord.add_argument("--lens", required=True); frecord.add_argument("--severity", choices=["blocking", "serious", "nit"], required=True); frecord.add_argument("--summary", required=True); frecord.add_argument("--disposition", choices=["accepted-fixed", "accepted-tracked", "partially-accepted", "rejected", "escalated"], required=True); frecord.add_argument("--rationale", required=True); block = frecord.add_mutually_exclusive_group(required=True); block.add_argument("--blocks-this-pr", action="store_true"); block.add_argument("--does-not-block-this-pr", action="store_false", dest="blocks_this_pr"); frecord.add_argument("--handoff-summary"); frecord.set_defaults(func=cmd_finding_record)
    checkpoint = sub.add_parser("checkpoint"); checkpoint.add_argument("--plan", required=True); checkpoint.add_argument("--input", required=True); checkpoint.set_defaults(func=cmd_checkpoint)
    validate = sub.add_parser("validate"); validate.set_defaults(func=cmd_validate)
    repair = sub.add_parser("repair").add_subparsers(dest="repair_command", required=True)
    assess = repair.add_parser("assess"); assess.add_argument("--judgment", choices=["none", "scoped", "full"], required=True); assess.add_argument("--rationale", required=True); assess.add_argument("--lens", action="append"); assess.set_defaults(func=cmd_repair_assess)
    preflight = sub.add_parser("preflight"); preflight.add_argument("--pr-body"); preflight.set_defaults(func=cmd_preflight)
    handoff = sub.add_parser("handoff").add_subparsers(dest="handoff_command", required=True)
    export = handoff.add_parser("export"); export.add_argument("--output", default="-"); export.add_argument("--publish", action="store_true"); export.add_argument("--ack-visibility", action="store_true"); export.set_defaults(func=cmd_handoff_export)
    restore = handoff.add_parser("restore"); restore.add_argument("--input"); restore.add_argument("--repository"); restore.add_argument("--pr", type=int); restore.set_defaults(func=cmd_handoff_restore)
    submit = sub.add_parser("submit").add_subparsers(dest="submit_command", required=True)
    preview = submit.add_parser("preview"); preview.add_argument("--plan", required=True); preview.set_defaults(func=cmd_submit_preview)
    apply = submit.add_parser("apply"); apply.add_argument("--plan", required=True); apply.set_defaults(func=cmd_submit_apply)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    store = StateStore(args.state, args.expect_revision)
    try:
        args.func(args, store)
        return 0
    except CoordinatorError as exc:
        print(f"build-coordinator: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
