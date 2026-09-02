#!/usr/bin/env python3
"""Immutable persistent-state context and single-use operation capability.

The accepted dispatcher loads this file directly from the materialized accepted tree before importing the
``memory`` package. Resolution distrusts
the outer bootstrap's derived state: it accepts only its already-qualified activation and canonical roots,
then observes store/recovery state again from accepted code, creates the opaque store identity through a locked
create-if-absent compare-and-set, and seals the result.

This is an in-process integrity boundary, not an OS security boundary.  A capability cannot be constructed,
edited, crossed to another context/writer, or reused through the supported API.  Same-user hostile Python can
inspect process memory, which remains explicitly outside issue StarshipSuperjam/engine-template#1151's support claim.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import secrets
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import weakref
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType


SCHEMA_VERSION = "persistent-execution-context.v1"
STORE_IDENTITY_VERSION = "persistent-store-identity.v1"
CONTEXT_ENV = "ENGINE_PERSISTENT_EXECUTION_CONTEXT"
STORE_IDENTITY_FILENAME = "store-identity.json"
STORE_IDENTITY_LOCK_FILENAME = ".store-identity.lock"

_AUTOMATIC_OPERATIONS = MappingProxyType({
    ".engine/tools/boot.py": "automatic-boot-operation",
    ".engine/tools/close.py": "automatic-close-operation",
    ".engine/tools/memory/compact.py": "automatic-compaction",
    ".engine/tools/memory/erasure_observer.py": "automatic-erasure-observer",
    ".engine/tools/memory/backup_vault.py": "automatic-backup",
})
_LIFECYCLE_FILENAMES = MappingProxyType({
    "ledger": "ledger.ndjson",
    "ledger_meta": "ledger-meta.json",
    "keyword_index": "index.sqlite3",
    "semantic_index": "vectors.sqlite3",
    "capture_cursor": "capture-state.json",
    "capture_transaction": ".capture-transaction.json",
    "capture_lock": ".capture.lock",
    "migration_in_flight": "migration-in-flight.json",
    "backup_state": "backup-vault-state.json",
    "migration_stamp": "migration-stamp.json",
    "restore_transaction": ".restore-transaction.json",
    "store_identity": STORE_IDENTITY_FILENAME,
    "store_identity_lock": STORE_IDENTITY_LOCK_FILENAME,
})
_HEALTH_FILENAMES = MappingProxyType({
    "capture_status": "memory-capture.status",
    "capture_failures": "memory-capture-failures.ndjson",
    "runtime_health": "runtime-health.marker",
    "hook_crash_debug": "hook-crash-debug.log",
})
_TOP_KEYS = frozenset({
    "schema_version", "activation", "project", "target", "state", "operation", "invocation", "receipt",
    "extensions",
})
_IDENTITY_KEYS = frozenset({"schema_version", "store_id", "project_repository", "target_kind"})
_OID_LENGTHS = frozenset({40, 64})


class ContextError(RuntimeError):
    """Context, identity, target, or capability did not match its qualified boundary.

    The base class is the fail-safe: any staleness or mismatch that is NOT one of the typed subclasses
    below refuses the write and degrades to read-only. Only `ExpectedStateStale` is refreshable; the
    write path keys its one-shot re-seal on exactly that type and treats every other `ContextError` as
    a genuine boundary change to refuse. The subclasses exist so the write path and the operator-facing
    message can tell an index heal apart from the project moving under a running holder — they never
    widen what is accepted."""


class ExpectedStateStale(ContextError):
    """The observed store fingerprint drifted while store, activation and backup pointer all held.

    This is the one staleness a refresh can heal: the context stayed bound to the same store, the same
    accepted activation and the same backup pointer, and only the fingerprint of the store's observed
    state moved (a keyword index that healed itself, a sibling write that landed between binding and
    use). Re-observing disk and re-sealing from the same document yields a context whose fingerprint
    matches current disk; every staleness below re-raises through that re-seal because its bound
    identity genuinely changed, not merely its observed state."""


class ActivationStale(ContextError):
    """The accepted activation no longer matches the one this context is bound to.

    The repository moved under a running holder — a new commit, tree, engine release or epoch was
    accepted. A refresh cannot heal this: the context is bound to an activation the project has left
    behind. Recall still reads the store as it stands; a session restart re-accepts the current
    activation and restores full memory, writes included. This is the staleness the operator sees when
    a commit lands under a running server."""


class AcceptedTreeStale(ContextError):
    """The materialized accepted tree for this context's activation is gone.

    A sibling of `ActivationStale`: the activation identity itself still matches, but the on-disk tree
    that activation was materialized into is no longer present (a cache sweep, a moved checkout). A
    refresh cannot heal it, and like an activation change a session restart re-materializes the tree and
    restores full memory. Kept a distinct type so telemetry can tell a missing tree from a moved commit."""


class StoreIdentityStale(ContextError):
    """The persistent store's own identity no longer matches the one this context is bound to.

    Not a view drift but the store itself: its recorded identity moved. A refresh cannot heal it, and
    unlike an activation change a restart does not necessarily restore the same store — this is a
    deeper divergence that the write path refuses and reads survive read-only."""


class BackupPointerStale(ContextError):
    """The canonical backup pointer no longer matches the one this context is bound to.

    The store's durable backup pointer moved under the holder. Like a store-identity change this is a
    genuine boundary shift, not a refreshable view drift: the write path refuses and reads continue."""


class ArtifactUnreadable(ContextError):
    """A persistent memory artefact exists but could not be read (an I/O or permission fault).

    Distinct from a stale binding and from a malformed or absent artefact: the file is there and its
    bytes could not be obtained. Never refreshable — a re-seal reads the same disk — so the write path
    refuses; the operator-facing message points at the store on disk rather than at the session. Raised
    from the module's two canonical readers so the category is total wherever a read can fail."""


def _digest(value) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _file_digest(path: str) -> str | None:
    try:
        with open(path, "rb") as handle:
            return "sha256:" + hashlib.sha256(handle.read()).hexdigest()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ArtifactUnreadable(f"persistent state is unreadable: {path}") from exc


def _deep_freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _deep_thaw(value):
    if isinstance(value, Mapping):
        return {key: _deep_thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_deep_thaw(item) for item in value]
    return copy.deepcopy(value)


def _strict_absolute(path: str, label: str, *, directory: bool = False) -> str:
    if not isinstance(path, str) or not path or not os.path.isabs(path):
        raise ContextError(f"{label} must be an absolute path")
    absolute = os.path.abspath(path)
    if absolute != path or os.path.realpath(path) != path:
        raise ContextError(f"{label} is relative, normalized differently, or crosses a symlink")
    if directory and not os.path.isdir(path):
        raise ContextError(f"{label} is not an existing directory")
    return path


def _within(path: str, parent: str) -> bool:
    try:
        return os.path.commonpath((path, parent)) == parent
    except ValueError:
        return False


def _path_identity(path: str) -> dict:
    info = os.stat(path, follow_symlinks=False)
    return {"device": info.st_dev, "inode": info.st_ino}


def _snapshot_file(path: str, *, hash_content: bool = False) -> dict:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return {"present": False}
    except OSError as exc:
        raise ArtifactUnreadable(f"persistent artifact is unreadable: {path}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise ContextError(f"persistent artifact is a symlink: {path}")
    if not stat.S_ISREG(info.st_mode):
        raise ContextError(f"persistent artifact is not a regular file: {path}")
    result = {
        "present": True, "device": info.st_dev, "inode": info.st_ino, "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
    }
    if hash_content:
        result["digest"] = _file_digest(path)
    return result


def _git_common_dir(path: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", path, "rev-parse", "--git-common-dir"], capture_output=True, text=True,
            timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    raw = proc.stdout.strip()
    return os.path.realpath(raw if os.path.isabs(raw) else os.path.join(path, raw))


def validate_disposable_target(path: str, *, canonical_project_root: str, canonical_memory_dir: str,
                               canonical_git_common_dir: str) -> str:
    """Return one private target root; refuse path, inode, symlink, or shared-Git aliases."""
    target = _strict_absolute(path, "disposable target", directory=True)
    canonical_project_root = _strict_absolute(canonical_project_root, "canonical project root", directory=True)
    canonical_memory_dir = _strict_absolute(canonical_memory_dir, "canonical memory directory")
    canonical_git_common_dir = _strict_absolute(
        canonical_git_common_dir, "canonical Git common directory", directory=True)
    for label, protected in (
        ("canonical project", canonical_project_root), ("canonical memory", canonical_memory_dir),
        ("canonical Git metadata", canonical_git_common_dir),
    ):
        if _within(target, protected) or _within(protected, target):
            raise ContextError(f"disposable target aliases the {label}")
        if os.path.exists(protected) and _path_identity(target) == _path_identity(protected):
            raise ContextError(f"disposable target shares the {label}'s filesystem identity")
    target_common = _git_common_dir(target)
    if target_common and target_common == canonical_git_common_dir:
        raise ContextError("disposable target shares canonical Git metadata")
    return target


def _read_identity(path: str) -> dict | None:
    try:
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ContextError("persistent store identity is not a regular file")
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ContextError("persistent store identity is unreadable") from exc
    if not isinstance(value, dict) or set(value) != _IDENTITY_KEYS:
        raise ContextError("persistent store identity has an unknown or incomplete schema")
    if value.get("schema_version") != STORE_IDENTITY_VERSION:
        raise ContextError("persistent store identity version is unsupported")
    store_id = value.get("store_id")
    if not isinstance(store_id, str) or len(store_id) != 32 or any(ch not in "0123456789abcdef" for ch in store_id):
        raise ContextError("persistent store identity id is malformed")
    if not isinstance(value.get("project_repository"), str) or not value["project_repository"]:
        raise ContextError("persistent store identity repository is missing")
    if value.get("target_kind") not in ("canonical", "disposable"):
        raise ContextError("persistent store identity target kind is invalid")
    return value


def ensure_store_identity(memory_dir: str, *, project_repository: str, target_kind: str,
                          initializer=None) -> dict:
    """Read the stable store id, asking the accepted metadata writer to initialize it when absent.

    This module deliberately owns no file writer.  The accepted dispatcher supplies its already-registered,
    locked create-if-absent primitive, keeping identity bootstrap inside the closed mutation inventory.
    """
    memory_dir = _strict_absolute(memory_dir, "memory directory")
    if target_kind not in ("canonical", "disposable"):
        raise ContextError("store identity target kind must be canonical or disposable")
    if not isinstance(project_repository, str) or not project_repository:
        raise ContextError("store identity repository is missing")
    identity_path = os.path.join(memory_dir, STORE_IDENTITY_FILENAME)
    lock_path = os.path.join(memory_dir, STORE_IDENTITY_LOCK_FILENAME)
    existing = _read_identity(identity_path)
    if existing is None:
        if not callable(initializer):
            raise ContextError("persistent store identity is absent and no accepted initializer was supplied")
        candidate = {
            "schema_version": STORE_IDENTITY_VERSION,
            "store_id": secrets.token_hex(16),
            "project_repository": project_repository,
            "target_kind": target_kind,
        }
        initializer(identity_path, lock_path, candidate)
        existing = _read_identity(identity_path)
    if existing is None:
        raise ContextError("persistent store identity compare-and-set did not produce a winner")
    if (existing["project_repository"].casefold() != project_repository.casefold()
            or existing["target_kind"] != target_kind):
        raise ContextError("persistent store identity belongs to another repository or target kind")
    return dict(existing)


def read_store_identity(memory_dir: str) -> dict | None:
    memory_dir = _strict_absolute(memory_dir, "memory directory")
    return _read_identity(os.path.join(memory_dir, STORE_IDENTITY_FILENAME))


def _load_contract():
    loaded = sys.modules.get("memory.mutation_contract") or sys.modules.get("_engine_mutation_contract")
    if loaded is not None:
        return loaded
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mutation_contract.py")
    spec = importlib.util.spec_from_file_location("_engine_mutation_contract", path)
    if spec is None or spec.loader is None:
        raise ContextError("the accepted mutation registry could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _entry(entry_id: str) -> dict:
    contract = _load_contract()
    matches = [entry for entry in contract.REGISTRY if entry["id"] == entry_id]
    if len(matches) != 1:
        raise ContextError(f"unknown or ambiguous mutation registry entry: {entry_id}")
    return copy.deepcopy(matches[0])


def _strict_pointer(path: str) -> tuple[dict, str]:
    try:
        info = os.lstat(path)
    except FileNotFoundError as exc:
        raise ContextError("the committed backup pointer is absent") from exc
    except OSError as exc:
        raise ContextError("the committed backup pointer is unreadable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ContextError("the committed backup pointer is not a regular file")
    digest = _file_digest(path)
    if digest is None:
        raise ContextError("the committed backup pointer is absent")
    try:
        with open(path, encoding="utf-8") as handle:
            pointer = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ContextError("the committed backup pointer is unreadable") from exc
    if not isinstance(pointer, dict) or pointer.get("schema_version") != 1:
        raise ContextError("the committed backup pointer is malformed")
    if pointer.get("configured") is False:
        if set(pointer) != {"schema_version", "configured"}:
            raise ContextError("the unconfigured backup pointer has unexpected fields")
        return pointer, digest
    required = ("owner", "repo", "branch", "namespace")
    if any(not isinstance(pointer.get(key), str) or not pointer[key] for key in required):
        raise ContextError("the configured backup pointer is incomplete")
    return pointer, digest


def _read_ledger_meta(path: str) -> tuple[int, int, str]:
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError:
        return 0, 0, "absent"
    except (OSError, ValueError) as exc:
        raise ContextError("ledger metadata is unreadable") from exc
    if not isinstance(value, dict):
        raise ContextError("ledger metadata is malformed")
    generation, epoch = value.get("generation", 0), value.get("index_epoch", 0)
    for label, item in (("generation", generation), ("index epoch", epoch)):
        if not isinstance(item, int) or isinstance(item, bool) or item < 0:
            raise ContextError(f"ledger {label} is malformed")
    return generation, epoch, "present"


def _sqlite_meta(path: str, query: str) -> dict | None:
    if not os.path.exists(path):
        return None
    if os.path.islink(path) or not os.path.isfile(path):
        raise ContextError(f"derived index is unsafe: {path}")
    try:
        uri = Path(path).as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            row = connection.execute(query).fetchone()
    except (OSError, sqlite3.Error):
        return {"readable": False}
    return {"readable": True, "values": list(row) if row is not None else None}


def _lifecycle(project_root: str, memory_dir: str, common_dir: str) -> dict:
    telemetry = os.path.join(project_root, ".engine", "telemetry", ".cache")
    values = {key: os.path.join(memory_dir, filename) for key, filename in _LIFECYCLE_FILENAMES.items()}
    values.update({key: os.path.join(telemetry, filename) for key, filename in _HEALTH_FILENAMES.items()})
    values.update({
        "backup_pointer": os.path.join(project_root, ".engine", "memory-backup", "pointer.json"),
        "canonical_backup_pointer": os.path.join(project_root, ".engine", "memory-backup", "pointer.json"),
        "erasure_proposal": os.path.join(project_root, ".engine", "erasures", "proposal.json"),
        "restore_staging_root": memory_dir,
        "accepted_activation": os.path.join(common_dir, "engine", "accepted-hooks", "activation.json"),
        "accepted_cache": os.path.join(common_dir, "engine", "accepted-hooks", "trees"),
        "accepted_activation_lock": os.path.join(common_dir, "engine", "accepted-hooks", "activation.lock"),
        "accepted_materialization_lock": os.path.join(
            common_dir, "engine", "accepted-hooks", "activation.lock.materialize"),
    })
    return values


def _observe_state(lifecycle: dict, store_identity: dict, pointer: dict, pointer_digest: str) -> dict:
    generation, epoch, meta_state = _read_ledger_meta(lifecycle["ledger_meta"])
    snapshots = {}
    content_hashed = {
        "ledger_meta", "capture_cursor", "capture_transaction", "migration_in_flight", "backup_state",
        "migration_stamp",
        "restore_transaction", "capture_status", "capture_failures", "runtime_health", "hook_crash_debug",
        "backup_pointer", "canonical_backup_pointer", "erasure_proposal", "store_identity",
    }
    for key, path in lifecycle.items():
        if key in ("restore_staging_root", "accepted_cache"):
            continue
        snapshots[key] = _snapshot_file(path, hash_content=key in content_hashed)
    state = {
        "store_id": store_identity["store_id"],
        "ledger_generation": generation,
        "index_epoch": epoch,
        "ledger_meta_state": meta_state,
        "keyword_index_meta": _sqlite_meta(
            lifecycle["keyword_index"],
            "SELECT generation, schema_version, index_epoch FROM meta WHERE rowid = 1",
        ),
        "semantic_index_meta": _sqlite_meta(
            lifecycle["semantic_index"],
            "SELECT schema_version, table_fingerprint FROM meta WHERE rowid = 1",
        ),
        "backup_pointer_digest": pointer_digest,
        "backup_pointer_identity": pointer,
        "artifacts": snapshots,
    }
    state["expected_state_fingerprint"] = _digest(state)
    return state


def _activation(value) -> dict:
    required = {"repository", "commit", "tree", "engine_release", "epoch"}
    if not isinstance(value, dict) or set(value) != required:
        raise ContextError("accepted activation binding is incomplete")
    for key in ("repository", "engine_release"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise ContextError(f"accepted activation {key} is missing")
    for key in ("commit", "tree"):
        oid = value.get(key)
        if (not isinstance(oid, str) or len(oid) not in _OID_LENGTHS
                or any(char not in "0123456789abcdef" for char in oid)):
            raise ContextError(f"accepted activation {key} is malformed")
    if not isinstance(value.get("epoch"), int) or isinstance(value["epoch"], bool) or value["epoch"] < 1:
        raise ContextError("accepted activation epoch is malformed")
    return {**value, "future_code_generation": None}


def _seal(document: dict) -> dict:
    document = copy.deepcopy(document)
    document["state"]["expected_state_fingerprint"] = _digest({
        key: value for key, value in document["state"].items() if key != "expected_state_fingerprint"
    })
    receipt = document["receipt"]
    receipt["context_digest"] = None
    receipt["context_digest"] = _digest(document)
    return document


class ExecutionContext:
    """Deeply immutable context with exact JSON round-trip and self-authenticating digest."""

    __slots__ = ("_document", "__weakref__")

    def __init__(self, document: dict, *, _trusted=False):
        if not _trusted:
            raise ContextError("execution contexts must be resolved or decoded through the accepted API")
        object.__setattr__(self, "_document", _deep_freeze(document))

    def __setattr__(self, _name, _value):
        raise AttributeError("ExecutionContext is immutable")

    def __getitem__(self, key):
        return self._document[key]

    @property
    def digest(self) -> str:
        return self._document["receipt"]["context_digest"]

    @property
    def expected_state_fingerprint(self) -> str:
        return self._document["state"]["expected_state_fingerprint"]

    def to_document(self) -> dict:
        return _deep_thaw(self._document)

    def to_json(self) -> str:
        return json.dumps(self.to_document(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_document(cls, value: dict) -> "ExecutionContext":
        document = copy.deepcopy(value)
        _validate_document(document)
        return cls(document, _trusted=True)


def _validate_document(document: dict) -> None:
    if not isinstance(document, dict) or set(document) != _TOP_KEYS:
        raise ContextError("execution context has an unknown or incomplete top-level schema")
    if document.get("schema_version") != SCHEMA_VERSION:
        raise ContextError("execution context version is unsupported")
    if not isinstance(document.get("extensions"), dict):
        raise ContextError("execution context extensions must be an object")
    activation = document.get("activation")
    if not isinstance(activation, dict) or set(activation) != {
            "repository", "commit", "tree", "engine_release", "epoch", "future_code_generation"}:
        raise ContextError("execution context activation is incomplete")
    _activation({key: activation[key] for key in ("repository", "commit", "tree", "engine_release", "epoch")})
    if activation["future_code_generation"] is not None and not isinstance(
            activation["future_code_generation"], str):
        raise ContextError("future code generation must stay opaque")
    project, target, state = document.get("project"), document.get("target"), document.get("state")
    operation, invocation, receipt = document.get("operation"), document.get("invocation"), document.get("receipt")
    if not all(isinstance(item, dict) for item in (project, target, state, operation, invocation, receipt)):
        raise ContextError("execution context sections must be objects")
    if set(project) != {
            "repository", "namespace", "root", "root_identity", "git_common_dir", "git_common_identity"}:
        raise ContextError("execution context project binding is incomplete")
    if set(target) != {"kind", "memory_dir", "store_identity", "store_namespace", "lifecycle"}:
        raise ContextError("execution context target binding is incomplete")
    if target.get("kind") not in ("canonical", "disposable"):
        raise ContextError("execution context target kind is invalid")
    if not isinstance(target.get("store_identity"), dict) or set(target["store_identity"]) != _IDENTITY_KEYS:
        raise ContextError("execution context store identity is incomplete")
    if not isinstance(target.get("lifecycle"), dict) or set(target["lifecycle"]) != set(
            _LIFECYCLE_FILENAMES) | set(_HEALTH_FILENAMES) | {
                "backup_pointer", "erasure_proposal", "restore_staging_root", "accepted_activation",
                "accepted_cache", "accepted_activation_lock", "accepted_materialization_lock",
                "canonical_backup_pointer"}:
        raise ContextError("execution context lifecycle binding is incomplete")
    if not isinstance(state.get("expected_state_fingerprint"), str):
        raise ContextError("execution context expected-state fingerprint is missing")
    expected = _digest({key: value for key, value in state.items() if key != "expected_state_fingerprint"})
    if state["expected_state_fingerprint"] != expected:
        raise ContextError("execution context expected-state fingerprint is inconsistent")
    required_operation = {
        "registry_id", "capability_identity", "writer", "target_kind", "effect_class",
        "declared_cardinality", "schema_cutover", "invocation_mode",
    }
    if set(operation) != required_operation:
        raise ContextError("execution context operation binding is incomplete")
    registered = _entry(operation.get("registry_id"))
    for key in required_operation - {"registry_id", "invocation_mode"}:
        if operation.get(key) != registered.get(key):
            raise ContextError(f"execution context operation {key} differs from the registry")
    if operation.get("invocation_mode") not in registered["allowed_invocation_modes"]:
        raise ContextError("execution context operation mode differs from the registry")
    if set(invocation) != {"script", "provider", "run_id", "task_id"}:
        raise ContextError("execution context invocation binding is incomplete")
    if not isinstance(invocation.get("script"), str) or not invocation["script"]:
        raise ContextError("execution context invocation script is missing")
    if invocation.get("provider") not in ("claude", "codex"):
        raise ContextError("execution context provider is invalid")
    if set(receipt) != {"context_id", "context_digest"}:
        raise ContextError("execution context receipt is incomplete")
    context_id = receipt.get("context_id")
    if not isinstance(context_id, str) or len(context_id) != 32:
        raise ContextError("execution context receipt identity is malformed")
    supplied = receipt.get("context_digest")
    candidate = copy.deepcopy(document)
    candidate["receipt"]["context_digest"] = None
    if supplied != _digest(candidate):
        raise ContextError("execution context digest is inconsistent")


_AUTHORIZED_CONTEXTS = weakref.WeakValueDictionary()
_CURRENT_CONTEXT: ExecutionContext | None = None
_CONTEXT_LOCK = threading.RLock()


def _remember_context(context: ExecutionContext) -> None:
    with _CONTEXT_LOCK:
        _AUTHORIZED_CONTEXTS[context.digest] = context


def _is_authorized_context(context: ExecutionContext) -> bool:
    with _CONTEXT_LOCK:
        return _AUTHORIZED_CONTEXTS.get(context.digest) is context


def revalidate_context(context: ExecutionContext) -> ExecutionContext:
    """Match a decoded context to the live canonical namespace before authorizing it in this module."""
    if not isinstance(context, ExecutionContext):
        raise ContextError("execution context has an unsupported runtime type")
    document = context.to_document()
    project, target = document["project"], document["target"]
    root = _strict_absolute(project["root"], "context project root", directory=True)
    common = _strict_absolute(project["git_common_dir"], "context Git common directory", directory=True)
    if project["root_identity"] != _path_identity(root):
        raise ContextError("execution context project identity no longer matches")
    if project["git_common_identity"] != _path_identity(common):
        raise ContextError("execution context Git common-directory identity no longer matches")
    expected_lifecycle = _lifecycle(root, target["memory_dir"], common)
    if target["kind"] == "disposable":
        target_root = os.path.dirname(target["memory_dir"])
        private_health = os.path.join(target_root, "health")
        for key, filename in _HEALTH_FILENAMES.items():
            expected_lifecycle[key] = os.path.join(private_health, filename)
        expected_lifecycle["backup_pointer"] = os.path.join(
            target_root, ".engine", "memory-backup", "pointer.json")
        expected_lifecycle["erasure_proposal"] = os.path.join(
            target_root, ".engine", "erasures", "proposal.json")
    if target["lifecycle"] != expected_lifecycle:
        raise ContextError("execution context lifecycle paths no longer match their qualified roots")
    try:
        info = os.lstat(expected_lifecycle["accepted_activation"])
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ActivationStale("accepted activation is not a regular file during context revalidation")
        with open(expected_lifecycle["accepted_activation"], encoding="utf-8") as handle:
            active = json.load(handle)
    except ContextError:
        raise
    except (OSError, ValueError) as exc:
        raise ActivationStale("accepted activation is unavailable during context revalidation") from exc
    activation = document["activation"]
    for key in ("repository", "commit", "tree", "engine_release", "epoch"):
        if active.get(key) != activation.get(key):
            raise ActivationStale(f"execution context activation {key} no longer matches")
    accepted_tree = os.path.join(
        expected_lifecycle["accepted_cache"], f"{activation['commit']}-{activation['tree']}")
    if not os.path.isfile(os.path.join(accepted_tree, ".engine", "tools", "accepted_hook_dispatch.py")):
        raise AcceptedTreeStale("execution context accepted tree is unavailable")
    identity = _read_identity(expected_lifecycle["store_identity"])
    if identity != target["store_identity"]:
        raise StoreIdentityStale("execution context store identity no longer matches")
    pointer, pointer_digest = _strict_pointer(expected_lifecycle["canonical_backup_pointer"])
    state = document["state"]
    if pointer != state["backup_pointer_identity"] or pointer_digest != state["backup_pointer_digest"]:
        raise BackupPointerStale("execution context backup pointer no longer matches")
    observed = _observe_state(expected_lifecycle, identity, pointer, pointer_digest)
    if observed["expected_state_fingerprint"] != state["expected_state_fingerprint"]:
        raise ExpectedStateStale("execution context expected-state fingerprint is stale")
    return context


def resolve_execution_context(bootstrap: dict, *, accepted_tree: str, script: str,
                              target_kind: str = "canonical", target_root: str | None = None,
                              operation_id: str | None = None, provider: str | None = None,
                              run_id: str | None = None, task_id: str | None = None,
                              extensions: dict | None = None, identity_initializer=None) -> ExecutionContext:
    """Resolve disk state once from accepted code and seal a context before the target imports."""
    if not isinstance(bootstrap, dict) or bootstrap.get("schema_version") != "accepted-hook-context.v1":
        raise ContextError("accepted bootstrap context is absent or unsupported")
    activation = _activation(bootstrap.get("activation"))
    raw = bootstrap.get("canonical")
    if not isinstance(raw, dict):
        raise ContextError("accepted bootstrap canonical binding is absent")
    project_root = _strict_absolute(raw.get("project_root"), "canonical project root", directory=True)
    common_dir = _strict_absolute(raw.get("git_common_dir"), "canonical Git common directory", directory=True)
    canonical_memory = _strict_absolute(raw.get("memory_dir"), "canonical memory directory")
    if canonical_memory != os.path.join(project_root, ".engine", "memory"):
        raise ContextError("canonical memory directory is not the main checkout's shared memory root")
    if raw.get("project_root_identity") != _path_identity(project_root):
        raise ContextError("canonical project root identity changed after bootstrap")
    accepted_tree = _strict_absolute(accepted_tree, "accepted materialized tree", directory=True)
    if not os.path.isfile(os.path.join(accepted_tree, ".engine", "tools", "accepted_hook_dispatch.py")):
        raise ContextError("accepted materialized tree has no dispatcher")
    pointer_path = os.path.join(project_root, ".engine", "memory-backup", "pointer.json")
    pointer, pointer_digest = _strict_pointer(pointer_path)
    if raw.get("backup_pointer_digest") != pointer_digest:
        raise ContextError("canonical backup pointer changed after bootstrap")
    if target_kind == "canonical":
        memory_dir = canonical_memory
    elif target_kind == "disposable":
        target_root = validate_disposable_target(
            target_root, canonical_project_root=project_root, canonical_memory_dir=canonical_memory,
            canonical_git_common_dir=common_dir,
        )
        memory_dir = os.path.join(target_root, "memory")
    else:
        raise ContextError("persistent target kind is invalid")
    store_identity = ensure_store_identity(
        memory_dir, project_repository=activation["repository"], target_kind=target_kind,
        initializer=identity_initializer)
    lifecycle = _lifecycle(project_root, memory_dir, common_dir)
    if target_kind == "disposable":
        # Candidate state and health stay private together; canonical pointer/erasure inputs remain read-only
        # bindings in the context and are never copied into the candidate target.
        private_health = os.path.join(target_root, "health")
        for key, filename in _HEALTH_FILENAMES.items():
            lifecycle[key] = os.path.join(private_health, filename)
        lifecycle["backup_pointer"] = os.path.join(
            target_root, ".engine", "memory-backup", "pointer.json")
        lifecycle["erasure_proposal"] = os.path.join(target_root, ".engine", "erasures", "proposal.json")
    entry_id = operation_id or _AUTOMATIC_OPERATIONS.get(script)
    registered = _entry(entry_id) if entry_id else None
    if registered is None:
        raise ContextError("the invocation has no registered operation")
    mode = "automatic" if script in _AUTOMATIC_OPERATIONS and operation_id is None else "attended"
    if mode not in registered["allowed_invocation_modes"]:
        raise ContextError("the registered operation does not allow this invocation mode")
    invocation_provider = provider or bootstrap.get("invocation", {}).get("provider")
    if invocation_provider not in ("claude", "codex"):
        raise ContextError("the invocation provider is not qualified")
    state = _observe_state(lifecycle, store_identity, pointer, pointer_digest)
    operation = {
        key: copy.deepcopy(registered[key])
        for key in ("capability_identity", "writer", "target_kind", "effect_class", "declared_cardinality",
                    "schema_cutover")
    }
    operation.update({"registry_id": registered["id"], "invocation_mode": mode})
    pointer_namespace = pointer.get("namespace") if pointer.get("configured") is not False else None
    document = _seal({
        "schema_version": SCHEMA_VERSION,
        "activation": activation,
        "project": {
            "repository": activation["repository"], "namespace": f"github:{activation['repository'].casefold()}",
            "root": project_root, "root_identity": _path_identity(project_root), "git_common_dir": common_dir,
            "git_common_identity": _path_identity(common_dir),
        },
        "target": {
            "kind": target_kind, "memory_dir": memory_dir, "store_identity": store_identity,
            "store_namespace": pointer_namespace or store_identity["store_id"], "lifecycle": lifecycle,
        },
        "state": state,
        "operation": operation,
        "invocation": {
            "script": script, "provider": invocation_provider,
            "run_id": run_id if run_id is not None else bootstrap.get("invocation", {}).get("run_id"),
            "task_id": task_id if task_id is not None else bootstrap.get("invocation", {}).get("task_id"),
        },
        "receipt": {"context_id": secrets.token_hex(16), "context_digest": None},
        "extensions": copy.deepcopy(extensions or {}),
    })
    _validate_document(document)
    context = ExecutionContext(document, _trusted=True)
    _remember_context(context)
    return context


def install_automatic_context(bootstrap: dict, *, accepted_tree: str, script: str,
                              identity_initializer) -> ExecutionContext:
    global _CURRENT_CONTEXT
    context = resolve_execution_context(
        bootstrap, accepted_tree=accepted_tree, script=script,
        provider=bootstrap.get("invocation", {}).get("provider"),
        run_id=bootstrap.get("invocation", {}).get("run_id"),
        task_id=bootstrap.get("invocation", {}).get("task_id"),
        identity_initializer=identity_initializer,
    )
    if _CURRENT_CONTEXT is not None and _CURRENT_CONTEXT.digest != context.digest:
        raise ContextError("a different persistent execution context is already installed")
    _CURRENT_CONTEXT = context
    os.environ[CONTEXT_ENV] = context.to_json()
    return context


def install_attended_context(bootstrap: dict, *, accepted_tree: str, script: str, operation_id: str,
                             identity_initializer) -> ExecutionContext:
    """Install one exact accepted attended operation before its target module imports."""
    global _CURRENT_CONTEXT
    context = resolve_execution_context(
        bootstrap, accepted_tree=accepted_tree, script=script, operation_id=operation_id,
        provider=bootstrap.get("invocation", {}).get("provider"),
        run_id=bootstrap.get("invocation", {}).get("run_id"),
        task_id=bootstrap.get("invocation", {}).get("task_id"),
        identity_initializer=identity_initializer,
    )
    if _CURRENT_CONTEXT is not None and _CURRENT_CONTEXT.digest != context.digest:
        raise ContextError("a different persistent execution context is already installed")
    _CURRENT_CONTEXT = context
    os.environ[CONTEXT_ENV] = context.to_json()
    return context


def current_context() -> ExecutionContext:
    global _CURRENT_CONTEXT
    if _CURRENT_CONTEXT is not None:
        return _CURRENT_CONTEXT
    try:
        document = json.loads(os.environ[CONTEXT_ENV])
    except (KeyError, ValueError) as exc:
        raise ContextError("no persistent execution context is installed") from exc
    context = revalidate_context(ExecutionContext.from_document(document))
    _remember_context(context)
    _CURRENT_CONTEXT = context
    return context


def _refreshed_context(context: ExecutionContext, operation_id: str | None = None) -> ExecutionContext:
    """Re-seal current disk state, optionally narrowing an attended composite to one child operation."""
    if not _is_authorized_context(context):
        raise ContextError("execution context is not authorized in this process")
    document = context.to_document()
    if operation_id is not None:
        contract = _load_contract()
        if operation_id not in _allowed_registry_ids(context, contract):
            raise ContextError("registry operation is outside this invocation's closed transitive boundary")
        selected = _entry(operation_id)
        operation = {
            key: copy.deepcopy(selected[key])
            for key in ("capability_identity", "writer", "target_kind", "effect_class",
                        "declared_cardinality", "schema_cutover")
        }
        operation.update({"registry_id": selected["id"], "invocation_mode": "attended"})
        document["operation"] = operation
    target, state = document["target"], document["state"]
    document["state"] = _observe_state(
        target["lifecycle"], target["store_identity"],
        state["backup_pointer_identity"], state["backup_pointer_digest"],
    )
    document["receipt"] = {"context_id": secrets.token_hex(16), "context_digest": None}
    document = _seal(document)
    _validate_document(document)
    refreshed = ExecutionContext(document, _trusted=True)
    revalidate_context(refreshed)
    _remember_context(refreshed)
    return refreshed


def refresh_for_operation(context: ExecutionContext, operation_id: str) -> ExecutionContext:
    """Create one exact request context for the accepted memory server under its held store lock."""
    if context["operation"]["registry_id"] != "attended-memory-mcp":
        raise ContextError("per-request operation refresh is only available to the accepted memory server")
    return _refreshed_context(context, operation_id)


def reseal_for_stale_state(context: ExecutionContext) -> ExecutionContext:
    """Re-seal an authorized context from current disk after `ExpectedStateStale`, for the write retry.

    The write path calls this exactly once, under its held lock, when the under-lock revalidation raised
    `ExpectedStateStale` — the observed store fingerprint drifted while the store identity, the accepted
    activation and the backup pointer all still held. It re-observes disk and re-validates from the SAME
    document, so a genuine activation, store-identity or backup-pointer change — none of which is ever
    `ExpectedStateStale` — still raises its own typed subclass here and refuses the retry. It narrows no
    operation and grants no capability; it only advances the observed state the caller already holds, so
    the binding it returns is the same store, activation and pointer it was given, at current fingerprint.

    Unlike `refresh_for_operation` and `refresh_current_context`, this is not gated to the attended memory
    server: a CLI write holds its own authorized context, and its fingerprint can drift the same way."""
    return _refreshed_context(context)


def refresh_current_context(context: ExecutionContext) -> ExecutionContext:
    """Advance the long-lived memory server's root context after one successful request."""
    global _CURRENT_CONTEXT
    if context["operation"]["registry_id"] != "attended-memory-mcp":
        raise ContextError("only the accepted memory server has a renewable root context")
    refreshed = _refreshed_context(context)
    _CURRENT_CONTEXT = refreshed
    os.environ[CONTEXT_ENV] = refreshed.to_json()
    return refreshed


def observe_state_fingerprint(context: ExecutionContext) -> str:
    if not _is_authorized_context(context):
        raise ContextError("execution context is not authorized in this process")
    document = context.to_document()
    state = _observe_state(
        document["target"]["lifecycle"], document["target"]["store_identity"],
        document["state"]["backup_pointer_identity"], document["state"]["backup_pointer_digest"],
    )
    return state["expected_state_fingerprint"]


_BINDING_ACTIVATION_KEYS = ("repository", "commit", "tree", "engine_release", "epoch")


def binding_identity(context: ExecutionContext) -> dict:
    """The authority-bearing binding of a context: the store, its target and its accepted activation.

    Deliberately excludes the observed-state fingerprint, which a legitimate re-seal advances. The write
    path captures this before and after its one-shot re-seal and refuses if they differ, so the refresh
    that heals a fingerprint drift can never quietly move the store identity, the memory directory, the
    target kind or any of the five activation fields — the executable form of 'recover without widening
    authority'. Reads the decoded document directly, so it does not require an installed context."""
    document = context.to_document()
    target = document["target"]
    activation = document["activation"]
    return {
        "store_identity": copy.deepcopy(target["store_identity"]),
        "memory_dir": target["memory_dir"],
        "target_kind": target["kind"],
        "activation": {key: activation.get(key) for key in _BINDING_ACTIVATION_KEYS},
    }


class OperationCapability:
    """Opaque immutable handle; the mutable used/budget state lives only in ``_GRANTS``."""

    __slots__ = ("_document", "__weakref__")

    def __init__(self, document: dict, seal):
        if seal is not _CAPABILITY_SEAL:
            raise ContextError("operation capabilities can only be minted by the accepted authority")
        object.__setattr__(self, "_document", _deep_freeze(document))

    def __setattr__(self, _name, _value):
        raise AttributeError("OperationCapability is immutable")

    def to_document(self) -> dict:
        return _deep_thaw(self._document)


class CapabilityReceipt:
    __slots__ = ("_document",)

    def __init__(self, document: dict):
        object.__setattr__(self, "_document", _deep_freeze(document))

    def __setattr__(self, _name, _value):
        raise AttributeError("CapabilityReceipt is immutable")

    def to_document(self) -> dict:
        return _deep_thaw(self._document)


_CAPABILITY_SEAL = object()
_GRANTS = weakref.WeakKeyDictionary()
_CAPABILITY_LOCK = threading.RLock()


def _allowed_registry_ids(context: ExecutionContext, contract) -> set[str]:
    operation = context["operation"]
    if operation["invocation_mode"] == "automatic":
        script = context["invocation"]["script"]
        allowed = set(contract.AUTOMATIC_ENTRYPOINTS.get(script, ()))
        allowed.update(contract.AUTOMATIC_COMMON_EFFECTS)
    else:
        allowed = {operation["registry_id"]}
    entries = {entry["id"]: copy.deepcopy(entry) for entry in contract.REGISTRY}
    changed = True
    while changed:
        changed = False
        writers = {entries[entry_id]["writer"] for entry_id in allowed if entry_id in entries}
        for boundary in tuple(writers):
            for entry_id in contract.TRANSITIVE_BOUNDARIES.get(boundary, ()):
                if entry_id not in allowed:
                    allowed.add(entry_id)
                    changed = True
        for entry_id, entry in entries.items():
            if entry_id not in allowed and writers.intersection(entry.get("callers", ())):
                allowed.add(entry_id)
                changed = True
    return allowed


def mint_capability(context: ExecutionContext, *, measured_cardinality: int,
                    registry_id: str | None = None) -> OperationCapability:
    if not _is_authorized_context(context):
        raise ContextError("execution context is not authorized in this process")
    contract = _load_contract()
    selected_id = registry_id or context["operation"]["registry_id"]
    if selected_id not in _allowed_registry_ids(context, contract):
        raise ContextError("registry operation is outside this invocation's closed transitive boundary")
    operation = _entry(selected_id)
    invocation_mode = context["operation"]["invocation_mode"]
    try:
        contract.classify(
            writer=operation["writer"], target_kind=operation["target_kind"],
            effect_class=operation["effect_class"], invocation_mode=invocation_mode,
            measured_cardinality=measured_cardinality, schema_cutover=operation["schema_cutover"],
        )
    except Exception as exc:
        raise ContextError(str(exc)) from exc
    document = {
        "grant_id": secrets.token_hex(16), "context_digest": context.digest,
        "issuer_pid": os.getpid(),
        "expected_state_fingerprint": context.expected_state_fingerprint,
        "registry_id": operation["id"], "capability_identity": operation["capability_identity"],
        "writer": operation["writer"], "target_kind": operation["target_kind"],
        "effect_class": operation["effect_class"], "invocation_mode": invocation_mode,
        "measured_cardinality": measured_cardinality, "schema_cutover": operation["schema_cutover"],
    }
    capability = OperationCapability(document, _CAPABILITY_SEAL)
    with _CAPABILITY_LOCK:
        _GRANTS[capability] = False
    return capability


def consume_capability(capability: OperationCapability, *, context: ExecutionContext, writer: str,
                       target_kind: str, effect_class: str, invocation_mode: str,
                       measured_cardinality: int, schema_cutover: bool,
                       observed_state_fingerprint: str) -> CapabilityReceipt:
    if not isinstance(capability, OperationCapability):
        raise ContextError("forged operation capability")
    try:
        value = capability.to_document()
    except (AttributeError, TypeError) as exc:
        raise ContextError("forged or unknown operation capability") from exc
    expected = {
        "context_digest": context.digest, "expected_state_fingerprint": observed_state_fingerprint,
        "writer": writer, "target_kind": target_kind, "effect_class": effect_class,
        "invocation_mode": invocation_mode, "measured_cardinality": measured_cardinality,
        "schema_cutover": schema_cutover,
    }
    for key, item in expected.items():
        if value.get(key) != item:
            raise ContextError(f"operation capability {key} mismatch")
    if value.get("issuer_pid") != os.getpid():
        raise ContextError("operation capability crossed a process boundary")
    if observed_state_fingerprint != context.expected_state_fingerprint:
        raise ContextError("operation capability expected-state fingerprint is stale")
    if not _is_authorized_context(context):
        raise ContextError("operation capability crossed to an unauthorized context")
    with _CAPABILITY_LOCK:
        try:
            used = _GRANTS[capability]
        except KeyError as exc:
            raise ContextError("forged or unknown operation capability") from exc
        if used:
            raise ContextError("operation capability was already consumed")
        _GRANTS[capability] = True  # consume before the writer begins; a crash never restores authority
    receipt = {
        "receipt_id": secrets.token_hex(16), "grant_id": value["grant_id"],
        "context_digest": context.digest, "registry_id": value["registry_id"], "writer": writer,
        "target_kind": target_kind, "effect_class": effect_class, "invocation_mode": invocation_mode,
        "measured_cardinality": measured_cardinality, "schema_cutover": schema_cutover,
        "expected_state_fingerprint": observed_state_fingerprint,
    }
    receipt["receipt_digest"] = _digest(receipt)
    return CapabilityReceipt(receipt)


def _fixture_bootstrap(root: str, common: str, *, pointer_digest: str) -> dict:
    return {
        "schema_version": "accepted-hook-context.v1",
        "activation": {
            "repository": "owner/repo", "commit": "a" * 40, "tree": "b" * 40,
            "engine_release": "9.9.9", "epoch": 1,
        },
        "canonical": {
            "project_root": root, "project_root_identity": _path_identity(root), "git_common_dir": common,
            "memory_dir": os.path.join(root, ".engine", "memory"),
            "backup_pointer_digest": pointer_digest,
        },
        "invocation": {"script": ".engine/tools/close.py", "provider": "codex", "run_id": "run", "task_id": "task"},
    }


_FIXTURE_IDENTITY_LOCK = threading.RLock()


def _fixture_identity_initializer(identity_path: str, lock_path: str, candidate: dict) -> None:
    """Test-only locked CAS, excluded from production-writer discovery by its fixture prefix."""
    import fcntl

    os.makedirs(os.path.dirname(identity_path), mode=0o700, exist_ok=True)
    with _FIXTURE_IDENTITY_LOCK:
        with open(lock_path, "a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                if os.path.exists(identity_path):
                    return
                fd, temporary = tempfile.mkstemp(prefix=".identity-fixture-", dir=os.path.dirname(identity_path))
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        json.dump(candidate, handle, sort_keys=True, separators=(",", ":"))
                        handle.write("\n")
                        handle.flush()
                        os.fsync(handle.fileno())
                    try:
                        os.link(temporary, identity_path)
                    except FileExistsError:
                        pass
                finally:
                    try:
                        os.unlink(temporary)
                    except FileNotFoundError:
                        pass
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _fixture_self_test() -> dict:
    """Operator/CI-runnable behavioral matrix kept with the node's only approved implementation path."""
    import concurrent.futures
    import jsonschema

    global _CURRENT_CONTEXT
    with tempfile.TemporaryDirectory(prefix="engine-context-test-") as raw_temporary:
        temporary = os.path.realpath(raw_temporary)
        root = os.path.join(temporary, "project")
        common = os.path.join(temporary, "common.git")
        accepted = os.path.join(
            common, "engine", "accepted-hooks", "trees", f"{'a' * 40}-{'b' * 40}")
        memory = os.path.join(root, ".engine", "memory")
        pointer_path = os.path.join(root, ".engine", "memory-backup", "pointer.json")
        os.makedirs(memory)
        os.makedirs(common)
        os.makedirs(os.path.join(accepted, ".engine", "tools"))
        Path(os.path.join(accepted, ".engine", "tools", "accepted_hook_dispatch.py")).write_text("# accepted\n")
        activation_path = os.path.join(common, "engine", "accepted-hooks", "activation.json")
        os.makedirs(os.path.dirname(activation_path), exist_ok=True)
        Path(activation_path).write_text(json.dumps({
            "schema_version": "accepted-hook-activation.v1", "repository": "owner/repo",
            "commit": "a" * 40, "tree": "b" * 40, "engine_release": "9.9.9", "epoch": 1,
            "source": "reviewed-merge", "source_ref": "refs/heads/main",
            "authority": {"kind": "github-merged-pull", "evidence_id": "42"},
            "activated_at": "2026-01-01T00:00:00Z",
        }, sort_keys=True) + "\n")
        os.makedirs(os.path.dirname(pointer_path))
        Path(pointer_path).write_text('{"schema_version":1,"configured":false}\n')
        ledger_path = os.path.join(memory, "ledger.ndjson")
        payload = b'{"id":"kept","text":"payload bytes stay unchanged"}\n'
        Path(ledger_path).write_bytes(payload)
        bootstrap = _fixture_bootstrap(root, common, pointer_digest=_file_digest(pointer_path))

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            identities = list(pool.map(
                lambda _n: ensure_store_identity(
                    memory, project_repository="owner/repo", target_kind="canonical",
                    initializer=_fixture_identity_initializer),
                range(24),
            ))
        if len({item["store_id"] for item in identities}) != 1 or Path(ledger_path).read_bytes() != payload:
            raise AssertionError("identity CAS diverged or rewrote payload bytes")

        context = resolve_execution_context(
            bootstrap, accepted_tree=accepted, script=".engine/tools/close.py",
            operation_id="ledger-append", provider="codex", run_id="run", task_id="task",
            extensions={"future-consumer": {"opaque": 7}},
            identity_initializer=_fixture_identity_initializer,
        )
        schemas = Path(__file__).resolve().parents[2] / "schemas"
        context_schema = json.loads((schemas / "persistent-execution-context.v1.json").read_text())
        identity_schema = json.loads((schemas / "persistent-store-identity.v1.json").read_text())
        jsonschema.Draft202012Validator.check_schema(context_schema)
        jsonschema.Draft202012Validator.check_schema(identity_schema)
        jsonschema.validate(context.to_document(), context_schema)
        jsonschema.validate(identities[0], identity_schema)
        dispatcher_source = (Path(__file__).resolve().parents[1] / "accepted_hook_dispatch.py").read_text()
        install_at = dispatcher_source.index("context_authority.install_automatic_context")
        validate_at = dispatcher_source.index("    import validate", install_at)
        target_at = dispatcher_source.index("runpy.run_path(target", validate_at)
        if not install_at < validate_at < target_at:
            raise AssertionError("persistent context is not installed before accepted candidate imports")
        if ExecutionContext.from_document(context.to_document()).to_document() != context.to_document():
            raise AssertionError("context round-trip changed fields")
        try:
            context["activation"]["epoch"] = 2
            raise AssertionError("nested context mutation succeeded")
        except TypeError:
            pass

        mismatch_matrix = []
        for label, mutate in (
            ("activation", lambda doc: doc["activation"].update({"epoch": 2})),
            ("writer", lambda doc: doc["operation"].update({"writer": "memory.ledger.unknown"})),
            ("target", lambda doc: doc["target"].update({"kind": "unknown"})),
            ("expected-state", lambda doc: doc["state"].update({"ledger_generation": 99})),
            ("receipt", lambda doc: doc["receipt"].update({"context_id": "short"})),
        ):
            changed = context.to_document()
            mutate(changed)
            changed = _seal(changed)
            try:
                revalidate_context(ExecutionContext.from_document(changed))
                raise AssertionError(f"{label} mismatch was accepted")
            except ContextError:
                mismatch_matrix.append(label)

        disposable = os.path.join(temporary, "disposable")
        second_clone = os.path.join(temporary, "second-clone")
        os.makedirs(disposable)
        os.makedirs(second_clone)
        validate_disposable_target(
            disposable, canonical_project_root=root, canonical_memory_dir=memory,
            canonical_git_common_dir=common)
        disposable_context = resolve_execution_context(
            bootstrap, accepted_tree=accepted, script=".engine/tools/memory/candidate.py",
            target_kind="disposable", target_root=disposable, operation_id="ledger-append",
            provider="codex", run_id="candidate-run", task_id="candidate-task",
            extensions={"future_authorization": None},
            identity_initializer=_fixture_identity_initializer,
        )
        private_lifecycle = disposable_context["target"]["lifecycle"]
        for key in (*_HEALTH_FILENAMES, "backup_pointer", "erasure_proposal"):
            if not _within(private_lifecycle[key], disposable):
                raise AssertionError(f"disposable writable lifecycle path escaped private target: {key}")
        if private_lifecycle["canonical_backup_pointer"] != pointer_path:
            raise AssertionError("disposable context lost its read-only canonical recovery binding")
        revalidate_context(disposable_context)
        alias_matrix = []
        pointer_link = os.path.join(temporary, "pointer-link.json")
        os.symlink(pointer_path, pointer_link)
        try:
            _strict_pointer(pointer_link)
            raise AssertionError("symlinked backup pointer was accepted")
        except ContextError:
            alias_matrix.append("pointer-symlink")
        for label, candidate in (
                ("relative", "relative"), ("dot-dot", os.path.join(disposable, "..", "disposable")),
                ("canonical", root), ("memory", memory), ("common-dir", common)):
            try:
                validate_disposable_target(
                    candidate, canonical_project_root=root, canonical_memory_dir=memory,
                    canonical_git_common_dir=common)
                raise AssertionError(f"{label} disposable alias was accepted")
            except ContextError:
                alias_matrix.append(label)
        link = os.path.join(temporary, "linked")
        os.symlink(disposable, link)
        try:
            validate_disposable_target(
                link, canonical_project_root=root, canonical_memory_dir=memory,
                canonical_git_common_dir=common)
            raise AssertionError("symlinked disposable target was accepted")
        except ContextError:
            alias_matrix.append("symlink")
        # A second clone is not canonical merely because its repository identity may match; path/inode and
        # store identity remain distinct.
        validate_disposable_target(
            second_clone, canonical_project_root=root, canonical_memory_dir=memory,
            canonical_git_common_dir=common)
        alias_matrix.append("second-clone-distinct")

        git_root = os.path.join(temporary, "git-project")
        linked_worktree = os.path.join(temporary, "linked-worktree")
        git_memory = os.path.join(git_root, ".engine", "memory")
        os.makedirs(git_root)
        for command in (
            ["git", "-C", git_root, "init", "-b", "main"],
            ["git", "-C", git_root, "config", "user.name", "Context Fixture"],
            ["git", "-C", git_root, "config", "user.email", "fixture@example.invalid"],
        ):
            subprocess.run(command, check=True, capture_output=True)
        Path(os.path.join(git_root, "tracked.txt")).write_text("fixture\n")
        subprocess.run(["git", "-C", git_root, "add", "tracked.txt"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", git_root, "commit", "-m", "fixture"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", git_root, "worktree", "add", "-b", "fixture-linked", linked_worktree],
            check=True, capture_output=True)
        os.makedirs(git_memory)
        git_common = _git_common_dir(git_root)
        try:
            validate_disposable_target(
                linked_worktree, canonical_project_root=git_root, canonical_memory_dir=git_memory,
                canonical_git_common_dir=git_common)
            raise AssertionError("linked worktree disposable alias was accepted")
        except ContextError:
            alias_matrix.append("linked-worktree")

        capability = mint_capability(context, measured_cardinality=1)
        operation = context["operation"]
        receipt = consume_capability(
            capability, context=context, writer=operation["writer"], target_kind=operation["target_kind"],
            effect_class=operation["effect_class"], invocation_mode=operation["invocation_mode"],
            measured_cardinality=1, schema_cutover=operation["schema_cutover"],
            observed_state_fingerprint=context.expected_state_fingerprint,
        )
        if not receipt.to_document()["receipt_digest"].startswith("sha256:"):
            raise AssertionError("capability receipt has no digest")
        misuse = []
        for label, invoke in (
            ("reuse", lambda: consume_capability(
                capability, context=context, writer=operation["writer"], target_kind=operation["target_kind"],
                effect_class=operation["effect_class"], invocation_mode=operation["invocation_mode"],
                measured_cardinality=1, schema_cutover=False,
                observed_state_fingerprint=context.expected_state_fingerprint)),
            ("cardinality-overrun", lambda: mint_capability(context, measured_cardinality=2)),
            ("cross-registry", lambda: mint_capability(
                context, measured_cardinality=1, registry_id="backup-pointer-write")),
            ("cross-context", lambda: consume_capability(
                mint_capability(context, measured_cardinality=1),
                context=ExecutionContext.from_document(context.to_document()), writer=operation["writer"],
                target_kind=operation["target_kind"], effect_class=operation["effect_class"],
                invocation_mode=operation["invocation_mode"], measured_cardinality=1,
                schema_cutover=operation["schema_cutover"],
                observed_state_fingerprint=context.expected_state_fingerprint)),
            ("forged", lambda: consume_capability(
                object.__new__(OperationCapability), context=context, writer=operation["writer"],
                target_kind=operation["target_kind"], effect_class=operation["effect_class"],
                invocation_mode=operation["invocation_mode"], measured_cardinality=1, schema_cutover=False,
                observed_state_fingerprint=context.expected_state_fingerprint)),
        ):
            try:
                invoke()
                raise AssertionError(f"{label} capability misuse was accepted")
            except ContextError:
                misuse.append(label)
        for label, field, wrong in (
            ("cross-writer", "writer", "memory.ledger.bump_generation"),
            ("wrong-target", "target_kind", "remote-vault"),
            ("wrong-mode", "invocation_mode", "automatic"),
            ("stale-state", "observed_state_fingerprint", "sha256:" + "0" * 64),
        ):
            candidate = mint_capability(context, measured_cardinality=1)
            values = {
                "context": context, "writer": operation["writer"], "target_kind": operation["target_kind"],
                "effect_class": operation["effect_class"], "invocation_mode": operation["invocation_mode"],
                "measured_cardinality": 1, "schema_cutover": operation["schema_cutover"],
                "observed_state_fingerprint": context.expected_state_fingerprint,
            }
            values[field] = wrong
            try:
                consume_capability(candidate, **values)
                raise AssertionError(f"{label} capability misuse was accepted")
            except ContextError:
                misuse.append(label)

        raced = mint_capability(context, measured_cardinality=1)

        def race_consume():
            try:
                consume_capability(
                    raced, context=context, writer=operation["writer"],
                    target_kind=operation["target_kind"], effect_class=operation["effect_class"],
                    invocation_mode=operation["invocation_mode"], measured_cardinality=1,
                    schema_cutover=operation["schema_cutover"],
                    observed_state_fingerprint=context.expected_state_fingerprint,
                )
                return "consumed"
            except ContextError:
                return "refused"

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            race_results = sorted(pool.map(lambda _n: race_consume(), range(2)))
        if race_results != ["consumed", "refused"]:
            raise AssertionError(f"capability race was not atomic: {race_results}")
        misuse.append("concurrent-double-use")

        _CURRENT_CONTEXT = None
        return {
            "context_mismatch_matrix": mismatch_matrix,
            "capability_misuse_matrix": misuse,
            "identity_bootstrap": {"contenders": len(identities), "unique_ids": 1, "payload_unchanged": True},
            "path_alias_matrix": alias_matrix,
            "disposable_binding": {
                "writable_lifecycle_private": True, "canonical_recovery_read_only": True,
                "future_authorization": disposable_context["extensions"]["future_authorization"],
            },
            "pre_import_boundary": {
                "context_before_validate": True, "context_before_target": True,
                "memory_package_preimport_guard": 'if "memory" in sys.modules' in dispatcher_source,
            },
            "context_digest": context.digest,
        }


def main(argv: list[str]) -> int:
    if argv == ["self-test"]:
        print(json.dumps(_fixture_self_test(), indent=2, sort_keys=True))
        return 0
    print("usage: execution_context.py self-test", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
