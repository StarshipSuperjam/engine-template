#!/usr/bin/env python3
"""What a repair round actually touched, by kind of surface.

A Build's repair rounds are judged by the orchestrator, never by this module. What this module supplies is
the *evidence* that judgment reads: for one round's increment — the two-dot diff from the round's anchor to
its head — which files moved, sorted into four kinds, and how much churn each kind carries.

The four kinds, in strict precedence order:

  guarded    a file the weakening guard protects (the enforcement floor, `.github/workflows/`,
             `.engine/check/`, a check rule's `params.script`, a module-provided check-kind callable, or a
             path this deployment declared in its own instance floor). Guarded wins over derived
             deliberately: a *regenerated* guarded file still deserves a reader's attention, so the
             precedence fails toward the more serious kind.
  derived    a file the derived-state registry owns — an exact `file` output, an EXCLUSIVE `tree` output by
             directory boundary, or a `dynamic` member's concrete output resolved from this tree. This is
             exactly the predicate `sync-artifacts` uses to decide a regenerated path is legitimate, reused
             rather than restated so a routes regeneration is described as generated in both places.
  docs       reader-facing documentation, and ONLY that: `docs/` and `.engine/docs/`. Governing prose —
             `.engine/operations/`, `.engine/contracts/`, `.engine/conduct/`, `CLAUDE.md`, the reviewer and
             skill mandates — is NOT documentation. It is the text that governs how the engine behaves, so
             it classifies `authored` and is read as seriously as code.
  authored   everything else. Unmatched is authored, never a fifth silent bucket.

Nothing here decides whether a round is counted, whether it should be re-reviewed, or how deeply. Those are
the coordinator's and the orchestrator's business (eADR-0041 BC-16). This module measures and refuses; when
it cannot measure, it raises `DivergenceError` rather than returning a defaulted or fabricated result, so a
caller can never mistake a git failure for a quiet, empty diff.
"""
from __future__ import annotations

import os
import subprocess

KINDS = ("guarded", "derived", "docs", "authored")

# Reader-facing documentation, exhaustively. Deliberately narrow — see the module docstring on why
# governing prose is excluded.
DOCS_PREFIXES = ("docs/", ".engine/docs/")

_DERIVE = object()  # sentinel: derive the guard sets from `root` (the default)


class DivergenceError(Exception):
    """The increment could not be measured. Never raised to mean "nothing changed"."""


def _run_git(argv: list, root: str) -> str:
    try:
        result = subprocess.run(argv, cwd=root, text=True, capture_output=True, check=False)
    except OSError as exc:
        raise DivergenceError(f"could not run git in {root}: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout or "no diagnostic").strip()
        raise DivergenceError(f"{' '.join(argv[:4])} failed: {detail}")
    return result.stdout


def numstat_rows(root, base: str, head: str, runner=None) -> list:
    """`(added, deleted, path)` for every file in the TWO-DOT diff `base..head`, run in `root`.

    Two-dot, not three: a repair round's increment is the movement between two commits on the same branch,
    and a merge-base-relative diff would re-count everything the round did not touch.

    `-z` keeps paths with newlines or non-ASCII bytes intact. Its record shape differs for renames: an
    ordinary record is one NUL-terminated `added\\tdeleted\\tpath`, while a rename emits
    `added\\tdeleted\\t` followed by the old path and the new path as two further NUL-terminated fields. A
    rename is attributed to its NEW path — that is the file the reader will open.

    A binary file's counts render as `-`, which counts as churn 0 (scope_profile's convention: line churn is
    not defined for bytes, and inventing a number would put a fictional figure in front of the operator).
    """
    argv = ["git", "-c", "core.quotepath=false", "diff", "--numstat", "-z",
            "--end-of-options", f"{base}..{head}"]
    out = runner(argv, str(root)) if runner is not None else _run_git(argv, str(root))
    fields = out.split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    rows = []
    index = 0
    while index < len(fields):
        parts = fields[index].split("\t")
        if len(parts) != 3:
            raise DivergenceError(f"unparseable git numstat record: {fields[index]!r}")
        added, deleted, path = parts
        index += 1
        if path == "":                       # a rename: the old and new paths follow as separate fields
            if index + 1 >= len(fields):
                raise DivergenceError("truncated git numstat rename record")
            path = fields[index + 1]         # the NEW path — fields[index] is the old one
            index += 2
        rows.append((int(added) if added.isdigit() else 0,
                     int(deleted) if deleted.isdigit() else 0,
                     path))
    return rows


def _dynamic_outputs(derived_state, root: str) -> set:
    """A `dynamic` member's concrete outputs, resolved from this tree — the second half of sync-artifacts'
    declared-output predicate. `owner_of` reads only the STATIC outputs, so without this a regenerated setup
    route would be described to the operator as authored work."""
    return {output.path for member in derived_state.MEMBERS if member.dynamic
            for output in derived_state._concrete_outputs(member, root)}


def classify(root, base: str, head: str, *, derived_scripts=_DERIVE, instance_guards=_DERIVE,
             runner=None) -> dict:
    """Sort one increment's changed files into the four kinds, with per-kind churn.

    `root` is explicit and governs everything read: the git command runs there, and the guard sets are
    derived from that tree's own `.engine/check/` and instance declaration. The coordinator passes its ROOT
    at call time rather than letting this module infer a tree from its own file location, so the tree
    measured is always the tree the caller meant.

    The guard-script set and the instance pair are derived ONCE here and threaded through every
    `is_guardrail` call — the `flagged_changes` pattern, one disk scan per round rather than one per file.

    Returns `{"anchor", "head", "files": {kind: [paths]}, "churn": {kind: int}, "total_churn": int}`.
    Raises `DivergenceError` if the increment cannot be measured.
    """
    root = str(root)
    try:
        import derived_state
        import weakening_guard
    except Exception as exc:  # noqa: BLE001 — an unimportable seam is a failure to measure, not an empty diff
        raise DivergenceError(f"could not load the classification seams: {exc}") from exc

    rows = numstat_rows(root, base, head, runner=runner)

    try:
        if derived_scripts is _DERIVE:
            derived_scripts = weakening_guard._derive_check_scripts(
                os.path.join(root, ".engine", "check"))
        if instance_guards is _DERIVE:
            instance_guards = weakening_guard._read_instance_guards(
                os.path.join(root, weakening_guard.INSTANCE_DECL_REL))
        dynamic_files = _dynamic_outputs(derived_state, root)
    except DivergenceError:
        raise
    except Exception as exc:  # noqa: BLE001 — same rule: refuse legibly rather than measure against a guess
        raise DivergenceError(f"could not derive the classification sets: {exc}") from exc

    files = {kind: [] for kind in KINDS}
    churn = {kind: 0 for kind in KINDS}
    for added, deleted, path in rows:
        if weakening_guard.is_guardrail(path, derived_scripts, instance_guards):
            kind = "guarded"
        elif derived_state.owner_of(path) is not None or path in dynamic_files:
            kind = "derived"
        elif path.startswith(DOCS_PREFIXES):
            kind = "docs"
        else:
            kind = "authored"
        files[kind].append(path)
        churn[kind] += added + deleted

    for paths in files.values():
        paths.sort()
    return {"anchor": base, "head": head, "files": files, "churn": churn,
            "total_churn": sum(churn.values())}
