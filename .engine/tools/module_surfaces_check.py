#!/usr/bin/env python3
"""Module-surfaces drift gate — the thin custom/script entry for engine/check/module-surfaces-drift.

Runs as a `custom/script` check in CI: it confirms the committed module-surfaces registry
(`.engine/provisioning/module-surfaces.json`) still matches its canonical derivation from the present module
manifests — so a hand-edited or stale registry turns engine-ci red until it is regenerated and committed. It
reads local committed files only (no network, no token). It emits finding.v1 JSON on stdout (an empty array in
sync, one hard finding when drifted) and returns 0 on a successful evaluation; an internal crash returns
non-zero, which the custom/script kind turns into a hard fail-closed finding.

HOME-ONLY: the registry lists EVERY module's surfaces, but a deployment carries only its installed subset, so a
fresh derive there legitimately differs. Production therefore goes through `module_surfaces.check`, which is
silent anywhere that is not a positively confirmed home repository. The negative-fixture meta-check instead
drives `module_surfaces._compare` directly against a seeded tree (MODULE_SURFACES_FIXTURE_ROOT): a fixture tree
has no confirmed-home origin, so the gated `check` would skip it and the bite could never be witnessed. The
home GATE itself (silence off-home) is proven by unit tests in test_module_surfaces.py, not by this fixture.
"""
from __future__ import annotations
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import module_surfaces  # noqa: E402
import validate  # noqa: E402  (env-override seam for the negative-fixture meta-check)


def main() -> int:
    fixture_root = validate.env_override_path("MODULE_SURFACES_FIXTURE_ROOT")
    if fixture_root is not None:
        finding = module_surfaces._compare(fixture_root)   # bite path, home gate bypassed for the fixture
    else:
        finding = module_surfaces.check()                  # production: home-gated (silent off-home)
    print(json.dumps([finding] if finding else []))
    return 0


if __name__ == "__main__":
    sys.exit(main())
