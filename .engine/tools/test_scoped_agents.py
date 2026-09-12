"""Native assignment regressions. Synthetic transport payloads, never claimed as live host proof."""
import concurrent.futures
import copy
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import build_coordinator_core as core
import plan_store
import providers
import scoped_agents as scoped
import result_contracts


class ScopedAssignments(unittest.TestCase):
    def setUp(self):
        from selftest_support import review_fixture
        review_fixture(self)
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

    def test_closed_historical_assignment_shape_refuses_current_writer_with_guidance(self):
        """Use a production assignment and its real schema, not a toy evidence record."""
        schema = core._local_validation_schema(Path(scoped.__file__).resolve().parents[1] /
                                               "schemas/scoped-agent-evidence.v1.json")
        historical = copy.deepcopy(schema)
        del historical["$defs"]["assignment"]["properties"]["result_contract"]
        repo = self.root / "reader-history"
        schema_path = repo / ".engine/schemas/scoped-agent-evidence.v1.json"
        schema_path.parent.mkdir(parents=True)
        def git(*args):
            return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                                  text=True, check=True).stdout.strip()
        git("init", "-q")
        git("config", "user.email", "fixture@example.invalid")
        git("config", "user.name", "Fixture")
        schema_path.write_text(json.dumps(historical))
        git("add", ".")
        git("-c", "core.hooksPath=/dev/null", "commit", "-qm", "historical closed shape")
        old_head = git("rev-parse", "HEAD")
        schema_path.write_text(json.dumps(schema))
        git("add", ".")
        git("-c", "core.hooksPath=/dev/null", "commit", "-qm", "writer result binding")
        git("update-ref", "refs/remotes/origin/main", "HEAD")
        git("checkout", "-q", "--detach", old_head)
        value = self.store.read()
        original = self.store.path.read_bytes()
        self.assertEqual(plan_store.shared_reader_diagnosis(value, schema_path), "incompatible")
        with self.assertRaisesRegex(core.CoordinatorError, "older than the shared record"):
            plan_store.validate_shared_record(value, schema_path, local_refs=True)
        self.assertEqual(self.store.path.read_bytes(), original)

    def observe(self, event, tool=None, inp=None, child=None, response=None, **kw):
        payload = {"session_id": "root-id", "tool_use_id": "call-1", **kw}
        if tool:
            payload.update(tool_name=tool, tool_input=inp or {})
        if child:
            payload.update(agent_id=child, agent_type=self.a["role"])
            if event == "SubagentStart" and not payload.get("transcript_path"):
                transcript = self.root / (child + "-start.jsonl")
                transcript.write_text(json.dumps({"type": "session_meta", "payload": {"id": child,
                    "source": {"subagent": {"thread_spawn": {"parent_thread_id": payload["session_id"],
                    "agent_path": "/root/" + self.a["id"]}}}}}) + "\n")
                payload["transcript_path"] = str(transcript)
        if response is not None:
            payload["tool_response"] = response
        return self.store.observe(event, providers.normalize(event, payload))

    def launch(self, **overrides):
        inp = {"task_name": self.a["id"], "agent_type": self.a["role"], "fork_turns": "none", "message": "opaque launch", **overrides}
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

    def stop(self, output="[]", messages=None, launch_message="opaque launch"):
        if messages is None:
            messages = [{"author": "/root", "recipient": "/root/" + self.a["id"],
                         "content": [{"type": "input_text", "text": "native header"},
                                     {"type": "encrypted_content", "encrypted_content": c["content"]}]}
                        for c in self.store.read()["assignments"][self.a["id"]]["continuations"] if c["dispatched"]]
        messages = [{"author": "/root", "recipient": "/root/" + self.a["id"],
                     "content": [{"type": "input_text", "text": "native header"},
                                 {"type": "encrypted_content", "encrypted_content": launch_message}]}] + messages
        with mock.patch.object(providers, "scoped_transcript", return_value={"final": output, "messages": messages,
                "child": "child-a", "root": "root-id", "name": "/root/" + self.a["id"]}):
            self.observe("SubagentStop", child="child-a")

    def verified(self):
        return self.store.verified_locked(owner=self.owner, root="root-id", lens="architecture",
                                          packet_digest=self.a["packet_digest"])

    def test_oversized_stop_is_not_hashed_or_retained_and_can_recover(self):
        self.launch()
        self.child_read()
        huge = "é" * (result_contracts.LIMITS["bytes"] // 2 + 1)
        original = core.digest
        def checked(value):
            self.assertNotEqual(value, huge)
            return original(value)
        with mock.patch.object(core, "digest", side_effect=checked):
            self.stop(huge)
        stop = self.store.read()["assignments"][self.a["id"]]["stops"][-1]
        self.assertIsNone(stop["output"])
        self.assertEqual(stop["rejection"]["rule"], "output_limit")
        self.assertLess(self.store.path.stat().st_size, 100000)
        with self.assertRaises(scoped.EvidenceError):
            self.verified()
        self.stop("[]")
        self.assertEqual(self.verified()["id"], self.a["id"])

    def test_generated_packet_mutation_cannot_receive_the_original_target_label(self):
        expected = core.digest(self.packet.read_bytes())
        before = self.store.path.read_bytes()
        frozen = sorted(self.store.path.parent.joinpath("scoped-packets").iterdir())
        self.packet.write_text("a different generated packet")
        with self.assertRaisesRegex(scoped.EvidenceError, "changed before freezing"):
            scoped.prepare_packets(self.library, self.store.slug, self.owner, "root-id", self.packet,
                {"architecture": "logical-target-digest"}, {"architecture": self.a["role"]},
                expected_file_digest=expected)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(sorted(self.store.path.parent.joinpath("scoped-packets").iterdir()), frozen)

    def test_generated_file_digest_is_distinct_from_lens_target_identity(self):
        expected = core.digest(self.packet.read_bytes())
        assignments = scoped.prepare_packets(self.library, self.store.slug, self.owner, "root-id", self.packet,
            {"feasibility": "logical-lens-target"}, {"feasibility": "engine-design-review-feasibility"},
            expected_file_digest=expected)
        self.assertEqual(assignments[0]["file_digest"], expected)
        self.assertEqual(assignments[0]["packet_digest"], "logical-lens-target")
        self.assertEqual(Path(assignments[0]["packet_path"]).read_bytes(), self.packet.read_bytes())

    def test_result_binding_is_frozen_and_invalid_acceptance_does_not_mutate(self):
        from test_plan_store import _document
        slug = self.library.create(_document())
        self.store = scoped.Store(self.library, slug)
        self.owner["kind"] = "plan"
        self.a = self.register("architecture")
        self.launch(); self.child_read(); self.stop()
        binding = self.a["result_contract"]
        self.assertEqual(binding, result_contracts.resolve("plan-review-finding.v1"))
        receipt = {"lens": "architecture", "packet_digest": self.a["packet_digest"]}
        changed_type = copy.deepcopy(binding)
        changed_type["schema"]["items"]["additionalProperties"] = 0
        self.assertEqual(changed_type, binding)  # Python equality must not authorize this change.
        for mutation in (None, {**binding, "schema_digest": "sha256:" + "0" * 64}, changed_type):
            def change(data):
                data["assignments"][self.a["id"]].pop("result_contract", None)
                if mutation is not None:
                    data["assignments"][self.a["id"]]["result_contract"] = mutation
            self.store.change(change)
            before = self.store.path.read_bytes()
            with self.assertRaises(scoped.EvidenceError):
                self.store.accept_locked(owner=self.owner, root="root-id", receipt=receipt,
                    lenses=["architecture"], packet_digests={"architecture": self.a["packet_digest"]})
            self.assertEqual(before, self.store.path.read_bytes())

    def test_native_capacity_rejection_permits_one_fresh_retry_and_preserves_failure(self):
        args = {"task_name": self.a["id"], "agent_type": self.a["role"], "fork_turns": "none", "message": "opaque launch"}
        self.observe("PreToolUse", "spawn_agent", args, tool_use_id="failed-launch")
        error = "collab spawn failed: agent thread limit reached"
        self.observe("PostToolUse", "spawn_agent", args, tool_use_id="failed-launch", response=error, is_error=True)
        args = {**args, "message": "opaque retry"}
        self.assertEqual(self.observe("PreToolUse", "spawn_agent", args, tool_use_id="retry-launch")["action"], "proceed")
        self.observe("PostToolUse", "spawn_agent", args, tool_use_id="retry-launch", response={"task_name": "/root/" + self.a["id"]})
        self.child_read()
        self.stop(launch_message="opaque retry")
        result = self.verified()
        self.assertEqual(result["failed_launches"][0]["response"], error)
        self.assertEqual(result["failed_launches"][0]["call_id"], "failed-launch")
        self.assertEqual(result["launch"]["call_id"], "retry-launch")

    def test_capacity_retry_is_bounded_and_missing_or_uncertain_errors_do_not_retry(self):
        for outcome in (None, "transport failed", {"error": "agent thread limit reached"},
                        "collab spawn failed: agent thread limit reached"):
            self.a = self.register("architecture")
            args = {"task_name": self.a["id"], "agent_type": self.a["role"], "fork_turns": "none", "message": "opaque launch"}
            self.observe("PreToolUse", "spawn_agent", args, tool_use_id="first")
            if outcome is not None:
                self.observe("PostToolUse", "spawn_agent", args, tool_use_id="first", response=outcome, is_error=True)
            args = {**args, "message": "opaque retry"}
            result = self.observe("PreToolUse", "spawn_agent", args, tool_use_id="retry")
            if outcome == "collab spawn failed: agent thread limit reached":
                self.assertEqual(result["action"], "proceed")
                self.observe("PostToolUse", "spawn_agent", args, tool_use_id="retry", response=outcome, is_error=True)
                self.assertEqual(self.observe("PreToolUse", "spawn_agent", args, tool_use_id="third")["action"], "block")
                self.assertEqual(len(self.store.read()["assignments"][self.a["id"]]["failed_launches"]), 1)
            else:
                self.assertEqual(result["action"], "block")

    def test_capacity_retry_cannot_accept_the_failed_attempts_late_child(self):
        args = {"task_name": self.a["id"], "agent_type": self.a["role"], "fork_turns": "none", "message": "opaque launch"}
        self.observe("PreToolUse", "spawn_agent", args, tool_use_id="first")
        self.observe("PostToolUse", "spawn_agent", args, tool_use_id="first",
            response="collab spawn failed: agent thread limit reached", is_error=True)
        self.assertEqual(self.observe("PreToolUse", "spawn_agent", args, tool_use_id="retry")["action"], "block")
        retry = {**args, "message": "opaque retry"}
        self.assertEqual(self.observe("PreToolUse", "spawn_agent", retry, tool_use_id="retry")["action"], "proceed")
        self.observe("PostToolUse", "spawn_agent", retry, tool_use_id="retry",
            response={"task_name": "/root/" + self.a["id"]})
        self.child_read()
        self.stop()  # Late first-attempt child has the same name and role, but the original launch message.
        with self.assertRaises(scoped.EvidenceError):
            self.verified()

    def test_native_error_without_post_hook_can_qualify_one_retry(self):
        args = {"task_name":self.a['id'],"agent_type":self.a['role'],"fork_turns":"none","message":"opaque launch"}
        self.observe('PreToolUse','spawn_agent',args,tool_use_id='failed')
        path=self.root/'parent.jsonl'
        rows=[{'type':'session_meta','payload':{'id':'root-id'}},
              {'type':'response_item','payload':{'type':'function_call','name':'spawn_agent','namespace':'collaboration',
                'call_id':'failed','arguments':json.dumps(args)}},
              {'type':'response_item','payload':{'type':'function_call_output','call_id':'failed',
                'output':'collab spawn failed: agent thread limit reached'}}]
        retry={**args,'message':'opaque retry'}
        rows[1]['payload']['arguments']=json.dumps({**args,'message':'wrong original input'})
        path.write_text('\n'.join(json.dumps(r) for r in rows)+'\n')
        self.assertEqual(self.observe('PreToolUse','spawn_agent',retry,tool_use_id='retry',transcript_path=str(path))['action'],'block')
        rows[1]['payload']['arguments']=json.dumps(args)
        path.write_text('\n'.join(json.dumps(r) for r in rows)+'\n')
        self.assertEqual(self.observe('PreToolUse','spawn_agent',retry,tool_use_id='retry',transcript_path=str(path))['action'],'proceed')
        self.observe('PostToolUse','spawn_agent',retry,tool_use_id='retry',response={'task_name':'/root/'+self.a['id']})
        self.child_read()
        self.stop(launch_message='opaque retry')
        result=self.verified()
        self.assertEqual(result['failed_launches'][0]['rejection_observation']['path'],str(path))
        self.assertEqual(result['failed_launches'][0]['call_id'],'failed')

    def test_duplicate_clarification_is_refused_before_dispatch(self):
        self.launch()
        self.child_read()
        args = {"target": "child-a", "message": "repeat"}
        self.observe("PreToolUse", "followup_task", args, tool_use_id="first")
        self.observe("PostToolUse", "followup_task", args, tool_use_id="first", response={"ok": True})
        self.stop('{"status":"needs_clarification"}')
        before = self.store.path.read_bytes()
        result = self.observe("PreToolUse", "followup_task", args, tool_use_id="second")
        self.assertEqual(result["action"], "block")
        self.assertIn("distinct clarification", result["reason"])
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_oversized_and_nonregular_inputs_are_refused_without_unbounded_reads(self):
        for label in ("packet", "supplement"):
            with self.subTest(label=label):
                self.packet.write_bytes(b'x' * (providers.SCOPED_READ_MAX_BYTES + 1))
                with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("unbounded read")), \
                     self.assertRaisesRegex(scoped.EvidenceError, "narrow the " + label):
                    scoped._bounded_input(self.packet, label)
                fifo = self.root / (label + '.fifo')
                if hasattr(os, "mkfifo"):
                    os.mkfifo(fifo)
                    with self.assertRaisesRegex(scoped.EvidenceError, "regular"):
                        scoped._bounded_input(fifo, label)

    def test_unrelated_native_start_is_not_persisted(self):
        before = self.store.path.read_bytes()
        with mock.patch.object(providers, "scoped_transcript", return_value={"child":"other", "root":"root-id", "name":"/root/unrelated"}) as read:
            self.observe("SubagentStart", child="other")
        self.assertTrue(read.call_args.kwargs['metadata_only'])
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_uncertain_followup_transport_failure_remains_unverified_not_retryable(self):
        self.launch()
        self.child_read()
        args = {"target": "child-a", "message": "opaque clarification"}
        self.observe("PreToolUse", "followup_task", args, tool_use_id="first")
        self.observe("PostToolUse", "followup_task", args, tool_use_id="first", response={"error": "transport failed"}, is_error=True)
        self.assertEqual(self.observe("PreToolUse", "followup_task", args, tool_use_id="retry")["action"], "block")
        self.stop(messages=[])
        with self.assertRaises(scoped.EvidenceError):
            self.verified()

    def test_packet_and_supplement_utf8_limits_refuse_before_writes(self):
        limit = providers.SCOPED_READ_MAX_BYTES
        self.packet.write_text("é" * (limit // 2))
        exact = self.register("architecture")
        self.assertEqual(Path(exact["packet_path"]).stat().st_size, limit)
        before = self.store.path.read_bytes()
        existing = set(self.store.path.parent.rglob("*"))
        self.packet.write_text("é" * (limit // 2) + "x")
        with self.assertRaisesRegex(scoped.EvidenceError, "narrow the packet"):
            self.register("architecture")
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(set(self.store.path.parent.rglob("*")), existing)
        # Original assignment remains small and can stage exactly the reader's UTF-8 byte bound.
        self.packet.write_text("Frozen obligations\nUnique packet content.\n")
        self.launch()
        self.child_read()
        exact_supplement = self.store.clarify(self.a["id"], "root-id", "é" * (limit // 2))
        self.assertEqual(Path(exact_supplement["path"]).stat().st_size, limit)
        before = self.store.path.read_bytes()
        existing = set(self.store.path.parent.rglob("*"))
        with self.assertRaisesRegex(scoped.EvidenceError, "narrow the supplement"):
            self.store.clarify(self.a["id"], "root-id", "é" * (limit // 2) + "x")
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(set(self.store.path.parent.rglob("*")), existing)

    def test_corrupt_companion_cannot_disable_valid_guard_in_either_order(self):
        import io
        self.launch()
        self.child_read()
        for damaged in ("{malformed", "[]", "null", "true", "42", '"private detail"', "{}"):
            for order in (("broken", "test-plan"), ("test-plan", "broken")):
                with self.subTest(order=order, damaged=damaged):
                    broken = self.library.plan_dir("broken") / scoped.FILENAME
                    broken.parent.mkdir(exist_ok=True)
                    broken.write_text(damaged)
                    payload = providers.normalize("PreToolUse", {"session_id": "root-id", "tool_use_id": "queue",
                        "tool_name": "send_message", "tool_input": {"target": "child-a", "message": "queued"}})
                    notice = io.StringIO()
                    with mock.patch.object(self.library, "slugs", return_value=list(order)), mock.patch("sys.stderr", notice), \
                         mock.patch.object(scoped.hooks, "_promote_fail_open", return_value=False) as promote, \
                         mock.patch.object(scoped.hooks, "_record_crash_debug") as debug:
                        result = scoped.handler("PreToolUse", payload, self.library)
                    self.assertEqual(result["action"], "block")
                    self.assertIn("unverified", notice.getvalue())
                    self.assertNotIn(str(broken), notice.getvalue())
                    self.assertNotIn("private detail", notice.getvalue())
                    promote.assert_called_once()
                    debug.assert_called_once()
                    self.assertEqual(broken.read_text(), damaged)
                    self.assertFalse(scoped.Store(self.library, "broken").receipt_verified({"lens": "architecture"}, self.owner))

    def test_damaged_companions_use_real_hook_failure_reporting_once(self):
        import io
        self.store.path.write_text("null")
        broken = self.library.plan_dir("broken") / scoped.FILENAME
        broken.parent.mkdir(exist_ok=True)
        broken.write_text("[]")
        for recorded in (True, False):
            with self.subTest(recorded=recorded):
                out, err = io.StringIO(), io.StringIO()
                with mock.patch.object(scoped.hooks, "_promote_fail_open", return_value=recorded) as promote, \
                     mock.patch.object(scoped.hooks, "_record_crash_debug") as debug, mock.patch("sys.stderr", err), \
                     mock.patch.object(self.library, "slugs", return_value=["test-plan", "broken"]):
                    code = scoped.hooks.run_hook("PreToolUse", lambda p: scoped.handler("PreToolUse", p, self.library),
                        stdin=io.StringIO('{"session_id":"root-id"}'), stdout=out, stderr=err)
                self.assertEqual(code, scoped.hooks.EXIT_PROCEED)
                self.assertIn("unverified", err.getvalue())
                tail = scoped.hooks._RECORDED_TAIL if recorded else scoped.hooks._NOT_RECORDED_TAIL
                self.assertIn(tail, err.getvalue())
                promote.assert_called_once()
                debug.assert_called_once()
                self.assertEqual(self.store.path.read_text(), "null")
                self.assertEqual(broken.read_text(), "[]")

    def test_incompatible_reader_reaches_hook_user_without_disclosing_record_contents(self):
        import io
        import telemetry
        err = io.StringIO()
        with mock.patch.object(self.library, "slugs", return_value=["test-plan"]), \
             mock.patch.object(plan_store, "validate_shared_record", side_effect=plan_store.IncompatibleReaderError("private record contents")), \
             mock.patch.object(telemetry, "observe_reader_health", return_value=True), \
             mock.patch.object(scoped.hooks, "_record_crash_debug"), mock.patch("sys.stderr", err):
            result = scoped.handler("PreToolUse", {"session_id": "root-id"}, self.library)
        self.assertEqual(result["action"], "proceed")
        self.assertIn("older than the shared record", err.getvalue())
        self.assertIn("restart the session", err.getvalue())
        self.assertNotIn("private record contents", err.getvalue())

    def test_failure_recorder_errors_do_not_erase_a_healthy_refusal(self):
        import io
        self.launch()
        self.child_read()
        broken = self.library.plan_dir("broken") / scoped.FILENAME
        broken.parent.mkdir(exist_ok=True)
        broken.write_text("[]")
        payload = {"session_id": "root-id", "tool_use_id": "queue", "tool_name": "send_message",
                   "tool_input": {"target": "child-a", "message": "queued"}}
        err = io.StringIO()
        with mock.patch.object(scoped.hooks, "_promote_fail_open", side_effect=OSError("offline")), \
             mock.patch.object(scoped.hooks, "_record_crash_debug", side_effect=OSError("disk")), \
             mock.patch("sys.stderr", err), \
             mock.patch.object(self.library, "slugs", return_value=["test-plan", "broken"]):
            code = scoped.hooks.run_hook("PreToolUse", lambda p: scoped.handler("PreToolUse", p, self.library),
                stdin=io.StringIO(json.dumps(payload)), stdout=io.StringIO(), stderr=err)
        self.assertEqual(code, scoped.hooks.EXIT_BLOCK)
        self.assertIn(scoped.hooks._NOT_RECORDED_TAIL, err.getvalue())
        self.assertEqual(broken.read_text(), "[]")

    def test_worker_partial_status_is_not_finished_work(self):
        self.a = self.store.register(owner=self.owner, root="root-id", purpose="worker", lens=None,
            role="engine-worker-bounded", packet=self.packet, packet_digest=core.digest(self.packet.read_bytes()))
        self.launch()
        self.child_read()
        def verify():
            return self.store.verified_locked(owner=self.owner, root="root-id", lens=None,
                packet_digest=self.a["packet_digest"], assignment_id=self.a["id"])
        for status in ("blocked", "partial", "needs_clarification", "cancelled", "failed", "error"):
            self.stop(json.dumps({"status": status}))
            with self.assertRaises(scoped.EvidenceError):
                verify()
        self.stop(json.dumps({"outcome": "failed", "reason": "Cannot complete", "evidence": {
            "changed_paths": [], "verification_results": [], "assumptions": [], "unresolved_concerns": []}}))
        self.assertEqual(verify()["child"], "child-a")

    def test_relative_native_cat_binds_the_same_immutable_packet(self):
        self.launch()
        self.observe("SubagentStart", child="child-a")
        body = self.packet.read_text()
        meta = {"type": "session_meta", "payload": {"id": "child-a", "source": {"subagent": {
            "thread_spawn": {"parent_thread_id": "root-id", "agent_path": "/root/" + self.a["id"]}}}}}
        event = {"type": "event_msg", "payload": {"type": "item_completed", "thread_id": "child-a",
                 "turn_id": "read-turn", "item": {"type": "CommandExecution", "id": "relative-read",
                 "cwd": self.root.as_uri(), "status": "completed", "exit_code": 0,
                 "stdout": body, "aggregated_output": body}}}
        transcript = self.root / "child.jsonl"
        transcript.write_text(json.dumps(meta) + "\n" + json.dumps(event) + "\n")
        relative = os.path.relpath(self.a["packet_path"], self.root)
        self.observe("PostToolUse", "Bash", {"command": "cat " + relative}, child="child-a",
                     response=body, tool_use_id="relative-read", turn_id="read-turn",
                     transcript_path=str(transcript), cwd=str(self.root))
        self.stop()
        self.assertEqual(self.verified()["child"], "child-a")

    def test_failed_packet_read_can_be_clarified_then_completed_without_erasing_failure(self):
        self.launch()
        self.observe("SubagentStart", child="child-a")
        facts = {"child": "child-a", "root": "root-id", "name": "/root/" + self.a["id"]}
        with mock.patch.object(providers, "scoped_transcript", return_value=facts):
            self.observe("PostToolUse", "Bash", {"command": "cat " + self.a["packet_path"]},
                         child="child-a", response={"exit_code": 1, "stdout": ""}, tool_use_id="failed-read")
        self.stop('{"status":"needs_clarification"}')
        failed = self.store.read()["assignments"][self.a["id"]]
        self.assertIsNone(failed["read"])
        self.assertEqual(len(failed["read_failures"]), 1)
        with self.assertRaises(scoped.EvidenceError):
            self.verified()
        supplement = self.store.clarify(self.a["id"], "root-id", "Read access to the original packet is repaired.")
        args = {"target": "child-a", "message": supplement["path"]}
        self.assertEqual(self.observe("PreToolUse", "followup_task", args, tool_use_id="repair")["action"], "proceed")
        self.observe("PostToolUse", "followup_task", args, tool_use_id="repair", response={"ok": True})
        self.observe("PostToolUse", "Bash", {"command": "cat " + supplement["path"]}, child="child-a",
                     response={"exit_code": 0, "stdout": Path(supplement["path"]).read_text()})
        self.stop()
        with self.assertRaises(scoped.EvidenceError):
            self.verified()  # a delivered supplement and valid final do not replace the packet read
        self.child_read()
        self.stop()
        repaired = self.verified()
        self.assertEqual(repaired["child"], "child-a")
        self.assertEqual(repaired["read_failures"], failed["read_failures"])
        self.assertEqual(repaired["faults"], [])

    def test_exact_blocked_child_metadata_permits_clarification_but_not_review_credit(self):
        self.launch()
        self.observe("SubagentStart", child="child-a")
        facts = {"child": "child-a", "root": "root-id", "name": "/root/" + self.a["id"],
                 "final": '{"status":"needs_clarification"}'}
        with mock.patch.object(providers, "scoped_transcript", return_value=facts):
            self.observe("SubagentStop", child="child-a")
        self.assertEqual(self.store.read()["assignments"][self.a["id"]]["child"], "child-a")
        self.store.clarify(self.a["id"], "root-id", "Use the unchanged original packet path.")
        with self.assertRaises(scoped.EvidenceError):
            self.verified()

    def test_wrong_blocked_child_metadata_cannot_enable_access_clarification(self):
        self.launch()
        facts = {"child": "child-a", "root": "other-root", "name": "/root/" + self.a["id"],
                 "final": '{"status":"needs_clarification"}'}
        with mock.patch.object(providers, "scoped_transcript", return_value=facts):
            self.observe("SubagentStart", child="child-a")
            self.observe("SubagentStop", child="child-a")
        with self.assertRaises(scoped.EvidenceError):
            self.store.clarify(self.a["id"], "root-id", "Do not bind another root's child.")
        with self.assertRaises(scoped.EvidenceError):
            self.verified()

    def test_unobserved_incoming_steering_cannot_earn_review_or_receipt_credit(self):
        self.launch()
        self.child_read()
        self.stop('{"status":"needs_clarification"}')
        incoming = {"author": "/root", "recipient": "/root/" + self.a["id"],
                    "content": [{"type": "input_text", "text": "native header"}, {"type": "encrypted_content", "encrypted_content": "unobserved steering"}]}
        self.stop(messages=[incoming])
        with self.assertRaises(scoped.EvidenceError):
            self.verified()
        # Publishing an orphan acceptance cannot turn the missing send into verified coverage.
        receipt = {"lens": "architecture"}
        def orphan(data):
            data["assignments"][self.a["id"]]["accepted"] = True
            data["acceptances"][scoped.receipt_key(receipt)] = {"owner": self.owner,
                "assignments": [self.a["id"]], "outputs": {self.a["id"]: core.digest("[]")}}
        self.store.change(orphan)
        self.assertFalse(self.store.receipt_verified(receipt, self.owner))

    def test_supplement_path_delivery_requires_full_read_before_acceptance(self):
        self.launch()
        self.child_read()
        supplement = self.store.clarify(self.a["id"], "root-id", "Immutable clarification body")
        args = {"target": "child-a", "message": "Read " + supplement["path"]}
        self.observe("PreToolUse", "followup_task", args, tool_use_id="clarify")
        self.observe("PostToolUse", "followup_task", args, tool_use_id="clarify", response={"ok": True})
        self.stop()
        self.assertTrue(self.store.read()["assignments"][self.a["id"]]["continuations"][0]["delivered"])
        with self.assertRaises(scoped.EvidenceError):
            self.verified()
        for child, body in (("child-b", "Immutable clarification body"), ("child-a", "Immutable")):
            self.observe("PostToolUse", "Bash", {"command": "cat " + supplement["path"]}, child=child,
                         response={"exit_code": 0, "stdout": body})
            self.stop()
            with self.assertRaises(scoped.EvidenceError):
                self.verified()
        self.observe("PostToolUse", "Bash", {"command": "cat " + supplement["path"]}, child="child-a",
                     response={"exit_code": 0, "stdout": "Immutable clarification body"})
        self.stop()
        self.assertEqual(self.verified()["child"], "child-a")

    def test_early_started_child_id_queue_is_blocked_in_either_event_order(self):
        for start_first in (False, True):
            with self.subTest(start_first=start_first):
                self.a = self.register("architecture")
                child = "early-child-" + str(start_first)
                facts = {"child": child, "root": "root-id", "name": "/root/" + self.a["id"]}
                if not start_first:
                    self.launch()
                with mock.patch.object(providers, "scoped_transcript", return_value=facts):
                    self.observe("SubagentStart", child=child)
                if start_first:
                    self.launch()
                observed = self.store.read()["assignments"][self.a["id"]]
                self.assertEqual(observed["child"], child)
                self.assertIsNone(observed["read"])
                self.assertEqual(self.observe("PreToolUse", "send_message", {"target": child, "message": "queued"})["action"], "block")
                with self.assertRaises(scoped.EvidenceError):
                    self.verified()

    def test_early_queue_guard_does_not_wait_for_launch_return_or_ambiguous_binding(self):
        args = {"task_name": self.a["id"], "agent_type": self.a["role"], "fork_turns": "none", "message": "opaque launch"}
        self.observe("PreToolUse", "spawn_agent", args)
        for child in ("early-a", "early-b"):
            facts = {"child": child, "root": "root-id", "name": "/root/" + self.a["id"]}
            with mock.patch.object(providers, "scoped_transcript", return_value=facts):
                self.observe("SubagentStart", child=child)
            self.assertEqual(self.observe("PreToolUse", "send_message", {"target": child, "message": "queued"})["action"], "block")
        self.observe("PostToolUse", "spawn_agent", args, response={"task_name": "/root/" + self.a["id"]})
        self.assertIsNone(self.store.read()["assignments"][self.a["id"]]["child"])
        for child in ("early-a", "early-b"):
            self.assertEqual(self.observe("PreToolUse", "send_message", {"target": child, "message": "queued"})["action"], "block")

    def test_unknown_or_wrong_early_child_identity_never_claims_user_task(self):
        self.launch()
        for facts in ({}, {"child": "child-a", "root": "wrong", "name": "/root/" + self.a["id"]},
                      {"child": "child-a", "root": "root-id", "name": "/root/user-task"}):
            with mock.patch.object(providers, "scoped_transcript", return_value=facts):
                self.observe("SubagentStart", child="child-a")
            self.assertIsNone(self.store.read()["assignments"][self.a["id"]]["child"])
        self.assertEqual(self.observe("PreToolUse", "send_message", {"target": "user-task", "message": "hello"})["action"], "proceed")

    def test_codex_old_stop_without_control_observation_remains_unverified(self):
        self.launch()
        self.child_read()
        self.stop()
        self.assertEqual(self.verified()["child"], "child-a")
        self.store.change(lambda data: data["assignments"][self.a["id"]]["stops"][-1].pop("control_verified"))
        with self.assertRaises(scoped.EvidenceError):
            self.verified()

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
                   "content": [{"type": "input_text", "text": "native header"}, {"type": "encrypted_content", "encrypted_content": args["message"]}]}
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
        self.stop(messages=[])
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
        with self.assertRaisesRegex(scoped.EvidenceError, "contradictory child start identity"):
            self.child_read()
        data = self.store.read()["assignments"]
        self.assertIsNone(data[self.a["id"]]["child"])
        self.assertEqual(data[original["id"]]["child"], "child-a")
        with self.assertRaises(scoped.EvidenceError):
            self.verified()

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
        self.claude_clarification()

    def test_claude_failed_read_then_access_clarification_preserves_failed_attempt(self):
        self.claude_clarification(failed_read=True)

    def claude_clarification(self, failed_read=False):
        # Documented Agent/Read/SubagentStop shapes; no fabricated Claude transcript envelope.
        with mock.patch.dict(os.environ, {providers.PROVIDER_ENV: "claude"}):
            args = {"subagent_type": self.a["role"], "prompt": "Read " + self.a["packet_path"]}
            self.assertEqual(self.observe("PreToolUse", "Agent", args)["action"], "proceed")
            self.observe("SubagentStart", child="child-a")
            self.observe("PostToolUse", "Read", {"file_path": self.a["packet_path"]}, child="child-a",
                         response={"isError": True} if failed_read else {"file": {"content": self.packet.read_text()}})
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
            if failed_read:
                with self.assertRaises(scoped.EvidenceError):
                    self.verified()  # even a read supplement cannot replace the original packet
                self.observe("PostToolUse", "Read", {"file_path": self.a["packet_path"]}, child="child-a",
                             response={"file": {"content": self.packet.read_text()}})
                self.observe("SubagentStop", child="child-a", last_assistant_message="[]")
                self.assertEqual(len(self.verified()["read_failures"]), 1)
            self.assertEqual(self.verified()["child"], "child-a")

    def test_duplicate_launch_observation_is_idempotent_but_new_launch_is_not(self):
        self.launch()
        self.assertEqual(self.launch()["action"], "proceed")
        args = {"task_name": self.a["id"], "agent_type": self.a["role"], "fork_turns": "none", "message": "opaque launch"}
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


class NativeShellCompletion(unittest.TestCase):
    """Sanitized native CommandExecution shape observed during actual Desktop PostToolUse."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "native.jsonl"
        self.meta = {"type": "session_meta", "payload": {"id": "child-a", "source": {
            "subagent": {"thread_spawn": {"parent_thread_id": "root-id", "agent_path": "/root/assignment"}}}}}
        self.item = {"type": "CommandExecution", "id": "exec-read", "status": "completed", "exit_code": 0,
                     "stdout": "whole packet\n", "stderr": "", "aggregated_output": "whole packet\n"}
        self.event = {"type": "event_msg", "payload": {"type": "item_completed", "thread_id": "child-a",
                      "turn_id": "turn-a", "item": self.item}}
        self.payload = {"session_id": "root-id", "agent_id": "child-a", "turn_id": "turn-a",
                        "tool_use_id": "exec-read", "tool_name": "Bash", "tool_response": "whole packet\n",
                        "transcript_path": str(self.path)}
        self.env = mock.patch.dict(os.environ, {providers.PROVIDER_ENV: "codex"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def check(self, extra=()):
        self.path.write_text("\n".join(json.dumps(r) for r in (self.meta, self.event, *extra)) + "\n")
        return providers.scoped_read_succeeded(self.payload, "whole packet\n")

    def test_exact_success_and_failed_command_with_identical_output(self):
        self.assertTrue(self.check())
        self.item.update(exit_code=7, status="failed")
        self.assertFalse(self.check())

    def test_completion_identity_and_output_are_required(self):
        for section, key, value in [(self.event["payload"], "thread_id", "wrong-child"),
                                    (self.event["payload"], "turn_id", "old-turn"),
                                    (self.item, "id", "other-call"), (self.item, "type", "other-tool"),
                                    (self.item, "status", "running"), (self.item, "exit_code", None),
                                    (self.item, "exit_code", False), (self.item, "aggregated_output", "different"),
                                    (self.item, "stdout", "")]:
            with self.subTest(key=key, value=value):
                old = section[key]
                section[key] = value
                self.assertFalse(self.check())
                section[key] = old
        self.meta["payload"]["source"]["subagent"]["thread_spawn"]["parent_thread_id"] = "other-root"
        self.assertFalse(self.check())
        self.meta["payload"]["id"] = "other-child"
        self.assertFalse(self.check())

    def test_missing_duplicate_and_malformed_evidence_stays_unverified(self):
        self.assertFalse(self.check([self.event]))
        self.path.write_text("not json")
        self.assertFalse(providers.scoped_read_succeeded(self.payload, "whole packet\n"))
        self.path.unlink()
        self.assertFalse(providers.scoped_read_succeeded(self.payload, "whole packet\n"))

    def test_relative_cat_requires_the_observed_directory_and_simple_command(self):
        self.item["cwd"] = self.path.parent.as_uri()
        self.payload["tool_input"] = {"command": "cat -- packet.md"}
        target = str(self.path.parent / "packet.md")
        self.check()
        self.assertTrue(providers.scoped_reads_path(self.payload, target))
        self.assertFalse(providers.scoped_reads_path(self.payload, str(self.path.parent / "other.md")))
        self.item["cwd"] = "/another/directory"
        self.check()
        self.assertFalse(providers.scoped_reads_path(self.payload, target))
        self.payload["tool_input"]["command"] = "cd elsewhere && cat packet.md"
        self.assertFalse(providers.scoped_reads_path(self.payload, target))

    def test_json_printed_by_a_command_cannot_claim_its_own_exit_code(self):
        self.payload["tool_response"] = json.dumps({"exit_code": 0, "stdout": "whole packet\n"})
        self.item.update(status="failed", exit_code=7, aggregated_output=self.payload["tool_response"])
        self.assertFalse(self.check())


class ScopedAgentHookRunner(unittest.TestCase):
    """Documented Claude envelopes through the real shell runner and candidate CLI.

    These are offline contract tests, not live Anthropic model execution. The private fixture
    library is the only evidence store. No production plan or Build receives fixture evidence.
    """
    def setUp(self):
        self.fixture = ScopedAssignments()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        slug = "test-plan--a1b2c3"
        self.fixture.library._mkdir(self.fixture.library.plan_dir(slug))
        self.fixture.library._write_json(self.fixture.library._record_path(slug), {})
        self.fixture.store = scoped.Store(self.fixture.library, slug)
        self.fixture.a = self.fixture.register("architecture")
        self.fixture.observe = self.observe

    def observe(self, event, tool=None, inp=None, child=None, response=None, **kw):
        import subprocess
        import sys
        payload = {"hook_event_name": event, "session_id": "root-id", "tool_use_id": "call-1", **kw}
        if tool:
            payload.update(tool_name=tool, tool_input=inp or {})
        if child:
            payload.update(agent_id=child, agent_type=self.fixture.a["role"])
        if response is not None:
            payload["tool_response"] = response
        tools = Path(__file__).resolve().parent
        env = {**os.environ, plan_store.ENV_DIR: str(self.fixture.library.root)}
        result = subprocess.run(["sh", str(tools / "hook-runner.sh"), sys.executable,
                                 str(tools / "scoped_agents.py"), event],
                                input=json.dumps(payload), text=True, capture_output=True,
                                env=env, cwd=self.fixture.root, timeout=20)
        self.assertIn(result.returncode, (0, 2), result.stderr)
        if result.returncode == 0:
            # This envelope-only fixture has no registered Git checkout. The health
            # producer must disclose that it cannot persist recovery evidence there.
            self.assertIn(result.stderr, ("", "Engine reader health could not be recorded; "
                "automatic recovery remains unverified.\n"), "an allowed call must not hide a hook crash")
        return {"action": "block" if result.returncode == 2 else "proceed"}

    def test_claude_partial_then_same_child_clarification(self):
        self.fixture.test_claude_packet_and_clarification_reads_with_actual_returned_child()

    def test_claude_failed_read_can_recover_through_the_real_hook_runner(self):
        self.fixture.test_claude_failed_read_then_access_clarification_preserves_failed_attempt()

    def test_claude_fork_and_resume_cannot_start_a_fresh_assignment(self):
        f = self.fixture
        with mock.patch.dict(os.environ, {providers.PROVIDER_ENV: "claude"}):
            for extra in ({"resume": "old-child"}, {"fork_context": True}, {"subagent_type": "fork"}):
                args = {"subagent_type": f.a["role"], "prompt": "Read " + f.a["packet_path"], **extra}
                self.assertEqual(self.observe("PreToolUse", "Agent", args)["action"], "block")
            self.assertIsNone(f.store.read()["assignments"][f.a["id"]]["launch"])

    def test_claude_missing_child_start_and_partial_final_cannot_earn_credit(self):
        f = self.fixture
        with mock.patch.dict(os.environ, {providers.PROVIDER_ENV: "claude"}):
            args = {"subagent_type": f.a["role"], "prompt": "Read " + f.a["packet_path"]}
            self.observe("PreToolUse", "Agent", args)
            self.observe("PostToolUse", "Agent", args, response={"agentId": "child-a"})
            self.observe("PostToolUse", "Read", {"file_path": f.a["packet_path"]}, child="child-a",
                         response={"file": {"content": f.packet.read_text()}})
            self.observe("SubagentStop", child="child-a", last_assistant_message="[]")
            with self.assertRaises(scoped.EvidenceError):
                f.verified()
            self.observe("SubagentStart", child="child-a")
            self.observe("SubagentStop", child="child-a", last_assistant_message='{"status":"blocked"}')
            with self.assertRaises(scoped.EvidenceError):
                f.verified()

    def test_claude_failed_launch_cannot_earn_credit_from_a_final_message(self):
        f = self.fixture
        with mock.patch.dict(os.environ, {providers.PROVIDER_ENV: "claude"}):
            args = {"subagent_type": f.a["role"], "prompt": "Read " + f.a["packet_path"]}
            self.observe("PreToolUse", "Agent", args)
            self.observe("SubagentStart", child="child-a")
            self.observe("PostToolUse", "Read", {"file_path": f.a["packet_path"]}, child="child-a",
                         response={"file": {"content": f.packet.read_text()}})
            self.observe("SubagentStop", child="child-a", last_assistant_message="[]")
            self.observe("PostToolUse", "Agent", args, response={"agentId": "child-a"}, is_error=True)
            with self.assertRaises(scoped.EvidenceError):
                f.verified()

    def test_missing_companion_does_not_create_execution_credit(self):
        f = self.fixture
        f.store.path.unlink()
        with mock.patch.dict(os.environ, {providers.PROVIDER_ENV: "claude"}):
            args = {"subagent_type": f.a["role"], "prompt": "Read " + f.a["packet_path"]}
            self.assertEqual(self.observe("PreToolUse", "Agent", args)["action"], "proceed")
            self.observe("SubagentStart", child="child-a")
            self.observe("SubagentStop", child="child-a", last_assistant_message="[]")
            with self.assertRaises(scoped.EvidenceError):
                f.verified()
            self.assertFalse(f.store.path.exists())

    def test_claude_two_concurrent_launches_keep_their_packet_and_child(self):
        f = self.fixture
        assignments = [f.a, f.register("feasibility")]
        with mock.patch.dict(os.environ, {providers.PROVIDER_ENV: "claude"}):
            def launch(pair):
                index, assignment = pair
                args = {"subagent_type": assignment["role"], "prompt": "Read " + assignment["packet_path"]}
                return self.observe("PreToolUse", "Agent", args, tool_use_id="launch-" + str(index))
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                self.assertTrue(all(r["action"] == "proceed" for r in pool.map(launch, enumerate(assignments))))
            # Reverse completion order; no nearest-start or timestamp join can satisfy this test.
            for index in (1, 0):
                f.a = assignments[index]
                child = "child-" + str(index)
                args = {"subagent_type": f.a["role"], "prompt": "Read " + f.a["packet_path"]}
                self.observe("SubagentStart", child=child)
                self.observe("PostToolUse", "Read", {"file_path": f.a["packet_path"]}, child=child,
                             response={"file": {"content": f.packet.read_text()}})
                self.observe("SubagentStop", child=child, last_assistant_message="[]")
                self.observe("PostToolUse", "Agent", args, response={"agentId": child}, tool_use_id="launch-" + str(index))
                self.assertEqual(f.store.verified_locked(owner=f.owner, root="root-id", lens=f.a["lens"],
                    packet_digest=f.a["packet_digest"], assignment_id=f.a["id"])["child"], child)


if __name__ == "__main__":
    unittest.main()
