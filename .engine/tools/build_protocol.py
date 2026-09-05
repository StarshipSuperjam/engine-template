#!/usr/bin/env python3
"""build_protocol.py — the ONE loader for `.engine/build-protocol.json`, and the projection of its
review-consumer map into the Build runbook.

The Build protocol is schema-checked data: what candidate validation runs, what the final import
verifies, which deliverable-review lenses each depth runs, the submission preflights, the hard holds —
and, since StarshipSuperjam/engine-template#821's part C, which Build STAGE consumes which review roster
(`review_consumers`). That last table used to live as a fenced text block inside
build-orchestration.md, parsed back out by the lens-consumption check; a runbook is judgment prose, not
a data store, so the table moved here and the runbook now carries a GENERATED projection of it between
two marker comments, drift-checked at merge (engine/check/build-protocol).

Two rules this module keeps:

  * FAIL CLOSED. `load` raises `ProtocolError` on a missing, unparseable, or schema-violating file —
    never a partial dict. A consumer that wants "nothing consumed" must say so; it cannot get there by
    accident.
  * NO LENS LIST LIVES TWICE. A consumer entry names a ROSTER, not lenses. The deliverable roster is
    `deliverable_review.thorough` in the same file; the plan-review roster is the Project Manager's
    `PLAN_REVIEW_LENSES` (it left this file with the panel — a Build protocol declaring a review the
    Build does not run is a table nobody reads). Resolution happens here, in one place.

Operator commands:
  uv run --directory .engine --frozen -- python tools/build_protocol.py show     # print the projection
  uv run --directory .engine --frozen -- python tools/build_protocol.py render   # write it into the runbook
  uv run --directory .engine --frozen -- python tools/build_protocol.py check    # exit 1 on drift
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402

PROTOCOL_REL = ".engine/build-protocol.json"
SCHEMA_REL = ".engine/schemas/build-protocol.v1.json"
RUNBOOK_REL = ".engine/operations/build-orchestration.md"
#: Input-substitution seam for the negative-fixture meta-check (unset in production; see
#: validate.env_override_path): points `load` at a seeded bad protocol so the check is witnessed biting.
ENV_OVERRIDE = "ENGINE_BUILD_PROTOCOL_PATH"

#: The generated region's delimiters in the runbook. The begin line names the renderer so a reader who
#: meets the block knows not to hand-edit it; the end line closes the region for the drift check and for
#: the operation length count, which excludes generated regions (they are data, not runbook prose).
GENERATED_BEGIN = ("<!-- generated: build-protocol review-consumers — do not edit by hand; "
                   "render with `uv run --directory .engine --frozen -- python tools/build_protocol.py render` -->")
GENERATED_END = "<!-- /generated: build-protocol review-consumers -->"

#: Where each roster's lens list lives, in the projection's own words.
ROSTER_SOURCE = {
    "plan-review": "the Project Manager's plan-review roster",
    "deliverable-review": "the deliverable review at its widest depth",
}


class ProtocolError(ValueError):
    """The protocol could not be loaded as build-protocol.v1 — missing, unparseable, or off-schema."""


def protocol_path(root: str | None = None) -> str:
    return validate.env_override_path(ENV_OVERRIDE, os.path.join(root or validate.ROOT, PROTOCOL_REL))


def load(root: str | None = None) -> dict:
    """The protocol, validated against build-protocol.v1. Raises ProtocolError on any miss."""
    path = protocol_path(root)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError as exc:
        raise ProtocolError(f"{PROTOCOL_REL} is missing ({path})") from exc
    except (OSError, ValueError) as exc:
        raise ProtocolError(f"{PROTOCOL_REL} is not readable JSON: {exc}") from exc
    schema_path = os.path.join(root or validate.ROOT, SCHEMA_REL)
    with open(schema_path, encoding="utf-8") as fh:
        schema = json.load(fh)
    from jsonschema import Draft202012Validator  # lazy: the tool-runtime dependency validate.py also defers
    errors = sorted(Draft202012Validator(schema).iter_errors(data), key=lambda e: list(e.absolute_path))
    if errors:
        err = errors[0]
        where = "/".join(str(p) for p in err.absolute_path) or "(top level)"
        raise ProtocolError(f"{PROTOCOL_REL} does not match build-protocol.v1 at {where}: {err.message}")
    return data


def roster_lenses(protocol: dict, roster: str) -> list:
    """The lens list a roster name resolves to — the single place the two rosters are joined."""
    if roster == "deliverable-review":
        return list(protocol["deliverable_review"]["thorough"])
    if roster == "plan-review":
        import project_manager  # lazy: the panel's home; imported only to read its roster
        return list(project_manager.PLAN_REVIEW_LENSES["thorough"])
    raise ProtocolError(f"unknown review roster {roster!r} (the schema admits {sorted(ROSTER_SOURCE)})")


def consumers(protocol: dict) -> list:
    """[{stage, roster, lenses}] with every roster resolved; raises if a roster resolves to nothing."""
    out = []
    for entry in protocol["review_consumers"]:
        lenses = roster_lenses(protocol, entry["roster"])
        if not lenses:
            raise ProtocolError(f"stage {entry['stage']!r} consumes roster {entry['roster']!r}, which lists no lens")
        out.append({"stage": entry["stage"], "roster": entry["roster"], "lenses": lenses})
    return out


def consumed_lenses(protocol: dict | None = None, root: str | None = None) -> set:
    """The union of every lens some Build stage consumes. Fail-closed: raises rather than returning
    an empty set, so an unjudged roster can never read as 'nothing dangling'."""
    tokens: set = set()
    for entry in consumers(load(root) if protocol is None else protocol):
        tokens.update(entry["lenses"])
    if not tokens:
        raise ProtocolError("no Build stage consumes any review lens")
    return tokens


def render(protocol: dict) -> str:
    """The generated region, markers included, exactly as the runbook must carry it."""
    lines = [GENERATED_BEGIN,
             f"Which review lenses each Build stage consumes, from `{PROTOCOL_REL}` (`review_consumers`):",
             ""]
    for entry in consumers(protocol):
        lines.append(f"- **{entry['stage']}** — {', '.join(entry['lenses'])} ({ROSTER_SOURCE[entry['roster']]}).")
    lines += ["", "The coordinator still derives actual coverage from the installed roster and the approved depth; "
                  "this record keeps every installed review connected to a stage (engine/check/lens-consumption).",
              GENERATED_END]
    return "\n".join(lines)


def _split_runbook(text: str) -> tuple:
    """(before, region, after) around the generated region; region is None when the markers are absent
    or malformed (a begin without an end, or in the wrong order)."""
    b = text.find(GENERATED_BEGIN)
    e = text.find(GENERATED_END)
    if b < 0 or e < 0 or e < b:
        return text, None, ""
    end = e + len(GENERATED_END)
    return text[:b], text[b:end], text[end:]


def projection_status(root: str | None = None) -> tuple:
    """(expected_region, actual_region_or_None) for the runbook on disk."""
    expected = render(load(root))
    path = os.path.join(root or validate.ROOT, RUNBOOK_REL)
    with open(path, encoding="utf-8") as fh:
        _, actual, _ = _split_runbook(fh.read())
    return expected, actual


def apply(root: str | None = None) -> bool:
    """Write the current projection into the runbook's generated region. Returns True when the file
    changed. Refuses (ProtocolError) when the runbook carries no region to replace — a region is added
    by hand once, then only ever regenerated."""
    path = os.path.join(root or validate.ROOT, RUNBOOK_REL)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    before, region, after = _split_runbook(text)
    if region is None:
        raise ProtocolError(f"{RUNBOOK_REL} carries no generated review-consumers region to render into")
    new = before + render(load(root)) + after
    if new == text:
        return False
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new)
    return True


def main(argv: list) -> int:
    cmd = argv[0] if argv else "show"
    if cmd == "show":
        print(render(load()))
        return 0
    if cmd == "render":
        changed = apply()
        print(f"{'rendered' if changed else 'already current'}: {RUNBOOK_REL}")
        return 0
    if cmd == "check":
        expected, actual = projection_status()
        if actual == expected:
            print(f"current: {RUNBOOK_REL} matches {PROTOCOL_REL}")
            return 0
        print(f"DRIFT: the generated review-consumers region in {RUNBOOK_REL} does not match "
              f"{PROTOCOL_REL} — run `build_protocol.py render` and commit", file=sys.stderr)
        return 1
    print(f"usage: build_protocol.py [show|render|check]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
