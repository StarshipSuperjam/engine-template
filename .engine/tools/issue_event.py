#!/usr/bin/env python3
"""The shared GitHub issues-event parsing boundary — one defensive reader for the on:issues backstops.

WHAT THIS IS. The `on:issues` CI nets (issue_conformance_ci, issue_kind_label) each run from a GitHub Actions
`issues` event and must read the same handful of things from it, the same safe way: the event JSON at
`$GITHUB_EVENT_PATH`, the `.issue` dict with a numeric id, that issue's label names, and the
`GITHUB_REPOSITORY`/`GITHUB_TOKEN` credentials. This module single-homes that event-mechanics layer so a defect
in the safe-reading contract is fixed once, below every backstop, and a THIRD future on:issues consumer can
reuse the parser without copying either backstop's policy.

WHAT STAYS WITH THE CALLER — MECHANICS HERE, POLICY THERE. Only the parsing lives here; never a scope or fail
decision. Each backstop keeps its own SCOPE predicate (conformance acts on engine-labelled Issues only;
kind-label acts on any Issue with a mappable title kind) and its own FAIL contract (what a missing event or
missing credentials MEANS — a quiet no-op or a visible red run). So `issue_or_none` is deliberately SCOPE-FREE
(a numeric id is all it asserts), and `resolve_repo_token` returns the pair and decides nothing — it never
prints and never exits.

READ FROM JSON, NEVER SHELL-INTERPOLATED. The event is read from the file at `$GITHUB_EVENT_PATH` (the safe
pattern shared with validate.get_pr_body), never from a shell-interpolated argument, so an attacker-controllable
title/body/label never reaches a shell. This module only PARSES: it applies no label and runs no command, so it
adds no new title→shell or title→value path.

Dependency-light by design (stdlib `os`/`json` only), so every per-issue CI hot path can import it without
dragging a heavier stack in.

GENERIC CORE, ISSUE-SPECIFIC EDGE. Two of these primitives — `load_event` and `resolve_repo_token` — are
event-type-agnostic: a `pull_request`-event tool would read `$GITHUB_EVENT_PATH` and the repo/token pair the
same way. Only `labels_of`/`issue_or_none` are issue-shaped. Scoping a general Actions-event framework OUT is
deliberate here; but if a broader event home is ever built, those two are the generic core to lift into it —
noted so a future PR-event tool finds them rather than re-copying `load_event` and reintroducing the drift this
module exists to remove.
"""
from __future__ import annotations

import json
import os


def load_event():
    """The issue event JSON from $GITHUB_EVENT_PATH (read from the file, never a shell-interpolated argument),
    or None when unavailable/unreadable (a local run, a partial event) → the caller no-ops quietly."""
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def labels_of(issue: dict) -> list:
    """The label names on an issue event payload (`.issue.labels[].name`), defensively."""
    return [lab.get("name") for lab in (issue.get("labels") or []) if isinstance(lab, dict)]


def issue_or_none(event):
    """The issue dict from an issues-event payload IFF it carries a numeric id; else None. SCOPE-FREE — it
    imposes no label or title policy, so each backstop layers its own scope on top (the conformance net adds
    the engine-label gate; the kind-label net adds the mappable-title check). Defensive against a partial
    event (a non-dict event, a missing/`None` issue, or a non-integer number → None)."""
    if not isinstance(event, dict):
        return None
    issue = event.get("issue")
    if not isinstance(issue, dict) or not isinstance(issue.get("number"), int):
        return None
    return issue


def resolve_repo_token():
    """The (GITHUB_REPOSITORY, GITHUB_TOKEN) pair from the environment — each None when unset. This RESOLVES
    only; it does not decide what a missing value means. Each caller keeps that policy local (an out-of-scope
    no-op or a visible red run), so this never prints and never exits."""
    return os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
