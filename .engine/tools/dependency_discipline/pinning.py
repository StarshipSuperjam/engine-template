#!/usr/bin/env python3
"""Dependency-pinning inspector — the read-only `custom/script` entry for engine/check/dependency-pinning
(the dependency-discipline module's *soft* pinning nudge).

What it does: detects the product's dependency ecosystem by the presence of a dependency manifest at the
repository ROOT (package.json, pyproject.toml, requirements.txt, Cargo.toml, go.mod, Gemfile, composer.json)
and checks whether a lock/pin artifact is committed alongside it. A manifest without its lock file earns one
`soft` "unpinned" nudge; a manifest with its lock passes cleanly; and when no dependency manifest is present
it emits a *disclosed no-op* ("not yet applicable — it activates when the project adds dependencies"),
distinct from "could not run" and never a silent pass.

Honest floor: detection is ROOT-LEVEL and PRESENCE-BASED — it does not walk subdirectories and does not parse
individual version specifiers (a `requirements.txt` with loose ranges counts as a pin record). It is a soft
hygiene nudge, not a guarantee; a deeper per-package or monorepo audit is a later refinement.

Tiers / blocking: every finding is `soft`, so this check never blocks a merge even in CI's blocking-gate
context. Read-only: it inspects file presence only and never writes (the R5 mutation firewall).

Engine/product wall (§13): it scans `validate.ROOT` (the repository root) only, never the engine's own
walled `.engine/` tooling — so `.engine/pyproject.toml` / `.engine/uv.lock` are never mistaken for product
dependencies. On a repo with no product manifest (such as engine-template itself) it emits the disclosed
no-op and stays green.

Contract: invoked by the validator with NO arguments, it prints a finding.v1 JSON array to stdout and exits
0. A separate `demo` subcommand runs a falsifiable self-check.
"""
from __future__ import annotations
import json
import os
import sys

# Make the sibling `.engine/tools/` modules importable whether imported as `dependency_discipline.pinning`
# or run directly as the wired check script (the projects_sync idiom).
_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

import validate  # noqa: E402 — ROOT (test-redirectable) + the finding.v1 helper


# Each ecosystem: the root manifest(s) that DECLARE dependencies, and the lock/pin artifacts whose presence
# means those dependencies are locked to exact versions. Presence-based and root-level by design (a soft
# hygiene nudge, not a per-package version audit). `requirements.txt` is both a Python manifest and counts as
# its own pin record — the idiomatic pip "lock" — so its presence satisfies the pinned condition.
_ECOSYSTEMS = (
    {"label": "Node.js (npm)", "manifests": ("package.json",),
     "locks": ("package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml"),
     "recommend": "a lock file such as package-lock.json, yarn.lock, or pnpm-lock.yaml"},
    {"label": "Python", "manifests": ("pyproject.toml", "requirements.txt"),
     "locks": ("uv.lock", "poetry.lock", "Pipfile.lock", "requirements.txt"),
     "recommend": "a lock file such as uv.lock or poetry.lock (a pinned requirements.txt also counts)"},
    {"label": "Rust (Cargo)", "manifests": ("Cargo.toml",),
     "locks": ("Cargo.lock",), "recommend": "Cargo.lock"},
    {"label": "Go modules", "manifests": ("go.mod",),
     "locks": ("go.sum",), "recommend": "go.sum"},
    {"label": "Ruby (Bundler)", "manifests": ("Gemfile",),
     "locks": ("Gemfile.lock",), "recommend": "Gemfile.lock"},
    {"label": "PHP (Composer)", "manifests": ("composer.json",),
     "locks": ("composer.lock",), "recommend": "composer.lock"},
)

_NO_OP_MESSAGE = (
    "Dependency pinning isn't active here yet — this check looks for a dependency file at your project's "
    "root (such as package.json, pyproject.toml, requirements.txt, Cargo.toml, go.mod, Gemfile, or "
    "composer.json) and didn't find one. That's a normal, expected state for a project that hasn't taken on "
    "any outside packages yet, not an error: the check will start watching for unlocked versions on its own "
    "once your project adds dependencies."
)


def _first_present(root: str, names) -> "str | None":
    """The first of `names` that exists as a file directly under `root` (root-level only), or None."""
    for name in names:
        if os.path.isfile(os.path.join(root, name)):
            return name
    return None


def _unpinned_message(eco: dict, manifest: str) -> str:
    return (
        f"Your project's {eco['label']} dependencies (declared in {manifest}) aren't locked to exact "
        f"versions — there's no lock file committed alongside it. Without one, the same project can install "
        f"slightly different code on different days, which makes builds harder to reproduce and lets a "
        f"changed dependency slip in unnoticed. Committing {eco['recommend']} pins every dependency to an "
        f"exact version. This is a gentle hygiene nudge, not a blocker — nothing is being stopped, and this "
        f"check never changes a file."
    )


def findings(tier: str, root: "str | None" = None) -> list:
    """The pinning findings for `root` (defaults to `validate.ROOT`), as a list of finding.v1 dicts.

    Empty list = a clean pass (every detected ecosystem is locked). A single disclosed-no-op finding when no
    dependency manifest is present at the root. One `soft` "unpinned" nudge per detected ecosystem whose
    manifest has no lock artifact. Every finding carries `tier` severity (`soft`) — never `hard`.
    """
    root = root or validate.ROOT
    detected = []
    for eco in _ECOSYSTEMS:
        manifest = _first_present(root, eco["manifests"])
        if manifest is not None:
            detected.append((eco, manifest))

    if not detected:
        return [validate.disclosed_noop(_NO_OP_MESSAGE, None)]

    out = []
    for eco, manifest in detected:
        if _first_present(root, eco["locks"]) is None:
            out.append(validate.finding(tier, _unpinned_message(eco, manifest),
                                        {"file": manifest, "line": None}))
    return out


def emit_findings() -> int:
    """The no-argument path the validator invokes: print the finding.v1 array and return 0."""
    print(json.dumps(findings("soft")))
    return 0


def demo() -> int:
    """Prove the inspector flags an unpinned manifest, passes a pinned one, discloses the no-op on an empty
    root, and never counts the engine's own `.engine/` tooling as a product dependency (the §13 wall) —
    RETURNS NON-ZERO if any invariant is broken (the falsification can fail). Mutation-free: every case runs
    against a throwaway temp root, so the real working tree is never touched."""
    import shutil
    import tempfile

    def _seed(files: dict) -> str:
        d = tempfile.mkdtemp(prefix="engine-pinning-demo-")
        for rel, body in files.items():
            path = os.path.join(d, rel)
            parent = os.path.dirname(path)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
        return d

    cases = []  # (label, seeded files, predicate over the findings list)
    cases.append(("an unpinned package.json earns one soft nudge",
                  {"package.json": "{}"},
                  lambda fs: len(fs) == 1 and fs[0]["severity"] == "soft"
                  and "aren't locked" in fs[0]["message"]))
    cases.append(("a pinned package.json passes cleanly",
                  {"package.json": "{}", "package-lock.json": "{}"},
                  lambda fs: fs == []))
    cases.append(("an empty root discloses the no-op (never a silent pass)",
                  {},
                  lambda fs: len(fs) == 1 and "isn't active here yet" in fs[0]["message"]))
    cases.append(("the engine's own .engine/ tooling is not a product dependency (the §13 wall)",
                  {".engine/pyproject.toml": "[project]\nname = 'engine'\n", ".engine/uv.lock": ""},
                  lambda fs: len(fs) == 1 and "isn't active here yet" in fs[0]["message"]))

    failures = []
    for label, files, ok in cases:
        root = _seed(files)
        try:
            result = findings("soft", root=root)
        finally:
            shutil.rmtree(root, ignore_errors=True)
        if any(f.get("severity") == "hard" for f in result):
            failures.append(f"{label}: a pinning finding must never be hard, got {result}")
        elif not ok(result):
            failures.append(f"{label}: invariant broken, got {result}")

    if failures:
        print("DEMO FAILED — the pinning inspector broke an invariant:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("DEMO PASSED — the pinning inspector flags an unpinned manifest, passes a pinned one, discloses "
          "the no-op on an empty root, and never counts the engine's own tooling as a product dependency.")
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return demo()
    return emit_findings()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
