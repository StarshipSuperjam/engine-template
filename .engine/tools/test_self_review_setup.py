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

# The operator never meets maintainer or audit backstage vocabulary. The maintainer stems mirror the apply
# copy's forbidden set (kept in step with the convention, INLINED — never imported from the retired
# test_instantiator) and the audit/schedule terms are this module's own (audit-library/README.md:132-138 +
# "never call the schedule a cron"). NOT forbidden: "agent" — the page legitimately names the
# `.claude/agents/audit.md` path in the pasted Cloud-Routine prompt.
_FORBIDDEN = ("orchestrat", "coherence", "wiring", "wires", "manifest", "idempotent", "venv", "sync",
              "lockfile", "pyproject", "ruleset", "override", "custom/script", "provides", "invocation",
              "model-auto", "operator-typed", "model-only", "foundation",
              "persona", "lens", "function-probe", "concern-list", "cron")


def _page_text() -> str:
    with open(PAGE_PATH, encoding="utf-8") as fh:
        return fh.read()


def _prose_only(text: str) -> str:
    # Drop ``` fenced blocks before the vocabulary scan: the schedule line is GitHub's own literal syntax
    # (`- cron: "17 7 * * 0"`), shown for the operator to copy verbatim — that is not the engine CALLING the
    # schedule a "cron" in prose, which is the thing the plain-language bar forbids. Fences are balanced, so
    # the even-indexed split segments are the text OUTSIDE code blocks.
    return "".join(text.split("```")[0::2])


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

    def test_carries_the_two_step_token_setup(self):
        # Setup is genuinely TWO steps — the sign-in token, then the GitHub secret. The phantom third step
        # ("turn on access for scheduled runs", a Claude-account toggle that does not exist) was removed (#175):
        # a qualifying-plan token plus the secret is the whole setup.
        self.assertIn("claude setup-token", self.text)        # step 1: the sign-in token
        self.assertIn("two one-time steps", self.text)        # two, not three — the count must not regress
        # The phantom toggle must not return. Key on text UNIQUE to the deleted Step 2: Step 1 legitimately
        # keeps "sign in to your Claude account" and the heading is "Turn it on", so don't assert on those.
        self.assertNotIn("scheduled or developer", self.text)
        self.assertNotIn("turn on access for scheduled runs", self.text.lower())

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


class TestSetupPagePlainLanguage(unittest.TestCase):
    def test_no_backstage_vocabulary_in_prose(self):
        prose = _prose_only(_page_text()).lower()
        for term in _FORBIDDEN:
            self.assertNotIn(term, prose,
                             f"the operator setup page must not leak backstage vocabulary: {term!r}")


if __name__ == "__main__":
    unittest.main()
