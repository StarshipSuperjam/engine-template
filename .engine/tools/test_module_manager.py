#!/usr/bin/env python3
"""Self-tests for slice 25b — the module manager: `remove` (refusals + manifest-derived reversal),
the group-scoped uv-sync derivation, and the reverse-dependency / required-foundation guards.

Run: uv run --directory .engine -- python -m unittest discover -s tools -p 'test_*.py'

The pure refusal policy and the derivation are tested directly (fixture manifest lists / a temp
pyproject — no disk mutation); the live `remove` mutation glue is exercised end-to-end by run_demo()
against a throwaway fixture engine (real reversal, real coherence, real re-derivation). The
deliverable-gate cold review attests each test's assertion matches its name.
"""
from __future__ import annotations
import contextlib
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import module_manager  # noqa: E402


def _m(mid, status="optional", depends=None):
    return (f".engine/modules/{mid}/manifest.json",
            {"id": mid, "version": "0.0.0", "status": status, "provides": {},
             "depends": depends or {}})


class TestRemoveRefusals(unittest.TestCase):
    """Pure refusal policy over an injected manifest list — no disk."""

    def test_absent_module_is_refused_plainly(self):
        plan = module_manager.plan_remove("nope", [_m("a")])
        self.assertTrue(plan["refused"])
        self.assertIn("no module named 'nope'", plan["reason"])

    def test_depended_on_module_is_refused_naming_the_dependent(self):
        manifests = [_m("a"), _m("b", depends={"a": ""})]
        plan = module_manager.plan_remove("a", manifests)
        self.assertTrue(plan["refused"])
        self.assertIn("'b'", plan["reason"])          # names the dependent
        self.assertIn("needs it", plan["reason"])     # singular verb agreement

    def test_two_dependents_are_both_named_with_plural_grammar(self):
        manifests = [_m("a"), _m("b", depends={"a": ""}), _m("c", depends={"a": ""})]
        plan = module_manager.plan_remove("a", manifests)
        self.assertTrue(plan["refused"])
        self.assertIn("'b'", plan["reason"])
        self.assertIn("'c'", plan["reason"])
        self.assertIn("need it", plan["reason"])      # plural verb

    def test_required_module_is_refused(self):
        plan = module_manager.plan_remove("base", [_m("base", status="required")])
        self.assertTrue(plan["refused"])
        self.assertIn("required", plan["reason"])

    def test_dependency_refusal_takes_precedence_over_required(self):
        # A module both required AND depended-on surfaces the dependency reason (the actionable one).
        manifests = [_m("base", status="required"), _m("ext", depends={"base": ""})]
        plan = module_manager.plan_remove("base", manifests)
        self.assertTrue(plan["refused"])
        self.assertIn("'ext'", plan["reason"])
        self.assertNotIn("required part", plan["reason"])

    def test_optional_with_no_dependents_is_allowed(self):
        plan = module_manager.plan_remove("a", [_m("a"), _m("b")])
        self.assertFalse(plan["refused"])
        self.assertIsNone(plan["reason"])


class TestRealRepoRefusals(unittest.TestCase):
    """The two real modules (both required) — read-only, no mutation."""

    def test_remove_core_is_refused_naming_validators_core(self):
        plan = module_manager.plan_remove("core")
        self.assertTrue(plan["refused"])
        self.assertIn("validators-core", plan["reason"])   # reverse-dependency refusal

    def test_remove_validators_core_is_refused_as_required(self):
        plan = module_manager.plan_remove("validators-core")
        self.assertTrue(plan["refused"])
        self.assertIn("required", plan["reason"])          # required-foundation refusal


class TestUvGroupDerivation(unittest.TestCase):

    def test_pep735_normalization(self):
        self.assertEqual(module_manager.normalize_pep735("a_b.c"), "a-b-c")
        self.assertEqual(module_manager.normalize_pep735("Core"), "core")
        self.assertEqual(module_manager.normalize_pep735("validators-core"), "validators-core")
        self.assertEqual(module_manager.normalize_pep735("my__group..x"), "my-group-x")

    def test_derive_matches_committed_default_groups_on_the_real_repo(self):
        # The drift gate: the committed [tool.uv] default-groups equals what the present set derives.
        self.assertEqual(module_manager.derive_uv_groups(), module_manager.committed_default_groups())
        self.assertEqual(module_manager.committed_default_groups(), ["core"])

    def test_a_module_with_no_dependency_group_is_excluded(self):
        with tempfile.TemporaryDirectory() as d:
            py = os.path.join(d, "pyproject.toml")
            with open(py, "w") as fh:
                fh.write('[dependency-groups]\na = ["x"]\n[tool.uv]\ndefault-groups = ["a"]\n')
            # present ids {a, b, c}; only `a` declares a group -> b and c contribute nothing
            manifests = [_m("a"), _m("b"), _m("c")]
            self.assertEqual(module_manager.derive_uv_groups(manifests=manifests, pyproject_path=py), ["a"])

    def test_rewrite_default_groups_is_minimal_and_validates_shape(self):
        text = ('# a comment\n[project]\nname = "x"\n\n[dependency-groups]\n'
                'base = ["p"]\noptx = ["q"]\n\n[tool.uv]\ndefault-groups = ["base", "optx"]\n')
        new, changed = module_manager.rewrite_default_groups_text(text, ["base"])
        self.assertTrue(changed)
        self.assertIn('default-groups = ["base"]', new)
        self.assertIn("[dependency-groups]", new)            # the rest is preserved byte-for-byte
        self.assertIn('optx = ["q"]', new)
        # idempotent: rewriting to the same value reports no change
        _, changed2 = module_manager.rewrite_default_groups_text(new, ["base"])
        self.assertFalse(changed2)
        # a missing, duplicated, or MULTI-LINE array fails loud (the caller fails open, never blind-writes
        # nor silently reformats the operator's pyproject)
        with self.assertRaises(ValueError):
            module_manager.rewrite_default_groups_text("[tool.uv]\n", ["x"])
        with self.assertRaises(ValueError):
            module_manager.rewrite_default_groups_text(text + 'default-groups = ["base"]\n', ["base"])
        with self.assertRaises(ValueError):
            module_manager.rewrite_default_groups_text(
                '[tool.uv]\ndefault-groups = [\n  "base",\n]\n', ["base"])


class TestRemoveEndToEnd(unittest.TestCase):
    """The live `remove` mutation glue — reversal, deletion, engine.json update, group re-derivation,
    idempotence, and post-removal coherence — exercised by the shipped fail-then-pass demo against a
    throwaway fixture engine (real logic, fixture boundary)."""

    def test_run_demo_passes(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(module_manager.run_demo())


class TestCli(unittest.TestCase):

    def test_status_exits_zero(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(module_manager.main(["status"]), 0)

    def test_plan_remove_required_exits_one(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(module_manager.main(["plan-remove", "validators-core"]), 1)

    def test_demo_exits_zero(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(module_manager.main(["demo"]), 0)


if __name__ == "__main__":
    unittest.main()
