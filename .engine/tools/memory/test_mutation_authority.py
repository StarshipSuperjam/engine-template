#!/usr/bin/env python3
"""Authority, under-lock drift, and converted-call-graph tests for issue StarshipSuperjam/engine-template#1151 S04."""
from __future__ import annotations

import importlib
import gc
import inspect
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import capture, execution_context, ledger, mutation_authority, mutation_contract, records
import first_run_health


TOOLS = Path(__file__).resolve().parents[1]
NATIVE_TRUST_ROOTS = frozenset({
    # These two seams establish or enter the qualified interpreter before ordinary Python writers run.
    "accepted_hook_dispatch", "hook-runner",
})


class _QualifiedFixture:
    def __init__(self, *, automatic: bool = False, mcp: bool = False):
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
            "authority": {"kind": "github-merged-pull", "evidence_id": "42"},
            "activated_at": "2026-01-01T00:00:00Z",
        }, sort_keys=True) + "\n", encoding="utf-8")
        bootstrap = execution_context._fixture_bootstrap(
            self.root, self.common, pointer_digest=execution_context._file_digest(self.pointer))
        arguments = {
            "bootstrap": bootstrap, "accepted_tree": self.accepted, "provider": "codex",
            "run_id": "run", "task_id": "task",
            "identity_initializer": execution_context._fixture_identity_initializer,
        }
        if mcp:
            arguments.update({"script": ".engine/tools/memory/mcp_server.py",
                              "operation_id": "attended-memory-mcp"})
        elif automatic:
            arguments.update({"script": ".engine/tools/close.py"})
        else:
            arguments.update({"script": ".engine/tools/memory/pins.py", "operation_id": "ledger-append"})
        self.context = execution_context.resolve_execution_context(**arguments)

    def install(self):
        execution_context._CURRENT_CONTEXT = self.context
        os.environ[execution_context.CONTEXT_ENV] = self.context.to_json()
        os.environ[ledger.ENV_DIR] = self.memory

    def cleanup(self):
        mutation_authority.set_after_lock_test_hook(None)
        mutation_authority._THREAD.state = None
        execution_context._CURRENT_CONTEXT = None
        os.environ.pop(execution_context.CONTEXT_ENV, None)
        os.environ.pop(ledger.ENV_DIR, None)
        self.temp.cleanup()


class ConvertedCallGraphTests(unittest.TestCase):
    def test_pre_activation_first_run_hint_is_registered_but_not_shared_memory(self):
        entries = {entry["writer"]: entry for entry in mutation_contract.REGISTRY}
        marker = entries["first_run_health.mark_first_run_applied"]
        self.assertEqual(marker["id"], "attended-first-run-marker-stage")
        self.assertEqual(marker["target_kind"], "lifecycle-marker")
        self.assertEqual(marker["allowed_invocation_modes"], ["attended"])
        self.assertIn("first_run_health.clear_first_run_marker", entries)
        self.assertEqual(
            first_run_health._LANDING_MARKER_REL,
            os.path.join(".engine", "boot", ".cache", "first-run-landing.json"),
        )

    def test_every_in_scope_mutating_registry_referent_is_guarded(self):
        missing = []
        guarded = []
        module_names = sorted({
            entry["writer"].rpartition(".")[0]
            for entry in mutation_contract.REGISTRY
            if entry["effect_class"] != "semantic-read"
            and entry["writer"].rpartition(".")[0] not in NATIVE_TRUST_ROOTS
        })
        for module_name in module_names:
            module = importlib.import_module(module_name)
            for entry in mutation_contract.REGISTRY:
                writer_module, _, function_name = entry["writer"].rpartition(".")
                # Both skips are the SAME mechanism, `mutation_authority._SKIP_WRAPPERS`: an entry whose
                # authority is deliberately carried somewhere other than an auto-installed wrapper on the
                # named writer. Reading the set rather than restating it means a future skip cannot be added
                # without this test seeing it.
                if (writer_module != module_name or entry["effect_class"] == "semantic-read"
                        or entry["id"] in mutation_authority._SKIP_WRAPPERS):
                    continue
                function = getattr(module, function_name, None)
                if getattr(function, "__engine_registry_id__", None) != entry["id"]:
                    missing.append(entry["writer"])
                else:
                    guarded.append(entry["id"])
        self.assertEqual(missing, [])
        self.assertGreaterEqual(len(guarded), 72)
        # And each skip is accounted for, so the set cannot quietly become an escape hatch:
        # `capture-lock-create` is taken through `authorize_nested`, and `automatic-capture` is the fail-soft
        # outer boundary of a leaf that must never raise, whose mutation is guarded one frame in by
        # `capture-transaction` on `capture._capture`.
        self.assertEqual(mutation_authority._SKIP_WRAPPERS,
                         frozenset({"capture-lock-create", "automatic-capture"}))
        self.assertEqual(
            getattr(capture._capture, "__engine_registry_id__", None), "capture-transaction",
            "the outer capture boundary is unwrapped, so the inner transaction MUST carry the guard")

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
                    request = dict(
                        writer=entry["writer"], target_kind=entry["target_kind"],
                        effect_class=entry["effect_class"], invocation_mode=mode,
                        measured_cardinality=measured, schema_cutover=entry["schema_cutover"],
                    )
                    if mode == "automatic" and mutation_contract._needs_attendance(entry):
                        # Destroying the record needs a person even when everything else is in order, so
                        # here the witness IS the refusal (see TestAttendedOnlyRecordRewrites).
                        with self.assertRaises(mutation_contract.MutationContractError):
                            mutation_contract.classify(**request)
                        continue
                    classified = mutation_contract.classify(**request)
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
            "import os,sys,tempfile,unittest; sys.path.insert(0," + repr(str(TOOLS)) + "); "
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

    def test_a_compiled_frame_claiming_a_test_filename_has_no_test_authority(self):
        script = (
            "import os,sys,tempfile; sys.path.insert(0," + repr(str(TOOLS)) + "); "
            "from memory import ledger; d=tempfile.mkdtemp(); p=os.path.join(d,'ledger.ndjson'); "
            "src=\"ledger.append({'body':'forbidden'},path=p)\"; "
            "code=compile(src," + repr(str(TOOLS / "test_spoofed_authority.py")) + ",'exec'); "
            "\ntry: exec(code,{'ledger':ledger,'p':p})\n"
            "except Exception as e: print(type(e).__name__, os.path.exists(p))\n"
            "else: print('UNEXPECTED', os.path.exists(p))\n"
        )
        env = dict(os.environ)
        env.pop(execution_context.CONTEXT_ENV, None)
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "MutationAuthorityError False")

    def test_a_fabricated_module_cannot_borrow_a_real_test_source_path(self):
        script = (
            "import os,sys,tempfile,types; sys.path.insert(0," + repr(str(TOOLS)) + "); "
            "from memory import ledger; d=tempfile.mkdtemp(); p=os.path.join(d,'ledger.ndjson'); "
            "name='test_fabricated_authority'; m=types.ModuleType(name); "
            "m.__file__=" + repr(str(Path(__file__).resolve())) + "; sys.modules[name]=m; "
            "src=\"ledger.append({'body':'forbidden'},path=p)\"; "
            "m.__dict__.update({'ledger':ledger,'p':p}); code=compile(src,m.__file__,'exec'); "
            "\ntry: exec(code,m.__dict__)\n"
            "except Exception as e: print(type(e).__name__, os.path.exists(p))\n"
            "else: print('UNEXPECTED', os.path.exists(p))\n"
        )
        env = dict(os.environ)
        env.pop(execution_context.CONTEXT_ENV, None)
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "MutationAuthorityError False")

    def test_an_untracked_real_test_module_cannot_obtain_context_free_authority(self):
        candidate = TOOLS / "test_untracked_candidate_authority.py"
        target = None
        try:
            candidate.write_text(
                "import os, sys, tempfile\n"
                f"sys.path.insert(0, {str(TOOLS)!r})\n"
                "from memory import ledger\n"
                "p = os.path.join(tempfile.mkdtemp(), 'ledger.ndjson')\n"
                "try:\n"
                "    ledger.append({'body': 'forbidden'}, path=p)\n"
                "except Exception as exc:\n"
                "    print(type(exc).__name__, os.path.exists(p))\n"
                "else:\n"
                "    print('UNEXPECTED', os.path.exists(p))\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, str(candidate)], capture_output=True, text=True,
                env={key: value for key, value in os.environ.items()
                     if key != execution_context.CONTEXT_ENV},
            )
        finally:
            candidate.unlink(missing_ok=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "MutationAuthorityError False")

    def test_the_compiled_source_cache_is_keyed_on_the_bytes_and_admits_no_stale_answer(self):
        """The one risk the compile cache introduces, pinned.

        `_source_bound_frame` compares the live frame against the code compiled from the file on disk. That
        compile is now memoised, because it fired on every guarded mutation and made one index test 22 times
        slower than the whole rest of its module. Memoising a security check is only sound if the key is the
        thing the answer depends on — so this asserts the key IS the source bytes: the same path with
        different contents must produce different signature sets, and the second must not inherit the first.
        The function takes only the bytes and decodes them itself, so the key and the compiled input cannot
        drift apart the way they could when the caller supplied both.
        """
        real = str(TOOLS / "memory" / "mutation_authority.py")
        first = b"def alpha():\n    return 1\n"
        second = b"def alpha():\n    return 2\n"
        sig_first = mutation_authority._compiled_signatures(real, first)
        sig_second = mutation_authority._compiled_signatures(real, second)
        self.assertIsNotNone(sig_first)
        self.assertIsNotNone(sig_second)
        self.assertNotEqual(sig_first, sig_second, "different sources shared one cached answer")
        # …and asking again for the first bytes returns the first answer, not whatever was cached last.
        self.assertEqual(mutation_authority._compiled_signatures(real, first), sig_first)

    def test_same_named_direct_script_cannot_preflight_instantiator_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "instantiator.py"
            script_path.write_text(
                "import sys\n"
                f"sys.path.insert(0, {str(TOOLS)!r})\n"
                "from memory import mutation_authority\n"
                "def retire(root):\n"
                "    capability = mutation_authority.acquire_preactivation_local_capability(\n"
                "        'attended-first-run-marker-stage', project_root=root)\n"
                "    print(type(capability).__name__)\n"
                "retire(sys.argv[1])\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, str(script_path), tmp], capture_output=True, text=True,
                env={key: value for key, value in os.environ.items()
                     if key != execution_context.CONTEXT_ENV},
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("MutationAuthorityError", result.stderr)

    def test_preactivation_capability_is_issuer_created_registry_backed_and_one_use(self):
        from unittest import mock

        with self.assertRaises(mutation_authority.MutationAuthorityError):
            mutation_authority._PreActivationCapability()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
                mutation_authority, "_source_bound_frame", return_value=True):
            capability = mutation_authority.acquire_preactivation_local_capability(
                "attended-first-run-marker-stage", project_root=tmp)
            self.assertFalse(hasattr(capability, "used"))
            self.assertTrue(first_run_health.mark_first_run_applied(
                tmp, _engine_capability=capability))
            with self.assertRaisesRegex(
                    mutation_authority.MutationAuthorityError, "already consumed"):
                first_run_health.mark_first_run_applied(tmp, _engine_capability=capability)

    def test_preactivation_marker_refuses_symlink_escape_without_touching_external_file(self):
        from unittest import mock

        for final_symlink in (False, True):
            with self.subTest(final_symlink=final_symlink), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "project"
                outside = Path(tmp) / "outside"
                outside.mkdir()
                external = outside / "external.json"
                external.write_text("ORIGINAL", encoding="utf-8")
                if final_symlink:
                    cache = root / ".engine" / "boot" / ".cache"
                    cache.mkdir(parents=True)
                    (cache / "first-run-landing.json").symlink_to(external)
                else:
                    (root / ".engine" / "boot").mkdir(parents=True)
                    (root / ".engine" / "boot" / ".cache").symlink_to(outside, target_is_directory=True)
                with mock.patch.object(mutation_authority, "_source_bound_frame", return_value=True):
                    capability = mutation_authority.acquire_preactivation_local_capability(
                        "attended-first-run-marker-stage", project_root=str(root))
                self.assertFalse(first_run_health.mark_first_run_applied(
                    str(root), _engine_capability=capability))
                self.assertEqual(external.read_text(encoding="utf-8"), "ORIGINAL")


class TerminalAttendedAuthorityTests(unittest.TestCase):
    """The authority a terminal verb (the ClawMem exporter, the erasure verb) runs its own writes on when it has
    no execution context: a real terminal, checked before the scope opens, fail-closed on both halves. The tty
    check is a speed-bump, not a proof of human presence (a pty passes it) — these tests pin the frame/entry
    gating, not attendance. They exercise the REAL path (not the test-only adapter), where the original gap hid."""

    def tearDown(self):
        mutation_authority._THREAD.state = None

    def test_refuses_without_a_real_terminal_on_either_stream(self):
        from unittest import mock
        for stdin_tty, stdout_tty in ((False, True), (True, False), (False, False)):
            with self.subTest(stdin=stdin_tty, stdout=stdout_tty):
                with mock.patch("sys.stdin.isatty", return_value=stdin_tty), \
                        mock.patch("sys.stdout.isatty", return_value=stdout_tty):
                    with self.assertRaisesRegex(mutation_authority.MutationAuthorityError, "real terminal"):
                        with mutation_authority.terminal_attended(["attended-clawmem-export"]):
                            pass
                self.assertIsNone(getattr(mutation_authority._THREAD, "state", None))

    def test_refuses_a_caller_that_is_not_a_sanctioned_verb(self):
        # A tty is not enough: the opener must be one of the engine's OWN terminal verb entrypoints. This test
        # frame is not one, so even with a tty on both streams the scope refuses and sets no state. NOTE this does
        # NOT stop an AI from running the GENUINE verb under a pty (that passes both checks) — the frame gate only
        # blocks arbitrary/non-verb callers; attendance is not enforced (see terminal_attended's honesty note).
        from unittest import mock
        with mock.patch("sys.stdin.isatty", return_value=True), \
                mock.patch("sys.stdout.isatty", return_value=True):
            with self.assertRaisesRegex(mutation_authority.MutationAuthorityError, "terminal verbs"):
                with mutation_authority.terminal_attended(["attended-clawmem-export"]):
                    pass
        self.assertIsNone(getattr(mutation_authority._THREAD, "state", None))

    def test_refuses_opening_inside_another_scope(self):
        from unittest import mock
        mutation_authority._THREAD.state = {"test_only": True, "mode": "attended"}
        try:
            with mock.patch("sys.stdin.isatty", return_value=True), \
                    mock.patch("sys.stdout.isatty", return_value=True):
                with self.assertRaisesRegex(mutation_authority.MutationAuthorityError, "inside another"):
                    with mutation_authority.terminal_attended(["attended-clawmem-export"]):
                        pass
        finally:
            mutation_authority._THREAD.state = None

    def test_within_a_scope_only_the_named_writes_are_authorized(self):
        # White-box: with a scope open for exactly one entry, a nested authorization for THAT entry is granted and
        # any other registered writer is refused — an allowed verb cannot become a door to the store beneath it.
        mutation_authority._THREAD.state = {
            "test_only": False, "terminal_attended": True,
            "allowed_entries": frozenset({"attended-clawmem-export"})}
        try:
            receipt = mutation_authority.authorize_nested("attended-clawmem-export")
            self.assertEqual(receipt["exception"], "operator-attended-terminal")
            with self.assertRaisesRegex(mutation_authority.MutationAuthorityError, "not one of the writes"):
                mutation_authority.authorize_nested("erasure-proposal-write")
        finally:
            mutation_authority._THREAD.state = None

    def test_the_sanctioned_verbs_name_only_real_registry_entries(self):
        # The allowlist must not drift from the registry: every id a terminal verb may authorize is a real,
        # currently-registered writer, and each verb names a source file and an entrypoint function.
        known = {entry["id"] for entry in mutation_contract.REGISTRY}
        self.assertTrue(mutation_authority._TERMINAL_ATTENDED_VERBS)
        for source, (function_name, allowed) in mutation_authority._TERMINAL_ATTENDED_VERBS.items():
            self.assertTrue(str(source).endswith(".py"))
            self.assertIsInstance(function_name, str)
            self.assertTrue(allowed)
            for entry_id in allowed:
                self.assertIn(entry_id, known)

    def test_each_verb_authorizes_exactly_its_own_guarded_registry_writers(self):
        # The lockstep the verb modules' comments claim: a verb's terminal-attended allowlist must equal EXACTLY
        # the guarded registry writers of that verb's module, and its own declared write-entries constant must
        # equal that allowlist. This is the coverage test that catches drift both ways — a new guarded writer the
        # scope would refuse, or an allowlist id dropped so a real write silently loses authorization (the exact
        # "green but wrong" regression class for erase.py that the behavioural main() tests also guard).
        from memory import clawmem_export, erase
        module_writers = {}
        for entry in mutation_contract.REGISTRY:
            module = entry["writer"].rsplit(".", 1)[0]
            if entry["effect_class"] != "semantic-read" and entry["id"] not in mutation_authority._SKIP_WRAPPERS:
                module_writers.setdefault(module, set()).add(entry["id"])
        cases = {
            "memory.clawmem_export": clawmem_export._EXPORT_WRITE_ENTRIES,
            "memory.erase": erase._ERASE_WRITE_ENTRIES,
        }
        by_source = {src: allowed for src, (_fn, allowed) in mutation_authority._TERMINAL_ATTENDED_VERBS.items()}
        for module, declared in cases.items():
            allowlist = next(a for s, a in by_source.items() if s.endswith(module.split(".")[-1] + ".py"))
            self.assertEqual(set(declared), set(allowlist),
                             f"{module}'s declared write-entries constant must equal its allowlist")
            self.assertEqual(set(allowlist), module_writers.get(module, set()),
                             f"{module}'s allowlist must equal exactly its guarded registry writers")


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

    def test_a_second_outer_write_heals_the_self_inflicted_fingerprint_drift_and_lands(self):
        # The first append advances the store, so the cached context's observed fingerprint is now stale by
        # the second append — but the store identity, activation and pointer all still hold, so this is the
        # one refreshable class. Before n3 the second write refused; n3 re-observes under the lock once and
        # lets it land. The re-seal preserved the binding and its single cost was measured.
        target = os.path.join(self.fixture.memory, "ledger.ndjson")
        ledger.append({"body": "first"}, path=target)
        ledger.append({"body": "second"}, path=target)
        self.assertEqual(ledger.read(path=target).records, [{"body": "first"}, {"body": "second"}])
        reseal = mutation_authority.last_reseal()
        self.assertIsNotNone(reseal)
        self.assertTrue(reseal["binding_preserved"])
        self.assertEqual(reseal["before"], reseal["after"])
        self.assertIsInstance(reseal["cost_seconds"], float)
        self.assertGreaterEqual(reseal["cost_seconds"], 0.0)

    def test_fingerprint_drift_injected_after_lock_acquisition_heals_and_lands(self):
        # A ledger-meta change is an observed-state (fingerprint) drift while the binding holds: the one
        # refreshable class. Injected after the lock, it is re-observed once under the lock and the write
        # lands, rather than refusing.
        target = os.path.join(self.fixture.memory, "ledger.ndjson")
        meta = os.path.join(self.fixture.memory, "ledger-meta.json")

        def drift():
            Path(meta).write_text('{"generation":7,"index_epoch":0}\n', encoding="utf-8")

        mutation_authority.set_after_lock_test_hook(drift)
        ledger.append({"body": "lands after one re-seal"}, path=target)
        self.assertEqual(ledger.read(path=target).records, [{"body": "lands after one re-seal"}])
        self.assertTrue(mutation_authority.last_reseal()["binding_preserved"])

    def test_binding_drift_injected_after_lock_acquisition_refuses_content_free_before_payload_write(self):
        # An activation change under the lock is NOT the refreshable class: it re-raises through the re-seal
        # and refuses. The payload never lands, and the refusal names no path, commit or fingerprint.
        target = os.path.join(self.fixture.memory, "ledger.ndjson")
        activation = os.path.join(self.fixture.common, "engine", "accepted-hooks", "activation.json")
        moved_commit = "c" * 40

        def drift():
            record = json.loads(Path(activation).read_text(encoding="utf-8"))
            record["commit"] = moved_commit
            Path(activation).write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")

        mutation_authority.set_after_lock_test_hook(drift)
        with self.assertRaises(mutation_authority.MutationAuthorityError) as caught:
            ledger.append({"body": "must not land"}, path=target)
        message = str(caught.exception)
        self.assertIn("restart", message)
        self.assertNotIn(moved_commit, message)
        self.assertNotIn(self.fixture.base, message)
        self.assertNotIn("fingerprint", message)
        self.assertFalse(os.path.exists(target))

    def test_a_reseal_that_moves_the_binding_refuses_and_does_not_write(self):
        # The under-lock re-seal must only refresh observed state, never move the store binding. The
        # `if before != after` guard defends the future refactor that breaks that invariant. Drive the
        # refreshable path (a fingerprint drift under the lock) and stub the re-seal to hand back a context
        # whose binding identity differs, so the guard fires: the refusal is content-free and nothing lands.
        target = os.path.join(self.fixture.memory, "ledger.ndjson")
        meta = os.path.join(self.fixture.memory, "ledger-meta.json")

        def drift():
            Path(meta).write_text('{"generation":7,"index_epoch":0}\n', encoding="utf-8")

        original_reseal = execution_context.reseal_for_stale_state
        moved = object()  # a stand-in "re-sealed" context whose binding reads as different

        def reseal_moves_binding(context):
            original_reseal(context)  # run the real refresh for its validation, then discard it
            return moved

        def binding_of(context):
            return {"store_identity": "moved" if context is moved else "original"}

        mutation_authority.set_after_lock_test_hook(drift)
        with mock.patch.object(execution_context, "reseal_for_stale_state", reseal_moves_binding), \
                mock.patch.object(execution_context, "binding_identity", binding_of):
            with self.assertRaises(mutation_authority.MutationAuthorityError) as caught:
                ledger.append({"body": "must not land"}, path=target)
        message = str(caught.exception)
        self.assertIn("binding shifted", message)
        self.assertNotIn(self.fixture.base, message)
        self.assertFalse(os.path.exists(target))
        self.assertFalse(mutation_authority.last_reseal()["binding_preserved"])

    def test_explicit_path_outside_the_bound_store_refuses(self):
        escaped = os.path.join(self.fixture.base, "escaped.ndjson")
        with self.assertRaisesRegex(mutation_authority.MutationAuthorityError, "escapes"):
            ledger.append({"body": "must not land"}, path=escaped)
        self.assertFalse(os.path.exists(escaped))

    def test_positional_path_outside_the_bound_store_refuses(self):
        escaped = os.path.join(self.fixture.base, "escaped")
        with self.assertRaisesRegex(mutation_authority.MutationAuthorityError, "escapes"):
            capture._write_cursor(escaped, "session", 3)
        self.assertFalse(os.path.exists(os.path.join(escaped, capture.CURSOR_FILENAME)))

    def test_collection_cardinality_overrun_is_not_clamped_to_the_declared_maximum(self):
        self.fixture.cleanup()
        self.fixture = _QualifiedFixture(automatic=True)
        self.fixture.install()
        called = []

        def writer(*, records):
            called.append(records)

        guarded = mutation_authority._guard("capture-failure-history", writer)
        with self.assertRaisesRegex(mutation_authority.MutationAuthorityError, "cardinality"):
            guarded(records=[{"n": value} for value in range(21)])
        self.assertEqual(called, [])

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

    def test_long_lived_mcp_resolves_each_mutation_against_fresh_state(self):
        from memory import pins

        self.fixture.cleanup()
        self.fixture = _QualifiedFixture(mcp=True)
        self.fixture.install()
        first = pins.add("first standing preference")
        second = pins.add("second standing preference")
        self.assertNotEqual(first[records.RECORD_ID_KEY], second[records.RECORD_ID_KEY])
        self.assertEqual(execution_context.current_context()["operation"]["registry_id"],
                         "attended-memory-mcp")
        target = os.path.join(self.fixture.memory, "ledger.ndjson")
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"kind": "external-accepted-capture", "body": "intervening"}) + "\n")
        third = pins.add("third preference after an automatic write")
        self.assertTrue(third[records.RECORD_ID_KEY])

    def test_post_commit_mcp_refresh_failure_does_not_turn_success_into_failure(self):
        from memory import pins
        from unittest import mock

        self.fixture.cleanup()
        self.fixture = _QualifiedFixture(mcp=True)
        self.fixture.install()
        for failure in (execution_context.ContextError("injected refresh failure"),
                        OSError("injected ordinary refresh failure")):
            with self.subTest(failure=type(failure).__name__), mock.patch.object(
                    execution_context, "refresh_current_context", side_effect=failure):
                record = pins.add(f"committed despite {type(failure).__name__}")
            self.assertTrue(record[records.RECORD_ID_KEY])
        third = pins.add("next request refreshes from the renewable root")
        self.assertTrue(third[records.RECORD_ID_KEY])
        self.assertEqual(len(list(ledger.iter_records(path=os.path.join(
            self.fixture.memory, "ledger.ndjson")))), 3)

    def test_long_lived_mcp_authority_state_is_bounded_after_many_requests(self):
        from memory import pins

        self.fixture.cleanup()
        self.fixture = _QualifiedFixture(mcp=True)
        self.fixture.install()
        for number in range(80):
            pins.add(f"bounded request {number}")
        gc.collect()
        self.assertLessEqual(len(execution_context._AUTHORIZED_CONTEXTS), 2)
        self.assertEqual(len(execution_context._GRANTS), 0)


class AttendedWithholdRestoreEndToEndTests(unittest.TestCase):
    """A withhold and a restore, end-to-end through the REAL guard path under a minted attended context.

    These are the first tests to drive a SUCCESSFUL attended withhold/restore through the transitive-boundary
    closure. Every existing round-trip in `test_forget` runs in the no-context arm, where the source-bound
    test adapter waves the write through and the closure is never consulted. Here a real qualified context is
    installed (`_QualifiedFixture`, the MCP root the 15 refused `mcp__engine-memory__withhold` calls used), so
    the adapter is bypassed and the genuine capture-lock -> index-epoch -> append path runs and mints real
    `CapabilityReceipt`s. Before the withhold/restore boundary keys this refused at the first nested write with
    the raw "outside this invocation's closed transitive boundary" error — exactly the observed bug."""

    def setUp(self):
        self.fixture = _QualifiedFixture(mcp=True)
        self.fixture.install()

    def tearDown(self):
        self.fixture.cleanup()

    def _seed_turn(self, session, text):
        record = capture._make_record(session, 0, "user", text)
        ledger.append(record, path=os.path.join(self.fixture.memory, "ledger.ndjson"))
        return record[records.RECORD_ID_KEY]

    def _live_ids(self):
        from memory import forget
        return {record.get(records.RECORD_ID_KEY) for record in forget.live_records()}

    def test_withhold_then_restore_round_trips_through_the_real_capability_path(self):
        from memory import forget
        rid = self._seed_turn("s-e2e", "a secret turn note")
        self.assertIn(rid, self._live_ids())

        receipts = []
        original = execution_context.consume_capability

        def _spy(capability, **kwargs):
            receipt = original(capability, **kwargs)
            receipts.append(receipt)
            return receipt

        execution_context.consume_capability = _spy
        try:
            marker = forget.withhold(record_id=rid)
        finally:
            execution_context.consume_capability = original

        # The write went through the real path: genuine CapabilityReceipts, not a test-only dict.
        self.assertEqual(marker["kind"], records.WITHHOLD_KIND)
        self.assertTrue(receipts)
        self.assertTrue(all(isinstance(receipt, execution_context.CapabilityReceipt) for receipt in receipts))

        # The marker landed in the real temp ledger, and the record left recall.
        landed = [record for record in ledger.iter_records(
            path=os.path.join(self.fixture.memory, "ledger.ndjson"))
            if record.get("kind") == records.WITHHOLD_KIND and record.get(records.TARGET_KEY) == rid]
        self.assertEqual(len(landed), 1)
        self.assertNotIn(rid, self._live_ids())

        # Restore reverses it — the record re-enters live_records.
        forget.restore(record_id=rid)
        self.assertIn(rid, self._live_ids())


class RevalidationTaxonomyTests(unittest.TestCase):
    """Every staleness the revalidation path can observe raises its own typed ContextError subclass, and only
    the fingerprint class is refreshable. These drive `revalidate_context` directly, so they need no lock or
    capture adapter — they pin the classification at its source."""

    def setUp(self):
        self.fixture = _QualifiedFixture()
        self.fixture.install()
        self.activation = os.path.join(
            self.fixture.common, "engine", "accepted-hooks", "activation.json")
        self.store_identity = os.path.join(self.fixture.memory, "store-identity.json")
        self.pointer = self.fixture.pointer
        self.dispatcher = os.path.join(
            self.fixture.accepted, ".engine", "tools", "accepted_hook_dispatch.py")

    def tearDown(self):
        self.fixture.cleanup()

    def test_activation_move_is_typed_activation_stale(self):
        record = json.loads(Path(self.activation).read_text(encoding="utf-8"))
        record["commit"] = "c" * 40
        Path(self.activation).write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
        with self.assertRaises(execution_context.ActivationStale):
            execution_context.revalidate_context(self.fixture.context)

    def test_missing_accepted_tree_is_typed_accepted_tree_stale(self):
        os.remove(self.dispatcher)
        with self.assertRaises(execution_context.AcceptedTreeStale):
            execution_context.revalidate_context(self.fixture.context)

    def test_unstattable_root_is_typed_artifact_unreadable_not_raw_oserror(self):
        # The residual crash #1199 missed: revalidate_context reads the project root and Git common
        # directory through _path_identity -> os.stat, which was UNWRAPPED. Under drift either can fail to
        # stat (moved/replaced/permission), and the bare OSError escaped revalidate_context untyped —
        # crashing every memory read and write that revalidates rather than being caught by the
        # ContextError-only handlers (reads degrade, writes refuse). revalidate_context now types any
        # OSError from the matched body as ArtifactUnreadable (a ContextError) so those handlers do their
        # job and it reads as the store-on-disk problem it is.
        with mock.patch.object(execution_context, "_path_identity", side_effect=OSError(13, "denied")):
            with self.assertRaises(execution_context.ArtifactUnreadable):
                execution_context.revalidate_context(self.fixture.context)

    def test_unrelated_in_body_exception_propagates_rather_than_masking(self):
        # The backstop is deliberately NOT total: it types the I/O read-fault class (OSError) but lets a
        # genuine logic bug or corruption inside revalidation (a non-OSError) PROPAGATE and surface, rather
        # than mask it as a benign "restart" refusal. A silently-malfunctioning write-authority trust root
        # is more dangerous than a visible crash. This pins the plan's W2 "unrelated in-body exception still
        # propagates" bound so a future refactor cannot quietly widen the catch back to `except Exception`.
        with mock.patch.object(execution_context, "_path_identity", side_effect=ValueError("boom")):
            with self.assertRaises(ValueError):
                execution_context.revalidate_context(self.fixture.context)

    def test_store_identity_change_is_typed_store_identity_stale(self):
        identity = json.loads(Path(self.store_identity).read_text(encoding="utf-8"))
        identity["store_id"] = "0" * 32
        Path(self.store_identity).write_text(json.dumps(identity, sort_keys=True) + "\n", encoding="utf-8")
        with self.assertRaises(execution_context.StoreIdentityStale):
            execution_context.revalidate_context(self.fixture.context)

    def test_backup_pointer_bytes_change_is_typed_backup_pointer_stale(self):
        # Same parsed content, different bytes — so the pointer identity holds but its digest moves, which is
        # exactly the backup-pointer staleness the class names.
        Path(self.pointer).write_text('{"schema_version": 1, "configured": false}\n', encoding="utf-8")
        with self.assertRaises(execution_context.BackupPointerStale):
            execution_context.revalidate_context(self.fixture.context)

    def test_expected_state_fingerprint_drift_is_typed_expected_state_stale_and_is_the_only_refreshable(self):
        Path(os.path.join(self.fixture.memory, "ledger-meta.json")).write_text(
            '{"generation":9,"index_epoch":0}\n', encoding="utf-8")
        with self.assertRaises(execution_context.ExpectedStateStale):
            execution_context.revalidate_context(self.fixture.context)
        # The refreshable class re-seals cleanly from the same document and its binding is preserved.
        refreshed = execution_context.reseal_for_stale_state(self.fixture.context)
        self.assertEqual(execution_context.binding_identity(self.fixture.context),
                         execution_context.binding_identity(refreshed))

    def test_unreadable_artifact_is_typed_artifact_unreadable(self):
        blocked = os.path.join(self.fixture.memory, "blocked.json")
        Path(blocked).write_text("{}", encoding="utf-8")
        os.chmod(blocked, 0)

        def _restore():
            try:  # the fixture's temp dir may already be gone; restoring perms is best-effort
                os.chmod(blocked, 0o600)
            except FileNotFoundError:
                pass

        self.addCleanup(_restore)
        try:
            with open(blocked, "rb"):
                pass
        except OSError:
            readable = False
        else:
            readable = True
        if readable:  # some CI runs as a user that bypasses the mode bits; the classification still holds
            self.skipTest("filesystem does not enforce unreadable mode for this user")
        with self.assertRaises(execution_context.ArtifactUnreadable):
            execution_context._file_digest(blocked)


class RaiseSiteTaxonomyEnumerationTests(unittest.TestCase):
    """Every `raise` in execution_context names a class inside the sanctioned taxonomy — no bare exception, and
    nothing outside the ContextError family for a boundary failure. This is the executable form of the node's
    'enumerated against every site': a future untyped divergence fails here instead of leaking to a caller."""

    # The ContextError family: a boundary/staleness failure must raise one of these.
    _FAMILY = frozenset({
        "ContextError", "ExpectedStateStale", "ActivationStale", "AcceptedTreeStale",
        "StoreIdentityStale", "BackupPointerStale", "ArtifactUnreadable",
    })
    # Standard exceptions the module raises for non-boundary reasons: the immutability protocol
    # (AttributeError), the CLI's exit code (SystemExit), and the embedded self-test's own checks
    # (AssertionError). A NEW class name outside both sets fails the test, forcing whoever adds it to
    # classify it deliberately.
    _ALLOWED_STDLIB = frozenset({"AttributeError", "SystemExit", "AssertionError"})

    def test_every_raise_site_uses_a_sanctioned_class(self):
        import ast

        source = Path(execution_context.__file__).read_text(encoding="utf-8")
        offenders = []
        family = 0
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Raise) or node.exc is None:
                continue
            # Only construction sites (`raise Class(...)`) are classified; `raise exc` / bare `raise`
            # re-raise a caught exception and carry no class name to check.
            if not isinstance(node.exc, ast.Call) or not isinstance(node.exc.func, ast.Name):
                continue
            name = node.exc.func.id
            if name in self._FAMILY:
                family += 1
            elif name not in self._ALLOWED_STDLIB:
                offenders.append((node.lineno, name))
        self.assertEqual(offenders, [], f"unsanctioned raise classes: {offenders}")
        # Guard the guard: if a refactor stopped the family from being raised, this must not pass vacuously.
        self.assertGreater(family, 30)


from memory import stranding_log as _stranding_log  # noqa: E402 — the writer under test below


class StrandingLogAuthorityTests(unittest.TestCase):
    """The diagnostic-private tier (program prg_d15d7dc8f3df, C1): the stranding log must RECORD in exactly
    the sessions where the ordinary guard cannot finish — and it must be able to write nothing else.

    `mutation_scope` reads the context, opens the store lock and refreshes authority before it routes an
    ordinary degraded-allowed writer, catching only ContextError on the way, so a lock that cannot be taken
    or a refresh that raises anything else stops such a writer cold. The early route for
    `DIAGNOSTIC_PRIVATE_ENTRY_IDS` never enters those steps. These tests inject each failure in turn and
    assert the record still lands — and that the failing step was not even attempted."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="engine-stranding-")
        self.path = os.path.join(self.temp.name, "stranding-log.ndjson")
        self.fixture = None
        mutation_authority._THREAD.state = None

    def tearDown(self):
        if self.fixture is not None:
            self.fixture.cleanup()
        mutation_authority._THREAD.state = None
        execution_context._CURRENT_CONTEXT = None
        os.environ.pop(execution_context.CONTEXT_ENV, None)
        self.temp.cleanup()

    def _fault(self):
        try:
            raise RuntimeError("a fault whose message must never be recorded")
        except RuntimeError as exc:
            return exc

    def _records(self):
        if not os.path.exists(self.path):
            return []
        with open(self.path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def _install_qualified(self):
        self.fixture = _QualifiedFixture(mcp=True)
        self.fixture.install()

    def test_records_with_no_execution_context_at_all(self):
        self.assertTrue(_stranding_log.record_stranding(
            _stranding_log.Event.TOOL_FAULT, self._fault(), path=self.path))
        (record,) = self._records()
        self.assertEqual(record["event"], "tool-fault")
        self.assertEqual(record["qualification"], "none")

    def test_records_under_a_genuinely_stale_typed_context(self):
        self._install_qualified()
        activation = os.path.join(self.fixture.common, "engine", "accepted-hooks", "activation.json")
        document = json.loads(Path(activation).read_text(encoding="utf-8"))
        document["epoch"] = 2
        Path(activation).write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")
        with self.assertRaises(execution_context.ActivationStale):
            execution_context.revalidate_context(self.fixture.context)  # the staleness is real, not mocked
        self.assertTrue(_stranding_log.record_stranding(
            _stranding_log.Event.READ_DEGRADED, self._fault(), path=self.path))
        self.assertEqual(len(self._records()), 1)

    def test_records_when_the_authority_refresh_raises_something_unexpected(self):
        self._install_qualified()
        with mock.patch.object(execution_context, "refresh_for_operation",
                               side_effect=RuntimeError("refresh exploded")) as refresh, \
             mock.patch.object(execution_context, "revalidate_context",
                               side_effect=RuntimeError("revalidate exploded")) as revalidate:
            self.assertTrue(_stranding_log.record_stranding(
                _stranding_log.Event.TOOL_FAULT, self._fault(), path=self.path))
        refresh.assert_not_called()
        revalidate.assert_not_called()
        self.assertEqual(len(self._records()), 1)

    def test_records_when_the_store_lock_cannot_be_taken(self):
        """The writer never needs the store lock. The real `_open_store_lock` BLOCKS rather than raising,
        so this injects the failure the ordinary path would otherwise sit in and proves the diagnostic
        route never reaches that step at all (the mock is not called) — the record lands regardless."""
        self._install_qualified()
        with mock.patch.object(mutation_authority, "_open_store_lock",
                               side_effect=OSError(11, "store lock busy")) as lock:
            self.assertTrue(_stranding_log.record_stranding(
                _stranding_log.Event.TOOL_FAULT, self._fault(), path=self.path))
        lock.assert_not_called()
        self.assertEqual(len(self._records()), 1)

    def test_a_guard_fault_that_is_not_a_context_error_stays_inside_the_boundary(self):
        with mock.patch.object(mutation_authority, "_entry", side_effect=RuntimeError("registry exploded")):
            self.assertFalse(_stranding_log.record_stranding(
                _stranding_log.Event.TOOL_FAULT, self._fault(), path=self.path))
        with mock.patch.object(mutation_contract, "classify", side_effect=RuntimeError("classify exploded")):
            self.assertFalse(_stranding_log.record_stranding(
                _stranding_log.Event.TOOL_FAULT, self._fault(), path=self.path))
        self.assertEqual(self._records(), [])

    def test_the_early_route_leaves_an_enclosing_scope_untouched(self):
        with mutation_authority.test_scope("attended"):
            before = mutation_authority._THREAD.state
            self.assertTrue(_stranding_log.record_stranding(_stranding_log.Event.SELF_CHECK, path=self.path))
            self.assertIs(mutation_authority._THREAD.state, before)

    def test_the_writer_reaches_only_its_own_sink_outside_the_test_harness(self):
        """Authority narrowness. With the test-harness allowance switched off — the production shape — a
        caller-supplied path, even one aimed at a ledger, is ignored and the record lands in the sink."""
        root = os.path.realpath(self.temp.name)
        sink = os.path.join(root, ".engine", "telemetry", ".cache", "stranding-log.ndjson")
        elsewhere = os.path.join(root, ".engine", "memory", "ledger.ndjson")
        outside = os.path.join(os.path.realpath(tempfile.gettempdir()), "engine-stranding-outside.ndjson")
        os.makedirs(os.path.dirname(elsewhere))
        with mock.patch.object(_stranding_log, "_test_path_allowed", return_value=False), \
             mock.patch.object(_stranding_log, "_project_root", return_value=root):
            # The derived sink is the only destination; a caller-supplied path aimed at the ledger is ignored.
            self.assertTrue(_stranding_log.record_stranding(
                _stranding_log.Event.TOOL_FAULT, self._fault(), path=elsewhere))
            self.assertTrue(os.path.exists(sink))
            self.assertFalse(os.path.exists(elsewhere))
            self.assertEqual(os.listdir(os.path.dirname(elsewhere)), [])
            # And a sink that would resolve OUTSIDE the project root is refused, not written.
            with mock.patch.object(_stranding_log, "sink_path", return_value=outside):
                self.assertFalse(_stranding_log.record_stranding(
                    _stranding_log.Event.TOOL_FAULT, self._fault()))
            self.assertFalse(os.path.exists(outside))
        # The writer's shape admits no other destination: one line, and the test-only path.
        self.assertEqual(list(inspect.signature(_stranding_log._append).parameters), ["line", "path"])

    def test_the_tier_is_declared_closed_and_the_module_is_censused(self):
        entry = mutation_contract.entry_by_id(_stranding_log.APPEND_REGISTRY_ID)
        self.assertEqual(mutation_contract.DIAGNOSTIC_PRIVATE_ENTRY_IDS,
                         frozenset({_stranding_log.APPEND_REGISTRY_ID}))
        self.assertEqual(mutation_contract.degraded_disposition(entry), "allow")
        self.assertEqual(entry["declared_cardinality"]["maximum"], 1)
        self.assertEqual(getattr(_stranding_log._append, "__engine_registry_id__", None),
                         _stranding_log.APPEND_REGISTRY_ID)
        export = mutation_contract.entry_by_id(_stranding_log.EXPORT_REGISTRY_ID)
        self.assertEqual(getattr(_stranding_log._export_write, "__engine_registry_id__", None),
                         _stranding_log.EXPORT_REGISTRY_ID)
        # The export is an ordinary export artifact: no early route, refused unqualified.
        self.assertNotIn(_stranding_log.EXPORT_REGISTRY_ID, mutation_contract.DIAGNOSTIC_PRIVATE_ENTRY_IDS)
        self.assertEqual(mutation_contract.degraded_disposition(export), "refuse")
        module_path = os.path.join(TOOLS, "memory", "stranding_log.py")
        self.assertEqual(mutation_contract.coverage_failures([(module_path, "memory.stranding_log")]), [])

    def test_a_test_that_names_no_file_records_nothing(self):
        sink = os.path.join(self.temp.name, "never.ndjson")
        with mock.patch.object(_stranding_log, "sink_path", return_value=sink):
            self.assertFalse(_stranding_log.record_stranding(_stranding_log.Event.SELF_CHECK))
        self.assertFalse(os.path.exists(sink))



class ReadBindingTests(unittest.TestCase):
    """C2 (pln_b5eb869e55b4): the read-side resolution never raises, installs nothing except the reviewed
    root refresh on an uncached refreshable drift in the memory server, and maps every way revalidation can
    end onto a total, closed vocabulary. Each row here is a real on-disk drift against a sealed canonical
    attended-memory-mcp context, except the two declared monkeypatches (base ContextError, unknown subclass),
    which no disk change reaches."""

    def setUp(self):
        self.fixture = _QualifiedFixture(mcp=True)
        # Not fixture.cleanup(): that routes through the under-lock test hook, which the test adapter refuses
        # from a test file that is not yet the committed HEAD source. These tests never take the lock.
        self.addCleanup(self.fixture.temp.cleanup)
        self.addCleanup(self._reset_globals)
        self._reset_globals()

    def _reset_globals(self):
        execution_context._CURRENT_CONTEXT = None
        with execution_context._CONTEXT_LOCK:
            execution_context._AUTHORIZED_CONTEXTS.clear()
        os.environ.pop(execution_context.CONTEXT_ENV, None)
        os.environ.pop(ledger.ENV_DIR, None)

    def _uncached(self):
        """The captured production shape: a context in the environment, nothing cached in the process."""
        os.environ[execution_context.CONTEXT_ENV] = self.fixture.context.to_json()
        execution_context._CURRENT_CONTEXT = None
        self.assertIsNone(execution_context._CURRENT_CONTEXT)

    # ---- injections (each a real drift on disk) ----
    def _drift_fingerprint(self):
        with open(os.path.join(self.fixture.memory, "ledger.ndjson"), "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"body": "a sibling session wrote this"}) + "\n")

    def _advance_activation(self, epoch=2):
        activation = os.path.join(self.fixture.common, "engine", "accepted-hooks", "activation.json")
        document = json.loads(Path(activation).read_text(encoding="utf-8"))
        document["epoch"] = epoch
        Path(activation).write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")

    def _remove_accepted_tree(self):
        os.remove(os.path.join(self.fixture.accepted, ".engine", "tools", "accepted_hook_dispatch.py"))

    def _replace_store_identity(self):
        path = os.path.join(self.fixture.memory, execution_context.STORE_IDENTITY_FILENAME)
        identity = json.loads(Path(path).read_text(encoding="utf-8"))
        identity["store_id"] = "f" * 32
        Path(path).write_text(json.dumps(identity, sort_keys=True) + "\n", encoding="utf-8")

    def _rewrite_pointer(self):
        Path(self.fixture.pointer).write_text(
            '{"schema_version":1,"owner":"o","repo":"r","branch":"b","namespace":"n"}\n', encoding="utf-8")

    def _make_artifact_unreadable(self):
        # A content-hashed artefact that exists but cannot be opened: the I/O-fault class #1208 typed.
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("chmod 0 does not make a file unreadable to root")
        cursor = os.path.join(self.fixture.memory, "capture-state.json")
        Path(cursor).write_text("{}\n", encoding="utf-8")
        os.chmod(cursor, 0)
        self.addCleanup(os.chmod, cursor, 0o600)

    # ---- rows ----
    def test_absent_context_resolves_absent_and_installs_nothing(self):
        binding = execution_context.read_binding()
        self.assertEqual((binding.kind, binding.reason, binding.restart_clears), ("absent", "none-installed", False))
        self.assertIsNone(binding.memory_dir)
        self.assertIsNone(execution_context._CURRENT_CONTEXT)

    def test_clean_uncached_context_is_healthy_and_cached_the_ordinary_way(self):
        self._uncached()
        binding = execution_context.read_binding()
        self.assertEqual(binding.kind, "healthy")
        self.assertEqual(binding.memory_dir, self.fixture.memory)
        self.assertIsNotNone(execution_context._CURRENT_CONTEXT)

    def test_uncached_fingerprint_drift_is_healed_by_the_root_refresh(self):
        self._uncached()
        before = execution_context.binding_identity(self.fixture.context)
        self._drift_fingerprint()
        with self.assertRaises(execution_context.ExpectedStateStale):
            execution_context.revalidate_context(self.fixture.context)  # the drift is real
        binding = execution_context.read_binding()
        self.assertEqual((binding.kind, binding.reason), ("healthy", None))
        installed = execution_context._CURRENT_CONTEXT
        self.assertIsNotNone(installed)
        # An invariant on the refresh's own code (it copies the authority-bearing fields through), not a control
        # against disk - the control is the full revalidation the refresh runs before installing.
        self.assertEqual(execution_context.binding_identity(installed), before)
        self.assertNotEqual(installed.expected_state_fingerprint, self.fixture.context.expected_state_fingerprint)
        self.assertTrue(execution_context._is_authorized_context(installed))
        self.assertEqual(json.loads(os.environ[execution_context.CONTEXT_ENV])["receipt"]["context_digest"],
                         installed.digest)
        # A restart's seal: the installed context revalidates clean and the write path's own resolution sees it.
        execution_context.revalidate_context(installed)
        self.assertIs(execution_context.current_context(), installed)

    def test_two_concurrent_first_reads_produce_one_refresh(self):
        import threading
        self._uncached()
        self._drift_fingerprint()
        original = execution_context._remember_context
        remembered = []

        def counted(context):
            remembered.append(context)
            return original(context)

        results = []
        with mock.patch.object(execution_context, "_remember_context", side_effect=counted):
            threads = [threading.Thread(target=lambda: results.append(execution_context.read_binding()))
                       for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual([r.kind for r in results], ["healthy", "healthy"])
        self.assertIs(results[0].context, results[1].context)
        self.assertEqual(len(remembered), 1, "the refresh installed more than once")   # the bound, asserted

    def test_a_drift_during_the_refresh_itself_is_healthy_read_only_not_unbound(self):
        """A sibling write landing between the refresh's observation and its revalidation raises the same
        refreshable class again; that is a read reflecting disk, never 'the store is not yours'."""
        self._uncached()
        self._drift_fingerprint()
        original = execution_context._observe_state
        seen = []

        def drift_once(*args, **kwargs):
            state = original(*args, **kwargs)
            seen.append(1)
            if len(seen) == 2:
                # The second observation is the refresh's own: the write lands after it and before the
                # refresh's revalidation, which then raises the refreshable class again.
                self._drift_fingerprint()
            return state

        with mock.patch.object(execution_context, "_observe_state", side_effect=drift_once):
            binding = execution_context.read_binding()
        self.assertEqual((binding.kind, binding.reason), ("healthy", None))
        self.assertIsNone(execution_context._CURRENT_CONTEXT)
        self.assertEqual(binding.memory_dir, self.fixture.memory)

    def test_uncached_drift_outside_the_memory_server_is_healthy_read_only_and_installs_nothing(self):
        cli = _QualifiedFixture()  # ledger-append on pins.py: a CLI context, not the memory server
        self.addCleanup(cli.temp.cleanup)
        os.environ[execution_context.CONTEXT_ENV] = cli.context.to_json()
        execution_context._CURRENT_CONTEXT = None
        with open(os.path.join(cli.memory, "ledger.ndjson"), "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"body": "drift"}) + "\n")
        binding = execution_context.read_binding()
        self.assertEqual(binding.kind, "healthy")
        self.assertEqual(binding.memory_dir, cli.memory)
        self.assertIsNone(execution_context._CURRENT_CONTEXT)
        with self.assertRaises(execution_context.ContextError):
            execution_context.refresh_root_for_read(cli.context)  # gated to the memory server

    def test_moved_rows_answer_from_disk_and_install_nothing(self):
        for name, inject, reason in (("activation", self._advance_activation, "ActivationStale"),
                                     ("tree", self._remove_accepted_tree, "AcceptedTreeStale")):
            with self.subTest(row=name):
                self.setUp()
                self._uncached()
                inject()
                binding = execution_context.read_binding()
                self.assertEqual((binding.kind, binding.reason, binding.restart_clears), ("moved", reason, True))
                self.assertEqual(binding.memory_dir, self.fixture.memory)
                self._assert_not_installed_or_authorized(binding)

    def test_unbound_rows_carry_no_memory_dir_and_install_nothing(self):
        rows = (("store-identity", self._replace_store_identity, "StoreIdentityStale"),
                ("pointer", self._rewrite_pointer, "BackupPointerStale"),
                ("artifact", self._make_artifact_unreadable, "ArtifactUnreadable"))
        for name, inject, reason in rows:
            with self.subTest(row=name):
                self.setUp()
                self._uncached()
                inject()
                binding = execution_context.read_binding()
                self.assertEqual((binding.kind, binding.reason, binding.restart_clears), ("unbound", reason, True))
                self.assertIsNone(binding.memory_dir)
                self._assert_not_installed_or_authorized(binding)

    def test_base_context_error_and_unknown_subclass_are_unbound_by_default(self):
        class FutureStale(execution_context.ContextError):
            pass

        for name, raised, reason in (
                ("base", execution_context.ContextError("lifecycle paths no longer match"), "ContextError"),
                ("unknown-subclass", FutureStale("a class this module does not know"), "unknown")):
            with self.subTest(row=name):
                self.setUp()
                self._uncached()
                with mock.patch.object(execution_context, "_revalidate_matched", side_effect=raised):
                    binding = execution_context.read_binding()
                self.assertEqual((binding.kind, binding.reason), ("unbound", reason))
                self.assertIsNone(binding.memory_dir)
                self._assert_not_installed_or_authorized(binding)

    def test_activation_and_store_moving_together_is_unbound_not_moved(self):
        """Check order puts the activation before the store identity, so ActivationStale surfaces first;
        the explicit store check must still win."""
        self._uncached()
        self._advance_activation()
        self._replace_store_identity()
        with self.assertRaises(execution_context.ActivationStale):
            execution_context.revalidate_context(self.fixture.context)
        binding = execution_context.read_binding()
        self.assertEqual((binding.kind, binding.reason), ("unbound", "StoreIdentityStale"))
        self.assertEqual(execution_context.explicit_store_check(self.fixture.context), "StoreIdentityStale")

    def test_drift_after_a_healthy_read_is_disclosed_on_the_next_read(self):
        self._uncached()
        self.assertEqual(execution_context.read_binding().kind, "healthy")
        self._advance_activation()
        binding = execution_context.read_binding()
        self.assertEqual((binding.kind, binding.reason), ("moved", "ActivationStale"))

    def test_the_read_only_binding_is_never_authorized_and_cannot_mint(self):
        self._uncached()
        self._advance_activation()
        binding = execution_context.read_binding()
        self.assertEqual(binding.kind, "moved")
        self._assert_not_installed_or_authorized(binding)
        with self.assertRaises(execution_context.ContextError):
            execution_context.mint_capability(binding.context, measured_cardinality=1)
        with self.assertRaises(execution_context.ContextError):
            execution_context.observe_state_fingerprint(binding.context)

    def test_reason_vocabulary_is_closed_and_carries_no_message_text(self):
        self._uncached()
        self._replace_store_identity()
        binding = execution_context.read_binding()
        document = binding.to_document()
        self.assertEqual(set(document), {"binding", "reason", "restart_clears"})
        self.assertIn(document["reason"], execution_context._READ_REASONS)
        self.assertNotIn("/", json.dumps(document))
        self.assertNotIn(self.fixture.memory, json.dumps(document))
        with self.assertRaises(execution_context.ContextError):
            execution_context.ReadBinding("unbound", "the store at /tmp moved", True, None, None)

    def test_every_context_error_subclass_has_a_declared_row(self):
        """Coverage assertion: an unlisted subclass in this module fails here before it can be omitted from
        the matrix."""
        declared = {"ExpectedStateStale", "ActivationStale", "AcceptedTreeStale", "StoreIdentityStale",
                    "BackupPointerStale", "ArtifactUnreadable"}
        found = {name for name, value in vars(execution_context).items()
                 if isinstance(value, type) and issubclass(value, execution_context.ContextError)
                 and value is not execution_context.ContextError}
        self.assertEqual(found, declared)
        self.assertTrue((declared - {"ExpectedStateStale"}) <= execution_context._READ_REASONS)
        self.assertNotIn("ExpectedStateStale", execution_context._READ_REASONS)   # always healed or healthy
        from memory import test_mcp_server as tms   # the matrix's own table is the second half of the check
        self.assertTrue((declared - {"ExpectedStateStale"}) <= set(tms.ReadDegradationMatrixTests.REASON.values()))

    def test_every_current_context_caller_still_fails_closed_under_every_non_refreshable_row(self):
        """The strict resolution is untouched: under every non-refreshable row (R3-R9) an uncached
        current_context() raises, read_binding() answers moved or unbound, and the callers that route on the
        strict resolution fail closed - drain.is_qualified, backup_vault._pointer_path and capture._health_path
        exercised under all seven rows; candidate_invocation.run under the five disk-drift rows only, because
        it loads the context library under its own module name, which the two declared monkeypatch rows (R8,
        R9) cannot reach and no disk drift shows. The write path's own mutation_scope is exercised under the
        same rows by the matrix."""
        import argparse
        from contextlib import ExitStack
        from memory import drain, candidate_invocation, backup_vault, capture

        class FutureStale(execution_context.ContextError):
            """A staleness class this module does not know (the matrix's R9)."""

        def base_context_error(stack):        # the matrix's R8: the base class's own raise site
            stack.enter_context(mock.patch.object(execution_context, "_path_identity",
                                                  return_value={"device": -1, "inode": -1}))

        def unknown_subclass(stack):          # the matrix's R9
            stack.enter_context(mock.patch.object(execution_context, "_revalidate_matched",
                                                  side_effect=FutureStale("a class this module does not know")))

        rows = (("activation", lambda _stack: self._advance_activation(), "moved"),
                ("tree", lambda _stack: self._remove_accepted_tree(), "moved"),
                ("store-identity", lambda _stack: self._replace_store_identity(), "unbound"),
                ("pointer", lambda _stack: self._rewrite_pointer(), "unbound"),
                ("artifact", lambda _stack: self._make_artifact_unreadable(), "unbound"),
                ("base-context-error", base_context_error, "unbound"),
                ("unknown-subclass", unknown_subclass, "unbound"))
        for name, inject, expected in rows:
            with self.subTest(row=name), ExitStack() as stack:
                self.setUp()
                self._uncached()
                inject(stack)
                self.assertEqual(execution_context.read_binding().kind, expected)
                self.assertIsNone(execution_context._CURRENT_CONTEXT)
                with self.assertRaises(execution_context.ContextError):
                    execution_context.current_context()
                self.assertFalse(drain.is_qualified())

                def fails_closed(call):
                    # candidate_invocation loads the context library under its own module name, so its
                    # staleness classes are distinct objects: match the ContextError lineage by name.
                    try:
                        call()
                    except Exception as exc:  # noqa: BLE001 - the lineage is the assertion
                        self.assertIn("ContextError", [klass.__name__ for klass in type(exc).__mro__], type(exc))
                        return
                    self.fail("did not fail closed")

                if inject not in (base_context_error, unknown_subclass):
                    # candidate_invocation loads the context library under its own module name, so the two
                    # DECLARED monkeypatch rows cannot reach its copy and there is no disk drift for it to see;
                    # every disk-drift row exercises it.
                    fails_closed(lambda: candidate_invocation.run(argparse.Namespace(
                        target_root=self.fixture.root, candidate_root=self.fixture.root, script="x")))
                fails_closed(backup_vault._pointer_path)
                fails_closed(lambda: capture._health_path("capture_status", "fallback"))
                # mutation_scope itself: on this (main, checked-in test) thread the test adapter would admit
                # the write, so its refusal is proven where the adapter cannot see - through the SDK client's
                # worker threads, under every row, in test_mcp_server.ReadDegradationMatrixTests
                # .test_write_authority_is_unchanged_across_the_rows.

    def test_the_read_refresh_and_the_write_reseal_seal_the_same_state_from_the_same_disk(self):
        """Drift alarm: the read-side refresh repeats the write-side re-seal's steps rather than sharing its
        code (the plan forbids editing the write re-seal inside this fix). If the two ever diverge in what they
        observe or seal, this fails: from the same document and the same disk both must produce the same
        state (everything but the receipt) and the same binding identity."""
        self._uncached()
        self._drift_fingerprint()
        execution_context._remember_context(self.fixture.context)
        write_side = execution_context.reseal_for_stale_state(self.fixture.context).to_document()
        with execution_context._CONTEXT_LOCK:
            execution_context._CURRENT_CONTEXT = None
        read_side = execution_context.refresh_root_for_read(self.fixture.context).to_document()
        for document in (write_side, read_side):
            document.pop("receipt")
        self.assertEqual(write_side, read_side)

    def test_degraded_allowed_sets_are_unchanged(self):
        self.assertEqual(mutation_contract.DEGRADED_ALLOWED_ENTRY_IDS, frozenset({
            "attended-keyword-mcp-search", "attended-keyword-search-heal",
            "attended-semantic-mcp-search", "attended-semantic-search-reconcile"}))
        self.assertNotIn("semantic-store-connect", mutation_contract.DEGRADED_ALLOWED_ENTRY_IDS)
        self.assertEqual(mutation_contract.DEGRADED_ALLOWED_TARGETS, frozenset({
            "degraded-health", "tracked-finding", "lifecycle-marker", "ephemeral-staging"}))

    def _assert_not_installed_or_authorized(self, binding):
        self.assertIsNone(execution_context._CURRENT_CONTEXT)
        if binding.context is not None:
            self.assertFalse(execution_context._is_authorized_context(binding.context))

if __name__ == "__main__":
    unittest.main()
