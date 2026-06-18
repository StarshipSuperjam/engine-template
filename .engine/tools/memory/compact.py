"""compact.py — ledger compaction: the self-directed crash-safe rebuild-and-swap (memory-substrate, slice 4d).

Active forgetting (memory/README) bounds the ledger's growth WITHOUT a second store and without seek-and-edit,
by a **whole-ledger rebuild-and-swap** — memory invoking its own restore primitive (*backup = copy; restore =
replace the ledger and rebuild the index*, ledger.replace_ledger). This module is **Layer 1**: *reversible,
mechanical, memory-autonomous* tidying that needs no human gate because **nothing recall-bearing is lost**.

What one pass does, under the single-writer `.capture.lock`, as ONE critical section:

  1. Read the RAW ledger (every record — including archived/retired ones; compaction must not drop recall).
  2. **Fold** each record's reinforcement markers into carried current-state fields (records.py:
     frecency_snapshot / snapshot_ts / last_access_ts / tier) via `score.mint_snapshot`, then **drop those
     markers**. The 4b `id` is carried verbatim. A record with no markers is rewritten byte-identical.
  3. Write the folded, marker-pruned ledger to a temp IN THE LEDGER'S OWN DIRECTORY, fsynced.
  4. **Bump the generation BEFORE the swap**, then `ledger.replace_ledger` (fsync temp → atomic rename → fsync
     dir), then rebuild the index stamped with the new generation.

**Layer-1 never erases recall content** — only the (non-recall) `reinforcement` markers are pruned; every
turn-delta, episodic (including a 4a crash-duplicate orphan, which stays logically retired), and `consolidated`
marker survives the rewrite (a build-conformance invariant: no Layer-1 routine reaches erasure — Layer-2
physical erasure of recall content is slice 4e, audit-adjudicated and operator-merge-gated). Because frecency is
a **recurrence on the carried snapshot** (score.frecency), a compacted record scores IDENTICALLY to before — so
demotion survives the fold. The crash-safe-swap law (README): a crash at any point leaves exactly one intact
ledger (old or new); a stale index is always fully rebuilt — and the generation gate (index.py) routes a
crash-staled index to the always-correct scan until it is rebuilt, so an erased record (slice 4e) can never
resurface from a stale index. Recovery binds to the fixed canonical ledger name: a temp left by a crash is a
complete same-schema file but is NEVER the canonical name, so it is ignored-and-reaped, never promoted.

**Degenerate live (expected, precedented — the 4c shape).** The live caller that appends reinforcement markers
is slice 5; until then the engine's young ledger has none, so a `compact` pass folds nothing and rewrites an
identical record set at generation 1 — a safe no-op-shaped tidy. The live AUTO-trigger (a maintenance cadence)
is a forward-owe; 4d ships the mechanism + the manual `compact` verb + the operator demo.

Leaf discipline (principle §16): RETURNS a small report and renders no operator-facing prose (the demo is the
one operator surface). stdlib + the cycle-free `memory` set (ledger / records / score / forget); `capture` (the
lock) and `index` (the rebuild) are lazy-imported inside `compact` to keep them off the module-load path.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid

_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from memory import forget, ledger, records, score  # noqa: E402

_TEMP_PREFIX = ".compact-"          # the in-dir swap temp; NEVER the canonical name, so it is reapable
_TEMP_SUFFIX = ".ndjson"


class _InjectedCrash(Exception):
    """A TEST/DEMO-only fault, raised at a chosen swap point to model a power-cut. Production `compact` never
    passes `_crash_after`, so this never fires in real use (a test pins the default off)."""


def _reap_temps(data_dir: str) -> int:
    """Remove any leftover compaction temp (`.compact-*.ndjson`) — a crash between write and rename leaves a
    complete same-schema file that is NEVER the canonical ledger name, so it is ignored-and-reaped, never
    promoted (recovery binds to the fixed canonical name, README). Returns how many were reaped."""
    reaped = 0
    try:
        names = os.listdir(data_dir)
    except OSError:
        return 0
    for name in names:
        if name.startswith(_TEMP_PREFIX) and name.endswith(_TEMP_SUFFIX):
            try:
                os.remove(os.path.join(data_dir, name))
                reaped += 1
            except OSError:
                pass
    return reaped


def _fold_record(record, access_index: dict, t0: int):
    """`record` with its reinforcement history folded into carried current-state fields (slice 4d), or unchanged
    when it has nothing to fold. Only a record whose id is an access-index key — necessarily a recall-content
    record, since only recall results are reinforced — gets a snapshot; everything else (markers, un-reinforced
    records) is returned verbatim, its 4b id preserved either way."""
    if not isinstance(record, dict):
        return record
    rid = record.get(records.RECORD_ID_KEY)
    accesses = access_index.get(rid) if isinstance(rid, str) and rid else None
    if not accesses:
        return record
    snap, last = score.mint_snapshot(record, accesses, t0)
    folded = dict(record)
    folded[records.FRECENCY_SNAPSHOT_KEY] = snap
    folded[records.SNAPSHOT_TS_KEY] = t0
    folded[records.LAST_ACCESS_TS_KEY] = last
    folded[records.TIER_KEY] = score.tier(record, accesses, t0)   # snapshot-time tier (legibility; recomputed on read)
    return folded


def _write_compacted_temp(data_dir: str, raw_records, access_index: dict, t0: int) -> str:
    """Write the folded, marker-pruned ledger to a fresh temp in `data_dir`, fsynced. Drops ONLY `reinforcement`
    markers (folded away); every recall-content record + every `consolidated` marker survives (Layer-1 never
    erases recall content). Returns the temp path."""
    tmp = os.path.join(data_dir, _TEMP_PREFIX + uuid.uuid4().hex + _TEMP_SUFFIX)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        for record in raw_records:
            if isinstance(record, dict) and record.get("kind") == records.REINFORCEMENT_KIND:
                continue  # folded into its target's snapshot; pruned (non-recall derivation fuel)
            folded = _fold_record(record, access_index, t0)
            line = json.dumps(folded, ensure_ascii=False, separators=(",", ":")) + "\n"
            os.write(fd, line.encode("utf-8"))
        ledger._durable_fsync(fd)
    finally:
        os.close(fd)
    return tmp


def compact(path: "str | None" = None, *, now: "int | None" = None, _crash_after: "str | None" = None) -> dict:
    """Run one compaction pass over the ledger (the default store, or `path`). Returns a small report dict:
    `{status, folded, pruned, generation}` (or `{status: "busy", ...}` on lock contention — the pass retries
    later, never writes lock-free). Held under the single-writer `.capture.lock` across the whole
    read→fold→write→bump→swap→rebuild critical section, so a live append or a second compaction waits (or
    no-ops and retries) rather than interleaving — no committed append is lost and two compactions cannot race.

    `_crash_after` is a TEST/DEMO-only power-cut injector — `"write"` (after the temp is written, before the
    swap) or `"swap"` (after the swap, before the index rebuild). Production callers never pass it. A real
    power-cut at either point (the OS releases the lock on process death, which the `finally` mirrors here)
    leaves exactly one intact ledger — old for `"write"` (the tidy never took), new for `"swap"` — and a temp /
    stale index that the next pass reaps / the generation gate routes to the scan until rebuilt."""
    target = path if path is not None else ledger.ledger_path()
    data_dir = os.path.dirname(target) or "."
    index_dst = os.path.join(data_dir, _index_filename())
    from memory import capture  # lazy: keep capture off the module-load path (cycle discipline)
    os.makedirs(data_dir, exist_ok=True)
    lock_fd = capture._acquire_lock(os.path.join(data_dir, capture.LOCK_FILENAME))
    if lock_fd is None:
        return {"status": "busy",
                "reason": "another memory write held the single-writer lock; compaction retries later",
                "folded": 0, "pruned": 0}
    try:
        _reap_temps(data_dir)                          # recovery: clear any prior-crash leftover, under the lock
        t0 = int(time.time()) if now is None else now
        raw = list(ledger.iter_records(path=target))
        access_index = forget._access_index(target)
        pruned = sum(1 for r in raw if isinstance(r, dict) and r.get("kind") == records.REINFORCEMENT_KIND)
        folded = sum(1 for r in raw if isinstance(r, dict)
                     and isinstance(r.get(records.RECORD_ID_KEY), str)
                     and access_index.get(r.get(records.RECORD_ID_KEY)))
        tmp = _write_compacted_temp(data_dir, raw, access_index, t0)
        if _crash_after == "write":
            raise _InjectedCrash("write")              # power-cut: temp left, OLD ledger intact, gen unbumped
        ledger.bump_generation(for_path=target)        # bump BEFORE the swap (the crash-safe ordering)
        ledger.replace_ledger(tmp, path=target)        # fsync temp → atomic rename → fsync dir
        if _crash_after == "swap":
            raise _InjectedCrash("swap")               # power-cut: NEW ledger in place, gen bumped, index stale
        from memory import index                       # lazy: index imports forget; import at use, not load
        index.rebuild(ledger_file=target, index_file=index_dst)
        return {"status": "ok", "folded": folded, "pruned": pruned,
                "generation": ledger.generation(for_path=target)}
    finally:
        capture._release_lock(lock_fd)                 # the OS frees the flock on a real power-cut; mirror it


def _index_filename() -> str:
    """The derived-index filename, read from index lazily (avoids importing index at module load)."""
    from memory import index
    return index.INDEX_FILENAME


# --- Operator demonstration -------------------------------------------------------------------------------
# A walkthrough on a THROWAWAY practice cabinet (a temp folder), never real data. It runs the REAL fold + swap +
# rebuild + recall and reads the cabinet back, so every claim is recognizable words/counts on screen — and
# prints ONLY plain language (never "compaction"/"generation"/"frecency"/"snapshot"/"tier"/"index"). It proves
# the load-bearing promise: a power-cut mid-tidy never loses or corrupts your memory. PART 2 exercises BOTH
# crash points (just before, and just after, the tidied copy is put in place). Vary the notes/ages near the top
# and re-run:
#     uv run --directory .engine --frozen -- python tools/memory/compact.py compact
_DEMO_SESSION = "session-harbor"
_DEMO_KEEP_TEXT = "Decided the harbor lights switch to solar next spring. DO-NOT-LOSE-THIS."
_DEMO_KEEP_WORD = "harbor"
_DEMO_KEEP2_TEXT = "Decided the ferry timetable moves to a 20-minute cadence in summer."
_DEMO_USED_TIMES = 4              # how many "used it" notes pile up behind the kept note
_DEMO_SET_ASIDE_TEXT = "Lesson: the old gantry crane jammed below freezing — never run it under 0C."
_DEMO_SET_ASIDE_WORD = "gantry"
_DEMO_SET_ASIDE_AGE_DAYS = 40    # untouched this long -> set aside (hidden from search), but never deleted
_DEMO_DUP_TEXT = "Decided the festival route skips the drawbridge this year."
_DEMO_DUP_WORD = "festival"
_DEMO_DAY = 86400


def _ledger_path() -> str:
    return ledger.ledger_path()


def _all_records() -> list:
    return [r for r in ledger.iter_records(path=_ledger_path()) if isinstance(r, dict)]


def _content_records() -> list:
    """Every recall-bearing record in the cabinet (notes + summaries), regardless of recall visibility — the
    thing a rewrite must never lose."""
    keep = (records.EPISODIC_KIND, "turn-delta")
    return [r for r in _all_records() if r.get("kind") in keep]


def _content_ids() -> set:
    return {r.get(records.RECORD_ID_KEY) for r in _content_records()}


def _bookkeeping_count() -> int:
    """The private 'used it' notes (reinforcement markers) physically in the cabinet."""
    return sum(1 for r in _all_records() if r.get("kind") == records.REINFORCEMENT_KIND)


def _recall_count(word: str) -> int:
    from memory import index
    return len(index.query(word).records)


def _in_cabinet(record_id: str) -> int:
    return sum(1 for r in _all_records() if r.get(records.RECORD_ID_KEY) == record_id)


def _freshness(record: dict) -> str:
    """The plain-language freshness of `record`, scored against its real (post-tidy) state + the real clock."""
    accesses = forget._access_index(_ledger_path()).get(record.get(records.RECORD_ID_KEY), ())
    return forget._FRESHNESS[score.tier(record, accesses)]


def _scratch_copies(data_dir: str) -> int:
    try:
        return sum(1 for n in os.listdir(data_dir) if n.startswith(_TEMP_PREFIX) and n.endswith(_TEMP_SUFFIX))
    except OSError:
        return 0


def _cabinet_whole() -> "tuple[bool, int]":
    """(is the one canonical cabinet present and complete — no torn final entry, AND, int — how many notes it
    holds)."""
    report = ledger.read(path=_ledger_path())
    whole = os.path.exists(_ledger_path()) and not report.torn_trailing
    return whole, len(report.records)


def _make_episodic(text: str, age_days: int, role: str = "decision", batchless: bool = True) -> dict:
    """A real episodic through the live factory, back-dated by `age_days`. `batchless` makes it always-live (not
    a crashed-pass orphan); pass batchless=False to leave its batch unclosed (a 4a retired duplicate)."""
    from memory import consolidate
    rec = consolidate._make_episodic(_DEMO_SESSION, {"role": role, "text": text}, "demo-batch")
    if batchless:
        rec.pop(records.BATCH_KEY, None)
    rec["ts"] = int(time.time()) - age_days * _DEMO_DAY
    return rec


def _rebuild() -> None:
    from memory import index
    index.rebuild()


def _snippet(text, width: int = 66) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _demo() -> int:
    import tempfile

    print("=" * 88)
    print("MEMORY — tidying away the private bookkeeping, safely, even if the power cuts out mid-tidy (practice)")
    print("=" * 88)
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["ENGINE_MEMORY_DIR"] = tmp          # the throwaway cabinet
        try:
            ok = _demo_body(tmp)
        finally:
            os.environ.pop("ENGINE_MEMORY_DIR", None)

    print("\n" + "-" * 88)
    print("What this just proved: the engine can tidy away the private 'used it' bookkeeping that piles up — and")
    print("a power-cut in the MIDDLE of that tidy never loses or corrupts a single note: you always end with one")
    print("whole filing cabinet (the old one if the tidy hadn't finished, the new one if it had), never a")
    print("half-written mess. Your real notes — even ones set aside from search, even a recovered duplicate —")
    print("all survive the rewrite. On your real data nothing is tidied yet: the engine doesn't record when you")
    print("use a note (that comes in a later step), so today this only runs in this practice demo. NOTHING is")
    print("ever erased here: tidying only removes the private bookkeeping; permanently erasing a real note is a")
    print("SEPARATE step you approve later by merging a pull request (and applying the `guardrail-ack` safety")
    print("label) — never this. That was a PRACTICE cabinet, thrown away when the demo ended; like all memory,")
    print("private, local, and deletable. Vary it: edit the notes and ages near the top of this file and re-run.")
    return 0 if ok else 1


def _demo_body(data_dir: str) -> bool:
    # --- PART 1 ------------------------------------------------------------------------------------------
    print("\nPART 1 — the private bookkeeping piles up, the tidy clears it, your note is untouched")
    print("-" * 88)
    keep = _make_episodic(_DEMO_KEEP_TEXT, age_days=0)
    keep_id = keep[records.RECORD_ID_KEY]
    ledger.append(keep)
    for _ in range(_DEMO_USED_TIMES):
        forget.record_access(keep_id)
    _rebuild()
    book_before = _bookkeeping_count()
    fresh_before = _freshness(keep)
    found_before = _recall_count(_DEMO_KEEP_WORD)
    print(f'  a note you use a lot: "{_snippet(_DEMO_KEEP_TEXT)}"')
    print(f"  private 'used it' bookkeeping piled up behind it: {book_before}")
    print(f'  search "{_DEMO_KEEP_WORD}" -> found {found_before}    freshness: {fresh_before}')
    report = compact()
    book_after = _bookkeeping_count()
    fresh_after = _freshness(keep)
    found_after = _recall_count(_DEMO_KEEP_WORD)
    print(f"  ...tidied. status: {report['status']}")
    print(f"  private 'used it' bookkeeping now: {book_after}   (folded away — no longer cluttering the cabinet)")
    print(f'  search "{_DEMO_KEEP_WORD}" -> found {found_after}    freshness: {fresh_after}')
    part1 = (book_before >= _DEMO_USED_TIMES and book_after == 0 and found_after == 1
             and fresh_after == fresh_before)
    print(f"  => {'tidied the bookkeeping; the note and its freshness are intact.' if part1 else '!!! the note or its freshness changed'}")

    # --- PART 2 ------------------------------------------------------------------------------------------
    print("\nPART 2 — a power-cut in the MIDDLE of the tidy never loses or corrupts your memory")
    print("-" * 88)
    ledger.append(_make_episodic(_DEMO_KEEP2_TEXT, age_days=0))      # a second real note, so 'N of N' is plural
    for _ in range(_DEMO_USED_TIMES):                  # pile the bookkeeping back up so there is something to tidy
        forget.record_access(keep_id)
    _rebuild()
    ids_before = _content_ids()
    print(f"  before the power-cut: {len(ids_before)} real notes in the cabinet, bookkeeping piled up again")

    print("\n  (a) the power cuts out JUST BEFORE the tidied copy is put in place")
    try:
        compact(_crash_after="write")
    except _InjectedCrash:
        pass
    whole_a, count_a = _cabinet_whole()
    present_a = _content_ids() == ids_before
    print(f"    the one filing cabinet is here and complete (no half-written entry): {'yes' if whole_a else 'NO'}")
    print(f"    entries on file: {count_a}  (the kept note + its bookkeeping)")
    print(f"    leftover half-finished scratch copies (ignored; cleaned up next tidy): {_scratch_copies(data_dir)}")
    print(f"    the DO-NOT-LOSE-THIS note still reads back: {'yes' if _in_cabinet(keep_id) >= 1 else 'NO'}")
    print(f"    every real note still present: {len(_content_ids())} of {len(ids_before)}")
    print(f"    the bookkeeping is still here (the tidy did NOT take): {_bookkeeping_count()}")
    part2a = whole_a and present_a and _bookkeeping_count() >= _DEMO_USED_TIMES
    print(f"    => {'the cabinet is UNCHANGED — one whole cabinet, nothing lost.' if part2a else '!!! the before-swap crash lost or changed something'}")

    print("\n  (b) the power comes back, it tries again, and cuts out JUST AFTER the tidied copy is put in place")
    try:
        compact(_crash_after="swap")
    except _InjectedCrash:
        pass
    whole_b, count_b = _cabinet_whole()
    present_b = _content_ids() == ids_before
    found_b = _recall_count(_DEMO_KEEP_WORD)           # answered by the slow backup (the quick-search isn't rebuilt yet)
    print(f"    the one filing cabinet is here and complete (no half-written entry): {'yes' if whole_b else 'NO'}")
    print(f"    entries on file: {count_b}  (the kept note; bookkeeping folded away)")
    print(f"    leftover half-finished scratch copies: {_scratch_copies(data_dir)}")
    print(f"    the DO-NOT-LOSE-THIS note still reads back: {'yes' if _in_cabinet(keep_id) >= 1 else 'NO'}")
    print(f"    every real note still present: {len(_content_ids())} of {len(ids_before)}")
    print(f"    the bookkeeping is now folded away (the tidy DID take): {_bookkeeping_count()}")
    print(f'    search still answers "{_DEMO_KEEP_WORD}" (via the slow backup until the fast search is refreshed): {found_b}')
    part2b = whole_b and present_b and _bookkeeping_count() == 0 and found_b == 1
    print(f"    => {'the tidied cabinet is in place and complete — one whole cabinet, nothing lost.' if part2b else '!!! the after-swap crash lost or changed something'}")

    # --- PART 3 ------------------------------------------------------------------------------------------
    print("\nPART 3 — nothing is erased: a set-aside note and a recovered duplicate both survive the rewrite")
    print("-" * 88)
    aside = _make_episodic(_DEMO_SET_ASIDE_TEXT, age_days=_DEMO_SET_ASIDE_AGE_DAYS, role="lesson")
    aside_id = aside[records.RECORD_ID_KEY]
    ledger.append(aside)
    dup = _make_episodic(_DEMO_DUP_TEXT, age_days=0, batchless=False)   # a crashed-pass orphan (batch never closed)
    dup_id = dup[records.RECORD_ID_KEY]
    ledger.append(dup)
    _rebuild()
    aside_hidden_before = _recall_count(_DEMO_SET_ASIDE_WORD) == 0
    dup_retired_before = dup_id not in {r.get(records.RECORD_ID_KEY) for r in forget.live_records()}
    compact()
    aside_in = _in_cabinet(aside_id)
    dup_in = _in_cabinet(dup_id)
    aside_hidden_after = _recall_count(_DEMO_SET_ASIDE_WORD) == 0
    dup_retired_after = dup_id not in {r.get(records.RECORD_ID_KEY) for r in forget.live_records()}
    print(f"  a note set aside from search (unused ~{_DEMO_SET_ASIDE_AGE_DAYS} days): \"{_snippet(_DEMO_SET_ASIDE_TEXT)}\"")
    print(f"    still in the cabinet after the rewrite: {aside_in}    still hidden from search: {'yes' if aside_hidden_after else 'NO'}")
    print(f"  a recovered duplicate (a save that didn't finish): \"{_snippet(_DEMO_DUP_TEXT)}\"")
    print(f"    still in the cabinet after the rewrite: {dup_in}    still kept out of recall: {'yes' if dup_retired_after else 'NO'}")
    part3 = (aside_in == 1 and dup_in == 1 and aside_hidden_before and aside_hidden_after
             and dup_retired_before and dup_retired_after)
    print(f"  => {'every real note survived the rewrite; only the private bookkeeping was removed.' if part3 else '!!! a real note was lost or its state changed'}")

    return part1 and part2a and part2b and part3


def main(argv: list) -> int:
    cmd = argv[0] if argv else "compact"
    if cmd == "compact":
        return _demo()
    print(f"usage: compact.py [compact]\nunknown command {cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
