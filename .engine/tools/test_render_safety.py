#!/usr/bin/env python3
"""Tests for render_safety — the one identifier render-safety boundary."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import render_safety as rs  # noqa: E402


class TestSafeIdent(unittest.TestCase):
    def test_real_path_is_lossless(self):
        self.assertEqual(rs.safe_ident(".engine/tools/overlay_disclosure.py"),
                         ".engine/tools/overlay_disclosure.py")

    def test_real_branch_is_lossless(self):
        self.assertEqual(rs.safe_ident("engine-update-1.4.0"),
                         "engine-update-1.4.0")

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
        # The marker is counted IN the budget: the RESULT never exceeds max_len (the invariant a caller that
        # also enforces max_len as a hard field bound relies on), not max_len + marker.
        self.assertLessEqual(len(out), rs.MAX_IDENT_LEN)
        self.assertTrue(out.endswith("...TRUNCATED"))

    def test_result_never_exceeds_max_len_even_below_marker_width(self):
        # A max_len smaller than the marker itself must still bound the result — the marker is clipped, not
        # appended past the budget (else a caller enforcing max_len as a hard field bound rejects a value
        # this function claims it made fit).
        for n in range(0, 15):
            out = rs.safe_ident("x" * 5000, max_len=n)
            self.assertLessEqual(len(out), n)

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
