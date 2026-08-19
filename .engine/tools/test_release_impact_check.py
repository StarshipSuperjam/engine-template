"""Tests for release_impact_check — the hard CI check that every pull request declares exactly one valid impact."""
from __future__ import annotations

import json
import os
import unittest

import release_impact
import release_impact_check as chk


class ReleaseImpactCheck(unittest.TestCase):
    def _run(self, body):
        orig = chk._read_pr_body
        chk._read_pr_body = lambda: body
        try:
            return chk.findings()
        finally:
            chk._read_pr_body = orig

    def test_valid_marker_passes(self):
        self.assertEqual(self._run("purpose\n<!-- engine-release-impact: minor -->"), [])

    def test_all_four_values_accepted(self):
        for v in release_impact.RELEASE_IMPACTS:
            self.assertEqual(self._run(f"body\n<!-- engine-release-impact: {v} -->"), [], v)

    def test_missing_marker_hard_fails(self):
        f = self._run("a body with no marker")
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["severity"], "hard")
        self.assertNotIn("not_applicable", f[0])          # a real block in CI, not a disclosed no-op

    def test_duplicate_markers_hard_fail(self):
        f = self._run("<!-- engine-release-impact: patch -->\nx\n<!-- engine-release-impact: minor -->")
        self.assertEqual(f[0]["severity"], "hard")
        self.assertIn("exactly one", f[0]["message"].lower())

    def test_invalid_value_hard_fails(self):
        f = self._run("<!-- engine-release-impact: huge -->")
        self.assertEqual(f[0]["severity"], "hard")
        self.assertIn("not one of", f[0]["message"].lower())

    def test_no_body_is_disclosed_no_op_not_a_hard_block(self):
        f = self._run(None)
        self.assertTrue(f[0].get("not_applicable"))       # local rehearsal: never a fail-closed wall

    def test_unreadable_event_is_disclosed_no_op(self):
        orig = chk._read_pr_body

        def boom():
            raise RuntimeError("a distinctive parse error")
        chk._read_pr_body = boom
        try:
            f = chk.findings()
        finally:
            chk._read_pr_body = orig
        self.assertTrue(f[0].get("not_applicable"))
        # the diagnostic (type + message) must be surfaced, not masked — so a real future bug is visible in CI.
        self.assertIn("RuntimeError", f[0]["message"])
        self.assertIn("a distinctive parse error", f[0]["message"])

    def test_main_emits_json_array(self):
        import io
        import contextlib
        orig = chk._read_pr_body
        chk._read_pr_body = lambda: "<!-- engine-release-impact: patch -->"
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                rc = chk.main()
        finally:
            chk._read_pr_body = orig
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue()), [])


class ExemptAuthorsSingleSource(unittest.TestCase):
    def test_check_json_exempt_matches_the_leaf(self):
        # arch/feas: the exempt-author set has ONE Python home (release_impact.EXEMPT_AUTHORS). The check json's
        # ci_author_exempt is bound equal to it here so the CI check and the cut-time fold cannot drift.
        here = os.path.dirname(os.path.abspath(chk.__file__))
        check_path = os.path.join(here, "..", "check", "pr-release-impact.json")
        with open(check_path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(tuple(data["ci_author_exempt"]), release_impact.EXEMPT_AUTHORS)


if __name__ == "__main__":
    unittest.main()
