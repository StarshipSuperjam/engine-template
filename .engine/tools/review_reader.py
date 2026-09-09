#!/usr/bin/env python3
"""Compact stdio MCP reader for reviews; no command or write tool is exposed.

Repository text is confined to the checkout containing this server. Outside it, only an exact
registered frozen packet or supplement in the canonical PlanLibrary is readable. The companion is
same-user provenance, not a boundary against an operator who can change the server or its records.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import sys

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import StrictInt

import plan_store

ROOT = Path(__file__).resolve().parents[2]
MAX_BYTES = 1024 * 1024
COMPANION_MAX_BYTES = 16 * MAX_BYTES
server = MCPServer("engine-review-reader")


def _bytes(path: Path, maximum: int) -> bytes:
    """Open every canonical path component without following links, then read a bounded regular file.

    Descriptor-relative traversal prevents swapping an ancestor for a symlink between checking and
    opening. O_NONBLOCK prevents a substituted FIFO from blocking before the regular-file check.
    """
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        leaf = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            before = os.fstat(leaf)
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("Only regular text files are readable")
            if before.st_size > maximum:
                raise ValueError(f"File exceeds {maximum} byte read limit")
            chunks, length = [], 0
            while length <= maximum:
                chunk = os.read(leaf, min(65536, maximum + 1 - length))
                if not chunk:
                    break
                chunks.append(chunk)
                length += len(chunk)
            after = os.fstat(leaf)
            if length > maximum:
                raise ValueError(f"File exceeds {maximum} byte read limit")
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError("File changed during read; retry")
            return b"".join(chunks)
        finally:
            os.close(leaf)
    finally:
        os.close(fd)


def _registered_digest(path: Path) -> str:
    library = plan_store.PlanLibrary(plan_store.library_root(cwd=str(ROOT)))
    relative = path.relative_to(library.root)
    if len(relative.parts) != 3 or relative.parts[1] != "scoped-packets":
        raise ValueError("External reads require an exact registered packet or supplement")
    companion = library.root / relative.parts[0] / "scoped-agent-evidence.v1.json"
    record = json.loads(_bytes(companion, COMPANION_MAX_BYTES))
    if not isinstance(record, dict) or record.get("schema_version") != "scoped-agent-evidence.v1":
        raise ValueError("Unsupported scoped assignment record")
    assignments = record.get("assignments")
    if not isinstance(assignments, dict):
        raise ValueError("Malformed scoped assignments")
    for assignment in assignments.values():
        if not isinstance(assignment, dict):
            raise ValueError("Malformed scoped assignment")
        if assignment.get("packet_path") == str(path):
            return assignment["file_digest"]
        supplements = assignment.get("supplements", [])
        if not isinstance(supplements, list):
            raise ValueError("Malformed scoped supplements")
        for supplement in supplements:
            if not isinstance(supplement, dict):
                raise ValueError("Malformed scoped supplement")
            if supplement.get("path") == str(path):
                return supplement["digest"]
    raise ValueError("External file is not a registered packet or supplement")


def _read_file(path: str, offset: int = 0, limit: int | None = None) -> dict:
    if type(offset) is not int or offset < 0:
        raise ValueError("offset must be a nonnegative integer line offset")
    if limit is not None and (type(limit) is not int or limit <= 0):
        raise ValueError("limit must be a positive integer line count")
    requested = Path(path)
    if not path or ".." in requested.parts:
        raise ValueError("Use a file path without parent traversal")
    if not requested.is_absolute():
        requested = ROOT / requested
    canonical = requested.resolve(strict=True)
    # Resolve before authorizing, then perform a no-follow descriptor walk of that canonical path.
    # A link to an external unregistered file therefore never confers repository read authority.
    expected = None if canonical.is_relative_to(ROOT) else _registered_digest(canonical)
    data = _bytes(canonical, MAX_BYTES)
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    if expected is not None and expected != digest:
        raise ValueError("Registered file digest changed")
    content = data.decode("utf-8")
    if any(ord(c) < 32 and c not in "\n\r\t" for c in content):
        raise ValueError("Binary content is not readable")
    lines = content.splitlines(keepends=True)
    if offset >= len(lines) and not (offset == 0 and not lines):
        raise ValueError("offset is outside the file line range")
    end = len(lines) if limit is None else min(len(lines), offset + limit)
    return {"file_path": str(canonical), "content": "".join(lines[offset:end]), "sha256": digest,
            "complete": offset == 0 and end == len(lines), "offset": offset}


@server.tool(name="read_file", annotations=ToolAnnotations(read_only_hint=True,
             open_world_hint=False, idempotent_hint=True), description="Read checkout text or a registered review packet/supplement (1 MiB max). Optional zero-based line offset and line limit; default whole file. No writes or commands.")
def read_file(path: str, offset: StrictInt = 0, limit: StrictInt | None = None) -> dict:
    try:
        return _read_file(path, offset, limit)
    except (OSError, ValueError, KeyError, TypeError, plan_store.PlanStoreError) as exc:
        raise ToolError(f"Read refused: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    argv = list(argv or [])
    usage = "usage: review_reader.py [--help]\nRead-only read_file MCP tool; bare invocation serves stdio."
    if "--help" in argv or "-h" in argv:
        print(usage)
        return 0
    if argv:
        print(usage, file=sys.stderr)
        return 2
    server.run()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
