"""Deterministic, plaintext, multipart representation for a memory ledger.

This module is deliberately a pure codec.  Backup owns publication and restore
owns selecting a namespace; both can use this v2 representation without making
network or filesystem policy part of the format.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import time
from typing import Iterable, Sequence

SNAPSHOT_FORMAT = 2
COMPRESSION = "gzip"
MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_COMPRESSED_BYTES = 128 * 1024 * 1024
MAX_PARTS = 32
PART_REQUEST_BYTES = 8 * 1024 * 1024
_COPY_CHUNK_BYTES = 64 * 1024

MANIFEST_KEYS = frozenset({
    "snapshot-format", "compression", "ledger-sha256", "ledger-bytes",
    "compressed-sha256", "compressed-bytes", "parts",
})
PART_KEYS = frozenset({"name", "bytes", "sha256"})


class SnapshotError(ValueError):
    """A deterministic refusal to encode or read a snapshot."""
    code = "snapshot-error"


class SnapshotLimitError(SnapshotError):
    code = "snapshot-limit"


class SnapshotValidationError(SnapshotError):
    code = "snapshot-invalid"


class SnapshotDeadlineError(SnapshotError):
    code = "snapshot-deadline"


def _refuse(kind, message: str):
    raise kind(message)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _check_deadline(deadline, clock=None) -> None:
    if deadline is not None and (time.monotonic() if clock is None else clock()) >= deadline:
        _refuse(SnapshotDeadlineError, "snapshot deadline expired")


def part_name(index: int) -> str:
    """The one canonical, lexical-order-safe name for a zero-based part index."""
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < MAX_PARTS:
        _refuse(SnapshotLimitError, "part index is outside the canonical range")
    return f"part-{index + 1:02d}.gz"


def blob_request(part: bytes) -> bytes:
    """The exact JSON body used by Git's create-blob request for ``part``."""
    if not isinstance(part, bytes):
        _refuse(SnapshotValidationError, "part bytes are required")
    body = {"content": base64.b64encode(part).decode("ascii"), "encoding": "base64"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def encoded_request_size(part: bytes) -> int:
    """Measure the fully serialized blob request, rather than raw/base64 bytes."""
    return len(blob_request(part))


def max_part_bytes(request_limit: int = PART_REQUEST_BYTES) -> int:
    """Largest raw part whose actual serialized request fits ``request_limit``."""
    if isinstance(request_limit, bool) or not isinstance(request_limit, int) or request_limit < encoded_request_size(b"x"):
        _refuse(SnapshotLimitError, "request limit cannot hold one byte")
    low, high = 1, request_limit
    while low < high:
        middle = (low + high + 1) // 2
        if encoded_request_size(b"x" * middle) <= request_limit:
            low = middle
        else:
            high = middle - 1
    return low


def _validate_limit(request_limit: int) -> int:
    return max_part_bytes(request_limit)


def _is_digest(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _is_count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate_manifest(manifest, *, request_limit: int) -> list[dict]:
    """Validate all attacker-controlled layout facts before any decompression."""
    raw_limit = _validate_limit(request_limit)
    if not isinstance(manifest, dict) or set(manifest) != MANIFEST_KEYS:
        _refuse(SnapshotValidationError, "manifest shape is not v2")
    if manifest["snapshot-format"] != SNAPSHOT_FORMAT or manifest["compression"] != COMPRESSION:
        _refuse(SnapshotValidationError, "unsupported snapshot format or compression")
    if not _is_count(manifest["ledger-bytes"]) or manifest["ledger-bytes"] > MAX_UNCOMPRESSED_BYTES:
        _refuse(SnapshotLimitError, "ledger size exceeds the v2 limit")
    if not _is_count(manifest["compressed-bytes"]) or manifest["compressed-bytes"] > MAX_COMPRESSED_BYTES:
        _refuse(SnapshotLimitError, "compressed size exceeds the v2 limit")
    if not _is_digest(manifest["ledger-sha256"]) or not _is_digest(manifest["compressed-sha256"]):
        _refuse(SnapshotValidationError, "manifest digest is malformed")
    parts = manifest["parts"]
    if not isinstance(parts, list) or not 1 <= len(parts) <= MAX_PARTS:
        _refuse(SnapshotLimitError, "part count is outside the v2 limit")
    total = 0
    for index, entry in enumerate(parts):
        if not isinstance(entry, dict) or set(entry) != PART_KEYS:
            _refuse(SnapshotValidationError, "part manifest shape is invalid")
        if entry["name"] != part_name(index) or not _is_count(entry["bytes"]) or not _is_digest(entry["sha256"]):
            _refuse(SnapshotValidationError, "part manifest is non-canonical")
        if entry["bytes"] > raw_limit:
            _refuse(SnapshotLimitError, "part exceeds serialized request limit")
        total += entry["bytes"]
        if total > MAX_COMPRESSED_BYTES:
            _refuse(SnapshotLimitError, "parts exceed compressed size limit")
    if total != manifest["compressed-bytes"]:
        _refuse(SnapshotValidationError, "part sizes do not match compressed size")
    return parts


def encode_snapshot(ledger_bytes: bytes, *, request_limit: int = PART_REQUEST_BYTES,
                    deadline=None, clock=None) -> dict:
    """Return deterministic ``{'manifest': ..., 'parts': [...]}`` for ledger bytes.

    The encoder keeps one compressed buffer while splitting it.  The decoder
    deliberately does not reassemble parts: it streams them through gzip in
    bounded reads and refuses output over the advertised v1 ceiling.
    """
    if not isinstance(ledger_bytes, bytes):
        _refuse(SnapshotValidationError, "ledger bytes are required")
    if len(ledger_bytes) > MAX_UNCOMPRESSED_BYTES:
        _refuse(SnapshotLimitError, "ledger size exceeds the v2 limit")
    _check_deadline(deadline, clock)
    raw_limit = _validate_limit(request_limit)
    compressed_file = io.BytesIO()
    try:
        with gzip.GzipFile(fileobj=compressed_file, mode="wb", compresslevel=6, mtime=0) as target:
            for offset in range(0, len(ledger_bytes), _COPY_CHUNK_BYTES):
                _check_deadline(deadline, clock)
                target.write(ledger_bytes[offset:offset + _COPY_CHUNK_BYTES])
                _check_deadline(deadline, clock)
    except SnapshotError:
        raise
    compressed = compressed_file.getvalue()
    _check_deadline(deadline, clock)
    if len(compressed) > MAX_COMPRESSED_BYTES:
        _refuse(SnapshotLimitError, "compressed size exceeds the v2 limit")
    count = max(1, (len(compressed) + raw_limit - 1) // raw_limit)
    if count > MAX_PARTS:
        _refuse(SnapshotLimitError, "snapshot needs more than 32 parts")
    parts = [compressed[offset:offset + raw_limit] for offset in range(0, len(compressed), raw_limit)] or [b""]
    # An empty gzip member is nonempty, but retain this guard if gzip ever changes.
    if len(parts) > MAX_PARTS:
        _refuse(SnapshotLimitError, "snapshot needs more than 32 parts")
    manifest = {
        "snapshot-format": SNAPSHOT_FORMAT,
        "compression": COMPRESSION,
        "ledger-sha256": _sha256(ledger_bytes),
        "ledger-bytes": len(ledger_bytes),
        "compressed-sha256": _sha256(compressed),
        "compressed-bytes": len(compressed),
        "parts": [{"name": part_name(i), "bytes": len(part), "sha256": _sha256(part)} for i, part in enumerate(parts)],
    }
    # Exercise the same exact serialized-body assertion used at an upload boundary.
    _validate_manifest(manifest, request_limit=request_limit)
    return {"manifest": manifest, "parts": parts}


class _PartReader(io.RawIOBase):
    """A non-joining reader over already-validated parts, with bounded reads."""
    def __init__(self, parts: Sequence[bytes]):
        self.parts, self.index, self.offset = parts, 0, 0
        self.max_read_request = 0
        self.max_chunk_returned = 0

    def readable(self):
        return True

    def readinto(self, target):
        self.max_read_request = max(self.max_read_request, len(target))
        while self.index < len(self.parts):
            part = self.parts[self.index]
            remaining = len(part) - self.offset
            if remaining:
                count = min(len(target), remaining, _COPY_CHUNK_BYTES)
                target[:count] = part[self.offset:self.offset + count]
                self.offset += count
                self.max_chunk_returned = max(self.max_chunk_returned, count)
                return count
            self.index, self.offset = self.index + 1, 0
        return 0


def decode_snapshot(manifest, parts: Iterable[bytes], *, request_limit: int = PART_REQUEST_BYTES,
                    deadline=None, clock=None) -> bytes:
    """Strictly validate and decode a v1 snapshot without joining its parts."""
    entries = _validate_manifest(manifest, request_limit=request_limit)
    # Do not turn an untrusted iterable into an unbounded tuple: a caller can
    # offer at most the manifest's <=32 parts, plus one sentinel to prove it
    # did not offer more.
    iterator = iter(parts)
    supplied_list = []
    try:
        for _entry in entries:
            supplied_list.append(next(iterator))
    except StopIteration:
        _refuse(SnapshotValidationError, "supplied part count does not match manifest")
    try:
        next(iterator)
    except StopIteration:
        pass
    else:
        _refuse(SnapshotValidationError, "supplied part count does not match manifest")
    supplied = tuple(supplied_list)
    whole = hashlib.sha256()
    for entry, part in zip(entries, supplied):
        _check_deadline(deadline, clock)
        if not isinstance(part, bytes) or len(part) != entry["bytes"] or _sha256(part) != entry["sha256"]:
            _refuse(SnapshotValidationError, "part bytes do not match manifest")
        if encoded_request_size(part) > request_limit:
            _refuse(SnapshotLimitError, "part exceeds serialized request limit")
        whole.update(part)
    if whole.hexdigest() != manifest["compressed-sha256"]:
        _refuse(SnapshotValidationError, "compressed digest does not match manifest")
    reader = _PartReader(supplied)
    output = io.BytesIO()
    try:
        with gzip.GzipFile(fileobj=reader, mode="rb") as source:
            while True:
                _check_deadline(deadline, clock)
                chunk = source.read(_COPY_CHUNK_BYTES)
                if not chunk:
                    break
                if output.tell() + len(chunk) > MAX_UNCOMPRESSED_BYTES:
                    _refuse(SnapshotLimitError, "decompression exceeds the v2 limit")
                output.write(chunk)
                _check_deadline(deadline, clock)
    except SnapshotError:
        raise
    except (OSError, EOFError):
        _refuse(SnapshotValidationError, "gzip payload is invalid")
    ledger_bytes = output.getvalue()
    _check_deadline(deadline, clock)
    if len(ledger_bytes) != manifest["ledger-bytes"] or _sha256(ledger_bytes) != manifest["ledger-sha256"]:
        _refuse(SnapshotValidationError, "ledger digest does not match manifest")
    return ledger_bytes


# Short aliases make the pure transform convenient to use at future boundaries.
encode = encode_snapshot
decode = decode_snapshot
