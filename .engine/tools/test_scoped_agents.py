"""Native assignment regressions. Synthetic transport payloads, never claimed as live host proof."""
import concurrent.futures
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import build_coordinator_core as core
import plan_store
import providers
import scoped_agents as scoped


class ScopedAssignments(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.library = plan_store.PlanLibrary(self.root / "plans")
        self.library._mkdir(self.library.plan_dir("test-plan"))
        self.store = scoped.Store(self.library, "test-plan")
        self.packet = self.root / "packet.md"
        self.packet.write_text("Frozen obligations\nUnique packet content.\n")
        self.owner = {"plan": "pln_test", "revision": 1}
        self.a = self.register("architecture")
        self.env = mock.patch.dict(os.environ, {providers.PROVIDER_ENV: "codex"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def register(self, lens):
        return self.store.register(owner=self.owner, root="root-id", purpose="review", lens=lens,
                                   role="engine-design-review-" + lens, packet=self.packet,
                                   packet_digest=core.digest(self.packet.read_bytes()))

    def observe(self, event, tool=None, inp=None, child=None, response=None, **kw):
        payload = {"session_id": "root-id", "tool_use_id": "call-1", **kw}
        if tool:
            payload.update(tool_name=tool, tool_input=inp or {})
        if child:
            payload.update(agent_id=child, agent_type=self.a["role"])
        if response is not None:
            payload["tool_response"] = response
        return self.store.observe(event, providers.normalize(event, payload))

    def launch(self, **overrides):
        inp = {"task_name": self.a["id"], "agent_type": self.a["role"], "fork_turns": "none", **overrides}
        result = self.observe("PreToolUse", "collaborationspawn_agent", inp)
        if result["action"] == "proceed":
            self.observe("PostToolUse", "collaborationspawn_agent", inp,
                         response={"task_name": "/root/" + self.a["id"]})
        return result

    def child_read(self, child="child-a"):
        self.observe("SubagentStart", child=child)
        transcript = {"child": child, "root": "root-id", "name": "/root/" + self.a["id"]}
        with mock.patch.object(providers, "scoped_transcript", return_value=transcript):
            return self.observe("PostToolUse", "Bash", {"command": "cat " + self.a["packet_path"]},
                child=child, response={"exit_code": 0, "stdout": self.packet.read_text()})

    def stop(self, output="[]", messages=None):
        with mock.patch.object(providers, "scoped_transcript", return_value={"final": output, "messages": messages or []}):
            self.observe("SubagentStop", child="child-a")

    def verified(self):
        return self.store.verified_locked(owner=self.owner, root="root-id", lens="architecture",
                                          packet_digest=self.a["packet_digest"])

    def test_queue_is_denied_while_active_and_after_turn_end(self):
        self.launch()
        self.child_read()
        for stopped in (False, True):
            if stopped:
                self.stop('{"status":"needs_clarification"}')
            for target in (self.a["id"], "child-a", "/root/" + self.a["id"]):
                with self.subTest(stopped=stopped, target=target):
                    result = self.observe("PreToolUse", "collaborationsend_message", {"target": target, "message": "x"})
                    self.assertEqual(result["action"], "block")

    def test_parent_reporting_allowed_but_peer_traffic_denied(self):
        self.launch()
        self.child_read()
        for target, action in (("/root", "proceed"), ("root-id", "proceed"), ("peer", "block")):
            result = self.observe("PreToolUse", "collaborationsend_message", {"target": target, "message": "result"}, child="child-a")
            self.assertEqual(result["action"], action)

    def test_blocked_then_clarified_same_child_counts_once(self):
        self.launch()
        self.child_read()
        self.stop('{"status":"needs_clarification"}')
        with self.assertRaises(scoped.EvidenceError):
            self.verified()
        args = {"target": "child-a", "message": "opaque-native-clarification"}
        self.assertEqual(self.observe("PreToolUse", "collaborationfollowup_task", args, tool_use_id="clarify")["action"], "proceed")
        self.observe("PostToolUse", "collaborationfollowup_task", args, response={"task_name": "/root/" + self.a["id"]}, tool_use_id="clarify")
        message = {"author": "/root", "recipient": "/root/" + self.a["id"],
                   "content": [{"type": "encrypted_text", "encrypted_content": args["message"]}]}
        self.stop(messages=[message])
        self.assertEqual(self.verified()["child"], "child-a")
        self.stop(messages=[message])
        self.assertEqual(len(self.verified()["stops"]), 2)  # partial plus final, duplicate ignored

    def test_dispatch_success_without_delivery_does_not_earn_coverage(self):
        self.launch()
        self.child_read()
        args = {"target": "child-a", "message": "not delivered"}
        self.observe("PreToolUse", "followup_task", args)
        self.observe("PostToolUse", "followup_task", args, response={"success": True})
        self.stop()
        with self.assertRaises(scoped.EvidenceError):
            self.verified()
        self.assertEqual(self.observe("PreToolUse", "followup_task", args, tool_use_id="retry")["action"], "block")

    def test_empty_clarification_is_rejected(self):
        self.launch()
        self.child_read()
        for content in (None, "", " "):
            self.assertEqual(self.observe("PreToolUse", "followup_task", {"target": "child-a", "message": content})["action"], "block")

    def test_fork_and_wrong_role_cannot_dispatch(self):
        for changes in ({"fork_turns": "all"}, {"agent_type": "default"}, {"fork_turns": "3"}):
            self.assertEqual(self.launch(**changes)["action"], "block")

    def test_unrelated_tasks_and_unknown_tools_are_not_governed(self):
        self.assertEqual(self.observe("PreToolUse", "spawn_agent", {"task_name": "user-task", "fork_turns": "all"})["action"], "proceed")
        self.assertEqual(self.observe("PreToolUse", "future_control", {"target": self.a["id"]})["action"], "proceed")
        with self.assertRaises(scoped.EvidenceError):
            self.verified()

    def test_packet_read_without_launch_and_wrong_child_metadata_fail(self):
        self.child_read()
        self.stop()
        with self.assertRaises(scoped.EvidenceError):
            self.verified()

    def test_packet_changed_or_failed_read_is_not_evidence(self):
        self.launch()
        self.child_read()
        Path(self.a["packet_path"]).write_text("changed")
        self.stop()
        with self.assertRaises(scoped.EvidenceError):
            self.verified()

    def test_actual_child_cannot_read_a_second_assignment_for_credit(self):
        self.launch()
        self.child_read()
        original = self.a
        self.a = self.register("architecture")
        self.launch()
        self.child_read()
        data = self.store.read()["assignments"]
        self.assertTrue(data[self.a["id"]]["faults"])
        self.assertEqual(data[original["id"]]["child"], "child-a")

    def test_parallel_distinct_registration_preserves_every_assignment(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            assignments = list(pool.map(self.register, ["architecture"] * 8))
        self.assertEqual(len({a["packet_path"] for a in assignments}), 8)
        self.assertEqual(len(self.store.read()["assignments"]), 9)

    def test_parent_restart_reads_same_observations_without_relaunch(self):
        self.launch()
        self.child_read()
        self.stop()
        self.store = scoped.Store(self.library, "test-plan")
        self.assertEqual(self.verified()["child"], "child-a")

    def test_wrong_generation_and_absent_companion_fail(self):
        self.launch()
        self.child_read()
        self.stop()
        self.owner = {**self.owner, "generation": 2}
        with self.assertRaises(scoped.EvidenceError):
            self.verified()

    def test_claude_packet_and_clarification_reads_with_actual_returned_child(self):
        # Documented Agent/Read/SubagentStop shapes; no fabricated Claude transcript envelope.
        with mock.patch.dict(os.environ, {providers.PROVIDER_ENV: "claude"}):
            args = {"subagent_type": self.a["role"], "prompt": "Read " + self.a["packet_path"]}
            self.assertEqual(self.observe("PreToolUse", "Agent", args)["action"], "proceed")
            self.observe("SubagentStart", child="child-a")
            self.observe("PostToolUse", "Read", {"file_path": self.a["packet_path"]}, child="child-a",
                         response={"file": {"content": self.packet.read_text()}})
            self.observe("SubagentStop", child="child-a", last_assistant_message='{"status":"needs_clarification"}')
            self.observe("PostToolUse", "Agent", args, response={"agentId": "child-a", "status": "completed"})
            supplement = self.store.clarify(self.a["id"], "root-id", "The unchanged term means X.")
            message = {"recipient": "child-a", "content": "Read " + supplement["path"]}
            self.assertEqual(self.observe("PreToolUse", "SendMessage", message, tool_use_id="clarify")["action"], "proceed")
            self.observe("PostToolUse", "SendMessage", message, response={"success": True}, tool_use_id="clarify")
            self.observe("SubagentStop", child="child-a", last_assistant_message="[]")
            with self.assertRaises(scoped.EvidenceError):
                self.verified()  # success plus a turn boundary is not supplement delivery
            self.observe("PostToolUse", "Read", {"file_path": supplement["path"]}, child="child-a",
                         response={"file": {"content": "The unchanged term means X."}})
            self.observe("SubagentStop", child="child-a", last_assistant_message="[]")
            self.assertEqual(self.verified()["child"], "child-a")

    def test_duplicate_launch_observation_is_idempotent_but_new_launch_is_not(self):
        self.launch()
        self.assertEqual(self.launch()["action"], "proceed")
        args = {"task_name": self.a["id"], "agent_type": self.a["role"], "fork_turns": "none"}
        self.assertEqual(self.observe("PreToolUse", "spawn_agent", args, tool_use_id="different-call")["action"], "block")

    def test_supplement_mutation_invalidates_completed_execution(self):
        self.launch()
        self.child_read()
        supplement = self.store.clarify(self.a["id"], "root-id", "Original clarification")
        args = {"target": "child-a", "message": "Read " + supplement["path"]}
        self.observe("PreToolUse", "followup_task", args)
        self.observe("PostToolUse", "followup_task", args, response={"success": True})
        self.observe("PostToolUse", "Bash", {"command": "cat " + supplement["path"]}, child="child-a",
                     response={"exit_code": 0, "stdout": "Original clarification"})
        self.stop()
        self.assertEqual(self.verified()["child"], "child-a")
        Path(supplement["path"]).write_text("Changed clarification")
        with self.assertRaises(scoped.EvidenceError):
            self.verified()
        self.store.path.unlink()
        with self.assertRaises(scoped.EvidenceError):
            self.verified()


class NativeProviderFacts(unittest.TestCase):
    def test_no_turn_id_required_for_known_codex_control_names(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(providers.scoped_call({"tool_name": "collaborationfollowup_task"})["provider"], "codex")

    def test_read_error_or_partial_content_cannot_satisfy_packet(self):
        for response in ({"exit_code": 1, "stdout": "packet"}, {"exit_code": 0, "stdout": "pac"}, "packet"):
            self.assertFalse(providers.scoped_read_succeeded({"tool_name": "Bash", "tool_response": response}, "packet"))

    def test_documented_claude_agent_and_read_output(self):
        with mock.patch.dict(os.environ, {providers.PROVIDER_ENV: "claude"}):
            self.assertEqual(providers.scoped_launch_child({"agentId": "actual-child", "status": "completed"}), "actual-child")
            self.assertTrue(providers.scoped_read_succeeded({"tool_name": "Read", "tool_response": {"file": {"content": "packet"}}}, "packet"))
            self.assertFalse(providers.scoped_call({"tool_name": "Agent", "tool_input": {"subagent_type": "fork"}})["fresh"])
            self.assertEqual(providers.scoped_call({"tool_name": "SendMessage", "tool_input": {"recipient": "child", "content": "x"}})["kind"], "continue")


if __name__ == "__main__":
    unittest.main()
