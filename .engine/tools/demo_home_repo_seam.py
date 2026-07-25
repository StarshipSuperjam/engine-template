#!/usr/bin/env python3
"""Operator-runnable demo: the engine's two public-safety scope checks now decide "is this the engine's own
home repo?" from a STRUCTURAL signal — the checkout's git origin matching the recorded home — not from a text
marker in CLAUDE.md that both travels into every copy and vanishes the moment that file becomes the deployed
floor.

Run: uv run --directory .engine -- python tools/demo_home_repo_seam.py

Why this matters: two HARD checks only act in the engine's OWN home repository — the memory-backup pointer leak
guard (it must never let a maintainer's private vault coordinates ship to everyone who uses the template) and
the demo-census guard (it keeps the engine's own build-evidence demos from shipping as clutter). If they keyed off a CLAUDE.md
marker, then promoting that file to the plain deployed floor — which drops the marker — would silently switch
BOTH guards off in the very repo they protect. This demo proves they don't: it builds throwaway git repos and
runs the REAL check logic under a redirected root, so nothing touches your project.

Read the cells by eye:
  [1] a HOME repo (git origin == the recorded home) whose CLAUDE.md carries NO marker -> both guards still see
      "this is home" and RUN; the census guard actually BITES a planted orphan demo. (Revert the re-key so the
      guards read the marker again and this cell fails — with the marker stripped they would read "not home"
      and silently no-op. That is the falsification.)
  [2] a DOWNSTREAM copy (git origin != home) whose CLAUDE.md DOES carry the marker -> origin wins over the
      traveled text: both guards see "not home" and no-op, so a deployed project's own choices are never flagged.
  [3] the sentence a DEPLOYED project actually reads in its own validator output about one of those inert
      checks. It must be a soft note (never a block), state the REAL reason, name `.engine/engine.json` — the
      one file its operator can open and check — and never mention CLAUDE.md. (Restore the old wording and this
      cell fails: it asserted the check was inert because the root CLAUDE.md "is not the construction-governance
      body", which has not been true since that body was deleted.)

(The separate, destructive first-run cleanup — retire/verify — does NOT gate on this signal at all; it is
guarded by the `--first-run` token so no origin read can ever stand between a bare hand-run and an irreversible
self-delete. That interlock has its own tests; this demo is only about the two scope guards.)"""
from __future__ import annotations
import contextlib
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402
import census_completeness_check as census  # noqa: E402
import memory_pointer_public_safety_check as leak  # noqa: E402

HOME = "StarshipSuperjam/engine-template"
_MARKER = "# engine-template — construction governance\n"
_DEPLOYED_FLOOR = "# Your project runs on an Engine\n"


def _git(root: str, *args: str) -> None:
    subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, check=False)


def _fixture(d: str, *, origin: str, claude: str) -> str:
    """A throwaway git checkout with a set origin, a manifest recording HOME, the given root CLAUDE.md, a
    planted ORPHAN demo (`demo_orphan_stand_in.py`), and a first-run census that OMITS it — so the census guard,
    if it runs, has a real bad input to bite."""
    root = os.path.join(d, "repo")
    tools = os.path.join(root, ".engine", "tools")
    prov = os.path.join(root, ".engine", "provisioning")
    os.makedirs(tools)
    os.makedirs(prov)
    _git(root, "init", "-q")
    _git(root, "remote", "add", "origin", origin)
    with open(os.path.join(root, ".engine", "engine.json"), "w", encoding="utf-8") as fh:
        json.dump({"engine_release": "0.0.0", "home_repository": HOME}, fh)
    with open(os.path.join(root, "CLAUDE.md"), "w", encoding="utf-8") as fh:
        fh.write(claude)
    with open(os.path.join(tools, "demo_orphan_stand_in.py"), "w", encoding="utf-8") as fh:
        fh.write('"""a home-only demo that neither retires nor is reached — orphan drift."""\n')
    with open(os.path.join(prov, "first-run-assets.json"), "w", encoding="utf-8") as fh:
        json.dump({"description": "fixture", "files": [], "directories": []}, fh)  # omits the orphan
    return root


@contextlib.contextmanager
def _rooted_at(root: str):
    """Point the checks' shared `validate.ROOT` at the fixture, so their real scope gate judges the fixture's
    on-disk origin — restored afterward."""
    original = validate.ROOT
    validate.ROOT = root
    try:
        yield
    finally:
        validate.ROOT = original


def _home_repo_runs_the_guards_without_a_marker() -> bool:
    with tempfile.TemporaryDirectory() as d:
        root = _fixture(d, origin=f"https://github.com/{HOME}.git", claude=_DEPLOYED_FLOOR)
        with _rooted_at(root):
            leak_sees_home = leak._in_home_repo()
            census_sees_home = census._in_home_repo()
            findings = census.check(root)
    bit = [f["location"]["file"] for f in findings]
    orphan_bitten = ".engine/tools/demo_orphan_stand_in.py" in bit
    print(f"   home repo, NO marker in CLAUDE.md: leak guard sees home? {leak_sees_home}; "
          f"census guard sees home? {census_sees_home}; census BIT the planted orphan? {orphan_bitten}")
    return leak_sees_home and census_sees_home and orphan_bitten


def _downstream_copy_no_ops_even_with_a_marker() -> bool:
    with tempfile.TemporaryDirectory() as d:
        root = _fixture(d, origin="https://github.com/adopter/their-product.git", claude=_MARKER)
        with _rooted_at(root):
            leak_sees_home = leak._in_home_repo()
            census_sees_home = census._in_home_repo()
            findings = census.check(root)
    print(f"   downstream copy, marker PRESENT in CLAUDE.md: leak guard sees home? {leak_sees_home}; "
          f"census guard sees home? {census_sees_home}; census findings? {len(findings)}")
    return (not leak_sees_home) and (not census_sees_home) and findings == []


def _adopter_is_told_plainly_why_the_check_is_inert() -> bool:
    """The sentence a DEPLOYED project reads in its own validator output. It used to say the check was inert
    'because this repository's root CLAUDE.md is not the construction-governance body' — a body that no longer
    exists. This drives the REAL applicability path against the REAL shipped declaration."""
    import hard_check_bite_check as hcb
    fdir = os.path.join(validate.ROOT, ".engine", "_fixtures", "census-completeness")
    with tempfile.TemporaryDirectory() as d:
        root = _fixture(d, origin="https://github.com/adopter/their-product.git", claude=_DEPLOYED_FLOOR)
        found = hcb._failed_bite_applicability("engine/check/census-completeness", fdir, root, "hard")
    if not found:
        print("   the shipped declaration was not honored at all -- nothing rendered")
        return False
    soft = all(f["severity"] == "soft" for f in found)
    print(f"   severity: {'soft (a note, never a block)' if soft else 'HARD -- would block their merge'}")
    for f in found:
        for line in _wrap(f["message"]):
            print(f"   | {line}")
    text = " ".join(f["message"] for f in found)
    return soft and "NOT APPLICABLE HERE" in text and "CLAUDE.md" not in text and ".engine/engine.json" in text


def _wrap(text: str, width: int = 96) -> list:
    out, line = [], ""
    for word in text.split():
        if len(line) + len(word) + 1 > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return out


def main() -> int:
    print("=" * 78)
    print("Home-repo scope guards key on git origin == recorded home, not a CLAUDE.md marker")
    print("=" * 78)

    print("\n[1] HOME repo whose CLAUDE.md has NO marker. Expect: both guards RUN; census bites the orphan.")
    print("-" * 78)
    one = _home_repo_runs_the_guards_without_a_marker()

    print("\n[2] DOWNSTREAM copy whose CLAUDE.md HAS the marker. Expect: both guards no-op (origin wins).")
    print("-" * 78)
    two = _downstream_copy_no_ops_even_with_a_marker()

    print("\n[3] What a DEPLOYED project is told about a check that is inert there. Expect: a plain, TRUE note")
    print("    that names .engine/engine.json and never mentions CLAUDE.md.")
    print("-" * 78)
    three = _adopter_is_told_plainly_why_the_check_is_inert()

    ok = one and two and three
    print("\n" + "=" * 78)
    print("In plain words: the guards that protect the engine's own home repo now recognize it by a fact a copy")
    print("can't fake or inherit — its git origin — instead of a line of text that copies carry and the deployed")
    print("floor drops. So promoting CLAUDE.md to the plain deployed floor can never quietly switch them off at")
    print("home, and a deployed project's own legitimate choices are never mistaken for the template's.")
    print("A deployed project is also told the truth about why such a check does nothing there: a plain note")
    print("pointing at its own .engine/engine.json, not at a governance file the engine no longer has.")
    print("DEMO OK" if ok else "DEMO FAILED -- a guard keyed on the marker, or an adopter was told something untrue")
    print("=" * 78)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
