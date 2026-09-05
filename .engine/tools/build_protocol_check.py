#!/usr/bin/env python3
"""build_protocol_check.py — the custom/script entry for engine/check/build-protocol.

Three conditions, each a hard finding, all fail-closed:

  1. `.engine/build-protocol.json` loads as build-protocol.v1 (the shared loader, build_protocol.load,
     validates it against the closed schema). A protocol that does not load would strand the Build
     Coordinator and the lens-consumption check at once, so it is refused at the merge.
  2. The Build runbook's generated review-consumers region matches what the protocol renders — the
     projection is data, not prose, and a hand-edited or stale copy would tell the next author a stage
     consumes a review it does not.
  3. No Markdown under `.engine/` carries the retired `consumed-review-lenses:` fenced record — the
     sentinel the lens-consumption check used to parse. A revived copy would look like the source of
     truth while nothing reads it (StarshipSuperjam/engine-template#821).

`ENGINE_BUILD_PROTOCOL_PATH` (unset in production) lets the negative-fixture meta-check point the loader
at a seeded off-schema protocol so this check is witnessed biting.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402
import build_protocol as bp  # noqa: E402
import agent_coherence_check  # noqa: E402  (emit — the one finding.v1 writer)

_SENTINEL_LINE = "consumed-review-lenses:"
_SCAN_DIRS = (".engine/operations", ".engine/policies", ".engine/docs", ".engine/templates")


def sentinel_hits(root: str | None = None) -> list:
    """(rel_path, line_number) for every Markdown line under the scanned dirs that starts the retired
    record. Fixtures under .engine/_fixtures are test data and are not scanned."""
    base = root or validate.ROOT
    hits = []
    for rel_dir in _SCAN_DIRS:
        top = os.path.join(base, rel_dir)
        for dirpath, _dirs, files in os.walk(top):
            for name in sorted(files):
                if not name.endswith(".md"):
                    continue
                path = os.path.join(dirpath, name)
                with open(path, encoding="utf-8", errors="replace") as fh:
                    for n, line in enumerate(fh, 1):
                        if line.strip().startswith(_SENTINEL_LINE):
                            hits.append((os.path.relpath(path, base), n))
    return hits


def findings(tier: str, root: str | None = None) -> list:
    out = []
    try:
        expected, actual = bp.projection_status(root)
    except bp.ProtocolError as exc:
        out.append(validate.finding(tier, f"{bp.PROTOCOL_REL} does not load as build-protocol.v1: {exc}. The Build "
                                    f"Coordinator and the lens-consumption check both read this file through one "
                                    f"loader, so a protocol that does not load blocks the merge rather than "
                                    f"stranding a Build later.", validate.loc(os.path.join(root or validate.ROOT, bp.PROTOCOL_REL))))
        return out
    runbook = os.path.join(root or validate.ROOT, bp.RUNBOOK_REL)
    if actual is None:
        out.append(validate.finding(tier, f"{bp.RUNBOOK_REL} carries no generated review-consumers region (the two "
                                    f"marker comments are missing or out of order); add the region once, then "
                                    f"regenerate it with `uv run --directory .engine --frozen -- python "
                                    f"tools/build_protocol.py render`.", validate.loc(runbook)))
    elif actual != expected:
        out.append(validate.finding(tier, f"the generated review-consumers region in {bp.RUNBOOK_REL} has drifted "
                                    f"from {bp.PROTOCOL_REL}: regenerate it with `uv run --directory .engine "
                                    f"--frozen -- python tools/build_protocol.py render` and commit the result.",
                                    validate.loc(runbook)))
    for rel, line in sentinel_hits(root):
        out.append(validate.finding(tier, f"'{rel}' line {line} carries a `{_SENTINEL_LINE}` record — the retired "
                                    f"Markdown form of the review-consumer map. Nothing reads it any more; the map "
                                    f"lives in {bp.PROTOCOL_REL} (review_consumers). Delete the block.",
                                    validate.loc(os.path.join(root or validate.ROOT, rel), line)))
    return out


def main(argv: list) -> int:
    tier = os.environ.get("ENGINE_RULE_TIER", "hard")
    return agent_coherence_check.emit(findings(tier))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
