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
requiring the operator's recorded decision, and `review record` began requiring the effort its panel
delivered. Both gates are correct; the demonstration's own calls simply were not updated, and it died on an
unhandled traceback. The only thing that would ever have executed it was the nightly workflow — a day late,
on main.

Five seconds of suite time is a very small price for the arc that sells the whole component.
"""
from __future__ import annotations

import unittest


class TheFrontDoorDemoStillWalks(unittest.TestCase):
    def test_the_plan_to_ready_pull_request_demo_passes(self):
        import quiet_call
        import demo_plan_to_ready_pr as demo
        self.assertEqual(quiet_call.run(demo.main), 0)


if __name__ == "__main__":
    unittest.main()
