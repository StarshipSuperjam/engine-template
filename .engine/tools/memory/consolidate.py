"""consolidate.py — AI-judged episodic consolidation: the reflection half of the memory substrate.

The content half (capture.py) saves every completed turn's RAW notes to the ledger. This module is the
REFLECTION half — turning those raw turn-deltas into clean, role-typed EPISODIC summaries (the "tidy-up"):

  - **The tidy-up is AI-judged, and a hook cannot think.** A compact episodic summary ("explored X; rejected
    Y because Z; the operator wants W") is written by the in-context AI, which types it with ONE closed ROLE
    (decision / rationale-pushback / lesson / dead-end / preference / intent / observation). This was
    validated against the SHIPPED Claude Code runtime: a hook steers the model only by injecting context a
    *later* turn reads, or by blocking — it can NEVER make the model generate. So consolidation cannot run
    inside a `PreCompact`/close hook; the model is not in that loop.

  - **The mechanism: a `SessionStart` sweep, run in a subagent.** Memory's own `SessionStart` hook DETECTS
    earlier sessions whose raw notes were never tidied (have turn-deltas, no consolidation marker, not the live
    session) and INJECTS a directive — the in-context AI, at the first natural pause AFTER the operator's request
    (so never a first-turn hijack), SPAWNS A SUBAGENT that reads each session's raw notes (`read`), writes a
    short labelled summary of each thread, and stores it (`store`). The mechanics run OFF the operator's main
    transcript: the subagent's tool calls and raw JSON stay in its own context (only the runtime's brief task
    card may show), and the main loop relays NOTHING about the tidy-up when the subagent returns — so a routine
    chore no longer floods the operator's chat (#280). The ONE exception: if the backlog has grown past
    `_BACKLOG_ALARM_THRESHOLD` (a sign the tidy has stalled), the MAIN loop itself surfaces ONE plain line (a
    COUNT, never the id codes) so a silent failure can't hide. The directive stays prompt — done THIS session,
    not deferred forever (the passivity that left 21 sessions untidied is gone) — and always subordinate to the
    operator's request.
    This unifies the "normal" and "abandoned-session" consolidation into ONE sweep: the locked design's
    abandoned-session predicate subsumes the normal path — the previous session is "no longer live with no
    marker" shortly after it ends. The lease heartbeat (#396) is what tells "no longer live" from a still-
    running concurrent session: a session is swept once its lease has been silent for a small N (`_LEASE_STALE_N`
    session-starts), so the previous session is tidied within a few starts, not the very next — a small,
    deliberate cushion so a briefly-idle live session is not tidied mid-run (the store-time re-check is the real
    guard; N only tunes promptness). The sweep ALSO carries the roll-up backlog in the
    same single injection. `PreCompact` cannot reach the AI, so it carries no consolidation — but it now rides
    the deterministic ledger-compaction trigger (`compact.maybe_compact`).

  - **Leaf discipline.** `detect_unconsolidated` RETURNS session-id signals and renders no
    prose; the hook handler — memory's own behavior, exactly as boot's handler renders its briefing — composes
    the directive. The store path is idempotent + race-safe under the SHARED capture lock, favors a duplicate
    over a loss on a mid-write crash, and never gates a turn.

The closed role vocabulary attaches to EPISODIC records only (raw turn-deltas are unlabelled). The role, the
tags, and the provenance fields stay OUT of the search body (`index._NON_BODY_KEYS`) so the human narrative
text is what recall ranks — searching a label like "decision" must never drag in every record that shares it.
stdlib-only; runs on the venv python alongside boot.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid

# Make the `memory` package + the sibling `hooks` tool importable whether we are imported as
# `memory.consolidate` or run as the wired hook script (the same _PARENT insert capture.py/index.py use).
_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

import hooks  # noqa: E402 — .engine/tools/hooks.py: memory's SessionStart/PreCompact ride its fail-open harness
from memory import capture, index, ledger  # noqa: E402
# The record kinds, tags, and the per-pass `batch` key live in `records` — the shared, cycle-free vocabulary
# `forget` and `index` also use. Importing the NAMES (not the module) avoids shadowing the
# `store_episodic(..., records=...)` parameter and keeps `consolidate.EPISODIC_KIND` resolving for tests/demo.
from memory.records import (  # noqa: E402
    BATCH_KEY, DEFAULT_EPISODIC_TAG, EPISODIC_KIND, MARKER_KIND, MARKER_TAG, RECORD_ID_KEY, THROUGH_SEQ_KEY,
    is_injected_record, new_record_id,
)

# The never-consolidated sentinel: STRICTLY below any real `seq` (which is 0-based — capture assigns
# `cursor + offset` from 0). It must NOT be literal 0, or a session whose only genuine turn is at seq 0 would
# read `0 > 0 == False` and never be tidied. A pass advances the watermark to a real seq (>= 0); a session with
# no marker sits at this sentinel, so its first genuine turn (seq 0) is `0 > -1 == True` — detected. (#446)
_NO_WATERMARK = -1

# The closed, Engine-shipped role vocabulary — amendable via the grammar, never
# invented per session. `rationale/pushback` is ONE role (the literal slash); `dead-end` is hyphenated.
ROLE_VOCABULARY = (
    "decision", "rationale/pushback", "lesson", "dead-end", "preference", "intent", "observation",
)
ROLES = frozenset(ROLE_VOCABULARY)

SESSION_ENV = "CLAUDE_CODE_SESSION_ID"   # the live session id (the platform var; NOT capture's older name)
_MAX_DIRECTIVE_IDS = 8                    # cap the directive enumeration; never list thousands of ids
_BACKLOG_ALARM_THRESHOLD = 5             # build-spec leaf: the tidy-up runs off the operator's chat (in a
                                         # subagent), but the normal backlog is ~1 (the previous session). A pile
                                         # this deep means the tidy has stalled, so the MAIN loop breaks silence
                                         # with ONE plain line — a silent failure must not hide (the
                                         # 21-untidied-sessions failure mode, PR #203).
_MAX_TAGS = 8                            # build-spec leaf (#235): the reject-not-coerce ceiling on the
                                         # topic/entity tags the AI may attach to one summary. The directive asks
                                         # for 1–4; 8 is the hard cap so a summary is anchored by a few stable
                                         # topics, never a tag dumping-ground that would blur cross-session
                                         # relatedness (the tags are what roll-up later clusters on).
_LEASE_STALE_N = 3                        # build-spec leaf (maintainer, at the plan gate): a session is "silent"
                                         # (consolidatable) once its lease has aged this many SessionStarts. SMALL
                                         # on purpose — the module consolidates the previous session at the NEXT
                                         # start (the 21-untidied failure mode); a large N would
                                         # reintroduce that passivity. N is only a recovery-promptness knob — the
                                         # store-time re-check below, NOT N, is the real concurrency guarantee, so
                                         # correctness never rides on N's value.


def _lease_is_stale(session_id, epoch, leases, n=_LEASE_STALE_N) -> bool:
    """Has `session_id` gone silent? Its lease being ABSENT (never checked in this era — abandoned, or a session
    that predates the lease) OR aged `>= n` SessionStarts is stale, i.e. safe to consolidate. A genuinely-live
    session re-stamps its lease every turn (capture.refresh_lease_locked), so it reads fresh and is spared."""
    lease = leases.get(session_id)
    if lease is None:
        return True
    return epoch - lease >= n


# --- Reading the raw notes (what the AI consolidates) -----------------------------------------

def _is_genuine_delta(r) -> bool:
    """The ONE shared predicate for 'a genuine turn-delta the sweep consolidates': a turn-delta that is NOT a
    harness-injected pseudo-turn (#274). detect, read, and store ALL key on this — if they disagreed on what
    counts as content, the sweep would loop forever on a junk-only session (an injected `<task-notification>`
    would be detected as pending but yield nothing to store). One filter, one definition, no drift."""
    return isinstance(r, dict) and r.get("kind") == capture.RECORD_KIND and not is_injected_record(r)


def _delta_seq(r) -> int:
    """A record's `seq` as an int (0-based per-message index), defaulting to 0 for a malformed/absent value."""
    s = r.get("seq")
    return s if isinstance(s, int) and not isinstance(s, bool) else 0


def _delta_ts(r) -> int:
    """A record's `ts` as an int (wall-clock seconds), defaulting to 0 for a malformed/absent value."""
    t = r.get("ts")
    return t if isinstance(t, int) and not isinstance(t, bool) else 0


def read_deltas(session_id: str, *, after_seq: int = _NO_WATERMARK, cwd=None) -> list:
    """The raw genuine turn-delta records for one session, ordered by `seq` — what the in-context AI reads to
    write a summary. A pure read (the ledger reader is line-resilient, so no lock is needed). Harness-injected
    pseudo-turns (issue #274) are skipped as fuel — they stay resident + recoverable in the ledger, but the AI
    never consolidates a `<task-notification>` or `/compact` continuation summary as if the operator wrote it.

    `after_seq` scopes to the UN-consolidated tail (`seq > after_seq`) — the incremental sweep (#446) reads a
    session past its last watermark so the AI re-summarizes only its later half. The default `_NO_WATERMARK`
    (-1, below seq 0) returns EVERY genuine delta, so a bare `read_deltas(sid)` stays the full-history read its
    non-sweep callers rely on; the `read` verb and the store residual pass the session's real watermark."""
    out = [
        r for r in ledger.iter_records(path=ledger.ledger_path(cwd))
        if _is_genuine_delta(r) and r.get("session_id") == session_id and _delta_seq(r) > after_seq
    ]
    out.sort(key=_delta_seq)
    return out


# --- Detecting what still needs tidying (the sweep signal — a leaf) ----------------------------

def _marker_watermark(m_ts, through_seq, genuine) -> int:
    """One marker's watermark in seq-space. When it carries a `through_seq` (written now, #446), that IS the
    watermark. A LEGACY marker (pre-#446) has none — project its `ts` boundary into seq-space as the max
    genuine seq captured no later than the marker (`ts <= marker.ts`), the same correlation
    `forget.earned_consolidated_raw` uses. This keeps the model on ONE axis (seq) and, crucially, means a
    pre-#446 ledger is NOT re-consolidated wholesale on rollout — a legacy-tidied session projects to the seq
    it was actually tidied through, not to the never-consolidated sentinel (which would re-summarize its whole
    history into duplicates)."""
    if isinstance(through_seq, int) and not isinstance(through_seq, bool):
        return through_seq
    return max((seq for dts, seq in genuine if dts <= m_ts), default=_NO_WATERMARK)


def _session_states(cwd=None) -> dict:
    """ONE ledger pass → {session_id: (max_genuine_seq, effective_watermark, has_marker)}. The SINGLE shared
    watermark authority — detect, read (via the `read` verb), and store all derive the watermark from HERE, so
    the three can never disagree on how far a session has been swept (the drift that would loop the sweep or
    skip a tail). A session's `max_genuine_seq` counts only genuine (non-injected #274) turn-deltas; a session
    whose deltas are ALL injected has `max_genuine_seq == _NO_WATERMARK` and so is never pending. The effective
    watermark is the MAX across the session's `consolidated` markers (monotonic → revival-safe: a slow sweep
    landing after the session revived and self-consolidated can never regress it); a session with no marker
    sits at `_NO_WATERMARK`."""
    genuine: dict = {}    # sid -> [(ts, seq)] for genuine deltas (ts feeds the legacy-marker projection)
    markers: dict = {}    # sid -> [(ts, through_seq | None)]
    for r in ledger.iter_records(path=ledger.ledger_path(cwd)):
        if not isinstance(r, dict):
            continue
        sid = r.get("session_id")
        if not isinstance(sid, str) or not sid:   # skip a missing/empty id — un-consolidatable, never pending
            continue
        kind = r.get("kind")
        if kind == capture.RECORD_KIND:
            if not is_injected_record(r):
                genuine.setdefault(sid, []).append((_delta_ts(r), _delta_seq(r)))
        elif kind == MARKER_KIND:
            markers.setdefault(sid, []).append((_delta_ts(r), r.get(THROUGH_SEQ_KEY)))
    states = {}
    for sid in set(genuine) | set(markers):
        gts = genuine.get(sid, ())
        max_gseq = max((seq for _ts, seq in gts), default=_NO_WATERMARK)
        ms = markers.get(sid)
        watermark = max((_marker_watermark(m_ts, tseq, gts) for m_ts, tseq in ms), default=_NO_WATERMARK) \
            if ms else _NO_WATERMARK
        states[sid] = (max_gseq, watermark, bool(ms))
    return states


def detect_unconsolidated(live_session_id=None, *, cwd=None) -> list:
    """Session-ids carrying genuine notes BEYOND their last consolidation watermark, EXCLUDING the live session.
    A DETECT leaf: returns the signal (a sorted list of id strings) and renders no operator prose. This single
    predicate unifies the normal, abandoned, and re-tidy sweeps: a never-consolidated session sits at the
    `_NO_WATERMARK` sentinel so any genuine turn makes it pending (the previous session is "no longer live,
    un-swept" by the next SessionStart); a session tidied mid-run and then left idle is pending again exactly
    for its later half (`max_genuine_seq > watermark`); and a fully-swept session is not pending (the sweep
    terminates, #446)."""
    pending = {sid for sid, (max_gseq, watermark, _has) in _session_states(cwd).items() if max_gseq > watermark}
    if live_session_id:
        pending.discard(live_session_id)
    if not pending:
        return []
    # The lease heartbeat: drop any candidate whose session is still LIVE. A CORRUPT lease sidecar means we
    # cannot tell who is live, so skip the whole sweep this pass (fail safe — all sessions possibly-live), DISTINCT
    # from a missing/empty sidecar (all-absent, the intended first-run recovery of prior sessions).
    lease = capture.read_lease_state(ledger.ledger_dir(cwd))
    if lease is None:
        return []
    epoch, leases = lease
    return sorted(sid for sid in pending if _lease_is_stale(sid, epoch, leases))


# --- Writing the tidied summaries (idempotent, race-safe, reject-not-coerce) -------------------

def _validate(records) -> "str | None":
    """None if every record is a well-formed {role in the closed vocabulary, non-empty text, and — if present —
    a `tags` list of ≤ `_MAX_TAGS` non-empty strings}; else a plain rejection reason. REJECT-not-coerce and
    WHOLE-BATCH-atomic: one bad record stores nothing (the engine never silently guesses a label, nor quietly
    drops a malformed tag list, that it was not given cleanly). `tags` absent or empty is valid — a thread with
    no clear topic stays untagged rather than forcing an invented tag (#235). An EMPTY array is valid too — it
    is the explicit "examined this tail, nothing worth summarizing" signal `store_episodic` needs to advance the
    watermark and TERMINATE the sweep (#446); it is deliberate, never a crash (a crashed subagent never reaches
    the store, so no marker lands and the tail is safely re-swept)."""
    if not isinstance(records, (list, tuple)):
        return "summaries must be a JSON array"
    for i, rec in enumerate(records):
        if not isinstance(rec, dict):
            return f"summary {i} is not an object"
        role = rec.get("role")
        if role not in ROLES:
            return f"summary {i} has label {role!r}, which is not one of {list(ROLE_VOCABULARY)}"
        text = rec.get("text")
        if not isinstance(text, str) or not text.strip():
            return f"summary {i} has empty text"
        tags = rec.get("tags")
        if tags is not None:
            if not isinstance(tags, (list, tuple)):
                return f"summary {i} has a tags field that is not a list"
            if len(tags) > _MAX_TAGS:
                return f"summary {i} has more than {_MAX_TAGS} tags"
            if not all(isinstance(t, str) and t.strip() for t in tags):
                return f"summary {i} has a malformed tag (each tag must be a non-empty string)"
    return None


def _make_episodic(session_id: str, rec: dict, batch: str) -> dict:
    """The episodic-summary record envelope. Only `text` is human content; `role`/`tags` are secondary
    filters and `ts`/`consolidated_ts`/`source_seqs`/`v`/`batch` are non-content — so the derived index keeps
    them all OUT of the search body (index._NON_BODY_KEYS + the string-leaf-only projection). `batch` is this
    pass's id (shared with its marker), by which `forget` derives a crashed pass's orphans to logically retire."""
    now = int(time.time())
    tags = [DEFAULT_EPISODIC_TAG]
    for t in rec.get("tags") or []:
        t = t.strip() if isinstance(t, str) else t   # store stripped so " rollup " and "rollup" cluster as one (#235)
        if isinstance(t, str) and t and t not in tags:
            tags.append(t)
    out = {
        "v": capture.RECORD_VERSION,
        "kind": EPISODIC_KIND,
        RECORD_ID_KEY: new_record_id(),     # the stable, content-free record id minted at capture
        "session_id": session_id,
        "ts": now,
        "role": rec["role"],
        "text": rec["text"].strip(),
        "tags": tags,
        "consolidated_ts": now,
        BATCH_KEY: batch,
    }
    seqs = rec.get("source_seqs")
    if isinstance(seqs, (list, tuple)) and seqs:
        clean = [int(s) for s in seqs if isinstance(s, int) or (isinstance(s, str) and s.isdigit())]
        if clean:
            out["source_seqs"] = clean
    return out


def _make_marker(session_id: str, batch: str, *, through_seq: "int | None" = None) -> dict:
    """The consolidation marker — an in-ledger record (not a sidecar), so it travels with the backup law
    ("copy the ledger") and the sweep can never re-tidy a session after a restore. It carries this pass's
    `batch` id: a *completed* pass is exactly one whose episodics' batch has a marker, so `forget` can
    logically retire the orphan episodics of a pass that crashed before its marker landed.

    `through_seq` (#446) is the per-session HIGH-WATER-MARK — the `seq` this pass swept THROUGH. A marker
    written now always carries it (so a session tidied mid-run is re-swept for only its later half); it is an
    OPTIONAL keyword because a LEGACY marker on disk (pre-#446) lacks the field and `_session_states` projects
    its `ts` boundary into seq-space instead. `store_episodic` is the sole production caller and always passes
    it; a bare `_make_marker(sid, batch)` mints a legacy-shaped marker (used only by tests/fixtures)."""
    now = int(time.time())
    marker = {"v": capture.RECORD_VERSION, "kind": MARKER_KIND, RECORD_ID_KEY: new_record_id(),
              "session_id": session_id, "ts": now, "tags": [MARKER_TAG], BATCH_KEY: batch}
    if isinstance(through_seq, int) and not isinstance(through_seq, bool):
        marker[THROUGH_SEQ_KEY] = through_seq
    return marker


def store_episodic(session_id: str, records, *, cwd=None) -> dict:
    """Append the AI-authored episodic summaries for `session_id`, then the consolidation marker, then rebuild
    the fast lookup so a summary is immediately findable. Returns a small report dict.

    Safety: validate (reject-not-coerce, whole-batch) BEFORE any write; then, under the SHARED capture lock
    (so a concurrent capture or a second boot can never interleave), RECOMPUTE the residual — the genuine
    deltas beyond the session's current effective watermark — and append. The marker is written LAST: a crash
    between the summary appends and the marker re-files next sweep (a duplicate, never a loss — `forget` then
    logically retires the orphaned pass from recall, leaving it recoverable in the ledger). Lock contention =>
    a clean no-op this boot (the sweep retries next session); it NEVER writes lock-free.

    Incremental (#446): the marker records `through_seq` = the high-water genuine `seq` this pass examined, so
    a session tidied mid-run is re-swept for only its later half. Recomputing the residual UNDER THE LOCK (not
    a "does the watermark already cover this?" check) is the concurrency guarantee: a concurrent boot — or the
    session's own later self-consolidation — can never double-consolidate an already-swept prefix, because the
    second writer sees the first's advanced watermark and finds an empty residual. An EMPTY `records` with a
    non-empty residual is the "examined, nothing to summarize" case: it writes a marker (no episodics) that
    still advances the watermark, so the sweep TERMINATES instead of re-firing that tail every session."""
    reason = _validate(records)
    if reason is not None:
        return {"status": "rejected", "reason": reason, "stored": 0}
    if not isinstance(session_id, str) or not session_id:
        return {"status": "rejected", "reason": "missing session id", "stored": 0}

    data_dir = ledger.ledger_dir(cwd)
    os.makedirs(data_dir, exist_ok=True)
    lock_fd = capture._acquire_lock(os.path.join(data_dir, capture.LOCK_FILENAME))
    if lock_fd is None:
        return {"status": "busy", "reason": "another memory write held the lock; the sweep retries next session",
                "stored": 0}
    try:
        max_gseq, watermark, has_marker = _session_states(cwd).get(
            session_id, (_NO_WATERMARK, _NO_WATERMARK, False))
        # Idempotent no-op: nothing genuine lies beyond the watermark (the session is fully swept), AND either a
        # marker already records that or there is nothing to store — so writing again would only duplicate. A
        # genuine TAIL beyond the watermark is NOT caught here: it advances the mark below (that is the whole
        # point of the incremental sweep, and the termination path for an unsummarizable tail).
        if max_gseq <= watermark and (has_marker or not records):
            return {"status": "already-consolidated", "stored": 0}
        # The store-time re-check — the actual concurrency guarantee. Detection ran at SessionStart; this
        # write lands minutes later, in a subagent. RE-COMPUTE staleness under the held lock: if the target has
        # checked in since detection (its lease now reads fresh) — or the lease is unreadable — ABORT before any
        # append, leaving it for its own consolidation. This ALSO closes the incremental revival race: a genuine
        # delta appended after the subagent's `read` would refresh the lease, so the target reads fresh here and
        # we abort rather than advance `through_seq` past a delta the AI never examined.
        lease = capture.read_lease_state(data_dir)
        if lease is None:
            return {"status": "deferred", "reason": "lease unreadable; not consolidating a possibly-live session",
                    "stored": 0}
        epoch, leases = lease
        if not _lease_is_stale(session_id, epoch, leases):
            return {"status": "live", "reason": "session checked in since detection; left for its own tidy-up",
                    "stored": 0}
        ledger_file = ledger.ledger_path(cwd)
        # Advance the watermark to the high-water genuine message this pass EXAMINED — past examined-but-
        # unsummarizable turns alongside any summarized ones — so the sweep terminates. `max`
        # never regresses a watermark a concurrent pass already advanced (monotonic → revival-safe).
        through_seq = max(max_gseq, watermark)
        batch = uuid.uuid4().hex   # ONE id for this whole pass, stamped on every episodic AND the marker below
        stored = 0
        for rec in records:
            ledger.append(_make_episodic(session_id, rec, batch), path=ledger_file)
            stored += 1
        # Marker LAST, carrying the SAME batch: a crash before it orphans this pass's episodics (their batch
        # unmarked) — the next sweep re-files and `forget` logically retires the orphans (favor duplicate over loss).
        ledger.append(_make_marker(session_id, batch, through_seq=through_seq), path=ledger_file)
        # Reap this session's lease under the same held lock. A re-swept session's lease can go here: if it
        # revives, capture re-stamps it next turn, and a concurrent sweep in the gap finds an empty residual.
        capture.drop_lease_locked(data_dir, session_id)
        index.rebuild(ledger_file=ledger_file, index_file=index.index_path(cwd))
        return {"status": "ok", "stored": stored}
    finally:
        capture._release_lock(lock_fd)


# --- Memory's own hooks (SessionStart sweep + PreCompact compaction trigger) -------------------

def _consolidation_directive(pending: list) -> str:
    """The directive the SessionStart sweep injects when earlier sessions need tidying. It stays SUBORDINATE to
    the operator's request (memory's operation, not orientation — never a first-turn hijack) and
    ACTIVE (done THIS session — not "whenever, or never"; that passivity is what left 21 sessions untidied). The
    mechanics run OFF the operator's main transcript: the main loop SPAWNS A SUBAGENT to do the read/store, and
    when it returns the main loop relays nothing about the tidy-up (only the runtime's own brief task card may
    show). The ONE exception is a stalled backlog (>= `_BACKLOG_ALARM_THRESHOLD`): then the MAIN loop itself
    speaks ONE plain line (a COUNT, never the id codes) so a silent failure can't hide — that alarm is the main
    loop's job, never the suppressed subagent's. The ids are listed for the subagent's `read`/`store` only.
    Memory's own hook composes this prose; the detection leaf above renders nothing."""
    n = len(pending)
    shown = pending[:_MAX_DIRECTIVE_IDS]
    ids = ", ".join(shown) + (f", and {n - len(shown)} more" if n > len(shown) else "")
    sessions = "1 earlier session" if n == 1 else f"{n} earlier sessions"
    have = "has" if n == 1 else "have"
    behind = (
        f" The backlog has fallen behind ({sessions}), which usually means this background tidy-up has been "
        "getting skipped. Because the subagent's work is not shown to the operator, YOU (the main loop) must "
        "this once break the silence yourself: at the first natural pause, tell the operator in plain words "
        "that memory upkeep has fallen behind and you're catching it up now. Give them only that COUNT, never "
        "the id codes or any internal terms."
    ) if n >= _BACKLOG_ALARM_THRESHOLD else ""
    return (
        "Memory upkeep — a background pass that must NOT clutter the operator's chat. Do it DURING this "
        "session, but ALWAYS after you have served the operator's current request (never before it, never a "
        "first-turn hijack). Do not announce it — say nothing to the operator about tidying memory unless "
        "they ask (if asked, a count is enough; the session ids stay internal)." + behind + " "
        f"{sessions} {have} raw turn-notes not yet folded into summaries. At the first natural pause, "
        "once the operator's current request is served, SPAWN A SUBAGENT (the Task tool) to do the read/store "
        "so its tool calls and output stay off the operator's main transcript — only a brief task card may "
        "show. When the subagent returns, relay NOTHING to the operator about the tidy-up: do not summarize "
        "it, do not report a count, do not say \"done\" — its result stays internal (the stalled-backlog line "
        "above, if present, is the only thing you ever say). Hand the subagent this full instruction and these "
        f"session ids: {ids}. Tell it: for each id, run "
        "`.engine/.venv/bin/python .engine/tools/memory/consolidate.py read <session-id>` to read its raw "
        "notes, write a short compact summary of each meaningful thread (what was explored; what was decided "
        "or rejected, and why; what the operator prefers or learned), choose ONE label per summary from "
        f"{{{', '.join(ROLE_VOCABULARY)}}}. "
        "For each summary ALSO assign a short list of topic/entity tags — 1 to 4 lowercase tokens naming what "
        "the thread is ABOUT (a subsystem, feature, file area, or concept, e.g. `rollup`, `erasure`, "
        "`backup`), plus any decision record it names verbatim, kept in its canonical case so two notes about the "
        "same decision share a tag — an engine decision id like `eADR-0031`, or a product ADR id like "
        "`docs/adr/0007-slug` (whatever id your project's ADRs carry). These tags are what a later pass uses to "
        "relate notes ACROSS sessions; they are NOT "
        "the label. Prefer stable nouns you would reuse across sessions over one-off phrases, keep the whole "
        "list to at most 8 tags, and omit the list rather than invent a tag. "
        "If the operator explicitly asked to remember something (\"remember X\", \"always do Y\"), the "
        "subagent must preserve THAT as its own summary typed `preference` — a durable operator instruction, "
        "never folded into another thread's summary and never dropped as a passing note. The subagent then "
        "stores the summaries with `… consolidate.py store <session-id>` (a JSON array of "
        "{\"role\": …, \"text\": …, \"tags\": [\"…\", …]} on stdin; a summary's own tags may be omitted). If a "
        "whole session turns out to have nothing worth summarizing, STILL run `store` for it, passing a JSON "
        "array with no summaries in it (`[]`) — that records the session as examined so it is not flagged again "
        "every session; skipping the store entirely leaves it pending forever. This is "
        "reflection, not re-litigation — be terse. The "
        "operator's request always comes first; the subagent keeps the mechanics off the main transcript — "
        "but do it this session, not someday."
    )


def _session_start_handler(payload) -> dict:
    """Memory's ONE SessionStart behavior: inject ONE combined directive carrying BOTH maintenance
    backlogs — untidied raw notes (consolidation) and clusters of old episodes ready to roll up (roll-up,
    via the lazy `rollup` import) — or proceed silently when neither is pending (the self-
    interference floor: this fires on the operator's OWN sessions, so a nothing-pending start must add nothing).
    ONE injection, not two competing ones, keeps the operator's first turn unsplit. FINE-GRAINED fail-open: a
    roll-up fault degrades to consolidation-only (it can never drop the older, more important consolidation
    directive); run_hook fail-opens the whole handler on any other fault."""
    live = payload.get("session_id") if isinstance(payload, dict) else None
    # Stamp THIS session's lease before sweeping: bump the epoch and record us live, so a concurrent sweep
    # never mistakes us for abandoned. If the stamp can't land (lock contention / a corrupt sidecar), DEFER the
    # consolidation sweep this pass — never sweep with a missing self-lease — but let roll-up (lease-independent)
    # still run. With no live id we can't stamp; fall through to a best-effort detection (the lease filter still
    # guards concurrent sessions).
    if live and not capture.open_session_lease(ledger.ledger_dir(), live):
        pending = []
    else:
        pending = detect_unconsolidated(live_session_id=live)
    roll_block = ""
    try:
        from memory import rollup   # lazy: keep rollup + its deps off the cold-start load path until needed
        groups = rollup.detect_rollup_candidates()   # the COLD-tier floor self-excludes the live session (no live id)
        if groups:
            roll_block = rollup.rollup_directive(groups)
    except Exception:
        roll_block = ""   # fine-grained fail-open: a roll-up fault never drops the consolidation directive
    if not pending and not roll_block:
        return hooks.proceed()
    blocks = []
    if pending:
        blocks.append(_consolidation_directive(pending))
    if roll_block:
        blocks.append(roll_block)
    return hooks.inject("\n\n".join(blocks))


def _pre_compact_handler(payload) -> dict:
    """PreCompact CANNOT reach the in-context AI (the shipped runtime: a hook never makes the model generate;
    PreCompact's only lever is blocking compaction), so AI consolidation stays on the SessionStart sweep. But
    DETERMINISTIC ledger compaction is exactly the cheap pre-compaction housekeeping this hook CAN do — it rides
    the "tolerable moment, never the hot path" the design names for the expensive step and, IF
    enough reclaimable waste has piled up, folds-and-prunes it. `maybe_compact` is fail-open (never
    raises); the compaction it rides folds Layer-1 bookkeeping AND physically removes any record an
    `operator-adjudicated-erasure` marker targets (Layer-2) — and the sole minter, `compact.enact_erasure`, is now
    fired only by the cross-session erasure observer, and only for a target the operator authorised by merging a
    single-purpose `engine-erasure` PR, so absent such a merge it still erases nothing. This handler ALWAYS proceeds — PreCompact must never block the squash."""
    from memory import compact   # lazy: keep compaction's import graph off the SessionStart load path
    compact.maybe_compact()       # gated on reclaimable waste; report dropped (a leaf renders no prose); fail-open
    return hooks.proceed()


# --- CLI (the hook entry points + the verbs the in-context AI / operator call) -----------------

def main(argv: list) -> int:
    cmd = argv[0] if argv else "demo"
    if cmd == "session-start":
        return hooks.run_hook("SessionStart", _session_start_handler)
    if cmd == "pre-compact":
        return hooks.run_hook("PreCompact", _pre_compact_handler)
    if cmd == "detect":
        import providers   # lazy: the provider seam (neutral override, then the platform session vars)
        for sid in detect_unconsolidated(os.environ.get(SESSION_ENV) or providers.session_from_env()):
            print(sid)
        return 0
    if cmd == "read":
        if len(argv) < 2:
            print("usage: consolidate.py read <session-id>", file=sys.stderr)
            return 2
        # Scope to the UN-consolidated tail: the sweep re-reads a session only past its last watermark, so the
        # AI re-summarizes just its later half (#446). A never-consolidated session sits at _NO_WATERMARK, so
        # this returns its whole history.
        _gseq, watermark, _has = _session_states().get(argv[1], (_NO_WATERMARK, _NO_WATERMARK, False))
        deltas = read_deltas(argv[1], after_seq=watermark)
        print(json.dumps(
            [{"seq": r.get("seq"), "speaker": r.get("speaker"), "text": r.get("text")} for r in deltas],
            ensure_ascii=False, indent=2,
        ))
        return 0
    if cmd == "store":
        if len(argv) < 2:
            print("usage: consolidate.py store <session-id>   (a JSON array of {role,text} on stdin)",
                  file=sys.stderr)
            return 2
        try:
            records = json.load(sys.stdin)
        except ValueError as exc:
            print(f"could not parse the summaries from stdin (expected a JSON array): {exc}", file=sys.stderr)
            return 2
        report = store_episodic(argv[1], records)
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report.get("status") in ("ok", "already-consolidated") else 1
    if cmd == "demo":
        return _demo()
    print(f"usage: consolidate.py [session-start|pre-compact|detect|read <sid>|store <sid>|demo]\n"
          f"unknown command {cmd!r}", file=sys.stderr)
    return 2


# --- Operator demonstration -------------------------------------------------------------------
# A walkthrough on a THROWAWAY practice cabinet (a temp folder), never real data. It runs the REAL consolidate
# functions and reads the cabinet back, so every claim is recognizable words on screen. The ONE step that needs
# a live AI — composing a GOOD summary — is SIMULATED with a clearly-labelled sample, and the exact directive
# the real AI would receive is printed. Run it and vary the raw notes AND the sample summaries yourself:
#     uv run --directory .engine --frozen -- python tools/memory/consolidate.py demo

# Vary these and re-run. Note: none of the sample summary texts contains the word "decision" — that is what
# lets Part 3 prove the LABEL "decision" is never search text (searching it returns none, though all are
# labelled "decision"). If you add the word "decision" to a text, it will (correctly) be found as content.
_DEMO_RAW_A = [
    ("user", "Let's make the pelican-feeding schedule configurable instead of hard-coded."),
    ("assistant", "Good idea — I'll add a setting. I also weighed a cron approach but ruled it out: cron "
                  "cannot see the user's calendar."),
]
_DEMO_SUMMARY_A = {
    "role": "decision",
    "text": "Made the pelican-feeding schedule configurable; ruled out the cron approach because it cannot "
            "see the calendar.",
    "tags": ["pelican", "scheduling"],
}
# Part 3 — three summaries, all labelled "decision", distinct content (no "decision" word) → search "decision"
# must return NONE (the label is not indexed), while each is found by its own word.
_DEMO_POLLUTION = [
    {"role": "decision", "text": "Chose kubernetes for the staging cluster rollout."},
    {"role": "decision", "text": "Settled on weekly invoice batches instead of daily."},
    {"role": "decision", "text": "Picked a darkmode-first palette for the dashboard."},
]


def _filed_summary(query_word: str):
    """Read a stored SUMMARY back out of the cabinet by one of its own words (so what is shown is what landed,
    not what we hoped). Filters to episodic records — a raw turn-note can share a word with its summary, and
    here we are showing the summary. Returns the first matching episodic record or None."""
    for rec in index.query(query_word).records:
        if isinstance(rec, dict) and rec.get("kind") == EPISODIC_KIND:
            return rec
    return None


def _demo() -> int:
    import tempfile

    print("=" * 80)
    print("MEMORY — tidying raw turn-notes into clean, labelled summaries (a practice run)")
    print("=" * 80)
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["ENGINE_MEMORY_DIR"] = tmp           # the throwaway cabinet
        try:
            ok = _demo_body()
        finally:
            os.environ.pop("ENGINE_MEMORY_DIR", None)

    print("\n" + "-" * 80)
    print("Reminder: that was a PRACTICE cabinet, thrown away when this demo ended. These summaries are")
    print("written BY the AI about your sessions — its read of what you decided, preferred, and learned. Like")
    print("the raw notes, they are private, local, and deletable — never shipped or uploaded anywhere. If a")
    print("summary misreads you, delete it: the raw notes it came from are still in the cabinet, so you lose")
    print("nothing you can't re-tidy. The one thing this practice run CANNOT show is whether the AI writes a")
    print("GOOD summary — that is judged on your real sessions, where every summary is readable and deletable;")
    print("here the summaries are hand-written SAMPLES standing in for the AI's. To vary it: edit the raw")
    print("notes AND the sample summaries near the top of this file and re-run.")
    if not ok:
        print("\nDEMO UNEXPECTED: the filing around the summary did not behave as expected (stored and "
              "findable, the label kept out of search text, the backlog detection).", file=sys.stderr)
        return 1
    return 0


def _demo_body() -> bool:
    session_a = "session-pelican"
    session_b = "session-three-labels"
    session_c = "session-login-fix"
    session_d = "session-abandoned"
    session_live = "session-current-live"

    print("\nPART 1 — raw notes go IN, a clean LABELLED summary comes OUT")
    print("-" * 80)
    for seq, (spk, txt) in enumerate(_DEMO_RAW_A):
        ledger.append(capture._make_record(session_a, seq, spk, txt))
    print(f"  Filed {len(_DEMO_RAW_A)} raw turn-notes for '{session_a}'. The raw notes the AI would read:")
    for d in read_deltas(session_a):
        print(f"    - ({d['speaker']}) {d['text']}")
    print("  The AI then writes a short labelled summary. (Here that summary is a hand-written SAMPLE standing")
    print("  in for what the live AI writes — this run proves the FILING around it, not the AI's wording.)")
    report = store_episodic(session_a, [_DEMO_SUMMARY_A])
    out = _filed_summary(capture._demo_distinctive_word(_DEMO_SUMMARY_A["text"]))
    print(f"  Stored: {report}")
    if out:
        topics = [t for t in out.get("tags", []) if t != DEFAULT_EPISODIC_TAG]
        print(f"    label:   {out['role']}")
        print(f"    summary: {out['text']}")
        print(f"    topics:  {topics}   (the topic tags the AI would assign — like the summary, these are a")
        print("             hand-written SAMPLE here; kept out of search text, they are what a later roll-up")
        print("             pass uses to relate this note to others ACROSS sessions)")
    print("  => The filing around the AI's summary works (stored, labelled, TAGGED, readable back). Whether the")
    print("     summary itself is GOOD is judged live on your real sessions.")

    print("\nPART 2 — a tidied summary is findable by its own words")
    print("-" * 80)
    word = capture._demo_distinctive_word(_DEMO_SUMMARY_A["text"])
    found = _filed_summary(word)
    print(f"  ask for \"{word}\"  ->  {found['text'] if found else '(nothing found)'}")
    print(f"  => Found by a word from the summary itself, and it carries its label "
          f"('{found['role'] if found else '?'}').")

    print("\nPART 3 — the LABEL never pollutes search (a separate field, kept out of the search text)")
    print("-" * 80)
    store_episodic(session_b, _DEMO_POLLUTION)
    labelled_decision = 1 + len(_DEMO_POLLUTION)   # session_a's summary + the three here
    by_label = index.query("decision").records
    print(f"  {labelled_decision} summaries are labelled \"decision\". Searching the label \"decision\" "
          f"returns: {len(by_label)} (expected 0).")
    print("  Yet each is found by its OWN words:")
    for rec in _DEMO_POLLUTION:
        w = capture._demo_distinctive_word(rec["text"])
        hit = _filed_summary(w)
        print(f"    ask for \"{w}\"  ->  {hit['text'] if hit else '(nothing found)'}")
    ok = len(by_label) == 0
    print(f"  => {'The label is never search text — searching it drags in nothing, though all carry it.' if ok else '!!! the label leaked into search'}")

    print("\nPART 4 — a tidied session is re-swept ONLY for notes added later (and settles when there's no more)")
    print("-" * 80)
    ledger.append(capture._make_record(session_c, 0, "user", "The login page logs people out after thirty minutes."))
    before = detect_unconsolidated(live_session_id=session_live)
    print(f"  Sessions still needing tidy-up (before): {before}")
    store_episodic(session_c, [{"role": "lesson",
                                "text": "The thirty-minute logout was a too-short session-timeout setting."}])
    after = detect_unconsolidated(live_session_id=session_live)
    print(f"  After tidying '{session_c}': {after}   (it dropped off — its first half is summarized)")
    # A LATER note is added to the same session (it was idle, now it says a bit more):
    ledger.append(capture._make_record(session_c, 1, "user", "Also: raise the same timeout on the mobile app."))
    reappears = detect_unconsolidated(live_session_id=session_live)
    _g, wm_c, _h = _session_states().get(session_c, (-1, -1, False))
    tail = [d["text"] for d in read_deltas(session_c, after_seq=wm_c)]
    print(f"  A later note is added -> needing tidy-up again: {reappears}")
    print(f"    the sweep re-reads ONLY the later half (not the whole session): {tail}")
    store_episodic(session_c, [{"role": "lesson", "text": "The mobile app needs the same longer timeout."}])
    settled = detect_unconsolidated(live_session_id=session_live)
    retidy_ok = session_c not in after and session_c in reappears \
        and tail == ["Also: raise the same timeout on the mobile app."] and session_c not in settled
    print(f"  After tidying the later half: {settled}")
    # A genuine-but-trivial tail (nothing worth summarizing): storing an empty result still SETTLES it, so the
    # sweep terminates instead of re-flagging it forever.
    ledger.append(capture._make_record(session_c, 2, "user", "Thanks, that's all for today."))
    before_term = session_c in detect_unconsolidated(live_session_id=session_live)
    store_episodic(session_c, [])   # "examined this tail, nothing worth summarizing"
    terminates_ok = before_term and session_c not in detect_unconsolidated(live_session_id=session_live)
    print(f"  A trivial tail ('thanks, that's all') is examined and settled with no new summary -> terminates: "
          f"{terminates_ok}")
    print(f"  => '{session_c}' is re-tidied for each later half, and the sweep always settles — it drains real "
          f"work and never re-does what's already summarized.")

    print("\nPART 5 — a past session that was never tidied is detected (e.g. one closed without a clean finish)")
    print("-" * 80)
    ledger.append(capture._make_record(session_d, 0, "user", "Remember: the spare key is under the blue pot."))
    ledger.append(capture._make_record(session_live, 0, "user", "I'm working in this session right now."))
    pending = detect_unconsolidated(live_session_id=session_live)
    print(f"  The session you are in now is '{session_live}' (it has notes too, but is still being written).")
    print(f"  Sessions detected as needing tidy-up: {pending}")
    print(f"    -> '{session_d}' (a past session, never tidied) IS listed; the live "
          f"'{session_live}' is NOT.")
    print("\n  The directive the engine would hand the AI at the next session start (the AI spawns a subagent")
    print("  to do this at a pause, so the read/store mechanics stay off your main transcript — only a brief")
    print("  task card may show):\n")
    for line in _wrap(_consolidation_directive(pending), 76):
        print(f"    | {line}")
    behind_demo = [f"stalled-{i:02d}" for i in range(_BACKLOG_ALARM_THRESHOLD + 1)]
    print(f"\n  And the SAME directive once the backlog has stalled past the alarm threshold "
          f"({_BACKLOG_ALARM_THRESHOLD}+ untidied")
    print("  sessions) — it breaks silence with ONE plain line so a silent failure can't hide:\n")
    for line in _wrap(_consolidation_directive(behind_demo), 76):
        print(f"    | {line}")
    print("\n  => A past, never-tidied session is caught by the same sweep; the AI receives the directive above")
    print("     and spawns a subagent to do the tidy-up THIS session, right after your request — keeping the")
    print("     mechanics off your chat, unless the backlog has fallen behind, when the main loop surfaces one")
    print("     plain line (a count, never the id codes). (Whether the spawn actually keeps the mechanics off")
    print("     your transcript is a property of the live runtime, proven on the next real session; whether it")
    print("     then writes a good summary is also judged live.)")
    print("\nPART 6 — the engine's own notifications never get tidied as if YOU had said them")
    print("-" * 80)
    session_injected = "session-with-notifications"
    ledger.append(capture._make_record(
        session_injected, 0, "user", "Let's redesign the export to write a manifest first."))
    ledger.append(capture._make_record(
        session_injected, 1, "user", "<task-notification>\n  a background job finished\n</task-notification>",
        injected=True))                                  # tagged at capture (the durable path)
    ledger.append(capture._make_record(
        session_injected, 2, "user",
        "This session is being continued from a previous conversation that ran out of context."))  # back-compat, untagged
    tidy_notes = [d["text"] for d in read_deltas(session_injected)]
    resident = [r for r in ledger.iter_records(path=ledger.ledger_path())
                if isinstance(r, dict) and r.get("session_id") == session_injected]
    print(f"  Filed 3 notes for '{session_injected}': your real request, a background")
    print("  'job finished' notice, and a 'continued from an earlier chat' banner.")
    print("  What the AI then reads to tidy this session:")
    for note in tidy_notes:
        print(f"    - {note}")
    print(f"  Still kept in the cabinet, recoverable — all {len(resident)} notes (nothing was deleted).")
    injected_skipped = tidy_notes == ["Let's redesign the export to write a manifest first."]
    injected_resident = len(resident) == 3
    print(f"  => {'The notice and banner are skipped but kept — only your real words are summarised.' if (injected_skipped and injected_resident) else '!!! a harness notification leaked into the tidy-up, or a note was lost'}")

    # The mechanical invariants this practice run proves (NOT the AI's wording, which is judged live): a
    # summary stored+findable, the label kept out of search text, a tidied session dropping off the backlog,
    # a tidied session RE-SWEPT only for its later half and then SETTLING (incremental + termination, #446),
    # a past untidied session detected while the live one is not, and the engine's own injected notifications
    # skipped as tidy-up fuel while staying resident + recoverable.
    return (bool(out) and ok and session_c in before and session_c not in after
            and retidy_ok and terminates_ok
            and session_d in pending and session_live not in pending
            and injected_skipped and injected_resident)


def _wrap(text: str, width: int) -> list:
    """A tiny word-wrap so the printed directive is legible (stdlib textwrap, kept local to the demo)."""
    import textwrap
    return textwrap.wrap(text, width=width)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
