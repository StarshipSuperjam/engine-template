"""Tests for `selftest_support.py`'s two env-var markers and the pure predicate built on them.

These cases must run everywhere — home repo, nested run, or projected deployed tree — so none of them is
gated on `selftest_support.CONSTRUCTION`: gating this module on the very predicate it verifies would let a
broken predicate hide by skipping its own test. `shape_verdict` is exercised as a pure function with an
injected `is_home` stub, never by reloading the module or mutating `os.environ`, so the case stays hermetic
regardless of which repo or environment it happens to run in.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import release_gate       # noqa: E402
import selftest           # noqa: E402
import selftest_support   # noqa: E402


_HOME = "/dummy/home/repo"
_FOREIGN = "/dummy/foreign/repo"


class TestShapeVerdict(unittest.TestCase):
    def test_home_repo_bare_environ_is_true(self):
        self.assertTrue(selftest_support.shape_verdict(_HOME, {}, is_home=lambda root: root == _HOME))

    def test_home_repo_with_nested_marker_is_true(self):
        # NESTED_ENV is a recursion guard only — it must never affect the verdict.
        environ = {selftest_support.NESTED_ENV: "1"}
        self.assertTrue(selftest_support.shape_verdict(_HOME, environ, is_home=lambda root: root == _HOME))

    def test_home_repo_with_projection_marker_is_false(self):
        environ = {selftest_support.PROJECTION_ENV: "1"}
        self.assertFalse(selftest_support.shape_verdict(_HOME, environ, is_home=lambda root: root == _HOME))

    def test_foreign_repo_bare_environ_is_false(self):
        self.assertFalse(selftest_support.shape_verdict(_FOREIGN, {}, is_home=lambda root: root == _HOME))


class TestMarkerNamesArePinned(unittest.TestCase):
    def test_nested_env_is_pinned_across_its_three_homes(self):
        self.assertEqual(selftest_support.NESTED_ENV, selftest._NESTED_ENV)
        self.assertEqual(selftest_support.NESTED_ENV, release_gate._NESTED_ENV)

    def test_projection_env_is_pinned_between_support_and_release_gate(self):
        self.assertEqual(selftest_support.PROJECTION_ENV, release_gate._PROJECTION_ENV)


class TestNestedEnvSetsBothMarkers(unittest.TestCase):
    def test_nested_env_helper_carries_both_markers(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            env = release_gate._nested_env()
        self.assertEqual(env.get(selftest_support.NESTED_ENV), "1")
        self.assertEqual(env.get(selftest_support.PROJECTION_ENV), "1")


class TestReviewFixture(unittest.TestCase):
    def test_absent_and_invalid_personas_still_refuse_registration(self):
        import plan_store
        import scoped_agents
        import project_manager
        root = selftest_support.review_fixture(self)
        self.assertEqual(len(project_manager.installed_lenses()), 4)
        library = plan_store.PlanLibrary(root / "plans")
        library._mkdir(library.plan_dir("fixture"))
        store = scoped_agents.Store(library, "fixture")
        packet = root / "packet.md"
        packet.write_text("Fixture obligations\n")
        role = "engine-design-review-architecture"
        persona = root / ".claude" / "agents" / (role + ".md")
        args = dict(owner={"plan": "fixture"}, root="test-root", purpose="review",
                    lens="architecture", role=role, packet=packet, packet_digest="fixture-digest")
        self.assertEqual(store.register(**args)["role"], role)
        persona.unlink()
        with self.assertRaisesRegex(scoped_agents.EvidenceError, "persona is missing"):
            store.register(**args)
        persona.write_text("---\nrole: plan-review\noutput-contract: unknown.v1\n---\n")
        import result_contracts
        with self.assertRaises(result_contracts.Rejection):
            store.register(**args)


class TestAcceptedHookFixture(unittest.TestCase):
    def test_deployed_event_key_order_is_normalized_but_home_and_unknown_drift_stay_strict(self):
        import json
        import tempfile
        from pathlib import Path
        source = Path(selftest_support.__file__).resolve().parents[2]
        for rel in (".claude/settings.json", ".codex/hooks.json"):
            with self.subTest(rel=rel), tempfile.TemporaryDirectory() as directory:
                approved = selftest_support.accepted_hook_fixture_bytes(source, rel)
                document = json.loads(approved)
                document["hooks"]["PreCompact"] = document["hooks"].pop("PreCompact")
                root = Path(directory)
                (root / rel).parent.mkdir()
                (root / rel).write_text(json.dumps(document, indent=2) + "\n")
                with mock.patch.object(selftest_support, "CONSTRUCTION", False):
                    self.assertEqual(selftest_support.accepted_hook_fixture_bytes(root, rel), approved)
                with mock.patch.object(selftest_support, "CONSTRUCTION", True):
                    with self.assertRaisesRegex(AssertionError, "pinned approved hook generation"):
                        selftest_support.accepted_hook_fixture_bytes(root, rel)
                document["hooks"]["UnknownEvent"] = []
                (root / rel).write_text(json.dumps(document, indent=2) + "\n")
                with mock.patch.object(selftest_support, "CONSTRUCTION", False):
                    with self.assertRaisesRegex(AssertionError, "pinned approved hook generation"):
                        selftest_support.accepted_hook_fixture_bytes(root, rel)

    def test_a_missing_core_hook_is_not_restored_or_re_pinned(self):
        import json
        import tempfile
        from pathlib import Path
        source = Path(selftest_support.__file__).resolve().parents[2]
        rel = ".claude/settings.json"
        approved = selftest_support.accepted_hook_fixture_bytes(source, rel)
        document = json.loads(approved)
        document["hooks"]["SessionStart"][0]["hooks"].pop(0)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".claude").mkdir()
            (root / rel).write_text(json.dumps(document, indent=2) + "\n")
            with self.assertRaisesRegex(AssertionError, "pinned approved hook generation"):
                selftest_support.accepted_hook_fixture_bytes(root, rel)


if __name__ == "__main__":
    unittest.main()
