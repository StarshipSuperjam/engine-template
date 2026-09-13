#!/usr/bin/env python3
"""Falsify multipart credit in disposable libraries through the production owners.

Permanent regression home: MultipartReads, MultipartClaudeHook, and the multipart ingress
tests in test_project_manager and test_build_coordinator. Native events are synthetic;
live Claude/Codex qualification must be recorded separately, never inferred.
"""
import argparse
import contextlib
import sys
import unittest
from unittest import mock


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--overcredit-missing-piece", action="store_true",
                        help="Bypass verification in the disposable fixture; the demo must fail.")
    args = parser.parse_args(argv)
    import scoped_agents
    print("Disposable demonstration: UTF-8 pieces, incomplete-read refusal, restart/recovery, "
          "clarification coverage, and real Project Manager/Build receipt ingestion. "
          "Native events are synthetic; this is not live model qualification.", flush=True)
    def broken(store, data, *, owner, root, lens, packet_digest, assignment_id=None):
        return next(a for a in data["assignments"].values() if a["owner"] == owner and
                    a["root"] == root and a["lens"] == lens and a["packet_digest"] == packet_digest)
    fault = mock.patch.object(scoped_agents.Store, "_verified", broken) if args.overcredit_missing_piece else contextlib.nullcontext()
    names = ["test_scoped_agents.MultipartReads", "test_scoped_agents.MultipartClaudeHook",
             "test_project_manager.ObservedPlanReview.test_multipart_receipt_requires_every_original_piece_at_real_plan_ingress",
             "test_build_coordinator.TestReviewAndFindings.test_multipart_native_evidence_is_required_at_build_receipt_ingress"]
    with fault:
        result = unittest.TextTestRunner(stream=sys.stdout, verbosity=1).run(
            unittest.defaultTestLoader.loadTestsFromNames(names))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
