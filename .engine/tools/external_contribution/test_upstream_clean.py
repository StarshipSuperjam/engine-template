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
    # The first sentence of each message is what telemetry publishes VERBATIM as the leak Issue title
    # (submit.py._leak_record). #777 pins both: the home title is reframed, the stranger title is preserved.
    STRANGER_TITLE = ("This contribution branch includes files that belong to the Engine, not to the product "
                      "you're contributing to — and the Engine's files should never ride along into someone "
                      "else's repository.")
    HOME_TITLE = ("This contribution to the Engine's own home includes files that belong to just this copy "
                  "of the Engine — your own saved state, settings, or private tuning — not to the shared "
                  "template.")

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

    def test_stranger_published_title_is_unchanged(self):
        # #777: the objective is truthful on BOTH targets, so the default (stranger) branch must be preserved
        # byte-for-byte — a split that silently regressed the stranger title would fail no other test.
        fs = upstream_clean_check.findings(
            "soft", changed=["src/app.py", ".engine/check/upstream-clean.json"], owned=OWNED)
        self.assertEqual(len(fs), 1)
        self.assertTrue(fs[0]["message"].startswith(self.STRANGER_TITLE), fs[0]["message"])

    def test_engine_home_reframes_the_published_title_and_stays_title_safe(self):
        # #777: contributing to the Engine's OWN home, the flagged files are this copy's own state — the
        # published title must name that, never the backwards "someone else's repository" framing. And the
        # offending path must stay OUT of the first sentence (the verbatim Issue title), only ever in the body.
        fs = upstream_clean_check.findings(
            "soft", changed=["src/app.py", ".engine/state/state.json"], owned=[".engine/state/state.json"],
            contributing_to_engine_home=True)
        self.assertEqual(len(fs), 1)
        msg = fs[0]["message"]
        self.assertTrue(msg.startswith(self.HOME_TITLE), msg)          # published title = the home framing
        self.assertNotIn("someone else's repository", msg)             # never the backwards stranger wording
        self.assertIn(".engine/state/state.json", msg)                 # offending path still named (in the body)
        self.assertNotIn(".engine/state/state.json", msg.split(". ", 1)[0])  # ...but NOT in the title sentence

    def test_home_framing_selects_wording_not_detection(self):
        # The home flag is framing-only: the SAME diff+owned fires the same single finding on the same path
        # under either framing — only the wording differs. Detection never depends on the flag.
        kw = dict(changed=["src/app.py", ".engine/check/upstream-clean.json"], owned=OWNED)
        stranger = upstream_clean_check.findings("soft", **kw)
        home = upstream_clean_check.findings("soft", contributing_to_engine_home=True, **kw)
        self.assertEqual(len(stranger), 1)   # both framings fire exactly one finding...
        self.assertEqual(len(home), 1)       # ...on the same path (detection is flag-independent)
        self.assertEqual(stranger[0]["location"], home[0]["location"])
        self.assertNotEqual(stranger[0]["message"], home[0]["message"])

    def test_demo_self_check_passes_on_real_logic(self):
        self.assertEqual(quiet_call.run(upstream_clean_check.demo), 0)


if __name__ == "__main__":
    unittest.main()
