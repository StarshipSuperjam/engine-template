#!/usr/bin/env python3
"""Tests for plan_program — the multi-PR program object.

The guarantee under test is one sentence and the tests are shaped to match it: an obligation a plan
declares it is CARRYING cannot vanish from its successor. It is satisfied, re-declared, or released
with a stated reason.

Just as important is what must NOT be claimed. The program does not judge whether the decomposition
was wise, whether the order is right, or whether a release was justified. A test at the bottom pins
that a release is accepted on the strength of a reason alone, so nobody later reads a passing program
check as approval of a decision nothing examined.
"""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import plan_program
import plan_store

from test_plan_store import _document


def _obligation(identifier, statement, state="carried", reason=None):
    obligation = {"id": identifier, "statement": statement, "state": state}
    if reason:
        obligation["reason"] = reason
    return obligation


class _Program(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.plans = plan_store.PlanLibrary(Path(self._tmp.name) / "plans")
        self.programs = plan_program.ProgramLibrary(self.plans)
        self.addCleanup(self._tmp.cleanup)

    def _plan(self, plan_id, title, *obligations, program_id="prg_aaaaaaaaaaaa", predecessor=None):
        document = _document(plan_id=plan_id, title=title)
        if obligations:
            program = {"program_id": program_id, "carried_obligations": list(obligations)}
            if predecessor:
                program["predecessor_plan_id"] = predecessor
            document["program"] = program
        return self.plans.create(document), document

    def _two_pr_program(self):
        slug = self.programs.create("Plan Coordinator", "A coordinator delivered across two PRs.")
        self._plan("pln_aaaaaaaaaaaa", "PR A",
                   _obligation("OB-1", "PR B cuts the Build Coordinator over to sealed handoffs."),
                   _obligation("OB-2", "PR B amends eADR-0025 and eADR-0041."))
        self.programs.add_child(slug, "pln_aaaaaaaaaaaa")
        return slug


class Creation(_Program):
    def test_a_program_starts_empty_and_says_so(self):
        slug = self.programs.create("A program", "Deliver a thing across PRs.")
        record = self.programs.read(slug)
        self.assertEqual(record["children"], [])
        self.assertEqual(self.programs.derived_status(record), "empty")
        self.assertTrue(record["program_id"].startswith("prg_"))

    def test_the_first_child_needs_no_predecessor_and_a_later_one_does(self):
        slug = self._two_pr_program()
        self._plan("pln_bbbbbbbbbbbb", "PR B",
                   _obligation("OB-1", "Cut over.", "satisfied"),
                   _obligation("OB-2", "Amend the contracts.", "satisfied"))
        with self.assertRaisesRegex(plan_program.ProgramError, "must declare which plan it succeeds"):
            self.programs.add_child(slug, "pln_bbbbbbbbbbbb")

    def test_declaring_a_predecessor_on_the_first_child_is_refused(self):
        slug = self.programs.create("A program", "Objective.")
        self._plan("pln_aaaaaaaaaaaa", "PR A")
        with self.assertRaisesRegex(plan_program.ProgramError, "no predecessor to declare"):
            self.programs.add_child(slug, "pln_aaaaaaaaaaaa", predecessor="pln_aaaaaaaaaaaa")

    def test_the_same_plan_cannot_be_added_twice(self):
        slug = self._two_pr_program()
        with self.assertRaisesRegex(plan_program.ProgramError, "already a child"):
            self.programs.add_child(slug, "pln_aaaaaaaaaaaa", predecessor="pln_aaaaaaaaaaaa")

    def test_nothing_auto_selects_a_program(self):
        self.programs.create("Only program", "Objective.")
        with self.assertRaisesRegex(plan_program.ProgramError, "nothing is selected by default"):
            self.programs.resolve("")


class TheGuarantee(_Program):
    """An obligation declared as carried cannot vanish from its successor."""

    def test_a_dropped_obligation_is_refused_and_named(self):
        slug = self._two_pr_program()
        self._plan("pln_bbbbbbbbbbbb", "PR B",
                   _obligation("OB-1", "Cut over.", "satisfied"))   # OB-2 simply not mentioned
        with self.assertRaises(plan_program.ProgramError) as caught:
            self.programs.add_child(slug, "pln_bbbbbbbbbbbb", predecessor="pln_aaaaaaaaaaaa")
        message = str(caught.exception)
        self.assertIn("does not answer for 1 obligation", message)
        self.assertIn("OB-2", message)
        self.assertIn("amends eADR-0025", message)
        self.assertIn("decay", message)
        # And the refusal is total: the program is unchanged.
        self.assertEqual(len(self.programs.read(slug)["children"]), 1)

    def test_mentioning_nothing_at_all_drops_everything(self):
        slug = self._two_pr_program()
        self._plan("pln_bbbbbbbbbbbb", "PR B")      # no program block whatsoever
        with self.assertRaises(plan_program.ProgramError) as caught:
            self.programs.add_child(slug, "pln_bbbbbbbbbbbb", predecessor="pln_aaaaaaaaaaaa")
        self.assertIn("2 obligation", str(caught.exception))

    def test_satisfying_an_obligation_answers_for_it(self):
        slug = self._two_pr_program()
        self._plan("pln_bbbbbbbbbbbb", "PR B",
                   _obligation("OB-1", "Cut over.", "satisfied"),
                   _obligation("OB-2", "Amend the contracts.", "satisfied"))
        record = self.programs.add_child(slug, "pln_bbbbbbbbbbbb", predecessor="pln_aaaaaaaaaaaa")
        self.assertEqual(len(record["children"]), 2)
        self.assertEqual(self.programs.outstanding_obligations(record), [])

    def test_re_declaring_an_obligation_carries_it_further(self):
        slug = self._two_pr_program()
        self._plan("pln_bbbbbbbbbbbb", "PR B",
                   _obligation("OB-1", "Cut over.", "satisfied"),
                   _obligation("OB-2", "Amend the contracts."))     # still carried
        record = self.programs.add_child(slug, "pln_bbbbbbbbbbbb", predecessor="pln_aaaaaaaaaaaa")
        outstanding = self.programs.outstanding_obligations(record)
        self.assertEqual([o["id"] for o in outstanding], ["OB-2"])

    def test_releasing_an_obligation_answers_for_it_and_keeps_the_reason(self):
        slug = self._two_pr_program()
        self._plan("pln_bbbbbbbbbbbb", "PR B",
                   _obligation("OB-1", "Cut over.", "satisfied"),
                   _obligation("OB-2", "Amend the contracts.", "released",
                               reason="The contracts were amended in PR A after all."))
        record = self.programs.add_child(slug, "pln_bbbbbbbbbbbb", predecessor="pln_aaaaaaaaaaaa")
        self.assertEqual(self.programs.outstanding_obligations(record), [])
        rendered = plan_program.render(self.programs, record)
        self.assertIn("released along the way", rendered)
        self.assertIn("The contracts were amended in PR A after all.", rendered)

    def test_a_chain_of_three_carries_a_debt_the_whole_way(self):
        slug = self._two_pr_program()
        self._plan("pln_bbbbbbbbbbbb", "PR B",
                   _obligation("OB-1", "Cut over.", "satisfied"),
                   _obligation("OB-2", "Amend the contracts."))
        self.programs.add_child(slug, "pln_bbbbbbbbbbbb", predecessor="pln_aaaaaaaaaaaa")
        self._plan("pln_cccccccccccc", "PR C",
                   _obligation("OB-2", "Amend the contracts.", "satisfied"))
        record = self.programs.add_child(slug, "pln_cccccccccccc", predecessor="pln_bbbbbbbbbbbb")
        self.assertEqual(self.programs.outstanding_obligations(record), [])
        self.assertEqual(len(record["children"]), 3)

    def test_dropping_at_the_third_link_is_caught_too(self):
        slug = self._two_pr_program()
        self._plan("pln_bbbbbbbbbbbb", "PR B",
                   _obligation("OB-1", "Cut over.", "satisfied"),
                   _obligation("OB-2", "Amend the contracts."))
        self.programs.add_child(slug, "pln_bbbbbbbbbbbb", predecessor="pln_aaaaaaaaaaaa")
        self._plan("pln_cccccccccccc", "PR C")
        with self.assertRaises(plan_program.ProgramError) as caught:
            self.programs.add_child(slug, "pln_cccccccccccc", predecessor="pln_bbbbbbbbbbbb")
        self.assertIn("OB-2", str(caught.exception))

    def test_the_predecessor_is_declared_not_inferred_from_position(self):
        # Re-pointing what a successor answers for must take a declaration, not an array shuffle.
        slug = self._two_pr_program()
        self._plan("pln_bbbbbbbbbbbb", "PR B",
                   _obligation("OB-1", "Cut over.", "satisfied"),
                   _obligation("OB-2", "Amend the contracts.", "satisfied"))
        record = self.programs.add_child(slug, "pln_bbbbbbbbbbbb", predecessor="pln_aaaaaaaaaaaa")
        self.assertEqual(record["children"][1]["predecessor_plan_id"], "pln_aaaaaaaaaaaa")

    def test_an_unknown_predecessor_is_refused(self):
        slug = self._two_pr_program()
        self._plan("pln_bbbbbbbbbbbb", "PR B")
        self._plan("pln_dddddddddddd", "An unrelated plan")
        with self.assertRaisesRegex(plan_program.ProgramError, "not a child of this program"):
            self.programs.add_child(slug, "pln_bbbbbbbbbbbb", predecessor="pln_dddddddddddd")


class WhatItDoesNotJudge(_Program):
    def test_a_release_is_accepted_on_a_stated_reason_alone(self):
        # Deliberate. The mechanism makes dropping an obligation VISIBLE; it does not and must not
        # pretend to weigh whether letting it go was right. A check that appeared to judge that would
        # launder the judgement, and an operator would read a green program as approval.
        slug = self._two_pr_program()
        self._plan("pln_bbbbbbbbbbbb", "PR B",
                   _obligation("OB-1", "Cut over.", "released", reason="we changed our minds"),
                   _obligation("OB-2", "Amend the contracts.", "released", reason="not worth it"))
        record = self.programs.add_child(slug, "pln_bbbbbbbbbbbb", predecessor="pln_aaaaaaaaaaaa")
        self.assertEqual(self.programs.outstanding_obligations(record), [])
        # But every release stays findable, with its reason attached.
        rendered = plan_program.render(self.programs, record)
        self.assertIn("we changed our minds", rendered)
        self.assertIn("not worth it", rendered)

    def test_nothing_selects_advances_or_starts_a_child(self):
        slug = self._two_pr_program()
        record = self.programs.read(slug)
        rendered = plan_program.render(self.programs, record)
        self.assertIn("Nothing here selects, starts, or advances a child", rendered)
        for attribute in dir(self.programs):
            self.assertNotIn(attribute, ("current_child", "next_child", "advance", "start"))


class DerivedProgramStatus(_Program):
    def _complete(self, plan_id):
        self.plans.update_record(self.plans.resolve(plan_id), lambda r: r.update({"closure": {
            "state": "complete", "at": "2026-08-23T05:00:00Z", "reason": "merged"}}))

    def test_sealing_a_child_does_not_complete_the_program(self):
        slug = self._two_pr_program()
        plan_slug = self.plans.resolve("pln_aaaaaaaaaaaa")
        digest = self.plans.read_record(plan_slug)["current"]["plan_digest"]
        self.plans.update_record(plan_slug, lambda r: r.update({"seal": {
            "revision": 1, "reviewed_digest": digest, "sealed_digest": digest,
            "build_plan_digest": digest, "at": "2026-08-23T03:00:00Z", "delta_judgment": "none"}}))
        self.assertEqual(self.programs.derived_status(self.programs.read(slug)), "in-progress")

    def test_completing_every_child_derives_program_completion(self):
        slug = self._two_pr_program()
        self._plan("pln_bbbbbbbbbbbb", "PR B",
                   _obligation("OB-1", "Cut over.", "satisfied"),
                   _obligation("OB-2", "Amend the contracts.", "satisfied"))
        self.programs.add_child(slug, "pln_bbbbbbbbbbbb", predecessor="pln_aaaaaaaaaaaa")
        self._complete("pln_aaaaaaaaaaaa")
        self.assertEqual(self.programs.derived_status(self.programs.read(slug)), "in-progress")
        self._complete("pln_bbbbbbbbbbbb")
        self.assertEqual(self.programs.derived_status(self.programs.read(slug)), "complete")

    def test_a_program_has_no_seal_of_its_own(self):
        schema = json.loads(plan_program.PROGRAM_SCHEMA.read_text(encoding="utf-8"))
        self.assertNotIn("seal", schema["properties"])
        self.assertNotIn("status", schema["properties"])
        self.assertEqual(set(schema["$defs"]["closure"]["properties"]["state"]["enum"]),
                         {"retired", "abandoned"})

    def test_a_missing_child_is_reported_rather_than_skipped(self):
        slug = self._two_pr_program()
        record = self.programs.read(slug)
        record["children"].append({"plan_id": "pln_ffffffffffff", "position": 2,
                                   "added_at": "2026-08-23T06:00:00Z",
                                   "predecessor_plan_id": "pln_aaaaaaaaaaaa"})
        self.programs._write(slug, record)
        view = self.programs.child_view(record)
        self.assertEqual(view[1]["status"], "missing")
        self.assertEqual(self.programs.derived_status(record), "needs-attention")

    def test_closing_and_reopening_a_program(self):
        slug = self._two_pr_program()
        self.programs.close(slug, "abandoned", "the split was wrong")
        self.assertEqual(self.programs.derived_status(self.programs.read(slug)), "abandoned")
        with self.assertRaisesRegex(plan_program.ProgramError, "already abandoned"):
            self.programs.close(slug, "retired", "again")
        self.programs.reopen(slug)
        self.assertEqual(self.programs.derived_status(self.programs.read(slug)), "in-progress")

    def test_a_closed_program_takes_no_new_children(self):
        slug = self._two_pr_program()
        self.programs.close(slug, "retired", "superseded")
        self._plan("pln_bbbbbbbbbbbb", "PR B",
                   _obligation("OB-1", "Cut over.", "satisfied"),
                   _obligation("OB-2", "Amend.", "satisfied"))
        with self.assertRaisesRegex(plan_program.ProgramError, "reopen it first"):
            self.programs.add_child(slug, "pln_bbbbbbbbbbbb", predecessor="pln_aaaaaaaaaaaa")


class Rendering(_Program):
    def test_a_program_renders_children_statuses_and_outstanding_obligations(self):
        slug = self._two_pr_program()
        rendered = plan_program.render(self.programs, self.programs.read(slug))
        self.assertIn("Plan Coordinator", rendered)
        self.assertIn("PR A", rendered)
        self.assertIn("pln_aaaaaaaaaaaa", rendered)
        self.assertIn("OB-1", rendered)
        self.assertIn("OB-2", rendered)
        self.assertIn("None of them can be dropped by saying nothing", rendered)

    def test_a_program_with_nothing_outstanding_says_so(self):
        slug = self.programs.create("Small program", "One PR after all.")
        self._plan("pln_aaaaaaaaaaaa", "The only PR")
        self.programs.add_child(slug, "pln_aaaaaaaaaaaa")
        rendered = plan_program.render(self.programs, self.programs.read(slug))
        self.assertIn("None outstanding", rendered)

    def test_a_pipe_in_a_child_title_does_not_break_the_table(self):
        slug = self.programs.create("A program", "Objective.")
        self._plan("pln_aaaaaaaaaaaa", "Before | after")
        self.programs.add_child(slug, "pln_aaaaaaaaaaaa")
        self.assertIn("Before \\| after", plan_program.render(self.programs, self.programs.read(slug)))


class ObligationHelpers(unittest.TestCase):
    def test_only_carried_obligations_create_a_debt(self):
        document = {"program": {"program_id": "prg_aaaaaaaaaaaa", "carried_obligations": [
            _obligation("A", "carried on"),
            _obligation("B", "done here", "satisfied"),
            _obligation("C", "let go", "released", reason="no longer relevant")]}}
        self.assertEqual(sorted(plan_program.carried_forward(document)), ["A"])

    def test_a_plan_with_no_program_block_carries_nothing(self):
        self.assertEqual(plan_program.carried_forward({}), {})


if __name__ == "__main__":
    unittest.main()
