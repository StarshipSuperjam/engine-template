#!/usr/bin/env python3
"""One rule across every operator-facing surface that tells the operator how to trust the engine's Codex
hooks: whenever a surface names the CLI approval path (`/hooks`), it must ALSO name the Codex Desktop Hooks
screen — because on Desktop and in the VS Code extension the approval prompt may not appear on its own, so a
surface that mentions only the CLI would silently strand a Desktop operator with grounding, the exploration
write-gate, and memory capture all off (StarshipSuperjam/engine-template#805).

The surfaces are spread across code constants, the first-run copy, the boot health line, and four docs; each
has its own narrower test that pins its exact wording. This file is the single home for the cross-surface
INVARIANT, so adding a new surface that names /hooks without the Desktop path fails here even if that surface
has no test of its own yet.

Run: uv run --directory .engine --frozen -- python tools/selftest.py
"""
from __future__ import annotations

import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import boot           # noqa: E402
import instantiator   # noqa: E402
import validate       # noqa: E402
import wiring         # noqa: E402

# The Codex Desktop Hooks screen, however a surface happens to phrase it: "Hooks screen under Settings",
# or "Settings -> Hooks" / "Settings → Hooks" (ASCII or arrow). This is the second path every /hooks surface
# must also name.
_DESKTOP_HOOKS = re.compile(r"Hooks screen|Settings\s*(?:->|→)\s*Hooks")
_CLI_HOOKS = "/hooks"


def _doc(rel: str) -> str:
    with open(os.path.join(validate.ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _window(text: str, needle: str, radius: int = 2) -> str:
    """The block of lines within `radius` of the FIRST line containing `needle` — so a wrapped instruction
    (a numbered-list item split across physical lines) is checked as a unit, and a stray mention of the
    Desktop screen elsewhere in the file cannot satisfy the rule for this instruction."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if needle in line:
            return "\n".join(lines[max(0, i - radius): i + radius + 1])
    raise AssertionError(f"expected to find {needle!r} in the text, but it is absent")


class TestCodexTrustSurfacesNameBothPaths(unittest.TestCase):
    def _assert_both_paths(self, name: str, text: str) -> None:
        self.assertIn(_CLI_HOOKS, text, f"{name} must name the CLI approval path (/hooks)")
        self.assertRegex(text, _DESKTOP_HOOKS,
                         f"{name} names /hooks but not the Codex Desktop Hooks screen — a Desktop operator, "
                         f"whose prompt may never appear, would be stranded")

    # --- code + copy surfaces: the whole string is the instruction, so check it directly ---

    def test_wiring_retrust_note_names_both(self):
        self._assert_both_paths("wiring.CODEX_RETRUST_NOTE", wiring.CODEX_RETRUST_NOTE)

    def test_first_run_fallback_copy_names_both(self):
        self._assert_both_paths("instantiator.FALLBACK_COPY['codex-hook-trust']",
                                instantiator.FALLBACK_COPY["codex-hook-trust"])

    def test_rendered_first_run_copy_names_both(self):
        # The copy actually spoken during setup, read from first-run.md by heading (not the fallback).
        rendered = instantiator.load_copy()["codex-hook-trust"]
        self._assert_both_paths("first-run.md 'codex-hook-trust' section", rendered)

    def test_boot_hooks_health_line_names_both(self):
        with mock.patch.object(boot.providers, "read_live_session", return_value=None):
            line = boot.hooks_health_line()
        self.assertIsNotNone(line, "with no live-session marker the health line must render")
        self._assert_both_paths("boot.hooks_health_line()", line)

    # --- doc surfaces: bind the two paths to the SAME instruction via a line window ---

    def test_agents_grounding_note_names_both(self):
        self._assert_both_paths("AGENTS.md grounding note", _window(_doc("AGENTS.md"), _CLI_HOOKS))

    def test_codex_validation_step_names_both(self):
        self._assert_both_paths("codex-validation.md step 2",
                                _window(_doc(".engine/operations/codex-validation.md"), _CLI_HOOKS))

    def test_codex_settings_hooks_row_names_both(self):
        # The Hooks row is a single table line; find it by its leading cell.
        self._assert_both_paths("codex-settings.md Hooks row",
                                _window(_doc(".engine/operations/codex-settings.md"), "| Hooks |", radius=0))

    def test_readme_hook_note_names_the_desktop_screen(self):
        # The README's hook sentence says "when prompted" rather than the literal "/hooks", so it is exempt
        # from the CLI-token half — but the reason the whole rule exists (the Desktop prompt may not appear)
        # is exactly what it must still tell a Desktop reader, so the Desktop screen must be named here too.
        window = _window(_doc("README.md"), "project hooks when prompted")
        self.assertRegex(window, _DESKTOP_HOOKS,
                         "README's hook-approval note must name the Desktop Hooks screen: the CLI prompts "
                         "but the Desktop app may not")


if __name__ == "__main__":
    unittest.main()
