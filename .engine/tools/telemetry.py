#!/usr/bin/env python3
"""Telemetry — the engine's self-monitoring detect->surface machinery (core slice 18).

Answers "is the Engine healthy?" and feeds the remediation loop. It is SELF-SURFACING, NOT
self-healing (D-009 / Risk R3 / principles §8): it detects drift over the Engine's OWN work and
surfaces it for the AI to fix next session — never product quality, never autonomous repair.

The one loop is consume -> dedup -> promote(triage) -> auto-resolve over engine-labelled GitHub
Issues. Triage is the only thing telemetry does autonomously, and it does exactly one kind of
write: open or update one engine-labelled Issue, deduplicated by a SOURCE-keyed stable key (the
rule / surface / stream id that emitted the signal — NEVER per-occurrence material like the file
or run it was seen on). A `trust-critical` signal (a gate or check that could not run) promotes
immediately; a `persistent-but-benign` signal promotes only after it crosses the persistence
threshold; auto-resolve closes a tracked Issue once its signal has been absent for the policy's
observation count. The threshold values are READ from the governed triage-threshold policy
(legible and tunable), never redefined here.

Seams (principles §16 — telemetry OWNS its acting-mechanism; producers only emit):
  - The GitHub boundary is an INJECTABLE transport: tests and the demo fake ONLY the network and
    run the REAL reconcile logic. A read failure RAISES (DegradedReadError) and is NEVER swallowed
    as "no open issues"; the caller then falls back to State's committed offline count.
  - State's debt count/pointer is a derived convenience telemetry refreshes (never authoritative,
    state/README). This tool COMPUTES it and, on explicit request, writes a schema-valid cursor —
    it never auto-commits (the committed cursor advances on committed acts).
  - Telemetry is itself a local gate, so its own crash emits a finding and exits 0 (fail-open).

The engine-domain label string (`engine`) is the build-spec leaf decided with the maintainer
(control-plane §"Engine Issues and the label scheme"). It is homed here as the FIRST producer to
apply it; provisioning (a later slice) owns the general ensure-both-labels step and inherits this
minimal ensure. Consumers (build-orchestration's build Issues, audits) READ this one constant.

Operator demo (faked GitHub, real logic — no real Issues, no token):
  uv run --directory .engine --frozen -- python tools/telemetry.py demo
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402  (sibling tool; reused for finding/frontmatter/effective_policy_values/ROOT)
import issue_author  # noqa: E402  (the shared issue-authoring helper — assembles the body to the control-plane contract)
import standing_situation  # noqa: E402  (the read-only "where we are" derive; telemetry refreshes its offline cache on this same GitHub pass — pure leaf, imports nothing back, so no cycle)

# ---- constants -------------------------------------------------------------

# The engine-domain label (control-plane owns the SCHEME; the STRING is a build-spec leaf decided
# with the maintainer). The single shared home: build-orchestration (build Issues) and audits READ
# this; provisioning (later slice) generalises the ensure-it-exists step and inherits the minimal
# ensure below. Renaming it elsewhere would split the routing substrate, so it lives once, here.
ENGINE_DOMAIN_LABEL = "engine"

# The two self-monitoring severity classes (distinct from the agent and check enums).
TRUST_CRITICAL = "trust-critical"          # could-not-run; promotes immediately
PERSISTENT_BENIGN = "persistent-but-benign"  # recurring low-impact; promotes after persistence

API_ROOT = "https://api.github.com"
USER_AGENT = "engine-telemetry"

# An invisible marker carried in a tracked Issue's body so a later run can recover which signal the
# Issue belongs to even if the local cache was wiped (a fresh clone / new machine). It is an HTML
# comment, so it never renders as visible prose — no backstage vocabulary reaches the operator.
_SENTINEL_TEMPLATE = "<!-- engine-signal: {sid} -->"
_SENTINEL_RE = re.compile(r"<!--\s*engine-signal:\s*(.+?)\s*-->")

DEFAULT_POLICY_PATH = os.path.join(validate.ROOT, ".engine", "policies", "triage-threshold.md")
DEFAULT_STATE_PATH = os.path.join(validate.ROOT, ".engine", "state", "state.json")
DEFAULT_CACHE_PATH = os.path.join(validate.ROOT, ".engine", "telemetry", ".cache", "streams.json")


class DegradedReadError(Exception):
    """Raised when GitHub cannot be read (an outage, or a 401/403/404 auth/scope/permission error).
    It is NEVER swallowed as an empty result — an auth failure that read as "no open issues" would
    silently misreport the engine's health (the failure mode state/README forbids)."""


# ---- time ------------------------------------------------------------------

def utc_now() -> str:
    """The current UTC moment in the trailing-Z shape state.v1 / finding-record.v1 enforce.
    Lives at the IO edge (run/main), never inside the pure reconcile logic, which takes `now`."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- thresholds (read the governed policy; never redefine) -----------------

def load_thresholds(policy_path: str | None = None, override: dict | None = None) -> dict:
    """The effective triage thresholds = the policy's shipped defaults with any operator override
    merged per-key (validate.effective_policy_values). Telemetry's structural_keys is EMPTY — it has
    no structural ordering an override must be barred from retuning (the function's documented
    telemetry case), so a consumer never re-implements the merge."""
    fm = validate.frontmatter(policy_path or DEFAULT_POLICY_PATH)
    default = fm.get("values") or {}
    effective, _findings = validate.effective_policy_values(
        default, override or {}, structural_keys=set(), tier="soft",
        message="Telemetry's triage thresholds are tuning values; an override retunes them, never a law.")
    return effective


# ---- pure helpers (no IO, no clock) ----------------------------------------

def derive_source_key(record: dict) -> str:
    """The dedup key is the signal's source id, verbatim — never per-occurrence material. One tiny
    one-homed function so the 'source-keyed, never per-occurrence' law is stated once and testable."""
    return record["source_id"]


def promotion_due(record: dict, persist_count: int, persistence_threshold: int) -> bool:
    """Severity class sets promotion latency. A trust-critical signal promotes immediately; a
    persistent-but-benign one only once it has persisted across the threshold."""
    if record.get("severity") == TRUST_CRITICAL:
        return True
    return persist_count >= persistence_threshold


def resolution_due(absent_observations: int, auto_resolve_threshold: int) -> bool:
    """Auto-resolve closes a tracked Issue once its signal has been absent for the threshold count of
    observations. It closes only — it repairs nothing."""
    return absent_observations >= auto_resolve_threshold


def triage_pressure_line(open_low_severity_count: int, threshold: int) -> str | None:
    """The triage-pressure stream is render-only: one plain-language line once the count of open
    low-impact engine items crosses the threshold, else nothing. Crossing it promotes NOTHING — the
    meter never becomes an item itself, so it cannot feed the volume it measures."""
    if open_low_severity_count > threshold:
        # The trailing sentence is the reactive retune offer (D-167): the threshold that decides when this
        # reminder fires is itself tunable, so the command is offered at the moment it surfaces. Dormant until
        # the triage-pressure stream renders live (a later slice wires the run); the offer rides the template.
        return ("The engine's self-monitoring backlog is growing — there are several low-priority "
                "engine items open. Nothing here is urgent; you can review them when convenient. "
                "You can also change when this reminder appears — type /engine-tune.")
    return None


def parse_source_id(body: str) -> str | None:
    """Recover a tracked Issue's signal id from the invisible marker in its body (the cache-wipe
    recovery path). Returns None when the marker is absent or was stripped.

    Takes the LAST marker, not the first: _with_tracking_trailers always appends the engine's own
    marker as the final line, so if author-influenced body prose (a finding message or filename)
    contains a forged `<!-- engine-signal: ... -->`, the real trailing marker still wins and dedup
    cannot be hijacked by an earlier forgery."""
    matches = _SENTINEL_RE.findall(body or "")
    return matches[-1] if matches else None


def issue_title(record: dict) -> str:
    """A plain-language Issue title that says, with no backstage vocabulary, that this is about the
    engine's own health. The first sentence of the finding's message carries the specifics."""
    first = re.split(r"(?<=[.!?])\s", record["message"].strip(), maxsplit=1)[0]
    if len(first) > 110:
        first = first[:107].rstrip() + "..."
    return f"Engine health: {first}"


def issue_body(record: dict, first_seen: str, last_seen: str) -> str:
    """The tracked Issue's body — telemetry's plain-language operator contract, assembled through the
    shared issue-authoring helper so every engine-authored Issue carries the one control-plane body
    shape (control-plane §"Engine Issues"; the single issue-authoring path). Telemetry fills the
    contract's parts with its OWN language — what it noticed about the engine's own health (not your
    product) and what, if anything, you must do — and appends two telemetry-specific trailers the
    helper does not own: the first-/last-seen line and the invisible signal marker (an HTML comment
    appended last, recovered by parse_source_id even after a cache wipe). No backstage vocabulary
    (stream / severity class / persistence / triage / source) reaches the operator."""
    what_this_is = (
        "The engine watches the health of *its own* machinery — the tools and checks that help run "
        "your project, and it noticed something it is tracking here so it stays visible. This is not "
        "a problem with your product, and the engine will never open or close an item you created — "
        f"only its own.\n\n**What it noticed.** {record['message']}"
    )
    whats_next = (
        "Usually nothing right now. The engine will propose a fix in a later session under the same "
        "review-and-merge step you already use, and once the cause is gone this item closes itself. "
        "If it lingers and you want it resolved sooner, you can ask for the fix to be prioritised."
    )
    body = issue_author.render_engine_issue_body(what_this_is=what_this_is, whats_next=whats_next)
    return _with_tracking_trailers(body, record["source_id"], first_seen, last_seen)


def _with_tracking_trailers(body_core: str, source_id: str, first_seen: str, last_seen: str) -> str:
    """Append telemetry's two own trailers to an already-rendered issue body: the first-/last-seen
    line and the invisible signal marker (an HTML comment, recovered by parse_source_id even after a
    cache wipe). Telemetry OWNS the marker, which it appends LAST — so even if a caller's body prose
    carries a forged `<!-- engine-signal: ... -->`, parse_source_id (which takes the LAST match) still
    recovers this real one. The producer's contract: the source_id must be marker-safe (no `<!--`/`-->`
    or newline — every producer's source_id is a plain key, and the soft-finding promoter skips any path
    that is not). Shared by issue_body (telemetry's own health framing) and promote_finding's
    pre-rendered-body path (a producer's lane-aware framing), so the trailer/marker shape is stated once."""
    return (
        f"{body_core}\n"
        f"*First noticed {first_seen}; last reconfirmed {last_seen}.*\n\n"
        f"{_SENTINEL_TEMPLATE.format(sid=source_id)}\n"
    )


# ---- the pure heart: consume -> dedup -> promote -> auto-resolve ------------

class Plan:
    """The intended writes a reconcile pass produces, plus the next cache and the open count. No IO —
    the caller applies it (so tests/demo run the real logic over fake data)."""

    def __init__(self):
        self.to_open: list[tuple] = []    # (source_id, title, body)
        self.to_update: list[tuple] = []  # (issue_number, body)
        self.to_close: list[int] = []     # issue_number
        self.next_counts: dict = {}
        self.open_count: int = 0
        self.low_severity_open_count: int = 0


def reconcile(records: list, open_issues: list, counts: dict, thresholds: dict, now: str) -> Plan:
    """The pure loop. Inputs: the finding-records observed THIS run; the currently-open engine Issues
    (each {number, title, body, source_id}); the cross-run cache keyed by source_id
    ({persist, absent, issue, first_seen, severity}); the effective thresholds; and the clock value.
    Returns a Plan. No IO, no clock — deterministic given its inputs.

    Dedup is two-layer: an open Issue is matched to a signal by the source-id marker recovered from
    its body (open_issues[*].source_id), and the cache's remembered issue number is the fast path /
    cross-check. Worst case (marker stripped AND cache wiped) is one duplicate Issue — never a missed
    signal, and self-correcting once the signal next goes absent."""
    persistence = int(thresholds.get("persistence", 0))
    auto_resolve = int(thresholds.get("auto_resolve", 0))
    plan = Plan()

    observed = {derive_source_key(r): r for r in records}
    open_by_sid = {i["source_id"]: i for i in open_issues if i.get("source_id")}
    # cache recovery: a cached issue number for a sid still open but whose marker was stripped
    open_numbers = {i["number"] for i in open_issues}
    for sid, prev in counts.items():
        num = (prev or {}).get("issue")
        if sid not in open_by_sid and num in open_numbers:
            open_by_sid[sid] = next(i for i in open_issues if i["number"] == num)

    # present signals: refresh-or-promote
    for sid, record in observed.items():
        prev = counts.get(sid) or {}
        persist = int(prev.get("persist", 0)) + 1
        first_seen = prev.get("first_seen") or now
        entry = {"persist": persist, "absent": 0, "issue": prev.get("issue"),
                 "first_seen": first_seen, "severity": record.get("severity")}
        existing = open_by_sid.get(sid)
        if existing is not None:
            entry["issue"] = existing["number"]
            plan.to_update.append((existing["number"], issue_body(record, first_seen, now)))
        elif promotion_due(record, persist, persistence):
            entry["issue"] = None  # assigned by the caller once the Issue is created
            plan.to_open.append((sid, issue_title(record), issue_body(record, first_seen, now)))
        plan.next_counts[sid] = entry

    # absent signals on open Issues: count toward auto-resolve, close when due
    for sid, issue in open_by_sid.items():
        if sid in observed:
            continue
        prev = counts.get(sid) or {}
        absent = int(prev.get("absent", 0)) + 1
        if resolution_due(absent, auto_resolve):
            plan.to_close.append(issue["number"])
            # dropped from next_counts — the signal is gone and its Issue is closing
        else:
            plan.next_counts[sid] = {"persist": int(prev.get("persist", 0)), "absent": absent,
                                     "issue": issue["number"], "first_seen": prev.get("first_seen") or now,
                                     "severity": prev.get("severity")}

    opened, closed = len(plan.to_open), len(plan.to_close)
    plan.open_count = len(open_issues) - closed + opened
    # low-severity (benign) open items, for the render-only triage-pressure meter
    will_be_open = {i["number"] for i in open_issues} - set(plan.to_close)
    low = 0
    for sid, entry in plan.next_counts.items():
        sev = entry.get("severity")
        is_open = entry.get("issue") in will_be_open or sid in {s for s, _, _ in plan.to_open}
        if is_open and sev == PERSISTENT_BENIGN:
            low += 1
    plan.low_severity_open_count = low
    return plan


# ---- the GitHub boundary (the only network seam; transport is injectable) ---

class GitHubIssues:
    """The engine-labelled-Issue boundary. Reuses the urllib + GITHUB_TOKEN pattern of the seed
    guards, EXTENDED to writes (POST/PATCH). `transport(method, path, body) -> (status, json)` is
    injectable so tests/demo replace ONLY the network and run the real logic above. Every read raises
    DegradedReadError on failure (never returns [])."""

    def __init__(self, repo: str, token: str, label: str = ENGINE_DOMAIN_LABEL, transport=None):
        self.repo = repo
        self.token = token
        self.label = label
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

    def ensure_label(self) -> None:
        """Idempotently ensure the engine-domain label exists (create it iff absent). The genesis
        construction repo runs no first-run bootstrap, so the first producer ensures its own label;
        provisioning later owns the general, re-runnable ensure and inherits this."""
        status, _ = self._transport("GET", f"/repos/{self.repo}/labels/{self.label}", None)
        if status == 404:
            self._transport("POST", f"/repos/{self.repo}/labels",
                            {"name": self.label, "color": "ededed",  # a calm neutral grey
                             "description": "Opened by the engine about its own health (not your product)."})
        elif status >= 400:
            raise DegradedReadError(f"GitHub returned {status} checking the '{self.label}' label")

    def list_open_engine_issues(self) -> list:
        """Every open Issue carrying the engine-domain label, paginated to exhaustion. RAISES on any
        HTTP error (401/403/404 included) — an auth/scope failure must never read as an empty list."""
        out, page = [], 1
        while True:
            status, data = self._transport(
                "GET",
                f"/repos/{self.repo}/issues?state=open&labels={self.label}&per_page=100&page={page}",
                None)
            if status >= 400 or data is None:
                raise DegradedReadError(f"GitHub returned {status} reading open engine issues")
            for i in data:
                if "pull_request" in i:   # the issues endpoint also lists PRs; skip them
                    continue
                out.append({"number": i["number"], "title": i.get("title", ""),
                            "body": i.get("body") or ""})
            if len(data) < 100:
                break
            page += 1
        for i in out:
            i["source_id"] = parse_source_id(i["body"])
        return out

    def open_issue(self, title: str, body: str) -> dict:
        status, data = self._transport(
            "POST", f"/repos/{self.repo}/issues",
            {"title": title, "body": body, "labels": [self.label]})  # label applied at creation
        if status >= 400 or data is None:
            raise DegradedReadError(f"GitHub returned {status} opening an engine issue")
        return data

    def update_issue(self, number: int, body: str) -> dict:
        status, data = self._transport("PATCH", f"/repos/{self.repo}/issues/{number}", {"body": body})
        if status >= 400:
            raise DegradedReadError(f"GitHub returned {status} updating engine issue #{number}")
        return data or {"number": number}

    def close_issue(self, number: int) -> dict:
        status, data = self._transport("PATCH", f"/repos/{self.repo}/issues/{number}", {"state": "closed"})
        if status >= 400:
            raise DegradedReadError(f"GitHub returned {status} closing engine issue #{number}")
        return data or {"number": number}

    def issues_query_url(self) -> str:
        """The human-citable register: where the live list of open engine items lives."""
        return f"https://github.com/{self.repo}/issues?q=is:open+label:{self.label}"


# ---- the gitignored cache (best-effort; absent reads as empty) -------------

class Cache:
    """The cross-run stream cache (persistence/absence counts keyed by source_id). Gitignored and
    regenerable — a wipe (fresh clone / new machine) simply restarts accrual, which is acceptable
    (best-effort; a trust-critical signal never waits, so it is unaffected)."""

    def __init__(self, path: str = DEFAULT_CACHE_PATH):
        self.path = path

    def load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as fh:
                return json.load(fh)
        except (FileNotFoundError, ValueError, OSError):
            return {}   # absent or unreadable -> empty; best-effort, never a guarantee

    def store(self, counts: dict) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(counts, fh, indent=2, sort_keys=True)
            fh.write("\n")


# ---- State: read the offline count; refresh on explicit request ------------

def read_state_debt(state_path: str = DEFAULT_STATE_PATH):
    """The committed offline (count, as_of) — the degraded fallback. Returns (None, None) if it
    cannot be read, so the caller renders an honest 'unknown' rather than a wrong zero."""
    try:
        with open(state_path, encoding="utf-8") as fh:
            debt = json.load(fh).get("integration_debt") or {}
        return debt.get("open_count"), debt.get("as_of")
    except (FileNotFoundError, ValueError, OSError, AttributeError):
        return None, None


def refresh_state(state_path: str, debt: dict | None = None, standing: dict | None = None) -> None:
    """Refresh the committed cursor's offline-cache fields, schema-valid, preserving the rest. Telemetry is
    the sole writer of state.json. The two cache fields are DISJOINT and each optional: `debt` writes the
    three integration_debt keys; `standing` writes the standing_situation cache (milestone/phase/as_of).
    Passing only one leaves the other untouched, so the standing-situation co-writer never clobbers the debt
    count and vice-versa. EXPLICIT refresh only — telemetry never auto-commits it; the committed cursor
    advances on committed acts and the operator reviews the diff (state/README)."""
    with open(state_path, encoding="utf-8") as fh:
        data = json.load(fh)
    if debt is not None:
        data["integration_debt"] = {"open_count": int(debt["open_count"]),
                                    "as_of": debt["as_of"], "register": debt["register"]}
    if standing is not None:
        data["standing_situation"] = {"milestone": standing.get("milestone"),
                                      "phase": standing.get("phase"),
                                      "as_of": standing.get("as_of")}
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")


def refresh_standing(state_path: str, repo: str, token: str, *, now: str | None = None, transport=None) -> dict:
    """Derive the standing-situation live from GitHub and write ONLY that offline-cache field (with its
    `as_of` stamped) into the committed cursor — the focused offline-floor refresh, the standing-situation
    sibling of refresh_state's debt write. Raises on any read failure (standing_situation.DeriveUnavailable
    for an HTTP-status error, or telemetry's DegradedReadError if the host is unreachable) and writes nothing
    on that failure — the caller decides whether to proceed. `transport` is injectable so tests/demo run
    offline on the real derive + write."""
    gh = GitHubIssues(repo, token, transport=transport)
    derived = standing_situation.derive_standing_situation(gh)
    standing = {"milestone": derived.get("milestone"), "phase": derived.get("phase"),
                "as_of": now or utc_now()}
    refresh_state(state_path, standing=standing)
    return standing


def refresh_cache(state_path: str, repo: str, token: str, *, now: str | None = None, transport=None) -> dict:
    """The scheduled audit-prep workflow's offline-cache refresh: derive BOTH offline-floor fields — the
    integration-debt count and the standing situation — from GitHub in ONE read-only pass and write them
    together as freight (D-198: the standing cache rides the same GitHub pass as the debt count). UNLIKE
    `run`, it makes NO triage writes — it never opens, updates, or closes an Issue, so it can never
    auto-close the open Issues a `run([])` would — it only counts open items and reads the milestone/phase.
    Each field degrades INDEPENDENTLY: a debt-read failure or a standing-derive failure leaves that one field's
    prior cached value untouched (never clobbered with a failed read), so an unreachable GitHub leaves the
    committed cache as-is. A GitHub read/derive failure never raises; the `refresh` CLI's fail-open backstops
    any other error, so the workflow never crashes (the digest still commits). `transport` is injectable so
    tests/the demo run the real derive + write offline. Returns
    {debt, standing, degraded} for the caller's plain-language report."""
    now = now or utc_now()
    gh = GitHubIssues(repo, token, transport=transport)
    debt = standing = None
    try:
        open_issues = gh.list_open_engine_issues()
        debt = {"open_count": len(open_issues), "as_of": now, "register": gh.issues_query_url()}
    except DegradedReadError:
        debt = None   # leave the prior committed debt count untouched
    try:
        derived = standing_situation.derive_standing_situation(gh)
        standing = {"milestone": derived.get("milestone"), "phase": derived.get("phase"), "as_of": now}
    except Exception:  # noqa: BLE001 — a where-we-are read failure must not break the debt refresh or the workflow
        standing = None   # leave the prior committed standing untouched
    if debt is not None or standing is not None:
        refresh_state(state_path, debt, standing)
    return {"debt": debt, "standing": standing, "degraded": debt is None and standing is None}


def degraded_readout(count, as_of) -> str:
    """The exact, plain-language degraded line (state/README + telemetry/README wording). Telemetry
    PRODUCES it; boot renders it."""
    if count is None:
        return ("I couldn't reach GitHub to refresh the engine's self-monitoring backlog, and there "
                "is no offline count to fall back on — treat the engine's open-problem count as "
                "unknown until GitHub returns.")
    when = as_of or "an earlier session"
    return (f"{count} open problems as of {when} — I couldn't refresh this, so it may be wrong; "
            f"re-ground before you rely on it. The per-issue detail is temporarily unreachable until "
            f"GitHub returns.")


# ---- the run (applies the pure Plan via the boundary) ----------------------

class Report:
    def __init__(self, *, degraded, debt=None, pressure_line=None, opened=0, updated=0, closed=0,
                 degraded_line=None):
        self.degraded = degraded
        self.debt = debt
        self.pressure_line = pressure_line
        self.opened = opened
        self.updated = updated
        self.closed = closed
        self.degraded_line = degraded_line


def run(github: GitHubIssues, records: list, cache: Cache, thresholds: dict, now: str,
        state_path: str | None = None) -> Report:
    """One telemetry pass: ensure the label, read the open engine Issues, reconcile, apply the Plan's
    writes, persist the cache, and compute the refreshed State debt + the render-only pressure line.

    Fail-open / degrade is honoured for EVERY GitHub call, not only the read: any GitHub failure —
    ensure-label, the list read, OR a write — whether a 4xx auth/scope error or a transient 5xx/outage,
    degrades to State's committed offline count and returns a degraded Report rather than raising, so a
    telemetry pass can never strand the in-session caller (the operator-never-stranded floor, state/README
    + principles §5/§6). Writes already applied before a mid-pass failure stand (best-effort; the next
    pass reconciles). The only side effect on the read-failure path is the idempotent label ensure — no
    Issue is opened/updated/closed. An UNEXPECTED (non-GitHub) error is deliberately left to surface:
    telemetry's own-crash fail-open is main()'s boundary and the in-session caller's degraded-capability
    seam, and silently swallowing a real bug would let self-monitoring quietly do nothing."""
    try:
        github.ensure_label()
        open_issues = github.list_open_engine_issues()
    except DegradedReadError:
        count, as_of = read_state_debt(state_path or DEFAULT_STATE_PATH)
        return Report(degraded=True, degraded_line=degraded_readout(count, as_of))

    plan = reconcile(records, open_issues, cache.load(), thresholds, now)
    opened = updated = closed = 0
    try:
        for sid, title, body in plan.to_open:
            created = github.open_issue(title, body)
            if sid in plan.next_counts:
                plan.next_counts[sid]["issue"] = created.get("number")
            opened += 1
        for number, body in plan.to_update:
            github.update_issue(number, body)
            updated += 1
        for number in plan.to_close:
            github.close_issue(number)
            closed += 1
    except DegradedReadError:
        cache.store(plan.next_counts)   # persist accrued counts; the writes already applied stand
        count, as_of = read_state_debt(state_path or DEFAULT_STATE_PATH)
        return Report(degraded=True, degraded_line=degraded_readout(count, as_of),
                      opened=opened, updated=updated, closed=closed)
    cache.store(plan.next_counts)

    debt = {"open_count": plan.open_count, "as_of": now, "register": github.issues_query_url()}
    if state_path:
        # The standing-situation cache rides THIS same GitHub-derived pass as the debt count (D-198): derive
        # it live and write both fields together. A standing-derive failure degrades only that one field —
        # we pass standing=None so the debt write still lands and the prior cached standing is left intact
        # (never clobbered with a failed read).
        standing = None
        try:
            derived = standing_situation.derive_standing_situation(github)
            standing = {"milestone": derived.get("milestone"), "phase": derived.get("phase"), "as_of": now}
        except Exception:  # noqa: BLE001 — a where-we-are read failure must not break the debt refresh
            standing = None
        refresh_state(state_path, debt, standing)
    pressure = triage_pressure_line(plan.low_severity_open_count, int(thresholds.get("triage_pressure", 0)))
    return Report(degraded=False, debt=debt, pressure_line=pressure,
                  opened=opened, updated=updated, closed=closed)


def promote_finding(github: GitHubIssues, record: dict, now: str, *, title: str | None = None,
                    body_core: str | None = None):
    """Promote ONE finding to a tracked engine Issue — the out-of-band "log it" relay a producer hands
    a single concern to for durable tracking WITHOUT running a full triage pass. Close (slice 22) calls
    it at cap-exhaustion / fail-open to degrade a still-undispositioned finding to logged (never lost);
    the soft-finding promoter (audit_soft_promote) calls it to track a standing length-budget nudge.

    Open-or-update, deduped by `source_id` (the same source-keyed dedup `run` uses, via
    list_open_engine_issues + the body sentinel). It does **no auto-resolve**: unlike `run`, it never
    closes Issues absent from a records list, so logging one finding can never silently close every
    OTHER open engine Issue — the exact hazard that bars `run([one_finding])`. It is **cache-free and
    State-free**: a one-shot surfacing, not a triage pass, so it never disturbs `run`'s persistence
    accrual or the committed debt cursor.

    By default the Issue's title/body are telemetry's own health framing (issue_title/issue_body). A
    producer that needs DIFFERENT operator-facing prose — e.g. the soft-finding promoter's lane-aware
    body, which must say a machinery fix belongs upstream — passes a pre-rendered `title` and `body_core`
    (the prose only; telemetry still owns and appends the first-/last-seen line and the invisible signal
    marker via _with_tracking_trailers, so dedup/recovery stay sound regardless of the framing).

    Degrades to **False** when GitHub is unreachable or errors (DegradedReadError) — the finding was
    already surfaced to the operator in-session and the protected-branch merge is the durable backstop;
    a caller must NOT claim durable tracking when the write could not land. Returns the Issue number
    (opened or updated) on success. An unexpected (non-GitHub) error is left to surface to the caller's
    own fail-open boundary (close's Stop handler rides hooks.run_hook's fail-open)."""
    sid = derive_source_key(record)
    first_seen = record.get("first_seen") or now
    ttl = title if title is not None else issue_title(record)
    body = (_with_tracking_trailers(body_core, sid, first_seen, now) if body_core is not None
            else issue_body(record, first_seen, now))
    try:
        github.ensure_label()
        existing = next((i for i in github.list_open_engine_issues() if i.get("source_id") == sid), None)
        if existing is not None:
            github.update_issue(existing["number"], body)
            return existing["number"]
        return github.open_issue(ttl, body).get("number")
    except DegradedReadError:
        return False


# ---- the operator demo (faked GitHub, REAL reconcile logic) ----------------

class _FakeGitHub:
    """An in-memory stand-in for GitHub used ONLY by the demo: it records issues in a dict and serves
    the (method, path, body) transport contract. The harness it drives is the REAL GitHubIssues +
    reconcile — only the network is faked (the demo-fidelity rule)."""

    def __init__(self, *, fail_status: int | None = None):
        self.issues: dict = {}
        self.labels: set = set()
        self._next = 1
        self.fail_status = fail_status

    def transport(self, method, path, body):
        if self.fail_status and "/issues" in path and method == "GET":
            return self.fail_status, None
        if path.endswith("/labels") and method == "POST":
            self.labels.add(body["name"])
            return 201, body
        if "/labels/" in path and method == "GET":
            name = path.rsplit("/", 1)[1]
            return (200, {"name": name}) if name in self.labels else (404, None)
        if path.split("?")[0].endswith("/issues") and method == "GET":
            page = int(re.search(r"[?&]page=(\d+)", path).group(1)) if "page=" in path else 1
            rows = [i for i in self.issues.values() if i["state"] == "open"]
            return 200, (rows if page == 1 else [])
        if path.split("?")[0].endswith("/issues") and method == "POST":
            num = self._next
            self._next += 1
            self.issues[num] = {"number": num, "title": body["title"], "body": body["body"],
                                "labels": body.get("labels", []), "state": "open"}
            return 201, self.issues[num]
        m = re.search(r"/issues/(\d+)$", path)
        if m and method == "PATCH":
            num = int(m.group(1))
            self.issues[num].update({k: v for k, v in body.items()})
            return 200, self.issues[num]
        return 404, None


def _rec(sid, severity, message):
    return {"source_id": sid, "severity": severity, "message": message, "location": None}


def _demo(_argv) -> int:
    th = {"persistence": 3, "auto_resolve": 2, "triage_pressure": 10}
    fake = _FakeGitHub()
    gh = GitHubIssues("you/your-project", "demo-token", transport=fake.transport)
    cache = Cache(os.path.join(validate.ROOT, ".engine", "telemetry", ".cache", "_demo.json"))
    try:
        os.remove(cache.path)
    except OSError:
        pass
    clock = ["2026-06-05T0%d:00:00Z" % n for n in range(1, 9)]

    print("TELEMETRY DEMO — real triage logic, fake in-memory GitHub (no real issues, no token).\n")

    benign = _rec("rule:flaky-format-check", PERSISTENT_BENIGN,
                  "A formatting check keeps reporting the same minor issue run after run.")
    print(f"Thresholds (read from the policy file): persistence={th['persistence']}, "
          f"auto_resolve={th['auto_resolve']}.\n")

    print("(1) A low-impact signal fires below the persistence threshold — nothing should open:")
    for k in range(2):
        r = run(gh, [benign], cache, th, clock[k])
        print(f"    fire {k+1}: opened={r.opened} updated={r.updated} closed={r.closed} "
              f"-> open issues now: {sum(1 for i in fake.issues.values() if i['state']=='open')}")

    print("\n(2) It crosses the threshold on the 3rd fire — ONE issue opens:")
    r = run(gh, [benign], cache, th, clock[2])
    open2 = sum(1 for i in fake.issues.values() if i['state'] == 'open')
    print(f"    fire 3: opened={r.opened} -> open issues now: {open2}")

    print("\n(3) The SAME signal fires again 3 more times — the one issue is UPDATED, never duplicated:")
    for k in range(3, 6):
        r = run(gh, [benign], cache, th, clock[k])
        print(f"    re-fire: opened={r.opened} updated={r.updated} -> open issues now: "
              f"{sum(1 for i in fake.issues.values() if i['state']=='open')}  (still one — dedup holds)")
    open3 = sum(1 for i in fake.issues.values() if i['state'] == 'open')

    print("\n(4) A DIFFERENT, trust-critical signal fires once — it opens immediately (no waiting):")
    crit = _rec("check/protection", TRUST_CRITICAL,
                "A safety check could not run, so the engine may be unable to catch a bad change.")
    r = run(gh, [benign, crit], cache, th, clock[6])
    open4 = sum(1 for i in fake.issues.values() if i['state'] == 'open')
    print(f"    opened={r.opened} -> open issues now: {open4}  (two distinct signals, two issues)")

    print("\n(5) The cause is removed — after auto_resolve absent observations the benign issue closes:")
    for k in range(2):
        r = run(gh, [crit], cache, th, clock[7])  # benign absent; crit still firing
        print(f"    absent {k+1}: closed={r.closed} -> open issues now: "
              f"{sum(1 for i in fake.issues.values() if i['state']=='open')}")

    print("\n(6) GitHub is unreachable — the read FAILS rather than reading 'no issues', and we fall")
    print("    back to the committed offline count with an honest line (never a silent or wrong zero):")
    down = GitHubIssues("you/your-project", "demo-token", transport=_FakeGitHub(fail_status=403).transport)
    r6 = run(down, [benign], cache, th, clock[0], state_path=None)
    print("    " + r6.degraded_line)

    print("\n(7) A sample of the engine-opened issue, exactly as it appears in your tracker "
          "(read it for jargon):")
    sample = next(i for i in fake.issues.values())
    print("    ┌─ TITLE: " + issue_title(crit))
    for line in issue_body(crit, clock[6], clock[6]).split("\n"):
        print("    │ " + line)
    print("    └─ (the last line is an invisible marker; it does not render in GitHub)")

    try:
        os.remove(cache.path)
    except OSError:
        pass
    print("\nDone — no real issues were created; only the network was faked. The triage LOGIC above is "
          "real; that it writes correctly to your REAL GitHub is confirmed the first time it runs live.")
    # Self-check: ONE issue opens only when the benign signal crosses the threshold (3rd fire), re-fires
    # never duplicate it, a distinct trust-critical signal opens a 2nd, and an unreachable GitHub degrades
    # in-band (never a silent or wrong zero).
    ok = open2 == 1 and open3 == 1 and open4 == 2 and bool(r6.degraded_line)
    if not ok:
        print("\nDEMO UNEXPECTED: the triage open/dedup/critical-open counts or the offline degrade line "
              "did not behave as expected.", file=sys.stderr)
        return 1
    return 0


# ---- the engine-issue backlog feed for the scheduled self-review (audit concern #2) ----

# Cap each issue body fed to the persona: the backlog is small, but one pathological issue must not bloat
# the prompt. An engine issue carries its substance at the head, so a generous cap loses nothing material.
_ISSUE_BODY_CAP = 2000


def render_engine_issue_backlog(repo: str, token: str, *, transport=None) -> str:
    """Plain text the audit-prep workflow feeds into the read-only self-review persona's prompt so it can work
    concern #2 (the engine-labelled open issues / debt register). The persona never reaches GitHub itself —
    the telemetry boundary owns the issue view and the workflow injects the result. Returns a header plus each
    open engine-labelled issue (number, title, capped body); a plain 'none open' line when the backlog is
    empty; or — on ANY read failure — a 'could not be read' line (NEVER a silent empty), so the persona
    discloses the gap rather than presenting concern #2 as worked. `transport` is injectable for tests."""
    try:
        issues = GitHubIssues(repo, token, transport=transport).list_open_engine_issues()
    except Exception as exc:  # noqa: BLE001 — any read failure must surface as an honest gap, never silent empty
        return ("OPEN ENGINE-LABELLED ISSUES (the debt register): could not be read this run — "
                f"{exc}. Treat concern #2 as unreviewed and say so plainly in your digest.")
    if not issues:
        return ("OPEN ENGINE-LABELLED ISSUES (the debt register): none are open right now — "
                "concern #2 has nothing to review this run.")
    parts = [f"OPEN ENGINE-LABELLED ISSUES (the debt register) — {len(issues)} open, fetched for concern #2. "
             "This is the COMPLETE set of currently-open engine-labelled issues: it was read by paging the "
             "engine-label query to exhaustion, and any read failure is reported in-band (on a failed read you "
             "would see a read-failure notice in place of this list, never a silently short one) — so "
             "treat it as the whole open backlog, not a sample, and do not hedge that issues may be missing. "
             "Judge each against the CURRENT code: does it still reproduce, and is the backlog still honestly "
             "triageable?", ""]
    for issue in issues:
        body = issue["body"].strip()
        if len(body) > _ISSUE_BODY_CAP:
            body = body[:_ISSUE_BODY_CAP] + "\n…(body truncated)"
        # Both the title and the body are third-party-authorable text fed BETWEEN the workflow's fence
        # markers (----- BEGIN/END OPEN ENGINE-LABELLED ISSUES -----); defang any fence-marker shape in
        # EITHER so a forged marker can neither close the section nor smuggle an instruction past it.
        parts.append(f"#{issue['number']}  {validate.defang_prompt_fence_markers(issue['title'])}")
        if body:
            parts.append(validate.defang_prompt_fence_markers(body))
        parts.append("---")
    return "\n".join(parts)


def _engine_issues_cli(argv: list) -> int:
    """The audit-prep workflow's read-only debt-register verb: print the open engine-labelled issues for the
    self-review persona (which never reaches GitHub itself). Reads GITHUB_REPOSITORY + GITHUB_TOKEN from the
    environment (the GitHub token, never the Claude OAuth token, which only auths the persona run). Exits 0
    whenever the env is present — even on a read failure, which it reports in-band so the persona can disclose
    the gap; a transient GitHub blip must never fail the self-review."""
    repo = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        print("usage: telemetry.py engine-issues   (needs GITHUB_REPOSITORY and GITHUB_TOKEN in the "
              "environment; it uses the GitHub token, never the Claude token)", file=sys.stderr)
        return 2
    print(render_engine_issue_backlog(repo, token))
    return 0


def _refresh_cli(argv: list) -> int:
    """The audit-prep workflow's offline-cache refresh verb. Reads GITHUB_REPOSITORY + GITHUB_TOKEN from the
    environment (the workflow passes the GitHub token — never the Claude OAuth token, which only auths the
    persona run), refreshes BOTH offline-cache fields read-only, and reports in plain language. Exits 0 even
    when GitHub is unreachable — the cache is best-effort freight and a failed refresh must never block the
    digest the workflow commits. An optional argv[0] overrides the state path (for tests)."""
    repo = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        print("usage: telemetry.py refresh   (needs GITHUB_REPOSITORY and GITHUB_TOKEN in the environment; "
              "it uses the GitHub token, never the Claude token)", file=sys.stderr)
        return 2
    state_path = argv[0] if argv else DEFAULT_STATE_PATH
    result = refresh_cache(state_path, repo, token)
    if result["degraded"]:
        print("Could not reach GitHub to refresh the offline where-we-stand cache; left the committed "
              "values unchanged. The digest still commits.")
        return 0
    bits = []
    if result["debt"] is not None:
        bits.append(f"{result['debt']['open_count']} open engine item(s)")
    if result["standing"] is not None:
        bits.append("the where-we-are markers")
    print("Refreshed the committed offline cache (" + ", ".join(bits) + ").")
    return 0


def main(argv: list) -> int:
    """Fail-open: telemetry is self-surfacing and must never break a session. Any unexpected error
    emits a plain finding and exits 0."""
    try:
        if argv and argv[0] == "demo":
            return _demo(argv[1:])
        if argv and argv[0] == "refresh":
            return _refresh_cli(argv[1:])
        if argv and argv[0] == "engine-issues":
            return _engine_issues_cli(argv[1:])
        print("usage: telemetry.py {demo|refresh|engine-issues}   (the in-session triage run is driven by "
              "boot/build, not this CLI)", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — fail-open is the whole point
        print(json.dumps(validate.finding(
            "soft", f"The engine's self-monitoring hit an unexpected error and stopped without acting "
            f"({exc}); this was recorded and the session continues normally.")), file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
