#!/usr/bin/env python3
"""Tests for the operator setup page `.engine/audits/self-review-setup.md` (audit-library slice 3b).

This page is the returnable, plain-language guide that tells the operator how to arm the engine's scheduled
self-review (the one-time token), keep it running (expiry / usage limits / re-arm), change how-often / which
model, and optionally run it as a Cloud Routine. It survives first-run (the year-later token re-arm depends on
it), so it lives with the audit's own files and is owned by audit-library's `provides`.

These pin the load-bearing facts a future edit must not silently drop — the EXACT secret name, the two-step
shape, the Cloud-Routine eliminations, the un-run-at-v1 honesty, the monthly cadence line — and the
plain-language bar (no maintainer/audit backstage vocabulary reaches the operator). All assertions read the
committed files directly; this surviving test never imports the retired first-run machinery it reasons about
(the first-run reference-closure invariant, engine-planning D-219/D-220).
"""
from __future__ import annotations
import json
import os
import unittest

_TOOLS = os.path.dirname(os.path.abspath(__file__))
_ENGINE = os.path.dirname(_TOOLS)
PAGE_REL = ".engine/audits/self-review-setup.md"
PAGE_PATH = os.path.join(_ENGINE, "audits", "self-review-setup.md")
MANIFEST_PATH = os.path.join(_ENGINE, "modules", "audit-library", "manifest.json")
FIRST_RUN_ASSETS_PATH = os.path.join(_ENGINE, "provisioning", "first-run-assets.json")


def _page_text() -> str:
    with open(PAGE_PATH, encoding="utf-8") as fh:
        return fh.read()


class TestSetupPagePresenceAndOwnership(unittest.TestCase):
    def test_page_exists(self):
        self.assertTrue(os.path.isfile(PAGE_PATH), f"the setup page must be present at {PAGE_REL}")

    def test_page_is_owned_by_audit_library(self):
        # Without this the file is an ownership orphan (every .engine/ file is claimed by exactly one
        # module's provides). It rides audit-library's `audits` group, beside the seeded concern-list.
        with open(MANIFEST_PATH, encoding="utf-8") as fh:
            manifest = json.load(fh)
        self.assertIn(PAGE_REL, manifest["provides"]["audits"],
                      "the setup page must be claimed by audit-library's provides or it is an orphan")

    def test_page_survives_first_run(self):
        # Load-bearing: the year-later token re-arm sends the operator back to this page, so it must NOT be
        # among the first-run-only assets the instantiator retires. Read the committed manifest of the
        # retired set (never the instantiator itself).
        with open(FIRST_RUN_ASSETS_PATH, encoding="utf-8") as fh:
            retired = json.load(fh)
        self.assertNotIn(PAGE_REL, retired["files"],
                         "the setup page must survive first-run — the re-arm guidance depends on it")


class TestSetupPageContent(unittest.TestCase):
    """The facts the page exists to carry. A future edit that drops one fails here, name-to-fact."""

    def setUp(self):
        self.text = _page_text()

    def test_names_the_exact_secret(self):
        # A typo in the secret name fails the run silently, so the page must carry it EXACTLY.
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", self.text)

    def test_carries_the_three_step_setup(self):
        # Setup is THREE steps now — the sign-in token, the GitHub secret, then the create/approve-PR setting
        # that lets the run open its summary (round-2: without it the run dies with no summary opened). The
        # phantom step from #175 ("turn on access for scheduled runs", a Claude-account toggle that does NOT
        # exist) stays banned — the real third step is the GitHub setting, not that fiction.
        self.assertIn("claude setup-token", self.text)        # step 1: the sign-in token
        self.assertIn("three one-time steps", self.text)      # three, not two — and never the phantom toggle
        self.assertNotIn("two one-time steps", self.text)     # the old count must not linger
        # The #175 phantom toggle must never return:
        self.assertNotIn("scheduled or developer", self.text)
        self.assertNotIn("turn on access for scheduled runs", self.text.lower())

    def test_names_the_cli_prerequisite_for_the_token(self):
        # Round-2 / engine-planning D-229 S2: `claude setup-token` needs Claude Code's command-line tool, which
        # the engine's Claude-Desktop operator may not have installed — the page must name that (and offer to
        # help set it up), not silently assume it.
        self.assertIn("command-line tool", self.text)

    def test_documents_the_create_and_approve_pr_setting(self):
        # Round-2 / Option A: the run can only open its summary if the operator enables GitHub's "create and
        # approve pull requests" setting (off by default for EVERY repo). Without this step on the page, every
        # adopter's self-review runs but silently opens no summary.
        self.assertIn("create and approve pull requests", self.text)

    def test_discloses_it_now_reads_the_open_issue_list(self):
        # Round-2: the review now also reads the open engine issues (concern #2), so the coverage note says so
        # — keeping the honesty contract that names what the review can and cannot see.
        self.assertIn("open issue", self.text)

    def test_cloud_routine_eliminates_the_wrong_choices(self):
        # The design's Cloud-Routine walkthrough names Remote (not Local) on a recurring schedule.
        self.assertIn("Remote", self.text)
        self.assertIn("recurring", self.text)

    def test_discloses_the_cloud_path_is_unrun_at_v1(self):
        self.assertIn("not yet been run end-to-end", self.text)

    def test_gives_a_copy_paste_monthly_cadence_line(self):
        self.assertIn("17 7 1 * *", self.text)

    def test_names_the_model_knob(self):
        self.assertIn("AUDIT_MODEL", self.text)

    def test_discloses_the_saved_memory_coverage_limit(self):
        # Honesty line (operator-ratified): the review can't yet read saved working memory (the off-repo
        # backup is unbuilt), so the page says so up front rather than letting a clean review be over-trusted.
        self.assertIn("saved working memory", self.text)


if __name__ == "__main__":
    unittest.main()
