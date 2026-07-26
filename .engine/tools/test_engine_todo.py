#!/usr/bin/env python3
"""Tests for the deferred-work marker parser and its form check (eADR-0039).

Every trigger below is ASSEMBLED FROM PARTS rather than written literally. A literal would be a real marker
in a real tracked file, so this file would show up in `list` forever and the check would grade the test
fixtures as production markers. Assembling keeps the authoring rule the contract states.

The recognition cases carry most of the weight here. The rule reached its shipped form after two wrong ones —
anchoring only at line start missed a trailing comment after code, and requiring only that a leader precede
the trigger matched every heading and issue citation naming the form — so each of those failures has a test
that fails if the rule regresses to it.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_todo
import engine_todo_form_check
import quiet_call          # noqa: E402  (capture the demo walkthrough so it can't bury the summary)
import validate

T = engine_todo.TOKEN + ":"                    # the bare trigger
R = engine_todo.TOKEN + "(#412):"              # the trigger carrying an issue reference


class RecognisedPositions(unittest.TestCase):
    """The two positions the frozen rule accepts."""

    def test_a_trailing_comment_after_code_is_a_marker(self):
        # The regression that anchoring at line start alone got wrong: the author believes a deferral was
        # recorded while nothing can see it, which is worse than the prose it replaced.
        found = engine_todo.scan_text("    return _append(record)   # " + T + " no retry path yet")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].description, "no retry path yet")

    def test_a_docstring_line_is_a_marker(self):
        # Where this engine's real notes actually sit — no comment leader anywhere on the line.
        found = engine_todo.scan_text('"""Append one record.\n\n    ' + T + ' the envelope is not written.\n"""')
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].description, "the envelope is not written.")

    def test_an_html_comment_is_a_marker(self):
        found = engine_todo.scan_text("<!-- " + T + " the seed is not rendered yet -->")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].description, "the seed is not rendered yet")

    def test_an_issue_reference_is_captured(self):
        found = engine_todo.scan_text("# " + R + " no retry path")
        self.assertEqual(found[0].ref, "#412")

    def test_a_bare_marker_reports_no_reference(self):
        self.assertIsNone(engine_todo.scan_text("# " + T + " nothing cited")[0].ref)


class RejectedPositions(unittest.TestCase):
    """Shapes that name the form without being one. Each of these matched under a rejected rule."""

    def _none(self, line, why):
        self.assertEqual(engine_todo.scan_text(line), [], why)

    def test_a_markdown_heading_is_not_a_marker(self):
        # Matched when the rule required only that a leader PRECEDE the trigger.
        self._none("## Writing an " + T + " marker", "a heading naming the form is not a marker")

    def test_an_issue_citation_is_not_a_marker(self):
        self._none("Issue #412 tracks the " + T + " grammar", "a citation naming the form is not a marker")

    def test_an_inline_prose_mention_is_not_a_marker(self):
        self._none("the parser -- see `" + T + "` above -- is offline", "an inline mention is not a marker")

    def test_a_string_literal_is_not_a_marker(self):
        self._none('MESSAGE = "' + T + ' this is data"', "a string literal is not a marker")

    def test_a_markdown_bullet_is_not_a_marker(self):
        # The bullet character is deliberately absent from the leader set; including it made an ordinary
        # list item a marker.
        self._none("* " + T + " an ordinary list item", "a markdown bullet is not a comment leader")

    def test_the_bare_token_without_its_colon_is_not_a_marker(self):
        self._none("# the " + engine_todo.TOKEN + " grammar is frozen", "the token alone is not a trigger")


class Continuation(unittest.TestCase):
    """Multi-line markers. An older parser reading only the first line gets a truncated description, never
    a wrong one — which is what makes widening the rule later safe."""

    def test_a_commented_marker_joins_lines_carrying_the_same_leader(self):
        found = engine_todo.scan_text("# " + T + " the module manager is not wired\n#   the caller raises instead\n")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].description, "the module manager is not wired the caller raises instead")

    def test_a_docstring_marker_joins_lines_indented_deeper(self):
        found = engine_todo.scan_text("    " + T + " the envelope is missing\n        callers read the header\n")
        self.assertIn("callers read the header", found[0].description)

    def test_a_blank_line_closes_the_marker(self):
        found = engine_todo.scan_text("# " + T + " first\n\n# unrelated trailing comment\n")
        self.assertEqual(found[0].description, "first")

    def test_a_second_trigger_closes_the_first_and_starts_its_own(self):
        found = engine_todo.scan_text("# " + T + " first\n# " + T + " second\n")
        self.assertEqual([m.description for m in found], ["first", "second"])

    def test_a_line_at_or_left_of_the_leader_column_closes_the_marker(self):
        found = engine_todo.scan_text("    # " + T + " first\n# a comment further left\n")
        self.assertEqual(found[0].description, "first")


class FormCheck(unittest.TestCase):
    """The hard tier is held to one unambiguous case."""

    def _run(self, source):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "seeded.py"), "w", encoding="utf-8") as fh:
                fh.write(source)
            return engine_todo_form_check.findings("hard", root=tmp)

    def test_a_marker_with_no_description_is_hard(self):
        out = self._run("x = 1   # " + T + "\n")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["severity"], "hard")
        self.assertIn("no description", out[0]["message"])

    def test_a_marker_with_a_description_is_clean(self):
        self.assertEqual(self._run("x = 1   # " + T + " the retry path is missing\n"), [])

    def test_a_description_supplied_only_by_a_continuation_line_is_clean(self):
        # The emptiness test applies to the JOINED description, so substance on the next line counts.
        self.assertEqual(self._run("# " + T + "\n#   the retry path is missing\n"), [])

    def test_an_unrecognised_parenthetical_is_soft_never_hard(self):
        # Reserved for a later extension of the grammar: widening it must not redden committed source.
        out = self._run("x = 1   # " + engine_todo.TOKEN + "(slice-7): the retry path is missing\n")
        self.assertTrue(all(f["severity"] != "hard" for f in out))


class FixtureAndScope(unittest.TestCase):

    def test_the_committed_negative_fixture_makes_the_check_bite(self):
        root = os.path.join(validate.ROOT, ".engine", "_fixtures", "engine-todo-form")
        with open(os.path.join(root, "expect.json"), encoding="utf-8") as fh:
            expect = json.load(fh)
        out = engine_todo_form_check.findings("hard", root=os.path.join(root, "tree"))
        self.assertTrue(out, "the seeded fixture must produce a finding, or the meta-check passes vacuously")
        self.assertTrue(any(f["severity"] == expect["severity"] and expect["message_contains"] in f["message"]
                            for f in out))

    def test_the_fixture_tree_is_pruned_from_a_repository_scan(self):
        # Base-relative, so the fixture prunes from a repo scan but never from its own.
        self.assertTrue(any(p.startswith(engine_todo._FIXTURE_PREFIX)
                            for p in engine_todo.tracked_files(validate.ROOT)),
                        "the fixture must be tracked, or this test proves nothing")
        self.assertFalse(any(m.path.startswith(engine_todo._FIXTURE_PREFIX)
                             for m in engine_todo.markers()))

    def test_the_live_tree_carries_no_malformed_marker(self):
        self.assertEqual(engine_todo_form_check.findings("hard"), [])

    def test_the_engine_owned_skip_is_empty_in_the_home_repository(self):
        # These files ARE the work here; the skip exists for a deployed copy, where an update overwrites them.
        self.assertEqual(engine_todo.engine_owned_skip(), set())


class DemoAndCli(unittest.TestCase):

    def test_the_demo_exercises_the_real_parser_and_passes(self):
        self.assertEqual(quiet_call.run(lambda: engine_todo._demo([])), 0)

    def test_the_demo_can_fail(self):
        # A demo that cannot fail proves nothing. Break recognition and the demo must notice.
        original = engine_todo.TRIGGER
        try:
            engine_todo.TRIGGER = engine_todo.re.compile(r"THIS-MATCHES-NOTHING:")
            self.assertEqual(quiet_call.run(lambda: engine_todo._demo([])), 1)
        finally:
            engine_todo.TRIGGER = original

    def test_list_runs_and_reports_json(self):
        done = subprocess.run([sys.executable, os.path.join(validate.ROOT, ".engine", "tools", "engine_todo.py"),
                               "list", "--json"], capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIsInstance(json.loads(done.stdout), list)


if __name__ == "__main__":
    unittest.main()
