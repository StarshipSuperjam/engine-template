#!/usr/bin/env python3
"""Regression tests for the dependency-review gate (.engine/tools/dependency_discipline/review.py)."""
from __future__ import annotations
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # .engine/tools on sys.path
from dependency_discipline import review  # noqa: E402
import validate  # noqa: E402


_ADVISORY = {"severity": "high", "advisory_ghsa_id": "GHSA-test-1234",
             "advisory_summary": "Remote code execution",
             "advisory_url": "https://github.com/advisories/GHSA-test-1234"}
_VULN_CHANGE = {"change_type": "added", "manifest": "package.json", "ecosystem": "npm",
                "name": "demo-pkg", "version": "1.0.0", "vulnerabilities": [_ADVISORY]}
_CLEAN_CHANGE = {"change_type": "added", "manifest": "package.json", "name": "ok",
                 "version": "1.0.0", "vulnerabilities": []}


class _Canned:
    """A stand-in client: `compare` returns a canned change list or raises a canned exception, and records
    whether it was called (so a test can assert the client is never constructed/used on the no-PR path)."""

    def __init__(self, outcome):
        self._outcome = outcome
        self.called = False

    def compare(self, base, head):
        self.called = True
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


class ReviewGateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="engine-depreview-test-")
        self.addCleanup(shutil.rmtree, self._tmp, True)
        self._event = os.path.join(self._tmp, "event.json")
        self._write_event({"pull_request": {"base": {"sha": "aaa"}, "head": {"sha": "bbb"}}})

    def _write_event(self, payload, *, raw: "str | None" = None):
        with open(self._event, "w", encoding="utf-8") as fh:
            fh.write(raw if raw is not None else json.dumps(payload))

    def _find(self, client):
        return review.findings("hard", event_path=self._event, repo="o/r", token="t", client=client)

    def _severities(self, fs):
        return {f["severity"] for f in fs}

    # --- the hard vulnerability block -------------------------------------------------------------
    def test_vulnerable_added_product_dependency_blocks_hard(self):
        fs = self._find(_Canned([_VULN_CHANGE]))
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "hard")
        self.assertIn("GHSA-test-1234", fs[0]["message"])
        self.assertIn("demo-pkg", fs[0]["message"])
        self.assertEqual(fs[0]["location"], {"file": "package.json", "line": None})

    def test_hard_block_names_the_deliberate_decision_escape(self):
        # the operator must never read the block as a permanent dead end (the non-stranding obligation)
        msg = self._find(_Canned([_VULN_CHANGE]))[0]["message"]
        self.assertIn("the decision to proceed is yours", msg)

    # --- the §13 wall: the engine's own .engine/ tooling is never a product dependency -------------
    def test_engine_manifest_vulnerability_is_walled_off(self):
        engine_vuln = dict(_VULN_CHANGE, manifest=".engine/pyproject.toml")
        self.assertEqual(self._find(_Canned([engine_vuln])), [],
                         "a vulnerability under .engine/ must not block — it is the engine's own tooling")

    # --- a clean comparison passes cleanly --------------------------------------------------------
    def test_clean_comparison_passes(self):
        self.assertEqual(self._find(_Canned([_CLEAN_CHANGE])), [])

    def test_removed_dependency_never_blocks(self):
        removed = dict(_VULN_CHANGE, change_type="removed")
        self.assertEqual(self._find(_Canned([removed])), [],
                         "removing a dependency is not something the PR brings in — never a block")

    def test_version_bump_reports_only_the_added_side_once(self):
        removed_old = {"change_type": "removed", "manifest": "package.json", "name": "demo-pkg",
                       "version": "0.9.0", "vulnerabilities": [_ADVISORY]}
        fs = self._find(_Canned([removed_old, _VULN_CHANGE]))
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "hard")

    def test_nested_product_manifest_still_blocks(self):
        nested = dict(_VULN_CHANGE, manifest="frontend/package.json")
        fs = self._find(_Canned([nested]))
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["location"]["file"], "frontend/package.json")

    # --- the disclosed branches: soft, passing, never [] ------------------------------------------
    def test_unavailable_tier_discloses_cost_and_passes_soft(self):
        fs = self._find(_Canned(review._Unavailable()))
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "soft")
        self.assertIn("GitHub Code Security", fs[0]["message"])
        self.assertIn("$30", fs[0]["message"])

    def test_degraded_read_discloses_transient_and_names_no_cost(self):
        fs = self._find(_Canned(review.DegradedReadError("boom")))
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "soft")
        self.assertIn("temporary", fs[0]["message"].lower())
        self.assertNotIn("$30", fs[0]["message"], "a transient read failure must not mention a price")
        self.assertNotIn("Code Security", fs[0]["message"])

    def test_no_pull_request_context_discloses_soft_no_op_without_using_client(self):
        missing = os.path.join(self._tmp, "missing.json")  # never created
        client = _Canned(RuntimeError("must not be called when there is no PR"))
        fs = review.findings("hard", event_path=missing, repo="o/r", token="t", client=client)
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "soft")
        self.assertIn("nothing for it to review", fs[0]["message"])
        self.assertFalse(client.called, "the client must not be constructed/used when there's no PR to compare")

    def test_corrupt_event_degrades_to_soft_no_op_not_fail_closed(self):
        # a present-but-unparseable event must NOT crash (which would hit the validator's fail-closed hard path)
        self._write_event(None, raw="{ this is not json")
        client = _Canned(RuntimeError("must not be called on a corrupt event"))
        fs = review.findings("hard", event_path=self._event, repo="o/r", token="t", client=client)
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "soft")
        self.assertFalse(client.called)

    def test_missing_token_or_repo_discloses_soft_no_op_without_using_client(self):
        client = _Canned(RuntimeError("must not be called without a token"))
        fs = review.findings("hard", event_path=self._event, repo="o/r", token=None, client=client)
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "soft")
        self.assertFalse(client.called)

    # --- no handled branch is ever hard except the real vulnerability block, and none ever raises -
    def test_disclosure_branches_are_never_hard(self):
        for client in (_Canned(review._Unavailable()), _Canned(review.DegradedReadError("x")),
                       _Canned([_CLEAN_CHANGE])):
            self.assertNotIn("hard", self._severities(self._find(client)))

    # --- the compare() client maps statuses correctly (over a fake transport) ---------------------
    def test_compare_maps_403_to_unavailable(self):
        calls = []

        def transport(method, path, body):
            calls.append((method, path))
            return 403, None
        client = review.DependencyReview("o/r", "t", transport=transport)
        with self.assertRaises(review._Unavailable):
            client.compare("aaa", "bbb")
        self.assertIn("dependency-graph/compare/aaa...bbb", calls[0][1])

    def test_compare_maps_404_and_5xx_to_degraded(self):
        for status in (404, 500, 503):
            client = review.DependencyReview("o/r", "t", transport=lambda m, p, b, s=status: (s, None))
            with self.assertRaises(review.DegradedReadError):
                client.compare("aaa", "bbb")

    def test_compare_returns_the_change_list_on_200(self):
        client = review.DependencyReview("o/r", "t", transport=lambda m, p, b: (200, [_VULN_CHANGE]))
        self.assertEqual(client.compare("aaa", "bbb"), [_VULN_CHANGE])

    # --- the custom/script contract ---------------------------------------------------------------
    def test_emit_findings_prints_a_json_array_and_returns_zero(self):
        # in the test environment there is no PR event / token, so this exercises the soft no-op contract
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = review.emit_findings()
        self.assertEqual(rc, 0)
        parsed = json.loads(buf.getvalue())
        self.assertIsInstance(parsed, list)
        for f in parsed:
            self.assertIn("severity", f)
            self.assertIn("message", f)
            self.assertIn("location", f)

    def test_main_routes_demo_and_bare_invocation(self):
        self.assertEqual(review.main(["demo"]), 0)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(review.main([]), 0)
        self.assertIsInstance(json.loads(buf.getvalue()), list)

    def test_demo_passes(self):
        self.assertEqual(review.demo(), 0)

    # --- the rule json declares the contract the script depends on --------------------------------
    def test_rule_json_declares_hard_pass_token_and_suites(self):
        path = os.path.join(validate.ROOT, ".engine/check/dependency-review.json")
        with open(path, "r", encoding="utf-8") as fh:
            rule = json.load(fh)
        self.assertEqual(rule["tier"], "hard")
        self.assertEqual(rule["kind"], "custom/script")
        self.assertTrue(rule["params"].get("pass_token"), "the gate needs the token to read the API")
        self.assertEqual(rule["params"]["script"], ".engine/tools/dependency_discipline/review.py")
        self.assertEqual(sorted(rule["suites"]), ["CI", "pre-close", "pre-commit"])


if __name__ == "__main__":
    unittest.main()
