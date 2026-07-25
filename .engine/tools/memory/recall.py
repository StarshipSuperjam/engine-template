#!/usr/bin/env python3
"""recall.py — the transcript-window reader (memory-substrate-sqlite-fts5).

The read side of eADR-0038's transcript-first recall: memory's canonical record is the exact user/assistant
conversation, so recall must be able to READ IT BACK. `index.search` deliberately cannot — raw `turn-delta`
records are excluded from every ranked path (`forget._is_ambient_capture`, applied once on the shared
`live_records` stream), because verbatim turns would swamp a lexical ranking. This module is the missing
fetch: given a session, hand back that session's conversation as readable turns.

IT FETCHES, IT DOES NOT RANK. There is exactly one ranking contract for memory (the `search` interface); a
second ranked path would fork it. The workflow above the seam ranks: it searches for candidate sessions, then
calls here to READ each one. Ordering here is the conversation's own order, never a relevance judgement.

THE LAWS (load-bearing, each pinned by a test):
  - READ-ONLY. Never writes, never reinforces, never mutates the ledger. A window changes nothing.
  - GENUINE TURNS ONLY. Harness-injected pseudo-turns (a `/compact` continuation summary, a
    `task-notification` block) are skipped — presenting machine scaffolding as the operator's own words is a
    correctness bug, not a cosmetic one. Same rule as the consolidation sweep's `_is_genuine_delta`.
  - ORDER BY `seq`, NEVER `ts`. `ts` is whole-second and identical across a turn's chunks; `seq` is the real
    per-message ordinal. The sort is STABLE, so the chunks of one message keep ledger append order — which is
    the only authority on intra-message order (the envelope carries no chunk ordinal).
  - COMPLETENESS IS NOT PROVABLE. A >4KB message is split into chunks that share one `seq`, and physical
    erasure is per-record-id — so a message CAN lose a middle chunk with no way to detect it. This module
    never claims verbatim completeness it cannot verify: it reports what it found and says the wording is
    reconstructed from stored chunks. Honest degradation (eADR-0034), not a false guarantee.
  - TOLERATE THE LEGACY STORE. Real ledgers hold turn-deltas predating parts of the envelope (no `id`, no
    `session_id`, no `seq`). A malformed record is skipped, never a crash.

WHY IT DOES NOT IMPORT `consolidate.read_deltas`: that reader is the consolidation sweep's, and consolidation
is retired by the curation-removal slice — importing it would strand this reader on a dying module. The
genuine-turn predicate is re-stated here deliberately, not by oversight.

CLI:  python tools/memory/recall.py demo               # falsifiable walkthrough on a THROWAWAY cabinet
      python tools/memory/recall.py window <session>   # read one session's conversation (local, read-only)
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # .engine/tools on path
from memory import ledger, records  # noqa: E402


# ---- tuning leaves (recorded build-spec leaves) ---------------------------------------------------------
DEFAULT_RADIUS = 6          # turns either side of an anchor when one is given ("around the hit")
DEFAULT_MAX_TURNS = 40      # hard cap on one window, so a huge session cannot flood a session's context

_TURN_DELTA_KIND = records.AMBIENT_CAPTURE_KIND

# The plain-language caveat that rides every non-empty window. The wording is reconstructed from stored
# chunks; nothing here can prove a middle chunk was never physically erased, so the note says so rather than
# implying a guarantee.
COMPLETENESS_NOTE = ("Reconstructed from the stored conversation. Long messages were saved in pieces and are "
                     "rejoined here in the order they were written; if a piece was permanently erased, the "
                     "rejoined wording would be missing it without saying so.")


# ---- the leak guard ------------------------------------------------------------------------------------

def assert_not_live_store(*paths) -> None:
    """Fail loud if a throwaway path would resolve to the real memory store. A read tool that misfires does
    not corrupt — it EXFILTRATES: this module's whole job is printing verbatim conversation, and a demo's
    stdout can be a CI log. Every worktree of one clone shares a single ledger (`_git_common_root`), so a
    missing environment override silently resolves to the operator's real store."""
    live = os.path.realpath(ledger.ledger_path())
    for p in paths:
        if os.path.realpath(p) == live:
            raise SystemExit("recall: refusing to run a throwaway window against the LIVE memory store")


# ---- the reader ----------------------------------------------------------------------------------------

def _seq_of(record) -> int:
    """A record's `seq` as an int (the per-message ordinal), defaulting to 0 for a malformed/absent value."""
    s = record.get("seq")
    return s if isinstance(s, int) and not isinstance(s, bool) else 0


def is_genuine_turn(record) -> bool:
    """True iff `record` is a real captured conversation turn: a `turn-delta` that is NOT a harness-injected
    pseudo-turn. Deliberately mirrors the consolidation sweep's predicate — a window that included a
    `/compact` continuation summary would show the model its own scaffolding as if the operator had said it."""
    return (isinstance(record, dict)
            and record.get("kind") == _TURN_DELTA_KIND
            and not records.is_injected_record(record))


def session_turns(session_id: str, *, path: "str | None" = None) -> list:
    """Every genuine turn of one session, in conversation order. A pure read over the raw ledger (the ranked
    stream excludes turn-deltas by design, so this cannot go through `live_records`). Malformed legacy records
    are skipped rather than crashing. The sort is STABLE on `seq`, preserving append order within a message."""
    if not isinstance(session_id, str) or not session_id:
        return []
    src = ledger.ledger_path() if path is None else path
    out = [r for r in ledger.iter_records(path=src)
           if is_genuine_turn(r) and r.get("session_id") == session_id]
    out.sort(key=_seq_of)
    return out


def _join_chunks(turns: list) -> list:
    """Rejoin the chunks of each message into one readable turn. Capture splits a >4KB message into several
    records sharing ONE `seq` and speaker; here they concatenate in the order they were appended. Returns
    dicts of {seq, speaker, text, chunks} — `chunks` is how many stored pieces were rejoined, which is
    reported, never used as a completeness proof (an erased middle piece is indistinguishable from a shorter
    message)."""
    joined: list = []
    for record in turns:
        seq = _seq_of(record)
        speaker = record.get("speaker") if isinstance(record.get("speaker"), str) else "unknown"
        text = record.get("text") if isinstance(record.get("text"), str) else ""
        if joined and joined[-1]["seq"] == seq and joined[-1]["speaker"] == speaker:
            joined[-1]["text"] += text          # a continuation chunk of the same message
            joined[-1]["chunks"] += 1
            continue
        joined.append({"seq": seq, "speaker": speaker, "text": text, "chunks": 1})
    return joined


def window(session_id: str, *, anchor_seq: "int | None" = None, radius: int = DEFAULT_RADIUS,
           max_turns: int = DEFAULT_MAX_TURNS, path: "str | None" = None) -> dict:
    """One session's conversation as readable turns — the transcript window a recall workflow reads after a
    search names a candidate session.

    `anchor_seq` centres the window on one message (the hit), taking `radius` turns either side; omitted, the
    window starts at the beginning of the session. `max_turns` caps the result either way, so a very long
    session cannot flood the caller's context. Returns
    {session_id, turns, total, returned, truncated, note} — `note` is the completeness caveat, present
    whenever any turn is returned."""
    turns = _join_chunks(session_turns(session_id, path=path))
    total = len(turns)
    if anchor_seq is not None:
        centre = next((i for i, t in enumerate(turns) if t["seq"] >= anchor_seq), total)
        lo = max(0, centre - max(0, radius))
        selected = turns[lo:lo + max(0, radius) * 2 + 1]
    else:
        selected = turns
    selected = selected[:max(0, max_turns)]
    return {
        "session_id": session_id,
        "turns": selected,
        "total": total,
        "returned": len(selected),
        "truncated": len(selected) < total,
        "note": COMPLETENESS_NOTE if selected else "",
    }


def render(result: dict) -> str:
    """A window as plain readable conversation — what a reader (model or operator) actually consumes."""
    if not result.get("turns"):
        return f"No stored conversation found for session {result.get('session_id')}."
    lines = [f"Conversation from session {result.get('session_id')} "
             f"({result.get('returned')} of {result.get('total')} turns"
             f"{', truncated' if result.get('truncated') else ''}):", ""]
    for turn in result["turns"]:
        lines.append(f"{turn['speaker']}: {turn['text']}")
        lines.append("")
    lines.append(result.get("note") or "")
    return "\n".join(lines).strip()


# --- Operator demonstration -------------------------------------------------------------------------------
# A falsifiable walkthrough on a THROWAWAY practice cabinet (a temp folder), never real memory. It exercises
# the REAL reader above and checks claims that CAN fail — so a green run is evidence, not a showcase:
#     uv run --directory .engine --frozen -- python tools/memory/recall.py demo

def _demo_record(session_id: str, seq: int, speaker: str, text: str, *, injected: bool = False) -> dict:
    tags = ["transcript", "stop"] + ([records.INJECTED_TAG] if injected else [])
    return {"v": 1, "kind": _TURN_DELTA_KIND, records.RECORD_ID_KEY: records.new_record_id(),
            "session_id": session_id, "ts": 1, "seq": seq, "speaker": speaker, "text": text, "tags": tags}


def _demo() -> int:
    """Prove the reader's four load-bearing claims, each able to FAIL: conversation order, chunk rejoining,
    the injected-pseudo-turn skip, and session isolation."""
    import tempfile

    ok = True
    with tempfile.TemporaryDirectory(prefix="engine-recall-demo-") as tmp:
        cabinet = os.path.join(tmp, "ledger.ndjson")
        assert_not_live_store(cabinet)          # the guard runs on the real path this demo will write

        print("PART 1 — a practice conversation is written to a throwaway folder (never your real memory).")
        for record in [
            _demo_record("s-demo", 0, "user", "Let's move the nightly export to run before the upload."),
            _demo_record("s-demo", 1, "assistant", "Done — the manifest is written first now."),
            _demo_record("s-demo", 2, "user", "<task-notification> ignore me </task-notification>",
                         injected=True),
            _demo_record("s-demo", 3, "user", "BIG-ONE "),
            _demo_record("s-demo", 3, "user", "BIG-TWO"),
            _demo_record("s-other", 0, "user", "A different session entirely."),
        ]:
            ledger.append(record, path=cabinet)

        result = window("s-demo", path=cabinet)
        print(render(result))
        print()

        print("PART 2 — the checks that can fail:")
        texts = [t["text"] for t in result["turns"]]

        in_order = texts[:2] == ["Let's move the nightly export to run before the upload.",
                                 "Done — the manifest is written first now."]
        print(f"  conversation is in the order it happened .......... {'PASS' if in_order else 'FAIL'}")
        ok = ok and in_order

        rejoined = "BIG-ONE BIG-TWO" in texts
        print(f"  a long split message is rejoined whole ............ {'PASS' if rejoined else 'FAIL'}")
        ok = ok and rejoined

        skipped = not any("ignore me" in t for t in texts)
        print(f"  machine-inserted text is not shown as yours ....... {'PASS' if skipped else 'FAIL'}")
        ok = ok and skipped

        isolated = not any("different session" in t for t in texts)
        print(f"  another session's words never leak in ............. {'PASS' if isolated else 'FAIL'}")
        ok = ok and isolated

        empty = window("s-nothing", path=cabinet)
        quiet = empty["turns"] == [] and empty["note"] == ""
        print(f"  an unknown session returns nothing, quietly ....... {'PASS' if quiet else 'FAIL'}")
        ok = ok and quiet

    print()
    print("Reading a conversation back changes nothing — this only reads." if ok else "The reader is WRONG.")
    return 0 if ok else 1


def main(argv: list) -> int:
    cmd = argv[0] if argv else "demo"
    if cmd == "demo":
        return _demo()
    if cmd == "window":
        if len(argv) < 2:
            print("usage: recall.py window <session-id>")
            return 2
        print(render(window(argv[1])))
        return 0
    print("usage: recall.py [demo|window <session-id>]")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
