#!/usr/bin/env python3
"""Self-map drift gate — the thin custom/script entry for engine/check/self-map-drift.

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

Superseded if a generalized coverage-fingerprint mode later re-homes this gate.
"""
from __future__ import annotations
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import self_map  # noqa: E402
import validate  # noqa: E402  (the negative-fixture meta-check's input-substitution seam)


emit = validate.emit
USAGE = ("Usage: self_map_check.py [-h|--help]\n\n"
         "Checks the committed self-map and emits a finding.v1 JSON array. "
         "Environment: ENGINE_SELF_MAP_PATH.")


def _main() -> int:
    # ENGINE_SELF_MAP_PATH (unset in every production run) lets the negative-fixture meta-check
    # point the committed-side read at a seeded stale map while the canonical side still derives
    # from the real repo — so the drift gate is witnessed biting a real bad input (StarshipSuperjam/engine-template#286).
    f = self_map.check(validate.env_override_path("ENGINE_SELF_MAP_PATH"))
    return emit([f] if f["severity"] == "hard" else [])


def main(argv: list | None = None) -> int:
    argv = [] if argv is None else argv
    return validate.cli_main(argv, usage=USAGE, run=lambda _argv: _main())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
