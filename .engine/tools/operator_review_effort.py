#!/usr/bin/env python3
"""The operator per-depth review-EFFORT override FILE reader — the single home for reading the
per-deployment file that retunes how much reasoning effort each review depth spends.

The override is committed **operator config**: a per-deployment file that supersedes the shipped per-depth
review effort (`review_depths` in `.engine/policies/model-bindings.json`) at read time. Its shape is
`{depth: {"effort": level}}` — one slice per depth (`standard`, `thorough`), each naming an effort level
(`low`/`medium`/`high`). It is **absent until the operator first retunes a depth's effort**, and it is
**preserved across an engine update** (claimed by no module + the operator-config carve-out in
module_coherence.OPERATOR_CONFIG), so a deployment's retune survives while an update still ships new
shipped defaults.

WHY THIS EXISTS SEPARATELY FROM `/engine-tune`: that command and its merge (`validate.effective_policy_values`)
are number-only and flat — they refuse a non-number value and key their defaults off a policy's markdown
`values` block. A review-depth effort is a string enum in a nested JSON file with no such markdown home, so it
cannot ride that seam; this is its own thin reader instead (StarshipSuperjam/engine-template#677).

DELIBERATE no-shape-check exception. Unlike the two sibling operator-config files
(`operator-guarded-paths.json`, `operator-local-references.json`, each with a hard `engine/check/operator-*`
shape gate), this file has none — mirroring `operator-overrides.json`'s own no-check precedent. The reason the
siblings need a gate is that a malformed declaration silently degrades to EMPTY, so declared protection stops
happening. Here the degrade direction is inverted and SAFE: a missing, unreadable, malformed, or
unrecognised-effort slice degrades to the SHIPPED default (the strong anchor), so malformation can only make
review stronger, never weaker. A misspelled depth or effort therefore reverts to the default rather than
opening a hole; the operator meets that in the per-build receipt (planned-vs-actual effort) and the merge diff.

This module is a thin, pure READER: it never writes (the set/forget verb owns that), never merges (the depth
resolver `agent_bindings.depth_effort` layers it over the shipped default), and never reads the shipped
default. A missing, unreadable, malformed, or non-object file returns `{}` — the deployment simply runs on the
shipped per-depth efforts. Each depth slice that is not a plain object, or whose effort is not a recognised
level, is dropped, so one bad slice cannot poison the others.
"""
from __future__ import annotations
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402

# Committed operator config, top-level under .engine/ beside engine.json (configuration, not engine data);
# preserved across an engine update. Absent until the first depth-effort retune.
OVERRIDES_PATH = os.path.join(validate.ENGINE_DIR, "operator-review-effort.json")

_DEPTHS = ("standard", "thorough")   # quick runs no reviewers, so it carries no tunable effort
_EFFORTS = ("low", "medium", "high")


def overrides_path(root: str | None = None) -> str:
    """The committed override file's path, for a given engine tree root (or the running tree when None).
    The single home for this derivation so callers never re-spell `.engine/operator-review-effort.json`."""
    return OVERRIDES_PATH if root is None else os.path.join(root, ".engine", "operator-review-effort.json")


def _write(path: str, data: dict) -> None:
    """Write the override map crash-safe (temp-file + os.replace), matching the bootstrap/knowledge_index
    convention, so an interrupted retune never leaves a truncated operator-config file."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def load(path: str = OVERRIDES_PATH) -> dict:
    """The committed operator depth-effort override as `{depth: {"effort": level}}`, or `{}` when there is no
    override file yet (the normal state until the operator first retunes). A missing, unreadable, malformed, or
    non-object file degrades to `{}` (never raises). A slice is kept only when it names a real depth and a real
    effort level; anything else is dropped (degrade-to-shipped-default, the safe direction)."""
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001 — a damaged operator file must narrow to shipped defaults, never strand
        return {}
    if not isinstance(data, dict):
        return {}
    kept: dict = {}
    for depth, slice_ in data.items():
        if depth in _DEPTHS and isinstance(slice_, dict) and slice_.get("effort") in _EFFORTS:
            kept[depth] = {"effort": slice_["effort"]}
    return kept


def stale_slices(path: str = OVERRIDES_PATH) -> list[str]:
    """The depth slices in the committed file that the reader DROPS — an unrecognised depth key or an effort
    that is not a real level. Reported so a misspelled retune (which silently reverts to the shipped default)
    is surfaced to the operator rather than passing unnoticed. Empty when the file is absent or fully valid."""
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(data, dict):
        return []
    stale = []
    for depth, slice_ in data.items():
        if depth not in _DEPTHS:
            stale.append(f"{depth}: not a tunable review depth (expected one of {', '.join(_DEPTHS)})")
        elif not isinstance(slice_, dict) or slice_.get("effort") not in _EFFORTS:
            got = slice_.get("effort") if isinstance(slice_, dict) else slice_
            stale.append(f"{depth}: effort {got!r} is not one of {', '.join(_EFFORTS)}")
    return stale


def set_effort(depth: str, effort: str, path: str = OVERRIDES_PATH) -> dict:
    """Write one depth's effort override into the committed file (creating it on first use), returning the
    new full override map. Refuses an unknown depth or effort in plain words — the caller relays it. The write
    only prepares the change; it takes effect when the operator merges it, like any committed config."""
    if depth not in _DEPTHS:
        raise ValueError(f"'{depth}' is not a tunable review depth — choose one of {', '.join(_DEPTHS)} "
                         "(quick runs no reviewers, so it has no effort to set).")
    if effort not in _EFFORTS:
        raise ValueError(f"'{effort}' is not a reasoning-effort level — choose one of {', '.join(_EFFORTS)}.")
    current = load(path)
    current[depth] = {"effort": effort}
    _write(path, current)
    return current


def forget(depth: str, path: str = OVERRIDES_PATH) -> tuple[dict, bool]:
    """Drop one depth's effort override so it reverts to the shipped default, returning
    `(remaining_map, changed)`. Refuses an unknown depth in plain words (like `set_effort`), so a typo is
    caught rather than silently treated as a no-op. `changed` is False when the depth carried no override —
    nothing was written and the caller can say so honestly. Removes the file when the last override clears."""
    if depth not in _DEPTHS:
        raise ValueError(f"'{depth}' is not a tunable review depth — choose one of {', '.join(_DEPTHS)} "
                         "(quick runs no reviewers, so it has no effort to forget).")
    current = load(path)
    if depth not in current:
        return current, False
    current.pop(depth)
    if current:
        _write(path, current)
    elif os.path.isfile(path):
        os.remove(path)
    return current, True


def main(argv: list | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if argv and argv[0] == "show":
        overrides = load()
        print(json.dumps(overrides, indent=2, sort_keys=True) if overrides
              else "no operator review-effort override (running on shipped per-depth defaults)")
        for s in stale_slices():
            print("IGNORED (reverts to shipped default): " + s, file=sys.stderr)
        return 0
    if len(argv) == 3 and argv[0] == "set":
        try:
            set_effort(argv[1], argv[2])
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"prepared: {argv[1]} review depth now runs at {argv[2]} effort — merge the change to apply it.")
        return 0
    if len(argv) == 2 and argv[0] == "forget":
        try:
            _, changed = forget(argv[1])
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if changed:
            print(f"prepared: {argv[1]} review depth reverts to the shipped default — merge the change to apply it.")
        else:
            print(f"nothing to revert: {argv[1]} review depth already runs on the shipped default.")
        return 0
    print("usage: operator_review_effort.py show | set <standard|thorough> <low|medium|high> | forget <depth>")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
