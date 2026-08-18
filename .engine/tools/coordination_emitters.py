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

GITHUB REACH: writes go ONLY through coordination_board (the one comment transport); reads (peer check,
changed files) are read-only GETs. No merge, label, commit-status, or issue-body write lives here — the
confinement check enforces that.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import coordination_board as board  # noqa: E402
import coordination_domains as domains  # noqa: E402
import coordination_ledger as ledger  # noqa: E402
import coordination_notice as cn  # noqa: E402
import github_client  # noqa: E402

USER_AGENT = "engine-coordination-emit"

# A module-level switch tests flip to prove the swallow: when True, the internal path raises before any write,
# and the public wrappers must still return None without propagating.
_FORCE_RAISE = False


def _now() -> str:
    import moment  # lazy: wall-clock read at the IO edge (eADR-0032)
    return moment.utc_now()


def _repo_token() -> tuple:
    """(repo, token) from trusted env/config, or (None, None) when either is missing — in which case every
    emit is a silent no-op (best-effort)."""
    repo = (os.environ.get("GITHUB_REPOSITORY") or "").strip()
    if not repo:
        try:
            import repo_identity  # lazy
            repo = repo_identity.origin_slug(None) or ""
        except Exception:  # noqa: BLE001
            repo = ""
    token = (os.environ.get("GITHUB_TOKEN") or "").strip()
    return (repo or None), (token or None)


def _get(repo: str, token: str):
    """A read-only GET callable (method, path) -> (status, data) over github_client. Read-only by
    construction: it never sends a body and is used only for GETs (peer check, changed files)."""
    def _reader(method, path):
        req = github_client.request(path, token, user_agent=USER_AGENT, method=method, data=None)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            return exc.code, None
        except urllib.error.URLError:
            return 599, None
    return _reader


def _peer_present(reader, repo: str) -> bool:
    """True iff more than one open pull request targets the default branch — i.e. a peer candidate exists to
    coordinate with. On any read failure returns False (fail toward NOT writing on a solo/unknown repo)."""
    status, data = reader("GET", f"/repos/{repo}/pulls?state=open&per_page=100&page=1")
    if status >= 400 or not isinstance(data, list):
        return False
    return len(data) > 1


def _emit(pr: int, *, kind: str, event: str, verify_action: str, subject: dict, work_ref: dict,
          observed: "dict | None" = None, require_peer: bool = True) -> "str | None":
    """The shared best-effort emit: resolve repo/token, gate on a peer, render, post to the board, record a
    measurement event. Returns the board outcome ('posted'/'edited'/'deduped') or None when skipped. Never
    raises — the public wrappers rely on that, and so does the caller."""
    if _FORCE_RAISE:
        raise RuntimeError("forced failure (test): the caller must be unaffected")
    repo, token = _repo_token()
    if not repo or not token:
        return None
    reader = _get(repo, token)
    if require_peer and not _peer_present(reader, repo):
        return None
    notice = cn.render(kind=kind, event=event, emitter_work_ref=work_ref, audience={"pr": pr},
                       subject=subject, verify_action=verify_action, observed=observed, now=_now())
    client = board._Comments(repo, token)
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


# ---- the public emit points (each best-effort, each one guarded line at the call site) --------------------

def emit_integration_admitted(pr: int, *, base_sha: "str | None" = None) -> "str | None":
    obs = {"head_sha": base_sha} if base_sha else None
    return _safe(lambda: _emit(
        pr, kind="integration-notice", event="admitted", verify_action="recheck-queue",
        subject={"pr": pr}, work_ref={"pr": pr}, observed=None))


def emit_integration_blocked(pr: int) -> "str | None":
    out = _safe(lambda: _emit(
        pr, kind="integration-notice", event="blocked", verify_action="recheck-pr-state",
        subject={"pr": pr}, work_ref={"pr": pr}))
    _safe(lambda: ledger.record_event("late-conflict", at=_now(), pr=pr))
    return out


def emit_integration_next(pr: int) -> "str | None":
    return _safe(lambda: _emit(
        pr, kind="integration-notice", event="next-in-queue", verify_action="recheck-queue",
        subject={"pr": pr}, work_ref={"pr": pr}))


def emit_handoff_slot_released(pr: int) -> "str | None":
    return _safe(lambda: _emit(
        pr, kind="handoff", event="slot-released", verify_action="recheck-queue",
        subject={"pr": pr}, work_ref={"pr": pr}))


def emit_revalidation_base_advanced(pr: int, *, base_sha: str) -> "str | None":
    """Emitted only when an OBSERVED base-SHA change is known (never merely because a slot was released — an
    abandon leaves the base unchanged). `base_sha` is the new protected head the emitter saw."""
    return _safe(lambda: _emit(
        pr, kind="revalidation-notice", event="base-advanced", verify_action="recheck-base",
        subject={"pr": pr}, work_ref={"pr": pr}, observed={"base_sha": base_sha}))


def emit_bounded_status(pr: int, event: str, *, paths: "list | None" = None) -> "str | None":
    subject = {"pr": pr}
    if paths:
        subject["paths"] = paths
    return _safe(lambda: _emit(
        pr, kind="bounded-status", event=event, verify_action="none",
        subject=subject, work_ref={"pr": pr}, require_peer=True))


def emit_overlap(pr: int, other_pr: int, *, paths: "list | None" = None) -> "str | None":
    """An overlap-warning posted on `pr` naming that a peer pull request (`other_pr`) touches an overlapping
    surface. Advisory only — the receiver re-computes the overlap; it is never a lock."""
    subject = {"pr": other_pr}
    if paths:
        subject["paths"] = paths
    return _safe(lambda: _emit(
        pr, kind="overlap-warning", event="domains-intersect", verify_action="recheck-overlap",
        subject=subject, work_ref={"pr": pr}))
