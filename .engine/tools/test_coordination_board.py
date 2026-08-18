#!/usr/bin/env python3
"""Tests for coordination_board — the maintained-comment read-modify-write, fingerprint dedupe, priority-aware
cap eviction, skip-malformed read, and the confinement property that only comment endpoints are touched
(StarshipSuperjam/engine-template#939)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import coordination_board as cb  # noqa: E402
import coordination_notice as cn  # noqa: E402


class FakeGitHub:
    """An in-memory GitHub comments backend. Records every (method, path) so a test can assert the board only
    ever reaches the comments endpoints (the confinement property)."""

    def __init__(self):
        self.comments = {}   # id -> {"id","body","user":{"type":"Bot"}}
        self._next = 1
        self.paths = []      # every path touched, for the confinement assertion

    def transport(self, method, path, body):
        self.paths.append((method, path))
        if method == "GET" and "/comments" in path:
            number = int(path.split("/issues/")[1].split("/")[0])
            return 200, [c for c in self.comments.values() if c["number"] == number]
        if method == "POST" and path.endswith("/comments"):
            number = int(path.split("/issues/")[1].split("/")[0])
            cid = self._next
            self._next += 1
            self.comments[cid] = {"id": cid, "number": number, "body": body["body"],
                                  "user": {"type": "Bot"}}
            return 201, self.comments[cid]
        if method == "PATCH" and "/issues/comments/" in path:
            cid = int(path.rstrip("/").split("/")[-1])
            self.comments[cid]["body"] = body["body"]
            return 200, self.comments[cid]
        raise AssertionError(f"unexpected call {method} {path}")


def _notice(**over):
    kw = dict(kind="integration-notice", event="admitted", emitter_work_ref={"pr": 5},
              audience={"pr": 5}, subject={"pr": 5}, verify_action="recheck-queue",
              now="2026-08-18T00:00:00Z", id_source=lambda: "a" * 32)
    kw.update(over)
    return cn.render(**kw)


class TestBoardRMW(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        self.client = cb._Comments("o/r", "tok", transport=self.gh.transport)

    def test_first_notice_posts_a_board(self):
        outcome = cb.post_notice(self.client, 5, _notice())
        self.assertEqual(outcome, "posted")
        self.assertEqual(len(self.gh.comments), 1)
        board = list(self.gh.comments.values())[0]
        self.assertIn(cb.BOARD_MARKER, board["body"])

    def test_identical_condition_is_deduped(self):
        cb.post_notice(self.client, 5, _notice(id_source=lambda: "a" * 32))
        outcome = cb.post_notice(self.client, 5, _notice(id_source=lambda: "b" * 32))
        # same condition (kind/event/subject/observed), different id -> same fingerprint -> deduped
        self.assertEqual(outcome, "deduped")
        self.assertEqual(len(cb.read_board(self.client, 5)), 1)

    def test_distinct_notice_edits_in_place(self):
        cb.post_notice(self.client, 5, _notice(event="admitted", id_source=lambda: "a" * 32))
        outcome = cb.post_notice(self.client, 5, _notice(event="next-in-queue", id_source=lambda: "c" * 32))
        self.assertEqual(outcome, "edited")
        self.assertEqual(len(self.gh.comments), 1)  # still ONE comment, edited not appended
        self.assertEqual(len(cb.read_board(self.client, 5)), 2)

    def test_read_board_skips_malformed(self):
        cb.post_notice(self.client, 5, _notice())
        board = list(self.gh.comments.values())[0]
        board["body"] = board["body"].replace('"admitted"', '"blocked"')  # tamper -> digest fails
        self.assertEqual(cb.read_board(self.client, 5), [])

    def test_only_comment_endpoints_touched(self):
        cb.post_notice(self.client, 5, _notice())
        cb.read_board(self.client, 5)
        for method, path in self.gh.paths:
            self.assertIn("/comments", path,
                          f"coordination touched a non-comment endpoint: {method} {path}")
            self.assertNotIn("/merge", path)
            self.assertNotIn("/labels", path)
            self.assertNotIn("/statuses", path)


class TestEviction(unittest.TestCase):
    def test_cap_and_priority(self):
        gh = FakeGitHub()
        client = cb._Comments("o/r", "tok", transport=gh.transport)
        # Post one high-priority integration notice first (oldest timestamp), then flood with low-priority
        # bounded-status notices that would evict it if priority were ignored.
        cb.post_notice(client, 5, _notice(kind="integration-notice", event="blocked",
                                          verify_action="recheck-queue",
                                          now="2026-08-18T00:00:00Z", id_source=lambda: "0" * 32))
        for i in range(1, 15):
            cb.post_notice(client, 5, _notice(
                kind="bounded-status", event="work-declared", verify_action="none",
                subject={"pr": 5, "issue": i}, emitter_work_ref={"issue": i},
                now=f"2026-08-18T00:00:{i:02d}Z", id_source=lambda i=i: f"{i:032d}"))
        board = cb.read_board(client, 5)
        self.assertLessEqual(len(board), cb.BOARD_CAP)
        kinds = [n["kind"] for n in board]
        # the high-priority integration notice survived the flood of low-priority ones
        self.assertIn("integration-notice", kinds)


if __name__ == "__main__":
    unittest.main()
