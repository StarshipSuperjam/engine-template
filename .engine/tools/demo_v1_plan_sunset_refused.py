#!/usr/bin/env python3
"""Behavioral FALSIFICATION that the v1 plan generation is GONE, not merely discouraged.

The sunset (StarshipSuperjam/engine-template#1058's successor, plan pln_289714f21654) deleted the v1 state
and handoff schemas, every version-dispatch entry, the plan-block marker readers, `plan migrate-v1`, and
`checkpoint --complete-item`. A sunset that only stops SHIPPING the v1 path is not a sunset: what matters to
a deployed project is that a v1 document cannot get in and the converter that used to let it in is not there
to reach for. So this asserts the doors, not the absence of code.

FAIL-THEN-PASS on the same fixture; the arms differ only in which door is knocked on:
  * POSITIVE (the sunset): a `build-plan.v1` document is REFUSED at the version gate, and the refusal NAMES
    v1 rather than reporting an unrecognised shape — a refusal that cannot say what it refused sends its
    reader looking for a typo. The migration verb is gone from the parser entirely, so the escape hatch
    cannot be invoked, and the escape hatch's own removal is stated rather than silently discovered.
  * NEGATIVE CONTROL (what a half-sunset looks like): a document with NO schema_version at all is ALSO
    refused, and by name. That arm is the one that proves the gate is not merely pattern-matching the
    string "v1": before the sunset an absent version DEFAULTED to v1, which is how an unreadable file
    became a silently-misread one.

The one surface deliberately kept is not tested as absent, because it is not: `plan_contract`'s read-only
map of the build-plan.v1 PAYLOAD survives so a plan already stored in a workstation's library stays
readable, and StarshipSuperjam/engine-template#1070 carries its removal. This demo asserts the map is still
there, so a future sweep that removes it early has to meet this rather than discover it in a deployment.

Run:  uv run --directory .engine --frozen -- python tools/demo_v1_plan_sunset_refused.py
Its companion test (`test_review_economy` runs it via `test_build_coordinator.TestV1Sunset`) travels with the
engine, so this stays a permanent guard in every generated repository.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_coordinator as bc          # noqa: E402  (the real version gate)
import build_coordinator_dag as dag     # noqa: E402  (the plan-version reader)
import plan_contract                    # noqa: E402  (the one kept read-only surface)


def _refusal(callable_, *args) -> str:
    try:
        callable_(*args)
    except Exception as exc:                                # noqa: BLE001 — the message IS the subject
        return str(exc)
    return ""


def main() -> int:
    failures = []
    print("=" * 78)
    print("DEMO — the v1 plan generation is gone: a v1 document is refused BY NAME at the door, and the")
    print("converter that used to let one in no longer exists to reach for.")
    print("=" * 78)

    # ---- POSITIVE: a v1 document is refused, and the refusal says v1 ----------------------------
    v1 = {"schema_version": "build-plan.v1", "profile": "normal", "objective": "anything"}
    # `validate_plan_document`, not `plan_version`: reading the declared version is not the gate — the
    # gate is having no schema to validate that version against, which is exactly what the sunset removed.
    message = _refusal(dag.validate_plan_document, v1, bc.PLAN_SCHEMAS)
    named = "v1" in message
    print("\n[POSITIVE — a build-plan.v1 document at the version gate]")
    print(f"  refused:                                      {bool(message)}")
    print(f"  the refusal names v1:                         {named}")
    if message:
        print(f"  message: {message[:150]}")
    if not message:
        failures.append("POSITIVE: a build-plan.v1 document was NOT refused")
    elif not named:
        failures.append("POSITIVE: the refusal did not name v1, so its reader cannot tell what was refused")

    # ---- POSITIVE: the converter is gone from the surface, not merely deprecated ----------------
    dispatch_clean = "build-plan.v1" not in bc.PLAN_SCHEMAS and "build-state.v1" not in bc.STATE_SCHEMAS
    verb_gone = "migrate-v1" not in _parser_verbs()
    print("\n[POSITIVE — the escape hatch]")
    print(f"  no v1 entry in the plan/state dispatch maps:   {dispatch_clean}")
    print(f"  `plan migrate-v1` is not a verb any more:      {verb_gone}")
    if not dispatch_clean:
        failures.append("POSITIVE: a v1 dispatch entry survives, so a v1 document still has a door")
    if not verb_gone:
        failures.append("POSITIVE: the migration verb is still invocable")

    # ---- NEGATIVE CONTROL: an ABSENT version is refused too, and by name ------------------------
    # This is the arm that matters. Before the sunset an absent schema_version defaulted to v1, so an
    # unreadable file became a silently-misread one. A gate that only pattern-matched "v1" would let this
    # through.
    versionless = {"profile": "normal", "objective": "anything"}
    message = _refusal(dag.plan_version, versionless)
    says_what = bool(message) and ("schema_version" in message or "version" in message)
    print("\n[NEGATIVE CONTROL — a document that does not say what it is]")
    print(f"  refused rather than assumed to be v1:         {bool(message)}")
    print(f"  the refusal names the missing declaration:    {says_what}")
    if not message:
        failures.append("NEGATIVE CONTROL: a versionless document was accepted — the old v1 default survives")
    elif not says_what:
        failures.append("NEGATIVE CONTROL: the refusal did not name the missing version declaration")

    # ---- The one kept surface, asserted PRESENT ------------------------------------------------
    kept = "build-plan.v1" in getattr(plan_contract, "BUILD_PLAN_SCHEMAS", {})
    print("\n[THE ONE KEPT SURFACE]")
    print(f"  plan_contract still READS a stored v1 payload: {kept}")
    print("  (deliberate: a plan already in a library stays readable; issue 1070 carries its removal)")
    if not kept:
        failures.append(
            "the read-only v1 payload map is gone. If that was intentional it belongs with issue 1070 and "
            "this demo should have been updated in the same change; if it was not, a stored v1 plan in a "
            "deployed library just became unreadable")

    print("\n" + "=" * 78)
    if failures:
        print("DEMO FAILED:")
        for f in failures:
            print(f"  - {f}")
        print("=" * 78)
        return 1
    print("DEMO PASSED — v1 is refused by name at every door, the converter is gone, and the one kept")
    print("read-only surface is still there for the plans that need it.")
    print("=" * 78)
    return 0


def _parser_verbs() -> str:
    """The parser's own text, as the cheapest honest way to ask whether a verb is still registered
    without constructing and introspecting the whole subparser tree."""
    with open(bc.__file__, encoding="utf-8") as fh:
        return fh.read()


if __name__ == "__main__":
    sys.exit(main())
