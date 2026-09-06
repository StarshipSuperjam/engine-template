#!/usr/bin/env python3
"""Self-tests for the engine-ci gatekeeper: the mode decision, receipt provenance, and fail-closed degradation.

The load-bearing cases here are the ones that would let the frozen `engine-ci` context report success without
the inventory having run for the tree being merged — a receipt from another workflow, another pull request,
another tree, or no genuine full run at all — and the enumeration case that would silently destroy the saving
by picking a reuse run as its own evidence source.
"""
from __future__ import annotations

import datetime
import io
import json
import os
import re
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ci_gatekeeper as gk  # noqa: E402
import github_client  # noqa: E402

# The runner's control files. A test that writes one of these while it still points at the runner's REAL file
# is writing into the live job's control plane — which is exactly how StarshipSuperjam/engine-template#1043
# shipped inert: two tests in this module invoked the gate CLI with $GITHUB_ENV unset-and-inherited, so the
# self-test step appended a mode to the job environment and flipped the arm underneath a job that had already
# done the work. They are removed from the environment for the whole module; a test that needs one points it
# at its own temporary file.
_RUNNER_CONTROL_FILES = ("GITHUB_ENV", "GITHUB_OUTPUT", "GITHUB_PATH", "GITHUB_STATE", "GITHUB_STEP_SUMMARY")

# Set by the isolation regression below in the child it spawns, so the child skips that test rather than
# recursing. Deliberately NOT selftest.py's ENGINE_NESTED_SELFTEST: the canonical local launcher sets that on
# the child running the whole suite, so borrowing it would make this test skip on every local run and execute
# only in CI — feedback in the one place a round trip is expensive.
_ISOLATION_CHILD_ENV = "ENGINE_CI_GATEKEEPER_ISOLATION_CHILD"

_CONTROL_FILE_ISOLATION = None


def setUpModule():
    global _CONTROL_FILE_ISOLATION
    _CONTROL_FILE_ISOLATION = mock.patch.dict(os.environ, {}, clear=False)
    _CONTROL_FILE_ISOLATION.start()
    for var in _RUNNER_CONTROL_FILES:
        os.environ.pop(var, None)


def tearDownModule():
    if _CONTROL_FILE_ISOLATION is not None:
        _CONTROL_FILE_ISOLATION.stop()


HEAD = "a" * 40
BASE = "b" * 40
TREE = "c" * 40
REPO = "StarshipSuperjam/engine-template"
PR = 1043
COUNT, DIGEST = 177, "sha256:" + "d" * 64
NOW = datetime.datetime(2026, 8, 22, tzinfo=datetime.timezone.utc)


def event(action, *, name="pull_request", head=HEAD, number=PR):
    return {"event_name": name,
            "payload": {"action": action, "number": number,
                        "pull_request": {"number": number, "head": {"sha": head}}}}


def receipt(**over):
    base = {"schema": gk.RECEIPT_SCHEMA, "mode": "full", "result": "success", "repository": REPO,
            "pr_number": PR, "head_sha": HEAD, "base_sha": BASE, "tree_sha": TREE,
            "workflow_path": gk.WORKFLOW_PATH, "check_context": gk.CHECK_CONTEXT,
            "run_id": 900, "run_attempt": 1, "test_module_count": COUNT,
            "test_module_digest": DIGEST, "completed_at": "2026-08-21T00:00:00Z"}
    base.update(over)
    return base


def run_record(run_id=900, *, path=gk.WORKFLOW_PATH, conclusion="success", head=HEAD):
    return {"id": run_id, "path": path, "conclusion": conclusion, "head_sha": head,
            "run_attempt": 1, "html_url": f"https://example.invalid/{run_id}"}


def zipped(body) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr(gk.RECEIPT_FILENAME, json.dumps(body))
    return buf.getvalue()


def transport_for(runs, artifacts_by_run):
    """A canned Actions API: run listing then per-run artifact listing."""
    def _t(method, path, body=None):
        if "/actions/runs?" in path:
            return 200, {"workflow_runs": runs}
        for rid, arts in artifacts_by_run.items():
            if f"/actions/runs/{rid}/artifacts" in path:
                return 200, {"artifacts": arts}
        return 404, None
    return _t


ARTIFACT = [{"id": 5, "name": gk.RECEIPT_ARTIFACT_NAME, "expired": False}]


ENGINE_AFFECTING = {"schema_version": "change-classification.v1", "verdict": "engine-affecting",
                    "reason": {"code": "engine-corner-path", "detail": "fixture"},
                    "changed_paths": [".engine/tools/x.py"], "engine_paths": [".engine/tools/x.py"],
                    "project_paths": []}
PROJECT_ONLY = {"schema_version": "change-classification.v1", "verdict": "project-only",
                "reason": {"code": "project-only", "detail": "fixture"},
                "changed_paths": ["src/app.py"], "engine_paths": [], "project_paths": ["src/app.py"]}


class DecideMatrix(unittest.TestCase):
    """Every accepted event and action resolves to exactly full, reuse or project-only — never a fourth state.

    The classifier is canned here: these cases are about the EVENT rules, and a real classification would
    make them depend on this checkout's identity and history."""

    def setUp(self):
        patcher = mock.patch.object(gk, "tree_sha", return_value=TREE)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _decide(self, ev, transport=None, classifier=lambda _root: ENGINE_AFFECTING, env=None):
        return gk.decide(ev, repo=REPO, token="t", transport=transport or (lambda *a, **k: (404, None)),
                         classifier=classifier, env=env if env is not None else {})

    def test_push_to_default_branch_runs_full(self):
        # The badge witness, the default-branch telemetry signal and the integration queue all read this run.
        mode, reason, _ = self._decide({"event_name": "push", "payload": {}})
        self.assertEqual((mode, reason), (gk.MODE_FULL, gk.REASON_NOT_PULL_REQUEST))

    def test_code_actions_run_full_even_with_a_valid_receipt(self):
        t = transport_for([run_record()], {900: ARTIFACT})
        for action in ("opened", "synchronize", "reopened"):
            with self.subTest(action=action):
                mode, reason, _ = self._decide(event(action), t)
                self.assertEqual((mode, reason), (gk.MODE_FULL, gk.REASON_CODE_EVENT))

    def test_unrecognised_action_runs_full(self):
        mode, reason, _ = self._decide(event("converted_to_draft"))
        self.assertEqual((mode, reason), (gk.MODE_FULL, gk.REASON_UNRECOGNISED_ACTION))

    def test_metadata_action_with_valid_receipt_reuses(self):
        t = transport_for([run_record()], {900: ARTIFACT})
        with mock.patch.object(gk, "download_artifact", return_value=zipped(receipt())), \
             mock.patch.object(gk, "inventory_digest", return_value=(COUNT, DIGEST)), \
             mock.patch.object(gk, "_age_ok", return_value=None):
            for action in ("edited", "labeled", "unlabeled"):
                with self.subTest(action=action):
                    mode, reason, detail = self._decide(event(action), t)
                    self.assertEqual(mode, gk.MODE_REUSE)
                    self.assertIsNone(reason)
                    self.assertEqual(detail["run_id"], 900)

    def test_metadata_action_without_any_receipt_runs_full(self):
        mode, reason, _ = self._decide(event("edited"), transport_for([], {}))
        self.assertEqual((mode, reason), (gk.MODE_FULL, gk.REASON_NO_RECEIPT))

    def test_every_decision_is_one_of_three_modes(self):
        t = transport_for([], {})
        for classifier in (lambda _r: ENGINE_AFFECTING, lambda _r: PROJECT_ONLY):
            for ev in (event("edited"), event("opened"), event("labeled"), event("wat"),
                       {"event_name": "push", "payload": {}}, {"event_name": "schedule", "payload": {}}):
                mode, _, _ = self._decide(ev, t, classifier=classifier)
                self.assertIn(mode, gk.MODES)


class ProjectOnlyRoute(unittest.TestCase):
    """Route three: a deployed copy's product-only change set runs the validator alone, on any action."""

    def setUp(self):
        patcher = mock.patch.object(gk, "tree_sha", return_value=TREE)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _decide(self, ev, classifier, transport=None, env=None):
        return gk.decide(ev, repo=REPO, token="t", transport=transport or (lambda *a, **k: (404, None)),
                         classifier=classifier, env=env if env is not None else {})

    def test_a_product_only_synchronize_takes_the_project_only_arm(self):
        mode, reason, detail = self._decide(event("synchronize"), lambda _r: PROJECT_ONLY)
        self.assertEqual((mode, reason), (gk.MODE_PROJECT_ONLY, gk.REASON_PROJECT_ONLY))
        self.assertEqual(detail["classification"]["project_paths"], ["src/app.py"])

    def test_a_body_only_edit_on_a_product_only_pull_request_needs_no_receipt(self):
        # No receipt exists (a project-only run uploads one the reuse path refuses; here there is none at all),
        # and the edit is still classified project-only before the receipt rules are consulted.
        mode, reason, _ = self._decide(event("edited"), lambda _r: PROJECT_ONLY, transport_for([], {}))
        self.assertEqual((mode, reason), (gk.MODE_PROJECT_ONLY, gk.REASON_PROJECT_ONLY))

    def test_an_engine_touching_synchronize_runs_full(self):
        mode, reason, _ = self._decide(event("synchronize"), lambda _r: ENGINE_AFFECTING)
        self.assertEqual((mode, reason), (gk.MODE_FULL, gk.REASON_CODE_EVENT))

    def test_an_engine_touching_edit_with_a_full_receipt_still_reuses(self):
        t = transport_for([run_record()], {900: ARTIFACT})
        with mock.patch.object(gk, "download_artifact", return_value=zipped(receipt())), \
             mock.patch.object(gk, "inventory_digest", return_value=(COUNT, DIGEST)), \
             mock.patch.object(gk, "_age_ok", return_value=None):
            mode, _, detail = self._decide(event("edited"), lambda _r: ENGINE_AFFECTING, t)
        self.assertEqual(mode, gk.MODE_REUSE)
        self.assertEqual(detail["run_id"], 900)

    def test_a_default_branch_push_never_takes_the_route(self):
        mode, reason, _ = self._decide({"event_name": "push", "payload": {}}, lambda _r: PROJECT_ONLY)
        self.assertEqual((mode, reason), (gk.MODE_FULL, gk.REASON_NOT_PULL_REQUEST))

    def test_a_classifier_that_fails_or_doubts_falls_through_to_the_event_rules(self):
        def boom(_root):
            raise RuntimeError("no git here")
        for classifier in (boom, lambda _r: None, lambda _r: {"verdict": "engine-affecting"},
                           lambda _r: {**PROJECT_ONLY, "verdict": "not-a-merge-checkout"}):
            mode, reason, _ = self._decide(event("synchronize"), classifier)
            self.assertEqual((mode, reason), (gk.MODE_FULL, gk.REASON_CODE_EVENT))

    def test_the_repository_variable_switches_the_arm_off(self):
        mode, reason, _ = self._decide(event("synchronize"), lambda _r: PROJECT_ONLY,
                                       env={gk.PROJECT_ONLY_ARM_ENV: gk.PROJECT_ONLY_ARM_OFF})
        self.assertEqual((mode, reason), (gk.MODE_FULL, gk.REASON_CODE_EVENT))
        # Every plain spelling of "off", trimmed and in any case, disables; unset, empty, or anything
        # else leaves the arm on. The repository-variable form is typed by hand.
        for value in ("OFF ", " Off", "false", "False", "0", "no", "NO", "disabled"):
            mode, _, _ = self._decide(event("synchronize"), lambda _r: PROJECT_ONLY,
                                      env={gk.PROJECT_ONLY_ARM_ENV: value})
            self.assertEqual(mode, gk.MODE_FULL, repr(value))
        for value in ("", "on", "true", "yes", "1", "project-only"):
            mode, _, _ = self._decide(event("synchronize"), lambda _r: PROJECT_ONLY,
                                      env={gk.PROJECT_ONLY_ARM_ENV: value})
            self.assertEqual(mode, gk.MODE_PROJECT_ONLY, repr(value))

    def test_the_live_classifier_calls_this_repository_the_engines(self):
        # The home repository never takes route three: this is the one un-canned case, and it holds on the
        # real checkout whatever its history looks like.
        manifest = gk.classify_checkout(gk._repo_root())
        self.assertEqual(manifest["verdict"], "engine-affecting")
        self.assertIn(manifest["reason"]["code"], ("home-repository", "not-a-merge-checkout"))

    def test_a_project_only_receipt_is_refused_by_the_reuse_path(self):
        ok, why = gk.verify_receipt(receipt(mode=gk.MODE_PROJECT_ONLY), repo=REPO, pr_number=PR,
                                    head_sha=HEAD, expected_tree=TREE, run=run_record(), now=NOW)
        self.assertFalse(ok)
        self.assertEqual(why, "not-a-full-run-receipt")

    def test_a_project_only_receipt_is_accepted_only_when_asked_for(self):
        with mock.patch.object(gk, "inventory_digest", return_value=(COUNT, DIGEST)):
            ok, why = gk.verify_receipt(receipt(mode=gk.MODE_PROJECT_ONLY), repo=REPO, pr_number=PR,
                                        head_sha=HEAD, expected_tree=TREE, run=run_record(), now=NOW,
                                        accept_modes=gk.ACCEPT_FULL_OR_PROJECT_ONLY)
            self.assertTrue(ok, why)
            ok, why = gk.verify_receipt(receipt(mode="reuse"), repo=REPO, pr_number=PR, head_sha=HEAD,
                                        expected_tree=TREE, run=run_record(), now=NOW,
                                        accept_modes=gk.ACCEPT_FULL_OR_PROJECT_ONLY)
        self.assertFalse(ok)
        self.assertEqual(why, "receipt-mode-not-accepted:reuse")

    def test_the_receipt_carries_the_gates_mode_and_refuses_any_other(self):
        with mock.patch.object(gk, "head_and_base", return_value=(HEAD, BASE)), \
             mock.patch.object(gk, "inventory_digest", return_value=(COUNT, DIGEST)), \
             mock.patch.object(gk, "tree_sha", return_value=TREE):
            full = gk.emit_receipt(event("synchronize"), repo=REPO, env={gk.RECEIPT_MODE_ENV: "full"}, now=NOW)
            self.assertEqual(full["mode"], gk.MODE_FULL)
            self.assertIsNone(full["classification"])
            project = gk.emit_receipt(event("synchronize"), repo=REPO,
                                      env={gk.RECEIPT_MODE_ENV: gk.MODE_PROJECT_ONLY}, now=NOW,
                                      classifier=lambda _r: PROJECT_ONLY)
            self.assertEqual(project["mode"], gk.MODE_PROJECT_ONLY)
            self.assertEqual(project["classification"]["project_paths"], ["src/app.py"])
            for bad in ("", "reuse", "FULL", "project_only"):
                with self.assertRaises(gk.GatekeeperError):
                    gk.emit_receipt(event("synchronize"), repo=REPO, env={gk.RECEIPT_MODE_ENV: bad}, now=NOW)
            # A project-only receipt is refused when the checkout does not classify project-only: the mode
            # is the gate's, but the emitter still will not vouch for a verdict it cannot re-derive.
            with self.assertRaises(gk.GatekeeperError):
                gk.emit_receipt(event("synchronize"), repo=REPO, env={gk.RECEIPT_MODE_ENV: gk.MODE_PROJECT_ONLY},
                                now=NOW, classifier=lambda _r: ENGINE_AFFECTING)

    def test_the_disclosure_names_what_did_not_run_and_the_evidentiary_limit(self):
        line = gk.project_only_disclosure({"classification": PROJECT_ONLY})
        self.assertIn("NOT run", line)
        self.assertIn("src/app.py", line)
        self.assertIn("Engine health only", line)
        self.assertIn("no product validation", line)
        self.assertIn("#1147", line)
        # The words a reader trusts are exactly what the floor enforces — "owns", not the broader
        # "reads, executes, or owns" — and the count reads grammatically for one path and for many.
        self.assertIn("outside everything the Engine owns", line)
        self.assertNotIn("reads, executes", line)
        self.assertIn("1 changed path (src/app.py) lies", line)
        two = gk.project_only_disclosure({"classification": {**PROJECT_ONLY, "project_paths": ["a.py", "b.py"]}})
        self.assertIn("2 changed paths (a.py, b.py) lie outside", two)

    def test_the_decide_verb_refuses_a_project_only_run_it_cannot_disclose(self):
        # Mirrors the reuse refusal: the summary line is the whole disclosure, so no writable summary means
        # no project-only arm — the run exits 1 rather than reporting a green nobody can account for.
        with tempfile.TemporaryDirectory() as tmp:
            output = os.path.join(tmp, "output")
            with mock.patch.object(gk, "_load_event", return_value=event("synchronize")), \
                 mock.patch.object(gk, "decide", return_value=(gk.MODE_PROJECT_ONLY, gk.REASON_PROJECT_ONLY,
                                                               {"classification": PROJECT_ONLY})), \
                 mock.patch.dict(os.environ, {"GITHUB_OUTPUT": output}, clear=False):
                os.environ.pop("GITHUB_STEP_SUMMARY", None)
                self.assertEqual(gk.main(["decide"]), 1)
                summary = os.path.join(tmp, "summary")
                with mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": summary}, clear=False):
                    self.assertEqual(gk.main(["decide"]), 0)
                with open(summary, encoding="utf-8") as fh:
                    self.assertIn("Engine health only", fh.read())
            with open(output, encoding="utf-8") as fh:
                self.assertIn(f"{gk.MODE_OUTPUT_KEY}={gk.MODE_PROJECT_ONLY}", fh.read())


class FailsClosed(unittest.TestCase):
    """Any failure resolves to MORE work. No discovery failure may ever return reuse."""

    def setUp(self):
        patcher = mock.patch.object(gk, "tree_sha", return_value=TREE)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_api_error_listing_runs_runs_full(self):
        mode, reason, _ = gk.decide(event("edited"), repo=REPO, token="t",
                                    transport=lambda *a, **k: (403, None))
        self.assertEqual((mode, reason), (gk.MODE_FULL, gk.REASON_DISCOVERY_FAILED))

    def test_transport_raising_runs_full(self):
        def boom(*a, **k):
            raise RuntimeError("network gone")
        mode, reason, _ = gk.decide(event("edited"), repo=REPO, token="t", transport=boom)
        self.assertEqual((mode, reason), (gk.MODE_FULL, gk.REASON_DISCOVERY_FAILED))

    def test_unreadable_artifact_runs_full(self):
        t = transport_for([run_record()], {900: ARTIFACT})
        with mock.patch.object(gk, "download_artifact", return_value=b"not a zip"):
            mode, reason, _ = gk.decide(event("edited"), repo=REPO, token="t", transport=t)
        self.assertEqual(mode, gk.MODE_FULL)
        self.assertEqual(reason, gk.REASON_REFUSED)

    def test_unresolvable_tree_runs_full(self):
        with mock.patch.object(gk, "tree_sha", side_effect=gk.GatekeeperError("no git")):
            mode, reason, _ = gk.decide(event("edited"), repo=REPO, token="t",
                                        transport=transport_for([], {}))
        self.assertEqual((mode, reason), (gk.MODE_FULL, gk.REASON_DISCOVERY_FAILED))


class CandidateSelection(unittest.TestCase):
    """Provenance comes from platform metadata alone, and every matching run is walked."""

    def setUp(self):
        for target, value in (("tree_sha", TREE), ("inventory_digest", (COUNT, DIGEST))):
            p = mock.patch.object(gk, target, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(gk, "_age_ok", return_value=None)
        p.start()
        self.addCleanup(p.stop)

    def test_a_reuse_run_does_not_shadow_the_genuine_full_run(self):
        # THE case that would silently destroy the saving: a reuse run is itself a successful run of this
        # workflow at this head, so it matches the candidate filter. Taking only the newest match would pick
        # it, find no artifact, and fall back to a full run for every metadata event after the first.
        runs = [run_record(901), run_record(900)]          # 901 is the newer REUSE run: no artifact
        t = transport_for(runs, {901: [], 900: ARTIFACT})
        with mock.patch.object(gk, "download_artifact", return_value=zipped(receipt(run_id=900))):
            found, detail = gk.find_reusable_receipt(repo=REPO, token="t", pr_number=PR, head_sha=HEAD,
                                                     expected_tree=TREE, transport=t)
        self.assertTrue(found)
        self.assertEqual(detail["run_id"], 900)

    def test_a_run_of_another_workflow_is_never_a_candidate(self):
        # Adding a sibling workflow is only a SOFT disclosure under the guard, so a pull request could upload
        # an artifact under this exact name. The file-path filter is what refuses it.
        runs = [run_record(902, path=".github/workflows/impostor.yml")]
        t = transport_for(runs, {902: ARTIFACT})
        with mock.patch.object(gk, "download_artifact", return_value=zipped(receipt(run_id=902))):
            found, _ = gk.find_reusable_receipt(repo=REPO, token="t", pr_number=PR, head_sha=HEAD,
                                                expected_tree=TREE, transport=t)
        self.assertFalse(found)

    def test_a_failed_run_is_never_a_candidate(self):
        t = transport_for([run_record(903, conclusion="failure")], {903: ARTIFACT})
        found, _ = gk.find_reusable_receipt(repo=REPO, token="t", pr_number=PR, head_sha=HEAD,
                                            expected_tree=TREE, transport=t)
        self.assertFalse(found)

    def test_a_run_for_another_head_is_never_a_candidate(self):
        t = transport_for([run_record(904, head="f" * 40)], {904: ARTIFACT})
        found, _ = gk.find_reusable_receipt(repo=REPO, token="t", pr_number=PR, head_sha=HEAD,
                                            expected_tree=TREE, transport=t)
        self.assertFalse(found)

    def test_an_expired_artifact_is_refused(self):
        t = transport_for([run_record()], {900: [{"id": 5, "name": gk.RECEIPT_ARTIFACT_NAME, "expired": True}]})
        found, _ = gk.find_reusable_receipt(repo=REPO, token="t", pr_number=PR, head_sha=HEAD,
                                            expected_tree=TREE, transport=t)
        self.assertFalse(found)


class ReceiptVerification(unittest.TestCase):
    """One negative fixture per rejection reason. Every one must refuse, never pass."""

    def _verify(self, body, *, run=None, tree=TREE):
        with mock.patch.object(gk, "inventory_digest", return_value=(COUNT, DIGEST)):
            return gk.verify_receipt(body, repo=REPO, pr_number=PR, head_sha=HEAD,
                                     expected_tree=tree, run=run or run_record(), now=NOW)

    def test_a_matching_receipt_is_accepted(self):
        ok, why = self._verify(receipt())
        self.assertTrue(ok, why)

    def test_rejections(self):
        cases = {
            "wrong-schema": receipt(schema="something/v9"),
            "not-a-full-run-receipt": receipt(mode="reuse"),
            "wrong-repository": receipt(repository="someone/else"),
            "wrong-pull-request": receipt(pr_number=999),
            "wrong-head-commit": receipt(head_sha="9" * 40),
            "wrong-workflow-path": receipt(workflow_path=".github/workflows/impostor.yml"),
            "wrong-check-context": receipt(check_context="not-engine-ci"),
            "receipt-does-not-claim-the-run-it-was-found-on": receipt(run_id=12345),
            "different-tree": receipt(tree_sha="e" * 40),
            "receipt-does-not-record-success": receipt(result="failure"),
            "receipt-too-old": receipt(completed_at="2026-01-01T00:00:00Z"),
            "unparseable-completion-time": receipt(completed_at="last tuesday"),
            "no-completion-time": receipt(completed_at=None),
            "inventory-mismatch": receipt(test_module_digest="sha256:" + "0" * 64),
            "receipt-not-an-object": ["not", "an", "object"],
        }
        for why_expected, body in cases.items():
            with self.subTest(reason=why_expected):
                ok, why = self._verify(body)
                self.assertFalse(ok)
                self.assertEqual(why, why_expected)

    def test_a_receipt_that_is_internally_consistent_but_externally_wrong_is_refused(self):
        # The comparison must be receipt-versus-this-event, never receipt-versus-itself: a receipt whose own
        # fields agree with each other still attests a different tree.
        ok, why = self._verify(receipt(tree_sha="e" * 40, head_sha=HEAD))
        self.assertFalse(ok)
        self.assertEqual(why, "different-tree")

    def test_an_underivable_inventory_refuses_rather_than_passes(self):
        with mock.patch.object(gk, "inventory_digest", side_effect=ValueError("cannot parse")):
            ok, why = gk.verify_receipt(receipt(), repo=REPO, pr_number=PR, head_sha=HEAD,
                                        expected_tree=TREE, run=run_record(), now=NOW)
        self.assertFalse(ok)
        self.assertEqual(why, "inventory-not-re-derivable")


class TerminalAssertion(unittest.TestCase):
    """The unconditioned last step: a job where no arm ran must not report success."""

    def test_no_marker_refuses(self):
        # Both names absent is what a job whose arms all skipped would present.
        with mock.patch.dict(os.environ, {gk.FULL_RAN_ENV: "", gk.REUSE_RAN_ENV: ""}, clear=False):
            self.assertEqual(gk.main(["assert-ran"]), 1)

    def test_an_unexpected_marker_refuses(self):
        # Only the runner's own `success` counts. Anything else — a drifted value, a half-written
        # expression — is not completion.
        with mock.patch.dict(os.environ,
                             {gk.FULL_RAN_ENV: "something-else", gk.REUSE_RAN_ENV: "skipped"}, clear=False):
            self.assertEqual(gk.main(["assert-ran"]), 1)

    def test_each_real_arm_passes(self):
        skipped = {gk.FULL_RAN_ENV: "skipped", gk.REUSE_RAN_ENV: "skipped", gk.PROJECT_ONLY_RAN_ENV: "skipped"}
        for arm, env in ((gk.MODE_FULL, {**skipped, gk.FULL_RAN_ENV: "success"}),
                         (gk.MODE_REUSE, {**skipped, gk.REUSE_RAN_ENV: "success"}),
                         (gk.MODE_PROJECT_ONLY, {**skipped, gk.PROJECT_ONLY_RAN_ENV: "success"})):
            with self.subTest(arm=arm), mock.patch.dict(os.environ, env, clear=False):
                self.assertEqual(gk.main(["assert-ran"]), 0)


class TransportStaysDumb(unittest.TestCase):
    """Every trust predicate lives in the hard-tier tool, never in the shared client."""

    def test_the_client_names_no_trust_predicate(self):
        with open(github_client.__file__, "r", encoding="utf-8") as fh:
            source = fh.read()
        # Pin the specific identities a trust predicate would have to name, not ordinary English: the shared
        # client is core-owned and a future helper may legitimately mention a conclusion in passing.
        for token in (gk.WORKFLOW_PATH, gk.RECEIPT_ARTIFACT_NAME, gk.CHECK_CONTEXT, '"success"', "'success'"):
            self.assertNotIn(token, source,
                             f"{token!r} appears in github_client: a trust predicate has leaked out of the "
                             f"hard-tier gatekeeper into a soft-tier transport module")

    def test_a_redirect_to_a_foreign_host_is_followed_without_credentials(self):
        import urllib.error

        sent = {}

        class FakeResp:
            def __init__(self, body=b"payload"):
                self._body = body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, size=-1):
                # Model urllib's response: read(n) returns at most n bytes. The capped reader asks for
                # cap+1, so a body longer than the cap is what trips the guard.
                return self._body if size is None or size < 0 else self._body[:size]

        def fake_urlopen(req, timeout=None):
            sent["headers"] = dict(req.headers)
            sent["url"] = req.full_url
            return FakeResp()

        def opener_to(location):
            class Opener:
                def open(self, req, timeout=None):
                    raise urllib.error.HTTPError(
                        req.full_url, 302, "Found", {"Location": location}, None)
            return Opener()

        with mock.patch.object(github_client.urllib.request, "build_opener",
                               return_value=opener_to("https://blob.example.invalid/signed")), \
             mock.patch.object(github_client, "_urlopen", fake_urlopen):
            body = github_client.download_redirected("/repos/x/y/actions/artifacts/5/zip", "SECRET",
                                                     user_agent="ua")
        self.assertEqual(body, b"payload")
        self.assertEqual(sent["url"], "https://blob.example.invalid/signed")
        joined = " ".join(f"{k}: {v}" for k, v in sent["headers"].items())
        self.assertNotIn("SECRET", joined, "the token was forwarded to a foreign redirect target")
        self.assertNotIn("Authorization", sent["headers"])

        # A redirect to a non-https scheme (a local file, plain http) is refused before the second hop is made,
        # so the default opener's file:/ftp: handlers can never be reached through the redirect door.
        for hostile in ("file:///etc/passwd", "http://blob.example.invalid/signed", "ftp://host/x"):
            with mock.patch.object(github_client.urllib.request, "build_opener",
                                   return_value=opener_to(hostile)), \
                 mock.patch.object(github_client, "_urlopen", fake_urlopen):
                with self.assertRaises(ValueError):
                    github_client.download_redirected("/repos/x/y/actions/artifacts/5/zip", "SECRET",
                                                      user_agent="ua")

    def test_a_redirected_download_is_read_under_a_size_cap(self):
        import urllib.error

        class FakeResp:
            def __init__(self, body):
                self._body = body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, size=-1):
                return self._body if size is None or size < 0 else self._body[:size]

        def opener_to(location):
            class Opener:
                def open(self, req, timeout=None):
                    raise urllib.error.HTTPError(
                        req.full_url, 302, "Found", {"Location": location}, None)
            return Opener()

        with mock.patch.object(github_client, "_MAX_DOWNLOAD_BYTES", 4), \
             mock.patch.object(github_client.urllib.request, "build_opener",
                               return_value=opener_to("https://blob.example.invalid/signed")), \
             mock.patch.object(github_client, "_urlopen",
                               lambda req, timeout=None: FakeResp(b"0123456789")):
            with self.assertRaises(ValueError):
                github_client.download_redirected("/repos/x/y/actions/artifacts/5/zip", "SECRET",
                                                  user_agent="ua")


class CandidatePagination(unittest.TestCase):
    """The head-scoped runs listing can exceed the page budget on a long-churned PR."""

    def test_a_truncated_candidate_list_reports_a_distinct_reason(self):
        # Every page comes back full and none yields a valid receipt: the budget is exhausted with more runs
        # unread. That must resolve to a DISTINCT reason, not the same no-receipt a fresh tree gives, so the
        # silent "reuse quietly stopped working" degradation is observable.
        with mock.patch.object(gk, "_RUNS_PER_PAGE", 2), mock.patch.object(gk, "_MAX_CANDIDATE_PAGES", 2):
            full_page = [run_record(run_id=1), run_record(run_id=2)]  # candidates, but no artifacts → refused

            def t(method, path, body=None):
                if "/actions/runs?" in path:
                    return 200, {"workflow_runs": full_page}
                return 404, None  # no receipt artifact on any run

            found, detail = gk.find_reusable_receipt(
                repo=REPO, token="t", pr_number=PR, head_sha=HEAD, expected_tree=TREE, transport=t)
        self.assertFalse(found)
        self.assertTrue(detail["truncated"])
        self.assertEqual(detail["reason"], gk.REASON_CANDIDATE_LIST_TRUNCATED)

    def test_a_short_final_page_is_not_truncated(self):
        # A page shorter than the budget means we saw every run: an honest no-receipt/refused, not truncation.
        with mock.patch.object(gk, "_RUNS_PER_PAGE", 5), mock.patch.object(gk, "_MAX_CANDIDATE_PAGES", 3):
            def t(method, path, body=None):
                if "/actions/runs?" in path:
                    return 200, {"workflow_runs": [run_record(run_id=1)]}  # 1 < per_page → last page
                return 404, None

            found, detail = gk.find_reusable_receipt(
                repo=REPO, token="t", pr_number=PR, head_sha=HEAD, expected_tree=TREE, transport=t)
        self.assertFalse(found)
        self.assertFalse(detail["truncated"])
        self.assertEqual(detail["reason"], gk.REASON_REFUSED)


class ReceiptExtraction(unittest.TestCase):
    """Reading the receipt out of the downloaded artifact zip stays bounded."""

    def test_a_receipt_member_over_the_cap_is_refused(self):
        # A genuine receipt is a few hundred bytes; a member whose declared uncompressed size exceeds the cap is
        # refused before it is read into memory, so a zip-bomb entry cannot balloon before the JSON parse.
        payload = zipped({"anything": "x" * 100})
        with mock.patch.object(gk, "_MAX_RECEIPT_BYTES", 8):
            with self.assertRaises(ValueError):
                gk._extract_receipt(payload)

    def test_a_normal_receipt_extracts(self):
        payload = zipped({"schema": gk.RECEIPT_SCHEMA if hasattr(gk, "RECEIPT_SCHEMA") else "x"})
        self.assertIn("schema", gk._extract_receipt(payload))


class Disclosures(unittest.TestCase):
    """What a person is told, in the place they will look."""

    def test_reuse_disclosure_names_the_source_run_and_says_the_inventory_did_not_run(self):
        line = gk.reuse_disclosure({"run_id": 900, "run_url": "https://example.invalid/900",
                                    "receipt": receipt()})
        self.assertIn("900", line)
        self.assertIn("NOT", line)
        self.assertIn(TREE, line)

    def test_a_reuse_run_refuses_when_it_cannot_disclose_what_it_did(self):
        # The summary line is the only thing distinguishing a reuse green from a full green in the checks
        # list. A reuse that cannot say so has no account of itself, so it must not report success.
        with mock.patch.object(gk, "decide", return_value=(gk.MODE_REUSE, None, {"run_id": 900,
                                                                                 "run_url": "u",
                                                                                 "receipt": receipt()})), \
             mock.patch.object(gk, "_load_event", return_value=event("labeled")), \
             mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": ""}, clear=False):
            self.assertEqual(gk.main(["decide"]), 1)

    def test_a_reuse_run_that_can_disclose_passes(self):
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
            summary = fh.name
        self.addCleanup(os.unlink, summary)
        with mock.patch.object(gk, "decide", return_value=(gk.MODE_REUSE, None, {"run_id": 900,
                                                                                 "run_url": "u",
                                                                                 "receipt": receipt()})), \
             mock.patch.object(gk, "_load_event", return_value=event("labeled")), \
             mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": summary}, clear=False):
            self.assertEqual(gk.main(["decide"]), 0)
        with open(summary, encoding="utf-8") as fh:
            self.assertIn("900", fh.read())

    def test_the_verdict_is_published_as_a_step_output_and_never_to_the_job_environment(self):
        # The inversion of the test this replaces. The job environment is a single mutable store every later
        # step re-reads, so a verdict published there can be rewritten by anything the job runs afterwards —
        # which is precisely how the arm flipped mid-job in StarshipSuperjam/engine-template#1043. A step
        # output can be written only by the step that owns it. Both files are real here, so "the environment
        # file is untouched" is an observation rather than an absence.
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
            envfile = fh.name
        with tempfile.NamedTemporaryFile("w", suffix=".out", delete=False) as fh:
            outfile = fh.name
        self.addCleanup(os.unlink, envfile)
        self.addCleanup(os.unlink, outfile)
        with mock.patch.object(gk, "decide", return_value=(gk.MODE_FULL, gk.REASON_CODE_EVENT, None)), \
             mock.patch.object(gk, "_load_event", return_value=event("opened")), \
             mock.patch.dict(os.environ, {"GITHUB_ENV": envfile, "GITHUB_OUTPUT": outfile}, clear=False):
            self.assertEqual(gk.main(["decide"]), 0)
        with open(outfile, encoding="utf-8") as fh:
            self.assertIn(f"{gk.MODE_OUTPUT_KEY}={gk.MODE_FULL}", fh.read())
        with open(envfile, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "", "the gate must never write the job environment")

    def test_the_publisher_refuses_a_value_that_is_not_a_known_mode(self):
        # This helper is the sole author of the channel the arm decision travels on, so it validates rather
        # than trusting its caller. A newline in particular would inject a second output key.
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".out", delete=False) as fh:
            outfile = fh.name
        self.addCleanup(os.unlink, outfile)
        with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": outfile}, clear=False):
            for bad in ("", "FULL", "reuse\nmode=full", "full extra"):
                with self.assertRaises(ValueError):
                    gk._publish_mode(bad)
        with open(outfile, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "", "a refused value must reach the channel not at all")

    def test_the_branch_value_and_the_completion_markers_are_different_names(self):
        # A marker written by the decision step would prove only that the decision ran. The terminal
        # assertion must read something an ARM produced, or it cannot tell "an arm finished" from "we
        # decided". The arm evidence is the runner's own per-step outcome, carried under these two names.
        self.assertNotIn(gk.MODE_OUTPUT_KEY, (gk.FULL_RAN_ENV, gk.REUSE_RAN_ENV, gk.PROJECT_ONLY_RAN_ENV))
        self.assertEqual(len({gk.FULL_RAN_ENV, gk.REUSE_RAN_ENV, gk.PROJECT_ONLY_RAN_ENV}), 3)

    def test_the_terminal_assertion_refuses_unless_exactly_one_arm_succeeded(self):
        # `skipped` is what the runner reports for the arm whose condition was false, and the empty string is
        # what a reference that resolves to nothing yields. Neither is completion. More than one succeeding
        # means the arms stopped being mutually exclusive — work was done, but the branch structure is broken.
        cases = {
            ("success", "skipped", "skipped"): 0,
            ("skipped", "success", "skipped"): 0,
            ("skipped", "skipped", "success"): 0,
            ("skipped", "skipped", "skipped"): 1,
            ("", "", ""): 1,
            ("failure", "skipped", "skipped"): 1,
            ("cancelled", "cancelled", "cancelled"): 1,
            ("success", "success", "skipped"): 1,
            ("success", "skipped", "success"): 1,
            ("skipped", "success", "success"): 1,
            ("success", "success", "success"): 1,
        }
        for (full, reuse, project), expected in cases.items():
            with self.subTest(full=full, reuse=reuse, project=project):
                with mock.patch.dict(os.environ,
                                     {gk.FULL_RAN_ENV: full, gk.REUSE_RAN_ENV: reuse,
                                      gk.PROJECT_ONLY_RAN_ENV: project}, clear=False):
                    self.assertEqual(gk.main(["assert-ran"]), expected)

    def test_the_terminal_assertion_names_the_arm_that_ran(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {gk.FULL_RAN_ENV: "skipped", gk.REUSE_RAN_ENV: "skipped",
                                          gk.PROJECT_ONLY_RAN_ENV: "success"}, clear=False), \
             mock.patch("sys.stdout", out):
            self.assertEqual(gk.main(["assert-ran"]), 0)
        self.assertIn("project-only arm completed", out.getvalue())


class WorkflowShape(unittest.TestCase):
    """The workflow's three-arm invariant, read from the file itself.

    The gatekeeper's tests prove the decision and the assertion; this proves the YAML wires them the way the
    assertion assumes — one gate output, the full arm the negation of BOTH other modes, positive equalities on
    the other two, the receipt's mode taken from the gate, and the two validator invocations kept identical.
    A full arm that forgot one exclusion would otherwise surface only at runtime in a deployed copy, as a
    permanently red required check."""

    @classmethod
    def setUpClass(cls):
        import yaml
        path = os.path.join(gk._repo_root(), gk.WORKFLOW_PATH)
        with open(path, encoding="utf-8") as fh:
            cls.workflow = yaml.safe_load(fh)
        cls.job = cls.workflow["jobs"]["engine-ci"]
        cls.steps = {step.get("id") or step["name"]: step for step in cls.job["steps"]}

    def _condition(self, key):
        return self.steps[key].get("if", "")

    def test_the_frozen_names_are_unchanged(self):
        self.assertEqual(self.workflow["name"], gk.CHECK_CONTEXT)
        self.assertEqual(self.job["name"], gk.CHECK_CONTEXT)
        self.assertEqual(list(self.workflow["jobs"]), [gk.CHECK_CONTEXT])

    def test_the_full_arm_excludes_both_other_modes(self):
        for key in ("Run the seed validator (CI suite)", "selftests"):
            condition = self._condition(key)
            self.assertIn("steps.gate.outputs.mode != 'reuse'", condition, key)
            self.assertIn("steps.gate.outputs.mode != 'project-only'", condition, key)
            self.assertIn("&&", condition, key)

    def test_the_other_arms_are_positive_equalities_on_the_one_gate_output(self):
        self.assertEqual(self._condition("metadata"), "steps.gate.outputs.mode == 'reuse'")
        self.assertEqual(self._condition("project"), "steps.gate.outputs.mode == 'project-only'")
        for key, step in self.steps.items():
            condition = step.get("if", "")
            if "mode" in condition:
                self.assertIn("steps.gate.outputs.mode", condition, key)
                self.assertNotIn("env.", condition, key)

    def test_the_project_arm_runs_the_validator_alone_with_the_validator_steps_env(self):
        project, validator = self.steps["project"], self.steps["Run the seed validator (CI suite)"]
        self.assertIn("validate.py --suite CI", project["run"])
        self.assertNotIn("unittest", project["run"])
        self.assertEqual(project["env"], validator["env"])
        # The whole job runs exactly one unittest discovery, on the full arm.
        runners = [key for key, step in self.steps.items() if "unittest" in str(step.get("run", ""))]
        self.assertEqual(runners, ["selftests"])

    def test_the_receipt_takes_its_mode_from_the_gate_and_never_runs_on_reuse(self):
        emit, upload = self.steps["Write the receipt"], self.steps["Upload the receipt"]
        self.assertEqual(emit["env"][gk.RECEIPT_MODE_ENV], "${{ steps.gate.outputs.mode }}")
        for step in (emit, upload):
            self.assertIn("steps.gate.outputs.mode != 'reuse'", step["if"])
        self.assertEqual(upload["with"]["name"], gk.RECEIPT_ARTIFACT_NAME)
        uploads = [key for key, step in self.steps.items() if "upload-artifact" in str(step.get("uses", ""))]
        self.assertEqual(uploads, ["Upload the receipt"])

    def test_every_arms_substantive_steps_precede_the_receipt(self):
        # The receipt attests by construction only if a failure in its arm skips it: a substantive step
        # placed AFTER the receipt would let a run write and upload a full-looking receipt and then fail.
        # A reviewer found the project-only step there; this holds the order for both receipt-bearing arms.
        order = [step.get("id") or step["name"] for step in self.job["steps"]]
        write, upload = order.index("Write the receipt"), order.index("Upload the receipt")
        self.assertLess(write, upload)
        for key in ("Run the seed validator (CI suite)", "selftests", "project"):
            self.assertLess(order.index(key), write, key)
        terminal = order.index("Refuse a run in which no arm did any work")
        self.assertEqual(terminal, len(order) - 1, "the terminal assertion is the last word")
        self.assertLess(order.index("metadata"), terminal)

    def test_the_terminal_step_reads_all_three_outcomes_and_carries_no_condition(self):
        terminal = self.steps["Refuse a run in which no arm did any work"]
        self.assertNotIn("if", terminal)
        self.assertEqual(terminal["env"], {
            gk.FULL_RAN_ENV: "${{ steps.selftests.outcome }}",
            gk.REUSE_RAN_ENV: "${{ steps.metadata.outcome }}",
            gk.PROJECT_ONLY_RAN_ENV: "${{ steps.project.outcome }}",
        })

    def test_the_gate_reads_the_off_switch_and_the_checkout_fetches_the_parent(self):
        self.assertEqual(self.steps["gate"]["env"][gk.PROJECT_ONLY_ARM_ENV],
                         "${{ vars.ENGINE_CI_PROJECT_ONLY_ARM }}")
        self.assertEqual(self.steps["Check out the revision"]["with"]["fetch-depth"], 2)

    def test_full_disclosure_states_why_reuse_did_not_happen(self):
        line = gk.full_disclosure(gk.REASON_REFUSED, {"refusals": [{"run_id": 901, "why": "different-tree"}]})
        self.assertIn(gk.REASON_REFUSED, line)
        self.assertIn("different-tree", line)


@unittest.skipIf(os.environ.get(_ISOLATION_CHILD_ENV) == "1",
                 "the child this test spawns must not spawn its own")
class RunnerControlPlaneIsolation(unittest.TestCase):
    """Running this module leaves every runner control file exactly as it found it.

    The regression for StarshipSuperjam/engine-template#1043. Immutable step outputs already make the ARM
    unreachable; this holds the broader property the workflow's decoy block also defends.

    What it proves, stated precisely rather than generously: that this module's isolation is present and
    effective. The child's own `setUpModule` removes the five names before any test runs, so no individual
    test can reach the fabricated files — which means this does NOT certify that each test is independently
    well-behaved. It fails exactly when the isolation is weakened or dropped, and dropping it is how #1043
    happened, so that is the regression worth having. Confirmed by experiment rather than assumed: disabling
    the pop makes this fail on GITHUB_OUTPUT, written by the two tests that drive the `decide` verb.

    A child process rather than an in-process runner, for one reason: running this module's own suite from
    inside one of its tests would re-enter the very setUpModule/tearDownModule fixture under test while it is
    already active, nesting the patch on itself. A fresh interpreter sidesteps that, matches how CI invokes
    the suite, and costs well under a second against an inventory of several thousand tests."""

    def test_running_this_module_leaves_every_runner_control_file_untouched(self):
        import subprocess
        import tempfile

        here = os.path.dirname(os.path.abspath(__file__))
        with tempfile.TemporaryDirectory() as tmp:
            # Seeded with content, so "unchanged" is a real comparison rather than two empty files agreeing.
            before = {}
            env = {**os.environ, _ISOLATION_CHILD_ENV: "1", "PYTHONPATH": here}
            for var in _RUNNER_CONTROL_FILES:
                path = os.path.join(tmp, var.lower())
                marker = f"# {var} sentinel — the job's control plane, not a scratch file\n"
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(marker)
                before[var] = marker
                env[var] = path

            proc = subprocess.run(
                [sys.executable, "-m", "unittest", "test_ci_gatekeeper", "-v"],
                cwd=here, env=env, capture_output=True, text=True, timeout=600,
            )

            # Proof of work, not just proof of absence: a child that failed to start, crashed on import, or
            # collected nothing would leave the files untouched too and pass vacuously.
            self.assertEqual(proc.returncode, 0,
                             f"the child suite must pass\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
            match = re.search(r"^Ran (\d+) tests?", proc.stderr, re.MULTILINE)
            self.assertIsNotNone(match, f"could not read a test count from the child\n{proc.stderr}")
            self.assertGreater(int(match.group(1)), 1, "the child collected almost nothing; it proved nothing")

            for var in _RUNNER_CONTROL_FILES:
                with open(env[var], encoding="utf-8") as fh:
                    self.assertEqual(fh.read(), before[var],
                                     f"a test in this module wrote the runner's {var}")


if __name__ == "__main__":
    unittest.main()
