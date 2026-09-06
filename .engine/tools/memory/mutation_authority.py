#!/usr/bin/env python3
"""Locked, context-bound authority for every persistent memory writer.

Modules call :func:`install_module_guards` after defining their public and private write seams.  The guard
serializes the outer operation on the stable store-identity lock, revalidates the immutable execution context
after that lock is held, and consumes one exact registry capability immediately before the function begins.
Nested helpers reuse the held store lock but consume independent subgrants, so composite capture, backup,
compaction, and restore operations remain one coherent critical section without laundering one broad token
through several distinct effects.

Production calls without an accepted execution context are TIERED, not uniformly refused. The tier is data in
``mutation_contract`` (``degraded_disposition``): an effect whose loss costs no memory — diagnostics, markers,
caches, and the regenerable search indexes — proceeds and returns an unqualified receipt; anything that writes
the record itself or the machinery that can destroy it refuses with a sentence the operator can act on.
Refusing everything is what took memory, capture and Build entry down in
StarshipSuperjam/engine-template#1153; refusing the right things is what
StarshipSuperjam/engine-template#1151 actually asked for. Existing hermetic unit tests retain a
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
import time
import weakref
from contextvars import ContextVar
from contextlib import contextmanager

try:  # package import in production
    from . import execution_context, mutation_contract, refusals
except ImportError:  # direct memory-module CLI import
    import execution_context  # type: ignore
    import mutation_contract  # type: ignore
    import refusals  # type: ignore

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX is the current Engine floor
    fcntl = None


class MutationAuthorityError(RuntimeError):
    """A persistent call lacked exact context, lock, target, or capability authority.

    The plain type carries the invariant and tamper failures (measured cardinality, target escapes, capability
    misuse, guard installation) - engine faults whose text may name a writer or a path and is NOT for the
    operator. The refusals written to be read by the operator raise `MutationRefusal` below instead."""


class MutationRefusal(refusals.EngineRefusal, MutationAuthorityError):
    """A write refused with a sentence written for the operator: the qualification refusal, every stale-context
    refusal, the re-seal refusal, and the three lock refusals (which name no path and say what the machine
    lacks). Raised nowhere else; the MCP seam translates exactly this."""


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
# Registry entries whose authority is carried somewhere OTHER than an auto-installed wrapper on the named
# writer. `capture-lock-create` is taken through `authorize_nested`. `automatic-capture` is the outer, fail-soft
# boundary of a leaf that documents itself as never raising into its caller: wrapping it there turned a
# qualification refusal into an exception out of `capture_turn_delta`, which is the one thing that function
# promises cannot happen. The mutation itself is still fully guarded one frame in, by `capture-transaction` on
# `capture._capture`, so the refusal lands INSIDE the leaf's own fail-soft body — no append, no cursor move,
# and a recorded capture-status marker instead of a crash.
_SKIP_WRAPPERS = frozenset({"capture-lock-create", "automatic-capture"})
_TOOLS_ROOT = os.path.realpath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_ENGINE_ROOT = os.path.realpath(os.path.dirname(os.path.dirname(_TOOLS_ROOT)))
_INSTANTIATOR_SOURCE = os.path.join(_TOOLS_ROOT, "instantiator.py")

# The TERMINAL-ATTENDED verbs. These are the operator-present CLI verbs that move private cleartext OUT of the
# governed store, or propose its permanent erasure — the ClawMem exporter and the erasure verb. They cannot carry
# an ordinary execution context: that context is minted only for AI-SESSION operations. `terminal_attended()`
# gives them their own authority instead, gated on a real terminal AND a caller that is one of these exact verb
# entrypoints running its own genuine loaded code, then authorizes that verb's OWN registered writes (nested or
# sibling), and only those, for the block.
#
# HONESTY ABOUT THE tty GATE — it is a SPEED-BUMP, not a proof of human presence, exactly as `erase.py`'s own
# docstring says. `isatty()` distinguishes an ordinary automated run from a terminal, but an AI session that
# allocates a pseudo-terminal (`pty.spawn`) makes it return True and can then run these GENUINE verbs — the tty +
# genuine-frame checks do not stop that. What keeps it acceptable is NOT the gate's strength but the bounded
# stakes: the export is a scrubbed, withhold-honouring projection of a ledger already in cleartext on disk (no new
# read capability), and erasure's real barrier is the operator merging its pull request on GitHub. So do NOT treat
# "terminal-attended" as an invariant that a human is present; it means "not a plain unattended run, over data the
# session can already read." Keyed by the verb's source file; each value is (entrypoint function name, the
# registry ids it may authorize).
_TERMINAL_ATTENDED_VERBS = {
    os.path.realpath(os.path.join(_TOOLS_ROOT, "memory", "clawmem_export.py")): (
        "main", frozenset({"attended-clawmem-export", "attended-clawmem-export-teardown"})),
    os.path.realpath(os.path.join(_TOOLS_ROOT, "memory", "erase.py")): (
        "main", frozenset({"attended-erasure-request", "erasure-proposal-write", "erasure-pr-open"})),
}
_PREACTIVATION_LOCK = threading.RLock()
_PREACTIVATION_ISSUER = object()
_PREACTIVATION_GRANTS = weakref.WeakKeyDictionary()
_TRACKED_SOURCE_LOCK = threading.RLock()
_TRACKED_SOURCE_CACHE = {}
#: Compiled-source signatures, keyed by the exact bytes they were compiled from. `_tracked_head_source` was
#: already cached; the COMPILE was not, and it is the expensive half — see `_compiled_signatures`.
_COMPILED_SIGNATURE_CACHE = {}


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


def _compiled_signatures(real: str, payload: bytes):
    """Every code signature compiled from one exact source snapshot, as a set, computed once per snapshot.

    Purely a cost fix, and it changes no decision: the answer is a function of the source BYTES alone, so
    keying on their digest returns exactly what recompiling would. Found while validating the repair round —
    one index test that takes 1.6 seconds without the qualification guard took 288 seconds with it, and the
    profile put 256 of those in `builtins.compile`. This adapter fires on every guarded mutation, so a test
    that writes 33,000 records recompiled and re-walked the whole test module 33,000 times.

    The verification itself is unchanged and still per call: the file is re-read, its identity re-checked
    either side of the read, and the LIVE frame's code compared against these signatures. Returns None if the
    signatures cannot be hashed, so the caller falls back to the exact comparison rather than guessing.
    """
    key = (real, hashlib.sha256(payload).digest())
    with _TRACKED_SOURCE_LOCK:
        cached = _COMPILED_SIGNATURE_CACHE.get(key)
    if cached is not None:
        return cached
    # Decoded HERE rather than taken as a second argument. Keying on `payload` while compiling a separately
    # supplied `source` would make soundness depend on a caller keeping two things in step, with nothing
    # checking it; deriving one from the other makes the key and the compiled input the same bytes by
    # construction.
    compiled = _compiled_code_tree(
        compile(payload.decode("utf-8"), real, "exec", dont_inherit=True))
    try:
        signatures = frozenset(_code_signature(candidate) for candidate in compiled)
    except TypeError:      # a constant that will not hash — fall back to the exact linear comparison
        return None
    with _TRACKED_SOURCE_LOCK:
        _COMPILED_SIGNATURE_CACHE[key] = signatures
    return signatures


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
        signatures = _compiled_signatures(real, payload)
        compiled = (None if signatures is not None
                    else _compiled_code_tree(compile(source, real, "exec", dont_inherit=True)))
    except (OSError, UnicodeError, SyntaxError):
        return False
    if os.path.realpath(frame.f_code.co_filename) != real:
        return False
    if signatures is not None:
        try:
            if _code_signature(frame.f_code) not in signatures:
                return False
        except TypeError:  # an unhashable live signature: refuse rather than admit an uncomparable frame
            return False
    elif not any(_same_compiled_code(frame.f_code, candidate) for candidate in compiled):
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


def _default_mode(entry: dict) -> str:
    """The invocation mode to assume when no context named one.

    An effect that may only be enacted with someone attending resolves to `attended`: a unit test or a
    directly-invoked maintenance call IS the attended path, and calling it `automatic` would make the
    attendance rule refuse the very callers it was never aimed at (a background hook is what it aims at,
    and a background hook reaches this code through an installed context that says so).
    """
    if mutation_contract._needs_attendance(entry) or "automatic" not in entry["allowed_invocation_modes"]:
        return "attended"
    return "automatic"


def _degraded_receipt(entry: dict, mode: str, measured: int) -> dict:
    """The receipt for an effect an UNQUALIFIED session is allowed to perform.

    It is deliberately not a capability receipt: no context was resolved, so there is nothing to bind to and
    nothing to consume. It says plainly what it is, so a caller or a later reader can tell a degraded effect
    from a qualified one rather than having to infer it.
    """
    try:
        mutation_contract.classify(
            writer=entry["writer"], target_kind=entry["target_kind"], effect_class=entry["effect_class"],
            invocation_mode=mode, measured_cardinality=measured, schema_cutover=entry["schema_cutover"],
        )
    except Exception as exc:
        raise MutationAuthorityError(str(exc)) from exc
    receipt = {
        "unqualified": True, "registry_id": entry["id"], "writer": entry["writer"],
        "target_kind": entry["target_kind"], "effect_class": entry["effect_class"],
        "mode": mode, "measured_cardinality": measured,
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


def _terminal_attended_verb_frame(entry_ids: frozenset) -> bool:
    """True iff one of the sanctioned terminal verb entrypoints — running its own GENUINE loaded code, not a
    monkeypatched stand-in — is on the stack and is allowed to authorize every id in `entry_ids`."""
    frame = inspect.currentframe()
    try:
        frame = frame.f_back if frame is not None else None
        while frame is not None:
            sanctioned = _TERMINAL_ATTENDED_VERBS.get(os.path.realpath(frame.f_code.co_filename))
            if sanctioned is not None:
                function_name, allowed = sanctioned
                # `_source_bound_frame` proves the frame's code is the module's own currently-loaded source (not a
                # patched replacement); the function-name membership proves it is THIS verb's entrypoint, not some
                # other function in the same file; and the id subset proves the verb is not reaching past what it
                # is allowed to authorize.
                if entry_ids <= allowed and _source_bound_frame(frame):
                    module = sys.modules.get(frame.f_globals.get("__name__"))
                    target = getattr(module, function_name, None) if module is not None else None
                    if any(frame.f_code is candidate for candidate in _code_tree(target)):
                        return True
            frame = frame.f_back
    finally:
        del frame
    return False


def _terminal_attended_receipt(entry: dict, measured: int) -> dict:
    try:
        mutation_contract.classify(
            writer=entry["writer"], target_kind=entry["target_kind"], effect_class=entry["effect_class"],
            invocation_mode="attended", measured_cardinality=measured,
            schema_cutover=entry["schema_cutover"],
        )
    except Exception as exc:
        raise MutationAuthorityError(str(exc)) from exc
    receipt = {
        "exception": "operator-attended-terminal",
        "registry_id": entry["id"], "writer": entry["writer"], "mode": "attended",
        "measured_cardinality": measured,
    }
    receipt["receipt_digest"] = _digest(receipt)
    return receipt


@contextmanager
def terminal_attended(entry_ids):
    """Authority a terminal verb runs its own registered writes on: a real terminal, no execution context.

    These verbs (the ClawMem exporter, the erasure verb) move private cleartext out of the governed store or
    propose its permanent erasure. They carry no execution context — a context is minted only for AI-session
    operations — so the terminal gate is their authority instead. This checks BOTH halves BEFORE it opens: stdin
    AND stdout are a terminal, and the caller is one of the exact verb entrypoints allowed to authorize every id in
    `entry_ids`. Inside the block, every registered write named in `entry_ids` — nested or sibling — is authorized;
    ANY other writer is refused, so an allowed verb cannot become a door to the store beneath it.

    The tty half is a SPEED-BUMP, not a proof a human is present: an AI that allocates a pseudo-terminal passes it
    and can run these genuine verbs (see `_TERMINAL_ATTENDED_VERBS`). It is acceptable only because the stakes are
    bounded — the export is a scrubbed projection of a ledger already readable on disk, and erasure's real gate is
    the merged pull request. Do not build on `terminal_attended` as if it guaranteed attendance."""
    ids = frozenset(entry_ids)
    for entry_id in ids:
        _entry(entry_id)  # every id must name a real registry entry, or this is a programming error, not authority
    if getattr(_THREAD, "state", None) is not None:
        raise MutationAuthorityError("a terminal-attended operation cannot open inside another mutation scope")
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise MutationAuthorityError(
            "this runs only from a real terminal: stdin and stdout are not a terminal here, so it was refused "
            "before anything was written.")
    if not _terminal_attended_verb_frame(ids):
        raise MutationAuthorityError(
            "terminal-attended authority is available only to the engine's own terminal verbs, for their own "
            "registered writes")
    _THREAD.state = {"test_only": False, "terminal_attended": True, "allowed_entries": ids}
    try:
        yield
    finally:
        _THREAD.state = None


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
        raise MutationRefusal("This machine's Python installation is missing the file-locking feature memory "
                              "writes need, so writing is held and nothing was changed. Recall keeps working. "
                              "This is unusual and usually means the Python running the engine's tools is not "
                              "an ordinary installation. " + refusals.ESCALATION)
    lock_path = context["target"]["lifecycle"]["store_identity_lock"]
    try:
        info = os.lstat(lock_path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise MutationRefusal("The memory store's lock file is not an ordinary file, so writing is held and "
                                  "nothing was changed - something replaced it on disk. Recall keeps working. "
                                  + refusals.ESCALATION)
        handle = open(lock_path, "r", encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle
    except MutationAuthorityError:
        raise
    except OSError as exc:
        raise MutationRefusal("The memory store's lock could not be taken, so writing is held and nothing was "
                              "changed - the memory folder may be unreadable or its disk unmounted. Reads from "
                              "this store may be held too, and each read answer says how it was resolved. "
                              + refusals.ESCALATION) from exc


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


_LAST_RESEAL = None  # the one-shot stale-state retry's before/after binding and measured cost


def _record_reseal(before: dict, after: dict, cost_seconds: float) -> None:
    """Record the last stale-state re-seal so its binding equality and cost are observable.

    The retry is bounded to one attempt, so this holds exactly that attempt's evidence: the binding
    before and after, whether they were preserved, and how long the single re-seal took. Diagnostics and
    tests read it; it drives no control decision, so a plain module global is the right home."""
    global _LAST_RESEAL
    _LAST_RESEAL = {
        "binding_preserved": before == after,
        "before": before,
        "after": after,
        "cost_seconds": cost_seconds,
    }


def last_reseal() -> dict | None:
    """The most recent stale-state re-seal's binding-equality and cost evidence, or None if none ran."""
    return _LAST_RESEAL


def _stale_refusal(exc: "execution_context.ContextError") -> str:
    """The plain, content-free sentence a write answers with when its bound context no longer matches disk.

    Keyed on the exception TYPE, never its text: the raw `ContextError` message names paths, fingerprints
    and commits, none of which belong in an operator- or client-facing refusal (obligation: no path,
    fingerprint or commit reaches the caller for any refusal). Every branch says what held the write, that
    nothing changed, what reads do under the SAME class - and that clause is true for its branch: under the
    two MOVED classes reads answer from disk and say so; under the UNBOUND classes reads are held too and say
    so (StarshipSuperjam/engine-template#1211's over-promise was one sentence for both) - then the one restart
    action and the one escalation
    pointer reads and writes share (memory/refusals.py)."""
    tail = " " + refusals.RESTART_ACTION + " " + refusals.ESCALATION
    if isinstance(exc, (execution_context.ActivationStale, execution_context.AcceptedTreeStale)):
        return (
            "This project moved to a new commit while this memory server was running, so its write context no "
            "longer matches the project on disk. Nothing was changed, and writing is held. Recall keeps working, "
            "and every read answer says how it was resolved." + tail
        )
    if isinstance(exc, execution_context.ArtifactUnreadable):
        return (
            "A memory file on disk could not be read, so writing is held and nothing was changed - a problem "
            "with the store on disk, not with what is saved in it. Reads from this store are held too, and each "
            "read answer says so." + tail
        )
    if isinstance(exc, (execution_context.StoreIdentityStale, execution_context.BackupPointerStale)):
        return (
            "The memory store under this session is not the one it was bound to, so writing is held and nothing "
            "was changed. Reads from this store are held too, and each read answer says so." + tail
        )
    return (
        "This session's memory context could not be confirmed against the store, so writing is held and "
        "nothing was changed. Reads from this store are held too, and each read answer says so." + tail
    )


@contextmanager
def mutation_scope(entry_id: str, args: tuple, kwargs: dict, *, supplied_capability=None, function=None):
    """Hold one coherent outer lock and consume this exact writer's one-shot subgrant."""
    entry = _entry(entry_id)
    measured = _measured_cardinality(entry, args, kwargs)
    if entry_id in mutation_contract.DIAGNOSTIC_PRIVATE_ENTRY_IDS:
        # The narrowly authorized diagnostic tier, routed BEFORE any context is read, any store lock is
        # taken, or any authority is refreshed — every one of those is a step that can itself fail in a
        # stranded session, and a diagnostic that dies on the guard records nothing about the fault it exists
        # to record (StarshipSuperjam/engine-template program prg_d15d7dc8f3df, C1). The early route is safe
        # only because the tier is narrow by construction (the registry says which writers qualify and why):
        # the writer's sole destination is its own gitignored file and it takes no destination argument, so
        # this receipt is a door to nothing beneath it. It is still classified (cardinality, mode) — it is
        # early, not unchecked — and it neither reads nor replaces `_THREAD.state`, so a diagnostic taken
        # inside another writer's scope leaves that scope exactly as it found it. This is the ONE bypass of
        # the scope rules below — including a terminal-attended verb's "only its own writes" contract and a
        # test scope's test receipt — and it is bypassed for a file no scope could reach anyway.
        yield _degraded_receipt(entry, _default_mode(entry), measured)
        return
    state = getattr(_THREAD, "state", None)
    if state is not None:
        if state.get("test_only"):
            yield _test_receipt(entry, state["mode"], measured)
            return
        if state.get("terminal_attended"):
            # A person at a real terminal opened this scope for one verb's own writes. Authorize exactly those,
            # nested or sibling; refuse anything else so the verb cannot become a door to the store beneath it.
            if entry_id in state["allowed_entries"]:
                yield _terminal_attended_receipt(entry, measured)
                return
            raise MutationAuthorityError(
                f"{entry['writer']} is not one of the writes this terminal-attended operation is authorized for")
        if state.get("degraded"):
            # A nested writer inside a degraded outer effect is tiered on its OWN entry, never waved through
            # by its caller: an allowed diagnostic must not become a door to the ledger beneath it.
            if mutation_contract.degraded_disposition(entry) == "refuse":
                raise MutationRefusal(mutation_contract.degraded_refusal(entry))
            yield _degraded_receipt(entry, state["mode"], measured)
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
        _THREAD.state = {"test_only": True, "mode": scoped_test_mode}
        try:
            yield _test_receipt(entry, scoped_test_mode, measured)
        finally:
            _THREAD.state = None
        return

    try:
        context = execution_context.current_context()
    except execution_context.ContextError as exc:
        if _test_adapter_allowed():
            mode = _default_mode(entry)
            _THREAD.state = {"test_only": True, "mode": mode}
            try:
                yield _test_receipt(entry, mode, measured)
            finally:
                _THREAD.state = None
            return
        if mutation_contract.degraded_disposition(entry) == "refuse":
            raise MutationRefusal(mutation_contract.degraded_refusal(entry)) from exc
        mode = _default_mode(entry)
        _THREAD.state = {"test_only": False, "degraded": True, "mode": mode}
        try:
            yield _degraded_receipt(entry, mode, measured)
        finally:
            _THREAD.state = None
        return

    base_context = context
    handle = _open_store_lock(base_context)
    try:
        stale = None
        if (base_context["operation"]["registry_id"] == "attended-memory-mcp"
                and entry_id != "attended-memory-mcp"):
            try:
                context = execution_context.refresh_for_operation(base_context, entry_id)
            except execution_context.ContextError as exc:
                stale = exc
        if stale is None:
            _validate_explicit_targets(context, entry, args, kwargs, function)
            _run_after_lock_test_hook()
            try:
                execution_context.revalidate_context(context)
            except execution_context.ExpectedStateStale:
                # The one refreshable staleness: the observed store fingerprint drifted (a keyword index
                # that healed itself, a sibling write that landed between binding and this lock) while the
                # store identity, the accepted activation and the backup pointer all still held. Re-seal
                # from the SAME document exactly once, timing that single attempt, and refuse if the re-seal
                # moved any authority-bearing field. A genuine activation, store or pointer change is never
                # ExpectedStateStale, so it re-raises its own typed subclass through the re-seal and is
                # caught below; this retry can only ever advance an in-place fingerprint, never widen binding.
                before = execution_context.binding_identity(context)
                started = time.perf_counter()
                try:
                    context = execution_context.reseal_for_stale_state(context)
                except execution_context.ContextError as exc:
                    stale = exc
                else:
                    after = execution_context.binding_identity(context)
                    _record_reseal(before, after, time.perf_counter() - started)
                    if before != after:
                        raise MutationRefusal(
                            "This session's memory binding shifted while its write context was being "
                            "refreshed, so writing is held and nothing was changed. Each read answer says "
                            "how it was resolved. " + refusals.RESTART_ACTION + " " + refusals.ESCALATION)
            except execution_context.ContextError as exc:
                stale = exc
        if stale is not None:
            # A stale binding is not a dead store. Refuse only the writers whose contract says a degraded
            # run must refuse; degrade the reads so recall keeps answering — each read tool attaches its
            # own plain caveat — rather than failing the caller outright. This mirrors, under the lock, the
            # no-context branch above, so a stale context and an absent one route by the same disposition.
            if mutation_contract.degraded_disposition(entry) == "refuse":
                raise MutationRefusal(_stale_refusal(stale)) from stale
            mode = _default_mode(entry)
            _THREAD.state = {"test_only": False, "degraded": True, "mode": mode}
            try:
                yield _degraded_receipt(entry, mode, measured)
            finally:
                _THREAD.state = None
            return
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
            mode = _default_mode(entry)
            return _test_receipt(entry, mode, measured_cardinality)
        raise MutationAuthorityError(f"{entry['writer']} requires an active locked mutation scope")
    if state.get("test_only"):
        return _test_receipt(entry, state["mode"], measured_cardinality)
    if state.get("terminal_attended"):
        if entry_id in state["allowed_entries"]:
            return _terminal_attended_receipt(entry, measured_cardinality)
        raise MutationAuthorityError(
            f"{entry['writer']} is not one of the writes this terminal-attended operation is authorized for")
    if state.get("degraded"):
        if mutation_contract.degraded_disposition(entry) == "refuse":
            raise MutationRefusal(mutation_contract.degraded_refusal(entry))
        return _degraded_receipt(entry, state["mode"], measured_cardinality)
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
