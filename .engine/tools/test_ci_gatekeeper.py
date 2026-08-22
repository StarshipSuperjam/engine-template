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
import sys
import unittest
import zipfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ci_gatekeeper as gk  # noqa: E402
import github_client  # noqa: E402

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


class DecideMatrix(unittest.TestCase):
    """Every accepted event and action resolves to exactly full or reuse — never a third state."""

    def setUp(self):
        patcher = mock.patch.object(gk, "tree_sha", return_value=TREE)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _decide(self, ev, transport=None):
        return gk.decide(ev, repo=REPO, token="t", transport=transport or (lambda *a, **k: (404, None)))

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

    def test_every_decision_is_one_of_two_modes(self):
        t = transport_for([], {})
        for ev in (event("edited"), event("opened"), event("labeled"), event("wat"),
                   {"event_name": "push", "payload": {}}, {"event_name": "schedule", "payload": {}}):
            mode, _, _ = self._decide(ev, t)
            self.assertIn(mode, (gk.MODE_FULL, gk.MODE_REUSE))


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
        with mock.patch.dict(os.environ, {"ENGINE_CI_RAN": ""}, clear=False):
            self.assertEqual(gk.main(["assert-ran"]), 1)

    def test_an_unexpected_marker_refuses(self):
        with mock.patch.dict(os.environ, {"ENGINE_CI_RAN": "something-else"}, clear=False):
            self.assertEqual(gk.main(["assert-ran"]), 1)

    def test_each_real_arm_passes(self):
        for arm in (gk.MODE_FULL, gk.MODE_REUSE):
            with self.subTest(arm=arm), mock.patch.dict(os.environ, {"ENGINE_CI_RAN": arm}, clear=False):
                self.assertEqual(gk.main(["assert-ran"]), 0)


class TransportStaysDumb(unittest.TestCase):
    """Every trust predicate lives in the hard-tier tool, never in the shared client."""

    def test_the_client_names_no_trust_predicate(self):
        with open(github_client.__file__, "r", encoding="utf-8") as fh:
            source = fh.read()
        for token in (gk.WORKFLOW_PATH, gk.RECEIPT_ARTIFACT_NAME, gk.CHECK_CONTEXT, "conclusion"):
            self.assertNotIn(token, source,
                             f"{token!r} appears in github_client: a trust predicate has leaked out of the "
                             f"hard-tier gatekeeper into a soft-tier transport module")

    def test_a_redirect_to_a_foreign_host_is_followed_without_credentials(self):
        import urllib.error

        sent = {}

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"payload"

        def fake_urlopen(req, timeout=None):
            sent["headers"] = dict(req.headers)
            sent["url"] = req.full_url
            return FakeResp()

        class Opener:
            def open(self, req, timeout=None):
                raise urllib.error.HTTPError(
                    req.full_url, 302, "Found",
                    {"Location": "https://blob.example.invalid/signed"}, None)

        with mock.patch.object(github_client.urllib.request, "build_opener", return_value=Opener()), \
             mock.patch.object(github_client, "_urlopen", fake_urlopen):
            body = github_client.download_redirected("/repos/x/y/actions/artifacts/5/zip", "SECRET",
                                                     user_agent="ua")
        self.assertEqual(body, b"payload")
        self.assertEqual(sent["url"], "https://blob.example.invalid/signed")
        joined = " ".join(f"{k}: {v}" for k, v in sent["headers"].items())
        self.assertNotIn("SECRET", joined, "the token was forwarded to a foreign redirect target")
        self.assertNotIn("Authorization", sent["headers"])


class Disclosures(unittest.TestCase):
    """What a person is told, in the place they will look."""

    def test_reuse_disclosure_names_the_source_run_and_says_the_inventory_did_not_run(self):
        line = gk.reuse_disclosure({"run_id": 900, "run_url": "https://example.invalid/900",
                                    "receipt": receipt()})
        self.assertIn("900", line)
        self.assertIn("NOT", line)
        self.assertIn(TREE, line)

    def test_full_disclosure_states_why_reuse_did_not_happen(self):
        line = gk.full_disclosure(gk.REASON_REFUSED, {"refusals": [{"run_id": 901, "why": "different-tree"}]})
        self.assertIn(gk.REASON_REFUSED, line)
        self.assertIn("different-tree", line)


if __name__ == "__main__":
    unittest.main()
