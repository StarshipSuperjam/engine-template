#!/usr/bin/env python3
"""Acceptance pins for the complete, canonical Codex settings audit."""
from __future__ import annotations

import os
import json
import sys
import unittest


TOOLS = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.dirname(TOOLS)
POLICY = os.path.join(ENGINE, "operations", "codex-settings.md")
ROUTINE_MANIFEST = os.path.join(ENGINE, "modules", "routine-mode", "manifest.json")
sys.path.insert(0, TOOLS)
import module_manager  # noqa: E402


class TestCodexSettingsAudit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(POLICY, encoding="utf-8") as fh:
            cls.text = fh.read()

    def test_carries_a_dated_versioned_official_evidence_baseline(self):
        self.assertIn("2026-08-12", self.text)
        self.assertIn("0.147.0-alpha.6.5", self.text)
        for evidence in ("configuration precedence", "permissions", "custom agents",
                         "scheduled tasks", "desktop settings"):
            self.assertIn(evidence, self.text)

    def test_every_visible_settings_category_is_dispositioned(self):
        # The complete desktop sidebar baseline shown in the operator's audit request. Sub-settings under
        # General are pinned separately below; an omitted category makes this fail rather than disappear.
        categories = (
            "General", "Import", "Profile", "Appearance", "Voice", "Configuration",
            "Personalization", "Notifications", "Suggested prompts", "Pets", "Keyboard shortcuts", "Usage & billing", "Account",
            "Appshots", "Plugins", "Browser", "Computer use", "Hooks", "Connections", "Git",
            "Environments", "Worktrees", "Archived chats",
        )
        for category in categories:
            self.assertIn(f"| {category}", self.text, category)
        for general in ("sandbox / permissions", "web search", "output detail",
                        "reasoning summary", "prevent sleep / follow-up behavior",
                        "require Cmd+Enter for multiline prompts"):
            self.assertIn(general, self.text)

    def test_three_verified_platform_limits_are_unconditional(self):
        for fact in ("Live task selection wins", "Scheduled tasks share one default",
                     "Reviewer files request; they do not confine"):
            self.assertIn(fact, self.text)
        self.assertIn("no per-schedule sandbox profile", self.text)
        self.assertIn("parent task's live runtime override", self.text)

    def test_engine_does_not_claim_authority_over_operator_sandbox_config(self):
        self.assertIn("does not write sandbox or approval defaults", self.text)
        self.assertIn("manages only its own fenced MCP registrations", self.text)
        self.assertIn("Full Access is not an Engine", self.text)
        self.assertIn("never routine Full Access", self.text)

    def test_manual_interventions_name_outsized_value_and_fallback(self):
        self.assertIn("one manual choice with outsized value", self.text)
        self.assertIn("Approve once and re-approve", self.text)
        self.assertIn("If declined", self.text)
        self.assertIn("ground manually", self.text)

    def test_records_the_three_load_bearing_host_dependencies(self):
        self.assertIn("separate checkout", self.text)
        self.assertIn("desktop app, CLI, and IDE", self.text)
        self.assertIn("git-linked", self.text)
        self.assertIn("never switch the whole session to Full Access", self.text)

    def test_names_the_operator_runnable_uv_sandbox_proof(self):
        self.assertIn("tools/demo_uv_workspace_cache.py", self.text)
        self.assertIn("leaves tracked worktree state", self.text)

    def test_shared_default_requires_inventory_and_codex_build_automation_is_retired(self):
        self.assertIn("inventory every existing scheduled task", self.text)
        self.assertIn("disable any\n  forgotten or untrusted", self.text)
        self.assertIn("Codex Automations are not an Engine write path", self.text)
        self.assertIn("disable every existing `$engine-routine` Automation", self.text)
        self.assertIn("only the operator merges", self.text)
        self.assertIn("Keep Codex Engine work interactive", self.text)
        self.assertIn("Before merging or releasing this policy", self.text)
        self.assertIn("disabling the external task is the real retirement boundary", self.text)

    def test_upgrade_announces_codex_build_automation_retirement(self):
        with open(ROUTINE_MANIFEST, encoding="utf-8") as fh:
            manifest = json.load(fh)
        notice = manifest["retired_capabilities"]["0.2.0"]["description"]
        self.assertIn("Open Scheduled", notice)
        self.assertIn("$engine-routine", notice)
        self.assertIn("interactive Codex", notice)
        selected = module_manager.select_retired_capabilities(
            {"routine-mode": "0.1.0"}, {"routine-mode": "0.2.0"}, [manifest])
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["version"], "0.2.0")

    def test_credentials_are_masked_from_shell_with_an_honest_limit(self):
        self.assertIn('cli_auth_credentials_store = "keyring"', self.text)
        self.assertIn("ignore_default_excludes = false", self.text)
        self.assertIn("cannot redact a credential deliberately", self.text)
        self.assertIn("push interactively", self.text)

    def test_platform_answers_are_recorded_without_waiving_live_release_gate(self):
        self.assertIn("Acceptance record, 2026-08-12", self.text)
        self.assertIn("documented-platform acceptance", self.text)
        self.assertIn("hard pre-release gate", self.text)
        self.assertIn("not to waive live rollout", self.text)


if __name__ == "__main__":
    unittest.main()
