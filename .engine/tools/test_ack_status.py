#!/usr/bin/env python3
"""Tests for ack_status.py — the head-binding acknowledgment companion (StarshipSuperjam/engine-template#710).

Drives ack_status.main() over crafted event payloads with the GitHub transport faked, and asserts:
  - a `labeled` event with the `guardrail-ack` label posts `engine-ack=success` to the CURRENT head SHA;
  - any other label posts nothing;
  - a `synchronize` event removes the stale label (and tolerates a 404 — no label present);
  - it reads only the event and the token — it never checks out or reads PR-head code.
The real transport (`github_client.json_request` -> `_urlopen`) is exercised once end-to-end to prove the
POST body shape; the rest fake `json_request` to record the exact calls.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ack_status  # noqa: E402
import github_client  # noqa: E402


class TestAckStatus(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        self._json_request = github_client.json_request
        self._urlopen = github_client._urlopen
        self._tmp = tempfile.mkdtemp(prefix="engine-ack-status-")

    def tearDown(self):
        import shutil
        os.environ.clear()
        os.environ.update(self._env)
        github_client.json_request = self._json_request
        github_client._urlopen = self._urlopen
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _run(self, event, *, responses=None, repo="o/r", token="t0ken"):
        """Run main() over `event` with json_request faked. `responses` is a list of (status, data) returned
        in order (default (201, None)). Returns (rc, recorded_calls)."""
        path = os.path.join(self._tmp, "event.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(event, fh)
        os.environ["GITHUB_EVENT_PATH"] = path
        if repo is not None:
            os.environ["GITHUB_REPOSITORY"] = repo
        else:
            os.environ.pop("GITHUB_REPOSITORY", None)
        if token is not None:
            os.environ["GITHUB_TOKEN"] = token
        else:
            os.environ.pop("GITHUB_TOKEN", None)
        queue = list(responses or [])
        calls = []

        def fake(method, api_path, tok, *, user_agent, body=None):
            calls.append({"method": method, "path": api_path, "body": body, "ua": user_agent, "token": tok})
            return queue.pop(0) if queue else (201, None)

        github_client.json_request = fake
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = ack_status.main()
        return rc, calls

    # ---- labeled: post the head-bound status ----

    def test_labeled_with_ack_posts_engine_ack_success_to_head(self):
        rc, calls = self._run({
            "action": "labeled", "label": {"name": "guardrail-ack"},
            "pull_request": {"number": 7, "head": {"sha": "deadbeef"}}})
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        c = calls[0]
        self.assertEqual(c["method"], "POST")
        self.assertEqual(c["path"], "/repos/o/r/statuses/deadbeef")
        self.assertEqual(c["body"]["context"], "engine-ack")
        self.assertEqual(c["body"]["state"], "success")
        self.assertEqual(c["ua"], "engine-ack-status")

    def test_labeled_with_a_different_label_posts_nothing(self):
        rc, calls = self._run({
            "action": "labeled", "label": {"name": "needs-review"},
            "pull_request": {"number": 7, "head": {"sha": "deadbeef"}}})
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [])

    def test_unlabeled_ack_posts_engine_ack_failure_to_head(self):
        # Removing the ack label is a deliberate withdrawal: post engine-ack=failure so the guard re-blocks
        # the same head (a commit status cannot be deleted, only overwritten).
        rc, calls = self._run({
            "action": "unlabeled", "label": {"name": "guardrail-ack"},
            "pull_request": {"number": 7, "head": {"sha": "deadbeef"}}})
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        c = calls[0]
        self.assertEqual(c["method"], "POST")
        self.assertEqual(c["path"], "/repos/o/r/statuses/deadbeef")
        self.assertEqual(c["body"]["context"], "engine-ack")
        self.assertEqual(c["body"]["state"], "failure")

    def test_unlabeled_with_a_different_label_posts_nothing(self):
        rc, calls = self._run({
            "action": "unlabeled", "label": {"name": "needs-review"},
            "pull_request": {"number": 7, "head": {"sha": "deadbeef"}}})
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [])

    def test_labeled_ack_without_head_sha_fails_visibly(self):
        rc, calls = self._run({
            "action": "labeled", "label": {"name": "guardrail-ack"},
            "pull_request": {"number": 7}})
        self.assertEqual(rc, 1)          # a visible failure — the guard will fail closed until the status posts
        self.assertEqual(calls, [])

    def test_labeled_post_failure_is_visible(self):
        rc, calls = self._run({
            "action": "labeled", "label": {"name": "guardrail-ack"},
            "pull_request": {"number": 7, "head": {"sha": "abc"}}}, responses=[(500, None)])
        self.assertEqual(rc, 1)
        self.assertEqual(len(calls), 1)

    # ---- synchronize: clear the stale label (UX only) ----

    def test_synchronize_removes_the_stale_label(self):
        rc, calls = self._run({
            "action": "synchronize",
            "pull_request": {"number": 7, "head": {"sha": "newhead"}}})
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        c = calls[0]
        self.assertEqual(c["method"], "DELETE")
        self.assertEqual(c["path"], "/repos/o/r/issues/7/labels/guardrail-ack")

    def test_synchronize_tolerates_missing_label_404(self):
        rc, calls = self._run({
            "action": "synchronize", "pull_request": {"number": 7, "head": {"sha": "newhead"}}},
            responses=[(404, None)])
        self.assertEqual(rc, 0)          # no label to clear is the common case, not a failure

    def test_synchronize_label_removal_error_is_visible(self):
        rc, calls = self._run({
            "action": "synchronize", "pull_request": {"number": 7, "head": {"sha": "newhead"}}},
            responses=[(500, None)])
        self.assertEqual(rc, 1)

    # ---- non-events and missing context ----

    def test_other_action_does_nothing(self):
        rc, calls = self._run({"action": "opened", "pull_request": {"number": 7, "head": {"sha": "abc"}}})
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [])

    def test_no_pull_request_context_does_nothing(self):
        rc, calls = self._run({"action": "labeled", "label": {"name": "guardrail-ack"}})
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [])

    def test_missing_token_does_nothing(self):
        rc, calls = self._run({
            "action": "labeled", "label": {"name": "guardrail-ack"},
            "pull_request": {"number": 7, "head": {"sha": "abc"}}}, token=None)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [])

    # ---- end-to-end through the real transport (proves the POST body serializes correctly) ----

    def test_post_body_shape_through_real_transport(self):
        captured = {}

        class _Resp:
            status = 201

            def read(self):
                return b""

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["body"] = req.data
            return _Resp()

        github_client._urlopen = fake_urlopen
        path = os.path.join(self._tmp, "event.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"action": "labeled", "label": {"name": "guardrail-ack"},
                       "pull_request": {"number": 3, "head": {"sha": "cafe"}}}, fh)
        os.environ.update({"GITHUB_EVENT_PATH": path, "GITHUB_REPOSITORY": "o/r", "GITHUB_TOKEN": "t"})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = ack_status.main()
        self.assertEqual(rc, 0)
        self.assertTrue(captured["url"].endswith("/repos/o/r/statuses/cafe"))
        self.assertEqual(captured["method"], "POST")
        sent = json.loads(captured["body"].decode())
        self.assertEqual(sent["context"], "engine-ack")
        self.assertEqual(sent["state"], "success")


if __name__ == "__main__":
    unittest.main()
