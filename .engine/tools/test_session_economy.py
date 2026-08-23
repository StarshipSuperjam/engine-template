#!/usr/bin/env python3
"""Tests for the session-economy PreToolUse gate.

The payload shapes here are the platform's contract, not the engine's, so the cases that matter most are
the ones asserting the gate ALLOWS what it does not recognize: a shape guess that is wrong must degrade to
a no-op, never to a block.
"""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import session_economy as se


def spawn(kind, model=None, tool="Agent"):
    payload = {"tool_name": tool, "tool_input": {"subagent_type": kind}}
    if model is not None:
        payload["tool_input"]["model"] = model
    return payload


class GateCase(unittest.TestCase):
    def setUp(self):
        clear = mock.patch.dict(os.environ, {}, clear=False)
        clear.start()
        os.environ.pop(se.OFF_SWITCH, None)
        self.addCleanup(clear.stop)

    def assertAllowed(self, payload):
        self.assertEqual(se.handler(payload), {"action": "proceed"}, payload)

    def assertDenied(self, payload):
        decision = se.handler(payload)
        self.assertEqual(decision.get("action"), "decide", payload)
        self.assertEqual(decision.get("permissionDecision"), "deny", payload)
        return decision["reason"]


class TestSubagentModelGate(GateCase):
    def test_an_explore_spawn_naming_no_model_is_denied(self):
        reason = self.assertDenied(spawn("Explore"))
        self.assertIn("cheap model", reason)
        # The reason must name the escape, or a denied session has nowhere to go.
        self.assertIn(se.OFF_SWITCH, reason)

    def test_a_cheap_explore_spawn_is_allowed(self):
        for model in ("sonnet", "haiku"):
            self.assertAllowed(spawn("Explore", model))

    def test_an_expensive_search_or_plan_spawn_is_denied(self):
        self.assertDenied(spawn("Explore", "opus"))
        self.assertDenied(spawn("Plan", "opus"))

    def test_a_versioned_alias_suffix_still_resolves(self):
        # The harness may present an alias with a trailing marker; the model identity is the head.
        self.assertAllowed(spawn("Explore", "sonnet[1m]"))

    def test_both_subagent_tool_names_are_gated(self):
        # The subagent tool has been named both Task and Agent; a rename must not silently un-gate.
        self.assertDenied(spawn("Explore", "opus", tool="Task"))
        self.assertDenied(spawn("Explore", "opus", tool="Agent"))

    def test_bound_personas_and_judgment_agents_are_untouched(self):
        # The engine's own reviewers carry a stamped model: this gate is for the UNBOUND types only.
        for kind in ("engine-qa-review-usability", "engine-design-review-architecture",
                     "general-purpose", "claude"):
            self.assertAllowed(spawn(kind))
            self.assertAllowed(spawn(kind, "opus"))

    def test_the_accepted_set_is_derived_from_the_bindings_file(self):
        self.assertIn("haiku", se.cheap_models())
        with mock.patch.object(se, "BINDINGS", Path("/nonexistent/model-bindings.json")):
            # An unreadable bindings file must fall back to the floor, never to an empty set: an empty set
            # would deny every spawn on a file-read error.
            self.assertEqual(se.cheap_models(), {"haiku", "sonnet"})
            self.assertAllowed(spawn("Explore", "sonnet"))


class TestWakeupGate(GateCase):
    def test_self_scheduling_is_denied(self):
        reason = self.assertDenied({"tool_name": "ScheduleWakeup", "tool_input": {"delaySeconds": 1500}})
        self.assertIn(se.OFF_SWITCH, reason)

    def test_ordinary_tools_are_untouched(self):
        for name in ("Bash", "Read", "Edit", "Write", "ExitPlanMode"):
            self.assertAllowed({"tool_name": name, "tool_input": {"command": "ls"}})


class TestFailsTowardAllow(GateCase):
    def test_unrecognized_shapes_never_block(self):
        for payload in ({}, {"tool_name": None}, {"tool_name": "Agent"},
                        {"tool_name": "Agent", "tool_input": None},
                        {"tool_name": "Agent", "tool_input": "not-a-dict"},
                        {"tool_name": "Agent", "tool_input": {}},
                        {"nothing": "recognizable"}, None, "", []):
            self.assertEqual(se.handler(payload), {"action": "proceed"}, payload)

    def test_the_off_switch_disables_every_deny(self):
        for value in ("off", "0", "false", "OFF"):
            with mock.patch.dict(os.environ, {se.OFF_SWITCH: value}):
                self.assertAllowed(spawn("Explore", "opus"))
                self.assertAllowed({"tool_name": "ScheduleWakeup", "tool_input": {}})

    def test_the_deny_rides_the_structured_channel_not_exit_two(self):
        # exit-2 block() is read by the platform as a CRASH, dropping the deny AND its reason.
        decision = se.handler(spawn("Explore", "opus"))
        self.assertEqual(decision["action"], "decide")
        self.assertNotEqual(decision.get("action"), "block")


class TestRegistration(unittest.TestCase):
    def test_the_block_is_declared_for_the_governance_registry(self):
        self.assertEqual(se.BLOCK_INVARIANT["event"], "PreToolUse")
        self.assertTrue(se.BLOCK_INVARIANT["modes"])

    def test_the_gate_is_floored_in_the_weakening_guard(self):
        import weakening_guard
        self.assertIn(".engine/tools/session_economy.py", weakening_guard._FLOOR_ENFORCEMENT_HOOKS)


if __name__ == "__main__":
    unittest.main()
