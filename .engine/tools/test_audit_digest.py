#!/usr/bin/env python3
"""Tests for audit_digest.py (audit-library slice 2) — the self-seal and the freshness signal.

These pin the behaviours the two rules and the demo rely on: a sealed file verifies; a hand-edit to the
body breaks the seal; the seal is independent of how the header is serialized (it covers the parsed
run-date + the raw body, never the header text); an absent or malformed file is handled honestly, never a
crash; and the freshness boundary sits exactly at STALENESS_DAYS. All work on throwaway temp files.
"""
from __future__ import annotations
import datetime
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import audit_digest  # noqa: E402
import validate      # noqa: E402

BODY = "# Engine self-review\n\nI looked things over; here is what I found.\n"
JUNE = datetime.date(2026, 6, 1)


class TestSeal(unittest.TestCase):
    def _scratch(self, d):
        return os.path.join(d, "audit-digest.md")

    def test_seal_then_check_is_in_sync(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._scratch(d)
            audit_digest.seal(p, generated=JUNE, body=BODY)
            f = audit_digest.check(p)
            self.assertEqual(f["severity"], "note", f["message"])

    def test_stored_fingerprint_is_the_seal_over_date_and_body(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._scratch(d)
            audit_digest.seal(p, generated=JUNE, body=BODY)
            fm, body = audit_digest.split(p)
            self.assertEqual(fm["fingerprint"], audit_digest.compute_seal("2026-06-01", body))

    def test_hand_edit_to_the_body_breaks_the_seal(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._scratch(d)
            audit_digest.seal(p, generated=JUNE, body=BODY)
            with open(p, "a", encoding="utf-8", newline="") as fh:
                fh.write("a line the audit never wrote\n")
            f = audit_digest.check(p)
            self.assertEqual(f["severity"], "hard", "a hand-edit must be caught")

    def test_changing_the_run_date_breaks_the_seal(self):
        # The seal covers the date too: silently editing the run-date is caught.
        with tempfile.TemporaryDirectory() as d:
            p = self._scratch(d)
            audit_digest.seal(p, generated=JUNE, body=BODY)
            text = validate.read(p).replace("generated: 2026-06-01", "generated: 2026-05-01")
            with open(p, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
            self.assertEqual(audit_digest.check(p)["severity"], "hard")

    def test_seal_is_independent_of_header_serialization(self):
        # The seal reads the PARSED date + the RAW body, not the header text — so re-quoting the date and
        # re-ordering the header keys must NOT break verification. This is the plan-gate-hardened invariant.
        with tempfile.TemporaryDirectory() as d:
            p = self._scratch(d)
            audit_digest.seal(p, generated=JUNE, body=BODY)
            fm, body = audit_digest.split(p)
            reserialized = (f"---\nfingerprint: {fm['fingerprint']}\ngenerated: '2026-06-01'\n"
                            f"schema_version: 1\n---{body}")
            with open(p, "w", encoding="utf-8", newline="") as fh:
                fh.write(reserialized)
            self.assertEqual(audit_digest.check(p)["severity"], "note",
                             "re-quoting/re-ordering the header must not break the seal")

    def test_reseal_preserves_the_body_verbatim(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._scratch(d)
            audit_digest.seal(p, generated=JUNE, body=BODY)
            _fm, body_before = audit_digest.split(p)
            audit_digest.seal(p, generated=datetime.date(2026, 7, 1))  # re-seal, body=None
            _fm2, body_after = audit_digest.split(p)
            self.assertEqual(body_before, body_after)
            self.assertEqual(audit_digest.check(p)["severity"], "note")


class TestCheckEdgeCases(unittest.TestCase):
    def test_absent_digest_passes_the_seal_gate(self):
        with tempfile.TemporaryDirectory() as d:
            f = audit_digest.check(os.path.join(d, "audit-digest.md"))
            self.assertEqual(f["severity"], "note", "no digest yet = nothing to verify")

    def test_missing_header_fields_is_hard(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "audit-digest.md")
            with open(p, "w", encoding="utf-8", newline="") as fh:
                fh.write("---\nschema_version: 1\n---\nbody with no date or seal\n")
            self.assertEqual(audit_digest.check(p)["severity"], "hard")

    def test_no_frontmatter_at_all_is_hard(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "audit-digest.md")
            with open(p, "w", encoding="utf-8", newline="") as fh:
                fh.write("just some prose, no header at all\n")
            self.assertEqual(audit_digest.check(p)["severity"], "hard")


class TestStaleness(unittest.TestCase):
    def _dated(self, d, days_old, now):
        p = os.path.join(d, "audit-digest.md")
        audit_digest.seal(p, generated=now - datetime.timedelta(days=days_old), body=BODY)
        return p

    def test_absent_digest_says_not_run_yet(self):
        with tempfile.TemporaryDirectory() as d:
            f = audit_digest.staleness(os.path.join(d, "audit-digest.md"), now=JUNE)
            self.assertEqual(f["severity"], "soft")
            self.assertIn("hasn't run yet", f["message"])

    def test_fresh_digest_is_clear(self):
        with tempfile.TemporaryDirectory() as d:
            now = datetime.date(2026, 6, 20)
            p = self._dated(d, 1, now)
            self.assertEqual(audit_digest.staleness(p, now=now)["severity"], "note")

    def test_exactly_the_bound_is_clear(self):
        with tempfile.TemporaryDirectory() as d:
            now = datetime.date(2026, 6, 20)
            p = self._dated(d, audit_digest.STALENESS_DAYS, now)
            self.assertEqual(audit_digest.staleness(p, now=now)["severity"], "note",
                             "exactly STALENESS_DAYS old is still current")

    def test_one_day_past_the_bound_is_flagged(self):
        with tempfile.TemporaryDirectory() as d:
            now = datetime.date(2026, 6, 20)
            p = self._dated(d, audit_digest.STALENESS_DAYS + 1, now)
            f = audit_digest.staleness(p, now=now)
            self.assertEqual(f["severity"], "soft")
            self.assertIn(str(audit_digest.STALENESS_DAYS + 1), f["message"])

    def test_staleness_bound_is_thirty(self):
        # A deliberate pin: the maintainer chose 30 days; a silent change to the bound fails here.
        self.assertEqual(audit_digest.STALENESS_DAYS, 30)


if __name__ == "__main__":
    unittest.main()
