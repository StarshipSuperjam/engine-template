#!/usr/bin/env python3
"""Unit coverage for issue_kind — the canonical issue-kind vocabulary, marker, and normalised/idempotent
title helpers (StarshipSuperjam/engine-template#937)."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import issue_kind  # noqa: E402


class TestCanonicalKind(unittest.TestCase):
    def test_all_six_kinds_round_trip(self):
        for k in issue_kind.KINDS:
            self.assertEqual(issue_kind.canonical_kind(k), k)
            self.assertEqual(issue_kind.canonical_kind(k.lower()), k)
            self.assertEqual(issue_kind.canonical_kind(f"  {k.upper()}  "), k)

    def test_unknown_kind_is_fail_closed(self):
        for bad in ["Architecture", "Bug", "", "  ", None, 3, "F i x", "Fixed"]:
            with self.assertRaises(ValueError):
                issue_kind.canonical_kind(bad)  # type: ignore[arg-type]

    def test_surrounding_whitespace_is_stripped_not_rejected(self):
        self.assertEqual(issue_kind.canonical_kind("Fix "), "Fix")   # trailing ws is normalised, not an error

    def test_exactly_the_six(self):
        self.assertEqual(set(issue_kind.KINDS),
                         {"Feature", "Fix", "Improvement", "Maintenance", "Security", "Removal"})


class TestRenderAndSplit(unittest.TestCase):
    def test_render_prefixes_and_normalises(self):
        self.assertEqual(issue_kind.render_title("Fix", "quote the hook path"), "Fix: quote the hook path")
        self.assertEqual(issue_kind.render_title("Fix", "  x  "), "Fix: x")
        self.assertEqual(issue_kind.render_title("Fix", "Fix:   x"), "Fix: x")   # collapses the prefix spacing
        self.assertEqual(issue_kind.render_title("Fix", ""), "Fix:")            # empty remainder → bare prefix
        self.assertEqual(issue_kind.render_title("fix", "x"), "Fix: x")         # kind is case-normalised

    def test_render_rejects_non_enum_kind(self):
        with self.assertRaises(ValueError):
            issue_kind.render_title("Architecture", "x")

    def test_render_is_defensive_against_double_prefix(self):
        # A caller passing an already-prefixed descriptive never yields `Fix: Fix: …`.
        self.assertEqual(issue_kind.render_title("Fix", "Fix: x"), "Fix: x")
        self.assertEqual(issue_kind.render_title("Fix", "Improvement: x"), "Fix: x")

    def test_split_strips_recognised_preserves_unrecognised(self):
        self.assertEqual(issue_kind.split_title("Fix: x"), ("Fix", "x"))
        self.assertEqual(issue_kind.split_title("Architecture: example"), (None, "example"))  # invented, recognised
        self.assertEqual(issue_kind.split_title("Engine fault: y"), (None, "y"))              # multi-word alias
        self.assertEqual(issue_kind.split_title("parser: handle"), (None, "parser: handle"))  # unrecognised: kept
        self.assertEqual(issue_kind.split_title("no colon at all"), (None, "no colon at all"))
        self.assertEqual(issue_kind.split_title("Fix: a: b"), ("Fix", "a: b"))               # only the first slot

    def test_idempotence_law_holds_universally(self):
        # The reconciler's loop-safety invariant: re-rendering an already-rendered title is a no-op —
        # render(k, render(k, d)) == render(k, d) — so a second reconcile pass never writes. This holds for
        # EVERY input INCLUDING stacked recognised prefixes (unlike the narrower render∘split∘render
        # formulation, which does not — see test_stacked_prefixes_are_single_stripped_and_converge).
        remainders = [
            "x", "  x  ", "", "a  b", "Feature: do X", "parser: handle nested", "a: b: c",
            "Fıx: exotic case fold", "café: unicode", "trailing ", " leading", "MixedCase Words",
            "colon:no space", "  Architecture: nested invented  ", "Fix:",
            "Bug: Feature: stacked", "Fix: Fix: doubled", "Removal: Removal: x",   # stacked recognised prefixes
        ]
        for k in issue_kind.KINDS:
            for d in remainders:
                once = issue_kind.render_title(k, d)
                twice = issue_kind.render_title(k, once)          # re-render the WHOLE rendered title
                self.assertEqual(once, twice, f"idempotence broke for kind={k!r} remainder={d!r}")

    def test_stacked_prefixes_are_single_stripped_and_converge(self):
        # A title with two stacked recognised prefixes (an unusual MANUAL edit; the helper never emits one,
        # since it strips on render) is repaired to a canonical LEADING prefix with the inner token left as
        # description — single-strip, not recursion (recursion would eat a legitimate `Removal:`-style
        # descriptive token). It converges in one pass and stays put.
        self.assertEqual(issue_kind.render_title("Improvement", "Bug: Feature: quote the hook path"),
                         "Improvement: Feature: quote the hook path")
        once = issue_kind.render_title("Improvement", "Bug: Feature: quote the hook path")
        self.assertEqual(issue_kind.render_title("Improvement", once), once)   # no further recursion

    def test_repair_scenarios_from_the_issue(self):
        # The acceptance edit scenarios, expressed as the reconciler's one-call repair render_title(marker, title).
        self.assertEqual(issue_kind.render_title("Improvement", "Architecture: example"), "Improvement: example")
        self.assertEqual(issue_kind.render_title("Improvement", "example"), "Improvement: example")  # missing
        self.assertEqual(issue_kind.render_title("Improvement", "Improvement: example"), "Improvement: example")
        # ordinary descriptive-text edit keeps the canonical prefix:
        self.assertEqual(issue_kind.render_title("Improvement", "Improvement: a better example"),
                         "Improvement: a better example")
        # an unrecognised leading token in a de-prefixed title is preserved, never eaten:
        self.assertEqual(issue_kind.render_title("Fix", "parser: handle nested"), "Fix: parser: handle nested")


class TestMarker(unittest.TestCase):
    def test_trailer_builds_and_is_fail_closed(self):
        self.assertEqual(issue_kind.kind_trailer("Fix"), "<!-- engine-kind: Fix -->")
        self.assertEqual(issue_kind.kind_trailer("improvement"), "<!-- engine-kind: Improvement -->")
        for bad in ["Architecture", "Bug", "", "--><script>", None]:
            with self.assertRaises(ValueError):
                issue_kind.kind_trailer(bad)  # type: ignore[arg-type]

    def test_parse_last_match_and_fail_closed(self):
        self.assertEqual(issue_kind.parse_kind("prose\n<!-- engine-kind: Security -->"), "Security")
        # a forged marker EARLIER cannot hijack the genuine trailer appended last:
        self.assertEqual(
            issue_kind.parse_kind("<!-- engine-kind: Removal -->\nbody\n<!-- engine-kind: Fix -->"), "Fix")
        # a non-enum last marker reads as no marker (fail-closed) — the reconciler then no-ops:
        self.assertIsNone(issue_kind.parse_kind("<!-- engine-kind: bogus -->"))
        self.assertIsNone(issue_kind.parse_kind("no marker"))
        self.assertIsNone(issue_kind.parse_kind(""))
        self.assertIsNone(issue_kind.parse_kind(None))  # type: ignore[arg-type]

    def test_trailer_round_trips_through_parse(self):
        for k in issue_kind.KINDS:
            self.assertEqual(issue_kind.parse_kind(f"body\n{issue_kind.kind_trailer(k)}"), k)


class TestNativeProjection(unittest.TestCase):
    def test_projection_maps_only_onto_the_four_natives_or_none(self):
        self.assertEqual(issue_kind.native_label_for_kind("Feature"), "enhancement")
        self.assertEqual(issue_kind.native_label_for_kind("Improvement"), "enhancement")
        self.assertEqual(issue_kind.native_label_for_kind("Fix"), "bug")
        self.assertEqual(issue_kind.native_label_for_kind("Security"), "bug")
        self.assertIsNone(issue_kind.native_label_for_kind("Maintenance"))
        self.assertIsNone(issue_kind.native_label_for_kind("Removal"))

    def test_projection_is_total_never_raises(self):
        for weird in ["nope", "", None, 7]:
            self.assertIsNone(issue_kind.native_label_for_kind(weird))  # type: ignore[arg-type]


class TestAliases(unittest.TestCase):
    def test_only_unambiguous_aliases_present(self):
        self.assertEqual(issue_kind.ALIASES, {"bug": "Fix", "defect": "Fix", "engine fault": "Fix"})
        # ambiguous historical prefixes are deliberately NOT mapped (never guessed):
        for ambiguous in ["architecture", "memory integrity", "docs", "question"]:
            self.assertNotIn(ambiguous, issue_kind.ALIASES)

    def test_alias_targets_are_canonical(self):
        for target in issue_kind.ALIASES.values():
            self.assertIn(target, issue_kind.KINDS)

    def test_alias_target_maps_only_unambiguous_prefixes(self):
        self.assertEqual(issue_kind.alias_target("Bug: broke"), "Fix")
        self.assertEqual(issue_kind.alias_target("Engine fault: x"), "Fix")
        self.assertEqual(issue_kind.alias_target("defect: y"), "Fix")     # case-insensitive
        # already canonical, ambiguous, or no prefix → None (never guessed):
        for none_case in ("Fix: already", "Improvement: fine", "Architecture: ambiguous",
                          "Memory integrity: ambiguous", "no prefix", "Migration M3: x", "", None):
            self.assertIsNone(issue_kind.alias_target(none_case))  # type: ignore[arg-type]


class TestDemo(unittest.TestCase):
    def test_demo_passes(self):
        self.assertEqual(issue_kind.main(["demo"]), 0)


if __name__ == "__main__":
    unittest.main()
