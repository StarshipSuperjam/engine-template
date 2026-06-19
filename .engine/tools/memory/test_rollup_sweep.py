"""Unit tests for the live roll-up SWEEP — memory's SessionStart caller for gist roll-up (slice 5, PR 3).

The roll-up MECHANISM (detect / store / fold) is pinned in test_rollup.py. THIS file pins the live CALLER: the
SessionStart sweep that hands the in-context AI the cold session-groups to roll up. It is FOLDED INTO memory's one
SessionStart behavior (`consolidate._session_start_handler`), which injects ONE combined background directive
carrying both the consolidation backlog (3b) and the roll-up backlog (this slice). These tests drive the REAL
handlers through the REAL fail-open `run_hook` harness over a throwaway `ENGINE_MEMORY_DIR` cabinet.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hooks  # noqa: E402
from memory import capture, consolidate, index, ledger, records, rollup  # noqa: E402

_DAY = 86400


class _Base(unittest.TestCase):
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

    def _cold_group(self, n=3, *, session_id="old-S", age_days=25):
        """Plant `n` COLD episodics of one session -> a roll-up candidate group."""
        for i in range(n):
            rec = consolidate._make_episodic(session_id, {"role": "decision", "text": f"old note {i} word{i}"}, "b")
            rec.pop(records.BATCH_KEY, None)
            rec["ts"] = int(time.time()) - age_days * _DAY
            ledger.append(rec)
        index.rebuild()

    def _consolidation_backlog(self, *, session_id="raw-S"):
        """Plant raw turn-deltas of a non-live session with no marker -> a consolidation candidate."""
        ledger.append(capture._make_record(session_id, 0, "user", "decided the harbor lights go solar"))
        index.rebuild()

    def _run_hook(self, event, handler, payload):
        out, err = io.StringIO(), io.StringIO()
        code = hooks.run_hook(event, handler, stdin=io.StringIO(json.dumps(payload)), stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()


class DirectiveTests(_Base):
    def test_directive_is_subordinate_and_names_the_rollup_verbs_and_sources(self):
        d = rollup.rollup_directive({"old-S": []})
        self.assertIn("NOT to be done before the operator", d)
        self.assertIn("after you have served the operator", d.lower())   # never a first-turn hijack
        self.assertIn("rollup.py read", d)
        self.assertIn("rollup.py store", d)
        self.assertIn("source_ids", d)                                   # the roll-up-specific obligation
        self.assertNotIn("consolidate.py", d)                            # must not cross-wire to the other sweep

    def test_directive_role_list_matches_the_validators_closed_vocabulary(self):
        d = rollup.rollup_directive({"old-S": []})
        for role in consolidate.ROLE_VOCABULARY:
            self.assertIn(role, d)

    def test_directive_caps_the_id_enumeration(self):
        many = {f"sess-{i}": [] for i in range(rollup._MAX_DIRECTIVE_IDS + 5)}
        self.assertIn("and 5 more", rollup.rollup_directive(many))       # capped, not thousands of ids listed


class IsolatedSweepTests(_Base):
    def test_injects_when_a_cold_group_exists(self):
        self._cold_group()
        dec = rollup._session_start_handler({"session_id": "live-now"})
        self.assertEqual(dec["action"], "inject")
        self.assertIn("old-S", dec["context"])

    def test_silent_when_empty(self):
        self.assertEqual(rollup._session_start_handler({"session_id": "x"})["action"], "proceed")

    def test_silent_when_notes_are_too_fresh(self):
        self._cold_group(age_days=0)                                     # below the COLD floor -> no candidates
        self.assertEqual(rollup._session_start_handler({"session_id": "x"})["action"], "proceed")


class CombinedSweepTests(_Base):
    # the 4-quadrant matrix: consolidation backlog x roll-up backlog, each empty / non-empty
    def test_both_empty_is_inert(self):
        code, out, _err = self._run_hook("SessionStart", consolidate._session_start_handler, {"session_id": "x"})
        self.assertEqual(code, hooks.EXIT_PROCEED)
        self.assertEqual(out.strip(), "")                               # adds nothing on a nothing-pending start

    def test_consolidation_only(self):
        self._consolidation_backlog()
        dec = consolidate._session_start_handler({"session_id": "live-now"})
        self.assertEqual(dec["action"], "inject")
        self.assertIn("consolidate.py read", dec["context"])            # consolidation section present
        self.assertNotIn("rollup.py read", dec["context"])             # no roll-up section

    def test_rollup_only(self):
        self._cold_group()
        dec = consolidate._session_start_handler({"session_id": "live-now"})
        self.assertEqual(dec["action"], "inject")
        self.assertIn("rollup.py read", dec["context"])                # roll-up section present
        self.assertNotIn("consolidate.py read", dec["context"])        # no consolidation section

    def test_both_inject_one_combined_block_with_both_sections(self):
        self._consolidation_backlog()
        self._cold_group()
        dec = consolidate._session_start_handler({"session_id": "live-now"})
        self.assertEqual(dec["action"], "inject")                       # ONE injection, not two competing ones
        self.assertIn("consolidate.py read", dec["context"])           # consolidation section
        self.assertIn("rollup.py read", dec["context"])                # roll-up section
        self.assertIn("after you have served the operator", dec["context"].lower())   # no-hijack clause present

    def test_a_rollup_fault_degrades_to_consolidation_only(self):
        # fine-grained fail-open: a roll-up crash must NEVER drop the older, more important consolidation directive
        self._consolidation_backlog()
        with mock.patch.object(rollup, "detect_rollup_candidates", side_effect=RuntimeError("boom")):
            dec = consolidate._session_start_handler({"session_id": "live-now"})
        self.assertEqual(dec["action"], "inject")
        self.assertIn("consolidate.py read", dec["context"])           # consolidation survived the roll-up fault
        self.assertNotIn("rollup.py read", dec["context"])

    def test_handler_crash_fails_open_through_run_hook(self):
        boom = lambda _p: (_ for _ in ()).throw(RuntimeError("boom"))
        code, _out, err = self._run_hook("SessionStart", boom, {"session_id": "x"})
        self.assertEqual(code, hooks.EXIT_NONBLOCKING)
        self.assertTrue(err.strip())                                   # a plain-language finding was emitted


class DemoTests(_Base):
    def test_demo_sweep_runs_clean(self):
        # the operator demo drives the REAL handler on throwaway cabinets; it must exit 0 (no !!! regression)
        self.assertEqual(rollup._demo_sweep(), 0)


if __name__ == "__main__":
    unittest.main()
