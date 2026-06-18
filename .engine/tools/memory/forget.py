"""forget.py — the engine's active forgetting: Layer-1 logical retirement over the memory ledger (slice 4a).

Active forgetting (memory/README) is **two-layered**. This module is **Layer 1** — *reversible, mechanical,
memory-autonomous* tidying that needs no human gate because **nothing is lost**: a forgotten record is
excluded from recall but stays **resident and fully recoverable in the one canonical ledger**. (Layer 2 —
irreversible physical erasure, gated on the operator's merge of a single-purpose erasure PR — is a later
slice; this module deliberately has **no** erasure / ledger-delete code path, a build-conformance invariant a
test pins.)

Slice-4a scope: the **logical retirement of crash-duplicate consolidations**. When a consolidation pass
crashes after its episodic summaries are appended but before its `consolidated` marker lands (consolidate.py),
the next sweep re-files the session — leaving two passes in the ledger for one session ("a duplicate, never a
loss"). `live_records` retires the orphaned pass from recall: an episodic whose `batch` carries no closing
marker is dropped, while the marked (completed) pass is kept. The retirement is **derived from the ledger**
(the batch↔marker linkage), never stored only in the throwaway index, so it survives a rebuild.

Leaf discipline (principle §16): this module RETURNS records / a report and renders no operator-facing prose
(boot/audits own that). Both recall paths — the FTS5 `rebuild` and the plain `_scan` — consume `live_records`,
so the fast and slow lookups retire identically (the parity law, index.py). stdlib-only.

The stable, content-free record id (slice 4b) is minted in the record factories (records/capture/consolidate),
not here; this module hosts its operator demo (the `identity` verb). The scored demotion tiers, the gist
roll-up, ledger compaction (the rebuild-and-swap), and Layer-2 audit-gated erasure are later sub-slices of the
memory build plan's step 4.
"""

from __future__ import annotations

import os
import sys

# Make the package parent (.engine/tools) importable so `from memory import ledger` resolves even when this
# file is run directly as the demo script. Imported as `memory.forget`, the parent is already on sys.path, so
# this is a guarded no-op. Module-level imports stay limited to `ledger` + `records` (the cycle-free set):
# `index` imports THIS module for the fold, so importing `index`/`consolidate` here would cycle — the demo
# imports them lazily instead.
_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from memory import ledger, records  # noqa: E402


def _closed_batches(src: str) -> set:
    """The set of `batch` ids that a *completed* pass closed — i.e. carried by a `consolidated` marker."""
    closed = set()
    for record in ledger.iter_records(path=src):
        if isinstance(record, dict) and record.get("kind") == records.MARKER_KIND:
            batch = record.get(records.BATCH_KEY)
            if isinstance(batch, str) and batch:
                closed.add(batch)
    return closed


def _is_retired(record, closed: set) -> bool:
    """True iff `record` is an episodic summary orphaned by a crashed pass — its `batch` is set but no marker
    closed it. Everything else stays live: turn-deltas, markers, batch-less records (any pre-4a record), and
    the episodics of a completed (marked) pass. A batch-less episodic is ALWAYS live — there is no crash
    duplicate to resolve there, and nothing folds what it cannot key. The batch↔marker match is intentionally
    GLOBAL (not per-session): a `batch` is a uuid, so a cross-session collision is a 2**-128 non-event, and the
    failure direction is fail-safe anyway — a stray match could only ever KEEP a duplicate, never lose a record."""
    if not isinstance(record, dict) or record.get("kind") != records.EPISODIC_KIND:
        return False
    batch = record.get(records.BATCH_KEY)
    if not isinstance(batch, str) or not batch:
        return False
    return batch not in closed


def live_records(path: "str | None" = None):
    """Yield the ledger records recall should surface — every record EXCEPT the episodics a crashed
    consolidation pass orphaned (logical retirement). The orphan stays in the ledger, fully recoverable; this
    generator just doesn't surface it. The single shared authority both retrieval paths consume, so the fast
    (FTS5) and slow (scan) lookups retire identically.

    Two cheap sequential passes over the ledger: (1) collect the batch ids a marker closed; (2) stream,
    dropping only the retired orphans. Mutates nothing — never writes, never deletes."""
    src = ledger.ledger_path() if path is None else path
    closed = _closed_batches(src)
    for record in ledger.iter_records(path=src):
        if not _is_retired(record, closed):
            yield record


def duplicates(path: "str | None" = None) -> dict:
    """The logically-retired passes — what `live_records` drops from recall but the ledger still holds, grouped
    by session id. A READ-ONLY report (mutates nothing); the records are returned as-is so a caller can render
    a snippet. The retired copies remain fully recoverable in the ledger."""
    src = ledger.ledger_path() if path is None else path
    closed = _closed_batches(src)
    out: dict = {}
    for record in ledger.iter_records(path=src):
        if _is_retired(record, closed):
            sid = record.get("session_id") or "(unknown session)"
            out.setdefault(sid, []).append(record)
    return out


def _snippet(text, width: int = 70) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _print_duplicates(path: "str | None" = None) -> int:
    """The `duplicates` CLI verb: an operator-legible list of what is logically retired from recall — by
    session and a plain-language snippet, never a record-id or ledger offset. Each is still in the ledger."""
    groups = duplicates(path)
    if not groups:
        print("No hidden duplicates — recall surfaces every consolidated session once.")
        return 0
    total = sum(len(v) for v in groups.values())
    print(f"{total} duplicate summary record(s) across {len(groups)} session(s) are hidden from recall")
    print("(left behind by a consolidation pass that didn't finish saving; each is STILL SAVED and fully")
    print("recoverable — nothing was erased):\n")
    for sid, recs in groups.items():
        print(f"  session {sid}:")
        for rec in recs:
            print(f"    - hidden from recall: {_snippet(rec.get('text'))}")
    return 0


# --- Operator demonstration -------------------------------------------------------------------------------
# A walkthrough on a THROWAWAY practice cabinet (a temp folder), never real data. It runs the REAL consolidate
# + rebuild + recall code and reads the cabinet back, so every claim is recognizable words on screen. Run it
# and vary the two summaries near the top:
#     uv run --directory .engine --frozen -- python tools/memory/forget.py demo

# Two summaries of ONE session: the first is the pass that CRASHED before its marker (so it is an orphan); the
# second is the retry that completed. Both mention "sourdough", so a search for it would find both copies were
# they both surfaced — which is exactly what logical retirement prevents. Vary the wording and re-run.
_DEMO_SESSION = "session-sourdough"
_DEMO_CRASHED_TEXT = "Decided the sourdough starter gets fed every morning at eight — DO-NOT-LOSE-THIS."
_DEMO_RETRY_TEXT = "Decided the sourdough starter is fed daily at 8am."
_DEMO_WORD = "sourdough"


def _demo() -> int:
    import tempfile

    print("=" * 80)
    print("MEMORY — tidying a crash-duplicated summary out of recall, without losing it (a practice run)")
    print("=" * 80)
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["ENGINE_MEMORY_DIR"] = tmp          # the throwaway cabinet
        try:
            ok = _demo_body()
        finally:
            os.environ.pop("ENGINE_MEMORY_DIR", None)

    print("\n" + "-" * 80)
    print("Reminder: that was a PRACTICE cabinet, thrown away when this demo ended. The duplicate existed only")
    print("because a save CRASHED mid-write (rare) — this is crash-recovery of an accidental double, never the")
    print("deletion of anything you meant to keep. The extra copy is LOGICALLY RETIRED: dropped from search,")
    print("but STILL IN THE CABINET and fully recoverable — nothing is erased here. (Permanently erasing a")
    print("record is a separate, audit-reviewed step you approve by merging a pull request — never this.) Like")
    print("all memory, this is private, local, and deletable. Vary it: edit the two summaries near the top of")
    print("this file and re-run.")
    return 0 if ok else 1


def _ledger_episodics(session_id: str) -> list:
    """The episodic records for one session AS THEY SIT IN THE LEDGER (unfiltered) — the recoverability proof:
    everything is still here even after a copy is retired from recall."""
    return [
        r for r in ledger.iter_records(path=ledger.ledger_path())
        if isinstance(r, dict) and r.get("kind") == records.EPISODIC_KIND and r.get("session_id") == session_id
    ]


def _recall_episodics(word: str, session_id: str) -> list:
    """What RECALL surfaces for `word` (the fast index, rebuilt through `live_records`) — filtered to this
    session's summaries."""
    from memory import index  # lazy: index imports THIS module, so import it here, not at module load
    return [
        r for r in index.query(word).records
        if isinstance(r, dict) and r.get("kind") == records.EPISODIC_KIND and r.get("session_id") == session_id
    ]


def _demo_body() -> bool:
    from memory import consolidate  # lazy (consolidate → index → forget would cycle at module load)

    print("\nPART 1 — a crash leaves the SAME session's summary in the cabinet twice")
    print("-" * 80)
    # The pass that CRASHED: its episodic was appended, but the crash hit before its `consolidated` marker —
    # so this batch is never closed. (A fixed id stands in for the real per-pass uuid.)
    crashed = consolidate._make_episodic(_DEMO_SESSION, {"role": "decision", "text": _DEMO_CRASHED_TEXT},
                                         "the-pass-that-crashed")
    ledger.append(crashed)
    # The RETRY that completed: store_episodic writes its episodic + a marker (a NEW batch) and rebuilds recall.
    consolidate.store_episodic(_DEMO_SESSION, [{"role": "decision", "text": _DEMO_RETRY_TEXT}])
    in_ledger = _ledger_episodics(_DEMO_SESSION)
    print(f"  The cabinet now holds {len(in_ledger)} summaries for '{_DEMO_SESSION}':")
    for r in in_ledger:
        print(f"    - {_snippet(r.get('text'))}")
    print("  (One is from the pass that crashed before it finished; one is the completed retry.)")

    print(f"\nPART 2 — recall surfaces it ONCE (search for \"{_DEMO_WORD}\")")
    print("-" * 80)
    recalled = _recall_episodics(_DEMO_WORD, _DEMO_SESSION)
    print(f"  in the cabinet: {len(in_ledger)}    surfaced by recall: {len(recalled)}")
    for r in recalled:
        print(f"    recall returns: {_snippet(r.get('text'))}")
    deduped = len(recalled) == 1 and len(in_ledger) == 2
    print(f"  => {'recall shows 1 (the completed pass), though the cabinet holds 2.' if deduped else '!!! recall did not dedupe'}")

    print("\nPART 3 — nothing was erased: the retired copy is STILL in the cabinet, and recoverable")
    print("-" * 80)
    groups = duplicates()
    retired = [r for recs in groups.values() for r in recs]
    still_there = _ledger_episodics(_DEMO_SESSION)
    print(f"  logically retired from recall: {len(retired)}")
    for r in retired:
        print(f"    - retired: {_snippet(r.get('text'))}")
    print(f"  summaries still physically in the cabinet: {len(still_there)} (unchanged — nothing was deleted)")
    recoverable = len(retired) == 1 and len(still_there) == 2
    print(f"  => {'the duplicate is hidden from recall but still in the cabinet (recoverable).' if recoverable else '!!! something was lost'}")

    print("\nPART 4 — reversible by construction: rebuild from the cabinet alone, recall stays correct")
    print("-" * 80)
    from memory import index  # lazy
    index.rebuild()                          # rebuilt from the one real copy — the retirement is re-derived
    again = _recall_episodics(_DEMO_WORD, _DEMO_SESSION)
    stable = len(again) == 1 and len(_ledger_episodics(_DEMO_SESSION)) == 2
    print(f"  after a fresh rebuild — surfaced by recall: {len(again)}; in the cabinet: {len(_ledger_episodics(_DEMO_SESSION))}")
    print("  The retirement is a RULE derived from the cabinet, not a deletion: rebuilding re-applies it, and")
    print("  the retired copy is one rule-change away from resurfacing — it never left.")
    print(f"  => {'stable across a rebuild; nothing destroyed.' if stable else '!!! rebuild changed the answer'}")

    return deduped and recoverable and stable


# --- Operator demonstration: the stable, content-free record id (slice 4b) --------------------------------
# A second THROWAWAY-cabinet walkthrough, for the per-record name-tag the id adds. It runs the REAL factories +
# store + rebuild + recall and reads the cabinet back, so every claim is recognizable words on screen. Vary the
# notes near the top and re-run:
#     uv run --directory .engine --frozen -- python tools/memory/forget.py identity
_ID_DEMO_SESSION = "session-blueprint"
_ID_DEMO_TURN_TEXT = "Let's lock the launch to the blue plan."
_ID_DEMO_EPISODIC_TEXT = "Decided: the launch ships on the blue plan."
_ID_DEMO_TWIN_TEXT = "Decided: the launch ships on the blue plan."   # identical wording, stored twice
_ID_DEMO_WORD = "launch"


def _short(tag) -> str:
    """A readable short form of a 32-char name-tag, e.g. '3f9a…c7d1' — enough to compare two by eye."""
    tag = str(tag or "")
    return f"{tag[:4]}…{tag[-4:]}" if len(tag) >= 8 else (tag or "(none)")


def _all_records() -> list:
    return [r for r in ledger.iter_records(path=ledger.ledger_path()) if isinstance(r, dict)]


def _demo_identity() -> int:
    import tempfile

    print("=" * 80)
    print("MEMORY — every note gets a permanent, private name-tag that survives tidying (a practice run)")
    print("=" * 80)
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["ENGINE_MEMORY_DIR"] = tmp          # the throwaway cabinet
        try:
            ok = _demo_identity_body()
        finally:
            os.environ.pop("ENGINE_MEMORY_DIR", None)

    print("\n" + "-" * 80)
    print("Reminder: that was a PRACTICE cabinet, thrown away when this demo ended — private, on your machine,")
    print("and deletable. NOTHING was removed here; this step only ADDS the name-tag. The tag is permanent and")
    print("private: its only job is to let a LATER piece of work ask for exactly one note to be removed without")
    print("that request ever showing the note's words. Vary it: edit the notes near the top of this file and")
    print("re-run — the tags change, the stability still holds.")
    return 0 if ok else 1


def _demo_identity_body() -> bool:
    from memory import capture, consolidate, index  # lazy: consolidate → index → forget would cycle at load

    print("\nPART 1 — every note gets a name-tag")
    print("-" * 80)
    # A real captured turn-delta and a real stored episodic, both through the live factories the id rides in.
    ledger.append(capture._make_record(_ID_DEMO_SESSION, 0, "user", _ID_DEMO_TURN_TEXT))
    consolidate.store_episodic(_ID_DEMO_SESSION, [{"role": "decision", "text": _ID_DEMO_EPISODIC_TEXT}])
    stored = [r for r in _all_records() if r.get("session_id") == _ID_DEMO_SESSION]
    for r in stored:
        if r.get("text"):
            label = _snippet(r.get("text"))
        else:
            label = {records.MARKER_KIND: "(a tidy-up marker)",
                     records.EPISODIC_KIND: "(a summary note)"}.get(r.get("kind"), "(a conversation note)")
        print(f"  note: {label:<52}  tag: {_short(r.get(records.RECORD_ID_KEY))}")
    tagged = [r for r in stored
              if isinstance(r.get(records.RECORD_ID_KEY), str) and len(r[records.RECORD_ID_KEY]) == 32]
    every_tagged = len(stored) >= 2 and len(tagged) == len(stored)
    print(f"  => {'every note carries its own private name-tag.' if every_tagged else '!!! a note is missing its tag'}")

    print("\nPART 2 — the name-tag reveals nothing about the note")
    print("-" * 80)
    twin_a = capture._make_record(_ID_DEMO_SESSION, 1, "user", _ID_DEMO_TWIN_TEXT)
    twin_b = capture._make_record(_ID_DEMO_SESSION, 2, "user", _ID_DEMO_TWIN_TEXT)
    ledger.append(twin_a)
    ledger.append(twin_b)
    print(f'  two notes with the SAME wording: "{_snippet(_ID_DEMO_TWIN_TEXT)}"')
    print(f"    note 1 tag: {_short(twin_a[records.RECORD_ID_KEY])}")
    print(f"    note 2 tag: {_short(twin_b[records.RECORD_ID_KEY])}")
    different = twin_a[records.RECORD_ID_KEY] != twin_b[records.RECORD_ID_KEY]
    print(f"  => {'identical words, DIFFERENT tags (the tag is random, not made from the words).' if different else '!!! identical text produced the same tag'}")
    index.rebuild()
    found_by_word = index.query(_ID_DEMO_WORD).records
    found_by_tag = index.query(twin_a[records.RECORD_ID_KEY]).records
    tag_result = "[no matches]" if not found_by_tag else f"{len(found_by_tag)} match(es)"
    if found_by_word:
        print(f'  search for the word "{_ID_DEMO_WORD}": {len(found_by_word)} match(es) — the notes ARE findable by their words')
    else:
        print(f'  search for the word "{_ID_DEMO_WORD}": 0 matches — that word is not in the notes above')
        print('    (edit _ID_DEMO_WORD near the top of this file to a word you can see, then re-run)')
    print(f'  search for a tag "{_short(twin_a[records.RECORD_ID_KEY])}": {tag_result} — you cannot find a note by its tag')
    # The ONLY failure here is the tag actually surfacing in search. A search-word that matches no note is the
    # operator's own input, not a leak — guide them to a present word, never cry "leaked".
    tag_private = not found_by_tag
    if not tag_private:
        print("  => !!! the tag leaked into search")
    elif found_by_word:
        print("  => the tag is private: words find the note, the tag never does.")
    else:
        print("  => the tag is private (no match by tag); pick a word from the notes to see the other half.")

    print("\nPART 3 — the name-tag stays the same when the engine tidies")
    print("-" * 80)
    # Track ONE note's tag through a rebuild, a re-file (the move the future tidy-up makes), and a 2nd rebuild.
    tag0 = twin_a[records.RECORD_ID_KEY]
    index.rebuild()                                                    # (a) the index READS the tag, never re-mints
    fetched = [r for r in index.query(_ID_DEMO_WORD).records if r.get(records.RECORD_ID_KEY) == tag0]
    tag1 = fetched[0][records.RECORD_ID_KEY] if fetched else None
    ledger.append(twin_a)                                             # (b) re-file the SAME note (compaction's move)
    refiled = [r for r in _all_records() if r.get(records.RECORD_ID_KEY) == tag0]
    tag2 = refiled[-1][records.RECORD_ID_KEY] if refiled else None
    index.rebuild()                                                   # (c) rebuild once more
    again = [r for r in index.query(_ID_DEMO_WORD).records if r.get(records.RECORD_ID_KEY) == tag0]
    tag3 = again[0][records.RECORD_ID_KEY] if again else None
    print(f"  the note's tag at creation:        {_short(tag0)}")
    print(f"    after rebuilding recall:           {_short(tag1)}")
    print(f"    after re-filing the note:          {_short(tag2)}")
    print(f"    after rebuilding recall once more: {_short(tag3)}")
    stable = bool(tag0) and tag1 == tag0 and tag2 == tag0 and tag3 == tag0
    print(f"  => STABLE: {'yes' if stable else 'NO — !!! tag changed'}")

    return every_tagged and different and tag_private and stable


def main(argv: list) -> int:
    cmd = argv[0] if argv else "demo"
    if cmd == "duplicates":
        return _print_duplicates()
    if cmd == "demo":
        return _demo()
    if cmd == "identity":
        return _demo_identity()
    print(f"usage: forget.py [duplicates|demo|identity]\nunknown command {cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
