#!/usr/bin/env python3
"""Behavioral evidence for #864: a stale `module-surfaces.json` must fail the launcher's own self-tests
in the ENGINE'S HOME repo, and must be silently SKIPPED — not falsely green, not falsely red — in a
DEPLOYED copy, where the registry-drift case has no business running at all (#646's construction-only
scope). This is the case the launcher's earlier false green ("Self-tests PASSED" with the registry test
deterministically failing) hid: nesting alone used to gate the case, so any nested child — home or not —
ran it, and any un-nested one skipped it regardless of shape.

`repo_identity.is_home_repo` and `derived_state.is_confirmed_home` disagree ON PURPOSE about which way to
fail when a checkout's identity cannot be confidently placed: `is_home_repo` fails TOWARD home (so a
construction-only test case keeps running rather than silently skipping), while `is_confirmed_home` fails
TOWARD deployed (so a destructive/repo-wide regeneration refuses rather than running somewhere it
shouldn't). A plain local `git clone` of this repo inherits no manifest surprises, but it also inherits no
origin remote of its own — so both clones below get an EXPLICIT origin set before any assertion runs,
rather than relying on whatever a bare clone happens to read as "no origin" (which `is_home_repo` would
also read as home, defeating the deployed-shape arm of this demo).

Both clones are the COMMITTED tree only (a `--no-checkout` clone pinned to ROOT's own HEAD, mirroring the
pin `test_launch_contract.py` uses), never the working tree — so this demo proves the shipped construction
predicate, not whatever happens to be sitting uncommitted in this worktree. This is CONSTRUCTION EVIDENCE
(it clones the whole engine and asserts on the home/deployed split), retired from a generated repo at
first-run; the durable per-PR guard is `test_selftest.py`'s banner-skip-count tests plus
`test_module_surfaces.TestRegistryInSync`. Run it directly:
    uv run --directory .engine --frozen -- python tools/demo_864_stale_registry_selftest.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import repo_identity  # noqa: E402
import validate        # noqa: E402

_SELFTEST_REL = os.path.join("tools", "selftest.py")
_REGISTRY_REL = os.path.join(".engine", "provisioning", "module-surfaces.json")
_INHERIT_DROP = ("ENGINE_NESTED_SELFTEST", "ENGINE_DEPLOYED_PROJECTION")


# The case #864 saw masked: it must be NAMED in the run record, not just its module.
REGISTRY_CASE = "test_committed_registry_matches_the_derived_set"

def _git(*args, cwd=None, check=True):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check)


def _resolved_head(root: str) -> str:
    return _git("rev-parse", "HEAD", cwd=root).stdout.strip()


def _clone_pinned(root: str, dest: str, *, shared: bool) -> str:
    """A `--no-checkout` clone of `root`, detached at `root`'s own resolved HEAD sha — the COMMITTED tree,
    never the working tree — mirroring the pin `test_launch_contract.LaunchContractTests.setUpClass` uses."""
    head = _resolved_head(root)
    args = ["clone", "--quiet", "--no-checkout"]
    if shared:
        args.append("--shared")
    _git(*args, root, dest)
    _git("checkout", "--quiet", "--detach", head, cwd=dest)
    return dest


def _set_origin(clone: str, slug: str) -> None:
    _git("remote", "set-url", "origin", f"https://github.com/{slug}.git", cwd=clone)


def _stale_registry_key(clone: str) -> str:
    """Delete one deterministic (first sorted) key from the CLONE's own registry and write it back. Never
    touches ROOT. Returns the removed key."""
    path = os.path.join(clone, _REGISTRY_REL)
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    surfaces = doc["surfaces"]
    key = sorted(surfaces)[0]
    del surfaces[key]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False)
        fh.write("\n")
    return key


def _restore_registry(clone: str) -> None:
    _git("checkout", "--quiet", "--", _REGISTRY_REL, cwd=clone)


def _run_launcher(clone: str, record_path: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    for key in _INHERIT_DROP:
        env.pop(key, None)
    return subprocess.run(
        [sys.executable, _SELFTEST_REL,
         "--pattern", "test_module_surfaces.py",
         "--run-record-path", record_path],
        cwd=os.path.join(clone, ".engine"),
        capture_output=True, text=True, env=env, timeout=120,
    )


def _read_record(record_path: str) -> dict:
    with open(record_path, encoding="utf-8") as fh:
        return json.load(fh)


def main() -> int:
    real_root = validate.ROOT
    failures = []
    print("=" * 78)
    print("DEMO #864 — a stale module-surfaces.json fails the registry self-test in the home repo, and is")
    print("silently SKIPPED (not falsely green, not falsely red) in a deployed copy.")
    print("=" * 78)

    home_slug = repo_identity.home_repository(real_root)
    print(f"\n[setup] recorded home_repository: {home_slug}")

    original = None
    with open(os.path.join(real_root, _REGISTRY_REL), "rb") as fh:
        original = fh.read()
    # The status this demo must PRESERVE, not necessarily "clean" — a construction worktree with an
    # uncommitted registry regeneration in flight (as Part C of this build leaves it) legitimately shows
    # modified here; what matters is that running THIS demo changes nothing further.
    original_status = _git("status", "--porcelain=v1", "--", _REGISTRY_REL, cwd=real_root).stdout

    tmp = tempfile.mkdtemp(prefix="demo-864-")
    real_tmp_root = os.path.realpath(tempfile.gettempdir())
    assert os.path.realpath(tmp).startswith(real_tmp_root), \
        f"fixture dir {tmp} escaped the system temp dir {real_tmp_root}"

    try:
        # ---- Step 2-3: home clone, pinned to ROOT's committed HEAD, origin set to the recorded home ----
        home_clone = _clone_pinned(real_root, os.path.join(tmp, "home-clone"), shared=True)
        _set_origin(home_clone, home_slug)
        is_home = repo_identity.is_home_repo(home_clone)
        print(f"\n[1] home clone origin set to {home_slug}; repo_identity.is_home_repo == {is_home}")
        if not is_home:
            failures.append(f"home clone with origin {home_slug} was not recognized as home_repo")

        # ---- Step 5: stale the home clone's registry ----
        removed_key = _stale_registry_key(home_clone)
        print(f"[2] staled the home clone's registry — removed key: {removed_key!r}")

        # ---- Step 6: run the real launcher against the staled home clone; must FAIL on the registry case ----
        record_path = os.path.join(tmp, "record-home-stale.json")
        proc = _run_launcher(home_clone, record_path)
        record = _read_record(record_path)
        problem_ids = [p.get("id", "") for p in record.get("problems", [])]
        print(f"[3] launcher exit={proc.returncode}, record verdict={record.get('verdict')!r}, "
              f"problems={problem_ids}")
        print("    banner tail:")
        for ln in proc.stdout.strip().splitlines()[-6:]:
            print(f"      {ln}")
        home_skipped = record.get("executed", {}).get("skipped_count", 0)
        if record.get("verdict") == "passed" and home_skipped >= 1:
            # The exact #864 shape: a staled registry, a green verdict, and a skip where the failure should
            # be — the registry test was SKIPPED rather than failed. This is the false green the fix removes.
            failures.append(f"staled home clone: verdict 'passed' with executed.skipped_count={home_skipped} — "
                            f"the registry test was SKIPPED rather than failed (the #864 false green)")
        elif record.get("verdict") != "failed":
            failures.append(f"staled home clone: expected verdict 'failed', got {record.get('verdict')!r}")
        if not any(REGISTRY_CASE in pid for pid in problem_ids):
            failures.append(f"staled home clone: {REGISTRY_CASE} did not appear among the record's problems")
        if "FAILED" not in proc.stdout:
            failures.append("staled home clone: banner did not contain FAILED")

        # ---- Step 7: restore the clone's registry from its own git; must PASS ----
        _restore_registry(home_clone)
        record_path2 = os.path.join(tmp, "record-home-restored.json")
        proc2 = _run_launcher(home_clone, record_path2)
        record2 = _read_record(record_path2)
        print(f"\n[4] restored home clone's registry; launcher exit={proc2.returncode}, "
              f"record verdict={record2.get('verdict')!r}")
        print("    banner tail:")
        for ln in proc2.stdout.strip().splitlines()[-6:]:
            print(f"      {ln}")
        if record2.get("verdict") != "passed":
            failures.append(f"restored home clone: expected verdict 'passed', got {record2.get('verdict')!r}")
        if "PASSED" not in proc2.stdout:
            failures.append("restored home clone: banner did not contain PASSED")
        if "skipped" not in proc2.stdout:
            failures.append("restored home clone: banner did not contain a '… skipped' line")

        # ---- Step 8: deployed-shape clone (foreign origin) with the same staling; must SKIP, not fail ----
        deployed_clone = _clone_pinned(real_root, os.path.join(tmp, "deployed-clone"), shared=True)
        _set_origin(deployed_clone, "acme/deployed-product")
        is_home2 = repo_identity.is_home_repo(deployed_clone)
        print(f"\n[5] deployed-shape clone origin set to acme/deployed-product; is_home_repo == {is_home2}")
        if is_home2:
            failures.append("deployed-shape clone with a foreign origin was recognized as home_repo")
        removed_key2 = _stale_registry_key(deployed_clone)
        print(f"    staled the deployed clone's registry — removed key: {removed_key2!r}")
        record_path3 = os.path.join(tmp, "record-deployed-stale.json")
        proc3 = _run_launcher(deployed_clone, record_path3)
        record3 = _read_record(record_path3)
        skipped_count = record3.get("executed", {}).get("skipped_count", 0)
        print(f"[6] launcher exit={proc3.returncode}, record verdict={record3.get('verdict')!r}, "
              f"executed.skipped_count={skipped_count}")
        print("    banner tail:")
        for ln in proc3.stdout.strip().splitlines()[-6:]:
            print(f"      {ln}")
        if record3.get("verdict") != "passed":
            failures.append(f"deployed clone: expected the registry case to be SKIPPED (verdict 'passed'), "
                            f"got {record3.get('verdict')!r} — origin masking failed to skip it")
        if skipped_count < 1:
            failures.append("deployed clone: expected executed.skipped_count >= 1 (the registry case skipped)")
        # The run record counts skips but does not name them (StarshipSuperjam/engine-template#1253 is the
        # per-case surfacing). The arm still holds: the registry file IS staled, so a green verdict means the
        # case that would have failed on it did not run.
        print("    the deployed shape masks the staled registry by origin: the file is staled and the verdict")
        print("    is green, so the registry case did not run there (the record counts skips, not names).")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- Step 9: the REAL registry at ROOT must be untouched ----
    with open(os.path.join(real_root, _REGISTRY_REL), "rb") as fh:
        after = fh.read()
    if after != original:
        failures.append("the REAL registry at ROOT was modified by this demo — this must never happen")
    status = _git("status", "--porcelain=v1", "--", _REGISTRY_REL, cwd=real_root).stdout
    if status != original_status:
        failures.append(f"git status for the REAL registry changed during this demo: "
                        f"before={original_status!r} after={status!r}")
    print(f"\n[7] real registry at ROOT unchanged: {after == original}; "
          f"git status for it unchanged by this demo: {status == original_status}")

    print("\n" + "=" * 78)
    if failures:
        print("DEMO #864 FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("DEMO #864 PASSED: a stale registry fails the launcher in the home repo (origin == recorded home), "
          "a restored registry passes, and the same staling is silently SKIPPED — never falsely green, "
          "never falsely red — in a deployed-shape clone (foreign origin). The real registry at ROOT was "
          "never touched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
