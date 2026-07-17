#!/usr/bin/env python3
"""Tests for the upstream-clean nudge (external-contribution module).

Every case injects `changed` and `owned` directly, so the predicate is exercised fully offline — no git,
no manifest discovery — and the assertions pin name↔behavior fidelity (a leaked engine path fires and is
named; a product-only diff is silent; the foundation-union leg is covered; findings are never hard).
"""
from __future__ import annotations
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from external_contribution import upstream_clean_check  # noqa: E402
import quiet_call  # noqa: E402  (capture a demo walkthrough's stdout so it can't bury the suite summary)

# A small engine-owned set covering both legs: a module-provided file and two foundation-infra files.
OWNED = [
    ".engine/check/upstream-clean.json",
    ".engine/tools/external_contribution/upstream_clean_check.py",
    "CLAUDE.md",
    ".github/CODEOWNERS",
]


class TestUpstreamClean(unittest.TestCase):
    def test_clean_product_only_diff_passes(self):
        fs = upstream_clean_check.findings("soft", changed=["src/app.py", "README.md"], owned=OWNED)
        self.assertEqual(fs, [])

    def test_empty_diff_passes(self):
        fs = upstream_clean_check.findings("soft", changed=[], owned=OWNED)
        self.assertEqual(fs, [])

    def test_leaked_engine_path_fires_one_soft_finding_naming_it(self):
        fs = upstream_clean_check.findings(
            "soft", changed=["src/app.py", ".engine/check/upstream-clean.json"], owned=OWNED)
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "soft")
        self.assertIn(".engine/check/upstream-clean.json", fs[0]["message"])
        # location is built literally from the relpath (no validate.loc() double-.engine/ mangling)
        self.assertEqual(fs[0]["location"]["file"], ".engine/check/upstream-clean.json")

    def test_leaked_foundation_file_fires(self):
        # the foundation-union leg: CLAUDE.md / .github/CODEOWNERS are engine-owned though no module
        # 'provides' claims them, so this proves engine_owned_paths' foundation union is honored.
        for path in ("CLAUDE.md", ".github/CODEOWNERS"):
            fs = upstream_clean_check.findings("soft", changed=[path], owned=OWNED)
            self.assertEqual(len(fs), 1, path)
            self.assertIn(path, fs[0]["message"])

    def test_multiple_leaked_paths_all_named_in_one_finding(self):
        leaked = [".engine/check/upstream-clean.json", "CLAUDE.md"]
        fs = upstream_clean_check.findings("soft", changed=leaked + ["src/app.py"], owned=OWNED)
        self.assertEqual(len(fs), 1)
        for p in leaked:
            self.assertIn(p, fs[0]["message"])
        self.assertNotIn("src/app.py", fs[0]["message"])

    def test_findings_are_never_hard(self):
        fs = upstream_clean_check.findings(
            "soft", changed=[".engine/check/upstream-clean.json"], owned=OWNED)
        self.assertTrue(fs and all(f["severity"] != "hard" for f in fs))

    def test_no_arg_default_reads_the_uncapped_diff(self):
        # #416: the no-argument path must read the diff UNCAPPED (cap=None), so an engine path that
        # sorts past work_record's 50-path orientation cap is still seen and still fires — a safety predicate
        # must never drop a leak. Patch the reader to prove the call is uncapped and that the hit fires.
        seen = {}

        def fake_changed_paths(*, cap):
            seen["cap"] = cap
            # an engine-owned path that would sort well past a 50-item prefix
            return [f"src/f{i:03d}.py" for i in range(80)] + [".engine/check/upstream-clean.json"]

        with mock.patch.object(upstream_clean_check.work_record, "changed_paths", fake_changed_paths):
            fs = upstream_clean_check.findings("soft", owned=[".engine/check/upstream-clean.json"])
        self.assertIsNone(seen["cap"], "the no-arg leak check must read changed_paths uncapped (cap=None)")
        self.assertEqual(len(fs), 1)
        self.assertIn(".engine/check/upstream-clean.json", fs[0]["message"])

    def test_demo_self_check_passes_on_real_logic(self):
        self.assertEqual(quiet_call.run(upstream_clean_check.demo), 0)


if __name__ == "__main__":
    unittest.main()
