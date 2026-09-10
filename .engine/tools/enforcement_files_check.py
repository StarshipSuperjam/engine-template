#!/usr/bin/env python3
"""Check that hard enforcement sources still match their reviewed import topology.

The checker reads Python files as AST data; it never imports or executes the files
it audits.  Repair a finding by classifying the exact local edge in
``weakening_guard.py`` as an enforcement dependency or a reasoned exclusion.
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402
import weakening_guard  # noqa: E402

USAGE = ("Usage: enforcement_files_check.py [-h|--help]\n\n"
         "Check reviewed local imports of hard enforcement sources without executing them.\n"
         "For a new hard script check, edit .engine/tools/weakening_guard.py: add its rule ID and script "
         "to _HARD_SCRIPT_ROOTS, and a source entry to ENFORCEMENT_SOURCE_INVENTORY. Classify every local "
         "import as a dependency or an exclusion with a specific reason; give each dependency its own entry. "
         "Unsupported loader calls need a source, reason and exact sorted AST call multiset in "
         "ENFORCEMENT_DYNAMIC_LOADERS. Keep enforcement sources as regular files, not symbolic links.\n"
         "Recheck: uv run --directory .engine --frozen -- python tools/validate.py "
         "--check engine/check/enforcement-files\n"
         "These declarations are protected guard code and receive the existing acknowledgment review.\n"
         "Environment: ENGINE_RULE_TIER, ENGINE_ENFORCEMENT_FILES_ROOT (fixture/candidate root only).")


def _main(_argv: list) -> int:
    root = validate.env_override_path("ENGINE_ENFORCEMENT_FILES_ROOT") or validate.ROOT
    tier = os.environ.get("ENGINE_RULE_TIER", "hard")
    return validate.emit([validate.finding(tier, message) for message in
                          weakening_guard.enforcement_drift_findings(root)])


def main(argv: list) -> int:
    return validate.cli_main(argv, usage=USAGE, run=_main)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
