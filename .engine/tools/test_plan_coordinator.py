#!/usr/bin/env python3
"""Tests for plan_coordinator — the command surface over the plan library.

Two invariants get most of the attention because both are the kind that quietly stop holding.

NOTHING AUTO-SELECTS. Not the newest plan, not the only plan. The way the wrong plan gets sealed is a
tool being helpful.

DEPTH IS OFFERED ONLY AFTER A FULL RENDER, at the digest being approved. A marker that survived a
revision would vouch for a plan nobody read, so the tests revise and re-check rather than trusting
that the marker is digest-keyed because the code says so.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

import plan_coordinator
import plan_projection
import plan_store

from test_plan_store import _document


class _Surface(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "plans"
        self.lib = plan_store.PlanLibrary(self.root)
        self.addCleanup(self._tmp.cleanup)

    def run_command(self, *argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = plan_coordinator.main(["--library", str(self.root), *argv])
        return code, out.getvalue(), err.getvalue()

    def _plan(self, **over):
        document = _document(**over)
        return self.lib.create(document), document

    def _write_document(self, document) -> str:
        path = Path(self._tmp.name) / f"{document['plan_id']}-{document['revision']}.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return str(path)


class Selection(_Surface):
    def test_no_command_auto_selects_the_only_plan(self):
        self._plan()
        for verb in ("show", "resume", "preview", "validate", "depths"):
            code, _, err = self.run_command(verb, "")
            self.assertEqual(code, 2, verb)
            self.assertIn("nothing is selected by default", err, verb)

    def test_an_ambiguous_prefix_fails_and_names_the_candidates(self):
        self._plan(plan_id="pln_abc111111111", title="First")
        self._plan(plan_id="pln_abc222222222", title="Second")
        code, _, err = self.run_command("show", "pln_abc")
        self.assertEqual(code, 2)
        self.assertIn("matches 2 plans", err)
        self.assertIn("pln_abc111111111", err)

    def test_a_unique_prefix_and_a_slug_both_select(self):
        slug, document = self._plan()
        self.assertEqual(self.run_command("show", document["plan_id"][:9])[0], 0)
        self.assertEqual(self.run_command("show", slug)[0], 0)


class DepthGate(_Surface):
    def test_depths_are_refused_before_the_plan_has_been_presented(self):
        slug, _ = self._plan()
        code, _, err = self.run_command("depths", slug)
        self.assertEqual(code, 2)
        self.assertIn("has not been presented", err)
        self.assertIn("preview", err)

    def test_depths_are_offered_after_a_preview(self):
        slug, _ = self._plan()
        self.assertEqual(self.run_command("preview", slug)[0], 0)
        code, out, _ = self.run_command("depths", slug)
        self.assertEqual(code, 0)
        for depth in ("light", "standard", "thorough"):
            self.assertIn(depth, out)

    def test_a_revision_invalidates_the_presentation(self):
        # The marker is keyed by digest, so it cannot vouch for a plan nobody read. Proven by
        # revising rather than by reading the implementation.
        slug, _ = self._plan()
        self.run_command("preview", slug)
        self.assertEqual(self.run_command("depths", slug)[0], 0)
        self.lib.append_revision(slug, _document(revision=2), expected_revision=1)
        code, _, err = self.run_command("depths", slug)
        self.assertEqual(code, 2)
        self.assertIn("has not been presented", err)

    def test_the_preview_marker_is_not_plan_evidence(self):
        # An operator's reading habits must not become part of the document a Build consumes.
        slug, _ = self._plan()
        self.run_command("preview", slug)
        record = self.lib.read_record(slug)
        self.assertNotIn("_previewed", record)
        self.assertTrue((self.root / slug / plan_coordinator._PREVIEW_FILENAME).exists())

    def test_depths_warn_when_the_plan_is_not_sealable(self):
        document = _document()
        document["deliberation"]["unresolved_decisions"] = ["Who owns retention?"]
        slug = self.lib.create(document)
        self.run_command("preview", slug)
        code, out, _ = self.run_command("depths", slug)
        self.assertEqual(code, 0)
        self.assertIn("moving target", out)
        self.assertIn("Who owns retention?", out)


class DerivedStatus(_Surface):
    def test_status_comes_from_evidence_at_every_stage(self):
        slug, document = self._plan()
        digest = self.lib.read_record(slug)["current"]["plan_digest"]
        self.assertIn("awaiting-approval", self.run_command("show", slug)[1])

        self.lib.update_record(slug, lambda r: r.update({"approval": {
            "revision": 1, "plan_digest": digest, "depth": "standard", "at": "2026-08-23T01:00:00Z"}}))
        self.assertIn("awaiting-review", self.run_command("show", slug)[1])

        self.lib.update_record(slug, lambda r: r.update({"plan_review": {
            "revision": 1, "plan_digest": digest, "packet_digest": digest,
            "at": "2026-08-23T02:00:00Z", "lenses": ["architecture"]}}))
        self.assertIn("review-recorded", self.run_command("show", slug)[1])

        self.lib.update_record(slug, lambda r: r.update({"seal": {
            "revision": 1, "reviewed_digest": digest, "sealed_digest": digest,
            "build_plan_digest": digest, "at": "2026-08-23T03:00:00Z", "delta_judgment": "none"}}))
        self.assertIn("sealed", self.run_command("show", slug)[1])

    def test_a_draft_with_open_decisions_is_a_draft_not_awaiting_approval(self):
        document = _document()
        document["deliberation"]["unresolved_decisions"] = ["Still open."]
        slug = self.lib.create(document)
        out = self.run_command("show", slug)[1]
        self.assertIn("draft", out)
        self.assertIn("Still open.", out)

    def test_list_and_show_agree_because_status_has_one_home(self):
        slug, _ = self._plan()
        listed = self.run_command("list")[1]
        shown = self.run_command("show", slug)[1]
        self.assertIn("awaiting-approval", listed)
        self.assertIn("awaiting-approval", shown)


class Resume(_Surface):
    def test_resume_names_the_one_next_step_at_each_stage(self):
        slug, _ = self._plan()
        digest = self.lib.read_record(slug)["current"]["plan_digest"]
        self.assertIn("preview the full revision", self.run_command("resume", slug)[1])

        self.lib.update_record(slug, lambda r: r.update({"approval": {
            "revision": 1, "plan_digest": digest, "depth": "standard", "at": "2026-08-23T01:00:00Z"}}))
        self.assertIn("one cold plan review", self.run_command("resume", slug)[1])

        self.lib.update_record(slug, lambda r: r.update({"plan_review": {
            "revision": 1, "plan_digest": digest, "packet_digest": digest,
            "at": "2026-08-23T02:00:00Z", "lenses": ["architecture"],
            "findings": [{"id": "ARCH-1", "lens": "architecture", "severity": "serious",
                          "summary": "A thing."}]}}))
        self.assertIn("disposition 1 outstanding finding", self.run_command("resume", slug)[1])

    def test_resume_on_a_draft_with_blockers_names_them(self):
        document = _document()
        document["deliberation"]["unresolved_decisions"] = ["An open question."]
        slug = self.lib.create(document)
        self.assertIn("An open question.", self.run_command("resume", slug)[1])

    def test_resume_refuses_to_advise_past_a_damaged_chain(self):
        slug, _ = self._plan()
        head = self.root / slug / self.lib.read_record(slug)["current"]["snapshot"]
        head.write_text(json.dumps({"schema_version": "engine-plan.v1"}), encoding="utf-8")
        code, out, _ = self.run_command("resume", slug)
        self.assertEqual(code, 1)
        self.assertIn("needs attention before anything else", out)
        self.assertNotIn("next:", out)

    def test_a_closed_plan_has_nothing_next(self):
        slug, _ = self._plan()
        self.lib.update_record(slug, lambda r: r.update({"closure": {
            "state": "abandoned", "at": "2026-08-23T04:00:00Z", "reason": "superseded by a better shape"}}))
        out = self.run_command("resume", slug)[1]
        self.assertIn("nothing", out)
        self.assertIn("superseded by a better shape", out)


class Diff(_Surface):
    def test_diff_shows_the_rendered_plan_changing_not_its_json(self):
        slug, _ = self._plan()
        second = _document(revision=2)
        second["deliberation"]["problem_frame"] = "A materially different framing of the problem."
        self.lib.append_revision(slug, second, expected_revision=1)
        code, out, _ = self.run_command("diff", slug)
        self.assertEqual(code, 0)
        self.assertIn("A materially different framing of the problem.", out)
        self.assertIn("revision 1", out)
        self.assertIn("revision 2", out)
        self.assertNotIn('"problem_frame":', out)      # prose, not encoding

    def test_diff_defaults_to_the_last_two_revisions(self):
        slug, _ = self._plan()
        for revision in (2, 3):
            document = _document(revision=revision)
            document["intent"]["interpretation"] = f"Interpretation at revision {revision}."
            self.lib.append_revision(slug, document, expected_revision=revision - 1)
        out = self.run_command("diff", slug)[1]
        self.assertIn("revision 2", out)
        self.assertIn("revision 3", out)
        self.assertNotIn("Interpretation at revision 1.", out)

    def test_identical_revisions_say_so_rather_than_printing_nothing(self):
        slug, _ = self._plan()
        self.lib.append_revision(slug, _document(revision=2), expected_revision=1)
        out = self.run_command("diff", slug, "--from", "1", "--to", "1")[1]
        self.assertIn("nothing to compare", out)


class Doctor(_Surface):
    def test_a_healthy_library_reports_no_problems(self):
        self._plan()
        code, out, _ = self.run_command("doctor")
        self.assertEqual(code, 0)
        self.assertIn("no problems found", out)

    def test_doctor_reports_a_damaged_revision_without_repairing_it(self):
        slug, _ = self._plan()
        head_path = self.root / slug / self.lib.read_record(slug)["current"]["snapshot"]
        before = head_path.read_bytes()
        document = json.loads(before)
        document["title"] = "Tampered"
        head_path.write_text(json.dumps(document), encoding="utf-8")

        code, out, _ = self.run_command("doctor")
        self.assertEqual(code, 1)
        self.assertIn("does not match its recorded digest", out)
        # Evidence of what happened must survive the diagnosis.
        self.assertEqual(json.loads(head_path.read_text(encoding="utf-8"))["title"], "Tampered")

    def test_doctor_warns_about_a_world_readable_library(self):
        self._plan()
        os.chmod(self.root, 0o755)
        code, out, _ = self.run_command("doctor")
        self.assertEqual(code, 1)
        self.assertIn("0755", out)
        self.assertIn("operator intent", out)

    def test_doctor_warns_about_a_synced_volume(self):
        synced = Path(self._tmp.name) / "Dropbox" / "repo" / ".engine" / "plans"
        synced.mkdir(parents=True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = plan_coordinator.main(["--library", str(synced), "doctor"])
        self.assertEqual(code, 1)
        self.assertIn("sync", out.getvalue().lower())

    def test_an_absent_library_is_not_an_error(self):
        code, out, _ = self.run_command("doctor")
        self.assertEqual(code, 0)
        self.assertIn("created with the first plan", out)


class InitAndReindex(_Surface):
    def test_init_mints_a_plan_and_projects_it(self):
        document = _document()
        code, out, _ = self.run_command("init", "--document", self._write_document(document))
        self.assertEqual(code, 0)
        self.assertIn(document["plan_id"], out)
        slug = self.lib.resolve(document["plan_id"])
        self.assertTrue((self.root / slug / plan_projection.PLAN_MD).exists())
        self.assertTrue((self.root / plan_projection.INDEX_MD).exists())

    def test_init_refuses_an_invalid_document_and_writes_nothing(self):
        document = _document()
        del document["deliberation"]
        code, _, err = self.run_command("init", "--document", self._write_document(document))
        self.assertEqual(code, 2)
        self.assertEqual(self.lib.slugs(), [])

    def test_reindex_rebuilds_every_projection(self):
        slug, _ = self._plan()
        self.run_command("reindex")
        (self.root / slug / plan_projection.PLAN_MD).unlink()
        (self.root / plan_projection.INDEX_MD).unlink()
        code, out, _ = self.run_command("reindex")
        self.assertEqual(code, 0)
        self.assertIn("projected 1 plan", out)
        self.assertTrue((self.root / slug / plan_projection.PLAN_MD).exists())

    def test_reindex_flags_an_unreadable_plan(self):
        slug, _ = self._plan()
        (self.root / slug / self.lib.read_record(slug)["current"]["snapshot"]).unlink()
        code, _, err = self.run_command("reindex")
        self.assertEqual(code, 1)
        self.assertIn("needs attention", err)

    def test_list_on_an_empty_library_is_calm(self):
        code, out, _ = self.run_command("list")
        self.assertEqual(code, 0)
        self.assertIn("no plans", out)

    def test_list_says_a_shelf_is_not_a_queue(self):
        self._plan()
        self.assertIn("nothing here is current by default", self.run_command("list")[1])


class Validate(_Surface):
    def test_a_sound_plan_validates(self):
        slug, _ = self._plan()
        code, out, _ = self.run_command("validate", slug)
        self.assertEqual(code, 0)
        self.assertIn("revision chain is sound", out)

    def test_validate_reports_blockers_without_failing(self):
        document = _document()
        document["deliberation"]["unresolved_decisions"] = ["Open."]
        slug = self.lib.create(document)
        code, out, _ = self.run_command("validate", slug)
        self.assertEqual(code, 0)
        self.assertIn("not sealable yet", out)

    def test_validate_fails_on_a_broken_chain(self):
        slug, _ = self._plan()
        (self.root / slug / self.lib.read_record(slug)["current"]["snapshot"]).unlink()
        code, _, err = self.run_command("validate", slug)
        self.assertEqual(code, 1)
        self.assertIn("missing from disk", err)


class _Governed(_Surface):
    """A plan walked to the edge of a seal, so each test can remove exactly one precondition."""

    def _findings(self, *findings) -> str:
        path = Path(self._tmp.name) / "findings.json"
        path.write_text(json.dumps(list(findings)), encoding="utf-8")
        return str(path)

    def _to_reviewed(self, findings=(), **over):
        slug, document = self._plan(**over)
        self.run_command("preview", slug)
        self.assertEqual(self.run_command("approve", slug, "--depth", "standard")[0], 0)
        digest = self.lib.read_record(slug)["current"]["plan_digest"]
        argv = ["review", "record", slug, "--lens", "architecture", "--lens", "risk-governance",
                "--packet-digest", digest]
        if findings:
            argv += ["--findings", self._findings(*findings)]
        self.assertEqual(self.run_command(*argv)[0], 0)
        return slug, document


class Approval(_Surface):
    def test_approval_is_refused_before_the_plan_is_presented(self):
        slug, _ = self._plan()
        code, _, err = self.run_command("approve", slug, "--depth", "standard")
        self.assertEqual(code, 2)
        self.assertIn("has not been presented", err)

    def test_approval_binds_the_revision_and_its_digest(self):
        slug, _ = self._plan()
        self.run_command("preview", slug)
        code, out, _ = self.run_command("approve", slug, "--depth", "thorough")
        self.assertEqual(code, 0)
        approval = self.lib.read_record(slug)["approval"]
        self.assertEqual(approval["depth"], "thorough")
        self.assertEqual(approval["revision"], 1)
        self.assertEqual(approval["plan_digest"], self.lib.read_record(slug)["current"]["plan_digest"])
        self.assertIn("one cold plan review", out)


class OneReviewPerPlan(_Governed):
    def test_a_second_review_is_refused_with_the_reason(self):
        slug, _ = self._to_reviewed()
        digest = self.lib.read_record(slug)["current"]["plan_digest"]
        code, _, err = self.run_command("review", "record", slug, "--lens", "architecture",
                                        "--packet-digest", digest)
        self.assertEqual(code, 2)
        self.assertIn("exactly one per plan", err)
        self.assertIn("scrap-and-redesign", err)

    def test_folding_fixes_in_does_not_force_a_re_panel(self):
        # The whole point of the cadence: revisions after the review are fixes, not a new plan.
        slug, _ = self._to_reviewed()
        for revision in (2, 3):
            self.lib.append_revision(slug, _document(revision=revision), expected_revision=revision - 1)
        record = self.lib.read_record(slug)
        self.assertIsNotNone(record["plan_review"])
        self.assertFalse(plan_store.approval_is_stale(record))
        # The next step is a seal, not another panel.
        next_step = self.run_command("resume", slug)[1].split("next:")[1]
        self.assertIn("seal the plan", next_step)
        self.assertNotIn("run the one cold plan review", next_step)

    def test_a_packet_names_the_digest_it_rendered(self):
        slug, _ = self._plan()
        self.run_command("preview", slug)
        self.run_command("approve", slug, "--depth", "standard")
        code, out, err = self.run_command("review", "packet", slug)
        self.assertEqual(code, 0)
        self.assertIn("Packet digest: sha256:", out)
        self.assertIn(self.lib.read_record(slug)["current"]["plan_digest"], out)
        self.assertIn("packet digest:", err)

    def test_a_packet_is_refused_on_a_stale_approval(self):
        slug, _ = self._plan()
        self.run_command("preview", slug)
        self.run_command("approve", slug, "--depth", "standard")
        self.lib.append_revision(slug, _document(revision=2), expected_revision=1)
        code, _, err = self.run_command("review", "packet", slug)
        self.assertEqual(code, 2)
        self.assertIn("re-approve", err)


class SealRefusals(_Governed):
    """Every refusal N06 requires, each proven by removing exactly one precondition."""

    def _blocking(self):
        return {"id": "ARCH-B1", "lens": "architecture", "severity": "blocking",
                "summary": "The store's first write precedes its fence."}

    def test_a_clean_reviewed_plan_seals(self):
        slug, _ = self._to_reviewed()
        code, out, _ = self.run_command("seal", slug)
        self.assertEqual(code, 0)
        self.assertIn("sealed", out)
        seal = self.lib.read_record(slug)["seal"]
        self.assertEqual(seal["reviewed_digest"], seal["sealed_digest"])
        self.assertEqual(seal["delta_judgment"], "none")

    def test_an_unresolved_decision_refuses_the_seal(self):
        document = _document()
        document["deliberation"]["unresolved_decisions"] = ["Who owns retention?"]
        slug = self.lib.create(document)
        self.run_command("preview", slug)
        self.run_command("approve", slug, "--depth", "standard")
        digest = self.lib.read_record(slug)["current"]["plan_digest"]
        self.run_command("review", "record", slug, "--lens", "architecture", "--packet-digest", digest)
        code, _, err = self.run_command("seal", slug)
        self.assertEqual(code, 1)
        self.assertIn("unresolved", err)
        self.assertIsNone(self.lib.read_record(slug)["seal"])

    def test_an_unresolved_assumption_refuses_the_seal(self):
        document = _document()
        document["build_plan"]["assumptions"] = [{"claim": "The disk is durable.", "status": "unresolved"}]
        slug = self.lib.create(document)
        self.run_command("preview", slug)
        self.run_command("approve", slug, "--depth", "standard")
        digest = self.lib.read_record(slug)["current"]["plan_digest"]
        self.run_command("review", "record", slug, "--lens", "architecture", "--packet-digest", digest)
        code, _, err = self.run_command("seal", slug)
        self.assertEqual(code, 1)
        self.assertIn("The disk is durable.", err)

    def test_a_missing_review_refuses_the_seal(self):
        slug, _ = self._plan()
        self.run_command("preview", slug)
        self.run_command("approve", slug, "--depth", "standard")
        code, _, err = self.run_command("seal", slug)
        self.assertEqual(code, 1)
        self.assertIn("no cold plan review", err)

    def test_a_missing_approval_refuses_the_seal(self):
        slug, _ = self._plan()
        code, _, err = self.run_command("seal", slug)
        self.assertEqual(code, 1)
        self.assertIn("has not been approved", err)

    def test_an_undispositioned_finding_refuses_the_seal(self):
        slug, _ = self._to_reviewed(findings=(self._blocking(),))
        code, _, err = self.run_command("seal", slug)
        self.assertEqual(code, 1)
        self.assertIn("no disposition", err)
        self.assertIn("ARCH-B1", err)

    def test_a_stale_approval_refuses_the_seal(self):
        slug, _ = self._plan()
        self.run_command("preview", slug)
        self.run_command("approve", slug, "--depth", "standard")
        self.lib.append_revision(slug, _document(revision=2), expected_revision=1)
        code, _, err = self.run_command("seal", slug)
        self.assertEqual(code, 1)
        self.assertIn("changed before it was ever reviewed", err)

    def test_a_payload_the_build_coordinator_would_refuse_refuses_the_seal(self):
        # Delegated, not re-expressed: a v1 payload reads fine and cannot be handed to a DAG Build.
        slug, _ = self._to_reviewed()
        record = self.lib.read_record(slug)
        document = self.lib.head(slug)
        v1 = {k: v for k, v in document["build_plan"].items() if k != "parallelism"}
        v1["schema_version"] = "build-plan.v1"
        v1["work_items"] = [{k: v for k, v in item.items()
                             if k in ("id", "description", "paths", "verification")}
                            for item in v1["work_items"]]
        second = _document(revision=2, build_plan=v1)
        self.lib.append_revision(slug, second, expected_revision=record["current"]["revision"])
        code, _, err = self.run_command("seal", slug)
        self.assertEqual(code, 1)
        self.assertIn("only build-plan.v2 can be sealed", err)

    def test_all_refusals_are_reported_together(self):
        slug, _ = self._plan()
        code, _, err = self.run_command("seal", slug)
        self.assertEqual(code, 1)
        self.assertIn("has not been approved", err)
        self.assertIn("no cold plan review", err)


class SealIsTerminal(_Governed):
    def test_a_blocking_finding_leaves_a_resumable_draft_and_no_seal_artifact(self):
        # There is deliberately no sealed-but-failed state.
        slug, _ = self._to_reviewed(findings=({"id": "RISK-B1", "lens": "risk-governance",
                                               "severity": "blocking",
                                               "summary": "The library is the only copy."},))
        self.assertEqual(self.run_command("seal", slug)[0], 1)
        record = self.lib.read_record(slug)
        self.assertIsNone(record["seal"])
        self.assertEqual(plan_store.derived_status(record), "review-recorded")
        # Still editable, still resumable — the plan is not stuck anywhere.
        self.lib.append_revision(slug, _document(revision=2), expected_revision=1)
        self.assertIn("disposition 1 outstanding finding", self.run_command("resume", slug)[1])

    def test_sealing_twice_is_refused(self):
        slug, _ = self._to_reviewed()
        self.assertEqual(self.run_command("seal", slug)[0], 0)
        code, _, err = self.run_command("seal", slug)
        self.assertEqual(code, 1)
        self.assertIn("already sealed", err)
        self.assertIn("clone", err)

    def test_a_sealed_plan_cannot_be_approved_again(self):
        slug, _ = self._to_reviewed()
        self.run_command("seal", slug)
        self.run_command("preview", slug)
        code, _, err = self.run_command("approve", slug, "--depth", "light")
        self.assertEqual(code, 2)
        self.assertIn("terminal", err)

    def test_a_seal_cannot_be_reopened(self):
        slug, _ = self._to_reviewed()
        self.run_command("seal", slug)
        self.run_command("retire", slug, "--reason", "trying to escape the seal")
        code, _, err = self.run_command("reopen", slug)
        self.assertEqual(code, 2)
        self.assertIn("terminal", err)


class DeltaJudgment(_Governed):
    def _reviewed_then_revised(self):
        slug, _ = self._to_reviewed()
        second = _document(revision=2)
        second["deliberation"]["failure_modes"].append("A fix folded in after the review.")
        self.lib.append_revision(slug, second, expected_revision=1)
        return slug

    def test_a_changed_plan_needs_one_proportional_judgment(self):
        slug = self._reviewed_then_revised()
        code, _, err = self.run_command("seal", slug)
        self.assertEqual(code, 2)
        self.assertIn("delta needs one proportional judgment", err)
        self.assertIn("diff", err)
        self.assertIsNone(self.lib.read_record(slug)["seal"])

    def test_the_judgment_seals_and_the_delta_is_recorded_for_disclosure(self):
        slug = self._reviewed_then_revised()
        code, out, _ = self.run_command("seal", slug, "--delta-judgment", "scoped",
                                        "--delta-rationale", "One failure mode added; nothing else moved.")
        self.assertEqual(code, 0)
        seal = self.lib.read_record(slug)["seal"]
        self.assertNotEqual(seal["reviewed_digest"], seal["sealed_digest"])
        self.assertEqual(seal["delta_judgment"], "scoped")
        self.assertIn("One failure mode added", seal["delta_rationale"])
        self.assertIn("must disclose", out)

    def test_a_scoped_judgment_needs_a_rationale(self):
        slug = self._reviewed_then_revised()
        code, _, err = self.run_command("seal", slug, "--delta-judgment", "scoped")
        self.assertEqual(code, 2)
        self.assertIn("needs a rationale", err)

    def test_an_unchanged_plan_needs_no_judgment(self):
        slug, _ = self._to_reviewed()
        code, out, _ = self.run_command("seal", slug)
        self.assertEqual(code, 0)
        self.assertIn("unchanged since review", out)


class Dispositions(_Governed):
    def test_disposing_a_finding_clears_it_and_reports_what_is_left(self):
        slug, _ = self._to_reviewed(findings=(
            {"id": "A1", "lens": "architecture", "severity": "serious", "summary": "One."},
            {"id": "A2", "lens": "architecture", "severity": "nit", "summary": "Two."}))
        code, out, _ = self.run_command("finding", "dispose", slug, "--id", "A1",
                                        "--disposition", "accepted-fixed",
                                        "--rationale", "Folded into revision 2.")
        self.assertEqual(code, 0)
        self.assertIn("outstanding: A2", out)
        self.run_command("finding", "dispose", slug, "--id", "A2",
                         "--disposition", "rejected", "--rationale", "Style preference.")
        self.assertIn("outstanding: none", self.run_command(
            "finding", "dispose", slug, "--id", "A1", "--disposition", "accepted-fixed",
            "--rationale", "Folded into revision 2.")[1])

    def test_an_unknown_finding_id_lists_the_real_ones(self):
        slug, _ = self._to_reviewed(findings=({"id": "A1", "lens": "architecture",
                                               "severity": "nit", "summary": "One."},))
        code, _, err = self.run_command("finding", "dispose", slug, "--id", "NOPE",
                                        "--disposition", "rejected", "--rationale", "n/a")
        self.assertEqual(code, 2)
        self.assertIn("A1", err)

    def test_disposing_without_a_review_is_refused(self):
        slug, _ = self._plan()
        code, _, err = self.run_command("finding", "dispose", slug, "--id", "A1",
                                        "--disposition", "rejected", "--rationale", "n/a")
        self.assertEqual(code, 2)
        self.assertIn("nothing to disposition", err)


class Closing(_Surface):
    def test_retire_abandon_and_complete_all_keep_everything(self):
        for verb, state, plan_id in (("retire", "retired", "pln_aaaaaaaaaaaa"),
                                     ("abandon", "abandoned", "pln_bbbbbbbbbbbb"),
                                     ("complete", "complete", "pln_cccccccccccc")):
            with self.subTest(verb=verb):
                slug, _ = self._plan(plan_id=plan_id, title=f"Plan to {verb}")
                before = sorted(p.name for p in (self.root / slug / "revisions").iterdir())
                code, out, _ = self.run_command(verb, slug, "--reason", f"because {verb}")
                self.assertEqual(code, 0)
                self.assertIn(state, out)
                self.assertEqual(sorted(p.name for p in (self.root / slug / "revisions").iterdir()),
                                 before)
                self.assertEqual(plan_store.derived_status(self.lib.read_record(slug)), state)

    def test_reopen_undoes_a_retirement(self):
        slug, _ = self._plan()
        self.run_command("retire", slug, "--reason", "superseded")
        code, out, _ = self.run_command("reopen", slug)
        self.assertEqual(code, 0)
        self.assertIn("was retired", out)
        self.assertEqual(plan_store.derived_status(self.lib.read_record(slug)), "draft")

    def test_reopening_an_open_plan_says_so(self):
        slug, _ = self._plan()
        code, _, err = self.run_command("reopen", slug)
        self.assertEqual(code, 2)
        self.assertIn("not closed", err)

    def test_closing_twice_is_refused(self):
        slug, _ = self._plan()
        self.run_command("retire", slug, "--reason", "superseded")
        code, _, err = self.run_command("abandon", slug, "--reason", "changed my mind")
        self.assertEqual(code, 2)
        self.assertIn("already retired", err)


class Enumeration(unittest.TestCase):
    def test_the_depths_offered_match_the_documented_set(self):
        self.assertEqual(set(plan_coordinator.DEPTHS), {"light", "standard", "thorough"})

    def test_every_status_the_surface_can_report_is_in_the_enumeration(self):
        for status in ("draft", "awaiting-approval", "awaiting-review", "review-recorded",
                       "sealed", "active", "complete", "retired", "abandoned"):
            self.assertIn(status, plan_store.STATUSES)


if __name__ == "__main__":
    unittest.main()
