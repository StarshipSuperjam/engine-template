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
import glob
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402  (sibling tool; reused for finding/frontmatter/effective_policy_values/ROOT)
import issue_author  # noqa: E402  (the shared issue-authoring helper — assembles the body to the control-plane contract)
import standing_situation  # noqa: E402  (the read-only "where we are" derive; telemetry refreshes its offline cache on this same GitHub pass — pure leaf, imports nothing back, so no cycle)
import github_client  # noqa: E402  (the shared authenticated GitHub API client; request-build for the issue read/write transport)

# ---- constants -------------------------------------------------------------

# The engine-domain label (control-plane owns the SCHEME; the STRING is a build-spec leaf decided
# with the maintainer). The single shared home: build-orchestration (build Issues) and audits READ
# this; provisioning (later slice) generalises the ensure-it-exists step and inherits the minimal
# ensure below. Renaming it elsewhere would split the routing substrate, so it lives once, here.
ENGINE_DOMAIN_LABEL = "engine"

# The two self-monitoring severity classes (distinct from the agent and check enums).
TRUST_CRITICAL = "trust-critical"          # could-not-run; promotes immediately
PERSISTENT_BENIGN = "persistent-but-benign"  # recurring low-impact; promotes after persistence

# The numeric grades attention's debt-blocking rule ranks a tracked finding on. Telemetry owns the severity
# CLASS (promotion — what becomes tracked debt at all); whether a graded finding actually BLOCKS the start of
# work is ATTENTION's own rule (D-117 explicitly rejects attributing blocking-membership to telemetry), so this
# only GRADES — it never decides. The two numbers are a recorded build-spec leaf (D-052/D-113), calibrated
# against the shipped `debt_blocking_threshold` (2) in .engine/policies/attention.md.
_TRUST_CRITICAL_RANK = 3.0    # above the shipped bar
_BENIGN_RANK = 1.0            # below the shipped bar -> debt that can wait, mentioned but not gating work


def severity_rank(severity: "str | None", blocking_threshold: float) -> "float | None":
    """Grade one tracked finding's severity CLASS into the numeric severity attention ranks and compares against
    its own debt-blocking threshold, or None when telemetry has no severity for it. `blocking_threshold` is the
    CALLER's bar, passed in — telemetry neither owns nor reads it (it is attention's policy value,
    operator-tunable per D-167).

    Three cases, each a deliberate posture:
      - TRUST_CRITICAL -> `max(rank, threshold)`: **always** meets the bar. Telemetry's contract for this class
        is "could-not-run; promotes immediately", so a safety gate that could not run must never be tuned OUT of
        blocking — without the clamp an operator raising `debt_blocking_threshold` past the fixed rank would
        silently defer the most urgent class, with no feedback. It rides the bar however high it goes.
      - PERSISTENT_BENIGN -> a fixed rank BELOW the shipped bar: a recurring low-impact item is debt that can
        WAIT rather than gate the start of work (attention/README:53) — `assign_partition` returns None for it,
        so it is mentioned in the card's open-problems count but never surfaced among the budgeted five.
      - anything else (an unmarked, pre-severity Issue — e.g. an audit-authored conformance finding, which
        carries no severity marker) -> **None: there is no severity to report.** Telemetry owns this class
        (D-118) and simply has not graded that Issue, so the honest answer is "unknown", not a number.
        Grading it to the threshold ITSELF — the shape this first carried — was the bug it was meant to fix,
        one layer up: an item pinned exactly AT the bar makes the bar compare to itself, so moving the dial
        cannot change the outcome. And any fixed stand-in would be telemetry inventing a severity it was
        never given, which `assign_partition` forbids in as many words ("attention does NOT invent it").
        The caller passes None straight through as an ABSENT severity, and attention's own rule decides —
        absent severity is a deferral, mentioned in the open-problems count but not gating the start of
        work, exactly the policy's "rather than just being mentioned"."""
    if severity == TRUST_CRITICAL:
        # The clamp rides ONLY a real bar, and cannot rescue an unreal one — be clear about that, because
        # the tempting comment here is a false one. Against an endless bar every severity is below it, so
        # this class would be dropped whatever number came back; returning the fixed rank does not save it.
        # What saves it is that a value the engine cannot measure against never reaches this function: the
        # read-time merge (`validate.effective_policy_values`) refuses one and the shipped default stands.
        # This is the belt for a caller that bypassed that merge — it keeps a non-finite value from
        # propagating INTO the ranking as a severity, which is worth doing and is all it is worth claiming.
        bar = blocking_threshold if isinstance(blocking_threshold, (int, float)) else _TRUST_CRITICAL_RANK
        return max(_TRUST_CRITICAL_RANK, float(bar)) if math.isfinite(bar) else _TRUST_CRITICAL_RANK
    if severity == PERSISTENT_BENIGN:
        return _BENIGN_RANK
    return None

# The set of source-ids a triage pass is AUTHORITATIVE for — the ONLY open Issues its auto-resolve may
# close. A live pass reads a PARTIAL slice of the engine's signals (this build reads CI outcomes only), so
# it must never retire an Issue opened by a source it did not observe this pass (e.g. an out-of-band
# hooks-fail-open Issue). `authoritative` is a REQUIRED argument to reconcile/run — there is deliberately
# NO claim-all default, because a claim-all default would put the "silently close every other engine Issue"
# hazard one forgotten argument away; a forgotten argument is instead a loud TypeError. Pass a concrete set
# of the source-ids the pass owns, or AUTHORITATIVE_ALL for a pass that genuinely observes every source. A
# failed or partial read passes frozenset() so the pass closes NOTHING (fail-safe).
AUTHORITATIVE_ALL = object()

USER_AGENT = "engine-telemetry"

# An invisible marker carried in a tracked Issue's body so a later run can recover which signal the
# Issue belongs to even if the local cache was wiped (a fresh clone / new machine). It is an HTML
# comment, so it never renders as visible prose — no backstage vocabulary reaches the operator.
_SENTINEL_TEMPLATE = "<!-- engine-signal: {sid} -->"
_SENTINEL_RE = re.compile(r"<!--\s*engine-signal:\s*(.+?)\s*-->")
# The severity class carried in a SECOND invisible marker alongside the signal id, so the durable Issue
# itself records whether it is a low-impact (persistent-but-benign) or trust-critical item. This is what lets
# boot count the COMPLETE open low-severity set for the render-only triage-pressure meter from its existing
# read-only Issue read — authoritative from the durable record, never a per-machine cache that a scoped pass
# (CI on an ephemeral runner, ambient on one laptop) can only partially see. The two constants are marker-safe
# by construction; parse_severity takes the LAST match, so forged body prose cannot hijack it (as source-id).
_SEVERITY_TEMPLATE = "<!-- engine-severity: {sev} -->"
_SEVERITY_RE = re.compile(r"<!--\s*engine-severity:\s*(.+?)\s*-->")
# The first-seen half of _with_tracking_trailers' visible trailer ("*First noticed X; last reconfirmed
# Y.*"). Recovered so a cache-free promote (promote_finding) and a consolidation can PRESERVE a tracked
# Issue's original first-noticed instead of resetting it to `now` — the durable first-seen lives in the
# Issue body itself, the same place parse_source_id recovers the signal id from.
_FIRST_NOTICED_RE = re.compile(r"\*First noticed\s+(.+?);\s+last reconfirmed")

DEFAULT_POLICY_PATH = os.path.join(validate.ROOT, ".engine", "policies", "triage-threshold.md")
CONTRACT_THRESHOLD_POLICY_PATH = os.path.join(validate.ROOT, ".engine", "policies", "contract-threshold.md")
# The operator's OWN engine decisions — the per-project decision records the engine writes when the operator
# makes a real choice about how their engine is set up. They live in the deployment-owned folder the engine
# never overwrites on an upgrade, one level below the engine's own founding records. The founding records
# travel with the engine and would false-count toward the operator's rate right after an upgrade, so the
# contract-rate meter reads ONLY this folder. (This path is the same one the knowledge graph treats as the
# deployment-owned decision stream; a test pins the two together so they can't drift apart.)
INSTANCE_CONTRACTS_DIR = os.path.join(validate.ROOT, ".engine", "contracts", "instance")
_CONTRACT_RATE_WINDOW_DAYS = 7
_DEFAULT_CONTRACT_RATE_MAX = 3  # the fallback limit if the policy's own value can't be read
DEFAULT_STATE_PATH = os.path.join(validate.ROOT, ".engine", "state", "state.json")
DEFAULT_CACHE_PATH = os.path.join(validate.ROOT, ".engine", "telemetry", ".cache", "streams.json")
# The engine-only sink for a fail-open hook crash's backstage diagnostic (exception message + code
# location). The hook fail-open crash is promoted as a telemetry finding (hooks._do_promote_fail_open ->
# promote_finding), so the detail BEHIND that finding lives beside telemetry's other gitignored cache —
# never committed (topology law 5), never operator-visible. hooks appends to it; telemetry owns the path.
HOOK_CRASH_DEBUG_PATH = os.path.join(validate.ROOT, ".engine", "telemetry", ".cache", "hook-crash-debug.log")


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


def source_id_is_marker_safe(sid) -> bool:
    """A source-id must be a plain dedup key. It is carried verbatim into the invisible
    `<!-- engine-signal: {sid} -->` trailer (_with_tracking_trailers) that parse_source_id recovers, so a
    sid containing an HTML-comment delimiter or a newline could forge or split that marker and hijack dedup.
    A producer reading third-party-authorable ids (a GitHub check name, a rule id) validates each with this
    and SKIPS an unsafe one — it never promotes it, and never crashes the pass on it."""
    return isinstance(sid, str) and bool(sid) and "<!--" not in sid and "-->" not in sid and "\n" not in sid


def _claims(authoritative, sid: str) -> bool:
    """Whether this pass is authoritative for `sid` — i.e. may absent-count and close its Issue. A pass owns
    only the source-ids it observes (or AUTHORITATIVE_ALL); any other open Issue is another source's and is
    carried forward untouched, never retired by a pass that never looked at it (see reconcile)."""
    return authoritative is AUTHORITATIVE_ALL or sid in authoritative


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
        # reminder fires is itself tunable, so the command is offered at the moment it surfaces. Boot renders
        # this line live from the complete open low-severity count (#403.2), so the offer fires on a real backlog.
        return ("The engine's self-monitoring backlog is growing — there are several low-priority "
                "engine items open. Nothing here is urgent; you can review them when convenient. "
                "You can also change when this reminder appears — type /engine-tune.")
    return None


def contract_rate_threshold(policy_path: str | None = None, override: dict | None = None) -> int:
    """The effective limit on how many permanent decision records may be accepted in a 7-day window
    before the nudge fires = the policy's shipped default merged per-key with any operator override
    (the same read-time merge the triage thresholds use), so a reviewed /engine-tune governs it live
    instead of the shipped default silently winning."""
    fm = validate.frontmatter(policy_path or CONTRACT_THRESHOLD_POLICY_PATH)
    default = fm.get("values") or {}
    effective, _findings = validate.effective_policy_values(
        default, override or {}, structural_keys=set(), tier="soft",
        message="The contract-rate limit is a tuning value; an override retunes it, never a law.")
    return int(effective.get("contract_rate_max", _DEFAULT_CONTRACT_RATE_MAX))


def derive_contract_rate(now: str, contracts_dir: str | None = None) -> int | None:
    """Count the operator's OWN engine decisions that reached the accepted state in the trailing
    7 days — the input to the "are decisions being over-recorded?" nudge. Reads only the
    deployment-owned per-instance decision folder, never the engine's own founding records (those
    travel with the engine and carry historical dates, so counting them would false-fire right after
    an upgrade). A record counts when its saved state is `accepted` and its date falls in the last
    7 days of `now`. A single unreadable/malformed record is skipped — so one bad file can't blind
    the meter during exactly the busy stretch it exists to notice; only a folder that can't be listed
    at all yields None, and the line is then suppressed rather than showing a false number."""
    directory = contracts_dir or validate.env_override_path("ENGINE_INSTANCE_CONTRACTS_DIR", INSTANCE_CONTRACTS_DIR)
    try:
        paths = glob.glob(os.path.join(directory, "**", "*eADR-*.md"), recursive=True)
        now_date = datetime.date.fromisoformat(now[:10])
    except Exception:  # noqa: BLE001 — an unlistable folder or an unparseable clock suppresses the meter, no false number
        return None
    cutoff = now_date - datetime.timedelta(days=_CONTRACT_RATE_WINDOW_DAYS)
    count = 0
    for path in paths:
        try:
            fm = validate.frontmatter(path)
            if str(fm.get("status")) != "accepted":
                continue
            accepted = datetime.date.fromisoformat(str(fm.get("date"))[:10])
        except Exception:  # noqa: BLE001 — skip one malformed record, never blind the whole meter
            continue
        if cutoff < accepted <= now_date:
            count += 1
    return count


def contract_rate_line(count: int, threshold: int) -> str | None:
    """Render-only: one plain-language nudge once the operator's engine decisions written down as
    permanent records in the last 7 days cross the threshold, else nothing. Like the backlog meter,
    crossing it promotes NOTHING — it never becomes an item itself, so it can't feed what it measures."""
    if count > threshold:
        # The trailing sentence is the reactive retune offer: the threshold that decides when this note fires
        # is itself tunable, so the command is offered at the moment it surfaces. The line names the engine's
        # own decision records (engine-domain, so the operator knows it is not about their product) and a real
        # next move (ask to see what got recorded), not only a way to silence it.
        return ("I've been writing down more of our engine decisions as permanent decision records than "
                "usual this past week — it's worth a quick look at whether they're being over-recorded. "
                "Nothing here is urgent. Ask me to show you what got recorded and why, and I'll help you tell "
                "the keepers from the ones that could just ride a pull request instead. To change when this "
                "note appears, type /engine-tune.")
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


def parse_severity(body: str) -> str | None:
    """Recover a tracked Issue's severity class from its invisible severity marker — the read boot counts
    for the render-only triage-pressure meter (the COMPLETE open low-severity set, authoritative from the
    durable Issue itself). Takes the LAST marker (telemetry appends its own last, so forged body prose cannot
    hijack it), mirroring parse_source_id. None when the marker is absent (a pre-severity Issue, counted once
    telemetry next updates it) or was stripped."""
    matches = _SEVERITY_RE.findall(body or "")
    return matches[-1] if matches else None


def parse_first_noticed(body: str) -> str | None:
    """Recover a tracked Issue's original first-noticed timestamp from the visible trailer in its body,
    so a cache-free promote or a consolidation can PRESERVE it rather than reset it to `now`. Returns
    None when the trailer is absent or was stripped.

    Takes the LAST match, mirroring parse_source_id: _with_tracking_trailers always appends its trailer
    after the author-influenced body prose, so a forged "*First noticed ...*" line earlier in the body
    cannot hijack the real one. The stored timestamps are ISO-8601 with a trailing `Z`, so plain string
    `min()` over recovered values is chronological — the earliest across a duplicate group is the true
    first-noticed."""
    matches = _FIRST_NOTICED_RE.findall(body or "")
    return matches[-1] if matches else None


def issue_title(record: dict) -> str:
    """A plain-language Issue title that says, with no backstage vocabulary, that this is about the
    engine's own health. A producer that needs its OWN lane-aware title (the soft-finding promoter's
    "Engine length budget: …") carries it as a `title` FIELD on the record; otherwise the first sentence
    of the finding's message derives the title. One override-honouring point, shared by reconcile and
    promote_finding, so producer-framed and telemetry-framed titles decide their framing in ONE place.
    Honours a present title even if empty (`is not None`, matching promote_finding's prior `title is not None`
    and issue_body's body_core check) — an absent title falls through to the message-derived one."""
    if record.get("title") is not None:
        return record["title"]
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
    (stream / severity class / persistence / triage / source) reaches the operator.

    THE SINGLE body-assembly point (shared by reconcile AND promote_finding, so producer-framed and
    telemetry-framed bodies decide their framing in ONE place — no divergent renderer). A producer that
    needs its OWN lane-aware prose (the soft-finding promoter's machinery-vs-local-owned body) carries a
    pre-rendered `body_core` FIELD on the record — already run through render_engine_issue_body with its
    own `references`, so it is used verbatim and only the trailers are appended. Otherwise telemetry's own
    framing is rendered here, and any `references` the record carries (a knowledge-graph entity permalink
    for "what is broken", F0202) are passed to the helper as labelled links."""
    body_core = record.get("body_core")
    if body_core is None:
        what_this_is = (
            "The engine watches the health of *its own* machinery — the tools and checks that help run "
            "your project, and it noticed something it is tracking here so it stays visible. This is not "
            "a problem with your product, and the engine will never open or close an item you created — "
            f"only its own.\n\n**What it noticed.** {record['message']}"
        )
        whats_next = (
            "Usually nothing right now. When a session next works on the engine it can prepare a fix for you to "
            "review and merge, the same way you already do — the engine never changes anything in your project "
            "on its own. This is a flag, not a repair: it stays visible until the underlying problem is resolved. "
            "If it lingers and you want it sorted sooner, just say so."
        )
        body_core = issue_author.render_engine_issue_body(
            what_this_is=what_this_is, whats_next=whats_next, references=record.get("references"))
    return _with_tracking_trailers(body_core, record["source_id"], record["severity"], first_seen, last_seen)


def _with_tracking_trailers(body_core: str, source_id: str, severity: str, first_seen: str,
                            last_seen: str) -> str:
    """Append telemetry's own trailers to an already-rendered issue body: the first-/last-seen line and
    two invisible markers — the signal id (recovered by parse_source_id) and the severity class (recovered
    by parse_severity), both HTML comments surviving a cache wipe. Telemetry OWNS the markers, appended LAST —
    so even if a caller's body prose carries a forged marker, parse_source_id / parse_severity (each taking
    the LAST match) still recover the real ones. The producer's contract: the source_id must be marker-safe
    (no `<!--`/`-->` or newline — every producer's source_id is a plain key, and the soft-finding promoter
    skips any path that is not); severity is one of the two known classes, marker-safe by construction. Shared
    by issue_body (telemetry's own health framing) and promote_finding's pre-rendered-body path (a producer's
    lane-aware framing), so the trailer/marker shape is stated once."""
    return (
        f"{body_core}\n"
        f"*First noticed {first_seen}; last reconfirmed {last_seen}.*\n\n"
        f"{_SEVERITY_TEMPLATE.format(sev=severity)}\n"
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


def reconcile(records: list, open_issues: list, counts: dict, thresholds: dict, now: str, *,
              authoritative, live: bool = False) -> Plan:
    """The pure loop. Inputs: the finding-records observed THIS run; the currently-open engine Issues
    (each {number, title, body, source_id}); the cross-run cache keyed by source_id
    ({persist, absent, issue, first_seen, severity}); the effective thresholds; the clock value;
    `authoritative` — the source-ids this pass may auto-resolve (a set, or AUTHORITATIVE_ALL; REQUIRED, no
    claim-all default — see AUTHORITATIVE_ALL); and `live`. Returns a Plan. No IO, no clock — deterministic.

    Dedup is two-layer: an open Issue is matched to a signal by the source-id marker recovered from
    its body (open_issues[*].source_id), and the cache's remembered issue number is the fast path /
    cross-check. When a create/create race (GitHub has no atomic create-if-absent) has left MORE THAN
    ONE open Issue for one sid — both markers intact — this pass CONSOLIDATES them: the lowest-numbered
    survivor is kept and the rest are closed (within authority scope, below), so a keyable duplicate is
    healed, never silently dropped. The one residual worst case is a marker stripped AND the cache wiped:
    that Issue is unkeyable by any pass, so a single duplicate can persist (never a missed signal) — it
    is the honest limit of body+cache dedup, not something absence self-corrects.

    Auto-resolve is SOURCE-SCOPED: an open Issue whose source-id this pass is not `authoritative` for is
    another source's signal, so it is carried forward with its prior counts UNTOUCHED — never absent-counted
    and never closed. This is what lets a partial live pass (e.g. a CI-only run) run safely without silently
    retiring the out-of-band Issues (hooks fail-open, module-manager, restore-vault) it never observed.

    `live` distinguishes the two state substrates. A CACHE-ACCRUED signal (live=False — the ambient local
    check-fires) uses the persistence/auto-resolve LATENCY: it promotes only after its cross-pass `persist`
    count crosses the threshold and resolves only after `auto_resolve` absent observations, both counted in
    the gitignored cross-run cache. A LIVE-DERIVED signal (live=True — read fresh from a DURABLE source each
    pass, e.g. the GitHub CI check-runs) carries its state in the durable Issue itself, not the ephemeral
    cache, so it promotes on the FIRST observed occurrence and resolves on the FIRST observed clearance — the
    cross-pass counters cannot be relied on for it (the driver runs on an ephemeral runner that wipes the
    cache each pass) and are not needed (the live read IS the current truth). Severity still classifies the
    signal; `live` only sets which substrate gates its latency."""
    persistence = int(thresholds.get("persistence", 0))
    auto_resolve = int(thresholds.get("auto_resolve", 0))
    plan = Plan()

    observed = {derive_source_key(r): r for r in records}
    # Group open Issues by signal id. A create/create race can leave MORE THAN ONE open Issue per sid;
    # keep the lowest-numbered as the canonical survivor and treat the rest as duplicates to consolidate
    # (closed within authority scope below) — never silently overwritten in a map and left open forever.
    groups: dict = {}
    for i in open_issues:
        sid = i.get("source_id")
        if sid:
            groups.setdefault(sid, []).append(i)
    for g in groups.values():
        g.sort(key=lambda i: i["number"])
    open_by_sid = {sid: g[0] for sid, g in groups.items()}
    duplicates_by_sid = {sid: g[1:] for sid, g in groups.items() if len(g) > 1}
    # cache recovery: a cached issue number for a sid still open but whose marker was stripped
    open_numbers = {i["number"] for i in open_issues}
    for sid, prev in counts.items():
        num = (prev or {}).get("issue")
        if sid not in open_by_sid and num in open_numbers:
            open_by_sid[sid] = next(i for i in open_issues if i["number"] == num)

    def _earliest_first_seen(sid: str, cached) -> str:
        # First-noticed = the EARLIEST ever seen: the cached value AND every group member's body-recovered
        # first-noticed (parse_first_noticed), so a live-derived signal (cache wiped each pass) or a
        # consolidation never resets it forward to `now`. ISO-`Z` timestamps make plain min() chronological.
        candidates = [cached] if cached else []
        candidates += [fn for fn in (parse_first_noticed(i.get("body") or "")
                                     for i in groups.get(sid, [])) if fn]
        return min(candidates) if candidates else now

    def _consolidate(sid: str, survivor_number: int) -> None:
        # Fold every same-signal duplicate into the survivor and close it — but ONLY when this pass is
        # authoritative for the sid, so a scoped pass (e.g. CI-only) never touches another source's dups.
        if not _claims(authoritative, sid):
            return
        for dup in duplicates_by_sid.get(sid, []):
            plan.to_update.append((dup["number"],
                                   _consolidation_note(survivor_number) + (dup.get("body") or "")))
            plan.to_close.append(dup["number"])

    # present signals: refresh-or-promote
    for sid, record in observed.items():
        prev = counts.get(sid) or {}
        persist = int(prev.get("persist", 0)) + 1
        first_seen = _earliest_first_seen(sid, prev.get("first_seen"))
        entry = {"persist": persist, "absent": 0, "issue": prev.get("issue"),
                 "first_seen": first_seen, "severity": record.get("severity")}
        existing = open_by_sid.get(sid)
        if existing is not None:
            entry["issue"] = existing["number"]
            plan.to_update.append((existing["number"], issue_body(record, first_seen, now)))
            _consolidate(sid, existing["number"])
        elif live or promotion_due(record, persist, persistence):
            # A live-derived signal promotes on first observation (its state is the durable Issue, not the
            # ephemeral cache the persistence count needs); a cache-accrued one waits for promotion_due.
            entry["issue"] = None  # assigned by the caller once the Issue is created
            plan.to_open.append((sid, issue_title(record), issue_body(record, first_seen, now)))
        plan.next_counts[sid] = entry

    # absent signals on open Issues: count toward auto-resolve, close when due — but ONLY for the source-ids
    # this pass is authoritative for. An Issue whose source this pass never observed is carried forward with
    # its prior counts unchanged (no absent-increment, no close), so a scoped run neither retires another
    # source's Issue nor drops it from the cache/pressure count.
    for sid, issue in open_by_sid.items():
        if sid in observed:
            continue
        prev = counts.get(sid) or {}
        if not _claims(authoritative, sid):
            plan.next_counts[sid] = {"persist": int(prev.get("persist", 0)),
                                     "absent": int(prev.get("absent", 0)),
                                     "issue": issue["number"], "first_seen": prev.get("first_seen") or now,
                                     "severity": prev.get("severity")}
            continue
        absent = int(prev.get("absent", 0)) + 1
        if live or resolution_due(absent, auto_resolve):
            # A live-derived signal's Issue closes as soon as the durable source shows it clear (no
            # cross-pass absent count is available or needed); a cache-accrued one waits for resolution_due.
            # The whole signal is resolving, so close its duplicates too — but WITHOUT the "tracking
            # continues at #N" note _consolidate adds, since #N is closing in this same pass (the pointer
            # would land the operator on a closed Issue).
            plan.to_close.append(issue["number"])
            plan.to_close.extend(dup["number"] for dup in duplicates_by_sid.get(sid, []))
            # dropped from next_counts — the signal is gone and its Issue is closing
        else:
            # Survivor stays open: fold any create/create-race duplicates into it now, with the note.
            _consolidate(sid, issue["number"])
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
        req = github_client.request(path, self.token, user_agent=USER_AGENT, method=method, data=data)
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
        self.ensure_named_label(self.label, "ededed",  # a calm neutral grey
                                "Opened by the engine about its own health (not your product).")

    def ensure_named_label(self, name: str, color: str, description: str) -> None:
        """Idempotently ensure an arbitrary repo label exists (create it iff absent). `ensure_label` above is
        the engine-domain special case that delegates here with its own hardcoded values; provisioning calls
        this directly for the OTHER bootstrap-provisioned labels the operator applies but never hand-creates.
        The name is url-quoted into the GET path — a label string is an operator-facing build-spec leaf that
        could carry a space, so it must never be interpolated raw. RAISES on any non-404 HTTP error (a scope or
        auth failure must never read as "already present"). (A sibling parametrised copy lives on
        issue_conformance_ci.IssueConformanceClient; both trace back to github_client, the eventual single home
        once the shared-client debt is collapsed — kept behavior-identical meanwhile.)"""
        status, _ = self._transport("GET", f"/repos/{self.repo}/labels/{urllib.parse.quote(name, safe='')}", None)
        if status == 404:
            self._transport("POST", f"/repos/{self.repo}/labels",
                            {"name": name, "color": color, "description": description})
        elif status >= 400:
            raise DegradedReadError(f"GitHub returned {status} checking the '{name}' label")

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
            i["severity"] = parse_severity(i["body"])
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

    def list_head_check_runs(self, ref: str) -> list:
        """Every CI check-run on `ref` (a branch name or SHA), paginated to exhaustion. The response is the
        wrapped `{total_count, check_runs: [...]}` object (unlike the bare-array issues endpoint), so the
        inner `check_runs` key is extracted each page. RAISES DegradedReadError on ANY HTTP error OR an
        unexpected shape — a partial/failed read must never be mistaken for a complete 'these are the
        checks' set (which would let the caller wrongly auto-resolve a still-red check's Issue)."""
        out, page = [], 1
        while True:
            # filter=latest (GitHub's default, requested explicitly) returns only the MOST RECENT run per
            # check name, so a re-run's fresh green supersedes a stale red of the same name — the current
            # outcome, which is what a live-derived signal reconciles against.
            status, data = self._transport(
                "GET", f"/repos/{self.repo}/commits/{ref}/check-runs?filter=latest&per_page=100&page={page}",
                None)
            if status >= 400 or not isinstance(data, dict) or "check_runs" not in data:
                raise DegradedReadError(f"GitHub returned {status} reading check-runs for {ref}")
            runs = data["check_runs"]
            out.extend(runs)
            if len(runs) < 100:
                break
            page += 1
        return out


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
        state_path: str | None = None, *, authoritative, live: bool = False) -> Report:
    """One telemetry pass: ensure the label, read the open engine Issues, reconcile, apply the Plan's
    writes, persist the cache, and compute the refreshed State debt + the render-only pressure line.

    `authoritative` (REQUIRED — see AUTHORITATIVE_ALL) is the set of source-ids this pass may auto-resolve;
    it is threaded to reconcile so a partial live pass never closes an Issue it did not observe. A caller
    that could not build a complete records set (a failed/partial read) MUST pass frozenset() so the pass
    closes nothing. `live` (threaded to reconcile) marks a durable-read signal (e.g. the CI check-runs) that
    promotes/resolves on first observation rather than via the cache-accrued persistence latency.

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

    plan = reconcile(records, open_issues, cache.load(), thresholds, now,
                     authoritative=authoritative, live=live)
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


def _consolidation_note(survivor_number: int) -> str:
    """The plain-language line prepended to a duplicate Issue as it is closed and folded into the
    canonical one — so an operator who had the duplicate open sees why it vanished, not a silent close.
    Peer voice: it states what happened and where tracking continues, no backstage vocabulary."""
    return (f"*Consolidated into #{survivor_number} — this is a duplicate of the same thing the engine "
            f"noticed; tracking continues there.*\n\n")


def promote_finding(github: GitHubIssues, record: dict, now: str, *, title: str | None = None,
                    body_core: str | None = None):
    """Promote ONE finding to a tracked engine Issue — the out-of-band "log it" relay a producer hands
    a single concern to for durable tracking WITHOUT running a full triage pass. Close (slice 22) calls
    it at cap-exhaustion / fail-open to degrade a still-undispositioned finding to logged (never lost);
    the soft-finding promoter (audit_soft_promote) calls it to track a standing length-budget nudge.

    Open-or-update-and-CONVERGE, deduped by `source_id` (the same source-keyed dedup `run` uses, via
    list_open_engine_issues + the body sentinel). GitHub has no atomic create-if-absent, so two rapid
    firings of ONE signal can each open an Issue (a create/create race — this is how #433/#434 arose);
    this heals that by keeping the LOWEST-numbered match as the canonical survivor, folding every
    same-signal duplicate into it and closing them, and PRESERVING the earliest first-noticed across the
    group (never resetting it to `now`). It still does **no auto-resolve** of OTHER signals: unlike
    `run`, it never closes an Issue for a DIFFERENT source_id, so logging one finding can never silently
    close every other open engine Issue — the exact hazard that bars `run([one_finding])`. It is
    **cache-free and State-free**: a one-shot surfacing, not a triage pass, so it never disturbs `run`'s
    persistence accrual or the committed debt cursor.

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
    # Fold the producer's optional lane-aware framing ONTO the record, so the one shared assembly point
    # (issue_title / issue_body) renders it — promote_finding no longer carries its own renderer that could
    # drift from reconcile's. A record already carrying title/body_core fields (the soft-finding promoter's)
    # is honoured either way; the kwargs are the out-of-band callers' path.
    if title is not None or body_core is not None:
        record = {**record, **({"title": title} if title is not None else {}),
                  **({"body_core": body_core} if body_core is not None else {})}
    sid = derive_source_key(record)
    ttl = issue_title(record)

    def _render(first_seen: str) -> str:
        return issue_body(record, first_seen, now)

    try:
        github.ensure_label()
        matches = sorted((i for i in github.list_open_engine_issues() if i.get("source_id") == sid),
                         key=lambda i: i["number"])
        if matches:
            survivor = matches[0]  # lowest number = the first the operator saw = the canonical one
            # Preserve the EARLIEST first-noticed across the whole group — never reset it to `now`. The
            # durable first-seen lives in the Issue bodies (parse_first_noticed), so a cache-free promote
            # recovers it there; ISO-`Z` timestamps make plain min() chronological.
            seen = [s for s in (parse_first_noticed(i.get("body") or "") for i in matches) if s]
            if record.get("first_seen"):
                seen.append(record["first_seen"])
            first_seen = min(seen) if seen else now
            github.update_issue(survivor["number"], _render(first_seen))
            # Converge a create/create race: fold every same-signal duplicate into the survivor and close
            # it, so a recurring signal amends ONE tracked Issue instead of multiplying (never touches a
            # DIFFERENT source_id — promote_finding is authoritative only for the sid it is promoting).
            for dup in matches[1:]:
                github.update_issue(dup["number"], _consolidation_note(survivor["number"]) + (dup.get("body") or ""))
                github.close_issue(dup["number"])
            return survivor["number"]
        return github.open_issue(ttl, _render(record.get("first_seen") or now)).get("number")
    except DegradedReadError:
        return False


# ---- the findings-inbox emit-and-done seam (F0203 / D-031 / principles §16) ----
# The clean seam D-031 pins (decision-log D-031 + its anti-choice "cognition acting on findings it emits —
# rejected"; principles §16; telemetry README "Findings inbox — cognition emits, telemetry acts"): a cognition
# substrate — a hooks fail-open flag, a boot degradation — EMITS a finding and is DONE; it never holds
# telemetry's acting-mechanism (the GitHub boundary or a triage pass). Telemetry owns the act. SEVERITY sets
# immediacy: a TRUST_CRITICAL signal (a gate/check that could not run) must surface AT ONCE, so it is promoted
# synchronously — telemetry resolving the boundary itself, the §16 un-inversion of today's producer-held
# reach-in; a benign/degraded signal defers, spooled into a telemetry-owned gitignored inbox that a later
# drain pass promotes. This slice stands up the channel and migrates the hooks emitter onto it; the PRODUCTION
# drain cadence is #412 and boot's own emission is #398 (both build ON this seam — see drain_inbox).
# Build-spec leaf (recorded with the maintainer, PR body + memory): the spool is a gitignored NDJSON at the
# path below, beside telemetry's other .cache siblings (ambient.ndjson / *-streams.json).
INBOX_SPOOL_PATH = os.path.join(validate.ROOT, ".engine", "telemetry", ".cache", "findings-inbox.ndjson")
# The drain driver's OWN reconcile-accrual cache — separate from streams.json / ambient-streams.json /
# episodic-streams.json, so a sibling run() (which prunes any sid not currently an open Issue) can never
# clobber the inbox drain's cross-session persistence accrual.
DEFAULT_INBOX_STREAMS_PATH = os.path.join(validate.ROOT, ".engine", "telemetry", ".cache", "inbox-streams.json")

# The broken-runtime marker (build-spec leaf, recorded with the maintainer). The hook LAUNCHER (hook-runner.sh)
# fails BEFORE any Python runs when `.engine/.venv/` is absent, so it cannot reach telemetry to emit a finding;
# instead it best-effort drops this PRESENCE marker (a single file, no schema — the shell only knows the path),
# and the drain-inbox SessionStart driver, on a later session where the runtime is healthy again, converts a
# present marker into ONE TRUST_CRITICAL "could-not-run" finding promoted IMMEDIATELY (hooks/README
# fail-open-and-flag: a missing tool-runtime surfaces as a crash would, on first occurrence, not persistence-
# gated; D-156) and clears it. Presence-based, NOT a parsed format, so the drift test pins the whole shell↔Python
# contract by the path literal alone; the marker's bytes NEVER enter the finding (fixed source_id + fixed
# message), so a poisoned marker cannot forge a signal sentinel. The fixed source_id de-dups across sessions.
RUNTIME_HEALTH_MARKER_PATH = os.path.join(validate.ROOT, ".engine", "telemetry", ".cache", "runtime-health.marker")
RUNTIME_UNHEALTHY_SOURCE_ID = "runtime/tool-runtime-unhealthy"
# A `*.draining` aside is swept back through the drain only when it is older than this — far beyond any real
# drain (a few GitHub calls, seconds) — so the sweep recovers a CRASHED drain's stranded batch without ever
# scavenging a CONCURRENT live drain's in-flight aside (per-PID asides overlap under the multi-session model).
_ASIDE_STALE_SECONDS = 300.0


def emit_finding(record: dict, *, gh: "GitHubIssues | None" = None, spool_path: str = INBOX_SPOOL_PATH):
    """The emit-and-done front door (D-031 / §16). A producer hands ONE finding-record and is DONE — telemetry
    owns the acting-mechanism. Routes by SEVERITY:

      - TRUST_CRITICAL (a gate/check that could not run) → promote IMMEDIATELY; returns the tracked Issue
        number (truthy) on success, or **False** when there is no local GitHub context or the write could not
        land. It is NEVER spooled: a could-not-run signal must surface at once, and a no-token trust-critical
        returns False = surfaced-but-not-durably-recorded — never a false "tracked" claim (the #391 honesty
        law the fail-open copy depends on).
      - anything else (benign / degraded) → APPEND to the telemetry-owned gitignored inbox spool and return
        **False** (a spool append is capture, NOT durable tracking — the caller must not claim tracked). It is
        promoted later by drain_inbox; until the production drain lands (#412) a benign emit is captured but
        not yet surfaced.

    `gh` is an injectable boundary so the demo/tests fake ONLY the network; by default the trust-critical path
    resolves the LOCAL GitHub context (boot.repo_slug/gh_token) itself — the credential resolution the producer
    used to hold (hooks), now telemetry's (the un-inversion). SAFETY BACKSTOP: under a test harness (`unittest`
    in sys.modules) the default (un-injected) trust-critical path refuses to reach live GitHub and returns False
    — boot.gh_token()'s `gh auth token` fallback can resolve a real token even locally (#416-F5), so a direct
    emit_finding test must never open a real Issue. FAIL-OPEN: any error degrades to a no-op (False), never
    raises — emitting a finding must never break the caller (a fail-open hook, a read-only boot)."""
    try:
        if record.get("severity") == TRUST_CRITICAL:
            if gh is None:
                if "unittest" in sys.modules:   # backstop: never reach live GitHub from a test run
                    return False
                from boot import repo_slug, gh_token   # lazy: boot imports telemetry (back-edge; lazy-safe)
                repo, token = repo_slug(), gh_token()
                if not repo or not token:
                    return False
                gh = GitHubIssues(repo, token)
            return promote_finding(gh, record, utc_now())
        return _append_inbox(record, path=spool_path)
    except Exception:  # noqa: BLE001 — emit-and-done must never break the emitting caller
        return False


def _append_inbox(record: dict, *, path: str = INBOX_SPOOL_PATH) -> bool:
    """Append a benign/degraded finding-record to the telemetry-owned inbox spool (gitignored NDJSON),
    best-effort. Always returns False — a spool append is emit-and-done CAPTURE, never durable tracking
    (drain_inbox promotes it later). Swallows OSError (mirrors Cache.store): the spool is best-effort and a
    write failure must never break the emitting caller."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError:
        pass
    return False


def _take_inbox(spool_path: str):
    """Atomically CLAIM the spool for draining: rename it aside (os.replace is atomic) into a PER-PROCESS
    aside name, so (a) a concurrent emit appends into a FRESH spool and is never truncated away, and (b) two
    concurrent drains — separate sessions, separate pids under this repo's multi-session actor model — claim
    into DISTINCT asides and never overwrite each other's batch. Returns `(records, aside_path)` — the parsed
    records (a corrupt line skipped, per-line tolerant) and the aside file to dispose of; `([], None)` when the
    spool is absent (nothing to drain).

    Residual bound (handed to #412, the production-drain owner): a HARD crash between the claim and the drain's
    dispose strands that batch in its per-process aside — recoverable (the data is on disk, not overwritten),
    but nothing re-reads it until a sweep runs. The production drain cadence (#412) must sweep stale
    `*.draining` asides on start-up; this fixture-exercised builder does not, and never claims it does."""
    aside = f"{spool_path}.{os.getpid()}.draining"   # per-process: concurrent drains never clobber each other
    try:
        os.replace(spool_path, aside)   # atomic claim; FileNotFoundError when the spool is absent
    except (FileNotFoundError, OSError):
        return [], None
    records = []
    try:
        with open(aside, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue   # per-line tolerant: a corrupt spool line is skipped, not fatal
    except OSError:
        pass
    return records, aside


def drain_inbox(github: GitHubIssues, *, cache: Cache, thresholds: dict, now: str,
                spool_path: str = INBOX_SPOOL_PATH):
    """Drain the findings-inbox spool: read the spooled emit-and-done findings, run ONE triage pass over them,
    and dispose of the drained batch. The benign counterpart to emit_finding's immediate trust-critical path —
    what promotes a spooled degraded finding (a boot degradation, #398) to a tracked Issue. The PRODUCTION
    cadence that calls this is #412; this slice builds and fixture-exercises it. Returns the run Report, or
    None when there was nothing promotable to drain.

    live=FALSE: a spooled batch is a set of one-shot emissions, NOT a complete current-state snapshot, so it
    carries NO clearance events. Persistence accrues across drains in the cache (like ambient), and
    `authoritative` is scoped to the drained source-ids ONLY — so a drain never auto-closes another source's
    Issue, AND a spooled finding never auto-resolves (there is no positive-clearance emission; a safe
    asymmetry #412 must not "fix" by widening authority).

    SPOOL-BOUNDARY VALIDATION: the in-memory source_id_is_marker_safe guarantee does not survive the spool, so
    each drained record is re-validated here and an unsafe one is DROPPED — and `authoritative` is built from
    the VALIDATED records actually fed to run(), never the raw lines (a dropped line's sid must not sit in
    authoritative, which would wrongly auto-close its Issue). On a GitHub degrade the validated batch is
    re-spooled (promotion is idempotent, so it re-drains cleanly next pass) — nothing promotable is lost.
    CONCURRENCY: the claim is a per-process atomic rename (see _take_inbox), so a concurrent emit or a
    concurrent drain never clobbers this batch; a hard crash mid-drain strands (never overwrites) the batch —
    #412's cadence owns sweeping a stranded aside. Fail-open: an absent/unreadable spool drains nothing."""
    records, aside = _take_inbox(spool_path)
    if aside is None:
        return None
    valid = [r for r in records if isinstance(r, dict) and source_id_is_marker_safe(r.get("source_id"))]
    if not valid:
        _dispose_aside(aside)   # only empty / corrupt / marker-unsafe lines — nothing promotable
        return None
    authoritative = frozenset(r["source_id"] for r in valid)
    report = run(github, valid, cache, thresholds, now, authoritative=authoritative, live=False)
    if report.degraded:
        _restore_inbox(valid, spool_path)   # re-spool only the promotable records; idempotent re-drain
    _dispose_aside(aside)
    return report


def _dispose_aside(aside: str) -> None:
    try:
        os.remove(aside)
    except OSError:
        pass


def _restore_inbox(records: list, spool_path: str) -> None:
    """Re-append a claimed-but-undrained batch to the live spool after a GitHub degrade, so nothing is lost
    (it re-drains next pass; promotion is idempotent). Appends to the CURRENT spool, never renames the aside
    back — a concurrent emit may have created a fresh spool in the meantime."""
    try:
        os.makedirs(os.path.dirname(spool_path), exist_ok=True)
        with open(spool_path, "a", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r, sort_keys=True) + "\n")
    except OSError:
        pass


def _runtime_marker_message() -> str:
    """The plain-language operator line for a broken tool-runtime finding. Engine's-own-health, not the
    operator's project; names the absent runtime (no stack trace exists — the launcher failed pre-Python);
    honest that it is a PAST condition retired by review / the provisioning fix (never self-closing); no
    backstage vocabulary (no marker / spool / drain / severity)."""
    return ("The engine couldn't run its own tools during a recent session — its private Python environment "
            "wasn't ready, so one of its safety checks could not run and that session's work was not verified. "
            "This is about the engine's own setup, not your project, and it changes nothing on its own. If it "
            "keeps happening, the engine's environment needs to be set up again; once that's done you can "
            "retire this note.")


def promote_runtime_marker(github: GitHubIssues, *, marker_path: str = RUNTIME_HEALTH_MARKER_PATH) -> bool:
    """Convert a present broken-runtime marker into ONE tracked finding. The hook launcher drops the marker
    when it cannot start the engine's Python (see RUNTIME_HEALTH_MARKER_PATH); here — on a session where the
    runtime IS healthy — that becomes a TRUST_CRITICAL "could-not-run" finding, promoted IMMEDIATELY (never
    spooled/persistence-gated: hooks/README fail-open-and-flag, a missing tool-runtime surfaces as a crash
    would). The marker is cleared ONLY on a successful promote, so a no-token / GitHub-unreachable pass leaves
    it to retry next session (nothing lost). De-duped across sessions by the fixed source_id. Presence-only:
    the marker's bytes never enter the finding. Returns True when a marker was promoted. Fail-open: any error
    is a no-op (False) — surfacing a finding must never break the session-start driver."""
    try:
        if not os.path.exists(marker_path):
            return False
        record = {"source_id": RUNTIME_UNHEALTHY_SOURCE_ID, "severity": TRUST_CRITICAL,
                  "message": _runtime_marker_message(), "location": None}
        if emit_finding(record, gh=github):   # trust-critical -> promote_finding at once; truthy on success
            try:
                os.remove(marker_path)         # clear ONLY after a durable promote; else leave it to retry
            except OSError:
                pass
            return True
    except Exception:  # noqa: BLE001 — fail-open: a marker promote must never break the driver
        pass
    return False


def _sweep_stranded_asides(spool_path: str = INBOX_SPOOL_PATH, *, min_age_seconds: float = _ASIDE_STALE_SECONDS) -> int:
    """Recover a crashed drain's stranded batch (the residual _take_inbox hands to #412): re-append any
    mtime-STALE `<spool>.<pid>.draining` aside back to the LIVE spool, so the next drain_inbox picks it up
    through the SINGLE validation + authoritative-scoping path (never a second run() that could widen
    authority). AGE-GATED: only asides older than `min_age_seconds` — far beyond any real drain — are swept,
    so a CONCURRENT live drain's in-flight aside is never scavenged. A double-sweep of one aside re-appends
    idempotently (promotion is source_id-deduped), so the un-claimed read/remove race is harmless. Returns the
    count swept. Fail-open throughout."""
    directory = os.path.dirname(spool_path)
    prefix = os.path.basename(spool_path) + "."
    try:
        names = os.listdir(directory)
    except OSError:
        return 0
    now_ts = time.time()
    swept = 0
    for name in names:
        if not (name.startswith(prefix) and name.endswith(".draining")):
            continue
        aside = os.path.join(directory, name)
        try:
            if now_ts - os.path.getmtime(aside) < min_age_seconds:
                continue   # young enough to be a live concurrent drain's aside — never scavenge it
            with open(aside, encoding="utf-8") as fh:
                lines = [ln for ln in fh if ln.strip()]
            os.makedirs(directory, exist_ok=True)
            with open(spool_path, "a", encoding="utf-8") as out:
                for ln in lines:
                    out.write(ln if ln.endswith("\n") else ln + "\n")
            os.remove(aside)
            swept += 1
        except OSError:
            continue
    return swept


# ---- the CI-outcome signal (the first "signal of record"; native, no bespoke ledger) ----

# The source-id namespace for a CI-outcome signal. Keeping every producer's ids namespaced (here `ci/`)
# lets a live pass declare exactly which sources it observed and is authoritative to auto-resolve.
CI_NAMESPACE = "ci/"

# The check-run conclusions that count as "not passing" — a definitively-failed outcome — and, separately,
# the one that counts as a definitive PASS. A CI signal is severity PERSISTENT_BENIGN (the check RAN and
# reported — not a "could not run" signal), but it is a LIVE-DERIVED signal (read fresh from the durable
# GitHub record each pass), so it promotes on first observed failure and resolves on first observed pass —
# its state lives in the durable Issue, not the ephemeral cache the persistence latency would need.
CI_NOT_PASSING = frozenset({"failure", "timed_out", "cancelled", "action_required"})
CI_PASSING = frozenset({"success"})
# Only a DEFINITIVE conclusion (a pass or a failure) makes a check authoritative — i.e. lets this pass
# auto-resolve a stale Issue for it. `skipped` / `neutral` / `None` (still running) / anything else is
# INDETERMINATE: it neither promotes (it is not a failure) nor authorises auto-resolve (it is not a
# definitive pass), so a formerly-red Issue is carried forward, never wrongly closed on a check that merely
# stopped running.


def _ci_message(name: str) -> str:
    """The plain-language operator line for a not-passing check. No backstage vocabulary — it says, in
    non-engineer terms, that one of the engine's own checks on the main branch keeps reporting a failure."""
    return (f"One of the checks that runs on your main branch — “{name}” — keeps reporting a "
            "failure. This is about the engine's own checks, not your product; it stays tracked here until "
            "that check is passing again.")


def derive_ci_records(github: GitHubIssues, ref: str, now: str):
    """Derive telemetry finding-records from the CI check-runs on the default branch's head — the first
    signal of record (the protected-branch checks are the authoritative pass/fail). Reads the FULL current
    check-run set (list_head_check_runs pages to exhaustion and raises DegradedReadError on any read error).
    Returns `(records, authoritative)`:
      - `records`: one PERSISTENT_BENIGN record per check whose conclusion is not-passing, keyed
        `source_id = "ci/{name}"`.
      - `authoritative`: the set of `"ci/{name}"` for EVERY check OBSERVED this pass (any conclusion) — the
        source-ids this pass is authoritative to auto-resolve. So a check that ran and PASSED is in
        `authoritative` but not in `records` -> its Issue (if any) auto-resolves; a check ABSENT from the
        head entirely (e.g. it did not run on a squash-merge SHA) is in NEITHER -> it is neither promoted nor
        auto-closed (absent != failing, and absent != resolved).
    A check whose derived source-id is not marker-safe (a crafted check name) is SKIPPED — dropped from both
    records and authoritative — so it is never promoted and can never crash the pass. `now` is unused here
    (the record carries no timestamp; reconcile stamps first/last-seen) but kept for signature symmetry with
    the other producers. Raises DegradedReadError to the caller, which must then claim frozenset()."""
    runs = github.list_head_check_runs(ref)
    records, authoritative = [], set()
    for run_obj in runs:
        name = run_obj.get("name")
        if not isinstance(name, str) or not name:   # a check-run always has a name; skip a malformed one
            continue
        sid = f"{CI_NAMESPACE}{name}"
        if not source_id_is_marker_safe(sid):
            continue
        conclusion = run_obj.get("conclusion")
        if conclusion in CI_NOT_PASSING:
            authoritative.add(sid)
            records.append({"source_id": sid, "severity": PERSISTENT_BENIGN,
                            "message": _ci_message(name), "location": None})
        elif conclusion in CI_PASSING:
            authoritative.add(sid)   # a definitive pass -> authoritative so any stale red Issue resolves
        # else: indeterminate (skipped/neutral/still-running) -> neither promoted nor authoritative
    return records, authoritative


# ---- the ambient signal (best-effort local check-fires; the second signal of record) ----

# The source-id namespace for an ambient signal (parallel to `ci/`). Ambient capture is telemetry's second
# native signal: the local checks that run while you work append their fire + pass/fail (read from the check's
# verdict, NOT the hook's exit) to a gitignored cache, and this reader derives persistent-warning finding-
# records from it. It is best-effort and never complete (a local run is skippable), so nothing treats it as a
# guarantee — a reliable signal is read from CI or a tracked Issue, never assumed from this cache (spec :45-48).
AMBIENT_NAMESPACE = "ambient/"
# Telemetry OWNS the ambient record shape (ambient-capture.v1) and where the cache lives. The cache path is a
# build-spec leaf decided here: an NDJSON append-log (one check-fire per line), a SEPARATE file from the
# reconcile persistence cache (streams.json) — raw fires vs derived accrual. It sits under the already whole-
# dir-gitignored `.engine/telemetry/.cache/`, so it never commits and no new gitignore wire is owed.
DEFAULT_AMBIENT_CACHE_PATH = os.path.join(validate.ROOT, ".engine", "telemetry", ".cache", "ambient.ndjson")
# The ambient driver's OWN reconcile-accrual cache — separate from the CI run's streams.json, so a local CI
# `run` (which prunes any sid not currently an open Issue) can never clobber ambient's cross-session accrual.
DEFAULT_AMBIENT_STREAMS_PATH = os.path.join(validate.ROOT, ".engine", "telemetry", ".cache",
                                            "ambient-streams.json")
# The freshness WATERMARK: the observed_at of the newest fire the last pass consumed. Each pass considers only
# fires NEWER than it for PROMOTION, so persistence counts genuinely FRESH re-fails across sessions — a one-time
# transient fail that is never re-run does not climb to promotion (the persistence threshold is patience, not a
# stale replay). Stored as one UTC-Z line in its own gitignored file.
DEFAULT_AMBIENT_WATERMARK_PATH = os.path.join(validate.ROOT, ".engine", "telemetry", ".cache",
                                              "ambient-watermark")


def _ambient_target_exists(target: str) -> bool:
    """Whether an ambient fire's recorded target still exists. The target is stored RELATIVE to the repo root
    (evaluate_touched_fires → os.path.relpath(…, ROOT)), so it MUST be resolved against ROOT, never the process
    CWD — a SessionStart hook or a manual `run-ambient` from a subdirectory would otherwise mis-read a still-
    present file as vanished and wrongly auto-resolve a still-failing check's Issue (a false all-clear)."""
    return os.path.exists(os.path.join(validate.ROOT, target))


def load_ambient_watermark(path: str = DEFAULT_AMBIENT_WATERMARK_PATH) -> str:
    """The last consumed fire's observed_at (a UTC-Z string), or "" when absent — on "" every fire is fresh
    (a first run promotes nothing extra: each rule's first observation is persist=1)."""
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def store_ambient_watermark(observed_at: str, path: str = DEFAULT_AMBIENT_WATERMARK_PATH) -> None:
    """Persist the new watermark — best-effort (a failure just re-considers some fires fresh next pass, which
    at worst nudges one extra persistence increment; never raises)."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write((observed_at or "") + "\n")
    except OSError:
        pass


def ambient_record(rule_id: str, passed: bool, target, observed_at: str) -> dict:
    """One ambient-capture.v1 check-fire record: a named check ran locally and did/didn't pass, plus the
    touched target (or None when the moment names none) and when. Deliberately NOT a finding — a signal
    source telemetry later derives findings from (the promotion happens in derive_ambient_records)."""
    return {"rule_id": rule_id, "outcome": "pass" if passed else "fail",
            "target": target, "observed_at": observed_at}


def append_ambient(records: list, path: str = DEFAULT_AMBIENT_CACHE_PATH) -> None:
    """Append check-fire records to the gitignored NDJSON cache — best-effort: it makes the dir, appends one
    JSON object per line, and swallows any OSError. It NEVER raises into the caller (the PostToolUse hot
    path); a lost fire is acceptable (the capture is never a guarantee)."""
    if not records:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec, sort_keys=True) + "\n")
    except OSError:
        pass


def load_ambient(path: str = DEFAULT_AMBIENT_CACHE_PATH) -> list:
    """Read the NDJSON check-fire log, tolerant PER LINE: a corrupt line is skipped, not the whole file (so
    one bad byte never discards all accrual). An absent/unreadable file reads as [] (best-effort)."""
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return out


def capture_touched_fires(paths: list, now: str, *, cache_path: str = DEFAULT_AMBIENT_CACHE_PATH) -> int:
    """The PostToolUse ambient writer (telemetry OWNS the record + cache; the check-running hook RELAYS).
    For each local check that evaluated a touched file, append a check-fire record with its pass/fail (read
    from the check's verdict via validate.evaluate_touched_fires, not the hook's exit). Best-effort — the
    file work is swallowed by append_ambient and the caller wraps this too, so it never breaks a tool call.
    Returns the number of fires recorded."""
    fires = validate.evaluate_touched_fires(paths)
    records = [ambient_record(rid, passed, target, now) for (rid, passed, target) in fires]
    append_ambient(records, cache_path)
    return len(records)


def _ambient_message(rule_id: str) -> str:
    """The plain-language operator line for a persistently-failing local check. No backstage vocabulary; it is
    honest that ambient reflects the LAST local run and may be stale (the capture is best-effort, and absence
    is never read as resolved — it clears only when the check is seen passing again or its file is gone)."""
    return (f"One of the engine's own checks — “{rule_id}” — was failing the last time it ran locally while "
            "you were working (it may be stale if you haven't touched that area since). This is about the "
            "engine's own checks, not your project, and it changes nothing on its own; it stays tracked here "
            "until that check is seen passing again.")


def derive_ambient_records(path: str = DEFAULT_AMBIENT_CACHE_PATH, watermark: str = "", *,
                           exists=_ambient_target_exists):
    """Derive telemetry finding-records from the ambient check-fire cache — a CACHE-ACCRUED signal (live=False).
    Returns `(records, authoritative, new_watermark)`. Two distinct reads over the NDJSON log, deliberately so:

    PROMOTION is FRESH-gated. Only fires NEWER than `watermark` count — the latest FRESH fire per rule wins. A
    rule whose fresh latest is a FAIL (and whose target still exists) becomes a PERSISTENT_BENIGN record keyed
    `source_id = "ambient/{rule_id}"`; every FRESHLY-observed rule (pass or fail) is `authoritative`. So a rule
    freshly seen PASSING is authoritative-not-a-record → its Issue auto-resolves (a POSITIVE clearance, never
    assumed from absence). Because only fresh fails feed the persistence count, a one-time/transient fail that
    is never re-run does NOT climb to promotion — the persistence threshold stays what it is meant to be, a
    patience window over genuinely recurring fresh fails, not a stale replay of one observation.

    VANISHED-TARGET resolution reads the FULL cache (freshness-independent): a rule whose LATEST fire overall is
    a fail but whose target file no longer exists is authoritative-to-resolve — the source is objectively gone,
    a real positive observation, so its Issue clears even if the operator never re-runs it.

    A rule with a stale still-failing fire (target exists, not freshly re-observed) is in NEITHER records NOR
    authoritative → carried forward UNTOUCHED: an already-open Issue stays open honestly (the copy says it may
    be stale), and it is never wrongly resolved on mere absence. A rule wiped from the cache is likewise carried
    forward. `authoritative` is the OBSERVED-scoped `ambient/` set, NEVER AUTHORITATIVE_ALL — the cache is
    per-machine and never complete, so a pass may only ever auto-resolve the `ambient/` Issues it actually saw.

    `new_watermark` is the newest observed_at consumed (>= the old one) for the caller to persist. A rule whose
    derived source-id is not marker-safe is SKIPPED. `exists` is injectable for tests (default root-anchored)."""
    fresh_latest, full_latest, new_wm = {}, {}, watermark or ""
    for f in load_ambient(path):
        if not isinstance(f, dict):
            continue
        rid = f.get("rule_id")
        ts = str(f.get("observed_at") or "")
        if not rid or f.get("outcome") not in ("pass", "fail"):
            continue
        if full_latest.get(rid) is None or ts >= str(full_latest[rid].get("observed_at") or ""):
            full_latest[rid] = f
        if ts > (watermark or ""):                       # fresh: newer than the last consumed fire
            if ts > new_wm:
                new_wm = ts
            if fresh_latest.get(rid) is None or ts >= str(fresh_latest[rid].get("observed_at") or ""):
                fresh_latest[rid] = f
    records, authoritative = [], set()
    for rid, full in full_latest.items():
        sid = f"{AMBIENT_NAMESPACE}{rid}"
        if not source_id_is_marker_safe(sid):
            continue
        target = full.get("target")
        outcome = full.get("outcome")
        # RESOLUTION reads the FULL cache (a rule's current true state is STABLE across passes, so auto-resolve
        # can accrue its `absent` count): a positive clearance is the latest fire being a PASS, or a FAIL whose
        # target file has objectively vanished. Never assumed from mere absence.
        if outcome == "pass" or (outcome == "fail" and target and not exists(target)):
            authoritative.add(sid)
            continue
        # PROMOTION reads only FRESH fires (anti-transient): a still-failing rule becomes a record only when its
        # FRESH latest is a fail whose target exists, so a one-time fail never re-run does not climb persistence.
        fresh = fresh_latest.get(rid)
        if fresh is not None and fresh.get("outcome") == "fail":
            ftarget = fresh.get("target")
            if not ftarget or exists(ftarget):
                records.append({"source_id": sid, "severity": PERSISTENT_BENIGN,
                                "message": _ambient_message(rid),
                                "location": {"file": ftarget, "line": None} if ftarget else None})
    return records, frozenset(authoritative), new_wm


# ---- the episodic (memory-ledger) signal (403.4 / F0210) -------------------
# The THIRD signal of record: the memory ledger's CONSOLIDATION BACKLOG — earlier sessions whose raw notes were
# never folded into short summaries (the abandoned-session recovery the memory durability law names). Telemetry
# only COUNTS this content-free structural signal (a list of session-ids, never record CONTENT) and, when the
# tidy-up stays chronically behind across sessions, tracks ONE engine issue; it never writes engine-state back
# into the ledger (the ledger holds project narrative recall only — D-039). The ledger is local + gitignored
# like the ambient cache, so this is a CACHE-ACCRUED (live=False) signal driven by a LOCAL SessionStart verb,
# never the ephemeral audit runner (which has no ledger). It reuses memory's public detect leaf rather than
# re-deriving the scan (the exclusions — live session, lease staleness, injected pseudo-turns — live there).
EPISODIC_NAMESPACE = "episodic/"
EPISODIC_BACKLOG_SID = EPISODIC_NAMESPACE + "consolidation-backlog"   # ONE stable id: the CONDITION, not per-session
# A signal-DEFINITION constant (like CI_NOT_PASSING), NOT a D-114-tunable policy threshold: the point at which a
# lagging tidy-up counts as "deep". Intentionally aligned with memory's in-session-nag `_BACKLOG_ALARM_THRESHOLD`
# for a coherent operator story, but INDEPENDENTLY owned so a later change to memory's chat nudge can never
# silently move telemetry's durable-issue promotion floor — the two are distinct control surfaces.
EPISODIC_BACKLOG_THRESHOLD = 5
# The episodic driver's OWN reconcile-accrual cache — separate from streams.json / ambient-streams.json, so no
# other local `run` can clobber its cross-session persistence accrual.
DEFAULT_EPISODIC_STREAMS_PATH = os.path.join(validate.ROOT, ".engine", "telemetry", ".cache",
                                             "episodic-streams.json")


def _episodic_message(count: int) -> str:
    """The plain-language operator line for a chronically-lagging memory tidy-up. No backstage vocabulary
    (no ledger/sweep/lease/turn-delta/stream/severity). It names the engine's OWN housekeeping and explicitly
    negates the operator's project (spec :87-89 — every engine issue reads as the engine noticing something
    about its OWN health, "not a problem with the operator's product"; "memory" would otherwise read as the
    operator's own data). It keeps the content-floor promise (the raw notes stay safe and recoverable) and is
    honest about the STALL — it does NOT re-promise the catch-up, because this issue exists only after the
    in-session catch-up ran across several sessions and did not clear it."""
    sessions = "session" if count == 1 else "sessions"
    return (f"The engine's own memory housekeeping has fallen behind — this is about the engine's internal "
            f"notes on our work together, not your project or its data. {count} earlier {sessions} still have "
            "raw notes that haven't been folded into short summaries. Nothing is lost: those notes are safe and "
            "fully recoverable, and recall still works. The routine catch-up hasn't been keeping up across the "
            "last several sessions; the engine's next self-review will look at why. This item retires on its "
            "own once the backlog is genuinely cleared.")


def derive_episodic_records(live_session_id: str | None = None, cwd: str | None = None):
    """Derive telemetry finding-records from the memory ledger's consolidation backlog — a CACHE-ACCRUED signal
    (live=False), re-derived as a CURRENT-STATE snapshot each pass. Returns `(records, authoritative)`. NO
    watermark, unlike ambient: this is a live count of the CURRENT backlog (like never-fired), not an append-log
    of historical fires, so there is no stale-replay to fence — each pass is a genuine fresh observation and the
    persistence latency lives in the reconcile cache.

    Reuses memory's public leaf `consolidate.detect_unconsolidated` (a content-free sorted list of session-id
    strings with raw notes but no consolidation marker), so the read never touches record CONTENT and the D-039
    boundary holds. Emits ONE record keyed to the stable `EPISODIC_BACKLOG_SID` when the backlog is deep.

    POSITIVE-OBSERVATION GATE (safety). `detect_unconsolidated` returns an empty list for FOUR non-raising
    states — genuinely clear, an absent/empty ledger, a corrupt lease sidecar, and all-sessions-live — so an
    empty list ALONE is not proof the backlog cleared. Because the ledger is PER-MACHINE but the tracked issue
    is GLOBAL, claiming authority on an unobserved (absent/empty) pass would let a fresh worktree auto-close a
    real backlog issue raised elsewhere. So this pass is `authoritative` for the episodic source ONLY when it
    POSITIVELY observed a usable ledger: the ledger file is present with >=1 record AND the lease sidecar is
    readable (not corrupt). Otherwise `authoritative = frozenset()` — it closes nothing, carrying an open issue
    forward untouched. Promotion stays monotone-safe (a deep backlog only ever promotes); only the absent-CLOSE
    is gated on a real observation — mirroring ambient's observed-scoped authority (never a fixed claim on an
    unobserved pass). Content-level corruption (skipped malformed lines) can only UNDER-count and self-heals on
    the next append — an accepted, documented bound, not gated."""
    from memory import capture, consolidate, ledger   # lazy: the canonical intra-core import (no cycle)
    try:
        # The WHOLE read is fail-safe: an unreadable memory environment (probe OR the backlog scan) must
        # close nothing, never crash the pass — so `detect_unconsolidated` is inside the guard too.
        observed = (any(True for _ in ledger.iter_records(path=ledger.ledger_path(cwd)))
                    and capture.read_lease_state(ledger.ledger_dir(cwd)) is not None)
        if not observed:
            return [], frozenset()   # no trustworthy observation on this machine → resolve nothing
        pending = consolidate.detect_unconsolidated(live_session_id, cwd=cwd)
    except Exception:  # noqa: BLE001
        return [], frozenset()
    records = []
    if len(pending) >= EPISODIC_BACKLOG_THRESHOLD:
        records = [{"source_id": EPISODIC_BACKLOG_SID, "severity": PERSISTENT_BENIGN,
                    "message": _episodic_message(len(pending)), "location": None}]
    return records, frozenset({EPISODIC_BACKLOG_SID})


# ---- the operator demo (faked GitHub, REAL reconcile logic) ----------------

class _FakeGitHub:
    """An in-memory stand-in for GitHub used ONLY by the demo: it records issues in a dict and serves
    the (method, path, body) transport contract. The harness it drives is the REAL GitHubIssues +
    reconcile — only the network is faked (the demo-fidelity rule)."""

    def __init__(self, *, fail_status: int | None = None, check_runs: list | None = None):
        self.issues: dict = {}
        self.labels: set = set()
        self._next = 1
        self.fail_status = fail_status
        self.check_runs = check_runs if check_runs is not None else []

    def transport(self, method, path, body):
        if self.fail_status and ("/issues" in path or "/check-runs" in path) and method == "GET":
            return self.fail_status, None
        if "/check-runs" in path and method == "GET":
            page = int(re.search(r"[?&]page=(\d+)", path).group(1)) if "page=" in path else 1
            rows = self.check_runs if page == 1 else []
            return 200, {"total_count": len(self.check_runs), "check_runs": rows}
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
        r = run(gh, [benign], cache, th, clock[k], authoritative=AUTHORITATIVE_ALL)
        print(f"    fire {k+1}: opened={r.opened} updated={r.updated} closed={r.closed} "
              f"-> open issues now: {sum(1 for i in fake.issues.values() if i['state']=='open')}")

    print("\n(2) It crosses the threshold on the 3rd fire — ONE issue opens:")
    r = run(gh, [benign], cache, th, clock[2], authoritative=AUTHORITATIVE_ALL)
    open2 = sum(1 for i in fake.issues.values() if i['state'] == 'open')
    print(f"    fire 3: opened={r.opened} -> open issues now: {open2}")

    print("\n(3) The SAME signal fires again 3 more times — the one issue is UPDATED, never duplicated:")
    for k in range(3, 6):
        r = run(gh, [benign], cache, th, clock[k], authoritative=AUTHORITATIVE_ALL)
        print(f"    re-fire: opened={r.opened} updated={r.updated} -> open issues now: "
              f"{sum(1 for i in fake.issues.values() if i['state']=='open')}  (still one — dedup holds)")
    open3 = sum(1 for i in fake.issues.values() if i['state'] == 'open')

    print("\n(4) A DIFFERENT, trust-critical signal fires once — it opens immediately (no waiting):")
    crit = _rec("check/protection", TRUST_CRITICAL,
                "A safety check could not run, so the engine may be unable to catch a bad change.")
    r = run(gh, [benign, crit], cache, th, clock[6], authoritative=AUTHORITATIVE_ALL)
    open4 = sum(1 for i in fake.issues.values() if i['state'] == 'open')
    print(f"    opened={r.opened} -> open issues now: {open4}  (two distinct signals, two issues)")

    print("\n(5) The cause is removed — after auto_resolve absent observations the benign issue closes:")
    for k in range(2):
        r = run(gh, [crit], cache, th, clock[7], authoritative=AUTHORITATIVE_ALL)  # benign absent; crit still firing
        print(f"    absent {k+1}: closed={r.closed} -> open issues now: "
              f"{sum(1 for i in fake.issues.values() if i['state']=='open')}")

    print("\n(6) GitHub is unreachable — the read FAILS rather than reading 'no issues', and we fall")
    print("    back to the committed offline count with an honest line (never a silent or wrong zero):")
    down = GitHubIssues("you/your-project", "demo-token", transport=_FakeGitHub(fail_status=403).transport)
    r6 = run(down, [benign], cache, th, clock[0], state_path=None, authoritative=AUTHORITATIVE_ALL)
    print("    " + r6.degraded_line)

    print("\n(7) A sample of the engine-opened issue, exactly as it appears in your tracker "
          "(read it for jargon):")
    sample = next(i for i in fake.issues.values())
    print("    ┌─ TITLE: " + issue_title(crit))
    for line in issue_body(crit, clock[6], clock[6]).split("\n"):
        print("    │ " + line)
    print("    └─ (the last line is an invisible marker; it does not render in GitHub)")

    print("\n(8) The FIRST live signal source — your main branch's CI checks. Read fresh from the durable")
    print("    GitHub record each pass, a failing check is tracked at once and clears the moment it is green")
    print("    — with NO reliance on a saved counter; and a CI-only pass NEVER touches an unrelated item it")
    print("    did not look at (the source-scoping safety rail):")
    ci_fake = _FakeGitHub(check_runs=[{"name": "engine-ci", "conclusion": "failure"},
                                      {"name": "actionlint", "conclusion": "success"}])
    ci_gh = GitHubIssues("you/your-project", "demo-token", transport=ci_fake.transport)
    ci_cache = Cache(os.path.join(validate.ROOT, ".engine", "telemetry", ".cache", "_demo_ci.json"))
    try:
        os.remove(ci_cache.path)
    except OSError:
        pass
    cclock = ["2026-06-06T0%d:00:00Z" % n for n in range(1, 9)]
    # An UNRELATED, out-of-band item (a "gate could not run" alarm) is opened directly, the way a hook does.
    oob = _rec("hooks/fail-open/PreToolUse/modes", TRUST_CRITICAL, "A safety gate could not run this session.")
    promote_finding(ci_gh, oob, cclock[0])
    oob_open = lambda: any(i["state"] == "open" and "fail-open" in i["body"] for i in ci_fake.issues.values())
    ci_open = lambda: any(i["state"] == "open" and "main branch" in i["body"] for i in ci_fake.issues.values())
    print(f"    an unrelated 'gate could not run' item is open: {oob_open()}")
    recs, auth = derive_ci_records(ci_gh, "main", cclock[0])
    run(ci_gh, recs, ci_cache, th, cclock[0], authoritative=auth, live=True)   # one pass: red -> tracked
    print(f"    a failing check is tracked on the first pass: {ci_open()}   "
          f"(unrelated item untouched: {oob_open()})")
    os.remove(ci_cache.path)                        # the saved counter is wiped, as the ephemeral runner does
    ci_fake.check_runs = [{"name": "engine-ci", "conclusion": "success"},
                          {"name": "actionlint", "conclusion": "success"}]
    recs, auth = derive_ci_records(ci_gh, "main", cclock[1])
    run(ci_gh, recs, ci_cache, th, cclock[1], authoritative=auth, live=True)   # one pass: green -> resolved
    print(f"    green again, so it clears on the very next pass — even with the counter wiped: {not ci_open()}   "
          f"(and the unrelated item was NEVER closed: {oob_open()})")
    ci_ok = (not ci_open()) and oob_open()

    print("\n(9) A create/create race left TWO issues for ONE signal (GitHub has no atomic create-if-absent).")
    print("    The next promotion CONSOLIDATES them — the lowest-numbered survives, the duplicate is closed")
    print("    with a note, and the earliest first-noticed is preserved (never reset to 'now'):")
    dup_fake = _FakeGitHub()
    dup_fake.labels.add(ENGINE_DOMAIN_LABEL)
    dup_rec = _rec("hooks/fail-open/PreToolUse/crash", TRUST_CRITICAL,
                   "A safety check on the PreToolUse step could not run.")
    for number, seen in ((433, cclock[0]), (434, cclock[1])):     # two open issues, same signal marker
        dup_fake.issues[number] = {"number": number, "title": issue_title(dup_rec),
                                   "body": issue_body(dup_rec, seen, seen),
                                   "labels": [ENGINE_DOMAIN_LABEL], "state": "open"}
    dup_fake._next = 435
    dup_gh = GitHubIssues("you/your-project", "demo-token", transport=dup_fake.transport)
    before = sum(1 for i in dup_fake.issues.values() if i["state"] == "open")
    survivor = promote_finding(dup_gh, dup_rec, cclock[5])        # cclock[5] is 'now' — must NOT become first-noticed
    after_open = sorted(n for n, i in dup_fake.issues.items() if i["state"] == "open")
    kept_earliest = parse_first_noticed(dup_fake.issues[433]["body"]) == cclock[0]
    print(f"    before: {before} open (#433, #434) -> after: {after_open} open  "
          f"(survivor #{survivor}, earliest first-noticed preserved: {kept_earliest})")
    dup_ok = before == 2 and after_open == [433] and survivor == 433 and kept_earliest
    for path in (cache.path, ci_cache.path):
        try:
            os.remove(path)
        except OSError:
            pass
    print("\n(9) The SECOND signal source — best-effort AMBIENT capture of local check-fires. A local check")
    print("    that keeps failing across sessions is tracked after it persists; once it is seen passing again")
    print("    — or its file is gone — its item clears; and it NEVER touches an unrelated item:")
    amb_fake = _FakeGitHub()
    amb_gh = GitHubIssues("you/your-project", "demo-token", transport=amb_fake.transport)
    amb_cache = Cache(os.path.join(validate.ROOT, ".engine", "telemetry", ".cache", "_demo_ambient_streams.json"))
    amb_ndjson = os.path.join(validate.ROOT, ".engine", "telemetry", ".cache", "_demo_ambient.ndjson")
    for p in (amb_cache.path, amb_ndjson):
        try:
            os.remove(p)
        except OSError:
            pass
    aclock = ["2026-06-07T0%d:00:00Z" % n for n in range(1, 9)]
    amb_oob = _rec("hooks/fail-open/Stop/close", TRUST_CRITICAL, "A safety gate could not run this session.")
    promote_finding(amb_gh, amb_oob, aclock[0])                       # an unrelated out-of-band item, opened directly
    amb_oob_open = lambda: any(i["state"] == "open" and "fail-open" in i["body"] for i in amb_fake.issues.values())
    amb_open = lambda rid: any(i["state"] == "open" and rid in i["body"] for i in amb_fake.issues.values())
    seen_open, wm = [], ""
    for k in range(3):                                               # a local check FRESHLY fails across 3 sessions
        append_ambient([ambient_record("engine/check/policy-shape", False, "README.md", aclock[k])], amb_ndjson)
        recs, auth, wm = derive_ambient_records(amb_ndjson, wm, exists=lambda p: True)
        run(amb_gh, recs, amb_cache, th, aclock[k], authoritative=auth, live=False)
        seen_open.append(amb_open("policy-shape"))
    print(f"    fails freshly across 3 sessions -> tracked only after it persists: {seen_open}   "
          f"(unrelated item untouched: {amb_oob_open()})")
    # A ONE-TIME fail that is never re-run must NOT promote — persistence is a patience window over recurring
    # FRESH fails, not a stale replay of one observation (the freshness watermark makes this real):
    tr_fake = _FakeGitHub()
    tr_gh = GitHubIssues("you/your-project", "demo-token", transport=tr_fake.transport)
    tr_cache = Cache(os.path.join(validate.ROOT, ".engine", "telemetry", ".cache", "_demo_ambient_tr.json"))
    tr_ndjson = os.path.join(validate.ROOT, ".engine", "telemetry", ".cache", "_demo_ambient_tr.ndjson")
    for p in (tr_cache.path, tr_ndjson):
        try:
            os.remove(p)
        except OSError:
            pass
    append_ambient([ambient_record("engine/check/one-off", False, "x.md", aclock[0])], tr_ndjson)  # fires ONCE
    tr_wm, tr_open = "", []
    for k in range(3):                                              # three passes, NO new fire appended
        recs, auth, tr_wm = derive_ambient_records(tr_ndjson, tr_wm, exists=lambda p: True)
        run(tr_gh, recs, tr_cache, th, aclock[k], authoritative=auth, live=False)
        tr_open.append(any(i["state"] == "open" for i in tr_fake.issues.values()))
    transient_ok = tr_open == [False, False, False]
    print(f"    a ONE-TIME fail never re-run -> NEVER promoted: {transient_ok}  (open across 3 passes: {tr_open})")
    for p in (tr_cache.path, tr_ndjson):
        try:
            os.remove(p)
        except OSError:
            pass
    append_ambient([ambient_record("engine/check/policy-shape", True, "README.md", aclock[3])], amb_ndjson)
    for k in range(3, 6):                                           # now it is seen PASSING -> clears
        recs, auth, wm = derive_ambient_records(amb_ndjson, wm, exists=lambda p: True)
        run(amb_gh, recs, amb_cache, th, aclock[k], authoritative=auth, live=False)
    cleared = not amb_open("policy-shape")
    print(f"    seen passing again -> it clears: {cleared}   (unrelated item STILL untouched: {amb_oob_open()})")
    append_ambient([ambient_record("engine/check/gone-rule", False, "deleted.md", aclock[6])], amb_ndjson)
    gone_recs, gone_auth, _ = derive_ambient_records(amb_ndjson, "", exists=lambda p: p != "deleted.md")
    gone_ok = ("ambient/engine/check/gone-rule" in gone_auth
               and not any(r["source_id"] == "ambient/engine/check/gone-rule" for r in gone_recs))
    print(f"    a failing check whose file was deleted is treated as gone, not stuck open: {gone_ok}")
    amb_ok = (seen_open == [False, False, True] and transient_ok and cleared and amb_oob_open() and gone_ok)
    for p in (amb_cache.path, amb_ndjson):
        try:
            os.remove(p)
        except OSError:
            pass

    print("\n(10) The THIRD input the self-review consumes — the engine's own file-scoped checks that select")
    print("    NO files right now (F0200). Each is surfaced for the self-review to judge (raise-it-upstream if")
    print("    it is dead template weight, or leave it) — NEVER a local retirement; a check that DOES match")
    print("    files, and a non-file-scoped check, are excluded. Driven over a fixture (the real checks all")
    print("    match files in this repo, so the live feed is correctly empty here):")
    nf_rules = [
        {"id": "engine/check/demo-empty", "kind": "shape", "target": {"path": ".engine/_no_such_demo_dir/*.json"}},
        {"id": "engine/check/demo-matches", "kind": "shape", "target": {"path": ".engine/policies/*.md"}},
        {"id": "engine/check/demo-context", "kind": "presence", "target": {"context": "pull-request-body"}},
        {"id": "engine/check/demo-coverage", "kind": "coverage", "target": {"path": ".engine/_no_such_demo_dir/*"}},
    ]
    nf_ids = {r["rule_id"] for r in derive_never_fired(nf_rules)}
    nf_scoped_ok = (nf_ids == {"engine/check/demo-empty"})   # only the zero-match file-scoped rule; not the
    #   matching one, not the context-targeted one, not the whole-tree coverage kind (even with an empty glob)
    nf_render = render_never_firing_checks(nf_rules)
    nf_framing_ok = ("raise-it-upstream" in nf_render and "QUESTION, not a verdict" in nf_render)
    nf_ok = nf_scoped_ok and nf_framing_ok
    print(f"    only a zero-match file-scoped check is surfaced (matching / context / whole-tree excluded): "
          f"{nf_scoped_ok}")
    print(f"    the feed frames it escalate-or-ignore (a question, not a retire verdict): {nf_framing_ok}")

    print("\n(11) The THIRD signal source — the memory ledger's consolidation backlog (F0210, cache-accrued")
    print("    like ambient, not a live read). When the")
    print("    engine's memory tidy-up stays behind across sessions it is tracked; once the backlog is genuinely")
    print("    cleared its item resolves; an absent/unreadable ledger claims NO authority (a per-machine read")
    print("    must not resolve a global issue); and it NEVER touches an unrelated item:")
    import shutil as _shutil
    import tempfile as _tempfile
    from memory import capture as _cap, consolidate as _con, ledger as _led
    epi_fake = _FakeGitHub()
    epi_gh = GitHubIssues("you/your-project", "demo-token", transport=epi_fake.transport)
    epi_cache = Cache(os.path.join(validate.ROOT, ".engine", "telemetry", ".cache", "_demo_episodic_streams.json"))
    try:
        os.remove(epi_cache.path)
    except OSError:
        pass
    eclock = ["2026-06-08T0%d:00:00Z" % n for n in range(1, 9)]
    epi_oob = _rec("hooks/fail-open/PreToolUse/modes", TRUST_CRITICAL, "A safety gate could not run this session.")
    promote_finding(epi_gh, epi_oob, eclock[0])                      # an unrelated out-of-band item, opened directly
    epi_oob_open = lambda: any(i["state"] == "open" and "fail-open" in i["body"] for i in epi_fake.issues.values())
    epi_open = lambda: any(i["state"] == "open" and EPISODIC_BACKLOG_SID in i["body"] for i in epi_fake.issues.values())
    prev_env = os.environ.get(_led.ENV_DIR)
    tmp = _tempfile.mkdtemp(prefix="engine-demo-episodic-")
    try:
        os.environ[_led.ENV_DIR] = tmp
        for i in range(EPISODIC_BACKLOG_THRESHOLD):                  # seed a DEEP backlog: 5 un-consolidated sessions
            _led.append(_cap._make_record(f"demo-sess-{i}", 1, "user", f"a genuine turn in session {i}"))
        promoted = []
        for k in range(3):                                          # deep across 3 passes -> promotes only on the 3rd
            recs, auth = derive_episodic_records()
            run(epi_gh, recs, epi_cache, th, eclock[k], authoritative=auth, live=False)
            promoted.append(epi_open())
        for i in range(EPISODIC_BACKLOG_THRESHOLD):                 # the tidy catches up: every session consolidated
            _con.store_episodic(f"demo-sess-{i}", [{"role": "observation", "text": "tidied"}])
        for k in range(3, 5):                                       # backlog genuinely clear -> auto-resolves
            recs, auth = derive_episodic_records()
            run(epi_gh, recs, epi_cache, th, eclock[k], authoritative=auth, live=False)
        cleared = not epi_open()
        os.environ[_led.ENV_DIR] = _tempfile.mkdtemp(prefix="engine-demo-episodic-empty-")  # an ABSENT ledger
        abs_recs, abs_auth = derive_episodic_records()
        gate_ok = abs_recs == [] and abs_auth == frozenset()        # absent read -> no authority -> closes nothing
        _shutil.rmtree(os.environ[_led.ENV_DIR], ignore_errors=True)
    finally:
        if prev_env is None:
            os.environ.pop(_led.ENV_DIR, None)
        else:
            os.environ[_led.ENV_DIR] = prev_env
        _shutil.rmtree(tmp, ignore_errors=True)
    print(f"    a backlog deep across 3 sessions -> tracked only after it persists: {promoted}   "
          f"(unrelated item untouched: {epi_oob_open()})")
    print(f"    the tidy-up catches up -> the item resolves: {cleared}   "
          f"(unrelated item STILL untouched: {epi_oob_open()})")
    print(f"    an absent/unreadable ledger claims no authority -> closes nothing: {gate_ok}")
    epi_ok = (promoted == [False, False, True] and cleared and epi_oob_open() and gate_ok)
    try:
        os.remove(epi_cache.path)
    except OSError:
        pass

    # --- the findings-inbox emit-and-done seam (F0203): a producer EMITS and is done; telemetry owns the act.
    ibx_dir = _tempfile.mkdtemp(prefix="engine-demo-inbox-")
    spool = os.path.join(ibx_dir, "findings-inbox.ndjson")
    ibx_cache = Cache(os.path.join(ibx_dir, "inbox-streams.json"))
    ibx_fake = _FakeGitHub()
    ibx_gh = GitHubIssues("you/your-project", "demo-token", transport=ibx_fake.transport)
    ibx_now = utc_now()

    def _ibx_open():
        return {parse_source_id(i["body"]) for i in ibx_fake.issues.values() if i["state"] == "open"}
    print("\n(inbox) The engine's 'report a problem and move on' channel — a part of the engine flags "
          "something and hands it off; the engine tracks it from there. (Only the urgent path is live "
          "today; the routine side turns on when a later step wires it.)")
    # a could-not-run (trust-critical) signal promotes IMMEDIATELY and is NEVER spooled
    tc_num = emit_finding({"source_id": "hooks/fail-open/PreToolUse/crash", "severity": TRUST_CRITICAL,
                           "message": "a safety gate could not run", "location": None,
                           "first_seen": ibx_now, "last_seen": ibx_now}, gh=ibx_gh, spool_path=spool)
    tc_ok = bool(tc_num) and not os.path.exists(spool)
    # a benign signal SPOOLS; only a later drain promotes it — and only once it persists across drains
    ibx_thr = load_thresholds()
    persistence = int(ibx_thr.get("persistence", 3))
    benign = {"source_id": "boot/refused-cursor", "severity": PERSISTENT_BENIGN, "location": None,
              "message": "the engine's saved place could not be trusted", "first_seen": ibx_now,
              "last_seen": ibx_now}
    drained_open = []
    for _ in range(persistence):
        emit_finding(benign, spool_path=spool)
        drain_inbox(ibx_gh, cache=ibx_cache, thresholds=ibx_thr, now=ibx_now, spool_path=spool)
        drained_open.append("boot/refused-cursor" in _ibx_open())
    # a forged/marker-unsafe spool line is dropped: never promoted, and never triggers a wrongful close
    with open(spool, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"source_id": "x<!-- forged -->", "severity": PERSISTENT_BENIGN,
                             "message": "m", "location": None}) + "\n")
    unsafe_report = drain_inbox(ibx_gh, cache=ibx_cache, thresholds=ibx_thr, now=ibx_now, spool_path=spool)
    _shutil.rmtree(ibx_dir, ignore_errors=True)
    print(f"    an urgent report — a safety check that could not run — is tracked at once (issue "
          f"#{tc_num}), never set aside: {tc_ok}")
    print(f"    a routine report is set aside and becomes a tracked item only after it keeps recurring: "
          f"{drained_open}   (a tampered entry is discarded and never tracked: {unsafe_report is None})")
    inbox_ok = (tc_ok and drained_open == [False] * (persistence - 1) + [True] and unsafe_report is None)

    print("\nDone — no real issues were created; only the network was faked. The triage LOGIC above is "
          "real; that it writes correctly to your REAL GitHub is confirmed the first time it runs live.")
    # Self-check: ONE issue opens only when the benign signal crosses the threshold (3rd fire), re-fires
    # never duplicate it, a distinct trust-critical signal opens a 2nd, an unreachable GitHub degrades
    # in-band (never a silent or wrong zero), the LIVE CI source is tracked on the first failing pass and
    # clears on the first green pass EVEN WITH THE CACHE WIPED, the cache-accrued AMBIENT source promotes only
    # after it persists and clears when seen passing or its file is gone, a create/create-race duplicate pair
    # CONVERGES to one survivor, the memory-ledger EPISODIC backlog is tracked only after it persists and clears
    # once genuinely tidied (with an absent/unreadable ledger claiming no authority — the per-machine/global
    # guard), none of these ever closes the unrelated out-of-band item, and the never-firing signal surfaces only
    # a zero-match file-scoped check, framed escalate-or-ignore.
    ok = (open2 == 1 and open3 == 1 and open4 == 2 and bool(r6.degraded_line)
          and ci_ok and amb_ok and dup_ok and nf_ok and epi_ok and inbox_ok)
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


def derive_never_fired(rules: list | None = None) -> list:
    """The never-firing-check signal (F0200): the engine's own FILE-SCOPED checks that select ZERO files in the
    current tree — a check that inspects nothing, so it never fires. Returns a list of
    `{rule_id, kind, target_glob, message}` (message is the check's own plain-language statement of what it
    guards, so the self-review can tell the operator what raising it upstream would protect).

    Computed FRESH each run from the committed check corpus (`.engine/check/*.json`) plus the working tree
    (`validate.target_files`) — no cache, no persisted fire-count, no ledger (D-038); re-derivable on the
    ephemeral audit-prep runner, which has both. Only the FILE-SCOPED in-process kinds are considered
    (`validate._AMBIENT_KINDS` — schema/shape/presence: the single source of truth, the same set the ambient
    writer uses). Context/surface-targeted rules and whole-tree kinds (coverage/coherence/custom-script) are
    excluded by the KIND GATE — NOT because their `target_files` is empty (a path-targeted coverage/custom rule
    can match files); their "firing" isn't a glob-match at all, so the kind gate is load-bearing, not redundant.

    This is the LITERAL "never fires" (never evaluates) reading — a MECHANICAL "currently matches no files"
    fact, NOT a claim the rule is dead, and deliberately narrower than the check-surface README's "ever bites in
    production" gloss (which watches fire history — unbuildable ledger-free: D-038 forbids a committed
    check-fire ledger, and the best-effort ambient cache can't ride the audit runner; the v1 scope is a logged
    decision, disclosed in the PR). The audit judges which never-firing check is dead template weight (raise
    upstream) vs a file kind the project doesn't use.

    A SYSTEMIC failure (e.g. the file-scoped kind set gone) raises to the caller, so the render shows an honest
    "could not be computed" marker rather than a silent wrong-zero. A single MALFORMED rule (bad data shape) or
    a corrupt rule FILE is skipped, never aborts the scan. `rules` is injectable for tests/demo."""
    kinds = validate._AMBIENT_KINDS   # hoisted: a systemic loss raises ONCE here -> honest marker, not a silent per-rule skip
    if rules is None:
        rules = []
        check_dir = validate.CHECK_DIR
        names = sorted(os.listdir(check_dir)) if os.path.isdir(check_dir) else []
        for name in names:
            if not name.endswith(".json"):
                continue
            try:
                rules.append(validate.load_json(os.path.join(check_dir, name)))
            except Exception:  # noqa: BLE001 — a corrupt rule FILE is skipped, never aborts the whole scan
                continue
    never = []
    for rule in rules:
        try:
            if rule.get("kind") not in kinds:
                continue
            glob = (rule.get("target") or {}).get("path")
            if not glob:
                continue
            if not validate.target_files(rule):
                never.append({"rule_id": rule.get("id"), "kind": rule.get("kind"),
                              "target_glob": glob, "message": rule.get("message")})
        except (AttributeError, KeyError, TypeError, ValueError):
            # a single MALFORMED rule (non-dict, wrong-typed field) is skipped; an UNEXPECTED error type is NOT
            # swallowed — it propagates to the render's honest marker rather than silently emptying the feed
            continue
    return never


def render_never_firing_checks(rules: list | None = None) -> str:
    """Plain text the audit-prep workflow feeds the read-only self-review persona (F0200): the engine's own
    checks that currently match NO files in this repo, for the persona to judge whether each still earns its
    place. Frames the fact as escalate-OR-ignore, NEVER retire/keep (a check rule is engine machinery — never a
    local retirement), and carries each check's plain-language purpose so the persona can tell the operator what
    raising it upstream would protect. On ANY error — including a systemic derivation failure — it returns an
    honest "could not be computed" marker (NEVER a silent empty), so `main()`'s fail-open never sees an
    exception and the workflow's own `if !` guard is only a backstop. Every author-controlled field (id, glob,
    message) is defanged before it enters the persona prompt. `rules` is injectable for tests/demo (the real
    corpus matches files, so the live feed is empty here)."""
    try:
        never = derive_never_fired(rules)
        if not never:
            return ("ENGINE CHECKS MATCHING NO FILES: every one of the engine's own file-scoped checks "
                    "currently matches at least one file in this repository — nothing to review on this "
                    "concern this run.")
        parts = [f"ENGINE CHECKS MATCHING NO FILES — {len(never)} of the engine's own checks currently select "
                 "no files in this repository. A check that matches nothing here is a QUESTION, not a verdict, "
                 "and matching nothing can be perfectly correct — the check may guard a kind of file this "
                 "project does not use (yet). Because a check is engine machinery (overlaid on every update), "
                 "it is NEVER something to retire locally: the only two responses are raise-it-upstream — if it "
                 "looks like dead weight the template ships — or leave-it. Judge each on that basis; do not "
                 "recommend a local retirement.", ""]
        for item in never:
            rid = item.get("rule_id") or "(a check with no id)"
            parts.append(f"- {validate.defang_prompt_fence_markers(str(rid))}  (checks files matching: "
                         f"{validate.defang_prompt_fence_markers(str(item['target_glob']))})")
            msg = item.get("message")
            if msg:
                parts.append("    what this check protects: "
                             f"{validate.defang_prompt_fence_markers(str(msg))}")
        return "\n".join(parts)
    except Exception as exc:  # noqa: BLE001 — an honest gap (incl. a systemic derive failure), never a silent empty
        return ("ENGINE CHECKS MATCHING NO FILES: could not be computed this run — "
                f"{exc}. Treat this concern as unreviewed and say so plainly in your digest.")


def _never_fired_cli(argv: list) -> int:
    """The audit-prep workflow's never-firing-checks verb (F0200): print the engine's own checks that match no
    files, for the self-review persona to judge. Reads ONLY the committed check corpus + the working tree — no
    GitHub, no token — so it always exits 0; the render degrades in-band on any internal error."""
    print(render_never_firing_checks())
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


def _run_cli(argv: list) -> int:
    """The live triage verb — the driver the scheduled audit-prep workflow invokes each pass. Reads the main
    branch's CI check-runs (the first signal of record) and reconciles the engine-labelled issues for the
    `ci/` source: a failing check is tracked, and once it is green again its item auto-resolves. Reads
    GITHUB_REPOSITORY + GITHUB_TOKEN (the GitHub token, never the Claude OAuth token) and the default branch
    from GITHUB_DEFAULT_BRANCH (the workflow passes `github.ref_name`, which on a scheduled run is the
    default branch; falls back to 'main' when unset).

    SAFETY: auto-resolve is scoped to the `ci/` source-ids OBSERVED this pass, so it never touches an
    out-of-band issue (a hooks fail-open alarm, a migration/resurrection finding); and a failed OR partial CI
    read claims frozenset() — closing nothing — so an unread check is never mistaken for a passing one. It
    writes NO committed state; the offline where-we-stand cache is the separate `refresh` verb, run AFTER
    this so its open-count reflects this pass. Exits 0 even when GitHub is unreachable — triage is
    best-effort and must never fail the self-review; main()'s fail-open backstops any other error. An
    optional argv[0] overrides the stream-cache path (for tests)."""
    repo = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        print("usage: telemetry.py run   (needs GITHUB_REPOSITORY and GITHUB_TOKEN in the environment; it "
              "uses the GitHub token, never the Claude token)", file=sys.stderr)
        return 2
    branch = os.environ.get("GITHUB_DEFAULT_BRANCH") or "main"
    now = utc_now()
    gh = GitHubIssues(repo, token)
    cache = Cache(argv[0]) if argv else Cache()
    ci_read_failed = False
    try:
        records, authoritative = derive_ci_records(gh, branch, now)
    except DegradedReadError:
        records, authoritative, ci_read_failed = [], frozenset(), True
    # CI is a LIVE-DERIVED signal (read fresh from the durable GitHub record each pass): promote on the
    # first observed failure, resolve on the first observed pass, keyed off the durable Issue set — NOT the
    # gitignored stream cache, which this ephemeral scheduled runner wipes every run.
    report = run(gh, records, cache, load_thresholds(), now, authoritative=authoritative, live=True)
    if report.degraded:
        if report.opened or report.updated or report.closed:
            print(f"GitHub became unreachable partway through the engine's CI-health triage; opened="
                  f"{report.opened}, updated={report.updated}, closed={report.closed} before it failed — "
                  "the next pass reconciles the rest. The digest still commits.")
        else:
            print("Could not reach GitHub to run the engine's CI-health triage; nothing was changed. "
                  "The digest still commits.")
        return 0
    if ci_read_failed:
        print("Could not read your main branch's CI checks this run; left every tracked item untouched. "
              "The digest still commits.")
        return 0
    print(f"Ran the engine's CI-health triage: opened={report.opened}, updated={report.updated}, "
          f"closed={report.closed}.")
    return 0


def _run_ambient_cli(argv: list) -> int:
    """The ambient triage verb — the LOCAL SessionStart driver (a sibling of the memory/backup SessionStart
    writers). Reads the gitignored local check-fire cache (the second signal of record) and reconciles the
    engine-labelled issues for the `ambient/` source: a local check that keeps failing across sessions is
    tracked, and once it is seen passing again — or its target file is gone — its item auto-resolves.

    Unlike the CI `run` verb (which runs on the EPHEMERAL audit-prep runner that wipes the cache), this runs
    on the LOCAL machine that OWNS the per-machine ambient cache — so it resolves the GitHub context the LOCAL
    way (boot's repo_slug/gh_token, as close._github borrows), NOT CI env vars, and accrues persistence in its
    OWN cache (ambient-streams.json) across sessions, separate from the CI run's streams.json. CACHE-ACCRUED
    (live=False): it promotes only after the persistence threshold (smoothing a transient). SAFETY: auto-
    resolve is scoped to the `ambient/` source-ids OBSERVED this pass, so it can never touch a `ci/` or other
    out-of-band issue. Fail-open: no local repo/token (the normal state on a machine not logged in), or an
    unreachable GitHub, degrades to exit 0 touching nothing; main()'s boundary backstops any other error. An
    optional argv[0] overrides the ambient stream-cache path (for tests)."""
    from boot import repo_slug, gh_token   # lazy: boot imports telemetry, a back-edge safe only lazily
    repo, token = repo_slug(), gh_token()
    if not repo or not token:
        return 0   # no local GitHub context — the normal state off a logged-in machine; skip silently
    now = utc_now()
    gh = GitHubIssues(repo, token)
    cache = Cache(argv[0]) if argv else Cache(DEFAULT_AMBIENT_STREAMS_PATH)
    watermark = load_ambient_watermark()
    records, authoritative, new_watermark = derive_ambient_records(watermark=watermark)
    report = run(gh, records, cache, load_thresholds(), now, authoritative=authoritative, live=False)
    if report.degraded:
        print("Could not reach GitHub to run the engine's ambient check-health triage; nothing was changed.")
        return 0   # leave the watermark unadvanced — the un-consumed fires stay fresh for the next pass
    store_ambient_watermark(new_watermark)   # advance only after a clean pass, so nothing is silently skipped
    print(f"Ran the engine's ambient check-health triage: opened={report.opened}, "
          f"updated={report.updated}, closed={report.closed}.")
    return 0


def _hook_payload_session_id() -> str | None:
    """The live session id from a SessionStart hook's stdin PAYLOAD (mirroring memory's handler,
    consolidate.py:361 — SessionStart hooks receive the id in the payload, not reliably in the environment).
    Returns None on a tty / empty / unparseable stdin, so a manual CLI run falls back to the env var. Never
    raises — a bad payload just means the reader's reused lease-heartbeat filter is the only live-session guard
    this pass (a bounded, safe degrade)."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return None
        raw = sys.stdin.read()
    except Exception:  # noqa: BLE001
        return None
    if not raw or not raw.strip():
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    sid = payload.get("session_id") if isinstance(payload, dict) else None
    return sid if isinstance(sid, str) and sid else None


def _run_episodic_cli(argv: list) -> int:
    """The episodic triage verb — the LOCAL SessionStart driver (a sibling of run-ambient and the memory/backup
    SessionStart writers). Reads the memory ledger's consolidation backlog (the third signal of record) and
    reconciles the engine-labelled issue for the `episodic/` source: when the memory tidy-up stays chronically
    behind across sessions it is tracked, and once the backlog is genuinely cleared its item auto-resolves.

    Like run-ambient (and UNLIKE the CI `run` verb on the ephemeral audit runner), this runs on the LOCAL
    machine that OWNS the per-machine ledger — so it resolves the GitHub context the LOCAL way (boot's
    repo_slug/gh_token) and accrues persistence in its OWN cache (episodic-streams.json). CACHE-ACCRUED
    (live=False): it promotes only after the persistence threshold. SAFETY: `authoritative` is the single
    `episodic/` source-id and ONLY when this machine POSITIVELY observed a usable ledger (derive_episodic_records)
    — so it can never touch a `ci/`/`ambient/`/out-of-band issue, and an absent/corrupt ledger closes nothing.
    Fail-open: no local repo/token (the normal state off a logged-in machine), or an unreachable GitHub, degrades
    to exit 0 touching nothing; main()'s boundary backstops any other error. Optional argv[0] overrides the
    stream-cache path (for tests)."""
    from boot import repo_slug, gh_token   # lazy: boot imports telemetry, a back-edge safe only lazily
    repo, token = repo_slug(), gh_token()
    if not repo or not token:
        return 0   # no local GitHub context — the normal state off a logged-in machine; skip silently
    live = _hook_payload_session_id() or os.environ.get("CLAUDE_CODE_SESSION_ID")   # payload first, env fallback
    now = utc_now()
    gh = GitHubIssues(repo, token)
    cache = Cache(argv[0]) if argv else Cache(DEFAULT_EPISODIC_STREAMS_PATH)
    records, authoritative = derive_episodic_records(live_session_id=live)
    report = run(gh, records, cache, load_thresholds(), now, authoritative=authoritative, live=False)
    if report.degraded:
        print("Could not reach GitHub to run the engine's memory-upkeep triage; nothing was changed.")
        return 0
    print(f"Ran the engine's memory-upkeep triage: opened={report.opened}, "
          f"updated={report.updated}, closed={report.closed}.")
    return 0


def _run_drain_cli(argv: list) -> int:
    """The findings-inbox drain verb — the LOCAL SessionStart driver (a sibling of run-ambient/run-episodic and
    the memory/backup SessionStart writers) that stands the emit-and-done seam's drain up in PRODUCTION (the
    cadence #412 owns). Three fail-open jobs:
      1) promote the broken-runtime marker → ONE TRUST_CRITICAL could-not-run finding, immediately (the live
         producer this slice delivers; the hook launcher drops the marker when the engine's Python is absent);
      2) sweep mtime-stale `*.draining` asides (a crashed drain's stranded batch) back through the drain;
      3) drain the findings-inbox spool → promote the benign/degraded findings a producer emitted out-of-band
         (the boot degradation emitter that FEEDS this benign path is #398 — until it lands the spool is fed
         only by tests/fixtures, so this leg is live infrastructure, not yet a live benign producer).
    Runs on the LOCAL machine (the spool + marker are per-machine, gitignored), resolving the GitHub context the
    LOCAL way (boot's repo_slug/gh_token). SAFETY: drain_inbox scopes auto-resolve to the drained source-ids
    ONLY (never a ci/ambient/episodic/out-of-band issue), and the marker promote is de-duped by a fixed
    source_id. Fail-open: no local repo/token (the normal state off a logged-in machine), or an unreachable
    GitHub, degrades to exit 0 touching nothing; main()'s boundary backstops any other error. Optional argv[0]
    overrides the drain stream-cache path (for tests)."""
    from boot import repo_slug, gh_token   # lazy: boot imports telemetry, a back-edge safe only lazily
    repo, token = repo_slug(), gh_token()
    if not repo or not token:
        return 0   # no local GitHub context — the normal state off a logged-in machine; skip silently
    gh = GitHubIssues(repo, token)
    runtime_alert = promote_runtime_marker(gh)
    _sweep_stranded_asides(INBOX_SPOOL_PATH)
    cache = Cache(argv[0]) if argv else Cache(DEFAULT_INBOX_STREAMS_PATH)
    report = drain_inbox(gh, cache=cache, thresholds=load_thresholds(), now=utc_now())
    if report is not None and report.degraded:
        print("Could not reach GitHub to check the engine's own health inbox; nothing was changed.")
        return 0
    opened = report.opened if report is not None else 0
    updated = report.updated if report is not None else 0
    closed = report.closed if report is not None else 0
    # Plain-voice summary (matching the run-ambient/run-episodic sibling drivers) — no backstage words.
    alert = "a broken-tool-runtime alert was raised" if runtime_alert else "no new alerts"
    print(f"Checked the engine's own health inbox ({alert}): "
          f"opened={opened}, updated={updated}, closed={closed}.")
    return 0


def main(argv: list) -> int:
    """Fail-open: telemetry is self-surfacing and must never break a session. Any unexpected error
    emits a plain finding and exits 0."""
    try:
        if argv and argv[0] == "run":
            return _run_cli(argv[1:])
        if argv and argv[0] == "run-ambient":
            return _run_ambient_cli(argv[1:])
        if argv and argv[0] == "run-episodic":
            return _run_episodic_cli(argv[1:])
        if argv and argv[0] == "drain-inbox":
            return _run_drain_cli(argv[1:])
        if argv and argv[0] == "demo":
            return _demo(argv[1:])
        if argv and argv[0] == "refresh":
            return _refresh_cli(argv[1:])
        if argv and argv[0] == "engine-issues":
            return _engine_issues_cli(argv[1:])
        if argv and argv[0] == "never-fired":
            return _never_fired_cli(argv[1:])
        print("usage: telemetry.py {run|run-ambient|run-episodic|drain-inbox|demo|refresh|engine-issues|"
              "never-fired}   (`run` is the live CI-health triage the scheduled audit-prep workflow drives; "
              "`run-ambient`, `run-episodic`, and `drain-inbox` are the local SessionStart triages — over local "
              "check-fires, over the memory tidy-up backlog, and over the findings inbox (promoting a broken "
              "tool-runtime alert and any out-of-band findings); `engine-issues` and `never-fired` (the engine's "
              "own checks that currently match no files) feed the scheduled self-review; demo shows the logic on "
              "a fake GitHub)", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — fail-open is the whole point
        print(json.dumps(validate.finding(
            "soft", f"The engine's self-monitoring hit an unexpected error and stopped without acting "
            f"({exc}); this was recorded and the session continues normally.")), file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
