#!/usr/bin/env python3
"""Tests for the session-economy PreToolUse gate.

The payload shapes here are the platform's contract, not the engine's, so the cases that matter most are
the ones asserting the gate ALLOWS what it does not recognize: a shape guess that is wrong must degrade to
a no-op, never to a block.
"""
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hooks
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
        self.assertIn(se.MODEL_OFF_SWITCH, reason)

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
        self.assertIn("Continue the next actionable step", reason)

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

    def test_the_master_switch_disables_every_deny(self):
        for value in ("off", "0", "false", "OFF"):
            with mock.patch.dict(os.environ, {se.OFF_SWITCH: value}):
                self.assertAllowed(spawn("Explore", "opus"))
                self.assertAllowed({"tool_name": "ScheduleWakeup", "tool_input": {}})

    def test_each_rule_has_its_own_switch_and_does_not_disable_the_other(self):
        # The rules are unrelated: turning off a self-scheduling deny must not silently un-gate expensive
        # subagent spawns, which one combined switch did.
        with mock.patch.dict(os.environ, {se.MODEL_OFF_SWITCH: "off"}):
            self.assertAllowed(spawn("Explore", "opus"))
            self.assertDenied({"tool_name": "ScheduleWakeup", "tool_input": {}})
        with mock.patch.dict(os.environ, {se.WAKEUP_OFF_SWITCH: "off"}):
            self.assertAllowed({"tool_name": "ScheduleWakeup", "tool_input": {}})
            self.assertDenied(spawn("Explore", "opus"))

    def test_each_deny_names_its_own_switch(self):
        model_reason = self.assertDenied(spawn("Explore", "opus"))
        self.assertIn(se.MODEL_OFF_SWITCH, model_reason)
        wakeup_reason = self.assertDenied({"tool_name": "ScheduleWakeup", "tool_input": {}})
        self.assertNotIn("ENGINE_SESSION_ECONOMY", wakeup_reason)

    def test_the_deny_rides_the_structured_channel_not_exit_two(self):
        # exit-2 block() is read by the platform as a CRASH, dropping the deny AND its reason.
        decision = se.handler(spawn("Explore", "opus"))
        self.assertEqual(decision["action"], "decide")
        self.assertNotEqual(decision.get("action"), "block")


class TestThroughTheRealHookRunner(GateCase):
    """The handler cases above prove the decision logic. These drive the SAME gate through hooks.run_hook,
    which is what actually runs in a session — so the exit code, the stdout envelope, and the fail-open
    harness are proven rather than assumed."""

    def drive(self, payload, handler=None):
        out, err = io.StringIO(), io.StringIO()
        code = hooks.run_hook("PreToolUse", handler or se.handler,
                              stdin=io.StringIO(json.dumps(payload)), stdout=out, stderr=err)
        return code, out.getvalue()

    def test_a_deny_is_exit_zero_with_the_structured_envelope_never_exit_two(self):
        # exit 2 is read by the platform as a CRASH: the deny AND its reason are dropped.
        code, out = self.drive(spawn("Explore", "opus"))
        self.assertEqual(code, hooks.EXIT_PROCEED)
        self.assertNotEqual(code, hooks.EXIT_BLOCK)
        body = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(body["hookEventName"], "PreToolUse")
        self.assertEqual(body["permissionDecision"], "deny")
        self.assertIn("cheap model", body["permissionDecisionReason"])

    def test_an_allow_injects_nothing(self):
        code, out = self.drive({"tool_name": "Read", "tool_input": {}})
        self.assertEqual(code, hooks.EXIT_PROCEED)
        self.assertEqual(out, "")

    def test_a_crashing_gate_fails_open_and_never_blocks(self):
        # The handler cases prove odd INPUT allows; only this proves a raising gate does not strand anyone.
        with mock.patch.object(se, "cheap_models", side_effect=Exception("boom")):
            code, _ = self.drive(spawn("Explore", "opus"))
        self.assertNotEqual(code, hooks.EXIT_BLOCK)

    def test_a_payload_the_platform_cannot_deliver_never_blocks(self):
        out, err = io.StringIO(), io.StringIO()
        code = hooks.run_hook("PreToolUse", se.handler,
                              stdin=io.StringIO("not json at all"), stdout=out, stderr=err)
        self.assertNotEqual(code, hooks.EXIT_BLOCK)


class TestRegistration(unittest.TestCase):
    def test_the_block_is_declared_for_the_governance_registry(self):
        self.assertEqual(se.BLOCK_INVARIANT["event"], "PreToolUse")
        self.assertTrue(se.BLOCK_INVARIANT["modes"])

    def test_the_gate_is_floored_in_the_weakening_guard(self):
        import weakening_guard
        self.assertIn(".engine/tools/session_economy.py", weakening_guard._FLOOR_ENFORCEMENT_HOOKS)


if __name__ == "__main__":
    unittest.main()
