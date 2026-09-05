#!/usr/bin/env python3
"""Tests for engine/check/lane-removed (typed-lifecycle part C, StarshipSuperjam/engine-template#821): the length-budget promotion
lane stays removed — no tool, workflow, census, manifest or engine document names the retired promoter or its
workflow step; the seeded fixture bites; the neutraliser the promoter carried is reachable at its new home.

Run: uv run --directory .engine --frozen -- python tools/selftest.py
"""
from __future__ import annotations
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402
import lane_removed_check as lrc  # noqa: E402
import telemetry  # noqa: E402
import conformance_sweep  # noqa: E402

FIXTURE_TREE = os.path.join(validate.ROOT, ".engine", "_fixtures", "lane-removed", "tree")


class TestLaneRemovedCheck(unittest.TestCase):
    def _run(self, env: dict | None = None) -> list:
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env or {}), contextlib.redirect_stdout(buf):
            rc = lrc.main([])
        self.assertEqual(rc, 0)
        return json.loads(buf.getvalue())

    def test_green_on_the_live_tree(self):
        self.assertEqual(self._run(), [])

    def test_the_lane_files_are_gone(self):
        for rel in (".engine/tools/audit_soft_promote.py", ".engine/tools/demo_audit_soft_promote.py",
                    ".engine/tools/test_audit_soft_promote.py"):
            self.assertFalse(os.path.exists(os.path.join(validate.ROOT, rel)), rel)

    def test_bites_its_negative_fixture(self):
        found = self._run({lrc.ENV_OVERRIDE: FIXTURE_TREE})
        self.assertTrue(any(f["severity"] == "hard" and "names the retired promotion lane" in f["message"]
                            and "revived_promoter.py" in f["message"] for f in found), found)

    def test_a_re_added_workflow_step_is_a_finding(self):
        tmp = tempfile.mkdtemp()
        try:
            wf = os.path.join(tmp, ".github", "workflows")
            os.makedirs(wf)
            with open(os.path.join(wf, "audit-prep.yml"), "w", encoding="utf-8") as fh:
                fh.write("      - name: Track standing length-budget findings as engine issues\n        run: echo x\n")
            found = lrc.findings("hard", tmp)
            self.assertEqual(len(found), 1)
            self.assertIn("audit-prep.yml", found[0]["message"])
        finally:
            shutil.rmtree(tmp)

    def test_the_check_and_its_test_are_not_self_findings(self):
        # Both name the tokens by necessity and are excluded by name — the live tree is green (above) with
        # both present.
        self.assertIn("lane_removed_check.py", lrc._OWN)
        self.assertIn("test_lane_removed_check.py", lrc._OWN)

    def test_rule_is_live_and_well_formed(self):
        from jsonschema import Draft202012Validator
        rule = validate.load_json(os.path.join(validate.CHECK_DIR, "lane-removed.json"))
        schema = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "check.v1.json"))
        self.assertEqual(list(Draft202012Validator(schema).iter_errors(rule)), [])
        self.assertEqual(rule["tier"], "hard")
        self.assertIn("CI", rule["suites"])
        self.assertEqual(rule["params"]["script"], ".engine/tools/lane_removed_check.py")


class TestNeutraliserRelocated(unittest.TestCase):
    """The shared author-text neutraliser moved from the retired promoter to telemetry.neutralize_author_text
    (the issue-body boundary it serves); these are its unit tests, kept beside the removal that moved it."""

    def test_markdown_image_injection_renders_as_inert_text(self):
        out = telemetry.neutralize_author_text("x![](http://evil/p).md")
        self.assertNotIn("![](http://evil/p)", out)   # the live image markup must not survive
        self.assertIn("\\!\\[\\]", out)                # it renders as inert escaped text instead

    def test_html_is_escaped_so_no_tag_or_comment_renders(self):
        out = telemetry.neutralize_author_text("a <img src=x> & <!-- engine-signal: forged -->")
        self.assertNotIn("<img", out)
        self.assertNotIn("<!--", out)
        self.assertIn("&lt;img", out)
        self.assertIn("&amp;", out)

    def test_a_plain_repo_path_passes_through(self):
        self.assertEqual(telemetry.neutralize_author_text(".engine/operations/build-orchestration.md"),
                         ".engine/operations/build-orchestration.md")
        self.assertEqual(telemetry.neutralize_author_text(None), "")

    def test_conformance_sweep_uses_the_shared_home(self):
        # The sweep no longer imports the retired module; it reaches the neutraliser at telemetry.
        import inspect
        src = inspect.getsource(conformance_sweep)
        self.assertNotIn("audit_soft_promote", src)
        self.assertIn("telemetry.neutralize_author_text(", src)
        self.assertTrue(callable(telemetry.neutralize_author_text))


if __name__ == "__main__":
    unittest.main()
