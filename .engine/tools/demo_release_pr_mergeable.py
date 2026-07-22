#!/usr/bin/env python3
"""Behavioural demonstration: the Part-A fix makes a real 0.1.0 cut produce a MERGEABLE release PR.

On a throwaway COPY of this very repo, it runs the exact sequence release.yml runs on a real cut —
record the versions, regenerate the two generated maps, render the pull-request body — and asserts the
three things a real release PR's `engine-ci` is gated on, each of which a live verification run showed
RED *before* its fix:

  1. THE DERIVED MAPS. Bumping every manifest to 0.1.0 makes the knowledge graph and the self-map stale
     (their CI drift checks `knowledge-coverage` + `self-map-drift` go red). Step 1 reproduces that
     staleness (the defect, as a negative control); Step 2 shows the workflow's regen step clears it.
  2. THE SELF-TESTS. `engine-ci` runs the self-test suite too, not just the validator — so a test that
     hardcodes the construction sentinel (0.0.0-dev) as a module's live version passes the validator but
     fails `engine-ci` the instant a cut moves the version off the sentinel (PR #384 hit exactly this).
     Step 3 asserts the bumped, regenerated tree passes the version-derivation self-tests — the layer a
     tree-only check (Part A's original demo) could not see.
  3. THE PULL-REQUEST BODY. The generated body must carry all eight sections `pr-body-completeness`
     requires AND the consent preamble (a RELEASE_PAT-opened PR is not author-exempt). Step 4 shows the
     rendered body passes that hard check; Step 5 is a negative control — an incomplete body still trips
     it — and Step 6 a second negative control — a fully-sectioned body that dropped the preamble still
     trips it — so Step 4's green is not vacuous on either leg.

Everything runs ROOTED IN THE COPY: each tool is invoked as `<copy>/.engine/tools/<tool>.py`, so its
`validate.ROOT` resolves to the copy — the real repo's maps are never touched. It runs the REAL tools
(release_cut, knowledge_gen, self_map, validate); only the repo it acts on is a throwaway. Offline, no
network, no real-repo mutation, and able to fail (the three negative controls prove the checks bite).

  uv run --directory .engine -- python tools/demo_release_pr_mergeable.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

import release_cut  # for the strictly-greater next-version bump (_bump_at_least); a pure version function
import validate     # to locate the real repo root (validate.ROOT)

# The exact finding signatures the live run (PR #378) produced — the three checks Part A drives to green.
GRAPH_SIG = "knowledge/graph.json) is out of date"   # knowledge-coverage: the graph is stale
SELFMAP_SIG = "self-map.md) is out of date"           # self-map-drift: the self-map is stale
BODY_SIG = "Required section '##"                      # pr-body-completeness: a required section is missing
PREAMBLE_SIG = "consent preamble"                     # pr-body-completeness: the preamble anchor is absent


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


def _selftest(engine_dir: str, *modules: str):
    """Run engine self-test module(s) FROM THE COPY's tools dir — the layer `engine-ci` runs after the
    validator. Rooted in the copy (each test file inserts its own dir on sys.path), so it exercises the
    copy's derivation, never the real repo's. Returns (returncode, stdout, stderr)."""
    env = dict(os.environ)
    env.pop("GITHUB_EVENT_PATH", None)
    r = subprocess.run([sys.executable, "-m", "unittest", *modules],
                       cwd=os.path.join(engine_dir, "tools"), capture_output=True, text=True, env=env)
    return r.returncode, r.stdout, r.stderr


def main() -> int:
    ok = True
    scratch = tempfile.mkdtemp(prefix="release-pr-mergeable-")
    copy = os.path.join(scratch, "repo")
    engine = os.path.join(copy, ".engine")
    try:
        _copy_repo(copy)

        # A real cut always moves the version UP from wherever the repo is now — so bump to the next version
        # above the copy's current engine version. This works whether the repo is at the construction sentinel
        # or at an already-published release; cutting the SAME version would be a no-op that regenerates
        # nothing, so the defect + fix would not show.
        with open(os.path.join(engine, "engine.json")) as fh:
            current = json.load(fh)["engine_release"]
        version = release_cut._bump_at_least(current, "minor")

        # 1. THE DEFECT — record the versions, DON'T regenerate the maps: the two generated maps go stale.
        _, applied_json, _ = _run(engine, "release_cut.py", "apply",
                                  "--engine", version, "--all", version, "--json")
        defect = _validate(engine)
        graph_stale = GRAPH_SIG in defect
        selfmap_stale = SELFMAP_SIG in defect
        print(f"1. THE DEFECT — bump every manifest {current} → {version}, no regen (the stale-maps defect PR #378 hit):")
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

        # 3. THE SELF-TEST LAYER — engine-ci runs the self-tests too. A test that hardcodes the sentinel as a
        #    module's live version passes the validator (Step 2) but fails engine-ci once a cut moves the
        #    version (PR #384). Assert the bumped, regenerated tree passes the version-derivation self-tests —
        #    the exact suite + failure PR #384 hit, which the tree-only Step 2 check cannot see.
        st_code, st_out, st_err = _selftest(engine, "test_knowledge")
        selftests_pass = st_code == 0
        print(f"\n3. THE SELF-TEST LAYER — the {version} tree passes the version-derivation self-tests engine-ci runs:")
        print(f"   test_knowledge green on the bumped tree = {selftests_pass}")
        if not selftests_pass:
            tail = (st_out + st_err).strip().splitlines()
            print("   " + (tail[-1] if tail else "(no output)"))
        ok &= selftests_pass

        # 4. THE BODY FIX — render the release PR body and run it through the real pr-body-completeness gate.
        with open(os.path.join(engine, "applied.json"), "w") as fh:
            fh.write(applied_json)
        proposal = {"impacts": [], "engine_floor_version": None,
                    "change_inventory": [f"Records the engine and all installed packages at {version}."]}
        with open(os.path.join(engine, "proposal.json"), "w") as fh:
            json.dump(proposal, fh)
        _, body_md, _ = _run(engine, "release_cut.py", "pr-body",
                             "--proposal", "proposal.json", "--applied", "applied.json")
        with open(os.path.join(engine, "body.md"), "w") as fh:
            fh.write(body_md)
        body_result = _validate(engine, pr_body_file="body.md")
        body_complete = BODY_SIG not in body_result and PREAMBLE_SIG not in body_result
        print("\n4. THE BODY FIX — the rendered release body clears pr-body-completeness:")
        print(f"   eight required sections filled AND the consent preamble carried = {body_complete}")
        ok &= body_complete

        # 5. NEGATIVE CONTROL — an incomplete body STILL trips the gate, so Step 4's green isn't vacuous.
        with open(os.path.join(engine, "bad_body.md"), "w") as fh:
            fh.write("## Purpose\nA one-section body that is deliberately incomplete.\n")
        bad_result = _validate(engine, pr_body_file="bad_body.md")
        body_check_bites = BODY_SIG in bad_result
        print("\n5. NEGATIVE CONTROL — an incomplete body is still caught (the check bites):")
        print(f"   incomplete body flagged by pr-body-completeness = {body_check_bites}")
        ok &= body_check_bites

        # 6. PREAMBLE NEGATIVE CONTROL — a body with all eight sections filled but the preamble DROPPED
        #    still trips the gate, so Step 4's preamble check is not vacuous (the #491 preamble-drop class).
        _sections = ["Purpose", "Scope", "Out of scope", "Risk", "Validation", "Review",
                     "Files of interest", "AI involvement"]
        preambleless = "\n".join(f"## {s}\n**Real summary**\n- a real bullet\n*Impact: real consequence*"
                                 for s in _sections)
        with open(os.path.join(engine, "no_preamble_body.md"), "w") as fh:
            fh.write(preambleless)
        no_preamble_result = _validate(engine, pr_body_file="no_preamble_body.md")
        preamble_check_bites = PREAMBLE_SIG in no_preamble_result and BODY_SIG not in no_preamble_result
        print("\n6. PREAMBLE NEGATIVE CONTROL — a fully-sectioned body that dropped the preamble is caught:")
        print(f"   preamble-less body flagged by pr-body-completeness = {preamble_check_bites}")
        ok &= preamble_check_bites

        print("\n" + (f"DEMO PASSED: a {version} cut goes red on stale maps; regenerating the maps clears them, the "
                      "bumped tree passes the version-derivation self-tests, and the rendered eight-section body — "
                      "consent preamble included — clears pr-body-completeness; the release PR is mergeable, and the "
                      "body gate still bites both an incomplete body and a fully-sectioned body that dropped the preamble."
                      if ok else "DEMO DID NOT BEHAVE AS EXPECTED — see the per-step results above."))
        return 0 if ok else 1
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
