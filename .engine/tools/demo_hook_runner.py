#!/usr/bin/env python3
"""Behavioral demo for the hook launcher's per-OS interpreter resolution (issue #407 / U03). The committed
hook commands always name the POSIX interpreter (`.engine/.venv/bin/python`); this shows the REAL committed
`hook-runner.sh` resolving the interpreter that actually exists on the machine at fire time — so one
committed repo boots on every OS, including a mixed-OS team, instead of a Windows adopter's hooks (boot
included) silently failing open to a floor-only engine.

Nothing is faked: it runs the real `hook-runner.sh` under a real `sh`, against real stub interpreters on
disk. It can fail — every case asserts, and the self-check at the end is the falsification (a regression
that broke the resolution, the POSIX-preference, or the never-fall-back-to-system-Python law returns 1).

What it shows:
  (1) POSIX layout present  -> the launcher runs the named bin/python (byte-for-byte the prior behavior);
  (2) ONLY the Windows layout present (bin/python absent) -> the launcher resolves and runs the
      Scripts/python.exe sibling under the same venv root (the #407 fix, on a POSIX host via a stub);
  (3) NEITHER layout present -> the launcher runs NOTHING, names the absent runtime, and exits
      NON-blocking (never the platform's block code 2 — the #390 fail-closed stranding it must not cause).

Run:
  uv run --directory .engine --frozen -- python tools/demo_hook_runner.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hooks  # noqa: E402  (the resolver whose two per-OS forms the launcher mirrors)

WRAPPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hook-runner.sh")
_FAST = {"ENGINE_HOOK_WAIT_POLLS": "3", "ENGINE_HOOK_WAIT_INTERVAL": "0.05"}   # ~0.15 s bound, keep it snappy


def _stub(path: str, tag: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(f'#!/bin/sh\necho "{tag} $@"\n')
    os.chmod(path, 0o755)   # executable so `exec` succeeds when this layout is the one chosen


def _run(td: str, *, posix: bool, windows: bool):
    """Lay down the requested venv layout(s), fire the REAL launcher with the POSIX interpreter named
    (as every committed command does), and return the completed process."""
    venv = os.path.join(td, ".engine", ".venv")
    named = os.path.join(venv, "bin", "python")                 # what the committed command names ($1)
    if posix:
        _stub(named, "POSIX-RAN")
    if windows:
        _stub(os.path.join(venv, "Scripts", "python.exe"), "WINDOWS-RAN")
    script = os.path.join(td, ".engine", "tools", "boot.py")
    return subprocess.run(["sh", WRAPPER, named, script, "hook"],
                          capture_output=True, text=True, timeout=10, env={**os.environ, **_FAST})


def demo() -> int:
    print("hook-runner.sh per-OS interpreter resolution (#407) — driving the REAL committed launcher\n")
    print(f"  the resolver's two layouts (single source of truth): "
          f"{hooks.interpreter_path('posix')}  |  {hooks.interpreter_path('nt')}\n")
    ok = True

    with tempfile.TemporaryDirectory() as td:
        r = _run(td, posix=True, windows=False)
        ran = "POSIX-RAN" in r.stdout and "WINDOWS-RAN" not in r.stdout
        print(f"  (1) Mac/Linux layout present        -> {'ran bin/python' if ran else 'MISRESOLVED'} "
              f"(exit {r.returncode})")
        ok = ok and ran and r.returncode == 0

    with tempfile.TemporaryDirectory() as td:
        r = _run(td, posix=False, windows=True)
        ran = "WINDOWS-RAN" in r.stdout
        print(f"  (2) only Windows layout present     -> {'resolved + ran Scripts/python.exe' if ran else 'FAILED OPEN (regression)'} "
              f"(exit {r.returncode})   <- the #407 fix")
        ok = ok and ran and r.returncode == 0

    with tempfile.TemporaryDirectory() as td:
        r = _run(td, posix=False, windows=False)
        safe = r.stdout == "" and r.returncode != 0 and r.returncode != 2 and "not a block" in r.stderr
        print(f"  (3) neither layout present          -> {'ran nothing, named it, non-blocking' if safe else 'UNSAFE (regression)'} "
              f"(exit {r.returncode}, never 2)")
        ok = ok and safe

    print("\nself-check:", "PASS" if ok else "FAIL — the launcher's per-OS resolution regressed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(demo())
