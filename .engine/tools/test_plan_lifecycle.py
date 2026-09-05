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

import program_manager
import project_manager
import plan_lifecycle
import plan_program
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
        # The program surface moved to its own address; a `program ...` verb goes to program_manager,
        # every plan verb to project_manager — the routing a caller does by choosing a tool name.
        tool = program_manager if argv and argv[0] == "program" else project_manager
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tool.main(["--library", str(self.root), *argv])
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


class D11DepthChoiceClosesAtTheSeal(unittest.TestCase):
    """One predicate says when a review depth can no longer be chosen, and it says WHY
    (StarshipSuperjam/engine-template#1108). Negative enumeration: an unknown status reads as open."""

    def _record(self, **over):
        record = {"current": {"revision": 1, "plan_digest": "sha256:" + "0" * 64},
                  "approval": None, "plan_review": None, "seal": None,
                  "build_binding": None, "closure": None}
        record.update(over)
        return record

    def test_open_on_every_pre_seal_status(self):
        digest = "sha256:" + "0" * 64
        self.assertIsNone(plan_lifecycle.depth_choice_closed(self._record()))
        self.assertIsNone(plan_lifecycle.depth_choice_closed(self._record(
            approval={"revision": 1, "plan_digest": digest, "depth": "standard", "at": "2026-09-04T00:00:00Z"})))
        self.assertIsNone(plan_lifecycle.depth_choice_closed(self._record(
            approval={"revision": 1, "plan_digest": digest, "depth": "standard", "at": "2026-09-04T00:00:00Z"},
            plan_review={"revision": 1, "lenses": ["architecture"], "findings": []})))

    def test_closed_with_the_real_state_and_the_real_way_on(self):
        seal = {"revision": 3}
        cases = {
            "sealed": (self._record(seal=seal), ("sealed", "Clone")),
            "bound": (self._record(seal=seal, build_binding={"x": 1}), ("bound", "Build", "clone")),
            "complete": (self._record(closure={"state": "complete", "reason": "merged"}), ("complete", "new plan")),
            "retired": (self._record(closure={"state": "retired", "reason": "superseded"}), ("retired", "reopen")),
            "abandoned": (self._record(closure={"state": "abandoned", "reason": "dropped"}), ("abandoned", "reopen")),
        }
        for name, (record, words) in cases.items():
            reason = plan_lifecycle.depth_choice_closed(record)
            self.assertIsNotNone(reason, name)
            for word in words:
                self.assertIn(word, reason, name)
            self.assertNotIn("preview", reason, name)   # never a remedy that cannot succeed
        # `complete` names no reopen: completed history is terminal and reopen refuses it.
        self.assertNotIn("reopen", plan_lifecycle.depth_choice_closed(cases["complete"][0]))


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
            "--rationale", "Not a real problem.", "--does-not-block-this-pr")[0], 0)
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
                         "--disposition", "rejected", "--rationale", "No.",
                         "--does-not-block-this-pr")
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
                         "--disposition", "rejected", "--rationale", "No.",
                         "--does-not-block-this-pr")
        stages.append(self.run_command("resume", slug)[1])
        for text in stages:
            self.assertIn("project_manager.py ", text, text)

    def _replay(self, slug, printed):
        """Substitute the placeholders a reader would, then run precisely what was printed."""
        import shlex
        # Substituted in the RAW STRING first: a placeholder like `<digest from the packet>` carries
        # spaces, and splitting before substituting turns one placeholder into four unknown tokens.
        swap = {"<why>": "Not a real problem.", "<lens>": self.covering()[0],
                "<packet.md>": str(Path(self.root) / "packet.md"),
                "<digest from the packet>": self.packet_digest(slug),
                "<digest>": self.packet_digest(slug),
                "<findings.json>": self.findings_file(self.finding()),
                "<low|medium|high>": "high",
                "<accepted-fixed|accepted-tracked|partially-accepted|rejected|escalated>": "rejected",
                "<--blocks-this-pr|--does-not-block-this-pr>": "--does-not-block-this-pr"}
        for placeholder, value in swap.items():
            printed = printed.replace(placeholder, value)
        replayed = shlex.split(printed)[1:]
        self.assertFalse([t for t in replayed if t.startswith("<")],
                         f"the guidance printed a placeholder this test does not know: {replayed}")
        return self.run_command(*replayed)

    def test_every_command_the_guidance_prints_actually_runs(self):
        """The CLASS, not one member of it.

        The previous version of this test filtered printed lines to `finding dispose` and replayed that
        one. It proved a single line runs and said nothing about the other three — and two of those three
        were refused by their own verb at the time it was green, because `--delivered-effort` became
        mandatory and no printed command gained it. A guard that covers one member of a class it names is
        the same failure as grepping for the tool's name, one level up. Every `project_manager.py` line
        the guidance prints at each stage is replayed here."""
        slug = self.plan()
        self.run_command("preview", slug)
        self.run_command("approve", slug, "--depth", "standard", "--operator-decision", "Yes.")
        # Stage one: awaiting-review. `review packet` is replayed, then the printed `review record`.
        printed = [line.strip() for line in self.run_command("resume", slug)[1].splitlines()
                   if line.strip().startswith("project_manager.py ")]
        self.assertTrue(any("review record" in line for line in printed), printed)
        for line in printed:
            if "review record" in line:
                # The single-lens form the guidance prints does not cover a standard roster, so the
                # refusal it earns is the coverage one — never the effort one this test exists for.
                code, _, err = self._replay(slug, line)
                self.assertNotIn("has to say what they actually ran at", err,
                                 f"the guidance printed a command its own verb refuses: {line}")
            else:
                code, _, err = self._replay(slug, line)
                self.assertEqual(code, 0, f"{line}\n{err}")
        # Stage two: review recorded, findings outstanding — the printed `finding dispose`.
        argv = ["review", "record", slug, "--packet-digest", self.packet_digest(slug),
                "--delivered-effort", "high"]
        for lens in self.covering():
            argv += ["--lens", lens]
        argv += ["--findings", self.findings_file(self.finding())]
        self.run_command(*argv)
        printed = [line.strip() for line in self.run_command("resume", slug)[1].splitlines()
                   if line.strip().startswith("project_manager.py ")]
        self.assertTrue(any("finding dispose" in line for line in printed), printed)
        for line in printed:
            code, _, err = self._replay(slug, line)
            self.assertEqual(code, 0, f"{line}\n{err}")


class D7CarryForwardDecayIsRechecked(_Ceremony):
    """The B2 shape: a predecessor mints obligations AFTER its successor has joined."""

    def _program_with_two_children(self):
        programs = plan_program.ProgramLibrary(self.lib)
        program = programs.create("A program", "Two plans, one after the other")
        self.program_id = programs.read(program)["program_id"]
        first = self.plan(plan_id="pln_aaaaaaaaaaaa", title="First",
                          program={"program_id": self.program_id})
        second = self.plan(plan_id="pln_bbbbbbbbbbbb", title="Second",
                           program={"program_id": self.program_id})
        programs.add_child(program, "pln_aaaaaaaaaaaa")
        programs.add_child(program, "pln_bbbbbbbbbbbb", predecessor="pln_aaaaaaaaaaaa")
        return programs, program, first, second

    def _mint_obligation_on(self, slug, document):
        document = dict(document)
        document["revision"] = 2
        document["revised_at"] = "2026-08-25T00:00:00Z"
        document["revision_note"] = "Carry an obligation the successor has never seen."
        document["program"] = {"program_id": self.program_id, "carried_obligations": [
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
                "--rationale", "Answered in the deliberation.",
                "--does-not-block-this-pr")[0], 0)
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
                         "--disposition", "rejected", "--rationale", "No.",
                         "--does-not-block-this-pr")
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
                         "--disposition", "rejected", "--rationale", "No.",
                         "--does-not-block-this-pr")
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

    def test_the_honest_exit_records_the_acknowledgement_it_bypassed_the_refusal_with(self):
        slug = self.approved("standard")
        argv = ["review", "record", slug, "--packet-digest", self.packet_digest(slug),
                "--delivered-effort", "low", "--accept-effort-shortfall"]
        for lens in self.covering():
            argv += ["--lens", lens]
        code, _, err = self.run_command(*argv)
        self.assertEqual(code, 0, err)
        self.assertTrue(self.lib.read_record(slug)["plan_review"]["effort_shortfall_accepted"])

    def test_an_amendment_that_uses_the_escape_records_it_too(self):
        """The half of the fix with no test anywhere: `review amend` passed the accept flag to the
        refusal to get past it and never wrote the acknowledgement down, leaving a gap on the record
        with nothing saying anyone accepted it — the one state the disclosure cannot describe."""
        slug = self.approved("standard")
        first, second = self.covering()[0], self.covering()[1]
        self.assertEqual(self.run_command(
            "review", "record", slug, "--packet-digest", self.packet_digest(slug),
            "--lens", first, "--delivered-effort", "high")[0], 0)
        record = self.lib.read_record(slug)["plan_review"]
        self.assertFalse(record.get("effort_shortfall_accepted"))
        code, _, err = self.run_command(
            "review", "amend", slug, "--packet-digest", self.recorded_packet_digest(slug),
            "--lens", second, "--delivered-effort", "low", "--reason", "A late lens, run cheaper.")
        self.assertEqual(code, 2, "an under-depth amendment must refuse before it records")
        self.assertIn("came in under the depth", err)
        code, _, err = self.run_command(
            "review", "amend", slug, "--packet-digest", self.recorded_packet_digest(slug),
            "--lens", second, "--delivered-effort", "low", "--accept-effort-shortfall",
            "--reason", "A late lens, run cheaper.")
        self.assertEqual(code, 0, err)
        amended = self.lib.read_record(slug)["plan_review"]
        self.assertEqual(amended["delivered_efforts"][second], "low")
        self.assertTrue(amended["effort_shortfall_accepted"],
                        "the escape was used, so the acknowledgement has to be on the record")

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


class D12OneBrokenProgramRecordFrozeEveryPlansSeal(_Ceremony):
    """The seal reached every program record, validated each, and let any failure become a refusal.

    So a single malformed file on the shelf refused the seal of EVERY plan in the library — including
    plans belonging to no program at all — and `show`, which renders the same set, went with it. The
    obvious repair is the except-continue discipline the decay re-check already uses, and taken alone
    it is a fail-OPEN: a plan whose OWN program record is unreadable would look exactly like a plan in
    no program, the carry-forward re-check would be skipped, and a debt would slip past the one gate
    that catches it. Both directions are pinned here, in both corruption classes.
    """

    def _program_with_child(self, plan_slug):
        programs = plan_program.ProgramLibrary(self.lib)
        slug = programs.create("A program", "Delivered across PRs.")
        program_id = programs.read(slug)["program_id"]
        document = self.lib.head(plan_slug)
        document["revision"] = 2
        document["program"] = {"program_id": program_id}
        self.lib.append_revision(plan_slug, document, expected_revision=1)
        programs.add_child(slug, self.lib.read_record(plan_slug)["plan_id"])
        return programs, slug, program_id

    def _corrupt(self, programs, slug, text):
        (programs.program_dir(slug) / plan_program.RECORD_FILENAME).write_text(text, encoding="utf-8")

    def _sealable(self):
        slug = self.reviewed()
        self.run_command("present-findings", slug, "--operator-decision", "Nothing was found.")
        return slug

    def test_a_standalone_plan_seals_while_an_unrelated_record_is_unparseable(self):
        victim = self._sealable()
        other = self.plan(plan_id="pln_ffffffffff01", title="Someone else's plan")
        programs, program_slug, _ = self._program_with_child(other)
        self._corrupt(programs, program_slug, "{not json at all")
        self.assertEqual(project_manager.seal_refusals(self.lib, victim), [],
                         "a plan in no program must not be held hostage by someone else's record")
        code, _, err = self.run_command("seal", victim, "--operator-decision", "Seal")
        self.assertEqual(code, 0)
        self.assertIn("worth knowing", err)
        # An unparseable record gets the honest wording: nothing can be read out of it, so whether it
        # names this plan is undetermined rather than answered in either direction.
        self.assertIn("could not be parsed at all", err)

    def test_a_standalone_plan_seals_while_an_unrelated_record_fails_its_schema(self):
        victim = self._sealable()
        other = self.plan(plan_id="pln_ffffffffff02", title="Someone else's plan")
        programs, program_slug, _ = self._program_with_child(other)
        record = programs.read(program_slug)
        record["schema_version"] = "engine-program.v99"
        self._corrupt(programs, program_slug, json.dumps(record))
        self.assertEqual(project_manager.seal_refusals(self.lib, victim), [])
        self.assertEqual(self.run_command("seal", victim, "--operator-decision", "Seal")[0], 0)

    def test_the_owning_plan_still_refuses_when_its_record_fails_its_schema(self):
        slug = self._sealable()
        programs, program_slug, _ = self._program_with_child(slug)
        record = programs.read(program_slug)
        record["schema_version"] = "engine-program.v99"
        self._corrupt(programs, program_slug, json.dumps(record))
        # Two independent sources would each catch this one — the record still parses, so its
        # children array names the plan, AND the plan's back-link names the record. Either is enough;
        # what matters here is that the seal refuses. (That the ownership half works on its own, for a
        # legacy child carrying no back-link, is pinned at unit level in test_plan_program.)
        refusals = project_manager.seal_refusals(self.lib, slug)
        self.assertTrue(refusals, "the plan's OWN broken program must still stop its seal")
        self.assertIn("cannot be read", " ".join(refusals))
        self.assertEqual(self.run_command("seal", slug, "--operator-decision", "Seal")[0], 1)

    def test_the_owning_plan_still_refuses_when_its_record_will_not_parse(self):
        # The fail-open. Nothing in the record can say whose it is, so membership rests entirely on
        # the plan's own back-link — which is why joining requires one.
        slug = self._sealable()
        programs, program_slug, _ = self._program_with_child(slug)
        self._corrupt(programs, program_slug, "{not json at all")
        refusals = project_manager.seal_refusals(self.lib, slug)
        self.assertTrue(refusals, "a plan whose own program cannot be parsed must not seal in silence")
        self.assertIn("declares that it belongs to program", " ".join(refusals))
        self.assertEqual(self.run_command("seal", slug, "--operator-decision", "Seal")[0], 1)

    def test_the_legacy_gap_is_disclosed_rather_than_resolved(self):
        # A child added before the back-link was required, under a record that will not parse, is
        # genuinely indistinguishable from a standalone plan. That is stated, not assumed away.
        victim = self._sealable()
        other = self.plan(plan_id="pln_ffffffffff03", title="Someone else's plan")
        programs, program_slug, _ = self._program_with_child(other)
        self._corrupt(programs, program_slug, "{not json at all")
        disclosures = project_manager.seal_disclosures(self.lib, victim)
        self.assertTrue(any("nothing here could tell" in line for line in disclosures))

    def test_show_prints_exactly_what_seal_discloses(self):
        # D9's rule, extended: the two commands agree about disclosures as well as refusals, so a
        # plan never reads differently depending on which one the operator happened to run.
        victim = self._sealable()
        other = self.plan(plan_id="pln_ffffffffff04", title="Someone else's plan")
        programs, program_slug, _ = self._program_with_child(other)
        self._corrupt(programs, program_slug, "{not json at all")
        shown = self.run_command("show", victim)[1]
        for disclosure in project_manager.seal_disclosures(self.lib, victim):
            self.assertIn(disclosure.splitlines()[0], shown)


class D13UnknownDebtMustNotReadAsZeroInTheOneLineSummary(_Ceremony):
    """`program list` is what an operator scans first, and it printed a bare count.

    The unknown rendering reached `program show` and stopped there, so a program whose debt could not
    be computed still summarised as '0 obligation(s) outstanding' on the line most likely to be read —
    the one place a corrupt program most needed not to look clean.
    """

    def test_a_program_with_an_unreadable_child_does_not_summarise_as_zero(self):
        programs = plan_program.ProgramLibrary(self.lib)
        slug = programs.create("Broken", "One child is not in this library.")
        program_id = programs.read(slug)["program_id"]
        plan_slug = self.plan(plan_id="pln_ffffffffff10", title="Present",
                              program={"program_id": program_id})
        programs.add_child(slug, "pln_ffffffffff10")
        record = programs.read(slug)
        record["children"].append({"plan_id": "pln_ffffffffff99", "position": 2,
                                   "added_at": "2026-01-01T00:00:00Z",
                                   "predecessor_plan_id": "pln_ffffffffff10"})
        programs._write(slug, record)
        listing = self.run_command("program", "list")[1]
        self.assertIn("obligations unknown", listing)
        self.assertNotIn("0 obligation(s) outstanding", listing)

    def test_a_healthy_program_still_shows_its_count(self):
        programs = plan_program.ProgramLibrary(self.lib)
        slug = programs.create("Healthy", "Nothing broken here.")
        program_id = programs.read(slug)["program_id"]
        self.plan(plan_id="pln_ffffffffff11", title="Only child",
                  program={"program_id": program_id})
        programs.add_child(slug, "pln_ffffffffff11")
        self.assertIn("0 obligation(s) outstanding", self.run_command("program", "list")[1])


class D14TheAddMessageSpeaksForOneBranch(_Ceremony):
    """`program add` reports what the NEXT child must answer for, and on a fork that is branch-local.

    WHAT THIS DOES AND DOES NOT PROVE, stated because the first version of this class claimed more
    than it earned. This is NOT a fix to prior behaviour: the old code read the last child by stored
    position, and `add_child` always appends, so the old message already named the added child's own
    carries and was right. What changed underneath is `outstanding_obligations`, which now returns the
    UNION over every open branch end — so had `cmd_program_add` gone on calling it, a fork would have
    started attributing the other branch's debts to a successor that can never answer them. This class
    guards that regression, not a historical defect, and its discriminating assertion is the last one:
    the union genuinely contains both branches while the message names one.
    """

    def _program(self):
        programs = plan_program.ProgramLibrary(self.lib)
        slug = programs.create("Forked", "One root, two branches.")
        return programs, slug, programs.read(slug)["program_id"]

    def _child(self, programs, program_id, plan_id, obligation_id=None, predecessor=None):
        program = {"program_id": program_id}
        if obligation_id:
            program["carried_obligations"] = [
                {"id": obligation_id, "statement": f"{obligation_id} is still owed.", "state": "carried"}]
        if predecessor:
            program["predecessor_plan_id"] = predecessor
        self.plan(plan_id=plan_id, title=plan_id[-4:], program=program)
        return plan_id

    def test_the_message_names_only_the_added_childs_own_carries(self):
        programs, slug, program_id = self._program()
        root = self._child(programs, program_id, "pln_aaaaaaaa0001")
        programs.add_child(slug, root)
        self._child(programs, program_id, "pln_aaaaaaaa0002", "OB-A", predecessor=root)
        self._child(programs, program_id, "pln_aaaaaaaa0003", "OB-B", predecessor=root)
        programs.add_child(slug, "pln_aaaaaaaa0002", predecessor=root)

        out = self.run_command("program", "add", slug, "pln_aaaaaaaa0003", "--after", root)[1]
        self.assertIn("ON THIS BRANCH", out)
        self.assertIn("OB-B", out)
        self.assertNotIn("OB-A", out,
                         "the other branch's debt must not be attributed to this branch's successor")
        # The assertion that discriminates. The program-wide union really does hold BOTH branches'
        # debts here, so a message built from it would have named OB-A too; that it does not is the
        # property under test, and it would fail the moment this verb went back to the union.
        programs = plan_program.ProgramLibrary(self.lib)
        union = {o["id"] for o in programs.outstanding_obligations(programs.read(slug))}
        self.assertEqual(union, {"OB-A", "OB-B"},
                         "precondition: the union spans both branches, so naming one is a real choice")

    def test_a_child_carrying_nothing_says_nothing(self):
        programs, slug, program_id = self._program()
        root = self._child(programs, program_id, "pln_bbbbbbbb0001")
        out = self.run_command("program", "add", slug, root)[1]
        self.assertIn("as child 1", out)
        self.assertNotIn("carried into the next child", out)


class D15TheUnknownSummaryDoesNotBlameTheWrongCause(_Ceremony):
    """A dangling or cyclic edge sits in a record that parses perfectly well.

    The summary called every unknown cause 'unreadable', which would send an operator looking for a
    corrupt file when the record is fine and the edge is the problem.
    """

    def test_a_dangling_edge_is_not_reported_as_unreadable(self):
        programs = plan_program.ProgramLibrary(self.lib)
        slug = programs.create("Dangling", "A readable record with a broken edge.")
        program_id = programs.read(slug)["program_id"]
        self.plan(plan_id="pln_ffffffffff20", title="Root", program={"program_id": program_id})
        programs.add_child(slug, "pln_ffffffffff20")
        record = programs.read(slug)
        record["children"][0]["predecessor_plan_id"] = "pln_ffffffffff99"
        programs._write(slug, record)
        listing = self.run_command("program", "list")[1]
        self.assertNotIn("unreadable", listing,
                         "the record parses; only its edge is broken")


class D16TheDisclosuresDoNotStateThingsTheCodeKnowsAreFalse(_Ceremony):
    """Two sentences the seal printed that its own inputs contradicted.

    A disclosure is the operator's only view of a record they cannot read, so a wrong one is worse
    than none: it sends them somewhere the fault is not.
    """

    def _broken(self, text, plan_ids):
        programs = plan_program.ProgramLibrary(self.lib)
        slug = programs.create("Broken", "A record that will not validate.")
        program_id = programs.read(slug)["program_id"]
        for plan_id in plan_ids:
            self.plan(plan_id=plan_id, title=plan_id[-4:], program={"program_id": program_id})
            programs.add_child(slug, plan_id)
        (programs.program_dir(slug) / plan_program.RECORD_FILENAME).write_text(text, encoding="utf-8")
        return programs, slug, program_id

    def test_a_schema_broken_record_that_DOES_name_the_plan_is_not_said_to_disown_it(self):
        # Parseable, so its children ARE readable and the code computed names_this_plan = True — then
        # printed "it does not name this plan" anyway.
        programs = plan_program.ProgramLibrary(self.lib)
        first = programs.create("Claims it", "Broken, but its children are readable.")
        first_id = programs.read(first)["program_id"]
        self.plan(plan_id="pln_ffffffffff30", title="Child", program={"program_id": first_id})
        programs.add_child(first, "pln_ffffffffff30")
        record = programs.read(first)
        record["schema_version"] = "engine-program.v99"          # parseable, schema-invalid
        (programs.program_dir(first) / plan_program.RECORD_FILENAME).write_text(
            json.dumps(record), encoding="utf-8")

        disclosures = project_manager.seal_disclosures(self.lib, self.lib.resolve("pln_ffffffffff30"))
        self.assertFalse(any("does not name this plan" in line for line in disclosures),
                         "the code computed that it DOES name this plan")

    def test_an_unparseable_record_is_not_said_to_disown_a_plan_either(self):
        # Nothing can be read out of it, so "it does not name this plan" is exactly as unfounded as
        # the opposite claim would be.
        programs = plan_program.ProgramLibrary(self.lib)
        slug = programs.create("Unreadable", "Will not parse.")
        program_id = programs.read(slug)["program_id"]
        self.plan(plan_id="pln_ffffffffff31", title="Theirs", program={"program_id": program_id})
        programs.add_child(slug, "pln_ffffffffff31")
        (programs.program_dir(slug) / plan_program.RECORD_FILENAME).write_text(
            "{not json", encoding="utf-8")
        standalone = self.plan(plan_id="pln_ffffffffff32", title="Standalone")

        disclosures = project_manager.seal_disclosures(self.lib, standalone)
        self.assertFalse(any("does not name this plan" in line for line in disclosures),
                         "nothing can be read out of an unparseable record, in either direction")
        self.assertTrue(any("cannot be determined" in line for line in disclosures))

    def test_a_schema_broken_record_is_not_described_as_unparseable(self):
        # It parses. Only its schema fails, so its children are readable and membership is knowable —
        # telling the operator "nothing here could tell" contradicts the line printed above it.
        programs = plan_program.ProgramLibrary(self.lib)
        slug = programs.create("Schema broken", "Parses; fails its schema.")
        (programs.program_dir(slug) / plan_program.RECORD_FILENAME).write_text(
            json.dumps({"schema_version": "engine-program.v99", "children": []}), encoding="utf-8")
        standalone = self.plan(plan_id="pln_ffffffffff33", title="Standalone")

        disclosures = project_manager.seal_disclosures(self.lib, standalone)
        self.assertFalse(any("cannot be parsed" in line for line in disclosures),
                         "a schema-invalid record parses; only its schema fails")
