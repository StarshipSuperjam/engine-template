#!/usr/bin/env python3
"""coordination_ledger — the local, ephemeral bookkeeping for advisory coordination
(StarshipSuperjam/engine-template#939, eADR-0043 law 6).

WHAT IT HOLDS, AND WHAT IT IS NOT. Two kinds of purely-local state: (1) per pull request, the last-known
board snapshot and which notices this machine has already shown, so boot can relay "N coordination notices
await" from LOCAL state with no network read (eADR-0043: boot never touches GitHub); and (2) a bounded
measurement ring — enum/id/timestamp events only — that answers "did coordination reduce late conflicts and
polling?" without keeping any message content. This is presentation + measurement bookkeeping, never a
knowledge corpus: a wipe (fresh clone, new machine) simply restarts it, and a future session reconstructs
project truth from GitHub, never from here.

WHY A DEDICATED FILE (not the governance-alarm ledger). boot_alarm_ledger.decide() REBUILDS
`standing-alarms.json` from scratch each SessionStart, so a foreign key written there is silently dropped;
and its "a vanished alarm was verified-fixed" semantics are false for a transient notice. So coordination
keeps its own file — `coordination.json` beside the alarm ledger under the shared clone root's gitignored,
`.cache`-pruned `.engine/boot/.cache/` — and copies (never imports) the clone-root + flock idiom, so the two
share no code path and no lock.

CONCURRENCY. Every write is a read-modify-write under one non-blocking exclusive flock; contention degrades
to best-effort (a coordination measurement is never worth stalling a caller). The ring evicts oldest-beyond
cap; the seen/known lists are per-pull-request and capped. Reads never lock and never raise — a missing or
malformed file reads as empty.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

try:
    import fcntl
    _HAVE_FCNTL = True
except Exception:  # pragma: no cover — non-POSIX target; degrade to no cross-process lock
    _HAVE_FCNTL = False

# Test/override hook for the cache directory (the boot_alarm_ledger.ENV_DIR idiom).
ENV_DIR = "ENGINE_COORDINATION_CACHE_DIR"
# Reuse the alarm ledger's gitignored, `.cache`-pruned, clone-root cache dir — a SEPARATE FILE, so decide()'s
# whole-file rebuild never touches it. `.cache` is pruned by module_coherence, so this needs no new gitignore
# wire and never trips the orphan-wire walk.
CACHE_SUBDIR = os.path.join(".engine", "boot", ".cache")
LEDGER_FILENAME = "coordination.json"

_EVENTS_CAP = 500     # the measurement ring: newest 500 events, oldest evicted
_SEEN_CAP = 200       # per-pull-request seen-id history (a board never holds more than a handful live)
_PR_CAP = 100         # tracked pull requests: bound the file so a long-lived clone never grows unboundedly


def _run(cmd: list) -> "str | None":
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=False)
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


def _git_common_root(cwd: "str | None" = None) -> "str | None":
    """The shared clone root (parent of the common `.git`), so every worktree shares ONE ledger. COPIED (not
    imported) from the boot-ledger / checkout-health idiom, so coordination shares no code path with them."""
    base = cwd or os.getcwd()
    out = _run(["git", "-C", base, "rev-parse", "--git-common-dir"])
    if not out:
        return None
    common = out if os.path.isabs(out) else os.path.join(base, out)
    common = os.path.normpath(os.path.abspath(common))
    if os.path.basename(common) == ".git":
        return os.path.dirname(common)
    return None


def ledger_dir(cwd: "str | None" = None) -> str:
    env = os.environ.get(ENV_DIR)
    if env:
        return os.path.abspath(os.path.expanduser(env))
    root = _git_common_root(cwd)
    if root is None:
        # git unavailable: the launch cwd is typically `<root>/.engine` (tools run via `uv run --directory
        # .engine`), so peel a trailing `.engine` to name the clone root rather than doubling the path
        # (StarshipSuperjam/engine-template#753). A git-confirmed root is never peeled.
        base_cwd = os.path.normpath(cwd or os.getcwd())
        base = os.path.dirname(base_cwd) if os.path.basename(base_cwd) == ".engine" else base_cwd
    else:
        base = root
    return os.path.join(base, CACHE_SUBDIR)


def ledger_path(cwd: "str | None" = None, path: "str | None" = None) -> str:
    return path if path else os.path.join(ledger_dir(cwd), LEDGER_FILENAME)


def _read(path: str) -> dict:
    """The ledger dict, or an empty skeleton on absent/unreadable/malformed (never raises)."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001 — absent / unreadable / malformed -> empty
        data = None
    if not isinstance(data, dict):
        data = {}
    data.setdefault("boards", {})   # pr(str) -> [{"notice_id","kind"}]  (last-known board snapshot)
    data.setdefault("seen", {})     # pr(str) -> [notice_id]             (already shown by this machine)
    data.setdefault("events", [])   # the bounded measurement ring
    return data


def _write(path: str, ledger: dict) -> bool:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(ledger, fh, separators=(",", ":"), sort_keys=True)
        os.replace(tmp, path)
        return True
    except Exception:  # noqa: BLE001
        return False


def _acquire(lock_path: str):
    fd = None
    try:
        fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o644)
    except OSError:
        return None
    if not _HAVE_FCNTL:  # pragma: no cover
        return fd
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError:
        os.close(fd)
        return None


def _release(fd) -> None:
    try:
        if _HAVE_FCNTL:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _mutate(fn, *, cwd=None, path=None):
    """Read-modify-write under one non-blocking lock. `fn(ledger)` mutates in place and returns any value to
    hand back. On lock contention or write failure the mutation is best-effort-skipped (returns fn's default
    via a fresh read) — a coordination write never stalls or crashes a caller."""
    full = ledger_path(cwd, path)
    lock = _acquire(full + ".lock")
    if lock is None:
        return None
    try:
        ledger = _read(full)
        result = fn(ledger)
        _write(full, ledger)
        return result
    finally:
        _release(lock)


# ---- board snapshot + seen bookkeeping (boot reads these locally; no network) -----------------------------

def sync_board(pr: int, notices: list, *, cwd=None, path=None) -> list:
    """Record the current board snapshot for `pr` and return the UNSEEN notices (those not yet shown by this
    machine). Called by a mid-session read point AFTER it fetches the board from GitHub — so boot can later
    relay the unseen count purely from local state. Each entry kept is {notice_id, kind}."""
    snapshot = [{"notice_id": n["notice_id"], "kind": n["kind"]} for n in notices]

    def _fn(ledger):
        # Move this pull request to the most-recent position (pop then set), then bound the tracked set so a
        # long-lived shared clone never grows the file without limit — evict the least-recently-synced.
        ledger["boards"].pop(str(pr), None)
        ledger["boards"][str(pr)] = snapshot
        while len(ledger["boards"]) > _PR_CAP:
            stale = next(iter(ledger["boards"]))
            ledger["boards"].pop(stale, None)
            ledger["seen"].pop(stale, None)
        seen = set(ledger["seen"].get(str(pr), []))
        return [n for n in notices if n["notice_id"] not in seen]

    result = _mutate(_fn, cwd=cwd, path=path)
    if result is None:  # contention: fall back to a lock-free read so the caller still sees the unseen set
        seen = set(_read(ledger_path(cwd, path))["seen"].get(str(pr), []))
        return [n for n in notices if n["notice_id"] not in seen]
    return result


def mark_seen(pr: int, notice_ids, *, cwd=None, path=None) -> None:
    """Record that this machine has shown `notice_ids` for `pr`, so boot's relay stops counting them."""
    ids = [i for i in notice_ids if isinstance(i, str)]
    if not ids:
        return

    def _fn(ledger):
        cur = ledger["seen"].get(str(pr), [])
        merged = cur + [i for i in ids if i not in cur]
        ledger["seen"][str(pr)] = merged[-_SEEN_CAP:]
        return None

    _mutate(_fn, cwd=cwd, path=path)


def pending(*, cwd=None, path=None) -> dict:
    """The unseen notices across every pull request this machine has snapshotted — {pr(int): [{notice_id,
    kind}]}. Pure local read (no lock, no network): boot's relay source. A pull request with nothing unseen is
    omitted."""
    ledger = _read(ledger_path(cwd, path))
    out = {}
    for pr_str, snapshot in ledger.get("boards", {}).items():
        seen = set(ledger.get("seen", {}).get(pr_str, []))
        unseen = [e for e in snapshot if e.get("notice_id") not in seen]
        if unseen:
            try:
                out[int(pr_str)] = unseen
            except (TypeError, ValueError):
                continue
    return out


# ---- measurement ring (enum/id/timestamp events only; no message content) ---------------------------------

_EVENT_TYPES = ("posted", "read", "poke-sent", "poke-skipped", "late-conflict", "queue-poll", "overlap-computed")


def record_event(event_type: str, *, at: str, cwd=None, path=None, **fields) -> None:
    """Append one bounded measurement event (evicting oldest beyond the cap). `event_type` must be a known
    enum; `fields` may carry only enum/id/integer/timestamp values (never message text) — a str longer than a
    conservative bound is dropped, so no prose can leak into the ring."""
    if event_type not in _EVENT_TYPES:
        return
    rec = {"t": event_type, "at": at}
    for k, v in fields.items():
        if isinstance(v, bool) or isinstance(v, int):
            rec[k] = v
        elif isinstance(v, str) and len(v) <= 64:
            rec[k] = v
        # anything else (a dict, a long string) is silently dropped — the ring holds no content

    def _fn(ledger):
        ledger["events"].append(rec)
        if len(ledger["events"]) > _EVENTS_CAP:
            ledger["events"] = ledger["events"][-_EVENTS_CAP:]
        return None

    _mutate(_fn, cwd=cwd, path=path)


def events(*, cwd=None, path=None) -> list:
    """The measurement ring (oldest-first), for the metrics report. Pure read."""
    return list(_read(ledger_path(cwd, path)).get("events", []))


def metrics(*, cwd=None, path=None) -> dict:
    """Aggregate the measurement ring into the numbers that answer "did coordination reduce late conflicts and
    polling?" — counts per event type, plus late-conflicts and queue-polls. Pure read; the evidence the
    StarshipSuperjam/engine-template#989 dogfood decision reads. Content-free (only enum/id/timestamp events ever entered the ring)."""
    evs = events(cwd=cwd, path=path)
    by_type: dict = {}
    for e in evs:
        by_type[e.get("t", "?")] = by_type.get(e.get("t", "?"), 0) + 1
    return {"total": len(evs), "by_type": by_type,
            "late_conflicts": by_type.get("late-conflict", 0),
            "notices_posted": by_type.get("posted", 0),
            "queue_polls": by_type.get("queue-poll", 0)}


def main(argv: list) -> int:
    if argv and argv[0] == "metrics":
        import json as _json
        print(_json.dumps(metrics(), indent=2))
        return 0
    print("usage: coordination_ledger.py metrics", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
