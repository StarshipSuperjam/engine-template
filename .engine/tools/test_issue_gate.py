#!/usr/bin/env python3
"""Tests for issue_gate — the engine-Issue reroute matcher.

These lock the load-bearing behaviours a non-engineer cannot read code to verify: that EVERY direct
engine-labelled Issue creation is rerouted (a reason returned) regardless of body shape — a Bash `gh`/API
form, a heredoc, or a connector issue-creation tool; that an unlabelled, other-labelled, or out-of-scope call
is allowed (None); that label detection is PRECISE (an innocent body that merely mentions "engine"/"label" is
never denied); that the matcher fails open on anything it cannot parse; and — the drift pin — that the helper's
real output carries every CONTRACT_MARKER, so an operator-facing copy change to the framing/headers breaks THIS
test rather than the CI backstop silently.
"""
from __future__ import annotations

import shlex
import unittest
from unittest import mock

import issue_author
import issue_gate
import quiet_call  # capture a demo walkthrough's stdout so it can't bury the suite summary

# A conforming body is whatever the helper actually renders. Under the widened gate it is rerouted just like a
# free-text body (the create CLI is the supported path, not a hand-rolled `gh` with a helper-rendered body).
CONFORMING = issue_author.render_engine_issue_body(what_this_is="a demo item", whats_next="nothing to do")
FREE_TEXT = "just some free text with no contract markers at all"


def _reason(command: str, **kwargs):
    """The gate's verdict for a Bash command string: a reason str (reroute) or None (allow)."""
    return issue_gate.reroute_reason("Bash", {"command": command}, **kwargs)


def _create(body: str, *, label: str | None = "engine", flag: str = "-b") -> str:
    """A `gh issue create` command with an inline body, optionally labelled."""
    parts = ["gh", "issue", "create", "--title", "t", flag, shlex.quote(body)]
    if label is not None:
        parts += ["--label", label]
    return " ".join(parts)


class TestEveryEngineCreationReroutes(unittest.TestCase):
    """Every direct engine-labelled creation returns the redirect reason — the widened contract: the body's
    shape no longer decides, only that it is an engine-labelled creation."""

    def test_inline_free_text_is_rerouted(self):
        self.assertIsNotNone(_reason(_create(FREE_TEXT)))

    def test_inline_CONFORMING_body_is_still_rerouted(self):
        # The key behaviour change: even a body that already matches the contract is rerouted, because the
        # supported path is the create CLI (trusted target + label-by-construction), not `gh` with a good body.
        self.assertIsNotNone(_reason(_create(CONFORMING)))

    def test_label_equals_form_is_rerouted(self):
        self.assertIsNotNone(_reason(f"gh issue create --label=engine -b {shlex.quote(FREE_TEXT)}"))

    def test_engine_in_a_comma_list_is_rerouted(self):
        self.assertIsNotNone(_reason(f"gh issue create --label engine,bug -b {shlex.quote(FREE_TEXT)}"))

    def test_gh_api_field_form_is_rerouted(self):
        cmd = ("gh api repos/o/r/issues -X POST "
               f"-f {shlex.quote('labels[]=engine')} -f {shlex.quote('body=' + FREE_TEXT)}")
        self.assertIsNotNone(_reason(cmd))

    def test_heredoc_engine_creation_is_rerouted(self):
        cmd = "gh issue create --label engine --body-file - <<'EOF'\n" + FREE_TEXT + "\nEOF"
        self.assertIsNotNone(_reason(cmd))

    def test_chained_command_is_rerouted(self):
        self.assertIsNotNone(_reason("cd /tmp && " + _create(FREE_TEXT)))

    def test_reason_names_the_private_runtime_envelope_and_no_fallback(self):
        reason = _reason(_create(FREE_TEXT))
        self.assertIn(".engine/tools/issue_author.py", reason)   # the in-repo helper, not a cross-repo path
        self.assertIn("create", reason)                          # points at the supported create path
        self.assertIn("--confirm", reason)
        self.assertIn("--frozen", reason)
        self.assertIn("submission_id", reason)
        self.assertIn("assessment", reason)
        self.assertIn("Product scope", reason)
        self.assertIn(".engine/schemas/issue-submission-input.v1.json", reason)
        self.assertNotIn("drop the `engine` label", reason)

    def test_redirect_examples_are_accepted_by_the_helper(self):
        import json
        examples = [json.loads(line.strip()) for line in issue_gate.DENY_REASON.splitlines()
                    if line.strip().startswith('{')]
        self.assertEqual([example['scope'] for example in examples], ['engine', 'product'])
        for example in examples:
            self.assertEqual(issue_author.submission_input(example), example)



class TestConnectorArm(unittest.TestCase):
    """A connector issue-creation tool (name ends `github_create_issue`) is rerouted when it carries the engine
    label, and only then — the label is read from the structured input, never inferred from prose."""

    def test_the_real_github_mcp_tool_name_is_rerouted(self):
        # S1 regression: the official GitHub MCP server exposes `mcp__github__create_issue` (harness
        # double-underscore naming), which a literal `github_create_issue` suffix would MISS. The
        # ends-in-create_issue + contains-github rule catches it; jira does not (see below).
        for name in ("mcp__github__create_issue", "mcp__composio__github_create_issue", "github_create_issue"):
            self.assertIsNotNone(issue_gate.reroute_reason(name, {"title": "x", "labels": ["engine"]}),
                                 f"{name} carrying the engine label must reroute")

    def test_connector_with_engine_label_is_rerouted(self):
        self.assertIsNotNone(issue_gate.reroute_reason(
            "mcp__github__github_create_issue", {"title": "x", "labels": ["engine", "bug"]}))

    def test_connector_with_engine_label_as_comma_string_is_rerouted(self):
        self.assertIsNotNone(issue_gate.reroute_reason(
            "mcp__github__github_create_issue", {"title": "x", "labels": "engine,bug"}))

    def test_connector_without_engine_label_is_allowed(self):
        self.assertIsNone(issue_gate.reroute_reason(
            "mcp__github__github_create_issue", {"title": "x", "labels": ["bug"]}))

    def test_current_connector_repository_full_name_routes_unlabelled_trusted_create(self):
        self.assertIsNotNone(issue_gate.reroute_reason(
            "mcp__codex_apps__github_create_issue",
            {"title": "x", "repository_full_name": "trusted/project"},
            trusted_targets=["trusted/project"]))

    def test_legacy_connector_owner_repo_and_external_repository_stay_distinct(self):
        self.assertIsNotNone(issue_gate.reroute_reason(
            "mcp__github__create_issue", {"owner": "trusted", "repo": "project"},
            trusted_targets=["trusted/project"]))
        self.assertIsNone(issue_gate.reroute_reason(
            "mcp__github__create_issue", {"repository": "elsewhere/project"},
            trusted_targets=["trusted/project"]))

    def test_connector_with_no_labels_field_is_allowed(self):
        self.assertIsNone(issue_gate.reroute_reason("some__github_create_issue", {"title": "x"}))

    def test_similarly_named_but_not_a_github_creator_is_allowed(self):
        # a `create_issue` that does not end in the precise suffix is not swept in
        self.assertIsNone(issue_gate.reroute_reason("jira_create_issue", {"labels": ["engine"]}))


class TestAllows(unittest.TestCase):
    """An unlabelled, other-labelled, or out-of-scope call is allowed (None) — the channel stays narrow."""

    def test_unlabelled_free_text_is_allowed(self):
        self.assertIsNone(_reason(_create(FREE_TEXT, label=None)))

    def test_other_label_is_allowed(self):
        self.assertIsNone(_reason(_create(FREE_TEXT, label="bug")))

    def test_reads_and_non_creations_are_allowed(self):
        for cmd in ("gh issue view 5", "gh issue list --label engine",
                    "gh issue comment 5 --body whatever", "gh issue edit 5 --add-label engine"):
            self.assertIsNone(_reason(cmd), f"{cmd!r} must be allowed")

    def test_pr_creation_is_allowed(self):
        self.assertIsNone(_reason(f"gh pr create --label engine -b {shlex.quote(FREE_TEXT)}"))

    def test_non_bash_non_connector_tool_is_allowed(self):
        self.assertIsNone(issue_gate.reroute_reason("Edit", {"file_path": "/x"}))
        self.assertIsNone(issue_gate.reroute_reason("Bash", {}))   # empty command

    def test_echoed_creation_command_is_not_a_creation(self):
        # command-position anchored: the verb inside an argument (echo/grep) is not a real invocation
        self.assertIsNone(_reason('echo gh issue create --label engine -b "free text"'))
        self.assertIsNone(_reason('grep "gh issue create" notes.md'))


class TestTrustedTargetRouting(unittest.TestCase):
    """Unlabelled direct creates route only after an offline trusted-target match."""

    def test_porcelain_and_exact_rest_collection_route_trusted_targets(self):
        targets = ["trusted/project"]
        self.assertIsNotNone(_reason("gh issue create -R trusted/project -t x", trusted_targets=targets))
        self.assertIsNotNone(_reason("gh api repos/trusted/project/issues -f title=x", trusted_targets=targets))

    def test_explicit_get_item_comment_and_external_target_do_not_route(self):
        targets = ["trusted/project"]
        for command in (
                "gh api -X GET repos/trusted/project/issues",
                "gh api -X POST repos/trusted/project/issues/12",
                "gh api -X POST repos/trusted/project/issues/12/comments",
                "gh issue create -R elsewhere/project -t x"):
            self.assertIsNone(_reason(command, trusted_targets=targets), command)

    def test_command_boundaries_prevent_target_and_label_leaks(self):
        targets = ["trusted/project"]
        self.assertIsNone(_reason(
            "gh issue create -R elsewhere/project -t x; echo --label engine",
            trusted_targets=targets))
        self.assertIsNone(_reason(
            "gh api repos/elsewhere/project/issues -f title=x && echo --repo trusted/project",
            trusted_targets=targets))

    def test_newline_boundaries_route_trusted_creates_without_flag_leaks(self):
        targets = ["trusted/project"]
        self.assertIsNotNone(_reason("echo ready\ngh issue create -R trusted/project -t x",
                                     trusted_targets=targets))
        self.assertIsNone(_reason("gh issue create -R elsewhere/project -t x\necho --label engine",
                                  trusted_targets=targets))

    def test_newline_boundaries_handle_comments_quotes_continuations_and_repeated_separators(self):
        targets = ["trusted/project"]
        self.assertIsNotNone(_reason("echo ready # preparation\n# still preparation\ngh issue create -R trusted/project -t x",
                                     trusted_targets=targets))
        self.assertIsNone(_reason("echo 'gh issue create\n--label engine'\necho ready",
                                  trusted_targets=targets))
        continued = "gh issue " + "\\" + "\n" + "create -R trusted/project -t x"
        self.assertIsNotNone(_reason(continued, trusted_targets=targets))
        self.assertIsNotNone(_reason("true && && gh issue create -R trusted/project -t x",
                                     trusted_targets=targets))

    def test_normalized_modes_handler_routes_newline_create_in_every_stance(self):
        import modes
        import providers
        with mock.patch.object(issue_gate, "_trusted_repositories", return_value=["trusted/project"]):
            for stance in (modes.EXPLORE, modes.BUILD, modes.ROUTINE):
                payload = providers.normalize("PreToolUse", {
                    "session_id": "newline-routing", "tool_name": "exec_command",
                    "tool_input": {"cmd": "echo ready\ngh issue create -R trusted/project -t x"},
                })
                with mock.patch.object(modes, 'current_stance', return_value=stance):
                    self.assertEqual(modes.handler(payload).get("permissionDecision"), "deny", stance)

    def test_heredoc_payload_is_data_and_following_commands_still_route(self):
        literal = "gh issue create --label engine"
        for opener, ending in (("<<'EOF'", "EOF"), ('<<"EOF"', "EOF"),
                               ("<<E'OF'", "EOF"), (r"<<\EOF", "EOF"),
                               ("<<-EOF", "\tEOF"), ("<<''", "")):
            command = f"cat {opener}\n{literal}\n{ending}\n"
            with self.subTest(opener=opener):
                self.assertIsNone(_reason(command))
                self.assertIsNotNone(_reason(command + literal))
        self.assertIsNone(_reason("cat <<'EOF'\ngh issue create --repo $REPO\nEOF"))
        self.assertIsNone(issue_gate.classification_limitation(
            "Bash", {"command": "cat <<'EOF'\ngh issue create --repo $REPO\nEOF"}))
        self.assertIsNone(_reason(f"cat <<ONE <<'TWO'\n{literal}\nONE\n{literal}\nTWO"))
        self.assertIsNotNone(_reason(f"cat <<ONE <<'TWO'\n{literal}\nONE\n{literal}\nTWO\n{literal}"))
        self.assertIsNotNone(_reason(f"{literal} --body-file - <<'EOF'\nbody\nEOF"))

    def test_quoted_redirection_and_here_string_do_not_consume_later_commands(self):
        for prefix in ("echo '<<EOF'", 'echo "<<EOF"', "cat <<< 'text'", "echo ready # <<EOF"):
            self.assertIsNotNone(_reason(prefix + "\ngh issue create --label engine"), prefix)

    def test_normalized_build_hook_does_not_block_literal_heredoc_as_issue_create(self):
        import modes
        import providers
        payload = providers.normalize("PreToolUse", {
            "session_id": "heredoc-routing", "tool_name": "exec_command",
            "tool_input": {"cmd": "cat <<'EOF'\ngh issue create --label engine\nEOF"},
        })
        with mock.patch.object(modes, 'current_stance', return_value=modes.BUILD):
            self.assertNotEqual(modes.handler(payload).get("permissionDecision"), "deny")

    def test_opaque_or_dynamic_forms_fail_open(self):
        targets = ["trusted/project"]
        for command in ("eval 'gh issue create -R trusted/project'", "gh issue create -R $REPO -t x"):
            self.assertIsNone(_reason(command, trusted_targets=targets), command)

    def test_invalid_explicit_target_never_falls_back_to_checkout_origin(self):
        with mock.patch.object(issue_gate, "_origin_for_directory", return_value="trusted/project"):
            self.assertIsNone(_reason("gh issue create -R $REPO -t x", cwd="/session",
                                      trusted_targets=["trusted/project"]))
        self.assertEqual(issue_gate.classification_limitation(
            "Bash", {"command": "gh issue create --repo https://evil.example/trusted/project -t x"},
            cwd="/session"), issue_gate.CLASSIFICATION_LIMITATION)

    def test_github_url_target_is_accepted_and_unresolved_forms_are_visible(self):
        self.assertIsNotNone(_reason("gh issue create -R https://github.com/trusted/project -t x",
                                     trusted_targets=["trusted/project"]))
        self.assertEqual(issue_gate.classification_limitation(
            "Bash", {"command": "gh issue create -R $REPO -t x"}, cwd="/session"),
            issue_gate.CLASSIFICATION_LIMITATION)

    def test_missing_origin_and_absent_session_context_are_visible_limitations(self):
        with mock.patch.object(issue_gate, "_origin_for_directory", return_value=None):
            self.assertEqual(issue_gate.classification_limitation(
                "Bash", {"command": "gh issue create -t x"}, cwd="/known-checkout"),
                issue_gate.CLASSIFICATION_LIMITATION)
        self.assertEqual(issue_gate.classification_limitation(
            "Bash", {"command": "gh issue create -R elsewhere/project -t x"}),
            issue_gate.CLASSIFICATION_LIMITATION)

    def test_body_label_text_and_api_input_filename_are_not_creation_metadata(self):
        targets = ["trusted/project"]
        self.assertIsNone(_reason(
            "gh issue create -R elsewhere/project -b 'labels[]=engine'", trusted_targets=targets))
        self.assertIsNone(_reason(
            "gh api -X POST repos/elsewhere/project/issues/12/comments --input repos/trusted/project/issues",
            trusted_targets=targets))

    def test_command_checkout_resolves_target_but_never_enlarges_trust(self):
        with mock.patch.object(issue_gate, "_origin_for_directory", return_value="elsewhere/project"):
            self.assertIsNone(_reason("gh -C /other issue create -t x",
                                      cwd="/session", trusted_targets=["trusted/project"]))
        with mock.patch.object(issue_gate, "_origin_for_directory", return_value="trusted/project"):
            self.assertIsNotNone(_reason("cd /trusted && gh issue create -t x",
                                         cwd="/session", trusted_targets=["trusted/project"]))


class TestLabelDetectionPrecise(unittest.TestCase):
    """Label detection keys on a REAL label flag/field, never a loose substring on prose — an innocent Issue
    whose body/title merely mentions "engine" and "label" is NOT denied."""

    def test_body_mentioning_engine_and_label_is_allowed(self):
        self.assertIsNone(_reason("gh issue create --title t -b 'please relabel the engine room'"))

    def test_title_mentioning_engine_and_label_is_allowed(self):
        self.assertIsNone(_reason("gh issue create --title 'the engine label gate' -b 'the engine label is off'"))

    def test_engineering_label_is_not_the_engine_label(self):
        self.assertIsNone(_reason(_create(FREE_TEXT, label="engineering")))


class TestFailOpen(unittest.TestCase):
    """Anything the matcher cannot parse resolves to None (allow) — the nudge, never a wall."""

    def test_unparseable_shell_fails_open(self):
        self.assertIsNone(_reason('gh issue create --label engine -b "unterminated'))

    def test_non_string_or_absent_command_fails_open_without_raising(self):
        for bad in (123, ["a", "b"], None):
            self.assertIsNone(issue_gate.reroute_reason("Bash", {"command": bad}))
        self.assertIsNone(issue_gate.reroute_reason("Bash", "not-a-dict"))
        self.assertIsNone(issue_gate.reroute_reason("Bash", None))

    def test_connector_with_non_dict_input_fails_open(self):
        self.assertIsNone(issue_gate.reroute_reason("mcp__github__github_create_issue", "not-a-dict"))
        self.assertIsNone(issue_gate.reroute_reason("mcp__github__github_create_issue", None))


class TestBackstopMarkerCoupling(unittest.TestCase):
    """The drift pin: the CONTRACT_MARKERS the gate publishes for the CI backstop ARE in the helper's real
    output — so a copy change to the framing/headers breaks THIS test, not the backstop silently."""

    def test_helper_output_carries_every_contract_marker(self):
        body = issue_author.render_engine_issue_body(what_this_is="a", whats_next="b")
        for marker in issue_gate.CONTRACT_MARKERS:
            self.assertIn(marker, body, f"the helper output must carry the backstop marker {marker!r}")


class TestDemo(unittest.TestCase):
    def test_submission_demo_variations_and_false_expectation(self):
        for options in ([], ['--failure', 'closed-before-recovery'], ['--failure', 'claim-loss'],
                        ['--failure', 'recurrence'], ['--scope', 'product'],
                        ['--scope', 'product', '--label', 'engine'], ['--target', 'external'],
                        ['--target', 'external', '--label', 'engine'], ['--assessment', 'missing']):
            with self.subTest(options=options):
                self.assertEqual(quiet_call.run(issue_gate.main, ['submission-demo', *options]), 0)
        self.assertEqual(quiet_call.run(issue_gate.main, ['submission-demo', '--expected-posts', '99']), 1)

    def test_redirect_command_resolves_under_uv_engine_directory(self):
        from pathlib import Path
        for line in issue_gate.DENY_REASON.splitlines():
            if 'uv run' in line:
                tokens = shlex.split(line)
                target = tokens[tokens.index('python') + 1]
                self.assertTrue((Path(__file__).resolve().parents[1] / target).is_file())

    def test_demo_self_check_passes(self):
        self.assertEqual(quiet_call.run(issue_gate.main, ["demo"]), 0)


if __name__ == "__main__":
    unittest.main()
