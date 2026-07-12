#!/usr/bin/env python3
"""Cross-fork submission tooling — the external-contribution module's submission operation (Slice 2).

WHAT IT DOES. When the Engine runs inside an operator's FORK of a product repo the operator does NOT own (an
open-source upstream, or the engine-mechanic building engine-template), this prepares and opens a product-only
contribution to that upstream as a cross-fork pull request (`upstream ← fork:feature`). It is the live caller
that finally exercises Slice 1's dormant upstream-clean nudge against a real outgoing diff, and it follows the
host project's pull-request conventions rather than imposing the Engine's own.

THE HUMAN GATE (the irreversible act is never the engine's alone). Opening a pull request on a repo the
operator does not own is irreversible and outward-facing (it notifies maintainers, creates a public record
under the operator's fork identity). So `submit()` PREPARES everything — the outgoing diff, the engine-clean
check, the body to the host's template — and stops. It opens the pull request ONLY when handed an explicit
affirmative decision (`confirm=True`). Without it, the prepared submission is returned for the operator to
approve. This is the §6/§13 posture: the engine reads-and-proposes; the human authorizes the outward act.

KEEPING THE CONTRIBUTION CLEAN (an operator-DECIDABLE nudge, §6 — "not a hard gate"). Before any submit, the
outgoing diff is intersected with the file-precise engine-owned path set (Slice 1's predicate). If an
engine-owned path is about to ride upstream, `submit()` PAUSES and surfaces it as a decision — it narrates the
leak, emits a telemetry finding (the design's "emits a telemetry finding when it fires"), and returns
`leak-decision-needed` rather than opening the pull request. The operator may clear the files (recommended) or
tell the engine to proceed anyway (`proceed_despite_leak=True`), which still passes through the ordinary human
`confirm` gate — a leaked engine file is a §6 hygiene failure, "never a bare block" (external-contribution
README), not a §15 guardrail weakening. Two guards keep it honest: the intersection runs over the UNCAPPED
outgoing diff (a large accidental leak can never sort past a cap and slip through), and a foundation-named path
the upstream PRODUCT legitimately owns (its OWN CLAUDE.md / CODEOWNERS — content differs from the engine's) is
disambiguated OUT by content provenance, while a genuine engine back-merge (content IS the engine's) still
fires. Telemetry-on-fire is emitted whichever way the operator decides, so a knowingly-carried leak still
leaves a durable trace. The upstream's own review is the backstop.

DEGRADATION (never stranded). If the upstream is unreachable when opening the pull request, nothing is lost:
the work is committed on the operator's own fork (a working fork they fully own). The stalled submission is
DRAFTED (the engine drafts, the operator files via their own `gh`) and best-effort tracked via telemetry.

UN-EXERCISED AT v1 (R14, disclosed). Every boundary is injectable — the git diff reader (`run`), the
engine-owned set (`owned`), the `gh` transport (`gh_run`), and the telemetry GitHub boundary (`github`) — so
the whole deterministic surface (diff, clean-check, template detection, body assembly, degradation) is proven
fully offline by `test_submit.py` and the falsifiable `demo`. The ONE step not exercised end-to-end at v1 is
the real `gh pr create` firing against a live upstream — it runs behind `gh_run` for the first time only when
an operator actually submits. The honest line: the machinery is tested; the live network submission is not.

CONTRACT. This is an operation tool, not a `custom/script` check — it is invoked by the engine/operator (and
narrated by the `external-contribution-submit` runbook), never by the validator. `demo` runs a falsifiable
self-check and prints the real operator-facing narration.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

# Make the package dir importable (sibling `upstream_clean_check`) and the parent `.engine/tools/` importable
# (`validate`, `module_coherence`, `telemetry`, `issue_author`) — the dependency_discipline / projects_sync
# idiom, whether imported as `external_contribution.submit` or run directly as a script.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
for _p in (_HERE, _PARENT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import upstream_clean_check  # noqa: E402 — the Slice-1 predicate, reused unchanged
import validate  # noqa: E402 — validate.ROOT (the live tree root) for template detection
import module_coherence  # noqa: E402 — engine_owned_paths: the file-precise engine-owned set
import telemetry  # noqa: E402 — promote_finding (telemetry-on-fire), utc_now, GitHubIssues, severities
import issue_author  # noqa: E402 — render_engine_issue_body (the degradation draft)


# ---- git transport (read-only; mirrors work_record._run_git) ----------------------------------

def _run_git(args: list) -> str | None:
    """Run a local read-only git command; stripped stdout, or None on any failure (missing binary, not a repo,
    non-zero exit, timeout). Never raises — the flow degrades rather than crashing.

    LOAD-BEARING contract: `None` means "could not run / failed", and `""` (empty string) means "ran cleanly,
    no output". `outgoing_diff_status` relies on this distinction to tell an *uninspected* diff (git failed)
    apart from a genuinely *clean* one (git ran, no changed paths) — collapsing the two would re-open the
    fail-open-AND-flag hole where an unread diff is narrated as clean. A refactor must preserve it."""
    try:
        out = subprocess.run(["git", *args], capture_output=True, text=True, timeout=10, check=False)
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:  # noqa: BLE001 — missing git / OS error / timeout all degrade to "unavailable"
        return None


def outgoing_diff_status(base: str, *, run=_run_git) -> tuple[list, bool]:
    """The outgoing contribution's changed paths AND whether the diff was actually inspected.

    Returns `(paths, inspected)`:
      - git FAILED (`run` returns None)     -> `([], False)` — UNINSPECTED; the diff is unknown, not clean.
      - git ran, diff empty (`run` == "")   -> `([], True)`  — inspected and genuinely clean.
      - git ran, has paths                  -> `(sorted set, True)`.

    The paths are the COMMITTED diff of the current branch against the upstream's default branch —
    `git diff --name-only <base>...HEAD` (three-dot: against the merge-base, "what this branch adds"). `base`
    is the upstream's default ref (e.g. `upstream/main`), injectable through `run` for offline tests.

    DELIBERATELY UNCAPPED. Unlike `work_record.changed_paths` (which caps at 50 for orientation), this feeds a
    safety check: a cap could let a leaked engine path sort past it and slip through the intersection. The
    `inspected` flag is the fail-open-AND-flag guard (D-016-family / hooks fail-open-and-flag): the clean
    check still fails open to `[]`, but a caller must never narrate cleanliness on an uninspected diff."""
    out = run(["diff", "--name-only", f"{base}...HEAD"])
    if out is None:            # git failed — NOT inspected (distinct from a clean, empty diff)
        return [], False
    return sorted({p for p in out.splitlines() if p}), True


def outgoing_diff(base: str, *, run=_run_git) -> list:
    """The changed-path list only (fail-open: `[]` on either a git failure or a clean diff). A thin wrapper
    over `outgoing_diff_status` for the callers that only need the leak-check intersection; the submission
    flow uses the status form so it can refuse to narrate cleanliness on an uninspected diff."""
    return outgoing_diff_status(base, run=run)[0]


# ---- the engine-clean check (the live caller of Slice 1's predicate) --------------------------

def _resolve_owned(owned):
    """The engine-owned set: the injected one, or the real file-precise set (CODEOWNERS' source of truth)."""
    if owned is not None:
        return owned
    return module_coherence.engine_owned_paths(module_coherence.discover_manifests())


def clean_findings(base: str, *, run=_run_git, owned=None) -> list:
    """The upstream-clean findings for the outgoing contribution: Slice 1's predicate run against the
    cross-fork outgoing diff. Empty list = clean. `owned` defaults to the real engine-owned set; inject it
    (with `run`) to keep tests and the demo fully offline."""
    changed = outgoing_diff(base, run=run)
    return upstream_clean_check.findings("soft", changed=changed, owned=_resolve_owned(owned))


# ---- content-provenance disambiguation of foundation-named paths (F2 / plan-gate B1) ----------
# The engine-owned predicate is a file-precise NAME set (topology README §"The engine/product wall"). That is
# exact INSIDE a fork, but a few foundation members live OUTSIDE .engine/ — the root CLAUDE.md and the .github/
# control-plane files — in containers an upstream PRODUCT co-occupies. So an upstream that keeps its OWN
# CLAUDE.md / CODEOWNERS would, on a name-only intersection, have its own file misread as an engine leak. The
# real leak the nudge must still catch is "a back-merge of the fork's engine branch" (external-contribution
# README) — which puts the ENGINE's content at that path. So the two are told apart by CONTENT, not presence:
# the contributed version IS the engine's own copy -> a leak; it differs -> the product's own file. A product
# never carries .engine/… files, so only the non-.engine foundation members are ambiguous; the set is derived
# from the single FOUNDATION_INFRA source so it cannot drift.

def _ambiguous_foundation() -> set:
    """The foundation infrastructure paths that live outside .engine/ — the only ones name-ambiguous against a
    foreign product's own tree (a product never carries .engine/… files)."""
    return set(module_coherence.foundation_infra_paths()) - module_coherence.NAMED_INFRA


def _read_engine_file(path: str, *, root: str) -> str | None:
    """The engine's OWN copy of `path` from its live tree (the fork the engine runs in), or None if unreadable."""
    try:
        with open(os.path.join(root, path), encoding="utf-8") as fh:
            return fh.read()
    except Exception:  # noqa: BLE001 — cannot read the engine's own copy -> the caller fails SAFE (flags)
        return None


def _engine_introduced(path: str, *, root: str, run=_run_git) -> bool:
    """True iff the outgoing branch's version of a foundation-named `path` IS the engine's own copy (a leak —
    e.g. a back-merge of the fork's engine branch), False iff its content differs (the upstream product's own
    same-named file, legitimately carried). FAIL-SAFE: any inconclusive read returns True (flag it) — a false
    nudge is a heads-up the operator can wave past, a missed engine leak is not."""
    contributed = run(["show", f"HEAD:{path}"])
    if contributed is None:                       # could not read the contributed content -> flag (fail-safe)
        return True
    engine_own = _read_engine_file(path, root=root)
    if engine_own is None:                        # could not read the engine's own copy -> flag (fail-safe)
        return True
    return contributed.strip() == engine_own.strip()


def _disambiguate_foundation(changed: list, *, root: str, run=_run_git) -> list:
    """Drop from `changed` the ambiguous foundation-named paths that carry the PRODUCT's own content (not the
    engine's) — content provenance, so a product's own CLAUDE.md / CODEOWNERS is not misread as an engine leak
    while a genuine engine back-merge still fires. .engine/… paths and non-foundation paths pass through."""
    ambiguous = _ambiguous_foundation()
    return [p for p in changed
            if p not in ambiguous or _engine_introduced(p, root=root, run=run)]


# ---- upstream pull-request template detection (follow the host's conventions) ------------------

# The standard committed locations a GitHub project keeps its pull-request template (the contributor adapts to
# the host's form — §13). Read from the checkout; no API. The first present file wins.
_PR_TEMPLATE_LOCATIONS = (
    os.path.join(".github", "pull_request_template.md"),
    os.path.join(".github", "PULL_REQUEST_TEMPLATE.md"),
    os.path.join("docs", "pull_request_template.md"),
    os.path.join("docs", "PULL_REQUEST_TEMPLATE.md"),
    "pull_request_template.md",
    "PULL_REQUEST_TEMPLATE.md",
)
# GitHub also allows a directory of named templates; its first entry (alphabetical) is the convention.
_PR_TEMPLATE_DIRS = (
    os.path.join(".github", "PULL_REQUEST_TEMPLATE"),
    os.path.join("docs", "PULL_REQUEST_TEMPLATE"),
)
_CONTRIBUTING_LOCATIONS = (
    "CONTRIBUTING.md",
    os.path.join(".github", "CONTRIBUTING.md"),
    os.path.join("docs", "CONTRIBUTING.md"),
)


def _read(root: str, rel: str) -> str | None:
    path = os.path.join(root, rel)
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return None
    return None


def detect_upstream_pr_template(root: str | None = None) -> str | None:
    """The upstream's own pull-request template text, read from the checkout, or None when the project has
    none. Scans the standard committed locations (and a template directory's first `.md`). `root` defaults to
    the live tree; inject a temp root for offline tests (the instantiator tuple-scan idiom)."""
    root = validate.ROOT if root is None else root
    for rel in _PR_TEMPLATE_LOCATIONS:
        text = _read(root, rel)
        if text is not None:
            return text
    for d in _PR_TEMPLATE_DIRS:
        abs_d = os.path.join(root, d)
        if os.path.isdir(abs_d):
            for name in sorted(os.listdir(abs_d)):
                if name.lower().endswith(".md"):
                    text = _read(root, os.path.join(d, name))
                    if text is not None:
                        return text
    return None


def detect_contributing(root: str | None = None) -> str | None:
    """The relative path of the upstream's CONTRIBUTING file, or None. The Engine honors it (DCO/CLA live
    there) — surfaced to the operator, never auto-interpreted."""
    root = validate.ROOT if root is None else root
    for rel in _CONTRIBUTING_LOCATIONS:
        if os.path.isfile(os.path.join(root, rel)):
            return rel
    return None


# The Engine's own contribution body shape — the FALLBACK used only when the upstream has no template. These
# are plain, contribution-appropriate sections; this is a SHAPE, never the owner-repo `pr-body-completeness`
# hard gate (that contract governs the operator's own repo, never a contribution to someone else's — §13).
_FALLBACK_SECTIONS = ("Summary", "What changed", "Why", "How it was checked")


def build_pr_body(*, summary: str, template_text: str | None = None) -> str:
    """The cross-fork pull-request body. When the upstream has a template, follow the host's form: lead with a
    plain one-line summary, then carry the upstream's template for completion. When it has none, fall back to
    the Engine's own contribution shape (the `_FALLBACK_SECTIONS`). Never invokes the owner-repo completeness
    check."""
    summary = summary.strip()
    if template_text is not None:
        return f"{summary}\n\n{template_text}"
    parts = [f"## {_FALLBACK_SECTIONS[0]}\n\n{summary}\n"]
    for section in _FALLBACK_SECTIONS[1:]:
        parts.append(f"## {section}\n\n")
    return "\n".join(parts)


# ---- operator-facing narration (peer voice; build-spec leaf, gated by review) ------------------

def _submitted_narration(upstream_repo: str) -> str:
    """Submitted-is-not-accepted — narrated at the moment of submission. Hedged for the ungoverned-upstream
    case: it never categorically asserts a review will happen (the standalone ungoverned-upstream honesty
    policy is Slice 3)."""
    return (
        f"I've opened the pull request to {upstream_repo}. Submitting it isn't the same as it being "
        "accepted — if the project reviews contributions, its maintainers decide whether it lands, and that "
        "can take a while or be declined; either outcome is normal. Your fork already has all of this work, "
        "so if it's declined you keep it and can revise or resubmit."
    )


def _repo_from_pr_url(pr_url: str) -> str:
    """The 'owner/name' slug parsed from a GitHub pull-request url, or a plain fallback if it doesn't parse."""
    m = re.search(r"github\.com/([^/]+/[^/]+)/pull/\d+", pr_url or "")
    return m.group(1) if m else "the project"


def _status_narration(upstream_repo: str, pr_url: str, state: str) -> str:
    """Where a submission stands — restated on EVERY status check (submitted-is-not-accepted, never parked in a
    doc; external-contribution README "narrated ... on each status check"). Reports the live state honestly,
    keeps the your-fork-always-has-it reassurance, and — when the state can't be read — says so rather than
    inventing progress. Reports only open/merged/declined; never the raw review-decision or a reviewer's name."""
    if state == "merged":
        return (f"Your contribution to {upstream_repo} landed — it was merged. (Your fork always had the work "
                f"too.)\n{pr_url}")
    if state == "declined":
        return (f"Your contribution to {upstream_repo} was declined — the maintainers closed it without "
                "merging. Nothing is lost: your fork still has all of the work, so you can revise it and "
                f"resubmit, or leave it as is.\n{pr_url}")
    if state == "open":
        return (f"Your contribution to {upstream_repo} is still open — it's a proposal, and the project's "
                "maintainers decide whether it lands; that can take a while, or be declined, either of which "
                f"is normal. Your fork already has the work regardless.\n{pr_url}")
    return (f"I couldn't reach {upstream_repo} just now to check where your contribution stands, so I can't "
            "tell you its current state — this is usually a temporary hiccup, try again in a bit. What hasn't "
            f"changed: your fork has all of the work, and submitting it isn't the same as it being accepted.\n"
            f"{pr_url}")


def _prepared_narration(upstream_repo: str, head: str, base: str) -> str:
    return (
        f"I've prepared the contribution to {upstream_repo} ({head} → {base}): the changes carry no engine "
        "files and the pull-request text is ready. I won't open it until you say so — opening a pull request "
        "on a project you don't own is your call. Say the word and I'll submit it."
    )


def _unverified_narration(upstream_repo: str) -> str:
    """The pause narration when the outgoing diff could not be inspected. It refuses to assert cleanliness on
    an unread diff (the fail-open-AND-flag honesty rule) and holds the one-way outward act rather than open a
    pull request on a project the operator doesn't own with the contents unchecked."""
    return (
        f"I couldn't read what this contribution would carry to {upstream_repo} — git didn't answer, so I "
        "can't check whether any of the Engine's own files would ride along. I won't tell you it's clean when "
        "I couldn't look, and I won't open a pull request on a project you don't own on a change I couldn't "
        "check. This is usually a temporary git hiccup; sort that out and tell me, and I'll re-check."
    )


def _leak_narration(upstream_repo: str, offending: list) -> str:
    """The submission's own pause narration — distinct from the Slice-1 check message (which is a merge-gate
    nudge that 'never blocks'). Opening a pull request is a one-way outward act, so the submission tool PAUSES
    here and surfaces the leak as a decision rather than send the engine's files along on its own; it names the
    files and both ways forward (clear them, or proceed anyway) in plain words. §6 "never a bare block"."""
    files = ", ".join(offending)
    return (
        f"Before opening the pull request, I checked what it would carry to {upstream_repo} and found files "
        f"that belong to the Engine, not to the project you're contributing to: {files}. The Engine's files "
        "shouldn't ride along into a repository that isn't yours — they've most likely slipped in by accident. "
        "I'd take them off this branch first (your fork keeps its copy, nothing is lost) and then I'll prepare "
        "the contribution again — or, if you're sure, tell me to go ahead anyway and I'll open it as it is. "
        "I've paused rather than send them along on my own, because opening a pull request on a project you "
        "don't own can't be undone — so it's your call."
    )


def _degraded_narration(upstream_repo: str) -> str:
    return (
        f"I couldn't reach {upstream_repo} to open the pull request, so nothing was submitted. Nothing is "
        "lost — all of this work is committed on your fork, which you fully own. I've drafted the submission "
        "so it can be filed once the project is reachable; you can also open it yourself with your own `gh`."
    )


# ---- telemetry: the leak finding (telemetry-on-fire) and the degradation draft ----------------

_UNSET = object()  # sentinel: distinguishes "resolve the real boundary" from "offline (None)"


def _github():
    """The engine-Issue boundary, repo/token from boot's single source (lazy — kept off import for the common
    offline path). None when repo/token are unavailable -> tracking degrades to surfaced-not-tracked, the
    merge wall the backstop (the close.py precedent)."""
    try:
        from boot import repo_slug, gh_token  # lazy: only reached when actually promoting
        repo, token = repo_slug(), gh_token()
    except Exception:  # noqa: BLE001 — any failure obtaining GitHub context -> no durable tracking
        return None
    if not repo or not token:
        return None
    return telemetry.GitHubIssues(repo, token)


def _leak_record(finding: dict, now: str) -> dict:
    """A finding-record.v1 for telemetry's promotion path when the clean-check fires. severity is
    persistent-but-benign — a recurring hygiene catch, surfaced in-session and halted, not a trust weakening.
    `location` is carried verbatim from the finding (built literally upstream, never via validate.loc)."""
    return {
        "source_id": "external-contribution/upstream-clean-leak",
        "severity": telemetry.PERSISTENT_BENIGN,
        "message": finding.get("message"),
        "location": finding.get("location"),
        "first_seen": now,
        "last_seen": now,
    }


def _unverified_record(now: str) -> dict:
    """A finding-record.v1 for when the outgoing diff could not be inspected (git unavailable). Fail-open-AND-
    flag: the submission is held rather than opened on an unread diff, and the failure is promoted so it is
    surfaced, never silent. Persistent-but-benign — a recurring local-tooling hiccup, not a trust weakening;
    no `location` (nothing to point at — the diff itself is what could not be read)."""
    return {
        "source_id": "external-contribution/unverified-diff",
        # This message is operator-facing: telemetry.issue_title uses its first sentence verbatim and
        # issue_body embeds it. Keep it plain-language (no "diff"/backstage vocabulary), matching the
        # in-session narration and the sibling stalled-submission record.
        "severity": telemetry.PERSISTENT_BENIGN,
        "message": "Couldn't check what a contribution would carry before opening it, because git wasn't "
                   "available. The submission was held rather than opened without that check — nothing was "
                   "sent.",
        "location": None,
        "first_seen": now,
        "last_seen": now,
    }


def _promote(record: dict, now: str, *, github=_UNSET):
    """Best-effort durable tracking of one finding. Returns the Issue number on success, or False when GitHub
    is unavailable (the concern was already surfaced in-session). `github` is injectable for the demo/tests
    (passing None = OFFLINE; omitting it resolves the real boundary)."""
    gh = _github() if github is _UNSET else github
    if gh is None:
        return False
    return telemetry.promote_finding(gh, record, now)


def _degradation_draft(upstream_repo: str, head: str, base: str, url_hint: str | None = None) -> str:
    """The operator-facing draft of a stalled submission — the engine drafts, the operator files. Assembled
    through the one engine-Issue body contract so it reads like every engine-authored item."""
    references = [("Your fork's branch", url_hint)] if url_hint else None
    return issue_author.render_engine_issue_body(
        what_this_is=(
            f"A contribution to {upstream_repo} is ready but couldn't be submitted — the project wasn't "
            "reachable when the engine tried to open the pull request.\n\n"
            f"- **The change:** product-only commits on `{head}`, to be opened against `{base}`.\n"
            "- **Where it is:** committed and safe on your own fork — nothing was lost."
        ),
        whats_next=(
            "When the project is reachable again, the pull request can be opened.\n\n"
            "- The engine can retry the submission on your say-so.\n"
            "- Or you can open it yourself with your own `gh`, from your fork's branch.\n"
            "- A decline or a delay changes nothing about your fork — the work stays yours."
        ),
        references=references,
    )


# ---- the submission orchestration -------------------------------------------------------------

def _run_gh(args: list):
    """Run a `gh` command. Returns (returncode, stdout, stderr). Never raises — a missing/failed `gh`
    degrades to a non-zero return so the caller takes the degradation path. This is the one boundary not
    exercised end-to-end at v1 (R14): the real `gh pr create` runs here for the first time only on a live
    submission; tests and the demo inject a fake `gh_run`."""
    try:
        out = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60, check=False)
        return out.returncode, out.stdout.strip(), out.stderr.strip()
    except Exception as exc:  # noqa: BLE001 — missing gh / OS error / timeout -> degrade
        return 1, "", str(exc)


def submit(*, upstream_repo: str, base: str, head: str, title: str, summary: str,
           run=_run_git, root=None, owned=None, gh_run=None, github=_UNSET,
           confirm: bool = False, proceed_despite_leak: bool = False, now: str | None = None) -> dict:
    """Prepare (and, on an explicit affirmative decision, open) a cross-fork contribution pull request.

    Returns a result dict whose `status` is one of:
      - `"unverified-diff"`     — the outgoing diff could NOT be inspected (git unavailable); STOPPED before
        submitting. Refuses to narrate cleanliness on an unread diff. Carries the plain-language `narration`
        and `promoted` (the fail-open-AND-flag telemetry trace).
      - `"leak-decision-needed"` — the outgoing diff carries engine-owned files; PAUSED and surfaced as an
        operator decision (§6 "not a hard gate"), not a terminal halt. Carries the findings, the plain-language
        `narration`, and `promoted` (the telemetry-on-fire result). The operator clears the files, or re-calls
        with `proceed_despite_leak=True` to carry on to the ordinary `confirm` gate.
      - `"prepared"`       — clean (or leak-acknowledged), but no affirmative decision yet; the pull request is
        NOT opened. Carries the assembled `pr` (repo/base/head/title/body) the engine WOULD open and the
        prepared `narration`.
      - `"submitted"`      — clean (or leak-acknowledged) and `confirm=True`; the pull request was opened.
        Carries its `url` and the submitted-is-not-accepted `narration`.
      - `"degraded-draft"` — clean and `confirm=True`, but the upstream was unreachable; the submission is
        DRAFTED for the operator to file. Carries `draft` (the issue body), `promoted`, and the `narration`.

    Every boundary is injectable for offline proof: `run` (git diff / content read), `root` (template detection
    AND the engine's own tree for content provenance), `owned` (engine-owned set), `gh_run` (the gh transport),
    `github` (telemetry boundary). Two independent decisions gate the outward act: `proceed_despite_leak`
    acknowledges a hygiene leak, and `confirm` authorizes opening the pull request — the real `gh pr create` is
    reached only when `confirm=True` (and never while a leak is unacknowledged).
    """
    now = now or telemetry.utc_now()
    root = root if root is not None else validate.ROOT

    # 0. Refuse to assert cleanliness on an UNINSPECTED diff (fail-open-AND-flag). A git failure yields
    #    changed=[] just like a clean diff, so without this an unread diff would narrate "carries no engine
    #    files" and open a one-way pull request on an unchecked change. Hold, and promote the failure.
    changed, inspected = outgoing_diff_status(base, run=run)
    if not inspected:
        promoted = _promote(_unverified_record(now), now, github=github)
        return {
            "status": "unverified-diff",
            "promoted": promoted,
            "narration": _unverified_narration(upstream_repo),
        }

    # 1. Keep the contribution clean — an operator-DECIDABLE nudge (§6 "not a hard gate"), over the uncapped
    #    outgoing diff. Disambiguate foundation-named paths by CONTENT provenance FIRST, so one disambiguated
    #    set feeds findings, the decision, `offending`, and the telemetry record together — a product's own
    #    CLAUDE.md / CODEOWNERS is dropped, while a genuine engine back-merge (its content) still fires.
    owned_resolved = _resolve_owned(owned)
    changed = _disambiguate_foundation(changed, root=root, run=run)
    findings = upstream_clean_check.findings("soft", changed=changed, owned=owned_resolved)
    if findings:
        owned_set = set(owned_resolved)
        offending = [p for p in changed if p in owned_set]
        # Telemetry-on-fire fires WHICHEVER way the operator decides (the design's "emits a telemetry finding
        # when it fires"): a knowingly-carried leak is exactly the event worth a durable trace.
        promoted = _promote(_leak_record(findings[0], now), now, github=github)
        if not proceed_despite_leak:
            # Surface the leak as a DECISION, not a terminal halt: the operator clears the files (recommended),
            # or re-calls with proceed_despite_leak=True — which still meets the ordinary `confirm` gate below.
            return {
                "status": "leak-decision-needed",
                "findings": findings,
                "offending": offending,
                "promoted": promoted,
                "narration": _leak_narration(upstream_repo, offending),
            }
        # proceed_despite_leak: the operator has acknowledged the leak — fall through to the human confirm gate.

    # 2. Follow the host's conventions: build the body to the upstream's template, else the fallback shape.
    template_text = detect_upstream_pr_template(root)
    contributing = detect_contributing(root)
    body = build_pr_body(summary=summary, template_text=template_text)
    pr = {"repo": upstream_repo, "base": base, "head": head, "title": title, "body": body,
          "followed_template": template_text is not None, "contributing": contributing}

    # 3. The human gate: without an affirmative decision, PREPARE only — never open the pull request. (B1)
    if not confirm:
        return {"status": "prepared", "pr": pr,
                "narration": _prepared_narration(upstream_repo, head, base)}

    # 4. Open the pull request (the one un-exercised-at-v1 boundary). Degrade to a draft on any failure.
    gh = gh_run or _run_gh
    try:
        rc, out, err = gh(["pr", "create", "--repo", upstream_repo, "--base", base,
                           "--head", head, "--title", title, "--body", body])
    except Exception as exc:  # noqa: BLE001 — a misbehaving transport degrades like an unreachable upstream
        rc, out, err = 1, "", str(exc)
    if rc == 0 and out:
        return {"status": "submitted", "url": out, "pr": pr,
                "narration": _submitted_narration(upstream_repo)}
    draft = _degradation_draft(upstream_repo, head, base)
    promoted = _promote(
        {"source_id": "external-contribution/stalled-submission",
         "severity": telemetry.PERSISTENT_BENIGN,
         "message": f"A contribution to {upstream_repo} is ready on '{head}' but could not be submitted "
                    f"({err or 'the upstream was unreachable'}). It is drafted for you to file.",
         "location": None, "first_seen": now, "last_seen": now},
        now, github=github)
    return {"status": "degraded-draft", "draft": draft, "promoted": promoted, "error": err,
            "narration": _degraded_narration(upstream_repo)}


def status(*, pr_url: str, gh_run=None) -> dict:
    """Where a submitted contribution stands, ON DEMAND — the live 'status check' half of submitted-is-not-
    accepted (narrated on EACH check, never parked in a doc). Reads the pull request's live state via
    `gh pr view` and narrates it in plain words. Returns {status: open|merged|declined|unknown, pr_url,
    upstream_repo, narration}. `gh_run` is injectable for offline tests; a missing / failed / unparseable `gh`
    degrades to `unknown` + an honest "I couldn't reach it" line — it never invents progress (the policy's
    "when you want to know, you ask the engine, and it answers")."""
    gh = gh_run or _run_gh
    upstream_repo = _repo_from_pr_url(pr_url)
    state = "unknown"
    try:
        rc, out, _err = gh(["pr", "view", pr_url, "--json", "state,merged"])
        if rc == 0 and out:
            data = json.loads(out)
            if data.get("merged"):
                state = "merged"
            else:
                gh_state = (data.get("state") or "").upper()
                state = {"OPEN": "open", "CLOSED": "declined", "MERGED": "merged"}.get(gh_state, "unknown")
    except Exception:  # noqa: BLE001 — any transport / parse failure degrades to 'unknown' + the honest line
        state = "unknown"
    return {"status": state, "pr_url": pr_url, "upstream_repo": upstream_repo,
            "narration": _status_narration(upstream_repo, pr_url, state)}


# ---- falsifiable, offline demo (drives the REAL submit; prints the real operator narration) ----

def demo() -> int:
    """Prove the submission flow over injected boundaries — and PRINT the actual operator-facing narration so
    a reviewer reads the words, not just PASS/FALSE. Cases: git unreadable HOLDS the diff as uninspected
    (never narrated clean, never opened); a leaked engine path halts before submit; a clean diff with no
    decision PREPARES (does not open); a clean diff + decision + a present upstream template SUBMITS and
    follows the host's form; no template falls back to the engine's shape; an unreachable upstream DEGRADES to
    a drafted submission. RETURNS NON-ZERO if any invariant breaks. Fully offline: every boundary is injected
    (git `run`, template `root`, `owned`, `gh_run`, `github`=None), so no git/gh/network runs."""
    import shutil
    import tempfile

    owned = [
        ".engine/check/upstream-clean.json",
        ".engine/tools/external_contribution/submit.py",
        "CLAUDE.md",
    ]
    now = "2026-01-01T00:00:00Z"

    def run_with(paths):
        return lambda args: "\n".join(paths)  # a fake git that returns the given diff regardless of args

    # A temp checkout WITH an upstream PR template, and one WITHOUT.
    root_with = tempfile.mkdtemp(prefix="engine-submit-demo-with-")
    root_without = tempfile.mkdtemp(prefix="engine-submit-demo-without-")
    os.makedirs(os.path.join(root_with, ".github"), exist_ok=True)
    template_marker = "## Description\n<!-- upstream's own template -->\n"
    with open(os.path.join(root_with, ".github", "pull_request_template.md"), "w", encoding="utf-8") as fh:
        fh.write(template_marker)

    recorded = {}

    def gh_ok(args):
        recorded["args"] = args
        return 0, "https://github.com/upstream/project/pull/42", ""

    def gh_fail(args):
        return 1, "", "could not resolve host github.com"

    failures = []
    print("(This is a dry run against a pretend project — no real repository is touched and nothing is "
          "sent. It shows what the engine would say and do at each point.)\n")
    try:
        # Case 0 — git can't be read: the diff is UNINSPECTED, so the flow refuses to narrate cleanliness
        #          and never opens a PR, even with the decision given (confirm=True).
        r0 = submit(upstream_repo="upstream/project", base="upstream/main", head="me:feature",
                    title="Fix the thing", summary="Fixes the thing.",
                    run=lambda args: None,  # a git that fails on every call
                    root=root_without, owned=owned, gh_run=gh_ok, github=None, confirm=True, now=now)
        print("--- git couldn't be read: held, not narrated clean, not opened ---")
        print(r0["narration"], "\n")
        if r0["status"] != "unverified-diff" or "args" in recorded:
            failures.append(f"unverified case: expected unverified-diff and NO pr create, got {r0['status']} "
                            f"/ recorded={recorded}")
        if "carry no engine" in r0["narration"] or "no engine files" in r0["narration"]:
            failures.append("unverified case: narrated cleanliness on an uninspected diff")

        # Case 1 — a leaked engine path PAUSES for a decision (not a terminal halt), fires telemetry-on-fire,
        #          and never opens a PR while the leak is unacknowledged (even with confirm=True).
        leak_diff = run_with(["src/app.py", ".engine/tools/external_contribution/submit.py"])
        r1 = submit(upstream_repo="upstream/project", base="upstream/main", head="me:feature",
                    title="Fix the thing", summary="Fixes the thing.",
                    run=leak_diff,
                    root=root_without, owned=owned, gh_run=gh_ok, github=None, confirm=True, now=now)
        print("--- a leaked engine file: paused for your decision, not opened ---")
        print(r1["narration"], "\n")
        if r1["status"] != "leak-decision-needed" or "args" in recorded:
            failures.append(f"leak case: expected leak-decision-needed and NO pr create, got {r1['status']} "
                            f"/ recorded={recorded}")
        if not any(".engine/tools/external_contribution/submit.py" in f["message"]
                   for f in r1.get("findings", [])):
            failures.append("leak case: the offending engine path was not named in the finding")

        # Case 1b — the operator OVERRIDES the leak (proceed_despite_leak=True): the flow no longer terminates;
        #           it carries on to the ordinary human gate (here confirm=False -> prepared, still not opened).
        r1b = submit(upstream_repo="upstream/project", base="upstream/main", head="me:feature",
                     title="Fix the thing", summary="Fixes the thing.",
                     run=leak_diff,
                     root=root_without, owned=owned, gh_run=gh_ok, github=None,
                     confirm=False, proceed_despite_leak=True, now=now)
        if r1b["status"] != "prepared" or "args" in recorded:
            failures.append(f"override case: expected the leak to be operator-decidable (prepared), got "
                            f"{r1b['status']} / recorded={recorded}")

        # Case 2 — a clean diff with NO decision PREPARES; it must NOT open a pull request.
        r2 = submit(upstream_repo="upstream/project", base="upstream/main", head="me:feature",
                    title="Fix the thing", summary="Fixes the thing.",
                    run=run_with(["src/app.py", "README.md"]),
                    root=root_with, owned=owned, gh_run=gh_ok, github=None, confirm=False, now=now)
        print("--- clean, but not yet authorized: prepared, not opened ---")
        print(r2["narration"], "\n")
        if r2["status"] != "prepared" or "args" in recorded:
            failures.append(f"prepared case: expected prepared and NO pr create, got {r2['status']}")
        if not r2["pr"]["followed_template"] or template_marker not in r2["pr"]["body"]:
            failures.append("prepared case: the body did not follow the upstream's template")

        # Case 3 — clean + decision + a present upstream template: SUBMITS, follows the host's form.
        r3 = submit(upstream_repo="upstream/project", base="upstream/main", head="me:feature",
                    title="Fix the thing", summary="Fixes the thing.",
                    run=run_with(["src/app.py", "README.md"]),
                    root=root_with, owned=owned, gh_run=gh_ok, github=None, confirm=True, now=now)
        print("--- clean and authorized: opened, with submitted-is-not-accepted ---")
        print(r3["narration"], "\n")
        if r3["status"] != "submitted" or r3.get("url") != "https://github.com/upstream/project/pull/42":
            failures.append(f"submit case: expected submitted with a url, got {r3['status']} / {r3.get('url')}")
        if recorded.get("args", [])[:2] != ["pr", "create"]:
            failures.append("submit case: gh pr create was not invoked with the expected verb")

        # Case 4 — clean + decision but NO upstream template: falls back to the engine's own shape.
        recorded.clear()
        r4 = submit(upstream_repo="upstream/project", base="upstream/main", head="me:feature",
                    title="Fix the thing", summary="Fixes the thing.",
                    run=run_with(["src/app.py"]),
                    root=root_without, owned=owned, gh_run=gh_ok, github=None, confirm=True, now=now)
        if r4["status"] != "submitted" or r4["pr"]["followed_template"]:
            failures.append(f"fallback case: expected submitted with the fallback shape, got {r4['status']}")
        if "## Summary" not in r4["pr"]["body"] or "## How it was checked" not in r4["pr"]["body"]:
            failures.append("fallback case: the engine's fallback section shape was not used")

        # Case 5 — clean + decision but the upstream is unreachable: degrades to a drafted submission.
        r5 = submit(upstream_repo="upstream/project", base="upstream/main", head="me:feature",
                    title="Fix the thing", summary="Fixes the thing.",
                    run=run_with(["src/app.py"]),
                    root=root_without, owned=owned, gh_run=gh_fail, github=None, confirm=True, now=now)
        print("--- the upstream was unreachable: degraded to a drafted submission ---")
        print(r5["narration"], "\n")
        if r5["status"] != "degraded-draft" or not r5.get("draft"):
            failures.append(f"degrade case: expected degraded-draft with a draft, got {r5['status']}")
        if "engine opened this item itself" not in r5["draft"]:
            failures.append("degrade case: the draft was not assembled through the engine-Issue body contract")

        # Case 6 — content provenance on a foundation-named path (root CLAUDE.md). A temp tree stands in for the
        #          engine's own copy; a fake git returns the CONTRIBUTED version for `show HEAD:CLAUDE.md`.
        root_prov = tempfile.mkdtemp(prefix="engine-submit-demo-prov-")
        engine_claude = "# The Engine's own floor\nthis is the engine's own CLAUDE.md content\n"
        with open(os.path.join(root_prov, "CLAUDE.md"), "w", encoding="utf-8") as fh:
            fh.write(engine_claude)

        def run_prov(contributed_claude):
            def _r(args):
                if args and args[0] == "diff":
                    return "CLAUDE.md\nsrc/app.py"
                if args and args[0] == "show":               # ["show", "HEAD:CLAUDE.md"]
                    return contributed_claude
                return ""
            return _r

        # 6a — the PRODUCT's own CLAUDE.md (content differs from the engine's) is NOT misread as a leak.
        r6a = submit(upstream_repo="upstream/project", base="upstream/main", head="me:feature",
                     title="Fix", summary="Fixes.",
                     run=run_prov("# The product's own CLAUDE.md\ncompletely different content\n"),
                     root=root_prov, owned=owned, gh_run=gh_ok, github=None, confirm=False, now=now)
        print("--- the upstream product's OWN CLAUDE.md: not misread as an engine leak (clean) ---")
        print(r6a["narration"], "\n")
        if r6a["status"] != "prepared":
            failures.append(f"provenance case: a product's own CLAUDE.md was misread as an engine leak, got "
                            f"{r6a['status']}")
        # 6b — a genuine engine back-merge (contributed content IS the engine's own) STILL fires.
        r6b = submit(upstream_repo="upstream/project", base="upstream/main", head="me:feature",
                     title="Fix", summary="Fixes.",
                     run=run_prov(engine_claude),
                     root=root_prov, owned=owned, gh_run=gh_ok, github=None, confirm=False, now=now)
        if r6b["status"] != "leak-decision-needed" or "CLAUDE.md" not in r6b.get("offending", []):
            failures.append(f"provenance case: an engine CLAUDE.md back-merge did NOT fire, got {r6b['status']}"
                            f" / offending={r6b.get('offending')}")
        shutil.rmtree(root_prov, ignore_errors=True)

        # Case 7 — the status verb: where a submission stands, restating submitted-is-not-accepted every time,
        #          and degrading honestly when the state can't be read.
        pr_url = "https://github.com/upstream/project/pull/42"
        s_open = status(pr_url=pr_url, gh_run=lambda args: (0, '{"state":"OPEN","merged":false}', ""))
        print("--- checking where a submission stands (still open) ---")
        print(s_open["narration"], "\n")
        if s_open["status"] != "open" or "still open" not in s_open["narration"]:
            failures.append(f"status case: expected open, got {s_open['status']}")
        s_merged = status(pr_url=pr_url, gh_run=lambda args: (0, '{"state":"MERGED","merged":true}', ""))
        if s_merged["status"] != "merged" or "landed" not in s_merged["narration"]:
            failures.append(f"status case: expected merged, got {s_merged['status']}")
        s_unknown = status(pr_url=pr_url, gh_run=lambda args: (1, "", "could not resolve host github.com"))
        if s_unknown["status"] != "unknown" or "couldn't reach" not in s_unknown["narration"]:
            failures.append(f"status case: expected an honest unknown, got {s_unknown['status']}")
    finally:
        shutil.rmtree(root_with, ignore_errors=True)
        shutil.rmtree(root_without, ignore_errors=True)

    if failures:
        print("DEMO FAILED — the cross-fork submission broke an invariant:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("DEMO PASSED — an unreadable diff is held rather than narrated clean or opened; a leaked engine file "
          "pauses for your decision (clear it, or proceed anyway) rather than a bare halt; the upstream "
          "product's own CLAUDE.md is not misread as an engine leak, while a genuine engine back-merge still "
          "fires; a clean contribution is only PREPARED until you authorize it; on your go-ahead it opens "
          "following the host's template (or the engine's fallback shape); an unreachable upstream degrades to "
          "a drafted submission, nothing lost; and the status check tells you honestly where a submission "
          "stands, or that it couldn't be read.")
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return demo()
    if argv and argv[0] == "status":
        if len(argv) < 2:
            print("usage: submit.py status <pull-request-url>")
            return 2
        print(status(pr_url=argv[1])["narration"])
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
