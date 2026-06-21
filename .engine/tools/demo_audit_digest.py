#!/usr/bin/env python3
"""Operator-runnable demonstration of the two rules that protect the engine's self-review file
(engine/check/audit-digest-fingerprint and engine/check/audit-digest-staleness).

Run it:  uv run --directory .engine -- python tools/demo_audit_digest.py

The engine's periodic self-review writes a short, plain-language file — .engine/audits/audit-digest.md —
that says what it looked at, what it found, and what it recommends. Two rules protect it: one catches the
file being quietly edited by hand after the audit wrote it, and one warns when the self-review has gone too
long without running (or hasn't run yet). This demo lets you SEE — without reading code — that both rules
do what they claim.

It works entirely on a THROWAWAY copy in a temp folder; your real repo is never touched. It uses the very
same logic the real rules run. Vary it yourself: change the sample text, change the dates, and re-run — the
verdicts follow. The sample below is also a fair picture of what a real self-review file reads like.
"""
from __future__ import annotations
import datetime
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate       # noqa: E402
import audit_digest   # noqa: E402

BANNER = "=" * 78

# A realistic sample self-review, in the plain register a real digest uses (this is what a non-engineer
# would open and read in the repo).
SAMPLE_BODY = """# Engine self-review

I reviewed your saved decisions, open debts, and how the installed parts are fitting together.

**What I found**
- Three saved decisions now contradict newer ones — here's which, and what I'd drop. (#231)
- One open debt has sat untouched for 90 days and looks abandoned. (#232)

**What looks healthy**
- Your memory, project state, and knowledge map are all current.

Nothing here changes anything on its own — each item above is a suggestion you decide on.
"""


def _verdict(f: dict) -> str:
    return {"hard": "RED", "soft": "FLAGGED", "note": "clear"}.get(f["severity"], f["severity"])


def main() -> int:
    today = datetime.date.today()
    ok = True
    with tempfile.TemporaryDirectory() as d:
        digest = os.path.join(d, "audit-digest.md")

        print(BANNER)
        print("What this checks: the engine's self-review file must be exactly what the audit wrote (not")
        print("quietly hand-edited afterwards), and it must be reasonably recent. Two rules enforce that.")
        print(BANNER)

        print("\n[1] Before any self-review has run, there is no file yet.")
        print("-" * 78)
        f = audit_digest.check(digest)
        s = audit_digest.staleness(digest, now=today)
        print(f"   seal rule:      {_verdict(f)}   ({validate.fmt(f)})")
        print(f"   freshness rule: {_verdict(s)}   ({validate.fmt(s)})")
        step1 = f["severity"] == "note" and s["severity"] == "soft"
        print(f"   expected: seal rule passes (nothing to verify), freshness says 'hasn't run yet' -> {step1}")
        ok = ok and step1

        print("\n[2] The self-review runs today and writes its file. Both rules should be happy.")
        print("-" * 78)
        audit_digest.seal(digest, generated=today, body=SAMPLE_BODY)
        print("   --- what got written (this is what you would open in the repo) ---")
        for line in validate.read(digest).splitlines():
            print("   | " + line)
        f = audit_digest.check(digest)
        s = audit_digest.staleness(digest, now=today)
        print(f"\n   seal rule:      {_verdict(f)}   ({validate.fmt(f)})")
        print(f"   freshness rule: {_verdict(s)}   ({validate.fmt(s)})")
        step2 = f["severity"] == "note" and s["severity"] == "note"
        print(f"   expected: both clear -> {step2}")
        ok = ok and step2

        print("\n[3] Now someone hand-edits the committed file after the fact. The seal rule should catch it.")
        print("-" * 78)
        with open(digest, "a", encoding="utf-8", newline="") as fh:
            fh.write("\n(a line slipped in by hand that the audit never wrote)\n")
        f = audit_digest.check(digest)
        print(f"   seal rule: {_verdict(f)}   ({validate.fmt(f)})")
        step3 = f["severity"] == "hard"
        print(f"   expected: caught (RED) -> {step3}")
        ok = ok and step3

        print(f"\n[4] A self-review that ran but then stopped: its file is now {audit_digest.STALENESS_DAYS + 60}"
              " days old.")
        print("-" * 78)
        old = today - datetime.timedelta(days=audit_digest.STALENESS_DAYS + 60)
        audit_digest.seal(digest, generated=old, body=SAMPLE_BODY)
        s = audit_digest.staleness(digest, now=today)
        print(f"   freshness rule: {_verdict(s)}   ({validate.fmt(s)})")
        step4 = s["severity"] == "soft"
        print(f"   expected: flagged (so you're told to re-arm it) -> {step4}")
        ok = ok and step4

        print("\n[5] The scheduled run's own path: the self-review writes its words to a file, and the engine")
        print("    seals THAT file in — the same --body-file step the workflow runs — and it verifies the same.")
        print("-" * 78)
        prose = os.path.join(d, "captured-prose.md")
        with open(prose, "w", encoding="utf-8", newline="") as fh:
            fh.write(SAMPLE_BODY)
        produced = os.path.join(d, "from-body-file.md")
        rc = audit_digest.main(["seal", produced, "--body-file", prose])
        f = audit_digest.check(produced)
        print(f"   sealed from the captured file -> CLI exit {rc}; seal rule: {_verdict(f)}")
        step5 = rc == 0 and f["severity"] == "note"
        print(f"   expected: sealed from the file and verifies (clear) -> {step5}")
        ok = ok and step5

        print("\n" + BANNER)
        print("In plain words: the seal rule passes on a freshly-written file and goes RED the moment the")
        print("file is hand-edited; the freshness rule stays quiet while the self-review is recent and")
        print("speaks up when it hasn't run in a while (or at all). Both read a throwaway copy and change")
        print("nothing. Your real .engine/audits/audit-digest.md was never touched.")
        print(f"DEMO {'OK' if ok else 'FAILED'}")
        print(BANNER)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
