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

import json
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
    """`(added, deleted, path, previous_path)` for every file in the TWO-DOT diff `base..head`, in `root`.

    Two-dot, not three: a repair round's increment is the movement between two commits on the same branch,
    and a merge-base-relative diff would re-count everything the round did not touch.

    `-z` keeps paths with newlines or non-ASCII bytes intact. Its record shape differs for renames: an
    ordinary record is one NUL-terminated `added\\tdeleted\\tpath`, while a rename emits
    `added\\tdeleted\\t` followed by the old path and the new path as two further NUL-terminated fields. A
    rename is attributed to its NEW path — that is the file the reader will open — but the OLD path is
    carried alongside it, because a rename AWAY from a serious surface is as serious as a rename into one
    and classifying only the destination would report a guarded file renamed to an ordinary name as
    ordinary authored work. `previous_path` is `""` for everything that is not a rename.

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
        previous = ""
        if path == "":                       # a rename: the old and new paths follow as separate fields
            if index + 1 >= len(fields):
                raise DivergenceError("truncated git numstat rename record")
            previous, path = fields[index], fields[index + 1]
            index += 2
        rows.append((int(added) if added.isdigit() else 0,
                     int(deleted) if deleted.isdigit() else 0,
                     path, previous))
    return rows


def _guard_sets_at(weakening_guard, root: str, commit: str) -> tuple:
    """The check-script set and instance pair as they stood AT `commit`, read out of git rather than off
    disk.

    Needed because the two halves of a classification come from different points in time: the file list is
    the `anchor..head` diff, while the guard sets would otherwise be read from the working tree at head. A
    round that de-registers a guard — deleting a check rule's `params.script`, or dropping a path from the
    instance declaration — would then make every later round's churn on that file classify as ordinary
    authored work, exactly when a reader most needs to be told enforcement logic moved. The caller unions
    this with the head sets, so a path guarded at EITHER end classifies guarded: the same fail-toward-the-
    serious-kind direction the precedence order takes.

    Returns `(scripts, instance_pair, complete)`. `scripts` is None on any failure — `weakening_guard`'s own
    fail-safe convention, meaning "guard all of `.engine/tools/`". The instance half has NO safe substitute:
    a declaration that cannot be read cannot be guessed at, and an empty pair silently understates the
    guarded set. So the read's success is reported as `complete`, and a caller that could not read the
    anchor's registrations tells the operator so rather than presenting a possibly-short answer as fact.

    Each blob is read with its own `git show`, whose path travels in ARGV where a newline is just a byte.
    A batched reader was tried here and reverted: `git cat-file --batch` takes newline-delimited requests,
    so one check rule with a newline in its name split into two requests, desynchronised the reply stream,
    and handed a real path another file's content — silently, with the read still reporting itself clean.
    That is the one failure this module must never have, and it was bought for two thirds of a second on an
    operation that runs at most six times in a Build. The cost is stated rather than optimised away.
    """
    try:
        listing = _run_git(["git", "ls-tree", "-z", "--name-only", commit, "--", ".engine/check/"], root)
    except DivergenceError:
        return None, (set(), ()), False
    scripts: set = set()
    for name in [entry for entry in listing.split("\0") if entry.endswith(".json")]:
        try:
            data = json.loads(_run_git(["git", "show", f"{commit}:{name}"], root))
        except (DivergenceError, ValueError):
            return None, (set(), ()), False      # all-or-nothing, like _derive_check_scripts
        script = (data.get("params") or {}).get("script")
        if isinstance(script, str) and script.strip():
            scripts.add(script)
    try:
        declared_body = _run_git(["git", "show", f"{commit}:{weakening_guard.INSTANCE_DECL_REL}"], root)
    except DivergenceError:
        # ABSENT is the normal steady state (most deployments never declare one) and is not a degradation:
        # nothing was declared, so the empty pair is the true answer rather than a short one. git cannot
        # tell us "absent" apart from "unreadable" here, and absent is overwhelmingly the common case, so
        # this resolves toward it -- the head-side read still covers a declaration that exists.
        return scripts, (set(), ()), True
    try:
        declared = json.loads(declared_body)
        if not isinstance(declared, dict):
            raise ValueError("not an object")
        # Mirrors weakening_guard._read_instance_guards' defensive parse. Read here rather than imported
        # because that reader takes a filesystem path and this one reads a git object; weakening_guard is a
        # hard-floor guarded file, so a sibling reaches for its shape rather than editing it.
        exact = {p for p in declared.get("guarded_paths", []) if isinstance(p, str) and p.strip()}
        prefixes = tuple(p for p in declared.get("guarded_prefixes", [])
                         if isinstance(p, str) and p.strip() and p.strip() not in {".", "/", "./"})
    except (ValueError, AttributeError, TypeError):
        return scripts, (set(), ()), False       # PRESENT but unreadable: a real degradation, disclosed
    return scripts, (exact, prefixes), True


def _dynamic_outputs(derived_state, root: str) -> set:
    """A `dynamic` member's concrete outputs, resolved from this tree — the second half of sync-artifacts'
    declared-output predicate. `owner_of` reads only the STATIC outputs, so without this a regenerated setup
    route would be described to the operator as authored work."""
    return {output.path for member in derived_state.MEMBERS if member.dynamic
            for output in derived_state._concrete_outputs(member, root)}


def classify(root, base: str, head: str, *, derived_scripts=_DERIVE, instance_guards=_DERIVE,
             guard_reference: str | None = None, runner=None) -> dict:
    """Sort one increment's changed files into the four kinds, with per-kind churn.

    `root` is explicit and governs everything read: the git command runs there, and the guard sets are
    derived from that tree's own `.engine/check/` and instance declaration. The coordinator passes its ROOT
    at call time rather than letting this module infer a tree from its own file location, so the tree
    measured is always the tree the caller meant.

    The guard-script set and the instance pair are derived ONCE here and threaded through every
    `is_guardrail` call — the `flagged_changes` pattern, one disk scan per round rather than one per file.

    `guard_reference` is an optional third commit whose guard registrations are unioned in as a FLOOR —
    the caller's "everything guarded since here stays guarded". A repair loop passes the commit the
    deliverable review stood on, so a guard de-registered in round 2 cannot make round 5's churn on that
    file read as ordinary authored work; without it the union reaches back exactly one round.

    Returns `{"anchor", "head", "files": {kind: [paths]}, "churn": {kind: int}, "total_churn": int,
    "guards_read": bool}`. `guards_read` is False when a guard registration that WAS present could not be
    read, which means the guarded set may be understated and the caller owes the reader that caveat.
    Raises `DivergenceError` if the increment cannot be measured at all.
    """
    root = str(root)
    try:
        import derived_state
        import weakening_guard
    except Exception as exc:  # noqa: BLE001 — an unimportable seam is a failure to measure, not an empty diff
        raise DivergenceError(f"could not load the classification seams: {exc}") from exc

    rows = numstat_rows(root, base, head, runner=runner)

    try:
        # Explicitly supplied sets govern BOTH ends: a caller that names the guard sets is describing one
        # world, not two, and a test that pinned head-side membership should not have an anchor-side read
        # appear behind it.
        derive_scripts = derived_scripts is _DERIVE
        derive_instance = instance_guards is _DERIVE
        if derive_scripts:
            derived_scripts = weakening_guard._derive_check_scripts(
                os.path.join(root, ".engine", "check"))
        if derive_instance:
            instance_guards = weakening_guard._read_instance_guards(
                os.path.join(root, weakening_guard.INSTANCE_DECL_REL))
        # One batched read per REFERENCED COMMIT, and only on the derive path. A caller that pinned both
        # sets is describing one world, not several, and gets no extra reads behind its back.
        extra, guards_read = [], True
        if derive_scripts or derive_instance:
            for commit in [base] + ([guard_reference] if guard_reference
                                    and guard_reference != base else []):
                scripts_at, instance_at, complete = _guard_sets_at(weakening_guard, root, commit)
                extra.append((scripts_at if derive_scripts else derived_scripts,
                              instance_at if derive_instance else instance_guards))
                guards_read = guards_read and complete
        dynamic_files = _dynamic_outputs(derived_state, root)
    except DivergenceError:
        raise
    except Exception as exc:  # noqa: BLE001 — same rule: refuse legibly rather than measure against a guess
        raise DivergenceError(f"could not derive the classification sets: {exc}") from exc

    def guarded(path: str) -> bool:
        """Guarded at ANY of the commits consulted. The head sets alone would let a round that
        de-registered a guard hide later churn on that file; the anchor sets alone would miss a guard added
        by this very round; the optional reference floor carries the property back past one round."""
        if weakening_guard.is_guardrail(path, derived_scripts, instance_guards):
            return True
        return any(weakening_guard.is_guardrail(path, scripts, instance) for scripts, instance in extra)

    files = {kind: [] for kind in KINDS}
    churn = {kind: 0 for kind in KINDS}
    for added, deleted, path, previous in rows:
        # A rename is judged on BOTH names: renaming a guarded file to an ordinary one is a guarded event.
        if guarded(path) or (previous and guarded(previous)):
            kind = "guarded"
        elif (derived_state.owner_of(path) is not None or path in dynamic_files
              or (previous and (derived_state.owner_of(previous) is not None
                                or previous in dynamic_files))):
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
            "total_churn": sum(churn.values()), "guards_read": guards_read}
