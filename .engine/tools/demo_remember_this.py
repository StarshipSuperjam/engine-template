#!/usr/bin/env python3
"""Operator-runnable demo of the "remember this" path in Explore (engine-template #257 + #258).

It answers, in plain words, two things a non-engineer can't read code to verify:
  1. (#257) When I ask the engine to remember something and it can't hand-edit its memory directly, does it
     refuse like a peer who *noted it* — not like it misheard me asking for a code change?
  2. (#258) Does an explicit "remember X" actually become a durable PREFERENCE the engine keeps, not a
     passing note that fades?

It runs the REAL logic end-to-end — the REAL Explore write-gate (`modes.handler`), the REAL consolidation
detector + directive (`consolidate.detect_unconsolidated` / `_consolidation_directive`), and the REAL
lock-safe store + ranked recall (`consolidate.store_episodic` / `index.query`). The ONLY thing faked is the
memory location: the whole demo runs against a THROWAWAY temp store, and it asserts your real
`.engine/memory/` is never touched. No network, no token, nothing written to your tree.

What it CANNOT show (stated honestly, like the consolidation practice run): whether a live AI, at its next
startup, actually OBEYS the directive and types your "remember X" as a preference is judged at sweep time —
here we prove the directive *instructs* it to, and that such a preference, once stored, persists and is
found.

Run: uv run --directory .engine -- python tools/demo_remember_this.py
"""
from __future__ import annotations
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import modes   # noqa: E402
from memory import ledger, capture, consolidate, index   # noqa: E402


def _gate_reason(tool_name, file_path):
    """The REAL Explore write-gate's relayed reason for a file-mutating call (session_id=None -> Explore)."""
    decision = modes.handler({"session_id": None, "tool_name": tool_name,
                              "tool_input": {"file_path": file_path}})
    return decision.get("reason", "")


def _real_store_fingerprint():
    """(exists, size) of the REAL ledger, read BEFORE we redirect to a temp store — so we can prove the
    demo leaves the real, backed-up memory untouched. Computed without the env override in effect."""
    path = ledger.ledger_path()
    try:
        return (True, os.path.getsize(path), path)
    except OSError:
        return (False, 0, path)


def main(argv: list | None = None) -> int:
    print("The 'remember this' path in Explore — respond like a peer who noted it, then keep it "
          "durably (#257 + #258).\n")
    ok = True

    # --- #257: the memory-specific denial relay (no store writes; just the real gate) -------------------
    mem_reason = _gate_reason("Write", ".engine/memory/ledger.ndjson")
    code_reason = _gate_reason("Write", ".engine/tools/some_module.py")
    print("1) You ask me to remember something, and I'd have to hand-edit my own memory to do it:")
    print(f"   memory write -> \"{mem_reason}\"\n")
    print("2) Versus a real code change I'm asked to make while exploring:")
    print(f"   code write   -> \"{code_reason}\"\n")
    # Self-checks: the memory refusal is the peer "noted", names a real correlate, and never mishears it as
    # a code change or leaks the two-store seam; the code refusal still points at building.
    if mem_reason != modes._MEMORY_DENIAL or "noted" not in mem_reason.lower() \
            or "read it back" not in mem_reason.lower() \
            or "pull request" in mem_reason.lower() or "harness" in mem_reason.lower():
        ok = False
        print("DEMO UNEXPECTED: the memory write did not get the peer 'noted' relay.\n", file=sys.stderr)
    if code_reason != modes._DENIAL or "build" not in code_reason.lower():
        ok = False
        print("DEMO UNEXPECTED: the code write lost the generic build-set denial.\n", file=sys.stderr)

    # --- #258: durable preference capture, entirely on a throwaway store --------------------------------
    before = _real_store_fingerprint()                       # capture the REAL store BEFORE redirecting
    prev_env = os.environ.get(ledger.ENV_DIR)
    with tempfile.TemporaryDirectory() as tmp:
        os.environ[ledger.ENV_DIR] = tmp                    # every memory call below hits the temp store
        try:
            past, live = "demo-past-session", "demo-live-session"
            note = "always use snake_case for new file names"
            ledger.append(capture._make_record(past, 1, "user", f"remember to {note}"))
            ledger.append(capture._make_record(live, 1, "user", "(this session, still live)"))

            pending = consolidate.detect_unconsolidated(live_session_id=live)
            directive = consolidate._consolidation_directive(pending).lower()
            says_pref = "remember" in directive and "preference" in directive and "never dropped" in directive
            print("3) When a past session is tidied up later, your 'remember X' is kept as a lasting")
            print("   preference — not folded into other notes and not dropped:")
            print(f"   a past session is waiting to be tidied: {'yes' if past in pending else 'no'} "
                  f"(this live session is left alone: {'yes' if live not in pending else 'no'})")
            print(f"   'remember X' is kept as a durable preference: {'yes' if says_pref else 'no'}\n")

            # The sweep's store (on the PAST session — never the live one, which would strand its later notes).
            consolidate.store_episodic(past, [{"role": "preference", "text": note}])
            hits = [r for r in index.query("snake_case").records
                    if r.get("role") == "preference" and r.get("kind") == consolidate.EPISODIC_KIND]
            print("4) You ask me later what I remembered, and I read it back:")
            print(f"   what I'd read back to you: {[r.get('text') or r.get('body') for r in hits]}\n")

            live_marked = any(r.get("kind") == consolidate.MARKER_KIND and r.get("session_id") == live
                              for r in ledger.iter_records())
            if past not in pending or live in pending or live_marked or not says_pref or not hits:
                ok = False
                print("DEMO UNEXPECTED: the durable-preference path did not behave as specified.\n",
                      file=sys.stderr)
        finally:
            if prev_env is None:
                os.environ.pop(ledger.ENV_DIR, None)
            else:
                os.environ[ledger.ENV_DIR] = prev_env

    after = _real_store_fingerprint()
    if after != before:
        ok = False
        print(f"DEMO UNEXPECTED: the real memory store at {before[2]} was modified — a demo must never "
              "write the real ledger.\n", file=sys.stderr)
    else:
        print(f"Your real memory store ({before[2]}) was not touched — all of the above ran on a throwaway "
              "temp store.")

    print("\nThe gate decision is unchanged (a memory write is still denied) — only the relayed words differ;")
    print("the durable capture rides the engine's own automatic upkeep, never a hand-edited ledger.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
