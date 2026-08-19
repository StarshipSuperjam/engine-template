"""Engine-home validation for historical Build-orchestration traceability.

This is deliberately outside the runtime coordinator. The historical source
identity records the completed migration audit; ongoing validation proves that
the resulting preservation map remains structurally live. Semantic equivalence
remains an independent source-contract review judgment.
"""
from __future__ import annotations

from pathlib import Path
import sys

import build_coordinator_core as core


MAP_PATH = ".engine/build-orchestration-obligations.json"
SCHEMA_PATH = ".engine/schemas/build-orchestration-obligations.v1.json"


def _declined_owner(root: Path, owner: str) -> bool:
    """True when `owner` belongs to a real module that is NOT installed in this tree — a legitimate decline,
    not a dangling reference. Delegates to the engine's one authority for that question so this validator and
    the link-integrity check can never disagree; any failure to decide is False (tolerate nothing)."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import module_surfaces
        return bool(module_surfaces.declined_surface_owner(str(root / owner), str(root)))
    except Exception:  # noqa: BLE001 — undecidable ⇒ not tolerated
        return False


def validate_map(root: Path) -> dict:
    value = core.json_file(root / MAP_PATH)
    core.validate(value, root / SCHEMA_PATH)
    tests = "\n".join(path.read_text(encoding="utf-8")
                      for path in sorted((root / ".engine" / "tools").glob("test_build_coordinator*.py")))
    seen = set()
    for obligation in value["obligations"]:
        if obligation["id"] in seen:
            raise core.CoordinatorError(f"duplicate preservation id {obligation['id']}")
        seen.add(obligation["id"])
        owner = root / obligation["owner"]
        if not owner.is_file():
            # A `home-only` obligation names an owner that exists ONLY in the engine's home repository: the
            # schema already declares the field (`scope`: all | home-only) and the absence is mechanical —
            # the owner is a first-run-retired asset, so a deployed copy legitimately does not carry it, and
            # its prose anchor cannot be read there either. Keyed on the DECLARED scope, never on ambient
            # deployedness, so every other obligation keeps verifying its owner in every projection.
            if obligation.get("scope") == "home-only":
                continue
            # An owner delivered by a module the operator DECLINED is legitimately absent too — the same
            # tolerance the link-integrity check already applies. `declined_surface_owner` fails CLOSED (an
            # unreadable installed roster tolerates nothing) and never covers a path no real module owns, so a
            # genuinely dangling owner stays an error everywhere.
            if _declined_owner(root, obligation["owner"]):
                continue
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
