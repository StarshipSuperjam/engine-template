"""Bounded serial unittest observations. Outcome accounting is mandatory; timing is advisory.

The child owns discovery and result callbacks. The parent validates its final document and
adds process completion; the progress pipe is deliberately not an evidence channel.
No document here is a merge receipt. No traceback or environment dump is serialized.
"""
from __future__ import annotations

from collections import Counter, defaultdict, deque
from contextlib import contextmanager
import functools
import json
import math
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import tempfile
import time
import unittest

MAX_BYTES = 64 * 1024 * 1024
MAX_CASES = 200_000
MAX_STRING = 4096
OUTCOMES = {"passed", "failed", "error", "expected-failure", "unexpected-success",
            "skipped", "fixture-blocked", "unexecuted"}
FAILURES = {"failed", "error", "unexpected-success", "fixture-blocked"}


def text(value):
    """Bound required strings before publication; redact absolute paths and terminal controls."""
    value = str(value)
    if len(value.encode("utf-8")) > MAX_STRING:
        raise ValueError("required outcome string exceeds limit")
    value = re.sub(r"(?:[A-Za-z]:[\\/]|/)(?:[^\s<>\"']+)", "[path]", value)
    return "".join(c if c in "\n\t" or ord(c) >= 32 else "?" for c in value)


def write(path, value):
    data = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    if len(data) > MAX_BYTES:
        raise ValueError("artifact exceeds byte limit")
    parent = Path(path).parent
    fd, temp = tempfile.mkstemp(prefix=".selftest-", dir=parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def read(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError("artifact is not a bounded regular file")
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("artifact exceeds byte limit")
    return json.loads(data, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))


@functools.lru_cache(maxsize=2)
def _validator(version):
    # Construct once per artifact kind, never per case and never check the metaschema per validation.
    import jsonschema
    schema = json.loads((Path(__file__).resolve().parents[1] / "schemas" / (version + ".json")).read_text())
    return jsonschema.Draft202012Validator(schema)


def validate_shape(document, version):
    if next(_validator(version).iter_errors(document), None) is not None:
        raise ValueError("artifact does not satisfy its versioned schema")


def identities(cases):
    counts = Counter()
    records = []
    for case in cases:
        if len(records) >= MAX_CASES:
            raise ValueError("inventory exceeds case limit")
        name = text(case.id())
        counts[name] += 1
        records.append({"id": name, "occurrence": counts[name]})
    return records


class Observation:
    def __init__(self, inventory, selected, *, source, scope, invocation, timing=False):
        self.inventory = identities(inventory)
        self.cases = []
        self.objects = []
        self.waiting = defaultdict(deque)
        self.active = {}
        self.cursor = 0
        self.fixtures = []
        self.issues = []
        self.timing = timing
        self.spans = []
        self._phase_start = self._phase_stop = None
        self.origin = time.monotonic()
        self.metadata = {"source": source, "scope": scope, "invocation": invocation}
        self.metadata["ci"] = {"run_id": os.environ.get("GITHUB_RUN_ID"),
                               "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
                               "head": os.environ.get("GITHUB_SHA"), "route": os.environ.get("GITHUB_EVENT_NAME")}
        available = defaultdict(deque)
        for case, identity in zip(inventory, self.inventory):
            available[id(case)].append(identity)
        for case in selected:
            if not available[id(case)]:
                raise ValueError("selected object is not in discovered inventory")
            identity = available[id(case)].popleft()
            index = len(self.cases)
            self.objects.append(id(case))
            self.cases.append({**identity, "module": text(type(case).__module__),
                               "class": text(type(case).__module__ + "." + type(case).__qualname__),
                               "outcome": "unexecuted", "started": False, "stopped": False,
                               "reason": None, "subtests": [], "seconds": None})
            self.waiting[id(case)].append(index)
        self.selected = [{"id": r["id"], "occurrence": r["occurrence"]} for r in self.cases]

    def issue(self, reason):
        if len(self.issues) < 100:
            self.issues.append(text(reason))

    def start(self, case):
        queue = self.waiting[id(case)]
        if id(case) in self.active or not queue:
            self.issue("unexpected or duplicate test start")
            return
        index = queue.popleft()
        self.cursor = max(self.cursor, index + 1)
        self.active[id(case)] = (index, time.monotonic())
        self.cases[index]["started"] = True
        if self._phase_start:
            self._phase_start(case)

    def stop(self, case):
        if self._phase_stop:
            self._phase_stop(case)
        active = self.active.pop(id(case), None)
        if active is None:
            self.issue("unexpected test stop")
            return
        index, began = active
        self.cases[index]["stopped"] = True
        if self.timing:
            self.cases[index]["seconds"] = time.monotonic() - began

    def outcome(self, case, kind, reason=None, cleanup=False):
        active = self.active.get(id(case))
        if active is None:
            parent = getattr(case, "test_case", None)
            if kind == "skipped" and parent is not None and id(parent) in self.active:
                row = self.cases[self.active[id(parent)][0]]
                if len(row["subtests"]) >= 1000:
                    self.issue("subtest limit exceeded")
                else:
                    row["subtests"].append({"id": text(case.id()), "outcome": "skipped", "reason": text(reason)})
                return
            self.fixture(case, kind, reason, cleanup)
            return
        row = self.cases[active[0]]
        if row["outcome"] != "unexecuted":
            # unittest can report several errors for one case (body plus cleanup).
            if kind == "error" and row["outcome"] in {"error", "failed"}:
                row["outcome"] = "error"
                return
            self.issue("duplicate terminal outcome")
            return
        row["outcome"], row["reason"] = kind, text(reason) if reason is not None else None

    def subtest(self, case, subtest, err):
        active = self.active.get(id(case))
        if active is None:
            self.issue("subtest has no selected active parent")
            return
        row = self.cases[active[0]]
        if len(row["subtests"]) >= 1000:
            self.issue("subtest limit exceeded")
            return
        kind = "passed" if err is None else ("failed" if issubclass(err[0], case.failureException) else "error")
        row["subtests"].append({"id": text(subtest.id()), "outcome": kind})
        if err is not None:
            row["outcome"] = "error" if kind == "error" or row["outcome"] == "error" else "failed"

    def fixture(self, holder, kind, reason, cleanup=False):
        description = getattr(holder, "description", "")
        match = re.fullmatch(r"(setUpModule|tearDownModule|setUpClass|tearDownClass) \((.+)\)", description)
        if not match or kind not in {"error", "skipped"}:
            self.issue("outcome outside selected inventory or known fixture")
            return
        phase, owner = match.groups()
        key = "module" if phase.endswith("Module") else "class"
        # unittest fixtures cover contiguous suite groups; the same class/module may recur later.
        # Never turn a failure in one group into invented skips for a later independent group.
        if phase.startswith("setUp"):
            affected = []
            for i in range(self.cursor, len(self.cases)):
                if self.cases[i][key] != owner:
                    break
                affected.append(i)
            if affected:
                self.cursor = affected[-1] + 1
        else:
            affected = []
            for i in range(self.cursor - 1, -1, -1):
                if self.cases[i][key] != owner:
                    break
                affected.append(i)
            affected.reverse()
        if not affected and self.fixtures and self.fixtures[-1]["phase"] == phase and self.fixtures[-1]["owner"] == owner:
            affected = self.fixtures[-1]["affected"][:]
        if not affected:
            self.issue("fixture outcome has no selected cases")
        if len(self.fixtures) >= MAX_CASES:
            self.issue("fixture outcome limit exceeded")
            return
        self.fixtures.append({"phase": phase, "owner": text(owner), "outcome": kind,
                              "cleanup": cleanup,
                              "reason": text(reason) if reason is not None else None,
                              "affected": affected})
        if phase.startswith("setUp"):
            for i in affected:
                row = self.cases[i]
                if not row["started"] and (row["outcome"] == "unexecuted" or kind == "error"):
                    if i in self.waiting[self.objects[i]]:
                        self.waiting[self.objects[i]].remove(i)
                    row["outcome"] = "skipped" if kind == "skipped" else "fixture-blocked"
                    row["reason"] = text(reason) if reason is not None else None

    def document(self, finalized=False):
        complete = (finalized and not self.issues and not self.active and all(
            r["outcome"] != "unexecuted" and (not r["started"] or r["stopped"]) for r in self.cases))
        passed = complete and not any(r["outcome"] in FAILURES for r in self.cases) and not any(
            f["outcome"] == "error" for f in self.fixtures)
        return {"schema_version": "selftest-results.v1", **self.metadata, "inventory": self.inventory,
                "selected": self.selected, "cases": self.cases, "fixtures": self.fixtures, "issues": self.issues,
                "child_finalized": finalized, "complete": bool(complete), "passed": bool(passed),
                "process_exit": None, "executed_count": sum(r["started"] for r in self.cases)}

    def performance(self, collection_seconds, elapsed):
        return {"schema_version": "selftest-performance.v1", **self.metadata,
                "environment": environment(), "collection_seconds": collection_seconds,
                "child_seconds": elapsed, "parent_seconds": None,
                "case_seconds": sum(r["seconds"] or 0 for r in self.cases),
                "cases": [{"id": r["id"], "occurrence": r["occurrence"], "seconds": r["seconds"]}
                          for r in self.cases],
                "spans": self.spans, "timing_semantics": "inclusive; nested spans must not be summed",
                "unallocated_seconds": max(0, elapsed - collection_seconds - sum(r["seconds"] or 0 for r in self.cases)
                                           - interval_union([(s["start"], s["start"]+s["seconds"])
                                                             for s in self.spans if s["level"] == "fixture"])),
                "unknown": ["custom run implementations may bypass phase hooks", "cache state is not inferred"]}

    @contextmanager
    def phases(self, selected, result_ref):
        """Observe existing hooks without replacing suite order or fixture ownership."""
        del selected
        restores = []
        active_restores = {}
        def wrap(obj, name, level, owner, target, guard=lambda args: True):
            if not hasattr(obj, name):
                return
            original = getattr(obj, name)
            had = name in vars(obj)
            prior = vars(obj).get(name)
            @functools.wraps(original)
            def observed(*args, **kwargs):
                if not guard(args):
                    return original(*args, **kwargs)
                start = time.monotonic()
                try:
                    return original(*args, **kwargs)
                finally:
                    if len(self.spans) < MAX_CASES * 8:
                        self.spans.append({"phase": name, "level": level, "owner": owner,
                                           "start": start - self.origin, "seconds": time.monotonic() - start})
            try:
                setattr(obj, name, observed)
            except (AttributeError, TypeError):
                return  # Unsupported timing hook stays unknown; it cannot fail the test run.
            target.append((obj, name, had, prior))
        def restore(entries):
            for obj, name, had, prior in reversed(entries):
                if had:
                    setattr(obj, name, prior)
                else:
                    delattr(obj, name)
        def start(case):
            entries = active_restores.setdefault(id(case), [])
            for name in ("_callSetUp", "_callTestMethod", "_callTearDown", "_callCleanup"):
                wrap(case, name, "case", text(case.id()), entries)
        def stop(case):
            restore(active_restores.pop(id(case), []))
        try:
            if self.timing:
                self._phase_start, self._phase_stop = start, stop
                for name in ("_handleModuleFixture", "_handleClassSetUp", "_tearDownPreviousClass", "_handleModuleTearDown"):
                    wrap(unittest.TestSuite, name, "fixture", "unittest.TestSuite", restores,
                         lambda args: bool(args) and args[-1] is result_ref[0])
            yield
        finally:
            self._phase_start = self._phase_stop = None
            for entries in active_restores.values():
                restore(entries)
            restore(restores)


def environment():
    try:
        uv = subprocess.run(["uv", "--version"], capture_output=True, text=True, timeout=5)
        uv_version = text(uv.stdout.strip()) if uv.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        uv_version = None
    try:
        memory = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        memory = None
    try:
        topology = subprocess.run(["git", "worktree", "list", "--porcelain"],
                                  capture_output=True, text=True, timeout=5)
        worktrees = sum(line.startswith("worktree ") for line in topology.stdout.splitlines()) if topology.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        worktrees = None
    return {"os": platform.system(), "release": platform.release(), "architecture": platform.machine(),
            "python": platform.python_version(), "cpu_count": os.cpu_count(),
            "runner_os": os.environ.get("RUNNER_OS"), "runner_arch": os.environ.get("RUNNER_ARCH"),
            "uv": uv_version, "memory_bytes": memory, "worktree_count": worktrees, "cache": "unknown"}


def interval_union(intervals):
    """Duration of observed overlapping intervals, never a sum of nested spans."""
    total = 0.0
    end = None
    for start, stop in sorted(intervals):
        if stop < start:
            raise ValueError("negative interval")
        total += max(0, stop - max(start, end)) if end is not None else stop - start
        end = max(end, stop) if end is not None else stop
    return total


def validate(document):
    """Recompute completeness rather than accepting a producer's green boolean."""
    if not isinstance(document, dict) or document.get("schema_version") != "selftest-results.v1":
        raise ValueError("unsupported result document")
    validate_shape(document, "selftest-results.v1")
    inventory, cases = document["inventory"], document["cases"]
    if document["scope"] not in {"full", "focused", "project-only"}:
        raise ValueError("unknown selection scope")
    if not isinstance(inventory, list) or not isinstance(cases, list) or max(len(inventory), len(cases)) > MAX_CASES:
        raise ValueError("invalid inventory")
    def key(row):
        if not isinstance(row, dict) or not isinstance(row["id"], str) or text(row["id"]) != row["id"]:
            raise ValueError("invalid identity")
        if type(row["occurrence"]) is not int or row["occurrence"] < 1:
            raise ValueError("invalid occurrence")
        return row["id"], row["occurrence"]
    ikeys, ckeys = [key(r) for r in inventory], [key(r) for r in cases]
    selected = [key(r) for r in document["selected"]]
    if selected != ckeys:
        raise ValueError("reports differ from selected inventory")
    if len(set(ikeys)) != len(ikeys) or len(set(ckeys)) != len(ckeys) or not set(ckeys) <= set(ikeys):
        raise ValueError("duplicate or unexpected identity")
    if document["scope"] == "full" and set(ikeys) != set(ckeys):
        raise ValueError("full selection omits inventory cases")
    mapped = {}
    fixtures = document["fixtures"]
    for fixture in fixtures:
        if fixture["outcome"] not in {"error", "skipped"}:
            raise ValueError("invalid fixture outcome")
        for i in fixture["affected"]:
            if type(i) is not int or not 0 <= i < len(cases):
                raise ValueError("invalid fixture mapping")
            if fixture["phase"].startswith("setUp"):
                key_name = "module" if fixture["phase"].endswith("Module") else "class"
                if cases[i][key_name] != fixture["owner"]:
                    raise ValueError("fixture owner does not match affected case")
                if i not in mapped or fixture["outcome"] == "error":
                    mapped[i] = fixture
    for i, row in enumerate(cases):
        if row["outcome"] not in OUTCOMES or type(row["started"]) is not bool or type(row["stopped"]) is not bool:
            raise ValueError("invalid case outcome")
        if row["reason"] is not None and text(row["reason"]) != row["reason"]:
            raise ValueError("invalid reason")
        if row["outcome"] == "skipped" and row["reason"] is None:
            raise ValueError("skip reason absent")
        if row["outcome"] != "unexecuted" and not row["started"] and i not in mapped:
            raise ValueError("unobserved case outcome")
        if not row["started"] and i in mapped:
            expected = "skipped" if mapped[i]["outcome"] == "skipped" else "fixture-blocked"
            if row["outcome"] != expected or row["reason"] != mapped[i]["reason"]:
                raise ValueError("case outcome disagrees with fixture")
        seconds = row["seconds"]
        if seconds is not None and (type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0):
            raise ValueError("invalid duration")
    complete = bool(document["child_finalized"] and not document["issues"] and all(
        r["outcome"] != "unexecuted" and (not r["started"] or r["stopped"]) for r in cases))
    if document["executed_count"] != sum(r["started"] for r in cases):
        raise ValueError("executed count disagrees with observed starts")
    passed = complete and document["process_exit"] in (None, 0) and not any(r["outcome"] in FAILURES for r in cases) and not any(f["outcome"] == "error" for f in fixtures)
    if document["complete"] != complete or document["passed"] != passed:
        raise ValueError("claimed completeness or verdict disagrees with observations")
    return complete, passed


def finalize(path, exit_status):
    try:
        document = read(path)
        complete, passed = validate(document)
        document["process_exit"] = exit_status
        if exit_status != 0:
            document["passed"] = False
        write(path, document)
        return complete and passed and exit_status == 0
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return False
