"""capture.py — ambient turn-delta capture: the content half of the memory substrate.

The locked design splits memory capture
along a "content survives / reflection defers" seam. This module is the CONTENT half:

  - **Every completed turn (`Stop`) appends the turn's session-id-tagged delta to the ledger** — an
    *append, not a summarization*, so it never taxes mid-session use. There used to be a second half — an
    AI-judged pass that folded each session into role-typed summaries — and it is gone: the transcript
    itself is the record now, and meaning is spent at read time by the session's own model
    rather than accumulated as summaries that go stale.

  - **Capture is cheap, generous, and LOSSLESS over conversation.** A long *turn* is *chunked*
    (paragraph-preferred, 4 KB) and every chunk is stored — conversational content is never elided at
    capture time; curation/compression is the later reflection step's job. What capture does NOT store is
    Claude Code's own transcript scaffolding — slash-command echoes, local-command output/caveats, and
    control sentinels (`_is_noise`) — because that plumbing is *not conversation*. Excluding it before the
    ledger is an "is this conversation at all" filter, NEVER the importance keep/discard gate the design
    forbids: the design bars gatekeeping on *worth* because worth is future-unknowable
    ("importance is a function of the future the capturing
    session cannot see"), whereas a harness wrapper's non-conversation status is knowable now and stable.
    "Raw deltas are already in the ledger" is the durability promise this keeps: once a turn finishes, its
    conversational notes cannot be lost, even on an ungraceful exit.

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

The record SHAPE established here (with the per-record `v` version envelope, which the ledger's own
format version does not cover) is record-kind `"turn-delta"`; the closed memory *role* vocabulary attaches to the
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

from memory import ledger, records, scrub  # noqa: E402

RECORD_VERSION = 1                       # stamped as `v` on each record: the shape it was written in. Nothing
                                         # reads it while only one shape has existed; a restore routes its
                                         # migration on ledger.LEDGER_FORMAT_VERSION, not on this.
RECORD_KIND = records.AMBIENT_CAPTURE_KIND   # the ambient-capture kind, now homed in `records` (the cycle-free
                                             # leaf `forget` also reads); aliased here so the string never drifts
CURSOR_FILENAME = "capture-state.json"   # {session_id: captured-message-count}; gitignored sibling
CAPTURE_TRANSACTION_FILENAME = ".capture-transaction.json"  # durable roll-forward journal
LOCK_FILENAME = ".capture.lock"          # the capture transaction lock; gitignored sibling

CHUNK_MAX_CHARS = 4_000                  # per-record body cap (paragraph-preferred, LOSSLESS split)
MAX_TRANSCRIPT_BYTES = 64 * 1024 * 1024  # 64 MiB hard ceiling; refuse a larger transcript

TRANSCRIPT_DIR_ENV = "ENGINE_MEMORY_TRANSCRIPT_DIR"  # adopter/test escape hatch (an ADDITIONAL root)
SESSION_ENV = "CLAUDE_CODE_SESSION_ID"   # the live platform session var (the shared convention); the env
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


# --- Harness-scaffolding filter ----------------------------------------------------------------
# Claude Code emits `type: user` transcript lines that are NOT conversation — slash-command echoes,
# local-command output/caveats, and control sentinels. Captured verbatim they poison recall (they
# rank as exact lexical matches) and inflate the raw-note count, so capture skips them. This is a
# CONSERVATIVE denylist of known-harness shapes, anchored to the START of the message (the whole
# message IS the wrapper), so a genuine turn that merely mentions a tag mid-sentence is never dropped.
_NOISE_TAG_PREFIXES = (
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<local-command-caveat>",
)
_NOISE_TEXT_PREFIXES = (
    "Caveat: The messages below were generated by",  # the post-/compact system caveat block
    "[Request interrupted by user",                  # an aborted turn (…] or …for tool use])
)
_NOISE_EXACT = frozenset({
    "No response requested.",
})


def _is_noise(text: str) -> bool:
    """True iff `text` is Claude Code harness scaffolding rather than conversation. Conservative: matches
    only known harness shapes, anchored at the message start, so genuine conversation is never dropped."""
    stripped = text.strip()
    if stripped in _NOISE_EXACT:
        return True
    return stripped.startswith(_NOISE_TAG_PREFIXES) or stripped.startswith(_NOISE_TEXT_PREFIXES)


# --- The Codex transcript recognizer (provider-routed; never the tolerant Claude parser) --------
# Codex's transcript format is EXPLICITLY unstable (its docs reserve the right to change it), so a
# Codex-tagged session parses ONLY through this dedicated recognizer and NEVER falls through to the
# tolerant Claude parser above — a partially-recognized transcript writing fragments into long-term
# memory is strictly worse than an honestly-empty capture. The recognizer captures fully
# or not at all: a transcript with no recognized record shapes reads as UNRECOGNIZED, the capture is
# a zero-record no-op, and the loud status marker (below) says so.

# The record `type` values the recognizer knows. A transcript whose records match NONE of these is an
# unrecognized (changed) format → refuse + status, never guess.
_CODEX_KNOWN_TYPES = frozenset({
    "session_meta", "turn_context", "response_item", "event_msg", "compacted", "message",
})
# Codex-side scaffolding wrappers (same conservative anchored-prefix doctrine as the Claude tuple,
# kept SEPARATE so the Claude tuple is untouched).
_CODEX_NOISE_PREFIXES = (
    "<environment_context>",
    "<user_instructions>",
    "<turn_aborted>",
)


def _codex_message(rec: dict):
    """One recognized Codex conversation message as a plain {'role','text'} dict, or None for a
    non-conversation record (reasoning, function calls, meta — the same 'is this conversation at
    all?' filter doctrine, never a worth judgment). Accepts the rollout envelope
    ({'type':'response_item','payload':{...}}) and a bare message record. Only a `message` payload with
    an explicit user/assistant role is a conversation turn: the newer multi-agent `agent_message` record
    is deliberately NOT captured — on the real corpus its dominant `event_msg` form is a byte-identical
    echo of the assistant `message` already captured here (capturing it would double-store every
    assistant turn), so recognizing it adds duplication, not conversation."""
    payload = None
    if rec.get("type") == "response_item" and isinstance(rec.get("payload"), dict):
        payload = rec["payload"]
    elif rec.get("type") == "message":
        payload = rec
    if not isinstance(payload, dict) or payload.get("type") not in ("message", None):
        return None
    role = payload.get("role")
    if role not in ("user", "assistant"):
        return None
    content = payload.get("content")
    if isinstance(content, str):
        parts = [content]
    elif isinstance(content, list):
        parts = [b.get("text") for b in content
                 if isinstance(b, dict) and isinstance(b.get("text"), str)]
    else:
        parts = []
    text = "\n".join(p for p in parts if p).strip()
    if not text or text.startswith(_CODEX_NOISE_PREFIXES):
        return None
    return {"role": role, "text": text}


def _codex_shape_detail(reason: str, recs: list) -> dict:
    """A CONTENT-FREE structural fingerprint of an unrecognized Codex transcript: which predicate refused
    it, the distinct record `type` and payload `type` names present, and the record count. It NEVER
    includes any message text — this is a diagnostic of a CHANGED FORMAT (so the next drift is
    actionable, not a bare 'unparseable'), never a capture of the transcript's content. Both the list
    length AND each individual type-name are bounded, so a pathological transcript (a huge type value, or
    thousands of distinct ones) cannot bloat the marker."""
    def _names(values):
        return sorted({str(v)[:64] for v in values})[:20]   # cap each name AND the list
    rtypes = _names(r.get("type") for r in recs if isinstance(r, dict))
    ptypes = _names(r["payload"].get("type") for r in recs
                    if isinstance(r, dict) and isinstance(r.get("payload"), dict))
    return {"reason": reason, "record_types": rtypes, "payload_types": ptypes,
            "record_count": len(recs)}


def _codex_messages(transcript_path: str):
    """(messages, recognized, detail): the conversation messages of a Codex transcript as plain
    {'role','text'} dicts, whether the transcript's format was recognized, and — when it was NOT — a
    content-free structural fingerprint (`_codex_shape_detail`) naming which check refused it, else None.
    recognized is False — the caller must surface it loudly instead of capturing fragments — in EVERY
    zero-yield shape a format change can take (the review-gate holes): a non-empty file that parses to no
    JSON records at all (the whole format moved off JSON lines); JSON records none of which carries a
    known Codex record type (the envelope changed); and known record types that yield NO conversation
    messages (the message payload shape changed inside a familiar envelope). A genuinely empty file is
    recognized-and-empty (nothing happened yet, nothing to say)."""
    recs = _extract_records(transcript_path)
    if not recs:
        try:
            non_empty = os.path.getsize(transcript_path) > 0
        except OSError:
            non_empty = False
        if non_empty:                  # bytes but no JSON records → the format changed → refuse, loudly
            return [], False, _codex_shape_detail("no-json-records", recs)
        return [], True, None          # genuinely empty → recognized-and-empty
    if not any(r.get("type") in _CODEX_KNOWN_TYPES for r in recs):
        return [], False, _codex_shape_detail("no-known-record-type", recs)
    messages = [m for m in (_codex_message(r) for r in recs) if m is not None]
    if not messages:                   # familiar envelope, zero conversation → payload shape changed
        return [], False, _codex_shape_detail("no-conversation-messages", recs)
    return messages, True, None


# --- Transcript-path safety (defense-in-depth) ------------------------------------------------

def _allowed_roots(cwd=None) -> list:
    """Directory roots a transcript_path may resolve under. `~/.claude/` and `~/.codex/` are the two
    runtime homes (each platform's default transcript territory — the payload's transcript_path is
    the source of truth, so no location inside them is ever hardcoded); the shared clone root is
    belt-and-suspenders (in-repo test fixtures); the env override is an ADDITIONAL root, never a
    bypass of the checks below."""
    home = os.path.expanduser("~")
    roots = [os.path.realpath(os.path.join(home, ".claude")),
             os.path.realpath(os.path.join(home, ".codex"))]
    root = ledger._git_common_root(cwd)
    if root:
        roots.append(os.path.realpath(root))
    override = os.environ.get(TRANSCRIPT_DIR_ENV)
    if override:
        roots.append(os.path.realpath(os.path.expanduser(override)))
    return roots


def _runtime_home_provider(transcript_path: str):
    """`"claude"` if the resolved transcript sits under `~/.claude`, `"codex"` if under `~/.codex`, or
    `None` for an AMBIGUOUS root — the shared clone root or the `ENGINE_MEMORY_TRANSCRIPT_DIR` override,
    where the transcript's own location does not decide its format. A transcript's runtime home is what
    a format actually is, regardless of a process-level provider signal, so this is consulted BEFORE
    `providers.detect`: it stops a leaked or stale ENGINE_PROVIDER from handing a Claude transcript to
    the Codex recognizer (StarshipSuperjam/engine-template#1193). The two homes are disjoint; an
    ambiguous root falls back to `providers.detect` unchanged. The returned tokens equal
    `providers.CLAUDE` / `providers.CODEX`."""
    home = os.path.expanduser("~")
    resolved = os.path.realpath(transcript_path)
    for name, provider in ((".claude", "claude"), (".codex", "codex")):
        root = os.path.realpath(os.path.join(home, name))
        try:
            if os.path.commonpath([resolved, root]) == root:
                return provider
        except ValueError:  # different drives on Windows, etc. — not under this home
            continue
    return None


def _validate_transcript_path(path_str: str, cwd=None):
    """Reject traversal / wrong-suffix / out-of-scope / missing / oversized. Returns
    `(resolved_path, None)` on success, `(None, reason)` on refusal — the reason is a fixed code
    (`traversal` / `suffix` / `out-of-scope` / `missing` / `oversized` / `size-unreadable`), never
    path or content text, so the capture-status marker can record WHY a path was refused (StarshipSuperjam/engine-template#774:
    `invalid-path` alone collapsed five distinct causes into one undiagnosable word)."""
    raw = os.path.expanduser(path_str)
    if ".." in raw.replace("\\", "/").split("/"):
        return None, "traversal"
    resolved = os.path.realpath(raw)
    if os.path.splitext(resolved)[1] not in (".jsonl", ".json"):
        return None, "suffix"
    under = False
    for root in _allowed_roots(cwd):
        try:
            if os.path.commonpath([resolved, root]) == root:
                under = True
                break
        except ValueError:
            continue
    if not under:
        return None, "out-of-scope"
    if not os.path.isfile(resolved):
        return None, "missing"
    try:
        if os.path.getsize(resolved) > MAX_TRANSCRIPT_BYTES:
            return None, "oversized"
    except OSError:
        return None, "size-unreadable"
    return resolved, None


# --- The cursor (per-session captured-message count) ------------------------------------------

def _capture_fault(_boundary: str) -> None:
    """No-op seam used by the deterministic crash-boundary matrix in the checked-in tests."""


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
        fh.flush()
        os.fsync(fh.fileno())
        _capture_fault("cursor-file-fsync")
    os.replace(tmp, path)
    _capture_fault("cursor-replace")
    directory = os.open(data_dir, os.O_RDONLY)
    try:
        os.fsync(directory)
        _capture_fault("cursor-directory-fsync")
    finally:
        os.close(directory)


def _capture_transaction_path(data_dir: str) -> str:
    return os.path.join(data_dir, CAPTURE_TRANSACTION_FILENAME)


def _write_capture_transaction(data_dir: str, document: dict) -> None:
    """Durably publish the exact roll-forward work before any ledger record lands."""
    path = _capture_transaction_path(data_dir)
    tmp = path + f".{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(document, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        _capture_fault("journal-file-fsync")
    os.replace(tmp, path)
    _capture_fault("journal-replace")
    directory = os.open(data_dir, os.O_RDONLY)
    try:
        os.fsync(directory)
        _capture_fault("journal-directory-fsync")
    finally:
        os.close(directory)


def _read_capture_transaction(data_dir: str):
    path = _capture_transaction_path(data_dir)
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise RuntimeError("capture transaction journal is unreadable") from exc
    if (not isinstance(value, dict) or set(value) != {"schema_version", "session_id", "cursor_after", "records"}
            or value.get("schema_version") != "capture-transaction.v1"
            or not isinstance(value.get("session_id"), str) or not value["session_id"]
            or not isinstance(value.get("cursor_after"), int) or isinstance(value["cursor_after"], bool)
            or value["cursor_after"] < 0 or not isinstance(value.get("records"), list)
            or any(not isinstance(record, dict) or not isinstance(record.get(records.RECORD_ID_KEY), str)
                   for record in value["records"])):
        raise RuntimeError("capture transaction journal has an invalid shape")
    return value


def _clear_capture_transaction(data_dir: str) -> None:
    try:
        os.unlink(_capture_transaction_path(data_dir))
    except FileNotFoundError:
        return
    _capture_fault("journal-unlink")
    directory = os.open(data_dir, os.O_RDONLY)
    try:
        os.fsync(directory)
        _capture_fault("journal-clear-directory-fsync")
    finally:
        os.close(directory)


def _recover_capture_transaction(data_dir: str, cwd=None, *, recovering: bool, routing=None) -> int:
    """Idempotently roll a prepared capture forward, then remove its journal last. `routing` is the
    content-free provider-routing record for the current capture; it is recorded on the `captured`
    marker so a misroute is visible after the fact. A crash-recovery rollforward (recovering=True) has
    no current routing and passes None — the marker then simply carries no routing that pass."""
    transaction = _read_capture_transaction(data_dir)
    if transaction is None:
        return 0
    ledger_file = ledger.ledger_path(cwd)
    existing_ids = {
        record.get(records.RECORD_ID_KEY)
        for record in ledger.read(path=ledger_file).records if isinstance(record, dict)
    }
    appended = []
    for record in transaction["records"]:
        if record[records.RECORD_ID_KEY] in existing_ids:
            continue
        ledger.append(record, path=ledger_file)
        _capture_fault("ledger-append")
        appended.append(record)
        existing_ids.add(record[records.RECORD_ID_KEY])
    _write_cursor(data_dir, transaction["session_id"], transaction["cursor_after"])
    from memory import index  # lazy: index -> forget -> capture
    index_file = index.index_path(cwd)
    if recovering:
        # A prior process may have died at any point around incremental indexing. A full atomic rebuild is the
        # only idempotent repair that cannot duplicate rows or leave a current-looking hole.
        index.rebuild(ledger_file=ledger_file, index_file=index_file)
    elif transaction["records"]:
        index.extend(transaction["records"], ledger_file=ledger_file, index_file=index_file)
    _capture_fault("index-apply")
    _write_capture_status("captured", transaction["session_id"], required=True,
                          detail=({"routing": routing} if routing is not None else None))
    _capture_fault("required-status")
    _clear_capture_transaction(data_dir)
    return len(appended)


def _restore_quarantine_present(transaction_path: str) -> bool:
    """Fail closed for every directory entry, including dangling symlinks and unreadable paths."""
    try:
        os.lstat(transaction_path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _acquire_lock(lock_path: str, *, allow_restore_quarantine: bool = False, deadline=None):
    """Acquire the capture transaction lock, NON-blocking with a bounded ~1s retry. Returns the held
    fd, or None on contention (=> a clean no-op; the delta is caught at the next Stop). Bounding the
    wait is what guarantees capture can never stall turn-end behind a stuck holder."""
    _mutation_authority.authorize_nested("capture-lock-create", measured_cardinality=1)
    transaction_path = os.path.join(os.path.dirname(lock_path), ledger.RESTORE_TRANSACTION_FILENAME)
    if not allow_restore_quarantine and _restore_quarantine_present(transaction_path):
        return None
    if not _HAVE_FCNTL:  # pragma: no cover - POSIX target; no cross-process lock available
        try:
            return os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o644)
        except OSError:
            return None
    for attempt in range(_LOCK_ATTEMPTS):
        # Recheck on every retry. A restore may create its quarantine marker
        # while this writer is waiting on the same lock.
        if not allow_restore_quarantine and _restore_quarantine_present(transaction_path):
            return None
        if deadline is not None and time.monotonic() >= deadline:
            return None
        fd = None
        try:
            fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o644)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # The pre-lock check closes the common case; this check closes the
            # check-then-lock race. A restore can create quarantine while this
            # writer waits, then release the lock with the journal still live.
            if (not allow_restore_quarantine
                    and _restore_quarantine_present(transaction_path)):
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
                fd = None
                return None
            return fd
        except BaseException as exc:
            if fd is not None:
                os.close(fd)
            if not isinstance(exc, OSError):
                raise
            if attempt < _LOCK_ATTEMPTS - 1:
                delay = _LOCK_INTERVAL if deadline is None else min(
                    _LOCK_INTERVAL, max(0.0, deadline - time.monotonic()))
                if delay <= 0:
                    return None
                time.sleep(delay)
    return None


def _release_lock(fd) -> None:
    try:
        if _HAVE_FCNTL:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# --- The in-flight-migration marker (compaction refuses within a migration window) -------------
# The compaction↔provisioning ordering law: the single-writer lock serializes individual
# writes but does NOT order a whole compaction against a separate migration's snapshot+mutation (each a distinct
# critical section). So a migration raises an in-flight marker for its duration and compaction refuses within it.
# The marker is a FILE (written then the lock released), NOT a held lock: the migration's own snapshot reads the
# ledger lock-free (backup_vault.snapshot_for_migration), and a long migration must never hold the single-writer
# lock and stall every turn-capture. The marker carries the migrating PID + a wall-clock start so an orphaned
# marker (a process that died mid-migration) is recoverable — a migration is a bounded synchronous run, never
# "parked", so wall-clock is a sound orphan bound HERE (unlike the lease's sessions-since metric).

MIGRATION_MARKER_FILENAME = "migration-in-flight.json"   # {"pid": int, "started_at": float}; gitignored sibling
MIGRATION_ORPHAN_CEILING_S = 3600     # 1h — far above any real memory migration; a wall-clock orphan backstop


def _marker_path(data_dir: str) -> str:
    return os.path.join(data_dir, MIGRATION_MARKER_FILENAME)


def _read_marker(data_dir: str):
    """The migration marker dict, or None if absent/unreadable/malformed (fail-safe: a marker we can't trust
    is treated as absent so it can never wedge compaction shut on its own)."""
    try:
        with open(_marker_path(data_dir), "r", encoding="utf-8") as fh:
            marker = json.load(fh)
    except (OSError, ValueError):
        return None
    return marker if isinstance(marker, dict) else None


def _pid_alive(pid) -> bool:
    """Is `pid` a live process? Errs toward ALIVE on any uncertainty (so we never clear a marker we aren't sure
    is orphaned): only a definitive ProcessLookupError (no such process) counts as dead."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:              # exists, owned by another user => alive
        return True
    except OSError:                      # unknown => assume alive (safe)
        return True


def _marker_orphaned(marker: dict, now=None) -> bool:
    """A marker is CONFIDENTLY orphaned only when its process is definitively gone OR its wall-clock age far
    exceeds any real migration. Anything uncertain reads as live, so compaction defers rather than risk
    interleaving a running migration."""
    now = time.time() if now is None else now
    pid_dead = not _pid_alive(marker.get("pid"))
    started = marker.get("started_at")
    too_old = isinstance(started, (int, float)) and (now - started) > MIGRATION_ORPHAN_CEILING_S
    return pid_dead or too_old


def open_migration_window(data_dir: str) -> bool:
    """Raise the in-flight-migration marker for a migration about to snapshot+mutate the store. Acquires the
    lock, atomically writes the marker (this PID + now), and RELEASES the lock immediately (the marker persists
    as a file a later compaction still sees; holding the lock across the whole migration would stall every
    turn-capture). **Fails CLOSED**: returns False if the lock can't be had — the caller must then REFUSE the
    migration rather than run it unguarded (a marker-less migration is exactly the interleave this prevents)."""
    try:
        os.makedirs(data_dir, exist_ok=True)
        lock_fd = _acquire_lock(os.path.join(data_dir, LOCK_FILENAME))
        if lock_fd is None:
            return False                 # fail closed: no marker => caller refuses the migration
        try:
            path = _marker_path(data_dir)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"pid": os.getpid(), "started_at": time.time()}, fh, separators=(",", ":"))
            os.replace(tmp, path)
            return True
        finally:
            _release_lock(lock_fd)
    except OSError:
        return False                     # fail closed on any write fault


def close_migration_window(data_dir: str) -> None:
    """Lower the marker when a migration finishes. Acquires the lock (its own ~1s bounded retry), removes the
    marker, releases; idempotent and best-effort. If it can't remove (transient contention), the marker lingers
    carrying THIS process's PID — so it is NOT recovered by the orphan path until this process exits (PID dies)
    or the wall-clock ceiling elapses. For the short-lived `module_manager` migration run that is ~immediate;
    only a long-lived host process could hold recovery off for up to the ceiling."""
    try:
        lock_fd = _acquire_lock(os.path.join(data_dir, LOCK_FILENAME))
        if lock_fd is None:
            return
        try:
            os.remove(_marker_path(data_dir))
        except OSError:
            pass                         # already gone / unremovable => the orphan path recovers it
        finally:
            _release_lock(lock_fd)
    except OSError:
        pass


def migration_in_flight(data_dir: str) -> bool:
    """True iff a migration marker is present AND not confidently orphaned — the guard compaction checks (under
    its own lock) to refuse. An orphaned marker (dead PID / past the ceiling) reads False so a crashed migration
    never wedges compaction shut forever."""
    marker = _read_marker(data_dir)
    return marker is not None and not _marker_orphaned(marker)


def clear_orphaned_migration_locked(data_dir: str) -> bool:
    """Clear the marker IFF it is confidently orphaned. The CALLER MUST HOLD the lock (compact calls this after
    acquiring it, to self-heal a crashed migration and resume). Returns True if it cleared one. A live marker is
    left untouched (its migration is still running)."""
    marker = _read_marker(data_dir)
    if marker is None or not _marker_orphaned(marker):
        return False
    try:
        os.remove(_marker_path(data_dir))
    except OSError:
        return False
    return True


def reap_orphaned_migration(data_dir: str) -> bool:
    """Acquire the lock and clear an orphaned marker (a self-acquiring wrapper over the *_locked form). Best-effort:
    a cheap lock-free pre-check first, so the common no-marker case never touches the lock. This is what lets the
    orphan recovery ride EVERY `maybe_compact` (its `PreCompact` cadence) instead of only a fold that clears enough
    waste — so a crashed migration's boot heads-up clears on the next tidy, not only once the ledger is dirty
    enough to compact. Returns True iff it cleared one. Never raises."""
    try:
        if _read_marker(data_dir) is None:
            return False                     # no marker: skip the lock entirely (the overwhelmingly common case)
        lock_fd = _acquire_lock(os.path.join(data_dir, LOCK_FILENAME))
        if lock_fd is None:
            return False                     # contended: the next pass reaps it
        try:
            return clear_orphaned_migration_locked(data_dir)
        finally:
            _release_lock(lock_fd)
    except OSError:
        return False


def detect_orphaned_migration(data_dir: str):
    """For boot's read-only heads-up: the marker dict IF a migration marker is present AND orphaned (a migration
    that didn't finish), else None. Read-only — the actual clear happens under compact's lock (self-heal)."""
    marker = _read_marker(data_dir)
    return marker if (marker is not None and _marker_orphaned(marker)) else None


def _make_record(session_id: str, seq: int, speaker: str, text: str, *, injected: bool = False,
                 origin: "str | None" = None) -> dict:
    """The turn-delta record envelope. `ts`/`seq` are INTEGERS on purpose: the derived index's
    record-text projection indexes only string leaves, so integers stay out of the search body. `id` is the
    stable, content-free record id minted at capture — kept out of the search body too
    (index._NON_BODY_KEYS). `injected` adds `records.INJECTED_TAG` so the consolidation sweep skips a
    harness-injected pseudo-turn as fuel (issue StarshipSuperjam/engine-template#274) — the record still lands and stays fully recoverable;
    the tag (like every tag) is kept out of the search body. That tag now also keeps the pseudo-turn out of
    RECALL: genuine turns are recall content, so this tag is what separates the operator's words from the
    harness's."""
    tags = ["transcript", "stop"]
    if injected:
        tags.append(records.INJECTED_TAG)
    if origin:
        # Provenance for a record captured LATE, by the session-start drain rather than at its own Stop. A
        # reader deserves to know a note was recovered from a transcript afterwards rather than filed live.
        tags.append(origin)
    return {
        "v": RECORD_VERSION,
        "kind": RECORD_KIND,
        records.RECORD_ID_KEY: records.new_record_id(),
        "session_id": session_id,
        "ts": int(time.time()),
        "seq": seq,
        "speaker": speaker,
        "text": text,
        "tags": tags,
    }


# --- The capture-status marker (loud degradation) ----------------------------------------------
# The one intended Claude-side behavioral delta of the dual-runtime work: capture used to no-op
# SILENTLY on a fault. Now every capture attempt records its outcome to a gitignored marker —
# captured / no-transcript / invalid-path / unparseable — which boot renders as one plain dashboard
# line when the previous session's conversation could not be saved, and telemetry's inbox drain
# promotes a persistently failing marker to one tracked finding. EVERY failing state records a
# CONTENT-FREE structural `detail` (StarshipSuperjam/engine-template#774): `unparseable` keeps its Codex shape fingerprint; the Claude
# states carry which field was absent, the path-validation reason code, or the exception class name
# only — never any message text, path text, or exception message. Best-effort: a marker write failure
# never disturbs the capture or the turn.
#
# The marker keeps only the LAST outcome, so a success overwrites the failure that preceded it — which
# made an INTERMITTENT failure undiagnosable after the fact (StarshipSuperjam/engine-template#774). Every failing write therefore also
# appends to a small rolling history file beside the marker (newest MAX_FAILURE_HISTORY kept), swapped
# in atomically (write-temp + os.replace) so a reader never sees a torn file. Concurrent failing
# writers can still last-writer-win a just-appended line (the marker itself has the same posture);
# bounded loss in a best-effort diagnostic is accepted — the file guides a human/session diagnosing,
# and is NEVER an input to any health or clearance decision. A reader should skip any malformed line.

CAPTURE_STATUS_STATES = ("captured", "no-transcript", "invalid-path", "unparseable", "failed")
_ENGINE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CAPTURE_STATUS_PATH = os.path.join(_ENGINE_DIR, "telemetry", ".cache", "memory-capture.status")
CAPTURE_FAILURES_PATH = os.path.join(_ENGINE_DIR, "telemetry", ".cache", "memory-capture-failures.ndjson")
MAX_FAILURE_HISTORY = 20


def redirect_health_paths(directory: str) -> tuple:
    """Point the capture-status marker and its failure history at *directory* and return the previous
    (status, failures) pair so a caller can restore them with `restore_health_paths`.

    This is a TEST SEAM and nothing else. A self-test run drives the real capture path, and without a
    redirect its status/failure writes land on the PRODUCTION cache files this project reads for its own
    memory-capture health — so a green suite silently latches a false "capture is failing" signal
    (StarshipSuperjam/engine-template#1193). Every test that drives capture calls this, and a repo-wide
    guard (`test_launch_contract.TestCaptureHermeticity`) fails the build if one does not. It moves ONLY
    where the two sidecars live; it is never consulted by any capture, health or clearance decision, and
    NO production code path calls it — hermeticity is a property of the tests, never a runtime "am I under
    test" guess (the rejected alternative). `_health_path` reads these module globals at call time when no
    persistent execution context is installed, which is the case under the test suite."""
    global CAPTURE_STATUS_PATH, CAPTURE_FAILURES_PATH
    saved = (CAPTURE_STATUS_PATH, CAPTURE_FAILURES_PATH)
    os.makedirs(directory, exist_ok=True)
    CAPTURE_STATUS_PATH = os.path.join(directory, "memory-capture.status")
    CAPTURE_FAILURES_PATH = os.path.join(directory, "memory-capture-failures.ndjson")
    return saved


def restore_health_paths(saved: tuple) -> None:
    """Restore the (status, failures) pair returned by `redirect_health_paths`."""
    global CAPTURE_STATUS_PATH, CAPTURE_FAILURES_PATH
    CAPTURE_STATUS_PATH, CAPTURE_FAILURES_PATH = saved


def _health_path(key: str, fallback: str) -> str:
    if "ENGINE_PERSISTENT_EXECUTION_CONTEXT" not in os.environ:
        return fallback
    from memory import execution_context as _execution_context
    return _execution_context.current_context()["target"]["lifecycle"][key]


def _append_failure_history(record: dict) -> None:
    """Append one failing outcome to the rolling history and trim to the newest MAX_FAILURE_HISTORY,
    atomically (temp file + os.replace, pid-suffixed so concurrent writers never share a temp; a
    crash between write and replace can orphan one stale `.tmp` per pid — bounded litter in a
    gitignored cache, cleaned the next time that pid number recurs, never read by anything).
    Best-effort by the marker's own contract: any OSError is swallowed and the capture is undisturbed."""
    failures_path = _health_path("capture_failures", CAPTURE_FAILURES_PATH)
    try:
        os.makedirs(os.path.dirname(failures_path), exist_ok=True)
        lines = []
        try:
            with open(failures_path, encoding="utf-8") as fh:
                lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        except OSError:
            lines = []
        lines.append(json.dumps(record))
        tmp = f"{failures_path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines[-MAX_FAILURE_HISTORY:]) + "\n")
        os.replace(tmp, failures_path)
    except OSError:
        pass


def _write_capture_status(state: str, session_id=None, *, detail=None, required: bool = False) -> None:
    status_path = _health_path("capture_status", CAPTURE_STATUS_PATH)
    tmp = status_path + f".{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(status_path), exist_ok=True)
        record = {"state": state, "session_id": session_id, "ts": int(time.time())}
        if detail is not None:
            record["detail"] = detail   # a CONTENT-FREE structural fingerprint on a failure (no text)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(record))
            if required:
                fh.flush()
                os.fsync(fh.fileno())
        os.replace(tmp, status_path)
        if required:
            directory = os.open(os.path.dirname(status_path), os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        if state != "captured":
            _append_failure_history(record)   # a failure survives the next success (StarshipSuperjam/engine-template#774)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        if required:
            raise


def read_capture_status():
    """The last capture attempt's outcome record, or None (no marker yet / unreadable). Consumers
    (boot's dashboard line, telemetry's drain) treat None as nothing-to-say, never as failure."""
    status_path = _health_path("capture_status", CAPTURE_STATUS_PATH)
    try:
        with open(status_path, encoding="utf-8") as fh:
            record = json.load(fh)
        return record if isinstance(record, dict) and record.get("state") in CAPTURE_STATUS_STATES \
            else None
    except (OSError, ValueError):
        return None


# --- The public capture entry (what close's relay calls) --------------------------------------

def _session_from_env_chain():
    """The provider seam's env chain (the neutral ENGINE_SESSION_ID override and the platform vars) —
    consulted after the payload and the historical SESSION_ENV read, so existing behavior is a strict
    superset. Fail-soft: an unimportable seam resolves nothing."""
    try:
        import providers   # lazy: the tools-dir seam; this package puts the tools dir on sys.path
        return providers.session_from_env()
    except Exception:  # noqa: BLE001 — capture never breaks on an optional seam
        return None


def capture_turn_delta(payload, *, cwd=None) -> int:
    """Append the completed turn's new transcript messages to the memory ledger. Returns the number of
    records appended. FAIL-SOFT: any fault — bad payload, missing/oversized/out-of-scope transcript,
    lock contention — is a clean no-op (returns 0) and NEVER raises into the caller; the outcome is
    recorded to the capture-status marker so the degradation is visible, never silent. This is the
    mechanism close's ambient-capture relay triggers on every `Stop`."""
    try:
        return _capture(payload, cwd=cwd)
    except Exception as exc:  # noqa: BLE001 — ambient capture never gates close; any failure is a no-op
        # …but never a SILENT one: the crash path is loud too, and it carries the exception CLASS
        # NAME only (StarshipSuperjam/engine-template#774) — never the message, which can embed paths or transcript content.
        _write_capture_status("failed", None,
                              detail={"reason": "exception", "exception_class": type(exc).__name__})
        return 0


def _capture(payload, *, cwd) -> int:
    if not isinstance(payload, dict):
        return 0
    session_id = payload.get("session_id") or os.environ.get(SESSION_ENV) or _session_from_env_chain()
    transcript_str = payload.get("transcript_path") or os.environ.get(TRANSCRIPT_ENV)
    if not session_id or not transcript_str:
        missing = [name for name, value in (("session_id", session_id),
                                            ("transcript_path", transcript_str)) if not value]
        _write_capture_status("no-transcript", session_id,
                              detail={"reason": "missing-field", "missing": missing})
        return 0
    transcript_path, path_reason = _validate_transcript_path(transcript_str, cwd)
    if transcript_path is None:
        _write_capture_status("invalid-path", session_id, detail={"reason": path_reason})
        return 0
    origin = payload.get("origin") if isinstance(payload.get("origin"), str) else None

    data_dir = ledger.ledger_dir(cwd)
    os.makedirs(data_dir, exist_ok=True)
    if _restore_quarantine_present(ledger.restore_transaction_path(cwd)):
        _write_capture_status("failed", session_id, detail={"reason": "restore-quarantine"})
        return 0
    lock_fd = _acquire_lock(os.path.join(data_dir, LOCK_FILENAME))
    if lock_fd is None:
        return 0  # contended ~1s; the delta is caught at the next Stop
    try:
        # A previous process may have stopped after any one of journal -> ledger -> cursor -> index -> status.
        # Roll that exact prepared work forward before looking at a newer transcript delta.
        if _read_capture_transaction(data_dir) is not None:
            _recover_capture_transaction(data_dir, cwd, recovering=True)
        # PROVIDER-ROUTED parsing: a Codex session's transcript goes ONLY through the
        # Codex recognizer — an unrecognized (changed) format is a loud zero-capture, never a
        # fall-through to the tolerant Claude parser below, which could capture fragments.
        #
        # Route by the transcript's RUNTIME HOME first: a transcript under ~/.claude is a Claude
        # transcript and one under ~/.codex a Codex one, whatever a process-level ENGINE_PROVIDER
        # signal says — so a leaked or stale provider signal can never hand a Claude transcript to the
        # Codex recognizer (StarshipSuperjam/engine-template#1193, where exactly that misrouted one real
        # session). Only an AMBIGUOUS root (the shared clone root, or the ENGINE_MEMORY_TRANSCRIPT_DIR
        # override) falls back to providers.detect exactly as before — turn_id and a Codex tool name
        # stay Codex signals there, and detect's own answer is unchanged for every caller. The routing
        # decision and the detect signal behind it are recorded, content-free, on every marker so a
        # future misroute is visible after the fact.
        import providers  # lazy: the tools-dir seam; this package puts the tools dir on sys.path
        home_provider = _runtime_home_provider(transcript_path)
        if home_provider is not None:
            provider = home_provider
            routing = {"by": "home", "provider": provider, "signal": None}
        else:
            provider = providers.detect(payload)
            routing = {"by": "detect", "provider": provider, "signal": providers.detect_signal(payload)}
        if provider == providers.CODEX:
            messages, recognized, detail = _codex_messages(transcript_path)
            if not recognized:
                detail = {**detail, "routing": routing}
                _write_capture_status("unparseable", session_id, detail=detail)
                return 0
        else:
            messages = [r for r in _extract_records(transcript_path) if _is_message(r)]
        cursor = _read_cursor(data_dir, session_id)
        delta = messages[cursor:]
        if not delta:
            _write_capture_status("captured", session_id, detail={"routing": routing})
            return 0
        fresh: list = []          # prepared first; the journal lands before any one of these records
        for offset, rec in enumerate(delta):
            text = _message_text(rec)
            if not text or not text.strip():
                continue
            if _is_noise(text):
                continue
            # Redact secret-shaped content AFTER the empty/noise discard — large machine-output noise
            # (command stdout: hex, base64, minified) is dropped without being scrubbed — but BEFORE
            # chunking, so a credential straddling the >4KB chunk boundary is still caught as one unit
            # (scrubbed at capture; precision-biased, fail-soft).
            text = scrub.scrub_text(text)
            speaker = _speaker(rec)
            # Recognise a harness-injected pseudo-turn on the WHOLE message, before chunking, so every chunk of a
            # multi-chunk block (e.g. the >4 KB /compact continuation summary) is tagged — not just the first
            # (issue StarshipSuperjam/engine-template#274). The record still lands + stays recoverable; consolidation skips it as fuel.
            injected = records.is_injected_pseudo_turn_text(text)
            for chunk in chunk_text(text):
                record = _make_record(session_id, cursor + offset, speaker, chunk, injected=injected,
                                      origin=origin)
                fresh.append(record)
        transaction = {
            "schema_version": "capture-transaction.v1",
            "session_id": session_id,
            "cursor_after": len(messages),
            "records": fresh,
        }
        _write_capture_transaction(data_dir, transaction)
        return _recover_capture_transaction(data_dir, cwd, recovering=False, routing=routing)
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
    """Read the saved turn-notes straight back out of the cabinet (the ledger) and return those whose text
    contains the asked-for words. The LEDGER is the one source of truth and the index is derived from it, so
    the honest read-back for what capture just wrote is the ledger itself — which is exactly what this demo
    proves ('saved and can't be lost'), independently of whether any index exists. Recall and ranking are a
    separate part of the engine, not exercised here — a plain substring match stands in for 'ask for these
    words'."""
    needle = query_text.lower()
    return [r.get("text", "") for r in ledger.read(path=ledger.ledger_path()).records
            if needle in (r.get("text") or "").lower()]


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

        print("\nPART 5 — a secret in your conversation is scrubbed before it is ever saved")
        print("-" * 80)
        fake_secret = "AKIAIOSFODNN7EXAMPLE"   # a SYNTHETIC, non-real AWS-shaped example key
        secret_transcript = os.path.join(tmp, "secret.jsonl")
        secret_turn = ("Deploy note: the pelican-migration access key " + fake_secret
                       + " should never be stored — rotate it quarterly.")
        _demo_transcript(secret_transcript, [("user", secret_turn)])
        capture_turn_delta({"session_id": "practice-session-secret", "transcript_path": secret_transcript})
        all_texts = [r.get("text", "") for r in ledger.read(path=ledger.ledger_path()).records]
        leaked = any(fake_secret in t for t in all_texts)                 # the raw key must be NOWHERE
        redacted_present = any("[redacted:aws-key]" in t for t in all_texts)
        prose_kept = any("pelican-migration" in t and "quarterly" in t for t in all_texts)
        redaction_ok = (not leaked) and redacted_present and prose_kept
        print("  Filed a turn that contained a fake AWS-shaped key (not printed here — that's the point).")
        print(f"  Is the raw secret anywhere in the saved notes?   {'YES — LEAK!' if leaked else 'no'}")
        print(f"  ask for \"pelican-migration\"  ->  {_demo_excerpt(_demo_notes('pelican-migration'), 'pelican-migration')}")
        print("  => The secret was replaced with [redacted:aws-key] before saving; the surrounding note")
        print("     (\"pelican-migration … rotate it quarterly\") is kept intact. Redaction happens at")
        print("     capture, so the secret never touches the cabinet — or any backup of it.")

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
    ok = (n > 0 and filed > 0 and again == 0 and not before_handoff and bool(after_handoff)
          and redaction_ok)
    if not ok:
        print("\nDEMO UNEXPECTED: a practice turn did not file notes, a re-run was not a no-op, the "
              "end-of-turn ambient capture did not save a note, or a secret was NOT scrubbed before "
              "saving (leaked, no placeholder, or the surrounding note was corrupted).", file=sys.stderr)
        return 1
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    print("usage: capture.py demo")
    return 0


try:
    from . import mutation_authority as _mutation_authority
except ImportError:  # direct CLI
    from memory import mutation_authority as _mutation_authority
_mutation_authority.install_module_guards(globals())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
