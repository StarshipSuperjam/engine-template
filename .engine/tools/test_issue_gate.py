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

import issue_author
import issue_gate
import quiet_call  # capture a demo walkthrough's stdout so it can't bury the suite summary

# A conforming body is whatever the helper actually renders. Under the widened gate it is rerouted just like a
# free-text body (the create CLI is the supported path, not a hand-rolled `gh` with a helper-rendered body).
CONFORMING = issue_author.render_engine_issue_body(what_this_is="a demo item", whats_next="nothing to do")
FREE_TEXT = "just some free text with no contract markers at all"


def _reason(command: str):
    """The gate's verdict for a Bash command string: a reason str (reroute) or None (allow)."""
    return issue_gate.reroute_reason("Bash", {"command": command})


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

    def test_reason_names_the_create_cli_and_the_escape_hatch(self):
        reason = _reason(_create(FREE_TEXT))
        self.assertIn(".engine/tools/issue_author.py", reason)   # the in-repo helper, not a cross-repo path
        self.assertIn("create", reason)                          # points at the supported create path
        self.assertIn("--confirm", reason)
        self.assertIn("drop the `engine` label", reason)         # the not-an-engine-Issue escape hatch


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
    def test_demo_self_check_passes(self):
        self.assertEqual(quiet_call.run(issue_gate.main, ["demo"]), 0)


if __name__ == "__main__":
    unittest.main()
