#!/usr/bin/env python3
"""Authority, under-lock drift, and converted-call-graph tests for issue #1151 S04."""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import execution_context, ledger, mutation_authority, mutation_contract


TOOLS = Path(__file__).resolve().parents[1]
GUARDED_MODULES = (
    "memory.ledger", "memory.capture", "memory.compact", "memory.backup_vault",
    "memory.restore_vault", "memory.pins", "memory.forget", "memory.erase", "memory.rescrub",
    "memory.index", "memory.mcp_server", "memory.erasure_observer", "memory.export",
    "memory.semantic.store", "close", "boot",
)


class _QualifiedFixture:
    def __init__(self, *, automatic: bool = False):
        self.temp = tempfile.TemporaryDirectory(prefix="engine-authority-")
        self.base = os.path.realpath(self.temp.name)
        self.root = os.path.join(self.base, "project")
        self.common = os.path.join(self.base, "common.git")
        self.memory = os.path.join(self.root, ".engine", "memory")
        self.pointer = os.path.join(self.root, ".engine", "memory-backup", "pointer.json")
        self.accepted = os.path.join(
            self.common, "engine", "accepted-hooks", "trees", f"{'a' * 40}-{'b' * 40}")
        os.makedirs(self.memory)
        os.makedirs(os.path.join(self.accepted, ".engine", "tools"))
        Path(os.path.join(self.accepted, ".engine", "tools", "accepted_hook_dispatch.py")).write_text(
            "# accepted fixture\n", encoding="utf-8")
        os.makedirs(os.path.dirname(self.pointer))
        Path(self.pointer).write_text('{"schema_version":1,"configured":false}\n', encoding="utf-8")
        activation = os.path.join(self.common, "engine", "accepted-hooks", "activation.json")
        os.makedirs(os.path.dirname(activation), exist_ok=True)
        Path(activation).write_text(json.dumps({
            "schema_version": "accepted-hook-activation.v1", "repository": "owner/repo",
            "commit": "a" * 40, "tree": "b" * 40, "engine_release": "9.9.9", "epoch": 1,
            "source": "reviewed-merge", "source_ref": "refs/heads/main",
            "activated_at": "2026-01-01T00:00:00Z",
        }, sort_keys=True) + "\n", encoding="utf-8")
        bootstrap = execution_context._fixture_bootstrap(
            self.root, self.common, pointer_digest=execution_context._file_digest(self.pointer))
        arguments = {
            "bootstrap": bootstrap, "accepted_tree": self.accepted, "provider": "codex",
            "run_id": "run", "task_id": "task",
            "identity_initializer": execution_context._fixture_identity_initializer,
        }
        if automatic:
            arguments.update({"script": ".engine/tools/close.py"})
        else:
            arguments.update({"script": ".engine/tools/memory/pins.py", "operation_id": "ledger-append"})
        self.context = execution_context.resolve_execution_context(**arguments)

    def install(self):
        execution_context._CURRENT_CONTEXT = self.context
        os.environ[execution_context.CONTEXT_ENV] = self.context.to_json()

    def cleanup(self):
        mutation_authority.set_after_lock_test_hook(None)
        mutation_authority._THREAD.state = None
        execution_context._CURRENT_CONTEXT = None
        os.environ.pop(execution_context.CONTEXT_ENV, None)
        self.temp.cleanup()


class ConvertedCallGraphTests(unittest.TestCase):
    def test_every_in_scope_mutating_registry_referent_is_guarded(self):
        missing = []
        guarded = []
        for module_name in GUARDED_MODULES:
            module = importlib.import_module(module_name)
            for entry in mutation_contract.REGISTRY:
                writer_module, _, function_name = entry["writer"].rpartition(".")
                if (writer_module != module_name or entry["effect_class"] == "semantic-read"
                        or entry["id"] == "capture-lock-create"):
                    continue
                function = getattr(module, function_name, None)
                if getattr(function, "__engine_registry_id__", None) != entry["id"]:
                    missing.append(entry["writer"])
                else:
                    guarded.append(entry["id"])
        self.assertEqual(missing, [])
        self.assertGreaterEqual(len(guarded), 72)

    def test_registry_modes_are_closed_over_registered_call_edges(self):
        by_writer = {entry["writer"]: entry for entry in mutation_contract.REGISTRY}
        conflicts = []
        for child in mutation_contract.REGISTRY:
            for caller in child.get("callers", ()):
                parent = by_writer.get(caller)
                if parent:
                    missing = set(parent["allowed_invocation_modes"]) - set(child["allowed_invocation_modes"])
                    if missing:
                        conflicts.append(f"{parent['id']}->{child['id']}:{sorted(missing)}")
        self.assertEqual(conflicts, [])

    def test_every_registry_entry_has_positive_and_negative_classification_witnesses(self):
        for entry in mutation_contract.REGISTRY:
            measured = entry["declared_cardinality"]["maximum"] or 1
            for mode in entry["allowed_invocation_modes"]:
                with self.subTest(entry=entry["id"], mode=mode, witness="positive"):
                    classified = mutation_contract.classify(
                        writer=entry["writer"], target_kind=entry["target_kind"],
                        effect_class=entry["effect_class"], invocation_mode=mode,
                        measured_cardinality=measured, schema_cutover=entry["schema_cutover"],
                    )
                    self.assertEqual(classified["id"], entry["id"])
            refused_mode = "attended" if "attended" not in entry["allowed_invocation_modes"] else (
                "automatic" if "automatic" not in entry["allowed_invocation_modes"] else "unknown")
            with self.subTest(entry=entry["id"], witness="wrong-mode"):
                with self.assertRaises(mutation_contract.MutationContractError):
                    mutation_contract.classify(
                        writer=entry["writer"], target_kind=entry["target_kind"],
                        effect_class=entry["effect_class"], invocation_mode=refused_mode,
                        measured_cardinality=measured, schema_cutover=entry["schema_cutover"],
                    )
            maximum = entry["declared_cardinality"]["maximum"]
            if maximum is not None:
                with self.subTest(entry=entry["id"], witness="cardinality-overrun"):
                    with self.assertRaises(mutation_contract.MutationContractError):
                        mutation_contract.classify(
                            writer=entry["writer"], target_kind=entry["target_kind"],
                            effect_class=entry["effect_class"],
                            invocation_mode=entry["allowed_invocation_modes"][0],
                            measured_cardinality=maximum + 1,
                            schema_cutover=entry["schema_cutover"],
                        )

    def test_context_free_production_process_refuses_before_payload_creation(self):
        script = (
            "import os,sys,tempfile; sys.path.insert(0," + repr(str(TOOLS)) + "); "
            "from memory import ledger; d=tempfile.mkdtemp(); p=os.path.join(d,'ledger.ndjson'); "
            "\ntry: ledger.append({'body':'forbidden'},path=p)\n"
            "except Exception as e: print(type(e).__name__, os.path.exists(p))\n"
            "else: print('UNEXPECTED', os.path.exists(p))\n"
        )
        env = dict(os.environ)
        env.pop(execution_context.CONTEXT_ENV, None)
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "MutationAuthorityError False")


class LockedAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = _QualifiedFixture()
        self.fixture.install()

    def tearDown(self):
        self.fixture.cleanup()

    def test_exact_context_capability_allows_one_locked_append(self):
        target = os.path.join(self.fixture.memory, "ledger.ndjson")
        ledger.append({"body": "qualified"}, path=target)
        self.assertEqual(ledger.read(path=target).records, [{"body": "qualified"}])

    def test_a_second_outer_write_with_the_stale_context_refuses_without_appending(self):
        target = os.path.join(self.fixture.memory, "ledger.ndjson")
        ledger.append({"body": "first"}, path=target)
        before = Path(target).read_bytes()
        with self.assertRaisesRegex(mutation_authority.MutationAuthorityError, "stale"):
            ledger.append({"body": "second"}, path=target)
        self.assertEqual(Path(target).read_bytes(), before)

    def test_drift_injected_after_lock_acquisition_refuses_before_payload_write(self):
        target = os.path.join(self.fixture.memory, "ledger.ndjson")
        meta = os.path.join(self.fixture.memory, "ledger-meta.json")

        def drift():
            Path(meta).write_text('{"generation":7,"index_epoch":0}\n', encoding="utf-8")

        mutation_authority.set_after_lock_test_hook(drift)
        with self.assertRaisesRegex(mutation_authority.MutationAuthorityError, "under-lock.*stale"):
            ledger.append({"body": "must not land"}, path=target)
        self.assertFalse(os.path.exists(target))

    def test_explicit_path_outside_the_bound_store_refuses(self):
        escaped = os.path.join(self.fixture.base, "escaped.ndjson")
        with self.assertRaisesRegex(mutation_authority.MutationAuthorityError, "escapes"):
            ledger.append({"body": "must not land"}, path=escaped)
        self.assertFalse(os.path.exists(escaped))

    def test_consumed_capability_cannot_be_reused_at_the_writer(self):
        target = os.path.join(self.fixture.memory, "ledger.ndjson")
        context = self.fixture.context
        capability = execution_context.mint_capability(
            context, registry_id="ledger-append", measured_cardinality=1)
        operation = mutation_contract.entry_by_id("ledger-append")
        execution_context.consume_capability(
            capability, context=context, writer=operation["writer"], target_kind=operation["target_kind"],
            effect_class=operation["effect_class"], invocation_mode="attended", measured_cardinality=1,
            schema_cutover=operation["schema_cutover"],
            observed_state_fingerprint=context.expected_state_fingerprint,
        )
        with self.assertRaisesRegex(mutation_authority.MutationAuthorityError, "already consumed"):
            ledger.append({"body": "must not land"}, path=target, _engine_capability=capability)
        self.assertFalse(os.path.exists(target))

    def test_automatic_composite_consumes_distinct_nested_subgrants_under_one_lock(self):
        self.fixture.cleanup()
        self.fixture = _QualifiedFixture(automatic=True)
        self.fixture.install()
        target = os.path.join(self.fixture.memory, "ledger.ndjson")
        with mutation_authority.mutation_scope("automatic-close-operation", (), {}):
            with mutation_authority.mutation_scope("automatic-capture", (), {}):
                ledger.append({"body": "nested qualified"}, path=target)
        self.assertEqual(ledger.read(path=target).records, [{"body": "nested qualified"}])


if __name__ == "__main__":
    unittest.main()
