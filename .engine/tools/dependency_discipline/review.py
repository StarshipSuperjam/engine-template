#!/usr/bin/env python3
"""Dependency-review gate — the read-only `custom/script` entry for engine/check/dependency-review
(the dependency-discipline module's *hard* review gate).

What it does: when a pull request is being checked in CI, it relays GitHub's own first-party dependency-review
data — the comparison of the change's dependencies against the project's current ones
(`GET /repos/{owner}/{repo}/dependency-graph/compare/{base}...{head}`) — and BLOCKS the merge when the change
ADDS an outside package with EITHER a known security vulnerability OR a license problem. It is read-only: it
only reads the comparison and emits findings; it never writes or rewrites a lockfile (the R5 mutation firewall).

The two license rules (this slice):
  • an UNIDENTIFIABLE license (GitHub can't determine it) blocks on ANY repo — the question is whether you may
    legally use or ship the package at all, which has nothing to do with whether your code is open;
  • a strong-COPYLEFT license (GPL/AGPL, plus the source-available SSPL) blocks ONLY on a private/internal repo —
    its "you may have to publish your own source" obligation only bites on code you mean to keep closed; on a
    public project, where your source is already open, it isn't enforced (a soft heads-up is shown instead).
Repo visibility is read once (a metadata GET) ONLY to decide the copyleft rule; a read miss leaves the copyleft
rule inactive and SAYS SO, never a spurious block.

Accepted exceptions: the check carries its OWN committed allow-lists in its rule params — `allow-ghsas` (accept
a specific security advisory) and `allow-licenses` (accept a specific license) — so a genuinely-unfixable
finding never strands the operator. Adding an entry edits the guarded check definition, which the existing §15
weakening guard catches and blocks until the operator's deliberate `guardrail-ack`; this script neither owns nor
re-implements that acknowledgment — it relays into it. An accepted finding passes with a SOFT note that names
what was accepted (never a silent pass).

Honest tiers / blocking: a vulnerability block, a copyleft-on-private block, and an unknown-license block are
each `hard` findings, so they fail CI's blocking gate. Every other outcome is a visible `soft` finding that
PASSES, never a silent green and never a hard block:
  • no pull request to compare (a local run, a non-PR run) → a disclosed "nothing to review here" note;
  • the data isn't available on this repository tier (a private repo without GitHub's paid code-security
    feature, or a fork → HTTP 403) → a disclosed cost/benefit note naming the price and the levers;
  • the data couldn't be reached this run (a 404, a 5xx, a network glitch) → a disclosed "didn't evaluate —
    re-run" note that names no cost and frames itself as transient;
  • a copyleft dependency on a PUBLIC repo → a disclosed heads-up that it isn't enforced and why;
  • repo visibility couldn't be read → a disclosed note that the copyleft rule was left inactive this run;
  • an accepted advisory/license → a disclosed accept note.
The script always exits 0 on these handled branches, so the validator's fail-closed path is reserved for a
genuine crash (a broken check must still fail loud). Every read and parse degrades in-band, never raises.

Engine/product wall (§13): the dependency-review API reports the whole repository's dependency graph, so this
check filters out any change whose `manifest` is under `.engine/` — it gates the PRODUCT's own dependencies,
never the engine's walled internal tooling.

GitHub Actions carve-out (license only): GitHub's dependency graph reports NO SPDX license for the Actions
ecosystem, so an action declared in a workflow file (`.github/workflows/`) always classifies as an
unidentifiable license — and `unknown` has no per-package accept-path. Gating it would permanently block any
change that adds a workflow action for a data-availability artifact, not a real license risk. So the LICENSE
gate is skipped for workflow-declared actions; the VULNERABILITY gate still applies (a vulnerable action runs
in CI with a token — a real supply-chain risk worth blocking).

Contract: invoked by the validator with NO arguments, it prints a finding.v1 JSON array to stdout and exits 0.
A separate `demo` subcommand runs a falsifiable self-check over a fake transport.
"""
from __future__ import annotations
import json
import os
import sys
import urllib.error
import urllib.request

# Make the sibling `.engine/tools/` modules importable whether imported as `dependency_discipline.review`
# or run directly as the wired check script (the projects_sync / pinning idiom).
_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

import validate  # noqa: E402 — the finding.v1 helper + ROOT (test-redirectable)
import github_client  # noqa: E402  (the shared authenticated GitHub API client; request-build)

USER_AGENT = "engine-dependency-review"
_PRICING_URL = "https://github.com/pricing"
_ENGINE_PREFIX = ".engine/"  # the §13 wall: manifests here are the engine's own tooling, never product deps
# GitHub Actions are declared only in workflow files. GitHub's dependency graph carries NO SPDX license for the
# Actions ecosystem, so every action classifies as an unidentifiable license — and there is no per-package
# accept-path for 'unknown'. License-gating them would permanently block any change that adds an action for a
# data-availability artifact, not a real license risk. So the LICENSE gate is skipped for workflow-declared
# actions; the VULNERABILITY gate still applies (a vulnerable action running in CI is a real supply-chain risk).
_WORKFLOW_PREFIX = ".github/workflows/"

# The strong-copyleft / source-disclosure deny-set, by SPDX *base* identifier (the `-only` / `-or-later` / `+`
# variants normalize to these). Strong copyleft (GPL, AGPL) can require publishing your own source if you
# distribute software that includes the package; SSPL is source-available and can require publishing the source
# of a *service* built on it — both are "may have to publish your own source" risks. LGPL (weak copyleft) is
# deliberately EXCLUDED: linking does not trigger the disclosure obligation. This is a module policy, not an
# operator knob — the operator's knob is the `allow-licenses` accept-path; it is quoted in the PR for consent.
_COPYLEFT_BASE = frozenset({"GPL-2.0", "GPL-3.0", "AGPL-1.0", "AGPL-3.0", "SSPL-1.0"})


# --- the disclosed-but-passing notes (every one is emitted as a SOFT finding, never []) ------------------
_NO_CONTEXT_MESSAGE = (
    "The dependency review gate runs while a pull request is being checked, where it compares the change's "
    "outside packages against the project's current ones. There's no pull request to compare here (for "
    "example a local run), so there's nothing for it to review. That's the normal, expected state, not a "
    "problem."
)

_UNAVAILABLE_MESSAGE = (
    "The dependency review gate is on, but GitHub isn't providing the data it needs on this project's plan, "
    "so it didn't block anything this run — and it's telling you that plainly rather than passing silently. "
    "This data is free on public projects. On a private project it comes with GitHub's paid code-security "
    f"feature (GitHub Code Security): see GitHub's current pricing at {_PRICING_URL} — as of June 2026 it is "
    "around $30 per active committer per month (an active committer is someone who pushed a commit in the "
    "last 90 days); check that page for the current figure. With it on, a change that adds an outside package "
    "with a known security problem would be caught and blocked here. Until then, your protection is the pull "
    "request you review and approve. Whether that's worth the cost is your call."
)

_DEGRADED_MESSAGE = (
    "The dependency review gate couldn't reach GitHub's review data on this run, so it didn't evaluate this "
    "change — and it's saying so rather than passing silently. This is usually a temporary glitch (a network "
    "hiccup or a brief GitHub outage), not a setting you need to change and not anything to pay for. "
    "Re-running the check normally clears it; if it keeps happening, treat this change as unreviewed until it "
    "does."
)


class DegradedReadError(Exception):
    """Raised when the dependency-review read fails for a reason other than 'unavailable on this tier' (a
    network error, a 404, a 5xx, an unreadable body). NEVER swallowed as 'clean' — the caller turns it into a
    visible SOFT disclosed-degradation note that PASSES, never a silent green and never a hard block."""


class _Unavailable(Exception):
    """HTTP 403 — dependency review is not available on this repository tier (a private repo without GitHub's
    paid code-security feature), or the comparison was run against a fork. The caller discloses the
    cost/benefit and PASSES (a soft note)."""


class DependencyReview:
    """The dependency-review comparison client. Mirrors the audit_digest / telemetry / issue_conformance
    injectable-transport seam: `transport(method, path, body) -> (status, json|None)` is injectable, so the
    demo and tests fake ONLY the network and run the real logic. Strictly read-only — it only GETs."""

    def __init__(self, repo: str, token: str, *, transport=None):
        self.repo = repo
        self.token = token
        self._transport = transport or self._http

    def _http(self, method: str, path: str, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = github_client.request(path, self.token, user_agent=USER_AGENT, method=method, data=data)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:           # 4xx/5xx — surface the status, never swallow
            return exc.code, None
        except urllib.error.URLError as exc:             # network unreachable — a read failure
            raise DegradedReadError(f"GitHub is unreachable: {exc}") from exc

    def compare(self, base: str, head: str) -> list:
        """The dependency changes between `base` and `head` (the API's list of change objects). Raises
        `_Unavailable` on 403 (the gate is not available on this tier, or a fork) and `DegradedReadError` on
        any other non-200 / unreadable body (a 404 not-found, a 5xx, a bad comparison) — distinct from
        'unavailable', surfaced as a transient note."""
        status, data = self._transport(
            "GET", f"/repos/{self.repo}/dependency-graph/compare/{base}...{head}", None)
        if status == 403:
            raise _Unavailable()
        if status >= 400 or data is None:
            raise DegradedReadError(f"GitHub returned {status} comparing dependencies")
        if not isinstance(data, list):
            raise DegradedReadError("the dependency-review comparison was not a list")
        return data

    def visibility(self) -> "str | None":
        """The repository's visibility — 'public', 'private', or 'internal' — or None if it can't be read.
        A read-only metadata GET (`/repos/{owner}/{repo}`), authorized by the always-granted `metadata: read`
        scope of the Actions token (so it works even under the validator's `contents: read`). Used ONLY to
        decide the copyleft license rule. NEVER raises: any failure returns None so the caller fails OPEN
        (copyleft rule inactive) and discloses, rather than spuriously blocking or crashing into the
        validator's fail-closed path."""
        try:
            status, data = self._transport("GET", f"/repos/{self.repo}", None)
        except DegradedReadError:
            return None
        if status != 200 or not isinstance(data, dict):
            return None
        vis = data.get("visibility")
        if isinstance(vis, str) and vis.strip():
            return vis.strip().lower()
        if data.get("private") is True:                  # fall back to the boolean if visibility is absent
            return "private"
        if data.get("private") is False:
            return "public"
        return None


def _load_event(event_path: "str | None"):
    """The pull-request event JSON from `$GITHUB_EVENT_PATH` (the safe issue_conformance._load_event /
    validate.get_pr_body pattern — read from the file, never a shell-interpolated argument), or None when
    absent/unreadable (a local run, a partial or CORRUPT event) → the caller treats it as 'no pull request to
    compare', so a malformed event degrades to the soft no-op, never an escape to the fail-closed hard path."""
    path = event_path or os.environ.get("GITHUB_EVENT_PATH")
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _pr_base_head(event) -> tuple:
    """(base_sha, head_sha) from a pull_request event, or (None, None) when it isn't a readable PR event."""
    if not isinstance(event, dict):
        return None, None
    pr = event.get("pull_request")
    if not isinstance(pr, dict):
        return None, None
    base = (pr.get("base") or {}).get("sha")
    head = (pr.get("head") or {}).get("sha")
    if isinstance(base, str) and base and isinstance(head, str) and head:
        return base, head
    return None, None


def _read_check_params() -> tuple:
    """(allow_ghsas, allow_licenses) from this check's OWN committed definition
    (`.engine/check/dependency-review.json`, resolved via `validate.ROOT` — the single source of truth, so the
    accept-lists live in the guarded check file the §15 weakening guard watches). Fail-SAFE: on ANY problem
    (missing / unreadable / malformed JSON, wrong shape) it returns empty lists — honoring NO exceptions, so a
    corrupt config keeps the gate fully blocking rather than silently loosening, and it never raises into the
    validator's fail-closed path. Lists default to empty; a present-but-empty list loosens nothing."""
    try:
        path = os.path.join(validate.ROOT, ".engine", "check", "dependency-review.json")
        with open(path, "r", encoding="utf-8") as fh:
            params = (json.load(fh) or {}).get("params") or {}
        ag = params.get("allow-ghsas")
        al = params.get("allow-licenses")
        ag = [g for g in ag if isinstance(g, str)] if isinstance(ag, list) else []
        al = [lic for lic in al if isinstance(lic, str)] if isinstance(al, list) else []
        return ag, al
    except Exception:
        return [], []


def _normalize_license_id(token: str) -> str:
    """Canonicalize one SPDX license identifier for matching: uppercase, strip whitespace, and drop a trailing
    '+' (the deprecated 'or later' marker). Matching normalizes BOTH the dependency's license and the operator's
    `allow-licenses` entries the same way, so an accept entry the operator pastes from the block message matches."""
    return (token or "").strip().upper().rstrip("+").strip()


def _license_base(token: str) -> str:
    """The deny-set base of one normalized identifier: drop the SPDX `-ONLY` / `-OR-LATER` suffix, so
    GPL-3.0-only / GPL-3.0-or-later / GPL-3.0 / GPL-3.0+ all map to the same base GPL-3.0."""
    t = _normalize_license_id(token)
    for suffix in ("-OR-LATER", "-ONLY"):
        if t.endswith(suffix):
            return t[: -len(suffix)]
    return t


class _ParseError(Exception):
    """The SPDX expression could not be parsed (unbalanced parens, a stray operator). The caller falls back to
    a conservative flat scan that leans toward blocking — never toward letting an un-accepted copyleft through."""


def _tokenize_spdx(raw: str) -> list:
    """Tokens of an SPDX expression: '(' / ')' / 'AND' / 'OR' / ('LIC', id). A `WITH <exception>` clause keeps
    the base license and drops the exception (the exception narrows the license; the base still governs).
    Never raises."""
    parts = raw.replace("(", " ( ").replace(")", " ) ").split()
    tokens, k = [], 0
    while k < len(parts):
        p, up = parts[k], parts[k].upper()
        if p in ("(", ")"):
            tokens.append(p)
        elif up in ("AND", "OR"):
            tokens.append(up)
        elif up == "WITH":
            k += 1                                        # skip the exception identifier that follows
        else:
            tokens.append(("LIC", p))
        k += 1
    return tokens


def _parse_spdx(tokens: list):
    """Parse SPDX tokens into a tree — ('lic', id) / ('and', a, b) / ('or', a, b) — with OR lower-precedence
    than AND (`A AND B OR C` == `(A AND B) OR C`). Raises _ParseError on a malformed expression."""
    pos = 0

    def peek():
        return tokens[pos] if pos < len(tokens) else None

    def expr():
        nonlocal pos
        node = term()
        while peek() == "OR":
            pos += 1
            node = ("or", node, term())
        return node

    def term():
        nonlocal pos
        node = factor()
        while peek() == "AND":
            pos += 1
            node = ("and", node, factor())
        return node

    def factor():
        nonlocal pos
        t = peek()
        if t == "(":
            pos += 1
            node = expr()
            if peek() != ")":
                raise _ParseError("unbalanced parentheses")
            pos += 1
            return node
        if isinstance(t, tuple) and t[0] == "LIC":
            pos += 1
            return ("lic", t[1])
        raise _ParseError(f"unexpected token {t!r}")

    tree = expr()
    if pos != len(tokens):
        raise _ParseError("trailing tokens")
    return tree


def _eval_blocking(node, allow) -> set:
    """The set of un-escapable, un-accepted copyleft license ids in `node` (empty = not copyleft-blocked). A
    license blocks iff its base is in the deny-set and it is NOT on `allow`. AND = union (you must satisfy
    both, so either side's obligation binds); OR = clean if EITHER side is clean (the licensee may pick the
    clean option), else the union. This honors the real meaning of an SPDX expression, so copyleft inside a
    required AND branch cannot be hidden behind a permissive OR elsewhere."""
    if node[0] == "lic":
        lic = node[1]
        if _license_base(lic) in _COPYLEFT_BASE and _normalize_license_id(lic) not in allow:
            return {lic}
        return set()
    left = _eval_blocking(node[1], allow)
    right = _eval_blocking(node[2], allow)
    if node[0] == "and":
        return left | right
    return set() if (not left or not right) else (left | right)   # OR: a clean branch clears it


def _license_status(expr, allow_licenses) -> tuple:
    """Classify a change's `license` field for gating. Returns `(status, ids)` where status is one of
    `clean` / `copyleft` / `unknown` / `accepted`, and `ids` is the operator-facing license tokens that drove a
    copyleft/accepted result (for the message). NEVER raises.

      • `unknown`  — no license string, 'NOASSERTION', or no license identifier at all → block-leaning.
      • `copyleft` — a strong-copyleft/SSPL obligation the licensee can't escape and hasn't accepted.
      • `accepted` — the only thing that would block is on `allow_licenses` → waved through (a soft note).
      • `clean`    — no copyleft obligation, or one the licensee can escape via a permissive OR-choice.
    AND/OR structure is evaluated, so copyleft in a required AND branch blocks even with a permissive OR
    elsewhere. `allow_licenses` is a set of normalized identifiers. A malformed expression falls back to a
    conservative flat scan that leans toward blocking."""
    raw = expr.strip() if isinstance(expr, str) else ""
    if not raw or raw.upper() == "NOASSERTION":
        return "unknown", ()
    tokens = _tokenize_spdx(raw)
    lic_ids = [t[1] for t in tokens if isinstance(t, tuple)]
    if not lic_ids:
        return "unknown", ()
    try:
        if len(tokens) > 100:                             # pathological size — skip the deep recursive parse
            raise _ParseError("expression too large")
        tree = _parse_spdx(tokens)
    except (_ParseError, RecursionError):                 # malformed/pathological → conservative flat scan
        copyleft = [c for c in lic_ids if _license_base(c) in _COPYLEFT_BASE]
        unaccepted = [c for c in copyleft if _normalize_license_id(c) not in allow_licenses]
        if unaccepted:
            return "copyleft", tuple(sorted(set(unaccepted)))
        if copyleft:
            return "accepted", tuple(sorted({c for c in copyleft if _normalize_license_id(c) in allow_licenses}))
        return "unknown", ()
    net = _eval_blocking(tree, allow_licenses)
    if net:
        return "copyleft", tuple(sorted(net))
    raw_block = _eval_blocking(tree, frozenset())
    if raw_block:                                         # copyleft present, but the allow-list cleared it
        accepted = sorted({c for c in raw_block if _normalize_license_id(c) in allow_licenses})
        return ("accepted", tuple(accepted)) if accepted else ("clean", ())
    return "clean", ()


def _filter_vulns(vulns, allow_ghsas) -> tuple:
    """(kept, accepted): drop any vulnerability whose advisory GHSA id is on `allow_ghsas` (a non-empty id on
    both sides). Returns the vulnerabilities that still block and the set of accepted ids that were dropped."""
    kept, accepted = [], set()
    for v in vulns:
        if not isinstance(v, dict):
            continue
        ghsa = (v.get("advisory_ghsa_id") or "").strip()
        if ghsa and ghsa in allow_ghsas:
            accepted.add(ghsa)
        else:
            kept.append(v)
    return kept, accepted


def _where(change: dict) -> tuple:
    """(display 'name version', manifest) for one change, with plain fallbacks."""
    name = change.get("name") or "an outside package"
    version = change.get("version") or ""
    return f"{name} {version}".strip(), (change.get("manifest") or "your project's dependency file")


def _vuln_message(change: dict, vulns: list) -> str:
    """The plain-language hard-block message for one vulnerable added dependency: it leads with the package and
    what's wrong, glosses 'advisory', gives the next step plus the AI-remediation offer, and names the now-live
    accept-path as a deliberate, acknowledged choice (never a one-click bypass)."""
    where, manifest = _where(change)
    name = change.get("name") or "an outside package"
    eco = change.get("ecosystem")
    eco_note = f" ({eco})" if eco else ""
    lines, ids = [], []
    for v in vulns:
        if not isinstance(v, dict):
            continue
        sev = (v.get("severity") or "unknown").strip()
        summary = (v.get("advisory_summary") or "a known security problem").strip()
        url = (v.get("advisory_url") or "").strip()
        ghsa = (v.get("advisory_ghsa_id") or "").strip()
        if ghsa:
            ids.append(ghsa)
        ref = f" [{ghsa}]" if ghsa else ""
        link = f" — {url}" if url else ""
        lines.append(f"  - {sev} severity: {summary}{ref}{link}")
    advisories = "\n".join(lines) if lines else "  - a known security problem"
    which = f" ({ids[0]})" if len(ids) == 1 else ""
    return (
        f"This change adds {where}{eco_note}, declared in {manifest}, and it has a known security problem — a "
        f"published security advisory reports a way it can be exploited:\n"
        f"{advisories}\n"
        f"A vulnerable package can put your project at risk, so this check blocks the merge. To clear it: "
        f"update {name} to a version that fixes the advisory, or remove or replace the dependency — your "
        f"engine can propose the change for you if you ask. If {name} genuinely can't be updated or removed "
        f"right now, the decision to proceed is yours to make deliberately: you can formally accept this "
        f"advisory{which} so the check passes — your engine can add it to this check's accepted-exceptions "
        f"list for you and bring it back for your approval as a deliberate, acknowledged change (it never turns "
        f"the check off on its own). This check surfaces the risk; it doesn't take the choice away from you."
    )


def _copyleft_block_message(change: dict, ids: tuple) -> str:
    """Hard block: a strong-copyleft/SSPL dependency on a PRIVATE repo. Glosses 'copyleft', names the license,
    explains the private-vs-public 'why' (the proxy) right in the finding, and gives both escapes."""
    where, manifest = _where(change)
    lic = ", ".join(ids) if ids else "a copyleft license"
    return (
        f"This change adds {where}, declared in {manifest}, under {lic} — a 'copyleft' or source-available "
        f"license: the kind that can require you to publish your own project's source code under the same terms "
        f"if you distribute or host software that includes this package. Your project is private, so that "
        f"obligation would land on "
        f"code you mean to keep closed — so this check blocks the merge. To clear it: replace the dependency "
        f"with one under a permissive license (such as MIT, Apache-2.0, or BSD) — your engine can propose the "
        f"change for you if you ask — or, if you've weighed it and accept the obligation, formally accept this "
        f"license ({lic}) so the check passes: your engine can add it to this check's accepted-exceptions list "
        f"for you and bring it back for your approval as a deliberate, acknowledged change. This is a judgment "
        f"based on your project being private (the precise legal line is about how you distribute or host the "
        f"software); on a public project, where your source is already open, this license isn't enforced."
    )


def _unknown_license_message(change: dict) -> str:
    """Hard block: a dependency whose license could not be identified — blocks on ANY repo."""
    where, manifest = _where(change)
    return (
        f"This change adds {where}, declared in {manifest}, but no license could be identified for it. Without a "
        f"clear license you may not have the right to use or ship this package at all, and you can't tell what "
        f"obligations come with it — so this check blocks the merge. This applies whether your project is public "
        f"or private: the question here isn't about keeping your code closed, it's whether you're permitted to "
        f"use this package in the first place. To clear it: replace it with a package that has a clear, known "
        f"license — your engine can propose the change for you if you ask — or, once you've confirmed the "
        f"license yourself, formally accept it so the check passes: your engine can add it to this check's "
        f"accepted-exceptions list for you and bring it back for your approval as a deliberate, acknowledged "
        f"change."
    )


def _copyleft_public_note(change: dict, ids: tuple) -> str:
    """Soft heads-up: a copyleft dependency on a PUBLIC repo — not enforced, and why."""
    name = change.get("name") or "an outside package"
    lic = ", ".join(ids) if ids else "a copyleft license"
    return (
        f"Heads-up: this change adds {name} under {lic} — a 'copyleft' or source-available license, the kind "
        f"that can require you to publish your own source code if you distribute software that includes it. Your "
        f"project is public, so "
        f"its source is already open and that obligation adds no surprise — this isn't blocked and you don't "
        f"need to do anything. (This is a judgment based on your project being public; the precise legal line is "
        f"about how you distribute or host the software. If you also ship this inside a separate closed or "
        f"private product, weigh it there.)"
    )


def _visibility_unreadable_note(change: dict, ids: tuple) -> str:
    """Soft disclosure: a copyleft dependency was present but the repo's visibility couldn't be read this run."""
    name = change.get("name") or "an outside package"
    lic = ", ".join(ids) if ids else "a copyleft license"
    return (
        f"This change adds {name} under {lic} — a 'copyleft' or source-available license, which is only checked "
        f"on private projects (on a public project your source is already open, so it doesn't apply). The check "
        f"couldn't determine "
        f"whether this project is private or public on this run, so it left that license check switched off "
        f"rather than block you on a guess — nothing was blocked on license grounds. If this project is private "
        f"and you want this enforced, re-running the check normally clears the read. The package's security was "
        f"still checked and is unaffected."
    )


def _accepted_ghsas_note(ids: list) -> str:
    """Soft note: one or more security advisories passed because they are on the accepted-exceptions list."""
    listed = ", ".join(ids)
    return (
        f"For your awareness: this check passed a security advisory you have formally accepted as an allowed "
        f"exception ({listed}). It isn't blocking because it's on this check's accepted-exceptions list — a "
        f"deliberate, acknowledged choice, not a silent pass. If you no longer want to accept it, your engine "
        f"can remove it from that list for you and bring the change back for your approval."
    )


def _accepted_licenses_note(ids: list) -> str:
    """Soft note: one or more dependency licenses passed because they are on the accepted-exceptions list."""
    listed = ", ".join(ids)
    return (
        f"For your awareness: this check passed a dependency license you have formally accepted as an allowed "
        f"exception ({listed}). It isn't blocking because it's on this check's accepted-exceptions list — a "
        f"deliberate, acknowledged choice, not a silent pass. If you no longer want to accept it, your engine "
        f"can remove it from that list for you and bring the change back for your approval."
    )


def _safe_visibility(client) -> "str | None":
    """`client.visibility()`, guarded so even an injected client that raises degrades to None (fail-open)."""
    try:
        return client.visibility()
    except Exception:
        return None


def _is_private(visibility: "str | None") -> bool:
    """A 'private' or 'internal' repo is closed source for the copyleft question; anything else (public,
    unknown, unreadable) is treated as not-private — so a visibility miss never blocks copyleft on a guess."""
    return visibility in ("private", "internal")


def findings(block_tier: str = "hard", *, event_path: "str | None" = None,
             repo: "str | None" = None, token: "str | None" = None, client=None,
             allow_ghsas: "list | None" = None, allow_licenses: "list | None" = None) -> list:
    """The review-gate findings for the current pull request, as a list of finding.v1 dicts.

    A vulnerability block, a copyleft-on-private block, and an unknown-license block are emitted at `block_tier`
    (the rule tier, `hard`). Every disclosure branch — no pull request, the data unavailable on this tier, a
    read failure, a copyleft dep on a public repo, an unreadable visibility, an accepted exception — is a `soft`
    finding that PASSES; never `[]` where a risk was waived, and never `hard`. This function never raises on a
    handled branch (every read/parse degrades in-band), so the script exits 0 and the validator's fail-closed
    hard path is reserved for a genuine crash. `event_path`/`repo`/`token`/`client`/`allow_*` are injectable for
    the demo and tests; in production the allow-lists default to this check's own committed params (fail-safe to
    empty) and the rest to the CI environment + a real client.
    """
    if allow_ghsas is None or allow_licenses is None:
        ag, al = _read_check_params()
        allow_ghsas = ag if allow_ghsas is None else allow_ghsas
        allow_licenses = al if allow_licenses is None else allow_licenses
    allow_ghsas = {g.strip() for g in allow_ghsas if isinstance(g, str) and g.strip()}
    allow_licenses = {_normalize_license_id(lic) for lic in allow_licenses if isinstance(lic, str) and lic.strip()}

    event = _load_event(event_path)
    base, head = _pr_base_head(event)
    repo = repo or os.environ.get("GITHUB_REPOSITORY")
    token = token or os.environ.get("GITHUB_TOKEN")
    if not base or not head or not repo or not token:
        return [validate.finding("soft", _NO_CONTEXT_MESSAGE, None)]

    client = client or DependencyReview(repo, token)
    try:
        changes = client.compare(base, head)
    except _Unavailable:
        return [validate.finding("soft", _UNAVAILABLE_MESSAGE, None)]
    except DegradedReadError:
        return [validate.finding("soft", _DEGRADED_MESSAGE, None)]

    out = []
    accepted_ghsas, accepted_licenses, copyleft = set(), set(), []
    for change in changes:
        if not isinstance(change, dict):
            continue
        if change.get("change_type") != "added":            # only what the PR brings IN can block
            continue
        manifest = change.get("manifest") or ""
        if isinstance(manifest, str) and manifest.startswith(_ENGINE_PREFIX):
            continue                                          # the §13 wall: never the engine's own tooling
        location = {"file": manifest, "line": None} if isinstance(manifest, str) and manifest else None

        kept, dropped = _filter_vulns(change.get("vulnerabilities") or [], allow_ghsas)
        accepted_ghsas |= dropped
        if kept:
            out.append(validate.finding(block_tier, _vuln_message(change, kept), location))

        # the LICENSE gate does not apply to a GitHub Action (declared in a workflow file): GitHub's dependency
        # graph reports no SPDX license for the Actions ecosystem, so it always classifies 'unknown', which has
        # no accept-path — gating it would permanently block adding an action for a data artifact, not a real
        # license risk. The vulnerability gate above still ran for it.
        if isinstance(manifest, str) and manifest.startswith(_WORKFLOW_PREFIX):
            continue

        status, ids = _license_status(change.get("license"), allow_licenses)
        if status == "accepted":
            accepted_licenses.update(ids)
        elif status == "unknown":
            out.append(validate.finding(block_tier, _unknown_license_message(change), location))
        elif status == "copyleft":
            copyleft.append((change, ids, location))          # decided once visibility is known, below

    if copyleft:
        visibility = _safe_visibility(client)
        for change, ids, location in copyleft:
            if _is_private(visibility):
                out.append(validate.finding(block_tier, _copyleft_block_message(change, ids), location))
            elif visibility is None:
                out.append(validate.finding("soft", _visibility_unreadable_note(change, ids), location))
            else:
                out.append(validate.finding("soft", _copyleft_public_note(change, ids), location))

    if accepted_ghsas:
        out.append(validate.finding("soft", _accepted_ghsas_note(sorted(accepted_ghsas)), None))
    if accepted_licenses:
        out.append(validate.finding("soft", _accepted_licenses_note(sorted(accepted_licenses)), None))
    return out


def emit_findings() -> int:
    """The no-argument path the validator invokes: print the finding.v1 array and return 0. The blocking
    findings carry the rule tier (`ENGINE_RULE_TIER`, set by the validator to `hard`); the disclosure branches
    are soft regardless, by design. The accept-lists are read from this check's own committed params."""
    print(json.dumps(findings(os.environ.get("ENGINE_RULE_TIER", "hard"))))
    return 0


def demo() -> int:
    """Prove the gate: blocks a vulnerable product dependency; blocks an unidentifiable license on any repo and
    a copyleft license on a PRIVATE repo; DISCLOSES (never silent-greens) a copyleft license on a PUBLIC repo,
    an unreadable visibility, an accepted advisory, and an accepted license; passes a clean/permissive one;
    walls off the engine's own `.engine/` tooling; reports only the ADDED side of a version bump; and discloses
    an unavailable tier, a transient read failure, and a non-PR run. RETURNS NON-ZERO if any invariant is broken
    (the falsification can fail). No network: every case runs the real `findings()` over a fake transport/client
    and a throwaway event file, so nothing is ever written."""
    import shutil
    import tempfile

    class _Canned:
        """A stand-in client whose `compare`/`visibility` return canned values or raise canned exceptions."""
        def __init__(self, outcome, visibility="public"):
            self._outcome = outcome
            self._visibility = visibility

        def compare(self, base, head):
            if isinstance(self._outcome, Exception):
                raise self._outcome
            return self._outcome

        def visibility(self):
            if isinstance(self._visibility, Exception):
                raise self._visibility
            return self._visibility

    tmp = tempfile.mkdtemp(prefix="engine-depreview-demo-")
    try:
        event = os.path.join(tmp, "event.json")
        with open(event, "w", encoding="utf-8") as fh:
            json.dump({"pull_request": {"base": {"sha": "aaa"}, "head": {"sha": "bbb"}}}, fh)
        missing_event = os.path.join(tmp, "missing.json")  # deliberately never created

        adv = {"severity": "high", "advisory_ghsa_id": "GHSA-demo-0000",
               "advisory_summary": "Remote code execution", "advisory_url":
               "https://github.com/advisories/GHSA-demo-0000"}
        vuln = {"change_type": "added", "manifest": "package.json", "ecosystem": "npm",
                "name": "demo-pkg", "version": "1.0.0", "license": "MIT", "vulnerabilities": [adv]}
        engine_vuln = dict(vuln, manifest=".engine/pyproject.toml")     # same vuln, the engine's tooling
        clean = {"change_type": "added", "manifest": "package.json", "name": "ok", "version": "1.0.0",
                 "license": "Apache-2.0", "vulnerabilities": []}
        removed_old = {"change_type": "removed", "manifest": "package.json", "name": "demo-pkg",
                       "version": "0.9.0", "license": "MIT", "vulnerabilities": [adv]}  # base side of a bump
        copyleft = {"change_type": "added", "manifest": "package.json", "name": "gpl-pkg",
                    "version": "2.0.0", "license": "GPL-3.0-or-later", "vulnerabilities": []}
        copyleft_nested = dict(copyleft, name="nested-pkg",         # copyleft in a required AND branch
                               license="(MIT OR Apache-2.0) AND GPL-3.0")
        unknown_lic = {"change_type": "added", "manifest": "package.json", "name": "mystery-pkg",
                       "version": "1.0.0", "license": "NOASSERTION", "vulnerabilities": []}
        action_no_license = {"change_type": "added", "manifest": ".github/workflows/release.yml",
                             "name": "actions/checkout", "version": "v7.0.0", "license": "NOASSERTION",
                             "vulnerabilities": []}                          # a workflow action: license unknowable
        action_vuln = dict(action_no_license, name="evil/action", vulnerabilities=[adv])  # but a vuln still bites

        def run(client, *, allow_ghsas=(), allow_licenses=()):
            return findings("hard", event_path=event, repo="o/r", token="t", client=client,
                            allow_ghsas=list(allow_ghsas), allow_licenses=list(allow_licenses))

        def one_hard(fs):
            return len(fs) == 1 and fs[0]["severity"] == "hard"

        def one_soft(fs, needle):
            return len(fs) == 1 and fs[0]["severity"] == "soft" and needle in fs[0]["message"]

        cases = [
            ("a vulnerable added product dependency earns one hard block",
             lambda: run(_Canned([vuln])),
             lambda fs: one_hard(fs) and "GHSA-demo-0000" in fs[0]["message"]),
            ("the same vulnerability under .engine/ is walled off (the §13 wall)",
             lambda: run(_Canned([engine_vuln])), lambda fs: fs == []),
            ("a clean, permissively-licensed added dependency passes",
             lambda: run(_Canned([clean])), lambda fs: fs == []),
            ("a version bump reports only the added side, once",
             lambda: run(_Canned([removed_old, vuln])), one_hard),
            ("a copyleft license on a PRIVATE repo earns one hard block",
             lambda: run(_Canned([copyleft], visibility="private")),
             lambda fs: one_hard(fs) and "copyleft" in fs[0]["message"]),
            ("a copyleft license on an INTERNAL repo earns one hard block",
             lambda: run(_Canned([copyleft], visibility="internal")), one_hard),
            ("copyleft hidden in a mandatory AND branch still blocks on a private repo",
             lambda: run(_Canned([copyleft_nested], visibility="private")),
             lambda fs: one_hard(fs) and "copyleft" in fs[0]["message"]),
            ("a copyleft license on a PUBLIC repo discloses a soft heads-up, never silent",
             lambda: run(_Canned([copyleft], visibility="public")),
             lambda fs: one_soft(fs, "public")),
            ("an unreadable visibility leaves the copyleft rule inactive and discloses it",
             lambda: run(_Canned([copyleft], visibility=RuntimeError("no repo read"))),
             lambda fs: one_soft(fs, "couldn't determine")),
            ("an unidentifiable license blocks even on a PUBLIC repo",
             lambda: run(_Canned([unknown_lic], visibility="public")),
             lambda fs: one_hard(fs) and "no license could be identified" in fs[0]["message"]),
            ("a workflow action with an unidentifiable license is NOT license-blocked (no SPDX for Actions)",
             lambda: run(_Canned([action_no_license], visibility="public")), lambda fs: fs == []),
            ("a VULNERABLE workflow action is still blocked (the carve-out is license-only)",
             lambda: run(_Canned([action_vuln], visibility="public")),
             lambda fs: one_hard(fs) and "GHSA-demo-0000" in fs[0]["message"]),
            ("an accepted license passes with a soft accept-note (no hard)",
             lambda: run(_Canned([copyleft], visibility="private"), allow_licenses=["GPL-3.0-or-later"]),
             lambda fs: one_soft(fs, "accepted")),
            ("an accepted advisory passes with a soft accept-note (no hard)",
             lambda: run(_Canned([vuln]), allow_ghsas=["GHSA-demo-0000"]),
             lambda fs: one_soft(fs, "accepted")),
            ("an unavailable tier (403) discloses the cost/benefit and passes (soft)",
             lambda: run(_Canned(_Unavailable())),
             lambda fs: one_soft(fs, "GitHub Code Security")),
            ("a transient read failure discloses a soft note and passes",
             lambda: run(_Canned(DegradedReadError("boom"))),
             lambda fs: len(fs) == 1 and fs[0]["severity"] == "soft"
             and "temporary" in fs[0]["message"].lower()),
        ]

        failures = []
        for label, call, ok in cases:
            result = call()
            if not ok(result):
                failures.append(f"{label}: invariant broken, got {result}")

        # the no-pull-request branch: a missing event short-circuits to one soft no-op WITHOUT touching the client
        no_ctx = findings("hard", event_path=missing_event, repo="o/r", token="t",
                          client=_Canned(RuntimeError("the client must not be used when there's no PR")),
                          allow_ghsas=[], allow_licenses=[])
        if not (len(no_ctx) == 1 and no_ctx[0]["severity"] == "soft"
                and "nothing for it to review" in no_ctx[0]["message"]):
            failures.append(f"no-pull-request should disclose one soft no-op, got {no_ctx}")

        if failures:
            print("DEMO FAILED — the dependency-review gate broke an invariant:")
            for f in failures:
                print(f"  - {f}")
            return 1
        print("DEMO PASSED — the review gate blocks a vulnerable dependency, an unidentifiable license (any "
              "repo), and a copyleft license on a private repo; discloses (never silent-greens) a copyleft "
              "license on a public repo, an unreadable visibility, accepted exceptions, an unavailable tier, "
              "and a transient failure; passes a clean one; walls off the engine's own tooling; and reports "
              "only the added side of a version bump.")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return demo()
    return emit_findings()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
