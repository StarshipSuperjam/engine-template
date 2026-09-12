"""Read-only test-performance reports and bounded CI summaries; never merge receipts."""
from __future__ import annotations

import argparse
from collections import Counter
import html
import os
from pathlib import Path

import selftest_results as records

TARGET_SECONDS = 900
CONCERN_SECONDS = 1200
RESULTS_PREFIX = "engine-selftest-results"
PERFORMANCE_PREFIX = "engine-selftest-performance"


def observed_summary(results_path, performance_path):
    lines = ["## Observed self-test interval", "",
             "This interval covers the serial self-test launcher, not the complete required PR CI path.", ""]
    try:
        result = records.read(results_path)
        complete, passed = records.validate(result)
        counts = Counter(row["outcome"] for row in result["cases"])
        lines.append(f"Outcomes: {'complete' if complete else 'incomplete'}; {'passed' if passed else 'not passed'}.")
        lines.append(f"Selected: {len(result['cases'])}; observed starts: {result['executed_count']}.")
        lines.append("; ".join(f"{name}: {count}" for name,count in sorted(counts.items())))
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        lines.append("Outcomes: unknown (missing or invalid observation). The self-test step owns the verdict.")
    try:
        metrics = records.read(performance_path)
        records.validate_shape(metrics, "selftest-performance.v1")
        seconds = metrics["parent_seconds"]
        if seconds is None:
            raise ValueError("parent interval missing")
        lines.extend(["", f"Launcher elapsed: {seconds:.3f} seconds; collection: {metrics['collection_seconds']:.3f} seconds.",
                      "Case and fixture spans are inclusive; nested test runs are parent cost.", "", "Slowest observed cases:"])
        for row in sorted(metrics["cases"], key=lambda r: -(r["seconds"] or 0))[:20]:
            duration = "unknown" if row["seconds"] is None else f"{row['seconds']:.6f}s"
            # HTML-escaped inside a code element; never interpret test names as Markdown or workflow commands.
            label = html.escape(records.text(row["id"])).replace("\n", " ").replace("\r", " ")
            lines.append(f"- <code>{label}</code> occurrence {row['occurrence']}: {duration}")
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        lines.extend(["", "Timing: unknown (missing, incomplete or invalid optional observations)."])
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    publish = subs.add_parser("publish", help="append bounded self-test observations to the job summary")
    publish.add_argument("--results", required=True)
    publish.add_argument("--performance", required=True)
    args = parser.parse_args(argv)
    summary = observed_summary(args.results, args.performance)
    destination = os.environ.get("GITHUB_STEP_SUMMARY")
    if destination:
        try:
            with open(destination, "a", encoding="utf-8") as stream:
                stream.write(summary)
        except OSError:
            print("Self-test summary unavailable; original test verdict is unchanged.")
    else:
        print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
