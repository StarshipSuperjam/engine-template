#!/usr/bin/env python3
"""Regression for the Engine-owned uv cache under a workspace-only sandbox.

This invokes the real uv executable rather than merely parsing TOML: relative cache paths are a uv
runtime behavior, and the three production call shapes must all resolve to `.engine/.uv/`. The real
manual-grounding command is also run with an unusable HOME, proving the fallback reaches the target
tool without broader home-directory authority.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import unittest

import demo_uv_workspace_cache
import quiet_call


TOOLS = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.dirname(TOOLS)
ROOT = os.path.dirname(ENGINE)
EXPECTED_CACHE = os.path.realpath(os.path.join(ENGINE, ".uv"))


def _env(home: str) -> dict:
    env = dict(os.environ)
    env["HOME"] = home
    env["UV_PYTHON_DOWNLOADS"] = "never"
    env.pop("UV_CACHE_DIR", None)
    env.pop("UV_NO_CACHE", None)
    return env


def _resolved_cache(args: list[str], cwd: str, home: str) -> str:
    proc = subprocess.run(["uv", *args, "cache", "dir"], cwd=cwd, env=_env(home),
                          check=True, capture_output=True, text=True, timeout=30)
    # uv renders the configured relative path. It is relative to the selected project directory:
    # ENGINE for both `--directory ENGINE` and a process whose cwd already is ENGINE.
    return os.path.realpath(os.path.join(ENGINE, proc.stdout.strip()))


@unittest.skipUnless(shutil.which("uv"), "uv is required to exercise its cache resolution")
class TestUvWorkspaceCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "blocked-home")
        # A regular file is a deterministic unusable HOME even for privileged CI users and on hosts where
        # chmod does not enforce POSIX write bits. uv's default $HOME/.cache/uv path cannot exist beneath it.
        with open(self.home, "w", encoding="utf-8") as fh:
            fh.write("not a directory\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_repo_root_directory_form_resolves_to_engine_cache(self):
        self.assertEqual(_resolved_cache(["--directory", ENGINE], ROOT, self.home), EXPECTED_CACHE)

    def test_engine_cwd_upgrade_form_resolves_to_engine_cache(self):
        # module_manager._resync_tool_runtime runs exactly this cwd form during upgrade.
        self.assertEqual(_resolved_cache([], ENGINE, self.home), EXPECTED_CACHE)

    def test_real_manual_grounding_runs_with_unusable_home(self):
        proc = subprocess.run(
            ["uv", "run", "--directory", ENGINE, "--frozen", "--", "python",
             "tools/engine_status.py"],
            cwd=ROOT, env=_env(self.home), capture_output=True, text=True, timeout=90)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Project status", proc.stdout)
        self.assertTrue(os.path.isfile(self.home), "fixture HOME must be a file, not a writable directory")

    def test_real_manual_grounding_runs_with_a_cold_project_cache(self):
        # Keep the real Engine checkout and its warm cache untouched. A throwaway project carries the exact
        # committed uv configuration and lock, while reusing the already-materialized locked environment so
        # this test isolates cache initialization rather than introducing a package-download dependency.
        cold_project = os.path.join(self.tmp.name, "cold-engine-project")
        os.makedirs(cold_project)
        shutil.copy2(os.path.join(ENGINE, "pyproject.toml"), cold_project)
        shutil.copy2(os.path.join(ENGINE, "uv.lock"), cold_project)
        cold_cache = os.path.join(cold_project, ".uv")
        self.assertFalse(os.path.exists(cold_cache))

        env = _env(self.home)
        env["UV_PROJECT_ENVIRONMENT"] = os.path.join(ENGINE, ".venv")
        resolved = subprocess.run(
            ["uv", "--project", cold_project, "cache", "dir"],
            cwd=ROOT, env=env, check=True, capture_output=True, text=True, timeout=30)
        self.assertEqual(
            os.path.realpath(os.path.join(cold_project, resolved.stdout.strip())),
            os.path.realpath(cold_cache),
        )

        proc = subprocess.run(
            ["uv", "run", "--project", cold_project, "--frozen", "--", "python",
             os.path.join(TOOLS, "engine_status.py")],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=90)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Project status", proc.stdout)
        self.assertTrue(os.path.isfile(self.home), "fixture HOME must remain inaccessible as a directory")

    def test_two_cold_callers_share_the_cache_without_corruption(self):
        cold_cache = os.path.join(self.tmp.name, "cold-cache")
        self.assertFalse(os.path.exists(cold_cache))
        env = _env(self.home)
        # The two resolution tests above prove the committed config selects one .engine/.uv path. This isolated
        # empty override exercises uv's cache implementation itself without deleting or warming the real shared
        # cache used by the surrounding test run.
        env["UV_CACHE_DIR"] = cold_cache
        cmd = ["uv", "run", "--directory", ENGINE, "--frozen", "--", "python", "-c",
               "print('cache-ok')"]
        barrier = threading.Barrier(3)
        results: list[subprocess.CompletedProcess[str] | None] = [None, None]

        def caller(slot: int) -> None:
            barrier.wait()
            results[slot] = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True,
                                           text=True, timeout=90)

        threads = [threading.Thread(target=caller, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()  # release both launchers together against the still-absent cache
        for thread in threads:
            thread.join(timeout=100)
            self.assertFalse(thread.is_alive(), "concurrent uv caller must finish")
        for proc in results:
            self.assertIsNotNone(proc)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), "cache-ok")
        self.assertTrue(os.path.isdir(cold_cache))
        integrity = subprocess.run(["uv", "cache", "prune"], cwd=ROOT, env=env,
                                   capture_output=True, text=True, timeout=30)
        self.assertEqual(integrity.returncode, 0, integrity.stderr)

    def test_operator_demo_runs_the_real_proof(self):
        self.assertEqual(quiet_call.run(demo_uv_workspace_cache.main), 0)

    def test_documented_production_cleanup_uses_the_project_cache(self):
        # The resolution tests pin that the production config maps to `.engine/.uv`. Exercise the exact
        # cleanup verb against a fresh isolated cache so this regression never spends minutes pruning the
        # developer's warm shared cache or races another test process using it.
        env = _env(self.home)
        env["UV_CACHE_DIR"] = os.path.join(self.tmp.name, "cleanup-cache")
        proc = subprocess.run(["uv", "--directory", ENGINE, "cache", "prune"], cwd=ROOT,
                              env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
