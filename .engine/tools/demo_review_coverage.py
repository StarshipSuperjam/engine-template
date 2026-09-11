#!/usr/bin/env python3
"""Falsify cumulative coverage through real Git and the production submission gate.

Permanent fate: TestCumulativeReviewScenario and test_review_economy.ReviewCoverageDemo.
Native event transport, CI, preflight and GitHub observations are labelled synthetic boundaries;
this demonstration does not qualify a live reviewer or replace this Build's real validation.
"""
import argparse
import contextlib
import sys
import unittest
from unittest import mock


def main(argv=None, *, stream=None):
    stream = stream if stream is not None else sys.stdout
    parser = argparse.ArgumentParser(description=__doc__)
    faults = parser.add_mutually_exclusive_group()
    faults.add_argument("--lose-coverage", action="store_true",
                        help="Discard archived reads after four repairs; the real coverage assertion must fail.")
    faults.add_argument("--overcredit-gap", action="store_true",
                        help="Lie that a nonempty unread set is covered; the submission refusal must fail.")
    args = parser.parse_args(argv)
    import build_coordinator_review as review
    import build_review_range as ranges
    retained = review.retained_receipts
    cumulative = ranges.cumulative_coverage
    def lost(state):
        if len(state.get("review_evidence_history", [])) >= 4:
            return retained(dict(state, review_evidence_history=[]))
        return retained(state)
    def overcredited(*a, **k):
        result = cumulative(*a, **k)
        if result["verified"] and result["unread"]:
            result.update(covered=True, unread=[])
        return result
    print("Disposable cumulative-review demonstration. Real Git, receipt acceptance, findings and submission logic; "
          "synthetic native transport, CI/preflight and GitHub inputs.", file=stream, flush=True)
    print("Fault control: " + ("loss of retained coverage" if args.lose_coverage else
          "gap overcredit" if args.overcredit_gap else "none"), file=stream, flush=True)
    suite = unittest.defaultTestLoader.loadTestsFromName(
        "test_build_coordinator.TestCumulativeReviewScenario.test_four_repairs_merge_gap_and_later_edit_use_production_readiness")
    with contextlib.ExitStack() as stack:
        if args.lose_coverage:
            stack.enter_context(mock.patch.object(review,"retained_receipts",side_effect=lost))
        if args.overcredit_gap:
            stack.enter_context(mock.patch.object(ranges,"cumulative_coverage",side_effect=overcredited))
        with contextlib.redirect_stdout(stream):
            result = unittest.TextTestRunner(stream=stream,verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
