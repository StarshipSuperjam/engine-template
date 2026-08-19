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
    # Confine the transport to reads + comment writes at the coordination boundary, so no coordination code
    # path (now or after a future edit) can reach a merge/label/status/body endpoint (eADR-0043 law 3).
    transport = board.comment_only(transport)
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


def emit_handoff(transport, repo: str, pr: int, event: str) -> "str | None":
    """A prerequisite/handoff notice on `pr` (ready-for-review, slot-released, node-abandoned,
    work-abandoned) — a peer waiting on this work re-checks the pull request's state."""
    return _safe(lambda: _emit(
        transport, repo, pr, kind="handoff", event=event, verify_action="recheck-pr-state",
        subject={"pr": pr}, work_ref={"pr": pr}))


def emit_bounded_status(transport, repo: str, pr: int, event: str, *, paths: "list | None" = None) -> "str | None":
    """A declarative status notice (work-declared / work-completed) on `pr`. `paths` (optional) names the
    change domain the session declared, so a peer can see the surface without a request round-trip."""
    subject = {"pr": pr}
    if paths:
        subject["paths"] = list(paths)[:20]
    return _safe(lambda: _emit(
        transport, repo, pr, kind="bounded-status", event=event, verify_action="none",
        subject=subject, work_ref={"pr": pr}))


def emit_revalidation_base_advanced(transport, repo: str, pr: int, *, base_sha: str) -> "str | None":
    """Emitted only when an OBSERVED base-SHA change is known (never merely because a slot was released — an
    abandon leaves the base unchanged). `base_sha` is the new protected head the emitter saw."""
    return _safe(lambda: _emit(
        transport, repo, pr, kind="revalidation-notice", event="base-advanced", verify_action="recheck-base",
        subject={"pr": pr}, work_ref={"pr": pr}, observed={"base_sha": base_sha}))


def emit_overlap(transport, repo: str, pr: int, other_pr: int, *, paths: "list | None" = None) -> "str | None":
    """An overlap-warning on `pr` naming that a peer pull request (`other_pr`) touches an overlapping surface.
    Advisory only — the receiver re-computes the overlap; it is never a lock."""
    subject = {"pr": other_pr}
    if paths:
        subject["paths"] = list(paths)[:20]
    return _safe(lambda: _emit(
        transport, repo, pr, kind="overlap-warning", event="domains-intersect",
        verify_action="recheck-overlap", subject=subject, work_ref={"pr": pr}))


# ---- roster-driven scans (read peers, compute overlap, fan out) — each fully best-effort -----------------

def _open_prs(transport, repo: str) -> list:
    status, data = transport("GET", f"/repos/{repo}/pulls?state=open&per_page=100&page=1", None)
    if status >= 400 or not isinstance(data, list):
        return []
    return data


def emit_overlap_scan(transport, repo: str, pr: int, declared_paths: "list | None" = None) -> int:
    """When a session declares its work on `pr`, warn about each OTHER open pull request whose change domain
    overlaps. Reads peers and their changed files, composes each domain, and posts one overlap-warning per
    overlapping peer. Returns the count posted (0 on any failure). Never raises. Advisory — never a lock."""
    def _run():
        import coordination_domains as cdz
        reader = lambda m, p: transport(m, p, None)  # noqa: E731 — domains wants (method, path)
        mine = cdz.domain(reader, repo, pr, declared=declared_paths or [])
        posted = 0
        for other in _open_prs(transport, repo):
            opr = other.get("number")
            if not isinstance(opr, int) or opr == pr:
                continue
            theirs = cdz.domain(reader, repo, opr)
            if cdz.overlaps(mine, theirs):
                if emit_overlap(transport, repo, pr, opr, paths=mine.get("actual") or declared_paths):
                    posted += 1
        return posted
    return _safe(_run) or 0


def emit_dependency_merged_scan(transport, repo: str, merged_pr: int, *, base_sha: str) -> int:
    """After `merged_pr` merged, tell every OTHER open pull request whose change domain overlaps the merged
    one that a dependency landed — a domain-filtered signal to re-check the canonical files that moved (use
    case 5). Distinct from the revalidation fan-out: revalidation says "the base moved" to everyone;
    dependency-update says "the merge touched YOUR surface" only to overlapping peers. `base_sha` is the new
    protected head (the observed change the receiver re-checks against). Returns the count posted; never
    raises."""
    def _run():
        import coordination_domains as cdz
        reader = lambda m, p: transport(m, p, None)  # noqa: E731 — domains wants (method, path)
        merged_dom = cdz.domain(reader, repo, merged_pr)
        posted = 0
        for other in _open_prs(transport, repo):
            opr = other.get("number")
            if not isinstance(opr, int) or opr == merged_pr:
                continue
            theirs = cdz.domain(reader, repo, opr)
            if cdz.overlaps(merged_dom, theirs):
                out = _safe(lambda o=opr: _emit(
                    transport, repo, o, kind="dependency-update", event="merged",
                    verify_action="recheck-base", subject={"pr": merged_pr}, work_ref={"pr": merged_pr},
                    observed={"base_sha": base_sha}))
                if out:
                    posted += 1
        return posted
    return _safe(_run) or 0


def emit_revalidation_scan(transport, repo: str, *, base_sha: str, exclude_pr: "int | None" = None) -> int:
    """After the protected base advanced (a merge), tell every other open candidate its green may be stale.
    Emits revalidation-notice/base-advanced on each open pull request except `exclude_pr`. Returns the count
    posted (0 on failure). Never raises."""
    def _run():
        posted = 0
        for other in _open_prs(transport, repo):
            opr = other.get("number")
            if not isinstance(opr, int) or opr == exclude_pr:
                continue
            if emit_revalidation_base_advanced(transport, repo, opr, base_sha=base_sha):
                posted += 1
        return posted
    return _safe(_run) or 0
