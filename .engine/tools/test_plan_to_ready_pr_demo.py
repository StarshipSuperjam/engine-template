#!/usr/bin/env python3
"""The front-door demonstration actually walks: plan → seal → Build → ready pull request.

RETIRES AT FIRST RUN, with its subject. `demo_plan_to_ready_pr` is engine-development scaffolding and is
removed when a project is set up, so a surviving test naming it would break a generated repository's very
first check with a programmer error its owner cannot read. That is why this test does not live beside the
plan-library tests in `test_plan_dogfood`, which ship: a test file inherits the retirement of the thing it
imports, and splitting by provenance is the only way both halves stay honest.

WHY IT EXISTS AT ALL. The demonstration is the whole arc in one run — a plan written into the Project
Manager, approved, sealed, bound, built, and left as a ready pull request — and NOTHING RAN IT. Two gates
added in the same change that added this test broke it in the meantime: `seal`/`bind`/`approve` began
requiring the operator's recorded decision, and `review record` gained a required argument (since retired).
The demonstration's own calls simply were not updated, and it died on an unhandled traceback. The only thing that would ever have executed it was the nightly workflow — a day late,
on main.

Five seconds of suite time is a very small price for the arc that sells the whole component.
"""
from __future__ import annotations

import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Vocabulary that left with the review contract's effort dimension. Review depth is the lens roster plus
# each lens's model; no operator-facing surface describes reviewers by effort.
RETIRED = ("--session-effort", "--delivered-effort", "--accept-effort-shortfall", "review_depths",
           "operator_review_effort", "reviewer effort", "reviewer EFFORT", "scales reviewer",
           "Depth scales EFFORT", "at higher effort", "effort configured for", "depth-scaled",
           "how hard each reviewer looks", "TheB1EffortShortfall")


def _operator_facing_files():
    for rel in ("operations", "policies", "docs", "templates", "conduct"):
        base = os.path.join(ROOT, ".engine", rel)
        for dirpath, _dirs, files in os.walk(base):
            for name in files:
                if name.endswith(".md"):
                    yield os.path.join(dirpath, name)
    for rel in (os.path.join(".claude", "skills"), os.path.join(".claude", "agents")):
        base = os.path.join(ROOT, rel)
        for dirpath, _dirs, files in os.walk(base):
            for name in files:
                if name.endswith(".md"):
                    yield os.path.join(dirpath, name)
    tools = os.path.join(ROOT, ".engine", "tools")
    for name in sorted(os.listdir(tools)):
        if name.startswith("test_") and name.endswith(".py"):
            yield os.path.join(tools, name)


def _module_docstring(path):
    text = open(path, encoding="utf-8").read()
    match = re.search(r'"""(.*?)"""', text, re.S)
    return match.group(1) if match else ""


class TheOperatorFacingSurfacesDescribeDepthByLenses(unittest.TestCase):
    """Every runbook, policy, doc, template, skill, persona, and test-module docstring (the docstrings feed
    the derived CI-assurance page) describes review depth by the lenses it runs and by nothing retired."""

    def test_no_retired_vocabulary_survives(self):
        hits = []
        for path in _operator_facing_files():
            text = _module_docstring(path) if path.endswith(".py") else open(path, encoding="utf-8").read()
            for word in RETIRED:
                if word in text:
                    hits.append((os.path.relpath(path, ROOT), word))
        self.assertEqual(hits, [])

    def test_the_instructions_that_survive_are_still_there(self):
        # The review commands live in the phase runbook the spine names for that phase, not in the spine.
        review = open(os.path.join(ROOT, ".engine", "operations", "build-validation-and-review.md"),
                      encoding="utf-8").read()
        self.assertIn("--code-execution none|discarded-copy|in-place", review)
        self.assertIn("review packet --stage deliverable", review)
        routing = open(os.path.join(ROOT, ".engine", "policies", "model-routing.md"), encoding="utf-8").read()
        self.assertIn("tools/agent_bindings.py render", routing)
        self.assertIn("not yet supported", routing)
        template = open(os.path.join(ROOT, ".engine", "templates", "risk-assessment.md"), encoding="utf-8").read()
        self.assertIn("a focused subset of the independent reviews", template)
        self.assertIn("every independent review available", template)


    def test_the_condensed_consent_template_keeps_its_shape(self):
        # Node condense-consent-template pins the risk-assessment template's structure here, in the file
        # that actually reads the template (NOT test_doc). Three obligations:
        # (a) the required frontmatter sections appear in the body in their declared order — a subsequence,
        #     because the allowed 'If this weakens a safety guardrail' section may interleave before 'Your
        #     call' — and the body stays within its declared length_budget;
        # (b) the care recommendation is one dominant line and the depth ladder is stated once;
        # (c) the fast-path-collapse and anti-habituation clauses survive the condensing.
        path = os.path.join(ROOT, ".engine", "templates", "risk-assessment.md")
        text = open(path, encoding="utf-8").read()
        fm = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
        self.assertIsNotNone(fm, "risk-assessment.md must open with a frontmatter block")
        front, body = fm.group(1), fm.group(2)
        required = json.loads(re.search(r"required_sections:\s*(\[.*\])", front).group(1))
        allowed = json.loads(re.search(r"allowed_sections:\s*(\[.*\])", front).group(1))
        budget = int(re.search(r"length_budget:\s*(\d+)", front).group(1))
        headings = re.findall(r"^##\s+(.*?)\s*$", body, re.M)
        # (a) no section outside required+allowed; the required ones keep their declared order as a subsequence
        for h in headings:
            self.assertIn(h, set(required) | set(allowed), f"unexpected section '## {h}'")
        self.assertEqual([h for h in headings if h in required], required,
                         "required sections are missing or out of their declared order")
        self.assertLessEqual(len(body.splitlines()), budget,
                             f"template body exceeds its {budget}-line length_budget")
        # (b) exactly one dominant recommendation line; the ladder is stated exactly once
        self.assertEqual(len(re.findall(r"^>\s*\*\*My recommendation:", body, re.M)), 1,
                         "the care recommendation must be exactly one dominant line")
        for rung in ("**Quick check**", "**Standard review**", "**Thorough review**"):
            self.assertEqual(body.count(rung), 1, f"the ladder rung {rung} must appear exactly once")
        self.assertEqual(body.count("a focused subset of the independent reviews"), 1)
        self.assertEqual(body.count("every independent review available"), 1)
        # (c) the two anti-rubber-stamp clauses survive the condensing
        self.assertIn("this whole surface collapses to just the Headline", body)  # fast-path collapse
        self.assertIn("habituation never dulls the high-stakes consent", body)    # anti-habituation


    def test_the_first_presentation_carries_its_decision_context(self):
        # Node first-presentation-doctrine: a drafted plan's FIRST presentation carries its decision context
        # whole, while the depth/approval stays a distinct, session-led stop reached only once the open
        # questions reach zero. Asserted against section 6's prose (what a test can inspect), with whitespace
        # normalized so a phrase that wraps across source lines still matches.
        runbook = os.path.join(ROOT, ".engine", "operations", "plan-orchestration.md")
        text = open(runbook, encoding="utf-8").read()
        def slab(start, end):
            i = text.index(start)
            return " ".join(text[i:text.index(end, i)].split())
        # (a) the first showing carries the risk information, the open-questions-with-answers, the revise invite
        show = slab("**Show the drafted plan with no ask attached.**", "**Then, once they are satisfied")
        self.assertIn("PLAN.md", show)
        self.assertIn("one-line care recommendation", show)
        self.assertIn("every open question with the answer you propose", show)
        self.assertIn("invite revisions", show)
        self.assertIn("No depth menu rides along", show)  # the depth ask does NOT ride with the first showing
        # (b) the depth ask is a distinct, led stop reached only after the open questions reach zero
        approval = slab("**Then, once they are satisfied, the approval.**", "**One cold review")
        self.assertIn("only when the open questions have reached zero", approval)
        self.assertIn("distinct, led step", approval)
        self.assertIn("--operator-decided", approval)
        self.assertIn("That one choice covers the plan's cold review and the Build's later one", approval)  # one-choice rule
        # (c) the guard (the Notes failure-mode) forbids a context-free or questions-unanswered depth offer
        notes = " ".join(text[text.index("## Notes"):].split())
        self.assertIn("depth-approval menu with no plan context", notes)
        self.assertIn("open questions still unanswered", notes)
        self.assertIn("no invitation to revise", notes)
        # (d) section 8 and the engine-start skill still name the typed start as the sole Build entry
        s8 = slab("### 8. Know the seams", "## Done when")
        self.assertIn("the Build begins only when they type the engine-start command", s8)
        skill = open(os.path.join(ROOT, ".claude", "skills", "engine-start", "SKILL.md"), encoding="utf-8").read()
        self.assertIn("the typed command is the only way in", skill)


class TheFrontDoorDemoStillWalks(unittest.TestCase):
    def test_the_plan_to_ready_pull_request_demo_passes(self):
        import quiet_call
        import demo_plan_to_ready_pr as demo
        self.assertEqual(quiet_call.run(demo.main), 0)


if __name__ == "__main__":
    unittest.main()
