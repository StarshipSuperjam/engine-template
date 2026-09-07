#!/usr/bin/env python3
"""One rule across every operator-facing surface that tells the operator how to trust the engine's Codex
hooks: whenever a surface names the CLI approval path (`/hooks`), it must ALSO name the Codex Desktop Hooks
screen — because on Desktop the approval prompt may not appear on its own, so a surface that mentions only the
CLI would silently strand a Desktop operator with grounding, the exploration write-gate, and memory capture all
off (StarshipSuperjam/engine-template#805). The VS Code extension is a separate case: it runs no project hooks
at all, so the surfaces send a VS Code operator to the CLI or the Desktop app rather than to any Hooks screen —
that copy is pinned per-surface, not by this cross-surface rule.

This is a DYNAMIC invariant, not a fixed list of surfaces. It walks every Markdown file in the repo (via
`validate.markdown_files`) and, for each place a standalone `/hooks` CLI token appears, requires the Desktop
Hooks screen to be named in the same instruction. So a NEW surviving surface — a doc added later that names
`/hooks` but forgets the Desktop path — fails here even though it has no test of its own yet; that is the
whole reason this file exists on top of the per-surface tests that pin each one's exact wording. A vacuity
guard asserts the walk actually reached the known surviving surfaces, so a broken walk fails loudly instead
of passing because it matched nothing. The two surfaces the .md scan can't reach — a code constant
(`wiring.CODEX_RETRUST_NOTE`) and the boot health line — are checked directly below, and the README (which
says "when prompted" rather than the literal token) has its own Desktop-screen check.

Note the generated CI-assurance manifest (`.engine/docs/ci-assurance.md`) quotes this very docstring, so it
is legitimately one of the scanned surfaces — which is why this docstring itself names both the `/hooks` CLI
path and the Desktop Hooks screen: the rule applies to the manifest's echo of it exactly as to any other.

The first-run copy surfaces (instantiator.FALLBACK_COPY and instantiator.load_copy) carry the same rule but
are deliberately NOT checked here: this file survives first-run, and a surviving file may not import the
setup-only `instantiator` module the engine removes (the reference-closure check forbids it). Their both-
paths rule is proven in test_instantiator.TestCodexHookTrustHandoff, which is removed with instantiator.

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
import validate       # noqa: E402
import wiring         # noqa: E402
# NOTE: this file must NOT import `instantiator` (or any other setup-only module the engine removes when a
# project is first set up) — it is a *surviving* file, and a surviving file that references removed setup
# code trips the first-run reference-closure check (test_first_run_reference_closure). The two first-run
# copy surfaces (instantiator.FALLBACK_COPY and instantiator.load_copy) carry the same both-paths rule, but
# they are proven in test_instantiator.TestCodexHookTrustHandoff — which is itself removed alongside
# instantiator — so this cross-surface invariant covers only the surfaces that live on in a set-up project.

# The Codex Desktop Hooks screen, however a surface happens to phrase it: "Hooks screen", or
# "Settings -> Hooks" / "Settings → Hooks" (ASCII or arrow). This is the second path every /hooks surface
# must also name.
_DESKTOP_HOOKS = re.compile(r"Hooks screen|Settings\s*(?:->|→)\s*Hooks")
# A standalone `/hooks` CLI token — NOT the ".codex/hooks.json" file path. A word character or a dot on
# either side (as in ".codex/hooks.json") excludes the path form, so only the CLI command matches.
_CLI_HOOKS = re.compile(r"(?<![\w.])/hooks(?![\w.])")

# Surfaces that survive first-run AND name /hooks today, relative to the repo root. The scan is dynamic — it
# discovers every surface on its own — but these are asserted reached so a walk that silently matches nothing
# (a renamed file, a bad root) fails loudly rather than passing vacuously. first-run.md is NOT listed: it
# names /hooks too, but the engine removes it at first-run, so it is absent in a set-up adopter project and
# must never be a required surface here.
_KNOWN_SURVIVING = (
    "AGENTS.md",
    ".engine/operations/codex-validation.md",
    ".engine/operations/codex-settings.md",
)


def _hooks_windows(text: str, radius: int = 2):
    """Yield (1-based line number, block-of-lines) for each line carrying a /hooks token — the block is the
    line plus `radius` lines either side, so a wrapped instruction (a numbered-list item split across
    physical lines, or a rule spanning a sentence) is checked as a unit, and a stray Desktop mention
    elsewhere in the file cannot satisfy the rule for a different instruction."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if _CLI_HOOKS.search(line):
            yield i + 1, "\n".join(lines[max(0, i - radius): i + radius + 1])


class TestEveryMarkdownHooksSurfaceNamesTheDesktopPath(unittest.TestCase):
    def test_every_markdown_hooks_mention_also_names_the_desktop_screen(self):
        reached = set()
        for path in validate.markdown_files(set()):
            rel = os.path.relpath(path, validate.ROOT)
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            for line_no, window in _hooks_windows(text):
                reached.add(rel)
                self.assertRegex(
                    window, _DESKTOP_HOOKS,
                    f"{rel}:{line_no} names the /hooks CLI path but not the Codex Desktop Hooks screen — a "
                    f"Desktop operator, whose approval prompt may never appear on its own, would be left with "
                    f"grounding, the exploration write-gate, and memory capture silently off. Every surface "
                    f"that names /hooks must name both approval paths.")
        # Vacuity guard: the dynamic walk MUST have reached the known surviving surfaces. If it reached none
        # of them, the scan is broken (bad root, renamed files) and every assertion above passed only because
        # it never ran — fail loudly instead of green-on-nothing.
        missing = [rel for rel in _KNOWN_SURVIVING if rel not in reached]
        self.assertEqual(
            [], missing,
            f"the /hooks scan did not reach known surviving surface(s) {missing}; the walk is broken or a "
            f"surface was renamed, so the invariant above would pass vacuously")


class TestCodexTrustCodeSurfacesNameBothPaths(unittest.TestCase):
    """The two surfaces the Markdown scan can't reach: a code constant and the boot health line."""

    def _assert_both_paths(self, name: str, text: str) -> None:
        self.assertRegex(text, _CLI_HOOKS, f"{name} must name the CLI approval path (/hooks)")
        self.assertRegex(text, _DESKTOP_HOOKS,
                         f"{name} names /hooks but not the Codex Desktop Hooks screen — a Desktop operator, "
                         f"whose prompt may never appear, would be stranded")

    def test_wiring_retrust_note_names_both(self):
        self._assert_both_paths("wiring.CODEX_RETRUST_NOTE", wiring.CODEX_RETRUST_NOTE)

    def test_boot_hooks_health_line_names_both(self):
        with mock.patch.object(boot.providers, "read_live_session", return_value=None):
            line = boot.hooks_health_line()
        self.assertIsNotNone(line, "with no live-session marker the health line must render")
        self._assert_both_paths("boot.hooks_health_line()", line)


class TestReadmeHookNoteNamesTheDesktopScreen(unittest.TestCase):
    def test_readme_hook_note_names_the_desktop_screen(self):
        # The README's hook sentence says "project hooks when prompted" rather than the literal "/hooks", so
        # the dynamic scan does not reach it — but the reason the whole rule exists (the Desktop prompt may
        # not appear) is exactly what it must still tell a Desktop reader, so the Desktop screen must be
        # named here too.
        with open(os.path.join(validate.ROOT, "README.md"), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        window = None
        for i, line in enumerate(lines):
            if "project hooks when prompted" in line:
                window = "\n".join(lines[max(0, i - 2): i + 3])
                break
        self.assertIsNotNone(window, "README must carry its Codex hook-approval note")
        self.assertRegex(window, _DESKTOP_HOOKS,
                         "README's hook-approval note must name the Desktop Hooks screen: the CLI prompts "
                         "but the Desktop app may not")


if __name__ == "__main__":
    unittest.main()
