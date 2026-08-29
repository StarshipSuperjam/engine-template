#!/usr/bin/env python3
"""Resolve and run automatic memory hooks from one attended, exact accepted Engine tree.

The file is deliberately a small bootstrap.  It runs from the active checkout only long enough to read the
common activation record, prove its exact Git object and canonical state binding, and start a fresh isolated
interpreter from the activated materialization.  No memory module is imported on that bootstrap side.

Trust boundary: this closes accidental candidate/stale-code execution against canonical durable memory.  It is
operational provenance, not protection from malicious code running as the same user: such code can rewrite the
launcher, Git common metadata, or the cache.  Stronger same-user isolation belongs to the future mediator work.

Automatic callers may use only ``run`` or ``inspect``.  ``activate`` is an attended compare-and-set operation:
it accepts a full commit already reachable from a reviewed default-branch ref, or exactly named by a published
release tag, and is the sole command that can advance the activation epoch.
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


SCHEMA_VERSION = "accepted-hook-activation.v1"
CONTEXT_VERSION = "accepted-hook-context.v1"
ACTIVATION_REL = os.path.join("engine", "accepted-hooks", "activation.json")
CACHE_REL = os.path.join("engine", "accepted-hooks", "trees")
LOCK_REL = os.path.join("engine", "accepted-hooks", "activation.lock")
DISPATCH_MARKER = "ENGINE_ACCEPTED_HOOK_DISPATCH=1"
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
_PYTHON_ENV_PREFIXES = ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE", "PYTHONSTARTUP")


class QualificationError(RuntimeError):
    """A typed no-mutation outcome.  The CLI always maps it to exit 1, never the host's block code 2."""


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
        "source", "source_ref", "activated_at",
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
    if marker.get("inventory") != _tree_inventory(tree_path):
        return None
    dispatch = os.path.join(tree_path, ".engine", "tools", "accepted_hook_dispatch.py")
    return tree_path if os.path.isfile(dispatch) else None


def _materialize(root: str, activation: dict) -> str:
    """Materialize through release_source's existing exact local-archive seam, then atomically publish."""
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
            tools_dir = os.path.join(root, ".engine", "tools")
            sys.path.insert(0, tools_dir)
            try:
                import release_source
                release_source._archive_tree(activation["commit"], stage, root=root)
            finally:
                try:
                    sys.path.remove(tools_dir)
                except ValueError:
                    pass
                sys.modules.pop("release_source", None)
                sys.modules.pop("validate", None)
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
    if accepted_tree and pointer_digest != accepted_pointer_digest:
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


def _registered_worktrees(root: str) -> list[str]:
    text = _git(root, "worktree", "list", "--porcelain")
    paths = []
    for line in text.splitlines():
        if line.startswith("worktree "):
            paths.append(os.path.realpath(line[len("worktree "):].strip()))
    if not paths:
        raise QualificationError("registered Git worktrees could not be enumerated")
    return paths


def _verify_activation_barrier(root: str) -> None:
    legacy = []
    for worktree in _registered_worktrees(root):
        runner = os.path.join(worktree, ".engine", "tools", "hook-runner.sh")
        try:
            with open(runner, encoding="utf-8") as handle:
                qualified = DISPATCH_MARKER in handle.read()
        except OSError:
            qualified = False
        if not qualified:
            legacy.append(worktree)
    if legacy:
        shown = ", ".join(legacy[:3])
        more = f" (+{len(legacy) - 3} more)" if len(legacy) > 3 else ""
        raise QualificationError(
            "activation refused: retire or recreate pre-fix/unreadable worktrees before advancing the "
            f"accepted epoch: {shown}{more}"
        )


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
    value = _engine_manifest_at(root, commit)
    version = value.get("engine_version")
    if not isinstance(version, str) or not version:
        raise QualificationError("the accepted commit's Engine release is missing")
    return version


def activate(args: argparse.Namespace) -> dict:
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
        manifest = _engine_manifest_at(root, commit)
        default_branch = manifest.get("default_branch")
        allowed_refs = {
            f"refs/heads/{default_branch}", f"refs/remotes/origin/{default_branch}",
        } if isinstance(default_branch, str) and default_branch else set()
        if not (args.source_ref.startswith("refs/heads/") or args.source_ref.startswith("refs/remotes/origin/")):
            raise QualificationError("a reviewed merge must be qualified through an explicit default-branch ref")
        source_ref = args.source_ref
        if source_ref not in allowed_refs:
            raise QualificationError("the reviewed merge ref is not the accepted commit's recorded default branch")
        proc = subprocess.run(
            ["git", "-C", root, "merge-base", "--is-ancestor", commit, source_ref],
            capture_output=True, timeout=30,
        )
        if proc.returncode != 0:
            raise QualificationError("the accepted commit is not reachable from the reviewed branch")
    actual_release = _engine_release_at(root, commit)
    if actual_release != args.engine_release:
        raise QualificationError("the declared Engine release differs from the accepted commit's manifest")
    _verify_activation_barrier(root)
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
            "activated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        _materialize(root, record)
        _atomic_json(activation_path, record)
    return record


def _relative_script(root: str, script: str) -> str:
    absolute = os.path.realpath(script if os.path.isabs(script) else os.path.join(root, script))
    rel = os.path.relpath(absolute, root).replace(os.sep, "/")
    if rel not in AUTOMATIC_MUTATORS or absolute != os.path.realpath(os.path.join(root, rel)):
        raise QualificationError("the dispatcher was asked to run an unregistered automatic mutator")
    return rel


def _site_paths() -> list[str]:
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
    context["invocation"] = {
        "script": rel,
        "provider": os.environ.get("ENGINE_PROVIDER", "claude"),
        "run_id": os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CODEX_THREAD_ID"),
    }
    env = {
        key: value for key, value in os.environ.items()
        if key not in _PYTHON_ENV_PREFIXES and key not in {"ENGINE_MEMORY_DIR", "ENGINE_PROJECT_ROOT"}
    }
    canonical = context["canonical"]
    env.update({
        "PYTHONNOUSERSITE": "1",
        "ENGINE_PROVIDER": context["invocation"]["provider"],
        "ENGINE_PROJECT_ROOT": canonical["project_root"],
        "ENGINE_MEMORY_DIR": canonical["memory_dir"],
        "ENGINE_BOOT_CACHE_DIR": os.path.join(canonical["project_root"], ".engine", "telemetry", ".cache"),
        "ENGINE_ACCEPTED_HOOK_CONTEXT": json.dumps(context, sort_keys=True, separators=(",", ":")),
    })
    accepted_dispatch = os.path.join(accepted_tree, ".engine", "tools", "accepted_hook_dispatch.py")
    argv = [
        sys.executable, "-I", "-S", accepted_dispatch, "_run-accepted",
        "--tree", accepted_tree, "--script", rel,
    ]
    for site_path in _site_paths():
        argv.extend(["--site-path", site_path])
    argv.append("--")
    argv.extend(target_args)
    os.chdir(canonical["project_root"])
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


def run_accepted(args: argparse.Namespace) -> int:
    accepted_tree = os.path.realpath(args.tree)
    tools_root = os.path.join(accepted_tree, ".engine", "tools")
    rel = args.script
    if rel not in AUTOMATIC_MUTATORS:
        raise QualificationError("accepted interpreter received an unregistered automatic mutator")
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
        persistent_context = context_authority.install_automatic_context(
            authoritative, accepted_tree=accepted_tree, script=rel,
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    act = sub.add_parser("activate")
    act.add_argument("--root", required=True)
    act.add_argument("--repository", required=True)
    act.add_argument("--commit", required=True)
    act.add_argument("--source", required=True, choices=sorted(_SOURCE_KINDS))
    act.add_argument("--source-ref", required=True)
    act.add_argument("--engine-release", required=True)
    act.add_argument("--expected-epoch", required=True, type=int)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--root", required=True)
    run = sub.add_parser("run")
    run.add_argument("--root", required=True)
    run.add_argument("--script", required=True)
    run.add_argument("target_args", nargs=argparse.REMAINDER)
    internal = sub.add_parser("_run-accepted")
    internal.add_argument("--tree", required=True)
    internal.add_argument("--script", required=True)
    internal.add_argument("--site-path", action="append", default=[])
    internal.add_argument("target_args", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if hasattr(args, "target_args") and args.target_args[:1] == ["--"]:
        args.target_args = args.target_args[1:]
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
        if args.command == "run":
            dispatch(args.root, args.script, args.target_args)
            return 1  # os.execve never returns
        if args.command == "_run-accepted":
            return run_accepted(args)
        raise QualificationError("unknown accepted-hook operation")
    except QualificationError as exc:
        print(f"Engine memory mutation skipped: {exc}. This did not block the host action.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
