#!/usr/bin/env python3
"""Decide which self-test modules a set of changes can affect — conservatively, and never by guessing.

WHY THIS EXISTS. A full self-test run is the largest wall-clock cost in a build, and a session iterating
on one engine tool pays it over and over for a tree it has barely moved. This library answers the narrow
question that makes a cheaper iteration loop honest: given what changed, which test modules could possibly
have their verdict changed by it? The answer is consumed by `selftest.py --changed-from`; it is NEVER merge
evidence. CI still runs the complete inventory against the exact submitted head, and that run alone gates
the merge.

THE PARTITION IS TOTAL, AND ONLY ONE CATEGORY IS POSITIVELY CLASSIFIED. Exactly one kind of change can
narrow a run: a Python file under `.engine/tools/`, whose reachable tests are read off the import graph.
EVERYTHING else — every prose file, every governed data file, every deletion, every rename, every path this
module does not recognise — runs the complete inventory, with a reason recorded. There is no third
category and no residue, so a file kind this module has never heard of cannot fall into a gap: it falls
into the full run.

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
            tree = knowledge_gen.parse_tool_ast(abs_path)
            if tree is None:
                raise SelectionError(("unparseable-python", f"{rel} will not parse"))
            try:
                targets = knowledge_gen.resolve_tool_imports(rel, tree, index, TOOLS_ROOT_REL)
            except knowledge_gen.DanglingImportError as exc:
                raise SelectionError(("dangling-import", str(exc).split(".")[0])) from exc
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


def derived_artifact_guard(importers: dict):
    """The test modules a focused run must ALWAYS include, whatever the import graph says.

    THIS CLOSES THE LAST SILENT-MISS PATH, and it is the one the import graph can never express.
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


def classify(changed, importers_factory, *, guard_factory=derived_artifact_guard,
             changed_from=None, git_failure=None) -> dict:
    """Decide, and return the `selftest-selection.v1` manifest. Pure with respect to git and the clock.

    `changed` is `changed_paths`' entry list. `importers_factory` is called only when there is something
    to resolve, so a classification that falls back never pays for the graph. `guard_factory` is injected
    rather than reached for, which keeps this function pure — it otherwise had to import the engine's
    derived-artifact register, and a fixture over a synthetic tree would then fail closed on a register
    describing a repository it is not looking at."""
    def full(code: str, detail: str) -> dict:
        assert code in FULL_REASONS, code
        return {
            "schema_version": SCHEMA_VERSION,
            "classification": "full",
            "changed_from": changed_from,
            "changed_paths": sorted({p for p, _ in changed}),
            "full_reason": {"code": code, "detail": detail},
            "selected": [],
        }

    if git_failure is not None:
        return full("git-unavailable", git_failure)
    if not changed:
        return full("no-changed-paths", "no path differs from the named commit or the working tree")

    for path, status in sorted(changed):
        if status in ("D", "R"):
            return full("deleted-or-renamed",
                        f"{path} was deleted or renamed; the import graph is built from the current "
                        f"tree and has no node for a file that is gone")
    unclassifiable = [path for path, _ in sorted(changed) if not is_tool_python(path)]
    if unclassifiable:
        shown = ", ".join(unclassifiable[:3])
        more = f" (and {len(unclassifiable) - 3} more)" if len(unclassifiable) > 3 else ""
        return full("path-not-classifiable",
                    f"{shown}{more} — not a Python file under {TOOLS_ROOT_REL}/, so nothing here can "
                    f"say which tests read it")

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
    for path, _ in sorted(changed):
        if is_test_module(path):
            selected.setdefault(path, "changed-test-module")
            continue
        reached = reaching_tests(path, importers)
        reached_for[path] = reached
        if not reached:
            return full("unreached-tool",
                        f"{path} changed but no test module imports it, directly or transitively")
        for test_path, reason in reached.items():
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

    return {
        "schema_version": SCHEMA_VERSION,
        "classification": "focused",
        "changed_from": changed_from,
        "changed_paths": sorted({p for p, _ in changed}),
        "full_reason": None,
        "selected": [
            {"module": module_name(p), "path": p,
             "reason": {"code": selected[p],
                        "detail": _selection_detail(selected[p], p, reached_for)}}
            for p in sorted(selected)
        ],
    }


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
    causes = sorted(changed for changed, reached in reached_for.items() if path in reached)
    named = ", ".join(causes) if causes else "a changed tool"
    if code == "direct-import":
        return f"{path} imports {named} directly"
    return f"{path} reaches {named} through a chain of imports"


def select(root: str, since: str) -> dict:
    """The whole answer for one repository and one base: what changed, and what that means."""
    changed, failure = changed_paths(root, since)
    return classify(changed, lambda: build_importer_index(root),
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
