"""forget.py — the engine's active forgetting: Layer-1 logical retirement + scored demotion over the memory
ledger (slices 4a + 4c).

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

Slice-4c scope: **scored demotion tiers**. A record is reinforced each time it is recalled — an append-only
`reinforcement` marker (records.py) naming it by its stable id (slice 4b). `score` (score.py) folds a record's
reinforcements into a **frecency x role-weight x recency** score and a tier (hot -> warm -> cold -> archived);
`live_records` additionally drops the **archived** ones from recall (still resident + recoverable in the
ledger — demotion never deletes), and drops the reinforcement markers themselves (pure derivation fuel, never
recall results). Tier is **derived on read**, never persisted — like 4a's retirement, the state is re-derived
from the ledger each rebuild, so it survives a throwaway-index rebuild. The live caller that appends a marker
on recall is **slice 5** (the search server); 4c ships the marker kind, the `record_access` appender, and the
operator demo (the `demote` verb) — with no live caller, the engine's own young ledger demotes nothing yet.

Slice-4d-ii scope: the **logical retirement of gist-rolled-up episodes**. A deferred AI-judged pass (`rollup.py`)
consolidates old, low-frecency EPISODIC summaries of one session into a compact GIST and supersedes the raws — a
per-raw `superseded` marker (records.py) names the raw by its stable id and the gist by `superseded_by`, under a
roll-up `batch` closed by a `rolled-up` marker. `live_records` retires a raw whose supersession's batch is CLOSED
(its gist is intact) — keyed on the ROLL-UP closed set (`_closed_rollup_batches`), NOT the consolidation set — so a
crash before the closing marker leaves every supersession inert and no raw is hidden without its gist. An orphaned
GIST of a crashed roll-up is itself retired (`_is_gist_orphan`), mirror of 4a's episodic orphan. The retired raw
stays resident + fully recoverable; physical erasure is Layer-2/4e.

The stable, content-free record id (slice 4b) is minted in the record factories (records/capture/consolidate),
not here; this module hosts its operator demo (the `identity` verb). Ledger compaction — the rebuild-and-swap
that folds the reinforcement markers into a carried frecency snapshot AND a closed-batch supersession into a carried
`superseded_by` field — is slice 4d-i and lives in `compact.py` (it needs the atomic file-replace primitive the
Layer-1 erasure-free source-scan bans HERE); Layer-2 audit-gated erasure is a later sub-slice (4e) of the memory
build plan's step 4. `record_access` (the reinforcement appender) is held under the shared single-writer lock so a
compaction swap can never race it. Perf forward-owe: 4c+4d-ii add O(ledger) passes to `live_records` (the access
index over the reinforcement markers; the supersession passes over the markers); 4d-i's compaction — folding those
markers into carried fields — is the designed retirement of that cost.
"""

from __future__ import annotations

import os
import sys
import time

# Make the package parent (.engine/tools) importable so `from memory import ledger` resolves even when this
# file is run directly as the demo script. Imported as `memory.forget`, the parent is already on sys.path, so
# this is a guarded no-op. Module-level imports stay limited to the cycle-free set `ledger` + `records` +
# `score` (each imports nothing that reaches back to `forget`): `index` imports THIS module for the fold, so
# importing `index`/`consolidate`/`capture` here would cycle — the demo + the appender import them lazily.
_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from memory import ledger, records, score  # noqa: E402


def _closed_batches(src: str) -> set:
    """The set of `batch` ids that a *completed* pass closed — i.e. carried by a `consolidated` marker."""
    closed = set()
    for record in ledger.iter_records(path=src):
        if isinstance(record, dict) and record.get("kind") == records.MARKER_KIND:
            batch = record.get(records.BATCH_KEY)
            if isinstance(batch, str) and batch:
                closed.add(batch)
    return closed


def _closed_rollup_batches(src: str) -> set:
    """The set of roll-up `batch` ids a *completed* roll-up closed — carried by a `rolled-up` marker (slice
    4d-ii). DISTINCT from `_closed_batches` (which reads `consolidated` markers): the two closure namespaces
    never mix — a 3b consolidation and a gist roll-up can never cross-close — and uuid batch ids are globally
    unique anyway, so the disjointness is belt-and-suspenders. One pass over the RAW ledger (like
    `_closed_batches`): a roll-up's supersessions take effect ONLY once their batch is in this set."""
    closed = set()
    for record in ledger.iter_records(path=src):
        if isinstance(record, dict) and record.get("kind") == records.ROLLUP_KIND:
            batch = record.get(records.BATCH_KEY)
            if isinstance(batch, str) and batch:
                closed.add(batch)
    return closed


def _superseded_by_map(src: str, closed_rollup: set) -> dict:
    """Map each raw episode's id -> the gist id that superseded it, from `superseded` markers whose roll-up
    `batch` is CLOSED (slice 4d-ii). One pass over the **RAW** ledger — `ledger.iter_records`, NOT
    `live_records` — exactly as `_closed_batches`/`_access_index` read markers raw: a `superseded` marker is
    itself dropped from recall (`_is_demoted`), and an aged-out one would score archived and drop too, so
    deriving the supersession off the filtered stream would let an old marker silently un-hide its raw. A marker
    in an un-closed (crashed-pass) batch is INERT and never enters the map — the load-bearing crash-safety:
    a raw is hidden only once its gist's pass completed. Skips malformed entries (a fallen line never costs the
    records after it)."""
    out: dict = {}
    for record in ledger.iter_records(path=src):
        if not isinstance(record, dict) or record.get("kind") != records.SUPERSEDED_KIND:
            continue
        batch = record.get(records.BATCH_KEY)
        if not isinstance(batch, str) or batch not in closed_rollup:
            continue
        raw_id = record.get(records.TARGET_KEY)
        gist_id = record.get(records.SUPERSEDED_BY_KEY)
        if isinstance(raw_id, str) and raw_id and isinstance(gist_id, str) and gist_id:
            out[raw_id] = gist_id
    return out


def _access_index(src: str) -> dict:
    """Map each reinforced record's id -> the wall-clock `ts` of every `reinforcement` marker that names it
    (slice 4c). One pass over the **RAW** ledger — `ledger.iter_records`, NOT `live_records` — exactly as
    `_closed_batches` reads markers raw. This is load-bearing: a `reinforcement` marker is itself dropped by
    `live_records` (it is never a recall result), and a marker for an already-*archived* record would be
    dropped with it — so scoring off the filtered stream would hide the very accesses that should keep or
    restore that record, and an archived record could never climb back. The scorer must see ALL reinforcements.
    Skips malformed entries (a fallen line never costs the records after it)."""
    index: dict = {}
    for record in ledger.iter_records(path=src):
        if not isinstance(record, dict) or record.get("kind") != records.REINFORCEMENT_KIND:
            continue
        target = record.get(records.TARGET_KEY)
        ts = record.get("ts")
        if isinstance(target, str) and target and isinstance(ts, int) and not isinstance(ts, bool):
            index.setdefault(target, []).append(ts)
    return index


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


def _is_gist_orphan(record, closed_rollup: set) -> bool:
    """True iff `record` is a GIST orphaned by a crashed roll-up pass — its `batch` is set but no closing
    `rolled-up` marker landed (slice 4d-ii). The mirror of `_is_retired` for a gist, but keyed on the ROLL-UP
    closed set (`closed_rollup`), NEVER the consolidation `closed` set: a gist's batch is closed by a `rolled-up`
    marker, which `_closed_batches` never sees, so keying it on the consolidation set would wrongly retire EVERY
    completed gist. A batchless or closed gist stays live (and recall-scored like an episodic). So a crashed
    roll-up shows neither the orphan gist nor hides its raws — exactly one intact state."""
    if not isinstance(record, dict) or record.get("kind") != records.GIST_KIND:
        return False
    batch = record.get(records.BATCH_KEY)
    if not isinstance(batch, str) or not batch:
        return False
    return batch not in closed_rollup


def _is_superseded(record, superseded_ids: set) -> bool:
    """True iff recall should drop `record` because a COMPLETED roll-up consolidated it into a gist (slice
    4d-ii) — either a closed-batch `superseded` marker names it (`superseded_ids`, derived from the RAW ledger
    by `_superseded_by_map`) OR it carries the folded `superseded_by` field a compaction stamped (minted only
    across a closed gate, so its mere presence proves the gist pass completed — trusted unconditionally). The raw
    stays resident + fully recoverable in the ledger (logical retirement, reversible — physical erasure is
    Layer-2/4e); recall just doesn't surface it, so its gist is the one copy recall returns. ORTHOGONAL to the
    frecency score — a superseded raw is dropped even if it would score hot (the two exclusions OR together)."""
    if not isinstance(record, dict):
        return False
    if record.get(records.SUPERSEDED_BY_KEY):
        return True
    rid = record.get(records.RECORD_ID_KEY)
    return isinstance(rid, str) and bool(rid) and rid in superseded_ids


def _is_demoted(record, access_index: dict, now: int) -> bool:
    """True iff recall should NOT surface `record` for a demotion reason (slice 4c):
      * a `reinforcement` marker — pure derivation fuel for the scorer, never itself a recall result; or
      * a record scored into the **archived** tier (frecency x role-weight x recency, score.py) — excluded
        from the hot index, but it stays resident + fully recoverable in the ledger (demotion never deletes).
    `consolidated` markers are NEVER demoted here — they are structural (carry no recall text) and stay
    always-live, unchanged from 4a. The gist roll-up markers (`superseded` + `rolled-up`, slice 4d-ii) ARE
    dropped from recall here like `reinforcement`: pure bookkeeping (no recall text), never a recall result.
    Everything else (turn-deltas, episodics, gists) is scored by its own reinforcements (`access_index[id]`,
    empty for an un-reinforced record — born hot from its `ts`); a gist is recall content and demotes like an
    episodic. The slice-4e `operator-adjudicated-erasure` marker is likewise dropped from recall (pure
    content-free bookkeeping — it authorises erasing its target, it is never itself a recall result)."""
    if not isinstance(record, dict):
        return False
    kind = record.get("kind")
    if kind in (records.REINFORCEMENT_KIND, records.SUPERSEDED_KIND, records.ROLLUP_KIND, records.ERASURE_KIND):
        return True
    if kind == records.MARKER_KIND:
        return False
    access_ts = access_index.get(record.get(records.RECORD_ID_KEY), ())
    return score.tier(record, access_ts, now) == score.ARCHIVED


def record_access(target_id: str, *, path: "str | None" = None, now: "int | None" = None) -> None:
    """Append a `reinforcement` (access) marker naming `target_id` — the move slice-5 recall makes on every
    hit, recorded so demotion can score usage by the stable record id (slice 4b). A no-op on a blank/non-str
    target. APPENDS ONLY; it never deletes or rewrites (the Layer-1 erasure-free invariant — a test source-
    scans this module). Reuses the crash-safe `ledger.append` primitive; the marker is an ordinary record.

    Held under the shared single-writer `.capture.lock` (slice 4d): it is the one live writer that would
    otherwise append lock-free, so a compaction swap (which holds that lock across its whole fold-and-swap) could
    race it and lose the marker as the ledger is renamed away. On contention the call is a clean no-op — a
    missed access marker is harmless bookkeeping (slice-5 recall reinforces again on the next hit); it NEVER
    writes lock-free. The lock lives in the SAME directory the write resolves to (so the lock and the file never
    diverge under a `path` override)."""
    if not isinstance(target_id, str) or not target_id:
        return
    from memory import capture  # lazy: keep capture off the module-load path (cycle discipline)
    target = path if path is not None else ledger.ledger_path()
    data_dir = os.path.dirname(target) or "."
    os.makedirs(data_dir, exist_ok=True)
    lock_fd = capture._acquire_lock(os.path.join(data_dir, capture.LOCK_FILENAME))
    if lock_fd is None:
        return  # a compaction or a live capture holds the single-writer lock — skip this marker, never write lock-free
    try:
        marker = {
            "v": capture.RECORD_VERSION,
            "kind": records.REINFORCEMENT_KIND,
            records.RECORD_ID_KEY: records.new_record_id(),
            records.TARGET_KEY: target_id,
            "ts": int(time.time()) if now is None else now,
            "tags": [records.REINFORCEMENT_TAG],
        }
        ledger.append(marker, path=path)
    finally:
        capture._release_lock(lock_fd)


def _is_ambient_capture(record) -> bool:
    """True iff `record` is an ambient `turn-delta` capture — the role-less, `Stop`-appended verbatim that is fuel
    for consolidation and the abandoned-session sweep, never recall content (D-273/D-274, issue #332). The single
    recall-membership discriminator; recall drops it on every path via `live_records`."""
    return isinstance(record, dict) and record.get("kind") == records.AMBIENT_CAPTURE_KIND


def live_records(path: "str | None" = None, *, now: "int | None" = None):
    """Yield the ledger records recall should surface.

    Recall surfaces the curated layer, NOT ambient capture (D-273/D-274, issue #332): a `turn-delta` is the
    role-less, `Stop`-appended verbatim — fuel for consolidation and the abandoned-session sweep, NEVER recall
    content. `_is_ambient_capture` drops it here, on the ONE shared read path the fast (FTS5) and slow (scan)
    lookups both consume, so the exclusion holds identically on every path — including the degraded plain scan the
    #332 verdict singles out. The discriminator is the record's `kind`, re-derived on every read / index rebuild:
    no per-record marker, no carried bit, so membership survives compaction for free and edits no ledger line in
    place. (A targeted exclusion of the ambient kind, not a curated-kind allowlist: a record carrying a `role` +
    `text` but no explicit kind is an episodic-shaped recall record and stays surfaced — only the named ambient
    kind is fuel. A future *ambient* kind must be added to `_is_ambient_capture` to stay out of recall.)

    The four pre-existing exclusions still trim a record that is retired or demoted: (a) an episodic a crashed
    consolidation pass orphaned (logical retirement, 4a); (b) a reinforcement marker / a record scored into the
    archived tier (scored demotion, 4c); (c) a raw episode a COMPLETED gist roll-up superseded, a crashed roll-up's
    orphaned gist, and the roll-up markers (gist roll-up, 4d-ii). A dropped record stays in the ledger, fully
    recoverable; this generator just doesn't surface it.

    Cheap sequential passes over the RAW ledger (never the filtered stream): the consolidation + roll-up closed
    sets, the supersession map, and the reinforcement access index; then stream, dropping a record if ANY exclusion
    fires (they OR together — any one reason hides it). `now` is resolved once so every record in one rebuild scores
    against a single clock. Mutates nothing — never writes, never deletes."""
    src = ledger.ledger_path() if path is None else path
    closed = _closed_batches(src)
    closed_rollup = _closed_rollup_batches(src)
    superseded = set(_superseded_by_map(src, closed_rollup))
    access_index = _access_index(src)
    now = int(time.time()) if now is None else now
    for record in ledger.iter_records(path=src):
        if (not _is_ambient_capture(record)
                and not _is_retired(record, closed)
                and not _is_gist_orphan(record, closed_rollup)
                and not _is_superseded(record, superseded)
                and not _is_demoted(record, access_index, now)):
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


# --- Operator demonstration: scored demotion tiers (slice 4c) ---------------------------------------------
# A THROWAWAY-cabinet walkthrough for "active forgetting": a note left unused for weeks is set aside from
# search but stays in the cabinet (recoverable), and using it again brings it straight back. It runs the REAL
# factories + record_access + rebuild + recall and reads the cabinet back, so every claim is recognizable words
# on screen — and prints only plain-language labels (never "tier"/"frecency"/"reinforcement"). Vary the notes
# and the ages near the top and re-run:
#     uv run --directory .engine --frozen -- python tools/memory/forget.py demote
_DEMO_DEMOTE_SESSION = "session-orchard"
_DEMO_FRESH_TEXT = "Decided the orchard layout ships with the apple rows first."
_DEMO_OLD_TEXT = "Lesson: the midnight cron double-ran — never schedule it at 00:00. DO-NOT-LOSE-THIS."
_DEMO_FRESH_WORD = "apple"
_DEMO_OLD_WORD = "midnight"
_DEMO_OLD_AGE_DAYS = 35          # ~2.5 half-lives untouched -> set aside (archived)
_DEMO_REINFORCE_TIMES = 3
_DEMO_DAY = 86400

# Plain-language names for the freshness tiers — what the operator sees instead of hot/warm/cold/archived.
_FRESHNESS = {score.HOT: "fresh", score.WARM: "getting stale", score.COLD: "stale",
              score.ARCHIVED: "set aside (hidden from search)"}


def _days(n: int) -> str:
    """'1 day' / '35 days' — keep the screen grammatical if the operator varies the age."""
    return f"{n} day" if n == 1 else f"{n} days"


def _make_demo_episodic(role: str, text: str, age_days: int) -> dict:
    """A real episodic through the live factory, made BATCHLESS (so it is always-live — never mistaken for a
    crashed-pass orphan, 4a) and back-dated by `age_days` so the demo can age it without sleeping."""
    from memory import consolidate  # lazy (consolidate -> index -> forget would cycle at module load)
    rec = consolidate._make_episodic(_DEMO_DEMOTE_SESSION, {"role": role, "text": text}, "demo-batch")
    rec.pop(records.BATCH_KEY, None)
    rec["ts"] = int(time.time()) - age_days * _DEMO_DAY
    return rec


def _demo_recall_count(word: str) -> int:
    from memory import index  # lazy
    return len(index.query(word).records)


def _demo_in_cabinet(record_id: str) -> int:
    """How many records carrying `record_id` are physically in the ledger (the recoverability proof)."""
    return sum(1 for r in ledger.iter_records(path=ledger.ledger_path())
               if isinstance(r, dict) and r.get(records.RECORD_ID_KEY) == record_id)


def _demo_freshness(record: dict) -> str:
    """The plain-language freshness of `record`, scored against its real accesses + the real wall clock."""
    accesses = _access_index(ledger.ledger_path()).get(record.get(records.RECORD_ID_KEY), ())
    return _FRESHNESS[score.tier(record, accesses)]


def _demo_demote() -> int:
    import tempfile

    print("=" * 80)
    print("MEMORY — setting an unused note aside from search without losing it, and bringing it back (practice)")
    print("=" * 80)
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["ENGINE_MEMORY_DIR"] = tmp          # the throwaway cabinet
        try:
            ok = _demo_demote_body()
        finally:
            os.environ.pop("ENGINE_MEMORY_DIR", None)

    print("\n" + "-" * 80)
    print(f"What this just proved: a note nobody had used in ~{_days(_DEMO_OLD_AGE_DAYS)} was SET ASIDE from search but")
    print("stayed in the cabinet (read back above: 1 record), and ONE use brought it straight back — reversible, on screen.")
    print("On your real data, nothing changes yet: the engine doesn't record when you use a note (that trigger")
    print("ships in a later step), so today this only runs in this practice demo on the old note it invents — the")
    print("machinery is here, the live trigger comes later. NOTHING is ever erased here: setting-aside is")
    print("hide-from-search only; permanently erasing a record is a SEPARATE step you approve later by merging a")
    print("single-purpose pull request (and applying the `guardrail-ack` safety label) — never this. That was a")
    print("PRACTICE cabinet, thrown away when the demo ended; like all memory, private, local, and deletable.")
    print("Vary it: edit the notes and the age near the top of this file and re-run.")
    return 0 if ok else 1


def _demo_demote_body() -> bool:
    print("\nPART 1 — a fresh note is found in search")
    print("-" * 80)
    fresh = _make_demo_episodic("decision", _DEMO_FRESH_TEXT, age_days=0)
    ledger.append(fresh)
    _demo_rebuild()
    fresh_hits = _demo_recall_count(_DEMO_FRESH_WORD)
    print(f'  a note from today: "{_snippet(_DEMO_FRESH_TEXT)}"')
    print(f'  search "{_DEMO_FRESH_WORD}" -> found {fresh_hits}    freshness: {_demo_freshness(fresh)}')
    part1 = fresh_hits == 1
    print(f"  => {'a fresh note is in search.' if part1 else '!!! a fresh note was not found'}")

    print(f"\nPART 2 — a note unused for ~{_days(_DEMO_OLD_AGE_DAYS)} is SET ASIDE from search, but stays in the cabinet")
    print("-" * 80)
    old = _make_demo_episodic("lesson", _DEMO_OLD_TEXT, age_days=_DEMO_OLD_AGE_DAYS)
    old_id = old[records.RECORD_ID_KEY]
    ledger.append(old)
    _demo_rebuild()
    old_hits = _demo_recall_count(_DEMO_OLD_WORD)
    in_cab = _demo_in_cabinet(old_id)
    print(f'  a note nobody has used in ~{_days(_DEMO_OLD_AGE_DAYS)}: "{_snippet(_DEMO_OLD_TEXT)}"')
    print(f'  search "{_DEMO_OLD_WORD}" -> found {old_hits}    freshness: {_demo_freshness(old)}')
    print(f"  still in the cabinet: {in_cab} record(s)  (hidden from search, NOT deleted)")
    part2 = old_hits == 0 and in_cab == 1
    print(f"  => {'set aside from search, still in the cabinet (recoverable).' if part2 else '!!! the old note was lost or still searchable'}")

    print("\nPART 3 — using it again brings it straight back")
    print("-" * 80)
    for _ in range(_DEMO_REINFORCE_TIMES):
        record_access(old_id)
    _demo_rebuild()
    back_hits = _demo_recall_count(_DEMO_OLD_WORD)
    print(f"  used the old note {_DEMO_REINFORCE_TIMES} times (what search will do for you in a later step)")
    print(f'  search "{_DEMO_OLD_WORD}" -> found {back_hits}    freshness: {_demo_freshness(old)}')
    part3 = back_hits == 1
    print(f"  => {'using it restored it to search — nothing was ever deleted.' if part3 else '!!! using it did not restore the note'}")

    print('\nPART 4 — the private "when you used it" notes never show up in search')
    print("-" * 80)
    from memory import index  # lazy
    surfaced = index.query(_DEMO_OLD_WORD).records + index.query(old_id).records
    leaked = [r for r in surfaced if isinstance(r, dict) and r.get("kind") == records.REINFORCEMENT_KIND]
    print("  searched for the note's words AND for its private name-tag;")
    print(f'  "when you used it" notes returned by search: {len(leaked)}')
    part4 = len(leaked) == 0
    print(f"  => {'the usage notes are private bookkeeping, never search results.' if part4 else '!!! a usage note leaked into search'}")

    return part1 and part2 and part3 and part4


def _demo_rebuild() -> None:
    from memory import index  # lazy
    index.rebuild()


def main(argv: list) -> int:
    cmd = argv[0] if argv else "demo"
    if cmd == "duplicates":
        return _print_duplicates()
    if cmd == "demo":
        return _demo()
    if cmd == "identity":
        return _demo_identity()
    if cmd == "demote":
        return _demo_demote()
    print(f"usage: forget.py [duplicates|demo|identity|demote]\nunknown command {cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
