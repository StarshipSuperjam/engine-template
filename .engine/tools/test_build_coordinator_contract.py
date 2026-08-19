"""Focused tests for the pure PR-body composer (build_coordinator_contract).

These exercise the composer in isolation: schema validation of the claim, and — the load-bearing
check — that a composed body passes the REAL `pr-body-completeness` rule (validate.kind_presence),
never a reimplementation of it. Coordinator-side behavior (verbs, live apply, preflight recording) is
tested separately in test_build_coordinator*.py.
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import unittest

import build_coordinator_contract as bcc
import build_coordinator_review as review
import spec_referent
import validate

ROOT = bcc.ROOT


def _good_claim() -> dict:
    return {
        "schema_version": "pr-body-claim.v1",
        "release_impact": "minor",
        "linkage": {"closes": [], "part_of": [900]},
        "purpose": {
            "thesis": "Compose the PR body from a typed claim instead of hand-pasting the template.",
            "problem": "Today the session hand-fills the template and pastes generated evidence (BO-35).",
            "mechanism": ["Add a `contract` verb family.", "Render nine sections from one claim."],
            "impact": "Deterministic assembly moves to the coordinator; narrative stays the AI's.",
        },
        "scope": {
            "summary": "A new coordinator verb family and one schema.",
            "items": ["`contract template|preview|apply`", "`pr-body-claim.v1` schema"],
            "impact": "One claim in, one complete body out.",
        },
        "out_of_scope": {
            "summary": "Third-party host templates stay separate.",
            "items": [{"item": "External-contribution templates", "reason": "a separate submission path",
                       "tracked_as": "#900"}],
            "impact": "Deliberate boundaries with recorded reasoning, not gaps.",
        },
        "risk": {
            "items": [{"risk": "`apply` writes a live PR body", "bound": "read-back verify then rollback",
                       "most_sensitive": True}],
            "guardrail_note": "No check rule is modified, so no killswitch-tier acknowledgement is owed.",
            "impact": "Every write fails in the safer direction; your merge remains the gate.",
        },
        "behaviors": {
            "observable": True,
            "entries": [
                {"claim": "A composed body passes the real completeness gate",
                 "tests": ["test_build_coordinator_contract.py::test_composed_body_passes_real_gate"]},
                {"claim": "A multiline claim field is refused",
                 "tests": ["test_build_coordinator_contract.py::test_multiline_field_rejected"],
                 "regression_lock": "blocks structure injection"},
            ],
        },
        "demonstration": {
            "kind": "runnable",
            "command": "python tools/build_coordinator.py contract preview --claim claim.json ...",
            "pass_signal": "a complete body renders and the gate reports green",
            "fail_signal": "a schema error names the unfilled slot",
        },
        "validation": {
            "summary": "The mechanical floor this change cleared, bound to the final commit.",
            "caveats": ["One unrelated flake in an unchanged module, not implicated by this diff."],
            "live_helpers": {"all_available": True},
            "impact": "A green floor shows conformance, not correctness — your read at merge is the gate.",
        },
        "review": {
            "summary": "One thorough cold pass; what it found and how the merged version compares.",
            "loop_narrative": ["One clean cold pass; no repairs, so reviewed and final commits match."],
            "material_divergence": False,
            "finding_summaries": [],
            "impact": "Review is a deliberate-effort pass, not a gate; your merge is the binding gate.",
        },
        "files_of_interest": {
            "items": [{"path": ".engine/tools/build_coordinator_contract.py", "role": "the pure composer"}],
            "impact": "This file most determines the composed body's shape.",
        },
        "ai_involvement": {
            "tools": [{"tool": "Claude Code", "model": "Opus 4.8", "role": "authored the change end to end"}],
            "operator_decisions": [{"summary": "Approved thorough review depth.", "decision_date": "2026-08-15"}],
            "judgment_split": "Narrative is AI judgment; assembly and evidence are mechanical.",
            "impact": "The AI drafts; the operator merges.",
        },
    }


def _good_evidence() -> dict:
    return {
        "closes": [901],
        "change_profile": "**Change profile** — small: 3 files, tools + schemas.",
        "validation_results": "- `validate.py --suite CI` passed; self-tests passed at commit abc1234.",
        "index_regen": "Regeneration updated 1 of the engine's generated index files (.engine/knowledge/graph.json) from the final tree — generated paths only.",
        "spec_steps": "**Things I checked for you**\n_(engine's side)_\n- All criteria: automated tests.",
        "review_coverage": "thorough; plan review (4 lenses) ran before build, five cold lenses after.",
        "disagreement_lines": [],
        "drift_line": "reviewed and submitted commits are identical; no post-review divergence.",
        "composition_marker": "<!-- engine-pr-contract:v1 sha256:deadbeef commit=abc1234 -->",
        "preserved_blocks": ["<!-- engine-build-handoff:v2 sha256:cafe -->\npreserved\n<!-- /engine-build-handoff:v2 -->"],
    }


class TestComposedBodyOmitsPrivateReference(unittest.TestCase):
    def test_body_carries_operator_summary_not_private_reference(self):
        # StarshipSuperjam/engine-template#981, plan obligation #5: drive the REAL composer end to end
        # (finding -> disagreement line -> evidence -> composed body) and confirm the public body carries
        # the operator-safe disclosure but never the reviewer-internal private_reference. This guards the
        # composition seam (build_coordinator_contract.compose appends disagreement_lines verbatim), not
        # just the disagreement_line unit.
        finding = {"id": "SEC-9", "severity": "blocking", "blocks_this_pr": False,
                   "operator_summary": "Concern rejected on verified evidence.",
                   "private_reference": "LEAKME-body-private-XYZ"}
        line = review.disagreement_line(finding)
        body = bcc.compose(_good_claim(), {**_good_evidence(), "disagreement_lines": [line]})
        self.assertIn("Reviewer disagreement `SEC-9`", body)
        self.assertIn("Concern rejected on verified evidence.", body)
        self.assertNotIn("LEAKME-body-private-XYZ", body)
        self.assertNotIn("Private details", body)


class TestClaimValidation(unittest.TestCase):
    def test_good_claim_validates(self):
        bcc.validate_claim(_good_claim())  # must not raise

    def test_multiline_field_rejected(self):
        bad = _good_claim()
        bad["purpose"]["thesis"] = "line one\n## Injected heading"
        with self.assertRaises(bcc.ContractError):
            bcc.validate_claim(bad)

    def test_blank_field_rejected(self):
        bad = _good_claim()
        bad["purpose"]["thesis"] = ""
        with self.assertRaises(bcc.ContractError):
            bcc.validate_claim(bad)

    def test_overlapping_linkage_rejected(self):
        bad = _good_claim()
        bad["linkage"] = {"closes": [5], "part_of": [5]}
        with self.assertRaises(bcc.ContractError) as ctx:
            bcc.validate_claim(bad)
        self.assertIn("#5", str(ctx.exception))

    def test_behavior_without_tests_rejected(self):
        bad = _good_claim()
        del bad["behaviors"]["entries"][0]["tests"]
        with self.assertRaises(bcc.ContractError):
            bcc.validate_claim(bad)

    def test_unknown_property_rejected(self):
        bad = _good_claim()
        bad["surprise"] = 1
        with self.assertRaises(bcc.ContractError):
            bcc.validate_claim(bad)

    def test_validation_lists_every_problem_with_neutral_remediation(self):
        bad = _good_claim()
        bad["purpose"]["thesis"] = ""            # an empty slot
        bad["scope"]["summary"] = "line a\nline b"  # a malformed (multiline) value, not empty
        with self.assertRaises(bcc.ContractError) as ctx:
            bcc.validate_claim(bad)
        msg = str(ctx.exception)
        self.assertIn("purpose/thesis", msg)     # both problems named, not just the first
        self.assertIn("scope/summary", msg)
        self.assertNotIn("null/empty slots", msg)  # remediation no longer misdescribes a malformed value


class TestCompose(unittest.TestCase):
    def setUp(self):
        self.body = bcc.compose(_good_claim(), _good_evidence())

    def test_composed_body_passes_real_gate(self):
        rule_path = os.path.join(ROOT, ".engine", "check", "pr-body-completeness.json")
        with open(rule_path, encoding="utf-8") as fh:
            rule = json.load(fh)
        verdict, findings = validate.kind_presence(rule, {"pr_body": self.body})
        self.assertTrue(verdict, msg=f"gate rejected composed body: {[f['message'] for f in findings]}")

    def test_all_nine_sections_present_in_order(self):
        positions = [self.body.index(f"## {h}") for h in bcc.HEADING_ORDER]
        self.assertEqual(positions, sorted(positions), "sections out of gate order")

    def test_behaviors_is_level_three_under_scope(self):
        scope = self.body.index("## Scope")
        oos = self.body.index("## Out of scope")
        beh = self.body.index("### Behaviors")
        self.assertTrue(scope < beh < oos, "Behaviors must be a level-3 subsection inside Scope")
        self.assertNotIn("\n## Behaviors", self.body)  # never promoted to a level-2 section

    def test_consent_preamble_present_verbatim(self):
        import release_cut
        self.assertIn(release_cut.template_preamble(), self.body)

    def test_no_template_placeholder_or_comment(self):
        import re
        # The template's instructional comments and angle-bracket placeholders must never survive.
        self.assertNotIn("TITLE THIS PULL REQUEST", self.body)
        self.assertNotIn("BEFORE OPENING THIS PR", self.body)
        self.assertNotIn("<one-line", self.body)
        self.assertNotIn("<Replace this line", self.body)
        # The only HTML comments allowed are the engine's own machine markers (open or close).
        for tag in re.findall(r"<!--\s*(/?[^\s]+)", self.body):
            self.assertTrue(tag.lstrip("/").startswith("engine-"), f"non-engine comment survived: {tag}")

    def test_preserved_marker_block_carried(self):
        self.assertIn("engine-build-handoff:v2", self.body)

    def test_composition_marker_present(self):
        self.assertIn("engine-pr-contract:v1", self.body)

    def test_linkage_at_top_one_per_line_never_comma(self):
        purpose_at = self.body.index("## Purpose")
        # Both declarations appear above the first section, each on its own line, never comma-joined.
        self.assertIn("Closes #901", self.body[:purpose_at])
        self.assertIn("Part of #900", self.body[:purpose_at])
        self.assertRegex(self.body, r"(?m)^Closes #901$")
        self.assertRegex(self.body, r"(?m)^Part of #900$")
        self.assertNotIn(", #", self.body)  # no comma-separated linkage anywhere
        # Part-of must NOT be buried in Out of scope anymore.
        oos = self.body.index("## Out of scope")
        nxt = self.body.index("## Risk")
        self.assertNotIn("Part of #900", self.body[oos:nxt])

    def test_close_linkage_reads_top_placed_part_of(self):
        # The safety cross-check must see a top-placed Part of, not only a section-placed one.
        import close_linkage_preflight as clp
        self.assertEqual(clp.part_of_declarations(self.body), {900})

    def test_non_observable_behaviors_render(self):
        claim = _good_claim()
        claim["behaviors"] = {"observable": False, "none_observable_reason": "a docs-only change"}
        body = bcc.compose(claim, _good_evidence())
        self.assertIn("Nothing here is observable behaviour", body)


class TestFillableTemplate(unittest.TestCase):
    def test_template_shape_has_every_top_level_key(self):
        tpl = bcc.fillable_template()
        schema = bcc._load_schema()
        self.assertEqual(set(tpl), set(schema["required"]))

    def test_template_does_not_validate_and_names_a_slot(self):
        # The emitted skeleton must fail validation so unfilled slots are caught by ordinary validation.
        with self.assertRaises(bcc.ContractError) as ctx:
            bcc.validate_claim(bcc.fillable_template())
        self.assertIn("pr-body-claim.v1", str(ctx.exception))


class TestMultiDocSpecSteps(unittest.TestCase):
    def _proj(self, path, runnable, engine):
        return {"path": path,
                "runnable": [{"criterion": c, "how_verified": v} for c, v in runnable],
                "engine_account": [{"criterion": c, "how_verified": v} for c, v in engine],
                "no_op_reason": None if runnable else "all-engine-account"}

    def test_merges_two_documents_into_two_groups(self):
        projections = [
            self._proj("docs/spec/a.md", [("A1", "click the button")], [("A2", "`pytest`")]),
            self._proj("docs/spec/b.md", [("B1", "open the page")], []),
        ]
        out = spec_referent.render_review_steps_multi(projections)
        self.assertIn("**Things you can confirm yourself**", out)
        self.assertIn("**Things I checked for you**", out)
        self.assertIn("A1: click the button", out)
        self.assertIn("B1: open the page", out)
        self.assertIn("A2: `pytest`", out)
        self.assertIn(spec_referent._PROMISE_CAVEAT, out)

    def test_no_runnable_across_docs_renders_engine_account_only(self):
        projections = [self._proj("docs/spec/a.md", [], [("A2", "`pytest`")])]
        out = spec_referent.render_review_steps_multi(projections)
        self.assertIn("Nothing here is something you can run yourself", out)
        self.assertIn("**Things I checked for you**", out)
        self.assertNotIn("**Things you can confirm yourself**", out)


class TestPreviewEvidence(unittest.TestCase):
    """The coordinator-side evidence assembler + finding-id match, exercised offline (I/O monkeypatched)."""

    def _state(self, findings=None):
        return {
            "build": {"repository": "owner/repo", "pr": 977, "base_at_bind": "b" * 40},
            "plan": {"durable_issue": None},
            "approval": {"depth": "thorough"},
            "validation": {"commit": "a" * 40,
                           "results": [{"id": "engine-ci", "commit": "a" * 40, "passed": True,
                                        "log_digest": "sha256:abc", "log_path": "/tmp/secret.log"}]},
            "repair": None,
            "findings": findings if findings is not None else [],
        }

    def test_assemble_evidence_composes_a_gate_passing_body(self):
        import build_coordinator as bc
        from unittest import mock
        claim = _good_claim()
        pr_data = {"body": "old body", "baseRefOid": "b" * 40}
        prof = mock.Mock(stdout="**Change profile** — small: 3 files.", returncode=0)
        with mock.patch.object(bc, "_run", return_value=prof), \
             mock.patch.object(bc.spec_service, "canonical_spec",
                               return_value={"posture": "none", "review_steps": "No settled spec applies."}), \
             mock.patch.object(bc.review, "required_disagreement_lines", return_value=[]), \
             mock.patch.object(bc, "_installed",
                               return_value=[{"lens": "spec-conformance"}, {"lens": "divergence-hunter"}]):
            ev = bc._assemble_evidence(self._state(), {"intent_source": {"kind": "direct"}, "spec": {}},
                                       claim, "c" * 40, pr_data)
        self.assertEqual(ev["closes"], [])           # claim closes [] + no durable issue
        self.assertNotIn("secret.log", ev["validation_results"])  # machine-local log path stripped
        self.assertIn("thorough depth", ev["review_coverage"])
        body = bcc.compose(claim, ev)
        rule_path = os.path.join(ROOT, ".engine", "check", "pr-body-completeness.json")
        with open(rule_path, encoding="utf-8") as fh:
            rule = json.load(fh)
        verdict, findings = validate.kind_presence(rule, {"pr_body": body})
        self.assertTrue(verdict, [f["message"] for f in findings])
        self.assertIn("Change profile", body)

    def test_post_approval_assumption_resolution_reaches_the_pr_review_record(self):
        # StarshipSuperjam/engine-template#1014: a disposition of an assumption authored 'unresolved' is the
        # operator's merge-time disclosure — it must render into the composed PR body, for verified AND
        # accepted-risk, distinct from any plan-authored status.
        import build_coordinator as bc
        from unittest import mock
        claim = _good_claim()
        pr_data = {"body": "old body", "baseRefOid": "b" * 40}
        prof = mock.Mock(stdout="**Change profile** — small: 3 files.", returncode=0)
        plan = {"intent_source": {"kind": "direct"}, "spec": {},
                "assumptions": [{"claim": "eADR-0043 has no dependents", "status": "unresolved"}]}
        for resolved_as in ("verified", "accepted-risk"):
            with self.subTest(resolved_as=resolved_as):
                state = self._state()
                state["assumption_dispositions"] = [
                    {"claim": "eADR-0043 has no dependents", "resolved_as": resolved_as,
                     "basis": "the review confirmed it"}]
                with mock.patch.object(bc, "_run", return_value=prof), \
                     mock.patch.object(bc.spec_service, "canonical_spec",
                                       return_value={"posture": "none", "review_steps": "No settled spec applies."}), \
                     mock.patch.object(bc.review, "required_disagreement_lines", return_value=[]), \
                     mock.patch.object(bc, "_installed",
                                       return_value=[{"lens": "spec-conformance"}, {"lens": "divergence-hunter"}]):
                    ev = bc._assemble_evidence(state, plan, claim, "c" * 40, pr_data)
                self.assertEqual(len(ev["assumption_resolutions"]), 1)
                self.assertIn("eADR-0043 has no dependents", ev["assumption_resolutions"][0])
                self.assertIn(resolved_as, ev["assumption_resolutions"][0])
                self.assertIn("the review confirmed it", ev["assumption_resolutions"][0])
                body = bcc.compose(claim, ev)
                self.assertIn("Assumption resolved after approval", body)
                self.assertIn("eADR-0043 has no dependents", body)

    def test_disposition_of_a_non_unresolved_assumption_is_not_surfaced(self):
        # The defensive filter: only assumptions authored 'unresolved' produce a disclosure (a stray
        # disposition for an already-clean claim never leaks into the PR body).
        import build_coordinator as bc
        from unittest import mock
        claim = _good_claim()
        pr_data = {"body": "old body", "baseRefOid": "b" * 40}
        prof = mock.Mock(stdout="**Change profile** — small: 3 files.", returncode=0)
        plan = {"intent_source": {"kind": "direct"}, "spec": {},
                "assumptions": [{"claim": "already settled", "status": "verified"}]}
        state = self._state()
        state["assumption_dispositions"] = [
            {"claim": "already settled", "resolved_as": "verified", "basis": "x"}]
        with mock.patch.object(bc, "_run", return_value=prof), \
             mock.patch.object(bc.spec_service, "canonical_spec",
                               return_value={"posture": "none", "review_steps": "No settled spec applies."}), \
             mock.patch.object(bc.review, "required_disagreement_lines", return_value=[]), \
             mock.patch.object(bc, "_installed",
                               return_value=[{"lens": "spec-conformance"}, {"lens": "divergence-hunter"}]):
            ev = bc._assemble_evidence(state, plan, claim, "c" * 40, pr_data)
        self.assertEqual(ev["assumption_resolutions"], [])

    def test_durable_issue_added_to_closes(self):
        import build_coordinator as bc
        from unittest import mock
        state = self._state()
        state["plan"]["durable_issue"] = 500
        claim = _good_claim()
        pr_data = {"body": "", "baseRefOid": "b" * 40}
        with mock.patch.object(bc, "_run", return_value=mock.Mock(stdout="p", returncode=0)), \
             mock.patch.object(bc.spec_service, "canonical_spec",
                               return_value={"posture": "none", "review_steps": "x"}), \
             mock.patch.object(bc.review, "required_disagreement_lines", return_value=[]), \
             mock.patch.object(bc, "_installed", return_value=[]):
            ev = bc._assemble_evidence(state, {"intent_source": {"kind": "direct"}, "spec": {}},
                                       claim, "c" * 40, pr_data)
        self.assertIn(500, ev["closes"])

    def test_claim_findings_must_match_exactly(self):
        import build_coordinator as bc
        claim = _good_claim()  # finding_summaries == []
        # a live finding with no claim summary -> reject
        with self.assertRaises(bc.CoordinatorError) as ctx:
            bc._assert_claim_findings(self._state(findings=[{"id": "SG-1"}]), claim)
        self.assertIn("SG-1", str(ctx.exception))
        # a claim summary for an unknown finding -> reject
        claim["review"]["finding_summaries"] = [{"id": "GHOST", "operator_summary": "x"}]
        with self.assertRaises(bc.CoordinatorError) as ctx:
            bc._assert_claim_findings(self._state(findings=[]), claim)
        self.assertIn("GHOST", str(ctx.exception))

    def _state_with_receipts(self, receipts):
        s = self._state()
        s["reviews"] = {"plan": {"receipts": []}, "deliverable": {"receipts": receipts}}
        return s

    def _assemble(self, bc, state):
        from unittest import mock
        with mock.patch.object(bc, "_run", return_value=mock.Mock(stdout="p", returncode=0)), \
             mock.patch.object(bc.spec_service, "canonical_spec",
                               return_value={"posture": "none", "review_steps": "x"}), \
             mock.patch.object(bc.review, "required_disagreement_lines", return_value=[]), \
             mock.patch.object(bc, "_installed", return_value=[]):
            return bc._assemble_evidence(state, {"intent_source": {"kind": "direct"}, "spec": {}},
                                         _good_claim(), "c" * 40, {"body": "", "baseRefOid": "b" * 40})

    def test_code_execution_disclosure_reads_receipts(self):
        import build_coordinator as bc
        ev = self._assemble(bc, self._state_with_receipts(
            [{"lens": "usability", "code_execution": "discarded-copy"}]))
        self.assertIn("throwaway copy", ev["code_execution_line"])
        ev_none = self._assemble(bc, self._state_with_receipts(
            [{"lens": "usability", "code_execution": "none"}]))
        self.assertIn("no reviewer executed", ev_none["code_execution_line"])

    def test_receipt_missing_code_execution_refuses(self):
        import build_coordinator as bc
        with self.assertRaises(bc.CoordinatorError) as ctx:
            self._assemble(bc, self._state_with_receipts([{"lens": "usability"}]))  # predates the field
        self.assertIn("re-recorded", str(ctx.exception))

    def test_review_coverage_reflects_whether_cold_reviewers_actually_ran(self):
        # A false-claim guard (surfaced by dogfooding the coordinator at quick depth): the Review "Coverage"
        # line must not say the deliverable lenses "ran after" when no cold-review receipt was recorded. It
        # keys on the recorded receipts, not on the installed lens set.
        import build_coordinator as bc
        from unittest import mock
        quick = self._state()
        quick["approval"] = {"depth": "quick"}                        # quick depth, and no "reviews" receipts
        with mock.patch.object(bc, "_run", return_value=mock.Mock(stdout="p", returncode=0)), \
             mock.patch.object(bc.spec_service, "canonical_spec",
                               return_value={"posture": "none", "review_steps": "x"}), \
             mock.patch.object(bc.review, "required_disagreement_lines", return_value=[]), \
             mock.patch.object(bc, "_installed",
                               return_value=[{"lens": "usability"}, {"lens": "spec-conformance"}]):
            ev_quick = bc._assemble_evidence(quick, {"intent_source": {"kind": "direct"}, "spec": {}},
                                             _good_claim(), "c" * 40, {"body": "", "baseRefOid": "b" * 40})
            ev_ran = bc._assemble_evidence(
                self._state_with_receipts([{"lens": "usability", "code_execution": "none"}]),
                {"intent_source": {"kind": "direct"}, "spec": {}},
                _good_claim(), "c" * 40, {"body": "", "baseRefOid": "b" * 40})
        # No receipt recorded -> the line says no cold reviewers ran, and never claims lenses "ran after".
        self.assertIn("no cold reviewers ran", ev_quick["review_coverage"])
        self.assertNotIn("ran after", ev_quick["review_coverage"])
        # A recorded cold-review receipt -> the deliverable lenses that ran are named.
        self.assertIn("ran after", ev_ran["review_coverage"])

    def test_index_regen_is_computed_from_the_diff(self):
        # Drive the real index_regen computation (not the fixture): the git-diff leg names a generated index
        # file, so the disclosure must be computed non-empty.
        import build_coordinator as bc
        from unittest import mock

        def run(argv, **k):
            if argv[:2] == ["git", "diff"]:
                return mock.Mock(stdout=".engine/knowledge/graph.json\n.engine/tools/x.py\n", returncode=0)
            return mock.Mock(stdout="**Change profile** — small", returncode=0)
        with mock.patch.object(bc, "_run", side_effect=run), \
             mock.patch.object(bc.spec_service, "canonical_spec",
                               return_value={"posture": "none", "review_steps": "x"}), \
             mock.patch.object(bc.review, "required_disagreement_lines", return_value=[]), \
             mock.patch.object(bc, "_installed", return_value=[]):
            ev = bc._assemble_evidence(self._state(), {"intent_source": {"kind": "direct"}, "spec": {}},
                                       _good_claim(), "c" * 40, {"body": "", "baseRefOid": "b" * 40})
        self.assertIn("graph.json", ev["index_regen"])
        self.assertIn("generated paths only", ev["index_regen"])


@contextlib.contextmanager
def _fake_stable_commit(root, label):
    """Stub for core.StableCommit, which otherwise refuses a dirty worktree (evidence binds to a commit)."""
    yield "f" * 40


class TestContractApply(unittest.TestCase):
    """The live-write fixed-point loop, exercised against a fake PR + fake store with the heavy helpers
    monkeypatched — the digest compare-and-swap, convergence, idempotent reapply, and safe rollback."""

    class _Store:
        def __init__(self, state):
            self._s = state
        def read(self):
            return dict(self._s)
        def mutate(self, change, from_revision=None):
            change(self._s)

    def _env(self, source_body):
        pr = {"body": source_body}
        def verify_draft(repo, pr_num):
            return {"body": pr["body"], "baseRefOid": "b" * 40, "state": "OPEN", "isDraft": True,
                    "headRefOid": "h" * 40, "mergeable": "MERGEABLE"}
        def must_run(argv, *, input_text=None):
            if argv[:3] == ["gh", "pr", "edit"]:
                pr["body"] = input_text
            return ""
        return pr, verify_draft, must_run

    def _args(self, digest, ack=True):
        import argparse
        return argparse.Namespace(plan="p.json", claim="c.json", source_body_digest=digest,
                                  ack_visibility=ack, json=False)

    def _patches(self, bc, verify_draft, must_run, close_result, legs=None):
        from unittest import mock
        legs = legs or {"results": [{"id": "pr-contract", "passed": True}], "contract_passed": True,
                        "contract_summary": "all filled", "ci_passed": True, "ci_summary": "", "declarations": []}
        return [
            mock.patch.object(bc, "_plan", return_value={}),
            mock.patch.object(bc, "_assert_plan", return_value=None),
            mock.patch.object(bc, "_assert_claim_findings", return_value=None),
            mock.patch.object(bc.composer, "load_claim", return_value=_good_claim()),
            mock.patch.object(bc, "_assemble_evidence", return_value={}),
            mock.patch.object(bc, "_verify_draft", side_effect=verify_draft),
            mock.patch.object(bc, "_must_run", side_effect=must_run),
            mock.patch.object(bc, "_close_linkage_result", side_effect=close_result),
            mock.patch.object(bc, "_compute_preflight_legs", return_value=legs),
            mock.patch.object(bc.core, "StableCommit", _fake_stable_commit),
        ]

    def _run_apply(self, bc, source_body, close_result, ack=True):
        import contextlib, io
        pr, verify_draft, must_run = self._env(source_body)
        store = self._Store({"revision": 1, "build": {"repository": "o/r", "pr": 977, "base_at_bind": "b" * 40},
                             "plan": {"durable_issue": None}})
        digest = bc._digest(source_body.encode())
        args = self._args(digest, ack=ack)
        patches = self._patches(bc, verify_draft, must_run, close_result)
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            with contextlib.redirect_stdout(io.StringIO()):
                bc.cmd_contract_apply(args, store)
        return pr, store

    def test_converges_and_records_pr_contract(self):
        import build_coordinator as bc
        pr, store = self._run_apply(bc, "old body", lambda *a, **k: {"lines": [], "defang": None})
        self.assertIn("## Purpose", pr["body"])                       # the composed body was applied
        self.assertEqual(store._s["pr_contract"]["body_digest"], bc._digest(pr["body"].encode()))
        self.assertTrue(store._s["pr_contract"]["complete"])
        self.assertIn("preflights", store._s)

    def test_source_digest_mismatch_refuses(self):
        import build_coordinator as bc
        from unittest import mock
        pr, verify_draft, must_run = self._env("live body")
        store = self._Store({"revision": 1, "build": {"repository": "o/r", "pr": 1, "base_at_bind": "b" * 40},
                             "plan": {"durable_issue": None}})
        args = self._args(bc._digest(b"a DIFFERENT body"))            # stale digest
        with contextlib.ExitStack() as stack:
            for p in self._patches(bc, verify_draft, must_run, lambda *a, **k: {"lines": [], "defang": None}):
                stack.enter_context(p)
            with self.assertRaises(bc.CoordinatorError) as ctx:
                bc.cmd_contract_apply(args, store)
        self.assertIn("source-body-digest", str(ctx.exception))
        self.assertEqual(pr["body"], "live body")                     # nothing written

    def test_requires_ack_visibility(self):
        import build_coordinator as bc
        args = self._args("sha256:x", ack=False)
        with self.assertRaises(bc.CoordinatorError) as ctx:
            bc.cmd_contract_apply(args, self._Store({"revision": 1}))
        self.assertIn("--ack-visibility", str(ctx.exception))

    def test_non_convergence_restores_original(self):
        import build_coordinator as bc
        counter = {"n": 0}
        def ever_changing(*a, **k):
            counter["n"] += 1
            return {"lines": [f"line variant {counter['n']}"], "defang": None}   # different every pass
        with self.assertRaises(bc.CoordinatorError) as ctx:
            self._run_apply(bc, "original body", ever_changing)
        self.assertIn("fixed point", str(ctx.exception))

    def test_armed_accidental_close_fails_safe(self):
        import build_coordinator as bc
        pr, verify_draft, must_run = self._env("orig")
        store = self._Store({"revision": 1, "build": {"repository": "o/r", "pr": 1, "base_at_bind": "b" * 40},
                             "plan": {"durable_issue": None}})
        args = self._args(bc._digest(b"orig"))
        with contextlib.ExitStack() as stack:
            for p in self._patches(bc, verify_draft, must_run,
                                   lambda *a, **k: {"lines": [], "defang": {"number": 5}}):
                stack.enter_context(p)
            with self.assertRaises(bc.CoordinatorError) as ctx:
                bc.cmd_contract_apply(args, store)
        self.assertIn("accidental close", str(ctx.exception))
        self.assertEqual(pr["body"], "orig")                          # restored

    def test_idempotent_reapply_writes_nothing(self):
        import build_coordinator as bc
        # The live body already equals what the composer produces (a re-run against a converged PR).
        composed = bcc.compose(_good_claim(), {"preserved_blocks": [], "close_linkage_lines": []})
        pr = {"body": composed}
        edits = []
        def verify_draft(repo, n):
            return {"body": pr["body"], "baseRefOid": "b" * 40}
        def must_run(argv, *, input_text=None):
            if argv[:3] == ["gh", "pr", "edit"]:
                edits.append(input_text); pr["body"] = input_text
            return ""
        store = self._Store({"revision": 1, "build": {"repository": "o/r", "pr": 1, "base_at_bind": "b" * 40},
                             "plan": {"durable_issue": None}})
        args = self._args(bc._digest(composed.encode()))
        with contextlib.ExitStack() as stack:
            for p in self._patches(bc, verify_draft, must_run, lambda *a, **k: {"lines": [], "defang": None}):
                stack.enter_context(p)
            with contextlib.redirect_stdout(io.StringIO()):
                bc.cmd_contract_apply(args, store)
        self.assertEqual(edits, [])                                   # converged pass 0: zero writes
        self.assertIn("pr_contract", store._s)                        # still records the contract

    def test_mid_loop_revision_bump_restores_the_intermediate(self):
        import build_coordinator as bc
        # Blocking-fix proof: a write succeeds, then Build state moves before the next pass; the intermediate
        # must be rolled back to the original, not left live.
        pr, verify_draft, must_run = self._env("orig")

        class BumpStore:
            def __init__(self):
                self.reads = 0
                self.s = {"revision": 1, "build": {"repository": "o/r", "pr": 1, "base_at_bind": "b" * 40},
                          "plan": {"durable_issue": None}}
            def read(self):
                self.reads += 1
                snap = dict(self.s)
                snap["revision"] = 2 if self.reads >= 3 else 1   # bumps at the pass-1 _apply_body check
                return snap
            def mutate(self, change, from_revision=None):
                change(self.s)

        store = BumpStore()
        args = self._args(bc._digest(b"orig"))
        with contextlib.ExitStack() as stack:
            for p in self._patches(bc, verify_draft, must_run, lambda *a, **k: {"lines": ["x"], "defang": None}):
                stack.enter_context(p)
            with self.assertRaises(bc.CoordinatorError) as ctx:
                bc.cmd_contract_apply(args, store)
        self.assertIn("Build evidence changed", str(ctx.exception))
        self.assertEqual(pr["body"], "orig")                          # intermediate rolled back

    def test_echo_mismatch_rolls_back_the_unconfirmed_write(self):
        import build_coordinator as bc
        pr, verify_draft, _ = self._env("orig")
        edits = []
        def must_run(argv, *, input_text=None):
            if argv[:3] == ["gh", "pr", "edit"]:
                edits.append(input_text)
                # GitHub mangles our composed write (stores something other than what we sent); a rollback to
                # "orig" is echoed faithfully.
                pr["body"] = "orig" if input_text == "orig" else input_text + " [MANGLED BY GITHUB]"
            return ""
        store = self._Store({"revision": 1, "build": {"repository": "o/r", "pr": 1, "base_at_bind": "b" * 40},
                             "plan": {"durable_issue": None}})
        args = self._args(bc._digest(b"orig"))
        with contextlib.ExitStack() as stack:
            for p in self._patches(bc, verify_draft, must_run, lambda *a, **k: {"lines": [], "defang": None}):
                stack.enter_context(p)
            with self.assertRaises(bc.CoordinatorError) as ctx:
                bc.cmd_contract_apply(args, store)
        self.assertIn("did not preserve", str(ctx.exception))
        self.assertEqual(pr["body"], "orig")           # the unconfirmed (mangled) write was rolled back
        self.assertEqual(edits[-1], "orig")            # last write was the rollback

    def test_mid_loop_external_edit_is_preserved_not_clobbered(self):
        import build_coordinator as bc
        pr, verify_draft, must_run = self._env("orig")

        def close_result(*a, **k):
            pr["body"] = "AN EXTERNAL EDIT"    # someone else edits the PR between passes
            return {"lines": ["x"], "defang": None}

        store = self._Store({"revision": 1, "build": {"repository": "o/r", "pr": 1, "base_at_bind": "b" * 40},
                             "plan": {"durable_issue": None}})
        args = self._args(bc._digest(b"orig"))
        with contextlib.ExitStack() as stack:
            for p in self._patches(bc, verify_draft, must_run, close_result):
                stack.enter_context(p)
            with self.assertRaises(bc.CoordinatorError) as ctx:
                bc.cmd_contract_apply(args, store)
        self.assertIn("concurrent edit", str(ctx.exception))
        self.assertEqual(pr["body"], "AN EXTERNAL EDIT")              # external edit preserved, never restored over


class TestReleaseImpactMarker(unittest.TestCase):
    # StarshipSuperjam/engine-template#942: the session supplies only the enum value (claim.release_impact); the renderer owns the visible
    # line AND the machine marker the release-action fold and the pr-release-impact CI check read.
    def test_compose_renders_visible_line_and_exactly_one_marker(self):
        import release_impact
        body = bcc.compose(_good_claim(), _good_evidence())
        self.assertIn("Release-Impact: minor", body)                        # visible operator-readable line
        self.assertEqual(release_impact.parse_impact(body), "minor")        # the machine marker
        self.assertEqual(len(release_impact.find_impact_markers(body)), 1)  # exactly one -> passes the CI check

    def test_missing_release_impact_fails_validation(self):
        claim = _good_claim()
        del claim["release_impact"]
        with self.assertRaises(bcc.ContractError):
            bcc.validate_claim(claim)

    def test_null_release_impact_fails_validation(self):
        claim = _good_claim()
        claim["release_impact"] = None
        with self.assertRaises(bcc.ContractError):
            bcc.validate_claim(claim)


if __name__ == "__main__":
    unittest.main()
