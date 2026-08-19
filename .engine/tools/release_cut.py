#!/usr/bin/env python3
"""Release-cut classifier + writer — the produce side of the engine's version line
(the complement to the consume side the module manager owns).

The engine and every module carry a version (`.engine/engine.json` `engine_release` + the
`packages` map; each `.engine/modules/<id>/manifest.json` `version`). The module manager
*consumes* a published release (fetch + overlay + migrate); this tool *produces* one:
it decides the next version from what changed since the last release, and it
records the chosen versions into the manifests. It does NOT tag, open a PR, or publish a Release —
that GitHub-facing plumbing lives in the terminal cut (`release_terminal.py`, driven by
`release-publish.yml`); this is the version-decision core it builds on.

Two subcommands, split so consent attaches to a proposal the writer cannot silently drift from:

  propose  — read-only. Resolve the last release baseline from the engine's HOME repo (the StarshipSuperjam/engine-template#369
             `home_repository` coordinate — the same source the updater fetches from, so producer
             and consumer agree on what "a release" is), diff since it, and author:
               * the mechanical bump FLOOR: a module ADDED => engine >= minor;
                 a module REMOVED => engine >= major; a new `migrations` entry in a package => that
                 package >= minor; the engine version = the MAX implied bump;
               * a plain-language CHANGE INVENTORY (what changed since the last release), so the
                 maintainer can catch a wrong floor or a missing signal;
               * where a contract/seam/interface/wiring surface changed, an AI-authored plain-language
                 IMPACT statement, with the break/no-break behavioral demonstration marked present
                 (a correlate exists) or "no correlate — release consciously sub-bar, named" (the
                 legible gate path; no acceptance-benchmark instrument is available,
                 and its absence is stated, never faked).
             It writes nothing.

  apply    — the writer. Records the chosen engine + per-package versions into the manifests, with:
               * RAISE-ONLY enforcement: the engine version and every CHANGED
                 capability are compared against the current on-disk version, and a target that is a
                 detectable LOWERING is REFUSED loudly (the dev sentinel `0.0.0-dev` sorts below any
                 real release). An unchanged capability keeps its recorded version — it is a no-op keep,
                 not a lowering, so it is neither rewritten nor refused; a capability a change requires
                 to bump (its package_floor) is auto-raised to that floor. Nothing is ever silently
                 lowered, and a required bump is never silently skipped (below-confirmed-floor is
                 checked over the full floor set);
               * an ATOMIC staged write: every touched file is written to a temp sibling and
                 schema-re-validated (plus a packages<->manifest equality check) BEFORE any swap, then
                 all swapped together; a validation failure changes nothing, and a write error mid-swap
                 rolls back the files already written and reports loudly (no split-brain — the
                 "atomic-or-loudly-incomplete" invariant; the reviewed-PR merge is the real
                 all-or-nothing unit, this bounds the on-disk window);
               * shape preservation: manifests are loaded, mutated in place, and rewritten with the
                 house 2-space+newline writer, so only version VALUES change — the `home_repository`
                 line stays byte-identical and the tightened weakening_guard is not
                 tripped by a version-only cut.

Read-only discovery + the release-ref/fetch/manifest-write helpers are reused from module_coherence,
module_manager (one present-set reader), and release_source (the release-ref resolver + fetch — no drift).

A third subcommand renders the maintainer's evidence:

  pr-body  — read-only. Render the release pull request's body from a `propose` JSON + an `apply` result
             JSON: the change inventory, the versions actually recorded, a legible gate-path line
             (passed / consciously-sub-bar / errored — the three read as distinct), and the confirm/raise/
             reject guidance that makes the PR review the consent act. Authored HERE, never in workflow
             bash, so the gate-path legibility has one home.

CLI:
  python tools/release_cut.py propose [--json] [--baseline-tree DIR]
  python tools/release_cut.py apply --engine VER [--all VER] [--package id=ver ...] \
                                    [--proposal FILE] [--dry-run] [--json]
  python tools/release_cut.py pr-body --proposal FILE --applied FILE [--gate-state STATE]
"""
from __future__ import annotations
import argparse
import json
import os
import re
import shutil
import sys
import tempfile

import validate
import module_coherence
import module_manager
import release_source  # release fetch + ref/tag resolution (StarshipSuperjam/engine-template#925 Part 5)
import engine_write  # the engine-owned write boundary — the cut's stage/swap pre-flight (StarshipSuperjam/engine-template#923)
import local_references  # the declared local-reference vocabulary (StarshipSuperjam/engine-template#639)
import shipped_local_references_check  # the shipped-surface scan this cut reuses as its backstop (StarshipSuperjam/engine-template#943)
import release_impact  # the declared-impact vocabulary + ordering + marker (StarshipSuperjam/engine-template#942)
import module_surfaces  # the file -> owning-module registry, for per-package impact attribution (StarshipSuperjam/engine-template#942 L10)

SENTINEL = "0.0.0-dev"
ENGINE_SCHEMA = os.path.join(validate.SCHEMAS_DIR, "engine.v1.json")
MODULE_SCHEMA = os.path.join(validate.SCHEMAS_DIR, "module.v1.json")

# The change-inventory line classify() adds when NOTHING structural fired — a caveat, not a per-item signal,
# so the renderers exclude it when listing the structural signals beside the merged-PR list (one home for the
# string, referenced in both places).
_NO_STRUCTURAL_SIGNAL_NOTE = ("No module added or removed and no contract surface added or removed since the "
                              "last release — so the diff proves NO mechanical compatibility floor. This does "
                              "NOT mean 'at most a patch': the version is whatever the merged pull requests "
                              "declared (a behaviour change carries its impact there, not in the structure).")


# --------------------------------------------------------------------------- version ordering
# Strict MAJOR.MINOR.PATCH with an optional pre-release suffix — the SAME grammar the module.v1 schema
# now enforces on the manifest `version` field (StarshipSuperjam/engine-template#402 U07a), so the writer here and the schema gate at CI
# cannot bless different shapes. Kept in sync deliberately: the schema is the harder gate, and this writer
# check catches a nonsense version before it ever reaches a release manifest.
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$")


def _valid_version(v: str) -> bool:
    """A MAJOR.MINOR.PATCH version, optionally with a pre-release suffix (1.2.0, 1.0.0-rc1, 0.0.0-dev).
    The manifest schema requires this exact shape (module.v1 version pattern), and it is enforced HERE at
    the writer too so a nonsense version (a typo, a shell fragment, a 1- or 2-component number) never
    reaches a release manifest and never fools the digit-only ordering."""
    return bool(_VERSION_RE.match(v or ""))


def _is_prerelease(v: str) -> bool:
    """A version carrying a pre-release suffix (a '-', e.g. the `0.0.0-dev` sentinel or `1.0.0-rc1`)."""
    return "-" in (v or "")


def _release_tuple(v: str) -> tuple:
    """The numeric release identity of a version, the pre-release suffix REMOVED before tupling —
    otherwise `validate._ver_tuple` folds `-rc1`'s digits into the tuple and a pre-release sorts
    ABOVE its own release (1.0.0-rc1 -> (1,0,0,1) > (1,0,0))."""
    return validate._ver_tuple((v or "").split("-", 1)[0])


def _strictly_greater(new: str, cur: str) -> bool:
    """True iff `new` is a strictly higher RELEASE than `cur`. Compared on the release numbers with the
    pre-release stripped; on equal numbers a real release outranks a pre-release of the same numbers
    (so `0.1.0` > `0.0.0-dev` and `1.0.0` > `1.0.0-rc1`), and a pre-release is never taken as greater
    than another version of the same numbers (conservative — a pre-release progression like rc1 -> rc2
    is refused rather than risk a silent mis-order; raise-only never lowers)."""
    nt, ct = _release_tuple(new), _release_tuple(cur)
    if nt != ct:
        return nt > ct
    return _is_prerelease(cur) and not _is_prerelease(new)


# --------------------------------------------------------------------------- product-release mode (StarshipSuperjam/engine-template#516)
# Once the engine is DEPLOYED, this same machinery cuts the deployed repo's OWN product release instead of the
# engine's version: the version is read from (and written to) a product-owned `product-version.json` at the
# repository ROOT (product territory, eADR-0007 — so it survives an engine uninstall), the baseline is the
# deployed repo's own last release, and the tag + GitHub Release publish into the deployed repo itself
# (release_terminal already targets GITHUB_REPOSITORY). The CONSTRUCTION repo (where the engine IS the product)
# keeps cutting the engine version, unchanged. A deployment inherits a working release system instead of
# building versioning plumbing from scratch. The workflow shell is untouched: product-mode speaks the SAME
# propose/apply JSON shape (a `mode`, an `engine_floor_version` carrying the patch-bump default, an `engine`
# key carrying the recorded version) with product semantics underneath, plus a `product` marker the renderers
# and the publisher read to speak of the PRODUCT rather than the engine.
PRODUCT_VERSION_REL = "product-version.json"
_PRODUCT_MALFORMED = object()   # the file exists but is not a readable {"version": "<semver>"} -> refuse loudly


def _product_version_path(root: str | None = None) -> str:
    return os.path.join(root if root is not None else validate.ROOT, PRODUCT_VERSION_REL)


def read_product_version(root: str | None = None):
    """The current product version string, or None (no file — an un-seeded deployment / a first cut), or the
    `_PRODUCT_MALFORMED` sentinel (the file is present but is not a readable `{"version": "<semver>"}`).
    Malformed is NEVER silently treated as absent: the mode resolver turns it into a loud refuse, so a corrupt
    product file can never fall through to an ENGINE cut in a deployed repo."""
    path = _product_version_path(root)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001 — present-but-unreadable is a loud refuse, never "absent"
        return _PRODUCT_MALFORMED
    v = data.get("version") if isinstance(data, dict) else None
    return v if isinstance(v, str) and _valid_version(v) else _PRODUCT_MALFORMED


def release_mode(own_slug: str | None = None, root: str | None = None) -> tuple:
    """Which release this repo cuts: `("engine" | "product" | "refuse", ctx)` where `ctx` carries the current
    product version (`current`, None on a first product cut) and the repo `slug`. PRODUCT dominates: a repo is
    in product-mode when it carries a `product-version.json` OR it is a downstream deployment (recorded update
    home != own origin) — so product-mode ARMS on a deployed repo's very first upgrade, and the first product
    cut CREATES the file. A present-but-MALFORMED product file is a loud REFUSE, never an engine cut. Only the
    construction repo — not a downstream copy, no product file — cuts the ENGINE version. `own_slug`/`root` are
    injectable so a fixture forces either mode offline.

    File-presence DOMINATES deliberately. `module_coherence.is_downstream_copy` fails soft to False on an
    unreadable origin; keying product-mode on the downstream check alone could route a deployed repo that DOES
    carry the product file into an engine cut whenever its origin momentarily can't be read. Because a present
    product file forces product-mode on its own, that regression cannot happen — the deployed repo's committed
    declaration wins over live origin resolution."""
    pv = read_product_version(root)
    if pv is _PRODUCT_MALFORMED:
        return "refuse", {"current": None, "slug": own_slug}
    if own_slug is None:
        import boot   # local: only mode resolution needs the origin slug (mirrors _generate_notes_body)
        own_slug = boot.repo_slug()
    if pv is not None or module_coherence.is_downstream_copy(own_slug):
        return "product", {"current": pv, "slug": own_slug}
    return "engine", {"current": None, "slug": own_slug}


# --------------------------------------------------------------------------- baseline resolution
class Baseline:
    """The last-release baseline for the diff. `ref` is None in FIRST-CUT mode (the home has no
    published release yet — the current reality, and the state the v1/beta cut is made from)."""
    def __init__(self, ref, first_cut: bool, note: str):
        self.ref = ref
        self.first_cut = first_cut
        self.note = note


def _product_baseline(slug: str | None) -> Baseline:
    """The product baseline for a deployed repo's OWN release stream. When the repo slug could NOT be resolved
    (`boot.repo_slug()` returned None — no GITHUB_REPOSITORY and no readable git origin), a product cut must
    NOT fall through to `resolve_baseline`'s engine-`home_repository` default (that would diff the product
    against the ENGINE's releases): with no slug there is no release stream to look up, so it is a first cut —
    the version is chosen, not derived. On the sanctioned CI path the slug is always set, so this is defensive."""
    if not slug:
        return Baseline(None, True, "the repository could not be identified, so there is no prior release to "
                                    "diff against — treating this as the first product release.")
    return resolve_baseline(slug=slug)


def resolve_baseline(slug: str | None = None) -> Baseline:
    """The last released tag to diff against, or a first-cut baseline when there is no release yet. `slug`
    defaults to the engine's HOME repo (StarshipSuperjam/engine-template#369 `home_repository` — the engine's own release stream); in
    PRODUCT-mode (StarshipSuperjam/engine-template#516) the caller passes the DEPLOYED repo's own slug, so a product cut resolves the product's
    own last release, never the engine's home. A TRANSPORT failure (offline/DNS) is not a first cut — it is
    unknowable, and we say so rather than guess an empty baseline."""
    home = slug if slug is not None else release_source._home_repository()
    if not home:
        return Baseline(None, True, "no home repository is recorded, so there is no prior release to "
                                    "diff against — treating this as the first cut.")
    try:
        ref = release_source._resolve_release_ref(None, repo=home)
        return Baseline(ref, False, f"diffing since the last release {ref} of {home}.")
    except Exception as exc:  # _resolve_release_ref raises RuntimeError subclasses (Exception), never BaseException
        if release_source._release_is_missing(exc):
            return Baseline(None, True, f"{home} has no published release yet — this is the first cut.")
        raise


def _baseline_tree_for(baseline: Baseline, injected: str | None) -> tuple:
    """The baseline release tree to diff against, and a temp dir to clean up (or None). An INJECTED local
    tree always wins (tests and an explicit `--baseline-tree` pass one, so `propose` never reaches the
    network in a test). Otherwise, in diff mode, the tree is fetched from the home's release tarball at the
    resolved ref via the module_manager network boundary — a TESTED Python caller (like the other release
    helpers), never a private symbol reached from workflow bash. First-cut mode diffs nothing, so no tree."""
    if injected:
        return injected, None
    if baseline.first_cut:
        return None, None
    home = release_source._home_repository()
    tmp = tempfile.mkdtemp(prefix="release-baseline-")
    try:
        tree = release_source._fetch_release_tree(baseline.ref, tmp, repo=home)
    except BaseException:
        # the fetch can raise (transport failure, non-200, a malformed tarball) BEFORE the temp dir is
        # returned to the caller's finally — clean it up here so a failed fetch never strands a temp dir
        # (the caller only removes what it receives back).
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return tree, tmp


# --------------------------------------------------------------------------- merged-PR summary (the work log)
# The structural floor signals (a capability added/removed, a new migration, a changed contract) justify the
# VERSION, but they are a narrow slice of a release — a busy release can merge dozens of pull requests that
# touch none of them. So the notes ALSO carry the plain list of pull requests merged since the last release —
# the actual body of work — from GitHub's own generator, which lists them independently of the merge strategy
# (merge / squash / rebase), so it holds in a generated repo too. This is a derived view of the pull requests
# themselves (the one history store, eADR-0014), never a second store.
_PR_LINE_RE = re.compile(r"^\* (.+) by @\S+ in \S+/pull/(\d+)\s*$")
# A looser signature: ANY line that carries a `…/pull/N` link plainly names a merged pull request (capturing N).
# The version-authority enumerator uses it to detect a line that names a PR whose number was NOT counted into the
# parsed list (undisclosed generate-notes format drift), so a genuinely dropped line fails closed instead of
# vanishing — while a line that merely RE-references an already-counted PR (GitHub's `## New Contributors` section:
# `* @user made their first contribution in …/pull/N`) is recognised as accounted-for and does not trip the guard.
_PR_PULL_URL_RE = re.compile(r"/pull/(\d+)\b")
# The engine's OWN release pull request (title "Release X.Y.Z", authored by release.yml). At publish the notes
# are generated over previous_tag..merge_sha, which spans the release PR's own merge — so without this it would
# list itself and the count would be one high. Past release PRs sit before previous_tag, out of range.
_RELEASE_PR_RE = re.compile(r"^Release \d+\.\d+\.\d+")
# A closing keyword directly bound to an issue reference (GitHub's own auto-close grammar: close/closes/closed,
# fix/fixes/fixed, resolve/resolves/resolved, then optional colon/whitespace, then `#N` or a cross-repo
# `owner/repo#N`). A merged PR's author often writes "(Closes #N)" into the PR title; rendered VERBATIM into the
# RELEASE pull-request body it makes GitHub attribute that close to the release — so on merge the release would
# (re-)close it. We strip the KEYWORD and keep the reference (readable, inert). A keyword NOT directly adjacent
# to the reference is not a GitHub close (e.g. "fail closed (#N)" — the `(` breaks the bond; "Fixed several
# bugs, see #N") and is left untouched (confirmed empirically against GitHub). The bare-URL and `GH-N` forms are
# out of scope: GitHub's documented auto-close grammar does not include them.
_CLOSING_KEYWORD_RE = re.compile(
    r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b[:\s]+((?:[\w.-]+/[\w.-]+)?#\d+)")


def _defuse_closing_keywords(text: str) -> str:
    """Neutralise any `Closes #N` / `Fixes #N` / `Resolves #N` in a merged-PR title so it cannot auto-close the
    issue from the release pull-request body — the keyword is dropped, the `#N` reference is kept."""
    return _CLOSING_KEYWORD_RE.sub(r"\1", text)


def _parse_pr_lines(body: str) -> list:
    """GitHub's generated 'What's Changed' body -> plain 'Title (#N)' lines, dropping the author + URL noise
    the engine's plain-language notes don't carry, the engine's own release pull request (a release must not
    list itself), and any closing keyword the title carries (so the release does not re-close those issues)."""
    out = []
    for line in (body or "").splitlines():
        m = _PR_LINE_RE.match(line.strip())
        if m and not _RELEASE_PR_RE.match(m.group(1).strip()):
            out.append(f"{_defuse_closing_keywords(m.group(1).strip())} (#{m.group(2)})")
    return out


# The release-notes change kinds, in DISPLAY ORDER. A title that leads with one as a `Kind:` prefix
# (`Fix: quote the hook path`) groups the merged-PR list so the notes read as sorted work, not one flat pile; a
# title with no recognised prefix falls to "Other changes", rendered last. The KIND VOCABULARY itself is
# single-sourced in `issue_kind.KINDS` (StarshipSuperjam/engine-template#937) — the one place a deployed repo
# edits to change its kinds; a test holds this list's SET equal to KINDS so the two can never silently desync.
# This list keeps its OWN order because the release notes read in a deliberate sequence, not the vocabulary's
# declaration order (a load-time coupling is deliberately avoided — it would crash the release path on a
# vocabulary edit). Each kind is regex-escaped before matching, since `render_*` are not best-effort wrapped and
# an edited kind carrying a metacharacter must not break the render. Grouping is a DISPLAY view: it never
# touches `_parse_pr_lines`' flat list, which both render sites share.
_RELEASE_NOTE_KINDS = ["Feature", "Improvement", "Fix", "Security", "Removal", "Maintenance"]
_OTHER_KIND = "Other changes"
# The security marker a dependency bot writes into a title AFTER any configured prefix (dependabot-core's
# pr_name_prefixer.rb: `prefix = commit_prefix.to_s; prefix += security_prefix if security_fix?`, where
# security_prefix is "[Security] "). So a CVE fix in a repo that prefixes its bumps arrives as
# "Maintenance: [Security] bump …". A security fix must NEVER read as the upkeep that prefix claims, so the
# marker WINS over the declared kind — on any title that carries it, whoever wrote it.
_SECURITY_MARKER_RE = re.compile(r"^\[security\][ \t]*", re.I)


def _compile_kind_prefix(kinds: list) -> "re.Pattern":
    """The case-insensitive `^Kind:` matcher, with each kind regex-escaped so an edited kind vocabulary
    carrying a metacharacter (a deployer's `C++`, `.NET`) matches literally and cannot make the render throw."""
    return re.compile(r"^(" + "|".join(re.escape(k) for k in kinds) + r"):[ \t]*", re.I)


_KIND_PREFIX_RE = _compile_kind_prefix(_RELEASE_NOTE_KINDS)
_KIND_BY_LOWER = {k.lower(): k for k in _RELEASE_NOTE_KINDS}


def _group_prs_by_kind(lines: list) -> list:
    """Group the plain 'Title (#N)' merged-PR lines by the change kind their title declares as a leading
    'Kind:' prefix — stripping that prefix from the displayed line (the group heading now carries it). A line
    with no recognised prefix collects under 'Other changes'. Returns (kind, [line, …]) pairs in
    `_RELEASE_NOTE_KINDS` order with 'Other changes' always last, skipping any empty group; `[]` in, `[]` out."""
    buckets = {k: [] for k in _RELEASE_NOTE_KINDS}
    other = []
    for ln in lines:
        m = _KIND_PREFIX_RE.match(ln)
        # `re.I` case-folds WIDER than str.lower() (Turkish `İmprovement`, dotless `ımprovement`, long-s
        # `ſecurity`), so a match can carry a spelling this map has no key for. The lookup is therefore TOTAL:
        # an unmappable match falls through to "Other changes" rather than raising. render_* is NOT
        # best-effort wrapped, so a KeyError here would block a release cut over nothing but a title's spelling.
        kind = _KIND_BY_LOWER.get(m.group(1).lower()) if m else None
        rest = ln[m.end():] if kind else ln
        sm = _SECURITY_MARKER_RE.match(rest)
        if sm:
            kind, rest = "Security", rest[sm.end():]
        if kind:
            buckets[kind].append(rest)
        else:
            other.append(rest)
    grouped = [(k, buckets[k]) for k in _RELEASE_NOTE_KINDS if buckets[k]]
    if other:
        grouped.append((_OTHER_KIND, other))
    return grouped


def _render_pr_groups(merged: list, heading) -> list:
    """The merged-PR list as display lines: one `heading(kind)` block per change kind with its bullets under
    it. The two render sites share this — only the heading form differs (a `###` subheading in the published
    Release body; a bold label inside the pull request's one `## Scope` section, whose plain-text peers a
    heading would out-rank). When NOTHING carries a kind, the lone 'Other changes' heading is OMITTED: a
    heading that says "other" is only meaningful against something else, and standing alone it would label a
    reader's whole release as leftovers. So an unadopted convention degrades to EXACTLY the old flat list —
    never worse — which is the state every generated repo starts in."""
    groups = _group_prs_by_kind(merged)
    if len(groups) == 1 and groups[0][0] == _OTHER_KIND:
        return [f"- {p}" for p in groups[0][1]]
    out = []
    for i, (kind, items) in enumerate(groups):
        if i:
            out.append("")
        out += [heading(kind), ""]
        out += [f"- {p}" for p in items]
    return out


def _generate_notes_body(slug: str, previous_tag: str, target: str, token: str | None) -> str:
    """POST /repos/{slug}/releases/generate-notes -> the generated markdown body. Despite the POST verb this
    creates nothing — it is GitHub's read-only release-notes generator. `tag_name` is a placeholder label; the
    listed pull requests depend only on the previous_tag..target range. This builds its own request rather than
    routing through `github_client` DELIBERATELY: the host is a hardcoded literal, it is a single POST with no
    pagination / Link-following, so the off-host guard `github_client` carries has nothing to protect here; a
    future edit that adds pagination should reconsider that."""
    import urllib.request, json as _json, boot   # local: only the real fetch needs these (mirrors resolve_baseline)
    tok = token if token is not None else boot.gh_token()
    payload = _json.dumps({"tag_name": "unreleased", "previous_tag_name": previous_tag,
                           "target_commitish": target}).encode("utf-8")
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "engine-release-cut", "Content-Type": "application/json"}
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    url = f"https://api.github.com/repos/{slug}/releases/generate-notes"
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return (_json.loads(resp.read()) or {}).get("body") or ""


def merged_pr_titles(previous_tag: str | None, target: str, repo: str | None = None,
                     token: str | None = None, *, _fetch=None) -> list:
    """The pull requests merged since the last release, as plain 'Title (#N)' lines — the release's body of
    work, beside the structural floor signals. BEST-EFFORT: any failure (offline, no token, no previous tag,
    an unexpected response) returns [] so the notes simply omit the section — never a crash, never a blocked
    release. `previous_tag` is the last release tag; `target` is the commit-ish being released (the branch tip
    at cut time, the merge commit at publish). `repo` defaults to the engine's home (where the release tags
    and the pull requests live). `_fetch` is injectable so tests run offline."""
    try:
        slug = repo if repo is not None else release_source._home_repository()
        if not slug or not previous_tag or not target:
            return []
        return _parse_pr_lines((_fetch or _generate_notes_body)(slug, previous_tag, target, token))
    except Exception:  # noqa: BLE001 — best-effort; on any failure the section is omitted, never blocking
        return []


# ------------------------------------------------------ declared release impact (StarshipSuperjam/engine-template#942)
_PR_NUMBER_RE = re.compile(r"\(#(\d+)\)\s*$")


def _fetch_pr_meta(slug: str, number: int, token: str | None) -> dict:
    """GET /repos/{slug}/pulls/{number} -> {'body', 'author'}. RAISES on any failure — unlike the cosmetic
    generate-notes read, a pull request's declared impact is version AUTHORITY, so the caller must fail closed on
    an unreadable body rather than silently under-count it. Builds its own request (single GET, hardcoded host,
    no pagination) like _generate_notes_body."""
    import urllib.request, json as _json, boot   # local: only the real fetch needs these
    tok = token if token is not None else boot.gh_token()
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "engine-release-cut"}
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    url = f"https://api.github.com/repos/{slug}/pulls/{number}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = _json.loads(resp.read()) or {}
    return {"body": data.get("body") or "", "author": (data.get("user") or {}).get("login")}


def _fetch_pr_files(slug: str, number: int, token: str | None) -> list:
    """The repo-relative paths a pull request changed -> which PACKAGE a declared impact lands on (StarshipSuperjam/engine-template#942 L10).
    RAISES on any failure, like _fetch_pr_meta: an unreadable file list must fail closed rather than silently
    attribute a change to no package (which would under-bump it). Unlike the single-GET peers this endpoint is
    PAGINATED, so it delegates to the ONE Link-following, cycle-guarded changed-files paginator the codebase already
    homes (weakening_guard.fetch_all_changed_files -> github_client) rather than a second hand-rolled loop that could
    drift or lack a cycle guard (a QA finding). NOTE: this is a SECOND GitHub read per merged pull request beside the
    body read, so a large release makes two sequential calls per PR — a disclosed efficiency cost (only paid on the
    engine cut, which is the only caller that asks for files)."""
    import boot, weakening_guard   # local: only the real fetch needs these (the shared github_client-homed paginator)
    tok = token if token is not None else boot.gh_token()
    return [f.get("filename") for f in weakening_guard.fetch_all_changed_files(slug, number, tok)
            if f.get("filename")]


def _enumerate_merged_pr_lines(previous_tag: str, target: str, repo: str | None = None,
                               token: str | None = None) -> list:
    """The merged-pull-request 'Title (#N)' lines since `previous_tag`, RAISING on a fetch/transport failure.
    This is the version-authority enumerator merged_pr_impacts folds — deliberately NOT merged_pr_titles, which
    swallows every failure and returns [] for the cosmetic notes list. A swallowed connectivity/auth/HTTP failure
    would read as 'zero pull requests merged' and silently under-version a breaking release (a QA blocking
    finding); propagating the exception makes the fold fail CLOSED on that class. It ALSO fails closed on a
    PARTIAL drop: a merged pull request whose number appears in the notes but was NOT counted into the parsed list
    is undisclosed FORMAT DRIFT — a single such line could carry a `major` marker and vanish, so we raise. The
    accounting is SHAPE-AWARE (three QA blocking findings taught this): a well-formed 'What's Changed' line is judged
    by ITS OWN number (the trailing `in …/pull/N`), never a `…/pull/N` its TITLE happens to embed (a title that
    cites another pull request's URL, e.g. "supersedes …/pull/…") — so such a title does not read as a drop; only a line that is NOT a
    'What's Changed' entry (GitHub's `## New Contributors` back-reference) is matched by the loose scan, and it is
    cleared when its number is already accounted. ACCOUNTED = the counted numbers PLUS the release pull request's own
    number(s) (dropped from `parsed` by design, but a New Contributors line can back-reference the release PR itself
    on a repo's first cut). HONEST LIMIT: a 200 response that parses to zero lines with NO `…/pull/N` at all is still
    read as an empty range — indistinguishable from a legitimately-empty range without a second source; the reachable
    classes (network/auth/status, and a named-but-uncounted pull request) close."""
    body = _generate_notes_body(repo, previous_tag, target, token)
    parsed = _parse_pr_lines(body)
    counted = {int(m.group(1)) for line in parsed for m in (_PR_NUMBER_RE.search(line),) if m}
    # The release PR(s) are deliberately dropped from `parsed`; fold their own numbers in so a New Contributors
    # back-reference to the release PR itself (a bot's first-ever merged PR on a fresh repo) never reads as a drop.
    release_nums = {int(m.group(2)) for line in body.splitlines()
                    for m in (_PR_LINE_RE.match(line.strip()),) if m and _RELEASE_PR_RE.match(m.group(1).strip())}
    accounted = counted | release_nums
    for line in body.splitlines():
        s = line.strip()
        m = _PR_LINE_RE.match(s)
        if m:                                              # a well-formed 'What's Changed' line — judge by ITS number
            if int(m.group(2)) in accounted:
                continue                                   # counted, or the release PR (embedded title refs ignored)
            raise RuntimeError(                            # shape-valid but its own number never parsed (defensive)
                f"pull request #{m.group(2)} is a well-formed notes line but did not parse into the release's list "
                f"(format drift) — refusing to under-count the release: {s!r}")
        pull = _PR_PULL_URL_RE.search(s)                   # NOT a 'What's Changed' entry (e.g. New Contributors)
        if not pull:
            continue                                       # not a pull-request line (header / changelog footer / blank)
        if int(pull.group(1)) in accounted:
            continue                                       # a back-reference to an already-accounted pull request
        raise RuntimeError(
            f"pull request #{pull.group(1)} appears in GitHub's generated notes but did not parse into the release's "
            f"pull-request list (format drift) — refusing to under-count the release rather than drop it: {s!r}")
    return parsed


def merged_pr_impacts(previous_tag: str | None, target: str, repo: str | None = None,
                      token: str | None = None, *, want_files: bool = False,
                      _fetch_lines=None, _fetch_meta=None, _fetch_files=None) -> dict:
    """The declared release impact of every pull request merged since the last release — FAIL-CLOSED, because
    this drives the version number. Returns
        {'per_pr': [{'number','title','impact'(canonical|None),'author','files'[paths]}], 'error': <reason|None>}.
    When `want_files` is set, each entry ALSO carries the repo-relative paths the pull request changed (`files`), so
    a later PURE fold can attribute the declared impact to the specific PACKAGE(s) it touched (StarshipSuperjam/engine-template#942 L10) — that
    per-PR file read is version authority too, so it fails closed alongside the body read. Only the ENGINE cut asks
    for files (it is the only path that attributes to packages); a product cut leaves `want_files` false so it never
    inherits a `/pulls/{n}/files` failure it has no use for (a QA finding).
    `error` is non-None when the read could not be PROVEN complete — the enumeration failed, or a non-exempt pull
    request's body could not be read — and the caller then refuses to auto-derive rather than emit a version it
    cannot stand behind (a skipped body would silently drop a `major` PR to a lower release). A pull request's
    `impact` is its parsed marker, or None when absent (an exempt bot PR folds to a default later; a non-exempt
    markerless PR is legacy/undeclared, handled by resolve_release_impact). Injectable for offline tests."""
    fetch_lines = _fetch_lines or _enumerate_merged_pr_lines   # RAISING default (never the swallowing titles list)
    fetch_meta = _fetch_meta or _fetch_pr_meta
    fetch_files = _fetch_files or _fetch_pr_files
    slug = repo if repo is not None else release_source._home_repository()
    if not slug or not previous_tag or not target:
        # No prior tag (first cut) or no target: no pull-request set to fold — not an error, just empty.
        return {"per_pr": [], "error": None}
    # Resolve the token ONCE for the whole fold (avoid re-shelling `gh auth token` per pull request off-CI); only
    # for the REAL fetchers — an injected (test) fetcher path never touches the network, so it stays offline.
    tok = token
    if tok is None and _fetch_lines is None and _fetch_meta is None and _fetch_files is None:
        import boot
        tok = boot.gh_token()
    try:
        lines = fetch_lines(previous_tag, target, repo=slug, token=tok)
    except Exception as exc:  # noqa: BLE001 — a version-authority enumeration; a failure fails CLOSED, never []
        return {"per_pr": [], "error": f"could not list the pull requests merged since {previous_tag}: {exc}"}
    numbered = [(int(m.group(1)), _PR_NUMBER_RE.sub("", line).strip())
                for line in lines for m in (_PR_NUMBER_RE.search(line),) if m]
    per_pr = []
    for i, (number, title) in enumerate(numbered, 1):
        print(f"  reading declared release impact from pull request #{number} ({i}/{len(numbered)})...",
              file=sys.stderr)
        try:
            meta = fetch_meta(slug, number, tok)
        except Exception as exc:  # noqa: BLE001 — a version-authority read; an unreadable body fails CLOSED
            return {"per_pr": [], "error": f"could not read pull request #{number}'s body: {exc}"}
        files: list = []
        if want_files:
            try:
                files = list(fetch_files(slug, number, tok) or [])
            except Exception as exc:  # noqa: BLE001 — the touched-package attribution read; also fails CLOSED
                return {"per_pr": [], "error": f"could not read pull request #{number}'s changed files: {exc}"}
        per_pr.append({"number": number, "title": title,
                       "impact": release_impact.parse_impact(meta.get("body")),
                       "author": meta.get("author"), "files": files})
    return {"per_pr": per_pr, "error": None}


def resolve_release_impact(mechanical_level: str, current_engine: str, per_pr: list,
                           legacy_impact: str | None = None) -> dict:
    """The fold (pure, so it is directly testable): fold the merged pull requests' DECLARED impact, raise it by
    the PROVEN mechanical floor, and compute the concrete floor version — refusing when a declaration is missing
    (legacy) or provably too low (mismatch). Returns a dict carrying `declared`, `effective`,
    `engine_floor_version`, the `per_pr` echo, and — on a problem — a `refusal` {reason, violations, recovery}.

    - A pull request that DECLARED an impact contributes it. A markerless EXEMPT (bot) pull request folds as
      DEFAULT_EXEMPT_IMPACT (patch), named in the evidence — never hidden. A markerless NON-exempt pull request is
      LEGACY/undeclared: the cut cannot auto-derive across it and requires an explicit `legacy_impact` aggregate.
    - MISMATCH (refuse-until-corrected): if the mechanical floor the diff PROVED exceeds the declared impact, the
      cut refuses and names the under-declared pull requests — history stays honest; the operator raises the PR's
      impact and re-cuts.
    - effective = max(declared, mechanical); engine_floor_version = bump(current, effective) unless effective is
      `none` (then None — an all-none release has no floor, so the workflow requires an explicit version rather
      than silently deriving a patch)."""
    declared_candidates: list[str] = []
    defaulted: list[str] = []
    legacy: list[str] = []
    for pr in per_pr:
        if pr.get("impact"):
            declared_candidates.append(pr["impact"])
        elif release_impact.is_author_exempt(pr.get("author")):
            declared_candidates.append(release_impact.DEFAULT_EXEMPT_IMPACT)
            defaulted.append(f"#{pr['number']} {pr['title']} (exempt author "
                             f"{pr.get('author')}; folded as {release_impact.DEFAULT_EXEMPT_IMPACT})")
        else:
            legacy.append(f"#{pr['number']} {pr['title']}")

    if legacy and legacy_impact is None:
        return {"declared": None, "effective": None, "engine_floor_version": None, "per_pr": per_pr,
                "defaulted": defaulted,
                "refusal": {
                    "reason": f"{len(legacy)} merged pull request(s) declare no release impact and predate the "
                              f"impact marker (mandatory since {release_impact.MANDATORY_SINCE}) or were merged past "
                              f"the check; the cut cannot auto-derive across an undeclared pull request",
                    "violations": legacy,
                    "recovery": "Give the pre-marker tranche one explicit aggregate impact with "
                                "--legacy-impact <none|patch|minor|major> (choose the highest true impact across "
                                "them), or add the hidden marker '<!-- engine-release-impact: VALUE -->' (VALUE = "
                                "that pull request's true level: none/patch/minor/major) to each listed pull "
                                "request's body and re-run — the visible '*Release-Impact: …*' line is cosmetic; "
                                "only the hidden marker counts. Going forward every pull request carries its own "
                                "marker."}}
    if legacy_impact is not None and legacy:
        declared_candidates.append(release_impact.canonical_impact(legacy_impact))

    declared = release_impact.max_impact(declared_candidates)   # 'none' when nothing declared

    if release_impact.rank(mechanical_level) > release_impact.rank(declared):
        # The mechanical floor is a WHOLE-RELEASE structural signal (a capability/contract added or removed
        # SOMEWHERE in the diff); the fold cannot attribute it to a specific pull request. So NEVER tell the
        # operator to raise every under-floor pull request — that would order an honest patch relabelled as a
        # breaking change (a QA finding). State the proven floor and the highest declaration, point at the
        # change inventory that NAMES the structural change, and ask them to correct only the pull request
        # responsible for it (or supply an explicit version).
        return {"declared": declared, "effective": None, "engine_floor_version": None, "per_pr": per_pr,
                "defaulted": defaulted,
                "refusal": {
                    "reason": f"the release diff PROVES at least a {mechanical_level} change (named in the change "
                              f"inventory above), but the highest impact any merged pull request declared is "
                              f"{declared} — the pull request that made the {mechanical_level}-level change "
                              f"under-declared it",
                    "violations": [f"proven mechanical floor: {mechanical_level}; highest declared impact: "
                                   f"{declared}"],
                    "recovery": f"Find the pull request responsible for the {mechanical_level}-level change named "
                                f"in the inventory above and raise ITS Release-Impact to {mechanical_level} by "
                                f"editing the HIDDEN marker in its body to '{release_impact.impact_trailer(mechanical_level)}' "
                                f"(the visible '*Release-Impact: …*' line is cosmetic — editing it alone has no "
                                f"effect; the cut and the CI check read only the hidden marker). Do NOT relabel "
                                f"unrelated pull requests (an honest patch stays a patch). Then re-run. Or supply an "
                                f"explicit version at or above the {mechanical_level} floor."}}

    effective = release_impact.max_impact([declared, mechanical_level])
    floor_version = (_bump_at_least(current_engine, effective)
                     if effective in ("patch", "minor", "major") else None)
    return {"declared": declared, "effective": effective, "engine_floor_version": floor_version,
            "per_pr": per_pr, "defaulted": defaulted, "refusal": None}


def _modules_for_paths(paths, surfaces: dict) -> list:
    """The set of owning MODULE ids for a list of repo-relative paths, via the module-surfaces registry
    ({path: [module_id, …]}). A path no module owns (a root file, engine core plumbing, a test) contributes
    nothing. Sorted for a deterministic fold/evidence order."""
    owners: set[str] = set()
    for p in paths or []:
        owners.update(surfaces.get(p) or [])
    return sorted(owners)


def fold_package_impacts(package_floor: dict, present_versions: dict, per_pr: list, surfaces: dict) -> dict:
    """PURE per-package attribution (StarshipSuperjam/engine-template#942 L10): raise each PACKAGE's floor to reflect ONLY the pull requests
    that actually touched it — never the release aggregate. For every merged pull request, its DECLARED impact
    (an exempt bot's markerless PR folds as DEFAULT_EXEMPT_IMPACT; a `none` declaration asserts no compatibility
    impact and never bumps) is folded, max-per-package, into the version of every present module whose surface it
    changed (mapped through `surfaces`). Raise-only: the result is the higher of any existing mechanical floor
    (a migration/retirement already set) and the declared-impact floor, so a second writer never lowers the first.
    Scoping WHERE an impact lands (the touched packages) never INVENTS a level — an unrelated module that only took
    a patch stays a patch even in a minor/major release. Returns {'package_floor': {…}, 'attributions': [note,…]}.

    Pure and offline: `surfaces` and `present_versions` are passed in, `per_pr` carries each PR's `files`, so the
    fold is fully unit-testable without a network or a checkout. `attributions` is keyed by module id so the render
    can print each package's evidence directly under its OWN floor line (never a trailing block that misattributes)."""
    floor = dict(package_floor)
    attributions: dict[str, str] = {}
    # Highest declared level each present package received, and the PRs that drove it (for the evidence line).
    per_module: dict[str, str] = {}
    drivers: dict[str, list] = {}
    for pr in per_pr:
        if pr.get("impact"):
            level = pr["impact"]
        elif release_impact.is_author_exempt(pr.get("author")):
            level = release_impact.DEFAULT_EXEMPT_IMPACT
        else:
            continue                                       # legacy/undeclared: the whole cut already refused
        if level not in ("patch", "minor", "major"):
            continue                                       # `none` asserts no compatibility impact — never a bump
        for mid in _modules_for_paths(pr.get("files"), surfaces):
            if mid not in present_versions:
                continue                                   # a surface whose module is not a present package
            if mid not in per_module or release_impact.rank(level) > release_impact.rank(per_module[mid]):
                per_module[mid] = level
            drivers.setdefault(mid, []).append(f"#{pr['number']} ({level})")
    for mid, level in sorted(per_module.items()):
        candidate = _bump_at_least(present_versions[mid], level)
        prior = floor.get(mid)
        floor[mid] = (candidate if not prior
                      or validate._ver_tuple(candidate) >= validate._ver_tuple(prior) else prior)
        attributions[mid] = f"declared {level} by {', '.join(drivers[mid])}"
    return {"package_floor": floor, "attributions": attributions}


# --------------------------------------------------------------------------- present / baseline sets
def _present_modules() -> dict:
    """id -> manifest for every present module (the live tree)."""
    out = {}
    for _rel, man in module_coherence.discover_manifests():
        mid = man.get("id")
        if mid:
            out[mid] = man
    return out


def _modules_in_tree(tree_root: str) -> dict:
    """id -> manifest for every module manifest under a fetched/injected release TREE root (the
    baseline side of the diff — `discover_manifests` only reads the live tree, so the baseline set
    is read from the release tree here)."""
    import glob as _glob
    out = {}
    for path in sorted(_glob.glob(os.path.join(tree_root, ".engine", "modules", "*", "manifest.json"))):
        man = validate.load_json(path)
        mid = man.get("id")
        if mid:
            out[mid] = man
    return out


# --------------------------------------------------------------------------- floor classification
def _bump_at_least(current: str, level: str) -> str:
    """The version `current` bumped to at least the given `level` (major|minor|patch). Used to express the
    mechanical FLOOR as a concrete next version for the change inventory (the engine floor uses major/minor; a
    product cut's derive-default uses patch); the maintainer may raise it."""
    parts = list(validate._ver_tuple(current))
    while len(parts) < 3:
        parts.append(0)
    major, minor, patch = parts[0], parts[1], parts[2]
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    if level == "patch":
        return f"{major}.{minor}.{patch + 1}"
    # Fail closed on anything else (including 'none' or a typo): a silent patch-bump fallback would let a caller
    # that passes an off-enum level get a wrong-but-plausible version (QA). Callers gate 'none' out before here.
    raise ValueError(f"_bump_at_least: level must be one of major/minor/patch, not {level!r}")


def _max_level(a: str, b: str) -> str:
    """The higher of two impact/floor levels on the none<patch<minor<major ladder. Delegates to release_impact
    so the ordering lives in ONE home (that ladder IS the version math; a second copy could drift into a
    different answer). A level of 'unknown' is not on the ladder — it never reaches here (the classifier folds
    only minor/major floors; see _impact_statements)."""
    return release_impact.max_impact((a, b))


def _norm_ver(v):
    """The length-normalized version key both accumulation guards compare on. Delegates to
    module_manager._ver_key — the single normalizer the upgrade selectors also use, so the guards and the
    selectors can never drift on the 2-vs-3-part boundary; see _ver_key for why the normalization is load-bearing."""
    return module_manager._ver_key(v)


def _accumulation_violations(was: dict, present: dict, block: str, message) -> list:
    """Every version-key a RETAINED module shipped in the previous release but the candidate no longer declares,
    for the named version-keyed `block` (`migrations` or `retired_capabilities`). Both are replayed by version
    RANGE at upgrade (from < ver <= target), so a key silently removed from a manifest is SKIPPED on a
    multi-version jump — the StarshipSuperjam/engine-template#599 silent-skip class. Keys are compared on NORMALIZED tuples (`_norm_ver`). A
    whole REMOVED module is NOT checked here — its still-unseen entries for a lagging upgrader are a KNOWN BOUND
    handled with the min-upgradeable-from floor. `message(mid, ver)` builds the block-specific refusal line.

    Coverage assumes a POPULATED baseline: a missing baseline tree fails closed upstream (classify raises), but a
    baseline that resolves yet carries no module manifests compares against an empty set and finds no drop — loud
    in practice (every present module then reads as newly Added and forces a major floor), so the residual gap is
    low, but the hard fail-closed guarantee is only at the no-tree level."""
    out = []
    for mid, man in present.items():
        old = was.get(mid)
        if not old:
            continue
        new_keys = {_norm_ver(k) for k in (man.get(block) or {})}
        for ver in sorted((old.get(block) or {}), key=validate._ver_tuple):
            if _norm_ver(ver) not in new_keys:
                out.append(message(mid, ver))
    return out


def _migration_accumulation_violations(was: dict, present: dict) -> list:
    """Dropped migration version-keys on retained modules. The sanctioned way to retire a transform is to KEEP
    its key with a no-op `run`, never to delete the key — so a drop is always a defect the cut refuses."""
    return _accumulation_violations(
        was, present, "migrations",
        lambda mid, ver: (f"the '{mid}' capability dropped the upgrade step for version {ver} that the last "
                          f"release shipped; an engine updating across this version would skip it"))


def _retired_capabilities_accumulation_violations(was: dict, present: dict) -> list:
    """Dropped retired-capability version-keys on retained modules. Unlike a migration there is NO no-op form to
    retire the announcement to: the key must persist for the life of the module, or a lagging upgrader crossing
    that version silently never sees the notice. So the recovery is simply — never drop the key."""
    return _accumulation_violations(
        was, present, "retired_capabilities",
        lambda mid, ver: (f"the '{mid}' capability dropped its retired-capability notice for version {ver} that "
                          f"the last release shipped; an engine updating across this version would never see it "
                          f"— restore the key (a retirement notice has no no-op form, so it must never be dropped)"))


def _engine_in_tree(tree_root: str | None) -> dict:
    """The engine manifest (.engine/engine.json) under a fetched/injected BASELINE tree, or {} when absent or
    unreadable. FAIL-OPEN by design: a baseline cut before `removed_capabilities` shipped carries none, and the
    test baseline helper writes only module manifests (no engine.json) — a strict read would spuriously fail the
    retention leg on every such baseline. The missing-baseline-TREE case is still fail-CLOSED upstream (classify
    raises when a prior release exists but no tree was provided); this only tolerates a tree that carries no
    engine.json."""
    if not tree_root:
        return {}
    path = os.path.join(tree_root, ".engine", "engine.json")
    if not os.path.isfile(path):
        return {}
    try:
        return validate.load_json(path) or {}
    except Exception:   # noqa: BLE001 — a malformed baseline engine.json degrades to "no prior notices", never a crash
        return {}


def _removed_capability_violations(baseline_tree: str | None, removed_modules: list, live_engine: dict,
                                   present: dict) -> list:
    """The whole-module removal-notice guard — the third sibling of the migration / retired-capability
    accumulation guards, but keyed FLAT by module-id (engine.json `removed_capabilities`), NOT version-keyed, so
    it is NOT built on `_accumulation_violations` (which iterates a per-module version-keyed sub-block). Two legs:

      - MISSING-NOTICE: a module this cut removes (`removed_modules` = baseline − present) with no
        `removed_capabilities[mid]` line in the live engine.json. A validly-cut release must carry the plain-
        language line so the deployer's update can both announce the loss AND treat the module's absence as
        intentional (the upgrade reconciles it away rather than refusing) — so its absence is a refusal.
      - RETENTION: a `removed_capabilities` key the BASELINE release recorded that the candidate dropped — the
        notice would silently vanish for a lagging upgrader who still holds the module. Refuse UNLESS (a) the
        module is present again (a re-add legitimately clears the entry) or (b) its `removed_in` is at or below
        the release's clean-upgrade floor (`min_upgradeable_from`), because then no supported upgrader can still
        hold the module and the notice can never fire — so pruning it is safe (this is the schema's documented
        obsolescence escape; the live floor is used as a conservative stand-in, since the release floor only ever
        advances from it)."""
    live_rc = live_engine.get("removed_capabilities") or {}
    out = []
    for mid in sorted(set(removed_modules)):
        if mid not in live_rc:
            out.append(f"the '{mid}' capability was removed but no plain-language removal notice was recorded "
                       f"for it — add it to engine.json removed_capabilities: {{ \"{mid}\": {{ \"description\": "
                       f"\"…what an operator could ask for before and no longer can…\" }} }} so the update can "
                       f"tell the operator what they lost instead of refusing")
    floor = live_engine.get("min_upgradeable_from")
    base_rc = _engine_in_tree(baseline_tree).get("removed_capabilities") or {}
    for mid in sorted(base_rc):
        if mid in live_rc or mid in present:
            continue
        removed_in = (base_rc.get(mid) or {}).get("removed_in")
        if floor and removed_in and _norm_ver(removed_in) <= _norm_ver(floor):
            continue   # obsolete: no supported upgrader can still hold the module → safe to prune
        out.append(f"the '{mid}' removal notice recorded by the last release was dropped; an engine still "
                   f"holding '{mid}' would update across the removal and never learn what it lost — restore the "
                   f"key (it may only be pruned once the version it was removed in, removed_in, is at or below "
                   f"this release's oldest-still-upgradeable version, min_upgradeable_from — after which no "
                   f"engine can still hold '{mid}')")
    return out


def _dependency_integrity_violations(present: dict, removed_modules: list) -> list:
    """A surviving module that still `depends` on a module THIS cut removes. Removing a depended-on capability
    leaves the survivor's dependency dangling on every deployer's upgrade (and, once the update reconciles the
    removed module away, dead-ends that upgrade at the coherence gate with no operator recourse). Belt-and-
    suspenders with the branch coherence gate and plan_remove's reverse-dependency refusal — consistent with how
    the cut also refuses a dropped migration CI would already have caught. Flags ONLY a dep this cut removes; a
    dep absent for any other reason is a pre-existing coherence concern the branch gate owns."""
    removed_set = set(removed_modules)
    out = []
    for mid, man in sorted(present.items()):
        for dep in sorted((man or {}).get("depends") or {}):
            if dep in removed_set:
                out.append(f"the '{mid}' capability still needs the '{dep}' capability this release removes; "
                           f"keep '{dep}', or remove '{mid}' too")
    return out


# A default-on module is installed on every deployment unless the operator opts out (StarshipSuperjam/engine-template#759). It may therefore
# depend only on capabilities that are ALSO guaranteed present — required (always) or other default-on (unless
# opted out, and StarshipSuperjam/engine-template#759 keeps a default-on's own deps satisfied). Everything else is not guaranteed there.
_DEFAULT_ON_ALLOWED_DEP_TIERS = frozenset({"required", "default-on"})


def _default_on_dependency_violations(present: dict) -> list:
    """A `default-on` module that `depends` on a capability NOT guaranteed to be present — anything outside
    {required, default-on}: an `optional`/`experimental` module a deployment may never have chosen, or a `retired`
    one kept only for migration history. Such a module cannot be coherently installed where the dependency is
    absent. StarshipSuperjam/engine-template#759 already handles this at runtime by DEMOTING the default-on module to an offer rather than pulling
    the unchosen dependency in; this guard catches the same contradiction once, at the author's release cut, so a
    release is never cut needing that per-deployment safety net (and so the FIRST cut, which has no runtime
    predecessor to lean on, is covered too — this field is set in both classify() modes).

    An ALLOWLIST: only {required, default-on} deps are sound; every other tier — and a missing/unknown status
    (already a schema defect, flagged fail-closed) — is refused. Judges only the STATUS of a dep PRESENT in this
    set; a dep absent for any reason is a dependency-presence concern the branch coherence gate owns, not this
    guard, so the two never double-report. Direct dependencies only: applied to every default-on module the rule
    covers default-on chains inductively (a default-on link's own bad dep is flagged when that link is checked). A
    `default-on -> required -> optional` reach is a distinct required-depends-on-optional smell, out of scope."""
    out = []
    for mid, man in sorted(present.items()):
        if (man or {}).get("status") != "default-on":
            continue
        for dep in sorted((man or {}).get("depends") or {}):
            dep_man = present.get(dep)
            if dep_man is None:
                continue   # a missing dependency is the branch coherence gate's concern, not this status guard
            tier = dep_man.get("status")
            if tier in _DEFAULT_ON_ALLOWED_DEP_TIERS:
                continue
            shown = f"'{tier}'" if tier else "of an unset/unknown tier"
            out.append(f"the default-on '{mid}' capability depends on '{dep}', which is {shown} — not "
                       f"guaranteed present on every deployment; a default-on module may depend only on "
                       f"required or default-on capabilities, so make '{dep}' required or default-on, or make "
                       f"'{mid}' optional")
    return out


def _local_reference_violations() -> "tuple[list, str]":
    """(violations, note) for the shipped local-reference floor at an ENGINE cut (StarshipSuperjam/engine-template#943).

    A bare declared local reference — a decision-record id, a spec section, a ticket prefix — on a traveling
    surface names a record a generated repository cannot reach: a dead pointer for every downstream reader. This
    scans the CANDIDATE tree's shipped surface against the deployment's declared vocabulary — the SAME scan the
    per-PR floor `shipped_local_references_check` runs, reused via its `hits()` so the two never disagree about
    the surface — and returns one refusal per hit. The release cut is the last-line backstop behind that per-PR
    floor; it refuses BEFORE `apply` writes, and is set in BOTH classify() modes (a first cut has no
    predecessor to lean on, and must not ship dangling references either).

    The `note` is the DISCLOSED-not-silent half (operator decision on StarshipSuperjam/engine-template#943): when the deployment has declared
    NO vocabulary (absent/empty/unusable/unreadable) there is nothing to scan — a legitimate steady state for a
    product with no shorthand of its own, but at an engine cut a removed or emptied declaration would silently
    switch this floor off. So rather than pass wordlessly the cut states plainly that the scan did not run. It
    never REFUSES on an absent declaration (that is not a defect); it only refuses to be silent. The declaration
    is deliberately left unguarded so tuning it stays cheap (StarshipSuperjam/engine-template#639); this note is the visibility that
    replaces a guard. Reads validate.ROOT — the candidate tree being cut — for both the declaration and the
    scan, so the two never disagree about which tree is the release; tests repoint validate.ROOT via _Tree."""
    root = validate.ROOT
    vocabulary, state = local_references.load_vocabulary(os.path.join(root, local_references.DECLARATION_REL))
    if not vocabulary:
        return [], (
            "This release was not scanned for bare local references that would dangle in a generated "
            f"repository: no local-reference vocabulary is declared (`{local_references.DECLARATION_REL}` is "
            f"{state}). That is fine for a product with no shorthand of its own; if this is the engine's own "
            "release, restore the declaration so the floor runs.")
    found = shipped_local_references_check.hits(root, vocabulary)
    if found is None:
        return ([
            "the shipped surface could not be enumerated to scan for bare local references — the first-run "
            "retire census (.engine/provisioning/first-run-assets.json) is unreadable, so this release cannot "
            "be confirmed free of dangling references"], "")
    out = []
    for h in found:
        where = h.get("where") or "(unknown file)"
        line = h.get("line")
        loc = where + (f" line {line}" if line else "")
        out.append(f"{loc} carries a bare local reference ({h.get('token')}) that ships into every generated "
                   f"repository, where it names a record the reader cannot reach")
    return out, ""


def classify(baseline: Baseline, baseline_tree: str | None) -> dict:
    """The proposal: the floor per package + engine, the change inventory, and the impact statements.
    In first-cut mode there is no baseline to diff, so no delta/floor is derived — the initial version
    is the maintainer's explicit choice."""
    present = _present_modules()
    engine = module_coherence.load_engine_manifest() or {}
    inventory: list[str] = []
    impacts: list[dict] = []
    package_floor: dict[str, str] = {}
    engine_level = "none"
    # The shipped local-reference backstop — a bare declared local reference on a traveling surface would dangle
    # in every generated repository (StarshipSuperjam/engine-template#943). Baseline-independent (it reads the candidate tree, not the
    # diff), so it is computed once and set in BOTH modes; the note discloses an absent declaration rather than
    # passing silently.
    lref_violations, lref_note = _local_reference_violations()

    if baseline.first_cut:
        inventory.append(
            f"First release: establishes the baseline version for the engine and all "
            f"{len(present)} installed packages. No prior release exists to diff against, so the "
            f"initial version is chosen, not derived.")
        return {
            "mode": "first-cut",
            "baseline": None,
            "baseline_note": baseline.note,
            "current_engine": engine.get("engine_release"),
            "engine_floor_level": "none",
            "engine_floor_version": None,   # first cut: no prior release, so no mechanical floor to meet
            "package_floor": {},
            "change_inventory": inventory,
            "impacts": impacts,
            "compatibility_unknown": [],
            # A default-on module depending on a not-guaranteed-present capability — baseline-independent, so it is
            # refused on the FIRST cut too (the diff siblings above need a baseline and so are absent here).
            "default_on_dependency_violations": _default_on_dependency_violations(present),
            # A bare declared local reference on a traveling surface (StarshipSuperjam/engine-template#943) — baseline-independent, so the
            # FIRST cut is scanned too; the note discloses an absent declaration rather than passing silently.
            "local_reference_violations": lref_violations,
            "local_reference_note": lref_note,
        }

    # diff mode — compare the present set against the baseline release tree
    if not baseline_tree:
        raise RuntimeError(
            "a prior release exists but no baseline tree was provided to diff against; the release "
            "workflow fetches it (release_source._fetch_release_tree), and tests inject a local tree.")
    was = _modules_in_tree(baseline_tree)
    added = sorted(set(present) - set(was))
    removed = sorted(set(was) - set(present))

    for mid in added:
        inventory.append(f"Added the '{mid}' capability.")
        engine_level = _max_level(engine_level, "minor")
    for mid in removed:
        inventory.append(f"Removed the '{mid}' capability.")
        engine_level = _max_level(engine_level, "major")

    for mid, man in present.items():
        old = was.get(mid)
        if not old:
            continue
        new_migs = set((man.get("migrations") or {}).keys())
        old_migs = set((old.get("migrations") or {}).keys())
        if new_migs - old_migs:
            keys = ", ".join(sorted(new_migs - old_migs))
            inventory.append(f"'{mid}' gained a data/config migration ({keys}).")
            # A migration MOVES the package version (a patch) so the updater's version-ranged migration machinery
            # sees it in a new release — but its mere existence is NOT a SemVer signal: a migration can accompany a
            # patch bug-fix, a minor feature, or a major break. The semantic level comes from the declared PR
            # impact or a proven floor, never from the migration's presence (StarshipSuperjam/engine-template#942). The dropped-migration
            # refusal (migration_violations) is a SEPARATE, untouched axis.
            package_floor[mid] = _bump_at_least(man.get("version", "0.0.0"), "patch")
        # A newly-announced retired capability floors its module minor too — it is an operator-visible removal.
        # This RAISES the floor; it never certifies severity: a breaking removal must still carry its own
        # major/impact signal (a whole-module removal already floors major above). Combine with any migration
        # floor by taking the higher version so a second writer never clobbers the first. TODAY both compute the
        # SAME minor bump of this manifest's version, so the max is a formality — but structuring it as a combine
        # (not a bare re-assign) keeps the floor correct the day one side gains a different bump level, instead of
        # silently clobbering it (design-review).
        new_rets = set((man.get("retired_capabilities") or {}).keys())
        old_rets = set((old.get("retired_capabilities") or {}).keys())
        if new_rets - old_rets:
            keys = ", ".join(sorted(new_rets - old_rets))
            inventory.append(f"'{mid}' announced a retired capability ({keys}).")
            floor = _bump_at_least(man.get("version", "0.0.0"), "minor")
            prior = package_floor.get(mid)
            package_floor[mid] = (floor if not prior
                                  or validate._ver_tuple(floor) >= validate._ver_tuple(prior) else prior)

    # contract / seam / interface / wiring changes carry an AI-authored impact statement. Only a PROVABLE floor
    # raises the engine level — `minor` for a genuinely added surface, `major` for a genuine removal. A renamed/
    # relocated or in-place-changed surface is `unknown`: it sets NO floor (a byte diff cannot prove
    # compatibility), and is surfaced for review, where the declared PR impact governs (StarshipSuperjam/engine-template#942).
    impacts = _impact_statements(baseline_tree)
    for im in impacts:
        if im["floor_level"] in ("minor", "major"):
            engine_level = _max_level(engine_level, im["floor_level"])
    compatibility_unknown = [im for im in impacts if im["floor_level"] == "unknown"]

    if not inventory and not impacts:
        inventory.append(_NO_STRUCTURAL_SIGNAL_NOTE)

    # The concrete mechanical floor version: the minimum next engine version a minor/major signal forces
    # (None when nothing structural fired — a patch is discretionary, so raise-only alone bounds it). This is
    # what `apply` enforces the chosen version against and what the PR body shows the maintainer to check.
    current_engine = engine.get("engine_release", SENTINEL)
    engine_floor_version = (_bump_at_least(current_engine, engine_level)
                            if engine_level in ("minor", "major") else None)

    return {
        "mode": "diff",
        "baseline": baseline.ref,
        "baseline_note": baseline.note,
        "current_engine": current_engine,
        "engine_floor_level": engine_level,
        "engine_floor_version": engine_floor_version,
        "package_floor": package_floor,
        "change_inventory": inventory,
        "impacts": impacts,
        # Contract/interface surfaces whose compatibility the STRUCTURE could not prove (renamed or changed in
        # place) — they set no floor; the renderers surface them in the Risk section as "review required", since
        # the human is the backstop for that category (StarshipSuperjam/engine-template#942 design-review).
        "compatibility_unknown": compatibility_unknown,
        # A dropped migration key on a retained module — the cut is refused on this, before apply writes (see
        # _cmd_propose). Empty on a clean diff; a stable field of the diff proposal so the refusal is legible.
        "migration_violations": _migration_accumulation_violations(was, present),
        # A dropped retired-capability key — same range-skip class as a dropped migration, but the notice, not a
        # transform, is what silently vanishes for a lagging upgrader. Its own field + its own refusal message,
        # because the recovery differs: a retirement has no no-op form, so the only recourse is to never drop it.
        "retired_capability_violations": _retired_capabilities_accumulation_violations(was, present),
        # The modules this cut removes (baseline − present), STRUCTURED so `apply` can stamp `removed_in` onto
        # exactly their engine.json removed_capabilities entries — the change inventory carries the same fact as
        # prose, which is not machine-consumable.
        "removed_modules": removed,
        # A whole-module removal with no plain-language notice, or a prior release's notice dropped (the FLAT
        # module-keyed sibling of the two accumulation guards). Refused at the cut: a validly-cut release must
        # carry the notice so the deployer's update can announce the loss AND treat the drop as intentional.
        "removed_capability_violations": _removed_capability_violations(baseline_tree, removed, engine, present),
        # A surviving module that still depends on a module this cut removes — a dangling dependency that would
        # dead-end every holder's upgrade at the coherence gate; refused at the cut (belt-and-suspenders).
        "dependency_violations": _dependency_integrity_violations(present, removed),
        # A default-on module depending on a capability outside {required, default-on} — one a deployment may lack,
        # so it cannot be coherently installed everywhere; refused at the cut so the author fixes it once (StarshipSuperjam/engine-template#891).
        "default_on_dependency_violations": _default_on_dependency_violations(present),
        # A bare declared local reference on a traveling surface — a dead pointer for every downstream reader;
        # refused at the cut as the last-line backstop behind the per-PR floor (StarshipSuperjam/engine-template#943). The note discloses an
        # absent declaration rather than passing silently.
        "local_reference_violations": lref_violations,
        "local_reference_note": lref_note,
    }


# --------------------------------------------------------------------------- product proposal (StarshipSuperjam/engine-template#516)
def _product_proposal(baseline: Baseline, current_version: str, merged_prs: list) -> dict:
    """The release proposal for a PRODUCT cut — the SAME mode-neutral shape the workflow shell and the
    renderers consume, with product semantics. A product has no engine packages to diff, so there is no
    mechanical capability floor at all: `engine_floor_version` is left None here and the DECLARED-impact fold
    (_apply_impact_fold, in the product cut path) sets the effective floor from the merged pull requests
    (StarshipSuperjam/engine-template#942). Absence of a structural floor means "no floor", NOT "patch": an all-none product tranche
    derives no version and the workflow requires an explicit one — it never silently patches. None on a first
    cut too (the version is chosen, not derived). The `product` marker rides in the proposal so the renderers
    and the publisher speak of the PRODUCT."""
    first_cut = baseline.first_cut
    note = ("this deployment has no published release yet — this is the first release of your product."
            if first_cut else f"releasing your product; the last release was {baseline.ref}.")
    inventory = (["First release: establishes the starting version of your product. No prior release exists, "
                  "so the version is chosen, not derived."] if first_cut else [])
    return {
        "mode": "first-cut" if first_cut else "diff",
        "product": True,
        "baseline": baseline.ref,
        "baseline_note": note,
        "current_engine": current_version,           # the current PRODUCT version (the renderers' generic key)
        "engine_floor_level": "none",                # a product has no structural capability floor
        # StarshipSuperjam/engine-template#942: the declared-impact fold sets the effective floor. None here means "no floor" — an all-none
        # product tranche does NOT silently become a patch; the workflow requires an explicit version instead.
        "engine_floor_version": None,
        "package_floor": {},
        "change_inventory": inventory,
        "impacts": [],
        "compatibility_unknown": [],
        "merged_prs": merged_prs,
    }


# --------------------------------------------------------------------------- impact statements
_CONTRACT_GLOBS = (
    os.path.join(".engine", "contracts"),        # eADR contracts
    os.path.join(".engine", "interfaces"),        # interface surfaces
)


# A removed↔added surface pair this similar (by LINE similarity) reads as a RENAME. Set HIGH (0.85, not git's
# 0.5) deliberately: a genuine rename keeps NEAR-IDENTICAL content, whereas two UNRELATED files that merely share
# heavy structural boilerplate — this repo's own eADRs share ~10 identical frontmatter/heading lines and score
# 0.77 line-similarity between unrelated decisions (a QA re-audit reproduced this) — must NOT pair, or a genuine
# breaking removal would be masked as a rename and lose its major floor. The bias is SAFETY: when content
# similarity is ambiguous, DON'T pair, so the removal keeps its major floor (over-flagging major is safe;
# masking a removal is not). A rename that also substantially rewrites content falls below the bar and is treated
# as a removal+addition — the conservative, non-masking outcome.
_RENAME_SIMILARITY = 0.85


def _rename_lines(raw: bytes) -> list:
    """A surface's content as a list of LINES for rename similarity. Line-level (not byte-level) matches git's
    own rename detection AND resists the boilerplate-collision false positive a QA review reproduced: two
    unrelated contract files that merely share a Status/Context/Decision header collide at ~0.76 BYTE similarity
    (short files, header bytes dominate) but only ~0.25 LINE similarity (the shared header is a few lines out of
    many). It is also markedly cheaper — comparing ~N lines, not ~5000 bytes, per file pair."""
    return (raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)).splitlines()


def _pair_renames(removed: dict, added: dict) -> dict:
    """Pair each removed surface with the most similar added surface above _RENAME_SIMILARITY — the in-process
    equivalent of git's rename detection (difflib LINE similarity, like git itself), so a renamed or relocated
    contract file is recognised as a rename rather than a remove+add. Returns {removed_name: added_name} for the
    detected renames (each added surface used at most once, best match first). Kept dependency-free and working
    on the fetched-tree byte maps _dir_bytes already produced (git --no-index would need both trees on disk in a
    repo; this does not). LINE-level, not byte-level, to resist boilerplate-collision false positives (QA)."""
    import difflib
    removed_lines = {n: _rename_lines(b) for n, b in removed.items()}
    added_lines = {n: _rename_lines(b) for n, b in added.items()}
    pairs: dict[str, str] = {}
    used_added: set[str] = set()
    # Deterministic, best-ratio-first: sort candidate (removed, added) pairs by descending similarity.
    scored = []
    for r_name in removed_lines:
        for a_name in added_lines:
            ratio = difflib.SequenceMatcher(None, removed_lines[r_name], added_lines[a_name]).ratio()
            if ratio >= _RENAME_SIMILARITY:
                scored.append((ratio, r_name, a_name))
    for _ratio, r_name, a_name in sorted(scored, key=lambda t: (-t[0], t[1], t[2])):
        if r_name in pairs or a_name in used_added:
            continue
        pairs[r_name] = a_name
        used_added.add(a_name)
    return pairs


def _impact_statements(baseline_tree: str) -> list[dict]:
    """For each contract/interface surface that differs between the baseline tree and the live tree, an
    AI-authored plain-language impact statement, tagged with the compatibility floor the STRUCTURE can PROVE —
    and only where it genuinely can (StarshipSuperjam/engine-template#942):

      * a surface ADDED (with no rename partner) -> `minor` floor (additive — nothing depended on it yet);
      * a surface REMOVED (with no rename partner) -> `major` floor (a genuine breaking removal);
      * a surface RENAMED/relocated (a removed↔added pair above the similarity bar) -> `unknown` (the surface
        persists; a rename is tech-debt evolution, NOT a removal — the declared PR impact governs);
      * a surface CHANGED in place -> `unknown` (a byte diff cannot prove compatibility either way).

    An `unknown` floor sets NO version number by itself — it is surfaced as "compatibility unknown — review
    required" (the renderers put these in the Risk section, where the human is the backstop) and the declared PR
    impact governs. This replaces the old blunt added->minor / removed->major / changed->minor, whose
    remove+add reading of a rename produced a FALSE major. The break/no-break demonstration marking stays honest
    (no acceptance-benchmark instrument exists, so it is named, not faked)."""
    demo = ("none — no behavioral correlate is available for this signal, so this rests on the impact statement "
            "and your confirmation; the release is consciously sub-bar on this signal, named here.")
    out: list[dict] = []
    for sub in _CONTRACT_GLOBS:
        live = _dir_bytes(os.path.join(validate.ROOT, sub))
        base = _dir_bytes(os.path.join(baseline_tree, sub))
        changed = {n for n in set(live) | set(base) if live.get(n) != base.get(n)}
        added = {n: live[n] for n in changed if n not in base}
        removed = {n: base[n] for n in changed if n not in live}
        renames = _pair_renames(removed, added)          # {removed_name: added_name}
        renamed_added = set(renames.values())
        for name in sorted(changed):
            if name in renames:                          # the removed half of a rename pair
                what, level = (f"the contract surface '{name}' was renamed/relocated to "
                               f"'{renames[name]}'"), "unknown"
                why = ("a rename/relocation is not a removal — the surface persists, so this is tech-debt "
                       "evolution; the declared release impact governs. Review the move against consumers.")
            elif name in renamed_added:                  # the added half of a rename pair — reported by its old name
                continue
            elif name in added:
                what, level = f"a new contract surface '{name}' was added", "minor"
                why = "new surfaces are additive — nothing existing depended on it yet."
            elif name in removed:
                what, level = f"the contract surface '{name}' was removed", "major"
                why = "removing a surface other parts may depend on is a breaking change."
            else:
                what, level = f"the contract surface '{name}' changed in place", "unknown"
                why = ("a changed contract can be additive or breaking depending on its consumers — a byte diff "
                       "cannot prove which, so the declared release impact governs; read the change against them.")
            out.append({
                "surface": os.path.join(sub, name),
                "what": what,
                "why": why,
                "floor_level": level,          # 'minor' | 'major' | 'unknown' — 'unknown' sets no floor
                "behavioral_demo": demo,
            })
    return out


def _dir_bytes(d: str) -> dict:
    """relative-path -> raw bytes for every file ANYWHERE under `d`, recursively (empty when the dir is
    absent). Recursive so a contract/interface surface in a subdirectory (e.g. `contracts/instance/…`) is
    diffed too — a non-recursive read silently skipped an entire subtree, so a nested surface added, changed,
    or removed produced no impact statement and no floor signal."""
    out = {}
    if not os.path.isdir(d):
        return out
    for root, _dirs, files in os.walk(d):
        for name in files:
            p = os.path.join(root, name)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, d)] = fh.read()
    return out


# --------------------------------------------------------------------------- apply (the writer)
def _target_versions(engine_ver: str, all_ver: str | None, packages: dict, present: dict) -> dict:
    """The concrete version each package is written to: `--all` sets every present package, an explicit
    `--package id=ver` overrides, and any package left unspecified keeps its current version."""
    out = {}
    for mid, man in present.items():
        if mid in packages:
            out[mid] = packages[mid]
        elif all_ver is not None:
            out[mid] = all_ver
        else:
            out[mid] = man.get("version", SENTINEL)
    return out


def _raise_only_violations(engine_ver: str, targets: dict, engine_cur: str, present: dict) -> list[str]:
    """Every target that is NOT strictly greater than its current on-disk version — the raise-only
    guard. The guard itself is strict; the caller passes only the capabilities
    actually being WRITTEN (a no-op keep at the current version is excluded upstream, so this flags a
    genuine lowering, never an unchanged capability). A returned non-empty list means the write must be
    refused."""
    bad = []
    if not _strictly_greater(engine_ver, engine_cur):
        bad.append(f"engine version {engine_ver} is not higher than the current {engine_cur}")
    for mid, ver in targets.items():
        cur = present[mid].get("version", SENTINEL)
        if not _strictly_greater(ver, cur):
            bad.append(f"package '{mid}' version {ver} is not higher than the current {cur}")
    return bad


def _schema_ok(instance, schema_path: str) -> list[str]:
    import jsonschema  # lazy: a tool-runtime dep absent on the bare-3.9 arrival floor, so keep it out of module
    # import so `import release_cut` stays 3.9-safe (arrive() reaches pr_section/template_preamble on that floor;
    # StarshipSuperjam/engine-template#755) — mirrors validate.py's lazy third-party discipline. This is the only jsonschema user here.
    schema = validate.load_json(schema_path)
    v = jsonschema.Draft202012Validator(schema)
    return [e.message for e in v.iter_errors(instance)]


def apply(engine_ver: str, all_ver: str | None, packages: dict, proposal: dict | None,
          dry_run: bool, min_upgradeable_from: str | None = None) -> dict:
    """Record the chosen versions atomically. Returns a result dict (applied/refused + the proposed-vs-
    applied record for traceability). Writes nothing on a raise-only violation or a validation failure.
    `min_upgradeable_from` (optional) records the clean-upgrade floor into engine.json; a malformed value is
    refused fail-loud at the door (below), never persisted. When None, any prior floor is carried forward
    unchanged (engine.json is copied byte-preserved)."""
    present = _present_modules()
    engine = module_coherence.load_engine_manifest()
    if engine is None:
        raise RuntimeError("the engine manifest (.engine/engine.json) is missing; cannot cut a release.")
    engine_cur = engine.get("engine_release", SENTINEL)
    targets = _target_versions(engine_ver, all_ver, packages, present)

    # Auto-raise each floored capability to its mechanical floor. When a proposal is
    # supplied, a capability a change REQUIRES to bump (a new migration => its package_floor entry) is
    # written to that floor unless the caller set it explicitly with `--package`. This is the per-capability
    # analogue of the engine version auto-deriving to its floor: the release workflow passes no `--package`,
    # so without this a migration-bearing cut would keep the capability at its current version and then refuse
    # on below-confirmed-floor with no way to bump it.
    if proposal:
        for mid, floor in (proposal.get("package_floor") or {}).items():
            if mid in present and mid not in packages and _strictly_greater(floor, targets[mid]):
                targets[mid] = floor

    # version grammar: refuse a non-version string at the door (a typo must not reach a manifest)
    bad_fmt = []
    if not _valid_version(engine_ver):
        bad_fmt.append(f"engine version '{engine_ver}' is not a valid version (expected like 1.2.0 or 1.0.0-rc1)")
    for mid, ver in targets.items():
        if not _valid_version(ver):
            bad_fmt.append(f"package '{mid}' version '{ver}' is not a valid version (expected like 1.2.0)")
    if min_upgradeable_from is not None and not _valid_version(min_upgradeable_from):
        bad_fmt.append(f"minimum-upgradeable-from '{min_upgradeable_from}' is not a valid version "
                       f"(expected like 0.3.2) — a malformed floor would silently disable the upgrade guard")
    if bad_fmt:
        return {"applied": False, "reason": "invalid-version", "violations": bad_fmt,
                "recovery": "use dotted-number versions, optionally with a -prerelease suffix (1.2.0, 1.0.0-rc1)."}

    # The capabilities this cut actually WRITES: those whose version changes. A capability left at its current
    # version is a no-op keep — an unchanged capability keeps its recorded version (the locked module-system
    # law: per-package versions are independent recorded state, bumped only on that capability's own signal),
    # not a lowering — so it is neither rewritten nor raise-only-checked. This is what lets an engine-only cut
    # (the engine version moves; no capability changed) apply, instead of refusing because unchanged
    # capabilities are not strictly greater than themselves.
    changed = {mid: ver for mid, ver in targets.items() if ver != present[mid].get("version", SENTINEL)}

    # raise-only over the engine + the CHANGED set: a target that is a detectable
    # lowering is refused loudly (the guard is strict and unchanged — only the set it sees is narrowed to the
    # capabilities being written); the engine version must strictly increase. Nothing is ever silently lowered.
    violations = _raise_only_violations(engine_ver, changed, engine_cur, present)
    if violations:
        return {"applied": False, "reason": "raise-only", "violations": violations,
                "recovery": "choose versions strictly higher than the current ones, then re-run."}

    # not-below-the-confirmed-floor: when a proposal is supplied, a target must MEET OR RAISE its
    # confirmed floor — compared against the floor value, not the current version (raise-only already
    # covered current). A target strictly below the floor is refused.
    floor_notes = []
    if proposal:
        # the ENGINE floor: a minor/major bump forced by what changed since the last release (a module added
        # or removed, an interface changed) must be MET, not just be higher than the current version. Without
        # this, a removed-module major floor could be undercut by a patch bump — the "catch a wrong floor"
        # backstop. None when nothing structural fired (a patch is discretionary; raise-only bounds it).
        engine_floor = proposal.get("engine_floor_version")
        if engine_floor and _strictly_greater(engine_floor, engine_ver):
            floor_notes.append(f"engine version {engine_ver} is below the required floor {engine_floor} "
                               f"(the higher of what the merged pull requests declared and what the release "
                               f"diff proved)")
        pf = proposal.get("package_floor", {})
        for mid, floor in pf.items():
            if mid in targets and _strictly_greater(floor, targets[mid]):
                floor_notes.append(f"'{mid}' version {targets[mid]} is below its confirmed floor {floor}")
        if floor_notes:
            return {"applied": False, "reason": "below-confirmed-floor", "violations": floor_notes,
                    "recovery": "raise the engine and any flagged packages to at least their mechanical floor."}

    # stage every touched file, validate ALL before any swap, then swap together (rollback on failure)
    staged: list[tuple[str, str]] = []  # (target_path, temp_path)
    errors: list[str] = []
    try:
        # engine.json — mutate in place so home_repository/identity/order are byte-preserved
        new_engine = dict(engine)
        new_engine["engine_release"] = engine_ver
        pkgs = dict(new_engine.get("packages", {}))
        for mid, ver in changed.items():
            if mid in pkgs:
                pkgs[mid] = ver
        new_engine["packages"] = pkgs
        if min_upgradeable_from is not None:               # record/refresh the clean-upgrade floor when given;
            new_engine["min_upgradeable_from"] = min_upgradeable_from   # else the dict copy carries any prior one
        # Stamp `removed_in` onto the modules THIS cut removes (from the proposal) — the maintainer authored only
        # `description` at removal time; the cut is where the release version is known. Rebuild each entry FRESH
        # (new_engine is a SHALLOW copy of `engine`, so the nested removed_capabilities dict and its entries are
        # shared with the loaded manifest — mutating in place would write through). Only stamp entries that this
        # cut removed and that are not already stamped, so a prior release's removed_in is never overwritten.
        removed_now = set((proposal or {}).get("removed_modules") or [])
        rc = new_engine.get("removed_capabilities")
        if rc and removed_now:
            new_rc = dict(rc)
            for mid in removed_now:
                entry = new_rc.get(mid)
                if isinstance(entry, dict) and not entry.get("removed_in"):
                    new_rc[mid] = {**entry, "removed_in": engine_ver}
            new_engine["removed_capabilities"] = new_rc
        errors += [f"engine.json: {m}" for m in _schema_ok(new_engine, ENGINE_SCHEMA)]

        # each CHANGED module manifest — mutate version only; unchanged capabilities are left untouched
        module_new: dict[str, dict] = {}
        for _rel, man in module_coherence.discover_manifests():
            mid = man.get("id")
            if mid in changed:
                nm = dict(man)
                nm["version"] = changed[mid]
                module_new[_rel] = nm
                errors += [f"{_rel}: {m}" for m in _schema_ok(nm, MODULE_SCHEMA)]

        # split-brain guard: engine.json packages[mid] must equal each module manifest version
        for _rel, nm in module_new.items():
            mid = nm.get("id")
            if new_engine["packages"].get(mid) != nm.get("version"):
                errors.append(f"split-brain: engine.json packages['{mid}']="
                              f"{new_engine['packages'].get(mid)} != {_rel} version={nm.get('version')}")

        if errors:
            return {"applied": False, "reason": "validation", "violations": errors,
                    "recovery": "the computed manifests did not validate; nothing was written."}

        if dry_run:
            return {"applied": False, "reason": "dry-run", "targets": changed, "engine": engine_ver,
                    "from_engine": engine_cur}

        # StarshipSuperjam/engine-template#923: never stage/swap an engine-owned manifest through a shortcut (symlink) or out of the
        # tree. The swap's os.replace defeats only a symlinked LEAF — it still lands out-of-tree
        # through a symlinked ANCESTOR directory — so check every destination BEFORE any temp file is
        # created. Path and base both derive from validate.ROOT (the same source, per the engine_write
        # doctrine), so fixture trees that repoint ROOT stay legitimate.
        unsafe = [reason for path in
                  [module_manager._engine_manifest_path()]
                  + [os.path.join(validate.ROOT, _rel) for _rel in module_new]
                  if (reason := engine_write.write_through_symlink_reason(path, validate.ROOT))]
        if unsafe:
            return {"applied": False, "reason": "unsafe-destination", "violations": unsafe,
                    "recovery": "delete or replace the shortcut(s) named above, then run the cut "
                                "again; nothing was written."}

        # write temps
        def _stage(path, data):
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
                fh.write("\n")
            staged.append((path, tmp))

        _stage(module_manager._engine_manifest_path(), new_engine)
        for _rel, nm in module_new.items():
            _stage(os.path.join(validate.ROOT, _rel), nm)

        # swap together; a write error mid-swap rolls back the files already swapped, so the tree is
        # never left half-written (best-effort atomic — the reviewed-PR merge is the real all-or-
        # nothing unit, and the release-integrity check catches any residual split-brain at merge).
        def _read_bytes(p):
            with open(p, "rb") as fh:
                return fh.read()

        originals = {path: _read_bytes(path) for path, _tmp in staged}
        swapped = []
        try:
            for path, tmp in staged:
                os.replace(tmp, path)
                swapped.append(path)
            staged = []
        except OSError as exc:
            for path in swapped:
                with open(path, "wb") as fh:
                    fh.write(originals[path])
            raise RuntimeError(f"a write error interrupted the cut ({exc}); the files already written were "
                               f"restored, so no versions changed and nothing was left half-written.")
    finally:
        for _path, tmp in staged:  # any un-swapped temp on an error path
            try:
                os.unlink(tmp)
            except OSError:
                pass

    return {"applied": True, "engine": engine_ver, "from_engine": engine_cur, "targets": changed,
            "proposed_floor": (proposal or {}).get("package_floor", {})}


# --------------------------------------------------------------------------- apply (product writer, StarshipSuperjam/engine-template#516)
def apply_product(version: str, dry_run: bool, root: str | None = None, proposal: dict | None = None) -> dict:
    """Record the product version into `product-version.json` — the product analogue of `apply`. A product has
    no engine packages, so there is no per-package/split-brain machinery: one root file, one version.
    Validate the version, enforce RAISE-ONLY against the current product version AND the confirmed floor the
    declared-impact fold derived (`proposal["engine_floor_version"]`, StarshipSuperjam/engine-template#942 — without this a product could
    publish BELOW its declared/proven floor, the version-authority law defeated for downstream), then write
    ATOMICALLY (temp sibling + os.replace, temp cleaned up on ANY error), mirroring `apply`'s staged swap. An
    ABSENT file is a first cut from the construction sentinel (the summary reads 'no earlier version'); the
    common seeded first cut reads its `0.0.0` starting version ('0.0.0 → …'); a present-but-MALFORMED file
    refuses loudly. Returns the same result shape `apply` does (`engine` = the recorded version, `targets` =
    {} for a product) plus a `product` marker, so the workflow shell and the renderers are unchanged."""
    path = _product_version_path(root)
    current = read_product_version(root)
    if current is _PRODUCT_MALFORMED:
        return {"applied": False, "reason": "malformed-product-file",
                "violations": [f"{PRODUCT_VERSION_REL} is present but is not a readable "
                               f"{{\"version\": \"<semver>\"}} object"],
                "recovery": f"fix {PRODUCT_VERSION_REL} to be a JSON object with a version like 0.1.0, then re-run."}
    from_v = current if current is not None else SENTINEL
    if not _valid_version(version):
        return {"applied": False, "reason": "invalid-version",
                "violations": [f"product version '{version}' is not a valid version (expected like 1.2.0)"],
                "recovery": "use dotted-number versions, optionally with a -prerelease suffix (1.2.0, 1.0.0-rc1)."}
    if not _strictly_greater(version, from_v):
        return {"applied": False, "reason": "raise-only",
                "violations": [f"product version {version} is not higher than the current {from_v}"],
                "recovery": "choose a version strictly higher than the current one, then re-run."}
    floor = (proposal or {}).get("engine_floor_version")
    if floor and _strictly_greater(floor, version):
        return {"applied": False, "reason": "below-confirmed-floor",
                "violations": [f"product version {version} is below the required floor {floor} (the higher of "
                               f"what the merged pull requests declared and what the release diff proved)"],
                "recovery": f"raise the product version to at least {floor}, then re-run — a declared or proven "
                            f"floor may be raised, never undercut."}
    if dry_run:
        return {"applied": False, "reason": "dry-run", "engine": version, "from_engine": from_v,
                "targets": {}, "product": True}
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"version": version}, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)   # atomic swap; tmp no longer exists after this
    except OSError as exc:
        raise RuntimeError(f"a write error interrupted the cut ({exc}); {PRODUCT_VERSION_REL} was not changed.")
    finally:
        if os.path.exists(tmp):   # any un-swapped temp on an error path (mirrors apply()'s finally)
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return {"applied": True, "engine": version, "from_engine": from_v, "targets": {}, "product": True}


# --------------------------------------------------------------------------- rendering
def _render_proposal(p: dict) -> str:
    lines = ["Release proposal", "================", "", p["baseline_note"], ""]
    lines.append("What changed since the last release:")
    for c in p["change_inventory"]:
        lines.append(f"  - {c}")
    if p["impacts"]:
        lines.append("")
        lines.append("Contract / interface changes (read before confirming):")
        for im in p["impacts"]:
            lines.append(f"  - {im['what']}: {im['why']}")
            lines.append(f"    behavioral check: {im['behavioral_demo']}")
    lines.append("")
    if p["mode"] == "first-cut":
        lines.append("This is the first cut — choose the initial version explicitly, e.g.:")
        lines.append("  release_cut.py apply --engine <ver> --all <ver>")
    else:
        floor = p["engine_floor_level"]
        declared = p.get("declared_impact")
        refusal = p.get("impact_refusal")
        if refusal:
            # A fold refusal (mismatch, legacy/undeclared, or an unreadable body) — WITHHOLD the version rather
            # than print a floor line that contradicts it. The full reason + recovery print as the refusal below.
            lines.append(f"Version decision (current {p['current_engine']}): WITHHELD — this release is refused.")
            if declared is not None:
                lines.append(f"  - highest declared pull-request impact: {declared}")
            if floor and floor != "none":
                lines.append(f"  - mechanical compatibility floor the diff could prove: {floor}")
            lines.append(f"  - {refusal['reason']}")
            lines.append("  - no version is derived until this is resolved; the refusal reason and how to fix it "
                         "are printed with the 'Refused' details.")
        elif declared is not None:                       # the declared-impact fold ran and did NOT refuse (StarshipSuperjam/engine-template#942)
            lines.append(f"Version decision (current {p['current_engine']}):")
            lines.append(f"  - highest declared pull-request impact: {declared}")
            lines.append(f"  - mechanical compatibility floor the diff could prove: "
                         f"{floor if floor != 'none' else 'none detected'}")
            lines.append(f"  - effective (the higher of the two): {p.get('effective_impact')}")
            if p.get("engine_floor_version"):
                lines.append(f"  - so the least this release can be is {p['engine_floor_version']} "
                             f"(you may raise it, never lower it).")
            else:
                lines.append("  - every merged pull request declared 'none' and the diff proved no floor — no "
                             "automatic version; name one explicitly to publish.")
            for d in p.get("impact_defaulted") or []:
                lines.append(f"  - defaulted: {d}")
        elif floor == "none":
            lines.append(f"No mechanical compatibility floor was proven (current {p['current_engine']}). The "
                         f"version follows what the merged pull requests declared — a behaviour change with no "
                         f"structural signal carries its impact there. You can never lower it.")
        else:
            lines.append(f"Mechanical compatibility floor: at least a {floor} bump "
                         f"(current {p['current_engine']}). You may raise it, never lower it.")
        if p.get("compatibility_unknown"):
            lines.append(f"  ! {len(p['compatibility_unknown'])} contract/interface change(s) have UNKNOWN "
                         f"compatibility — review required (the declared impact governs; see the Risk section).")
        if p.get("package_attribution_note"):
            lines.append(f"  ! {p['package_attribution_note']}")
        if p["package_floor"]:
            lines.append("Per-package floors (raise-only — each from that package's own mechanical floor "
                         "(migration/retirement) and/or the declared impact of the pull requests that touched it):")
            attributions = p.get("package_impact_attributions") or {}
            for mid, ver in p["package_floor"].items():
                lines.append(f"  - {mid}: at least {ver}")
                if mid in attributions:                    # printed under its OWN package, never a trailing block
                    lines.append(f"    from declared impact: {attributions[mid]}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- change summary (one renderer)
def change_summary(proposal: dict) -> list:
    """The plain-language "what changed since the last release" list that JUSTIFIES the version — the
    single derived view rendered into BOTH the release pull-request body and the published GitHub Release
    notes. One renderer over one proposal, never a second history store (eADR-0014): history routes to the
    pull-request body and, as a derived view of the same signals, the Release notes.

    It merges the structural change inventory (a capability added or removed, a new migration) with a
    one-line surface note for each CHANGED contract/interface — because a contract-only release carries no
    structural inventory line, so without the impact surfaces the "what changed" list would read empty even
    though a changed contract is exactly what forced the bump. The detail on each impact (why it may be
    breaking) is rendered separately in the pull-request Risk section; this is the summary line."""
    lines = list(proposal.get("change_inventory") or [])
    for im in proposal.get("impacts") or []:
        what = im.get("what")
        if what:                       # e.g. "the contract surface 'X' changed" -> "The contract surface 'X' changed."
            lines.append(_cap(what) + ".")
    return lines


def _cap(text: str) -> str:
    """Capitalize the first letter of a plain-language fragment (the impact `what` strings are lower-case)."""
    text = (text or "").strip()
    return (text[0].upper() + text[1:]) if text else text


def _structural_signals(proposal: dict) -> list:
    """The capability + data signals from the change inventory — 'Added the X capability', 'Removed the X
    capability', and (consent-critical for an upgrader) ''X' gained a data/config migration' — with the
    no-signal caveat and the first-release framing excluded. These answer 'what does upgrading DO to me?', a
    question the flat merged-PR list does not, so they are surfaced BESIDE the pull-request list, not replaced
    by it. A new migration in particular has no other callout (a removed capability rides the breaking
    warning, a changed contract rides the interface section)."""
    return [c for c in (proposal.get("change_inventory") or [])
            if c != _NO_STRUCTURAL_SIGNAL_NOTE and not c.startswith("First release:")]


def _version_decision_lines(proposal: dict, heading: str = "## Version decision", detailed: bool = False) -> list:
    """The 'why this version' record (StarshipSuperjam/engine-template#942 §12). `detailed=False` (the CONCISE form) is a few lines —
    effective / highest declared / mechanical floor — for the PUBLISHED release notes (§12: keep the notes
    readable, don't dump classifier detail there). `detailed=True` adds the per-pull-request declared-impact
    snapshot AND the exempt-author defaults, for the release PULL REQUEST the maintainer reviews (§12: retain
    detailed evidence in the release PR; and the exempt-bot default must be DISCLOSED where the maintainer sees
    it, not only in the transient CLI preview). Empty when the fold did not run (a first cut, or the offline
    path)."""
    declared = proposal.get("declared_impact")
    if declared is None:
        return []
    floor = proposal.get("mechanical_floor_level") or proposal.get("engine_floor_level") or "none"
    out = ["", heading, "",
           f"- Effective release impact: **{proposal.get('effective_impact')}**",
           f"- Highest declared pull-request impact: {declared}",
           f"- Mechanical compatibility floor the diff proved: {floor if floor != 'none' else 'none detected'}"]
    unknown = proposal.get("compatibility_unknown") or []
    if unknown:
        out.append(f"- {len(unknown)} contract/interface change(s) had unknown compatibility (review-required); "
                   f"the declared impact governed.")
    if detailed:
        per_pr = proposal.get("declared_per_pr") or []
        if per_pr:
            out += ["", "Declared impact per merged pull request (a snapshot, so it survives later body edits):"]
            out += [f"  - #{pr['number']} {pr['title']}: "
                    f"{pr['impact'] if pr['impact'] else 'no marker (undeclared)'}" for pr in per_pr]
        defaulted = proposal.get("impact_defaulted") or []
        if defaulted:
            out += ["", "Automated pull requests folded to a conservative default (disclosed, not hidden):"]
            out += [f"  - {d}" for d in defaulted]
    return out


def render_release_notes(tag: str, proposal: dict | None = None, gate_state: str = "sub-bar") -> str:
    """The published GitHub Release body — a human-readable, self-contained account of the release: the
    version, the readiness line, a breaking-change callout when the release is breaking, a "What changed"
    section (the pull requests merged since the last release, or the structural signals when that list is
    unavailable), and an "Interface changes to read" section carrying each changed contract/interface WITH
    its plain-language description. It is a derived VIEW of the same signals the
    release pull-request body renders (one source — the proposal recomputed at publish — never a second
    history store, eADR-0014); it does not restate the version-by-version manifest table (that is the pull
    request's job), it tells a reader of the published release what changed and why it matters. A None/empty
    proposal (the best-effort fallback when the publish-time recompute could not run) degrades to the version
    + readiness line alone. Maintainer register: 'engine version vX.Y.Z', no internal vocabulary."""
    product = bool((proposal or {}).get("product"))
    out = [f"Release {tag}." if product else f"Engine version {tag}.", "", _gate_path_line(gate_state, product)]
    proposal = proposal or {}
    if proposal.get("engine_floor_level") == "major":
        out += ["", "⚠️ **This release makes a breaking change.** Something an earlier version provided was "
                    "removed, or changed in a way that is not backward-compatible — so anything that relied on "
                    "it will need attention. See the changes below."]
    # The durable version-decision record (why this version was chosen), snapshotting the per-PR declared impact
    # so it survives later edits to the source pull-request bodies (StarshipSuperjam/engine-template#942).
    out += _version_decision_lines(proposal)
    # "What changed" leads with the pull requests merged since the last release — the actual body of work —
    # when the list is available; otherwise it falls back to the structural signals (a first release, or a
    # best-effort failure to reach the pull-request list). Either way, the capability + data signals are
    # surfaced BESIDE the list (a flat PR title does not answer 'does this migrate my data?'), and the
    # interface-change detail follows.
    merged = proposal.get("merged_prs") or []
    inventory = proposal.get("change_inventory") or []
    if merged:
        n = len(merged)
        out += ["", f"## What changed since the last release ({n} pull request{'' if n == 1 else 's'})", ""]
        out += _render_pr_groups(merged, lambda k: f"### {k}")
        signals = _structural_signals(proposal)
        if signals:
            out += ["", "## Capability and data changes", ""]
            out += [f"- {c}" for c in signals]
    elif inventory:
        # "since the last release" would contradict a first release (there is no last release); title it plainly.
        heading = "What this release establishes" if proposal.get("mode") == "first-cut" \
            else "What changed since the last release"
        out += ["", f"## {heading}", ""]
        out += [f"- {c}" for c in inventory]
    impacts = proposal.get("impacts") or []
    if impacts:
        out += ["", "## Interface changes to read", ""]
        for im in impacts:
            what = _cap(im.get("what")) or "A contract surface changed"
            why = _cap(im.get("why"))          # its own sentence after the bold heading — capitalized, not a run-on
            out.append(f"- **{what}.**" + (f" {why}" if why else ""))
    return "\n".join(out)


# --------------------------------------------------------------------------- release-PR body (legibility)
def _gate_path_line(state: str, product: bool = False) -> str:
    """The legible gate-path line: the three release-readiness states must read as VISIBLY DISTINCT, never
    alike. Only `sub-bar` is reachable — no acceptance-benchmark instrument measures a release — but
    `passed`/`errored` are rendered here structurally so a benchmark reads legibly
    rather than as a retrofit (the standing legibility invariant, not a one-of-three accident). `product` swaps
    the subject to 'this release' for a deployed repo's product cut (the sub-bar text is already neutral)."""
    subject = "this release" if product else "the engine"
    if state == "passed":
        return (f"**Release readiness — passed.** {_cap(subject)} was exercised against its readiness check and "
                "met the bar for this release.")
    if state == "errored":
        return ("**Release readiness — could not be checked (it errored).** The readiness check did not run to "
                "completion, so readiness is unproven — treat this release as unverified until it runs clean.")
    return ("**Release readiness — no automated check ran (this is on purpose).** There is no automated "
            "readiness check built yet, so this release was not measured against one. It rests on the summary "
            "below and your own read — not a machine check. This is a deliberate, recorded choice, not a "
            "passed check.")


def _version_lines(applied: dict) -> list:
    """Plain-language 'what versions this sets' — collapsed to one line when every capability moves to the
    engine's own new version (the uniform first-cut case), else itemised so a per-capability difference shows."""
    engine = applied.get("engine")
    from_engine = applied.get("from_engine")
    targets = applied.get("targets") or {}
    # the first cut moves from the construction sentinel `0.0.0-dev`, which is internal and means nothing to the
    # maintainer — say "no earlier version" instead of surfacing it.
    from_shown = "no earlier version" if from_engine == SENTINEL else from_engine
    # PRODUCT cut: one product version, no per-capability lines (a product has no engine packages; targets={}).
    label = "Product" if applied.get("product") else "Engine"
    lines = [f"- {label}: {from_shown} → {engine}"]
    if targets and all(v == engine for v in targets.values()):
        lines.append(f"- Every capability ({len(targets)}): → {engine}")
    else:
        for mid in sorted(targets):
            lines.append(f"- {mid}: → {targets[mid]}")
    return lines


def pr_section(header: str, summary: str, body_lines: list, impact: str) -> list:
    """One pull-request-body section in the repo template's shape — a **bold one-line summary**, its bullets,
    then the italic `*Impact:*` line — so the release body matches the form every engine pull request's body
    uses, not merely the required headers (a header-only body clears the completeness gate but is not a
    template-conforming body). PUBLIC: also consumed by module_manager's upgrade-PR-body author, so the
    engine's update pull request reads in the same template shape — the name is public because the dependency
    crosses a module boundary (an underscore would hide that a second module relies on it)."""
    return [f"## {header}", "", f"**{summary}**", "", *body_lines, "", f"*Impact: {impact}*", ""]


def template_preamble() -> str:
    """The consent-preamble blockquote lifted VERBATIM from the repo pull-request template, so the release
    body carries the same standing note on how to read the checks that every other pull request carries —
    one source, no second copy to drift, and always the preamble the pull-request-completeness gate requires.
    It is the leading `>` blockquote that sits above the first `## ` heading in the template. PUBLIC for the
    same cross-module reason as pr_section — the upgrade-PR-body author reuses it rather than keeping a second
    preamble copy that could drift from the template's anchor phrases."""
    path = os.path.join(validate.ROOT, ".github", "pull_request_template.md")
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    block: list = []
    for line in lines:
        if line.startswith("## "):
            break
        if line.startswith(">"):
            block.append(line)
        elif block:                    # the blockquote ended before the first heading
            break
    if not block:
        raise RuntimeError("the pull-request template carries no consent-preamble blockquote to lift into "
                           "the release body (.github/pull_request_template.md).")
    return "\n".join(block)


def _working_tree_sha() -> "str | None":
    """The git tree sha of the current working tree (through a THROWAWAY index, never the real one) — used to
    confirm a supplied deployment-gate result describes THIS release candidate. At the `release.yml` pr-body
    step the tree is unchanged since the gate ran two steps earlier, so this matches the gate's stamped
    `candidate_tree`; a mismatch means a stale or foreign gate JSON. Best-effort: None on any git failure."""
    import subprocess   # local: only this correspondence check needs it (mirrors the file's other local uses)
    try:
        with tempfile.TemporaryDirectory() as idx:
            env = {**os.environ, "GIT_INDEX_FILE": os.path.join(idx, "index")}
            if subprocess.run(["git", "-C", validate.ROOT, "add", "-A"], env=env,
                              capture_output=True, timeout=120).returncode != 0:
                return None
            r = subprocess.run(["git", "-C", validate.ROOT, "write-tree"], env=env,
                               capture_output=True, text=True, timeout=60)
        return (r.stdout.strip() or None) if r.returncode == 0 else None
    except Exception:   # noqa: BLE001 — correspondence is advisory, never a block
        return None


def _deployment_check_lines(gate: "dict | None") -> list:
    """The Validation-section bullets recording the deployed upgrade+rollback check (the `release_gate` result).
    ENGINE cuts only — the caller suppresses this in product mode, where the gate is inert. STRUCTURED FIELDS
    ONLY reach the body: the baseline tag and per-leg outcome, never a raw `detail` string (those are
    unsanitized nested stderr — local paths, tracebacks, `::`-prefixed text a public body must not carry). It
    ALWAYS emits something on an engine cut: a missing / unreadable / mismatched / incomplete gate result
    renders an honest line rather than silently restoring a body that looks like no check exists. The wording is
    strictly mechanical — a deploy-and-undo CHECK, never a 'qualification' (the engine never qualifies itself;
    that word names the operator's own frozen judgment)."""
    lead = "- **Deployed upgrade and rollback check** —"
    if gate is None:
        return [f"{lead} no deployed upgrade/rollback evidence was supplied with this summary."]
    if gate.get("_unreadable"):
        return [f"{lead} deployed upgrade/rollback evidence was supplied but could not be read "
                f"({gate.get('_error') or 'unreadable'})."]
    if not gate.get("ran"):
        return [f"{lead} the deployment gate was inert here (not the engine's home repo), so it recorded no "
                "transitions."]
    up = gate.get("upgrades") or {}
    transitions = up.get("transitions")
    if not transitions:
        return [f"{lead} the deployment gate did not complete, so it recorded no per-transition evidence."]
    stamped, current = gate.get("candidate_tree"), _working_tree_sha()
    if stamped and current and stamped != current:
        return [f"{lead} the gate evidence supplied does not correspond to this release candidate, so it is "
                "not shown (re-run the deployment gate against this tree)."]
    unverified = "" if (stamped and current) else " (candidate correspondence could not be verified)"
    lines = [f"{lead} on a projected deployed copy, from each supported source version, a practice upgrade to "
             f"this release then an undo of it{unverified}:"]
    for t in transitions:
        base, up_ok = t.get("baseline"), (t.get("upgrade") or {}).get("passed")
        rb_ok = (t.get("rollback") or {}).get("passed")
        if up_ok and rb_ok:
            lines.append(f"  - from {base}: practice upgrade completed, then the undo restored the copy.")
        elif up_ok and rb_ok is False:
            lines.append(f"  - from {base}: practice upgrade completed, but the undo did not cleanly restore "
                         "the copy.")
        elif up_ok is False:
            lines.append(f"  - from {base}: the practice upgrade did not complete.")
        else:
            lines.append(f"  - from {base}: recorded an unexpected state.")
    floor, n = up.get("floor"), len(transitions)
    excl = up.get("excluded") or []
    excl_note = f"; below the floor and not tested: {', '.join(excl)}" if excl else ""
    if floor:
        lines.append(f"  - Supported source versions: every released version at or above the clean-upgrade "
                     f"floor {floor} ({n} transition{'' if n == 1 else 's'} this cut{excl_note}).")
    lines.append("  - This is a mechanical deploy-and-undo check on a projected deployed copy, not the "
                 "readiness judgment referred to under Risk. It proves a stalled/staged update from each "
                 "version above can be undone; it does not exercise reverting an already-merged upgrade "
                 "pull request.")
    return lines


def render_pr_body(proposal: dict, applied: dict, gate_state: str = "sub-bar",
                   deployment_gate: "dict | None" = None) -> str:
    """The release pull request's body — the maintainer's whole evidence bundle, authored HERE (never
    composed in workflow bash) so the gate-path legibility has one home. It takes both the `propose` JSON
    (the change inventory + interface impacts) and the `apply` result JSON (the versions actually recorded),
    and closes with the confirm/raise/reject guidance that makes the PR review the consent act: the merge
    is the go-ahead, and a wrong or missing signal is caught by closing and re-running with the right version.
    Maintainer-facing register: one engine version moving vX→vY — no 'release-cut'/'bump'/'version
    production' vocabulary. Every section follows the repo pull-request template's form (bold summary →
    bullets → `*Impact:*`), not just its headers — a real template-conforming body, whose section names also
    clear the pull-request-completeness gate."""
    engine = applied.get("engine")
    from_engine = applied.get("from_engine")
    # this body IS the maintainer's consent surface, so it must never author a "None → None" release: a refused
    # or malformed apply result carries no versions and cannot be rendered as a release.
    if not engine:
        raise RuntimeError("cannot render a release summary: the apply result recorded no engine version "
                           "(the release was refused or the result is malformed).")
    # the construction sentinel `0.0.0-dev` is internal — never surface it to the maintainer (see _version_lines)
    from_shown = "no earlier version" if from_engine == SENTINEL else from_engine
    # PRODUCT cut (StarshipSuperjam/engine-template#516): a deployed repo cutting its OWN product release — speak of the product, not the engine.
    product = bool(applied.get("product") or proposal.get("product"))
    thing = "product" if product else "engine"

    # The consent preamble every pull request carries at the top — lifted from the template so the release
    # body reads the same and satisfies the pull-request-completeness gate's preamble anchors (StarshipSuperjam/engine-template#589). Emitted in
    # BOTH modes, so a product release PR clears the same gate an engine one does.
    out = [f"# A new {'release of your product' if product else 'engine version'}: "
           f"{from_shown} → {engine}", "", template_preamble(), ""]

    out += pr_section(
        "Purpose",
        f"This records a new version of your {thing} — {from_shown} → {engine} — for you to review and publish.",
        [f"- Merging this is your go-ahead to release {engine}; closing it releases nothing and changes none of "
         "your own settings or content.",
         "- A release only ever moves the version up, never down."],
        (f"merging publishes {engine} as a release of your product; nothing is published until then." if product
         else f"merging publishes {engine} for your instances to upgrade to; nothing is published until then."))

    # Scope — the versions recorded + the change inventory that set them (the itemised version lines and the
    # least-version floor line stay verbatim; they are what a reviewer checks the release against).
    scope = ["The versions this release sets:", *_version_lines(applied)]
    floor_v = proposal.get("engine_floor_version")
    if floor_v:
        scope.append(f"- The least this release could be is **{floor_v}** — the higher of what the merged pull "
                     f"requests declared and what the release diff could prove; a higher version is fine, a "
                     f"lower one is not.")
    # The DETAILED version-decision evidence lands HERE, in the release pull request the maintainer reviews
    # (StarshipSuperjam/engine-template#942 §12: detailed evidence in the release PR, concise rationale in the published notes) — the
    # declared/mechanical/effective breakdown, the per-PR snapshot, and the exempt-bot defaults, so the
    # maintainer sees WHY the version was chosen (and that a bot PR was folded to a default) at the consent moment.
    scope += _version_decision_lines(proposal, heading="Version decision (why this version):", detailed=True)
    # "What changed" leads with the pull requests merged since the last release (the actual work) when the
    # list is available; otherwise the structural floor-signal summary. The capability + data signals are
    # surfaced beside the list (the migration signal has no other home); the interface detail is under Risk.
    merged = proposal.get("merged_prs") or []
    if merged:
        n = len(merged)
        header = f"What changed since the last release ({n} pull request{'' if n == 1 else 's'}):"
        # Same kind-grouping as the published Release notes, but rendered as BOLD LABELS, not `###` headings:
        # this block sits inside the one `## Scope` section, whose peers ("Capability and data changes:") are
        # plain-text labels — a heading here would out-rank them and invert the outline — and bold labels render
        # cleanly inside the <details> block below where headings need careful blank-line handling.
        pr_lines = _render_pr_groups(merged, lambda k: f"**{k}**")
        # a long list is wrapped in a foldable <details> so the reader CAN collapse it (it otherwise pushes the
        # Review guidance far down the consent surface) — but rendered OPEN by default, so the work is visible on
        # load, not hidden behind a click.
        if n > 15:
            scope += ["", header, "", "<details open><summary>Merged pull requests</summary>", "", *pr_lines,
                      "", "</details>"]
        else:
            scope += ["", header, "", *pr_lines]
        signals = _structural_signals(proposal)
        if signals:
            scope += ["", "Capability and data changes:"]
            scope += [f"- {c}" for c in signals]
    else:
        heading = "What this release establishes" if proposal.get("mode") == "first-cut" \
            else "What changed since the last release"
        scope += ["", f"{heading}:"]
        scope += [f"- {c}" for c in change_summary(proposal)]
    out += pr_section(
        "Scope",
        ("The product version this records, and the changes that set it." if product
         else "The engine and capability versions this records, and the changes that set them."),
        scope,
        ("this is the exact version written into product-version.json." if product
         else "these are the exact versions written into the manifests and the maps that mirror them."))

    out += pr_section(
        "Out of scope",
        "What merging does not do.",
        [f"- It does not change how your {thing} behaves beyond the version stamp.",
         "- It does not migrate any of your data.",
         "- It does not touch your own settings or content."],
        ("the only thing this pull request changes is the recorded product version." if product
         else "the only thing this pull request changes is the recorded version and the generated maps that "
              "mirror it."))

    # Risk — the gate-path line is the (already bold-led) section summary; the breaking-change warning and
    # the interface-impact list are its bullets, so a reviewer scanning "Risk" sees the weight here, not only
    # as a neutral line up in Scope.
    risk = []
    if proposal.get("engine_floor_level") == "major":
        risk.append("- **This release makes a breaking change.** Something an earlier version provided was "
                    "removed, or changed in a way that is not backward-compatible — so anything that relied on "
                    "it will need attention. What changed is listed under Scope above.")
    impacts = proposal.get("impacts") or []
    if impacts:
        if risk:             # a breaking-change bullet precedes this intro — a blank line keeps the intro from
            risk.append("")  # being absorbed into that bullet as a lazy markdown continuation (the two would
                             # otherwise fuse, hiding the interface-changes signpost on the highest-stakes release).
        risk.append("Interface changes to read before you merge:")
        if proposal.get("compatibility_unknown"):
            risk.append(f"- **{len(proposal['compatibility_unknown'])} of these have UNKNOWN compatibility** "
                        f"(a rename/relocation or an in-place change) — the diff set no version floor for them, "
                        f"so the declared release impact governs and your review is the backstop. Read each "
                        f"against its consumers before merging.")
        # Same polished rendering as the published Release notes — a bold heading, then the description as its
        # own sentence — so the consent surface the maintainer reads FIRST is no rougher than the Release body.
        risk += [f"- **{_cap(im.get('what')) or 'A contract surface changed'}.**"
                 + (f" {_cap(im.get('why'))}" if im.get("why") else "") for im in impacts]
    elif product:
        risk.append("- The summary can only show what it detects mechanically — the list of merged pull "
                    "requests above. Your own knowledge of what you shipped is the backstop (see Review).")
    else:
        risk.append("- No changes to interface contract files were detected — this does not cover a removed "
                    "capability or a data migration, which would be listed under Scope. The summary can only "
                    "show changes it detects mechanically, so your own knowledge of what you shipped is the "
                    "backstop (see Review).")
    out += ["## Risk", "", _gate_path_line(gate_state), "", *risk, "",
            "*Impact: a wrong version, or a change the summary could not detect mechanically, is caught by "
            "closing and re-running with the right version — nothing publishes until you merge.*", ""]

    validation_bullets = [
        ("- A green check shows the recorded version is well-formed and this summary is complete." if product else
         "- A green check shows the versions agree across all the files that record them, the generated maps "
         "are in sync, and this summary is complete."),
        f"- It does **not** judge whether {engine} is the right version to release — that judgment is yours."]
    if not product:      # the deployment gate is an ENGINE-cut instrument; it is inert on a product cut
        validation_bullets += _deployment_check_lines(deployment_gate)
    # The shipped local-reference floor's DISCLOSED-not-silent note (StarshipSuperjam/engine-template#943): present only when no local-reference
    # vocabulary was declared at the cut, so the scan did not run. Surfaced in the maintainer's evidence bundle
    # here — not only the propose step's log — so a removed/emptied declaration is visible at merge, never silent.
    lref_note = proposal.get("local_reference_note")
    if lref_note:
        validation_bullets.append(f"- ⚠ {lref_note}")
    out += pr_section(
        "Validation",
        "The engine's own tooling produced this and `engine-ci` checks it — the mechanical floor.",
        validation_bullets,
        f"green means the release conforms to the engine's rules, not that {engine} is the right call.")

    out += pr_section(
        "Review",
        "How to act on this — go ahead, raise the version, or stop.",
        [f"- **Go ahead** — if the summary above matches what you built, merge this; that merge is your consent "
         f"to release {engine}.",
         "- **Want a higher version** — close this and run the release again with a higher version number (a "
         "release can only ever go up, never down).",
         "- **Something's missing** — if you know you changed something that is not listed above" +
         ("" if product else " (for example you removed a capability but do not see it here)") +
         ", close this and run the release again with the "
         "version you know it should be; the summary shows only what it can detect mechanically, so your own "
         "knowledge of what you shipped is the backstop."],
        f"your merge is the binding consent to publish {engine} — the engine never merges this for you.")

    out += pr_section(
        "Demonstration",
        "Nothing to run — this is release plumbing, not a behaviour change.",
        ["- This pull request only records the new version and refreshes the generated maps; there is no "
         "product behaviour to watch work here. What you review is the version summary above.",
         "- There is no operator-runnable demonstration, and saying so is honest: a release cut has no "
         "behavioural correlate of its own — not a missing one to apologise for."],
        "there is no behaviour to demonstrate; the version summary above is what you review.")

    out += pr_section(
        "Files of interest",
        ("Where to look — the recorded product version." if product
         else "Where to look — the recorded versions and the maps that mirror them."),
        (["- `product-version.json` — the recorded version of your product."] if product else
         ["- `.engine/engine.json` and each installed capability's `.engine/modules/<id>/manifest.json` — the "
          "recorded versions.",
          "- `.engine/knowledge/graph.json` and `.engine/self-map.md` — the generated maps, refreshed to match."]),
        "these are the only files this pull request changes.")

    out += pr_section(
        "AI involvement",
        "The engine's release workflow prepared this; the version choice and the decision to publish are yours.",
        [("- It computed the version, recorded it into product-version.json, and opened this for your review."
          if product else
          "- It computed the version, recorded it into the manifests, regenerated the derived maps, and opened "
          "this for your review."),
         "- The version follows the engine's release process; nothing is published until you merge."],
        f"the mechanical steps are the engine's; the decision to publish {engine} is yours.")

    out += ["_Closing this pull request leaves behind the `release/…` branch it was opened from. That branch is "
            "not a release — nothing is released until you merge — and it is safe to delete._"]
    # The release pull request declares its OWN release impact as `none` (StarshipSuperjam/engine-template#942): a version-recording cut
    # changes no public behaviour, so it carries a valid marker and passes the release-impact check like any pull
    # request — and it is dropped from the merged-PR fold anyway (_parse_pr_lines strips the release PR).
    out += ["", release_impact.impact_trailer("none")]
    return "\n".join(out)


# --------------------------------------------------------------------------- CLI
def _current_sha() -> "str | None":
    """The commit being released — the workflow's `GITHUB_SHA`, else the local `git rev-parse HEAD`. Used as
    the generate-notes target; a sha not on GitHub simply yields no pull-request list (best-effort)."""
    sha = os.environ.get("GITHUB_SHA")
    if sha:
        return sha.strip()
    try:
        import subprocess
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except Exception:  # noqa: BLE001 — no sha available -> no pull-request list, never a failure
        return None


def _apply_impact_fold(proposal: dict, previous_tag: str | None, target: str, slug: str | None,
                       mechanical_level: str, legacy_impact: str | None, *, fold_packages: bool = False):
    """Run the declared-impact fold (fetch each merged pull request's marker, FAIL-CLOSED) and fold it into
    `proposal`: set declared_impact / mechanical_floor_level / effective_impact / impact_defaulted /
    declared_per_pr and, on success, engine_floor_version = the effective floor. Returns a refusal dict (an
    unreadable body, a legacy/undeclared pull request, or a declaration below the proven floor) or None. The ONE
    fold entry both the engine and product cut paths share, so their posture cannot drift (StarshipSuperjam/engine-template#942).

    `fold_packages` (engine cut only) additionally attributes each pull request's declared impact to the PACKAGE(s)
    it touched, raising `proposal['package_floor']` per package (StarshipSuperjam/engine-template#942 L10). Product cuts pass False — a deployed
    product has no engine module tree to attribute against."""
    imp = merged_pr_impacts(previous_tag, target, repo=slug, want_files=fold_packages)
    if imp["error"]:
        fetch_refusal = {
            "reason": "the release could not read the declared impact of every merged pull request, so it "
                      "refuses to auto-derive a version it cannot stand behind (a skipped body could hide a "
                      "breaking change)",
            "violations": [imp["error"]],
            "recovery": "This is a fail-closed guard for a version-authority read — a network/token failure, "
                        "not your change. Re-run the release; if it persists, check the release job's "
                        "GITHUB_TOKEN and GitHub availability."}
        proposal["impact_refusal"] = fetch_refusal          # so the renderer withholds the version, not contradicts
        return fetch_refusal
    res = resolve_release_impact(mechanical_level, proposal["current_engine"], imp["per_pr"], legacy_impact)
    proposal["declared_impact"] = res["declared"]
    proposal["mechanical_floor_level"] = mechanical_level
    proposal["effective_impact"] = res["effective"]
    proposal["impact_defaulted"] = res["defaulted"]
    proposal["declared_per_pr"] = [{"number": pr["number"], "title": pr["title"], "impact": pr["impact"]}
                                   for pr in imp["per_pr"]]
    if res["refusal"]:
        # Stamp the refusal onto the proposal so `_render_proposal` WITHHOLDS the version decision honestly instead
        # of inferring "nothing declared" from a falsy engine_floor_version (a QA finding: the renderer printed
        # self-contradictory text in exactly the mismatch/legacy cases the feature exists to make legible).
        proposal["impact_refusal"] = res["refusal"]
        return res["refusal"]
    proposal["engine_floor_version"] = res["engine_floor_version"]
    if fold_packages:
        # L10: raise each PACKAGE only for the pull requests that touched IT. `surfaces` maps a changed path to its
        # owning module; an empty registry (unreadable catalog) can attribute nothing, so DISCLOSE it rather than
        # silently under-bump packages — the engine version (folded fail-closed above) is unaffected, and package
        # floors are raise-only + guarded by the untouched package validity checks, so this is a note, not a refusal.
        surfaces = module_surfaces.load()
        present_versions = {mid: man.get("version", "0.0.0") for mid, man in _present_modules().items()}
        folded = fold_package_impacts(proposal.get("package_floor") or {}, present_versions, imp["per_pr"], surfaces)
        proposal["package_floor"] = folded["package_floor"]
        proposal["package_impact_attributions"] = folded["attributions"]
        if not surfaces:
            proposal["package_attribution_note"] = (
                "per-package impact attribution was SKIPPED — the module-surfaces registry "
                "(.engine/provisioning/module-surfaces.json) is empty or unreadable, so each package kept only its "
                "mechanical floor. The engine version still reflects the declared impact.")
    return None


def _cmd_propose(args) -> int:
    mode, ctx = release_mode()
    if mode == "refuse":
        print(f"RELEASE-CUT ERROR: your product's version file ({PRODUCT_VERSION_REL}) could not be read — it "
              f'must be a small JSON file with a version, like {{"version": "0.1.0"}}. Fix it, then run the '
              f"release again. Nothing was changed.", file=sys.stderr)
        return 2
    if mode == "product":
        # PRODUCT cut (StarshipSuperjam/engine-template#516): baseline is the DEPLOYED repo's own last release; no capability tree to diff.
        # A None slug (unresolved origin) forces a first cut — never the engine-home fallback (see _product_baseline).
        baseline = _product_baseline(ctx["slug"])
        merged = ([] if args.baseline_tree
                  else merged_pr_titles(baseline.ref, _current_sha(), repo=ctx["slug"]))
        proposal = _product_proposal(baseline, ctx["current"] or "0.0.0", merged)
        # A product has no capability tree to diff, so there is NO mechanical floor — the version follows the
        # declared pull-request impact alone (absence of a structural floor means "no floor", never "patch": an
        # all-none tranche derives no version and the workflow requires an explicit one, StarshipSuperjam/engine-template#942). Skipped on a
        # first cut (the version is chosen) and the offline path. A refusal reaches a NON-ZERO exit (F6).
        impact_refusal = (None if args.baseline_tree or baseline.first_cut else
                          _apply_impact_fold(proposal, baseline.ref, _current_sha(), ctx["slug"],
                                             "none", getattr(args, "legacy_impact", None)))
        print(json.dumps(proposal, indent=2) if args.json else _render_proposal(proposal))
        if impact_refusal:
            _print_refusal(impact_refusal)
            return 2
        return 0
    baseline = resolve_baseline()
    tree, cleanup = _baseline_tree_for(baseline, args.baseline_tree)
    try:
        proposal = classify(baseline, tree)
    finally:
        if cleanup:
            shutil.rmtree(cleanup, ignore_errors=True)
    # The pull requests merged since the last release — the body of work beside the floor signals. Skipped when
    # a baseline tree is injected (the tests' / `--baseline-tree` offline path), best-effort otherwise.
    proposal["merged_prs"] = ([] if args.baseline_tree
                              else merged_pr_titles(baseline.ref, _current_sha()))
    # The declared-impact fold (StarshipSuperjam/engine-template#942): fold the merged pull requests' declared impact, raise it by the
    # PROVEN mechanical floor, and REWRITE engine_floor_version to the effective floor so apply's raise-only
    # enforces "not below what was declared". FAIL-CLOSED (see _apply_impact_fold). Skipped on the offline path.
    impact_refusal = (None if args.baseline_tree else
                      _apply_impact_fold(proposal, baseline.ref, _current_sha(), None,
                                         proposal["engine_floor_level"], getattr(args, "legacy_impact", None),
                                         fold_packages=True))
    print(json.dumps(proposal, indent=2) if args.json else _render_proposal(proposal))
    # DISCLOSED-not-silent: if no local-reference vocabulary is declared, the shipped local-reference floor did
    # not run. Never a refusal (an absent declaration is a legitimate steady state), but never silent either at an
    # engine cut, so a removed/emptied declaration is visible (StarshipSuperjam/engine-template#943).
    if proposal.get("local_reference_note"):
        print(proposal["local_reference_note"], file=sys.stderr)
    # A dropped migration key, a dropped retired-capability notice, a whole-module removal with no plain-language
    # notice, a survivor that still depends on a removed module, or a default-on module depending on a capability
    # not guaranteed present everywhere — each would break a deployer's upgrade (the first three silently, the last
    # two as a dead-end / an uninstallable default). REFUSE the cut here, before `apply` writes anything. All are
    # reported together in ONE refusal so a maintainer fixing one isn't ambushed by another on a re-run (design-
    # review). `propose` runs under `set -euo pipefail` in release.yml, so this non-zero exit fails the release
    # job at this step; apply and pr-body never run, so there is no PR body to carry the fact — the refusal
    # message is the whole surface. The recovery differs by kind.
    mig_viol = proposal.get("migration_violations") or []
    ret_viol = proposal.get("retired_capability_violations") or []
    rem_viol = proposal.get("removed_capability_violations") or []
    dep_viol = proposal.get("dependency_violations") or []
    don_viol = proposal.get("default_on_dependency_violations") or []
    lref_viol = proposal.get("local_reference_violations") or []
    if mig_viol or ret_viol or rem_viol or dep_viol or don_viol or lref_viol or impact_refusal:
        recovery = ["nothing was written and no release was opened."]
        if mig_viol:
            recovery.append("Restore each dropped upgrade step to the capability's settings file; to retire a "
                            "step, keep its version key and make its action do nothing — never delete the key.")
        if ret_viol:
            recovery.append("Restore each dropped retired-capability notice by keeping its version key — a "
                            "retirement notice has no no-op form, so it must never be dropped.")
        if rem_viol:
            recovery.append("For each removed module, add its plain-language removal notice to engine.json "
                            "removed_capabilities by hand (the module is already gone, so this is an edit, not "
                            "another `remove`) — a whole-module removal has no no-op form, so the notice is "
                            "required.")
        if dep_viol:
            recovery.append("Keep each still-depended-on capability, or remove its dependents too.")
        if don_viol:
            recovery.append("Make each such dependency required or default-on, or lower the dependent to optional "
                            "so it is not installed by default — a default-on module may depend only on "
                            "capabilities guaranteed present on every deployment.")
        if lref_viol:
            recovery.append("For each flagged line, name the capability the reference means instead of the bare "
                            "identifier, or move it to a form that travels — an engine eADR-#### record, or a "
                            "fully-qualified owner/repo#N — so a reader of a generated repository is not left with "
                            "a pointer to nothing.")
        violations = mig_viol + ret_viol + rem_viol + dep_viol + don_viol + lref_viol
        reasons = []
        if violations:
            reasons.append("a required release record or module dependency is missing, dropped, or "
                           "inconsistent, or a bare local reference would ship on a traveling surface")
        if impact_refusal:
            reasons.append(impact_refusal["reason"])
            violations = violations + impact_refusal["violations"]
            recovery.append(impact_refusal["recovery"])
        _print_refusal({"reason": "; ".join(reasons), "violations": violations, "recovery": " ".join(recovery)})
        return 2
    return 0


def _cmd_pr_body(args) -> int:
    proposal = validate.load_json(args.proposal)
    applied = validate.load_json(args.applied)
    deployment_gate = None
    if getattr(args, "deployment_gate_json", None):
        try:
            deployment_gate = validate.load_json(args.deployment_gate_json)
        except Exception as exc:   # noqa: BLE001 — a supplied-but-unreadable gate result renders honestly
            deployment_gate = {"_unreadable": args.deployment_gate_json, "_error": str(exc)}
    print(render_pr_body(proposal, applied, args.gate_state, deployment_gate))
    return 0


def _print_refusal(result: dict) -> None:
    """The plain-language reason a cut was refused, to stderr — the one legible account shared by the
    `--json` and human paths, so a refusal always says WHY (never a bare non-zero exit)."""
    print(f"Refused ({result.get('reason')}):", file=sys.stderr)
    for v in result.get("violations", []):
        print(f"  - {v}", file=sys.stderr)
    if result.get("recovery"):
        print(f"To fix: {result['recovery']}", file=sys.stderr)


def _cmd_apply(args) -> int:
    mode, _ctx = release_mode()
    if mode == "refuse":
        print(f"CONFIG ERROR: your product's version file ({PRODUCT_VERSION_REL}) could not be read — it must "
              f'be a small JSON file with a version, like {{"version": "0.1.0"}}. Fix it, then run the release '
              f"again. Nothing was changed.", file=sys.stderr)
        return 2
    # Load the proposal (if supplied) for BOTH paths — the product path needs it too, to enforce the derived
    # floor (StarshipSuperjam/engine-template#942); it was previously ignored there.
    proposal = None
    if args.proposal:
        if not os.path.isfile(args.proposal):
            print(f"CONFIG ERROR: the proposal file '{args.proposal}' does not exist. Pass the path to a "
                  f"proposal written by `propose --json`.", file=sys.stderr)
            return 2
        proposal = validate.load_json(args.proposal)
    if mode == "product":
        # PRODUCT cut (StarshipSuperjam/engine-template#516): write the one root product-version.json; --all/--package (engine package
        # machinery) do not apply to a product and are ignored — but the proposal's floor IS enforced.
        result = apply_product(args.engine, args.dry_run, proposal=proposal)
    else:
        packages = {}
        for spec in args.package or []:
            if "=" not in spec:
                print(f"CONFIG ERROR: --package expects id=version, got '{spec}'.", file=sys.stderr)
                return 2
            mid, ver = spec.split("=", 1)
            packages[mid.strip()] = ver.strip()
        result = apply(args.engine, getattr(args, "all"), packages, proposal, args.dry_run,
                       min_upgradeable_from=getattr(args, "min_upgradeable_from", None))
    ok = bool(result.get("applied")) or result.get("reason") == "dry-run"
    if args.json:
        print(json.dumps(result, indent=2))
        # The machine-readable refusal goes to stdout (the caller captures it, e.g. into applied.json). Print
        # the plain-language reason to STDERR too, so a refusal is never a bare non-zero exit: the release
        # workflow redirects stdout into a file, so without this the maintainer would see only "exit code 1".
        if not ok:
            _print_refusal(result)
        return 0 if ok else 1
    if result.get("applied"):
        print(f"Applied: engine {result['from_engine']} -> {result['engine']}; "
              f"{len(result['targets'])} package version(s) recorded.")
        return 0
    if result.get("reason") == "dry-run":
        print(f"Dry run: engine {result['from_engine']} -> {result['engine']} across "
              f"{len(result['targets'])} package(s); nothing written.")
        return 0
    _print_refusal(result)
    return 1


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(prog="release_cut.py", description="Decide and record the next engine version.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("propose", help="read-only: the proposed bump floor + change inventory")
    pp.add_argument("--json", action="store_true")
    pp.add_argument("--baseline-tree", help="a local release tree to diff against (tests/workflow inject this)")
    pp.add_argument("--legacy-impact", choices=list(release_impact.RELEASE_IMPACTS),
                    help="the aggregate release impact for pre-marker merged pull requests that carry no marker "
                         "(the one-time rollout tranche); the cut refuses to auto-derive across an undeclared "
                         "pull request until this is given or each is marked")
    pa = sub.add_parser("apply", help="record the chosen versions into the manifests (atomic, raise-only)")
    pa.add_argument("--engine", required=True, help="the new engine version")
    pa.add_argument("--all", help="set every present package to this version (the first-cut / uniform case)")
    pa.add_argument("--package", action="append", help="id=version override for one package (repeatable)")
    pa.add_argument("--proposal", help="a proposal JSON from `propose` to enforce the confirmed floor against")
    pa.add_argument("--min-upgradeable-from", dest="min_upgradeable_from",
                    help="record the oldest engine release with a clean one-run upgrade path to this release "
                         "(for example 0.3.2) into engine.json; omit to carry any prior floor forward unchanged")
    pa.add_argument("--dry-run", action="store_true", help="compute + validate but write nothing")
    pa.add_argument("--json", action="store_true")
    pb = sub.add_parser("pr-body", help="render the release pull-request body from a proposal + apply-result")
    pb.add_argument("--proposal", required=True, help="the proposal JSON written by `propose --json`")
    pb.add_argument("--applied", required=True, help="the result JSON written by `apply --json`")
    pb.add_argument("--gate-state", default="sub-bar", choices=["passed", "sub-bar", "errored"],
                    help="the acceptance-benchmark outcome to render (only 'sub-bar' is reachable while no "
                         "benchmark measures a release)")
    pb.add_argument("--deployment-gate-json", dest="deployment_gate_json", metavar="PATH",
                    help="the release_gate.py --json-out result; its per-transition upgrade/rollback outcomes "
                         "are recorded in the Validation section (engine cuts only)")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "propose":
            return _cmd_propose(args)
        if args.cmd == "pr-body":
            return _cmd_pr_body(args)
        return _cmd_apply(args)
    except Exception as exc:  # plain-language failure, never a traceback
        print(f"\nRELEASE-CUT ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
