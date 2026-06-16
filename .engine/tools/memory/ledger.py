"""ledger.py — the engine's memory ledger: the canonical, append-only, plain-text store.

This is the foundation of the memory substrate (memory-substrate-sqlite-fts5, build slice 1).
The locked design (engine-planning systems/cognitive/memory) makes this NDJSON ledger the ONE
source of truth; the SQLite/FTS5 index built later is a throwaway accelerator rebuilt from here,
and backup is "copy this file". So this file's integrity is a law, not a leaf:

  - Writes are SERIALIZED. A bare append is atomic only for writes under the platform's PIPE_BUF
    bound (~4 KB); an episodic memory record routinely exceeds that, so two live sessions appending
    at once could tear a line. Every append takes an exclusive advisory lock (`fcntl.flock`) so the
    writes never interleave. (The serialization requirement is the law; flock is the build-spec leaf.)

  - Reads are LINE-RESILIENT. The reader skips and counts a malformed line rather than halting (one
    bad line never costs the records after it), and tolerates a torn TRAILING line from a crash
    mid-append by rejecting it structurally: a record is complete only if it is newline-terminated.
    A final line without its terminating newline is a half-written record and is dropped, never read
    back as if real.

  - Each record is one JSON object per line, terminated by a single "\n" (the record terminator).
    `json.dumps` escapes any newline inside the data, so the trailing "\n" is unambiguous.

Slice-1 scope: the read/write primitives only. They are RECORD-AGNOSTIC — they store and return any
JSON-serializable dict; the closed memory *record shape* (the role vocabulary) and the per-record
ledger-version envelope are a later slice's job (a hard forward-owe: slice 3 fixes the record shape,
and because the ledger ships EMPTY there are no real records to retrofit before then).

This module is a leaf: it never logs findings or writes telemetry of its own. It RETURNS its
read-health report to the caller; surfacing degraded recall is boot/telemetry's job.

Path resolution shares the ledger across every git worktree of one clone (the engine's AI sessions
each work in their own worktree under .claude/worktrees/, but the project's memory is one store): it
resolves to the shared clone root's `.engine/memory/`, overridable via the ENGINE_MEMORY_DIR env var.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field

try:
    import fcntl  # POSIX advisory file locking (macOS dev + ubuntu CI); absent on Windows.
    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover - the engine targets POSIX; degrade rather than crash.
    _HAVE_FCNTL = False

# The on-disk framing version (newline-terminated NDJSON). This is the *format* version; the
# record-SHAPE version (what a future migration routes on) is established with the record schema
# in a later slice. Exposed so the backup snapshot-manifest has a legible version source.
LEDGER_FORMAT_VERSION = 1

ENV_DIR = "ENGINE_MEMORY_DIR"
DATA_SUBDIR = os.path.join(".engine", "memory")
LEDGER_FILENAME = "ledger.ndjson"


@dataclass
class LedgerRead:
    """The result of reading the ledger: the intact records plus an honest read-health report.

    `malformed` counts complete lines that failed to parse (each skipped, never halting the read).
    `torn_trailing` is True when the final line lacked its terminating newline — a half-written
    record from a crash mid-append, dropped rather than read back as real.
    """

    records: list = field(default_factory=list)
    malformed: int = 0
    torn_trailing: bool = False


def _git_common_root(cwd: str | None = None) -> str | None:
    """The shared clone root (parent of the common `.git` dir), so every worktree shares one ledger.

    Returns None for a bare repo, an unusual layout, or when git is unavailable — the caller then
    falls back to a CWD-relative path. (Mirrors checkout_health.py's `.git`-name guard: only resolve
    the parent when the common dir is actually named `.git`; never guess otherwise.)
    """
    base = cwd or os.getcwd()
    try:
        out = subprocess.run(
            ["git", "-C", base, "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None
    if not out:
        return None
    common = out if os.path.isabs(out) else os.path.join(base, out)
    common = os.path.normpath(os.path.abspath(common))
    if os.path.basename(common) == ".git":
        return os.path.dirname(common)
    return None


def ledger_dir(cwd: str | None = None) -> str:
    """Resolve the directory holding the ledger: ENGINE_MEMORY_DIR override, else the shared clone
    root's `.engine/memory/`, else a CWD-relative fallback."""
    env = os.environ.get(ENV_DIR)
    if env:
        return os.path.abspath(os.path.expanduser(env))
    base_cwd = cwd or os.getcwd()
    root = _git_common_root(base_cwd)
    base = root if root is not None else base_cwd
    return os.path.join(base, DATA_SUBDIR)


def ledger_path(cwd: str | None = None) -> str:
    """The resolved path to the ledger file."""
    return os.path.join(ledger_dir(cwd), LEDGER_FILENAME)


def append(record: dict, *, path: str | None = None) -> None:
    """Append one record to the ledger as a single newline-terminated JSON line, under an exclusive
    advisory lock so concurrent writers never tear a line. Creates the data directory on first write.

    `record` may be any JSON-serializable dict (record shape is a later slice's concern).
    """
    target = path or ledger_path()
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    data = line.encode("utf-8")
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        if _HAVE_FCNTL:
            fcntl.flock(fd, fcntl.LOCK_EX)
        # Write the whole framed record while holding the lock (O_APPEND keeps every write at EOF;
        # the lock keeps a second writer out until we have written all bytes and flushed them).
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        if _HAVE_FCNTL:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def read(*, path: str | None = None) -> LedgerRead:
    """Read every intact record from the ledger, line-resilient, returning a read-health report.

    Skips and counts a malformed (complete-but-unparseable) line; skips blank lines without counting
    them; drops a torn trailing line (a record whose terminating newline never landed) and flags it.
    A missing ledger file reads as empty — the substrate ships empty.
    """
    target = path or ledger_path()
    result = LedgerRead()
    if not os.path.exists(target):
        return result
    with open(target, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            if not raw.endswith("\n"):
                # Only the final line of a file can lack its newline: a half-written record from a
                # crash mid-append. Reject it structurally (the missing terminator) and stop.
                result.torn_trailing = True
                break
            stripped = raw.strip()
            if not stripped:
                continue  # a blank line is not a record and is not corruption
            try:
                result.records.append(json.loads(stripped))
            except ValueError:
                result.malformed += 1
    return result


def iter_records(*, path: str | None = None):
    """Stream the intact records, quietly skipping malformed and torn lines. The streaming form for
    a full scan (e.g. rebuilding the derived index); use read() when the read-health report is wanted.
    """
    target = path or ledger_path()
    if not os.path.exists(target):
        return
    with open(target, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            if not raw.endswith("\n"):
                return  # torn trailing record — drop it
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except ValueError:
                continue
            yield record


# --- Operator demonstration -------------------------------------------------------------------
# An operator-runnable fail-then-pass walkthrough (the same pattern other engine tools use, e.g.
# checkout_health.py's _demo). It exercises the REAL append/read above; only the file location is a
# throwaway temp folder, so it never touches any real data. Run it and vary the numbers yourself:
#     uv run --directory .engine --frozen -- python tools/memory/ledger.py demo
# The module-level helpers below exist only for this demo (multiprocessing under "spawn" needs the
# worker to be importable by name); normal use never imports multiprocessing.

_DEMO_WRITERS = 8
_DEMO_PER_WRITER = 15
_DEMO_BODY_LEN = 9000  # > the ~4 KB the OS appends in one shot — the size that tears if unprotected


def _demo_entry(writer: int, seq: int) -> dict:
    body = f"WRITER-{writer}-START " + ("." * _DEMO_BODY_LEN) + f" END-WRITER-{writer}"
    return {"writer": writer, "seq": seq, "body": body}


def _demo_naive_append(record: dict, path: str) -> None:
    """What a naive writer does: NO lock, and it writes the entry in two pieces, so concurrent writers
    scramble each other. This is the hazard we protect against — NOT the engine's real code."""
    line = json.dumps(record) + "\n"
    half = len(line) // 2
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line[:half].encode("utf-8"))
        os.write(fd, line[half:].encode("utf-8"))
    finally:
        os.close(fd)


def _demo_worker(mode: str, path: str, writer_id: int) -> None:
    writer = _demo_naive_append if mode == "naive" else (lambda r, p: append(r, path=p))
    for seq in range(_DEMO_PER_WRITER):
        writer(_demo_entry(writer_id, seq), path)


def _demo_run_writers(mode: str, path: str) -> None:
    import multiprocessing as mp

    procs = [mp.Process(target=_demo_worker, args=(mode, path, w)) for w in range(_DEMO_WRITERS)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join()


def _demo() -> int:
    import tempfile

    expected = _DEMO_WRITERS * _DEMO_PER_WRITER
    print("=" * 78)
    print(f"TEST 1 — {_DEMO_WRITERS} sessions filing {_DEMO_PER_WRITER} large entries each, at the same moment")
    print(f"         (expecting {expected} whole entries)")
    print("=" * 78)
    with tempfile.TemporaryDirectory() as tmp:
        bad = os.path.join(tmp, "naive.txt")
        _demo_run_writers("naive", bad)
        good = scrambled = 0
        sample = None
        for raw in open(bad, "r", encoding="utf-8", errors="replace"):
            if not raw.endswith("\n"):
                continue
            try:
                rec = json.loads(raw)
                body = rec["body"]
                if body.startswith(f"WRITER-{rec['writer']}-START") and body.endswith(f"END-WRITER-{rec['writer']}"):
                    good += 1
                    continue
            except Exception:
                pass
            scrambled += 1
            sample = sample or raw.strip()
        print("\n  WITHOUT protection (a naive writer):")
        print(f"    whole entries: {good} / {expected}     SCRAMBLED entries: {scrambled}")
        if sample:
            print(f"    e.g. a scrambled entry begins:  {sample[:88]}…")
            print("    ^ notice a START with the wrong writer's number, or two entries mashed together.")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "ledger.ndjson")
        _demo_run_writers("real", path)  # the REAL engine code
        result = read(path=path)
        intact = sum(
            1 for r in result.records
            if r["body"].startswith(f"WRITER-{r['writer']}-START")
            and r["body"].endswith(f"END-WRITER-{r['writer']}")
        )
        print("\n  WITH the engine's memory file (the real code):")
        print(f"    whole entries: {intact} / {expected}     scrambled: {len(result.records) - intact}     corrupted on read: {result.malformed}")
        print(f"    => {'EVERY entry survived whole.' if intact == expected and result.malformed == 0 else '!!! something was lost'}")

    print("\n" + "=" * 78)
    print("TEST 2 — a crash leaves a half-written entry; does the good entry before it survive?")
    print("=" * 78)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "ledger.ndjson")
        append({"note": "DO NOT LOSE THIS"}, path=path)
        with open(path, "a", encoding="utf-8") as fh:  # crash mid-write: no terminating newline
            fh.write('{"note":"half-written when the power went ou')
        result = read(path=path)
        print(f"\n  entries read back: {[r['note'] for r in result.records]}")
        print(f"  half-written entry dropped: {result.torn_trailing}")
        ok = [r["note"] for r in result.records] == ["DO NOT LOSE THIS"] and result.torn_trailing
        print(f"  => {'The good entry survived; the half-written one was discarded.' if ok else '!!! unexpected'}")

    print("\n" + "=" * 78)
    print("TEST 3 — a single corrupted entry in the middle; do the entries around it survive?")
    print("=" * 78)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "ledger.ndjson")
        append({"note": "entry before the corruption"}, path=path)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("@@@ this line is corrupted garbage @@@\n")
        append({"note": "entry after the corruption"}, path=path)
        result = read(path=path)
        print(f"\n  entries read back: {[r['note'] for r in result.records]}")
        print(f"  corrupted entries skipped (and counted): {result.malformed}")
        ok = [r["note"] for r in result.records] == ["entry before the corruption", "entry after the corruption"]
        print(f"  => {'Both good entries survived; only the corrupted one was skipped.' if ok else '!!! unexpected'}")

    print("\n" + "-" * 78)
    print("Reminder: this is the empty filing cabinet. Nothing reads from it yet, and the engine won't")
    print("remember across sessions until later steps. Today's proof is only that the cabinet keeps")
    print("what's filed and never scrambles it.")
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    print("usage: ledger.py demo")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv[1:]))
