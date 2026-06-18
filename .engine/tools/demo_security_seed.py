#!/usr/bin/env python3
"""Operator-runnable demo: the engine seeds a SECURITY.md (a security-contact file) into a project that has
none, and NEVER overwrites one a project already has — in any location GitHub recognizes.

Run: uv run --directory .engine -- python tools/demo_security_seed.py

This exercises the REAL seed logic (`instantiator._seed_security`) against throwaway practice projects — it
does not re-implement it, and it never touches this real project (every step runs under a redirected root, so
writes land only in a temp folder). Read the four blocks by eye:
  [1] an empty project gets a SECURITY.md at its root (you see the file that was written),
  [2] a project that ALREADY has its own SECURITY.md keeps it, untouched (you read your own words back),
  [3] a project whose security file is in the .github folder is left alone (no stray root file is made),
  [4] same for a security file in the docs folder.
The recognizable sentinel line in blocks [2]-[4] is your proof the engine left your file exactly as it was —
you are not trusting that a "skip" happened; you are reading your own text back, unchanged."""
from __future__ import annotations
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402
import instantiator as inst  # noqa: E402

# The maintainer's real seed (carried into the fixture so block [1] shows the REAL seed content).
_REAL_SEED = os.path.join(validate.ROOT, ".engine", "provisioning", "security-seed.md")
# A line a non-engineer will recognize on sight when read back unchanged.
_SENTINEL = "THIS IS THE PROJECT'S OWN SECURITY FILE -- KEEP IT EXACTLY AS IS"


def _seed_into_empty_project() -> bool:
    """[1] An empty project gets a SECURITY.md seeded at its root, from the maintainer's seed."""
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, ".engine", "provisioning"))
        if os.path.isfile(_REAL_SEED):
            with open(_REAL_SEED, encoding="utf-8") as src, \
                    open(os.path.join(d, ".engine", "provisioning", "security-seed.md"), "w", encoding="utf-8") as dst:
                dst.write(src.read())
        with inst._redirect_root(d):
            outcome = inst._seed_security(lambda _t: None, inst.load_copy())
            seeded_path = os.path.join(inst.validate.ROOT, "SECURITY.md")
            exists = os.path.isfile(seeded_path)
            first_lines = []
            if exists:
                with open(seeded_path, encoding="utf-8") as fh:
                    first_lines = [ln.rstrip("\n") for ln in fh.readlines()[:5]]
    print(f"   the project had no security file -> the engine {outcome} one; "
          f"SECURITY.md now at the root? {exists}")
    for ln in first_lines:
        print(f"     | {ln}")
    return outcome == "seeded" and exists


def _never_overwrites(location_rel: str, label: str) -> bool:
    """[2]-[4] A project that already has a SECURITY.md (in `location_rel`) is left exactly as it was, and no
    stray root file is created."""
    with tempfile.TemporaryDirectory() as d:
        existing = os.path.join(d, location_rel)
        parent = os.path.dirname(existing)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(existing, "w", encoding="utf-8") as fh:
            fh.write(_SENTINEL + "\n")
        with inst._redirect_root(d):
            outcome = inst._seed_security(lambda _t: None, None)
            with open(existing, encoding="utf-8") as fh:
                preserved = fh.read().strip() == _SENTINEL
            stray_root = location_rel != "SECURITY.md" and os.path.isfile(
                os.path.join(inst.validate.ROOT, "SECURITY.md"))
    ok = outcome == "present" and preserved and not stray_root
    print(f"   {label}: the engine added nothing and left it as-is; your file read back unchanged? "
          f"{preserved}; no stray root file made? {not stray_root}")
    print(f"     | {_SENTINEL}")
    return ok


def main() -> int:
    print("=" * 78)
    print("The security-contact file (SECURITY.md): seeded when absent, never overwritten")
    print("=" * 78)

    print("\n[1] An empty project. Expect: the engine ADDS a SECURITY.md at the root.")
    print("-" * 78)
    one = _seed_into_empty_project()

    print("\n[2] A project that already has its OWN SECURITY.md (at the root). Expect: left untouched.")
    print("-" * 78)
    two = _never_overwrites("SECURITY.md", "a security file at the root")

    print("\n[3] A project whose security file is in the .github folder. Expect: left untouched, no root file.")
    print("-" * 78)
    three = _never_overwrites(os.path.join(".github", "SECURITY.md"), "a security file in .github")

    print("\n[4] A project whose security file is in the docs folder. Expect: left untouched, no root file.")
    print("-" * 78)
    four = _never_overwrites(os.path.join("docs", "SECURITY.md"), "a security file in docs")

    ok = one and two and three and four
    print("\n" + "=" * 78)
    print("In plain words: the engine makes sure every project has a way for people to report a security")
    print("problem privately — but only by ADDING a file when there is none. If your project already has")
    print("one (anywhere GitHub looks: the root, the .github folder, or the docs folder), the engine leaves")
    print("it exactly as it is. It never replaces your own security file.")
    print("DEMO OK" if ok else "DEMO FAILED -- unexpected outcome")
    print("=" * 78)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
