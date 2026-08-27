#!/usr/bin/env python3
"""transaction-envelope.v1 — the typed result every lifecycle transaction returns.

WHY THIS LOADS ITS SCHEMA EAGERLY. Whole-engine removal deletes `.engine/` — this file and its schema
among them — and must then still validate and render its own receipt in the same still-running process.
Python keeps imported MODULES resident; it does not keep deleted FILES. The house pattern elsewhere is to
read a schema from disk at call time (`validate.py`) and to bind third-party names lazily, and both of
those fail once the directory is gone. So this module reads the schema ONCE at import and validates from
the in-memory copy forever after: `validate()` and `render()` perform no disk read and no import. The test
that proves it deletes `.engine/` outright and then validates and renders an envelope.

STANDARD LIBRARY ONLY, ON THE 3.9 FLOOR. Brownfield arrival runs on the operator's system interpreter
before the engine's own 3.11 runtime exists (macOS ships 3.9), so every module the arrival adapter can
reach must import nothing beyond the standard library and must carry `from __future__ import annotations`
— an evaluated `X | None` annotation is a TypeError on 3.9, and that is a load-bearing rule here, not a
style preference. That rules out `jsonschema`, which is a tool-runtime dependency: validation below is
hand-written against the one schema this module owns, which is also what keeps it honest after deletion.

WHAT THIS MODULE IS NOT. It is not a store. An envelope is process output — the durable history of a
transaction that ends in a pull request is that pull request. There is deliberately no ledger.
"""
from __future__ import annotations

import hashlib
import json
import os

SCHEMA_VERSION = "transaction-envelope.v1"

_SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "schemas",
                            "transaction-envelope.v1.json")


class EnvelopeError(ValueError):
    """An envelope that does not conform. Raised by `validate`; never a silent pass."""


def _load_schema_once() -> dict:
    """Read the schema at IMPORT time. The one disk read this module ever performs."""
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


# Read now, while the file certainly exists. Everything below works from this copy.
SCHEMA = _load_schema_once()

PHASES = ("inspect", "plan", "run", "resume")
OUTCOMES = ("ok", "refused", "partial")
RESULTS = ("passed", "failed", "unavailable")
HANDOFF_KINDS = ("pull-request", "in-tree-commit", "verified-external-state", "checkless-confirmed",
                 "local-recovery", "manual-follow-up")
EXTERNAL_STATE_KINDS = ("verified-external-state", "checkless-confirmed")
EFFECT_KINDS = ("tracked-files", "shared-settings", "saved-data", "external-settings", "capability",
                "review-artifact")
REVERSIBILITY = ("reverted-pull-request", "local-recovery", "external-reapply", "irreversible")
OPERATIONS = tuple(SCHEMA["properties"]["operation"]["enum"])

# What canonical hashing leaves OUT, so that re-planning an unchanged world yields the same handle and
# only a real change invalidates it. Presentation ordering is excluded by sorting keys, not by name.
_UNHASHED_KEYS = frozenset({"digest", "consent_handle", "observed_at", "timestamp", "generated_at",
                            "token", "credential", "tmpdir", "temp_path"})


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise EnvelopeError(message)


def _enum(value, allowed, field: str) -> None:
    _require(value in allowed,
             "{0} must be one of {1}, got {2!r}".format(field, ", ".join(allowed), value))


def _nonblank(value, field: str) -> None:
    _require(isinstance(value, str) and value.strip() != "", "{0} must be a non-blank string".format(field))


def _digest(value, field: str) -> None:
    _nonblank(value, field)
    _require(value.startswith("sha256:") and len(value) == 71,
             "{0} must be a sha256: digest".format(field))
    _require(all(c in "0123456789abcdef" for c in value[7:]),
             "{0} must be lowercase hexadecimal".format(field))


def validate(envelope: dict) -> dict:
    """Check an envelope against transaction-envelope.v1. Returns it, or raises EnvelopeError.

    Performs no disk read and no import, so it keeps working after `.engine/` is deleted.
    """
    _require(isinstance(envelope, dict), "an envelope must be an object")
    _require(envelope.get("schema_version") == SCHEMA_VERSION,
             "schema_version must be {0}".format(SCHEMA_VERSION))
    _enum(envelope.get("operation"), OPERATIONS, "operation")
    _enum(envelope.get("requested_phase"), PHASES, "requested_phase")
    _enum(envelope.get("outcome"), OUTCOMES, "outcome")

    completed = envelope.get("completed_phases")
    _require(isinstance(completed, list), "completed_phases must be a list")
    for phase in completed:
        _enum(phase, ("inspect", "plan", "apply", "verify", "handoff"), "completed_phases entry")

    known = set(SCHEMA["properties"])
    unknown = sorted(set(envelope) - known)
    _require(not unknown, "unknown field(s): {0}".format(", ".join(unknown)))

    if "facts" in envelope:
        facts = envelope["facts"]
        _require(isinstance(facts, dict), "facts must be an object")
        prints = facts.get("fingerprints")
        _require(isinstance(prints, dict), "facts.fingerprints must be an object")
        for key, value in prints.items():
            _nonblank(value, "facts.fingerprints[{0}]".format(key))

    if "plan" in envelope:
        _validate_plan(envelope["plan"])

    if "refusal" in envelope:
        _validate_refusal(envelope["refusal"])

    for index, receipt in enumerate(envelope.get("verification", []) or []):
        _require(isinstance(receipt, dict), "verification[{0}] must be an object".format(index))
        _nonblank(receipt.get("check"), "verification[{0}].check".format(index))
        _enum(receipt.get("result"), RESULTS, "verification[{0}].result".format(index))

    if "handoff" in envelope:
        _validate_handoff(envelope["handoff"])

    # The two conditionals the shape exists to enforce.
    _require(envelope["outcome"] != "refused" or "refusal" in envelope,
             "a refused envelope must carry a refusal — a stop with no stated reason is the dead end "
             "this protocol removes")
    if envelope["requested_phase"] == "plan" and envelope["outcome"] == "ok":
        _require("plan" in envelope, "a successful plan phase must carry a plan")
    return envelope


def _validate_plan(plan) -> None:
    _require(isinstance(plan, dict), "plan must be an object")
    _require(isinstance(plan.get("inputs"), dict), "plan.inputs must be an object")

    consequences = plan.get("consequences")
    _require(isinstance(consequences, list) and consequences,
             "plan.consequences must list at least one plain-language consequence")
    for index, line in enumerate(consequences):
        _nonblank(line, "plan.consequences[{0}]".format(index))

    for index, choice in enumerate(plan.get("choices", []) or []):
        _require(isinstance(choice, dict), "plan.choices[{0}] must be an object".format(index))
        _nonblank(choice.get("id"), "plan.choices[{0}].id".format(index))
        _nonblank(choice.get("chosen"), "plan.choices[{0}].chosen".format(index))
        options = choice.get("options")
        _require(isinstance(options, list) and options,
                 "plan.choices[{0}].options must list the alternatives".format(index))

    for index, effect in enumerate(plan.get("effects", []) or []):
        _require(isinstance(effect, dict), "plan.effects[{0}] must be an object".format(index))
        _enum(effect.get("kind"), EFFECT_KINDS, "plan.effects[{0}].kind".format(index))
        _nonblank(effect.get("description"), "plan.effects[{0}].description".format(index))

    _enum(plan.get("reversibility"), REVERSIBILITY, "plan.reversibility")
    _digest(plan.get("digest"), "plan.digest")
    _digest(plan.get("consent_handle"), "plan.consent_handle")


def _validate_refusal(refusal) -> None:
    _require(isinstance(refusal, dict), "refusal must be an object")
    code = refusal.get("code")
    _nonblank(code, "refusal.code")
    _require(all(c.islower() or c.isdigit() or c == "-" for c in code) and code[0].isalpha(),
             "refusal.code must be a stable lowercase identifier, safe to branch on")
    _nonblank(refusal.get("explanation"), "refusal.explanation")
    _require(isinstance(refusal.get("retryable"), bool),
             "refusal.retryable must be stated — an environment-gated skip that re-runs identically is "
             "NOT retryable, and saying otherwise sends the caller in a circle")
    actions = refusal.get("next_actions")
    _require(isinstance(actions, list) and actions,
             "refusal.next_actions must name at least one way forward")
    for index, action in enumerate(actions):
        _nonblank(action, "refusal.next_actions[{0}]".format(index))


def _validate_handoff(handoff) -> None:
    _require(isinstance(handoff, dict), "handoff must be an object")
    _enum(handoff.get("kind"), HANDOFF_KINDS, "handoff.kind")
    _nonblank(handoff.get("summary"), "handoff.summary")
    if handoff["kind"] in EXTERNAL_STATE_KINDS:
        _nonblank(handoff.get("observed_at"), "handoff.observed_at")
        _require(handoff.get("point_in_time") is True,
                 "an external-state handoff must mark itself point-in-time: it was true when read and "
                 "may not be now, and the live read remains the only answer")
    follow_up = handoff.get("follow_up")
    if follow_up is not None:
        _require(isinstance(follow_up, dict), "handoff.follow_up must be an object")
        _nonblank(follow_up.get("operation"), "handoff.follow_up.operation")
        _nonblank(follow_up.get("when"), "handoff.follow_up.when")


def canonical(plan: dict) -> str:
    """The plan's canonical form for hashing — what the consent handle is taken over.

    Excludes timestamps, credentials, temporary paths, and the digest fields themselves, and sorts keys
    so presentation order cannot change the handle. Re-planning an unchanged world therefore yields the
    same handle; only a real change to target, choices, consequences, effects, or fingerprints breaks it.
    """
    return json.dumps(_strip(plan), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _strip(value):
    if isinstance(value, dict):
        return {k: _strip(v) for k, v in value.items() if k not in _UNHASHED_KEYS}
    if isinstance(value, list):
        return [_strip(v) for v in value]
    return value


def consent_handle(plan: dict) -> str:
    """The digest of the plan the operator was shown — evidence of plan identity, not of who consented."""
    return "sha256:" + hashlib.sha256(canonical(plan).encode("utf-8")).hexdigest()


def render(envelope: dict) -> str:
    """Concise operator prose from the same envelope the --json path returns.

    No disk read, no import: this still renders after `.engine/` has been deleted.
    """
    lines = ["{0} — {1}".format(envelope["operation"], envelope["requested_phase"])]
    facts = envelope.get("facts") or {}
    if facts.get("summary"):
        lines.append(facts["summary"])

    plan = envelope.get("plan")
    if plan:
        lines.append("")
        lines.append("What this would do:")
        for line in plan["consequences"]:
            lines.append("  - {0}".format(line))
        for choice in plan.get("choices", []) or []:
            lines.append("  choice {0}: {1} (of {2})".format(
                choice["id"], choice["chosen"], ", ".join(choice["options"])))
        lines.append("Undo: {0}".format(plan["reversibility"].replace("-", " ")))
        for step in plan.get("manual_steps", []) or []:
            lines.append("  you do this part: {0}".format(step))
        lines.append("Consent handle: {0}".format(plan["consent_handle"]))

    refusal = envelope.get("refusal")
    if refusal:
        lines.append("")
        lines.append("Refused ({0}) — nothing was changed.".format(refusal["code"]))
        lines.append(refusal["explanation"])
        lines.append("Retrying {0} help.".format("could" if refusal["retryable"] else "will not"))
        for action in refusal["next_actions"]:
            lines.append("  next: {0}".format(action))

    receipts = envelope.get("verification") or []
    if receipts:
        lines.append("")
        lines.append("Checked:")
        for receipt in receipts:
            # `unavailable` is spelled out, never rendered as a pass.
            state = {"passed": "passed", "failed": "FAILED",
                     "unavailable": "could not run — this area is unverified"}[receipt["result"]]
            lines.append("  {0}: {1}".format(receipt["check"], state))

    handoff = envelope.get("handoff")
    if handoff:
        lines.append("")
        lines.append("Where this leaves you: {0}".format(handoff["summary"]))
        if handoff.get("reference"):
            lines.append("  {0}".format(handoff["reference"]))
        if handoff.get("point_in_time"):
            lines.append("  read at {0} — true when read; the live read is the only current answer"
                         .format(handoff.get("observed_at")))
        follow_up = handoff.get("follow_up")
        if follow_up:
            lines.append("  then: {0} — {1}".format(follow_up["operation"], follow_up["when"]))
    return "\n".join(lines)
