#!/usr/bin/env python3
"""Line-budget conformance for the operation runbooks under .engine/operations/ (the length tier that the
operation-shape rule enforces), owned here with named files and pinned baselines.

Run: uv run --directory .engine --frozen -- python tools/selftest.py

Counting rule. A runbook's length is its prose-body line count: the lines after the YAML frontmatter, with
closed fenced code blocks (``` or ~~~) and closed, REGISTERED generated regions (`<!-- generated: <name>
... -->` ... `<!-- /generated: <name> -->`, registered for that file in `validate.GENERATED_REGION_OWNERS`)
excluded, because neither is prose a cold operator reads and both are owned by a renderer or a fence, not
the runbook author; a marker that does not close, or a region no renderer owns, counts as prose and is a hard
finding, so a file cannot switch its own budget off. This is the count the hardened operation-shape tier applies (typed-lifecycle part C,
StarshipSuperjam/engine-template#821); `body_lines` below reads it from the validator's `prose_line_count`, so the
audit table in the delivering pull request, the merge check, and these assertions read the same number.

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
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402

OPERATIONS_DIR = os.path.join(validate.ENGINE_DIR, "operations")
TEMPLATE_PATH = os.path.join(validate.ENGINE_DIR, "templates", "operation.md")
SHAPE_RULE_PATH = os.path.join(validate.CHECK_DIR, "operation-shape.json")
MODULES_DIR = os.path.join(validate.ENGINE_DIR, "modules")

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
    # The Build spine's phase runbooks (StarshipSuperjam/engine-template#726): each pinned at the size it
    # measured when the spine was split, under the ordinary 120-line budget with no override. The spine
    # itself is under the ordinary budget too and is pinned by test_build_coordinator at its own measurement.
    ".engine/operations/build-continuity.md": 37,
    ".engine/operations/build-implementation.md": 69,
    ".engine/operations/build-kickoff.md": 81,
    ".engine/operations/build-plan-correction.md": 32,
    ".engine/operations/build-submission.md": 77,
    ".engine/operations/build-validation-and-review.md": 61,
}

# The recorded ceilings as the operator left them at part C: boot-session-start raised 200 -> 260 by the
# operator's typed ruling; the other two unchanged. build-orchestration.md's 250 left the set when the
# spine was split into phase runbooks (#726): the spine now fits the ordinary budget, so the override
# would have been slack, not a ceiling.
RECORDED_CEILINGS = {
    ".engine/operations/boot-session-start.md": 260,
    ".engine/operations/plan-orchestration.md": 150,
    ".engine/operations/memory-migration-trial.md": 160,
}


def body_lines(path: str) -> int:
    """Prose-body line count: after the frontmatter, excluding fenced blocks and generated regions — the
    validator's own `prose_line_count`, so the pins and the merge check read one number."""
    return validate.prose_line_count(validate.read(path), os.path.relpath(path, validate.ROOT))


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
    def test_fenced_blocks_and_registered_generated_regions_are_not_prose(self):
        import tempfile
        region = "build-protocol review-consumers"          # the region build-orchestration.md registers
        text = ("---\ntitle: t\n---\n\n## Purpose\n\nprose\n\n```text\ncode one\ncode two\n```\n\n"
                f"<!-- generated: {region} (never hand-edit) -->\nrendered one\nrendered two\n"
                f"<!-- /generated: {region} -->\n\n## Steps\n\n1. step\n")
        # two leading blanks, heading, blank, prose, blank, blank after the fence, blank after the region,
        # heading, blank, step — the four fenced lines are never counted, and the four generated-region lines
        # are left out only for the file that registers that region (validate.GENERATED_REGION_OWNERS).
        self.assertEqual(validate.prose_line_count(text, ".engine/operations/build-orchestration.md"), 11)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "op.md")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(text)
            self.assertEqual(body_lines(p), 15)      # an unregistered file: the region counts as prose


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
