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
from unittest import mock

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


if __name__ == "__main__":
    unittest.main()


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
