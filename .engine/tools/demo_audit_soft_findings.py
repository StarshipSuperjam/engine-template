#!/usr/bin/env python3
"""Behavioral demo (issue #273 half 2): prove the audit soft-findings FEED surfaces a real firing
soft nudge end-to-end — real check rules, the real collect seam, a real over-budget operation file
on disk — not a stubbed render. Falsifiable both ways: if the feed stopped carrying a firing nudge
(a rule dropped from audit-prep, the collect seam regressed, the soft filter over-broadened) the
first assertion fails; if the feed reported a finding that is no longer firing, the second fails.

Fakes nothing: it writes a genuinely over-budget, well-formed operation under .engine/operations/,
runs the real tool, asserts the nudge is in the feed, removes the fixture, and asserts the feed then
clears. CONSTRUCTION EVIDENCE walled from travel (the first-run retirement set) — it proves the build,
it is not a shipped operator capability; standing regression coverage is test_audit_soft_findings.py
(render logic) plus the suite-membership tests."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import audit_soft_findings as asf  # noqa: E402
import validate  # noqa: E402

FIXTURE = os.path.join(validate.ROOT, ".engine", "operations", "_demo_273_overbudget.md")


def main() -> int:
    # A genuinely over-budget operation: well-formed shape (Purpose/Steps/Done when) so the only
    # finding is the soft length nudge, and > the 120-line default operations budget.
    body = ("## Purpose\np\n## Steps\n"
            + "\n".join(f"step line {i}" for i in range(140))
            + "\n## Done when\nd\n")
    with open(FIXTURE, "w", encoding="utf-8") as fh:
        fh.write(body)
    try:
        feed = asf.render()
    finally:
        if os.path.exists(FIXTURE):
            os.remove(FIXTURE)
    assert "_demo_273_overbudget.md" in feed and "over its 120-line budget" in feed, \
        f"the feed did not surface the firing over-budget nudge:\n{feed}"

    # With the fixture gone, the feed must not still claim it fires — the signal tracks reality.
    feed_after = asf.render()
    assert "_demo_273_overbudget.md" not in feed_after, \
        f"the feed reported a finding that is no longer firing:\n{feed_after}"

    print("OK — the audit soft-findings feed surfaced the firing over-budget nudge end-to-end "
          "(real rules + real file), and cleared once the file was gone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
