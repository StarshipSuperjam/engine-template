"""capture.py — ambient turn-delta capture: the content half of the memory substrate (slice 3a).

The locked design (engine-planning systems/cognitive/memory + lifecycle/close) splits memory capture
along a "content survives / reflection defers" seam. This module is the CONTENT half:

  - **Every completed turn (`Stop`) appends the turn's session-id-tagged delta to the ledger** — an
    *append, not a summarization*, so it never taxes mid-session use. The expensive AI-judged
    consolidation into clean, role-typed episodic records is the REFLECTION half (slice 3b), deferred
    because it needs the in-context AI's judgment, which a fire-and-forget hook does not have.

  - **Capture is cheap, generous, and LOSSLESS over conversation.** A long *turn* is *chunked*
    (paragraph-preferred, 4 KB) and every chunk is stored — conversational content is never elided at
    capture time; curation/compression is the later reflection step's job. What capture does NOT store is
    Claude Code's own transcript scaffolding — slash-command echoes, local-command output/caveats, and
    control sentinels (`_is_noise`) — because that plumbing is *not conversation*. Excluding it before the
    ledger is an "is this conversation at all" filter, NEVER the importance keep/discard gate the design
    forbids: the design bars gatekeeping on *worth* because worth is future-unknowable
    (engine-planning systems/cognitive/memory: "importance is a function of the future the capturing
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
RECORD_KIND = records.AMBIENT_CAPTURE_KIND   # the ambient-capture kind, now homed in `records` (the cycle-free
                                             # leaf `forget` also reads); aliased here so the string never drifts
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


# --- The consolidation lease (a "sessions-since" liveness heartbeat) ---------------------------
# The lease sidecar answers the one question the abandoned-session sweep (consolidate.py) cannot answer
# from the ledger alone: is a session with un-consolidated deltas still LIVE right now, or genuinely gone?
# It holds a monotonic session-epoch counter (bumped once per SessionStart) plus a lease map
# {session_id: the epoch at that session's last check-in}. A session is "silent" when its lease has aged past
# a small threshold N (consolidate.py owns N) — so the sweep recovers a truly-gone session promptly while a
# live concurrent session (which re-stamps its lease every turn) is spared. The lease lives beside the cursor
# and is guarded by the SAME `.capture.lock`, so a per-turn refresh and the sweep's store-time re-check
# serialize against each other. It is the "no lease heartbeat" signal the durability law names (README §76-79).

LEASE_FILENAME = "consolidation-lease.json"   # {"epoch": int, "leases": {session_id: epoch}}; gitignored sibling
LEASE_PRUNE_HORIZON = 64      # drop a lease aged this far past the epoch (long-gone; re-stamps if it revives)


def _lease_path(data_dir: str) -> str:
    return os.path.join(data_dir, LEASE_FILENAME)


def read_lease_state(data_dir: str):
    """The lease sidecar as `(epoch, leases)`, or **None if the file exists but is unparseable (CORRUPT)**.
    A MISSING/empty sidecar reads as `(0, {})` (the intended first-run state — every prior session is absent,
    i.e. recoverable), split DELIBERATELY from corrupt: consolidate must treat corrupt as "skip the sweep"
    (all sessions possibly-live) but absent as "proceed". This split is exactly why the lease reader does NOT
    mirror `_read_cursor`, which folds missing and corrupt into one `return 0`."""
    path = _lease_path(data_dir)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return 0, {}
    except OSError:
        return None                      # unreadable => fail safe (corrupt), never "absent"
    if not raw.strip():
        return 0, {}
    try:
        state = json.loads(raw)
    except ValueError:
        return None                      # present but unparseable => CORRUPT
    if not isinstance(state, dict):
        return None
    epoch = state.get("epoch", 0)
    leases = state.get("leases", {})
    if not (isinstance(epoch, int) and epoch >= 0) or not isinstance(leases, dict):
        return None
    clean = {k: v for k, v in leases.items() if isinstance(k, str) and isinstance(v, int) and v >= 0}
    return epoch, clean


def _write_lease_state(data_dir: str, epoch: int, leases: dict) -> None:
    """Atomically replace the lease sidecar (temp + os.replace inside the capture lock). Writes EXACTLY what
    it is given — the caller decides what to persist; it never resets-to-empty on its own (unlike
    `_write_cursor`), so a corrupt read can be handled by refusing to write rather than healing to `{}`."""
    path = _lease_path(data_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"epoch": epoch, "leases": leases}, fh, separators=(",", ":"))
    os.replace(tmp, path)


def _prune_far_aged(leases: dict, epoch: int) -> dict:
    """Drop any lease aged more than LEASE_PRUNE_HORIZON past the current epoch. Reaping-on-marker (consolidate)
    misses a session that never produces a genuine delta (so never gets a marker); this secondary prune keeps
    the per-turn-rewritten map bounded. A pruned session that revives simply re-stamps its lease next turn."""
    return {sid: e for sid, e in leases.items() if epoch - e <= LEASE_PRUNE_HORIZON}


def open_session_lease(data_dir: str, session_id: str) -> bool:
    """The SessionStart heartbeat (holds NO lock on entry, so it acquires the capture lock itself): bump the
    epoch, prune far-aged leases, stamp this session's lease at the new epoch, write. Returns True if the stamp
    LANDED — the caller may then run the sweep — and False if it could not (lock contention OR a corrupt
    sidecar), in which case the caller MUST DEFER the sweep this pass (never sweep with a missing self-lease,
    which would make this very session look consolidatable to a concurrent sweep). Best-effort: never raises."""
    try:
        os.makedirs(data_dir, exist_ok=True)
        lock_fd = _acquire_lock(os.path.join(data_dir, LOCK_FILENAME))
        if lock_fd is None:
            return False                 # contended ~1s; defer the sweep, caught next start
        try:
            state = read_lease_state(data_dir)
            if state is None:
                # CORRUPT: all prior lease info is unrecoverable. This is the ONE writer that resets the sidecar
                # (refresh/drop deliberately REFUSE on corrupt, to keep the corrupt->skip fail-safe holding) —
                # because if every writer refused, a corrupt sidecar would wedge consolidation shut forever. We
                # repair to a fresh valid sidecar seeded with only this session and DEFER the sweep this pass. The
                # cost: other parked sessions lose their lease, so on the NEXT start they read absent -> stale
                # with no cushion — a rare, corruption-gated one-off. No content is ever at risk (raw deltas stay
                # in the ledger); a live concurrent session re-stamps via its heartbeat and the re-check spares it.
                _write_lease_state(data_dir, 1, {session_id: 1})
                return False
            epoch, leases = state
            epoch += 1
            leases = _prune_far_aged(leases, epoch)
            leases[session_id] = epoch
            _write_lease_state(data_dir, epoch, leases)
            return True
        finally:
            _release_lock(lock_fd)
    except Exception:  # noqa: BLE001 — the heartbeat is best-effort; never take down the SessionStart directive
        return False


def refresh_lease_locked(data_dir: str, session_id: str) -> None:
    """The per-turn heartbeat — the CALLER MUST ALREADY HOLD the capture lock (an inline write, never a nested
    `_acquire_lock`: `flock` is not re-entrant across fds, so a second acquire would fail the non-blocking lock
    and silently drop the heartbeat). Stamps this session's lease at the CURRENT epoch, so an active session
    tracks the frontier and reads fresh to the sweep's store-time re-check. On a corrupt sidecar it REFUSES to
    write (leaves it for the next SessionStart repair) — never heals-to-empty. Cheap: a no-op once fresh this
    epoch."""
    state = read_lease_state(data_dir)
    if state is None:
        return                           # corrupt => refuse to write (do not reset to {})
    epoch, leases = state
    if leases.get(session_id) == epoch:
        return                           # already stamped this epoch; skip the rewrite
    leases[session_id] = epoch
    _write_lease_state(data_dir, epoch, leases)


def drop_lease_locked(data_dir: str, session_id: str) -> None:
    """Reap a session's lease (called when its consolidation marker is written — a marked session can never be
    live again). The CALLER MUST HOLD the lock; this RE-READS the sidecar under that lock and rewrites, so it
    never clobbers a concurrent heartbeat by writing back a copy cached across the lock. No-op on corrupt/absent."""
    state = read_lease_state(data_dir)
    if state is None:
        return
    epoch, leases = state
    if session_id in leases:
        del leases[session_id]
        _write_lease_state(data_dir, epoch, leases)


# --- The in-flight-migration marker (compaction refuses within a migration window) -------------
# The compaction↔provisioning ordering law (README §269-283): the single-writer lock serializes individual
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
    interleaving a running migration (§269-283)."""
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


def _make_record(session_id: str, seq: int, speaker: str, text: str, *, injected: bool = False) -> dict:
    """The turn-delta record envelope. `ts`/`seq` are INTEGERS on purpose: the derived index's
    record-text projection indexes only string leaves, so integers stay out of the search body. `id` is the
    stable, content-free record id minted at capture (slice 4b) — kept out of the search body too
    (index._NON_BODY_KEYS). `injected` adds `records.INJECTED_TAG` so the consolidation sweep skips a
    harness-injected pseudo-turn as fuel (issue #274) — the record still lands and stays fully recoverable;
    the tag (like every tag) is kept out of the search body, and turn-deltas are recall-excluded by kind anyway."""
    tags = ["transcript", "stop"]
    if injected:
        tags.append(records.INJECTED_TAG)
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
        # The per-turn lease heartbeat: stamp this session live at the current epoch, INSIDE the lock we already
        # hold and BEFORE the no-delta early return — so even a no-delta turn (noise-only, interrupted) still
        # refreshes liveness and a live session can never drift stale to the consolidation sweep.
        refresh_lease_locked(data_dir, session_id)
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
            if _is_noise(text):
                continue
            speaker = _speaker(rec)
            # Recognise a harness-injected pseudo-turn on the WHOLE message, before chunking, so every chunk of a
            # multi-chunk block (e.g. the >4 KB /compact continuation summary) is tagged — not just the first
            # (issue #274). The record still lands + stays recoverable; consolidation skips it as fuel.
            injected = records.is_injected_pseudo_turn_text(text)
            for chunk in chunk_text(text):
                ledger.append(_make_record(session_id, cursor + offset, speaker, chunk, injected=injected),
                              path=ledger_file)
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
