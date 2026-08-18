#!/usr/bin/env python3
"""Tests for render_safety — the one identifier render-safety boundary (StarshipSuperjam/engine-template#939)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import render_safety as rs  # noqa: E402


class TestSafeIdent(unittest.TestCase):
    def test_real_path_is_lossless(self):
        self.assertEqual(rs.safe_ident(".engine/tools/coordination_notice.py"),
                         ".engine/tools/coordination_notice.py")

    def test_real_branch_is_lossless(self):
        self.assertEqual(rs.safe_ident("claude/939-session-coordination"),
                         "claude/939-session-coordination")

    def test_markup_is_neutralised(self):
        out = rs.safe_ident("```x`)[a](http://b)<img>")
        for bad in ("`", "(", ")", "[", "]", "<", ">", ":"):
            self.assertNotIn(bad, out)

    def test_default_replacement_is_question_mark(self):
        self.assertEqual(rs.safe_ident("a b"), "a?b")

    def test_whitelist_replacement_stays_in_whitelist(self):
        out = rs.safe_ident("a b`c", replacement="_")
        self.assertEqual(out, "a_b_c")
        # every character is in the conservative whitelist
        self.assertIsNone(rs._UNSAFE_IDENT_CHAR.search(out))

    def test_length_is_bounded(self):
        out = rs.safe_ident("x" * 5000)
        self.assertLessEqual(len(out), rs.MAX_IDENT_LEN + len("...TRUNCATED"))
        self.assertTrue(out.endswith("...TRUNCATED"))

    def test_truncation_marker_is_whitelist_safe(self):
        # a truncated value must still satisfy a whitelist-constrained field
        out = rs.safe_ident("a" * 5000, replacement="_")
        self.assertIsNone(rs._UNSAFE_IDENT_CHAR.search(out))

    def test_replacement_must_be_one_char(self):
        with self.assertRaises(ValueError):
            rs.safe_ident("x", replacement="__")

    def test_non_string_coerced(self):
        self.assertEqual(rs.safe_ident(123), "123")


if __name__ == "__main__":
    unittest.main()
