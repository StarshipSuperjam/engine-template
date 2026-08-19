#!/usr/bin/env python3
"""coordination_board — the one Engine-maintained coordination comment on a pull request or issue: the
durable (best-effort) carrier for advisory notices (StarshipSuperjam/engine-template#939, eADR-0043).

WHAT IT IS. A single comment per work item, marked `<!-- engine-coordination-board:v1 -->`, holding the live
coordination notices as machine-marked blocks (coordination_notice). Posting a notice is a read-modify-write
of that one comment: fetch it, parse the existing notices, dedupe the new one by its condition fingerprint,
evict to a small cap (priority-aware, never dropping a higher-priority live entry for a lower one), re-render,
and edit it in place — so the pull request timeline gets ONE comment that updates quietly, never a stream of
notifications that would make the operator the message bus again.

WHAT IT IS NOT. Not a system of record. The comment is multi-writer with no compare-and-swap, so a concurrent
write may lose an entry, a write-collaborator may alter or delete one, and a burst of forged entries may evict
live ones under the cap. Every one of those is bounded to lost *advisory latency*, never lost correctness,
because a receiver always re-verifies canonical state (eADR-0043). This module is the ONLY coordination
surface that reaches GitHub, and it reaches it ONLY through the comments endpoints — the confinement check
depends on that: no merge, no label, no commit status, no issue-body edit lives here.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import coordination_notice as cn  # noqa: E402
import github_client  # noqa: E402

USER_AGENT = "engine-coordination-board"

# The RUNTIME half of the advisory-only law (StarshipSuperjam/engine-template#939, eADR-0043 law 3): the two comment write shapes
# coordination may perform. Anything else is refused by comment_only() below — so even a future coordination
# edit, or an indirection through a differently-named helper, cannot reach a merge/label/status/issue-body
# endpoint. The static confinement check is the compile-time half; this is the mechanical backstop the static
# scan cannot give (it catches the naive in-file case, not a deliberate indirection).
#
# The patterns FULLMATCH the ROUTED path component ONLY — never the query string or fragment. GitHub routes
# solely on the path; a query/fragment is caller-controlled and inert on the wire (urllib even strips a
# fragment entirely). So the guard first extracts `urlsplit(path).path` and matches THAT exactly: a decoy that
# appends a comment-shaped query or fragment onto a non-comment path (a label or issue-body write) is refused,
# where an unanchored substring search over the raw string would have admitted it.
_POST_COMMENT_PATH = re.compile(r"/repos/[^/]+/[^/]+/issues/\d+/comments/?")
_PATCH_COMMENT_PATH = re.compile(r"/repos/[^/]+/[^/]+/issues/comments/\d+/?")


def comment_only(transport):
    """Wrap a raw GitHub transport so coordination can issue ONLY read-only GETs and the two comment-write
    shapes (POST an issue's comments collection, PATCH a comment by id). Any other method, or a write to any
    other path — a merge, a label, a commit status, an issue-body edit — raises, at runtime, before it
    reaches GitHub. This makes the advisory-only law a property coordination cannot escape by refactoring the
    call into a helper the static check does not scan. Idempotent: wrapping an already-wrapped transport is
    harmless."""
    def _guarded(method, path, body=None):
        m = (method or "").upper()
        raw = path or ""
        parts = urllib.parse.urlsplit(raw)
        # Confine the HOST too, not just the path shape: a request must be a host-relative path ROOTED at a
        # single "/", so coordination can never be aimed off-host. Rejected: a scheme or netloc
        # (`http://evil/...`); a protocol-relative `//evil/...` (netloc follows); and — critically — a path
        # that does not start with "/", e.g. a userinfo trick `@evil.com/repos/.../comments`. The transport
        # this wraps builds the URL by concatenation (`"https://" + host + path`), so `@evil.com/...` becomes
        # `https://api.github.com@evil.com/...` whose REAL host is `evil.com` — a bearer-token exfiltration.
        # This holds for GETs as well as writes (a read off-host leaks the token too). Real callers always pass
        # a literal `/repos/...` path, so nothing legitimate is refused.
        if parts.scheme or parts.netloc or not raw.startswith("/") or raw.startswith("//"):
            raise BoardError(
                f"coordination may reach only the current GitHub host (eADR-0043 law 3); refused {m} {path}")
        if m == "GET":
            return transport(method, path, body)
        # Match the path GitHub actually routes on — never the query/fragment, which are caller-controlled
        # and do not participate in routing. Anchored fullmatch, so no prefix/suffix decoy can satisfy it.
        routed = parts.path
        if m == "POST" and _POST_COMMENT_PATH.fullmatch(routed):
            return transport(method, path, body)
        if m == "PATCH" and _PATCH_COMMENT_PATH.fullmatch(routed):
            return transport(method, path, body)
        raise BoardError(
            f"coordination is confined to reads and comment writes (eADR-0043 law 3); refused {m} {path}")
    return _guarded
BOARD_MARKER = "<!-- engine-coordination-board:v1 -->"
BOARD_INTRO = (
    "**Engine coordination board.** Advisory notices from concurrent Engine worker sessions on this work "
    "item. Each is a prompt to re-check canonical state — never an approval, a review, or a merge signal. "
    "Delivery is best-effort; the real record is Git, the checks, and your merge.")
BOARD_CAP = 10  # the most live notices the board holds; oldest-lowest-priority evicted beyond this

# Kept-priority by kind (higher survives eviction). An integration block/revalidation carries more urgency
# than a status declaration; within a priority tie, the newer notice wins. This ordering is the guarantee
# that eviction never drops a higher-priority live entry to keep a lower one.
_KIND_PRIORITY = {
    "integration-notice": 5,
    "revalidation-notice": 5,
    "dependency-update": 4,
    "overlap-warning": 3,
    "handoff": 2,
    "bounded-status": 1,
}


class BoardError(Exception):
    """A GitHub comment read/write failed. The caller (an emitter) swallows it — coordination is best-effort
    and must never break the lifecycle step it rides."""


class _Comments:
    """The thin GitHub comments client — the SINGLE GitHub surface coordination touches, and only the
    comments endpoints (GET/POST issues/{n}/comments, PATCH issues/comments/{id}). Injectable transport so
    tests and the demo drive it with a fake GitHub and no network. Mirrors overlay_disclosure._Comments."""

    def __init__(self, repo: str, token: str, *, transport=None):
        self.repo = repo
        self.token = token
        # Wrap the effective transport in the comment-only guard, so EVERY board call — and anything that
        # holds this client — can issue only reads and comment writes, at runtime, whatever the caller passed
        # (StarshipSuperjam/engine-template#939, eADR-0043 law 3: the mechanical backstop the static confinement scan cannot give).
        self._transport = comment_only(transport or self._http)

    def _http(self, method: str, path: str, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = github_client.request(path, self.token, user_agent=USER_AGENT, method=method, data=data)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            return exc.code, None
        except urllib.error.URLError as exc:
            raise BoardError(f"GitHub is unreachable: {exc}") from exc

    def list_comments(self, number: int) -> list:
        out, page = [], 1
        while True:
            status, data = self._transport(
                "GET", f"/repos/{self.repo}/issues/{number}/comments?per_page=100&page={page}", None)
            if status >= 400 or data is None:
                raise BoardError(f"GitHub returned {status} listing comments on #{number}")
            out.extend(data)
            if len(data) < 100:
                break
            page += 1
        return out

    def post_comment(self, number: int, body: str) -> None:
        status, _ = self._transport("POST", f"/repos/{self.repo}/issues/{number}/comments", {"body": body})
        if status >= 400:
            raise BoardError(f"GitHub returned {status} commenting on #{number}")

    def edit_comment(self, comment_id, body: str) -> None:
        status, _ = self._transport(
            "PATCH", f"/repos/{self.repo}/issues/comments/{comment_id}", {"body": body})
        if status >= 400:
            raise BoardError(f"GitHub returned {status} editing comment {comment_id} on {self.repo}")


def _is_bot(comment: dict) -> bool:
    return ((comment.get("user") or {}).get("type")) == "Bot"


def _find_board(comments: list):
    """The engine's own coordination board comment among a work item's comments — the first BOT-authored
    comment carrying the board marker (so a user comment quoting the marker is never overwritten). Returns
    (comment_id, body) or (None, None)."""
    for c in comments:
        if BOARD_MARKER in (c.get("body") or "") and _is_bot(c):
            return c.get("id"), c.get("body")
    return None, None


def _evict(notices: list) -> list:
    """Keep at most BOARD_CAP notices, dropping lowest-priority-then-oldest first. Sorted newest-priority-high
    first for the keep decision, then returned in a stable document order (oldest first) for rendering."""
    ordered = sorted(
        notices,
        key=lambda n: (_KIND_PRIORITY.get(n["kind"], 0), n.get("emitted_at", "")),
        reverse=True)
    kept = ordered[:BOARD_CAP]
    return sorted(kept, key=lambda n: n.get("emitted_at", ""))


def _compose(notices: list) -> str:
    blocks = "\n\n".join(cn.render_block(n) for n in notices)
    return f"{BOARD_MARKER}\n\n{BOARD_INTRO}\n\n{blocks}"


def read_board(client: _Comments, number: int) -> list:
    """Every well-formed notice currently on the work item's board (skip-malformed via the parser)."""
    _id, body = _find_board(client.list_comments(number))
    return cn.parse_blocks(body or "") if body is not None else []


def post_notice(client: _Comments, number: int, notice: dict) -> str:
    """Read-modify-write the board comment to include `notice`. Deduped on the condition fingerprint: an
    identical condition already on the board is a no-op ('deduped'). Otherwise the notice is added, the board
    is evicted to the cap, and the comment is edited in place ('edited') or created ('posted'). Returns the
    outcome string. Raises BoardError on a GitHub failure — the emitter swallows it."""
    comments = client.list_comments(number)
    board_id, body = _find_board(comments)
    existing = cn.parse_blocks(body or "") if body else []

    new_fp = cn.fingerprint(notice)
    if any(cn.fingerprint(e) == new_fp for e in existing):
        return "deduped"

    merged = _evict(existing + [notice])
    composed = _compose(merged)
    if board_id is None:
        client.post_comment(number, composed)
        return "posted"
    client.edit_comment(board_id, composed)
    return "edited"
