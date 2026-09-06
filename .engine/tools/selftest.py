#!/usr/bin/env python3
"""Run the full self-test suite once, legibly — the canonical local full-suite launcher.

Why this exists: the prescribed serial `unittest discover -s tools -p 'test_*.py' -b` run is hard to
read while it happens and hard to trust when it ends. It can run for minutes while `-b` buffers every
test's output, so a session polls the process guessing whether it hung; a demo that reads stdin blocks
outright under an attached terminal; and when the run finally prints, the result is routinely piped
through `tail`/`grep`, which truncates the very tracebacks the session needs — forcing a re-run. This
launcher wraps the SAME proven run (same discovery, order, buffering, test set) and makes one run
enough: it announces progress on a heartbeat so a live run is never mistaken for a hang, forces the
child's stdin to end-of-input so no demo can block, and writes the full output to a log while printing
a clean, self-contained result — the complete list of failing tests plus their tracebacks — so nothing
needs to be piped and nothing needs to be re-run.

The log is a UNIQUE per-run temp file (so concurrent sessions — this repo's normal model — never share
or clobber one) and is KEPT whether the run passes or fails, so a session can always read its own run
rather than mistaking a vanished log for a failure; its path is printed at the start and again in the
closing summary. Cleanup is the daily sweep — a later run removes any of this user's logs older than a
day. The on-screen result is built from the run's own in-memory output, never by re-reading the file, so
a concurrent run can never make it show the wrong failures.

The load-bearing invariant: the launcher's exit status is the child suite's exit status, VERBATIM
(`proc.returncode`). It is NEVER derived from parsing the run's text for an `OK`/`FAILED` line. A test
that errors at import/collection time, or a child that is killed or crashes, exits non-zero without a
tidy summary — and must still surface as a failure, never a false green. The displayed summary is the
child's own words; only the display is textual, never the verdict.

How progress is read without scraping human output: the child runs discovery in-process under a custom
`TestResult` (with `buffer=True`, matching `-b`) that emits STRUCTURED events (a total, then one per
test completion) on a dedicated pipe. The parent reads that pipe for the heartbeat and never parses the
`-v` stream, whose format carries docstrings instead of ids and shifts across Python versions.

How draining stays deadlock- and hang-free: the parent drives ONE non-blocking `selectors` loop that
reads the child's combined output and the progress pipe as data arrives, firing the heartbeat on a
wall-clock tick. It never blocks in a read, so a full OS pipe can't stall it; and once the child exits
it drains only what is already buffered (a bounded, non-blocking sweep) rather than waiting on the pipe
to reach end-of-file — which a background grandchild process the child spawned could otherwise hold
open forever. That single-reader model is why teardown can never hang.

Layering: `quiet_call.py` silences one demo's stdout in-process at a single call site; this supervises
the whole run at the process level. Different layers — neither replaces the other. CI stays on the raw
`unittest discover ... -b` command (the merge gate is unchanged); this is the local build path.

Usage:
    uv run --directory .engine --frozen -- python tools/selftest.py
    uv run --directory .engine --frozen -- python tools/selftest.py --changed-from origin/main
    uv run --directory .engine --frozen -- python tools/selftest.py --run-record-path /tmp/record.json

Two flags are for you. `--changed-from` runs only the self-tests that the changes since a commit could
affect, falling back to the complete inventory for anything it cannot positively classify (see
selftest_select.py, which decides); it is an ITERATION aid — at most CANDIDATE evidence inside a
coordinated Build — and never merge evidence: the only merge path is the Build coordinator's final
import of the engine-ci proof. For any change that touches the Engine, CI still runs everything against
the exact submitted head; a DEPLOYED copy's change set that lies outside everything the Engine owns
selects only the standing guard here (scope `project-only`) and CI's project-only arm runs the validator
alone — the inventory then runs nowhere, which the record and the closing banner say in words.
`--run-record-path` writes a machine-readable record of what the run actually did — the complete
discovered inventory, the committed tree it ran over (nullable, with a dirty flag), what was selected
and why, per-module timings, failures, and skips. Both appear in `--help`.

Every OTHER flag is hidden and exists for the regression fixture (test_selftest.py), which drives the
launcher against tiny synthetic suites in a temp directory with a millisecond heartbeat; a normal run
needs none of them.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from typing import Optional

# Shared with release_gate.py: set on every nested in-process suite spawn so a suite run can never
# re-enter the real full-suite target from inside itself.
_NESTED_ENV = "ENGINE_NESTED_SELFTEST"

# tools/ sits directly under .engine/ — the canonical run is `discover -s tools` from `.engine`.
_ENGINE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(_ENGINE_DIR)   # the repo root, derived once from this file's own location

_DEFAULT_START_DIR = "tools"
_DEFAULT_PATTERN = "test_*.py"
_DEFAULT_HEARTBEAT_S = 30.0
_DEFAULT_STALL_S = 30.0
_POLL_INTERVAL_S = 0.25  # re-check child exit at least this often, independent of the heartbeat cadence
_MAX_SHOWN_TRACEBACKS = 12   # cap on inline tracebacks; the full log always holds every one
_SLOWEST_KEPT = 25           # slowest cases kept in the run record; the record is a summary, not a log
_TAIL_LINES = 80             # fallback echo when a failure produced no standard failure block


_LOG_PREFIX = "engine-selftest-"
_LOG_MAX_AGE_S = 86400  # sweep run logs older than a day


def _log_user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return str(os.getuid()) if hasattr(os, "getuid") else "user"


def _user_log_prefix() -> str:
    """The temp-log name prefix for THIS user — both the minter and the sweeper key on it, so the sweep
    only ever touches this user's own logs, never another user's on a shared temp dir."""
    return f"{_LOG_PREFIX}{_log_user()}-"


def _open_run_log(explicit: Optional[str]):
    """Open the run log. A caller-supplied path (the fixture) is honoured verbatim; otherwise a UNIQUE
    per-run file is minted with `mkstemp` — 0600 and O_EXCL, so concurrent runs by the same user never
    collide (this repo runs many sessions as one user) and no other user can read it or pre-plant the
    name as a symlink. Returns (path, open file); raises OSError only for an unwritable explicit path."""
    if explicit:
        return explicit, open(explicit, "w", encoding="utf-8", errors="replace")
    fd, path = tempfile.mkstemp(prefix=_user_log_prefix(), suffix=".log")
    return path, os.fdopen(fd, "w", encoding="utf-8", errors="replace")


def _sweep_stale_logs() -> None:
    """Best-effort removal of THIS user's own leftover run logs older than a day. Never raises — a
    cleanup that fails must not fail the run."""
    try:
        tmp = tempfile.gettempdir()
        prefix = _user_log_prefix()
        now = time.time()
        for name in os.listdir(tmp):
            if name.startswith(prefix) and name.endswith(".log"):
                path = os.path.join(tmp, name)
                try:
                    if now - os.path.getmtime(path) > _LOG_MAX_AGE_S:
                        os.remove(path)
                except OSError:
                    pass
    except OSError:
        pass


# --------------------------------------------------------------------------------------------------
# Child mode — discover and run in-process, emitting structured progress on a dedicated fd.
# --------------------------------------------------------------------------------------------------


def _case_module(test) -> str:
    """The module a case belongs to, taken from the case OBJECT rather than from its printed form.

    Two reasons never to parse `str(test)` for this. Its shape shifts across Python versions — the very
    instability that made this launcher use a structured channel instead of the `-v` stream — and a module
    that FAILS TO IMPORT is presented by unittest as `unittest.loader._FailedTest`, whose module attribute
    is the loader, not the module that broke. That second case is why the runner never filters on module
    alone: see `_filter_to_modules`."""
    cls = type(test)
    if cls.__name__ == "_FailedTest":
        # unittest parks the real module name in the method name of its synthetic failure case.
        return getattr(test, "_testMethodName", "") or "unittest.loader"
    holder = getattr(test, "description", None)
    if cls.__name__ == "_ErrorHolder" and isinstance(holder, str):
        # `setUpModule (pkg.mod)` / `setUpClass (pkg.mod.Class)` — the module is inside the parentheses.
        inner = holder.partition("(")[2].rpartition(")")[0]
        return inner or "unittest"
    return getattr(cls, "__module__", "") or "unittest"


class _StructuredResult(unittest.TextTestResult):
    """A normal buffered result (so failing-test output is replayed and passing noise is discarded,
    exactly like `-b`) that ALSO writes one structured JSON line per test completion to a progress fd,
    and accumulates the RUN RECORD from its own first-hand knowledge of each outcome. The verdict still
    comes from the base class's `wasSuccessful()`, never from what we emit.

    THE TWO JOBS ARE DELIBERATELY SEPARATE, and this is the correction of a design a reviewer caught.
    The progress fd is a best-effort LIVENESS channel: the parent never drains it after the child exits
    and discards its trailing partial line, which is exactly right for a heartbeat and silently lossy for
    a record — worst on a killed child, where the last events are the most diagnostic. So the record is
    built HERE, in the process that holds every outcome, and only the heartbeat rides the pipe. That also
    means there is one derivation of "what failed" instead of two that could disagree.

    Failures are taken from `addError`/`addFailure`, never from stop events, because a `setUpModule`
    failure is reported straight to `addError` and NEVER passes through `startTest` at all — a failure
    list built from stop events would be silently empty while the exit status said FAILED."""

    def __init__(self, *args, progress_write=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._progress_write = progress_write
        self._completed = 0
        self.module_times: dict = {}
        self.slowest: list = []
        self.problems: list = []
        self.skipped_count = 0
        self._record_broke = False
        self._started_at: dict = {}

    def _emit(self, payload: dict) -> None:
        if self._progress_write is None:
            return
        try:
            self._progress_write.write(json.dumps(payload) + "\n")
            self._progress_write.flush()
        except (ValueError, OSError):
            # A broken progress pipe (parent gone) must never break the run itself.
            self._progress_write = None

    def _note_problem(self, test, kind: str) -> None:
        # Guarded exactly as `_emit` is, and for the reason its comment gives: recording must never
        # break the run. A case whose own `__str__` raised would otherwise propagate out of the result
        # hooks and abort the WHOLE run rather than that one case.
        try:
            self.problems.append({"kind": kind, "id": str(test), "module": _case_module(test)})
        except Exception:                          # noqa: BLE001 - recording must not cost a verdict
            self._record_broke = True

    def startTest(self, test):
        super().startTest(test)
        self._started_at[id(test)] = time.monotonic()
        self._emit({"event": "start", "id": str(test)})

    def stopTest(self, test):
        super().stopTest(test)
        self._completed += 1
        began = self._started_at.pop(id(test), None)
        if began is not None:
            try:
                elapsed = time.monotonic() - began
                module = _case_module(test)
                slot = self.module_times.setdefault(module, {"seconds": 0.0, "cases": 0})
                slot["seconds"] += elapsed
                slot["cases"] += 1
                self.slowest.append({"id": str(test), "module": module,
                                     "seconds": round(elapsed, 4)})
            except Exception:                      # noqa: BLE001 - recording must not cost a verdict
                self._record_broke = True
        self._emit({"event": "stop", "completed": self._completed, "id": str(test)})

    def addError(self, test, err):
        super().addError(test, err)
        self._note_problem(test, "error")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._note_problem(test, "failure")

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.skipped_count += 1


RECORD_SCHEMA_VERSION = "selftest-run-record.v1"
# The record attests exactly ONE of the two validation commands the Build protocol registers. A
# reviewer's point, and a sharp one: a documentation-only change legitimately runs no self-tests while
# still being able to break the validator suite, so a record whose verdict said "passed" with nothing
# naming its scope would read as "this tree is validated". It names what it covered and stays silent
# about the rest.
RECORD_ATTESTS = "engine-selftest"


def _sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _atomic_write_json(path: str, payload: dict) -> bool:
    """Write one JSON document, replacing any previous one indivisibly. Returns False on any failure and
    NEVER raises: a record that cannot be written must not change the run's verdict."""
    tmp = f"{path}.{os.getpid()}.partial"
    try:
        # O_EXCL|O_NOFOLLOW and 0600: never follow a symlink planted at the temporary name, never
        # adopt one already there, and never widen the mode by the caller's umask.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _read_selection(path):
    """The selection manifest the parent computed, or None for an ordinary full run."""
    return _read_json(path) if path else None


def _selection_digest(selection):
    """The selection manifest's own digest, from the SELECTOR's canonical serializer rather than a second
    copy of it here. A nested JSON object is not byte-preserved, so this is the only thing that can carry
    the embedded copy's identity across the boundary — which is what a reviewer actually asked for when
    they flagged the nesting. Null rather than a guess if it cannot be computed."""
    if selection is None:
        return None
    try:
        import selftest_select
        return selftest_select.digest(selection)
    except Exception:                              # noqa: BLE001 - a record must never fail the run
        return None


def _tree_binding(start_dir) -> dict:
    """The committed tree this record describes, and whether the working tree had drifted from it.

    `tree` is `git rev-parse HEAD^{tree}` resolved FROM THE SUITE'S START DIRECTORY — the tree the
    record is an account of, not the tool's own home; a fixture run against a synthetic suite in a
    temp directory therefore binds nothing rather than falsely binding the engine's tree. Null
    exactly when no repository or committed tree is derivable there. `worktree_dirty` reads the same
    porcelain the selector reads (`--untracked-files=all`, so a new file inside a new directory
    counts) and is null exactly when `tree` is: the dirtiness of a tree that could not be resolved is
    not a fact this record can state.

    One helper, called by EVERY writer of the record — the required-field-in-one-writer-of-three
    mistake was made twice in the slice that built this record, and a shared derivation is the shape
    that prevents the third. Best-effort by the same rule as everything else here: a binding that
    cannot be computed must never fail the run, so any failure is (None, None), recorded rather than
    guessed. A consumer gating on this record refuses null or dirty and compares the tree against an
    identity it derived itself; the record cannot authenticate its own claim."""
    try:
        proc = subprocess.run(["git", "-C", start_dir, "rev-parse", "HEAD^{tree}"],
                              capture_output=True, text=True, timeout=30)
        tree = proc.stdout.strip()
        if proc.returncode != 0 or len(tree) != 40 or any(c not in "0123456789abcdef" for c in tree):
            return {"tree": None, "worktree_dirty": None}
        status = subprocess.run(["git", "-C", start_dir, "status", "--porcelain=v1",
                                 "--untracked-files=all"],
                                capture_output=True, text=True, timeout=60)
        if status.returncode != 0:
            return {"tree": None, "worktree_dirty": None}
        return {"tree": tree, "worktree_dirty": bool(status.stdout.strip())}
    except (OSError, subprocess.SubprocessError):
        return {"tree": None, "worktree_dirty": None}


def _write_run_record(args, outcome: dict, started: float, *, inventory=None, selection=None,
                      scope="full", result=None, executed=None) -> None:
    """The child's own account of what it ran. Best-effort by construction — see `_atomic_write_json`."""
    path = getattr(args, "run_record_path", None)
    if not path:
        return
    modules, ids = inventory if inventory else ([], [])
    record = {
        "schema_version": RECORD_SCHEMA_VERSION,
        "attests": RECORD_ATTESTS,
        "scope": scope,
        "verdict": outcome["verdict"],
        "detail": outcome["detail"],
        "exit_status": None,
        "started_at": round(started, 3),
        "finished_at": round(time.time(), 3),
        "inventory": {
            "module_count": len(modules),
            "case_count": len(ids),
            "modules_digest": _sha256_text("\n".join(modules)),
            "cases_digest": _sha256_text("\n".join(ids)),
        },
        "executed": {
            "case_count": executed if executed is not None else 0,
            "skipped_count": getattr(result, "skipped_count", 0) if result is not None else 0,
        },
        "selection": selection,
        "selection_digest": _selection_digest(selection),
        "nested_sentinel": bool(os.environ.get(_NESTED_ENV)),
        "modules": sorted(
            ({"module": m, "seconds": round(v["seconds"], 4), "cases": v["cases"]}
             for m, v in (getattr(result, "module_times", {}) or {}).items()),
            key=lambda e: e["module"]),
        "slowest": sorted(getattr(result, "slowest", []) or [],
                          key=lambda e: (-e["seconds"], e["id"]))[:_SLOWEST_KEPT],
        "problems": sorted(getattr(result, "problems", []) or [],
                           key=lambda e: (e["module"], e["id"])),
        # Surfaced, not swallowed. The guards around the new bookkeeping turned a loud failure into a
        # silently partial record, and the flag they set was read nowhere — so a record could
        # under-report in the artifact this slice calls the honesty record, with no trace at all.
        "record_incomplete": bool(getattr(result, "_record_broke", False)),
        "log": None,
        **_tree_binding(args.start_dir),
    }
    _atomic_write_json(path, record)


def _finish_run_record(path, rc: int, log_path, captured: str, *, scope="full", selection=None,
                       start_dir=None) -> None:
    """The parent's postscript: the VERBATIM exit status, and the log digest.

    The digest is taken from the parent's in-memory capture rather than by re-reading the file. The two
    are byte-identical by construction — the same decoded text is written to both — and this way the
    digest does not depend on a buffered flush having completed during teardown, which is precisely the
    condition (a full disk) under which the record matters most.

    If the child never wrote a record — it was killed, or died before it could — the parent writes one
    here, so a crashed run still leaves an account of itself."""
    if not path:
        return
    record = _read_json(path)
    if record is None:
        # The parent knows the scope even when the child died before saying so. Defaulting this to
        # "full" made a crashed focused run claim the complete inventory had run — the exact field the
        # schema calls its load-bearing honesty field.
        record = {
            "schema_version": RECORD_SCHEMA_VERSION,
            "attests": RECORD_ATTESTS,
            "scope": scope,
            "verdict": "crashed",
            "detail": "the run ended before it could write its own record",
            "started_at": None, "finished_at": round(time.time(), 3),
            "inventory": {"module_count": 0, "case_count": 0,
                          "modules_digest": _sha256_text(""), "cases_digest": _sha256_text("")},
            "executed": {"case_count": 0, "skipped_count": 0},
            "record_incomplete": False,   # the child never got far enough to collect anything
            "selection": selection, "selection_digest": _selection_digest(selection),
            # True by construction: this record is written by the PARENT, which sets the sentinel for
            # the child it spawns. Reading the parent's own environment reported False on every
            # ordinary run — a regression that rode in on the scope fix, that nothing asked for, and
            # that contradicted the schema's own statement about this field.
            "nested_sentinel": True,
            "modules": [], "slowest": [], "problems": [],
            # The parent still knows which tree the crashed run was pointed at, so a crashed record
            # binds (or honestly declines to bind) exactly as a completed one does. A caller that
            # cannot name the start directory gets the null binding, never a guess from the cwd.
            **(_tree_binding(start_dir) if start_dir is not None
               else {"tree": None, "worktree_dirty": None}),
        }
    record["exit_status"] = rc
    record["log"] = {"path": log_path, "sha256": _sha256_text(captured)} if log_path else None
    _atomic_write_json(path, record)


def _flatten(suite):
    """Every leaf case in a discovered suite, in discovery order."""
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


def _inventory(suite) -> tuple:
    """(sorted module names, sorted case ids) for the WHOLE discovered tree, before any filtering.

    Taken from the canonical full discovery so the recorded inventory is the real inventory even on a
    focused run — which is what lets a later reader tell a focused run from a full one, and is half of
    what makes a focused record unusable as merge evidence."""
    modules, ids = set(), []
    for case in _flatten(suite):
        modules.add(_case_module(case))
        ids.append(str(case))
    return sorted(modules), sorted(ids)


def _filter_to_modules(suite, selected: frozenset):
    """Keep the cases belonging to `selected` — plus, ALWAYS, every collection error, whatever the
    selection says.

    That exception is load-bearing, not defensive tidiness. A test module that fails to IMPORT is
    presented by unittest as a synthetic case whose printed form attributes it to `unittest.loader`; a
    filter that dropped it would let the filtered suite run clean and the child exit 0 — an unearned
    green in exactly the mode a build loop is meant to trust.

    A collection error also COUNTS AS A MATCH for its own module, and that half was wrong at first. Two
    reviewers hit the same case independently: break the import in the very file you are editing, and the
    module is selected, kept in the suite — and then reported as one the tree "does not produce", with the
    run abandoned before it could show the actual ImportError. The module plainly exists; it is broken.
    `_case_module` recovers the real module name from the synthetic case, so counting it as matched lets
    the suite run and surface the real traceback, which is the whole point of not filtering it out.

    Returns (filtered suite, set of selected modules that matched at least one case)."""
    kept, matched = [], set()
    for case in _flatten(suite):
        module = _case_module(case)
        if type(case).__name__ in ("_FailedTest", "_ErrorHolder"):
            kept.append(case)                       # never filtered out, whatever was selected
            if module in selected:
                matched.add(module)                 # it exists and is broken — let the run say so
            continue
        if module in selected:
            kept.append(case)
            matched.add(module)
    return unittest.TestSuite(kept), matched


def _run_child(args: argparse.Namespace) -> int:
    """Discover and run in-process; return 0 on success, non-zero otherwise. A discovery/import failure
    is surfaced as a non-zero exit, never swallowed.

    Exit statuses: 0 green, 1 a test failed OR discovery failed, 2 a focused run could not be set up
    honestly — a selection naming modules this tree does not produce, or naming none at all. Discovery
    failure is deliberately 1, not 2: it is on the DEFAULT path, whose exit statuses this change must
    leave untouched. Only the paths that did not exist before use 2."""
    progress_write = None
    if args.progress_fd is not None and args.progress_fd >= 0:
        progress_write = os.fdopen(args.progress_fd, "w", buffering=1)

    def _progress(payload: dict) -> None:
        nonlocal progress_write
        if progress_write is None:
            return
        try:
            progress_write.write(json.dumps(payload) + "\n")
            progress_write.flush()
        except (ValueError, OSError):
            progress_write = None

    started = time.time()
    loader = unittest.TestLoader()
    try:
        suite = loader.discover(start_dir=args.start_dir, pattern=args.pattern)
    except Exception as exc:  # pragma: no cover - defensive; discover usually defers import errors
        _progress({"event": "total", "total": 0})
        print(f"selftest: discovery failed: {exc!r}", file=sys.stderr)
        _write_run_record(args, {"verdict": "setup-failed", "detail": repr(exc)}, started)
        # Deliberately 1, not 2. This branch is on the DEFAULT path, and the obligation to leave the
        # default invocation's exit status untouched outranks the tidier "2 means setup failure"
        # reading — a reviewer caught the drift. The 2s below are on paths that did not exist before.
        return 1

    inventory_modules, inventory_ids = _inventory(suite)
    selection = _read_selection(args.selection_path)
    scope = "full"
    unmatched: list = []
    if selection is not None and selection.get("classification") in ("focused", "project-only"):
        # `project-only` is a focused run whose every considered path was a deployed project's own, so
        # what runs is the standing guard alone; the scope keeps that name so the record never reads as
        # an ordinary narrowing.
        scope = selection["classification"]
        wanted = frozenset(entry["module"] for entry in selection.get("selected", ()))
        if not wanted:
            # The SELECTOR cannot produce this — a run is focused only when something was positively
            # selected. The RUNNER can still be handed it, and an empty filtered suite is reported by
            # unittest as successful, so the guard belongs here too rather than only upstream.
            print("selftest: refusing a focused selection that names no module — an empty suite would "
                  "report success having run nothing.", file=sys.stderr)
            _write_run_record(args, {"verdict": "selection-unmatched",
                                     "detail": "the focused selection named no module"}, started,
                              inventory=(inventory_modules, inventory_ids), selection=selection,
                              scope=scope)
            return 2
        suite, matched = _filter_to_modules(suite, wanted)
        unmatched = sorted(wanted - matched)
        if unmatched:
            # A non-empty selection that matches nothing discovery produced would otherwise run an EMPTY
            # suite, and unittest reports an empty suite as successful — a clean green having run nothing.
            print("selftest: the selection names module(s) this tree does not produce: "
                  + ", ".join(unmatched), file=sys.stderr)
            _write_run_record(args, {"verdict": "selection-unmatched", "detail": unmatched}, started,
                              inventory=(inventory_modules, inventory_ids), selection=selection,
                              scope=scope)
            return 2

    total = suite.countTestCases()
    # The heartbeat's denominator is what THIS RUN will execute, so a focused run's counter still
    # converges. The complete inventory count lives in the run record under its own name; conflating the
    # two would make a focused run's progress line look like a hang, which is what this launcher exists
    # to prevent.
    _progress({"event": "total", "total": total})

    def _factory(stream, descriptions, verbosity):
        return _StructuredResult(stream, descriptions, verbosity, progress_write=progress_write)

    runner = unittest.TextTestRunner(stream=sys.stderr, verbosity=1, buffer=True, resultclass=_factory)
    try:
        result = runner.run(suite)
    finally:
        if progress_write is not None:
            try:
                progress_write.close()
            except OSError:
                pass
    rc = 0 if result.wasSuccessful() else 1
    _write_run_record(args, {"verdict": "passed" if rc == 0 else "failed", "detail": None}, started,
                      inventory=(inventory_modules, inventory_ids), selection=selection, scope=scope,
                      result=result, executed=total)
    return rc


# --------------------------------------------------------------------------------------------------
# Parent mode — spawn the child, drain non-blocking, heartbeat on a timer, propagate exit verbatim.
# --------------------------------------------------------------------------------------------------


class _Progress:
    """The child's progress, updated by the single reader loop and read by the heartbeat. Single
    reader → no lock needed."""

    def __init__(self) -> None:
        self.total: Optional[int] = None
        self.completed = 0
        self.current: Optional[str] = None
        self.last_completion = time.monotonic()

    def apply(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        kind = event.get("event")
        if kind == "total":
            self.total = int(event.get("total", 0))
        elif kind == "start":
            self.current = str(event.get("id", ""))
        elif kind == "stop":
            self.completed = int(event.get("completed", 0))
            self.last_completion = time.monotonic()

    def snapshot(self, now: float) -> dict:
        return {"total": self.total, "completed": self.completed,
                "current": self.current, "since_last": now - self.last_completion}


def _forward_signal(proc, signum) -> None:
    """Forward `signum` to the child's whole process group — or, while no child exists yet, do what the
    default disposition would have done (SIGINT interrupts, SIGTERM terminates with the conventional
    128+signal status). The handlers are installed before the spawn so the child inherits a caught handler
    (reset to default across exec) rather than an ignore (preserved across exec); this pre-spawn branch keeps
    that window from swallowing a signal or leaving a launcher nothing can stop
    (StarshipSuperjam/engine-template#1188)."""
    if proc is None:
        if signum == signal.SIGINT:
            raise KeyboardInterrupt
        raise SystemExit(128 + int(signum))
    try:
        os.killpg(os.getpgid(proc.pid), signum)
    except (ProcessLookupError, PermissionError):
        pass


def _heartbeat_line(snap: dict, elapsed: float, stall_threshold: float) -> str:
    total_s = str(snap["total"]) if snap["total"] is not None else "?"
    current = snap["current"] or "(starting)"
    line = (f"  … {snap['completed']}/{total_s} tests  |  {elapsed:0.0f}s elapsed  |  "
            f"{snap['since_last']:0.0f}s since last completion  |  now: {current}")
    if snap["since_last"] > stall_threshold:
        # Can't tell a slow-but-alive test from a stall without mid-test signal — say so honestly,
        # and name the test in hand (above) so the reader can judge.
        line += "  |  ⚠ slow or possibly stalled"
    return line


def _extract_failure_blocks(lines: list) -> list:
    """Pull each `FAIL:`/`ERROR:` block (id + traceback) out of the child's unittest output. Display
    only — the verdict never depends on this."""
    def is_sep(s: str) -> bool:
        t = s.strip()
        return len(t) >= 20 and set(t) == {"="}

    def is_dash(s: str) -> bool:
        t = s.strip()
        return len(t) >= 20 and set(t) == {"-"}

    blocks = []
    i, n = 0, len(lines)
    while i < n:
        if is_sep(lines[i]) and i + 1 < n and (lines[i + 1].startswith("FAIL:") or lines[i + 1].startswith("ERROR:")):
            header = lines[i + 1].strip()
            block = [lines[i + 1]]
            j = i + 2
            while j < n:
                # End only at a real NEXT block or the final summary trailer — a stray banner of `=`
                # or `-` inside a traceback's own text must not cut the block short.
                if is_sep(lines[j]) and j + 1 < n and (lines[j + 1].startswith("FAIL:") or lines[j + 1].startswith("ERROR:")):
                    break
                if is_dash(lines[j]) and j + 1 < n and lines[j + 1].startswith("Ran "):
                    break
                block.append(lines[j])
                j += 1
            blocks.append((header, block))
            i = j
        else:
            i += 1
    return blocks


def _print_result(rc: int, elapsed: float, log_path: Optional[str], output: str,
                  scope_note: Optional[str] = None) -> None:
    print()
    print("=" * 78)
    verdict = "PASSED" if rc == 0 else f"FAILED (exit {rc})"
    print(f"Self-tests {verdict} in {elapsed:0.0f}s")
    if scope_note:
        # The closing banner is what a reader actually sees after a long run — the opening announcement
        # is hundreds of lines up the buffer, and this launcher's own docstring says output here is
        # routinely read through `tail`. An unqualified "PASSED" on a focused run is the one place the
        # qualifier could silently drop off, so it is repeated where the verdict is.
        print(scope_note)
    if log_path:  # always set now — the log is kept for every run, and its path is printed so the session can read it
        print(f"Full output: {log_path}")
    if rc == 0:
        print("=" * 78)
        return

    # Parse the run's OWN captured output — never re-read the log file, so a concurrent run writing a
    # different file (or this run's log being swept) can never make this result show the wrong failures.
    lines = output.splitlines()
    blocks = _extract_failure_blocks(lines)
    print("-" * 78)
    if blocks:
        # The complete list of what failed always prints — even at 50 failures it is 50 short lines.
        print(f"Failing tests ({len(blocks)}):")
        for header, _ in blocks:
            print(f"  {header}")
        print("-" * 78)
        for idx, (_, block) in enumerate(blocks):
            if idx >= _MAX_SHOWN_TRACEBACKS:
                print(f"… and {len(blocks) - idx} more failing test(s) — full tracebacks in the log above.")
                break
            for ln in block:
                print(ln)
            print()
    else:
        # No standard failure block (a killed/crashed child, or a failure before any test) — the exit
        # code already told the truth; show the tail so the cause is still visible.
        print("The run failed without a standard failure list; last lines of the output:")
        for ln in lines[-_TAIL_LINES:]:
            print(ln)
    print("=" * 78)


def _final_drain(fd: int, log_file, captured: list, deadline_s: float = 2.0) -> None:
    """After the child exits, sweep up its already-buffered output without blocking. A background
    grandchild can hold the pipe open forever, so this never waits on EOF — it stops as soon as no
    more data is immediately available, or a short deadline passes."""
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        try:
            chunk = os.read(fd, 65536)
        except BlockingIOError:
            return
        except OSError:
            return
        if not chunk:
            return
        text = chunk.decode("utf-8", "replace")
        log_file.write(text)
        log_file.flush()
        captured.append(text)


def _compute_selection(changed_from: str) -> dict:
    """Ask the selector what the changes since `changed_from` can affect.

    Imported lazily and defensively: the selector is a normal in-repo tool, but a run that cannot import
    it must fall back to the complete inventory rather than fail — the failure direction this whole
    design keeps is toward more work, never toward less."""
    try:
        import selftest_select
        return selftest_select.select(_REPO_ROOT, changed_from)
    except Exception as exc:                       # noqa: BLE001 - any failure means "run everything"
        return {
            "schema_version": "selftest-selection.v1",
            "classification": "full",
            "changed_from": changed_from,
            "changed_paths": [],
            # Not `git-unavailable`: that code means a git command failed, and filing an import error
            # or a broken register under it sends a reader looking at the wrong thing.
            "full_reason": {"code": "selector-unavailable",
                            "detail": f"the selector could not run ({exc!r}); running everything"},
            "exempt_paths": [],
            "selected": [],
        }


def _cleanup_selection(selection_path, caller_supplied) -> None:
    """Remove the manifest we minted for the child. Never one the caller handed us, and never so early
    that the run record cannot still embed it. The log sweeper matches only `*.log`, so a manifest left
    behind on an error path would never be collected by anything."""
    if selection_path and selection_path != caller_supplied:
        try:
            os.remove(selection_path)
        except OSError:
            pass


def _discoverable_module_count(start_dir: str):
    """How many test modules the tree holds, for the focused announcement's denominator.

    Anchored the way the child's discovery is — against the engine directory derived from this file's
    own location, never the parent's incidental working directory. Best-effort: a count that cannot be
    taken goes unmentioned rather than failing the run."""
    try:
        count = 0
        for dirpath, dirnames, filenames in os.walk(start_dir):
            dirnames[:] = [d for d in dirnames if d != "__pycache__" and not d.startswith(".")]
            count += sum(1 for f in filenames
                         if f.startswith("test_") and f.endswith(".py"))
        return count or None
    except OSError:
        return None


def _not_started_record(args, verdict: str, detail: str) -> None:
    """A record for the three parent-side exits the child never reaches. Their existence is the reason
    the promise is 'every outcome where the parent survives to return' rather than 'every outcome': a
    parent killed by an unblockable signal writes nothing, and no placement can change that."""
    path = getattr(args, "run_record_path", None)
    if not path:
        return
    _atomic_write_json(path, {
        "schema_version": RECORD_SCHEMA_VERSION,
        "attests": RECORD_ATTESTS,
        "scope": "full",
        "verdict": verdict,
        "detail": detail,
        "exit_status": 2,
        "started_at": None,
        "finished_at": round(time.time(), 3),
        "inventory": {"module_count": 0, "case_count": 0,
                      "modules_digest": _sha256_text(""), "cases_digest": _sha256_text("")},
        "executed": {"case_count": 0, "skipped_count": 0},
        "record_incomplete": False,   # nothing was collected, so nothing is missing from it
        "selection": None,
        "selection_digest": None,
        # True for the same reason as the crash record: the launcher sets the sentinel for the child it
        # spawns, so reading the parent's own environment reported False on every ordinary run. The
        # earlier fix corrected one of the two parent-written record paths and left this one.
        "nested_sentinel": True,
        "modules": [], "slowest": [], "problems": [],
        "log": None,
        **_tree_binding(args.start_dir),
    })


def _run_parent(args: argparse.Namespace) -> int:
    start_dir = args.start_dir
    real_target = os.path.abspath(start_dir) == os.path.abspath(_DEFAULT_START_DIR) or start_dir == _DEFAULT_START_DIR
    if real_target and os.environ.get(_NESTED_ENV):
        print("selftest: refusing to run the real full suite while nested "
              f"({_NESTED_ENV} is set) — this would recurse.", file=sys.stderr)
        _not_started_record(args, "nested-refusal", "refused to re-enter the real suite from inside it")
        return 2

    _sweep_stale_logs()
    try:
        log_path, log_file = _open_run_log(args.log_path)
    except OSError as exc:
        print(f"selftest: cannot open the run log at {args.log_path}: {exc}", file=sys.stderr)
        _not_started_record(args, "log-unavailable", f"cannot open the run log: {exc}")
        return 2

    # An explicitly supplied selection wins: that is the fixture's seam, letting a test hand in a
    # manifest without constructing a git repository to derive one from.
    scope_note = None
    scope_name = "full"
    selection_path = args.selection_path
    if selection_path is None and args.changed_from:
        # Minted the way the run log is, for the reason the log's own docstring already gives: 0600
        # and O_EXCL, so no other user can read it or pre-plant the name as a symlink. The first
        # version composed a predictable name from the process id and opened it with a plain write,
        # which follows symlinks — one guess per pid, in a world-writable directory, in the very
        # module that had already rejected that posture for its neighbour.
        fd, selection_path = tempfile.mkstemp(prefix="engine-selftest-selection-", suffix=".json",
                                              dir=os.path.dirname(log_path))
        os.close(fd)
        _atomic_write_json(selection_path, _compute_selection(args.changed_from))
    if selection_path:
        # Announce whichever way it went, and say WHY on a fallback. A session that asked to narrow and
        # silently got the whole inventory would otherwise have no idea the selector declined.
        manifest = _read_json(selection_path) or {}
        if manifest.get("classification") == "project-only":
            picked = len(manifest.get("selected", ()))
            owned = len(manifest.get("project_paths", ()))
            print(f"Project-only run: {owned} changed path(s) lie outside everything the Engine owns, so "
                  f"only the standing derived-artifact guard ({picked} module(s)) runs.", flush=True)
            scope_note = (f"Project-only run — {picked} guard module(s) ran; the Engine self-test inventory "
                          f"did not run for this change set. Engine health only: no product validation "
                          f"is registered (StarshipSuperjam/engine-template#1147). "
                          f"This is NOT a full-inventory result.")
            scope_name = "project-only"
        elif manifest.get("classification") == "focused":
            scope_name = "focused"
            picked = len(manifest.get("selected", ()))
            # Say the proportion, not just the count. Editing a leaf tool selects a couple of modules;
            # editing a widely-imported one can select most of the tree, and both used to be announced
            # in identical words — leaving a session unable to tell a cheap run from a near-full one
            # until it watched the clock.
            available = _discoverable_module_count(
                start_dir if os.path.isabs(start_dir)
                else os.path.join(args.cwd or _ENGINE_DIR, start_dir))
            of_total = f" of {available}" if available else ""
            print(f"Focused run: {picked}{of_total} self-test module(s) selected by the changes since "
                  f"{manifest.get('changed_from')}.", flush=True)
            scope_note = f"Focused run — {picked}{of_total} module(s) ran; this is NOT a full-inventory result."
        else:
            reason = (manifest.get("full_reason") or {}).get("detail", "no selection was possible")
            print(f"Running the COMPLETE inventory: {reason}", flush=True)

    progress_read_fd, progress_write_fd = os.pipe()
    child_cmd = [
        sys.executable, os.path.abspath(__file__), "--child",
        "--start-dir", start_dir,
        "--pattern", args.pattern,
        "--progress-fd", str(progress_write_fd),
    ]
    if args.run_record_path:
        child_cmd += ["--run-record-path", os.path.abspath(args.run_record_path)]
    if selection_path:
        child_cmd += ["--selection-path", selection_path]
    # Ambient qualification OFF for the whole suite. It reaches live GitHub and writes activation state into
    # the repository's Git common directory, so a test that exercises the SessionStart handler would qualify
    # the developer's own machine as a side effect of running the tests. A test that wants the seam ON turns
    # it on for itself; see boot.AMBIENT_QUALIFICATION_OFF_ENV.
    env = {**os.environ, _NESTED_ENV: "1", "ENGINE_AMBIENT_QUALIFICATION_OFF": "1"}

    progress = _Progress()
    captured: list = []   # the run's own output, held in memory for a concurrency-safe result printout
    start = time.monotonic()
    previous_handlers: dict = {}
    sel = selectors.DefaultSelector()
    proc = None
    try:
        # The forwarding handlers go in BEFORE the child is spawned (StarshipSuperjam/engine-template#1188).
        # A signal disposition inherits across exec, and the two kinds inherit differently: a CAUGHT handler
        # resets to the default in the new program, while an IGNORE is preserved. A POSIX shell starts a
        # `cmd &` job with SIGINT ignored, so a launcher that spawned first handed that ignore straight to the
        # suite, and the SIGINT it later forwarded reached a child that could not be interrupted — a
        # backgrounded validation run could not be stopped, and the teardown test read as a launcher hang.
        # With a handler already installed, the child starts with SIGINT at its default however this
        # launcher itself was started.
        def _forward(signum, _frame):
            _forward_signal(proc, signum)

        for sig in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[sig] = signal.signal(sig, _forward)

        try:
            proc = subprocess.Popen(
                child_cmd,
                cwd=args.cwd or _ENGINE_DIR,
                env=env,
                stdin=subprocess.DEVNULL,  # end-of-input, so no demo blocks on stdin under a real tty
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # one combined stream to drain
                pass_fds=(progress_write_fd,),
                start_new_session=True,    # own process group, so teardown reaches demo grandchildren
            )
        except OSError as exc:
            print(f"selftest: failed to start the suite: {exc}", file=sys.stderr)
            _not_started_record(args, "spawn-failed", f"failed to start the suite: {exc}")
            _cleanup_selection(selection_path, args.selection_path)
            os.close(progress_write_fd)  # the `finally` closes the read fd and the log
            # The suite never started, so the log is empty — the sole exception to the otherwise-always-
            # keep rule: there is nothing to read, and this run reports its failure loudly via the exit
            # code and the message above, so a dropped empty log can never read as a vanished one.
            if not args.log_path:
                try:
                    os.remove(log_path)
                except OSError:
                    pass
            return 2

        os.close(progress_write_fd)  # parent holds only the read end
        # Announce the log path only now the child is running, so an announced path always names a file
        # that will be kept. A run that never started (the branch above) reports its failure loudly on
        # stderr instead, and so never leaves a session hunting for a log it was promised.
        print(f"Running the self-test suite (log: {log_path})", flush=True)
        out_fd = proc.stdout.fileno()
        os.set_blocking(out_fd, False)
        os.set_blocking(progress_read_fd, False)
        sel.register(out_fd, selectors.EVENT_READ, "out")
        sel.register(progress_read_fd, selectors.EVENT_READ, "prog")

        prog_buf = ""
        registered = {"out", "prog"}
        next_beat = start + args.heartbeat_interval
        while True:
            # Cap the wait so the child's exit is noticed promptly (within _POLL_INTERVAL_S) even when
            # no fd is readable — a background grandchild holding the pipe open means the "out" fd never
            # signals again, and gating exit-detection on the full heartbeat interval would look hung.
            timeout = min(_POLL_INTERVAL_S, max(0.0, next_beat - time.monotonic()))
            for key, _ in sel.select(timeout=timeout):
                try:
                    chunk = os.read(key.fd, 65536)
                except BlockingIOError:
                    continue
                except OSError:
                    chunk = b""
                if not chunk:  # this stream reached EOF
                    sel.unregister(key.fd)
                    registered.discard(key.data)
                    continue
                if key.data == "out":
                    text = chunk.decode("utf-8", "replace")
                    log_file.write(text)
                    log_file.flush()
                    captured.append(text)
                else:
                    prog_buf += chunk.decode("utf-8", "replace")
                    parts = prog_buf.split("\n")
                    prog_buf = parts.pop()
                    for line in parts:
                        progress.apply(line)

            now = time.monotonic()
            if now >= next_beat:
                print(_heartbeat_line(progress.snapshot(now), now - start, args.stall_threshold), flush=True)
                next_beat = now + args.heartbeat_interval

            if proc.poll() is not None:
                if "out" in registered:
                    _final_drain(out_fd, log_file, captured)
                break

        rc = proc.returncode
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
        sel.close()
        if proc is not None:
            if proc.poll() is None:
                # Leaving with the child still alive (an unexpected error mid-loop) — never orphan it
                # or the demo grandchildren it may have started; escalate to SIGKILL if it clings on.
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
            try:
                proc.stdout.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
                try:
                    proc.wait(timeout=5)
                except (subprocess.TimeoutExpired, OSError):
                    pass
            except OSError:
                pass
        try:
            os.close(progress_read_fd)
        except OSError:
            pass
        log_file.close()

    # The log is kept for every run — pass or fail — so a session can always read its own run and never
    # reads a vanished log as a failure. Cleanup is the daily sweep (_sweep_stale_logs) at the next run.
    elapsed = time.monotonic() - start
    output = "".join(captured)
    _finish_run_record(args.run_record_path, rc, log_path, output,
                       scope=scope_name,
                       selection=_read_json(selection_path) if selection_path else None,
                       start_dir=args.start_dir)
    _cleanup_selection(selection_path, args.selection_path)
    _print_result(rc, elapsed, log_path, output, scope_note)
    return rc  # VERBATIM — the child's exit status is the launcher's verdict.


# --------------------------------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run the full self-test suite once, legibly.")
    p.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--progress-fd", type=int, default=None, help=argparse.SUPPRESS)
    p.add_argument("--start-dir", default=_DEFAULT_START_DIR, help=argparse.SUPPRESS)
    p.add_argument("--pattern", default=_DEFAULT_PATTERN, help=argparse.SUPPRESS)
    p.add_argument("--cwd", default=None, help=argparse.SUPPRESS)
    p.add_argument("--heartbeat-interval", type=float,
                   default=float(os.environ.get("ENGINE_SELFTEST_HEARTBEAT_S", _DEFAULT_HEARTBEAT_S)),
                   help=argparse.SUPPRESS)
    p.add_argument("--stall-threshold", type=float,
                   default=float(os.environ.get("ENGINE_SELFTEST_STALL_S", _DEFAULT_STALL_S)),
                   help=argparse.SUPPRESS)
    p.add_argument("--log-path", default=None, help=argparse.SUPPRESS)
    p.add_argument("--changed-from", default=None, metavar="COMMIT",
                   help="run only the self-tests that changes since COMMIT could affect. Anything the "
                        "selector cannot positively classify runs the complete inventory instead. An "
                        "iteration aid, never merge evidence.")
    p.add_argument("--run-record-path", default=None, metavar="PATH",
                   help="write a machine-readable record of this run to PATH: the complete discovered "
                        "inventory, what was selected and why, per-module timings, failures and skips.")
    # The parent computes the selection and hands the child this file. A typed artifact rather than a
    # widening argv, so the child can report per-module match results back instead of filtering silently.
    p.add_argument("--selection-path", default=None, help=argparse.SUPPRESS)
    return p


def main(argv: Optional[list] = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.child:
        return _run_child(args)
    return _run_parent(args)


if __name__ == "__main__":
    sys.exit(main())
