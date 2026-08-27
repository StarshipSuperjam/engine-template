#!/usr/bin/env python3
"""Module add and remove as typed transactions: ungated by ceremony, clean in the tree.

These prove the operator's ruling holds in code — the one-turn flow survives, and what changed is that
the apply is now a discrete labelled commit instead of uncommitted sprawl.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import module_manager  # noqa: E402
import transaction  # noqa: E402
import transaction_adapters_module as adapters  # noqa: E402
import transaction_envelope as te  # noqa: E402
import transaction_handoff as handoff  # noqa: E402


class Args:
    def __init__(self, module_id="design-review"):
        self.operation = "module-add"
        self.rest = [module_id] if module_id else []
        self.json = False
        self.consent_handle = ""


class TestTheHandoffIsNotAPullRequest(unittest.TestCase):
    """The operator's ruling: adoption is not gated by ceremony."""

    def test_adding_a_module_hands_off_as_an_in_tree_commit(self):
        adapter = adapters.AddModule()
        handoff_result = adapter.handoff(Args(), {"module_id": "design-review", "committed": "abc1234"}, [])
        self.assertEqual(handoff_result["kind"], "in-tree-commit")
        self.assertNotEqual(handoff_result["kind"], "pull-request")
        self.assertIn("reverting that commit", handoff_result["summary"])

    def test_removing_a_module_hands_off_as_an_in_tree_commit(self):
        adapter = adapters.RemoveModule()
        handoff_result = adapter.handoff(Args(), {"module_id": "design-review", "committed": "def5678"}, [])
        self.assertEqual(handoff_result["kind"], "in-tree-commit")

    def test_neither_verb_is_operator_typed_only(self):
        """The one-turn offer flow depends on the model being able to run these after a yes."""
        self.assertNotIn("module-add", transaction._OPERATOR_TYPED_ONLY)
        self.assertNotIn("module-remove", transaction._OPERATOR_TYPED_ONLY)


class TestPlanningIsReadOnlyAndDefersToTheDomain(unittest.TestCase):
    def test_the_plan_phase_calls_the_domain_preview_rather_than_re_deriving_rules(self):
        adapter = adapters.AddModule()
        with mock.patch.object(module_manager, "preview_add",
                               return_value={"refused": False, "version": "1.2.3",
                                             "would_provide": [".engine/tools/x.py"], "notes": []}) as preview:
            plan = adapter.plan(Args(), {"fingerprints": {}})
        preview.assert_called_once()
        self.assertIn("1.2.3", " ".join(plan["consequences"]))
        self.assertIn(".engine/tools/x.py",
                      [p for effect in plan["effects"] for p in (effect.get("paths") or [])])

    def test_a_domain_refusal_becomes_a_typed_refusal_with_a_way_forward(self):
        adapter = adapters.AddModule()
        with mock.patch.object(module_manager, "preview_add",
                               return_value={"refused": True, "reason": "'x' is already installed."}):
            with self.assertRaises(transaction.TransactionRefused) as caught:
                adapter.plan(Args(), {"fingerprints": {}})
        self.assertEqual(caught.exception.code, "add-refused")
        self.assertIn("already installed", caught.exception.explanation)
        self.assertTrue(caught.exception.next_actions)

    def test_stubbing_the_domain_changes_the_plan(self):
        """Deference, made checkable: the adapter reports what the domain said, not its own opinion."""
        adapter = adapters.AddModule()
        with mock.patch.object(module_manager, "preview_add",
                               return_value={"refused": False, "version": "1.0.0",
                                             "would_provide": [], "notes": ["a note from the domain"]}):
            first = adapter.plan(Args(), {"fingerprints": {}})
        with mock.patch.object(module_manager, "preview_add",
                               return_value={"refused": False, "version": "2.0.0",
                                             "would_provide": [], "notes": ["a different note"]}):
            second = adapter.plan(Args(), {"fingerprints": {}})
        self.assertNotEqual(first["consequences"], second["consequences"])

    def test_a_missing_module_id_refuses_before_anything_else(self):
        adapter = adapters.AddModule()
        with self.assertRaises(transaction.TransactionRefused) as caught:
            adapter.plan(Args(module_id=None), {"fingerprints": {}})
        self.assertEqual(caught.exception.code, "module-id-missing")


class TestVerifyReportsHonestly(unittest.TestCase):
    def test_a_coherence_check_that_did_not_report_is_unavailable_never_passed(self):
        receipts = adapters.AddModule().verify(Args(), {"module_id": "x"})
        self.assertEqual(receipts[0]["result"], "unavailable")
        self.assertIn("unverified", receipts[0]["detail"])

    def test_hard_findings_render_as_failed(self):
        receipts = adapters.AddModule().verify(
            Args(), {"module_id": "x", "findings": [{"severity": "hard", "message": "a wire is missing"}]})
        self.assertEqual(receipts[0]["result"], "failed")
        self.assertIn("a wire is missing", receipts[0]["detail"])

    def test_no_findings_renders_as_passed(self):
        receipts = adapters.AddModule().verify(Args(), {"module_id": "x", "findings": []})
        self.assertEqual(receipts[0]["result"], "passed")


class TestTheApplyIsCleanNotSprawling(unittest.TestCase):
    """What actually changes versus today: the file set lands as one revertable commit."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=self.root, check=True)
        os.makedirs(os.path.join(self.root, ".engine"), exist_ok=True)
        with open(os.path.join(self.root, ".engine", "engine.json"), "w", encoding="utf-8") as fh:
            fh.write("{}\n")
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=self.root, check=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_operator_s_own_work_is_never_swept_into_the_engine_s_commit(self):
        with open(os.path.join(self.root, "my_work.py"), "w", encoding="utf-8") as fh:
            fh.write("# half-finished\n")
        module_dir = os.path.join(self.root, ".engine", "modules", "design-review")
        os.makedirs(module_dir, exist_ok=True)
        with open(os.path.join(module_dir, "manifest.json"), "w", encoding="utf-8") as fh:
            fh.write('{"id": "design-review"}\n')
        result = handoff.commit_in_tree([".engine/modules/design-review/manifest.json"],
                                        "Add the design-review module", root=self.root)
        self.assertTrue(result["committed"])
        listed = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"],
                                cwd=self.root, capture_output=True, text=True).stdout.split()
        self.assertEqual(listed, [".engine/modules/design-review/manifest.json"])
        status = subprocess.run(["git", "status", "--porcelain"], cwd=self.root,
                                capture_output=True, text=True).stdout
        self.assertIn("my_work.py", status, "the operator's own work stays exactly where they left it")


class TestStaysOnTheArrivalFloor(unittest.TestCase):
    def test_standard_library_only_with_the_future_import(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "transaction_adapters_module.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("from __future__ import annotations", source)
        self.assertNotIn("import tomllib", source)


if __name__ == "__main__":
    unittest.main()
