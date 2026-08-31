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
import sys
import tempfile
import unittest
from unittest import mock

import plan_program
from plan_program import DEAD_BRANCH_STATES
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

    def _plan(self, plan_id, title, *obligations, program_id=None, predecessor=None):
        """A plan document, always carrying the program back-link `add_child` now requires.

        The back-link used to be written only when the fixture declared obligations, which was fine
        while membership was read from the program record alone. It is now load-bearing — it is the
        only evidence of membership that survives a program record that will not parse — so every
        fixture plan carries it, defaulting to the program this test case created.
        """
        document = _document(plan_id=plan_id, title=title)
        program = {"program_id": program_id or getattr(self, "program_id", "prg_aaaaaaaaaaaa")}
        if obligations:
            program["carried_obligations"] = list(obligations)
        if predecessor:
            program["predecessor_plan_id"] = predecessor
        document["program"] = program
        return self.plans.create(document), document

    def _program(self, title, objective):
        """Create a program and remember its minted id, so fixture plans can declare it."""
        slug = self.programs.create(title, objective)
        self.program_id = self.programs.read(slug)["program_id"]
        return slug

    def _two_pr_program(self):
        slug = self._program("Plan Coordinator", "A coordinator delivered across two PRs.")
        self._plan("pln_aaaaaaaaaaaa", "PR A",
                   _obligation("OB-1", "PR B cuts the Build Coordinator over to sealed handoffs."),
                   _obligation("OB-2", "PR B updates the plan-authority documentation and tests."))
        self.programs.add_child(slug, "pln_aaaaaaaaaaaa")
        return slug


class Creation(_Program):
    def test_a_program_starts_empty_and_says_so(self):
        slug = self._program("A program", "Deliver a thing across PRs.")
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
        slug = self._program("A program", "Objective.")
        self._plan("pln_aaaaaaaaaaaa", "PR A")
        with self.assertRaisesRegex(plan_program.ProgramError, "no predecessor to declare"):
            self.programs.add_child(slug, "pln_aaaaaaaaaaaa", predecessor="pln_aaaaaaaaaaaa")

    def test_the_same_plan_cannot_be_added_twice(self):
        slug = self._two_pr_program()
        with self.assertRaisesRegex(plan_program.ProgramError, "already a child"):
            self.programs.add_child(slug, "pln_aaaaaaaaaaaa", predecessor="pln_aaaaaaaaaaaa")

    def test_nothing_auto_selects_a_program(self):
        self._program("Only program", "Objective.")
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
        self.assertIn("updates the plan-authority documentation", message)
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

    def test_every_child_complete_does_not_derive_program_completion(self):
        """THE reproduction. This asserted `complete` against the prior code, and that was the bug.

        Deriving completion from the stored children makes `complete` mean "nothing left recorded":
        successors nobody has authored yet derive as done. It was observed live on this object's own
        program, which read complete with one of five planned pull requests landed.
        """
        slug = self._two_pr_program()
        self._plan("pln_bbbbbbbbbbbb", "PR B",
                   _obligation("OB-1", "Cut over.", "satisfied"),
                   _obligation("OB-2", "Amend the contracts.", "satisfied"))
        self.programs.add_child(slug, "pln_bbbbbbbbbbbb", predecessor="pln_aaaaaaaaaaaa")
        self._complete("pln_aaaaaaaaaaaa")
        self.assertEqual(self.programs.derived_status(self.programs.read(slug)), "in-progress")
        self._complete("pln_bbbbbbbbbbbb")
        self.assertEqual(self.programs.derived_status(self.programs.read(slug)), "children-complete")
        self.assertNotEqual(self.programs.derived_status(self.programs.read(slug)), "complete")

    def test_a_program_has_no_seal_of_its_own(self):
        schema = json.loads(plan_program.PROGRAM_SCHEMA.read_text(encoding="utf-8"))
        self.assertNotIn("seal", schema["properties"])
        self.assertNotIn("status", schema["properties"])
        # `complete` joined the closure states when completion stopped being derived. A program has
        # still never had a seal or a stored status: what it gained is a way for the OPERATOR to
        # record a judgment, not a way for the record to compute one.
        self.assertEqual(set(schema["$defs"]["closure"]["properties"]["state"]["enum"]),
                         {"retired", "abandoned", "complete"})

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
        # The one child carries OB-1, so closing now settles its books first: released on the record
        # with a reason, which is the door the refusal names.
        for obligation_id in ("OB-1", "OB-2"):
            self.programs.release(slug, "pln_aaaaaaaaaaaa", obligation_id,
                                  "the successor that would have answered was never written")
        self.programs.close(slug, "abandoned", "the split was wrong")
        self.assertEqual(self.programs.derived_status(self.programs.read(slug)), "abandoned")
        with self.assertRaisesRegex(plan_program.ProgramError, "already abandoned"):
            self.programs.close(slug, "retired", "again")
        self.programs.reopen(slug, "the split may be salvageable after all")
        self.assertEqual(self.programs.derived_status(self.programs.read(slug)), "in-progress")

    def test_a_closed_program_takes_no_new_children(self):
        slug = self._two_pr_program()
        for obligation_id in ("OB-1", "OB-2"):
            self.programs.release(slug, "pln_aaaaaaaaaaaa", obligation_id,
                                  "the work it awaited is void")
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
        self.assertIn("None can be dropped by saying nothing", rendered)

    def test_a_program_with_nothing_outstanding_says_so(self):
        slug = self._program("Small program", "One PR after all.")
        self._plan("pln_aaaaaaaaaaaa", "The only PR")
        self.programs.add_child(slug, "pln_aaaaaaaaaaaa")
        rendered = plan_program.render(self.programs, self.programs.read(slug))
        self.assertIn("None outstanding", rendered)

    def test_a_pipe_in_a_child_title_does_not_break_the_table(self):
        slug = self._program("A program", "Objective.")
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


class ReleasedImpliesAReason(_Program):
    """The escape hatch's price, collected mechanically rather than by convention.

    Releasing an obligation is allowed and sometimes right. The stated reason is what separates a
    decision from an omission — and it was optional: the field had no conditional behind it, the
    projection printed "(no reason given)" and carried on, and nothing refused. That put an escape
    hatch inside the escape hatch, and the release that would have used it is the inconvenient one,
    where the reason matters most.

    So the rule is pinned for EVERY FUTURE release rather than asserted about the ones already
    written: at the schema, which is where a plan revision is minted, and at add_child, which is where
    a plan enters a program chain. Two gates because there are two ways in.
    """

    @staticmethod
    def _released(reason=None):
        return _obligation("OB-9", "Re-accept the settled specification documents.", "released", reason)

    def test_a_release_without_a_reason_does_not_validate(self):
        document = _document(plan_id="pln_cccccccccccc", title="Releases without saying why")
        document["program"] = {"program_id": "prg_aaaaaaaaaaaa",
                               "carried_obligations": [self._released()]}
        with self.assertRaises(plan_store.PlanStoreError):
            self.plans.create(document)

    def test_a_release_with_a_reason_validates(self):
        slug, _ = self._plan("pln_cccccccccccc", "Releases, and says why",
                             self._released("The corpus is stale; re-accepting it would record assent "
                                            "to text that no longer describes the engine."))
        self.assertEqual(len(self.plans.head(slug)["program"]["carried_obligations"]), 1)

    def test_whitespace_is_not_a_reason(self):
        # minLength catches the empty string. It cannot catch a space, which is the shape a session
        # under pressure actually produces, so the program-side check tests the text rather than its
        # length.
        document = _document(plan_id="pln_dddddddddddd", title="A blank reason")
        document["program"] = {"program_id": "prg_aaaaaaaaaaaa",
                               "carried_obligations": [self._released("   ")]}
        self.assertEqual([o["id"] for o in plan_program.unexplained_releases(document)], ["OB-9"])

    def test_add_child_refuses_an_unexplained_release_on_the_first_child_too(self):
        # There is no predecessor on a first child, so the carry-forward check cannot see this one —
        # which is exactly why the release check is separate and runs for every child. A first plan
        # can release something it inherited from outside the program.
        slug = self._program("Plan Coordinator", "Delivered across PRs.")
        document = _document(plan_id="pln_eeeeeeeeeeee", title="First child")
        document["program"] = {"program_id": self.program_id,
                               "carried_obligations": [self._released("a real reason")]}
        plan_slug = self.plans.create(document)
        # Reach past the schema on purpose, to prove the program gate stands on its own rather than
        # riding the schema's coat-tails: a record written before this rule existed must still be
        # refused entry to the chain. The store verifies its own digests, so the doctored document is
        # handed to add_child through the reader rather than written to disk.
        doctored = self.plans.head(plan_slug)
        doctored["program"]["carried_obligations"][0].pop("reason")
        with mock.patch.object(self.plans, "head", return_value=doctored), \
                self.assertRaisesRegex(plan_program.ProgramError, "without saying why"):
            self.programs.add_child(slug, "pln_eeeeeeeeeeee")

    def test_the_refusal_names_every_unexplained_release(self):
        document = _document(plan_id="pln_ffffffffffff", title="Two silent releases")
        document["program"] = {"program_id": "prg_aaaaaaaaaaaa", "carried_obligations": [
            {"id": "OB-8", "statement": "Sunset v1.", "state": "released"},
            {"id": "OB-9", "statement": "Re-accept the spec.", "state": "released"}]}
        self.assertEqual([o["id"] for o in plan_program.unexplained_releases(document)],
                         ["OB-8", "OB-9"])

    def test_only_the_escape_hatch_costs_a_reason(self):
        # The rule is about releasing, not about paperwork: carried and satisfied stay free.
        document = _document(plan_id="pln_999999999999", title="Ordinary states")
        document["program"] = {"program_id": "prg_aaaaaaaaaaaa", "carried_obligations": [
            _obligation("OB-1", "Cut over.", "carried"),
            _obligation("OB-2", "Amend the contracts.", "satisfied")]}
        self.assertEqual(plan_program.unexplained_releases(document), [])
        self.assertTrue(self.plans.create(document))


class TheChainIsAuthoritative(_Program):
    """Order comes from the predecessor edges, never from the stored `position` numbering.

    Each test here is written to fail against the code that preceded it. That matters more than usual:
    the defect being fixed is a REPORT that was wrong, and a test which passes either way vouches for
    nothing. The obvious fixture — permute the children array and assert the render is unchanged — is
    exactly such a test, because `child_view` already sorted by the stored `position` field, so array
    order never mattered and the assertion was green before the fix and after it. The fixtures below
    permute the position VALUES instead, which is what the old sort actually read.

    One exception, stated rather than glossed: `test_duplicate_positions_do_not_disturb_the_order`
    passes against the prior code too, because a stable sort left the array order intact. It is kept
    as a supplementary assertion, not as evidence — the reversal fixture is the one that discriminates.
    """

    def _chain(self, *ids):
        """A linear chain a -> b -> c ..., every child declaring the one before it."""
        slug = self._program("Chained", "Delivered across several PRs.")
        previous = None
        for plan_id in ids:
            self._plan(plan_id, f"Child {plan_id[-1]}", predecessor=previous)
            self.programs.add_child(slug, plan_id, predecessor=previous)
            previous = plan_id
        return slug

    def test_order_follows_the_edges_when_the_positions_contradict_them(self):
        slug = self._chain("pln_aaaaaaaaaaa1", "pln_aaaaaaaaaaa2", "pln_aaaaaaaaaaa3")
        record = self.programs.read(slug)
        # Reverse the stored numbering so it disagrees with the edges. The old ordering read these
        # numbers and would render 3, 2, 1; the chain still says 1, 2, 3 and that is what must win.
        for child, number in zip(record["children"], [3, 2, 1]):
            child["position"] = number
        self.programs._write(slug, record)
        self.assertEqual([child["plan_id"] for child in self.programs.child_view(record)],
                         ["pln_aaaaaaaaaaa1", "pln_aaaaaaaaaaa2", "pln_aaaaaaaaaaa3"])
        self.assertEqual([child["chain_ordinal"] for child in self.programs.child_view(record)],
                         [1, 2, 3])

    def test_duplicate_positions_do_not_disturb_the_order(self):
        slug = self._chain("pln_bbbbbbbbbbb1", "pln_bbbbbbbbbbb2", "pln_bbbbbbbbbbb3")
        record = self.programs.read(slug)
        for child in record["children"]:
            child["position"] = 1
        self.programs._write(slug, record)
        self.assertEqual([child["plan_id"] for child in self.programs.child_view(record)],
                         ["pln_bbbbbbbbbbb1", "pln_bbbbbbbbbbb2", "pln_bbbbbbbbbbb3"])

    def test_every_stored_child_appears_exactly_once_even_when_unreachable(self):
        slug = self._chain("pln_ccccccccccc1", "pln_ccccccccccc2")
        record = self.programs.read(slug)
        # A predecessor that is not a child of this program: the edge dangles, so nothing reaches
        # this row from the start of the chain. Dropping it would make the program look shorter than
        # it is — the exact lie `child_view` was written to refuse for missing plans.
        record["children"][1]["predecessor_plan_id"] = "pln_dddddddddddd"
        self.programs._write(slug, record)
        view = self.programs.child_view(record)
        self.assertEqual([child["plan_id"] for child in view],
                         ["pln_ccccccccccc1", "pln_ccccccccccc2"])
        self.assertEqual(view[1]["anomaly"], "dangling-predecessor")
        self.assertIn("no such child is in this program",
                      plan_program.render(self.programs, record))

    def test_a_cycle_is_reported_rather_than_looped_on(self):
        slug = self._chain("pln_eeeeeeeeeee1", "pln_eeeeeeeeeee2")
        record = self.programs.read(slug)
        record["children"][0]["predecessor_plan_id"] = "pln_eeeeeeeeeee2"   # 1 -> 2 -> 1
        self.programs._write(slug, record)
        view = self.programs.child_view(record)
        self.assertEqual(len(view), 2)
        self.assertTrue(all(child.get("anomaly") == "unreachable" for child in view))
        rendered = plan_program.render(self.programs, record)
        self.assertIn("lead in a circle", rendered)
        # No leaf exists, so the debt is UNKNOWN — and unknown must never print as a clean zero.
        self.assertIn("Cannot be computed", rendered)
        self.assertTrue(self.programs.obligation_report(record)["unknown"])


class ForkedChainsOweBothBranches(_Program):
    """`view[-1]` answered "what does the last row carry", not "what does this program still owe"."""

    def _fork(self, tail_state=None):
        """root -> (branch_a, branch_b). Both branches end; each carries its own debt."""
        slug = self._program("Forked", "One root, two branches.")
        self._plan("pln_100000000001", "Root")
        self.programs.add_child(slug, "pln_100000000001")
        self._plan("pln_100000000002", "Branch A",
                   _obligation("OB-A", "Branch A still owes this."), predecessor="pln_100000000001")
        self.programs.add_child(slug, "pln_100000000002", predecessor="pln_100000000001")
        self._plan("pln_100000000003", "Branch B",
                   _obligation("OB-B", "Branch B still owes this."), predecessor="pln_100000000001")
        self.programs.add_child(slug, "pln_100000000003", predecessor="pln_100000000001")
        if tail_state:
            # Branch B deliberately, because it is the LAST row: the code this replaces answered with
            # `view[-1]`, so retiring any earlier branch would let the old answer come out right by
            # accident and the fixture would pin nothing. Retiring the last row makes the two answers
            # disagree — old code reports the dead branch's debt, the fix reports the living one's.
            self.plans.update_record(
                self.plans.resolve("pln_100000000003"),
                lambda current: current.__setitem__(
                    "closure", {"state": tail_state, "at": "2026-01-01T00:00:00Z", "reason": "done"}))
        return slug

    def test_both_branches_debts_are_reported_not_only_the_last_row(self):
        record = self.programs.read(self._fork())
        self.assertEqual([o["id"] for o in self.programs.outstanding_obligations(record)],
                         ["OB-A", "OB-B"])

    def test_each_debt_says_which_branch_end_carries_it(self):
        record = self.programs.read(self._fork())
        by_leaf = self.programs.obligation_report(record)["by_leaf"]
        self.assertEqual({leaf: [o["id"] for o in obligations]
                          for leaf, obligations in by_leaf.items()},
                         {"pln_100000000002": ["OB-A"], "pln_100000000003": ["OB-B"]})

    def test_the_fork_itself_is_disclosed(self):
        rendered = plan_program.render(self.programs, self.programs.read(self._fork()))
        # Under "Where the chain branches" — a structural fact — never under the corruption heading.
        self.assertIn("Where the chain branches", rendered)
        self.assertIn("is the declared predecessor of", rendered)
        self.assertNotIn("What does not add up", rendered)

    def test_a_retired_branchs_debts_died_with_it(self):
        # The shape observed on a live shelf: one branch retired, still carrying obligations, one of
        # which the surviving branch deliberately RELEASED with a reason. Unioning it back in would
        # resurrect a debt someone consciously let go — a new wrong answer, not a fix.
        record = self.programs.read(self._fork(tail_state="retired"))
        self.assertEqual([o["id"] for o in self.programs.outstanding_obligations(record)], ["OB-A"])

    def test_an_abandoned_branchs_debts_died_with_it(self):
        record = self.programs.read(self._fork(tail_state="abandoned"))
        self.assertEqual([o["id"] for o in self.programs.outstanding_obligations(record)], ["OB-A"])


class UnknownIsNeverZero(_Program):
    """A corrupt program is the one case where "0 outstanding" would be worst, so it is refused."""

    def test_a_missing_child_makes_the_debt_unknown_not_zero(self):
        slug = self._program("Broken", "A child that is not in this library.")
        self._plan("pln_200000000001", "Present")
        self.programs.add_child(slug, "pln_200000000001")
        record = self.programs.read(slug)
        record["children"].append({"plan_id": "pln_200000000009", "position": 2,
                                   "added_at": "2026-01-01T00:00:00Z",
                                   "predecessor_plan_id": "pln_200000000001"})
        self.programs._write(slug, record)
        report = self.programs.obligation_report(record)
        self.assertEqual(report["obligations"], [])
        self.assertTrue(report["unknown"], "a missing child must make the debt unknown, not zero")
        self.assertIn("Cannot be computed", plan_program.render(self.programs, record))


class AForkIsNotADefect(_Program):
    """A fork is how a branch gets superseded. Filing it under corruption made the shelf's one real
    forked program read as damaged every time an operator looked at it."""

    def _superseded_fork(self):
        slug = self._program("Superseded", "One branch retired, one carried on.")
        self._plan("pln_500000000001", "Root")
        self.programs.add_child(slug, "pln_500000000001")
        for plan_id, title in (("pln_500000000002", "Abandoned branch"),
                               ("pln_500000000003", "Surviving branch")):
            self._plan(plan_id, title, predecessor="pln_500000000001")
            self.programs.add_child(slug, plan_id, predecessor="pln_500000000001")
        self.plans.update_record(
            self.plans.resolve("pln_500000000002"),
            lambda current: current.__setitem__(
                "closure", {"state": "retired", "at": "2026-01-01T00:00:00Z", "reason": "superseded"}))
        return self.programs.read(slug)

    def test_a_superseded_fork_is_not_reported_as_corruption(self):
        rendered = plan_program.render(self.programs, self._superseded_fork())
        self.assertIn("Where the chain branches", rendered)
        self.assertNotIn("What does not add up", rendered)
        self.assertIn("Nothing here needs fixing", rendered)

    def test_a_fork_with_two_live_branches_says_the_program_has_two_ends(self):
        slug = self._program("Live fork", "Two branches, both open.")
        self._plan("pln_510000000001", "Root")
        self.programs.add_child(slug, "pln_510000000001")
        for plan_id in ("pln_510000000002", "pln_510000000003"):
            self._plan(plan_id, "Branch", predecessor="pln_510000000001")
            self.programs.add_child(slug, plan_id, predecessor="pln_510000000001")
        rendered = plan_program.render(self.programs, self.programs.read(slug))
        self.assertIn("more than one of these branches is still open", rendered)
        self.assertNotIn("Nothing here needs fixing", rendered)

    def test_real_corruption_still_gets_the_alarming_heading(self):
        slug = self._program("Broken", "A dangling edge.")
        self._plan("pln_520000000001", "Root")
        self.programs.add_child(slug, "pln_520000000001")
        record = self.programs.read(slug)
        record["children"][0]["predecessor_plan_id"] = "pln_529999999999"
        self.programs._write(slug, record)
        self.assertIn("What does not add up", plan_program.render(self.programs, record))


class TheBackLinkIsRequiredToJoin(_Program):
    """Membership must survive a program record that will not parse; the back-link is how."""

    def test_a_plan_that_does_not_declare_the_program_is_refused(self):
        slug = self._program("Strict", "Every child declares its program.")
        document = _document(plan_id="pln_300000000001", title="No back-link")
        self.plans.create(document)
        with self.assertRaisesRegex(plan_program.ProgramError, "does not declare that it belongs"):
            self.programs.add_child(slug, "pln_300000000001")

    def test_the_refusal_names_the_fix(self):
        slug = self._program("Strict", "Every child declares its program.")
        self.plans.create(_document(plan_id="pln_300000000002", title="No back-link"))
        with self.assertRaisesRegex(plan_program.ProgramError, "program.program_id"):
            self.programs.add_child(slug, "pln_300000000002")

    def test_the_sealed_refusal_names_the_revise_step_a_clone_still_needs(self):
        # `clone` deliberately drops the program block, so "clone it" alone leaves the operator
        # failing this very check a second time. The message has to name the middle step.
        slug = self._program("Strict", "Every child declares its program.")
        plan_slug = self.plans.create(_document(plan_id="pln_300000000004", title="Sealed, no link"))
        self.plans.update_record(plan_slug, lambda current: current.__setitem__(
            "seal", {"revision": 1, "reviewed_digest": "sha256:" + "0" * 64,
                     "sealed_digest": "sha256:" + "0" * 64,
                     "build_plan_digest": "sha256:" + "0" * 64,
                     "at": "2026-01-01T00:00:00Z", "delta_judgment": "none"}))
        with self.assertRaises(plan_program.ProgramError) as caught:
            self.programs.add_child(slug, "pln_300000000004")
        self.assertIn("revise", str(caught.exception).lower())
        self.assertIn("clone", str(caught.exception).lower())

    def test_a_plan_declaring_this_program_joins(self):
        slug = self._program("Strict", "Every child declares its program.")
        self._plan("pln_300000000003", "Declares it")
        self.assertEqual(len(self.programs.add_child(slug, "pln_300000000003")["children"]), 1)


class OneBrokenRecordDoesNotHideTheRest(_Program):
    """`program_for_plan` validated every record and let any failure escape — so one malformed file
    refused the seal of every plan in the library, including plans in no program at all."""

    def _corrupt(self, slug, text):
        (self.programs.program_dir(slug) / plan_program.RECORD_FILENAME).write_text(
            text, encoding="utf-8")

    def test_a_schema_broken_record_still_owns_its_children(self):
        # Parseable but invalid: the children array is still readable, so ownership is attributable
        # and the owner's seal must still refuse. Fail CLOSED.
        slug = self._program("Owner", "Broken but readable.")
        self._plan("pln_400000000001", "Child")
        self.programs.add_child(slug, "pln_400000000001")
        record = self.programs.read(slug)
        record["schema_version"] = "engine-program.v99"
        self._corrupt(slug, json.dumps(record))
        membership = self.programs.program_membership("pln_400000000001")
        self.assertEqual(membership["slug"], slug)
        self.assertTrue(membership["unreadable"])

    def test_an_unrelated_broken_record_does_not_claim_a_standalone_plan(self):
        slug = self._program("Unrelated", "Nothing to do with the other plan.")
        self._plan("pln_400000000002", "Child")
        self.programs.add_child(slug, "pln_400000000002")
        self._corrupt(slug, "{not json at all")
        self.plans.create(_document(plan_id="pln_400000000003", title="Standalone"))
        membership = self.programs.program_membership("pln_400000000003")
        self.assertIsNone(membership["slug"])
        self.assertTrue(membership["unreadable"], "the broken record is still reported, not hidden")

    def test_an_unparseable_record_is_still_claimed_by_the_plans_own_back_link(self):
        # The fail-OPEN this split exists to prevent: without the back-link, a plan whose own program
        # cannot be parsed looks exactly like a plan in no program, and its carry-forward re-check
        # would be skipped in silence.
        slug = self._program("Claimed", "Unparseable, but the child says whose it is.")
        program_id = self.program_id
        self._plan("pln_400000000004", "Child")
        self.programs.add_child(slug, "pln_400000000004")
        self._corrupt(slug, "{not json at all")
        membership = self.programs.program_membership("pln_400000000004",
                                                      claimed_program_id=program_id)
        self.assertTrue(membership["claims_unreadable"],
                        "a plan whose own program cannot be read must not look standalone")


class TheSeamHoldsAtModuleLevel(_Program):
    """plan_program may READ the plan library. It may never write to it.

    Pinned as an ALLOWLIST over the syntax tree, not a search for two forbidden spellings. A literal
    grep for `plan_store.append_revision` would miss `self.plans.append_revision(...)`, which is how
    this module would actually reach it — and it would miss `update_record` entirely, which can stamp
    gate evidence onto a plan record without minting a revision at all. Every mutating door is closed
    by omission, including any added later, which a denylist cannot do.
    """

    # Exactly what plan_program actually calls on its PlanLibrary handle — `slugs` and `plan_dir`
    # were in here defensively and the module calls neither. Carrying an unused permission is how an
    # allowlist quietly turns back into a denylist.
    PERMITTED = {"resolve", "read_record", "head", "root"}
    # Module-level reads of plan_store itself — a different namespace from the library HANDLE, and the
    # one the seam is about. Enumerated rather than exempted by a shape rule, so adding a call here is
    # a visible edit to this list.
    PERMITTED_MODULE = {"PlanStoreError", "PlanLibrary", "derived_status", "FILE_MODE", "slug_for",
                        "ensure_dir"}

    def _forbidden(self, source: str) -> set:
        """Every plan-library access this source makes that the allowlist does not permit.

        THE ONE function the guard rests on, so a seeded violation is proven to turn the real
        assertion red rather than merely being visible to a detector standing next to it.
        """
        import ast
        forbidden = set()
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Attribute):
                continue
            value = node.value
            if isinstance(value, ast.Attribute) and value.attr == "plans":   # self.plans.<x>
                if node.attr not in self.PERMITTED:
                    forbidden.add(node.attr)
            elif isinstance(value, ast.Name) and value.id == "plan_store":   # plan_store.<x>
                if node.attr not in self.PERMITTED_MODULE:
                    forbidden.add(node.attr)
        return forbidden

    def _source(self):
        return (Path(plan_program.__file__)).read_text(encoding="utf-8")

    def _assert_pin_holds(self, source):
        forbidden = self._forbidden(source)
        self.assertEqual(forbidden, set(),
                         f"plan_program reached {sorted(forbidden)} on the plan library; it may only "
                         f"read ({sorted(self.PERMITTED)}). Minting or stamping a plan record is the "
                         "Project Manager's act, and plan_store has no seal check of its own.")

    def test_the_module_only_reads_the_plan_library(self):
        self._assert_pin_holds(self._source())

    def test_the_pin_goes_red_when_a_revision_would_be_minted(self):
        seeded = self._source() + "\ndef _seeded(self):\n    self.plans.append_revision(1, 2)\n"
        with self.assertRaises(AssertionError):        # the GUARD fails, not merely a detector
            self._assert_pin_holds(seeded)

    def test_the_pin_goes_red_when_a_record_would_be_stamped(self):
        # The spelling a literal grep for `append_revision` would sail straight past: no revision is
        # minted at all, and a seal could be written directly onto the plan record.
        seeded = self._source() + "\ndef _seeded(self):\n    self.plans.update_record(1, 2)\n"
        with self.assertRaises(AssertionError):
            self._assert_pin_holds(seeded)

    def test_the_pin_goes_red_on_a_mutating_module_function(self):
        seeded = self._source() + "\ndef _seeded():\n    plan_store.atomic_write('x', 'y')\n"
        with self.assertRaises(AssertionError):
            self._assert_pin_holds(seeded)


class ADebtOffTheChainIsUnknownNotAbsent(_Program):
    """A detached cycle beside a healthy root printed "None outstanding" and "lead in a circle" on
    the same page — the obligations on the loop belonged to no branch, so the union never saw them
    and the no-leaf guard never fired because the healthy branch still had a leaf."""

    def _detached_loop(self):
        slug = self._program("Detached", "A healthy root, plus a loop that carries a real debt.")
        self._plan("pln_600000000001", "Root")
        self.programs.add_child(slug, "pln_600000000001")
        self._plan("pln_600000000002", "In the loop",
                   _obligation("OB-LOST", "This debt sits off the chain."),
                   predecessor="pln_600000000001")
        self.programs.add_child(slug, "pln_600000000002", predecessor="pln_600000000001")
        self._plan("pln_600000000003", "Also in the loop",
                   _obligation("OB-LOST", "This debt sits off the chain."),
                   predecessor="pln_600000000002")
        self.programs.add_child(slug, "pln_600000000003", predecessor="pln_600000000002")
        record = self.programs.read(slug)
        record["children"][1]["predecessor_plan_id"] = "pln_600000000003"   # 2 -> 3 -> 2, detached
        self.programs._write(slug, record)
        return record

    def test_the_debt_is_reported_as_unknown_rather_than_absent(self):
        report = self.programs.obligation_report(self._detached_loop())
        self.assertTrue(report["unknown"], "a debt that sits on no branch must not simply vanish")
        self.assertIn("OB-LOST", " ".join(report["unknown"]))

    def test_the_render_never_says_none_outstanding_while_a_debt_is_stranded(self):
        rendered = plan_program.render(self.programs, self._detached_loop())
        self.assertIn("lead in a circle", rendered)
        self.assertNotIn("_None outstanding._", rendered)
        self.assertIn("Cannot be computed", rendered)

    def test_the_one_line_summary_agrees(self):
        record = self._detached_loop()
        self.assertTrue(self.programs.obligation_report(record)["unknown"])

    def test_a_dangling_end_still_owes_what_it_carries_and_says_so_once(self):
        """A broken edge is a fact about ORDER, not about whether the debt is owed.

        The first repair over-corrected: it excluded every off-chain child from the union, which
        turned a real, live, unanswered obligation into silence. And the repair before that reported
        it twice — attributed as a branch carry AND named as unattributable. The rule that settles
        both: nothing live succeeds this child, so it IS an end and its debt is owed there; the
        broken edge is disclosed on its own, once.
        """
        slug = self._program("Dangling", "A child whose predecessor is not in this program.")
        self._plan("pln_700000000001", "Root")
        self.programs.add_child(slug, "pln_700000000001")
        self._plan("pln_700000000002", "Dangling",
                   _obligation("OB-X", "Owed at a place the chain cannot reach."),
                   predecessor="pln_700000000001")
        self.programs.add_child(slug, "pln_700000000002", predecessor="pln_700000000001")
        record = self.programs.read(slug)
        record["children"][1]["predecessor_plan_id"] = "pln_799999999999"
        self.programs._write(slug, record)

        report = self.programs.obligation_report(record)
        self.assertEqual([o["id"] for o in report["obligations"]], ["OB-X"],
                         "a live end's debt is owed even when its predecessor edge is broken")
        self.assertEqual(list(report["by_leaf"]), ["pln_700000000002"])
        self.assertFalse(any("OB-X" in reason for reason in report["unknown"]),
                         "attributed and unattributable are two answers to one question")
        rendered = plan_program.render(self.programs, record)
        self.assertEqual(rendered.count("OB-X"), 1, "the same obligation must not be reported twice")
        self.assertIn("no such child is in this program", rendered)   # the edge, disclosed separately

    def test_a_live_child_whose_only_successor_died_is_the_branch_end(self):
        """The shape that still lost a debt silently: an open child carrying an unanswered obligation
        whose only successor was retired. It is not a structural leaf, and the dead leaf is excluded,
        so the program reported nothing outstanding while genuinely still owing it."""
        slug = self._program("Dead successor", "The branch's end was retired; its ancestor lives.")
        self._plan("pln_710000000001", "Still open",
                   _obligation("OB-LIVE", "Never answered; the successor was retired."))
        self.programs.add_child(slug, "pln_710000000001")
        self._plan("pln_710000000002", "Retired successor",
                   _obligation("OB-LIVE", "Never answered; the successor was retired."),
                   predecessor="pln_710000000001")
        self.programs.add_child(slug, "pln_710000000002", predecessor="pln_710000000001")
        self.plans.update_record(
            self.plans.resolve("pln_710000000002"),
            lambda current: current.__setitem__(
                "closure", {"state": "retired", "at": "2026-01-01T00:00:00Z", "reason": "stopped"}))

        report = self.programs.obligation_report(self.programs.read(slug))
        self.assertEqual([o["id"] for o in report["obligations"]], ["OB-LIVE"])
        self.assertEqual(list(report["by_leaf"]), ["pln_710000000001"],
                         "the live ancestor is the branch's end once its successor is dead")

    def test_the_render_says_WHY_a_debt_is_unresolved(self):
        """A debt that appears because a successor was abandoned must say so on the page.

        Attributing it correctly is not enough: an operator meets a number that moved and has to
        infer the reason from a status column in another section. The reasoning existed only in this
        module's docstring, which is not somewhere they read.
        """
        slug = self._program("Stopped", "The successor stopped without answering.")
        self._plan("pln_730000000001", "Still open",
                   _obligation("OB-OPEN", "Never answered."))
        self.programs.add_child(slug, "pln_730000000001")
        self._plan("pln_730000000002", "Abandoned successor",
                   _obligation("OB-OPEN", "Never answered."), predecessor="pln_730000000001")
        self.programs.add_child(slug, "pln_730000000002", predecessor="pln_730000000001")
        self.plans.update_record(
            self.plans.resolve("pln_730000000002"),
            lambda current: current.__setitem__(
                "closure", {"state": "abandoned", "at": "2026-01-01T00:00:00Z", "reason": "stopped"}))

        rendered = plan_program.render(self.programs, self.programs.read(slug))
        self.assertIn("stopped without doing so", rendered)
        self.assertIn("pln_730000000002", rendered.split("Obligations still carried")[1])

    def test_the_whole_dead_sub_chain_is_named_not_just_the_first(self):
        """A branch usually dies more than one plan deep.

        The live shelf's own case is B abandoned and then C abandoned after it. Naming only the
        immediate successor left an operator reading about one stopped plan while the table above
        showed two, with nothing in the narrative connecting them.
        """
        slug = self._program("Two deep", "The branch died over two plans.")
        self._plan("pln_770000000001", "Still open", _obligation("OB-2D", "Never answered."))
        self.programs.add_child(slug, "pln_770000000001")
        previous = "pln_770000000001"
        for plan_id in ("pln_770000000002", "pln_770000000003"):
            self._plan(plan_id, "Stopped", _obligation("OB-2D", "Never answered."),
                       predecessor=previous)
            self.programs.add_child(slug, plan_id, predecessor=previous)
            self.plans.update_record(
                self.plans.resolve(plan_id),
                lambda current: current.__setitem__(
                    "closure", {"state": "abandoned", "at": "2026-01-01T00:00:00Z",
                                "reason": "stopped"}))
            previous = plan_id

        rendered = plan_program.render(self.programs, self.programs.read(slug))
        narrative = rendered.split("Obligations still carried")[1]
        self.assertIn("pln_770000000002", narrative)
        self.assertIn("pln_770000000003", narrative,
                      "the second stopped plan is in the table; the narrative must account for it")
        self.assertIn("were meant to answer", narrative)   # plural, since two plans stopped

    def test_an_ordinary_branch_end_is_not_given_a_reason_it_does_not_have(self):
        slug = self._program("Ordinary", "Nothing stopped; this is just the end.")
        self._plan("pln_740000000001", "Tip", _obligation("OB-TIP", "Owed at the tip."))
        self.programs.add_child(slug, "pln_740000000001")
        rendered = plan_program.render(self.programs, self.programs.read(slug))
        self.assertIn("where that branch currently ends", rendered)
        self.assertNotIn("stopped without doing so", rendered)

    def test_a_broken_edge_mid_chain_does_not_resurrect_an_answered_debt(self):
        """"Not an end" is not the same test as "cannot reach an end", and the difference is a debt.

        A child whose own predecessor edge is broken but which still has a live successor is fine:
        its carries flow forward exactly as they always did, and the end that receives them already
        answers for them. Testing only for end-ness printed such a debt twice — once attributed at the
        real end, once as unattributable — and, when the end had SATISFIED it, brought it back from
        the dead as an outstanding unknown.
        """
        slug = self._program("Mid-chain dangle", "Broken edge, but the chain continues past it.")
        self._plan("pln_750000000001", "Root")
        self.programs.add_child(slug, "pln_750000000001")
        self._plan("pln_750000000002", "Broken edge",
                   _obligation("OB-M", "Handed forward."), predecessor="pln_750000000001")
        self.programs.add_child(slug, "pln_750000000002", predecessor="pln_750000000001")
        self._plan("pln_750000000003", "The real end",
                   _obligation("OB-M", "Handed forward.", state="satisfied"),
                   predecessor="pln_750000000002")
        self.programs.add_child(slug, "pln_750000000003", predecessor="pln_750000000002")
        record = self.programs.read(slug)
        record["children"][1]["predecessor_plan_id"] = "pln_759999999999"
        self.programs._write(slug, record)

        report = self.programs.obligation_report(record)
        self.assertEqual(report["obligations"], [], "the end satisfied it; nothing is owed")
        self.assertEqual(report["unknown"], [],
                         "a debt that reaches an end is accounted for, not unattributable")
        self.assertIn("no such child is in this program",
                      plan_program.render(self.programs, record))   # the edge, still disclosed

    def test_a_debt_that_can_reach_no_end_is_still_unknown(self):
        # The case the unknown path exists for must survive the narrowing.
        record = self._detached_loop() if hasattr(self, "_detached_loop") else None
        if record is None:
            slug = self._program("Detached", "A loop that carries a debt.")
            self._plan("pln_760000000001", "Root")
            self.programs.add_child(slug, "pln_760000000001")
            self._plan("pln_760000000002", "In the loop",
                       _obligation("OB-L", "Off the chain."), predecessor="pln_760000000001")
            self.programs.add_child(slug, "pln_760000000002", predecessor="pln_760000000001")
            self._plan("pln_760000000003", "Also in the loop",
                       _obligation("OB-L", "Off the chain."), predecessor="pln_760000000002")
            self.programs.add_child(slug, "pln_760000000003", predecessor="pln_760000000002")
            record = self.programs.read(slug)
            record["children"][1]["predecessor_plan_id"] = "pln_760000000003"
            self.programs._write(slug, record)
        report = self.programs.obligation_report(record)
        self.assertTrue(report["unknown"])
        self.assertTrue(any("OB-L" in reason for reason in report["unknown"]))

    def test_a_dead_child_off_the_chain_contributes_nothing(self):
        """A stopped branch's carries stopped with it — including when its edge is also broken. The
        unknown path must not resurrect them by another door."""
        slug = self._program("Dead and dangling", "An abandoned child with a broken edge.")
        self._plan("pln_720000000001", "Root")
        self.programs.add_child(slug, "pln_720000000001")
        self._plan("pln_720000000002", "Abandoned",
                   _obligation("OB-DEAD", "Let go when the branch stopped."),
                   predecessor="pln_720000000001")
        self.programs.add_child(slug, "pln_720000000002", predecessor="pln_720000000001")
        self.plans.update_record(
            self.plans.resolve("pln_720000000002"),
            lambda current: current.__setitem__(
                "closure", {"state": "abandoned", "at": "2026-01-01T00:00:00Z", "reason": "stopped"}))
        record = self.programs.read(slug)
        record["children"][1]["predecessor_plan_id"] = "pln_729999999999"
        self.programs._write(slug, record)

        report = self.programs.obligation_report(record)
        self.assertFalse(any("OB-DEAD" in reason for reason in report["unknown"]),
                         "a stopped branch's debt must not come back as 'unknown'")
        self.assertNotIn("pln_720000000002", report["by_leaf"])


class AFinishedProgramIsNotACorruptOne(_Program):
    """The false-alarm side of the silent-zero coin, and a regression this repair introduced.

    The cycle message keyed on "no live branch ends", which is true of a genuine cycle AND of a
    program whose every child was legitimately retired or abandoned. The second is not damage — it is
    a finished program — and reporting it as corruption tells the operator something the record does
    not say, exactly as reporting a corrupt program's debt as zero did.
    """

    def _closed(self, *states):
        slug = self._program("Closed", "Every child stopped.")
        previous = None
        for index, state in enumerate(states, start=1):
            plan_id = f"pln_a0000000000{index}"
            self._plan(plan_id, f"Child {index}", predecessor=previous)
            self.programs.add_child(slug, plan_id, predecessor=previous)
            if state:
                self.plans.update_record(
                    self.plans.resolve(plan_id),
                    lambda current, s=state: current.__setitem__(
                        "closure", {"state": s, "at": "2026-01-01T00:00:00Z", "reason": "stopped"}))
            previous = plan_id
        return slug

    def test_a_single_retired_child_is_not_reported_as_a_cycle(self):
        record = self.programs.read(self._closed("retired"))
        report = self.programs.obligation_report(record)
        self.assertEqual(report["unknown"], [], "a finished program is not a corrupt one")
        self.assertEqual(report["obligations"], [])
        self.assertNotIn("form a cycle", plan_program.render(self.programs, record))

    def test_a_wholly_abandoned_chain_is_not_reported_as_a_cycle(self):
        report = self.programs.obligation_report(self.programs.read(
            self._closed("abandoned", "abandoned")))
        self.assertEqual(report["unknown"], [])

    def test_a_chain_with_one_live_child_is_still_quiet(self):
        report = self.programs.obligation_report(self.programs.read(self._closed("retired", None)))
        self.assertEqual(report["unknown"], [])

    def test_a_genuine_all_live_cycle_still_warns(self):
        slug = self._closed(None, None)
        record = self.programs.read(slug)
        record["children"][0]["predecessor_plan_id"] = "pln_a00000000002"
        self.programs._write(slug, record)
        report = self.programs.obligation_report(record)
        self.assertTrue(any("form a cycle" in reason for reason in report["unknown"]),
                        "the warning must survive for the case it was written for")


class TheOrderCanBeReDecided(_Program):
    """Insert places work AHEAD of work already on the chain, and re-points the edge that reached it.

    These are NEW-CAPABILITY fixtures, and the label is deliberate. There is no defect here to
    reproduce red-then-green: `insert_child` did not exist, so every assertion below would fail
    against the prior code by AttributeError rather than by a wrong answer. Calling that a
    reproduction would dress a new door up as a repair, which is the kind of evidence inflation the
    red-then-green label exists to prevent. The genuine reproductions in this program's work are the
    ones aimed at readers that gave a wrong answer, and they say so where they live.

    What is worth pinning here is the second edge. Appending creates one answerability; inserting
    creates two, and the one an operator does not picture is the DISPLACED child — it stops
    succeeding what it used to and starts succeeding the newcomer. A verb that moved that edge
    without re-checking it would mint a debt nothing downstream ever had to answer for, which is the
    decay this whole object exists to prevent, arriving through the new door.
    """

    def _chain(self, *ids):
        slug = self._program("Re-decidable", "Delivered across several PRs, in an order that moved.")
        previous = None
        for plan_id in ids:
            self._plan(plan_id, f"Child {plan_id[-1]}", predecessor=previous)
            self.programs.add_child(slug, plan_id, predecessor=previous)
            previous = plan_id
        return slug

    def _seal(self, plan_id):
        plan_slug = self.plans.resolve(plan_id)
        digest = self.plans.read_record(plan_slug)["current"]["plan_digest"]
        self.plans.update_record(plan_slug, lambda r: r.update({"seal": {
            "revision": 1, "reviewed_digest": digest, "sealed_digest": digest,
            "build_plan_digest": digest, "at": "2026-08-29T03:00:00Z", "delta_judgment": "none"}}))

    def _complete(self, plan_id):
        self.plans.update_record(self.plans.resolve(plan_id), lambda r: r.update({"closure": {
            "state": "complete", "at": "2026-08-29T05:00:00Z", "reason": "merged"}}))

    def _edges(self, slug):
        return {child["plan_id"]: child.get("predecessor_plan_id")
                for child in self.programs.read(slug)["children"]}

    def test_inserting_mid_chain_re_points_both_edges_and_reorders(self):
        slug = self._chain("pln_1aaaaaaaaaaa", "pln_1bbbbbbbbbbb", "pln_1ccccccccccc")
        self._plan("pln_1eeeeeeeeeee", "Child X")
        record = self.programs.insert_child(slug, "pln_1eeeeeeeeeee", before="pln_1bbbbbbbbbbb")
        self.assertEqual(self._edges(slug), {
            "pln_1aaaaaaaaaaa": None,
            "pln_1eeeeeeeeeee": "pln_1aaaaaaaaaaa",   # takes the displaced child's former edge
            "pln_1bbbbbbbbbbb": "pln_1eeeeeeeeeee",   # and the displaced child now succeeds it
            "pln_1ccccccccccc": "pln_1bbbbbbbbbbb",
        })
        self.assertEqual([child["plan_id"] for child in self.programs.child_view(record)],
                         ["pln_1aaaaaaaaaaa", "pln_1eeeeeeeeeee",
                          "pln_1bbbbbbbbbbb", "pln_1ccccccccccc"])
        self.assertEqual([child["chain_ordinal"] for child in self.programs.child_view(record)],
                         [1, 2, 3, 4])

    def test_the_inserted_entry_carries_no_position(self):
        slug = self._chain("pln_2aaaaaaaaaaa", "pln_2bbbbbbbbbbb")
        self._plan("pln_2eeeeeeeeeee", "Child X")
        self.programs.insert_child(slug, "pln_2eeeeeeeeeee", before="pln_2bbbbbbbbbbb")
        inserted = next(child for child in self.programs.read(slug)["children"]
                        if child["plan_id"] == "pln_2eeeeeeeeeee")
        self.assertNotIn("position", inserted)

    def test_appending_no_longer_writes_a_position_either(self):
        """The stored field is dead on BOTH doors, not merely absent from the new one."""
        slug = self._chain("pln_3aaaaaaaaaaa", "pln_3bbbbbbbbbbb")
        for child in self.programs.read(slug)["children"]:
            self.assertNotIn("position", child)

    def test_a_record_whose_children_carry_no_position_validates(self):
        slug = self._chain("pln_4aaaaaaaaaaa", "pln_4bbbbbbbbbbb")
        record = self.programs.read(slug)          # read() validates against the schema
        self.assertTrue(all("position" not in child for child in record["children"]))
        self.assertIn("Child a", plan_program.render(self.programs, record))

    def test_a_legacy_record_still_carrying_position_stays_valid(self):
        """Records written before the field died must not become unreadable."""
        slug = self._chain("pln_5aaaaaaaaaaa", "pln_5bbbbbbbbbbb")
        record = self.programs.read(slug)
        for number, child in enumerate(record["children"], start=1):
            child["position"] = number
        self.programs._write(slug, record)         # _write validates too
        self.assertEqual([child["plan_id"] for child in
                          self.programs.child_view(self.programs.read(slug))],
                         ["pln_5aaaaaaaaaaa", "pln_5bbbbbbbbbbb"])

    def test_inserting_before_the_first_child_makes_the_new_plan_the_root(self):
        slug = self._chain("pln_6aaaaaaaaaaa", "pln_6bbbbbbbbbbb")
        self._plan("pln_6eeeeeeeeeee", "Child X")
        record = self.programs.insert_child(slug, "pln_6eeeeeeeeeee", before="pln_6aaaaaaaaaaa")
        self.assertEqual(self._edges(slug), {
            "pln_6eeeeeeeeeee": None,
            "pln_6aaaaaaaaaaa": "pln_6eeeeeeeeeee",
            "pln_6bbbbbbbbbbb": "pln_6aaaaaaaaaaa",
        })
        self.assertEqual(plan_program.chain_analysis(record)["roots"], ["pln_6eeeeeeeeeee"])

    def test_inserting_ahead_of_merged_history_is_refused_naming_appended_work(self):
        slug = self._chain("pln_7aaaaaaaaaaa", "pln_7bbbbbbbbbbb")
        self._complete("pln_7bbbbbbbbbbb")
        self._plan("pln_7eeeeeeeeeee", "Child X")
        with self.assertRaises(plan_program.ProgramError) as caught:
            self.programs.insert_child(slug, "pln_7eeeeeeeeeee", before="pln_7bbbbbbbbbbb")
        message = str(caught.exception)
        self.assertIn("is complete", message)
        self.assertIn("program add --after", message)
        # And the record is untouched: a refusal that half-wrote would be worse than no verb.
        self.assertEqual(self._edges(slug),
                         {"pln_7aaaaaaaaaaa": None, "pln_7bbbbbbbbbbb": "pln_7aaaaaaaaaaa"})

    def test_a_sealed_displaced_child_that_cannot_answer_is_told_to_supersede_it(self):
        slug = self._chain("pln_8aaaaaaaaaaa", "pln_8bbbbbbbbbbb")
        self._seal("pln_8bbbbbbbbbbb")
        self._plan("pln_8eeeeeeeeeee", "Child X",
                   _obligation("OB-NEW", "The displaced child must answer for this."))
        with self.assertRaises(plan_program.ProgramError) as caught:
            self.programs.insert_child(slug, "pln_8eeeeeeeeeee", before="pln_8bbbbbbbbbbb")
        message = str(caught.exception)
        self.assertIn("OB-NEW", message)
        self.assertIn("SEALED", message)
        self.assertIn("program supersede", message)

    def test_an_active_displaced_child_is_not_sent_at_a_verb_that_would_refuse_it(self):
        """REGRESSION, round 2. An ACTIVE plan carries a seal too, so testing bool(seal) sent the
        operator to `program supersede` — which refuses an active target. A real verb, named as the
        fix, that would refuse the moment it was run: the round-1 dead-end shape, a third time.
        """
        slug = self._chain("pln_00000000ba01", "pln_00000000bb01")
        self._seal("pln_00000000bb01")
        self.plans.update_record(self.plans.resolve("pln_00000000bb01"),
                                 lambda r: r.update({"build_binding": _BUILD_BINDING}))
        self._plan("pln_00000000be01", "Child X",
                   _obligation("OB-NEW", "The displaced child must answer for this."))
        with self.assertRaises(plan_program.ProgramError) as caught:
            self.programs.insert_child(slug, "pln_00000000be01", before="pln_00000000bb01")
        message = str(caught.exception)
        self.assertIn("OB-NEW", message)
        self.assertIn("A Build is bound to", message)
        # Naming supersede here is honest ONLY because the step that opens it is named too. The
        # other route — letting the Build merge — leads somewhere else entirely, and the message
        # must say so rather than implying supersede works at the end of both.
        self.assertIn("ABANDON that Build", message)
        self.assertIn("`program supersede", message)
        self.assertIn("program add --after", message)
        self.assertIn("merged history is not replaced", message)

    def test_an_open_displaced_child_that_cannot_answer_is_told_to_revise_it(self):
        """The sealed refusal names supersede because revision is closed. Here it is open."""
        slug = self._chain("pln_9aaaaaaaaaaa", "pln_9bbbbbbbbbbb")
        self._plan("pln_9eeeeeeeeeee", "Child X",
                   _obligation("OB-NEW", "The displaced child must answer for this."))
        with self.assertRaises(plan_program.ProgramError) as caught:
            self.programs.insert_child(slug, "pln_9eeeeeeeeeee", before="pln_9bbbbbbbbbbb")
        message = str(caught.exception)
        self.assertIn("OB-NEW", message)
        self.assertIn("Revise pln_9bbbbbbbbbbb", message)
        self.assertNotIn("supersede", message)

    def test_the_inserted_plan_must_answer_for_what_its_new_predecessor_carries(self):
        """The FIRST of the two edges: the newcomer now stands between predecessor and displaced."""
        slug = self._program("Two edges", "Both are checked.")
        self._plan("pln_aaaaaaaaaaa0", "Child A",
                   _obligation("OB-OLD", "Someone downstream must answer for this."))
        self.programs.add_child(slug, "pln_aaaaaaaaaaa0")
        self._plan("pln_bbbbbbbbbbb0", "Child B",
                   _obligation("OB-OLD", "Answered.", "satisfied"), predecessor="pln_aaaaaaaaaaa0")
        self.programs.add_child(slug, "pln_bbbbbbbbbbb0", predecessor="pln_aaaaaaaaaaa0")
        self._plan("pln_eeeeeeeeeee0", "Child X")          # says nothing about OB-OLD
        with self.assertRaises(plan_program.ProgramError) as caught:
            self.programs.insert_child(slug, "pln_eeeeeeeeeee0", before="pln_bbbbbbbbbbb0")
        message = str(caught.exception)
        self.assertIn("OB-OLD", message)
        self.assertIn("inserting it here makes it the plan that must", message)

    def test_an_insertion_that_answers_both_edges_lands(self):
        slug = self._program("Two edges answered", "Both are checked, and both are answered.")
        self._plan("pln_aaaaaaaaaaa7", "Child A",
                   _obligation("OB-OLD", "Someone downstream must answer for this."))
        self.programs.add_child(slug, "pln_aaaaaaaaaaa7")
        self._plan("pln_bbbbbbbbbbb7", "Child B",
                   _obligation("OB-OLD", "Answered.", "satisfied"), predecessor="pln_aaaaaaaaaaa7")
        self.programs.add_child(slug, "pln_bbbbbbbbbbb7", predecessor="pln_aaaaaaaaaaa7")
        # X answers for A's carry by re-declaring it, and carries nothing of its own, so B — which
        # already satisfies OB-OLD — answers for everything X hands on.
        self._plan("pln_eeeeeeeeeee7", "Child X",
                   _obligation("OB-OLD", "Still carried, now by X."))
        self.programs.insert_child(slug, "pln_eeeeeeeeeee7", before="pln_bbbbbbbbbbb7")
        self.assertEqual(self._edges(slug), {
            "pln_aaaaaaaaaaa7": None,
            "pln_eeeeeeeeeee7": "pln_aaaaaaaaaaa7",
            "pln_bbbbbbbbbbb7": "pln_eeeeeeeeeee7",
        })

    def test_a_plan_already_on_the_chain_cannot_be_inserted_again(self):
        slug = self._chain("pln_caaaaaaaaaaa", "pln_cbbbbbbbbbbb")
        with self.assertRaisesRegex(plan_program.ProgramError, "already a child"):
            self.programs.insert_child(slug, "pln_caaaaaaaaaaa", before="pln_cbbbbbbbbbbb")

    def test_inserting_before_a_plan_that_is_not_a_child_is_refused(self):
        slug = self._chain("pln_daaaaaaaaaaa", "pln_dbbbbbbbbbbb")
        self._plan("pln_deeeeeeeeeee", "Child X")
        self._plan("pln_dfffffffffff", "A stranger")
        with self.assertRaisesRegex(plan_program.ProgramError, "is not a child of this program"):
            self.programs.insert_child(slug, "pln_deeeeeeeeeee", before="pln_dfffffffffff")

    def test_a_closed_program_takes_no_insertion_either(self):
        slug = self._chain("pln_eaaaaaaaaaaa", "pln_ebbbbbbbbbbb")
        self.programs.close(slug, "retired", "superseded")
        self._plan("pln_eeeeeeeeeeee", "Child X")
        with self.assertRaisesRegex(plan_program.ProgramError, "reopen it first"):
            self.programs.insert_child(slug, "pln_eeeeeeeeeeee", before="pln_ebbbbbbbbbbb")

    def test_the_back_link_is_required_to_insert_as_much_as_to_add(self):
        """Both doors run one copy of the join checks, so neither can drift from the other."""
        slug = self._chain("pln_faaaaaaaaaaa", "pln_fbbbbbbbbbbb")
        self._plan("pln_feeeeeeeeeee", "Child X", program_id="prg_ffffffffffff")
        with self.assertRaisesRegex(plan_program.ProgramError, "does not declare that it belongs"):
            self.programs.insert_child(slug, "pln_feeeeeeeeeee", before="pln_fbbbbbbbbbbb")


_BUILD_BINDING = {
    "sealed_digest": "sha256:" + "a" * 64,
    "build_plan_digest": "sha256:" + "b" * 64,
    "at": "2026-08-29T07:00:00Z",
    "repository": "owner/repo",
    "pull_request": 7,
}


class ReplacementInPlace(_Program):
    """Supersede: a child that turned out wrong is replaced, and nothing is deleted to do it.

    The record half only. The ordered two-step — retire the plan first, then mark the record — lives
    in the Project Manager's command layer, because plan_program may not write the plan library at
    all, and `test_project_manager` drives it end to end. What is pinned here is the shape the
    program record ends up in, and every refusal decided before a single write.
    """

    def _chain(self, *ids):
        slug = self._program("Replaceable", "Delivered across PRs, one of which turned out wrong.")
        previous = None
        for plan_id in ids:
            self._plan(plan_id, f"Child {plan_id[-2:]}", predecessor=previous)
            self.programs.add_child(slug, plan_id, predecessor=previous)
            previous = plan_id
        return slug

    def _retire(self, plan_id, reason="replaced"):
        """The supersede target's half-state — sealed first, because supersede now refuses an
        unsealed target, and for two rounds these fixtures modelled supersessions of plans that
        could never legitimately have been superseded."""
        self._seal(plan_id)
        self.plans.update_record(self.plans.resolve(plan_id), lambda r: r.update({"closure": {
            "state": "retired", "at": "2026-08-29T06:00:00Z", "reason": reason}}))

    def _seal(self, plan_id):
        plan_slug = self.plans.resolve(plan_id)
        digest = self.plans.read_record(plan_slug)["current"]["plan_digest"]
        self.plans.update_record(plan_slug, lambda r: r.update({"seal": {
            "revision": 1, "reviewed_digest": digest, "sealed_digest": digest,
            "build_plan_digest": digest, "at": "2026-08-29T03:00:00Z", "delta_judgment": "none"}}))

    def _complete(self, plan_id):
        self.plans.update_record(self.plans.resolve(plan_id), lambda r: r.update({"closure": {
            "state": "complete", "at": "2026-08-29T05:00:00Z", "reason": "merged"}}))

    def _bind(self, plan_id):
        """A Build bound to this plan — what makes `derived_status` read `active`."""
        self.plans.update_record(self.plans.resolve(plan_id), lambda r: r.update({
            "build_binding": _BUILD_BINDING}))

    def _edges(self, slug):
        return {child["plan_id"]: child.get("predecessor_plan_id")
                for child in self.programs.read(slug)["children"]}

    def test_a_mid_chain_supersession_re_points_the_downstream_edges(self):
        slug = self._chain("pln_0000000000a1", "pln_0000000000b1", "pln_0000000000c1")
        self._plan("pln_0000000000d1", "Replacement for B")
        self._retire("pln_0000000000b1")
        record = self.programs.mark_superseded(slug, "pln_0000000000b1", "pln_0000000000d1")
        self.assertEqual(self._edges(slug), {
            "pln_0000000000a1": None,
            # The replaced child keeps its TRUE ancestry: it really did succeed A. Re-pointing it at
            # its own replacement would assert an answerability that never existed.
            "pln_0000000000b1": "pln_0000000000a1",
            "pln_0000000000d1": "pln_0000000000a1",   # the replacement inherits the place
            "pln_0000000000c1": "pln_0000000000d1",   # and everything downstream follows it
        })
        marked = next(c for c in record["children"] if c["plan_id"] == "pln_0000000000b1")
        self.assertEqual(marked["superseded_by"], "pln_0000000000d1")

    def test_nothing_is_deleted_and_the_replaced_child_says_what_replaced_it(self):
        slug = self._chain("pln_0000000000a2", "pln_0000000000b2")
        self._plan("pln_0000000000d2", "Replacement for B")
        self._retire("pln_0000000000b2")
        record = self.programs.mark_superseded(slug, "pln_0000000000b2", "pln_0000000000d2")
        self.assertEqual(len(record["children"]), 3)
        rendered = plan_program.render(self.programs, record)
        self.assertIn("superseded by `pln_0000000000d2`", rendered)
        self.assertIn("pln_0000000000b2", rendered)

    def test_superseding_merged_history_is_refused_naming_appended_work(self):
        slug = self._chain("pln_0000000000a3", "pln_0000000000b3")
        self._plan("pln_0000000000d3", "Replacement for B")
        self._complete("pln_0000000000b3")
        with self.assertRaises(plan_program.ProgramError) as caught:
            self.programs.supersede_check(slug, "pln_0000000000b3", "pln_0000000000d3")
        message = str(caught.exception)
        self.assertIn("is complete", message)
        self.assertIn("program add --after", message)

    def test_superseding_a_plan_with_a_build_running_is_refused_naming_the_build(self):
        slug = self._chain("pln_0000000000a4", "pln_0000000000b4")
        self._plan("pln_0000000000d4", "Replacement for B")
        self._bind("pln_0000000000b4")
        with self.assertRaises(plan_program.ProgramError) as caught:
            self.programs.supersede_check(slug, "pln_0000000000b4", "pln_0000000000d4")
        message = str(caught.exception)
        self.assertIn("ACTIVE", message)
        self.assertIn("ABANDON that Build", message)
        # Both routes must end at doors that open: superseding a merged plan is refused flat, so
        # the merge route names appended work, never "then supersede".
        self.assertIn("program add --after", message)

    def test_the_replacement_inherits_the_place_and_the_debt(self):
        slug = self._program("Inherited debt", "A replacement takes on what its place owed.")
        self._plan("pln_0000000000a5", "Child A",
                   _obligation("OB-1", "Someone after this must answer."))
        self.programs.add_child(slug, "pln_0000000000a5")
        self._plan("pln_0000000000b5", "Child B",
                   _obligation("OB-1", "Answered.", "satisfied"), predecessor="pln_0000000000a5")
        self.programs.add_child(slug, "pln_0000000000b5", predecessor="pln_0000000000a5")
        self._plan("pln_0000000000d5", "Replacement that forgot")   # says nothing about OB-1
        self._retire("pln_0000000000b5")
        with self.assertRaises(plan_program.ProgramError) as caught:
            self.programs.supersede_check(slug, "pln_0000000000b5", "pln_0000000000d5")
        message = str(caught.exception)
        self.assertIn("OB-1", message)
        self.assertIn("clone --supersedes", message)

    def test_a_half_completed_supersession_re_runs_to_convergence(self):
        """The crash window: the plan is retired, the record was never marked. Run it again."""
        slug = self._chain("pln_0000000000a6", "pln_0000000000b6")
        self._plan("pln_0000000000d6", "Replacement for B")
        self._retire("pln_0000000000b6")              # step 2 landed
        self.programs.mark_superseded(slug, "pln_0000000000b6", "pln_0000000000d6")
        before = self._edges(slug)
        # Step 3 again, on an already-marked record: converges rather than raising or duplicating.
        self.programs.mark_superseded(slug, "pln_0000000000b6", "pln_0000000000d6")
        self.assertEqual(self._edges(slug), before)
        self.assertEqual(len(self.programs.read(slug)["children"]), 3)

    def test_a_second_different_replacement_is_refused_rather_than_overwriting_the_first(self):
        slug = self._chain("pln_0000000000a7", "pln_0000000000b7")
        self._plan("pln_0000000000d7", "First replacement")
        self._plan("pln_0000000000e7", "Second replacement")
        self._retire("pln_0000000000b7")
        self.programs.mark_superseded(slug, "pln_0000000000b7", "pln_0000000000d7")
        with self.assertRaises(plan_program.ProgramError) as caught:
            self.programs.supersede_check(slug, "pln_0000000000b7", "pln_0000000000e7")
        self.assertIn("was already superseded by pln_0000000000d7", str(caught.exception))

    def test_the_decay_sweep_goes_quiet_on_a_superseded_child(self):
        """Otherwise every deliberate supersession mints a permanent, unanswerable complaint."""
        slug = self._program("Decay", "A superseded child must not nag forever.")
        self._plan("pln_0000000000a8", "Child A")
        self.programs.add_child(slug, "pln_0000000000a8")
        self._plan("pln_0000000000b8", "Child B", predecessor="pln_0000000000a8")
        self.programs.add_child(slug, "pln_0000000000b8", predecessor="pln_0000000000a8")
        # A mints an obligation after B joined: exactly the decay the sweep exists to report.
        slug_a = self.plans.resolve("pln_0000000000a8")
        revised = dict(self.plans.head(slug_a))
        revised["program"] = {"program_id": self.program_id,
                              "carried_obligations": [
                                  _obligation("OB-LATE", "Minted after B joined.")]}
        revised["revision"] = 2
        revised["revision_note"] = "mint an obligation after the successor joined"
        self.plans.append_revision(slug_a, revised, expected_revision=1)
        self.assertTrue(self.programs.carry_forward_decay(slug), "the sweep should see it first")
        # The replacement answers for the late obligation — that is a separate guarantee, asserted
        # in its own fixture. What is under test here is that B stops being nagged about it.
        self._plan("pln_0000000000d8", "Replacement for B",
                   _obligation("OB-LATE", "Answered by the replacement.", "satisfied"))
        self._retire("pln_0000000000b8")
        self.programs.mark_superseded(slug, "pln_0000000000b8", "pln_0000000000d8")
        decay = self.programs.carry_forward_decay(slug)
        self.assertNotIn("pln_0000000000b8", [entry["plan_id"] for entry in decay])

    def test_superseding_the_first_child_is_not_reported_as_two_broken_chains(self):
        """The replacement becomes a second root, and the replaced one is retired. Not corruption."""
        slug = self._chain("pln_0000000000a9", "pln_0000000000b9")
        self._plan("pln_0000000000d9", "Replacement for A")
        self._retire("pln_0000000000a9")
        record = self.programs.mark_superseded(slug, "pln_0000000000a9", "pln_0000000000d9")
        rendered = plan_program.render(self.programs, record)
        self.assertNotIn("several disconnected chains", rendered)

    def test_two_live_roots_are_still_reported(self):
        """The liveness filter must not swallow the real alarm it was narrowed around."""
        slug = self._chain("pln_0000000000af", "pln_0000000000bf")
        record = self.programs.read(slug)
        record["children"][1].pop("predecessor_plan_id")
        self.programs._write(slug, record)
        self.assertIn("several disconnected chains",
                      plan_program.render(self.programs, self.programs.read(slug)))

    def test_supersede_runs_the_join_checks_and_cannot_swallow_a_debt(self):
        """REGRESSION. The carry-forward comparison used to be nested inside a test for the
        replacement's back-link, so a replacement declaring no program — or a different one —
        skipped the comparison entirely and was joined anyway. Reproduced against that code: the
        predecessor's obligation vanished from `obligation_report`, which then let the program be
        closed and even recorded complete over a debt nobody answered for. Supersede is a JOIN and
        must run the same checks every other join door runs.
        """
        slug = self._program("Join checks", "A replacement joins, and joining has rules.")
        self._plan("pln_0000000000fa", "Child A",
                   _obligation("OB-1", "Someone after this must answer."))
        self.programs.add_child(slug, "pln_0000000000fa")
        self._plan("pln_0000000000fb", "Child B", _obligation("OB-1", "Answered.", "satisfied"),
                   predecessor="pln_0000000000fa")
        self.programs.add_child(slug, "pln_0000000000fb", predecessor="pln_0000000000fa")
        self._retire("pln_0000000000fb")

        # A replacement belonging to a DIFFERENT program, saying nothing about OB-1.
        self._plan("pln_0000000000fc", "A stranger", program_id="prg_ffffffffffff")
        with self.assertRaises(plan_program.ProgramError) as caught:
            self.programs.mark_superseded(slug, "pln_0000000000fb", "pln_0000000000fc")
        self.assertIn("does not declare that it belongs", str(caught.exception))
        # And the debt is still on the books, which is the fact the old code destroyed.
        report = self.programs.obligation_report(self.programs.read(slug))
        self.assertEqual([o["id"] for o in report["obligations"]], ["OB-1"])

        # A replacement in the right program that simply forgets the debt is refused too.
        self._plan("pln_0000000000fd", "A forgetful replacement")
        with self.assertRaises(plan_program.ProgramError) as caught:
            self.programs.mark_superseded(slug, "pln_0000000000fb", "pln_0000000000fd")
        self.assertIn("OB-1", str(caught.exception))
        self.assertEqual(
            [o["id"] for o in
             self.programs.obligation_report(self.programs.read(slug))["obligations"]],
            ["OB-1"])

    def test_a_plan_already_on_the_chain_cannot_also_take_another_s_place(self):
        slug = self._chain("pln_0000000000ga" .replace("g", "1"), "pln_0000000000gb".replace("g", "1"))
        self._plan("pln_00000000001c", "A third")
        self.programs.add_child(slug, "pln_00000000001c", predecessor="pln_00000000001b")
        self._retire("pln_00000000001b")
        with self.assertRaises(plan_program.ProgramError) as caught:
            self.programs.mark_superseded(slug, "pln_00000000001b", "pln_00000000001c")
        # The DISTINCTIVE message, not merely "already a child" — the shared join check raises that
        # phrase a moment later, so regexing on it alone passed whether this branch ran or not.
        self.assertIn("would then sit in two positions at once", str(caught.exception))

    def test_derivations_follow_the_record_edge_not_the_document_provenance_copy(self):
        """The divergence fixture. `clone --supersedes` writes the predecessor edge into the plan
        DOCUMENT as authoring-time provenance, and that copy is a second place the order appears.
        The program record is the sole authority; this re-points the record after authoring and
        asserts every derivation follows it while the document copy says something else.
        """
        slug = self._program("Two copies", "One authority, one note about it.")
        # Child 2 carries a debt child 3 does not answer for. THIS is what makes the fixture able to
        # fail: `carry_forward_decay` is the one derivation here that holds the plan library and so
        # could actually follow the document copy. Following the RECORD, child 3 succeeds child 1 and
        # owes nothing, so decay is empty; following the DOCUMENT, it succeeds child 2 and owes OB-2,
        # so decay is not. Without this obligation the decay assertion was [] either way — the
        # fixture asserted four derivations of which three structurally cannot consult the document
        # and the fourth could not tell the difference, which a reviewer proved by rewiring decay to
        # prefer the document and watching 322 tests stay green.
        self._plan("pln_000000000021", "Child 1")
        self.programs.add_child(slug, "pln_000000000021")
        self._plan("pln_000000000022", "Child 2",
                   _obligation("OB-2", "Whoever succeeds child 2 must answer for this."),
                   predecessor="pln_000000000021")
        self.programs.add_child(slug, "pln_000000000022", predecessor="pln_000000000021")
        self._plan("pln_000000000023", "Child 3",
                   _obligation("OB-2", "Answered.", "satisfied"), predecessor="pln_000000000022")
        self.programs.add_child(slug, "pln_000000000023", predecessor="pln_000000000022")
        # Child 3 now SATISFIES OB-2, which is what let it join after child 2. Rewrite its document
        # so it answers for nothing: against the record's edge (it will succeed child 1, who owes
        # nothing) that is fine, and against the document's copy (child 2, who owes OB-2) it is a
        # dropped obligation the decay sweep must report.
        head = dict(self.plans.head(self.plans.resolve("pln_000000000023")))
        head["program"] = {"program_id": self.program_id,
                           "predecessor_plan_id": "pln_000000000022"}
        head["revision"] = 2
        head["revision_note"] = "stop answering for OB-2"
        self.plans.append_revision(self.plans.resolve("pln_000000000023"), head,
                                   expected_revision=1)

        # The document of child 3 still says it succeeds child 2. Re-point the RECORD so it
        # succeeds child 1 instead: the two sources now disagree, deliberately.
        record = self.programs.read(slug)
        entry = next(c for c in record["children"] if c["plan_id"] == "pln_000000000023")
        entry["predecessor_plan_id"] = "pln_000000000021"
        self.programs._write(slug, record)
        document = self.plans.head(self.plans.resolve("pln_000000000023"))
        self.assertEqual(document["program"]["predecessor_plan_id"], "pln_000000000022",
                         "the document copy must still disagree, or this fixture proves nothing")

        record = self.programs.read(slug)
        analysis = plan_program.chain_analysis(record)
        # THE discriminator. Following the record, child 1 now has two successors and the chain
        # forks. Following the document's provenance copy, it would still be the flat line
        # 1 -> 2 -> 3 with no fork at all — so the fork's existence is what proves which source won.
        self.assertEqual([(f["predecessor_plan_id"], sorted(f["successors"]))
                          for f in analysis["forks"]],
                         [("pln_000000000021",
                           ["pln_000000000022", "pln_000000000023"])])
        view = {c["plan_id"]: c for c in self.programs.child_view(record)}
        self.assertEqual(view["pln_000000000023"]["predecessor_plan_id"], "pln_000000000021")
        self.assertIn("`pln_000000000021` is the declared predecessor of",
                      plan_program.render(self.programs, record))
        # THE discriminating assertion. Child 3 answers for nothing; the record says it succeeds
        # child 1, who is owed nothing, so the sweep is empty. Were any reader to follow the
        # document's copy instead, child 3 would succeed child 2 and OB-2 would be reported dropped.
        self.assertEqual(self.programs.carry_forward_decay(slug), [])
        # And prove that assertion can distinguish: pointed at the document's edge by hand, the very
        # same comparison DOES report the drop. Without this the empty list above proves nothing.
        following_the_document = plan_program.dropped_obligations(
            self.plans.head(self.plans.resolve("pln_000000000022")),
            self.plans.head(self.plans.resolve("pln_000000000023")))
        self.assertEqual([o["id"] for o in following_the_document], ["OB-2"])

    def test_supersede_checks_the_downstream_edge_it_creates(self):
        """REGRESSION, round 2. The first repair fixed the INHERITED edge and left the one supersede
        creates downstream. Everything that succeeded the replaced child comes to succeed the
        REPLACEMENT, so it must answer for what the replacement carries — the same edge `insert`
        already refused on, at a door that did not. Reproduced against the unfixed code: the
        replacement's new obligation vanished from `obligation_report`, which read `None
        outstanding` over a live, unanswered debt, and the completion gate saw nothing owed.
        """
        slug = self._program("Edge two", "The edge supersede creates, not only the one it inherits.")
        self._plan("pln_0000000000a0", "A")
        self.programs.add_child(slug, "pln_0000000000a0")
        self._plan("pln_0000000000c0", "S, the one that turned out wrong",
                   predecessor="pln_0000000000a0")
        self.programs.add_child(slug, "pln_0000000000c0", predecessor="pln_0000000000a0")
        self._plan("pln_0000000000d0", "D, downstream", predecessor="pln_0000000000c0")
        self.programs.add_child(slug, "pln_0000000000d0", predecessor="pln_0000000000c0")
        self._plan("pln_0000000000e0", "R, the replacement",
                   _obligation("OB-NEW", "Whoever comes after R must finish this."))
        self._retire("pln_0000000000c0")

        with self.assertRaises(plan_program.ProgramError) as caught:
            self.programs.mark_superseded(slug, "pln_0000000000c0", "pln_0000000000e0")
        message = str(caught.exception)
        self.assertIn("OB-NEW", message)
        self.assertIn("pln_0000000000d0", message)
        self.assertIn("Revise pln_0000000000d0", message)
        # Nothing was written, so the chain is untouched and the debt is still findable.
        self.assertEqual(
            {c["plan_id"]: c.get("predecessor_plan_id")
             for c in self.programs.read(slug)["children"]},
            {"pln_0000000000a0": None, "pln_0000000000c0": "pln_0000000000a0",
             "pln_0000000000d0": "pln_0000000000c0"})

    def test_a_sealed_downstream_child_that_cannot_answer_is_told_to_supersede_it_too(self):
        slug = self._program("Edge two, sealed", "The downstream child cannot be revised.")
        self._plan("pln_0000000000a1", "A")
        self.programs.add_child(slug, "pln_0000000000a1")
        self._plan("pln_0000000000c1", "S", predecessor="pln_0000000000a1")
        self.programs.add_child(slug, "pln_0000000000c1", predecessor="pln_0000000000a1")
        self._plan("pln_0000000000d1", "D, sealed", predecessor="pln_0000000000c1")
        self.programs.add_child(slug, "pln_0000000000d1", predecessor="pln_0000000000c1")
        self._seal("pln_0000000000d1")
        self._plan("pln_0000000000e1", "R", _obligation("OB-NEW", "Someone must finish this."))
        self._retire("pln_0000000000c1")
        with self.assertRaises(plan_program.ProgramError) as caught:
            self.programs.mark_superseded(slug, "pln_0000000000c1", "pln_0000000000e1")
        self.assertIn("SEALED", str(caught.exception))
        self.assertIn("program supersede pln_0000000000d1", str(caught.exception))

    def test_a_dead_downstream_child_is_owed_nothing_and_does_not_bar_the_supersession(self):
        """A retired successor answers for nothing, so it must not block a replacement either."""
        slug = self._program("Edge two, dead", "The downstream child was abandoned.")
        self._plan("pln_0000000000a2", "A")
        self.programs.add_child(slug, "pln_0000000000a2")
        self._plan("pln_0000000000c2", "S", predecessor="pln_0000000000a2")
        self.programs.add_child(slug, "pln_0000000000c2", predecessor="pln_0000000000a2")
        self._plan("pln_0000000000d2", "D, abandoned", predecessor="pln_0000000000c2")
        self.programs.add_child(slug, "pln_0000000000d2", predecessor="pln_0000000000c2")
        self.plans.update_record(self.plans.resolve("pln_0000000000d2"),
                                 lambda r: r.update({"closure": {
                                     "state": "abandoned", "at": "2026-08-29T06:00:00Z",
                                     "reason": "dropped"}}))
        self._plan("pln_0000000000e2", "R", _obligation("OB-NEW", "Someone must finish this."))
        self._retire("pln_0000000000c2")
        record = self.programs.mark_superseded(slug, "pln_0000000000c2", "pln_0000000000e2")
        self.assertEqual(
            next(c for c in record["children"]
                 if c["plan_id"] == "pln_0000000000c2")["superseded_by"], "pln_0000000000e2")

    def test_a_plan_cannot_supersede_itself(self):
        slug = self._chain("pln_0000000000ae", "pln_0000000000be")
        with self.assertRaisesRegex(plan_program.ProgramError, "cannot supersede itself"):
            self.programs.supersede_check(slug, "pln_0000000000be", "pln_0000000000be")

    def test_superseding_something_that_is_not_a_child_is_refused(self):
        slug = self._chain("pln_0000000000ad", "pln_0000000000bd")
        self._plan("pln_0000000000cd", "A stranger")
        self._plan("pln_0000000000dd", "Replacement")
        with self.assertRaisesRegex(plan_program.ProgramError, "is not a child of this program"):
            self.programs.supersede_check(slug, "pln_0000000000cd", "pln_0000000000dd")


class EndsThatSettleTheirBooks(_Program):
    """Closing a program used to leave its debts reporting as outstanding under a closed status.

    Two of the assertions here are genuine red-then-green reproductions of readers that gave a wrong
    answer, and they say which: `close` succeeded over outstanding debts, and `derived_status`
    returned `complete` for a program whose objective nobody had judged met. The rest — the release
    verb, the acknowledged-unknown path, the completion verb — are new doors, and the fixtures for
    them are labelled as new capability rather than dressed up as repairs.
    """

    def _seal_plan(self, plan_id):
        plan_slug = self.plans.resolve(plan_id)
        digest = self.plans.read_record(plan_slug)["current"]["plan_digest"]
        self.plans.update_record(plan_slug, lambda r: r.update({"seal": {
            "revision": 1, "reviewed_digest": digest, "sealed_digest": digest,
            "build_plan_digest": digest, "at": "2026-08-29T03:00:00Z", "delta_judgment": "none"}}))

    def _retire_plan(self, plan_id):
        self.plans.update_record(self.plans.resolve(plan_id), lambda r: r.update({"closure": {
            "state": "retired", "at": "2026-08-29T06:00:00Z", "reason": "stopped"}}))

    def _abandon_plan(self, plan_id):
        self.plans.update_record(self.plans.resolve(plan_id), lambda r: r.update({"closure": {
            "state": "abandoned", "at": "2026-08-29T06:00:00Z", "reason": "dropped"}}))

    def _complete_plan(self, plan_id):
        self.plans.update_record(self.plans.resolve(plan_id), lambda r: r.update({"closure": {
            "state": "complete", "at": "2026-08-29T05:00:00Z", "reason": "merged"}}))

    def _orphaned_debt(self):
        """The live shelf's shape, with synthetic ids: a completed child carrying debts whose
        successors were every one of them abandoned without releasing anything."""
        slug = self._program("Orphaned", "A program whose successors were abandoned.")
        self._plan("pln_00000000000a", "Child A",
                   _obligation("MECH-ONE", "The successor was to finish this."),
                   _obligation("MECH-TWO", "And this."))
        self.programs.add_child(slug, "pln_00000000000a")
        self._plan("pln_00000000000b", "Child B",
                   _obligation("MECH-ONE", "Still carried."),
                   _obligation("MECH-TWO", "Still carried."),
                   predecessor="pln_00000000000a")
        self.programs.add_child(slug, "pln_00000000000b", predecessor="pln_00000000000a")
        self._complete_plan("pln_00000000000a")
        self._abandon_plan("pln_00000000000b")
        return slug

    # -- reproduction: close settled nothing --------------------------------------------------

    def test_closing_over_a_readable_debt_is_refused_and_names_the_release_verb(self):
        """RED-THEN-GREEN. Against the prior code this close SUCCEEDED, leaving the debt
        outstanding under a closed status — owed by nobody and answerable by nothing."""
        slug = self._orphaned_debt()
        with self.assertRaises(plan_program.ProgramError) as caught:
            self.programs.close(slug, "retired", "setting it down")
        message = str(caught.exception)
        self.assertIn("MECH-ONE", message)
        self.assertIn("MECH-TWO", message)
        self.assertIn("program release", message)
        self.assertIsNone(self.programs.read(slug).get("closure"))

    def test_releasing_each_debt_lets_the_program_close(self):
        slug = self._orphaned_debt()
        for obligation_id in ("MECH-ONE", "MECH-TWO"):
            self.programs.release(slug, "pln_00000000000a", obligation_id,
                                  "the work it awaited was abandoned with its program, so it is void")
        self.assertEqual(self.programs.obligation_report(self.programs.read(slug))["obligations"], [])
        record = self.programs.close(slug, "retired", "setting it down")
        self.assertEqual(record["closure"]["state"], "retired")
        rendered = plan_program.render(self.programs, record)
        self.assertIn("MECH-ONE", rendered)
        self.assertIn("released at PROGRAM level", rendered)
        self.assertIn("abandoned with its program", rendered)

    def test_a_release_costs_a_reason(self):
        slug = self._orphaned_debt()
        with self.assertRaisesRegex(plan_program.ProgramError, "costs a reason"):
            self.programs.release(slug, "pln_00000000000a", "MECH-ONE", "   ")

    def test_a_release_is_refused_while_a_live_successor_could_answer(self):
        """The precondition that keeps this from becoming the easy door around answering."""
        slug = self._program("Live successor", "Somebody can still answer.")
        self._plan("pln_00000000001a", "Child A", _obligation("OB-1", "Answer this."))
        self.programs.add_child(slug, "pln_00000000001a")
        self._plan("pln_00000000001b", "Child B", _obligation("OB-1", "Still carried."),
                   predecessor="pln_00000000001a")
        self.programs.add_child(slug, "pln_00000000001b", predecessor="pln_00000000001a")
        with self.assertRaises(plan_program.ProgramError) as caught:
            self.programs.release(slug, "pln_00000000001a", "OB-1", "cannot be bothered")
        message = str(caught.exception)
        self.assertIn("still has a live successor", message)
        self.assertIn("pln_00000000001b", message)

    def test_a_release_on_one_branch_end_leaves_the_other_branch_still_owing(self):
        """THE keying, proven. Two live branch ends owe the SAME obligation id; one branch's
        continuation was abandoned, so its debt is released there. The other still owes it.

        Released program-wide instead of per-child, this would clear both — which is the silent drop
        the whole object exists to prevent, arriving through the door built to prevent it.
        """
        slug = self._program("Forked", "Two live ends owe the same id.")
        self._plan("pln_00000000002a", "Root", _obligation("OB-1", "Both branches carry this."))
        self.programs.add_child(slug, "pln_00000000002a")
        # Branch one: X is complete and still carries OB-1; its only successor Y was abandoned, so
        # X is itself a live branch end and the debt sits on it with nothing left to answer.
        self._plan("pln_00000000002b", "Branch one", _obligation("OB-1", "Still carried."),
                   predecessor="pln_00000000002a")
        self.programs.add_child(slug, "pln_00000000002b", predecessor="pln_00000000002a")
        self._plan("pln_00000000002d", "Branch one, successor", _obligation("OB-1", "Still carried."),
                   predecessor="pln_00000000002b")
        self.programs.add_child(slug, "pln_00000000002d", predecessor="pln_00000000002b")
        self._complete_plan("pln_00000000002b")
        self._abandon_plan("pln_00000000002d")
        # Branch two: open, carrying OB-1, and perfectly able to answer for it.
        self._plan("pln_00000000002c", "Branch two", _obligation("OB-1", "Still carried."),
                   predecessor="pln_00000000002a")
        self.programs.add_child(slug, "pln_00000000002c", predecessor="pln_00000000002a")

        before = self.programs.obligation_report(self.programs.read(slug))
        self.assertEqual(sorted(before["by_leaf"]), ["pln_00000000002b", "pln_00000000002c"])

        self.programs.release(slug, "pln_00000000002b", "OB-1",
                              "this branch's continuation was abandoned, so the debt is void here")
        after = self.programs.obligation_report(self.programs.read(slug))
        self.assertNotIn("pln_00000000002b", after["by_leaf"])
        self.assertEqual([o["id"] for o in after["obligations"]], ["OB-1"])
        self.assertIn("pln_00000000002c", after["by_leaf"])

    def test_a_released_debt_stops_refusing_add_child_and_stops_the_decay_complaint(self):
        """Honored by every program-side reader, not only the report an operator reads."""
        slug = self._program("Honored", "A release must mean the same thing everywhere.")
        self._plan("pln_00000000003a", "Child A", _obligation("OB-1", "Answer this."))
        self.programs.add_child(slug, "pln_00000000003a")
        self._plan("pln_00000000003b", "Child B")          # answers for nothing
        with self.assertRaises(plan_program.ProgramError):
            self.programs.add_child(slug, "pln_00000000003b", predecessor="pln_00000000003a")
        self.programs.release(slug, "pln_00000000003a", "OB-1", "the debt is void")
        self.programs.add_child(slug, "pln_00000000003b", predecessor="pln_00000000003a")
        self.assertEqual(self.programs.carry_forward_decay(slug), [])

    # -- the acknowledged-unknown path -------------------------------------------------------

    def test_closing_over_an_unknown_takes_an_acknowledgement_rather_than_a_refusal(self):
        """New capability. Refusing here would wedge exactly the wrecked programs abandon exists
        for: unknown entries carry no obligation id, so no release could ever clear them."""
        slug = self._program("Wrecked", "Its record no longer parses cleanly.")
        self._plan("pln_00000000004a", "Child A")
        self.programs.add_child(slug, "pln_00000000004a")
        record = self.programs.read(slug)
        record["children"].append({"plan_id": "pln_00000000004f",
                                   "added_at": "2026-08-29T06:00:00Z",
                                   "predecessor_plan_id": "pln_00000000004a"})
        self.programs._write(slug, record)
        self.assertTrue(self.programs.obligation_report(self.programs.read(slug))["unknown"])
        with self.assertRaises(plan_program.ProgramError) as caught:
            self.programs.close(slug, "abandoned", "giving up on it")
        self.assertIn("--acknowledge-unknown", str(caught.exception))
        closed = self.programs.close(slug, "abandoned", "giving up on it",
                                     acknowledged_unknown="the missing child was never authored")
        self.assertEqual(closed["closure"]["acknowledged_unknown"],
                         "the missing child was never authored")
        self.assertIn("Closed over an unknown",
                      plan_program.render(self.programs, closed))

    # -- reproduction: completion was derived ------------------------------------------------

    def test_completion_is_recorded_by_a_verb_and_never_derived(self):
        """RED-THEN-GREEN on the derivation; the verb itself is new capability."""
        slug = self._program("Judged", "Finished when the operator says so.")
        self._plan("pln_00000000005a", "Child A")
        self.programs.add_child(slug, "pln_00000000005a")
        self._complete_plan("pln_00000000005a")
        record = self.programs.read(slug)
        # Against the prior code this read `complete` with one child on record and no judgment made.
        self.assertEqual(self.programs.derived_status(record), "children-complete")
        self.assertFalse(self.programs.status_is_recorded(record))
        completed = self.programs.complete(slug, "the objective is met")
        self.assertEqual(self.programs.derived_status(completed), "complete")
        self.assertTrue(self.programs.status_is_recorded(completed))
        self.assertEqual(completed["closure"]["reason"], "the objective is met")
        self.assertIn("at", completed["closure"])
        # And no attestation of anyone's words: struck as ceremony, since no local field can prove
        # someone was present, and one implying it would be false confidence.
        self.assertEqual(set(completed["closure"]), {"state", "at", "reason"})

    def test_the_children_complete_token_and_its_sentence_say_what_they_do_not_claim(self):
        slug = self._program("Not finished", "Five PRs planned; one written.")
        self._plan("pln_00000000006a", "Child A")
        self.programs.add_child(slug, "pln_00000000006a")
        self._complete_plan("pln_00000000006a")
        record = self.programs.read(slug)
        self.assertNotEqual(self.programs.derived_status(record), "complete")
        rendered = plan_program.render(self.programs, record)
        self.assertIn("This program is not recorded as complete", rendered)
        self.assertIn("UNKNOWN, not done", rendered)
        self.assertIn("derived from the children, never stored", rendered)

    def test_the_caption_tells_a_recorded_closure_from_a_derived_state(self):
        slug = self._program("Captions", "A stored decision must not read as a computation.")
        self._plan("pln_00000000007a", "Child A")
        self.programs.add_child(slug, "pln_00000000007a")
        self._complete_plan("pln_00000000007a")
        derived = plan_program.render(self.programs, self.programs.read(slug))
        self.assertIn("derived from the children, never stored", derived)
        recorded = plan_program.render(
            self.programs, self.programs.complete(slug, "the objective is met"))
        self.assertIn("recorded by an explicit close, not derived", recorded)
        self.assertNotIn("derived from the children, never stored", recorded)

    def test_complete_refuses_over_an_incomplete_live_child(self):
        """The operator's recorded WIDENING of the refuse-only-on-carry-forward boundary."""
        slug = self._program("Half done", "One child landed, one did not.")
        self._plan("pln_00000000008a", "Child A")
        self.programs.add_child(slug, "pln_00000000008a")
        self._plan("pln_00000000008b", "Child B", predecessor="pln_00000000008a")
        self.programs.add_child(slug, "pln_00000000008b", predecessor="pln_00000000008a")
        self._complete_plan("pln_00000000008a")
        with self.assertRaises(plan_program.ProgramError) as caught:
            self.programs.complete(slug, "close enough")
        self.assertIn("pln_00000000008b", str(caught.exception))
        self.assertIn("not complete", str(caught.exception))

    def test_complete_refuses_when_nothing_has_shipped(self):
        slug = self._program("Nothing shipped", "No child is complete.")
        self._plan("pln_00000000009a", "Child A")
        self.programs.add_child(slug, "pln_00000000009a")
        self._retire_plan("pln_00000000009a")
        with self.assertRaisesRegex(plan_program.ProgramError, "nothing has actually shipped"):
            self.programs.complete(slug, "calling it done")

    def test_complete_refuses_over_an_outstanding_debt_and_over_an_unknown(self):
        """Both halves of the name, because for two rounds this test constructed only the first —
        a reviewer read the title as evidence the unknown leg was covered, and it was not."""
        slug = self._orphaned_debt()
        with self.assertRaises(plan_program.ProgramError) as caught:
            self.programs.complete(slug, "done")
        self.assertIn("MECH-ONE", str(caught.exception))
        # And the unknown: a child the library does not hold. Written into the record directly,
        # the same wreckage `test_a_missing_child_makes_the_debt_unknown_not_zero` builds.
        record = self.programs.read(slug)
        record["children"].append({"plan_id": "pln_00000000009e",
                                   "added_at": "2026-01-01T00:00:00Z",
                                   "predecessor_plan_id": "pln_00000000000a"})
        self.programs._write(slug, record)
        blockers = "\n".join(self.programs.completion_blockers(self.programs.read(slug)))
        self.assertIn("cannot be computed", blockers)
        self.assertIn("pln_00000000009e", blockers)
        with self.assertRaisesRegex(plan_program.ProgramError, "cannot be recorded complete"):
            self.programs.complete(slug, "done")

    def test_completion_refuses_a_reason_that_is_only_whitespace(self):
        """The reason is the judgment's whole record; blank-but-technically-present is blank."""
        slug = self._program("Blank reason", "Completion records a judgment.")
        self._plan("pln_0000000000b9", "Child A")
        self.programs.add_child(slug, "pln_0000000000b9")
        self._complete_plan("pln_0000000000b9")
        with self.assertRaisesRegex(plan_program.ProgramError, "costs a reason"):
            self.programs.complete(slug, "   ")
        self.assertIsNone(self.programs.read(slug).get("closure"))
        self.assertEqual(self.programs.complete(slug, "met")["closure"]["state"], "complete")

    def test_a_missing_superseded_child_is_history_damage_not_open_books(self):
        """A deliberate supersession must not wedge the program when the replaced plan's file goes.

        The replaced child's debts moved to its replacement when the join checks ran, so a record
        that has lost the replaced plan can still answer everything the gates ask. Before this, one
        missing superseded plan forced `needs-attention` forever, made the books uncomputable, and
        blocked completion — an alarm with no door behind it.
        """
        slug = self._program("Replaced and gone", "The replaced plan's file was lost.")
        self._plan("pln_0000000000ba", "Child A")
        self.programs.add_child(slug, "pln_0000000000ba")
        self._plan("pln_0000000000bb", "Replacement for A")
        self._seal_plan("pln_0000000000ba")
        self._retire_plan("pln_0000000000ba")
        self.programs.mark_superseded(slug, "pln_0000000000ba", "pln_0000000000bb")
        self._complete_plan("pln_0000000000bb")
        # Lose the replaced plan's records entirely.
        import shutil
        shutil.rmtree(self.plans.root / self.plans.resolve("pln_0000000000ba"))
        record = self.programs.read(slug)
        self.assertNotEqual(self.programs.derived_status(record), "needs-attention")
        report = self.programs.obligation_report(record)
        self.assertEqual(report["unknown"], [])
        self.assertEqual(self.programs.completion_blockers(record), [])
        self.assertEqual(self.programs.complete(slug, "objective met")["closure"]["state"],
                         "complete")
        # An ordinary (non-superseded) missing child still alarms — the narrowing is exact.
        slug2 = self._program("Broken for real", "A live child is gone.")
        self._plan("pln_0000000000bc", "Child B")
        self.programs.add_child(slug2, "pln_0000000000bc")
        record2 = self.programs.read(slug2)
        record2["children"].append({"plan_id": "pln_0000000000bd",
                                    "added_at": "2026-01-01T00:00:00Z",
                                    "predecessor_plan_id": "pln_0000000000bc"})
        self.programs._write(slug2, record2)
        self.assertEqual(self.programs.derived_status(self.programs.read(slug2)),
                         "needs-attention")

    def test_a_superseded_child_does_not_bar_completion(self):
        slug = self._program("Replaced", "One child was replaced, the rest landed.")
        self._plan("pln_0000000000ba", "Child A")
        self.programs.add_child(slug, "pln_0000000000ba")
        self._plan("pln_0000000000bb", "Replacement for A")
        self._seal_plan("pln_0000000000ba")
        self._retire_plan("pln_0000000000ba")
        self.programs.mark_superseded(slug, "pln_0000000000ba", "pln_0000000000bb")
        self._complete_plan("pln_0000000000bb")
        record = self.programs.complete(slug, "the objective is met")
        self.assertEqual(record["closure"]["state"], "complete")

    # -- reopen keeps what it undoes ---------------------------------------------------------

    def test_a_debt_minted_after_its_successor_sealed_is_owed_and_releasable(self):
        """The third path is closed: mid-chain decay is a debt the gates see, with a door that opens.

        A reviewer hunting for a third path by which an obligation leaves the books found this one:
        the outstanding-debt report answered what the live branch ENDS carry, so an obligation
        minted after its successor sealed sat mid-chain where the report could not attribute it,
        and both closure gates read the report. The first fix — gating on the decay sweep — was
        reverted because it wedged: the sealed successor can never answer, and release refused
        while any live successor existed. The operator ruled that a sealed successor is not
        somewhere a debt can be answered, so the report now carries the decayed debt, the gates
        refuse over it, and `release` opens for exactly this shape. Asserted end to end here.
        """
        slug = self._program("Closed gap", "A debt the report now places.")
        self._plan("pln_00000000000f", "Child one")
        self.programs.add_child(slug, "pln_00000000000f")
        self._plan("pln_0000000000f2", "Child two", predecessor="pln_00000000000f")
        self.programs.add_child(slug, "pln_0000000000f2", predecessor="pln_00000000000f")
        digest = self.plans.read_record(
            self.plans.resolve("pln_0000000000f2"))["current"]["plan_digest"]
        self.plans.update_record(self.plans.resolve("pln_0000000000f2"), lambda r: r.update({
            "seal": {"revision": 1, "reviewed_digest": digest, "sealed_digest": digest,
                     "build_plan_digest": digest, "at": "2026-08-29T03:00:00Z",
                     "delta_judgment": "none"}}))
        slug_one = self.plans.resolve("pln_00000000000f")
        revised = dict(self.plans.head(slug_one))
        revised["program"] = {"program_id": self.program_id,
                              "carried_obligations": [
                                  _obligation("OB-LATE", "Minted after the successor sealed.")]}
        revised["revision"] = 2
        revised["revision_note"] = "mint an obligation the successor can never answer for"
        self.plans.append_revision(slug_one, revised, expected_revision=1)

        record = self.programs.read(slug)
        # The decay sweep sees it — and now so does the report, attributed to its carrier.
        self.assertEqual([e["plan_id"] for e in self.programs.carry_forward_decay(slug)],
                         ["pln_0000000000f2"])
        report = self.programs.obligation_report(record)
        self.assertEqual([o["id"] for o in report["obligations"]], ["OB-LATE"])
        self.assertIn("OB-LATE", str(report["by_leaf"].get("pln_00000000000f")))
        # The render tells the truth about WHERE it sits — mid-chain, not a branch end — and
        # names the one door that opens; the end-shaped sentence would be a lie printed three
        # lines under the table contradicting it.
        rendered = plan_program.render(self.programs, self.programs.read(slug))
        self.assertIn("MID-CHAIN", rendered)
        self.assertIn("program release", rendered)
        self.assertNotIn("where that branch currently ends", rendered)
        # Both closure gates refuse over it, naming the door that opens.
        with self.assertRaisesRegex(plan_program.ProgramError, "OB-LATE") as caught:
            self.programs.close(slug, "retired", "setting it down")
        self.assertIn("program release", str(caught.exception))
        self.assertIn("obligation(s) are still outstanding: OB-LATE",
                      "\n".join(self.programs.completion_blockers(self.programs.read(slug))))
        # The door opens: the only live successor is sealed, so it cannot take a revision, and the
        # release that used to refuse over it now records the decision.
        self.programs.release(slug, "pln_00000000000f", "OB-LATE",
                              "the successor sealed before this was minted; nothing can answer it")
        self.assertEqual(
            self.programs.close(slug, "retired", "setting it down")["closure"]["state"], "retired")

    def test_a_debt_awaiting_an_unsealed_successor_renders_the_revise_door(self):
        """The render must not tell a draft's operator that "no revision can answer": while any
        live successor is unsealed, revision IS the door and `release` refuses — a reviewer drove
        the false sentence and the refusing door together. Same shape as the sealed case, other
        truthful sentence.
        """
        slug = self._program("Awaiting", "The successor is still a draft.")
        self._plan("pln_00000000000f", "Child one")
        self.programs.add_child(slug, "pln_00000000000f")
        self._plan("pln_0000000000f2", "Child two, a draft", predecessor="pln_00000000000f")
        self.programs.add_child(slug, "pln_0000000000f2", predecessor="pln_00000000000f")
        slug_one = self.plans.resolve("pln_00000000000f")
        revised = dict(self.plans.head(slug_one))
        revised["program"] = {"program_id": self.program_id,
                              "carried_obligations": [
                                  _obligation("OB-LATE", "Minted after the successor joined.")]}
        revised["revision"] = 2
        revised["revision_note"] = "mint an obligation the draft successor has not yet answered"
        self.plans.append_revision(slug_one, revised, expected_revision=1)

        record = self.programs.read(slug)
        report = self.programs.obligation_report(record)
        self.assertEqual([o["id"] for o in report["obligations"]], ["OB-LATE"])
        self.assertIn("pln_00000000000f", report["decayed_awaiting"])
        self.assertNotIn("pln_00000000000f", report["decayed"])
        rendered = plan_program.render(self.programs, record)
        self.assertIn("Revise", rendered)
        self.assertIn("pln_0000000000f2", rendered)
        self.assertNotIn("no revision can answer", rendered)
        self.assertNotIn("program release", rendered)
        # And the doors behave as the sentences say: release refuses, the gates still refuse.
        with self.assertRaisesRegex(plan_program.ProgramError, "can be revised"):
            self.programs.release(slug, "pln_00000000000f", "OB-LATE", "trying the wrong door")
        with self.assertRaisesRegex(plan_program.ProgramError, "OB-LATE"):
            self.programs.close(slug, "retired", "setting it down")

    def test_a_dangling_marker_on_a_successor_does_not_open_the_release_door(self):
        """A reviewer's exact reproduction: mark the live draft successor with a supersession
        pointing at a plan that is not a child, and release used to walk straight past it."""
        slug = self._program("Dangling successor", "The marker names nobody.")
        self._plan("pln_00000000000f", "Child one", _obligation("OB-1", "Carried forward."))
        self.programs.add_child(slug, "pln_00000000000f")
        self._plan("pln_0000000000f2", "Child two", _obligation("OB-1", "Still carried."),
                   predecessor="pln_00000000000f")
        self.programs.add_child(slug, "pln_0000000000f2", predecessor="pln_00000000000f")
        record = self.programs.read(slug)
        for child in record["children"]:
            if child["plan_id"] == "pln_0000000000f2":
                child["superseded_by"] = "pln_ffffffffffff"     # names no child on this record
        self.programs._write(slug, record)
        with self.assertRaisesRegex(plan_program.ProgramError, "can be revised"):
            self.programs.release(slug, "pln_00000000000f", "OB-1", "walking past the marker")

    def test_release_fails_closed_when_a_successor_cannot_be_told(self):
        """Missing is not "unable to answer" — it may be a revisable draft behind a broken record,
        and this verb exists to be the hard door."""
        slug = self._program("Untellable", "A successor the library does not hold.")
        self._plan("pln_00000000000f", "Child one", _obligation("OB-1", "Carried forward."))
        self.programs.add_child(slug, "pln_00000000000f")
        record = self.programs.read(slug)
        record["children"].append({"plan_id": "pln_00000000009f",
                                   "added_at": "2026-01-01T00:00:00Z",
                                   "predecessor_plan_id": "pln_00000000000f"})
        self.programs._write(slug, record)
        with self.assertRaisesRegex(plan_program.ProgramError, "cannot be told"):
            self.programs.release(slug, "pln_00000000000f", "OB-1", "trying over a broken record")

    def test_release_still_refuses_while_an_unsealed_successor_could_answer(self):
        """The narrowing is exactly "sealed cannot answer" — a revisable successor still must."""
        slug = self._program("Ordinary door", "A draft successor answers in its own document.")
        self._plan("pln_00000000000f", "Child one", _obligation("OB-1", "Carried forward."))
        self.programs.add_child(slug, "pln_00000000000f")
        self._plan("pln_0000000000f2", "Child two", _obligation("OB-1", "Still carried."),
                   predecessor="pln_00000000000f")
        self.programs.add_child(slug, "pln_0000000000f2", predecessor="pln_00000000000f")
        with self.assertRaisesRegex(plan_program.ProgramError, "can be revised"):
            self.programs.release(slug, "pln_00000000000f", "OB-1", "trying the easy door")

    def test_close_is_not_a_second_door_into_completion(self):
        """`complete` has one door because it is the only closure with a gate of its own."""
        slug = self._program("One door", "Completion is not written through close.")
        self._plan("pln_00000000000d", "Child A")
        self.programs.add_child(slug, "pln_00000000000d")
        with self.assertRaises(plan_program.ProgramError) as caught:
            self.programs.close(slug, "complete", "sneaking past the gate")
        self.assertIn("not written through `close`", str(caught.exception))
        self.assertIsNone(self.programs.read(slug).get("closure"))

    def test_acknowledging_an_unknown_that_does_not_exist_is_refused(self):
        """Silently dropping it would leave the record holding less than the operator believes."""
        slug = self._program("Nothing unknown", "Its books compute fine.")
        self._plan("pln_00000000000e", "Child A")
        self.programs.add_child(slug, "pln_00000000000e")
        with self.assertRaisesRegex(plan_program.ProgramError, "nothing to "):
            self.programs.close(slug, "retired", "down", acknowledged_unknown="just in case")

    def test_reopen_requires_a_reason_for_every_state_and_keeps_what_it_undid(self):
        for index, (state, close) in enumerate((
                ("retired", lambda s: self.programs.close(s, "retired", "set down")),
                ("abandoned", lambda s: self.programs.close(s, "abandoned", "dropped")),
                ("complete", lambda s: self.programs.complete(s, "objective met")))):
            with self.subTest(state=state):
                slug = self._program(f"Reopenable {index}", "A closure that can be undone.")
                plan_id = f"pln_0000000000c{index}"
                self._plan(plan_id, f"Child {index}")
                self.programs.add_child(slug, plan_id)
                self._complete_plan(plan_id)
                close(slug)
                with self.assertRaisesRegex(plan_program.ProgramError, "costs a reason"):
                    self.programs.reopen(slug, "  ")
                record = self.programs.reopen(slug, "the evidence changed")
                self.assertIsNone(record["closure"])
                self.assertEqual(len(record["closure_history"]), 1)
                self.assertEqual(record["closure_history"][0]["closure"]["state"], state)
                self.assertEqual(record["closure_history"][0]["reason"], "the evidence changed")
                rendered = plan_program.render(self.programs, record)
                self.assertIn("Closures that were undone", rendered)
                self.assertIn("the evidence changed", rendered)

    # -- a closed program takes corrections, never new structure ------------------------------

    def test_a_closed_program_takes_a_release_but_no_new_structure(self):
        slug = self._orphaned_debt()
        self.programs.release(slug, "pln_00000000000a", "MECH-ONE", "void")
        self.programs.release(slug, "pln_00000000000a", "MECH-TWO", "void")
        self.programs.close(slug, "retired", "set down")
        # A correction is permitted on a closed record; structure is not.
        self._plan("pln_00000000000c", "A late arrival")
        with self.assertRaisesRegex(plan_program.ProgramError, "reopen it first"):
            self.programs.add_child(slug, "pln_00000000000c", predecessor="pln_00000000000a")
        with self.assertRaisesRegex(plan_program.ProgramError, "reopen it first"):
            self.programs.insert_child(slug, "pln_00000000000c", before="pln_00000000000b")


class TheObjectiveCanFollowTheEvidence(_Program):
    """An objective is written when the least is known. It must be correctable, with its history."""

    def _one_child(self, title="Amendable", objective="The first thing we thought."):
        slug = self._program(title, objective)
        self._plan("pln_000000000e01", "Child A")
        self.programs.add_child(slug, "pln_000000000e01")
        return slug

    def test_revising_twice_keeps_both_prior_texts_in_order_with_reasons(self):
        slug = self._one_child()
        self.programs.revise_objective(slug, "The second thing.", "the first was written too early")
        record = self.programs.revise_objective(slug, "The third thing.",
                                                "the evidence moved again")
        self.assertEqual([entry["objective"] for entry in record["objective_history"]],
                         ["The first thing we thought.", "The second thing."])
        self.assertEqual([entry["reason"] for entry in record["objective_history"]],
                         ["the first was written too early", "the evidence moved again"])
        self.assertEqual(record["objective"], "The third thing.")

    def test_the_history_is_rendered_where_an_operator_reads_it(self):
        slug = self._one_child()
        record = self.programs.revise_objective(slug, "The second thing.", "the order changed")
        rendered = plan_program.render(self.programs, record)
        self.assertIn("How the objective has been revised", rendered)
        self.assertIn("The first thing we thought.", rendered)
        self.assertIn("the order changed", rendered)

    def test_a_revision_costs_a_reason_and_an_objective_cannot_be_emptied(self):
        slug = self._one_child()
        with self.assertRaisesRegex(plan_program.ProgramError, "costs a reason"):
            self.programs.revise_objective(slug, "Something new.", "  ")
        with self.assertRaisesRegex(plan_program.ProgramError, "cannot be empty"):
            self.programs.revise_objective(slug, "   ", "a reason")

    def test_rewriting_the_same_text_mints_no_history_entry(self):
        slug = self._one_child()
        with self.assertRaisesRegex(plan_program.ProgramError, "already carries"):
            self.programs.revise_objective(slug, "The first thing we thought.", "no change")
        self.assertNotIn("objective_history", self.programs.read(slug))

    def test_a_closed_program_takes_corrections_but_still_refuses_new_structure(self):
        """A correction is not a reversal: fixing a sentence must not require reopening a decision."""
        slug = self._one_child()
        self.programs.close(slug, "retired", "set down")
        record = self.programs.revise_objective(slug, "What it was actually for.",
                                                "the original wording was never accurate")
        self.assertEqual(record["objective"], "What it was actually for.")
        self._plan("pln_000000000e02", "Child B")
        with self.assertRaisesRegex(plan_program.ProgramError, "reopen it first"):
            self.programs.add_child(slug, "pln_000000000e02", predecessor="pln_000000000e01")


class NoNamedWayThroughIsADeadEnd(unittest.TestCase):
    """Every verb a refusal points at must exist. Checked mechanically, not by reading the prose.

    A refusal that names its way through is only as good as the door it names. This sweeps the
    module's own source for the `program <verb>` and `clone --<flag>` forms its messages use and
    asserts each resolves against the real command-line parser — so adding a refusal that points at
    a verb nobody built, or renaming a verb out from under an existing refusal, turns this red.
    """

    #: Every module whose operator-facing refusals may name a verb. The sweep read only
    #: plan_program.py at first, and the gap was not theoretical: a cold reviewer found the bind
    #: refusal in build_coordinator.py pointing at `reopen`, which refuses every plan that can
    #: reach that message. A guard that covers one file while the promise covers the change is a
    #: guard that reports safety it has not checked.
    SOURCES = ("plan_program", "project_manager", "build_coordinator")

    def _sources(self) -> str:
        import importlib
        return "\n".join(Path(importlib.import_module(name).__file__).read_text(encoding="utf-8")
                          for name in self.SOURCES)

    def _named_program_verbs(self, source: str | None = None) -> set:
        import re
        # The literal form the refusal texts use, e.g. `program release`, `program add --after`.
        return set(re.findall(r"`program ([a-z-]+)", source if source is not None else self._sources()))

    def _named_clone_flags(self, source: str | None = None) -> set:
        import re
        return set(re.findall(r"`clone (--[a-z-]+)",
                              source if source is not None else self._sources()))

    def _named_plan_verbs(self, source: str | None = None) -> set:
        """Top-level Project Manager verbs, e.g. `project_manager.py reopen pln_...`."""
        import re
        return set(re.findall(r"`project_manager\.py ([a-z-]+)",
                              source if source is not None else self._sources()))

    def test_every_program_verb_a_refusal_names_is_a_real_verb(self):
        import project_manager
        parser = project_manager.build_parser()
        program_action = next(
            action for action in parser._subparsers._group_actions[0].choices["program"]._actions
            if hasattr(action, "choices") and action.choices)
        available = set(program_action.choices)
        named = self._named_program_verbs()
        self.assertTrue(named, "the sweep found no named verbs, so it is not checking anything")
        self.assertEqual(named - available, set(),
                         f"refusal text points at verbs that do not exist; available: {sorted(available)}")

    def test_every_clone_flag_a_refusal_names_is_a_real_flag(self):
        import project_manager
        parser = project_manager.build_parser()
        clone = parser._subparsers._group_actions[0].choices["clone"]
        available = {option for action in clone._actions for option in action.option_strings}
        named = self._named_clone_flags()
        self.assertTrue(named, "the sweep found no named clone flags")
        self.assertEqual(named - available, set())

    def test_every_top_level_verb_a_refusal_names_is_a_real_verb(self):
        """The refusals that name `project_manager.py <verb>` — the ones in build_coordinator.py."""
        import project_manager
        parser = project_manager.build_parser()
        available = set(parser._subparsers._group_actions[0].choices)
        named = self._named_plan_verbs()
        self.assertTrue(named, "the sweep found no named top-level verbs")
        self.assertEqual(named - available, set())

    def test_the_sweep_runs_its_real_machinery_against_a_seeded_miss(self):
        """THE function the guard rests on, exercised end to end rather than asserted about.

        The first version of this compared two hardcoded literals with a set difference. It named a
        behaviour it never touched: deleting the regex, the file list and the parser lookup left it
        green. This drives the ACTUAL extractor over a line of invented refusal text and requires it
        to surface the verb nobody built.
        """
        seeded = 'raise ProgramError("try `program unburden <program>` instead")'
        self.assertEqual(self._named_program_verbs(seeded), {"unburden"})

        import project_manager
        parser = project_manager.build_parser()
        program_action = next(
            action for action in parser._subparsers._group_actions[0].choices["program"]._actions
            if hasattr(action, "choices") and action.choices)
        self.assertEqual(self._named_program_verbs(seeded) - set(program_action.choices),
                         {"unburden"},
                         "the real extractor plus the real parser must together surface the miss")

    def test_the_sweep_reads_every_module_that_writes_a_refusal(self):
        """The scope of the guard is itself asserted, so narrowing it cannot pass unnoticed."""
        import build_coordinator
        self.assertIn("build_coordinator", self.SOURCES)
        self.assertIn("project_manager", self.SOURCES)
        source = self._sources()
        self.assertIn("does not start a Build", source,
                      "the bind refusal must be inside the swept text, not merely nearby")


class NoRefusalSendsYouAtADoorThatRefuses(_Program):
    """The defect CLASS, closed by exhaustion rather than by fixing the instances one at a time.

    Three separate refusals in this change named a way through that would itself refuse: the bind
    door named `reopen` for a sealed plan, and both edge-two refusals named `program supersede` for
    an ACTIVE plan, because an active plan carries a seal and `bool(seal)` cannot tell the two apart.
    Each was found by a different reviewer, one round after the last, which is the signal that
    patching instances was not going to finish the job.

    So this drives the real refusals against a displaced or downstream child in EVERY lifecycle
    status, and asserts of each message that whatever it names is something that would not turn
    round and refuse. It is a matrix, so a status nobody thought about is covered by construction.
    """

    # `complete` belongs here and was missing, which is the hole this class was written to make
    # impossible: the assertion below explicitly names complete as a dead end, and the matrix never
    # drove it. A guard that reads as enforcing and is slipped past on one path is exactly the shape
    # under review, committed inside the guard against it.
    STATUSES = ("draft", "sealed", "active", "complete", "retired", "abandoned")

    def _put_in_status(self, plan_id, status):
        slug = self.plans.resolve(plan_id)
        if status == "draft":
            return
        if status in ("sealed", "active", "complete"):
            digest = self.plans.read_record(slug)["current"]["plan_digest"]
            self.plans.update_record(slug, lambda r: r.update({"seal": {
                "revision": 1, "reviewed_digest": digest, "sealed_digest": digest,
                "build_plan_digest": digest, "at": "2026-08-29T03:00:00Z",
                "delta_judgment": "none"}}))
        if status == "active":
            self.plans.update_record(slug, lambda r: r.update({"build_binding": _BUILD_BINDING}))
        if status in DEAD_BRANCH_STATES or status == "complete":
            self.plans.update_record(slug, lambda r: r.update({"closure": {
                "state": status, "at": "2026-08-29T06:00:00Z",
                "reason": "merged" if status == "complete" else "stopped"}}))

    #: The precondition each status needs stated before a verb it would otherwise refuse can be
    #: named. A two-step route whose first step is spelled out is a way through; the same verb named
    #: bare is a dead end. Empty means the verb may not be named at all for that status.
    PRECONDITION = {"active": "ABANDON", "complete": None}

    def _assert_no_dead_end(self, message, plan_id, status):
        """The named verb must be one that would actually run for a plan in THIS status — or the
        message must state, in the message itself, what has to happen first to make it run."""
        # Detect the ADVICE shape — supersede named against THIS plan — not the word in passing.
        # "superseding a plan with a Build running would strand it" is explanation; "supersede
        # pln_x" is an instruction. Matching only the backticked form missed the second, which my
        # own seeded case below caught.
        if f"supersede {plan_id}" in message:
            # supersede refuses complete and active targets. For a complete plan there is no
            # precondition that helps: merged history is corrected by appended work, never replaced,
            # so naming supersede at all is a dead end. For an active plan there IS one — abandon
            # the Build — and naming the verb is honest only if the message names that step too.
            if status in self.PRECONDITION:
                precondition = self.PRECONDITION[status]
                self.assertIsNotNone(
                    precondition,
                    f"the refusal names supersede for a {status} plan, which supersede refuses "
                    f"with no precondition that would open it:\n{message}")
                self.assertIn(
                    precondition, message,
                    f"the refusal names supersede for a {status} plan without stating the step "
                    f"that has to happen first, so as written it is a dead end:\n{message}")
        if "Revise " in message and "`program supersede" not in message:
            # A sealed plan is terminal and cannot be revised.
            self.assertNotIn(status, ("sealed", "active"),
                             f"the refusal says to revise a {status} plan, which cannot be "
                             f"revised:\n{message}")
        if status == "active":
            self.assertIn("Build", message,
                          "an active plan's refusal must say the Build has to stop first")
            # The word ABANDON being present is not the same as the routes being sound: a message
            # can state the precondition and STILL route the merge outcome at supersede, which
            # refuses a complete target flat. A reviewer passed the old detector exactly that
            # message. The merge route must never continue into supersede.
            self.assertNotRegex(
                message, r"(?is)MERGE\b(?:(?!program add --after).)*\bsupersede",
                "the merge route ends at appended work; routing it onward into supersede sends "
                f"the operator at a door that refuses a complete plan:\n{message}")
        if status == "complete":
            self.assertIn("program add --after", message,
                          "merged history is corrected by appended work, and nothing else opens; "
                          f"the refusal must name that door:\n{message}")

    def test_inserts_edge_two_refusal_never_names_a_door_that_would_refuse(self):
        for index, status in enumerate(self.STATUSES):
            with self.subTest(status=status):
                slug = self._program(f"Matrix insert {index}", "Every status of a displaced child.")
                first = f"pln_00000000c{index}01"
                displaced = f"pln_00000000c{index}02"
                newcomer = f"pln_00000000c{index}03"
                self._plan(first, "First")
                self.programs.add_child(slug, first)
                self._plan(displaced, "Displaced", predecessor=first)
                self.programs.add_child(slug, displaced, predecessor=first)
                self._put_in_status(displaced, status)
                self._plan(newcomer, "Newcomer",
                           _obligation("OB-NEW", "The displaced child must answer for this."))
                try:
                    self.programs.insert_child(slug, newcomer, before=displaced)
                except plan_program.ProgramError as refusal:
                    self._assert_no_dead_end(str(refusal), displaced, status)
                else:
                    # A row that raises nothing must assert the ALTERNATIVE guarantee, or it is a
                    # vacuous pass — which two rows of this matrix silently were for a round: a
                    # dead displaced child skips edge two by design, and the debt is then owed at
                    # the inserted plan itself, the new live end of that branch.
                    self.assertIn(status, ("retired", "abandoned"),
                                  f"insert quietly accepted a displaced child in status {status}")
                    report = self.programs.obligation_report(self.programs.read(slug))
                    self.assertIn("OB-NEW", [o["id"] for o in report["obligations"]],
                                  "the newcomer's debt must survive at the live end")

    def test_supersedes_edge_two_refusal_never_names_a_door_that_would_refuse(self):
        for index, status in enumerate(self.STATUSES):
            with self.subTest(status=status):
                slug = self._program(f"Matrix supersede {index}", "Every status of a downstream child.")
                first = f"pln_00000000d{index}01"
                target = f"pln_00000000d{index}02"
                downstream = f"pln_00000000d{index}03"
                replacement = f"pln_00000000d{index}04"
                self._plan(first, "First")
                self.programs.add_child(slug, first)
                self._plan(target, "The one being replaced", predecessor=first)
                self.programs.add_child(slug, target, predecessor=first)
                self._plan(downstream, "Downstream", predecessor=target)
                self.programs.add_child(slug, downstream, predecessor=target)
                self._put_in_status(downstream, status)
                self._plan(replacement, "Replacement",
                           _obligation("OB-NEW", "Whoever follows must answer for this."))
                # The target is sealed then retired — the only shape a real supersession leaves,
                # now that an unsealed target is refused before edge two is ever examined.
                target_slug = self.plans.resolve(target)
                digest = self.plans.read_record(target_slug)["current"]["plan_digest"]
                self.plans.update_record(target_slug, lambda r: r.update({"seal": {
                    "revision": 1, "reviewed_digest": digest, "sealed_digest": digest,
                    "build_plan_digest": digest, "at": "2026-08-29T03:00:00Z",
                    "delta_judgment": "none"}}))
                self.plans.update_record(target_slug, lambda r: r.update({"closure": {
                    "state": "retired", "at": "2026-08-29T06:00:00Z", "reason": "replaced"}}))
                try:
                    self.programs.mark_superseded(slug, target, replacement)
                except plan_program.ProgramError as refusal:
                    self._assert_no_dead_end(str(refusal), downstream, status)

    def test_the_matrix_would_catch_the_defect_it_was_written_for(self):
        """THE assertion the matrix rests on, exercised against the exact message that was wrong."""
        # The round-two defect: supersede named for an ACTIVE plan with no precondition stated.
        was_wrong = ("pln_x does not answer for 1 obligation(s):\n  - OB-NEW: x\n"
                     "That plan is SEALED, and a seal is terminal, so it cannot be revised to "
                     "answer for them. Replace it: `program supersede pln_x` with a plan that does.")
        with self.assertRaises(AssertionError):
            self._assert_no_dead_end(was_wrong, "pln_x", "active")
        # The round-three defect: a precondition IS stated, but the route it describes ends at a
        # verb that refuses anyway — "let the Build merge, then supersede" leaves a complete plan.
        also_wrong = ("A Build is bound to pln_x. Let that Build merge, or abandon it, and then "
                      "supersede pln_x with a plan that answers.")
        with self.assertRaises(AssertionError):
            self._assert_no_dead_end(also_wrong, "pln_x", "active")
        # And supersede named for a COMPLETE plan is a dead end no precondition can open.
        self.assertIsNone(self.PRECONDITION["complete"])
        # A reviewer defeated the round-three guard with this: the magic word present, the defect
        # verbatim. The detector now reads the ROUTE — merge must never continue into supersede —
        # so the spelling alone no longer buys a pass.
        still_wrong = ("A Build is bound to pln_x right now. ABANDON that Build, or let it MERGE, "
                       "and then supersede pln_x with a plan that answers.")
        with self.assertRaises(AssertionError):
            self._assert_no_dead_end(still_wrong, "pln_x", "active")
        # While the actual shipped message — two routes, each ending at its own open door — passes.
        sound = plan_program.way_through_for("pln_x", "active", True)
        self._assert_no_dead_end(sound, "pln_x", "active")


class LaneRecord(_Program):
    """The decided lane split on the program record: advisory, exhaustively input-validated, with a
    discriminated history. Its ONLY refusal surface is input validity — liveness, ordering, and
    disagreement with what `propose` recommended are never refused. Every enumerated refusal fires
    with its own named message, and nothing else refuses.
    """

    A = "pln_a00000000001"
    B = "pln_b00000000002"
    C = "pln_c00000000003"

    def _shelf(self, *ids):
        """A program carrying the given children on one chain. Returns its slug."""
        slug = self._program("A program with lanes", "Children that may ride in parallel.")
        prev = None
        for index, plan_id in enumerate(ids, start=1):
            self._plan(plan_id, f"PR {index}", predecessor=prev)
            self.programs.add_child(slug, plan_id, predecessor=prev)
            prev = plan_id
        return slug

    # -- the enumerated input refusals, each asserting its own message --

    def test_set_refuses_without_a_reason(self):
        slug = self._shelf(self.A)
        with self.assertRaisesRegex(plan_program.ProgramError, "costs a reason"):
            self.programs.set_lanes(slug, [{"name": "L1", "children": [self.A]}], "   ")

    def test_set_refuses_an_empty_split(self):
        slug = self._shelf(self.A)
        with self.assertRaisesRegex(plan_program.ProgramError, "at least one lane"):
            self.programs.set_lanes(slug, [], "a reason")

    def test_set_refuses_an_empty_lane_name(self):
        slug = self._shelf(self.A)
        with self.assertRaisesRegex(plan_program.ProgramError, "lane name cannot be empty"):
            self.programs.set_lanes(slug, [{"name": "  ", "children": [self.A]}], "a reason")

    def test_set_refuses_a_duplicate_lane_name(self):
        slug = self._shelf(self.A, self.B)
        with self.assertRaisesRegex(plan_program.ProgramError, "used twice"):
            self.programs.set_lanes(slug, [{"name": "L", "children": [self.A]},
                                           {"name": "L", "children": [self.B]}], "a reason")

    def test_set_refuses_a_lane_with_no_members(self):
        slug = self._shelf(self.A)
        with self.assertRaisesRegex(plan_program.ProgramError, "has no members"):
            self.programs.set_lanes(slug, [{"name": "L", "children": []}], "a reason")

    def test_set_refuses_a_child_twice_in_one_lane(self):
        slug = self._shelf(self.A)
        with self.assertRaisesRegex(plan_program.ProgramError, "appears twice in lane"):
            self.programs.set_lanes(slug, [{"name": "L", "children": [self.A, self.A]}], "a reason")

    def test_set_refuses_a_child_in_two_lanes(self):
        slug = self._shelf(self.A, self.B)
        with self.assertRaisesRegex(plan_program.ProgramError, "in two lanes"):
            self.programs.set_lanes(slug, [{"name": "L1", "children": [self.A]},
                                           {"name": "L2", "children": [self.A]}], "a reason")

    def test_set_refuses_a_child_not_stored_in_the_program(self):
        slug = self._shelf(self.A)
        with self.assertRaisesRegex(plan_program.ProgramError, "not stored in this program"):
            self.programs.set_lanes(slug, [{"name": "L", "children": ["pln_f00000000009"]}],
                                    "a reason")

    def test_set_refuses_a_child_stored_but_missing_from_the_library(self):
        # Stored in the program record, but its plan is not in this library — a distinct case from an
        # unknown child, and it must name itself honestly rather than surface a bare resolver error.
        slug = self._shelf(self.A)
        record = self.programs.read(slug)
        record["children"].append({"plan_id": "pln_d00000000004",
                                   "added_at": "2026-01-01T00:00:00Z",
                                   "predecessor_plan_id": self.A})
        self.programs._write(slug, record)
        with self.assertRaisesRegex(plan_program.ProgramError, "missing from this library"):
            self.programs.set_lanes(slug, [{"name": "L", "children": ["pln_d00000000004"]}],
                                    "a reason")

    def test_an_identical_re_set_is_a_no_op_that_mints_no_history(self):
        slug = self._shelf(self.A, self.B)
        self.programs.set_lanes(slug, [{"name": "L", "children": [self.A, self.B]}], "first")
        with self.assertRaisesRegex(plan_program.ProgramError, "already carries"):
            self.programs.set_lanes(slug, [{"name": "L", "children": [self.A, self.B]}],
                                    "a different reason entirely")
        self.assertNotIn("lanes_history", self.programs.read(slug))

    # -- what it never refuses: liveness, ordering, disagreement --

    def test_a_split_lanes_a_superseded_or_dead_child_cleanly(self):
        slug = self._shelf(self.A, self.B)
        # B's plan is retired and marked superseded on the record — both are liveness facts, and
        # set_lanes reads neither: it records the operator's decision as given.
        self.plans.update_record(
            self.plans.resolve(self.B),
            lambda current: current.__setitem__(
                "closure", {"state": "retired", "at": "2026-01-01T00:00:00Z", "reason": "superseded"}))
        record = self.programs.read(slug)
        for child in record["children"]:
            if child["plan_id"] == self.B:
                child["superseded_by"] = self.A
        self.programs._write(slug, record)
        result = self.programs.set_lanes(
            slug, [{"name": "L1", "children": [self.A, self.B]}], "lane both, B retired")
        self.assertEqual(result["lanes"]["lanes"], [{"name": "L1", "children": [self.A, self.B]}])

    def test_the_operator_may_override_a_standing_split_freely(self):
        slug = self._shelf(self.A, self.B, self.C)
        self.programs.set_lanes(slug, [{"name": "one", "children": [self.A, self.B, self.C]}],
                                "all together")
        result = self.programs.set_lanes(
            slug, [{"name": "x", "children": [self.A]}, {"name": "y", "children": [self.B]},
                   {"name": "z", "children": [self.C]}], "split them apart")
        self.assertEqual(len(result["lanes"]["lanes"]), 3)
        self.assertEqual(result["lanes_history"][0]["split"]["lanes"],
                         [{"name": "one", "children": [self.A, self.B, self.C]}])

    # -- the discriminated history --

    def test_history_is_discriminated_across_set_reset_clear_set(self):
        slug = self._shelf(self.A, self.B, self.C)
        self.programs.set_lanes(slug, [{"name": "L1", "children": [self.A]}], "first split")
        self.programs.set_lanes(slug, [{"name": "L1", "children": [self.A, self.B]}], "widen it")
        self.programs.clear_lanes(slug, "pause concurrency")
        self.programs.set_lanes(slug, [{"name": "L1", "children": [self.C]}], "resume, differently")
        record = self.programs.read(slug)
        history = record["lanes_history"]
        # set -> set -> clear -> set: two endings, replaced then cleared, and a gap where none stood.
        self.assertEqual([entry["ended_by"] for entry in history], ["replaced", "cleared"])
        # Each ended split keeps its OWN reason; the entry's reason is why it ended.
        self.assertEqual(history[0]["split"]["reason"], "first split")
        self.assertEqual(history[0]["reason"], "widen it")
        self.assertEqual(history[1]["split"]["reason"], "widen it")
        self.assertEqual(history[1]["reason"], "pause concurrency")
        # The current split is the one set after the gap.
        self.assertEqual(record["lanes"]["reason"], "resume, differently")
        self.assertEqual(record["lanes"]["lanes"], [{"name": "L1", "children": [self.C]}])
        for entry in history:
            self.assertRegex(entry["ended_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_clear_refuses_without_a_reason_and_when_nothing_stands(self):
        slug = self._shelf(self.A)
        with self.assertRaisesRegex(plan_program.ProgramError, "no decided lane split to clear"):
            self.programs.clear_lanes(slug, "a reason")
        self.programs.set_lanes(slug, [{"name": "L", "children": [self.A]}], "stand up a split")
        with self.assertRaisesRegex(plan_program.ProgramError, "costs a reason"):
            self.programs.clear_lanes(slug, "   ")

    # -- concurrency: a second session's write is not lost --

    def test_a_concurrent_second_set_is_not_lost(self):
        import threading
        slug = self._shelf(self.A, self.B)
        start = threading.Barrier(2)
        errors: list = []

        def worker(name, child):
            start.wait()
            try:
                self.programs.set_lanes(slug, [{"name": name, "children": [child]}], f"reason {name}")
            except plan_program.ProgramError as exc:   # a lost-update would surface here or below
                errors.append(str(exc))

        threads = [threading.Thread(target=worker, args=("L1", self.A)),
                   threading.Thread(target=worker, args=("L2", self.B))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [], f"a concurrent set was refused: {errors}")
        record = self.programs.read(slug)
        # Both splits are accounted for: one stands, the other is in history as REPLACED. Without the
        # lock both writers would read the empty record and the second would clobber the first with no
        # history entry — so exactly one history entry, holding the other split, is the proof.
        standing = {lane["name"] for lane in record["lanes"]["lanes"]}
        historic = {lane["name"] for entry in record["lanes_history"]
                    for lane in entry["split"]["lanes"]}
        self.assertEqual(standing | historic, {"L1", "L2"})
        self.assertEqual(len(record["lanes_history"]), 1)
        self.assertEqual(record["lanes_history"][0]["ended_by"], "replaced")


class LaneProposal(_Program):
    """`propose_lanes`: a pure, deterministic read on the engine's own fail-closed conflict rule.

    Children that SHARE territory group into one lane; disjoint children ride separate lanes up to the
    ceiling, so a contended shelf honestly recommends a single lane. Every stored child lands in exactly
    one visible bucket. Nothing is written.
    """

    def _territory_child(self, slug, plan_id, title, paths, *, predecessor=None, resources=None):
        """A plan whose single work item declares the given territory, joined to the program.

        The territory-varying document helper the existing program fixtures lacked: it is what lets a
        test place two children over the same or disjoint files and watch the recommendation react.
        """
        document = _document(plan_id=plan_id, title=title)
        document["build_plan"]["work_items"] = [{
            "id": "w", "description": title, "paths": list(paths), "depends_on": [],
            "exclusive_resources": list(resources or []), "executor_class": "builder",
            "verification": ["it runs"],
            "output_contract": {"deliverable": "x", "artifact_kinds": ["code"],
                                "required_evidence": ["a test"]}}]
        program = {"program_id": self.program_id}
        if predecessor:
            program["predecessor_plan_id"] = predecessor
        document["program"] = program
        self.plans.create(document)
        self.programs.add_child(slug, plan_id, predecessor=predecessor)
        return plan_id

    def _chain(self, slug, *specs):
        """specs: (plan_id, paths). Chained in order, each succeeding the previous."""
        prev = None
        for plan_id, paths in specs:
            self._territory_child(slug, plan_id, plan_id[-4:], paths, predecessor=prev)
            prev = plan_id

    def test_propose_leaves_the_record_byte_identical(self):
        slug = self._program("Pure", "A pure read writes nothing.")
        self._chain(slug, ("pln_a00000000001", ["x.py"]), ("pln_b00000000002", ["y.py"]))
        path = self.programs._record_path(slug)
        before = path.read_bytes()
        self.programs.propose_lanes(slug)
        self.assertEqual(path.read_bytes(), before)

    def test_disjoint_children_ride_separate_lanes_and_conflicts_group(self):
        slug = self._program("Split", "Disjoint apart, shared together.")
        # A(x) -> B(y, disjoint) -> C(x, conflicts A)
        self._chain(slug, ("pln_a00000000001", ["x.py"]), ("pln_b00000000002", ["y.py"]),
                    ("pln_c00000000003", ["x.py"]))
        proposal = self.programs.propose_lanes(slug)
        lanes = {lane["name"]: lane["members"] for lane in proposal["lanes"]}
        self.assertEqual(lanes["lane-1"], ["pln_a00000000001", "pln_c00000000003"])
        self.assertEqual(lanes["lane-2"], ["pln_b00000000002"])
        self.assertFalse(proposal["contended"])
        # B succeeds A across lanes, and C succeeds B across lanes — both are merge-order risks.
        edges = {(e["child"], e["predecessor"]) for e in proposal["cross_lane_edges"]}
        self.assertIn(("pln_b00000000002", "pln_a00000000001"), edges)
        self.assertIn(("pln_c00000000003", "pln_b00000000002"), edges)

    def test_a_contended_shelf_recommends_a_single_lane_naming_the_territory(self):
        slug = self._program("Contended", "Everything over the same two files.")
        self._chain(slug, ("pln_a00000000001", ["p.py", "q.py"]),
                    ("pln_b00000000002", ["p.py", "q.py"]), ("pln_c00000000003", ["p.py", "q.py"]))
        proposal = self.programs.propose_lanes(slug)
        self.assertEqual(len(proposal["lanes"]), 1)
        self.assertEqual(len(proposal["lanes"][0]["members"]), 3)
        self.assertTrue(proposal["contended"])
        self.assertEqual(proposal["lanes"][0]["territory"], ["p.py", "q.py"])

    def test_a_glob_declared_path_conflicts_with_a_file_under_it_fails_closed(self):
        slug = self._program("Glob", "A glob reaches under itself.")
        self._chain(slug, ("pln_a00000000001", [".engine/tools/*"]),
                    ("pln_b00000000002", [".engine/tools/x.py"]))
        proposal = self.programs.propose_lanes(slug)
        # The glob and the file under it are NOT provably disjoint, so they share a lane (fails closed).
        self.assertEqual(len(proposal["lanes"]), 1)

    def test_a_shared_resource_token_with_disjoint_paths_is_a_caution_not_a_verdict(self):
        slug = self._program("Tokens", "A shared token does not separate lanes.")
        # Disjoint paths but the SAME exclusive_resources token on both.
        self._territory_child(slug, "pln_a00000000001", "A", ["x.py"], resources=["shared-token"])
        self._territory_child(slug, "pln_b00000000002", "B", ["y.py"],
                              predecessor="pln_a00000000001", resources=["shared-token"])
        proposal = self.programs.propose_lanes(slug)
        self.assertEqual(len(proposal["lanes"]), 2)   # disjoint paths => separate lanes despite the token
        tokens = {caution["token"] for caution in proposal["resource_cautions"]}
        self.assertIn("shared-token", tokens)

    def _seal(self, plan_id):
        digest = self.plans.read_record(self.plans.resolve(plan_id))["current"]["plan_digest"]
        self.plans.update_record(self.plans.resolve(plan_id), lambda cur: cur.__setitem__(
            "seal", {"revision": 1, "reviewed_digest": digest, "sealed_digest": digest,
                     "build_plan_digest": digest, "at": "2026-01-01T00:00:00Z",
                     "delta_judgment": "none"}))

    def _close(self, plan_id, state):
        self.plans.update_record(self.plans.resolve(plan_id), lambda cur: cur.__setitem__(
            "closure", {"state": state, "at": "2026-01-01T00:00:00Z", "reason": "fixture"}))

    _V1_PAYLOAD = {
        "schema_version": "build-plan.v1", "profile": "normal",
        "intent_source": {"kind": "direct"}, "raw_intent": "a legacy payload",
        "interpretation": "A v1 payload for a fixture; it cannot express exclusive_resources.",
        "evidence": [{"claim": "it stores", "basis": "this test", "kind": "observed"}],
        "assumptions": [],
        "objective": "A v1 payload that cannot express exclusive_resources.",
        "success_obligations": [{"outcome": "it exists", "verification": "this test"}],
        "scope_boundary": ["one node"], "non_goals": ["everything else"],
        "risks": ["none worth listing in a fixture"], "review_strategy": "this test",
        "work_items": [{"id": "w", "description": "d", "paths": ["c.py"], "verification": ["v"]}],
        "spec": {"posture": "none", "selection_basis": "fixture", "disclosure": "fixture"}}
    _IMPORTED_PAYLOAD = {"schema_version": "build-plan.imported", "work_items": []}

    def _shaped_child(self, slug, plan_id, predecessor, build_plan):
        """Create a child carrying `build_plan` verbatim, validated and digested at creation time.

        The shape must be set BEFORE create() so the stored revision hashes to its own digest —
        rewriting a head afterward trips the store's read-time digest check and reads as unreadable,
        not as the v1/imported shape under test. create() validates the payload against its versioned
        schema, so these are REAL v1 and imported payloads, not mutated v2 ones.
        """
        document = _document(plan_id=plan_id, title=plan_id[-4:])
        document["build_plan"] = json.loads(json.dumps(build_plan))   # a fresh copy
        if build_plan.get("schema_version") == "build-plan.imported":
            document["intake"] = {"provenance": "an imported native plan"}
            document["deliberation"]["unresolved_decisions"] = ["the decomposition is not authored yet"]
        program = {"program_id": self.program_id}
        if predecessor:
            program["predecessor_plan_id"] = predecessor
        document["program"] = program
        self.plans.create(document)
        self.programs.add_child(slug, plan_id, predecessor=predecessor)

    def test_every_stored_child_lands_in_exactly_one_bucket(self):
        slug = self._program("Buckets", "One of each kind of child.")
        # Nine children created through the real path (v1/imported shaped at creation so their digests
        # hold), chained; the closures, seal, corruption and program-record facts are applied after.
        self._territory_child(slug, "pln_000000000001", "1", ["a.py"])                     # v2-sealed
        self._territory_child(slug, "pln_000000000002", "2", ["b.py"],
                              predecessor="pln_000000000001")                              # v2-draft
        self._shaped_child(slug, "pln_000000000003", "pln_000000000002", self._V1_PAYLOAD)   # v1
        self._shaped_child(slug, "pln_000000000004", "pln_000000000003",
                           self._IMPORTED_PAYLOAD)                                            # imported
        self._territory_child(slug, "pln_000000000005", "5", ["e.py"],
                              predecessor="pln_000000000004")                              # invalid head
        self._territory_child(slug, "pln_000000000006", "6", ["f.py"],
                              predecessor="pln_000000000005")                              # complete
        self._territory_child(slug, "pln_000000000007", "7", ["g.py"],
                              predecessor="pln_000000000006")                              # retired
        self._territory_child(slug, "pln_000000000008", "8", ["h.py"],
                              predecessor="pln_000000000007")                              # abandoned
        self._territory_child(slug, "pln_000000000009", "9", ["i.py"],
                              predecessor="pln_000000000008")                              # superseded
        self._seal("pln_000000000001")
        self._corrupt_head("pln_000000000005")
        self._close("pln_000000000006", "complete")
        self._close("pln_000000000007", "retired")
        self._close("pln_000000000008", "abandoned")
        record = self.programs.read(slug)
        for child in record["children"]:
            if child["plan_id"] == "pln_000000000009":
                child["superseded_by"] = "pln_000000000001"
        record["children"].append({"plan_id": "pln_00000000000a",
                                   "added_at": "2026-01-01T00:00:00Z",
                                   "predecessor_plan_id": "pln_000000000009"})    # missing from library
        self.programs._write(slug, record)

        proposal = self.programs.propose_lanes(slug)
        placed = set(proposal["placed"])
        unplaceable = {u["plan_id"]: u["class"] for u in proposal["unplaceable"]}
        excluded = {e["plan_id"]: e["reason"] for e in proposal["excluded"]}
        self.assertEqual(placed, {"pln_000000000001", "pln_000000000002"})
        self.assertEqual(unplaceable["pln_000000000003"],
                         plan_program.ProgramLibrary.UNPLACEABLE_V1)
        self.assertEqual(unplaceable["pln_000000000004"],
                         plan_program.ProgramLibrary.UNPLACEABLE_IMPORTED)
        self.assertEqual(unplaceable["pln_000000000005"],
                         plan_program.ProgramLibrary.UNPLACEABLE_UNREADABLE)
        self.assertEqual(excluded["pln_000000000006"], "complete")
        self.assertEqual(excluded["pln_000000000007"], "retired")
        self.assertEqual(excluded["pln_000000000008"], "abandoned")
        self.assertEqual(excluded["pln_000000000009"], "superseded-marked")
        self.assertIn("missing from this library", excluded["pln_00000000000a"])
        # Every stored child appears exactly once across the three buckets — nothing dropped, nothing double.
        all_ids = placed | set(unplaceable) | set(excluded)
        self.assertEqual(len(all_ids), 10)
        stored = {c["plan_id"] for c in self.programs.read(slug)["children"]}
        self.assertEqual(all_ids, stored)

    def _head_path(self, plan_id):
        slug = self.plans.resolve(plan_id)
        record = self.plans.read_record(slug)
        return self.plans.root / slug / record["current"]["snapshot"]

    def _corrupt_head(self, plan_id):
        path = self._head_path(plan_id)
        path.write_text(json.dumps({"schema_version": "engine-plan.v1"}), encoding="utf-8")

    def test_amend_places_only_newcomers_and_preserves_recorded_membership(self):
        slug = self._program("Amend", "A recorded split, then a newcomer.")
        self._chain(slug, ("pln_a00000000001", ["x.py"]), ("pln_b00000000002", ["y.py"]))
        self.programs.set_lanes(slug, [{"name": "keep", "children": ["pln_a00000000001"]}],
                                "the recorded split")
        # A newcomer disjoint from the seed's territory.
        self._territory_child(slug, "pln_c00000000003", "C", ["z.py"],
                              predecessor="pln_b00000000002")
        proposal = self.programs.propose_lanes(slug)
        self.assertEqual(proposal["mode"], "amend")
        seed = next(lane for lane in proposal["lanes"] if lane["name"] == "keep")
        self.assertTrue(seed["seed"])
        self.assertEqual(seed["members"], ["pln_a00000000001"])   # recorded membership verbatim
        # The newcomers (B and C) are placed; A is not re-placed.
        placed_members = [m for lane in proposal["lanes"] for m in lane["members"]]
        self.assertIn("pln_b00000000002", placed_members)
        self.assertIn("pln_c00000000003", placed_members)

    def test_fresh_sets_aside_the_recorded_split(self):
        slug = self._program("Fresh", "Recompute from scratch.")
        self._chain(slug, ("pln_a00000000001", ["x.py"]), ("pln_b00000000002", ["y.py"]))
        self.programs.set_lanes(slug, [{"name": "keep", "children": ["pln_a00000000001"]}],
                                "the recorded split")
        proposal = self.programs.propose_lanes(slug, fresh=True)
        self.assertEqual(proposal["mode"], "fresh")
        self.assertTrue(proposal["recorded_split_present"])
        # No seed lanes in fresh mode — every lane is freshly computed.
        self.assertFalse(any(lane["seed"] for lane in proposal["lanes"]))

    def test_max_lanes_caps_new_lanes_and_the_declared_paths_caveat_is_present(self):
        slug = self._program("Cap", "More disjoint children than lanes.")
        self._chain(slug, ("pln_a00000000001", ["a.py"]), ("pln_b00000000002", ["b.py"]),
                    ("pln_c00000000003", ["c.py"]))
        proposal = self.programs.propose_lanes(slug, max_lanes=2)
        self.assertLessEqual(len(proposal["lanes"]), 2)
        # The third disjoint child, at the cap, joins its nearest-predecessor lane rather than a new one.
        placed_members = [m for lane in proposal["lanes"] for m in lane["members"]]
        self.assertEqual(sorted(placed_members),
                         ["pln_a00000000001", "pln_b00000000002", "pln_c00000000003"])
        self.assertIn("declared work-item paths only", proposal["declared_paths_caveat"])

    def test_propose_does_not_reimplement_the_conflict_rule(self):
        # Obligation 1's grep-proof: the conflict rule is IMPORTED from build_coordinator_dag and used
        # as-is; plan_program must not define its own _pair_conflict or paths_conflict.
        source = Path(plan_program.__file__).read_text(encoding="utf-8")
        self.assertNotIn("def paths_conflict", source)
        self.assertNotIn("def _pair_conflict", source)
        self.assertIn("dag.paths_conflict", source)

    def test_proposal_is_byte_identical_across_hash_seeds(self):
        import os
        import subprocess
        slug = self._program("Determinism", "Same output whatever the hash seed.")
        self._chain(slug, ("pln_a00000000001", ["x.py"]), ("pln_b00000000002", ["y.py"]),
                    ("pln_c00000000003", ["x.py"]), ("pln_d00000000004", ["z.py", "w.py"]))
        tools_dir = str(Path(plan_program.__file__).resolve().parent)
        root = str(self.plans.root)
        code = (
            "import sys, json\n"
            f"sys.path.insert(0, {tools_dir!r})\n"
            "import plan_store, plan_program\n"
            f"progs = plan_program.ProgramLibrary(plan_store.PlanLibrary({root!r}))\n"
            f"proposal = progs.propose_lanes({slug!r})\n"
            "sys.stdout.buffer.write(json.dumps(proposal, sort_keys=True).encode('utf-8'))\n")

        def run(seed):
            proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                                  env={**os.environ, "PYTHONHASHSEED": seed}, check=True)
            return proc.stdout

        self.assertEqual(run("1"), run("2"))


class LaneRender(_Program):
    """`program show` (via plan_program.render) tells the truth about the DECIDED split, forever after
    the chain moves — a Lanes section only when a split stands, dead members marked in place, newcomers
    listed as unlaned, cross-lane edges disclosed, and the ended splits kept in a discriminated log."""

    def _shelf(self, *ids):
        slug = self._program("Render", "Ride in parallel.")
        prev = None
        for plan_id, paths in ids:
            document = _document(plan_id=plan_id, title=plan_id[-3:])
            document["build_plan"]["work_items"] = [{
                "id": "w", "description": "d", "paths": paths, "depends_on": [],
                "exclusive_resources": [], "executor_class": "builder", "verification": ["v"],
                "output_contract": {"deliverable": "x", "artifact_kinds": ["code"],
                                    "required_evidence": ["t"]}}]
            program = {"program_id": self.program_id}
            if prev:
                program["predecessor_plan_id"] = prev
            document["program"] = program
            self.plans.create(document)
            self.programs.add_child(slug, plan_id, predecessor=prev)
            prev = plan_id
        return slug

    def test_no_lanes_section_without_a_recorded_split(self):
        slug = self._shelf(("pln_a00000000001", ["x.py"]))
        self.assertNotIn("## Lanes", plan_program.render(self.programs, self.programs.read(slug)))

    def test_a_recorded_split_renders_with_its_reason(self):
        slug = self._shelf(("pln_a00000000001", ["x.py"]), ("pln_b00000000002", ["y.py"]))
        self.programs.set_lanes(slug, [{"name": "fast", "children": ["pln_a00000000001"]},
                                       {"name": "slow", "children": ["pln_b00000000002"]}],
                                "split by territory")
        rendered = plan_program.render(self.programs, self.programs.read(slug))
        self.assertIn("## Lanes", rendered)
        self.assertIn("split by territory", rendered)
        self.assertIn("**fast**", rendered)
        # A cross-lane predecessor edge is disclosed.
        self.assertIn("succeeds", rendered)

    def test_a_superseded_laned_member_is_marked_not_hidden(self):
        slug = self._shelf(("pln_a00000000001", ["x.py"]), ("pln_b00000000002", ["y.py"]))
        self.programs.set_lanes(slug, [{"name": "fast", "children": ["pln_a00000000001"]}],
                                "one lane for now")
        self.plans.update_record(self.plans.resolve("pln_a00000000001"), lambda cur: cur.__setitem__(
            "closure", {"state": "retired", "at": "2026-01-01T00:00:00Z", "reason": "superseded"}))
        record = self.programs.read(slug)
        for child in record["children"]:
            if child["plan_id"] == "pln_a00000000001":
                child["superseded_by"] = "pln_b00000000002"
        self.programs._write(slug, record)
        rendered = plan_program.render(self.programs, self.programs.read(slug))
        self.assertIn("pln_a00000000001", rendered)          # marked in place, never dropped
        self.assertIn("superseded by `pln_b00000000002`", rendered)

    def test_a_post_split_child_lists_as_unlaned(self):
        slug = self._shelf(("pln_a00000000001", ["x.py"]))
        self.programs.set_lanes(slug, [{"name": "only", "children": ["pln_a00000000001"]}],
                                "the initial split")
        # A child added after the split is not in any lane.
        document = _document(plan_id="pln_b00000000002", title="B")
        document["program"] = {"program_id": self.program_id,
                               "predecessor_plan_id": "pln_a00000000001"}
        self.plans.create(document)
        self.programs.add_child(slug, "pln_b00000000002", predecessor="pln_a00000000001")
        rendered = plan_program.render(self.programs, self.programs.read(slug))
        self.assertIn("not in any lane", rendered)
        self.assertIn("pln_b00000000002", rendered.split("not in any lane")[1])

    def test_the_lane_history_renders_discriminated(self):
        slug = self._shelf(("pln_a00000000001", ["x.py"]), ("pln_b00000000002", ["y.py"]))
        self.programs.set_lanes(slug, [{"name": "one", "children": ["pln_a00000000001"]}], "first")
        self.programs.set_lanes(slug, [{"name": "one", "children": ["pln_a00000000001",
                                                                     "pln_b00000000002"]}], "widen")
        self.programs.clear_lanes(slug, "pause")
        rendered = plan_program.render(self.programs, self.programs.read(slug))
        self.assertIn("## Lane splits that stopped standing", rendered)
        self.assertIn("**replaced**", rendered)
        self.assertIn("**cleared**", rendered)
        self.assertNotIn("## Lanes\n", rendered)   # nothing stands now, so no current-split section


class TheLaneRecordHasOneReader(unittest.TestCase):
    """The mechanical answer to advisory-drift: nothing outside the lanes surface reads the lane record.

    An AST allowlist in PR 1's seam-pin style — a scan over every module under .engine/tools/ for any
    read of the record keys `lanes`/`lanes_history`, permitted only in the enumerated surface. A future
    coordinator, skill or tool that reads the lane record to select or start work trips this even though
    it adds no refusal and touches no lanes code, so authority drift has to edit the allowlist in the open.
    """

    KEYS = {"lanes", "lanes_history"}
    ALLOWLIST = {"plan_program.py", "project_manager.py", "test_plan_program.py",
                 "test_project_manager.py", "demo_program_lanes.py"}

    def _record_key_reads(self, source: str) -> set:
        """Every read of a lane record key this source makes: `x["lanes"]` or `x.get("lanes")`."""
        import ast
        found = set()
        for node in ast.walk(ast.parse(source)):
            key = None
            if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) \
                    and isinstance(node.slice.value, str):
                key = node.slice.value
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "get" and node.args \
                    and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, str):
                key = node.args[0].value
            if key in self.KEYS:
                found.add(key)
        return found

    def test_only_the_allowlisted_surface_reads_the_lane_record(self):
        tools = Path(plan_program.__file__).resolve().parent
        offenders = {}
        for path in sorted(tools.glob("*.py")):
            if path.name in self.ALLOWLIST:
                continue
            reads = self._record_key_reads(path.read_text(encoding="utf-8"))
            if reads:
                offenders[path.name] = sorted(reads)
        self.assertEqual(offenders, {},
                         "the lane record is advisory and has exactly one reader surface; these "
                         f"modules read its keys and are not on the allowlist: {offenders}")

    def test_the_tripwire_catches_a_seeded_out_of_allowlist_reader(self):
        # THE function the guard rests on, proven to go red — a synthetic drifting reader outside the
        # allowlist must turn the real assertion red, not merely be visible to a detector beside it.
        seeded = "def drift(record):\n    return record['lanes'], record.get('lanes_history')\n"
        self.assertEqual(self._record_key_reads(seeded), {"lanes", "lanes_history"})
        offenders = {"a_drifting_module.py": sorted(self._record_key_reads(seeded))}
        with self.assertRaises(AssertionError):
            self.assertEqual(offenders, {})


class LaneSchema(_Program):
    """The schema pins the lane block's shape; the code pins what the schema cannot express."""

    def _record_with_lanes(self, lanes_block):
        slug = self._program("Schema", "Pin the lane block.")
        record = self.programs.read(slug)
        record["lanes"] = lanes_block
        return slug, record

    def _assert_refused(self, lanes_block):
        slug, record = self._record_with_lanes(lanes_block)
        with self.assertRaises(plan_store.PlanStoreError):
            self.programs._write(slug, record)

    def test_a_split_needs_at_least_one_lane(self):
        self._assert_refused({"decided_at": "2026-01-01T00:00:00Z", "reason": "why", "lanes": []})

    def test_a_lane_needs_at_least_one_child(self):
        self._assert_refused({"decided_at": "2026-01-01T00:00:00Z", "reason": "why",
                              "lanes": [{"name": "L", "children": []}]})

    def test_a_lane_name_cannot_be_empty(self):
        self._assert_refused({"decided_at": "2026-01-01T00:00:00Z", "reason": "why",
                              "lanes": [{"name": "", "children": ["pln_a00000000001"]}]})

    def test_the_split_reason_is_required(self):
        self._assert_refused({"decided_at": "2026-01-01T00:00:00Z",
                              "lanes": [{"name": "L", "children": ["pln_a00000000001"]}]})

    def test_a_valid_split_writes(self):
        slug, record = self._record_with_lanes(
            {"decided_at": "2026-01-01T00:00:00Z", "reason": "why",
             "lanes": [{"name": "L", "children": ["pln_a00000000001"]}]})
        self.programs._write(slug, record)   # does not raise
        self.assertEqual(self.programs.read(slug)["lanes"]["lanes"][0]["name"], "L")


if __name__ == "__main__":
    unittest.main()
