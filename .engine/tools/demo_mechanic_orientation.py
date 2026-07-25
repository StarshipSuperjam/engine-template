#!/usr/bin/env python3
"""Operator-runnable demo: what the engine SAYS at the start of a session when it is an engine-mechanic — one
that builds a separate, owned product checkout — and what it says when that checkout's path isn't set yet.

Run: uv run --directory .engine -- python tools/demo_mechanic_orientation.py

This exercises the REAL boot renderer (`boot.render_dashboard`), not a re-implementation: each block feeds boot
a real orientation value from the real reader shape and prints the actual card lines an operator would see.
Read the three blocks by eye:
  [1] NOT a mechanic (the ordinary deployment that builds its own repo) — boot says nothing about a product
      checkout at all. No nagging where there is nothing to set;
  [2] a mechanic whose local checkout IS set — the card names the product it builds and acknowledges that the
      local checkout is set, WITHOUT printing the machine-local path (that path names a folder on this computer,
      and a boot card is the kind of text an operator pastes when asking for help; the assistant gets the path
      through its own grounding, which is not shown to the operator);
  [3] a mechanic whose local checkout is NOT set — the fork case. The committed slug travelled with the engine,
      but the per-machine path never did, so boot pins a plain-language setup offer that teaches the whole
      first-time step (clone the product as a separate folder BESIDE this one, then point the engine at it).

Block [4] then shows the one precedence rule that keeps onboarding sane: when first-time engine setup is ALSO
unfinished, the mechanic offer holds back, so the operator gets one onboarding ask rather than two.

The demo self-checks and exits non-zero if boot does not behave as narrated (e.g. if the operator card started
leaking the absolute checkout path, or the setup offer stopped suppressing) — it is a falsification that can
fail, not a showcase.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import boot  # noqa: E402

_PRODUCT = "StarshipSuperjam/engine-template"
_CHECKOUT = "/Users/example/code/engine-template"      # a machine-local path, as a real one would look

# A complete, healthy signals dict — the shape boot.gather_signals produces, which the pure renderer consumes.
# Only the `mechanic` (and, in block [4], `first_run`) key varies between blocks, so each block isolates exactly
# the orientation behaviour and nothing else.
_BASE = {
    "state": {"schema_version": 1, "standing_situation": {}, "integration_debt": {}},
    "refused": False, "gate": "on", "reason": None, "finding_count": 0, "unrated_count": 0, "register": "",
    "total_open": None, "counts_state": "offline", "all_open_register": None,
    "blocking_findings": [], "blocking_finding_fingerprint": None,
    "debt_count": 0, "debt_as_of": None, "att_lines": [], "att_degraded": [], "shipped": [],
    "stance": "Exploring", "strand": None, "behind_origin": None, "off_main": None,
    "pr_conflict": None, "restore_offer": None, "migration_revert": None, "staged_update": None,
    "audit_stale": None, "live_standing": None, "neighborhood": None, "map_rebuilt": False,
    "map_corrupt": False, "ledger_malformed": None, "migration_stalled": False, "recall_offline": False,
    "set_aside": None, "foreign_license": None, "first_run": None, "greenfield_intake": None,
    "operator_backlog_count": None, "operator_backlog_register": None, "operator_backlog_degraded": False,
    "product_repository": None, "home_workshop": None, "mechanic": None,
}


def _signals(**over) -> dict:
    s = dict(_BASE)
    s.update(over)
    return s


def _card(**over) -> str:
    return boot.render_dashboard(_signals(**over))


def _show(card: str, keep: tuple) -> None:
    """Print only the card lines that carry the orientation, so the block reads at a glance."""
    for line in card.splitlines():
        if any(k.lower() in line.lower() for k in keep):
            print(f"    {line.strip()}")


def _not_a_mechanic_says_nothing() -> bool:
    card = _card(mechanic=None)
    ok = "what this engine builds" not in card.lower() and "separate checkout of its own" not in card.lower()
    print("    (nothing about a product checkout — this engine builds its own repo)" if ok
          else f"    UNEXPECTED: an ordinary deployment was told about a product checkout:\n{card}")
    return ok


def _resolved_names_product_without_leaking_the_path() -> bool:
    mech = {"product": _PRODUCT, "checkout": _CHECKOUT, "state": "resolved"}
    card = _card(mechanic=mech)
    names_product = _PRODUCT in card
    leaks_path = _CHECKOUT in card
    # BOTH halves of the privacy property: the path must be absent from the operator's card AND present in the
    # assistant's own grounding (without it the mechanic could not build anywhere). Testing only the first half
    # would still pass if the grounding silently stopped carrying it.
    grounding = boot.render_mechanic_grounding(mech, first_run_pending=False)
    carries_path = _CHECKOUT in grounding
    ok = names_product and not leaks_path and carries_path
    if ok:
        _show(card, ("What this engine builds", "local checkout of it is set"))
        print(f"    (the machine-local path {_CHECKOUT} is NOT on the operator's card ...")
        print("     ... and IS in the assistant's own grounding, which the operator never sees)")
    else:
        print(f"    UNEXPECTED: names_product={names_product} leaks_path={leaks_path} "
              f"grounding_carries_path={carries_path}")
    return ok


def _an_unreachable_path_keeps_asking() -> bool:
    # Under the operator's OWN home, so the card can be checked for the home-contracted form (`~/…`) rather than
    # the raw `/Users/<account>/…` — recognisable enough to correct, without the identifying part.
    typo = os.path.join(os.path.expanduser("~"), "code", "engine-template-typo")
    card = _card(mechanic={"product": _PRODUCT, "checkout": typo, "state": "path-unreachable"})
    low = card.lower()
    shortened = "~/code/engine-template-typo"
    ok = ("isn't there" in low
          and shortened in card              # shown, so a typo is fixable ...
          and typo not in card               # ... but never with the home directory spelled out
          and "local checkout of it is set" not in low)
    if ok:
        _show(card, ("isn't there",))
        print(f"    (shown as {shortened} — not {typo})")
    else:
        print(f"    UNEXPECTED: a missing folder did not keep asking as narrated:\n{card}")
    return ok


def _path_unset_offers_the_full_first_time_setup() -> bool:
    card = _card(mechanic={"product": _PRODUCT, "checkout": None, "state": "path-unset"})
    low = card.lower()
    ok = ("separate checkout of its own" in low
          and "point me at my product checkout" in low        # a spoken handle, like its neighbouring offers
          and "clone my product for me" in low                # the no-clone-yet case is actionable too
          and "beside it, never inside it" in low             # the load-bearing sibling-not-subdirectory rule
          and "product-checkout-path" in low)                 # the DURABLE seam, not a session-only env var
    if ok:
        _show(card, ("doesn't know where that checkout is yet",))
    else:
        print(f"    UNEXPECTED: the setup offer did not read as narrated:\n{card}")
    return ok


def _first_run_setup_comes_first() -> bool:
    first_run = {"present": True, "main": "/proj", "home": _PRODUCT, "own": "someone/their-fork"}
    mech = {"product": _PRODUCT, "checkout": None, "state": "path-unset"}
    card = _card(mechanic=mech, first_run=first_run)
    low = card.lower()
    one_ask = "set up my project" in low and "separate checkout of its own" not in low
    # ... and the assistant is told plainly that the offer was withheld, rather than that the operator saw one.
    grounding = boot.render_mechanic_grounding(mech, first_run_pending=True)
    honest = "is not being shown the mechanic setup offer" in grounding.lower()
    ok = one_ask and honest
    if ok:
        _show(card, ("first-time setup hasn't finished",))
        print("    (the mechanic offer waits its turn — and the assistant is told it was withheld,")
        print("     so it can't act as though you had already been asked)")
    else:
        print(f"    UNEXPECTED: one_ask={one_ask} grounding_is_honest={honest}")
    return ok


def main() -> int:
    print("=" * 78)
    print("What the engine says at session start when it builds a SEPARATE, owned product checkout.")
    print("=" * 78)

    print("\n[1] An ordinary deployment (it builds its own repo). Expect: nothing about a product checkout.")
    print("-" * 78)
    one = _not_a_mechanic_says_nothing()

    print("\n[2] A mechanic whose local checkout IS set. Expect: names the product, no machine path on the card.")
    print("-" * 78)
    two = _resolved_names_product_without_leaking_the_path()

    print("\n[3] A mechanic whose local checkout is NOT set (the fork case). Expect: a plain setup offer.")
    print("-" * 78)
    three = _path_unset_offers_the_full_first_time_setup()

    print("\n[4] A mechanic pointed at a folder that ISN'T THERE (a typo). Expect: it keeps asking, and shows")
    print("    you the value it has, so you can correct it — never 'all set'.")
    print("-" * 78)
    four = _an_unreachable_path_keeps_asking()

    print("\n[5] Path not set AND first-time engine setup unfinished. Expect: ONE onboarding ask, not two.")
    print("-" * 78)
    five = _first_run_setup_comes_first()

    ok = one and two and three and four and five
    print("\n" + "=" * 78)
    print("In plain words: an engine that builds a separate product says so at the start of a session, and if it")
    print("doesn't yet know where your copy of that product lives — or the folder it has isn't there — it asks")
    print("you, in plain language, and tells you how to get one. When everything is fine the card shows no folder")
    print("path at all; the one time it does is when the folder is missing and you need to see it to fix it, and")
    print("even then it is shortened to `~/...` so your account name isn't on a card you might paste to someone.")
    print("The path never travels with the project when a colleague forks it — they set only their own.")
    print("DEMO OK" if ok else "DEMO FAILED -- boot did not orient as narrated (a behaviour may have regressed)")
    print("=" * 78)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
