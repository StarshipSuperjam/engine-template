#!/usr/bin/env python3
"""Line-budget conformance for the operation runbooks under .engine/operations/ (the length tier that the
operation-shape rule enforces), owned here with named files and pinned baselines.

Run: uv run --directory .engine --frozen -- python tools/selftest.py

Counting rule. A runbook's length is its prose-body line count: the lines after the YAML frontmatter, with
fenced code blocks (``` or ~~~) and generated regions (`<!-- generated: ... -->` ... `<!-- /generated: ... -->`)
excluded, because neither is prose a cold operator reads and both are owned by a renderer or a fence, not
the runbook author. This is the count the hardened operation-shape tier applies (typed-lifecycle part C,
StarshipSuperjam/engine-template#821); `body_lines` below is that count, kept alongside the pins so the
audit table in the delivering pull request and these assertions read the same number.

What is pinned. (1) Every operation file is at or under its EFFECTIVE budget — the template's default
`length_budget` unless the guarded rule carries a recorded, reasoned override for that file. (2) The six
runbooks the typed-lifecycle program's parts A and B own (engine-upgrade, module-add, module-remove,
engine-remove; engine-arrival, control-plane-bootstrap) carry a per-file baseline pinned at their measured
size — a ratchet, so a later edit that grows one past its measurement is a deliberate act that moves the pin,
never silent drift. (3) No override names an operation owned by a non-core module: an optional add-on's
runbook is trimmed to the default budget, never granted a ceiling, so the ceiling list stays the short,
operator-signed set it is. (4) The single ceiling raise part C granted — boot-session-start to 260, the
operator's typed ruling — is recorded with its reason, and the other recorded ceilings hold at their values.
"""
from __future__ import annotations
import glob
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402

OPERATIONS_DIR = os.path.join(validate.ENGINE_DIR, "operations")
TEMPLATE_PATH = os.path.join(validate.ENGINE_DIR, "templates", "operation.md")
SHAPE_RULE_PATH = os.path.join(validate.CHECK_DIR, "operation-shape.json")
MODULES_DIR = os.path.join(validate.ENGINE_DIR, "modules")

GENERATED_BEGIN = re.compile(r"^\s*<!-- generated:")
GENERATED_END = re.compile(r"^\s*<!-- /generated:")

# The six lifecycle runbooks parts A and B of the typed-lifecycle program delivered, pinned at the size measured
# when part C hardened the tier. Raising a pin is a deliberate edit to this table, made in the same change that
# grows the runbook and disclosed there.
PROGRAM_RUNBOOK_BASELINES = {
    ".engine/operations/control-plane-bootstrap.md": 90,
    ".engine/operations/engine-arrival.md": 103,
    ".engine/operations/engine-remove.md": 55,
    ".engine/operations/engine-upgrade.md": 102,
    ".engine/operations/module-add.md": 53,
    ".engine/operations/module-remove.md": 54,
}

# The recorded ceilings as the operator left them at part C: boot-session-start raised 200 -> 260 by the
# operator's typed ruling; the other three unchanged.
RECORDED_CEILINGS = {
    ".engine/operations/boot-session-start.md": 260,
    ".engine/operations/plan-orchestration.md": 150,
    ".engine/operations/build-orchestration.md": 250,
    ".engine/operations/memory-migration-trial.md": 160,
}


def body_lines(path: str) -> int:
    """Prose-body line count: after the frontmatter, excluding fenced blocks and generated regions."""
    body = validate._body_without_frontmatter(validate.read(path))
    count, in_fence, in_generated = 0, False, False
    for line in body.splitlines():
        if GENERATED_BEGIN.match(line):
            in_generated = True
            continue
        if GENERATED_END.match(line):
            in_generated = False
            continue
        if in_generated:
            continue
        if validate.FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        count += 1
    return count


def overrides() -> dict:
    rule = validate.load_json(SHAPE_RULE_PATH)
    return (rule.get("params") or {}).get("length_budget_overrides") or {}


def effective_budget(rel: str) -> int:
    default = validate.frontmatter(TEMPLATE_PATH)["length_budget"]
    entry = overrides().get(rel)
    return entry["budget"] if isinstance(entry, dict) and isinstance(entry.get("budget"), int) else default


def operation_files() -> list:
    return sorted(os.path.relpath(p, validate.ROOT)
                  for p in glob.glob(os.path.join(OPERATIONS_DIR, "*.md")))


def non_core_module_operations() -> dict:
    """{operation rel path: module id} for every operation an optional (non-core) module provides."""
    owned = {}
    for manifest_path in glob.glob(os.path.join(MODULES_DIR, "*", "manifest.json")):
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)
        if manifest.get("status") == "required":
            continue
        for pattern in (manifest.get("provides") or {}).get("operation") or []:
            for path in glob.glob(os.path.join(validate.ROOT, pattern)):
                owned[os.path.relpath(path, validate.ROOT)] = manifest.get("id")
    return owned


class TestCountingRule(unittest.TestCase):
    def test_fenced_blocks_and_generated_regions_are_not_prose(self):
        import tempfile
        text = ("---\ntitle: t\n---\n\n## Purpose\n\nprose\n\n```text\ncode one\ncode two\n```\n\n"
                "<!-- generated: x (never hand-edit) -->\nrendered one\nrendered two\n<!-- /generated: x -->\n\n"
                "## Steps\n\n1. step\n")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "op.md")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(text)
            # two leading blanks, heading, blank, prose, blank, blank after the fence, blank after the region,
            # heading, blank, step — the four fenced lines and the four generated-region lines are not counted
            self.assertEqual(body_lines(p), 11)


class TestEveryOperationWithinBudget(unittest.TestCase):
    def test_every_operation_is_at_or_under_its_effective_budget(self):
        over = {rel: (body_lines(os.path.join(validate.ROOT, rel)), effective_budget(rel))
                for rel in operation_files()}
        over = {rel: pair for rel, pair in over.items() if pair[0] > pair[1]}
        self.assertEqual(over, {}, f"operations over their effective line budget (lines, budget): {over}")

    def test_the_program_runbooks_hold_their_pinned_baselines(self):
        grown = {}
        for rel, pin in PROGRAM_RUNBOOK_BASELINES.items():
            path = os.path.join(validate.ROOT, rel)
            self.assertTrue(os.path.isfile(path), rel)
            measured = body_lines(path)
            if measured > pin:
                grown[rel] = (measured, pin)
        self.assertEqual(grown, {}, f"program runbooks grew past their pinned baseline (measured, pin): {grown}")


class TestCeilings(unittest.TestCase):
    def test_no_override_names_a_non_core_module_operation(self):
        non_core = non_core_module_operations()
        self.assertTrue(non_core, "expected at least one optional module to provide an operation")
        offending = {rel: non_core[rel] for rel in overrides() if rel in non_core}
        self.assertEqual(offending, {}, f"a non-core module's runbook carries a ceiling instead of a trim: {offending}")

    def test_recorded_ceilings_are_the_operator_signed_set(self):
        recorded = {rel: entry["budget"] for rel, entry in overrides().items()}
        self.assertEqual(recorded, RECORDED_CEILINGS)
        for rel, entry in overrides().items():
            self.assertTrue(entry.get("why", "").strip(), rel)

    def test_boot_session_start_raise_records_the_operator_ruling(self):
        why = overrides()[".engine/operations/boot-session-start.md"]["why"]
        self.assertIn("2026-09-04", why)
        self.assertIn("260", why)
        self.assertIn("operator", why)


if __name__ == "__main__":
    unittest.main()
