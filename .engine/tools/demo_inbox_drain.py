#!/usr/bin/env python3
"""Behavioral demo for the findings-inbox drain wired into production (issue #412). It drives the REAL
telemetry functions against a fake GitHub (only the network is faked) and real temp files, showing the
degraded-condition → durable-tracked-finding loop end to end:

  (1) BROKEN RUNTIME: the hook launcher couldn't start the engine's Python and left a presence marker; on a
      healthy session the drain driver promotes it into ONE tracked "could-not-run" finding IMMEDIATELY (a
      trust-critical signal is never persistence-gated) and clears the marker.
  (2) INBOX DRAIN: a benign degraded finding a producer emitted out-of-band spools, and a later drain promotes
      it once it has persisted — while an unrelated `ci/` issue is never touched (authoritative-scoped).
  (3) STRANDED ASIDE: a crashed drain's mtime-stale `*.draining` batch is swept back and promoted, so nothing
      is lost — but a FRESH aside (a possible concurrent live drain) is never scavenged.

Nothing is faked but the network. Every case asserts, and the self-check at the end is the falsification
(a regression that broke the immediacy, the authoritative-scoping, or the sweep's age-gate returns 1).

Run:
  uv run --directory .engine --frozen -- python tools/demo_inbox_drain.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import telemetry  # noqa: E402  (the real surface under test)

_NOW = "2026-06-05T01:00:00Z"


def _gh():
    fake = telemetry._FakeGitHub()
    return fake, telemetry.GitHubIssues("o/r", "tok", transport=fake.transport)


def _open_sids(fake):
    return {telemetry.parse_source_id(i["body"]) for i in fake.issues.values() if i["state"] == "open"}


def _benign(sid="boot/refused-cursor"):
    return {"source_id": sid, "severity": telemetry.PERSISTENT_BENIGN, "location": None,
            "message": "the engine's saved place could not be trusted", "first_seen": _NOW, "last_seen": _NOW}


def demo() -> int:
    print("findings-inbox drain wired into production (#412) — driving the REAL telemetry surface\n")
    ok = True

    # (1) broken-runtime marker -> immediate trust-critical promote
    with tempfile.TemporaryDirectory() as td:
        fake, gh = _gh()
        marker = os.path.join(td, "runtime-health.marker")
        open(marker, "w", encoding="utf-8").close()
        promoted = telemetry.promote_runtime_marker(gh, marker_path=marker)
        tracked = telemetry.RUNTIME_UNHEALTHY_SOURCE_ID in _open_sids(fake)
        cleared = not os.path.exists(marker)
        good = promoted and tracked and cleared
        print(f"  (1) broken runtime          -> {'promoted at once + marker cleared' if good else 'REGRESSION'} "
              f"(tracked={tracked}, cleared={cleared})")
        ok = ok and good

    # (2) benign spool -> drain promotes after persistence; an unrelated ci/ issue is untouched
    with tempfile.TemporaryDirectory() as td:
        fake, gh = _gh()
        spool = os.path.join(td, "findings-inbox.ndjson")
        cache = telemetry.Cache(os.path.join(td, "inbox-streams.json"))
        telemetry.promote_finding(gh, {"source_id": "ci/build", "severity": telemetry.PERSISTENT_BENIGN,
                                       "message": "a required check is red", "location": None}, _NOW)
        n = int(telemetry.load_thresholds().get("persistence", 3))
        for _ in range(n):
            telemetry.emit_finding(_benign(), spool_path=spool)
            telemetry.drain_inbox(gh, cache=cache, thresholds=telemetry.load_thresholds(), now=_NOW, spool_path=spool)
        sids = _open_sids(fake)
        good = "boot/refused-cursor" in sids and "ci/build" in sids
        print(f"  (2) benign inbox drain      -> {'promoted; ci/ untouched' if good else 'REGRESSION'} "
              f"(drained sid tracked={'boot/refused-cursor' in sids}, ci/ safe={'ci/build' in sids})")
        ok = ok and good

    # (3) a mtime-stale stranded aside is swept + drained; a fresh aside is left alone
    with tempfile.TemporaryDirectory() as td:
        fake, gh = _gh()
        spool = os.path.join(td, "findings-inbox.ndjson")
        cache = telemetry.Cache(os.path.join(td, "inbox-streams.json"))
        stale = f"{spool}.99999.draining"
        with open(stale, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(_benign("hooks/fail-open/PreToolUse/crash")) + "\n")
        old = time.time() - 10_000
        os.utime(stale, (old, old))
        fresh = f"{spool}.88888.draining"
        with open(fresh, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(_benign("scent/index-unavailable")) + "\n")
        swept = telemetry._sweep_stranded_asides(spool)   # re-appends the stale batch to the live spool (once)
        n = int(telemetry.load_thresholds().get("persistence", 3))
        for _ in range(n):   # a persistent degradation re-emits each pass; the recovered record promotes at persistence
            telemetry.emit_finding(_benign("hooks/fail-open/PreToolUse/crash"), spool_path=spool)
            telemetry.drain_inbox(gh, cache=cache, thresholds=telemetry.load_thresholds(), now=_NOW, spool_path=spool)
        recovered = "hooks/fail-open/PreToolUse/crash" in _open_sids(fake)
        fresh_left = os.path.exists(fresh)
        good = swept == 1 and recovered and fresh_left
        print(f"  (3) stranded-aside sweep    -> {'stale recovered, fresh left alone' if good else 'REGRESSION'} "
              f"(swept={swept}, recovered={recovered}, fresh_untouched={fresh_left})")
        ok = ok and good

    print("\nself-check:", "PASS" if ok else "FAIL — the inbox drain loop regressed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(demo())
