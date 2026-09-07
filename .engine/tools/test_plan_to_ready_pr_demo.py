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


class TheFrontDoorDemoStillWalks(unittest.TestCase):
    def test_the_plan_to_ready_pull_request_demo_passes(self):
        import quiet_call
        import demo_plan_to_ready_pr as demo
        self.assertEqual(quiet_call.run(demo.main), 0)


if __name__ == "__main__":
    unittest.main()
