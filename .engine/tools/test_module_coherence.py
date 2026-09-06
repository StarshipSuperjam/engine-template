#!/usr/bin/env python3
"""Self-tests for module_coherence's register readers' root seam.

The ownership register is a LIVE-TREE enumeration, and until StarshipSuperjam/engine-template#883 its
foundation half read `validate.ROOT` unconditionally, so a caller handed a fixture tree silently got the
ambient repository's foundation files back — the fail-OPEN direction for a classifier that treats "not in
the register" as the project's. These tests hold the seam: a root is honoured by both halves, the default
still reads the real tree, and the enumeration remains presence-based (which is why the classifier
declares its own floor by name rather than trusting this set for deleted files).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import module_coherence as mc  # noqa: E402
import validate  # noqa: E402


def _write(root: str, rel: str, text: str = "x\n") -> None:
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class TestRegisterRootSeam(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        _write(self.root, "CLAUDE.md")
        _write(self.root, ".gitignore")
        _write(self.root, ".engine/engine.json", json.dumps({"home_repository": "test/home"}))
        _write(self.root, ".engine/modules/fixture/manifest.json",
               json.dumps({"id": "fixture", "provides": {"tool": [".engine/tools/*.py"]}}))
        _write(self.root, ".engine/tools/fixture_tool.py")
        _write(self.root, "src/app.py")

    def tearDown(self):
        self.tmp.cleanup()

    def test_foundation_paths_read_the_named_root(self):
        owned = mc.foundation_infra_paths(self.root)
        self.assertEqual(owned, [".engine/engine.json", ".gitignore", "CLAUDE.md"])
        # AGENTS.md is a FOUNDATION_INFRA member the fixture does not carry: presence-based, so absent.
        self.assertIn("AGENTS.md", mc.FOUNDATION_INFRA)
        self.assertNotIn("AGENTS.md", owned)

    def test_engine_owned_paths_threads_the_root_through_both_halves(self):
        manifests = mc.discover_manifests(self.root)
        self.assertEqual([rel for rel, _ in manifests], [".engine/modules/fixture/manifest.json"])
        owned = mc.engine_owned_paths(manifests, root=self.root)
        self.assertIn(".engine/tools/fixture_tool.py", owned)      # the provides half, against the fixture
        self.assertIn("CLAUDE.md", owned)                            # the foundation half, against the fixture
        self.assertNotIn("src/app.py", owned)
        # Nothing from the ambient repository leaks in: its tools are not the fixture's.
        self.assertNotIn(".engine/tools/module_coherence.py", owned)

    def test_default_root_is_the_real_tree(self):
        owned = mc.foundation_infra_paths()
        self.assertEqual(owned, mc.foundation_infra_paths(validate.ROOT))
        self.assertIn("CLAUDE.md", owned)
        self.assertIn(".engine/engine.json", owned)


if __name__ == "__main__":
    unittest.main()
