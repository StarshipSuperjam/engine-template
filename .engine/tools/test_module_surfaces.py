#!/usr/bin/env python3
"""Self-tests for the module-surfaces registry: it stays in sync with the manifests (in the source repo), and
`declined_surface_owner` recognizes a path owned by a NOT-installed module — the seam the link-integrity check
uses to tolerate a dangling link into a declined module's surface (#646)."""
from __future__ import annotations
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate            # noqa: E402
import module_surfaces as ms  # noqa: E402
import derived_state       # noqa: E402
import repo_identity       # noqa: E402

_CONSTRUCTION = repo_identity.is_home_repo(validate.ROOT) and not os.environ.get("ENGINE_NESTED_SELFTEST")


class TestRegistryInSync(unittest.TestCase):
    @unittest.skipUnless(_CONSTRUCTION, "the committed registry equals the DERIVED set only where every module "
                         "is present — a deployment carries the full registry but a subset of manifests (#646)")
    def test_committed_registry_matches_the_derived_set(self):
        # Stale registry gate: regenerate with `module_surfaces.py generate` and commit if this fails.
        self.assertEqual(ms.load(), ms.derive(),
                         "the committed module-surfaces.json is stale — run module_surfaces.py generate")

    def test_registry_maps_an_optional_module_surface(self):
        # A concrete anchor: the product-design operation a core runbook links to is owned by product-design.
        self.assertEqual(ms.load().get(os.path.join(".engine", "operations", "product-intake.md")),
                         ["product-design"])


class TestDriftCheck(unittest.TestCase):
    """The new drift check (a derived_state member's own gate). The BITE path is proven directly via _compare
    against a seeded tree (a fixture tree has no confirmed-home origin, so the gated check would skip it); the
    home GATE (silence off-home) is proven here as unit tests, per the deliberate split."""

    def _tree(self, provides_file: str | None):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, ".engine", "provisioning"))
        os.makedirs(os.path.join(d, ".engine", "modules", "fx"))
        os.makedirs(os.path.join(d, "src"))
        with open(os.path.join(d, ".engine", "modules", "fx", "manifest.json"), "w") as fh:
            json.dump({"id": "fx", "status": "optional", "version": "1.0.0",
                       "provides": {"src": ["src/fx.txt"]}}, fh)
        with open(os.path.join(d, "src", "fx.txt"), "w") as fh:
            fh.write("surface")
        surfaces = {} if provides_file is None else {"src/fx.txt": ["fx"]}
        with open(os.path.join(d, ms.REGISTRY_REL), "w") as fh:
            fh.write(ms._serialize(surfaces))
        return d

    def test_compare_bites_a_stale_registry(self):
        d = self._tree(provides_file=None)                      # committed registry is EMPTY = stale
        finding = ms._compare(d)
        self.assertIsNotNone(finding)
        self.assertEqual(finding["severity"], "hard")
        self.assertIn("out of date", finding["message"])

    def test_compare_is_silent_when_in_sync(self):
        d = self._tree(provides_file="src/fx.txt")              # committed registry matches derive
        self.assertIsNone(ms._compare(d))

    def test_check_is_silent_off_home_even_when_stale(self):
        # the home GATE: a stale registry in a NON-home tree returns None (no false drift in a deployment).
        d = self._tree(provides_file=None)
        with mock.patch.object(derived_state, "is_confirmed_home", return_value=False):
            self.assertIsNone(ms.check(root=d))

    def test_check_bites_when_confirmed_home(self):
        d = self._tree(provides_file=None)
        with mock.patch.object(derived_state, "is_confirmed_home", return_value=True):
            self.assertIsNotNone(ms.check(root=d))


class TestDeclinedSurfaceOwner(unittest.TestCase):
    def _root(self, packages, surfaces):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, ".engine", "provisioning"))
        with open(os.path.join(d, ".engine", "engine.json"), "w", encoding="utf-8") as fh:
            json.dump({"packages": {p: "0.1.0" for p in packages}}, fh)
        with open(os.path.join(d, ms.REGISTRY_REL), "w", encoding="utf-8") as fh:
            json.dump({"surfaces": surfaces}, fh)
        return d

    def test_overlaid_surface_of_a_declined_module_is_owned(self):
        d = self._root(packages=["core"], surfaces={".engine/operations/opt.md": ["opt-mod"]})
        self.assertEqual(ms.declined_surface_owner(os.path.join(d, ".engine/operations/opt.md"), root=d), "opt-mod")

    def test_surface_of_an_installed_module_is_not_flagged(self):
        d = self._root(packages=["core", "opt-mod"], surfaces={".engine/operations/opt.md": ["opt-mod"]})
        self.assertIsNone(ms.declined_surface_owner(os.path.join(d, ".engine/operations/opt.md"), root=d))

    def test_a_shared_surface_is_tolerated_only_when_no_owner_is_installed(self):
        # A path two modules provide is softened only if BOTH are declined — an installed co-owner keeps it hard.
        surf = {".engine/tools/shared.py": ["opt-a", "opt-b"]}
        kept = self._root(packages=["core", "opt-b"], surfaces=surf)      # opt-b still installed
        self.assertIsNone(ms.declined_surface_owner(os.path.join(kept, ".engine/tools/shared.py"), root=kept))
        gone = self._root(packages=["core"], surfaces=surf)              # both declined
        self.assertEqual(ms.declined_surface_owner(os.path.join(gone, ".engine/tools/shared.py"), root=gone),
                         "opt-a")

    def test_a_file_under_a_declined_REAL_modules_own_directory_is_owned(self):
        # opt-mod is a real module (it owns a surface in the registry) that is declined here.
        d = self._root(packages=["core"], surfaces={".engine/tools/opt/x.py": ["opt-mod"]})
        self.assertEqual(ms.declined_surface_owner(
            os.path.join(d, ".engine/modules/opt-mod/manifest.json"), root=d), "opt-mod")

    def test_a_typo_or_fake_module_directory_stays_a_real_broken_link(self):
        # A module-dir-shaped path whose name is NOT a real module (a typo / renamed / removed dir) must stay a
        # HARD broken link — the gate-integrity hole the re-audit caught.
        d = self._root(packages=["core"], surfaces={".engine/tools/opt/x.py": ["opt-mod"]})
        self.assertIsNone(ms.declined_surface_owner(
            os.path.join(d, ".engine/modules/totally-fake/x.md"), root=d))

    def test_fails_closed_when_the_installed_roster_cannot_be_read(self):
        # No engine.json -> installed set empty -> tolerate NOTHING (never soften every link).
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, ".engine", "provisioning"))
        with open(os.path.join(d, ms.REGISTRY_REL), "w", encoding="utf-8") as fh:
            json.dump({"surfaces": {".engine/operations/opt.md": ["opt-mod"]}}, fh)
        self.assertIsNone(ms.declined_surface_owner(os.path.join(d, ".engine/operations/opt.md"), root=d))

    def test_a_path_no_module_owns_is_a_real_broken_link(self):
        d = self._root(packages=["core"], surfaces={".engine/operations/opt.md": ["opt-mod"]})
        self.assertIsNone(ms.declined_surface_owner(os.path.join(d, ".engine/docs/nope.md"), root=d))


class TestGenerateCliHomeGuard(unittest.TestCase):
    """The bare `module_surfaces.py generate` hand-run must refuse off-home: the registry lists EVERY module's
    surfaces, so regenerating it from a deployment's reduced manifest set would erase declined modules'
    ownership. The guard delegates to the positive-home, fail-closed derived_state.is_confirmed_home."""

    def test_refuses_and_does_not_generate_when_home_is_not_confirmed(self):
        with mock.patch.object(derived_state, "is_confirmed_home", return_value=False), \
             mock.patch.object(ms, "generate") as gen:
            code = ms._run_generate_cli()
        self.assertEqual(code, 2)
        gen.assert_not_called()

    def test_generates_when_home_is_confirmed(self):
        with mock.patch.object(derived_state, "is_confirmed_home", return_value=True), \
             mock.patch.object(ms, "generate") as gen:
            code = ms._run_generate_cli()
        self.assertEqual(code, 0)
        gen.assert_called_once()


if __name__ == "__main__":
    unittest.main()
