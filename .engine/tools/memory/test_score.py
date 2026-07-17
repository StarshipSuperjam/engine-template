"""Unit tests for score.py + forget.py's scored demotion (slice 4c).

Two groups: (1) the pure scoring law (score.py) — determinism, the recurrence/fold property slice-4d relies on,
the birth seed, the role-weight prior, the ageing curve, and `ts` robustness; (2) the ledger-backed demotion
through `forget` — the `record_access` appender, the raw-ledger access index, archived-exclusion-from-recall
with recoverability, the reinforcement-restores path (which doubles as the raw-read leak guard), and the
no-erasure build-conformance invariant.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import capture, consolidate, forget, index, ledger, records, score  # noqa: E402

_DAY = 86400


class ScorerMathTests(unittest.TestCase):
    """The pure scoring law — no ledger; explicit `now`, back-dated `ts`, fully deterministic."""

    def test_frecency_is_deterministic(self):
        # Case 1: same inputs -> same float, every call.
        a = score.frecency(1_000_000, [1_200_000, 1_500_000], 2_000_000)
        b = score.frecency(1_000_000, [1_200_000, 1_500_000], 2_000_000)
        self.assertEqual(a, b)

    def test_frecency_is_a_recurrence_on_the_carried_snapshot(self):
        # Case 2: the fold property slice-4d depends on — frecency(now) splits at any t into
        # decay(now-t)*frecency_snapshot(t) + sum(decay(now-a) for a after t). Exponential decay is separable,
        # so a windowed/population score (which would fail this) is structurally excluded.
        birth, now, t_split = 1_000_000, 2_000_000, 1_500_000
        accesses = [1_100_000, 1_300_000, 1_700_000, 1_900_000]
        before = [a for a in accesses if a <= t_split]
        after = [a for a in accesses if a > t_split]
        snapshot = score.frecency(birth, before, t_split)
        folded = score._decay(now - t_split) * snapshot + sum(score._decay(now - a) for a in after)
        full = score.frecency(birth, accesses, now)
        self.assertAlmostEqual(full, folded, places=12)

    def test_birth_seed_keeps_a_brand_new_record_hot(self):
        # Case 3: without the implicit birth reinforcement a never-accessed record would score 0 -> archived
        # (the archive-everything trap). A fresh default-role record scores exactly 1.0 -> hot.
        now = 10_000_000
        rec = {"ts": now}
        self.assertEqual(score.score(rec, [], now), 1.0)
        self.assertEqual(score.tier(rec, [], now), score.HOT)
        # Even the lowest-weighted role is hot when fresh (worst case dead-end 0.70 >= 0.50).
        self.assertEqual(score.tier({"ts": now, "role": "dead-end"}, [], now), score.HOT)

    def test_role_weight_is_a_per_type_prior(self):
        # Case 4: same ts + accesses, different role -> scores differ by exactly the weight ratio; identical
        # role -> identical weight (a per-type prior, never per-record).
        now = 10_000_000
        ts = now - 10 * _DAY
        s_decision = score.score({"ts": ts, "role": "decision"}, [], now)
        s_deadend = score.score({"ts": ts, "role": "dead-end"}, [], now)
        self.assertAlmostEqual(s_decision / s_deadend, 1.30 / 0.70, places=12)
        self.assertEqual(score.role_weight({"role": "lesson"}), score.role_weight({"role": "lesson"}))
        # A role-less record gets the default weight.
        self.assertEqual(score.role_weight({"ts": ts}), score.DEFAULT_ROLE_WEIGHT)

    def test_role_weights_keys_match_the_closed_role_vocabulary(self):
        # Case 4 (drift guard): ROLE_WEIGHTS must cover exactly the closed vocabulary — caught without importing
        # consolidate into score (that would cycle); the test file may import both.
        self.assertEqual(set(score.ROLE_WEIGHTS), set(consolidate.ROLE_VOCABULARY))

    def test_the_ageing_curve_pins_the_thresholds(self):
        # Case 5: a never-reinforced default-role record (score = decay(age)**2) — 14 d -> warm, 35 d -> archived.
        now = 10_000_000
        self.assertEqual(score.tier({"ts": now - 14 * _DAY}, [], now), score.WARM)
        self.assertEqual(score.tier({"ts": now - 35 * _DAY}, [], now), score.ARCHIVED)
        # And the boundaries are ordered as designed: a 5-day note is hot, a 22-day note is cold.
        self.assertEqual(score.tier({"ts": now - 5 * _DAY}, [], now), score.HOT)
        self.assertEqual(score.tier({"ts": now - 22 * _DAY}, [], now), score.COLD)

    def test_a_recent_access_pulls_an_old_record_back_up(self):
        # Case 6 (pure half): an archived-aged record + recent accesses scores strictly higher and climbs out
        # of archived — the recency spike + frecency boost.
        now = 10_000_000
        rec = {"ts": now - 35 * _DAY, "role": "lesson"}
        cold_score = score.score(rec, [], now)
        warm_score = score.score(rec, [now, now, now], now)
        self.assertGreater(warm_score, cold_score)
        self.assertEqual(score.tier(rec, [], now), score.ARCHIVED)
        self.assertIn(score.tier(rec, [now, now, now], now), (score.HOT, score.WARM))

    def test_missing_or_bad_ts_is_treated_as_now_failsafe_toward_keeping(self):
        # Case 10 (robustness): a missing / non-int / bool / future ts must never silently archive a record —
        # it is scored as born-now -> hot.
        now = 10_000_000
        for bad in ({}, {"ts": None}, {"ts": "garbage"}, {"ts": True}, {"ts": now + 99 * _DAY}):
            self.assertEqual(score.tier(bad, [], now), score.HOT, bad)

    def test_score_py_reaches_no_physical_erasure_path(self):
        # Case 11 (partial): the pure scorer touches no storage at all — and certainly no erasure.
        with open(os.path.join(os.path.dirname(__file__), "score.py"), "r", encoding="utf-8") as fh:
            src = fh.read()
        for token in ("os.remove", "os.unlink", "os.truncate", "truncate(", "rmtree", "os.replace", "open("):
            self.assertNotIn(token, src, f"score.py must stay a pure leaf: found {token!r}")


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

    def _aged_episodic(self, text, age_days, role="decision"):
        """A real, always-live (batchless) episodic, back-dated by `age_days`."""
        rec = consolidate._make_episodic("s", {"role": role, "text": text}, "b")
        rec.pop(records.BATCH_KEY, None)
        rec["ts"] = int(time.time()) - age_days * _DAY
        return rec

    def _recall(self, word):
        return index.query(word).records


class LedgerDemotionTests(_Base):
    def test_record_access_appends_a_well_formed_marker(self):
        # Case 9.
        forget.record_access("the-target-id")
        markers = [r for r in ledger.iter_records() if r.get("kind") == records.REINFORCEMENT_KIND]
        self.assertEqual(len(markers), 1)
        m = markers[0]
        self.assertEqual(m[records.TARGET_KEY], "the-target-id")
        self.assertEqual(len(m[records.RECORD_ID_KEY]), 32)            # its own distinct id
        self.assertNotEqual(m[records.RECORD_ID_KEY], "the-target-id")
        self.assertIsInstance(m["ts"], int)
        self.assertNotIn("text", m)                                   # no content
        self.assertEqual(m["tags"], [records.REINFORCEMENT_TAG])

    def test_record_access_is_a_no_op_on_a_blank_target(self):
        # Case 9.
        forget.record_access("")
        forget.record_access(None)
        self.assertEqual(list(ledger.iter_records()), [])

    def test_an_archived_record_is_excluded_from_recall_but_stays_in_the_ledger(self):
        # Case 7: recoverability — set aside from recall, still resident.
        old = self._aged_episodic("the bridge tolls double on holidays", age_days=35)
        ledger.append(old)
        index.rebuild()
        self.assertEqual(self._recall("bridge"), [])                  # excluded from recall
        self.assertNotIn(old[records.RECORD_ID_KEY],
                         [r.get(records.RECORD_ID_KEY) for r in forget.live_records()])
        in_ledger = [r for r in ledger.iter_records()
                     if r.get(records.RECORD_ID_KEY) == old[records.RECORD_ID_KEY]]
        self.assertEqual(len(in_ledger), 1)                           # still in the ledger, recoverable

    def test_reinforcing_an_archived_record_restores_it_the_raw_read_leak_guard(self):
        # Case 6: an archived-aged record, reinforced, returns to recall. This FAILS if `_access_index` reads
        # `live_records` instead of the raw ledger (the markers would be filtered out, the accesses invisible,
        # the record stuck archived) — so it pins the raw-read requirement.
        old = self._aged_episodic("the equinox parade route changed", age_days=35)
        old_id = old[records.RECORD_ID_KEY]
        ledger.append(old)
        index.rebuild()
        self.assertEqual(self._recall("equinox"), [])                 # archived first
        for _ in range(3):
            forget.record_access(old_id)
        index.rebuild()
        restored = self._recall("equinox")
        self.assertEqual(len(restored), 1)                            # back in recall
        self.assertEqual(restored[0][records.RECORD_ID_KEY], old_id)
        self.assertIn(old_id, [r.get(records.RECORD_ID_KEY) for r in forget.live_records()])

    def test_access_index_reads_markers_for_an_archived_record(self):
        # Case 6 (direct): the access index keys by target id and includes markers for an archived record,
        # because it reads the RAW ledger (live_records would have dropped both the record and its markers).
        old = self._aged_episodic("solstice", age_days=40)
        old_id = old[records.RECORD_ID_KEY]
        ledger.append(old)
        forget.record_access(old_id, now=123)
        forget.record_access(old_id, now=456)
        idx = forget._access_index(ledger.ledger_path())
        self.assertEqual(sorted(idx.get(old_id, [])), [123, 456])

    def test_reinforcement_markers_never_surface_in_recall(self):
        # Case 8: markers are pure derivation fuel — absent from live_records and from any query result.
        live = self._aged_episodic("the lighthouse beam sweeps every twelve seconds", age_days=1)
        ledger.append(live)
        forget.record_access(live[records.RECORD_ID_KEY])
        index.rebuild()
        kinds = {r.get("kind") for r in forget.live_records()}
        self.assertNotIn(records.REINFORCEMENT_KIND, kinds)
        for term in ("lighthouse", live[records.RECORD_ID_KEY]):
            self.assertEqual(
                [r for r in index.query(term).records if r.get("kind") == records.REINFORCEMENT_KIND], [])

    def test_a_consolidated_marker_is_never_demoted(self):
        # The structural `consolidated` marker stays always-live even when old — unchanged from 4a (it carries
        # no recall text and is load-bearing for _closed_batches, which reads it raw).
        marker = consolidate._make_marker("s", "b")
        marker["ts"] = int(time.time()) - 90 * _DAY                   # ancient, but must still pass through
        ledger.append(marker)
        kinds = {r.get("kind") for r in forget.live_records()}
        self.assertIn(records.MARKER_KIND, kinds)

    def test_back_compat_a_recent_record_with_no_id_or_no_role_still_recalls(self):
        # Case 10: a pre-4b record (no `id`) and a role-less curated record still score (born hot) and recall; the
        # access-index lookup for a missing id is empty, never a crash. Ambient turn-deltas are no longer recall
        # content (D-273/D-274, #332), so the role-less vehicle is a role-less episodic, not a turn-delta.
        ledger.append({"v": 1, "kind": records.EPISODIC_KIND, "session_id": "s", "ts": int(time.time()),
                       "text": "the cartographer mislabeled the map", "tags": ["episodic"]})   # no id, no role
        legacy = {"v": 1, "kind": records.EPISODIC_KIND, "session_id": "s",
                  "ts": int(time.time()), "role": "decision", "text": "no id here either", "tags": []}
        ledger.append(legacy)
        index.rebuild()
        self.assertEqual(len(self._recall("cartographer")), 1)
        self.assertEqual(len(self._recall("here")), 1)


class SnapshotAwareScoringTests(unittest.TestCase):
    """The slice-4d snapshot branch: a record carrying a frecency snapshot (minted by compaction) scores the
    SAME as the un-compacted record (the recurrence), folds post-snapshot accesses onto it, and fails safe on a
    malformed carried field (back to the deterministic birth path, never inflated to 'now'). Pure — no ledger."""

    def test_a_carried_snapshot_reproduces_the_birth_path_score(self):
        now = 2_000_000_000
        rec = {"ts": now - 10 * _DAY, "role": "lesson"}
        accesses = [now - 6 * _DAY, now - 2 * _DAY]
        s_birth = score.score(rec, accesses, now=now)
        snap, last = score.mint_snapshot(rec, accesses, now=now)        # compaction mints at t0 = now
        compacted = dict(rec, **{records.FRECENCY_SNAPSHOT_KEY: snap, records.SNAPSHOT_TS_KEY: now,
                                 records.LAST_ACCESS_TS_KEY: last})
        self.assertAlmostEqual(score.score(compacted, [], now=now), s_birth, places=9)
        # And the equality holds at a LATER time (the recurrence carries forward), with no post-snapshot accesses.
        later = now + 5 * _DAY
        self.assertAlmostEqual(score.score(compacted, [], now=later),
                               score.score(rec, accesses, now=later), places=9)

    def test_post_snapshot_accesses_fold_onto_the_carried_snapshot(self):
        now = 2_000_000_000
        rec = {"ts": now - 20 * _DAY, "role": "decision"}
        early = [now - 15 * _DAY, now - 12 * _DAY]
        t0 = now - 8 * _DAY
        snap, last = score.mint_snapshot(rec, early, now=t0)            # compacted at t0, folding `early`
        compacted = dict(rec, **{records.FRECENCY_SNAPSHOT_KEY: snap, records.SNAPSHOT_TS_KEY: t0,
                                 records.LAST_ACCESS_TS_KEY: last})
        late = [now - 4 * _DAY, now - 1 * _DAY]                         # accesses AFTER the snapshot
        # The compacted record + post-snapshot accesses scores the same as the un-compacted record + ALL accesses.
        self.assertAlmostEqual(score.score(compacted, late, now=now),
                               score.score(rec, early + late, now=now), places=9)

    def test_mint_snapshot_is_idempotent_on_recompaction(self):
        now = 2_000_000_000
        rec = {"ts": now - 10 * _DAY, "role": "intent"}
        accesses = [now - 7 * _DAY, now - 3 * _DAY]
        snap, last = score.mint_snapshot(rec, accesses, now=now)
        compacted = dict(rec, **{records.FRECENCY_SNAPSHOT_KEY: snap, records.SNAPSHOT_TS_KEY: now,
                                 records.LAST_ACCESS_TS_KEY: last})
        snap2, last2 = score.mint_snapshot(compacted, [], now=now)     # re-compact, no new accesses
        self.assertAlmostEqual(snap2, snap, places=12)
        self.assertEqual(last2, last)

    def test_a_malformed_snapshot_falls_back_to_the_birth_path_not_now(self):
        now = 2_000_000_000
        rec = {"ts": now - 20 * _DAY, "role": "lesson"}                # aged -> archived on the birth path
        s_birth = score.score(rec, [], now=now)
        for bad_snap, bad_ts in (("garbage", now - 10 * _DAY), (1.5, None), (float("inf"), now - 10 * _DAY),
                                 (float("nan"), now - 10 * _DAY), (-1.0, now - 10 * _DAY), (True, now - 10 * _DAY)):
            rec_bad = dict(rec, **{records.FRECENCY_SNAPSHOT_KEY: bad_snap, records.SNAPSHOT_TS_KEY: bad_ts})
            self.assertAlmostEqual(score.score(rec_bad, [], now=now), s_birth, places=9,
                                   msg=f"malformed ({bad_snap!r}, {bad_ts!r}) should fall back to birth, not now")
        # A genuinely fresh record (born now) is NOT what a malformed snapshot collapses to — the failure is
        # toward the record's real age, never toward 'just compacted'.
        self.assertNotAlmostEqual(s_birth, score.score({"ts": now, "role": "lesson"}, [], now=now), places=3)

    def test_a_malformed_last_access_ts_floors_to_the_snapshot_ts_not_now(self):
        now = 2_000_000_000
        snap_ts = now - 10 * _DAY
        good = {"ts": now - 20 * _DAY, "role": "decision",
                records.FRECENCY_SNAPSHOT_KEY: 1.5, records.SNAPSHOT_TS_KEY: snap_ts,
                records.LAST_ACCESS_TS_KEY: snap_ts}
        bad = dict(good, **{records.LAST_ACCESS_TS_KEY: "garbage"})
        self.assertAlmostEqual(score.score(bad, [], now=now), score.score(good, [], now=now), places=9)

    def test_valid_snapshot_rejects_nan_inf_negative_and_bool(self):
        self.assertFalse(score._valid_snapshot(float("nan"), 1))
        self.assertFalse(score._valid_snapshot(float("inf"), 1))
        self.assertFalse(score._valid_snapshot(-0.1, 1))
        self.assertFalse(score._valid_snapshot(True, 1))
        self.assertFalse(score._valid_snapshot(1.0, True))
        self.assertFalse(score._valid_snapshot("1.0", 1))
        self.assertFalse(score._valid_snapshot(1.0, None))
        self.assertFalse(score._valid_snapshot(1.0, float("nan")))   # a non-finite snapshot_ts is rejected...
        self.assertFalse(score._valid_snapshot(1.0, float("inf")))   # ...so _effective never does int(nan/inf)
        self.assertTrue(score._valid_snapshot(0.0, 1))
        self.assertTrue(score._valid_snapshot(2.5, 1_000))

    def test_non_finite_carried_timestamps_never_crash_recall(self):
        # The line-resilience law for the new 4d fields: a corrupt (NaN/inf) carried timestamp on ONE record —
        # only reachable via an out-of-band corrupted ledger line — must score (fall back to the birth path),
        # never raise `int(NaN)`/`int(inf)` and take down recall over the whole store. Also covers a non-finite
        # birth `ts` (a latent gap pre-4d) and a non-finite access timestamp.
        now = 2_000_000_000
        cases = [
            {"ts": now - 5 * _DAY, "role": "lesson", records.FRECENCY_SNAPSHOT_KEY: 2.0,
             records.SNAPSHOT_TS_KEY: float("nan"), records.LAST_ACCESS_TS_KEY: now - 5 * _DAY},
            {"ts": now - 5 * _DAY, "role": "lesson", records.FRECENCY_SNAPSHOT_KEY: 2.0,
             records.SNAPSHOT_TS_KEY: now - 3 * _DAY, records.LAST_ACCESS_TS_KEY: float("inf")},
            {"ts": float("nan"), "role": "decision"},                # non-finite birth ts
        ]
        for rec in cases:
            s = score.score(rec, [float("inf"), float("nan")], now=now)   # non-finite accesses too
            self.assertTrue(isinstance(s, float) and s == s and s != float("inf"), rec)   # finite, no crash
            self.assertIn(score.tier(rec, [], now=now), score.TIERS)


if __name__ == "__main__":
    unittest.main()
