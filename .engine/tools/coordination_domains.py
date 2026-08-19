#!/usr/bin/env python3
"""coordination_domains — a pull request's CHANGE DOMAIN and lock-free overlap between two of them
(StarshipSuperjam/engine-template#939).

WHAT A DOMAIN IS. The set of paths a unit of work touches, in two parts: the DECLARED domain (the path
patterns the durable build plan reserves — future, not-yet-pushed work) and the ACTUAL domain (the files the
pull request has already changed). Their union is the domain. Declared covers a build before it has pushed
anything; actual covers drift and non-Build pull requests.

OVERLAP IS ADVISORY, NEVER A LOCK. Two overlapping domains produce an overlap-warning notice — a prompt for
the operator or the two sessions to sequence or consult, never an admission gate (eADR-0043: no locking, no
admission control by overlap). The binding protections are unchanged: the per-branch non-fast-forward push
rejection, the strict freshness ruleset at merge, and pr_reconcile's authored-conflict refusal.

REUSED PRIMITIVES (StarshipSuperjam/engine-template#939 architecture review). Two DIFFERENT questions need two different primitives:
`build_coordinator_dag.paths_conflict` answers set-of-patterns vs set-of-patterns (declared vs declared, the
pre-pull-request case); `build_coordinator_dag.path_within_declared` answers one concrete file vs a pattern
set (an already-changed file vs a declared domain). Using one for the other would be wrong, so overlap()
composes both, plus a concrete-vs-concrete file-set intersection.

GITHUB REACH. The only GitHub call here is a READ-ONLY GET of the pull request's changed files — permitted by
the confinement whitelist (comment post/patch, plus read-only reads). It writes nothing.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_coordinator_dag as dag  # noqa: E402  (path pattern primitives)

_FILES_PAGE = 100  # the GitHub pulls/files page size; we read one page and disclose truncation beyond it


def declared_paths_from_plan(plan: dict) -> list:
    """The declared path patterns from a build plan dict — the union of every work item's `paths`. Pure (no
    network): the caller resolves the plan (from the durable Issue block) and hands it here. Returns a sorted,
    de-duplicated list; an absent/edge plan yields []."""
    out = set()
    for item in (plan or {}).get("work_items", []) or []:
        for p in (item or {}).get("paths", []) or []:
            if isinstance(p, str) and p:
                out.add(p)
    return sorted(out)


def changed_files(reader, repo: str, pr: int) -> tuple:
    """The pull request's changed file paths and a truncation flag, via a read-only GET (injectable `reader`,
    a callable (method, path) -> (status, data), so tests drive it offline). Reads ONE page; if the page is
    full the domain is marked truncated (a very large pull request is disclosed, never silently under-read).
    Returns (files, truncated); on a read failure returns ([], False) — a domain the caller treats as unknown,
    never a false 'touches nothing'."""
    status, data = reader("GET", f"/repos/{repo}/pulls/{pr}/files?per_page={_FILES_PAGE}&page=1")
    if status >= 400 or not isinstance(data, list):
        return [], False
    files = [f.get("filename") for f in data if isinstance(f, dict) and f.get("filename")]
    return files, (len(data) >= _FILES_PAGE)


def domain(reader, repo: str, pr: int, *, declared: "list | None" = None) -> dict:
    """A pull request's full change domain: {declared: [patterns], actual: [files], truncated: bool}.
    `declared` is the pattern list the caller resolved from the durable plan (or None -> empty)."""
    actual, truncated = changed_files(reader, repo, pr)
    return {"declared": list(declared or []), "actual": actual, "truncated": truncated}


def overlaps(a: dict, b: dict) -> bool:
    """Whether two change domains touch a common surface — the lock-free overlap test. Composes the two DAG
    primitives at the right granularity: declared-vs-declared as pattern sets, each domain's actual files
    against the other's declared patterns, and a concrete file-set intersection. Conservative by construction
    (the primitives over-report rather than miss a real collision), which is correct for an advisory warning."""
    da, aa = a.get("declared", []), a.get("actual", [])
    db, ab = b.get("declared", []), b.get("actual", [])
    if da and db and dag.paths_conflict(da, db):
        return True
    if any(dag.path_within_declared(f, db) for f in aa if f):
        return True
    if any(dag.path_within_declared(f, da) for f in ab if f):
        return True
    if set(f for f in aa if f) & set(f for f in ab if f):
        return True
    return False
