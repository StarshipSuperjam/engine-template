#!/usr/bin/env python3
"""The protocol core's own properties, proven against a recording fake adapter.

The load-bearing test is `TestNothingMutatesBeforeConsent`: the fake records every apply, and a stale or
absent handle must leave that record empty. Everything else in this protocol rests on that.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import transaction  # noqa: E402
import transaction_envelope as te  # noqa: E402


class RecordingAdapter(transaction.Adapter):
    """A fake that records what it was asked to do, and lets a test move the world underneath."""

    operation = "module-add"

    def __init__(self):
        self.applied = []
        self.verified = []
        self.handed_off = []
        self.world = "unchanged"
        self.domain_answer = "the dependency rules said yes"

    def inspect(self, args):
        return {"summary": "module 'design-review' is available",
                "fingerprints": {"world": self.world}}

    def plan(self, args, facts):
        # A thin adapter: the CONSEQUENCE text comes from the domain answer, so stubbing the domain
        # visibly changes the envelope. That is the deference property, made checkable.
        return {
            "inputs": {"module": "design-review"},
            "consequences": ["Adds the design-review capability. " + self.domain_answer],
            "effects": [{"kind": "capability", "description": "design-review becomes available"}],
            "reversibility": "local-recovery",
        }

    def apply(self, args, plan):
        self.applied.append(plan["consent_handle"])
        return {"committed": "abc1234"}

    def verify(self, args, applied):
        self.verified.append(applied)
        return [{"check": "wiring coherence", "result": "passed"}]

    def handoff(self, args, applied, receipts):
        self.handed_off.append(applied)
        return {"kind": "in-tree-commit", "summary": "Added as one labelled commit.",
                "reference": applied["committed"]}


class Args:
    def __init__(self, **kw):
        self.operation = kw.pop("operation", "module-add")
        self.rest = []
        self.json = False
        self.consent_handle = kw.pop("consent_handle", "")
        for key, value in kw.items():
            setattr(self, key, value)


class ProtocolTestCase(unittest.TestCase):
    def setUp(self):
        self._saved = dict(transaction._REGISTRY)
        self.adapter = RecordingAdapter()
        transaction.register(self.adapter)

    def tearDown(self):
        transaction._REGISTRY.clear()
        transaction._REGISTRY.update(self._saved)


class TestPhases(ProtocolTestCase):
    def test_inspect_changes_nothing_and_produces_no_plan(self):
        result = transaction.do_inspect(self.adapter, Args())
        te.validate(result)
        self.assertEqual(result["outcome"], "ok")
        self.assertNotIn("plan", result)
        self.assertEqual(self.adapter.applied, [])

    def test_plan_mints_a_handle_and_still_changes_nothing(self):
        result = transaction.do_plan(self.adapter, Args())
        te.validate(result)
        self.assertTrue(result["plan"]["consent_handle"].startswith("sha256:"))
        self.assertEqual(self.adapter.applied, [])

    def test_planning_twice_against_an_unchanged_world_yields_the_same_handle(self):
        first = transaction.do_plan(self.adapter, Args())["plan"]["consent_handle"]
        second = transaction.do_plan(self.adapter, Args())["plan"]["consent_handle"]
        self.assertEqual(first, second)

    def test_run_applies_verifies_and_hands_off_in_one_process(self):
        handle = transaction.do_plan(self.adapter, Args())["plan"]["consent_handle"]
        result = transaction.do_run(self.adapter, Args(), handle)
        te.validate(result)
        self.assertEqual(result["completed_phases"], ["inspect", "plan", "apply", "verify", "handoff"])
        self.assertEqual(self.adapter.applied, [handle])
        self.assertEqual(len(self.adapter.verified), 1)
        self.assertEqual(len(self.adapter.handed_off), 1)


class TestNothingMutatesBeforeConsent(ProtocolTestCase):
    """The property everything else rests on."""

    def test_an_absent_handle_refuses_and_applies_nothing(self):
        with self.assertRaises(transaction.TransactionRefused) as caught:
            transaction.do_run(self.adapter, Args(), "")
        self.assertEqual(caught.exception.code, "consent-handle-missing")
        self.assertEqual(self.adapter.applied, [])
        self.assertEqual(self.adapter.verified, [])
        self.assertEqual(self.adapter.handed_off, [])

    def test_a_stale_handle_refuses_and_applies_nothing(self):
        handle = transaction.do_plan(self.adapter, Args())["plan"]["consent_handle"]
        self.adapter.world = "moved"          # the world changes after the operator saw the plan
        with self.assertRaises(transaction.StalePlan) as caught:
            transaction.do_run(self.adapter, Args(), handle)
        self.assertEqual(self.adapter.applied, [])
        stale = caught.exception.envelope
        te.validate(stale)
        self.assertEqual(stale["outcome"], "refused")
        self.assertEqual(stale["refusal"]["code"], "consent-handle-stale")

    def test_a_stale_refusal_hands_back_the_fresh_plan_not_merely_a_complaint(self):
        handle = transaction.do_plan(self.adapter, Args())["plan"]["consent_handle"]
        self.adapter.world = "moved"
        with self.assertRaises(transaction.StalePlan) as caught:
            transaction.do_run(self.adapter, Args(), handle)
        stale = caught.exception.envelope
        self.assertIn("plan", stale, "the operator must see WHAT moved, not only that it did")
        self.assertNotEqual(stale["plan"]["consent_handle"], handle)
        self.assertTrue(stale["refusal"]["retryable"])

    def test_a_moved_world_invalidates_the_handle_even_when_the_plan_reads_identically(self):
        """The regression this suite caught during the build.

        The handle was first taken over the plan's own fields only. The world-state fingerprints live in
        the facts, so a repository that moved underneath an unchanged-looking plan kept a VALID handle —
        the staleness guarantee was decorative. The plan now binds the fingerprints it was derived
        against, so state is part of what the operator consented to.
        """
        first = transaction.do_plan(self.adapter, Args())["plan"]
        self.adapter.world = "moved"          # only the fingerprint changes; every word stays the same
        second = transaction.do_plan(self.adapter, Args())["plan"]
        self.assertEqual(first["consequences"], second["consequences"],
                         "precondition: the plan's prose is identical")
        self.assertEqual(first["effects"], second["effects"])
        self.assertNotEqual(first["consent_handle"], second["consent_handle"],
                            "a moved world must invalidate consent even when the wording did not change")

    def test_a_forged_handle_refuses(self):
        with self.assertRaises(transaction.StalePlan):
            transaction.do_run(self.adapter, Args(), "sha256:" + "f" * 64)
        self.assertEqual(self.adapter.applied, [])


class TestAdapterDefersToTheDomain(ProtocolTestCase):
    """An adapter that duplicates a domain rule instead of wrapping it would pass its own suite.

    Stubbing the domain must visibly change the envelope — that is what proves deference.
    """

    def test_stubbing_the_domain_answer_changes_the_envelope(self):
        before = transaction.do_plan(self.adapter, Args())["plan"]["consequences"]
        self.adapter.domain_answer = "the dependency rules refused"
        after = transaction.do_plan(self.adapter, Args())["plan"]["consequences"]
        self.assertNotEqual(before, after)

    def test_stubbing_the_domain_answer_changes_the_handle(self):
        before = transaction.do_plan(self.adapter, Args())["plan"]["consent_handle"]
        self.adapter.domain_answer = "the dependency rules refused"
        after = transaction.do_plan(self.adapter, Args())["plan"]["consent_handle"]
        self.assertNotEqual(before, after, "a different domain answer is a different change")


class TestOperatorTypedOnlyOperations(ProtocolTestCase):
    """Whole-engine removal refuses `run` outright: a deletion is a harder recovery than an upgrade."""

    def test_engine_remove_run_refuses_unconditionally_and_names_the_door(self):
        class Removal(RecordingAdapter):
            operation = "engine-remove"

        removal = Removal()
        transaction.register(removal)
        # Even with a perfectly good handle, run refuses.
        handle = transaction.do_plan(removal, Args(operation="engine-remove"))["plan"]["consent_handle"]
        with self.assertRaises(transaction.TransactionRefused) as caught:
            transaction.do_run(removal, Args(operation="engine-remove"), handle)
        self.assertEqual(caught.exception.code, "operator-typed-only")
        self.assertTrue(any("module_manager.py remove-engine" in action
                            for action in caught.exception.next_actions + [caught.exception.explanation]))
        self.assertEqual(removal.applied, [], "a refused run must apply nothing")

    def test_the_refusal_is_not_a_judgement_the_protocol_invents(self):
        self.assertIn("engine-remove", transaction._OPERATOR_TYPED_ONLY)
        self.assertNotIn("module-add", transaction._OPERATOR_TYPED_ONLY)
        self.assertNotIn("engine-upgrade", transaction._OPERATOR_TYPED_ONLY,
                         "upgrade's start protections are the harness-gated skill and the merge, and "
                         "its consent is the digest handle — not a refusal here")


class TestResume(ProtocolTestCase):
    def test_resume_without_a_progress_marker_replans_and_says_so(self):
        result = transaction.do_resume(self.adapter, Args())
        te.validate(result)
        self.assertIn("plan", result)
        unavailable = [r for r in result["verification"] if r["result"] == "unavailable"]
        self.assertTrue(unavailable, "an adapter with no marker must say prior progress is unreadable")
        self.assertIn("not a continuation", unavailable[0]["detail"])

    def test_resume_applies_nothing_by_itself(self):
        transaction.do_resume(self.adapter, Args())
        self.assertEqual(self.adapter.applied, [])

    def test_an_adapter_with_its_own_resume_is_used(self):
        marker = {"schema_version": te.SCHEMA_VERSION, "operation": "module-add",
                  "requested_phase": "resume", "completed_phases": ["inspect", "plan", "apply"],
                  "outcome": "partial"}
        self.adapter.resume = lambda args: marker
        self.assertEqual(transaction.do_resume(self.adapter, Args())["outcome"], "partial")


class TestUnknownOperation(ProtocolTestCase):
    def test_an_unimplemented_operation_refuses_with_a_way_forward(self):
        with self.assertRaises(transaction.TransactionRefused) as caught:
            transaction._adapter_for("engine-do-whatever")
        self.assertEqual(caught.exception.code, "unknown-operation")
        self.assertTrue(caught.exception.next_actions)


class TestStaysOnTheArrivalFloor(unittest.TestCase):
    def test_core_is_standard_library_only_with_the_future_import(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "transaction.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("from __future__ import annotations", source)
        for third_party in ("jsonschema", "yaml", "requests"):
            self.assertNotIn("import {0}".format(third_party), source)
        self.assertNotIn("import tomllib", source)


if __name__ == "__main__":
    unittest.main()
