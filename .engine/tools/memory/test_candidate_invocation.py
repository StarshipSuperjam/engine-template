#!/usr/bin/env python3
"""Disposable attended-candidate lane tests for issue #1151 S05."""
from __future__ import annotations

import copy
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import jsonschema

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import accepted_hook_dispatch as dispatcher
from memory import execution_context


ROOT = Path(__file__).resolve().parents[3]
TOOLS = ROOT / ".engine/tools"
MEMORY_TOOLS = TOOLS / "memory"
RECEIPT_SCHEMA = ROOT / ".engine/schemas/candidate-disposable-receipt.v1.json"


class CandidateFixture:
    def __init__(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="engine-candidate-fixture-")
        self.base = Path(self.temporary.name).resolve()
        self.project = self.base / "canonical"
        self.common = self.base / "common.git"
        self.memory = self.project / ".engine/memory"
        self.pointer = self.project / ".engine/memory-backup/pointer.json"
        self.commit = "a" * 40
        self.tree = "b" * 40
        self.accepted = self.common / "engine/accepted-hooks/trees" / f"{self.commit}-{self.tree}"
        self.memory.mkdir(parents=True)
        self.pointer.parent.mkdir(parents=True)
        self.pointer.write_text('{"schema_version":1,"configured":false}\n', encoding="utf-8")
        accepted_tools = self.accepted / ".engine/tools"
        (accepted_tools / "memory").mkdir(parents=True)
        for source, destination in (
            (TOOLS / "accepted_hook_dispatch.py", accepted_tools / "accepted_hook_dispatch.py"),
            (MEMORY_TOOLS / "execution_context.py", accepted_tools / "memory/execution_context.py"),
            (MEMORY_TOOLS / "mutation_contract.py", accepted_tools / "memory/mutation_contract.py"),
            (MEMORY_TOOLS / "candidate_invocation.py", accepted_tools / "memory/candidate_invocation.py"),
        ):
            shutil.copy2(source, destination)
        activation_path = self.common / "engine/accepted-hooks/activation.json"
        activation_path.parent.mkdir(parents=True, exist_ok=True)
        self.activation = {
            "schema_version": "accepted-hook-activation.v1", "repository": "owner/repo",
            "commit": self.commit, "tree": self.tree, "engine_release": "9.9.9", "epoch": 7,
            "source": "reviewed-merge", "source_ref": "refs/heads/main",
            "authority": {"kind": "github-merged-pull", "evidence_id": "42"},
            "activated_at": "2026-01-01T00:00:00Z",
        }
        activation_path.write_text(json.dumps(self.activation, sort_keys=True) + "\n", encoding="utf-8")
        execution_context.ensure_store_identity(
            str(self.memory), project_repository="owner/repo", target_kind="canonical",
            initializer=execution_context._fixture_identity_initializer,
        )
        self.candidate = self.base / "candidate"
        self._make_candidate()
        self.targets: list[Path] = []

    def _make_candidate(self):
        candidate_memory = self.candidate / ".engine/tools/memory"
        candidate_memory.mkdir(parents=True)
        (self.candidate / ".engine/tools/validate.py").write_text(
            "ROOT = None\n", encoding="utf-8")
        (candidate_memory / "__init__.py").write_text("", encoding="utf-8")
        for name in ("execution_context.py", "mutation_contract.py", "mutation_authority.py", "ledger.py"):
            shutil.copy2(MEMORY_TOOLS / name, candidate_memory / name)
        self.script = candidate_memory / "candidate_append.py"
        self.script.write_text(
            "import os\n"
            "from memory import ledger\n"
            "ledger.append({'body':'private candidate'}, "
            "path=os.path.join(os.environ['ENGINE_MEMORY_DIR'], 'ledger.ndjson'))\n",
            encoding="utf-8",
        )
        for command in (
            ("git", "init", "-b", "main", str(self.candidate)),
            ("git", "-C", str(self.candidate), "config", "user.name", "Candidate Fixture"),
            ("git", "-C", str(self.candidate), "config", "user.email", "fixture@example.invalid"),
            ("git", "-C", str(self.candidate), "remote", "add", "origin",
             "https://github.com/owner/repo.git"),
            ("git", "-C", str(self.candidate), "add", "."),
            ("git", "-C", str(self.candidate), "commit", "-m", "candidate"),
        ):
            subprocess.run(command, check=True, capture_output=True)

    def bootstrap(self):
        root_info = os.stat(self.project)
        pointer_digest = execution_context._file_digest(str(self.pointer))
        return {
            "schema_version": "accepted-hook-context.v1",
            "activation": {
                key: self.activation[key]
                for key in ("repository", "commit", "tree", "engine_release", "epoch")
            },
            "canonical": {
                "project_root": str(self.project),
                "project_root_identity": {"device": root_info.st_dev, "inode": root_info.st_ino},
                "git_common_dir": str(self.common), "memory_dir": str(self.memory),
                "backup_pointer_digest": pointer_digest,
            },
        }

    def new_target(self) -> Path:
        path = Path(os.path.realpath(tempfile.gettempdir())) / f"engine-memory-candidate-{secrets.token_hex(10)}"
        dispatcher._create_disposable_target(str(path), self.bootstrap()["canonical"])
        self.targets.append(path)
        return path

    def context(self, target: Path, *, operation="ledger-append", extensions=None):
        candidate_extensions = {
            "candidate_authorized_registry_ids": dispatcher._candidate_registry_boundary(
                execution_context, operation),
            **(extensions or {}),
        }
        return execution_context.resolve_execution_context(
            self.bootstrap(), accepted_tree=str(self.accepted),
            script=".engine/tools/memory/candidate_append.py", target_kind="disposable",
            target_root=str(target), operation_id=operation, provider="codex", run_id="run-1151",
            task_id="task-1151", extensions=candidate_extensions,
            identity_initializer=execution_context._fixture_identity_initializer,
        )

    def invoke(self, context, target: Path, *, script: Path | None = None):
        helper = self.accepted / ".engine/tools/memory/candidate_invocation.py"
        env = {
            key: value for key, value in os.environ.items()
            if key not in {
                "PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE", "PYTHONSTARTUP",
                "ENGINE_MEMORY_DIR", "ENGINE_PROJECT_ROOT", "ENGINE_PERSISTENT_EXECUTION_CONTEXT",
            }
        }
        env["ENGINE_PERSISTENT_EXECUTION_CONTEXT"] = context.to_json()
        return subprocess.run(
            [sys.executable, "-I", "-S", str(helper), "--candidate-root", str(self.candidate),
             "--script", str(script or self.script), "--target-root", str(target), "--"],
            capture_output=True, env=env, timeout=30,
        )

    def cleanup(self):
        execution_context._CURRENT_CONTEXT = None
        os.environ.pop(execution_context.CONTEXT_ENV, None)
        for target in self.targets:
            shutil.rmtree(target, ignore_errors=True)
        self.temporary.cleanup()


class CandidateInvocationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = CandidateFixture()

    def tearDown(self):
        self.fixture.cleanup()

    def test_valid_run_changes_only_private_state_and_emits_complete_receipt_shape(self):
        target = self.fixture.new_target()
        context = self.fixture.context(
            target, extensions={"future_authorization": None, "opaque_consumer_fixture": {"version": 1}})
        canonical_before = dispatcher._safe_inventory(str(self.fixture.memory), details=False)
        result = self.fixture.invoke(context, target)
        canonical_after = dispatcher._safe_inventory(str(self.fixture.memory), details=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(canonical_before, canonical_after)
        ledger = target / "memory/ledger.ndjson"
        self.assertEqual(json.loads(ledger.read_text(encoding="utf-8")), {"body": "private candidate"})

        candidate = dispatcher._candidate_code_identity(str(self.fixture.candidate), str(self.fixture.script))
        receipt = dispatcher._candidate_receipt(
            activation=self.fixture.activation, accepted_tree=str(self.fixture.accepted), candidate=candidate,
            context=context, target_root=str(target), result=result, before=canonical_before,
            after=canonical_after, timed_out=False,
        )
        schema = json.loads(RECEIPT_SCHEMA.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.validate(receipt, schema)
        unhashed = copy.deepcopy(receipt)
        supplied = unhashed.pop("receipt_digest")
        unhashed["receipt_digest"] = None
        self.assertEqual(supplied, dispatcher._json_digest(unhashed))
        self.assertTrue(receipt["canonical"]["unchanged"])
        self.assertIsNone(receipt["future_authorization"])
        self.assertEqual(receipt["operation"]["invocation_mode"], "attended")
        self.assertEqual(receipt["invocation"]["run_id"], "run-1151")

    def test_target_refusal_matrix_preserves_canonical_bytes_and_preexisting_targets(self):
        canonical_before = dispatcher._safe_inventory(str(self.fixture.memory), details=False)
        canonical = self.fixture.bootstrap()["canonical"]
        system_temp = Path(os.path.realpath(tempfile.gettempdir()))
        preexisting = system_temp / f"engine-memory-candidate-{secrets.token_hex(10)}"
        preexisting.mkdir()
        marker = preexisting / "owned-by-caller"
        marker.write_bytes(b"unchanged")
        symlink = system_temp / f"engine-memory-candidate-{secrets.token_hex(10)}"
        symlink.symlink_to(preexisting)
        self.fixture.targets.extend([preexisting, symlink])
        cases = {
            "missing/relative": "engine-memory-candidate-relative",
            "normalized-dot-dot": str(system_temp / ".." / system_temp.name
                                      / f"engine-memory-candidate-{secrets.token_hex(5)}"),
            "symlink": str(symlink),
            "canonical-alias": str(self.fixture.project),
            "caller-prepopulated": str(preexisting),
            "outside-temp": str(self.fixture.base / f"engine-memory-candidate-{secrets.token_hex(5)}"),
            "bad-prefix": str(system_temp / f"candidate-{secrets.token_hex(5)}"),
            "nested-target": str(preexisting / f"engine-memory-candidate-{secrets.token_hex(5)}"),
        }
        refused = []
        for label, path in cases.items():
            with self.subTest(label=label), self.assertRaises(dispatcher.QualificationError):
                dispatcher._create_disposable_target(path, canonical)
            refused.append(label)
        self.assertEqual(marker.read_bytes(), b"unchanged")
        self.assertEqual(dispatcher._safe_inventory(str(self.fixture.memory), details=False), canonical_before)
        self.assertEqual(set(refused), set(cases))

    def test_context_target_code_effect_cardinality_and_run_mismatches_refuse(self):
        # Wrong target.
        first = self.fixture.new_target()
        other = self.fixture.new_target()
        context = self.fixture.context(first)
        wrong_target = self.fixture.invoke(context, other)
        self.assertNotEqual(wrong_target.returncode, 0)
        self.assertFalse((first / "memory/ledger.ndjson").exists())
        self.assertFalse((other / "memory/ledger.ndjson").exists())

        # Wrong code path (outside candidate memory tools).
        outside = self.fixture.candidate / ".engine/tools/outside.py"
        outside.write_text("raise SystemExit(0)\n", encoding="utf-8")
        wrong_code = self.fixture.invoke(context, first, script=outside)
        self.assertNotEqual(wrong_code.returncode, 0)

        # The selected effect does not authorize ledger append.
        effect_target = self.fixture.new_target()
        wrong_effect = self.fixture.context(effect_target, operation="ledger-generation-bump")
        effect_result = self.fixture.invoke(wrong_effect, effect_target)
        self.assertNotEqual(effect_result.returncode, 0)
        self.assertFalse((effect_target / "memory/ledger.ndjson").exists())

        # A tampered run identity invalidates the sealed context.
        run_target = self.fixture.new_target()
        run_context = self.fixture.context(run_target)
        document = run_context.to_document()
        document["invocation"]["run_id"] = "wrong-run"
        env = dict(os.environ)
        env[execution_context.CONTEXT_ENV] = json.dumps(document)
        helper = self.fixture.accepted / ".engine/tools/memory/candidate_invocation.py"
        run_result = subprocess.run(
            [sys.executable, "-I", "-S", str(helper), "--candidate-root", str(self.fixture.candidate),
             "--script", str(self.fixture.script), "--target-root", str(run_target)],
            capture_output=True, env=env, timeout=30,
        )
        self.assertNotEqual(run_result.returncode, 0)
        self.assertFalse((run_target / "memory/ledger.ndjson").exists())

        # Hidden measured cardinality is still classified by the accepted contract before writing.
        cardinality_script = self.fixture.candidate / ".engine/tools/memory/cardinality.py"
        cardinality_script.write_text(
            "import os\nfrom memory import ledger\n"
            "ledger.append({'body':'too wide'}, path=os.path.join(os.environ['ENGINE_MEMORY_DIR'], "
            "'ledger.ndjson'), _engine_measured_cardinality=2)\n",
            encoding="utf-8",
        )
        cardinality_target = self.fixture.new_target()
        cardinality_context = execution_context.resolve_execution_context(
            self.fixture.bootstrap(), accepted_tree=str(self.fixture.accepted),
            script=".engine/tools/memory/cardinality.py", target_kind="disposable",
            target_root=str(cardinality_target), operation_id="ledger-append", provider="codex",
            run_id="run-1151", task_id="task-1151",
            identity_initializer=execution_context._fixture_identity_initializer,
        )
        cardinality_result = self.fixture.invoke(
            cardinality_context, cardinality_target, script=cardinality_script)
        self.assertNotEqual(cardinality_result.returncode, 0)
        self.assertFalse((cardinality_target / "memory/ledger.ndjson").exists())

    def test_candidate_lane_is_attended_only_and_issues_no_canonical_canary(self):
        parser = dispatcher._parser()
        self.assertNotIn("candidate", dispatcher.AUTOMATIC_MUTATORS)
        automatic = {
            ".engine/tools/boot.py", ".engine/tools/close.py", ".engine/tools/memory/compact.py",
            ".engine/tools/memory/erasure_observer.py", ".engine/tools/memory/backup_vault.py",
        }
        self.assertEqual(set(dispatcher.AUTOMATIC_MUTATORS), automatic)
        args = parser.parse_args([
            "candidate", "--root", str(self.fixture.project), "--candidate-root", str(self.fixture.candidate),
            "--script", str(self.fixture.script), "--target-root",
            str(Path(os.path.realpath(tempfile.gettempdir()))
                / f"engine-memory-candidate-{secrets.token_hex(5)}"),
            "--operation", "ledger-append", "--provider", "codex",
        ])
        self.assertEqual(args.command, "candidate")
        with self.assertRaises(execution_context.ContextError):
            execution_context.resolve_execution_context(
                self.fixture.bootstrap(), accepted_tree=str(self.fixture.accepted), script=str(self.fixture.script),
                target_kind="disposable", target_root=str(self.fixture.new_target()),
                operation_id="automatic-capture", provider="codex",
                identity_initializer=execution_context._fixture_identity_initializer,
            )
        with self.assertRaisesRegex(dispatcher.QualificationError, "outside its disposable target"):
            dispatcher._candidate_registry_boundary(execution_context, "automatic-backup")
        self.assertEqual(
            dispatcher._candidate_registry_boundary(execution_context, "ledger-append"),
            ["ledger-append"],
        )
        source = (TOOLS / "accepted_hook_dispatch.py").read_text(encoding="utf-8")
        candidate_source = source[source.index("def dispatch_candidate"):source.index("def _parser")]
        self.assertNotIn("canary", candidate_source.casefold())
        self.assertNotIn("future_authorization\": {", candidate_source)


class TypedStalenessInvocationCompatTests(unittest.TestCase):
    """The typed staleness subclasses n3 introduces stay inside the ContextError family, so every existing
    `except ContextError` boundary on the candidate and invocation paths keeps catching them unchanged — the
    'existing handlers still catch the subclasses' guarantee, checked directly rather than through a fixture."""

    def test_every_typed_staleness_is_a_context_error(self):
        for cls in (execution_context.ExpectedStateStale, execution_context.ActivationStale,
                    execution_context.AcceptedTreeStale, execution_context.StoreIdentityStale,
                    execution_context.BackupPointerStale, execution_context.ArtifactUnreadable):
            with self.subTest(cls.__name__):
                self.assertTrue(issubclass(cls, execution_context.ContextError))
                with self.assertRaises(execution_context.ContextError):
                    raise cls("typed staleness caught by a base handler")

    def test_only_expected_state_stale_is_the_refreshable_class(self):
        # The write path keys its one-shot re-seal on exactly this type; every sibling must NOT be a subtype of
        # it, or a genuine binding change would be mistaken for a refreshable fingerprint drift.
        for cls in (execution_context.ActivationStale, execution_context.AcceptedTreeStale,
                    execution_context.StoreIdentityStale, execution_context.BackupPointerStale,
                    execution_context.ArtifactUnreadable):
            with self.subTest(cls.__name__):
                self.assertFalse(issubclass(cls, execution_context.ExpectedStateStale))


class TestCandidateLaneBytecodeHygiene(unittest.TestCase):
    """W2: the candidate helper the dispatcher launches from the accepted tree must not write __pycache__
    into that tree, or its content digest changes and the materialization is judged invalid."""

    def setUp(self):
        self.fixture = CandidateFixture()

    def tearDown(self):
        self.fixture.cleanup()

    def _pycache(self, root):
        found = []
        for cur, dirs, _files in os.walk(root):
            for name in dirs:
                if name == "__pycache__":
                    found.append(os.path.relpath(os.path.join(cur, name), root))
        return sorted(found)

    def _run_helper(self, *, dash_b):
        target = self.fixture.new_target()
        context = self.fixture.context(target)
        helper = self.fixture.accepted / ".engine/tools/memory/candidate_invocation.py"
        env = {
            key: value for key, value in os.environ.items()
            if key not in {
                "PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE", "PYTHONSTARTUP",
                "ENGINE_MEMORY_DIR", "ENGINE_PROJECT_ROOT", "ENGINE_PERSISTENT_EXECUTION_CONTEXT",
            }
        }
        env["ENGINE_PERSISTENT_EXECUTION_CONTEXT"] = context.to_json()
        flags = ["-I", "-S", "-B"] if dash_b else ["-I", "-S"]
        return subprocess.run(
            [sys.executable, *flags, str(helper), "--candidate-root", str(self.fixture.candidate),
             "--script", str(self.fixture.script), "--target-root", str(target), "--"],
            capture_output=True, env=env, timeout=30)

    def test_helper_launched_with_dash_b_leaves_the_accepted_tree_clean(self):
        # This is how run_candidate now launches the helper (see the argv source assertion below).
        result = self._run_helper(dash_b=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self._pycache(self.fixture.accepted), [],
                         "the candidate helper wrote __pycache__ into the accepted tree under -B")

    def test_without_dash_b_the_helper_poisons_the_tree_so_the_dash_b_test_is_not_vacuous(self):
        # A standing witness that the hazard is real: without -B the same run drops __pycache__ into the
        # accepted tree. It is exactly this write that -B (and the module-level guard) suppress.
        result = self._run_helper(dash_b=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn(".engine/tools/memory/__pycache__", self._pycache(self.fixture.accepted))

    def test_the_candidate_launch_sites_carry_dash_b(self):
        source = (TOOLS / "accepted_hook_dispatch.py").read_text(encoding="utf-8")
        # dispatch_candidate re-execs the accepted dispatcher; run_candidate launches the helper.
        self.assertIn('sys.executable, "-I", "-S", "-B", accepted_dispatch, "_run-candidate"', source)
        self.assertIn('sys.executable, "-I", "-S", "-B", helper, "--candidate-root"', source)

if __name__ == "__main__":
    unittest.main()
