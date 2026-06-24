#!/usr/bin/env python3
"""Tests for the cross-fork submission tooling (external-contribution module, Slice 2).

Every boundary is injected — the git diff reader (`run`), the template-detection `root`, the engine-owned set
(`owned`), the `gh` transport (`gh_run`), and the telemetry GitHub boundary (`github`) — so the whole
deterministic surface runs fully offline: no git, no gh, no network. The assertions pin the load-bearing
behaviors: the outgoing diff is uncapped (a leak can never sort past a cap), a leaked engine path HALTS before
any submit, a clean contribution is only PREPARED until an affirmative decision, the body follows the host's
template (or the engine's fallback shape), and an unreachable upstream degrades to a drafted submission.
"""
from __future__ import annotations
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from external_contribution import submit  # noqa: E402
import telemetry  # noqa: E402

OWNED = [
    ".engine/check/upstream-clean.json",
    ".engine/tools/external_contribution/submit.py",
    "CLAUDE.md",
    ".github/CODEOWNERS",
]


def _run(paths):
    """A fake git transport that returns the given diff as `git diff --name-only` would (newline-joined)."""
    return lambda args: "\n".join(paths)


def _gh_ok(record):
    def gh(args):
        record["args"] = args
        return 0, "https://github.com/upstream/project/pull/7", ""
    return gh


def _gh_fail(args):
    return 1, "", "could not resolve host github.com"


def _fake_github(opened):
    """A real telemetry.GitHubIssues wired to a fake transport (only the network is faked) that records the
    opened issue, so a test can assert telemetry-on-fire actually promoted."""
    label = telemetry.ENGINE_DOMAIN_LABEL

    def transport(method, path, body):
        if method == "GET" and f"/labels/{label}" in path:
            return 200, {}
        if method == "GET" and "/issues?" in path:
            return 200, []
        if method == "POST" and path.endswith("/issues"):
            opened.append(body)
            return 201, {"number": 99}
        return 200, {}
    return telemetry.GitHubIssues("owner/repo", "tok", transport=transport)


class TestOutgoingDiff(unittest.TestCase):
    def test_sorted_deduped(self):
        paths = submit.outgoing_diff("upstream/main", run=_run(["b.py", "a.py", "b.py"]))
        self.assertEqual(paths, ["a.py", "b.py"])

    def test_fail_open_on_none(self):
        self.assertEqual(submit.outgoing_diff("upstream/main", run=lambda args: None), [])

    def test_uncapped_so_a_late_sorting_leak_is_never_dropped(self):
        # 60 product files that sort BEFORE an engine path, plus the engine path — all must be returned (no
        # 50-cap), else the intersection could miss the leak. (ARCH-S2)
        many = [f"src/a{n:03d}.py" for n in range(60)] + [".engine/tools/x.py"]
        paths = submit.outgoing_diff("upstream/main", run=_run(many))
        self.assertEqual(len(paths), 61)
        self.assertIn(".engine/tools/x.py", paths)


class TestCleanFindings(unittest.TestCase):
    def test_clean_product_only_diff_is_empty(self):
        fs = submit.clean_findings("upstream/main", run=_run(["src/app.py", "README.md"]), owned=OWNED)
        self.assertEqual(fs, [])

    def test_leaked_engine_path_fires(self):
        fs = submit.clean_findings(
            "upstream/main", run=_run(["src/app.py", ".engine/check/upstream-clean.json"]), owned=OWNED)
        self.assertEqual(len(fs), 1)
        self.assertIn(".engine/check/upstream-clean.json", fs[0]["message"])


class TestTemplateDetection(unittest.TestCase):
    def _root(self, files):
        d = tempfile.mkdtemp(prefix="engine-submit-test-")
        self.addCleanup(__import__("shutil").rmtree, d, True)
        for rel, text in files.items():
            full = os.path.join(d, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as fh:
                fh.write(text)
        return d

    def test_detects_github_lowercase(self):
        root = self._root({".github/pull_request_template.md": "TEMPLATE-A"})
        self.assertEqual(submit.detect_upstream_pr_template(root), "TEMPLATE-A")

    def test_detects_github_uppercase(self):
        root = self._root({".github/PULL_REQUEST_TEMPLATE.md": "TEMPLATE-B"})
        self.assertEqual(submit.detect_upstream_pr_template(root), "TEMPLATE-B")

    def test_detects_template_directory_first_md(self):
        root = self._root({".github/PULL_REQUEST_TEMPLATE/bug.md": "BUG-TMPL",
                           ".github/PULL_REQUEST_TEMPLATE/feature.md": "FEAT-TMPL"})
        # alphabetical: bug.md wins
        self.assertEqual(submit.detect_upstream_pr_template(root), "BUG-TMPL")

    def test_no_template_returns_none(self):
        root = self._root({"README.md": "hi"})
        self.assertIsNone(submit.detect_upstream_pr_template(root))

    def test_detects_contributing(self):
        root = self._root({"CONTRIBUTING.md": "follow these"})
        self.assertEqual(submit.detect_contributing(root), "CONTRIBUTING.md")
        self.assertIsNone(submit.detect_contributing(self._root({"README.md": "x"})))


class TestBuildPrBody(unittest.TestCase):
    def test_follows_upstream_template_when_present(self):
        body = submit.build_pr_body(summary="Fixes the bug.", template_text="## Their Heading\n<!-- fill -->")
        self.assertIn("## Their Heading", body)
        self.assertIn("Fixes the bug.", body)

    def test_falls_back_to_engine_shape_when_absent(self):
        body = submit.build_pr_body(summary="Fixes the bug.", template_text=None)
        self.assertIn("## Summary", body)
        self.assertIn("## How it was checked", body)
        self.assertIn("Fixes the bug.", body)


class TestSubmitFlow(unittest.TestCase):
    BASE = dict(upstream_repo="upstream/project", base="upstream/main", head="me:feature",
                title="Fix the thing", summary="Fixes the thing.", now="2026-01-01T00:00:00Z")

    def test_leak_halts_before_submit_and_fires_telemetry(self):
        opened = []
        rec = {}
        r = submit.submit(**self.BASE, run=_run(["src/app.py", ".engine/check/upstream-clean.json"]),
                          owned=OWNED, gh_run=_gh_ok(rec), github=_fake_github(opened), confirm=True)
        self.assertEqual(r["status"], "halted-unclean")
        self.assertNotIn("args", rec)                    # gh pr create was NOT reached
        self.assertEqual(r["promoted"], 99)              # telemetry-on-fire promoted the leak
        self.assertEqual(len(opened), 1)
        self.assertIn(".engine/check/upstream-clean.json", r["narration"])

    def test_clean_without_confirm_is_prepared_not_opened(self):
        rec = {}
        r = submit.submit(**self.BASE, run=_run(["src/app.py", "README.md"]), owned=OWNED,
                          gh_run=_gh_ok(rec), github=None, confirm=False)
        self.assertEqual(r["status"], "prepared")
        self.assertNotIn("args", rec)                    # the human gate: nothing opened without a decision
        self.assertEqual(r["pr"]["repo"], "upstream/project")

    def test_clean_with_confirm_opens_via_gh_pr_create(self):
        root = tempfile.mkdtemp(prefix="engine-submit-flow-")
        self.addCleanup(__import__("shutil").rmtree, root, True)
        os.makedirs(os.path.join(root, ".github"))
        with open(os.path.join(root, ".github", "pull_request_template.md"), "w", encoding="utf-8") as fh:
            fh.write("## Upstream Description\n")
        rec = {}
        r = submit.submit(**self.BASE, run=_run(["src/app.py", "README.md"]), owned=OWNED, root=root,
                          gh_run=_gh_ok(rec), github=None, confirm=True)
        self.assertEqual(r["status"], "submitted")
        self.assertEqual(r["url"], "https://github.com/upstream/project/pull/7")
        self.assertEqual(rec["args"][:2], ["pr", "create"])
        self.assertIn("upstream/project", rec["args"])
        self.assertTrue(r["pr"]["followed_template"])
        self.assertIn("## Upstream Description", r["pr"]["body"])

    def test_unreachable_upstream_degrades_to_a_drafted_submission(self):
        r = submit.submit(**self.BASE, run=_run(["src/app.py"]), owned=OWNED,
                          gh_run=_gh_fail, github=None, confirm=True)
        self.assertEqual(r["status"], "degraded-draft")
        self.assertIn("engine opened this item itself", r["draft"])    # the engine-Issue body contract
        self.assertIn("upstream/project", r["narration"])

    def test_submitted_narration_does_not_assert_review_categorically(self):
        # Honest across the governed/ungoverned spectrum (RISK-S1): never a bare "maintainers WILL review".
        rec = {}
        r = submit.submit(**self.BASE, run=_run(["src/app.py"]), owned=OWNED,
                          gh_run=_gh_ok(rec), github=None, confirm=True)
        self.assertIn("if the project reviews contributions", r["narration"])


class TestDemo(unittest.TestCase):
    def test_demo_self_check_passes_on_real_logic(self):
        self.assertEqual(submit.demo(), 0)


if __name__ == "__main__":
    unittest.main()
