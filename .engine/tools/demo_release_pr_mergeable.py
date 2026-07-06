#!/usr/bin/env python3
"""Behavioural demonstration: the Part-A fix makes a real 0.1.0 cut produce a MERGEABLE release PR.

On a throwaway COPY of this very repo, it runs the exact sequence release.yml runs on a real cut —
record the versions, regenerate the two generated maps, render the pull-request body — and asserts the
two things a real release PR is gated on, both of which the live verification run (PR #378) showed RED
*before* this fix:

  1. THE DERIVED MAPS. Bumping every manifest to 0.1.0 makes the knowledge graph and the self-map stale
     (their CI drift checks `knowledge-coverage` + `self-map-drift` go red). Step 1 reproduces that
     staleness (the defect, as a negative control); Step 2 shows the workflow's new regen step clears it.
  2. THE PULL-REQUEST BODY. The generated body must carry all eight sections `pr-body-completeness`
     requires (a RELEASE_PAT-opened PR is not author-exempt). Step 3 shows the rendered body passes that
     hard check; Step 4 is a negative control — an incomplete body still trips it, so Step 3's green is
     not vacuous.

Everything runs ROOTED IN THE COPY: each tool is invoked as `<copy>/.engine/tools/<tool>.py`, so its
`validate.ROOT` resolves to the copy — the real repo's maps are never touched. It runs the REAL tools
(release_cut, knowledge_gen, self_map, validate); only the repo it acts on is a throwaway. Offline, no
network, no real-repo mutation, and able to fail (the two negative controls prove the checks bite).

  uv run --directory .engine -- python tools/demo_release_pr_mergeable.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

import validate  # only to locate the real repo root (validate.ROOT)

VERSION = "0.1.0"
# The exact finding signatures the live run (PR #378) produced — the three checks Part A drives to green.
GRAPH_SIG = "knowledge/graph.json) is out of date"   # knowledge-coverage: the graph is stale
SELFMAP_SIG = "self-map.md) is out of date"           # self-map-drift: the self-map is stale
BODY_SIG = "Required section '##"                      # pr-body-completeness: a required section is missing


def _copy_repo(dst: str) -> None:
    """A throwaway copy of the real repo — everything except the transient/gitignored trees. The copied
    tools carry their own `validate.py`, so running them rooted here roots ROOT here (never the real repo)."""
    shutil.copytree(validate.ROOT, dst, ignore=shutil.ignore_patterns(
        ".git", ".venv", "__pycache__", "*.pyc", ".cache", "node_modules"))


def _run(engine_dir: str, tool: str, *args: str):
    """Run one real engine tool FROM THE COPY (so validate.ROOT resolves to the copy), with the current
    interpreter (the real venv supplies any deps). GITHUB_EVENT_PATH is cleared so the run is offline and
    the PR body comes only from an explicit --pr-body-file."""
    env = dict(os.environ)
    env.pop("GITHUB_EVENT_PATH", None)
    r = subprocess.run([sys.executable, os.path.join(engine_dir, "tools", tool), *args],
                       cwd=engine_dir, capture_output=True, text=True, env=env)
    return r.returncode, r.stdout, r.stderr


def _validate(engine_dir: str, pr_body_file: str | None = None) -> str:
    args = ["--suite", "CI"]
    if pr_body_file:
        args += ["--pr-body-file", pr_body_file]
    _, out, err = _run(engine_dir, "validate.py", *args)
    return out + err


def main() -> int:
    ok = True
    scratch = tempfile.mkdtemp(prefix="release-pr-mergeable-")
    copy = os.path.join(scratch, "repo")
    engine = os.path.join(copy, ".engine")
    try:
        _copy_repo(copy)

        # 1. THE DEFECT — record the versions, DON'T regenerate the maps: the two generated maps go stale.
        _, applied_json, _ = _run(engine, "release_cut.py", "apply",
                                  "--engine", VERSION, "--all", VERSION, "--json")
        defect = _validate(engine)
        graph_stale = GRAPH_SIG in defect
        selfmap_stale = SELFMAP_SIG in defect
        print("1. THE DEFECT — bump every manifest to 0.1.0, no regen (what PR #378 hit):")
        print(f"   knowledge graph went stale  = {graph_stale}")
        print(f"   self-map went stale         = {selfmap_stale}")
        ok &= graph_stale and selfmap_stale

        # 2. THE MAP FIX — the workflow's new step: regenerate both maps from the just-written manifests.
        _run(engine, "knowledge_gen.py", "generate")
        _run(engine, "self_map.py", "generate")
        fixed = _validate(engine)
        graph_ok = GRAPH_SIG not in fixed
        selfmap_ok = SELFMAP_SIG not in fixed
        print("\n2. THE MAP FIX — regenerate both maps (release.yml's new step):")
        print(f"   knowledge graph now consistent = {graph_ok}")
        print(f"   self-map now consistent        = {selfmap_ok}")
        ok &= graph_ok and selfmap_ok

        # 3. THE BODY FIX — render the release PR body and run it through the real pr-body-completeness gate.
        with open(os.path.join(engine, "applied.json"), "w") as fh:
            fh.write(applied_json)
        proposal = {"mode": "first-cut", "impacts": [], "engine_floor_version": None,
                    "change_inventory": ["First release: establishes the baseline version for the engine "
                                         "and all installed packages."]}
        with open(os.path.join(engine, "proposal.json"), "w") as fh:
            json.dump(proposal, fh)
        _, body_md, _ = _run(engine, "release_cut.py", "pr-body",
                             "--proposal", "proposal.json", "--applied", "applied.json")
        with open(os.path.join(engine, "body.md"), "w") as fh:
            fh.write(body_md)
        body_result = _validate(engine, pr_body_file="body.md")
        body_complete = BODY_SIG not in body_result
        print("\n3. THE BODY FIX — the rendered release body clears pr-body-completeness:")
        print(f"   all eight required sections present + filled = {body_complete}")
        ok &= body_complete

        # 4. NEGATIVE CONTROL — an incomplete body STILL trips the gate, so Step 3's green isn't vacuous.
        with open(os.path.join(engine, "bad_body.md"), "w") as fh:
            fh.write("## Purpose\nA one-section body that is deliberately incomplete.\n")
        bad_result = _validate(engine, pr_body_file="bad_body.md")
        body_check_bites = BODY_SIG in bad_result
        print("\n4. NEGATIVE CONTROL — an incomplete body is still caught (the check bites):")
        print(f"   incomplete body flagged by pr-body-completeness = {body_check_bites}")
        ok &= body_check_bites

        print("\n" + ("DEMO PASSED: a 0.1.0 cut goes red on stale maps + an incomplete body; regenerating the "
                      "maps and rendering the eight-section body drives all three checks green — the release PR "
                      "is mergeable, and the body gate still bites an incomplete body."
                      if ok else "DEMO DID NOT BEHAVE AS EXPECTED — see the per-step results above."))
        return 0 if ok else 1
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
