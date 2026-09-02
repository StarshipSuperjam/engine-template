#!/usr/bin/env python3
"""Authority, under-lock drift, and converted-call-graph tests for issue StarshipSuperjam/engine-template#1151 S04."""
from __future__ import annotations

import importlib
import gc
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
