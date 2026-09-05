#!/usr/bin/env python3
"""The stranding log — a narrowly authorized, no-throw diagnostic path for the memory server.

Why this exists. When the memory server's recall crashes, the MCP boundary flattens the exception to a bare
"Error executing tool" before it reaches the client, and the process that knows the real exception is the
long-lived server itself — which cannot be instrumented from outside. So the only instrument that can ever
observe the real fault is one that lives INSIDE the server and records a content-safe trace at the moment the
fault happens. That is this module. It is the observation instrument of program prg_d15d7dc8f3df (child C1):
once it is deployed into an attended server (activation + a new process — merging alone deploys nothing), the
next real stranding leaves a trace here instead of nothing.

What makes it safe to run in a STRANDED session. The mutation guard (`mutation_authority.mutation_scope`)
opens the store lock and refreshes write authority before it routes anything as degraded, and it catches only
`ContextError` on that path — so a writer registered as an ordinary degraded-allowed effect is stopped by an
unavailable lock or an unexpected refresh error in exactly the sessions this log is for. This writer is
therefore named in `mutation_contract.DIAGNOSTIC_PRIVATE_ENTRY_IDS`, the tier the guard routes BEFORE it reads
a context, takes the lock, or refreshes anything. The price of that early route is narrowness, and the narrowness
is structural, not a promise: the writer's only destination is its own gitignored file under the engine's cache
directory — `.engine/telemetry/.cache/stranding-log.ndjson` under the project root, with at most one rotation
beside it as `stranding-log.ndjson.1` (`sink_path`) — it takes no destination argument in production (`path` is
honored only under the unit test harness), and it can write nothing else — never the ledger, a derived cache,
the activation, or a backup. The CLI (`readiness`, `export`, `baseline`) is how an operator or a later session
reads it. Two different processes answer two different questions, and a deployment receipt needs both:
`health`, called over the live MCP connection to the DEPLOYED server, is the only source of that server's own
facts — the loaded `<commit>-<tree>` and its qualification tier (a health answer with no `diagnostics` block
means that server has not loaded the instrument at all, which before activation and a new process is the
expected answer); the `readiness` verb reports on the PROCESS THAT RUNS IT — from a shell that is always
qualification `none` and code_version null — and is the one that confirms, with git, that the sink under the
project root is ignored, which `health` deliberately never checks (a health probe forks nothing).

What it records — and, more importantly, what it never records. Only: the exception's TYPE (a checked,
capped dotted identifier), a capped chain of cause/context TYPES, a few frame LOCATIONS (a checked file
basename, line number, function name — read from the live frames, never through a `FrameSummary`, so no source
line is ever loaded, and never `f_locals`), the pid, the loaded code version and a shape-checked activation
identity, the qualification tier, presence booleans for a fixed set of engine environment keys, and a
checked-enum event. The exception's MESSAGE is never written: it is the one field that carries whatever the
failing code was holding.

Every public entry point is wrapped in a no-throw boundary that encloses the guard itself: a diagnostic that
crashed the thing it was diagnosing would be worse than none. It returns False — one attempt, no retry, no
wait — when it cannot record, so a caller can tell an honest miss from a saved trace.
"""
from __future__ import annotations

import enum
import fcntl
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import time

_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from memory import mutation_authority as _mutation_authority  # noqa: E402
from memory import mutation_contract as _mutation_contract  # noqa: E402

APPEND_REGISTRY_ID = "stranding-log-append"
EXPORT_REGISTRY_ID = "stranding-log-export"
SINK_NAME = "stranding-log.ndjson"
SCHEMA_VERSION = "stranding-log.v1"

# The engine root this module ships in: .engine/tools/memory/stranding_log.py -> parents[3]. Under a
# qualified launch that is the materialized accepted tree (inside the git common dir, not a work tree); the
# project root the sink and git questions belong to arrives as ENGINE_PROJECT_ROOT from either launcher.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# One rotation, at most, per append: when the sink is this large it is renamed to `.1` (dropping the previous
# `.1`) and a fresh sink starts. Bounded disk, no loop, no second attempt.
_ROTATE_BYTES = 256 * 1024
_OUTER_FRAMES = 2       # the outermost ENGINE frames (the tool entry point), never the SDK plumbing above it ...
_INNER_FRAMES = 4       # ... and where it actually broke
# What places a frame's file inside the engine's OWN code — a checkout's or an accepted tree's `.engine/tools/`.
# Not `/.engine/` alone: the launchers run `uv run --directory .engine`, so the project's Python environment and
# every SDK frame live under `.engine/.venv/…` and would match too, and the budget would keep the plumbing.
_ENGINE_MARK = f"{os.sep}.engine{os.sep}tools{os.sep}"
_MAX_ROOT_PROBES = 8    # distinct live-checkout roots the server sweep will ask git about in one baseline
_MAX_CHAIN = 5
_MAX_SEGMENTS = 8
_MAX_SEGMENT = 64
_MAX_OBSERVED_ERROR = 240
_SUBPROCESS_TIMEOUT = 5

# Environment keys whose PRESENCE is recorded (never their values — several are paths). The set is closed on
# purpose: adding a key here is a review-visible change to what the log can say about the process.
_ENV_PRESENCE_KEYS = (
    "ENGINE_MEMORY_DIR", "ENGINE_PROJECT_ROOT", "ENGINE_BOOT_CACHE_DIR",
    "ENGINE_ACCEPTED_HOOK_CONTEXT", "ENGINE_PERSISTENT_EXECUTION_CONTEXT",
    "ENGINE_QUALIFICATION_DEGRADED",
)
# Environment keys whose VALUE is recorded — as a boolean ("is it set to 1"), never the string itself, so
# even a value someone put a secret into cannot flow through. Structural, non-secret, non-path by construction.
_ENV_VALUE_KEYS = ("PYTHONNOUSERSITE",)
# The record fields the sanitized export keeps. Anything else in a raw record is dropped on export, not
# redacted. Every field here is either an enum, a number, a shape-checked identifier, or a checked path-free
# string; `observed_error` carries text only when it IS the content-free generic boundary string.
_EXPORT_KEYS = frozenset({
    "schema_version", "ts", "event", "pid", "tool", "exception", "frames", "qualification", "activation",
    "code_version", "env_present", "env", "observed_error", "servers", "servers_scope", "activation_on_disk",
    "lifecycle", "cache_effects", "query_kind",
})
_SAFE_TOOL = re.compile(r"[a-z][a-z0-9\-]{0,63}")
_SAFE_BASENAME = re.compile(r"[A-Za-z0-9_.\-]{1,80}\.py|<[a-z ]{1,24}>")
_TREE_NAME = re.compile(r"[0-9a-f]{40}-[0-9a-f]{40}")
_HEX_OID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_RELEASE = re.compile(r"[0-9A-Za-z.\-+]{1,32}")
_SLUG = re.compile(r"[A-Za-z0-9_.\-]{1,100}/[A-Za-z0-9_.\-]{1,100}")
# The one boundary string the MCP layer emits for an unexpected fault. Content-free by construction, so it is
# the only observed error text the baseline keeps verbatim.
_GENERIC_TOOL_ERROR = re.compile(r"Error executing tool [a-z0-9\-]{1,64}\.?", re.I)


class Event(enum.Enum):
    """The closed set of things the log can say happened. A free-text event is refused, not recorded."""

    TOOL_FAULT = "tool-fault"          # an unexpected exception escaped a tool (the flattened crash)
    READ_DEGRADED = "read-degraded"    # a read tool attached the stale-context caveat
    SELF_CHECK = "self-check"          # the readiness self-check ran
    BASELINE = "baseline"              # a Stage-0 baseline of the visible failure was taken


# ---- where the log lives ----------------------------------------------------------------------------------

def _project_root() -> str:
    """The project root the sink lives under: what the launcher handed the server, else this engine's own
    root (a bare CLI run). Both launchers set ENGINE_PROJECT_ROOT to the canonical checkout."""
    configured = os.environ.get("ENGINE_PROJECT_ROOT")
    return configured if configured and os.path.isabs(configured) else _ROOT


def _cache_dir() -> str:
    """The engine's own gitignored cache directory, DERIVED from the project root the launcher supplied
    (ENGINE_PROJECT_ROOT, else this engine's own root). The writer takes no destination argument and never
    reads ENGINE_BOOT_CACHE_DIR; what it IS bound to is the launcher's root — a narrower claim than "no
    environment value can move it", which is why `readiness(check_ignore=True)` refuses to call the instrument
    armed until git confirms the sink under that root is ignored."""
    return os.path.join(_project_root(), ".engine", "telemetry", ".cache")


def sink_path() -> str:
    """The one file this module writes. Hardcoded relative to the engine's cache; never a parameter."""
    return os.path.join(_cache_dir(), SINK_NAME)


def _test_path_allowed() -> bool:
    """A caller-supplied `path` is honored ONLY under the unit-test harness (the tests' own temp file), the
    same hermetic rule `hooks._record_crash_debug` follows. In production the argument is ignored, so the
    sink is the writer's only reachable destination."""
    return "unittest" in sys.modules


# ---- what a record may carry --------------------------------------------------------------------------------

def _safe_identifier(value, *, segments: int = _MAX_SEGMENTS) -> str:
    """A dotted Python name, kept only if every segment IS a Python identifier (so no '-', no token shapes),
    with the segment count and each segment's length capped. Anything else is replaced, not trusted: a
    dynamic `__name__` or `__module__` can carry whatever built it."""
    if not isinstance(value, str) or not value:
        return "<unnamed>"
    parts = value.split(".")
    if len(parts) > segments or not all(part.isidentifier() and len(part) <= _MAX_SEGMENT for part in parts):
        return "<unnamed>"
    return value


def _safe_basename(filename) -> str:
    """A frame's file basename, kept only when it looks like a module file (or an interpreter marker such as
    `<string>`): a `compile()`-supplied filename can carry anything."""
    base = os.path.basename(filename) if isinstance(filename, str) else ""
    return base if _SAFE_BASENAME.fullmatch(base) else "<unnamed>"


def _type_name(exc: BaseException) -> str:
    kind = type(exc)
    return _safe_identifier(f"{kind.__module__}.{kind.__qualname__}")


def _exception_facts(exc: BaseException) -> dict:
    """Type, capped type-only chain, and frame LOCATIONS. Reads `f_code.co_filename`, `f_lineno` and
    `co_name` straight off the live frames: no `FrameSummary`, so no source line is loaded, and `f_locals` is
    never touched. The message (`str(exc)`, `exc.args`, `__notes__`) is deliberately absent. Frames keep the
    outermost few (the engine call site) and the innermost few (where it broke)."""
    chain = []
    seen = set()
    link = exc.__cause__ or exc.__context__
    while link is not None and len(chain) < _MAX_CHAIN and id(link) not in seen:
        seen.add(id(link))
        chain.append(_type_name(link))
        link = link.__cause__ or link.__context__
    frames = []
    engine = []
    tb = exc.__traceback__
    while tb is not None:
        code = tb.tb_frame.f_code
        frames.append([_safe_basename(code.co_filename), int(tb.tb_lineno), _safe_identifier(code.co_name)])
        engine.append(isinstance(code.co_filename, str) and _ENGINE_MARK in code.co_filename)
        tb = tb.tb_next
    if len(frames) > _OUTER_FRAMES + _INNER_FRAMES:
        # Every tool call travels the same SDK plumbing (`Tool.run` -> `call_fn` -> a worker thread) before it
        # reaches the engine, so the outermost frames overall would be identical in every record and the
        # engine's own entry point would fall out of the middle. The outer slots therefore go to the
        # outermost ENGINE frames above the inner window — the outermost frames overall only when none exist.
        head, inner = frames[:-_INNER_FRAMES], frames[-_INNER_FRAMES:]
        head_engine = [frame for frame, ours in zip(head, engine) if ours]
        frames = (head_engine or head)[:_OUTER_FRAMES] + inner
    return {"type": _type_name(exc), "chain": chain, "frames": frames}


def _accepted_context() -> dict | None:
    raw = os.environ.get("ENGINE_ACCEPTED_HOOK_CONTEXT")
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _activation_facts(activation) -> dict:
    """The activation identity, each field SHAPE-CHECKED before it is copied: a slug, two object ids, a short
    release string, an integer epoch. A value of any other shape — a path, a nested object — is replaced."""
    source = activation if isinstance(activation, dict) else {}

    def take(key, pattern):
        value = source.get(key)
        if value is None:
            return None
        return value if isinstance(value, str) and pattern.fullmatch(value) else "<malformed>"

    epoch = source.get("epoch")
    return {
        "repository": take("repository", _SLUG),
        "commit": take("commit", _HEX_OID),
        "tree": take("tree", _HEX_OID),
        "engine_release": take("engine_release", _RELEASE),
        "epoch": epoch if isinstance(epoch, int) and not isinstance(epoch, bool) else (
            None if epoch is None else "<malformed>"),
    }


def _qualification() -> str:
    if os.environ.get("ENGINE_ACCEPTED_HOOK_CONTEXT"):
        return "attended"
    if "ENGINE_QUALIFICATION_DEGRADED" in os.environ:
        return "degraded"
    return "none"


def _loaded_tree_name() -> str | None:
    """`<commit>-<tree>` when this file was loaded from a materialized accepted tree (`.../accepted-hooks/
    trees/<commit>-<tree>/.engine/tools/memory/...`), else None — which is itself the fact a deployment
    receipt wants (the instrument is running from a live checkout, not the accepted tree)."""
    name = os.path.basename(_ROOT)
    if os.path.basename(os.path.dirname(_ROOT)) == "trees" and _TREE_NAME.fullmatch(name):
        return name
    return None


def _runtime_facts() -> dict:
    """The loaded code's identity and the process's shape — never its location."""
    context = _accepted_context() or {}
    return {
        "pid": os.getpid(),
        "qualification": _qualification(),
        "activation": _activation_facts(context.get("activation")),
        "code_version": _loaded_tree_name(),
        "env_present": {key: key in os.environ for key in _ENV_PRESENCE_KEYS},
        "env": {key: os.environ.get(key) == "1" for key in _ENV_VALUE_KEYS if key in os.environ},
    }


def _safe_tool(tool) -> str | None:
    if tool is None:
        return None
    return tool if isinstance(tool, str) and _SAFE_TOOL.fullmatch(tool) else "<unnamed>"


def _record(event: Event, exc: BaseException | None, tool: str | None) -> dict:
    record = {"schema_version": SCHEMA_VERSION, "ts": time.time(), "event": event.value}
    record.update(_runtime_facts())
    if tool is not None:
        record["tool"] = _safe_tool(tool)
    if exc is not None:
        record["exception"] = _exception_facts(exc)
    return record


def _encode(record: dict) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


# ---- the one writer ---------------------------------------------------------------------------------------

def _regular_or_absent(path: str) -> bool:
    """True when `path` is absent or a regular file — never a symlink, FIFO, socket or directory, any of which
    could redirect or block the open below. Uses lstat so a link is seen as a link."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode)


def _append(line: str, *, path: str | None = None) -> bool:
    """Append one encoded line to the sink. This is the module's ONLY file writer and the registered
    `stranding-log-append` effect; everything above it is pure computation.

    Locked, single attempt, bounded, non-blocking: the lock and sink paths must be absent or regular files
    (checked before anything is created, so a planted FIFO or symlink is refused rather than opened), both
    opens carry `O_NONBLOCK | O_NOFOLLOW`, the lock is taken `LOCK_NB` (a busy peer means this record is
    dropped, honestly, rather than waited on), and the sink is rotated at most once when it is over the size
    cap. Returns True only after the bytes are written."""
    if _test_path_allowed():
        if path is None:
            # The hermetic backstop: a test that did not name its own temp file records nothing, so the
            # suite never appends harness noise to the production log.
            return False
        target = path
    else:
        target = sink_path()
        # Belt over the derivation: whatever the environment said the project root is, the sink must RESOLVE
        # under it — a symlinked parent cannot carry the file out of the tree.
        root = os.path.realpath(_project_root())
        if os.path.commonpath((os.path.realpath(os.path.dirname(target)), root)) != root:
            return False
    directory = os.path.dirname(target)
    lock_path = target + ".lock"
    if os.path.islink(directory) or not _regular_or_absent(target) or not _regular_or_absent(lock_path):
        return False
    os.makedirs(directory, mode=0o700, exist_ok=True)
    if os.path.islink(directory):
        return False
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
    lock_fd = -1
    try:
        lock_fd = os.open(lock_path, flags, 0o600)
        if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
            return False
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        try:
            size = os.stat(target).st_size
        except FileNotFoundError:
            size = 0
        if size >= _ROTATE_BYTES:
            os.replace(target, target + ".1")
        fd = -1
        try:
            fd = os.open(target, flags | os.O_APPEND, 0o600)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                return False
            # `write(2)` may legitimately store fewer bytes than asked (disk full mid-record, a signal); an
            # unchecked short write would leave a partial line that fuses with the next record and loses BOTH
            # on read while this call still reported success. So: write until the whole line is stored, and if
            # that cannot complete, cut the sink back to where this record began (the exclusive lock above
            # means nothing else appended meanwhile) and report an honest miss.
            payload = (line + "\n").encode("utf-8")
            start, written = info.st_size, 0
            try:
                while written < len(payload):
                    count = os.write(fd, payload[written:])
                    if count <= 0:
                        raise OSError("short write")
                    written += count
            except OSError:
                try:
                    os.ftruncate(fd, start)
                except OSError:
                    pass
                return False
        finally:
            if fd >= 0:
                os.close(fd)
        return True
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)


def record_stranding(event: Event, exc: BaseException | None = None, *, tool: str | None = None,
                     path: str | None = None) -> bool:
    """Record one event. The no-throw boundary: nothing raised inside — not by the record's construction, not
    by the mutation guard, not by the filesystem — escapes to the caller. False means "not recorded"."""
    try:
        if not isinstance(event, Event):
            return False
        return bool(_append(_encode(_record(event, exc, tool)), path=path))
    except Exception:  # noqa: BLE001 — the boundary IS the contract; a diagnostic must never re-break its caller
        return False


# ---- readiness --------------------------------------------------------------------------------------------

def readiness(*, check_ignore: bool = False) -> dict:
    """Whether this instrument can record right now, IN THIS PROCESS, truthfully, without writing anything.
    From a shell that is the shell's own answer (qualification `none`, code_version None): the deployed
    server's tier and loaded version come from its `health` answer over the live connection, never from here.

    `armed` is the one bit a health probe or a deployment receipt reads: the writer is registered, its guard
    is installed, the sink's directory (or the nearest existing parent) is writable, and the test-harness gate
    is not holding the writer back. `sink_ignored` — a `git check-ignore` of the sink from the project root —
    is asked for only when `check_ignore` is set (the CLI), never on a health probe: True/False when git
    answered, None when it could not — and under `check_ignore` the instrument is NOT armed until that answer
    is True, so a receipt cannot report armed at an unverified destination. `reason` says, in one sentence,
    what holds the writer back when it is not armed. `code_version` is the loaded `<commit>-<tree>` (None
    from a live checkout) and `rotated` whether an older `.1` file exists beside the sink — both facts a
    deployment receipt and an export reader need."""
    registered = any(entry["id"] == APPEND_REGISTRY_ID for entry in _mutation_contract.REGISTRY)
    guard_installed = getattr(_append, "__engine_registry_id__", None) == APPEND_REGISTRY_ID
    sink = sink_path()
    probe = os.path.dirname(sink)
    while probe and not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    writable = bool(probe) and os.path.isdir(probe) and os.access(probe, os.W_OK)
    gated = _test_path_allowed()
    ignored = _is_ignored(sink) if check_ignore else None
    reason = None
    if not registered:
        reason = "the writer is not in the mutation registry"
    elif not guard_installed:
        reason = "the writer's mutation guard is not installed"
    elif not writable:
        reason = "the sink's directory is not writable"
    elif gated:
        reason = "the unit-test harness is loaded, which holds the production writer back"
    elif check_ignore and ignored is not True:
        reason = ("git could not confirm the sink is ignored under the project root" if ignored is None
                  else "git reports the sink as NOT ignored under the project root")
    return {
        "schema_version": SCHEMA_VERSION,
        "armed": reason is None,
        "reason": reason,
        "registered": registered,
        "guard_installed": guard_installed,
        "sink_dir_writable": writable,
        "harness_gated": gated,
        "sink_present": os.path.exists(sink),
        "sink_ignored": ignored,
        "rotated": os.path.exists(sink + ".1"),
        "qualification": _qualification(),
        "code_version": _loaded_tree_name(),
    }


def _is_ignored(target: str) -> bool | None:
    """`git check-ignore` asked from the PROJECT root (the sink's own tree), not from this module's root,
    which under a qualified launch is a materialized tree inside the git directory and not a work tree."""
    try:
        done = subprocess.run(
            ["git", "-C", _project_root(), "check-ignore", "-q", "--", target],
            capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode == 0:
        return True
    if done.returncode == 1:
        return False
    return None


# ---- export (sanitized) -----------------------------------------------------------------------------------

def _account_name() -> str:
    """The account name, from the password database rather than USER/LOGNAME (which an agent-spawned process
    may not carry); the home directory's basename is the fallback."""
    try:
        name = pwd.getpwuid(os.getuid()).pw_name
    except (KeyError, OSError):
        name = ""
    return name or os.path.basename(os.path.expanduser("~"))


def _redact(value):
    """Defense in depth over records that already avoid paths and messages: any string that looks like a path
    or carries the account name is replaced wholesale on the way out. The account rule needs a name of at
    least four characters so a short login cannot blank enum fields it merely appears inside."""
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        home = os.path.expanduser("~")
        if home and home != "~" and home in value:
            return "<redacted-path>"
        # A filesystem path: anchored (`/`, `~`, `.`), deeper than one separator, or a Windows drive path. A
        # single `owner/repo` slug is identity, not location, and stays.
        if (value.startswith((os.sep, "~", ".")) or value.count(os.sep) >= 2
                or re.match(r"[A-Za-z]:\\", value) or "\\" in value):
            return "<redacted-path>"
        user = _account_name()
        if len(user) >= 4 and user in value:
            return "<redacted-user>"
    return value


def sanitize(record: dict) -> dict:
    """The exported form of one record: the allowlisted fields only, redacted."""
    return {key: _redact(value) for key, value in record.items() if key in _EXPORT_KEYS}


def read_records(*, path: str | None = None) -> list[dict]:
    """Read the raw sink — the rotated `.1` file first, then the live one, so an export covers the whole
    retained window rather than silently omitting everything older than the last rotation. The source is
    hardcoded: a test may name its own file, production always reads the sink."""
    source = path if (path is not None and _test_path_allowed()) else sink_path()
    out = []
    for candidate in (source + ".1", source):
        try:
            with open(candidate, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        value = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(value, dict):
                        out.append(value)
        except OSError:
            continue
    return out


def export_sanitized(destination: str | None = None, *, path: str | None = None) -> list[dict]:
    """Sanitize every record from the hardcoded source. With no destination the sanitized records are returned
    (the CLI prints them for the caller to redirect). With a destination the export is REFUSED unless the
    destination resolves UNDER the engine's cache directory — the writer's authority is bound structurally,
    exactly as the sink's is, so no context can aim it at the store — AND git reports it ignored; the write
    then goes through the registered `stranding-log-export` effect, so an unqualified session is refused by
    the same tiering as every other export artifact."""
    sanitized = [sanitize(record) for record in read_records(path=path)]
    if destination is None:
        return sanitized
    resolved = os.path.realpath(destination)
    cache = os.path.realpath(_cache_dir())
    if os.path.commonpath((resolved, cache)) != cache or resolved == cache:
        raise ValueError(
            f"refusing to export the stranding log to {destination!r}: an export may land only under the "
            f"engine's own cache directory, and this path resolves elsewhere. Omit the destination and "
            "redirect the printed output yourself if you need it somewhere else.")
    sink = os.path.realpath(sink_path())
    if resolved in (sink, sink + ".1", sink + ".lock"):
        raise ValueError(
            f"refusing to export the stranding log to {destination!r}: that is the raw log itself (or its "
            "rotation or lock), and the sanitized export must never overwrite the record it was made from.")
    if _is_ignored(resolved) is not True:
        raise ValueError(
            f"refusing to export the stranding log to {destination!r}: git does not report that path ignored, "
            "and a diagnostic export must never land in a tracked file.")
    _export_write(resolved, "".join(_encode(item) + "\n" for item in sanitized))
    return sanitized


def _export_write(destination: str, text: str) -> None:
    """Atomic replace of one export file under the cache directory. The registered `stranding-log-export`
    effect; `export_sanitized` has already bound the destination structurally."""
    os.makedirs(os.path.dirname(destination), mode=0o700, exist_ok=True)
    temporary = destination + ".tmp"
    fd = -1
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        os.write(fd, text.encode("utf-8"))
    finally:
        if fd >= 0:
            os.close(fd)
    os.replace(temporary, destination)


# ---- the Stage-0 baseline ---------------------------------------------------------------------------------

_CACHE_EFFECTS = (
    "Observation only: calling a recall tool may reconcile the derived keyword/meaning caches under the memory "
    "directory; nothing is written to the durable ledger or any canonical record."
)


_SERVERS_SCOPE = (
    "every memory server this account runs for ANY engine checkout on this machine (a shared activation "
    "serves every worktree of one repository); `same_repository` marks the ones under THIS project's git "
    "directory, None when the argv does not say — an attribution INFERRED from each process's self-reported "
    "command line, not a verified property of the process."
)
_VENV_PYTHON = f"{os.sep}.engine{os.sep}.venv{os.sep}bin{os.sep}"


def _git_common_dir(root: str | None = None) -> str | None:
    """The resolved git common directory of `root` (the project root by default), or None."""
    root = _project_root() if root is None else root
    try:
        done = subprocess.run(["git", "-C", root, "rev-parse", "--git-common-dir"],
                              capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    common = done.stdout.strip()
    return os.path.realpath(common if os.path.isabs(common) else os.path.join(root, common))


def _same_repository(argv: list[str], tree_path: str | None, ours: str | None, cache: dict) -> bool | None:
    """Whether one server's argv places it under THIS project's git directory. An accepted tree lives at
    `<common>/engine/accepted-hooks/trees/<name>`, so its path answers directly; a live-checkout launch runs
    the checkout's own interpreter (`<root>/.engine/.venv/bin/...`), so that root is asked for its common
    directory once per distinct root. None when the argv carries neither shape or git could not answer."""
    if ours is None:
        return None
    if tree_path:
        return os.path.commonpath((os.path.realpath(tree_path), ours)) == ours
    for token in argv:
        at = token.find(_VENV_PYTHON)
        if at > 0:
            root = token[:at]
            if root not in cache:
                if len(cache) >= _MAX_ROOT_PROBES:
                    return None   # bounded: a machine strewn with stray servers cannot make a baseline slow
                cache[root] = _git_common_dir(root)
            return None if cache[root] is None else cache[root] == ours
    return None


def _live_servers() -> list[dict]:
    """This engine's memory-server processes owned by THIS account: an argv that runs the accepted-hook
    dispatcher for the memory server (the `uv run` wrapper that merely spawns it is not counted twice).
    Records the launcher, whether the server sits under this project's git directory, and — when the
    `--tree` argument names a materialized accepted tree — its `<commit>-<tree>` name; never the argv itself,
    which carries paths and whatever else a command line holds."""
    try:
        done = subprocess.run(["ps", "-axo", "pid=,uid=,args="], capture_output=True, text=True,
                              timeout=_SUBPROCESS_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    mine = str(os.getuid())
    ours = _git_common_dir()
    roots: dict = {}
    found = []
    for line in done.stdout.splitlines():
        parts = line.split()
        if len(parts) < 3 or not parts[0].isdigit() or parts[1] != mine:
            continue
        argv = parts[2:]
        if os.path.basename(argv[0]) == "uv":
            continue   # the launcher's wrapper process; its child is the server
        if not any(token.endswith("accepted_hook_dispatch.py") for token in argv):
            continue
        if "attended-memory-mcp" not in argv and not any(token.endswith("memory/mcp_server.py") for token in argv):
            continue
        tree, tree_path = None, None
        if "--tree" in argv:
            candidate = argv[argv.index("--tree") + 1:][:1]
            tree_path = candidate[0] if candidate else None
            name = os.path.basename(tree_path) if tree_path else ""
            tree = name if _TREE_NAME.fullmatch(name) else None
        found.append({"pid": int(parts[0]), "launcher": "accepted-tree" if tree else "live-checkout",
                      "code_version": tree, "same_repository": _same_repository(argv, tree_path, ours, roots)})
    return found


def _activation_on_disk() -> dict | None:
    common = _git_common_dir()
    if common is None:
        return None
    activation = os.path.join(common, "engine", "accepted-hooks", "activation.json")
    try:
        with open(activation, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return None
    return _activation_facts(value) if isinstance(value, dict) else None


def _main_worktree() -> str:
    """The repository's main checkout — where the canonical memory store lives — even when this runs from a
    linked worktree (a build lane), which has no store of its own. The project root when git cannot say."""
    try:
        done = subprocess.run(["git", "-C", _project_root(), "worktree", "list", "--porcelain"],
                              capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return _project_root()
    first = done.stdout.split("\n\n", 1)[0] if done.returncode == 0 else ""
    for line in first.splitlines():
        if line.startswith("worktree "):
            return line[len("worktree "):]
    return _project_root()


def _lifecycle_state() -> dict:
    memory_dir = os.environ.get("ENGINE_MEMORY_DIR") or os.path.join(_main_worktree(), ".engine", "memory")
    state = {"memory_dir_present": os.path.isdir(memory_dir), "files": {}}
    for name in ("index.sqlite3", "vectors.sqlite3"):
        candidate = os.path.join(memory_dir, name)
        try:
            state["files"][name] = {"present": True, "bytes": os.stat(candidate).st_size}
        except OSError:
            state["files"][name] = {"present": False, "bytes": 0}
    return state


def _observed_error(text) -> dict:
    """What the client showed, classified rather than copied: the text is kept ONLY when it is the generic,
    content-free boundary string ("Error executing tool <name>"); anything else — a refusal sentence, a
    pasted message — is summarized by its length alone, because free text is exactly what this log does not
    write."""
    value = text if isinstance(text, str) else ""
    generic = bool(_GENERIC_TOOL_ERROR.fullmatch(value.strip()))
    return {"generic": generic, "text": value.strip() if generic else None,
            "length": min(len(value), _MAX_OBSERVED_ERROR * 10)}


def capture_baseline(observed_error: str, *, tool: str | None = None, query_kind: str = "innocuous",
                     path: str | None = None) -> dict | None:
    """Record the VISIBLE failure of the live server as a baseline: what the client showed (classified), this
    account's running memory servers and the code version each loaded, the activation on disk, and the
    derived caches' presence.

    This is Stage-0 of C1 and it is honest about its limits: it observes what the process and the disk show,
    it never claims to know the hidden exception, and it writes nothing durable (see `_CACHE_EFFECTS`).
    Returns the sanitized form, or None when nothing was recorded."""
    try:
        record = {"schema_version": SCHEMA_VERSION, "ts": time.time(), "event": Event.BASELINE.value}
        record.update(_runtime_facts())
        record.update({
            "observed_error": _observed_error(observed_error),
            "tool": _safe_tool(tool),
            "query_kind": query_kind if query_kind in ("innocuous", "production") else "innocuous",
            "servers": _live_servers(),
            "servers_scope": _SERVERS_SCOPE,
            "activation_on_disk": _activation_on_disk(),
            "lifecycle": _lifecycle_state(),
            "cache_effects": _CACHE_EFFECTS,
        })
        if not _append(_encode(record), path=path):
            return None
        return sanitize(record)
    except Exception:  # noqa: BLE001 — same boundary as record_stranding
        return None


# ---- CLI --------------------------------------------------------------------------------------------------

def main(argv) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="stranding_log.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="verb", required=True)
    sub.add_parser("readiness", help="say whether the instrument can record right now (writes nothing)")
    export = sub.add_parser("export", help="print the sanitized records; --to writes them under the engine cache")
    export.add_argument("--to", default=None, help=(
        "write the export to this file instead of printing it. Refused unless the path resolves under the "
        "engine's cache directory (.engine/telemetry/.cache) AND git reports it ignored, and the write needs a "
        "session qualified to write memory; without --to the export is printed and needs no write authority."))
    baseline = sub.add_parser("baseline", help="record a Stage-0 baseline of the visible failure")
    baseline.add_argument("--observed-error", required=True, help="the error text the client showed")
    baseline.add_argument("--tool", default=None)
    baseline.add_argument("--query-kind", choices=("innocuous", "production"), default="innocuous")
    args = parser.parse_args(argv)
    if args.verb == "readiness":
        print(json.dumps(readiness(check_ignore=True), indent=2, sort_keys=True))
        return 0
    if args.verb == "export":
        try:
            records = export_sanitized(args.to)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        except _mutation_authority.MutationAuthorityError as exc:
            print(f"{exc}\nOmit --to and redirect the printed output yourself: a plain `export` needs no "
                  "write authority, so it works from a session that is not (yet) qualified.", file=sys.stderr)
            return 2
        if args.to is None:
            for item in records:
                print(_encode(item))
        else:
            print(f"exported {len(records)} sanitized record(s) to {args.to}")
        return 0
    result = capture_baseline(args.observed_error, tool=args.tool, query_kind=args.query_kind)
    if result is None:
        print("baseline not recorded (the sink could not be written)", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


_mutation_authority.install_module_guards(globals())

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
