#!/usr/bin/env python3
"""The dogfood: the plan for THIS pull request, driven end to end through the coordinator it builds.

Everything else in this suite proves a verb behaves. This proves the whole thing is usable — that a
real plan, written by hand before any of this code existed, goes in one end and comes out the other
as a sealed, verifiable handoff without anyone bending the schema to fit it.

The plan below is the actual PR A plan, verbatim apart from renumbering it to revision 1 and fixing
its timestamps so the test is deterministic. It was scanned before committing for home paths, email
addresses, usernames, temp paths and credential-shaped strings, and carries none; it is a design
document about an engine, which is why it can be public at all. That scan is repeated as a test
below rather than trusted to have been done once, because the thing that makes a fixture unsafe is
usually added later.

It lives INLINE here rather than in `.engine/_fixtures/`, whose README reserves that namespace for
deliberately-broken negative inputs used to prove the engine's own checks bite. This is the
opposite: the known-good case.

What the walk proves that nothing else can. Every governance step runs through the real command, so
this is the lifecycle exercised end to end rather than the data structures shown accepting values.
The verbs it drives have no other consumer until PR B exists — nothing in PR A's own tests would otherwise exercise a review recorded against a real
multi-lens panel, a finding folded into a revision, a delta judged proportional, and a seal minted
over the result. A suite that stopped at unit tests would ship those green and unused.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import tempfile
import unittest

import plan_contract
import plan_program
import plan_projection
import plan_store


def _plan_document() -> dict:
    return json.loads(PLAN_JSON)


def _fold_in_the_review_fix(document: dict) -> dict:
    """Revision 2: the fix the cold review actually asked for.

    The review found that the plan authorized its gitignored library by citing eADR-0003, and that
    eADR-0003 in fact forbids the design twice. The fix was not to argue with the finding but to stop
    claiming inherited precedent and state the amendment openly — which is exactly the shape of a fix
    that folds in as a revision rather than sending the plan back for another panel.
    """
    revised = json.loads(json.dumps(document))
    revised["revision"] = 2
    revised["revised_at"] = "2026-08-23T22:19:44Z"
    revised["revision_note"] = (
        "Folds in the cold review's blocking finding: the eADR-0003 authorization argument was wrong, "
        "so the plan now states the amendment openly instead of claiming inherited precedent.")
    revised["deliberation"]["failure_modes"].append(
        "The library is authorized by an argument that does not survive reading the contract it cites.")
    return revised


REVIEW_FINDINGS = [
    {"id": "ARCH-B1", "lens": "architecture", "severity": "blocking",
     "summary": "The plan cites eADR-0003 as precedent for a gitignored plan library, but that contract "
                "states no store may make a gitignored derivative the only copy — it forbids the design "
                "rather than authorizing it."},
    {"id": "RISK-B1", "lens": "risk-governance", "severity": "blocking",
     "summary": "The store holds raw operator intent and would be created unignored, one `git add -A` "
                "away from being committed."},
    {"id": "FEAS-S1", "lens": "feasibility", "severity": "serious",
     "summary": "Reusing write_private_path would leave the durability obligation unmet: it uses a plain "
                "os.fsync, which is not a barrier on Darwin."},
    {"id": "PROD-N1", "lens": "product-intent", "severity": "nit",
     "summary": "`list` should say plainly that nothing on the shelf is current by default."},
]

DISPOSITIONS = {
    "ARCH-B1": ("accepted-fixed", "The plan now amends eADR-0003 openly instead of claiming precedent "
                                  "it does not have."),
    "RISK-B1": ("accepted-fixed", "The gitignore fence lands first, before any store code exists."),
    "FEAS-S1": ("accepted-fixed", "The store writes through F_FULLFSYNC with a directory fsync after "
                                  "the rename, and a test asserts both calls happen."),
    "PROD-N1": ("accepted-fixed", "`list` and the generated index both say a shelf is not a queue."),
}


class _Dogfood(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "plans"
        self.lib = plan_store.PlanLibrary(self.root)
        self.addCleanup(self._tmp.cleanup)

    def _packet_digest(self, slug):
        """The digest of the packet the coordinator would really cut for this plan's head."""
        import project_manager
        import plan_projection
        return project_manager.core.digest(
            plan_projection.render_plan(self.lib.head(slug), self.lib.read_record(slug)).encode("utf-8"))


class TheSeededPlanIsReal(_Dogfood):
    def test_it_validates_as_engine_plan_v1_unconditionally(self):
        # No escape hatch. If the hand-authored plan and the shipped schema disagree, one of them is
        # wrong and this test is how we find out — not a note explaining why it is fine.
        document = _plan_document()
        self.assertEqual(plan_contract.validate_document(document), "build-plan.v2")

    def test_it_carries_the_whole_ten_node_graph_the_build_coordinator_would_accept(self):
        document = _plan_document()
        items = document["build_plan"]["work_items"]
        self.assertEqual(len(items), 10)
        self.assertEqual(items[0]["id"], "N01-fence-and-carveout")
        # Every dependency resolves and the graph is acyclic — checked by the Build Coordinator's own
        # validator, reached through the contract, not re-expressed here.
        self.assertEqual(plan_contract.seal_blockers(document), [])

    def test_it_regenerates_byte_identically(self):
        document = _plan_document()
        slug = self.lib.create(document)
        first = plan_projection.project_plan(self.lib, slug).read_bytes()
        second = plan_projection.project_plan(self.lib, slug).read_bytes()
        self.assertEqual(first, second)
        self.assertGreater(len(first.splitlines()), 200, "the projection is implausibly short")

    def test_the_committed_fixture_carries_nothing_that_must_not_be_public(self):
        # Repeated as a test rather than trusted to have been done once: what makes a fixture unsafe
        # is almost always added after the first look.
        raw = PLAN_JSON
        for pattern, what in ((r"/Users/[^\"\\ ,]*", "a home directory path"),
                              (r"[\w.+-]+@[\w-]+\.[\w.]+", "an email address"),
                              (r"/private/tmp[^\"]*", "a temp path"),
                              (r"gh[pousr]_[A-Za-z0-9]{16,}", "a GitHub token"),
                              (r"sk-[A-Za-z0-9]{16,}", "an API key"),
                              (r"-----BEGIN [A-Z ]*PRIVATE KEY", "a private key")):
            self.assertEqual(re.findall(pattern, raw), [], f"the fixture contains {what}")

    def test_the_digests_are_stable_across_reserialization(self):
        document = _plan_document()
        again = json.loads(json.dumps(document, indent=4, sort_keys=True))
        self.assertEqual(plan_contract.document_digest(document),
                         plan_contract.document_digest(again))
        self.assertEqual(plan_contract.build_plan_digest(document),
                         plan_contract.build_plan_digest(again))


class TheFullDistance(_Dogfood):
    """import -> approve -> review -> dispose -> fold the fix in -> judge the delta -> seal."""

    def _walk(self):
        document = _plan_document()
        slug = self.lib.create(document, intake={
            "provenance": "hand-authored before the coordinator existed; the dogfood seed",
            "predecessors": ["Make Builds V2-Only and Mechanically Schedule DAG Work",
                             "the pre-approval DAG authoring revision",
                             "Local-First Plan Coordinator and DAG-Only Build Orchestration"]})

        # Every step below goes through the REAL commands, not a record written by hand. That is the
        # difference between proving the lifecycle works and proving the data structures accept being
        # filled in: the CLI carries the guardrails (preview-before-approve, one-review-per-plan,
        # the seal preconditions) and a walk that bypassed them would be exercising nothing.
        library = ["--library", str(self.root)]
        _run(library + ["preview", slug])
        _run(library + ["approve", slug, "--depth", "thorough",
                        "--operator-decision", "Approve at thorough."])

        # One cold panel, four lenses, against the approved revision — carrying the findings the real
        # review actually raised.
        findings_file = Path(self._tmp.name) / "findings.json"
        findings_file.write_text(json.dumps(REVIEW_FINDINGS), encoding="utf-8")
        _run(library + ["review", "record", slug,
                        "--lens", "architecture", "--lens", "feasibility",
                        "--lens", "product-intent", "--lens", "risk-governance",
                        # The receipt names the PACKET it read, and `review record` now re-renders and
                        # compares — the plan digest is a different thing and no longer stands in for it.
                        "--packet-digest", self._packet_digest(slug),
                        "--findings", str(findings_file),
                        # The panel ran at the effort the approved depth promises, and now says so:
                        # the record is refused without it (StarshipSuperjam/engine-template#1067).
                        "--delivered-effort", "high"])
        return slug, document

    def _dispose_all(self, slug):
        for finding in self.lib.read_record(slug)["plan_review"]["findings"]:
            disposition, rationale = DISPOSITIONS[finding["id"]]
            argv = ["--library", str(self.root), "finding", "dispose", slug,
                    "--id", finding["id"], "--disposition", disposition, "--rationale", rationale,
                    # Stated, never defaulted: the verb refuses silence, so the dogfood states it too.
                    "--does-not-block-this-pr"]
            # A BLOCKING finding that is not left blocking owes the operator a sentence they can read at
            # merge — the disclosure rule that arrived with the panel. The dogfood walks the real path, so
            # it pays the same price a real session does.
            if finding["severity"] == "blocking":
                argv += ["--operator-summary",
                         f"{finding['id']} was raised as blocking and answered before the seal: {rationale}"]
            _run(argv)
        # The seal's findings-presentation gate: the operator is shown what the panel found and what
        # was done about each. The dogfood walks the real path, so it walks this too.
        _run(["--library", str(self.root), "present-findings", slug,
              "--operator-decision", "I read all four lenses and every disposition."])

    def test_a_blocking_finding_leaves_an_editable_draft_and_no_seal(self):
        import project_manager
        slug, _ = self._walk()
        refusals = project_manager.seal_refusals(self.lib, slug)
        self.assertTrue(any("no disposition" in r for r in refusals), refusals)
        self.assertIsNone(self.lib.read_record(slug)["seal"])
        self.assertEqual(plan_store.derived_status(self.lib.read_record(slug)), "review-recorded")

    def test_the_walk_reaches_a_seal_and_records_the_delta(self):
        import project_manager
        slug, document = self._walk()
        self._dispose_all(slug)

        # Fold the review's fix in as a revision. The panel does not re-run.
        revised = _fold_in_the_review_fix(document)
        self.lib.append_revision(slug, revised, expected_revision=1)
        record = self.lib.read_record(slug)
        self.assertIsNotNone(record["plan_review"], "folding a fix must not un-review the plan")
        self.assertFalse(plan_store.approval_is_stale(record))

        # The delta needs one proportional judgment, and then the plan seals.
        self.assertEqual(project_manager.seal_refusals(self.lib, slug), [])
        out, err = _run(["--library", str(self.root), "seal", slug, "--operator-decision", "Seal it.",
                         "--delta-judgment", "scoped",
                         "--delta-rationale", "One failure mode added; the authorization argument was "
                                              "corrected, nothing in the graph moved."])
        seal = self.lib.read_record(slug)["seal"]
        self.assertEqual(seal["revision"], 2)
        self.assertNotEqual(seal["reviewed_digest"], seal["sealed_digest"])
        self.assertEqual(seal["delta_judgment"], "scoped")
        self.assertEqual(seal["build_plan_digest"],
                         plan_contract.build_plan_digest(self.lib.head(slug)))
        self.assertEqual(plan_store.derived_status(self.lib.read_record(slug)), "sealed")

    def test_the_seal_is_terminal_for_this_plan_too(self):
        import project_manager
        slug, document = self._walk()
        self._dispose_all(slug)
        self.lib.append_revision(slug, _fold_in_the_review_fix(document), expected_revision=1)
        _run(["--library", str(self.root), "seal", slug, "--operator-decision", "Seal it.", "--delta-judgment", "scoped",
              "--delta-rationale", "As above."])
        refusals = project_manager.seal_refusals(self.lib, slug)
        self.assertTrue(any("already sealed" in r for r in refusals), refusals)

    def test_the_sealed_plan_exports_and_imports_with_every_digest_verified(self):
        slug, document = self._walk()
        self._dispose_all(slug)
        self.lib.append_revision(slug, _fold_in_the_review_fix(document), expected_revision=1)
        _run(["--library", str(self.root), "seal", slug, "--operator-decision", "Seal it.", "--delta-judgment", "scoped",
              "--delta-rationale", "As above."])
        bundle = Path(self._tmp.name) / "pra.json"
        _run(["--library", str(self.root), "export", slug, "--output", str(bundle)])

        elsewhere = Path(self._tmp.name) / "elsewhere"
        _run(["--library", str(elsewhere), "import", "--bundle", str(bundle)])
        other = plan_store.PlanLibrary(elsewhere)
        imported = other.resolve(document["plan_id"])
        self.assertEqual(other.head(imported), self.lib.head(slug))
        self.assertIsNotNone(other.read_record(imported)["seal"])
        self.assertEqual(other.verify_chain(imported), [])


class ThisBuildsOwnProgram(_Dogfood):
    """PR A and PR B as a two-child program, with PR A's obligations carried into PR B."""

    PR_A_CARRIES = [
        {"id": "OB-CUTOVER", "state": "carried",
         "statement": "PR B cuts the Build Coordinator over to sealed-handoff-only entry and removes "
                      "the GitHub plan publication path."},
        {"id": "OB-PANEL-MOVE", "state": "carried",
         "statement": "PR B moves the design review panel and risk assessment out of the Build "
                      "Coordinator and deletes the retrospective plan-review waiver."},
        {"id": "OB-CANON", "state": "carried",
         "statement": "PR B amends eADR-0025 and eADR-0041 so the canon is not self-contradictory."},
        {"id": "OB-SPEC-REACCEPT", "state": "carried",
         "statement": "PR B obtains operator re-acceptance for the two settled spec documents its "
                      "cutover invalidates."},
    ]

    def _program(self):
        programs = plan_program.ProgramLibrary(self.lib)
        program_slug = programs.create(
            "Local-First Plan Coordinator",
            "A planning coordinator paired with the Build Coordinator, delivered across two PRs: the "
            "substrate lands inert in PR A, and PR B cuts over to it.")
        pr_a = _plan_document()
        pr_a["program"] = {"program_id": programs.read(program_slug)["program_id"],
                           "carried_obligations": json.loads(json.dumps(self.PR_A_CARRIES))}
        self.lib.create(pr_a)
        programs.add_child(program_slug, pr_a["plan_id"])
        return programs, program_slug, pr_a

    def test_pr_a_hands_four_enumerable_obligations_forward(self):
        programs, program_slug, _ = self._program()
        outstanding = programs.outstanding_obligations(programs.read(program_slug))
        self.assertEqual([o["id"] for o in outstanding],
                         ["OB-CANON", "OB-CUTOVER", "OB-PANEL-MOVE", "OB-SPEC-REACCEPT"])
        rendered = plan_program.render(programs, programs.read(program_slug))
        self.assertIn("sealed-handoff-only entry", rendered)

    def test_a_pr_b_that_forgets_one_is_refused_by_name(self):
        # The decay this object exists for, on this build's own program.
        programs, program_slug, pr_a = self._program()
        pr_b = _plan_document()
        pr_b["plan_id"] = "pln_b0b0b0b0b0b0"
        pr_b["title"] = "Plan Coordinator — PR B: cutover"
        pr_b["program"] = {"program_id": programs.read(program_slug)["program_id"],
                           "predecessor_plan_id": pr_a["plan_id"],
                           "carried_obligations": [
                               dict(o, state="satisfied") for o in self.PR_A_CARRIES
                               if o["id"] != "OB-SPEC-REACCEPT"]}
        self.lib.create(pr_b)
        with self.assertRaises(plan_program.ProgramError) as caught:
            programs.add_child(program_slug, pr_b["plan_id"], predecessor=pr_a["plan_id"])
        self.assertIn("OB-SPEC-REACCEPT", str(caught.exception))
        self.assertIn("re-acceptance", str(caught.exception))

    def test_a_pr_b_that_answers_for_all_four_completes_the_chain(self):
        programs, program_slug, pr_a = self._program()
        pr_b = _plan_document()
        pr_b["plan_id"] = "pln_b0b0b0b0b0b0"
        pr_b["title"] = "Plan Coordinator — PR B: cutover"
        pr_b["program"] = {"program_id": programs.read(program_slug)["program_id"],
                           "predecessor_plan_id": pr_a["plan_id"],
                           "carried_obligations": [dict(o, state="satisfied") for o in self.PR_A_CARRIES]}
        self.lib.create(pr_b)
        record = programs.add_child(program_slug, pr_b["plan_id"], predecessor=pr_a["plan_id"])
        self.assertEqual(len(record["children"]), 2)
        self.assertEqual(programs.outstanding_obligations(record), [])
        self.assertEqual(programs.derived_status(record), "in-progress")

    def test_sealing_pr_a_does_not_complete_the_program(self):
        programs, program_slug, pr_a = self._program()
        slug_a = self.lib.resolve(pr_a["plan_id"])
        digest = self.lib.read_record(slug_a)["current"]["plan_digest"]
        self.lib.update_record(slug_a, lambda r: r.update({"seal": {
            "revision": 1, "reviewed_digest": digest, "sealed_digest": digest,
            "build_plan_digest": digest, "at": "2026-08-23T23:00:00Z", "delta_judgment": "none"}}))
        self.assertEqual(programs.derived_status(programs.read(program_slug)), "in-progress")


def _run(argv):
    import contextlib
    import io
    import project_manager
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = project_manager.main(argv)
    if code != 0:
        raise AssertionError(f"{argv} exited {code}: {err.getvalue() or out.getvalue()}")
    return out.getvalue(), err.getvalue()


# The plan for this pull request, verbatim apart from the renumbering and fixed timestamps noted at
# the top of this module. Kept at the end so it does not obstruct reading the walk above.
PLAN_JSON = r"""{
  "build_plan": {
    "assumptions": [
      {
        "claim": "The storage trust model is one operator account on one workstation, on a LOCAL, non-network, non-synced filesystem. A synced or network volume breaks both fcntl locking and the compare-and-swap guarantee, so doctor detects and warns on one. Filesystem permissions are the only confidentiality mechanism; backup remains an operating-system responsibility.",
        "status": "accepted-risk"
      },
      {
        "claim": "The store retains raw operator intent across immutable revisions, in a location no secret scanner reads. A redaction path exists, but nothing prevents a secret being written in the first place, and redaction is an operator action rather than an automatic one.",
        "status": "accepted-risk"
      },
      {
        "claim": "The plan record is local-only, so no reviewer, CI check, or second machine can verify the PR's account of what was agreed against it. The PR body must disclose this plainly.",
        "status": "accepted-risk"
      },
      {
        "claim": "Landing inert changes no Build behavior, so PR A merges with the substrate amendments only; the plan-authority amendments land with the behavior in PR B.",
        "status": "verified"
      },
      {
        "claim": "Where the built schema and the hand-authored dogfood plan disagree, the schema follows reality unless the hand-authored shape is itself the defect; each accommodation is recorded as a named decision in the PR body.",
        "status": "verified"
      }
    ],
    "evidence": [
      {
        "basis": "Read build_coordinator.py cmd_plan_bind at engine-template origin/main 981d81a.",
        "claim": "Planning has no mechanical owner: the Build Coordinator becomes authoritative only at plan bind, which requires an already-open draft PR.",
        "kind": "observed"
      },
      {
        "basis": "Read build_coordinator_dag.py and build_coordinator_work.py; each guarantee carries an eADR-0041 assertion (BC-25, BC-26, BC-27) and a focused test.",
        "claim": "The v2 DAG scheduler already exists and is contract-pinned — frontier and admissibility selection, resource and glob-path conflict proving, attempt-bound claims, compare-and-swap writes, and integration-only completion.",
        "kind": "observed"
      },
      {
        "basis": "Read build_coordinator_core.StateStore.__init__ (the temp-root commonpath refusal).",
        "claim": "StateStore refuses any path outside the OS temporary directory, so a durable plan store cannot subclass it and must be a new class reusing its primitives.",
        "kind": "observed"
      },
      {
        "basis": "Read modes.py accept_handler and its wiring in .claude/settings.json.",
        "claim": "An ExitPlanMode PostToolUse hook already flips the session to Build stance on plan acceptance; it is the exact seam the Claude adapter must intercept.",
        "kind": "observed"
      },
      {
        "basis": "Read .engine/pyproject.toml dependency-groups; PyYAML 1.1 semantics are documented upstream.",
        "claim": "pyyaml is already a core dependency but implements YAML 1.1, whose silent boolean coercion and duplicate-key last-wins are unacceptable in a digest-authority path — which is why JSON is the sole plan authority.",
        "kind": "observed"
      },
      {
        "basis": "Compared PLAN.md, PLAN (1).md, and the current proposal in this session; seven groups of dropped obligations were recovered by hand.",
        "claim": "Prose plan revisions silently drop obligations: three successive revisions of this very proposal lost the checkpoint-completion bypass removal, the scheduler deferral reasons, and the v1-removal completeness list.",
        "kind": "observed"
      },
      {
        "basis": "Read mechanic_build.create_worktree and checkout_health._main_checkout / engine_common_checkout; confirmed worktrees live under the mechanic tree while their git common dir resolves to the product.",
        "claim": "Planning and Building resolve to DIFFERENT canonical roots on the owned-product arm: the mechanic session's root is engine-mechanic while the build worktree's git common dir is engine-template, so naive resolution strands the plan.",
        "kind": "observed"
      },
      {
        "basis": "Read build_coordinator.py:86-91 in the build worktree.",
        "claim": "The Build Coordinator applies THREE validation layers to a bound plan -- JSON Schema, a work-item-id uniqueness check absent from the schema, and dag.validate_dag -- so any independently written validator would diverge.",
        "kind": "observed"
      },
      {
        "basis": "Read mechanic_build.py:217-237.",
        "claim": "resolve_build_target adds a checkout-health leg returning checkout-unhealthy on a dirty tree, and its docstring states the leg is added there and only there; create_worktree deliberately avoids it.",
        "kind": "observed"
      },
      {
        "basis": "Enumerated module-level definitions in build_coordinator_core.py.",
        "claim": "The fcntl lock and the revision compare-and-swap exist only as StateStore methods, while canonical, digest and write_private_path are reusable free functions.",
        "kind": "observed"
      },
      {
        "basis": "Read memory/ledger.py:61-63 and _durable_fsync; read write_private_path.",
        "claim": "A bare os.fsync is not a durability barrier on Darwin; F_FULLFSYNC is, and the engine already homes that logic. write_private_path uses plain os.fsync because its store is deliberately non-durable.",
        "kind": "observed"
      },
      {
        "basis": "Read .engine/check/catalog-coverage.json params.",
        "claim": "catalog-coverage is a hard check reading the filesystem, and every peer gitignored runtime home is listed in its infra_dirs carve-out; .engine/plans/ is absent.",
        "kind": "observed"
      },
      {
        "basis": "Ran git status and git check-ignore in the product checkout during this session; mitigated with a local .git/info/exclude entry.",
        "claim": "The plan library sat unignored in the product checkout, visible to git status and one 'git add -A' from committing raw operator intent.",
        "kind": "observed"
      },
      {
        "basis": "Read eADR-0003 Significance in full.",
        "claim": "eADR-0003 requires a later store to declare which side of the reviewable-truth line it sits on, and states that none may make a gitignored derivative the only copy -- so it does not authorize this library and must be amended.",
        "kind": "observed"
      },
      {
        "basis": "Read docs/spec/systems/lifecycle/build-orchestration.md and docs/spec/modules/design-review.md frontmatter and body in the mechanic checkout.",
        "claim": "Two settled spec documents describe mechanisms this work replaces, each requiring the operator's recorded re-acceptance at merge under decision 0331.",
        "kind": "observed"
      },
      {
        "basis": "Searched the coordinator for a risk-assessment stage and read build_coordinator_contract.py.",
        "claim": "There is no risk-assessment stage in the Build Coordinator; risk is a narrative field of the PR contract composed by _risk_summary.",
        "kind": "observed"
      }
    ],
    "intent_source": {
      "kind": "direct"
    },
    "interpretation": "PR A of two. Deliver the engine-plan.v1 contract, the durable local plan library, the plan lifecycle command surface, and the multi-PR program object the operator directed be built here rather than deferred. PR A lands INERT: no Build path consumes it. PR B does the provider adapters, the Build Coordinator cutover, the design-panel and risk-narrative move, and the remaining governance amendments, and is itself planned through the coordinator PR A ships -- as a child of the same program.",
    "non_goals": [
      "Any change to how Builds run -- no sealed-handoff gate, no Build Coordinator cutover, no snapshot relocation in this PR.",
      "Moving the design review panel, or making engine-plan.v1's risks field the authoritative home of the PR contract's risk narrative -- both PR B.",
      "Removing build-plan.v1, plan promote, or handoff export --publish (PR B).",
      "Provider adapters, the ExitPlanMode interception, and cross-runtime resume (PR B).",
      "Team collaboration, cross-machine synchronization, cloud backup, cryptographic operator identity.",
      "Any mechanical judgment of whether a plan, a decomposition, or a program's PR split is wise.",
      "Automatic secret detection inside plan content."
    ],
    "objective": "Give planning a mechanical lifecycle owner with a durable, operator-browsable local record -- the engine-plan.v1 contract, the plan library, its command surface, and a first-class multi-PR program object -- landing inert so the Build Coordinator's behavior is unchanged until PR B.",
    "parallelism": {
      "max_concurrency": 1,
      "mode": "serial"
    },
    "profile": "normal",
    "raw_intent": "The build coordinator is starting to settle, despite the bugs being resolved regularly, but it ends up only solving half the problem. The build is only as good as the plan, and the plan is only as good as the spec. We need to take on the plan issue now, and the spec issue will be tackled in a future release. Proposal: a Local-First Plan Coordinator that pairs with the Build Coordinator, owning the planning lifecycle from raw intent through an immutable, reviewed handoff, with durable plans living only on the operator's workstation.",
    "review_strategy": "One cold plan review at the approved depth against the approved revision, run once before seal -- never re-run per revision. Fixes fold into a revision and receive a single proportional judgment with prescribed-fix termination. A full re-review has exactly one trigger: the operator judging the shape wrong and calling for a redesign. Deliverable review before submission per the Build Coordinator's normal gate.",
    "risks": [
      "The library could become an opaque ledger nobody opens -- the artifact eADR-0025 rejected. Mitigated by generated human-readable projections and now GRADED by a browsability obligation rather than asserted.",
      "Location resolution could strand a plan where the Build cannot find it. Mitigated by reusing checkout_health under a stated precedence, never the write-authorization gate, and failing closed on an ambiguous root.",
      "PR A is large, and the operator directed the program object be added to it. Mitigated by splitting the lifecycle into three separately reviewable nodes; if review judges the result oversized, the transport or program node is the intended shed to PR B rather than compressing any node.",
      "Landing inert risks dead code if PR B stalls; mitigated because the dogfood now drives the full lifecycle to a seal rather than only validating and round-tripping.",
      "Reverting PR A removes the fence but not the populated directory, leaving an un-ignored orphan on every machine that created a plan. The reverse posture leaves the fence in place, following the permission seam's precedent, and discloses the residual with the command to remove it.",
      "A secret pasted into raw intent persists across immutable revisions in a store no scanner reads; a redaction path exists but is operator-initiated."
    ],
    "schema_version": "build-plan.v2",
    "scope_boundary": [
      "The managed gitignore fence and catalog carve-out, landed before any store code.",
      "The engine-plan.v1 contract, delegating payload authority to the Build Coordinator's validator.",
      "The durable local plan store, including redaction and genuine crash-safety.",
      "Deterministic projections and the browsable shelf.",
      "The plan lifecycle command surface, split into read, governance, and transport, landing inert.",
      "The multi-PR program object with mechanically carried obligations.",
      "The inline dogfood proof driving this plan to a seal.",
      "Amendments to eADR-0003, eADR-0025 and eADR-0041, plus the new Plan Coordinator decision record."
    ],
    "spec": {
      "disclosure": "Conformance for PR A is plan-derived: the success obligations below are the acceptance referent. PR B inherits a named obligation this plan records rather than leaves to be discovered -- changing either locked document requires the operator's recorded re-acceptance at its merge, per decision 0331. An earlier revision of this plan claimed the corpus had been confirmed when it had not; that claim was false and is corrected here.",
      "posture": "none",
      "selection_basis": "Posture none because PR A changes no behavior described by any settled document: the library lands inert and no Build path consumes it. This is NOT a claim that no settled document is relevant. Two locked documents in the product's spec corpus describe mechanisms PR B will change -- docs/spec/systems/lifecycle/build-orchestration.md, which describes promoting a plan verbatim to a scope-locked Build Issue, and docs/spec/modules/design-review.md, which describes the four-lens plan-review stage roster. Both are settled under decision 0331."
    },
    "success_obligations": [
      {
        "outcome": "An operator can author and revise a plan through the coordinator's own commands, with JSON as the sole authority and no hand-edited working copy.",
        "verification": "Author and revise a plan through the command surface and assert the revision ledger and digests; assert no editable working-copy state exists."
      },
      {
        "outcome": "An operator can resume any plan across sessions and reboots, selecting it explicitly by full id, unique prefix, or slug.",
        "verification": "Save, restart the process, resume by each selector, and prove no command auto-selects the newest or only plan. Cross-runtime resume is PR B's obligation, not this PR's."
      },
      {
        "outcome": "The plan record survives the failure that motivated it: deletion of OS temporary files leaves the plan and its evidence intact, and the write path is a genuine platform durability barrier.",
        "verification": "Delete OS temp state and prove the plan resolves; assert F_FULLFSYNC where available and a directory fsync after rename."
      },
      {
        "outcome": "Every lifecycle status is derived from evidence rather than stored, and no revision can be approved, reviewed, or sealed while a decision or assumption is unresolved.",
        "verification": "Assert each enumerated status derives from evidence, with a focused refusal test per precondition on observable derived status."
      },
      {
        "outcome": "Seal is terminal and single-minted, and never seals a payload the Build Coordinator would refuse. A plan whose review found blocking problems remains an unsealed editable draft.",
        "verification": "Attempt to seal with a blocking finding and prove no seal artifact is written; attempt to seal a payload failing any of the Build Coordinator's three validation layers and prove refusal."
      },
      {
        "outcome": "Two concurrent writers cannot lose each other's work, and a stale writer changes no file.",
        "verification": "Concurrent-writer test asserting the loser receives the current digest and the tree is unchanged."
      },
      {
        "outcome": "The live library is invisible to git from before its first write, and reachable from every worktree of the product. The dogfood fixture is the deliberate tracked exception.",
        "verification": "Assert the fence renders through wiring.py in the first node, that catalog-coverage passes against a populated library, and that the library resolves identically from a main checkout, a linked worktree, and a simulated second clone."
      },
      {
        "outcome": "An operator opening the library folder with no tooling can identify every plan, its status, and its last activity from generated files alone.",
        "verification": "Assert INDEX.md lists every stored plan with derived status and rebuilds correctly after deletion."
      },
      {
        "outcome": "A partially written or corrupted revision is recovered, not merely detected, and a broken revision chain has a stated read-time rule rather than undefined behavior.",
        "verification": "Corrupt and truncate heads and delete an ancestor revision; assert recovery or a stated refusal, and that every projection rebuilds from immutable revisions alone."
      },
      {
        "outcome": "An obligation carried from one plan into a successor within a program cannot be dropped silently.",
        "verification": "Attempt to drop a carried obligation and assert refusal; assert an explicit release requires a reason and is surfaced."
      },
      {
        "outcome": "This plan itself goes the full distance through the coordinator it builds -- validated, approved, reviewed, dispositioned, and sealed -- and links forward to PR B's plan as a program.",
        "verification": "The inline dogfood test drives the seeded plan end to end to a minted seal and enumerates the carried obligations of the two-PR program."
      },
      {
        "outcome": "The PR body discloses plainly that the plan record is local-only and externally unverifiable.",
        "verification": "A body check asserts the disclosure is present, rather than leaving it to authorial memory."
      }
    ],
    "work_items": [
      {
        "depends_on": [],
        "description": "FIRST, before any code can write into the library: declare the .engine/plans/ gitignore fence as a module directive rendered through wiring.py (the memory-substrate-ledger block is the precedent, and core already declares four such wires), and add .engine/plans/ to catalog-coverage.json infra_dirs alongside every peer gitignored runtime home. The invariant this node exists to hold: the store's first write must never precede its fence. The infra_dirs edit touches .engine/check/, so this node carries a deliberate guardrail disclosure at the soft tier.",
        "exclusive_resources": [
          "managed-wiring"
        ],
        "executor_class": "integrator",
        "id": "N01-fence-and-carveout",
        "output_contract": {
          "artifact_kinds": [
            "integrated-commit"
          ],
          "deliverable": "The managed gitignore fence and catalog carve-out, landed before any store code exists",
          "required_evidence": [
            "changed_paths",
            "verification_results"
          ]
        },
        "paths": [
          ".gitignore",
          ".engine/check/catalog-coverage.json",
          ".engine/modules/"
        ],
        "verification": [
          "The fence renders through wiring.py idempotently with no hand-edited line, and reverses cleanly.",
          "catalog-coverage passes against a tree that actually contains a populated .engine/plans/.",
          "Run the weakening classifier and record its verdict here rather than discovering it in CI."
        ]
      },
      {
        "depends_on": [
          "N01-fence-and-carveout"
        ],
        "description": "engine-plan.v1 as a JSON schema with its canonicalization and digest binding: deliberation (problem frame, case against, alternatives and dispositions, failure modes, unresolved decisions), the nested exact build-plan.v2 payload, and optional program-linkage fields. JSON is the sole authority; there is no YAML working copy, no uncheckpointed-edit state, and no checkpoint verb. The nested payload is NOT re-validated by re-expressed rules: engine-plan.v1 delegates to the Build Coordinator's own authority -- core.validate against build-plan.v2, the work-item-id uniqueness check, and dag.validate_dag -- so the two coordinators can never hold different notions of a valid payload.",
        "exclusive_resources": [
          "plan-contract"
        ],
        "executor_class": "integrator",
        "id": "N02-plan-contract",
        "output_contract": {
          "artifact_kinds": [
            "integrated-commit"
          ],
          "deliverable": "engine-plan.v1 schema delegating payload authority to the Build Coordinator's validator",
          "required_evidence": [
            "changed_paths",
            "verification_results"
          ]
        },
        "paths": [
          ".engine/schemas/engine-plan.v1.json",
          ".engine/tools/plan_contract.py",
          ".engine/tools/test_plan_contract.py",
          ".engine/schemas/surface-catalog.json"
        ],
        "verification": [
          "A payload the Build Coordinator would refuse at bind is refused here, proven for each of its three layers: schema, duplicate work-item id, and DAG closure.",
          "Digest over canonical JSON is stable across key reordering and re-serialization.",
          "Structural defects are refused: missing deliberation, absent build_plan, unresolved decision."
        ]
      },
      {
        "depends_on": [
          "N02-plan-contract"
        ],
        "description": "The durable local store. Location resolves through checkout_health.engine_common_checkout and resolve_product_checkout under an explicitly stated precedence -- the verified product checkout when one is recorded, otherwise the engine common checkout -- reusing the memory ledger's de-doubling and environment-override shape. It does NOT import mechanic_build (killswitch tier) and does NOT use resolve_build_target, whose checkout-health leg would make plans unreadable on a dirty tree. Durability: F_FULLFSYNC where available with os.fsync as the floor, per the ledger's _durable_fsync, PLUS a directory fsync after os.replace so the rename itself is durable. Permissions 0700 on every directory and 0600 on every file, independent of umask. Extracts the fcntl lock and the revision compare-and-swap out of StateStore into free functions both stores ride, rather than growing a second drifting copy. Includes a redaction path able to excise a revision body while keeping the digest chain honest and visibly marked.",
        "exclusive_resources": [
          "plan-store",
          "coordinator-core"
        ],
        "executor_class": "integrator",
        "id": "N03-plan-store",
        "output_contract": {
          "artifact_kinds": [
            "integrated-commit"
          ],
          "deliverable": "Durable, lock-protected, genuinely crash-safe local store resolved to the canonical checkout",
          "required_evidence": [
            "changed_paths",
            "verification_results"
          ]
        },
        "paths": [
          ".engine/tools/plan_store.py",
          ".engine/tools/test_plan_store.py",
          ".engine/tools/build_coordinator_core.py",
          ".engine/schemas/surface-catalog.json"
        ],
        "verification": [
          "Library resolves identically from a main checkout, a linked worktree of the same clone, and a simulated cross-repo case whose common dir resolves to a second temp clone -- all constructible with git worktree add. The real mechanic topology is a named inductive gap, per this repo's convention.",
          "Refuse an ambiguous canonical root rather than silently creating a worktree-local library.",
          "A dirty product checkout does NOT make plans unreadable.",
          "Two concurrent writers cannot overwrite each other; the stale writer changes no file.",
          "Corrupt head, truncated write, deleted ancestor revision: each is detected AND recovered, or refused with a stated read-time chain-integrity rule.",
          "Directories are 0700 and files 0600 under a permissive umask.",
          "Redaction excises a body, marks it redacted, and leaves the chain verifiable.",
          "doctor detects and warns when the library sits on a synced or network volume."
        ]
      },
      {
        "depends_on": [
          "N03-plan-store"
        ],
        "description": "Deterministic PLAN.md, INDEX.md and index.json generated from immutable revisions alone. PLAN.md is marked generated-do-not-edit and carries narrative, obligations, risks, node descriptions, DAG diagram, critical path, resource conflicts and concurrency consequences. The indexes are rebuildable views, never authorities.",
        "exclusive_resources": [
          "plan-projection"
        ],
        "executor_class": "integrator",
        "id": "N04-projection",
        "output_contract": {
          "artifact_kinds": [
            "integrated-commit"
          ],
          "deliverable": "Deterministic operator-facing projections and a browsable shelf",
          "required_evidence": [
            "changed_paths",
            "verification_results"
          ]
        },
        "paths": [
          ".engine/tools/plan_projection.py",
          ".engine/tools/test_plan_projection.py",
          ".engine/schemas/surface-catalog.json"
        ],
        "verification": [
          "PLAN.md regenerates byte-identically from the same revision.",
          "Indexes rebuild solely from revisions after deletion.",
          "An operator opening the library cold can identify every plan, its status and last activity from the generated files alone.",
          "Unicode and multiline prose survive the round trip."
        ]
      },
      {
        "depends_on": [
          "N04-projection"
        ],
        "description": "The read and derive surface: init, list, show, resume, diff, validate, preview, reindex, doctor. Lifecycle status is DERIVED from evidence -- the enumerated statuses are draft, awaiting-approval, awaiting-review, review-recorded, sealed, active, complete, retired, abandoned -- with no stored phase field anywhere. Selection is by full id, unique prefix, or slug; nothing auto-selects the newest or only plan.",
        "exclusive_resources": [
          "plan-lifecycle"
        ],
        "executor_class": "integrator",
        "id": "N05-read-surface",
        "output_contract": {
          "artifact_kinds": [
            "integrated-commit"
          ],
          "deliverable": "The read and derive command surface over the store",
          "required_evidence": [
            "changed_paths",
            "verification_results"
          ]
        },
        "paths": [
          ".engine/tools/project_manager.py",
          ".engine/tools/test_project_manager.py",
          ".engine/schemas/surface-catalog.json"
        ],
        "verification": [
          "Each enumerated status is derived from evidence and none is stored.",
          "No command auto-selects a plan; an ambiguous prefix fails naming its candidates.",
          "A title change does not move the plan folder."
        ]
      },
      {
        "depends_on": [
          "N05-read-surface"
        ],
        "description": "The governance verbs: depths, approve, review packet, review record, finding dispose, seal, reopen, retire, abandon. Seal is TERMINAL and single-minted, and runs the Build Coordinator's own payload validator as a precondition. Ordering is fixed: approve, one cold review, fold fixes as revisions, one proportional delta judgment, then seal. Nothing locks before review, so a plan with blocking findings remains an unsealed editable draft -- there is no sealed-but-failed state. Review depth is offered only after the full revision has been rendered.",
        "exclusive_resources": [
          "plan-lifecycle"
        ],
        "executor_class": "integrator",
        "id": "N06-governance-verbs",
        "output_contract": {
          "artifact_kinds": [
            "integrated-commit"
          ],
          "deliverable": "Terminal, validator-gated seal and the governance verbs around it",
          "required_evidence": [
            "changed_paths",
            "verification_results"
          ]
        },
        "paths": [
          ".engine/tools/project_manager.py",
          ".engine/tools/test_project_manager.py"
        ],
        "verification": [
          "Sealing is refused with an unresolved decision or assumption, a missing review, an undispositioned finding, a stale approval, or a payload the Build Coordinator would refuse.",
          "A plan with a blocking finding stays a resumable draft with NO seal artifact written.",
          "Depth is not offered until the full revision has been rendered.",
          "One cold review per approved revision; folding fixes does not force a re-panel."
        ]
      },
      {
        "depends_on": [
          "N05-read-surface"
        ],
        "description": "revise, export, and import. revise is the sole revision-minting verb. export produces an operator-chosen local bundle carrying the exact authority and receipts; import verifies every digest and refuses plan-id collisions unless content is identical. Neither uploads anything.",
        "exclusive_resources": [
          "plan-transport"
        ],
        "executor_class": "integrator",
        "id": "N07-revision-transport",
        "output_contract": {
          "artifact_kinds": [
            "integrated-commit"
          ],
          "deliverable": "Revision minting and local-only portability",
          "required_evidence": [
            "changed_paths",
            "verification_results"
          ]
        },
        "paths": [
          ".engine/tools/project_manager.py",
          ".engine/tools/test_project_manager.py"
        ],
        "verification": [
          "Every mutating command takes and enforces the expected head; a stale writer is refused.",
          "export/import round-trips with every digest verified, and refuses a colliding non-identical id."
        ]
      },
      {
        "depends_on": [
          "N06-governance-verbs",
          "N07-revision-transport"
        ],
        "description": "The multi-PR program object, built here at the operator's direction rather than deferred: a program record owning an ordered set of child plans, each child declaring its predecessor, and obligations carried forward explicitly between them. The mechanical guarantee is narrow and honest -- an obligation declared as carried into a successor cannot be dropped silently; it is either satisfied, explicitly re-declared, or explicitly released with a reason. The program does not judge whether the decomposition into PRs is wise, and it never auto-selects a current child.",
        "exclusive_resources": [
          "plan-program"
        ],
        "executor_class": "integrator",
        "id": "N08-program-object",
        "output_contract": {
          "artifact_kinds": [
            "integrated-commit"
          ],
          "deliverable": "First-class multi-PR program record with mechanically carried obligations",
          "required_evidence": [
            "changed_paths",
            "verification_results"
          ]
        },
        "paths": [
          ".engine/schemas/engine-program.v1.json",
          ".engine/tools/plan_program.py",
          ".engine/tools/test_plan_program.py",
          ".engine/schemas/surface-catalog.json"
        ],
        "verification": [
          "An obligation carried from a parent plan into a successor cannot be dropped without an explicit release carrying a reason; the drop is refused and named.",
          "A program renders its children, their statuses, and every outstanding carried obligation.",
          "Sealing a child does not seal the program; completing every child derives program completion.",
          "The program never auto-selects a current child."
        ]
      },
      {
        "depends_on": [
          "N08-program-object"
        ],
        "description": "Close the loop for real, homed INLINE in the test module rather than in .engine/_fixtures/, whose README reserves that namespace for negative fixtures. The seeded plan for this very PR is driven the FULL distance: import, approve, record a review with findings, dispose them, judge the delta, and mint a seal. Then the program linkage is exercised as this build's own two-PR program, with PR A's plan carrying obligations forward into PR B's.",
        "exclusive_resources": [
          "plan-fixtures"
        ],
        "executor_class": "integrator",
        "id": "N09-dogfood-proof",
        "output_contract": {
          "artifact_kinds": [
            "integrated-commit",
            "test-evidence"
          ],
          "deliverable": "The plan for this PR, driven end to end through the coordinator it builds",
          "required_evidence": [
            "changed_paths",
            "verification_results"
          ]
        },
        "paths": [
          ".engine/tools/test_plan_dogfood.py"
        ],
        "verification": [
          "The seeded plan validates as engine-plan.v1 and regenerates byte-identically -- unconditionally, with no prose escape hatch.",
          "The full happy path produces a seal, proving the verbs that have no other consumer until PR B.",
          "PR A's plan and PR B's plan form a program whose carried obligations are enumerable.",
          "The committed fixture content is scrubbed of anything that must not be public."
        ]
      },
      {
        "depends_on": [
          "N09-dogfood-proof"
        ],
        "description": "Record the decisions and regenerate derived surfaces last. Amend eADR-0003 with a dated, scoped paragraph carving out the only-copy clause for a per-instance planning store, discharging that contract's own obligation that a later store declare which side of the reviewable-truth line it sits on. Amend eADR-0025 and eADR-0041 with dated paragraphs scoped to the substrate existing and being unconsumed, with plan authority unchanged until PR B, so the canon is never self-contradictory in the merged tree. Add the new Plan Coordinator eADR whose anti-choice engages eADR-0025's ACTUAL position -- session-held plans promoted only for cold continuation -- not a decoy. Regenerate the knowledge graph, self-map and module-surface inventory LAST.",
        "exclusive_resources": [
          "governance",
          "generated-surfaces"
        ],
        "executor_class": "integrator",
        "id": "N10-governance-record",
        "output_contract": {
          "artifact_kinds": [
            "integrated-commit",
            "decision-record"
          ],
          "deliverable": "Amended canon with no self-contradiction, plus the new decision record",
          "required_evidence": [
            "changed_paths",
            "verification_results"
          ]
        },
        "paths": [
          ".engine/contracts/",
          ".engine/knowledge/",
          ".engine/self-map.md"
        ],
        "verification": [
          "No two accepted contracts in the merged tree contradict each other on plan authority.",
          "The new record's anti-choice engages the incumbent position it carves out from.",
          "Generated surfaces regenerate clean from the final reconciled tree."
        ]
      }
    ]
  },
  "created_at": "2026-08-23T21:43:34Z",
  "deliberation": {
    "alternatives": [
      {
        "disposition": "rejected",
        "option": "Keep plans in GitHub Issue machine blocks, as the current promote path does.",
        "reason": "The operator's own objection is decisive: good plans are far too large for comments, and data blocks create noise only one operator can decode. It also makes cold continuation depend on network reachability for something that is purely local working material."
      },
      {
        "disposition": "rejected",
        "option": "Leave the plan in the provider's native harness, as today.",
        "reason": "Native plan artifacts are single throwaway documents with no revisions, no diffs, and no review receipts, and they are provider-specific — so they cannot carry a plan across runtimes or detect that a revision dropped an obligation."
      },
      {
        "disposition": "rejected",
        "option": "Make plan.yaml the editable authority, with a restricted YAML 1.2 subset.",
        "reason": "The only available YAML library implements YAML 1.1, whose silent boolean coercion, duplicate-key last-wins, and anchor support are all silent semantic corruption in the file whose digest IS the plan's authority. Enforcing the stated subset means building a custom loader in the most trust-critical path. JSON-only authority removes the loader, the formatting-versus-semantic digest distinction, and the uncheckpointed-working-copy state machinery at once."
      },
      {
        "disposition": "rejected",
        "option": "Deliver the whole redesign — coordinator, adapters, cutover, governance — as one PR.",
        "reason": "That is the shape that killed the first Build Coordinator implementation. Splitting lets PR A land inert and small, and lets PR B be planned and executed THROUGH the coordinator PR A ships, which is the real dogfood."
      },
      {
        "disposition": "rejected",
        "option": "Re-run the cold plan review on every material revision, as the proposal originally specified.",
        "reason": "That rebuilds the review death spiral the Build side just removed. If the orchestrating AI cannot carry a plan revision with the operator in the loop, the plan is not buildable and more cold reviews will not fix it. One review, pre-seal, with a proportional judgment on the fix delta."
      },
      {
        "disposition": "rejected",
        "option": "Keep a committed copy of the sealed plan so the gitignored store is not the only copy.",
        "reason": "It would satisfy eADR-0003 and eADR-0025 without amendment and make the plan reviewable at merge. Rejected by the operator: it cuts against the local-first requirement for the one artifact that matters most, and sealed plans are large. The amendment is the honest cost."
      },
      {
        "disposition": "rejected",
        "option": "Argue a plan library is experiential per-instance data like the memory ledger.",
        "reason": "Deliberation, alternatives and an operator's approval are reviewable truth under eADR-0003's own dichotomy. Claiming otherwise would be arguing for a conclusion rather than from the text."
      },
      {
        "disposition": "rejected",
        "option": "Defer the multi-PR program object to a later release, as an earlier revision did.",
        "reason": "Rejected by the operator after the product-intent lens showed the deferral was silent and unexplained, and that this build's own two-PR delivery is exactly the case being deferred. Built here instead."
      }
    ],
    "case_against": "The strongest case against is that eADR-0025 and eADR-0041 already decided this the other way, deliberately, reasoned against a real failure where a private receipt chain produced recursive audits without improving any pull request. A gitignored local library is structurally the kind of object that reasoning rejected, and it buys durability at the cost of a record no reviewer, no CI check, and no second machine can see: if the store drifts from the PR's account of what was agreed, nothing external catches it. An earlier revision of this plan answered that by citing eADR-0003 as precedent. THAT ARGUMENT WAS WRONG, and the cold review caught it: eADR-0003 requires a later store to declare which side of the reviewable-truth line it sits on and states plainly that none may make a gitignored derivative the only copy. A plan -- deliberation, alternatives, obligations, an operator's approval -- is reviewable truth by that contract's own dichotomy, and this design makes the gitignored store the only copy. So the authorization is not inherited from precedent; it is an open amendment to eADR-0003, made deliberately, with the cost stated: cross-session recovery becomes workstation-only, and the PR body must disclose that its account of what was agreed cannot be externally verified.",
    "failure_modes": [
      "Location resolution picks the calling worktree or a write-authorization gate, so plans are unreachable either always or whenever the checkout is dirty.",
      "The library becomes an opaque ledger the operator never opens -- now graded by an obligation.",
      "A partially written revision leaves a corrupt head that blocks resume -- now graded for RECOVERY, not merely detection.",
      "Seal semantics drift into a lock-then-review order, producing a sealed-but-failed limbo state.",
      "A sealed plan carries a payload the Build Coordinator refuses at bind, undiscoverable until PR B and unfixable because a seal cannot be re-minted.",
      "The store writes plan content before its fence exists, exposing raw operator intent to a commit.",
      "PR A grows past reviewable size now that the program object is in it.",
      "Landing inert becomes landing dead if PR B stalls."
    ],
    "problem_frame": "The Build Coordinator has been stabilizing through repeated defect fixes, but it only governs execution. Nothing mechanical owns the plan that execution consumes: the coordinator becomes authoritative at plan bind, which already requires an open draft PR, so grounding, deliberation, DAG authoring, and presentation are convention a session is trusted to remember. Two failures follow. Plans are not durable — coordinator state has been lost to OS temporary-file cleanup across a reboot — and plans decay silently under revision, as three successive drafts of this very proposal demonstrated by dropping obligations no mechanism noticed.",
    "unresolved_decisions": []
  },
  "intent": {
    "interpretation": "PR A of two. Deliver the engine-plan.v1 contract, the durable local plan library, and the complete plan lifecycle command surface — landing INERT, changing nothing about how Builds run today. PR B then performs the provider-adapter work, the Build Coordinator cutover, the design-panel move, and the governance amendments, and is itself planned through the coordinator PR A ships.",
    "raw": "The build coordinator is starting to settle, despite the bugs being resolved regularly, but it ends up only solving half the problem. The build is only as good as the plan, and the plan is only as good as the spec. We need to take on the plan issue now, and the spec issue will be tackled in a future release. Proposal: a Local-First Plan Coordinator that pairs with the Build Coordinator, owning the planning lifecycle from raw intent through an immutable, reviewed handoff, with durable plans living only on the operator's workstation.",
    "source": {
      "kind": "direct"
    }
  },
  "operator_decisions": [
    {
      "decision": "Two staged PRs, not one integrated delivery.",
      "recorded": "2026-08-23T21:43:34Z"
    },
    {
      "decision": "JSON-only plan authority; plan.yaml is dropped entirely.",
      "recorded": "2026-08-23T21:43:34Z"
    },
    {
      "decision": "Accepting a native Claude or Codex plan imports it as an unapproved draft revision — groundwork, never a bypass and never a restart. Gaps are marked as gaps, never fabricated.",
      "recorded": "2026-08-23T21:43:34Z"
    },
    {
      "decision": "One cold plan review, run once pre-seal. Revisions churn freely before approval. A full re-review has exactly one trigger: the operator saying the shape is wrong and it should be scrapped and redesigned.",
      "recorded": "2026-08-23T21:43:34Z"
    },
    {
      "decision": "Seal is the terminal act. Approve, review once, fold fixes, judge the delta, then seal. Nothing locks before review, so no sealed-but-failed state exists.",
      "recorded": "2026-08-23T21:43:34Z"
    },
    {
      "decision": "The design review panel and risk assessment move to the Plan Coordinator and are removed from the Build Coordinator, which keeps only the deliverable review. Lands in PR B.",
      "recorded": "2026-08-23T21:43:34Z"
    },
    {
      "decision": "Build-time plan revisions are continuous improvement, not collapse: the plan panel does not re-run, unchanged nodes keep their evidence, and only an operator scrap-and-redesign returns the Build to planning.",
      "recorded": "2026-08-23T21:43:34Z"
    },
    {
      "decision": "eADR-0003's only-copy clause is amended openly in PR A rather than relying on a precedent that does not fit; the design stays local-first and nothing large is published to GitHub.",
      "recorded": "2026-08-23T22:19:44Z"
    },
    {
      "decision": "The multi-PR program object is BUILT IN PR A rather than deferred, meeting the stated need for multi-phase program builds directly.",
      "recorded": "2026-08-23T22:19:44Z"
    }
  ],
  "plan_id": "pln_e910c2029ffe",
  "revised_at": "2026-08-23T21:43:34Z",
  "revision": 1,
  "revision_note": "The plan as approved for PR A, after the cold review and the operator corrections.",
  "schema_version": "engine-plan.v1",
  "title": "Local-First Plan Coordinator — PR A: contract, store, lifecycle, and program object"
}"""


if __name__ == "__main__":
    unittest.main()
