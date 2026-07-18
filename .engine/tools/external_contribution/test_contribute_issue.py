#!/usr/bin/env python3
"""Tests for the cross-repo issue contribution tooling (external-contribution module).

Every boundary is injected — the `gh` transport (`gh_run`, used for BOTH the remote template fetch and the
filing) and the telemetry GitHub boundary (`github`) — so the whole deterministic surface runs fully offline: no
gh, no network. The assertions pin the load-bearing behaviors: the templates are read REMOTELY from the TARGET
repo (never a local tree), the target's OWN title prefix is carried verbatim, an unknown kind is surfaced for
the operator's choice (never guessed) and files nothing, a blank summary is refused, a matched template with no
`title:` files a plain title and is not narrated as carrying a heading, a zero-exit filing is reported filed even
without a captured URL (never a duplicate), a non-zero filing degrades to a drafted issue that traces to
telemetry in the operator's OWN repo, and the contributed body follows the target's template (never the engine's
own engine-domain body contract).
"""
from __future__ import annotations
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from external_contribution import contribute_issue  # noqa: E402
import quiet_call  # noqa: E402  (capture a demo walkthrough's stdout so it can't bury the suite summary)
import telemetry  # noqa: E402


def _tmpl(name, about, title, body):
    """Build an issue-template file's text; a `title` of None omits the frontmatter `title:` line."""
    lines = ["---", f"name: {name}", f"about: {about}"]
    if title is not None:
        lines.append(f"title: '{title}'")
    lines += ["---", "", body, ""]
    return "\n".join(lines)


_STD = {
    "bug.md": _tmpl("Bug report", "Something isn't working.", "Bug: ", "**What happened**"),
    "feature.md": _tmpl("Feature request", "Ask for something new.", "Feature: ", "**The need**"),
}


def _fake_gh(files=None, *, has_templates=True, create=(0, "https://github.com/upstream/project/issues/7", "")):
    """A fake `gh` that serves `gh api` contents (directory listing + raw file) for a target's
    `.github/ISSUE_TEMPLATE`, and `gh issue create` — running the REAL detection/flow with no network. `files`
    maps filename -> file text (a `config.yml` or a non-`.md` name is listed but should be skipped by the
    scanner). `has_templates=False` makes every contents call a 404. Records every call so a test can assert BOTH
    the remote sourcing path and the create args."""
    files = files if files is not None else _STD
    calls = []

    def gh(args):
        calls.append(list(args))
        if args and args[0] == "api":
            path = args[-1]
            after = path.split("/contents/", 1)[1] if "/contents/" in path else ""
            if after == ".github/ISSUE_TEMPLATE":
                if not has_templates:
                    return 1, '{"message":"Not Found"}', "gh: Not Found (HTTP 404)"
                return 0, json.dumps([{"name": n, "type": "file"} for n in sorted(files)]), ""
            if after.startswith(".github/ISSUE_TEMPLATE/"):
                fname = after.rsplit("/", 1)[-1]
                if has_templates and fname in files:
                    return 0, files[fname], ""
                return 1, '{"message":"Not Found"}', "gh: Not Found (HTTP 404)"
            return 1, '{"message":"Not Found"}', "gh: Not Found (HTTP 404)"  # docs/ fallback etc.
        if args[:2] == ["issue", "create"]:
            return create
        return 1, "", "unexpected gh call"

    gh.calls = calls  # type: ignore[attr-defined]
    return gh


def _fake_github(opened):
    """A real telemetry.GitHubIssues wired to a fake transport (only the network is faked) that records the
    opened issue, so a test can assert the stalled-contribution trace actually promoted."""
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


class TestDetectTemplates(unittest.TestCase):
    def test_reads_kinds_and_prefixes_from_the_target(self):
        gh = _fake_gh(_STD)
        got = contribute_issue.detect_upstream_issue_templates("upstream/project", gh_run=gh)
        by_key = {t["key"]: t for t in got}
        self.assertEqual(set(by_key), {"bug", "feature"})
        self.assertEqual(by_key["bug"]["title_prefix"], "Bug: ")
        self.assertIn("**What happened**", by_key["bug"]["body_text"])

    def test_reads_from_the_named_target_repo_not_a_local_tree(self):
        # S2 regression: the scan must query the TARGET repo remotely, not read any local checkout. Pin that the
        # gh call is aimed at repos/<target>/contents/.github/ISSUE_TEMPLATE.
        gh = _fake_gh(_STD)
        contribute_issue.detect_upstream_issue_templates("acme/widgets", gh_run=gh)
        api_paths = [c[-1] for c in gh.calls if c and c[0] == "api"]
        self.assertIn("repos/acme/widgets/contents/.github/ISSUE_TEMPLATE", api_paths)
        self.assertTrue(any(p.startswith("repos/acme/widgets/contents/.github/ISSUE_TEMPLATE/") for p in api_paths))

    def test_no_templates_is_empty(self):
        gh = _fake_gh(has_templates=False)
        self.assertEqual(contribute_issue.detect_upstream_issue_templates("u/p", gh_run=gh), [])

    def test_non_markdown_is_skipped(self):
        gh = _fake_gh({**_STD, "config.yml": "blank_issues_enabled: false\n"})
        keys = {t["key"] for t in contribute_issue.detect_upstream_issue_templates("u/p", gh_run=gh)}
        self.assertEqual(keys, {"bug", "feature"})

    def test_malformed_template_is_skipped_not_crashing(self):
        gh = _fake_gh({**_STD, "broken.md": "---\nname: [unbalanced\n---\nbody\n"})
        keys = {t["key"] for t in contribute_issue.detect_upstream_issue_templates("u/p", gh_run=gh)}
        self.assertEqual(keys, {"bug", "feature"})

    def test_hostile_non_basename_name_is_skipped(self):
        # Defense-in-depth: a listing entry whose name is not a plain basename (path separator / traversal /
        # leading dash) is skipped before it can reach a gh api path, even if it ends in .md.
        gh = _fake_gh({**_STD, "../../evil.md": _tmpl("Evil", "x", "Evil: ", "b"),
                       "-oops.md": _tmpl("Oops", "x", "Oops: ", "b")})
        keys = {t["key"] for t in contribute_issue.detect_upstream_issue_templates("u/p", gh_run=gh)}
        self.assertEqual(keys, {"bug", "feature"})

    def test_title_less_template_has_empty_prefix(self):
        gh = _fake_gh({"question.md": _tmpl("Question", "Ask.", None, "**Your question**")})
        got = contribute_issue.detect_upstream_issue_templates("u/p", gh_run=gh)
        self.assertEqual(got[0]["key"], "question")
        self.assertEqual(got[0]["title_prefix"], "")


class TestResolveKind(unittest.TestCase):
    def setUp(self):
        self.templates = [{"key": "bug", "name": "Bug report", "title_prefix": "Bug: ", "body_text": ""},
                          {"key": "feature", "name": "Feature request", "title_prefix": "Feature: ",
                           "body_text": ""}]

    def test_matches_by_key_case_insensitive(self):
        self.assertEqual(contribute_issue.resolve_kind("BUG", self.templates)["key"], "bug")

    def test_tolerates_trailing_colon(self):
        self.assertEqual(contribute_issue.resolve_kind("Bug:", self.templates)["key"], "bug")

    def test_matches_by_human_name(self):
        self.assertEqual(contribute_issue.resolve_kind("feature request", self.templates)["key"], "feature")

    def test_unknown_kind_is_none(self):
        self.assertIsNone(contribute_issue.resolve_kind("banana", self.templates))

    def test_absent_kind_is_none(self):
        self.assertIsNone(contribute_issue.resolve_kind(None, self.templates))


class TestBuildTitleAndBody(unittest.TestCase):
    def setUp(self):
        self.templates = [{"key": "bug", "name": "Bug report", "title_prefix": "Bug: ",
                           "body_text": "**What happened**"},
                          {"key": "question", "name": "Question", "title_prefix": "",
                           "body_text": "**Your question**"}]

    def test_title_carries_prefix_verbatim(self):
        title, matched = contribute_issue.build_issue_title(kind="bug", summary="it 500s",
                                                            templates=self.templates)
        self.assertEqual(title, "Bug: it 500s")
        self.assertEqual(matched["key"], "bug")

    def test_title_less_template_matches_but_stays_plain(self):
        title, matched = contribute_issue.build_issue_title(kind="question", summary="how do I X",
                                                            templates=self.templates)
        self.assertEqual(title, "how do I X")
        self.assertEqual(matched["key"], "question")  # matched (body follows) but prefix is empty

    def test_unknown_kind_gives_plain_title_and_no_match(self):
        title, matched = contribute_issue.build_issue_title(kind="banana", summary="it 500s",
                                                            templates=self.templates)
        self.assertEqual(title, "it 500s")
        self.assertIsNone(matched)

    def test_body_follows_template(self):
        self.assertEqual(contribute_issue.build_issue_body(summary="it 500s", template_text="**What happened**"),
                         "it 500s\n\n**What happened**")

    def test_body_plain_when_no_template(self):
        self.assertEqual(contribute_issue.build_issue_body(summary="it 500s", template_text=None), "it 500s")


class TestContributeFlow(unittest.TestCase):
    def test_blank_summary_is_refused_before_reading_or_filing(self):
        gh = _fake_gh(_STD)
        r = contribute_issue.contribute_issue(upstream_repo="u/p", kind="bug", summary="   ",
                                              gh_run=gh, github=None, confirm=True)
        self.assertEqual(r["status"], "needs-summary")
        self.assertEqual(gh.calls, [])  # nothing read, nothing filed

    def test_unknown_kind_surfaces_choices_and_files_nothing(self):
        gh = _fake_gh(_STD)
        r = contribute_issue.contribute_issue(upstream_repo="upstream/project", kind="banana",
                                              summary="x", gh_run=gh, github=None, confirm=True)
        self.assertEqual(r["status"], "kind-choice-needed")
        self.assertEqual({k["key"] for k in r["kinds"]}, {"bug", "feature"})
        self.assertNotIn(["issue", "create"], [c[:2] for c in gh.calls])  # nothing filed despite confirm=True

    def test_clean_prepare_does_not_file(self):
        gh = _fake_gh(_STD)
        r = contribute_issue.contribute_issue(upstream_repo="upstream/project", kind="bug",
                                              summary="it 500s", gh_run=gh, github=None, confirm=False)
        self.assertEqual(r["status"], "prepared")
        self.assertEqual(r["issue"]["title"], "Bug: it 500s")
        self.assertTrue(r["issue"]["title_prefixed"])
        self.assertNotIn(["issue", "create"], [c[:2] for c in gh.calls])

    def test_confirm_files_with_prefixed_title(self):
        gh = _fake_gh(_STD)
        r = contribute_issue.contribute_issue(upstream_repo="upstream/project", kind="feature",
                                              summary="add dark mode", gh_run=gh, github=None, confirm=True)
        self.assertEqual(r["status"], "filed")
        self.assertEqual(r["url"], "https://github.com/upstream/project/issues/7")
        create = next(c for c in gh.calls if c[:2] == ["issue", "create"])
        self.assertIn("Feature: add dark mode", create)
        self.assertIn("--repo", create)
        self.assertIn("upstream/project", create)

    def test_title_less_kind_files_plain_and_narrates_plainly(self):
        # M1 regression: a matched template with no title: prefix must not be narrated as carrying a heading.
        gh = _fake_gh({"question.md": _tmpl("Question", "Ask.", None, "**Your question**")})
        r = contribute_issue.contribute_issue(upstream_repo="u/p", kind="question",
                                              summary="how do I X", gh_run=gh, github=None, confirm=False)
        self.assertEqual(r["issue"]["title"], "how do I X")
        self.assertFalse(r["issue"]["title_prefixed"])
        self.assertNotIn("carries that project's own issue heading", r["narration"])

    def test_no_templates_files_plain_title(self):
        gh = _fake_gh(has_templates=False)
        r = contribute_issue.contribute_issue(upstream_repo="plain/project", kind="bug",
                                              summary="a plain report", gh_run=gh, github=None, confirm=True)
        self.assertEqual(r["status"], "filed")
        create = next(c for c in gh.calls if c[:2] == ["issue", "create"])
        self.assertIn("a plain report", create)
        self.assertNotIn("Bug: a plain report", create)  # no convention to follow → no prefix

    def test_zero_exit_without_url_is_still_filed_not_a_duplicate(self):
        # S1 regression: gh exits 0 but prints no URL → the issue WAS created; never narrate "nothing submitted".
        gh = _fake_gh(_STD, create=(0, "", ""))
        r = contribute_issue.contribute_issue(upstream_repo="u/p", kind="bug", summary="x",
                                              gh_run=gh, github=None, confirm=True)
        self.assertEqual(r["status"], "filed")
        self.assertIsNone(r["url"])
        self.assertNotIn("nothing was submitted", r["narration"])

    def test_nonzero_exit_degrades_to_draft_and_traces(self):
        gh = _fake_gh(_STD, create=(1, "", "could not resolve host github.com"))
        opened = []
        r = contribute_issue.contribute_issue(upstream_repo="upstream/project", kind="bug",
                                              summary="it 500s", gh_run=gh, github=_fake_github(opened),
                                              confirm=True)
        self.assertEqual(r["status"], "degraded-draft")
        self.assertIn("Bug: it 500s", r["draft"])
        self.assertEqual(len(opened), 1)  # traced into the operator's OWN repo

    def test_degrade_offline_github_is_surfaced_not_tracked(self):
        gh = _fake_gh(_STD, create=(1, "", "unreachable"))
        r = contribute_issue.contribute_issue(upstream_repo="upstream/project", kind="bug",
                                              summary="it 500s", gh_run=gh, github=None, confirm=True)
        self.assertEqual(r["status"], "degraded-draft")
        self.assertFalse(r["promoted"])  # GitHub offline → surfaced-not-tracked, never crashes


class TestDemo(unittest.TestCase):
    def test_demo_self_check_passes_on_real_logic(self):
        self.assertEqual(quiet_call.run(contribute_issue.demo), 0)


if __name__ == "__main__":
    unittest.main()
