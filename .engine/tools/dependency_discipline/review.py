#!/usr/bin/env python3
"""Dependency-review gate — the read-only `custom/script` entry for engine/check/dependency-review
(the dependency-discipline module's *hard* review gate).

What it does: when a pull request is being checked in CI, it relays GitHub's own first-party dependency-review
data — the comparison of the change's dependencies against the project's current ones
(`GET /repos/{owner}/{repo}/dependency-graph/compare/{base}...{head}`) — and BLOCKS the merge when the change
ADDS an outside package with a known security vulnerability. It is read-only: it only reads the comparison and
emits findings; it never writes or rewrites a lockfile (the R5 mutation firewall).

Honest tiers / blocking: the vulnerability block is a `hard` finding, so it fails CI's blocking gate. Every
other outcome is a visible `soft` finding that PASSES, never a silent green and never a hard block:
  • no pull request to compare (a local run, a non-PR run) → a disclosed "nothing to review here" note;
  • the data isn't available on this repository tier (a private repo without GitHub's paid code-security
    feature, or a fork → HTTP 403) → a disclosed cost/benefit note naming the price and the levers;
  • the data couldn't be reached this run (a 404, a 5xx, a network glitch) → a disclosed "didn't evaluate —
    re-run" note that names no cost and frames itself as transient.
The script always exits 0 on these handled branches, so the validator's fail-closed path is reserved for a
genuine crash (a broken check must still fail loud).

Engine/product wall (§13): the dependency-review API reports the whole repository's dependency graph, so this
check filters out any change whose `manifest` is under `.engine/` — it gates the PRODUCT's own dependencies,
never the engine's walled internal tooling. License-compatibility gating is a separate step that arrives later
in this module (the accepted-exception allow-list); this gate blocks on security vulnerabilities only.

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

import validate  # noqa: E402 — the finding.v1 helper

API_ROOT = "https://api.github.com"
USER_AGENT = "engine-dependency-review"
_PRICING_URL = "https://github.com/pricing"
_ENGINE_PREFIX = ".engine/"  # the §13 wall: manifests here are the engine's own tooling, never product deps


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
        req = urllib.request.Request(
            API_ROOT + path, data=data, method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
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


def _vuln_message(change: dict) -> str:
    """The plain-language hard-block message for one vulnerable added dependency: it leads with the package
    and what's wrong, glosses 'advisory', gives the next step plus the AI-remediation offer, names the
    operator's deliberate-decision escape, and notes the formal accept-path that arrives later."""
    name = change.get("name") or "an outside package"
    version = change.get("version") or ""
    where = (f"{name} {version}".strip())
    manifest = change.get("manifest") or "your project's dependency file"
    eco = change.get("ecosystem")
    eco_note = f" ({eco})" if eco else ""
    lines = []
    for v in (change.get("vulnerabilities") or []):
        if not isinstance(v, dict):
            continue
        sev = (v.get("severity") or "unknown").strip()
        summary = (v.get("advisory_summary") or "a known security problem").strip()
        url = (v.get("advisory_url") or "").strip()
        ghsa = (v.get("advisory_ghsa_id") or "").strip()
        ref = f" [{ghsa}]" if ghsa else ""
        link = f" — {url}" if url else ""
        lines.append(f"  - {sev} severity: {summary}{ref}{link}")
    advisories = "\n".join(lines) if lines else "  - a known security problem"
    return (
        f"This change adds {where}{eco_note}, declared in {manifest}, and it has a known security problem — a "
        f"published security advisory reports a way it can be exploited:\n"
        f"{advisories}\n"
        f"A vulnerable package can put your project at risk, so this check blocks the merge. To clear it: "
        f"update {name} to a version that fixes the advisory, or remove or replace the dependency — your "
        f"engine can propose the change for you if you ask. If {name} genuinely can't be updated or removed "
        f"right now, the decision to proceed is yours to make deliberately: this check surfaces the risk, it "
        f"doesn't take the choice away from you. (A way to formally mark a specific security problem as one "
        f"you've judged safe is arriving in a later step of this module.)"
    )


def findings(block_tier: str = "hard", *, event_path: "str | None" = None,
             repo: "str | None" = None, token: "str | None" = None, client=None) -> list:
    """The review-gate findings for the current pull request, as a list of finding.v1 dicts.

    The vulnerability block is emitted at `block_tier` (the rule tier, `hard`). Every disclosure branch — no
    pull request to compare, the data unavailable on this tier, or a read failure — is a `soft` finding that
    PASSES; never `[]` (a silent green the design forbids) and never `hard`. This function never raises on a
    handled branch (a read failure becomes the soft degraded note), so the script exits 0 and the validator's
    fail-closed hard path is reserved for a genuine crash. `event_path`/`repo`/`token`/`client` are injectable
    for the demo and tests; in production they default to the CI environment and a real client.
    """
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
    for change in changes:
        if not isinstance(change, dict):
            continue
        if change.get("change_type") != "added":            # only what the PR brings IN can block
            continue
        manifest = change.get("manifest") or ""
        if isinstance(manifest, str) and manifest.startswith(_ENGINE_PREFIX):
            continue                                          # the §13 wall: never the engine's own tooling
        if not (change.get("vulnerabilities") or []):
            continue
        location = {"file": manifest, "line": None} if isinstance(manifest, str) and manifest else None
        out.append(validate.finding(block_tier, _vuln_message(change), location))
    return out


def emit_findings() -> int:
    """The no-argument path the validator invokes: print the finding.v1 array and return 0. The vulnerability
    block carries the rule tier (`ENGINE_RULE_TIER`, set by the validator to `hard`); the disclosure branches
    are soft regardless, by design."""
    print(json.dumps(findings(os.environ.get("ENGINE_RULE_TIER", "hard"))))
    return 0


def demo() -> int:
    """Prove the gate blocks a vulnerable product dependency, passes a clean one, walls off the engine's own
    `.engine/` tooling (the §13 wall), reports only the ADDED side of a version bump, and DISCLOSES — never
    silent-greens — an unavailable tier, a transient read failure, and a non-pull-request run. RETURNS
    NON-ZERO if any invariant is broken (the falsification can fail). No network: every case runs the real
    `findings()` over a fake transport/client and a throwaway event file, so nothing is ever written."""
    import shutil
    import tempfile

    class _Canned:
        """A stand-in client whose `compare` returns a canned change list or raises a canned exception."""
        def __init__(self, outcome):
            self._outcome = outcome

        def compare(self, base, head):
            if isinstance(self._outcome, Exception):
                raise self._outcome
            return self._outcome

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
                "name": "demo-pkg", "version": "1.0.0", "vulnerabilities": [adv]}
        engine_vuln = dict(vuln, manifest=".engine/pyproject.toml")     # same vuln, the engine's tooling
        clean = {"change_type": "added", "manifest": "package.json", "name": "ok", "version": "1.0.0",
                 "vulnerabilities": []}
        removed_old = {"change_type": "removed", "manifest": "package.json", "name": "demo-pkg",
                       "version": "0.9.0", "vulnerabilities": [adv]}   # the base side of a version bump

        cases = [
            ("a vulnerable added product dependency earns one hard block", _Canned([vuln]),
             lambda fs: len(fs) == 1 and fs[0]["severity"] == "hard" and "GHSA-demo-0000" in fs[0]["message"]),
            ("the same vulnerability under .engine/ is walled off (the §13 wall)", _Canned([engine_vuln]),
             lambda fs: fs == []),
            ("a clean added dependency passes", _Canned([clean]),
             lambda fs: fs == []),
            ("a version bump reports only the added side, once", _Canned([removed_old, vuln]),
             lambda fs: len(fs) == 1 and fs[0]["severity"] == "hard"),
            ("an unavailable tier (403) discloses the cost/benefit and passes (soft)", _Canned(_Unavailable()),
             lambda fs: len(fs) == 1 and fs[0]["severity"] == "soft" and "GitHub Code Security" in fs[0]["message"]),
            ("a transient read failure discloses a soft note and passes", _Canned(DegradedReadError("boom")),
             lambda fs: len(fs) == 1 and fs[0]["severity"] == "soft" and "temporary" in fs[0]["message"].lower()),
        ]

        failures = []
        for label, client, ok in cases:
            result = findings("hard", event_path=event, repo="o/r", token="t", client=client)
            # each case's `ok` predicate fully pins the expected severity, so a disclosure that wrongly went
            # hard (or a vuln that wrongly went soft) fails its own check — no label-coupled special-casing.
            if not ok(result):
                failures.append(f"{label}: invariant broken, got {result}")

        # the no-pull-request branch: a missing event short-circuits to one soft no-op WITHOUT touching the client
        no_ctx = findings("hard", event_path=missing_event, repo="o/r", token="t",
                          client=_Canned(RuntimeError("the client must not be used when there's no PR")))
        if not (len(no_ctx) == 1 and no_ctx[0]["severity"] == "soft" and "nothing for it to review" in no_ctx[0]["message"]):
            failures.append(f"no-pull-request should disclose one soft no-op, got {no_ctx}")

        if failures:
            print("DEMO FAILED — the dependency-review gate broke an invariant:")
            for f in failures:
                print(f"  - {f}")
            return 1
        print("DEMO PASSED — the review gate blocks a vulnerable product dependency, passes a clean one, "
              "walls off the engine's own tooling, reports only the added side of a version bump, and "
              "discloses (never silent-greens) an unavailable tier, a transient read failure, and a non-PR run.")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return demo()
    return emit_findings()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
