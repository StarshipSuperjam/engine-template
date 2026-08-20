"""Engine-home validation for historical Build-orchestration traceability.

This is deliberately outside the runtime coordinator. The historical source
identity records the completed migration audit; ongoing validation proves that
the resulting preservation map remains structurally live. Semantic equivalence
remains an independent source-contract review judgment.
"""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_coordinator_core as core


MAP_PATH = ".engine/build-orchestration-obligations.json"
SCHEMA_PATH = ".engine/schemas/build-orchestration-obligations.v1.json"


def _declined_owner(root: Path, owner: str) -> "str | None":
    """The module id when `owner` belongs to a real module NOT installed in this tree — a legitimate decline,
    not a dangling reference — else None. Delegates to the engine's one authority for that question so this
    validator and the link-integrity check can never disagree. Undecidable means NOT tolerated; the reason is
    returned to the caller rather than swallowed, so a genuine fault is not reported as a missing owner."""
    import module_surfaces
    return module_surfaces.declined_surface_owner(str(root / owner), str(root))


def _away_from_home(root: Path) -> bool:
    """True when this checkout is NOT the engine's own home repository. `is_home_repo` fails TOWARD home, so
    an undecidable checkout is treated as home — the strict direction for a carve-out, since the home arm is
    the one that keeps enforcing."""
    import repo_identity
    return not repo_identity.is_home_repo(str(root))


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
            # its prose anchor cannot be read there either. Keyed on the DECLARED scope AND on actually being
            # away from home: at home the owner must exist, which is where this obligation is enforceable at
            # all. Every other obligation keeps verifying its owner in every projection.
            tolerated = obligation.get("scope") == "home-only" and _away_from_home(root)
            # An owner delivered by a module the operator DECLINED is legitimately absent too — the same
            # tolerance the link-integrity check already applies. `declined_surface_owner` fails CLOSED (an
            # unreadable installed roster tolerates nothing) and never covers a path no real module owns, so a
            # genuinely dangling owner stays an error everywhere.
            tolerated = tolerated or bool(_declined_owner(root, obligation["owner"]))
            if not tolerated:
                raise core.CoordinatorError(f"{obligation['id']} names missing owner {obligation['owner']}")
        anchor = obligation["anchor"]
        # A MECHANICAL anchor names a test function in the core-owned test_build_coordinator*.py files, which
        # every projection carries — so it stays verifiable even where the owner document is legitimately
        # absent. Only the PROSE arm needs the owner's text, and only that arm is skipped with it.
        if obligation["disposition"] == "mechanical":
            if not anchor.startswith("test_") or f"def {anchor}(" not in tests:
                raise core.CoordinatorError(f"{obligation['id']} lacks focused test anchor {anchor}")
        elif not owner.is_file():
            continue
        elif anchor.lower() not in owner.read_text(encoding="utf-8").lower():
            raise core.CoordinatorError(
                f"{obligation['id']} prose anchor is absent from {obligation['owner']}: {anchor}"
            )
    return value
