#!/usr/bin/env python3
"""Operator-runnable proof that manual grounding needs no home-directory write access."""
from __future__ import annotations

import os
import subprocess
import tempfile


TOOLS = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.dirname(TOOLS)
ROOT = os.path.dirname(ENGINE)
EXPECTED_CACHE = os.path.realpath(os.path.join(ENGINE, ".uv"))


def _run(args: list[str], *, env: dict[str, str], cwd: str = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=90)


def main() -> int:
    before = subprocess.run(
        ["git", "status", "--short", "--untracked-files=no"], cwd=ROOT,
        check=True, capture_output=True, text=True, timeout=30).stdout

    with tempfile.TemporaryDirectory() as tmp:
        blocked_home = os.path.join(tmp, "home-is-a-file")
        with open(blocked_home, "w", encoding="utf-8") as fh:
            fh.write("uv cannot create its normal home cache below this file\n")

        env = dict(os.environ)
        env["HOME"] = blocked_home
        env["UV_PYTHON_DOWNLOADS"] = "never"
        env.pop("UV_CACHE_DIR", None)
        env.pop("UV_NO_CACHE", None)

        cache = _run(["uv", "--directory", ENGINE, "cache", "dir"], env=env)
        if cache.returncode != 0:
            print(cache.stderr, end="")
            return cache.returncode
        resolved = os.path.realpath(os.path.join(ENGINE, cache.stdout.strip()))
        if resolved != EXPECTED_CACHE:
            print(f"FAIL: uv resolved {resolved}; expected {EXPECTED_CACHE}")
            return 1

        status = _run(
            ["uv", "run", "--directory", ENGINE, "--frozen", "--", "python",
             "tools/engine_status.py"], env=env)
        if status.returncode != 0:
            print(status.stderr, end="")
            return status.returncode
        print(status.stdout, end="")

    after = subprocess.run(
        ["git", "status", "--short", "--untracked-files=no"], cwd=ROOT,
        check=True, capture_output=True, text=True, timeout=30).stdout
    if before != after:
        print("FAIL: tracked worktree status changed during the proof")
        return 1

    print(f"PASS: uv used Engine-owned cache {EXPECTED_CACHE}")
    print("PASS: the real manual-grounding command ran with an unusable HOME")
    print("PASS: tracked worktree status was unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
