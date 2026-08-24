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

from unittest import mock

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


class Revise(_Governed):
    def test_revise_mints_the_next_revision(self):
        slug, _ = self._plan()
        code, out, _ = self.run_command("revise", slug, "--document",
                                        self._write_document(_document(revision=2)))
        self.assertEqual(code, 0)
        self.assertIn("revision 2", out)
        self.assertEqual(self.lib.read_record(slug)["current"]["revision"], 2)

    def test_a_stale_expected_head_is_refused_and_writes_nothing(self):
        slug, _ = self._plan()
        self.run_command("revise", slug, "--document", self._write_document(_document(revision=2)))
        before = {p.name: p.read_bytes() for p in sorted((self.root / slug).rglob("*")) if p.is_file()}
        code, _, err = self.run_command("revise", slug, "--document",
                                        self._write_document(_document(revision=2, title="Clobber")),
                                        "--expect-revision", "1")
        self.assertEqual(code, 2)
        self.assertIn("another session revised this plan", err)
        after = {p.name: p.read_bytes() for p in sorted((self.root / slug).rglob("*")) if p.is_file()}
        self.assertEqual(before, after)

    def test_revising_a_sealed_plan_is_refused_and_points_at_clone(self):
        slug, _ = self._to_reviewed()
        self.run_command("seal", slug)
        code, _, err = self.run_command("revise", slug, "--document",
                                        self._write_document(_document(revision=2)))
        self.assertEqual(code, 2)
        self.assertIn("clone", err)

    def test_revising_after_a_review_says_the_panel_does_not_re_run(self):
        slug, _ = self._to_reviewed()
        out = self.run_command("revise", slug, "--document",
                               self._write_document(_document(revision=2)))[1]
        self.assertIn("does NOT re-run", out)
        self.assertIn("proportional judgment", out)

    def test_revising_before_a_review_says_the_approval_no_longer_speaks(self):
        slug, _ = self._plan()
        self.run_command("preview", slug)
        self.run_command("approve", slug, "--depth", "standard")
        out = self.run_command("revise", slug, "--document",
                               self._write_document(_document(revision=2)))[1]
        self.assertIn("approve again", out)

    def test_only_two_functions_can_mint_a_revision_identity(self):
        # A second minting route could put a NEW revision on disk without a compare-and-swap, without
        # validation, or without a ledger entry — sound-looking and wrong. `import` also writes
        # revision files, but it mints nothing: it replays revisions that already exist with digests
        # it verified first, which is why this pins the MINTING helper rather than the act of writing.
        import ast
        source = Path(plan_store.__file__).read_text(encoding="utf-8")
        minters = [node.name for node in ast.walk(ast.parse(source))
                   if isinstance(node, ast.FunctionDef) and node.name != "_snapshot_name"
                   and any(isinstance(child, ast.Attribute) and child.attr == "_snapshot_name"
                           for child in ast.walk(node))]
        self.assertEqual(sorted(minters), ["append_revision", "create"])


class Clone(_Governed):
    def test_a_clone_carries_no_approval_review_or_seal(self):
        slug, document = self._to_reviewed()
        self.run_command("seal", slug)
        code, out, _ = self.run_command("clone", slug, "--reason", "the shape needs rethinking")
        self.assertEqual(code, 0)
        new_slug = next(s for s in self.lib.slugs() if s != slug)
        record = self.lib.read_record(new_slug)
        self.assertIsNone(record["approval"])
        self.assertIsNone(record["plan_review"])
        self.assertIsNone(record["seal"])
        self.assertNotEqual(record["plan_id"], document["plan_id"])
        self.assertEqual(record["current"]["revision"], 1)

    def test_a_clone_records_where_it_came_from(self):
        slug, document = self._plan()
        self.run_command("clone", slug, "--reason", "forking the approach")
        new_slug = next(s for s in self.lib.slugs() if s != slug)
        intake = self.lib.read_record(new_slug)["intake"]
        self.assertIn(document["plan_id"], intake["provenance"])
        self.assertIn("forking the approach", intake["provenance"])

    def test_a_clone_takes_a_new_title_when_given_one(self):
        slug, _ = self._plan()
        self.run_command("clone", slug, "--reason", "r", "--title", "A different approach entirely")
        new_slug = next(s for s in self.lib.slugs() if s != slug)
        self.assertEqual(self.lib.read_record(new_slug)["title"], "A different approach entirely")


class Transport(_Governed):
    def _bundle_path(self, name="bundle.json") -> str:
        return str(Path(self._tmp.name) / name)

    def _other_library(self) -> tuple[Path, plan_store.PlanLibrary]:
        root = Path(self._tmp.name) / "elsewhere"
        return root, plan_store.PlanLibrary(root)

    def _import_into(self, root: Path, bundle: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = plan_coordinator.main(["--library", str(root), "import", "--bundle", bundle])
        return code, out.getvalue(), err.getvalue()

    def test_a_plan_round_trips_with_every_digest_verified(self):
        slug, document = self._to_reviewed()
        for revision in (2, 3):
            self.lib.append_revision(slug, _document(revision=revision), expected_revision=revision - 1)
        path = self._bundle_path()
        self.assertEqual(self.run_command("export", slug, "--output", path)[0], 0)

        root, other = self._other_library()
        code, out, _ = self._import_into(root, path)
        self.assertEqual(code, 0, out)
        self.assertIn("every digest verified", out)

        imported = other.resolve(document["plan_id"])
        self.assertEqual(other.read_record(imported)["current"]["revision"], 3)
        self.assertEqual(other.head(imported), self.lib.head(slug))
        self.assertEqual(other.verify_chain(imported), [])
        # The gate evidence travels too — a bundle that lost the review would silently un-review a plan.
        self.assertIsNotNone(other.read_record(imported)["plan_review"])

    def test_export_uploads_nothing_and_says_so(self):
        slug, _ = self._plan()
        out = self.run_command("export", slug, "--output", self._bundle_path())[1]
        self.assertIn("Nothing was uploaded", out)

    def test_a_tampered_bundle_is_refused(self):
        slug, _ = self._plan()
        path = self._bundle_path()
        self.run_command("export", slug, "--output", path)
        bundle = json.loads(Path(path).read_text(encoding="utf-8"))
        bundle["revisions"]["1"]["title"] = "Tampered in transit"
        Path(path).write_text(json.dumps(bundle), encoding="utf-8")
        root, _ = self._other_library()
        code, _, err = self._import_into(root, path)
        self.assertEqual(code, 2)
        self.assertIn("does not match its own digest", err)

    def test_a_bundle_with_a_swapped_revision_body_is_refused(self):
        # Digest recomputed over the whole bundle AND per revision, so re-stamping the outer digest
        # is not enough to smuggle a changed revision through.
        slug, _ = self._plan()
        path = self._bundle_path()
        self.run_command("export", slug, "--output", path)
        bundle = json.loads(Path(path).read_text(encoding="utf-8"))
        bundle["revisions"]["1"]["title"] = "Swapped"
        bundle["bundle_digest"] = plan_coordinator.core.digest(
            {"record": bundle["record"], "revisions": bundle["revisions"]})
        Path(path).write_text(json.dumps(bundle), encoding="utf-8")
        root, _ = self._other_library()
        code, _, err = self._import_into(root, path)
        self.assertEqual(code, 2)
        self.assertIn("does not match its recorded digest", err)

    def test_re_importing_an_identical_plan_is_a_no_op(self):
        slug, _ = self._plan()
        path = self._bundle_path()
        self.run_command("export", slug, "--output", path)
        code, out, _ = self.run_command("import", "--bundle", path)
        self.assertEqual(code, 0)
        self.assertIn("already here and identical", out)

    def test_a_colliding_but_different_plan_is_refused(self):
        slug, _ = self._plan()
        path = self._bundle_path()
        self.run_command("export", slug, "--output", path)
        self.lib.append_revision(slug, _document(revision=2, title="Diverged locally"),
                                 expected_revision=1)
        code, _, err = self.run_command("import", "--bundle", path)
        self.assertEqual(code, 2)
        self.assertIn("DIFFERENT plan", err)
        self.assertIn("ambiguous", err)

    def test_a_redacted_body_is_not_resurrected_by_a_round_trip(self):
        # The one thing a transport format must not do.
        slug, _ = self._plan()
        self.lib.append_revision(slug, _document(revision=2), expected_revision=1)
        self.lib.redact_revision(slug, 1, reason="raw intent held a credential")
        path = self._bundle_path()
        out = self.run_command("export", slug, "--output", path)[1]
        self.assertIn("not resurrected", out)
        bundle = json.loads(Path(path).read_text(encoding="utf-8"))
        self.assertNotIn("1", bundle["revisions"])

        root, other = self._other_library()
        self.assertEqual(self._import_into(root, path)[0], 0)
        imported = other.slugs()[0]
        self.assertIn("redacted", other.read_record(imported)["ledger"][0])
        self.assertEqual(other.verify_chain(imported), [])

    def test_a_file_that_is_not_a_bundle_is_named_as_such(self):
        path = self._bundle_path("nonsense.json")
        Path(path).write_text(json.dumps({"schema_version": "something-else"}), encoding="utf-8")
        code, _, err = self.run_command("import", "--bundle", path)
        self.assertEqual(code, 2)
        self.assertIn("not a plan bundle", err)


class HostileBundles(_Governed):
    """A bundle is the one untrusted input this tool has. Its own digests prove nothing about the
    honesty of whoever built it — every one of them is computed by that same author — so the checks
    that matter are on SHAPE, and they must run before any write."""

    def _hostile(self, name, mutate) -> str:
        slug, _ = self._plan()
        path = str(Path(self._tmp.name) / f"{name}.json")
        self.run_command("export", slug, "--output", path)
        bundle = json.loads(Path(path).read_text(encoding="utf-8"))
        mutate(bundle)
        # Re-stamp the outer digest so content verification cannot be what catches this.
        bundle["bundle_digest"] = plan_coordinator.core.digest(
            {"record": bundle["record"], "revisions": bundle["revisions"]})
        Path(path).write_text(json.dumps(bundle), encoding="utf-8")
        return path

    def _import_elsewhere(self, path):
        root = Path(self._tmp.name) / "target"
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = plan_coordinator.main(["--library", str(root), "import", "--bundle", path])
        return code, out.getvalue(), err.getvalue()

    def test_an_absolute_snapshot_path_cannot_write_outside_the_library(self):
        # `Path("/library") / "/etc/passwd"` is `/etc/passwd` — the left side is discarded silently.
        victim = Path(self._tmp.name) / "victim.txt"
        victim.write_text("original\n", encoding="utf-8")

        def point_at_victim(bundle):
            bundle["record"]["ledger"][0]["snapshot"] = str(victim)
            bundle["record"]["current"]["snapshot"] = str(victim)

        code, _, err = self._import_elsewhere(self._hostile("absolute", point_at_victim))
        self.assertEqual(code, 2)
        self.assertEqual(victim.read_text(encoding="utf-8"), "original\n",
                         "an imported bundle overwrote a file outside the library")

    def test_a_traversing_snapshot_path_cannot_write_outside_the_library(self):
        victim = Path(self._tmp.name) / "victim.txt"
        victim.write_text("original\n", encoding="utf-8")

        def traverse(bundle):
            bundle["record"]["ledger"][0]["snapshot"] = "revisions/../../../victim.txt"

        code, _, err = self._import_elsewhere(self._hostile("traverse", traverse))
        self.assertEqual(code, 2)
        self.assertEqual(victim.read_text(encoding="utf-8"), "original\n")

    def test_a_traversing_slug_cannot_create_directories_outside_the_library(self):
        def traverse(bundle):
            bundle["record"]["slug"] = "../../../../escaped--abc123"

        code, _, err = self._import_elsewhere(self._hostile("slug", traverse))
        self.assertEqual(code, 2)
        self.assertFalse((Path(self._tmp.name).parent / "escaped--abc123").exists())
        self.assertFalse((Path(self._tmp.name) / "escaped--abc123").exists())

    def test_a_hostile_bundle_writes_nothing_at_all(self):
        def traverse(bundle):
            bundle["record"]["ledger"][0]["snapshot"] = "/tmp/should-never-be-written.json"

        path = self._hostile("nothing", traverse)
        root = Path(self._tmp.name) / "target"
        self._import_elsewhere(path)
        self.assertFalse(root.exists(), "a refused import still created the library")

    def test_ensure_dir_refuses_a_target_outside_its_boundary(self):
        outside = Path(self._tmp.name) / "outside"
        with self.assertRaisesRegex(plan_store.PlanStoreError, "outside the plan library"):
            plan_store.ensure_dir(outside, within=self.root)
        self.assertFalse(outside.exists(), "the refusal still created the directory")

    def test_contain_allows_the_boundary_itself_and_anything_under_it(self):
        self.assertEqual(plan_store.contain(self.root, self.root, "x"), self.root.resolve())
        inside = self.root / "a" / "b"
        self.assertEqual(plan_store.contain(inside, self.root, "x"), inside.resolve())


class SingleMintedGatesUnderConcurrency(_Governed):
    """Recording a review, sealing, or closing does not mint a revision, so the compare-and-swap on
    `current.revision` cannot catch a second one. Only re-checking inside the lock can — these drive
    the exact call shape the CLI uses, which the earlier CAS tests never did."""

    def test_a_second_review_is_refused_even_when_both_readers_saw_none(self):
        slug, _ = self._plan()
        self.run_command("preview", slug)
        self.run_command("approve", slug, "--depth", "standard")
        digest = self.lib.read_record(slug)["current"]["plan_digest"]
        # Both sessions read a record with no review; A records first, B must still be refused.
        self.assertEqual(self.run_command("review", "record", slug, "--lens", "architecture",
                                          "--packet-digest", digest)[0], 0)

        def racing_write(current):
            current["plan_review"] = {"revision": 1, "plan_digest": digest, "packet_digest": digest,
                                      "at": "2026-08-23T09:00:00Z", "lenses": ["clobber"]}
        # The raw update_record shape the old code used: no CAS, guard evaluated outside.
        self.lib.update_record(slug, racing_write)
        self.assertEqual(self.lib.read_record(slug)["plan_review"]["lenses"], ["clobber"],
                         "fixture sanity: an unguarded write does clobber")
        # And through the command, which re-checks inside the lock, it is refused.
        code, _, err = self.run_command("review", "record", slug, "--lens", "architecture",
                                        "--packet-digest", digest)
        self.assertEqual(code, 2)
        self.assertIn("exactly one per plan", err)

    def test_creating_the_same_plan_twice_is_refused_inside_the_lock(self):
        slug, document = self._plan()
        with self.assertRaisesRegex(plan_store.PlanStoreError, "already exists"):
            self.lib.create(document)
        self.assertEqual(len(self.lib.read_record(slug)["ledger"]), 1)

    def test_a_refused_create_leaves_no_orphan_skeleton(self):
        bad = _document()
        del bad["deliberation"]
        with self.assertRaises(plan_store.PlanStoreError):
            self.lib.create(bad)
        stray = [p for p in self.root.iterdir() if p.is_dir()] if self.root.exists() else []
        for path in stray:
            self.assertFalse((path / "revisions").exists(),
                             "a failed create left a revisions/ skeleton behind")

    def test_a_second_close_is_refused_rather_than_silently_dropped(self):
        slug, _ = self._plan()
        self.assertEqual(self.run_command("retire", slug, "--reason", "superseded")[0], 0)
        code, _, err = self.run_command("abandon", slug, "--reason", "changed my mind")
        self.assertEqual(code, 2)
        self.assertEqual(self.lib.read_record(slug)["closure"]["state"], "retired")

    def test_a_program_create_holds_the_lock(self):
        import plan_program
        programs = plan_program.ProgramLibrary(self.lib)
        slug = programs.create("A program", "An objective.")
        self.assertTrue((programs.program_dir(slug) / "record.json").exists())
        self.assertTrue((programs.program_dir(slug) / "record.json.lock").exists(),
                        "ProgramLibrary.create did not take its lock")


class DurabilityIsReported(_Governed):
    def test_a_failed_flush_refuses_the_write_instead_of_reporting_success(self):
        # A silent degrade would make a full or failing disk indistinguishable from a durable write.
        slug, document = self._plan()
        before = (self.root / slug / "record.json").read_bytes()
        with mock.patch.object(plan_coordinator.core, "durable_fsync", return_value=False):
            with self.assertRaisesRegex(plan_store.PlanStoreError, "flush it to stable storage"):
                self.lib.append_revision(slug, _document(revision=2), expected_revision=1)
        self.assertEqual((self.root / slug / "record.json").read_bytes(), before,
                         "a failed durable write still changed the record")

    def test_a_declined_directory_flush_is_not_fatal(self):
        # Some filesystems legitimately refuse to fsync a directory fd; the file is already durable.
        with mock.patch.object(plan_coordinator.core, "fsync_dir", return_value=False):
            slug, document = self._plan()
        self.assertEqual(self.lib.head(slug), document)


class RecoverVerb(_Governed):
    def test_recover_reports_the_intact_ancestor_and_changes_nothing(self):
        slug, _ = self._plan()
        self.lib.append_revision(slug, _document(revision=2), expected_revision=1)
        head = self.root / slug / self.lib.read_record(slug)["current"]["snapshot"]
        head.write_text(json.dumps({"schema_version": "engine-plan.v1"}), encoding="utf-8")
        before = (self.root / slug / "record.json").read_bytes()

        code, out, _ = self.run_command("recover", slug)
        self.assertEqual(code, 1)
        self.assertIn("newest intact revision is 1", out)
        self.assertIn("Nothing was changed", out)
        self.assertEqual((self.root / slug / "record.json").read_bytes(), before)

    def test_recover_on_a_sound_plan_says_there_is_nothing_to_do(self):
        slug, _ = self._plan()
        code, out, _ = self.run_command("recover", slug)
        self.assertEqual(code, 0)
        self.assertIn("Nothing to recover", out)


class ProgramVerbs(_Governed):
    def _obligation(self, identifier, statement, state="carried"):
        return {"id": identifier, "statement": statement, "state": state}

    def _program_with_child(self):
        code, out, err = self.run_command("program", "new", "--title", "Two-PR program",
                                          "--objective", "Delivered across two PRs.")
        self.assertEqual(code, 0, err)
        program_id = out.split()[2]
        document = _document(plan_id="pln_aaaaaaaaaaaa", title="PR A")
        document["program"] = {"program_id": program_id, "carried_obligations": [
            self._obligation("OB-1", "PR B cuts the Build Coordinator over.")]}
        self.lib.create(document)
        self.assertEqual(self.run_command("program", "add", program_id, "pln_aaaaaaaaaaaa")[0], 0)
        return program_id

    def test_a_program_is_creatable_and_readable_from_the_command_line(self):
        program_id = self._program_with_child()
        code, out, _ = self.run_command("program", "show", program_id)
        self.assertEqual(code, 0)
        self.assertIn("PR A", out)
        self.assertIn("OB-1", out)
        self.assertIn(program_id, self.run_command("program", "list")[1])

    def test_the_verb_refuses_a_successor_that_drops_an_obligation(self):
        program_id = self._program_with_child()
        successor = _document(plan_id="pln_bbbbbbbbbbbb", title="PR B")
        successor["program"] = {"program_id": program_id,
                                "predecessor_plan_id": "pln_aaaaaaaaaaaa",
                                "carried_obligations": []}
        self.lib.create(successor)
        code, _, err = self.run_command("program", "add", program_id, "pln_bbbbbbbbbbbb",
                                        "--after", "pln_aaaaaaaaaaaa")
        self.assertEqual(code, 2)
        self.assertIn("OB-1", err)
        self.assertIn("does not answer for", err)

    def test_the_verb_reports_what_the_next_child_now_owes(self):
        program_id = self._program_with_child()
        out = self.run_command("program", "show", program_id)[1]
        self.assertIn("None of them can be dropped by saying nothing", out)


class FilesystemProbe(unittest.TestCase):
    """The df/stat shell-out itself, not a mock of the function that wraps it."""

    def _fake_run(self, results):
        calls = iter(results)

        def run(argv, **kwargs):
            return next(calls)
        return run

    def _completed(self, stdout, code=0):
        import subprocess
        return subprocess.CompletedProcess(args=[], returncode=code, stdout=stdout, stderr="")

    def test_a_matching_network_type_is_reported(self):
        with self._as("Darwin"), \
             mock.patch.object(plan_store.subprocess, "run",
                               side_effect=self._fake_run([
                                   self._completed("Filesystem ...\n//host/share  1 1 1 1% /mnt\n"),
                                   self._completed("smbfs\n")])):
            self.assertEqual(plan_store._filesystem_type(Path("/")), "smbfs")

    def test_a_local_disk_reports_nothing(self):
        with self._as("Darwin"), \
             mock.patch.object(plan_store.subprocess, "run",
                               side_effect=self._fake_run([self._completed("Filesystem ...\n", 1)])):
            self.assertIsNone(plan_store._filesystem_type(Path("/")))

    def _as(self, sysname):
        return mock.patch.object(plan_store.os, "uname",
                                 return_value=type("U", (), {"sysname": sysname})())

    def test_a_failing_probe_degrades_to_unknown_rather_than_crashing_on_darwin(self):
        # Pin the platform. Without this the test passes on Darwin and fails on Linux, where the
        # probe reads /proc/mounts and never calls subprocess at all — so mocking subprocess proves
        # nothing there. CI caught exactly that: it returned 'ext4' and the assertion blew up.
        with self._as("Darwin"), \
             mock.patch.object(plan_store.subprocess, "run", side_effect=OSError("no df here")):
            self.assertIsNone(plan_store._filesystem_type(Path("/")))

    def test_the_linux_branch_reports_the_longest_matching_mount(self):
        # A REAL directory, because the probe deliberately walks up to the nearest existing path —
        # the library may not exist yet when this is asked. A made-up mount point would resolve to
        # "/" and quietly test the wrong line.
        with tempfile.TemporaryDirectory() as tmp:
            mounts = ("proc /proc proc rw 0 0\n"
                      "/dev/sda1 / ext4 rw 0 0\n"
                      f"//host/share {tmp} cifs rw 0 0\n")
            with self._as("Linux"), mock.patch("builtins.open", mock.mock_open(read_data=mounts)):
                self.assertEqual(plan_store._filesystem_type(Path(tmp)), "cifs")

    def test_the_linux_branch_prefers_the_deepest_mount_not_the_first_match(self):
        # "/" prefixes every path, so a shallower mount must never win over a deeper one.
        with tempfile.TemporaryDirectory() as tmp:
            mounts = (f"//host/share {tmp} nfs rw 0 0\n"
                      "/dev/sda1 / ext4 rw 0 0\n")
            with self._as("Linux"), mock.patch("builtins.open", mock.mock_open(read_data=mounts)):
                self.assertEqual(plan_store._filesystem_type(Path(tmp)), "nfs")

    def test_a_probe_of_a_path_that_does_not_exist_yet_resolves_to_its_nearest_parent(self):
        # The library is asked about before it is created, so this is the normal case, not an edge.
        with tempfile.TemporaryDirectory() as tmp:
            mounts = f"//host/share {tmp} smbfs rw 0 0\n"
            with self._as("Linux"), mock.patch("builtins.open", mock.mock_open(read_data=mounts)):
                self.assertEqual(
                    plan_store._filesystem_type(Path(tmp) / "not" / "created" / "yet"), "smbfs")

    def test_an_unreadable_proc_mounts_degrades_to_unknown_rather_than_crashing(self):
        with self._as("Linux"), mock.patch("builtins.open", side_effect=OSError("no /proc here")):
            self.assertIsNone(plan_store._filesystem_type(Path("/")))

    def test_an_undeterminable_volume_is_reported_as_undetermined(self):
        # The distinction the old code claimed and did not make: unknown is not the same as local.
        with mock.patch.object(plan_store, "_filesystem_type", return_value=None):
            with tempfile.TemporaryDirectory() as tmp:
                self.assertIsNone(plan_store.volume_warning(Path(tmp)))
                self.assertFalse(plan_store.volume_determined(Path(tmp)))

    def test_a_determined_local_volume_reads_as_determined(self):
        with mock.patch.object(plan_store, "_filesystem_type", return_value="apfs"):
            with tempfile.TemporaryDirectory() as tmp:
                self.assertIsNone(plan_store.volume_warning(Path(tmp)))
                self.assertTrue(plan_store.volume_determined(Path(tmp)))

    def test_a_synced_path_is_determined_from_the_path_alone(self):
        self.assertTrue(plan_store.volume_determined(Path("/Users/x/Dropbox/repo/.engine/plans")))


class ErrorLegibility(_Governed):
    def test_a_schema_failure_names_the_field_and_the_constraint(self):
        # Every optional gate is `oneOf(null, {...})`, so without descending into the branch this
        # reported the whole object as "not valid under any of the given schemas".
        slug, _ = self._plan()
        self.run_command("preview", slug)
        self.run_command("approve", slug, "--depth", "standard")
        findings = Path(self._tmp.name) / "bad-findings.json"
        findings.write_text(json.dumps([{"id": "A1", "lens": "architecture",
                                         "severity": "major", "summary": "s"}]), encoding="utf-8")
        code, _, err = self.run_command("review", "record", slug, "--lens", "architecture",
                                        "--packet-digest", self.lib.read_record(slug)["current"]["plan_digest"],
                                        "--findings", str(findings))
        self.assertEqual(code, 2)
        self.assertIn("severity", err)
        self.assertIn("blocking", err)
        self.assertNotIn("is not valid under any of the given schemas", err)

    def test_a_malformed_digest_names_the_digest_field(self):
        slug, _ = self._plan()
        self.run_command("preview", slug)
        self.run_command("approve", slug, "--depth", "standard")
        code, _, err = self.run_command("review", "record", slug, "--lens", "architecture",
                                        "--packet-digest", "sha256:abc")
        self.assertEqual(code, 2)
        self.assertIn("packet_digest", err)

    def test_validate_reports_one_problem_once(self):
        slug, _ = self._plan()
        head = self.root / slug / self.lib.read_record(slug)["current"]["snapshot"]
        head.write_text(json.dumps({"schema_version": "engine-plan.v1"}), encoding="utf-8")
        code, _, err = self.run_command("validate", slug)
        self.assertEqual(code, 1)
        self.assertEqual(err.count("does not match its recorded digest"), 1)

    def test_doctor_states_the_remedy_for_a_permission_finding(self):
        self._plan()
        os.chmod(self.root, 0o755)
        out = self.run_command("doctor")[1]
        self.assertIn("chmod 700", out)

    def test_show_does_not_over_reassure_on_an_unapproved_plan(self):
        slug, _ = self._plan()
        out = self.run_command("show", slug)[1]
        self.assertIn("but the gates do", out)

    def test_resume_does_not_promise_a_handoff_this_pr_does_not_ship(self):
        slug, _ = self._to_reviewed()
        self.run_command("seal", slug)
        out = self.run_command("resume", slug)[1]
        self.assertIn("not wired up yet", out)
        self.assertIn("clone", out)

class ImportCannotOverwriteANeighbour(_Governed):
    """Constraining the slug's SHAPE stops a bundle escaping the library. It does nothing to stop one
    landing on top of a neighbour — a slug is not secret; it is printed by `list` and it is the
    folder name."""

    def test_a_bundle_claiming_another_plans_slug_is_refused(self):
        victim_slug, victim = self._plan(plan_id="pln_aaaaaaaaaaaa", title="Victim plan")
        attacker_root = Path(self._tmp.name) / "attacker"
        attacker = plan_store.PlanLibrary(attacker_root)
        attacker.create(_document(plan_id="pln_bbbbbbbbbbbb", title="Attacker plan"))
        bundle = str(Path(self._tmp.name) / "b.json")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            plan_coordinator.main(["--library", str(attacker_root), "export",
                                   attacker.slugs()[0], "--output", bundle])
        payload = json.loads(Path(bundle).read_text(encoding="utf-8"))
        payload["record"]["slug"] = victim_slug          # the only edit
        payload["bundle_digest"] = plan_coordinator.core.digest(
            {"record": payload["record"], "revisions": payload["revisions"]})
        Path(bundle).write_text(json.dumps(payload), encoding="utf-8")

        code, _, err = self.run_command("import", "--bundle", bundle)
        self.assertEqual(code, 2)
        self.assertIn("a different plan already occupies", err)
        self.assertEqual(self.lib.read_record(victim_slug)["plan_id"], victim["plan_id"],
                         "an imported bundle destroyed a different local plan")
        self.assertEqual(self.lib.head(victim_slug), victim)

    def test_re_importing_the_same_plan_at_its_own_slug_is_still_fine(self):
        # The guard is about a DIFFERENT plan; a plan's own backup must still import.
        slug, _ = self._plan()
        bundle = str(Path(self._tmp.name) / "own.json")
        self.run_command("export", slug, "--output", bundle)
        code, out, _ = self.run_command("import", "--bundle", bundle)
        self.assertEqual(code, 0)
        self.assertIn("already here and identical", out)

    def test_import_takes_the_plans_own_lock(self):
        import ast
        source = Path(plan_coordinator.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        importer = next(node for node in ast.walk(tree)
                        if isinstance(node, ast.FunctionDef) and node.name == "cmd_import")
        locked = [node for node in ast.walk(importer)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and node.func.attr == "exclusive_lock_for"]
        self.assertTrue(locked, "cmd_import writes without taking the plan's lock")


class RedactionCannotLie(_Governed):
    """Neither crash window may misreport: a redaction must never read as corruption, and the store
    must never vouch that a body is gone while it is still on disk."""

    def _two_revisions(self):
        slug, _ = self._plan()
        self.lib.append_revision(slug, _document(revision=2), expected_revision=1)
        return slug

    def test_a_crash_before_the_unlink_reads_as_interrupted_not_as_done(self):
        slug = self._two_revisions()
        snapshot = self.root / slug / self.lib.read_record(slug)["ledger"][0]["snapshot"]
        with mock.patch.object(plan_store.PlanLibrary, "_unlink_body", side_effect=OSError("crash")):
            with self.assertRaises(OSError):
                self.lib.redact_revision(slug, 1, reason="a credential")

        # The body is still there — and crucially the record does NOT claim it is gone.
        self.assertTrue(snapshot.exists())
        self.assertNotIn("redacted", self.lib.read_record(slug)["ledger"][0],
                         "the record claimed a redaction that had not happened")
        problems = self.lib.verify_chain(slug)
        self.assertTrue(any("began and did not finish" in p for p in problems), problems)
        self.assertTrue(any("rotate it regardless" in p for p in problems), problems)

    def test_a_crash_after_the_unlink_reads_as_interrupted_not_as_loss(self):
        slug = self._two_revisions()
        snapshot = self.root / slug / self.lib.read_record(slug)["ledger"][0]["snapshot"]
        real_write = plan_store.PlanLibrary._write_json

        def fail_on_the_record(self_, path, value):
            if path.name == plan_store.RECORD_FILENAME:
                raise OSError("crash")
            return real_write(self_, path, value)

        with mock.patch.object(plan_store.PlanLibrary, "_write_json", fail_on_the_record):
            with self.assertRaises(OSError):
                self.lib.redact_revision(slug, 1, reason="a credential")

        self.assertFalse(snapshot.exists())
        problems = self.lib.verify_chain(slug)
        # Named as an interrupted redaction, never as the "loss rather than intent" corruption line.
        self.assertTrue(any("began and did not finish" in p for p in problems), problems)
        self.assertFalse(any("loss rather than intent" in p for p in problems), problems)

    def test_retrying_completes_the_redaction_and_clears_the_marker(self):
        slug = self._two_revisions()
        with mock.patch.object(plan_store.PlanLibrary, "_unlink_body", side_effect=OSError("crash")):
            with self.assertRaises(OSError):
                self.lib.redact_revision(slug, 1, reason="a credential")
        self.lib.redact_revision(slug, 1, reason="a credential")
        self.assertEqual(self.lib.interrupted_redactions(slug), [])
        self.assertEqual(self.lib.verify_chain(slug), [])
        self.assertIn("redacted", self.lib.read_record(slug)["ledger"][0])

    def test_a_completed_redaction_leaves_no_marker_behind(self):
        slug = self._two_revisions()
        self.lib.redact_revision(slug, 1, reason="a credential")
        self.assertEqual(self.lib.interrupted_redactions(slug), [])
        self.assertEqual(self.lib.verify_chain(slug), [])

    def test_a_retry_takes_a_corrected_reason(self):
        slug = self._two_revisions()
        self.lib.redact_revision(slug, 1, reason="wrong reason typed in haste")
        self.lib.redact_revision(slug, 1, reason="the actual reason")
        self.assertEqual(self.lib.read_record(slug)["ledger"][0]["redacted"]["reason"],
                         "the actual reason")


class ProjectionsNeverGoStale(_Governed):
    def test_closing_a_plan_updates_its_own_plan_md(self):
        # The transition an earlier skip-heuristic broke: `close` projects immediately afterwards, so
        # a skip keyed on "is closed" fired on the very first render and PLAN.md kept its old status
        # forever, disagreeing with the index beside it.
        slug, _ = self._plan()
        self.run_command("retire", slug, "--reason", "superseded")
        text = (self.root / slug / plan_projection.PLAN_MD).read_text(encoding="utf-8")
        self.assertIn("**Status**: retired", text)
        index = json.loads((self.root / plan_projection.INDEX_JSON).read_text(encoding="utf-8"))
        self.assertEqual(index["plans"][0]["status"], "retired")

    def test_reopening_then_reclosing_updates_it_again(self):
        slug, _ = self._plan()
        self.run_command("retire", slug, "--reason", "superseded")
        self.run_command("reopen", slug)
        self.run_command("abandon", slug, "--reason", "dropped for good")
        text = (self.root / slug / plan_projection.PLAN_MD).read_text(encoding="utf-8")
        self.assertIn("**Status**: abandoned", text)

    def test_plan_md_and_the_index_never_disagree_about_status(self):
        slug, _ = self._plan()
        for verb, reason in (("retire", "a"), ("reopen", None), ("complete", "merged")):
            self.run_command(*( [verb, slug] + (["--reason", reason] if reason else []) ))
            text = (self.root / slug / plan_projection.PLAN_MD).read_text(encoding="utf-8")
            index = json.loads((self.root / plan_projection.INDEX_JSON).read_text(encoding="utf-8"))
            self.assertIn(f"**Status**: {index['plans'][0]['status']}", text)


class RecoverOnTotalLoss(_Governed):
    def test_total_loss_gives_guidance_rather_than_a_bare_exception(self):
        # The case `recover` most exists for, and the one where its advice used to be unreachable.
        slug, _ = self._plan()
        (self.root / slug / self.lib.read_record(slug)["current"]["snapshot"]).unlink()
        code, out, err = self.run_command("recover", slug)
        self.assertEqual(code, 1)
        self.assertIn("No revision of this plan is intact", out)
        self.assertIn("restore", out)
        self.assertIn("import", out)
        self.assertIn("re-authoring", out)
        self.assertNotIn("Traceback", err)

    def test_total_loss_still_changes_nothing(self):
        slug, _ = self._plan()
        (self.root / slug / self.lib.read_record(slug)["current"]["snapshot"]).unlink()
        before = (self.root / slug / "record.json").read_bytes()
        self.run_command("recover", slug)
        self.assertEqual((self.root / slug / "record.json").read_bytes(), before)


class ReadyToSealReadsCleanly(_Governed):
    def test_a_fully_clear_plan_says_ready_rather_than_contradicting_itself(self):
        slug, _ = self._to_reviewed()
        out = self.run_command("show", slug)[1]
        self.assertIn("ready to seal", out)
        self.assertNotIn("but the gates do", out)

    def test_a_plan_with_gates_ahead_of_it_still_says_so(self):
        slug, _ = self._plan()
        out = self.run_command("show", slug)[1]
        self.assertIn("but the gates do", out)
        self.assertIn("preview the full revision", out)


class SchemaErrorsNameTheRealProblem(_Governed):
    """Depth alone was not enough: a missing required field and an unexpected key are reported AT the
    object's own path, tying with the null branch's 'not of type null'."""

    def _refusal(self, approval) -> str:
        slug, _ = self._plan()
        with self.assertRaises(plan_store.PlanStoreError) as caught:
            self.lib.update_record(slug, lambda r: r.update({"approval": approval}))
        return str(caught.exception)

    def test_a_missing_required_field_is_named(self):
        message = self._refusal({"revision": 1, "plan_digest": "sha256:" + "a" * 64,
                                 "at": "2026-08-23T00:00:00Z"})
        self.assertIn("'depth' is a required property", message)
        self.assertNotIn("is not of type 'null'", message)

    def test_an_unexpected_key_is_named(self):
        message = self._refusal({"revision": 1, "plan_digest": "sha256:" + "a" * 64,
                                 "depth": "standard", "at": "2026-08-23T00:00:00Z", "bogus": "x"})
        self.assertIn("'bogus' was unexpected", message)
        self.assertNotIn("is not of type 'null'", message)

    def test_a_deeper_failure_still_names_its_field(self):
        message = self._refusal({"revision": 1, "plan_digest": "not-a-digest",
                                 "depth": "standard", "at": "2026-08-23T00:00:00Z"})
        self.assertIn("approval.plan_digest", message)


class LedgerDiagnostics(_Governed):
    def test_a_reordered_ledger_is_named_as_reordered_not_duplicated(self):
        slug, _ = self._plan()
        for revision in (2, 3):
            self.lib.append_revision(slug, _document(revision=revision), expected_revision=revision - 1)

        def reorder(record):
            record["ledger"] = [record["ledger"][1], record["ledger"][0], record["ledger"][2]]
        self.lib.update_record(slug, reorder)
        problems = self.lib.verify_chain(slug)
        self.assertTrue(any("appears after revision" in p for p in problems), problems)
        self.assertFalse(any("more than once" in p for p in problems), problems)

    def test_a_genuinely_duplicated_entry_is_named_as_duplicated(self):
        slug, _ = self._plan()
        self.lib.append_revision(slug, _document(revision=2), expected_revision=1)

        def duplicate(record):
            record["ledger"] = [record["ledger"][0], record["ledger"][0], record["ledger"][1]]
        self.lib.update_record(slug, duplicate)
        self.assertTrue(any("more than once" in p for p in self.lib.verify_chain(slug)))

    def test_reopen_reports_the_state_it_actually_found(self):
        slug, _ = self._plan()
        self.run_command("abandon", slug, "--reason", "dropped")
        out = self.run_command("reopen", slug)[1]
        self.assertIn("was abandoned", out)

class MarkerRobustness(_Governed):
    """The intent marker is read by the integrity check, so anything that can appear beside it must
    not be able to take that check down."""

    def _redactable(self):
        slug, _ = self._plan()
        self.lib.append_revision(slug, _document(revision=2), expected_revision=1)
        return slug

    def test_a_malformed_marker_is_ignored_rather_than_crashing_the_integrity_check(self):
        # A cloud-sync "conflicted copy" is the obvious source, on exactly the volumes the store
        # already warns about. A check that raises takes the whole plan's diagnostics with it.
        slug = self._redactable()
        for name in (".redacting-000001 (conflicted copy)", ".redacting-", ".redacting-abc",
                     ".redacting-000001.tmp"):
            (self.root / slug / "revisions" / name).write_text("x", encoding="utf-8")
        self.assertEqual(self.lib.verify_chain(slug), [])
        self.assertEqual(self.lib.interrupted_redactions(slug), [])
        self.assertEqual(self.run_command("validate", slug)[0], 0)

    def test_a_malformed_marker_does_not_crash_recover_either(self):
        slug = self._redactable()
        (self.root / slug / "revisions" / ".redacting-nonsense").write_text("x", encoding="utf-8")
        code, out, err = self.run_command("recover", slug)
        self.assertEqual(code, 0)
        self.assertNotIn("Traceback", err)
        self.assertIn("Nothing to recover", out)

    def test_a_leftover_marker_on_a_COMPLETED_redaction_is_not_reported_as_unfinished(self):
        # The very last step is clearing the marker. A crash there leaves a redaction that genuinely
        # finished, and reporting it as unfinished would send an operator to rotate a credential that
        # was in fact excised.
        slug = self._redactable()
        self.lib.redact_revision(slug, 1, reason="a credential")
        self.lib._write_intent(slug, {"revision": 1}, "stale")
        self.assertEqual(self.lib.verify_chain(slug), [])

    def test_a_marker_on_a_revision_whose_body_survives_is_still_reported(self):
        # The safe direction must keep working: marked in the record but the body still on disk is a
        # genuinely unfinished redaction and must not be swallowed by the rule above.
        slug = self._redactable()
        with mock.patch.object(plan_store.PlanLibrary, "_unlink_body", side_effect=OSError("crash")):
            with self.assertRaises(OSError):
                self.lib.redact_revision(slug, 1, reason="a credential")
        self.assertTrue(any("began and did not finish" in p for p in self.lib.verify_chain(slug)))


class ImportToleratesADamagedNeighbour(_Governed):
    def test_a_corrupt_unrelated_record_does_not_block_an_import(self):
        slug, _ = self._plan(plan_id="pln_aaaaaaaaaaaa", title="Healthy plan")
        (self.root / slug / "record.json").write_text("{ not json", encoding="utf-8")

        source_root = Path(self._tmp.name) / "source"
        source = plan_store.PlanLibrary(source_root)
        source.create(_document(plan_id="pln_bbbbbbbbbbbb", title="Incoming plan"))
        bundle = str(Path(self._tmp.name) / "b.json")
        with contextlib.redirect_stdout(io.StringIO()):
            plan_coordinator.main(["--library", str(source_root), "export",
                                   source.slugs()[0], "--output", bundle])

        code, out, err = self.run_command("import", "--bundle", bundle)
        self.assertEqual(code, 0, err)
        self.assertIn("pln_bbbbbbbbbbbb", out)
        # And the incompleteness of the collision check is disclosed, not swallowed.
        self.assertIn("that check is incomplete", err)
        self.assertIn(slug, err)

    def test_a_healthy_library_imports_without_any_warning(self):
        source_root = Path(self._tmp.name) / "source"
        source = plan_store.PlanLibrary(source_root)
        source.create(_document(plan_id="pln_bbbbbbbbbbbb", title="Incoming plan"))
        bundle = str(Path(self._tmp.name) / "b.json")
        with contextlib.redirect_stdout(io.StringIO()):
            plan_coordinator.main(["--library", str(source_root), "export",
                                   source.slugs()[0], "--output", bundle])
        code, _, err = self.run_command("import", "--bundle", bundle)
        self.assertEqual(code, 0)
        self.assertNotIn("incomplete", err)

class Enumeration(unittest.TestCase):
    def test_the_depths_offered_match_the_documented_set(self):
        self.assertEqual(set(plan_coordinator.DEPTHS), {"light", "standard", "thorough"})

    def test_every_status_the_surface_can_report_is_in_the_enumeration(self):
        for status in ("draft", "awaiting-approval", "awaiting-review", "review-recorded",
                       "sealed", "active", "complete", "retired", "abandoned"):
            self.assertIn(status, plan_store.STATUSES)


if __name__ == "__main__":
    unittest.main()
