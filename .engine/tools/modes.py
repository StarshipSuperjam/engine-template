#!/usr/bin/env python3
"""Slice 21 — modes: the operating stance + the Explore write-gate (the M1 write-gate).

The session's operating STANCE is what it may do, and whether a human is present to answer for it
(systems/lifecycle/modes/README.md). Three stances on two axes:
  - explore (default, interactive, writes gated OFF) — every session boots here;
  - build   (interactive, writes on) — entered by a typed verb (slice 26) OR by accepting a plan;
  - routine (unattended, scope-locked, writes on) — entered by an operator-authored scheduled fire.

This module ships THREE things (the operator-typed Build verb and Routine entry are later — slice 26 /
post-core):

  1. THE STANCE SIGNAL — an ephemeral, session-keyed marker in OS-temp storage, never committed and
     never carried across sessions. It is set only by a deliberate in-session entry, and CLEARED at
     every SessionStart (boot calls clear_stance first, so a resumed Build session never resurrects as
     Build). When the signal is absent, stale, or unreadable, the stance is explore: the safe default is
     the floor, never the ceiling (modes/README §"Stance is session-scoped and never persists").

  2. THE EXPLORE WRITE-GATE — a PreToolUse hook, active only while the stance is explore, that DENIES the
     small enumerated set that BEGINS building — edits to engine or product files, branch creation,
     commits, and the opening of a pull request (via gh or a GitHub MCP tool) — and ALLOWS everything
     else: reads, read-only command/test execution, greps, subagent spawning, `gh issue` calls, AND
     Claude Code's own plan-mode artifact (the plan file is planning, not building — D-177/D-178; see
     is_plan_artifact, recognized by the platform's own marker, never a path). There is NO default-deny:
     an action it cannot classify resolves to ALLOW (modes/README §"Explore").

  3. THE PLAN-ACCEPTANCE BUILD-ENTRY TRIGGER — a PostToolUse hook (accept_handler) that flips the stance
     to Build when the operator accepts a plan (the plan-exit `ExitPlanMode` completion). The second
     interactive entry path alongside the slice-26 verb; it sets the signal and nothing else, never
     blocks, and fails safe to explore (D-179/D-180; modes/README §"Entering Build").

THE GATE IS A §6 NUDGE, NOT A WALL — stated honestly, never overstated (modes/README §"the gate is a
strong default, and its enforcement is fallible"; D-171). The gate emits its deny in the form the platform
acts on — exit 0 + a hookSpecificOutput-wrapped permissionDecision (hooks.decide), the path the engine
uses (hooks/README), which the current platform honors across built-in AND GitHub-MCP tools; it never uses
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hooks  # noqa: E402  (run_hook + decide/proceed: the fail-open harness the gate rides)


# ---- the three stances (modes/README §"Operating modes") ------------------------------------
EXPLORE = "explore"
BUILD = "build"
ROUTINE = "routine"
STANCES = frozenset({EXPLORE, BUILD, ROUTINE})

# The block this owning system declares for the hook block budget. hooks.py "names no invariant
# itself", so the consumer (module_coherence.block_eligible_registrations) assembles the registry
# from each owner's declaration; the validator reads only `event`. PreToolUse is block-eligible.
BLOCK_INVARIANT = {"event": "PreToolUse", "name": "explore-write-gate", "owner": "modes"}


# ---- the stance signal: ephemeral, session-keyed, OS-temp, non-committed --------------------
# A session_id-keyed marker in OS-temp storage (a build-spec leaf settled here). NON-committed, never
# read across sessions, no repo footprint. Cleared at every SessionStart; resolves to explore when
# absent / stale / unreadable. The gate reads it from the session id the platform supplies.
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


def set_stance(session_id: str | None, stance: str) -> bool:
    """Set the session's stance signal. Callers: the plan-acceptance trigger (accept_handler, this slice),
    the operator-typed Build verb (slice 26), and the demo/tests. Setting EXPLORE clears the marker
    (explore is the absence of a signal). Returns True on success, False when there is no usable session
    id or the write fails; never raises."""
    if stance == EXPLORE:
        return clear_stance(session_id)
    if stance not in STANCES:
        raise ValueError(f"unknown stance {stance!r}; expected one of {sorted(STANCES)}")
    path = _signal_path(session_id)
    if not path:
        return False
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(stance)
        return True
    except Exception:  # noqa: BLE001 — a failed write degrades to "no signal" → explore, never a crash
        return False


def clear_stance(session_id: str | None) -> bool:
    """Delete the session's stance marker → the session resolves to EXPLORE. Idempotent (a missing
    marker is success) and never raises. Boot calls this FIRST at every SessionStart so a resumed
    session never inherits a prior Build signal (the resume-safety guarantee)."""
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


# ---- operator-legible stance copy (modes owns the stance vocabulary; boot/README §Seams) ----
_STANCE_LINES = {
    EXPLORE: "Exploring — I won't change files or open a pull request until you tell me to build.",
    BUILD: "Building — I'll make changes and submit them as a pull request for your approval.",
    ROUTINE: "Running unattended (routine) — scope-locked build work; nothing merges without review.",
}


def describe_stance(stance: str) -> str:
    """The plain-language one-line description of a stance — modes owns this vocabulary; boot places it
    in the orientation card (boot/README line 218). An unknown stance falls back to the explore line."""
    return _STANCE_LINES.get(stance, _STANCE_LINES[EXPLORE])


def describe_explore_scope() -> str:
    """The ASSISTANT-FACING scope of the Explore write-gate — what it ALLOWS and DENIES, in plain words, so
    a session knows its own structure and does not over-restrict itself (e.g. switch to Build merely to log
    a GitHub issue, which Explore already allows). This is for the MODEL's grounding, NOT the operator: boot
    places it in the AI-facing briefing, never the operator dashboard, and it is self-labelled "don't relay
    this" so it cannot leak into the operator-presentation relay.

    Explore-ONLY by design: boot clears the stance to Explore at every SessionStart (boot.handler), so the
    briefing that carries this note is always an Explore session — Build/Routine never receive a fresh boot
    pack, so a per-stance variant would be copy that is never surfaced. The allow/deny wording here MUST
    track is_building_action / _MUTATING_TOOLS / _BASH_BUILD_PATTERNS; a fidelity test (test_modes) pins the
    prose to that set so the two cannot drift."""
    return (
        "How your Explore stance actually works (for you — don't relay this; it's about how your own "
        "session is wired, not a status update for the operator). Right now, WITHOUT entering Build, you "
        "may: read files; run tests and other read-only commands; search the codebase; spawn subagents; "
        "write Claude Code's plan file; and log GitHub issues (`gh issue create`). You may NOT, until the "
        "operator tells you to build: edit or write any files, create a branch, commit, or open a pull "
        "request. So don't switch to Build just to log an issue or read around — those are allowed in "
        "Explore. (The gate is a strong default, not a wall; the real guarantee is that nothing reaches the "
        "main branch without a pull-request review.)"
    )


# ---- the denied-action match list (a build-spec leaf, settled here) -------------------------
# The small enumerated set that BEGINS building (modes/README §"Explore"): file edits, branch creation,
# commits, and opening a pull request. `git push` is deliberately NOT here — the source enumerates these
# four, and Explore must stay the comfortable place to work (no default-deny, nothing else taxed).
# The plain-language, assistant-facing rendering of THIS allow/deny split lives in describe_explore_scope();
# a fidelity test (test_modes) pins that prose to this set — change the two together, never one alone.
_MUTATING_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})

# Best-effort shell building-verb patterns over the Bash command string. Best-effort by construction:
# a verb behind an alias / eval / substitution / chaining evades these (modes/README, stated honestly).
# Each verb must appear at COMMAND POSITION — the start of the command, or just after a shell separator
# (newline ; & |) — so an occurrence inside a quoted argument or an echoed/grepped string (e.g.
# `echo 'git commit'`) does NOT trip a false deny. This errs toward ALLOW, as the source requires (§"no
# default-deny": don't tax Explore), at the cost of missing prefixed forms (`time git commit`, a subshell,
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
    if tool_name == "Bash":
        command = ""
        if isinstance(tool_input, dict):
            command = tool_input.get("command") or ""
        return any(p.search(command) for p in _BASH_BUILD_PATTERNS)
    return False


# ---- the plan-mode artifact carve-out (D-177/D-178) -----------------------------------------
# Claude Code's NATIVE plan file (the file the platform writes when a plan is accepted) is *planning,
# not building*, so the gate allows it even though it is a Write/Edit — denying it would regress a
# Claude Code basic the Explore stance exists to support, leaving the non-engineer worse off than plain
# Claude Code (modes/README §"Explore"). It is recognized by the platform's OWN plan-mode MARKER, NOT a
# path: the plan file's location is operator-configurable (`plansDirectory`) and can resolve INSIDE the
# repo, exactly where a path match would wrongly re-trip the gate. The marker is the session's
# `permission_mode == "plan"` — the signal Claude Code's built-in plan-mode permission itself uses to
# write the file (and `tool_input.is_plan_file`, honored too if a platform sets it). The carve-out is
# the plan artifact SPECIFICALLY: it never exempts a commit/branch/PR, and every other `~/.claude/`
# write (settings, hooks) carries no marker → stays denied (it has no protected-branch merge to back it
# up). The exact field is a build-spec leaf verified against current Claude Code (D-178).
_PLAN_MODE = "plan"


def is_plan_artifact(tool_name: str, tool_input, permission_mode) -> bool:
    """True iff this call is Claude Code's plan-mode artifact write: a file-mutating tool while the
    platform reports plan mode (`permission_mode == "plan"`), or a tool_input the platform flags as the
    plan file (`is_plan_file`). Keyed on the marker, never a path. Anything outside plan mode carries no
    marker → not the artifact → stays subject to the gate."""
    if tool_name not in _MUTATING_TOOLS:
        return False
    if isinstance(tool_input, dict) and tool_input.get("is_plan_file") is True:
        return True
    return permission_mode == _PLAN_MODE


# The plain-language denial — names what was blocked AND the concrete way forward, never a silent
# refusal (modes/README §"The stance is always operator-legible").
_DENIAL = ("I didn't make that change — we're exploring, so I won't edit files, commit, create a branch, "
           "or open a pull request yet. (I can still read, run tests, search, and log GitHub issues while "
           "we explore — those don't need build.) Tell me to build it and I'll open a pull request — the "
           "change I submit for your approval.")


# ---- the PreToolUse write-gate handler ------------------------------------------------------

def handler(payload: dict) -> dict:
    """The Explore write-gate, run on every tool call (broad matcher; the decision logic lives here in
    one reviewable place, per hooks/README). In Build or Routine it permits the write; in Explore it
    denies a building action with the plain sentence and allows everything else. The deny rides the
    structured permissionDecision channel (hooks.decide → exit 0 + hookSpecificOutput), which the
    platform honors; exit-2 block() would be read as a crash and the deny dropped."""
    session_id = payload.get("session_id") if isinstance(payload, dict) else None
    if current_stance(session_id) != EXPLORE:
        return hooks.proceed()                       # Build / Routine permit the write
    tool_name = payload.get("tool_name", "") if isinstance(payload, dict) else ""
    tool_input = payload.get("tool_input") if isinstance(payload, dict) else None
    permission_mode = payload.get("permission_mode") if isinstance(payload, dict) else None
    if is_building_action(tool_name, tool_input) and not is_plan_artifact(tool_name, tool_input, permission_mode):
        return hooks.decide("deny", _DENIAL)
    return hooks.proceed()                # reads, tests, greps, gh issue, subagents, the plan file — allowed


# ---- the plan-acceptance Build-entry trigger (D-179/D-180) ----------------------------------
# The SECOND interactive way into Build (the first is the operator-typed verb, slice 26): when the
# operator ACCEPTS a plan, Claude Code's plan-exit completion — the `ExitPlanMode` tool call — fires a
# PostToolUse hook, and the engine flips the stance signal to Build. "Approving a plan is 'build it'",
# with no verb to type (modes/README §"Entering Build"). Keyed on the completion EVENT itself
# (tool_name == "ExitPlanMode"), NOT a permission_mode value — acceptance offers several target modes,
# so the durable discriminator is that the completion fired. A REJECTED plan fires no PostToolUse, so it
# never enters Build; the model cannot accept its own plan, so this is not self-electable.
#
# It SETS THE SIGNAL AND NOTHING ELSE: a PostToolUse hook cannot inject conversational text, so the
# entry is announced by build-orchestration's kickoff ("opening a draft pull request and planning the
# work"), not here. It ALWAYS proceeds — PostToolUse is non-block-eligible (the harness fails open on a
# block/decide there), so it declares no BLOCK_INVARIANT and the block budget is untouched. FAIL-SAFE:
# if the hook errors or never fires, the signal stays absent → Explore, never Build (the safe floor).
_PLAN_EXIT_TOOL = "ExitPlanMode"


def accept_handler(payload: dict) -> dict:
    """The plan-acceptance Build-entry trigger, run on PostToolUse. On the plan-exit completion
    (`ExitPlanMode`), set the session's stance to Build; on anything else, no-op. ALWAYS proceeds —
    never blocks, never emits text. A non-ExitPlanMode completion (or a missing tool name) leaves the
    stance untouched, and a rejected plan fires no PostToolUse at all → the stance stays Explore."""
    if isinstance(payload, dict) and payload.get("tool_name") == _PLAN_EXIT_TOOL:
        set_stance(payload.get("session_id"), BUILD)
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
    """The session id for a CLI stance change: the explicit `--session` value, else the platform's
    `CLAUDE_CODE_SESSION_ID` environment variable. The operator-typed Build verb's skill body passes the
    documented `${CLAUDE_SESSION_ID}` content token; if a platform leaves it empty or unexpanded (a
    literal `${...}`), fall back to the env var so the verb still resolves the real session. A session
    that supplies neither degrades SAFE — set_stance returns False and the stance stays explore."""
    session = _arg(argv, "--session")
    if not session or "${" in session:
        session = os.environ.get("CLAUDE_CODE_SESSION_ID")
    return session


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
    the Explore write-gate, the plan-mode carve-out (#64), and the plan-acceptance Build-entry (#67)."""
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

    print("\nAccepting a plan enters Build (#67) — this runs the real trigger:")
    print(f"  before:                                  stance={current_stance(sid)}")
    accept_handler({"session_id": sid, "tool_name": "SomeOtherTool"})
    print(f"  some other action finishes ->            stance={current_stance(sid)} (unchanged — only "
          f"accepting a plan enters Build)")
    accept_handler({"session_id": sid, "tool_name": _PLAN_EXIT_TOOL})
    print(f"  accepting a plan ->                      stance={current_stance(sid)}")
    print(f"  the SAME edit denied above is now ->     {_decision_line(gate('Edit'))} "
          f"(the real capability, not just the label)")

    print(f"\nClear the signal (what boot does at SessionStart): clear_stance -> {clear_stance(sid)}")
    print(f"Back in EXPLORE (stance={current_stance(sid)}): an Edit is denied again -> "
          f"{_decision_line(gate('Edit'))}")
    print("\nThe gate is a §6 nudge, not a wall — a disguised verb slips it, a crash fails it open; the "
          "merge wall is the guarantee. Accepting a plan enters Build (human-gated, not a stronger gate); "
          "the entry is announced as the build begins, not by the (silent) hook.")
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
        print(current_stance(_arg(argv, "--session")))
        return 0
    if cmd == "set-build":
        ok = set_stance(_resolve_session(argv), BUILD)
        print(f"set Build: {ok}")
        return 0 if ok else 1
    if cmd == "clear":
        ok = clear_stance(_arg(argv, "--session"))
        print(f"cleared: {ok}")
        return 0 if ok else 1
    if cmd == "demo":
        return _demo(argv[1:])
    print("usage: modes.py [hook | accept-hook | classify <Tool> [cmd] [--session S] [--pm MODE] "
          "[--plan-file] | stance --session S | set-build --session S | clear --session S | demo]",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
