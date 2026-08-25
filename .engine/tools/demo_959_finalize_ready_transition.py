#!/usr/bin/env python3
"""Demo — the owned-build finalize transition cannot report "ready" before GitHub confirms it (#959).

What #959 is about, in plain words: when the engine builds a change to its own product, the work is carried
by a DRAFT pull request. "Done" means that draft has been turned READY for you to merge. The worry #959
raised is a build that stops one command short — the work is complete, but the pull request is left in draft,
so the durable handoff never happens; and, worse, that a tool might *say* it finished when the pull request is
actually still a draft. The fix already lives in the build coordinator's `submit apply`: it marks the draft
ready and then reads GitHub back, refusing to record or report success unless GitHub confirms the pull request
is really open, un-drafted, and still on the exact commit that was reviewed. This demo proves that guarantee —
and proves it can FAIL, which is what makes it evidence rather than decoration.

How it runs (and why it's trustworthy):
  * It exercises the REAL coordinator. Each scenario invokes `build_coordinator.py submit apply` as a
    subprocess ROOTED IN A THROWAWAY COPY of this repo (the tool is called as
    `<copy>/.engine/tools/build_coordinator.py`, so its `ROOT` resolves to the copy, exactly like
    demo_release_pr_mergeable.py). The real `_submit_preview`, the real head/base/body pins, and the real
    post-`gh pr ready` read-back all run — nothing about the transition is mocked.
  * Only the GitHub *service* is faked. A tiny fake `gh` on `PATH` models ONE pull request via a JSON state
    file: it answers `gh pr view … --json …` and, on `gh pr ready …`, flips the draft flag according to the
    scenario. CI cannot reach real GitHub and must never mutate a real pull request, so the boundary the real
    coordinator shells out to is the one thing stood in for — the same seam the coordinator's own unit tests
    stub. No network, no real repository or pull request touched.
  * Readiness accounting is seeded, not re-demonstrated. Reaching `submit apply`'s ready gate needs a whole
    build's worth of evidence (approval, validation, preflights, a complete PR contract). That machinery is
    covered exhaustively by test_build_coordinator.py; here a trivial-profile fixture build is seeded straight
    to the ready gate so the demo can concentrate on the FINALIZE TRANSITION itself — the subject of #959.

Four scenarios, two of them negative controls that MUST bite:
  (1) Honest ready      — `gh pr ready` really un-drafts the PR: submit apply marks ready, the read-back
                          confirms draft==false on the expected head, and it records/report success (exit 0).
  (2) Idempotent re-run — the PR is already ready: submit apply takes the record-ready path, calls
                          `gh pr ready` ZERO times, and still ends "ready" (exit 0).
  (3) No-op ready (#959) — `gh pr ready` is a no-op and the PR stays draft: submit apply must NOT report
                          success — it fails non-zero and drives the PR back to draft. This is the exact
                          fault #959 names: completion is impossible to report before GitHub confirms ready.
  (4) Moved head        — the head moves out from under the ready transition: submit apply rejects the
                          changed evidence, fails non-zero, and returns the PR to draft.

Run: uv run --directory .engine -- python tools/demo_959_finalize_ready_transition.py
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_coordinator as bc  # noqa: E402 — the real coordinator; used for schema-true seeding helpers
import validate                  # noqa: E402 — locates this repo's root (validate.ROOT) to copy

REPO = "owner/finalize-demo"
PR = 7
BODY = "Finalize-transition demo fixture body — the exact PR contract the read-back pins."
OTHER_HEAD = "b" * 40  # a syntactically valid but different head, for the moved-head control

# A minimal, schema-true fixture plan: trivial profile + quick depth takes the coordinator's fast path to the
# ready gate (no cold-review packets), and a `direct`/`none` spec needs no Issue read (spec_digest is None).
FIXTURE_PLAN = {
    "schema_version": "build-plan.v2",
    "profile": "trivial",
    "intent_source": {"kind": "direct"},
    "raw_intent": "Fixture build whose only purpose is to exercise the finalize/ready transition.",
    "objective": "Reach the submission-ready gate so the finalize transition can be exercised end to end.",
    "success_obligations": [
        {"outcome": "The draft PR is marked ready only after GitHub confirms it is no longer a draft.",
         "verification": "submit apply reads GitHub back and requires isDraft==false on the expected head."}
    ],
    "work_items": [
        {"id": "W1", "description": "Fixture work item (no real change).",
         "paths": [".engine/tools/build_coordinator.py"], "verification": ["Fixture only."],
         "depends_on": [], "exclusive_resources": [], "executor_class": "integrator",
         "output_contract": {"deliverable": "The fixture work item",
                             "artifact_kinds": ["integrated-commit"],
                             "required_evidence": ["changed_paths", "verification_results"]}}
    ],
    "parallelism": {"mode": "serial", "max_concurrency": 1},
    "spec": {"posture": "none", "selection_basis": "Demo fixture; no product specification governs it.",
             "disclosure": "No settled spec; the fixture's obligations are the referent."},
}

# The fake `gh`. It models one PR via $DEMO_PR_STATE and logs `gh pr ready` invocations to $DEMO_PR_CALLS so
# the idempotent scenario can prove set_ready was never called. $DEMO_READY_MODE selects how `gh pr ready`
# (without --undo) resolves: honest un-drafts; noop leaves the draft; movehead un-drafts but moves the head.
_FAKE_GH = '''#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
state_path = os.environ["DEMO_PR_STATE"]
calls_path = os.environ["DEMO_PR_CALLS"]
mode = os.environ.get("DEMO_READY_MODE", "honest")
with open(state_path) as fh:
    pr = json.load(fh)

def save():
    with open(state_path, "w") as fh:
        json.dump(pr, fh)

# gh pr view <n> --repo <r> --json <csv>
if argv[:2] == ["pr", "view"]:
    fields = []
    if "--json" in argv:
        fields = argv[argv.index("--json") + 1].split(",")
    out = {k: pr[k] for k in fields if k in pr} if fields else pr
    print(json.dumps(out))
    sys.exit(0)

# gh pr ready <n> --repo <r> [--undo]
if argv[:2] == ["pr", "ready"]:
    if "--undo" in argv:
        pr["isDraft"] = True
        save()
        with open(calls_path, "a") as fh:
            fh.write("ready-undo\\n")
        sys.exit(0)
    with open(calls_path, "a") as fh:
        fh.write("ready\\n")
    if mode == "honest":
        pr["isDraft"] = False
    elif mode == "movehead":
        pr["isDraft"] = False
        pr["headRefOid"] = "%s"
    elif mode == "noop":
        pass  # the fault under test: the PR stays a draft
    save()
    sys.exit(0)

# Defensive no-ops for anything the finalize path does not use.
if argv[:2] == ["issue", "view"]:
    print(json.dumps({"number": pr.get("number"), "state": "OPEN", "body": ""}))
    sys.exit(0)
if argv[:1] == ["api"]:
    print("demo-user")
    sys.exit(0)
sys.exit(0)
''' % OTHER_HEAD


def _git(root: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, check=False)


def _throwaway_repo(holder: str) -> tuple[str, str]:
    """A clean, committed git copy of this repo. Returns (copy_root, head_sha). The coordinator is invoked
    from the copy, so its ROOT — and every git/gh/StableCommit read — resolves to the copy, never this tree."""
    copy = os.path.join(holder, "repo")
    shutil.copytree(validate.ROOT, copy, symlinks=True,
                    ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", "*.pyc", ".pytest_cache"))
    _git(copy, "init", "-q", "-b", "main")
    _git(copy, "add", "-A")
    _git(copy, "-c", "user.email=e@x", "-c", "user.name=n", "commit", "-q", "-m", "seed (copy of this repo)")
    head = _git(copy, "rev-parse", "HEAD").stdout.strip()
    return copy, head


def _write_fake_gh(holder: str) -> str:
    """Write the fake `gh` and return the bin dir to prepend to PATH."""
    bin_dir = os.path.join(holder, "bin")
    os.makedirs(bin_dir, exist_ok=True)
    gh = os.path.join(bin_dir, "gh")
    with open(gh, "w", encoding="utf-8") as fh:
        fh.write(_FAKE_GH)
    os.chmod(gh, os.stat(gh).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


def _ready_state(head: str) -> dict:
    """A schema-true build snapshot seeded to the submission-ready gate for the fixture plan, with every
    commit-bound receipt pinned to the copy's HEAD. `submit apply` runs the real readiness gate over this."""
    required_preflights = [x["id"] for x in bc._protocol()["preflights"] if x["required"]]
    return {
        "schema_version": "build-state.v2", "revision": 1,
        "build": {"repository": REPO, "pr": PR, "base_at_bind": head, "mode": "same-session"},
        "plan": {"plan_id": "pln_0000000d0959", "sealed_digest": "sha256:" + "0" * 64,
                 "diverged_from_seal": False, "digest": bc._digest(FIXTURE_PLAN),
                 "intent_digest": bc._digest(FIXTURE_PLAN["raw_intent"].encode()),
                 "spec_digest": None, "authorizing_issue": None, "profile": "trivial",
                 "bound_head": head},
        "approval": {"plan_digest": bc._digest(FIXTURE_PLAN), "spec_digest": None, "depth": "quick"},
        # One review stage. The plan stage moved to the Project Manager with the panel, and
        # build-state.v2 forbids the key outright — this fixture still carried `plan` after that move
        # and was refused at seeding the moment it ran, which is how the demonstration found the break.
        "reviews": {"deliverable": bc._empty_review()},
        "findings": [], "checkpoint": None,
        "progress": {"current_item": None, "completed": [{"id": "W1", "commit": head}]},
        "work": {"W1": {"attempt_count": 1, "claim": None, "latest_result": None,
                        "latest_failure": None,
                        "integration": {"attempt_id": "0" * 32, "commit": head,
                                        "focused_verification": "fixture only"}}},
        "validation": {"commit": head, "results": [
            {"id": "self-test", "commit": head, "passed": True,
             "summary": "seeded green for the finalize-transition fixture"}]},
        "repair": None,
        "preflights": [{"id": pid, "commit": head, "passed": True,
                        "summary": "seeded green for the finalize-transition fixture"}
                       for pid in required_preflights],
        "pr_contract": {"commit": head, "body_digest": bc._digest(BODY.encode()), "complete": True},
        "submission": "draft",
        "checkout_snapshot": None,
    }


def _seed_state(head: str) -> str:
    """Write the ready-state snapshot to a fresh path under the OS temp dir (StateStore requires that)."""
    fd, path = tempfile.mkstemp(prefix="finalize-demo-state-", suffix=".json")
    os.close(fd)
    os.unlink(path)  # StateStore.create refuses a pre-existing file
    bc.StateStore(path).create(_ready_state(head))
    return path


def _run_submit_apply(copy: str, state_path: str, plan_path: str, bin_dir: str,
                      pr_state_path: str, calls_path: str, mode: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
    env["DEMO_PR_STATE"] = pr_state_path
    env["DEMO_PR_CALLS"] = calls_path
    env["DEMO_READY_MODE"] = mode
    env.pop("GITHUB_EVENT_PATH", None)  # keep the run offline
    tool = os.path.join(copy, ".engine", "tools", "build_coordinator.py")
    return subprocess.run(
        [sys.executable, tool, "--state", state_path, "submit", "apply", "--plan", plan_path],
        cwd=os.path.join(copy, ".engine"), capture_output=True, text=True, env=env)


def _scenario(copy: str, head: str, plan_path: str, bin_dir: str, holder: str, *,
              initial_draft: bool, mode: str) -> tuple[subprocess.CompletedProcess, dict, list[str]]:
    """Run one finalize transition. Returns (process, final snapshot, gh-ready call log)."""
    pr_state_path = os.path.join(holder, f"pr-{mode}-{initial_draft}.json")
    calls_path = os.path.join(holder, f"calls-{mode}-{initial_draft}.log")
    with open(pr_state_path, "w") as fh:
        json.dump({"number": PR, "state": "OPEN", "isDraft": initial_draft,
                   "headRefOid": head, "baseRefOid": head, "mergeable": "MERGEABLE", "body": BODY}, fh)
    open(calls_path, "w").close()
    state_path = _seed_state(head)
    proc = _run_submit_apply(copy, state_path, plan_path, bin_dir, pr_state_path, calls_path, mode)
    with open(state_path) as fh:
        final = json.load(fh)
    with open(calls_path) as fh:
        calls = [line.strip() for line in fh if line.strip()]
    return proc, final, calls


def _pass(label: str, ok: bool, detail: str) -> bool:
    print(f"      [{'PASS' if ok else 'FAIL'}] {label}: {detail}")
    return ok


def main(_argv=None) -> int:
    print("What this checks: the finalize transition marks a draft PR ready and refuses to report success")
    print("until GitHub confirms it — proven against a fake GitHub, with two negative controls. (#959)\n")
    with tempfile.TemporaryDirectory(prefix="finalize-demo-") as holder:
        copy, head = _throwaway_repo(holder)
        bin_dir = _write_fake_gh(holder)
        plan_path = os.path.join(holder, "plan.json")
        with open(plan_path, "w", encoding="utf-8") as fh:
            json.dump(FIXTURE_PLAN, fh)

        results = []

        print("(1) Honest ready — `gh pr ready` un-drafts the PR; submit apply confirms and reports ready.")
        proc, final, calls = _scenario(copy, head, plan_path, bin_dir, holder, initial_draft=True, mode="honest")
        ok = _pass("exit status", proc.returncode == 0, f"returncode={proc.returncode}")
        ok &= _pass("recorded submission", final["submission"] == "ready", repr(final["submission"]))
        ok &= _pass("gh pr ready was called once", calls == ["ready"], repr(calls))
        results.append(ok)
        print()

        print("(2) Idempotent re-run — the PR is already ready; submit apply records ready without re-calling.")
        proc, final, calls = _scenario(copy, head, plan_path, bin_dir, holder, initial_draft=False, mode="honest")
        ok = _pass("exit status", proc.returncode == 0, f"returncode={proc.returncode}")
        ok &= _pass("recorded submission", final["submission"] == "ready", repr(final["submission"]))
        ok &= _pass("gh pr ready was NOT called", calls == [], repr(calls))
        results.append(ok)
        print()

        print("(3) NEGATIVE CONTROL — `gh pr ready` is a no-op; the PR stays draft. Completion must be refused.")
        proc, final, calls = _scenario(copy, head, plan_path, bin_dir, holder, initial_draft=True, mode="noop")
        ok = _pass("fails non-zero", proc.returncode != 0, f"returncode={proc.returncode}")
        ok &= _pass("did NOT record ready", final["submission"] != "ready", repr(final["submission"]))
        ok &= _pass("drove the PR back to draft", final["submission"] == "draft", repr(final["submission"]))
        results.append(ok)
        print()

        print("(4) NEGATIVE CONTROL — the head moves during the transition; changed evidence must be refused.")
        proc, final, calls = _scenario(copy, head, plan_path, bin_dir, holder, initial_draft=True, mode="movehead")
        ok = _pass("fails non-zero", proc.returncode != 0, f"returncode={proc.returncode}")
        ok &= _pass("did NOT record ready", final["submission"] != "ready", repr(final["submission"]))
        ok &= _pass("drove the PR back to draft", final["submission"] == "draft", repr(final["submission"]))
        results.append(ok)
        print()

    passed = all(results)
    if passed:
        print("In plain words: the finalize transition marked the draft ready and only reported success once")
        print("GitHub confirmed it; it re-ran without re-marking; and when GitHub did NOT confirm — a no-op")
        print("ready, or a head that moved — it refused, failed non-zero, and put the PR back to draft. So a")
        print("build can never report completion while its pull request is still a draft. (#959)")
    else:
        print("This run did NOT confirm the finalize guarantee. That is a real signal worth investigating,")
        print("not a pass. No real repository or pull request was touched.")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
