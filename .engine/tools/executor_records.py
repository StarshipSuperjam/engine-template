#!/usr/bin/env python3
"""Executor-qualification records — load, validate, and refuse malformed records loudly.

This module owns the SHAPE of the .engine/executors/ surface. It reads a committed
executor-qualification.v1 record, validates it against the governing schema, and RAISES on anything
malformed rather than letting a half-formed record flow into an eligibility decision. It is the single
in-code reader of these records; executor_eligibility consumes what this module returns and never parses a
record itself.

These records qualify EXTERNAL executor artifacts by observed behavior. They are NOT the runtime-environment
store at .engine/state/execution.json (execution-state.v1), which records whether the Engine's own
senior-session runtime is qualified against the current release, is a frozen operator-judgment snapshot, and
is written only by execution_environment.record_qualification. Different subject, different writer, different
lifecycle — the two stores must never be conflated.
"""
from __future__ import annotations

import json
import os

SCHEMA_VERSION = "executor-qualification.v1"
_HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.normpath(os.path.join(_HERE, "..", "schemas", "executor-qualification.v1.json"))
EXECUTORS_DIR = os.path.normpath(os.path.join(_HERE, "..", "executors"))


class ExecutorRecordError(ValueError):
    """A record is unreadable, not JSON, or fails the schema. Always raised — never swallowed into a default."""


def _load_schema() -> dict:
    with open(SCHEMA_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def validate_record(record: dict) -> None:
    """Validate one record against executor-qualification.v1; raise ExecutorRecordError on any violation.

    This is the same schema the hard check engine/check/executor-record runs over every committed
    .engine/executors/*.json, so an in-code caller and the merge gate refuse the same malformed record."""
    from jsonschema import Draft202012Validator  # lazy: tool-runtime dep
    if not isinstance(record, dict):
        raise ExecutorRecordError("an executor record must be a JSON object")
    schema = _load_schema()
    errors = sorted(Draft202012Validator(schema).iter_errors(record), key=lambda e: list(e.path))
    if errors:
        joined = "; ".join(
            f"{'/'.join(str(p) for p in e.path) or '(root)'}: {e.message}" for e in errors)
        raise ExecutorRecordError(f"executor record does not satisfy {SCHEMA_VERSION}: {joined}")


def load_record(path: str) -> dict:
    """Read and validate one record file. Raises on missing, non-JSON, or schema-invalid input."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError as exc:
        raise ExecutorRecordError(f"no executor record at {path}") from exc
    except json.JSONDecodeError as exc:
        raise ExecutorRecordError(f"executor record at {path} is not valid JSON: {exc}") from exc
    validate_record(data)
    return data


def load_records(directory: str | None = None) -> list:
    """Load and validate every record under the executors surface (top-level ``*.json`` only; ``.gitkeep`` and
    any subdirectory are ignored). Sorted by filename for a stable, reviewable order. Refuses LOUDLY — one
    malformed record raises rather than being silently skipped, so an eligibility query never runs over a
    partially-read surface."""
    directory = directory or EXECUTORS_DIR
    records = []
    if not os.path.isdir(directory):
        return records
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        records.append(load_record(os.path.join(directory, name)))
    return records


def qualification_records(directory: str | None = None) -> list:
    """Only the ``record_kind == 'qualification'`` records; fail-closed witnesses are excluded."""
    return [r for r in load_records(directory) if r.get("record_kind") == "qualification"]
