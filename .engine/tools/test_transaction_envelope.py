#!/usr/bin/env python3
"""Schema teeth for transaction-envelope.v1, and the one property the shape exists for.

The deletion test is the load-bearing one: whole-engine removal deletes `.engine/` and must still
validate and render its own receipt, so the loader is proven against a tree with the schema file gone.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import transaction_envelope as te  # noqa: E402


def _read_module_source():
    """The module's own text, for the two guards that assert on how it is written."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "transaction_envelope.py")
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def plan(**over):
    base = {
        "inputs": {"release": "v1.2.3"},
        "consequences": ["The engine moves to v1.2.3."],
        "effects": [{"kind": "tracked-files", "description": "engine files replaced"}],
        "reversibility": "reverted-pull-request",
        "digest": "sha256:" + "a" * 64,
        "consent_handle": "sha256:" + "b" * 64,
    }
    base.update(over)
    return base


def envelope(**over):
    base = {
        "schema_version": "transaction-envelope.v1",
        "operation": "engine-upgrade",
        "requested_phase": "plan",
        "completed_phases": ["inspect", "plan"],
        "outcome": "ok",
        "plan": plan(),
    }
    base.update(over)
    return base


class TestSchemaTeeth(unittest.TestCase):
    def test_a_well_formed_envelope_validates(self):
        self.assertIsNotNone(te.validate(envelope()))

    def test_unknown_field_is_refused(self):
        with self.assertRaises(te.EnvelopeError):
            te.validate(envelope(sneaked_in="value"))

    def test_unknown_operation_is_refused(self):
        with self.assertRaises(te.EnvelopeError):
            te.validate(envelope(operation="engine-do-whatever"))

    def test_refused_outcome_must_carry_a_refusal(self):
        with self.assertRaises(te.EnvelopeError) as caught:
            te.validate(envelope(outcome="refused"))
        self.assertIn("refusal", str(caught.exception))

    def test_successful_plan_phase_must_carry_a_plan(self):
        bare = envelope()
        del bare["plan"]
        with self.assertRaises(te.EnvelopeError):
            te.validate(bare)

    def test_refusal_needs_a_stable_code_and_a_way_forward(self):
        good = {"code": "no-update-home", "explanation": "No update home is recorded.",
                "retryable": False, "next_actions": ["Record an update home, then try again."]}
        te.validate(envelope(outcome="refused", refusal=good))
        for broken in ({"code": "Not A Code"}, {"next_actions": []}, {"retryable": "no"}):
            with self.assertRaises(te.EnvelopeError):
                te.validate(envelope(outcome="refused", refusal=dict(good, **broken)))

    def test_receipt_results_are_closed_so_unavailable_cannot_be_spelled_as_green(self):
        for result in ("passed", "failed", "unavailable"):
            te.validate(envelope(verification=[{"check": "coherence", "result": result}]))
        with self.assertRaises(te.EnvelopeError):
            te.validate(envelope(verification=[{"check": "coherence", "result": "green"}]))

    def test_unavailable_never_renders_as_a_pass(self):
        text = te.render(envelope(verification=[{"check": "branch protection", "result": "unavailable"}]))
        self.assertIn("could not run", text)
        self.assertIn("unverified", text)
        self.assertNotIn(": passed", text)

    def test_external_state_handoff_must_be_dated_and_marked_point_in_time(self):
        handoff = {"kind": "verified-external-state", "summary": "Protection floor confirmed.",
                   "observed_at": "2026-08-27T10:00:00Z", "point_in_time": True}
        te.validate(envelope(handoff=handoff))
        for broken in ({"observed_at": None}, {"point_in_time": False}, {"point_in_time": None}):
            candidate = dict(handoff, **broken)
            candidate = {k: v for k, v in candidate.items() if v is not None}
            with self.assertRaises(te.EnvelopeError):
                te.validate(envelope(handoff=candidate))

    def test_checkless_confirmed_is_a_distinct_kind_from_verified(self):
        self.assertIn("checkless-confirmed", te.HANDOFF_KINDS)
        self.assertIn("checkless-confirmed", te.EXTERNAL_STATE_KINDS)
        self.assertNotEqual("checkless-confirmed", "verified-external-state")

    def test_a_plan_must_state_at_least_one_consequence(self):
        with self.assertRaises(te.EnvelopeError):
            te.validate(envelope(plan=plan(consequences=[])))


class TestConsentHandle(unittest.TestCase):
    def test_the_handle_is_stable_across_identical_plans(self):
        self.assertEqual(te.consent_handle(plan()), te.consent_handle(plan()))

    def test_key_order_does_not_change_the_handle(self):
        forward = plan()
        reversed_order = {k: forward[k] for k in reversed(list(forward))}
        self.assertEqual(te.consent_handle(forward), te.consent_handle(reversed_order))

    def test_excluded_noise_does_not_change_the_handle(self):
        baseline = te.consent_handle(plan())
        for noise in ({"timestamp": "2026-01-01T00:00:00Z"}, {"tmpdir": "/tmp/xyz"},
                      {"token": "ghp_secret"}, {"generated_at": "now"}):
            self.assertEqual(te.consent_handle(plan(**noise)), baseline,
                             "{0} must not move the handle".format(noise))

    def test_a_real_change_does_move_the_handle(self):
        baseline = te.consent_handle(plan())
        self.assertNotEqual(te.consent_handle(plan(inputs={"release": "v9.9.9"})), baseline)
        self.assertNotEqual(te.consent_handle(plan(consequences=["Something else entirely."])), baseline)
        self.assertNotEqual(
            te.consent_handle(plan(effects=[{"kind": "external-settings", "description": "protection off"}])),
            baseline)

    def test_a_credential_in_a_plan_never_reaches_the_canonical_form(self):
        self.assertNotIn("ghp_secret", te.canonical(plan(token="ghp_secret")))


class TestSurvivesItsOwnDeletion(unittest.TestCase):
    """The property whole-engine removal depends on: validate and render with `.engine/` gone.

    Run in a subprocess against a throwaway copy — never this checkout — because the scenario is a
    deletion, and a scenario that succeeds in doing the wrong thing has then done it for real.
    """

    def test_validate_and_render_still_work_after_the_engine_tree_is_deleted(self):
        here = os.path.dirname(os.path.abspath(__file__))
        engine_dir = os.path.dirname(here)
        with tempfile.TemporaryDirectory() as tmp:
            copy_root = os.path.join(tmp, "copy")
            shutil.copytree(engine_dir, os.path.join(copy_root, ".engine"),
                            ignore=shutil.ignore_patterns(".venv", ".uv", "plans", "memory", "__pycache__"))
            copied_engine = os.path.join(copy_root, ".engine")
            script = (
                "import json, shutil, sys, os\n"
                "sys.path.insert(0, {tools!r})\n"
                "import transaction_envelope as te\n"
                "shutil.rmtree({engine!r})\n"
                "assert not os.path.exists({engine!r})\n"
                "env = json.loads(sys.stdin.read())\n"
                "te.validate(env)\n"
                "text = te.render(env)\n"
                "print('OK' if 'Where this leaves you' in text else 'RENDER-FAILED')\n"
            ).format(tools=os.path.join(copied_engine, "tools"), engine=copied_engine)
            receipt = envelope(
                operation="engine-remove", requested_phase="run",
                completed_phases=["inspect", "plan", "apply", "verify", "handoff"],
                verification=[{"check": "operator content preserved", "result": "passed"}],
                handoff={"kind": "pull-request", "summary": "The removal is proposed for your review."})
            del receipt["plan"]
            result = subprocess.run([sys.executable, "-c", script], input=json.dumps(receipt),
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("OK", result.stdout)

    def test_the_module_reads_no_file_during_validate_or_render(self):
        """A guard against reintroducing the house's read-at-call-time pattern here."""
        source = _read_module_source()
        after_load = source.split("SCHEMA = _load_schema_once()", 1)[1]
        for forbidden in ("open(", "json.load(", "import "):
            self.assertNotIn(forbidden, after_load,
                             "{0} appears after the eager load; validate/render must touch no file"
                             .format(forbidden))


class TestStaysOnTheArrivalFloor(unittest.TestCase):
    """Arrival runs on the operator's system Python before the 3.11 runtime exists."""

    def _source(self):
        return _read_module_source()

    def test_carries_the_load_bearing_future_import(self):
        self.assertIn("from __future__ import annotations", self._source())

    def test_imports_nothing_beyond_the_standard_library(self):
        source = self._source()
        for third_party in ("jsonschema", "yaml", "requests", "numpy"):
            self.assertNotIn("import {0}".format(third_party), source)

    def test_imports_no_module_that_is_stdlib_only_from_3_11(self):
        self.assertNotIn("import tomllib", self._source())


if __name__ == "__main__":
    unittest.main()
