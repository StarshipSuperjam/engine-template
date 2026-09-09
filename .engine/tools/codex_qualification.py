#!/usr/bin/env python3
"""Check a live Codex qualification record's completeness, never its conclusions.

The format belongs to operations/codex-validation.md. This tool reads one bounded
JSON record; it neither runs probes nor changes execution-environment authority.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re

import moment

PARENT_MODES = ("read-only", "workspace-write")
CAPABILITIES = (
    "custom-agent-model", "reasoning-effort", "parent-child-sandbox",
    "shell-availability", "file-writes", "hook-session-identity",
    "compact-context", "agent-observation", "spawn-payload",
)
EXPECTED_CELLS = frozenset((mode, capability) for mode in PARENT_MODES for capability in CAPABILITIES)
MAX_RECORD_BYTES = 1_048_576


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _texts(value):
    return isinstance(value, list) and bool(value) and all(_text(v) for v in value)


def validate_record(record):
    """Return mechanical defects; an empty list does not mean qualification passed."""
    errors = []
    if not isinstance(record, dict):
        return ["record must be an object"]
    if record.get("schema_version") != "codex-qualification.v1":
        errors.append("schema_version must be codex-qualification.v1")
    if moment.parse_z(record.get("recorded_at")) is None:
        errors.append("recorded_at must be a timezone-qualified ISO timestamp")
    for field in ("base_commit", "head_commit"):
        if not isinstance(record.get(field), str) or not re.fullmatch(r"[0-9a-f]{40}", record[field]):
            errors.append(f"{field} must be a full git commit id")
    host = record.get("host")
    if not isinstance(host, dict):
        host = {}
    for field in ("kind", "os", "cli_version", "host_version"):
        value = host.get(field)
        if not isinstance(value, dict) or not (
            _text(value.get("value")) or
            (value.get("value") is None and _text(value.get("unavailable_reason")))
        ):
            errors.append(f"host.{field} needs a value or an explicit unavailable_reason")
    if not _texts(record.get("official_sources")):
        errors.append("official_sources must contain source references")
    commands = record.get("reproduction_commands")
    if not isinstance(commands, list) or not commands or not all(_texts(cmd) for cmd in commands):
        errors.append("reproduction_commands must contain nonempty argv arrays")
    cells = record.get("cells")
    if not isinstance(cells, list):
        return errors + ["cells must be an array"]
    seen = Counter()
    for index, cell in enumerate(cells):
        label = f"cells[{index}]"
        if not isinstance(cell, dict):
            errors.append(f"{label} must be an object")
            continue
        mode, capability = cell.get("parent_mode"), cell.get("capability")
        if not isinstance(mode, str) or not isinstance(capability, str) or (mode, capability) not in EXPECTED_CELLS:
            errors.append(f"{label} has an unknown parent_mode/capability")
        else:
            seen[(mode, capability)] += 1
        status = cell.get("status")
        if status not in ("pass", "fail", "not-verified"):
            errors.append(f"{label} has an invalid status")
        if not isinstance(cell.get("requested"), dict) or not cell["requested"]:
            errors.append(f"{label} needs nonempty requested settings")
        if status in ("pass", "fail"):
            if not isinstance(cell.get("observed"), dict) or not cell["observed"]:
                errors.append(f"{label} needs nonempty observed results")
            if not _texts(cell.get("evidence_refs")):
                errors.append(f"{label} needs evidence_refs for observed results")
        elif status == "not-verified" and not _text(cell.get("reason")):
            errors.append(f"{label} needs a reason for not-verified")
    for key in sorted(EXPECTED_CELLS):
        if seen[key] != 1:
            errors.append(f"{key[0]}/{key[1]} must occur exactly once (found {seen[key]})")
    return errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record", type=Path)
    args = parser.parse_args(argv)
    try:
        with args.record.open("rb") as stream:
            raw = stream.read(MAX_RECORD_BYTES + 1)
        if len(raw) > MAX_RECORD_BYTES:
            raise ValueError("record exceeds 1 MiB")
        record = json.loads(raw)
        errors = validate_record(record)
    except (OSError, ValueError) as exc:
        errors = [str(exc)]
    print(json.dumps({"complete": not errors, "errors": errors,
                      "meaning": "Completeness only; live capability and evidence quality require review."}))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
