#!/usr/bin/env python3
"""Self-tests for the policy surface (slice 14): the policy.v1 frontmatter grammar, the committed policy
template, the live policy-shape rule, the four committed v1-core policy instances, the contract-threshold
filled-presence rule (the slice-13 forward-obligation), and the catalog flip that wires schema + template in.

Run: uv run --directory .engine -- python -m unittest discover -s tools -p 'test_*.py'

These lock: policy.v1 is a well-formed schema with teeth (a status outside the decision lifecycle, a bad
date, a missing required field, an unknown extra field, or a malformed established_by link is rejected, and
the optional established_by conforms when present and when absent); the committed policy template's body
carries exactly the four required sections in order, its frontmatter shape-spec matches the policy-shape
rule's params byte-for-byte (no drift between the authoring scaffold and the machine-read rule), and that
shape-spec is a well-formed template.v1; the policy-shape rule is well-formed, joins CI, dispatches the shape
kind over .engine/policies/*.md, and the FOUR real committed policies pass it, while a missing/ out-of-order/
stray section fires a hard finding and an over-length body is only a soft nudge; the contract-threshold rule
is a well-formed presence rule that joins CI, is green on the empty contract stream, and fires a hard finding
when Significance or Anti-choice is blank or only the template placeholder (presence is checkable), passing
when both are filled; and the catalog now routes the policy surface to its in-repo schema and template.
Frontmatter is NOT live-checked in core (no frontmatter reader yet, D-090) — that conformance is proven here
over fixtures, with the live check deferred to a later module.
"""
from __future__ import annotations
import glob
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402

POLICY_SCHEMA = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "policy.v1.json"))
TEMPLATE_SCHEMA = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "template.v1.json"))
TEMPLATE_PATH = os.path.join(validate.ENGINE_DIR, "templates", "policy.md")
POLICIES_DIR = os.path.join(validate.ENGINE_DIR, "policies")
SHAPE_RULE = validate.load_json(os.path.join(validate.CHECK_DIR, "policy-shape.json"))
CT_RULE = validate.load_json(os.path.join(validate.CHECK_DIR, "contract-threshold.json"))
STATUS_ENUM = POLICY_SCHEMA["properties"]["status"]["enum"]

# The four v1-core policies that ship from layer one (modules/core/README.md), non-removable.
EXPECTED_POLICIES = {"contract-threshold", "finding-disposition", "escalation", "triage-threshold"}

# A representative, conforming policy frontmatter instance (a foundational policy omits established_by).
VALID_FM = {"title": "Contract threshold", "status": "accepted", "date": "2026-06-03"}

# A well-formed policy BODY (the shape kind reads the body, never the frontmatter).
VALID_BODY = (
    "## Rule\nRecord a contract only for a significant, hard-to-reverse decision.\n"
    "## Scope\nApplies to every decision made inside the engine.\n"
    "## Rationale\nKeeps permanent decision records rare and worth reading.\n"
    "## Enforcement-tier\nPosture, plus a hard-fail on filled presence and a soft-warn rate signal.\n")

# A well-formed CONTRACT body for the contract-threshold presence rule (it checks Significance + Anti-choice).
CONTRACT_BODY_FILLED = (
    "## Decision\nUse a thin validator core.\n"
    "## Significance\nIt constrains how every later check is added.\n"
    "## Rationale\nKeeps the core small and data-driven.\n"
    "## Anti-choice\nA fat validator with hard-coded checks; rejected — it couples the kinds.\n"
    "## Status\naccepted\n")


def _errors(schema, instance):
    return list(validate.Draft202012Validator(schema).iter_errors(instance))


def _run_kind(kind_fn, rule, files):
    """Run a kind callable with validate.target_files stubbed to `files`, so a fixture under a temp dir
    can be targeted directly (the test_seed.py / test_contract.py pattern)."""
    orig = validate.target_files
    validate.target_files = lambda r: list(files)
    try:
        return kind_fn(rule, {})
    finally:
        validate.target_files = orig


def _write(d, name, text):
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(text)
    return p


def _template_frontmatter_shape_spec(path):
    """Parse the template's frontmatter shape-spec WITHOUT a YAML dependency (core has no frontmatter
    reader): the three keys' values are all JSON-compatible (quoted strings, arrays, an int), so each
    'key: <value>' line in the block between the first two '---' fences parses with json.loads."""
    text = validate.read(path)
    block = text.split("---", 2)[1]
    spec = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, val = line.partition(":")
        spec[key.strip()] = json.loads(val.strip())
    return spec


class TestSchema(unittest.TestCase):
    def test_policy_schema_is_well_formed(self):
        validate.Draft202012Validator.check_schema(POLICY_SCHEMA)

    def test_representative_instance_conforms(self):
        self.assertEqual(_errors(POLICY_SCHEMA, VALID_FM), [])

    def test_established_by_optional_present_and_absent_both_conform(self):
        self.assertEqual(_errors(POLICY_SCHEMA, VALID_FM), [])                       # absent
        with_link = {**VALID_FM, "established_by": "eADR-0001"}
        self.assertEqual(_errors(POLICY_SCHEMA, with_link), [])                      # present

    def test_missing_required_field_is_rejected(self):
        for drop in ("title", "status", "date"):
            bad = {k: v for k, v in VALID_FM.items() if k != drop}
            self.assertNotEqual(_errors(POLICY_SCHEMA, bad), [], f"dropping {drop} should fail")

    def test_status_outside_the_decision_lifecycle_is_rejected(self):
        # the same 'decision' vocabulary contracts use; there is deliberately no 'rejected' state.
        for bad_status in ("rejected", "draft", "active", "Accepted"):
            bad = {**VALID_FM, "status": bad_status}
            self.assertNotEqual(_errors(POLICY_SCHEMA, bad), [], f"{bad_status} is not a lifecycle state")
        self.assertEqual(set(STATUS_ENUM), {"proposed", "accepted", "superseded"})

    def test_non_iso_date_is_rejected_by_pattern(self):
        # the pattern is the real enforcer; format:"date" is not asserted by the bundled validator.
        for bad_date in ("June 3", "2026-6-3", "06-03-2026", "2026/06/03", "2026-06-03T00:00:00Z"):
            bad = {**VALID_FM, "date": bad_date}
            self.assertNotEqual(_errors(POLICY_SCHEMA, bad), [], f"{bad_date} should fail the date pattern")

    def test_bad_established_by_pattern_is_rejected(self):
        bad = {**VALID_FM, "established_by": "eADR-1"}
        self.assertNotEqual(_errors(POLICY_SCHEMA, bad), [])

    def test_unknown_extra_field_is_rejected(self):
        bad = {**VALID_FM, "enforcement_tier": "posture"}   # enforcement tier is the body section, not frontmatter
        self.assertNotEqual(_errors(POLICY_SCHEMA, bad), [], "the schema is closed (additionalProperties false)")

    def test_schema_carries_no_id_field(self):
        # policies are slug-named (no numbered id scheme), unlike contracts' eADR-####.
        self.assertNotIn("id", POLICY_SCHEMA["properties"])


class TestTemplate(unittest.TestCase):
    def test_catalog_template_pointer_resolves_to_an_existing_file(self):
        catalog = validate.load_json(validate.CATALOG_PATH)
        pointer = catalog["surfaces"]["policy"]["template"]
        self.assertEqual(pointer, "../templates/policy.md")
        resolved = os.path.normpath(os.path.join(validate.SCHEMAS_DIR, pointer))
        self.assertTrue(os.path.isfile(resolved), f"{pointer} must resolve to a committed file")
        self.assertEqual(resolved, os.path.normpath(TEMPLATE_PATH))

    def test_template_body_has_exactly_the_required_sections_in_order(self):
        body = validate.read(TEMPLATE_PATH)
        self.assertEqual(validate.section_order(body),
                         SHAPE_RULE["params"]["required_sections"] + SHAPE_RULE["params"]["allowed_sections"])

    def test_template_frontmatter_shape_spec_matches_the_rule_params_no_drift(self):
        """The authoring scaffold the human reads and the machine-read rule cannot silently diverge."""
        self.assertEqual(_template_frontmatter_shape_spec(TEMPLATE_PATH), SHAPE_RULE["params"])

    def test_rule_params_are_a_well_formed_template_v1(self):
        self.assertEqual(_errors(TEMPLATE_SCHEMA, SHAPE_RULE["params"]), [])


class TestPolicyShapeRule(unittest.TestCase):
    def test_rule_is_well_formed_and_joins_ci(self):
        check_schema = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "check.v1.json"))
        self.assertEqual(_errors(check_schema, SHAPE_RULE), [])
        self.assertIn("CI", SHAPE_RULE.get("suites", []))
        self.assertEqual(SHAPE_RULE["kind"], "shape")
        self.assertEqual(SHAPE_RULE["target"], {"path": ".engine/policies/*.md"})
        self.assertEqual(SHAPE_RULE["tier"], "hard")

    def test_the_four_committed_policies_exist_and_pass_the_live_rule(self):
        slugs = {os.path.splitext(os.path.basename(p))[0] for p in glob.glob(os.path.join(POLICIES_DIR, "*.md"))}
        self.assertEqual(slugs, EXPECTED_POLICIES, "the four v1-core policies must be committed")
        passed, found = validate.kind_shape(SHAPE_RULE, {})        # the REAL rule over the REAL policies
        self.assertTrue(passed)
        self.assertEqual([f for f in found if f["severity"] == "hard"], [])

    def test_missing_required_section_is_a_hard_finding(self):
        body = VALID_BODY.replace("## Scope\nApplies to every decision made inside the engine.\n", "")
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "demo.md", body)
            passed, found = _run_kind(validate.kind_shape, SHAPE_RULE, [p])
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" and "Scope" in f["message"] for f in found))

    def test_out_of_order_sections_are_a_hard_finding(self):
        body = ("## Rule\nr\n## Rationale\nwhy\n## Scope\nwhere\n## Enforcement-tier\nposture\n")
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "demo.md", body)
            passed, found = _run_kind(validate.kind_shape, SHAPE_RULE, [p])
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" and "out of order" in f["message"] for f in found))

    def test_stray_section_is_a_hard_finding(self):
        body = VALID_BODY + "## Notes\nnot allowed here\n"
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "demo.md", body)
            passed, found = _run_kind(validate.kind_shape, SHAPE_RULE, [p])
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" and "does not allow" in f["message"] for f in found))

    def test_over_length_is_a_soft_nudge_not_a_block(self):
        body = VALID_BODY + "\n".join(f"filler line {i}" for i in range(200)) + "\n"
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "demo.md", body)
            passed, found = _run_kind(validate.kind_shape, SHAPE_RULE, [p])
        self.assertTrue(passed)
        self.assertTrue(any(f["severity"] == "soft" and "budget" in f["message"] for f in found))
        self.assertEqual([f for f in found if f["severity"] == "hard"], [])


class TestContractThresholdRule(unittest.TestCase):
    def test_rule_is_well_formed_and_joins_ci(self):
        check_schema = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "check.v1.json"))
        self.assertEqual(_errors(check_schema, CT_RULE), [])
        self.assertIn("CI", CT_RULE.get("suites", []))
        self.assertEqual(CT_RULE["kind"], "presence")
        self.assertEqual(CT_RULE["target"], {"path": ".engine/contracts/*.md"})
        self.assertEqual(CT_RULE["tier"], "hard")
        self.assertEqual(set(CT_RULE["params"]["sections"]), {"Significance", "Anti-choice"})

    def test_live_rule_is_green_on_the_empty_contract_stream(self):
        # the real rule over the real (empty) .engine/contracts/ — zero *.md matches, trivially green.
        passed, found = validate.kind_presence(CT_RULE, {})
        self.assertTrue(passed)
        self.assertEqual([f for f in found if f["severity"] == "hard"], [])

    def test_filled_significance_and_anti_choice_pass(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "eADR-0001-demo.md", CONTRACT_BODY_FILLED)
            passed, found = _run_kind(validate.kind_presence, CT_RULE, [p])
        self.assertTrue(passed)
        self.assertEqual([f for f in found if f["severity"] == "hard"], [])

    def test_placeholder_only_anti_choice_is_a_hard_finding(self):
        body = CONTRACT_BODY_FILLED.replace(
            "## Anti-choice\nA fat validator with hard-coded checks; rejected — it couples the kinds.\n",
            "## Anti-choice\n<Name the strongest alternative that was weighed and rejected.>\n")
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "eADR-0001-demo.md", body)
            passed, found = _run_kind(validate.kind_presence, CT_RULE, [p])
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" and "Anti-choice" in f["message"] for f in found))

    def test_blank_significance_is_a_hard_finding(self):
        body = CONTRACT_BODY_FILLED.replace(
            "## Significance\nIt constrains how every later check is added.\n", "## Significance\n\n")
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "eADR-0001-demo.md", body)
            passed, found = _run_kind(validate.kind_presence, CT_RULE, [p])
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" and "Significance" in f["message"] for f in found))


class TestCatalog(unittest.TestCase):
    def test_catalog_routes_policy_to_in_repo_schema_and_template(self):
        catalog = validate.load_json(validate.CATALOG_PATH)
        rec = catalog["surfaces"]["policy"]
        self.assertEqual(rec["governing_schema"], "policy.v1.json")
        self.assertEqual(rec["template"], "../templates/policy.md")
        self.assertEqual(rec["class"], "prose")
        self.assertEqual(rec["lifecycle"], "decision")
        self.assertEqual(rec["authority"], "standing-rules")


if __name__ == "__main__":
    unittest.main()
