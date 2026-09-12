#!/usr/bin/env python3
"""Demonstrate fix-first repair completion through production commands and real Git.

Native reviewer transport, candidate/final CI and GitHub inputs are explicit fixtures.
This demonstration neither qualifies live reviewer execution nor replaces Build validation.
"""
import argparse
import contextlib
import sys
import unittest
from unittest import mock


def main(argv=None, *, stream=None):
    stream = sys.stdout if stream is None else stream
    parser = argparse.ArgumentParser(description=__doc__)
    faults = parser.add_mutually_exclusive_group()
    faults.add_argument('--bypass-own-commit-hold', action='store_true',
                        help='Suppress the original-commit hold; unchanged-fix refusal must fail.')
    faults.add_argument('--lose-originals', action='store_true',
                        help='Delete retained originals after a terminal decision; retention must fail.')
    faults.add_argument('--omit-decision-freshness', action='store_true',
                        help='Treat terminal PR evidence as current; stale-disclosure refusal must fail.')
    args = parser.parse_args(argv)
    import build_coordinator as coordinator
    import build_coordinator_review as review
    assess = coordinator.cmd_repair_assess
    contract_current = coordinator._review_contract_current

    def lose_originals(arguments, store):
        assess(arguments, store)
        if (store.read().get('repair') or {}).get('direct_verification'):
            store.mutate(lambda state: state.update(review_evidence_history=[]))

    def omit_freshness(state):
        return True if (state.get('repair') or {}).get('direct_verification') else contract_current(state)

    print('Real Git and production review acceptance, findings, repair assessment, preflight and submit preview; '
          'synthetic native transport, CI and GitHub inputs.', file=stream, flush=True)
    print('Fault control: '+ next((name for name, enabled in vars(args).items() if enabled), 'none'),
          file=stream, flush=True)
    suite = unittest.defaultTestLoader.loadTestsFromName(
        'test_build_coordinator.TestRepairCompletionScenario')
    with contextlib.ExitStack() as stack:
        if args.bypass_own_commit_hold:
            stack.enter_context(mock.patch.object(review, 'accepted_fixed_holds', return_value=[]))
        if args.lose_originals:
            stack.enter_context(mock.patch.object(coordinator, 'cmd_repair_assess', side_effect=lose_originals))
        if args.omit_decision_freshness:
            stack.enter_context(mock.patch.object(coordinator, '_review_contract_current', side_effect=omit_freshness))
        with contextlib.redirect_stdout(stream):
            result = unittest.TextTestRunner(stream=stream, verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
