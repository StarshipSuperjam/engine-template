"""Approval-owned review obligations, independent of mutable file provenance.

A declared mandate revision is a reviewed semantic claim, not an NLP proof. Exact sources are
retained for disclosure; structured constraints are always compared even without a version bump.
This module reads bounded local sources and returns values. Plan/Build owners persist decisions.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import yaml

import agent_bindings
import build_coordinator_core as core
import result_contracts

VERSION = "reviewer-contract.v1"
ROLES = ("plan-review", "pre-submission-review")
MAX_SOURCE_BYTES = 1024 * 1024
MAX_ENVELOPE_BYTES = 8 * 1024 * 1024
IDENTITY = re.compile(r"^[a-z][a-z0-9.-]*:[a-z][a-z0-9.-]*$")


class ContractError(core.CoordinatorError):
    pass


def _source(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_SOURCE_BYTES + 1)
    if len(raw) > MAX_SOURCE_BYTES:
        raise ContractError("review source exceeds the bounded snapshot size")
    return raw.decode("utf-8")


def frontmatter(text):
    if not text.startswith("---\n"):
        raise ContractError("reviewer source has no frontmatter")
    try:
        fields = yaml.safe_load(text.split("---\n", 2)[1])
    except (IndexError, yaml.YAMLError) as exc:
        raise ContractError("reviewer source has malformed frontmatter") from exc
    if not isinstance(fields, dict):
        raise ContractError("reviewer source frontmatter must be an object")
    return fields


def declaration(fields):
    identity, version = fields.get("reviewer-contract"), fields.get("reviewer-contract-version")
    if not isinstance(identity, str) or not IDENTITY.fullmatch(identity):
        raise ContractError("reviewer requires a namespaced reviewer-contract identity")
    if type(version) is not int or version < 1:
        raise ContractError("reviewer requires a positive reviewer-contract-version")
    if fields.get("role") not in ROLES or not fields.get("lens"):
        raise ContractError("reviewer identity requires a review role and lens")
    return {"id": identity, "version": version}


def _tool_set(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = [x.strip() for x in value.split(",") if x.strip()]
    if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
        raise ContractError("reviewer tool restrictions are malformed")
    return sorted(set(value))


def persona(path, root, bindings):
    root, path = Path(root), Path(path)
    text = _source(path)
    fields = frontmatter(text)
    if fields.get("role") not in ROLES:
        raise ContractError("only review personas carry reviewer contracts")
    mandate = declaration(fields)
    providers = ["claude", *sorted((bindings.get("providers") or {}).keys())]
    models = {}
    for provider in providers:
        try:
            resolved = agent_bindings.resolve_persona(fields, bindings, provider)
        except (KeyError, ValueError) as exc:
            raise ContractError(f"cannot resolve {provider} reviewer model: {exc}") from exc
        models[provider] = {"model": resolved["model"]}
    semantic = {
        "mandate": mandate, "role": fields["role"], "lens": fields["lens"],
        "model_class": fields.get("model-tier"), "bindings": models,
        "effort_policy": {"mode": "harness-controlled", "floor": None},
        "effects": {"permissions": fields.get("permissions"),
                    "tools": _tool_set(fields.get("tools")),
                    "disallowed_tools": _tool_set(fields.get("disallowedTools")),
                    "permission_mode": fields.get("permissionMode")},
        "isolation": "fresh-native-assignment",
        "result_contract": result_contracts.resolve(fields.get("output-contract"),
                                                      role=fields["role"], root=root),
    }
    return {"lens": fields["lens"], "semantic": semantic, "semantic_digest": core.digest(semantic),
            "source": {"path": path.relative_to(root).as_posix(), "name": fields["name"],
                       "digest": core.digest(text.encode()), "instructions": text}}


def discover(root, role=None):
    root = Path(root)
    bindings = agent_bindings.load_bindings(str(root))
    found, identities, lenses = [], set(), set()
    for path in sorted((root / ".claude/agents").glob("*.md")):
        text = _source(path)
        if not text.startswith("---\n"):
            continue
        fields = frontmatter(text)
        if fields.get("role") not in ROLES or (role and fields["role"] != role):
            continue
        item = persona(path, root, bindings)
        key = (fields["role"], fields["lens"])
        identity = item["semantic"]["mandate"]["id"]
        if key in lenses or identity in identities:
            raise ContractError("duplicate reviewer lens or mandate identity")
        lenses.add(key); identities.add(identity); found.append(item)
    return found


def capture(root, referent, depth, plan_lenses, deliverable_lenses, *, instructions):
    """Snapshot both obligations. Re-read all source inputs before returning to the owner lock."""
    root = Path(root)
    bindings_text = _source(root / ".engine/policies/model-bindings.json")
    roster = discover(root)
    panels = {}
    for role, names in (("plan-review", plan_lenses), ("pre-submission-review", deliverable_lenses)):
        selected = {p["lens"]: p for p in roster if p["semantic"]["role"] == role}
        missing = set(names) - set(selected)
        if missing:
            raise ContractError("required reviewers are unavailable: " + ", ".join(sorted(missing)))
        panels[role] = [copy.deepcopy(selected[n]) for n in sorted(set(names))]
    result = {"schema_version": VERSION, "referent": copy.deepcopy(referent), "depth": depth,
              "panels": panels, "binding_policy_digest": core.digest(json.loads(bindings_text)),
              "binding_policy": json.loads(bindings_text), "instructions": instructions}
    result["digest"] = envelope_digest(result)
    validate(result)
    if _source(root / ".engine/policies/model-bindings.json") != bindings_text or discover(root) != roster:
        raise ContractError("review installation changed during approval capture; retry approval")
    return result


def envelope_digest(envelope):
    return core.digest({k: v for k, v in envelope.items() if k != "digest"})


def validate(envelope):
    if not isinstance(envelope, dict) or len(core.canonical(envelope)) > MAX_ENVELOPE_BYTES:
        raise ContractError("invalid or oversized reviewer contract envelope")
    schema = Path(__file__).resolve().parents[1] / "schemas/reviewer-contract.v1.json"
    core.validate(envelope, schema)
    if envelope["digest"] != envelope_digest(envelope):
        raise ContractError("review contract envelope digest mismatch")
    if core.digest(envelope["binding_policy"]) != envelope["binding_policy_digest"]:
        raise ContractError("binding policy digest mismatch")
    seen = set()
    for role, panel in envelope["panels"].items():
        lenses = set()
        for item in panel:
            semantic, source = item["semantic"], item["source"]
            if semantic["role"] != role or item["lens"] != semantic["lens"] or item["lens"] in lenses:
                raise ContractError("review contract role/lens mismatch")
            lenses.add(item["lens"])
            identity = semantic["mandate"]["id"]
            if identity in seen:
                raise ContractError("duplicate frozen reviewer identity")
            seen.add(identity)
            if core.digest(semantic) != item["semantic_digest"]:
                raise ContractError("reviewer semantic digest mismatch")
            if core.digest(source["instructions"].encode()) != source["digest"]:
                raise ContractError("frozen reviewer source digest mismatch")
            fields = frontmatter(source["instructions"])
            if declaration(fields) != semantic["mandate"] or fields.get("role") != role or fields.get("lens") != item["lens"]:
                raise ContractError("frozen source does not declare its reviewer identity")
            bound = semantic["result_contract"]
            if bound.get("schema_digest") != result_contracts.digest(bound.get("schema")):
                raise ContractError("frozen result schema digest mismatch")
    return envelope


def panel(envelope, role):
    validate(envelope)
    return copy.deepcopy(envelope["panels"][role])


def drift(envelope, root):
    """Editorial and policy provenance never silently select a new obligation."""
    validate(envelope)
    current = {(p["semantic"]["role"], p["lens"]): p for p in discover(root)}
    changed, editorial = [], []
    for role, items in envelope["panels"].items():
        for old in items:
            new = current.get((role, old["lens"]))
            entry = {"role": role, "lens": old["lens"], "old": old["semantic_digest"],
                     "new": new["semantic_digest"] if new else None}
            if not new or new["semantic_digest"] != old["semantic_digest"]:
                changed.append(entry)
            elif old["source"] != new["source"]:
                editorial.append({**entry, "old_source": old["source"]["digest"],
                                  "new_source": new["source"]["digest"]})
    return {"changed": changed, "editorial": editorial}


def obligation_digest(referent, item):
    return core.digest({"referent": referent, "reviewer": item["semantic"]})
