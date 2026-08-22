"""Tests for the derived-state substrate — the single registry of derived-committed artifacts.

Two layers: (1) the roster/consumer invariants that must stay byte-identical for the pre-existing four
members as the model generalized, and (2) the generalized MODEL — multi-output members, owner_of,
positive-home scope, type-aware presence, the fail-closed verify normalizer, and topological order — tested
with SYNTHETIC members where the current roster does not yet carry the shape (E2 registers the real ones)."""

import os
import tempfile
import unittest
from unittest import mock

import derived_state as ds


# ---- synthetic members exercising the generalized dimensions ----------------------------------------

_TREE_MEMBER = ds.DerivedMember(
    path=".codex/agents", tool="codex_gen.py",
    outputs=(ds.Output("tree", ".codex/agents/"), ds.Output("tree", ".agents/skills/")),
    check_rules=("engine/check/codex-agent-coherence", "engine/check/codex-skill-coherence"),
    reconcile=True, release=False, upgrade=False, scope="both", exclusive=True, fork_guard_core=False)

_HOME_MEMBER = ds.DerivedMember(
    path=".engine/provisioning/module-surfaces.json", tool="module_surfaces.py",
    outputs=(ds.Output("file", ".engine/provisioning/module-surfaces.json"),),
    check_rules=("engine/check/module-surfaces-drift",),
    reconcile=True, release=True, upgrade=False, scope="home", exclusive=False, fork_guard_core=False)

_NONEXCLUSIVE_TREE = ds.DerivedMember(
    path=".claude/skills/engine-setup", tool="setup_route_gen.py",
    outputs=(ds.Output("tree", ".claude/skills/engine-setup-x/"),),
    check_rules=("engine/check/setup-route-drift",),
    reconcile=False, release=False, upgrade=False, scope="both", exclusive=False, fork_guard_core=False)


class TestRoster(unittest.TestCase):
    def test_roster_is_the_four_derived_members(self):
        self.assertEqual(ds.paths(), (
            ".engine/self-map.md",
            ".engine/docs/ci-assurance.md",
            ".engine/knowledge/graph.json",
            ".engine/product-spec-matrix.json",
        ))

    def test_reconcile_release_upgrade_filters(self):
        self.assertEqual(set(ds.paths(reconcile=True)), set(ds.paths()))
        self.assertEqual(ds.paths(release=True),
                         (".engine/self-map.md", ".engine/docs/ci-assurance.md",
                          ".engine/knowledge/graph.json"))
        # every current member regenerates at upgrade, so paths(upgrade=True) == the full roster today
        self.assertEqual(set(ds.paths(upgrade=True)), set(ds.paths()))

    def test_assurance_regenerates_before_the_graph_that_fingerprints_it(self):
        order = ds._REGEN_ORDER
        self.assertLess(order.index(".engine/docs/ci-assurance.md"),
                        order.index(".engine/knowledge/graph.json"))

    def test_every_member_is_named_in_the_regen_order_and_graph_is_last(self):
        # The topological order is a declared, tested property (not a comment). Every member must appear, and
        # the knowledge graph — which fingerprints the other surfaces — must sort last.
        for m in ds.MEMBERS:
            self.assertIn(m.path, ds._REGEN_ORDER, f"{m.path} missing from _REGEN_ORDER")
        self.assertEqual(ds._REGEN_ORDER[-1], ".engine/knowledge/graph.json")

    def test_ordered_places_graph_last_even_from_scrambled_selection(self):
        scrambled = tuple(reversed(ds.MEMBERS))
        ordered = ds._ordered(scrambled)
        self.assertEqual(ordered[-1].path, ".engine/knowledge/graph.json")

    def test_verify_returns_one_result_per_member(self):
        # repair converges only if verify and regenerate share one roster: one verify result per MEMBER.
        results = ds.verify()
        self.assertEqual([r.path for r in results], [m.path for m in ds.members()])


class TestPathsFlatten(unittest.TestCase):
    def test_single_output_members_flatten_to_their_path(self):
        # byte-identical: for every real member outputs == (path,), so paths() is unchanged.
        self.assertEqual(ds.paths(), tuple(m.outputs[0].path for m in ds.MEMBERS))

    def test_multi_output_member_flattens_every_output(self):
        with mock.patch.object(ds, "MEMBERS", (_TREE_MEMBER,)):
            self.assertEqual(ds.paths(), (".codex/agents/", ".agents/skills/"))
            # reconcile filter still flattens
            self.assertEqual(ds.paths(reconcile=True), (".codex/agents/", ".agents/skills/"))


class TestOwnerOf(unittest.TestCase):
    def test_exact_file_match(self):
        m = ds.owner_of(".engine/knowledge/graph.json")
        self.assertIsNotNone(m)
        self.assertEqual(m.path, ".engine/knowledge/graph.json")

    def test_authored_path_is_unowned(self):
        self.assertIsNone(ds.owner_of("src/app/main.py"))

    def test_exclusive_tree_prefix_matches_on_a_directory_boundary(self):
        with mock.patch.object(ds, "MEMBERS", (_TREE_MEMBER,)):
            self.assertEqual(ds.owner_of(".codex/agents/engine-audit.toml").path, ".codex/agents")
            self.assertEqual(ds.owner_of(".agents/skills/engine-x/openai.yaml").path, ".codex/agents")
            # the bare directory itself is owned
            self.assertEqual(ds.owner_of(".codex/agents").path, ".codex/agents")
            # a sibling one character past the boundary is NOT owned (no raw startswith)
            self.assertIsNone(ds.owner_of(".codex/agents-x/foo.toml"))

    def test_non_exclusive_tree_never_prefix_owns(self):
        # a mixed authored/generated directory (setup routes) must not prefix-own, so a conflict there
        # classifies authored and refuses rather than risk staging an authored file.
        with mock.patch.object(ds, "MEMBERS", (_NONEXCLUSIVE_TREE,)):
            self.assertIsNone(ds.owner_of(".claude/skills/engine-setup-x/SKILL.md"))


class TestForkGuardCore(unittest.TestCase):
    def test_fork_guard_core_is_exactly_the_three_always_present_files(self):
        # the fork-main / external-contribution guard set — derived from the flag, never a hand literal, and
        # it must EXCLUDE the optional matrix and any future reconcilable tree member.
        self.assertEqual(ds.fork_guard_core_paths(),
                         (".engine/self-map.md", ".engine/docs/ci-assurance.md",
                          ".engine/knowledge/graph.json"))

    def test_a_new_reconcilable_member_does_not_join_the_fork_guard_core(self):
        with mock.patch.object(ds, "MEMBERS", ds.MEMBERS + (_TREE_MEMBER,)):
            self.assertNotIn(".codex/agents/", ds.fork_guard_core_paths())
            self.assertNotIn(".codex/agents", ds.fork_guard_core_paths())


class TestScope(unittest.TestCase):
    """Positive-home scope: a home-only member regenerates ONLY where the write-target root's origin is
    confidently the recorded home; every unplaceable case is treated as DEPLOYED (skip), the safe direction
    when a wrong 'home' guess means a destructive regeneration."""

    def test_confirmed_home_is_in_scope(self):
        with mock.patch.object(ds.repo_identity, "origin_slug", return_value="acme/engine"), \
             mock.patch.object(ds.repo_identity, "home_repository", return_value="acme/engine"):
            self.assertTrue(ds._is_confirmed_home("/tree"))
            self.assertTrue(ds._in_scope(_HOME_MEMBER, "/tree"))

    def test_downstream_origin_is_out_of_scope(self):
        with mock.patch.object(ds.repo_identity, "origin_slug", return_value="fork/engine"), \
             mock.patch.object(ds.repo_identity, "home_repository", return_value="acme/engine"):
            self.assertFalse(ds._is_confirmed_home("/tree"))
            self.assertFalse(ds._in_scope(_HOME_MEMBER, "/tree"))

    def test_no_origin_remote_is_out_of_scope(self):
        # the fail-open case the old is_home_repo got wrong: an unreadable/absent origin must be DEPLOYED.
        with mock.patch.object(ds.repo_identity, "origin_slug", return_value=None), \
             mock.patch.object(ds.repo_identity, "home_repository", return_value="acme/engine"):
            self.assertFalse(ds._is_confirmed_home("/tree"))

    def test_malformed_manifest_is_out_of_scope(self):
        def boom(_root):
            raise ValueError("malformed engine.json")
        with mock.patch.object(ds.repo_identity, "origin_slug", return_value="acme/engine"), \
             mock.patch.object(ds.repo_identity, "home_repository", side_effect=boom):
            self.assertFalse(ds._is_confirmed_home("/tree"))

    def test_home_only_member_is_skipped_out_of_scope_on_regenerate_in_a_deployment(self):
        with mock.patch.object(ds, "MEMBERS", (_HOME_MEMBER,)), \
             mock.patch.object(ds.repo_identity, "origin_slug", return_value=None):
            results = ds.regenerate()
        self.assertEqual(results[0].status, "skipped-out-of-scope")
        self.assertFalse(results[0].changed)

    def test_home_only_member_is_skipped_out_of_scope_on_verify_in_a_deployment(self):
        with mock.patch.object(ds, "MEMBERS", (_HOME_MEMBER,)), \
             mock.patch.object(ds.repo_identity, "origin_slug", return_value=None):
            results = ds.verify()
        self.assertEqual(results[0].status, "skipped-out-of-scope")

    def test_scope_root_follows_the_write_target_not_the_process(self):
        # the decision must be judged against the tree being written into (dirname of ENGINE_DIR), which the
        # upgrade tail / arrival redirect — not this process's checkout.
        with mock.patch.object(ds.validate, "ENGINE_DIR", "/some/other/tree/.engine"):
            self.assertEqual(ds._scope_root(None), "/some/other/tree")
        self.assertEqual(ds._scope_root("/explicit"), "/explicit")


class TestPresence(unittest.TestCase):
    def test_present_requires_a_resolvable_generator_not_just_a_file(self):
        _needs_product_design(self)
        matrix = ".engine/product-spec-matrix.json"
        root = ds.validate.ROOT
        self.assertIn(matrix, ds.paths(present_root=root))
        with mock.patch.object(ds, "_resolve_generate",
                               side_effect=lambda m: None if m.path == matrix else _real_resolve(m)):
            self.assertNotIn(matrix, ds.paths(present_root=root))

    def test_tree_presence_is_a_nonempty_directory_not_isfile(self):
        with tempfile.TemporaryDirectory() as tmp:
            # a tree output present as an empty dir is NOT present; with a file it is
            os.makedirs(os.path.join(tmp, ".codex", "agents"))
            os.makedirs(os.path.join(tmp, ".agents", "skills"))
            self.assertFalse(ds._output_exists(ds.Output("tree", ".codex/agents/"), tmp))
            with open(os.path.join(tmp, ".codex", "agents", "engine-x.toml"), "w") as fh:
                fh.write("x")
            self.assertTrue(ds._output_exists(ds.Output("tree", ".codex/agents/"), tmp))
            # a file output is judged by isfile
            self.assertFalse(ds._output_exists(ds.Output("file", ".engine/self-map.md"), tmp))


class TestRootRelativeResolution(unittest.TestCase):
    def test_abspath_is_root_relative_for_any_output_location(self):
        # the fix over the old _target that dropped the first segment under .engine/: a non-.engine output
        # now resolves at its real path, and an .engine/ output is byte-identical to the old form.
        self.assertEqual(ds._abspath(".engine/self-map.md", "/r"), "/r/.engine/self-map.md")
        self.assertEqual(ds._abspath(".codex/agents/", "/r"), "/r/.codex/agents/")


class TestVerifyNormalizer(unittest.TestCase):
    """The fail-closed normalizer: a drift severity or a bare-string problem is drift; a soft finding or None
    is in-sync; an unrecognized return type is a loud error, never a false in-sync."""

    def test_dict_with_drift_severity_is_drift(self):
        self.assertEqual(ds._normalize_verify({"severity": "hard", "message": "stale"})[0], "drift")

    def test_dict_with_soft_severity_is_in_sync(self):
        # a generator's disclosed-tolerance finding (e.g. a declined module's kept route) is NOT drift.
        self.assertEqual(ds._normalize_verify({"severity": "note", "message": "declined route kept"})[0],
                         "in-sync")

    def test_list_of_bare_strings_is_drift(self):
        self.assertEqual(ds._normalize_verify(["render out of date"])[0], "drift")

    def test_list_of_soft_dicts_is_in_sync(self):
        self.assertEqual(ds._normalize_verify([{"severity": "note", "message": "ok"}])[0], "in-sync")

    def test_list_mixing_soft_and_hard_is_drift(self):
        self.assertEqual(ds._normalize_verify(
            [{"severity": "note"}, {"severity": "hard", "message": "x"}])[0], "drift")

    def test_none_and_empty_are_in_sync(self):
        self.assertEqual(ds._normalize_verify(None)[0], "in-sync")
        self.assertEqual(ds._normalize_verify([])[0], "in-sync")

    def test_unrecognized_return_type_raises(self):
        with self.assertRaises(TypeError):
            ds._normalize_verify(42)
        with self.assertRaises(TypeError):
            ds._normalize_verify([object()])

    def test_verify_records_an_unrecognized_return_as_error_not_in_sync(self):
        # end-to-end: a check returning garbage must surface as 'error' (fail-closed), never a false in-sync.
        graph = ".engine/knowledge/graph.json"
        with mock.patch.object(ds, "_resolve_check",
                               side_effect=lambda m: (lambda root, primary_target: 42)
                               if m.path == graph else _real_resolve_check(m)):
            results = {r.path: r for r in ds.verify([graph])}
        self.assertEqual(results[graph].status, "error")


class TestSingleSource(unittest.TestCase):
    def test_module_manager_regenerated_derived_is_the_upgrade_registry(self):
        import module_manager
        # E5 rebinds this to the upgrade subset; today upgrade==all four, so it still equals paths().
        self.assertEqual(tuple(module_manager.REGENERATED_DERIVED), ds.paths(upgrade=True))

    def test_pr_reconcile_members_is_the_reconcile_registry(self):
        import pr_reconcile
        self.assertEqual(tuple(pr_reconcile.MEMBERS), ds.paths(reconcile=True))


class TestRegenerate(unittest.TestCase):
    def test_a_generator_failure_is_surfaced_per_member_never_swallowed(self):
        graph = ".engine/knowledge/graph.json"

        def boom(root, primary_target):
            raise RuntimeError("generator exploded")

        def fake_resolve(member):
            return boom if member.path == graph else _real_resolve(member)

        with mock.patch.object(ds, "_resolve_generate", side_effect=fake_resolve):
            results = {r.path: r for r in ds.regenerate()}

        self.assertEqual(results[graph].status, "failed")
        self.assertIsNotNone(results[graph].error)
        self.assertIn("generator exploded", results[graph].error)
        self.assertIn(results[".engine/self-map.md"].status, ("regenerated", "unchanged"))

    def test_changed_is_truthful_from_the_output_digest(self):
        # a generator that writes nothing new reports 'unchanged'; one that mutates the file reports
        # 'regenerated' — driven by a byte digest, not a log-message heuristic.
        self_map = ".engine/self-map.md"

        def noop(root, primary_target):
            return ["some", "list", "return"]     # a list return the old heuristic could never read

        with mock.patch.object(ds, "_resolve_generate",
                               side_effect=lambda m: noop if m.path == self_map else _real_resolve(m)):
            r = {x.path: x for x in ds.regenerate([self_map])}[self_map]
        self.assertEqual(r.status, "unchanged")
        self.assertFalse(r.changed)

        def mutate(root, primary_target):
            with open(primary_target, "a") as fh:
                fh.write("\n<!-- drift -->\n")

        try:
            with mock.patch.object(ds, "_resolve_generate",
                                   side_effect=lambda m: mutate if m.path == self_map else _real_resolve(m)):
                r = {x.path: x for x in ds.regenerate([self_map])}[self_map]
            self.assertEqual(r.status, "regenerated")
            self.assertTrue(r.changed)
        finally:
            # restore the file the mutate test dirtied (E7 regenerates for real; keep the tree clean here)
            import self_map as sm
            sm.generate(path=os.path.join(ds.validate.ROOT, self_map))

    def test_absent_targets_are_skipped_not_fabricated(self):
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
        self.assertEqual(results[matrix].status, "skipped-no-generator")

    def test_unknown_dispatch_raises(self):
        with self.assertRaises(ValueError):
            ds.regenerate(dispatch="carrier-pigeon")

    def test_subprocess_dispatch_surfaces_a_raise_as_failed_never_propagates(self):
        graph = ".engine/knowledge/graph.json"
        with mock.patch.object(ds.subprocess, "run", side_effect=OSError("spawn boom")):
            results = {r.path: r for r in ds.regenerate([graph], dispatch="subprocess")}
        self.assertEqual(results[graph].status, "failed")
        self.assertIn("spawn boom", results[graph].error)


class TestRepair(unittest.TestCase):
    def test_repair_regenerates_exactly_the_drifted_members(self):
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

        self.assertEqual(regen_calls, [[drifted_path]])
        self.assertTrue(all(d.status == "in-sync" for d in a))


_REAL_RESOLVE = ds._resolve_generate
_REAL_RESOLVE_CHECK = ds._resolve_check


def _installed_module_ids() -> set:
    import module_coherence
    return {m.get("id") for _p, m in module_coherence.discover_manifests() if isinstance(m, dict)}


def _needs_product_design(case) -> None:
    if "product-design" not in _installed_module_ids():
        case.skipTest("the product-spec matrix is provided by the declined product-design module")


def _real_resolve(member):
    return _REAL_RESOLVE(member)


def _real_resolve_check(member):
    return _REAL_RESOLVE_CHECK(member)


if __name__ == "__main__":
    unittest.main()
