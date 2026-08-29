#!/usr/bin/env python3
"""Tests for project_manager — the command surface over the plan library.

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

import plan_contract
import project_manager
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
            code = project_manager.main(["--library", str(self.root), *argv])
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
        self.assertTrue((self.root / slug / project_manager._PREVIEW_FILENAME).exists())

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
        self.assertIn("present the full revision", self.run_command("resume", slug)[1])

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
            code = project_manager.main(["--library", str(synced), "doctor"])
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

    def _packet_digest(self, slug):
        """The digest of the packet the coordinator would actually cut — never the plan digest.

        The two were interchangeable while nothing verified the receipt; now that `review record`
        re-renders and compares, a receipt has to name the packet it really read."""
        import plan_projection as _pp
        return project_manager.core.digest(
            _pp.render_plan(self.lib.head(slug), self.lib.read_record(slug)).encode("utf-8"))

    def _covering_lenses(self, depth="standard"):
        """Every lens the approved depth requires — the seal refuses anything short of it."""
        return project_manager.required_lenses(depth, project_manager.installed_lenses())

    def _to_reviewed(self, findings=(), depth="standard", lenses=None, **over):
        slug, document = self._plan(**over)
        self.run_command("preview", slug)
        self.assertEqual(self.run_command("approve", slug, "--depth", depth, "--operator-decision", "yes, at that depth")[0], 0)
        # Every lens in this fixture ran at the effort its depth promises. `review record` refuses a
        # panel that does not say what it delivered (StarshipSuperjam/engine-template#1067), and the
        # bare level applies to every lens named in the record.
        argv = ["review", "record", slug, "--packet-digest", self._packet_digest(slug),
                "--delivered-effort", "high"]
        for lens in (lenses if lenses is not None else self._covering_lenses(depth)):
            argv += ["--lens", lens]
        if findings:
            argv += ["--findings", self._findings(*findings)]
        self.assertEqual(self.run_command(*argv)[0], 0)
        if not findings:
            # The seal's findings-presentation gate. With no findings to disposition the panel's
            # outcome can be presented immediately, so a plan "walked to the edge of a seal" is one
            # that has been. Cases that DO carry findings present after dispositioning them.
            self.assertEqual(self.present(slug)[0], 0)
        return slug, document

    def present(self, slug, decision="I read every finding and its disposition"):
        return self.run_command("present-findings", slug, "--operator-decision", decision)


class Approval(_Surface):
    def test_approval_is_refused_before_the_plan_is_presented(self):
        slug, _ = self._plan()
        code, _, err = self.run_command("approve", slug, "--depth", "standard", "--operator-decision", "yes, at that depth")
        self.assertEqual(code, 2)
        self.assertIn("has not been presented", err)

    def test_approval_binds_the_revision_and_its_digest(self):
        slug, _ = self._plan()
        self.run_command("preview", slug)
        code, out, _ = self.run_command("approve", slug, "--depth", "thorough", "--operator-decision", "yes, at that depth")
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
        self.run_command("approve", slug, "--depth", "standard", "--operator-decision", "yes, at that depth")
        code, out, err = self.run_command("review", "packet", slug)
        self.assertEqual(code, 0)
        self.assertIn("Packet digest: sha256:", out)
        self.assertIn(self.lib.read_record(slug)["current"]["plan_digest"], out)
        self.assertIn("packet digest:", err)

    def test_a_packet_is_refused_on_a_stale_approval(self):
        slug, _ = self._plan()
        self.run_command("preview", slug)
        self.run_command("approve", slug, "--depth", "standard", "--operator-decision", "yes, at that depth")
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
        code, out, _ = self.run_command("seal", slug, "--operator-decision", "seal it")
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
        self.run_command("approve", slug, "--depth", "standard", "--operator-decision", "yes, at that depth")
        digest = self.lib.read_record(slug)["current"]["plan_digest"]
        self.run_command("review", "record", slug, "--lens", "architecture", "--packet-digest", digest)
        code, _, err = self.run_command("seal", slug, "--operator-decision", "seal it")
        self.assertEqual(code, 1)
        self.assertIn("unresolved", err)
        self.assertIsNone(self.lib.read_record(slug)["seal"])

    def test_an_unresolved_assumption_refuses_the_seal(self):
        document = _document()
        document["build_plan"]["assumptions"] = [{"claim": "The disk is durable.", "status": "unresolved"}]
        slug = self.lib.create(document)
        self.run_command("preview", slug)
        self.run_command("approve", slug, "--depth", "standard", "--operator-decision", "yes, at that depth")
        digest = self.lib.read_record(slug)["current"]["plan_digest"]
        self.run_command("review", "record", slug, "--lens", "architecture", "--packet-digest", digest)
        code, _, err = self.run_command("seal", slug, "--operator-decision", "seal it")
        self.assertEqual(code, 1)
        self.assertIn("The disk is durable.", err)

    def test_a_missing_review_refuses_the_seal(self):
        slug, _ = self._plan()
        self.run_command("preview", slug)
        self.run_command("approve", slug, "--depth", "standard", "--operator-decision", "yes, at that depth")
        code, _, err = self.run_command("seal", slug, "--operator-decision", "seal it")
        self.assertEqual(code, 1)
        self.assertIn("no cold plan review", err)

    def test_a_missing_approval_refuses_the_seal(self):
        slug, _ = self._plan()
        code, _, err = self.run_command("seal", slug, "--operator-decision", "seal it")
        self.assertEqual(code, 1)
        self.assertIn("has not been approved", err)

    def test_an_undispositioned_finding_refuses_the_seal(self):
        slug, _ = self._to_reviewed(findings=(self._blocking(),))
        code, _, err = self.run_command("seal", slug, "--operator-decision", "seal it")
        self.assertEqual(code, 1)
        self.assertIn("no disposition", err)
        self.assertIn("ARCH-B1", err)

    def test_a_stale_approval_refuses_the_seal(self):
        slug, _ = self._plan()
        self.run_command("preview", slug)
        self.run_command("approve", slug, "--depth", "standard", "--operator-decision", "yes, at that depth")
        self.lib.append_revision(slug, _document(revision=2), expected_revision=1)
        code, _, err = self.run_command("seal", slug, "--operator-decision", "seal it")
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
        code, _, err = self.run_command("seal", slug, "--operator-decision", "seal it")
        self.assertEqual(code, 1)
        self.assertIn("only build-plan.v2 can be sealed", err)

    def test_all_refusals_are_reported_together(self):
        slug, _ = self._plan()
        code, _, err = self.run_command("seal", slug, "--operator-decision", "seal it")
        self.assertEqual(code, 1)
        self.assertIn("has not been approved", err)
        # With no approval there is no depth, so no roster to demand — the review refusal is keyed on
        # the approved depth's roster now, and reporting a coverage gap for a depth nobody chose would
        # be noise. Approve, and the missing review is named alongside everything else still in the way.
        self.run_command("preview", slug)
        self.run_command("approve", slug, "--depth", "standard", "--operator-decision", "yes, at that depth")
        code, _, err = self.run_command("seal", slug, "--operator-decision", "seal it")
        self.assertEqual(code, 1)
        self.assertIn("no cold plan review has been recorded", err)
        for lens in self._covering_lenses():
            self.assertIn(lens, err)


class SealIsTerminal(_Governed):
    def test_a_blocking_finding_leaves_a_resumable_draft_and_no_seal_artifact(self):
        # There is deliberately no sealed-but-failed state.
        slug, _ = self._to_reviewed(findings=({"id": "RISK-B1", "lens": "risk-governance",
                                               "severity": "blocking",
                                               "summary": "The library is the only copy."},))
        self.assertEqual(self.run_command("seal", slug, "--operator-decision", "seal it")[0], 1)
        record = self.lib.read_record(slug)
        self.assertIsNone(record["seal"])
        self.assertEqual(plan_store.derived_status(record), "review-recorded")
        # Still editable, still resumable — the plan is not stuck anywhere.
        self.lib.append_revision(slug, _document(revision=2), expected_revision=1)
        self.assertIn("disposition 1 outstanding finding", self.run_command("resume", slug)[1])

    def test_sealing_twice_is_refused(self):
        slug, _ = self._to_reviewed()
        self.assertEqual(self.run_command("seal", slug, "--operator-decision", "seal it")[0], 0)
        code, _, err = self.run_command("seal", slug, "--operator-decision", "seal it")
        self.assertEqual(code, 1)
        self.assertIn("already sealed", err)
        self.assertIn("clone", err)

    def test_a_sealed_plan_cannot_be_approved_again(self):
        slug, _ = self._to_reviewed()
        self.run_command("seal", slug, "--operator-decision", "seal it")
        self.run_command("preview", slug)
        code, _, err = self.run_command("approve", slug, "--depth", "quick", "--operator-decision", "yes, at that depth")
        self.assertEqual(code, 2)
        self.assertIn("terminal", err)

    def test_a_seal_cannot_be_reopened(self):
        slug, _ = self._to_reviewed()
        self.run_command("seal", slug, "--operator-decision", "seal it")
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
        code, _, err = self.run_command("seal", slug, "--operator-decision", "seal it")
        self.assertEqual(code, 2)
        self.assertIn("delta needs one proportional judgment", err)
        self.assertIn("diff", err)
        self.assertIsNone(self.lib.read_record(slug)["seal"])

    def test_the_judgment_seals_and_the_delta_is_recorded_for_disclosure(self):
        slug = self._reviewed_then_revised()
        code, out, _ = self.run_command("seal", slug, "--delta-judgment", "scoped",
                                        "--delta-rationale", "One failure mode added; nothing else moved.", "--operator-decision", "seal it")
        self.assertEqual(code, 0)
        seal = self.lib.read_record(slug)["seal"]
        self.assertNotEqual(seal["reviewed_digest"], seal["sealed_digest"])
        self.assertEqual(seal["delta_judgment"], "scoped")
        self.assertIn("One failure mode added", seal["delta_rationale"])
        self.assertIn("must disclose", out)

    def test_a_scoped_judgment_needs_a_rationale(self):
        slug = self._reviewed_then_revised()
        code, _, err = self.run_command("seal", slug, "--delta-judgment", "scoped", "--operator-decision", "seal it")
        self.assertEqual(code, 2)
        self.assertIn("needs a rationale", err)

    def test_an_unchanged_plan_needs_no_judgment(self):
        slug, _ = self._to_reviewed()
        code, out, _ = self.run_command("seal", slug, "--operator-decision", "seal it")
        self.assertEqual(code, 0)
        self.assertIn("unchanged since review", out)


class Dispositions(_Governed):
    def test_disposing_a_finding_clears_it_and_reports_what_is_left(self):
        slug, _ = self._to_reviewed(findings=(
            {"id": "A1", "lens": "architecture", "severity": "serious", "summary": "One."},
            {"id": "A2", "lens": "architecture", "severity": "nit", "summary": "Two."}))
        code, out, _ = self.run_command("finding", "dispose", slug, "--id", "A1",
                                        "--disposition", "accepted-fixed",
                                        "--rationale", "Folded into revision 2.",
                                        "--does-not-block-this-pr")
        self.assertEqual(code, 0)
        self.assertIn("outstanding: A2", out)
        self.run_command("finding", "dispose", slug, "--id", "A2",
                         "--disposition", "rejected", "--rationale", "Style preference.",
                         "--does-not-block-this-pr")
        self.assertIn("outstanding: none", self.run_command(
            "finding", "dispose", slug, "--id", "A1", "--disposition", "accepted-fixed",
            "--rationale", "Folded into revision 2.", "--does-not-block-this-pr")[1])

    def test_the_blocking_choice_has_no_default_and_must_be_stated(self):
        """Driven through the real parser, both arms. The Build side's `finding record` learned this the
        expensive way: an omitted flag resolving to False is a submission gate failing toward permitting,
        and the falsiness check written to replace it then broke `--does-not-block-this-pr`, whose const
        is itself falsy. Same shape, same two arms, tested here before it could repeat."""
        slug, _ = self._to_reviewed(findings=(
            {"id": "A1", "lens": "architecture", "severity": "serious", "summary": "One."},))
        code, _, err = self.run_command("finding", "dispose", slug, "--id", "A1",
                                        "--disposition", "accepted-fixed", "--rationale", "Fixed.")
        self.assertEqual(code, 2)
        self.assertIn("--does-not-block-this-pr", err)
        self.assertEqual(self.run_command(
            "finding", "dispose", slug, "--id", "A1", "--disposition", "accepted-fixed",
            "--rationale", "Fixed.", "--does-not-block-this-pr")[0], 0)
        self.assertFalse(self.lib.read_record(slug)["plan_review"]["findings"][0]["blocks_this_pr"])
        self.assertEqual(self.run_command(
            "finding", "dispose", slug, "--id", "A1", "--disposition", "accepted-tracked",
            "--rationale", "Still open.", "--blocks-this-pr")[0], 0)
        self.assertTrue(self.lib.read_record(slug)["plan_review"]["findings"][0]["blocks_this_pr"])

    def test_an_unknown_finding_id_lists_the_real_ones(self):
        slug, _ = self._to_reviewed(findings=({"id": "A1", "lens": "architecture",
                                               "severity": "nit", "summary": "One."},))
        code, _, err = self.run_command("finding", "dispose", slug, "--id", "NOPE",
                                        "--disposition", "rejected", "--rationale", "n/a")
        self.assertEqual(code, 2)
        self.assertIn("A1", err)

    def test_a_review_cannot_be_recorded_onto_a_sealed_plan(self):
        """The sibling hole to the one below: at a depth needing no lenses, a plan seals with no review
        at all — so "a review already exists" guards nothing. Without the seal check a whole review,
        findings and pre-set dispositions included, could be written onto a sealed plan and would reach
        the merge surface looking like something read before the plan was locked."""
        slug, _ = self._plan()
        self.run_command("preview", slug)
        self.assertEqual(self.run_command("approve", slug, "--depth", "quick", "--operator-decision", "yes, at that depth")[0], 0)
        self.assertEqual(self.run_command("seal", slug, "--operator-decision", "seal it")[0], 0)
        self.assertIsNone(self.lib.read_record(slug).get("plan_review"))
        code, _, err = self.run_command("review", "record", slug, "--packet-digest",
                                        self._packet_digest(slug), "--lens", "architecture")
        self.assertEqual(code, 2)
        self.assertIn("sealed", err)
        self.assertIsNone(self.lib.read_record(slug).get("plan_review"))

    def test_a_reviewed_plan_cannot_be_re_approved_at_another_depth(self):
        """Downgrading after review would leave the review attached while the seal asked a smaller
        question of it: at quick the roster is empty, so a one-of-four-lens review sails through and the
        pull request then tells the operator a cold panel read the plan. The review cannot be dropped to
        make room either — exactly one per plan is what stops the re-review spiral — so depth holds."""
        slug, _ = self._to_reviewed(depth="standard")
        code, _, err = self.run_command("approve", slug, "--depth", "quick", "--operator-decision", "yes, at that depth")
        self.assertEqual(code, 2)
        self.assertIn("cannot be re-approved", err)
        self.assertEqual(self.lib.read_record(slug)["approval"]["depth"], "standard")
        # Re-approving at the SAME depth stays legal: nothing about the question changed.
        self.assertEqual(self.run_command("approve", slug, "--depth", "standard", "--operator-decision", "yes, at that depth")[0], 0)

    def test_a_seal_freezes_the_dispositions_the_pull_request_will_publish(self):
        """The Build reads this review live from the record, so an editable record is an editable PR.

        A blocking finding left honestly blocking at seal is a disagreement the operator meets at merge.
        If it could be turned into "rejected, no issue" afterwards, reading live would relocate the
        silent drop rather than close it — the immunity the panel move claims would be worth nothing.
        """
        slug, _ = self._to_reviewed(findings=({"id": "A1", "lens": "architecture",
                                               "severity": "blocking", "summary": "One."},))
        self.assertEqual(self.run_command(
            "finding", "dispose", slug, "--id", "A1", "--disposition", "accepted-tracked",
            "--rationale", "Carried to the successor plan.", "--blocks-this-pr")[0], 0)
        self.assertEqual(self.present(slug)[0], 0)
        self.assertEqual(self.run_command("seal", slug, "--operator-decision", "seal it")[0], 0)
        code, _, err = self.run_command(
            "finding", "dispose", slug, "--id", "A1", "--disposition", "rejected",
            "--rationale", "On reflection, no.", "--does-not-block-this-pr",
            "--operator-summary", "No real issue here.")
        self.assertEqual(code, 2)
        self.assertIn("sealed", err)
        frozen = self.lib.read_record(slug)["plan_review"]["findings"][0]
        self.assertEqual(frozen["disposition"], "accepted-tracked")
        self.assertTrue(frozen["blocks_this_pr"])

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
        self.run_command("seal", slug, "--operator-decision", "seal it")
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
        self.run_command("approve", slug, "--depth", "standard", "--operator-decision", "yes, at that depth")
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
        self.run_command("seal", slug, "--operator-decision", "seal it")
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
            code = project_manager.main(["--library", str(root), "import", "--bundle", bundle])
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
        bundle["bundle_digest"] = project_manager.core.digest(
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
        bundle["bundle_digest"] = project_manager.core.digest(
            {"record": bundle["record"], "revisions": bundle["revisions"]})
        Path(path).write_text(json.dumps(bundle), encoding="utf-8")
        return path

    def _import_elsewhere(self, path):
        root = Path(self._tmp.name) / "target"
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = project_manager.main(["--library", str(root), "import", "--bundle", path])
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
        self.run_command("approve", slug, "--depth", "standard", "--operator-decision", "yes, at that depth")
        digest = self._packet_digest(slug)
        # Both sessions read a record with no review; A records first, B must still be refused.
        self.assertEqual(self.run_command("review", "record", slug, "--lens", "architecture",
                                          "--packet-digest", digest,
                                          "--delivered-effort", "medium")[0], 0)

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
        with mock.patch.object(project_manager.core, "durable_fsync", return_value=False):
            with self.assertRaisesRegex(plan_store.PlanStoreError, "flush it to stable storage"):
                self.lib.append_revision(slug, _document(revision=2), expected_revision=1)
        self.assertEqual((self.root / slug / "record.json").read_bytes(), before,
                         "a failed durable write still changed the record")

    def test_a_declined_directory_flush_is_not_fatal(self):
        # Some filesystems legitimately refuse to fsync a directory fd; the file is already durable.
        with mock.patch.object(project_manager.core, "fsync_dir", return_value=False):
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

    def _plan_doc(self, program_id, plan_id, title, *obligations, predecessor=None):
        document = _document(plan_id=plan_id, title=title)
        program = {"program_id": program_id}
        if obligations:
            program["carried_obligations"] = list(obligations)
        if predecessor:
            program["predecessor_plan_id"] = predecessor
        document["program"] = program
        self.lib.create(document)

    def test_insert_places_a_plan_before_an_existing_child_and_says_which_edges_moved(self):
        program_id = self._program_with_child()
        self._plan_doc(program_id, "pln_bbbbbbbbbbbb", "PR B",
                       self._obligation("OB-1", "Cut over.", "satisfied"),
                       predecessor="pln_aaaaaaaaaaaa")
        self.assertEqual(self.run_command("program", "add", program_id, "pln_bbbbbbbbbbbb",
                                          "--after", "pln_aaaaaaaaaaaa")[0], 0)
        # X re-declares OB-1 as carried, so it answers for A; B satisfies OB-1, so it answers for X.
        self._plan_doc(program_id, "pln_cccccccccccc", "PR X",
                       self._obligation("OB-1", "Still carried, now by X."))
        code, out, err = self.run_command("program", "insert", program_id, "pln_cccccccccccc",
                                          "--before", "pln_bbbbbbbbbbbb")
        self.assertEqual(code, 0, err)
        self.assertIn("inserted pln_cccccccccccc as child 2", out)
        self.assertIn("pln_aaaaaaaaaaaa -> pln_cccccccccccc -> pln_bbbbbbbbbbbb", out)
        self.assertIn("Nothing was renumbered", out)
        # The half an operator does not picture: the displaced child now answers for the newcomer.
        self.assertIn("pln_bbbbbbbbbbbb now answers for 1 obligation(s)", out)
        shown = self.run_command("program", "show", program_id)[1]
        self.assertLess(shown.index("PR X"), shown.index("PR B"))

    def test_insert_refuses_at_the_command_line_when_the_displaced_child_cannot_answer(self):
        program_id = self._program_with_child()
        self._plan_doc(program_id, "pln_bbbbbbbbbbbb", "PR B",
                       self._obligation("OB-1", "Cut over.", "satisfied"),
                       predecessor="pln_aaaaaaaaaaaa")
        self.assertEqual(self.run_command("program", "add", program_id, "pln_bbbbbbbbbbbb",
                                          "--after", "pln_aaaaaaaaaaaa")[0], 0)
        self._plan_doc(program_id, "pln_cccccccccccc", "PR X",
                       self._obligation("OB-1", "Still carried, now by X."),
                       self._obligation("OB-NEW", "Something B has never heard of."))
        code, _, err = self.run_command("program", "insert", program_id, "pln_cccccccccccc",
                                        "--before", "pln_bbbbbbbbbbbb")
        self.assertEqual(code, 2)
        self.assertIn("OB-NEW", err)
        self.assertIn("Revise pln_bbbbbbbbbbbb", err)

    def _superseded_setup(self):
        """A -> B, where B is sealed and about to be replaced. Returns the program id."""
        program_id = self._program_with_child()
        self._plan_doc(program_id, "pln_bbbbbbbbbbbb", "PR B",
                       self._obligation("OB-1", "Cut over.", "satisfied"),
                       predecessor="pln_aaaaaaaaaaaa")
        self.assertEqual(self.run_command("program", "add", program_id, "pln_bbbbbbbbbbbb",
                                          "--after", "pln_aaaaaaaaaaaa")[0], 0)
        return program_id

    def test_supersede_retires_the_plan_first_and_then_marks_the_record(self):
        program_id = self._superseded_setup()
        self._plan_doc(program_id, "pln_cccccccccccc", "PR B, second attempt",
                       self._obligation("OB-1", "Cut over, properly this time.", "satisfied"),
                       predecessor="pln_aaaaaaaaaaaa")
        code, out, err = self.run_command("program", "supersede", program_id, "pln_bbbbbbbbbbbb",
                                          "--with", "pln_cccccccccccc",
                                          "--reason", "the cut-over shape was wrong")
        self.assertEqual(code, 0, err)
        self.assertIn("retired pln_bbbbbbbbbbbb", out)
        self.assertIn("pln_cccccccccccc supersedes pln_bbbbbbbbbbbb", out)
        # The plan is retired through the ordinary close path, so `show` reports it that way.
        self.assertIn("retired", self.run_command("show", "pln_bbbbbbbbbbbb")[1])
        rendered = self.run_command("program", "show", program_id)[1]
        self.assertIn("superseded by `pln_cccccccccccc`", rendered)
        self.assertIn("PR B, second attempt", rendered)
        self.assertIn("PR B", rendered)          # nothing was deleted to make room

    def test_supersede_refuses_before_it_retires_anything(self):
        """A refusal must not leave the replaced plan out of play for a supersession that never landed."""
        program_id = self._superseded_setup()
        self._plan_doc(program_id, "pln_cccccccccccc", "A replacement that forgot",
                       predecessor="pln_aaaaaaaaaaaa")     # says nothing about OB-1
        code, _, err = self.run_command("program", "supersede", program_id, "pln_bbbbbbbbbbbb",
                                        "--with", "pln_cccccccccccc", "--reason", "no")
        self.assertEqual(code, 2)
        self.assertIn("OB-1", err)
        self.assertNotIn("retired", self.run_command("show", "pln_bbbbbbbbbbbb")[1])

    def test_clone_supersedes_sources_obligations_from_the_predecessor(self):
        """Not from the plan being replaced: its own claims describe work that never landed."""
        program_id = self._superseded_setup()
        code, out, err = self.run_command("clone", "pln_bbbbbbbbbbbb", "--supersedes",
                                          "pln_bbbbbbbbbbbb", "--reason", "the shape was wrong")
        self.assertEqual(code, 0, err)
        self.assertIn("Pre-filled to supersede pln_bbbbbbbbbbbb", out)
        self.assertIn("OB-1", out)
        clone_id = out.split("into ")[1].split()[0]
        document = self.lib.head(self.lib.resolve(clone_id))
        program = document["program"]
        self.assertEqual(program["program_id"], program_id)
        self.assertEqual(program["predecessor_plan_id"], "pln_aaaaaaaaaaaa")
        # B SATISFIED OB-1. The clone re-declares it as CARRIED, because B never landed.
        self.assertEqual([(o["id"], o["state"]) for o in program["carried_obligations"]],
                         [("OB-1", "carried")])
        record = self.lib.read_record(self.lib.resolve(clone_id))
        for evidence in ("approval", "plan_review", "seal"):
            self.assertIsNone(record.get(evidence), evidence)

    def test_clone_supersedes_refuses_a_plan_in_no_program(self):
        self._plan_doc_standalone = _document(plan_id="pln_dddddddddddd", title="Standalone")
        self.lib.create(self._plan_doc_standalone)
        code, _, err = self.run_command("clone", "pln_dddddddddddd", "--supersedes",
                                        "pln_dddddddddddd", "--reason", "why")
        self.assertEqual(code, 2)
        self.assertIn("is not a child of any program", err)

    def test_the_verb_writes_no_position_for_either_door(self):
        program_id = self._program_with_child()
        record = json.loads((self.lib.root / "programs" / next(
            d.name for d in (self.lib.root / "programs").iterdir()) / "record.json")
            .read_text(encoding="utf-8"))
        self.assertTrue(all("position" not in child for child in record["children"]))


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
        self.run_command("approve", slug, "--depth", "standard", "--operator-decision", "yes, at that depth")
        findings = Path(self._tmp.name) / "bad-findings.json"
        findings.write_text(json.dumps([{"id": "A1", "lens": "architecture",
                                         "severity": "major", "summary": "s"}]), encoding="utf-8")
        code, _, err = self.run_command("review", "record", slug, "--lens", "architecture",
                                        "--packet-digest", self._packet_digest(slug),
                                        "--findings", str(findings))
        self.assertEqual(code, 2)
        self.assertIn("severity", err)
        self.assertIn("blocking", err)
        self.assertNotIn("is not valid under any of the given schemas", err)

    def test_a_malformed_digest_names_the_digest_field(self):
        slug, _ = self._plan()
        self.run_command("preview", slug)
        self.run_command("approve", slug, "--depth", "standard", "--operator-decision", "yes, at that depth")
        code, _, err = self.run_command("review", "record", slug, "--lens", "architecture",
                                        "--packet-digest", "sha256:abc")
        self.assertEqual(code, 2)
        # The receipt no longer has to be malformed to be caught: it has to be WRONG. The refusal names
        # the digest given and the digest the approved revision actually renders to.
        self.assertIn("sha256:abc", err)
        self.assertIn(self._packet_digest(slug), err)

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
        # `show` derives its refusals from seal_refusals, exactly as `seal` does, so it names the gate
        # in the gate's own words rather than gesturing at "the gates" as a category.
        slug, _ = self._plan()
        out = self.run_command("show", slug)[1]
        self.assertIn("not sealable yet", out)
        self.assertIn("has not been approved at any revision", out)

    def test_resume_on_a_sealed_plan_states_the_bind_command(self):
        # The counterpart of the honesty this case used to enforce: the handoff DOES ship now, so the
        # next step names the exact command rather than steering the operator to clone.
        slug, document = self._to_reviewed()
        self.run_command("seal", slug, "--operator-decision", "seal it")
        out = self.run_command("resume", slug)[1]
        self.assertIn("plan bind --plan " + document["plan_id"], out)
        self.assertIn("--repository <owner/repo> --pr <number>", out)
        self.assertNotIn("not wired up yet", out)
        self.assertIn("clone", out)   # still the only way past a terminal seal

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
            project_manager.main(["--library", str(attacker_root), "export",
                                   attacker.slugs()[0], "--output", bundle])
        payload = json.loads(Path(bundle).read_text(encoding="utf-8"))
        payload["record"]["slug"] = victim_slug          # the only edit
        payload["bundle_digest"] = project_manager.core.digest(
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
        source = Path(project_manager.__file__).read_text(encoding="utf-8")
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
        self.assertIn("not sealable yet", out)
        self.assertIn("has not been approved at any revision", out)
        self.assertIn("project_manager.py preview", out)


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
            project_manager.main(["--library", str(source_root), "export",
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
            project_manager.main(["--library", str(source_root), "export",
                                   source.slugs()[0], "--output", bundle])
        code, _, err = self.run_command("import", "--bundle", bundle)
        self.assertEqual(code, 0)
        self.assertNotIn("incomplete", err)

class ThePanelMovedHere(_Governed):
    """The enforcement that came across with the panel, not just the panel."""

    def test_a_one_lens_review_at_thorough_is_refused_at_seal_naming_the_missing_lenses(self):
        # The hole this closes: "a sealed plan is reviewed by definition" was an assumption. A single lens
        # could seal a plan approved at thorough, and nothing said otherwise.
        slug, _ = self._to_reviewed(depth="thorough", lenses=["architecture"])
        code, _, err = self.run_command("seal", slug, "--operator-decision", "seal it")
        self.assertEqual(code, 1)
        self.assertIn("missing", err)
        for lens in self._covering_lenses("thorough"):
            if lens != "architecture":
                self.assertIn(lens, err)

    def test_a_covering_review_seals(self):
        slug, _ = self._to_reviewed(depth="thorough")
        code, out, err = self.run_command("seal", slug, "--operator-decision", "seal it")
        self.assertEqual(code, 0, err)
        self.assertIn("sealed", out)

    def test_quick_needs_no_cold_lenses_and_still_seals(self):
        # At quick the operator's own read IS the review, by their choice at approval. Demanding a recorded
        # review anyway would make the depth unusable; the demand is keyed on the roster the depth requires.
        slug, _ = self._plan()
        self.run_command("preview", slug)
        self.assertEqual(self.run_command("approve", slug, "--depth", "quick", "--operator-decision", "yes, at that depth")[0], 0)
        code, _, err = self.run_command("seal", slug, "--operator-decision", "seal it")
        self.assertEqual(code, 0, err)

    def test_a_receipt_naming_a_packet_nobody_cut_is_refused(self):
        slug, _ = self._plan()
        self.run_command("preview", slug)
        self.run_command("approve", slug, "--depth", "standard", "--operator-decision", "yes, at that depth")
        code, _, err = self.run_command("review", "record", slug, "--lens", "architecture",
                                        "--packet-digest", "sha256:" + "4" * 64)
        self.assertEqual(code, 2)
        self.assertIn("renders to", err)
        self.assertIn(self._packet_digest(slug), err)

    def test_the_depth_offer_is_computed_from_the_installed_roster_with_resolved_effort(self):
        roster = [{"lens": "architecture"}, {"lens": "feasibility"},
                  {"lens": "product-intent"}, {"lens": "risk-governance"}]
        efforts = {"quick": None, "standard": "medium", "thorough": "high"}
        self.assertEqual(project_manager.available_depths(roster, efforts=efforts),
                         ["quick", "standard", "thorough"])
        # A depth that buys nothing is suppressed: with no reviewers every heavier depth runs what quick
        # runs, so only the floor is offered (the 763/677 protection, moved with the consent surface).
        self.assertEqual(project_manager.available_depths([], efforts=efforts), ["quick"])
        # ...and with equal lens-sets AND equal effort, the heavier depth collapses too.
        flat = {"quick": None, "standard": "medium", "thorough": "medium"}
        self.assertEqual(project_manager.available_depths(roster, efforts=flat), ["quick", "standard"])

    def test_a_depth_that_buys_nothing_cannot_be_approved_either(self):
        # Suppressing it from the offer is not enough if it can still be typed: consent spent on nothing
        # is the failure, wherever it is spent.
        slug, _ = self._plan()
        self.run_command("preview", slug)
        with mock.patch.object(project_manager, "installed_lenses", return_value=[]):
            code, _, err = self.run_command("approve", slug, "--depth", "thorough", "--operator-decision", "yes, at that depth")
        self.assertEqual(code, 2)
        self.assertIn("not offered here", err)

    def test_the_depths_verb_names_the_lenses_the_seal_will_require(self):
        slug, _ = self._plan()
        self.run_command("preview", slug)
        out = self.run_command("depths", slug)[1]
        for lens in self._covering_lenses("standard"):
            self.assertIn(lens, out)
        self.assertIn("only those that add coverage or effort", out)

    def test_a_blocking_finding_left_unblocking_owes_an_operator_summary(self):
        slug, _ = self._to_reviewed(findings=({"id": "RISK-1", "lens": "risk-governance",
                                               "severity": "blocking",
                                               "summary": "internal reviewer detail"},))
        code, _, err = self.run_command("finding", "dispose", slug, "--id", "RISK-1",
                                        "--disposition", "accepted-tracked", "--rationale", "tracked",
                                        "--does-not-block-this-pr")
        self.assertEqual(code, 2)
        self.assertIn("--operator-summary", err)
        code, _, err = self.run_command("finding", "dispose", slug, "--id", "RISK-1",
                                        "--disposition", "accepted-tracked", "--rationale", "tracked",
                                        "--does-not-block-this-pr",
                                        "--operator-summary", "A residual the operator must weigh.")
        self.assertEqual(code, 0, err)
        finding = self.lib.read_record(slug)["plan_review"]["findings"][0]
        self.assertEqual(finding["operator_summary"], "A residual the operator must weigh.")
        self.assertFalse(finding["blocks_this_pr"])

    def test_a_finding_left_blocking_is_recorded_as_blocking_and_holds_the_seal(self):
        slug, _ = self._to_reviewed(findings=({"id": "RISK-2", "lens": "risk-governance",
                                               "severity": "blocking", "summary": "s"},))
        code, _, err = self.run_command("finding", "dispose", slug, "--id", "RISK-2",
                                        "--disposition", "escalated", "--rationale", "operator's call",
                                        "--blocks-this-pr", "--operator-summary", "Still blocking.")
        self.assertEqual(code, 0, err)
        self.assertTrue(self.lib.read_record(slug)["plan_review"]["findings"][0]["blocks_this_pr"])


class ImportingANativePlan(_Surface):
    """Native-plan intake: groundwork, not bypass and not restart.

    An accepted Claude or Codex plan lands as an unapproved draft revision 1 carrying the text
    verbatim. Nothing interprets it, decomposes it, or writes deliberation prose on its behalf — the
    four things an import cannot know are recorded as unresolved decisions, which the seal refuses
    while any remain. So the import moves a plan onto the shelf and moves nobody closer to building
    it, which is what makes it safe to run from a hook on an operator's ordinary keystroke.
    """

    NATIVE = "# Cache the widgets\n\nThey are slow, so cache them.\n"

    def _import(self, text=None, provenance="Accepted Claude Code plan, imported at plan-exit."):
        return project_manager.import_native_plan(text if text is not None else self.NATIVE,
                                                   provenance=provenance, library=self.lib)

    def test_the_native_text_is_kept_verbatim_as_the_raw_intent(self):
        # The gap between what was said and what anyone made of it is the thing a reviewer needs, so
        # the text is never tidied on the way in.
        document = self.lib.head(self._import()["slug"])
        self.assertEqual(document["intent"]["raw"], self.NATIVE)

    def test_nothing_is_interpreted_decomposed_or_deliberated(self):
        document = self.lib.head(self._import()["slug"])
        self.assertEqual(document["build_plan"],
                         {"schema_version": "build-plan.imported", "work_items": []})
        self.assertEqual(document["deliberation"]["problem_frame"], "")
        self.assertEqual(document["deliberation"]["case_against"], "")
        self.assertGreaterEqual(len(document["deliberation"]["unresolved_decisions"]), 4)

    def test_an_import_lands_unapproved_unreviewed_and_unsealed(self):
        record = self.lib.read_record(self._import()["slug"])
        self.assertIsNone(record["approval"])
        self.assertIsNone(record["plan_review"])
        self.assertIsNone(record["seal"])
        self.assertIsNone(record["build_binding"])

    def test_the_gaps_are_what_stands_between_an_import_and_a_seal(self):
        document = self.lib.head(self._import()["slug"])
        blockers = plan_contract.seal_blockers(document)
        self.assertTrue(any("unresolved" in b for b in blockers), blockers)
        self.assertTrue(any("imported native plan" in b for b in blockers), blockers)

    def test_the_title_is_lifted_from_the_text_never_invented(self):
        self.assertEqual(self._import()["title"], "Cache the widgets")
        self.assertEqual(self._import("no heading here, just prose\n")["title"],
                         "no heading here, just prose")
        self.assertEqual(self._import("   \n\n   \n.")["title"], ".")

    def test_an_empty_document_is_refused_rather_than_imported_as_a_blank_plan(self):
        for text in ("", "   \n\t\n", None):
            with self.assertRaises(project_manager.ProjectManagerError):
                self._import(text if text is not None else "")

    def test_every_import_mints_its_own_identity(self):
        first, second = self._import(), self._import()
        self.assertNotEqual(first["plan_id"], second["plan_id"])
        self.assertNotEqual(first["slug"], second["slug"])

    def test_the_readable_projection_renders_and_says_there_is_no_build_half(self):
        # The arrival report points the operator at `preview`, which renders through this same
        # function — so an import that cannot be rendered would hand them a crash as their next step.
        rendered = (self.lib.plan_dir(self._import()["slug"]) / plan_projection.PLAN_MD).read_text()
        self.assertIn("## The Build half", rendered)
        self.assertIn("an import decomposes nothing", rendered)
        self.assertIn("Not stated", rendered)                 # the deliberation gaps read AS gaps
        self.assertNotIn("## Execution graph", rendered)

    def test_the_arrival_report_names_the_plan_the_revision_and_the_next_command(self):
        report = project_manager.arrival_report(self._import())
        self.assertIn(self.lib.read_record(self.lib.slugs()[0])["plan_id"], report)
        self.assertIn("revision 1", report)
        self.assertIn("preview --plan", report)
        self.assertIn("no Build authority", report)
        self.assertIn("tell the operator", report.lower())     # unlike the directive it replaces

    def test_the_typed_verb_performs_the_identical_import(self):
        # The recovery path for envelope drift or a declined hook trust: an extra command, never a
        # lesser import. Same document shape, same status, same gaps.
        path = Path(self._tmp.name) / "native.md"
        path.write_text(self.NATIVE, encoding="utf-8")
        code, out, err = self.run_command("import-native", "--input", str(path),
                                          "--provenance", "Typed recovery path.")
        self.assertEqual(code, 0, err)
        self.assertIn("revision 1", out)
        self.assertIn("preview --plan", out)
        document = self.lib.head(self.lib.slugs()[0])
        self.assertEqual(document["intent"]["raw"], self.NATIVE)
        self.assertEqual(document["build_plan"]["schema_version"], "build-plan.imported")

    def test_an_import_records_where_it_came_from_on_the_document_and_the_record(self):
        arrival = self._import(provenance="Accepted Codex plan, imported from the typed envelope.")
        self.assertIn("Codex", self.lib.head(arrival["slug"])["intake"]["provenance"])
        self.assertIn("Codex", self.lib.read_record(arrival["slug"])["intake"]["provenance"])

    def test_an_imported_draft_can_be_revised_into_a_real_plan(self):
        # The whole point of importing rather than restarting: the coordinator continues from here.
        arrival = self._import()
        real = _document(plan_id=arrival["plan_id"], revision=2)
        self.lib.append_revision(arrival["slug"], real, expected_revision=1)
        head = self.lib.head(arrival["slug"])
        self.assertEqual(head["build_plan"]["schema_version"], "build-plan.v2")
        self.assertEqual(len(self.lib.read_record(arrival["slug"])["ledger"]), 2)


class DepthSelectsReviewersAndNothingElse(_Surface):
    """Depth never selects the plan's FORMAT and never selects its GRAPH TOPOLOGY.

    It sounds too obvious to test, which is why it is worth testing: the shortcut it forbids is real
    and would be quiet. Letting `quick` accept a thinner document, or fold a graph into a chain
    "since nobody is reviewing it anyway", would make how carefully a plan is read decide what the
    plan IS — and the plan is the thing a Build executes long after the reviewing is over. Depth is
    how much scrutiny the operator asked for; it is never a discount on the artifact.
    """

    def _approved_at(self, depth):
        slug, document = self._plan(plan_id=f"pln_{'0' * 11}{project_manager.DEPTH_ORDER.index(depth)}")
        self.run_command("preview", slug)
        self.assertEqual(self.run_command("approve", slug, "--depth", depth, "--operator-decision", "yes, at that depth")[0], 0)
        return slug, self.lib.head(slug), self.lib.read_record(slug)

    def test_the_document_and_its_payload_are_byte_identical_at_every_depth(self):
        shapes = {}
        for depth in project_manager.DEPTH_ORDER:
            _, document, record = self._approved_at(depth)
            payload = document["build_plan"]
            shapes[depth] = {
                "document_version": document["schema_version"],
                "payload_version": payload["schema_version"],
                "payload_digest": record["current"]["build_plan_digest"],
                "topology": sorted((item["id"], tuple(sorted(item.get("depends_on", []))))
                                   for item in payload["work_items"]),
                "parallelism": payload["parallelism"],
            }
        reference = shapes[project_manager.DEPTH_ORDER[0]]
        for depth, shape in shapes.items():
            self.assertEqual(shape, reference, f"{depth} changed the plan itself, not just its review")

    def test_the_approved_depth_is_the_only_thing_the_depth_choice_writes(self):
        # Stated as the complement of the test above: approval records the depth against the digest
        # being approved, and touches nothing else on the record.
        for depth in project_manager.DEPTH_ORDER:
            slug, _, record = self._approved_at(depth)
            self.assertEqual(record["approval"]["depth"], depth)
            self.assertEqual(record["approval"]["plan_digest"], record["current"]["plan_digest"])
            self.assertIsNone(record["plan_review"])
            self.assertIsNone(record["seal"])

    def test_a_deeper_depth_adds_reviewers_and_only_reviewers(self):
        roster = project_manager.installed_lenses()
        lighter = set(project_manager.required_lenses("quick", roster))
        deeper = set(project_manager.required_lenses("thorough", roster))
        self.assertTrue(lighter <= deeper, "a deeper depth must never drop a reviewer a lighter one ran")


class Enumeration(unittest.TestCase):
    def test_the_depths_offered_match_the_documented_set(self):
        self.assertEqual(set(project_manager.DEPTHS), {"quick", "standard", "thorough"})
        # ONE vocabulary across both coordinators, which is what lets a single consent cover both gates.
        import build_coordinator_review as bcr
        self.assertEqual(set(project_manager.DEPTHS), set(bcr.DEPTH_ORDER))

    def test_every_status_the_surface_can_report_is_in_the_enumeration(self):
        for status in ("draft", "awaiting-approval", "awaiting-review", "review-recorded",
                       "sealed", "active", "complete", "retired", "abandoned"):
            self.assertIn(status, plan_store.STATUSES)


class TheRetitle(unittest.TestCase):
    """The component is the Project Manager, and the old name survives only where it is HISTORY.

    A rename is only finished when you can prove where it stopped. This walks the whole repository for
    the three written forms of the old name and holds the survivors to a list that says, per entry, why
    that one is not a miss. The list is the point: without it a sweep is a claim, and a claim is what
    leaves a live component half-renamed."""

    ROOT = Path(__file__).resolve().parents[2]
    FORMS = ("Plan Coordinator", "plan_coordinator", "plan-coordinator")

    # path -> why the old name belongs there. Each of these is history, data, or generated.
    #
    # A NOTE ON WHAT THIS LIST IS FOR. Two schema files were once excused here on the ground that "the
    # data boundary must not move". That reason was wrong, and a wrong reason is worse than a missing
    # entry: what was asked to be held is schema IDENTIFIERS and stored records, and editing an English
    # description string moves neither. The strings were retitled and the entries removed. An exclusion
    # list earns its keep only while every reason on it is true — otherwise the next genuine miss hides
    # among the excuses.
    ALLOWED = {
        ".engine/tools/test_plan_dogfood.py":
            "the historical plan document itself — the real PR A plan's title, raw intent and decisions. "
            "History keeps the name it was written under",
        ".engine/tools/test_plan_program.py":
            "a program fixture named for the real program, same reason",
        ".engine/tools/test_plan_store.py":
            "a stored plan record's own title, same reason",
        ".engine/tools/test_plan_contract.py":
            "a plan_id fixture exercising the id validator, not a reference to the component",
        ".engine/tools/test_project_manager.py":
            "this exclusion list",
        ".engine/tools/project_manager.py":
            "the module docstring's WHY THIS NAME paragraph, which names the old title as the past and "
            "says where the retitle deliberately stops",
    }

    # Searched AROUND, not excused. These two are generated: their contents are whatever the last
    # regeneration wrote, so a hit in them is evidence about when a generator last ran and nothing about
    # the rename. Keeping them in the ALLOWED map above made the list itself unstable — a full suite run
    # regenerates ci-assurance.md, and the entry went stale mid-run.
    GENERATED = (".engine/docs/ci-assurance.md", ".engine/knowledge/graph.json")

    def _hits(self) -> set:
        import subprocess
        found = set()
        for form in self.FORMS:
            out = subprocess.run(["git", "grep", "-lI", "--", form, ":(exclude).engine/docs/ci-assurance.md",
                                  ":(exclude).engine/knowledge/graph.json"],
                                 cwd=self.ROOT, capture_output=True, text=True)
            found.update(line for line in out.stdout.splitlines() if line)
        return found

    def test_the_old_name_survives_only_on_the_stated_exclusion_list(self):
        unexplained = sorted(self._hits() - set(self.ALLOWED))
        self.assertEqual(unexplained, [],
                         "these still carry the old name and nothing says why: " + ", ".join(unexplained))

    def test_the_exclusion_list_carries_no_entry_that_is_already_clean(self):
        """A stale exclusion is how a list stops meaning anything — it accumulates permissions for
        things that no longer need them, and the next real miss hides among them."""
        stale = sorted(set(self.ALLOWED) - self._hits())
        self.assertEqual(stale, [], "these are on the exclusion list but are already clean: "
                         + ", ".join(stale))

    def test_the_tool_ships_under_its_new_name_and_the_old_module_is_gone(self):
        self.assertTrue((self.ROOT / ".engine/tools/project_manager.py").exists())
        self.assertFalse((self.ROOT / ".engine/tools/plan_coordinator.py").exists())
        self.assertFalse((self.ROOT / ".engine/tools/test_plan_coordinator.py").exists())

    def test_the_data_boundary_is_untouched(self):
        """The line the retitle deliberately stops at. Asserted against the SCHEMAS as they stand, not
        against a diff, so it keeps holding after this node's commit is history."""
        for name in ("engine-plan.v1", "plan-record.v1", "engine-program.v1"):
            self.assertTrue((self.ROOT / f".engine/schemas/{name}.json").exists(),
                            f"{name} must keep its id — renaming it would make every stored record in "
                            "every deployed project unreadable")
        import project_manager
        self.assertIn('prog="project_manager.py"',
                      (self.ROOT / ".engine/tools/project_manager.py").read_text(encoding="utf-8"))
        self.assertIn('add_parser("plan")',
                      (self.ROOT / ".engine/tools/build_coordinator.py").read_text(encoding="utf-8"),
                      "the Build's `plan` verb namespace names the artifact a Build binds, not the "
                      "component that authored it, and stays")
        self.assertTrue(hasattr(project_manager, "ProjectManagerError"))


class ARealProjectCrossesTheRenameWhole(unittest.TestCase):
    """The rename is of a DELIVERED file, so proving the prose is consistent proves nothing about the
    thing an operator ends up running.

    An already-deployed project has `plan_coordinator.py` sitting in its own tools directory. An update
    that only DELIVERS would leave it there beside the new module: still importable, still runnable,
    still offering a `plan` shelf, and now the second answer to a question that should have one. This
    drives the REAL upgrade against a throwaway clone and asserts the deployed tree ends with exactly
    one of them.

    Note the manifest shape this rests on. Core provides its tools as the glob `.engine/tools/*.py`, so
    the new file is delivered without a manifest edit — and the OLD file is owned by that same glob in
    the deployed tree, which is what puts it in the reconcile's delete set. That is a different path
    from the literal-`provides` rename orphan the #599 demo covers, so it is worth its own proof rather
    than an argument by analogy."""

    def test_the_deployed_tree_ends_with_project_manager_and_no_runnable_predecessor(self):
        import shutil
        import tempfile
        import engine_fixture
        import module_manager as mm
        import validate
        real_root = validate.ROOT
        with tempfile.TemporaryDirectory() as d:
            live = engine_fixture.clone_engine(real_root, os.path.join(d, "live"))
            release = engine_fixture.clone_engine(real_root, os.path.join(d, "release"))
            # The deployed project as it stands BEFORE this release: the predecessor is on disk under its
            # old name. Copied from the real module so it is a genuinely runnable file, not a stub.
            stale = os.path.join(live, ".engine", "tools", "plan_coordinator.py")
            shutil.copyfile(os.path.join(live, ".engine", "tools", "project_manager.py"), stale)
            self.assertTrue(os.path.exists(stale))
            with mm._redirect_root(live):
                mm.upgrade(ref="v-retitle", release_tree=release)
            self.assertTrue(os.path.exists(os.path.join(live, ".engine", "tools", "project_manager.py")),
                            "the retitled module must be delivered")
            self.assertFalse(os.path.exists(stale),
                             "the predecessor must be REMOVED, not left runnable beside its successor — "
                             "two modules answering the same question is the failure a rename creates")
            self.assertFalse(os.path.exists(os.path.join(live, ".engine", "tools",
                                                         "test_plan_coordinator.py")))


class TestSealHandback(unittest.TestCase):
    """The seal used to print digests and stop; then it printed a manual. Both were wrong.

    The operator's ruling, after living with the long form: a hand-back that needs paragraphs of
    meta-commentary to explain the next step is a poorly designed step. So what these pin is the
    SHORTNESS as much as the content — the brevity cap is a real requirement, not a style note —
    plus the two hard content rules: never /clear (the one session that cleared at this boundary is
    the one that lost its thread), and no gate vocabulary (the pause is an offer).
    """

    def text(self):
        return project_manager.seal_handback("pln_0123456789ab")

    def test_it_is_brief(self):
        # Six lines of substance. A hand-back that grows past this is becoming a manual again.
        self.assertLessEqual(len([l for l in self.text().splitlines() if l.strip()]), 6)

    def test_it_names_every_required_element(self):
        text = self.text()
        self.assertIn("Settle", text)
        self.assertIn("/compact", text)
        self.assertIn("model and effort", text)
        self.assertIn("Wait", text)

    def test_it_carries_the_plan_id_into_the_bind_it_suggests(self):
        self.assertIn("--plan pln_0123456789ab", self.text())

    def test_it_never_suggests_clear(self):
        # The only build session that lost its thread is the one that ran /clear here instead of
        # /compact: a cleared session keeps nothing to re-ground from. Not guidance to ever revive.
        self.assertNotIn("/clear", self.text())

    def test_it_claims_no_teeth_and_teaches_no_flag(self):
        text = self.text()
        self.assertNotIn("REFUSES", text)
        self.assertNotIn("--override", text)
        self.assertNotIn("--session-model", text)

    def test_it_does_not_lecture(self):
        # The recurring recommendations and provider disclosures live in the runbook, read when
        # orchestrating — not re-printed at every seal into every session.
        text = self.text()
        self.assertNotIn("/autocompact", text)
        self.assertNotIn("/hooks", text)
        self.assertNotIn("Codex", text)


if __name__ == "__main__":
    unittest.main()
