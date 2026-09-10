"""Canonical, bounded persona-result protocols; no state or provider invocation lives here.

The registry distinguishes human prose, validated audit input, and durable review/worker
acceptance. Consumers supply trusted identity and own persistence. All model input crosses
``ingest`` before a compiler sees it; schema validity never establishes evidence truth.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
LIMITS = {"bytes": 1048576, "depth": 64, "values": 10000,
          "array_items": 1000, "string_bytes": 65536, "detail_chars": 256}

# Schema fragments are owned by their durable consumer, not duplicated here.
CONTRACTS = {
    "plan-review-finding.v1": {
        "mode": "structured", "roles": ["plan-review"],
        "schema": "plan-review-finding.v1.json", "array": True,
        "handler": "project_manager.ingest_review_report", "compiler": "compile_review",
        "enforcement": "canonical-ingress"},
    "pre-submission-review-finding.v1": {
        "mode": "structured", "roles": ["pre-submission-review"],
        "schema": "pre-submission-review-finding.v1.json", "array": True,
        "handler": "build_coordinator_review.ingest_review_report", "compiler": "compile_review",
        "enforcement": "canonical-ingress"},
    "worker-result.v1": {
        "mode": "structured", "roles": ["worker"],
        "schema": "build-state.v2.json#/$defs/worker_report",
        "handler": "build_coordinator_work.ingest_worker_report", "compiler": "compile_worker",
        "enforcement": "canonical-ingress"},
    "conformance-verdicts.v1": {
        "mode": "structured", "roles": ["audit"],
        "schema": "conformance-verdicts.v1.json",
        "handler": "conformance_sweep.validate_block", "compiler": "compile_audit",
        "enforcement": "validation-only"},
    "audit-finding.v1": {"mode": "prose", "roles": ["audit"],
                         "enforcement": "prose-only", "structured_parts": ["conformance-verdicts.v1"]},
    "grounding-brief.v1": {"mode": "prose", "roles": ["scout"], "enforcement": "prose-only"},
    "validation-digest.v1": {"mode": "prose", "roles": ["scout"], "enforcement": "prose-only"},
}


class Rejection(ValueError):
    """Safe protocol failure; never interpolate raw payloads or exception messages."""
    def __init__(self, category, rule, *, contract=None, path="", detail="Result rejected"):
        self.envelope = {"schema_version": "result-rejection.v1", "category": category,
                         "rule": rule, "path": path[:256], "contract": contract,
                         "detail": detail[:LIMITS["detail_chars"]]}
        super().__init__(json.dumps(self.envelope, ensure_ascii=True, sort_keys=True))


def reject(rule, *, category="schema", contract=None, path="", detail="Result does not satisfy its contract"):
    raise Rejection(category, rule, contract=contract, path=path, detail=detail)


def digest(value):
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(data).hexdigest()


def read_input(path, *, stream=None):
    """Bound bytes BEFORE decoding or allocation; stdin must be a binary stream."""
    try:
        if path == "-":
            raw = (stream if stream is not None else sys.stdin.buffer).read(LIMITS["bytes"] + 1)
        else:
            with Path(path).open("rb") as handle:
                raw = handle.read(LIMITS["bytes"] + 1)
    except (OSError, ValueError):
        reject("input_unavailable", category="syntax", detail="Could not read result input")
    return raw


def _pointer(parts):
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def _scan(text, contract):
    # This bounds parser recursion before json.loads. JSON syntax remains the parser's job.
    depth = values = 0
    quoted = escaped = token = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
            values += 1  # keys count too: a conservative bound on allocated JSON nodes
            token = False
        elif char in "[{":
            depth += 1
            values += 1
            token = False
            if depth > LIMITS["depth"]:
                reject("maxDepth", contract=contract)
        elif char in "]}":
            depth -= 1
            token = False
        elif char in ",: \t\r\n":
            token = False
        elif not token:
            values += 1
            token = True
        if values > LIMITS["values"]:
            reject("maxValues", contract=contract)


def _limits(value, contract, parts=()):
    if isinstance(value, str):
        try:
            size = len(value.encode("utf-8"))
        except UnicodeError:
            reject("unicode", category="syntax", contract=contract, path=_pointer(parts))
        if size > LIMITS["string_bytes"]:
            reject("maxStringBytes", contract=contract, path=_pointer(parts))
    elif isinstance(value, list):
        if len(value) > LIMITS["array_items"]:
            reject("maxItems", contract=contract, path=_pointer(parts))
        for i, child in enumerate(value):
            _limits(child, contract, (*parts, i))
    elif isinstance(value, dict):
        for key, child in value.items():
            _limits(key, contract, parts)
            _limits(child, contract, (*parts, key))


def parse(raw, *, contract=None):
    if not isinstance(raw, (bytes, str)):
        reject("raw_input_required", category="syntax", contract=contract)
    if len(raw) > LIMITS["bytes"]:
        reject("maxBytes", contract=contract)
    try:
        data = raw.encode("utf-8") if isinstance(raw, str) else raw
        if len(data) > LIMITS["bytes"]:
            reject("maxBytes", contract=contract)
        text = data.decode("utf-8")
    except UnicodeError:
        reject("utf8", category="syntax", contract=contract)
    _scan(text, contract)

    def pairs(entries):
        result = {}
        for key, value in entries:
            if key in result:
                reject("duplicate_key", category="syntax", contract=contract)
            result[key] = value
        return result

    def constant(_):
        reject("finite_number", category="syntax", contract=contract)

    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except Rejection:
        raise
    except (ValueError, RecursionError, OverflowError):
        reject("json", category="syntax", contract=contract, detail="Result is not valid JSON")
    # JSON exponents can overflow without invoking parse_constant.
    def finite(node):
        import math
        if isinstance(node, float) and not math.isfinite(node):
            constant(node)
        elif isinstance(node, (dict, list)):
            for child in (node.values() if isinstance(node, dict) else node):
                finite(child)
    finite(value)
    _limits(value, contract)
    return value


def _schema(reference, root):
    """Expand only bounded local schema references; recursive result schemas are refused."""
    directory = (Path(root) / ".engine/schemas").resolve()
    budget = [0, 0]

    def load(ref, source=None, chain=()):
        if not isinstance(ref, str):
            reject("invalid_schema_reference", category="authority")
        budget[0] += 1
        if budget[0] > 256 or len(chain) > LIMITS["depth"]:
            reject("schema_reference_limit", category="authority")
        filename, _, fragment = ref.partition("#")
        path = (source.parent / filename if source and filename else
                source if source else directory / filename).resolve()
        if not path.is_relative_to(directory) or ":" in filename:
            reject("nonlocal_schema", category="authority")
        key = (str(path), fragment)
        if key in chain:
            reject("recursive_schema", category="authority")
        try:
            with path.open("rb") as handle:
                document = parse(handle.read(LIMITS["bytes"] + 1))
            value = document
            if fragment:
                if not fragment.startswith("/"):
                    raise ValueError()
                for part in fragment[1:].split("/"):
                    value = value[part.replace("~1", "/").replace("~0", "~")]
        except (OSError, KeyError, TypeError, ValueError):
            reject("schema_unavailable", category="authority")
        return expand(value, path, (*chain, key))

    def expand(value, source, chain):
        budget[1] += 1
        if budget[1] > LIMITS["values"]:
            reject("schema_expansion_limit", category="authority")
        if isinstance(value, list):
            return [expand(v, source, chain) for v in value]
        if not isinstance(value, dict):
            return value
        if "$dynamicRef" in value or "$recursiveRef" in value:
            reject("dynamic_schema", category="authority")
        # Unused definitions are not part of the selected canonical schema closure.
        result = {k: expand(v, source, chain) for k, v in value.items()
                  if k not in ("$ref", "$defs", "definitions", "$id")}
        if "$ref" in value:
            target = load(value["$ref"], source, chain)
            result = {"allOf": [target, result]} if result else target
        return result

    return load(reference)


def resolve(contract, *, role=None, root=ROOT):
    if not isinstance(contract, str) or contract not in CONTRACTS:
        reject("unknown_contract", category="authority")
    entry = CONTRACTS[contract]
    if role is not None and role not in entry["roles"]:
        reject("producer_role", category="authority", contract=contract)
    result = {"id": contract, "mode": entry["mode"], "enforcement": entry["enforcement"],
              "limits": dict(LIMITS), "schema": None, "schema_digest": None}
    if entry["mode"] == "structured":
        schema = _schema(entry["schema"], root)
        if entry.get("array"):
            schema = {"type": "array", "maxItems": LIMITS["array_items"], "items": schema}
        try:
            Draft202012Validator.check_schema(schema)
        except Exception:
            reject("invalid_schema", category="authority", contract=contract)
        result.update(schema=schema, schema_digest=digest(schema))
    return result


def validate_binding(binding, *, contract=None, role=None, root=ROOT):
    if not isinstance(binding, dict):
        reject("missing_binding", category="authority", contract=contract)
    expected = resolve(contract or binding.get("id"), role=role, root=root)
    if binding != expected:
        reject("changed_binding", category="authority", contract=expected["id"])
    return expected


def ingest(raw, binding, *, contract=None, role=None, root=ROOT):
    bound = validate_binding(binding, contract=contract, role=role, root=root)
    if bound["mode"] != "structured":
        reject("prose_is_not_evidence", category="authority", contract=bound["id"])
    value = parse(raw, contract=bound["id"])
    error = next(Draft202012Validator(bound["schema"]).iter_errors(value), None)
    if error is not None:
        reject(str(error.validator), contract=bound["id"], path=_pointer(error.absolute_path))
    return value


def observed_report(raw, observed, binding, *, contract=None, role=None, root=ROOT):
    """The immutable observed output, never a caller's replacement, is compiled."""
    original = ingest(observed, binding, contract=contract, role=role, root=root)
    supplied = ingest(raw, binding, contract=contract, role=role, root=root)
    if digest(supplied) != digest(original):
        reject("observed_report_mismatch", category="authority", contract=binding["id"])
    return original


def compile_review(report, *, lens):
    prefix = "".join(p[0] for p in lens.replace("_", "-").split("-") if p).upper() or "F"
    findings = []
    for i, item in enumerate(report, 1):
        loc = item["location"]
        where = "the plan as a whole" if loc is None else loc["file"]
        if loc is not None and loc.get("line") is not None:
            where += ":" + str(loc["line"])
        findings.append({"id": f"{prefix}-{i}", "lens": lens, "severity": item["severity"],
                         "summary": item["message"], "location": where})
    return {"findings": findings, "report": copy.deepcopy(report)}


def compile_worker(report):
    # Identity and integration are the caller's trusted transaction, never the compiler's guesses.
    return copy.deepcopy(report)


def compile_audit(report):
    # All verdicts survive validation; divergence-only projection is explicit consumer policy.
    return copy.deepcopy(report)
