#!/usr/bin/env python3
"""Behavioral FALSIFICATION for issue #599 (Slice 3) — a release that silently DROPS a shipped upgrade step
(a module migration) is REFUSED at cut time, before anything is written.

Upgrades replay migrations by version RANGE (module_manager.select_migrations runs each key where
from < ver <= target), so a key removed from a manifest is simply never iterated for an engine sitting below it
— skipped forever, never run. That is the #599 silent-skip class at the migration layer, and this guards it.

FAIL-THEN-PASS on the SAME synthetic tree; the only difference between the two arms is whether the candidate
keeps the previous release's migration key:
  * ARM 1 (the drop): the candidate drops the key -> `release_cut propose` REFUSES the cut (a non-zero exit that
    fails the release job at the propose step, before `apply` writes anything) with a plain-language reason.
  * ARM 2 (retained): the candidate keeps the key -> the cut PROCEEDS.

It exercises the REAL release-cut guard (release_cut.classify -> _cmd_propose) against a throwaway synthetic
engine tree, faking only the network baseline. Engine-VERSION cuts happen in the CONSTRUCTION repo (a deployed
repo cuts its own PRODUCT release, which never runs this engine-migration guard), so this is construction
evidence: it RETIRES at first run (.engine/provisioning/first-run-assets.json + instantiator._FIRST_RUN_ASSET_FILES)
rather than travelling. The permanent regression lives in test_release_cut.MigrationAccumulation.
"""
from __future__ import annotations
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate                # noqa: E402
import release_cut as rc       # noqa: E402  (the real cut guard under test)

_HOME = "acme/engine-home"
_MIG = {"0.2.0": {"description": "reshape the notes store", "run": "migrations/m.py", "kind": "config"}}


def _write(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(obj) + "\n")


def _module(mid: str, ver: str = "0.1.0", migrations=None) -> dict:
    m = {"id": mid, "version": ver, "status": "required", "provides": {}}
    if migrations:
        m["migrations"] = migrations
    return m


def _candidate_tree(modules: dict) -> str:
    root = tempfile.mkdtemp()
    _write(os.path.join(root, ".engine", "engine.json"),
           {"engine_release": "0.1.0", "packages": {mid: m["version"] for mid, m in modules.items()},
            "identity": "solo", "home_repository": _HOME})
    for mid, m in modules.items():
        _write(os.path.join(root, ".engine", "modules", mid, "manifest.json"), m)
    return root


def _baseline_tree(modules: dict) -> str:
    root = tempfile.mkdtemp()
    for mid, m in modules.items():
        _write(os.path.join(root, ".engine", "modules", mid, "manifest.json"), m)
    return root


def _propose(candidate: dict, baseline: dict) -> tuple:
    """Run the REAL `release_cut propose` for a candidate against an injected baseline (no network). Returns
    (exit_code, stderr_text)."""
    base = _baseline_tree(baseline)
    cand = _candidate_tree(candidate)
    saved = (validate.ROOT, validate.ENGINE_DIR, os.environ.get("GITHUB_REPOSITORY"), rc.resolve_baseline)
    validate.ROOT, validate.ENGINE_DIR = cand, os.path.join(cand, ".engine")
    os.environ["GITHUB_REPOSITORY"] = _HOME              # own == home => a construction (engine) cut, offline
    rc.resolve_baseline = lambda *a, **k: rc.Baseline("v0.0.9", False, "diff")
    err = io.StringIO()
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = rc._cmd_propose(types.SimpleNamespace(json=True, baseline_tree=base))
    finally:
        validate.ROOT, validate.ENGINE_DIR, repo, rc.resolve_baseline = saved
        if repo is None:
            os.environ.pop("GITHUB_REPOSITORY", None)
        else:
            os.environ["GITHUB_REPOSITORY"] = repo
        shutil.rmtree(base, ignore_errors=True)
        shutil.rmtree(cand, ignore_errors=True)
    return code, err.getvalue()


def main() -> int:
    ok = True

    print("ARM 1 — a release that DROPS a shipped upgrade step:")
    code, reason = _propose({"core": _module("core")}, {"core": _module("core", migrations=_MIG)})
    if code != 0 and "upgrade step" in reason.lower() and "0.2.0" in reason:
        print("  the cut was REFUSED before anything was written. The reason the maintainer sees:")
        for line in reason.strip().splitlines():
            print(f"    {line}")
    else:
        print(f"  FAIL: expected a refusal naming the dropped step, got exit {code}.")
        ok = False

    print("\nARM 2 — the same release that KEEPS the upgrade step:")
    code2, _ = _propose({"core": _module("core", migrations=_MIG)},
                        {"core": _module("core", migrations=_MIG)})
    if code2 == 0:
        print("  the cut PROCEEDS — nothing to refuse.")
    else:
        print(f"  FAIL: expected the cut to proceed, got exit {code2}.")
        ok = False

    print("\n" + ("PASS: a dropped migration blocks the cut; keeping it does not." if ok
                  else "FAIL: the migration-accumulation guard did not behave as intended."))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
