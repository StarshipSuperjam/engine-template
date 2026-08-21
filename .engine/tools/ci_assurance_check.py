#!/usr/bin/env python3
"""Thin finding.v1 adapter for engine/check/ci-assurance-drift."""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ci_assurance  # noqa: E402
import validate  # noqa: E402


def main() -> int:
    finding = ci_assurance.check(validate.env_override_path("ENGINE_CI_ASSURANCE_PATH"))
    print(json.dumps([finding] if finding["severity"] == "hard" else []))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
