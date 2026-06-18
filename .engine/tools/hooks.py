#!/usr/bin/env python3
"""The hooks contract substrate (core slice 17) — the cross-cutting hook LAWS every local gate
is built on. Where the control-plane is the GitHub-side substrate, hooks is the in-session
substrate the boot pack, the close ritual, the local nudges, and the experiential capture all
fire through. It is foundational: boot, close, telemetry, validation, and memory all presuppose
it (systems/infrastructure/hooks/README.md).

THIS MODULE OWNS THE LAWS, NOT THE BEHAVIORS (hooks/README §"Hooks"). It ships:
  - the closed EVENT INVENTORY (the engine's chosen subset of the platform's larger event set),
  - the BLOCK BUDGET (which events may hard-block — only PreToolUse and Stop) + the block cap,
  - the FAIL-OPEN-AND-FLAG harness (a crashing gate never strands the operator and never fails
    silently — principles §5),
  - the HOOK-SCRIPT CONTRACT translation (stdin event JSON; exit code / structured stdout),
  - the per-OS INTERPRETER-PATH resolver (D-156).
It ships NO hook behavior and NO registered hook: the boot SessionStart pack (slice 20), the close
Stop ritual (slice 22), the modes explore write-gate (slice 21), and validation/telemetry's
PostToolUse each supply their own handler and ride this harness. The committed `.claude/settings.json`
that registers a hook is born at the first hook-wiring slice (20); the keyed, reversible registration
MECHANISM is the wiring library's (wiring.py), applied by provisioning — hooks fixes only that
registration must be keyed and reversible, never the mechanism (hooks/README §"Registration ...").

The STATIC half of the block-budget law — the pure `block_budget_findings` coherence leg that flags a
block declared on a non-eligible event — lives in validate.py beside its sibling legs (agent/skill/
interface/policy), fixture-tested with no live rule until the first hook-wiring slice (20) gives it a
registration source to read (the agent/skill-coherence precedent). This module owns the RUNTIME half:
the harness enforces the same budget at the moment a handler asks to block.

BOUNDARY (hooks/README §"Boundary"): a hook script is a `tool` instance — deterministic engine code
homed at `.engine/tools/`, not a dedicated surface. Hook registrations and the settings file are
wiring, not surfaces.

CLI (the operator-runnable demo on a throwaway fixture — no registered hook exists until slice 20):
  uv run --directory .engine -- python tools/hooks.py demo
"""
from __future__ import annotations
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402


# ---- the closed event inventory (hooks/README §"The event inventory") -------------------------
# The Engine governs a SUBSET of the Claude Code hook events: the platform exposes many more
# (SubagentStart/Stop, PostCompact, PermissionRequest, StopFailure, Notification, ...) — the set
# the Engine binds is an end-state decision, not the platform's full list, and it grows additively
# like the surface catalog. This is NOT a claim that only seven events exist.
#   blocks  — may this event HARD-BLOCK? Only PreToolUse and Stop (the block-budget law).
#   injects — may this event inject `additionalContext`?
#   owners  — the system(s) that own the behavior on this event. PostToolUse has THREE owners
#             (validation's local nudge + telemetry's ambient capture + modes' plan-acceptance
#             Build-entry trigger coexist on one event — D-180); SessionEnd is hooks-owned
#             (cleanup/flush, cannot block); UserPromptSubmit is boot/orientation's per-prompt scent.
#             SessionStart has TWO owners: boot's orientation pack + memory's consolidation sweep (3b),
#             which coexist on one event by keyed registration (the PostToolUse multi-owner precedent).
EVENT_INVENTORY = {
    "SessionStart":     {"owners": ("boot", "memory"),          "blocks": False, "injects": True},
    "PreToolUse":       {"owners": ("invariant-owner",),         "blocks": True,  "injects": True},
    "PostToolUse":      {"owners": ("validation", "telemetry", "modes"), "blocks": False, "injects": False},
    "PreCompact":       {"owners": ("memory",),                  "blocks": False, "injects": False},
    "Stop":             {"owners": ("close",),                   "blocks": True,  "injects": False},
    "SessionEnd":       {"owners": ("hooks",),                   "blocks": False, "injects": False},
    "UserPromptSubmit": {"owners": ("boot",),                    "blocks": False, "injects": True},
}
EVENTS = frozenset(EVENT_INVENTORY)
# The block budget: the closed set of events that MAY hard-block. The platform would let PreCompact,
# UserPromptSubmit, and SubagentStop block too; the Engine declines — a local hard-block buys
# friction without proportional trust (principles §6). The one unbypassable gate stays the
# protected-branch review.
BLOCK_ELIGIBLE_EVENTS = frozenset(e for e, m in EVENT_INVENTORY.items() if m["blocks"])  # {PreToolUse, Stop}

# The block-eligible INVARIANT set starts EMPTY (hooks/README §"The block-budget law"): owning
# systems register their block into it additively when designed — close's findings-disposition Stop
# block (slice 22) and modes' explore write-gate PreToolUse block (slice 21). Hooks names no
# invariant itself, so it presupposes none of the systems that will populate the set. A registration
# is {event, name, owner} (forward-compatible: a consumer may add modes/other fields); the validator
# (validate.block_budget_findings) reads only `event`.
BLOCK_ELIGIBLE_INVARIANTS: tuple = ()


# ---- the Stop-hook block cap (hooks/README §"The block-budget law"; verified on the live platform) ---
# A Stop or PreToolUse block is a STRONG local block, not an absolute wall: Claude Code force-ends the
# turn after this many consecutive Stop blocks (it sets `stop_hook_active` on the forced continuation),
# so a local gate makes evasion take deliberate effort while the durable backstop stays the merge gate.
STOP_HOOK_BLOCK_CAP = 8
STOP_HOOK_BLOCK_CAP_ENV = "CLAUDE_CODE_STOP_HOOK_BLOCK_CAP"   # the operator override (raise the cap)


# ---- exit codes (the platform contract; hooks/README §"Fail-open-and-flag") -------------------
# Exit 2 is the ONLY blocking exit (and only PreToolUse/Stop may use it); 2 also feeds stderr back to
# Claude. Any OTHER non-zero exit is a NON-BLOCKING error — the tool runs. A gate must never be wrapped
# to deny-on-error: that would make a bug fail closed and strand a non-engineer who cannot debug it.
EXIT_PROCEED = 0       # allow / no-op / injection
EXIT_NONBLOCKING = 1   # the fail-open exit: a non-blocking error; the guarded action proceeds
EXIT_BLOCK = 2         # the single blocking exit (PreToolUse/Stop only)

# The PreToolUse structured-stdout permission decision values the Engine uses (the platform also
# offers `defer`, which the Engine does not need). hooks/README §"The hook-script contract".
PERMISSION_DECISIONS = frozenset({"allow", "deny", "ask"})


# ---- the per-OS interpreter-path resolver (D-156; hooks/README §"The hook-script contract") ----
# A hook command names the engine tool-runtime interpreter EXPLICITLY and ${CLAUDE_PROJECT_DIR}-rooted,
# so it is portable (resolves on any operator's machine after the template is generated) and independent
# of any PATH the non-interactive hook shell may lack. A bare `python`/`uv`, or `uv run` with its
# implicit re-sync, is NEVER used on a hot path (the re-sync adds latency at a latency-sensitive moment).
PROJECT_DIR_VAR = "${CLAUDE_PROJECT_DIR}"


def interpreter_path(os_name: str | None = None) -> str:
    """The ${CLAUDE_PROJECT_DIR}-rooted engine venv interpreter, resolved per-OS: POSIX `bin/python`,
    Windows `Scripts/python.exe` (the standard venv layout). `os_name` defaults to os.name; pass it
    explicitly to render the other OS's form (fixture-testable)."""
    name = os.name if os_name is None else os_name
    sub = "Scripts/python.exe" if name == "nt" else "bin/python"
    return f"{PROJECT_DIR_VAR}/.engine/.venv/{sub}"


# The hook launcher (.engine/tools/hook-runner.sh) holds the bounded wait that lets a hook survive the
# fresh-worktree race (issue #83): the gitignored `.engine/.venv` is provisioned a beat AFTER a checkout,
# so a hook that fires in that window finds no interpreter and exits 127 — a SessionStart hook cannot
# block, so the failure is silent and boot never runs. The launcher polls for the interpreter, then execs
# it; the ceiling is ~5 s (the observed provisioning gap is well under 1 s), overridable for tests via
# ENGINE_HOOK_WAIT_POLLS / ENGINE_HOOK_WAIT_INTERVAL. It is NOT extended to cover a cold multi-second
# runtime build, and NEVER falls back to the operator's system Python. The wait/exec preamble used to be
# inlined in every hook command; it moved into this one launcher so the command Claude Code DISPLAYS after
# a hook fires stays short and legible to the non-engineer operator (it otherwise reads as a wall of code).
HOOK_RUNNER = f"{PROJECT_DIR_VAR}/.engine/tools/hook-runner.sh"


def hook_command(script_relpath: str, os_name: str | None = None) -> str:
    """The full hook `command` string a settings.json registration carries: a call to the hook launcher
    (`.engine/tools/hook-runner.sh`) passing the explicit ${CLAUDE_PROJECT_DIR}-rooted venv interpreter and
    the ${CLAUDE_PROJECT_DIR}-rooted script. The launcher does the bounded wait that closes the
    fresh-worktree race (issue #83) and then `exec`s the interpreter; if the interpreter never appears it
    runs NOTHING and NEVER falls back to the operator's system Python (constraints §"cannot manage a
    language runtime"). The interpreter is still NAMED EXPLICITLY in the command (the launcher's first
    argument), so D-156's "the hook command names the interpreter explicitly and ${CLAUDE_PROJECT_DIR}-
    rooted" stays witnessable in the diff; only the wait/exec MECHANICS moved into the launcher — the
    invocation FORM is hooks' (D-156); the command's internal STRUCTURE (inline vs. launcher) is an
    unspecified build-spec leaf. Shell-form (no `args`), so Claude Code runs it under `sh -c` (macOS/Linux)
    / Git Bash (Windows); `${CLAUDE_PROJECT_DIR}` is substituted before the shell sees it, and the script +
    any trailing args stay the UNQUOTED tail so they word-split into the launcher's positional params. The
    settings.json registration itself is wiring's (slice 20)."""
    interp = interpreter_path(os_name)
    return f'sh "{HOOK_RUNNER}" "{interp}" {PROJECT_DIR_VAR}/{script_relpath}'


# ---- the decision vocabulary a handler returns (the hook-script contract, normalized) ----------
# A handler is the OWNING SYSTEM'S behavior (boot/close/modes/...). The harness ships no handler and
# seeds no block: it is a pure MECHANICAL TRANSLATOR of a handler's returned decision into the platform
# contract, never deciding *when* to block. These constructors are the small, reviewed surface a
# consumer's handler returns.

def proceed() -> dict:
    """Allow the action / no-op (the default). → exit 0, no output."""
    return {"action": "proceed"}


def block(reason: str) -> dict:
    """Request a HARD block with a plain-language reason. Honored only on a block-eligible event
    (PreToolUse/Stop); on any other event it is a budget violation → fail open + flag."""
    return {"action": "block", "reason": reason}


def inject(context: str) -> dict:
    """Inject `additionalContext` back to Claude (SessionStart / UserPromptSubmit / PreToolUse
    injectors). → structured stdout, exit 0."""
    return {"action": "inject", "context": context}


def decide(permission: str, reason: str | None = None) -> dict:
    """A PreToolUse structured permission decision (allow/deny/ask). → structured stdout, exit 0.
    `deny` is a block expressed through the structured channel rather than exit 2."""
    return {"action": "decide", "permissionDecision": permission, "reason": reason}


# ---- the fail-open-and-flag harness (hooks/README §"Fail-open-and-flag") -----------------------

def _emit_finding(err, severity: str, message: str) -> None:
    """Surface a fail-open finding in plain language on stderr (the channel the platform shows). The
    finding is `finding.v1`-shaped; the DURABLE, tracked promotion onto the telemetry remediation
    loop is telemetry's mechanism (slice 18) — hooks DETECTS and emits, telemetry tracks (the §16
    detection-vs-relay seam; `owes → 18`)."""
    f = validate.finding(severity, message)
    err.write(f["message"] + "\n")


def run_hook(event: str, handler, *, stdin=None, stdout=None, stderr=None) -> int:
    """Run one hook event under the fail-open-and-flag law and the platform contract. `event` is the
    Claude Code event name (the calling hook script declares it); `handler(payload) -> decision` is the
    owning system's behavior. Returns the process exit code (the caller does `sys.exit(run_hook(...))`).

    The law, in order:
      1. Read the event JSON from stdin (tolerant). A payload the platform cannot even deliver is the
         platform's contract breaking, not the operator's fault — FAIL OPEN (never block on it) + flag.
      2. A forced Stop continuation (`stop_hook_active` true: the platform is force-ending the turn
         after the block cap) STILL runs the handler — the owning system may need the give-up moment
         (close degrades a still-undispositioned finding to a logged tracked finding here, so the cap
         can never lose one) — but it MUST NOT re-block, or it loops until the cap, so ANY block it
         returns is downgraded to proceed (in run_hook, the budget law; `_translate` stays pure).
      3. Run the handler. ANY exception → the guarded action proceeds (non-blocking exit) and the
         failure becomes a plain-language finding: a crashing gate must never strand a non-engineer
         who cannot debug it, and must never fail silently (principles §5).
      4. Translate the handler's decision to the platform contract — gating a block to the two
         block-eligible events (the budget); a block requested elsewhere is itself fail-open + flag."""
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    inp = sys.stdin if stdin is None else stdin

    try:
        raw = inp.read()
        payload = json.loads(raw) if raw and raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
    except Exception:  # noqa: BLE001 — reading the platform's event must NEVER block: any input the
        #   platform delivers (or fails to) is fail-open, never the operator's fault.
        _emit_finding(err, "hard",
                      f"The {event} hook could not read its event input, so it could not run; the "
                      f"action was allowed to proceed and this was recorded as a problem to fix.")
        return EXIT_NONBLOCKING

    # A forced Stop continuation: the handler still runs (close needs the give-up moment to log a
    # still-undispositioned finding), but its block is downgraded to proceed below so it can NEVER
    # re-block and loop the cap. stop_hook_active is only ever set by the platform on a Stop.
    forced_stop = event == "Stop" and payload.get("stop_hook_active") is True

    try:
        decision = handler(payload) if handler is not None else proceed()
    except (Exception, SystemExit) as exc:  # noqa: BLE001 — fail-open is the whole point. SystemExit
        #   is included DELIBERATELY: a handler that reaches past the decision protocol and calls
        #   sys.exit() (e.g. exit 2 to force a block) must STILL fail open — the harness owns the exit
        #   code, so a handler bug can never fail-closed and strand a non-engineer. KeyboardInterrupt /
        #   GeneratorExit (not caught here) stay propagating so an operator can still interrupt.
        _emit_finding(err, "hard",
                      f"A safety check on the {event} step could not run ({type(exc).__name__}); the "
                      f"action was allowed to proceed and this was recorded as a problem to fix. The "
                      f"work was not verified by that check.")
        return EXIT_NONBLOCKING

    if forced_stop and isinstance(decision, dict) and decision.get("action") == "block":
        decision = proceed()   # no-re-block guarantee, by construction (the harness, not the handler)

    return _translate(event, decision or proceed(), out, err)


def _translate(event: str, decision, out, err) -> int:
    """Pure translation of a handler decision → (exit code, stdout/stderr writes). Enforces the block
    budget: a block is honored (exit 2) ONLY on a block-eligible event; anywhere else it is a misuse
    that fails open and flags rather than blocks."""
    action = decision.get("action") if isinstance(decision, dict) else None

    if action == "block":
        if event not in BLOCK_ELIGIBLE_EVENTS:
            _emit_finding(err, "hard",
                          f"A {event} hook tried to hard-block, but only {sorted(BLOCK_ELIGIBLE_EVENTS)} "
                          f"may block; the action was allowed to proceed and this was recorded as a "
                          f"problem to fix.")
            return EXIT_NONBLOCKING
        err.write((decision.get("reason") or "") + "\n")
        return EXIT_BLOCK

    if action == "inject":
        out.write(json.dumps({"hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": decision.get("context", ""),
        }}) + "\n")
        return EXIT_PROCEED

    if action == "decide":
        perm = decision.get("permissionDecision")
        if event != "PreToolUse" or perm not in PERMISSION_DECISIONS:
            _emit_finding(err, "hard",
                          f"A {event} hook returned a permission decision {perm!r}, which is only valid "
                          f"as one of {sorted(PERMISSION_DECISIONS)} on a PreToolUse hook; the action was "
                          f"allowed to proceed and this was recorded as a problem to fix.")
            return EXIT_NONBLOCKING
        result = {"hookEventName": "PreToolUse", "permissionDecision": perm}
        if decision.get("reason"):
            result["permissionDecisionReason"] = decision["reason"]
        out.write(json.dumps({"hookSpecificOutput": result}) + "\n")
        return EXIT_PROCEED

    return EXIT_PROCEED


# ---- the operator-runnable demo (a throwaway fixture; no registered hook exists until slice 20) ----

def _run_capture(event: str, handler, payload: dict):
    """Run the REAL committed harness with a fixture handler over a synthetic payload, capturing its
    stdout/stderr — so the demo exercises the shipped run_hook, not a reimplementation."""
    import io
    out, err = io.StringIO(), io.StringIO()
    code = run_hook(event, handler, stdin=io.StringIO(json.dumps(payload)), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def _demo(_argv: list) -> int:
    """A scripted, operator-runnable fail-open demonstration. It runs the REAL `run_hook` (only the
    misbehaving handler is a fixture) for the block and crash cases, and shells out to an absent
    interpreter for the missing-runtime case — proving the THREE fail-open behaviors without touching
    the real .venv. Vary it: change the handlers, or rename .engine/.venv and re-run the printed
    hook command line yourself."""
    print("The hooks fail-open contract — three behaviors (only the misbehaving handler is a fixture;")
    print("the harness run is the real committed .engine/tools/hooks.py):\n")

    print("(1) A gate that BLOCKS — a handler that returns block() on a block-eligible event (Stop):")
    code, _out, err = _run_capture("Stop", lambda p: block("not done yet — finish the disposition"),
                                   {"hook_event_name": "Stop"})
    print(f"    exit code = {code}  (2 = block; the platform feeds this back to Claude)")
    print(f"    stderr → Claude: {err.strip()!r}\n")

    print("(2) A gate that CRASHES — a handler that raises; the action must still proceed:")
    code, _out, err = _run_capture("PreToolUse", lambda p: (_ for _ in ()).throw(RuntimeError("boom")),
                                   {"hook_event_name": "PreToolUse"})
    print(f"    exit code = {code}  (not 2, so NON-blocking — the tool runs)")
    print(f"    plain-language finding on stderr: {err.strip()!r}\n")

    print("(2b) A block requested on a NON-eligible event (PostToolUse) — the budget rejects it, "
          "fail-open:")
    code, _out, err = _run_capture("PostToolUse", lambda p: block("I should not be able to block here"),
                                   {"hook_event_name": "PostToolUse"})
    print(f"    exit code = {code}  (not 2 — only PreToolUse/Stop may block)")
    print(f"    finding on stderr: {err.strip()!r}\n")

    import tempfile
    print("(3) The hook LAUNCHER (.engine/tools/hook-runner.sh) — the wait/exec preamble lives here now,")
    print("    so the command Claude Code DISPLAYS after a hook fires is short instead of a wall of shell:")
    old = ('n=0; while [ ! -x "${CLAUDE_PROJECT_DIR}/.engine/.venv/bin/python" ] && [ "$n" -lt 50 ]; '
           'do sleep 0.1; n=$((n+1)); done; [ -x "${CLAUDE_PROJECT_DIR}/.engine/.venv/bin/python" ] && '
           'exec "${CLAUDE_PROJECT_DIR}/.engine/.venv/bin/python" ${CLAUDE_PROJECT_DIR}/tools/boot.py')
    print(f"    BEFORE: {old}")
    print(f"    AFTER:  {hook_command('tools/boot.py')}")
    print("    (the AFTER line is exactly what the hook-display renders; the surrounding 'Pre-compact /")
    print("     completed successfully' chrome is the platform's and cannot be changed.)\n")

    runner = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hook-runner.sh")
    print("    The launcher still waits-then-execs the venv interpreter and NEVER falls back to system")
    print("    Python — proven against the REAL hook-runner.sh on throwaway interpreters:")
    with tempfile.TemporaryDirectory() as td:                         # (a) interpreter PRESENT -> execs it
        stub = os.path.join(td, "python")
        with open(stub, "w") as fh:
            fh.write('#!/bin/sh\necho "RAN $@"\n')
        os.chmod(stub, 0o755)
        r = subprocess.run(["sh", runner, stub, os.path.join(td, "hook.py"), "demo-arg"],
                           capture_output=True, text=True, timeout=10)
        print(f"      present → {r.stdout.strip()!r}  (execs the interpreter; the script + args forward)")
    with tempfile.TemporaryDirectory() as td:                         # (b) NEVER appears -> runs nothing
        r = subprocess.run(["sh", runner, os.path.join(td, "python"), os.path.join(td, "hook.py")],
                           capture_output=True, text=True, timeout=10,
                           env={**os.environ, "ENGINE_HOOK_WAIT_POLLS": "3",
                                "ENGINE_HOOK_WAIT_INTERVAL": "0.05"})
        print(f"      never   → stdout={r.stdout.strip()!r} exit={r.returncode}  "
              f"(ran nothing; no system-Python fallback)\n")

    print("All three proceeded without a hard block except the one deliberate, eligible block — the "
          "fail-open floor holds.")
    print("(The plain-language operator surfacing in the PR Validation section and boot orientation is "
          "rendered by later slices; here the failure is EMITTED as a finding.)")
    return 0


def main(argv: list) -> int:
    cmd = argv[0] if argv else "demo"
    if cmd == "demo":
        return _demo(argv[1:])
    print(f"usage: hooks.py demo\nunknown command {cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
