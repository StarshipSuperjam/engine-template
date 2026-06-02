#!/usr/bin/env python3
"""Self-tests for slice 11a — the interface surface: the interface.v1 declaration grammar, the
committed knowledge-retrieval declaration, the schema-kind validation rule, and the single-active /
conformance coherence leg (validate.interface_resolution_findings).

Run: uv run --directory .engine -- python -m unittest discover -s tools -p 'test_*.py'

These lock: interface.v1 is a well-formed schema with teeth (a malformed declaration is rejected);
the committed knowledge-retrieval declaration conforms and pins the D-116 op-set EXACTLY (a dropped or
renamed operation fails this suite, not only review); each operation's inline input/output schema is
itself a well-formed JSON Schema; the catalog-resolved schema-kind rule joins CI and passes on the real
declaration; and the coherence leg fires single-active (>1 non-default → hard) + conformance (missing
op → hard) and treats an absent named fallback as an expected-pending NOTE, never drift.
"""
from __future__ import annotations
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402

INTERFACE_SCHEMA = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "interface.v1.json"))
KR_PATH = os.path.join(validate.ENGINE_DIR, "interfaces", "knowledge-retrieval.json")
KR = validate.load_json(KR_PATH)
RULE_PATH = os.path.join(validate.CHECK_DIR, "interface-declaration.json")
D116_OPS = {"get-entity", "find", "neighbors", "relate"}


def _errors(schema, instance):
    return list(validate.Draft202012Validator(schema).iter_errors(instance))


class TestSchema(unittest.TestCase):
    def test_interface_schema_is_well_formed(self):
        validate.Draft202012Validator.check_schema(INTERFACE_SCHEMA)

    def test_declaration_conforms(self):
        self.assertEqual(_errors(INTERFACE_SCHEMA, KR), [])

    def test_malformed_declaration_is_rejected(self):
        """The grammar has teeth: a declaration missing 'operations' or 'fallback' is refused."""
        for drop in ("operations", "fallback", "id"):
            bad = {k: v for k, v in KR.items() if k != drop}
            self.assertNotEqual(_errors(INTERFACE_SCHEMA, bad), [], f"dropping {drop} should fail")
        bad_op = json.loads(json.dumps(KR))
        bad_op["operations"][0].pop("input_schema")
        self.assertNotEqual(_errors(INTERFACE_SCHEMA, bad_op), [], "an op missing input_schema must fail")

    def test_each_operation_io_schema_is_well_formed(self):
        for op in KR["operations"]:
            validate.Draft202012Validator.check_schema(op["input_schema"])
            validate.Draft202012Validator.check_schema(op["output_schema"])


class TestDeclaration(unittest.TestCase):
    def test_id_matches_filename_stem(self):
        self.assertEqual(KR["id"], os.path.splitext(os.path.basename(KR_PATH))[0])

    def test_op_set_is_exactly_d116(self):
        """Pin the locked op-set: dropping/renaming/adding an operation is caught here, not only review."""
        self.assertEqual({op["name"] for op in KR["operations"]}, D116_OPS)

    def test_fallback_is_the_engine_prefixed_mcp_handle(self):
        self.assertEqual(KR["fallback"]["kind"], "mcp")
        self.assertEqual(KR["fallback"]["handle"], "engine-knowledge-graph")
        self.assertTrue(KR["fallback"]["handle"].startswith("engine-"))

    def test_status_active(self):
        self.assertEqual(KR["status"], "active")


class TestCheckRule(unittest.TestCase):
    def test_rule_is_well_formed_and_joins_ci(self):
        check_schema = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "check.v1.json"))
        rule = validate.load_json(RULE_PATH)
        self.assertEqual(_errors(check_schema, rule), [])
        self.assertIn("CI", rule.get("suites", []))
        self.assertEqual(rule["kind"], "schema")
        self.assertEqual(rule["target"], {"path": ".engine/interfaces/*.json"})

    def test_live_check_passes_on_the_real_declaration(self):
        passed, found = validate.kind_schema(validate.load_json(RULE_PATH), {})
        self.assertTrue(passed)
        self.assertEqual([f for f in found if f["severity"] == "hard"], [])

    def test_catalog_resolves_interface_to_in_repo_schema(self):
        """The flip None -> interface.v1.json is load-bearing: without it kind_schema would silently
        skip interface declarations (governing_schema None -> nothing to check)."""
        catalog = validate.load_json(validate.CATALOG_PATH)
        self.assertEqual(catalog["surfaces"]["interface"]["governing_schema"], "interface.v1.json")


class TestResolutionLeg(unittest.TestCase):
    """validate.interface_resolution_findings — single-active + conformance + expected-pending fallback."""

    def _ops(self):
        return [op["name"] for op in KR["operations"]]

    def test_core_reality_no_findings(self):
        # the shipped fallback is present, no non-default implementation -> the steady state, silent
        f = validate.interface_resolution_findings([KR], {}, {"engine-knowledge-graph"}, "hard", "m")
        self.assertEqual(f, [])

    def test_two_non_default_implementations_is_hard(self):
        impls = {"knowledge-retrieval": [
            {"handle": "engine-a", "operations": self._ops()},
            {"handle": "engine-b", "operations": self._ops()}]}
        f = validate.interface_resolution_findings(
            [KR], impls, {"engine-knowledge-graph", "engine-a", "engine-b"}, "hard", "m")
        self.assertTrue(any(x["severity"] == "hard" for x in f))
        self.assertTrue(any("more than one implementation" in x["message"] for x in f))

    def test_non_conforming_implementation_is_hard(self):
        impls = {"knowledge-retrieval": [{"handle": "engine-a", "operations": ["get-entity"]}]}
        f = validate.interface_resolution_findings(
            [KR], impls, {"engine-knowledge-graph", "engine-a"}, "hard", "m")
        self.assertTrue(any(x["severity"] == "hard" and "does not provide" in x["message"] for x in f))

    def test_absent_named_fallback_is_a_note_not_hard(self):
        # a protocol-only interface whose fallback ships with a later module (the 11b `search` case)
        search = {"id": "search", "operations": [{"name": "search"}],
                  "fallback": {"kind": "mcp", "handle": "engine-memory-search"}}
        f = validate.interface_resolution_findings([search], {}, set(), "hard", "m")
        self.assertEqual([x["severity"] for x in f], ["note"])
        self.assertTrue(any("pending-setup" in x["message"] for x in f))


if __name__ == "__main__":
    unittest.main()
