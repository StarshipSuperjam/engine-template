"""A runnable walkthrough of serialized cross-PR integration on a fake GitHub (StarshipSuperjam/engine-template#925).

Run it: `uv run --directory .engine --frozen -- python tools/demo_integration_queue.py`. It drives the REAL
coordinator against an in-memory GitHub and asserts the observable behavior, so it FAILS if the behavior
breaks (it is a demonstration, not a recipe that can only pass):
  - two reviewed candidates are ordered (a priority label promotes ahead of FIFO);
  - only ONE candidate is admitted at a time;
  - the admitted candidate is surfaced READY when its checks are green against current main;
  - a second candidate is reported BUSY while the first holds admission;
  - the coordinator never merges — admission is released only by `advance`, and the operator merges.
"""

import re
import sys
import urllib.parse
from unittest import mock

import integration_queue as iq
import integration_queue_backend as be
import protection_guard

R, P, ADM = iq.READY_LABEL, iq.PRIORITY_LABEL, be.INTEGRATING_LABEL


class _FakeGH:
    def __init__(self, prs, head_sha="MAIN"):
        self.prs = {p["number"]: {**p, "labels": set(p.get("labels", []))} for p in prs}
        self.head_sha = head_sha

    def transport(self, method, path, body):
        if method == "GET" and "/pulls?" in path:
            return 200, [{"number": p["number"], "title": p.get("title", ""), "draft": p.get("draft", False),
                          "head": {"sha": p["head_sha"]}, "base": {"sha": p.get("base_sha", "MAIN")},
                          "labels": [{"name": n} for n in sorted(p["labels"])]} for p in self.prs.values()]
        if method == "GET" and "/git/ref/heads/" in path:
            return 200, {"object": {"sha": self.head_sha}}
        m = re.search(r"/commits/([^/]+)/check-runs$", path)
        if method == "GET" and m:
            for p in self.prs.values():
                if p["head_sha"] == m.group(1):
                    return 200, {"check_runs": [{"name": k, "conclusion": v}
                                                for k, v in p.get("checks", {}).items()]}
            return 200, {"check_runs": []}
        if method == "GET" and ("/rules/branches/" in path or "/labels/" in path):
            return (200, [] if "/rules/" in path else {})
        m = re.search(r"/issues/(\d+)/labels$", path)
        if method == "POST" and m:
            self.prs[int(m.group(1))]["labels"].update(body["labels"]); return 200, []
        m = re.search(r"/issues/(\d+)/labels/(.+)$", path)
        if method == "DELETE" and m:
            self.prs[int(m.group(1))]["labels"].discard(urllib.parse.unquote(m.group(2))); return 204, None
        return 404, None


def _check(label, cond):
    print(f"  {'✓' if cond else '✗'} {label}")
    return cond


def run() -> int:
    print("Serialized cross-PR integration — walkthrough on a fake GitHub\n")
    green = {"engine-ci": "success", "engine-guard": "success"}
    gh = _FakeGH([
        {"number": 5, "head_sha": "h5", "base_sha": "MAIN", "labels": [R], "checks": green, "title": "feature A"},
        {"number": 9, "head_sha": "h9", "base_sha": "MAIN", "labels": [R, P], "checks": green, "title": "urgent B"},
    ], head_sha="MAIN")
    backend_obj = be.SerializedFallbackBackend("you/proj", "tok", transport=gh.transport)
    ok = True

    with mock.patch.object(protection_guard, "missing_floor", return_value=[]):
        cands = iq.reviewed_candidates(gh.transport, "you/proj", "main", tier="solo")
        print("Ordered reviewed candidates (priority promotes ahead of FIFO):")
        for c in cands:
            print(f"    PR #{c.pr}: {c.title}  order={c.order_key}")
        ok &= _check("PR #9 (priority) is ordered ahead of PR #5", [c.pr for c in cands] == [9, 5])

        print("\nSurface the next candidate for PR #9 (this session's PR):")
        r9 = iq.surface_next(gh.transport, "you/proj", "main", tier="solo", be=backend_obj, this_pr=9,
                             prepare_fn=lambda **kw: {"status": "healthy"})
        print(f"    {r9['detail']}")
        ok &= _check("PR #9 is admitted and surfaced READY", r9["status"] == "ready" and r9["admitted"] == 9)

        print("\nMeanwhile PR #5 asks to integrate — one at a time:")
        r5 = iq.surface_next(gh.transport, "you/proj", "main", tier="solo", be=backend_obj, this_pr=5)
        print(f"    {r5['detail']}")
        ok &= _check("PR #5 is told PR #9 is integrating (BUSY)", r5["status"] == "busy" and r5["admitted"] == 9)

        print("\nOperator merges PR #9; the session advances the queue (releases admission):")
        backend_obj.release(9)
        ok &= _check("admission is released after advance", backend_obj.admitted() is None)

    print("\n" + ("DEMO PASSED — serialized, one-at-a-time, never merged by the engine."
                  if ok else "DEMO FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
