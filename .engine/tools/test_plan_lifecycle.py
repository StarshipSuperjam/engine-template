#!/usr/bin/env python3
"""The plan ceremony's operability defects (issue 1062), each reproduced then answered.

Every case below starts from the shape the incident actually had. A test that only asserts the new
behaviour proves the code does what it now does; a test that first reproduces the wedge proves the
thing that hurt is gone. Ten defects, one class each, named for the defect rather than the fix.

Then two more groups: the FREEZE MOMENTS that make correction safe, and the CONSENT GATES bought by
the silent thirty-two-minute ceremony of 2026-08-25.
"""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

import project_manager
import plan_lifecycle
import plan_store
from test_plan_store import _document


class _Ceremony(unittest.TestCase):
    """A real library driven through the real CLI. Nothing here writes a record by hand."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "plans"
        self.lib = plan_store.PlanLibrary(self.root)
        self.addCleanup(self._tmp.cleanup)

    def run_command(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = project_manager.main(["--library", str(self.root), *argv])
        return code, out.getvalue(), err.getvalue()

    def plan(self, **over):
        return self.lib.create(_document(**over))

    def packet_digest(self, slug):
        """The digest `review record` will verify against — re-rendered exactly as the verb does."""
        import plan_projection
        return project_manager.core.digest(
            plan_projection.render_plan(self.lib.head(slug), self.lib.read_record(slug)).encode("utf-8"))

    def recorded_packet_digest(self, slug):
        """The digest the RECORDED review names. An amendment must match this, not a fresh render:
        the packet projection includes the record, so re-cutting it after a review exists yields a
        different digest for the same plan — which is precisely the confusion the refusal names."""
        return self.lib.read_record(slug)["plan_review"]["packet_digest"]

    def findings_file(self, *findings):
        path = self.tmp / "findings.json"
        path.write_text(json.dumps(list(findings)), encoding="utf-8")
        return str(path)

    def approved(self, depth="standard", **over):
        slug = self.plan(**over)
        self.run_command("preview", slug)
        self.assertEqual(self.run_command("approve", slug, "--depth", depth,
                                          "--operator-decision", "Approve at " + depth)[0], 0)
        return slug

    def covering(self, depth="standard"):
        return project_manager.required_lenses(depth, project_manager.installed_lenses())

    def reviewed(self, *findings, depth="standard", lenses=None):
        slug = self.approved(depth)
        # The panel delivered the effort its depth promises, and now says so: `review record` refuses
        # a record that does not (StarshipSuperjam/engine-template#1067).
        argv = ["review", "record", slug, "--packet-digest", self.packet_digest(slug),
                "--delivered-effort", "high"]
        for lens in (lenses if lenses is not None else self.covering(depth)):
            argv += ["--lens", lens]
        if findings:
            argv += ["--findings", self.findings_file(*findings)]
        self.assertEqual(self.run_command(*argv)[0], 0)
        return slug

    def finding(self, id_="ARCH-1", severity="serious", lens=None):
        return {"id": id_, "lens": lens or self.covering()[0], "severity": severity,
                "summary": "The store's first write precedes its fence."}


class D1PartialReviewIsNoLongerPermanent(_Ceremony):
    """The incident: a partial panel was recorded, and the one review slot was spent."""

    def test_the_wedge_reproduces_a_second_review_is_still_refused(self):
        slug = self.reviewed(lenses=[self.covering()[0]])
        code, _, err = self.run_command("review", "record", slug, "--packet-digest",
                                        self.packet_digest(slug), "--lens", self.covering()[1],
                                        "--delivered-effort", "high")
        self.assertEqual(code, 2)
        self.assertIn("exactly one per plan", err)

    def test_but_the_record_can_now_be_completed_by_amendment(self):
        slug = self.reviewed(lenses=[self.covering()[0]])
        code, out, _ = self.run_command(
            "review", "amend", slug, "--packet-digest", self.recorded_packet_digest(slug),
            "--lens", self.covering()[1], "--delivered-effort", "high",
            "--reason", "The second lens finished after the record.")
        self.assertEqual(code, 0)
        self.assertIn("+1 lens", out)
        self.assertEqual(self.lib.read_record(slug)["plan_review"]["lenses"],
                         [self.covering()[0], self.covering()[1]])
        self.assertEqual(self.lib.read_record(slug)["amendments"][0]["artifact"], "plan_review")

    def test_an_amendment_that_read_a_different_packet_is_refused(self):
        slug = self.reviewed(lenses=[self.covering()[0]])
        code, _, err = self.run_command(
            "review", "amend", slug, "--packet-digest", "sha256:" + "0" * 64,
            "--lens", self.covering()[1], "--reason", "Wrong packet.")
        self.assertEqual(code, 2)
        self.assertIn("did not review the same plan", err)

    def test_an_amendment_never_rewrites_a_finding_already_recorded(self):
        slug = self.reviewed(self.finding(), lenses=list(self.covering()))
        code, _, err = self.run_command(
            "review", "amend", slug, "--packet-digest", self.recorded_packet_digest(slug),
            "--findings", self.findings_file(self.finding()),
            "--reason", "Same id again.")
        self.assertEqual(code, 2)
        self.assertIn("already in the review", err)


class D2UnderCoverageWarnsInExactTerms(_Ceremony):
    def test_the_warning_names_the_missing_lenses_and_the_command_that_lands_them(self):
        slug = self.approved("standard")
        code, _, err = self.run_command("review", "record", slug, "--packet-digest",
                                        self.packet_digest(slug), "--lens", self.covering()[0],
                                        "--delivered-effort", "high")
        self.assertEqual(code, 0, "an under-covering record is a warning, never a refusal")
        self.assertIn("Missing: " + ", ".join(self.covering()[1:]), err)
        self.assertIn("review amend", err)
        self.assertIn("NOT spent", err)

    def test_the_seal_remains_the_single_hard_coverage_gate(self):
        slug = self.reviewed(lenses=[self.covering()[0]])
        refusals = project_manager.seal_refusals(self.lib, slug)
        self.assertTrue(any("missing" in r for r in refusals), refusals)


class D3TheTwoFindingShapesTranslate(_Ceremony):
    def test_the_reviewer_shape_is_mapped_rather_than_refused(self):
        slug = self.approved("standard")
        persona = {"severity": "blocking", "message": "The fence lands after the write.",
                   "location": "C01, the durable store"}
        code, _, _ = self.run_command(
            "review", "record", slug, "--packet-digest", self.packet_digest(slug),
            "--lens", "architecture", "--findings", self.findings_file(persona),
            "--delivered-effort", "high")
        self.assertEqual(code, 0)
        recorded = self.lib.read_record(slug)["plan_review"]["findings"][0]
        self.assertEqual(recorded["summary"], persona["message"])
        self.assertEqual(recorded["location"], persona["location"])
        self.assertEqual(recorded["lens"], "architecture")
        self.assertEqual(recorded["id"], "A-1")

    def test_a_mixed_batch_is_refused_naming_both_contracts_and_the_mapping(self):
        with self.assertRaises(plan_lifecycle.PlanLifecycleError) as caught:
            plan_lifecycle.translate_findings(
                [{"severity": "nit", "message": "m", "location": "l"},
                 {"id": "X", "lens": "architecture", "severity": "nit", "summary": "s"}],
                lenses=["architecture"])
        message = str(caught.exception)
        self.assertIn("plan-review-finding.v1", message)
        self.assertIn("id, lens, severity, summary", message)
        self.assertIn("message becomes the summary", message)

    def test_reviewer_shaped_findings_refuse_when_the_lens_is_ambiguous(self):
        with self.assertRaises(plan_lifecycle.PlanLifecycleError) as caught:
            plan_lifecycle.translate_findings(
                [{"severity": "nit", "message": "m", "location": "l"}],
                lenses=["architecture", "feasibility"])
        self.assertIn("carry no lens of their own", str(caught.exception))


class D4FreezeMoments(_Ceremony):
    """Recording mistakes stop being seal-grade — and corrections stop at a named moment."""

    def test_a_finding_amends_before_its_disposition_and_refuses_after(self):
        slug = self.reviewed(self.finding(severity="nit"))
        self.assertEqual(self.run_command(
            "finding", "amend", slug, "--id", "ARCH-1", "--severity", "blocking",
            "--reason", "Entered at the wrong severity.")[0], 0)
        self.assertEqual(self.lib.read_record(slug)["plan_review"]["findings"][0]["severity"], "blocking")
        self.assertEqual(self.run_command(
            "finding", "dispose", slug, "--id", "ARCH-1", "--disposition", "accepted-fixed",
            "--rationale", "Fixed in revision 2.", "--blocks-this-pr")[0], 0)
        code, _, err = self.run_command(
            "finding", "amend", slug, "--id", "ARCH-1", "--severity", "nit",
            "--reason", "Changed my mind.")
        self.assertEqual(code, 2)
        self.assertIn("silently re-aim", err)

    def test_the_review_freezes_at_its_first_finding_s_disposition(self):
        slug = self.reviewed(self.finding(), lenses=[self.covering()[0]])
        self.assertEqual(self.run_command(
            "finding", "dispose", slug, "--id", "ARCH-1", "--disposition", "rejected",
            "--rationale", "Not a real problem.")[0], 0)
        code, _, err = self.run_command(
            "review", "amend", slug, "--packet-digest", self.recorded_packet_digest(slug),
            "--lens", self.covering()[1], "--reason", "Late lens.")
        self.assertEqual(code, 2)
        self.assertIn("being adjudicated", err)

    def test_the_approval_freezes_at_the_first_review_which_pins_the_depth(self):
        slug = self.reviewed()
        code, _, err = self.run_command("approve", slug, "--depth", "quick",
                                        "--operator-decision", "Actually, quick.")
        self.assertEqual(code, 2)
        self.assertIn("pins the approved depth", err)
        self.assertEqual(self.lib.read_record(slug)["approval"]["depth"], "standard")

    def test_nothing_is_correctable_once_the_plan_is_sealed(self):
        slug = self.reviewed(self.finding())
        self.run_command("finding", "dispose", slug, "--id", "ARCH-1",
                         "--disposition", "rejected", "--rationale", "No.")
        self.run_command("present-findings", slug, "--operator-decision", "I read it.")
        self.assertEqual(self.run_command("seal", slug, "--operator-decision", "Seal it.")[0], 0)
        for argv in (("review", "amend", slug, "--packet-digest", self.recorded_packet_digest(slug),
                      "--lens", "feasibility", "--reason", "After the seal."),
                     ("finding", "amend", slug, "--id", "ARCH-1", "--severity", "nit",
                      "--reason", "After the seal.")):
            code, _, err = self.run_command(*argv)
            self.assertEqual(code, 2, argv)
            self.assertIn("a seal is terminal", err, argv)


class D5FirstRevisionNeedsNoRevisedAt(unittest.TestCase):
    def test_a_first_revision_omitting_revised_at_defaults_to_created_at(self):
        import plan_contract
        document = _document()
        document.pop("revised_at")
        plan_contract.validate_document(document)
        self.assertEqual(document["revised_at"], document["created_at"])

    def test_a_later_revision_still_has_to_say_when_it_was_revised(self):
        import plan_contract
        document = _document(revision=2)
        document.pop("revised_at")
        with self.assertRaises(plan_contract.PlanContractError):
            plan_contract.validate_document(document)


class D6NextStepsNameTheirCommand(_Ceremony):
    def test_every_stage_prints_a_runnable_command_rather_than_a_verb_to_look_up(self):
        slug = self.plan()
        stages = []
        stages.append(self.run_command("resume", slug)[1])
        self.run_command("preview", slug)
        self.run_command("approve", slug, "--depth", "standard", "--operator-decision", "Yes.")
        stages.append(self.run_command("resume", slug)[1])
        # The panel delivered the effort its depth promises, and now says so: `review record` refuses
        # a record that does not (StarshipSuperjam/engine-template#1067).
        argv = ["review", "record", slug, "--packet-digest", self.packet_digest(slug),
                "--delivered-effort", "high"]
        for lens in self.covering():
            argv += ["--lens", lens]
        argv += ["--findings", self.findings_file(self.finding())]
        self.run_command(*argv)
        stages.append(self.run_command("resume", slug)[1])
        self.run_command("finding", "dispose", slug, "--id", "ARCH-1",
                         "--disposition", "rejected", "--rationale", "No.")
        stages.append(self.run_command("resume", slug)[1])
        for text in stages:
            self.assertIn("project_manager.py ", text, text)


class D7CarryForwardDecayIsRechecked(_Ceremony):
    """The B2 shape: a predecessor mints obligations AFTER its successor has joined."""

    def _program_with_two_children(self):
        import plan_program
        programs = plan_program.ProgramLibrary(self.lib)
        program = programs.create("A program", "Two plans, one after the other")
        first = self.plan(plan_id="pln_aaaaaaaaaaaa", title="First")
        second = self.plan(plan_id="pln_bbbbbbbbbbbb", title="Second")
        programs.add_child(program, "pln_aaaaaaaaaaaa")
        programs.add_child(program, "pln_bbbbbbbbbbbb", predecessor="pln_aaaaaaaaaaaa")
        return programs, program, first, second

    def _mint_obligation_on(self, slug, document):
        document = dict(document)
        document["revision"] = 2
        document["revised_at"] = "2026-08-25T00:00:00Z"
        document["revision_note"] = "Carry an obligation the successor has never seen."
        document["program"] = {"program_id": "prg_0123456789ab", "carried_obligations": [
            {"id": "OB-LATE", "statement": "The late obligation.", "state": "carried"}]}
        self.lib.append_revision(slug, document, expected_revision=1)

    def test_an_obligation_minted_after_the_join_is_surfaced_not_silent(self):
        programs, program, first, _ = self._program_with_two_children()
        self.assertEqual(programs.carry_forward_decay(program), [])
        self._mint_obligation_on(first, self.lib.head(first))
        decay = programs.carry_forward_decay(program)
        self.assertEqual([e["plan_id"] for e in decay], ["pln_bbbbbbbbbbbb"])
        self.assertEqual([o["id"] for o in decay[0]["obligations"]], ["OB-LATE"])

    def test_the_successor_cannot_seal_while_it_does_not_answer_for_the_obligation(self):
        _, _, first, second = self._program_with_two_children()
        self._mint_obligation_on(first, self.lib.head(first))
        refusals = project_manager.seal_refusals(self.lib, second)
        self.assertTrue(any("OB-LATE" in r for r in refusals), refusals)


class D8ApproveRefusesToOrphanAReview(_Ceremony):
    def test_re_approving_at_a_new_revision_is_refused_and_names_both_ways_out(self):
        slug = self.reviewed()
        document = dict(self.lib.head(slug))
        document["revision"] = 2
        document["revised_at"] = "2026-08-25T00:00:00Z"
        document["revision_note"] = "The review's fix, folded in."
        self.lib.append_revision(slug, document, expected_revision=1)
        self.run_command("preview", slug)
        code, _, err = self.run_command("approve", slug, "--depth", "standard",
                                        "--operator-decision", "Approve the new revision.")
        self.assertEqual(code, 2)
        self.assertIn("would orphan it", err)
        self.assertIn("--delta-judgment scoped", err)
        self.assertIn("clone", err)


class D9ShowAndSealDeriveFromOneRefusalSet(_Ceremony):
    def test_show_prints_exactly_what_seal_refuses(self):
        slug = self.reviewed(lenses=[self.covering()[0]])
        refusals = project_manager.seal_refusals(self.lib, slug)
        shown = self.run_command("show", slug)[1]
        self.assertIn("not sealable yet", shown)
        for refusal in refusals:
            self.assertIn(refusal.splitlines()[0], shown)

    def test_a_plan_with_nothing_in_the_way_reads_as_ready(self):
        slug = self.reviewed()
        self.run_command("present-findings", slug, "--operator-decision", "Nothing was found.")
        self.assertEqual(project_manager.seal_refusals(self.lib, slug), [])
        self.assertIn("ready to seal", self.run_command("show", slug)[1])


class D10TheOrphanedApprovalWedgeHasAnInCliRepair(_Ceremony):
    """The wedge: a partial record plus a plan that moved, escapable only by store surgery."""

    def test_the_whole_wedge_is_now_walked_out_of_with_shipped_verbs(self):
        # 1. A panel is recorded partially — one lens of the depth's roster.
        slug = self.reviewed(self.finding(), lenses=[self.covering()[0]])
        self.assertTrue(any("missing" in r for r in project_manager.seal_refusals(self.lib, slug)))
        # 2. The missing lenses are run and folded in by amendment, which used to be impossible.
        argv = ["review", "amend", slug, "--packet-digest", self.recorded_packet_digest(slug)]
        for lens in self.covering()[1:]:
            argv += ["--lens", lens]
        argv += ["--findings", self.findings_file(self.finding(id_="FEAS-1", lens=self.covering()[1])),
                 "--delivered-effort", "high",
                 "--reason", "The remaining lenses returned late."]
        self.assertEqual(self.run_command(*argv)[0], 0)
        # 3. Both findings are dispositioned, presented, and the plan seals — no store surgery.
        for finding_id in ("ARCH-1", "FEAS-1"):
            self.assertEqual(self.run_command(
                "finding", "dispose", slug, "--id", finding_id, "--disposition", "rejected",
                "--rationale", "Answered in the deliberation.")[0], 0)
        self.assertEqual(self.run_command(
            "present-findings", slug, "--operator-decision", "Read both.")[0], 0)
        self.assertEqual(self.run_command("seal", slug, "--operator-decision", "Seal it.")[0], 0)
        self.assertIsNotNone(self.lib.read_record(slug)["seal"])


class ConsentGates(_Ceremony):
    """The silent thirty-two-minute ceremony of 2026-08-25, reproduced and refused."""

    def test_approve_refuses_without_the_operator_s_recorded_decision(self):
        slug = self.plan()
        self.run_command("preview", slug)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            project_manager.build_parser().parse_args(["approve", slug, "--depth", "standard"])

    def test_an_empty_decision_is_refused_as_firmly_as_a_missing_one(self):
        slug = self.plan()
        self.run_command("preview", slug)
        code, _, err = self.run_command("approve", slug, "--depth", "standard",
                                        "--operator-decision", "   ")
        self.assertEqual(code, 2)
        self.assertIn("nothing shows the operator was asked", err)
        self.assertIsNone(self.lib.read_record(slug)["approval"])

    def test_seal_refuses_until_the_panel_s_outcome_was_presented(self):
        slug = self.reviewed(self.finding())
        self.run_command("finding", "dispose", slug, "--id", "ARCH-1",
                         "--disposition", "rejected", "--rationale", "No.")
        code, _, err = self.run_command("seal", slug, "--operator-decision", "Seal it.")
        self.assertEqual(code, 1)
        self.assertIn("has not been presented to the operator", err)
        self.assertIsNone(self.lib.read_record(slug)["seal"])

    def test_the_presentation_cannot_precede_the_dispositions_it_reports(self):
        slug = self.reviewed(self.finding())
        code, _, err = self.run_command("present-findings", slug, "--operator-decision", "Read it.")
        self.assertEqual(code, 2)
        self.assertIn("Outstanding: ARCH-1", err)

    def test_the_whole_trail_is_recorded_in_the_operator_s_own_words(self):
        slug = self.reviewed(self.finding())
        self.run_command("finding", "dispose", slug, "--id", "ARCH-1",
                         "--disposition", "rejected", "--rationale", "No.")
        self.run_command("present-findings", slug, "--operator-decision", "I read the one finding.")
        self.run_command("seal", slug, "--operator-decision", "Ship it.")
        record = self.lib.read_record(slug)
        self.assertEqual([c["gate"] for c in record["consent"]],
                         ["approve", "findings-presented", "seal"])
        self.assertEqual(record["consent"][-1]["decision"], "Ship it.")
        trail = plan_lifecycle.consent_trail(record)
        self.assertTrue(any("Ship it." in line for line in trail))

    def test_a_depth_with_no_cold_lenses_is_not_asked_to_present_a_panel_that_never_ran(self):
        slug = self.approved("quick")
        self.assertEqual(project_manager.seal_refusals(self.lib, slug), [])
        self.assertEqual(self.run_command("seal", slug, "--operator-decision", "Seal it.")[0], 0)

    def test_the_gate_states_plainly_that_it_records_rather_than_proves(self):
        message = plan_lifecycle.missing_consent({}, "seal")
        self.assertIn("a record, not a proof", message)
        self.assertIn("published in the pull request", message)


class D11TheApprovalPaysForTwoPanelsAndBothMustSayWhatTheyDelivered(_Ceremony):
    """One approval, one depth, TWO panels: this one before the seal and the Build's deliverable review
    after it. Until StarshipSuperjam/engine-template#1067 neither recorded the effort it actually ran at,
    so a sealed `thorough` could publish a promise nothing kept."""

    def test_a_panel_that_came_in_under_the_approved_depth_is_refused_at_the_record(self):
        slug = self.approved("standard")
        argv = ["review", "record", slug, "--packet-digest", self.packet_digest(slug),
                "--delivered-effort", "low"]
        for lens in self.covering():
            argv += ["--lens", lens]
        code, _, err = self.run_command(*argv)
        self.assertEqual(code, 2)
        self.assertIn("came in under the depth the operator approved", err)

    def test_the_refusal_lands_where_the_exits_still_exist(self):
        """Not at the seal. The approval freezes at the first review recorded, so by seal time the depth
        can no longer be re-chosen and a refusal there would wedge the plan with no way out. Here, both
        honest answers are still available — and the refusal names them."""
        slug = self.approved("standard")
        argv = ["review", "record", slug, "--packet-digest", self.packet_digest(slug),
                "--delivered-effort", "low"]
        for lens in self.covering():
            argv += ["--lens", lens]
        _, _, err = self.run_command(*argv)
        self.assertIn("re-run those lenses", err)
        self.assertIn("re-approve at the depth", err)
        self.assertIsNone(self.lib.read_record(slug)["plan_review"],
                          "a refused record must leave the one review slot unspent")

    def test_a_record_that_says_nothing_about_effort_is_refused_rather_than_assumed(self):
        slug = self.approved("standard")
        argv = ["review", "record", slug, "--packet-digest", self.packet_digest(slug)]
        for lens in self.covering():
            argv += ["--lens", lens]
        code, _, err = self.run_command(*argv)
        self.assertEqual(code, 2)
        self.assertIn("has to say what they actually ran at", err)
        self.assertIn("self-reported", err,
                      "the gate must not imply it verified anything; it records a claim")

    def test_the_delivered_efforts_reach_the_record_per_lens(self):
        slug = self.reviewed()
        efforts = self.lib.read_record(slug)["plan_review"]["delivered_efforts"]
        self.assertEqual(sorted(efforts), sorted(self.covering()))
        self.assertEqual(set(efforts.values()), {"high"})

    def test_a_per_lens_form_names_one_lens_and_a_bare_level_names_them_all(self):
        lenses = ["architecture", "feasibility"]
        self.assertEqual(project_manager.parse_delivered_efforts(["high"], lenses),
                         {"architecture": "high", "feasibility": "high"})
        self.assertEqual(project_manager.parse_delivered_efforts(["high", "feasibility=medium"], lenses),
                         {"architecture": "high", "feasibility": "medium"})

    def test_a_half_filled_map_refuses_the_seal_as_incoherent(self):
        """The seal's own check is COHERENCE, not level — the level was gated where the exits were. What
        the seal owes is that a record which has started stating delivered effort states it for every
        lens it seals, or it publishes a depth as met by lenses that never said so."""
        slug = self.reviewed()
        def drop_one(current):
            current["plan_review"]["delivered_efforts"].pop(self.covering()[0])
        self.lib.update_record(slug, drop_one)
        refusals = project_manager.seal_refusals(self.lib, slug)
        self.assertTrue(any("never said what they ran at" in r for r in refusals), refusals)
        self.assertTrue(any(self.covering()[0] in r for r in refusals), refusals)

    def test_a_review_predating_the_field_seals_rather_than_wedging(self):
        """A record with no map at all is a legacy record, not a violation. Refusing it would wedge a plan
        whose panel already ran; the pull-request body carries the silence honestly instead."""
        slug = self.reviewed()
        def drop_all(current):
            current["plan_review"].pop("delivered_efforts")
        self.lib.update_record(slug, drop_all)
        refusals = project_manager.seal_refusals(self.lib, slug)
        self.assertFalse(any("ran at" in r for r in refusals), refusals)


if __name__ == "__main__":
    unittest.main()
