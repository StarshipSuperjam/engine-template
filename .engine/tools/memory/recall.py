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

THE LAWS (load-bearing, each pinned by a test — except the leak guard, whose honest tier is stated at its
own definition: it is belt-and-braces over explicit path threading, not the protection itself):
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
  - TOLERATE THE LEGACY STORE WITHOUT INVENTING. Real ledgers hold turn-deltas predating parts of the
    envelope (no `id`, no `session_id`, no `seq`). Reading one never crashes and never drops it: a record with
    no `session_id` is simply unreachable (nothing names its session), and one missing `seq`/`speaker` is
    still shown. But a record with no usable ordinal is NEVER merged with its neighbour — its identity is
    unknown, and guessing would splice unrelated messages into an utterance nobody said. Tolerance means
    showing what is there, never manufacturing continuity across it.

WHY IT DOES NOT IMPORT `consolidate.read_deltas`: that reader is the consolidation sweep's, and consolidation
is retired by the curation-removal slice — importing it would strand this reader on a dying module. The
genuine-turn predicate is re-stated here deliberately, not by oversight.

CLI:  python tools/memory/recall.py demo               # falsifiable walkthrough on a THROWAWAY cabinet

"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # .engine/tools on path
from memory import ledger, records  # noqa: E402


# ---- tuning leaves (recorded build-spec leaves) ---------------------------------------------------------
DEFAULT_RADIUS = 6          # turns either side of an anchor when one is given ("around the hit")
DEFAULT_MAX_TURNS = 40      # default cap on one window, so a huge session cannot flood a session's context
MAX_TURNS_CEILING = 200     # the real ceiling: a caller may raise the cap, but not to "the whole store". A
                            # caller-supplied cap with no upper bound is not containment — and the one move
                            # available when a window misses is to raise it, so the pressure is toward dumps.
MAX_TEXT_BYTES = 200_000    # the OTHER dimension. Capping turns alone does not bound a response: chunking is
                            # lossless and unbounded, so ONE pasted document can be thousands of chunks and
                            # megabytes in a single turn. Bound the text too, or the flood guard guards nothing.

_TURN_DELTA_KIND = records.AMBIENT_CAPTURE_KIND

# The plain-language caveat that rides every non-empty window. The wording is reconstructed from stored
# chunks; nothing here can prove a middle chunk was never physically erased, so the note says so rather than
# implying a guarantee.
COMPLETENESS_NOTE = ("Reconstructed from the stored conversation. Long messages were saved in pieces and are "
                     "rejoined here in the order they were written; if a piece was permanently erased, the "
                     "rejoined wording would be missing it without saying so.")

# Said whenever the byte budget bit. Without it a shortened turn reads as the whole message — the same class
# of defect as splicing two messages together: wording presented as complete when it is not.
SHORTENED_NOTE = ("This window hit its size limit, so at least one message is cut short here — ask for a "
                  "narrower window (an anchor, or fewer turns) to see any of it in full.")


# ---- the leak guard ------------------------------------------------------------------------------------

def assert_not_live_store(*paths) -> None:
    """Refuse a throwaway path that resolves to the real memory store. A read tool that misfires does not
    corrupt — it EXFILTRATES: this module's whole job is printing verbatim conversation, and a demo's stdout
    can be a CI log. Every worktree of one clone shares a single ledger (`_git_common_root`), so a missing
    environment override silently resolves to the operator's real store.

    HONEST TIER — belt-and-braces, not the protection. The real safeguard is that the demo threads an explicit
    `path=` into every call, so it never consults the default at all; this guard would only catch a future
    edit that stopped doing so. Called where the path is a fresh temp directory, it cannot fire today."""
    live = os.path.realpath(ledger.ledger_path())
    for p in paths:
        if os.path.realpath(p) == live:
            raise SystemExit("recall: refusing to run a throwaway window against the LIVE memory store")


# ---- the reader ----------------------------------------------------------------------------------------

def _seq_of(record):
    """A record's `seq` as an int, or None when it is absent or not an integer. Returning None rather than
    defaulting to 0 is load-bearing: `seq` is message IDENTITY here, so collapsing 'absent', 'genuinely 0' and
    'wrong type' into one value makes unrelated messages look like chunks of each other and they get welded
    into an utterance nobody said. A record with no usable ordinal is kept and shown — just never merged."""
    s = record.get("seq")
    return s if isinstance(s, int) and not isinstance(s, bool) else None


def _sort_key(record):
    """Order by `seq`, with un-ordinalled records last in ledger order. Stable, so chunks of one message keep
    the order they were appended — the only authority on intra-message order."""
    seq = _seq_of(record)
    return (1, 0) if seq is None else (0, seq)


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
    out.sort(key=_sort_key)
    return out


def _join_chunks(turns: list) -> list:
    """Rejoin the chunks of each message into one readable turn. Capture splits a >4KB message into several
    records sharing ONE `seq` and speaker; here they concatenate in the order they were appended. Returns
    dicts of {seq, speaker, text, chunks} — `chunks` is how many stored pieces were rejoined, which is
    reported, never used as a completeness proof (an erased middle piece is indistinguishable from a shorter
    message)."""
    joined: list = []
    last_chunk: list = []          # the previous record's raw text, per joined turn (never returned)
    budget = MAX_TEXT_BYTES
    dropped = False
    for record in turns:
        seq = _seq_of(record)
        speaker = record.get("speaker") if isinstance(record.get("speaker"), str) else "unknown"
        raw = record.get("text") if isinstance(record.get("text"), str) else ""
        if budget <= 0:
            dropped = True
            break
        text = raw[:budget]
        budget -= len(text)
        dropped = dropped or len(text) < len(raw)
        previous = joined[-1] if joined else None
        # Merge ONLY a genuine continuation chunk: the same message means the SAME present ordinal and the
        # same speaker. A record with no usable ordinal never merges (its identity is unknown, and guessing
        # fabricates an utterance), and a record repeating the PREVIOUS RECORD's text exactly is a re-capture
        # of that message — capture re-reads a session from the start when its cursor is missing or corrupt —
        # not a second chunk. Compared against the previous CHUNK, never the accumulated text, so a genuine
        # chunk that merely ends the same way as what came before is still joined.
        if (previous is not None and seq is not None and previous["seq"] == seq
                and previous["speaker"] == speaker and text and last_chunk[-1] != raw):
            previous["text"] += text
            previous["chunks"] += 1
            last_chunk[-1] = raw
            continue
        joined.append({"seq": seq, "speaker": speaker, "text": text, "chunks": 1})
        last_chunk.append(raw)
    return joined, dropped


def resolve_sessions(session_id: str, *, path: "str | None" = None) -> list:
    """The REAL sessions a window id names. Normally that is the id itself — but a summary folded from several
    sessions carries a cluster key (`tag:…` / `sim:…`) that is not a session at all, and its own provenance is
    a list of RECORD ids, not session ids. Resolving it means following those record ids back to the episodes
    they fold and reading the session off each.

    Without this the caller is stranded: the two exposed operations take a query and a session id, so nothing
    can look a record id up, and the raw episodes behind a completed roll-up are dropped from ranked recall —
    a cluster-key window would silently return nothing on exactly the OLDEST memories, which is when a
    transcript is most wanted. One ledger pass; unresolvable ids simply yield no session."""
    if not records.is_cross_session_sentinel(session_id):
        return [session_id] if session_id else []
    src = ledger.ledger_path() if path is None else path
    wanted: set = set()
    # id -> session_id ONLY, never the records themselves: retaining whole records here costs memory
    # proportional to the WHOLE store (tens of MB on a real ledger) to answer a question about one cluster.
    session_of: dict = {}
    for record in ledger.iter_records(path=src):
        if not isinstance(record, dict):
            continue
        rid = record.get(records.RECORD_ID_KEY)
        if isinstance(rid, str) and rid:
            session_of[rid] = record.get("session_id")
        if record.get("session_id") == session_id:
            for source_id in (record.get(records.SOURCE_IDS_KEY) or []):
                if isinstance(source_id, str) and source_id:
                    wanted.add(source_id)
    out: list = []
    for source_id in sorted(wanted):
        real = session_of.get(source_id)
        if (isinstance(real, str) and real and real not in out
                and not records.is_cross_session_sentinel(real)):
            out.append(real)
    return out


def window(session_id: str, *, anchor_seq: "int | None" = None, radius: int = DEFAULT_RADIUS,
           max_turns: int = DEFAULT_MAX_TURNS, path: "str | None" = None) -> dict:
    """A past conversation as readable turns — what a recall workflow reads after a search names a candidate.

    `session_id` is a session, or a cluster key for a summary folded from several sessions (resolved through
    `resolve_sessions`, so the caller never has to chase record ids it has no tool to look up). `anchor_seq`
    centres the window on one message (the hit) with `radius` turns either side; omitted, the window starts at
    the beginning. `max_turns` caps the result — and when an anchor is given the cap is applied AROUND the
    anchor, never truncated from the front, so widening the radius can never push the hit out of its own
    window (silently returning a plausible window that lacks the very message asked about).

    Returns {session_id, sessions, turns, total, returned, truncated, note}; `note` always says something —
    the completeness caveat when turns come back, and why it is empty when they do not."""
    sessions = resolve_sessions(session_id, path=path)
    turns: list = []
    shortened = False
    for real in sessions:
        joined, dropped = _join_chunks(session_turns(real, path=path))
        shortened = shortened or dropped
        for turn in joined:
            turn["session_id"] = real
            turns.append(turn)
    total = len(turns)
    cap = min(max(0, max_turns), MAX_TURNS_CEILING)
    if anchor_seq is not None and turns:
        centre = next((i for i, t in enumerate(turns) if t["seq"] >= anchor_seq), total - 1)
        half = min(max(0, radius), cap // 2 if cap else 0)
        lo = max(0, centre - half)
        selected = turns[lo:lo + (half * 2 + 1)][:cap]
    else:
        selected = turns[:cap]
    return {
        "session_id": session_id,
        "sessions": sessions,
        "turns": selected,
        "total": total,
        "returned": len(selected),
        "truncated": len(selected) < total,
        "note": ((COMPLETENESS_NOTE + (" " + SHORTENED_NOTE if shortened else ""))
                 if selected else _empty_note(session_id, sessions)),
    }


def _empty_note(session_id: str, sessions: list) -> str:
    """Why an empty window is empty — so a caller can tell 'wrong id' from 'nothing readable there' instead of
    reading silence as 'memory does not hold it'."""
    if records.is_cross_session_sentinel(session_id) and not sessions:
        return ("That id is a cluster key for a summary folded from several sessions, and the sessions behind "
                "it could not be resolved — answer from the summary itself and say the original conversation "
                "is not reachable.")
    return ("No stored conversation for that session. Either the id is not one this project captured, or the "
            "session held nothing but machine-inserted text.")


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
        print("  (the last message was stored as two separate pieces, 'BIG-ONE ' and 'BIG-TWO' — above, it is")
        print("   rejoined into the one message it was. A machine-inserted line was also stored in this")
        print("   practice conversation; it is deliberately absent above, so it can never be read back as")
        print("   something you said.)")
        print()
        empty = window("s-nothing", path=cabinet)
        print("  Asked for a session that doesn't exist, it explains itself rather than going silent:")
        print(f"    {empty['note']}")
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

        explained = empty["turns"] == [] and "No stored conversation" in empty["note"]
        print(f"  an unknown session says WHY it found nothing ...... {'PASS' if explained else 'FAIL'}")
        ok = ok and explained

        # A summary folded from several sessions carries a cluster key, not a session. Reading it must
        # resolve to the real conversation, or the oldest memories are unreachable exactly when wanted.
        ledger.append({"v": 1, "kind": "gist", records.RECORD_ID_KEY: "g-demo", "session_id": "tag:exports",
                       "text": "a summary folded from earlier sessions",
                       records.SOURCE_IDS_KEY: ["ep-demo"]}, path=cabinet)
        ledger.append({"v": 1, "kind": "episodic", records.RECORD_ID_KEY: "ep-demo",
                       "session_id": "s-demo", "text": "the episode it folded"}, path=cabinet)
        folded = window("tag:exports", path=cabinet)
        resolved = folded["sessions"] == ["s-demo"] and any("nightly export" in t["text"]
                                                            for t in folded["turns"])
        print(f"  a folded summary resolves to its real session ..... {'PASS' if resolved else 'FAIL'}")
        ok = ok and resolved

    print()
    if ok:
        print("Reading a conversation back changes nothing — this only reads.")
        print()
        print("What this changes for you: until now I could only see short summaries I had written about past")
        print("sessions. I can now read the real conversation back, word for word, and I do it on my own")
        print("initiative while answering — you are not asked first. What I read is exactly what was typed,")
        print("including anything pasted into a session; most of what is stored was saved before the engine")
        print("began stripping secrets on the way in, and nothing is stripped on the way out.")
    else:
        print("The reader is WRONG.")
    return 0 if ok else 1


def main(argv: list) -> int:
    # `demo` only, deliberately. An earlier draft carried a `window <session-id>` verb that printed verbatim
    # conversation from the LIVE store to stdout — a surface nothing asked for, on the one path where a stray
    # invocation (or a CI log) leaks the operator's own words. Reading real memory goes through the MCP
    # operation, in a session the operator is present for.
    cmd = argv[0] if argv else "demo"
    if cmd == "demo":
        return _demo()
    print("usage: recall.py demo")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
