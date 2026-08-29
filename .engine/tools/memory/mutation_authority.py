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
import json
import os
import stat
import sys
import threading
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
_PRE_ACTIVATION_SCOPE = ContextVar("engine_pre_activation_mutation_scope", default=None)
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


def _source_bound_frame(frame, *, test_only: bool = False, module_name: str | None = None,
                        function_name: str | None = None) -> bool:
    """Trust an exact code object exported by its loaded on-disk module, never a claimed filename."""
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
    if os.path.realpath(frame.f_code.co_filename) != real:
        return False
    if module_name is not None and claimed_module.rsplit(".", 1)[-1] != module_name:
        return False
    if function_name is not None:
        return frame.f_code in _code_tree(getattr(module, function_name, None))
    if test_only:
        tools_root = os.path.realpath(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) + os.sep
        name = os.path.basename(real)
        if not (real.startswith(tools_root) and name.startswith("test_") and name.endswith(".py")):
            return False
    return frame.f_code in _module_code_objects(module)


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


@contextmanager
def preactivation_local_scope(entry_id: str, *, project_root: str):
    """Issue one exact attended capability for the setup-era local landing hint."""
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
    token = _PRE_ACTIVATION_SCOPE.set({"entry_id": entry_id, "project_root": root, "used": False})
    try:
        yield
    finally:
        _PRE_ACTIVATION_SCOPE.reset(token)


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
            yield _test_receipt(entry, state["mode"], measured)
            return
        context = state["context"]
        _validate_explicit_targets(context, entry, args, kwargs, function)
        yield _consume(context, entry, measured, supplied_capability)
        return

    bootstrap = _PRE_ACTIVATION_SCOPE.get()
    if bootstrap is not None:
        if entry_id != bootstrap["entry_id"] or bootstrap["used"]:
            raise MutationAuthorityError("pre-activation local capability is wrong or already consumed")
        arguments = _call_arguments(function, args, kwargs)
        target = arguments.get("main")
        if (not isinstance(target, (str, os.PathLike))
                or os.path.realpath(os.fspath(target)) != bootstrap["project_root"]):
            raise MutationAuthorityError("pre-activation local capability target mismatch")
        bootstrap["used"] = True
        yield _test_receipt(entry, "attended", measured)
        return

    scoped_test_mode = _TEST_SCOPE.get()
    if scoped_test_mode is not None:
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
            except execution_context.ContextError as exc:
                raise MutationAuthorityError(f"MCP server context refresh refused: {exc}") from exc
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
