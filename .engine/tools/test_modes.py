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

    def test_describe_explore_scope_is_self_labelled_and_faithful_to_the_gate(self):
        """The assistant-facing Explore-scope copy must name every allowed carve-out and every denied
        building action, be self-labelled "don't relay", and stay TRUE to the gate it describes — so an AI
        session does not over-restrict (the bug: switching to Build merely to log a GitHub issue). This is
        the fidelity pin between the prose and is_building_action / _MUTATING_TOOLS / _BASH_BUILD_PATTERNS:
        if the gate's allow/deny set changes, this test forces the copy to change with it."""
        scope = modes.describe_explore_scope().lower()
        # self-labelled assistant-facing: it must NOT be relayed into the operator-presentation channel
        self.assertIn("don't relay", scope)
        # every allowed carve-out the gate actually permits in Explore is named (no thin list)
        for allowed in ("read", "test", "search", "subagent", "plan file", "gh issue"):
            self.assertIn(allowed, scope, f"Explore-scope copy must name the ALLOWED action {allowed!r}")
        # the denied building set is named — and NOT path-scoped (the gate denies edits by tool, any file)
        for denied in ("edit or write any files", "branch", "commit", "pull request"):
            self.assertIn(denied, scope, f"Explore-scope copy must name the DENIED action {denied!r}")
        # fidelity to the live gate: what the copy calls "allowed" the gate allows; "denied" it denies
        self.assertTrue(_allow(modes.handler(_explore_payload("Bash", "gh issue create -t x -b y"))))
        self.assertTrue(_allow(modes.handler(_explore_payload("Read"))))
        self.assertTrue(_deny(modes.handler(_explore_payload("Bash", "git commit -m x"))))
        self.assertTrue(_deny(modes.handler(_explore_payload("Bash", "gh pr create"))))
        self.assertTrue(_deny(modes.handler(_explore_payload("Write"))))


class TestPlanArtifactCarveOut(unittest.TestCase):
    """#64 (D-177/D-178): in Explore the gate EXEMPTS Claude Code's native plan file — recognized by the
    platform's own marker (`permission_mode == "plan"` / `is_plan_file`), NEVER a path — while every other
    write stays denied. (On the current platform `is_plan_file` appears only in conversation text, so the
    live marker is `permission_mode == "plan"`; the exact field is a build-spec leaf, D-178.)"""

    def _payload(self, tool_name, permission_mode=None, tool_input=None):
        # session_id=None -> current_stance is Explore (the gated default); no signal file needed.
        return {"session_id": None, "tool_name": tool_name,
                "tool_input": dict(tool_input or {}), "permission_mode": permission_mode}

    def test_plan_file_write_in_plan_mode_is_allowed(self):
        self.assertTrue(_allow(modes.handler(self._payload("Write", permission_mode="plan"))))
        self.assertTrue(_allow(modes.handler(self._payload("Edit", permission_mode="plan"))))

    def test_plan_file_allowed_even_when_plansdir_is_inside_the_repo(self):
        # marker-not-path: a plan folder relocated INTO the repo must not re-trip the gate.
        d = modes.handler(self._payload("Write", permission_mode="plan",
                                        tool_input={"file_path": ".engine/plans/p.md"}))
        self.assertTrue(_allow(d))

    def test_is_plan_file_flag_is_allowed(self):
        # the belt-and-suspenders marker: a platform that flags the write as the plan file is honored too.
        self.assertTrue(_allow(modes.handler(self._payload("Write", tool_input={"is_plan_file": True}))))

    def test_non_plan_engine_source_write_stays_denied(self):
        d = modes.handler(self._payload("Write", permission_mode="default",
                                        tool_input={"file_path": ".engine/tools/x.py"}))
        self.assertTrue(_deny(d))

    def test_non_plan_home_claude_settings_write_stays_denied(self):
        # a ~/.claude/settings.json write carries no plan marker -> denied (it has no merge backstop).
        d = modes.handler(self._payload("Write", permission_mode="default",
                                        tool_input={"file_path": "~/.claude/settings.json"}))
        self.assertTrue(_deny(d))

    def test_write_without_permission_mode_stays_denied(self):
        # pm absent (older platform / a plain edit) -> not the artifact -> denied (old behavior preserved).
        self.assertTrue(_deny(modes.handler(self._payload("Write"))))

    def test_build_verbs_not_exempted_even_in_plan_mode(self):
        # the carve-out is the file-mutating tools specifically; a commit/branch/PR is never the artifact.
        for cmd in ("git commit -m x", "gh pr create", "git checkout -b f"):
            d = modes.handler(self._payload("Bash", permission_mode="plan", tool_input={"command": cmd}))
            self.assertTrue(_deny(d), f"{cmd!r} must stay denied even under permission_mode=plan")

    def test_is_plan_artifact_predicate(self):
        self.assertTrue(modes.is_plan_artifact("Write", {}, "plan"))
        self.assertTrue(modes.is_plan_artifact("Edit", {"is_plan_file": True}, None))
        self.assertFalse(modes.is_plan_artifact("Write", {}, "default"))
        self.assertFalse(modes.is_plan_artifact("Write", {}, None))
        self.assertFalse(modes.is_plan_artifact("Bash", {"command": "x"}, "plan"))  # not a file-mutating tool


class TestPlanAcceptanceBuildEntry(unittest.TestCase):
    """#67 (D-179/D-180): a PostToolUse on the plan-exit completion (`ExitPlanMode`) flips the stance to
    Build; every other completion leaves it untouched; the handler ALWAYS proceeds and emits no text; and
    a rejected plan fires no PostToolUse so the stance stays Explore (fail-safe to the floor)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(modes.tempfile, "gettempdir", return_value=self._tmp.name)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_accepting_a_plan_enters_build(self):
        self.assertEqual(modes.current_stance("s"), modes.EXPLORE)
        d = modes.accept_handler({"session_id": "s", "tool_name": "ExitPlanMode"})
        self.assertEqual(d.get("action"), "proceed")               # never blocks, never decides
        self.assertEqual(modes.current_stance("s"), modes.BUILD)

    def test_non_plan_exit_completion_does_not_enter_build(self):
        # The false-fire guard: ONLY ExitPlanMode enters Build. A subagent's inner tool calls do not fire
        # the parent PostToolUse, and a leave-without-approving fires no ExitPlanMode (platform behavior,
        # live-confirmed); this locks that the handler keys solely on the ExitPlanMode completion event.
        for tool in ("Edit", "Task", "Bash", "Read", "SomeFutureTool"):
            modes.accept_handler({"session_id": "s", "tool_name": tool})
            self.assertEqual(modes.current_stance("s"), modes.EXPLORE, f"{tool} must not enter Build")

    def test_handler_always_proceeds_and_tolerates_a_bad_payload(self):
        self.assertEqual(modes.accept_handler({"tool_name": "ExitPlanMode"}).get("action"), "proceed")
        self.assertEqual(modes.accept_handler({}).get("action"), "proceed")
        self.assertEqual(modes.accept_handler({"session_id": "s"}).get("action"), "proceed")  # no tool_name
        self.assertEqual(modes.current_stance("s"), modes.EXPLORE)        # none of those entered Build

    def test_end_to_end_via_run_hook_sets_build_and_proceeds(self):
        out, err = io.StringIO(), io.StringIO()
        payload = json.dumps({"session_id": "s", "tool_name": "ExitPlanMode"})
        code = hooks.run_hook("PostToolUse", modes.accept_handler,
                              stdin=io.StringIO(payload), stdout=out, stderr=err)
        self.assertEqual(code, hooks.EXIT_PROCEED)        # PostToolUse always proceeds (exit 0)
        self.assertEqual(out.getvalue(), "")              # sets a signal; emits no text
        self.assertEqual(modes.current_stance("s"), modes.BUILD)

    def test_main_accept_hook_routes_to_posttooluse(self):
        with mock.patch.object(modes.hooks, "run_hook", return_value=0) as rh:
            self.assertEqual(modes.main(["accept-hook"]), 0)
        rh.assert_called_once_with("PostToolUse", modes.accept_handler)

    def test_wired_command_ends_in_accept_hook(self):
        manifest_path = os.path.join(validate.ROOT, ".engine", "modules", "core", "manifest.json")
        with open(manifest_path, encoding="utf-8") as fh:
            wires = json.load(fh)["wires"]
        post = [w for w in wires if w.get("type") == "hook" and w.get("event") == "PostToolUse"]
        self.assertTrue(post, "core manifest must wire a PostToolUse hook (the modes plan-acceptance trigger)")
        self.assertTrue(any(w["hook"]["command"].rstrip().endswith(" accept-hook") for w in post),
                        "the PostToolUse modes wire must invoke `modes.py accept-hook`")


class TestResolveSession(unittest.TestCase):
    """The /engine-start (slice 26a) session-id resolution: the skill body passes
    `--session "${CLAUDE_CODE_SESSION_ID}"` (the shell expands that env var), with a fallback to reading
    the CLAUDE_CODE_SESSION_ID env var directly so the Build verb still resolves the real session when the
    argument arrives empty or unexpanded."""

    def test_explicit_session_wins(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "from-env"}):
            self.assertEqual(modes._resolve_session(["--session", "explicit"]), "explicit")

    def test_falls_back_to_env_when_absent(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "from-env"}):
            self.assertEqual(modes._resolve_session([]), "from-env")

    def test_falls_back_to_env_on_unexpanded_token(self):
        # a shell that did not expand the env var passes the literal ${CLAUDE_CODE_SESSION_ID}
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "from-env"}):
            self.assertEqual(modes._resolve_session(["--session", "${CLAUDE_CODE_SESSION_ID}"]), "from-env")

    def test_none_when_neither_present(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(modes._resolve_session([]))

    def test_set_build_cli_uses_env_fallback(self):
        # `modes.py set-build` with no --session resolves the env session and enters Build for it
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(modes.tempfile, "gettempdir", return_value=tmp), \
                mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "cli-env-session"}):
            self.assertEqual(modes.main(["set-build"]), 0)
            self.assertEqual(modes.current_stance("cli-env-session"), modes.BUILD)

    def test_set_build_makes_the_gate_allow_writes_for_that_session(self):
        # The end-to-end modes contract the Build verb relies on: set-build for a session id makes the
        # PreToolUse gate PERMIT a write for THAT SAME id (one it denies in explore), and ONLY that id.
        # Pins that the marker set-build writes is the one the gate reads — the binding the verb depends
        # on, independent of how the platform sources the id into --session.
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(modes.tempfile, "gettempdir", return_value=tmp):
            self.assertEqual(modes.main(["set-build", "--session", "sX"]), 0)
            allow = modes.handler({"session_id": "sX", "tool_name": "Edit", "tool_input": {}})
            self.assertTrue(_allow(allow), "the gate allows a write for the session that entered Build")
            other = modes.handler({"session_id": "sOther", "tool_name": "Edit", "tool_input": {}})
            self.assertTrue(_deny(other), "a different session stays in explore — the marker is session-keyed")


if __name__ == "__main__":
    unittest.main()
