#!/usr/bin/env python3
"""Locked, context-bound authority for every persistent memory writer.

Modules call :func:`install_module_guards` after defining their public and private write seams.  The guard
serializes the outer operation on the stable store-identity lock, revalidates the immutable execution context
after that lock is held, and consumes one exact registry capability immediately before the function begins.
Nested helpers reuse the held store lock but consume independent subgrants, so composite capture, backup,
compaction, and restore operations remain one coherent critical section without laundering one broad token
through several distinct effects.

Production calls without an accepted execution context fail closed. Existing hermetic unit tests retain a
strictly source-bound adapter: an active frame must come from a checked-in ``test_*.py`` under this exact
Engine tools tree. Merely importing a common library never changes mutation authority. Dedicated authority
tests exercise the real context/capability path in a subprocess with no checked-in test frame.
"""
from __future__ import annotations

import functools
import hashlib
import inspect
import io
import json
import os
import stat
import subprocess
import sys
import threading
import weakref
from contextvars import ContextVar
from contextlib import contextmanager

try:  # package import in production
    from . import execution_context, mutation_contract
except ImportError:  # direct memory-module CLI import
    import execution_context  # type: ignore
    import mutation_contract  # type: ignore

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX is the current Engine floor
    fcntl = None


class MutationAuthorityError(RuntimeError):
    """A persistent call lacked exact context, lock, target, or capability authority."""


_THREAD = threading.local()
_TEST_SCOPE = ContextVar("engine_mutation_test_scope", default=None)
_TEST_HOOK_LOCK = threading.RLock()
_TEST_AFTER_LOCK_HOOK = None
_STORE_TARGETS = frozenset({
    "ledger", "ledger-metadata", "derived-index", "semantic-index", "capture-cursor",
    "restore-journal", "ephemeral-staging",
})
_PROJECT_TARGETS = frozenset({
    "backup-pointer", "erasure-proposal", "project-repository", "degraded-health", "tracked-finding",
    "lifecycle-marker",
})
_PATH_ARGUMENTS = frozenset({
    "path", "for_path", "ledger_file", "index_file", "store_file", "root", "memory_dir",
    "data_dir", "target", "destination",
})
_SKIP_WRAPPERS = frozenset({"capture-lock-create"})
_TOOLS_ROOT = os.path.realpath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_ENGINE_ROOT = os.path.realpath(os.path.dirname(os.path.dirname(_TOOLS_ROOT)))
_INSTANTIATOR_SOURCE = os.path.join(_TOOLS_ROOT, "instantiator.py")
_PREACTIVATION_LOCK = threading.RLock()
_PREACTIVATION_ISSUER = object()
_PREACTIVATION_GRANTS = weakref.WeakKeyDictionary()
_TRACKED_SOURCE_LOCK = threading.RLock()
_TRACKED_SOURCE_CACHE = {}
_TEST_TARGET_LOCK = threading.RLock()
_TEST_FORBIDDEN_ROOTS = None


def _digest(value) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath((path, root)) == root
    except ValueError:
        return False


def _entry(entry_id: str) -> dict:
    try:
        return dict(mutation_contract.entry_by_id(entry_id))
    except Exception as exc:
        raise MutationAuthorityError(str(exc)) from exc


def _measured_cardinality(entry: dict, args: tuple, kwargs: dict) -> int:
    explicit = kwargs.pop("_engine_measured_cardinality", None)
    if explicit is not None:
        if not isinstance(explicit, int) or isinstance(explicit, bool) or explicit < 0:
            raise MutationAuthorityError("measured cardinality must be a non-negative integer")
        return explicit
    maximum = entry["declared_cardinality"]["maximum"]
    if maximum == 1:
        return 1
    for key in ("records", "rows", "files", "targets", "new_records", "passages", "tree"):
        value = kwargs.get(key)
        if isinstance(value, (list, tuple, set, dict)):
            return len(value)
    for value in args:
        if isinstance(value, (list, tuple, set)):
            return len(value)
    # A call is one operation even when its registered unit contains several journaled records.  The exact
    # low-level record/file helpers nested beneath it carry their own measured subgrants.
    return 1


def _call_arguments(function, args: tuple, kwargs: dict) -> dict:
    if function is None:
        return dict(kwargs)
    try:
        return dict(inspect.signature(function).bind_partial(*args, **kwargs).arguments)
    except (TypeError, ValueError):
        return dict(kwargs)


def _validate_explicit_targets(context, entry: dict, args: tuple, kwargs: dict, function=None) -> None:
    document = context.to_document()
    memory_root = os.path.realpath(document["target"]["memory_dir"])
    project_root = os.path.realpath(document["project"]["root"])
    target_kind = entry["target_kind"]
    if target_kind in _STORE_TARGETS:
        roots = (memory_root,)
    elif target_kind in _PROJECT_TARGETS:
        # A disposable context may observe canonical recovery identity, but it never authorizes a project-level
        # write there.  Its pointer, proposal, health, and staging surfaces all live beneath the private target.
        roots = (os.path.dirname(memory_root),) if document["target"]["kind"] == "disposable" else (project_root,)
    else:
        roots = ()
    if not roots:
        return
    arguments = _call_arguments(function, args, kwargs)
    for key in _PATH_ARGUMENTS:
        value = arguments.get(key)
        if value is None or not isinstance(value, (str, os.PathLike)):
            continue
        raw = os.fspath(value)
        if not os.path.isabs(raw):
            raise MutationAuthorityError(f"persistent writer {entry['writer']} received a relative {key}")
        resolved = os.path.realpath(raw)
        if not any(_within(resolved, root) for root in roots):
            raise MutationAuthorityError(
                f"persistent writer {entry['writer']} target {key} escapes its qualified context")


def _test_forbidden_roots() -> tuple[str, ...]:
    """Repository and shared-state roots a candidate test adapter must never mutate."""
    global _TEST_FORBIDDEN_ROOTS
    with _TEST_TARGET_LOCK:
        if _TEST_FORBIDDEN_ROOTS is not None:
            return _TEST_FORBIDDEN_ROOTS
        roots = {_ENGINE_ROOT}
        try:
            result = subprocess.run(
                ["git", "-C", _ENGINE_ROOT, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                capture_output=True, text=True, check=False, timeout=15,
            )
            if result.returncode == 0 and result.stdout.strip():
                common = os.path.realpath(result.stdout.strip())
                roots.add(common)
                if os.path.basename(common) == ".git":
                    main = os.path.realpath(os.path.dirname(common))
                    roots.update({main, os.path.join(main, ".engine", "memory")})
        except (OSError, subprocess.SubprocessError, ValueError):
            # The current checkout still remains forbidden. Failure to resolve shared Git state must never
            # make that checkout writable through the adapter.
            pass
        _TEST_FORBIDDEN_ROOTS = tuple(sorted(roots))
        return _TEST_FORBIDDEN_ROOTS


def _looks_like_path(key: str) -> bool:
    return (key in _PATH_ARGUMENTS or key in {"main", "project_root", "common_dir"}
            or key.endswith(("_path", "_file", "_dir", "_root", "_target", "_destination")))


def _validate_test_targets(entry: dict, args: tuple, kwargs: dict, function=None) -> None:
    """Keep the checked-in test adapter outside the checkout and canonical shared state.

    Candidate tests must exercise newly changed writers, so their source cannot equal the already activated
    commit. Source identity therefore narrows who can enter this harness seam; target confinement is what keeps
    that seam from becoming candidate authority over durable project data.
    """
    arguments = _call_arguments(function, args, kwargs)
    candidates = []
    for key, value in arguments.items():
        if _looks_like_path(key) and isinstance(value, (str, os.PathLike)):
            raw = os.path.expanduser(os.fspath(value))
            candidates.append(os.path.realpath(raw if os.path.isabs(raw) else os.path.join(os.getcwd(), raw)))
    if entry["target_kind"] in _STORE_TARGETS:
        memory_dir = os.environ.get("ENGINE_MEMORY_DIR")
        if memory_dir:
            candidates.append(os.path.realpath(os.path.abspath(os.path.expanduser(memory_dir))))
        elif not candidates:
            # A pathless persistent writer resolves through the repository's shared memory root. Treat the
            # current checkout as the effective target so the adapter fails closed instead of guessing.
            candidates.append(_ENGINE_ROOT)
    elif entry["target_kind"] in _PROJECT_TARGETS and not candidates:
        candidates.append(os.path.realpath(os.getcwd()))
    forbidden = _test_forbidden_roots()
    for candidate in candidates:
        if any(_within(candidate, root) or _within(root, candidate) for root in forbidden):
            raise MutationAuthorityError(
                f"test adapter for {entry['writer']} cannot target the checkout or canonical shared state")


def _code_tree(value) -> set:
    """Return exact code objects rooted at one loaded function, including nested callbacks."""
    code = getattr(value, "__code__", None)
    if code is None:
        return set()
    found = {code}
    pending = [code]
    while pending:
        current = pending.pop()
        for item in current.co_consts:
            if isinstance(item, type(current)) and item not in found:
                found.add(item)
                pending.append(item)
    return found


def _compiled_code_tree(code) -> set:
    """Return every code object compiled from one exact on-disk source snapshot."""
    found = {code}
    pending = [code]
    while pending:
        current = pending.pop()
        for item in current.co_consts:
            if isinstance(item, type(current)) and item not in found:
                found.add(item)
                pending.append(item)
    return found


def _code_signature(code):
    """Stable executable structure; unlike code hashing/marshal output this survives interpreter quickening."""
    constants = tuple(_code_signature(value) if isinstance(value, type(code)) else value
                      for value in code.co_consts)
    return (
        code.co_name, getattr(code, "co_qualname", code.co_name), code.co_firstlineno,
        code.co_argcount, getattr(code, "co_posonlyargcount", 0), code.co_kwonlyargcount, code.co_nlocals,
        code.co_stacksize, code.co_flags, code.co_code, constants, code.co_names, code.co_varnames,
        code.co_freevars, code.co_cellvars, getattr(code, "co_linetable", b""),
        getattr(code, "co_exceptiontable", b""),
    )


def _same_compiled_code(left, right) -> bool:
    return _code_signature(left) == _code_signature(right)


def _module_code_objects(module) -> set:
    found = set()
    for value in vars(module).values():
        if inspect.isfunction(value):
            found.update(_code_tree(value))
        elif inspect.isclass(value):
            for member in vars(value).values():
                if isinstance(member, (staticmethod, classmethod)):
                    member = member.__func__
                if inspect.isfunction(member):
                    found.update(_code_tree(member))
    return found


def _tracked_head_source(path: str, payload: bytes) -> bool:
    """Whether one source snapshot is the exact tracked blob at this checkout's committed HEAD."""
    cache_key = (path, hashlib.sha256(payload).digest())
    with _TRACKED_SOURCE_LOCK:
        cached = _TRACKED_SOURCE_CACHE.get(cache_key)
    if cached is not None:
        return cached
    try:
        rel = os.path.relpath(path, _ENGINE_ROOT)
        if rel == ".." or rel.startswith(".." + os.sep) or os.path.isabs(rel):
            return False
        result = subprocess.run(
            ["git", "-C", _ENGINE_ROOT, "show", f"HEAD:{rel.replace(os.sep, '/') }"],
            capture_output=True, check=False, timeout=15,
        )
        matched = result.returncode == 0 and result.stdout == payload
    except (OSError, subprocess.SubprocessError, ValueError):
        matched = False
    with _TRACKED_SOURCE_LOCK:
        _TRACKED_SOURCE_CACHE[cache_key] = matched
    return matched


def _source_bound_frame(frame, *, test_only: bool = False, module_name: str | None = None,
                        function_name: str | None = None) -> bool:
    """Trust exact loaded code only when its regular-file source is also the tracked HEAD blob."""
    claimed_module = frame.f_globals.get("__name__")
    if not isinstance(claimed_module, str):
        return False
    module = sys.modules.get(claimed_module)
    if module is None or frame.f_globals is not vars(module):
        return False
    path = getattr(module, "__file__", None)
    if not isinstance(path, str):
        return False
    real = os.path.realpath(path)
    try:
        before = os.lstat(real)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode) or before.st_size > 4 * 1024 * 1024:
            return False
        # Use the concrete I/O module rather than ``builtins.open``: callers legitimately patch the latter to
        # test their own filesystem failure handling, and that must not disable the source-verification gate.
        with io.open(real, "rb") as handle:
            payload = handle.read()
        source = payload.decode("utf-8")
        after = os.lstat(real)
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            return False
        # The source decides its own future flags. Inheriting this module's ``annotations`` future would make
        # an otherwise identical Python file compile to different code and silently deny legitimate callers.
        compiled = _compiled_code_tree(compile(source, real, "exec", dont_inherit=True))
    except (OSError, UnicodeError, SyntaxError):
        return False
    if os.path.realpath(frame.f_code.co_filename) != real:
        return False
    if not any(_same_compiled_code(frame.f_code, candidate) for candidate in compiled):
        return False
    if module_name is not None:
        if module_name != "instantiator" or real != _INSTANTIATOR_SOURCE:
            return False
        if not _tracked_head_source(real, payload):
            return False
    if function_name is not None:
        return any(frame.f_code is candidate for candidate in _code_tree(getattr(module, function_name, None)))
    if test_only:
        tools_root = _TOOLS_ROOT + os.sep
        name = os.path.basename(real)
        if not (real.startswith(tools_root) and name.startswith("test_") and name.endswith(".py")):
            return False
        if not _tracked_head_source(real, payload):
            return False
    return any(frame.f_code is candidate for candidate in _module_code_objects(module))


def _test_adapter_allowed() -> bool:
    frame = inspect.currentframe()
    try:
        frame = frame.f_back if frame is not None else None
        while frame is not None:
            if _source_bound_frame(frame, test_only=True):
                return True
            frame = frame.f_back
    finally:
        del frame
    return False


class _PreActivationCapability:
    """Opaque one-use handle for the approved setup-era presentation-marker exception."""

    __slots__ = ("__weakref__",)

    def __init__(self, issuer=None):
        if issuer is not _PREACTIVATION_ISSUER:
            raise MutationAuthorityError("pre-activation local capabilities are issuer-created only")


def acquire_preactivation_local_capability(entry_id: str, *, project_root: str):
    """Preflight one exact operator-approved capability before first-run retirement changes anything."""
    frame = inspect.currentframe()
    try:
        frame = frame.f_back if frame is not None else None
        allowed = False
        while frame is not None:
            if _source_bound_frame(frame, module_name="instantiator", function_name="retire"):
                allowed = True
                break
            frame = frame.f_back
        if not allowed:
            raise MutationAuthorityError("pre-activation local authority is available only to instantiator.retire")
    finally:
        del frame
    if entry_id != "attended-first-run-marker-stage":
        raise MutationAuthorityError("pre-activation local authority names an unsupported writer")
    root = os.path.realpath(project_root)
    # macOS commonly presents temporary directories through the absolute ``/var`` -> ``/private/var``
    # symlink. Bind the capability to the canonical target, but accept that ordinary absolute spelling:
    # the guarded writer compares its own target through ``realpath`` below.
    if not os.path.isabs(project_root) or not os.path.isdir(root):
        raise MutationAuthorityError("pre-activation local authority requires one normalized project root")
    capability = _PreActivationCapability(_PREACTIVATION_ISSUER)
    with _PREACTIVATION_LOCK:
        _PREACTIVATION_GRANTS[capability] = {"entry_id": entry_id, "project_root": root}
    return capability


def _preactivation_receipt(entry: dict, measured: int, grant: dict) -> dict:
    try:
        mutation_contract.classify(
            writer=entry["writer"], target_kind=entry["target_kind"], effect_class=entry["effect_class"],
            invocation_mode="attended", measured_cardinality=measured,
            schema_cutover=entry["schema_cutover"],
        )
    except Exception as exc:
        raise MutationAuthorityError(str(exc)) from exc
    receipt = {
        "exception": "operator-approved-first-run-presentation-marker",
        "registry_id": entry["id"], "writer": entry["writer"], "mode": "attended",
        "measured_cardinality": measured, "project_root": grant["project_root"],
        "one_use": True,
    }
    receipt["receipt_digest"] = _digest(receipt)
    return receipt


def _test_receipt(entry: dict, mode: str, measured: int) -> dict:
    try:
        mutation_contract.classify(
            writer=entry["writer"], target_kind=entry["target_kind"], effect_class=entry["effect_class"],
            invocation_mode=mode, measured_cardinality=measured, schema_cutover=entry["schema_cutover"],
        )
    except Exception as exc:
        raise MutationAuthorityError(str(exc)) from exc
    receipt = {
        "test_only": True, "registry_id": entry["id"], "writer": entry["writer"],
        "mode": mode, "measured_cardinality": measured,
    }
    receipt["receipt_digest"] = _digest(receipt)
    return receipt


def _consume(context, entry: dict, measured: int, supplied=None):
    try:
        capability = supplied or execution_context.mint_capability(
            context, registry_id=entry["id"], measured_cardinality=measured)
        return execution_context.consume_capability(
            capability, context=context, writer=entry["writer"], target_kind=entry["target_kind"],
            effect_class=entry["effect_class"],
            invocation_mode=context["operation"]["invocation_mode"], measured_cardinality=measured,
            schema_cutover=entry["schema_cutover"],
            observed_state_fingerprint=context.expected_state_fingerprint,
        )
    except execution_context.ContextError as exc:
        raise MutationAuthorityError(str(exc)) from exc


def _open_store_lock(context):
    if fcntl is None:
        raise MutationAuthorityError("persistent mutation authority requires POSIX advisory locking")
    lock_path = context["target"]["lifecycle"]["store_identity_lock"]
    try:
        info = os.lstat(lock_path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise MutationAuthorityError("persistent store authority lock is not a regular file")
        handle = open(lock_path, "r", encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle
    except MutationAuthorityError:
        raise
    except OSError as exc:
        raise MutationAuthorityError("persistent store authority lock is unavailable") from exc


def _close_store_lock(handle) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _run_after_lock_test_hook() -> None:
    with _TEST_HOOK_LOCK:
        hook = _TEST_AFTER_LOCK_HOOK
    if hook is not None:
        if not _test_adapter_allowed():
            raise MutationAuthorityError("under-lock test hook escaped a unit-test process")
        hook()


def set_after_lock_test_hook(hook) -> None:
    """Install a one-process failure-injection hook; unavailable outside unit tests."""
    if not _test_adapter_allowed():
        raise MutationAuthorityError("under-lock test hooks are test-only")
    if hook is not None and not callable(hook):
        raise MutationAuthorityError("under-lock test hook must be callable or None")
    global _TEST_AFTER_LOCK_HOOK
    with _TEST_HOOK_LOCK:
        _TEST_AFTER_LOCK_HOOK = hook


@contextmanager
def test_scope(mode: str = "attended"):
    """Explicitly carry the source-bound test adapter across async task or callback boundaries."""
    if not _test_adapter_allowed():
        raise MutationAuthorityError("test mutation scope is available only from a checked-in test module")
    if mode not in {"automatic", "attended"}:
        raise MutationAuthorityError("test mutation scope mode is invalid")
    token = _TEST_SCOPE.set(mode)
    try:
        yield
    finally:
        _TEST_SCOPE.reset(token)


@contextmanager
def mutation_scope(entry_id: str, args: tuple, kwargs: dict, *, supplied_capability=None, function=None):
    """Hold one coherent outer lock and consume this exact writer's one-shot subgrant."""
    entry = _entry(entry_id)
    measured = _measured_cardinality(entry, args, kwargs)
    state = getattr(_THREAD, "state", None)
    if state is not None:
        if state.get("test_only"):
            _validate_test_targets(entry, args, kwargs, function)
            yield _test_receipt(entry, state["mode"], measured)
            return
        context = state["context"]
        _validate_explicit_targets(context, entry, args, kwargs, function)
        yield _consume(context, entry, measured, supplied_capability)
        return

    if isinstance(supplied_capability, _PreActivationCapability):
        bootstrap = supplied_capability
        arguments = _call_arguments(function, args, kwargs)
        target = arguments.get("main")
        with _PREACTIVATION_LOCK:
            grant = _PREACTIVATION_GRANTS.pop(bootstrap, None)
        if grant is None or entry_id != grant["entry_id"]:
            raise MutationAuthorityError("pre-activation local capability is wrong or already consumed")
        if (not isinstance(target, (str, os.PathLike))
                or os.path.realpath(os.fspath(target)) != grant["project_root"]):
            raise MutationAuthorityError("pre-activation local capability target mismatch")
        yield _preactivation_receipt(entry, measured, grant)
        return

    scoped_test_mode = _TEST_SCOPE.get()
    if scoped_test_mode is not None:
        _validate_test_targets(entry, args, kwargs, function)
        _THREAD.state = {"test_only": True, "mode": scoped_test_mode}
        try:
            yield _test_receipt(entry, scoped_test_mode, measured)
        finally:
            _THREAD.state = None
        return

    try:
        context = execution_context.current_context()
    except execution_context.ContextError as exc:
        if not _test_adapter_allowed():
            raise MutationAuthorityError(
                f"persistent writer {entry['writer']} has no accepted execution context") from exc
        mode = "automatic" if "automatic" in entry["allowed_invocation_modes"] else "attended"
        _validate_test_targets(entry, args, kwargs, function)
        _THREAD.state = {"test_only": True, "mode": mode}
        try:
            yield _test_receipt(entry, mode, measured)
        finally:
            _THREAD.state = None
        return

    base_context = context
    handle = _open_store_lock(base_context)
    try:
        if (base_context["operation"]["registry_id"] == "attended-memory-mcp"
                and entry_id != "attended-memory-mcp"):
            try:
                context = execution_context.refresh_for_operation(base_context, entry_id)
            except execution_context.ContextError as exc:
                raise MutationAuthorityError(f"MCP request context refused: {exc}") from exc
        _validate_explicit_targets(context, entry, args, kwargs, function)
        _run_after_lock_test_hook()
        try:
            execution_context.revalidate_context(context)
        except execution_context.ContextError as exc:
            raise MutationAuthorityError(f"under-lock context revalidation refused: {exc}") from exc
        _THREAD.state = {"test_only": False, "context": context, "lock": handle}
        try:
            yield _consume(context, entry, measured, supplied_capability)
        finally:
            _THREAD.state = None
        if base_context["operation"]["registry_id"] == "attended-memory-mcp":
            try:
                execution_context.refresh_current_context(base_context)
            except Exception:  # noqa: BLE001 — the durable writer already committed; never invite a retry
                # The writer has already committed successfully. Keep the previous renewable root alive so the
                # next request can refresh under the lock; any ordinary cache-refresh fault must never turn a
                # committed mutation into an apparent failure that callers retry. BaseException still escapes.
                pass
    finally:
        _close_store_lock(handle)


def authorize_nested(entry_id: str, *, measured_cardinality: int = 1):
    """Consume authority for a special internal seam such as capture-lock creation."""
    state = getattr(_THREAD, "state", None)
    entry = _entry(entry_id)
    if state is None:
        scoped_test_mode = _TEST_SCOPE.get()
        if scoped_test_mode is not None:
            return _test_receipt(entry, scoped_test_mode, measured_cardinality)
        if _test_adapter_allowed():
            mode = "automatic" if "automatic" in entry["allowed_invocation_modes"] else "attended"
            return _test_receipt(entry, mode, measured_cardinality)
        raise MutationAuthorityError(f"{entry['writer']} requires an active locked mutation scope")
    if state.get("test_only"):
        return _test_receipt(entry, state["mode"], measured_cardinality)
    return _consume(state["context"], entry, measured_cardinality)


def _guard(entry_id: str, function):
    if getattr(function, "__engine_registry_id__", None) == entry_id:
        return function

    @functools.wraps(function)
    def guarded(*args, **kwargs):
        supplied = kwargs.pop("_engine_capability", None)
        with mutation_scope(entry_id, args, kwargs, supplied_capability=supplied, function=function):
            return function(*args, **kwargs)

    guarded.__engine_registry_id__ = entry_id
    return guarded


def guard(entry_id: str):
    """Decorator form for writers registered by another decorator during module import."""
    _entry(entry_id)
    return lambda function: _guard(entry_id, function)


def _module_name(namespace: dict) -> str:
    name = namespace.get("__name__")
    if isinstance(name, str) and name != "__main__":
        return name
    path = os.path.realpath(namespace.get("__file__", ""))
    marker = os.sep + ".engine" + os.sep + "tools" + os.sep
    if marker not in path:
        return str(name)
    rel = path.split(marker, 1)[1]
    return os.path.splitext(rel)[0].replace(os.sep, ".")


def install_module_guards(namespace: dict) -> tuple[str, ...]:
    """Wrap every registered mutating function defined by one completed module."""
    module = _module_name(namespace)
    installed = []
    for entry in mutation_contract.REGISTRY:
        writer_module, _, function_name = entry["writer"].rpartition(".")
        if (writer_module != module or entry["effect_class"] == "semantic-read"
                or entry["id"] in _SKIP_WRAPPERS):
            continue
        function = namespace.get(function_name)
        if not callable(function):
            raise MutationAuthorityError(
                f"registered persistent writer {entry['writer']} is unavailable while installing guards")
        namespace[function_name] = _guard(entry["id"], function)
        installed.append(entry["id"])
    return tuple(installed)


def guarded_registry_ids(namespace: dict) -> tuple[str, ...]:
    """Return the registry ids visibly installed in a module, for coverage tests."""
    return tuple(sorted(
        value.__engine_registry_id__ for value in namespace.values()
        if callable(value) and isinstance(getattr(value, "__engine_registry_id__", None), str)
    ))
