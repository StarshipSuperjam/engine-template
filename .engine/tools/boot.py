#!/usr/bin/env python3
"""Slice 20 — boot: the SessionStart orientation pack (the hook-DEPENDENT rich layer).

Beneath this sits slice 19's hook-INDEPENDENT floor (the root CLAUDE.md the platform always loads).
This module is the rich layer that rides on top when the SessionStart hook fires: it assembles a
bounded, prioritized, plain-language orientation pack from committed state and the substrates that
exist today, and injects it as `additionalContext` before the first prompt. The two-layer story is
the floor (always) + this pack (when the hook runs).

Boot's laws, all load-bearing here (systems/lifecycle/boot/README.md):
  - READ-ONLY orientation. Boot regenerates NO derived state; it only reads and surfaces.
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
import operator_overrides  # noqa: E402  (the operator policy-override file reader; boot loads it, passes the slice as DATA)
import telemetry         # noqa: E402  (read_state_debt / degraded_readout / the read-only Issue list)
import protection_guard  # noqa: E402  (api_get + missing_floor: the protected-branch evaluation)
import modes             # noqa: E402  (clear_stance + the stance vocabulary: the SessionStart clear + line)
import checkout_health   # noqa: E402  (provisioning's operator-checkout strand detector; boot relays its detection)
import standing_situation  # noqa: E402  ("where we are" derived live from GitHub, read-only; boot displays, never writes)

# The card title a healthy boot always renders — byte-identical to the present-marker the floor names
# in CLAUDE.deployed.md (slice 19 `owes ←`). The byte-identity is locked by test_boot.py; renaming it
# here without the floor (or vice-versa) breaks the double-fault check, so the two move together.
PRESENT_MARKER = "Project status"

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
NEEDS_ATTENTION_CAP = 4      # render at most this many items per attention category (a bounded view;
                             #   boot renders a prefix of attention's order — it never re-orders)


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
        rules = protection_guard.api_get(
            f"/repos/{repo}/rules/branches/{PROTECTED_BRANCH}", token)
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
        # A neutral re-ground nudge — NOT a quote of the committed cursor. "Where we are" is now shown live
        # in the facts block above (derived fresh each session); the committed copy is only the offline cache,
        # so quoting it here as "pick up where you left off" would present a possibly-stale value as the live
        # to-do (the conflation the card-rendering law forbids). Point back to the live line instead.
        return "Confirm where the project stands before building on it."
    if member_id == "state:integration-debt":
        # No count here: the card header already renders the authoritative open-problem figure (live
        # when reachable, else the offline shadow marked loud-if-stale). Restating a second, possibly-
        # disagreeing number would undercut it — so this line is the actionable nudge only.
        return "Open integration debt is waiting — clear it before new work piles on top."
    if ":" in member_id:
        kind, _, slug = member_id.partition(":")
        return f"Related: {slug} ({kind}) — query and verify before relying on it."
    return member_id


def needs_attention(state: dict | None) -> tuple[list[str], list[str]]:
    """Consume attention.rank_live and render the ranked partition in its GIVEN precedence order as
    plain-language lines (a bounded prefix per category — boot renders, never re-orders). Returns
    (lines, degraded_inputs). On the genesis repo the partition is largely empty and degraded_inputs
    is non-empty every run (telemetry-as-register + the git work-record reader do not exist yet) — the
    normal path, surfaced as a routine degraded notice, not an alarm."""
    try:
        # Load the operator policy-override (operator config, absent until first tuned) and pass attention's
        # slice as DATA — boot is the LOADING layer; attention merges it per-key (D-167), never reads the file.
        result = attention.rank_live(override=operator_overrides.slice_for("attention") or None)
    except Exception:  # noqa: BLE001 — attention unavailable -> no ranked lines, the rest of the pack stands
        return [], ["attention"]
    lines: list[str] = []
    for entry in result.get("partition", []):
        members = entry.get("members") or []
        for member in members[:NEEDS_ATTENTION_CAP]:
            line = _resolve_member(member.get("id", ""), state)
            if line:                       # skip an id-less member rather than render a blank bullet
                lines.append(line)
    return lines, list(result.get("degraded_inputs") or [])


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
    att_lines, att_degraded = needs_attention(state)
    try:
        # Provisioning's strand detector, RELAYED (boot computes no new state). A strand-check failure is
        # low-stakes (a stranded local checkout cannot reach the protected branch), so it degrades QUIETLY
        # to None — never a "couldn't check your folder" nag; the double-fault is the present-marker floor's.
        strand = checkout_health.detect_strand()
    except Exception:  # noqa: BLE001 — any detector failure degrades that one signal, never the pack
        strand = None
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
        # an online-looking session that couldn't reach the live register -> a degraded notice, not silence
        "findings_unavailable": finding_count is None and bool(repo) and token is None,
        "debt_count": debt_count, "debt_as_of": debt_as_of,
        "att_lines": att_lines, "att_degraded": att_degraded,
        "shipped": recently_shipped(),
        "stance": modes.describe_stance(modes.current_stance(session_id)),
        "strand": strand,   # a stranded operator checkout (detached / missing engine files), or None
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
    elif s["findings_unavailable"]:
        degraded.append("I couldn't check the engine's open findings (no GitHub access from here).")

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
        # Phrased about ranking DEPTH, not raw substrate names: those names ("telemetry"...) would
        # collide with the live, authoritative figures rendered above and make the operator doubt a
        # number that is in fact current.
        degraded.append(
            "I couldn't rank your work by priority this session — some of the ranking inputs aren't "
            "wired up yet, so the list below may be thin. (Expected on a new engine; it doesn't mean "
            "anything is wrong with your project.)")

    if degraded:
        out.append("")
        out.extend(f"_{line}_" for line in degraded)

    out.append("")
    out.append("### Needs your attention")
    out.extend(f"- {line}" for line in s["att_lines"]) if s["att_lines"] else out.append(
        "- Nothing is blocking right now.")

    out.append("")
    out.append("### Recently shipped")
    out.extend(f"- {line}" for line in s["shipped"]) if s["shipped"] else out.append(
        "- (no recent merges found)")

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
    return f"{PRESENT_MARKER}: all clear"


def must_push(s: dict) -> list:
    """The INFORM-marked items the AI MUST relay to the operator in plain words — the governance-critical
    alarms and the grounding-failure tell (D-187 must-push set). A fixed relay over detected signals;
    routine status carries no marker (it is pulled via the status verb)."""
    items: list[str] = []
    if s["gate"] == "off":
        items.append(
            f"{RELAY_MARKER} their safety gate is off — `{PROTECTED_BRANCH}` isn't protected, so "
            f"unreviewed work could reach the main branch ({s['reason']}); it needs re-enabling.")
    elif s["gate"] == "unknown":
        items.append(
            f"{RELAY_MARKER} the safety gate couldn't be verified (no GitHub access), so they shouldn't "
            f"assume `{PROTECTED_BRANCH}` is protected — confirm before merging anything important.")
    if s["refused"]:
        items.append(
            f"{RELAY_MARKER} the engine couldn't read where the project stands, so project status is "
            f"unknown until it re-grounds.")
    if s["finding_count"]:
        items.append(
            f"{RELAY_MARKER} there are {s['finding_count']} open engine finding(s) about the engine's "
            f"own health to review: {s['register']}")
    return items


def assemble_pack(session_id: str | None = None) -> str:
    """The AI-FACING briefing injected at SessionStart (the operator-presentation relay, D-187/D-188). It
    reaches the MODEL, never the operator's screen — so it tells the AI to (1) render the present-marker
    block first, (2) relay each INFORM line in plain words, (3) surface a brief needs-attention headline;
    the full operator dashboard follows for grounding. The present-marker instruction always names the
    `Project status` token (so the marker is present on every branch), and is emitted BEFORE the dashboard
    so a dashboard failure can't suppress it. Posture — the protected-branch merge is the real guarantee."""
    s = gather_signals(session_id)
    marker = present_marker_line(s)
    push = must_push(s)
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
    pack = assemble_pack(session_id)
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
