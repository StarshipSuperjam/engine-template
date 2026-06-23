"""capture.py — ambient turn-delta capture: the content half of the memory substrate (slice 3a).

The locked design (engine-planning systems/cognitive/memory + lifecycle/close) splits memory capture
along a "content survives / reflection defers" seam. This module is the CONTENT half:

  - **Every completed turn (`Stop`) appends the turn's session-id-tagged delta to the ledger** — an
    *append, not a summarization*, so it never taxes mid-session use. The expensive AI-judged
    consolidation into clean, role-typed episodic records is the REFLECTION half (slice 3b), deferred
    because it needs the in-context AI's judgment, which a fire-and-forget hook does not have.

  - **Capture is cheap, generous, and LOSSLESS.** A long turn is *chunked* (paragraph-preferred, 4 KB)
    and every chunk is stored — content is never elided at capture time. Curation/compression is the
    later reflection step's job. "Raw deltas are already in the ledger" is the durability promise this
    keeps: once a turn finishes, its notes cannot be lost, even on an ungraceful exit.

  - **This module is a LEAF.** It writes the ledger and RETURNS a small report; it emits no
    operator-facing prose at runtime and never raises into its caller. `capture_turn_delta` is the
    public entry the [close] turn-hook's pre-built ambient-capture relay calls
    (`import memory; memory.capture_turn_delta(payload)`); lighting it up here flips that dormant seam
    from a no-op to real capture with zero edits to close. Close only *triggers* capture and never
    gates it; memory owns the mechanism.

  - **Fail-soft + race-safe by construction.** The whole body is wrapped so any fault is a clean
    no-op (it can never block or break a turn). The per-session cursor + the entire read/append/advance
    transaction are held under ONE bounded, NON-blocking advisory lock, so two worktree sessions
    sharing the one ledger can never double-file a delta, and a stuck lock can never stall turn-end
    (on contention it gives up after ~1s and the delta is caught at the next Stop). Write-safety across
    the per-session appends is the ledger-integrity law (serialized writes), not hook ordering.

The record SHAPE established here (and the per-record `v` version envelope the slice-1 ledger left as a
forward-owe) is record-kind `"turn-delta"`; the closed memory *role* vocabulary attaches to the
`"episodic"` records the reflection step adds, not to raw turn-deltas. stdlib-only; runs on the venv
python alongside close.
"""

from __future__ import annotations

import json
import os
import sys
import time

try:
    import fcntl  # POSIX advisory locking (macOS dev + ubuntu CI); absent on Windows.
    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover - the engine targets POSIX; degrade rather than crash.
    _HAVE_FCNTL = False

# Make the `memory` package importable whether we are imported as `memory.capture` (close's relay) or
# run as a script (the demo): put `.engine/tools` on the path, then import the sibling ledger module.
_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from memory import ledger, records  # noqa: E402

RECORD_VERSION = 1                       # the per-record ledger-version envelope (slice-1 forward-owe)
RECORD_KIND = "turn-delta"
CURSOR_FILENAME = "capture-state.json"   # {session_id: captured-message-count}; gitignored sibling
LOCK_FILENAME = ".capture.lock"          # the capture transaction lock; gitignored sibling

CHUNK_MAX_CHARS = 4_000                  # per-record body cap (paragraph-preferred, LOSSLESS split)
MAX_TRANSCRIPT_BYTES = 64 * 1024 * 1024  # 64 MiB hard ceiling; refuse a larger transcript

TRANSCRIPT_DIR_ENV = "ENGINE_MEMORY_TRANSCRIPT_DIR"  # adopter/test escape hatch (an ADDITIONAL root)
SESSION_ENV = "CLAUDE_CODE_SESSION_ID"   # the live platform session var (matches consolidate.py); the env
                                         # fallback used only when the hook payload omits `session_id`
TRANSCRIPT_ENV = "CLAUDE_TRANSCRIPT_PATH"

_LOCK_ATTEMPTS = 20      # × interval => ~1s bound; on contention, a clean no-op (caught next Stop)
_LOCK_INTERVAL = 0.05


# --- The transcript delta (lossless) ----------------------------------------------------------

def chunk_text(text: str, max_chars: int = CHUNK_MAX_CHARS) -> list:
    """Split text into <=max_chars chunks, preferring paragraph then line boundaries. This SPLITS,
    never drops: every character of the input lands in exactly one chunk (lossless by construction).

    Walks the string by an advancing offset (O(n)) rather than re-slicing the tail each iteration
    (which is O(n^2) on a multi-megabyte boundary-free message — that runs under the capture lock at
    turn-end, so the linear walk keeps a huge paste from stalling the turn)."""
    text = text.strip()
    if not text:
        return []
    n = len(text)
    if n <= max_chars:
        return [text]
    chunks = []
    start = 0
    while n - start > max_chars:
        window = text[start:start + max_chars]
        cut = window.rfind("\n\n")
        if cut < max_chars // 4:
            cut = window.rfind("\n")
        if cut < max_chars // 4:
            cut = max_chars
        chunk = text[start:start + cut].rstrip()
        if chunk:
            chunks.append(chunk)
        start += cut
        while start < n and text[start].isspace():  # drop the boundary whitespace (mirrors the old lstrip)
            start += 1
    tail = text[start:].strip()
    if tail:
        chunks.append(tail)
    return chunks


def _extract_records(transcript_path: str) -> list:
    """Parse the transcript JSONL one line per record, tolerating a malformed line individually."""
    records = []
    with open(transcript_path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if isinstance(rec, dict):
                records.append(rec)
    return records


def _is_message(rec: dict) -> bool:
    """A conversation message line (vs a queue-operation/attachment/etc.). Tolerant of shape: the
    confirmed Claude Code transcript keys messages by top-level `type`, but a `message` dict is also
    accepted so an older/other harness shape still captures."""
    return rec.get("type") in ("user", "assistant") or isinstance(rec.get("message"), dict)


def _message_text(rec: dict):
    """Best-effort text extraction across plausible transcript shapes (string content, or a list of
    content blocks each carrying `text` — the assistant tool-use shape — joined; tool args are skipped)."""
    msg = rec.get("message")
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [b.get("text") for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)]
            if parts:
                return "\n".join(parts)
    if isinstance(rec.get("content"), str):
        return rec["content"]
    if isinstance(rec.get("text"), str):
        return rec["text"]
    return None


def _speaker(rec: dict) -> str:
    msg = rec.get("message")
    if isinstance(msg, dict) and isinstance(msg.get("role"), str):
        return msg["role"]
    if isinstance(rec.get("role"), str):
        return rec["role"]
    if rec.get("type") in ("user", "assistant"):
        return rec["type"]
    return "unknown"


# --- Transcript-path safety (defense-in-depth) ------------------------------------------------

def _allowed_roots(cwd=None) -> list:
    """Directory roots a transcript_path may resolve under. `~/.claude/` is the primary (Claude Code's
    default); the shared clone root is belt-and-suspenders (in-repo test fixtures); the env override is
    an ADDITIONAL root, never a bypass of the checks below."""
    roots = [os.path.realpath(os.path.join(os.path.expanduser("~"), ".claude"))]
    root = ledger._git_common_root(cwd)
    if root:
        roots.append(os.path.realpath(root))
    override = os.environ.get(TRANSCRIPT_DIR_ENV)
    if override:
        roots.append(os.path.realpath(os.path.expanduser(override)))
    return roots


def _validate_transcript_path(path_str: str, cwd=None):
    """Reject traversal / wrong-suffix / out-of-scope / missing / oversized; else the resolved path."""
    raw = os.path.expanduser(path_str)
    if ".." in raw.replace("\\", "/").split("/"):
        return None
    resolved = os.path.realpath(raw)
    if os.path.splitext(resolved)[1] not in (".jsonl", ".json"):
        return None
    under = False
    for root in _allowed_roots(cwd):
        try:
            if os.path.commonpath([resolved, root]) == root:
                under = True
                break
        except ValueError:
            continue
    if not under:
        return None
    if not os.path.isfile(resolved):
        return None
    try:
        if os.path.getsize(resolved) > MAX_TRANSCRIPT_BYTES:
            return None
    except OSError:
        return None
    return resolved


# --- The cursor (per-session captured-message count) ------------------------------------------

def _read_cursor(data_dir: str, session_id: str) -> int:
    """The count of messages already captured for this session; 0 if missing/corrupt (benign
    re-capture). Read inside the capture lock, so no torn-read race."""
    path = os.path.join(data_dir, CURSOR_FILENAME)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            state = json.load(fh)
        val = state.get(session_id, 0) if isinstance(state, dict) else 0
    except (OSError, ValueError):
        return 0
    return val if isinstance(val, int) and val >= 0 else 0


def _write_cursor(data_dir: str, session_id: str, count: int) -> None:
    """Monotonically advance this session's cursor (only ever forward). Written atomically (temp +
    os.replace) inside the capture lock."""
    path = os.path.join(data_dir, CURSOR_FILENAME)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            state = json.load(fh)
        if not isinstance(state, dict):
            state = {}
    except (OSError, ValueError):
        state = {}
    prev = state.get(session_id, 0)
    if not (isinstance(prev, int) and prev >= 0):
        prev = 0
    state[session_id] = max(prev, count)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, separators=(",", ":"))
    os.replace(tmp, path)


def _acquire_lock(lock_path: str):
    """Acquire the capture transaction lock, NON-blocking with a bounded ~1s retry. Returns the held
    fd, or None on contention (=> a clean no-op; the delta is caught at the next Stop). Bounding the
    wait is what guarantees capture can never stall turn-end behind a stuck holder."""
    if not _HAVE_FCNTL:  # pragma: no cover - POSIX target; no cross-process lock available
        try:
            return os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o644)
        except OSError:
            return None
    for attempt in range(_LOCK_ATTEMPTS):
        fd = None
        try:
            fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o644)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            if fd is not None:
                os.close(fd)
            if attempt < _LOCK_ATTEMPTS - 1:
                time.sleep(_LOCK_INTERVAL)
    return None


def _release_lock(fd) -> None:
    try:
        if _HAVE_FCNTL:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _make_record(session_id: str, seq: int, speaker: str, text: str) -> dict:
    """The turn-delta record envelope. `ts`/`seq` are INTEGERS on purpose: the derived index's
    record-text projection indexes only string leaves, so integers stay out of the search body. `id` is the
    stable, content-free record id minted at capture (slice 4b) — kept out of the search body too
    (index._NON_BODY_KEYS)."""
    return {
        "v": RECORD_VERSION,
        "kind": RECORD_KIND,
        records.RECORD_ID_KEY: records.new_record_id(),
        "session_id": session_id,
        "ts": int(time.time()),
        "seq": seq,
        "speaker": speaker,
        "text": text,
        "tags": ["transcript", "stop"],
    }


# --- The public capture entry (what close's relay calls) --------------------------------------

def capture_turn_delta(payload, *, cwd=None) -> int:
    """Append the completed turn's new transcript messages to the memory ledger. Returns the number of
    records appended. FAIL-SOFT: any fault — bad payload, missing/oversized/out-of-scope transcript,
    lock contention — is a clean no-op (returns 0) and NEVER raises into the caller. This is the
    mechanism close's ambient-capture relay triggers on every `Stop`."""
    try:
        return _capture(payload, cwd=cwd)
    except Exception:  # noqa: BLE001 — ambient capture never gates close; any failure is a no-op
        return 0


def _capture(payload, *, cwd) -> int:
    if not isinstance(payload, dict):
        return 0
    session_id = payload.get("session_id") or os.environ.get(SESSION_ENV)
    transcript_str = payload.get("transcript_path") or os.environ.get(TRANSCRIPT_ENV)
    if not session_id or not transcript_str:
        return 0
    transcript_path = _validate_transcript_path(transcript_str, cwd)
    if transcript_path is None:
        return 0

    data_dir = ledger.ledger_dir(cwd)
    os.makedirs(data_dir, exist_ok=True)
    lock_fd = _acquire_lock(os.path.join(data_dir, LOCK_FILENAME))
    if lock_fd is None:
        return 0  # contended ~1s; the delta is caught at the next Stop
    try:
        messages = [r for r in _extract_records(transcript_path) if _is_message(r)]
        cursor = _read_cursor(data_dir, session_id)
        delta = messages[cursor:]
        if not delta:
            return 0
        ledger_file = ledger.ledger_path(cwd)
        appended = 0
        for offset, rec in enumerate(delta):
            text = _message_text(rec)
            if not text or not text.strip():
                continue
            speaker = _speaker(rec)
            for chunk in chunk_text(text):
                ledger.append(_make_record(session_id, cursor + offset, speaker, chunk), path=ledger_file)
                appended += 1
        _write_cursor(data_dir, session_id, len(messages))
        return appended
    finally:
        _release_lock(lock_fd)


# --- Operator demonstration -------------------------------------------------------------------
# An operator-runnable walkthrough on a THROWAWAY practice cabinet (a temp folder), never real data.
# It exercises the REAL capture above — and, in Part 4, the REAL close relay — and reads the cabinet
# back so every claim is proven by recognizable words on screen, not asserted. Run it and vary the
# fake turns yourself:
#     uv run --directory .engine --frozen -- python tools/memory/capture.py demo

_DEMO_TURNS = [
    ("user", "Let's redesign the export so the nightly job writes a manifest before the upload."),
    ("assistant", "Good idea. I'll add a manifest step and make the pelican-feeding schedule configurable."),
    ("user", "Also the login page keeps logging people out after thirty minutes — please look into it."),
    ("assistant", "Found it: the session timeout was set to thirty minutes. I raised it and added a test."),
]


def _demo_transcript(path: str, turns) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for speaker, text in turns:
            fh.write(json.dumps({"type": speaker, "message": {"role": speaker, "content": text}}) + "\n")


def _demo_notes(query_text: str):
    from memory import index
    return [r.get("text", "") for r in index.query(query_text).records]


def _demo_excerpt(texts, needle: str, width: int = 64) -> str:
    """A short window around `needle` in the first matching note (so a long note prints legibly)."""
    for t in texts:
        i = t.find(needle)
        if i != -1:
            start = max(0, i - width)
            end = min(len(t), i + len(needle) + width)
            return ("…" if start else "") + t[start:end] + ("…" if end < len(t) else "")
    return "(not found)"


def _demo_distinctive_word(text: str) -> str:
    """A distinctive word taken FROM the turn text — so Part 1's search words always come from the actual
    turns. This is what keeps the 'vary it' instruction honest: edit _DEMO_TURNS and Part 1 still searches
    real words from them, never a stale hardcoded list that would print '(nothing found)'."""
    words = [w.strip(".,;:!?—-\"'()").lower() for w in text.split()]
    words = [w for w in words if w.isalpha() and len(w) >= 5]
    return max(words, key=len) if words else (text.split()[0].lower() if text.split() else "")


def _demo() -> int:
    import tempfile

    print("=" * 80)
    print("MEMORY — saving your turn-notes (a practice run on a throwaway filing cabinet)")
    print("=" * 80)
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["ENGINE_MEMORY_DIR"] = tmp            # the throwaway cabinet
        os.environ[TRANSCRIPT_DIR_ENV] = tmp             # allow the fake transcript under tmp
        transcript = os.path.join(tmp, "session.jsonl")
        session_id = "practice-session-1"
        payload = {"session_id": session_id, "transcript_path": transcript}

        print("\nPART 1 — your work is saved as you go")
        print("-" * 80)
        _demo_transcript(transcript, _DEMO_TURNS)
        n = capture_turn_delta(payload)
        print(f"  Filed {n} turn-notes from a {len(_DEMO_TURNS)}-message practice session.")
        seen = set()
        for _speaker, turn_text in _DEMO_TURNS[:3]:        # search words taken FROM the turns themselves
            word = _demo_distinctive_word(turn_text)
            if not word or word in seen:
                continue
            seen.add(word)
            hits = _demo_notes(word)
            print(f"    ask for \"{word}\"  ->  {hits[0] if hits else '(nothing found)'}")
        print("  => Each finished turn is in the cabinet and findable by its own words.")

        print("\nPART 2 — even a long, detailed turn is saved in full (the middle isn't snipped out)")
        print("-" * 80)
        long_turn = (
            "Here is a very long, detailed turn about the migration plan. "
            + "We went through a lot of back-and-forth detail here. " * 120
            + "THE-KEY-FACT: the production database password lives in the vault, never in the repo. "
            + "And then we kept going with even more detail before wrapping up. " * 120
            + "That was the end of a very long turn."
        )
        long_session = "practice-session-long"
        long_transcript = os.path.join(tmp, "long.jsonl")
        _demo_transcript(long_transcript, [("user", long_turn)])
        filed = capture_turn_delta({"session_id": long_session, "transcript_path": long_transcript})
        print(f"  Filed one {len(long_turn):,}-character turn (it became {filed} notes — split, not snipped).")
        print(f"  ask for a fact buried in the MIDDLE  ->  {_demo_excerpt(_demo_notes('THE-KEY-FACT'), 'THE-KEY-FACT')}")
        print("  => The buried fact is right there. A long turn is kept whole, so closing the window")
        print("     after a big turn loses nothing — not even the middle.")

        print("\nPART 3 — running it again adds nothing new")
        print("-" * 80)
        before = _demo_notes("the")
        again = capture_turn_delta(payload)
        after = _demo_notes("the")
        print(f"  Notes in the cabinet before re-running: {len(before)}")
        print(f"  Re-ran the save over the same finished turns; it filed {again} new notes.")
        print(f"  Notes in the cabinet after:            {len(after)}")
        print("  => The same finished turns are never filed twice.")
        print("     (If the engine were ever interrupted mid-save it might re-file a turn's notes —")
        print("      it would rather keep an extra copy than lose one.)")

        print("\nPART 4 — the engine files a note by itself when a turn ends")
        print("-" * 80)
        import close  # the REAL turn-close tool; its note-filing step was switched off until today
        new_session = "practice-session-2"
        new_transcript = os.path.join(tmp, "handoff.jsonl")
        _demo_transcript(new_transcript, [("user", "Remember this for me: the spare key is under the blue pot.")])
        before_handoff = _demo_notes("blue pot")
        close._trigger_ambient_capture({"session_id": new_session, "transcript_path": new_transcript})
        after_handoff = _demo_notes("blue pot")
        print(f"  Before the engine's own end-of-turn step ran:   {before_handoff or '(nothing there)'}")
        print(f"  After  (read straight back out of the cabinet):  {after_handoff[0] if after_handoff else '(still nothing!)'}")
        print("  => The step the engine runs at every turn-end really files a note (proven by reading")
        print("     it back out of the cabinet, not by trusting an 'it worked' message).")

        del os.environ["ENGINE_MEMORY_DIR"]
        del os.environ[TRANSCRIPT_DIR_ENV]

    print("\n" + "-" * 80)
    print("Reminder: that was a PRACTICE cabinet, thrown away when this demo ended. Your saved")
    print("notes are private, local, and deletable — never shipped or uploaded anywhere. In a real")
    print("project the cabinet starts empty. Tidying these raw notes into clean, labelled summaries")
    print("(and tidying up sessions that ended abruptly) is a separate step of the engine, and searching")
    print("your memory while you work is another; this demo just doesn't exercise them. It proves only that")
    print("notes are saved and can't be lost — even if you just close the window.")
    print("To vary it: edit _DEMO_TURNS at the top of this file and re-run.")
    ok = (n > 0 and filed > 0 and again == 0 and not before_handoff and bool(after_handoff))
    if not ok:
        print("\nDEMO UNEXPECTED: a practice turn did not file notes, a re-run was not a no-op, or the "
              "end-of-turn ambient capture did not save a note.", file=sys.stderr)
        return 1
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    print("usage: capture.py demo")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
