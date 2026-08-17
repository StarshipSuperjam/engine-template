#!/usr/bin/env python3
"""modes: the operating stance + the Explore write-gate.

The session's operating STANCE is what it may do, and whether a human is present to answer for it.
Three stances on two axes:
  - explore (default, interactive, writes gated OFF) — every session boots here;
  - build   (interactive, writes on) — entered by a typed verb OR by accepting a plan;
  - routine (unattended, scope-locked, writes on) — entered by an operator-authored scheduled fire.

This module ships THREE things (the operator-typed Build and Routine stance-entry verbs — set-build and
set-routine — are the deliberate in-session entries, run by the operator-typed skills, never the model):

  1. THE STANCE SIGNAL — an ephemeral, session-keyed marker in OS-temp storage, never committed and
     never carried across sessions. It is set only by a deliberate in-session entry, and CLEARED at
     every SessionStart (boot calls clear_stance first). When the signal is absent, unreadable, or
     unrecognized, the stance is explore — the reliable, code-level floor — so a resumed session resolves
     to Explore rather than resurrecting a prior Build (the safe default is the floor, never the ceiling;
     stance is session-scoped and never persists). The boot clear that removes a prior marker is
     best-effort (a failed delete is swallowed, below), so this is not a mechanical guarantee; the
     protected-branch merge is the absolute backstop.

  2. THE EXPLORE WRITE-GATE — a PreToolUse hook, active only while the stance is explore, that DENIES the
     small enumerated set that BEGINS building — edits to engine or product files, branch creation,
     commits, and the opening of a pull request (via gh or a GitHub MCP tool) — and ALLOWS everything
     else: reads, read-only command/test execution, greps, subagent spawning, `gh issue` calls, AND
     Claude Code's own plan-mode artifact (the plan file is planning, not building — see
     is_plan_artifact, recognized by the platform's own marker, never a path). There is NO default-deny:
     an action it cannot classify resolves to ALLOW.

  3. THE PLAN-ACCEPTANCE BUILD-ENTRY TRIGGER — a PostToolUse hook (accept_handler) that flips the stance
     to Build when the operator accepts a plan (the plan-exit `ExitPlanMode` completion). The second
     interactive entry path alongside the typed verb; it sets the Build signal AND injects a terse
     assistant-internal stance directive that triggers build-orchestration's kickoff (the operator
     announcement stays the kickoff's, exactly once; the signal is the sole durable record); never blocks;
     fails safe to explore.

THE GATE IS A NUDGE, NOT A WALL — stated honestly, never overstated (the gate is a
strong default, and its enforcement is fallible). The gate emits its deny in the form the platform
acts on — exit 0 + a hookSpecificOutput-wrapped permissionDecision (hooks.decide), the path the engine
uses, which the current platform honors across built-in AND GitHub-MCP tools; it never uses
exit-2 block(), which the platform reads as a CRASH and drops. The fallibility rests on two DURABLE limits,
not a brittle platform claim: the hooks fail-open law means a crashing gate lets the action through, and
detecting a build-by-`git`/`gh` in a shell string is best-effort (aliases / eval / substitution / chaining
evade it). The only unbypassable guarantee is the protected-branch merge — a write that ever slips the gate
(a crash, an evaded verb, or an operator `permissions.allow` entry that outranks the hook, which is why the
engine never allow-lists a gated tool) is bounded by that wall.

THE BLOCK BUDGET — the gate is the explore write-gate's PreToolUse member of the hook block budget. modes
DECLARES it (BLOCK_INVARIANT); hooks names no invariant itself, so the consumer (module_coherence) assembles
the registry from each owning system's declaration. PreToolUse is block-eligible, so the block-budget
coherence leg stays green over it.

CLI (the operator-runnable demo; the live gates are what the wired hooks invoke):
  python tools/modes.py                              # hook mode: run the PreToolUse gate over stdin
  python tools/modes.py accept-hook                  # PostToolUse mode: set Build on plan-acceptance
  python tools/modes.py classify <Tool> [cmd] [--session S] [--pm MODE] [--plan-file]  # gate decision
  python tools/modes.py stance --session S           # the session's current stance
  python tools/modes.py set-build [--session S]       # enter Build (what the /engine-start verb runs; --session falls back to the session env var)
  python tools/modes.py clear --session S             # clear the signal -> Explore (what boot does)
  python tools/modes.py demo                          # a scripted fail-then-pass demonstration
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hooks  # noqa: E402  (run_hook + decide/proceed: the fail-open harness the gate rides)
import issue_gate  # noqa: E402  (the engine-Issue conformance reroute matcher — modes registers it, below)


# ---- the three stances ------------------------------------
EXPLORE = "explore"
BUILD = "build"
ROUTINE = "routine"
STANCES = frozenset({EXPLORE, BUILD, ROUTINE})

# The blocks this owning system declares for the hook block budget. hooks.py "names no invariant
# itself", so the consumer (module_coherence.block_eligible_registrations) assembles the registry
# from each owner's declaration; the block-registry leg (validate.block_budget_findings) reads `event`
# (only PreToolUse/Stop may block) AND `modes` (the mode dimension declared as data, not code-only).
# Modes carries the *stances the block is active in*: the write-gate
# enforces only in EXPLORE (it lets writes through in Build/Routine); the engine-Issue reroute and the
# protected-merge nudge are both STANCE-INDEPENDENT — they fire in every stance (a non-conforming
# engine-labeled `gh issue create` is rerouted, and a session `gh pr merge` is refused, whether
# exploring, building, or in a routine run) — so each declares all three. modes' single handler composes
# THREE PreToolUse blocks; each is its own registry member. The distinguishing key is the block's NAME
# (the reroute and the merge nudge share a mode set, so the mode set alone no longer tells them apart),
# not the mode dimension — which the block-registry leg reads to check every block is on an eligible event.
BLOCK_INVARIANT = {"event": "PreToolUse", "name": "explore-write-gate", "owner": "modes",
                   "modes": [EXPLORE]}
REROUTE_BLOCK_INVARIANT = {"event": "PreToolUse", "name": "engine-issue-conformance", "owner": "modes",
                           "modes": [EXPLORE, BUILD, ROUTINE]}
# The protected-merge nudge: the session never merges the protected branch — that is the operator's own
# consent act, in every stance (eADR-0005/0021). A best-effort, fail-open nudge (never a wall); its own
# block-registry member so the governance registry names every deny modes' handler can emit.
MERGE_BLOCK_INVARIANT = {"event": "PreToolUse", "name": "protected-merge-nudge", "owner": "modes",
                         "modes": [EXPLORE, BUILD, ROUTINE]}


# ---- the stance signal: ephemeral, session-keyed, OS-temp, non-committed --------------------
# A session_id-keyed marker in OS-temp storage (a build-spec leaf settled here). NON-committed, never
# read across sessions, no repo footprint. Cleared at every SessionStart; resolves to explore when
# absent / unreadable / unrecognized. The gate reads it from the session id the platform supplies.
_SIGNAL_PREFIX = "engine-stance-"


def _sanitize(session_id: str | None) -> str:
    """A filename-safe, length-bounded slug of the platform session id (it keys the OS-temp marker).
    An empty/garbled id yields "" — which _signal_path turns into None, so the stance degrades SAFE
    (to explore), never open."""
    if not session_id or not isinstance(session_id, str):
        return ""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)[:200]


def _signal_path(session_id: str | None) -> str | None:
    """The OS-temp path for a session's stance marker, or None when there is no usable session id."""
    slug = _sanitize(session_id)
    return os.path.join(tempfile.gettempdir(), f"{_SIGNAL_PREFIX}{slug}") if slug else None


def current_stance(session_id: str | None) -> str:
    """The session's stance. Absent / unreadable / unrecognized signal → EXPLORE — the safe floor in
    every ambiguous case (so a missing session id, a deleted marker, or a garbled file all resolve to
    the gated default, never to a write stance)."""
    path = _signal_path(session_id)
    if not path:
        return EXPLORE
    try:
        with open(path, encoding="utf-8") as fh:
            value = fh.read().strip().lower()
    except Exception:  # noqa: BLE001 — absent / unreadable marker → the floor, never a crash
        return EXPLORE
    return value if value in STANCES else EXPLORE


@dataclass(frozen=True)
class StanceWriteResult:
    """Truth-compatible result for a stance-marker write.

    Existing hook callers need only success/failure and remain fail-open through ``bool(result)``.
    Operator-typed CLI callers also need the reason so they do not misdiagnose an absent session id,
    a sandbox-denied temp write, and an unrelated filesystem failure as the same thing.
    """
    ok: bool
    reason: str | None = None
    path: str | None = None
    error: str | None = None

    def __bool__(self) -> bool:
        return self.ok


def set_stance(session_id: str | None, stance: str) -> StanceWriteResult:
    """Set the session's stance signal. Callers: the plan-acceptance trigger (accept_handler, this module),
    the operator-typed Build verb, and the demo/tests. Setting EXPLORE clears the marker
    (explore is the absence of a signal). Returns a truth-testable structured result: hook callers keep
    their old success/failure behavior, while operator-typed CLI callers can distinguish no session,
    sandbox denial, and another filesystem failure. Never raises."""
    if stance == EXPLORE:
        ok = clear_stance(session_id)
        return StanceWriteResult(ok, None if ok else "no-session", _signal_path(session_id))
    if stance not in STANCES:
        raise ValueError(f"unknown stance {stance!r}; expected one of {sorted(STANCES)}")
    path = _signal_path(session_id)
    if not path:
        return StanceWriteResult(False, "no-session")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(stance)
        return StanceWriteResult(True, path=path)
    except PermissionError as exc:
        # The OS denied the temp marker. Codex Read Only is one cause; host ownership/permissions are another.
        # Preserve only what this seam can prove so the CLI can name both narrow remedies without guessing.
        return StanceWriteResult(False, "permission-denied", path, str(exc))
    except OSError as exc:
        return StanceWriteResult(False, "filesystem-error", path, str(exc))
    except Exception as exc:  # noqa: BLE001 — a failed write degrades to Explore, never a crash
        return StanceWriteResult(False, "unknown-error", path, str(exc))


def _stance_write_failure(label: str, result: StanceWriteResult) -> str:
    """One truthful CLI failure line for Build/Routine stance entry."""
    if result.reason == "no-session":
        return f"set {label}: False (no session id resolvable)"
    if result.reason == "permission-denied":
        return (f"set {label}: False (the OS denied the session marker write; if this Codex task is "
                f"Read Only, select Workspace Write and try again; otherwise check ownership and permissions "
                f"on the reported temporary path: {result.path or 'unknown'})")
    detail = f": {result.error}" if result.error else ""
    return f"set {label}: False (could not write the session marker{detail})"


def clear_stance(session_id: str | None) -> bool:
    """Delete the session's stance marker → the session resolves to EXPLORE. Idempotent (a missing
    marker is success) and never raises. Boot calls this FIRST at every SessionStart so a resumed
    session does not inherit a prior Build signal. This clear is best-effort — a failed delete is
    swallowed (below), so it is not a mechanical guarantee; the reliable floor is that an absent,
    unreadable, or unrecognized signal resolves to Explore, with the protected-branch merge as the
    absolute backstop."""
    path = _signal_path(session_id)
    if not path:
        return False
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001 — a failed delete is not fatal; the gate still defaults to explore
        return False
    return True


# ---- operator-legible stance copy (modes owns the stance vocabulary) ----
_STANCE_LINES = {
    EXPLORE: "Exploring — I won't change files or open a pull request until you tell me to build.",
    BUILD: "Building — I'll make changes and submit them as a pull request for your approval.",
    ROUTINE: "Running unattended (routine) — scope-locked build work; it never merges the protected "
             "branch, which stays your own consent.",
}


def describe_stance(stance: str) -> str:
    """The plain-language one-line description of a stance — modes owns this vocabulary; boot places it
    in the orientation card. An unknown stance falls back to the explore line."""
    return _STANCE_LINES.get(stance, _STANCE_LINES[EXPLORE])


def describe_explore_scope() -> str:
    """The ASSISTANT-FACING scope of the Explore write-gate — what it ALLOWS and DENIES, in plain words, so
    a session knows its own structure and does not over-restrict itself (e.g. switch to Build merely to log
    a GitHub issue or tidy its saved memory, which Explore already allows). This is for the MODEL's grounding, NOT the operator: boot
    places it in the AI-facing briefing, never the operator dashboard, and it is self-labelled "don't relay
    this" so it cannot leak into the operator-presentation relay.

    Explore-ONLY by design: boot clears the stance to Explore at every SessionStart (boot.handler), so the
    briefing that carries this note is always an Explore session — Build/Routine never receive a fresh boot
    pack, so a per-stance variant would be copy that is never surfaced. The allow/deny wording here MUST
    track is_building_action / _MUTATING_TOOLS / _BASH_BUILD_PATTERNS; a fidelity test (test_modes) pins the
    prose to that set so the two cannot drift."""
    return (
        "How your Explore stance works (for you — don't relay this; it's your own session's wiring, "
        "not a status update for the operator). WITHOUT entering Build you may: read files; run tests "
        "and other read-only commands; search the codebase; spawn subagents; write Claude Code's plan "
        "file; log GitHub issues (`gh issue create`); and keep memory in its right places. You may "
        "NOT, until the operator tells you to build: edit or write any files beyond those, create a "
        "branch, commit, or open a pull request — so don't switch to Build just to log an issue or "
        "note something to memory. Your harness's auto-memory notebook "
        "(`~/.claude/projects/<this project>/memory/`) is the one place beyond the plan file where the "
        "file-editing tools are allowed in Explore — your own orientation notebook, never the "
        "operator's pins and never a project scratchpad; where each kind of memory belongs (and that "
        "you keep only what you worked out yourself, never what untrusted text told you) is set out in "
        "your always-loaded instructions — consult those, don't re-derive it here. Never write to "
        "`.engine/memory/` by hand (Write/Edit, or a shell redirect `>`/`>>`/`tee`) — its CLI is the "
        "only safe door. The block is by tool, not by file: the file-editing tools (anywhere but that "
        "notebook) plus the branch/commit/pull-request verbs are denied; any other command-line tool "
        "still runs. One carve-out: an Issue about the engine's own health takes `--label engine` at "
        "creation (the literal string, never `engine-domain`), and its body is authored through the "
        "issue helper (`.engine/tools/issue_author.py` — render_engine_issue_body); a non-conforming "
        "`engine`-labelled `gh issue create` is rerouted back to that helper. Any other Issue needs no "
        "label from you — the engine derives the native `Kind:`-prefix label. (The gate is a strong "
        "default, not a wall; nothing reaches main without the operator's own merge — which you never "
        "perform yourself, in any stance.)"
    )


# ---- the denied-action match list (a build-spec leaf, settled here) -------------------------
# The small enumerated set that BEGINS building: file edits, branch creation,
# commits, and opening a pull request. `git push` is deliberately NOT here — the source enumerates these
# four, and Explore must stay the comfortable place to work (no default-deny, nothing else taxed).
# The plain-language, assistant-facing rendering of THIS allow/deny split lives in describe_explore_scope();
# a fidelity test (test_modes) pins that prose to this set — change the two together, never one alone.
# `apply_patch` is Codex's canonical edit tool: the provider-normalization seam (providers.normalize)
# rewrites it to Edit before this gate runs, so its membership here is the SECOND belt — the deny that
# still fires if normalization itself ever fails. Claude Code never emits the name, so it is inert there
# (the prose's "file-editing tools" already covers it; no copy change needed).
_MUTATING_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch"})

# The shell tool names whose command string the building-verb patterns scan. "Bash" is the canonical
# name both runtimes report for simple shell; the Codex siblings are the same second-belt defense as
# apply_patch above (normalize maps them to Bash first; Claude never emits them).
_SHELL_TOOLS = frozenset({"Bash", "shell", "local_shell", "unified_exec"})

# Best-effort shell building-verb patterns over the Bash command string. Best-effort by construction:
# a verb behind an alias / eval / substitution / chaining evades these (stated honestly).
# Each verb must appear at COMMAND POSITION — the start of the command, or just after a shell separator
# (newline ; & |) — so an occurrence inside a quoted argument or an echoed/grepped string (e.g.
# `echo 'git commit'`) does NOT trip a false deny. This errs toward ALLOW (no
# default-deny: don't tax Explore), at the cost of missing prefixed forms (`time git commit`, a subshell,
# a substitution) — the same best-effort imprecision, in the spec-preferred direction; the wall remains.
_CMD_START = r"(?:^|[\n;&|])\s*"
_BASH_BUILD_PATTERNS = (
    re.compile(_CMD_START + r"git\s+commit\b"),          # a commit
    re.compile(_CMD_START + r"git\s+branch\s+(?!-)\S"),  # branch creation (git branch <name>; not -a/-d/--list)
    re.compile(_CMD_START + r"git\s+checkout\s+-b\b"),   # branch creation
    re.compile(_CMD_START + r"git\s+switch\s+-c\b"),     # branch creation
    re.compile(_CMD_START + r"gh\s+pr\s+create\b"),      # opening a pull request via gh
)

# The GitHub-MCP pull-request-creation tool name(s) (mcp__<server>__create_pull_request and variants).
_MCP_PR_TOOL = re.compile(r"^mcp__.*(create_pull_request|create_pr)\b", re.IGNORECASE)


def is_building_action(tool_name: str, tool_input) -> bool:
    """True iff this tool call is in the enumerated building set the gate denies in Explore. Anything
    NOT recognized as building returns False → ALLOW (the no-default-deny law: an ambiguous action is
    permitted, because the gate is a local nudge, not the wall, and Explore must not be taxed)."""
    if tool_name in _MUTATING_TOOLS:
        return True
    if _MCP_PR_TOOL.match(tool_name or ""):
        return True
    if tool_name in _SHELL_TOOLS:
        command = ""
        if isinstance(tool_input, dict):
            command = tool_input.get("command") or ""
        return any(p.search(command) for p in _BASH_BUILD_PATTERNS)
    return False


# ---- the stance-independent protected-merge nudge -------------------------------------------
# The session never merges the protected branch — that is the operator's own consent act (eADR-0021), and
# an AI performing it would corrupt the very gate the trust model rests on, so eADR-0005's "may hard-fail
# a governance-critical invariant locally" carve-out applies. It is therefore NOT part of the Explore-only
# building set above (which Build/Routine let through): merging is illegitimate in EVERY stance, so this is
# a SEPARATE predicate checked before the stance short-circuit in handler(). Best-effort and fail-open like
# the build patterns — an alias/eval/substitution, or a `gh api graphql` mergePullRequest mutation, evades
# it (stated honestly; the wall is the protected-branch merge itself, never this nudge).
# The REST form is METHOD-ANCHORED on purpose: `GET /repos/{o}/{r}/pulls/{n}/merge` is a merge-STATUS read
# and must NOT be denied; only a write method (PUT, or a body flag) performs the merge — mirroring how
# issue_gate distinguishes a creating call from a reading one, so a read is never taxed. The `gh api`
# pattern is compiled IGNORECASE because `gh` normalises the method's case before sending, so a lowercase
# `-X put` / `--method put` performs a REAL merge and must still fire; an optional surrounding quote on the
# method value is tolerated too. (A quoted-in-a-variable or eval'd form is still the disclosed best-effort
# residual — the wall is the merge itself.)
_MERGE_WRITE_METHOD = (r"(?:-X\s*['\"]?PUT|--method(?:=|\s+)['\"]?PUT"
                       r"|-f\b|-F\b|--field\b|--raw-field\b|--input\b)")
_MERGE_PATTERNS = (
    re.compile(_CMD_START + r"gh\s+pr\s+merge\b"),        # the porcelain merge (incl. --auto scheduling)
    # the REST merge, order-independent: `gh api` at command position AND a pulls/<n>/merge path AND a
    # write method (a bare GET on the same path — the status read — matches neither lookahead → ALLOW):
    re.compile(_CMD_START + r"gh\s+api\b(?=.*pulls/\S+/merge\b)(?=.*" + _MERGE_WRITE_METHOD + r")",
               re.IGNORECASE),
)
# The GitHub-MCP pull-request-merge tool name. An UNVERIFIED build-spec leaf (no in-repo corroboration
# for the exact name): kept narrow so it never catches an unrelated tool, pinned by a test, best-effort,
# verified against current GitHub MCP.
_MCP_MERGE_TOOL = re.compile(r"^mcp__.*merge_pull_request\b", re.IGNORECASE)


def is_merge_action(tool_name: str, tool_input) -> bool:
    """True iff this call attempts to MERGE the protected branch — `gh pr merge`, the REST PUT merge form,
    or the GitHub-MCP merge tool. Stance-INDEPENDENT (the session never merges, in any stance); handler()
    checks it before the Explore short-circuit. Best-effort and fail-open like is_building_action; a
    merge-status GET read on the same path is deliberately NOT matched (see _MERGE_WRITE_METHOD)."""
    if _MCP_MERGE_TOOL.match(tool_name or ""):
        return True
    if tool_name in _SHELL_TOOLS:
        command = ""
        if isinstance(tool_input, dict):
            command = tool_input.get("command") or ""
        return any(p.search(command) for p in _MERGE_PATTERNS)
    return False


# The plain-language merge refusal — "won't" (the session's choice to leave the consent act to the
# operator), NEVER "cannot" (which would dress this fallible local nudge as the wall eADR-0005 forbids).
_MERGE_DENIAL = ("I won't merge that — or schedule a merge of it. Merging the protected branch is your "
                 "consent act, never the session's, in any stance; I open the pull request and stop, and "
                 "you merge it when the evidence convinces you. (This is a nudge, not a wall — it's "
                 "best-effort, so the real guarantee is your own merge, not this refusal.)")


# ---- the plan-mode artifact carve-out -----------------------------------------
# Claude Code's NATIVE plan file (the file the platform writes when a plan is accepted) is *planning,
# not building*, so the gate allows it even though it is a Write/Edit — denying it would regress a
# Claude Code basic the Explore stance exists to support, leaving the non-engineer worse off than plain
# Claude Code. It is recognized by the platform's OWN plan-mode MARKER, NOT a
# path: the plan file's location is operator-configurable (`plansDirectory`) and can resolve INSIDE the
# repo, exactly where a path match would wrongly re-trip the gate. The marker is the session's
# `permission_mode == "plan"` — the signal Claude Code's built-in plan-mode permission itself uses to
# write the file (and `tool_input.is_plan_file`, honored too if a platform sets it). The carve-out is
# the plan artifact SPECIFICALLY: it never exempts a commit/branch/PR, and every other `~/.claude/`
# write (settings, hooks) carries no marker → stays denied (it has no protected-branch merge to back it
# up). The exact field is a build-spec leaf verified against current Claude Code.
_PLAN_MODE = "plan"


def is_plan_artifact(tool_name: str, tool_input, permission_mode, provider: str = "claude") -> bool:
    """True iff this call is Claude Code's plan-mode artifact write: a file-mutating tool while the
    platform reports plan mode (`permission_mode == "plan"`), or a tool_input the platform flags as the
    plan file (`is_plan_file`). Keyed on the marker, never a path. Anything outside plan mode carries no
    marker → not the artifact → stays subject to the gate. PROVIDER-CONFINED: plan mode is Claude
    Code's feature, so on any other runtime this carve-out is inert BY RULE, not by hoping the other
    platform never reuses the field values — a Codex payload reporting `permission_mode: "plan"`
    (its vocabulary is unverified) must not open the Explore write-gate."""
    if provider != "claude" or tool_name not in _MUTATING_TOOLS:
        return False
    if isinstance(tool_input, dict) and tool_input.get("is_plan_file") is True:
        return True
    return permission_mode == _PLAN_MODE


# ---- the harness auto-memory carve-out (StarshipSuperjam/engine-template#766) -----------------------------------------------
# The harness's OWN memory notebook (Claude Code's auto-memory, ~/.claude/projects/<project>/memory/) is
# the session's notebook, not the project — writing it is upkeep of the assistant's own orientation, not
# building — so the gate allows it in Explore. Denying it produced the exact harm StarshipSuperjam/engine-template#766 records: the one
# durable self-store a session has was blocked (and the old relay claimed "saved"), so sessions dumped
# their operating notes into the operator's pin store instead. This is the gate's FIRST path-based allow,
# and it is held to a stricter standard than every matcher above, because unlike the plan file there is
# no platform marker to key on — the path anchor is the ONLY defense — and the surface it opens lies
# OUTSIDE the repo, where no protected-branch merge backstops a mistake. So the predicate is
# filesystem-anchored containment, never a lexical shape:
#   * every path is expanded and REALPATH-resolved (collapsing `..` and any existing symlink) before
#     judgment — a memory-shaped string that RESOLVES elsewhere is not the notebook;
#   * the resolved path must sit strictly INSIDE ~/.claude/projects/<slug>/memory/ — anchored to the real
#     auto-memory location, which structurally excludes worktree checkouts (~/.claude/worktrees/…), any
#     repo-internal `.claude/**/memory/`, and every other `~/.claude/` surface (settings, hooks);
#   * <slug> must be THIS session's own notebook key: the slug the platform derives from the session's
#     working directory — plus, only when the session runs in a platform worktree
#     (<repo>/.claude/worktrees/<wt>), the repo root that worktree belongs to, which is where the
#     platform actually keys the notebook. Never a general ancestor walk: accepting every enclosing
#     directory's slug would let a session reach a parent-directory project's auto-loaded memory (and
#     the universal slug of `/`), exactly the cross-project reach this anchor exists to prevent. One
#     residual is the platform's own: its slug encoding maps both `/` and `.` to `-`, so two paths the
#     PLATFORM already conflates into one notebook (`…/a.b` and `…/a/b`) are conflated here too —
#     an inherited ambiguity, not one this predicate adds;
#   * a batched edit qualifies only when EVERY path it touches qualifies; and ANY failure to resolve —
#     a relative path, a missing cwd, an exception — falls back to DENY. The allow fails CLOSED, the
#     opposite fail-direction from the stance signal (which fails to Explore): an undecidable path keeps
#     the old denial (a cosmetic miss), never an open door.
# Residuals, stated honestly: realpath is time-of-check — a symlink swapped in after the check can still
# redirect the write (the TOCTOU every path gate carries); and a RELOCATED auto-memory directory
# (autoMemoryDirectory) simply misses the anchor and falls back to deny — degraded to the pre-StarshipSuperjam/engine-template#766
# denial, never opened wider. PROVIDER-CONFINED like the plan carve-out: auto-memory is Claude Code's
# feature, so on any other runtime this allow is inert by rule.


def _harness_projects_root() -> str:
    """The real (symlink-resolved) Claude Code projects root — a seam the tests can point elsewhere."""
    return os.path.realpath(os.path.expanduser(os.path.join("~", ".claude", "projects")))


def _project_slug(path: str) -> str:
    """Claude Code's project-directory key for a working directory: the absolute path with `/` and `.`
    each encoded as `-` (verified against the live ~/.claude/projects layout — a build-spec leaf).
    Windows separators are normalized first, matching this file's convention (_is_memory_path); on a
    platform whose paths never match the encoding the predicate simply fails closed."""
    return path.replace("\\", "/").replace("/", "-").replace(".", "-")


def _candidate_slugs(cwd: str) -> set:
    """The notebook keys THIS session may legitimately write: the working directory's own slug, and —
    only when the session runs inside a platform worktree (`<repo>/.claude/worktrees/<wt>`) — the slug
    of the repo root that worktree belongs to (the platform keys the notebook to the repo, not the
    worktree). Both the raw and the realpath-resolved form of each, since the platform keys the slug
    from the path as it saw it. Deliberately NOT an ancestor walk (section comment above)."""
    bases = set()
    for base in {cwd, os.path.realpath(cwd)}:
        bases.add(base)
        norm = base.replace("\\", "/")
        marker = "/.claude/worktrees/"
        idx = norm.find(marker)
        if idx > 0:
            root = base[:idx]
            bases.add(root)
            bases.add(os.path.realpath(root))
    return {_project_slug(b) for b in bases}


def is_harness_memory_write(tool_name: str, tool_input, cwd, provider: str = "claude") -> bool:
    """True iff EVERY path this file-mutating call touches resolves inside THIS project's own harness
    auto-memory notebook (~/.claude/projects/<own slug>/memory/…). The gate's first path-based allow —
    see the section comment for the containment rules; the fail-direction is CLOSED (any doubt → False →
    the write stays denied)."""
    if provider != "claude" or tool_name not in _MUTATING_TOOLS:
        return False
    if not isinstance(tool_input, dict) or not isinstance(cwd, str) or not os.path.isabs(cwd):
        return False
    paths = [tool_input.get("file_path") or tool_input.get("notebook_path") or ""]
    extra = tool_input.get("file_paths")
    if isinstance(extra, list):
        paths += [p for p in extra if isinstance(p, str)]
    paths = [p for p in paths if p]
    if not paths:
        return False
    try:
        root = _harness_projects_root()
        slugs = _candidate_slugs(cwd)
        for p in paths:
            expanded = os.path.expanduser(p)
            if not os.path.isabs(expanded):
                return False
            parts = os.path.relpath(os.path.realpath(expanded), root).split(os.sep)
            # strictly inside <own slug>/memory/: never the notebook dir itself, never a sibling surface,
            # never outside the root (a relpath that climbs starts with "..")
            if len(parts) < 3 or parts[0] == ".." or parts[1] != "memory" or parts[0] not in slugs:
                return False
        return True
    except Exception:  # noqa: BLE001 — an undecidable path fails CLOSED: keep the deny
        return False


# The plain-language denial — names what was blocked AND the concrete way forward, never a silent
# refusal (the stance is always operator-legible).
_DENIAL = ("I didn't make that change — we're exploring, so I won't edit files, commit, create a branch, "
           "or open a pull request yet. (I can still read, run tests, search, and log GitHub issues — "
           "authoring any engine Issue through the issue helper — while we explore; those don't need build.) "
           "Tell me to build it and I'll open a pull request — the change I submit for your approval.")

# The MEMORY-specific denial relay (StarshipSuperjam/engine-template#257, made honest by StarshipSuperjam/engine-template#766). A blocked Write/Edit that targets a
# memory store is NOT a code change the operator must "build" — most often it is the operator asking to
# be REMEMBERED — so the generic _DENIAL ("…open a pull request…") would read as the engine mishearing
# "remember this" as a code change, corrosive to a non-engineer's trust at exactly that moment. The OLD
# relay went further and claimed the content was already "saved to this project's memory"; on a DENIED
# write that claim was false (StarshipSuperjam/engine-template#766 — nothing had been saved), and a session that believed it stopped
# writing the note anywhere. The message (NEVER the decision — the write stays denied) now confirms only
# what is true — nothing was saved — and makes the one promise the assistant can keep: save it properly
# (the pin verb, for an operator ask) and read it back on request. That promise rides the assistant's
# follow-through, stated here honestly as future tense, never as a done deed. It stays in the operator's
# plain words: no store names, no paths, no two-store tour — the ASSISTANT already knows the right doors
# from the Explore-scope briefing (describe_explore_scope); this line is for the person. Fires for ANY
# denied memory-shaped write: a hand-write to `.engine/memory/` and any memory-looking path the notebook
# allow rejected (a relative path, another project's notebook, a link that resolves elsewhere).
_MEMORY_DENIAL = ("Nothing was saved just now — that route into memory is blocked while we explore. If "
                  "you asked me to remember something, I'll save it properly right away, and you can ask "
                  "me anytime what I've remembered and I'll read it back.")


def is_memory_target(tool_name: str, tool_input) -> bool:
    """True iff this file-mutating call targets a MEMORY store — the engine's own `.engine/memory/` or the
    harness auto-memory notebook (the `~/.claude/.../memory/` default shape). MESSAGE-CHOICE ONLY: it never
    changes the gate's decision — it only selects, on a write the gate has ALREADY denied, the
    memory-specific relay (StarshipSuperjam/engine-template#257); the harness-notebook ALLOW is a separate, stricter predicate
    (is_harness_memory_write), never this one. Recognizing the memory path *for message choice* is safe —
    the allow-exemption hazard a path match raises does not apply here, so a relocated `autoMemoryDirectory`
    this misses simply falls back to the generic denial (cosmetic). It never hardcodes a platform-owned basename: it matches the engine store
    deterministically (`.engine/memory/`, NOT the `.engine/tools/memory/` source dir) and the harness store
    by path SHAPE (a `memory` directory nested under a `.claude` directory)."""
    if tool_name not in _MUTATING_TOOLS:
        return False
    paths = []
    if isinstance(tool_input, dict):
        paths = [tool_input.get("file_path") or tool_input.get("notebook_path") or ""]
        # A normalized multi-file edit (Codex's batch apply_patch) carries every touched path in
        # file_paths — a memory store among ANY of them selects the memory-specific relay.
        extra = tool_input.get("file_paths")
        if isinstance(extra, list):
            paths += [p for p in extra if isinstance(p, str)]
    return any(_is_memory_path(p) for p in paths if isinstance(p, str) and p)


def _is_memory_path(path: str) -> bool:
    norm = path.replace("\\", "/")
    if ".engine/memory/" in norm:                 # the engine's own substrate (excludes .engine/tools/memory/)
        return True
    if ".engine/" in norm:                        # any OTHER engine repo file is source/data, never a store —
        return False                              #   guards the worktree case (…/.claude/worktrees/<wt>/.engine/…)
    seen_claude = False                           # the harness auto-memory default shape: a `memory` dir
    for seg in (p for p in norm.split("/") if p): #   nested somewhere under a `.claude` dir (outside the repo)
        if seg == ".claude":
            seen_claude = True
        elif seg == "memory" and seen_claude:
            return True
    return False


# ---- the PreToolUse write-gate handler ------------------------------------------------------

def handler(payload: dict) -> dict:
    """The PreToolUse gate, run on every tool call (broad matcher). It composes THREE decisions in one
    reviewable place: two STANCE-INDEPENDENT denies checked first — the engine-Issue conformance reroute
    (matcher in issue_gate) and the protected-merge nudge (the session never merges, in any stance) — and
    then the Explore write-gate (stance-dependent — Build/Routine permit the write; Explore denies a
    building action and allows everything else). Either deny
    rides the structured permissionDecision channel (hooks.decide → exit 0 + hookSpecificOutput), which the
    platform honors AND feeds back to the session as the reason; exit-2 block() would be read as a crash and the
    deny — and its redirect reason — dropped."""
    tool_name = payload.get("tool_name", "") if isinstance(payload, dict) else ""
    tool_input = payload.get("tool_input") if isinstance(payload, dict) else None
    # The engine-Issue reroute — fires in Explore AND Build (the channel rule is unconditional), so it is
    # checked before the stance short-circuit. issue_gate holds the matcher; here we wrap its reason. It now
    # reroutes EVERY direct engine-labelled creation (Bash/API/connector) to the helper's create CLI.
    reroute = issue_gate.reroute_reason(tool_name, tool_input)
    if reroute is not None:
        return hooks.decide("deny", reroute)
    # The protected-merge nudge — also STANCE-INDEPENDENT (the session never merges the protected branch in
    # any stance; that is the operator's consent act), so likewise checked before the stance short-circuit.
    if is_merge_action(tool_name, tool_input):
        return hooks.decide("deny", _MERGE_DENIAL)
    session_id = payload.get("session_id") if isinstance(payload, dict) else None
    if current_stance(session_id) != EXPLORE:
        return hooks.proceed()                       # Build / Routine permit the write
    permission_mode = payload.get("permission_mode") if isinstance(payload, dict) else None
    import providers  # lazy: keep modes importable stand-alone in tests that stub the seam
    provider = providers.detect(payload)
    if is_building_action(tool_name, tool_input) \
            and not is_plan_artifact(tool_name, tool_input, permission_mode, provider) \
            and not is_harness_memory_write(tool_name, tool_input,
                                            payload.get("cwd") if isinstance(payload, dict) else None,
                                            provider):
        # Same DECISION (deny) for everything still in the building set; only the relayed reason differs —
        # a denied memory-shaped write earns the honest memory line (StarshipSuperjam/engine-template#257/StarshipSuperjam/engine-template#766), every other write the
        # generic build-set denial.
        reason = _MEMORY_DENIAL if is_memory_target(tool_name, tool_input) else _DENIAL
        return hooks.decide("deny", reason)
    return hooks.proceed()      # reads, tests, greps, an unlabelled/conforming gh issue, subagents, the plan file


# ---- the plan-acceptance Build-entry trigger ----------------------------------
# The SECOND interactive way into Build (the first is the operator-typed verb): when the
# operator ACCEPTS a plan, Claude Code's plan-exit completion — the `ExitPlanMode` tool call — fires a
# PostToolUse hook, and the engine flips the stance signal to Build. "Approving a plan is 'build it'",
# with no verb to type. Keyed on the completion EVENT itself
# (tool_name == "ExitPlanMode"), NOT a permission_mode value — acceptance offers several target modes,
# so the durable discriminator is that the completion fired. A REJECTED plan fires no PostToolUse, so it
# never enters Build; the model cannot accept its own plan, so this is not self-electable.
#
# It SETS THE SIGNAL AND INJECTS A TERSE ASSISTANT-INTERNAL STANCE DIRECTIVE: current Claude Code delivers a
# PostToolUse hook's additionalContext to the model (correcting the earlier, falsified "a PostToolUse
# hook cannot inject conversational text" claim), so the entry PUSHES a directive that names the new stance
# and triggers build-orchestration's kickoff ("opening a draft pull request and planning the work") — rather
# than relying on the model to override its stale start-of-session Explore briefing from memory. The OPERATOR
# announcement stays build-orchestration's, exactly once: the injected line is do-not-relay machine context,
# never an operator announcement. The SIGNAL is the sole durable record and the line is strictly advisory
# (the inject is gated on the flip succeeding), so there is no partial-failure split-brain, and a line
# replayed over a SessionStart-cleared signal on resume is inert (the live-signal re-read guard returns it to
# Explore). It ALWAYS proceeds — PostToolUse is non-block-eligible (the harness fails open on a block/decide
# there), so it declares no BLOCK_INVARIANT and the block budget is untouched. FAIL-SAFE: if the hook errors
# or never fires — including accept-with-clear-context, which does not fire (claude-code#20397) — the signal
# stays absent → Explore, never Build (the safe floor; the operator-typed verb is the recovery path).
_PLAN_EXIT_TOOL = "ExitPlanMode"


def _build_entry_directive() -> str:
    """The ASSISTANT-FACING stance directive injected on plan-acceptance. It NAMES the new Build
    stance and directs THIS turn into build-orchestration's kickoff — a push, so the session stops acting on
    its stale start-of-session Explore briefing. It is a TURN-LOCAL directive, never a durable stance record:
    the stance SIGNAL is the sole durable record (cleared to Explore at every SessionStart), so a copy of this
    line replayed on a resumed session is inert — the live-signal re-read guard sends a cleared-stance session
    back to Explore. It carries NO operator-facing copy, is self-labelled do-not-relay, and carries no
    imperative relay marker — the operator meets Build-entry once, through the kickoff, never through this note.
    A fidelity test (test_modes) pins it to _STANCE_LINES[BUILD] and to the do-not-relay / no-marker laws."""
    return (
        "Your stance just changed to Build — the operator accepted a plan. "
        f"{_STANCE_LINES[BUILD]} "
        "This note is for you, not the operator: don't relay it. The operator meets this entry once, through "
        "your build-orchestration kickoff (opening a draft pull request and planning the work) — and, before "
        "you change anything, that kickoff shows the operator the risk assessment and gets their how-careful "
        "depth choice. Go do that now. Before you act, confirm your live stance still reads Build — run "
        "`python tools/modes.py stance`. "
        "If it reads Explore instead, ignore this note and stay in Explore: do not open the kickoff. The live "
        "stance governs, never this note."
    )


def accept_handler(payload: dict) -> dict:
    """The plan-acceptance Build-entry trigger, run on PostToolUse. On the plan-exit completion
    (`ExitPlanMode`), set the session's stance to Build AND inject a terse assistant-internal stance directive
    that triggers build-orchestration's kickoff; on anything else, no-op. The inject is GATED on
    the durable flip succeeding — the SIGNAL is the sole durable record, the injected line strictly advisory —
    so a bad/sessionless payload (set_stance returns False) proceeds with no inject and no split-brain. ALWAYS
    proceeds — never blocks. A rejected plan fires no PostToolUse → the stance stays Explore (the safe floor)."""
    if isinstance(payload, dict) and payload.get("tool_name") == _PLAN_EXIT_TOOL:
        if set_stance(payload.get("session_id"), BUILD):
            return hooks.inject(_build_entry_directive())
    return hooks.proceed()


# ---- the CLI (the operator-runnable demo; the live gate is the wired hook) -------------------

def _arg(argv: list, flag: str) -> str | None:
    """The value following `flag` in argv, or None."""
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


def _resolve_session(argv: list) -> str | None:
    """The session id for a CLI stance change: the explicit `--session` value, else the provider
    seam's resolution chain (providers.resolve_session — the neutral override env var, then the
    platform session var, then the live-session marker boot writes, which is how a typed Codex verb
    with no session env var still finds its session; the marker refuses on any ambiguity). The
    Claude Build verb's skill body passes `--session "${CLAUDE_CODE_SESSION_ID}"`, which the shell
    expands from that env var; if it arrives empty or unexpanded (a literal `${...}`), the chain
    takes over. A session the chain cannot identify degrades SAFE — set_stance returns False and
    the stance stays explore."""
    import providers  # lazy: keep modes importable stand-alone in tests that stub the seam
    return providers.resolve_session(explicit=_arg(argv, "--session"))


def _decision_line(decision: dict) -> str:
    """Render a handler decision as a one-line operator-facing verdict for the demo."""
    if decision.get("action") == "decide" and decision.get("permissionDecision") == "deny":
        return f"DENY — {decision.get('reason')}"
    return "ALLOW"


def _classify(argv: list) -> int:
    """`classify <Tool> [command...] [--session S] [--pm MODE] [--plan-file]` — run the REAL handler over
    a synthetic payload and print what the gate decides, so the operator can vary the tool/command/mode
    and confirm the behavior (e.g. a Write under `--pm plan` is the plan artifact → ALLOW; the same write
    without it → DENY in Explore)."""
    session = _arg(argv, "--session")
    pm = _arg(argv, "--pm")
    plan_file = "--plan-file" in argv
    skip = {"--session", session, "--pm", pm, "--plan-file"}
    rest = [a for a in argv if a not in skip]
    if not rest:
        print("usage: modes.py classify <Tool> [command] [--session S] [--pm MODE] [--plan-file]",
              file=sys.stderr)
        return 2
    tool_name = rest[0]
    command = " ".join(rest[1:])
    tool_input = {}
    if command:
        tool_input["command"] = command
    if plan_file:
        tool_input["is_plan_file"] = True
    payload = {"session_id": session, "tool_name": tool_name,
               "tool_input": tool_input, "permission_mode": pm}
    decision = handler(payload)
    stance = current_stance(session)
    print(f"stance={stance}  tool={tool_name!r}  command={command!r}  permission_mode={pm!r}"
          f"{'  is_plan_file=True' if plan_file else ''}")
    print(f"  -> {_decision_line(decision)}")
    return 0


def _demo(_argv: list) -> int:
    """A scripted fail-then-pass demonstration over the REAL handlers (only the session id is a fixture):
    the Explore write-gate, the plan-mode carve-out (StarshipSuperjam/engine-template#64), and the plan-acceptance Build-entry (StarshipSuperjam/engine-template#67)."""
    sid = "engine-demo-session"
    clear_stance(sid)

    def gate(tool, cmd="", pm=None, tool_input=None):
        ti = dict(tool_input or {})
        if cmd:
            ti["command"] = cmd
        return handler({"session_id": sid, "tool_name": tool, "tool_input": ti, "permission_mode": pm})

    print("The Explore write-gate — what it decides for each action (this runs the real gate, not a "
          "mock-up):\n")
    print(f"In EXPLORE (stance={current_stance(sid)}): building actions denied, everything else allowed:")
    for label, tool, cmd in [("edit a file", "Edit", ""), ("write a file", "Write", ""),
                             ("commit", "Bash", "git commit -m wip"), ("open a PR", "Bash", "gh pr create"),
                             ("run a test", "Bash", "pytest -q"), ("log an issue", "Bash", "gh issue create -t x"),
                             ("read a file", "Read", "")]:
        print(f"  {label:42} {tool:5} -> {_decision_line(gate(tool, cmd))}")

    print("\nThe plan-file carve-out (#64) — Claude Code's own plan file is planning, not building, so it "
          "is allowed (recognized by the platform's own plan-mode signal, never the folder location):")
    for label, pm, ti in [
            ("the plan file, saved while in plan mode",       "plan",    None),
            ("the plan file, with its folder moved INTO repo", "plan",   {"file_path": ".engine/plans/x.md"}),
            ("the plan file, flagged as such by the platform", None,     {"is_plan_file": True}),
            ("a NON-plan write to ~/.claude/settings.json",   "default", {"file_path": "~/.claude/settings.json"})]:
        print(f"  {label:49} Write -> {_decision_line(gate('Write', pm=pm, tool_input=ti))}")

    print("\nAccepting a plan enters Build AND pushes you a stance directive (#67) — real trigger:")
    print(f"  before:                                  stance={current_stance(sid)}")
    d_other = accept_handler({"session_id": sid, "tool_name": "SomeOtherTool"})
    print(f"  some other action finishes ->            stance={current_stance(sid)} "
          f"(unchanged; hook action={d_other.get('action')} — only accepting a plan enters Build)")
    d_accept = accept_handler({"session_id": sid, "tool_name": _PLAN_EXIT_TOOL})
    built = current_stance(sid)
    directive = d_accept.get("context", "")
    print(f"  accepting a plan ->                      stance={built} (hook action={d_accept.get('action')})")
    print("  the directive it injected to YOU (do-not-relay, names Build, triggers the kickoff):")
    print(f"    {directive[:92]}…")
    e_build = _decision_line(gate('Edit'))
    print(f"  the SAME edit denied above is now ->     {e_build} (the real capability, not just the label)")

    # Resume inertness: SessionStart clears the signal; a replayed directive is then inert — the LIVE signal
    # reads Explore, so the gate denies and the assistant reports Explore (never the replayed line).
    cleared = clear_stance(sid)
    print(f"\nResume — SessionStart clears the signal (the directive may be replayed, but is NOT re-run): "
          f"clear_stance -> {cleared}")
    stance_after_clear = current_stance(sid)
    e_explore = _decision_line(gate('Edit'))
    print(f"  live stance now ->                       {stance_after_clear} (you report THIS, not the line)")
    print(f"  a replayed 'you are in Build' directive is inert: the same Edit is ->  {e_explore}")
    print("\nThe gate is a nudge, not a wall — a disguised verb slips it, a crash fails it open; the "
          "merge wall is the guarantee. Accepting a plan enters Build (human-gated, not a stronger gate) and "
          "pushes you a do-not-relay stance directive; the OPERATOR announcement stays the build-orchestration "
          "kickoff, exactly once. (The platform delivering that directive on PostToolUse is the inductive "
          "ceiling a fixture can't discharge — verified against current Claude Code.)")
    # Self-check: accept SETS Build AND injects a directive that names Build and is do-not-relay; a non-accept
    # completion proceeds with no inject; clearing the signal returns to Explore (the replayed line is inert —
    # the live signal, not the line, governs).
    ok = ("build" in str(built).lower() and "explore" in str(stance_after_clear).lower()
          and "allow" in e_build.lower() and ("den" in e_explore.lower())
          and d_accept.get("action") == "inject" and _STANCE_LINES[BUILD] in directive
          and "don't relay" in directive.lower() and d_other.get("action") == "proceed")
    if not ok:
        print("\nDEMO UNEXPECTED: the Explore->Build->Explore transitions, the injected stance directive, or "
              "the Edit-gate decisions did not behave as expected.", file=sys.stderr)
        return 1
    return 0


def main(argv: list) -> int:
    cmd = argv[0] if argv else "hook"
    if cmd == "hook":
        # Hook mode: what the wired PreToolUse hook invokes. run_hook reads the event JSON from stdin,
        # runs the gate, and translates decide(deny) -> structured stdout, fail-open on any error.
        return hooks.run_hook("PreToolUse", handler)
    if cmd == "accept-hook":
        # Hook mode: what the wired PostToolUse hook invokes. On a plan-exit completion it sets Build;
        # otherwise a no-op. Always proceeds (PostToolUse never blocks); fail-open on any error.
        return hooks.run_hook("PostToolUse", accept_handler)
    if cmd == "classify":
        return _classify(argv[1:])
    if cmd == "stance":
        # Resolve the session id like set-build/clear (explicit --session else $CLAUDE_CODE_SESSION_ID), so
        # a bare `modes.py stance` self-check reports the REAL stance instead of the safe-default `explore`
        # it would print with no session. Unresolvable → say so + non-zero, never a confident false `explore`.
        session = _resolve_session(argv)
        if not session:
            print("unknown (no session id resolvable)")
            return 1
        print(current_stance(session))
        return 0
    if cmd == "set-build":
        result = set_stance(_resolve_session(argv), BUILD)
        print("set Build: True" if result else _stance_write_failure("Build", result))
        return 0 if result else 1
    if cmd == "set-routine":
        # The unattended Routine stance-entry — run by the operator-authored scheduled fire through the
        # engine-routine skill (which carries the operator-only flag), never the model on its own. Unlike
        # set-build it grants the write stance ONLY on POSITIVE proof of worktree isolation: a scheduled run
        # that mutated the operator's own checkout is the never-strand-main harm, so any inability to confirm
        # isolation declines (stays Explore) and the run reports why rather than writing.
        import checkout_health  # lazy: keep modes importable stand-alone (mirrors the providers seam import)
        session = _resolve_session(argv)
        if not session:
            print("set Routine: False (no session id resolvable)")
            return 1
        if not checkout_health.is_isolated_worktree():
            print("set Routine: False (not a dedicated worktree — a routine writes only in an isolated "
                  "worktree, never the operator's checkout)")
            return 1
        result = set_stance(session, ROUTINE)
        print("set Routine: True" if result else _stance_write_failure("Routine", result))
        return 0 if result else 1
    if cmd == "clear":
        ok = clear_stance(_arg(argv, "--session"))
        print(f"cleared: {ok}")
        return 0 if ok else 1
    if cmd == "demo":
        return _demo(argv[1:])
    print("usage: modes.py [hook | accept-hook | classify <Tool> [cmd] [--session S] [--pm MODE] "
          "[--plan-file] | stance [--session S] | set-build [--session S] | set-routine [--session S] | "
          "clear --session S | demo]  (stance/set-build/set-routine resolve the session from --session else "
          "$CLAUDE_CODE_SESSION_ID; set-routine also requires a dedicated worktree)",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
