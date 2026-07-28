"""Release-cut deployment gate — the cut-time proof that a release OPERATES and UPGRADES when deployed.

A deployed repo runs a projected shape of this engine (the first-run-only setup files retired, the optional
modules a deployment declined absent). Two failure classes ride on that shape and never show up in the home
repo's own per-PR suite: a self-test that asserts a construction-only invariant with no deployed-skip guard
(the #599 class — it *operates* wrong when deployed), and a wiring-map regeneration that fails closed on an
optional module's absent subtree (the #663 class — the upgrade *reconcile* reds and stalls half-applied). This
gate catches both at CONSTRUCTION cut time, before a release pull request is ever opened:

- **Arm A — operates when deployed.** Project the release candidate to the deployed shape and run the
  validator + the whole self-test suite against it, in two configurations: the default install (every shipped
  module) and an optional-modules-declined install (each `default-on` module and the files it owns removed —
  the exact shape #663 broke on). A red here means the release would not operate on a real deployment.
- **Arm B — upgrades when deployed.** For each released baseline at or above the clean-upgrade floor, project
  that past release to its deployed shape and run a REAL practice upgrade to the candidate — the same child
  tail, the same six-check structural gate (including the wiring-map coverage check #663 failed), no pull
  request opened. A red means a deployed engine could not reconcile cleanly onto this release.

**Where deployed-shape protection now lives.** This gate REPLACES the inline `test_deployed_selftests.py` belt,
which ran Arm A's default configuration on every home-repo pull request (~44% of the suite's wall time). That
protection now runs at each release cut (and on demand via the `release-gate` workflow), NOT per pull request —
so a deployment-shape regression that lands on the default branch is caught at the next cut or manual run, not
on the pull request that introduced it. This is the deliberate #664 trade (the reopened #649 decision).

**Fail CLOSED.** A gate that cannot even build its projection, or that hits any unexpected error, BLOCKS the
cut — it never waves a release through unverified. The only clean pass is "ran, both arms green". The tool is
home-repo-only (`is_home_repo`): it ships committed but is inert on a deployed repo, whose own `engine-ci`
runs the suite directly. On a genuine engine cut the workflow asserts the gate actually RAN (not skipped), so
an origin-signal misdetection can never silently ship an engine release ungated.

**No bypass.** A red gate always blocks the cut (operator decision, 2026-07-28). A transient flake is cleared
by re-running the `release-gate` workflow; a real red is fixed and the release re-cut.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate                              # noqa: E402
import module_manager                        # noqa: E402  (retire_set safe reader, _archive_tree, upgrade)
import census_completeness_check as _ccc     # noqa: E402  (shared home-repo predicate; monkeypatched in tests)

# Set on every nested run this gate spawns (the in-projection suite and the upgrade driver) so a projected
# copy of the suite never re-enters the gate. Shared name with the retired belt's guard; `test_release_gate.py`
# also skips its gate-driving cases when it is set (belt-and-suspenders alongside the home-repo skip).
_NESTED_ENV = "ENGINE_NESTED_SELFTEST"

# A deployed origin that differs from the recorded home, so the projection reads as a downstream copy
# (`is_home_repo` -> False), exactly as the retired belt relied on.
_DEPLOYED_ORIGIN = "https://github.com/acme/deployed-product.git"

# The env var the in-projection driver reads to (1) verify it is running against the projection, not home
# (the ROOT-isolation guard), and (2) find the candidate release tree to inject.
_DRIVER_EXPECT_ROOT = "ENGINE_GATE_EXPECT_ROOT"
_DRIVER_CANDIDATE = "ENGINE_GATE_CANDIDATE"


class GateError(RuntimeError):
    """A gate step that could not complete — a setup/projection failure, or an unexpected error. It is a
    BLOCK, never a skip: the caller reports the cut as gated. Distinct from an arm finding a real red (also a
    block) only in the message; both stop the cut."""


def _run(cmd: list, cwd: str | None = None, env: dict | None = None, timeout: int = 600):
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)


def _tail(text: str | None, n: int = 600) -> str:
    return (text or "").strip()[-n:]


# --------------------------------------------------------------------------- projection

def _archive_candidate(dest: str) -> str:
    """Capture the current working tree as the release candidate — EXACTLY the bytes the cut will commit —
    through a THROWAWAY git index, so the real index is never touched. At the `release.yml` insertion point the
    candidate (version bumps + regenerated maps) is written but not yet committed; staging it into a temp index
    and archiving that tree yields the same content `git add -A && git commit` would, and shares the single
    'archive of a tree' source with the baselines (`_archive_tree`). Raises GateError on any git failure."""
    os.makedirs(dest, exist_ok=True)
    with tempfile.TemporaryDirectory() as idx_dir:
        env = {**os.environ, "GIT_INDEX_FILE": os.path.join(idx_dir, "index")}
        staged = _run(["git", "-C", validate.ROOT, "add", "-A"], env=env, timeout=120)
        if staged.returncode != 0:
            raise GateError(f"could not stage the candidate working tree ({_tail(staged.stderr)})")
        tree = _run(["git", "-C", validate.ROOT, "write-tree"], env=env, timeout=60)
        if tree.returncode != 0:
            raise GateError(f"could not capture the candidate tree ({_tail(tree.stderr)})")
        tree_sha = tree.stdout.strip()
    try:
        return module_manager._archive_tree(tree_sha, dest)
    except Exception as exc:                              # noqa: BLE001 — surface as a clean block
        raise GateError(f"could not archive the candidate tree ({exc})")


def _archive_baseline(tag: str, dest: str) -> str:
    """Materialize a released tag's committed tree offline (`git archive`). Raises GateError if the tag's tree
    object is absent — a shallow checkout with no tags fails the cut rather than silently skipping a baseline."""
    try:
        return module_manager._archive_tree(tag, dest)
    except Exception as exc:                              # noqa: BLE001
        raise GateError(f"could not archive baseline {tag} offline ({exc}) — is the checkout shallow / tag-less?")


def _decline_optional_modules(tree: str) -> list:
    """Model a deployment that DECLINED every optional (`default-on`) add-on, using the engine's OWN per-module
    removal run inside the tree (`module_manager.py remove`) — so the files, the `engine.json` packages entry,
    the tool-runtime dependency groups, the wiring, and coherence are all reconciled exactly as a real decline
    leaves them, never a hand-rolled deletion that drifts (e.g. a stale `default-groups` the uv-group-drift
    check would then red on). The required substrate that lazily imports an optional subtree stays — the exact
    #663 shape. Returns the declined module ids. Raises GateError on any failure."""
    modules_dir = os.path.join(tree, ".engine", "modules")
    if not os.path.isdir(modules_dir):
        raise GateError("the candidate tree has no .engine/modules directory to project a declined shape from")
    default_on = []
    for mid in sorted(os.listdir(modules_dir)):
        man_path = os.path.join(modules_dir, mid, "manifest.json")
        if not os.path.isfile(man_path):
            continue
        try:
            status = validate.load_json(man_path).get("status")
        except Exception as exc:                          # noqa: BLE001
            raise GateError(f"could not read module manifest for {mid} ({exc})")
        if status == "default-on":                        # only default-on modules are installed-and-declinable
            default_on.append(mid)
    env = {**os.environ, _NESTED_ENV: "1"}
    for mid in default_on:
        r = _run([sys.executable, os.path.join("tools", "module_manager.py"), "remove", mid, "--json"],
                 cwd=os.path.join(tree, ".engine"), env=env, timeout=300)
        if r.returncode != 0:
            raise GateError(f"could not project a declined shape (removing {mid}: {_tail(r.stderr or r.stdout)})")
    return default_on


def _project_to_deployed(dest: str, *, decline_optional: bool = False) -> list:
    """Turn an archived home-repo tree at `dest` into the shape a deployed repo actually runs — the same
    projection first-run provisioning applies: RETIRE the first-run-only assets (through `retire_set`, the
    fail-loud safe reader — NOT a naive re-read), optionally DECLINE the optional modules, git-init with a
    deployed origin so it reads as a copy, and REGENERATE the deployed-state indexes (self-map + knowledge
    graph) that now describe the reduced surface. Any failure raises GateError (the cut is blocked, never
    skipped). Returns the declined module ids (empty unless `decline_optional`)."""
    try:
        r_files, r_dirs = module_manager.retire_set(dest)     # the safe reader; raises on a bad manifest
    except Exception as exc:                                  # noqa: BLE001
        raise GateError(f"could not read the release's setup-file list to project the deployed shape ({exc})")
    for rel in list(r_files) + list(r_dirs):
        p = os.path.join(dest, rel)
        if os.path.isfile(p):
            os.remove(p)
        elif os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
    declined = _decline_optional_modules(dest) if decline_optional else []
    env = {**os.environ, _NESTED_ENV: "1"}
    for cmd in (["init", "-b", "main"],
                ["remote", "add", "origin", _DEPLOYED_ORIGIN],
                ["-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"],
                ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "deployed"]):
        r = _run(["git", "-C", dest, *cmd], timeout=120)
        if r.returncode != 0:
            raise GateError(f"could not build the deployed projection (git {cmd[0]}: {_tail(r.stderr)})")
    for gen in ("self_map.py", "knowledge_gen.py"):
        r = _run([sys.executable, os.path.join("tools", gen), "generate"],
                 cwd=os.path.join(dest, ".engine"), env=env, timeout=300)
        if r.returncode != 0:
            # On a declined projection this regen IS the #663 operation — a failure here is the real defect.
            raise GateError(f"the deployed projection could not regenerate its wiring map "
                            f"({gen} on {'a module-declined' if decline_optional else 'the default'} shape: "
                            f"{_tail(r.stderr)})")
    return declined


def _assert_isolated(projection: str) -> None:
    """Refuse to drive a mutating upgrade unless the projection is a real temporary directory distinct from the
    home checkout — the belt-and-suspenders half of the ROOT-isolation guarantee (the driver also asserts its
    own resolved ROOT). A practice upgrade overlays and deletes files at its ROOT; running it against home
    would corrupt the release mid-cut."""
    proj = os.path.realpath(projection)
    home = os.path.realpath(validate.ROOT)
    if proj == home or not proj.startswith(os.path.realpath(tempfile.gettempdir()) + os.sep):
        raise GateError(f"refusing to run an upgrade against a non-throwaway tree ({proj})")


def _worktree_digest() -> str:
    """A cheap fingerprint of the home working tree's tracked+staged state (`git status` porcelain + the index
    tree), used to prove the gate wrote nothing to the tree the cut is about to commit."""
    status = _run(["git", "-C", validate.ROOT, "status", "--porcelain"], timeout=60)
    with tempfile.TemporaryDirectory() as idx_dir:
        env = {**os.environ, "GIT_INDEX_FILE": os.path.join(idx_dir, "index")}
        _run(["git", "-C", validate.ROOT, "add", "-A"], env=env, timeout=120)
        tree = _run(["git", "-C", validate.ROOT, "write-tree"], env=env, timeout=60)
    return f"{status.stdout}\n{tree.stdout}"


# --------------------------------------------------------------------------- arms

def _validate_in(tree: str, label: str) -> dict:
    """Run the CI validator suite inside a projected tree. Returns {passed, detail}."""
    env = {**os.environ, _NESTED_ENV: "1"}
    r = _run([sys.executable, os.path.join("tools", "validate.py"), "--suite", "CI"],
             cwd=os.path.join(tree, ".engine"), env=env, timeout=900)
    return {"passed": r.returncode == 0,
            "detail": "" if r.returncode == 0 else f"{label}: validator red\n{_tail(r.stdout + r.stderr, 3000)}"}


def _suite_in(tree: str, label: str) -> dict:
    """Run the whole self-test suite inside a projected tree. Returns {passed, detail}."""
    env = {**os.environ, _NESTED_ENV: "1"}
    r = _run([sys.executable, "-m", "unittest", "discover", "-s", "tools", "-p", "test_*.py", "-b"],
             cwd=os.path.join(tree, ".engine"), env=env, timeout=1800)
    return {"passed": r.returncode == 0,
            "detail": "" if r.returncode == 0 else f"{label}: self-tests red\n{_tail(r.stderr, 3000)}"}


def _arm_operates(candidate: str) -> dict:
    """Arm A. Project the candidate to the deployed shape (default and optional-declined) and assert it
    operates. Default: validator + full suite (subsumes the retired belt). Declined: validator (whose
    knowledge-coverage check is the #663 detector, exercised by the declined projection's own regen)."""
    failures: list = []
    with tempfile.TemporaryDirectory() as d:
        default_tree = _archive_candidate(os.path.join(d, "default"))
        _project_to_deployed(default_tree, decline_optional=False)
        for res in (_validate_in(default_tree, "operate/default"),
                    _suite_in(default_tree, "operate/default")):
            if not res["passed"]:
                failures.append(res["detail"])
    with tempfile.TemporaryDirectory() as d:
        declined_tree = _archive_candidate(os.path.join(d, "declined"))
        declined = _project_to_deployed(declined_tree, decline_optional=True)
        res = _validate_in(declined_tree, f"operate/declined({','.join(declined) or 'none'})")
        if not res["passed"]:
            failures.append(res["detail"])
    return {"passed": not failures, "failures": failures}


def _driver_source() -> str:
    """The in-projection driver: run as `python -c` with the working directory inside the projection's own
    `.engine/tools`, so `import module_manager` binds the PROJECTION's copy and `validate.ROOT` (derived from
    that file's own path) resolves to the projection — never home. It asserts that isolation itself before
    mutating anything, injects the candidate as `release_tree` alone (practice mode -> the real six-check
    child gate, no pull request), and prints the result JSON."""
    return (
        "import json, os, sys\n"
        "sys.path.insert(0, os.getcwd())\n"
        "import validate, module_manager\n"
        "expect = os.path.realpath(os.environ['%s'])\n"
        "here = os.path.realpath(validate.ROOT)\n"
        "assert here == expect, 'ROOT isolation breach: %%r != %%r' %% (here, expect)\n"
        "res = module_manager.upgrade(release_tree=os.environ['%s'])\n"
        "sys.stdout.write('GATE_RESULT:' + json.dumps(res))\n"
    ) % (_DRIVER_EXPECT_ROOT, _DRIVER_CANDIDATE)


def _upgrade_from(baseline_tag: str, candidate: str) -> dict:
    """Arm B, one baseline. Project the baseline release to its deployed shape, then run a REAL practice
    upgrade to the candidate driven by the PROJECTION's own module_manager (so phase-1 runs as the baseline's
    shipped code, exactly as a real deployment would, and the tail runs as the overlaid candidate code). Assert
    the upgrade applied, the six-check gate found nothing hard, and it took the practice child path (not a
    silent network fetch or fall-through). Returns {passed, detail}."""
    with tempfile.TemporaryDirectory() as d:
        proj = _archive_baseline(baseline_tag, os.path.join(d, "old"))
        _project_to_deployed(proj, decline_optional=False)
        _assert_isolated(proj)
        env = {**os.environ, _NESTED_ENV: "1",
               _DRIVER_EXPECT_ROOT: os.path.abspath(proj),
               _DRIVER_CANDIDATE: os.path.abspath(candidate)}
        env.pop("GITHUB_TOKEN", None)                    # practice mode opens no PR; deny the token outright
        run = _run([sys.executable, "-c", _driver_source()],
                   cwd=os.path.join(proj, ".engine", "tools"), env=env, timeout=1200)
        if run.returncode != 0 or "GATE_RESULT:" not in run.stdout:
            return {"passed": False,
                    "detail": f"upgrade/{baseline_tag}: the practice upgrade did not complete\n"
                              f"{_tail(run.stderr or run.stdout, 3000)}"}
        result = json.loads(run.stdout.split("GATE_RESULT:", 1)[1])
    problems = []
    if result.get("refused"):
        problems.append(f"refused: {result.get('reason')}")
    if not result.get("applied"):
        problems.append("the upgrade did not apply to the projected deployment")
    hard = [f for f in result.get("findings", []) if (f or {}).get("severity") == "hard"]
    if hard:
        problems.append(f"the structural gate found {len(hard)} blocking problem(s): "
                        + "; ".join(str((f or {}).get("id") or (f or {}).get("message")) for f in hard))
    notes = " ".join(result.get("notes") or [])
    if "practice run" not in notes:                      # the upgrade must have taken the practice child path
        problems.append("the upgrade did not take the expected practice path (it may have fetched a real "
                        "release instead of testing the candidate)")
    return {"passed": not problems, "detail": "" if not problems
            else f"upgrade/{baseline_tag}: " + "; ".join(problems)}


def _upgrade_baselines() -> list:
    """The released baselines Arm B upgrades FROM: every version tag at or above the candidate's clean-upgrade
    floor, deduped. Tags are `v`-prefixed; the floor is bare — strip the `v` before the version compare."""
    floor = None
    try:
        floor = (validate.load_json(os.path.join(validate.ROOT, ".engine", "engine.json")) or {}).get(
            "min_upgradeable_from")
    except Exception:                                    # noqa: BLE001 — no floor -> take all version tags
        floor = None
    tags = _run(["git", "-C", validate.ROOT, "tag", "--list", "v*"], timeout=60)
    if tags.returncode != 0:
        raise GateError(f"could not list release tags to pick upgrade baselines ({_tail(tags.stderr)})")
    baselines = []
    for line in tags.stdout.split():
        m = re.match(r"^v(\d+\.\d+\.\d+)$", line.strip())
        if not m:
            continue
        version = m.group(1)
        if floor and validate._ver_tuple(version) < validate._ver_tuple(floor):
            continue
        baselines.append(line.strip())
    return sorted(set(baselines), key=lambda t: validate._ver_tuple(t[1:]))


def _arm_upgrades(candidate: str) -> dict:
    """Arm B. Upgrade each in-range released baseline to the candidate and collect the failures."""
    baselines = _upgrade_baselines()
    if not baselines:
        raise GateError("found no released baseline at or above the clean-upgrade floor to test upgrades from")
    failures = []
    for tag in baselines:
        res = _upgrade_from(tag, candidate)
        if not res["passed"]:
            failures.append(res["detail"])
    return {"passed": not failures, "baselines": baselines, "failures": failures}


# --------------------------------------------------------------------------- entrypoint

def run_gate() -> dict:
    """Run both arms against the candidate captured from the working tree. Returns a structured result:
    `ran` (False only when inert on a non-home checkout), `passed`, per-arm detail. Fails CLOSED — any
    GateError becomes `passed=False` with a plain reason; the caller must treat a non-pass as a blocked cut."""
    if not _ccc._in_home_repo():
        return {"ran": False, "passed": True, "reason": "inert: not the engine's home repo (a deployed repo "
                "runs the suite directly in its own engine-ci)"}
    before = _worktree_digest()
    with tempfile.TemporaryDirectory() as d:
        candidate = _archive_candidate(os.path.join(d, "candidate"))
        arm_a = _arm_operates(candidate)
        arm_b = _arm_upgrades(candidate)
    after = _worktree_digest()
    result = {"ran": True, "operates": arm_a, "upgrades": arm_b,
              "passed": bool(arm_a["passed"] and arm_b["passed"])}
    if before != after:                                  # the gate must have written nothing to the home tree
        result["passed"] = False
        result["home_tree_mutated"] = True
    return result


def _render(result: dict) -> str:
    """Plain-language operator copy for the cut (never a check id or arm token). A blocked cut says what would
    not work when deployed and that nothing was changed."""
    if not result.get("ran"):
        return "The deployment gate is inert here (this is not the engine's home repo); nothing to check."
    if result.get("passed"):
        return "The deployment gate passed: this release operates and upgrades cleanly when deployed."
    if result.get("home_tree_mutated"):
        return ("The deployment gate was stopped because it detected an unexpected change to the release "
                "working copy while checking. No release pull request was opened and nothing was changed; "
                "this is an engine defect to report.")
    parts = ["This release would not work correctly when deployed, so no release pull request was opened and "
             "nothing was changed:"]
    if not (result.get("operates") or {}).get("passed", True):
        parts.append("  - it does not OPERATE cleanly on a deployed shape (a self-test or consistency check "
                     "failed against a projected deployment).")
    if not (result.get("upgrades") or {}).get("passed", True):
        parts.append("  - a deployed engine could not UPGRADE cleanly onto it from a supported version.")
    parts.append("Fix the problem and cut the release again; a transient infrastructure hiccup clears by "
                 "re-running the deployment gate.")
    return "\n".join(parts)


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="Release-cut deployment gate (operate + upgrade when deployed).")
    ap.add_argument("--json", action="store_true", help="emit the structured result as JSON on stdout")
    args = ap.parse_args(argv)
    try:
        result = run_gate()
    except GateError as exc:                             # fail CLOSED: a setup failure blocks the cut
        result = {"ran": True, "passed": False, "reason": str(exc)}
    except Exception as exc:                             # noqa: BLE001 — any unexpected error also blocks
        result = {"ran": True, "passed": False, "reason": f"the deployment gate hit an unexpected error ({exc})"}
    if args.json:
        sys.stdout.write(json.dumps(result) + "\n")
    else:
        sys.stdout.write(_render(result) + "\n")
        if result.get("reason") and not result.get("passed"):
            sys.stderr.write(result["reason"] + "\n")
        for arm in ("operates", "upgrades"):
            for detail in (result.get(arm) or {}).get("failures", []):
                sys.stderr.write(detail + "\n")
    return 0 if result.get("passed") else 1


if __name__ == "__main__":
    sys.exit(main())
