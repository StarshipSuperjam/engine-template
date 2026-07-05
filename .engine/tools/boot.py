#!/usr/bin/env python3
"""Slice 20 — boot: the SessionStart orientation pack (the hook-DEPENDENT rich layer).

Beneath this sits slice 19's hook-INDEPENDENT floor (the root CLAUDE.md the platform always loads).
This module is the rich layer that rides on top when the SessionStart hook fires: it assembles a
bounded, prioritized, plain-language orientation pack from committed state and the substrates that
exist today, and injects it as `additionalContext` before the first prompt. The two-layer story is
the floor (always) + this pack (when the hook runs).

Boot's laws, all load-bearing here (systems/lifecycle/boot/README.md):
  - READ-ONLY OF CANONICAL STATE (D-269). Boot regenerates NO derived or committed state; it reads and
    surfaces. Its ONE local write is the gitignored, non-canonical standing-alarm presentation ledger
    (boot_alarm_ledger) — a record of what was already shown, not a regeneration of any canonical state.
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
modes exists (slice 21); memory's reversible-forgetting readout renders only once that substrate exists, so
on the genesis repo it is simply absent (no genesis-only scaffolding).

CLI:  python tools/boot.py pack     # print the assembled briefing (what the hook injects — a debug view)
      python tools/boot.py          # hook mode: run the SessionStart handler over stdin (what the
                                     #   wired hook invokes; injects additionalContext, fail-open)
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402
import hooks             # noqa: E402  (the fail-open harness + inject/proceed + command rendering)
import attention         # noqa: E402  (rank_live: the shared assembler boot consumes, never re-ranks)
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

# owes -> 25 (provisioning): the committed `.claude/settings.json` + the core-manifest hook `wires` carry
# the POSIX interpreter form (`.engine/.venv/bin/python`) — correct for the construction repo + CI, but a
# static committed file can name only ONE OS. Provisioning's Apply step must RE-RENDER each hook command
# per target OS at generation, via hooks.hook_command(relpath, os_name=<target>) (Windows = Scripts\python.exe),
# or a Windows adopter's interpreter path will not exist and the boot hook will fail open to floor-only boot.

PROTECTED_BRANCH = os.environ.get("PROTECTED_BRANCH", "main")
STATE_PATH = os.path.join(validate.ENGINE_DIR, "state", "state.json")

RECENTLY_SHIPPED_COUNT = 5   # the bounded "what just happened" digest

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

def read_state() -> tuple[dict | None, bool]:
    """Return (state, refused). `refused` is True when the committed cursor is unreadable or not a
    schema_version-1 cursor — boot then says project status is unknown and falls through to the rest of
    the pack, NEVER halting. A readable cursor returns (state, False), rendered defensively with .get().

    Boot surfaces the refusal in-band (the operator-facing half). The spec also has boot emit a telemetry
    FINDING on a refused cursor; that is a WRITE, which is the deferred hooks-stderr->findings-inbox relay
    telemetry owns — not boot's read-only path. So boot surfaces the refusal here and the finding-emission
    rides telemetry's writer relay (owes -> telemetry writer)."""
    try:
        state = validate.load_json(STATE_PATH)
        if not isinstance(state, dict) or state.get("schema_version") != 1:
            return None, True
        return state, False
    except Exception:  # noqa: BLE001 — absent / malformed cursor degrades to "unknown", never a crash
        return None, True


# ---- governance alarms (relayed from the substrates; pinned at the top of the card) ---------

def protected_branch_signal(repo: str | None, token: str | None) -> tuple[str, str | None]:
    """The protected-branch governance signal, RELAYED from protection_guard (the control-plane's own
    evaluation), in three honest states:
      ("off", reason)       -> the gate is NOT in force: a pinned governance alarm (a NAG — boot does not
                               apply the fix; the one-click apply is provisioning, slice 25).
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
        missing = protection_guard.missing_floor(rules, protection_guard.REQUIRED_CHECKS)
    except Exception:  # noqa: BLE001 — unreachable / auth / malformed body -> unknown, never a false "on"
        return "unknown", None
    if missing:
        return "off", "; ".join(missing)
    return "on", None


def open_findings(repo: str | None, token: str | None) -> tuple[int | None, str | None]:
    """The engine's open self-monitoring findings, RELAYED read-only from telemetry's debt register
    (the engine-labelled open Issues) via telemetry's own reader — NEVER the write loop. Returns
    (count, register_url): count is None when the register could not be read (degraded), 0 when the
    register is reachable and empty. Boot only reads; telemetry owns the register and its triage."""
    if not repo or not token:
        return None, None
    try:
        gh = telemetry.GitHubIssues(repo, token)
        issues = gh.list_open_engine_issues()
        return len(issues), gh.issues_query_url()
    except Exception:  # noqa: BLE001 — DegradedReadError or any transport failure -> unknown (degraded)
        return None, None


# ---- attention (consume the ranked partition; resolve member ids to plain language) ---------

def _resolve_member(member_id: str, state: dict | None) -> str:
    """Resolve one attention member id (a reference, not content) to a plain-language line. Boot
    resolves; it does not re-rank. Unknown ids fall back to the id itself so nothing is silently lost."""
    if member_id == "state:standing-situation":
        # NOT surfaced as an action line. The card already shows "Where we are" live in the facts block above
        # (fresh each session), and when that live read fails it carries its own stale-warning right there — so
        # a separate "confirm where you stand" nudge would be redundant in the fresh case and a duplicate of
        # that stale-warning in the failure case. Attention still ranks this orientation pointer for the budget
        # model; boot just doesn't nag with it. Returning "" -> needs_attention skips it (no blank bullet).
        return ""
    if member_id == "state:integration-debt":
        # No count here: the card header already renders the authoritative open-problem figure (live
        # when reachable, else the offline shadow marked loud-if-stale). Restating a second, possibly-
        # disagreeing number would undercut it — so this line is the actionable nudge only.
        return "Open integration debt is waiting — clear it before new work piles on top."
    if ":" in member_id:
        kind, _, slug = member_id.partition(":")
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


def needs_attention(state: dict | None, *, gh=None, live_findings: int | None = None, source=None) -> tuple[list, list, dict | None]:
    """Consume attention.rank_live and SPLIT its ranked partition into (1) operator ACTION lines, rendered in
    the GIVEN precedence order as plain language (a bounded prefix per category — boot renders, never
    re-orders), and (2) the knowledge NEIGHBORHOOD of the work in hand. The neighborhood is AI-orientation
    context, NOT an operator action item, so `structural_neighbors` are routed to the pack's neighborhood
    block (assemble_pack) and never to the action list. Returns (action_lines, degraded_inputs, neighborhood).

    The focus is DERIVED here from the in-flight work record (#37): the files the work touches -> their owning
    entities -> a focused knowledge read. `gh` is the GitHub reader boot built from the live repo/token;
    attention reads the work record (open PRs + the working branch) through it, and the focus from the local
    git floor (no token needed). `live_findings` is the live debt-register count boot already read
    (open_findings), threaded through to the assembler so the ranking and the card header read ONE number and no
    second GitHub read happens; when it is None (no reader / a failed read) telemetry degrades and the committed
    count stands in, so degraded_inputs carries `telemetry` and boot raises the loud 'couldn't reach' notice."""
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
        result = attention.rank_live(override=operator_overrides.slice_for("attention") or None,
                                     focus=focus or None, gh=gh, source=source, live_findings=live_findings,
                                     budget_total=COLD_START_BUDGET)
    except Exception:  # noqa: BLE001 — attention unavailable -> no ranked lines, the rest of the pack stands
        return [], ["attention"], None
    lines: list = []
    for entry in result.get("partition", []):
        if entry.get("category") == "structural_neighbors":
            continue        # the knowledge neighbourhood is the AI pack block (rendered from the richer
                            # neighborhood_of summary below), never an operator action line
        # The attention policy's reviewable per-kind budget governs how many items this kind surfaces (the
        # buried flat cap is retired). budget_size is 0 for a kind the trim order shed under a tight budget —
        # so it naturally contributes nothing — but at the shipped COLD_START_BUDGET every kind seats, so
        # nothing is shed here. NEEDS_ATTENTION_CAP is only the defensive floor for a budget-less result.
        cap = entry.get("budget_size", NEEDS_ATTENTION_CAP)
        for member in (entry.get("members") or [])[:cap]:
            line = _resolve_member(member.get("id", ""), state)
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
    return lines, list(result.get("degraded_inputs") or []), neighborhood


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

def recently_shipped(count: int = RECENTLY_SHIPPED_COUNT) -> list[str]:
    """A bounded digest reconstructed from merged pull requests (the structured PR body is the engine's
    narrative; there is no changelog file). Reads local merge commits — degrades to [] when unavailable."""
    raw = _run(["git", "log", "--merges", "--first-parent", f"-n{count}",
                "--format=%s%x1f%b%x1e"])
    if not raw:
        return []
    items: list[str] = []
    for record in raw.split("\x1e"):
        record = record.strip()
        if not record:
            continue
        subject, _, body = record.partition("\x1f")
        m = re.search(r"#(\d+)", subject)
        number = f"#{m.group(1)}" if m else ""
        title = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
        if not title:  # fall back to a humanized branch name from the merge subject
            b = re.search(r"from \S+?/(\S+)", subject)
            title = b.group(1).replace("-", " ").replace("/", " ") if b else subject
        items.append(f"{number} — {title}".strip(" —"))
    return items


# ---- assembly: gather signals -> render the operator dashboard -> wrap the AI briefing ------

# The imperative relay marker (glossary): the directive embedded in the AI-facing briefing that compels the
# AI to surface a high-impact item to the operator in plain words. RESERVED for the must-push set so the
# relay never becomes a firehose — routine status carries no marker and is pulled via the status verb.
RELAY_MARKER = "INFORM THE USER THAT"


def gather_signals(session_id: str | None = None) -> dict:
    """Read + DETECT every signal the dashboard renders — the substrates' own detection, which boot only
    relays (it computes no new state). Each read is best-effort upstream and degrades that signal only.
    Returns a flat dict consumed by render_dashboard / present_marker_line / must_push — the single place
    boot reaches the substrates, so the status verb (slice 3) re-gathers and renders the same way."""
    state, refused = read_state()
    repo, token = repo_slug(), gh_token()
    gate, reason = protected_branch_signal(repo, token)
    finding_count, register = open_findings(repo, token)
    debt_count, debt_as_of = telemetry.read_state_debt(STATE_PATH)
    # The GitHub reader for attention's in-flight work-record read (open PRs). None without a repo/token ->
    # attention falls back to the local-git floor (the working branch). Construction does no I/O (telemetry.py).
    gh = telemetry.GitHubIssues(repo, token) if repo and token else None
    # Thread the live debt-register count boot ALREADY read (open_findings, above) into the ranking, so the
    # ranking and the "Open problems" header read ONE number (they cannot disagree) and the SessionStart path
    # makes no second GitHub call. None (no repo/token, or a failed read) -> telemetry degrades and the
    # committed count stands in -> boot raises the loud 'couldn't reach' notice.
    # Boot's rung-1 knowledge slice (#37), read ONCE here and threaded into needs_attention — the SAME read also
    # carries `from_live`: True when the committed graph.json was absent and orientation ran on a LIVE rebuild
    # (rung 3, "loudly degraded"). That drives the rebuilt-map heads-up, NOT the att_degraded "couldn't reach"
    # notice — the map IS reachable, only the committed file is missing. read() fail-opens to None (never raises
    # into boot), so a read failure leaves map_rebuilt False and is covered instead by the "couldn't reach" path.
    source = boot_slice.read()
    map_rebuilt = bool(source and getattr(source, "from_live", False))
    att_lines, att_degraded, neighborhood = needs_attention(state, gh=gh, live_findings=finding_count, source=source)
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
        "finding_count": finding_count, "register": register,
        "debt_count": debt_count, "debt_as_of": debt_as_of,
        "att_lines": att_lines, "att_degraded": att_degraded,
        # True iff orientation ran on a LIVE-rebuilt map because the committed graph.json is absent (a distinct
        # heads-up, NOT the att_degraded "couldn't reach": the map is reachable, the committed file is missing)
        "map_rebuilt": map_rebuilt,
        # the knowledge neighborhood of the work in hand (focused read, #37) -> the AI pack block, or None
        "neighborhood": neighborhood,
        "shipped": recently_shipped(),
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
        # the self-review freshness finding (soft = hasn't-run-yet / has-gone-stale; note = current), or None
        "audit_stale": audit_stale,
        # the live-derived {milestone, phase}, or None when GitHub was unreachable (-> render the cached copy)
        "live_standing": live_standing,
    }


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
        pinned.append(
            f"⛔ **Your safety gate is off** — `{PROTECTED_BRANCH}` isn't protected, so unreviewed work "
            f"could reach your main branch ({s['reason']}). It needs re-enabling; an automated one-click "
            f"fix is coming, but for now turn branch protection back on in your repository settings.")
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
            "recorded. Tell me the repository your engine updates from (its owner/repo) and I'll record it, then "
            "updates will work. (Recording where the engine updates from is a newer part of the engine, so you may "
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
        elif s["debt_count"]:
            out.append(f"**Open problems:** {telemetry.degraded_readout(s['debt_count'], s['debt_as_of'])}")
        else:
            out.append("**Open problems:** none recorded yet.")

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
                        "git": "your in-flight branches and pull requests",
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
    out.extend(f"- {line}" for line in s["shipped"]) if s["shipped"] else out.append(
        "- (no recent merges found)")

    # The artifact warrant (D-261), proportionately LIGHT: this dashboard — and the project map it
    # draws on — is an automated readout derived from the engine's own checks, so it states its bound
    # right where it is read. The graph behind "your project map" is a byte-fingerprinted generated file
    # whose bound rides this startup view (an authored field in it would break exact-match regeneration),
    # so the line lives here, not in the raw graph. Light because the limit is near self-evident and the
    # real gate (the merge review) is named elsewhere in this briefing.
    out.append("")
    out.append("_This view is an automated readout: a clear status shows the checks the engine can run "
               "came back clean — not that everything is correct. Your review at merge is the real gate._")

    return "\n".join(out)


def present_marker_line(s: dict) -> str:
    """The short titled status block the AI is told to render FIRST — `Project status: all clear`, or a
    `⚠ ...` line when something governance-critical or a grounding failure fired. A fixed relay over
    already-detected signals (boot computes no new state); a couldn't-verify gate NEVER reads as a green
    all-clear (degrade-loud)."""
    if s["gate"] == "off":
        return "⚠ Protected branch is off"
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
        full = (f"{RELAY_MARKER} their safety gate is off — `{PROTECTED_BRANCH}` isn't protected, so "
                f"unreviewed work could reach the main branch ({s['reason']}); it needs re-enabling.")
        terse = (f"{RELAY_MARKER} their safety gate is still off (unchanged since last session) — "
                 f"unreviewed work could still reach `{PROTECTED_BRANCH}`; it still needs re-enabling.")
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
        alarms.append({"key": "findings", "value": s["finding_count"], "collapsible": True,
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


def _worse(key: str, prior, current) -> bool:
    """Whether a changed collapse-eligible condition got WORSE (so it relays full with the 'this got worse'
    wording, never a quiet reminder). Ordered only where 'worse' is meaningful: the open-findings count
    rising; an off-main park escalating to behind-the-main-line. A gate going on->off is an alarm that was
    ABSENT last session (no prior entry), so it surfaces as a first-appearance full relay, not a 'worse'."""
    if key == "findings":
        return isinstance(prior, int) and isinstance(current, int) and current > prior
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
    out.append("3. Then surface a brief plain-language headline of anything in the status below that needs "
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
