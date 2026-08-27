#!/usr/bin/env python3
"""transaction.py — one stateless CLI for every lifecycle transaction.

    transaction.py inspect <operation> [args] [--json]
    transaction.py plan    <operation> [args] [--json]
    transaction.py run     <operation> [args] --consent-handle <handle> [--json]
    transaction.py resume  <operation> [args] [--json]

The normal path is two commands: `plan`, then `run` once the operator has seen the plan and hands its
consent handle back. `resume` is for interruptions, not for ordinary use.

WHY THIS EXISTS. Upgrade, module add/remove and whole-engine removal each already have working domain
logic; what they lacked was a typed surface. Their runbooks narrated the commands instead, and narration
drifts from code, taxes every session that reads it, and invites paraphrase error at exactly the moments —
consent, refusal, recovery — where precision matters most. So the sequence moves into typed machinery and
the prose keeps only what a machine cannot decide.

STATELESS, AND NO LEDGER. Nothing here stores anything. An envelope is process output; the durable history
of a transaction that ends in a pull request IS that pull request. A second durable record would fork
authority over what a change is, and the pull request has to win that.

THE ADAPTER SEAM. Every adapter implements inspect, plan, apply, verify and handoff. `run` executes the
last three IN ONE PROCESS — not for tidiness but because whole-engine removal deletes this file during its
own apply and could not be re-entered afterwards. Adapters WRAP existing domain functions; they never
re-decide what the domain already decides, because two copies of a rule drift and the drift is invisible
until it matters.

CONSENT IS PLAN IDENTITY, NOT AUTHORSHIP. The handle is the digest of the plan the operator was shown,
re-derived and compared immediately before any mutation. It proves that what is about to run is what was
previewed; it does not prove who consented, and this module never claims otherwise. Two operations layer a
stronger start on top, and both are the operator's call rather than this module's: `engine-remove` refuses
`run` outright (a deletion is a harder recovery than an upgrade), and `engine-upgrade`'s apply is reached
through the operator-typed command, which the harness itself gates.

STANDARD LIBRARY ONLY on the 3.9 floor: the arrival adapter reaches this module before the engine's own
3.11 runtime exists.
"""
from __future__ import annotations

import argparse
import json
import sys

import transaction_envelope as envelope

# Operations whose `run` verb refuses outright, with the door that does apply them. The refusal is
# mechanical here; the reason is the operator's recorded judgment, not this module's opinion.
_OPERATOR_TYPED_ONLY = {
    "engine-remove": ("Removing the engine is applied only by the command you type yourself "
                      "(`module_manager.py remove-engine --confirm`). Undoing a removal is a harder "
                      "recovery than undoing an upgrade, so its start stays a deliberate act of yours."),
}


class TransactionRefused(Exception):
    """A clean stop: nothing was changed. Carries the typed refusal the envelope will report."""

    def __init__(self, code: str, explanation: str, next_actions, retryable: bool = False):
        super().__init__(explanation)
        self.code = code
        self.explanation = explanation
        self.next_actions = list(next_actions)
        self.retryable = retryable


class Adapter:
    """What every lifecycle transaction implements.

    An adapter is a THIN wrapper over domain logic that already exists. It resolves inputs, prices the
    change in the operator's terms, and reports what happened — it never re-implements a domain rule.
    Substituting a stubbed domain function must visibly change the adapter's envelope; that property is
    what a test asserts, and it is how the build proves an adapter defers rather than duplicating.
    """

    operation = ""

    def inspect(self, args) -> dict:
        """Read-only facts plus their fingerprints. Changes nothing, ever."""
        raise NotImplementedError

    def plan(self, args, facts: dict) -> dict:
        """Resolve inputs and choices, price consequences and effects. No handle while a required
        choice is unresolved — refuse instead, naming the choice."""
        raise NotImplementedError

    def apply(self, args, plan: dict) -> dict:
        """Perform the change. Reached only after the consent handle has been verified."""
        raise NotImplementedError

    def verify(self, args, applied: dict):
        """Return receipts. A check that could not run is `unavailable` — never quietly `passed`."""
        raise NotImplementedError

    def handoff(self, args, applied: dict, receipts) -> dict:
        """Where the operator is left: a pull request, a discrete in-tree commit, verified external
        state, a local recovery, or a named manual follow-up."""
        raise NotImplementedError

    def resume(self, args):
        """Pick up an interrupted transaction, or refuse with a fresh plan.

        Default: re-inspect and re-plan. That is the honest answer for an adapter with no durable
        progress marker of its own — there is no store here to consult, and guessing what was already
        applied is worse than starting the decision over.
        """
        return None


_REGISTRY = {}


def register(adapter: Adapter) -> Adapter:
    _REGISTRY[adapter.operation] = adapter
    return adapter


def _adapter_for(operation: str) -> Adapter:
    if operation not in _REGISTRY:
        raise TransactionRefused(
            "unknown-operation",
            "No adapter implements {0!r}.".format(operation),
            ["Run `transaction.py --help` to see the operations this engine implements."])
    return _REGISTRY[operation]


def _envelope(operation: str, phase: str, completed, outcome: str, **parts) -> dict:
    result = {
        "schema_version": envelope.SCHEMA_VERSION,
        "operation": operation,
        "requested_phase": phase,
        "completed_phases": list(completed),
        "outcome": outcome,
    }
    result.update({key: value for key, value in parts.items() if value is not None})
    return envelope.validate(result)


def _refusal_envelope(operation: str, phase: str, completed, refused: TransactionRefused) -> dict:
    return _envelope(operation, phase, completed, "refused", refusal={
        "code": refused.code,
        "explanation": refused.explanation,
        "retryable": refused.retryable,
        "next_actions": refused.next_actions,
    })


def _planned(adapter: Adapter, args):
    """inspect + plan, with the consent handle minted over the canonical plan."""
    facts = adapter.inspect(args)
    plan = dict(adapter.plan(args, facts))
    # Bind the world into the plan BEFORE hashing. The handle has to cover repository and external state,
    # not only the plan's wording: otherwise a plan whose prose happens not to change keeps a valid handle
    # across a moved repository, and the staleness guarantee is decorative.
    plan["bound_fingerprints"] = dict((facts or {}).get("fingerprints") or {})
    plan["digest"] = envelope.consent_handle(plan)
    plan["consent_handle"] = plan["digest"]
    return facts, plan


def do_inspect(adapter: Adapter, args) -> dict:
    facts = adapter.inspect(args)
    return _envelope(adapter.operation, "inspect", ["inspect"], "ok", facts=facts)


def do_plan(adapter: Adapter, args) -> dict:
    facts, plan = _planned(adapter, args)
    return _envelope(adapter.operation, "plan", ["inspect", "plan"], "ok", facts=facts, plan=plan)


def do_run(adapter: Adapter, args, supplied_handle: str) -> dict:
    """apply -> verify -> handoff, in one process, and only on a handle that still matches.

    The order here is the whole safety property: the plan is re-derived from the CURRENT world and its
    handle compared against the one the operator carried back. A world that moved yields a different
    handle, so the transaction refuses and returns the fresh plan rather than applying consent that was
    given to a different change. Nothing above this line mutates anything.
    """
    if adapter.operation in _OPERATOR_TYPED_ONLY:
        raise TransactionRefused(
            "operator-typed-only",
            _OPERATOR_TYPED_ONLY[adapter.operation],
            ["Type the operator command yourself; this protocol will not start it for you.",
             "Use `transaction.py plan {0}` to see exactly what it would do first."
             .format(adapter.operation)])

    facts, fresh = _planned(adapter, args)
    if not supplied_handle:
        raise TransactionRefused(
            "consent-handle-missing",
            "Applying takes the consent handle from the plan you were shown. Nothing was changed.",
            ["Run `transaction.py plan {0}` and pass its consent handle to `run --consent-handle`."
             .format(adapter.operation)])
    if supplied_handle != fresh["consent_handle"]:
        # Deliberately hand back the fresh plan: the operator sees WHAT moved, not merely that it did.
        stale = _envelope(adapter.operation, "run", ["inspect", "plan"], "refused",
                          facts=facts, plan=fresh, refusal={
                              "code": "consent-handle-stale",
                              "explanation": ("The world moved since the plan you approved, so that "
                                              "consent no longer describes this change. Nothing was "
                                              "changed. The current plan is included here."),
                              "retryable": True,
                              "next_actions": [
                                  "Read the fresh plan above.",
                                  "If it is still what you want, run again with its consent handle."],
                          })
        raise StalePlan(stale)

    completed = ["inspect", "plan"]
    applied = adapter.apply(args, fresh)
    completed.append("apply")
    receipts = list(adapter.verify(args, applied))
    completed.append("verify")
    handoff = adapter.handoff(args, applied, receipts)
    completed.append("handoff")
    return _envelope(adapter.operation, "run", completed, "ok",
                     facts=facts, plan=fresh, verification=receipts, handoff=handoff)


class StalePlan(Exception):
    """Carries the refusal envelope for a handle that no longer matches the world."""

    def __init__(self, envelope_dict: dict):
        super().__init__("the consent handle is stale")
        self.envelope = envelope_dict


def do_resume(adapter: Adapter, args) -> dict:
    """Continue an interrupted transaction, or refuse honestly.

    An adapter with a durable progress marker re-fingerprints only the effects it has NOT yet applied
    and continues from there. An adapter without one re-plans: `resume` then means "decide again from
    the current world", which is the truthful answer when nothing recorded how far the last attempt got.
    """
    resumed = adapter.resume(args)
    if resumed is not None:
        return envelope.validate(resumed)
    facts, plan = _planned(adapter, args)
    return _envelope(adapter.operation, "resume", ["inspect", "plan"], "ok", facts=facts, plan=plan,
                     verification=[{
                         "check": "prior progress",
                         "result": "unavailable",
                         "detail": ("This operation records no durable progress marker, so how far an "
                                    "interrupted attempt got cannot be read back. This is a fresh plan "
                                    "against the current state, not a continuation."),
                     }])


def _emit(result: dict, as_json: bool) -> int:
    if as_json:
        json.dump(result, sys.stdout, indent=1, sort_keys=True)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(envelope.render(result) + "\n")
    return 0 if result["outcome"] != "refused" else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="transaction.py",
        description="One typed protocol for the engine's lifecycle transactions: inspect, plan, "
                    "consent handle, apply, verify, reviewed handoff.")
    sub = parser.add_subparsers(dest="phase", required=True)
    for phase, help_text in (
            ("inspect", "read-only facts about this operation; changes nothing"),
            ("plan", "what this would do, and the consent handle for it"),
            ("run", "apply the plan you were shown, then verify and hand off"),
            ("resume", "pick up an interrupted transaction")):
        child = sub.add_parser(phase, help=help_text)
        child.add_argument("operation", help="which lifecycle transaction")
        child.add_argument("rest", nargs=argparse.REMAINDER,
                           help="operation arguments (module id, release reference, and so on)")
        child.add_argument("--json", action="store_true", help="the typed envelope, verbatim")
        if phase == "run":
            child.add_argument("--consent-handle", default="",
                               help="the handle from the plan you were shown")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    completed_by_phase = {"inspect": [], "plan": ["inspect"], "run": ["inspect"], "resume": []}
    try:
        adapter = _adapter_for(args.operation)
        if args.phase == "inspect":
            result = do_inspect(adapter, args)
        elif args.phase == "plan":
            result = do_plan(adapter, args)
        elif args.phase == "run":
            result = do_run(adapter, args, getattr(args, "consent_handle", ""))
        else:
            result = do_resume(adapter, args)
    except StalePlan as stale:
        return _emit(stale.envelope, args.json)
    except TransactionRefused as refused:
        operation = args.operation if args.operation in _REGISTRY else "engine-upgrade"
        result = _refusal_envelope(operation, args.phase,
                                   completed_by_phase.get(args.phase, []), refused)
        return _emit(result, args.json)
    return _emit(result, args.json)


if __name__ == "__main__":
    sys.exit(main())
