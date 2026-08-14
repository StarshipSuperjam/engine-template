"""Settled-specification and hard-check evidence services for Build."""
from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Callable

import build_coordinator_core as core


def criterion(path: str, index: int, value: dict) -> dict:
    identity = f"{path}#{index + 1}"
    payload = {"path": path, "index": index + 1, **value}
    return {"id": identity, "digest": core.digest(payload), "text": value["criterion"],
            "how_verified": value["how_verified"], "who": value["who"]}


def canonical_spec(
    root: Path,
    plan: dict,
    *,
    repository: str | None = None,
    check_issue: bool = True,
    issue_body: Callable[[str, int], str] | None = None,
) -> dict:
    """Re-read selected canonical documents and prove their row denominator.

    Semantic document selection remains engineering judgment. Mechanics prove
    complete, current mappings within every selected document and independently
    preserve any settled authority named by the originating Issue.
    """
    sys.path.insert(0, str(root / ".engine" / "tools"))
    import spec_referent

    spec = plan["spec"]
    issue_result = None
    intent = plan["intent_source"]
    if intent["kind"] == "issue" and check_issue:
        if not repository or issue_body is None:
            raise core.CoordinatorError("repository and Issue reader are required to resolve originating authority")
        try:
            issue_result = spec_referent.resolve_from_body(str(root), issue_body(repository, intent["issue"]))
        except spec_referent.SpecReferentError as exc:
            raise core.CoordinatorError(f"could not resolve the originating Issue's settled specification: {exc}") from exc

    authority_failures = {"ambiguous-pointer", "doc-missing", "doc-not-locked", "no-criteria"}
    if issue_result and not issue_result.get("ok") and (
        spec["posture"] == "settled" or issue_result.get("no_op_reason") in authority_failures
    ):
        raise core.CoordinatorError("the originating Issue's specification authority is unusable: " + issue_result["detail"])

    if spec["posture"] == "none":
        if issue_result and issue_result.get("ok"):
            raise core.CoordinatorError(
                f"the originating Issue resolves settled specification {issue_result['doc_path']}; the plan cannot declare no spec"
            )
        return {"posture": "none", "selection_basis": spec["selection_basis"],
                "disclosure": spec["disclosure"], "documents": [], "digest": None,
                "review_steps": spec["disclosure"]}

    work_ids = {item["id"] for item in plan["work_items"]}
    if len(work_ids) != len(plan["work_items"]):
        raise core.CoordinatorError("work-item ids must be unique")
    selected = {doc["path"] for doc in spec["documents"]}
    if len(selected) != len(spec["documents"]):
        raise core.CoordinatorError("settled specification documents must be unique")
    if issue_result and issue_result.get("ok") and issue_result["doc_path"] not in selected:
        raise core.CoordinatorError(
            f"the originating Issue resolves {issue_result['doc_path']}, which is omitted from the plan's affected documents"
        )

    canonical_documents = []
    review_step_lines = []
    for declared in spec["documents"]:
        resolved = spec_referent.resolve_doc(str(root), declared["path"])
        if not resolved.get("ok"):
            raise core.CoordinatorError(f"{declared['path']} is not a usable settled specification: {resolved['detail']}")
        raw_digest = core.digest((root / declared["path"]).read_bytes())
        if declared["digest"] != raw_digest:
            raise core.CoordinatorError(f"{declared['path']} digest is stale; revise the plan from the canonical document")
        canonical = [criterion(declared["path"], i, row) for i, row in enumerate(resolved["criteria"])]
        mappings = declared["criteria"]
        by_id = {row["id"]: row for row in mappings}
        if len(by_id) != len(mappings):
            raise core.CoordinatorError(f"{declared['path']} contains duplicate criterion mappings")
        expected_ids = {row["id"] for row in canonical}
        missing, extra = expected_ids - set(by_id), set(by_id) - expected_ids
        if missing:
            raise core.CoordinatorError("plan settlement refused; omitted settled criterion: " + ", ".join(sorted(missing)))
        if extra:
            raise core.CoordinatorError("plan contains criterion mappings absent from the canonical spec: " + ", ".join(sorted(extra)))
        for row in canonical:
            mapped = by_id[row["id"]]
            for key in ("digest", "text", "how_verified"):
                if mapped[key] != row[key]:
                    raise core.CoordinatorError(f"criterion {row['id']} has stale canonical {key}")
            if mapped["disposition"] == "mapped":
                unknown = set(mapped["work_item_ids"]) - work_ids
                if unknown:
                    raise core.CoordinatorError(
                        f"criterion {row['id']} refers to unknown work items: {', '.join(sorted(unknown))}"
                    )
        canonical_documents.append({"path": declared["path"], "selection_reason": declared["selection_reason"],
                                    "digest": raw_digest, "criteria": canonical, "mappings": mappings})
        review_step_lines.append({"path": declared["path"], **spec_referent.review_steps(resolved)})
    return {"posture": "settled", "selection_basis": spec["selection_basis"],
            "documents": canonical_documents, "review_steps": review_step_lines,
            "digest": core.digest(canonical_documents)}


def hard_check_declarations(root: Path) -> list[dict]:
    sys.path.insert(0, str(root / ".engine" / "tools"))
    import repo_identity

    home = repo_identity.is_home_repo(str(root))
    out = []
    fixture_root = root / ".engine" / "_fixtures"
    for name in ("not-applicable.json", "construction-scoped.json", "requires.json"):
        for path in sorted(fixture_root.rglob(name)):
            if name == "construction-scoped.json" and home:
                continue
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise core.CoordinatorError(f"could not read hard-check declaration {path}: {exc}") from exc
            out.append({"path": str(path.relative_to(root)), "digest": core.digest(value), "declaration": value})
    return out
