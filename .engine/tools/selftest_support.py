"""selftest_support.py — the ONE place the self-test suite's guard helpers live.

Three small decisions used to be copied from test module to test module, and the copies drifted: a
predicate for "this run judges the real construction repo, not a projection", a helper for "which modules
are present on disk", and a skip wrapper built on that helper. Each drift was harmless until it wasn't — a
missed copy is a case that quietly skips where it should run, or runs against a shape it was never meant
to judge. They live here now; the test modules import them (StarshipSuperjam/engine-template#940).

THE NAME IS DELIBERATE. This file does not start with `test_`, and it must never be renamed to. That prefix
is how the engine decides what is a test module — the self-test change selector (selftest_select.py) stops
walking the import graph at a test module and never selects its importers; the knowledge graph labels a
test module's imports as `tests` edges; the assurance page lists every test module; the integrity guards
scan them; the shipped-issue-reference check exempts them. A helper under that prefix would be wrongly
treated as all of those at once. mcp_test_support.py sits outside the prefix for the same reason.

WHICH "INSTALLED" THIS ANSWERS. `installed_module_ids` reads presence on disk via the module manifests
(installed-means-present): a deployment that DECLINED an optional module removes its subtree, so its id
drops out — the roster-aware signal a test uses to skip a leg that assumes the module is there (StarshipSuperjam/engine-template#646).
module_surfaces._installed_module_ids and engine_help._installed_module_ids answer a different question —
what engine.json's `packages` records — and are not copies of this one; do not unify them by name.

THE ENV-VAR NAME IS A VALUE, NOT AN IMPORT. selftest.py and release_gate.py each define the nested-run
marker by value, and this module does too: a test module's import graph stays light, so the launcher is not
imported here just to read one string (the choice the copies made before they were consolidated). The three
homes are pinned equal by a case in test_launch_contract.py — the one deliberate place that imports both
selftest and release_gate, precisely to compare their values — so they cannot drift apart silently.

TWO MARKERS, TWO MEANINGS, NEVER CONFLATED. `NESTED_ENV` is a recursion guard only: selftest.py sets it on
the single child that runs the whole suite, and release_gate.py sets it on every process it spawns inside a
projection, purely so a nested run refuses to spawn another nested run underneath it. It feeds no test gate
here. `PROJECTION_ENV` answers a different question — "is this process running inside a projected deployed
tree" — and only release_gate._nested_env sets it, once per projection spawn. `CONSTRUCTION` (via
`shape_verdict`) reads `PROJECTION_ENV` alone: the home repo, outside a projection, regardless of nesting.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import module_coherence  # noqa: E402
import repo_identity     # noqa: E402
import validate          # noqa: E402

# The recursion-refusal marker: selftest.py sets it on the child that runs the whole suite, and
# release_gate.py sets it on every process it spawns inside a projection, so a nested run refuses to spawn
# another nested run underneath it. Same string as selftest._NESTED_ENV and release_gate._NESTED_ENV
# (pinned by test). It gates recursion only — no test case is skipped or run because of it.
NESTED_ENV = "ENGINE_NESTED_SELFTEST"

# The projection marker: set only by release_gate._nested_env, on every process it spawns inside a
# projected deployed tree. "This process is running inside a projection" is a different fact from "this
# process is a nested run" (NESTED_ENV) — a projected run is always nested, but not every nested run is a
# projection (selftest.py's own recursion-refusal child is nested, never projected).
PROJECTION_ENV = "ENGINE_DEPLOYED_PROJECTION"


def shape_verdict(root, environ, *, is_home=repo_identity.is_home_repo) -> bool:
    """True only where a case may assert against the REAL ambient repo: the construction (home) repo, and
    not inside a projected deployed tree. The deployment gate re-collects test modules inside a projected
    deployed tree (foreign origin, or an add-on declined), and a case that judges the home repo's own shape
    must skip there rather than go red against a shape it was never meant to judge
    (StarshipSuperjam/engine-template#646). `is_home` is injectable so a test can stub the predicate without
    reloading this module or touching `os.environ`."""
    return bool(is_home(root) and not environ.get(PROJECTION_ENV))


# Evaluated once at import, like every copy it replaced: nothing mutates the marker in-process, and the
# repo identity is fixed for the life of the process.
CONSTRUCTION = shape_verdict(validate.ROOT, os.environ)


def installed_module_ids() -> set:
    """The ids of the modules present on disk (installed-means-present). A manifest that is not a mapping
    is not counted rather than raised on: this helper decides whether a case runs, and a malformed manifest
    is module_coherence's own tests' finding to make, not a reason for an unrelated case to error."""
    return {m.get("id") for _p, m in module_coherence.discover_manifests() if isinstance(m, dict)}


def needs_modules(case, *ids: str, reason: str | None = None) -> None:
    """Skip `case` when any of the named modules is not installed here. These cases read files the module
    DELIVERS, so in a deployment that declined it there is no subject to assert over — the absence is the
    module's contract. A caller with its own wording passes `reason`; the default names what is missing."""
    missing = sorted(set(ids) - installed_module_ids())
    if missing:
        case.skipTest(reason or f"{', '.join(missing)} is not installed in this repository, so the file this "
                                f"case reads is legitimately absent here")
