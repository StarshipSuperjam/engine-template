#!/usr/bin/env python3
"""Behavioral FALSIFICATION for issue #599 (Slice 4) — an engine BELOW the release's clean-upgrade floor is
refused cleanly instead of producing a broken update.

A deployed engine older than the target release's `min_upgradeable_from` cannot reconcile cleanly to it — its own
already-shipped update code predates the reconcile — so the update must STOP before changing anything, name both
versions, and route the operator to the undo, rather than stalling with a broken tree and no pull request.

FAIL-THEN-PASS on the SAME release, differing only in the deployed version:
  * ARM 1 (below the floor): a deployed 0.2.0 engine previewing an update to a release whose floor is 0.3.2 is
    REFUSED — status "below-floor", the message names 0.2.0 and 0.3.2, says the engine is unchanged, and routes to
    the undo (never to unsupported re-instantiation).
  * ARM 2 (at the floor): a deployed 0.3.2 engine previewing the SAME release is NOT refused by the floor — the
    update proceeds normally.

It drives the REAL operator preview surface (`module_manager.plan_upgrade`, what bare `/engine-upgrade` runs) against
injected local trees, faking only the network. Census/fate: construction-only evidence — it is on the first-run
retire list (engine-VERSION floors are a construction-repo concern; a deployed repo cuts PRODUCT releases), so it
does not travel; its companion test (`test_module_manager.TestUpgradeFloorPreflight`) is the standing coverage.
"""
from __future__ import annotations
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import module_manager as mm     # noqa: E402  (the real upgrade preview under test)


def _write(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)


def _deployed_root(d: str, version: str) -> str:
    """A minimal deployed engine at `version`: engine.json + one installed module manifest."""
    root = os.path.join(d, f"deployed-{version}")
    _write(os.path.join(root, ".engine", "engine.json"),
           {"engine_release": version, "packages": {"base": version}, "identity": "solo",
            "home_repository": "acme/engine-home"})
    _write(os.path.join(root, ".engine", "modules", "base", "manifest.json"),
           {"id": "base", "version": version, "status": "required", "provides": {}, "depends": {}})
    return root


def _release_tree(d: str, floor: str, ver: str = "9.9.9") -> str:
    """A minimal release tree that DECLARES a clean-upgrade floor in its engine.json."""
    tree = os.path.join(d, "release")
    _write(os.path.join(tree, ".engine", "engine.json"),
           {"engine_release": ver, "packages": {"base": ver}, "identity": "solo",
            "min_upgradeable_from": floor})
    _write(os.path.join(tree, ".engine", "modules", "base", "manifest.json"),
           {"id": "base", "version": ver, "status": "required", "provides": {}, "depends": {}})
    return tree


def _preview(deployed_root: str, release_tree: str) -> dict:
    """Run the REAL read-only upgrade preview with ROOT pointed at the deployed engine and the release injected."""
    with mm._redirect_root(deployed_root):
        return mm.plan_upgrade(release_tree=release_tree, target_ref="9.9.9", available="9.9.9")


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        release = _release_tree(d, floor="0.3.2")

        # ARM 1 — below the floor: must refuse cleanly.
        res1 = _preview(_deployed_root(d, "0.2.0"), release)
        reason1 = (res1.get("reason") or "")
        arm1_ok = (res1.get("refused") is True
                   and res1.get("status") == "below-floor"
                   and "0.2.0" in reason1 and "0.3.2" in reason1
                   and "engine is unchanged" in reason1.lower()
                   and "undo" in reason1.lower()
                   and "re-instantiate" not in reason1.lower())

        # ARM 2 — at the floor: the floor must NOT refuse.
        res2 = _preview(_deployed_root(d, "0.3.2"), release)
        arm2_ok = (res2.get("refused") is not True and res2.get("status") != "below-floor")

    print("ARM 1 — deployed 0.2.0 vs floor 0.3.2 (expect a clean below-floor refusal):")
    print(f"  refused={res1.get('refused')} status={res1.get('status')!r}")
    print(f"  reason: {reason1}")
    print(f"  => {'PASS' if arm1_ok else 'FAIL'}")
    print("ARM 2 — deployed 0.3.2 vs floor 0.3.2 (expect it to proceed):")
    print(f"  refused={res2.get('refused')} status={res2.get('status')!r}")
    print(f"  => {'PASS' if arm2_ok else 'FAIL'}")

    if arm1_ok and arm2_ok:
        print("\nDEMO PASSED: a below-floor engine is refused cleanly and routed to the undo; an at-floor engine "
              "proceeds.")
        return 0
    print("\nDEMO FAILED: the clean-upgrade floor did not behave as expected.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
