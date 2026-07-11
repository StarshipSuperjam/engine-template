#!/usr/bin/env python3
"""Conformance test for the fuller-detail authoring scaffolds
(.engine/modules/product-design/scaffold/{principles,architecture,diataxis-*}.md).

These are the "write it down properly" templates the product-intake authoring branch draws from when the operator
chooses the fuller path — the product principles, the architecture overview (with a C4-style diagram), and the
four kinds of user guide. Unlike the three spec-corpus templates (index / capability / build-plan, tied to the
form + coverage checks in test_scaffold.py), these fuller documents are **authoring-only**: the "What it
produces" table marks them product-owned, not validated, so NO engine check runs on them and there is no
write-from = check-against tie to lock. This test therefore guards only what is real to guard: that each template
is committed and carries the strip-on-author convention (an HTML guidance comment the author removes, and
bracketed placeholders to replace), so the scaffold the engine authors from stays a genuine starting shape and
never silently disappears. It reads the templates from disk, so it exercises exactly the bytes that ship.
"""
from __future__ import annotations
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # .engine/tools on sys.path
import validate  # noqa: E402

_SCAFFOLD_DIR = os.path.join(validate.ROOT, ".engine", "modules", "product-design", "scaffold")

# The fuller-detail authoring templates (authoring-only; no form check ties to them).
_FULLER_TEMPLATES = (
    "principles.md",
    "architecture.md",
    "diataxis-tutorial.md",
    "diataxis-how-to.md",
    "diataxis-reference.md",
    "diataxis-explanation.md",
)


def _read(name: str) -> str:
    with open(os.path.join(_SCAFFOLD_DIR, name), "r", encoding="utf-8") as fh:
        return fh.read()


class FullerScaffoldConventionTests(unittest.TestCase):
    def test_every_fuller_template_is_committed(self):
        for name in _FULLER_TEMPLATES:
            self.assertTrue(os.path.isfile(os.path.join(_SCAFFOLD_DIR, name)),
                            f"the fuller authoring template {name!r} must be committed")

    def test_every_fuller_template_carries_stripped_guidance_and_placeholders(self):
        # The strip-on-author convention: a guidance comment (removed when authoring) and bracketed placeholders
        # (replaced with the real thing) — the same convention the spec-corpus templates use.
        for name in _FULLER_TEMPLATES:
            text = _read(name)
            self.assertIn("<!--", text, f"{name} must carry an HTML guidance comment the author strips")
            self.assertIn("-->", text, f"{name} must close its HTML guidance comment")
            self.assertTrue(re.search(r"<[^>\n]+>", text.split("-->", 1)[-1]),
                            f"{name} must carry at least one bracketed placeholder for the author to replace")

    def test_every_fuller_template_names_its_authoring_only_bound(self):
        # Each template states plainly that it is the operator's to get right / not checked by the engine, so the
        # §17 "not validated" bound travels with the scaffold itself, not only the runbook copy. Whitespace is
        # normalized so the phrase is found regardless of where the guidance prose line-wraps.
        for name in _FULLER_TEMPLATES:
            normalized = " ".join(_read(name).split())
            self.assertIn("NOT checked by the engine", normalized,
                          f"{name} must state that it is authoring-only (not checked by the engine)")

    def test_architecture_template_carries_a_mermaid_flowchart_diagram(self):
        # The C4-style container view ships as a mermaid `flowchart` so it renders on GitHub and stays diffable.
        text = _read("architecture.md")
        self.assertIn("```mermaid", text, "the architecture template must embed a mermaid diagram")
        self.assertIn("flowchart", text, "the architecture diagram must be a stable `flowchart`")


if __name__ == "__main__":
    unittest.main()
