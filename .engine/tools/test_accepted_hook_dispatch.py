#!/usr/bin/env python3
"""Incident-shaped tests for exact accepted-code automatic-hook dispatch (issue #1151)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import jsonschema


TOOLS = Path(__file__).resolve().parent
DISPATCH = TOOLS / "accepted_hook_dispatch.py"
RELEASE_SOURCE = TOOLS / "release_source.py"
HOOK_RUNNER = TOOLS / "hook-runner.sh"
CODEX_RUNNER = TOOLS / "codex-hook-runner.sh"
SCHEMA = TOOLS.parent / "schemas" / "accepted-hook-activation.v1.json"


def _call(*args: str, cwd: str | None = None, env: dict | None = None, check: bool = True):
    proc = subprocess.run(
        list(args), cwd=cwd, env=env, capture_output=True, text=True, timeout=30,
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"command failed ({proc.returncode}): {args!r}\n{proc.stdout}\n{proc.stderr}")
    return proc


class AcceptedRepo:
    """A real Git repo whose accepted tree is later dirtied in a linked worktree."""

    def __init__(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "main"
        self.worktree = Path(self.temp.name) / "candidate"
        self.poison = Path(self.temp.name) / "poison"
        self.marker = Path(self.temp.name) / "startup-ran"
        self.root.mkdir()
        _call("git", "init", "-b", "main", str(self.root))
        _call("git", "-C", str(self.root), "config", "user.email", "fixture@example.test")
        _call("git", "-C", str(self.root), "config", "user.name", "Fixture")
        _call("git", "-C", str(self.root), "remote", "add", "origin", "https://github.com/owner/project.git")
        self._write_accepted_tree()
        _call("git", "-C", str(self.root), "add", ".")
        _call("git", "-C", str(self.root), "commit", "-m", "accepted")
        self.commit = _call("git", "-C", str(self.root), "rev-parse", "HEAD").stdout.strip()
        self.tree = _call("git", "-C", str(self.root), "rev-parse", "HEAD^{tree}").stdout.strip()
        _call("git", "-C", str(self.root), "worktree", "add", "-b", "candidate", str(self.worktree), self.commit)
        self.dispatcher = self.worktree / ".engine" / "tools" / "accepted_hook_dispatch.py"
        self.script = self.worktree / ".engine" / "tools" / "close.py"

    def cleanup(self):
        self.temp.cleanup()

    def _put(self, rel: str, text: str):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _write_accepted_tree(self):
        self._put(".engine/tools/accepted_hook_dispatch.py", DISPATCH.read_text(encoding="utf-8"))
        self._put(".engine/tools/release_source.py", RELEASE_SOURCE.read_text(encoding="utf-8"))
        self._put(
            ".engine/tools/validate.py",
            "from pathlib import Path\nROOT = str(Path(__file__).resolve().parents[2])\n",
        )
        self._put(
            ".engine/tools/helper.py",
            "from pathlib import Path\nVALUE = 'accepted'\nORIGIN = str(Path(__file__).resolve())\n",
        )
        self._put(
            ".engine/tools/close.py",
            textwrap.dedent(
                """\
                import json, os
                import helper, validate
                context = json.loads(os.environ["ENGINE_ACCEPTED_HOOK_CONTEXT"])
                print(json.dumps({
                    "value": helper.VALUE,
                    "helper_origin": helper.ORIGIN,
                    "validate_origin": validate.__file__,
                    "root": validate.ROOT,
                    "memory_dir": os.environ.get("ENGINE_MEMORY_DIR"),
                    "provider": os.environ.get("ENGINE_PROVIDER"),
                    "context": context,
                }, sort_keys=True))
                """
            ),
        )
        self._put(".engine/tools/boot.py", "raise SystemExit(0)\n")
        self._put(".engine/tools/memory/__init__.py", "")
        for name in ("compact.py", "erasure_observer.py", "backup_vault.py"):
            self._put(f".engine/tools/memory/{name}", "raise SystemExit(0)\n")
        self._put(".engine/tools/hook-runner.sh", HOOK_RUNNER.read_text(encoding="utf-8"))
        self._put(".engine/tools/codex-hook-runner.sh", CODEX_RUNNER.read_text(encoding="utf-8"))
        self._put(
            ".engine/engine.json",
            json.dumps({"engine_version": "9.9.9", "default_branch": "main"}) + "\n",
        )
        self._put(
            ".engine/memory-backup/pointer.json",
            json.dumps({
                "schema_version": 1, "owner": "vault-owner", "repo": "vault", "branch": "main",
                "namespace": "project-id",
            }) + "\n",
        )

    def activate(self, *, source: str = "reviewed-merge", source_ref: str = "refs/heads/main",
                 expected_epoch: int = 0, commit: str | None = None, root: Path | None = None):
        return _call(
            sys.executable, str((root or self.worktree) / ".engine/tools/accepted_hook_dispatch.py"),
            "activate", "--root", str(root or self.worktree), "--repository", "owner/project",
            "--commit", commit or self.commit, "--source", source, "--source-ref", source_ref,
            "--engine-release", "9.9.9", "--expected-epoch", str(expected_epoch), check=False,
        )

    def dirty_candidate(self):
        (self.worktree / ".engine/tools/helper.py").write_text(
            "from pathlib import Path\nVALUE = 'candidate'\nORIGIN = str(Path(__file__).resolve())\n",
            encoding="utf-8",
        )
        self.script.write_text(
            f"from pathlib import Path\nPath({str(self.marker)!r}).write_text('candidate-ran')\n",
            encoding="utf-8",
        )

    def poison_environment(self) -> dict:
        self.poison.mkdir(exist_ok=True)
        (self.poison / "helper.py").write_text(
            "VALUE = 'environment'\nORIGIN = __file__\n", encoding="utf-8",
        )
        for name in ("sitecustomize.py", "usercustomize.py", "startup.py"):
            (self.poison / name).write_text(
                f"from pathlib import Path\nPath({str(self.marker)!r}).write_text({name!r})\n",
                encoding="utf-8",
            )
        return {
            **os.environ,
            "PYTHONPATH": str(self.poison),
            "PYTHONUSERBASE": str(self.poison),
            "PYTHONSTARTUP": str(self.poison / "startup.py"),
            "ENGINE_MEMORY_DIR": str(self.poison / "memory"),
            "ENGINE_PROVIDER": "codex",
        }

    def run_close(self, *, env: dict | None = None):
        return _call(
            sys.executable, "-I", "-S", str(self.dispatcher), "run", "--root", str(self.worktree),
            "--script", str(self.script), "--", env=env, check=False,
        )

    def provision_interpreter(self):
        bindir = self.worktree / ".engine/.venv/bin"
        bindir.mkdir(parents=True, exist_ok=True)
        python = bindir / "python"
        if not python.exists():
            python.symlink_to(sys.executable)

    def run_claude_launcher(self, *, env: dict | None = None):
        self.provision_interpreter()
        clean_env = dict(env or os.environ)
        clean_env.pop("ENGINE_PROVIDER", None)
        return _call(
            "sh", str(self.worktree / ".engine/tools/hook-runner.sh"),
            str(self.worktree / ".engine/.venv/bin/python"), str(self.script),
            cwd=str(self.worktree), env=clean_env, check=False,
        )

    def run_codex_launcher(self, *, env: dict | None = None):
        self.provision_interpreter()
        return _call(
            "sh", ".engine/tools/codex-hook-runner.sh", ".engine/tools/close.py",
            cwd=str(self.worktree), env=env, check=False,
        )

    def common_dir(self) -> Path:
        raw = _call("git", "-C", str(self.root), "rev-parse", "--git-common-dir").stdout.strip()
        return Path(raw) if os.path.isabs(raw) else self.root / raw


class TestAcceptedActivationSchema(unittest.TestCase):
    def test_schema_is_well_formed_and_closed(self):
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            set(schema["required"]),
            {"schema_version", "repository", "commit", "tree", "engine_release", "epoch",
             "source", "source_ref", "activated_at"},
        )


class TestAcceptedHookDispatch(unittest.TestCase):
    def setUp(self):
        self.repo = AcceptedRepo()

    def tearDown(self):
        self.repo.cleanup()

    def test_reviewed_merge_activation_is_exact_common_and_compare_and_set(self):
        activated = self.repo.activate()
        self.assertEqual(activated.returncode, 0, activated.stderr)
        record = json.loads(activated.stdout)
        self.assertEqual(record["commit"], self.repo.commit)
        self.assertEqual(record["tree"], self.repo.tree)
        self.assertEqual(record["repository"], "owner/project")
        self.assertEqual(record["engine_release"], "9.9.9")
        self.assertEqual(record["epoch"], 1)
        common = self.repo.common_dir()
        activation_path = common / "engine/accepted-hooks/activation.json"
        self.assertEqual(json.loads(activation_path.read_text(encoding="utf-8")), record)

        stale = self.repo.activate(expected_epoch=0)
        self.assertEqual(stale.returncode, 1)
        self.assertNotEqual(stale.returncode, 2)
        self.assertIn("compare-and-set failed", stale.stderr)
        self.assertEqual(json.loads(activation_path.read_text(encoding="utf-8")), record)

    def test_activation_refuses_non_default_branch_and_pre_fix_worktree(self):
        wrong_ref = self.repo.activate(source_ref="refs/heads/candidate")
        self.assertEqual(wrong_ref.returncode, 1)
        self.assertIn("recorded default branch", wrong_ref.stderr)
        runner = self.repo.worktree / ".engine/tools/hook-runner.sh"
        runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        legacy = self.repo.activate()
        self.assertEqual(legacy.returncode, 1)
        self.assertIn("retire or recreate", legacy.stderr)

    def test_published_release_tag_is_resolved_once_to_exact_objects(self):
        _call("git", "-C", str(self.repo.root), "tag", "v9.9.9", self.repo.commit)
        activated = self.repo.activate(source="published-release", source_ref="v9.9.9")
        self.assertEqual(activated.returncode, 0, activated.stderr)
        self.repo.dirty_candidate()
        (self.repo.root / "later.txt").write_text("later\n", encoding="utf-8")
        _call("git", "-C", str(self.repo.root), "add", "later.txt")
        _call("git", "-C", str(self.repo.root), "commit", "-m", "later")
        later = _call("git", "-C", str(self.repo.root), "rev-parse", "HEAD").stdout.strip()
        _call("git", "-C", str(self.repo.root), "tag", "-f", "v9.9.9", later)

        run = self.repo.run_close(env=self.repo.poison_environment())
        self.assertEqual(run.returncode, 0, run.stderr)
        receipt = json.loads(run.stdout)
        self.assertEqual(receipt["context"]["activation"]["commit"], self.repo.commit)
        self.assertEqual(receipt["value"], "accepted")

    def test_dirty_linked_worktree_and_poisoned_python_run_only_accepted_modules(self):
        activated = self.repo.activate()
        self.assertEqual(activated.returncode, 0, activated.stderr)
        self.repo.dirty_candidate()
        run = self.repo.run_close(env=self.repo.poison_environment())
        self.assertEqual(run.returncode, 0, run.stderr)
        receipt = json.loads(run.stdout)
        accepted_cache = str(Path(receipt["helper_origin"]).parents[3])
        self.assertEqual(receipt["value"], "accepted")
        self.assertIn("accepted-hooks/trees", receipt["helper_origin"])
        self.assertTrue(receipt["helper_origin"].startswith(accepted_cache))
        self.assertTrue(receipt["validate_origin"].startswith(accepted_cache))
        self.assertEqual(receipt["root"], str(self.repo.root.resolve()))
        self.assertEqual(receipt["memory_dir"], str((self.repo.root / ".engine/memory").resolve()))
        self.assertEqual(receipt["provider"], "codex")
        self.assertEqual(receipt["context"]["activation"]["commit"], self.repo.commit)
        self.assertEqual(
            receipt["context"]["canonical"]["backup_pointer_identity"]["namespace"], "project-id",
        )
        self.assertFalse(self.repo.marker.exists(), "candidate/startup customization must never execute")

    def test_real_claude_and_codex_launchers_preserve_provider_and_closed_origins(self):
        activated = self.repo.activate()
        self.assertEqual(activated.returncode, 0, activated.stderr)
        self.repo.dirty_candidate()
        poison = self.repo.poison_environment()
        for provider, run in (
            ("claude", self.repo.run_claude_launcher),
            ("codex", self.repo.run_codex_launcher),
        ):
            with self.subTest(provider=provider):
                self.repo.marker.unlink(missing_ok=True)
                result = run(env=poison)
                self.assertEqual(result.returncode, 0, result.stderr)
                receipt = json.loads(result.stdout)
                self.assertEqual(receipt["provider"], provider)
                self.assertEqual(receipt["value"], "accepted")
                self.assertIn("accepted-hooks/trees", receipt["helper_origin"])
                self.assertEqual(receipt["root"], str(self.repo.root.resolve()))
                self.assertFalse(self.repo.marker.exists())

    def test_missing_or_corrupt_authority_refuses_without_candidate_fallback_and_never_exits_two(self):
        self.repo.dirty_candidate()
        missing = self.repo.run_close(env=self.repo.poison_environment())
        self.assertEqual(missing.returncode, 1)
        self.assertNotEqual(missing.returncode, 2)
        self.assertIn("mutation skipped", missing.stderr)
        self.assertFalse(self.repo.marker.exists())

        common = self.repo.common_dir()
        path = common / "engine/accepted-hooks/activation.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not-json", encoding="utf-8")
        corrupt = self.repo.run_close(env=self.repo.poison_environment())
        self.assertEqual(corrupt.returncode, 1)
        self.assertNotEqual(corrupt.returncode, 2)
        self.assertFalse(self.repo.marker.exists())

    def test_canonical_pointer_drift_after_activation_refuses_before_target_import(self):
        activated = self.repo.activate()
        self.assertEqual(activated.returncode, 0, activated.stderr)
        self.repo.dirty_candidate()
        (self.repo.root / ".engine/memory-backup/pointer.json").write_text(
            json.dumps({
                "schema_version": 1, "owner": "changed", "repo": "vault", "branch": "main",
                "namespace": "other-project-id",
            }) + "\n",
            encoding="utf-8",
        )
        refused = self.repo.run_close(env=self.repo.poison_environment())
        self.assertEqual(refused.returncode, 1)
        self.assertNotEqual(refused.returncode, 2)
        self.assertIn("canonical backup pointer differs", refused.stderr)
        self.assertFalse(self.repo.marker.exists())

    def test_accepted_target_exit_two_is_preserved_but_qualification_exit_is_not_two(self):
        (self.repo.root / ".engine/tools/close.py").write_text("raise SystemExit(2)\n", encoding="utf-8")
        _call("git", "-C", str(self.repo.root), "add", ".engine/tools/close.py")
        _call("git", "-C", str(self.repo.root), "commit", "-m", "accepted block")
        commit = _call("git", "-C", str(self.repo.root), "rev-parse", "HEAD").stdout.strip()
        # Recreate the linked worktree so its launcher is at the newly reviewed accepted commit.
        _call("git", "-C", str(self.repo.root), "worktree", "remove", "--force", str(self.repo.worktree))
        _call("git", "-C", str(self.repo.root), "branch", "-D", "candidate")
        _call("git", "-C", str(self.repo.root), "worktree", "add", "-b", "candidate", str(self.repo.worktree), commit)
        self.repo.dispatcher = self.repo.worktree / ".engine/tools/accepted_hook_dispatch.py"
        self.repo.script = self.repo.worktree / ".engine/tools/close.py"
        activated = self.repo.activate(commit=commit)
        self.assertEqual(activated.returncode, 0, activated.stderr)
        run = self.repo.run_close()
        self.assertEqual(run.returncode, 2)


if __name__ == "__main__":
    unittest.main()
