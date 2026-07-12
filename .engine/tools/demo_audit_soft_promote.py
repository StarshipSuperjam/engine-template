#!/usr/bin/env python3
"""Behavioral demo for the soft-finding promoter (issue #273 half 2, slice 2): a STANDING length-budget
finding becomes a deduped, lane-aware engine issue. The network (GitHub) is the only thing faked — the
over-budget file is REAL, and the collect -> filter-to-budget -> derive-lane -> render -> promote/dedup
logic all runs for real. It can fail: every step below asserts, and the self-check at the end is the
falsification.

What it shows, on a REAL temporary over-budget doc fixture:
  (1) the real collect picks the over-budget file up as a soft length-budget finding, and budget_records
      turns it into exactly one tracked record;
  (2) the LOCAL lane (the file is unclaimed — an operator-authored doc) renders WITHOUT the upstream
      caveat and offers the trim/raise/leave choice;
  (3) the MACHINERY lane (the same file, with ownership injected to simulate a template-shipped file)
      renders WITH the plain upstream-durability caveat the operator asked for;
  (4) promoting a record opens exactly one issue, and re-promoting the same finding UPDATES it rather than
      opening a duplicate (source-keyed dedup holds);
  (5) the rendered machinery issue body, exactly as it would appear in the tracker (read it for jargon).

Run:
  uv run --directory .engine --frozen -- python tools/demo_audit_soft_promote.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402  (ROOT)
import telemetry         # noqa: E402  (the real GitHubIssues + the demo's _FakeGitHub network stand-in)
import audit_soft_promote as asp  # noqa: E402

_FIXTURE_REL = ".engine/docs/_demo_soft_promote_fixture.md"
_UPSTREAM_PHRASE = "engine-template project"   # the load-bearing machinery-lane caveat marker
_CLOCK = "2026-06-05T01:00:00Z"


def _write_over_budget_fixture(path: str) -> None:
    # doc-shape's length budget is 200 lines; write well past it so the soft nudge fires. The section
    # shape is irrelevant here — any hard structural findings are filtered out; only the soft budget
    # finding is promoted.
    lines = ["## What this covers", "A throwaway fixture used only by this demo.", ""]
    lines += [f"Filler line {n} — padding this file past its length budget on purpose." for n in range(1, 221)]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _demo() -> int:
    abs_path = os.path.join(validate.ROOT, _FIXTURE_REL)
    print("SOFT-FINDING PROMOTER DEMO — real over-budget file + real logic, fake in-memory GitHub "
          "(no real issues, no token).\n")
    ok = True
    try:
        _write_over_budget_fixture(abs_path)

        # (1)+(2) Real collect + real (live) ownership: a brand-new doc is unclaimed -> the LOCAL lane.
        local_recs = asp.budget_records(_CLOCK)
        mine = [r for r in local_recs if r["source_id"] == f"{asp.SOURCE_PREFIX}{_FIXTURE_REL}"]
        print(f"(1) The real validator found the over-budget file; budget_records produced "
              f"{len(mine)} tracked record for it (source_id = {asp.SOURCE_PREFIX}{_FIXTURE_REL}).")
        one_local = len(mine) == 1
        local_has_caveat = one_local and _UPSTREAM_PHRASE in mine[0]["body_core"]
        print(f"(2) LOCAL lane (file unclaimed): upstream caveat present? {local_has_caveat}  "
              f"(expected False — a file you own is fixable here).")

        # (3) Same real finding, ownership INJECTED to simulate a template-shipped (machinery) file.
        mach_recs = asp.budget_records(_CLOCK, claims={_FIXTURE_REL: ["core"]})
        mrec = next(r for r in mach_recs if r["source_id"] == f"{asp.SOURCE_PREFIX}{_FIXTURE_REL}")
        mach_has_caveat = _UPSTREAM_PHRASE in mrec["body_core"]
        print(f"(3) MACHINERY lane (ownership injected): upstream caveat present? {mach_has_caveat}  "
              f"(expected True — a local trim is overwritten on the next upgrade).")

        # (4) Promote the machinery record, then re-promote it — dedup must hold (one issue, then updated).
        fake = telemetry._FakeGitHub()
        gh = telemetry.GitHubIssues("you/your-project", "demo-token", transport=fake.transport)
        n1 = telemetry.promote_finding(gh, mrec, _CLOCK, title=mrec["title"], body_core=mrec["body_core"])
        open_after_1 = sum(1 for i in fake.issues.values() if i["state"] == "open")
        n2 = telemetry.promote_finding(gh, mrec, _CLOCK, title=mrec["title"], body_core=mrec["body_core"])
        open_after_2 = sum(1 for i in fake.issues.values() if i["state"] == "open")
        print(f"(4) Promote -> open issues: {open_after_1}; re-promote the SAME finding -> open issues: "
              f"{open_after_2} (still one — dedup holds, same issue #{n1} updated, n2=#{n2}).")
        dedup_ok = open_after_1 == 1 and open_after_2 == 1 and n1 == n2

        # (5) The real machinery issue, exactly as the operator would read it.
        print("\n(5) The engine-opened MACHINERY issue, as it appears in your tracker:")
        print("    ┌─ TITLE: " + mrec["title"])
        body = fake.issues[n1]["body"]
        for line in body.split("\n"):
            print("    │ " + line)
        print("    └─ (the last line is an invisible marker; it does not render in GitHub)")
        marker_ok = "<!-- engine-signal: " + mrec["source_id"] + " -->" in body

        # (6) The LIVE-DERIVED pass (F0204): a full run opens the over-budget surface's issue, then — once the
        #     file is back UNDER budget (still an evaluated surface, just no longer firing) — AUTO-RESOLVES it,
        #     while never touching another source's issue. Firing is stubbed here so the ONE surface is
        #     deterministic; budget_surfaces() (the authoritative set) still reads the REAL shape rules, and the
        #     fixture file really exists, so it is genuinely in scope. `_CLK` steps forward so the close is seen.
        fixture_sid = f"{asp.SOURCE_PREFIX}{_FIXTURE_REL}"
        _orig_collect = validate.collect
        fake2 = telemetry._FakeGitHub()
        democache = os.path.join(tempfile.mkdtemp(), "soft-budget-streams.json")   # isolated -> deterministic
        try:
            telemetry.promote_finding(telemetry.GitHubIssues("you/your-project", "demo-token",
                                      transport=fake2.transport),
                                      {"source_id": "ci/build", "severity": telemetry.PERSISTENT_BENIGN,
                                       "message": "a required check is red", "location": None}, _CLOCK)
            validate.collect = lambda s, c, **k: [
                {"severity": "soft", "source_kind": "shape", "location": {"file": _FIXTURE_REL},
                 "message": f"'{_FIXTURE_REL}' is 300 lines, over its 200-line budget."}]
            asp.promote("you/your-project", "demo-token", _CLOCK, transport=fake2.transport, cache_path=democache)
            open1 = {telemetry.parse_source_id(i["body"]) for i in fake2.issues.values() if i["state"] == "open"}
            validate.collect = lambda s, c, **k: []               # the file is trimmed -> no longer firing
            rep_clear = asp.promote("you/your-project", "demo-token", _CLOCK, transport=fake2.transport,
                                    cache_path=democache)
            open2 = {telemetry.parse_source_id(i["body"]) for i in fake2.issues.values() if i["state"] == "open"}
        finally:
            validate.collect = _orig_collect
            shutil.rmtree(os.path.dirname(democache), ignore_errors=True)
        print(f"\n(6) LIVE pass: over-budget -> the surface's issue is open ({fixture_sid in open1}); trim it "
              f"-> closed={rep_clear.closed}, its issue is gone ({fixture_sid not in open2}); the unrelated CI "
              f"issue survives ({'ci/build' in open2}).")
        resolve_ok = (fixture_sid in open1 and fixture_sid not in open2 and "ci/build" in open2)

        # (7) The permalink to "what is broken" (F0202): a debt body for a CATALOGUED surface links to it on
        #     GitHub. Shown on a real catalogued file (the fixture is a throwaway, so it has no entity to link).
        cat_rel = ".engine/operations/build-orchestration.md"
        validate.collect = lambda s, c, **k: [
            {"severity": "soft", "source_kind": "shape", "location": {"file": cat_rel},
             "message": f"'{cat_rel}' is over its budget."}]
        try:
            cat_recs = asp.budget_records(_CLOCK, repo="you/your-project")
        finally:
            validate.collect = _orig_collect
        link = f"https://github.com/you/your-project/blob/HEAD/{cat_rel}"
        link_ok = any(link in r["body_core"] for r in cat_recs if r["source_id"] == f"{asp.SOURCE_PREFIX}{cat_rel}")
        print(f"(7) A debt body for a catalogued surface links to \"what is broken\" on GitHub (F0202): {link_ok}.")

        ok = (one_local and (not local_has_caveat) and mach_has_caveat and dedup_ok and marker_ok
              and resolve_ok and link_ok)
    finally:
        try:
            os.remove(abs_path)
        except OSError:
            pass

    print("\nDone — no real issues were created; only the network was faked. The over-budget file and the "
          "promote/dedup/render LOGIC above are real; that it writes to your REAL GitHub is confirmed the "
          "first time the armed self-review runs live.")
    if not ok:
        print("\nDEMO UNEXPECTED: a lane body, the dedup, or the tracking marker did not behave as "
              "expected.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_demo())
