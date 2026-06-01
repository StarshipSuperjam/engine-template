#!/usr/bin/env python3
"""Self-tests for the seed's checker-of-checkers (validator + the two guards).

Run: uv run --directory .engine -- python -m unittest discover -s tools -p 'test_*.py'

These lock in the load-bearing teeth so a later edit to the trust root cannot
silently regress them. The deliverable-gate cold review attests that each test's
assertion matches its name; CI runs them as a step in `engine-ci`.
"""
from __future__ import annotations
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402
import weakening_guard   # noqa: E402
import protection_guard  # noqa: E402

SECTIONS = ["Purpose", "Scope", "Out of scope", "Risk", "Validation", "Review",
            "Files of interest", "Claude involvement"]
COMPLETENESS_RULE = {"id": "t", "kind": "pr-body-completeness", "tier": "hard",
                     "suites": ["CI"], "params": {"sections": SECTIONS},
                     "message": "Fill the section."}


class TestCompletenessTeeth(unittest.TestCase):
    def test_placeholder_and_blank_count_as_empty(self):
        self.assertTrue(validate.is_empty_section("<why this exists>"))
        self.assertTrue(validate.is_empty_section("   \n  \n"))
        self.assertTrue(validate.is_empty_section("<a>\n\n<b>"))

    def test_real_content_is_not_empty(self):
        self.assertFalse(validate.is_empty_section("Real text."))
        self.assertFalse(validate.is_empty_section("<placeholder>\nbut also real text"))

    def test_section_blocks_parses_h2_only(self):
        blocks = validate.section_blocks("## Purpose\nx\n### Sub\ny\n## Scope\nz")
        self.assertIn("Purpose", blocks)
        self.assertIn("Scope", blocks)
        self.assertNotIn("Sub", blocks)  # a ### subsection is not a contract section

    def test_missing_body_fails_open(self):
        passed, found = validate.kind_pr_body_completeness(COMPLETENESS_RULE, {"pr_body": None})
        self.assertTrue(passed)
        self.assertTrue(all(f["severity"] != "hard" for f in found))

    def test_empty_body_flags_all_eight_hard(self):
        passed, found = validate.kind_pr_body_completeness(COMPLETENESS_RULE, {"pr_body": ""})
        self.assertFalse(passed)
        self.assertEqual(len(found), 8)
        self.assertTrue(all(f["severity"] == "hard" for f in found))

    def test_placeholder_only_body_fails(self):
        body = "\n".join(f"## {s}\n<prompt>" for s in SECTIONS)
        passed, found = validate.kind_pr_body_completeness(COMPLETENESS_RULE, {"pr_body": body})
        self.assertFalse(passed)
        self.assertEqual(len(found), 8)

    def test_filled_body_passes(self):
        body = "\n".join(f"## {s}\nreal content for {s}" for s in SECTIONS)
        passed, found = validate.kind_pr_body_completeness(COMPLETENESS_RULE, {"pr_body": body})
        self.assertTrue(passed)
        self.assertEqual(found, [])


class TestDispatcherGate(unittest.TestCase):
    """Lock in the fix: the CI exit code gates on a hard-severity finding, never on
    a callable's verdict flag, so report() and the exit code can never disagree."""
    def setUp(self):
        self._rules, self._reg = validate.load_rules, dict(validate.REGISTRY)

    def tearDown(self):
        validate.load_rules = self._rules
        validate.REGISTRY.clear()
        validate.REGISTRY.update(self._reg)

    def _install(self, kind_fn, tier="hard"):
        validate.load_rules = lambda: [{"id": "synthetic", "kind": "synthetic",
                                        "tier": tier, "suites": ["CI"], "params": {}}]
        validate.REGISTRY["synthetic"] = kind_fn

    def test_hard_finding_fails_even_when_verdict_true(self):
        self._install(lambda rule, ctx: (True, [validate.finding("hard", "boom")]))
        self.assertEqual(validate.run("CI", {"pr_body": None}), 1)

    def test_soft_finding_passes_even_when_verdict_false(self):
        self._install(lambda rule, ctx: (False, [validate.finding("soft", "note")]))
        self.assertEqual(validate.run("CI", {"pr_body": None}), 0)

    def test_unregistered_hard_kind_fails_closed(self):
        validate.load_rules = lambda: [{"id": "d", "kind": "nope", "tier": "hard",
                                        "suites": ["CI"], "params": {}}]
        self.assertEqual(validate.run("CI", {"pr_body": None}), 1)

    def test_erroring_kind_fails_closed(self):
        def boom(rule, ctx):
            raise RuntimeError("kaboom")
        self._install(boom)
        self.assertEqual(validate.run("CI", {"pr_body": None}), 1)


class TestWeakeningClassifier(unittest.TestCase):
    def test_is_guardrail_covers_guards_and_lockfiles(self):
        for p in (".github/workflows/engine-ci.yml", ".engine/check/x.json",
                  ".engine/tools/validate.py", ".github/CODEOWNERS",
                  ".engine/pyproject.toml", ".engine/uv.lock"):
            self.assertTrue(weakening_guard.is_guardrail(p), p)
        for p in ("README.md", "src/app.py", ".gitignore"):
            self.assertFalse(weakening_guard.is_guardrail(p), p)

    def test_copied_status_is_caught(self):
        self.assertIn("copied", weakening_guard.WEAKENING_STATUS)
        flagged = weakening_guard.flagged_changes(
            [{"filename": ".github/workflows/x.yml", "status": "copied"}])
        self.assertEqual(len(flagged), 1)

    def test_removed_renamed_and_modified_lock_are_flagged(self):
        files = [
            {"filename": ".engine/tools/validate.py", "status": "removed"},
            {"filename": ".github/workflows/new.yml", "status": "renamed",
             "previous_filename": ".github/workflows/engine-ci.yml"},
            {"filename": ".engine/uv.lock", "status": "modified"},
        ]
        self.assertEqual(len(weakening_guard.flagged_changes(files)), 3)

    def test_addition_and_nonguardrail_not_flagged(self):
        files = [
            {"filename": ".github/workflows/new.yml", "status": "added"},
            {"filename": "README.md", "status": "modified"},
        ]
        self.assertEqual(weakening_guard.flagged_changes(files), [])


class TestProtectionFloor(unittest.TestCase):
    CHECKS = ["engine-ci", "engine-guard"]

    def _full(self):
        return [
            {"type": "pull_request", "parameters": {
                "required_review_thread_resolution": True, "required_approving_review_count": 0}},
            {"type": "required_status_checks", "parameters": {
                "required_status_checks": [{"context": "engine-ci"}, {"context": "engine-guard"}]}},
            {"type": "non_fast_forward", "parameters": {}},
            {"type": "deletion", "parameters": {}},
        ]

    def test_full_floor_has_nothing_missing(self):
        self.assertEqual(protection_guard.missing_floor(self._full(), self.CHECKS), [])

    def test_empty_rules_flags_every_floor_piece(self):
        missing = protection_guard.missing_floor([], self.CHECKS)
        self.assertTrue(any("pull request" in m for m in missing))
        self.assertTrue(any("status checks" in m for m in missing))
        self.assertTrue(any("force-push" in m for m in missing))
        self.assertTrue(any("deletion" in m for m in missing))

    def test_unbound_required_check_is_flagged(self):
        rules = self._full()
        rules[1]["parameters"]["required_status_checks"] = [{"context": "engine-ci"}]
        missing = protection_guard.missing_floor(rules, self.CHECKS)
        self.assertTrue(any("engine-guard" in m for m in missing))

    def test_conversation_resolution_required(self):
        rules = self._full()
        rules[0]["parameters"]["required_review_thread_resolution"] = False
        missing = protection_guard.missing_floor(rules, self.CHECKS)
        self.assertTrue(any("conversations" in m for m in missing))


class TestDecoratedScaffold(unittest.TestCase):
    """The visible-scaffold template: decorated placeholder slots still read as
    unfilled, real content reads as filled, and an inline <token> in real text is
    not mistaken for a placeholder (the over-strip guard)."""

    def test_decorated_placeholder_lines_are_empty(self):
        for line in ("**<summary>**", "- <detail>", "*<Impact: why>*",
                     "<bare>", "__<x>__", "  - <y>  "):
            self.assertTrue(validate.is_empty_section(line), line)

    def test_real_content_lines_are_not_empty(self):
        for line in ("**Real bold summary**", "- a real detail", "*Impact: real text*",
                     "Uses the <head> ref here.", "- text with <token> inside"):
            self.assertFalse(validate.is_empty_section(line), line)

    def test_decorated_section_with_one_real_line_is_not_empty(self):
        self.assertFalse(validate.is_empty_section("**<summary>**\n- a real bullet\n*<Impact: x>*"))

    def test_committed_template_body_fails_completeness(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        with open(os.path.join(root, ".github", "pull_request_template.md"), encoding="utf-8") as fh:
            tmpl = fh.read()
        passed, found = validate.kind_pr_body_completeness(COMPLETENESS_RULE, {"pr_body": tmpl})
        self.assertFalse(passed)
        self.assertEqual(len(found), len(SECTIONS))  # every section unfilled

    def test_filled_scaffold_passes(self):
        body = "\n".join(
            f"## {s}\n**Real summary for {s}**\n- a real bullet\n*Impact: real impact*"
            for s in SECTIONS)
        passed, found = validate.kind_pr_body_completeness(COMPLETENESS_RULE, {"pr_body": body})
        self.assertTrue(passed)
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
