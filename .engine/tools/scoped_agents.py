#!/usr/bin/env python3
"""Scoped native assignments and private observed execution companions.

No scheduler or mailbox is implemented here. Native runtimes own execution. Hooks prevent known
misuse; review ingress requires observed facts. These same-user files are fallible provenance, not
a security boundary. Legacy plan and Build records are never extended or backfilled by this module.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import stat
import sys
import uuid
from pathlib import Path

import build_coordinator_core as core
import hooks
import moment
import plan_store
import providers
import result_contracts

VERSION = "scoped-agent-evidence.v1"
FILENAME = "scoped-agent-evidence.v1.json"
BLOCK_INVARIANT = {"event": "PreToolUse", "name": "scoped-assignment-gate",
                   "owner": "scoped_agents", "modes": ["explore", "build", "routine"]}


class EvidenceError(core.CoordinatorError):
    pass


def plan_owner(record, receipt=None):
    source = receipt if receipt is not None else record["approval"]
    return {"kind": "plan", "plan": record["plan_id"],
            "revision": source["revision"], "digest": source["plan_digest"]}


def build_owner(state):
    return {"kind": "build", "plan": state["plan"]["plan_id"],
            "build_id": state["ownership"]["build_id"], "generation": state["ownership"]["generation"],
            "digest": state["plan"]["digest"]}


def receipt_key(receipt):
    # Receipt identity survives existing finding correction/disposition and timestamp recovery.
    # Original observed output remains immutable in the companion; those editorial operations
    # neither add coverage nor attest another native execution.
    identity = {k: v for k, v in receipt.items() if k != "at"}
    if "findings" in identity:
        identity["findings"] = [{"id": f["id"], "lens": f["lens"]} for f in identity["findings"]]
    return core.digest(identity)


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _bounded_input(path, label):
    """Bound regular-file input before allocation, including a file that grows after stat."""
    maximum = providers.SCOPED_READ_MAX_BYTES
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise EvidenceError(f"{label} requires a regular UTF-8 text file")
        if info.st_size > maximum:
            raise EvidenceError(f"narrow the {label} to at most {maximum} UTF-8 bytes before dispatch")
        content = stream.read(maximum + 1)
        if len(content) > maximum:
            raise EvidenceError(f"narrow the {label} to at most {maximum} UTF-8 bytes before dispatch")
        return content


class Store:
    """A companion beside one plan, locked by that plan's existing record lock.

    Review callers holding the plan/Build transaction lock use the *_locked methods. Staged facts
    are not accepted coverage: acceptance additionally binds the exact legacy receipt digest.
    """

    def __init__(self, library, slug):
        self.library, self.slug = library, slug
        self.path = library.plan_dir(slug) / FILENAME

    def read(self):
        if not self.path.exists():
            return {"schema_version": VERSION, "assignments": {}, "acceptances": {}, "starts": {}}
        value = core.json_file(self.path)
        if not isinstance(value, dict) or value.get("schema_version") != VERSION or not isinstance(value.get("assignments"), dict):
            raise EvidenceError("unsupported or damaged scoped-assignment companion; review is unverified")
        schema = Path(__file__).resolve().parents[1] / "schemas" / (VERSION + ".json")
        try:
            core.validate(value, schema)
        except core.CoordinatorError as exc:
            raise EvidenceError("damaged scoped-assignment companion; review is unverified: " + str(exc)) from exc
        return value

    def write_locked(self, value):
        self.library._write_json(self.path, value)

    def change(self, fn):
        with plan_store.exclusive_lock_for(self.library, self.slug):
            data = self.read()
            before = copy.deepcopy(data)
            result = fn(data)
            if data != before:
                self.write_locked(data)
            return result

    def register(self, *, owner, root, purpose, lens, role, packet, packet_digest, expected_file_digest=None):
        """Freeze a uniquely located packet before native dispatch. Provider is not caller-selected."""
        if not all(_text(x) for x in (root, purpose, role, packet_digest)) or not isinstance(owner, dict):
            raise EvidenceError("assignment requires explicit owner, root, purpose, role and packet identity")
        source = Path(packet).resolve()
        content = _bounded_input(source, "packet")
        if expected_file_digest is not None and core.digest(content) != expected_file_digest:
            raise EvidenceError("Generated review packet changed before freezing; regenerate the packet before dispatch.")
        if len(content) > providers.SCOPED_READ_MAX_BYTES:
            raise EvidenceError(f"Review packet exceeds {providers.SCOPED_READ_MAX_BYTES} UTF-8 bytes; narrow the packet before dispatch.")
        from validate import frontmatter
        persona = Path(__file__).resolve().parents[2] / ".claude/agents" / (role + ".md")
        if not persona.is_file():
            raise EvidenceError("registered persona is missing; cannot bind its result contract")
        fields = frontmatter(str(persona))
        binding = result_contracts.resolve(fields.get("output-contract"), role=fields.get("role"))
        token = "sa_" + uuid.uuid4().hex
        directory = self.path.parent / "scoped-packets"
        self.library._mkdir(directory)
        location = directory / (token + source.suffix)
        core.atomic_write(location, content.decode("utf-8"), durable=True, mode=0o600)
        assignment = {"id": token, "owner": copy.deepcopy(owner), "root": root,
                      "purpose": purpose, "lens": lens, "role": role,
                      "result_contract": binding,
                      "packet_path": str(location), "packet_digest": packet_digest,
                      "file_digest": core.digest(content), "created_at": moment.utc_now(),
                      "launch": None, "child": None, "start": None, "read": None,
                      "continuations": [], "supplements": [], "stops": [], "faults": [], "accepted": False}
        self.change(lambda data: data["assignments"].__setitem__(token, assignment))
        return assignment

    def clarify(self, assignment_id, root, content):
        """Stage the actual clarification privately; the native send still belongs to the controller."""
        if not _text(content):
            raise EvidenceError("clarification must contain useful non-empty text")
        if len(content.encode("utf-8")) > providers.SCOPED_READ_MAX_BYTES:
            raise EvidenceError(f"Clarification exceeds {providers.SCOPED_READ_MAX_BYTES} UTF-8 bytes; narrow the supplement before dispatch.")
        def update(data):
            a = data["assignments"].get(assignment_id)
            if not a or a["root"] != root or not a["child"] or a["accepted"]:
                raise EvidenceError("clarification requires the owning root and an open observed assignment")
            if any(not s.get("call_id") for s in a["supplements"]) or any(not c["delivered"] for c in a["continuations"]):
                raise EvidenceError("reconcile the outstanding clarification before preparing another")
            path = self.path.parent / "scoped-packets" / (uuid.uuid4().hex + ".clarification.txt")
            core.atomic_write(path, content, durable=True, mode=0o600)
            supplement = {"path": str(path), "digest": core.digest(content.encode()), "call_id": None}
            a["supplements"].append(supplement)
            return supplement
        return self.change(update)

    def observe(self, event, payload):
        call = providers.scoped_call(payload)
        root = call.get("root")
        if not _text(root):
            return hooks.proceed()

        def update(data):
            owned = [a for a in data["assignments"].values() if a["root"] == root]
            if not owned:
                return hooks.proceed()
            actor = call.get("child")
            kind = call["kind"]
            if event == "SubagentStart" and _text(actor):
                start = {"root": root, "child": actor, "role": call.get("role")}
                transcript = providers.scoped_transcript(payload, call["provider"], metadata_only=True)
                if (call["provider"] == providers.CODEX and _text(transcript.get("name")) and
                        (transcript.get("child"), transcript.get("root")) == (actor, root)):
                    start["name"] = transcript["name"]
                if call["provider"] == providers.CODEX and not any(
                        start.get("name") == "/root/" + a["id"] and start["role"] == a["role"] for a in owned):
                    return hooks.proceed()
                previous = data["starts"].get(actor)
                if previous is not None and any(previous.get(k) != start.get(k) for k in ("root", "child", "role")):
                    raise EvidenceError("contradictory child start observations")
                if previous and previous.get("name") and start.get("name") not in (None, previous["name"]):
                    raise EvidenceError("contradictory child start identity")
                data["starts"][actor] = {**(previous or {}), **start}

            def bind_started_children():
                # Prevention identity does not grant read or completion credit. Start may precede
                # the launch's return, so reconcile on either observation without timing guesses.
                for assignment in owned:
                    launch = assignment["launch"] or {}
                    if not (launch.get("provider") == providers.CODEX and launch.get("fresh") and launch.get("successful")):
                        continue
                    starts = [s for s in data["starts"].values() if s.get("root") == root
                              and s.get("name") == "/root/" + assignment["id"] and s.get("role") == assignment["role"]]
                    if len(starts) != 1:
                        if len(starts) > 1 and "ambiguous child start identity" not in assignment["faults"]:
                            assignment["faults"].append("ambiguous child start identity")
                        continue
                    start = starts[0]
                    if (assignment["child"] in (None, start["child"]) and
                            not any(other["child"] == start["child"] and other["id"] != assignment["id"] for other in owned)):
                        assignment["child"] = start["child"]
                        assignment["start"] = start
            bind_started_children()
            if kind == "launch":
                matches = [a for a in owned if call.get("name") == a["id"] or
                           (_text(call.get("prompt")) and a["packet_path"] in call["prompt"])]
                if not matches:
                    return hooks.proceed()  # unrelated native user tasks are not assignments
                if len(matches) != 1:
                    return hooks.block("Use one unique Engine packet per native assignment.")
                a = matches[0]
                if event == "PreToolUse":
                    if a["launch"] and a["launch"]["call_id"] == call.get("call_id") and a["launch"]["input_digest"] == core.digest(call["input"]):
                        return hooks.proceed()  # repeat observation of the same native call
                    if (a["launch"] and call["provider"] == providers.CODEX and
                            not a["launch"].get("successful") and not a["launch"].get("capacity_rejected") and
                            a["child"] is None and not a.get("failed_launches")):
                        rejected = providers.scoped_capacity_rejection_from_transcript(payload, a["launch"]["call_id"])
                        if rejected and core.digest(rejected["input"]) == a["launch"]["input_digest"]:
                            a["launch"].update(capacity_rejected=True, response=rejected["response"],
                                rejection_observation={"path": rejected["path"], "tail_digest": rejected["tail_digest"]})
                    retry = (a["launch"] and a["launch"].get("capacity_rejected") is True
                             and call.get("call_id") != a["launch"]["call_id"]
                             and not a.get("failed_launches") and a["child"] is None and a["read"] is None
                             and not any(s.get("name") == "/root/" + a["id"] for s in data["starts"].values()))
                    if actor or not call.get("fresh") or call.get("role") != a["role"] or (a["launch"] and not retry):
                        if a["launch"] and a["child"] is None and not a["launch"].get("successful"):
                            return hooks.block("The prior launch is unverified and no child was observed. Do not resend or clarify a nonexistent child; inspect assignment status, preserve the uncertain attempt, and report the missing coverage.")
                        return hooks.block("This Engine assignment needs a fresh, fork-free agent of its registered role; use clarification only for its existing assignment.")
                    if not _text(call.get("call_id")):
                        return hooks.block("The native launch has no correlatable tool identity; fresh execution is unverified.")
                    if retry:
                        if providers.scoped_control_digest(call["input"].get("message")) == a["launch"].get("control_digest"):
                            return hooks.block("Retry this assignment with a distinct initial launch message so the failed attempt cannot be mistaken for the retry.")
                        a.setdefault("failed_launches", []).append(copy.deepcopy(a["launch"]))
                    a["launch"] = {"call_id": call["call_id"], "provider": call["provider"],
                                   "role": call["role"], "fresh": True, "successful": False,
                                   "input_digest": core.digest(call["input"])}
                    if call["provider"] == providers.CODEX:
                        a["launch"]["control_digest"] = providers.scoped_control_digest(call["input"].get("message"))
                elif event == "PostToolUse" and a["launch"] and a["launch"]["call_id"] == call.get("call_id"):
                    response = payload.get("tool_response")
                    if (call["provider"] == providers.CODEX and providers.scoped_launch_capacity_rejected(payload)
                            and not a["launch"]["successful"]):
                        a["launch"]["capacity_rejected"] = True
                        a["launch"]["response"] = response
                    if response is not None and not payload.get("is_error"):
                        if a["launch"].get("capacity_rejected"):
                            a["faults"].append("contradictory launch outcome")
                        a["launch"]["response"] = response
                        a["launch"]["successful"] = True
                        a["launch"]["returned_child"] = providers.scoped_launch_child(response)
                        bind_started_children()
                elif event == "PostToolUse":
                    for failed in a.get("failed_launches", []):
                        if failed["call_id"] == call.get("call_id") and not providers.scoped_launch_capacity_rejected(payload):
                            a["faults"].append("contradictory failed launch observation")
                return hooks.proceed()

            if kind in ("queue", "continue"):
                target = call.get("target")
                if not _text(target):
                    return hooks.proceed()
                def prevention_children(assignment):
                    launch = assignment["launch"] or {}
                    if launch.get("provider") != providers.CODEX or not launch.get("fresh"):
                        return []
                    return [s["child"] for s in data["starts"].values() if s.get("root") == root
                            and s.get("name") == "/root/" + assignment["id"] and s.get("role") == assignment["role"]]
                matches = [a for a in owned if target in (a["id"], a["child"], "/root/" + a["id"],
                                                          *prevention_children(a))]
                # A child may report to its own controller; peer traffic is not review work.
                mine = [a for a in owned if actor and a["child"] == actor]
                if mine:
                    if target in (root, "/root"):
                        return hooks.proceed()
                    if event == "PreToolUse":
                        return hooks.block("Report to the owning controller; do not relay peer conclusions or start another assignment.")
                if len(matches) != 1:
                    return hooks.proceed() if not matches else hooks.block("Ambiguous Engine assignment recipient.")
                a = matches[0]
                if event == "PreToolUse":
                    if kind == "queue":
                        return hooks.block("Queue-only child messages can strand native capacity. Use the native wake-and-deliver continuation for necessary clarification within this assignment.")
                    if actor or a["accepted"] or not a["child"]:
                        return hooks.block("Clarification requires the owning controller and an open, observed assignment. New work needs a fresh agent.")
                    content = call.get("content")
                    if not _text(content) or not _text(call.get("call_id")):
                        return hooks.block("Clarification needs non-empty content and an observed tool identity.")
                    previous = next((c for c in a["continuations"] if c["call_id"] == call["call_id"]), None)
                    if previous is not None:
                        if previous["input_digest"] == core.digest(call["input"]):
                            return hooks.proceed()  # duplicate observation, not another native send
                        return hooks.block("Contradictory clarification observations; preserve evidence and reconcile.")
                    if call["provider"] == providers.CODEX and providers.scoped_control_digest(content) in (
                            [a["launch"].get("control_digest")] + [providers.scoped_control_digest(c["content"]) for c in a["continuations"]]):
                        return hooks.block("Use a distinct clarification message for this delivery, including its newly registered supplement path; identical prior messages cannot establish separate deliveries.")
                    if any(not c.get("delivered") for c in a["continuations"]):
                        return hooks.block("Prior clarification delivery is uncertain. Reconcile the child's actual delivery before retrying.")
                    staged = [s for s in a["supplements"] if not s["call_id"]]
                    if call["provider"] == providers.CLAUDE and len(staged) != 1:
                        return hooks.block("Prepare one private clarification supplement and ask this child to read it; delivery cannot be inferred from a successful send.")
                    if staged:
                        staged[0]["call_id"] = call["call_id"]
                    a["continuations"].append({"call_id": call["call_id"], "sender": root,
                        "recipient": a["child"], "content": content, "input_digest": core.digest(call["input"]),
                        "dispatched": False, "delivered": False})
                elif event == "PostToolUse":
                    for c in a["continuations"]:
                        if c["call_id"] == call.get("call_id"):
                            c["dispatched"] = payload.get("tool_response") is not None and not payload.get("is_error")
                return hooks.proceed()

            # Packet reads supply the non-timing identity join. Parent reads never satisfy it.
            if not _text(actor):
                return hooks.proceed()
            packet_read = event == "PostToolUse" and any(providers.scoped_reads_path(payload, a["packet_path"]) for a in owned)
            transcript = providers.scoped_transcript(payload, call["provider"]) if packet_read or event == "SubagentStop" else {}
            for a in owned:
                # A blocked Codex child can identify its assignment before it can read. This
                # permits access clarification only; verification still requires the full read.
                if (event == "SubagentStop" and a["child"] is None and a["launch"]
                        and a["launch"].get("fresh") and a["launch"].get("successful")
                        and a["launch"].get("provider") == providers.CODEX
                        and call["provider"] == providers.CODEX and call.get("role") == a["role"]
                        and (transcript.get("child"), transcript.get("root"), transcript.get("name"))
                        == (actor, root, "/root/" + a["id"])
                        and not any(b["child"] == actor and b["id"] != a["id"] for b in owned)):
                    a["child"] = actor
                    a["start"] = data["starts"].get(actor)
                if event == "PostToolUse" and providers.scoped_reads_path(payload, a["packet_path"]):
                    if not a["launch"] or not a["launch"]["fresh"]:
                        a["faults"].append("packet read without observed fresh dispatch")
                        continue
                    if a["child"] not in (None, actor) or any(b["child"] == actor and b["id"] != a["id"] for b in owned):
                        a["faults"].append("child reused across assignments")
                        continue
                    if call.get("role") != a["role"]:
                        a["faults"].append("packet read by wrong role")
                        continue
                    if call["provider"] == providers.CODEX and (
                            transcript.get("child") != actor or transcript.get("root") != root or
                            transcript.get("name") != "/root/" + a["id"]):
                        a["faults"].append("child transcript does not match the registered launch name and root")
                        continue
                    content = Path(a["packet_path"]).read_text(encoding="utf-8")
                    response = payload.get("tool_response")
                    if core.digest(content.encode()) != a["file_digest"]:
                        a["faults"].append("immutable packet digest changed")
                        continue
                    a["child"] = actor
                    a["start"] = data["starts"].get(actor)
                    if not providers.scoped_read_succeeded(payload, content):
                        failure = {"call_id": call.get("call_id"), "child": actor,
                                   "response_digest": core.digest(response)}
                        failures = a.setdefault("read_failures", [])
                        if failure not in failures:
                            failures.append(failure)
                        continue  # no read credit; a later exact successful read may repair access
                    a["read"] = {"call_id": call.get("call_id"), "child": actor,
                                 "file_digest": a["file_digest"], "response_digest": core.digest(response)}
                if a["child"] != actor:
                    continue
                if a["launch"]["provider"] != call["provider"]:
                    a["faults"].append("contradictory execution provider")
                    continue
                if event == "SubagentStart":
                    a["start"] = {"child": actor, "role": call.get("role")}
                for c in a["continuations"]:
                    for s in a["supplements"]:
                        if event == "PostToolUse" and s["call_id"] == c["call_id"] and providers.scoped_reads_path(payload, s["path"]):
                            body = Path(s["path"]).read_text(encoding="utf-8")
                            if core.digest(body.encode()) == s["digest"] and providers.scoped_read_succeeded(payload, body):
                                c["delivered"] = True
                                c["supplement_digest"] = s["digest"]
                    matches = providers.scoped_deliveries(transcript, c["content"], a["id"])
                    if matches == 1:
                        c["delivered"] = True
                    elif matches > 1:
                        a["faults"].append("duplicate clarification delivery")
                if event == "SubagentStop":
                    final = payload.get("last_assistant_message") or transcript.get("final")
                    stop = {"child": actor, "output": final, "digest": core.digest(final),
                            "continuations": len(a["continuations"]),
                            "delivered": all(c["delivered"] for c in a["continuations"])}
                    if call["provider"] == providers.CODEX:
                        stop["control_verified"] = providers.scoped_control_verified(
                            transcript, root=root, child=actor, name=a["id"],
                            launch_digest=a["launch"].get("control_digest"), continuations=a["continuations"])
                    if stop not in a["stops"]:
                        a["stops"].append(stop)
            return hooks.proceed()
        return self.change(update)

    def verified_locked(self, *, owner, root, lens, packet_digest, assignment_id=None):
        candidates = [a for a in self.read()["assignments"].values() if a["owner"] == owner and
                      a["root"] == root and a["lens"] == lens and a["packet_digest"] == packet_digest
                      and (assignment_id is None or a["id"] == assignment_id)]
        valid = []
        for a in candidates:
            launch = a["launch"] or {}
            if a["faults"] or not launch.get("fresh") or not launch.get("successful") or not a["read"] or not a["stops"] or not a["start"]:
                continue
            if launch.get("provider") not in (providers.CLAUDE, providers.CODEX):
                continue
            if launch.get("provider") == providers.CLAUDE and launch.get("returned_child") != a["child"]:
                continue
            if a["start"].get("child") != a["child"] or a["start"].get("role") != a["role"]:
                continue
            if any(not c["dispatched"] or not c["delivered"] for c in a["continuations"]):
                continue
            if any(not s["call_id"] or core.digest(Path(s["path"]).read_bytes()) != s["digest"] or
                   len([c for c in a["continuations"] if c["call_id"] == s["call_id"]
                        and c.get("supplement_digest") == s["digest"]]) != 1 for s in a["supplements"]):
                continue
            if launch.get("provider") == providers.CODEX and a["stops"][-1].get("control_verified") is not True:
                continue  # old stops are readable but never acquire unobserved traffic evidence
            if a["stops"][-1]["continuations"] != len(a["continuations"]):
                continue
            if not a["stops"][-1]["delivered"]:
                continue
            if a["purpose"] == "review":
                try:
                    output = json.loads(a["stops"][-1]["output"])
                except (TypeError, ValueError):
                    continue
                # Existing finding-array contract. A partial/blocked object or prose is not coverage.
                if not isinstance(output, list) or any(not isinstance(f, dict) for f in output):
                    continue
            elif not _text(a["stops"][-1]["output"]):
                continue
            else:
                try:
                    worker_output = json.loads(a["stops"][-1]["output"])
                except ValueError:
                    worker_output = None
                if isinstance(worker_output, dict) and worker_output.get("status") in (
                        "blocked", "partial", "needs_clarification", "cancelled", "failed", "error"):
                    continue
            if core.digest(Path(a["packet_path"]).read_bytes()) != a["file_digest"]:
                continue
            valid.append(a)
        if len(valid) != 1:
            raise EvidenceError(f"{lens}: fresh completed execution is unverified ({len(valid)} unambiguous candidates); preserve evidence and finish or replace the assignment")
        return valid[0]

    def review_report(self, assignment, owner):
        """Read the entire bound, observed report without changing acceptance metadata."""
        try:
            stop = assignment["stops"][-1]
            if stop["digest"] != core.digest(stop["output"]):
                result_contracts.reject("observed_digest", category="authority")
            return result_contracts.ingest(stop["output"], assignment.get("result_contract"),
                contract="plan-review-finding.v1" if owner["kind"] == "plan" else "pre-submission-review-finding.v1")
        except result_contracts.Rejection as exc:
            raise EvidenceError(str(exc)) from exc

    def accept_locked(self, *, owner, root, receipt, lenses, packet_digests, prior_receipt=None):
        """Called inside the existing plan/Build transaction, before publishing its receipt.

        Persisting this first can leave an orphan after a crash. An orphan is never coverage:
        history lookup also needs the matching published legacy receipt. Retrying is idempotent.
        """
        assignments = [self.verified_locked(owner=owner, root=root, lens=lens,
                                             packet_digest=packet_digests[lens]) for lens in lenses]
        if any(a["purpose"] != "review" for a in assignments):
            raise EvidenceError("a worker/scout completion cannot satisfy independent review")
        if prior_receipt is not None:
            if not self.receipt_verified(prior_receipt, owner):
                raise EvidenceError("the prior review's fresh execution is unverified; an amendment cannot upgrade it")
            data = self.read()
            prior = data["acceptances"][receipt_key(prior_receipt)]
            assignments.extend(data["assignments"][key] for key in prior["assignments"]
                               if data["assignments"][key]["lens"] not in lenses)
        expected_lenses = set(receipt.get("lenses", [receipt.get("lens")]))
        if {a["lens"] for a in assignments} != expected_lenses or len(assignments) != len(expected_lenses):
            raise EvidenceError("observed assignments do not match the exact claimed lens coverage")
        if len({a["child"] for a in assignments}) != len(assignments):
            raise EvidenceError("one child cannot satisfy several independent lenses")
        for a in assignments:
            self.review_report(a, owner)
        data = self.read()
        key = receipt_key(receipt)
        for a in assignments:
            data["assignments"][a["id"]]["accepted"] = True
        data["acceptances"][key] = {"owner": owner, "assignments": [a["id"] for a in assignments],
                                     "result_contracts": {a["id"]: a["result_contract"] for a in assignments},
                                     "outputs": {a["id"]: a["stops"][-1]["digest"] for a in assignments}}
        self.write_locked(data)

    def receipt_verified(self, receipt, owner):
        """History remains readable; a missing companion never upgrades it to fresh execution."""
        try:
            data = self.read()
            accepted = data["acceptances"].get(receipt_key(receipt))
            if not accepted:
                return False
            recorded = accepted["owner"]
            # Valid handoff may advance a generation. The original receipt remains historical;
            # only new acceptance requires the CURRENT generation under its owning transaction.
            if any(recorded.get(k) != v for k, v in owner.items() if k != "generation"):
                return False
            assigned = [data["assignments"][key] for key in accepted["assignments"]]
            if {a["lens"] for a in assigned} != set(receipt.get("lenses", [receipt.get("lens")])):
                return False
            if len({a["child"] for a in assigned}) != len(assigned):
                return False
            for assignment_id in accepted["assignments"]:
                a = data["assignments"][assignment_id]
                # Historical facts remain readable, but missing contract evidence is unverified.
                binding = a.get("result_contract")
                result_contracts.validate_binding(binding)
                if accepted.get("result_contracts", {}).get(assignment_id) != binding:
                    return False
                if a["owner"] != recorded or not a["accepted"] or a["faults"] or not a["stops"]:
                    return False
                verified = self.verified_locked(owner=recorded, root=a["root"], lens=a["lens"],
                                                packet_digest=a["packet_digest"], assignment_id=assignment_id)
                if verified["id"] != assignment_id:
                    return False
                if a["stops"][-1]["digest"] != core.digest(a["stops"][-1]["output"]):
                    return False
                if a["stops"][-1]["digest"] != accepted["outputs"][assignment_id]:
                    return False
                if core.digest(Path(a["packet_path"]).read_bytes()) != a["file_digest"]:
                    return False
                if any(core.digest(Path(s["path"]).read_bytes()) != s["digest"] for s in a["supplements"]):
                    return False
            return bool(accepted["assignments"])
        except (OSError, ValueError, KeyError, core.CoordinatorError):
            return False


def prepare_packets(library, slug, owner, root, packet, digest_by_lens, roles, *, expected_file_digest):
    if not _text(root):
        raise EvidenceError("fresh review dispatch requires the current root session identity; supply --session")
    store = Store(library, slug)
    return [store.register(owner=owner, root=root, purpose="review", lens=lens, role=roles[lens],
                           packet=packet, packet_digest=digest, expected_file_digest=expected_file_digest)
            for lens, digest in digest_by_lens.items()]


def accept_plan(library, slug, record, receipt, lenses, root, prior_receipt=None):
    Store(library, slug).accept_locked(owner=plan_owner(record), root=root, receipt=receipt,
        lenses=lenses, packet_digests={lens: receipt["packet_digest"] for lens in lenses},
        prior_receipt=prior_receipt)


def accept_build(library, state, receipt, root):
    slug = library.resolve(state["plan"]["plan_id"])
    Store(library, slug).accept_locked(owner=build_owner(state), root=root, receipt=receipt,
        lenses=[receipt["lens"]], packet_digests={receipt["lens"]: receipt["lens_packet_digest"]})


def missing_build_evidence(library, state, receipts):
    if not receipts:
        return []
    try:
        store = Store(library, library.resolve(state["plan"]["plan_id"]))
        return sorted({r["lens"] for r in receipts if not store.receipt_verified(r, build_owner(state))})
    except (OSError, ValueError, KeyError, core.CoordinatorError):
        return sorted({r["lens"] for r in receipts})


def handler(event, payload, library=None):
    library = library or plan_store.PlanLibrary()
    blocked = None
    failures = []
    for slug in library.slugs():
        store = Store(library, slug)
        if not store.path.exists():
            continue
        try:
            decision = store.observe(event, payload)
        except (OSError, ValueError, TypeError, KeyError, core.CoordinatorError) as exc:
            failures.append(exc)
            continue
        if decision.get("action") == "block" and blocked is None:
            blocked = decision
    if failures:
        # Use the existing failure sinks once, without losing a healthy plan's refusal.
        # Raw exception detail stays in the private diagnostic sink, not the public finding.
        try:
            hooks._record_crash_debug(event, failures[0])
        except Exception:  # recording a failed check must not disable a healthy guard
            pass
        hooks._emit_finding(sys.stderr, "hard", event, "crash",
            "Engine agent checks could not read one or more plan evidence files; those plans' "
            "execution and review freshness are unverified. Other plans were still checked.",
            hooks._promote_fail_open)
    return blocked or hooks.proceed()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    events = ("PreToolUse", "PostToolUse", "SubagentStart", "SubagentStop")
    if argv and argv[0] in events:
        event = argv[0]
        return hooks.run_hook(event, lambda payload: handler(event, payload),
            fail_open_notice="Engine agent checks did not run; execution and review freshness are unverified.")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "register", "finish", "clarify", "reconcile", "abandon"))
    parser.add_argument("--plan", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--assignment")
    parser.add_argument("--input", help="Private text file containing necessary same-assignment clarification")
    parser.add_argument("--reason")
    parser.add_argument("--purpose", choices=("worker", "scout"))
    parser.add_argument("--role")
    parser.add_argument("--packet")
    parser.add_argument("--transcript")
    args = parser.parse_args(argv)
    try:
        library = plan_store.PlanLibrary()
        store = Store(library, library.resolve(args.plan))
        if args.command == "register":
            if not args.purpose or not _text(args.role) or not args.packet:
                raise EvidenceError("register requires --purpose worker|scout, --role and --packet; review assignments come from the review packet commands")
            record = library.read_record(store.slug)
            current = (record.get("build_lease") or {}).get("current")
            if current:
                import build_state_store
                state = core.json_file(Path(current["snapshot"]))
                build_state_store._assert_snapshot_claim(record, current, state)
                owner = build_owner(state)
            else:
                # Read-only planning scouts need identity, not Build or plan approval.
                # Registration grants neither write authority nor independent review coverage.
                owner = plan_owner(record, record["current"])
            packet = Path(args.packet)
            file_digest = core.digest(_bounded_input(packet, "packet"))
            assignment = store.register(owner=owner, root=args.session, purpose=args.purpose,
                lens=None, role=args.role, packet=packet, packet_digest=file_digest, expected_file_digest=file_digest)
            print(json.dumps(assignment, indent=2))
        elif args.command == "finish":
            def finish(data):
                a = data["assignments"].get(args.assignment)
                if not a or a["root"] != args.session or a["purpose"] == "review":
                    raise EvidenceError("finish requires an owned worker/scout assignment; reviews finish through review record")
                store.verified_locked(owner=a["owner"], root=args.session, lens=a["lens"], packet_digest=a["packet_digest"], assignment_id=a["id"])
                a["accepted"] = True
            store.change(finish)
            print("Worker assignment finished; this grants no review coverage or Build integration credit.")
        elif args.command == "status":
            data = store.read()
            print(json.dumps([{k: a[k] for k in ("id", "lens", "role", "child", "packet_path", "accepted", "faults")}
                              | {"clarifications": len(a["continuations"]),
                                 "launch_outcome": ("not-launched" if not a["launch"] else
                                     "capacity-rejected" if a["launch"].get("capacity_rejected") else
                                     "successful" if a["launch"].get("successful") else "unverified"),
                                 "undelivered": sum(not c["delivered"] for c in a["continuations"])}
                              for a in data["assignments"].values() if a["root"] == args.session], indent=2))
        elif args.command == "reconcile":
            def reconcile(data):
                a = data["assignments"].get(args.assignment)
                if not a or a["root"] != args.session or not a["launch"] or not args.transcript:
                    raise EvidenceError("reconcile requires an owned observed assignment and --transcript")
                provider = a["launch"]["provider"]
                if provider != providers.CODEX:
                    raise EvidenceError("Claude delivery requires an observed successful supplement read; transcript delivery is unqualified")
                facts = providers.scoped_transcript({"agent_transcript_path": args.transcript}, provider)
                if (facts.get("child"), facts.get("root"), facts.get("name")) != (a["child"], a["root"], "/root/" + a["id"]):
                    raise EvidenceError("transcript identity does not match this assignment")
                for c in a["continuations"]:
                    count = providers.scoped_deliveries(facts, c["content"], a["id"])
                    if count == 1:
                        c["delivered"] = True
                    elif count > 1:
                        a["faults"].append("duplicate clarification delivery")
            store.change(reconcile)
            print("Reconciled observed delivery only. No dispatch success, completion or review coverage was inferred.")
        elif args.command == "clarify":
            if not args.assignment or not args.input:
                raise EvidenceError("clarify requires --assignment and a private --input text file")
            print(json.dumps(store.clarify(args.assignment, args.session, _bounded_input(args.input, "supplement").decode("utf-8")), indent=2))
        else:
            if not args.assignment or not _text(args.reason):
                raise EvidenceError("abandon requires --assignment and --reason")
            def abandon(data):
                a = data["assignments"].get(args.assignment)
                if not a or a["root"] != args.session or a["accepted"]:
                    raise EvidenceError("only the owning root may abandon an unaccepted assignment")
                a["faults"].append("abandoned: " + args.reason)
            store.change(abandon)
            print("Assignment abandoned for review credit. Native execution and pending delivery still need reconciliation.")
        return 0
    except (OSError, ValueError, core.CoordinatorError) as exc:
        print("scoped-agents: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
