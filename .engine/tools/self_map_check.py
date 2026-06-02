#!/usr/bin/env python3
"""Self-map drift gate (core slice 8) — the thin custom/script entry for engine/check/self-map-drift.

Runs as a `custom/script` check rule in the CI suite: it confirms the committed self-map
(`.engine/self-map.md`) still matches its canonical derivation from the surface catalog + module
manifests, so a hand-edited or stale map turns engine-ci red until it is regenerated and committed.

It reads local committed files only — no network, no token — so it runs unchanged in the
head-checkout engine-ci context (tampering with this script to force a pass is a `.engine/tools/`
*modification*, which engine-guard flags; defense in depth). It emits finding.v1 JSON on stdout
(the custom/script machine channel) and returns 0 on a successful evaluation: an empty array when
the map is in sync, one `hard` finding (carrying the plain-language regenerate guidance) on drift
or an absent map. An internal crash returns non-zero, which the custom/script kind turns into a
hard fail-closed finding.

Superseded if slice 10's generalized coverage-fingerprint mode later re-homes this gate.
"""
from __future__ import annotations
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import self_map  # noqa: E402


def emit(findings: list) -> int:
    """Write the finding.v1 array to stdout and return 0 — a successful evaluation, whatever it
    found. The dispatcher's custom/script kind decides where the teeth land; the plain-language
    fix lives inside each finding's `message`, so stdout stays pure JSON."""
    print(json.dumps(findings))
    return 0


def main() -> int:
    f = self_map.check()
    return emit([f] if f["severity"] == "hard" else [])


if __name__ == "__main__":
    sys.exit(main())
