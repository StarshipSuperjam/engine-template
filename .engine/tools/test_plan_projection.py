#!/usr/bin/env python3
"""Tests for plan_projection — the generated, rebuildable views over the plan library.

Two properties carry the weight.

DETERMINISM: regenerating from the same revision must produce the same bytes. Without it an operator
cannot tell "the plan changed" from "the renderer changed", and diffing the library stops meaning
anything.

REBUILDABILITY: delete every generated file and they all come back identical, from the revisions
alone. That is what makes it safe for these views to be rich — a generated file that were also a
source would be a second place the truth lives.

The third group is about the cold reader: someone opening this folder months later with no session
context should be able to say what each plan is, where it stands, and when it last moved, from the
generated files alone.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

import plan_projection
import plan_store

from test_plan_store import _document, _payload


class _Projected(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "plans"
        self.lib = plan_store.PlanLibrary(self.root)
        self.addCleanup(self._tmp.cleanup)

    def _plan(self, **over):
        document = _document(**over)
        return self.lib.create(document), document


class Determinism(_Projected):
    def test_plan_md_regenerates_byte_identically(self):
        slug, _ = self._plan()
        first = plan_projection.project_plan(self.lib, slug).read_bytes()
        second = plan_projection.project_plan(self.lib, slug).read_bytes()
        self.assertEqual(first, second)

    def test_the_whole_library_regenerates_byte_identically(self):
        self._plan(plan_id="pln_aaaaaaaaaaaa", title="First plan")
        self._plan(plan_id="pln_bbbbbbbbbbbb", title="Second plan")
        plan_projection.project_library(self.lib)
        before = {p.relative_to(self.root): p.read_bytes()
                  for p in sorted(self.root.rglob("*")) if p.is_file()}
        plan_projection.project_library(self.lib)
        after = {p.relative_to(self.root): p.read_bytes()
                 for p in sorted(self.root.rglob("*")) if p.is_file()}
        self.assertEqual(before, after)

    def test_the_render_is_pure_of_the_moment(self):
        # No "generated at" stamp, no run-dependent value: those are the usual way determinism dies.
        slug, document = self._plan()
        record = self.lib.read_record(slug)
        text = plan_projection.render_plan(document, record)
        self.assertNotIn("generated at", text.lower())
        self.assertEqual(text, plan_projection.render_plan(document, record))

    def test_a_new_revision_changes_the_projection(self):
        slug, _ = self._plan()
        before = plan_projection.project_plan(self.lib, slug).read_text(encoding="utf-8")
        self.lib.append_revision(slug, _document(revision=2, title="A stored plan"), expected_revision=1)
        after = plan_projection.project_plan(self.lib, slug).read_text(encoding="utf-8")
        self.assertNotEqual(before, after)
        self.assertIn("revision 2", after)


class Rebuildability(_Projected):
    def test_every_generated_file_comes_back_after_deletion(self):
        self._plan(plan_id="pln_aaaaaaaaaaaa", title="First plan")
        self._plan(plan_id="pln_bbbbbbbbbbbb", title="Second plan")
        plan_projection.project_library(self.lib)
        generated = sorted(p for p in self.root.rglob("*")
                           if p.is_file() and p.name in
                           (plan_projection.PLAN_MD, plan_projection.INDEX_MD, plan_projection.INDEX_JSON))
        self.assertEqual(len(generated), 4)          # two PLAN.md, one INDEX.md, one index.json
        before = {p.relative_to(self.root): p.read_bytes() for p in generated}

        for path in generated:
            path.unlink()
        plan_projection.project_library(self.lib)

        after = {p.relative_to(self.root): p.read_bytes()
                 for p in sorted(self.root.rglob("*"))
                 if p.is_file() and p.name in
                 (plan_projection.PLAN_MD, plan_projection.INDEX_MD, plan_projection.INDEX_JSON)}
        self.assertEqual(before, after)

    def test_the_revisions_are_never_touched_by_projecting(self):
        slug, _ = self._plan()
        revisions = {p: p.read_bytes() for p in (self.root / slug / "revisions").iterdir()}
        record = (self.root / slug / "record.json").read_bytes()
        plan_projection.project_library(self.lib)
        self.assertEqual(revisions, {p: p.read_bytes() for p in (self.root / slug / "revisions").iterdir()})
        self.assertEqual(record, (self.root / slug / "record.json").read_bytes())

    def test_an_empty_library_projects_without_crashing(self):
        entries = plan_projection.project_library(self.lib)
        self.assertEqual(entries, [])
        self.assertIn("no plans yet", (self.root / plan_projection.INDEX_MD).read_text(encoding="utf-8"))


class ColdReader(_Projected):
    def test_the_index_identifies_every_plan_its_status_and_last_activity(self):
        self._plan(plan_id="pln_aaaaaaaaaaaa", title="First plan")
        self._plan(plan_id="pln_bbbbbbbbbbbb", title="Second plan")
        plan_projection.project_library(self.lib)
        index = (self.root / plan_projection.INDEX_MD).read_text(encoding="utf-8")
        for fragment in ("First plan", "Second plan", "pln_aaaaaaaaaaaa", "pln_bbbbbbbbbbbb",
                         "awaiting-approval", "2026-08-23T00:00:01Z"):
            self.assertIn(fragment, index)

    def test_the_index_says_a_shelf_is_not_a_queue(self):
        plan_projection.project_library(self.lib)
        index = (self.root / plan_projection.INDEX_MD).read_text(encoding="utf-8")
        self.assertIn("shelf, not a queue", index)

    def test_index_json_carries_the_same_facts_for_a_machine(self):
        slug, document = self._plan()
        plan_projection.project_library(self.lib)
        index = json.loads((self.root / plan_projection.INDEX_JSON).read_text(encoding="utf-8"))
        self.assertEqual(index["schema_version"], "plan-index.v1")
        entry = index["plans"][0]
        self.assertEqual(entry["plan_id"], document["plan_id"])
        self.assertEqual(entry["slug"], slug)
        self.assertEqual(entry["revision"], 1)

    def test_plan_md_leads_with_purpose_and_reasoning_not_the_graph(self):
        slug, _ = self._plan()
        text = plan_projection.project_plan(self.lib, slug).read_text(encoding="utf-8")
        self.assertLess(text.index("## Intent"), text.index("## Execution graph"))
        self.assertLess(text.index("## Deliberation"), text.index("## Execution graph"))
        self.assertIn("The strongest case against doing this", text)

    def test_plan_md_says_it_is_generated(self):
        slug, _ = self._plan()
        text = plan_projection.project_plan(self.lib, slug).read_text(encoding="utf-8")
        self.assertIn("edits here are overwritten", text)

    def test_open_decisions_are_shown_with_their_consequence(self):
        document = _document()
        document["deliberation"]["unresolved_decisions"] = ["Who owns retention?"]
        slug = self.lib.create(document)
        text = plan_projection.project_plan(self.lib, slug).read_text(encoding="utf-8")
        self.assertIn("Who owns retention?", text)
        self.assertIn("cannot be sealed", text)

    def test_the_revision_history_shows_a_redaction_rather_than_a_gap(self):
        slug, _ = self._plan()
        self.lib.append_revision(slug, _document(revision=2), expected_revision=1)
        self.lib.redact_revision(slug, 1, reason="raw intent held a credential")
        text = plan_projection.project_plan(self.lib, slug).read_text(encoding="utf-8")
        self.assertIn("body redacted", text)
        self.assertIn("raw intent held a credential", text)

    def test_a_damaged_plan_is_flagged_on_the_shelf_not_dropped_from_it(self):
        slug, _ = self._plan()
        head = self.root / slug / self.lib.read_record(slug)["current"]["snapshot"]
        head.write_text(json.dumps({"schema_version": "engine-plan.v1"}), encoding="utf-8")
        entries = plan_projection.project_library(self.lib)
        self.assertEqual(len(entries), 1)
        self.assertFalse(entries[0]["readable"])
        index = (self.root / plan_projection.INDEX_MD).read_text(encoding="utf-8")
        self.assertIn("Needs attention", index)
        self.assertIn(slug, index)


class Scheduling(_Projected):
    def _graph_plan(self):
        document = _document()
        payload = _payload()
        payload["work_items"] = [
            {"id": "alpha-root", "description": "Root.", "paths": ["a.py"], "depends_on": [],
             "exclusive_resources": [], "executor_class": "integrator", "verification": ["runs"],
             "output_contract": {"deliverable": "a", "artifact_kinds": ["code"],
                                 "required_evidence": ["test"]}},
            {"id": "beta-middle", "description": "Middle.", "paths": ["b.py"],
             "depends_on": ["alpha-root"], "exclusive_resources": ["shared"],
             "executor_class": "integrator", "verification": ["runs"],
             "output_contract": {"deliverable": "b", "artifact_kinds": ["code"],
                                 "required_evidence": ["test"]}},
            {"id": "gamma-leaf", "description": "Leaf.", "paths": ["c.py"],
             "depends_on": ["beta-middle"], "exclusive_resources": [],
             "executor_class": "integrator", "verification": ["runs"],
             "output_contract": {"deliverable": "c", "artifact_kinds": ["code"],
                                 "required_evidence": ["test"]}},
            {"id": "delta-other-root", "description": "A second root.", "paths": ["d.py"],
             "depends_on": [], "exclusive_resources": [], "executor_class": "integrator",
             "verification": ["runs"],
             "output_contract": {"deliverable": "d", "artifact_kinds": ["code"],
                                 "required_evidence": ["test"]}},
        ]
        document["build_plan"] = payload
        return self.lib.create(document), document

    def test_the_critical_path_counts_the_longest_successor_chain(self):
        _, document = self._graph_plan()
        chains = plan_projection.critical_path(document["build_plan"]["work_items"])
        self.assertEqual(chains, {"alpha-root": 3, "beta-middle": 2, "gamma-leaf": 1,
                                  "delta-other-root": 1})

    def test_the_prose_names_the_depth_the_entry_points_and_what_can_start_now(self):
        slug, _ = self._graph_plan()
        text = plan_projection.project_plan(self.lib, slug).read_text(encoding="utf-8")
        self.assertIn("longest chain runs 3 node(s) deep", text)
        self.assertIn("`alpha-root`", text)
        self.assertIn("2 node(s) can start immediately", text)
        self.assertIn("`delta-other-root`", text)
        self.assertIn("Execution is serial", text)

    def test_hyphenated_ids_survive_into_the_mermaid_diagram(self):
        # Mermaid reads a bare hyphen as syntax, so unescaped ids would render a broken diagram —
        # visible to an operator, invisible to a test that only checks the id appears somewhere.
        slug, _ = self._graph_plan()
        text = plan_projection.project_plan(self.lib, slug).read_text(encoding="utf-8")
        diagram = text.split("```mermaid")[1].split("```")[0]
        self.assertIn('n_alpha_root["alpha-root"]', diagram)
        self.assertIn("n_alpha_root --> n_beta_middle", diagram)
        self.assertNotIn("  alpha-root[", diagram)

    def test_conditional_execution_is_described_differently_from_serial(self):
        _, document = self._graph_plan()
        document = dict(document)
        document["build_plan"] = dict(document["build_plan"])
        document["build_plan"]["parallelism"] = {"mode": "conditional", "max_concurrency": 3}
        prose = plan_projection._scheduling_prose(
            document["build_plan"]["work_items"],
            plan_projection.critical_path(document["build_plan"]["work_items"]),
            document["build_plan"]["parallelism"])
        self.assertIn("up to 3 nodes at once", prose)
        self.assertNotIn("Execution is serial", prose)


class TextFidelity(_Projected):
    def test_unicode_survives_the_round_trip(self):
        document = _document(title="Plan — “quoted”, naïve, 🚂")
        document["intent"]["raw"] = "Búild it — with émphasis, 中文, and an emoji 🎯"
        slug = self.lib.create(document)
        text = plan_projection.project_plan(self.lib, slug).read_text(encoding="utf-8")
        self.assertIn("Plan — “quoted”, naïve, 🚂", text)
        self.assertIn("中文", text)
        self.assertIn("🎯", text)

    def test_multiline_intent_keeps_its_shape_as_a_blockquote(self):
        document = _document()
        document["intent"]["raw"] = "First line.\n\nThird line after a blank."
        slug = self.lib.create(document)
        text = plan_projection.project_plan(self.lib, slug).read_text(encoding="utf-8")
        self.assertIn("> First line.\n>\n> Third line after a blank.", text)

    def test_a_pipe_in_a_title_does_not_break_the_index_table(self):
        self._plan(title="Before | after")
        plan_projection.project_library(self.lib)
        index = (self.root / plan_projection.INDEX_MD).read_text(encoding="utf-8")
        self.assertIn("Before \\| after", index)

    def test_generated_files_are_owner_only_like_the_revisions_they_restate(self):
        previous = os.umask(0o000)
        try:
            slug, _ = self._plan()
            plan_projection.project_library(self.lib)
        finally:
            os.umask(previous)
        for name in (plan_projection.INDEX_MD, plan_projection.INDEX_JSON):
            self.assertEqual((self.root / name).stat().st_mode & 0o777, 0o600, name)
        self.assertEqual((self.root / slug / plan_projection.PLAN_MD).stat().st_mode & 0o777, 0o600)


class ImportedDraft(_Projected):
    """A plan with no Build half still renders, and says so.

    Rendering is not optional for an imported draft. `project_library` runs on every write, and the
    arrival report an import produces points the operator at `preview`, which renders through the same
    function — so a plan this could not render would hand them a crash as their next experience. The
    deliberation half, the intent and the ledger render through the SAME code as any other plan, which
    is why there is no second renderer to drift.
    """

    def _imported(self):
        document = _document(
            build_plan={"schema_version": "build-plan.imported", "work_items": []},
            deliberation={"problem_frame": "", "case_against": "", "alternatives": [],
                          "failure_modes": [], "unresolved_decisions": ["What is this asking for?"]},
            intake={"provenance": "Accepted Claude Code plan, imported at plan-exit."})
        slug = self.lib.create(document)
        plan_projection.project_library(self.lib)
        return slug, (self.root / slug / plan_projection.PLAN_MD).read_text(encoding="utf-8")

    def test_it_renders_and_names_the_missing_build_half(self):
        _, rendered = self._imported()
        self.assertIn("## The Build half", rendered)
        self.assertIn("an import decomposes nothing", rendered)
        self.assertIn("none yet — imported verbatim and not decomposed", rendered)

    def test_the_payload_sections_are_absent_rather_than_empty(self):
        # An empty "Execution graph" or a bare "Scope" heading would read as a rendering fault; the
        # honest projection of a plan with no Build half is that those sections are not there.
        _, rendered = self._imported()
        for heading in ("## Objective", "## Execution graph", "## Scope", "## What success requires",
                        "## The work, node by node", "## Specification posture"):
            self.assertNotIn(heading, rendered, heading)

    def test_the_deliberation_gaps_read_as_gaps(self):
        _, rendered = self._imported()
        self.assertEqual(rendered.count("_Not stated."), 2)     # problem frame and case against
        self.assertIn("### The strongest case against doing this", rendered)
        self.assertIn("What is this asking for?", rendered)
        self.assertIn("cannot be sealed", rendered)

    def test_the_shared_halves_render_through_the_same_code(self):
        # The anti-drift property: intent, intake and the ledger appear for an imported draft exactly
        # as they do for any other plan, because one function renders both shapes.
        _, rendered = self._imported()
        for shared in ("## Intent", "**As the operator put it:**", "## Where this plan came from",
                       "## Revision history"):
            self.assertIn(shared, rendered)

    def test_it_regenerates_byte_identically(self):
        slug, first = self._imported()
        (self.root / slug / plan_projection.PLAN_MD).unlink()
        plan_projection.project_library(self.lib)
        self.assertEqual((self.root / slug / plan_projection.PLAN_MD).read_text(encoding="utf-8"), first)


if __name__ == "__main__":
    unittest.main()
