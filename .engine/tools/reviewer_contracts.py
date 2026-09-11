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


def _models(fields, bindings):
    providers = ["claude", *sorted((bindings.get("providers") or {}).keys())]
    models = {}
    for provider in providers:
        try:
            resolved = agent_bindings.resolve_persona(fields, bindings, provider)
        except (KeyError, ValueError) as exc:
            raise ContractError(f"cannot resolve {provider} reviewer model: {exc}") from exc
        models[provider] = {"model": resolved["model"]}
    return models


def persona(path, root, bindings, *, historical=False):
    root, path = Path(root), Path(path)
    text = _source(path)
    fields = frontmatter(text)
    if fields.get("role") not in ROLES:
        raise ContractError("only review personas carry reviewer contracts")
    mandate = (_legacy_mandate(text) if historical and "reviewer-contract" not in fields
               else declaration(fields))
    models = _models(fields, bindings)
    semantic = {
        "mandate": mandate, "role": fields["role"], "lens": fields["lens"],
        "model_class": fields.get("model-tier"), "bindings": models,
        "effort_policy": {"mode": "harness-controlled", "floor": None},
        "effects": {"permissions": fields.get("permissions"),
                    "tools": _tool_set(fields.get("tools")),
                    "disallowed_tools": _tool_set(fields.get("disallowedTools")),
                    "permission_mode": fields.get("permissionMode")},
        "isolation": "fresh-native-assignment",
        "result_contract": (_legacy_result(root, fields) if historical and not
                            (root / ".engine/tools/result_contracts.py").exists() else
                            result_contracts.resolve(fields.get("output-contract"), role=fields["role"], root=root)),
    }
    return {"lens": fields["lens"], "semantic": semantic, "semantic_digest": core.digest(semantic),
            "source": {"path": path.relative_to(root).as_posix(), "name": fields["name"],
                       "digest": core.digest(text.encode()), "instructions": text,
                       **({"identity_mode": "legacy-source"} if historical and "reviewer-contract" not in fields else {})}}


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
            declared = (_legacy_mandate(source["instructions"]) if source.get("identity_mode") == "legacy-source"
                        and "reviewer-contract" not in fields else declaration(fields))
            if declared != semantic["mandate"] or fields.get("role") != role or fields.get("lens") != item["lens"]:
                raise ContractError("frozen source does not declare its reviewer identity")
            expected_effects = {"permissions": fields.get("permissions"), "tools": _tool_set(fields.get("tools")),
                                "disallowed_tools": _tool_set(fields.get("disallowedTools")),
                                "permission_mode": fields.get("permissionMode")}
            if semantic["model_class"] != fields.get("model-tier") or semantic["effects"] != expected_effects or semantic["bindings"] != _models(fields, source.get("binding_policy", envelope["binding_policy"])):
                raise ContractError("frozen structured mandate disagrees with its retained source or binding policy")
            bound = semantic["result_contract"]
            if fields.get("output-contract") != bound["id"]:
                raise ContractError("frozen source result contract mismatch")
            if bound["mode"] == "historical-receipt":
                if source.get("identity_mode") != "legacy-source" or bound["enforcement"] != "historical-unverified" or bound["limits"] != {}:
                    raise ContractError("historical result format cannot claim modern enforcement")
            else:
                try:
                    result_contracts.validate_retained_binding(bound)
                except result_contracts.Rejection as exc:
                    raise ContractError("frozen result schema or binding is invalid") from exc
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
    adopted = adoption(record)
    if adopted:
        if original is not None:
            raise ContractError("modern approval cannot carry a historical downgrade")
        original = adopted["contract"]
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
             "installation_digest": installation_digest(root),
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
            "review_contract_renewals": state.get("review_contract_renewals", []),
            "review_contract_adoptions": state.get("review_contract_adoptions", [])}


def effective_build(state):
    adopted = adoption(state)
    if adopted:
        import scoped_agents
        owner = scoped_agents.build_owner(state)
        if any(adopted["owner"].get(k) != v for k, v in owner.items() if k != "generation") or adopted["owner"].get("generation", 0) > owner["generation"]:
            raise ContractError("historical adoption belongs to another Build owner")
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
    # The Build renews its panel, not the sealed plan review. Retain the policy that
    # resolved the unchanged panel before replacing the current installation snapshot.
    # This is provenance only: it must not alter those semantic obligations.
    for item in proposed["panels"]["plan-review"]:
        item["source"].setdefault("binding_policy", copy.deepcopy(original["binding_policy"]))
    policy = agent_bindings.load_bindings(str(root))
    proposed["binding_policy"] = policy
    proposed["binding_policy_digest"] = core.digest(policy)
    proposed["digest"] = core.digest({k:v for k,v in proposed.items() if k != "digest"})
    if available != discover(root):
        raise ContractError("review installation changed during Build renewal capture")
    validate(proposed)
    return proposed


# Named, reviewed source transitions. An old timestamp or reviewed product commit is not evidence
# of an Engine capability. The verifier requires the actual Engine git objects and retained sources.
HISTORICAL_TRANSITIONS = {
    "native-collector": "3a79df419a2a6979015c7eadd64f4bfe67f218d0",
    "result-binding": "c4e56eb260904f9a133beaffa01aa96e2e2e212e",
}
MODEL_ONLY_TRANSITION = "4171a221152414edacebac04971e66e093989f86"


def _legacy_mandate(text):
    """A conservative source identity, never a retrospective declaration of prose equivalence."""
    return {"id": "legacy-source:s" + core.digest(text.encode()).split(":")[1], "version": 1}


def _legacy_result(root, fields):
    role = fields["role"]
    expected = {"plan-review": "plan-review-finding.v1",
                "pre-submission-review": "pre-submission-review-finding.v1"}[role]
    if fields.get("output-contract") != expected:
        raise ContractError("unsupported historical result format; restore or clone the original record")
    filename = "plan-record.v1.json" if role == "plan-review" else "build-state.v2.json"
    schema = result_contracts.local_schema(filename + "#/$defs/finding", Path(root) / ".engine/schemas")
    return {"id": expected, "mode": "historical-receipt", "enforcement": "historical-unverified",
            "limits": {}, "schema": schema, "schema_digest": result_contracts.digest(schema)}


def _git(root, *args):
    result = core.run(["git", *args], root=Path(root))
    if result.returncode:
        raise ContractError("historical source or git object is unavailable; restore the retained evidence")
    return result.stdout.strip()


def source_capabilities(root, commit):
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ContractError("historical source requires an exact Engine commit identity")
    _git(root, "cat-file", "-e", commit + "^{commit}")
    def contains(transition):
        _git(root, "cat-file", "-e", transition + "^{commit}")
        result = core.run(["git", "merge-base", "--is-ancestor", transition, commit], root=Path(root))
        if result.returncode not in (0, 1):
            raise ContractError("historical capability ancestry cannot be verified")
        return result.returncode == 0
    if not contains(MODEL_ONLY_TRANSITION):
        raise ContractError("unsupported historical effort-policy transition; restore or clone the plan")
    capabilities = {name: contains(sha) for name, sha in HISTORICAL_TRANSITIONS.items()}
    if capabilities["native-collector"] != capabilities["result-binding"]:
        raise ContractError("unsupported intermediate collector/result-binding record; restore or clone")
    # File presence corroborates the named transitions; a backport or partial removal is not this cohort.
    files = set(_git(root, "ls-tree", "-r", "--name-only", commit).splitlines())
    if (".engine/tools/scoped_agents.py" in files) != capabilities["native-collector"] or (
            ".engine/tools/result_contracts.py" in files) != capabilities["result-binding"]:
        raise ContractError("historical source capabilities do not match the supported transitions")
    if ".engine/tools/reviewer_contracts.py" in files:
        raise ContractError("modern envelope source cannot downgrade through historical adoption")
    return capabilities


def reconstruct_source(root, commit, referent, depth):
    """Read tracked source bytes only; never import or execute old code or trust today's installation."""
    import tempfile
    capabilities = source_capabilities(root, commit)
    paths = _git(root, "ls-tree", "-r", "--name-only", commit).splitlines()
    selected = [p for p in paths if (p.startswith(".claude/agents/") and p.endswith(".md")) or
                (p.startswith(".engine/schemas/") and p.endswith(".json")) or p in (
                    ".engine/policies/model-bindings.json", ".engine/build-protocol.json",
                    ".engine/tools/result_contracts.py")]
    total, sources = 0, {}
    with tempfile.TemporaryDirectory(prefix="engine-historical-source-") as directory:
        old_root = Path(directory)
        for path in selected:
            size = int(_git(root, "cat-file", "-s", commit + ":" + path))
            total += size
            if size > MAX_SOURCE_BYTES or total > MAX_ENVELOPE_BYTES:
                raise ContractError("historical source closure exceeds its size bound")
            # stdout is text and git show preserves the exact final newline.
            result = core.run(["git", "show", commit + ":" + path], root=Path(root))
            if result.returncode:
                raise ContractError("historical source disappeared during capture")
            target = old_root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(result.stdout, encoding="utf-8")
            sources[path] = core.digest(result.stdout.encode())
        bindings = agent_bindings.load_bindings(str(old_root))
        roster = []
        for path in sorted((old_root / ".claude/agents").glob("*.md")):
            if frontmatter(_source(path)).get("role") in ROLES:
                roster.append(persona(path, old_root, bindings, historical=True))
        if capabilities["result-binding"]:
            import ast
            try:
                parsed_result = ast.parse(_source(old_root / ".engine/tools/result_contracts.py"))
                limits = [ast.literal_eval(n.value) for n in parsed_result.body if isinstance(n, ast.Assign)
                          and any(isinstance(t, ast.Name) and t.id == "LIMITS" for t in n.targets)]
            except (SyntaxError, ValueError, TypeError, RecursionError) as exc:
                raise ContractError("unsupported historical result-limit declaration") from exc
            if len(limits) != 1 or not isinstance(limits[0], dict):
                raise ContractError("historical result limits are not recoverable from their original owner")
            for item in roster:
                item["semantic"]["result_contract"]["limits"] = copy.deepcopy(limits[0])
                item["semantic_digest"] = core.digest(item["semantic"])
        protocol = json.loads(_source(old_root / ".engine/build-protocol.json"))
        # This plan table is the old source's published plan authority, positively pinned below.
        plan_source = _git(root, "show", commit + ":.engine/tools/project_manager.py")
        import ast
        try:
            parsed = ast.parse(plan_source)
            tables = [ast.literal_eval(n.value) for n in parsed.body if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == "PLAN_REVIEW_LENSES" for t in n.targets)]
        except (SyntaxError, ValueError, TypeError, RecursionError) as exc:
            raise ContractError("unsupported historical plan roster declaration") from exc
        if len(tables) != 1 or not isinstance(tables[0], dict) or depth not in tables[0]:
            raise ContractError("unsupported historical plan roster authority")
        panels = {}
        for role in ROLES:
            allowed = None if depth == "thorough" else (
                tables[0] if role == "plan-review" else protocol["deliverable_review"])[depth]
            panels[role] = [p for p in roster if p["semantic"]["role"] == role and
                            (allowed is None or p["lens"] in allowed)]
        envelope = {"schema_version": VERSION, "referent": copy.deepcopy(referent), "depth": depth,
                    "panels": panels, "binding_policy": bindings, "binding_policy_digest": core.digest(bindings),
                    "instructions": "Historical source reconstruction adopted now; no approval-time envelope or new execution is claimed."}
        envelope["digest"] = envelope_digest(envelope)
        validate(envelope)
        return {"commit": commit, "capabilities": capabilities, "sources": sources, "contract": envelope}


def installation_digest(root):
    """Only changed obligations require another retention decision, not editorial bytes."""
    return core.digest([{"role": p["semantic"]["role"], "lens": p["lens"],
                         "semantic_digest": p["semantic_digest"]} for p in sorted(discover(root),
                             key=lambda p: (p["semantic"]["role"], p["lens"]))])


def _artifact(path):
    import scoped_agents
    text = scoped_agents._bounded_input(path, "historical retained artifact").decode("utf-8")
    return {"path": str(Path(path).resolve()), "digest": core.digest(text.encode()), "content": text}


def _plan_packet(text, record, receipt, source):
    # Old packet identity deliberately excluded its header. Check both the body hash and each
    # historical header authority without pretending the header was in the original digest.
    try:
        header, body = text.split("=" * 78 + "\n\n", 1)
    except ValueError as exc:
        raise ContractError("original historical plan packet is missing or malformed") from exc
    approval = record["approval"]
    required = [f"Plan review packet — {record['plan_id']} revision {receipt['revision']}",
                f"Plan digest: {receipt['plan_digest']}", f"Packet digest: {receipt['packet_digest']}",
                f"Depth: {approval['depth']} —"]
    if any(line not in header for line in required) or core.digest(body.encode()) != receipt["packet_digest"]:
        raise ContractError("historical plan packet referent or original digest mismatch")
    headers = header.splitlines()
    if any(sum(line.startswith(prefix) for line in headers) != 1 for prefix in ("Plan review packet", "Plan digest:", "Packet digest:", "Required lenses:", "Depth:")):
        raise ContractError("historical plan header contains conflicting authority fields")
    roster = next(line.removeprefix("Required lenses:").strip() for line in headers if line.startswith("Required lenses:"))
    names = [p["lens"] for p in source["contract"]["panels"]["plan-review"]]
    if sorted(roster.split(", ")) != sorted(names):
        raise ContractError("historical plan packet roster differs from its retained source")
    if source["capabilities"]["result-binding"] and names:
        bindings = [line.removeprefix("Result contract: ") for line in headers if line.startswith("Result contract: ")]
        if len(bindings) != 1 or json.loads(bindings[0]) != source["contract"]["panels"]["plan-review"][0]["semantic"]["result_contract"]:
            raise ContractError("historical plan result binding differs from its retained source")
    if source["commit"] not in body:
        raise ContractError("retained plan packet does not corroborate the original Engine source identity")


def _build_packet(packet, receipt, source, state):
    import build_coordinator_review as review
    original = {k: v for k, v in packet.items() if k not in ("packet_digest", "artifacts")}
    referent = {k: v for k, v in original.items() if k not in ("referent_digest", "reviewer_contracts")}
    if packet.get("plan_digest") != state["plan"]["digest"] or core.digest(packet.get("plan")) != state["plan"]["digest"] or receipt.get("referent_digest") != packet.get("referent_digest"):
        raise ContractError("historical packet names another approved Build payload or referent")
    if packet.get("standalone") or core.digest(original) != receipt["packet_digest"] or (
            packet.get("packet_digest") != receipt["packet_digest"]) or (
            core.digest(referent) != packet.get("referent_digest")):
        raise ContractError("historical Build packet digest or authority mismatch")
    if source["commit"] not in core.canonical(packet.get("plan", {})).decode():
        raise ContractError("retained Build packet does not corroborate its Engine source identity")
    if packet.get("protocol_digest") != source["protocol_digest"]:
        raise ContractError("historical packet uses another protocol configuration")
    matching = [p for p in packet["reviewer_contracts"] if p["lens"] == receipt["lens"]]
    if len(matching) != 1:
        raise ContractError("historical receipt has no original reviewer in its packet")
    declared = matching[0]
    old = next((p for p in source["contract"]["panels"]["pre-submission-review"]
                if p["lens"] == receipt["lens"]), None)
    if old is None or (declared.get("path"), declared.get("digest")) != (
            old["source"]["path"], old["source"]["digest"]):
        raise ContractError("historical reviewer source was substituted")
    descriptor = {k: v for k, v in declared.items() if k != "lens_packet_digest"}
    if review.lens_packet_digest(packet["referent_digest"], descriptor) != receipt.get("lens_packet_digest"):
        raise ContractError("historical lens packet digest mismatch")
    if declared.get("result_contract") and declared["result_contract"] != old["semantic"]["result_contract"]:
        raise ContractError("historical result binding was substituted")
    start = packet.get("repair_anchor") if packet["stage"] == "repair" else packet["base_commit"]
    if receipt.get("reviewed_range", {}).get("base") != start or receipt.get("reviewed_range", {}).get("tip") != packet["commit"] or receipt.get("commit") != packet["commit"]:
        raise ContractError("historical receipt read range differs from its original packet")


def adoption_preview(record, library, slug, locator, root):
    """A locator selects existing backup/packet artifacts. It cannot declare missing facts true.

    The packet must already correlate the source commit in its retained plan evidence. This deliberately
    supports a narrow existing-artifact provenance route; unknown formats use restore/clone instead.
    """
    import scoped_agents
    import build_coordinator_review as review
    import plan_contract
    if not isinstance(locator, dict) or set(locator) != {"source_root", "source_commit", "backup", "packets"}:
        raise ContractError("historical locator requires source_root, source_commit, backup and packets only")
    core.validate_part(locator, Path(__file__).resolve().parents[1] / "schemas/reviewer-contract.v1.json",
                       "#/$defs/adoption/properties/locator", "historical evidence locator")
    if not isinstance(locator["packets"], list) or len(locator["packets"]) > 100:
        raise ContractError("historical packet inventory is malformed or oversized")
    is_build = "ownership" in record
    plan = library.read_record(slug)
    if plan.get("closure") or record.get("closure"):
        raise ContractError("closed history remains unchanged; clone to start new work")
    if record.get("review_contract_format") or record.get("review_contract_adoptions") or (
            effective_build(record) if is_build else effective(record)):
        raise ContractError("record is already modern or adopted; use explicit renewal")
    if not plan.get("approval"):
        raise ContractError("unapproved plans must use ordinary approval, not historical adoption")
    problems = library.verify_chain(slug)
    if problems:
        raise ContractError("historical plan digest chain does not verify: " + "; ".join(problems))
    approval = plan["approval"]
    document = library.read_revision(slug, approval["revision"])
    if core.digest(document) != approval["plan_digest"]:
        raise ContractError("historical approval no longer names its original document")
    commit = locator["source_commit"]
    if commit not in core.canonical(document).decode():
        raise ContractError("original approved document does not retain the Engine source identity")
    referent = {"plan_id": plan["plan_id"], "revision": approval["revision"], "plan_digest": approval["plan_digest"]}
    source = reconstruct_source(locator["source_root"], commit, referent, approval["depth"])
    source["protocol_digest"] = core.digest(json.loads(_git(locator["source_root"], "show", commit + ":.engine/build-protocol.json")))
    backup = _artifact(locator["backup"])
    try:
        retained = json.loads(backup["content"])
    except ValueError as exc:
        raise ContractError("retained backup must be an original plan record or Build snapshot") from exc
    fields = ("plan", "approval", "ownership") if is_build else ("plan_id", "approval", "plan_review", "seal")
    if any(retained.get(k) != record.get(k) for k in fields):
        raise ContractError("retained backup differs from the original ownership, approval, seal or review")
    if retained.get("review_contract_format") or retained.get("review_contract_adoptions"):
        raise ContractError("a modern backup is not pre-envelope provenance")
    if is_build:
        if (record.get("approval") or {}).get("depth") != approval["depth"]:
            raise ContractError("historical Build depth differs from the retained approval; explicit renewal is required")
        if not plan.get("seal") or plan["seal"]["sealed_digest"] != record["plan"]["sealed_digest"] or (
                plan["seal"]["build_plan_digest"] != record["plan"]["digest"]) or (
                plan_contract.build_plan_digest(library.head(slug)) != record["plan"]["digest"]):
            raise ContractError("historical Build does not match the original sealed payload")
        owner = scoped_agents.build_owner(record)
        receipts = review.live_receipts(record)
        retained_receipts = review.live_receipts(retained)
        if receipts != retained_receipts:
            raise ContractError("missing original persisted Build receipts; a repair verdict cannot recreate them")
        required = {p["lens"] for p in source["contract"]["panels"]["pre-submission-review"]}
        original_delivery = {r["lens"] for stage, r in receipts if stage == "deliverable"}
        unreviewed = not receipts and not record["reviews"]["deliverable"].get("packet_digest") and not record["reviews"]["deliverable"].get("reviewed_commit") and not record.get("repair")
        if not unreviewed and not required <= original_delivery:
            raise ContractError("missing original deliverable receipt(s): " + ", ".join(sorted(required-original_delivery)))
        if review.missing_findings(record) or record["findings"] != retained["findings"]:
            raise ContractError("missing or changed original historical findings/dispositions")
        for _, receipt in receipts:
            for endpoint in (receipt.get("reviewed_range", {}).get("base"), receipt.get("reviewed_range", {}).get("tip")):
                if not isinstance(endpoint, str) or not re.fullmatch(r"[0-9a-f]{40}", endpoint):
                    raise ContractError("historical range has no exact git endpoint")
                _git(root, "cat-file", "-e", endpoint + "^{commit}")
    else:
        owner = scoped_agents.plan_owner(plan)
        receipts = [("plan", plan["plan_review"])] if plan.get("plan_review") else []
        if any(not f.get("disposition") for _, r in receipts for f in r["findings"]):
            raise ContractError("historical plan findings require their original dispositions")
    packets = [_artifact(p) for p in locator["packets"]]
    cohort = "observed-pre-envelope" if source["capabilities"]["native-collector"] else "historical-unverified"
    evidence = []
    companion = scoped_agents.Store(library, slug)
    if is_build and not receipts:
        if not unreviewed or cohort != "observed-pre-envelope":
            raise ContractError("a missing historical Build panel cannot be inferred as unreviewed")
        original_plan_review = plan.get("plan_review")
        if not original_plan_review or not companion.receipt_verified(original_plan_review,
                scoped_agents.plan_owner(plan, original_plan_review), retained_contract=source["contract"]):
            raise ContractError("unreviewed Build adoption requires intact observed original plan evidence")
        if any(a["owner"].get("kind") == "build" and a["owner"].get("build_id") == owner["build_id"]
               for a in companion.read()["acceptances"].values()):
            raise ContractError("original accepted Build receipts are missing; this is not an unreviewed Build")
        if not packets:
            raise ContractError("unreviewed Build adoption requires the retained original plan packet")
        if not any(f"Packet digest: {original_plan_review['packet_digest']}\n" in a["content"] for a in packets):
            raise ContractError("unreviewed Build adoption has no packet for its original plan review")
        for artifact in packets:
            if f"Packet digest: {original_plan_review['packet_digest']}\n" in artifact["content"]:
                _plan_packet(artifact["content"], plan, original_plan_review, source)
    for stage, receipt in receipts:
        expected = receipt["packet_digest"]
        candidates = []
        for artifact in packets:
            try:
                if is_build:
                    packet = json.loads(artifact["content"])
                    if packet.get("packet_digest") != expected:
                        continue
                    _build_packet(packet, receipt, source, record)
                else:
                    if f"Packet digest: {expected}\n" not in artifact["content"]:
                        continue
                    _plan_packet(artifact["content"], plan, receipt, source)
                candidates.append(artifact)
            except (ValueError, KeyError, TypeError) as exc:
                raise ContractError("retained historical packet is malformed") from exc
        if not candidates:
            raise ContractError("original packet not retained for receipt " + scoped_agents.receipt_key(receipt))
        if cohort == "observed-pre-envelope" and not companion.receipt_verified(receipt, owner, retained_contract=source["contract"]):
            raise ContractError("modern companion is missing or invalid; historical adoption cannot waive it")
        if cohort == "historical-unverified" and companion.path.exists():
            data = companion.read()
            if scoped_agents.receipt_key(receipt) in data["acceptances"] or any(
                    a.get("packet_digest") in (receipt["packet_digest"], receipt.get("lens_packet_digest"))
                    for a in data["assignments"].values()):
                raise ContractError("receipt has collector-era provenance; it cannot downgrade to pre-collector")
        lenses = receipt.get("lenses", [receipt.get("lens")])
        role = "pre-submission-review" if is_build else "plan-review"
        panel_by_lens = {p["lens"]: p for p in source["contract"]["panels"][role]}
        if not set(lenses) <= set(panel_by_lens):
            raise ContractError("historical receipt claims an unknown mandate")
        evidence.append({"receipt_key": scoped_agents.receipt_key(receipt), "receipt": copy.deepcopy(receipt),
            "stage": stage, "obligations": {lens: obligation_digest(referent, panel_by_lens[lens]) for lens in lenses},
            "packet_digest": candidates[0]["digest"]})
    gaps = ["approval-envelope"]
    if cohort == "historical-unverified" and receipts:
        gaps += ["native-collector", "result-binding", "raw-output-not-retained-by-pre-collector-format"]
    value = {"schema_version": "review-contract-adoption.v1", "owner": owner,
             "record_digest": core.digest(record), "plan_record_digest": core.digest(plan),
             "old_contract_digest": None, "contract": source["contract"], "source": source,
             "cohort": cohort, "gaps": gaps, "receipts": evidence,
             "artifacts": [backup, *packets], "locator": copy.deepcopy(locator),
             "installation_digest": installation_digest(root)}
    if len(core.canonical(value)) > MAX_ENVELOPE_BYTES:
        raise ContractError("historical evidence bundle exceeds its bounded size")
    value["preview_digest"] = core.digest(value)
    return value


def apply_adoption(record, preview, *, reason, at, operator_decided):
    if not operator_decided or not isinstance(reason, str) or not reason.strip():
        raise ContractError("historical adoption requires the operator's receipt-specific decision and reason")
    if preview.get("preview_digest") != core.digest({k: v for k, v in preview.items() if k != "preview_digest"}):
        raise ContractError("historical adoption preview was modified")
    if record.get("review_contract_adoptions"):
        if record["review_contract_adoptions"][0]["preview_digest"] == preview["preview_digest"]:
            return False
        raise ContractError("historical contract already adopted; use explicit renewal")
    if core.digest(record) != preview["record_digest"]:
        raise ContractError("historical adoption preview is stale")
    record["review_contract_adoptions"] = [{**copy.deepcopy(preview), "reason": reason, "at": at,
                                            "operator_decided": True}]
    return True


def adoption(record):
    """Validate the appended decision without touching any original record or companion."""
    entries = record.get("review_contract_adoptions", [])
    if len(core.canonical(entries)) > MAX_ENVELOPE_BYTES:
        raise ContractError("historical adoption exceeds its bounded size")
    if not entries:
        return None
    from jsonschema import Draft202012Validator
    schema = result_contracts.local_schema("reviewer-contract.v1.json#/$defs/adoption", Path(__file__).resolve().parents[1] / "schemas")
    if len(entries) != 1 or not Draft202012Validator(schema).is_valid(entries[0]):
        raise ContractError("historical adoption does not satisfy its closed decision schema")
    entry = entries[0]
    preview = {k: v for k, v in entry.items() if k not in ("at", "reason", "operator_decided", "preview_digest")}
    if entry["preview_digest"] != core.digest(preview):
        raise ContractError("historical adoption evidence digest mismatch")
    validate(entry["contract"])
    if entry["source"]["contract"] != entry["contract"] or entry["source"]["commit"] != entry["locator"]["source_commit"]:
        raise ContractError("historical reconstruction source identity mismatch")
    cohort = entry["cohort"]
    observed = cohort == "observed-pre-envelope"
    if entry["source"]["capabilities"] != {"native-collector": observed, "result-binding": observed}:
        raise ContractError("historical adoption names unsupported capability gaps")
    expected_gaps = ["approval-envelope"] + ([] if observed or not entry["receipts"] else [
        "native-collector", "result-binding", "raw-output-not-retained-by-pre-collector-format"])
    if entry["gaps"] != expected_gaps:
        raise ContractError("historical adoption attempts to waive an unsupported fact")
    for artifact in entry["artifacts"]:
        if core.digest(artifact["content"].encode()) != artifact["digest"]:
            raise ContractError("retained historical artifact digest mismatch")
    import scoped_agents
    role = "pre-submission-review" if entry["owner"]["kind"] == "build" else "plan-review"
    panel_by_lens = {p["lens"]: p for p in entry["contract"]["panels"][role]}
    for evidence in entry["receipts"]:
        receipt = evidence["receipt"]
        if evidence["receipt_key"] != scoped_agents.receipt_key(receipt):
            raise ContractError("original historical receipt was modified")
        expected = {lens: obligation_digest(entry["contract"]["referent"], panel_by_lens[lens])
                    for lens in receipt.get("lenses", [receipt.get("lens")]) if lens in panel_by_lens}
        if evidence["obligations"] != expected or not expected:
            raise ContractError("historical receipt semantic coverage mismatch")
    return entry


def adopted_obligation(record, receipt, lens):
    entry = adoption(record)
    if entry is None:
        return None
    import scoped_agents
    key = scoped_agents.receipt_key(receipt)
    return next((r["obligations"].get(lens) for r in entry["receipts"] if r["receipt_key"] == key), None)


def historical_execution(record, receipt, owner):
    """Separate historical continuity from Store.receipt_verified, which always stays strict."""
    entry = adoption(record)
    if not entry or entry["cohort"] != "historical-unverified":
        return False
    recorded = entry["owner"]
    if any(recorded.get(k) != v for k, v in owner.items() if k != "generation") or (
            owner.get("generation", 1) < recorded.get("generation", 1)):
        return False
    lenses = receipt.get("lenses", [receipt.get("lens")])
    return bool(lenses) and all(adopted_obligation(record, receipt, lens) for lens in lenses)


def historical_disclosure(record):
    entry = adoption(record)
    if entry is None:
        return None
    return (f"Review contract adopted after the original approval; {entry['cohort']}; "
            f"{len(entry['receipts'])} named original receipt(s). Accepted historical gaps: "
            + ", ".join(entry["gaps"]) + ". Original receipts and read ranges remain unchanged.")
