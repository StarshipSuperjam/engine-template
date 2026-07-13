"""Unit tests for consolidate.py — the reflection half (slice 3b).

The consolidation NARRATIVE needs a live AI; everything around it is deterministic. So these tests SIMULATE
the AI by passing a hand-written summary `{role, text}` to `store_episodic`, then exercise the real read /
store / detect / hook-handler machinery and read the ledger + the derived index back. The one un-runnable step
(the AI composing a *good* summary) is the only thing not covered here — by construction, not by omission.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hooks  # noqa: E402
from memory import capture, consolidate, index, ledger  # noqa: E402


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

    def _delta(self, session, seq, speaker, text):
        ledger.append(capture._make_record(session, seq, speaker, text))

    def _records(self, kind=None):
        return [r for r in ledger.iter_records() if kind is None or r.get("kind") == kind]

    def _run_hook(self, event, handler, payload):
        out, err = io.StringIO(), io.StringIO()
        code = hooks.run_hook(event, handler, stdin=io.StringIO(json.dumps(payload)), stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()


class ReadDeltasTests(_Base):
    def test_returns_only_that_session_in_seq_order(self):
        # interleave two sessions, file the target's notes out of order
        self._delta("A", 2, "user", "third A note")
        self._delta("B", 0, "user", "a B note")
        self._delta("A", 0, "user", "first A note")
        self._delta("A", 1, "assistant", "second A note")
        got = consolidate.read_deltas("A")
        self.assertEqual([r["seq"] for r in got], [0, 1, 2])
        self.assertTrue(all(r["session_id"] == "A" for r in got))
        self.assertNotIn("a B note", [r["text"] for r in got])

    def test_empty_for_an_unknown_session(self):
        self._delta("A", 0, "user", "note")
        self.assertEqual(consolidate.read_deltas("nope"), [])

    def test_skips_injected_pseudo_turns_but_keeps_real_ones(self):
        # issue #274: a tagged-injected record (the durable capture path) AND a back-compat text-prefix record
        # (captured before tagging existed) are both skipped as fuel; genuine turns are kept, in seq order.
        self._delta("A", 0, "user", "first real note")
        ledger.append(capture._make_record("A", 1, "user", "<task-notification>\n<id>x</id>", injected=True))
        ledger.append(capture._make_record(
            "A", 2, "user", "This session is being continued from a previous conversation."))  # no tag: back-compat
        self._delta("A", 3, "user", "second real note")
        self.assertEqual([r["text"] for r in consolidate.read_deltas("A")],
                         ["first real note", "second real note"])

    def test_keeps_a_real_turn_that_only_mentions_a_marker(self):
        self._delta("A", 0, "user", "what does <task-notification> mean in my transcript?")
        self.assertEqual([r["text"] for r in consolidate.read_deltas("A")],
                         ["what does <task-notification> mean in my transcript?"])


class StoreTests(_Base):
    def test_writes_a_typed_episodic_record_and_a_marker(self):
        report = consolidate.store_episodic("S", [{"role": "decision", "text": "chose the blue plan"}])
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["stored"], 1)
        episodic = self._records(consolidate.EPISODIC_KIND)
        markers = self._records(consolidate.MARKER_KIND)
        self.assertEqual(len(episodic), 1)
        self.assertEqual(len(markers), 1)
        rec = episodic[0]
        self.assertEqual(rec["kind"], "episodic")
        self.assertEqual(rec["role"], "decision")
        self.assertEqual(rec["text"], "chose the blue plan")
        self.assertEqual(rec["session_id"], "S")
        self.assertEqual(rec["v"], capture.RECORD_VERSION)
        self.assertIn("episodic", rec["tags"])
        self.assertIsInstance(rec["ts"], int)
        self.assertIsInstance(rec["consolidated_ts"], int)

    def test_open_tags_are_merged_and_episodic_is_always_present(self):
        consolidate.store_episodic("S", [{"role": "lesson", "text": "t", "tags": ["auth", "episodic"]}])
        rec = self._records(consolidate.EPISODIC_KIND)[0]
        self.assertEqual(rec["tags"][0], "episodic")
        self.assertIn("auth", rec["tags"])
        self.assertEqual(rec["tags"].count("episodic"), 1)

    def test_topic_and_entity_tags_are_persisted(self):
        # #235: the AI now assigns topic/entity tags to each summary — both a plain topic and a verbatim entity
        # ref (canonical case preserved) must land, so a later pass can cluster on them across sessions.
        consolidate.store_episodic("S", [{"role": "decision", "text": "t", "tags": ["rollup", "eADR-0031"]}])
        rec = self._records(consolidate.EPISODIC_KIND)[0]
        self.assertEqual(rec["tags"][0], "episodic")
        self.assertIn("rollup", rec["tags"])
        self.assertIn("eADR-0031", rec["tags"])   # entity ref kept verbatim, not lowercased

    def test_tags_are_stripped_so_padded_variants_collapse(self):
        # #235: tags exist to be matched across sessions by exact string, so store them stripped — " rollup "
        # and "rollup" must land as one topic, not two that would fail to cluster.
        consolidate.store_episodic("S", [{"role": "decision", "text": "t", "tags": [" rollup ", "rollup"]}])
        rec = self._records(consolidate.EPISODIC_KIND)[0]
        self.assertIn("rollup", rec["tags"])
        self.assertNotIn(" rollup ", rec["tags"])
        self.assertEqual(rec["tags"].count("rollup"), 1)

    def test_absent_or_empty_tags_is_accepted(self):
        # Untagged is legal — a thread with no clear topic stays untagged rather than forcing an invented tag.
        self.assertEqual(consolidate.store_episodic("S1", [{"role": "intent", "text": "t"}])["status"], "ok")
        self.assertEqual(
            consolidate.store_episodic("S2", [{"role": "intent", "text": "t", "tags": []}])["status"], "ok")

    def test_source_seqs_kept_only_when_given(self):
        consolidate.store_episodic("S", [{"role": "intent", "text": "t", "source_seqs": [0, 2, "5"]}])
        rec = self._records(consolidate.EPISODIC_KIND)[0]
        self.assertEqual(rec["source_seqs"], [0, 2, 5])
        consolidate.store_episodic("S2", [{"role": "intent", "text": "t2"}])
        rec2 = [r for r in self._records(consolidate.EPISODIC_KIND) if r["session_id"] == "S2"][0]
        self.assertNotIn("source_seqs", rec2)

    def test_all_roles_in_the_closed_vocabulary_are_accepted(self):
        for i, role in enumerate(consolidate.ROLE_VOCABULARY):
            report = consolidate.store_episodic(f"sess-{i}", [{"role": role, "text": f"note {i}"}])
            self.assertEqual(report["status"], "ok", role)

    def test_a_multi_summary_batch_writes_all_and_exactly_one_marker(self):
        recs = [{"role": "decision", "text": "a"}, {"role": "lesson", "text": "b"},
                {"role": "intent", "text": "c"}]
        report = consolidate.store_episodic("S", recs)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["stored"], 3)
        self.assertEqual(len(self._records(consolidate.EPISODIC_KIND)), 3)
        self.assertEqual(len(self._records(consolidate.MARKER_KIND)), 1)   # one marker for the whole batch

    def test_one_batch_id_links_every_episodic_to_the_passs_marker(self):
        # The id is minted ONCE per pass (before the append loop) and stamped on every episodic AND the marker,
        # so a completed pass is identifiable as one batch — the linkage forget.py retires an orphan by.
        consolidate.store_episodic("S", [{"role": "decision", "text": "a"}, {"role": "lesson", "text": "b"}])
        eps = self._records(consolidate.EPISODIC_KIND)
        marker = self._records(consolidate.MARKER_KIND)[0]
        batches = {r["batch"] for r in eps}
        self.assertEqual(len(batches), 1)                    # one id across the whole pass's episodics
        self.assertEqual(marker["batch"], eps[0]["batch"])   # and the marker carries that same id


class RejectionTests(_Base):
    def test_out_of_vocab_role_is_rejected_and_writes_nothing(self):
        report = consolidate.store_episodic("S", [{"role": "banana", "text": "t"}])
        self.assertEqual(report["status"], "rejected")
        self.assertEqual(report["stored"], 0)
        self.assertEqual(self._records(), [])  # nothing at all written

    def test_one_bad_role_rejects_the_whole_batch(self):
        report = consolidate.store_episodic("S", [
            {"role": "decision", "text": "good one"},
            {"role": "nope", "text": "bad one"},
        ])
        self.assertEqual(report["status"], "rejected")
        self.assertEqual(self._records(consolidate.EPISODIC_KIND), [])  # the good one is NOT half-written
        self.assertEqual(self._records(consolidate.MARKER_KIND), [])

    def test_empty_text_is_rejected(self):
        report = consolidate.store_episodic("S", [{"role": "decision", "text": "   "}])
        self.assertEqual(report["status"], "rejected")

    def test_empty_batch_with_nothing_new_is_a_clean_no_op(self):
        # #446: an empty batch is no longer a rejection — it is the "examined, nothing to summarize" signal. On a
        # session with nothing new to sweep it is a clean no-op that writes NOTHING (no episodic, no marker), so it
        # never mints a junk marker. (The advancing case — an empty batch over a genuine tail — is covered in the
        # IncrementalConsolidationTests termination test.)
        out = consolidate.store_episodic("S", [])
        self.assertEqual(out["status"], "already-consolidated")
        self.assertEqual(out["stored"], 0)
        self.assertEqual(self._records(), [])

    def test_missing_session_id_is_rejected(self):
        self.assertEqual(consolidate.store_episodic("", [{"role": "lesson", "text": "t"}])["status"], "rejected")

    def test_a_non_list_tags_field_rejects_the_whole_batch(self):
        # #235: reject-not-coerce — a bare-string tags field is malformed, so nothing is written (not silently
        # coerced into a one-tag list).
        report = consolidate.store_episodic("S", [{"role": "decision", "text": "t", "tags": "auth"}])
        self.assertEqual(report["status"], "rejected")
        self.assertEqual(self._records(), [])

    def test_an_empty_or_whitespace_tag_rejects_the_whole_batch(self):
        for bad in ("", "   "):
            report = consolidate.store_episodic("S", [{"role": "decision", "text": "t", "tags": ["ok", bad]}])
            self.assertEqual(report["status"], "rejected", repr(bad))
            self.assertEqual(self._records(), [])

    def test_a_non_string_tag_rejects_the_whole_batch(self):
        report = consolidate.store_episodic("S", [{"role": "decision", "text": "t", "tags": ["ok", 7]}])
        self.assertEqual(report["status"], "rejected")
        self.assertEqual(self._records(), [])

    def test_too_many_tags_are_rejected(self):
        over = [f"t{i}" for i in range(consolidate._MAX_TAGS + 1)]
        report = consolidate.store_episodic("S", [{"role": "decision", "text": "t", "tags": over}])
        self.assertEqual(report["status"], "rejected")
        self.assertEqual(self._records(), [])


class SearchTests(_Base):
    def test_summary_is_findable_by_its_narrative_words(self):
        consolidate.store_episodic("S", [{"role": "decision",
                                          "text": "the migration runs on a quokka cluster"}])
        hits = [r for r in index.query("quokka").records if r.get("kind") == consolidate.EPISODIC_KIND]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["role"], "decision")

    def test_the_label_is_not_a_search_term(self):
        # role "decision", but the TEXT never says "decision" → searching the label finds nothing.
        consolidate.store_episodic("S1", [{"role": "decision", "text": "chose the quokka cluster"}])
        consolidate.store_episodic("S2", [{"role": "decision", "text": "kept the onboarding copy short"}])
        self.assertEqual(index.query("decision").records, [])           # the label never pollutes search
        self.assertEqual(len(index.query("quokka").records), 1)         # but content words still find it

    def test_provenance_fields_stay_out_of_the_search_body(self):
        consolidate.store_episodic("unique-session-xyz", [{"role": "observation", "text": "a plain note",
                                                           "tags": ["zztag"]}])
        # neither the session id, the kind, the role, nor a tag should match by themselves
        for term in ("unique", "episodic", "observation", "zztag", "consolidated"):
            self.assertEqual(index.query(term).records, [], term)


class MarkerIdempotencyTests(_Base):
    def test_second_store_is_a_noop(self):
        first = consolidate.store_episodic("S", [{"role": "decision", "text": "first"}])
        self.assertEqual(first["status"], "ok")
        second = consolidate.store_episodic("S", [{"role": "decision", "text": "second"}])
        self.assertEqual(second["status"], "already-consolidated")
        self.assertEqual(second["stored"], 0)
        self.assertEqual(len(self._records(consolidate.EPISODIC_KIND)), 1)  # the second did NOT write
        self.assertEqual(len(self._records(consolidate.MARKER_KIND)), 1)

    def test_marker_removes_the_session_from_detect(self):
        self._delta("S", 0, "user", "a note")
        self.assertIn("S", consolidate.detect_unconsolidated())
        consolidate.store_episodic("S", [{"role": "lesson", "text": "t"}])
        self.assertNotIn("S", consolidate.detect_unconsolidated())


class DetectTests(_Base):
    def test_lists_sessions_with_deltas_and_no_marker(self):
        self._delta("A", 0, "user", "a")
        self._delta("B", 0, "user", "b")
        self.assertEqual(consolidate.detect_unconsolidated(), ["A", "B"])

    def test_excludes_the_live_session(self):
        self._delta("A", 0, "user", "a")
        self._delta("live", 0, "user", "still writing")
        self.assertEqual(consolidate.detect_unconsolidated(live_session_id="live"), ["A"])

    def test_excludes_a_consolidated_session(self):
        self._delta("A", 0, "user", "a")
        self._delta("B", 0, "user", "b")
        consolidate.store_episodic("A", [{"role": "decision", "text": "t"}])
        self.assertEqual(consolidate.detect_unconsolidated(), ["B"])

    def test_a_session_with_only_a_marker_is_not_pending(self):
        consolidate.store_episodic("A", [{"role": "decision", "text": "t"}])  # marker, no deltas
        self.assertEqual(consolidate.detect_unconsolidated(), [])

    def test_ignores_an_empty_session_id(self):
        ledger.append(capture._make_record("", 0, "user", "a weird empty-id record"))
        self.assertEqual(consolidate.detect_unconsolidated(), [])           # empty id never surfaces as pending

    def test_a_session_of_only_injected_pseudo_turns_is_not_pending(self):
        # else read_deltas yields nothing to store -> no marker is ever written -> the session is re-detected as
        # pending every SessionStart (a permanent sweep loop). Detection must agree with the read (issue #274).
        ledger.append(capture._make_record("J", 0, "user", "<task-notification>\n<id>x</id>", injected=True))
        ledger.append(capture._make_record(
            "J", 1, "user", "This session is being continued from a previous conversation."))
        self.assertEqual(consolidate.detect_unconsolidated(), [])

    def test_a_session_with_one_real_delta_among_injected_is_still_pending(self):
        ledger.append(capture._make_record("M", 0, "user", "<task-notification>\n<id>x</id>", injected=True))
        self._delta("M", 1, "user", "a genuine note worth tidying")
        self.assertIn("M", consolidate.detect_unconsolidated())


class IncrementalConsolidationTests(_Base):
    """#446: the marker is a per-session high-water-mark, so a session tidied mid-run is re-swept for its later
    half, the sweep terminates on an unsummarizable tail, and a pre-#446 ledger is not re-tidied wholesale."""

    def _delta_at(self, session, seq, ts, text="a genuine note"):
        rec = capture._make_record(session, seq, "user", text)
        rec["ts"] = ts
        ledger.append(rec)

    def _legacy_marker(self, session, ts, batch="legacy-batch"):
        marker = consolidate._make_marker(session, batch)      # no through_seq => a pre-#446 (legacy) marker
        self.assertNotIn(consolidate.THROUGH_SEQ_KEY, marker)  # guard: the projection path is what we exercise
        marker["ts"] = ts
        ledger.append(marker)

    def test_a_never_consolidated_single_seq0_turn_is_detected(self):
        # The sentinel must sit BELOW seq 0 (0-based), or a session whose only genuine turn is at seq 0 would
        # read `0 > 0 == False` and never be tidied — reflection lost forever, not deferred.
        self._delta("S", 0, "user", "the one and only turn")
        self.assertIn("S", consolidate.detect_unconsolidated())

    def test_a_tidied_session_is_re_swept_for_its_later_half_only(self):
        self._delta("S", 0, "user", "first half")
        consolidate.store_episodic("S", [{"role": "decision", "text": "summary of the first half"}])
        self.assertNotIn("S", consolidate.detect_unconsolidated())        # nothing new yet
        self._delta("S", 1, "user", "second half, added after the tidy")
        self.assertIn("S", consolidate.detect_unconsolidated())           # the later half re-flags it
        # the `read` verb scopes to the tail: only the later half is re-read
        _g, wm, _h = consolidate._session_states().get("S")
        tail = [d["text"] for d in consolidate.read_deltas("S", after_seq=wm)]
        self.assertEqual(tail, ["second half, added after the tidy"])
        consolidate.store_episodic("S", [{"role": "lesson", "text": "summary of the second half"}])
        self.assertNotIn("S", consolidate.detect_unconsolidated())        # settled again
        markers = self._records(consolidate.MARKER_KIND)
        self.assertEqual(len(markers), 2)                                 # two passes, two markers
        self.assertEqual(max(m[consolidate.THROUGH_SEQ_KEY] for m in markers), 1)  # effective watermark advanced

    def test_read_default_stays_a_full_history_read(self):
        # read_deltas with no after_seq is unchanged (the sweep-orthogonality contract): a consolidated delta is
        # still returned, so a non-sweep caller reading the whole session is not silently truncated.
        self._delta("S", 0, "user", "the note")
        consolidate.store_episodic("S", [{"role": "decision", "text": "t"}])
        self.assertEqual([d["text"] for d in consolidate.read_deltas("S")], ["the note"])

    def test_an_unsummarizable_tail_terminates_via_an_empty_store(self):
        # The termination guarantee (README §86-89): a genuine-but-unsummarizable tail advances the watermark
        # even though it yields no record, so it does not re-fire every session.
        self._delta("S", 0, "user", "worth summarizing")
        consolidate.store_episodic("S", [{"role": "decision", "text": "the summary"}])
        self._delta("S", 1, "user", "thanks, that's all for today")      # genuine, but nothing to summarize
        self.assertIn("S", consolidate.detect_unconsolidated())
        out = consolidate.store_episodic("S", [])                        # "examined, nothing to summarize"
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["stored"], 0)
        self.assertEqual(len(self._records(consolidate.EPISODIC_KIND)), 1)  # no new episodic written
        self.assertNotIn("S", consolidate.detect_unconsolidated())       # TERMINATES — not re-flagged
        self.assertEqual(consolidate.store_episodic("S", [])["status"], "already-consolidated")  # stays settled

    def test_an_all_injected_tail_never_re_fires(self):
        self._delta("S", 0, "user", "a real note")
        consolidate.store_episodic("S", [{"role": "decision", "text": "t"}])
        ledger.append(capture._make_record("S", 1, "user", "<task-notification>\ndone\n</task-notification>",
                                           injected=True))
        self.assertNotIn("S", consolidate.detect_unconsolidated())       # injected tail is not fuel, not a trigger

    def test_a_legacy_marker_is_projected_not_re_consolidated_wholesale(self):
        # A pre-#446 marker has no through_seq; its ts boundary projects to the seq it was tidied through, so a
        # historically-tidied session is NOT re-summarized end-to-end on rollout.
        self._delta_at("S", 0, ts=100, text="tidied long ago")
        self._delta_at("S", 1, ts=200, text="also tidied long ago")
        self._legacy_marker("S", ts=250)
        self.assertNotIn("S", consolidate.detect_unconsolidated())       # projected watermark covers seq 0..1
        self._delta_at("S", 2, ts=300, text="added after the legacy tidy")
        self.assertIn("S", consolidate.detect_unconsolidated())          # only the genuinely-newer tail re-flags
        _g, wm, _h = consolidate._session_states().get("S")
        self.assertEqual([d["text"] for d in consolidate.read_deltas("S", after_seq=wm)],
                         ["added after the legacy tidy"])

    def test_a_concurrent_second_sweep_of_the_same_window_is_a_no_op(self):
        # Two boot sweeps racing on one idle session: the first advances the watermark; the second recomputes the
        # residual under the lock, finds it empty, and no-ops — never double-consolidating the prefix.
        self._delta("S", 0, "user", "the note")
        first = consolidate.store_episodic("S", [{"role": "decision", "text": "first sweep"}])
        second = consolidate.store_episodic("S", [{"role": "decision", "text": "second sweep"}])
        self.assertEqual(first["status"], "ok")
        self.assertEqual(second["status"], "already-consolidated")
        self.assertEqual(len(self._records(consolidate.EPISODIC_KIND)), 1)

    def test_a_revived_session_aborts_the_re_tidy_without_advancing(self):
        # The revival race: a genuine tail whose session checked in since detection (fresh lease) is left for its
        # own tidy — the store aborts and does NOT advance the watermark, so the tail is preserved.
        self._delta("S", 0, "user", "first half")
        consolidate.store_episodic("S", [{"role": "decision", "text": "summary"}])
        self._delta("S", 1, "user", "second half")
        capture._write_lease_state(self._tmp.name, 5, {"S": 5})          # S is live again (fresh lease)
        out = consolidate.store_episodic("S", [{"role": "lesson", "text": "would-be tail summary"}])
        self.assertEqual(out["status"], "live")
        self.assertEqual(out["stored"], 0)
        self.assertEqual(len(self._records(consolidate.MARKER_KIND)), 1)   # watermark NOT advanced (no 2nd marker)
        capture._write_lease_state(self._tmp.name, 5, {"S": 1})          # S goes silent again (aged 4 >= 3)
        self.assertIn("S", consolidate.detect_unconsolidated())          # its later half is preserved, re-flagged


class DirectiveTests(_Base):
    def test_caps_the_enumerated_ids_but_keeps_the_count(self):
        pending = [f"s{i:02d}" for i in range(12)]
        text = consolidate._consolidation_directive(pending)
        self.assertIn("12 earlier sessions", text)
        self.assertIn("and 4 more", text)                                  # 12 pending, 8 shown
        self.assertEqual(text.count("s00"), 1)

    def test_is_subordinate_to_the_request_but_prompt_this_session(self):
        # The directive must stay AFTER the operator's request (no first-turn hijack) and stay ACTIVE — done THIS
        # session, never deferred ("can wait" is gone). Silence (do-not-announce) is now DECOUPLED from passivity:
        # the pass is quiet but still prompt. Holds for a normal backlog (1) and a stalled one (20).
        for pending in ([f"s{i}" for i in range(1)], [f"s{i}" for i in range(20)]):
            text = consolidate._consolidation_directive(pending).lower()
            self.assertIn("after you have served the operator's current request", text)  # subordinate
            self.assertIn("never a first-turn hijack", text)                             # subordinate
            self.assertIn("this session", text)                                          # prompt, not someday
            self.assertNotIn("can wait", text)                                           # the passivity is gone

    def test_is_a_silent_pass_when_the_backlog_is_normal(self):
        # The routine case is QUIET: a normal-size backlog tidies silently, with NO announcement to the operator
        # (the play-by-play they didn't want). It stays answerable on demand ("unless they ask").
        pending = [f"sess-{i:02d}" for i in range(3)]                       # 3 < _BACKLOG_ALARM_THRESHOLD
        self.assertLess(len(pending), consolidate._BACKLOG_ALARM_THRESHOLD)
        text = consolidate._consolidation_directive(pending)
        self.assertIn("do not announce it", text.lower())                  # silent by default
        self.assertIn("unless they ask", text.lower())                     # …but answerable if the operator asks
        self.assertNotIn("tell the operator", text.lower())                # no proactive announcement
        self.assertNotIn("fallen behind", text.lower())                    # the tripwire stays silent here

    def test_breaks_silence_once_when_the_backlog_has_stalled(self):
        # A backlog past the threshold means the silent tidy has stalled (the 21-untidied-sessions failure mode):
        # the directive breaks silence with ONE plain line — a COUNT, never the raw session ids.
        pending = [f"sess-{i:02d}" for i in range(consolidate._BACKLOG_ALARM_THRESHOLD + 2)]
        text = consolidate._consolidation_directive(pending)
        self.assertIn("fallen behind", text.lower())                       # the tripwire fires
        self.assertIn("tell the operator", text.lower())                   # …surfacing it to the operator
        self.assertIn(f"{len(pending)} earlier sessions", text)            # as a COUNT
        self.assertIn("never the id codes", text.lower())                  # …still never the raw ids

    def test_tripwire_boundary_is_inclusive_at_the_threshold(self):
        # Pin the >= comparison against an off-by-one: exactly AT the threshold fires; one below stays silent.
        thr = consolidate._BACKLOG_ALARM_THRESHOLD
        at = consolidate._consolidation_directive([f"s{i}" for i in range(thr)]).lower()
        below = consolidate._consolidation_directive([f"s{i}" for i in range(thr - 1)]).lower()
        self.assertIn("fallen behind", at)                                 # n == threshold -> breaks silence
        self.assertNotIn("fallen behind", below)                           # n == threshold - 1 -> still silent

    def test_operator_remember_directive_is_typed_preference(self):
        # #258 (D-251): the sweep MUST type an explicit operator "remember X" as its own `preference`
        # summary — never folded into a thread summary, never dropped. This is the falsifiable artifact
        # for #258's durable-capture guarantee (the directive is unenforceable AI prose otherwise).
        text = consolidate._consolidation_directive(["s0"]).lower()
        self.assertIn("remember", text)                                    # the trigger is named
        self.assertIn("preference", text)                                  # …and typed to the preference role
        self.assertIn("never dropped", text.replace("\n", " "))            # the strong-preservation clause
        self.assertIn("preference", consolidate.ROLE_VOCABULARY)           # the role it points at really exists

    def test_directive_spawns_a_subagent_off_the_main_transcript(self):
        # #280: the mechanics must run in a spawned subagent so the read/store tool calls + raw JSON stay OFF
        # the operator's main transcript (only a brief task card shows) — the structural fix, not a louder
        # "please be quiet" instruction.
        text = consolidate._consolidation_directive(["s0"]).lower()
        self.assertIn("spawn a subagent", text)                            # the mechanism is a subagent
        self.assertIn("main transcript", text)                            # …whose work stays off the main transcript
        self.assertIn("task card", text)                                  # only a brief card is acknowledged to show

    def test_directive_forbids_relaying_the_subagent_result(self):
        # #280 crux (plan-gate blocking): the parent loop's default after a tool returns is to narrate the
        # result — which would RE-create the announcement the fix removes. The directive must forbid relaying
        # the subagent's result. Holds for a NORMAL backlog (no proactive announcement at all).
        text = consolidate._consolidation_directive(["s0"]).lower()
        self.assertIn("relay nothing", text)                              # the no-relay clause is present
        self.assertIn("do not summarize", text)                           # …named concretely
        self.assertNotIn("tell the operator", text)                       # normal case: no operator-facing line

    def test_subagent_prompt_is_complete_carrying_ids_roles_and_the_cli(self):
        # Reliability (plan-gate serious): spawning from prose is heavier than "run this command", so the
        # directive must hand the subagent a COMPLETE prompt — the ids, the read/store CLI, and the role set —
        # not gesture at it.
        pending = ["alpha-sess", "beta-sess"]
        text = consolidate._consolidation_directive(pending)
        for sid in pending:
            self.assertIn(sid, text)                                       # the exact session ids are handed over
        self.assertIn("consolidate.py read <session-id>", text)            # the read verb
        self.assertIn("consolidate.py store <session-id>", text)           # the store verb
        for role in consolidate.ROLE_VOCABULARY:
            self.assertIn(role, text)                                      # the closed label set travels with it

    def test_directive_asks_for_topic_tags_and_the_store_format_carries_them(self):
        # #235: the fuel step — the directive must ask the subagent to assign topic/entity tags AND the store
        # format it hands over must include the tags field, or nothing would ever produce the tags a later
        # cross-session roll-up clusters on.
        text = consolidate._consolidation_directive(["s0"])
        low = text.lower()
        self.assertIn("topic/entity tags", low)                            # the tag-assignment instruction
        self.assertIn("across sessions", low)                              # …stated as the cross-session purpose
        self.assertIn("omit the list", low)                                # …omit-rather-than-invent discipline
        self.assertIn("\"tags\"", text)                                    # the store format includes the field

    def test_the_stalled_backlog_alarm_is_the_main_loops_job(self):
        # Plan-gate serious: the suppressed subagent's output is not shown to the operator, so the one
        # break-silence line MUST be spoken by the MAIN loop (deterministic on the count), or a silently
        # stalled sweep could hide — exactly the failure the tripwire exists to catch (#203).
        pending = [f"s{i:02d}" for i in range(consolidate._BACKLOG_ALARM_THRESHOLD + 1)]
        text = consolidate._consolidation_directive(pending).lower()
        self.assertIn("you (the main loop) must", text)                    # the alarm is anchored to the main loop
        self.assertIn("tell the operator", text)                          # …which is the one thing it does say
        # …and the no-relay clause names that alarm as its sole exception, so the two don't contradict.
        self.assertIn("the stalled-backlog line above", text)


class LockTests(_Base):
    def test_lock_miss_is_a_clean_noop(self):
        orig = capture._acquire_lock
        capture._acquire_lock = lambda _path: None   # simulate contention: the lock is never acquired
        try:
            report = consolidate.store_episodic("S", [{"role": "decision", "text": "t"}])
        finally:
            capture._acquire_lock = orig
        self.assertEqual(report["status"], "busy")
        self.assertEqual(report["stored"], 0)
        self.assertEqual(self._records(), [])         # NOTHING written without the lock


class CrashResidualTests(_Base):
    def test_a_crashed_pass_is_re_filed_then_logically_retired_from_recall(self):
        # Simulate a crash AFTER the summary appends but BEFORE the marker: an orphan episodic whose batch
        # never gets a closing marker.
        self._delta("S", 0, "user", "a note")
        ledger.append(consolidate._make_episodic("S", {"role": "decision", "text": "partial summary"},
                                                 "the-pass-that-crashed"))
        self.assertIn("S", consolidate.detect_unconsolidated())        # still pending — the marker never landed
        # The next sweep re-files the session: a fresh pass (new batch) + a marker, and rebuilds recall.
        consolidate.store_episodic("S", [{"role": "decision", "text": "retry summary"}])

        # Both copies stay RESIDENT in the ledger (recoverable) — the re-file favours a duplicate over a loss,
        # and nothing is erased.
        self.assertEqual(len(self._records(consolidate.EPISODIC_KIND)), 2)
        self.assertEqual(len(self._records(consolidate.MARKER_KIND)), 1)
        self.assertNotIn("S", consolidate.detect_unconsolidated())

        # But RECALL surfaces the completed pass ONLY — the orphaned duplicate is logically retired (4a).
        recalled = [r for r in index.query("summary").records if r.get("kind") == consolidate.EPISODIC_KIND]
        self.assertEqual(len(recalled), 1)
        self.assertEqual(recalled[0]["text"], "retry summary")

        # The marker's batch links to its own completed pass, never to the crashed orphan.
        marker = self._records(consolidate.MARKER_KIND)[0]
        retry = next(r for r in self._records(consolidate.EPISODIC_KIND) if r["text"] == "retry summary")
        orphan = next(r for r in self._records(consolidate.EPISODIC_KIND) if r["text"] == "partial summary")
        self.assertEqual(marker["batch"], retry["batch"])
        self.assertNotEqual(marker["batch"], orphan["batch"])


class HookHandlerTests(_Base):
    def test_session_start_injects_the_directive_when_pending(self):
        self._delta("old-session", 0, "user", "an untidied note")
        code, out, _err = self._run_hook("SessionStart", consolidate._session_start_handler,
                                         {"session_id": "live-session"})
        self.assertEqual(code, hooks.EXIT_PROCEED)
        payload = json.loads(out)
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("old-session", ctx)
        self.assertIn("during this session", ctx.lower())          # visible + prompt: tidy THIS session
        # it must subordinate tidy-up to the operator's request (the no-hijack law)
        self.assertIn("after you have served the operator's current request", ctx.lower())

    def test_session_start_excludes_the_live_session(self):
        self._delta("live-session", 0, "user", "still writing this one")
        code, out, _err = self._run_hook("SessionStart", consolidate._session_start_handler,
                                         {"session_id": "live-session"})
        self.assertEqual(out.strip(), "")              # nothing pending (live excluded) → no injection

    def test_session_start_is_inert_when_nothing_is_pending(self):
        code, out, _err = self._run_hook("SessionStart", consolidate._session_start_handler,
                                         {"session_id": "live-session"})
        self.assertEqual(code, hooks.EXIT_PROCEED)
        self.assertEqual(out.strip(), "")              # the common case adds nothing to the operator's session

    def test_pre_compact_skips_and_proceeds_when_no_waste(self):
        # PreCompact now runs the gated ledger-compaction trigger (slice 5 PR 3). On a ledger with no reclaimable
        # waste the gate SKIPS (no rewrite) and the handler proceeds with no output — it must never block the
        # squash. (The fires-and-still-proceeds path is pinned in test_compact_trigger.py.)
        code, out, _err = self._run_hook("PreCompact", consolidate._pre_compact_handler,
                                         {"trigger": "auto"})
        self.assertEqual(code, hooks.EXIT_PROCEED)
        self.assertEqual(out.strip(), "")

    def test_a_handler_crash_fails_open(self):
        # run_hook must never let a handler fault gate the turn (the fail-open floor)
        boom = lambda _p: (_ for _ in ()).throw(RuntimeError("boom"))
        code, _out, err = self._run_hook("SessionStart", boom, {"session_id": "x"})
        self.assertEqual(code, hooks.EXIT_NONBLOCKING)
        self.assertTrue(err.strip())                   # a plain-language finding was emitted


class LeaseHeartbeatTests(_Base):
    """U08 (#396): the session lease keeps a concurrent live session from being wrongly consolidated, and the
    store-time re-check — not N — is the guarantee."""

    def _mem(self):
        return self._tmp.name

    def _seed_lease(self, epoch, leases):
        capture._write_lease_state(self._mem(), epoch, leases)

    def _corrupt_lease(self):
        with open(os.path.join(self._mem(), capture.LEASE_FILENAME), "w", encoding="utf-8") as fh:
            fh.write("{corrupt")

    def test_stale_predicate(self):
        self.assertTrue(consolidate._lease_is_stale("s", 5, {}))                 # absent -> stale
        self.assertTrue(consolidate._lease_is_stale("s", 5, {"s": 2}, n=3))      # aged 3 >= 3
        self.assertFalse(consolidate._lease_is_stale("s", 5, {"s": 3}, n=3))     # aged 2 < 3
        self.assertFalse(consolidate._lease_is_stale("s", 5, {"s": 5}, n=3))     # fresh

    def test_detect_excludes_a_live_lease_session(self):
        self._delta("live", 0, "user", "note")
        self._delta("gone", 0, "user", "note")
        self._seed_lease(10, {"live": 10, "gone": 1})               # live is fresh, gone is far aged
        self.assertEqual(consolidate.detect_unconsolidated(), ["gone"])

    def test_detect_includes_a_session_with_no_lease_sidecar(self):
        self._delta("A", 0, "user", "note")                        # no sidecar => absent => stale => detected
        self.assertEqual(consolidate.detect_unconsolidated(), ["A"])

    def test_a_corrupt_lease_skips_the_whole_sweep(self):
        self._delta("A", 0, "user", "note")
        self._corrupt_lease()
        self.assertEqual(consolidate.detect_unconsolidated(), [])   # fail safe: all possibly-live

    def test_store_aborts_when_the_target_is_fresh(self):
        # The store-time re-check: a session that checked in since detection (fresh lease) is NOT consolidated.
        self._delta("A", 0, "user", "note")
        self._seed_lease(5, {"A": 5})                              # fresh
        out = consolidate.store_episodic("A", [{"role": "decision", "text": "did a thing"}])
        self.assertEqual(out["status"], "live")
        self.assertEqual(out["stored"], 0)
        self.assertEqual(self._records(kind=consolidate.MARKER_KIND), [])   # nothing half-written

    def test_store_proceeds_when_the_target_is_stale(self):
        self._delta("A", 0, "user", "note")
        self._seed_lease(5, {"A": 1})                             # aged 4 >= 3 => stale
        out = consolidate.store_episodic("A", [{"role": "decision", "text": "did a thing"}])
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["stored"], 1)

    def test_store_reaps_the_lease_on_success(self):
        self._delta("A", 0, "user", "note")
        self._seed_lease(5, {"A": 1, "B": 5})
        consolidate.store_episodic("A", [{"role": "decision", "text": "x"}])
        _, leases = capture.read_lease_state(self._mem())
        self.assertNotIn("A", leases)                            # reaped (a marked session can't be live again)
        self.assertIn("B", leases)

    def test_store_defers_on_a_corrupt_lease(self):
        self._delta("A", 0, "user", "note")
        self._corrupt_lease()
        out = consolidate.store_episodic("A", [{"role": "decision", "text": "x"}])
        self.assertEqual(out["status"], "deferred")
        self.assertEqual(out["stored"], 0)

    def test_session_start_stamps_the_live_session_lease(self):
        self._run_hook("SessionStart", consolidate._session_start_handler, {"session_id": "me"})
        _, leases = capture.read_lease_state(self._mem())
        self.assertIn("me", leases)

    def test_session_start_defers_the_sweep_when_it_cannot_stamp(self):
        # Hold the lock so the self-lease stamp can't land => the consolidation sweep is deferred (never swept
        # with a missing self-lease); no directive names the pending session.
        self._delta("gone", 0, "user", "old note")
        lock_fd = capture._acquire_lock(os.path.join(self._mem(), capture.LOCK_FILENAME))
        self.addCleanup(capture._release_lock, lock_fd)
        _, out, _ = self._run_hook("SessionStart", consolidate._session_start_handler, {"session_id": "me"})
        self.assertNotIn("gone", out)


if __name__ == "__main__":
    unittest.main()
