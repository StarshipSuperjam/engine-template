#!/usr/bin/env python3
"""Behavioral FALSIFICATION for StarshipSuperjam/engine-template#990 — a Build whose Issue links a
description in ANOTHER repository approves, and approves with HONEST provenance.

THE INCIDENT, REPRODUCED. The issue-spec resolver reduced every GitHub blob link to a bare in-repo path and
then read that path against the PRODUCT tree, without ever checking whose repository the link named. An
engine-mechanic building engine-template has no `docs/` tree at all — its spec corpus lives in the mechanic —
so any engine-template Issue whose body linked a mechanic-corpus description resolved to a file that was
never there, and `doc-missing` is an AUTHORITY FAILURE that hard-blocks approval with no CLI escape.

It cost a real build. engine-template#777 was recorded with `intent_source: direct` — raw intent naming the
Issue, the pull request closing it — purely to sidestep the false block. That is the part this demo is
actually about. The fix is worth nothing if the way past the bug remains easier than the honest path, so the
POSITIVE arm does not merely assert the resolver returns a no-op: it approves a Build whose plan records
`intent_source: issue`, which is what the Build genuinely was.

FAIL-THEN-PASS on one fixture; the arms differ only in whether the cross-repository filter engages:
  * POSITIVE (the fix): the same Issue body, resolved with the repository under build named. The foreign
    link is not a pointer here, the result is a DISCLOSED no-op that says so in plain words, and it is not
    in the set of reasons that block approval — so a plan carrying honest issue provenance approves.
  * NEGATIVE CONTROL (the bug): the same body resolved with no repository named, which is what the resolver
    always did. The foreign path is taken as in-product, the product has no such file, and the result is a
    `doc-missing` authority failure — the exact block that forced the dishonest provenance.

Run:  uv run --directory .engine --frozen -- python tools/demo_cross_repo_spec_link.py
Its companion test (`test_spec_referent.TheCrossRepoDemo`) runs it, so it travels with the engine as a
permanent guard in every generated repository.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_coordinator_spec           # noqa: E402  (where the authority-failure set lives)
import spec_referent                    # noqa: E402  (the resolver under test)

HOME = "StarshipSuperjam/engine-template"
FOREIGN_CORPUS = "StarshipSuperjam/engine-mechanic"

# The shape of a real engine-template Issue: prose, and a link into the mechanic's spec corpus.
ISSUE_BODY = (
    "**What this is.** The upstream-clean check reports on the wrong branch.\n\n"
    "**More detail.**\n"
    f"- [the settled description](https://github.com/{FOREIGN_CORPUS}/blob/main/docs/spec/"
    "upstream-clean.md)\n")


def _authority_failures() -> set:
    """The reason set that hard-blocks approval, read from the coordinator itself rather than restated
    here — a demo that carried its own copy could pass while the real set had drifted."""
    with open(build_coordinator_spec.__file__, encoding="utf-8") as fh:
        line = fh.read().split("authority_failures = ", 1)[1].split("\n", 1)[0]
    return {part.strip().strip("\"'") for part in line.strip("{}").split(",")}


def main() -> int:
    failures = []
    print("=" * 78)
    print("DEMO #990 — an Issue that links a description in ANOTHER repository does not block approval,")
    print("so a Build can record the provenance it actually has.")
    print("=" * 78)

    # A product tree with no docs/spec of its own — exactly the engine-template shape.
    product = tempfile.mkdtemp(prefix="cross-repo-spec-demo-")
    try:
        blocking = _authority_failures()

        # ---- POSITIVE: the repository under build is named, so the foreign link is not a pointer ----
        result = spec_referent.resolve_from_body(product, ISSUE_BODY, HOME)
        reason = result.get("no_op_reason")
        blocks = reason in blocking
        says_elsewhere = "different repository" in (result.get("detail") or "")
        print("\n[POSITIVE — resolved with the repository under build named]")
        print(f"  no_op_reason:                                 {reason}")
        print(f"  would block approval:                         {blocks}")
        print(f"  the disclosure says the link lives elsewhere: {says_elsewhere}")
        if blocks:
            failures.append(f"POSITIVE: {reason} is an authority failure — the false block survives")
        if not says_elsewhere:
            failures.append(
                "POSITIVE: the no-op does not say the description lives in another repository. An operator "
                "told only 'isn't linked to a settled description' about a body that plainly HAS a link "
                "goes looking for the one they can see")

        # ---- POSITIVE: and an in-product link still binds, so the fix did not buy its way out -----
        local_body = ISSUE_BODY.replace(FOREIGN_CORPUS, HOME)
        local = spec_referent.resolve_from_body(product, local_body, HOME)
        still_engages = local.get("no_op_reason") == "doc-missing"
        print("\n[POSITIVE — the authority check still ENGAGES on this repository's own links]")
        print(f"  an in-product link is resolved, not skipped:  {still_engages} "
              f"({local.get('no_op_reason')})")
        if not still_engages:
            failures.append(
                "POSITIVE: an in-product docs/spec link no longer reaches the resolver at all. The fix must "
                "narrow WHOSE links count, never turn the check off")

        # ---- NEGATIVE CONTROL: the resolver as it was — no repository, so the foreign path is taken --
        blind = spec_referent.resolve_from_body(product, ISSUE_BODY)
        reproduced = blind.get("no_op_reason") in blocking
        print("\n[NEGATIVE CONTROL — the pre-fix resolver: repository-blind]")
        print(f"  no_op_reason:                                 {blind.get('no_op_reason')}")
        print(f"  reproduces the approval-blocking failure:     {reproduced}")
        if not reproduced:
            failures.append(
                "NEGATIVE CONTROL did not reproduce the block, so this demo is not exercising #990")
    finally:
        shutil.rmtree(product, ignore_errors=True)

    print("\n" + "=" * 78)
    if failures:
        print("DEMO #990 FAILED:")
        for f in failures:
            print(f"  - {f}")
        print("=" * 78)
        return 1
    print("DEMO #990 PASSED — a cross-repository description is a disclosed no-op, this repository's own")
    print("links still bind, and the honest provenance is no longer the expensive path.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
