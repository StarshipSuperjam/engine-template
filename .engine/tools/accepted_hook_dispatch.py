#!/usr/bin/env python3
"""Resolve and run automatic memory hooks from one attended, exact accepted Engine tree.

The file is deliberately a small bootstrap.  It runs from the active checkout only long enough to read the
common activation record, prove its exact Git object and canonical state binding, and start a fresh isolated
interpreter from the activated materialization.  No memory module is imported on that bootstrap side.

Trust boundary: this closes accidental candidate/stale-code execution against canonical durable memory.  It is
operational provenance, not protection from malicious code running as the same user: such code can rewrite the
launcher, Git common metadata, or the cache.  Stronger same-user isolation belongs to the future mediator work.
In the same spirit, the accepted interpreter is handed the project venv's installed package directory (see
``_site_paths``), which is NOT verified against uv.lock or any manifest: same-user code able to write the venv
can influence what the accepted lane imports.  That is the existing provenance boundary, not a new exposure.

Automatic callers may use only ``run`` or ``inspect``.  ``candidate`` is a separate attended lane: accepted
code creates a fresh non-aliasing disposable target, gives selected candidate code authority only for one
registered operation against that target, and emits a complete receipt.  ``activate`` is an attended
compare-and-set operation: it accepts a full commit already reachable from a reviewed default-branch ref, or
exactly named by a published release tag, and is the sole command that can advance the activation epoch.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
import re
import runpy
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from urllib.parse import quote


SCHEMA_VERSION = "accepted-hook-activation.v1"
CONTEXT_VERSION = "accepted-hook-context.v1"
CANDIDATE_RECEIPT_VERSION = "candidate-disposable-receipt.v1"
ACTIVATION_REL = os.path.join("engine", "accepted-hooks", "activation.json")
CACHE_REL = os.path.join("engine", "accepted-hooks", "trees")
LOCK_REL = os.path.join("engine", "accepted-hooks", "activation.lock")
AUTOMATIC_MUTATORS = frozenset({
    ".engine/tools/boot.py",
    ".engine/tools/close.py",
    ".engine/tools/memory/compact.py",
    ".engine/tools/memory/erasure_observer.py",
    ".engine/tools/memory/backup_vault.py",
})
_FULL_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", re.ASCII)
_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*", re.ASCII)
_SOURCE_KINDS = frozenset({"reviewed-merge", "published-release"})
#: The sibling reachability mark: written next to activation.json, keyed by the activated commit AND its
#: activation epoch, so a mark taken for one generation can never speak for a successor. It is NEVER written
#: into activation.json (that record is the CAS-guarded acceptance fact; reachability is a mutable, network-
#: derived observation about whether that commit still sits on the default branch).
REACHABILITY_REL = os.path.join("engine", "accepted-hooks", "reachability.json")
REACHABILITY_SCHEMA_VERSION = "accepted-hook-reachability.v1"
#: Reachability gets its OWN wall-clock window, opened only after the ambient boot budget has closed, so a
#: hung compare read can never eat into the 2.0s the session-start repo read is guaranteed.
REACHABILITY_BUDGET_SECONDS = 2.0
#: The registered attended operation a dispatched memory write runs under (mirrors
#: memory.write_dispatch.OPERATION; kept as a literal so this bootstrap never imports the memory package).
_WRITE_DISPATCH_OPERATION = "attended-write-dispatch"
_PYTHON_ENV_PREFIXES = ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE", "PYTHONSTARTUP")
_DISPOSABLE_PREFIX = "engine-memory-candidate-"
_DISPOSABLE_OPERATION_TARGETS = frozenset({
    "ledger", "ledger-metadata", "derived-index", "capture-cursor", "lifecycle-marker",
    "degraded-health", "backup-pointer", "restore-journal", "erasure-proposal", "ephemeral-staging",
    "semantic-index", "project-repository", "tracked-finding",
})


class QualificationError(RuntimeError):
    """A typed no-mutation outcome.  The CLI always maps it to exit 1, never the host's block code 2."""

class WriteHeld(Exception):
    """A dispatched memory write is refused because the activated commit is no longer on the default branch.

    It is deliberately NOT a ``QualificationError``: the dispatcher's degraded fallback catches only
    ``QualificationError`` and would (wrongly) route a write into the reads-only degraded child with the
    self-healing 'converges by itself' guidance. Reachability-lost does not self-heal — it clears only when
    the operator re-activates — so it must escape that fallback and carry its own posture sentence, which
    ``main`` relays to the write parent byte-for-byte.
    """


def _git(root: str, *args: str, check: bool = True) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", root, *args], capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise QualificationError(f"git unavailable while qualifying accepted hooks: {exc}") from exc
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "git command failed").strip().splitlines()[0][:240]
        raise QualificationError(detail)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _top(root: str) -> str:
    top = _git(root, "rev-parse", "--show-toplevel")
    if not top:
        raise QualificationError("the hook project is not a Git working tree")
    return os.path.realpath(top)


def _common_dir(root: str) -> str:
    raw = _git(root, "rev-parse", "--git-common-dir")
    if not raw:
        raise QualificationError("the repository's common Git directory is unreadable")
    return os.path.realpath(raw if os.path.isabs(raw) else os.path.join(root, raw))


def _main_checkout(root: str) -> str:
    text = _git(root, "worktree", "list", "--porcelain")
    first = text.split("\n\n", 1)[0]
    for line in first.splitlines():
        if line.startswith("worktree "):
            main = os.path.realpath(line[len("worktree "):].strip())
            if os.path.isdir(main):
                return main
            break
    raise QualificationError("the canonical main checkout could not be resolved")


def _origin_slug(root: str) -> str:
    raw = _git(root, "remote", "get-url", "origin")
    match = re.fullmatch(
        r"(?:(?:https?|ssh)://)?(?:[^@/]+@)?github\.com[:/]+([^/]+/[^/]+?)(?:\.git)?/?",
        raw.strip(), re.IGNORECASE | re.ASCII,
    )
    if not match:
        raise QualificationError("the repository origin is not a qualified GitHub owner/repository")
    slug = match.group(1)
    if not _SLUG.fullmatch(slug):
        raise QualificationError("the repository origin has an invalid owner/repository shape")
    return slug.casefold()


def _state_paths(root: str) -> tuple[str, str, str]:
    common = _common_dir(root)
    return (
        os.path.join(common, ACTIVATION_REL),
        os.path.join(common, CACHE_REL),
        os.path.join(common, LOCK_REL),
    )


@contextmanager
def _exclusive_lock(path: str):
    """POSIX lock for activation/cache CAS; fail closed where advisory locking is unavailable."""
    try:
        import fcntl
    except ImportError as exc:  # pragma: no cover - the Engine's automatic hook floor is POSIX today
        raise QualificationError("accepted-hook activation requires advisory file locking") from exc
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as handle:
        os.chmod(path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_json(path: str, value: dict, *, create_only: bool = False) -> bool:
    """Publish JSON atomically, optionally as a create-if-absent compare-and-set.

    The create-only form is used for persistent store identity.  It never replaces a winning identity and
    therefore cannot rewrite an existing store or any ledger payload.
    """
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".accepted-hook-", dir=os.path.dirname(path))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if create_only:
            try:
                os.link(tmp, path)
                published = True
            except FileExistsError:
                published = False
        else:
            os.replace(tmp, path)
            published = True
        try:
            dir_fd = os.open(os.path.dirname(path), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
    return published


def _read_json(path: str, label: str) -> dict:
    try:
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise QualificationError(f"{label} is not a regular file")
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except QualificationError:
        raise
    except (OSError, ValueError) as exc:
        raise QualificationError(f"{label} is absent or unreadable") from exc
    if not isinstance(value, dict):
        raise QualificationError(f"{label} is not a JSON object")
    return value


def _validate_activation(value: dict) -> dict:
    required = {
        "schema_version", "repository", "commit", "tree", "engine_release", "epoch",
        "source", "source_ref", "authority", "activated_at",
    }
    if set(value) != required or value.get("schema_version") != SCHEMA_VERSION:
        raise QualificationError("accepted-hook activation has an unknown or incomplete schema")
    if not isinstance(value["repository"], str) or not _SLUG.fullmatch(value["repository"]):
        raise QualificationError("accepted-hook activation repository is malformed")
    if not isinstance(value["commit"], str) or not _FULL_OID.fullmatch(value["commit"]):
        raise QualificationError("accepted-hook activation commit is not a full object id")
    if not isinstance(value["tree"], str) or not _FULL_OID.fullmatch(value["tree"]):
        raise QualificationError("accepted-hook activation tree is not a full object id")
    if not isinstance(value["engine_release"], str) or not value["engine_release"].strip():
        raise QualificationError("accepted-hook activation release is missing")
    if not isinstance(value["epoch"], int) or isinstance(value["epoch"], bool) or value["epoch"] < 1:
        raise QualificationError("accepted-hook activation epoch is invalid")
    if value["source"] not in _SOURCE_KINDS:
        raise QualificationError("accepted-hook activation source is invalid")
    if not isinstance(value["source_ref"], str) or not value["source_ref"]:
        raise QualificationError("accepted-hook activation source ref is missing")
    authority = value.get("authority")
    if (not isinstance(authority, dict) or set(authority) != {"kind", "evidence_id"}
            or authority.get("kind") not in {"github-merged-pull", "github-release-workflow"}
            or not isinstance(authority.get("evidence_id"), str) or not authority["evidence_id"]):
        raise QualificationError("accepted-hook activation authority proof is malformed")
    if not isinstance(value["activated_at"], str) or not value["activated_at"]:
        raise QualificationError("accepted-hook activation timestamp is missing")
    return dict(value)


def load_activation(root: str) -> dict:
    activation_path, _, _ = _state_paths(root)
    return _validate_activation(_read_json(activation_path, "accepted-hook activation"))


def _file_digest(path: str) -> str | None:
    try:
        with open(path, "rb") as handle:
            return "sha256:" + hashlib.sha256(handle.read()).hexdigest()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise QualificationError(f"state binding is unreadable: {path}") from exc


def _json_digest(value) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _safe_inventory(root: str, *, details: bool) -> dict:
    """Hash a tree without following or disclosing link targets; candidate receipts may include file details."""
    if not os.path.isdir(root) or os.path.islink(root):
        raise QualificationError(f"inventory root is not a regular directory: {root}")
    base = os.path.realpath(root)
    entries = []
    total_bytes = 0
    for current, dirs, files in os.walk(base, topdown=True, followlinks=False):
        dirs.sort()
        files.sort()
        for name in dirs + files:
            path = os.path.join(current, name)
            rel = os.path.relpath(path, base).replace(os.sep, "/")
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                raise QualificationError(f"inventory refused a symlink: {rel}")
            if stat.S_ISDIR(info.st_mode):
                entry = {"path": rel, "kind": "directory", "mode": stat.S_IMODE(info.st_mode)}
            elif stat.S_ISREG(info.st_mode):
                entry = {
                    "path": rel, "kind": "file", "mode": stat.S_IMODE(info.st_mode),
                    "size": info.st_size, "digest": _file_digest(path),
                }
                total_bytes += info.st_size
            else:
                raise QualificationError(f"inventory refused a special filesystem entry: {rel}")
            entries.append(entry)
    summary = {
        "entry_count": len(entries), "total_file_bytes": total_bytes,
        "inventory_digest": _json_digest(entries),
    }
    if details:
        summary["entries"] = entries
    return summary


def _tree_inventory(root: str) -> str:
    """Content/mode/link inventory for accidental cache drift (same-user hostile rewriting is out of scope)."""
    digest = hashlib.sha256()
    base = os.path.realpath(root)
    for current, dirs, files in os.walk(base, topdown=True, followlinks=False):
        dirs.sort()
        files.sort()
        for name in dirs + files:
            path = os.path.join(current, name)
            rel = os.path.relpath(path, base).replace(os.sep, "/")
            info = os.lstat(path)
            mode = stat.S_IFMT(info.st_mode) | stat.S_IMODE(info.st_mode)
            digest.update(f"{mode:o} {rel}\0".encode())
            if stat.S_ISLNK(info.st_mode):
                digest.update(os.readlink(path).encode("utf-8", "surrogateescape"))
            elif stat.S_ISREG(info.st_mode):
                with open(path, "rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
            digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def _materialized_paths(root: str, activation: dict) -> tuple[str, str]:
    _, cache_root, _ = _state_paths(root)
    key = f"{activation['commit']}-{activation['tree']}"
    return os.path.join(cache_root, key), os.path.join(cache_root, key + ".json")


# Exact tree binding (SR "materialized tree matches the accepted commit's git tree exactly"). The inventory
# self-hash in the marker catches accidental drift, but a same-user rewrite could forge the tree AND its
# marker together. The check below is ADDITIVE and derives its expectation from git, not the cache: the
# on-disk materialization must equal, as a set of (path, git-mode, blob-oid) entries, the manifest git
# records for the activation commit. Only the immutable per-commit git manifest is memoized; a "this tree
# matched" verdict is NEVER cached, because that is exactly the forgeable thing.
_COMMIT_MANIFEST_MEMO: dict[str, frozenset] = {}
_OBJECT_FORMAT_MEMO: dict[str, str] = {}


def _object_format(root: str) -> str:
    """The repository's git object hash (``sha1`` or ``sha256``), read once per root — it decides how an
    on-disk file's blob oid is computed so it can be compared to what ``git ls-tree`` reports."""
    memoized = _OBJECT_FORMAT_MEMO.get(root)
    if memoized is not None:
        return memoized
    try:
        proc = subprocess.run(["git", "-C", root, "rev-parse", "--show-object-format"],
                              capture_output=True, text=True, timeout=30)
        fmt = (proc.stdout or "").strip() if proc.returncode == 0 else "sha1"
    except (OSError, subprocess.SubprocessError):
        fmt = "sha1"
    fmt = fmt if fmt in ("sha1", "sha256") else "sha1"
    _OBJECT_FORMAT_MEMO[root] = fmt
    return fmt


def _blob_oid(path: str, object_format: str, *, is_symlink: bool) -> str:
    """The git blob oid a file's (or symlink target's) content would hash to under this repo's object format:
    ``<algo>("blob <size>\\0" + content)``. A symlink hashes the bytes of its target string, exactly as git
    stores a mode-120000 blob."""
    hasher = hashlib.sha256() if object_format == "sha256" else hashlib.sha1()
    if is_symlink:
        content = os.readlink(path).encode("utf-8", "surrogateescape")
        hasher.update(b"blob %d\x00" % len(content))
        hasher.update(content)
    else:
        hasher.update(b"blob %d\x00" % os.path.getsize(path))
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
    return hasher.hexdigest()


def _git_manifest(root: str, commit: str) -> frozenset:
    """The authoritative ``(path, git-mode, blob-oid)`` set git records for this commit's tree. Immutable per
    commit, so memoized. It is both the source ``_write_tree_from_objects`` materializes from and the
    expectation ``_valid_materialization`` compares against, so the two can only agree or fail closed.
    Gitlinks (submodules, mode 160000) are excluded: a submodule's content is not part of this repository's
    object store, so nothing is materialized for it and there is nothing on disk to compare it against."""
    memoized = _COMMIT_MANIFEST_MEMO.get(commit)
    if memoized is not None:
        return memoized
    try:
        proc = subprocess.run(["git", "-C", root, "ls-tree", "-r", "-z", commit],
                              capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise QualificationError("the accepted commit's tree manifest could not be read") from exc
    if proc.returncode != 0:
        raise QualificationError("the accepted commit's tree manifest could not be read")
    entries = []
    for raw in proc.stdout.decode("utf-8", "surrogateescape").split("\x00"):
        if not raw:
            continue
        meta, tab, path = raw.partition("\t")
        if not tab:
            raise QualificationError("the accepted commit's tree manifest is malformed")
        parts = meta.split(" ")
        if len(parts) != 3:
            raise QualificationError("the accepted commit's tree manifest is malformed")
        mode, _kind, oid = parts
        if mode == "160000":  # gitlink: never materialized, so out of the on-disk comparison
            continue
        entries.append((path, mode, oid))
    manifest = frozenset(entries)
    _COMMIT_MANIFEST_MEMO[commit] = manifest
    return manifest


def _ondisk_manifest(tree_path: str, object_format: str) -> frozenset:
    """The ``(path, git-mode, blob-oid)`` set the materialized tree actually presents on disk, symlink-as-
    symlink. Any entry a git tree cannot contain (a fifo, socket, or device) is encoded so the set comparison
    fails closed rather than silently ignoring it."""
    base = os.path.realpath(tree_path)
    entries = []
    for current, dirs, files in os.walk(base, topdown=True, followlinks=False):
        dirs.sort()
        files.sort()
        # A symlink whose target is a directory is listed in `dirs`; record it as a symlink (mode 120000) and
        # do not descend. Everything left in `dirs` is a real directory, which git trees do not enumerate.
        real_dirs = []
        for name in dirs:
            path = os.path.join(current, name)
            if os.path.islink(path):
                rel = os.path.relpath(path, base).replace(os.sep, "/")
                entries.append((rel, "120000", _blob_oid(path, object_format, is_symlink=True)))
            else:
                real_dirs.append(name)
        dirs[:] = real_dirs
        for name in files:
            path = os.path.join(current, name)
            rel = os.path.relpath(path, base).replace(os.sep, "/")
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                entries.append((rel, "120000", _blob_oid(path, object_format, is_symlink=True)))
            elif stat.S_ISREG(info.st_mode):
                mode = "100755" if (info.st_mode & stat.S_IXUSR) else "100644"
                entries.append((rel, mode, _blob_oid(path, object_format, is_symlink=False)))
            else:
                entries.append((rel, "000000", "unsupported-object"))
    return frozenset(entries)


def _scan_materialized_tree(tree_path: str, object_format: str) -> tuple[str, frozenset]:
    """One traversal of the materialized tree that returns both integrity views ``_valid_materialization``
    needs — the inventory self-hash (accidental cache-drift digest, byte-identical to ``_tree_inventory``) and
    the ``(path, git-mode, blob-oid)`` manifest (exact git-tree binding, equal to ``_ondisk_manifest``).

    Folding matters because both are computed on every dispatched write: computed separately they read and
    hash the whole tree's bytes twice (StarshipSuperjam/engine-template TI-3). Here each regular file and
    symlink is read exactly once and its bytes fed to BOTH the inventory digest and its own blob-oid hasher,
    so the per-write cost is a single content pass. ``_tree_inventory`` and ``_ondisk_manifest`` stay as the
    canonical single-view implementations for their other callers; test_hooks.py asserts this fold agrees with
    both, so the two walk descriptions cannot drift apart.
    """
    digest = hashlib.sha256()
    base = os.path.realpath(tree_path)
    entries = []
    for current, dirs, files in os.walk(base, topdown=True, followlinks=False):
        dirs.sort()
        files.sort()
        # Iterate exactly ``sorted(dirs) + sorted(files)`` so the inventory digest matches _tree_inventory's
        # ordering byte-for-byte; real directories are collected to steer descent after the entries are hashed.
        real_dirs = []
        for name in list(dirs) + list(files):
            path = os.path.join(current, name)
            rel = os.path.relpath(path, base).replace(os.sep, "/")
            info = os.lstat(path)
            mode = stat.S_IFMT(info.st_mode) | stat.S_IMODE(info.st_mode)
            digest.update(f"{mode:o} {rel}\0".encode())
            if stat.S_ISLNK(info.st_mode):
                target = os.readlink(path).encode("utf-8", "surrogateescape")
                digest.update(target)
                blob = hashlib.sha256() if object_format == "sha256" else hashlib.sha1()
                blob.update(b"blob %d\x00" % len(target))
                blob.update(target)
                entries.append((rel, "120000", blob.hexdigest()))
            elif stat.S_ISDIR(info.st_mode):
                real_dirs.append(name)  # a real directory: inventory-only, and git trees do not enumerate it
            elif stat.S_ISREG(info.st_mode):
                blob = hashlib.sha256() if object_format == "sha256" else hashlib.sha1()
                blob.update(b"blob %d\x00" % info.st_size)
                with open(path, "rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                        blob.update(chunk)
                git_mode = "100755" if (info.st_mode & stat.S_IXUSR) else "100644"
                entries.append((rel, git_mode, blob.hexdigest()))
            else:
                entries.append((rel, "000000", "unsupported-object"))
            digest.update(b"\0")
        dirs[:] = real_dirs
    return "sha256:" + digest.hexdigest(), frozenset(entries)


def _valid_materialization(root: str, activation: dict) -> str | None:
    tree_path, marker_path = _materialized_paths(root, activation)
    if not os.path.isdir(tree_path):
        return None
    try:
        marker = _read_json(marker_path, "accepted-hook materialization marker")
    except QualificationError:
        return None
    expected = {
        "schema_version": "accepted-hook-materialization.v1",
        "commit": activation["commit"],
        "tree": activation["tree"],
    }
    if any(marker.get(key) != value for key, value in expected.items()):
        return None
    # One folded walk (TI-3): the inventory self-hash and the on-disk (path, mode, blob-oid) manifest, reading
    # each file's bytes once instead of twice. _object_format fails open to sha1 and never raises; the only
    # QualificationError source here is _git_manifest reading the commit's authoritative manifest.
    try:
        inventory, ondisk = _scan_materialized_tree(tree_path, _object_format(root))
        git_manifest = _git_manifest(root, activation["commit"])
    except QualificationError:
        return None
    if marker.get("inventory") != inventory:
        return None
    # Exact tree binding, additive to the inventory self-hash above: the on-disk set of (path, mode, blob-oid)
    # must equal the manifest git records for the activation commit. The expectation comes from git, never the
    # cache, and any mismatch fails closed to None so the caller rebuilds the tree under the materialize lock.
    if ondisk != git_manifest:
        return None
    dispatch = os.path.join(tree_path, ".engine", "tools", "accepted_hook_dispatch.py")
    return tree_path if os.path.isfile(dispatch) else None


_MATERIALIZABLE_MODES = ("100644", "100755", "120000")


def _write_tree_from_objects(root: str, commit: str, dest: str) -> None:
    """Materialize ``commit``'s exact tree into ``dest`` straight from git's object store: the same
    ``git ls-tree`` manifest the binding later compares against, each blob's bytes read through
    ``git cat-file --batch`` and written by its git mode (plain file, executable file, symlink).

    Never ``git archive`` (SG2-1): archive honours the tree's own ``.gitattributes`` — ``export-ignore`` drops
    paths and ``eol``/``export-subst`` rewrite bytes — so its output can differ from the manifest, and the
    exact binding would then fail on every attempt in any project carrying such attributes, holding every
    memory write. Reading objects directly is attribute-blind by construction: the materialized set equals
    the manifest whenever the object store is intact. Paths come from git, but are still refused if they
    could escape ``dest``; any object kind a working tree cannot hold fails closed.
    """
    manifest = sorted(_git_manifest(root, commit))
    base = os.path.realpath(dest)
    for path, mode, _oid in manifest:
        parts = path.split("/")
        if not path or path.startswith("/") or any(part in ("", ".", "..") for part in parts):
            raise QualificationError("the accepted commit's tree manifest names an unsafe path")
        if mode not in _MATERIALIZABLE_MODES:
            raise QualificationError("the accepted commit's tree holds an object that cannot be materialized")
    try:
        proc = subprocess.run(["git", "-C", root, "cat-file", "--batch"],
                              input="".join(oid + "\n" for _path, _mode, oid in manifest).encode("ascii"),
                              capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        raise QualificationError("the accepted commit's objects could not be read") from exc
    if proc.returncode != 0:
        raise QualificationError("the accepted commit's objects could not be read")
    stream = proc.stdout
    cursor = 0
    for path, mode, oid in manifest:
        newline = stream.find(b"\n", cursor)
        if newline < 0:
            raise QualificationError("the accepted commit's object stream ended early")
        header = stream[cursor:newline].decode("ascii", "replace").split(" ")
        if len(header) != 3 or header[0] != oid or header[1] != "blob" or not header[2].isdigit():
            raise QualificationError("the accepted commit's object stream does not match its manifest")
        size = int(header[2])
        start = newline + 1
        content = stream[start:start + size]
        if len(content) != size:
            raise QualificationError("the accepted commit's object stream ended early")
        cursor = start + size + 1  # each object is followed by one newline
        target = os.path.join(base, *path.split("/"))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if mode == "120000":
            os.symlink(content.decode("utf-8", "surrogateescape"), target)
            continue
        with open(target, "wb") as handle:
            handle.write(content)
        os.chmod(target, 0o755 if mode == "100755" else 0o644)


def _materialize(root: str, activation: dict) -> str:
    """Materialize the exact accepted tree from git's object store into a staging directory, then atomically
    publish it with its marker. Attribute-blind by construction — see ``_write_tree_from_objects``."""
    existing = _valid_materialization(root, activation)
    if existing:
        return existing
    _, cache_root, lock_path = _state_paths(root)
    with _exclusive_lock(lock_path + ".materialize"):
        existing = _valid_materialization(root, activation)
        if existing:
            return existing
        os.makedirs(cache_root, mode=0o700, exist_ok=True)
        stage = tempfile.mkdtemp(prefix=".accepted-tree-", dir=cache_root)
        tree_path, marker_path = _materialized_paths(root, activation)
        try:
            _write_tree_from_objects(root, activation["commit"], stage)
            dispatch = os.path.join(stage, ".engine", "tools", "accepted_hook_dispatch.py")
            if not os.path.isfile(dispatch):
                raise QualificationError("the activated tree does not contain the accepted-hook dispatcher")
            inventory = _tree_inventory(stage)
            if os.path.lexists(tree_path):
                if os.path.isdir(tree_path) and not os.path.islink(tree_path):
                    shutil.rmtree(tree_path)
                else:
                    os.unlink(tree_path)
            os.replace(stage, tree_path)
            _atomic_json(marker_path, {
                "schema_version": "accepted-hook-materialization.v1",
                "commit": activation["commit"],
                "tree": activation["tree"],
                "inventory": inventory,
            })
        finally:
            shutil.rmtree(stage, ignore_errors=True)
    exact = _valid_materialization(root, activation)
    if not exact:
        raise QualificationError("the exact accepted tree could not be materialized coherently")
    return exact


def _verify_exact_object(root: str, activation: dict) -> None:
    commit = _git(root, "rev-parse", f"{activation['commit']}^{{commit}}")
    tree = _git(root, "rev-parse", f"{activation['commit']}^{{tree}}")
    if commit != activation["commit"] or tree != activation["tree"]:
        raise QualificationError("the activated commit/tree object is missing or inconsistent")
    if _origin_slug(root) != activation["repository"].casefold():
        raise QualificationError("the activated repository does not match this clone's origin")


def _configured_pointer(value) -> bool:
    """A pointer carrying real vault coordinates — the same shape the public-safety check gates on."""
    return isinstance(value, dict) and all(
        isinstance(value.get(key), str) and value.get(key) for key in ("owner", "repo", "namespace"))


def _home_pointer_split_is_mandated(canonical: str, accepted_tree: str, pointer: str,
                                    accepted_pointer: str) -> bool:
    """True iff a pointer-parity mismatch is the engine HOME repo's mandated split — the one state the
    engine requires and forbids reconciling: the accepted committed pointer is the unconfigured placeholder
    (engine/check/memory-pointer-public-safety, StarshipSuperjam/engine-template#224, forbids committing the
    configured pointer there) while the live canonical pointer carries the real vault coordinates, kept
    local via skip-worktree. Everything else — garbage on disk, two differing configured pointers, any
    non-home repository — stays a refusal.

    Every input fails TOWARD refusal: an unreadable pointer, manifest, or origin returns False and the
    parity refusal stands (deliberately the opposite direction from `repo_identity.is_home_repo`, whose
    fail-toward-home is right for a detector that makes a safety check RUN, and wrong for one that would
    stand a safety refusal DOWN). The home judgment reads `home_repository` from the ACCEPTED tree's
    manifest — operator-accepted, digest-pinned state — never from the editable working tree."""
    try:
        committed = _read_json(accepted_pointer, "activated committed pointer")
        live = _read_json(pointer, "canonical backup pointer")
        manifest = _read_json(os.path.join(accepted_tree, ".engine", "engine.json"), "accepted manifest")
        origin = _origin_slug(canonical)
    except QualificationError:
        return False
    if committed.get("configured") is not False or _configured_pointer(committed):
        return False
    if not _configured_pointer(live):
        return False
    home = manifest.get("home_repository")
    if not isinstance(home, str) or not home.strip():
        return False
    return origin == home.strip().casefold()


def _canonical_context(root: str, activation: dict, accepted_tree: str | None = None) -> dict:
    canonical = _main_checkout(root)
    common = _common_dir(root)
    if _common_dir(canonical) != common:
        raise QualificationError("the canonical checkout does not share the activated repository")
    memory_dir = os.path.realpath(os.path.join(canonical, ".engine", "memory"))
    pointer = os.path.join(canonical, ".engine", "memory-backup", "pointer.json")
    accepted_pointer = (
        os.path.join(accepted_tree, ".engine", "memory-backup", "pointer.json") if accepted_tree else None
    )
    pointer_digest = _file_digest(pointer)
    accepted_pointer_digest = _file_digest(accepted_pointer) if accepted_pointer else pointer_digest
    if accepted_tree and pointer_digest != accepted_pointer_digest and not _home_pointer_split_is_mandated(
            canonical, accepted_tree, pointer, accepted_pointer):
        raise QualificationError("the canonical backup pointer differs from the activated committed pointer")
    pointer_identity = None
    if pointer_digest:
        try:
            pointer_value = _read_json(pointer, "canonical backup pointer")
            pointer_identity = {
                key: pointer_value.get(key)
                for key in ("schema_version", "configured", "owner", "repo", "branch", "namespace")
                if key in pointer_value
            }
        except QualificationError:
            pointer_identity = None
    root_stat = os.stat(canonical)
    lifecycle = {
        "ledger": os.path.join(memory_dir, "ledger.ndjson"),
        "ledger_meta": os.path.join(memory_dir, "ledger-meta.json"),
        "keyword_index": os.path.join(memory_dir, "index.sqlite3"),
        "semantic_index": os.path.join(memory_dir, "vectors.sqlite3"),
        "restore_transaction": os.path.join(memory_dir, ".restore-transaction.json"),
        "migration_in_flight": os.path.join(memory_dir, "migration-in-flight.json"),
        "migration_stamp": os.path.join(memory_dir, "migration-stamp.json"),
        "capture_cursor": os.path.join(memory_dir, "capture-state.json"),
        "capture_lock": os.path.join(memory_dir, ".capture.lock"),
        "backup_state": os.path.join(memory_dir, "backup-vault-state.json"),
        "erasure_proposal": os.path.join(canonical, ".engine", "erasures", "proposal.json"),
        "backup_pointer": pointer,
    }
    return {
        "schema_version": CONTEXT_VERSION,
        "activation": {
            key: activation[key]
            for key in ("repository", "commit", "tree", "engine_release", "epoch")
        },
        "canonical": {
            "project_root": canonical,
            "project_root_identity": {"device": root_stat.st_dev, "inode": root_stat.st_ino},
            "git_common_dir": common,
            "memory_dir": memory_dir,
            "backup_pointer_digest": pointer_digest,
            "backup_pointer_identity": pointer_identity,
            "lifecycle": lifecycle,
        },
    }


def _activation_topology(root: str) -> dict:
    """Load the dependency-light read-only topology authority from the attended checkout."""
    path = os.path.join(root, ".engine", "tools", "hooks_path_health.py")
    try:
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > 512 * 1024:
            raise QualificationError("accepted-hook topology authority is unsafe")
        spec = importlib.util.spec_from_file_location("_engine_accepted_hook_topology", path)
        if spec is None or spec.loader is None:
            raise QualificationError("accepted-hook topology authority is unavailable")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        if os.path.realpath(getattr(module, "__file__", "")) != os.path.realpath(path):
            raise QualificationError("accepted-hook topology authority escaped the attended checkout")
        topology = module.accepted_hook_topology(root)
    except QualificationError:
        raise
    except Exception as exc:
        raise QualificationError("registered Git worktree topology could not be qualified") from exc
    if (not isinstance(topology, dict) or not isinstance(topology.get("qualified"), bool)
            or not isinstance(topology.get("state"), str) or not isinstance(topology.get("worktrees"), list)):
        raise QualificationError("registered Git worktree topology returned an invalid verdict")
    return topology


def _provider_authority(tree: str):
    """Load the provider vocabulary seam without importing candidate paths implicitly."""
    path = os.path.join(tree, ".engine", "tools", "providers.py")
    try:
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > 512 * 1024:
            raise QualificationError("provider authority is unsafe")
        spec = importlib.util.spec_from_file_location("_engine_accepted_provider_authority", path)
        if spec is None or spec.loader is None:
            raise QualificationError("provider authority is unavailable")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        previous_bytecode = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            spec.loader.exec_module(module)
        finally:
            sys.dont_write_bytecode = previous_bytecode
        if os.path.realpath(getattr(module, "__file__", "")) != os.path.realpath(path):
            raise QualificationError("provider authority escaped its qualified tree")
        if module.PROVIDER_ENV in _PYTHON_ENV_PREFIXES:
            raise QualificationError("provider authority returned an invalid environment binding")
        return module
    except QualificationError:
        raise
    except Exception as exc:
        raise QualificationError("provider authority could not be loaded") from exc


def uncovered_worktrees(root: str) -> dict:
    """Which registered worktrees this activation does NOT cover — a disclosure, never a refusal.

    StarshipSuperjam/engine-template#1153 made an unqualified worktree topology REFUSE activation. That protected nothing: a pre-fix
    worktree runs its own pre-fix wiring whether or not this machine's activation advances, so refusing
    only stripped protection from the sessions that could actually have it. The honest report is a count
    the operator can see and act on, and the mechanism that genuinely covers those worktrees is the
    store-side cutover deferred to its own issue.

    Returns ``{"total", "uncovered", "sample", "state"}``, or ``{"readable": False}`` when the topology
    itself cannot be resolved — an unreadable topology is reported, never treated as clean.

    "Cannot be resolved" means more than "raised", and the deliverable review is why. The census answers
    ``unreadable`` (no toplevel, or ``git worktree list`` failed), ``ambiguous`` (no paths, or duplicates) and
    ``concurrent-change`` (the list moved while being read) by RETURNING those verdicts with an empty worktree
    list — which counted as zero offenders and rendered nothing at all. Since the refusal is gone and this
    disclosure is the whole of what replaced it, going quiet in exactly the states where the machine cannot
    tell whether it is covered is the one failure it must not have. Only ``qualified`` and ``blocked`` are
    census answers; everything else is unreadable.
    """
    try:
        topology = _activation_topology(root)
    except QualificationError as exc:
        return {"readable": False, "reason": str(exc), "total": None, "uncovered": None, "sample": []}
    if topology.get("state") not in ("qualified", "blocked"):
        return {"readable": False, "reason": f"the worktree census answered {topology.get('state')}",
                "state": topology.get("state"), "total": None, "uncovered": None, "sample": []}
    records = [record for record in topology["worktrees"] if isinstance(record, dict)]
    offenders = [record for record in records if record.get("state") != "qualified"]
    return {
        "readable": True,
        "state": topology["state"],
        "total": len(records),
        "uncovered": len(offenders),
        "sample": [
            f"{record.get('ref', 'unknown')} [{record.get('worktree_id', 'unknown')}]: "
            f"{record.get('state', 'unknown')}"
            + (f" ({record['component']})" if record.get("component") else "")
            for record in offenders[:6]
        ],
    }


def _engine_manifest_at(root: str, commit: str) -> dict:
    raw = _git(root, "show", f"{commit}:.engine/engine.json")
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise QualificationError("the accepted commit has no readable Engine manifest") from exc
    if not isinstance(value, dict):
        raise QualificationError("the accepted commit's Engine manifest is not an object")
    return value


def _engine_release_at(root: str, commit: str) -> str:
    """The release string the accepted commit declares. The key is `engine_release`, which is what
    `.engine/engine.json` has always carried; reading a key the real manifest never had is what made
    every activation refuse in StarshipSuperjam/engine-template#1153."""
    value = _engine_manifest_at(root, commit)
    version = value.get("engine_release")
    if not isinstance(version, str) or not version.strip():
        raise QualificationError("the accepted commit's Engine release is missing")
    return version


AMBIENT_BOOT_BUDGET_SECONDS = 2.0
_AMBIENT_DEADLINE: float | None = None


@contextmanager
def _ambient_budget(seconds: float = AMBIENT_BOOT_BUDGET_SECONDS):
    """Bound every GitHub read inside this block to one shared wall-clock budget.

    Ambient activation runs at session start, where a slow or hanging network call is indistinguishable
    from a broken session. The budget is shared across the whole attempt rather than per-call, so three
    sequential reads cannot add up to three timeouts, and it is enforced even when a single ``gh`` invocation
    would otherwise block on a credential prompt.
    """
    global _AMBIENT_DEADLINE
    previous = _AMBIENT_DEADLINE
    _AMBIENT_DEADLINE = time.monotonic() + seconds
    try:
        yield
    finally:
        _AMBIENT_DEADLINE = previous


def _github_timeout() -> float:
    """The seconds this call may take: the whole remaining ambient budget, or the attended default."""
    if _AMBIENT_DEADLINE is None:
        return 30.0
    remaining = _AMBIENT_DEADLINE - time.monotonic()
    if remaining <= 0:
        raise QualificationError("ambient activation exceeded its session-start budget")
    return remaining


def _github_json(endpoint: str):
    """Read one bounded GitHub acceptance fact through the operator's authenticated gh session."""
    timeout = _github_timeout()
    environment = {key: value for key, value in os.environ.items() if key not in _PYTHON_ENV_PREFIXES}
    # Never let activation stop at an interactive prompt: a hook has no terminal to answer one on, and an
    # unauthenticated `gh` must fail fast into the degraded path rather than hang holding the session.
    environment.update({"GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1", "GIT_TERMINAL_PROMPT": "0"})
    try:
        proc = subprocess.run(
            ["gh", "api", endpoint], capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL, env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        raise QualificationError(
            "GitHub acceptance proof did not answer within its time budget; qualification stays as it was"
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise QualificationError(
            "GitHub acceptance proof is unavailable; authenticate the GitHub CLI and retry activation"
        ) from exc
    if proc.returncode != 0:
        raise QualificationError(
            "GitHub acceptance proof was refused; verify GitHub CLI access and the selected commit"
        )
    try:
        return json.loads(proc.stdout)
    except ValueError as exc:
        raise QualificationError("GitHub acceptance proof returned unreadable data") from exc


_DEFAULT_BRANCH_MEMO: dict[str, str] = {}


def _github_default_branch(repository: str) -> str:
    """GitHub's own current default branch, read once per process — the ambient budget is shared, and one
    activation attempt asks for this fact from several places."""
    memoized = _DEFAULT_BRANCH_MEMO.get(repository)
    if memoized is not None:
        return memoized
    value = _github_json(f"repos/{repository}")
    branch = value.get("default_branch") if isinstance(value, dict) else None
    valid = (subprocess.run(
        ["git", "check-ref-format", "--branch", branch], capture_output=True, timeout=30,
    ).returncode == 0) if isinstance(branch, str) and branch else False
    if not valid:
        raise QualificationError("GitHub repository metadata has no usable default branch")
    _DEFAULT_BRANCH_MEMO[repository] = branch
    return branch


def _verify_github_acceptance(repository: str, commit: str, source: str, source_ref: str,
                              *, default_branch: str | None = None) -> dict:
    """Require an independent, immutable GitHub-side acceptance witness for the exact commit."""
    if source == "reviewed-merge":
        branch = source_ref.removeprefix("refs/remotes/origin/").removeprefix("refs/heads/")
        authoritative_branch = default_branch or _github_default_branch(repository)
        if branch != authoritative_branch:
            raise QualificationError("the reviewed merge ref is not GitHub's current default branch")
        pulls = _github_json(f"repos/{repository}/commits/{commit}/pulls")
        if not isinstance(pulls, list):
            raise QualificationError("GitHub reviewed-merge proof had an unexpected shape")
        accepted = [pull for pull in pulls if isinstance(pull, dict)
                    and pull.get("merged_at")
                    and pull.get("merge_commit_sha") == commit
                    and isinstance(pull.get("base"), dict)
                    and pull["base"].get("ref") == branch
                    and isinstance(pull.get("number"), int)]
        if not accepted:
            raise QualificationError(
                "the exact commit has no merged GitHub pull request into the recorded default branch"
            )
        return {"kind": "github-merged-pull", "evidence_id": str(accepted[0]["number"])}
    tag = source_ref.removeprefix("refs/tags/")
    release = _github_json(f"repos/{repository}/releases/tags/{tag}")
    if (not isinstance(release, dict) or release.get("tag_name") != tag
            or not isinstance(release.get("id"), int)):
        raise QualificationError("the selected tag has no identifiable published GitHub release")
    tag_ref = _github_json(f"repos/{repository}/git/ref/tags/{quote(tag, safe='')}")
    tag_object = tag_ref.get("object") if isinstance(tag_ref, dict) else None
    if (not isinstance(tag_object, dict) or tag_object.get("type") != "commit"
            or tag_object.get("sha") != commit):
        raise QualificationError("the published GitHub release tag does not name the exact selected commit")
    runs = _github_json(
        f"repos/{repository}/actions/workflows/release-publish.yml/runs?head_sha={commit}&status=completed&per_page=100"
    )
    values = runs.get("workflow_runs") if isinstance(runs, dict) else None
    successful = [run for run in values or [] if isinstance(run, dict)
                  and run.get("head_sha") == commit and run.get("conclusion") == "success"
                  and isinstance(run.get("id"), int)]
    if not successful:
        raise QualificationError(
            "the exact release commit has no successful immutable release-publish workflow witness"
        )
    return {
        "kind": "github-release-workflow",
        "evidence_id": f"{release['id']}:{successful[0]['id']}:{commit}",
    }


def activate(args: argparse.Namespace) -> dict:
    # Activation is an attended operation running from the tree the operator selected. Keep this import
    # inside that lane: automatic ``run``/``inspect`` must not import any current-worktree Engine module
    # before they have transferred into the exact accepted materialization.
    import moment

    root = _top(args.root)
    repository = args.repository.casefold()
    if not _SLUG.fullmatch(args.repository) or repository != _origin_slug(root):
        raise QualificationError("the requested repository does not exactly match this clone's origin")
    if not _FULL_OID.fullmatch(args.commit):
        raise QualificationError("activation requires a full lowercase commit object id")
    commit = _git(root, "rev-parse", f"{args.commit}^{{commit}}")
    if commit != args.commit:
        raise QualificationError("activation commit did not resolve byte-exactly")
    tree = _git(root, "rev-parse", f"{commit}^{{tree}}")
    if args.source == "published-release":
        tag_ref = args.source_ref if args.source_ref.startswith("refs/tags/") else f"refs/tags/{args.source_ref}"
        if _git(root, "rev-parse", f"{tag_ref}^{{commit}}") != commit:
            raise QualificationError("the published release tag does not name the requested exact commit")
        source_ref = tag_ref
    else:
        default_branch = _github_default_branch(repository)
        allowed_refs = {
            f"refs/heads/{default_branch}", f"refs/remotes/origin/{default_branch}",
        }
        if not (args.source_ref.startswith("refs/heads/") or args.source_ref.startswith("refs/remotes/origin/")):
            raise QualificationError("a reviewed merge must be qualified through an explicit default-branch ref")
        source_ref = args.source_ref
        if source_ref not in allowed_refs:
            raise QualificationError("the reviewed merge ref is not GitHub's current default branch")
        proc = subprocess.run(
            ["git", "-C", root, "merge-base", "--is-ancestor", commit, source_ref],
            capture_output=True, timeout=30,
        )
        if proc.returncode != 0:
            raise QualificationError("the accepted commit is not reachable from the reviewed branch")
    actual_release = _engine_release_at(root, commit)
    if actual_release != args.engine_release:
        raise QualificationError("the declared Engine release differs from the accepted commit's manifest")
    authority = _verify_github_acceptance(
        repository, commit, args.source, source_ref,
        default_branch=default_branch if args.source == "reviewed-merge" else None,
    )
    activation_path, _, lock_path = _state_paths(root)
    with _exclusive_lock(lock_path):
        if os.path.exists(activation_path):
            current = _validate_activation(_read_json(activation_path, "accepted-hook activation"))
            current_epoch = current["epoch"]
        else:
            current_epoch = 0
        if current_epoch != args.expected_epoch:
            raise QualificationError(
                f"activation compare-and-set failed: expected epoch {args.expected_epoch}, found {current_epoch}"
            )
        record = {
            "schema_version": SCHEMA_VERSION,
            "repository": repository,
            "commit": commit,
            "tree": tree,
            "engine_release": actual_release,
            "epoch": current_epoch + 1,
            "source": args.source,
            "source_ref": source_ref,
            "authority": authority,
            "activated_at": moment.utc_now(),
        }
        _materialize(root, record)
        _atomic_json(activation_path, record)
    return record


def _default_branch_tip(root: str, repository: str) -> tuple[str, str]:
    """The canonical checkout's default-branch commit and the exact ref that names it.

    The REMOTE-tracking ref is preferred, and the order matters. GitHub is where the acceptance proof lives,
    so the ref that mirrors GitHub is the more faithful answer to "what is the default branch". The practical
    difference the deliverable review found: if the operator force-pushes the default branch to undo a merge,
    their local branch may still contain that merge, and resolving from it would re-qualify a commit they
    have visibly rejected. `origin/<branch>` reflects the rollback as soon as they fetch. This does not make
    the check authoritative — a clone that has not fetched still sees the old tip, which is why an
    authoritative reachability read against GitHub is tracked separately — but it closes the common case at
    no cost, and it cannot stall an ordinary merge: pulling a merge updates the remote-tracking ref too.
    """
    canonical = _main_checkout(root)
    branch = _github_default_branch(repository)
    for candidate in (f"refs/remotes/origin/{branch}", f"refs/heads/{branch}"):
        commit = _git(canonical, "rev-parse", f"{candidate}^{{commit}}", check=False)
        if commit:
            return commit, candidate
    raise QualificationError(
        "the canonical checkout has no local record of its GitHub default branch; fetch it and retry"
    )


def ensure_activation(root: str, notices: list | None = None) -> dict:
    """Keep, bootstrap, or advance this machine's activation — always leaving it usable.

    Three states, one entry point, because a session start cannot ask an operator anything:

    * **absent** — bootstrap to the canonical checkout's default-branch tip.
    * **stale** — the default branch has moved ahead of the activated commit, so advance to it. The move
      must be FORWARD: the new commit has to be a descendant of the activated one, which makes a rollback,
      a force-push, or a branch swap unable to walk qualification backwards.
    * **current** — verify the recorded object and keep it.

    Every advance still needs the same GitHub acceptance proof a first activation does: a pull request the
    operator merged, whose merge commit IS this commit, on GitHub's own default branch. A direct push can
    therefore never qualify — it simply stalls advancement until the next merged pull request.
    """
    root = _top(root)
    repository = _origin_slug(root)
    activation_path, _, _ = _state_paths(root)
    if not os.path.lexists(activation_path):
        commit, ref = _default_branch_tip(root, repository)
        return activate(argparse.Namespace(
            root=root, repository=repository, commit=commit, source="reviewed-merge", source_ref=ref,
            engine_release=_engine_release_at(root, commit), expected_epoch=0,
        ))
    current = load_activation(root)
    if current["repository"] != repository:
        raise QualificationError("the accepted activation belongs to a different repository")
    _verify_exact_object(root, current)
    _materialize(root, current)
    if current["source"] != "reviewed-merge":
        return current  # a pinned published release is an operator's explicit choice; never auto-advance it
    # A FAILED ADVANCE NEVER COSTS A WORKING ACTIVATION. No network, no GitHub CLI, a branch that was
    # force-pushed — each leaves the machine qualified at the commit it already had, which is strictly
    # better than the alternative of tearing down qualification because a newer one could not be proven.
    try:
        commit, ref = _default_branch_tip(root, repository)
        if commit == current["commit"]:
            return current
        forward = subprocess.run(
            ["git", "-C", _main_checkout(root), "merge-base", "--is-ancestor", current["commit"], commit],
            capture_output=True, timeout=30,
        )
        if forward.returncode != 0:
            raise QualificationError(
                "the default branch no longer descends from the activated commit"
            )
        return activate(argparse.Namespace(
            root=root, repository=repository, commit=commit, source="reviewed-merge", source_ref=ref,
            engine_release=_engine_release_at(root, commit), expected_epoch=current["epoch"],
        ))
    except QualificationError as exc:
        message = (f"Engine memory kept working with the code from commit {current['commit'][:12]}; it could "
                   f"not move up to your project's latest merged code this session. Nothing is blocked by "
                   f"that. Detail: {exc}.")
        if notices is None:
            print(message, file=sys.stderr)
        else:
            notices.append(message)
        return current


def ensure_activation_ambient(root: str) -> tuple[dict | None, list[str]]:
    """Converge activation at session start without ever being able to hold the session up.

    Returns ``(activation_or_None, notices)``. Every failure is a notice, never an exception: an
    unauthenticated GitHub CLI, no network, a slow API, a rolled-back branch — each one leaves the session
    running unqualified, which the effect tiering already handles. What the operator is owed is being
    TOLD, which is what the notices carry into the status block.
    """
    notices: list[str] = []
    try:
        before = load_activation(_top(root)) if os.path.lexists(_state_paths(_top(root))[0]) else None
    except QualificationError:
        before = None
    try:
        with _ambient_budget():
            record = ensure_activation(root, notices)
    except QualificationError as exc:
        notices.append(_degraded_notice(str(exc)))
        return None, notices
    except Exception as exc:  # noqa: BLE001 — session start is fail-open; an unexpected fault degrades too
        notices.append(_degraded_notice(f"activation could not be resolved ({exc})"))
        return None, notices
    if before is None:
        notices.append(
            f"Engine memory can now write to this project's memory on this machine. It is running the code "
            f"from your merged commit {record['commit'][:12]}.")
    elif before["commit"] != record["commit"]:
        notices.append(
            f"Engine memory moved to the code from your merged commit {record['commit'][:12]} (it was "
            f"{before['commit'][:12]}). That is the code now allowed to write to memory.")
    # Reachability is measured LAST, in its own budget: whether the commit we just kept/advanced to still
    # sits on GitHub's default branch. A 'lost' result appends its own operator notice and arms the write
    # hold; 'unconfirmed' appends a calm notice and never holds; 'reachable' stays quiet. It can never hold
    # up session start — it never raises.
    try:
        measure_reachability(_top(root), record, notices=notices)
    except Exception:  # noqa: BLE001 — defense in depth; measure_reachability already swallows its own faults
        pass
    return record, notices


def _degraded_notice(detail: str) -> str:
    """The line an operator reads when qualification did not converge this session.

    Two things the deliverable review found missing. First, what still WORKS: the earlier line interpolated a
    raw internal error and stopped there, so a reader had no way to tell an inconvenience from a breakage,
    while the sentence that would have reassured them was printed to a stream they never see. Second, plain
    words: the internal detail is kept, because it is the only thing that distinguishes "not signed in to
    GitHub" from "offline", but it comes last and is labelled as detail rather than led with.
    """
    return ("Engine memory is not able to write to memory this session. Reading and recall work normally, "
            "and anything said in the meantime is kept in the conversation transcript and written to memory "
            "by the next session that can. It sorts itself out at a session start that can reach GitHub. "
            f"Technical detail, for a bug report rather than for you to act on: {detail}.")


def _reachability_posture(epoch: int) -> str:
    """The operator-facing sentence for a write held because the activated commit left the default branch.

    Recovery is a session RESTART, not a command the operator runs by hand: a fresh session re-resolves
    activation against the project's current default-branch commit (``ensure_activation_ambient``), and once
    that commit is reachable the hold clears on its own — the 'lost' mark is keyed to the old commit and epoch,
    so it stops matching the instant activation advances. The earlier text pointed at the ``activate`` verb,
    which is a seven-argument compare-and-set an operator cannot run unaided (StarshipSuperjam/engine-template
    US-1); the runnable recovery is the same restart every other refusal names. It never claims the state
    'converges by itself' inside THIS session — the running server stays pinned to the commit that left the
    branch until it is restarted.

    The restart action and escalation pointer are written verbatim here rather than imported: the bootstrap
    side must import no ``memory`` module (see the module docstring), so the single-source guarantee is held
    by a test (test_hooks.py) that asserts this sentence carries ``refusals.RESTART_ACTION`` and
    ``refusals.ESCALATION`` byte-for-byte, closing the drift StarshipSuperjam/engine-template#1211 warns of.
    The same sentence is shown as a session-start notice and relayed as the write refusal, so an operator
    reads one consistent explanation wherever it surfaces; it names no commit, path, or writer identity —
    only the activation epoch.

    It claims exactly what the hold covers (SG2-2): the hold is enforced for ``attended-write-dispatch``
    only — the saves an assistant makes from the conversation through the memory helper (a pin, setting a
    note aside, restoring one). Automatic turn capture and the memory command-line tools dispatch under their
    own operations and are not held, so the sentence says so instead of announcing that all saving is held.
    The recovery step is stated as what the operator actually does (US2-1): pull the default branch, then
    restart — not "get back on the default branch", which reads as a branch switch and is not the fix.
    """
    return ("The commit this project's memory activated on is no longer on the project's default branch, so "
            "saves made from this conversation through the memory helper (pins, setting a note aside, "
            "restoring one) are held. Nothing was changed, recall keeps working, automatic turn capture "
            "continues, and the memory command-line tools are unaffected. Pull the project's default branch "
            "so your copy has its latest merged commit. "
            "To fully reconnect, quit Claude Desktop completely and reopen it so the memory server restarts "
            "(in a Codex session, end the session and start a new one). A fresh start re-activates on the "
            "current commit and clears this hold. "
            "If this keeps happening after a restart, run /engine-status and open an engine issue. "
            f"(activation epoch {epoch})")


def _reachability_path(root: str) -> str:
    return os.path.join(_common_dir(root), REACHABILITY_REL)


def _reachability_lock(root: str) -> str:
    _, _, lock_path = _state_paths(root)
    return lock_path + ".reachability"


def reachability_state(root: str, activation: dict) -> str | None:
    """The recorded reachability state for THIS activation generation, or ``None``.

    Honored only when the mark's commit AND epoch both match the activation asking, so a mark taken for a
    superseded generation is silently ignored rather than mistaken for the current one. Every read fault —
    a missing mark, a symlinked or non-regular file, unreadable or non-JSON bytes — fails OPEN to ``None``:
    this reader only ADDS the default-branch-reachability refusal on top of the activation-object and
    exact-tree gates, and must never turn its own read trouble into a spurious hold.
    """
    try:
        path = _reachability_path(root)
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return None
        with open(path, encoding="utf-8") as handle:
            mark = json.load(handle)
    except (OSError, ValueError, QualificationError):
        # Every read fault fails OPEN: a missing/symlinked/unreadable mark (OSError), non-JSON bytes
        # (ValueError), or a tree with no readable Git common dir (QualificationError, from _reachability_path
        # -> _common_dir). This reader only ADDS a hold on a CONFIRMED loss; it must never manufacture one out
        # of its own read trouble.
        return None
    if (not isinstance(mark, dict)
            or mark.get("schema_version") != REACHABILITY_SCHEMA_VERSION
            or mark.get("commit") != activation.get("commit")
            or mark.get("epoch") != activation.get("epoch")):
        return None
    state = mark.get("state")
    return state if state in ("lost", "unconfirmed") else None


def _reachability_lost(root: str, activation: dict) -> bool:
    """True only for a confirmed 'lost'. 'unconfirmed' never holds a write: writing follows the merge, and a
    session that could not reach GitHub to confirm reachability must still be able to save."""
    return reachability_state(root, activation) == "lost"


def _record_reachability(root: str, activation: dict, state: str) -> None:
    """Persist the reachability observation under lock, keyed to this activation generation.

    Precedence, so ordering between concurrent or late measurements cannot corrupt the record:
      * a mark owned by a NEWER generation (higher epoch) is never walked back by an older measurement;
      * ``reachable`` clears the mark (the write hold, if any, is lifted);
      * ``lost`` is written, overwriting anything of this-or-older generation;
      * ``unconfirmed`` is written for disclosure only — it never creates a hold — and never over a
        confirmed ``lost`` of this same generation, so a later timeout can never quietly undo a confirmed
        loss.
    """
    path = _reachability_path(root)
    with _exclusive_lock(_reachability_lock(root)):
        current = None
        try:
            info = os.lstat(path)
            if stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                with open(path, encoding="utf-8") as handle:
                    current = json.load(handle)
        except (OSError, ValueError):
            current = None
        if (isinstance(current, dict) and isinstance(current.get("epoch"), int)
                and isinstance(activation.get("epoch"), int)
                and current["epoch"] > activation["epoch"]):
            return  # a newer generation already owns the mark; never let an older read overwrite it
        if state == "reachable":
            try:
                os.unlink(path)
            except OSError:
                pass
            return
        if state == "unconfirmed" and (
                isinstance(current, dict) and current.get("state") == "lost"
                and current.get("commit") == activation.get("commit")
                and current.get("epoch") == activation.get("epoch")):
            return  # a confirmed loss of this generation stands; a timeout never walks it back
        if state in ("lost", "unconfirmed"):
            _atomic_json(path, {
                "schema_version": REACHABILITY_SCHEMA_VERSION,
                "commit": activation["commit"],
                "epoch": activation["epoch"],
                "state": state,
            })


def _reachability_unconfirmed_notice(commit: str, reason: str) -> str:
    """The calm operator sentence for a reachability check that could not complete: disclosed, never a
    hold. Writing follows the merge, so an offline session still saves; the check runs again next start."""
    return (f"Engine memory could not confirm with GitHub that its activated commit {commit[:12]} is still "
            f"on the default branch ({reason}). Memory writing continues, and the check runs again at the "
            "next session start.")


def measure_reachability(root: str, activation: dict, *, notices: list | None = None) -> str:
    """Confirm the activated reviewed-merge commit is still on GitHub's default branch, and record it.

    Runs in reachability's OWN budget, opened only here — after the ambient boot budget has already closed in
    ``ensure_activation_ambient`` — so a hung compare read cannot borrow against the session-start repo read.
    Reads GitHub's compare of ``default_branch...activated_commit``: ``identical``/``behind`` establish
    reachability, ``ahead``/``diverged`` do not. Never raises: a time-budget or CLI failure is recorded as
    ``unconfirmed`` (which does not hold writes) and disclosed through one calm notice. A published release
    is a pinned operator choice and is not measured against the default branch.

    Returns the state it observed ('reachable' | 'lost' | 'unconfirmed'), for callers and tests.
    """
    if activation.get("source") != "reviewed-merge":
        return "reachable"
    repository = activation["repository"]
    commit = activation["commit"]

    def unconfirmed(reason: str) -> str:
        _record_reachability(root, activation, "unconfirmed")
        if notices is not None:
            notices.append(_reachability_unconfirmed_notice(commit, reason))
        return "unconfirmed"

    try:
        with _ambient_budget(REACHABILITY_BUDGET_SECONDS):
            default_branch = _github_default_branch(repository)
            comparison = _github_json(
                f"repos/{repository}/compare/{quote(default_branch, safe='')}...{commit}")
        status = comparison.get("status") if isinstance(comparison, dict) else None
    except QualificationError as exc:
        return unconfirmed(str(exc) or "GitHub could not be read")
    except Exception:  # noqa: BLE001 — reachability must never break session start
        return unconfirmed("an unexpected fault while reading GitHub")
    if status in ("identical", "behind"):
        _record_reachability(root, activation, "reachable")
        return "reachable"
    if status in ("ahead", "diverged"):
        _record_reachability(root, activation, "lost")
        if notices is not None:
            notices.append(_reachability_posture(activation["epoch"]))
        return "lost"
    return unconfirmed(f"GitHub answered with an unrecognized comparison status {status!r}")


def _relative_script(root: str, script: str) -> str:
    absolute = os.path.realpath(script if os.path.isabs(script) else os.path.join(root, script))
    rel = os.path.relpath(absolute, root).replace(os.sep, "/")
    if rel not in AUTOMATIC_MUTATORS or absolute != os.path.realpath(os.path.join(root, rel)):
        raise QualificationError("the dispatcher was asked to run an unregistered automatic mutator")
    return rel


def _venv_site_packages(executable: str | None = None, *,
                        version_info: object | None = None,
                        os_name: str | None = None) -> str | None:
    """The site-packages directory of the venv that owns ``executable``, or None if it is not in one.

    Isolation-flag safe.  Under ``-I -S`` the accepted interpreter runs with no ``site`` module, so a virtual
    environment is invisible and sysconfig reports the *base* interpreter's packages instead; the engine's own
    dependencies (jsonschema, imported by the session relay) then cannot be found and boot's grounding
    assembly crashes.  This computes the venv's package directory from the interpreter path alone, without
    importing ``site`` or trusting sysconfig: the venv root is two directories above the interpreter and is
    marked by a ``pyvenv.cfg``; its package directory is ``lib/python<major>.<minor>/site-packages`` on POSIX
    or ``Lib/site-packages`` on Windows.

    The interpreter path is used WITHOUT resolving symlinks: a venv's ``bin/python`` is typically a symlink to
    the base interpreter, and sys.executable deliberately keeps the venv path so the root is reachable.  A
    missing package directory returns None so the caller falls back to the ordinary scan rather than inventing
    a path that does not exist.
    """
    executable = sys.executable if executable is None else executable
    if not executable:
        return None
    version_info = sys.version_info if version_info is None else version_info
    os_name = os.name if os_name is None else os_name
    venv_root = os.path.dirname(os.path.dirname(os.path.abspath(executable)))
    if not os.path.isfile(os.path.join(venv_root, "pyvenv.cfg")):
        return None
    if os_name == "nt":
        candidate = os.path.join(venv_root, "Lib", "site-packages")
    else:
        candidate = os.path.join(
            venv_root, "lib", f"python{version_info[0]}.{version_info[1]}", "site-packages")
    if os.path.isdir(candidate):
        return os.path.realpath(candidate)
    return None


def _site_paths() -> list[str]:
    # Prefer the venv derivation: under -I -S it is the only path that finds the engine's own packages, and
    # when it succeeds it is returned ALONE so the base interpreter's site-packages cannot outrank it.  The
    # sysconfig/sys.path scan is kept only as the fallback for a non-venv interpreter.
    venv_site = _venv_site_packages()
    if venv_site is not None:
        return [venv_site]
    paths = []
    try:
        import sysconfig
        for key in ("purelib", "platlib"):
            value = sysconfig.get_paths().get(key)
            if isinstance(value, str) and os.path.isdir(value):
                paths.append(os.path.realpath(value))
    except (ImportError, OSError):
        pass
    for value in sys.path:
        if not isinstance(value, str) or not value:
            continue
        real = os.path.realpath(value)
        if ("site-packages" in real or "dist-packages" in real) and os.path.isdir(real):
            paths.append(real)
    return sorted(set(paths))


def dispatch(root: str, script: str, target_args: list[str]) -> None:
    root = _top(root)
    rel = _relative_script(root, script)
    activation = load_activation(root)
    _verify_exact_object(root, activation)
    accepted_tree = _materialize(root, activation)
    context = _canonical_context(root, activation, accepted_tree)
    provider_authority = _provider_authority(accepted_tree)
    context["invocation"] = {
        "script": rel,
        "provider": provider_authority.detect(),
        "run_id": provider_authority.resolve_session(),
    }
    env = {
        key: value for key, value in os.environ.items()
        if key not in _PYTHON_ENV_PREFIXES and key not in {"ENGINE_MEMORY_DIR", "ENGINE_PROJECT_ROOT"}
    }
    canonical = context["canonical"]
    env.update({
        "PYTHONNOUSERSITE": "1",
        provider_authority.PROVIDER_ENV: context["invocation"]["provider"],
        "ENGINE_PROJECT_ROOT": canonical["project_root"],
        "ENGINE_MEMORY_DIR": canonical["memory_dir"],
        "ENGINE_BOOT_CACHE_DIR": os.path.join(canonical["project_root"], ".engine", "telemetry", ".cache"),
        "ENGINE_ACCEPTED_HOOK_CONTEXT": json.dumps(context, sort_keys=True, separators=(",", ":")),
    })
    env["PYTHONDONTWRITEBYTECODE"] = "1"  # belt for any non-isolated descendant; -B below covers the child
    accepted_dispatch = os.path.join(accepted_tree, ".engine", "tools", "accepted_hook_dispatch.py")
    argv = [
        sys.executable, "-I", "-S", "-B", accepted_dispatch, "_run-accepted",
        "--tree", accepted_tree, "--script", rel,
    ]
    for site_path in _site_paths():
        argv.extend(["--site-path", site_path])
    argv.append("--")
    argv.extend(target_args)
    os.chdir(canonical["project_root"])
    os.execve(sys.executable, argv, env)


DEGRADED_ENV = "ENGINE_QUALIFICATION_DEGRADED"


def _dispatch_attended_degraded(root: str, absolute: str, rel: str, reason: str,
                                target_args: list[str]) -> None:
    """Run one attended tool from the live checkout when no accepted tree can be resolved.

    Availability comes first: an attended tool the operator launched — above all the memory MCP server,
    which a host starts once per session — must never fail to start because qualification is unavailable.
    The degrade hands the tool the canonical roots and NO execution context, which is precisely the signal
    the mutation authority tiers on: reads and health answer, canonical authoring refuses.
    """
    canonical = _main_checkout(root)
    if _common_dir(canonical) != _common_dir(root):
        raise QualificationError("the canonical checkout does not share this repository")
    memory_dir = os.path.realpath(os.path.join(canonical, ".engine", "memory"))
    env = {
        key: value for key, value in os.environ.items()
        if key not in {"ENGINE_MEMORY_DIR", "ENGINE_PROJECT_ROOT", "ENGINE_ACCEPTED_HOOK_CONTEXT",
                       "ENGINE_PERSISTENT_EXECUTION_CONTEXT"}
    }
    # The tool is launched by absolute path, so its own directory — not the tools root its package imports
    # resolve against — would be sys.path[0]. Name the tools root explicitly.
    tools_root = os.path.dirname(os.path.dirname(absolute))
    existing_path = env.get("PYTHONPATH")
    env.update({
        "ENGINE_PROJECT_ROOT": canonical,
        "ENGINE_MEMORY_DIR": memory_dir,
        "ENGINE_BOOT_CACHE_DIR": os.path.join(canonical, ".engine", "telemetry", ".cache"),
        "PYTHONPATH": tools_root + (os.pathsep + existing_path if existing_path else ""),
        DEGRADED_ENV: reason,
    })
    print(f"Engine {rel} is running unqualified: {reason}. Reads and health work; "
          f"writing to memory waits for qualification.", file=sys.stderr)
    os.chdir(canonical)
    os.execve(sys.executable, [sys.executable, absolute, *target_args], env)


def dispatch_attended(root: str, script: str, operation: str, target_args: list[str]) -> None:
    """Leave the attended checkout and re-enter one exact registered operation in accepted code."""
    root = _top(root)
    absolute = os.path.abspath(script if os.path.isabs(script) else os.path.join(root, script))
    tools_root = os.path.join(root, ".engine", "tools") + os.sep
    if (absolute != os.path.realpath(absolute) or not absolute.startswith(tools_root)
            or not absolute.endswith(".py") or not os.path.isfile(absolute)):
        raise QualificationError("attended script must be one normalized regular Python tool")
    rel = os.path.relpath(absolute, root).replace(os.sep, "/")
    try:
        activation = load_activation(root)
        _verify_exact_object(root, activation)
        accepted_tree = _materialize(root, activation)
    except QualificationError as exc:
        _dispatch_attended_degraded(root, absolute, rel, str(exc), target_args)
        return  # os.execve never returns
    # Reachability hold (place b), in the OUTER launcher before any exec: for the write-dispatch operation
    # only, a CONFIRMED-lost activation refuses the write here with the posture sentence, so no accepted
    # child is ever launched for a write the merge no longer authorizes. It is raised as WriteHeld — NOT a
    # QualificationError — so the degraded fallback above cannot swallow it into a silent re-exec; main()
    # turns it into the response line write_dispatch relays verbatim. Reads and health never pass here, and
    # the accepted child carries its own independent backstop (place a) for the case this is bypassed.
    if operation == _WRITE_DISPATCH_OPERATION and _reachability_lost(root, activation):
        raise WriteHeld(_reachability_posture(activation["epoch"]))
    context = _canonical_context(root, activation, accepted_tree)
    provider_authority = _provider_authority(accepted_tree)
    context["invocation"] = {
        "script": rel,
        "provider": provider_authority.detect(),
        "run_id": provider_authority.resolve_session(),
    }
    env = {
        key: value for key, value in os.environ.items()
        if key not in _PYTHON_ENV_PREFIXES and key not in {
            "ENGINE_MEMORY_DIR", "ENGINE_PROJECT_ROOT", "ENGINE_PERSISTENT_EXECUTION_CONTEXT",
        }
    }
    canonical = context["canonical"]
    env.update({
        "PYTHONNOUSERSITE": "1",
        provider_authority.PROVIDER_ENV: context["invocation"]["provider"],
        "ENGINE_PROJECT_ROOT": canonical["project_root"],
        "ENGINE_MEMORY_DIR": canonical["memory_dir"],
        "ENGINE_BOOT_CACHE_DIR": os.path.join(canonical["project_root"], ".engine", "telemetry", ".cache"),
        "ENGINE_ACCEPTED_HOOK_CONTEXT": json.dumps(context, sort_keys=True, separators=(",", ":")),
    })
    env["PYTHONDONTWRITEBYTECODE"] = "1"  # belt for any non-isolated descendant; -B below covers the child
    accepted_dispatch = os.path.join(accepted_tree, ".engine", "tools", "accepted_hook_dispatch.py")
    argv = [
        sys.executable, "-I", "-S", "-B", accepted_dispatch, "_run-attended",
        "--tree", accepted_tree, "--script", rel, "--operation", operation,
    ]
    for site_path in _site_paths():
        argv.extend(["--site-path", site_path])
    argv.append("--")
    argv.extend(target_args)
    os.chdir(canonical["project_root"])
    os.execve(sys.executable, argv, env)


def dispatch_candidate(args: argparse.Namespace) -> None:
    """Leave candidate code and re-enter the exact accepted dispatcher for the attended lane."""
    root = _top(args.root)
    activation = load_activation(root)
    _verify_exact_object(root, activation)
    accepted_tree = _materialize(root, activation)
    accepted_dispatch = os.path.join(accepted_tree, ".engine", "tools", "accepted_hook_dispatch.py")
    env = {
        key: value for key, value in os.environ.items()
        if key not in _PYTHON_ENV_PREFIXES and key not in {
            "ENGINE_MEMORY_DIR", "ENGINE_PROJECT_ROOT", "ENGINE_PERSISTENT_EXECUTION_CONTEXT",
            "ENGINE_CANDIDATE_DISPOSABLE",
        }
    }
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"  # belt for any non-isolated descendant; -B below covers the child
    argv = [
        sys.executable, "-I", "-S", "-B", accepted_dispatch, "_run-candidate", "--tree", accepted_tree,
        "--root", root, "--candidate-root", args.candidate_root, "--script", args.script,
        "--target-root", args.target_root, "--operation", args.operation,
        "--provider", args.provider, "--timeout", str(args.timeout),
    ]
    if args.run_id is not None:
        argv.extend(["--run-id", args.run_id])
    if args.task_id is not None:
        argv.extend(["--task-id", args.task_id])
    for site_path in _site_paths():
        argv.extend(["--site-path", site_path])
    argv.append("--")
    argv.extend(args.target_args)
    os.execve(sys.executable, argv, env)


def _engine_names(tools_root: str) -> set[str]:
    names = set()
    stdlib = getattr(sys, "stdlib_module_names", set())
    for child in os.listdir(tools_root):
        path = os.path.join(tools_root, child)
        if child.endswith(".py") and child != "__init__.py":
            name = child[:-3]
        elif os.path.isdir(path) and os.path.isfile(os.path.join(path, "__init__.py")):
            name = child
        else:
            continue
        if name not in stdlib:
            names.add(name)
    return names


def _verify_tools_do_not_escape(accepted_tree: str, tools_root: str) -> None:
    base = os.path.realpath(accepted_tree) + os.sep
    for current, dirs, files in os.walk(tools_root, topdown=True, followlinks=False):
        for name in dirs + files:
            path = os.path.join(current, name)
            if os.path.islink(path) and not os.path.realpath(path).startswith(base):
                raise QualificationError(f"accepted Engine tool symlink escapes its exact tree: {path}")


class _AcceptedEngineFinder(importlib.abc.MetaPathFinder):
    def __init__(self, tools_root: str, names: set[str]):
        self.tools_root = tools_root
        self.names = names

    def find_spec(self, fullname: str, path=None, target=None):  # noqa: ANN001 - importlib protocol
        top = fullname.partition(".")[0]
        if top not in self.names:
            return None
        search = [self.tools_root] if "." not in fullname else path
        return importlib.machinery.PathFinder.find_spec(fullname, search)


def _origin_report(accepted_tree: str, engine_names: set[str]) -> list[dict]:
    base = os.path.realpath(accepted_tree) + os.sep
    report = []
    violations = []
    for name, module in sorted(sys.modules.items()):
        if name.partition(".")[0] not in engine_names:
            continue
        origin = getattr(module, "__file__", None)
        if not isinstance(origin, str):
            violations.append(f"{name}=<no-file>")
            continue
        real = os.path.realpath(origin)
        report.append({"module": name, "origin": real})
        if not real.startswith(base):
            violations.append(f"{name}={real}")
    if violations:
        raise QualificationError("Engine module escaped the accepted tree: " + ", ".join(violations[:5]))
    return report


def _run_exact_accepted(args: argparse.Namespace, operation_id: str | None = None) -> int:
    accepted_tree = os.path.realpath(args.tree)
    tools_root = os.path.join(accepted_tree, ".engine", "tools")
    rel = args.script
    if operation_id is None and rel not in AUTOMATIC_MUTATORS:
        raise QualificationError("accepted interpreter received an unregistered automatic mutator")
    if operation_id is not None and (not rel.startswith(".engine/tools/") or not rel.endswith(".py")):
        raise QualificationError("accepted interpreter received an invalid attended tool path")
    target = os.path.realpath(os.path.join(accepted_tree, rel))
    if not target.startswith(accepted_tree + os.sep) or not os.path.isfile(target):
        raise QualificationError("accepted mutator is absent from the materialized tree")
    try:
        context = json.loads(os.environ["ENGINE_ACCEPTED_HOOK_CONTEXT"])
    except (KeyError, ValueError) as exc:
        raise QualificationError("accepted hook context is absent or malformed") from exc
    if not isinstance(context, dict) or context.get("schema_version") != CONTEXT_VERSION:
        raise QualificationError("accepted hook context version is not supported")
    canonical = context.get("canonical")
    if not isinstance(canonical, dict) or os.environ.get("ENGINE_MEMORY_DIR") != canonical.get("memory_dir"):
        raise QualificationError("accepted hook context and canonical memory binding disagree")
    # The outer bootstrap is candidate code.  Treat its envelope only as evidence and independently rebuild
    # every durable binding from this accepted dispatcher before any target or memory package can import.
    root = _top(canonical.get("project_root", os.getcwd()))
    activation = load_activation(root)
    _verify_exact_object(root, activation)
    qualified_tree = _valid_materialization(root, activation)
    if qualified_tree is None or os.path.realpath(qualified_tree) != accepted_tree:
        raise QualificationError("accepted interpreter tree no longer matches the active exact materialization")
    authoritative = _canonical_context(root, activation, accepted_tree)
    if context.get("activation") != authoritative["activation"]:
        raise QualificationError("outer and accepted activation bindings disagree")
    if canonical != authoritative["canonical"]:
        raise QualificationError("outer and accepted canonical state bindings disagree")
    invocation = context.get("invocation")
    if not isinstance(invocation, dict) or invocation.get("script") != rel:
        raise QualificationError("outer invocation does not name this accepted mutator")
    if invocation.get("provider") not in {"claude", "codex"}:
        raise QualificationError("outer invocation provider is not qualified")
    authoritative["invocation"] = invocation
    _verify_tools_do_not_escape(accepted_tree, tools_root)
    clean_sites = [os.path.realpath(path) for path in args.site_path if os.path.isdir(path)]
    sys.path[:] = [tools_root, *[
        path for path in sys.path
        if isinstance(path, str) and path and os.path.realpath(path) not in clean_sites
    ], *clean_sites]
    names = _engine_names(tools_root)
    sys.meta_path.insert(0, _AcceptedEngineFinder(tools_root, names))
    if "memory" in sys.modules:
        raise QualificationError("memory package imported before persistent context installation")
    context_path = os.path.join(tools_root, "memory", "execution_context.py")
    spec = importlib.util.spec_from_file_location("_engine_execution_context", context_path)
    if spec is None or spec.loader is None:
        raise QualificationError("accepted persistent context authority is unavailable")
    context_authority = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = context_authority
    spec.loader.exec_module(context_authority)
    if os.path.realpath(getattr(context_authority, "__file__", "")) != os.path.realpath(context_path):
        raise QualificationError("persistent context authority escaped the accepted tree")

    def initialize_store_identity(identity_path: str, lock_path: str, candidate: dict) -> None:
        with _exclusive_lock(lock_path):
            _atomic_json(identity_path, candidate, create_only=True)

    try:
        if operation_id is None:
            persistent_context = context_authority.install_automatic_context(
                authoritative, accepted_tree=accepted_tree, script=rel,
                identity_initializer=initialize_store_identity,
            )
        else:
            contract = context_authority._load_contract()
            try:
                entry = contract.entry_by_id(operation_id)
            except Exception as exc:
                raise QualificationError("attended operation is absent from the accepted registry") from exc
            script_module = rel[len(".engine/tools/"):-3].replace("/", ".")
            if ("attended" not in entry["allowed_invocation_modes"]
                    or entry["writer"].rpartition(".")[0] != script_module):
                raise QualificationError("attended operation does not belong to the selected accepted tool")
            persistent_context = context_authority.install_attended_context(
                authoritative, accepted_tree=accepted_tree, script=rel, operation_id=operation_id,
                identity_initializer=initialize_store_identity,
            )
    except context_authority.ContextError as exc:
        raise QualificationError(f"persistent execution context refused: {exc}") from exc
    import validate
    _origin_report(accepted_tree, names)
    validate.ROOT = persistent_context["project"]["root"]
    old_argv = sys.argv
    sys.argv = [target, *args.target_args]
    code = 0
    pending: BaseException | None = None
    try:
        runpy.run_path(target, run_name="__main__")
    except SystemExit as exc:
        pending = exc
        if exc.code is None:
            code = 0
        elif isinstance(exc.code, int):
            code = exc.code
        else:
            print(exc.code, file=sys.stderr)
            code = 1
    except BaseException as exc:  # preserve the normal traceback after the origin gate runs
        pending = exc
    finally:
        sys.argv = old_argv
    _origin_report(accepted_tree, names)
    if pending is not None and not isinstance(pending, SystemExit):
        raise pending
    return code


def run_accepted(args: argparse.Namespace) -> int:
    return _run_exact_accepted(args)


def run_attended(args: argparse.Namespace) -> int:
    return _run_exact_accepted(args, args.operation)


def _load_context_authority(accepted_tree: str):
    path = os.path.join(accepted_tree, ".engine", "tools", "memory", "execution_context.py")
    spec = importlib.util.spec_from_file_location("_engine_candidate_context_authority", path)
    if spec is None or spec.loader is None:
        raise QualificationError("accepted persistent context authority is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    if os.path.realpath(getattr(module, "__file__", "")) != os.path.realpath(path):
        raise QualificationError("candidate context authority escaped the accepted tree")
    return module


def _candidate_code_identity(candidate_root: str, script: str) -> dict:
    if (not os.path.isabs(candidate_root) or os.path.abspath(candidate_root) != candidate_root
            or os.path.realpath(candidate_root) != candidate_root):
        raise QualificationError("candidate root must be one normalized non-link absolute path")
    root = _top(candidate_root)
    if root != candidate_root:
        raise QualificationError("candidate root must name its exact Git top-level directory")
    tools = os.path.join(root, ".engine", "tools")
    allowed = os.path.join(tools, "memory") + os.sep
    raw_script = script if os.path.isabs(script) else os.path.join(root, script)
    absolute = os.path.abspath(raw_script)
    if absolute != raw_script or os.path.realpath(absolute) != absolute or not absolute.startswith(allowed):
        raise QualificationError("candidate script must be a normalized non-link path under .engine/tools/memory")
    try:
        info = os.lstat(absolute)
    except OSError as exc:
        raise QualificationError("candidate script is absent or unreadable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise QualificationError("candidate script is not a regular file")
    commit = _git(root, "rev-parse", "HEAD^{commit}")
    tree = _git(root, "rev-parse", "HEAD^{tree}")
    status = _git(root, "status", "--porcelain=v1", "-z")
    script_digest = _file_digest(absolute)
    if script_digest is None:
        raise QualificationError("candidate script disappeared during qualification")
    return {
        "repository": _origin_slug(root), "project_root": root, "commit": commit, "tree": tree,
        "script": os.path.relpath(absolute, root).replace(os.sep, "/"),
        "script_path": absolute, "script_digest": script_digest,
        "worktree_dirty": bool(status), "worktree_status_digest": _json_digest(status),
    }


def _create_disposable_target(requested: str, canonical: dict) -> str:
    if not isinstance(requested, str) or not os.path.isabs(requested):
        raise QualificationError("candidate target must be an absolute path")
    target = os.path.abspath(requested)
    if target != requested or os.path.realpath(target) != target:
        raise QualificationError("candidate target is normalized differently or crosses a symlink")
    temporary_root = os.path.realpath(tempfile.gettempdir())
    parent = os.path.dirname(target)
    if parent != temporary_root:
        raise QualificationError("candidate target must be a direct child of the operating-system temp directory")
    if not os.path.basename(target).startswith(_DISPOSABLE_PREFIX):
        raise QualificationError(f"candidate target basename must begin with {_DISPOSABLE_PREFIX}")
    if os.path.lexists(target):
        raise QualificationError("candidate target already exists; accepted code requires an absent path")
    for path in (canonical["project_root"], canonical["memory_dir"], canonical["git_common_dir"]):
        try:
            if os.path.commonpath((target, path)) in (target, path):
                raise QualificationError("candidate target aliases canonical project or Git state")
        except ValueError:
            continue
    try:
        os.mkdir(target, mode=0o700)
    except OSError as exc:
        raise QualificationError("accepted code could not create the candidate target") from exc
    return target


def _existing_store_lock(path: str):
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise QualificationError("canonical store identity lock is absent") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise QualificationError("canonical store identity lock is not a regular file")
    return _exclusive_lock(path)


def _candidate_registry_boundary(authority, operation_id: str) -> list[str]:
    """Close one attended operation over registered call edges, then prove every target is private-bindable."""
    contract = authority._load_contract()
    entries = {entry["id"]: entry for entry in contract.REGISTRY}
    if operation_id not in entries:
        raise QualificationError("candidate operation is absent from the accepted registry")
    allowed = {operation_id}
    changed = True
    while changed:
        changed = False
        writers = {entries[entry_id]["writer"] for entry_id in allowed}
        for writer in tuple(writers):
            for entry_id in contract.TRANSITIVE_BOUNDARIES.get(writer, ()):
                if entry_id not in allowed:
                    allowed.add(entry_id)
                    changed = True
        for entry_id, entry in entries.items():
            if entry_id not in allowed and writers.intersection(entry.get("callers", ())):
                allowed.add(entry_id)
                changed = True
    problems = []
    for entry_id in sorted(allowed):
        entry = entries.get(entry_id)
        if entry is None:
            problems.append(f"{entry_id}=missing")
        elif "attended" not in entry["allowed_invocation_modes"]:
            problems.append(f"{entry_id}=automatic-only")
        elif entry["target_kind"] not in _DISPOSABLE_OPERATION_TARGETS:
            problems.append(f"{entry_id}={entry['target_kind']}")
    if problems:
        raise QualificationError(
            "candidate operation reaches authority outside its disposable target: " + ", ".join(problems[:8]))
    return sorted(allowed)


def _candidate_receipt(*, activation: dict, accepted_tree: str, candidate: dict, context,
                       target_root: str, result, before: dict, after: dict, timed_out: bool) -> dict:
    document = context.to_document()
    script_after = _file_digest(candidate["script_path"])
    status_after = _git(candidate["project_root"], "status", "--porcelain=v1", "-z")
    candidate_receipt = {key: value for key, value in candidate.items() if key != "script_path"}
    candidate_receipt.update({
        "script_digest_after": script_after,
        "worktree_status_digest_after": _json_digest(status_after),
        "code_unchanged_during_run": (
            script_after == candidate["script_digest"]
            and _json_digest(status_after) == candidate["worktree_status_digest"]
        ),
    })
    stdout = result.stdout or b""
    stderr = result.stderr or b""
    receipt = {
        "schema_version": CANDIDATE_RECEIPT_VERSION,
        "accepted": {
            **{key: activation[key] for key in (
                "repository", "commit", "tree", "engine_release", "epoch", "source", "source_ref")},
            "materialized_tree": accepted_tree, "materialized_inventory": _tree_inventory(accepted_tree),
        },
        "candidate": candidate_receipt,
        "target": {
            "kind": "disposable", "root": target_root,
            "memory_dir": document["target"]["memory_dir"],
            "store_id": document["target"]["store_identity"]["store_id"],
            "inventory": _safe_inventory(target_root, details=True),
        },
        "operation": {
            **document["operation"],
            "authorized_registry_ids": document["extensions"]["candidate_authorized_registry_ids"],
        },
        "invocation": document["invocation"],
        "outcome": {
            "exit_code": result.returncode, "timed_out": timed_out,
            "stdout_size": len(stdout), "stdout_digest": "sha256:" + hashlib.sha256(stdout).hexdigest(),
            "stderr_size": len(stderr), "stderr_digest": "sha256:" + hashlib.sha256(stderr).hexdigest(),
        },
        "canonical": {
            "memory_inventory_before": before, "memory_inventory_after": after,
            "unchanged": before == after,
        },
        "context_digest": context.digest,
        "future_authorization": None,
        "receipt_digest": None,
    }
    receipt["receipt_digest"] = _json_digest(receipt)
    return receipt


def run_candidate(args: argparse.Namespace) -> int:
    """Accepted half of the attended candidate lane; automatic hook dispatch never reaches this command."""
    accepted_tree = os.path.realpath(args.tree)
    own_tree = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if accepted_tree != own_tree:
        raise QualificationError("candidate lane is not executing from the named accepted tree")
    root = _top(args.root)
    activation = load_activation(root)
    _verify_exact_object(root, activation)
    qualified = _valid_materialization(root, activation)
    if qualified is None or os.path.realpath(qualified) != accepted_tree:
        raise QualificationError("candidate lane no longer matches the active exact materialization")
    authoritative = _canonical_context(root, activation, accepted_tree)
    candidate = _candidate_code_identity(args.candidate_root, args.script)
    if candidate["repository"] != activation["repository"].casefold():
        raise QualificationError("candidate repository origin differs from the accepted activation")
    if args.provider not in {"claude", "codex"}:
        raise QualificationError("candidate invocation provider is not qualified")

    authority = _load_context_authority(accepted_tree)
    try:
        registered = authority._entry(args.operation)
    except authority.ContextError as exc:
        raise QualificationError(f"candidate operation refused: {exc}") from exc
    if "attended" not in registered["allowed_invocation_modes"]:
        raise QualificationError("candidate operation is not registered for attended invocation")
    authorized_registry_ids = _candidate_registry_boundary(authority, args.operation)
    canonical_identity = authority.read_store_identity(authoritative["canonical"]["memory_dir"])
    if (canonical_identity is None or canonical_identity.get("target_kind") != "canonical"
            or canonical_identity.get("project_repository", "").casefold()
            != activation["repository"].casefold()):
        raise QualificationError("canonical store identity must exist before a disposable candidate run")

    target_root = _create_disposable_target(args.target_root, authoritative["canonical"])
    try:
        def initialize_store_identity(identity_path: str, lock_path: str, value: dict) -> None:
            with _exclusive_lock(lock_path):
                _atomic_json(identity_path, value, create_only=True)

        context = authority.resolve_execution_context(
            authoritative, accepted_tree=accepted_tree, script=candidate["script"],
            target_kind="disposable", target_root=target_root, operation_id=args.operation,
            provider=args.provider, run_id=args.run_id, task_id=args.task_id,
            extensions={
                "candidate_code": {
                    key: value for key, value in candidate.items() if key != "script_path"
                },
                "candidate_authorized_registry_ids": authorized_registry_ids,
                "future_authorization": None,
            },
            identity_initializer=initialize_store_identity,
        )
        helper = os.path.join(accepted_tree, ".engine", "tools", "memory", "candidate_invocation.py")
        if not os.path.isfile(helper):
            raise QualificationError("accepted candidate invocation helper is absent")
        env = {
            key: value for key, value in os.environ.items()
            if key not in _PYTHON_ENV_PREFIXES and key not in {
                "ENGINE_MEMORY_DIR", "ENGINE_PROJECT_ROOT", "ENGINE_PERSISTENT_EXECUTION_CONTEXT",
                "ENGINE_CANDIDATE_DISPOSABLE",
            }
        }
        provider_authority = _provider_authority(accepted_tree)
        env.update({
            "PYTHONNOUSERSITE": "1", provider_authority.PROVIDER_ENV: args.provider,
            "PYTHONDONTWRITEBYTECODE": "1",  # belt for any non-isolated descendant; -B below covers the child
            "ENGINE_PERSISTENT_EXECUTION_CONTEXT": context.to_json(),
        })
        argv = [
            sys.executable, "-I", "-S", "-B", helper, "--candidate-root", candidate["project_root"],
            "--script", candidate["script_path"], "--target-root", target_root,
        ]
        for site_path in args.site_path:
            if os.path.isdir(site_path):
                argv.extend(["--site-path", os.path.realpath(site_path)])
        argv.append("--")
        argv.extend(args.target_args)

        canonical_memory = authoritative["canonical"]["memory_dir"]
        canonical_lock = os.path.join(canonical_memory, authority.STORE_IDENTITY_LOCK_FILENAME)
        timed_out = False
        with _existing_store_lock(canonical_lock):
            before = _safe_inventory(canonical_memory, details=False)
            try:
                authority.revalidate_context(context)
            except authority.ContextError as exc:
                raise QualificationError(f"candidate context changed before execution: {exc}") from exc
            try:
                result = subprocess.run(argv, capture_output=True, timeout=args.timeout, env=env)
            except subprocess.TimeoutExpired as exc:
                timed_out = True
                result = subprocess.CompletedProcess(
                    argv, 124, stdout=exc.stdout or b"", stderr=exc.stderr or b"",
                )
            after = _safe_inventory(canonical_memory, details=False)
        receipt = _candidate_receipt(
            activation=activation, accepted_tree=accepted_tree, candidate=candidate, context=context,
            target_root=target_root, result=result, before=before, after=after, timed_out=timed_out,
        )
        print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
        if not receipt["canonical"]["unchanged"]:
            print("candidate run refused: canonical memory changed during disposable execution", file=sys.stderr)
            return 1
        if not receipt["candidate"]["code_unchanged_during_run"]:
            print("candidate run refused: candidate code changed during execution", file=sys.stderr)
            return 1
        return result.returncode
    except BaseException:
        # Remove only a still-empty directory that this accepted invocation just created.  Any private evidence
        # written before a later refusal remains available for inspection and is never recursively deleted.
        try:
            os.rmdir(target_root)
        except OSError:
            pass
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    act = sub.add_parser(
        "activate",
        description="Advance accepted automatic-hook code after GitHub independently proves the exact commit.",
        epilog=("Example: accepted_hook_dispatch.py activate --root . --repository OWNER/REPO "
                "--commit FULL_SHA --source reviewed-merge --source-ref refs/heads/main "
                "--engine-release VERSION --expected-epoch 0"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    act.add_argument("--root", required=True)
    act.add_argument("--repository", required=True)
    act.add_argument("--commit", required=True)
    act.add_argument("--source", required=True, choices=sorted(_SOURCE_KINDS))
    act.add_argument("--source-ref", required=True)
    act.add_argument("--engine-release", required=True)
    act.add_argument("--expected-epoch", required=True, type=int)
    ensure = sub.add_parser(
        "ensure",
        help="attend activation of the canonical GitHub default-branch HEAD, or keep the exact current activation",
    )
    ensure.add_argument("--root", required=True)
    ensure.add_argument(
        "--ambient", action="store_true",
        help="session-start form: bounded, never raises, and reports what it did as notices",
    )
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--root", required=True)
    run = sub.add_parser("run")
    run.add_argument("--root", required=True)
    run.add_argument("--script", required=True)
    run.add_argument("target_args", nargs=argparse.REMAINDER)
    attended = sub.add_parser(
        "attended",
        help="run one registered maintenance operation from the exact accepted Engine tree",
    )
    attended.add_argument("--root", required=True)
    attended.add_argument("--script", required=True)
    attended.add_argument("--operation", required=True)
    attended.add_argument("target_args", nargs=argparse.REMAINDER)
    candidate = sub.add_parser("candidate")
    candidate.add_argument("--root", required=True)
    candidate.add_argument("--candidate-root", required=True)
    candidate.add_argument("--script", required=True)
    candidate.add_argument("--target-root", required=True)
    candidate.add_argument("--operation", required=True)
    candidate.add_argument("--provider", required=True, choices=("claude", "codex"))
    candidate.add_argument("--run-id")
    candidate.add_argument("--task-id")
    candidate.add_argument("--timeout", type=int, default=120, choices=range(1, 601))
    candidate.add_argument("target_args", nargs=argparse.REMAINDER)
    internal = sub.add_parser("_run-accepted")
    internal.add_argument("--tree", required=True)
    internal.add_argument("--script", required=True)
    internal.add_argument("--site-path", action="append", default=[])
    internal.add_argument("target_args", nargs=argparse.REMAINDER)
    internal_attended = sub.add_parser("_run-attended")
    internal_attended.add_argument("--tree", required=True)
    internal_attended.add_argument("--script", required=True)
    internal_attended.add_argument("--operation", required=True)
    internal_attended.add_argument("--site-path", action="append", default=[])
    internal_attended.add_argument("target_args", nargs=argparse.REMAINDER)
    internal_candidate = sub.add_parser("_run-candidate")
    internal_candidate.add_argument("--tree", required=True)
    internal_candidate.add_argument("--root", required=True)
    internal_candidate.add_argument("--candidate-root", required=True)
    internal_candidate.add_argument("--script", required=True)
    internal_candidate.add_argument("--target-root", required=True)
    internal_candidate.add_argument("--operation", required=True)
    internal_candidate.add_argument("--provider", required=True)
    internal_candidate.add_argument("--run-id")
    internal_candidate.add_argument("--task-id")
    internal_candidate.add_argument("--timeout", type=int, default=120, choices=range(1, 601))
    internal_candidate.add_argument("--site-path", action="append", default=[])
    internal_candidate.add_argument("target_args", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if hasattr(args, "target_args") and args.target_args[:1] == ["--"]:
        args.target_args = args.target_args[1:]
    # Bytecode-hygiene belt, scoped to the INNER accepted-dispatch process. The child launched for an
    # inner run already carries -B (see dispatch/dispatch_attended/dispatch_candidate), so this only bites
    # in the rollout window where an OLDER outer (without -B) launches this NEWER inner: it stops the inner
    # writing __pycache__ into the digest-attested accepted tree, whose content digest _tree_inventory
    # certifies. It is set HERE, not at module import, so importing this module as a LIBRARY — as boot.py
    # does at session start to reach ensure_activation_ambient — never disables bytecode caching for the
    # importer's own later imports (the session-start hot path this change exists to keep reliable).
    if args.command in ("_run-accepted", "_run-attended", "_run-candidate"):
        sys.dont_write_bytecode = True
    try:
        if args.command == "activate":
            print(json.dumps(activate(args), sort_keys=True))
            return 0
        if args.command == "inspect":
            root = _top(args.root)
            activation = load_activation(root)
            _verify_exact_object(root, activation)
            accepted_tree = _valid_materialization(root, activation)
            if not accepted_tree:
                raise QualificationError("the accepted tree is not already materialized and intact")
            print(json.dumps(_canonical_context(root, activation, accepted_tree), sort_keys=True))
            return 0
        if args.command == "ensure":
            if args.ambient:
                record, notices = ensure_activation_ambient(args.root)
                print(json.dumps({"activation": record, "notices": notices,
                                  "coverage": uncovered_worktrees(_top(args.root))}, sort_keys=True))
                return 0
            print(json.dumps(ensure_activation(args.root), sort_keys=True))
            return 0
        if args.command == "run":
            dispatch(args.root, args.script, args.target_args)
            return 1  # os.execve never returns
        if args.command == "attended":
            dispatch_attended(args.root, args.script, args.operation, args.target_args)
            return 1  # os.execve never returns
        if args.command == "candidate":
            dispatch_candidate(args)
            return 1  # os.execve never returns
        if args.command == "_run-accepted":
            return run_accepted(args)
        if args.command == "_run-attended":
            return run_attended(args)
        if args.command == "_run-candidate":
            return run_candidate(args)
        raise QualificationError("unknown accepted-hook operation")
    except WriteHeld as exc:
        # The write-dispatch launcher held the write because the activated commit left the default branch.
        # Emit the posture sentence as the child's response line; write_dispatch's parent folds it into a
        # DispatchRefused and the memory server forwards it to the operator byte-for-byte. No child ran.
        print(json.dumps({"event": "response", "response": {"refused": str(exc)}},
                         sort_keys=True, separators=(",", ":")))
        return 0
    except QualificationError as exc:
        if args.command == "run":
            print(f"Engine memory mutation skipped: {exc}. This did not block the host action.", file=sys.stderr)
        elif args.command in {"activate", "ensure"}:
            print(f"Accepted-hook activation refused: {exc}.", file=sys.stderr)
        else:
            print(f"Accepted-hook {args.command} failed: {exc}.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
