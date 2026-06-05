#!/usr/bin/env python3
"""Slice 21 — tests for modes, the operating stance + the Explore write-gate.

These lock the load-bearing behaviours a non-engineer cannot read code to verify: that the gate DENIES
each building action class in Explore with a plain sentence and ALLOWS everything else (reads, tests,
greps, subagent spawns, gh issue); that an unclassifiable action resolves to ALLOW (no default-deny);
that Build/Routine permit every write; that the stance signal boots Explore (absent / stale / unreadable
→ explore) and that clear_stance deletes it; that the gate fails open end-to-end (run_hook can never
return the blocking exit) and the deny rides the structured permissionDecision channel (exit 0 + the
hookSpecificOutput wrapper the platform honors), NEVER exit-2 block(); and that modes declares its block
on a block-eligible event.
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from unittest import mock

import hooks
import modes
import validate


def _deny(decision: dict) -> bool:
    return (decision.get("action") == "decide"
            and decision.get("permissionDecision") == "deny"
            and bool(decision.get("reason")))


def _allow(decision: dict) -> bool:
    return decision.get("action") == "proceed"


def _explore_payload(tool_name: str, command: str = "") -> dict:
    # session_id=None -> current_stance is Explore (the safe floor), with no signal file needed.
    return {"session_id": None, "tool_name": tool_name,
            "tool_input": {"command": command} if command else {}}


class TestExploreGateDenies(unittest.TestCase):
    """In Explore the gate denies the enumerated building set, each with a plain reason."""

    def test_file_mutating_tools_are_denied(self):
        for tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            d = modes.handler(_explore_payload(tool))
            self.assertTrue(_deny(d), f"{tool} must be denied in Explore")
            self.assertIn("exploring", d["reason"].lower())

    def test_bash_building_verbs_are_denied(self):
        for cmd in ("git commit -m wip", "git commit", "git checkout -b feat", "git switch -c feat",
                    "git branch newbranch", "gh pr create --fill",
                    # at command position after a shell separator (chaining), still denied:
                    "cd /tmp && git commit -m x", "git add . && git commit -m x", "true; gh pr create"):
            d = modes.handler(_explore_payload("Bash", cmd))
            self.assertTrue(_deny(d), f"Bash {cmd!r} must be denied in Explore")

    def test_github_mcp_pr_creation_is_denied(self):
        for tool in ("mcp__github__create_pull_request", "mcp__gh_server__create_pr"):
            d = modes.handler(_explore_payload(tool))
            self.assertTrue(_deny(d), f"{tool} must be denied in Explore")

    def test_deny_names_the_way_forward(self):
        d = modes.handler(_explore_payload("Edit"))
        self.assertIn("build", d["reason"].lower())          # tells the operator how to proceed


class TestExploreGateAllows(unittest.TestCase):
    """In Explore the gate allows everything else — exploring stays the comfortable place to work."""

    def test_reads_and_readonly_bash_are_allowed(self):
        self.assertTrue(_allow(modes.handler(_explore_payload("Read"))))
        self.assertTrue(_allow(modes.handler(_explore_payload("Grep"))))
        for cmd in ("pytest -q", "ls -la", "git status", "git diff", "git branch -a",
                    "git log --oneline", "rg pattern", "gh issue create -t x -b y", "gh issue list",
                    # a build verb inside a quoted/echoed/embedded string is NOT a building action — it
                    # must not trip a false deny (err toward allow; don't tax Explore):
                    "echo 'git commit -m x'", 'grep "gh pr create" notes.md',
                    "echo do not git commit here"):
            self.assertTrue(_allow(modes.handler(_explore_payload("Bash", cmd))),
                            f"Bash {cmd!r} must be allowed in Explore")

    def test_subagent_and_unknown_mcp_tools_are_allowed(self):
        self.assertTrue(_allow(modes.handler(_explore_payload("Task"))))
        self.assertTrue(_allow(modes.handler(_explore_payload("mcp__github__list_issues"))))

    def test_unclassifiable_action_resolves_to_allow_no_default_deny(self):
        # The no-default-deny law: an action the gate cannot classify is ALLOWED, never denied.
        self.assertTrue(_allow(modes.handler(_explore_payload("SomeFutureTool"))))
        self.assertTrue(_allow(modes.handler(_explore_payload("Bash", "some_unknown_binary --flag"))))


class TestStanceSignal(unittest.TestCase):
    """The ephemeral, session-keyed OS-temp signal: round-trips, and resolves to Explore in every
    ambiguous case (absent / unreadable / unrecognized → explore)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(modes.tempfile, "gettempdir", return_value=self._tmp.name)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_default_is_explore_when_no_signal(self):
        self.assertEqual(modes.current_stance("sess-1"), modes.EXPLORE)

    def test_set_build_then_read_then_clear(self):
        self.assertTrue(modes.set_stance("sess-1", modes.BUILD))
        self.assertEqual(modes.current_stance("sess-1"), modes.BUILD)
        self.assertTrue(modes.clear_stance("sess-1"))
        self.assertEqual(modes.current_stance("sess-1"), modes.EXPLORE)   # cleared -> explore

    def test_set_explore_clears_the_marker(self):
        modes.set_stance("sess-1", modes.BUILD)
        self.assertTrue(modes.set_stance("sess-1", modes.EXPLORE))         # explore == absence of a signal
        self.assertEqual(modes.current_stance("sess-1"), modes.EXPLORE)

    def test_unrecognized_signal_content_resolves_to_explore(self):
        path = modes._signal_path("sess-1")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("garbage-not-a-stance")
        self.assertEqual(modes.current_stance("sess-1"), modes.EXPLORE)   # stale/unknown -> the floor

    def test_clear_is_idempotent(self):
        self.assertTrue(modes.clear_stance("never-set"))                  # missing marker is success

    def test_absent_session_id_degrades_safe(self):
        self.assertEqual(modes.current_stance(None), modes.EXPLORE)
        self.assertFalse(modes.set_stance(None, modes.BUILD))             # no id -> cannot enter Build
        self.assertIsNone(modes._signal_path(""))


class TestBuildAndRoutinePermit(unittest.TestCase):
    """In Build or Routine the gate permits the building actions it denies in Explore."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(modes.tempfile, "gettempdir", return_value=self._tmp.name)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def _payload(self, sid, tool, command=""):
        return {"session_id": sid, "tool_name": tool,
                "tool_input": {"command": command} if command else {}}

    def test_build_permits_writes(self):
        modes.set_stance("s", modes.BUILD)
        self.assertTrue(_allow(modes.handler(self._payload("s", "Edit"))))
        self.assertTrue(_allow(modes.handler(self._payload("s", "Bash", "git commit -m x"))))

    def test_routine_permits_writes(self):
        modes.set_stance("s", modes.ROUTINE)
        self.assertTrue(_allow(modes.handler(self._payload("s", "Write"))))
        self.assertTrue(_allow(modes.handler(self._payload("s", "Bash", "gh pr create"))))


class TestFailOpenAndChannel(unittest.TestCase):
    """The gate is fail-open, and a deny rides the structured channel (exit 0), never exit-2 block()."""

    def test_deny_is_exit_0_with_hookSpecificOutput_never_exit_2(self):
        # The platform honors a PreToolUse deny ONLY as exit 0 + a hookSpecificOutput wrapper; exit 2 is
        # read as a crash and the deny dropped. So a real deny must come back as EXIT_PROCEED (0) with the
        # structured permissionDecision, NEVER the blocking exit.
        out, err = io.StringIO(), io.StringIO()
        payload = json.dumps({"session_id": None, "tool_name": "Edit", "tool_input": {"file_path": "/x"}})
        code = hooks.run_hook("PreToolUse", modes.handler,
                              stdin=io.StringIO(payload), stdout=out, stderr=err)
        self.assertEqual(code, hooks.EXIT_PROCEED)            # exit 0, never EXIT_BLOCK (2)
        body = json.loads(out.getvalue())["hookSpecificOutput"]
        self.assertEqual(body["hookEventName"], "PreToolUse")
        self.assertEqual(body["permissionDecision"], "deny")
        self.assertIn("exploring", body["permissionDecisionReason"].lower())

    def test_allow_emits_nothing_and_proceeds(self):
        out, err = io.StringIO(), io.StringIO()
        payload = json.dumps({"session_id": None, "tool_name": "Read", "tool_input": {}})
        code = hooks.run_hook("PreToolUse", modes.handler,
                              stdin=io.StringIO(payload), stdout=out, stderr=err)
        self.assertEqual(code, hooks.EXIT_PROCEED)
        self.assertEqual(out.getvalue(), "")                 # a plain allow injects nothing

    def test_a_crashing_gate_fails_open_never_blocks(self):
        # If the gate's own logic raises, the action must STILL proceed (exit != block) and flag — a gate
        # that crashes must never strand the operator (the hooks fail-open law).
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(modes, "current_stance", side_effect=Exception("boom")):
            code = hooks.run_hook("PreToolUse", modes.handler,
                                  stdin=io.StringIO('{"tool_name":"Edit","tool_input":{}}'),
                                  stdout=out, stderr=err)
        self.assertNotEqual(code, hooks.EXIT_BLOCK)          # never the blocking exit
        self.assertEqual(code, hooks.EXIT_NONBLOCKING)       # fail-open: the action proceeds, flagged

    def test_malformed_payload_fails_open(self):
        out, err = io.StringIO(), io.StringIO()
        code = hooks.run_hook("PreToolUse", modes.handler,
                              stdin=io.StringIO("not json"), stdout=out, stderr=err)
        self.assertNotEqual(code, hooks.EXIT_BLOCK)


class TestBlockInvariantAndVocabulary(unittest.TestCase):
    def test_block_invariant_is_on_a_block_eligible_event(self):
        self.assertEqual(modes.BLOCK_INVARIANT["event"], "PreToolUse")
        self.assertIn(modes.BLOCK_INVARIANT["event"], hooks.BLOCK_ELIGIBLE_EVENTS)
        self.assertEqual(modes.BLOCK_INVARIANT["owner"], "modes")
        # the block-budget leg produces no finding over modes' declaration (it sits on an eligible event)
        self.assertEqual(validate.block_budget_findings([modes.BLOCK_INVARIANT], "hard", "x"), [])

    def test_describe_stance_is_plain_and_falls_back_to_explore(self):
        self.assertIn("Exploring", modes.describe_stance(modes.EXPLORE))
        self.assertIn("Building", modes.describe_stance(modes.BUILD))
        self.assertEqual(modes.describe_stance("nonsense"), modes.describe_stance(modes.EXPLORE))


if __name__ == "__main__":
    unittest.main()
