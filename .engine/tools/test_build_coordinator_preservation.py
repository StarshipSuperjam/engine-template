#!/usr/bin/env python3
"""Structural checks for the historical Build preservation artifact."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_coordinator_preservation as preservation  # noqa: E402
import build_coordinator_core as core  # noqa: E402
import build_coordinator as coordinator  # noqa: E402


ROOT = Path(__file__).resolve().parents[2]


class TestPreservationTraceability(unittest.TestCase):
    def test_every_mapped_obligation_has_one_live_structural_disposition(self):
        value = preservation.validate_map(ROOT)
        self.assertEqual(len(value["obligations"]), 68)
        self.assertEqual(len({row["id"] for row in value["obligations"]}), 68)

    def test_preservation_assurance_does_not_claim_semantic_proof(self):
        assurance = preservation.validate_map(ROOT)["preservation_source"]["assurance"].lower()
        self.assertIn("not proof of semantic equivalence", assurance)
        self.assertIn("independent qa", assurance)

    def test_runtime_protocol_does_not_scan_historical_preservation_anchors(self):
        value = coordinator._protocol()
        self.assertNotIn("obligations", value)
        self.assertNotIn("preservation_source", value)


class TestMissingOwnerTolerance(unittest.TestCase):
    """The missing-owner branch's BOTH arms, driven in a copied tree. Nothing else exercises this code: at
    home every owner exists, so the branch is never entered there."""

    def _tree(self, d, *, installed=("core",)):
        """A minimal checkout carrying the REAL map, schema and module-surfaces registry — so "which module
        owns this path" is decided by the same data the engine ships — with only the INSTALLED roster varied,
        which is exactly what an operator's decline changes."""
        import json as _json
        import shutil
        root = Path(d)
        (root / ".engine" / "provisioning").mkdir(parents=True)
        (root / ".engine" / "schemas").mkdir(parents=True)
        (root / ".engine" / "tools").mkdir(parents=True)
        shutil.copy(ROOT / preservation.MAP_PATH, root / preservation.MAP_PATH)
        shutil.copy(ROOT / preservation.SCHEMA_PATH, root / preservation.SCHEMA_PATH)
        for src in (ROOT / ".engine" / "tools").glob("test_build_coordinator*.py"):
            shutil.copy(src, root / ".engine" / "tools" / src.name)
        (root / ".engine" / "engine.json").write_text(
            _json.dumps({"packages": {m: "1.0.0" for m in installed}}), encoding="utf-8")
        shutil.copy(ROOT / ".engine" / "provisioning" / "module-surfaces.json",
                    root / ".engine" / "provisioning" / "module-surfaces.json")
        # Every owner the map names, carrying EVERY prose anchor that names it (several obligations share one
        # owner, so each file must satisfy all of them). Cases then delete the one owner they are about.
        anchors: dict = {}
        for obligation in _json.loads((root / preservation.MAP_PATH).read_text(encoding="utf-8"))["obligations"]:
            if obligation["disposition"] != "mechanical":
                anchors.setdefault(obligation["owner"], []).append(obligation["anchor"])
            else:
                anchors.setdefault(obligation["owner"], [])
        for rel, texts in anchors.items():
            owner = root / rel
            owner.parent.mkdir(parents=True, exist_ok=True)
            owner.write_text("\n".join(texts) + "\n", encoding="utf-8")
        return root

    def _drop(self, root, owner):
        (root / owner).unlink()

    def _home_only_owner(self, root) -> str:
        """The owner of the map's `home-only` obligation, read FROM the map rather than named here — the file
        it points at is removed when a project is first set up, so a literal reference would break the very
        first check in a new project (and would go stale if the scoped obligation ever changes)."""
        import json as _json
        for obligation in _json.loads((root / preservation.MAP_PATH).read_text(encoding="utf-8"))["obligations"]:
            if obligation.get("scope") == "home-only":
                return obligation["owner"]
        self.skipTest("no home-only obligation in the map to exercise")

    def test_a_home_only_owner_is_tolerated_only_away_from_home(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._tree(d)
            self._drop(root, self._home_only_owner(root))
            with mock.patch.object(preservation, "_away_from_home", return_value=True):
                self.assertTrue(preservation.validate_map(root))
            with mock.patch.object(preservation, "_away_from_home", return_value=False):
                with self.assertRaises(core.CoordinatorError):   # AT HOME it must still be present
                    preservation.validate_map(root)

    def test_a_declined_modules_owner_is_tolerated(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._tree(d, installed=("core",))   # qa-review declined here
            self._drop(root, ".claude/agents/engine-qa-review-spec-conformance.md")
            self.assertTrue(preservation.validate_map(root))

    def test_an_owner_no_module_owns_still_raises(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._tree(d)
            self._drop(root, ".engine/operations/build-orchestration.md")   # core-owned; never tolerated
            with self.assertRaises(core.CoordinatorError):
                preservation.validate_map(root)

    def test_an_installed_modules_missing_owner_still_raises(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._tree(d, installed=("core", "qa-review"))   # installed ⇒ never tolerated
            self._drop(root, ".claude/agents/engine-qa-review-spec-conformance.md")
            with self.assertRaises(core.CoordinatorError):
                preservation.validate_map(root)

    def test_a_tolerated_owner_still_has_its_mechanical_anchor_checked(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._tree(d)
            self._drop(root, self._home_only_owner(root))
            for p in (root / ".engine" / "tools").glob("test_build_coordinator*.py"):
                p.write_text("", encoding="utf-8")   # every mechanical anchor now absent
            with mock.patch.object(preservation, "_away_from_home", return_value=True):
                with self.assertRaises(core.CoordinatorError):
                    preservation.validate_map(root)


if __name__ == "__main__":
    unittest.main()
