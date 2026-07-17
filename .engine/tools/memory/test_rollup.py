"""Unit tests for rollup.py — gist roll-up, the AI-judged second-order consolidation (memory slice 4d-ii).

Roll-up is Layer-1: reversible, mechanical, autonomous. It consolidates old episodes into a compact GIST and
LOGICALLY RETIRES the raws (excluded from recall, still resident + fully recoverable). These tests exercise the
REAL detect + store + supersession gate + compact fold + recall through a throwaway `ENGINE_MEMORY_DIR` cabinet,
and pin the load-bearing invariants:

  * the crash-safety spine — a raw is hidden ONLY once its roll-up batch is closed (a crash before the closing
    marker hides nothing and never leaves a raw hidden without its gist; a crashed pass's gist is itself retired);
  * the compact fold — a CLOSED-batch supersession folds into the raw's carried `superseded_by` and prunes the
    marker, while an UN-closed (crashed-pass) supersession is NEVER folded (the key recall-loss guard) — and the
    gist↔raw link is recoverable and idempotent across re-compaction;
  * recall content is NEVER dropped (the gist + every raw survive the rewrite; a superseded-AND-archived raw is
    excluded for both reasons and still survives);
  * the carried uuid-hex link fields never leak into search; the store is idempotent + lock-guarded; and rollup.py
    stays append-only (no erasure token).
"""

from __future__ import annotations

import inspect
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import capture, compact, consolidate, forget, index, ledger, records, rollup, score  # noqa: E402

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

    def _episodic(self, text, *, age_days=0, role="decision", session_id="S", batchless=True, tags=None):
        payload = {"role": role, "text": text}
        if tags is not None:
            payload["tags"] = tags
        rec = consolidate._make_episodic(session_id, payload, "b")
        if batchless:
            rec.pop(records.BATCH_KEY, None)
        rec["ts"] = int(time.time()) - age_days * _DAY
        ledger.append(rec)
        return rec

    def _raws(self, n, *, age_days=25, session_id="S", prefix="old note"):
        """Plant `n` batchless episodics and return their ids (the source set for a roll-up)."""
        return [self._episodic(f"{prefix} number {i} word{i}", age_days=age_days, session_id=session_id)
                [records.RECORD_ID_KEY] for i in range(n)]

    def _rollup(self, source_ids, *, session_id="S", role="lesson",
                text="Rolled-up summary compendium of the older notes.", crash=None):
        return rollup.store_gist(
            session_id, [{"role": role, "text": text, records.SOURCE_IDS_KEY: list(source_ids)}],
            _crash_after=crash)

    def _live_ids(self):
        return {r.get(records.RECORD_ID_KEY) for r in forget.live_records() if isinstance(r, dict)}

    def _kinds(self):
        return [r.get("kind") for r in ledger.iter_records() if isinstance(r, dict)]

    def _gist_id(self, session_id="S"):
        for r in ledger.iter_records():
            if isinstance(r, dict) and r.get("kind") == records.GIST_KIND and r.get("session_id") == session_id:
                return r.get(records.RECORD_ID_KEY)
        return None

    def _carried_link(self, rid):
        """The raw's carried `superseded_by` field (post-compaction), or None."""
        for r in ledger.iter_records():
            if isinstance(r, dict) and r.get(records.RECORD_ID_KEY) == rid:
                return r.get(records.SUPERSEDED_BY_KEY)
        return None

    def _on_file(self, rid):
        return sum(1 for r in ledger.iter_records()
                   if isinstance(r, dict) and r.get(records.RECORD_ID_KEY) == rid)

    def _whole(self):
        report = ledger.read()
        return os.path.exists(ledger.ledger_path()) and not report.torn_trailing

    def _recall(self, word):
        return index.query(word).records


class CrashSafeStoreTests(_Base):
    def test_a_crash_before_the_closing_marker_hides_no_raw(self):
        """W1–W3: gist + supersession markers written, the batch UN-closed → every supersession is inert, so no
        raw is filed away and the orphan gist is itself retired. Exactly one intact, recoverable state."""
        raws = self._raws(3)
        with self.assertRaises(rollup._InjectedCrash):
            self._rollup(raws, crash="markers")
        self.assertTrue(self._whole())
        live = self._live_ids()
        for rid in raws:
            self.assertIn(rid, live, "a raw must stay live while its roll-up batch is un-closed")
        self.assertNotIn(self._gist_id(), live, "a crashed-pass gist must be retired (orphan)")
        # nothing erased: every raw is still on file
        for rid in raws:
            self.assertEqual(self._on_file(rid), 1)

    def test_a_crash_after_the_closing_marker_files_the_raws_away(self):
        """W4: the closing marker landed → the ledger (the recall authority) already retires the raws and surfaces
        the gist; the fast index is merely stale until rebuilt (accepted, self-healing). Nothing lost."""
        raws = self._raws(3)
        with self.assertRaises(rollup._InjectedCrash):
            self._rollup(raws, crash="close")
        self.assertTrue(self._whole())
        live = self._live_ids()
        for rid in raws:
            self.assertNotIn(rid, live, "a raw must be filed away once its roll-up batch is closed")
            self.assertEqual(self._on_file(rid), 1, "but it stays resident + recoverable")
        self.assertIn(self._gist_id(), live, "the gist is live recall content")

    def test_a_complete_rollup_files_raws_away_keeps_the_gist_and_links_them(self):
        report = self._rollup(self._raws(3))
        self.assertEqual(report["status"], "ok")
        live = self._live_ids()
        gist = self._gist_id()
        self.assertIn(gist, live)
        # every source raw is retired from recall but still on file, and linked to the gist
        src = ledger.ledger_path()
        closed_rollup = forget._closed_rollup_batches(src)
        smap = forget._superseded_by_map(src, closed_rollup)
        self.assertEqual(len(smap), 3)
        for rid, gist_id in smap.items():
            self.assertNotIn(rid, live)
            self.assertEqual(gist_id, gist)


class SupersedeGateTests(_Base):
    def test_an_unclosed_supersession_does_not_hide_its_raw(self):
        raws = self._raws(3)
        with self.assertRaises(rollup._InjectedCrash):
            self._rollup(raws, crash="markers")
        # supersession markers exist, but the batch is un-closed → they are inert
        self.assertIn(records.SUPERSEDED_KIND, self._kinds())
        self.assertEqual(forget._superseded_by_map(ledger.ledger_path(),
                                                   forget._closed_rollup_batches(ledger.ledger_path())), {})
        for rid in raws:
            self.assertIn(rid, self._live_ids())

    def test_a_closed_supersession_hides_its_raw_even_when_it_would_score_hot(self):
        # a FRESH raw (born hot) that is superseded must still be hidden — supersession is orthogonal to the score
        raw = self._episodic("a fresh note word0", age_days=0)[records.RECORD_ID_KEY]
        self._rollup([raw])
        self.assertNotIn(raw, self._live_ids())

    def test_the_gist_is_visible_and_findable(self):
        self._rollup(self._raws(3), text="A findable rollup compendium note.")
        self.assertEqual(len(self._recall("compendium")), 1)


class FoldSupersedeTests(_Base):
    def test_a_closed_supersession_folds_to_a_carried_field_and_prunes_the_marker(self):
        raws = self._raws(3)
        self._rollup(raws)
        gist = self._gist_id()
        compact.compact()
        self.assertNotIn(records.SUPERSEDED_KIND, self._kinds(), "the folded marker is pruned")
        live = self._live_ids()
        for rid in raws:
            self.assertEqual(self._carried_link(rid), gist, "the link is carried on the raw")
            self.assertNotIn(rid, live, "the raw stays retired via the carried field")

    def test_an_unclosed_supersession_is_not_folded_and_the_raw_stays_live(self):
        """The key recall-loss guard: a crashed-pass (un-closed) supersession must NEVER be folded into a
        permanent carried field, or it would bake a hiding into the rewrite forever."""
        raws = self._raws(3)
        with self.assertRaises(rollup._InjectedCrash):
            self._rollup(raws, crash="markers")
        compact.compact()
        live = self._live_ids()
        for rid in raws:
            self.assertIsNone(self._carried_link(rid), "an un-closed supersession must NOT fold")
            self.assertIn(rid, live, "the raw must stay live")
        # the inert marker is kept verbatim (only what is folded is pruned)
        self.assertIn(records.SUPERSEDED_KIND, self._kinds())

    def test_re_running_compact_is_idempotent_on_a_superseded_raw(self):
        raws = self._raws(3)
        self._rollup(raws)
        gist = self._gist_id()
        compact.compact()
        compact.compact()
        live = self._live_ids()
        for rid in raws:
            self.assertEqual(self._carried_link(rid), gist)
            self.assertNotIn(rid, live)
        self.assertIn(gist, live)

    def test_the_link_is_recoverable_after_compaction(self):
        raws = self._raws(3)
        self._rollup(raws)
        gist = self._gist_id()
        compact.compact()
        # enumerate the gist's raws by the carried link (the perf-note lookup), recovering the whole roll-up
        recovered = [rid for rid in raws if self._carried_link(rid) == gist]
        self.assertEqual(sorted(recovered), sorted(raws))

    def test_a_reinforced_and_superseded_raw_keeps_both_carried_fields(self):
        """A raw that is BOTH reinforced (gets a frecency snapshot, 4d-i) AND superseded (gets superseded_by,
        4d-ii) must carry BOTH after one compaction — the two folds layer cleanly on independent dict copies."""
        raw = self._episodic("a note used then rolled up word0", age_days=10)[records.RECORD_ID_KEY]
        forget.record_access(raw)
        forget.record_access(raw)
        self._rollup([raw])
        compact.compact()
        rec = [r for r in ledger.iter_records() if r.get(records.RECORD_ID_KEY) == raw][0]
        self.assertIn(records.FRECENCY_SNAPSHOT_KEY, rec, "the reinforcement fold (snapshot) is present")
        self.assertEqual(rec.get(records.SUPERSEDED_BY_KEY), self._gist_id(), "the supersession fold is present")
        self.assertNotIn(raw, self._live_ids())

    def test_compaction_over_mixed_closed_and_unclosed_rollups(self):
        """The realistic production shape: one COMPLETED roll-up (folds + hides its raws) and one CRASHED,
        un-closed roll-up (its raws stay verbatim + live) in the same ledger, compacted together."""
        done_raws = self._raws(3, session_id="DONE", prefix="done note")
        self._rollup(done_raws, session_id="DONE")                          # completed
        crash_raws = self._raws(3, session_id="CRASH", prefix="crash note")
        with self.assertRaises(rollup._InjectedCrash):
            self._rollup(crash_raws, session_id="CRASH", crash="markers")   # crashed (un-closed)
        content = lambda: {r.get(records.RECORD_ID_KEY) for r in ledger.iter_records()
                           if isinstance(r, dict) and r.get("kind") in (records.EPISODIC_KIND, records.GIST_KIND)}
        before = content()
        compact.compact()
        self.assertEqual(before, content(), "no recall content dropped across the mixed compaction")
        live = self._live_ids()
        for rid in done_raws:
            self.assertEqual(self._carried_link(rid), self._gist_id("DONE"), "a closed raw is folded + linked")
            self.assertNotIn(rid, live, "a closed raw is filed away")
        for rid in crash_raws:
            self.assertIsNone(self._carried_link(rid), "an un-closed raw is NEVER folded")
            self.assertIn(rid, live, "an un-closed raw stays live")


class NeverDropsRecallContentTests(_Base):
    def test_the_gist_and_every_raw_survive_compaction(self):
        raws = self._raws(3)
        self._rollup(raws)
        gist = self._gist_id()
        before = {r.get(records.RECORD_ID_KEY) for r in ledger.iter_records()
                  if isinstance(r, dict) and r.get("kind") in (records.EPISODIC_KIND, records.GIST_KIND)}
        compact.compact()
        after = {r.get(records.RECORD_ID_KEY) for r in ledger.iter_records()
                 if isinstance(r, dict) and r.get("kind") in (records.EPISODIC_KIND, records.GIST_KIND)}
        self.assertEqual(before, after)
        self.assertIn(gist, after)
        for rid in raws:
            self.assertIn(rid, after)

    def test_a_superseded_and_archived_raw_is_excluded_for_both_reasons_and_survives(self):
        raw = self._episodic("an old set-aside note word0", age_days=40)[records.RECORD_ID_KEY]
        self._rollup([raw])
        now = int(time.time())
        # archived (demotion) AND superseded — two independent exclusion reasons
        rec = [r for r in ledger.iter_records() if r.get(records.RECORD_ID_KEY) == raw][0]
        self.assertEqual(score.tier(rec, (), now), score.ARCHIVED)
        self.assertNotIn(raw, self._live_ids())
        compact.compact()
        rec = [r for r in ledger.iter_records() if r.get(records.RECORD_ID_KEY) == raw][0]
        self.assertEqual(score.tier(rec, (), now), score.ARCHIVED, "demotion survives the fold")
        self.assertTrue(rec.get(records.SUPERSEDED_BY_KEY), "supersession survives the fold")
        self.assertNotIn(raw, self._live_ids())
        self.assertEqual(self._on_file(raw), 1, "and the raw is never erased")


class SearchBodyTests(_Base):
    def test_the_carried_link_fields_are_not_searchable(self):
        raws = self._raws(3)
        self._rollup(raws, text="A searchable gist compendium note.")
        gist = self._gist_id()
        compact.compact()                              # rebuilds the index over the folded records
        # the gist's real word IS findable
        self.assertEqual(len(self._recall("compendium")), 1)
        # but the uuid-hex link fields (a raw's carried superseded_by == the gist id; a gist's source_ids) are not
        self.assertEqual(self._recall(gist), [])
        for rid in raws:
            self.assertEqual(self._recall(rid), [])


class IdempotencyTests(_Base):
    def test_a_second_rollup_over_already_superseded_raws_is_a_no_op(self):
        raws = self._raws(3)
        self._rollup(raws)
        gists_before = sum(1 for k in self._kinds() if k == records.GIST_KIND)
        report = self._rollup(raws, text="A duplicate attempt that must not write.")
        self.assertEqual(report["status"], "already-rolled-up")
        self.assertEqual(report["stored"], 0)
        self.assertEqual(sum(1 for k in self._kinds() if k == records.GIST_KIND), gists_before)


class GistLifecycleTests(_Base):
    def test_a_live_gist_is_recall_visible_and_demotes_like_an_episodic(self):
        self._rollup(self._raws(3))
        gist = [r for r in ledger.iter_records()
                if isinstance(r, dict) and r.get("kind") == records.GIST_KIND][0]
        self.assertIn(gist[records.RECORD_ID_KEY], self._live_ids())
        now = int(time.time())
        self.assertEqual(score.tier(gist, (), now), score.HOT, "a fresh gist is born hot")
        aged = dict(gist, ts=now - 40 * _DAY)
        self.assertEqual(score.tier(aged, (), now), score.ARCHIVED, "an old, unused gist demotes like an episodic")

    def test_an_orphan_gist_from_a_crashed_rollup_is_retired(self):
        with self.assertRaises(rollup._InjectedCrash):
            self._rollup(self._raws(3), crash="markers")
        self.assertNotIn(self._gist_id(), self._live_ids())

    def test_a_rollup_and_a_consolidation_never_cross_close(self):
        """The two closure namespaces are disjoint: a crashed 3b consolidation's episodic stays retired AND a
        crashed roll-up's gist stays retired — neither marker closes the other's batch."""
        # a crashed 3b consolidation: an episodic carrying a batch with no `consolidated` marker
        orphan_ep = consolidate._make_episodic("C", {"role": "decision", "text": "a crashed consolidation note"}, "cb")
        ledger.append(orphan_ep)
        # a crashed roll-up: a gist + supersessions, no `rolled-up` marker
        with self.assertRaises(rollup._InjectedCrash):
            self._rollup(self._raws(3), crash="markers")
        live = self._live_ids()
        self.assertNotIn(orphan_ep[records.RECORD_ID_KEY], live, "the consolidation orphan stays retired")
        self.assertNotIn(self._gist_id(), live, "the roll-up orphan gist stays retired")


class LockTests(_Base):
    def test_store_gist_no_ops_under_lock_contention_and_never_writes(self):
        raw = self._episodic("a note to roll up word0", age_days=25)[records.RECORD_ID_KEY]
        fd = capture._acquire_lock(os.path.join(self._tmp.name, capture.LOCK_FILENAME))
        self.assertIsNotNone(fd)
        try:
            report = self._rollup([raw])
            self.assertEqual(report["status"], "busy")
            self.assertEqual(report["stored"], 0)
        finally:
            capture._release_lock(fd)
        self.assertEqual(sum(1 for k in self._kinds()
                             if k in (records.GIST_KIND, records.SUPERSEDED_KIND, records.ROLLUP_KIND)), 0)


class ProductionSafetyTests(_Base):
    def test_the_crash_injector_defaults_off(self):
        self.assertIsNone(inspect.signature(rollup.store_gist).parameters["_crash_after"].default)
        # a normal store completes (the injector never fires)
        self.assertEqual(self._rollup(self._raws(3))["status"], "ok")

    def test_rollup_module_is_append_only_no_erasure_tokens(self):
        src = inspect.getsource(rollup)
        for token in ("os.remove", "os.unlink", "os.truncate", ".truncate(", "rmtree", "os.replace"):
            self.assertNotIn(token, src, f"rollup.py must stay append-only (the erasure-free invariant); found {token!r}")

    def test_rejects_a_malformed_batch_whole_and_writes_nothing(self):
        raw = self._episodic("a note word0", age_days=25)[records.RECORD_ID_KEY]
        for bad in ([{"role": "nope", "text": "x", records.SOURCE_IDS_KEY: [raw]}],
                    [{"role": "lesson", "text": "", records.SOURCE_IDS_KEY: [raw]}],
                    [{"role": "lesson", "text": "x", records.SOURCE_IDS_KEY: []}]):
            report = rollup.store_gist("S", bad)
            self.assertEqual(report["status"], "rejected")
        self.assertEqual(sum(1 for k in self._kinds() if k == records.GIST_KIND), 0)


class DetectTests(_Base):
    def test_only_cold_grouped_non_superseded_episodics_are_candidates(self):
        cold = self._raws(3, age_days=25, session_id="S")     # 3 cold episodics in one session -> a candidate group
        self._episodic("a fresh note word9", age_days=0, session_id="S")  # too fresh -> not a candidate
        self._episodic("a lonely cold note word8", age_days=25, session_id="T")  # group of 1 -> below _MIN_GROUP
        groups = rollup.detect_rollup_candidates()
        self.assertIn("S", groups)
        self.assertEqual({r[records.RECORD_ID_KEY] for r in groups["S"]}, set(cold))
        self.assertNotIn("T", groups)

    def test_an_already_rolled_up_raw_is_not_re_detected(self):
        raws = self._raws(3, age_days=25)
        self._rollup(raws)
        self.assertNotIn("S", rollup.detect_rollup_candidates())

    def test_a_gist_is_never_a_candidate(self):
        self._rollup(self._raws(3, age_days=25))
        aged_gist = self._gist_id()
        # even after the gist itself would age into the cold window, it is never selected (raw episodics only)
        groups = rollup.detect_rollup_candidates(now=int(time.time()) + 25 * _DAY)
        for recs in groups.values():
            self.assertNotIn(aged_gist, {r[records.RECORD_ID_KEY] for r in recs})


class TagClusterDetectTests(_Base):
    # #235: cold episodics that share a TOPIC tag across sessions pre-group into a `tag:<tag>` cross-session
    # cluster, in precedence over the coarse per-session group. Fixtures plant tagged cold episodics directly.
    def test_a_shared_tag_across_sessions_forms_a_cross_session_cluster(self):
        ids = [self._episodic(f"auth note {i} word{i}", age_days=25, session_id=s, tags=["auth"])
               [records.RECORD_ID_KEY] for i, s in enumerate(("A", "B", "C"))]
        groups = rollup.detect_rollup_candidates()
        self.assertIn("tag:auth", groups)
        self.assertEqual({r[records.RECORD_ID_KEY] for r in groups["tag:auth"]}, set(ids))
        for s in ("A", "B", "C"):                       # no single session reaches _MIN_GROUP on its own
            self.assertNotIn(s, groups)

    def test_a_same_session_only_tag_is_not_a_cross_session_cluster(self):
        # three cold "auth" notes ALL in session S — a per-session group, never a tag cluster (fails _TAG_MIN_SESSIONS).
        for i in range(3):
            self._episodic(f"auth note {i} word{i}", age_days=25, session_id="S", tags=["auth"])
        groups = rollup.detect_rollup_candidates()
        self.assertNotIn("tag:auth", groups)
        self.assertIn("S", groups)
        self.assertEqual(len(groups["S"]), 3)

    def test_the_structural_episodic_tag_never_clusters(self):
        # cold notes across sessions carrying ONLY the structural "episodic" tag must not fuse into one cluster.
        for i, s in enumerate(("A", "B", "C")):
            self._episodic(f"note {i} word{i}", age_days=25, session_id=s)     # no topic tags
        groups = rollup.detect_rollup_candidates()
        self.assertFalse(any(k.startswith("tag:") for k in groups))

    def test_untagged_same_session_grouping_is_unchanged(self):
        # the legacy path: untagged cold notes still group by session exactly as before (guards the 175 legacy notes).
        ids = self._raws(3, age_days=25, session_id="S")
        groups = rollup.detect_rollup_candidates()
        self.assertIn("S", groups)
        self.assertEqual({r[records.RECORD_ID_KEY] for r in groups["S"]}, set(ids))
        self.assertFalse(any(k.startswith("tag:") for k in groups))

    def test_precedence_a_tag_cluster_claims_its_notes_from_the_session_group(self):
        # S has 3 tagged + 3 untagged cold notes; a note in B shares the tag, so tag:auth is cross-session and
        # claims S's three tagged notes. S's own group must then hold ONLY its untagged notes — disjoint source_ids.
        auth = [self._episodic(f"auth S {i} w{i}", age_days=25, session_id="S", tags=["auth"])
                [records.RECORD_ID_KEY] for i in range(3)]
        plain = [self._episodic(f"plain S {i} w{i}", age_days=25, session_id="S")[records.RECORD_ID_KEY]
                 for i in range(3)]
        self._episodic("auth B word", age_days=25, session_id="B", tags=["auth"])
        groups = rollup.detect_rollup_candidates()
        self.assertIn("tag:auth", groups)
        self.assertIn("S", groups)
        cluster_ids = {r[records.RECORD_ID_KEY] for r in groups["tag:auth"]}
        s_ids = {r[records.RECORD_ID_KEY] for r in groups["S"]}
        self.assertTrue(set(auth) <= cluster_ids)          # the tagged notes went to the cluster
        self.assertEqual(s_ids, set(plain))                # S keeps only its untagged notes
        self.assertEqual(cluster_ids & s_ids, set())       # …and the two groups are disjoint
        all_ids = [r[records.RECORD_ID_KEY] for recs in groups.values() for r in recs]
        self.assertEqual(len(all_ids), len(set(all_ids)))  # every group's source ids are disjoint across the pass

    def test_grouping_is_deterministic_derive_twice_and_compare(self):
        # the grouping must be a pure function of the ledger — same ledger, same {key: sorted(ids)} both times,
        # independent of dict/set iteration order (mirrors the repo's derive-twice flake idiom).
        for i, s in enumerate(("A", "B", "C")):
            self._episodic(f"auth {s} w{i}", age_days=25, session_id=s, tags=["auth", "billing"])
            self._episodic(f"plain {s} w{i}", age_days=25, session_id=s)
        first = {k: sorted(r[records.RECORD_ID_KEY] for r in v) for k, v in rollup.detect_rollup_candidates().items()}
        second = {k: sorted(r[records.RECORD_ID_KEY] for r in v) for k, v in rollup.detect_rollup_candidates().items()}
        self.assertEqual(first, second)

    def test_a_note_with_two_tags_is_claimed_by_the_lexicographically_smallest(self):
        # deterministic multi-tag tie-break: a note tagged both "auth" and "billing", each a valid cross-session
        # cluster, is claimed by "tag:auth" (smaller key), never both.
        shared = self._episodic("shared word0", age_days=25, session_id="A", tags=["auth", "billing"])[records.RECORD_ID_KEY]
        for i, s in enumerate(("B", "C")):
            self._episodic(f"auth {s} w{i}", age_days=25, session_id=s, tags=["auth"])
            self._episodic(f"billing {s} w{i}", age_days=25, session_id=s, tags=["billing"])
        groups = rollup.detect_rollup_candidates()
        in_auth = shared in {r[records.RECORD_ID_KEY] for r in groups.get("tag:auth", [])}
        in_billing = shared in {r[records.RECORD_ID_KEY] for r in groups.get("tag:billing", [])}
        self.assertTrue(in_auth)
        self.assertFalse(in_billing)


class TagGistProvenanceTests(_Base):
    # #235: rolling up a cross-session cluster stamps the gist's session_id with the sentinel key and supersedes
    # raws that came from DIFFERENT real sessions — the cross-session capability end to end.
    def test_a_cross_session_gist_carries_the_sentinel_and_supersedes_cross_session_raws(self):
        ids = [self._episodic(f"auth note {i} word{i}", age_days=25, session_id=s, tags=["auth"])
               [records.RECORD_ID_KEY] for i, s in enumerate(("A", "B", "C"))]
        self.assertIn("tag:auth", rollup.detect_rollup_candidates())
        report = self._rollup(ids, session_id="tag:auth")            # what the CLI hands store_gist for a cluster
        self.assertEqual(report["status"], "ok")
        gist_id = self._gist_id("tag:auth")
        self.assertIsNotNone(gist_id)                                # the gist carries the "tag:auth" sentinel
        live = self._live_ids()
        self.assertIn(gist_id, live)                                 # …is recall-visible
        for rid in ids:
            self.assertNotIn(rid, live)                              # …and every cross-session raw is retired


class DirectiveTests(_Base):
    # #280: the roll-up directive shares the SessionStart handler with consolidation and shipped the same
    # JSON-dumping read/store CLI, so it flooded the operator's chat identically. It must use the same
    # spawn-a-subagent + no-relay mechanism — but it has NO stalled-backlog alarm (the lower-priority path).
    def test_directive_spawns_a_subagent_and_forbids_relay(self):
        text = rollup.rollup_directive({"sess-a": [], "sess-b": []})
        low = text.lower()
        self.assertIn("spawn a subagent", low)                              # the mechanics move into a subagent
        self.assertIn("main transcript", low)                              # …off the operator's main transcript
        self.assertIn("relay nothing", low)                                # …and the result is not relayed back

    def test_directive_hands_the_subagent_a_complete_prompt(self):
        groups = {"alpha-sess": [], "beta-sess": []}
        text = rollup.rollup_directive(groups)
        for sid in groups:
            self.assertIn(sid, text)                                        # the exact ids travel with the prompt
        self.assertIn("rollup.py read <group>", text)                       # the read verb (group = session or tag cluster)
        self.assertIn("rollup.py store <group>", text)                      # the store verb
        self.assertIn("source_ids", text)                                  # roll-up's distinct contract is kept

    def test_directive_names_a_tag_cluster_as_cross_session_not_a_session(self):
        # #235: a `tag:` group key is a cross-session cluster, not a single session — the directive must say so,
        # and must use the group-generic read/store verb (the CLI passes the key through either way).
        text = rollup.rollup_directive({"tag:auth": [], "sess-b": []})
        self.assertIn("tag:auth", text)                                    # the cluster id travels with the prompt
        self.assertIn("cross-session", text.lower())                       # …explained as a cross-session cluster
        self.assertIn("rollup.py read <group>", text)                      # the group-generic verb
        plain = rollup.rollup_directive({"sess-a": [], "sess-b": []})       # a session-only backlog
        self.assertNotIn("cross-session", plain.lower())                   # …adds no cross-session note

    def test_directive_has_no_stalled_backlog_alarm(self):
        # Roll-up is the "can wait" path: it must never proactively surface a line to the operator.
        text = rollup.rollup_directive({f"s{i}": [] for i in range(20)}).lower()
        self.assertNotIn("tell the operator", text)
        self.assertNotIn("fallen behind", text)


if __name__ == "__main__":
    unittest.main()
