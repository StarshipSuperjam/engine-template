"""Tests for release_impact — the canonical release-impact vocabulary, marker quartet, ordering, and fold."""
from __future__ import annotations

import unittest

import release_impact


class TestVocabularyAndOrdering(unittest.TestCase):
    def test_four_classes_in_ladder_order(self):
        self.assertEqual(release_impact.RELEASE_IMPACTS, ("none", "patch", "minor", "major"))

    def test_rank_is_a_strict_ladder(self):
        self.assertLess(release_impact.rank("none"), release_impact.rank("patch"))
        self.assertLess(release_impact.rank("patch"), release_impact.rank("minor"))
        self.assertLess(release_impact.rank("minor"), release_impact.rank("major"))

    def test_rank_fail_closed(self):
        with self.assertRaises(ValueError):
            release_impact.rank("huge")

    def test_canonical_impact_normalises_and_fails_closed(self):
        self.assertEqual(release_impact.canonical_impact("Minor"), "minor")
        self.assertEqual(release_impact.canonical_impact("  MAJOR "), "major")
        with self.assertRaises(ValueError):
            release_impact.canonical_impact("huge")
        with self.assertRaises(ValueError):
            release_impact.canonical_impact(None)


class TestFold(unittest.TestCase):
    def test_max_impact_folds_to_highest(self):
        self.assertEqual(release_impact.max_impact(["patch", "minor", "none"]), "minor")

    def test_patch_patch_none_folds_to_patch(self):
        self.assertEqual(release_impact.max_impact(["patch", "patch", "none"]), "patch")

    def test_any_major_wins(self):
        self.assertEqual(release_impact.max_impact(["none", "patch", "major", "minor"]), "major")

    def test_empty_is_none(self):
        self.assertEqual(release_impact.max_impact([]), "none")

    def test_all_none_stays_none(self):
        self.assertEqual(release_impact.max_impact(["none", "none"]), "none")

    def test_fold_fail_closed_on_bad_member(self):
        with self.assertRaises(ValueError):
            release_impact.max_impact(["patch", "huge"])


class TestMarkerQuartet(unittest.TestCase):
    def test_trailer_builds_the_marker(self):
        self.assertEqual(release_impact.impact_trailer("minor"), "<!-- engine-release-impact: minor -->")

    def test_trailer_fail_closed(self):
        with self.assertRaises(ValueError):
            release_impact.impact_trailer("huge")

    def test_parse_recovers_the_marker(self):
        self.assertEqual(release_impact.parse_impact("body\n<!-- engine-release-impact: major -->"), "major")

    def test_parse_last_match_beats_forged_earlier_prose(self):
        body = "<!-- engine-release-impact: patch -->\nprose\n<!-- engine-release-impact: major -->"
        self.assertEqual(release_impact.parse_impact(body), "major")

    def test_parse_fail_closed_on_non_enum_last_marker(self):
        self.assertIsNone(release_impact.parse_impact("<!-- engine-release-impact: huge -->"))

    def test_parse_none_when_absent(self):
        self.assertIsNone(release_impact.parse_impact("no marker here"))
        self.assertIsNone(release_impact.parse_impact(""))
        self.assertIsNone(release_impact.parse_impact(None))

    def test_trailer_roundtrips_every_class(self):
        for impact in release_impact.RELEASE_IMPACTS:
            self.assertEqual(release_impact.parse_impact(release_impact.impact_trailer(impact)), impact)


class TestExemptAuthors(unittest.TestCase):
    def test_bots_exempt_humans_not(self):
        self.assertTrue(release_impact.is_author_exempt("dependabot[bot]"))
        self.assertTrue(release_impact.is_author_exempt("github-actions[bot]"))
        self.assertFalse(release_impact.is_author_exempt("shanekidd"))

    def test_total_on_none_and_odd(self):
        self.assertFalse(release_impact.is_author_exempt(None))
        self.assertFalse(release_impact.is_author_exempt(""))

    def test_default_exempt_impact_is_patch(self):
        self.assertEqual(release_impact.canonical_impact(release_impact.DEFAULT_EXEMPT_IMPACT), "patch")


class TestVisibleLine(unittest.TestCase):
    def test_impact_line_is_visible_and_named(self):
        line = release_impact.impact_line("major")
        self.assertTrue(line.startswith("Release-Impact: major"))
        self.assertIn("incompatible", line)

    def test_impact_line_fail_closed(self):
        with self.assertRaises(ValueError):
            release_impact.impact_line("huge")


class TestDemo(unittest.TestCase):
    def test_demo_passes(self):
        self.assertEqual(release_impact.main(["demo"]), 0)


if __name__ == "__main__":
    unittest.main()
