#!/usr/bin/env python3
"""The recall acceptance probe — the DESIRED behavior, kept where the standard suite will not run it.

This module is deliberately not a test module (no `test_` prefix, so `unittest discover -p 'test_*.py'` never
collects it) and it asserts nothing. It holds one judgment: given what a launched memory server answered,
did meaning-based recall return the seeded conversation? That is the acceptance target the fix child of
program prg_d15d7dc8f3df will run explicitly and assert; here, in C1, it is only EVALUATED and RECORDED with
its actual result by the disposable-clone reproduction harness (`test_candidate_invocation.py`), never
asserted and never skipped — a green C1 suite says the instrument and the harness work, not that recall is
repaired.

Why a separate module rather than a test the suite skips: a skipped or expected-failure test is a claim the
suite cannot see through, and an exclusion glob is a claim that rots. A module the suite does not collect,
imported by name by the harness that records its result and by the fix child that will assert it, is neither.
"""
from __future__ import annotations


def recall_acceptance_probe(observation: dict, nonce: str) -> dict:
    """Evaluate the desired behavior over one launcher's observation (see `drive_launcher`): meaning-based
    recall answered, was available, and returned the seeded conversation carrying `nonce`. Returns
    `{"passed": bool, "detail": str}` — the actual result, whatever it is."""
    entry = observation.get("tools", {}).get("recall-by-meaning")
    if entry is None:
        return {"passed": False, "detail": "recall-by-meaning was not offered by the launched server"}
    if entry.get("is_error"):
        return {"passed": False, "detail": "recall-by-meaning answered with an error"}
    payload = entry.get("payload") or {}
    if payload.get("unavailable"):
        return {"passed": False,
                "detail": f"recall-by-meaning reported itself unavailable: {payload['unavailable'][:120]}"}
    found = any(nonce in (hit.get("passage") or hit.get("text") or "") for hit in payload.get("results", []))
    return {"passed": found, "detail": "the seeded conversation was recalled by meaning" if found
            else "recall-by-meaning answered but did not return the seeded conversation"}
