#!/usr/bin/env python3
"""Release source — the engine's release fetch + ref/tag resolution boundary.

One home for the network (and offline) primitives that turn a target ref into a fetchable release tree:
resolve a target ref to a concrete published tag (`_resolve_release_ref`, with the bare-version -> real-tag
resolution of StarshipSuperjam/engine-template#760), download and extract the tag's SOURCE archive
(`_fetch_release_tree` — the ONE injectable network boundary the add/upgrade path stubs), materialize a
local ref offline via `git archive` (`_archive_tree` — the cut-time gate's offline sibling), and classify a
fetch failure into missing-vs-transport (`_release_is_missing`). The three release/tag network boundaries
build their GitHub Request through one shared, optionally-authenticated helper (`_release_api_request`,
StarshipSuperjam/engine-template#867) so an API-version or auth change is a single edit.

Extracted verbatim from `module_manager` (StarshipSuperjam/engine-template#925 Part 5): these primitives are
a change domain that recurs on its own and are consumed by more than the module manager — the upgrade/add
paths, the release cut (`release_cut`), the cut-time deployment gate (`release_gate`), and the StarshipSuperjam/engine-template#760 demo all
resolve/fetch through here, so they earn a single home instead of being reached as private symbols on the
module manager. THE NETWORK BOUNDARIES ARE THE NAMED INDUCTIVE GAP: they never run in the construction repo
(which cuts no releases of itself), so tests inject a local tree / stub these functions and exercise the real
overlay/wire/coherence logic on it.
"""
from __future__ import annotations
import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402  (ROOT — the offline `git archive` boundary runs against it)


class _NoPublishedRelease(RuntimeError):
    """The home is reachable but has NO release to resolve (the releases API returned 200 with no
    `tag_name`) — a genuine missing-release condition, distinct from a transport failure, so the caller
    refuses LOUDLY naming the home rather than degrading it as a network problem (StarshipSuperjam/engine-template#367)."""


def _release_api_request(path: str, *, token: str | None,
                         user_agent: str = "engine-module-manager"):
    """Build the authenticated-OR-anonymous GitHub API Request that the three release/tag network
    boundaries below share (the tarball fetch, the latest-release resolve, the tag-published probe), so the
    token resolution and the header block live in ONE place — an API-version or auth change is now a single
    edit here, not three. Resolves the token ITSELF: the caller passes its own `token`, or None to fall back
    to `boot.gh_token()` (matching the `tok = token if token is not None else boot.gh_token()` the three
    callers each used to inline). `path` is an `api.github.com`-relative path the caller builds.

    Deliberately NOT `github_client.request`: that core client sets `Authorization: Bearer` UNCONDITIONALLY
    (its off-host guard protects a token-BEARING request), but these release reads stay OPTIONALLY
    authenticated — a public engine home's release is fetchable with no token, and an empty `Bearer ` would
    401 even a public repo. So this helper keeps the `if tok` conditional. It also carries no off-host guard:
    the callers build their own paths and never follow a `Link` header, so there is no redirect to guard.
    Callers keep their own slug-resolve (each with its own not-found message) and their own transport (raw
    tarball bytes / JSON parse / 404-vs-raise), mirroring github_client's own request/get seam split. `path`
    must be host-relative (a leading `/`): it is joined onto the host verbatim, so a slash-less path would
    silently build a malformed URL — refuse it loudly instead."""
    if not path.startswith("/"):
        raise ValueError(f"release API path must be host-relative and start with '/': {path!r}")
    import urllib.request, boot   # lazy: only the real network path needs these (matches the call sites)
    tok = token if token is not None else boot.gh_token()
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": user_agent}
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    return urllib.request.Request(f"https://api.github.com{path}", headers=headers)


def _fetch_release_tree(ref: str, dest_dir: str, repo: str | None = None,
                        token: str | None = None) -> str:
    """Download the engine's SOURCE archive at the tagged release `ref`, extract it under `dest_dir`, and
    return the path to the extracted tree root (the directory that contains `.engine/`). THIS IS THE
    NETWORK BOUNDARY — `add` (and the later updater) accept an injected local `release_tree`, so the tests
    and the demo never reach the network: they pass a local tree and exercise the REAL overlay/wire/
    coherence logic. The concrete download-and-extract below is therefore the named inductive gap a fixture
    cannot discharge (it never runs in the construction repo — there are no releases to fetch).

    Build-spec leaf (recorded): the artifact is the tag's GitHub SOURCE archive (the `tarball` endpoint),
    NOT a curated release asset — the engine ships from one tagged release as one tree, so the source archive carries every module's files and resolves their
    `provides` globs, and no separate asset-build pipeline exists. `ref` is a TAG, pinned, never a moving
    branch (the supply-chain control)."""
    import tarfile                # local: only the real network path needs these
    import urllib.request
    import boot                   # lazy: only the real fetch needs the repo slug
    slug = repo or boot.repo_slug()
    if not slug:
        raise RuntimeError("could not determine the engine repository to fetch the release from.")
    req = _release_api_request(f"/repos/{slug}/tarball/{ref}", token=token)
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = resp.read()
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tf:
        tops = {n.split("/", 1)[0] for n in tf.getnames() if n}
        if len(tops) != 1:
            raise RuntimeError(f"unexpected release archive layout (top-level entries: {sorted(tops)[:3]}).")
        tf.extractall(dest_dir, filter="data")   # filter='data' blocks path traversal / device entries (py3.12)
    return os.path.join(dest_dir, tops.pop())


def _archive_tree(ref: str, dest_dir: str) -> str:
    """The OFFLINE sibling of `_fetch_release_tree`: materialize a local tag/ref's tree via `git archive`
    piped into `dest_dir` — no network, no token. The cut-time deployment gate uses it to project a genuine
    past release to its deployed shape and practice-upgrade it to the release candidate, asserting the
    structural gate stays green — the proof a synthetic fixture cannot make. Returns `dest_dir` ITSELF: `git
    archive` writes the tree with NO owner-repo-sha wrapper directory (unlike GitHub's tarball), so there is no
    top-level dir to descend into (arch-N2). Raises if the ref's tree object is absent (a shallow checkout with
    no tags — the gate blocks the cut on that)."""
    import subprocess   # local: only the offline projection needs it
    os.makedirs(dest_dir, exist_ok=True)
    proc = subprocess.run(["git", "-C", validate.ROOT, "archive", "--format=tar", ref],
                          capture_output=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"git archive {ref} failed: {(proc.stderr or b'').decode('utf-8', 'replace')[:200]}")
    import tarfile   # local: only the offline belt needs it
    with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r:") as tf:
        tf.extractall(dest_dir, filter="data")
    return dest_dir


def _resolve_release_ref(ref: str | None, repo: str | None = None, token: str | None = None) -> str:
    """Resolve a target release ref to a CONCRETE, fetchable tag. A pinned tag/sha passes through unchanged;
    None or "latest" is resolved to the repository's latest published release tag via the GitHub releases
    API; a BARE version (`0.4.1` — the shape the manifest records, since `_bump_engine_manifest` strips the
    leading `v`) is resolved to the home's real published tag (`v0.4.1` or `0.4.1`), so a home that tags
    releases `vX.Y.Z` is fetched correctly instead of 404ing on the bare version (issue StarshipSuperjam/engine-template#760). The engine
    never fetches, runs, or RECORDS a moving ref (the tag-pin is the supply-chain control). THE NETWORK
    BOUNDARY for ref resolution — only the real add/upgrade path reaches it (the injected release_tree path
    passes a concrete ref), so it is part of the same named inductive gap as the release fetch (never run in
    the construction repo)."""
    if ref and ref != "latest":
        if not _is_bare_version(ref):
            return ref                                                  # a real tag / sha — pinned, untouched
        return _resolve_bare_version_tag(ref, repo=repo, token=token)   # bare X.Y.Z -> the home's real tag
    import urllib.request, json as _json, boot   # local: only the real resolve needs these
    slug = repo or boot.repo_slug()
    if not slug:
        raise RuntimeError("could not determine the engine repository to resolve the latest release.")
    req = _release_api_request(f"/repos/{slug}/releases/latest", token=token)
    with urllib.request.urlopen(req, timeout=60) as resp:
        tag = (_json.loads(resp.read()) or {}).get("tag_name")
    if not tag:
        raise _NoPublishedRelease("the engine repository has no published release to update to.")
    return tag


# ---- bare-version -> published-tag resolution (issue StarshipSuperjam/engine-template#760) ------------------------------------------
# `_bump_engine_manifest` records the engine release BARE (it strips a leading `v`), so the manifest holds a
# VERSION (`0.4.1`), not a fetchable TAG. A home tags its releases either `vX.Y.Z` (the common convention) or
# bare `X.Y.Z`; `add`/`upgrade` must resolve the bare version to whichever tag the home actually published,
# rather than fetching the bare version verbatim (which 404s on a `v`-tagging home — the StarshipSuperjam/engine-template#760 bug). Resolution
# is a DIRECT `releases/tags/{tag}` lookup per candidate, never a paginated releases LIST (a list drops an
# older pinned version off page 1, and admits drafts/pre-releases) — authoritative and O(1) per candidate.

_BARE_VERSION = re.compile(r"\d+\.\d+\.\d+")


def _is_bare_version(ref: str | None) -> bool:
    """True iff `ref` is a bare three-part semantic version like `0.4.1` — a VERSION, not a fetchable tag. A
    real tag (`v0.4.1`), a sha, a branch, or `latest`/None is not bare and the resolver leaves it untouched.
    A pre-release / build-metadata suffix (`0.4.1-rc1`) is deliberately treated as NOT bare and passes
    through: the engine's release flow only ever records a stable `X.Y.Z` (the `releases/latest` resolution
    excludes pre-releases), so this boundary is safe, not a gap."""
    return bool(ref) and _BARE_VERSION.fullmatch(ref) is not None


def _release_ref_candidates(version: str) -> list[str]:
    """The tags a bare `version` could have been published under, in probe order. `v`-first matches the
    dominant convention (and the `v` that `_bump_engine_manifest` strips on the way in), so the usual home
    resolves in a single probe; the bare candidate covers a home that tags without the prefix."""
    return [f"v{version}", version]


def _release_tag_published(tag: str, repo: str | None = None, token: str | None = None) -> bool:
    """Does the home publish a RELEASE at this exact `tag`? A direct `releases/tags/{tag}` lookup: 200 -> True;
    404 -> False (try the next candidate); any other failure propagates so the caller degrades on a transport
    fault, never silently. THE NETWORK BOUNDARY for tag resolution — joins `_resolve_release_ref` /
    `_fetch_release_tree` as a named inductive gap (never run in the construction repo; tests inject it)."""
    import urllib.request, urllib.error, boot   # local: only the real probe needs these
    slug = repo or boot.repo_slug()
    if not slug:
        raise RuntimeError("could not determine the engine repository to resolve the release tag.")
    req = _release_api_request(f"/repos/{slug}/releases/tags/{tag}", token=token)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp.read()
        return True
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise


def _resolve_bare_version_tag(version: str, repo: str | None = None, token: str | None = None) -> str:
    """Resolve a bare recorded `version` (`0.4.1`) to the home's real published tag, probing the candidates in
    order. Raises `_NoPublishedRelease` (classified MISSING by `_release_is_missing`, so the caller refuses
    LOUDLY and names the home) when no candidate is a published release — never a silent wrong or moving ref.
    A transport fault on a probe propagates (the caller degrades to the current version)."""
    for cand in _release_ref_candidates(version):
        if _release_tag_published(cand, repo=repo, token=token):
            return cand
    raise _NoPublishedRelease(f"the engine's update home publishes no release for version {version}.")


def _home_repository() -> str | None:
    """The engine's HOME repository slug (`owner/repo`) recorded in the manifest — the single source of
    truth for where engine updates are fetched from (issue StarshipSuperjam/engine-template#367). None when the manifest
    records no home (a repo generated before this coordinate shipped). The release-fetch callers pass this
    as `repo=` so they resolve the HOME, never the deployed repo's own `origin` (which `boot.repo_slug()`
    returns and which has no engine releases). On a None home the caller REFUSES with a plain remedy and
    never falls back to origin — the engine does not guess a home.

    Delegates to `module_coherence.home_repository()`, the single accessor (also read by the
    external-contribution submit flow), so the field name and the absent/blank/unreadable -> None contract
    live in one place rather than two that could drift."""
    import module_coherence   # lazy: _home_repository is the only path that needs it, so release_source keeps a
                              # lean top-level surface (just validate) and defers the heavier coherence dependency
                              # to the one call that uses it — matching the boot/urllib/tarfile lazy imports above.
    return module_coherence.home_repository()


def _release_is_missing(exc: BaseException) -> bool:
    """Split a release-fetch failure into its two operator-distinct outcomes (three-state resolution). True → the home is recorded but UNRESOLVABLE: the release/repo does not exist (HTTP 404
    — release-less, renamed, or removed home) OR the home is reachable but has no published release at all
    (`_NoPublishedRelease`, a 200 with no tag) — both refused LOUDLY naming the home. False → a transport
    failure (offline / DNS / timeout / other status), which DEGRADES to the current version.
    urllib raises HTTPError (a URLError subclass) carrying a numeric `.code` for an HTTP status; a bare
    URLError or socket error carries none."""
    import urllib.error
    if isinstance(exc, _NoPublishedRelease):
        return True
    return isinstance(exc, urllib.error.HTTPError) and getattr(exc, "code", None) == 404
