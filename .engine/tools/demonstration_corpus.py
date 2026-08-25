#!/usr/bin/env python3
"""Run every behavioral demonstration this engine ships, and report the result as structured data.

WHY IT EXISTS. The demonstrations are the engine's fail-then-pass reproducers: each one holds a real past
incident open, so a change that silently reintroduces it goes red at the incident rather than at some
downstream symptom. Nothing ran them together. Individually a demo travels with its companion test, but the
corpus as a WHOLE — including the ones with no companion, which retire at first run — was exercised only when
somebody thought to. A guard nobody runs is a guard that has already stopped guarding.

WHAT IT PRODUCES, AND WHY THAT SHAPE. A JSON result, never a log for something else to scrape:

    {"ok": bool, "ran": [names], "failures": [{"demo": name, "exit_code": n, "output": "…"}],
     "duration_seconds": float, "python": "…"}

The reporting half of the nightly workflow consumes this and nothing else. That is a security boundary as
much as a convenience one: the reporter holds issue-write, and it must never be handed raw demonstration
output to interpret — a demo prints whatever it prints, and a job with a write token deciding what to do
based on unstructured text is a job that can be steered by its own input. Failure output IS carried, but as
a bounded, clearly-fenced field the reporter renders rather than parses.

MEASURED, SO THE COST IS NOT A GUESS. The first full run: 60 demonstrations in 947 seconds on a
workstation. It also found seven of them already failing on main with nothing watching
(StarshipSuperjam/engine-template#1074) — the corpus was never green, and nobody knew.

EVERY DEMO IS A SUBPROCESS. They set environment variables, chdir, redirect roots, and monkeypatch module
globals; running them in one interpreter would let one demo's residue decide another's verdict. The cost is
process startup per demo, which the recorded duration makes visible rather than hidden.

A DEMO THAT CANNOT BE RUN IS A FAILURE, not a skip. An unimportable or crashing demo is exactly the state
this exists to surface, so it is reported as a failure carrying its traceback.

Run:  uv run --directory .engine --frozen -- python tools/demonstration_corpus.py [--json-out <path>]
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

TOOLS = Path(__file__).resolve().parent
# NOT named `demo_*.py`, deliberately. Every enumeration in this engine that means "a demonstration"
# globs that prefix — this runner, the census-completeness guard, and the corpus below — so a runner
# carrying the prefix would enumerate and then execute ITSELF, and would be counted as a demonstration by
# a guard whose whole job is to notice a demonstration nobody references.
# How much of a failing demo's output travels into the report. Enough to see the failure lines the demos
# print at the end; bounded twice over, because this ends up in an Issue body a person reads AND it crosses
# a job boundary as a single value.
_OUTPUT_TAIL = 1200
# And how many failing demos carry their output at all. Past this the corpus is broadly broken and the
# right report is "start at the top", not a transcript nobody finishes.
_OUTPUT_FOR = 12
# A demo may legitimately take minutes (several drive real upgrades against throwaway clones). This is the
# per-demo backstop against one hanging and taking the whole nightly run with it.
_TIMEOUT_SECONDS = 1800


def demos(root: Path | None = None) -> list[Path]:
    """Every shipped demonstration, in a stable order. Read from the directory rather than a list, so a
    demo added without touching this file is still in the corpus — the opposite of the hand-maintained
    roster that lets a new guard quietly sit outside the sweep."""
    tools = Path(root) / ".engine" / "tools" if root else TOOLS
    return sorted(p for p in tools.glob("demo_*.py") if p.is_file())


def run_one(path: Path) -> dict:
    """One demonstration, in its own interpreter. Returns a failure dict or None."""
    try:
        completed = subprocess.run(
            [sys.executable, str(path)], capture_output=True, text=True,
            timeout=_TIMEOUT_SECONDS, cwd=str(path.resolve().parents[1]),
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        code, output = completed.returncode, (completed.stdout or "") + (completed.stderr or "")
    except subprocess.TimeoutExpired:
        return {"demo": path.name, "exit_code": None,
                "output": f"timed out after {_TIMEOUT_SECONDS}s and was stopped"}
    except OSError as exc:                                  # could not be launched at all
        return {"demo": path.name, "exit_code": None, "output": f"could not be run: {exc}"}
    if code == 0:
        return {}
    return {"demo": path.name, "exit_code": code, "output": output[-_OUTPUT_TAIL:]}


def run(root: Path | None = None) -> dict:
    started = time.monotonic()
    corpus = demos(root)
    failures = [failure for failure in (run_one(path) for path in corpus) if failure]
    for failure in failures[_OUTPUT_FOR:]:
        failure["output"] = ("(output omitted: more than "
                             f"{_OUTPUT_FOR} demonstrations are failing, so the corpus is broadly broken)")
    return {
        "ok": not failures,
        "ran": [p.name for p in corpus],
        "failures": failures,
        "duration_seconds": round(time.monotonic() - started, 1),
        "python": sys.version.split()[0],
    }


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(prog="demonstration_corpus.py",
                                     description="run every shipped behavioral demonstration")
    parser.add_argument("--json-out", help="write the structured result here (the reporter's only input)")
    args = parser.parse_args(argv)
    result = run()
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(f"{len(result['ran'])} demonstration(s) in {result['duration_seconds']}s: "
          + ("all passed" if result["ok"] else f"{len(result['failures'])} FAILED"))
    for failure in result["failures"]:
        print(f"  - {failure['demo']} (exit {failure['exit_code']})")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
