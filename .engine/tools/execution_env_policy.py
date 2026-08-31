#!/usr/bin/env python3
"""Execution-environment policy — a reusable, allowlist-first way to run and reap a supervised child process.

Three responsibilities, kept OUT of the qualification harness so they can be tested and reused on their own:

  1. ``allowlist_environment(...)`` — build a child environment from a NAMED allowlist of keys ONLY, never the
     ambient ``os.environ``. It copies exactly the keys it is told to keep; nothing else crosses. This is the
     seam the spike's environment witness checks (child environment equals the allowlist), and the seam that
     keeps no repository publisher credential in an executor's environment.
  2. ``launch(...)`` — start a child in its OWN process group so the whole tree can be signalled as a unit.
  3. ``terminate_tree(...)`` — signal the process group and VERIFY the tree is gone, returning a witness of what
     actually happened rather than assuming a clean kill.

This module builds and supervises processes. It enforces no OS-level isolation and makes no such claim: in
this Build containment is OBSERVED, not enforced (the operator accepted that for this proof-of-concept round).
The allowlist witness proves the Engine did not PROVIDE a credential — never that a credential is unreachable,
since publish authority remains reachable on disk regardless of the child environment.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time


def allowlist_environment(allowlist, source=None) -> dict:
    """A child environment containing EXACTLY the allowlisted keys present in ``source`` (default
    ``os.environ``), and nothing else. ``allowlist`` is an iterable of variable names — the named keep-list. A
    key absent from ``source`` is simply not set (never invented). The returned dict IS the whole child
    environment, so equality against the allowlist is exactly what the environment witness asserts."""
    src = os.environ if source is None else source
    return {key: src[key] for key in allowlist if key in src}


def launch(argv, *, env, cwd=None, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE):
    """Start ``argv`` in a fresh process group (``start_new_session=True``) so its whole tree can be reaped as a
    unit. The child receives EXACTLY ``env`` — a caller passes an ``allowlist_environment`` result, never
    ``os.environ`` — copied so a later mutation of the caller's dict cannot reach the running child."""
    return subprocess.Popen(
        list(argv), env=dict(env), cwd=cwd,
        stdin=stdin, stdout=stdout, stderr=stderr, start_new_session=True)


def _pgid(proc) -> "int | None":
    try:
        return os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError):
        return None


def _group_gone(pgid) -> bool:
    """Whether no process remains in ``pgid`` — a signal-0 to the group that raises ProcessLookupError."""
    if pgid is None:
        return True
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def terminate_tree(proc, *, grace_seconds: float = 2.0, poll_seconds: float = 0.02) -> dict:
    """Signal the child's whole process group (SIGTERM, then SIGKILL after a grace period) and VERIFY the
    result. Returns a witness dict — the pgid signalled, whether the leader exited, whether SIGKILL was needed,
    and whether the group is gone (a signal-0 to the group raising ProcessLookupError) — which a caller records
    as tree-reap evidence. It never assumes the kill worked."""
    pgid = _pgid(proc)
    witness = {"pid": proc.pid, "pgid": pgid, "leader_exited": False,
               "escalated_to_kill": False, "group_reaped": False}

    def _signal(sig):
        if pgid is not None:
            try:
                os.killpg(pgid, sig)
                return
            except ProcessLookupError:
                return
            except PermissionError:
                # The group is already gone and its pgid has been reused by a process this session does not
                # own; signalling it would target someone else's process, so we must NOT. Treat as
                # already-reaped rather than crashing the teardown.
                return
        try:
            proc.send_signal(sig)
        except ProcessLookupError:
            pass

    _signal(signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(poll_seconds)
    if proc.poll() is None:
        witness["escalated_to_kill"] = True
        _signal(signal.SIGKILL)
        try:
            proc.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            pass
    else:
        proc.wait()
    witness["leader_exited"] = proc.poll() is not None
    witness["group_reaped"] = witness["leader_exited"] and _group_gone(pgid)
    # Teardown owns the child's pipes: close any still-open standard stream so a reaped process leaves no
    # dangling file handle behind.
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is not None and not stream.closed:
            try:
                stream.close()
            except OSError:
                pass
    return witness
