#!/usr/bin/env python3
"""lane_removed_check.py — the custom/script entry for engine/check/lane-removed.

Typed-lifecycle part C (StarshipSuperjam/engine-template#821) removed the length-budget PROMOTION lane: the standing soft
length-budget nudge no longer exists to promote — the operation-shape rule's length tier now blocks at merge —
so the promoter (`audit_soft_promote.py`), its demo, its tests and its audit-prep workflow step are gone, and
the author-text neutraliser it carried lives in telemetry. This check keeps the lane gone: one hard finding for
every file under the scanned homes that still names the retired module or its workflow step — a revived import,
a re-added step, a stale census or manifest entry, a doc row that a regeneration missed. Git history is not
scanned; a reference there is history, not a revival.

`ENGINE_LANE_REMOVED_ROOT` (unset in production) points the scan at a seeded tree so the negative-fixture
meta-check can witness the check biting.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402
import agent_coherence_check  # noqa: E402  (emit — the one finding.v1 writer)

ENV_OVERRIDE = "ENGINE_LANE_REMOVED_ROOT"
# The retired lane's names. The module name is what an import, a census entry, a workflow step or a doc row
# would carry; the step title is what a re-added workflow step would carry even if it invoked a renamed script.
RETIRED_TOKENS = ("audit_soft_promote", "Track standing length-budget findings")
# Where a revival would land: tools (an import or a resurrected script), the workflows (a re-added step), the
# provisioning census and module manifests (a stale entry), and the prose homes (a doc row or a runbook pointer).
SCAN_HOMES = (
    (os.path.join(".engine", "tools"), (".py",)),
    (os.path.join(".github", "workflows"), (".yml", ".yaml")),
    (os.path.join(".engine", "provisioning"), (".json",)),
    (os.path.join(".engine", "modules"), (".json",)),
    (os.path.join(".engine", "docs"), (".md",)),
    (os.path.join(".engine", "policies"), (".md",)),
    (os.path.join(".engine", "operations"), (".md",)),
)
# This check and its own test name the tokens by necessity.
_OWN = {"lane_removed_check.py", "test_lane_removed_check.py"}


def references(root: str | None = None) -> list:
    """(rel_path, line, token) for every scanned file still naming the retired lane."""
    base = root or validate.ROOT
    hits = []
    for rel_dir, suffixes in SCAN_HOMES:
        top = os.path.join(base, rel_dir)
        if not os.path.isdir(top):
            continue
        for dirpath, dirs, files in os.walk(top):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))   # never the uv venv or a cache
            for name in sorted(files):
                if not name.endswith(suffixes) or name in _OWN:
                    continue
                path = os.path.join(dirpath, name)
                with open(path, encoding="utf-8", errors="replace") as fh:
                    for n, line in enumerate(fh, 1):
                        for token in RETIRED_TOKENS:
                            if token in line:
                                hits.append((os.path.relpath(path, base), n, token))
                                break
    return hits


def findings(tier: str, root: str | None = None) -> list:
    base = root or validate.ROOT
    return [validate.finding(tier, f"'{rel}' line {line} names the retired promotion lane (`{token}`): the "
                             f"length-budget promoter was removed by StarshipSuperjam/engine-template#821 — the budget now blocks "
                             f"at merge through operation-shape's hard length tier, and the author-text "
                             f"neutraliser lives in telemetry.neutralize_author_text. Delete the reference (or, "
                             f"for a generated file, regenerate it).", validate.loc(os.path.join(base, rel), line))
            for rel, line, token in references(base)]


def main(argv: list) -> int:
    tier = os.environ.get("ENGINE_RULE_TIER", "hard")
    root = validate.env_override_path(ENV_OVERRIDE, validate.ROOT)
    return agent_coherence_check.emit(findings(tier, root))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
