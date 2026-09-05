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
from unittest import mock
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

# ======================================================================================================
# C1 node 2 (program prg_d15d7dc8f3df): a BEST-EFFORT reproduction of the recall failure in a disposable
# clone, honest about what it is and is not.
#
# WHAT IT IS. A fresh `git clone --no-hardlinks` of this repository into a sandbox with its own git common
# directory, launched through the SHIPPED launchers (.mcp.json for Claude, .codex/config.toml for Codex)
# with a scrubbed environment, seeded with synthetic records, and driven through the real MCP client:
# search, recall-by-meaning, recall-window. Every observation is classified by a fixed rule that refuses
# the three counterfeit "reproductions", and the desired-behavior acceptance probe is PRESERVED with its
# actual result — never asserted, never skipped.
#
# WHAT IT IS NOT. A fresh clone has no accepted activation and a local-path origin, so the dispatcher falls
# to the DEGRADED launch (live checkout, no execution context). The operator's failing server ran the
# ATTENDED launch from a materialized accepted tree. That launcher difference is recorded on every run as a
# MECHANISM-REMOVING difference; nothing here claims production faithfulness, and the real attended-path
# trace is carried forward to be captured by the merged stranding log after deployment.
#
# ISOLATION. The clone is accepted only if `execution_context.validate_disposable_target` (the audited
# alias/inode/shared-git-metadata refusal) accepts it against the canonical checkout. The launch env drops
# every GIT_*, ENGINE_*, CLAUDE_*, CODEX_*, PYTHON* and UV_* variable (a leaked GIT_DIR from a build worktree
# would make `git -C <clone>` resolve the CANONICAL common directory and the degraded launch would bind the
# operator's real memory — the dispatcher scrubs nothing itself), then sets HOME/TMPDIR/CLAUDE_PROJECT_DIR
# inside the sandbox. The clone runs against THIS checkout's already-provisioned dependency environment
# (UV_PROJECT_ENVIRONMENT + UV_NO_SYNC; the same frozen lock), UV_CACHE_DIR and UV_PYTHON_INSTALL_DIR are
# pinned to the real locations so the HOME override cannot trigger a download, and UV_OFFLINE=1 makes any
# attempt to fetch fail loudly. So the reproduction needs NO network: the one assumption is that the engine's
# own environment is synced (any run of its selftest guarantees it). Sharing the dependency environment is a
# recorded, mechanism-PRESERVING difference — dependencies are not the engine's code, and every engine module
# the launched server imports comes from the clone (the dispatcher puts the clone's tools root on PYTHONPATH).
# ======================================================================================================

import asyncio  # noqa: E402
import re  # noqa: E402
import time  # noqa: E402
import tomllib  # noqa: E402

from memory import records as _records  # noqa: E402

_GENERIC_TOOL_ERROR = re.compile(r"Error executing tool [a-z0-9\-]+")
_SCRUB_PREFIXES = ("GIT_", "ENGINE_", "CLAUDE_", "CODEX_", "PYTHON", "UV_", "VIRTUAL_ENV", "XDG_")
_KEEP_KEYS = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "SHELL", "USER", "LOGNAME")

# The committed finding of this node, checked against reality on every run: the class the best-effort
# reproduction ACTUALLY yields on each shipped launcher. If a clone ever reproduces the failure, the
# reproduction test fails loudly here rather than silently changing the record — and if it never does, the
# record says so, in the launcher's own terms, without manufacturing a failure to fill an evidence label.
DISPOSITION = {
    "real_attended_cause": "CAPTURED by the deployed stranding log (2026-09-05, program prg_d15d7dc8f3df C1 -> "
                           "C2): the FIRST context resolution in a memory-server process raised the typed "
                           "staleness (ActivationStale; ExpectedStateStale) before caching, the read caveat "
                           "swallowed it once, and ledger_dir()'s second resolution re-raised it unhandled - "
                           "frames wrapper -> guarded -> ledger.ledger_dir -> execution_context.current_context "
                           "-> revalidate_context -> _revalidate_matched. C2 fixes it in-process (the read seam; "
                           "test_mcp_server.ReadDegradationMatrixTests). THIS harness still cannot exercise it: a "
                           "degraded launch installs no context, so no first resolution can be stale here.",
    "launcher_actually_run": "degraded (live checkout, no execution context): a fresh clone has no accepted "
                             "activation — a MECHANISM-REMOVING difference from the attended production launch.",
    "expected_class": {"claude": "not-reproduced", "codex": "not-reproduced"},
    "expected_label": {"claude": "semantic-unavailable-keyword-ok", "codex": "semantic-unavailable-keyword-ok"},
    # The acceptance probe's ACTUAL result, pinned like the outcome so a change in either fails loudly.
    "expected_probe": {"claude": {"passed": False, "reason": "semantic-unavailable"},
                       "codex": {"passed": False, "reason": "semantic-unavailable"}},
    "probe_disposition": (
        "A THIRD outcome, stated plainly: the harness did NOT reproduce the fault, AND the acceptance probe "
        "FAILED — for a separate, recorded reason (the degraded launcher cannot build the meaning index, so "
        "meaning recall reports itself unavailable), not because recall returned the wrong records. Neither "
        "fact is evidence about the real bug. The probe is a meaningful assertion only under an attended "
        "launch, which is the fix child's contract, not this harness's."),
    "mechanism_removing_differences": [
        "launcher: degraded (live checkout, no execution context) instead of attended (materialized accepted "
        "tree with an installed context) — the recall path under investigation runs behind the guard's "
        "qualified branch, which the clone never enters.",
        "meaning recall: reports itself unavailable ('not qualified to build the meaning index') instead of "
        "reconciling the semantic store — the exact operation suspected in the production failure never runs.",
        "derived caches: the keyword-index rebuild is refused unqualified and recall falls through to a "
        "ledger scan, so no cache is reconciled or written — the cache-trust seam (C3) is not exercised.",
        "authority: no activation, so no activation-advance can happen under the running server — the "
        "operator's moved-commit scenario cannot occur here.",
    ],
}


def _run(argv, *, env=None, cwd=None, timeout=60) -> str:
    """Run one command. The environment is SCRUBBED BY DEFAULT (`_setup_env()`: every `GIT_*` variable
    removed) — a caller that wants a specific environment names it. Opt-in scrubbing was how a leaked
    GIT_DIR twice found a git call in this file that inherited it; a default cannot be forgotten."""
    env = _setup_env() if env is None else env
    done = subprocess.run(argv, capture_output=True, text=True, env=env, cwd=cwd, timeout=timeout, check=False)
    if done.returncode != 0:
        raise AssertionError(f"{' '.join(argv)} failed ({done.returncode}): {(done.stderr or done.stdout)[:400]}")
    return done.stdout.strip()


def _real_home() -> str:
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_dir
    except (ImportError, KeyError, OSError):
        return os.path.expanduser("~")


def _setup_env() -> dict:
    """The environment the harness's OWN git calls run under: the inherited one minus every `GIT_*`
    variable, so the harness identifies what canonical IS under the same rule it launches under — a leaked
    GIT_DIR must not be allowed to name the repository this sandbox is then built to protect."""
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


class DisposableClone:
    """One sandboxed clone of this repository, its scrubbed launch environment, and the shipped launchers."""

    def __init__(self):
        setup = _setup_env()
        self.source = str(ROOT)
        # Cloned BY COMMIT, never by branch name: the merge check runs its tests from a detached, shallow
        # checkout where `--abbrev-ref HEAD` is the literal `HEAD` and no local branch exists, so a clone by
        # branch fails there on every run. `--no-checkout` fetches what the source has (its branches, or its
        # detached HEAD) and the sandbox is then detached at the resolved commit.
        self.commit = _run(["git", "-C", self.source, "rev-parse", "HEAD"], env=setup)
        self.branch = _run(["git", "-C", self.source, "rev-parse", "--abbrev-ref", "HEAD"], env=setup)  # informational
        canonical = _run(["git", "-C", self.source, "worktree", "list", "--porcelain"], env=setup).split("\n\n", 1)[0]
        self.canonical_root = os.path.realpath(
            next(line[len("worktree "):] for line in canonical.splitlines() if line.startswith("worktree ")))
        common = _run(["git", "-C", self.source, "rev-parse", "--git-common-dir"], env=setup)
        self.canonical_common = os.path.realpath(common if os.path.isabs(common) else os.path.join(self.source, common))
        self.canonical_memory = os.path.join(self.canonical_root, ".engine", "memory")
        self.sandbox = os.path.realpath(tempfile.mkdtemp(prefix="engine-repro-clone-"))
        try:
            self._build(setup)
        except BaseException:
            # A failure anywhere past mkdtemp — a clone timeout, a refused target, a missing uv — must not
            # strand a full clone of the repository under the temp directory: tearDownClass never runs when
            # setUpClass raised, so the sandbox is removed here, then the failure is re-raised unchanged.
            self.cleanup()
            raise

    def _build(self, setup: dict):
        self.root = os.path.join(self.sandbox, "repo")
        self.home = os.path.join(self.sandbox, "home")
        self.tmp = os.path.join(self.sandbox, "tmp")
        os.makedirs(self.home)
        os.makedirs(self.tmp)
        _run(["git", "clone", "--no-hardlinks", "--quiet", "--no-checkout", self.canonical_root, self.root],
             env=setup, timeout=300)
        _run(["git", "-C", self.root, "checkout", "--quiet", "--detach", self.commit], env=setup, timeout=300)
        self.head = _run(["git", "-C", self.root, "rev-parse", "HEAD"], env=setup)
        if self.head != self.commit:   # an explicit check, so it survives an optimized interpreter
            raise AssertionError(f"the sandbox landed on {self.head}, not the resolved commit {self.commit}")
        # The audited refusal: a clone that aliased the canonical project, memory, or git metadata is rejected
        # here, before anything is launched against it.
        execution_context.validate_disposable_target(
            self.root, canonical_project_root=self.canonical_root, canonical_memory_dir=self.canonical_memory,
            canonical_git_common_dir=self.canonical_common)
        pointer = os.path.join(self.root, ".engine", "memory-backup", "pointer.json")
        document = json.loads(Path(pointer).read_text(encoding="utf-8"))
        if document.get("configured") is not False:
            Path(pointer).write_text('{"schema_version": 1, "configured": false}\n', encoding="utf-8")
        self.memory = os.path.join(self.root, ".engine", "memory")
        self.nonce = f"repro-{secrets.token_hex(6)}"
        self.uv_cache = _run(["uv", "cache", "dir"])
        # Where uv keeps its managed interpreters: the host's own setting when it has one, else what uv says —
        # never a hardcoded default, which would fail loudly under UV_OFFLINE on a host that moved it.
        self.uv_python = os.environ.get("UV_PYTHON_INSTALL_DIR") or _run(["uv", "python", "dir"])
        self.seed_records()

    def seed_records(self):
        """Synthetic records that preserve the relationship under investigation — a captured conversation the
        session should be able to recall by meaning — with a nonce no real record could share."""
        os.makedirs(self.memory, exist_ok=True)
        now = int(time.time())
        lines = []
        for seq, text in enumerate((
            f"The build pipeline's flaky deploy step was traced to a stale cache ({self.nonce}).",
            f"We decided to clear the deploy cache on every release, tagging it {self.nonce}.",
            f"Follow-up: the cache-clearing step is now part of the release checklist ({self.nonce}).",
        ), start=1):
            lines.append(json.dumps({
                "ts": now - 100 + seq, "role": "observation", "tags": ["repro"], "text": text,
                "session_id": f"synthetic-{self.nonce}", "seq": seq, "speaker": "user",
                _records.RECORD_ID_KEY: _records.new_record_id(),
            }, sort_keys=True))
        Path(os.path.join(self.memory, "ledger.ndjson")).write_text("\n".join(lines) + "\n", encoding="utf-8")

    def env(self, base=None) -> dict:
        """The launch environment: everything that could bind the launched server to the canonical checkout is
        dropped, everything that would make uv reach the network is pinned."""
        source = dict(os.environ if base is None else base)
        kept = {key: value for key, value in source.items()
                if key in _KEEP_KEYS or not key.startswith(_SCRUB_PREFIXES)}
        for key in list(kept):
            if key.startswith(_SCRUB_PREFIXES) or key in ("HOME", "TMPDIR", "TMP", "TEMP"):
                kept.pop(key)
        kept.update({
            "HOME": self.home, "TMPDIR": self.tmp, "CLAUDE_PROJECT_DIR": self.root,
            "UV_CACHE_DIR": self.uv_cache,
            "UV_PYTHON_INSTALL_DIR": self.uv_python,
            "UV_PROJECT_ENVIRONMENT": os.path.realpath(os.path.join(self.source, ".engine", ".venv")),
            "UV_NO_SYNC": "1", "UV_OFFLINE": "1", "PYTHONNOUSERSITE": "1",
        })
        return kept

    def launcher(self, kind: str) -> tuple[str, list[str], str]:
        """The shipped launcher's command, argv and working directory, parsed from the clone's own copy of the
        config — never hand-written, so the reproduction runs what the deployment runs."""
        if kind == "claude":
            block = json.loads(Path(os.path.join(self.root, ".mcp.json")).read_text(encoding="utf-8"))
            entry = block["mcpServers"]["engine-memory"]
            args = [item.replace("${CLAUDE_PROJECT_DIR:-.}", self.root) for item in entry["args"]]
            return entry["command"], args, self.root
        if kind == "codex":
            block = tomllib.loads(Path(os.path.join(self.root, ".codex", "config.toml")).read_text(encoding="utf-8"))
            entry = block["mcp_servers"]["engine-memory"]
            return entry["command"], list(entry["args"]), self.root
        raise ValueError(kind)

    def resolved_launch_paths(self, env: dict) -> dict:
        """What the dispatcher's degraded launch would bind, computed the way it computes them (`git -C
        <root>` for the common dir and the main checkout) under a GIVEN environment — so the same question can
        be asked with a hostile environment and with the scrubbed one."""
        common = _run(["git", "-C", self.root, "rev-parse", "--git-common-dir"], env=env)
        common = os.path.realpath(common if os.path.isabs(common) else os.path.join(self.root, common))
        first = _run(["git", "-C", self.root, "worktree", "list", "--porcelain"], env=env).split("\n\n", 1)[0]
        main = os.path.realpath(next(line[len("worktree "):] for line in first.splitlines()
                                     if line.startswith("worktree ")))
        return {
            "project_root": main, "git_common_dir": common,
            "memory_dir": os.path.realpath(os.path.join(main, ".engine", "memory")),
            "activation": os.path.join(common, "engine", "accepted-hooks", "activation.json"),
        }

    def containment(self, paths: dict) -> dict:
        return {key: execution_context._within(os.path.realpath(value), self.sandbox)
                for key, value in paths.items()}

    def cleanup(self):
        shutil.rmtree(self.sandbox, ignore_errors=True)


_SHARED: dict = {}


def _shared_clone() -> DisposableClone:
    """ONE clone for every class in this module: a full, hardlink-free clone of the repository is the
    expensive part of these tests and nothing here mutates the clone's history, so the isolation and the
    reproduction classes share it. Torn down once, when the module is done."""
    if "clone" not in _SHARED:
        _SHARED["clone"] = DisposableClone()
        unittest.addModuleCleanup(_SHARED["clone"].cleanup)
    return _SHARED["clone"]


async def drive_launcher(clone: DisposableClone, kind: str) -> dict:
    """Launch the clone's server through one shipped launcher and observe the three read tools. Returns
    the observation: the launcher label the server itself reports, the dispatcher's stderr line, and each
    tool's raw outcome (error or payload)."""
    from mcp import ClientSession, StdioServerParameters, stdio_client

    command, args, cwd = clone.launcher(kind)
    params = StdioServerParameters(command=command, args=args, env=clone.env(), cwd=cwd)
    stderr_path = os.path.join(clone.sandbox, f"{kind}-server-stderr.txt")
    observation = {"launcher": kind, "command": [command, *args], "cwd": cwd, "tools": {}}

    async def call(session, name, arguments):
        result = await session.call_tool(name, arguments)
        text = result.content[0].text if result.content else ""
        entry = {"is_error": bool(result.is_error), "text": text[:2000]}
        if not result.is_error:
            try:
                entry["payload"] = json.loads(text)
            except ValueError:
                entry["payload"] = None
        return entry

    with open(stderr_path, "w", encoding="utf-8") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                observation["tool_names"] = sorted(tool.name for tool in (await session.list_tools()).tools)
                observation["tools"]["health"] = await call(session, "health", {})
                observation["tools"]["search"] = await call(session, "search", {"query": clone.nonce, "limit": 5})
                if "recall-by-meaning" in observation["tool_names"]:
                    observation["tools"]["recall-by-meaning"] = await call(
                        session, "recall-by-meaning",
                        {"query": "why did the deployment step keep failing and what did we change", "limit": 5})
                hits = (observation["tools"]["search"].get("payload") or {}).get("results") or []
                if hits:
                    observation["tools"]["recall-window"] = await call(
                        session, "recall-window", {"session_id": hits[0]["session_id"], "anchor_seq": hits[0]["seq"]})
    observation["stderr"] = Path(stderr_path).read_text(encoding="utf-8")[:2000]
    health = (observation["tools"]["health"].get("payload") or {}).get("diagnostics") or {}
    observation["qualification_reported"] = health.get("qualification")
    observation["diagnostics_armed"] = health.get("armed")
    return observation


def classify_outcome(observation: dict) -> dict:
    """The fixed rule: a reproduction is a read tool answering with the GENERIC boundary error — the flattened
    crash the operator saw. Everything else is NOT a reproduction, and the three known counterfeits are named
    so they can never be mistaken for one: a clean stale-context caveat is a designed degradation; keyword
    recall working while meaning recall reports itself unavailable is a designed degradation; a generic
    error on some other tool without the recall mechanism engaged is a different fault."""
    tools = observation.get("tools", {})
    for name in ("recall-by-meaning", "search", "recall-window"):
        entry = tools.get(name)
        if entry and entry.get("is_error") and _GENERIC_TOOL_ERROR.search(entry.get("text", "")):
            return {"class": "reproduced", "label": f"generic-boundary-error:{name}"}
    for name, entry in tools.items():
        if entry.get("is_error") and _GENERIC_TOOL_ERROR.search(entry.get("text", "")):
            return {"class": "not-reproduced", "label": f"generic-error-without-recall-mechanism:{name}"}
    meaning = tools.get("recall-by-meaning")
    search = tools.get("search")
    if "recall-by-meaning" not in observation.get("tool_names", []):
        return {"class": "not-reproduced", "label": "semantic-tool-absent"}
    if meaning and not meaning.get("is_error") and (meaning.get("payload") or {}).get("unavailable") \
            and search and not search.get("is_error"):
        return {"class": "not-reproduced", "label": "semantic-unavailable-keyword-ok"}
    if any(((entry.get("payload") or {}).get("outcome") or {}).get("binding") in ("moved", "unbound")
           for entry in tools.values() if entry):
        return {"class": "not-reproduced", "label": "caveat-only"}   # a disclosed degradation (C2's outcome)
    if any(entry.get("is_error") for entry in tools.values()):
        return {"class": "not-reproduced", "label": "other-error"}
    return {"class": "not-reproduced", "label": "healthy"}


# The DESIRED behavior — meaning-based recall returns the seeded conversation — lives in its own uncollected
# carrier module (memory/recall_acceptance_probe.py): the fix child imports and asserts it; this harness only
# evaluates it and RECORDS its actual result, never asserted, never skipped.
from memory.recall_acceptance_probe import recall_acceptance_probe  # noqa: E402


class CounterfeitReproductionTests(unittest.TestCase):
    """The classifier's fixed rule, pinned: exactly one shape counts as a reproduction."""

    def _observation(self, **tools):
        return {"tool_names": ["health", "search", "recall-by-meaning", "recall-window"],
                "tools": {"health": {"is_error": False, "payload": {"status": "ok"}}, **tools}}

    def test_only_the_generic_boundary_error_on_a_recall_tool_is_a_reproduction(self):
        seen = classify_outcome(self._observation(
            **{"search": {"is_error": False, "payload": {"results": []}},
               "recall-by-meaning": {"is_error": True, "text": "Error executing tool recall-by-meaning"}}))
        self.assertEqual(seen, {"class": "reproduced", "label": "generic-boundary-error:recall-by-meaning"})

    def test_a_clean_stale_context_caveat_is_not_a_reproduction(self):
        seen = classify_outcome(self._observation(
            **{"search": {"is_error": False, "payload": {"results": [], "outcome": {
                   "binding": "moved", "completeness": "incomplete", "reason": "ActivationStale",
                   "restart_clears": True, "note": "restart"}}},
               "recall-by-meaning": {"is_error": False, "payload": {"results": [], "outcome": {
                   "binding": "moved", "completeness": "incomplete", "reason": "ActivationStale",
                   "restart_clears": True, "note": "restart"}}}}))
        self.assertEqual(seen, {"class": "not-reproduced", "label": "caveat-only"})

    def test_keyword_ok_with_meaning_unavailable_is_not_a_reproduction(self):
        seen = classify_outcome(self._observation(
            **{"search": {"is_error": False, "payload": {"results": [{"text": "x"}]}},
               "recall-by-meaning": {"is_error": False, "payload": {"results": [], "unavailable": "not-qualified"}}}))
        self.assertEqual(seen, {"class": "not-reproduced", "label": "semantic-unavailable-keyword-ok"})

    def test_a_generic_error_off_the_recall_mechanism_is_not_a_reproduction(self):
        seen = classify_outcome(self._observation(
            **{"search": {"is_error": False, "payload": {"results": []}},
               "recall-by-meaning": {"is_error": False, "payload": {"results": []}},
               "pin": {"is_error": True, "text": "Error executing tool pin"}}))
        self.assertEqual(seen["class"], "not-reproduced")
        self.assertTrue(seen["label"].startswith("generic-error-without-recall-mechanism"))

    def test_the_probe_is_recorded_with_its_actual_result_in_every_branch(self):
        nonce = "repro-abc"
        self.assertFalse(recall_acceptance_probe({"tools": {}}, nonce)["passed"])
        self.assertFalse(recall_acceptance_probe(
            {"tools": {"recall-by-meaning": {"is_error": False, "payload": {"unavailable": "not-qualified"}}}},
            nonce)["passed"])
        self.assertTrue(recall_acceptance_probe(
            {"tools": {"recall-by-meaning": {"is_error": False, "payload": {"results": [{"passage": f"x {nonce} y"}]}}}},
            nonce)["passed"])
        # The marker may sit outside the matched chunk: a hit whose passage lacks it but whose record text
        # carries it IS a recall, and must never read as "not recalled".
        outside = recall_acceptance_probe(
            {"tools": {"recall-by-meaning": {"is_error": False, "payload": {"results": [
                {"passage": "an unrelated chunk", "text": f"the whole record mentions {nonce} later"}]}}}}, nonce)
        self.assertEqual({"passed": outside["passed"], "reason": outside["reason"]}, {"passed": True, "reason": "recalled"})
        self.assertEqual(recall_acceptance_probe(
            {"tools": {"recall-by-meaning": {"is_error": False, "payload": {"unavailable": "not-qualified"}}}},
            nonce)["reason"], "semantic-unavailable")


class DisposableCloneIsolationTests(unittest.TestCase):
    """The clone is a sandbox, proven against the launched server's OWN resolution — including the hostile
    case a build worktree makes real (a leaked GIT_DIR)."""

    @classmethod
    def setUpClass(cls):
        cls.clone = _shared_clone()

    def test_the_clone_is_accepted_by_the_audited_disposable_target_check_and_is_seeded(self):
        self.assertTrue(os.path.isdir(os.path.join(self.clone.root, ".git")))
        self.assertEqual(self.clone.head, self.clone.commit)          # detached at the resolved commit
        self.assertNotEqual(os.path.realpath(self.clone.root), self.clone.canonical_root)
        self.assertNotEqual(self.clone.resolved_launch_paths(self.clone.env())["git_common_dir"],
                            self.clone.canonical_common)
        self.assertTrue(os.path.exists(os.path.join(self.clone.memory, "ledger.ndjson")))
        self.assertEqual(json.loads(Path(os.path.join(self.clone.root, ".engine", "memory-backup",
                                                      "pointer.json")).read_text())["configured"], False)

    def test_the_launch_env_is_scrubbed_and_pinned(self):
        hostile = dict(os.environ, GIT_DIR=self.clone.canonical_common, GIT_WORK_TREE=self.clone.canonical_root,
                       ENGINE_MEMORY_DIR=self.clone.canonical_memory, UV_INDEX_URL="https://example.invalid")
        env = self.clone.env(hostile)
        self.assertFalse([key for key in env if key.startswith(("GIT_", "ENGINE_", "CODEX_", "UV_INDEX"))], env)
        self.assertEqual(env["CLAUDE_PROJECT_DIR"], self.clone.root)
        self.assertTrue(env["HOME"].startswith(self.clone.sandbox))
        self.assertTrue(env["TMPDIR"].startswith(self.clone.sandbox))
        self.assertEqual(env["UV_OFFLINE"], "1")
        self.assertEqual(env["UV_CACHE_DIR"], self.clone.uv_cache)
        self.assertEqual(env["UV_PYTHON_INSTALL_DIR"], self.clone.uv_python)
        self.assertTrue(os.path.isabs(self.clone.uv_python))
        self.assertIn("PATH", env)

    def test_the_clone_is_built_by_commit_so_a_detached_shallow_checkout_can_build_it_too(self):
        # The merge check's checkout is detached (`--abbrev-ref HEAD` == "HEAD") and shallow; a clone by
        # branch name fails there. Prove the clone form used above works from exactly that shape.
        scratch = tempfile.mkdtemp(prefix="engine-repro-detached-")
        try:
            src = os.path.join(scratch, "src")
            _run(["git", "init", "-q", src])
            Path(os.path.join(src, "f")).write_text("1\n")
            git = ["git", "-C", src, "-c", "user.email=t@t", "-c", "user.name=t"]
            _run(git + ["add", "f"])
            _run(git + ["commit", "-qm", "one"])
            sha = _run(["git", "-C", src, "rev-parse", "HEAD"])
            ci = os.path.join(scratch, "ci")
            _run(["git", "init", "-q", ci])
            _run(["git", "-C", ci, "fetch", "-q", "--depth=1", src, "+HEAD:refs/remotes/pull/1/merge"])
            _run(["git", "-C", ci, "checkout", "-q", "--force", "refs/remotes/pull/1/merge"])
            self.assertEqual(_run(["git", "-C", ci, "rev-parse", "--abbrev-ref", "HEAD"]), "HEAD")
            dst = os.path.join(scratch, "dst")
            _run(["git", "clone", "--no-hardlinks", "--quiet", "--no-checkout", ci, dst])
            _run(["git", "-C", dst, "checkout", "--quiet", "--detach", sha])
            self.assertEqual(_run(["git", "-C", dst, "rev-parse", "HEAD"]), sha)
            self.assertTrue(os.path.exists(os.path.join(dst, "f")))
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_the_harness_scrubs_git_variables_from_its_own_commands_by_default(self):
        # The harness's own git commands — including the writes in the detached-checkout test above — must
        # never inherit a leaked GIT_DIR, or they would land in the repository the sandbox exists to protect.
        with mock.patch.dict(os.environ, {"GIT_DIR": "/nonexistent/leaked", "GIT_WORK_TREE": "/nonexistent"}):
            self.assertEqual(_run(["sh", "-c", 'echo "${GIT_DIR:-unset} ${GIT_WORK_TREE:-unset}"']), "unset unset")
            self.assertEqual(_run(["sh", "-c", 'echo "${GIT_DIR:-unset}"'], env=dict(os.environ)),
                             "/nonexistent/leaked")   # only an explicitly named environment inherits it

    def test_a_clone_that_fails_to_build_leaves_no_sandbox_behind(self):
        # Measured as a DELTA over the temp directory: what this failed construction leaves behind, not
        # whatever earlier runs (the very leak this guards against) may have stranded there already.
        def sandboxes():
            return {name for name in os.listdir(tempfile.gettempdir()) if name.startswith("engine-repro-clone-")}

        before = sandboxes()
        made = []
        real_mkdtemp = tempfile.mkdtemp

        def spying_mkdtemp(*args, **kwargs):
            path = real_mkdtemp(*args, **kwargs)
            made.append(path)
            return path

        with mock.patch.object(tempfile, "mkdtemp", side_effect=spying_mkdtemp), \
             mock.patch.object(DisposableClone, "_build", side_effect=OSError("clone step failed")):
            with self.assertRaises(OSError):
                DisposableClone()
        self.assertEqual(len(made), 1)                       # the sandbox WAS created ...
        self.assertFalse(os.path.exists(made[0]))            # ... and removed on the way out
        self.assertEqual(sandboxes() - before, set())

    def test_a_leaked_git_dir_would_bind_canonical_and_the_scrub_prevents_it(self):
        hostile = dict(os.environ, GIT_DIR=self.clone.canonical_common)
        # The hazard is real: under the leaked variable the clone resolves the CANONICAL git metadata and
        # main checkout — exactly what the degraded launch would then bind memory to.
        leaked = self.clone.resolved_launch_paths(hostile)
        self.assertEqual(leaked["git_common_dir"], self.clone.canonical_common)
        self.assertEqual(leaked["project_root"], self.clone.canonical_root)
        self.assertFalse(any(self.clone.containment(leaked).values()))
        # Under the scrubbed environment every resolved write destination is inside the sandbox.
        resolved = self.clone.resolved_launch_paths(self.clone.env(hostile))
        self.assertEqual(resolved["project_root"], os.path.realpath(self.clone.root))
        self.assertEqual(resolved["memory_dir"], os.path.realpath(self.clone.memory))
        self.assertTrue(all(self.clone.containment(resolved).values()), resolved)
        self.assertFalse(os.path.exists(resolved["activation"]))   # no activation: the degraded launch is forced


class DisposableCloneReproductionTests(unittest.IsolatedAsyncioTestCase):
    """The best-effort reproduction itself, on both shipped launchers, labeled and classified — the
    committed DISPOSITION is checked against what actually happened."""

    @classmethod
    def setUpClass(cls):
        cls.clone = _shared_clone()

    async def test_both_shipped_launchers_run_degraded_and_the_disposition_matches_reality(self):
        report = {"clone_head": self.clone.head, "sandbox": self.clone.sandbox, "launchers": {}}
        for kind in ("claude", "codex"):
            with self.subTest(launcher=kind):
                observation = await asyncio.wait_for(drive_launcher(self.clone, kind), timeout=300)
                outcome = classify_outcome(observation)
                probe = recall_acceptance_probe(observation, self.clone.nonce)
                report["launchers"][kind] = {"observation": observation, "outcome": outcome, "probe": probe}
                # The launcher the server ACTUALLY ran, in its own words — the mechanism-removing difference.
                self.assertEqual(observation["qualification_reported"], "degraded", observation["stderr"])
                self.assertIn("running unqualified", observation["stderr"])
                self.assertIs(observation["diagnostics_armed"], True)
                self.assertFalse(observation["tools"]["health"]["is_error"])
                self.assertFalse(observation["tools"]["search"]["is_error"], observation["tools"]["search"]["text"])
                hits = observation["tools"]["search"]["payload"]["results"]
                self.assertTrue(any(self.clone.nonce in hit["text"] for hit in hits), hits)   # keyword recall reaches the seed
                self.assertIn("recall-window", observation["tools"])
                # The committed finding equals the observed class and label — or this test fails loudly.
                self.assertEqual(outcome["class"], DISPOSITION["expected_class"][kind], outcome)
                self.assertEqual(outcome["label"], DISPOSITION["expected_label"][kind], outcome)
                # The probe is preserved with its actual result AND that result is pinned — passed and the
                # closed-set reason — so the third outcome (not reproduced, probe failed for a recorded,
                # unrelated reason) can neither drift silently nor be mistaken for "recall is broken".
                self.assertEqual({"passed": probe["passed"], "reason": probe["reason"]},
                                 DISPOSITION["expected_probe"][kind], probe)
        # Containment, stated as what actually happened: a DEGRADED launch refuses the derived-index rebuild
        # (index-rebuild is not degraded-allowed; keyword recall falls through to a ledger scan), so the reads
        # wrote NOTHING — the sandbox's memory directory holds exactly what this harness seeded, and no
        # derived cache exists to have landed anywhere. That is itself a recorded mechanism-removing
        # difference from the attended launch, which reconciles its caches on the way past.
        self.assertEqual(sorted(os.listdir(self.clone.memory)), ["ledger.ndjson"])
        report["derived_cache_written"] = False
        report["disposition"] = DISPOSITION
        report["ts"] = time.time()
        report["complete"] = set(report["launchers"]) == {"claude", "codex"}
        summary = {kind: {"outcome": value["outcome"], "probe": value["probe"],
                          "qualification": value["observation"]["qualification_reported"]}
                   for kind, value in report["launchers"].items()}
        summary["probe_disposition"] = DISPOSITION["probe_disposition"]
        summary["complete"] = report["complete"]
        # The report outlives the run: the sandbox is destroyed with the raw server output it holds, so the
        # evidence goes to the source checkout's own gitignored engine cache — a durable, local-only home the
        # printed path actually still names when the run is over. It carries its timestamp and whether every
        # launcher was observed, so a leftover or a partial file cannot pass for a fresh, complete one; and a
        # checkout that cannot be written degrades to the inline summary rather than failing the test here,
        # after every real assertion has already run.
        report_path = os.path.join(self.clone.source, ".engine", "telemetry", ".cache", "reproduction-report.json")
        try:
            os.makedirs(os.path.dirname(report_path), exist_ok=True)
            Path(report_path).write_text(json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8")
            where = f"reproduction report ({'complete' if report['complete'] else 'PARTIAL'}, gitignored): {report_path}"
        except OSError as exc:
            where = f"reproduction report not persisted ({type(exc).__name__}); inline summary only"
        print(f"\n{where}\n" + json.dumps(summary, indent=2))


if __name__ == "__main__":
    unittest.main()
