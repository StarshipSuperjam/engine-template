#!/usr/bin/env python3
"""Slice 20 — boot: the SessionStart orientation pack (the hook-DEPENDENT rich layer).

Beneath this sits slice 19's hook-INDEPENDENT floor (the root CLAUDE.md the platform always loads).
This module is the rich layer that rides on top when the SessionStart hook fires: it assembles a
bounded, prioritized, plain-language orientation pack from committed state and the substrates that
exist today, and injects it as `additionalContext` before the first prompt. The two-layer story is
the floor (always) + this pack (when the hook runs).

Boot's laws, all load-bearing here (systems/lifecycle/boot/README.md):
  - READ-ONLY OF CANONICAL STATE (D-269). Boot regenerates NO derived or committed state; it reads and
    surfaces. Its own local write is the gitignored, non-canonical standing-alarm presentation ledger
    (boot_alarm_ledger) — a record of what was already shown, not a regeneration of any canonical state.
    The one durable FINDING boot emits — a refused state cursor (D-059 law 1, U15b) — is handed to
    telemetry's inbox spool via emit_finding: telemetry owns that write, it is a local gitignored append
    (NEVER a GitHub write), and the #412 drain promotes it — so the read-only-AGAINST-GITHUB posture holds.
  - ANTI-HABITUATION BY COLLAPSE, NOT SUPPRESSION (D-269). A standing governance alarm renders every
    session it is live, but one whose structured condition is UNCHANGED since last shown in full collapses
    to a terse reminder (consequence + fix offer kept); a new/changed/worsened one relays in full. The
    decision is deterministic in the hook path (_relay_lines -> boot_alarm_ledger.decide), fail-toward-full,
    never the model. The present-marker line and the all-clear render NEVER collapse.
  - RELAY, NOT DETECT. Boot reuses the substrates' own detection — attention's ranking
    (attention.rank_live, consumed in its given precedence order and NEVER re-ranked), telemetry's
    debt readout, protection_guard's protected-branch evaluation — and renders them. It computes none.
  - NEVER a SessionStart halt. The hooks harness (hooks.run_hook) fail-opens on any exception, and
    SessionStart is not block-eligible, so boot can only inject or fail open. Each substrate read is
    additionally wrapped so one absent/broken source degrades that line only, never the whole pack.
  - DEGRADE LOUD. A figure from a degraded source is rendered so it cannot be mistaken for current;
    an unreachable live source is named, never silently dropped, and a couldn't-verify safety gate
    NEVER reads as a green all-clear.
  - ALARMS PINNED + LEGIBLE. Governance-critical alarms head the must-push set the briefing tells the AI
    to relay first, and pin first (as loud quoted lines) in the operator-toned dashboard, above the work.
  - NO CHANGELOG ("recently shipped" reads merged PRs), NO compact re-render (the hook fires on the
    session-START sources startup/resume/clear, never compact — the post-compaction floor is the
    re-injected CLAUDE.md + the next scent), and the memory consolidation sweep is memory's, not
    boot's (boot does not fire it; it lands with the memory substrate, post-core).
  - THE MODES STANCE CLEAR is modes' operation, invoked at boot's SessionStart MOMENT (the event also
    carries non-orientation operations — cf. memory's sweep above): the handler calls modes.clear_stance
    FIRST so every session, including a resume, boots Explore and never inherits a prior Build signal;
    then it renders the stance line. The clear is modes' logic; boot's ORIENTATION rendering stays
    read-only (it regenerates no derived state — the read-only law is about derived state, not an
    ephemeral OS-temp session signal).

The boot pack is the AI's BRIEFING, not a message to the operator: it reaches the model, never the
operator's screen (constraints — `additionalContext` is model-only), so the operator meets it only through
the AI relaying it (the operator-presentation relay, D-187/D-188). `assemble_pack` builds the briefing — an
AI-facing preamble, the present-marker line the AI is told to render FIRST (a short titled `Project status`
block; PRESENT_MARKER, byte-identical to the floor's verify-presence copy in CLAUDE.deployed.md), the
INFORM-marked must-push items (governance alarms + a grounding-failure tell) the AI relays in plain words,
then the full operator-toned dashboard for grounding. The present-marker line + must-push partition are a
fixed RELAY over signals the substrates already detected — boot computes no new state. `render_dashboard` is
the operator-toned dashboard alone (PURE — no I/O; it renders gathered signals as DATA), reused by the status
verb (the "two renderings of the same data"). The present-marker's ABSENCE from the AI's opening is how the
floor tells the operator boot did not ground (the double-fault check). The modes stance line renders now that
modes exists (slice 21); memory's reversible-forgetting readout renders whenever memory has set anything aside
from recall, and is simply absent when nothing is set aside — a young store that has forgotten nothing yet
shows no block, no genesis-only scaffolding.

CLI:  python tools/boot.py pack     # print the assembled briefing (what the hook injects — a debug view)
      python tools/boot.py          # hook mode: run the SessionStart handler over stdin (what the
                                     #   wired hook invokes; injects additionalContext, fail-open)
"""
from __future__ import annotations

import datetime
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402
import hooks             # noqa: E402  (the fail-open harness + inject/proceed + command rendering)
import attention         # noqa: E402  (rank_live: the shared assembler boot consumes, never re-ranks)
import work_record       # noqa: E402  (#394: the merged-PR titles behind the ranked recent-decisions digest)
import boot_slice        # noqa: E402  (#37: boot's rung-1 knowledge cache; read() fail-opens to None)
import knowledge_gen     # noqa: E402  (REGEN_CMD: the one operator-facing regenerate-the-map command, cited not re-typed)
import boot_alarm_ledger  # noqa: E402  (D-269: the standing-alarm presentation ledger; decide() fail-opens to full)
import operator_overrides  # noqa: E402  (the operator policy-override file reader; boot loads it, passes the slice as DATA)
import telemetry         # noqa: E402  (read_state_debt / degraded_readout / the read-only Issue list)
import protection_guard  # noqa: E402  (get_json + missing_floor: the protected-branch evaluation)
import modes             # noqa: E402  (clear_stance + the stance vocabulary: the SessionStart clear + line)
import checkout_health   # noqa: E402  (provisioning's operator-checkout strand detector; boot relays its detection)
import standing_situation  # noqa: E402  ("where we are" derived live from GitHub, read-only; boot displays, never writes)
import audit_digest       # noqa: E402  (the self-review freshness signal; boot relays its staleness detection, never re-detects)
import pr_reconcile       # noqa: E402  (#136: the stranded-PR conflict detector; boot relays its detection and OFFERS the fix)

# The card title a healthy boot always renders — byte-identical to the present-marker the floor names
# in CLAUDE.deployed.md (slice 19 `owes ←`). The byte-identity is locked by test_boot.py; renaming it
# here without the floor (or vice-versa) breaks the double-fault check, so the two move together.
PRESENT_MARKER = "Project status"

# The standing, AI-facing advertisement of the knowledge faculty (#92). A cold session — one with no work
# in hand, where the #37 neighbourhood block (render_neighborhood) is empty — is otherwise told
# state/stance/attention/findings but NOT that it can query the project's wiring map at all, so it
# re-derives the wiring by hand. This line names the faculty unconditionally and points at the runbook that
# says WHEN to reach for it. AI-facing only: assemble_pack places it ABOVE the operator-dashboard divider and
# it carries no RELAY_MARKER (§12 — the engine's own machinery stays out of operator narration). It is
# distinct from the in-flow "pull deeper" cue render_neighborhood emits only when a change already reaches
# into the graph (this one advertises the standing faculty; that one points at a specific neighbourhood).
KNOWLEDGE_FACULTY_NOTE = (
    "You can query the project's own wiring map any time — for any part, what it is part of, what depends "
    "on it, what checks it, what governs it — with the knowledge tools that load every session. Reach for it "
    "before you change something other parts rely on (an impact check), to orient on something unfamiliar, "
    "or to trace how two parts connect. When and how: `.engine/operations/knowledge-impact-check.md`."
)

# The SessionStart sources boot grounds on: the genuine session-START moments. `compact` is DELIBERATELY
# excluded — a full boot-pack re-render on compaction is a deferred enhancement that must never be
# depended on (boot/README §Post-compaction grounding); the reliable post-compaction floor is the
# re-injected CLAUDE.md + the next per-prompt scent. These are the matcher values the hook registers on.
SESSION_START_SOURCES = ("startup", "resume", "clear")

# Per-OS hook interpreter: the committed `.claude/settings.json` + core-manifest hook `wires` carry the
# POSIX form (`.engine/.venv/bin/python`), and `hook-runner.sh` resolves the actual layout at fire time
# (POSIX bin/python or Windows Scripts/python.exe under the same venv root) — so one committed repo boots
# on every OS, including a mixed-OS team (#407 / D-157 build-spec leaf). No per-OS re-render at generation.

PROTECTED_BRANCH = os.environ.get("PROTECTED_BRANCH", "main")
STATE_PATH = os.path.join(validate.ENGINE_DIR, "state", "state.json")
# The schema read_state validates the committed cursor against on read (U15a): a schema_version-1 cursor
# whose INNER shape is broken is refused, never rendered as a confident cursor (D-059 law 1). Loaded lazily
# inside _cursor_conforms, so a missing/corrupt schema is an engine fault that never blames a good cursor.
_STATE_SCHEMA_PATH = os.path.join(validate.SCHEMAS_DIR, "state.v1.json")
# The fixed source-id + severity of the durable refused-cursor finding (D-059 law 1's telemetry half, U15b).
# A FIXED literal message (see _refused_cursor_message) — no bytes from the malformed cursor flow into the
# finding, so a hand-crafted cursor can neither inject Issue-body content nor forge the signal sentinel;
# marker-safe by construction, deduped downstream by source_id.
REFUSED_CURSOR_SOURCE_ID = "boot/refused-cursor"

# (The "what just happened" digest was sized here by a buried RECENTLY_SHIPPED_COUNT constant — the
# magic-number pattern attention exists to retire. It is now the attention policy's reviewable, tunable
# `budget_recent_decisions` slice over the ranked recent_decisions partition: see _shipped_lines. #394 U01.)

# The cold-start orientation event's budget total. Boot owns the event's cost budget; attention owns how it
# splits across the kinds and flexes (systems/lifecycle/boot/README "Boot owns the event model; attention
# owns the budget within it"; systems/cognitive/attention/README "their cost budgets … are boot's to
# define"). This is a count of ITEM-SLOTS to surface — NOT a token/context-window measurement (the engine
# has none) — split across the five kinds by the attention policy's reviewable shares. Set to 5 kinds × the
# retired flat per-kind cap of 4, so the total surfacing volume matches what boot showed before, now
# distributed by the policy's shares instead of a buried flat number. At this total the proportional split
# seats every kind, so the policy's trim order (the overflow rule) stays INERT here and bites only under a
# genuinely smaller budget (the demo, or a share re-tune that starves a kind) — never manufactured scarcity.
# A deliberate starting value, calibrated from use like the policy's other dials, not frozen.
COLD_START_BUDGET = 20
# A DEFENSIVE per-category cap, reached only when a ranking result carries no budget_size (a malformed or
# budget-less result). A normal session always supplies the budget total above, so the policy's per-kind
# budget_size governs surfacing and this floor is not used; it only keeps a budget-less result from
# rendering an unbounded list. boot renders a prefix of attention's order — it never re-orders.
NEEDS_ATTENTION_CAP = 4
# How much of a recalled decision's text the orientation block shows. A recorded decision is a narrative
# summary, not a headline, so a long one is elided rather than allowed to crowd the briefing — HOW MANY are
# shown is the policy's budget slice; this bounds only how much of each. A build-spec leaf (D-052/D-113).
_RECALL_SNIPPET_CHARS = 240


# ---- the git / gh boundary (best-effort, degrade-loud — never raises to the caller) ---------

def _run(cmd: list, timeout: int = 10) -> str | None:
    """Run a local command and return stripped stdout, or None on any failure. Never raises — boot's
    every external read is best-effort and degrades rather than stranding the session."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:  # noqa: BLE001 — a missing binary / timeout / OS error all degrade to "unavailable"
        return None


def repo_slug() -> str | None:
    """`owner/repo` for the GitHub reads, derived from the origin remote (env wins for CI). None when
    it cannot be determined — the live reads then degrade to the offline/floor posture."""
    env = os.environ.get("GITHUB_REPOSITORY")
    if env:
        return env
    url = _run(["git", "remote", "get-url", "origin"])
    if not url:
        return None
    m = re.search(r"github\.com[:/]+([^/]+/[^/]+?)(?:\.git)?$", url)
    return m.group(1) if m else None


def gh_token() -> str | None:
    """A GitHub token for the live reads: the environment first (CI), else the operator's own logged-in
    `gh` CLI (so a logged-in laptop gets the REAL protected-branch + findings reads). None when neither
    is available — the live reads then degrade, never error."""
    env = os.environ.get("GITHUB_TOKEN")
    if env:
        return env
    return _run(["gh", "auth", "token"])


# ---- committed state (the card facts; refuse-on-malformed) ----------------------------------

def _cursor_conforms(state: dict) -> bool:
    """True iff `state` validates against the state.v1 schema — the INNER-shape check read_state layers over
    the cheap version gate (U15a), so a schema_version-1 cursor with a broken/missing inner shape is REFUSED,
    not rendered as a confident 'all clear' (D-059 law 1: a malformed cursor fails loud, never misleads).

    An INFRASTRUCTURE fault — the schema file itself unreadable, or the validator unavailable — is NOT the
    cursor's fault, so it does NOT refuse: it returns True (falling back to the pre-existing version-only
    acceptance) rather than blame a good cursor for the engine's own missing schema. Only a genuine
    non-conformance (the validator reporting errors on a present schema) refuses."""
    try:
        schema = validate.load_json(_STATE_SCHEMA_PATH)
    except Exception:  # noqa: BLE001 — a missing / corrupt schema is an engine fault, never the cursor's
        return True
    try:
        return not list(validate.Draft202012Validator(schema).iter_errors(state))
    except Exception:  # noqa: BLE001 — a validator fault must not blame a good cursor
        return True


def read_state() -> tuple[dict | None, bool]:
    """Return (state, refused). `refused` is True when the committed cursor is unreadable, is not a
    schema_version-1 cursor, or does not conform to the state.v1 schema (a version-1 cursor with a broken
    INNER shape is refused, never rendered as a confident cursor — U15a / D-059 law 1) — boot then says
    project status is unknown and falls through to the rest of the pack, NEVER halting. A readable,
    conforming cursor returns (state, False), rendered defensively with .get().

    This is a PURE read/predicate. Boot surfaces the refusal in-band (the operator-facing half) in the
    dashboard/marker renders. The DURABLE half — the D-059 telemetry finding on a refused cursor — is emitted
    on the REAL SessionStart path only (assemble_pack, use_ledger), as a benign inbox-spool append via
    emit_refused_cursor_finding(): a GitHub write here would break boot's read-only posture, so the benign
    spool carries it and the #412 drain promotes it. Keeping the emit out of this read leaves the status
    verb / `pack` debug view side-effect-free and this predicate cheaply unit-testable."""
    try:
        state = validate.load_json(STATE_PATH)
        if not isinstance(state, dict) or state.get("schema_version") != 1:
            return None, True
        if not _cursor_conforms(state):
            return None, True
        return state, False
    except Exception:  # noqa: BLE001 — absent / malformed cursor degrades to "unknown", never a crash
        return None, True


def _refused_cursor_message() -> str:
    """The plain-language, engine's-own-health copy of the durable refused-cursor finding (U15b). Names what
    the operator must do (correct the saved record, or let the engine re-ground) and does NOT imply the
    engine self-repairs or self-closes it; no backstage vocabulary (spool / drain / severity / schema). The
    first sentence is a clean, title-length summary (issue_title derives the title from it)."""
    return (
        "The engine couldn't trust its saved record of where this project stands. "
        "That record no longer has the shape it needs, so the engine is treating the project's status as "
        "unknown rather than show a confident-but-wrong summary — this is about the engine's own bookkeeping, "
        "not your project or its data. Correcting that saved record, or letting the engine re-ground from "
        "GitHub, is what clears it; until then, don't rely on any 'where we are' status."
    )


def emit_refused_cursor_finding(*, spool_path: str | None = None) -> bool:
    """Emit ONE benign refused-cursor finding to the telemetry inbox spool (D-059 law 1's durable half, U15b).
    PERSISTENT_BENIGN routes emit_finding to a LOCAL gitignored spool append — boot never writes GitHub
    (read-only posture); the #412 drain promotes it once it persists across sessions, and the immediate
    operator surfacing is the existing in-band notice. Best-effort / fail-open (emit_finding swallows every
    fault). `spool_path` defaults to telemetry's inbox spool, resolved at CALL time (not frozen in the
    signature) so a test can redirect it at telemetry.INBOX_SPOOL_PATH. Returns emit_finding's result (falsy
    on the benign path — a spool append is capture, promoted later)."""
    record = {"source_id": REFUSED_CURSOR_SOURCE_ID, "severity": telemetry.PERSISTENT_BENIGN,
              "message": _refused_cursor_message(), "location": None}
    return telemetry.emit_finding(record, spool_path=spool_path or telemetry.INBOX_SPOOL_PATH)


# ---- governance alarms (relayed from the substrates; pinned at the top of the card) ---------

def protected_branch_signal(repo: str | None, token: str | None) -> tuple[str, str | None]:
    """The protected-branch governance signal, RELAYED from protection_guard (the control-plane's own
    evaluation), in three honest states:
      ("off", reason)       -> the gate is NOT in force: a pinned governance alarm that OFFERS the fix.
                               boot stays read-only and only offers; the assistant runs the already-built,
                               idempotent one-click apply (bootstrap.ControlPlane.apply) on the operator's
                               consent — the shared repair-offer contract (boot-session-start.md).
      ("on", None)          -> the gate fully bites: no alarm.
      ("unknown", None)     -> boot could not verify it (no token/repo/unreachable): a clear degraded line
                               that must NEVER read as a green all-clear.
    """
    if not repo or not token:
        return "unknown", None
    try:
        rules = protection_guard.get_json(
            f"/repos/{repo}/rules/branches/{PROTECTED_BRANCH}", token,
            user_agent=protection_guard.UA)  # reuse the protection guard's UA — the same probe, same identity
        if not isinstance(rules, list):   # a 200 with an unexpected body (an error object, null) is NOT
            return "unknown", None         # a confirmation that protection is on -> honest "unknown"
        # Read the repo's identity tier so a team repo's orientation card reflects the STRONGER team floor
        # (code-owner review + last-push approval), not just the solo baseline — matching what the standing CI
        # check enforces (U11).
        missing = protection_guard.missing_floor(
            rules, protection_guard.REQUIRED_CHECKS, tier=protection_guard.resolve_tier())
    except Exception:  # noqa: BLE001 — unreachable / auth / malformed body -> unknown, never a false "on"
        return "unknown", None
    if missing:
        return "off", "; ".join(missing)
    return "on", None


def open_findings(repo: str | None, token: str | None) -> tuple[int | None, str | None, list | None, int | None, list | None]:
    """The engine's open self-monitoring findings, RELAYED read-only from telemetry's debt register
    (the engine-labelled open Issues) via telemetry's own reader — NEVER the write loop. Returns
    (count, register_url, fingerprint, low_severity_count, findings): count is None when the register could not be
    read (degraded), 0 when the register is reachable and empty. `fingerprint` is the STRUCTURED-CONDITION
    identity of the open set — a SORTED list of each finding's stable identity (its source_id, else
    `#<issue-number>`) — the value the anti-habituation ledger compares, so a close+open at EQUAL count reads
    as CHANGED and is never mis-collapsed to "unchanged" (D-269 / R19). Duplicates are PRESERVED (two open
    Issues sharing one source_id keep both tokens, so closing one still moves the fingerprint). `low_severity_
    count` is the COMPLETE count of open low-impact (persistent-but-benign) engine Issues — the render-only
    triage-pressure meter's authoritative input, read from the durable Issue set (each Issue's severity marker)
    in this SAME single read, so it counts CI + ambient + every low-severity source, not the per-machine subset
    a scoped triage pass could see. An Issue with no severity marker (a pre-severity Issue) is not counted
    until telemetry next updates it. `findings` is the PER-ISSUE projection ({number, source_id, severity,
    title}) the ranking grades into one blocking-debt candidate EACH — carried out of this SAME single read, so
    attention's per-issue severities and the card header's count can never disagree and the SessionStart path
    still makes no second GitHub call (`count == len(findings)` by construction). The `title` rides along
    because a finding that surfaces needs to say WHICH problem it is: without it every finding line reads
    identically but for its number, which is a wall to scan rather than something to triage. Only the
    identifying fields travel; the Issue BODY never enters the pack. All five values are None when degraded,
    so they track together. Boot only reads; telemetry owns the register."""
    if not repo or not token:
        return None, None, None, None, None
    try:
        gh = telemetry.GitHubIssues(repo, token)
        issues = gh.list_open_engine_issues()
        fingerprint = sorted((i.get("source_id") or f"#{i['number']}") for i in issues)
        low = sum(1 for i in issues if i.get("severity") == telemetry.PERSISTENT_BENIGN)
        findings = [{"number": i.get("number"), "source_id": i.get("source_id"),
                     "severity": i.get("severity"), "title": i.get("title") or ""}
                    for i in issues]
        return len(issues), gh.issues_query_url(), fingerprint, low, findings
    except Exception:  # noqa: BLE001 — DegradedReadError or any transport failure -> unknown (degraded)
        return None, None, None, None, None


# ---- attention (consume the ranked partition; resolve member ids to plain language) ---------

def _resolve_member(member_id: str, state: dict | None, titles: dict | None = None) -> str:
    """Resolve one attention member id (a reference, not content) to a plain-language line. Boot
    resolves; it does not re-rank. Unknown ids fall back to the id itself so nothing is silently lost.

    `titles` re-joins the ranked member ids with the human names `rank()` strips (it reduces every member to
    {id, rank}) — the same channel the shipped digest and the knowledge neighbourhood need, for the same
    reason. Without it a register of open findings renders as lines identical but for a number."""
    if member_id == "state:standing-situation":
        # NOT surfaced as an action line. The card already shows "Where we are" live in the facts block above
        # (fresh each session), and when that live read fails it carries its own stale-warning right there — so
        # a separate "confirm where you stand" nudge would be redundant in the fresh case and a duplicate of
        # that stale-warning in the failure case. Attention still ranks this orientation pointer for the budget
        # model; boot just doesn't nag with it. Returning "" -> needs_attention skips it (no blank bullet).
        return ""
    if member_id == "state:integration-debt":
        # The OFFLINE stand-in only (the live register could not be read, so state's committed count carried it).
        # No count here: the card header already renders the authoritative open-problem figure (live
        # when reachable, else the offline shadow marked loud-if-stale). Restating a second, possibly-
        # disagreeing number would undercut it — so this line is the actionable nudge only.
        return "Open integration debt is waiting — clear it before new work piles on top."
    if ":" in member_id:
        kind, _, slug = member_id.partition(":")
        if kind == "finding":    # ONE open engine finding from the live debt register, graded blocking by the
            # policy's debt-blocking rule. Only findings that actually CLEAR the bar reach here — a sub-threshold
            # (benign) one, and an ungraded one, are deferrals assign_partition drops, so this line never cries
            # wolf over backlog. The per-kind budget bounds how many surface, so a deep register cannot flood
            # the card. The title says WHICH problem it is: several blocking findings at once are a list to
            # triage, and without their names they are only distinguishable by a number the operator would have
            # to go look up. Defanged — a finding's title can quote a check-run name from outside the repo.
            name = validate.defang_prompt_fence_markers((titles or {}).get(member_id) or "")
            if name:
                return (f"Engine finding #{slug} — {name} — is open and blocking; clear it before new work "
                        f"piles on top.")
            return f"Engine finding #{slug} is open and blocking — clear it before new work piles on top."
        if kind == "pr":         # an open pull request in flight (the work record's GitHub layer)
            return f"Pull request #{slug} is open and in flight — pick it back up, or close it if it's done."
        if kind == "branch":     # the working branch in flight (the work record's local-git floor)
            return f"You have unmerged work on branch '{slug}' — carry it forward or set it down deliberately."
        return f"Related: {slug} ({kind}) — query and verify before relying on it."
    return member_id


def _slug(member_id: str) -> str:
    """The bare slug of an entity id (`tool:attention` -> `attention`, `module:core` -> `core`) — the
    AI-/operator-legible name, never the raw `kind:slug` id."""
    return member_id.split(":", 1)[-1] if member_id else ""


def _and_list(items: list) -> str:
    """Join plain phrases into a readable clause: '' / 'a' / 'a and b' / 'a, b and c'. For the degraded
    notice, so it reads as a sentence ('I couldn't reach a and b') rather than a comma-joined dump."""
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} and {items[-1]}"


def needs_attention(state: dict | None, *, gh=None, live_findings: list | None = None,
                    source=None) -> tuple[list, list, dict | None, list, list]:
    """Consume attention.rank_live and SPLIT its ranked partition into (1) operator ACTION lines, rendered in
    the GIVEN precedence order as plain language (a bounded prefix per category — boot renders, never
    re-orders), and (2) the knowledge NEIGHBORHOOD of the work in hand. The neighborhood is AI-orientation
    context, NOT an operator action item, so `structural_neighbors` are routed to the pack's neighborhood
    block (assemble_pack) and never to the action list; `recent_decisions` are likewise routed out — its two
    halves to the "recently shipped" digest (merged PRs) and the recalled-decisions block (the memory recall),
    since what already happened is not something needing attention. Returns
    (action_lines, degraded_inputs, neighborhood, shipped_lines, recalled_entries).

    The focus is DERIVED here from the in-flight work record (#37): the files the work touches -> their owning
    entities -> a focused knowledge read. `gh` is the GitHub reader boot built from the live repo/token;
    attention reads the work record (open PRs + the working branch) through it, and the focus from the local
    git floor (no token needed). `live_findings` is the live debt register's PER-ISSUE rows boot already read
    (open_findings), threaded to the assembler so it grades each open finding on its own severity while the card
    header reads the SAME read's count (`len(live_findings)`) — one read, so they cannot disagree, and no second
    GitHub call. When it is None (no reader / a failed read) telemetry degrades and the committed count stands
    in, so degraded_inputs carries `telemetry` and boot raises the loud 'couldn't reach' notice."""
    # Boot's RUNG-1 knowledge read (#37): a fresh boot slice is read once and threaded into every knowledge
    # read below, so orientation reads the gitignored cache instead of the SQLite index. `read()` fail-opens to
    # None (a missing/stale/broken slice, or knowledge unavailable) — then the reads run on `knowledge_query`
    # exactly as before (the shared rungs 2-4), or boot orients without the block. Never blocks boot. The caller
    # (gather_signals) reads the slice ONCE and passes it in — so the same read also yields the `from_live`
    # provenance for the rebuilt-map heads-up without a second read; `source=None` (the CLI/tests) reads here.
    if source is None:
        source = boot_slice.read()
    try:
        # with_total: the count BEHIND the cap, so the render discloses focus truncation honestly (#165).
        focus, focus_total = attention.derive_focus(gh=gh, with_total=True, source=source)
    except Exception:  # noqa: BLE001 — focus derivation is best-effort; the rest of the pack stands
        focus, focus_total = [], 0
    try:
        # Load the operator policy-override (operator config, absent until first tuned) and pass attention's
        # slice as DATA — boot is the LOADING layer; attention merges it per-key (D-167), never reads the file.
        # The work record, by contrast, is a SUBSTRATE attention reads itself (through the gh reader boot hands it).
        # The memory half of recent decisions, pulled ONCE here: the same rows feed the ranking (which orders
        # and budgets them) and the render below (which needs the text `rank()` strips), so the block can never
        # show a decision the ranking did not rank.
        recall_rows = _recent_decisions_recall()
        # The merged-PR half, read ONCE for the same reason: the ranking needs the moments and the digest
        # below needs the titles rank() strips. Read twice, that is two `git log` spawns per session AND a
        # seam — a merge landing between them would leave the digest naming a number with no title.
        try:
            shipped_rows = work_record.read_recent_decisions()
        except Exception:  # noqa: BLE001 — the floor read is best-effort; attention re-reads and degrades
            shipped_rows = None
        result = attention.rank_live(override=operator_overrides.slice_for("attention") or None,
                                     focus=focus or None, gh=gh, source=source, live_findings=live_findings,
                                     memory_recall=recall_rows, shipped=shipped_rows,
                                     budget_total=COLD_START_BUDGET)
    except Exception:  # noqa: BLE001 — attention unavailable -> no ranked lines, the rest of the pack stands
        return [], ["attention"], None, [], []
    # The finding names, from the SAME rows the ranking graded — so a line can never name a finding the
    # ranking did not rank, and no second read is made for the sake of the wording.
    finding_titles = {f"finding:{r.get('number')}": r.get("title") for r in (live_findings or [])}
    lines: list = []
    for entry in result.get("partition", []):
        if entry.get("category") == "structural_neighbors":
            continue        # the knowledge neighbourhood is the AI pack block (rendered from the richer
                            # neighborhood_of summary below), never an operator action line
        if entry.get("category") == "recent_decisions":
            continue        # what already SHIPPED is not an action item: it is the "recently shipped" digest,
                            # rendered from _shipped_lines below (which restores the titles rank() strips)
        # The attention policy's reviewable per-kind budget governs how many items this kind surfaces (the
        # buried flat cap is retired). budget_size is 0 for a kind the trim order shed under a tight budget —
        # so it naturally contributes nothing — but at the shipped COLD_START_BUDGET every kind seats, so
        # nothing is shed here. NEEDS_ATTENTION_CAP is only the defensive floor for a budget-less result.
        cap = entry.get("budget_size", NEEDS_ATTENTION_CAP)
        for member in (entry.get("members") or [])[:cap]:
            line = _resolve_member(member.get("id", ""), state, finding_titles)
            if line:                       # skip an id-less member rather than render a blank bullet
                lines.append(line)
    # The focused knowledge read's render channel (#37 / D-224): a per-(member, relationship) summary that
    # PRESERVES the full neighbour counts the ranked partition strips, so render_neighborhood discloses
    # truncation honestly ("core provides 147, showing 4") instead of an arbitrary capped few passed off as
    # the whole. Best-effort — a failure degrades to no block, never breaks the rest of the pack.
    try:
        neighborhood = attention.neighborhood_of(focus, source=source) if focus else None
    except Exception:  # noqa: BLE001 — the neighbourhood is orientation context; its loss never breaks the pack
        neighborhood = None
    if neighborhood is not None:
        neighborhood["focus_total"] = focus_total   # the true count behind FOCUS_CAP, for honest disclosure (#165)
    return (lines, list(result.get("degraded_inputs") or []), neighborhood,
            _shipped_lines(result, read=(lambda: shipped_rows) if shipped_rows is not None else None),
            _recalled_entries(result, recall_rows))


# (predicate, direction) -> the plain-language relationship phrase for the AI orientation render. §12: these
# are VERBS only — never the internal type nouns ("surface"/"module"/"check"/"policy"/"schema"); the slugs
# already name the things. A walk edge is provided_by/governed_by/targets/depends_on; "in" means the edge
# points AT the focus — the reverse connective tissue D-224 surfaces.
_RELATION_PHRASE = {
    ("provided_by", "out"): "is part of",
    ("provided_by", "in"): "provides",
    ("governed_by", "out"): "is governed by",
    ("governed_by", "in"): "governs",
    ("targets", "out"): "checks",
    ("targets", "in"): "is checked by",
    ("depends_on", "out"): "depends on",
    ("depends_on", "in"): "is relied on by",
}


def render_neighborhood(nb: dict | None) -> list:
    """The AI-facing "knowledge neighborhood of your current work" orientation block, from the per-(member,
    relationship) summary `attention.neighborhood_of` derived — or [] when there is no work in hand. This is
    orientation CONTEXT for the model (the focused knowledge read, #37), NOT an operator alarm and NOT an
    action item; it carries no RELAY_MARKER.

    The walk is bidirectional (D-224): a connective focus surfaces its reverse tissue — its governing rule, its
    dependents, the checks that target it — not just the module it lives in. Each relationship is rendered with
    its TRUE count, so a highly-connected focus reads "core provides 147 (showing 4: ...)": the sample is
    DISCLOSED as a sample, never an arbitrary capped few passed off as the whole or the salient set (honest
    truncation — ranking WHICH few is relevant is deferred, D-224 Q38/Q39). A genuinely bare leaf (its only
    edge is `is part of` -> its module) honestly reads module-only. Plain words throughout (§12): relationship
    verbs + slugs, never raw ids or internal type nouns.

    When the focus itself was truncated (more files were changed than `FOCUS_CAP` shows), the header discloses
    the true count too ("touching: a, b, c, d, e (showing 5 of 7 you've changed)", #165) — the same honesty as
    the per-relationship counts, one level up, so the shown focus is never passed off as the whole change."""
    if not nb or not nb.get("focus"):
        return []
    focus = nb["focus"]
    focus_names = ", ".join(_slug(f) for f in focus)
    total = nb.get("focus_total") or len(focus)
    touching = (f"You're touching: {focus_names} (showing {len(focus)} of {total} you've changed)."
                if total > len(focus) else f"You're touching: {focus_names}.")
    out = ["--- knowledge neighborhood of your current work (orientation context, not an alarm) ---",
           touching]
    rel_lines: list = []
    for g in nb.get("groups") or []:
        phrase = _RELATION_PHRASE.get((g.get("predicate"), g.get("direction")))
        sample = [s for s in (_slug(x) for x in (g.get("sample") or [])) if s]
        if not phrase or not sample:
            continue
        src, total = _slug(g.get("source", "")), g.get("total", len(sample))
        if total <= 1:
            rel_lines.append(f"  {src} {phrase} {sample[0]}")
        elif total <= len(sample):                 # the whole set fits the sample -> the slugs ARE the full list
            rel_lines.append(f"  {src} {phrase}: {', '.join(sample)}")
        else:                                        # truncated -> disclose the TRUE count AND that the shown few
            # are arbitrary examples, not a ranked top-N (which few matter most is deferred, D-224 Q38/Q39), so
            # the sample can never read as "the 4 that matter".
            rel_lines.append(f"  {src} {phrase} {total} "
                             f"(showing {len(sample)} examples, not ranked by importance: {', '.join(sample)})")
    out.extend(rel_lines or ["  (nothing else is connected to your work in the graph yet)"])
    out.append("Pull deeper with the knowledge-graph tools if a change reaches into them.")
    out.append("")
    return out


# ---- "what just happened" — merged PRs, never a changelog -----------------------------------

def _recent_decisions_recall(read=None) -> list[dict]:
    """The saved memory's most recent DECISIONS, pulled read-only for attention's recent-decisions partition.

    Attention's recent decisions are "recently merged pull requests … **and** the memory recall boot assembles
    into the pack" (attention/README:49); cold start "pull[s] knowledge structure and memory recall when their
    servers are up" (boot/README:67). BOOT does that pull and RELAYS the rows to the ranking — attention never
    queries memory itself (D-154's anti-choice keeps memory off attention's direct-reads list, preserving the
    §16 model: memory detects and owns its store, boot relays, attention ranks what it is handed).

    The pull is non-lexical on purpose: a cold start has no prompt to match against, so it asks "what was
    decided lately?" (recency-ordered) — the per-prompt scent is what asks "what relates to THIS?".

    Normalises each record for the pure ranker: the ledger stores an epoch `ts`, the ranking reads a trailing-Z
    moment, so the conversion happens HERE at the relay boundary rather than letting a raw epoch reach the
    ranking math. Memory is imported LAZILY (it is off the cold-start import path) and every fault degrades to
    [] — an unreadable store costs the recall, never the pack, and boot already surfaces an unreadable store as
    its own plain-language memory-offline notice rather than from here."""
    try:
        from memory import index as _mem_index, records as _mem_records
        out: list[dict] = []
        for r in (read or _mem_index.recent_decisions)():
            ts, rid = r.get("ts"), r.get(_mem_records.RECORD_ID_KEY)
            if not rid or not isinstance(ts, (int, float)) or isinstance(ts, bool):
                continue          # no stable id / no usable moment -> it cannot be ranked or cited; skip it
            out.append({"id": str(rid), "text": (r.get("text") or ""),
                        "recency": datetime.datetime.fromtimestamp(
                            ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
        return out
    except Exception:  # noqa: BLE001 — recall is orientation context; its loss never breaks the pack
        return []


def _set_aside_recall(read=None) -> "dict | None":
    """Memory's own report of what it has set aside from recall — pulled read-only and RELAYED to the dashboard
    readout. Boot computes no new state here: memory owns the mechanism (it decides what is set aside), boot
    owns only the wording of the readout. Lazy import (memory is off the cold-start path); every fault degrades
    to None — an unreadable store costs the readout, never the pack, and boot already surfaces an unreadable
    store as its own memory-offline notice, never from here. None means "not read"; a report (even an empty
    one) means "read, and here is what is set aside"."""
    try:
        from memory import forget as _forget
        report = (read or _forget.set_aside)()
        rows = [r for r in report.get("rows", [])
                if isinstance(r.get("id"), str) and r.get("id") and isinstance(r.get("text"), str) and r["text"].strip()]
        return {"rows": rows, "totals": report.get("totals", {"demoted": 0, "summarised": 0}),
                "identity": report.get("identity", [])}
    except Exception:  # noqa: BLE001 — the readout is orientation context; its loss never breaks the pack
        return None


def _recent_entry_members(result: dict) -> list:
    """Every member the ranking placed in the recent-decisions partition, in its order and UNBOUNDED.

    The budget decides what is shown; this is what was found. A render needs both to tell "there is none of
    this" apart from "there is, and it did not fit" — two claims a bounded list alone cannot distinguish."""
    entry = next((e for e in result.get("partition", []) if e.get("category") == "recent_decisions"), None)
    return list((entry or {}).get("members") or [])


def _recent_members(result: dict) -> list:
    """The recent-decisions partition's members, in the ranking's own order and bounded by its budget slice.

    The partition carries BOTH halves of the spec's recent decisions — merged pull requests (`shipped:`) and
    the memory recall boot relays (`memory:`) — and they share ONE budget: `budget_recent_decisions` sizes the
    category, not each source. So the bound is applied HERE, to the ranked whole, and only then split by
    source; filtering first and bounding each half would quietly hand out twice the budget the policy set.

    KNOWN CALIBRATION, recorded rather than corrected: on an active repo merges land far more often than
    decisions are consolidated into memory, so the merged-PR half will normally take the whole slice and the
    recall block will often be empty. That is the shared budget working as specified — one partition, one
    budget — and the budget VALUES are explicitly uncalibrated build-spec leaves (D-052/D-113), tunable via
    `/engine-tune`. Splitting the slice per-source to "fix" it would invent a sub-budget the policy does not
    have. Worth revisiting only with real usage to calibrate against."""
    entry = next((e for e in result.get("partition", []) if e.get("category") == "recent_decisions"), None)
    if not entry:
        return []
    return (entry.get("members") or [])[:entry.get("budget_size", NEEDS_ATTENTION_CAP)]


def _recalled_entries(result: dict, rows: list) -> list:
    """The MEMORY half of the ranked recent-decisions partition: the budget-bounded members, in the ranking's
    order, re-joined with the record text `rank()` strips (it reduces every member to {id, rank}) — the same
    reason the knowledge neighbourhood needs its own channel."""
    by_id = {f"memory:{r.get('id')}": r for r in (rows or [])}
    return [by_id[m["id"]] for m in _recent_members(result)
            if m.get("id") in by_id]


def render_recalled_decisions(entries: list | None) -> list:
    """The AI-facing "decisions recorded recently" orientation block — the MEMORY half of attention's
    recent-decisions partition (attention/README:49), pulled at cold start (boot/README:67), ordered by the
    ranking and bounded by the policy's budget slice.

    Orientation CONTEXT for the model, like the knowledge neighbourhood: it sits above the dashboard divider,
    carries no RELAY_MARKER, and is not an operator alarm or an action item (§12 — the engine's own machinery
    stays out of operator narration).

    ATTRIBUTED, not confirmed. These are the project's own recorded decisions, which is exactly why the block
    says so rather than asserting them: a decision can have been superseded since it was written, and the
    ledger records what WAS decided, never a promise it still holds. Same trust seam the per-prompt scent
    carries — a pointer the model verifies before asserting, never content it repeats as current fact.

    Each record's text is defanged: it is replayed into the model's context and a session can have pasted
    anything into the notes it was consolidated from. [] when nothing was recalled (a fresh project, an
    unreadable store) — no block at all, never an empty heading."""
    if not entries:
        return []
    out = ["--- decisions recorded recently (orientation context, not an alarm) ---",
           "From the project's saved memory, newest first. Attributed, not confirmed — a decision here may "
           "have been superseded; check before you rely on it."]
    for e in entries:
        text = " ".join((e.get("text") or "").split())
        if not text:
            continue
        if len(text) > _RECALL_SNIPPET_CHARS:
            text = text[:_RECALL_SNIPPET_CHARS].rstrip() + "…"
        out.append(f"  • {validate.defang_prompt_fence_markers(text)}  (recorded {e.get('recency')})")
    if len(out) == 2:          # every entry was blank -> no block rather than a bare heading
        return []
    out.append("")
    return out


_SET_ASIDE_SHOW = 3    # how many most-recent notes of each class the readout names inline; the true total is
#                        always stated, and "ask me to list them all" reaches the rest — so the block stays a
#                        brief orientation cue, never a wall (a long-lived store sets aside many notes).


def _n_notes(count: int) -> str:
    """'1 note' / 'N notes' — a plain singular/plural so the readout never shows the robotic 'note(s)'. The
    readout renders every session and its whole job is to reassure, so this polish is load-bearing, not cosmetic."""
    return f"{count} note" if count == 1 else f"{count} notes"


def _set_aside_snippet(text) -> str:
    """One defanged, length-bounded line of a set-aside note's own words — the same treatment
    render_recalled_decisions gives recall text, and load-bearing for the same reason: this readout replays
    ledger text into the model's context, and a session can have pasted anything into the notes a summary was
    built from."""
    text = " ".join(str(text or "").split())
    if len(text) > _RECALL_SNIPPET_CHARS:
        text = text[:_RECALL_SNIPPET_CHARS].rstrip() + "…"
    return validate.defang_prompt_fence_markers(text)


def render_set_aside(sa: "dict | None") -> list:
    """The operator-facing readout of what memory has set aside from recall, so a quiet loss of the operator's
    own notes never goes unseen. Two things it can name, each with an honest handle:
      * notes set aside because nothing has come back to them in a while — offered to bring back (a real,
        mechanical restore); and
      * notes folded into a shorter summary — offered to show in their original wording (there is no un-fold;
        the summary stands in for them, and the readout never pretends otherwise).
    Nothing is ever deleted by either; the readout says so. Permanent erasure is NOT shown here — it is not a
    boot event and rides the audits digest instead.

    Bounded: a few most-recent notes plus the true total, so it never grows into noise. Repetition across
    sessions is handled by the caller (the same collapse machinery the pushed alarms use): `collapsed` renders
    one terse line that still carries both offers; `newly` names how many were set aside since the operator
    last saw this. [] when there is nothing set aside (a fresh or tidy project, or an unread store) — no block,
    never an empty heading.

    Every note's words go through `_set_aside_snippet`; no record id ever reaches this operator-facing text
    (the id is the machine binding the AI uses behind the scenes, never shown)."""
    if not sa:
        return []
    rows = sa.get("rows") or []
    totals = sa.get("totals") or {}
    demoted_total, summarised_total = totals.get("demoted", 0), totals.get("summarised", 0)
    total = demoted_total + summarised_total
    if total == 0:
        return []

    # Each offer names ONLY a class that is actually set aside: the terse form must never invite the operator to
    # "bring back" a note when the only thing set aside is a summarised one (which cannot be brought back), nor
    # offer to "show the original wording" when nothing is summarised.
    offers = []
    if demoted_total:
        offers.append("bring one back into search")
    if summarised_total:
        offers.append("show you the original wording of one")
    offer_sentence = "You can ask me to " + " or ".join(offers) + " whenever you like." if offers else ""

    if sa.get("collapsed"):
        bits = []
        if demoted_total:
            bits.append(f"{_n_notes(demoted_total)} set aside because nothing's come back to them")
        if summarised_total:
            bits.append(f"{_n_notes(summarised_total)} folded into a shorter summary"
                        if summarised_total == 1 else f"{summarised_total} notes folded into shorter summaries")
        return ["### Notes I've set aside",
                f"Still {', and '.join(bits)} (unchanged since last session). Nothing was deleted — they're all "
                f"still saved. {offer_sentence}", ""]

    newly = sa.get("newly")
    lead = f"I've set aside {_n_notes(total)} from what I search"
    if isinstance(newly, int) and newly > 0:
        lead += f" — {newly} more since you last saw this"
    # "still saved", NOT "fully recoverable": a demoted note comes all the way back, but a summarised one can
    # only be shown in its original wording, never returned to search — the per-class bullets carry that.
    out = ["### Notes I've set aside", f"{lead}. Nothing was deleted — every one is still saved."]

    demoted_rows = [r for r in rows if r.get("reason") == "demoted"]
    summarised_rows = [r for r in rows if r.get("reason") == "summarised"]
    if demoted_rows:
        out.append(f"- Set aside because nothing's come back to them in a while ({demoted_total} in total). "
                   "Name any one and I'll bring it back into search. Most recent:")
        for r in demoted_rows[:_SET_ASIDE_SHOW]:
            out.append(f"  - {_set_aside_snippet(r.get('text'))}")
    if summarised_rows:
        out.append(f"- Folded into a shorter summary ({summarised_total} in total). The summary is what I "
                   "search now; the originals are kept word-for-word, and I can show you the exact wording of "
                   "any of them. Most recent:")
        for r in summarised_rows[:_SET_ASIDE_SHOW]:
            out.append(f"  - {_set_aside_snippet(r.get('text'))}")
    out.append("Ask me to list them all whenever you like.")
    out.append("")
    return out


def _shipped_lines(result: dict, *, read=None) -> list[str]:
    """The "recently shipped" digest — reconstructed from merged pull requests (the structured PR body is the
    engine's narrative; there is no changelog file), rendered from ATTENTION's ranked recent_decisions
    partition.

    Which decisions surface, and how many, is now the policy's reviewable `budget_recent_decisions` slice and
    the partition's own recency ordering — retiring the buried RECENTLY_SHIPPED_COUNT constant (#394 U01).
    This needs its own render channel for the same reason the knowledge neighbourhood does: `rank()` reduces
    every member to {id, rank}, so the PR titles are stripped. The partition supplies WHICH and IN WHAT ORDER;
    this read supplies their titles. Shipped work is not an operator ACTION item, so it is routed here and
    never into the attention lines.

    Every title is defanged before it lands in the pack: a merged pull request's title is authorable by an
    outside contributor, and this text reaches the cold-boot model's context.

    This returns the WHOLE body of the section, absence copy included, and never an empty list — so the
    render cannot invent an absence claim this read never verified. "No recent merges" is a factual claim
    about the project, and there are three different reasons this digest can come up empty: none were
    ranked (the claim is true), some were ranked but the shared recency budget went to newer decisions
    (there ARE recent merges — claiming otherwise is simply false), or the title read failed (the honest
    answer is "couldn't read", not "none"). Only the read that can tell them apart may word the line."""
    # The MERGED-PR half of the (budget-bounded, shared) recent-decisions slice — the recall half renders as
    # AI-facing orientation, never as an operator-facing shipped list.
    ranked = [m for m in _recent_entry_members(result) if str(m.get("id", "")).startswith("shipped:")]
    members = [m for m in _recent_members(result) if str(m.get("id", "")).startswith("shipped:")]
    if not members:
        # Ranked-but-shed vs never-ranked: only the first is a merge the operator has that we are not showing.
        # The shed case must not point at what beat it: the recall half of this partition renders ABOVE the
        # dashboard divider, in the AI's briefing, so "it didn't make the list" would name a competition the
        # reader cannot see — and reads to him as "nothing shipped", the exact claim this read exists to
        # avoid. So it says the merges are there and that they are not shown, and nothing more.
        return ["(there are recent merges — none of them made this session's short list)"] if ranked else \
               ["(no recent merges found)"]
    try:
        titles = {r["id"]: (r.get("title") or "") for r in (read or work_record.read_recent_decisions)()}
    except Exception:  # noqa: BLE001 — the digest is orientation context; its loss never breaks the pack
        return ["(couldn't read the recent merges this session)"]
    out: list[str] = []
    for m in members:
        mid = m.get("id", "")
        title = validate.defang_prompt_fence_markers(titles.get(mid, ""))
        out.append(f"#{mid.partition(':')[2]} — {title}".strip(" —"))
    return out


# ---- assembly: gather signals -> render the operator dashboard -> wrap the AI briefing ------

# The imperative relay marker (glossary): the directive embedded in the AI-facing briefing that compels the
# AI to surface a high-impact item to the operator in plain words. RESERVED for the must-push set so the
# relay never becomes a firehose — routine status carries no marker and is pulled via the status verb.
RELAY_MARKER = "INFORM THE USER THAT"

# #400 F1 — the engine's live-helper (MCP substrate) availability notice. CONSENT-CRITICAL: when a server is
# not live this session the operator runs on the committed-file fallback and, unable to read code, has no other
# way to know. Boot CANNOT detect MCP tool routing — it reads committed files only, so RELAY-NOT-DETECT is
# preserved (boot computes nothing here); the check is one the MODEL runs against its own tool list. It is
# therefore emitted in the operator-RELAY portion of the pack (a numbered must-do, NOT the AI-orientation zone
# where KNOWLEDGE_FACULTY_NOTE sits), so an absent server carries the same "do not skip" force as any gathered
# alarm. Per-server naming; status-not-fault framing; host-agnostic fix (no Code-only `/mcp` baked into consent-
# critical copy — consistent with the deployed floor's "reopen Claude"). Single-homed: the PR-Validation runbook
# and the first-run guide point at THIS behaviour, they do not re-type the words. Residual bound (no non-AI
# correlate, disclosed at the merge gate): a skipped self-check leaves a degraded engine looking healthy, and a
# mis-read can false-fire on a healthy one — the must-relay placement is the strongest available mitigation.
MCP_AVAILABILITY_CHECK = (
    "Check your own available tools for the engine's two live helpers — each is a server the operator approves "
    "once, and it only comes online after the Claude app is fully restarted:\n"
    "     - `mcp__engine-memory__*` — their saved memory (recall of past decisions and notes)\n"
    "     - `mcp__engine-knowledge-graph__*` — the engine's wiring map (how the parts connect)\n"
    "   For EACH of these families ABSENT from your tools this session, you MUST tell the operator, in plain "
    "words (this is consent-critical — treat it like any must-relay alarm above, never as internal machinery): "
    "that its live version isn't switched on this session, so you're working from their saved files instead — "
    "which still works, but can be out of date; and that to switch it on they approve the engine's servers when "
    "their Claude app prompts them (or in its MCP settings), then fully quit and reopen Claude. If no prompt "
    "appears, or they say they already approved it but a family is still missing, offer to help them turn it on "
    "— find the setting for their Claude app, or look into why the server won't start. If BOTH families are "
    "present, say nothing about this."
)


def gather_signals(session_id: str | None = None) -> dict:
    """Read + DETECT every signal the dashboard renders — the substrates' own detection, which boot only
    relays (it computes no new state). Each read is best-effort upstream and degrades that signal only.
    Returns a flat dict consumed by render_dashboard / present_marker_line / must_push — the single place
    boot reaches the substrates, so the status verb (slice 3) re-gathers and renders the same way."""
    state, refused = read_state()
    repo, token = repo_slug(), gh_token()
    gate, reason = protected_branch_signal(repo, token)
    finding_count, register, finding_fingerprint, low_severity_count, findings = open_findings(repo, token)
    # The render-only triage-pressure line (telemetry/README §"triage-pressure stream"): one plain-language
    # "backlog is growing" line once the COMPLETE open low-severity count crosses the governed threshold, else
    # None. Boot DISPLAYS it read-only (it never runs a triage pass — D-269); the count is the durable-Issue
    # count open_findings just read (authoritative + complete), so it can never render a false number — it is
    # SUPPRESSED (None) whenever the register read degraded (low_severity_count is None) or sits at/under the
    # threshold. Crossing promotes NOTHING (the meter never becomes an item), so it cannot feed what it measures.
    triage_pressure_line = None
    if low_severity_count is not None:
        try:
            # Read the threshold through the operator-override merge so a reviewed /engine-tune of it governs
            # live — the line already tells the operator "type /engine-tune", so that tune must actually apply.
            threshold = int(telemetry.load_thresholds(
                override=operator_overrides.slice_for("triage-threshold") or None).get("triage_pressure", 0))
            triage_pressure_line = telemetry.triage_pressure_line(low_severity_count, threshold)
        except Exception:  # noqa: BLE001 — a policy-read failure suppresses the meter, never breaks the pack
            triage_pressure_line = None
    # The render-only contract-rate nudge (the contract-threshold policy's soft-warn): one plain-language
    # "are decisions being over-recorded?" line once the operator's OWN engine decisions accepted in the last
    # 7 days cross the governed limit, else None. Boot DISPLAYS it read-only (it never writes a record); the
    # count reads only the deployment-owned per-instance decision folder, and the threshold reads through the
    # override merge so /engine-tune governs it. SUPPRESSED (None) whenever the folder can't be read or the
    # count sits at/under the limit — never a false number.
    contract_rate_line = None
    contract_rate = telemetry.derive_contract_rate(telemetry.utc_now())
    if contract_rate is not None:
        try:
            contract_threshold = telemetry.contract_rate_threshold(
                override=operator_overrides.slice_for("contract-threshold") or None)
            contract_rate_line = telemetry.contract_rate_line(contract_rate, contract_threshold)
        except Exception:  # noqa: BLE001 — a policy-read failure suppresses the meter, never breaks the pack
            contract_rate_line = None
    debt_count, debt_as_of = telemetry.read_state_debt(STATE_PATH)
    # The GitHub reader for attention's in-flight work-record read (open PRs). None without a repo/token ->
    # attention falls back to the local-git floor (the working branch). Construction does no I/O (telemetry.py).
    gh = telemetry.GitHubIssues(repo, token) if repo and token else None
    # Thread the live debt register boot ALREADY read (open_findings, above) into the ranking as the PER-ISSUE
    # rows, so the ranking grades each open finding on its own severity (making the policy's debt-blocking
    # threshold and busy-session flex actually govern) while the "Open problems" header still reads the SAME
    # number off the SAME read — `finding_count == len(findings)`, so they cannot disagree — and the SessionStart
    # path makes no second GitHub call. None (no repo/token, or a failed read) -> telemetry degrades and the
    # committed count stands in -> boot raises the loud 'couldn't reach' notice.
    # Boot's rung-1 knowledge slice (#37), read ONCE here and threaded into needs_attention — the SAME read also
    # carries `from_live`: True when the committed graph.json was absent and orientation ran on a LIVE rebuild
    # (rung 3, "loudly degraded"). That drives the rebuilt-map heads-up, NOT the att_degraded "couldn't reach"
    # notice — the map IS reachable, only the committed file is missing. read() fail-opens to None (never raises
    # into boot), so a read failure leaves map_rebuilt False and is covered instead by the "couldn't reach" path.
    source = boot_slice.read()
    map_rebuilt = bool(source and getattr(source, "from_live", False))
    # The same read distinguishes the committed map being ABSENT (map_rebuilt) from present-but-DAMAGED
    # (map_corrupt) — both ran orientation on a live rebuild, but the operator's repair reads differently, so
    # each earns its own honestly-named heads-up (eADR-0004 'name what is reduced'). Mutually exclusive.
    map_corrupt = bool(source and getattr(source, "from_corrupt", False))
    att_lines, att_degraded, neighborhood, shipped, recalled = needs_attention(
        state, gh=gh, live_findings=findings, source=source)
    try:
        # Provisioning's strand detector, RELAYED (boot computes no new state). A strand-check failure is
        # low-stakes (a stranded local checkout cannot reach the protected branch), so it degrades QUIETLY
        # to None — never a "couldn't check your folder" nag; the double-fault is the present-marker floor's.
        strand = checkout_health.detect_strand()
    except Exception:  # noqa: BLE001 — any detector failure degrades that one signal, never the pack
        strand = None
    try:
        # The behind-origin tail (#335), RELAYED from checkout_health's own detection (boot computes no new
        # state). This is the engine's one ONLINE boot signal that fetches: a best-effort, tightly bounded
        # `git fetch` then a clean-fast-forward check. Online-only by nature (a behind checkout cannot be seen
        # offline), so it degrades SILENTLY to None on no-network / no-remote / a non-default branch — never a
        # false "behind". boot OFFERS bringing it current; the assistant runs checkout_health.catch_up only on
        # the operator's consent (the strand model). Lossless by construction (`merge --ff-only`).
        behind_origin = checkout_health.detect_behind_origin()
    except Exception:  # noqa: BLE001 — any detector/network failure degrades this one signal, never the pack
        behind_origin = None
    try:
        # The off-main Stage-1 signal (#342/D-275), RELAYED from checkout_health's own detection (boot computes
        # no new state). The OFFLINE companion to the behind tail: the operator's top-level checkout PARKED on a
        # non-default branch, caught every boot on day one (the cheap-to-fix window, before it falls behind). The
        # gentlest folder-health signal — a gentle invitation, collapse-eligible (anti-habituation). Fires only
        # when the default branch is KNOWN with confidence, so a pre-persistence checkout raises no false nag;
        # degrades QUIETLY to None otherwise (an on-default / unknown-default checkout is the normal state). The
        # behind-the-main-line escalation is the separate ONLINE behind_origin tail above.
        off_main = checkout_health.detect_off_main()
    except Exception:  # noqa: BLE001 — any detector failure degrades this one signal, never the pack
        off_main = None
    try:
        # The absent-update-home signal (#367, D-281/D-282), RELAYED from checkout_health's own OFFLINE
        # detection (boot computes no new state). A repo generated before the home coordinate shipped has an
        # installed engine that cannot fetch its own updates; boot OFFERS recording the home. Low-stakes and
        # the normal state for any repo with a home recorded, so it degrades QUIETLY to None — never a nag.
        absent_home = checkout_health.detect_absent_home()
    except Exception:  # noqa: BLE001 — any detector failure degrades this one signal, never the pack
        absent_home = None
    try:
        # The self-review freshness signal, RELAYED from audit_digest's own detection (boot computes no new
        # state). Called arg-less so it reads the committed digest + today and owns STALENESS_DAYS/the re-arm
        # copy itself — boot never re-detects or re-literals the bound. Low-stakes (a missing digest is the
        # normal pre-arm state), so it degrades SILENTLY to None — never a "couldn't check the self-review" nag.
        audit_stale = audit_digest.staleness()
    except Exception:  # noqa: BLE001 — any failure degrades this one signal, never the pack
        audit_stale = None
    try:
        # The stranded-PR conflict detector (#136), RELAYED from pr_reconcile's own detection (boot computes no
        # new state). A pull request stuck on the engine's two derived index files cannot reach the protected
        # branch (GitHub blocks the merge), so it degrades QUIETLY to None on no-PR / no-GitHub / an unknown
        # (async-uncomputed) merge state — never a false "all clear". boot OFFERS the fix; the assistant runs it
        # on the operator's consent (the strand model). gh is None without a repo/token -> detect returns None.
        pr_conflict = pr_reconcile.detect_conflict(gh)
    except Exception:  # noqa: BLE001 — any detector failure degrades this one signal, never the pack
        pr_conflict = None
    try:
        # The memory auto-restore offer (Floor 3, slice 6b), RELAYED from memory's own LOCAL-ONLY detector (no
        # network; boot computes no new state). restore_vault is imported LAZILY here because restore_vault ->
        # backup_vault -> boot is a back-edge that is only safe lazily (pr_reconcile has no such edge). Degrades
        # QUIETLY to None — a fresh project with no backup, or one whose memory is present, is the normal state.
        from memory import restore_vault
        restore_offer = restore_vault.detect_restore_offer()
    except Exception:  # noqa: BLE001 — any detector/import failure degrades this one signal, never the pack
        restore_offer = None
    try:
        # The code-older-than-data restore offer (Slice 3, D-264 floor a), RELAYED from memory's own OFFLINE detector
        # (boot computes no new state). Same lazy import as the restore-offer above (the restore_vault -> backup_vault
        # -> boot back-edge). `gh` is passed so the detector can ALSO promote the durable tracked Issue when online;
        # offline it still returns the in-session offer. Degrades QUIETLY to None — no stamp (no recent data migration)
        # is the normal state, and a non-version-shaped running version never false-fires.
        from memory import restore_vault as _rv
        migration_revert = _rv.detect_migration_revert(github=gh)
    except Exception:  # noqa: BLE001 — any detector/import failure degrades this one signal, never the pack
        migration_revert = None
    try:
        # The memory-health signal (#396 U07b), RELAYED from memory's own LOCAL read (no network; boot computes
        # no new state). Reads the live ledger and reports how many lines are unreadable — a rotting store that
        # would otherwise lose recall line by line with no signal. Lazy import (memory off the cold-start path).
        # Degrades QUIETLY to None on any read fault, and to 0 on a clean/torn-only ledger — the normal state.
        from memory import ledger_health
        ledger_malformed = ledger_health.detect_ledger_malformed()
    except Exception:  # noqa: BLE001 — any detector/import failure degrades this one signal, never the pack
        ledger_malformed = None
    try:
        # The stalled-migration signal (#396 U26): a memory migration didn't finish and left an orphaned in-flight
        # marker, so automatic tidying (compaction) is paused until it clears. Read-only relay from memory's own
        # detector; the clear itself is compaction's self-heal. Quietly False on a clean/live state or any fault.
        from memory import ledger_health as _lh
        migration_stalled = _lh.detect_stalled_migration()
    except Exception:  # noqa: BLE001 — any detector/import failure degrades this one signal, never the pack
        migration_stalled = False
    try:
        # The memory-availability signal (#397 U09), RELAYED from memory's own LOCAL read: True iff the saved
        # ledger is present-but-unreadable, so recall genuinely can't answer (the availability floor — distinct
        # from the malformed-LINES rot below, which the file still opens, and from the FTS5 slower-mode the scent
        # renders). Read-only; degrades quietly to False on any fault. The dead-MCP-SERVER case is the model's own
        # live-helper check (MCP_AVAILABILITY_CHECK), not here — boot reads committed files only.
        from memory import ledger_health as _lh_off
        recall_offline = _lh_off.detect_recall_offline()
    except Exception:  # noqa: BLE001 — any detector/import failure degrades this one signal, never the pack
        recall_offline = False
    # The reversible-forgetting readout (#413 U09), RELAYED from memory's own read: what recall has set aside
    # (notes gone quiet, notes folded into summaries) that the operator has a handle on. None means "not read"
    # (an unreadable store — surfaced by recall_offline above, never as a false "nothing set aside"); a report
    # means "read". Read-only; boot owns the wording, memory owns the mechanism.
    set_aside = _set_aside_recall()
    # "Where we are" assembled LIVE from native GitHub sources, read-only (D-198): the online card is always
    # current and cannot silently rot. ALL-OR-NOTHING — any read failure (or no repo/token) leaves this None,
    # and render falls back to the committed offline cache, rendered stale-labelled. boot DISPLAYS; it never
    # writes the cache (that rides telemetry's GitHub pass). A failure here NEVER reads as a confident "none set".
    live_standing = None
    if repo and token:
        try:
            live_standing = standing_situation.derive_standing_situation(telemetry.GitHubIssues(repo, token))
        except Exception:  # noqa: BLE001 — a read failure degrades to the cached line, never breaks the pack
            live_standing = None
    return {
        "state": state, "refused": refused,
        "gate": gate, "reason": reason,
        "finding_count": finding_count, "register": register, "finding_fingerprint": finding_fingerprint,
        # How many open findings carry NO urgency rating — from the SAME read as the count above, so the two
        # can never disagree. None when the register could not be read (the card then says nothing about it
        # rather than guessing zero).
        "unrated_count": (None if findings is None
                          else sum(1 for f in findings if not f.get("severity"))),
        "low_severity_count": low_severity_count, "triage_pressure_line": triage_pressure_line,
        "contract_rate_line": contract_rate_line,
        "debt_count": debt_count, "debt_as_of": debt_as_of,
        "att_lines": att_lines, "att_degraded": att_degraded,
        # True iff orientation ran on a LIVE-rebuilt map because the committed graph.json is absent (a distinct
        # heads-up, NOT the att_degraded "couldn't reach": the map is reachable, the committed file is missing)
        "map_rebuilt": map_rebuilt,
        # True iff orientation ran on a LIVE-rebuilt map because the committed graph.json is present but DAMAGED
        # (a distinct heads-up from the absent case above — same live rebuild, different repair for the operator)
        "map_corrupt": map_corrupt,
        # the knowledge neighborhood of the work in hand (focused read, #37) -> the AI pack block, or None
        "neighborhood": neighborhood,
        # the memory half of recent decisions (#394 U01) -> the AI-facing recalled-decisions block
        "recalled": recalled,
        # The "recently shipped" digest, now the attention policy's budget_recent_decisions slice over the
        # ranked partition rather than a buried constant's fixed 5 (#394 U01).
        "shipped": shipped,
        "stance": modes.describe_stance(modes.current_stance(session_id)),
        "strand": strand,   # a stranded operator checkout (detached / missing engine files), or None
        # the behind-origin tail (#335; branch-agnostic for #342): the checkout — on its default branch OR
        # parked on a side branch — is missing merged work past the velocity bar, or None (also None offline —
        # the signal is online-only). The Stage-2 firm escalation of the off-main signal below.
        "behind_origin": behind_origin,
        # the off-main Stage-1 signal (#342): the top-level checkout is parked on a non-default branch (offline,
        # gentle, collapse-eligible), or None. behind_origin above is its online Stage-2 escalation.
        "off_main": off_main,
        # the absent-update-home signal (#367): the engine's manifest records no home to fetch updates from, or None
        "absent_home": absent_home,
        # a pull request stuck in a conflicting merge state on the two derived index files (#136), or None
        "pr_conflict": pr_conflict,
        # the memory auto-restore offer (#R2, slice 6b): local memory is empty + a backup is configured, or None
        "restore_offer": restore_offer,
        # the code-older-than-data offer (D-264 #303): the store is ahead of the engine after a reverted update, or None
        "migration_revert": migration_revert,
        # the memory-health count (#396 U07b): unreadable lines in the live ledger (>0 -> a rot heads-up), 0/None otherwise
        "ledger_malformed": ledger_malformed,
        # the stalled-migration signal (#396 U26): True iff a memory migration didn't finish (orphaned marker) and
        # tidying is paused until it clears; False on a clean/live state (a live migration is normal, not a stall)
        "migration_stalled": migration_stalled,
        # the memory-availability signal (#397 U09): True iff the saved ledger is present-but-unreadable so recall
        # can't answer (the "memory offline" floor); False on a healthy, empty, or unreadable-to-detect state
        "recall_offline": recall_offline,
        # the reversible-forgetting readout (#413 U09): what recall has set aside (demoted / summarised) with the
        # full count + id set, or None when the store was not read (never a false "nothing set aside")
        "set_aside": set_aside,
        # the self-review freshness finding (soft = hasn't-run-yet / has-gone-stale; note = current), or None
        "audit_stale": audit_stale,
        # the live-derived {milestone, phase}, or None when GitHub was unreachable (-> render the cached copy)
        "live_standing": live_standing,
    }


# #416 U10-F1: the degraded inputs a Claude Desktop restart actually reconnects — the MCP/GitHub background
# reads (the knowledge map service, the GitHub-backed open-problems read). NOT git (a subprocess, not a
# service), state (a committed file), or the ranker (in-process logic): a restart does not fix those, so the
# self-serve restart line is scoped to this set (boot/README "Degradation is loud and consented" —
# "usually a Claude Desktop restart away from full capability").
_RESTART_FIXABLE = {"telemetry", "knowledge"}


def render_dashboard(s: dict) -> str:
    """The operator-toned `Project status` dashboard, rendered from gathered signals (gather_signals) as
    DATA — PURE: no I/O, computes no new state. Governance alarms pin warm at the top, then a stranded-
    checkout heads-up (open-findings tier — provisioning's detector, relayed read-only, ranked BELOW the
    governance alarms because a stranded local checkout cannot reach the protected branch), then the status
    facts, the stance, the consolidated degraded notice, the ranked work, and the recently-shipped digest.
    NO AI-facing markers — this is the operator's own view, which the status verb (slice 3) renders directly
    (the 'two renderings of the same data'). The card title is always the first line."""
    pinned: list[str] = []        # governance-critical alarms, loudest first
    degraded: list[str] = []      # the consolidated "what I couldn't refresh / verify" notice

    if s["gate"] == "off":
        # boot OFFERS the fix here and stays READ-ONLY; the assistant runs the already-built, idempotent
        # bootstrap.ControlPlane.apply(branch=PROTECTED_BRANCH) on the operator's consent — the shared
        # repair-offer contract (boot-session-start.md). boot never imports bootstrap (bootstrap imports
        # boot -> a cycle) and never applies the fix itself: read-only of canonical state (D-269).
        pinned.append(
            f"⛔ **Your safety gate is off** — `{PROTECTED_BRANCH}` isn't protected, so unreviewed work "
            f"could reach your main branch ({s['reason']}). Say **turn my safety gate back on** and I'll "
            f"re-enable branch protection for you — you'll approve a one-time GitHub permission, and I never "
            f"ask you to type commands yourself.")
    elif s["gate"] == "unknown":
        degraded.append(
            f"I couldn't verify your safety gate from here (no GitHub access), so **don't assume "
            f"`{PROTECTED_BRANCH}` is protected** — confirm it before merging anything important.")

    if s["finding_count"]:
        pinned.append(
            f"⚠️ **{s['finding_count']} open engine finding(s)** about the engine's own health need "
            f"review: {s['register']}")
    # When the live register could NOT be read (finding_count is None), the consolidated degraded notice below
    # names it ("I couldn't reach your open-problems list from GitHub ...") — that notice is driven by attention's
    # degraded set (telemetry, the same live register), so there is no separate "couldn't check findings" line
    # to duplicate it. The header above falls back to the loud-if-stale shadow count in that case.

    # A stranded operator checkout — surfaced read-only, pinned AFTER the governance alarms (open-findings
    # tier; a stranded local checkout cannot reach the protected branch). boot OFFERS the fix here; the
    # assistant runs the un-stranding fix (checkout_health.unstrand) only on the operator's consent — boot
    # itself stays read-only. The fix is lossless-or-rescue-then-update (checkout_health / boot-session-start).
    if s["strand"]:
        pinned.append(
            "⚠️ **Your project folder has drifted into a broken state** — I work in a separate copy, so "
            "this doesn't affect what we build, but your project folder needs attention. Just say the word "
            "and I'll get it healthy again — I'll save anything at risk first (including any work that's "
            "drifted off your branch) to a safe point, so nothing is lost.")

    # The widened "fifth" folder-health surfacing (#342/D-275): off-main Stage-1 + behind-the-main-line Stage-2,
    # pinned read-only at the strand tier (below the governance alarms — an off-main/behind checkout cannot reach
    # protected `main`). COUNT-FREE per the design's "never a count" leaf law, NO git verbs, ONE consent handle
    # ("bring it up to date") across both stages. boot OFFERS only; the assistant runs the correction on consent
    # (catch_up on the default, return_to_default off it) — both lossless by construction. Precedence: the FIRM
    # Stage-2 (missing merged work) supersedes the GENTLE Stage-1 (merely parked) when both are live.
    behind = s.get("behind_origin")
    off_main = s.get("off_main")
    if behind and behind.get("on_default"):
        # Stage-2 on the DEFAULT branch (#335): behind your own merged main line — the original consequence copy.
        pinned.append(
            "📦 **Your project folder has fallen behind your recent work** — merged updates have landed since "
            f"you last caught up (most recently on {behind['latest']}), and your folder doesn't "
            "have them yet. I work in a separate copy, so nothing is broken — when you're ready, say **bring "
            "it up to date** and I'll bring your folder current safely; or, if you have unsaved work in the "
            "way, I'll tell you and leave everything untouched. Either way, nothing you already have will be lost.")
    elif behind:
        # Stage-2 on a SIDE line of work: the firm escalation. Two tones from the advisory (errs gentle): if the
        # side line may carry unfinished work, promise to keep it; if it's only an older view, say nothing's lost.
        # When it escalated from a gentle off-main park already shown, name that lineage (product-S3).
        lead = ("📦 **The side line of work I flagged earlier is now missing finished work from your main "
                "project**" if (off_main and off_main.get("worsened"))
                else "📦 **Your project folder is pointed at a side line of work that's missing finished work "
                     "from your main project**")
        if behind.get("advisory") == "merged":
            tone = "Nothing here is unsaved or lost — your folder is just showing an older view."
        else:
            tone = ("There may be unfinished work saved on that side line that isn't in your main project yet, "
                    "so I'll keep it exactly where it is — nothing deleted.")
        pinned.append(
            f"{lead} — your main project moved on most recently on {behind['latest']}. {tone} When you're ready, "
            "say **bring it up to date** and I'll point your folder back at your main project and bring it "
            "current; if anything's in the way I'll tell you and change nothing.")
    elif off_main:
        # Stage-1 (gentle, OFFLINE): merely parked on a side line, not yet behind — a gentle INVITATION, not a
        # defect report (the top-level checkout on a side line is anomalous because sessions work in separate
        # copies — the actor-model premise). Collapse-eligible: TERSE when unchanged since last shown in full
        # (the `collapsed` flag is set hook-side; the pure status-verb path leaves it absent -> full).
        if off_main.get("collapsed"):
            pinned.append(
                "🧭 Your project folder is still pointed at a side line of work rather than your main project "
                "(unchanged since last session) — say **bring it up to date** whenever you'd like me to point "
                "it back; your work on that side line stays exactly where it is.")
        else:
            line = ("🧭 **Your project folder is pointed at a side line of work rather than your main project** "
                    "— nothing's wrong and nothing's at risk; your work on that side line stays exactly where it "
                    "is. Whenever you like, say **bring it up to date** and I'll point your folder back at your "
                    "main project.")
            if off_main.get("first_sighting"):
                # The disclosure gap (constraint 6): spotting this is a newer check, so a folder reported healthy
                # for a while isn't silently re-cast as freshly broken. Phrased to NOT assert how long it's been
                # parked (offline we cannot tell) — only that the CHECK is new, so it may be a long-standing state.
                line += (" (Spotting a folder parked off its main line is a newer check — earlier sessions "
                         "couldn't, so you may be seeing a long-standing state for the first time, not something "
                         "that just broke.)")
            pinned.append(line)

    # The absent-update-home OFFER (#367, D-281/D-282), surfaced read-only at the strand/offer tier — the engine's
    # manifest records no home to fetch updates from (a repo generated before that coordinate shipped), so the
    # update path can't run and refuses rather than guess. NOT a governance alarm (it cannot let anything reach
    # protected `main`), so it pins below them. boot OFFERS recording the home; the assistant records it on the
    # operator's consent (the strand model). Includes the newer-check disclosure (constraint 6) so a long-standing
    # setup isn't recast as freshly broken.
    if s.get("absent_home"):
        pinned.append(
            "🏠 **I don't have your engine's update home recorded, so I can't check for or fetch engine updates.** "
            "Nothing is wrong with your project and nothing is at risk — updates just can't run until the home is "
            "recorded. Tell me the repository your engine updates from (for example your-org/your-engine) and I'll "
            "record it, then updates will work. (Recording where the engine updates from is a newer part of the "
            "engine, so you may "
            "be seeing this for a long-standing setup for the first time, not something that just broke.)")

    # A pull request stranded on the two derived index files (#136), surfaced read-only at the strand tier
    # (below the governance alarms — a conflicting PR cannot reach protected `main`, so it is NOT a governance
    # alarm). boot OFFERS the one-step fix; the assistant runs pr_reconcile.reconcile only on the operator's
    # consent (the strand model; boot-session-start.md). Leads with "no work is lost" so it reconciles with
    # the integrate-time "a collision is never your problem" framing the operator already met.
    if s["pr_conflict"]:
        pinned.append(
            "⚠️ **One of your pull requests can't be merged yet** — two pieces of work landed at once and "
            "clashed. **No work is lost and nothing is broken.** Most often this is just a clash on the "
            "engine's internal index files, which I can clear in one step while keeping both pieces of work. "
            "Say **reconcile it** and I'll check: I'll either clear it for you, or — if the clash is in real "
            "content — tell you plainly that it needs your decision.")

    # The memory auto-restore OFFER (Floor 3, slice 6b), surfaced read-only at the strand/pr_conflict tier — a
    # recovery OPPORTUNITY, not a governance alarm, so it pins below them. boot OFFERS; the assistant runs the
    # restore on the operator's consent (the strand model). Memory owns the detector; boot owns this wording.
    if s["restore_offer"]:
        pinned.append(
            "↩️ **Your saved memory looks empty, and this project has a backup.** Say **restore my memory** and "
            "I'll try to bring it back from the backup. Nothing on this computer changes until you say so.")

    # The code-older-than-data restore OFFER (D-264 floor a, #303), surfaced read-only at the recovery tier. Memory's
    # offline detector found the saved memory was reshaped by an engine update that is no longer in place, so the store
    # is ahead of the code. Floor (a): exactly ONE action, by plain handle ("the copy saved before that update"), never
    # a tag/ref — the snapshot-vs-latest choice is the engine's. Worded to cover BOTH an operator-undone update and a
    # half-applied one that never landed (leads with the state, not "you undid"). boot OFFERS; the assistant runs
    # memory.restore_pre_migration(tag=…) on consent (the tag rides the signal, never the operator's eyes).
    if s.get("migration_revert"):
        pinned.append(
            "↩️ **Your saved memory was changed by an engine update that isn't in place** — so right now your "
            "memory and the engine don't match. I can put your memory back to **the copy saved before that update**, so "
            "they line up again. Say **restore my memory from before the update** and I'll bring it back — nothing on "
            "this computer changes until you say so.")

    out: list[str] = [f"## {PRESENT_MARKER}"]
    out.extend(f"> {line}" for line in pinned)
    if pinned:
        out.append("")

    if s["refused"]:
        out.append(
            "**I couldn't read where the project stands**, so I'm treating project status as unknown. "
            "Don't trust a status summary until the engine re-grounds.")
    else:
        # "Where we are" (the active work) and "Milestone" (the larger plan marker) are two self-explanatory
        # lines, from ONE source — live-or-cached, never both (boot/README rendering law). When the live GitHub
        # derive succeeded, render it (always current); otherwise fall back to the committed offline cache,
        # named with WHEN it was cached and that it may be stale (the debt-count staleness voice). An absent
        # milestone is an honest normal state on its own line ("No milestone is open"), never an error.
        live = s["live_standing"]
        source = live if live is not None else ((s["state"] or {}).get("standing_situation") or {})
        phase = source.get("phase") or "nothing in progress yet"
        milestone = source.get("milestone") or "No milestone is open"
        out.append(f"**Where we are:** {phase}")
        out.append(f"**Milestone:** {milestone}")
        if live is None:
            when = source.get("as_of") or "an earlier session"
            out.append(f"_(as of {when} — I couldn't refresh this from GitHub, so it may be out of date; "
                       f"re-ground before you rely on it.)_")
        # The open-problem count: the live register first, else the committed offline shadow rendered
        # loud-if-stale (degrade-loud) so a number can never be mistaken for freshly refreshed.
        if s["finding_count"] is not None:
            out.append(f"**Open problems:** {s['finding_count']}")
            # Say when open problems carry no urgency rating. Without this the card reads "18 open" beside
            # "Nothing is blocking right now" and the two together imply the engine weighed them and found
            # none urgent. It did not weigh them at all: nothing has ever rated them, so the debt-blocking
            # rule has nothing to compare and they neither block nor count toward the waiting-work meter
            # (which counts only the rated-as-low). "Not rated" and "rated, not urgent" look identical on
            # the card and mean opposite things, and only this line tells them apart.
            unrated = s.get("unrated_count")
            if unrated:
                which = ("None of these carries an urgency rating" if unrated == s["finding_count"]
                         else f"{unrated} of these carry no urgency rating")
                out.append(f"_{which}, so nothing weighs them against the bar that decides what stops you. "
                           f"That is not a judgement that they are minor — it means no one has rated them._")
        elif s["debt_count"]:
            out.append(f"**Open problems:** {telemetry.degraded_readout(s['debt_count'], s['debt_as_of'])}")
        else:
            out.append("**Open problems:** none recorded yet.")
        # The render-only triage-pressure line, only when the live low-severity backlog crosses the threshold
        # (suppressed on a degraded read or a below-threshold count — telemetry owns that decision).
        if s.get("triage_pressure_line"):
            out.append(s["triage_pressure_line"])
        # The render-only contract-rate nudge, only when the operator's own engine decisions accepted in the
        # last 7 days cross the governed limit (suppressed on a degraded read or below-limit — telemetry owns
        # that decision). A separate line from the backlog meter: a different signal about a different thing.
        if s.get("contract_rate_line"):
            out.append(s["contract_rate_line"])

    out.append(f"**Stance:** {s['stance']}")

    if s["att_degraded"]:
        # Name the actual input(s) the ranking couldn't reach this session, in plain words — so this notice
        # fires ONLY on a real read failure (an outage / no GitHub access), never as standing scaffolding, and
        # tells the operator WHAT was unreachable rather than an internal name. With the live debt register now
        # read each session, a healthy boot leaves this empty (the old "expected on a new engine" framing is
        # gone — it would be false here). EVERY value att_degraded can carry must map to a plain phrase: the
        # four substrate names AND "attention" (needs_attention reports ["attention"] when the ranker itself
        # failed), so no internal noun ever reaches operator copy (the §12 leak guard).
        _UNREACHABLE = {"telemetry": "your open-problems list from GitHub",
                        # `git` answers for in-flight work AND what shipped recently, and degrades as a
                        # whole, so this names the substrate rather than one of its halves. It does NOT name
                        # GitHub: a GitHub outage falls back to the local floor and leaves git available
                        # (work_record: "local git stands in"), so the only thing that reaches this line is
                        # git itself being unreadable HERE — sending the reader to check their network or
                        # token would send them away from the folder that is actually broken. Comma-free on
                        # purpose: _and_list joins these into one sentence, so an inner comma would read as
                        # another missing thing.
                        "git": "the record of your work in this project folder",
                        "knowledge": "your project map",
                        "state": "your saved project state",
                        "attention": "your work-priority ranking"}
        missing = _and_list([_UNREACHABLE.get(name, name) for name in s["att_degraded"]])
        degraded.append(
            f"I couldn't reach {missing} this session, so the priority order below may be incomplete — "
            f"re-ground before you rely on it.")

    if s.get("map_rebuilt"):
        # The committed project map (graph.json) is absent, so orientation ran on a LIVE rebuild (rung 3). The
        # map IS reachable — this is deliberately NOT the "couldn't reach" degrade above: it is a distinct
        # inform + consequence line in peer voice (never an alarm), naming the missing file and the one fix.
        # The operator chose this rare state earns its own at-boot heads-up rather than only the merge-time
        # coverage check. Cite the one canonical regenerate-and-commit command (REGEN_CMD) the way every sibling
        # message does, so the fix is actionable for a non-engineer. .get() so a fixed-signals test fixture
        # without the key never KeyErrors.
        degraded.append(
            "I'm running on a rebuilt project map — your committed map file is missing. Orientation still "
            f"works, but regenerate it with `{knowledge_gen.REGEN_CMD}` and commit the result to restore "
            "your saved map.")

    if s.get("map_corrupt"):
        # The committed project map (graph.json) is PRESENT but could not be read (damaged — e.g. a regen
        # killed mid-write, or merge markers). Orientation ran on a LIVE rebuild (rung 3), same as the absent
        # case above, but the repair differs: regenerating REPLACES the damaged file. A distinct inform +
        # consequence line in peer voice — naming the damage (not a "missing" file, which would point at the
        # wrong fix) and the one command. .get() so a fixed-signals test fixture without the key never KeyErrors.
        degraded.append(
            "I'm running on a rebuilt project map — your committed map file is present but damaged, so I "
            f"couldn't read it. Orientation still works, but regenerate it with `{knowledge_gen.REGEN_CMD}` "
            "and commit the result to replace the damaged file.")

    if s.get("recall_offline"):
        # #397 U09: the spec's "running degraded (memory offline)" notice — the saved-memory store is present but
        # couldn't be OPENED at all, so recall can't work this session. Distinct from and mutually exclusive with
        # the "N unreadable lines" rot below (there the file DID open and was read past line-by-line; an unopenable
        # file yields no line count — detect_ledger_malformed returns None). Boot RELAYS memory's own read result
        # read-only; it never repairs. Peer voice: name what's degraded, that the saved store isn't gone, and the
        # ONE self-serve action (restore from backup) — NOT a Claude restart, which cannot fix an unreadable local
        # file (so memory is deliberately kept out of the restart-fixable hedge below). No backstage vocab. .get()
        # so a fixed-signals test fixture without the key never KeyErrors.
        degraded.append(
            "I couldn't open your saved memory, so my recall of past decisions and notes is unavailable this "
            "session — I'm still oriented by the rest of your saved project files. Your saved memory isn't lost. "
            "If you set up a backup, ask me to restore it from there; if not, tell me and I'll help you get your "
            "recall working again.")

    malformed = s.get("ledger_malformed")
    if malformed:
        # #396 U07b: one or more unreadable lines in the saved-memory ledger — a genuine rot signal. Fires ONLY
        # on a positive count (a torn trailing line is the normal, self-healing post-crash state and is NOT
        # surfaced). Peer voice with reassurance + a remedy: a non-engineer can't hand-fix a gitignored store, so
        # name that the rest of recall is intact and point at the backup, not a raw alarm. .get() so a
        # fixed-signals test fixture without the key never KeyErrors.
        count = f"{malformed} unreadable line" + ("" if malformed == 1 else "s")
        degraded.append(
            f"Your saved memory has {count}, which I read past safely — everything I could read is intact. "
            "This clears on its own as your memory is tidied; if you keep seeing it, ask me to restore your "
            "memory from your backup.")

    if s.get("migration_stalled"):
        # #396 U26: a data migration didn't finish and left an orphaned marker (its process died). This fires ONLY
        # for the orphaned case, which does NOT block anything — so it says "didn't finish", not "paused" (the
        # marker no longer holds tidying off; the next tidy clears it). LEAD with the reassurance (the failure
        # direction here is "nothing lost" — README §279: content is untouched), mirroring the memory-health
        # sibling above. Plain language — never "migration"/"compaction"/"marker". Recovery is automatic (the next
        # memory tidy reaps the leftover), and a concrete recourse is named. .get() so a fixed-signals test
        # fixture without the key never KeyErrors.
        degraded.append(
            "A memory update didn't finish cleanly — nothing was lost, and everything saved is still there and "
            "readable. I clean up the leftover automatically the next time I tidy your memory; if you keep "
            "seeing this across sessions, tell me and I'll clear it right away.")

    # #416 U10-F1: name the single self-serve fix the spec's loud notice owes ("usually a Claude Desktop
    # restart away from full capability", boot/README §"Degradation is loud and consented"). SCOPED — fires
    # only when a restart-fixable substrate outage is present (a dropped MCP/GitHub connection, or the
    # gate-unknown no-GitHub-access case), never for the regenerate-a-file or self-healing lines above (a
    # restart does not fix those). Hedged ("if any of that … the usual cause") per the spec's "usually", so it
    # never over-promises on a genuine remote outage or expired auth.
    restart_fixable = (s.get("gate") == "unknown") or bool(set(s.get("att_degraded") or []) & _RESTART_FIXABLE)
    if degraded and restart_fixable:
        degraded.append(
            "If any of that is a dropped connection — the usual cause — quitting and reopening Claude Desktop "
            "reconnects it, and I'll re-check.")

    if degraded:
        out.append("")
        out.extend(f"_{line}_" for line in degraded)

    out.append("")
    out.append("### Needs your attention")
    attention = list(s["att_lines"])
    # The self-review freshness advisory (audit-library 3c), relayed read-only from audit_digest's own
    # detection. A SOFT, never-blocking nudge naming the one re-arming action — it sits here in the attention
    # body (surfaced by the pack's step-3 instruction so the assistant raises it when it matters), and is
    # DELIBERATELY never pinned / present-marker / must_push: a never-armed repo still reads "all clear" and
    # this never becomes a forced every-session alarm. A `note` (current) digest adds nothing — its silence is
    # the healthy signal. The fresh-digest recency line is a deferred build-spec leaf (lands with a real digest).
    stale = s["audit_stale"]
    if stale and stale["severity"] == "soft":
        attention.append(stale["message"])
    out.extend(f"- {line}" for line in attention) if attention else out.append(
        "- Nothing is blocking right now.")

    out.append("")
    out.append("### Recently shipped")
    # The digest owns its own absence copy (_shipped_lines): only that read knows whether there are no recent
    # merges or whether it simply is not showing them, and this render must not guess between the two.
    out.extend(f"- {line}" for line in s["shipped"])

    # The reversible-forgetting readout (#413): what memory has set aside from recall, with a handle per note.
    # render_set_aside returns [] when there is nothing set aside or the store was not read — no block then.
    set_aside_block = render_set_aside(s.get("set_aside"))
    if set_aside_block:
        out.append("")
        out.extend(set_aside_block)

    # The artifact warrant (D-261), proportionately LIGHT: this dashboard — and the project map it
    # draws on — is an automated readout derived from the engine's own checks, so it states its bound
    # right where it is read. The graph behind "your project map" is a byte-fingerprinted generated file
    # whose bound rides this startup view (an authored field in it would break exact-match regeneration),
    # so the line lives here, not in the raw graph. Light because the limit is near self-evident and the
    # real gate (the merge review) is named elsewhere in this briefing.
    out.append("")
    out.append("_This view is an automated readout: a clear status shows the checks the engine can run "
               "came back clean — not that everything is correct. Your review at merge is the real gate._")
    out.append("_About those checks: only the one that runs when a change is proposed for merge can stop a "
               "risky one — anything that ran while I worked is early advice. Each check is itself proven "
               "against a deliberately broken example it must catch, so a passing check can't be one that "
               "quietly did nothing — but that proves the check works, not that the change is right. And a "
               "check that could not run leaves that area unverified._")

    return "\n".join(out)


def present_marker_line(s: dict) -> str:
    """The short titled status block the AI is told to render FIRST — `Project status: all clear`, or a
    `⚠ ...` line when something governance-critical or a grounding failure fired. A fixed relay over
    already-detected signals (boot computes no new state); a couldn't-verify gate NEVER reads as a green
    all-clear (degrade-loud)."""
    if s["gate"] == "off":
        return "⚠ Your safety gate is off"   # same noun as the dashboard + the unknown-gate marker below
    if s["gate"] == "unknown":
        return f"⚠ {PRESENT_MARKER}: couldn't verify the safety gate"
    if s["refused"]:
        return f"⚠ {PRESENT_MARKER}: couldn't read where the project stands"
    if s["finding_count"]:
        return f"⚠ {PRESENT_MARKER}: {s['finding_count']} open engine finding(s) to review"
    if s["strand"]:   # ranked after the governance alarms + findings; a governance alarm still wins the marker
        return f"⚠ {PRESENT_MARKER}: your project folder needs attention"
    if s.get("behind_origin") and s["behind_origin"].get("on_default"):
        # Stage-2 on the DEFAULT branch (#335): the folder IS on its main line, only behind — the headline must
        # not say it's "off" the main line (that would contradict the dashboard's "fallen behind" line).
        return (f"⚠ {PRESENT_MARKER}: your project folder has fallen behind your recent work — say 'bring it "
                "up to date' and I'll bring it current")
    if s.get("behind_origin") or s.get("off_main"):   # off the main line (parked on a side line, maybe behind too)
        # ONE tone-neutral headline for the off-main stages; the two tones and the felt consequence live in the
        # dashboard's pinned line, not the marker (product-S1/S2). Accurate here — the checkout is genuinely off it.
        return (f"⚠ {PRESENT_MARKER}: your project folder isn't on your main line of work — say 'bring it up "
                "to date' and I'll sort it out safely")
    if s["pr_conflict"]:   # the always-visible surface so a stuck PR cannot rot unnoticed (not a must_push)
        return f"⚠ {PRESENT_MARKER}: a pull request is stuck — say 'reconcile it' and I'll look into clearing it"
    if s.get("migration_revert"):   # a recovery OFFER (not a ⚠ alarm): the store is ahead of the code after a revert
        return (f"{PRESENT_MARKER}: your saved memory is ahead of the engine after an update was undone — say "
                "'restore my memory from before the update' and I'll bring back the copy from before it")
    if s["restore_offer"]:   # a recovery OFFER (not a ⚠ alarm); ranked last, below every governance/strand signal
        return (f"{PRESENT_MARKER}: your saved memory looks empty — say 'restore my memory' and I'll try to bring "
                "back your backup")
    if s.get("absent_home"):   # an OFFER (not a ⚠ alarm): no update home recorded, so engine updates can't run
        return (f"{PRESENT_MARKER}: I can't fetch engine updates yet — no update home is recorded; tell me the "
                "repository your engine updates from and I'll record it")
    return f"{PRESENT_MARKER}: all clear"


def _pushed_alarms(s: dict) -> list:
    """The pushed governance set as STRUCTURED alarms — the single source for both must_push (the full
    lines) and the D-269 collapse decision. Each alarm carries:
      key         a stable identity (the ledger key);
      value       the STRUCTURED condition the ledger compares (never the prose) — JSON-able;
      collapsible whether it is in the D-269 collapse allowlist (a standing governance alarm). The
                  degrade-loud tells — a couldn't-verify gate and a refused cursor — are NOT collapsible:
                  they always render full so a grounding/verification failure never softens to a reminder;
      full        the neutral full INFORM line (first appearance, an improved/changed condition, or any
                  fail-toward-full fallback);
      terse       (collapsible only) the one-line reminder when the condition is UNCHANGED since last shown
                  in full — still names the consequence and still carries the offer to fix;
      worse       (collapsible only) the full line when the condition has WORSENED (lexically distinct).
    A fixed relay over detected signals; routine status carries no marker (it is pulled via the status verb)."""
    alarms: list = []
    if s["gate"] == "off":
        # full + terse BOTH carry the fix offer (spec: the terse collapse "still carries the offer to fix
        # it"). The offer is a plain-language handle — the assistant runs bootstrap.ControlPlane.apply on
        # consent (boot-session-start.md); it names the one-time GitHub permission, never an over-promised
        # silent flip. terse keeps a COMPACT handle so the collapse still buys brevity.
        full = (f"{RELAY_MARKER} their safety gate is off — `{PROTECTED_BRANCH}` isn't protected, so "
                f"unreviewed work could reach the main branch ({s['reason']}); tell them they can say "
                f"'turn my safety gate back on' and the engine will re-enable branch protection for them "
                f"(they approve a one-time GitHub permission — never a typed command).")
        terse = (f"{RELAY_MARKER} their safety gate is still off (unchanged since last session) — "
                 f"unreviewed work could still reach `{PROTECTED_BRANCH}`; the fix still stands: they can "
                 f"say 'turn my safety gate back on' and the engine re-enables it.")
        alarms.append({"key": "gate", "value": ["off", s["reason"]], "collapsible": True,
                       "full": full, "terse": terse, "worse": full})
    elif s["gate"] == "unknown":
        alarms.append({"key": "gate", "value": ["unknown", None], "collapsible": False, "full": (
            f"{RELAY_MARKER} the safety gate couldn't be verified (no GitHub access), so they shouldn't "
            f"assume `{PROTECTED_BRANCH}` is protected — confirm before merging anything important.")})
    if s["refused"]:
        alarms.append({"key": "refused", "value": True, "collapsible": False, "full": (
            f"{RELAY_MARKER} the engine couldn't read where the project stands, so project status is "
            f"unknown until it re-grounds.")})
    if s["finding_count"]:
        full = (f"{RELAY_MARKER} there are {s['finding_count']} open engine finding(s) about the engine's "
                f"own health to review: {s['register']}")
        terse = (f"{RELAY_MARKER} there are still {s['finding_count']} open engine finding(s) about the "
                 f"engine's own health to review (unchanged since last session): {s['register']}")
        worse = (f"{RELAY_MARKER} there are now {s['finding_count']} open engine finding(s) about the "
                 f"engine's own health to review — this has grown since last session: {s['register']}")
        # The ledger fingerprint is the STRUCTURED-CONDITION identity SET (finding_fingerprint), not the bare
        # count — so a close+open at equal count is seen as changed and relays full, never a false "unchanged"
        # (D-269 / R19). The display copy still reads the count. `.get` keeps synthetic test dicts fail-soft;
        # gather_signals always populates the key (and tracks count, so a real count>=1 carries a real list).
        alarms.append({"key": "findings", "value": s.get("finding_fingerprint"), "collapsible": True,
                       "full": full, "terse": terse, "worse": worse})
    return alarms


def must_push(s: dict) -> list:
    """The INFORM-marked items the AI MUST relay to the operator in plain words — the FULL (uncollapsed)
    governance-critical alarms and the grounding-failure tell (D-187 must-push set). This is the fresh
    render (the `pack` debug CLI and a fresh, ledger-less context); the SessionStart hook path applies the
    D-269 collapse via _relay_lines instead. A fixed relay over detected signals."""
    return [a["full"] for a in _pushed_alarms(s)]


def _off_main_value(s: dict):
    """The off-main ledger value — its STABLE structured identity for the D-269 collapse (never the prose):
    [the side line it's parked on, whether it has ALSO fallen behind the main line]. A repeat with the same
    value collapses to a terse reminder; the gentle->behind transition (False->True on the second element) is
    the worsening _worse detects, which drives the firm Stage-2 line's lineage. None when not off-main."""
    om = s.get("off_main")
    if not om:
        return None
    return [om.get("branch"), bool(s.get("behind_origin"))]


def _set_aside_value(s: dict):
    """The set-aside readout's STABLE structured identity for the D-269 collapse (never the prose): the sorted
    id set of the FULL set-aside population (never the bounded sample the render shows — a note leaving below
    the display cut must still relay full). The identity SET, never the bare count: one note coming back while
    another goes aside leaves the count equal but the situation changed, so a count would wrongly collapse it
    (the same trap the findings fingerprint avoids). None when nothing is set aside (a report that was read but
    is empty) or the store was not read — so a now-tidy store DROPS from the ledger and never wrongly collapses
    a later recurrence. The list is bounded by how many notes are set aside, which compaction bounds, so it
    needs no cap."""
    sa = s.get("set_aside")
    if not sa:
        return None
    identity = sa.get("identity") or []
    return sorted(identity) if identity else None


def _worse(key: str, prior, current) -> bool:
    """Whether a changed collapse-eligible condition got WORSE (so it relays full with the 'this got worse'
    wording, never a quiet reminder). Ordered only where 'worse' is meaningful: the open-findings SET
    growing (more open problems); an off-main park escalating to behind-the-main-line. A gate going on->off
    is an alarm that was ABSENT last session (no prior entry), so it is a first-appearance full relay, not a
    'worse'."""
    if key == "findings":
        # The value is now the identity SET (a list); worse = more open problems = the set grew. The list
        # guards are load-bearing: an OLD gitignored ledger holding the pre-upgrade INT count must NOT reach
        # len(int) here (this runs OUTSIDE decide's try/except) — an int prior fails the guard -> neutral
        # full relay (fail-toward-full), never a crash that would suppress the whole briefing.
        return isinstance(prior, list) and isinstance(current, list) and len(current) > len(prior)
    if key == "off_main":
        # the off-main Stage-1 park escalating to the behind Stage-2 (missing merged work): same side line,
        # not-behind -> behind. The value is [side-line, behind?]; worsening is False -> True on the flag. The
        # length guard contains a corrupted/short ledger value to this one signal (it is read OUTSIDE decide's
        # try/except, so an IndexError here would suppress the whole briefing, not just degrade this line).
        return (isinstance(prior, list) and len(prior) >= 2 and isinstance(current, list) and len(current) >= 2
                and prior[:1] == current[:1] and not prior[1] and bool(current[1]))
    return False


def _relay_lines(s: dict) -> list:
    """The hook-side relay set with the D-269 collapse applied (the deterministic decision lives here, in
    the hook path — never the model): a collapse-eligible alarm whose structured condition is unchanged
    since last shown in full renders TERSE; a new/changed one renders full; a worsened one renders the
    'got worse' full line; the degrade-loud tells always render full. Fail-toward-full: if the ledger could
    not be read (decide ok=False), every line is the neutral full form, never a misleading 'still'/'worse'."""
    alarms = _pushed_alarms(s)
    eligible = [{"key": a["key"], "value": a["value"]} for a in alarms if a["collapsible"]]
    # The gentle off-main signal rides this ONE decide() call (blocking B2): it is NOT a pushed governance alarm
    # (it has no relay line here — it renders only in the dashboard, below governance), but its collapse must use
    # the same ledger pass. A SECOND decide() call would clobber gate/findings (decide writes only the keys it is
    # passed), so off-main joins the single eligible set and its outcome is threaded onto `s` for render_dashboard.
    off_main_value = _off_main_value(s)
    if off_main_value is not None:
        eligible.append({"key": "off_main", "value": off_main_value})
    # The reversible-forgetting readout rides this SAME decide() call (#413), exactly like off_main: it is not a
    # pushed governance alarm (it has no relay line here — it renders only in the dashboard), but its collapse
    # must use the same ledger pass. A second decide() would clobber the keys this one writes.
    set_aside_value = _set_aside_value(s)
    if set_aside_value is not None:
        eligible.append({"key": "set_aside", "value": set_aside_value})
    # Always call decide — even with an empty eligible set — so a now-resolved standing alarm is DROPPED
    # from the ledger (verified-fixed), never left to wrongly collapse a later recurrence.
    decision = boot_alarm_ledger.decide(eligible)
    ok = decision.get("ok", False)
    results = decision.get("results", {})
    # Stamp the off-main collapse outcome onto `s` for the (pure) dashboard renderer — HOOK-SIDE ONLY, so the
    # status verb (which never calls _relay_lines) leaves these absent and renders the off-main line FULL
    # (fail-toward-full, arch-S1/S4). `worsened` drives the firm Stage-2 lineage; `first_sighting` the disclosure
    # gap (gated on ok, so a ledger-read failure never falsely claims a first sighting).
    if off_main_value is not None:
        r = results.get("off_main", {"outcome": "full", "prior": None})
        prior = r.get("prior")
        s["off_main"] = {**s["off_main"],
                         "collapsed": r.get("outcome") == "collapse",
                         "worsened": ok and prior is not None and _worse("off_main", prior, off_main_value),
                         "first_sighting": ok and prior is None and r.get("outcome") == "full"}
    # Stamp the set-aside collapse outcome onto `s` for the (pure) dashboard renderer — HOOK-SIDE ONLY, so the
    # status verb (which never calls _relay_lines) leaves these absent and renders the readout FULL. `newly` is
    # how many ids are set aside that were not last session (a plain diff of the two id lists), gated on `ok`
    # and a real list prior so a ledger-read failure never claims a false count.
    if set_aside_value is not None:
        r = results.get("set_aside", {"outcome": "full", "prior": None})
        prior = r.get("prior")
        newly = (len(set(set_aside_value) - set(prior))
                 if ok and isinstance(prior, list) else None)
        s["set_aside"] = {**s["set_aside"],
                          "collapsed": r.get("outcome") == "collapse",
                          "newly": newly}
    lines: list = []
    for a in alarms:
        if not a["collapsible"]:
            lines.append(a["full"])
            continue
        r = results.get(a["key"], {"outcome": "full", "prior": None})
        if r.get("outcome") == "collapse":
            lines.append(a["terse"])
        elif ok and r.get("prior") is not None and _worse(a["key"], r["prior"], a["value"]):
            lines.append(a["worse"])
        else:
            lines.append(a["full"])
    return lines


def assemble_pack(session_id: str | None = None, *, use_ledger: bool = False) -> str:
    """The AI-FACING briefing injected at SessionStart (the operator-presentation relay, D-187/D-188). It
    reaches the MODEL, never the operator's screen — so it tells the AI to (1) render the present-marker
    block first, (2) relay each INFORM line in plain words, (3) surface a brief needs-attention headline;
    the full operator dashboard follows for grounding. The present-marker instruction always names the
    `Project status` token (so the marker is present on every branch), and is emitted BEFORE the dashboard
    so a dashboard failure can't suppress it. Posture — the protected-branch merge is the real guarantee.

    `use_ledger` (the SessionStart HOOK path) applies the D-269 anti-habituation collapse — an unchanged
    standing alarm relays terse, a new/worsened one in full — via the deterministic ledger. The `pack`
    debug CLI leaves it False for a fresh, full render. The present-marker line and the dashboard NEVER
    collapse: only the must-push relay payload behind the marker varies (boot/README §Anti-habituation)."""
    s = gather_signals(session_id)
    marker = present_marker_line(s)
    push = _relay_lines(s) if use_ledger else must_push(s)
    # DURABLE half of the refused-cursor posture (D-059 law 1, U15b): on the REAL SessionStart path only
    # (use_ledger — never the `pack` debug view or the read-only status verb, both use_ledger=False), a
    # refused cursor spools ONE benign finding the #412 drain later promotes. A local gitignored append only,
    # so boot's read-only-against-GitHub posture holds; best-effort (emit_finding swallows every fault), so it
    # never perturbs the pack. Consistent with the one other use_ledger-gated side effect (the alarm ledger).
    if use_ledger and s["refused"]:
        emit_refused_cursor_finding()
    try:
        dashboard = render_dashboard(s)
    except Exception:
        dashboard = f"## {PRESENT_MARKER}\n(the full status couldn't be assembled this session)"

    out: list[str] = []
    out.append("=== ENGINE BOOT BRIEFING — for you, the assistant; the operator CANNOT see this ===")
    out.append("This reached you, not the operator: they see only what you type. Before you address the "
               "request, do these in order:")
    out.append(f"1. Open your reply with this `{PRESENT_MARKER}` block, exactly: **{marker}** — its "
               f"presence at the top is how the operator knows you grounded.")
    if push:
        out.append("2. Relay each of these to the operator in plain language (they are governance-critical "
                   "— do not skip any):")
        out.extend(f"   - {line}" for line in push)
        # AI-facing collapse contract (D-269; don't relay this line itself). An item phrased "still …
        # (unchanged since last session)" is a standing one already seen — relay it as the brief reminder
        # it is; a new or worsened item is stated in full. If a standing alarm has dropped off entirely
        # since last session, that means the engine re-checked and it is resolved — not that it stopped
        # watching; say so plainly if the operator asks. The emitted instruction below also bounds WHEN the
        # relay happens — once, in this grounding reply, with no invented "boot check" preamble and not
        # re-surfaced on later turns; keep this comment and that emitted text in step.
        out.append("   (An item marked 'still … (unchanged since last session)' is a standing one the "
                   "operator already saw — relay it as a brief reminder, not a fresh alarm; a new or "
                   "worsened item is stated in full. An alarm that dropped off since last session means "
                   "the engine verified it resolved, never that it stopped checking. Relay each alarm "
                   "once, here in this grounding reply, naming the thing and its consequence in plain "
                   "words — do not invent a 'boot check' or 'before we start setup' preamble, and do not "
                   "re-surface this framing on later turns of the same session. If the operator asks "
                   "again, answer plainly, without the boot-time framing.)")
    else:
        out.append("2. No governance alarm to relay this session.")
    out.append("3. Check the engine's live helpers and tell the operator about any that are off — a check you "
               "run against your own tools, since the engine cannot see them for you: " + MCP_AVAILABILITY_CHECK)
    out.append("4. Then surface a brief plain-language headline of anything in the status below that needs "
               "their attention. When the operator asks where things stand or what's next, run "
               "`uv run --directory .engine -- python tools/engine_status.py` and show its output verbatim "
               "— the same dashboard the `/engine-status` verb prints — rather than paraphrasing it. The "
               "protected-branch merge is the real governance guarantee — this relay is your discipline, "
               "not a wall.")
    out.append("")
    # The Explore write-gate's scope, in plain words, for the MODEL's grounding (modes owns the vocabulary;
    # boot places it). Self-labelled "don't relay" so it stays AI-facing and never enters the operator
    # relay. Always the Explore note: the handler clears the stance to Explore before this pack is built.
    out.append(modes.describe_explore_scope())
    out.append("")
    # The standing knowledge-faculty advertisement (#92): unconditional, so a cold session with no work in
    # hand still learns the wiring map exists and when to reach for it. AI-facing (above the dashboard
    # divider, no RELAY_MARKER); it complements — does not duplicate — the conditional in-flow cue below,
    # which fires only when there is a neighbourhood to pull deeper into.
    out.append(KNOWLEDGE_FACULTY_NOTE)
    out.append("")
    # The focused knowledge read (#37): the structural neighborhood of the work in hand — AI-orientation
    # context, placed in the briefing (not the operator dashboard), and only when there is work in hand.
    out.extend(render_neighborhood(s.get("neighborhood")))
    # The memory half of attention's recent decisions (#394 U01): what was DECIDED lately, pulled from the
    # project's saved memory at cold start and ordered by the ranking — AI-orientation context beside the
    # structural neighbourhood, not an operator alarm, and only when there is something recalled.
    out.extend(render_recalled_decisions(s.get("recalled")))
    out.append("--- the full status (your grounding for this session) ---")
    out.append(dashboard)
    return "\n".join(out)


# ---- the hook handler + CLI -----------------------------------------------------------------

def handler(payload: dict) -> dict:
    """The SessionStart handler. FIRST it clears the modes stance signal for this session (modes' own
    operation, run at boot's SessionStart moment) so every session — including a resume — boots Explore
    and never inherits a prior Build signal; THEN it assembles the orientation pack and injects it as
    additionalContext. Non-blocking — SessionStart cannot halt, and run_hook fail-opens on any exception.
    The clear is the FIRST statement so a later failure cannot skip it; if the platform cannot even
    deliver the payload, run_hook fail-opens before this runs — but the gate still defaults to Explore on
    any unreadable signal, and the merge wall backstops any write that slips that window."""
    session_id = payload.get("session_id") if isinstance(payload, dict) else None
    modes.clear_stance(session_id)
    # use_ledger=True: this is the real SessionStart path, so apply the D-269 collapse (an unchanged
    # standing alarm relays terse) via the deterministic ledger. fail-toward-full lives inside decide().
    pack = assemble_pack(session_id, use_ledger=True)
    return hooks.inject(pack) if pack else hooks.proceed()


def main(argv: list) -> int:
    if argv and argv[0] == "pack":
        print(assemble_pack())
        return 0
    if not argv or argv[0] == "hook":
        # Hook mode: what the wired SessionStart hook invokes. run_hook reads the event JSON from
        # stdin, runs the handler, and translates inject -> structured stdout (additionalContext),
        # fail-open on any error. The harness owns the exit code; boot never halts a session.
        return hooks.run_hook("SessionStart", handler)
    print("usage: boot.py [pack | hook]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
