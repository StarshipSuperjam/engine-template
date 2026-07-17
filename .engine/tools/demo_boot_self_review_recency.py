#!/usr/bin/env python3
"""Operator-runnable demo of boot's self-review recency line — the positive "the engine last reviewed its own
health N days ago" readout a returning operator sees when the scheduled self-review is current.

It answers a question a non-engineer can't read code to verify: *when my engine HAS been reviewing its own
health on schedule, does it show me so on the way in — not only nag me once it has stopped?*

It runs the REAL logic end-to-end — audit_digest.staleness() reading THIS repo's committed self-review file,
and boot.render_dashboard rendering the finding it returns — with only the clock pinned (to a fixed date a few
days after the file's own run-date) so the demo exercises the current-digest path deterministically instead of
drifting with the wall clock. It touches nothing and needs no network. The one boundary it fakes is the other
boot status signals a live session would read alongside the freshness one.

It shows, and CHECKS (so this demo can FAIL — it is a falsification, not a showcase):
  * CURRENT -> A POSITIVE LINE — a current digest makes staleness() return a plain "last reviewed ... N days
    ago" recency line, and boot renders it among the informational readouts (after "Recently shipped"), NOT in
    the needs-attention body (a healthy signal is not an attention item);
  * STALE -> THE NUDGE, NOT THE LINE — the same seam, given a stale finding, renders the re-arm nudge in the
    needs-attention body and shows no positive recency line.

Run: uv run --directory .engine -- python tools/demo_boot_self_review_recency.py
"""
from __future__ import annotations
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import audit_digest        # noqa: E402
import boot                # noqa: E402
import engine_status       # noqa: E402
import validate            # noqa: E402


def _healthy_signals(audit_stale):
    """A complete, valid all-clear signals dict — reusing the maintained example so it stays in step with
    render_dashboard — carrying the self-review finding under test and nothing else needing attention."""
    s = dict(engine_status._EXAMPLE_SIGNALS)
    s.update({"gate": "on", "reason": None, "finding_count": 0, "register": "",
              "att_lines": [], "audit_stale": audit_stale})
    return s


def _index_of(lines, predicate, label):
    for i, ln in enumerate(lines):
        if predicate(ln):
            return i
    raise AssertionError(f"expected to find {label} in the rendered dashboard")


def main() -> int:
    failures: list[str] = []
    digest = audit_digest.AUDIT_DIGEST_PATH

    print("=== Current self-review -> a positive recency line, among the informational readouts ===")
    # Pin the clock a few days after the committed file's OWN run-date, so this always exercises the
    # current-digest path however much real time has passed since the digest was sealed.
    fm, _body = audit_digest.split(digest)
    run_date = datetime.date.fromisoformat(audit_digest._iso(fm.get("generated")))
    now = run_date + datetime.timedelta(days=3)
    current = audit_digest.staleness(digest, now=now)
    print(f"  staleness() at {now.isoformat()}: {current['severity']} -> {current['message']!r}\n")
    if current["severity"] != "note":
        failures.append("a committed, in-window digest must read as current (a `note`)")
    if "last reviewed" not in current["message"]:
        failures.append("the current-digest message must read as a positive 'last reviewed ...' recency line")

    body = boot.render_dashboard(_healthy_signals(current))
    print(body + "\n")
    if current["message"] not in body:
        failures.append("boot must surface the current-digest recency line")
    else:
        lines = body.splitlines()
        shipped = _index_of(lines, lambda ln: ln.startswith("### Recently shipped"), "'Recently shipped'")
        marker = _index_of(lines, lambda ln: current["message"] in ln, "the recency line")
        if marker < shipped:
            failures.append("the recency line must sit among the informational readouts (after 'Recently "
                            "shipped'), never in the needs-attention body above it")

    print("=== Stale self-review -> the re-arm nudge in needs-attention, and NO positive recency line ===")
    stale = validate.finding("soft", "STALE-MARKER: the engine hasn't reviewed its own health in 99 days", None)
    body2 = boot.render_dashboard(_healthy_signals(stale))
    print(body2 + "\n")
    lines2 = body2.splitlines()
    attention = _index_of(lines2, lambda ln: ln.startswith("### Needs your attention"), "'Needs your attention'")
    shipped2 = _index_of(lines2, lambda ln: ln.startswith("### Recently shipped"), "'Recently shipped'")
    if "STALE-MARKER" not in body2:
        failures.append("a stale finding must still surface the re-arm nudge")
    else:
        nudge = _index_of(lines2, lambda ln: "STALE-MARKER" in ln, "the stale nudge")
        if not attention < nudge < shipped2:
            failures.append("the stale nudge belongs in the needs-attention body, above 'Recently shipped'")

    if failures:
        print("DEMO FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All checks passed: a current self-review shows a positive recency line among the informational "
          "readouts, and a stale one shows the re-arm nudge in needs-attention with no positive line.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
