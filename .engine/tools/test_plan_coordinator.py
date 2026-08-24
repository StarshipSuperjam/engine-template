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


class Enumeration(unittest.TestCase):
    def test_the_depths_offered_match_the_documented_set(self):
        self.assertEqual(set(plan_coordinator.DEPTHS), {"light", "standard", "thorough"})

    def test_every_status_the_surface_can_report_is_in_the_enumeration(self):
        for status in ("draft", "awaiting-approval", "awaiting-review", "review-recorded",
                       "sealed", "active", "complete", "retired", "abandoned"):
            self.assertIn(status, plan_store.STATUSES)


if __name__ == "__main__":
    unittest.main()
