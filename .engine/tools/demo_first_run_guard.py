#!/usr/bin/env python3
"""Operator-runnable demo: the one-time setup CLEANUP step (retire) refuses to delete anything that is not the
engine's own — so a mistake in the cleanup list can never remove YOUR files or folders.

Run: uv run --directory .engine -- python tools/demo_first_run_guard.py

The engine's first-run cleanup removes the setup-only files after the engine is installed. This demo proves
the safety guard on that step against throwaway practice projects — it runs the REAL cleanup logic
(`instantiator.retire`) under a redirected root, so writes land only in a temp folder and your real project is
never touched. Read the two blocks by eye:
  [1] a dangerous FOLDER entry (a plain, non-engine folder) is added to the cleanup list -> the guard refuses
      and your stand-in folder SURVIVES,
  [2] a dangerous FILE entry (a plain, non-engine file) is added -> the guard refuses and your file SURVIVES.
Your proof is that after the cleanup ran, your own folder/file is still there AND the engine reports it
refused for the specific reason 'unsafe-retire-target' — not that a delete "happened not to run", but that the
guard actively stopped it. Revert the guard and this demo deletes the stand-ins and fails (the falsification)."""
from __future__ import annotations
import contextlib
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import instantiator as inst  # noqa: E402


def _plant_file(p: str) -> None:
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("the operator's own file\n")


def _coherent_fixture(d: str) -> None:
    """Build a fully-installed, consistent practice engine so retire() runs its full path (if the guard is
    ever reverted, retire proceeds to the real delete and this demo's stand-ins are actually removed — that is
    what keeps the demo falsifiable). Quiet: the build's own narration is not part of this demo."""
    with contextlib.redirect_stdout(io.StringIO()):
        inst._build_fixture(d)
        inst._plant_first_run_assets(d)
        inst.confirm([], "solo", engine_release="1.0.0", handle="octocat")
        inst._finish_apply(d)


def _refuses_dangerous_entry(kind: str, rel: str, make, label: str) -> bool:
    """Add a dangerous non-engine `rel` to the cleanup set (`kind` is 'dir' or 'file'), plant the operator's
    own stand-in there, run the REAL retire(), and confirm the guard refused and the stand-in survives."""
    with tempfile.TemporaryDirectory() as d:
        with inst._redirect_root(d):
            _coherent_fixture(d)
            victim = os.path.join(d, rel)
            make(victim)                                    # the operator's OWN path, planted after the build
            orig_dirs, orig_files = inst._FIRST_RUN_ASSET_DIRS, inst._FIRST_RUN_ASSET_FILES
            if kind == "dir":
                inst._FIRST_RUN_ASSET_DIRS = orig_dirs + (rel,)
            else:
                inst._FIRST_RUN_ASSET_FILES = orig_files + (rel,)
            try:
                res = inst.retire(announce=lambda _t: None)
            finally:
                inst._FIRST_RUN_ASSET_DIRS, inst._FIRST_RUN_ASSET_FILES = orig_dirs, orig_files
            survived = os.path.exists(victim)
    refused = bool(res.get("refused")) and res.get("reason") == "unsafe-retire-target"
    nothing_deleted = res.get("deleted") == []
    print(f"   {label}: added '{rel}' to the cleanup list; the guard refused? {refused} "
          f"(reason: {res.get('reason')}); your {kind} still there? {survived}; nothing deleted? "
          f"{nothing_deleted}")
    return refused and survived and nothing_deleted


def main() -> int:
    print("=" * 78)
    print("First-run cleanup safety: the setup cleanup never deletes anything that isn't the engine's own")
    print("=" * 78)

    print("\n[1] A dangerous FOLDER entry in the cleanup list. Expect: refused, your folder survives.")
    print("-" * 78)
    one = _refuses_dangerous_entry(
        "dir", os.path.join("src", "app"),
        lambda p: os.makedirs(p, exist_ok=True), "a plain (non-engine) folder")

    print("\n[2] A dangerous FILE entry in the cleanup list. Expect: refused, your file survives.")
    print("-" * 78)
    two = _refuses_dangerous_entry(
        "file", "IMPORTANT.md", _plant_file, "a plain (non-engine) file")

    ok = one and two
    print("\n" + "=" * 78)
    print("In plain words: the engine's setup cleanup only ever removes the engine's own leftover files. If")
    print("the cleanup list ever named one of your files or folders by mistake, the engine stops and removes")
    print("nothing rather than risk deleting something of yours. Your folder and file above are still there.")
    print("DEMO OK" if ok else "DEMO FAILED -- the guard did not refuse a dangerous entry")
    print("=" * 78)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
