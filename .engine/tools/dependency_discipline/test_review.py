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
# product changes carry a permissive license so the (new) license rule sees them as clean and only the
# vulnerability rule fires — the real dependency-review API returns a license field on every change.
_VULN_CHANGE = {"change_type": "added", "manifest": "package.json", "ecosystem": "npm",
                "name": "demo-pkg", "version": "1.0.0", "license": "MIT", "vulnerabilities": [_ADVISORY]}
_CLEAN_CHANGE = {"change_type": "added", "manifest": "package.json", "name": "ok",
                 "version": "1.0.0", "license": "MIT", "vulnerabilities": []}
_COPYLEFT_CHANGE = {"change_type": "added", "manifest": "package.json", "name": "gpl-pkg",
                    "version": "2.0.0", "license": "GPL-3.0-or-later", "vulnerabilities": []}
_UNKNOWN_LICENSE_CHANGE = {"change_type": "added", "manifest": "package.json", "name": "mystery-pkg",
                           "version": "1.0.0", "license": "NOASSERTION", "vulnerabilities": []}


class _Canned:
    """A stand-in client: `compare`/`visibility` return a canned value or raise a canned exception, and records
    whether `compare` was called (so a test can assert the client is never used on the no-PR path)."""

    def __init__(self, outcome, visibility="public"):
        self._outcome = outcome
        self._visibility = visibility
        self.called = False

    def compare(self, base, head):
        self.called = True
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome

    def visibility(self):
        if isinstance(self._visibility, Exception):
            raise self._visibility
        return self._visibility


class ReviewGateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="engine-depreview-test-")
        self.addCleanup(shutil.rmtree, self._tmp, True)
        self._event = os.path.join(self._tmp, "event.json")
        self._write_event({"pull_request": {"base": {"sha": "aaa"}, "head": {"sha": "bbb"}}})

    def _write_event(self, payload, *, raw: "str | None" = None):
        with open(self._event, "w", encoding="utf-8") as fh:
            fh.write(raw if raw is not None else json.dumps(payload))

    def _find(self, client, *, allow_ghsas=(), allow_licenses=()):
        # always pass explicit (empty) allow-lists so a test never depends on the committed check file's params
        return review.findings("hard", event_path=self._event, repo="o/r", token="t", client=client,
                               allow_ghsas=list(allow_ghsas), allow_licenses=list(allow_licenses))

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
        self.assertIn("accepted-exceptions list", msg)  # the now-live accept-path, via the AI-remediation offer

    # --- the §13 wall: the engine's own .engine/ tooling is never a product dependency -------------
    def test_engine_manifest_vulnerability_is_walled_off(self):
        engine_vuln = dict(_VULN_CHANGE, manifest=".engine/pyproject.toml")
        self.assertEqual(self._find(_Canned([engine_vuln])), [],
                         "a vulnerability under .engine/ must not block — it is the engine's own tooling")

    def test_engine_manifest_copyleft_is_walled_off(self):
        engine_copyleft = dict(_COPYLEFT_CHANGE, manifest=".engine/pyproject.toml")
        self.assertEqual(self._find(_Canned([engine_copyleft], visibility="private")), [],
                         "a copyleft license under .engine/ must not block — it is the engine's own tooling")

    # --- a clean comparison passes cleanly --------------------------------------------------------
    def test_clean_comparison_passes(self):
        self.assertEqual(self._find(_Canned([_CLEAN_CHANGE])), [])

    def test_removed_dependency_never_blocks(self):
        removed = dict(_VULN_CHANGE, change_type="removed")
        self.assertEqual(self._find(_Canned([removed])), [],
                         "removing a dependency is not something the PR brings in — never a block")

    def test_version_bump_reports_only_the_added_side_once(self):
        removed_old = {"change_type": "removed", "manifest": "package.json", "name": "demo-pkg",
                       "version": "0.9.0", "license": "MIT", "vulnerabilities": [_ADVISORY]}
        fs = self._find(_Canned([removed_old, _VULN_CHANGE]))
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "hard")

    def test_nested_product_manifest_still_blocks(self):
        nested = dict(_VULN_CHANGE, manifest="frontend/package.json")
        fs = self._find(_Canned([nested]))
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["location"]["file"], "frontend/package.json")

    # --- the license gate: unidentifiable (any repo) and copyleft (private only) ------------------
    def test_unidentifiable_license_blocks_hard_even_on_public(self):
        fs = self._find(_Canned([_UNKNOWN_LICENSE_CHANGE], visibility="public"))
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "hard")
        self.assertIn("no license could be identified", fs[0]["message"])
        self.assertNotIn("NOASSERTION", fs[0]["message"], "machine vocabulary must never reach the operator")

    def test_unidentifiable_license_blocks_hard_on_private_too(self):
        fs = self._find(_Canned([_UNKNOWN_LICENSE_CHANGE], visibility="private"))
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "hard")

    def test_workflow_action_unidentifiable_license_is_not_blocked(self):
        # a GitHub Action (declared in a workflow file) carries no SPDX license in GitHub's graph, so its
        # 'unknown' license must NOT block — else every new workflow is permanently un-mergeable.
        action = {"change_type": "added", "manifest": ".github/workflows/release.yml",
                  "name": "actions/checkout", "version": "v7.0.0", "license": "NOASSERTION", "vulnerabilities": []}
        self.assertEqual(self._find(_Canned([action], visibility="public")), [],
                         "a workflow action's unidentifiable license must not block (license carve-out)")

    def test_vulnerable_workflow_action_still_blocks(self):
        # the carve-out is LICENSE-only: a vulnerable action running in CI is a real supply-chain risk.
        action = {"change_type": "added", "manifest": ".github/workflows/release.yml",
                  "name": "evil/action", "version": "v1", "license": "NOASSERTION", "vulnerabilities": [_ADVISORY]}
        fs = self._find(_Canned([action], visibility="public"))
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "hard")
        self.assertIn("GHSA-test-1234", fs[0]["message"])

    def test_copyleft_blocks_hard_on_private(self):
        fs = self._find(_Canned([_COPYLEFT_CHANGE], visibility="private"))
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "hard")
        self.assertIn("copyleft", fs[0]["message"])
        self.assertIn("gpl-pkg", fs[0]["message"])

    def test_copyleft_blocks_hard_on_internal(self):
        fs = self._find(_Canned([_COPYLEFT_CHANGE], visibility="internal"))
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "hard")

    def test_copyleft_on_public_discloses_soft_never_silent(self):
        fs = self._find(_Canned([_COPYLEFT_CHANGE], visibility="public"))
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "soft", "copyleft-on-public must disclose, not pass silently")
        self.assertIn("public", fs[0]["message"])
        self.assertNotEqual(fs, [])

    def test_copyleft_visibility_unreadable_discloses_soft_and_stays_inactive(self):
        client = _Canned([_COPYLEFT_CHANGE], visibility=RuntimeError("repo read failed"))
        fs = self._find(client)
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "soft", "an unreadable visibility must never hard-block copyleft")
        self.assertIn("couldn't determine", fs[0]["message"])

    def test_clean_license_with_a_vulnerability_blocks_only_on_the_vulnerability(self):
        # a copyleft+vulnerable dep on a public repo: the copyleft side is a soft note, the vuln side is hard
        copyleft_vuln = dict(_COPYLEFT_CHANGE, vulnerabilities=[_ADVISORY])
        fs = self._find(_Canned([copyleft_vuln], visibility="public"))
        self.assertEqual(self._severities(fs), {"hard", "soft"})
        self.assertEqual(sum(1 for f in fs if f["severity"] == "hard"), 1)

    # --- accepted exceptions: a soft accept-note, never a silent [] -------------------------------
    def test_accepted_license_passes_with_a_soft_note(self):
        fs = self._find(_Canned([_COPYLEFT_CHANGE], visibility="private"),
                        allow_licenses=["GPL-3.0-or-later"])
        self.assertNotIn("hard", self._severities(fs))
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "soft")
        self.assertIn("accepted", fs[0]["message"].lower())

    def test_accepted_advisory_passes_with_a_soft_note(self):
        fs = self._find(_Canned([_VULN_CHANGE]), allow_ghsas=["GHSA-test-1234"])
        self.assertNotIn("hard", self._severities(fs))
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "soft")
        self.assertIn("accepted", fs[0]["message"].lower())

    def test_partial_advisory_acceptance_still_blocks_the_remaining_one(self):
        second = {"severity": "low", "advisory_ghsa_id": "GHSA-test-9999",
                  "advisory_summary": "Information leak"}
        two = dict(_VULN_CHANGE, vulnerabilities=[_ADVISORY, second])
        fs = self._find(_Canned([two]), allow_ghsas=["GHSA-test-1234"])  # accept only the first
        hard = [f for f in fs if f["severity"] == "hard"]
        self.assertEqual(len(hard), 1)
        self.assertIn("GHSA-test-9999", hard[0]["message"])
        self.assertNotIn("GHSA-test-1234", hard[0]["message"], "the accepted advisory must not be re-listed")

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
        fs = review.findings("hard", event_path=missing, repo="o/r", token="t", client=client,
                             allow_ghsas=[], allow_licenses=[])
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "soft")
        self.assertIn("nothing for it to review", fs[0]["message"])
        self.assertFalse(client.called, "the client must not be constructed/used when there's no PR to compare")

    def test_corrupt_event_degrades_to_soft_no_op_not_fail_closed(self):
        # a present-but-unparseable event must NOT crash (which would hit the validator's fail-closed hard path)
        self._write_event(None, raw="{ this is not json")
        client = _Canned(RuntimeError("must not be called on a corrupt event"))
        fs = review.findings("hard", event_path=self._event, repo="o/r", token="t", client=client,
                             allow_ghsas=[], allow_licenses=[])
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "soft")
        self.assertFalse(client.called)

    def test_missing_token_or_repo_discloses_soft_no_op_without_using_client(self):
        client = _Canned(RuntimeError("must not be called without a token"))
        fs = review.findings("hard", event_path=self._event, repo="o/r", token=None, client=client,
                             allow_ghsas=[], allow_licenses=[])
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "soft")
        self.assertFalse(client.called)

    # --- no handled branch is ever hard except the real blocks, and none ever raises --------------
    def test_disclosure_branches_are_never_hard(self):
        for client in (_Canned(review._Unavailable()), _Canned(review.DegradedReadError("x")),
                       _Canned([_CLEAN_CHANGE]), _Canned([_COPYLEFT_CHANGE], visibility="public"),
                       _Canned([_COPYLEFT_CHANGE], visibility=RuntimeError("x"))):
            self.assertNotIn("hard", self._severities(self._find(client)))

    # --- the SPDX license classifier (_license_status / _license_base / _spdx_operands) -----------
    def test_license_status_classifies_spdx_expressions(self):
        cases = {
            "MIT": "clean",
            "Apache-2.0": "clean",
            "LGPL-3.0": "clean",                       # weak copyleft — deliberately excluded
            "LGPL-3.0-or-later": "clean",
            "MIT OR GPL-3.0": "clean",                 # licensee can choose the permissive option
            "GPL-3.0": "copyleft",
            "GPL-3.0-only": "copyleft",
            "GPL-3.0-or-later": "copyleft",
            "GPL-3.0+": "copyleft",                    # deprecated '+' = or-later
            "gpl-3.0": "copyleft",                     # SPDX ids are case-insensitive
            "AGPL-3.0-only": "copyleft",
            "SSPL-1.0": "copyleft",                    # source-available, in the deny-set
            "GPL-3.0 AND MIT": "copyleft",             # AND: the combined work is encumbered
            "(MIT OR Apache-2.0) AND GPL-3.0": "copyleft",   # nested: GPL is a mandatory AND branch
            "GPL-3.0 AND (MIT OR BSD-3-Clause)": "copyleft",  # the permissive OR can't escape the AND'd GPL
            "MIT OR (GPL-3.0 AND Apache-2.0)": "clean",      # but here the licensee can pick the MIT branch
            "GPL-3.0-only WITH Classpath-exception-2.0": "copyleft",  # over-block (safe; accept-clearable)
            "Apache-2.0 WITH LLVM-exception": "clean",       # a permissive base with an exception stays clean
            "GPL-3.0 OR": "copyleft",                  # degenerate expression → block-leaning
            "": "unknown",
            "NOASSERTION": "unknown",
        }
        for expr, expected in cases.items():
            status, _ = review._license_status(expr, set())
            self.assertEqual(status, expected, f"{expr!r} should classify as {expected}, got {status}")

    def test_license_status_never_raises_on_garbage(self):
        for bad in (None, 123, [], {"x": 1}, "((( unbalanced", "GPL-3.0 WITH"):
            status, _ = review._license_status(bad, set())
            self.assertIn(status, ("unknown", "clean", "copyleft"))  # the contract: a value, never an exception

    def test_license_status_handles_pathological_nesting_without_raising(self):
        # deeply-nested parens must not blow the parser's stack (the "never raises" contract); the copyleft case
        # still fails safe (blocks), the clean case degrades in-band — neither raises into the fail-closed path
        deep_gpl = "(" * 500 + "GPL-3.0" + ")" * 500
        deep_mit = "(" * 500 + "MIT" + ")" * 500
        self.assertEqual(review._license_status(deep_gpl, set())[0], "copyleft")
        self.assertIn(review._license_status(deep_mit, set())[0], ("clean", "unknown"))

    def test_license_status_accepts_normalized_allow_entry(self):
        status, ids = review._license_status("GPL-3.0-or-later", {"GPL-3.0-OR-LATER"})
        self.assertEqual(status, "accepted")
        self.assertIn("GPL-3.0-or-later", ids)

    def test_license_status_accept_clears_a_mandatory_and_branch(self):
        # the un-escapable GPL in an AND branch is what blocks; accepting it clears the whole expression
        status, ids = review._license_status("(MIT OR Apache-2.0) AND GPL-3.0", {"GPL-3.0"})
        self.assertEqual(status, "accepted")
        self.assertIn("GPL-3.0", ids)

    def test_copyleft_in_a_mandatory_and_branch_blocks_on_private(self):
        # regression: a permissive OR-group must NOT hide copyleft sitting in a required AND branch
        nested = dict(_COPYLEFT_CHANGE, license="(MIT OR Apache-2.0) AND GPL-3.0")
        fs = self._find(_Canned([nested], visibility="private"))
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "hard", "un-escapable copyleft in an AND branch must block")

    # --- the allow-lists are read from the check's own committed params (fail-safe) ---------------
    def test_read_check_params_reads_the_real_committed_file(self):
        ag, al = review._read_check_params()
        self.assertIsInstance(ag, list)
        self.assertIsInstance(al, list)  # ships empty; the point is the read resolves the real path and parses

    def test_read_check_params_reads_a_nonempty_list_from_the_real_path(self):
        # craft a check file at the REAL path computation (validate.ROOT/.engine/check/...) and confirm it reads
        root = tempfile.mkdtemp(prefix="engine-depreview-root-")
        self.addCleanup(shutil.rmtree, root, True)
        os.makedirs(os.path.join(root, ".engine", "check"))
        with open(os.path.join(root, ".engine", "check", "dependency-review.json"), "w", encoding="utf-8") as fh:
            json.dump({"params": {"allow-ghsas": ["GHSA-aaaa-bbbb-cccc"], "allow-licenses": ["GPL-3.0-only"]}}, fh)
        saved = validate.ROOT
        validate.ROOT = root
        self.addCleanup(setattr, validate, "ROOT", saved)
        ag, al = review._read_check_params()
        self.assertEqual(ag, ["GHSA-aaaa-bbbb-cccc"])
        self.assertEqual(al, ["GPL-3.0-only"])

    def test_read_check_params_fails_safe_to_empty_on_malformed_json(self):
        root = tempfile.mkdtemp(prefix="engine-depreview-bad-")
        self.addCleanup(shutil.rmtree, root, True)
        os.makedirs(os.path.join(root, ".engine", "check"))
        with open(os.path.join(root, ".engine", "check", "dependency-review.json"), "w", encoding="utf-8") as fh:
            fh.write("{ this is not valid json")
        saved = validate.ROOT
        validate.ROOT = root
        self.addCleanup(setattr, validate, "ROOT", saved)
        self.assertEqual(review._read_check_params(), ([], []),
                         "a corrupt config must keep the gate blocking (empty allow-lists), never crash")

    # --- the compare() / visibility() client maps statuses correctly (over a fake transport) ------
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

    def test_visibility_reads_the_field_and_falls_back_to_the_bool(self):
        priv = review.DependencyReview("o/r", "t", transport=lambda m, p, b: (200, {"visibility": "private"}))
        self.assertEqual(priv.visibility(), "private")
        internal = review.DependencyReview("o/r", "t", transport=lambda m, p, b: (200, {"visibility": "internal"}))
        self.assertEqual(internal.visibility(), "internal")
        boolean = review.DependencyReview("o/r", "t", transport=lambda m, p, b: (200, {"private": True}))
        self.assertEqual(boolean.visibility(), "private")

    def test_visibility_returns_none_on_any_failure(self):
        for outcome in ((404, None), (500, None)):
            client = review.DependencyReview("o/r", "t", transport=lambda m, p, b, o=outcome: o)
            self.assertIsNone(client.visibility())
        raising = review.DependencyReview(
            "o/r", "t", transport=lambda m, p, b: (_ for _ in ()).throw(review.DegradedReadError("x")))
        self.assertIsNone(raising.visibility())

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
    def test_rule_json_declares_hard_pass_token_suites_and_allow_lists(self):
        path = os.path.join(validate.ROOT, ".engine/check/dependency-review.json")
        with open(path, "r", encoding="utf-8") as fh:
            rule = json.load(fh)
        self.assertEqual(rule["tier"], "hard")
        self.assertEqual(rule["kind"], "custom/script")
        self.assertTrue(rule["params"].get("pass_token"), "the gate needs the token to read the API")
        self.assertEqual(rule["params"]["script"], ".engine/tools/dependency_discipline/review.py")
        self.assertEqual(sorted(rule["suites"]), ["CI", "pre-close", "pre-commit"])
        self.assertEqual(rule["params"]["allow-ghsas"], [], "ships an empty accept-list (loosens nothing)")
        self.assertEqual(rule["params"]["allow-licenses"], [])

    # --- the module is offered at first-run via a verb-less catalog entry --------------------------
    def test_catalog_entry_is_valid_and_verb_less(self):
        # dependency-discipline adds no operator command — it is offered at setup by its plain description
        # alone. The whole catalog must validate, and this module must appear exactly once, verb-less, under
        # its design-named SCM category, with the merge-block disclosure the design requires (README:120).
        catalog = validate.load_json(os.path.join(validate.ENGINE_DIR, "provisioning", "module-catalog.json"))
        schema = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "provisioning-catalog.v1.json"))
        self.assertEqual(list(validate.Draft202012Validator(schema).iter_errors(catalog)), [],
                         "the whole catalog must validate against provisioning-catalog.v1.json")
        entries = [e for e in catalog if e["id"] == "dependency-discipline"]
        self.assertEqual(len(entries), 1, "dependency-discipline must be offered exactly once at setup")
        entry = entries[0]
        self.assertNotIn("verb", entry,
                         "dependency-discipline adds no command — its catalog entry must be verb-less")
        self.assertEqual(entry["category"], "Software Configuration Management")
        self.assertIn("block the merge", entry["description"],
                      "the setup card must disclose that the check can block a merge (informed consent)")


if __name__ == "__main__":
    unittest.main()
