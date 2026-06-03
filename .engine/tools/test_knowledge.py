#!/usr/bin/env python3
"""Self-tests for slice 10 — the knowledge graph: the knowledge.v1 schema, the generic
catalog-driven generator, the committed graph, and the coverage/fingerprint relay gate.

Run: uv run --directory .engine -- python -m unittest discover -s tools -p 'test_*.py'

These lock the load-bearing teeth: the pure derivation is deterministic and produces well-shaped,
schema-conforming, referentially-intact entities (every predicate target resolves to an entity, ids
are unique, id == type:slug); coverage is TOTAL (every owned engine file under a catalogued surface
has an entity) and the derived-observational graph.json is NOT itself an entity (no recursion); the
conformance strips hold (no debt/finding/session types, no hand-authored predicates); generate->check
round-trips and a hand-edit/absence is REFUSED as a hard finding; the committed graph is in sync with
the live sources (so a forgotten regen fails this suite, not only CI); and the committed
`coverage`/`mode:fingerprint` rule, via validate.kind_coverage, passes the real graph, bites on a
stale/missing graph, fails closed on a broken generator, and the unknown-mode tail is intact.
"""
from __future__ import annotations
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402
import knowledge_gen     # noqa: E402

KNOWLEDGE_SCHEMA = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "knowledge.v1.json"))
RULE_PATH = os.path.join(validate.CHECK_DIR, "knowledge-coverage.json")
ID_RE = r"^(contract|policy|schema|check|tool|operation|skill|agent|interface|doc|state|module):[A-Za-z0-9._-]+$"


def _errors(schema, instance):
    return list(validate.Draft202012Validator(schema).iter_errors(instance))


def _live_entities():
    return knowledge_gen.derive_entities(*knowledge_gen.load_sources())


class TestHelpers(unittest.TestCase):
    def test_slug_is_the_filename_stem(self):
        self.assertEqual(knowledge_gen._slug(".engine/check/state-cursor.json"), "state-cursor")
        self.assertEqual(knowledge_gen._slug(".engine/schemas/check.v1.json"), "check.v1")
        self.assertEqual(knowledge_gen._slug(".engine/tools/validate.py"), "validate")

    def test_surface_for_longest_prefix(self):
        surfaces = {"check": {"location": ".engine/check/"}, "schema": {"location": ".engine/schemas/"}}
        self.assertEqual(knowledge_gen._surface_for(".engine/check/x.json", surfaces), "check")
        self.assertEqual(knowledge_gen._surface_for(".engine/schemas/x.json", surfaces), "schema")
        self.assertIsNone(knowledge_gen._surface_for(".engine/engine.json", surfaces))
        self.assertIsNone(knowledge_gen._surface_for(".engine/knowledge/graph.json", surfaces))


class TestSchema(unittest.TestCase):
    def test_schema_is_well_formed(self):
        validate.Draft202012Validator.check_schema(KNOWLEDGE_SCHEMA)


class TestLiveDerivation(unittest.TestCase):
    """derive_entities over the real catalog/inventory/manifests/claims."""

    def setUp(self):
        self.entities = _live_entities()
        self.by_id = {e["id"]: e for e in self.entities}

    def test_canonical_graph_conforms_to_its_schema(self):
        graph = json.loads(knowledge_gen.canonical_graph())
        self.assertEqual(_errors(KNOWLEDGE_SCHEMA, graph), [])

    def test_ids_are_unique(self):
        ids = [e["id"] for e in self.entities]
        self.assertEqual(len(ids), len(set(ids)), "entity ids must be unique")

    def test_id_equals_type_colon_slug(self):
        for e in self.entities:
            self.assertEqual(e["id"], f'{e["type"]}:{e["slug"]}', e["id"])

    def test_referential_integrity_every_edge_target_resolves(self):
        """The gate cannot catch a consistently-wrong edge (committed == re-derived); this can."""
        ids = set(self.by_id)
        for e in self.entities:
            for kind, targets in (e.get("predicates") or {}).items():
                for t in targets:
                    self.assertIn(t, ids, f"{e['id']} {kind} -> dangling target {t}")

    def test_total_coverage_every_owned_surface_file_has_an_entity(self):
        catalog, manifests, inventory, claims = knowledge_gen.load_sources()
        surfaces = catalog.get("surfaces", {})
        expected = {rel for rel in inventory
                    if knowledge_gen._surface_for(rel, surfaces) and claims.get(rel)}
        got = {e["source"]["path"] for e in self.entities if e["type"] != "module"}
        self.assertEqual(got, expected)

    def test_source_fingerprints_are_sha256(self):
        import re
        for e in self.entities:
            self.assertRegex(e["source"]["fingerprint"], r"^sha256:[0-9a-f]{64}$", e["id"])

    def test_graph_json_is_not_itself_an_entity(self):
        for e in self.entities:
            self.assertNotEqual(e["source"]["path"], ".engine/knowledge/graph.json")

    def test_no_stripped_entity_types_or_predicates(self):
        for e in self.entities:
            self.assertNotIn(e["type"], {"integration-debt", "audit-finding", "session-claim", "feature"})
            self.assertNotIn("pushback_drawers", e.get("predicates") or {})

    def test_known_edges_for_the_state_cursor_and_its_check(self):
        # the committed state-cursor check governs nothing but is governed by check.v1 and targets the cursor
        chk = self.by_id.get("check:state-cursor")
        self.assertIsNotNone(chk, "expected a check:state-cursor entity")
        self.assertEqual(chk["predicates"].get("governed_by"), ["schema:check.v1"])
        self.assertEqual(chk["predicates"].get("targets"), ["state:state"])
        self.assertEqual(chk["predicates"].get("provided_by"), ["module:core"])
        # the state cursor instance is governed by state.v1 and owned by core
        st = self.by_id.get("state:state")
        self.assertIsNotNone(st, "expected a state:state entity")
        self.assertEqual(st["predicates"].get("governed_by"), ["schema:state.v1"])
        self.assertEqual(st["predicates"].get("provided_by"), ["module:core"])
        # the module entity exists
        self.assertIn("module:core", self.by_id)

    def test_known_edges_for_the_interfaces_and_their_declaration_check(self):
        # slices 11a/11b: each interface declaration is governed by interface.v1 (the catalog
        # governing_schema flip) and provided by core; the interface-declaration check targets BOTH.
        # The non-fingerprint correlate for edge correctness — the fingerprint gate proves the graph
        # MATCHES the surfaces, never that the derived edges are right; this asserts the edges.
        for iid in ("interface:knowledge-retrieval", "interface:search"):
            iface = self.by_id.get(iid)
            self.assertIsNotNone(iface, f"expected an {iid} entity")
            self.assertEqual(iface["predicates"].get("governed_by"), ["schema:interface.v1"], iid)
            self.assertEqual(iface["predicates"].get("provided_by"), ["module:core"], iid)
        self.assertIn("schema:interface.v1", self.by_id)
        chk = self.by_id.get("check:interface-declaration")
        self.assertIsNotNone(chk, "expected a check:interface-declaration entity")
        # the check globs .engine/interfaces/*.json, so its derived `targets` are BOTH declarations
        # (sorted) — adding search.json widened this edge, the 11b graph delta the operator eyeballs.
        self.assertEqual(chk["predicates"].get("targets"),
                         ["interface:knowledge-retrieval", "interface:search"])
        self.assertEqual(chk["predicates"].get("governed_by"), ["schema:check.v1"])

    def test_schema_surface_files_have_no_governed_by(self):
        """A schema file's governing authority is the external 2020-12 dialect (a URI), not an in-repo
        schema entity, so it carries no governed_by edge — by design, not a gap."""
        for e in self.entities:
            if e["type"] == "schema":
                self.assertNotIn("governed_by", e.get("predicates") or {}, e["id"])

    def test_catalog_data_files_are_schema_entities(self):
        """A deliberate, pinned choice: the catalog data file and its meta-schema live under the
        schema surface location, so they appear as schema entities (mechanical/total coverage),
        not silently excluded."""
        self.assertIn("schema:surface-catalog", self.by_id)
        self.assertIn("schema:surface-catalog.schema", self.by_id)

    def test_expected_entities_are_present(self):
        """Concrete spot-checks, independent of the _surface_for oracle the total-coverage test
        reuses — so a classification bug cannot pass both tests."""
        for eid in ("tool:validate", "tool:knowledge_gen", "schema:check.v1", "schema:knowledge.v1",
                    "check:knowledge-coverage", "state:state", "module:core"):
            self.assertIn(eid, self.by_id, eid)


class TestCommittedGraph(unittest.TestCase):
    """The committed .engine/knowledge/graph.json — conforms to its schema and is in sync with the
    live sources (so a forgotten regenerate fails THIS suite, not only CI)."""

    def test_committed_graph_conforms(self):
        committed = knowledge_gen.read_committed(knowledge_gen.GRAPH_PATH)
        self.assertIsNotNone(committed, "the knowledge graph must be generated and committed")
        self.assertEqual(_errors(KNOWLEDGE_SCHEMA, json.loads(committed)), [])

    def test_committed_graph_is_in_sync_with_sources(self):
        f = knowledge_gen.check()
        self.assertEqual(f["severity"], "note",
                         "the committed graph is stale — run knowledge_gen.py generate and commit")


class TestGenerateCheckIO(unittest.TestCase):
    """Real sources + a redirected committed path in a temp dir."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "graph.json")

    def tearDown(self):
        self._tmp.cleanup()

    def test_generate_then_check_is_in_sync(self):
        g = knowledge_gen.generate(self.path)
        self.assertEqual(g["severity"], "note")
        self.assertIn("Wrote", g["message"])
        self.assertEqual(knowledge_gen.check(self.path)["severity"], "note")

    def test_generate_is_idempotent(self):
        knowledge_gen.generate(self.path)
        again = knowledge_gen.generate(self.path)
        self.assertIn("already up to date", again["message"])

    def test_hand_edit_is_caught_as_drift(self):
        knowledge_gen.generate(self.path)
        with open(self.path, "a", encoding="utf-8", newline="") as fh:
            fh.write("junk a human typed\n")
        self.assertEqual(knowledge_gen.check(self.path)["severity"], "hard")

    def test_absent_graph_is_caught(self):
        self.assertEqual(knowledge_gen.check(os.path.join(self._tmp.name, "nope.json"))["severity"], "hard")

    def test_drift_tier_is_the_callers_tier(self):
        knowledge_gen.generate(self.path)
        with open(self.path, "a", encoding="utf-8", newline="") as fh:
            fh.write("junk\n")
        self.assertEqual(knowledge_gen.check(self.path, tier="soft")["severity"], "soft")


class TestCoverageFingerprintMode(unittest.TestCase):
    """The committed rule, dispatched through validate.kind_coverage (no _run_kind stub needed — the
    fingerprint mode ignores target_files and re-derives from the catalog)."""

    def _rule(self):
        return validate.load_json(RULE_PATH)

    def test_rule_is_well_formed_and_joins_ci(self):
        check_schema = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "check.v1.json"))
        rule = self._rule()
        self.assertEqual(_errors(check_schema, rule), [])
        self.assertIn("CI", rule.get("suites", []))
        self.assertEqual(rule["params"]["mode"], "fingerprint")
        self.assertEqual(rule["target"], {"context": "knowledge-fingerprint"})

    def test_real_graph_passes_via_the_mode(self):
        passed, found = validate.kind_coverage(self._rule(), {})
        self.assertTrue(passed)
        self.assertEqual(found, [])

    def test_stale_committed_graph_is_hard(self):
        # mock.patch.object guarantees GRAPH_PATH is restored even on an unexpected path.
        with tempfile.TemporaryDirectory() as d:
            stale = os.path.join(d, "graph.json")
            knowledge_gen.write_graph('{"schema_version": 1, "entities": []}\n', stale)
            with mock.patch.object(knowledge_gen, "GRAPH_PATH", stale):
                passed, found = validate.kind_coverage(self._rule(), {})
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" for f in found))
        self.assertTrue(any("out of date" in f["message"] for f in found))

    def test_missing_committed_graph_is_hard(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(knowledge_gen, "GRAPH_PATH", os.path.join(d, "nope.json")):
                passed, found = validate.kind_coverage(self._rule(), {})
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" for f in found))

    def test_broken_generator_fails_closed(self):
        def boom(*a, **k):
            raise RuntimeError("simulated generator failure")

        with mock.patch.object(knowledge_gen, "check", boom):
            passed, found = validate.kind_coverage(self._rule(), {})
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" for f in found))

    def test_unrecognized_mode_still_fails_closed(self):
        passed, found = validate.kind_coverage(
            {"id": "x", "tier": "hard", "params": {"mode": "bogus"}}, {})
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" for f in found))


if __name__ == "__main__":
    unittest.main()
