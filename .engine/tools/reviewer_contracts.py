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
            if fields.get("output-contract") != bound["id"]:
                raise ContractError("frozen source result contract mismatch")
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


def effective(record):
    """The first approval and every renewal remain intact; only this selector advances."""
    original = (record.get("approval") or {}).get("review_contract")
    if original is None:
        if record.get("review_contract_renewals"):
            raise ContractError("renewals have no original frozen approval")
        return None
    validate(original)
    current = original
    previous = None
    for entry in record.get("review_contract_renewals", []):
        if entry["old_digest"] != current["digest"] or entry["previous_decision"] != previous:
            raise ContractError("review contract renewal lineage is broken")
        if entry["owner"] != original["referent"] or entry["contract"]["referent"] != original["referent"]:
            raise ContractError("review contract renewal names another approved plan")
        validate(entry["contract"])
        if entry["delta"] != compare(current, entry["contract"]):
            raise ContractError("review renewal delta does not describe the changed obligation")
        if entry["action"] == "retain" and entry["contract"] != current:
            raise ContractError("retaining a review obligation cannot change it")
        current = entry["contract"]
        previous = entry["preview_digest"]
    return copy.deepcopy(current)


def compare(old, new):
    before = {(role, p["lens"]): p for role, ps in old["panels"].items() for p in ps}
    after = {(role, p["lens"]): p for role, ps in new["panels"].items() for p in ps}
    return [{"role": key[0], "lens": key[1],
             "old": before[key]["semantic_digest"] if key in before else None,
             "new": after[key]["semantic_digest"] if key in after else None}
            for key in sorted(set(before) | set(after))
            if before.get(key, {}).get("semantic_digest") != after.get(key, {}).get("semantic_digest")]


def renewal_preview(record, proposed, root, action):
    old = effective(record)
    if old is None:
        raise ContractError("historical approval has no frozen contract; use explicit historical adoption")
    if action not in ("retain", "adopt"):
        raise ContractError("renewal must explicitly retain or adopt an obligation")
    validate(proposed)
    if proposed["referent"] != old["referent"] or proposed["depth"] != old["depth"]:
        raise ContractError("contract renewal cannot change the approved plan or review depth")
    contract = old if action == "retain" else proposed
    history = record.get("review_contract_renewals", [])
    value = {"schema_version": "review-contract-renewal-preview.v1", "owner": old["referent"],
             "record_digest": core.digest(record), "old_digest": old["digest"], "contract": contract,
             "action": action, "delta": compare(old, contract),
             "installation_digest": core.digest(discover(root)),
             "observed_drift": drift(old, root),
             "previous_decision": history[-1]["preview_digest"] if history else None}
    value["preview_digest"] = core.digest(value)
    return value


def apply_renewal(record, preview, *, reason, at, operator_decided):
    """Called under the owning store lock after recomputing the preview against live inputs."""
    if not operator_decided or not isinstance(reason, str) or not reason.strip():
        raise ContractError("review contract renewal requires the operator's decision and a reason")
    if preview["preview_digest"] != core.digest({k: v for k, v in preview.items() if k != "preview_digest"}):
        raise ContractError("review contract renewal preview was modified")
    if any(x["preview_digest"] == preview["preview_digest"] for x in record.get("review_contract_renewals", [])):
        return False
    if core.digest(record) != preview["record_digest"]:
        raise ContractError("review contract renewal preview is stale; preview the current evidence")
    old = effective(record)
    if old is None or old["digest"] != preview["old_digest"]:
        raise ContractError("review contract renewal does not follow the current obligation")
    entry = {k: copy.deepcopy(preview[k]) for k in
             ("owner", "old_digest", "contract", "action", "delta", "installation_digest",
              "observed_drift", "previous_decision", "preview_digest")}
    entry.update(reason=reason, at=at, operator_decided=True)
    record.setdefault("review_contract_renewals", []).append(entry)
    effective(record)
    return True


def build_record(state):
    """Shared decision lineage viewed through the Build's independently owned original snapshot."""
    return {"approval": {"review_contract": state.get("review_contract")},
            "review_contract_renewals": state.get("review_contract_renewals", [])}


def effective_build(state):
    if state.get("review_contract_format") == 1 and not state.get("review_contract"):
        raise ContractError("Build approval contract is missing; modern evidence cannot downgrade to legacy")
    if state.get("review_contract_renewals"):
        from jsonschema import Draft202012Validator
        schema = result_contracts.local_schema("plan-record.v1.json#/properties/review_contract_renewals",
            Path(__file__).resolve().parents[1] / "schemas")
        if not Draft202012Validator(schema).is_valid(state["review_contract_renewals"]):
            raise ContractError("Build renewal lineage does not satisfy the canonical plan renewal schema")
        for decision in state["review_contract_renewals"]:
            owner = state.get("review_contract_build_decisions", {}).get(decision["preview_digest"])
            if not isinstance(owner, dict) or set(owner) != {"kind", "plan", "build_id", "generation", "digest"} or owner.get("kind") != "build" or type(owner.get("generation")) is not int or not 1 <= owner["generation"] <= state.get("ownership", {}).get("generation", 0) or owner.get("build_id") != state.get("ownership", {}).get("build_id") or owner.get("plan") != state.get("plan", {}).get("plan_id"):
                raise ContractError("Build review renewal has no matching durable owner decision")
    return effective(build_record(state))


def build_panel(state):
    contract = effective_build(state)
    if contract is None:
        return None
    return [{"lens": p["lens"], "path": p["source"]["path"], "digest": p["source"]["digest"],
             "semantic_digest": p["semantic_digest"],
             "obligation_digest": obligation_digest(contract["referent"], p),
             "result_contract": p["semantic"]["result_contract"]}
            for p in contract["panels"]["pre-submission-review"]]


def propose_build(state, root, lenses):
    original = effective_build(state)
    if original is None:
        raise ContractError("historical Build requires evidence-scoped adoption before contract renewal")
    available = discover(root)
    proposed = copy.deepcopy(original)
    selected = [p for p in available if p["semantic"]["role"] == "pre-submission-review" and p["lens"] in lenses]
    if {p["lens"] for p in selected} != set(lenses):
        raise ContractError("a proposed Build reviewer is unavailable")
    proposed["panels"]["pre-submission-review"] = selected
    policy = agent_bindings.load_bindings(str(root))
    proposed["binding_policy"] = policy
    proposed["binding_policy_digest"] = core.digest(policy)
    proposed["digest"] = core.digest({k:v for k,v in proposed.items() if k != "digest"})
    if available != discover(root):
        raise ContractError("review installation changed during Build renewal capture")
    validate(proposed)
    return proposed
