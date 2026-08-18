#!/usr/bin/env python3
"""coordination_emitters — the best-effort emit points that turn a lifecycle moment into an advisory
coordination notice (StarshipSuperjam/engine-template#939, eADR-0043).

THE ONE LAW HERE: an emitter can NEVER affect the step it rides. Every public function is wrapped so that any
failure — no token, GitHub unreachable, a bug in this module — is swallowed and returns None. A core call
site invokes an emitter through a single guarded line; even if that guard were removed, the emitter still
cannot raise. This is what lets integration_queue / pr_reconcile / build_coordinator call these without a
behavioural risk, and it is pinned by a test that forces every emitter to throw and asserts the caller is
unaffected.

INERT ON A SINGLE-SESSION REPO: before writing anything, an emitter checks that a PEER candidate exists (more
than one open pull request against the default branch). On a solo repository there is no peer to coordinate
with, so nothing is posted — the board write itself short-circuits, not just the live doorbell.

GITHUB REACH: the caller passes its own write-capable `transport` (the github_client reader-style
`(method, path, body=None) -> (status, data)`); writes go ONLY through coordination_board (comment
endpoints), reads are GETs. No independent network client is built here — so an emit is inert in any context
that does not hand it a transport (every unit test), and it can never post to a real GitHub by surprise. No
merge, label, commit-status, or issue-body write lives here; the confinement check enforces that.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import coordination_board as board  # noqa: E402
import coordination_ledger as ledger  # noqa: E402
import coordination_notice as cn  # noqa: E402

# A module-level switch tests flip to prove the swallow: when True, the internal path raises before any write,
# and the public wrappers must still return None without propagating.
_FORCE_RAISE = False


def _now() -> str:
    import moment  # lazy: wall-clock read at the IO edge (eADR-0032)
    return moment.utc_now()


def _peer_present(transport, repo: str) -> bool:
    """True iff more than one open pull request targets the default branch — i.e. a peer candidate exists to
    coordinate with. On any read failure returns False (fail toward NOT writing on a solo/unknown repo)."""
    status, data = transport("GET", f"/repos/{repo}/pulls?state=open&per_page=100&page=1", None)
    if status >= 400 or not isinstance(data, list):
        return False
    return len(data) > 1


def _emit(transport, repo: str, pr: int, *, kind: str, event: str, verify_action: str, subject: dict,
          work_ref: dict, observed: "dict | None" = None, require_peer: bool = True) -> "str | None":
    """The shared best-effort emit: gate on a peer, render, post to the board through the caller's transport,
    record a measurement event. Returns the board outcome ('posted'/'edited'/'deduped') or None when skipped.
    Never raises — the public wrappers rely on it, and so does the caller. A None transport or blank repo is a
    silent no-op, so an emit is inert wherever the caller has no live GitHub context."""
    if _FORCE_RAISE:
        raise RuntimeError("forced failure (test): the caller must be unaffected")
    if transport is None or not repo:
        return None
    if require_peer and not _peer_present(transport, repo):
        return None
    notice = cn.render(kind=kind, event=event, emitter_work_ref=work_ref, audience={"pr": pr},
                       subject=subject, verify_action=verify_action, observed=observed, now=_now())
    client = board._Comments(repo, "", transport=transport)
    outcome = board.post_notice(client, pr, notice)
    if outcome in ("posted", "edited"):
        ledger.record_event("posted", at=notice["emitted_at"], pr=pr, kind=kind)
    return outcome


def _safe(fn):
    """Run an emit and swallow ANYTHING it raises. The single choke point the whole no-harm guarantee rests
    on; the core call sites also wrap, so this is belt and braces."""
    try:
        return fn()
    except Exception:  # noqa: BLE001 — an advisory emit never propagates into a lifecycle step
        return None


# ---- the public emit points (each best-effort; each one guarded line at the call site) --------------------

def emit_integration_admitted(transport, repo: str, pr: int) -> "str | None":
    return _safe(lambda: _emit(
        transport, repo, pr, kind="integration-notice", event="admitted", verify_action="recheck-queue",
        subject={"pr": pr}, work_ref={"pr": pr}))


def emit_integration_blocked(transport, repo: str, pr: int) -> "str | None":
    out = _safe(lambda: _emit(
        transport, repo, pr, kind="integration-notice", event="blocked", verify_action="recheck-pr-state",
        subject={"pr": pr}, work_ref={"pr": pr}))
    _safe(lambda: ledger.record_event("late-conflict", at=_now(), pr=pr))
    return out


def emit_integration_next(transport, repo: str, pr: int) -> "str | None":
    return _safe(lambda: _emit(
        transport, repo, pr, kind="integration-notice", event="next-in-queue", verify_action="recheck-queue",
        subject={"pr": pr}, work_ref={"pr": pr}))

# NOTE (StarshipSuperjam/engine-template#939): the notice vocabulary schema carries six kinds, but v1 wires ONLY the
# integration-notice emitters above — the ones whose lifecycle point (the integration queue) already holds a
# write-capable transport. The bounded-status, overlap-warning, revalidation fan-out, and handoff emitters
# need an emit point inside build_coordinator's submit/claim path, which today reaches GitHub through the `gh`
# subprocess and has no reusable transport; plumbing one there (without networking in that file's unit tests)
# is its own change. Those emitters are deferred to a tracked follow-up rather than shipped unwired as dead
# code. The receiver side (parser, board, boot relay, skills) already understands every kind, so the
# follow-up only adds emit points, never a vocabulary change.
