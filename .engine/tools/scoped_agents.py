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
import sys
import uuid
from pathlib import Path

import build_coordinator_core as core
import hooks
import moment
import plan_store
import providers

VERSION = "scoped-agent-evidence.v1"
FILENAME = "scoped-agent-evidence.v1.json"


class EvidenceError(core.CoordinatorError):
    pass


def _text(value):
    return isinstance(value, str) and bool(value.strip())


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
        if value.get("schema_version") != VERSION or not isinstance(value.get("assignments"), dict):
            raise EvidenceError("unsupported or damaged scoped-assignment companion; review is unverified")
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

    def register(self, *, owner, root, purpose, lens, role, packet, packet_digest):
        """Freeze a uniquely located packet before native dispatch. Provider is not caller-selected."""
        if not all(_text(x) for x in (root, purpose, role, packet_digest)) or not isinstance(owner, dict):
            raise EvidenceError("assignment requires explicit owner, root, purpose, role and packet identity")
        source = Path(packet).resolve()
        content = source.read_bytes()
        token = "sa_" + uuid.uuid4().hex
        directory = self.path.parent / "scoped-packets"
        self.library._mkdir(directory)
        location = directory / (token + source.suffix)
        core.atomic_write(location, content.decode("utf-8"), durable=True, mode=0o600)
        assignment = {"id": token, "owner": copy.deepcopy(owner), "root": root,
                      "purpose": purpose, "lens": lens, "role": role,
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
                previous = data["starts"].get(actor)
                if previous is not None and previous != start:
                    raise EvidenceError("contradictory child start observations")
                data["starts"][actor] = start
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
                    if actor or not call.get("fresh") or call.get("role") != a["role"] or a["launch"]:
                        return hooks.block("This Engine assignment needs a fresh, fork-free agent of its registered role; use clarification only for its existing assignment.")
                    if not _text(call.get("call_id")):
                        return hooks.block("The native launch has no correlatable tool identity; fresh execution is unverified.")
                    a["launch"] = {"call_id": call["call_id"], "provider": call["provider"],
                                   "role": call["role"], "fresh": True, "successful": False,
                                   "input_digest": core.digest(call["input"])}
                elif event == "PostToolUse" and a["launch"] and a["launch"]["call_id"] == call.get("call_id"):
                    response = payload.get("tool_response")
                    if response is not None and not payload.get("is_error"):
                        a["launch"]["response"] = response
                        a["launch"]["successful"] = True
                        a["launch"]["returned_child"] = providers.scoped_launch_child(response)
                return hooks.proceed()

            if kind in ("queue", "continue"):
                target = call.get("target")
                if not _text(target):
                    return hooks.proceed()
                matches = [a for a in owned if target in (a["id"], a["child"], "/root/" + a["id"])]
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
            packet_read = event == "PostToolUse" and any(a["packet_path"] in json.dumps(call["input"]) for a in owned)
            transcript = providers.scoped_transcript(payload, call["provider"]) if packet_read or event == "SubagentStop" else {}
            for a in owned:
                if event == "PostToolUse" and a["packet_path"] in json.dumps(call["input"]):
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
                    if core.digest(content.encode()) != a["file_digest"] or not providers.scoped_read_succeeded(payload, content):
                        a["faults"].append("packet digest or successful read response is missing")
                        continue
                    a["child"] = actor
                    a["start"] = data["starts"].get(actor)
                    a["read"] = {"call_id": call.get("call_id"), "child": actor,
                                 "file_digest": a["file_digest"], "response_digest": core.digest(response)}
                if a["child"] != actor:
                    continue
                if event == "SubagentStart":
                    a["start"] = {"child": actor, "role": call.get("role")}
                for c in a["continuations"]:
                    for s in a["supplements"]:
                        if event == "PostToolUse" and s["call_id"] == c["call_id"] and s["path"] in json.dumps(call["input"]):
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
                    if stop not in a["stops"]:
                        a["stops"].append(stop)
            return hooks.proceed()
        return self.change(update)

    def verified_locked(self, *, owner, root, lens, packet_digest):
        candidates = [a for a in self.read()["assignments"].values() if a["owner"] == owner and
                      a["root"] == root and a["lens"] == lens and a["packet_digest"] == packet_digest]
        valid = []
        for a in candidates:
            launch = a["launch"] or {}
            if a["faults"] or not launch.get("fresh") or not launch.get("successful") or not a["read"] or not a["stops"] or not a["start"]:
                continue
            if launch.get("provider") == providers.CLAUDE and launch.get("returned_child") != a["child"]:
                continue
            if a["start"].get("child") != a["child"] or a["start"].get("role") != a["role"]:
                continue
            if any(not c["dispatched"] or not c["delivered"] for c in a["continuations"]):
                continue
            if any(not s["call_id"] or core.digest(Path(s["path"]).read_bytes()) != s["digest"] for s in a["supplements"]):
                continue
            if a["stops"][-1]["continuations"] != len(a["continuations"]):
                continue
            if not a["stops"][-1]["delivered"]:
                continue
            try:
                output = json.loads(a["stops"][-1]["output"])
            except (TypeError, ValueError):
                continue
            # Existing finding-array contract. A partial/blocked object or prose is not coverage.
            if not isinstance(output, list) or any(not isinstance(f, dict) for f in output):
                continue
            if core.digest(Path(a["packet_path"]).read_bytes()) != a["file_digest"]:
                continue
            valid.append(a)
        if len(valid) != 1:
            raise EvidenceError(f"{lens}: fresh completed execution is unverified ({len(valid)} unambiguous candidates); preserve evidence and finish or replace the assignment")
        return valid[0]


def handler(event, payload, library=None):
    library = library or plan_store.PlanLibrary()
    for slug in library.slugs():
        store = Store(library, slug)
        if not store.path.exists():
            continue
        decision = store.observe(event, payload)
        if decision.get("action") == "block":
            return decision
    return hooks.proceed()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("event", choices=["PreToolUse", "PostToolUse", "SubagentStart", "SubagentStop"])
    args = parser.parse_args(argv)
    return hooks.run_hook(args.event, lambda payload: handler(args.event, payload),
                          fail_open_notice="Engine agent checks did not run; execution and review freshness are unverified.")


if __name__ == "__main__":
    sys.exit(main())
