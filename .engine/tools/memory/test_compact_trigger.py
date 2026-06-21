"""Unit tests for the live compaction TRIGGER — memory's gated PreCompact auto-compaction (slice 5, PR 3).

The compaction MECHANISM (crash-safe fold-and-swap) is pinned in test_compact.py. THIS file pins the live
TRIGGER: the gate that fires `compact()` only once enough reclaimable waste has piled up, rides the PreCompact
hook (`consolidate._pre_compact_handler`), and is fail-open + Layer-1-only. The load-bearing guard is BEHAVIORAL:
the auto-trigger path must never reduce the set of recall-CONTENT records (a name-grep would miss a real
regression — PR #153's lesson). Throwaway `ENGINE_MEMORY_DIR` cabinet throughout.
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
from memory import capture, compact, consolidate, forget, index, ledger, records, rollup  # noqa: E402

_DAY = 86400
_THRESHOLD = compact._COMPACT_WASTE_THRESHOLD
_CONTENT_KINDS = (records.EPISODIC_KIND, records.GIST_KIND, capture.RECORD_KIND)


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

    def _episodic(self, text, *, age_days=0, role="decision", session_id="S", batchless=True):
        rec = consolidate._make_episodic(session_id, {"role": role, "text": text}, "b")
        if batchless:
            rec.pop(records.BATCH_KEY, None)
        rec["ts"] = int(time.time()) - age_days * _DAY
        ledger.append(rec)
        return rec

    def _raws(self, n, *, session_id="roll-S", age_days=25):
        return [self._episodic(f"old note {i} word{i}", age_days=age_days, session_id=session_id)
                [records.RECORD_ID_KEY] for i in range(n)]

    def _turn_delta(self, text, *, session="raw-S", seq=0, speaker="user"):
        ledger.append(capture._make_record(session, seq, speaker, text))

    def _pile_waste(self, rid, n):
        for _ in range(n):
            forget.record_access(rid)

    def _content_ids(self):
        return {r.get(records.RECORD_ID_KEY) for r in ledger.iter_records()
                if isinstance(r, dict) and r.get("kind") in _CONTENT_KINDS}

    def _whole(self):
        report = ledger.read()
        return os.path.exists(ledger.ledger_path()) and not report.torn_trailing

    def _run_hook(self, event, handler, payload):
        out, err = io.StringIO(), io.StringIO()
        code = hooks.run_hook(event, handler, stdin=io.StringIO(json.dumps(payload)), stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()


class GateTests(_Base):
    def test_skips_a_clean_ledger_with_no_rewrite(self):
        self._episodic("a note", age_days=0)
        index.rebuild()
        gen_before = ledger.generation()
        self.assertFalse(compact.should_compact())
        report = compact.maybe_compact()
        self.assertEqual(report["status"], "skipped")
        self.assertEqual(ledger.generation(), gen_before)              # nothing was rewritten

    def test_fires_above_threshold(self):
        live = self._episodic("a used note", age_days=0)
        self._pile_waste(live[records.RECORD_ID_KEY], _THRESHOLD)
        index.rebuild()
        gen_before = ledger.generation()
        self.assertTrue(compact.should_compact())
        report = compact.maybe_compact()
        self.assertEqual(report["status"], "ok")
        self.assertGreater(ledger.generation(), gen_before)            # it actually compacted
        self.assertEqual(compact.reclaimable_waste(), 0)               # and the waste was folded away

    def test_unclosed_supersessions_never_trip_the_gate(self):
        # S2: the gate counts only CLOSED-batch supersessions (exactly what compact prunes), never un-closed
        # (crashed-pass) ones — else it would fire and rewrite a byte-identical ledger forever (waste never drops).
        raws = self._raws(_THRESHOLD + 4, session_id="crash-S")
        try:
            rollup.store_gist("crash-S", [{"role": "lesson", "text": "summary",
                                           records.SOURCE_IDS_KEY: raws}], _crash_after="markers")
        except rollup._InjectedCrash:
            pass                                                       # batch written but NOT closed
        index.rebuild()
        self.assertGreaterEqual(len(raws), _THRESHOLD)
        self.assertEqual(compact.reclaimable_waste(), 0)              # un-closed supersessions are not reclaimable
        self.assertFalse(compact.should_compact())

    def test_reclaimable_waste_and_should_compact_are_read_only(self):
        live = self._episodic("a note", age_days=0)
        self._pile_waste(live[records.RECORD_ID_KEY], 3)
        index.rebuild()
        gen_before = ledger.generation()
        size_before = os.path.getsize(ledger.ledger_path())
        compact.reclaimable_waste()
        compact.should_compact()
        self.assertEqual(ledger.generation(), gen_before)
        self.assertEqual(os.path.getsize(ledger.ledger_path()), size_before)   # the gate never writes


class Layer1InvariantTests(_Base):
    def test_auto_trigger_never_erases_unmarked_content(self):
        # The Layer-1 invariant, enforced BEHAVIORALLY through the REAL PreCompact handler with NO erasure marker
        # present: the auto-trigger may fold/prune only NON-recall markers; it must never reduce the set of
        # recall-CONTENT records (turn-delta / episodic / gist). Plant a mix, capture the content ids, fire the
        # trigger, assert IDENTICAL. (Mutation: deleting any content record before the compare makes this fail. The
        # MARKED case — a valid marker DOES erase its target via this same live path — is the next test.)
        live = self._episodic("a fresh live decision", age_days=0)
        self._episodic("an old archived lesson", age_days=40, role="lesson")            # archived, still content
        raws = self._raws(3, session_id="roll-S")                                       # rolled up -> closed waste
        rollup.store_gist("roll-S", [{"role": "lesson", "text": "rolled-up summary compendium",
                                      records.SOURCE_IDS_KEY: raws}])
        self._episodic("a crashed-pass duplicate", age_days=0, batchless=False)         # a 4a orphan, still content
        self._turn_delta("a raw turn note")                                             # a turn-delta content record
        self._pile_waste(live[records.RECORD_ID_KEY], _THRESHOLD)                        # push waste over the line
        index.rebuild()
        before = self._content_ids()
        self.assertGreaterEqual(compact.reclaimable_waste(), _THRESHOLD)
        code, _out, _err = self._run_hook("PreCompact", consolidate._pre_compact_handler, {"trigger": "auto"})
        self.assertEqual(code, hooks.EXIT_PROCEED)                    # the trigger fired AND the squash proceeded
        self.assertEqual(self._content_ids(), before)                # every recall-content record survived
        self.assertEqual(compact.reclaimable_waste(), 0)             # the non-recall waste was actually folded away

    def test_auto_trigger_erases_only_a_marked_target(self):
        # The LIVE erasure path end-to-end (the only end-to-end test of the unattended squash-triggered erasure):
        # fire the REAL PreCompact handler with a VALID erasure marker present AND enough reclaimable waste to trip
        # the gate (the gate counts foldable markers, NOT erasure markers, so the marked record is removed only when
        # a compaction actually fires). The marked target is physically gone; every UNMARKED content id survives;
        # the marker is retained; a 2nd auto-fire (waste now 0 -> gate skips) changes nothing. (Mutation: drop the
        # `_is_erased` continue -> the target survives; invert it -> a kept id vanishes.)
        def slips():
            return sum(1 for r in ledger.iter_records()
                       if isinstance(r, dict) and r.get("kind") == records.ERASURE_KIND)
        target = self._episodic("erase this one", age_days=0)
        keep = self._episodic("keep this one", age_days=0)
        live = self._episodic("a used note", age_days=0)
        self._pile_waste(live[records.RECORD_ID_KEY], _THRESHOLD)        # trip the gate so maybe_compact fires compact
        compact.enact_erasure(target[records.RECORD_ID_KEY], "merge-sha-xyz")
        index.rebuild()
        before = self._content_ids()
        self.assertIn(target[records.RECORD_ID_KEY], before)
        self.assertTrue(compact.should_compact())
        code, _out, _err = self._run_hook("PreCompact", consolidate._pre_compact_handler, {"trigger": "auto"})
        self.assertEqual(code, hooks.EXIT_PROCEED)                    # the trigger fired AND the squash proceeded
        after = self._content_ids()
        self.assertNotIn(target[records.RECORD_ID_KEY], after)        # the marked target is physically gone
        self.assertEqual(after, before - {target[records.RECORD_ID_KEY]})  # and ONLY it (every unmarked id survives)
        self.assertEqual(slips(), 1)                                  # the marker is retained (the tombstone)
        code2, _o2, _e2 = self._run_hook("PreCompact", consolidate._pre_compact_handler, {"trigger": "auto"})
        self.assertEqual(code2, hooks.EXIT_PROCEED)
        self.assertEqual(self._content_ids(), after)                 # 2nd fire: gate skips (waste folded), no change
        self.assertEqual(slips(), 1)


class FailOpenTests(_Base):
    def test_maybe_compact_swallows_a_fault_and_never_raises(self):
        live = self._episodic("a note", age_days=0)
        self._pile_waste(live[records.RECORD_ID_KEY], _THRESHOLD)
        index.rebuild()
        with mock.patch.object(compact, "compact", side_effect=RuntimeError("disk full")):
            report = compact.maybe_compact()                          # waste is over the line, so it WOULD compact
        self.assertEqual(report["status"], "skipped")                # the fault was swallowed, not raised
        self.assertTrue(self._whole())                               # the ledger is intact (compact never ran)

    def test_pre_compact_handler_always_proceeds_even_on_fault(self):
        live = self._episodic("a note", age_days=0)
        self._pile_waste(live[records.RECORD_ID_KEY], _THRESHOLD)
        index.rebuild()
        with mock.patch.object(compact, "compact", side_effect=RuntimeError("disk full")):
            code, _out, _err = self._run_hook("PreCompact", consolidate._pre_compact_handler, {"trigger": "auto"})
        self.assertEqual(code, hooks.EXIT_PROCEED)                   # PreCompact must never block the squash


class DemoTests(_Base):
    def test_demo_trigger_runs_clean(self):
        self.assertEqual(compact._demo_trigger(), 0)


if __name__ == "__main__":
    unittest.main()
