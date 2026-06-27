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

    def test_empty_batch_is_rejected(self):
        self.assertEqual(consolidate.store_episodic("S", [])["status"], "rejected")

    def test_missing_session_id_is_rejected(self):
        self.assertEqual(consolidate.store_episodic("", [{"role": "lesson", "text": "t"}])["status"], "rejected")


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


if __name__ == "__main__":
    unittest.main()
