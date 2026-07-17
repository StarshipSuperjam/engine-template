"""rollup.py — gist roll-up: AI-judged second-order consolidation of old episodes (memory substrate).

Active forgetting's first move: a perpetual project cannot only accumulate. A deferred,
AI-judged maintenance pass **consolidates old, related, low-frecency EPISODIC summaries into a compact GIST and
logically retires the raw episodes** — the SECOND-order consolidation (episode→gist), exactly parallel to
the first-order (delta→episode). "Related" is pre-grouped for the AI by a cross-session shared-topic-tag
cluster or, failing that, the coarse same-session group (#235); a cross-session gist carries a `tag:`/`sim:`
cluster key as its `session_id` (its real-session provenance lives in `source_ids`). This module is **Layer 1**: *reversible, mechanical,
memory-autonomous* tidying that needs no human gate because **nothing is lost** — a retired raw is excluded from
recall but stays resident + fully recoverable in the one ledger (physical erasure is Layer-2, audit-gated).

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
and nothing is hidden without its gist. The closing kind is DISTINCT from the first-order `consolidated` marker so a
roll-up never spuriously marks a session consolidated. APPENDS ONLY — never deletes or rewrites (the Layer-1
erasure-free invariant; the swap that prunes the folded markers lives in `compact.py`, which folds a
closed-batch supersession into the raw's carried `superseded_by` field).

**The live caller.** A consolidation-shaped `SessionStart` background sweep hands the in-context AI the cold
session-groups to judge — a hook cannot think, so it cannot ride a non-AI hook. It is FOLDED INTO memory's one
`SessionStart` behavior (`consolidate._session_start_handler`, the single keyed memory hook the design's wired-hook
list names — `Stop`, `PreCompact`, `SessionStart`): that handler injects ONE combined background directive — the
consolidation backlog and, via `rollup_directive`, the roll-up backlog — so the operator's first turn is never
split by two competing asks. On a young ledger there are still no old, low-frecency episodes, so the sweep finds no
candidates and the directive stays silent. The selection floors (COLD tier; grouping — a cross-session
shared-topic-tag cluster, then the coarse per-session group; ≥3 per group; episodics only) are build-spec leaves
recorded with the maintainer.

Leaf discipline: the detect/store functions RETURN a report and render no operator-facing prose
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

import hooks  # noqa: E402 — .engine/tools/hooks.py: the roll-up sweep rides its fail-open SessionStart harness
from memory import forget, ledger, records, score  # noqa: E402

# --- Build-spec leaves (the roll-up selection floors) -----------------------------------------------------
# A candidate is a raw EPISODIC (not a gist — v1 keeps the link single-hop raw→gist; recursive meta-gist is a
# deferred concern), not already superseded, that scores into the COLD tier (≈16–30 days untouched — "old +
# low-frecency", not yet ARCHIVED, which the scorer already index-excludes). The eligible pool is then pre-grouped by a
# "related" signal for the AI to judge WITHIN each group, in a fixed PRECEDENCE so each candidate lands in exactly
# ONE group (disjoint source_ids per pass): (1) a cross-session SHARED-TOPIC-TAG cluster (`tag:<tag>`, #235 — the
# richer relatedness signal), then (2) the coarse per-session group (the original floor, the fallback for untagged
# notes). A group needs >= _MIN_GROUP raws (a "gist" of one raw is a rename); a cross-session cluster must
# additionally span >= _TAG_MIN_SESSIONS distinct real sessions (else it is just a per-session group in disguise).
# All recorded with the maintainer. (A lexical-similarity cluster — `sim:<id8>` — slots between (1) and (2) in
# a later pass.)
_MIN_GROUP = 3
_TAG_MIN_SESSIONS = 2     # a cross-session tag cluster must span >= this many real sessions to count as cross-session
_MAX_DIRECTIVE_IDS = 8    # cap the directive's id enumeration (mirrors consolidate._MAX_DIRECTIVE_IDS)

# Tags that are STRUCTURAL, not topics — never a clustering signal (they mark a record's kind/provenance, and
# every episodic carries DEFAULT_EPISODIC_TAG, so clustering on any of them would fuse unrelated notes). #235.
_NON_TOPIC_TAGS = frozenset({
    records.DEFAULT_EPISODIC_TAG, records.GIST_TAG, records.MARKER_TAG, records.INJECTED_TAG,
    records.REINFORCEMENT_TAG, records.SUPERSEDED_KIND, records.ROLLUP_KIND, records.ERASURE_TAG,
})


class _InjectedCrash(Exception):
    """A TEST/DEMO-only fault, raised at a chosen point in `store_gist` to model a power-cut. Production callers
    never pass `_crash_after`, so this never fires in real use (a test pins the default off)."""


# --- Detecting candidates (the selection leaf) ------------------------------------------------------------

def _eligible_cold_episodics(*, cwd=None, now: "int | None" = None) -> list:
    """The flat pool of raw EPISODIC records eligible for roll-up: COLD-tier (`score.tier`), not already
    superseded, not a crashed-pass orphan, carrying a real `session_id`. Computed once (one ledger scan) and
    shared by the grouping passes below."""
    src = ledger.ledger_path(cwd)
    now = int(time.time()) if now is None else now
    access_index = forget._access_index(src)
    closed = forget._closed_batches(src)
    closed_rollup = forget._closed_rollup_batches(src)
    superseded = set(forget._superseded_by_map(src, closed_rollup))
    pool: list = []
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
        pool.append(record)
    return pool


def _distinct_sessions(recs) -> int:
    return len({r.get("session_id") for r in recs})


def _tag_clusters(candidates: list) -> list:
    """Cross-session shared-topic-tag buckets as `[("tag:<tag>", [record, ...]), ...]`, in sorted-tag order — so a
    note carrying two clustering tags is claimed deterministically by the lexicographically-smallest one. Only
    TOPIC tags cluster; the structural tags (`_NON_TOPIC_TAGS`, incl. the `episodic` tag every note carries) are
    skipped, else every episodic would fuse into one bucket. The floors (_MIN_GROUP, _TAG_MIN_SESSIONS) are
    applied by the caller after overlap-claiming."""
    buckets: dict = {}
    for r in candidates:
        for t in r.get("tags") or []:
            if not isinstance(t, str):
                continue
            t = t.strip()                                # defensive: a padded tag never forms a `tag: ` bucket
            if t and t not in _NON_TOPIC_TAGS:
                buckets.setdefault(t, []).append(r)
    return [(records.TAG_SESSION_PREFIX + t, recs) for t, recs in sorted(buckets.items())]


def detect_rollup_candidates(*, cwd=None, now: "int | None" = None) -> dict:
    """Pre-group the eligible cold EPISODIC records for roll-up, in a fixed PRECEDENCE so each candidate lands in
    exactly ONE group (disjoint `source_ids` per pass): (1) cross-session shared-topic-tag clusters keyed
    `tag:<tag>` (#235 — each spanning >= _TAG_MIN_SESSIONS real sessions), then (2) the coarse per-session group
    (the fallback for whatever a richer signal did not claim). A DETECT leaf — returns `{group_key: [record, ...]}`
    and renders no operator prose; a key is a real session id (uuid hex) or a `tag:` cluster sentinel, which cannot
    collide. Every group needs >= _MIN_GROUP raws. The mechanism selects deterministically; the in-context AI
    judges the roll-up (the gist text + which raws cohere) in-session."""
    candidates = _eligible_cold_episodics(cwd=cwd, now=now)
    claimed: set = set()
    groups: dict = {}
    # (1) cross-session shared-tag clusters — the richer signal, highest precedence.
    for key, recs in _tag_clusters(candidates):
        members = [r for r in recs if r.get(records.RECORD_ID_KEY) not in claimed]
        if len(members) >= _MIN_GROUP and _distinct_sessions(members) >= _TAG_MIN_SESSIONS:
            groups[key] = members
            claimed.update(r.get(records.RECORD_ID_KEY) for r in members)
    # (2) per-session groups — the coarse fallback for whatever a richer signal did not claim (legacy behavior).
    by_session: dict = {}
    for r in candidates:
        if r.get(records.RECORD_ID_KEY) in claimed:
            continue
        by_session.setdefault(r.get("session_id"), []).append(r)
    for sid, recs in by_session.items():
        if len(recs) >= _MIN_GROUP:
            groups[sid] = recs
    return groups


def read_candidates(session_id: str, *, cwd=None, now: "int | None" = None) -> list:
    """The roll-up candidate episodes for one GROUP — a real session id or a `tag:<tag>` cross-session cluster key
    (#235) — as `[{id, role, text}, ...]`, what the in-context AI reads to write a gist and name its `source_ids`.
    A pure read (no lock); empty if the key is not a candidate group."""
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
        records.RECORD_ID_KEY: records.new_record_id(),     # the stable, content-free record id
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


# --- The live sweep (memory's own SessionStart behavior; wired via consolidate's handler) -----------------

def rollup_directive(groups: dict) -> str:
    """The BACKGROUND directive the SessionStart sweep injects when sessions have a cluster of old, unused
    episodes ready to roll up. Like the consolidation directive it stays SUBORDINATE to the operator's request
    (memory's operation, not orientation): never a first-turn hijack, and its mechanics run OFF the
    operator's main transcript — the main loop SPAWNS A SUBAGENT to do the read/store and relays nothing about it
    when the subagent returns (#280; roll-up has no stalled-backlog alarm — it is the lower-priority "can wait"
    path). The detect leaf renders nothing; this composes the prose. DISTINCT from
    `consolidate._consolidation_directive` in two ways the subagent must honor: (1) it folds OLD episodic
    summaries, not raw turn-notes; (2) each gist must name its `source_ids` — the exact note ids it rolls up —
    which `rollup.py read` returns so the subagent can. Reuses the closed role vocabulary so the prose can never
    drift from `_validate`'s accepted set."""
    from memory import consolidate  # lazy: reuse the closed role vocabulary (3b) without an import cycle
    ids = sorted(groups)
    n = len(ids)
    shown = ids[:_MAX_DIRECTIVE_IDS]
    listed = ", ".join(shown) + (f", and {n - len(shown)} more" if n > len(shown) else "")
    count = "1 group of" if n == 1 else f"{n} groups of"
    each = "" if n == 1 else "each "
    has_cross = any(records.is_cross_session_sentinel(k) for k in ids)
    has_session = any(not records.is_cross_session_sentinel(k) for k in ids)
    if has_cross and has_session:
        cross_note = (" A group id beginning `tag:` is a CROSS-SESSION cluster of notes that share a topic tag "
                      "(not a single session); the others are single sessions — roll each group into one summary "
                      "either way.")
    elif has_cross:
        cross_note = (" A group id beginning `tag:` is a CROSS-SESSION cluster of notes that share a topic tag "
                      "(not a single session) — roll each cluster into one summary.")
    else:
        cross_note = ""
    return (
        "Background memory upkeep — older notes ready to summarize. NOT urgent, and NOT to be done before the "
        f"operator's actual request. There {'is' if n == 1 else 'are'} {count} old, unused notes ({listed}) that "
        f"can {each}be rolled into one short summary.{cross_note} At a natural pause, AFTER you have served the "
        "operator's current request, SPAWN A SUBAGENT (the Task tool) to do the read/store so its tool calls and "
        "output stay off the operator's main transcript — only a brief task card may show; when it returns, relay "
        f"NOTHING to the operator about it. Hand the subagent these group ids: {listed}, and this instruction: for "
        "each id, run `.engine/.venv/bin/python .engine/tools/memory/rollup.py read <group>` to read that group's "
        "old notes (each carries its id), decide which genuinely cohere, write ONE compact summary of the cluster, "
        "choose ONE label from "
        f"{{{', '.join(consolidate.ROLE_VOCABULARY)}}}, and store it with `… rollup.py store <group>` — a JSON "
        "array of {\"role\": …, \"text\": …, \"source_ids\": [the ids of the exact notes this summary replaces]} on "
        "stdin. Name only the ids actually read and folded in; the originals are filed away, never erased. "
        "The operator's request always comes first; this can wait turns or whole sessions. Do not announce it "
        "unless asked."
    )


def _session_start_handler(payload) -> dict:
    """Roll-up's SessionStart sweep IN ISOLATION: inject the roll-up directive only when cold candidate groups
    exist, else proceed silently. The WIRED sweep is `consolidate._session_start_handler`, which calls this
    module's `detect_rollup_candidates` + `rollup_directive` alongside the consolidation backlog in ONE injection;
    this handler is the manual/test twin (the unwired `session-start` verb) so the roll-up half can be exercised on
    its own. run_hook fail-opens on any fault. The live session needs no exclusion: roll-up candidacy is COLD-tier
    (≈16–30 days untouched), which a live session's fresh episodes can never be."""
    groups = detect_rollup_candidates()
    if not groups:
        return hooks.proceed()
    return hooks.inject(rollup_directive(groups))


# --- CLI (the verbs the in-context AI / operator call) ----------------------------------------------------

def main(argv: list) -> int:
    cmd = argv[0] if argv else "demo"
    if cmd == "session-start":
        return hooks.run_hook("SessionStart", _session_start_handler)
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
    if cmd == "demo-sweep":
        return _demo_sweep()
    print(f"usage: rollup.py [session-start|detect|read <sid>|store <sid>|demo|demo-sweep]\n"
          f"unknown command {cmd!r}", file=sys.stderr)
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


def _make_old_episode(role: str, text: str, age_days: int, session_id: str = _DEMO_SESSION,
                      tags: "list | None" = None) -> dict:
    """A real episodic through the live factory, made BATCHLESS (always-live, never a crashed-pass orphan) and
    back-dated by `age_days` so the demo can age it without sleeping. Optional topic `tags` let the demo plant a
    cross-session shared-tag cluster (#235)."""
    from memory import consolidate  # lazy
    payload = {"role": role, "text": text}
    if tags is not None:
        payload["tags"] = tags
    rec = consolidate._make_episodic(session_id, payload, "demo-batch")
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
    print("nothing. The engine now reminds the AI to do this on its own — in a spawned subagent so the")
    print("read/store mechanics stay off your chat (only a brief task card may show; whether the spawn keeps")
    print("them off your transcript is a property of the live runtime, seen on a real session), AFTER your")
    print("request, only when a session has a cluster of old, unused notes (the `demo-sweep` walkthrough shows")
    print("that reminder, and shows it stay silent when there's nothing old enough). That was a PRACTICE")
    print("cabinet, thrown away when the demo ended; like all memory, private, local, and deletable. Vary it:")
    print("edit the notes, the age, and the crash-point near the top and re-run.")
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

    # --- PART 4 ------------------------------------------------------------------------------------------
    print("\nPART 4 — notes from DIFFERENT sessions that share a topic tag roll up together (#235)")
    print("-" * 88)
    tag_sessions = ("session-mon", "session-wed", "session-fri")
    tag_word = "harvest"
    cross_ids = []
    for i, sess in enumerate(tag_sessions):
        rec = _make_old_episode("observation",
                                 f"Noted the {tag_word} yield looked strong in plot {i} — grapes{i}.",
                                 _DEMO_AGE_DAYS, session_id=sess, tags=[tag_word])
        cross_ids.append(rec[records.RECORD_ID_KEY])
        ledger.append(rec)
    _rebuild()
    cluster_key = records.TAG_SESSION_PREFIX + tag_word
    cluster = detect_rollup_candidates().get(cluster_key, [])
    spanned = len({r.get("session_id") for r in cluster})
    print(f"  filed 1 old '{tag_word}'-tagged note in each of {len(tag_sessions)} different sessions "
          f"({', '.join(tag_sessions)}).")
    print(f"  no single session has {_MIN_GROUP} such notes — so grouping by session alone would roll up NONE.")
    print(f"  instead the engine groups them by their shared topic into one cluster '{cluster_key}': "
          f"{len(cluster)} notes spanning {spanned} sessions.")
    report4 = store_gist(cluster_key,
                         [{"role": "observation",
                           "text": f"Across the week the {tag_word} came in strong everywhere.",
                           records.SOURCE_IDS_KEY: cross_ids}])
    cross_gist = _gist_id_for(cluster_key)
    live4 = _live_ids()
    filed_away = sum(1 for rid in cross_ids if rid not in live4)
    readable = sum(1 for rid in cross_ids if _read_text(rid) is not None)
    print(f"  ...the AI files ONE summary for the whole cluster. status: {report4['status']}")
    print(f"  the cross-session originals are now filed away (still saved): {filed_away} of {len(cross_ids)}")
    print(f"  each original still reads back from the cabinet by its id: {readable} of {len(cross_ids)}")
    part4 = (len(cluster) == len(tag_sessions) and spanned == len(tag_sessions)
             and cross_gist is not None and cross_gist in live4
             and filed_away == len(cross_ids) and readable == len(cross_ids))
    print(f"  => {'notes from three different sessions, sharing a topic, rolled into one summary — all still saved.' if part4 else '!!! the cross-session cluster did not form or an original was lost'}")

    return part1 and part2a and part2b and part3 and part4


# --- demo-sweep: the live SessionStart reminder (the caller), and its silence ------------------------------
# A SEPARATE walkthrough from `demo` (which proves the roll-up MECHANISM). This proves the live CALLER: at the
# start of a session, when an earlier session has a cluster of old unused notes, the engine leaves the AI the
# exact background reminder printed below (after the operator's request, never before) — and stays SILENT when
# there is nothing old enough. Each scenario runs in its OWN throwaway cabinet so "speaks" and "silent" can't
# bleed together. Vary the note age/count in the `demo` section above and watch the reminder appear/vanish:
#     uv run --directory .engine --frozen -- python tools/memory/rollup.py demo-sweep
_DEMO_SWEEP_SESSION = "session-orchard-old"


def _wrap_sweep(text: str, width: int) -> list:
    """A tiny word-wrap so the printed reminder is legible (stdlib textwrap, kept local to the demo)."""
    import textwrap
    return textwrap.wrap(text, width=width) if text else ["(silent — nothing to roll up)"]


def _sweep_part_speaks() -> bool:
    import tempfile
    print("\nPART 1 — an earlier session has a cluster of OLD notes: the engine leaves the AI a background reminder")
    print("-" * 88)
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["ENGINE_MEMORY_DIR"] = tmp          # the throwaway cabinet for THIS scenario
        try:
            for role, text, _word in _DEMO_OLD_NOTES:
                ledger.append(_make_old_episode(role, text, _DEMO_AGE_DAYS, session_id=_DEMO_SWEEP_SESSION))
            _rebuild()
            groups = detect_rollup_candidates()
            decision = _session_start_handler({"session_id": "the-session-you-are-in-now"})  # the REAL sweep handler
            injected = decision.get("context", "") if decision.get("action") == "inject" else ""
        finally:
            os.environ.pop("ENGINE_MEMORY_DIR", None)
    print(f"  old, unused notes filed for '{_DEMO_SWEEP_SESSION}': {len(_DEMO_OLD_NOTES)}")
    print(f"  sessions the engine flags as ready to roll up: {len(groups)}")
    print(f"  did the engine leave a background reminder? {'yes' if injected else 'NO'}")
    print("\n  The exact background reminder the engine would hand the AI at the next session start:\n")
    for line in _wrap_sweep(injected, 84):
        print(f"    | {line}")
    names_session = _DEMO_SWEEP_SESSION in injected
    names_verbs = ("rollup.py read" in injected) and ("rollup.py store" in injected)
    names_sources = "source_ids" in injected
    subordinate = "before the operator" in injected.lower() or "after you have served" in injected.lower()
    print()
    print(f"  it names the session to roll up: {'yes' if names_session else 'NO'}")
    print(f"  it tells the AI exactly how (read + store): {'yes' if names_verbs else 'NO'}")
    print(f"  it requires naming which notes are folded in (source_ids): {'yes' if names_sources else 'NO'}")
    print(f"  it keeps the work AFTER your request, never before: {'yes' if subordinate else 'NO'}")
    ok = bool(injected) and len(groups) == 1 and names_session and names_verbs and names_sources and subordinate
    print(f"  => {'the engine spoke, named the session, and told the AI how — after your request.' if ok else '!!! the reminder was missing, malformed, or not subordinate to your request'}")
    return ok


def _sweep_part_silent() -> bool:
    import tempfile
    print("\nPART 2 — nothing old enough: the engine stays SILENT and adds nothing")
    print("-" * 88)
    with tempfile.TemporaryDirectory() as tmp:        # (a) an empty cabinet — its own scenario
        os.environ["ENGINE_MEMORY_DIR"] = tmp
        try:
            silent_empty = _session_start_handler({"session_id": "x"}).get("action") == "proceed"
        finally:
            os.environ.pop("ENGINE_MEMORY_DIR", None)
    print(f"  (a) an empty cabinet -> the engine stays silent: {'yes' if silent_empty else 'NO'}")
    with tempfile.TemporaryDirectory() as tmp:        # (b) a cabinet whose notes are too FRESH to roll up
        os.environ["ENGINE_MEMORY_DIR"] = tmp
        try:
            for role, text, _word in _DEMO_OLD_NOTES:
                ledger.append(_make_old_episode(role, text, age_days=0, session_id="session-fresh"))
            _rebuild()
            fresh_groups = detect_rollup_candidates()
            silent_fresh = _session_start_handler({"session_id": "x"}).get("action") == "proceed"
        finally:
            os.environ.pop("ENGINE_MEMORY_DIR", None)
    print(f"  (b) {len(_DEMO_OLD_NOTES)} fresh notes (too new) -> flagged: {len(fresh_groups)}; "
          f"the engine stays silent: {'yes' if silent_fresh else 'NO'}")
    ok = silent_empty and len(fresh_groups) == 0 and silent_fresh
    print(f"  => {'with nothing old enough, the engine says nothing — it will not nag you.' if ok else '!!! the engine spoke when it should have stayed silent'}")
    return ok


def _demo_sweep() -> int:
    print("=" * 88)
    print("MEMORY — the background reminder to roll up old notes: when it speaks, and when it stays silent (practice)")
    print("=" * 88)
    ok1 = _sweep_part_speaks()
    ok2 = _sweep_part_silent()

    print("\n" + "-" * 88)
    print("What this just proved: at the start of a session, IF an earlier session has a cluster of old, unused")
    print("notes, the engine quietly leaves the AI the background reminder shown above — to roll them into one")
    print("summary, AFTER your request and never before. With nothing old enough it stays completely SILENT and")
    print("adds nothing to your session. This is a DISTINCT, second kind of reminder from the existing one that")
    print("tidies raw notes into summaries — this one rolls OLD summaries into a single shorter one. It only")
    print("points; the AI reads the notes and writes the summary, and you can delete any summary it makes. Once")
    print("you merge this PR this reminder is LIVE on your real sessions — it runs on its own in the background,")
    print("with no further approval each time; that is what merging turns on. On your real, fresh data it will be")
    print("silent (your notes are too new). Practice cabinet, thrown away. Vary it: raise/lower the note age and")
    print("count in the `demo` section above and re-run to watch the reminder appear, then go silent.")
    return 0 if (ok1 and ok2) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
