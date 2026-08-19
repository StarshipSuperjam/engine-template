"""Tests for the derived-state substrate — the single registry of derived-committed artifacts."""

import os
import tempfile
import unittest
from unittest import mock

import derived_state as ds


class TestRoster(unittest.TestCase):
    def test_roster_is_the_three_v1_members(self):
        self.assertEqual(ds.paths(), (
            ".engine/self-map.md",
            ".engine/knowledge/graph.json",
            ".engine/product-spec-matrix.json",
        ))

    def test_reconcile_and_release_filters(self):
        # all three participate in reconcile; the optional matrix is NOT in the release subset
        self.assertEqual(set(ds.paths(reconcile=True)), set(ds.paths()))
        self.assertEqual(ds.paths(release=True),
                         (".engine/self-map.md", ".engine/knowledge/graph.json"))

    def test_verify_and_regenerate_share_one_roster(self):
        # F-arch-2: repair (verify -> regenerate drifted -> re-verify) can only converge if the two
        # rosters are identical. verify() must report exactly one result per registered member.
        results = ds.verify()
        self.assertEqual([r.path for r in results], list(ds.paths()))


class TestSingleSource(unittest.TestCase):
    """The migrated consumers resolve their derived set FROM the registry — a future hard-coded copy that
    diverges fails here. Scoped to the consumers this substrate migrates (F-feas-4): sites that enumerate a
    DIFFERENT set for a different purpose are deliberately out of this binding."""

    def test_module_manager_regenerated_derived_is_the_registry(self):
        import module_manager
        self.assertEqual(tuple(module_manager.REGENERATED_DERIVED), ds.paths())

    def test_pr_reconcile_members_is_the_reconcile_registry(self):
        # pr_reconcile is migrated in the generalize step (with the present-gated reconcile set); once it
        # delegates, its MEMBERS is the reconcile registry.
        import pr_reconcile
        self.assertEqual(tuple(pr_reconcile.MEMBERS), ds.paths(reconcile=True))


class TestPresence(unittest.TestCase):
    def test_present_requires_a_resolvable_generator_not_just_a_file(self):
        # F-risk-3: a present file with an ABSENT generator must NOT count as present, so it stays out of
        # the reconcile set (needs-manual/refuse) instead of being append-merged then skipped.
        _needs_product_design(self)
        matrix = ".engine/product-spec-matrix.json"
        root = ds.validate.ROOT
        # with the real resolver the matrix is present on this tree
        self.assertIn(matrix, ds.paths(present_root=root))
        with mock.patch.object(ds, "_resolve_generate",
                               side_effect=lambda m: None if m.path == matrix else _real_resolve(m)):
            self.assertNotIn(matrix, ds.paths(present_root=root))


class TestRegenerate(unittest.TestCase):
    def test_a_generator_failure_is_surfaced_per_member_never_swallowed(self):
        # The core fix over module_manager._regen_indexes's `except Exception: pass`: a raising generator
        # yields a 'failed' MemberResult with the error, and its siblings still regenerate.
        graph = ".engine/knowledge/graph.json"

        def boom(**_kw):
            raise RuntimeError("generator exploded")

        def fake_resolve(member):
            return boom if member.path == graph else _real_resolve(member)

        with mock.patch.object(ds, "_resolve_generate", side_effect=fake_resolve):
            results = {r.path: r for r in ds.regenerate()}

        self.assertEqual(results[graph].status, "failed")
        self.assertIsNotNone(results[graph].error)
        self.assertIn("generator exploded", results[graph].error)
        # a sibling still ran (self-map is core and present on this tree)
        self.assertIn(results[".engine/self-map.md"].status, ("regenerated", "unchanged"))

    def test_absent_targets_are_skipped_not_fabricated(self):
        # Redirect the engine dir at an empty tree: every member's target is absent -> skipped-absent,
        # and nothing is written (never fabricate an index on a minimal tree).
        with tempfile.TemporaryDirectory() as tmp:
            engine_dir = os.path.join(tmp, ".engine")
            os.makedirs(engine_dir)
            with mock.patch.object(ds.validate, "ENGINE_DIR", engine_dir), \
                 mock.patch.object(ds.validate, "ROOT", tmp):
                results = ds.regenerate()
        self.assertTrue(all(r.status == "skipped-absent" for r in results))
        self.assertTrue(all(not r.changed for r in results))

    def test_optional_module_absent_is_skipped_no_generator(self):
        _needs_product_design(self)
        matrix = ".engine/product-spec-matrix.json"
        with mock.patch.object(ds, "_resolve_generate",
                               side_effect=lambda m: None if m.path == matrix else _real_resolve(m)):
            results = {r.path: r for r in ds.regenerate([matrix])}
        # the file exists on this tree, but the generator is (pretended) absent
        self.assertEqual(results[matrix].status, "skipped-no-generator")

    def test_unknown_dispatch_raises(self):
        with self.assertRaises(ValueError):
            ds.regenerate(dispatch="carrier-pigeon")

    def test_subprocess_dispatch_surfaces_a_raise_as_failed_never_propagates(self):
        # A hung (TimeoutExpired) or un-spawnable (OSError) generator on the subprocess path must become a
        # 'failed' MemberResult, NOT an uncaught exception — else a caller mid-merge skips its cleanup and the
        # integration queue wedges. (The subprocess path is what pr_reconcile/the coordinator use.)
        graph = ".engine/knowledge/graph.json"
        with mock.patch.object(ds.subprocess, "run", side_effect=OSError("spawn boom")):
            results = {r.path: r for r in ds.regenerate([graph], dispatch="subprocess")}
        self.assertEqual(results[graph].status, "failed")
        self.assertIn("spawn boom", results[graph].error)


class TestRepair(unittest.TestCase):
    def test_repair_regenerates_exactly_the_drifted_members(self):
        # verify -> regenerate(drifted) -> re-verify, without touching the real tree.
        drifted_path = ".engine/knowledge/graph.json"
        before = [
            ds.DriftResult(".engine/self-map.md", "r", "in-sync", ""),
            ds.DriftResult(drifted_path, "r", "drift", "stale"),
        ]
        after = [
            ds.DriftResult(".engine/self-map.md", "r", "in-sync", ""),
            ds.DriftResult(drifted_path, "r", "in-sync", ""),
        ]
        regen_calls = []

        def fake_regen(members_arg=None, **_kw):
            regen_calls.append(list(members_arg) if members_arg is not None else None)
            return [ds.MemberResult(drifted_path, "regenerated", True, "wrote")]

        with mock.patch.object(ds, "verify", side_effect=[before, after]), \
             mock.patch.object(ds, "regenerate", side_effect=fake_regen):
            b, regen, a = ds.repair()

        self.assertEqual(regen_calls, [[drifted_path]])   # only the drifted member regenerated
        self.assertTrue(all(d.status == "in-sync" for d in a))


_REAL_RESOLVE = ds._resolve_generate


def _installed_module_ids() -> set:
    """The module ids present in this tree. Mirrors the helper of the same name in test_seed.py."""
    import module_coherence
    return {m.get("id") for _p, m in module_coherence.discover_manifests() if isinstance(m, dict)}


def _needs_product_design(case) -> None:
    """The product-spec matrix is delivered by the OPTIONAL product-design module. A deployment that declined
    it has no matrix and no generator behind it, so a case keyed on that member has no subject there — the
    member's absence is the module's contract, not a derived-state defect."""
    if "product-design" not in _installed_module_ids():
        case.skipTest("the product-spec matrix is provided by the declined product-design module")


def _real_resolve(member):
    return _REAL_RESOLVE(member)


if __name__ == "__main__":
    unittest.main()
