#!/usr/bin/env python3
"""Tests for coordination_postmerge — the deterministic merge-reaction fan-out (StarshipSuperjam/engine-template#939, eADR-0043).

The fan-out rides the MERGE event (a workflow), not a human verb: given the merged pull request and the new
protected head SHA it posts revalidation to every other open candidate, dependency-update to each open
candidate whose change domain overlaps the merged one, and next-in-queue to the next reviewed candidate. These
tests drive it against a fake in-memory GitHub — no network — and assert it is deterministic, confined to
comment/read endpoints, inert on a solo repo, and best-effort (a broken transport never raises)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import coordination_postmerge as pm  # noqa: E402


class _FakeGitHub:
    """Open pull requests (number -> {files, labels, draft}), a merged pull request, and issue comments.
    Records every (method, path) so the confinement test can assert the boundary."""

    def __init__(self, *, merged_pr, merged_files, merged_base="main", merge_sha="a" * 40,
                 candidates=None, ready_label="engine-integrate-ready"):
        # candidates: {number: {"files": [...], "ready": bool, "draft": bool}}
        self.merged_pr = merged_pr
        self.merged_files = merged_files
        self.merged_base = merged_base
        self.merge_sha = merge_sha
        self.candidates = candidates or {}
        self.ready_label = ready_label
        self.comments = {}
        self._next = 1
        self.paths = []

    def transport(self, method, path, body=None):
        self.paths.append((method, path))
        # --- comment endpoints (the board) ---
        if "/issues/" in path and "/comments" in path and "/issues/comments/" not in path:
            n = int(path.split("/issues/")[1].split("/")[0])
            if method == "GET":
                return 200, [c for c in self.comments.values() if c["number"] == n]
            if method == "POST":
                cid = self._next
                self._next += 1
                self.comments[cid] = {"id": cid, "number": n, "body": body["body"], "user": {"type": "Bot"}}
                return 201, self.comments[cid]
        if "/issues/comments/" in path and method == "PATCH":
            cid = int(path.rstrip("/").split("/")[-1])
            self.comments[cid]["body"] = body["body"]
            return 200, self.comments[cid]
        # --- pull request reads ---
        if "/files" in path:
            n = int(path.split("/pulls/")[1].split("/")[0])
            files = self.merged_files if n == self.merged_pr else self.candidates.get(n, {}).get("files", [])
            return 200, [{"filename": f} for f in files]
        if "/reviews" in path:
            return 200, []  # solo tier never asks; team would
        if "/pulls?" in path:  # the open-PR list (both the emitters' and the queue's forms)
            out = []
            for n, c in self.candidates.items():
                labels = [{"name": self.ready_label}] if c.get("ready") else []
                out.append({"number": n, "draft": c.get("draft", False), "labels": labels,
                            "head": {"sha": f"h{n}"}, "base": {"sha": "b0"}})
            return 200, out
        if "/pulls/" in path:  # a single pull request (the merged-PR context read)
            n = int(path.split("/pulls/")[1].split("/")[0].split("?")[0])
            if n == self.merged_pr:
                return 200, {"number": n, "merged": True, "merge_commit_sha": self.merge_sha,
                             "base": {"ref": self.merged_base}}
            return 200, {"number": n, "merged": False}
        raise AssertionError(f"unexpected GitHub call {method} {path}")


def _confined(paths):
    """Every recorded call is a read (GET) or a comment write (POST/PATCH to a comments endpoint) — never a
    merge, label, status, or issue-body write."""
    for m, p in paths:
        if m == "GET":
            continue
        if m in ("POST", "PATCH") and "comments" in p:
            continue
        return False
    return True


class TestFanOut(unittest.TestCase):
    def test_fires_revalidation_dependency_and_next_on_merge(self):
        gh = _FakeGitHub(
            merged_pr=1, merged_files=[".engine/tools/boot.py"],
            candidates={
                2: {"files": [".engine/tools/boot.py"], "ready": True, "draft": False},   # overlaps + reviewed
                3: {"files": [".engine/docs/x.md"], "ready": False, "draft": False},       # no overlap, unready
            })
        result = pm.fan_out(gh.transport, "o/r", 1, base="main", tier="solo", base_sha="a" * 40)
        self.assertEqual(result["revalidation"], 2)   # both other open candidates
        self.assertEqual(result["dependency"], 1)     # only the domain-overlapping one (#2)
        self.assertEqual(result["next"], 2)           # the one reviewed candidate
        self.assertTrue(_confined(gh.paths))

    def test_inert_on_solo_no_other_open_candidate(self):
        gh = _FakeGitHub(merged_pr=1, merged_files=[".engine/tools/boot.py"], candidates={})
        result = pm.fan_out(gh.transport, "o/r", 1, base="main", tier="solo", base_sha="a" * 40)
        self.assertEqual(result, {"revalidation": 0, "dependency": 0, "next": None})
        # nothing posted: no comment write in the recorded calls
        self.assertFalse(any(m in ("POST", "PATCH") for m, _ in gh.paths))

    def test_single_remaining_candidate_is_still_notified(self):
        # The require_peer=False path: with the merged PR closed, ONE remaining open candidate must still get
        # revalidation — the bug the >1-open-PR gate would have caused.
        gh = _FakeGitHub(merged_pr=1, merged_files=[".engine/tools/boot.py"],
                         candidates={2: {"files": [".engine/docs/x.md"], "ready": False, "draft": False}})
        result = pm.fan_out(gh.transport, "o/r", 1, base="main", tier="solo", base_sha="a" * 40)
        self.assertEqual(result["revalidation"], 1)

    def test_rerun_is_idempotent(self):
        gh = _FakeGitHub(merged_pr=1, merged_files=[".engine/tools/boot.py"],
                         candidates={2: {"files": [".engine/tools/boot.py"], "ready": True, "draft": False}})
        pm.fan_out(gh.transport, "o/r", 1, base="main", tier="solo", base_sha="a" * 40)
        boards_after_first = {c["number"]: c["body"] for c in gh.comments.values()}
        pm.fan_out(gh.transport, "o/r", 1, base="main", tier="solo", base_sha="a" * 40)
        boards_after_second = {c["number"]: c["body"] for c in gh.comments.values()}
        # same base SHA -> the board dedupes on the fingerprint; the comment bodies are unchanged
        self.assertEqual(boards_after_first, boards_after_second)

    def test_best_effort_never_raises(self):
        def boom(method, path, body=None):
            raise RuntimeError("GitHub down")
        result = pm.fan_out(boom, "o/r", 1, base="main", tier="solo", base_sha="a" * 40)
        self.assertEqual(result, {"revalidation": 0, "dependency": 0, "next": None})


class TestMergedContext(unittest.TestCase):
    def test_reads_base_and_merge_sha_for_a_merged_pr(self):
        gh = _FakeGitHub(merged_pr=5, merged_files=[], merged_base="trunk", merge_sha="a" * 40)
        ctx = pm._merged_pr_context(gh.transport, "o/r", 5)
        self.assertEqual(ctx, {"base": "trunk", "merge_sha": "a" * 40})

    def test_none_for_a_pr_closed_without_merging(self):
        gh = _FakeGitHub(merged_pr=5, merged_files=[])
        self.assertIsNone(pm._merged_pr_context(gh.transport, "o/r", 6))  # 6 -> merged:False


class TestSummaryAndMain(unittest.TestCase):
    def test_summary_reports_counts(self):
        s = pm._summary("o/r", 1, {"base": "main", "merge_sha": "x"},
                        {"revalidation": 2, "dependency": 1, "next": 2})
        self.assertIn("2 revalidation", s)
        self.assertIn("1 dependency-update", s)
        self.assertIn("PR #2", s)

    def test_summary_no_merge(self):
        s = pm._summary("o/r", 1, None, None)
        self.assertIn("not a merge", s)

    def test_main_no_op_without_env(self):
        # No repo/token/PR in the environment -> a clean no-op that exits 0 and reports it was not a merge.
        for key in ("GITHUB_REPOSITORY", "GITHUB_TOKEN", "PR_NUMBER", "MERGE_SHA"):
            os.environ.pop(key, None)
        self.assertEqual(pm.main([]), 0)


if __name__ == "__main__":
    unittest.main()
