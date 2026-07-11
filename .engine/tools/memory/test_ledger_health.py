"""Unit tests for ledger_health.py — memory's boot-surfaced health detector (#396 U07b).

`detect_ledger_malformed` reports a rotting ledger (unreadable lines) so boot can disclose it. It reports
ONLY genuine malformed lines — never a torn trailing line, which is the normal self-healing post-crash state.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import ledger, ledger_health  # noqa: E402


class DetectLedgerMalformedTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._prev = os.environ.get(ledger.ENV_DIR)
        os.environ[ledger.ENV_DIR] = self._tmp.name

    def tearDown(self):
        if self._prev is None:
            os.environ.pop(ledger.ENV_DIR, None)
        else:
            os.environ[ledger.ENV_DIR] = self._prev
        self._tmp.cleanup()

    def _raw(self, text: str) -> None:
        with open(ledger.ledger_path(), "a", encoding="utf-8") as fh:
            fh.write(text)

    def test_a_clean_ledger_reports_zero(self):
        ledger.append({"kind": "episodic", "text": "clean"})
        self.assertEqual(ledger_health.detect_ledger_malformed(), 0)

    def test_a_missing_ledger_reports_zero(self):
        self.assertEqual(ledger_health.detect_ledger_malformed(), 0)   # the substrate ships empty

    def test_malformed_lines_are_counted(self):
        ledger.append({"kind": "episodic", "text": "before"})
        self._raw("not json at all\n")
        self._raw("{also not valid\n")
        ledger.append({"kind": "episodic", "text": "after"})
        self.assertEqual(ledger_health.detect_ledger_malformed(), 2)

    def test_a_torn_trailing_line_is_not_reported(self):
        """A torn trailing line is the normal, self-healing post-crash state — never surfaced as rot."""
        ledger.append({"kind": "episodic", "text": "kept"})
        self._raw('{"kind":"episodic","text":"torn, no newline"')  # torn trailing, no terminator
        self.assertEqual(ledger_health.detect_ledger_malformed(), 0)


if __name__ == "__main__":
    unittest.main()
