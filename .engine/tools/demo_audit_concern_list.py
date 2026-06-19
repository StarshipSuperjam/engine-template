#!/usr/bin/env python3
"""Operator-runnable demonstration of the audit checklist check (engine/check/audit-concern-list).

Run it:  uv run --directory .engine -- python tools/demo_audit_concern_list.py

The engine's periodic self-review keeps a small checklist of things to look over (.engine/audits/concern-list.json).
Each entry on it must say WHY it is worth checking. This demo lets you SEE — without reading code — that the rule
guarding that checklist does what it claims: it ACCEPTS the real checklist as it ships, and CATCHES a checklist
whose entry has lost its reason-for-being.

It uses the engine's own schema (.engine/schemas/concern-list.v1.json) and the very same validator the real check
runs, on the real committed checklist and on a deliberately-broken copy of it (nothing real is touched). Vary it
yourself: open the checklist, change or remove a field, and re-run — the verdict follows.
"""
from __future__ import annotations
import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402  (the engine's own schema loader + repo ROOT)

BANNER = "=" * 78
SCHEMA_REL = ".engine/schemas/concern-list.v1.json"
LIST_REL = ".engine/audits/concern-list.json"


def _errors(doc, schema) -> list:
    """The real schema verdict: the messages the engine's validator (JSON Schema 2020-12) raises, or []."""
    from jsonschema import Draft202012Validator  # the same dialect kind_schema validates with
    return [e.message for e in Draft202012Validator(schema).iter_errors(doc)]


def main() -> int:
    schema = validate.load_json(os.path.join(validate.ROOT, SCHEMA_REL))
    real = validate.load_json(os.path.join(validate.ROOT, LIST_REL))

    print(BANNER)
    print("What this checks: the self-review's checklist must be well-formed, and every entry on it must")
    print("carry its reason-for-being. A checklist that drifts — an entry that lost its reason — would let")
    print("the review's targets rot unnoticed. This rule catches that before it can merge.")
    print(BANNER)

    print("\n[1] The checklist exactly as it ships. Expect: accepted (GREEN).")
    print("-" * 78)
    errs = _errors(real, schema)
    ok1 = errs == []
    print(f"   problems found: {len(errs)}   accepted the real checklist? {ok1}")

    print("\n[2] The same checklist with one entry's reason-for-being removed. Expect: caught (RED).")
    print("-" * 78)
    broken = copy.deepcopy(real)
    removed = None
    if broken.get("concerns"):
        removed = broken["concerns"][0].pop("justification", None)
    errs = _errors(broken, schema)
    ok2 = any("justification" in m for m in errs)
    print(f"   problems found: {len(errs)}   caught the entry with no reason? {ok2}")
    if errs:
        print("   what the change's author would be told:")
        print("     " + errs[0])

    print("\n" + BANNER)
    print("In plain words: the rule accepts the checklist as it ships, and goes red — naming the exact")
    print("problem — the moment an entry loses the reason that earns its place. It reads the checklist as")
    print("plain data against the engine's own schema; it changes nothing.")
    ok = ok1 and ok2 and removed is not None
    print(f"DEMO {'OK' if ok else 'FAILED'}")
    print(BANNER)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
