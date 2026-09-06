#!/usr/bin/env python3
"""Decide which self-test modules a set of changes can affect — conservatively, and never by guessing.

WHY THIS EXISTS. A full self-test run is the largest wall-clock cost in a build, and a session iterating
on one engine tool pays it over and over for a tree it has barely moved. This library answers the narrow
question that makes a cheaper iteration loop honest: given what changed, which test modules could possibly
have their verdict changed by it? The answer is consumed by `selftest.py --changed-from`; it is NEVER merge
evidence. For any change that touches the Engine, CI still runs the complete inventory against the exact
submitted head, and that run alone gates the merge. For a DEPLOYED copy's change set that lies outside
everything the Engine owns (category 4 below), CI takes its project-only arm instead and the inventory runs
nowhere — a disclosed, acknowledged narrowing (StarshipSuperjam/engine-template#883 and StarshipSuperjam/engine-template#758), bounded by the
validator suite that runs in full on every arm and by the final import re-deriving the same verdict.

THE PARTITION IS TOTAL, AND HAS EXACTLY FOUR CATEGORIES. It had two until the guard below forced a third,
and three until the deployed project-only case added a fourth; each time the earlier wording here was
corrected rather than defended:

  1. A Python file under `.engine/tools/` NARROWS the run: its reachable tests are read off the import
     graph. This is the only category that can make a run cheaper.
  2. A registered derived-artifact OUTPUT — the engine's own generated maps, named by
     `derived_output_paths()` — is EXEMPT: it neither narrows nor forces the full inventory. Without the
     exemption the feature is unreachable; see the guard's own note.

     WHAT MAKES THAT SAFE, stated precisely, because an earlier version of this comment named the wrong
     mechanism. It said the guard below puts "every one of these outputs' drift tests" into every
     focused run. That is true of the knowledge map and the self map, whose guard tests do assert the
     COMMITTED artifact is current — but a reviewer showed it is false for the assurance catalogue and
     the two provisioning registries, whose test modules only exercise synthetic trees. What actually
     polices all of them is each one's HARD drift check in the validator suite — the other registered
     validation command, which this selector never narrows and which runs in full every time. That is
     the real bound, it is stronger than the one first claimed because it covers every member rather
     than two of them, and `test_every_exempt_output_is_policed_by_a_hard_check` holds it mechanically
     so a derived member added later cannot become exempt without one.
  3. EVERYTHING else — every prose file, every governed data file, every deletion, every rename, every
     path this module does not recognise — runs the complete inventory, with a reason recorded.
  4. In a DEPLOYED copy only, a path the change classifier (`change_classification.py`) calls the
     PROJECT'S — outside every Engine corner, every declared root file and the live ownership register —
     is PROJECT-OWNED: it contributes no tests, is recorded under `project_paths`, and is otherwise
     treated like an exempt path. When nothing else changed, the classification is `project-only` and
     the selection is the derived-artifact guard alone. The predicate is INJECTED (`project_owned_factory`)
     and defaults to nothing in the home repository, where every path is the Engine's.

Totality is the property that matters and it still holds: category 3 is a catch-all, so a file kind this
module has never heard of cannot fall into a gap, and category 4 is reachable only through a classifier
that resolves every doubt to "the Engine's".

That shape was chosen after an earlier design was rejected in review, and the reason is worth keeping
here. The earlier design decided from three cooperating sources — the import graph, a scan of string
constants inside test sources, and a hand-maintained list of globally significant paths. It had a hole
that three independent reviewers found separately: the engine's governed surfaces are mostly prose and
data reached by DIRECTORY SCAN, not by name. `test_operation.py` walks the operations directory and names
no operation; `test_doc.py` walks the docs directory and names no doc; `test_knowledge.py` reaches every
governed file through the generator's own catalogue-driven scan. No import edge and no string literal can
ever connect a change in those files to the test that polices it — so the single most common defect in a
build here (edit a governed file, forget to regenerate the map that describes it) would have selected
nothing and reported green. A denylist cannot fix that, because the file that changed is never the file
the list names: the list names the regenerated OUTPUT, and the stale INPUT is what moved. Inverting the
default fixes it permanently. `TestSurfaceCatalogueTotality` in the fixture holds the inversion in place:
it walks the engine's own surface catalogue and fails if any registered surface kind other than `tool`
ever becomes positively classifiable without someone deciding to make it so.

NO SECOND IMPORT SCANNER. The engine already parses every tool and resolves both bare and package imports
— the knowledge-map generator does it to build the map. This module calls those same entry points
(`knowledge_gen.tool_module_index`, `.parse_tool_ast`, `.resolve_tool_imports`) rather than growing a
private copy, so the two answers cannot drift apart. It reads the LIVE tree rather than the committed map
for two reasons: the committed map can be stale mid-build, and a stale map under-selects, which is the one
direction this design refuses; and the map carries test-to-tool edges but no tool-to-tool edges, so the
transitive closure this needs is not derivable from it.

FAIL TOWARD MORE WORK, ALWAYS. Every failure path here resolves to the complete inventory: a git command
that fails, a file that will not parse, an import that resolves to nothing, a tool no test reaches, an
empty changed set. The cost of being wrong in that direction is a slower loop; the cost of being wrong in
the other is a green run that proved nothing.

Usage:
    uv run --directory .engine --frozen -- python tools/selftest_select.py --changed-from origin/main
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import subprocess
import sys
from typing import Optional

import knowledge_gen
import validate

SCHEMA_VERSION = "selftest-selection.v1"

TOOLS_ROOT_REL = ".engine/tools"
_TEST_PREFIX = "test_"

# The complete, closed vocabulary of reasons. A reason is a token from one of these sets and never free
# text: a reader can match on it, and a reviewer can check the set is exhaustive. Reviewers specifically
# asked for this over "a non-empty string", which any single character satisfies.
FULL_REASONS = frozenset({
    "git-unavailable",        # a git command failed, or its output could not be parsed
    "selector-unavailable",   # the selector itself could not run — distinct from git having failed
    "no-changed-paths",       # nothing changed; there is no basis to narrow anything
    "path-not-classifiable",  # a changed path is not a .py under .engine/tools/ — the catch-all
    "deleted-or-renamed",     # the import graph is built from the CURRENT tree and has no node for a gone file
    "unparseable-python",     # a changed tool will not parse, so its imports cannot be read
    "dangling-import",        # an in-repo import resolves to no file; the graph refuses to record it
    "unreached-tool",         # a changed non-test tool that no test module reaches, directly or transitively
    "derived-guard-unreachable",  # a derived artifact's generator has no test importing it; the guard set is incomplete
})

SELECTION_REASONS = frozenset({
    "changed-test-module",    # the test module itself changed
    "direct-import",          # the test module imports the changed tool directly
    "transitive-import",      # the test module reaches the changed tool through a chain of imports
    "derived-artifact-guard", # always included: ANY change can stale a derived artifact, and no import expresses that
})


class SelectionError(RuntimeError):
    """A condition the caller must not paper over — distinct from a fallback, which is a normal outcome."""


# --------------------------------------------------------------------------------------------------
# The impure half: ask git what changed. Isolated here so the classification half below is testable
# against plain directory trees with no repository at all.
# --------------------------------------------------------------------------------------------------


def _git(root: str, *args: str):
    """Run one git command; return (ok, stdout). Never raises — a failure is a fallback, not a crash."""
    try:
        proc = subprocess.run(["git", "-C", root, *args],
                              capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return False, ""
    if proc.returncode != 0:
        return False, ""
    return True, proc.stdout


def changed_paths(root: str, since: str):
    """What changed between `since` and the working tree, as repo-relative paths.

    Returns `(entries, failure)`. Each entry is `(path, status)` where status is a single letter in the
    git vocabulary — `A` added, `M` modified, `D` deleted, `R` renamed, `?` untracked. `failure` is None
    on success, or a short diagnosis; on failure `entries` is empty and the caller must run everything.

    THREE SOURCES, deliberately, because each misses something the others catch:
      * the diff from the merge base, which reports committed adds, edits, deletions and renames;
      * the working-tree status, which reports what is staged or modified but not yet committed;
      * the untracked-but-not-ignored list, WITHOUT which a session's own brand-new test module is
        invisible to the selector that is supposed to run it. A reviewer caught that omission; it is the
        single most common shape of work in this repository.
    A rename contributes BOTH sides, and either side alone is enough to force the full inventory."""
    ok, base_out = _git(root, "merge-base", since, "HEAD")
    if not ok:
        return [], f"could not find a merge base between {since!r} and HEAD"
    base = base_out.strip()
    if not base:
        return [], f"git reported no merge base between {since!r} and HEAD"

    entries: list = []

    ok, diff_out = _git(root, "diff", "--name-status", "-z", base)
    if not ok:
        return [], f"git diff against {base[:12]} failed"
    fields = [f for f in diff_out.split("\0") if f != ""]
    i = 0
    while i < len(fields):
        status = fields[i]
        letter = status[0]
        if letter in ("R", "C"):
            # A rename/copy is `R<score>\0<old>\0<new>`: both sides matter.
            if i + 2 >= len(fields):
                return [], "git diff returned a truncated rename record"
            entries.append((fields[i + 1], "R"))
            entries.append((fields[i + 2], "R"))
            i += 3
        else:
            if i + 1 >= len(fields):
                return [], "git diff returned a truncated record"
            entries.append((fields[i + 1], letter))
            i += 2

    # `-uall` matters: without it git collapses an untracked DIRECTORY to its name, so a brand-new file
    # inside a brand-new directory would never appear as a path — the fixture caught exactly that.
    ok, status_out = _git(root, "status", "--porcelain", "-z", "-uall")
    if not ok:
        return [], "git status failed"
    for record in status_out.split("\0"):
        if len(record) < 4:
            continue
        code, path = record[:2], record[3:]
        if code == "??":
            entries.append((path, "?"))
        elif "D" in code:
            entries.append((path, "D"))
        elif "R" in code:
            entries.append((path, "R"))
        else:
            entries.append((path, "M"))

    return entries, None


# --------------------------------------------------------------------------------------------------
# The pure half: given changed paths and a tree, decide. No git, no network, no clock.
# --------------------------------------------------------------------------------------------------


def is_tool_python(path: str) -> bool:
    """The one positively-classified shape: a Python file under the engine's tools directory."""
    return path.startswith(TOOLS_ROOT_REL + "/") and path.endswith(".py")


def is_test_module(path: str) -> bool:
    return is_tool_python(path) and os.path.basename(path).startswith(_TEST_PREFIX)


def module_name(path: str) -> str:
    """The discovery name for a tool path: `.engine/tools/memory/test_ledger.py` -> `memory.test_ledger`.

    Dotted for a packaged module, matching what `unittest discover` produces from the tools root, so the
    runner can compare selections against discovered cases without a second naming convention."""
    rel = path[len(TOOLS_ROOT_REL) + 1:-3]
    return rel.replace("/", ".")


def build_importer_index(root: str):
    """Reverse the tool import graph: `{imported path: {paths that import it}}`.

    Built from the LIVE tree by the knowledge generator's own extraction, so this answer and the map's
    cannot disagree. Raises `SelectionError` on any condition that makes the graph incomplete — an
    unparseable file or an in-repo import that resolves to nothing — because an incomplete reverse graph
    under-selects silently, and a caller that cannot see the whole graph must run everything."""
    tools_abs = os.path.join(root, TOOLS_ROOT_REL)
    index = knowledge_gen.tool_module_index(tools_abs)
    importers: dict = collections.defaultdict(set)
    for dirpath, dirnames, filenames in os.walk(tools_abs):
        dirnames[:] = [d for d in dirnames if d != "__pycache__" and not d.startswith(".")]
        for name in sorted(filenames):
            if not name.endswith(".py"):
                continue
            abs_path = os.path.join(dirpath, name)
            rel = os.path.relpath(abs_path, root).replace(os.sep, "/")
            try:
                tree = knowledge_gen.parse_tool_ast(abs_path)
            except OSError as exc:
                # The parser deliberately catches only SyntaxError, so an unreadable file — a dangling
                # symlink under the tools tree, say — escaped a function whose docstring promises it
                # fails closed. It now does what it says.
                raise SelectionError(("unparseable-python", f"{rel} cannot be read: {exc}")) from exc
            if tree is None:
                raise SelectionError(("unparseable-python", f"{rel} will not parse"))
            try:
                targets = knowledge_gen.resolve_tool_imports(rel, tree, index, TOOLS_ROOT_REL)
            except knowledge_gen.DanglingImportError as exc:
                # The whole message, not a prefix. Splitting on the first period looked like "take the
                # first sentence" and was not: the message opens with a repo-relative path, so the first
                # period in the string is the one inside `.engine` and the detail came out as the four
                # words before it — losing the file, the bad import, and the explanation, in exactly the
                # failure a session would most need explained.
                raise SelectionError(("dangling-import", " ".join(str(exc).split()))) from exc
            for target in targets:
                if target != rel:
                    importers[target].add(rel)
    return importers


def reaching_tests(target: str, importers: dict):
    """Every test module that reaches `target`, directly or through a chain of imports, with the reason.

    Breadth-first over the reversed graph, so the first time a test is reached is by its shortest path —
    which is what makes `direct-import` and `transitive-import` mean what they say."""
    found: dict = {}
    seen = {target}
    frontier = [(imp, "direct-import") for imp in sorted(importers.get(target, ()))]
    while frontier:
        node, reason = frontier.pop(0)
        if node in seen:
            continue
        seen.add(node)
        if is_test_module(node):
            found.setdefault(node, reason)
            # A test module is a leaf for selection purposes: nothing imports a test module, and if
            # something did, reaching the importer would not change this test's own verdict.
            continue
        for nxt in sorted(importers.get(node, ())):
            if nxt not in seen:
                frontier.append((nxt, "transitive-import"))
    return found


def derived_output_paths() -> frozenset:
    """The engine's registered generated outputs, from its own register.

    Member anchors and declared output paths — NOT "every file the engine generates", which an earlier
    wording claimed. Three entries name directories (`.codex/agents`, `.agents/skills`, the setup-route
    skills). Git never reports a directory as a changed path, so those entries are inert, and a changed
    file INSIDE one of those trees is not exempt: it falls to the catch-all and forces the full
    inventory. That is the safe direction, and it is stated rather than left for a reader to assume the
    trees are covered. Empty if the register cannot be read, which simply means nothing is exempt."""
    try:
        import derived_state
        paths = set()
        for member in derived_state.members():
            paths.add(member.path)
            for output in getattr(member, "outputs", ()):
                paths.add(str(getattr(output, "path", "")).rstrip("/"))
        return frozenset(entry for entry in paths if entry)
    except Exception:                              # noqa: BLE001 - no register means no exemption
        return frozenset()


def derived_artifact_guard(importers: dict):
    """The test modules a focused run must ALWAYS include, whatever the import graph says.

    THIS CLOSES THE LARGEST SILENT-MISS PATH — not the last one, and the earlier wording saying
    otherwise was wrong. A reviewer named the residual by planting it: a whole-tree conformance test
    that scans every tool by GLOB and asserts something about its CONTENT (see
    `test_optional_module_isolation`) has no import edge to any particular tool either, and is not a
    generated-artifact drift test, so this guard does not reach it. That class is bounded by the full
    CI inventory on the submitted head for any change that touches the Engine — a deployed copy's
    project-only change set, which cannot alter a tool's content, is bounded instead by the validator
    suite's hard checks, which run in full on every CI arm — and is disclosed rather than papered over.

    What this DOES close is the one that fires on every single edit.
    Editing any tracked file changes that file's recorded source fingerprint, which stales the engine's
    generated maps — and the tests that police that staleness (`test_knowledge` and its peers) import
    nothing the edited file touches. A reviewer proved it on the real tree: append one comment to a tool,
    and the selector returns `focused` over a set that excludes `test_knowledge` while the knowledge-drift
    check reports HARD. Both of the most ordinary shapes of work here — edit a tool, add a test — hit it.

    The dependency is real but is not an import, so it is added structurally instead. The engine already
    names every derived artifact and its generator in one place (`derived_state.members()`), so the guard
    set is DERIVED from that register rather than listed here: the tests that import each generator are
    exactly the ones that assert its output is current. A hand-written list would rot; this cannot.

    Only DIRECT test importers, deliberately. The transitive closure of these generators is 157 of 179
    modules on the real tree — it would erase the feature — while the direct set is 17, which leaves a
    leaf-tool edit selecting 18 rather than 179. The drift assertions live in the modules that import the
    generator; a module that merely reaches one transitively does not make that assertion.

    Returns `(guard paths, unreachable generator)`. A generator no test imports means the guard set is
    incomplete, and an incomplete guard is exactly the silent miss this exists to prevent — so the caller
    runs everything instead."""
    import derived_state          # imported here so the module stays importable without the register
    guard: set = set()
    for member in derived_state.members():
        generator = f"{TOOLS_ROOT_REL}/{member.tool}"
        direct = {path for path in importers.get(generator, ()) if is_test_module(path)}
        if not direct:
            return set(), generator
        guard |= direct
    return guard, None


def no_project_owned_paths():
    """The default project-owned predicate: nothing is the project's. The right answer in the home
    repository, and the answer whenever the classifier cannot be consulted."""
    return lambda _path: False


def classify(changed, importers_factory, *, guard_factory=derived_artifact_guard,
             exempt_factory=derived_output_paths, project_owned_factory=no_project_owned_paths,
             changed_from=None, git_failure=None) -> dict:
    """Decide, and return the `selftest-selection.v1` manifest. Pure with respect to git and the clock:
    every impure input — the graph, the guard, the exemption, and the project-owned predicate, whose
    live default reaches git through `select()` — arrives through an injected factory.

    `changed` is `changed_paths`' entry list. `importers_factory` is called only when there is something
    to resolve, so a classification that falls back never pays for the graph.

    ALL of `guard_factory`, `exempt_factory` and `project_owned_factory` are injected rather than reached
    for, which is what keeps this function pure. They otherwise had to import the engine's derived-artifact
    register or read the repository's identity, and a fixture over a synthetic tree would then silently
    inherit the real repository's answer. The exemption and the project-owned predicate are the two
    decisions that remove a path from consideration entirely, so they are the last things that should be
    unreachable to a test — a reviewer caught the exemption being exactly that after the first version
    injected only the guard."""
    # Recorded so a reader can tell "considered, and covered by the guard" from "never looked at". This
    # is the artifact whose stated job is explaining why a test is or is not in the selection, and an
    # exempted path was the one thing it saw and never mentioned. `project_seen` is the same record for
    # category 4, and is ALWAYS emitted (empty in the home repository) so the manifest keeps one shape.
    exempt_seen: set = set()
    project_seen: set = set()

    def full(code: str, detail: str) -> dict:
        assert code in FULL_REASONS, code
        return {
            "schema_version": SCHEMA_VERSION,
            "classification": "full",
            "changed_from": changed_from,
            "changed_paths": sorted({p for p, _ in changed}),
            "full_reason": {"code": code, "detail": detail},
            "exempt_paths": sorted(exempt_seen),
            "project_paths": sorted(project_seen),
            "selected": [],
        }

    if git_failure is not None:
        return full("git-unavailable", git_failure)
    if not changed:
        return full("no-changed-paths", "no path differs from the named commit or the working tree")

    # ONE entry per path. The three sources overlap by design — a tracked file edited but not yet
    # committed is reported by both the diff and the working-tree status — so the raw list carries
    # duplicates, and counting it produced messages that named a file twice and overstated how many
    # had changed. That bit hardest in the commonest situation there is: uncommitted mid-build work.
    statuses: dict = {}
    for path, status in changed:
        statuses.setdefault(path, set()).add(status)
    paths = sorted(statuses)

    exempt = exempt_factory()      # computed ONCE; the first version re-derived it inside a loop
    # Recorded HERE, before the deleted-or-renamed return below — the first version populated it only
    # further down, so the branch where a path was most literally waived reported nothing waived.
    exempt_seen.update(path for path in paths if path in exempt)
    # CATEGORY 4, consulted BEFORE the deleted-or-renamed check: a deleted or renamed project file cannot
    # affect the Engine either, and the predicate answers by name. A deleted Engine file never reaches
    # the predicate's "yes" (the classifier's floor matches deleted paths by name too), so the gone check
    # below still forces the full inventory for it.
    is_project_owned = project_owned_factory()
    project_seen.update(path for path in paths if path not in exempt and is_project_owned(path))
    gone = [path for path in paths if statuses[path] & {"D", "R"}
            and path not in exempt and path not in project_seen]
    if gone:
        return full("deleted-or-renamed",
                    f"{_name_paths(gone)} deleted or renamed; the import graph is built from the "
                    f"current tree and has no node for a file that is gone")
    # A generated artifact's OWN OUTPUT is exempt, and without this the feature is unreachable. The
    # loop a reviewer proved end to end: edit any tool, and its restated fingerprint stales the
    # knowledge map; the guard below now always selects the test that catches that, so the focused run
    # goes red; regenerating to clear it puts the regenerated map — a non-Python path — into the
    # changed set, so the next run classifies `full`. No state in an ordinary build iteration was left
    # where a focused run could be green.
    #
    # WHAT MAKES IT SAFE is each output's own HARD drift check in the validator suite — the other
    # registered validation command, which this selector never narrows. NOT the guard's test modules:
    # that was the first rationale and a reviewer disproved it for three of the members, whose test
    # modules only exercise synthetic trees and never assert the committed artifact is current. See
    # this module's docstring, and `test_every_exempt_output_is_policed_by_a_hard_check`, which holds
    # the true bound mechanically so a member added later cannot become exempt without one.
    # CATEGORY 2 of the three-way partition — see this module's docstring. These paths neither narrow
    # the run nor force the full inventory, and that is a deliberate, disclosed exception to the
    # otherwise two-way rule, not an oversight.
    considered = [path for path in paths if path not in exempt and path not in project_seen]
    unclassifiable = [path for path in considered if not is_tool_python(path)]
    if unclassifiable:
        return full("path-not-classifiable",
                    f"{_name_paths(unclassifiable)} not a Python file under {TOOLS_ROOT_REL}/, so "
                    f"nothing here can say which tests read it")

    try:
        importers = importers_factory()
    except SelectionError as exc:
        code, detail = exc.args[0]
        return full(code, detail)

    guard, unreachable = guard_factory(importers)
    if unreachable is not None:
        return full("derived-guard-unreachable",
                    f"no test module imports {unreachable}, so the derived-artifact guard set is "
                    f"incomplete and a stale generated map could go unnoticed")

    selected: dict = {path: "derived-artifact-guard" for path in guard}
    reached_for: dict = {}
    unreached: list = []
    for path in considered:
        if is_test_module(path):
            # Assignment, not setdefault: the guard seeds this dict first, so setdefault could not
            # displace it — and a test module that BOTH changed and sits in the guard set reported
            # only that it guards a generated map, never that it was one of the files you edited.
            selected[path] = "changed-test-module"
            continue
        reached = reaching_tests(path, importers)
        reached_for[path] = reached
        if not reached:
            unreached.append(path)
    if unreached:
        # Named as a batch for the same reason as the two above: reporting only the first sends a
        # reader round the loop once per file, discovering the next one each time.
        return full("unreached-tool",
                    f"{_name_paths(unreached)} changed but no test module imports it, directly or "
                    f"transitively")
    for path in considered:
        if is_test_module(path):
            continue
        for test_path, reason in reached_for[path].items():
            # A more specific reason wins. The guard is the weakest — it says only "this module could
            # notice a stale generated map" — so any real import relationship replaces it, and a module
            # that itself changed outranks everything.
            current = selected.get(test_path)
            if current == "changed-test-module":
                continue
            if current is None or current == "derived-artifact-guard" or reason == "direct-import":
                selected[test_path] = reason

    if not selected:
        # Unreachable given the checks above; kept because a silent empty focused selection is the one
        # outcome that could pass while running nothing.
        return full("unreached-tool", "no test module was selected for any changed path")

    # `project-only` is the honest name for a run whose every considered path was the project's: what
    # runs is the standing guard alone, and the run record carries that scope so the Build's evidence
    # and the pull-request body can say the inventory did not run. A change set with ANY Engine path
    # beside the project's is an ordinary focused (or full) run and says nothing special.
    classification = "project-only" if project_seen and not considered else "focused"
    return {
        "schema_version": SCHEMA_VERSION,
        "classification": classification,
        "changed_from": changed_from,
        "changed_paths": sorted({p for p, _ in changed}),
        "full_reason": None,
        "exempt_paths": sorted(exempt_seen),
        "project_paths": sorted(project_seen),
        "selected": [
            {"module": module_name(p), "path": p,
             "reason": {"code": selected[p],
                        "detail": _selection_detail(selected[p], p, reached_for)}}
            for p in sorted(selected)
        ],
    }


def _name_paths(paths, limit: int = 3) -> str:
    """Name a batch of paths for a human: up to `limit`, then how many more. One home for the phrasing,
    so a message cannot name only the first offender in one place and the whole batch in another."""
    shown = ", ".join(paths[:limit])
    if len(paths) > limit:
        return f"{shown} (and {len(paths) - limit} more) —"
    return f"{shown} —"


def _selection_detail(code: str, path: str, reached_for: dict) -> str:
    """One plain sentence naming WHICH changed file put this test in the selection.

    Naming the specific file matters more than it looks. With several files changed at once — the normal
    case mid-build — "reaches a changed tool" is the same sentence for every entry and tells a reader
    nothing they did not already know, which is how a traceability artifact stops being read."""
    assert code in SELECTION_REASONS, code
    if code == "changed-test-module":
        return f"{path} is itself among the changed files"
    if code == "derived-artifact-guard":
        return (f"{path} asserts a generated map is current; ANY change can stale one, and no import "
                f"expresses that dependency, so it is always included in a focused run")
    # Each changed file is described by ITS OWN relationship to this test. Lumping them under the
    # single strongest code made the manifest assert that a file was a direct import when it was only
    # reached through a chain — a false claim about the import graph, in the artifact whose whole job
    # is explaining the import graph.
    direct = sorted(c for c, reached in reached_for.items()
                    if reached.get(path) == "direct-import")
    indirect = sorted(c for c, reached in reached_for.items()
                      if reached.get(path) == "transitive-import")
    clauses = []
    if direct:
        clauses.append(f"imports {', '.join(direct)} directly")
    if indirect:
        clauses.append(f"reaches {', '.join(indirect)} through a chain of imports")
    if not clauses:
        return f"{path} reaches a changed tool"
    return f"{path} {' and '.join(clauses)}"


def project_owned_predicate(root: str):
    """The live project-owned predicate for `root`, from the change classifier: a path is the project's
    only in a DEPLOYED copy with a readable, non-degenerate register, and only when it is outside every
    floor prefix, every declared root file, the register, and every directory the register occupies.
    Any doubt — the home repository, an unreadable identity or register — makes nothing the project's,
    which is the direction that runs more. Imported lazily so a repository that cannot load the
    classifier still selects exactly as it did before the fourth category existed."""
    try:
        import change_classification as cc
        if cc.identity_of(root) != cc.IDENTITY_DEPLOYED:
            return no_project_owned_paths()
        register = cc.register_of(root)
        if register is None or cc.ENGINE_MANIFEST_REL not in register:
            return no_project_owned_paths()
        corners = cc.register_corners(register)
        return lambda path: cc.floor_hit(path) is None and not cc.register_hit(path, register, corners)
    except Exception:  # noqa: BLE001 — a classifier that cannot answer makes nothing the project's
        return no_project_owned_paths()


def select(root: str, since: str) -> dict:
    """The whole answer for one repository and one base: what changed, and what that means."""
    changed, failure = changed_paths(root, since)
    return classify(changed, lambda: build_importer_index(root),
                    project_owned_factory=lambda: project_owned_predicate(root),
                    changed_from=since, git_failure=failure)


def serialize(manifest: dict) -> str:
    """The manifest's canonical bytes. Sorted keys and a fixed separator, so the same tree and the same
    changed set produce byte-identical output and a digest of it means something."""
    return json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def digest(manifest: dict) -> str:
    return "sha256:" + hashlib.sha256(serialize(manifest).encode("utf-8")).hexdigest()


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report which self-test modules the changes since a commit can affect.")
    parser.add_argument("--changed-from", required=True, metavar="COMMIT",
                        help="the commit to compare against (its merge base with HEAD is used)")
    parser.add_argument("--root", default=validate.ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--out", default=None,
                        help="write the selection manifest here instead of standard output")
    args = parser.parse_args(argv)

    manifest = select(args.root, args.changed_from)
    text = serialize(manifest)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
