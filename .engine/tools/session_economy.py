#!/usr/bin/env python3
"""The session-economy gate: a PreToolUse deny on the two spend behaviours nothing else governs.

Two rules, both narrow:

  * A search/plan subagent (`Explore`, `Plan`) must run on a cheap model. These types carry no model
    binding of their own — unlike the engine's own personas, which are stamped from
    `.engine/policies/model-bindings.json` — so without this they silently inherit the orchestrator's
    model. The operator's rule: a strong model is never valid here, because an expensive search agent is
    wasted spin-up the orchestrator should simply have done inline.
  * `ScheduleWakeup` is refused. Nothing in the engine instructs a session to schedule its own wake-ups;
    unattended work fires from the platform scheduler (see routine-entry), not from a session arranging
    to be woken. Observed waste included consecutive no-op wake-ups, each re-reading the whole context.

WHAT THIS IS NOT. It is not a cost router and does not meter spend: the engine cannot see its own token
use and does not own the model-invocation loop (model-routing.md rejects that scaffolding, and the
rejection stands). This gate acts at the one seam the engine genuinely observes — the spawn itself,
where a subagent type and a model are named in the tool call. It changes price per token, not the volume
of context re-reads; it is honest friction, not a budget.

FAIL TOWARD ALLOW. Every unrecognized shape allows. The payload contract for these tools is the
platform's, not the engine's, and it has changed before (the subagent tool has been named both `Task`
and `Agent`), so a shape this gate does not recognize must never become a block — matching the
no-default-deny law modes.is_building_action keeps. A wrong deny is not caught by the fail-open harness,
which only covers crashes, so the escape is explicit: set ENGINE_SESSION_ECONOMY=off.
"""
import json
import os
import sys
from pathlib import Path

import hooks

ROOT = Path(__file__).resolve().parents[2]
BINDINGS = ROOT / ".engine" / "policies" / "model-bindings.json"

# The subagent tool across platform revisions. Both are matched so a rename does not silently un-gate.
SUBAGENT_TOOLS = ("Agent", "Task")
# Only the UNBOUND general-purpose search/plan types. The engine's own personas carry a stamped `model:`
# and are never touched here; `general-purpose` is deliberately absent because it does judgment work.
GATED_SUBAGENT_TYPES = ("Explore", "Plan")
WAKEUP_TOOLS = ("ScheduleWakeup",)
OFF_SWITCH = "ENGINE_SESSION_ECONOMY"

BLOCK_INVARIANT = {"event": "PreToolUse", "name": "session-economy-gate", "owner": "session_economy",
                   "modes": ["explore", "build", "routine"]}


def disabled() -> bool:
    """The operator's escape, deliberately an environment variable rather than a tunable: the tunables
    surface holds preferences and explicitly never enforcement switches, and its override file sits outside
    the weakening guard on the strength of that. Honest limit: hooks inherit the LAUNCHING process's
    environment, so this is not settable from inside the session a wrong deny just stopped — it governs
    sessions started after it is set. That is a real cost of the choice, not a hidden one."""
    return os.environ.get(OFF_SWITCH, "").strip().lower() in {"off", "0", "false"}


def cheap_models() -> set:
    """The models a search/plan subagent may use, derived from the bindings file rather than restated here,
    so a fleet retune moves this gate with it instead of leaving it enforcing a stale set. `sonnet` is
    included per the operator's rule; a read failure yields the built-in floor rather than an empty set,
    which would deny every spawn."""
    accepted = {"haiku", "sonnet"}
    try:
        data = json.loads(BINDINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return accepted
    tiers = data.get("tiers") or {}
    # Deriving can only ever WIDEN what this gate accepts, so it is clamped: a model the bindings assign to
    # the judgment tier is by definition not the cheap end of the fleet, and a retune that pointed the
    # mechanical tier at it must not silently make this gate accept it.
    judgment = (tiers.get("judgment") or {}).get("model")
    candidates = [(tiers.get("mechanical") or {}).get("model"),
                  ((data.get("implementation_classes") or {}).get("bounded") or {}).get("claude", {}).get("model")]
    for model in candidates:
        if isinstance(model, str) and model and model != judgment:
            accepted.add(model)
    return accepted


def subagent_denial(tool_name, tool_input):
    """The deny reason for an over-powered search/plan spawn, or None to allow."""
    if tool_name not in SUBAGENT_TOOLS or not isinstance(tool_input, dict):
        return None
    kind = tool_input.get("subagent_type")
    if kind not in GATED_SUBAGENT_TYPES:
        return None
    accepted = cheap_models()
    model = tool_input.get("model")
    if isinstance(model, str) and model.split("[")[0].strip() in accepted:
        return None
    named = " or ".join(sorted(accepted))
    return (f"A {kind} subagent must name a cheap model ({named}); this spawn named "
            f"{model!r}. A search or planning agent on an expensive model is spin-up cost for work the "
            "orchestrator should do inline — if the task genuinely needs stronger judgment, do it yourself "
            "rather than delegating it. Relaunch this spawn with model set to one of those. To switch the "
            f"gate off entirely, set {OFF_SWITCH}=off in the environment (or the project settings' env "
            "block); hooks read the launching process's environment, so that takes effect for sessions "
            "started afterwards, not this one.")


def wakeup_denial(tool_name):
    if tool_name not in WAKEUP_TOOLS:
        return None
    return ("This session should not schedule its own wake-up. Each one re-reads the whole context, and "
            "observed builds spent consecutive wake-ups reporting nothing. Wait on a blocking check, or end "
            "the turn and let the operator resume; unattended work is fired by the platform scheduler, not "
            f"arranged from inside a session. To switch the gate off, set {OFF_SWITCH}=off in the "
            "environment (or the project settings' env block), which applies to sessions started after.")


def handler(payload: dict) -> dict:
    if disabled() or not isinstance(payload, dict):
        return hooks.proceed()
    tool_name = payload.get("tool_name")
    if not isinstance(tool_name, str):
        return hooks.proceed()
    reason = wakeup_denial(tool_name) or subagent_denial(tool_name, payload.get("tool_input"))
    if reason:
        return hooks.decide("deny", reason)
    return hooks.proceed()


def main(argv: list) -> int:
    if argv and argv[0] == "hook":
        return hooks.run_hook("PreToolUse", handler)
    print("usage: session_economy.py hook", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
