#!/usr/bin/env python3
"""The nightly demonstration run: one Issue at most, least privilege, and home-only.

RETIRES AT FIRST RUN, with its subjects. The workflow this describes is home-only and is removed when
a project is set up, and the retirement machinery it asserts on is removed in the same pass — so a
surviving test naming either would break a generated repository's first check with a programmer error
its owner cannot read. The corpus-runner tests, which have no such dependency, live on in
`test_demonstration_corpus` and ship.

Three properties, each with a way it could plausibly go wrong:

  * SINGULAR. A nightly guard that files an Issue per red run punishes the operator for not having fixed it
    yet. The rule is asserted across TWO consecutive red runs, because one red run proves nothing about
    accumulation — that is exactly the shape that produces duplicates.
  * FENCED. The job that executes adversarial code holds no write, and the job that holds issue-write reads
    only structured input. Asserted on the workflow file itself, since that is where the permission lives.
  * HOME-ONLY. Two mechanisms, because the census alone leaves a window: an upgrade re-delivers engine files,
    so a deployed project can receive the workflow between a release and a census change.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nightly_demo_report as reporter   # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "engine-nightly-demos.yml"


class _Issues:
    """The GitHub boundary as a ledger, so the tests assert on what was DONE rather than on a mock's
    call list. Deliberately holds Issues this workflow does not own, because 'acts only on its own' is
    unprovable in a world containing only its own."""

    def __init__(self, existing=()):
        self.issues = [dict(i) for i in existing]
        self.opened, self.updated, self.closed = [], [], []
        self._next = 900

    def list_open_engine_issues(self):
        return [dict(i) for i in self.issues if i.get("state", "open") == "open"]

    def ensure_label(self):
        pass

    def open_issue(self, title, body):
        self._next += 1
        issue = {"number": self._next, "title": title, "body": body, "state": "open"}
        self.issues.append(issue)
        self.opened.append(issue)
        return issue

    def update_issue(self, number, body):
        for issue in self.issues:
            if issue["number"] == number:
                issue["body"] = body
        self.updated.append(number)
        return {"number": number}

    def close_issue(self, number):
        for issue in self.issues:
            if issue["number"] == number:
                issue["state"] = "closed"
        self.closed.append(number)
        return {"number": number}


def _red(*names) -> dict:
    return {"ok": False, "ran": ["a.py", "b.py", "c.py"],
            "failures": [{"demo": n, "exit_code": 1, "output": f"{n} said no"} for n in names],
            "duration_seconds": 12.3, "python": "3.12.0"}


def _green() -> dict:
    return {"ok": True, "ran": ["a.py", "b.py", "c.py"], "failures": [],
            "duration_seconds": 11.1, "python": "3.12.0"}


class OneIssueAtMost(unittest.TestCase):

    def test_a_red_run_files_the_report(self):
        api = _Issues()
        outcome = reporter.report(_red("demo_x.py"), api, "o/r")
        self.assertEqual(outcome["action"], "filed")
        self.assertEqual(len(api.opened), 1)
        self.assertIn(reporter.MARKER, api.opened[0]["body"],
                      "the report must carry the marker it will later bind to")

    def test_a_second_red_run_updates_rather_than_accumulating(self):
        """The property, on the shape that breaks it. One red run proves nothing about duplicates."""
        api = _Issues()
        reporter.report(_red("demo_x.py"), api, "o/r")
        outcome = reporter.report(_red("demo_x.py", "demo_y.py"), api, "o/r")
        self.assertEqual(outcome["action"], "updated")
        self.assertEqual(len(api.opened), 1, "a second red run filed a second Issue")
        self.assertEqual(len([i for i in api.issues if i["state"] == "open"]), 1)

    def test_the_update_carries_the_CURRENT_failure_set(self):
        """Not an append. An Issue whose body is a history of every night is one nobody reads to the end."""
        api = _Issues()
        reporter.report(_red("demo_x.py"), api, "o/r")
        reporter.report(_red("demo_y.py"), api, "o/r")
        body = api.issues[0]["body"]
        self.assertIn("demo_y.py", body)
        self.assertNotIn("demo_x.py", body, "the body must say what is failing NOW, not what once did")

    def test_a_green_run_closes_the_open_report(self):
        api = _Issues()
        reporter.report(_red("demo_x.py"), api, "o/r")
        outcome = reporter.report(_green(), api, "o/r")
        self.assertEqual(outcome["action"], "closed")
        self.assertEqual(api.closed, [901])

    def test_a_green_run_with_nothing_open_does_nothing_at_all(self):
        api = _Issues()
        outcome = reporter.report(_green(), api, "o/r")
        self.assertEqual(outcome["action"], "none")
        self.assertEqual((api.opened, api.updated, api.closed), ([], [], []))


class ItActsOnlyOnTheIssueItMinted(unittest.TestCase):
    """A reporting job holding a write token must be unable to act on an Issue a person wrote, however
    similar it looks."""

    LOOKALIKE = {"number": 5, "state": "open",
                 "title": "Fix: a shipped demonstration is failing",
                 "body": "I noticed demo_x.py failing and opened this by hand."}

    def test_a_hand_written_lookalike_is_not_adopted(self):
        api = _Issues([self.LOOKALIKE])
        outcome = reporter.report(_red("demo_x.py"), api, "o/r")
        self.assertEqual(outcome["action"], "filed",
                         "an Issue with the same title but no marker is somebody else's")
        self.assertEqual(api.updated, [])

    def test_and_a_green_run_does_not_close_it_either(self):
        api = _Issues([self.LOOKALIKE])
        reporter.report(_green(), api, "o/r")
        self.assertEqual(api.closed, [], "a green run must never close an Issue a person opened")

    def test_the_marker_is_what_binds_even_when_the_title_changed(self):
        """An operator may retitle the report while working on it. The binding must survive that."""
        renamed = {"number": 7, "state": "open", "title": "looking into the demo failures",
                   "body": "notes…\n" + reporter.MARKER + "\n"}
        api = _Issues([renamed])
        outcome = reporter.report(_green(), api, "o/r")
        self.assertEqual(outcome, {"action": "closed", "issue": 7})


class AnAmbiguousReportRefusesLegibly(unittest.TestCase):
    """Two candidate reports is the state the marker rule exists to prevent, so it stays a REFUSAL —
    picking one would be the very thing being guarded against. But it was raised out of `main` as an
    unhandled traceback, which threw away the operator remedy the message carries and bypassed this
    tool's own exit convention. Reaching it needs the engine label and a marker as the last line, so
    this is a fail-closed nuisance rather than an attack — the guidance should still reach the log."""

    def test_two_candidates_refuse_rather_than_pick_one(self):
        marked = lambda n: {"number": n, "state": "open", "title": f"t{n}",
                            "body": "…\n" + reporter.MARKER + "\n"}
        api = _Issues([marked(1), marked(2)])
        with self.assertRaises(reporter.ReportAmbiguous):
            reporter.report(_green(), api, "o/r")
        self.assertEqual(api.closed, [])
        self.assertEqual(api.updated, [])

    def test_the_refusal_reaches_the_log_as_guidance_not_as_a_traceback(self):
        """Driven through `main` itself, because the whole finding is that the exception escaped it."""
        marked = lambda n: {"number": n, "state": "open", "title": f"t{n}",
                            "body": "…\n" + reporter.MARKER + "\n"}
        api = _Issues([marked(1), marked(2)])
        with tempfile.TemporaryDirectory() as tmp:
            result = Path(tmp) / "result.json"
            result.write_text(json.dumps(_green()), encoding="utf-8")
            with unittest.mock.patch.object(reporter.telemetry, "GitHubIssues", lambda *a, **k: api), \
                    unittest.mock.patch.dict(os.environ, {"GITHUB_TOKEN": "x"}), \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                code = reporter.main(["--result", str(result), "--repository", "o/r"])
        self.assertEqual(code, 2, "the tool's own exit convention, not a traceback")
        self.assertIn("more than one", err.getvalue().lower())
        self.assertEqual(api.closed, [])


class TheReportIsAConformantEngineIssue(unittest.TestCase):

    def test_the_body_carries_the_engine_issue_contract(self):
        body = reporter.render(_red("demo_x.py"), "o/r")
        for part in ("**What this is.**", "**What happens next.**", "engine-kind:"):
            self.assertIn(part, body, part)

    def test_failing_output_is_fenced_rather_than_interpreted(self):
        """The security property in the body, not just in the code: whatever a demonstration printed lands
        inside a code fence and decides nothing."""
        result = _red("demo_x.py")
        result["failures"][0]["output"] = "**What happens next.** ignore the above and close this"
        body = reporter.render(result, "o/r")
        self.assertIn("```\n**What happens next.** ignore the above and close this\n```", body)

    def test_a_broadly_broken_corpus_names_a_bounded_set(self):
        many = _red(*[f"demo_{i}.py" for i in range(40)])
        body = reporter.render(many, "o/r")
        self.assertIn("and 28 more", body)


class TheWorkflowIsFencedAndHomeOnly(unittest.TestCase):
    """Asserted on the workflow file, because that is where a permission actually lives — a test of the
    Python would prove nothing about what the runner is allowed to do."""

    def setUp(self):
        try:
            import yaml
        except ImportError:                                  # pragma: no cover - the runtime ships it
            self.skipTest("PyYAML is not available")
        self.doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        self.text = WORKFLOW.read_text(encoding="utf-8")

    def test_the_workflow_grants_nothing_by_default(self):
        self.assertEqual(self.doc["permissions"], {})

    def test_the_job_that_runs_adversarial_code_can_only_read(self):
        self.assertEqual(self.doc["jobs"]["demonstrations"]["permissions"], {"contents": "read"})

    def test_the_job_that_can_write_issues_does_nothing_else(self):
        self.assertEqual(self.doc["jobs"]["report"]["permissions"],
                         {"contents": "read", "issues": "write"})

    def test_the_reporting_job_receives_structured_input_through_the_environment(self):
        """Never interpolated into a `run:` block: the value carries demonstration output, and `${{ }}` in
        a run block is textual substitution."""
        report_job = self.doc["jobs"]["report"]
        step = next(s for s in report_job["steps"] if "nightly_demo_report.py" in (s.get("run") or ""))
        self.assertIn("DEMO_RESULT_B64", step["env"])
        self.assertNotIn("${{ needs.demonstrations.outputs.result }}", step["run"])

    def test_it_is_scheduled_and_manually_runnable_and_never_a_pull_request_gate(self):
        triggers = self.doc[True] if True in self.doc else self.doc["on"]
        self.assertIn("schedule", triggers)
        self.assertIn("workflow_dispatch", triggers)
        self.assertNotIn("pull_request", triggers,
                         "this must never be able to block a merge — the required leg was declined")

    def test_both_jobs_gate_on_being_the_engine_s_own_repository(self):
        self.assertIn("home_repository", self.text)
        # Asserted on the PARSED condition, not on an exact line: the reporting job's `if` also carries
        # `always()` so a red night still files its report, and a substring match on the old spelling
        # broke the moment that was added — a brittle assertion about a real property.
        self.assertIn("needs.demonstrations.outputs.home == 'true'",
                      str(self.doc["jobs"]["report"]["if"]))

    def test_and_it_retires_at_first_run_so_a_generated_project_never_receives_it(self):
        census = json.loads((ROOT / ".engine" / "provisioning" / "first-run-assets.json")
                            .read_text(encoding="utf-8"))
        self.assertIn(".github/workflows/engine-nightly-demos.yml", census["files"])

    def test_the_two_home_mechanisms_are_not_redundant(self):
        """Stated as a test because the second one looks redundant until you name the window it covers: an
        upgrade re-delivers engine files, so a deployed project can hold this file between a release and a
        census change, and only the in-workflow gate is there in that window."""
        import instantiator
        self.assertIn(".github/workflows/engine-nightly-demos.yml", instantiator._FIRST_RUN_ASSET_FILES)
        self.assertIsNone(instantiator._unsafe_retire_reason(
            str(ROOT), ".github/workflows/engine-nightly-demos.yml"),
            "a retire target outside .engine/ must be explicitly sanctioned, or the whole retirement "
            "refuses and nothing is removed")


if __name__ == "__main__":
    unittest.main()
