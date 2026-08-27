"""Low-level, repository-local primitives for the Build coordinator.

This module knows JSON, git, private artifacts, and atomic snapshot storage. It
does not know plans, reviewers, specifications, GitHub workflow, or CLI phases.

Two stores ride these primitives: the Build coordinator's own `StateStore`
(deliberately non-durable, in OS temp, one Build's current facts) and the Plan
Coordinator's durable local library (`plan_store`). They differ in exactly the
ways they should — where they may live, and how hard they try to survive a
power cut — and agree on everything else, because the locking, the
compare-and-swap, and the atomic-replace idiom are free functions here rather
than a second copy that drifts.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import tempfile
import time
from typing import Any, Callable


class CoordinatorError(Exception):
    pass


# The platform durability barrier. On Darwin a bare os.fsync returns once the bytes reach the drive's
# write cache, which a power cut can still lose; fcntl.F_FULLFSYNC forces them to stable storage.
# Absent on other platforms, where os.fsync is the floor.
#
# The memory ledger holds an equivalent private pair, and that duplication is deliberate: the ledger
# belongs to the OPTIONAL memory-substrate module, whose files are DELETED when an operator declines
# it, and core may not depend on something that can be uninstalled. Six lines of platform primitive
# is the right price for that independence; the payload validator, which had a real drift risk, was
# genuinely shared instead.
_F_FULLFSYNC = getattr(fcntl, "F_FULLFSYNC", None)


def durable_fsync(fd) -> bool:
    """Flush `fd` to stable storage as durably as the platform allows. True when a flush succeeded.

    Guarded throughout: an fsync fault must never crash past a caller's lock release, so it degrades
    rather than aborting a critical section that still leaves intact data on disk. But degrading
    SILENTLY is worse than either — a full disk or a failing drive would then be indistinguishable
    from a durable write, and the caller would report success for data that is not on the platter.
    So the outcome is returned, and `atomic_write(durable=True)` turns a total failure into a visible
    one.
    """
    if _F_FULLFSYNC is not None:
        try:
            fcntl.fcntl(fd, _F_FULLFSYNC)
            return True
        except OSError:
            pass          # fall through to the portable floor rather than giving up
    try:
        os.fsync(fd)
        return True
    except OSError:
        return False


def fsync_dir(path: Path | str) -> bool:
    """fsync a directory so a rename within it survives a crash. True when a flush succeeded.

    `os.replace` is atomic with respect to readers, which is an ordering guarantee, not a durability
    one: without this the new file can be on the platter while the directory entry pointing at it is
    not. Best-effort — some platforms and filesystems legitimately refuse to fsync a directory fd, so
    a False here is not on its own evidence of a failing disk, and callers treat it as weaker news
    than a failed file flush.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return False
    try:
        return durable_fsync(fd)
    finally:
        os.close(fd)


@contextlib.contextmanager
def exclusive_lock(lock_path: Path):
    """Hold an exclusive advisory lock on a sibling lock file for the duration of the block.

    A sibling rather than the data file itself, because the data file is replaced by rename on every
    write: a lock held on the old inode would guard nothing once the rename landed.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield handle
    finally:
        handle.close()


def assert_revision(actual: int, expected: int | None, what: str, remedy: str) -> None:
    """The compare-and-swap guard both stores enforce: a writer that read revision N may only write
    over revision N. Refusing here is what makes a lost update impossible rather than unlikely — the
    stale writer is told to reload, and NOTHING it holds is written.

    `remedy` is the caller's own words for how to recover, because the two stores recover differently
    and a generic phrase would send an operator to the wrong command.
    """
    if expected is not None and actual != expected:
        raise CoordinatorError(f"{what} revision is {actual}, not expected {expected}; {remedy}")


def atomic_write(path: Path, text: str, *, durable: bool = False, mode: int | None = None) -> None:
    """Write `text` to `path` so a reader sees either the whole old file or the whole new one.

    Write to a temp file in the SAME directory (a cross-filesystem rename is not atomic), flush,
    fsync, rename over the target. With `durable`, use the platform barrier and fsync the containing
    directory afterwards, so the write survives a power cut and not merely a process crash — the
    difference between a store that is atomic and one that is actually durable. `mode` is applied
    explicitly with os.chmod rather than left to mkstemp, so permissions do not depend on umask.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            if durable:
                if not durable_fsync(handle.fileno()):
                    # Every flush the platform offers failed. Refusing here is the point: the rename
                    # has not happened, so the previous contents are intact, and the caller is told
                    # the write did not become durable instead of being handed a false success for
                    # data that is not on the platter.
                    raise CoordinatorError(
                        f"refusing to complete a durable write to {path}: the filesystem would not "
                        "flush it to stable storage. The previous contents are untouched. This "
                        "usually means the disk is full or failing — check it before retrying.")
            else:
                os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temp_name, mode)
        os.replace(temp_name, path)
        if durable:
            # A directory flush that the platform declines is normal on some filesystems, so this one
            # is not fatal — the file itself is already durable, and only the rename's ordering is
            # at risk. Not worth refusing a write over; worth not pretending it happened either.
            fsync_dir(path.parent)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def json_file(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CoordinatorError(f"could not read JSON from {path}: {exc}") from exc


def input_text(path: str) -> str:
    if path == "-":
        return __import__("sys").stdin.read()
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise CoordinatorError(f"could not read {path}: {exc}") from exc


def validate(instance: Any, schema_path: Path) -> None:
    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:
        raise CoordinatorError("the Engine runtime is missing jsonschema; run this tool through uv") from exc
    errors = sorted(Draft202012Validator(json_file(schema_path)).iter_errors(instance), key=lambda e: list(e.path))
    if errors:
        error = _most_specific(errors[0])
        where = ".".join(str(p) for p in error.absolute_path) or "document"
        raise CoordinatorError(f"{schema_path.stem} rejected {where}: {error.message}")


def validate_part(instance: Any, schema_path: Path, pointer: str, label: str) -> None:
    """Validate one FRAGMENT against a named definition inside a schema, with the same error legibility
    the whole-document path gives.

    Why a fragment gets its own entry point: a payload handed to a verb should fail on its own terms at
    the moment it is read, rather than surviving until the write and surfacing as a complaint about the
    enclosing record. The ordering matters wherever a verb also enforces ceremony — a session that
    mistyped a severity should be told about the severity, not about a flag it has not reached yet."""
    document = json_file(schema_path)
    schema = {**{key: value for key, value in document.items() if key.startswith("$def")}, "$ref": pointer}
    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:
        raise CoordinatorError("the Engine runtime is missing jsonschema; run this tool through uv") from exc
    errors = sorted(Draft202012Validator(schema).iter_errors(instance), key=lambda e: list(e.path))
    if errors:
        error = _most_specific(errors[0])
        where = ".".join(str(p) for p in error.absolute_path) or label
        raise CoordinatorError(f"{label} rejected {where}: {error.message}")


def _most_specific(error) -> Any:
    """Descend into a failed `oneOf`/`anyOf` to the sub-error that actually names the problem.

    Without this, any field typed `oneOf(null, {...})` — which is every optional gate on a plan
    record — reports "{the entire object dumped as a Python dict} is not valid under any of the given
    schemas". That is true, useless, and lands on precisely the fields an operator hand-authors, so a
    mistyped digest or a misspelled severity gives them nothing to act on.

    Choosing the right branch is the whole difficulty, and it takes two rules in this order.

    DEPTH decides first: the branch whose error reached furthest into the document is the one the
    author plainly meant, so a `oneOf(null, {...})` reports the field inside the object rather than
    "this is not of type null".

    At EQUAL depth two tie-breaks apply, in order. A branch that failed only because the value is the
    wrong KIND entirely — `type` against a `null` or scalar branch — is a discriminator saying "you
    did not mean this branch", never a complaint about the branch that was meant; it loses to any
    substantive error. This matters more than it sounds: a missing required field or an unexpected
    extra key on an optional object is reported AT that object's own path, tying with the null
    branch's "not of type null", and without this rule the useless message wins on a coin toss.
    Then a const/enum failure loses to anything else, for the same discriminator reason.

    Applying either tie-break before depth would be wrong: a genuinely misspelled enum value deep
    inside the right branch is exactly the error worth showing.
    """
    while getattr(error, "context", None):
        error = max(error.context, key=_specificity)
    return error


# Wrong-kind-entirely, in descending order of "this is just the wrong branch": a `type` mismatch
# against a null/scalar alternative says nothing about the object the author actually wrote.
_DISCRIMINATOR_VALIDATORS = ("type", "const", "enum")


def _specificity(error) -> tuple:
    """Rank one candidate branch error: deeper is better, and a discriminator failure is worse."""
    validator = getattr(error, "validator", None)
    return (len(list(error.absolute_path)),
            validator not in _DISCRIMINATOR_VALIDATORS,
            validator not in ("const", "enum"))


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def digest(value: Any) -> str:
    payload = value if isinstance(value, bytes) else canonical(value)
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def run(argv: list[str], *, root: Path, input_value: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=root, input=input_value, text=True, capture_output=True, check=False)


def must_run(argv: list[str], *, root: Path, input_value: str | None = None) -> str:
    result = run(argv, root=root, input_value=input_value)
    if result.returncode:
        detail = (result.stderr or result.stdout or "no diagnostic").strip()
        raise CoordinatorError(f"{' '.join(argv[:3])} failed: {detail}")
    return result.stdout


def head(root: Path) -> str:
    value = must_run(["git", "rev-parse", "HEAD"], root=root).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise CoordinatorError("git did not return a full commit id")
    return value


def base(root: Path) -> str:
    return must_run(["git", "merge-base", "HEAD", "origin/HEAD"], root=root).strip()


def dirty_paths(root: Path) -> list[str]:
    output = must_run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"], root=root
    )
    return [line for line in output.splitlines() if line]


class StableCommit:
    """Prove a command observed only one clean committed tree."""

    def __init__(self, root: Path, activity: str):
        self.root = root
        self.activity = activity
        self.commit = ""

    def __enter__(self) -> str:
        dirty = dirty_paths(self.root)
        if dirty:
            raise CoordinatorError(
                f"{self.activity} requires a clean working tree; commit or remove: "
                + ", ".join(dirty[:8])
            )
        self.commit = head(self.root)
        return self.commit

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None:
            return False
        current = head(self.root)
        dirty = dirty_paths(self.root)
        if current != self.commit or dirty:
            details = []
            if current != self.commit:
                details.append(f"HEAD changed from {self.commit[:12]} to {current[:12]}")
            if dirty:
                details.append("working tree changed: " + ", ".join(dirty[:8]))
            raise CoordinatorError(f"{self.activity} did not remain bound to one clean commit; " + "; ".join(details))
        return False


def write_json_artifact(prefix: str, value: Any) -> tuple[str, str]:
    value_digest = digest(value)
    rendered = json.dumps(value, indent=2, sort_keys=True) + "\n"
    fd, raw_path = tempfile.mkstemp(prefix=f"{prefix}-", suffix=".json")
    path = Path(raw_path)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(rendered)
    return str(path), value_digest


def write_private_path(path: Path, rendered: str) -> None:
    """Write a caller-selected artifact atomically and owner-read/write only."""
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run_validation(command: list[str], log_path: Path, *, root: Path) -> int:
    """Retain complete output while relaying only bounded heartbeats."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(log_path, flags, 0o600)
    except OSError as exc:
        raise CoordinatorError(f"could not create private validation log {log_path}: {exc}") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as log:
        process = subprocess.Popen(command, cwd=root, text=True, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, bufsize=1)
        assert process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        last_heartbeat = time.monotonic()
        while process.poll() is None:
            ready = selector.select(timeout=5)
            if ready:
                line = process.stdout.readline()
                if line:
                    log.write(line)
                    log.flush()
            if time.monotonic() - last_heartbeat >= 30:
                print(f"validation still running; complete output is being retained at {log_path}", flush=True)
                last_heartbeat = time.monotonic()
        for line in process.stdout:
            log.write(line)
        selector.close()
        process.stdout.close()
        return process.wait()


def forward_migrate(state: dict) -> dict:
    """Drop fields a shipped snapshot may carry that the current schema no longer declares.

    The Build schemas set `additionalProperties: false`, so REMOVING a field is a breaking change for
    every document already written with it: the store re-validates the whole snapshot on read AND on
    write, so such a Build is unreadable from the moment the removal lands, with no verb named in the
    refusal that would recover it. Applied on load, before validation, in the one place both Build
    stores share -- `RevisionedStore` is theirs alone; the plan library has its own store, so nothing
    here reaches it.

    `spent` (StarshipSuperjam/engine-template#1071) marked a repair round whose panel was dispatched
    and never returned. Spend counting replaced it: what a round cost is now recorded once, as
    `counted`. Dropping it here rather than tolerating it in the schema is what keeps the ledger from
    carrying two answers to one question. A legacy round loses no protection by it -- `_round_counted`
    reads an absent `counted` as counted, which is the fail-toward-spent direction.
    """
    rounds = state.get("repair_rounds")
    if isinstance(rounds, list) and any(isinstance(r, dict) and "spent" in r for r in rounds):
        state = dict(state)
        state["repair_rounds"] = [{k: v for k, v in r.items() if k != "spent"}
                                  if isinstance(r, dict) else r for r in rounds]
    return state


class RevisionedStore:
    """THE revisioned-JSON store discipline, in one place: lock, validate, compare-and-swap, replace.

    Every store this engine keeps — the Build's snapshot, the durable Build snapshot in the plan
    library — is the same four moves in the same order, and the reason they live here rather than in
    each store is that two copies of a compare-and-swap drift, and a drifted CAS loses an update
    without saying so. Subclasses choose WHERE their file lives and HOW durably it is written; they
    do not restate what a safe write is.

    Three knobs, each a real difference between the stores rather than a configuration flourish:
    `durable` picks the platform barrier plus a directory fsync (survives a power cut, not merely a
    process crash), `file_mode` applies permissions explicitly instead of inheriting the umask, and
    `what`/`missing_remedy`/`stale_remedy` supply the store's own words for a failure, because two
    stores recover differently and a generic phrase sends an operator to the wrong command.
    """

    durable = False
    file_mode: int | None = None
    what = "snapshot"
    missing_remedy = "there is nothing to read"
    stale_remedy = "re-read it"

    def __init__(self, path: str, schema: "Path | Callable[[dict], Path]", expected_revision: int | None = None):
        self.path = Path(path).resolve()
        self.schema = schema
        self.lock = self.path.with_name(self.path.name + ".lock")
        self.expected_revision = expected_revision

    def _locked(self):
        return exclusive_lock(self.lock)

    def _schema_for(self, state: dict) -> Path:
        """Resolve the schema for one state document.

        `schema` is normally a fixed Path (the single-version case, unchanged). When it is a
        callable it is a version resolver `(state) -> Path`, so a versioned store selects the
        right schema from the document itself, inside the lock, against the state being validated.
        """
        if callable(self.schema):
            return self.schema(state)
        return self.schema

    def read(self) -> dict:
        with self._locked():
            if not self.path.exists():
                raise CoordinatorError(f"no {self.what} at {self.path}; {self.missing_remedy}")
            state = forward_migrate(json_file(self.path))
            validate(state, self._schema_for(state))
            return state

    def create(self, state: dict) -> None:
        with self._locked():
            if self.path.exists():
                raise CoordinatorError(f"{self.what} already exists at {self.path}")
            self._write(state)

    def mutate(self, change: Callable[[dict], Any], *, from_revision: int | None = None) -> Any:
        with self._locked():
            if not self.path.exists():
                raise CoordinatorError(f"no {self.what} at {self.path}; {self.missing_remedy}")
            state = forward_migrate(json_file(self.path))
            validate(state, self._schema_for(state))
            expected = self.expected_revision if self.expected_revision is not None else from_revision
            assert_revision(state["revision"], expected, "snapshot", self.stale_remedy)
            result = change(state)
            state["revision"] += 1
            self._write(state)
            return result

    def _write(self, state: dict) -> None:
        validate(state, self._schema_for(state))
        atomic_write(self.path, json.dumps(state, indent=2, sort_keys=True) + "\n",
                     durable=self.durable, mode=self.file_mode)


class StateStore(RevisionedStore):
    """A Build snapshot at a path the caller names outright.

    Until 2026-08-25 this constructor REFUSED any path outside the OS temporary directory, and that
    refusal encoded the claim that "the Build's state is never a durable leg." It is deleted here rather than
    quietly relaxed, because the claim it enforced is the
    one that changed: a Build's evidence was observed to die with a forced restart, and evidence that
    cannot survive a restart is not evidence an operator can rely on. Durability now has a proper
    home — `build_state_store.DurableBuildStore`, a peer of this class rather than a subclass of it,
    addressed by the plan binding — and this class keeps the narrower job it always did well: a
    snapshot at an explicit path, for a caller who has one, and for the tests that construct them.

    What did NOT change, and is worth saying because deleting a refusal invites the assumption that
    it did: this store still carries no authority. The plan is the authority, and it lives in the
    plan library.
    """

    what = "Build snapshot"
    missing_remedy = "use 'plan bind' first"
    stale_remedy = "reload status"
