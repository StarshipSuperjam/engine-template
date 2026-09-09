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
         "Checks reviewed local import edges of hard enforcement sources without executing them. "
         "Environment: ENGINE_RULE_TIER, ENGINE_ENFORCEMENT_FILES_ROOT.")


def _main(_argv: list) -> int:
    root = validate.env_override_path("ENGINE_ENFORCEMENT_FILES_ROOT") or validate.ROOT
    tier = os.environ.get("ENGINE_RULE_TIER", "hard")
    return validate.emit([validate.finding(tier, message) for message in
                          weakening_guard.enforcement_drift_findings(root)])


def main(argv: list) -> int:
    return validate.cli_main(argv, usage=USAGE, run=_main)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
