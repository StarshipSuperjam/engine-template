#!/usr/bin/env python3
"""Demo — a deployment's own decision records get a project-namespaced name that can't clash with the engine's.

What this checks, in plain words: your project's own engine-decision records (the ones under
.engine/contracts/instance/) are named with your project in front — e.g. `acme-eADR-0007` — while the engine's
own built-in records stay `eADR-0017`. This shows, on the REAL check + knowledge-graph logic (nothing in your
project is touched — it all runs in a throwaway folder), that after #467:
  1. a well-formed project-namespaced record PASSES all three contract checks;
  2. a MALFORMED one is FLAGGED by each check (the gate bites — it doesn't just stop rejecting);
  3. a canon `eADR-0034` and a deployment `acme-eADR-0034` — the SAME number — sit side by side as two
     distinct records with no name clash.

It feeds real records to the real check dispatch (validate.kind_shape/kind_schema/kind_presence) and the real
graph derivation (knowledge_gen.derive_entities) — not a stand-in. Nothing is changed.

Run:  uv run --directory .engine -- python tools/demo_467_deployment_eadr_namespace.py
Vary it: edit the planted records / ids below and re-run.
"""
from __future__ import annotations
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate            # noqa: E402
import knowledge_gen       # noqa: E402

_GOOD_BODY = ("## Decision\nTurn on the projects-sync module.\n"
              "## Significance\nIt constrains how this deployment tracks its own work.\n"
              "## Rationale\nThe team wanted a board; the cost is another integration to keep green.\n"
              "## Anti-choice\nA spreadsheet; rejected — it would drift from the issues.\n"
              "## Status\naccepted\n")
_RULES = {n: validate.load_json(os.path.join(validate.CHECK_DIR, f"contract-{n}.json"))
          for n in ("shape", "frontmatter", "threshold")}
_KINDS = {"shape": "kind_shape", "frontmatter": "kind_schema", "threshold": "kind_presence"}


def _fm(eid: str) -> str:
    return f"---\nid: {eid}\ntitle: {eid} decision\nstatus: accepted\ndate: 2026-07-12\n---\n\n"


def _check_one(record_text: str, filename: str) -> dict:
    """Run the three REAL contract checks over a throwaway instance/ tree holding this one record."""
    with tempfile.TemporaryDirectory() as root:
        inst = os.path.join(root, ".engine", "contracts", "instance")
        os.makedirs(inst)
        with open(os.path.join(inst, filename), "w", encoding="utf-8") as fh:
            fh.write(record_text)
        with mock.patch.object(validate, "ROOT", root):
            return {n: getattr(validate, _KINDS[n])(_RULES[n], {})[0] for n in _RULES}


def _coexist() -> tuple:
    """Derive the REAL graph over a canon eADR-0034 and a deployment acme-eADR-0034; return the two entities."""
    with tempfile.TemporaryDirectory() as root:
        os.makedirs(os.path.join(root, ".engine", "contracts", "instance"))
        canon_rel = ".engine/contracts/eADR-0034-x.md"
        dep_rel = ".engine/contracts/instance/acme-eADR-0034-x.md"
        for rel, eid in ((canon_rel, "eADR-0034"), (dep_rel, "acme-eADR-0034")):
            with open(os.path.join(root, rel), "w", encoding="utf-8") as fh:
                fh.write(_fm(eid) + "## Decision\n\nA decision.\n")
        catalog = {"surfaces": {"contract": {"class": "prose", "location": ".engine/contracts/",
                                             "governing_schema": "contract.v1.json",
                                             "template": "../templates/contract.md"}}}
        with mock.patch.object(validate, "ROOT", root):
            ents = knowledge_gen.derive_entities(catalog, [], [canon_rel], {canon_rel: ["core"]},
                                                 deployment_contracts=[dep_rel])
        by_id = {e["id"]: e for e in ents}
        return by_id.get("contract:eADR-0034-x"), by_id.get("contract:instance.acme-eADR-0034-x")


def main(_argv=None) -> int:
    print("What this checks: your project's own engine decisions get a name starting with your project")
    print("(e.g. acme-eADR-0007), so they can never clash with the engine's own (e.g. eADR-0017). (issue #467)\n")
    ok = True

    good = _check_one(_fm("acme-eADR-0007") + _GOOD_BODY, "acme-eADR-0007-good.md")
    passed = all(good.values())
    ok &= passed
    print(f"  [{'OK' if passed else 'WRONG':5}] a well-formed acme-eADR-0007 -> all three checks "
          f"{'pass' if passed else 'DID NOT pass'}  (shape={good['shape']} frontmatter={good['frontmatter']} "
          f"threshold={good['threshold']})")

    broken = ("---\nid: acme-eADR-9002\ntitle: broken\nstatus: accepted\ndate: 2026-07-12\n---\n\n"
              "## Decision\nA choice with no weighed alternative.\n## Status\naccepted\n")
    bad = _check_one(broken, "acme-eADR-9002-broken.md")
    bit = (not bad["shape"]) and (not bad["threshold"])
    ok &= bit
    print(f"  [{'OK' if bit else 'WRONG':5}] a malformed acme-eADR-9002 -> flagged "
          f"{'(the gate bites)' if bit else 'NOT flagged'}  (shape={bad['shape']} threshold={bad['threshold']}; "
          "both should be flagged)")

    off = _check_one("---\nid: Acme-eADR-0003\ntitle: bad id\nstatus: accepted\ndate: 2026-07-12\n---\n\n"
                     + _GOOD_BODY, "Acme-eADR-0003-badid.md")
    off_bit = not off["frontmatter"]
    ok &= off_bit
    print(f"  [{'OK' if off_bit else 'WRONG':5}] an off-charset id (Acme-…, uppercase) -> "
          f"{'flagged by the id gate' if off_bit else 'NOT flagged'}  (frontmatter={off['frontmatter']})")

    canon, dep = _coexist()
    coexist = bool(canon and dep) and canon["id"] != dep["id"] \
        and ("provided_by" in canon["predicates"]) and ("provided_by" not in dep["predicates"])
    ok &= coexist
    print(f"  [{'OK' if coexist else 'WRONG':5}] canon eADR-0034 and deployment acme-eADR-0034 -> "
          f"{'coexist as two distinct records' if coexist else 'DID NOT coexist cleanly'}")
    if canon and dep:
        print(f"          the engine's:   {canon['id']}  (owned by the engine)")
        print(f"          your project's: {dep['id']}  (yours, kept across updates)")

    print()
    if ok:
        print("In plain words: a record named for your project passes the same well-formed-record bar the")
        print("engine holds its own to, a malformed one is caught, and your `…-0034` and the engine's `0034`")
        print("never collide. Vary it: change the ids/records above and re-run. Your project was not touched.")
        return 0
    print("This run did NOT confirm the behavior — something above came out wrong. That is a real signal")
    print("worth investigating, not a pass. Your project was not touched.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
