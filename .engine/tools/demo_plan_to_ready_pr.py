#!/usr/bin/env python3
"""Demo — a plan goes from written, to sealed, to built, to a pull request ready for you to merge.

Three independent disposable arcs exercise the entry door and evidence continuity:

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

  ARC 3 — fresh admission refuses stale target ancestry and overlapping work, a scoped decision
  survives an interrupted preparation, and real conflicting and clean rebases preserve the original
  integration history through canonical handoff. A real clean target merge retains completed repair
  receipts only after explicitly labeled synthetic current-head candidate accounting is supplied.

How it runs, and why it is trustworthy. Everything happens inside a THROWAWAY COPY of this repository
with its own throwaway plan library, so no command can pass by leaning on this instance's own state —
and nothing here can touch your real plans, your real repository, or a real pull request. The Plan
Coordinator and the Build Coordinator are both invoked as real subprocesses rooted in that copy.

Four things are stood in for, and each is named rather than hidden:

  * TRANSPORT IDENTITY. A private Git wrapper substitutes only the exact origin-identity query,
    exposing the logical GitHub URL while insteadOf sends actual git transport to a local bare remote.
    All other git commands, including fetch, merge-tree, rebase and merge, run the saved real Git binary.
  * GITHUB. A tiny fake `gh` models the candidate and configurable competing pull requests in JSON. CI cannot reach GitHub and must
    never mutate a real pull request, so the boundary the coordinator shells out to is faked — the same
    seam its own tests stub, and the same one demo_959_finalize_ready_transition.py uses.
  * THE SUBMISSION ACCOUNTING. Reaching the ready gate honestly needs a full validation run (the CI
    suite and the self-tests, minutes of work) plus preflight results and a composed pull-request body.
    Those are seeded, exactly as demo_959 seeds them, because they are that demonstration's subject and
    this one's is the ENTRY DOOR.
  * THE REVIEWER. Synthetic launch, packet-read and completed-output events in the throwaway library
    stand in for reviewers. The real review acceptance commands consume them; this demonstration
    does not qualify any runtime's native agent execution. All entry and integration commands remain
    the real tools, refusing and succeeding on their own terms.

Run: uv run --directory .engine -- python tools/demo_plan_to_ready_pr.py
"""
from __future__ import annotations

import io
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_coordinator as bc  # noqa: E402 — the real coordinator, for schema-true seeding only
import validate                  # noqa: E402 — locates this repo's root (validate.ROOT) to copy

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

if argv and argv[0] == "api" and any("/pulls?" in arg for arg in argv):
    own = {"number": pr["number"], "title": pr.get("title", "Demo #12"), "body": pr["body"],
           "head": {"ref": pr["headRefName"], "sha": pr["headRefOid"],
                    "repo": {"full_name": pr["headRepository"]["nameWithOwner"]}}}
    print(json.dumps([[own], pr.get("demo_competitors", [])]))
    sys.exit(0)

if argv[:2] == ["api", "graphql"]:
    number = int(next(arg.split("=", 1)[1] for arg in argv if arg.startswith("number=")))
    row = pr if number == pr["number"] else next(item for item in pr.get("demo_competitors", [])
                                               if item["number"] == number)
    print(json.dumps([{"data": {"repository": {"pullRequest": {"closingIssuesReferences": {
        "nodes": row.get("closingIssuesReferences", []),
        "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}}]))
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


def _demo_env():
    # git -C alone does not override inherited repository/config selectors.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_OPTIONAL_LOCKS="0")
    return env


def _git(root, *args):
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True,
                          check=False, env=_demo_env())


def _copy_ignore(directory, names):
    ignored = shutil.ignore_patterns(".git", ".venv", ".uv", "__pycache__", "*.pyc", ".pytest_cache")(directory, names)
    for name in set(names) - ignored:
        if os.path.islink(os.path.join(directory, name)):
            raise ValueError("demo refuses a source symlink: " + os.path.join(directory, name))
    return ignored


def _throwaway(holder):
    """A committed git copy, a bare remote, and private transport/PR fixture boundaries."""
    os.makedirs(holder, exist_ok=True)
    copy = os.path.join(holder, "repo")
    shutil.copytree(validate.ROOT, copy, symlinks=True,
                    ignore=_copy_ignore)
    _git(copy, "init", "-q", "-b", "main")
    _git(copy, "config", "user.email", "demo@example.invalid")
    _git(copy, "config", "user.name", "Disposable demo")
    _git(copy, "config", "commit.gpgsign", "false")
    _git(copy, "config", "core.hooksPath", os.devnull)
    _git(copy, "add", "-A")
    _git(copy, "-c", "user.email=e@x", "-c", "user.name=n", "commit", "-q", "-m", "seed (copy of this repo)")
    head = _git(copy, "rev-parse", "HEAD").stdout.strip()
    remote = os.path.join(holder, "origin.git")
    _git(copy, "clone", "--bare", copy, remote)
    logical = "https://github.com/" + REPO + ".git"
    _git(copy, "config", "url." + Path(remote).as_uri() + ".insteadOf", logical)
    _git(copy, "remote", "add", "origin", logical)
    _git(copy, "fetch", "origin")
    _git(copy, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    _git(copy, "checkout", "-q", "-b", "codex/demo")

    bin_dir = os.path.join(holder, "bin")
    os.makedirs(bin_dir, exist_ok=True)
    real_git = shutil.which("git")
    if not real_git or not os.path.isabs(real_git):
        raise RuntimeError("demo needs an absolute real git executable before installing its wrapper")
    # `remote get-url` expands insteadOf; this exact identity query reports the logical configured
    # GitHub URL. Fetch/ls-remote and every history command still execute real Git offline.
    git_wrapper = os.path.join(bin_dir, "git")
    with open(git_wrapper, "w", encoding="utf-8") as fh:
        fh.write("#!/usr/bin/env python3\nimport os, sys, subprocess\n"
                 "args = sys.argv[1:]\n"
                 "prefix = args[:2] if len(args) >= 2 and args[0] == '-C' else []\n"
                 "query = args[len(prefix):]\n"
                 "if query == ['remote', 'get-url', 'origin']:\n"
                 "    raise SystemExit(subprocess.call([" + repr(real_git) + ", *prefix, 'config', '--get', 'remote.origin.url']))\n"
                 "os.execv(" + repr(real_git) + ", [" + repr(real_git) + ", *args])\n")
    os.chmod(git_wrapper, 0o700)
    gh = os.path.join(bin_dir, "gh")
    with open(gh, "w", encoding="utf-8") as fh:
        fh.write(_FAKE_GH)
    os.chmod(gh, os.stat(gh).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    pr_state = os.path.join(holder, "pr.json")
    with open(pr_state, "w", encoding="utf-8") as fh:
        json.dump({"number": PR, "state": "OPEN", "isDraft": True, "headRefOid": head,
                   "baseRefOid": head, "baseRefName": "main", "headRefName": "codex/demo",
                   "headRepository": {"nameWithOwner": REPO}, "closingIssuesReferences": [],
                   "mergeable": "MERGEABLE", "body": BODY,
                   "statusCheckRollup": [{"name": "engine-ci", "status": "COMPLETED",
                                          "conclusion": "SUCCESS",
                                          "completedAt": "2026-08-25T00:00:00Z"}]}, fh)

    env = _demo_env()
    env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
    env["DEMO_PR_STATE"] = pr_state
    env["ENGINE_PLAN_DIR"] = os.path.join(holder, "plans")
    env.pop("GITHUB_EVENT_PATH", None)
    identity_query = subprocess.run([git_wrapper, "-C", copy, "remote", "get-url", "origin"],
        capture_output=True, text=True, env=env)
    forwarded_query = subprocess.run([git_wrapper, "-C", copy, "rev-parse", "HEAD"],
        capture_output=True, text=True, env=env)
    expanded_query = subprocess.run([git_wrapper, "-C", copy, "remote", "get-url", "--all", "origin"],
        capture_output=True, text=True, env=env)
    if (identity_query.stdout.strip() != logical or forwarded_query.stdout.strip() != head
            or expanded_query.stdout.strip() != Path(remote).as_uri()):
        raise RuntimeError("fixture Git wrapper must substitute only the exact logical identity query")
    return copy, head, env, pr_state


def _tool(copy, name, env, *args):
    command = [sys.executable, os.path.join(copy, ".engine", "tools", name), *args]
    if name == "build_coordinator.py" and env.get("DEMO_INTERRUPT_PREPARATION"):
        # A disposable crash injection at the persistence seam; the real CLI/parser/admission run.
        code = ("import sys; sys.path.insert(0, " + repr(os.path.join(copy, ".engine", "tools")) + "); "
                "import build_coordinator as b; "
                "b.build_state_store.finish_binding=lambda *a, **k: (_ for _ in ()).throw(OSError('demo interruption after reservation')); "
                "raise SystemExit(b.main(sys.argv[1:]))")
        command = [sys.executable, "-c", code, *args]
    return subprocess.run(command,
                          cwd=os.path.join(copy, ".engine"), capture_output=True, text=True, env=env)


def _plan_cmd(copy, env, *args):
    return _tool(copy, "project_manager.py", env, *args)


def _observe_demo_review(copy, env, plan_id, digest, lens="architecture"):
    """Simulate one reviewer only inside this demonstration's disposable library."""
    script = """
import sys
import plan_store
import project_manager
import scoped_agents
from test_build_coordinator import observe_review_execution
library = plan_store.PlanLibrary(sys.argv[1])
slug = library.resolve(sys.argv[2])
text, digest, contract = project_manager.review_packet(library, slug)
assert digest == sys.argv[3]
observe_review_execution(library, slug, scoped_agents.plan_owner(library.read_record(slug)),
                         sys.argv[4], digest, [], root='demo-review-root',
                         review_contract=contract, packet_content=text)
"""
    return subprocess.run([sys.executable, "-c", script, env["ENGINE_PLAN_DIR"], plan_id, digest, lens],
                          cwd=os.path.join(copy, ".engine", "tools"), capture_output=True,
                          text=True, env=env)


def _build_cmd(copy, env, state_path, *args, ownership=None):
    expected = []
    if ownership is not None:
        # Keep bind's identity; only refresh the revision from its canonical evidence.
        with open(state_path, encoding="utf-8") as fh:
            revision = json.load(fh)["revision"]
        expected = ["--expect-build-id", ownership["build_id"],
                    "--expect-generation", str(ownership["generation"]),
                    "--expect-revision", str(revision)]
    return _tool(copy, "build_coordinator.py", env, "--state", state_path, *expected, *args)


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
                                  "complete": True,
                                  "review_lineage_digest": bc._review_lineage_digest(current)}

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
                           "--operator-decided")
    ok &= _pass("cannot seal what nobody approved", early.returncode != 0,
                "the seal refuses: " + (early.stdout + early.stderr).strip().splitlines()[-1][:96])

    state_path = os.path.join(holder, "arc1-state.json")
    unsealed = _build_cmd(copy, env, state_path, "plan", "bind", "--plan", plan_id,
                          "--repository", REPO, "--pr", str(PR),
                          "--operator-decided")
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
                  "--operator-decided")
    packet = _plan_cmd(copy, env, "review", "packet", gate_id)
    # Fresh approvals carry the packet identity inside their canonical JSON envelope.
    digest = json.loads(packet.stdout)["packet_digest"]
    observed = _observe_demo_review(copy, env, gate_id, digest)
    ok &= _pass("synthetic reviewer events are observed", observed.returncode == 0,
                "disposable event fixture; no native runtime qualification is claimed")
    recorded = _plan_cmd(copy, env, "review", "record", gate_id, "--packet-digest", digest,
                         "--lens", "architecture", "--session", "demo-review-root")
    ok &= _pass("one reviewer's review is recorded", recorded.returncode == 0,
                "the architecture event fixture was accepted; the other required lenses are absent")
    short = _plan_cmd(copy, env, "seal", gate_id, "--delta-judgment", "none",
                           "--operator-decided")
    ok &= _pass("cannot seal a thorough plan one reviewer looked at", short.returncode != 0,
                "the seal refuses: " + (short.stdout + short.stderr).strip().splitlines()[-1][:96])
    dodge = _plan_cmd(copy, env, "approve", gate_id, "--depth", "quick",
                      "--operator-decided")
    ok &= _pass("and cannot dodge that by asking for less care", dodge.returncode != 0,
                "re-approving lower would leave the half-finished review attached to a smaller question")

    _plan_cmd(copy, env, "preview", plan_id)
    approved = _plan_cmd(copy, env, "approve", plan_id, "--depth", "quick",
                         "--operator-decided")
    ok &= _pass("approved, after the whole plan was rendered", approved.returncode == 0,
                "care level: quick — your own read, no cold reviewers")

    sealed = _plan_cmd(copy, env, "seal", plan_id, "--delta-judgment", "none",
                           "--operator-decided")
    ok &= _pass("sealed", sealed.returncode == 0, "the plan is now read-only and can start a Build")

    _update_pr(pr_state, closingIssuesReferences=[{
        "number": 12, "url": "https://github.com/" + REPO + "/issues/12"}])
    bound = _build_cmd(copy, env, state_path, "plan", "bind", "--plan", plan_id,
                       "--repository", REPO, "--pr", str(PR),
                       "--operator-decided")
    ok &= _pass("the Build binds to that seal", bound.returncode == 0,
                "the Build is anchored to the sealed plan, not to a document handed over in chat")

    if bound.returncode != 0:
        return False
    binding = json.loads(bound.stdout)
    state_path = binding["state"]
    ownership = binding["ownership"]
    admission = json.loads(Path(state_path).read_text())["admission"]["material"]
    ok &= _pass("own issue-linked PR binds without an override", admission["issues"] == [12]
        and admission["overlap"]["coverage"] == "complete" and not admission["overlap"]["matches"]
        and admission["override"] is None, "the exact first-bind PR is self-excluded")

    def build(*args):
        return _build_cmd(copy, env, state_path, *args, ownership=ownership)

    # The Build records the depth the plan was approved at, against the payload it is executing. The
    # DECISION was made once, on the plan side, with the whole plan rendered; this is the Build writing
    # that decision into its own evidence, not a second time of asking.
    gate = build("approve", "--plan", payload, "--depth", "quick")
    ok &= _pass("the Build records the approved care level", gate.returncode == 0,
                "quick — the same level the plan was approved at, carried across")

    claim = build("work", "claim", "--item", "W1",
                       "--provider", "claude", "--plan", payload, "--worktree", copy)
    attempt = json.loads(claim.stdout)["attempt_id"] if claim.returncode == 0 else ""
    # The integration is now proven, not asserted: the node is integrator-inline, so the Engine observes
    # the staged candidate tree itself and integration binds a real commit whose receipt names the base,
    # the attributable range, and the changed paths. So the demo does real work — writes the file, stages
    # it, records the Engine-observed identity, commits, and integrates that commit.
    with open(os.path.join(copy, ".engine", "tools", "widget_cache.py"), "w", encoding="utf-8") as fh:
        fh.write("CACHE = {}\n\n\ndef get(key, load):\n    if key not in CACHE:\n        CACHE[key] = load(key)\n    return CACHE[key]\n")
    _git(copy, "add", "-A")
    result = _write(os.path.join(holder, "w1-result.json"),
                    {"outcome": "returned",
                     "evidence": {"changed_paths": [".engine/tools/widget_cache.py"],
                                  "verification_results": [{"command": "Inspect fixture cache source",
                                      "outcome": "passed", "detail": "Cache retains loaded keys; no test runner claimed."}],
                                  "assumptions": [], "unresolved_concerns": []}})
    build("work", "result", "--item", "W1", "--attempt", attempt,
               "--plan", payload, "--input", result)
    _git(copy, "-c", "user.email=e@x", "-c", "user.name=n", "commit", "-q", "-m", "Add the widget cache")
    new_head = _git(copy, "rev-parse", "HEAD").stdout.strip()
    with open(pr_state, encoding="utf-8") as fh:
        pr = json.load(fh)
    pr["headRefOid"] = new_head
    with open(pr_state, "w", encoding="utf-8") as fh:
        json.dump(pr, fh)
    integrated = build("work", "integrate", "--item", "W1",
                            "--attempt", attempt, "--commit", new_head, "--plan", payload,
                            "--verification-input", "Demo fixture source inspected at this commit; no test-run claim.")
    ok &= _pass("the work is integrated", integrated.returncode == 0,
                "one node, done and proven on the branch by an Engine-computed receipt")

    _seed_submission(state_path, new_head, json.loads(bound.stdout)["plan_digest"] if bound.returncode == 0 else "")
    submitted = build("submit", "apply", "--plan", payload)
    with open(pr_state, encoding="utf-8") as fh:
        final = json.load(fh)
    ok &= _pass("the pull request is ready for you", submitted.returncode == 0 and not final["isDraft"],
                f"exit {submitted.returncode}; draft={final['isDraft']}; {submitted.stderr.strip()[:600]}")
    return ok


def _arc_two(copy, head, env, holder, pr_state):
    print("\n  ARC 2 — a plan you ACCEPTED, imported as a draft, and only then made real.\n")
    ok = True
    _update_pr(pr_state, closingIssuesReferences=[{
        "number": 12, "url": "https://github.com/" + REPO + "/issues/12"}])
    native = os.path.join(holder, "native.md")
    with open(native, "w", encoding="utf-8") as fh:
        fh.write("# Cache the widgets\n\nLooking them up is slow, so cache them.\n")

    imported = _plan_cmd(copy, env, "import-native", "--input", native,
                         "--provenance", "Accepted plan, imported at plan-exit.")
    ok &= _pass("accepted, and imported as a draft", imported.returncode == 0,
                imported.stdout.strip().splitlines()[0] if imported.returncode == 0 else "refused")
    plan_id = imported.stdout.split()[1] if imported.returncode == 0 else ""
    imported_records = [json.loads(p.read_text()) for p in Path(env["ENGINE_PLAN_DIR"]).glob("*/record.json")]
    ok &= _pass("native acceptance did not start a Build", not any(
        (record.get("build_lease") or {}).get("current") for record in imported_records),
        "the independent plan library contains drafts, not a Build claim")

    refused = _plan_cmd(copy, env, "seal", plan_id, "--delta-judgment", "none",
                           "--operator-decided")
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
              "--operator-decided")
    sealed = _plan_cmd(copy, env, "seal", plan_id, "--delta-judgment", "none",
                           "--operator-decided")
    ok &= _pass("now it seals", sealed.returncode == 0, "approved at a care level, then locked")

    state_path = os.path.join(holder, "arc2-state.json")
    bound = _build_cmd(copy, env, state_path, "plan", "bind", "--plan", plan_id,
                       "--repository", REPO, "--pr", str(PR),
                       "--operator-decided")
    ok &= _pass("and drives a running Build", bound.returncode == 0,
                "the arc ends where arc 1 began: a Build anchored to a seal")
    return ok


def _update_pr(path, **updates):
    value = json.loads(Path(path).read_text())
    value.update(updates)
    _write(path, value)
    return value


def _publish_head(copy, pr_state, head):
    _require(_git(copy, "push", "--force", "origin", "codex/demo"), "publish disposable Build head")
    return _update_pr(pr_state, headRefOid=head)


def _require(result, label):
    if result.returncode:
        raise RuntimeError(label + " failed: " + (result.stdout + result.stderr)[-4000:])
    return result.stdout


def _commit(copy, path, content, message):
    target = Path(copy) / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    _require(_git(copy, "add", "-A"), message + " stage")
    _require(_git(copy, "commit", "-q", "-m", message), message)
    return _git(copy, "rev-parse", "HEAD").stdout.strip()


def _advance(copy, pr_state, path, content):
    _require(_git(copy, "checkout", "-q", "main"), "target fixture checkout")
    target = _commit(copy, path, content, "Disposable upstream advance")
    _require(_git(copy, "push", "origin", "main"), "publish disposable target")
    _require(_git(copy, "checkout", "-q", "codex/demo"), "restore disposable Build branch")
    _update_pr(pr_state, baseRefOid=target)
    return target


def _normal_payload():
    value = _payload()
    value.update(profile="normal", interpretation="Exercise admission and recovery with real git evidence.",
        evidence=[{"claim": "The fixture owns an offline bare remote.", "basis": "Demo setup", "kind": "observed"}],
        assumptions=[{"claim": "Git history is disposable.", "status": "verified"}],
        scope_boundary=["Disposable widget cache"], non_goals=["Real deployment"],
        risks=["Fixture interruption must preserve its claim."],
        review_strategy="Seed explicitly labeled downstream review accounting for the recovery witness.")
    return value


def _seal_fixture(copy, env, holder, plan_id, payload):
    doc = _write(os.path.join(holder, "recovery-plan.json"), _document(plan_id, "Recover the widget cache", payload=payload))
    for args in (("init", "--document", doc), ("preview", plan_id),
                 ("approve", plan_id, "--depth", "thorough", "--operator-decided")):
        _require(_plan_cmd(copy, env, *args), "seal fixture " + args[0])
    packet = json.loads(_require(_plan_cmd(copy, env, "review", "packet", plan_id), "plan review packet"))
    lenses = ("architecture", "feasibility", "product-intent", "risk-governance")
    for lens in lenses:
        _require(_observe_demo_review(copy, env, plan_id, packet["packet_digest"], lens), "observe plan reviewer fixture")
    _require(_plan_cmd(copy, env, "review", "record", plan_id, "--packet-digest", packet["packet_digest"],
        *[arg for lens in lenses for arg in ("--lens", lens)], "--session", "demo-review-root"), "accept plan reviewer fixture")
    _require(_plan_cmd(copy, env, "present-findings", plan_id, "--operator-decided"), "present empty simulated panel")
    _require(_plan_cmd(copy, env, "seal", plan_id, "--delta-judgment", "none", "--operator-decided"), "seal reviewed fixture")
    return _write(os.path.join(holder, "recovery-payload.json"), payload)


def _observe_demo_build_review(copy, env, state_path, packet, lens):
    """Simulated native events; the caller records them through the actual review command."""
    script = """
import json, sys
import build_coordinator as bc
import plan_store, scoped_agents
from test_build_coordinator import observe_review_execution
state = json.loads(open(sys.argv[1]).read())
packet = json.loads(sys.argv[2])
lens = sys.argv[3]
library = plan_store.PlanLibrary(sys.argv[4])
slug = library.resolve(state['plan']['plan_id'])
contract = next(c for c in packet['reviewer_contracts'] if c['lens'] == lens)
observe_review_execution(library, slug, scoped_agents.build_owner(state), lens,
    contract['lens_packet_digest'], [], root='demo-review-root',
    review_contract=bc.reviewer_contracts.effective_build(state), packet_content=json.dumps(packet))
"""
    return subprocess.run([sys.executable, "-c", script, state_path, json.dumps(packet), lens,
        env["ENGINE_PLAN_DIR"]], cwd=os.path.join(copy, ".engine", "tools"),
        capture_output=True, text=True, env=env)


def _seed_candidate_fixture(state_path, head):
    """Accounting fixture only. This does not represent a validation run or CI evidence."""
    store = bc.StateStore(state_path)
    state = store.read()
    store.mutate(lambda s: s.update(validation={"commit": head, "results": [{
        "id": "fixture-candidate", "commit": head, "passed": True,
        "summary": "DEMO FIXTURE ONLY: synthetic green candidate accounting; no validation run claimed"}]}),
        from_revision=state["revision"])


def _arc_three(copy, head, env, pr_state, holder):
    print("\n  ARC 3 — fresh admission, interrupted ownership, real rebase recovery and clean merge coverage.\n")
    print("      Validation is seeded; synthetic reviewer events pass real acceptance. Neither qualifies a runtime.")
    ok = True
    plan_id = "pln_" + "3" * 12
    payload = _seal_fixture(copy, env, holder, plan_id, _normal_payload())
    locator = os.path.join(holder, "recovery-locator.json")
    bind_args = ("plan", "bind", "--plan", plan_id, "--repository", REPO,
                 "--pr", str(PR), "--operator-decided")
    refs = [{"number": 12, "url": "https://github.com/" + REPO + "/issues/12"}]
    _update_pr(pr_state, closingIssuesReferences=refs)
    target = _advance(copy, pr_state, "upstream-before-bind.txt", "first target advance\n")
    stale = _build_cmd(copy, env, locator, *bind_args)
    records = list(Path(env["ENGINE_PLAN_DIR"]).glob("*/record.json"))
    plan_record = next(p for p in records if json.loads(p.read_text())["plan_id"] == plan_id)
    ok &= _pass("stale branch refuses before ownership", stale.returncode != 0 and
        not (json.loads(plan_record.read_text()).get("build_lease") or {}).get("current") and not Path(locator).exists(),
        "fresh target is required; no claim or locator exists")
    _require(_git(copy, "rebase", "origin/main"), "synchronize isolated unbound branch")
    head = _git(copy, "rev-parse", "HEAD").stdout.strip()
    _publish_head(copy, pr_state, head)
    competitor = {"number": PR + 1, "title": "Competing #12", "body": "Fixes #12",
                  "head": {"ref": "claude/12-other", "sha": "b" * 40, "repo": {"full_name": REPO}}}
    _update_pr(pr_state, demo_competitors=[competitor])
    collision = _build_cmd(copy, env, locator, *bind_args)
    scope = re.search(r"--overlap-override (sha256:[0-9a-f]{64})", collision.stdout + collision.stderr)
    ok &= _pass("real competitor refuses before ownership", collision.returncode != 0 and bool(scope)
        and not (json.loads(plan_record.read_text()).get("build_lease") or {}).get("current"),
        "the own source PR is excluded; the separate PR is named")
    if not scope:
        return False
    override = ("--overlap-override", scope[1], "--overlap-reason", "Disposable fixture explicitly accepts this observed competitor")
    interrupted_env = dict(env, DEMO_INTERRUPT_PREPARATION="1")
    interrupted = _build_cmd(copy, interrupted_env, locator, *bind_args, *override)
    claim = json.loads(plan_record.read_text())["build_lease"]["current"]
    original_record = plan_record.read_bytes()
    ok &= _pass("interrupted bind preserves frozen preparation", interrupted.returncode != 0 and
        claim["state"] == "preparing" and not Path(claim["snapshot"]).exists(), "reservation exists; activation did not run")
    _update_pr(pr_state, closingIssuesReferences=refs + [{"number": 13,
        "url": "https://github.com/" + REPO + "/issues/13"}])
    changed = _build_cmd(copy, env, locator, *bind_args, *override)
    ok &= _pass("changed admission cannot replace preparation", changed.returncode != 0 and
        plan_record.read_bytes() == original_record, "normalized issue set changed; original claim bytes survived")
    _update_pr(pr_state, closingIssuesReferences=refs)
    binding = json.loads(_require(_build_cmd(copy, env, locator, *bind_args, *override), "matching preparation retry"))
    state_path, identity = binding["state"], binding["ownership"]
    read = lambda: json.loads(Path(state_path).read_text())
    ok &= _pass("matching retry preserves admission timestamp", read()["admission"] == claim["admission"],
                "the first observation, ownership and consent remain the authority")
    def build(*args):
        return _build_cmd(copy, env, state_path, *args, ownership=identity)
    before = Path(state_path).read_bytes()
    continued = build(*bind_args)
    ok &= _pass("active bind continues without new admission", continued.returncode == 0 and
        json.loads(continued.stdout).get("continuation") and Path(state_path).read_bytes() == before,
        "same canonical Build, unchanged evidence")
    _require(build("approve", "--plan", payload, "--depth", "thorough"), "approve fixture Build")
    claim_result = json.loads(_require(build("work", "claim", "--item", "W1", "--provider", "claude",
        "--plan", payload, "--worktree", copy), "claim fixture work"))
    attempt = claim_result["attempt_id"]
    work_path = ".engine/tools/widget_cache.py"
    Path(copy, work_path).write_text("CACHE = {'local': 1}\n")
    _require(_git(copy, "add", "-A"), "stage fixture work")
    result_path = _write(os.path.join(holder, "recovery-result.json"), {"outcome": "returned",
        "evidence": {"changed_paths": [work_path],
            "verification_results": [{"command": "Inspect fixture cache source",
                "outcome": "passed", "detail": "CACHE contains local key; no test runner claimed."}],
            "assumptions": [], "unresolved_concerns": []}})
    _require(build("work", "result", "--item", "W1", "--attempt", attempt, "--plan", payload,
                   "--input", result_path), "record fixture result")
    _require(_git(copy, "commit", "-q", "-m", "Disposable cache implementation"), "commit fixture work")
    implemented = _git(copy, "rev-parse", "HEAD").stdout.strip()
    _publish_head(copy, pr_state, implemented)
    _require(build("work", "integrate", "--item", "W1", "--attempt", attempt, "--commit", implemented,
        "--plan", payload, "--verification-input", "Fixture source inspected: local cache key present."), "integrate actual receipt")
    original = read()
    _advance(copy, pr_state, work_path, "CACHE = {'upstream': 2}\n")
    _require(build("reconcile", "--plan", payload, "--prepare"), "prepare divergent rebase")
    conflicted = _git(copy, "rebase", "origin/main")
    ok &= _pass("rebase reaches a real conflict", conflicted.returncode != 0 and
                "CONFLICT" in conflicted.stdout, "both histories added different widget-cache content")
    Path(copy, work_path).write_text("CACHE = {'local': 1, 'upstream': 2}\n")
    _require(_git(copy, "add", work_path), "stage deliberate conflict resolution")
    _require(_git(copy, "-c", "core.editor=true", "rebase", "--continue"), "finish conflict rebase")
    rebased = _git(copy, "rev-parse", "HEAD").stdout.strip()
    _publish_head(copy, pr_state, rebased)
    _require(build("reconcile", "--plan", payload), "apply divergent recovery")
    recovered = read()
    ok &= _pass("divergent recovery preserves history and invalidates completion", not recovered["work"]["W1"]["integration"]
        and recovered["rewrite_recoveries"][-1]["prior_work"] == original["work"]
        and recovered["reviews"] == original["reviews"], "review fields remain empty; original receipt survives in canonical history")
    _require(build("work", "integrate", "--recovery", "--item", "W1", "--attempt", attempt,
        "--commit", rebased, "--plan", payload,
        "--verification-input", "Fixture reinspection: resolved cache contains both local and upstream keys."), "reverify resolved integration")
    _advance(copy, pr_state, "second-upstream.txt", "clean second advance\n")
    _require(build("reconcile", "--plan", payload, "--prepare"), "prepare clean recovery")
    _require(_git(copy, "rebase", "origin/main"), "clean second rebase")
    clean_head = _git(copy, "rev-parse", "HEAD").stdout.strip()
    _publish_head(copy, pr_state, clean_head)
    _require(build("reconcile", "--plan", payload), "apply clean recovery")
    handoff = os.path.join(holder, "canonical-handoff.json")
    before_handoff = read()
    _require(build("handoff", "export", "--output", handoff), "export recovered canonical evidence")
    _require(build("handoff", "restore", "--input", handoff), "restore recovered canonical evidence")
    after_handoff = read()
    ok &= _pass("clean recovery exports and restores canonical history", after_handoff["ownership"] == identity
        and after_handoff["rewrite_recoveries"] == before_handoff["rewrite_recoveries"]
        and after_handoff["work"]["W1"]["integration"]["receipt"] == original["work"]["W1"]["integration"]["receipt"],
        "original commit receipt is re-derived, private recovery records stay canonical")
    _require(build("approve", "--plan", payload, "--depth", "thorough"), "ordinary mutation after restore")
    def review_fixture(stage, commit):
        _seed_candidate_fixture(state_path, commit)
        packet = json.loads(_require(build("review", "packet", "--stage", stage, "--plan", payload, "--json"), "Build review packet"))
        for contract in packet["reviewer_contracts"]:
            lens = contract["lens"]
            _require(_observe_demo_build_review(copy, env, state_path, packet, lens), "observe Build reviewer fixture")
            _require(build("review", "record", "--stage", stage, "--lens", lens,
                "--packet-digest", packet["packet_digest"], "--lens-packet-digest", contract["lens_packet_digest"],
                "--code-execution", "none", "--session", "demo-review-root"), "accept Build reviewer fixture")
    review_fixture("deliverable", clean_head)
    repaired = _commit(copy, work_path, "CACHE = {'local': 1, 'upstream': 2, 'repair': 3}\n", "Disposable authored repair")
    _publish_head(copy, pr_state, repaired)
    _require(build("repair", "assess", "--judgment", "scoped", "--lens", "usability", "--lens", "spec-conformance",
        "--rationale", "DEMO FIXTURE: independently review the authored repair with simulated reviewer events"), "assess authored repair")
    review_fixture("repair", repaired)
    _advance(copy, pr_state, "merge-upstream.txt", "target to merge\n")
    _require(_git(copy, "merge", "--no-ff", "--no-edit", "origin/main"), "merge current target")
    merged = _git(copy, "rev-parse", "HEAD").stdout.strip()
    _publish_head(copy, pr_state, merged)
    before_validation = Path(state_path).read_bytes()
    refused = build("repair", "assess", "--judgment", "none", "--rationale", "Only target ancestry changed")
    ok &= _pass("merge preservation requires current candidate accounting", refused.returncode != 0
        and Path(state_path).read_bytes() == before_validation, "actual merged head has no candidate result yet")
    _seed_candidate_fixture(state_path, merged)
    before_merge_assess = read()
    _require(build("repair", "assess", "--judgment", "none", "--rationale", "Automatic target merge; fixture candidate accounting is current"),
        "retain receipts across clean target merge")
    final = read()
    prior_receipt_bytes = json.dumps(before_merge_assess["repair"]["receipts"], sort_keys=True).encode()
    current_receipt_bytes = json.dumps(final["repair"]["receipts"], sort_keys=True).encode()
    ok &= _pass("clean target merge retains prior receipts without another panel", current_receipt_bytes == prior_receipt_bytes
        and len(final["repair_rounds"]) == len(before_merge_assess["repair_rounds"])
        and sum(bc._round_counted(r) for r in final["repair_rounds"]) ==
            sum(bc._round_counted(r) for r in before_merge_assess["repair_rounds"])
        and final["base_advances"][-1]["validated_head"] == merged,
        "no accept-receipt-loss flag; receipt JSON bytes and counted-panel sum are unchanged")
    # Pure real composer, with the existing clearly synthetic claim/evidence fixture: no PR apply.
    import build_coordinator_contract as composer
    from test_build_coordinator_contract import _good_claim, _good_evidence
    narrative = _good_claim()
    narrative["purpose"]["thesis"] = "DEMO FIXTURE: disclose the observed clean target merge."
    narrative["validation"]["caveats"] = ["Synthetic demonstration accounting; no validation execution claimed."]
    body = composer.compose(narrative, dict(_good_evidence(), drift_line=bc._drift_line(final, merged)))
    proof = final["base_advances"][-1]
    ok &= _pass("real PR composer discloses the pinned merge proof", all(value in body for value in (
        proof["target_tip"], proof["merge_commit"], proof["validated_head"], REPO, "without restamping")),
        "target tip, merged head and validation head reach the composed body")
    return ok


class _IsolationTests(unittest.TestCase):
    def test_inherited_git_selectors_cannot_redirect_the_disposable_repository(self):
        with tempfile.TemporaryDirectory() as d:
            outside = Path(d) / 'outside'; outside.mkdir()
            target = Path(d) / 'target'; target.mkdir()
            clean = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
            clean.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
            subprocess.run(['git', 'init', '-q', str(outside)], env=clean, check=True)
            sentinel = outside / 'keep.txt'; sentinel.write_text('unchanged')
            before = (outside / '.git' / 'HEAD').read_bytes()
            with mock.patch.dict(os.environ, {'GIT_DIR': str(outside / '.git'),
                    'GIT_WORK_TREE': str(outside), 'GIT_INDEX_FILE': str(outside / 'wrong-index'),
                    'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'core.worktree',
                    'GIT_CONFIG_VALUE_0': str(outside)}):
                result = _git(str(target), 'init', '-q')
                self.assertEqual(result.returncode, 0, result.stderr)
                env = _demo_env()
                self.assertNotIn('GIT_DIR', env)
                self.assertNotIn('GIT_CONFIG_COUNT', env)
            self.assertTrue((target / '.git' / 'HEAD').is_file())
            self.assertEqual((outside / '.git' / 'HEAD').read_bytes(), before)
            self.assertEqual(sentinel.read_text(), 'unchanged')
            self.assertFalse((outside / 'wrong-index').exists())

    def test_source_symlink_refuses_before_any_git_or_fixture_write(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / 'source'; source.mkdir()
            holder = Path(d) / 'holder'; holder.mkdir()
            outside = Path(d) / 'keep.txt'; outside.write_text('unchanged')
            (source / 'widget_cache.py').symlink_to(outside)
            with mock.patch.object(validate, 'ROOT', str(source)), \
                    mock.patch(__name__ + '._git') as git:
                with self.assertRaisesRegex(ValueError, 'source symlink'):
                    _throwaway(str(holder))
                git.assert_not_called()
            self.assertEqual(outside.read_text(), 'unchanged')



def _source_snapshot():
    """Read-only source checkout invariant; no setup or Git mutation touches this checkout."""
    root = str(validate.ROOT)
    index_name = _git(root, "rev-parse", "--git-path", "index").stdout.strip()
    index = Path(index_name)
    if not index.is_absolute():
        index = Path(root) / index
    return {"head": _git(root, "rev-parse", "HEAD").stdout,
            "branch": _git(root, "symbolic-ref", "HEAD").stdout,
            "origin": _git(root, "remote", "get-url", "origin").stdout,
            "porcelain": _git(root, "status", "--porcelain=v1").stdout,
            "index_digest": hashlib.sha256(index.read_bytes()).hexdigest() if index.is_file() else None}


def main(_argv=None) -> int:
    # This setup-only demo owns its isolation regressions and retires with them.
    diagnostics = io.StringIO()
    result = unittest.TextTestRunner(stream=diagnostics).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(_IsolationTests))
    if not result.wasSuccessful():
        print(diagnostics.getvalue(), file=sys.stderr)
        return 1
    print("What this checks: a plan cannot be sealed before it is approved, cannot start a Build before")
    print("it is sealed, and — once it is — carries all the way to a pull request ready for you.\n")
    print("GitHub, submission accounting and reviewer events are simulated; acceptance commands are real.")
    print("The exact logical-origin identity query is also a fixture substitution.")
    print("All fetching, ancestry, rebasing, merging and ownership persistence use the real tools.\n")
    holder = tempfile.mkdtemp(prefix="entry-door-demo-")
    try:
        operator_before = _source_snapshot()
        first = os.path.join(holder, "arc1")
        copy, head, env, pr_state = _throwaway(first)
        ok = _arc_one(copy, head, env, pr_state, first)
        second = os.path.join(holder, "arc2")
        copy, head, env, pr_state = _throwaway(second)
        ok &= _arc_two(copy, head, env, second, pr_state)
        third = os.path.join(holder, "arc3")
        copy, head, env, pr_state = _throwaway(third)
        ok &= _arc_three(copy, head, env, pr_state, third)
        operator_after = _source_snapshot()
        ok &= _pass("operator checkout is unchanged", operator_before == operator_after,
                    "head, branch, origin, working-tree status and index digest stayed identical")
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
