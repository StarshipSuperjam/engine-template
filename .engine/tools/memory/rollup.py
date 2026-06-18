"""rollup.py — gist roll-up: AI-judged second-order consolidation of old episodes (memory-substrate, slice 4d-ii).

Active forgetting's first move (memory/README): a perpetual project cannot only accumulate. A deferred,
AI-judged maintenance pass **consolidates old, related, low-frecency EPISODIC summaries of one session into a
compact GIST and logically retires the raw episodes** — the SECOND-order consolidation (episode→gist), exactly
parallel to slice-3b's first-order (delta→episode). This module is **Layer 1**: *reversible, mechanical,
memory-autonomous* tidying that needs no human gate because **nothing is lost** — a retired raw is excluded from
recall but stays resident + fully recoverable in the one ledger (physical erasure is Layer-2/4e, audit-gated).

What one roll-up pass does, under the single-writer `.capture.lock`, **append-only, in STRICT ORDER**:

  1. Append the **GIST** record (`gist` kind, the AI-written summary, carrying `source_ids` = the raw ids it
     consolidates + the pass `batch`).
  2. Append one **`superseded` marker per raw** (points at the raw by `target`, names the gist by `superseded_by`,
     carries the `batch`).
  3. Append the closing **`rolled-up` marker LAST** — the ONLY kind that CLOSES a roll-up batch — then rebuild
     the index.

**The crash-safety spine.** A raw is retired from recall ONLY once its roll-up batch is closed (`forget`'s
supersession gate keys on `_closed_rollup_batches`), so a crash before the closing marker leaves every
`superseded` marker INERT — the raws stay live, the orphaned gist is itself retired (`forget._is_gist_orphan`),
and nothing is hidden without its gist. The closing kind is DISTINCT from 3b's `consolidated` marker so a
roll-up never spuriously marks a session consolidated. APPENDS ONLY — never deletes or rewrites (the Layer-1
erasure-free invariant; the swap that prunes the folded markers lives in `compact.py`, slice 4d-i, which folds a
closed-batch supersession into the raw's carried `superseded_by` field).

**Degenerate live (expected, precedented — the 4c/4d-i shape).** The live caller — a 3b-shaped `SessionStart`
background sweep that hands the in-context AI the cold session-groups to judge (a hook cannot think, so it cannot
ride a non-AI hook) — is **slice 5**, alongside the recall-time reinforcement caller and the live compaction
trigger. Until then the engine's young ledger has no old, low-frecency episodes, so a roll-up pass finds no
candidates; 4d-ii ships the mechanism + the manual verbs + the operator demo. The selection floors (COLD tier,
group-by-session, ≥3 per group, episodics only) are build-spec leaves recorded with the maintainer.

Leaf discipline (principle §16): the detect/store functions RETURN a report and render no operator-facing prose
(the demo is the one operator surface). stdlib + the cycle-free `memory` set (forget / ledger / records / score);
`capture` (the lock + record version), `index` (the rebuild), and `consolidate` (the closed role vocabulary +
the demo's episodic factory) are lazy-imported to keep them off the module-load path.
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

# --- Build-spec leaves (the roll-up selection floors) -----------------------------------------------------
# A candidate is a raw EPISODIC (not a gist — v1 keeps the link single-hop raw→gist; recursive meta-gist is a
# deferred concern), not already superseded, that scores into the COLD tier (≈16–30 days untouched — "old +
# low-frecency", not yet ARCHIVED, which 4c already index-excludes). Grouped by `session_id` (the deterministic
# coarse "related" pre-filter; the AI judges true relatedness within the group). A group needs >= _MIN_GROUP raws
# (a "gist" of one raw is a rename). All recorded with the maintainer; richer relatedness deferred.
_MIN_GROUP = 3


class _InjectedCrash(Exception):
    """A TEST/DEMO-only fault, raised at a chosen point in `store_gist` to model a power-cut. Production callers
    never pass `_crash_after`, so this never fires in real use (a test pins the default off)."""


# --- Detecting candidates (the selection leaf) ------------------------------------------------------------

def detect_rollup_candidates(*, cwd=None, now: "int | None" = None) -> dict:
    """Group the raw EPISODIC records eligible for roll-up by session: COLD-tier (`score.tier`), not already
    superseded, not a crashed-pass orphan, in a group of >= _MIN_GROUP. A DETECT leaf — returns
    `{session_id: [record, ...]}` and renders no operator prose. The mechanism selects candidates
    deterministically; the in-context AI judges the roll-up (the gist text + which raws cohere) in-session."""
    src = ledger.ledger_path(cwd)
    now = int(time.time()) if now is None else now
    access_index = forget._access_index(src)
    closed = forget._closed_batches(src)
    closed_rollup = forget._closed_rollup_batches(src)
    superseded = set(forget._superseded_by_map(src, closed_rollup))
    groups: dict = {}
    for record in ledger.iter_records(path=src):
        if not isinstance(record, dict) or record.get("kind") != records.EPISODIC_KIND:
            continue
        rid = record.get(records.RECORD_ID_KEY)
        if not isinstance(rid, str) or not rid or rid in superseded:
            continue
        if forget._is_retired(record, closed):          # a crashed-pass orphan (4a) — already out of recall
            continue
        accesses = access_index.get(rid, ())
        if score.tier(record, accesses, now) != score.COLD:
            continue
        sid = record.get("session_id")
        if not isinstance(sid, str) or not sid:
            continue
        groups.setdefault(sid, []).append(record)
    return {sid: recs for sid, recs in groups.items() if len(recs) >= _MIN_GROUP}


def read_candidates(session_id: str, *, cwd=None, now: "int | None" = None) -> list:
    """The roll-up candidate episodes for one session as `[{id, role, text}, ...]` — what the in-context AI
    reads to write a gist and name its `source_ids`. A pure read (no lock); empty if the session is not a
    candidate group."""
    recs = detect_rollup_candidates(cwd=cwd, now=now).get(session_id, [])
    return [{"id": r.get(records.RECORD_ID_KEY), "role": r.get("role"), "text": r.get("text")} for r in recs]


# --- Writing the roll-up (idempotent, race-safe, reject-not-coerce, strict order) -------------------------

def _validate(gists) -> "str | None":
    """None if every gist is a well-formed {role in the closed vocabulary, non-empty text, non-empty list of
    source ids}; else a plain rejection reason. REJECT-not-coerce and WHOLE-BATCH-atomic: one bad gist stores
    nothing (the engine never silently guesses a label or a source it was not given)."""
    from memory import consolidate  # lazy: reuse the closed role vocabulary (3b) without an import cycle
    if not isinstance(gists, (list, tuple)) or not gists:
        return "no gists to store"
    for i, g in enumerate(gists):
        if not isinstance(g, dict):
            return f"gist {i} is not an object"
        if g.get("role") not in consolidate.ROLES:
            return f"gist {i} has label {g.get('role')!r}, which is not one of {list(consolidate.ROLE_VOCABULARY)}"
        text = g.get("text")
        if not isinstance(text, str) or not text.strip():
            return f"gist {i} has empty text"
        sources = g.get(records.SOURCE_IDS_KEY)
        if not isinstance(sources, (list, tuple)) or not sources:
            return f"gist {i} names no source notes to roll up"
        if not all(isinstance(s, str) and s for s in sources):
            return f"gist {i} has a malformed source note id"
    return None


def _make_gist(session_id: str, rec: dict, batch: str) -> dict:
    """The gist record envelope. Only `text` is human content; `role`/`tags` are secondary filters and
    `ts`/`v`/`batch`/`source_ids` are non-content — so the derived index keeps them OUT of the search body
    (index._NON_BODY_KEYS + the string-leaf-only projection). `source_ids` is the forward half of the gist↔raw
    link; `batch` is this pass's id (shared with its markers), by which `forget` derives a crashed pass's gist
    orphan to retire and a completed pass's supersessions to honor."""
    from memory import capture  # lazy: the record-version envelope
    now = int(time.time())
    tags = [records.GIST_TAG, records.DEFAULT_EPISODIC_TAG]   # rides episodic recall, but tagged a gist
    for t in rec.get("tags") or []:
        if isinstance(t, str) and t and t not in tags:
            tags.append(t)
    sources = [s for s in rec[records.SOURCE_IDS_KEY] if isinstance(s, str) and s]
    return {
        "v": capture.RECORD_VERSION,
        "kind": records.GIST_KIND,
        records.RECORD_ID_KEY: records.new_record_id(),     # the stable, content-free record id (slice 4b)
        "session_id": session_id,
        "ts": now,
        "role": rec["role"],
        "text": rec["text"].strip(),
        "tags": tags,
        records.BATCH_KEY: batch,
        records.SOURCE_IDS_KEY: sources,
    }


def _make_superseded_marker(raw_id: str, gist_id: str, batch: str) -> dict:
    """A per-raw supersession marker: this raw episode's content now lives in `gist_id`. Carries no recall text
    (pure bookkeeping — `forget._is_demoted` drops it from recall, `index` keeps its uuid-hex `target`/
    `superseded_by` out of the body). INERT until its `batch` is closed by a `rolled-up` marker."""
    from memory import capture  # lazy
    return {
        "v": capture.RECORD_VERSION,
        "kind": records.SUPERSEDED_KIND,
        records.RECORD_ID_KEY: records.new_record_id(),
        records.TARGET_KEY: raw_id,                 # the raw episode this supersedes (by its stable id)
        records.SUPERSEDED_BY_KEY: gist_id,         # the gist that now holds its content
        "ts": int(time.time()),
        records.BATCH_KEY: batch,
        "tags": [records.SUPERSEDED_KIND],
    }


def _make_rollup_marker(session_id: str, batch: str) -> dict:
    """The closing marker of a roll-up pass — the ONLY kind that CLOSES its `batch`. Written LAST: a crash
    before it leaves the pass's gist orphaned and its supersessions inert (favor a no-op over a wrong hiding —
    `forget` then surfaces the raws and retires the orphan gist, all recoverable). DISTINCT from 3b's
    `consolidated` marker so a roll-up never marks a session consolidated."""
    from memory import capture  # lazy
    return {
        "v": capture.RECORD_VERSION,
        "kind": records.ROLLUP_KIND,
        records.RECORD_ID_KEY: records.new_record_id(),
        "session_id": session_id,
        "ts": int(time.time()),
        records.BATCH_KEY: batch,
        "tags": [records.ROLLUP_KIND],
    }


def store_gist(session_id: str, gists, *, cwd=None, _crash_after: "str | None" = None) -> dict:
    """Append the AI-authored gist(s) for `session_id`, their per-raw `superseded` markers, then the closing
    `rolled-up` marker, then rebuild the fast lookup. Returns a small report dict.

    Safety: validate (reject-not-coerce, whole-batch) BEFORE any write; then, under the SHARED capture lock (so
    a concurrent capture/consolidation/compaction can never interleave — a compaction holds it across its whole
    swap, so a roll-up runs entirely before or after, never during), skip if any named source raw is ALREADY
    superseded (idempotent — re-running over rolled-up raws is a clean no-op), and append in STRICT ORDER: each
    gist, then its supersession markers, then the closing marker LAST. A crash before the closing marker re-runs
    next pass (the orphan gist + inert markers are recoverable, the raws stay live — favor a no-op over a wrong
    hiding). Lock contention => a clean no-op (the pass retries); it NEVER writes lock-free.

    `_crash_after` is a TEST/DEMO-only power-cut injector — `"markers"` (gist + supersessions written, the
    closing marker NOT — the batch stays un-closed) or `"close"` (the closing marker written, the index NOT yet
    rebuilt — the ledger is correct, the fast search is transiently stale until refreshed). Production callers
    never pass it."""
    reason = _validate(gists)
    if reason is not None:
        return {"status": "rejected", "reason": reason, "stored": 0}
    if not isinstance(session_id, str) or not session_id:
        return {"status": "rejected", "reason": "missing session id", "stored": 0}

    from memory import capture, index  # lazy: keep off the module-load path (cycle discipline)
    data_dir = ledger.ledger_dir(cwd)
    os.makedirs(data_dir, exist_ok=True)
    lock_fd = capture._acquire_lock(os.path.join(data_dir, capture.LOCK_FILENAME))
    if lock_fd is None:
        return {"status": "busy", "reason": "another memory write held the lock; the roll-up retries later",
                "stored": 0}
    try:
        src = ledger.ledger_path(cwd)
        closed_rollup = forget._closed_rollup_batches(src)
        already = set(forget._superseded_by_map(src, closed_rollup))     # raw ids a COMPLETED roll-up already took
        requested = [s for g in gists for s in g[records.SOURCE_IDS_KEY]]
        if any(s in already for s in requested):
            return {"status": "already-rolled-up", "stored": 0}
        batch = uuid.uuid4().hex   # ONE id for this whole pass, stamped on every gist, marker, and the closer
        stored = 0
        for g in gists:
            gist = _make_gist(session_id, g, batch)
            ledger.append(gist, path=src)
            gist_id = gist[records.RECORD_ID_KEY]
            for raw_id in g[records.SOURCE_IDS_KEY]:
                ledger.append(_make_superseded_marker(raw_id, gist_id, batch), path=src)
            stored += 1
        if _crash_after == "markers":
            raise _InjectedCrash("markers")   # gist + supersessions written, batch UN-closed — supersessions inert
        # Closing marker LAST, carrying the SAME batch: only now do this pass's supersessions take effect.
        ledger.append(_make_rollup_marker(session_id, batch), path=src)
        if _crash_after == "close":
            raise _InjectedCrash("close")     # ledger correct (raws filed away), fast index NOT yet rebuilt
        index.rebuild(ledger_file=src, index_file=index.index_path(cwd))
        return {"status": "ok", "stored": stored, "batch": batch}
    finally:
        capture._release_lock(lock_fd)


# --- CLI (the verbs the in-context AI / operator call) ----------------------------------------------------

def main(argv: list) -> int:
    cmd = argv[0] if argv else "demo"
    if cmd == "detect":
        groups = detect_rollup_candidates()
        for sid in sorted(groups):
            print(f"{sid}\t{len(groups[sid])}")
        return 0
    if cmd == "read":
        if len(argv) < 2:
            print("usage: rollup.py read <session-id>", file=sys.stderr)
            return 2
        print(json.dumps(read_candidates(argv[1]), ensure_ascii=False, indent=2))
        return 0
    if cmd == "store":
        if len(argv) < 2:
            print("usage: rollup.py store <session-id>   (a JSON array of {role,text,source_ids} on stdin)",
                  file=sys.stderr)
            return 2
        try:
            gists = json.load(sys.stdin)
        except ValueError as exc:
            print(f"could not parse the gists from stdin (expected a JSON array): {exc}", file=sys.stderr)
            return 2
        report = store_gist(argv[1], gists)
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report.get("status") in ("ok", "already-rolled-up") else 1
    if cmd == "demo":
        return _demo()
    print(f"usage: rollup.py [detect|read <sid>|store <sid>|demo]\nunknown command {cmd!r}", file=sys.stderr)
    return 2


# --- Operator demonstration -------------------------------------------------------------------------------
# A walkthrough on a THROWAWAY practice cabinet (a temp folder), never real data. It runs the REAL detect + store
# + fold (compact) + recall and reads the cabinet back, so every claim is recognizable words/counts on screen —
# and prints ONLY plain language (never "gist"/"supersession"/"compaction"/"frecency"). The ONE step that needs a
# live AI — writing the one-line summary — is SIMULATED with a clearly-labelled sample (the FILING around it is
# real). It proves: old notes roll into one summary; the originals are filed away (no longer turn up in a search)
# but stay saved; a power-cut mid-roll-up loses nothing and never hides an original without its summary; and a
# later tidy keeps the original→summary link and erases nothing. Vary the notes/age/crash-point near the top:
#     uv run --directory .engine --frozen -- python tools/memory/rollup.py demo
_DEMO_AGE_DAYS = 25          # old enough to be eligible for roll-up; lower it below ~16 days and nothing rolls up
_DEMO_DAY = 86400
# Three old notes of one session. Each carries a UNIQUE word (tulips/herbs/beans) that is NOT in the summary, so
# a search for it visibly flips from "found" to "not found" once the note is filed away — the honest proof that
# roll-up trades the originals' individual search-reach for the one summary. The first carries a recognizable
# DO-NOT-LOSE-THIS phrase so its survival in the cabinet is unmistakable.
_DEMO_SESSION = "session-garden"
_DEMO_OLD_NOTES = [
    ("decision", "Decided the north beds get tulips in spring — DO-NOT-LOSE-THIS.", "tulips"),
    ("decision", "Decided the south beds get herbs by the kitchen door.", "herbs"),
    ("lesson", "Lesson: the east fence trellis must be sunk two feet to hold the beans.", "beans"),
]
_DEMO_KEEP_PHRASE = "DO-NOT-LOSE-THIS"
# A younger note of the same session — NOT old enough to roll up, so it must be left untouched.
_DEMO_YOUNG_NOTE = ("decision", "Decided the new pond goes in by the old oak this weekend.", "pond")
_DEMO_YOUNG_WORD = "pond"
# The AI's one-line summary (SIMULATED). Its distinctive word "almanac" appears in NONE of the originals (nor in
# the other sessions' practice summaries below), so a search for it finds THIS summary and only this one.
_DEMO_GIST = {"role": "decision",
              "text": "Garden beds almanac: each bed and its supports assigned by area across several older sessions."}
_DEMO_GIST_WORD = "almanac"


def _make_old_episode(role: str, text: str, age_days: int, session_id: str = _DEMO_SESSION) -> dict:
    """A real episodic through the live factory, made BATCHLESS (always-live, never a crashed-pass orphan) and
    back-dated by `age_days` so the demo can age it without sleeping."""
    from memory import consolidate  # lazy
    rec = consolidate._make_episodic(session_id, {"role": role, "text": text}, "demo-batch")
    rec.pop(records.BATCH_KEY, None)
    rec["ts"] = int(time.time()) - age_days * _DEMO_DAY
    return rec


def _recall_count(word: str) -> int:
    from memory import index  # lazy
    return len(index.query(word).records)


def _rebuild() -> None:
    from memory import index  # lazy
    index.rebuild()


def _live_ids() -> set:
    """The ids RECALL would surface (the ground truth in the cabinet, independent of the fast search index)."""
    return {r.get(records.RECORD_ID_KEY) for r in forget.live_records() if isinstance(r, dict)}


def _read_text(record_id: str) -> "str | None":
    """Read one record's text straight out of the cabinet by its id — the recoverability proof."""
    for r in ledger.iter_records(path=ledger.ledger_path()):
        if isinstance(r, dict) and r.get(records.RECORD_ID_KEY) == record_id:
            return r.get("text")
    return None


def _on_file(record_id: str) -> int:
    return sum(1 for r in ledger.iter_records(path=ledger.ledger_path())
               if isinstance(r, dict) and r.get(records.RECORD_ID_KEY) == record_id)


def _superseded_by(record_id: str) -> "str | None":
    """The summary id a filed-away original points at — read from its carried `superseded_by` field (post-tidy)
    or its live `superseded` marker (pre-tidy). The original→summary link, read back."""
    for r in ledger.iter_records(path=ledger.ledger_path()):
        if not isinstance(r, dict):
            continue
        if r.get(records.RECORD_ID_KEY) == record_id and r.get(records.SUPERSEDED_BY_KEY):
            return r.get(records.SUPERSEDED_BY_KEY)
        if r.get("kind") == records.SUPERSEDED_KIND and r.get(records.TARGET_KEY) == record_id:
            return r.get(records.SUPERSEDED_BY_KEY)
    return None


def _gist_id_for(session_id: str) -> "str | None":
    for r in ledger.iter_records(path=ledger.ledger_path()):
        if isinstance(r, dict) and r.get("kind") == records.GIST_KIND and r.get("session_id") == session_id:
            return r.get(records.RECORD_ID_KEY)
    return None


def _cabinet_whole() -> bool:
    report = ledger.read(path=ledger.ledger_path())
    return os.path.exists(ledger.ledger_path()) and not report.torn_trailing


def _snippet(text, width: int = 66) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _demo() -> int:
    import tempfile

    print("=" * 88)
    print("MEMORY — rolling old notes into one summary, safely, without losing or erasing the originals (practice)")
    print("=" * 88)
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["ENGINE_MEMORY_DIR"] = tmp          # the throwaway cabinet
        try:
            ok = _demo_body()
        finally:
            os.environ.pop("ENGINE_MEMORY_DIR", None)

    print("\n" + "-" * 88)
    print("The one thing to know: NOTHING is ever erased here. Filing an original away is REVERSIBLE — the")
    print("original stays in the cabinet, fully recoverable; it just stops turning up in a search. Permanently")
    print("erasing a note is a SEPARATE step you approve later by merging a pull request (and applying the")
    print("`guardrail-ack` safety label) — never this.")
    print("What this just proved: the engine can roll a cluster of OLD, unused notes from one session into a")
    print("single summary and file the originals away — they no longer turn up in a search (only the summary")
    print("does), but they are still saved. A power-cut in the middle never loses a note and never files an")
    print("original away without its summary in place; a later tidy keeps the original→summary link and erases")
    print("nothing. On your real data nothing rolls up yet: the engine doesn't run this pass on its own (that")
    print("live trigger is a later step), so today this only runs in this practice demo on the old notes it")
    print("invents. That was a PRACTICE cabinet, thrown away when the demo ended; like all memory, private,")
    print("local, and deletable. Vary it: edit the notes, the age, and the crash-point near the top and re-run.")
    return 0 if ok else 1


def _demo_body() -> bool:
    # --- PART 1 ------------------------------------------------------------------------------------------
    print("\nPART 1 — old, related notes roll into one summary; the originals are filed away, not lost")
    print("-" * 88)
    old_ids = []
    for role, text, _word in _DEMO_OLD_NOTES:
        rec = _make_old_episode(role, text, _DEMO_AGE_DAYS)
        old_ids.append(rec[records.RECORD_ID_KEY])
        ledger.append(rec)
    young = _make_old_episode(_DEMO_YOUNG_NOTE[0], _DEMO_YOUNG_NOTE[1], age_days=0)
    ledger.append(young)
    _rebuild()
    candidates = detect_rollup_candidates().get(_DEMO_SESSION, [])
    print(f"  filed {len(_DEMO_OLD_NOTES)} old notes + 1 fresh note for '{_DEMO_SESSION}'.")
    print(f"  old notes the engine flags as ready to roll up: {len(candidates)}"
          f"  (the fresh note is not old enough — left alone)")
    if len(candidates) < _MIN_GROUP:
        print("  => !!! no candidates — the notes are too fresh; raise _DEMO_AGE_DAYS near the top and re-run.")
        return False
    print("  before rolling up — each old note is findable by its own distinctive word:")
    before = {word: _recall_count(word) for _role, _t, word in _DEMO_OLD_NOTES}
    for _r, _t, word in _DEMO_OLD_NOTES:
        print(f'    search "{word}" -> found {before[word]}')
    # The AI writes ONE summary of these notes (here a hand-written SAMPLE; the filing around it is real).
    report = store_gist(_DEMO_SESSION, [dict(_DEMO_GIST, **{records.SOURCE_IDS_KEY: old_ids})])
    print(f"  ...the AI's one-line summary is filed. status: {report['status']}")
    after = {word: _recall_count(word) for _role, _t, word in _DEMO_OLD_NOTES}
    gist_found = _recall_count(_DEMO_GIST_WORD)
    young_found = _recall_count(_DEMO_YOUNG_WORD)
    kept_text = _read_text(old_ids[0])
    print(f'  after rolling up — search the summary\'s word "{_DEMO_GIST_WORD}" -> found {gist_found}')
    for _r, _t, word in _DEMO_OLD_NOTES:
        print(f'    search the original\'s word "{word}" -> found {after[word]}   (filed away — no longer in search)')
    print(f"  the '{_DEMO_KEEP_PHRASE}' original is STILL on file (read straight from the cabinet by its id):")
    print(f"    {_snippet(kept_text)}")
    print(f'  the fresh note is untouched — search "{_DEMO_YOUNG_WORD}" -> found {young_found}')
    part1 = (all(before[w] == 1 for _r, _t, w in _DEMO_OLD_NOTES)
             and all(after[w] == 0 for _r, _t, w in _DEMO_OLD_NOTES)
             and gist_found == 1 and young_found == 1
             and kept_text is not None and _DEMO_KEEP_PHRASE in kept_text)
    print(f"  => {'the originals rolled into one summary; they are filed away but still saved.' if part1 else '!!! a note was lost, still searchable, or the summary is missing'}")

    # --- PART 2 ------------------------------------------------------------------------------------------
    print("\nPART 2 — a power-cut in the MIDDLE never loses a note or files one away without its summary")
    print("-" * 88)

    print("\n  (a) the power cuts out AFTER the summary is written but BEFORE the originals are filed away")
    sess_a = "session-orchard-a"
    raws_a = []
    for i in range(_MIN_GROUP):
        rec = _make_old_episode("decision", f"Decided orchard row {i} gets a heritage apple cultivar number {i}.",
                                 _DEMO_AGE_DAYS, session_id=sess_a)
        raws_a.append(rec[records.RECORD_ID_KEY])
        ledger.append(rec)
    try:
        store_gist(sess_a, [{"role": "decision", "text": "Orchard rows recap: each row an apple cultivar.",
                             records.SOURCE_IDS_KEY: raws_a}], _crash_after="markers")
    except _InjectedCrash:
        pass
    live_a = _live_ids()
    raws_live_a = sum(1 for rid in raws_a if rid in live_a)
    half_summary_hidden = _gist_id_for(sess_a) not in live_a
    filed_without_summary = sum(1 for rid in raws_a if rid not in live_a)
    print(f"    the filing cabinet is here and complete (no half-written entry): {'yes' if _cabinet_whole() else 'NO'}")
    print(f"    every original still findable in the cabinet: {raws_live_a} of {len(raws_a)}")
    print(f"    the half-finished summary (one the engine never completed) is kept out of search: "
          f"{'yes' if half_summary_hidden else 'NO'}")
    print(f"    originals filed away WITHOUT their summary in place: {filed_without_summary}")
    part2a = (_cabinet_whole() and raws_live_a == len(raws_a) and half_summary_hidden
              and filed_without_summary == 0)
    print(f"    => {'nothing filed away yet — the originals are all still findable, the half-summary is set aside.' if part2a else '!!! the before-finish crash hid or lost something'}")

    print("\n  (b) the power comes back, finishes filing the originals away, then cuts out BEFORE the search refreshes")
    sess_b = "session-orchard-b"
    raws_b = []
    for i in range(_MIN_GROUP):
        rec = _make_old_episode("lesson", f"Lesson orchard plot {i}: the clay subsoil needs gypsum batch {i}.",
                                 _DEMO_AGE_DAYS, session_id=sess_b)
        raws_b.append(rec[records.RECORD_ID_KEY])
        ledger.append(rec)
    try:
        store_gist(sess_b, [{"role": "lesson", "text": "Orchard soil report: the clay plots need gypsum.",
                             records.SOURCE_IDS_KEY: raws_b}], _crash_after="close")
    except _InjectedCrash:
        pass
    live_b = _live_ids()
    gist_b = _gist_id_for(sess_b)
    summary_filed = gist_b in live_b
    originals_filed_away = all(rid not in live_b for rid in raws_b)
    originals_on_file = sum(_on_file(rid) for rid in raws_b)
    link_ok = sum(1 for rid in raws_b if _superseded_by(rid) == gist_b)
    print(f"    the filing cabinet is here and complete: {'yes' if _cabinet_whole() else 'NO'}")
    print(f"    the cabinet itself is already correct — the summary is filed in: {'yes' if summary_filed else 'NO'};"
          f" the originals are filed away: {'yes' if originals_filed_away else 'NO'}")
    print(f"    every original still on file (recoverable): {originals_on_file} of {len(raws_b)}")
    print(f"    each filed-away original still points at its summary: {link_ok} of {len(raws_b)}")
    _rebuild()                                          # the search refresh that the power-cut interrupted
    refreshed_summary = _recall_count("report")         # orchard-b's own summary word
    print(f'    after the search refreshes, a search for the summary agrees: "report" -> found {refreshed_summary}')
    part2b = (_cabinet_whole() and summary_filed and originals_filed_away
              and originals_on_file == len(raws_b) and link_ok == len(raws_b) and refreshed_summary >= 1)
    print(f"    => {'the cabinet was already correct after the cut; the search just had to catch up.' if part2b else '!!! the after-filing crash lost something or broke the link'}")

    # --- PART 3 ------------------------------------------------------------------------------------------
    print("\nPART 3 — a later tidy keeps the original→summary link and erases nothing")
    print("-" * 88)
    from memory import compact  # lazy
    gist_garden = _gist_id_for(_DEMO_SESSION)
    link_before = sum(1 for rid in old_ids if _superseded_by(rid) == gist_garden)
    compact.compact()                                   # the rebuild-and-swap that folds the link + prunes bookkeeping
    summary_found = _recall_count(_DEMO_GIST_WORD)
    originals_filed = all(_recall_count(w) == 0 for _r, _t, w in _DEMO_OLD_NOTES)
    on_file = sum(_on_file(rid) for rid in old_ids)
    link_after = sum(1 for rid in old_ids if _superseded_by(rid) == gist_garden)
    kept_text = _read_text(old_ids[0])
    print(f"  original→summary links before the tidy: {link_before} of {len(old_ids)}")
    print(f"  ...tidied (the bookkeeping is folded away).")
    print(f'  the summary is still here and findable: "{_DEMO_GIST_WORD}" -> found {summary_found}')
    print(f"  every original still on file after the rewrite: {on_file} of {len(old_ids)}")
    print(f"  the '{_DEMO_KEEP_PHRASE}' original still reads back: "
          f"{'yes' if kept_text and _DEMO_KEEP_PHRASE in kept_text else 'NO'}")
    print(f"  the original→summary link survived the rewrite: {link_after} of {len(old_ids)}")
    print(f"  the originals are still filed away (out of search): {'yes' if originals_filed else 'NO'}")
    part3 = (link_before == len(old_ids) and summary_found == 1 and originals_filed
             and on_file == len(old_ids) and link_after == len(old_ids)
             and kept_text is not None and _DEMO_KEEP_PHRASE in kept_text)
    print(f"  => {'the tidy kept every original, kept the link, and erased nothing.' if part3 else '!!! the tidy lost a note, broke the link, or re-surfaced an original'}")

    return part1 and part2a and part2b and part3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
