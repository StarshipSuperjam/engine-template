#!/usr/bin/env python3
"""boot_alarm_ledger — the standing-alarm presentation ledger.

Boot's ONE local write: boot stays read-only of *canonical* state, and its one local write is this
gitignored, non-canonical presentation ledger. It records, per standing governance alarm, the
structured CONDITION VALUE last relayed IN FULL, so a SessionStart that finds the same condition
unchanged can collapse it to a terse reminder instead of re-relaying the full paragraph every resume
(the #313 habituation). The decision is DETERMINISTIC and lives in this hook-side code, never the model —
boot relays whatever variant the decision hands it.

Laws (all load-bearing):
  - FAIL-TOWARD-FULL. A missing / unreadable / malformed ledger, lock contention, or a write failure
    renders every alarm IN FULL (repetition is the tolerable failure; suppression is not). Nothing here
    raises to the caller, and a failed write never blocks the turn.
  - FINGERPRINT THE STRUCTURED CONDITION, never the rendered prose (the prose is reworded on relay, so
    hashing it would never collapse). The compared value is the structured signal the substrate already
    detected — the gate `[state, reason]`, the open-findings count. Stored as the comparable VALUE (not an
    opaque hash) so the renderer can tell unchanged from worsened.
  - SHOWN-IN-FULL IS STAMPED ONLY ON A TRUE FULL RELAY. A collapsed (terse) session keeps the prior entry
    untouched; a vanished alarm is DROPPED, so a recurrence relays full again (the operator-facing "a
    problem vanishing means the engine verified it fixed, not that it stopped checking" promise). A terse
    render can never become a future collapse baseline (no suppression-by-drift).
  - ISOLATED FROM MEMORY'S CONSOLIDATION SWEEP — no shared code path: the git-common-root resolver is
    COPIED here (the checkout_health / memory-ledger idiom), never imported from the memory package, and
    the ledger lives in a distinct gitignored directory. The dependency is one-way: boot -> here only.
  - STABLE PER-INSTANCE PATH under the shared clone root's `.engine/boot/.cache/`, so the ledger spans
    separate sessions on the one operator's machine and is never trapped in an ephemeral worktree.

CLI (operator-runnable debug view): python tools/boot_alarm_ledger.py path   # print the resolved path
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

# A test/override hook for the cache directory (the ENGINE_MEMORY_DIR idiom in memory/ledger.py). Lets a
# test point the ledger at a tmp dir without a git layout.
ENV_DIR = "ENGINE_BOOT_CACHE_DIR"
# The `.cache` segment is DELIBERATE: module_coherence prunes `.cache` directories at any depth, so the
# ledger never trips the orphan-wire walk. `.engine/boot/` is boot's topology-sanctioned artifact home.
CACHE_SUBDIR = os.path.join(".engine", "boot", ".cache")
LEDGER_FILENAME = "standing-alarms.json"


def _run(cmd: list) -> str | None:
    """Run a local command, return stripped stdout or None on any failure. Never raises."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=False)
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:  # noqa: BLE001 — a missing binary / OS error degrades to "unavailable"
        return None


def _git_common_root(cwd: str | None = None) -> str | None:
    """The shared clone root (parent of the common `.git` dir) so every worktree shares ONE ledger; None
    for a bare repo / unusual layout / git unavailable. COPIED (not imported) from the checkout_health /
    memory-ledger idiom so boot's ledger shares no code path with memory's consolidation sweep."""
    base = cwd or os.getcwd()
    out = _run(["git", "-C", base, "rev-parse", "--git-common-dir"])
    if not out:
        return None
    common = out if os.path.isabs(out) else os.path.join(base, out)
    common = os.path.normpath(os.path.abspath(common))
    if os.path.basename(common) == ".git":
        return os.path.dirname(common)
    return None


def ledger_dir(cwd: str | None = None) -> str:
    """The directory holding the ledger: the ENV_DIR override, else the shared clone root's
    `.engine/boot/.cache/`, else a CWD-relative fallback (so it still resolves where git is unavailable)."""
    env = os.environ.get(ENV_DIR)
    if env:
        return os.path.abspath(os.path.expanduser(env))
    root = _git_common_root(cwd)
    base = root if root is not None else (cwd or os.getcwd())
    return os.path.join(base, CACHE_SUBDIR)


def ledger_path(cwd: str | None = None, path: str | None = None) -> str:
    """The full ledger path. An explicit `path` wins (tests); else `<ledger_dir>/standing-alarms.json`."""
    return path if path else os.path.join(ledger_dir(cwd), LEDGER_FILENAME)


def _read(path: str) -> dict | None:
    """The ledger as {key: {"value": <json>, "shown_in_full": bool}}, or None on
    absent/unreadable/malformed (-> fail-toward-full at the caller)."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001 — absent / unreadable / malformed -> None (full relay)
        return None
    return data if isinstance(data, dict) else None


def _write(path: str, ledger: dict) -> bool:
    """Atomically write the ledger — temp file in the SAME directory, then os.replace. Returns
    True/False; never raises (a failed write degrades to 'no ledger next time' -> full relay)."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(ledger, fh, separators=(",", ":"), sort_keys=True)
        os.replace(tmp, path)
        return True
    except Exception:  # noqa: BLE001 — degrade to no ledger, never a crash
        return False


def _acquire(lock_path: str):
    """A single non-blocking exclusive lock on the read-decide-write, or None on contention (the tightest
    bound — no sleep stalls a SessionStart hook; contention degrades to fail-toward-full)."""
    fd = None
    try:
        fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o644)
    except OSError:
        return None
    if not _HAVE_FCNTL:  # pragma: no cover — no cross-process lock available; proceed best-effort
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


def decide(alarms: list, *, cwd: str | None = None, path: str | None = None) -> dict:
    """Read the ledger, decide collapse-vs-full per collapse-eligible alarm, write the new ledger, and
    return the per-key outcome the renderer (boot) turns into wording.

    `alarms`: an ordered list of {"key": str, "value": <json-able>} for the COLLAPSE-ELIGIBLE alarms live
    THIS session (boot passes only those; the degrade-loud tells never reach the ledger). Returns
        {"ok": bool, "results": {key: {"outcome": "collapse"|"full", "prior": <value|None>}}}
    `ok` is False on any ledger-read failure / contention -> every alarm "full" with prior None
    (fail-toward-full; the renderer then uses neutral full wording, never a misleading "still"/"worse").

    Ledger semantics: an alarm whose stored value equals the live value AND was last shown in full
    collapses (and its entry is kept verbatim). A new / changed alarm renders full and (re)stamps
    shown_in_full at the live value. Alarms not live this session are dropped (vanished -> verified-fixed,
    so a recurrence relays full). A write failure leaves the decision intact (ok unchanged) and never
    blocks the turn."""
    # Build the key/value view DEFENSIVELY: a malformed alarm entry must degrade to fail-toward-full
    # (the renderer defaults a missing key to full), never raise into boot's SessionStart hook.
    try:
        keys = [a["key"] for a in alarms]
        current = {a["key"]: a["value"] for a in alarms}
    except Exception:  # noqa: BLE001 — a malformed alarm -> empty results -> renderer renders all full
        return {"ok": False, "results": {}}
    failsafe = {"ok": False, "results": {k: {"outcome": "full", "prior": None} for k in keys}}
    # An empty alarm set is NOT a no-op: it still runs so any prior entry is DROPPED (every alarm vanished
    # -> verified-fixed). Skipping the write here would let a stale entry survive and wrongly collapse a
    # recurrence. The write is boot's one sanctioned local write; an empty ledger is the correct result.
    target = ledger_path(cwd, path)
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
    except Exception:  # noqa: BLE001 — can't even make the dir -> fail-toward-full
        return failsafe
    fd = _acquire(target + ".lock")
    if fd is None:
        return failsafe  # contention -> fail-toward-full (a collapse lost is safe; a hidden alarm is not)
    try:
        old = _read(target)
        ok = old is not None  # a missing/unreadable/malformed ledger -> neutral full this run, then seed
        if old is None:
            old = {}
        results: dict = {}
        new_ledger: dict = {}
        for k in keys:
            val = current[k]
            entry = old.get(k) if isinstance(old.get(k), dict) else None
            if ok and entry is not None and entry.get("shown_in_full") and entry.get("value") == val:
                results[k] = {"outcome": "collapse", "prior": val}
                new_ledger[k] = {"value": val, "shown_in_full": True}   # keep the prior full baseline
            else:
                prior = entry.get("value") if (ok and entry is not None) else None
                results[k] = {"outcome": "full", "prior": prior}
                new_ledger[k] = {"value": val, "shown_in_full": True}   # stamp THIS true full relay
        # Keys present last session but not live now are simply absent from new_ledger -> dropped.
        _write(target, new_ledger)
        return {"ok": ok, "results": results}
    except Exception:  # noqa: BLE001 — any unexpected failure -> fail-toward-full
        return failsafe
    finally:
        _release(fd)


def main(argv: list) -> int:
    if argv and argv[0] == "path":
        print(ledger_path())
        return 0
    print("usage: boot_alarm_ledger.py path", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
