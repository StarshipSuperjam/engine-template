#!/usr/bin/env python3
"""Memory-backup pointer public-safety guard (#224) — the thin custom/script entry for
engine/check/memory-pointer-public-safety.

The committed memory-backup pointer (.engine/memory-backup/pointer.json) carries the vault's COORDINATES
(owner/repo/namespace). In a DEPLOYED project committing them is the operator's own choice — topology-law-5's
config-not-data carve-out, and the saved-memory read is legitimate on a public repo (only the digest CONTENT is
gated, never the pointer). But the PUBLIC engine-template CONSTRUCTION repo must always ship the unconfigured
PLACEHOLDER, so a maintainer's real vault coordinates can never travel to everyone who uses the template. This
backstop catches an accidental CONFIGURED pointer committed to the construction repo.

It is CONSTRUCTION-SCOPED: it acts only when the root CLAUDE.md is the construction-governance file (superseded
at v1), so it no-ops in any generated/deployed repo — never flagging the operator's own legitimate choice. It
reads the COMMITTED pointer via `git show HEAD:` (not the working tree), so the maintainer's local
skip-worktree-configured pointer — which is never committed — is correctly ignored and a local floor run stays
green.

It reads local committed state only — no network, no token. It emits finding.v1 JSON on stdout (the
custom/script machine channel) and returns 0: an empty array when safe (placeholder, or not the construction
repo, or the committed state can't be determined), one `hard` finding when a CONFIGURED pointer is committed in
the construction repo. Best-effort: an unreadable HEAD / CLAUDE.md degrades to a pass — a backstop never
false-fails a build over a condition it can't read.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402

POINTER_REL = ".engine/memory-backup/pointer.json"
_CONSTRUCTION_MARKER = "construction governance"   # the root CLAUDE.md genesis header (superseded at v1)


def _is_construction_repo() -> bool:
    try:
        with open(os.path.join(validate.ROOT, "CLAUDE.md"), encoding="utf-8") as fh:
            return _CONSTRUCTION_MARKER in fh.read().lower()
    except Exception:  # noqa: BLE001 — no/unreadable CLAUDE.md -> treat as not-construction (no-op)
        return False


def _committed_pointer_text() -> "str | None":
    try:
        out = subprocess.run(["git", "show", f"HEAD:{POINTER_REL}"], cwd=validate.ROOT,
                             capture_output=True, text=True, timeout=10)
    except Exception:  # noqa: BLE001 — git absent / detached HEAD / timeout -> can't determine -> pass (backstop)
        return None
    return out.stdout if out.returncode == 0 else None


def is_configured_pointer(text: str) -> bool:
    """Configured == it carries vault coordinates; the placeholder is {"schema_version": 1, "configured": false}."""
    try:
        p = json.loads(text)
    except Exception:  # noqa: BLE001 — an unparseable committed pointer is a schema concern, not this one's
        return False
    return isinstance(p, dict) and all(isinstance(p.get(k), str) and p.get(k)
                                       for k in ("owner", "repo", "namespace"))


def check() -> "dict | None":
    """The guard result: a `hard` finding when a configured pointer is committed in the construction repo, else
    None (safe / not applicable). Separated from main() so a test can drive it directly."""
    if not _is_construction_repo():
        return None
    text = _committed_pointer_text()
    if text is None or not is_configured_pointer(text):
        return None
    return validate.finding(
        "hard",
        "The committed memory-backup pointer in this public template carries real vault coordinates instead of "
        'the unconfigured placeholder. Restore the placeholder ({"schema_version": 1, "configured": false}) so a '
        "maintainer's private vault location never ships to everyone who uses the template, and keep your real "
        f"pointer local with `git update-index --skip-worktree {POINTER_REL}`.",
        {"file": POINTER_REL, "line": None})


def main() -> int:
    f = check()
    print(json.dumps([f] if f is not None else []))
    return 0


if __name__ == "__main__":
    sys.exit(main())
