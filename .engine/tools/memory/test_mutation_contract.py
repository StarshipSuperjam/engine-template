#!/usr/bin/env python3
"""Coverage and refusal tests for the persistent mutation registry (issue #1151 S02)."""
from __future__ import annotations

import ast
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import jsonschema

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import accepted_hook_dispatch
from memory import mutation_contract as contract


ROOT = Path(__file__).resolve().parents[3]
MEMORY = Path(__file__).resolve().parent
TOOLS = ROOT / ".engine/tools"
SCHEMA = ROOT / ".engine/schemas/persistent-mutation-registry.v1.json"

_CROSS_CUTTING_SURFACES = (
    "accepted_hook_dispatch", "boot", "boot_alarm_ledger", "checkout_auto_update", "close",
    "first_run_health", "hooks", "modes", "providers",
)


def _production_mutation_surfaces():
    surfaces = []
    for path in MEMORY.rglob("*.py"):
        if path.name.startswith("test_") or path.name in {"__init__.py", "mutation_contract.py"}:
            continue
        module = ".".join(path.relative_to(TOOLS).with_suffix("").parts)
        surfaces.append((str(path), module))
    surfaces.extend((str(TOOLS / f"{module}.py"), module) for module in _CROSS_CUTTING_SURFACES)
    return surfaces


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)


class TestMutationRegistryShape(unittest.TestCase):
    def test_schema_is_well_formed_and_the_canonical_registry_conforms(self):
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.validate(contract.document(), schema)

    def test_ids_writers_and_capability_identities_are_unique(self):
        for key in ("id", "writer", "capability_identity"):
            values = [entry[key] for entry in contract.REGISTRY]
            self.assertEqual(len(values), len(set(values)), key)

    def test_every_code_referent_names_a_real_function_in_the_declared_file(self):
        for entry in contract.REGISTRY:
            rel, function = entry["code_referent"].split(":", 1)
            path = ROOT / rel
            self.assertTrue(path.is_file(), entry["id"])
            if path.suffix == ".py":
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                functions = {node.name for node in ast.walk(tree)
                             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
                self.assertIn(function, functions, entry["id"])
            else:
                self.assertIn(function, path.read_text(encoding="utf-8"), entry["id"])

    def test_closed_vocabularies_match_the_schema(self):
        self.assertEqual(contract.EFFECT_CLASSES, {
            "semantic-read", "durable-append", "reversible-mutation", "destructive-irreversible"})
        self.assertEqual(contract.INVOCATION_MODES, {"automatic", "attended"})
        for entry in contract.REGISTRY:
            self.assertIn(entry["target_kind"], contract.TARGET_KINDS)
            self.assertIn(entry["effect_class"], contract.EFFECT_CLASSES)
            self.assertIn(entry["recovery_requirement"], contract.RECOVERY_REQUIREMENTS)
        self.assertEqual({entry["effect_class"] for entry in contract.REGISTRY}, contract.EFFECT_CLASSES)


class TestConfiguredMutationCoverage(unittest.TestCase):
    def test_dispatcher_and_registry_have_the_same_automatic_mutator_roster(self):
        self.assertEqual(set(contract.AUTOMATIC_ENTRYPOINTS), set(accepted_hook_dispatch.AUTOMATIC_MUTATORS))
        self.assertEqual(contract.automatic_coverage_failures(accepted_hook_dispatch.AUTOMATIC_MUTATORS), [])
        entries = {entry["id"]: entry for entry in contract.REGISTRY}
        referenced = set(contract.AUTOMATIC_COMMON_EFFECTS)
        for operation_ids in contract.AUTOMATIC_ENTRYPOINTS.values():
            referenced.update(operation_ids)
        for entry_id in referenced:
            with self.subTest(entry_id=entry_id):
                self.assertIn(entry_id, entries)
                self.assertIn("automatic", entries[entry_id]["allowed_invocation_modes"])

    def test_each_automatic_mutator_is_live_on_both_provider_surfaces(self):
        claude_docs = [
            json.loads((ROOT / ".claude/settings.json").read_text(encoding="utf-8")),
            json.loads((ROOT / ".engine/modules/memory-substrate-sqlite-fts5/manifest.json").read_text(
                encoding="utf-8")),
        ]
        codex_docs = [
            json.loads((ROOT / ".codex/hooks.json").read_text(encoding="utf-8")),
            claude_docs[1],
        ]
        claude = "\n".join(_strings(claude_docs))
        codex = "\n".join(_strings(codex_docs))
        for script in contract.AUTOMATIC_ENTRYPOINTS:
            with self.subTest(script=script):
                self.assertIn(script, claude)
                self.assertIn(script, codex)

    def test_composite_and_read_shaped_boundaries_have_closed_transitive_inventories(self):
        entries = {entry["id"]: entry for entry in contract.REGISTRY}
        writers = {entry["writer"] for entry in contract.REGISTRY}
        for boundary, entry_ids in contract.TRANSITIVE_BOUNDARIES.items():
            with self.subTest(boundary=boundary):
                self.assertIn(boundary, writers)
                self.assertTrue(entry_ids)
                self.assertEqual(len(entry_ids), len(set(entry_ids)))
                self.assertFalse(set(entry_ids) - entries.keys())

    def test_an_unregistered_hook_mutator_fails_coverage(self):
        configured = set(accepted_hook_dispatch.AUTOMATIC_MUTATORS) | {".engine/tools/memory/new_writer.py"}
        self.assertEqual(contract.automatic_coverage_failures(configured),
                         [".engine/tools/memory/new_writer.py"])


class TestLowLevelWriterCoverage(unittest.TestCase):
    def test_every_statically_visible_production_write_is_registered(self):
        self.assertEqual(contract.coverage_failures(_production_mutation_surfaces()), [])
        self.assertIn("hook-runner.runtime-health-marker", {entry["writer"] for entry in contract.REGISTRY})

    def test_an_unregistered_low_level_or_health_writer_fails_coverage(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "synthetic.py"
            path.write_text(
                "def new_writer(path):\n"
                "    with open(path, 'w', encoding='utf-8') as handle:\n"
                "        handle.write('x')\n",
                encoding="utf-8",
            )
            self.assertEqual(contract.coverage_failures([str(path)]), ["memory.synthetic.new_writer"])

    def test_os_open_and_sqlite_writes_cannot_hide_from_static_coverage(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "synthetic.py"
            path.write_text(
                "import os\n"
                "def flag_writer(path):\n"
                "    return os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)\n"
                "def database_writer(conn):\n"
                "    conn.execute('INSERT INTO entries VALUES (1)')\n",
                encoding="utf-8",
            )
            self.assertEqual(
                contract.coverage_failures([str(path)]),
                ["memory.synthetic.database_writer", "memory.synthetic.flag_writer"],
            )


class TestReadShapedMutationCoverage(unittest.TestCase):
    def test_keyword_and_semantic_public_search_boundaries_are_registered_as_mutations(self):
        by_writer = {entry["writer"]: entry for entry in contract.REGISTRY}
        expected = {
            "memory.mcp_server.search": "derived-index",
            "memory.index.search": "derived-index",
            "memory.mcp_server.recall_by_meaning": "semantic-index",
            "memory.semantic.store.search": "semantic-index",
        }
        for writer, target in expected.items():
            with self.subTest(writer=writer):
                self.assertEqual(by_writer[writer]["target_kind"], target)
                self.assertEqual(by_writer[writer]["effect_class"], "reversible-mutation")
                self.assertTrue(by_writer[writer]["schema_cutover"])

    def test_keyword_search_rebuilds_a_stale_derived_index(self):
        from memory import index, ledger

        if not index.fts5_available():
            self.skipTest("SQLite FTS5 is unavailable")
        with tempfile.TemporaryDirectory() as td:
            ledger_file = os.path.join(td, "ledger.ndjson")
            index_file = os.path.join(td, "index.sqlite3")
            ledger.append({"body": "qualified hook context"}, path=ledger_file)
            index.rebuild(ledger_file=ledger_file, index_file=index_file)
            with sqlite3.connect(index_file) as conn:
                conn.execute("UPDATE meta SET schema_version = 0 WHERE rowid = 1")
                conn.commit()
            result = index.search("qualified", ledger_file=ledger_file, index_file=index_file)
            self.assertTrue(result.records)
            with sqlite3.connect(index_file) as conn:
                current = conn.execute("SELECT schema_version FROM meta WHERE rowid = 1").fetchone()[0]
            self.assertEqual(current, index.INDEX_SCHEMA_VERSION)


class TestFailClosedClassification(unittest.TestCase):
    def test_exact_registered_shape_resolves_one_entry(self):
        entry = contract.classify(
            writer="memory.ledger.append", target_kind="ledger", effect_class="durable-append",
            invocation_mode="automatic", measured_cardinality=1,
        )
        self.assertEqual(entry["id"], "ledger-append")

    def test_unknown_writer_target_effect_or_mode_refuses(self):
        base = dict(writer="memory.ledger.append", target_kind="ledger", effect_class="durable-append",
                    invocation_mode="automatic", measured_cardinality=1)
        cases = (
            {**base, "writer": "memory.ledger.unknown"},
            {**base, "target_kind": "remote-vault"},
            {**base, "effect_class": "reversible-mutation"},
            {**base, "invocation_mode": "attended-only-if-convenient"},
        )
        for case in cases:
            with self.subTest(case=case), self.assertRaises(contract.MutationContractError):
                contract.classify(**case)

    def test_understated_cardinality_and_undeclared_schema_cutover_refuse(self):
        base = dict(writer="memory.ledger.append", target_kind="ledger", effect_class="durable-append",
                    invocation_mode="automatic")
        with self.assertRaises(contract.MutationContractError):
            contract.classify(**base, measured_cardinality=2)
        with self.assertRaises(contract.MutationContractError):
            contract.classify(**base, measured_cardinality=1, schema_cutover=True)

    def test_every_registry_entry_has_a_classifiable_boundary_witness(self):
        for entry in contract.REGISTRY:
            maximum = entry["declared_cardinality"]["maximum"]
            measured = 1 if maximum is None else min(maximum, 1)
            for mode in entry["allowed_invocation_modes"]:
                with self.subTest(entry=entry["id"], mode=mode):
                    resolved = contract.classify(
                        writer=entry["writer"], target_kind=entry["target_kind"],
                        effect_class=entry["effect_class"], invocation_mode=mode,
                        measured_cardinality=measured, schema_cutover=entry["schema_cutover"],
                    )
                    self.assertEqual(resolved["capability_identity"], entry["capability_identity"])


if __name__ == "__main__":
    unittest.main()
