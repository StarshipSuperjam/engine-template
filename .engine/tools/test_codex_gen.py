#!/usr/bin/env python3
"""Self-tests for the Codex render tool (codex_gen.py) — the pipeline five enforcement surfaces
depend on. These pin the render transforms (typed-prefix rewrite, session-flag strip, routing
lines, the read-only floor and no-model rule) and give the render-sync drift gate its fail-side
witnesses: a hand-edited render, a stale render, and an orphaned render must each be caught.

Run: uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py' -b
"""
from __future__ import annotations
import os
import shutil
import sys
import tempfile
import tomllib
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import codex_gen   # noqa: E402
import validate    # noqa: E402


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


AGENT_SRC = """---
name: qa-review-widget
description: Reviews widgets.
role: pre-submission-review
lens: widget
model-tier: judgment
permissions: read-only
output-contract: pre-submission-review-finding.v1
disallowedTools: [Edit, Write, NotebookEdit, Bash]
---

## Mandate

Review the widget. Run `/engine-status` first.
"""

SKILL_SRC = """---
name: engine-widget
description: Does widget things.
invocation: operator-typed
disable-model-invocation: true
allowed-tools: Bash(uv run *)
---

## Steps

1. Run `uv run --directory .engine -- python tools/widget.py --session "${CLAUDE_CODE_SESSION_ID}"`.
2. Then type `/engine-widget` again, or follow `.engine/operations/widget.md`.
"""


class _FixtureTree(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        _write(os.path.join(self.root, ".claude", "agents", "qa-review-widget.md"), AGENT_SRC)
        _write(os.path.join(self.root, ".claude", "skills", "engine-widget", "SKILL.md"), SKILL_SRC)

    def tearDown(self):
        self._tmp.cleanup()


class TestRenderTransforms(_FixtureTree):
    def test_agent_render_carries_the_floor_and_pins_no_model(self):
        codex_gen.generate(self.root)
        path = os.path.join(self.root, ".codex", "agents", "qa-review-widget.toml")
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        self.assertEqual(data["sandbox_mode"], "read-only")
        self.assertNotIn("model", data)
        self.assertEqual(data["model_reasoning_effort"], "high")     # judgment tier
        self.assertIn("read-only", data["developer_instructions"])
        self.assertIn("Do not run shell commands", data["developer_instructions"],
                      "a Bash-denylisting source renders the no-shell instruction line")
        self.assertIn("Review the widget.", data["developer_instructions"])

    def test_skill_render_rewrites_the_verb_and_strips_the_session_flag(self):
        codex_gen.generate(self.root)
        path = os.path.join(self.root, ".agents", "skills", "engine-widget", "SKILL.md")
        text = validate.read(path)
        self.assertIn("`$engine-widget`", text, "a backticked typed verb rewrites to the $ form")
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", text, "the Claude session flag is stripped")
        self.assertNotIn("`/engine-widget`", text, "no typed reference keeps the Claude sigil")
        self.assertIn(".engine/operations/widget.md", text, "runbook paths are untouched")
        fm = validate.frontmatter(path)
        self.assertEqual(sorted(fm), ["description", "name"],
                         "the Codex frontmatter narrows to the two keys Codex reads")
        policy = validate.read(os.path.join(self.root, ".agents", "skills", "engine-widget",
                                            "agents", "openai.yaml"))
        self.assertIn("allow_implicit_invocation: false", policy)

    def test_generate_is_idempotent(self):
        self.assertTrue(codex_gen.generate(self.root))
        self.assertEqual(codex_gen.generate(self.root), [], "a second render changes nothing")


class TestDriftGate(_FixtureTree):
    def test_in_sync_tree_is_clean(self):
        codex_gen.generate(self.root)
        self.assertEqual(codex_gen.check(self.root), [])

    def test_a_hand_edited_render_is_caught(self):
        codex_gen.generate(self.root)
        path = os.path.join(self.root, ".codex", "agents", "qa-review-widget.toml")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write('\nsandbox_mode = "workspace-write"\n')
        problems = codex_gen.check(self.root)
        self.assertTrue(any("does not match its canonical source" in p for p in problems), problems)

    def test_a_stale_render_is_caught_when_the_source_changes(self):
        codex_gen.generate(self.root)
        src = os.path.join(self.root, ".claude", "skills", "engine-widget", "SKILL.md")
        _write(src, SKILL_SRC.replace("Does widget things.", "Does widget things, better."))
        problems = codex_gen.check(self.root)
        self.assertTrue(any("does not match its canonical source" in p for p in problems), problems)

    def test_an_orphaned_render_is_caught(self):
        codex_gen.generate(self.root)
        shutil.rmtree(os.path.join(self.root, ".claude", "skills", "engine-widget"))
        problems = codex_gen.check(self.root)
        self.assertTrue(any("has no canonical source" in p for p in problems), problems)

    def test_a_missing_render_is_caught(self):
        codex_gen.generate(self.root)
        os.remove(os.path.join(self.root, ".codex", "agents", "qa-review-widget.toml"))
        problems = codex_gen.check(self.root)
        self.assertTrue(any("is missing" in p for p in problems), problems)

    def test_no_skill_is_excluded_and_engine_routine_now_renders(self):
        # The routine backend shipped, so SKILL_EXCLUDE is empty: an engine-routine skill renders its twin
        # like every other (the old exclusion, which would have shipped a stub, is retired).
        self.assertEqual(codex_gen.SKILL_EXCLUDE, frozenset(),
                         "no skill is excluded once the routine backend exists")
        _write(os.path.join(self.root, ".claude", "skills", "engine-routine", "SKILL.md"),
               SKILL_SRC.replace("engine-widget", "engine-routine"))
        codex_gen.generate(self.root)
        self.assertTrue(os.path.isfile(os.path.join(self.root, ".agents", "skills", "engine-routine",
                                                    "SKILL.md")), "the twin now renders")
        self.assertTrue(os.path.isfile(os.path.join(self.root, ".agents", "skills", "engine-routine",
                                                    "agents", "openai.yaml")), "with its operator-only policy")
        self.assertEqual(codex_gen.check(self.root), [], "and the drift gate is clean")


class TestCommittedRendersInSync(unittest.TestCase):
    def test_the_committed_tree_is_render_clean(self):
        """The live drift gate over the REAL repo: every committed render matches its source."""
        self.assertEqual(codex_gen.check(), [])


if __name__ == "__main__":
    unittest.main()
