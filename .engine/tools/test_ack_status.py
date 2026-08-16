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
import protection_guard  # noqa: E402

_UNSET = object()  # "no manifest override" sentinel, distinct from manifest=None (an unreadable manifest)


class TestAckStatus(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        self._json_request = github_client.json_request
        self._urlopen = github_client._urlopen
        self._pg_engine_dir = protection_guard._ENGINE_DIR
        self._tmp = tempfile.mkdtemp(prefix="engine-ack-status-")

    def tearDown(self):
        import shutil
        os.environ.clear()
        os.environ.update(self._env)
        github_client.json_request = self._json_request
        github_client._urlopen = self._urlopen
        protection_guard._ENGINE_DIR = self._pg_engine_dir
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _run(self, event, *, responses=None, repo="o/r", token="t0ken",
             manifest=_UNSET, actor=None):
        """Run main() over `event` with json_request faked. `responses` is a list of (status, data) returned
        in order (default (201, None)). Returns (rc, recorded_calls).

        `manifest` controls the committed base manifest the labeler-authority read sees (via the REAL
        protection_guard.resolve_labeler_authority — not a stub): the default is a SOLO manifest (hermetic, not
        the ambient checkout); pass a dict for a team/other manifest; pass None to simulate an ABSENT/unreadable
        manifest (no engine.json written). `actor` sets GITHUB_ACTOR, to prove the writer reads the frozen
        event `sender` and NEVER the (re-run-spoofable) actor env."""
        eng_dir = os.path.join(self._tmp, "engine")
        os.makedirs(eng_dir, exist_ok=True)
        protection_guard._ENGINE_DIR = eng_dir
        manifest = {"identity": "solo", "home_repository": "o/r"} if manifest is _UNSET else manifest
        if manifest is not None:
            with open(os.path.join(eng_dir, "engine.json"), "w", encoding="utf-8") as fh:
                json.dump(manifest, fh)
        path = os.path.join(self._tmp, "event.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(event, fh)
        os.environ["GITHUB_EVENT_PATH"] = path
        if actor is not None:
            os.environ["GITHUB_ACTOR"] = actor
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

    # ---- labeler authority (#958): who applied the label decides whether a success is minted ----

    _TEAM = {"identity": "team", "engine_identity": {"login": "engine-bot"}, "home_repository": "o/r"}

    def _labeled(self, sender=_UNSET, **kw):
        ev = {"action": "labeled", "label": {"name": "guardrail-ack"},
              "pull_request": {"number": 7, "head": {"sha": "deadbeef"}}}
        if sender is not _UNSET:
            ev["sender"] = sender
        return self._run(ev, **kw)

    def test_solo_labeled_posts_success_annotated_shared_credential(self):
        rc, calls = self._labeled(sender={"login": "alice", "type": "User"})  # default manifest = solo
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0]["body"]["state"], "success")
        self.assertIn("[shared credential]", calls[0]["body"]["description"])
        self.assertIn("@alice", calls[0]["body"]["description"])

    def test_team_distinct_operator_posts_success_annotated_operator(self):
        rc, calls = self._labeled(sender={"login": "alice", "type": "User"}, manifest=self._TEAM)
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0]["body"]["state"], "success")
        self.assertIn("[operator]", calls[0]["body"]["description"])
        self.assertIn("@alice", calls[0]["body"]["description"])

    def test_team_engine_identity_is_refused_with_failure(self):
        # The core threat: the engine's OWN identity applies the label to self-ack. Must refuse (post failure).
        rc, calls = self._labeled(sender={"login": "engine-bot", "type": "User"}, manifest=self._TEAM)
        self.assertEqual(rc, 0)  # posting the refusal IS the intended outcome, not an error
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["body"]["state"], "failure")
        self.assertIn("engine's own identity", calls[0]["body"]["description"])

    def test_team_engine_identity_match_is_case_insensitive(self):
        rc, calls = self._labeled(sender={"login": "Engine-Bot", "type": "User"}, manifest=self._TEAM)
        self.assertEqual(calls[0]["body"]["state"], "failure")

    def test_team_bot_sender_is_refused(self):
        rc, calls = self._labeled(sender={"login": "some-app[bot]", "type": "Bot"}, manifest=self._TEAM)
        self.assertEqual(calls[0]["body"]["state"], "failure")
        self.assertIn("user account", calls[0]["body"]["description"])

    def test_team_missing_sender_is_refused(self):
        rc, calls = self._labeled(manifest=self._TEAM)  # no sender in the payload
        self.assertEqual(calls[0]["body"]["state"], "failure")

    def test_team_manifest_without_engine_identity_fails_closed(self):
        # team recorded but no distinct identity on record -> the comparand would be empty -> fail closed.
        rc, calls = self._labeled(sender={"login": "alice", "type": "User"},
                                  manifest={"identity": "team", "home_repository": "o/r"})
        self.assertEqual(calls[0]["body"]["state"], "failure")
        self.assertIn("no distinct engine identity", calls[0]["body"]["description"])

    def test_unreadable_manifest_fails_closed(self):
        # an absent/unreadable base manifest -> the labeler's authority cannot be judged -> refuse.
        rc, calls = self._labeled(sender={"login": "alice", "type": "User"}, manifest=None)
        self.assertEqual(calls[0]["body"]["state"], "failure")
        self.assertIn("could not be read", calls[0]["body"]["description"])

    def test_github_actor_env_is_ignored_only_frozen_sender_counts(self):
        # Re-run spoof mirror: the writer reads the frozen event `sender`, never GITHUB_ACTOR. Here the sender
        # is the engine identity (must refuse) while GITHUB_ACTOR is a distinct operator — if the code read the
        # actor it would wrongly accept. It must refuse, proving actor is not consulted.
        rc, calls = self._labeled(sender={"login": "engine-bot", "type": "User"},
                                  manifest=self._TEAM, actor="alice")
        self.assertEqual(calls[0]["body"]["state"], "failure")

    def test_unlabeled_withdrawal_is_authority_free_even_from_engine_identity(self):
        # Withdrawal blocks regardless of who did it (fail-safe): even the engine identity removing the label
        # posts failure (re-blocks) — an agent can DoS-withdraw consent, never forge it.
        rc, calls = self._run({"action": "unlabeled", "label": {"name": "guardrail-ack"},
                               "sender": {"login": "engine-bot", "type": "User"},
                               "pull_request": {"number": 7, "head": {"sha": "deadbeef"}}},
                              manifest=self._TEAM)
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0]["body"]["state"], "failure")

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
