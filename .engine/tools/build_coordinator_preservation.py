"""Engine-home validation for historical Build-orchestration traceability.

This is deliberately outside the runtime coordinator. It proves that the
preservation map is structurally live; semantic equivalence remains an
independent source-contract review judgment.
"""
from __future__ import annotations

from pathlib import Path

import build_coordinator_core as core


MAP_PATH = ".engine/build-orchestration-obligations.json"
SCHEMA_PATH = ".engine/schemas/build-orchestration-obligations.v1.json"


def validate_map(root: Path) -> dict:
    value = core.json_file(root / MAP_PATH)
    core.validate(value, root / SCHEMA_PATH)
    source = value["preservation_source"]
    historical = core.run(["git", "show", source["git_object"]], root=root)
    if historical.returncode:
        raise core.CoordinatorError("the original Build runbook object is unavailable")
    payload = historical.stdout.encode()
    if (core.digest(payload) != source["digest"]
            or len(historical.stdout.splitlines()) != source["lines"]
            or len(historical.stdout.split()) != source["words"]):
        raise core.CoordinatorError("the original Build runbook does not match preservation metadata")
    tests = "\n".join(path.read_text(encoding="utf-8")
                      for path in sorted((root / ".engine" / "tools").glob("test_build_coordinator*.py")))
    seen = set()
    for obligation in value["obligations"]:
        if obligation["id"] in seen:
            raise core.CoordinatorError(f"duplicate preservation id {obligation['id']}")
        seen.add(obligation["id"])
        owner = root / obligation["owner"]
        if not owner.is_file():
            raise core.CoordinatorError(f"{obligation['id']} names missing owner {obligation['owner']}")
        anchor = obligation["anchor"]
        if obligation["disposition"] == "mechanical":
            if not anchor.startswith("test_") or f"def {anchor}(" not in tests:
                raise core.CoordinatorError(f"{obligation['id']} lacks focused test anchor {anchor}")
        elif anchor.lower() not in owner.read_text(encoding="utf-8").lower():
            raise core.CoordinatorError(
                f"{obligation['id']} prose anchor is absent from {obligation['owner']}: {anchor}"
            )
    return value
