"""Unit tests for forget.py — Layer-1 logical retirement of crash-duplicate consolidations.

The retirement is REVERSIBLE and recall-only: an orphaned crash-pass episodic is excluded from recall but
stays resident in the ledger, fully recoverable. These tests exercise the real filter (`live_records`), the
real recall paths (fast index + slow scan) through it, the read-only `duplicates` inspector, and the
build-conformance invariant that this Layer-1 module reaches NO physical-erasure path.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import capture, compact, consolidate, forget, index, ledger, records, rollup, score  # noqa: E402


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

    def _episodic(self, session, text, batch, role="decision"):
        """Append an episodic carrying `batch` (a crashed pass leaves this with no closing marker)."""
        ledger.append(consolidate._make_episodic(session, {"role": role, "text": text}, batch))

    def _marker(self, session, batch):
        ledger.append(consolidate._make_marker(session, batch))

    def _episodics(self):
        return [r for r in ledger.iter_records() if r.get("kind") == records.EPISODIC_KIND]

    def _live_episodics(self):
        return [r for r in forget.live_records() if r.get("kind") == records.EPISODIC_KIND]


class LiveRecordsTests(_Base):
    def test_an_orphan_episodic_is_retired_from_recall(self):
        self._episodic("S", "orphan note", "batch-x")          # batch-x never gets a marker
        self.assertEqual(list(forget.live_records()), [])      # the orphan is not surfaced

    def test_a_marked_pass_is_kept(self):
        self._episodic("S", "good note", "batch-x")
        self._marker("S", "batch-x")
        kinds = [r.get("kind") for r in forget.live_records()]
        self.assertIn(records.EPISODIC_KIND, kinds)            # the completed pass's episodic is live
        self.assertIn(records.MARKER_KIND, kinds)              # markers always pass through

    def test_two_consolidated_markers_per_session_keep_both_passes_live(self):
        # #446: a re-swept session carries more than one `consolidated` marker (one per pass). Both passes' batches
        # are closed, so neither pass's episodic is orphaned — the recall-closure keying tolerates multiple markers.
        consolidate.store_episodic("S", [{"role": "decision", "text": "first pass summary"}])
        ledger.append(capture._make_record("S", 1, "user", "a later turn"))
        consolidate.store_episodic("S", [{"role": "lesson", "text": "second pass summary"}])
        self.assertEqual(len(self._records_of(records.MARKER_KIND)), 2)
        live = {r["text"] for r in self._live_episodics()}
        self.assertEqual(live, {"first pass summary", "second pass summary"})   # both stay live, neither orphaned

    def _records_of(self, kind):
        return [r for r in ledger.iter_records() if r.get("kind") == kind]

    def test_a_batchless_episodic_is_always_live(self):
        # a pre-batch episodic with no batch field — nothing to resolve, so never retired (back-compat)
        ledger.append({"v": 1, "kind": records.EPISODIC_KIND, "session_id": "S", "text": "old note", "tags": []})
        self.assertEqual(len(self._live_episodics()), 1)

    def test_an_empty_string_batch_is_treated_as_batchless_and_stays_live(self):
        # Defensive: the real write path mints a uuid, never "" — but a hand-edited / corrupt record with an
        # empty batch must be treated as batchless (always live), never mistaken for an unmarked orphan.
        ledger.append({"v": 1, "kind": records.EPISODIC_KIND, "session_id": "S", "text": "edge note",
                       "tags": [], records.BATCH_KEY: ""})
        self.assertEqual(len(self._live_episodics()), 1)

    def test_genuine_turns_are_recall_content_and_markers_pass_through(self):
        # The conversation IS the canonical record (eADR-0038), so a genuine turn-delta is recall content. This
        # DELIBERATELY inverts the earlier verdict, which excluded the whole kind because verbatim raw crowded
        # paraphrased summaries out of recall; the answer now is that the summaries are the disposable layer.
        # The structural `consolidated` marker still passes through (it carries no recall text, so it never
        # surfaces as a hit, but it is not bookkeeping the reader drops).
        ledger.append(capture._make_record("S", 0, "user", "a turn note"))   # turn-delta, no batch
        self._marker("S", "batch-x")                                          # a lone marker
        kinds = sorted({r.get("kind") for r in forget.live_records()})
        self.assertIn(capture.RECORD_KIND, kinds)       # the genuine turn IS recall content now
        self.assertIn(records.MARKER_KIND, kinds)

    def test_a_harness_injected_pseudo_turn_is_still_excluded(self):
        # The one thing that stays out: text the operator never said. Presenting a `/compact` continuation
        # summary or a task notification as their own words is a correctness bug, not a cosmetic one — the same
        # rule the consolidation sweep and the transcript-window reader already apply.
        genuine = capture._make_record("S", 0, "user", "a genuine turn note")
        injected = capture._make_record("S", 1, "user", "<task-notification> ignore me </task-notification>")
        injected.setdefault("tags", []).append(records.INJECTED_TAG)
        ledger.append(genuine)
        ledger.append(injected)
        texts = [r.get("text") for r in forget.live_records()]
        self.assertIn("a genuine turn note", texts)
        self.assertNotIn("<task-notification> ignore me </task-notification>", texts)

    def test_an_excluded_pseudo_turn_stays_in_the_raw_ledger_recoverable(self):
        # exclusion is recall-only — nothing is ever deleted by it (recall-exclusion, not erasure)
        injected = capture._make_record("S", 0, "user", "This session is being continued from a previous conversation")
        ledger.append(injected)
        self.assertEqual([r.get("kind") for r in ledger.iter_records()], [capture.RECORD_KIND])  # still resident
        self.assertEqual(list(forget.live_records()), [])                                         # just not surfaced

    def test_a_turn_is_reachable_on_both_recall_paths_and_the_sweep_still_sees_it(self):
        # Membership holds identically on the fast FTS5 path AND the degraded forced scan (the parity law),
        # while the consolidation sweep reads the raw ledger UNFILTERED, so the delta is still its input too.
        ledger.append(capture._make_record("S", 0, "user", "a quokka turn note"))
        self._episodic("S", "the quokka decision", "batch-x")
        self._marker("S", "batch-x")                                  # close the batch -> the episodic is live
        index.rebuild()
        for hits in (index.query("quokka").records, index.query("quokka", force_scan=True).records):
            kinds = {r.get("kind") for r in hits}
            self.assertIn(records.EPISODIC_KIND, kinds)               # the curated summary surfaces...
            self.assertIn(capture.RECORD_KIND, kinds)                 # ...and so does the conversation itself
        self.assertEqual([r.get("text") for r in consolidate.read_deltas("S")],
                         ["a quokka turn note"])                      # sweep input intact (orthogonality)

    def test_every_chunk_of_a_legacy_injected_message_is_excluded_not_just_the_first(self):
        # Capture splits a long message into several records sharing one `seq`. For a message captured BEFORE
        # injected-tagging existed, the only recogniser is a start-anchored text match — which by construction
        # matches the FIRST chunk alone. That was inert while the whole kind was excluded from recall; now the
        # tail chunks would be surfaced as ordinary conversation, and a `/compact` summary contains a section
        # headed "All user messages" — so recall could return a machine's paraphrase of what was asked for, as
        # if the operator had said it. Measured on the real store when this was found: 442 such chunks.
        head = capture._make_record("S", 4, "user", "This session is being continued from a previous conversation")
        tail = capture._make_record("S", 4, "user", "6. All user messages:\n   - deploy the thing")
        for r in (head, tail):
            r.pop("tags", None)                       # legacy shape: captured before tagging existed
        ledger.append(head)
        ledger.append(tail)
        texts = [r.get("text") for r in forget.live_records()]
        self.assertEqual(texts, [], "a later chunk of an injected message must travel with its head")

    def test_nothing_of_any_kind_is_ever_aged_out_of_recall(self):
        # The tier ratchet used to archive a never-reinforced record at 26.7 days (`dead-end`) to 32.9
        # (`decision`), and a captured turn could never earn its way out (nothing reinforces what nothing could
        # recall). Exempting only the turn would have left the summaries carrying the decisions aging out from
        # underneath the conversation, so the ratchet is gone for every kind — which is what this asserts, one
        # record per role plus a role-less turn, all far past every boundary the ratchet ever had. The sealed
        # benchmark CANNOT catch a regression here (its corpus is stamped relative to run time), so this must.
        ancient = int(time.time()) - 400 * 86400
        turn = capture._make_record("S", 0, "user", "an ancient quokka turn note")
        turn["ts"] = ancient
        ledger.append(turn)
        for role in sorted(consolidate.ROLE_VOCABULARY):
            rec = consolidate._make_episodic("S", {"role": role, "text": f"an ancient {role} note"}, "b")
            rec.pop(records.BATCH_KEY, None)              # batchless: always live, never a crash orphan
            rec["ts"] = ancient
            ledger.append(rec)
        texts = [r.get("text") for r in forget.live_records()]
        self.assertIn("an ancient quokka turn note", texts)
        for role in consolidate.ROLE_VOCABULARY:
            self.assertIn(f"an ancient {role} note", texts, f"a {role} record aged out of recall")

    def test_the_orphan_stays_in_the_raw_ledger_recoverable(self):
        self._episodic("S", "orphan note", "batch-x")          # retired from recall...
        self.assertEqual(len(self._episodics()), 1)            # ...but STILL in the ledger (recoverable)
        self.assertEqual(self._live_episodics(), [])

    def test_multiple_orphan_batches_all_retired_only_the_marked_one_surfaces(self):
        self._episodic("S", "crash one", "batch-a")            # crashed pass A (no marker)
        self._episodic("S", "crash two", "batch-b")            # crashed pass B (no marker)
        self._episodic("S", "the good one", "batch-c")         # completed pass C...
        self._marker("S", "batch-c")                           # ...with its marker
        self.assertEqual([r["text"] for r in self._live_episodics()], ["the good one"])

    def test_store_idempotency_prevents_a_second_complete_pass(self):
        # Two COMPLETE passes cannot both exist: once a session has a marker, store refuses
        # (already-consolidated), so the only duplicate forget ever sees is an unmarked orphan — never two
        # marked passes to choose between (so live_records needs no keep-latest tie-break).
        consolidate.store_episodic("S", [{"role": "decision", "text": "first"}])
        again = consolidate.store_episodic("S", [{"role": "decision", "text": "second"}])
        self.assertEqual(again["status"], "already-consolidated")
        self.assertEqual(len(self._live_episodics()), 1)


class RecallRetirementTests(_Base):
    def test_a_crash_duplicate_surfaces_once_in_recall(self):
        self._episodic("S", "the sourdough decision", "the-pass-that-crashed")   # orphan
        consolidate.store_episodic("S", [{"role": "decision", "text": "the sourdough decision retried"}])
        hits = [r for r in index.query("sourdough").records if r.get("kind") == records.EPISODIC_KIND]
        self.assertEqual(len(hits), 1)
        self.assertIn("retried", hits[0]["text"])              # the completed retry, not the orphan

    def test_fast_and_slow_recall_agree_after_retirement(self):
        self._episodic("S", "the quokka migration", "the-pass-that-crashed")
        consolidate.store_episodic("S", [{"role": "decision", "text": "the quokka migration retried"}])
        fast = sorted(r["text"] for r in index.query("quokka").records
                      if r.get("kind") == records.EPISODIC_KIND)
        slow = sorted(r["text"] for r in index.query("quokka", force_scan=True).records
                      if r.get("kind") == records.EPISODIC_KIND)
        self.assertEqual(fast, slow)                           # parity holds through the retirement filter
        self.assertEqual(len(fast), 1)

    def test_the_batch_uuid_is_not_a_search_term(self):
        consolidate.store_episodic("S", [{"role": "decision", "text": "a plain note"}])
        ep = next(r for r in ledger.iter_records() if r.get("kind") == records.EPISODIC_KIND)
        self.assertEqual(index.query(ep[records.BATCH_KEY]).records, [])   # the uuid is provenance, not content


class DuplicatesInspectorTests(_Base):
    def test_lists_retired_passes_by_session_not_the_kept_ones(self):
        self._episodic("S1", "crashed note one", "batch-a")
        self._episodic("S2", "crashed note two", "batch-b")
        self._episodic("S2", "kept note", "batch-c")
        self._marker("S2", "batch-c")
        groups = forget.duplicates()
        self.assertEqual(set(groups), {"S1", "S2"})
        self.assertEqual([r["text"] for r in groups["S1"]], ["crashed note one"])
        self.assertEqual([r["text"] for r in groups["S2"]], ["crashed note two"])  # the kept note is NOT listed

    def test_empty_when_nothing_is_retired(self):
        self._episodic("S", "good", "batch-x")
        self._marker("S", "batch-x")
        self.assertEqual(forget.duplicates(), {})


class BuildConformanceTests(unittest.TestCase):
    def test_forget_reaches_no_physical_erasure_path(self):
        # Layer-1 logical retirement NEVER erases (the two-layer law): physical removal is reachable
        # only through Layer 2's merge-gated path. forget.py must carry no ledger-delete / erase call — a
        # build-conformance invariant pinned by source scan.
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "forget.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        for token in ("os.remove", "os.unlink", "os.truncate", "truncate(", "rmtree", "os.replace"):
            self.assertNotIn(token, src, f"forget.py must not reach physical erasure: found {token!r}")

    def test_no_layer1_file_mints_an_erasure_marker(self):
        # Layer-2 physical erasure is reachable ONLY through compact's gated removal, which fires only on
        # an `operator-adjudicated-erasure` marker. Exactly TWO Layer-2 files may touch the minter call:
        # `compact.py` (the chokepoint OWNER — it DEFINES `enact_erasure` and performs the removal) and
        # `erasure_observer.py` (the SANCTIONED cross-session ENACTOR — it calls the minter, but ONLY with a
        # merge SHA read from a genuinely-merged single-purpose erasure PR, never from evidence or argv). Every OTHER
        # (Layer-1) memory file must NOT call the minter — a Layer-1 routine that minted a marker could route the
        # autonomous fold into erasure. The ban targets the minter CALL (`enact_erasure(`), NOT the kind constant
        # (forget legitimately references `records.ERASURE_KIND` to drop the marker from recall) and NOT `compact()`
        # (rollup / consolidate legitimately call it — calling compact is not minting). A glob-walk over the whole
        # memory package (not a fixed file list) so a Layer-1 tool added LATER is covered too. The package marker
        # `__init__.py` is NOT exempted — it is scanned like any other file (it must never mint either).
        sanctioned = ("compact.py", "erasure_observer.py")
        mem_dir = os.path.dirname(os.path.abspath(__file__))
        for name in sorted(os.listdir(mem_dir)):
            if not name.endswith(".py") or name.startswith("test_") or name in sanctioned:
                continue
            with open(os.path.join(mem_dir, name), encoding="utf-8") as fh:
                src = fh.read()
            self.assertNotIn("enact_erasure(", src,
                             f"{name} must not mint an erasure marker — the only sanctioned callers are "
                             f"compact.enact_erasure (the owner) and erasure_observer (the cross-session enactor)")


class EarnedConsolidatedRawTests(_Base):
    """forget.earned_consolidated_raw — the consolidated-session raw-capture erasure class (#274). A session's
    turn-deltas earn erasure once its consolidation is SETTLED (its `consolidated` marker is older than age_days) AND
    no curated stand-in (an episodic OR the roll-up gist that superseded them) is recalled; injected pseudo-turns and
    deltas captured after the marker are never yielded, and a marker with no curated stand-in never erases raw."""
    NOW = 2_000_000_000
    DAY = 86400

    def _settled(self):
        return self.NOW - 40 * self.DAY        # a consolidation older than the 30-day window -> gist is stable

    def _delta(self, session, text, ts, *, injected=False):
        rec = {"v": 1, "kind": records.AMBIENT_CAPTURE_KIND, records.RECORD_ID_KEY: records.new_record_id(),
               "session_id": session, "ts": ts, "text": text,
               "tags": [records.INJECTED_TAG] if injected else []}
        ledger.append(rec)
        return rec[records.RECORD_ID_KEY]

    def _episodic_at(self, session, batch, ts, role="decision"):
        rec = consolidate._make_episodic(session, {"role": role, "text": "a summary"}, batch)
        rec["ts"] = ts
        ledger.append(rec)
        return rec[records.RECORD_ID_KEY]

    def _marker_at(self, session, batch, ts):
        rec = consolidate._make_marker(session, batch)
        rec["ts"] = ts
        ledger.append(rec)

    def _gist_at(self, session, ts):
        rec = {"v": 1, "kind": records.GIST_KIND, records.RECORD_ID_KEY: records.new_record_id(),
               "session_id": session, "ts": ts, "text": "a gist", "tags": [records.GIST_TAG]}
        ledger.append(rec)
        return rec[records.RECORD_ID_KEY]

    def _cross_gist(self, sentinel, source_ids, ts):
        # a CROSS-SESSION gist (#235): its session_id is a `tag:`/`sim:` cluster sentinel, and its source_ids name
        # the raw episodes (in real sessions) it rolled up.
        rec = {"v": 1, "kind": records.GIST_KIND, records.RECORD_ID_KEY: records.new_record_id(),
               "session_id": sentinel, "ts": ts, "text": "a cluster gist", "tags": [records.GIST_TAG],
               records.SOURCE_IDS_KEY: list(source_ids)}
        ledger.append(rec)
        return rec[records.RECORD_ID_KEY]

    def _reinforce(self, target_id, ts):
        ledger.append({"v": 1, "kind": records.REINFORCEMENT_KIND, records.RECORD_ID_KEY: records.new_record_id(),
                       records.TARGET_KEY: target_id, "ts": ts, "tags": [records.REINFORCEMENT_TAG]})

    def _earned(self):
        return forget.earned_consolidated_raw(now=self.NOW, age_days=30)

    def test_earns_a_settled_sessions_non_injected_raw(self):
        self._episodic_at("A", "b1", self._settled())
        self._marker_at("A", "b1", self._settled())
        d1 = self._delta("A", "hello", self._settled() - self.DAY)
        d2 = self._delta("A", "world", self._settled() - self.DAY)
        self.assertEqual({r[records.RECORD_ID_KEY] for r in self._earned().get("A", [])}, {d1, d2})

    def test_keeps_a_young_consolidation(self):
        self._episodic_at("Y", "b1", self.NOW - 5 * self.DAY)
        self._marker_at("Y", "b1", self.NOW - 5 * self.DAY)        # consolidated 5 days ago -> gist not yet stable
        self._delta("Y", "hi", self.NOW - 6 * self.DAY)
        self.assertNotIn("Y", self._earned())

    def test_an_examined_but_unsummarized_tail_under_a_termination_marker_is_not_erasure_eligible(self):
        # #446 erasure boundary (the two-marker termination case): pass 1 summarizes turn 0 (a reflecting marker
        # with an episodic); a genuine tail is then added; a later "examined, nothing to summarize" EMPTY
        # termination marker (no episodic) advances the consolidation watermark past the tail. That empty marker's
        # newer ts must NOT pull the erasure clock forward over the tail — the tail has NO gist standing in for it,
        # and erasing it would destroy un-reflected content. The clock is bound by the latest REFLECTING marker.
        self._episodic_at("A", "b1", self.NOW - 45 * self.DAY)                 # the reflection (a gist stands in)
        self._marker_at("A", "b1", self.NOW - 45 * self.DAY)                   # reflecting marker (batch b1 has an episodic)
        covered = self._delta("A", "the summarized turn", self.NOW - 46 * self.DAY)   # captured before the reflecting marker
        tail = self._delta("A", "IMPORTANT un-summarized tail", self.NOW - 44 * self.DAY)  # captured AFTER it
        self._marker_at("A", "b2", self.NOW - 42 * self.DAY)                   # LATER empty termination marker: NO b2 episodic
        earned_ids = {r[records.RECORD_ID_KEY] for r in self._earned().get("A", [])}
        self.assertIn(covered, earned_ids)          # the reflected fuel earns erasure (its gist stands in)
        self.assertNotIn(tail, earned_ids)          # the un-summarized tail does NOT — no empty marker can erase it

    def test_a_recalled_episodic_keeps_the_whole_sessions_raw(self):
        e = self._episodic_at("R", "b1", self._settled())
        self._marker_at("R", "b1", self._settled())
        self._delta("R", "hi", self._settled() - self.DAY)
        self._reinforce(e, self.NOW - self.DAY)                    # the gist (an episodic) is in active use
        self.assertNotIn("R", self._earned())

    def test_a_recalled_rollup_gist_keeps_the_whole_sessions_raw(self):
        # Once episodics are rolled up they drop from recall, so recall lands on the GIST — an episodic-only
        # veto would be blind and erase raw whose live stand-in is in use. The veto must see the gist.
        self._episodic_at("G", "b1", self._settled())
        self._marker_at("G", "b1", self._settled())
        g = self._gist_at("G", self._settled())
        self._delta("G", "hi", self._settled() - self.DAY)
        self._reinforce(g, self.NOW - self.DAY)
        self.assertNotIn("G", self._earned())

    def test_a_recalled_cross_session_gist_keeps_every_source_sessions_raw(self):
        # #235: a cross-session gist carries a "tag:" sentinel, not a real session; its raws came from A and B.
        # Recall on that ONE gist must veto erasure of BOTH sessions' turn-deltas — else fuel alive in an
        # actively-recalled cluster gist would be erased. (The fix credits the gist via its source_ids.)
        eA = self._episodic_at("A", "b1", self._settled())
        self._marker_at("A", "b1", self._settled())
        self._delta("A", "hi from A", self._settled() - self.DAY)
        eB = self._episodic_at("B", "b2", self._settled())
        self._marker_at("B", "b2", self._settled())
        self._delta("B", "hi from B", self._settled() - self.DAY)
        g = self._cross_gist("tag:auth", [eA, eB], self._settled())
        self._reinforce(g, self.NOW - self.DAY)                    # the cluster gist is in active use
        earned = self._earned()
        self.assertNotIn("A", earned)
        self.assertNotIn("B", earned)

    def test_an_unrecalled_cross_session_gist_still_lets_settled_raw_be_earned(self):
        # …and the credit is recall-gated, not a blanket block: an UN-recalled cross-session gist must not stop a
        # settled session's raw from being earned (the fix must not over-veto).
        eA = self._episodic_at("A", "b1", self._settled())
        self._marker_at("A", "b1", self._settled())
        dA = self._delta("A", "hi from A", self._settled() - self.DAY)
        eB = self._episodic_at("B", "b2", self._settled())
        self._marker_at("B", "b2", self._settled())
        dB = self._delta("B", "hi from B", self._settled() - self.DAY)
        self._cross_gist("tag:auth", [eA, eB], self._settled())    # present but NOT recalled
        earned = self._earned()
        self.assertEqual({r[records.RECORD_ID_KEY] for r in earned.get("A", [])}, {dA})
        self.assertEqual({r[records.RECORD_ID_KEY] for r in earned.get("B", [])}, {dB})

    def test_injected_pseudo_turns_are_never_yielded(self):
        self._episodic_at("I", "b1", self._settled())
        self._marker_at("I", "b1", self._settled())
        keep = self._delta("I", "a real turn", self._settled() - self.DAY)
        self._delta("I", "<task-notification> scaffolding", self._settled() - self.DAY, injected=True)
        self.assertEqual({r[records.RECORD_ID_KEY] for r in self._earned().get("I", [])}, {keep})

    def test_a_delta_captured_after_the_marker_is_never_yielded(self):
        m_ts = self._settled()
        self._episodic_at("P", "b1", m_ts)
        self._marker_at("P", "b1", m_ts)
        before = self._delta("P", "consolidated fuel", m_ts - self.DAY)
        self._delta("P", "appended after the pass", m_ts + self.DAY)   # no gist stands in for it
        self.assertEqual({r[records.RECORD_ID_KEY] for r in self._earned().get("P", [])}, {before})

    def test_a_marker_with_no_curated_stand_in_never_erases_raw(self):
        self._marker_at("N", "b1", self._settled())               # a marker, but no episodic/gist for the session
        self._delta("N", "raw with no summary standing in", self._settled() - self.DAY)
        self.assertNotIn("N", self._earned())

    def test_disjoint_from_duplicates(self):
        # Session D has BOTH a completed pass (marker) and a crash-orphan episodic (no marker). The orphan episodic
        # goes through `duplicates`; D's turn-deltas through this class — different kinds, so no record in both.
        self._episodic_at("D", "done", self._settled())
        self._marker_at("D", "done", self._settled())
        d = self._delta("D", "raw", self._settled() - self.DAY)
        orphan = self._episodic_at("D", "crashed", self._settled())   # batch 'crashed' never got a marker
        raw_ids = {r[records.RECORD_ID_KEY] for recs in self._earned().values() for r in recs}
        dup_ids = {r[records.RECORD_ID_KEY] for recs in forget.duplicates().values() for r in recs}
        self.assertIn(d, raw_ids)
        self.assertIn(orphan, dup_ids)
        self.assertEqual(raw_ids & dup_ids, set())


_DAY = 86400


class SetAsideReportTests(_Base):
    """The set-aside report the boot readout relays: the ONE class recall drops that the operator has a handle
    on (summarised), never the crash-orphan class. There used to be a second, reversible class — a note the
    archived-tier ratchet aged out — and these tests pinned it; the ratchet is gone for every kind, so what they
    pin now is that age alone sets nothing aside."""

    def _aged(self, text, *, age_days=400, session="D"):
        """A never-reinforced episodic far older than any threshold the retired ratchet used. Batchless, so it
        is never a crash orphan either — it is simply an old note, and it must stay in recall."""
        rec = consolidate._make_episodic(session, {"role": "decision", "text": text}, "b")
        rec.pop(records.BATCH_KEY, None)
        rec["ts"] = int(time.time()) - age_days * _DAY
        ledger.append(rec)
        return rec[records.RECORD_ID_KEY]

    def _raws(self, n, *, age_days=25, session="S"):
        out = []
        for i in range(n):
            rec = consolidate._make_episodic(session, {"role": "decision", "text": f"raw note {i} word{i}"}, "b")
            rec.pop(records.BATCH_KEY, None)
            rec["ts"] = int(time.time()) - age_days * _DAY
            ledger.append(rec)
            out.append(rec[records.RECORD_ID_KEY])
        return out

    def _summarise(self, raw_ids, *, session="S"):
        """A COMPLETED roll-up: fold the raws into one gist so each raw is superseded out of recall."""
        rollup.store_gist(session, [{"role": "lesson", "text": "rolled-up summary of the older notes",
                                     records.SOURCE_IDS_KEY: list(raw_ids)}])

    def test_an_old_unused_note_is_not_set_aside_at_all(self):
        # The inversion of what this class used to assert. A note nobody has come back to in over a year is
        # still searchable, so it is not "set aside" and the readout must not name it — there is nothing to
        # offer the operator a handle on.
        rid = self._aged("an old decision nobody revisits")
        report = forget.set_aside()
        self.assertEqual(report["rows"], [])
        self.assertEqual(report["totals"], {"summarised": 0})
        self.assertIn(rid, {r.get(records.RECORD_ID_KEY) for r in forget.live_records()})

    def test_a_summarised_raw_is_set_aside_and_not_reversible(self):
        raws = self._raws(3)
        self._summarise(raws)
        report = forget.set_aside()
        rows = {r["id"]: r for r in report["rows"]}
        for rid in raws:
            self.assertIn(rid, rows)
            self.assertEqual(rows[rid]["reason"], forget.SET_ASIDE_SUMMARISED)
            self.assertFalse(rows[rid]["reversible"])   # a folded raw CANNOT be brought back — only shown
            self.assertTrue(rows[rid]["stands_in"])     # it names the summary that stands in for it
        self.assertEqual(report["totals"]["summarised"], 3)

    def test_crash_orphans_are_excluded_from_the_readout(self):
        # A consolidation orphan (unclosed batch) and a roll-up gist orphan are duplicates the good copy replaces,
        # not losses — deliberately NOT in the operator readout (an "undo" would re-admit a duplicate).
        ledger.append(consolidate._make_episodic("S", {"role": "decision", "text": "orphaned episodic"}, "batch-x"))
        rollup.store_gist("S", [{"role": "lesson", "text": "orphan gist",
                                 records.SOURCE_IDS_KEY: ["nope"]}], _crash_after="marker")
        report = forget.set_aside()
        texts = {r["text"] for r in report["rows"]}
        self.assertNotIn("orphaned episodic", texts)
        self.assertNotIn("orphan gist", texts)
        self.assertEqual(report["identity"], [])       # neither class is set-aside for the operator

    def test_markers_and_turn_deltas_never_appear(self):
        self._summarise(self._raws(1))                  # one real set-aside row to prove the report is non-empty
        ledger.append(capture._make_record("S", 0, "user", "a raw turn note"))   # ambient turn-delta
        forget.record_access("whatever")                # a reinforcement marker
        report = forget.set_aside()
        self.assertTrue(report["rows"])
        self.assertEqual({r["reason"] for r in report["rows"]}, {forget.SET_ASIDE_SUMMARISED})
        self.assertTrue(all(r["text"] not in ("a raw turn note",) for r in report["rows"]))

    def test_set_aside_and_live_records_partition_the_recall_eligible_population(self):
        # The honest invariant: every content record (episodic/gist) is EITHER surfaced by recall, OR named in the
        # set-aside report, OR a crash-orphan (excluded from both by design) — an EXACT, disjoint partition. The
        # orphan set is derived INDEPENDENTLY from live_records' own predicates (never as a residual), so a
        # set_aside miss — a record recall hides that the readout fails to classify — cannot hide in the leftover;
        # it breaks the partition. This is what makes the test a real net for a future live_records exclusion.
        # Compact partway so the check runs against the folded (matured-store) form, not just fresh markers.
        live = self._aged("a fresh live note", age_days=0, session="L")
        old_but_live = self._aged("an old note nobody revisits", session="L2")
        raws = self._raws(2, session="R")
        self._summarise(raws, session="R")
        ledger.append(consolidate._make_episodic("O", {"role": "decision", "text": "orphan"}, "orphan-batch"))
        compact.compact()                                        # fold supersessions + prune markers

        src = ledger.ledger_path()
        closed = forget._closed_batches(src)
        closed_rollup = forget._closed_rollup_batches(src)
        content = [r for r in ledger.iter_records()
                   if isinstance(r, dict) and r.get("kind") in (records.EPISODIC_KIND, records.GIST_KIND)]
        all_content = {r.get(records.RECORD_ID_KEY) for r in content}
        live_ids = {r.get(records.RECORD_ID_KEY) for r in forget.live_records()
                    if r.get("kind") in (records.EPISODIC_KIND, records.GIST_KIND)}
        aside_ids = set(forget.set_aside()["identity"])
        # the crash-orphan set, derived from the SAME predicates live_records excludes by — not a residual
        orphan_ids = {r.get(records.RECORD_ID_KEY) for r in content
                      if forget._is_retired(r, closed) or forget._is_gist_orphan(r, closed_rollup)}

        self.assertEqual(live_ids & aside_ids, set())            # disjoint: nothing is both live and set aside
        self.assertEqual(aside_ids & orphan_ids, set())          # a crash orphan is never offered in the readout
        self.assertEqual(live_ids & orphan_ids, set())           # an orphan is not live either
        self.assertEqual(live_ids | aside_ids | orphan_ids, all_content)   # exact, complete partition
        self.assertIn(live, live_ids)
        self.assertIn(old_but_live, live_ids)                    # age alone never moves a note to the aside set
        self.assertLessEqual(set(raws), aside_ids)               # summarised raws set aside even after compaction
        self.assertTrue(orphan_ids)                              # the orphan is in the excluded set, not lost

    def test_summarised_survives_compaction_which_prunes_the_marker(self):
        # The matured-store case: compaction folds each supersession into the raw's carried `superseded_by`
        # field and PRUNES the `superseded` marker. The readout must still classify the raw as summarised (from
        # the carried field, exactly as live_records does) — never go silent, and never fall through to a false
        # "demoted + reversible". Regression guard for the marker-only-classification defect.
        raws = self._raws(2)
        self._summarise(raws)
        compact.compact()                                        # folds + prunes the markers
        report = forget.set_aside()
        rows = {r["id"]: r for r in report["rows"]}
        for rid in raws:
            self.assertIn(rid, rows, "a summarised raw vanished from the readout after compaction")
            self.assertEqual(rows[rid]["reason"], forget.SET_ASIDE_SUMMARISED)
            self.assertFalse(rows[rid]["reversible"])            # never a false bring-back offer post-compaction
        self.assertEqual(report["totals"], {"summarised": 2})

    def test_no_row_is_ever_offered_as_reversible(self):
        # The honesty contract, and it is now absolute: the only thing that sets a note aside is a summary
        # written over it, and there is no un-fold. Nothing in the readout may claim otherwise — which is why
        # the restore that once backed `reversible=True` was removed with the class it reversed rather than
        # left in place returning False.
        self._aged("an old note nobody revisits")
        raws = self._raws(2, age_days=40)
        self._summarise(raws)
        ledger.append(consolidate._make_episodic("O", {"role": "decision", "text": "aged orphan"}, "orphan-b"))
        compact.compact()
        rows = forget.set_aside()["rows"]
        self.assertTrue(rows)
        self.assertEqual([r["reversible"] for r in rows], [False] * len(rows))
        self.assertFalse(hasattr(forget, "restore_to_recall"))

    def test_totals_count_the_full_population_while_rows_respect_the_limit(self):
        self._summarise(self._raws(5))
        report = forget.set_aside(limit=2)
        self.assertEqual(len(report["rows"]), 2)                 # the sample is bounded
        self.assertEqual(report["totals"]["summarised"], 5)      # the total is the whole population
        self.assertEqual(len(report["identity"]), 5)             # identity is the whole population, not the sample

    def test_ordering_survives_a_damaged_timestamp(self):
        # The summarised class is set aside for a reason independent of its ts (a completed supersession), so a
        # raw carrying a damaged ts still belongs in the report — and the sort key must tolerate it, sorting it
        # last rather than raising mid-sort (the index.recent_decisions total-key guarantee). Hand-build a closed
        # supersession over a raw whose ts is a string, alongside a well-formed one.
        good = consolidate._make_episodic("S", {"role": "decision", "text": "well-formed folded raw word1"}, "b")
        good.pop(records.BATCH_KEY, None)
        good["ts"] = int(time.time()) - 25 * _DAY
        bad = consolidate._make_episodic("S", {"role": "decision", "text": "folded raw with a broken ts word2"}, "b")
        bad.pop(records.BATCH_KEY, None)
        bad["ts"] = "not-a-number"
        gist = rollup._make_gist("S", {"role": "lesson", "text": "the summary",
                                       records.SOURCE_IDS_KEY: [good[records.RECORD_ID_KEY],
                                                                bad[records.RECORD_ID_KEY]]}, "rb")
        for rec in (good, bad, gist):
            ledger.append(rec)
        gid = gist[records.RECORD_ID_KEY]
        ledger.append(rollup._make_superseded_marker(good[records.RECORD_ID_KEY], gid, "rb"))
        ledger.append(rollup._make_superseded_marker(bad[records.RECORD_ID_KEY], gid, "rb"))
        ledger.append(rollup._make_rollup_marker("S", "rb"))     # closes batch rb -> both supersessions live
        report = forget.set_aside()                              # no exception despite the string ts
        ids = [r["id"] for r in report["rows"]]
        self.assertIn(good[records.RECORD_ID_KEY], ids)
        self.assertIn(bad[records.RECORD_ID_KEY], ids)


class SetAsideHandleTests(_Base):
    def test_a_folded_raw_stays_out_of_recall_and_its_wording_stays_readable(self):
        # The only handle a set-aside note has, now that the reversible class is gone: the summary stands in for
        # it in search, and its original wording is still there to be read word-for-word.
        rec = consolidate._make_episodic("S", {"role": "decision", "text": "raw folded away word1"}, "b")
        rec.pop(records.BATCH_KEY, None)
        rec["ts"] = int(time.time()) - 25 * _DAY
        ledger.append(rec)
        raw_id = rec[records.RECORD_ID_KEY]
        rollup.store_gist("S", [{"role": "lesson", "text": "the summary",
                                 records.SOURCE_IDS_KEY: [raw_id]}])
        self.assertNotIn(raw_id, {r.get(records.RECORD_ID_KEY) for r in forget.live_records()})
        self.assertEqual(forget.recorded_text(raw_id)["text"], "raw folded away word1")

    def test_recorded_text_returns_the_exact_wording_and_does_not_reinforce(self):
        rec = consolidate._make_episodic("S", {"role": "decision", "text": "the exact original wording"}, "b")
        rec.pop(records.BATCH_KEY, None)
        ledger.append(rec)
        rid = rec[records.RECORD_ID_KEY]
        before = sum(1 for r in ledger.iter_records() if r.get("kind") == records.REINFORCEMENT_KIND)
        got = forget.recorded_text(rid)
        self.assertEqual(got["text"], "the exact original wording")
        after = sum(1 for r in ledger.iter_records() if r.get("kind") == records.REINFORCEMENT_KIND)
        self.assertEqual(before, after)                          # merely looking never re-ranks recall
        self.assertIsNone(forget.recorded_text("no-such-id"))
        self.assertIsNone(forget.recorded_text(""))


if __name__ == "__main__":
    unittest.main()
