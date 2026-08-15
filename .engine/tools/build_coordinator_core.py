"""Low-level, repository-local primitives for the Build coordinator.

This module knows JSON, git, private artifacts, and atomic snapshot storage. It
does not know plans, reviewers, specifications, GitHub workflow, or CLI phases.
"""
from __future__ import annotations

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
        where = ".".join(str(p) for p in errors[0].path) or "document"
        raise CoordinatorError(f"{schema_path.stem} rejected {where}: {errors[0].message}")


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


class StateStore:
    def __init__(self, path: str, schema: "Path | Callable[[dict], Path]", expected_revision: int | None = None):
        self.path = Path(path).resolve()
        self.schema = schema
        temp_root = Path(tempfile.gettempdir()).resolve()
        if os.path.commonpath((self.path, temp_root)) != str(temp_root):
            raise CoordinatorError(f"Build snapshots belong in the OS temporary directory ({temp_root})")
        self.lock = self.path.with_name(self.path.name + ".lock")
        self.expected_revision = expected_revision

    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock.open("a+", encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle

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
                raise CoordinatorError(f"no Build snapshot at {self.path}; use 'plan bind' first")
            state = json_file(self.path)
            validate(state, self._schema_for(state))
            return state

    def create(self, state: dict) -> None:
        with self._locked():
            if self.path.exists():
                raise CoordinatorError(f"Build snapshot already exists at {self.path}")
            self._write(state)

    def mutate(self, change: Callable[[dict], Any], *, from_revision: int | None = None) -> Any:
        with self._locked():
            if not self.path.exists():
                raise CoordinatorError(f"no Build snapshot at {self.path}; use 'plan bind' first")
            state = json_file(self.path)
            validate(state, self._schema_for(state))
            expected = self.expected_revision if self.expected_revision is not None else from_revision
            if expected is not None and state["revision"] != expected:
                raise CoordinatorError(f"snapshot revision is {state['revision']}, not expected {expected}; reload status")
            result = change(state)
            state["revision"] += 1
            self._write(state)
            return result

    def _write(self, state: dict) -> None:
        validate(state, self._schema_for(state))
        fd, temp_name = tempfile.mkstemp(prefix=self.path.name + ".", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
