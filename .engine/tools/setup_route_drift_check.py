#!/usr/bin/env python3
"""Setup-route drift gate — the thin custom/script entry for engine/check/setup-route-drift.

Runs as a `custom/script` check in CI: it confirms the committed per-module setup routes
(`.claude/skills/engine-setup-<module-id>/SKILL.md`) still match their canonical derivation from the offerable
manifests' `presentation` records — so a hand-edited or stale setup route (or one missing after a manifest
change) turns engine-ci red until it is regenerated and committed. It reads local committed files only (no
network, no token). It emits finding.v1 JSON on stdout and returns 0 on a successful evaluation: an empty
array in sync, one hard finding per drifted/missing/stray route. An internal crash returns non-zero, which the
custom/script kind turns into a hard fail-closed finding.
"""
from __future__ import annotations
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import setup_route_gen  # noqa: E402
import validate  # noqa: E402  (env-override seam for the negative-fixture meta-check)


def main() -> int:
    # SETUP_ROUTE_FIXTURE_ROOT (unset in production) roots BOTH sides at a seeded tree — the committed routes
    # and the module set the derivation reads — so the gate is witnessed biting a tree whose routes are gone
    # even in a deployment that declined its add-ons, where a real-tree derivation is empty and cannot bite.
    root = validate.env_override_path("SETUP_ROUTE_FIXTURE_ROOT")
    print(json.dumps(setup_route_gen.check("hard", root=root)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
