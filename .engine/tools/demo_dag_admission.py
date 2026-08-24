#!/usr/bin/env python3
"""Demo — the scheduler refuses work that is not ready, and advances the item that unblocks the most.

Three worries a non-engineer cannot check by reading code, answered by running the real coordinator:

  1. *If the work is a graph rather than a list, can a session skip ahead?* Start a piece whose
     prerequisite is not finished and the coordinator refuses it, naming which prerequisite and why.
  2. *Can a build report itself finished while a piece was never actually integrated?* Final validation
     refuses while any node is unintegrated. "All the tests passed" must not be evidence about a graph
     that is not built.
  3. *When something finishes and unblocks two pieces, which one comes next?* The one carrying the
     longer remaining chain behind it — the piece whose slipping would move the end date — not whichever
     happens to be first in the file.

How it runs, and why it is trustworthy. Every scenario invokes the REAL `build_coordinator.py` as a
subprocess ROOTED IN A THROWAWAY COPY of this repository, so its `ROOT` — and every git read it makes —
resolves to the copy and never to this tree. Nothing about the refusals is mocked: the real frontier
computation, the real claim gate and the real validation gate all run. No network is touched at all,
because none of these three questions involves GitHub.

What IS seeded rather than re-demonstrated: the build snapshot arrives already bound and approved, with
a four-node plan. Getting there needs a draft pull request and a sealed plan, which
`demo_plan_to_ready_pr.py` demonstrates end to end and `test_build_coordinator.py` covers exhaustively.
Here the point is the graph, so the graph is what runs.

The plan below is deliberately shaped so the answers are not accidents:

    root ──▶ deep ──▶ under        `deep` unblocks one further node; `flat` unblocks nothing.
      └───▶ flat                   Both become ready together, and their order is therefore a decision.

Run: uv run --directory .engine -- python tools/demo_dag_admission.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_coordinator as bc  # noqa: E402 — the real coordinator, for schema-true seeding only
import validate                  # noqa: E402 — locates this repo's root (validate.ROOT) to copy

REPO = "owner/admission-demo"
PR = 11
PLAN_ID = "pln_" + "a" * 12
SEALED = "sha256:" + "5" * 64


def _node(node_id, deps):
    return {
        "id": node_id, "description": f"Build {node_id}.",
        "paths": [f".engine/tools/{node_id}.py"], "verification": [f"Run the {node_id} tests."],
        "depends_on": list(deps), "exclusive_resources": [], "executor_class": "integrator",
        "output_contract": {"deliverable": f"{node_id} and its tests",
                            "artifact_kinds": ["integrated-commit"],
                            "required_evidence": ["changed_paths", "verification_results"]},
    }


FIXTURE_PLAN = {
    "schema_version": "build-plan.v2",
    "profile": "normal",
    "intent_source": {"kind": "direct"},
    "raw_intent": "Show that the scheduler refuses work that is not ready.",
    "interpretation": "A four-node graph whose admission order is a decision rather than an accident.",
    "objective": "Demonstrate the frontier refusal, the completion gate, and critical-path admission.",
    "success_obligations": [{"outcome": "The three refusals bite.",
                             "verification": "This demonstration runs them."}],
    "scope_boundary": [".engine/tools/"],
    "non_goals": ["Anything involving GitHub."],
    "risks": ["None — nothing here leaves the throwaway copy."],
    "evidence": [], "assumptions": [],
    "review_strategy": "Covered by the coordinator's own tests; this is the operator-visible proof.",
    "spec": {"posture": "none", "selection_basis": "A demonstration touches no settled specification.",
             "disclosure": "No settled specification governs this demonstration."},
    "parallelism": {"mode": "serial", "max_concurrency": 1},
    "work_items": [_node("root", []), _node("deep", ["root"]),
                   _node("under", ["deep"]), _node("flat", ["root"])],
}


def _git(root: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, check=False)


def _throwaway_repo(holder: str) -> tuple[str, str]:
    """A clean, committed git copy of this repo. Returns (copy_root, head_sha). The coordinator is invoked
    from the copy, so its ROOT and every git read resolve to the copy, never to this tree."""
    copy = os.path.join(holder, "repo")
    shutil.copytree(validate.ROOT, copy, symlinks=True,
                    ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", "*.pyc", ".pytest_cache"))
    _git(copy, "init", "-q", "-b", "main")
    _git(copy, "add", "-A")
    _git(copy, "-c", "user.email=e@x", "-c", "user.name=n", "commit", "-q", "-m", "seed (copy of this repo)")
    return copy, _git(copy, "rev-parse", "HEAD").stdout.strip()


def _seed_state(head: str) -> str:
    """A schema-true snapshot, bound and approved against the fixture plan, with nothing yet integrated."""
    state = {
        "schema_version": "build-state.v2", "revision": 1,
        "build": {"repository": REPO, "pr": PR, "base_at_bind": head, "mode": "same-session"},
        "plan": {"plan_id": PLAN_ID, "sealed_digest": SEALED, "diverged_from_seal": False,
                 "digest": bc._digest(FIXTURE_PLAN),
                 "intent_digest": bc._digest(FIXTURE_PLAN["raw_intent"].encode()),
                 "spec_digest": None, "authorizing_issue": None,
                 "profile": "normal", "bound_head": head},
        "approval": {"plan_digest": bc._digest(FIXTURE_PLAN), "spec_digest": None, "depth": "quick"},
        "reviews": {"deliverable": bc._empty_review()},
        "findings": [], "checkpoint": None, "progress": {"current_item": None, "completed": []},
        "validation": None, "repair": None, "repair_rounds": [], "plan_change_escalations": [],
        "reconciles": [], "preflights": [], "pr_contract": None, "submission": "draft",
        "checkout_snapshot": None, "work": {},
    }
    fd, path = tempfile.mkstemp(prefix="admission-demo-state-", suffix=".json")
    os.close(fd)
    os.unlink(path)                       # StateStore.create refuses a pre-existing file
    bc.StateStore(path).create(state)
    return path


def _coordinator(copy: str, state_path: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("GITHUB_EVENT_PATH", None)    # keep the run offline
    tool = os.path.join(copy, ".engine", "tools", "build_coordinator.py")
    return subprocess.run([sys.executable, tool, "--state", state_path, *args],
                          cwd=os.path.join(copy, ".engine"), capture_output=True, text=True, env=env)


def _pass(label: str, ok: bool, detail: str) -> bool:
    print(f"      [{'PASS' if ok else 'FAIL'}] {label}: {detail}")
    return ok


def main(_argv=None) -> int:
    print("What this checks: work that is not ready is refused, a build cannot call itself finished")
    print("before every piece is integrated, and the piece that unblocks the most goes next.\n")

    holder = tempfile.mkdtemp(prefix="dag-admission-demo-")
    try:
        copy, head = _throwaway_repo(holder)
        plan_path = os.path.join(holder, "plan.json")
        with open(plan_path, "w", encoding="utf-8") as fh:
            json.dump(FIXTURE_PLAN, fh)
        state_path = _seed_state(head)
        ok = True

        print("  (1) Skipping ahead — claim `under`, whose prerequisite `deep` is not finished:")
        skipped = _coordinator(copy, state_path, "work", "claim", "--item", "under",
                               "--provider", "claude", "--plan", plan_path, "--worktree", copy)
        message = (skipped.stderr + skipped.stdout).strip()
        ok &= _pass("refused", skipped.returncode != 0, f"exit {skipped.returncode}")
        ok &= _pass("names the item", "under" in message, "the refusal says which piece")
        ok &= _pass("names the prerequisite", "deep" in message,
                    "and which unfinished piece is holding it")

        print("\n  (2) Calling it finished early — validate the whole build with nothing integrated:")
        early = _coordinator(copy, state_path, "validate", "--plan", plan_path)
        early_message = (early.stderr + early.stdout).strip()
        ok &= _pass("refused", early.returncode != 0, f"exit {early.returncode}")
        ok &= _pass("says why", "unintegrated" in early_message,
                    "validation is evidence about a graph that is built, or it is not evidence")

        print("\n  (3) Finishing `root`, which unblocks two pieces at once:")
        claim = _coordinator(copy, state_path, "work", "claim", "--item", "root",
                             "--provider", "claude", "--plan", plan_path, "--worktree", copy)
        ok &= _pass("root was claimable", claim.returncode == 0, f"exit {claim.returncode}")
        attempt = json.loads(claim.stdout)["attempt_id"] if claim.returncode == 0 else ""
        result_path = os.path.join(holder, "root-result.json")
        with open(result_path, "w", encoding="utf-8") as fh:
            json.dump({"outcome": "returned", "base_sha": head,
                       "evidence": {"changed_paths": [".engine/tools/root.py"],
                                    "verification_results": ["The root tests pass."]}}, fh)
        _coordinator(copy, state_path, "work", "result", "--item", "root", "--attempt", attempt,
                     "--plan", plan_path, "--input", result_path)
        integrated = _coordinator(copy, state_path, "work", "integrate", "--item", "root",
                                  "--attempt", attempt, "--commit", head,
                                  "--verification-input", "The root tests pass at this commit.")
        ok &= _pass("integrated", integrated.returncode == 0, f"exit {integrated.returncode}")

        frontier = _coordinator(copy, state_path, "work", "frontier", "--plan", plan_path)
        lines = frontier.stdout.strip().splitlines()
        admitted = next((line for line in lines if "admitted" in line), "")
        print("      the scheduler now says:")
        for line in lines:
            print(f"        {line}")
        ok &= _pass("advances the longer chain first", admitted.strip().endswith("deep"),
                    "`deep` unblocks `under`; `flat` unblocks nothing, so `deep` goes first")
        ok &= _pass("does not seize the choice", any("claimable" in x and "flat" in x for x in lines),
                    "`flat` is still permitted — ranking orders the frontier, it never forbids")

        print("\n  Nothing above touched this repository, GitHub, or any real pull request: every command")
        print("  ran inside a throwaway copy that is deleted when this demonstration ends.")
        if not ok:
            print("\nDEMO FAILED: at least one refusal did not bite, or admission order was wrong.",
                  file=sys.stderr)
            return 1
        return 0
    finally:
        shutil.rmtree(holder, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
