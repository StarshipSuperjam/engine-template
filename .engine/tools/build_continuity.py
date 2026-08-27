"""One atomic corrective Stop continuation for an actionable Build epoch."""
from __future__ import annotations
import hashlib
import json
import os
import subprocess
from pathlib import Path

import build_coordinator_core as core
import build_state_store
import plan_store

SCHEMA = Path(__file__).resolve().parents[1] / "schemas" / "build-continuity-state.v1.json"
NAME = "continuity.json"

def _digest(value):
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

def _head():
    return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()

def _fingerprint():
    return hashlib.sha256(subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True).stdout.encode()).hexdigest()

def _identity(state, worktree):
    return {"worktree": str(Path(worktree).resolve()), "plan_id": state["plan"]["plan_id"], "sealed_digest": state["plan"]["sealed_digest"]}

def progress_token(state, worktree):
    """Only work evidence counts; revisions, checks, receipts, and diagnostics never re-arm a Stop."""
    work = {key: {"integration": value.get("integration"), "result": (value.get("latest_result") or {}).get("artifact_digest")}
            for key, value in (state.get("work") or {}).items()}
    return _digest({"identity": _identity(state, worktree), "head": _head(), "fingerprint": _fingerprint(),
                    "progress": state.get("progress"), "work": work})

def _path(store):
    return store.path.with_name(NAME)

def _condition(state):
    value = state.get("terminal_condition")
    if not value:
        return None
    # The state schema checks the closed shape; only a source reference is accepted, never prose.
    if not value.get("source", {}).get("reference"):
        return None
    return value

def decide(payload, worktree=None):
    """Return proceed|block. Resolution errors are intentionally fail-open and machine-readable."""
    worktree = Path(worktree or os.getcwd()).resolve()
    try:
        store = build_state_store.resolve_for_worktree(worktree, lambda _s: Path(__file__).resolve().parents[1] / "schemas" / "build-state.v2.json")
        state = store.read()
        if not state.get("approval") or state.get("submission") != "draft":
            return {"action": "proceed", "code": "not-actionable"}
        held = _condition(state)
        if held:
            return {"action": "proceed", "code": "terminal-" + held["kind"]}
        identity, token, path = _identity(state, worktree), progress_token(state, worktree), _path(store)
        with core.exclusive_lock(path.with_name(path.name + ".lock")):
            existing = core.json_file(path) if path.exists() else None
            if existing and existing.get("identity") == identity and existing.get("progress_token") == token:
                if not existing.get("diagnostic_emitted"):
                    existing["diagnostic_emitted"] = True
                    core.atomic_write(path, json.dumps(existing, sort_keys=True) + "\n", durable=True, mode=plan_store.FILE_MODE)
                    return {"action": "proceed", "code": "budget-spent", "diagnostic": True}
                return {"action": "proceed", "code": "budget-spent"}
            record = {"schema_version": "build-continuity-state.v1", "identity": identity, "progress_token": token, "claimed": True, "diagnostic_emitted": False}
            core.validate(record, SCHEMA)
            core.atomic_write(path, json.dumps(record, sort_keys=True) + "\n", durable=True, mode=plan_store.FILE_MODE)
        return {"action": "block", "reason": "This Build still has actionable work. Continue the next planned step; do not send a status-only handoff."}
    except Exception:
        return {"action": "proceed", "code": "continuity-unavailable"}
