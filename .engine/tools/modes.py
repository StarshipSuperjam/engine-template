#!/usr/bin/env python3
"""Slice 21 — modes: the operating stance + the Explore write-gate (the M1 write-gate).

The session's operating STANCE is what it may do, and whether a human is present to answer for it
(systems/lifecycle/modes/README.md). Three stances on two axes:
  - explore (default, interactive, writes gated OFF) — every session boots here;
  - build   (interactive, writes on) — entered by a deliberate operator-typed verb (slice 26);
  - routine (unattended, scope-locked, writes on) — entered by an operator-authored scheduled fire.

This module ships TWO things (the Build-entry verb and Routine entry are later — slice 26 / post-core):

  1. THE STANCE SIGNAL — an ephemeral, session-keyed marker in OS-temp storage, never committed and
     never carried across sessions. It is set only by a deliberate in-session entry, and CLEARED at
     every SessionStart (boot calls clear_stance first, so a resumed Build session never resurrects as
     Build). When the signal is absent, stale, or unreadable, the stance is explore: the safe default is
     the floor, never the ceiling (modes/README §"Stance is session-scoped and never persists").

  2. THE EXPLORE WRITE-GATE — a PreToolUse hook, active only while the stance is explore, that DENIES the
     small enumerated set that BEGINS building — edits to engine or product files, branch creation,
     commits, and the opening of a pull request (via gh or a GitHub MCP tool) — and ALLOWS everything
     else: reads, read-only command/test execution, greps, subagent spawning, and `gh issue` calls.
     There is NO default-deny: an action it cannot classify resolves to ALLOW (modes/README §"Explore").

THE GATE IS A §6 NUDGE, NOT A WALL — stated honestly, never overstated (modes/README §"the gate is a
strong default, and its enforcement is fallible"). The current platform DOES honor a PreToolUse deny
emitted as the engine's format (exit 0 + a hookSpecificOutput-wrapped permissionDecision — hooks.decide,
which is why the gate uses decide() and never exit-2 block(), which the platform reads as a CRASH). But:
detecting a build-by-`git`/`gh` in a shell string is best-effort (aliases / eval / substitution / chaining
evade it); the hooks fail-open law means a crashing gate lets the action through; and an operator who
allow-lists a gated tool in settings.json disarms the gate (allow > hook). The only unbypassable guarantee
stays the protected-branch merge — a write that ever slips the gate is bounded by that wall.

THE BLOCK BUDGET — the gate is the explore write-gate's PreToolUse member of the hook block budget. modes
DECLARES it (BLOCK_INVARIANT); hooks names no invariant itself, so the consumer (module_coherence) assembles
the registry from each owning system's declaration. PreToolUse is block-eligible, so the block-budget
coherence leg stays green over it.

CLI (the operator-runnable demo; the live gate is what the wired PreToolUse hook invokes):
  python tools/modes.py                              # hook mode: run the PreToolUse gate over stdin
  python tools/modes.py classify <Tool> [cmd] [--session S]   # what the gate decides for one action
  python tools/modes.py stance --session S           # the session's current stance
  python tools/modes.py set-build --session S         # enter Build for a session (demo of slice 26's verb)
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
    """Set the session's stance signal (used by the operator-typed Build verb at slice 26, and by the
    demo/tests now). Setting EXPLORE clears the marker (explore is the absence of a signal). Returns
    True on success, False when there is no usable session id or the write fails; never raises."""
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


# ---- the denied-action match list (a build-spec leaf, settled here) -------------------------
# The small enumerated set that BEGINS building (modes/README §"Explore"): file edits, branch creation,
# commits, and opening a pull request. `git push` is deliberately NOT here — the source enumerates these
# four, and Explore must stay the comfortable place to work (no default-deny, nothing else taxed).
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


# The plain-language denial — names what was blocked AND the concrete way forward, never a silent
# refusal (modes/README §"The stance is always operator-legible").
_DENIAL = ("I didn't make that change — we're exploring, so I won't edit files, commit, create a branch, "
           "or open a pull request yet. Tell me to build it and I'll open a pull request — the change I "
           "submit for your approval.")


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
    if is_building_action(tool_name, tool_input):
        return hooks.decide("deny", _DENIAL)
    return hooks.proceed()                           # reads, tests, greps, gh issue, subagents — allowed


# ---- the CLI (the operator-runnable demo; the live gate is the wired hook) -------------------

def _arg(argv: list, flag: str) -> str | None:
    """The value following `flag` in argv, or None."""
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


def _decision_line(decision: dict) -> str:
    """Render a handler decision as a one-line operator-facing verdict for the demo."""
    if decision.get("action") == "decide" and decision.get("permissionDecision") == "deny":
        return f"DENY — {decision.get('reason')}"
    return "ALLOW"


def _classify(argv: list) -> int:
    """`classify <Tool> [command...] [--session S]` — run the REAL handler over a synthetic payload and
    print what the gate decides, so the operator can vary the tool/command and confirm the behavior."""
    session = _arg(argv, "--session")
    rest = [a for a in argv if a != "--session" and a != session]
    if not rest:
        print("usage: modes.py classify <Tool> [command] [--session S]", file=sys.stderr)
        return 2
    tool_name = rest[0]
    command = " ".join(rest[1:])
    payload = {"session_id": session, "tool_name": tool_name,
               "tool_input": {"command": command} if command else {}}
    decision = handler(payload)
    stance = current_stance(session)
    print(f"stance={stance}  tool={tool_name!r}  command={command!r}")
    print(f"  -> {_decision_line(decision)}")
    return 0


def _demo(_argv: list) -> int:
    """A scripted fail-then-pass demonstration over the REAL handler (only the session id is a fixture)."""
    sid = "engine-demo-session"
    clear_stance(sid)
    print("The Explore write-gate — what the PreToolUse hook decides (the real handler):\n")
    print(f"In EXPLORE (stance={current_stance(sid)}):")
    for tool, cmd in [("Edit", ""), ("Write", ""), ("Bash", "git commit -m wip"),
                      ("Bash", "gh pr create"), ("Bash", "pytest -q"), ("Bash", "gh issue create -t x"),
                      ("Read", ""), ("Bash", "some_unknown_tool --flag")]:
        d = handler({"session_id": sid, "tool_name": tool, "tool_input": {"command": cmd}})
        print(f"  {tool:5} {cmd!r:28} -> {_decision_line(d)}")
    print(f"\nEnter Build (the slice-26 verb does this): set_stance -> {set_stance(sid, BUILD)}")
    print(f"In BUILD (stance={current_stance(sid)}): the same building actions are permitted:")
    for tool, cmd in [("Edit", ""), ("Bash", "git commit -m wip")]:
        d = handler({"session_id": sid, "tool_name": tool, "tool_input": {"command": cmd}})
        print(f"  {tool:5} {cmd!r:28} -> {_decision_line(d)}")
    print(f"\nClear the signal (what boot does at SessionStart): clear_stance -> {clear_stance(sid)}")
    print(f"Back in EXPLORE (stance={current_stance(sid)}): an Edit is denied again -> "
          f"{_decision_line(handler({'session_id': sid, 'tool_name': 'Edit', 'tool_input': {}}))}")
    print("\nThe gate is a §6 nudge, not a wall — a disguised verb slips it, a crash fails it open; "
          "the merge wall is the guarantee.")
    return 0


def main(argv: list) -> int:
    cmd = argv[0] if argv else "hook"
    if cmd == "hook":
        # Hook mode: what the wired PreToolUse hook invokes. run_hook reads the event JSON from stdin,
        # runs the gate, and translates decide(deny) -> structured stdout, fail-open on any error.
        return hooks.run_hook("PreToolUse", handler)
    if cmd == "classify":
        return _classify(argv[1:])
    if cmd == "stance":
        print(current_stance(_arg(argv, "--session")))
        return 0
    if cmd == "set-build":
        ok = set_stance(_arg(argv, "--session"), BUILD)
        print(f"set Build: {ok}")
        return 0 if ok else 1
    if cmd == "clear":
        ok = clear_stance(_arg(argv, "--session"))
        print(f"cleared: {ok}")
        return 0 if ok else 1
    if cmd == "demo":
        return _demo(argv[1:])
    print("usage: modes.py [hook | classify <Tool> [cmd] [--session S] | stance --session S | "
          "set-build --session S | clear --session S | demo]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
