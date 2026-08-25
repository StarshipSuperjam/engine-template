"""Tests for the persona-set coherence guard — the live consumer that runs
validate.agent_coherence_findings over the present personas and is wired as the engine/check/
agent-coherence custom/script CI rule. Verifies discovery + name injection, the read-only write-lock
guard firing on a planted lockless read-only persona while staying silent on a clean set, and that the
check + demo CLI modes run on the real repo.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_coherence_check as acc  # noqa: E402
import validate  # noqa: E402


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


_LOCKED_REVIEWER = ("---\nname: design-review-architecture\ndescription: Reviews the plan.\n"
                    "role: plan-review\nlens: architecture\nmodel-tier: judgment\n"
                    "permissions: read-only\noutput-contract: plan-review-finding.v1\n"
                    "disallowedTools: [Edit, Write, NotebookEdit, Bash]\n---\n\nbody\n")
_LOCKLESS_REVIEWER = ("---\nname: leaky-review\ndescription: Reviews the plan.\n"
                      "role: plan-review\nlens: architecture\nmodel-tier: judgment\n"
                      "permissions: read-only\noutput-contract: plan-review-finding.v1\n---\n\nbody\n")

# A qa-review persona keeps Bash (blocks only the write tools), so it MUST carry the git-safety recipe.
_GIT_SAFETY_RECIPE = ("You make the copy yourself with engine_fixture.clone_engine() and run only there; "
                      "never `git worktree add` from an existing checkout.")
_QA_HEAD = ("---\nname: qa-technical-integrity\ndescription: Reviews the build.\n"
            "role: pre-submission-review\nlens: technical-integrity\nmodel-tier: judgment\n"
            "permissions: read-only\noutput-contract: pre-submission-review-finding.v1\n"
            "disallowedTools: [Edit, Write, NotebookEdit]\n---\n\n")
_QA_WITH_RECIPE = _QA_HEAD + "You may run it in a throwaway copy. " + _GIT_SAFETY_RECIPE + "\n"
_QA_NO_RECIPE = _QA_HEAD + "You may run it in a throwaway copy and disclose that you did.\n"

# A shell-capable SCOUT owes a different recipe: it is dispatched at moments when the work is still
# uncommitted, so the tracked-file clone a reviewer carries would run against the wrong tree.
_SCOUT_RECIPE = ("Run in a copy you make yourself and never in the live checkout. Copy the whole working "
                 "tree, including uncommitted changes, into a fresh disposable copy in a private "
                 "temporary directory and run only there. Never `git worktree add` from an existing "
                 "checkout. Delete the copy when the run is done.")
_SCOUT_HEAD = ("---\nname: validation-runner\ndescription: Runs the suites.\n"
               "role: scout\nmodel-tier: mechanical\npermissions: read-only\n"
               "output-contract: validation-digest.v1\n"
               "disallowedTools: [Edit, Write, NotebookEdit, Agent, Task]\n---\n\n")
_SCOUT_WITH_RECIPE = _SCOUT_HEAD + _SCOUT_RECIPE + "\n"
_SCOUT_NO_RECIPE = _SCOUT_HEAD + "You run the suite and report a digest.\n"


class TestEngineAgentsDiscovery(unittest.TestCase):
    def test_discovers_personas_and_parses_frontmatter(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".claude/agents/design-review-architecture.md"), _LOCKED_REVIEWER)
            agents = acc.engine_agents(root=d)
            self.assertEqual([a.get("name") for a in agents], ["design-review-architecture"])
            self.assertEqual(agents[0].get("disallowedTools"), ["Edit", "Write", "NotebookEdit", "Bash"])

    def test_injects_filename_stem_when_frontmatter_omits_name(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".claude/agents/audit.md"),
                   "---\ndescription: Self-audit.\nrole: audit\nmodel-tier: judgment\n"
                   "permissions: read-only\noutput-contract: audit-finding.v1\n"
                   "disallowedTools: [Edit, Write, NotebookEdit]\n---\n\nbody\n")
            self.assertEqual(acc.engine_agents(root=d)[0].get("name"), "audit")


class TestReadOnlyWriteLockGuard(unittest.TestCase):
    def test_clean_locked_reviewer_no_finding(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".claude/agents/design-review-architecture.md"), _LOCKED_REVIEWER)
            findings = validate.agent_coherence_findings(acc.engine_agents(root=d), "hard", acc._MESSAGE)
            self.assertEqual(findings, [], "a read-only persona that blocks the write tools is clean")

    def test_lockless_readonly_persona_is_flagged(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".claude/agents/leaky-review.md"), _LOCKLESS_REVIEWER)
            findings = validate.agent_coherence_findings(acc.engine_agents(root=d), "hard", acc._MESSAGE)
            self.assertEqual(len(findings), 1, "the inherit-all read-only persona is caught")
            self.assertEqual(findings[0]["severity"], "hard")
            self.assertIn("leaky-review", findings[0]["message"])

    def test_malformed_persona_raises_fail_closed(self):
        # a malformed persona makes parsing RAISE, which propagates out of the script as a non-zero
        # exit → the custom/script runner turns that into a hard fail-closed finding.
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".claude/agents/broken.md"), "---\ndescription: [unclosed\n---\n\nbody\n")
            with self.assertRaises(Exception):
                acc.engine_agents(root=d)


class TestKeepsBash(unittest.TestCase):
    def test_tools_allowlist_governs(self):
        self.assertTrue(acc._keeps_bash({"tools": ["Read", "Bash"]}))
        self.assertFalse(acc._keeps_bash({"tools": ["Read", "Grep"]}))

    def test_disallowed_denylist_governs(self):
        self.assertTrue(acc._keeps_bash({"disallowedTools": ["Edit", "Write", "NotebookEdit"]}))
        self.assertFalse(acc._keeps_bash({"disallowedTools": ["Edit", "Write", "NotebookEdit", "Bash"]}))

    def test_neither_inherits_all_keeps_bash(self):
        self.assertTrue(acc._keeps_bash({}))

    def test_string_forms_conservatively_keep_bash(self):
        # a string-valued tools/disallowedTools is not a list, so it neither allows nor blocks:
        # fall through to inherit-all, which errs toward requiring the recipe.
        self.assertTrue(acc._keeps_bash({"tools": "inherit"}))
        self.assertTrue(acc._keeps_bash({"disallowedTools": "Bash"}))


class TestGitSafetyLeg(unittest.TestCase):
    def test_bash_keeper_with_recipe_no_finding(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".claude/agents/qa-technical-integrity.md"), _QA_WITH_RECIPE)
            self.assertEqual(acc.git_safety_findings("hard", root=d), [],
                             "a shell-capable persona carrying the recipe is clean")

    def test_bash_keeper_without_recipe_is_flagged(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".claude/agents/qa-technical-integrity.md"), _QA_NO_RECIPE)
            findings = acc.git_safety_findings("hard", root=d)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0]["severity"], "hard")
            self.assertIn("qa-technical-integrity", findings[0]["message"])
            # names both missing tokens so a fresh author knows exactly what to add
            self.assertIn("clone_engine", findings[0]["message"])
            self.assertIn("git worktree add", findings[0]["message"])

    def test_bash_locked_persona_is_exempt(self):
        # a design-review lens blocks Bash, so it cannot run commands and is exempt from the recipe.
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".claude/agents/design-review-architecture.md"), _LOCKED_REVIEWER)
            self.assertEqual(acc.git_safety_findings("hard", root=d), [],
                             "a Bash-locked reviewer owes no git-safety recipe")

    def test_partial_recipe_names_the_missing_token(self):
        with tempfile.TemporaryDirectory() as d:
            # carries the clone primitive but not the worktree prohibition
            body = _QA_HEAD + "Use engine_fixture.clone_engine() to make the copy.\n"
            _write(os.path.join(d, ".claude/agents/qa-technical-integrity.md"), body)
            findings = acc.git_safety_findings("hard", root=d)
            self.assertEqual(len(findings), 1)
            self.assertIn("git worktree add", findings[0]["message"])
            self.assertNotIn("missing: clone_engine,", findings[0]["message"])

    def test_fixture_dir_seam_bites(self):
        # the ENGINE_AGENT_FIXTURE_DIR seam the meta-check uses also drives the git-safety leg
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "recipe-less.md"), _QA_NO_RECIPE)
            findings = acc.git_safety_findings("hard", agents_dir=d)
            self.assertEqual(len(findings), 1)

    def test_review_persona_is_not_satisfied_by_the_scout_recipe(self):
        # The two recipes are not interchangeable in either direction: a reviewer that carried only
        # the scout's working-tree copy would be testing uncommitted work it was never given.
        with tempfile.TemporaryDirectory() as d:
            body = _QA_HEAD + _SCOUT_RECIPE + "\n"
            _write(os.path.join(d, ".claude/agents/qa-technical-integrity.md"), body)
            findings = acc.git_safety_findings("hard", root=d)
            self.assertEqual(len(findings), 1)
            self.assertIn("clone_engine", findings[0]["message"])
            self.assertIn("shell-capable review persona", findings[0]["message"])

    def test_main_includes_git_safety_leg_clean_on_real_repo(self):
        # main() concatenates both legs; the shipped personas carry the recipe, so it stays clean.
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = acc.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue()), [])


class TestScoutContainmentLeg(unittest.TestCase):
    """The scout branch of the same leg. A scout runs commands against work that is usually still
    uncommitted, so it owes the working-tree copy recipe rather than the reviewer's tracked-file
    clone — and being handed the wrong one is itself the defect these cases guard."""

    def test_scout_with_containment_recipe_no_finding(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".claude/agents/validation-runner.md"), _SCOUT_WITH_RECIPE)
            self.assertEqual(acc.git_safety_findings("hard", root=d), [],
                             "a shell-capable scout carrying the containment recipe is clean")

    def test_scout_without_recipe_is_flagged_with_the_scout_message(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".claude/agents/validation-runner.md"), _SCOUT_NO_RECIPE)
            findings = acc.git_safety_findings("hard", root=d)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0]["severity"], "hard")
            self.assertIn("Scout persona 'validation-runner'", findings[0]["message"])
            for token in ("including uncommitted changes", "disposable copy",
                          "never in the live checkout", "Never `git worktree add`"):
                self.assertIn(token, findings[0]["message"])

    def test_scout_carrying_the_review_recipe_is_still_flagged(self):
        # The load-bearing case: clone_engine() copies TRACKED files only, so a scout following the
        # reviewer's recipe would run the suite against a tree without the changes it was asked
        # about and report a green that means nothing. The right recipe is not optional for a scout.
        with tempfile.TemporaryDirectory() as d:
            body = _SCOUT_HEAD + _GIT_SAFETY_RECIPE + "\n"
            _write(os.path.join(d, ".claude/agents/validation-runner.md"), body)
            findings = acc.git_safety_findings("hard", root=d)
            self.assertEqual(len(findings), 1)
            self.assertIn("including uncommitted changes", findings[0]["message"])
            self.assertIn("disposable copy", findings[0]["message"])
    def test_partial_scout_recipe_names_only_what_is_missing(self):
        with tempfile.TemporaryDirectory() as d:
            body = _SCOUT_HEAD + ("Run never in the live checkout. Copy the whole working tree, including "
                                  "uncommitted changes, into a disposable copy and run there.\n")
            _write(os.path.join(d, ".claude/agents/validation-runner.md"), body)
            findings = acc.git_safety_findings("hard", root=d)
            self.assertEqual(len(findings), 1)
            self.assertIn("missing: Never `git worktree add`", findings[0]["message"])

    def test_a_body_recommending_the_forbidden_operation_is_flagged(self):
        # The bypass this token set exists to close, reproduced. A presence check scores a body by what
        # it MENTIONS, so an earlier form of this list — which required a bare "git worktree add" —
        # was satisfied just as well by a body telling the scout to work in the live checkout and make
        # its copy with `git worktree add` plus a remote repoint: the exact pair behind the two recorded
        # incidents. That body returned no finding. Requiring the negated forms is what makes the
        # difference between recommending and prohibiting visible to the check.
        with tempfile.TemporaryDirectory() as d:
            body = _SCOUT_HEAD + ("Work directly in the live checkout you were given, including "
                                  "uncommitted changes. Make your disposable copy with `git worktree "
                                  "add` and repoint its remote.\n")
            _write(os.path.join(d, ".claude/agents/validation-runner.md"), body)
            findings = acc.git_safety_findings("hard", root=d)
            self.assertEqual(len(findings), 1,
                             "a body RECOMMENDING the prohibited operation must not satisfy the recipe")
            self.assertIn("never in the live checkout", findings[0]["message"])
            self.assertIn("Never `git worktree add`", findings[0]["message"])

    def test_bash_locked_scout_is_exempt(self):
        # the grounding scout denies Bash outright, so it owes no containment recipe at all.
        with tempfile.TemporaryDirectory() as d:
            head = _SCOUT_HEAD.replace("[Edit, Write, NotebookEdit, Agent, Task]",
                                       "[Edit, Write, NotebookEdit, Bash, Agent, Task]")
            _write(os.path.join(d, ".claude/agents/grounding-scout.md"), head + "You search.\n")
            self.assertEqual(acc.git_safety_findings("hard", root=d), [],
                             "a Bash-locked scout owes no containment recipe")

    def test_line_wrapped_recipe_is_not_a_finding(self):
        # A persona body is wrapped prose. A phrase split across a line break is still the recipe,
        # and failing it would teach authors to reflow paragraphs to satisfy a substring test.
        with tempfile.TemporaryDirectory() as d:
            body = _SCOUT_HEAD + ("Run never in the live\ncheckout. Copy the whole working tree, including "
                                  "uncommitted\nchanges, into a fresh disposable\ncopy in a temporary "
                                  "directory. Never `git worktree\nadd` from an existing checkout.\n")
            _write(os.path.join(d, ".claude/agents/validation-runner.md"), body)
            self.assertEqual(acc.git_safety_findings("hard", root=d), [],
                             "a wrapped but complete scout recipe is clean")


class TestScriptModes(unittest.TestCase):
    def test_check_mode_emits_json_array_clean_on_real_repo(self):
        # main() with no args globs the REAL repo (validate.ROOT); the shipped personas are all locked.
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = acc.main([])
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue())
        self.assertIsInstance(out, list)
        self.assertEqual(out, [], "every shipped read-only persona blocks the write tools")

    def test_demo_runs_and_narrates(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = acc.main(["demo"])
        self.assertEqual(rc, 0)
        text = buf.getvalue()
        self.assertIn("read-only", text)
        self.assertIn("RED", text)


if __name__ == "__main__":
    unittest.main()
