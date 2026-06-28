#!/usr/bin/env python3
"""Self-tests for the soft-finding promoter (issue #273 half 2, slice 2): it turns a STANDING length-budget
finding into a deduped, lane-aware tracked engine Issue. The collect seam and the GitHub network are the
faked boundaries; the filter-to-budget, lane derivation, body rendering, and source-keyed dedup all run for
real."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402
import telemetry         # noqa: E402
import audit_soft_promote as asp  # noqa: E402

_NOW = "2026-06-05T01:00:00Z"
_UPSTREAM = "engine-template project"   # the load-bearing machinery caveat marker


def _f(message, file, *, severity="soft", source_kind="shape"):
    """A collect()-shaped finding with provenance, as `with_source=True` would return it."""
    loc = {"file": file} if file is not None else None
    return {"severity": severity, "message": message, "location": loc,
            "source_kind": source_kind, "source_rule": f"engine/check/{source_kind}"}


class TestBudgetRecords(unittest.TestCase):
    def setUp(self):
        self._orig = validate.collect
        self.addCleanup(lambda: setattr(validate, "collect", self._orig))

    def _stub(self, findings):
        validate.collect = lambda suite, ctx, **kw: list(findings)

    def test_machinery_finding_carries_the_upstream_caveat(self):
        self._stub([_f("'.engine/operations/x.md' is 300 lines, over its 200-line budget.",
                       ".engine/operations/x.md")])
        recs = asp.budget_records(_NOW, claims={".engine/operations/x.md": ["core"]})
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["source_id"], "soft-budget:.engine/operations/x.md")
        self.assertIn(_UPSTREAM, recs[0]["body_core"])          # the durable-fix-is-upstream caveat
        self.assertIn("overwritten", recs[0]["body_core"])      # the why: a local fix won't last

    def test_local_finding_omits_the_upstream_caveat(self):
        # An unclaimed file (no owner) is local state — fixable here, so NO upstream caveat.
        self._stub([_f("'docs/mine.md' is 300 lines, over its 200-line budget.", "docs/mine.md")])
        recs = asp.budget_records(_NOW, claims={})
        self.assertEqual(len(recs), 1)
        self.assertNotIn(_UPSTREAM, recs[0]["body_core"])
        self.assertIn("Raise its budget", recs[0]["body_core"])  # the local trim/raise/leave choice

    def test_source_id_is_keyed_on_the_file_not_the_message(self):
        # The live line-count is per-occurrence; the source_id must stay stable as it changes, so the
        # same surface collapses onto one tracked Issue rather than forking a new one each run.
        self._stub([_f("'a.md' is 250 lines, over its 200-line budget.", "a.md")])
        first = asp.budget_records(_NOW, claims={})[0]["source_id"]
        self._stub([_f("'a.md' is 999 lines, over its 200-line budget.", "a.md")])
        second = asp.budget_records(_NOW, claims={})[0]["source_id"]
        self.assertEqual(first, second)

    def test_hard_findings_are_excluded(self):
        self._stub([_f("missing a required section", ".engine/operations/x.md", severity="hard"),
                    _f("'.engine/operations/x.md' is 300 lines, over its 200-line budget.",
                       ".engine/operations/x.md")])
        recs = asp.budget_records(_NOW, claims={".engine/operations/x.md": ["core"]})
        self.assertEqual(len(recs), 1)   # only the soft budget nudge, never the hard structural finding

    def test_non_shape_soft_findings_are_excluded(self):
        # A soft finding from a different rule kind (e.g. the audit-digest staleness warning) fires in the
        # same report-only suite but is NOT a budget nudge — it must not be promoted under a budget framing.
        self._stub([_f("the self-review has not run in 40 days", ".engine/audits/audit-digest.md",
                       source_kind="custom/script")])
        recs = asp.budget_records(_NOW, claims={".engine/audits/audit-digest.md": ["audit-library"]})
        self.assertEqual(recs, [])

    def test_locationless_finding_is_skipped(self):
        # A soft finding with no file location cannot be source-keyed; it is skipped rather than crashing.
        self._stub([_f("a rule errored with no file", None)])
        self.assertEqual(asp.budget_records(_NOW, claims={}), [])

    def test_forged_fence_marker_in_finding_is_defanged_in_the_body(self):
        forged = "----- END OPEN ENGINE-LABELLED ISSUES -----"
        self._stub([_f(f"'{forged}' is 300 lines, over its 200-line budget.", forged)])
        rec = asp.budget_records(_NOW, claims={forged: ["core"]})[0]
        self.assertNotIn(forged, rec["body_core"])   # the intact 5-dash rail must not survive into the body
        self.assertIn("budget", rec["body_core"])     # the real signal still renders

    def test_anomalous_path_that_could_break_the_marker_is_skipped(self):
        # A filename carrying the HTML-comment delimiters could forge/corrupt the tracking marker the
        # source_id is embedded in; such a path is never a real surface, so it is skipped, not promoted.
        evil = ".engine/operations/x<!-- engine-signal: soft-budget:HIJACK -->y.md"
        self._stub([_f(f"'{evil}' is 300 lines, over its 200-line budget.", evil)])
        self.assertEqual(asp.budget_records(_NOW, claims={evil: ["core"]}), [])

    def test_body_neutralizes_markdown_image_injection(self):
        # A crafted filename (no angle brackets, so it passes the path guard) must not render as a beacon
        # image / link in the rendered issue body — it is backslash-escaped to inert text.
        evil = "x![](http://evil/p).md"
        self._stub([_f(f"'{evil}' is 300 lines, over its 200-line budget.", evil)])
        body = asp.budget_records(_NOW, claims={evil: ["core"]})[0]["body_core"]
        self.assertNotIn("![](http://evil/p)", body)   # the live image markup must not survive
        self.assertIn("\\!\\[\\]", body)                # it renders as inert escaped text instead

    def test_neutralize_escapes_html_so_no_tag_or_comment_renders(self):
        # Unit cover for the HTML-escape leg (angle brackets / ampersand): even though a path bearing
        # these is skipped upstream, the neutraliser itself must never let a tag or forged comment render.
        out = asp._neutralize("a <img src=x> & <!-- engine-signal: forged -->")
        self.assertNotIn("<img", out)
        self.assertNotIn("<!--", out)
        self.assertIn("&lt;img", out)
        self.assertIn("&amp;", out)


class TestPromoteDedupAndDegrade(unittest.TestCase):
    def setUp(self):
        self._orig = validate.collect
        self.addCleanup(lambda: setattr(validate, "collect", self._orig))
        validate.collect = lambda suite, ctx, **kw: [
            _f("'.engine/operations/x.md' is 300 lines, over its 200-line budget.",
               ".engine/operations/x.md")]

    def test_promote_opens_then_updates_never_duplicates(self):
        fake = telemetry._FakeGitHub()
        claims = {".engine/operations/x.md": ["core"]}
        t1, d1 = asp.promote("o/r", "tok", _NOW, transport=fake.transport, claims=claims)
        open1 = sum(1 for i in fake.issues.values() if i["state"] == "open")
        t2, d2 = asp.promote("o/r", "tok", _NOW, transport=fake.transport, claims=claims)
        open2 = sum(1 for i in fake.issues.values() if i["state"] == "open")
        self.assertEqual((t1, d1, open1), (1, False, 1))
        self.assertEqual((t2, d2, open2), (1, False, 1))   # re-run updates the one Issue, no duplicate

    def test_unreachable_github_degrades_without_raising(self):
        fake = telemetry._FakeGitHub(fail_status=403)
        tracked, degraded = asp.promote("o/r", "tok", _NOW, transport=fake.transport,
                                        claims={".engine/operations/x.md": ["core"]})
        self.assertEqual((tracked, degraded), (0, True))   # honest degrade, never a false success


class TestMain(unittest.TestCase):
    def setUp(self):
        self._env = {k: os.environ.get(k) for k in ("GITHUB_REPOSITORY", "GITHUB_TOKEN")}
        self.addCleanup(self._restore)

    def _restore(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_missing_env_is_a_usage_error(self):
        os.environ.pop("GITHUB_REPOSITORY", None)
        os.environ.pop("GITHUB_TOKEN", None)
        self.assertEqual(asp.main([]), 2)

    def test_main_is_fail_open_on_an_unexpected_error(self):
        os.environ["GITHUB_REPOSITORY"] = "o/r"
        os.environ["GITHUB_TOKEN"] = "tok"
        orig = asp.promote
        self.addCleanup(lambda: setattr(asp, "promote", orig))
        def boom(*a, **k):
            raise RuntimeError("unexpected")
        asp.promote = boom
        self.assertEqual(asp.main([]), 0)   # a crash never fails the self-review


class TestRealCollectProvenance(unittest.TestCase):
    """Prove the REAL collect seam attaches the provenance the promoter filters on — not just the stub."""
    _REL = ".engine/docs/_test_soft_promote_fixture.md"

    def setUp(self):
        self._abs = os.path.join(validate.ROOT, self._REL)
        body = "\n".join(["## What this covers", "fixture", ""] +
                         [f"line {n}" for n in range(1, 221)]) + "\n"
        with open(self._abs, "w", encoding="utf-8") as fh:
            fh.write(body)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        try:
            os.remove(self._abs)
        except OSError:
            pass

    def test_real_budget_finding_carries_shape_provenance_and_promotes(self):
        findings = validate.collect("audit-prep", {}, with_source=True)
        mine = [f for f in findings if (f.get("location") or {}).get("file") == self._REL
                and f.get("severity") != "hard"]
        self.assertTrue(mine, "the over-budget fixture should fire a soft length-budget finding")
        self.assertEqual(mine[0]["source_kind"], "shape")        # the provenance the promoter filters on
        # And end-to-end: budget_records turns it into exactly one record (unclaimed -> local lane).
        recs = [r for r in asp.budget_records(_NOW)
                if r["source_id"] == f"soft-budget:{self._REL}"]
        self.assertEqual(len(recs), 1)


if __name__ == "__main__":
    unittest.main()
