#!/usr/bin/env python3
"""Codex trust guidance must identify /hooks as a CLI command and must not promise an unverified
Desktop approval screen. Project trust is distinct from individual hook trust; actual events establish
activation. Scan current Markdown surfaces dynamically, with a reachability guard, and check the code
notices directly. Setup-only copy is tested in test_instantiator so this file survives first-run removal.
"""
from __future__ import annotations
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import boot
import validate
import wiring

_CLI_HOOKS = re.compile(r"(?<![\w.])/hooks(?!\w|\.json\b)")
_UNSUPPORTED_DESKTOP = re.compile(
    r"open\s+(?:the\s+)?(?:Hooks screen|Settings\s*(?:->|→)\s*Hooks)|"
    r"Hooks screen under\s+Settings", re.I)
_KNOWN_SURVIVING = ("AGENTS.md", ".engine/operations/codex-validation.md",
                    ".engine/operations/codex-settings.md")


def _hooks_windows(text, radius=2):
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if _CLI_HOOKS.search(line):
            yield i + 1, "\n".join(lines[max(0, i-radius):i+radius+1])


def _guidance_faults(text):
    faults = []
    if _UNSUPPORTED_DESKTOP.search(text):
        faults.append("promises an unverified Desktop approval screen")
    for line, window in _hooks_windows(text):
        if not re.search(r"\bCLI\b", window):
            faults.append(f"line {line}: /hooks must be identified as a CLI command")
    return faults


class TestCodexTrustGuidance(unittest.TestCase):
    def test_current_markdown_guidance_is_qualified(self):
        reached = set()
        for path in validate.markdown_files(set()):
            rel = os.path.relpath(path, validate.ROOT)
            with open(path, encoding="utf-8") as stream:
                text = stream.read()
            if list(_hooks_windows(text)):
                reached.add(rel)
            self.assertEqual([], _guidance_faults(text), rel)
        self.assertEqual([], [p for p in _KNOWN_SURVIVING if p not in reached])

    def test_guard_rejects_unqualified_command_and_invented_desktop_remedy(self):
        self.assertTrue(_guidance_faults("Approve with /hooks."))
        self.assertTrue(_guidance_faults("Use CLI /hooks or open Settings → Hooks."))
        self.assertTrue(_guidance_faults("Open the Hooks screen under\nSettings."))
        self.assertEqual([], _guidance_faults(
            "Use the CLI /hooks approval browser; do not assume Desktop exposes a Hooks settings screen."))
        self.assertEqual([], _guidance_faults("Registrations live in .codex/hooks.json."))

    def test_code_notices_distinguish_trust_from_activation(self):
        with mock.patch.object(boot.providers, "read_live_session", return_value=None):
            health = boot.hooks_health_line()
        self.assertIsNotNone(health)
        for text in (health, wiring.CODEX_RETRUST_NOTE):
            self.assertEqual([], _guidance_faults(text))
            self.assertRegex(text, _CLI_HOOKS)
            self.assertIn("Project trust alone", text)
            self.assertIn("actual hook event", text)
            self.assertIn("do not assume Desktop", text)

    def test_readme_contains_actionable_trust_guidance(self):
        with open(os.path.join(validate.ROOT, "README.md"), encoding="utf-8") as stream:
            text = stream.read()
        self.assertRegex(text, _CLI_HOOKS)
        self.assertIn("Project trust alone", text)
        self.assertIn("verify an actual event", text)
        self.assertEqual([], _guidance_faults(text))


if __name__ == "__main__":
    unittest.main()
