"""Tests for the serialized cross-PR integration coordinator."""

import os
import re
import unittest
import urllib.parse
from unittest import mock

import integration_queue as iq
import integration_queue_backend as be
import protection_guard


class _FakeGH:
    """A fake GitHub transport over in-memory PRs. Each PR: {number, head_sha, base_sha, labels(set),
    draft, title, reviews([...]), checks({name:conclusion})}. Also serves the ref, rules, and label writes."""

    def __init__(self, prs, *, head_sha="MAIN", rules=None):
        self.prs = {p["number"]: {**p, "labels": set(p.get("labels", []))} for p in prs}
        self.head_sha = head_sha
        self.rules = rules if rules is not None else []

    def transport(self, method, path, body):
        if method == "GET" and "/pulls?" in path:      # list open PRs (base= filter ignored; all target main)
            return 200, [self._pr_json(p) for p in self.prs.values()]
        m = re.search(r"/pulls/(\d+)/reviews$", path)
        if method == "GET" and m:
            return 200, self.prs[int(m.group(1))].get("reviews", [])
        if method == "GET" and "/git/ref/heads/" in path:
            return 200, {"object": {"sha": self.head_sha}}
        m = re.search(r"/commits/([^/]+)/check-runs$", path)
        if method == "GET" and m:
            sha = m.group(1)
            for p in self.prs.values():
                if p["head_sha"] == sha:
                    return 200, {"check_runs": [{"name": k, "conclusion": v}
                                                for k, v in p.get("checks", {}).items()]}
            return 200, {"check_runs": []}
        if method == "GET" and "/rules/branches/" in path:
            return 200, self.rules
        if method == "GET" and "/labels/" in path:
            return 200, {}
        m = re.search(r"/issues/(\d+)/labels$", path)
        if method == "POST" and m:
            self.prs[int(m.group(1))]["labels"].update(body["labels"])
            return 200, []
        m = re.search(r"/issues/(\d+)/labels/(.+)$", path)
        if method == "DELETE" and m:
            self.prs[int(m.group(1))]["labels"].discard(urllib.parse.unquote(m.group(2)))
            return 204, None
        return 404, None

    def _pr_json(self, p):
        return {"number": p["number"], "title": p.get("title", ""), "draft": p.get("draft", False),
                "head": {"sha": p["head_sha"]}, "base": {"sha": p.get("base_sha", "MAIN")},
                "labels": [{"name": n} for n in sorted(p["labels"])]}


R = iq.READY_LABEL
P = iq.PRIORITY_LABEL


class TestReviewedCandidates(unittest.TestCase):
    def test_solo_ready_is_label_plus_not_draft(self):
        gh = _FakeGH([
            {"number": 5, "head_sha": "a", "labels": [R]},
            {"number": 6, "head_sha": "b", "labels": [R], "draft": True},   # draft -> excluded
            {"number": 7, "head_sha": "c"},                                 # no ready label -> excluded
        ])
        cands = iq.reviewed_candidates(gh.transport, "you/proj", "main", tier="solo")
        self.assertEqual([c.pr for c in cands], [5])

    def test_team_requires_an_approval_surviving_last_push(self):
        gh = _FakeGH([
            {"number": 5, "head_sha": "a", "labels": [R],
             "reviews": [{"state": "APPROVED", "commit_id": "a"}]},          # approval on current head
            {"number": 6, "head_sha": "b", "labels": [R],
             "reviews": [{"state": "APPROVED", "commit_id": "OLD"}]},        # stale approval -> excluded
        ])
        cands = iq.reviewed_candidates(gh.transport, "you/proj", "main", tier="team")
        self.assertEqual([c.pr for c in cands], [5])

    def test_priority_label_orders_ahead_of_fifo(self):
        gh = _FakeGH([
            {"number": 5, "head_sha": "a", "labels": [R]},
            {"number": 9, "head_sha": "b", "labels": [R, P]},               # promoted
        ])
        cands = iq.reviewed_candidates(gh.transport, "you/proj", "main", tier="solo")
        self.assertEqual([c.pr for c in cands], [9, 5])


class TestProveReady(unittest.TestCase):
    def _candidate(self, base_sha="MAIN", head="h"):
        return iq.Candidate(5, head, base_sha, True, (1, 5), "t")

    def test_ready_when_floor_armed_uptodate_and_checks_green(self):
        gh = _FakeGH([{"number": 5, "head_sha": "h", "base_sha": "MAIN",
                       "checks": {"engine-ci": "success", "engine-guard": "success"}}], head_sha="MAIN")
        with mock.patch.object(protection_guard, "missing_floor", return_value=[]):
            proof = iq.prove_ready(gh.transport, "you/proj", self._candidate(), "main", tier="solo")
        self.assertTrue(proof["ready"], proof)

    def test_not_ready_when_behind_or_red(self):
        gh = _FakeGH([{"number": 5, "head_sha": "h", "base_sha": "OLD",     # behind current head MAIN
                       "checks": {"engine-ci": "failure", "engine-guard": "success"}}], head_sha="MAIN")
        with mock.patch.object(protection_guard, "missing_floor", return_value=[]):
            proof = iq.prove_ready(gh.transport, "you/proj", self._candidate(base_sha="OLD"), "main", tier="solo")
        self.assertFalse(proof["ready"])
        self.assertTrue(any("behind" in r for r in proof["reasons"]))
        self.assertTrue(any("checks" in r for r in proof["reasons"]))


class TestSurfaceNext(unittest.TestCase):
    def _backend(self, gh):
        return be.SerializedFallbackBackend("you/proj", "tok", transport=gh.transport)

    def test_admits_next_and_surfaces_ready(self):
        gh = _FakeGH([{"number": 5, "head_sha": "h", "base_sha": "MAIN", "labels": [R],
                       "checks": {"engine-ci": "success", "engine-guard": "success"}}], head_sha="MAIN")
        with mock.patch.object(protection_guard, "missing_floor", return_value=[]):
            r = iq.surface_next(gh.transport, "you/proj", "main", tier="solo", be=self._backend(gh),
                                this_pr=5, prepare_fn=lambda **kw: {"status": "healthy"})
        self.assertEqual(r["status"], "ready")
        self.assertEqual(r["admitted"], 5)

    def test_busy_when_another_pr_holds_admission(self):
        gh = _FakeGH([
            {"number": 5, "head_sha": "h", "base_sha": "MAIN", "labels": [R]},
            {"number": 3, "head_sha": "g", "base_sha": "MAIN", "labels": [R, be.INTEGRATING_LABEL]},
        ], head_sha="MAIN")
        r = iq.surface_next(gh.transport, "you/proj", "main", tier="solo", be=self._backend(gh), this_pr=5)
        self.assertEqual(r["status"], "busy")
        self.assertEqual(r["admitted"], 3)

    def test_authored_conflict_releases_admission_and_blocks(self):
        gh = _FakeGH([{"number": 5, "head_sha": "h", "base_sha": "MAIN", "labels": [R]}], head_sha="MAIN")
        backend_obj = self._backend(gh)
        r = iq.surface_next(gh.transport, "you/proj", "main", tier="solo", be=backend_obj, this_pr=5,
                            prepare_fn=lambda **kw: {"status": "needs-manual", "reason": "authored-conflict"})
        self.assertEqual(r["status"], "blocked")
        self.assertEqual(r["reason"], "authored-conflict")
        # the operator-facing detail is PLAIN language, not the raw reason code
        self.assertIn("conflicts with the latest main", r["detail"])
        self.assertNotEqual(r["detail"], "authored-conflict")
        self.assertIsNone(backend_obj.admitted())     # admission released, not left wedged


class TestNeverMerges(unittest.TestCase):
    def test_the_coordinator_source_carries_no_merge_path(self):
        # F-risk-1: the never-merge guarantee rests on this module carrying NO merge call (plus the ruleset),
        # not on the session merge hook. Prose in the docstring legitimately NAMES merge to say it never does
        # it, so we assert the real signals instead: every network call this module makes is a read (GET) —
        # label writes are delegated to the backend, never a merge API — and no merge tool is invoked. A
        # regression that added `transport("PUT", ".../merge", ...)` or an MCP merge would fail here.
        with open(os.path.join(os.path.dirname(__file__), "integration_queue.py"), encoding="utf-8") as fh:
            src = fh.read()
        methods = re.findall(r"transport\(\s*[\"'](\w+)", src)
        self.assertTrue(methods, "expected the coordinator to make transport calls")
        self.assertEqual(sorted(set(methods)), ["GET"], f"coordinator makes non-GET calls: {set(methods)}")
        self.assertNotIn("merge_pull_request", src)     # no MCP merge tool
        self.assertNotIn("/merge", src)                 # no REST merge endpoint


if __name__ == "__main__":
    unittest.main()
