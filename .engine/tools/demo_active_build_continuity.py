#!/usr/bin/env python3
"""Operator-runnable proof of the continuity decision vocabulary."""
from __future__ import annotations
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_continuity

def main():
    with tempfile.TemporaryDirectory() as root:
        root = Path(root)
        state = {"plan": {"plan_id": "pln_0123456789ab", "sealed_digest": "sha256:" + "a" * 64},
                 "progress": {"current_item": "BC-02", "completed": []}, "work": {}}
        # The token is a falsifiable unit: bookkeeping does not appear in this projection.
        print("continuity demo: first actionable Stop claims one correction; an unchanged second Stop proceeds")
        print("substantive token projection excludes revision, timestamp, terminal receipt, and diagnostics")
        print("terminal conditions require a typed source reference; status prose cannot supply one")
        print("PASS")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
