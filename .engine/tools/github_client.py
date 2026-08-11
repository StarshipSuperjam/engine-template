#!/usr/bin/env python3
"""github_client — the engine's one authenticated GitHub REST API client (core).

The single, core-owned home for the authenticated-Request shape every engine tool that talks to the
GitHub API shares: the Bearer-token + Accept + API-version header block, the host-relative-or-absolute
URL handling with the OFF-HOST GUARD, the single-resource and paginated GET, and the base64 content
decode. Before this, the same logic was hand-copied across weakening_guard, audit_digest, telemetry,
protection_guard, and lock_integrity (via weakening_guard) — two of those (the weakening guard and
the protection guard) are stage-0 seeds slated for supersession, so the importers risked being stranded
when those modules leave. They now all import from here.

GUARDRAIL-CLASS. `request` carries the off-host guard that protects a token-bearing
`pull_request_target` request from being redirected off-host by a crafted `Link` header (the
weakening guard's load-bearing security property). Weakening that check is a guardrail-weakening change
exactly as it was inside weakening_guard — which is why this file is a member of the guard's enforcement-hook
floor (_FLOOR_ENFORCEMENT_HOOKS; disclosure tier on modification, hard on removal — eADR-0040).

Two deliberate seams preserve each caller's own contract — this module does NOT impose one error model:
  - `request` BUILDS a Request and applies the guard; it never executes it. `get_json` / `get_page`
    execute a GET and let `urllib.error.HTTPError` / `URLError` propagate UNWRAPPED, so a caller can
    branch on a status code (audit_digest / telemetry catch `HTTPError` into a status; lock_integrity
    catches a 404; weakening_guard / protection_guard fail closed on any exception). The status-returning
    callers use `json_request` (below), the shared status-returning transport: it executes any method,
    maps `HTTPError` to a `(status, None)` result, and lets `URLError` propagate so each caller keeps its
    OWN read-vs-write failure policy. A read-only consumer that only needs a bound seam takes a `reader`
    (`.repo` + a `.transport` callable), never a domain client's private transport.
  - `request` takes optional `method` / `data` to serve the write-capable callers (telemetry's issue
    writes, audit_digest's POST-able transport), but it is INERT for a GET caller: Content-Type is set
    only when `data` is present (correct REST semantics), so a GET carries no Content-Type — identical to
    the read-only guards, and dropping the (inert) Content-Type the write-capable callers previously set
    on their own GETs, which GitHub ignores on a bodyless request. The privileged read-only guards call
    the GET-only `get_json`, never `request` with data, so they gain no write capability.

stdlib-only. `_urlopen` is the injectable network seam a test replaces to run the real request-building,
guard, pagination, and decode logic offline (there is never a live call in a test).
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request

API_HOST = "api.github.com"

# The network boundary, as a module attribute so a test can replace ONLY the network (monkeypatch
# `github_client._urlopen`) and exercise every line of real logic above it.
_urlopen = urllib.request.urlopen

_TIMEOUT = 30


def request(url_or_path: str, token: str, *, user_agent: str, method: str = "GET", data=None):
    """Build the authenticated GitHub API Request. `url_or_path` is either an api.github.com-relative
    path (e.g. '/repos/o/r/pulls/1/files?per_page=100') OR an absolute https URL taken verbatim from a
    `Link: rel="next"` header.

    THE OFF-HOST GUARD: an absolute URL must point at the GitHub API host — a token-bearing
    `pull_request_target` request must never be redirected off-host by a crafted `Link` header, so an
    off-host URL raises `ValueError` (the caller fails closed). A relative path (every caller passes one
    `/`-prefixed) is joined to the host and never reaches the guard; the leading `http` is the
    absolute-URL discriminator, so only a verbatim `Link` URL takes the guarded branch. `data` / `method`
    serve write-capable callers; `Content-Type` is set ONLY when `data` is present, so a GET carries none."""
    if url_or_path.startswith("http"):
        url = url_or_path
        if urllib.parse.urlparse(url).netloc != API_HOST:
            raise ValueError(f"refusing to follow an off-host pagination link: {url}")
    else:
        url = "https://" + API_HOST + url_or_path
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": user_agent,
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    return urllib.request.Request(url, data=data, method=method, headers=headers)


def get_page(url_or_path: str, token: str, *, user_agent: str):
    """GET one page; return (parsed_body, link_header_or_None). The `Link` header carries pagination
    (rel="next") for list endpoints; HTTPMessage.get is case-insensitive. Raises
    `urllib.error.HTTPError` / `URLError` UNWRAPPED on failure (the caller decides what that means)."""
    with _urlopen(request(url_or_path, token, user_agent=user_agent), timeout=_TIMEOUT) as resp:
        body = json.loads(resp.read().decode("utf-8"))
        return body, resp.headers.get("Link")


def get_json(url_or_path: str, token: str, *, user_agent: str):
    """GET a single JSON resource (body only) — for non-paginated reads. Raises
    `urllib.error.HTTPError` / `URLError` UNWRAPPED, so a caller can catch a 404 or fail closed."""
    body, _ = get_page(url_or_path, token, user_agent=user_agent)
    return body


def json_request(method: str, path: str, token: str, *, user_agent: str, body=None):
    """Execute an authenticated GitHub JSON call and return `(status, data | None)` — the neutral,
    status-returning transport the write-capable / status-branching callers share (telemetry's issue
    read/write boundary, the issue-label client, and the read-only projections that used to reach a domain
    client's private transport). It is the single home for the JSON transport mechanics those callers used
    to hand-copy.

    It BUILDS the Request via `request` (so the OFF-HOST GUARD and the Bearer/Accept/version header block
    stay single-homed) and executes through the injectable `_urlopen` seam, then:
      - returns `(resp.status, json.loads(raw) if raw else None)` — an EMPTY body (a 204 label DELETE, a
        no-content PATCH) decodes to `None`, never a `JSONDecodeError` on a successful write;
      - maps `urllib.error.HTTPError` to `(exc.code, None)` — the caller branches on the status (a 404 is
        data, never an exception), and the error-response body is discarded, exactly as the hand-copied
        transports did;
      - lets `urllib.error.URLError` PROPAGATE UNWRAPPED — an unreachable host is the one failure whose
        meaning differs by caller (a read degrades, a write fails), so each caller keeps its own
        `except URLError -> Degraded*Error` (or `-> None`) policy over this seam.
    It returns no `Link` header, so a `Link`-paginated read stays on `get_page`, not this."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = request(path, token, user_agent=user_agent, method=method, data=data)
    try:
        with _urlopen(req, timeout=_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:     # 4xx/5xx — surface the status; the body is discarded (as before)
        return exc.code, None
    # urllib.error.URLError is deliberately NOT caught here: an unreachable host means read-degrade vs
    # write-fail, a decision only the caller owns.


class _Reader:
    """A neutral GitHub read seam: `.repo` plus a `.transport(method, path, body) -> (status, data | None)`
    callable. A read-only consumer (standing_situation, work_record, pr_reconcile, first_run_health) accepts
    one of these INSTEAD of reaching a domain client's private `_transport` — the coupling
    StarshipSuperjam/engine-template#907 removes.
    `.transport` has the exact shape those consumers already duck-typed, so a canned test transport drops in
    unchanged."""
    __slots__ = ("repo", "transport")

    def __init__(self, repo, transport):
        self.repo = repo
        self.transport = transport


def reader(repo: str, token: str, *, user_agent: str, transport=None):
    """A neutral reader (`.repo` + `.transport`) bound to `repo` / `token` / `user_agent`. In production
    `.transport` is a thin closure over `json_request`; a test/demo passes `transport=` — a canned
    `(method, path, body) -> (status, data | None)` — to run the real consumer logic offline. This is how a
    generic read reaches GitHub without constructing a domain client (telemetry.GitHubIssues) merely to
    borrow its transport."""
    bound = transport if transport is not None else (
        lambda method, path, body=None: json_request(method, path, token, user_agent=user_agent, body=body))
    return _Reader(repo, bound)


def next_link(link_header):
    """The rel="next" URL from a GitHub `Link` header, or None when there is no next page."""
    if not link_header:
        return None
    for part in link_header.split(","):
        segments = part.split(";")
        if len(segments) < 2:
            continue
        url = segments[0].strip().lstrip("<").rstrip(">").strip()
        for param in segments[1:]:
            if param.strip() == 'rel="next"':
                return url
    return None


def decode_content(obj, *, codec: str = "utf-8") -> str:
    """Decode the base64 `content` field of a Contents-API OR Git-blobs-API response (both return
    `{"encoding": "base64", "content": ...}`). `codec` is "utf-8" by default, or "utf-8-sig" for a caller
    that must strip a committed byte-order mark. Lossy ("replace") on bad bytes, matching every caller."""
    return base64.b64decode(obj["content"]).decode(codec, "replace")
