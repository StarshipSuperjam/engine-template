"""Unit tests for compact.py — ledger compaction, the crash-safe rebuild-and-swap (memory slice 4d).

Compaction is Layer-1: reversible, mechanical, autonomous. It folds a record's reinforcement markers into a
carried frecency snapshot (so demotion survives the fold — the recurrence property), prunes those markers, and
swaps a fresh ledger in atomically under the single-writer lock. These tests exercise the REAL fold + swap +
generation gate + lock through a throwaway `ENGINE_MEMORY_DIR` cabinet, with an injected power-cut at each swap
point, and pin the load-bearing invariants: a crash leaves exactly one intact ledger (old or new); recall
content is NEVER dropped (only the non-recall markers are); the 4b id is preserved; the score is identical
before and after; the generation gate routes a crash-staled index to the scan; a leftover temp is reaped and
never promoted; and `record_access` is held under the lock so a swap can never race it.
"""

from __future__ import annotations

import inspect
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import capture, compact, consolidate, forget, index, ledger, records, score  # noqa: E402

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

    def _episodic(self, text, *, age_days=0, role="decision", batchless=True):
        rec = consolidate._make_episodic("S", {"role": role, "text": text}, "b")
        if batchless:
            rec.pop(records.BATCH_KEY, None)
        rec["ts"] = int(time.time()) - age_days * _DAY
        ledger.append(rec)
        return rec

    def _content_ids(self):
        return {r.get(records.RECORD_ID_KEY) for r in ledger.iter_records()
                if isinstance(r, dict) and r.get("kind") in (records.EPISODIC_KIND, capture.RECORD_KIND)}

    def _kinds(self):
        return [r.get("kind") for r in ledger.iter_records() if isinstance(r, dict)]

    def _reinforcements(self):
        return sum(1 for k in self._kinds() if k == records.REINFORCEMENT_KIND)

    def _scratch(self):
        return sum(1 for n in os.listdir(self._tmp.name)
                   if n.startswith(compact._TEMP_PREFIX) and n.endswith(compact._TEMP_SUFFIX))

    def _by_id(self, rid):
        return [r for r in ledger.iter_records() if isinstance(r, dict) and r.get(records.RECORD_ID_KEY) == rid]


class CrashSafeSwapTests(_Base):
    def test_a_crash_before_the_swap_leaves_the_old_ledger_intact(self):
        e = self._episodic("alpha bridge note")
        rid = e[records.RECORD_ID_KEY]
        for _ in range(3):
            forget.record_access(rid)
        ids = self._content_ids()
        book = self._reinforcements()
        gen0 = ledger.generation()
        with self.assertRaises(compact._InjectedCrash):
            compact.compact(_crash_after="write")
        # OLD ledger intact: content preserved, markers NOT pruned (the tidy didn't take), gen unbumped, complete.
        self.assertEqual(self._content_ids(), ids)
        self.assertEqual(self._reinforcements(), book)
        self.assertEqual(ledger.generation(), gen0)
        self.assertFalse(ledger.read().torn_trailing)
        self.assertGreaterEqual(self._scratch(), 1)            # the half-finished temp is left
        # Recovery: a clean pass reaps the leftover and completes.
        self.assertEqual(compact.compact()["status"], "ok")
        self.assertEqual(self._scratch(), 0)
        self.assertEqual(self._content_ids(), ids)
        self.assertEqual(self._reinforcements(), 0)

    def test_a_crash_after_the_swap_leaves_the_new_ledger_intact(self):
        e = self._episodic("equinox parade note")
        rid = e[records.RECORD_ID_KEY]
        for _ in range(3):
            forget.record_access(rid)
        ids = self._content_ids()
        index.rebuild()                                        # an index at generation 0
        with self.assertRaises(compact._InjectedCrash):
            compact.compact(_crash_after="swap")
        # NEW ledger in place: content preserved, markers folded away, gen bumped, complete, no leftover temp.
        self.assertEqual(self._content_ids(), ids)
        self.assertEqual(self._reinforcements(), 0)
        self.assertEqual(ledger.generation(), 1)
        self.assertFalse(ledger.read().torn_trailing)
        self.assertEqual(self._scratch(), 0)
        # The index is now generation-stale (built at 0, ledger at 1) -> query falls back to the scan, still finds it.
        q = index.query("equinox")
        self.assertTrue(q.degraded)                            # the gen-gate routed it to the scan
        self.assertEqual(len(q.records), 1)
        # A clean pass rebuilds the index -> the fast path returns.
        compact.compact()
        self.assertFalse(index.query("equinox").degraded)

    def test_exactly_one_intact_ledger_after_either_crash(self):
        for crash in ("write", "swap"):
            with self.subTest(crash=crash):
                self._tmp.cleanup(); self._tmp = tempfile.TemporaryDirectory()
                os.environ[ledger.ENV_DIR] = self._tmp.name
                e = self._episodic("solstice note")
                forget.record_access(e[records.RECORD_ID_KEY])
                with self.assertRaises(compact._InjectedCrash):
                    compact.compact(_crash_after=crash)
                read = ledger.read()
                self.assertFalse(read.torn_trailing)           # the canonical ledger is whole, never half-written
                self.assertIn(e[records.RECORD_ID_KEY],
                              [r.get(records.RECORD_ID_KEY) for r in read.records])


class FoldPreservesScoreTests(_Base):
    def test_the_score_is_identical_before_and_after_compaction(self):
        now = 2_000_000_000
        e = self._episodic("the cartographer note", role="lesson")
        e["ts"] = now - 10 * _DAY
        rid = e[records.RECORD_ID_KEY]
        # Re-append the back-dated record (the demo/test factory appended a now-ish one; overwrite via accesses' ts).
        for ts in (now - 6 * _DAY, now - 2 * _DAY):
            forget.record_access(rid, now=ts)
        accesses = forget._access_index(ledger.ledger_path()).get(rid, [])
        # Score the LIVE (un-compacted) record at two times.
        live = [r for r in ledger.iter_records() if r.get(records.RECORD_ID_KEY) == rid
                and r.get("kind") == records.EPISODIC_KIND][0]
        s_before_t0 = score.score(live, accesses, now=now)
        s_before_t1 = score.score(live, accesses, now=now + 4 * _DAY)
        compact.compact(now=now)                               # fold at t0 = now
        comp = self._by_id(rid)[0]
        self.assertIn(records.FRECENCY_SNAPSHOT_KEY, comp)     # the snapshot was carried
        after_acc = forget._access_index(ledger.ledger_path()).get(rid, [])
        self.assertEqual(after_acc, [])                        # the markers were folded away
        s_after_t0 = score.score(comp, after_acc, now=now)
        s_after_t1 = score.score(comp, after_acc, now=now + 4 * _DAY)
        self.assertAlmostEqual(s_before_t0, s_after_t0, places=9)
        self.assertAlmostEqual(s_before_t1, s_after_t1, places=9)   # the recurrence holds across time

    def test_a_record_reinforced_into_a_different_tier_keeps_its_recall_membership(self):
        # An archived-aged record (40 d, no accesses -> archived) reinforced enough to climb out of archived:
        # its recall VISIBILITY (not merely its scalar score) must be unchanged across compaction.
        e = self._episodic("the lighthouse note", age_days=40, role="lesson")
        rid = e[records.RECORD_ID_KEY]
        for _ in range(5):
            forget.record_access(rid)
        live_ids = lambda: {r.get(records.RECORD_ID_KEY) for r in forget.live_records()}
        self.assertIn(rid, live_ids())                         # reinforced -> visible before compaction
        compact.compact()
        self.assertIn(rid, live_ids())                         # still visible after the fold (tier preserved)
        comp = self._by_id(rid)[0]
        self.assertNotEqual(score.tier(comp, ()), score.ARCHIVED)


class IdAndPruneTests(_Base):
    def test_the_4b_id_is_preserved_on_re_append(self):
        e = self._episodic("the quokka decision")
        rid = e[records.RECORD_ID_KEY]
        forget.record_access(rid)
        compact.compact()
        survivors = self._by_id(rid)
        self.assertEqual(len(survivors), 1)                    # exactly one content record, same id
        self.assertEqual(survivors[0].get("kind"), records.EPISODIC_KIND)

    def test_reinforcement_markers_are_pruned(self):
        e = self._episodic("pelican note")
        for _ in range(4):
            forget.record_access(e[records.RECORD_ID_KEY])
        compact.compact()
        self.assertNotIn(records.REINFORCEMENT_KIND, self._kinds())

    def test_an_un_reinforced_record_is_rewritten_verbatim(self):
        # The degenerate-live shape: no markers -> compaction folds nothing onto the record (no snapshot fields).
        e = self._episodic("the verbatim note")
        before = {k: v for k, v in e.items()}
        compact.compact()
        after = self._by_id(e[records.RECORD_ID_KEY])[0]
        self.assertEqual(after, before)                        # byte-for-byte the same record (id preserved)
        self.assertNotIn(records.FRECENCY_SNAPSHOT_KEY, after)


class NeverDropsRecallContentTests(_Base):
    def test_every_content_record_survives_compaction(self):
        a = self._episodic("the manifest note")
        b = self._episodic("the migration note", role="decision")
        ledger.append(capture._make_record("S", 0, "user", "a raw turn note about turnips"))
        forget.record_access(a[records.RECORD_ID_KEY])
        ids = self._content_ids()
        compact.compact()
        self.assertEqual(self._content_ids(), ids)             # superset (==) before -> after; nothing dropped

    def test_a_crash_duplicate_orphan_survives_and_stays_retired(self):
        # An orphan episodic (batch never closed) + a completed pass; the orphan is retired (4a) but NOT erased.
        orphan = consolidate._make_episodic("S", {"role": "decision", "text": "orphaned summary"}, "batch-x")
        ledger.append(orphan)
        consolidate.store_episodic("S", [{"role": "decision", "text": "the completed summary"}])
        orphan_id = orphan[records.RECORD_ID_KEY]
        self.assertNotIn(orphan_id, [r.get(records.RECORD_ID_KEY) for r in forget.live_records()])
        compact.compact()
        self.assertEqual(len(self._by_id(orphan_id)), 1)                                  # survived the rewrite
        self.assertNotIn(orphan_id, [r.get(records.RECORD_ID_KEY) for r in forget.live_records()])  # still retired
        self.assertIn(records.MARKER_KIND, self._kinds())                                 # the marker survived too

    def test_an_archived_record_survives_the_rewrite(self):
        # The fold runs over the RAW ledger, not live_records — so an archived (recall-excluded) record's row is
        # NEVER silently dropped. (Folding over live_records would lose it: a true content loss.)
        archived = self._episodic("the buried gantry note", age_days=40, role="lesson")
        aid = archived[records.RECORD_ID_KEY]
        self.assertEqual(score.tier(archived, ()), score.ARCHIVED)
        self.assertNotIn(aid, [r.get(records.RECORD_ID_KEY) for r in forget.live_records()])  # excluded from recall
        compact.compact()
        self.assertEqual(len(self._by_id(aid)), 1)             # but still resident in the ledger


class SearchBodyTests(_Base):
    def test_the_carried_tier_word_is_not_searchable(self):
        # The carried `tier` is a STRING ("hot"/"cold"/"archived") on every compacted record — it MUST stay out
        # of the search body, else a query for one of those words would spuriously surface every compacted
        # record. The note's own text contains no tier word, so a hit on "hot"/"archived" could only be the
        # leaked field.
        e = self._episodic("the riverside survey note")               # no tier word in the text
        rid = e[records.RECORD_ID_KEY]
        for _ in range(3):
            forget.record_access(rid)
        compact.compact()
        comp = self._by_id(rid)[0]
        self.assertIn(records.TIER_KEY, comp)                         # it DID get a carried tier...
        index.rebuild()
        for word in (records.TIER_KEY, score.HOT, score.WARM, score.COLD, score.ARCHIVED):
            self.assertEqual(index.query(word).records, [], f"the carried {word!r} leaked into search")
        self.assertEqual(len(index.query("riverside").records), 1)    # ...but its real words are still findable


class GenerationTests(_Base):
    def test_generation_increments_per_compaction(self):
        self._episodic("a note")
        self.assertEqual(ledger.generation(), 0)
        compact.compact()
        self.assertEqual(ledger.generation(), 1)
        compact.compact()
        self.assertEqual(ledger.generation(), 2)

    def test_a_gen_stale_index_falls_back_to_the_scan(self):
        e = self._episodic("findable note")
        index.rebuild()                                        # index built at generation 0
        self.assertFalse(index.query("findable").degraded)     # fast path: index gen == ledger gen
        ledger.bump_generation()                               # ledger -> 1, index still 0 (stale)
        q = index.query("findable")
        self.assertTrue(q.degraded)                            # gen mismatch -> scan, never a stale fast answer
        self.assertEqual(len(q.records), 1)
        index.rebuild()                                        # rebuild stamps gen 1 -> fast again
        self.assertFalse(index.query("findable").degraded)

    def test_the_gen_gate_reads_the_queried_ledgers_own_sidecar_not_the_env_default(self):
        # The plan-gate's SERIOUS finding: an explicit ledger_file/index_file must compare against THAT store's
        # generation sidecar, never the ENGINE_MEMORY_DIR default. A SECOND store (not the env dir) proves it.
        other = tempfile.mkdtemp()
        try:
            led = os.path.join(other, ledger.LEDGER_FILENAME)
            idx = os.path.join(other, index.INDEX_FILENAME)
            ledger.append({"v": 1, "kind": records.EPISODIC_KIND, "session_id": "S",
                           records.RECORD_ID_KEY: records.new_record_id(), "ts": int(time.time()),
                           "text": "an offsite note", "tags": ["episodic"]}, path=led)
            index.rebuild(ledger_file=led, index_file=idx)     # stamps generation 0 from `other`'s sidecar (absent -> 0)
            self.assertFalse(index.query("offsite", ledger_file=led, index_file=idx).degraded)
            ledger.bump_generation(for_path=led)               # writes other/ledger-meta.json -> 1 (NOT the env dir)
            q = index.query("offsite", ledger_file=led, index_file=idx)
            self.assertTrue(q.degraded)                        # reads `other`'s gen (1) != index gen (0) -> scan
            self.assertEqual(len(q.records), 1)
        finally:
            shutil.rmtree(other, ignore_errors=True)


class LockTests(_Base):
    def test_compaction_reports_busy_when_the_single_writer_lock_is_held(self):
        self._episodic("a note")
        held = capture._acquire_lock(os.path.join(ledger.ledger_dir(), capture.LOCK_FILENAME))
        self.assertIsNotNone(held)
        try:
            self.assertEqual(compact.compact()["status"], "busy")   # never writes lock-free
        finally:
            capture._release_lock(held)

    def test_record_access_is_a_no_op_while_the_lock_is_held(self):
        e = self._episodic("a note")
        rid = e[records.RECORD_ID_KEY]
        held = capture._acquire_lock(os.path.join(ledger.ledger_dir(), capture.LOCK_FILENAME))
        try:
            forget.record_access(rid)                          # contended -> skipped, never lock-free
        finally:
            capture._release_lock(held)
        self.assertEqual(self._reinforcements(), 0)            # nothing was appended under contention
        forget.record_access(rid)                              # lock free now -> the marker lands
        self.assertEqual(self._reinforcements(), 1)


class ProductionSafetyTests(_Base):
    def test_compact_never_injects_a_crash_by_default(self):
        # The fault injector defaults OFF, so no production caller can reach it.
        self.assertIsNone(inspect.signature(compact.compact).parameters["_crash_after"].default)
        self._episodic("a note")
        self.assertEqual(compact.compact()["status"], "ok")    # a real pass completes, never raises

    def test_a_leftover_temp_is_reaped_and_never_promoted(self):
        # Recovery binds to the fixed canonical name: a complete same-schema leftover temp is ignored-and-reaped,
        # never mistaken for the canonical ledger.
        e = self._episodic("the canonical note")
        bogus = os.path.join(self._tmp.name, compact._TEMP_PREFIX + "deadbeef" + compact._TEMP_SUFFIX)
        with open(bogus, "w", encoding="utf-8") as fh:
            fh.write('{"kind":"episodic","text":"a stray leftover that must never become canonical"}\n')
        compact.compact()
        self.assertEqual(self._scratch(), 0)                   # the leftover was reaped
        self.assertEqual(len(self._by_id(e[records.RECORD_ID_KEY])), 1)   # the canonical note is the survivor
        self.assertEqual(index.query("stray").records, [])     # the leftover was never promoted into recall


if __name__ == "__main__":
    unittest.main()
