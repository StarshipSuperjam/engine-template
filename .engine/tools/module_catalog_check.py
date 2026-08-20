#!/usr/bin/env python3
"""Module-catalog drift gate — the thin custom/script entry for engine/check/module-catalog-drift.

Runs as a `custom/script` check in CI: it confirms the committed optional-module catalog
(`.engine/provisioning/module-catalog.json`) still equals the canonical derivation from the module
manifests' `presentation` records — so a hand-edited or stale catalog turns engine-ci red until it is
regenerated and committed. It reads local committed files only (no network, no token). It emits finding.v1
JSON on stdout and returns 0 on a successful evaluation: an empty array in sync, one hard finding on drift or
an absent catalog. An internal crash returns non-zero, which the custom/script kind turns into a hard
fail-closed finding.
"""
from __future__ import annotations
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import module_catalog  # noqa: E402
import validate  # noqa: E402  (env-override seam for the negative-fixture meta-check)


def main() -> int:
    # ENGINE_MODULE_CATALOG_PATH (unset in production) points the COMMITTED-side read at a seeded stale
    # catalog, and MODULE_CATALOG_FIXTURE_ROOT roots the DERIVATION at the fixture's own seeded module set —
    # so the gate is witnessed biting a real bad input even in a deployment that declined its add-ons, where
    # a real-tree derivation would merge-preserve the stale catalog unchanged and never bite.
    path = validate.env_override_path("ENGINE_MODULE_CATALOG_PATH")
    root = validate.env_override_path("MODULE_CATALOG_FIXTURE_ROOT")
    print(json.dumps(module_catalog.check("hard", path=path, root=root)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
