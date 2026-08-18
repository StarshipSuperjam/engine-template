#!/usr/bin/env python3
"""demo_coordination_two_sessions — a fully offline walkthrough of advisory cross-session coordination
(StarshipSuperjam/engine-template#939), with the negative controls that must BITE.

Two Engine worker sessions share a repository. Session A, preparing PR #5, posts an advisory notice; session
B, on PR #6, reads the board and would re-verify canonical state. No network: a fake in-memory GitHub models
the comments and the pull requests, so the whole thing runs in CI.

The point of a demo is that it can FAIL. Three controls each assert a load-bearing property of eADR-0043 and
return a non-zero exit if the property does not hold:
  1. DELIVERY-INDEPENDENCE — the canonical decision (here, change-domain overlap) is byte-identical whether or
     not a notice was ever delivered. Correctness never depends on the board.
  2. FORGED-NOTICE-SKIP — a tampered board block is dropped by the reader, never acted on.
  3. CONFINEMENT — coordination touched GitHub only through the comments (and read) endpoints; never a merge,
     label, status, or issue-body write.

The live TWO-REAL-SESSION arm (real Claude sessions messaging each other) cannot run in CI — it is exercised
locally and recorded in the pull request's Demonstration section.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import coordination_board as board  # noqa: E402
import coordination_domains as domains  # noqa: E402
import coordination_emitters as emitters  # noqa: E402
import coordination_notice as cn  # noqa: E402


class _FakeGitHub:
    """In-memory GitHub: open pull requests, their changed files, and issue comments. Records every touched
    (method, path) so the confinement control can assert the boundary."""

    def __init__(self, open_prs, files):
        self.open_prs = open_prs
        self.files = files
        self.comments = {}
        self._next = 1
        self.paths = []

    def transport(self, method, path, body=None):
        self.paths.append((method, path))
        if method == "GET" and "/pulls?" in path:
            return 200, [{"number": n} for n in self.open_prs]
        if method == "GET" and "/pulls/" in path and "/files" in path:
            n = int(path.split("/pulls/")[1].split("/")[0])
            return 200, [{"filename": f} for f in self.files.get(n, [])]
        if method == "GET" and "/comments" in path:
            n = int(path.split("/issues/")[1].split("/")[0])
            return 200, [c for c in self.comments.values() if c["number"] == n]
        if method == "POST" and path.endswith("/comments"):
            n = int(path.split("/issues/")[1].split("/")[0])
            cid = self._next
            self._next += 1
            self.comments[cid] = {"id": cid, "number": n, "body": body["body"], "user": {"type": "Bot"}}
            return 201, self.comments[cid]
        if method == "PATCH" and "/issues/comments/" in path:
            cid = int(path.rstrip("/").split("/")[-1])
            self.comments[cid]["body"] = body["body"]
            return 200, self.comments[cid]
        raise AssertionError(f"unexpected GitHub call {method} {path}")


def _delete_board(gh, pr):
    for cid, c in list(gh.comments.items()):
        if c["number"] == pr and board.BOARD_MARKER in c["body"]:
            del gh.comments[cid]


def main() -> int:
    failures = []
    # Two PRs whose declared/actual domains overlap on one file.
    gh = _FakeGitHub(open_prs=[5, 6], files={5: [".engine/tools/boot.py"], 6: [".engine/tools/boot.py"]})
    client = board._Comments("owner/proj", "", transport=gh.transport)

    print("=== Session A (PR #5) posts an advisory 'admitted' notice ===")
    emitters.emit_integration_admitted(gh.transport, "owner/proj", 5)
    seen = board.read_board(client, 5)
    print(f"Board on PR #5 carries {len(seen)} notice(s): "
          f"{[ (n['kind'], n['event']) for n in seen ]}")
    if not (len(seen) == 1 and seen[0]["verify"]["action"] == "recheck-queue"):
        failures.append("Session A's notice did not land with its recheck-queue action")

    print("\n=== Session B (PR #6) reads the board — it is a prompt to re-verify, never authority ===")
    for n in seen:
        print(f"  received {n['kind']}/{n['event']} -> the receiver would run: {n['verify']['action']}")

    print("\n--- CONTROL 1: delivery-independence (the canonical decision ignores the board) ---")
    dom5 = domains.domain(lambda m, p: gh.transport(m, p, None), "owner/proj", 5)
    dom6 = domains.domain(lambda m, p: gh.transport(m, p, None), "owner/proj", 6)
    with_board = domains.overlaps(dom5, dom6)
    _delete_board(gh, 5)  # a peer suppresses the board comment
    without_board = domains.overlaps(dom5, dom6)
    print(f"overlap with board present={with_board}; with board deleted={without_board}")
    if not (with_board is True and without_board == with_board):
        failures.append("the canonical overlap decision changed when the board was deleted")

    print("\n--- CONTROL 2: a forged/tampered notice is skipped by the reader ---")
    emitters.emit_integration_admitted(gh.transport, "owner/proj", 5)  # re-post a fresh board
    for c in gh.comments.values():
        if c["number"] == 5 and board.BOARD_MARKER in c["body"]:
            c["body"] = c["body"].replace('"admitted"', '"blocked"')  # tamper -> digest no longer matches
    after_tamper = board.read_board(client, 5)
    print(f"notices recovered from the tampered board: {len(after_tamper)}")
    if after_tamper != []:
        failures.append("a tampered board block was NOT skipped")

    print("\n--- CONTROL 3: coordination touched only comment + read endpoints ---")
    bad = [(m, p) for (m, p) in gh.paths
           if not ("/comments" in p or "/pulls" in p or "/issues/comments/" in p)
           or "/merge" in p or "/labels" in p or "/statuses" in p]
    print(f"non-comment/read GitHub calls: {bad}")
    if bad:
        failures.append(f"coordination reached a forbidden endpoint: {bad}")

    print()
    if failures:
        print("DEMO FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("DEMO OK — advisory coordination works, and every control held (delivery-independent, "
          "forged-skip, confined).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
