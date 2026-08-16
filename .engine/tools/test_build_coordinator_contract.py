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
import spec_referent
import validate

ROOT = bcc.ROOT


def _good_claim() -> dict:
    return {
        "schema_version": "pr-body-claim.v1",
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
            "caveats": ["One unrelated flake in an unchanged module, not implicated by this diff."],
            "live_helpers": {"all_available": True},
        },
        "review": {
            "loop_narrative": ["One clean cold pass; no repairs, so reviewed and final commits match."],
            "material_divergence": False,
            "finding_summaries": [],
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
        "index_regen": "Regeneration touched only generated index paths; 2 files changed, no authored work lost.",
        "fail_open_lines": [],
        "spec_steps": "**Things I checked for you**\n_(engine's side)_\n- All criteria: automated tests.",
        "review_coverage": "thorough; plan review (4 lenses) ran before build, five cold lenses after.",
        "disagreement_lines": [],
        "drift_line": "reviewed and submitted commits are identical; no post-review divergence.",
        "guardrail_line": "- Guardrail touch: none floored beyond a byte-identical check rule.",
        "composition_marker": "<!-- engine-pr-contract:v1 sha256:deadbeef commit=abc1234 -->",
        "preserved_blocks": ["<!-- engine-build-handoff:v2 sha256:cafe -->\npreserved\n<!-- /engine-build-handoff:v2 -->"],
    }


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
            mock.patch.object(bc.contract, "load_claim", return_value=_good_claim()),
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


if __name__ == "__main__":
    unittest.main()
