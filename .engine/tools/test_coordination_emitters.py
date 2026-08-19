#!/usr/bin/env python3
"""Tests for coordination_emitters — the no-harm guarantee (an emit never raises and never affects the
caller), the solo-repo inertness gate, and one happy path through a fake GitHub transport
(StarshipSuperjam/engine-template#939)."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import coordination_emitters as ce  # noqa: E402


class FakeGitHub:
    """In-memory GitHub: models the open-PR count (for the peer gate) and the comments (for the board)."""

    def __init__(self, open_prs=2, files=None):
        self.open_prs = open_prs
        self.files = files or {}   # pr -> [filenames]
        self.comments = {}
        self._next = 1
        self.paths = []

    def transport(self, method, path, body=None):
        self.paths.append((method, path))
        if method == "GET" and "/pulls?" in path:
            return 200, [{"number": i} for i in range(1, self.open_prs + 1)]
        if method == "GET" and "/pulls/" in path and "/files" in path:
            n = int(path.split("/pulls/")[1].split("/")[0])
            return 200, [{"filename": f} for f in self.files.get(n, [])]
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


def _all(transport):
    return [
        lambda: ce.emit_integration_admitted(transport, "o/r", 5),
        lambda: ce.emit_integration_blocked(transport, "o/r", 5),
        lambda: ce.emit_integration_next(transport, "o/r", 5),
        lambda: ce.emit_handoff(transport, "o/r", 5, "ready-for-review"),
        lambda: ce.emit_bounded_status(transport, "o/r", 5, "work-declared", paths=["a.py"]),
        lambda: ce.emit_revalidation_base_advanced(transport, "o/r", 5, base_sha="a" * 40),
        lambda: ce.emit_overlap(transport, "o/r", 5, 6, paths=["a.py"]),
    ]


class TestPokeSurfacing(unittest.TestCase):
    """The live-poke half of the doorbell: an emit that actually posts a durable notice surfaces exactly one
    fixed pointer line, drainable by the session-facing caller; a skipped/deduped emit surfaces none."""

    def setUp(self):
        ce.drain_pokes()  # isolate from any poke left by another test

    def test_a_posted_notice_surfaces_one_pointer_poke(self):
        gh = FakeGitHub(open_prs=2)
        ce.emit_integration_admitted(gh.transport, "o/r", 5)
        pokes = ce.drain_pokes()
        self.assertEqual(len(pokes), 1)
        self.assertTrue(pokes[0].startswith("engine-coordination:"))
        self.assertIn("PR #5", pokes[0])
        self.assertEqual(ce.drain_pokes(), [])  # draining is one-shot

    def test_a_deduped_re_emit_surfaces_no_new_poke(self):
        gh = FakeGitHub(open_prs=2)
        ce.emit_integration_admitted(gh.transport, "o/r", 5)
        ce.drain_pokes()
        ce.emit_integration_admitted(gh.transport, "o/r", 5)  # identical condition -> deduped, no post
        self.assertEqual(ce.drain_pokes(), [])

    def test_a_solo_skip_surfaces_no_poke(self):
        gh = FakeGitHub(open_prs=1)  # no peer -> nothing posted
        ce.emit_integration_admitted(gh.transport, "o/r", 5)
        self.assertEqual(ce.drain_pokes(), [])


class TestNoHarm(unittest.TestCase):
    def test_forced_raise_is_swallowed_for_every_emitter(self):
        gh = FakeGitHub()
        with mock.patch.object(ce, "_FORCE_RAISE", True):
            for emit in _all(gh.transport):
                self.assertIsNone(emit())

    def test_none_transport_is_a_silent_noop(self):
        for emit in _all(None):
            self.assertIsNone(emit())


class TestSoloInert(unittest.TestCase):
    def test_no_peer_writes_nothing(self):
        gh = FakeGitHub(open_prs=1)  # only this session's PR -> no peer
        self.assertIsNone(ce.emit_integration_admitted(gh.transport, "o/r", 5))
        self.assertEqual(gh.comments, {})  # the board write short-circuited, not just the doorbell


class TestHappyPath(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.mkdtemp()
        env = mock.patch.dict(os.environ, {"ENGINE_COORDINATION_CACHE_DIR": self.cache})
        env.start()
        self.addCleanup(env.stop)
        self.ledger = os.path.join(self.cache, "coordination.json")

    def test_posts_and_records(self):
        gh = FakeGitHub(open_prs=2)
        outcome = ce.emit_integration_admitted(gh.transport, "o/r", 7)
        self.assertEqual(outcome, "posted")
        board = [c for c in gh.comments.values() if c["number"] == 7]
        self.assertEqual(len(board), 1)
        import coordination_ledger as cl
        self.assertTrue(any(e["t"] == "posted" and e.get("pr") == 7 for e in cl.events(path=self.ledger)))

    def test_blocked_records_late_conflict(self):
        gh = FakeGitHub(open_prs=2)
        ce.emit_integration_blocked(gh.transport, "o/r", 7)
        import coordination_ledger as cl
        self.assertTrue(any(e["t"] == "late-conflict" for e in cl.events(path=self.ledger)))

    def test_only_comment_and_pulls_endpoints_touched(self):
        gh = FakeGitHub(open_prs=2)
        ce.emit_integration_admitted(gh.transport, "o/r", 7)
        for method, path in gh.paths:
            self.assertTrue("/comments" in path or "/pulls" in path, f"unexpected {method} {path}")
            self.assertNotIn("/merge", path)
            self.assertNotIn("/labels", path)
            self.assertNotIn("/statuses", path)


class TestScans(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.mkdtemp()
        env = mock.patch.dict(os.environ, {"ENGINE_COORDINATION_CACHE_DIR": self.cache})
        env.start()
        self.addCleanup(env.stop)

    def test_overlap_scan_posts_only_for_overlapping_peer(self):
        gh = FakeGitHub(open_prs=3, files={1: ["a.py"], 2: ["a.py"], 3: ["b.py"]})
        posted = ce.emit_overlap_scan(gh.transport, "o/r", 1, declared_paths=["a.py"])
        self.assertEqual(posted, 1)  # peer 2 overlaps, peer 3 does not
        board1 = [c for c in gh.comments.values() if c["number"] == 1]
        self.assertEqual(len(board1), 1)

    def test_revalidation_scan_excludes_self_and_fans_out(self):
        gh = FakeGitHub(open_prs=3)
        posted = ce.emit_revalidation_scan(gh.transport, "o/r", base_sha="a" * 40, exclude_pr=1)
        self.assertEqual(posted, 2)  # PRs 2 and 3, not 1
        self.assertEqual({c["number"] for c in gh.comments.values()}, {2, 3})

    def test_dependency_merged_scan_posts_only_for_overlapping_peer(self):
        gh = FakeGitHub(open_prs=3, files={1: ["a.py"], 2: ["a.py"], 3: ["b.py"]})
        posted = ce.emit_dependency_merged_scan(gh.transport, "o/r", 1, base_sha="a" * 40)
        self.assertEqual(posted, 1)  # peer 2 overlaps the merged PR 1; peer 3 does not
        self.assertEqual({c["number"] for c in gh.comments.values()}, {2})

    def test_scans_never_raise(self):
        # a transport that errors on everything -> scans return 0, never raise
        def _boom(method, path, body=None):
            raise RuntimeError("network down")
        self.assertEqual(ce.emit_overlap_scan(_boom, "o/r", 1, declared_paths=["a.py"]), 0)
        self.assertEqual(ce.emit_revalidation_scan(_boom, "o/r", base_sha="a" * 40, exclude_pr=1), 0)


if __name__ == "__main__":
    unittest.main()
