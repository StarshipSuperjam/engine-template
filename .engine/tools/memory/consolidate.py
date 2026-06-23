"""consolidate.py — AI-judged episodic consolidation: the reflection half of the memory substrate (slice 3b).

The 3a content half (capture.py) saves every completed turn's RAW notes to the ledger. This module is the
REFLECTION half — turning those raw turn-deltas into clean, role-typed EPISODIC summaries (the "tidy-up"):

  - **The tidy-up is AI-judged, and a hook cannot think.** A compact episodic summary ("explored X; rejected
    Y because Z; the operator wants W") is written by the in-context AI, which types it with ONE closed ROLE
    (decision / rationale-pushback / lesson / dead-end / preference / intent / observation — D-030). This was
    validated against the SHIPPED Claude Code runtime: a hook steers the model only by injecting context a
    *later* turn reads, or by blocking — it can NEVER make the model generate. So consolidation cannot run
    inside a `PreCompact`/close hook; the model is not in that loop.

  - **The mechanism: a `SessionStart` sweep.** Memory's own `SessionStart` hook DETECTS earlier sessions whose
    raw notes were never tidied (have turn-deltas, no consolidation marker, not the live session) and INJECTS
    a directive — the in-context AI, at the first natural pause AFTER the operator's request (so never a
    first-turn hijack), reads each session's raw notes (`read`), writes a short labelled summary of each thread,
    and stores it (`store`). The pass is QUIET: the AI does NOT announce the tidy-up to the operator — UNLESS the
    backlog has grown past `_BACKLOG_ALARM_THRESHOLD` (a sign the silent tidy has stalled), when it surfaces ONE
    plain line (a COUNT, never the id codes) so a silent failure can't hide. The directive stays prompt — done
    THIS session, not deferred forever (the passivity that left 21 sessions untidied is gone) — and always
    subordinate to the operator's request.
    This unifies the "normal" and "abandoned-session" consolidation into ONE sweep: the locked design's
    abandoned-session predicate already subsumes the normal path, since the previous session is "no longer
    live with no marker" by the next start. The sweep ALSO carries the roll-up backlog (slice 5 PR 3) in the
    same single injection. `PreCompact` cannot reach the AI, so it carries no consolidation — but it now rides
    the deterministic ledger-compaction trigger (`compact.maybe_compact`).

  - **Leaf discipline (principle §16).** `detect_unconsolidated` RETURNS session-id signals and renders no
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
    BATCH_KEY, DEFAULT_EPISODIC_TAG, EPISODIC_KIND, MARKER_KIND, MARKER_TAG, RECORD_ID_KEY, new_record_id,
)

# The closed, Engine-shipped role vocabulary (D-030 / memory/README) — amendable via the grammar, never
# invented per session. `rationale/pushback` is ONE role (the literal slash); `dead-end` is hyphenated.
ROLE_VOCABULARY = (
    "decision", "rationale/pushback", "lesson", "dead-end", "preference", "intent", "observation",
)
ROLES = frozenset(ROLE_VOCABULARY)

SESSION_ENV = "CLAUDE_CODE_SESSION_ID"   # the live session id (the platform var; NOT capture's older name)
_MAX_DIRECTIVE_IDS = 8                    # cap the directive enumeration; never list thousands of ids
_BACKLOG_ALARM_THRESHOLD = 5             # build-spec leaf: the tidy-up is a QUIET pass, but the normal backlog is
                                         # ~1 (the previous session). A pile this deep means the silent tidy has
                                         # stalled, so the sweep breaks silence with ONE plain line — a silent
                                         # failure must not hide (the 21-untidied-sessions failure mode, PR #203).


# --- Reading the raw notes (what the AI consolidates) -----------------------------------------

def read_deltas(session_id: str, *, cwd=None) -> list:
    """The raw turn-delta records for one session, ordered by `seq` — what the in-context AI reads to write a
    summary. A pure read (the ledger reader is line-resilient, so no lock is needed)."""
    out = [
        r for r in ledger.iter_records(path=ledger.ledger_path(cwd))
        if isinstance(r, dict) and r.get("kind") == capture.RECORD_KIND and r.get("session_id") == session_id
    ]
    out.sort(key=lambda r: r["seq"] if isinstance(r.get("seq"), int) else 0)
    return out


# --- Detecting what still needs tidying (the sweep signal — a leaf) ----------------------------

def _scan_sessions(cwd=None):
    """ONE ledger pass → (session-ids with >=1 raw turn-delta, session-ids with a consolidation marker)."""
    have_deltas, marked = set(), set()
    for r in ledger.iter_records(path=ledger.ledger_path(cwd)):
        if not isinstance(r, dict):
            continue
        sid = r.get("session_id")
        if not isinstance(sid, str) or not sid:   # skip a missing/empty id — un-consolidatable, never pending
            continue
        kind = r.get("kind")
        if kind == capture.RECORD_KIND:
            have_deltas.add(sid)
        elif kind == MARKER_KIND:
            marked.add(sid)
    return have_deltas, marked


def detect_unconsolidated(live_session_id=None, *, cwd=None) -> list:
    """Session-ids with raw notes but no consolidation marker, EXCLUDING the live session. A DETECT leaf:
    returns the signal (a sorted list of id strings) and renders no operator prose. This single predicate
    unifies the normal and abandoned-session sweeps — the previous session is "no longer live, no marker" by
    the next SessionStart, so it is caught here exactly like a session that ended abruptly."""
    have, marked = _scan_sessions(cwd)
    pending = have - marked
    if live_session_id:
        pending.discard(live_session_id)
    return sorted(pending)


# --- Writing the tidied summaries (idempotent, race-safe, reject-not-coerce) -------------------

def _validate(records) -> "str | None":
    """None if every record is a well-formed {role in the closed vocabulary, non-empty text}; else a plain
    rejection reason. REJECT-not-coerce and WHOLE-BATCH-atomic: one bad record stores nothing (the engine
    never silently guesses a label it was not given)."""
    if not isinstance(records, (list, tuple)) or not records:
        return "no summaries to store"
    for i, rec in enumerate(records):
        if not isinstance(rec, dict):
            return f"summary {i} is not an object"
        role = rec.get("role")
        if role not in ROLES:
            return f"summary {i} has label {role!r}, which is not one of {list(ROLE_VOCABULARY)}"
        text = rec.get("text")
        if not isinstance(text, str) or not text.strip():
            return f"summary {i} has empty text"
    return None


def _make_episodic(session_id: str, rec: dict, batch: str) -> dict:
    """The episodic-summary record envelope. Only `text` is human content; `role`/`tags` are secondary
    filters and `ts`/`consolidated_ts`/`source_seqs`/`v`/`batch` are non-content — so the derived index keeps
    them all OUT of the search body (index._NON_BODY_KEYS + the string-leaf-only projection). `batch` is this
    pass's id (shared with its marker), by which `forget` derives a crashed pass's orphans to logically retire."""
    now = int(time.time())
    tags = [DEFAULT_EPISODIC_TAG]
    for t in rec.get("tags") or []:
        if isinstance(t, str) and t and t not in tags:
            tags.append(t)
    out = {
        "v": capture.RECORD_VERSION,
        "kind": EPISODIC_KIND,
        RECORD_ID_KEY: new_record_id(),     # the stable, content-free record id minted at capture (slice 4b)
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


def _make_marker(session_id: str, batch: str) -> dict:
    """The consolidation marker — an in-ledger record (not a sidecar), so it travels with the backup law
    ("copy the ledger") and the sweep can never re-tidy a session after a restore. It carries this pass's
    `batch` id: a *completed* pass is exactly one whose episodics' batch has a marker, so `forget` can
    logically retire the orphan episodics of a pass that crashed before its marker landed."""
    now = int(time.time())
    return {"v": capture.RECORD_VERSION, "kind": MARKER_KIND, RECORD_ID_KEY: new_record_id(),
            "session_id": session_id, "ts": now, "tags": [MARKER_TAG], BATCH_KEY: batch}


def store_episodic(session_id: str, records, *, cwd=None) -> dict:
    """Append the AI-authored episodic summaries for `session_id`, then the consolidation marker, then rebuild
    the fast lookup so a summary is immediately findable. Returns a small report dict.

    Safety: validate (reject-not-coerce, whole-batch) BEFORE any write; then, under the SHARED capture lock
    (so a concurrent capture or a second boot can never interleave), re-check the marker is absent (idempotent
    — an already-tidied session is a clean no-op) and append. The marker is written LAST: a crash between the
    summary appends and the marker re-files next sweep (a duplicate, never a loss — `forget` then logically
    retires the orphaned pass from recall, leaving it recoverable in the ledger). Lock contention => a clean
    no-op this boot (the sweep retries next session); it NEVER writes lock-free."""
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
        _, marked = _scan_sessions(cwd)
        if session_id in marked:
            return {"status": "already-consolidated", "stored": 0}
        ledger_file = ledger.ledger_path(cwd)
        batch = uuid.uuid4().hex   # ONE id for this whole pass, stamped on every episodic AND the marker below
        stored = 0
        for rec in records:
            ledger.append(_make_episodic(session_id, rec, batch), path=ledger_file)
            stored += 1
        # Marker LAST, carrying the SAME batch: a crash before it orphans this pass's episodics (their batch
        # unmarked) — the next sweep re-files and `forget` logically retires the orphans (favor duplicate over loss).
        ledger.append(_make_marker(session_id, batch), path=ledger_file)
        index.rebuild(ledger_file=ledger_file, index_file=index.index_path(cwd))
        return {"status": "ok", "stored": stored}
    finally:
        capture._release_lock(lock_fd)


# --- Memory's own hooks (SessionStart sweep + PreCompact compaction trigger) -------------------

def _consolidation_directive(pending: list) -> str:
    """The directive the SessionStart sweep injects when earlier sessions need tidying. It stays SUBORDINATE to
    the operator's request (memory's operation, not orientation — boot/README: never a first-turn hijack) and
    ACTIVE (done THIS session — not "whenever, or never"; that passivity is what left 21 sessions untidied). It is
    a QUIET pass: the model does NOT announce the tidy-up to the operator — UNLESS the backlog has grown to
    `_BACKLOG_ALARM_THRESHOLD` or more (a sign the silent tidy has stalled), when it breaks silence with ONE plain
    line (a COUNT, never the id codes) so a silent failure can't hide. The ids are listed for the model's
    `read`/`store` only. Memory's own hook composes this prose; the detection leaf above renders nothing."""
    n = len(pending)
    shown = pending[:_MAX_DIRECTIVE_IDS]
    ids = ", ".join(shown) + (f", and {n - len(shown)} more" if n > len(shown) else "")
    sessions = "1 earlier session" if n == 1 else f"{n} earlier sessions"
    have = "has" if n == 1 else "have"
    behind = (
        f" The backlog has fallen behind ({sessions}), which usually means this quiet tidy-up has been getting "
        "skipped — so this once, break the silence: at the first natural pause, tell the operator in plain words "
        "that memory upkeep has fallen behind and you're catching it up now. Give them only that COUNT, never "
        "the id codes or any internal terms."
    ) if n >= _BACKLOG_ALARM_THRESHOLD else ""
    return (
        "Memory upkeep — a QUIET background pass: do it DURING this session, but ALWAYS after you have served the "
        "operator's current request (never before it, never a first-turn hijack). Do not announce it — say "
        "nothing to the operator about tidying memory unless they ask (if asked, a count is enough; the session "
        "ids stay internal)." + behind + " "
        f"{sessions} {have} raw turn-notes that were never tidied into summaries. At the first natural pause, "
        "once the operator's current request is served, tidy each one. The sessions are: "
        f"{ids}. For each, run "
        "`.engine/.venv/bin/python .engine/tools/memory/consolidate.py read <session-id>` to read its raw "
        "notes, write a short compact summary of each meaningful thread (what was explored; what was decided "
        "or rejected, and why; what the operator prefers or learned), choose ONE label per summary from "
        f"{{{', '.join(ROLE_VOCABULARY)}}}, and store them with "
        "`… consolidate.py store <session-id>` (a JSON array of {\"role\": …, \"text\": …} on stdin). "
        "This is reflection, not re-litigation — be terse. The operator's request always comes first; keep it "
        "a silent pass, not an interruption — but do it this session, not someday."
    )


def _session_start_handler(payload) -> dict:
    """Memory's ONE SessionStart behavior: inject ONE combined directive carrying BOTH maintenance
    backlogs — untidied raw notes (consolidation, 3b) and clusters of old episodes ready to roll up (roll-up,
    slice 5 PR 3, via the lazy `rollup` import) — or proceed silently when neither is pending (the self-
    interference floor: this fires on the operator's OWN sessions, so a nothing-pending start must add nothing).
    ONE injection, not two competing ones, keeps the operator's first turn unsplit. FINE-GRAINED fail-open: a
    roll-up fault degrades to consolidation-only (it can never drop the older, more important consolidation
    directive); run_hook fail-opens the whole handler on any other fault."""
    live = payload.get("session_id") if isinstance(payload, dict) else None
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
    the "tolerable moment, never the hot path" the design names for the expensive step (memory/README) and, IF
    enough reclaimable waste has piled up, folds-and-prunes it (slice 5 PR 3). `maybe_compact` is fail-open (never
    raises); the compaction it rides folds Layer-1 bookkeeping AND, since slice 4e, physically removes any record an
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
        for sid in detect_unconsolidated(os.environ.get(SESSION_ENV)):
            print(sid)
        return 0
    if cmd == "read":
        if len(argv) < 2:
            print("usage: consolidate.py read <session-id>", file=sys.stderr)
            return 2
        deltas = read_deltas(argv[1])
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
        print(f"    label:   {out['role']}")
        print(f"    summary: {out['text']}")
    print("  => The filing around the AI's summary works (stored, labelled, readable back). Whether the")
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

    print("\nPART 4 — once a session is tidied, it is never tidied again")
    print("-" * 80)
    ledger.append(capture._make_record(session_c, 0, "user", "The login page logs people out after thirty minutes."))
    before = detect_unconsolidated(live_session_id=session_live)
    print(f"  Sessions still needing tidy-up (before): {before}")
    store_episodic(session_c, [{"role": "lesson",
                                "text": "The thirty-minute logout was a too-short session-timeout setting."}])
    after = detect_unconsolidated(live_session_id=session_live)
    print(f"  Sessions still needing tidy-up (after tidying '{session_c}'): {after}")
    print(f"  => '{session_c}' dropped off the list (a marker records it as done) — the backlog only shrinks.")

    print("\nPART 5 — a past session that was never tidied is detected (e.g. one closed without a clean finish)")
    print("-" * 80)
    ledger.append(capture._make_record(session_d, 0, "user", "Remember: the spare key is under the blue pot."))
    ledger.append(capture._make_record(session_live, 0, "user", "I'm working in this session right now."))
    pending = detect_unconsolidated(live_session_id=session_live)
    print(f"  The session you are in now is '{session_live}' (it has notes too, but is still being written).")
    print(f"  Sessions detected as needing tidy-up: {pending}")
    print(f"    -> '{session_d}' (a past session, never tidied) IS listed; the live "
          f"'{session_live}' is NOT.")
    print("\n  The directive the engine would hand the AI at the next session start (a SILENT pass — no")
    print("  announcement; the AI just tidies quietly at a pause):\n")
    for line in _wrap(_consolidation_directive(pending), 76):
        print(f"    | {line}")
    behind_demo = [f"stalled-{i:02d}" for i in range(_BACKLOG_ALARM_THRESHOLD + 1)]
    print(f"\n  And the SAME directive once the backlog has stalled past the alarm threshold "
          f"({_BACKLOG_ALARM_THRESHOLD}+ untidied")
    print("  sessions) — it breaks silence with ONE plain line so a silent failure can't hide:\n")
    for line in _wrap(_consolidation_directive(behind_demo), 76):
        print(f"    | {line}")
    print("\n  => A past, never-tidied session is caught by the same sweep; the AI receives the directive above")
    print("     and does the tidy-up THIS session, right after your request — SILENTLY, unless the backlog has")
    print("     fallen behind, when it surfaces one plain line (a count, never the id codes). (Whether it then")
    print("     writes a good summary is the part judged live.)")
    # The mechanical invariants this practice run proves (NOT the AI's wording, which is judged live): a
    # summary stored+findable, the label kept out of search text, a tidied session dropping off the backlog,
    # and a past untidied session detected while the live one is not.
    return (bool(out) and ok and session_c in before and session_c not in after
            and session_d in pending and session_live not in pending)


def _wrap(text: str, width: int) -> list:
    """A tiny word-wrap so the printed directive is legible (stdlib textwrap, kept local to the demo)."""
    import textwrap
    return textwrap.wrap(text, width=width)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
