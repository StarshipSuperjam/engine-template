#!/usr/bin/env python3
"""Falsify the real review/worker ingress boundaries using disposable command fixtures.

Transport observations and approvals are synthetic, not live provider qualification. The actual
Project Manager, Build coordinator and companion stores execute; no operator data is changed.
"""
import argparse
import json
import sys
import unittest
from unittest import mock


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--break-ingress", action="store_true",
                        help="Deliberately bypass the validator in memory; the demo must fail.")
    args = parser.parse_args(argv)
    names = [
        "test_project_manager.ObservedPlanReview.test_raw_report_substitution_refuses_without_mutating_either_store",
        "test_build_coordinator_review.TestObservedExecutionIngress.test_raw_report_copy_cannot_substitute_for_observed_output",
        "test_build_coordinator_work.TestWorkClaims.test_canonical_raw_ingress_rejects_without_state_or_retry_changes",
        "test_build_coordinator_work.TestWorkClaims.test_inline_result_identity_is_observed_by_engine",
    ]
    # These fixtures are permanent command-level witnesses shared with the self-test suite.
    suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    print("Disposable result-contract demo: observed review equality, lossless worker failures, "
          "unchanged stores on rejection, and Engine-owned inline identity.", flush=True)
    with mock.patch("result_contracts.ingest", side_effect=lambda raw, *a, **k: json.loads(raw)) \
            if args.break_ingress else __import__("contextlib").nullcontext():
        result = unittest.TextTestRunner(stream=sys.stdout, verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
