#!/usr/bin/env python3
"""The operator policy-override FILE reader — the single home for reading the
per-deployment tuning file the `/engine-tune` command writes.

The override is committed **operator config**: a per-deployment file that supersedes named policy
tuning values per-key at read time. Its shape
is `{policy_id: {key: value}}` — one slice per policy, each a flat map of tuning key to a plain number.
It is **absent until the operator first tunes a value**, and it is **preserved across an engine update**
(claimed by no module + the operator-config carve-out in the ownership leg).

This module is a thin, pure READER and nothing more — it never writes (that is the verb tool
`tune.py`), never merges (that is `validate.effective_policy_values`, the core merge the consumers
call), and never reads a policy default. It is the floor both `boot` (which loads the override and
passes each slice as DATA to the live consumers) and `tune.py` (which shows the effective value) import
DOWN to — so neither cognitive consumer (`attention`/`telemetry`) ever reaches up to the operator-verb
tool, and the determinism law holds (the merged value is a static input the loading layer supplies).

Degradation: a missing, unreadable, or malformed override file returns `{}` — the deployment simply runs
on shipped defaults. The override is operator config, never a guardrail; a damaged file must narrow the
tuning to the defaults, never strand a boot (degrade-to-git-native). Each policy slice that is not a
plain object is dropped, so one malformed slice cannot poison the others.
"""
from __future__ import annotations
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402

# Committed operator config, top-level under .engine/ beside engine.json
# (configuration, not engine data); preserved across an engine update. Absent until the first tune.
OVERRIDES_PATH = os.path.join(validate.ENGINE_DIR, "operator-overrides.json")


def load(path: str = OVERRIDES_PATH) -> dict:
    """The committed operator policy-override as `{policy_id: {key: value}}`, or `{}` when there is no
    override file yet (the normal state until the operator first tunes). A missing, unreadable, malformed,
    or non-object file degrades to `{}` (never raises); any policy slice that is not itself a plain object
    is dropped, so one bad slice cannot poison the rest. The numbers themselves are not validated here —
    the per-key merge (`validate.effective_policy_values`) refuses a structural or stale key and surfaces
    it; this reader only delivers the committed data to the loading layer."""
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {policy_id: slice_ for policy_id, slice_ in data.items() if isinstance(slice_, dict)}


def slice_for(policy_id: str, path: str = OVERRIDES_PATH) -> dict:
    """The override slice for one policy (e.g. `attention`) as `{key: value}`, or `{}` when the policy has
    no override / there is no file. The shape a consumer passes to `effective_policy_values(default, …)`."""
    return load(path).get(policy_id, {})


if __name__ == "__main__":
    # A plain inspection aid: print the committed override (or {} when none), pretty + sorted.
    print(json.dumps(load(), indent=2, sort_keys=True))
