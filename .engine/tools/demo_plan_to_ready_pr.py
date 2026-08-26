#!/usr/bin/env python3
"""Demo — a plan goes from written, to sealed, to built, to a pull request ready for you to merge.

This is the new front door, run end to end for the first time. Two arcs:

  ARC 1 — the ordinary one. A plan is written into the Project Manager, read whole, approved with a
  care level, sealed, and only then handed to a Build. The Build binds to that seal, does the work,
  integrates it, and turns its draft pull request ready. The thing worth seeing is that every one of
  those steps refuses to be skipped: a plan nobody approved cannot be sealed, and a plan nobody sealed
  cannot start a Build.

  ARC 2 — the imported one. You accept a plan the assistant proposed. It arrives as a DRAFT: nothing
  interpreted, nothing decomposed, and four open questions recorded as gaps. That draft cannot be
  sealed and cannot reach a Build until someone actually does the work — which is the whole point of
  importing rather than building straight away. Then it is filled in, approved, sealed, and bound, so
  the arc ends where arc 1 begins: a real Build, running on a real seal.

How it runs, and why it is trustworthy. Everything happens inside a THROWAWAY COPY of this repository
with its own throwaway plan library, so no command can pass by leaning on this instance's own state —
and nothing here can touch your real plans, your real repository, or a real pull request. The Plan
Coordinator and the Build Coordinator are both invoked as real subprocesses rooted in that copy.

Two things are stood in for, and both are named rather than hidden:

  * GITHUB. A tiny fake `gh` models one pull request in a JSON file. CI cannot reach GitHub and must
    never mutate a real pull request, so the boundary the coordinator shells out to is faked — the same
    seam its own tests stub, and the same one demo_959_finalize_ready_transition.py uses.
  * THE SUBMISSION ACCOUNTING. Reaching the ready gate honestly needs a full validation run (the CI
    suite and the self-tests, minutes of work) plus preflight results and a composed pull-request body.
    Those are seeded, exactly as demo_959 seeds them, because they are that demonstration's subject and
    this one's is the ENTRY DOOR. What is not seeded is everything from `init` to `work integrate`:
    that entire chain is the real tools, refusing and succeeding on their own terms.

Run: uv run --directory .engine -- python tools/demo_plan_to_ready_pr.py
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
import build_coordinator as bc  # noqa: E402 — the real coordinator, for schema-true seeding only
import validate                  # noqa: E402 — locates this repo's root (validate.ROOT) to copy

DEMO_CONSENT = ("A demonstration stands in for the operator here; this text is what a real "
                "operator would have typed, and the engine records it verbatim.")
REPO = "owner/entry-door-demo"
PR = 4
BODY = "Entry-door demo fixture body — the exact PR contract the ready read-back pins."

_FAKE_GH = '''#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
state_path = os.environ["DEMO_PR_STATE"]
with open(state_path) as fh:
    pr = json.load(fh)

if argv[:2] == ["pr", "view"]:
    fields = argv[argv.index("--json") + 1].split(",") if "--json" in argv else []
    print(json.dumps({k: pr[k] for k in fields if k in pr} if fields else pr))
    sys.exit(0)

if argv[:2] == ["pr", "ready"]:
    pr["isDraft"] = "--undo" in argv
    with open(state_path, "w") as fh:
        json.dump(pr, fh)
    sys.exit(0)

# Everything else the coordinator may try (labels, edits, api reads) is a no-op here: this demo is
# about the entry door, and a fake that failed on an unrelated call would look like a real refusal.
print("{}")
sys.exit(0)
'''


def _work_item(node_id="W1"):
    return {
        "id": node_id, "description": "Add the widget cache and its tests.",
        "paths": [".engine/tools/widget_cache.py"], "verification": ["Run the widget-cache tests."],
        "depends_on": [], "exclusive_resources": [], "executor_class": "integrator",
        "output_contract": {"deliverable": "The widget cache and its tests",
                            "artifact_kinds": ["integrated-commit"],
                            "required_evidence": ["changed_paths", "verification_results"]},
    }


def _payload():
    return {
        "schema_version": "build-plan.v2", "profile": "trivial",
        "intent_source": {"kind": "direct"},
        "raw_intent": "Cache the widgets; looking them up is slow.",
        "objective": "Add a small widget cache so repeated lookups stop hitting the store.",
        "success_obligations": [{"outcome": "Repeated widget lookups are served from the cache.",
                                 "verification": "The widget-cache tests cover a hit and a miss."}],
        "spec": {"posture": "none",
                 "selection_basis": "No settled specification governs the widget cache.",
                 "disclosure": "There is no settled specification for this change."},
        "parallelism": {"mode": "serial", "max_concurrency": 1},
        "work_items": [_work_item()],
    }


def _document(plan_id, title, revision=1, payload=None, **over):
    document = {
        "schema_version": "engine-plan.v1", "plan_id": plan_id, "title": title,
        "revision": revision, "created_at": "2026-08-24T00:00:00Z",
        "revised_at": "2026-08-24T00:00:00Z",
        "revision_note": "The plan as written.",
        "intent": {"raw": "Cache the widgets; looking them up is slow.",
                   "interpretation": "Add a small cache in front of the widget store.",
                   "source": {"kind": "direct"}},
        "deliberation": {
            "problem_frame": "Widget lookups repeat constantly and each one hits the store.",
            "case_against": "A cache is a second place the truth lives, and a stale entry is worse "
                            "than a slow lookup.",
            "alternatives": [{"option": "Make the store faster instead", "disposition": "rejected",
                              "reason": "The cost is the round trip, not the store."}],
            "failure_modes": ["A stale entry is served after the widget changes."],
            "unresolved_decisions": [],
        },
        "build_plan": payload if payload is not None else _payload(),
    }
    document.update(over)
    return document


def _git(root, *args):
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, check=False)


def _throwaway(holder):
    """A committed git copy of this repo, a throwaway plan library, and a fake `gh` on PATH."""
    copy = os.path.join(holder, "repo")
    shutil.copytree(validate.ROOT, copy, symlinks=True,
                    ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", "*.pyc", ".pytest_cache"))
    _git(copy, "init", "-q", "-b", "main")
    _git(copy, "add", "-A")
    _git(copy, "-c", "user.email=e@x", "-c", "user.name=n", "commit", "-q", "-m", "seed (copy of this repo)")
    head = _git(copy, "rev-parse", "HEAD").stdout.strip()

    bin_dir = os.path.join(holder, "bin")
    os.makedirs(bin_dir, exist_ok=True)
    gh = os.path.join(bin_dir, "gh")
    with open(gh, "w", encoding="utf-8") as fh:
        fh.write(_FAKE_GH)
    os.chmod(gh, os.stat(gh).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    pr_state = os.path.join(holder, "pr.json")
    with open(pr_state, "w", encoding="utf-8") as fh:
        json.dump({"number": PR, "state": "OPEN", "isDraft": True, "headRefOid": head,
                   "baseRefOid": head, "mergeable": "MERGEABLE", "body": BODY,
                   "statusCheckRollup": [{"name": "engine-ci", "status": "COMPLETED",
                                          "conclusion": "SUCCESS",
                                          "completedAt": "2026-08-25T00:00:00Z"}]}, fh)

    env = dict(os.environ)
    env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
    env["DEMO_PR_STATE"] = pr_state
    env["ENGINE_PLAN_DIR"] = os.path.join(holder, "plans")
    env.pop("GITHUB_EVENT_PATH", None)
    return copy, head, env, pr_state


def _tool(copy, name, env, *args):
    return subprocess.run([sys.executable, os.path.join(copy, ".engine", "tools", name), *args],
                          cwd=os.path.join(copy, ".engine"), capture_output=True, text=True, env=env)


def _plan_cmd(copy, env, *args):
    return _tool(copy, "project_manager.py", env, *args)


def _build_cmd(copy, env, state_path, *args):
    return _tool(copy, "build_coordinator.py", env, "--state", state_path, *args)


def _pass(label, ok, detail):
    print(f"      [{'PASS' if ok else 'FAIL'}] {label}: {detail}")
    return ok


def _write(path, value):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(value, fh)
    return path


def _seed_submission(state_path, head, plan_digest):
    """Seed the submission accounting demo_959 owns: validation, preflights, the composed body."""
    required = [x["id"] for x in bc._protocol()["preflights"] if x["required"]]
    store = bc.StateStore(state_path)
    state = store.read()

    def fill(current):
        # Split-shaped, with the imported final proof: the ready gate now demands both halves, and
        # a legacy single-slot seed would honestly park at the final-validation rung.
        current["validation"] = {
            "candidate": {
                "commit": head, "merge_base": head,
                "protocol_digest": bc._digest(b"entry-door-protocol"),
                "argv_digests": {"self-test": bc._digest(b"entry-door-argv")},
                "inventory_digest": "fixture-inventory", "run_record": None,
                "results": [{"id": "self-test", "commit": head, "passed": True,
                             "summary": "seeded green — the validation run is demo_959's subject, "
                                        "not this one's"}]},
            "final": {"commit": head, "source": "ci-import", "run_id": 1,
                      "context": "engine-ci", "tree": "0" * 40}}
        current["preflights"] = [{"id": pid, "commit": head, "passed": True,
                                  "summary": "seeded green for the entry-door fixture"}
                                 for pid in required]
        current["pr_contract"] = {"commit": head, "body_digest": bc._digest(BODY.encode()),
                                  "complete": True}

    store.mutate(fill, from_revision=state["revision"])
    return plan_digest


def _arc_one(copy, head, env, pr_state, holder):
    print("  ARC 1 — a plan written here, sealed here, and built into a pull request ready to merge.\n")
    ok = True
    plan_id = "pln_" + "1" * 12
    document = _write(os.path.join(holder, "plan-doc.json"), _document(plan_id, "Cache the widgets"))
    payload = _write(os.path.join(holder, "payload.json"), _payload())

    created = _plan_cmd(copy, env, "init", "--document", document)
    ok &= _pass("the plan is on the shelf", created.returncode == 0, created.stdout.strip().split("\n")[0])

    early = _plan_cmd(copy, env, "seal", plan_id, "--delta-judgment", "none",
                           "--operator-decision", DEMO_CONSENT)
    ok &= _pass("cannot seal what nobody approved", early.returncode != 0,
                "the seal refuses: " + (early.stdout + early.stderr).strip().splitlines()[-1][:96])

    state_path = os.path.join(tempfile.mkdtemp(prefix="entry-door-state-"), "state.json")
    unsealed = _build_cmd(copy, env, state_path, "plan", "bind", "--plan", plan_id,
                          "--repository", REPO, "--pr", str(PR),
                          "--operator-decision", DEMO_CONSENT)
    ok &= _pass("cannot build what nobody sealed", unsealed.returncode != 0,
                "bind refuses: " + (unsealed.stdout + unsealed.stderr).strip().splitlines()[-1][:96])

    # The panel move, shown rather than asserted — on a SEPARATE plan, because the care level a plan is
    # reviewed at is not something it can back out of afterwards. The rule that used to live on the Build
    # side (every reviewer the chosen level calls for must actually have reviewed) is now a condition of
    # the SEAL: ask for the most thorough level, hand back a review one reviewer did, and the seal refuses
    # and names who is missing. That is what makes "a sealed plan is a reviewed plan" true rather than
    # assumed, and it is why the Build side can stop asking.
    gate_id = "pln_" + "9" * 12
    gate_doc = _write(os.path.join(holder, "gate-doc.json"), _document(gate_id, "Rotate the log keys"))
    _plan_cmd(copy, env, "init", "--document", gate_doc)
    _plan_cmd(copy, env, "preview", gate_id)
    _plan_cmd(copy, env, "approve", gate_id, "--depth", "thorough",
                  "--operator-decision", DEMO_CONSENT)
    packet = _plan_cmd(copy, env, "review", "packet", gate_id)
    # The PACKET digest, not the plan digest that precedes it in the same header — a receipt has to name
    # what the reviewer actually read, which is why `review record` re-renders and compares.
    digest = next((line.split(":", 1)[1].strip() for line in packet.stdout.splitlines()
                   if line.startswith("Packet digest:")), "")
    # `--delivered-effort` is required now: a review record has to say what its panel actually ran at,
    # checked against the depth the operator approved. The demo stands in for a reviewer that met it.
    recorded = _plan_cmd(copy, env, "review", "record", gate_id, "--packet-digest", digest,
                         "--lens", "architecture", "--delivered-effort", "high")
    ok &= _pass("one reviewer's review is recorded", recorded.returncode == 0,
                "architecture read that plan; the others its level calls for did not")
    short = _plan_cmd(copy, env, "seal", gate_id, "--delta-judgment", "none",
                           "--operator-decision", DEMO_CONSENT)
    ok &= _pass("cannot seal a thorough plan one reviewer looked at", short.returncode != 0,
                "the seal refuses: " + (short.stdout + short.stderr).strip().splitlines()[-1][:96])
    dodge = _plan_cmd(copy, env, "approve", gate_id, "--depth", "quick",
                      "--operator-decision", DEMO_CONSENT)
    ok &= _pass("and cannot dodge that by asking for less care", dodge.returncode != 0,
                "re-approving lower would leave the half-finished review attached to a smaller question")

    _plan_cmd(copy, env, "preview", plan_id)
    approved = _plan_cmd(copy, env, "approve", plan_id, "--depth", "quick",
                         "--operator-decision", DEMO_CONSENT)
    ok &= _pass("approved, after the whole plan was rendered", approved.returncode == 0,
                "care level: quick — your own read, no cold reviewers")

    sealed = _plan_cmd(copy, env, "seal", plan_id, "--delta-judgment", "none",
                           "--operator-decision", DEMO_CONSENT)
    ok &= _pass("sealed", sealed.returncode == 0, "the plan is now read-only and can start a Build")

    # THE PHASE BARRIER, and why this walk-through crosses it with the override rather than for real.
    # A real operator seals, then leaves the planning context — /compact, /clear, or a fresh session —
    # and chooses the model and effort for the build phase before binding. Bind REFUSES until it
    # carries that choice, so the demo states one. This is the whole ceremony, not a way around it:
    # there is no override, and a walk-through that could not answer would be a walk-through that
    # cannot bind.
    bound = _build_cmd(copy, env, state_path, "plan", "bind", "--plan", plan_id,
                       "--repository", REPO, "--pr", str(PR),
                       "--operator-decision", DEMO_CONSENT,
                       "--session-model", "demo-model", "--session-effort", "medium")
    ok &= _pass("the Build binds to that seal", bound.returncode == 0,
                "the Build is anchored to the sealed plan, not to a document handed over in chat")
    ok &= _pass("answering the plan-to-build hand-back to get there", bound.returncode == 0,
                "bind refuses until it carries the model and effort for the BUILD, which is what "
                "makes crossing a visible act rather than a silent one")

    # The Build records the depth the plan was approved at, against the payload it is executing. The
    # DECISION was made once, on the plan side, with the whole plan rendered; this is the Build writing
    # that decision into its own evidence, not a second time of asking.
    gate = _build_cmd(copy, env, state_path, "approve", "--plan", payload, "--depth", "quick")
    ok &= _pass("the Build records the approved care level", gate.returncode == 0,
                "quick — the same level the plan was approved at, carried across")

    claim = _build_cmd(copy, env, state_path, "work", "claim", "--item", "W1",
                       "--provider", "claude", "--plan", payload, "--worktree", copy)
    attempt = json.loads(claim.stdout)["attempt_id"] if claim.returncode == 0 else ""
    result = _write(os.path.join(holder, "w1-result.json"),
                    {"outcome": "returned", "base_sha": head,
                     "evidence": {"changed_paths": [".engine/tools/widget_cache.py"],
                                  "verification_results": ["The widget-cache tests pass."]}})
    _build_cmd(copy, env, state_path, "work", "result", "--item", "W1", "--attempt", attempt,
               "--plan", payload, "--input", result)
    integrated = _build_cmd(copy, env, state_path, "work", "integrate", "--item", "W1",
                            "--attempt", attempt, "--commit", head,
                            "--verification-input", "The widget-cache tests pass at this commit.")
    ok &= _pass("the work is integrated", integrated.returncode == 0,
                "one node, done and proven on the branch")

    _seed_submission(state_path, head, json.loads(bound.stdout)["plan_digest"] if bound.returncode == 0 else "")
    submitted = _build_cmd(copy, env, state_path, "submit", "apply", "--plan", payload)
    with open(pr_state, encoding="utf-8") as fh:
        final = json.load(fh)
    ok &= _pass("the pull request is ready for you", submitted.returncode == 0 and not final["isDraft"],
                f"exit {submitted.returncode}; draft={final['isDraft']}")
    return ok


def _arc_two(copy, head, env, holder, pr_state):
    print("\n  ARC 2 — a plan you ACCEPTED, imported as a draft, and only then made real.\n")
    ok = True
    # Arc 1 legitimately turned the fixture pull request ready, and a Build binds only to a DRAFT.
    # Reset it, so arc 2 starts where a second Build really would rather than tripping over arc 1.
    with open(pr_state, encoding="utf-8") as fh:
        pr = json.load(fh)
    pr["isDraft"] = True
    with open(pr_state, "w", encoding="utf-8") as fh:
        json.dump(pr, fh)
    native = os.path.join(holder, "native.md")
    with open(native, "w", encoding="utf-8") as fh:
        fh.write("# Cache the widgets\n\nLooking them up is slow, so cache them.\n")

    imported = _plan_cmd(copy, env, "import-native", "--input", native,
                         "--provenance", "Accepted plan, imported at plan-exit.")
    ok &= _pass("accepted, and imported as a draft", imported.returncode == 0,
                imported.stdout.strip().splitlines()[0] if imported.returncode == 0 else "refused")
    plan_id = imported.stdout.split()[1] if imported.returncode == 0 else ""

    refused = _plan_cmd(copy, env, "seal", plan_id, "--delta-judgment", "none",
                           "--operator-decision", DEMO_CONSENT)
    message = (refused.stdout + refused.stderr)
    ok &= _pass("the draft cannot be sealed", refused.returncode != 0,
                "nothing was interpreted or decomposed, and the seal says so")
    ok &= _pass("the gaps are named as gaps", "unresolved" in message and "imported native plan" in message,
                "four open questions, and an empty payload nobody may pretend is a plan")

    real = _document(plan_id, "Cache the widgets", revision=2)
    real["revision_note"] = "Interpreted, deliberated and decomposed by hand — the work the import declined to fake."
    document = _write(os.path.join(holder, "imported-real.json"), real)
    revised = _plan_cmd(copy, env, "revise", plan_id, "--document", document, "--expect-revision", "1")
    ok &= _pass("filled in by hand, as revision 2", revised.returncode == 0,
                "the import was groundwork; this is the plan")

    _plan_cmd(copy, env, "preview", plan_id)
    _plan_cmd(copy, env, "approve", plan_id, "--depth", "quick",
              "--operator-decision", DEMO_CONSENT)
    sealed = _plan_cmd(copy, env, "seal", plan_id, "--delta-judgment", "none",
                           "--operator-decision", DEMO_CONSENT)
    ok &= _pass("now it seals", sealed.returncode == 0, "approved at a care level, then locked")

    state_path = os.path.join(tempfile.mkdtemp(prefix="entry-door-arc2-"), "state.json")
    # Same as arc 1: the bind states what the build phase runs on, because bind refuses without it.
    bound = _build_cmd(copy, env, state_path, "plan", "bind", "--plan", plan_id,
                       "--repository", REPO, "--pr", str(PR),
                       "--operator-decision", DEMO_CONSENT,
                       "--session-model", "demo-model", "--session-effort", "medium")
    ok &= _pass("and drives a running Build", bound.returncode == 0,
                "the arc ends where arc 1 began: a Build anchored to a seal")
    return ok


def main(_argv=None) -> int:
    print("What this checks: a plan cannot be sealed before it is approved, cannot start a Build before")
    print("it is sealed, and — once it is — carries all the way to a pull request ready for you.\n")
    holder = tempfile.mkdtemp(prefix="entry-door-demo-")
    try:
        copy, head, env, pr_state = _throwaway(holder)
        ok = _arc_one(copy, head, env, pr_state, holder)
        ok &= _arc_two(copy, head, env, holder, pr_state)
        print("\n  Every command above ran inside a throwaway copy with its own throwaway plan library.")
        print("  Your plans, this repository and any real pull request were never touched.")
        if not ok:
            print("\nDEMO FAILED: a step that should have been refused was allowed, or the end-to-end "
                  "arc did not complete.", file=sys.stderr)
            return 1
        return 0
    finally:
        shutil.rmtree(holder, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
