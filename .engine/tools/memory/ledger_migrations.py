#!/usr/bin/env python3
"""Read-only accounting and safe routing for the v2 primary-evidence cutover.

Every legacy source position receives exactly one ``retain``, ``transform``,
``drop``, or ``unresolved`` decision. The audit never writes a ledger, index,
sidecar, archive, cursor, or stamp; one unresolved item blocks later mutation.
"""
from __future__ import annotations

from collections import Counter, deque
import hashlib
import json
import re

from memory import records

_LEGACY_DROP_KINDS = {
    records.EPISODIC_KIND: "obsolete-curated-summary",
    records.GIST_KIND: "obsolete-curated-summary",
    records.MARKER_KIND: "obsolete-curation-bookkeeping",
    records.REINFORCEMENT_KIND: "obsolete-curation-bookkeeping",
    records.ROLLUP_KIND: "obsolete-curation-bookkeeping",
    records.SUPERSEDED_KIND: "obsolete-curation-bookkeeping",
}
_LEGACY_CONTROL_KINDS = frozenset((records.WITHHOLD_KIND, records.RESTORE_KIND, records.ERASURE_KIND))
_TASK_HEAD = "<task-notification>"
_TASK_CLOSE = "</task-notification>"
_COMPACTION_HEAD = "This session is being continued from a previous conversation"
_TASK_RESULT = re.compile(r"<result>(.*?)</result>", re.DOTALL)
_TASK_STATUS = re.compile(r"<status>([^<]+)</status>")
_TASK_ID = re.compile(r"<task-id>([^<]+)</task-id>")


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _result_id(source_index: int, record: dict) -> str:
    """Use a declared legacy id when present; otherwise a content-free position id."""
    rid = record.get(records.RECORD_ID_KEY)
    return rid if isinstance(rid, str) and rid else f"legacy-{source_index}"


def _normalized_result(record: dict, source_index: int, event: str, text: str, *,
                       source_name=None, item_id=None) -> dict:
    """Project proven legacy fields into the closed v2 envelope; never infer provider."""
    rule = records.SOURCE_EVENT_TABLE[event]
    result = {
        "v": records.PRIMARY_EVIDENCE_VERSION,
        "kind": records.PRIMARY_EVIDENCE_KIND,
        "id": _result_id(source_index, record),
        "event": event,
        "provider": records.provider_or_unknown(record.get("provider")),
        "authority": rule["authority"],
        "source_type": rule["source_type"],
        "source_name": source_name,
        "session_id": record.get("session_id") if isinstance(record.get("session_id"), str) else None,
        "sequence": record.get("seq") if (isinstance(record.get("seq"), int)
                                             and not isinstance(record.get("seq"), bool)
                                             and record["seq"] >= 0) else None,
        "item_id": item_id or _result_id(source_index, record),
        "role": rule["role"],
        "terminal": rule["terminal"],
        "text": text,
    }
    valid, reason = records.validate_primary_evidence(result)
    if not valid:
        raise ValueError(reason)
    return result


def _base(record, source_index: int) -> dict:
    return {"source_index": source_index, "source_digest": _digest(record)}


def _decision(record, source_index: int, disposition: str, reason: str, *, event=None,
              result=None, result_id=None) -> dict:
    item = _base(record, source_index) | {"disposition": disposition, "reason": reason}
    if event is not None:
        item["event"] = event
    if result is not None:
        item["result"] = result
        item["result_id"] = result["id"]
        item["result_digest"] = _digest(result)
    elif result_id is not None:
        item["result_id"] = result_id
    return item


def _task_groups(source_records) -> dict:
    """Map task-notification source indexes to their complete deterministic group.

    Old capture chunked long notifications. A group begins only at the anchored
    wrapper and continues only through the same message sequence in the same
    session until the closing wrapper. Legacy chunking stamped every chunk of one
    message with that shared sequence. Content never starts a group by inference.
    """
    groups, active = {}, {}
    for index, record in enumerate(source_records):
        if not isinstance(record, dict) or record.get("kind") != records.AMBIENT_CAPTURE_KIND:
            continue
        text = record.get("text")
        if not isinstance(text, str):
            continue
        session = record.get("session_id")
        seq = record.get("seq")
        key = session if isinstance(session, str) else None
        if text.strip().startswith(_TASK_HEAD):
            group = {"indexes": [index], "parts": [text], "last_seq": seq, "closed": _TASK_CLOSE in text}
            groups[index] = group
            if key is not None and not group["closed"]:
                active[key] = group
            continue
        group = active.get(key) if key is not None else None
        if group is None or not isinstance(seq, int) or not isinstance(group["last_seq"], int) \
                or seq != group["last_seq"]:
            if key is not None:
                active.pop(key, None)
            continue
        group["indexes"].append(index)
        group["parts"].append(text)
        group["last_seq"] = seq
        groups[index] = group
        if _TASK_CLOSE in text:
            group["closed"] = True
            active.pop(key, None)
    return groups


def _compaction_indexes(source_records) -> set:
    """Return every source index in an anchored, same-message continuation group."""
    indexes, active = set(), {}
    for index, record in enumerate(source_records):
        if not isinstance(record, dict) or record.get("kind") != records.AMBIENT_CAPTURE_KIND:
            continue
        text = record.get("text")
        if not isinstance(text, str):
            continue
        session, seq = record.get("session_id"), record.get("seq")
        key = session if isinstance(session, str) else None
        if text.strip().startswith(_COMPACTION_HEAD):
            indexes.add(index)
            if key is not None:
                active[key] = seq
            continue
        if (key is not None and active.get(key) == seq
                and records.INJECTED_TAG in (record.get("tags") or [])):
            indexes.add(index)
            continue
        if key is not None:
            active.pop(key, None)
    return indexes


def _classify_task_group(source_records, group: dict) -> dict:
    """Return the shared terminal decision for one legacy task wrapper group."""
    text = "\n".join(group["parts"])
    if not group["closed"]:
        return {"disposition": "unresolved", "reason": "unterminated-task-notification"}
    result_match = _TASK_RESULT.search(text)
    if result_match is None or not result_match.group(1).strip():
        return {"disposition": "drop", "reason": "wrapper-notification"}
    status_match = _TASK_STATUS.search(text)
    status = status_match.group(1).strip() if status_match else None
    if status == "completed":
        event = "agent-result-success-text"
    elif status in ("failed", "killed", "stopped"):
        event = "agent-result-failure-text"
    else:
        return {"disposition": "unresolved", "reason": "unknown-task-terminal-status"}
    task_match = _TASK_ID.search(text)
    task_id = task_match.group(1).strip() if task_match else None
    head = source_records[group["indexes"][0]]
    try:
        result = _normalized_result(head, group["indexes"][0], event, result_match.group(1).strip(),
                                    source_name="background-agent", item_id=task_id)
    except ValueError as exc:
        return {"disposition": "unresolved", "reason": f"invalid-result:{exc}"}
    return {"disposition": "transform", "reason": "extract-terminal-agent-result",
            "event": event, "result": result}


def classify_legacy_record(record, source_index: int, *, task_group=None, task_decision=None,
                           compaction=False) -> dict:
    """Classify one legacy source position without content/provider inference."""
    if not isinstance(record, dict):
        return _decision(record, source_index, "unresolved", "record-not-object")
    kind = record.get("kind")
    if kind in _LEGACY_DROP_KINDS:
        return _decision(record, source_index, "drop", _LEGACY_DROP_KINDS[kind])
    if kind in _LEGACY_CONTROL_KINDS:
        return _decision(record, source_index, "transform", "carry-evidence-control-state")
    if kind == records.PIN_KIND:
        text = record.get("text")
        if not isinstance(text, str) or not text:
            return _decision(record, source_index, "unresolved", "pin-text-missing")
        try:
            result = _normalized_result(record, source_index, "operator-pin", text,
                                        source_name=record.get(records.PIN_VIA_KEY))
        except ValueError as exc:
            return _decision(record, source_index, "unresolved", f"invalid-result:{exc}")
        return _decision(record, source_index, "retain", "operator-pin", event="operator-pin", result=result)
    if kind != records.AMBIENT_CAPTURE_KIND:
        return _decision(record, source_index, "unresolved", "unknown-legacy-kind")
    if compaction:
        return _decision(record, source_index, "drop", "compaction-continuation",
                         event="compaction-continuation")
    if task_group is not None:
        first = task_group["indexes"][0]
        if task_decision["disposition"] == "unresolved":
            return _decision(record, source_index, "unresolved", task_decision["reason"])
        if task_decision["disposition"] == "drop":
            return _decision(record, source_index, "drop", task_decision["reason"],
                             event="wrapper-notification")
        result = task_decision["result"] if source_index == first else None
        return _decision(record, source_index, "transform", task_decision["reason"],
                         event=task_decision["event"], result=result,
                         result_id=task_decision["result"]["id"])
    text = record.get("text")
    if not isinstance(text, str) or not text:
        return _decision(record, source_index, "unresolved", "primary-text-missing")
    if records.INJECTED_TAG in (record.get("tags") or []):
        # A tagged record not linked to an anchored task or continuation group
        # has no provable lineage, so the transaction refuses.
        return _decision(record, source_index, "unresolved", "unlinked-injected-fragment")
    speaker = record.get("speaker", record.get("role"))
    event = {"user": "conversation-user", "assistant": "conversation-assistant",
             "observation": "legacy-primary-observation"}.get(speaker)
    if event is None:
        return _decision(record, source_index, "unresolved", "unknown-primary-role")
    try:
        result = _normalized_result(record, source_index, event, text)
    except ValueError as exc:
        return _decision(record, source_index, "unresolved", f"invalid-result:{exc}")
    disposition = "retain" if speaker in ("user", "assistant") else "transform"
    reason = "exact-conversation-evidence" if disposition == "retain" else "mark-legacy-observation"
    return _decision(record, source_index, disposition, reason, event=event, result=result)


def classify_legacy_records(source_records) -> dict:
    """Return closed, deterministic source-to-result accounting for a dry run."""
    groups = _task_groups(source_records)
    compactions = _compaction_indexes(source_records)
    decisions = {}
    for group in {id(group): group for group in groups.values()}.values():
        decisions[id(group)] = _classify_task_group(source_records, group)
    items = [classify_legacy_record(record, index, task_group=groups.get(index),
                                    task_decision=decisions.get(id(groups[index])) if index in groups else None,
                                    compaction=index in compactions)
             for index, record in enumerate(source_records)]
    counts = Counter(item["disposition"] for item in items)
    unresolved = [item["source_index"] for item in items if item["disposition"] == "unresolved"]
    # Hash inspectable dispositions, without duplicating source payload in the digest input.
    result_digest = _digest([{key: value for key, value in item.items() if key != "result"}
                             for item in items])
    return {
        "source_count": len(items),
        "retained_count": counts["retain"],
        "transformed_count": counts["transform"],
        "dropped_count": counts["drop"],
        "unresolved_count": counts["unresolved"],
        "unresolved_source_indexes": unresolved,
        "mutation_blocked": bool(unresolved),
        "result_digest": result_digest,
        "items": items,
    }


def dry_run_legacy_ledger(*, path: str | None = None) -> dict:
    """Inspect a ledger read-only and return source/result digests and accounting."""
    from memory import ledger  # lazy to keep the records vocabulary leaf-only
    read = ledger.read(path=path)
    report = classify_legacy_records(read.records)
    report.update({
        "source_digest": ledger.file_digest(path),
        "source_records_digest": ledger.records_digest(read.records),
        "malformed": read.malformed,
        "torn_trailing": read.torn_trailing,
        "mutation_blocked": report["mutation_blocked"] or bool(read.malformed) or read.torn_trailing,
    })
    return report


# The backup format remains v1 until a later node changes every producer and owns
# a proven byte-for-byte transformation. B01 intentionally exposes no write path.
_REGISTRY: dict = {}


def resolve_ledger_migration(from_version, to_version):
    """Return a registered byte migration chain, or None when no safe path exists."""
    if not (isinstance(from_version, int) and not isinstance(from_version, bool)):
        return None
    try:
        seen = {from_version}
        queue = deque([(from_version, [])])
        while queue:
            current, chain = queue.popleft()
            for (src, dst), transform in _REGISTRY.items():
                if src == current and dst not in seen:
                    next_chain = chain + [transform]
                    if dst == to_version:
                        return next_chain
                    seen.add(dst)
                    queue.append((dst, next_chain))
    except Exception:  # noqa: BLE001
        return None
    return None


def apply_ledger_migrations(ledger_bytes: bytes, chain) -> bytes:
    """Apply a registered byte chain all-or-nothing; callers own any write."""
    out = ledger_bytes
    for transform in chain:
        out = transform(out)
        if not isinstance(out, (bytes, bytearray)):
            raise TypeError("a ledger migration must return bytes")
    return bytes(out)
