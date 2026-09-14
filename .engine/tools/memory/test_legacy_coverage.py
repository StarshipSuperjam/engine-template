"""Unit tests for legacy_coverage.py — the read-only legacy summary/gist coverage census (StarshipSuperjam/engine-template#1053).

The census reads the RAW ledger through `ledger.read()` and classifies every referenced session and every
gist source-record into present / absent / excluded-but-present, carrying read-health and a start/end
consistency fingerprint. These tests mint the legacy record shapes with `legacy_shapes`' pure dict minters
and write them straight to a throwaway ledger file (the census only READS, so the guarded write path is not
needed and would demand write-qualification). They pin: the three source-record states — including an
inert-until-closed supersession staying PRESENT; the three referenced-session states measured over
CONVERSATION records; the read-health and fingerprint indeterminacy rails; and that the identifier-level
artifact carries NO record text.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import ledger, records, legacy_shapes, legacy_coverage

_ID = records.RECORD_ID_KEY


def _turn(session_id: str, text: str = "raw turn") -> dict:
    return {"v": 1, "kind": records.AMBIENT_CAPTURE_KIND, _ID: records.new_record_id(),
            "session_id": session_id, "ts": 123, "text": text}


def _withhold_session(sid: str) -> dict:
    return {"v": 1, "kind": records.WITHHOLD_KIND, _ID: records.new_record_id(),
            records.TARGET_SESSION_KEY: sid, "ts": 123}


def _restore_session(sid: str) -> dict:
    return {"v": 1, "kind": records.RESTORE_KIND, _ID: records.new_record_id(),
            records.TARGET_SESSION_KEY: sid, "ts": 124}


def _states(result, subject_kind):
    return {row["subject_id"]: row["state"] for row in result.rows if row["subject_kind"] == subject_kind}


class TestCensus(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "ledger.jsonl")

    def _write(self, recs):
        # Straight to disk in the on-disk ledger format — the census reads raw, so no guarded write is needed.
        with open(self.path, "a", encoding="utf-8") as fh:
            for r in recs:
                fh.write(json.dumps(r) + "\n")

    def test_source_record_states_and_inert_supersession_stays_present(self):
        # PRESENT: a raw whose gist roll-up never closed — the supersession is INERT, the raw is still live.
        e_inert = legacy_shapes.episodic("S_INERT", "user", "x", "b_inert")
        g_inert = legacy_shapes.gist("S_INERT", "g open", [e_inert[_ID]], "b_inert")
        sup_inert = legacy_shapes.superseded(e_inert[_ID], g_inert[_ID], "b_inert")
        self._write([e_inert, g_inert, sup_inert])   # no rollup_marker -> batch un-closed -> inert
        # EXCLUDED-BUT-PRESENT: a raw whose roll-up CLOSED — retained in the ledger, hidden from recall.
        e_sup = legacy_shapes.episodic("S_SUP", "user", "y", "b_sup")
        g_sup = legacy_shapes.gist("S_SUP", "g closed", [e_sup[_ID]], "b_sup")
        self._write([e_sup, g_sup,
                     legacy_shapes.superseded(e_sup[_ID], g_sup[_ID], "b_sup"),
                     legacy_shapes.rollup_marker("S_SUP", "b_sup")])
        # ABSENT: a gist naming a source id that has no line at all.
        g_miss = legacy_shapes.gist("S_MISS", "g missing", ["missing-src-id"], "b_miss")
        self._write([g_miss,
                     legacy_shapes.superseded("missing-src-id", g_miss[_ID], "b_miss"),
                     legacy_shapes.rollup_marker("S_MISS", "b_miss")])

        result = legacy_coverage.census(path=self.path)
        src = _states(result, legacy_coverage.GIST_SOURCE)
        self.assertEqual(src[e_inert[_ID]], legacy_coverage.PRESENT)
        self.assertEqual(src[e_sup[_ID]], legacy_coverage.EXCLUDED_BUT_PRESENT)
        self.assertEqual(src["missing-src-id"], legacy_coverage.ABSENT)
        self.assertFalse(result.indeterminate, result.indeterminate_reasons)

    def test_referenced_session_states_over_conversation_records(self):
        # PRESENT: a live turn-delta survives for the session the summary describes.
        self._write([_turn("S_LIVE"), legacy_shapes.episodic("S_LIVE", "user", "summary", "b1")])
        # EXCLUDED-BUT-PRESENT: the session's conversation exists but the operator withheld the whole session.
        self._write([_turn("S_HID"), legacy_shapes.episodic("S_HID", "user", "summary", "b2"),
                     _withhold_session("S_HID")])
        # ABSENT: a gist whose real session has NO surviving conversation record (turns compacted away).
        self._write([legacy_shapes.gist("S_GONE", "g", ["missing"], "b3")])

        result = legacy_coverage.census(path=self.path)
        epi = _states(result, legacy_coverage.EPISODIC_SESSION)
        gis = _states(result, legacy_coverage.GIST_SESSION)
        self.assertEqual(epi["S_LIVE"], legacy_coverage.PRESENT)
        self.assertEqual(epi["S_HID"], legacy_coverage.EXCLUDED_BUT_PRESENT)
        self.assertEqual(gis["S_GONE"], legacy_coverage.ABSENT)

    def test_restore_after_withhold_returns_session_to_present(self):
        self._write([_turn("S_R"), legacy_shapes.episodic("S_R", "user", "s", "b"),
                     _withhold_session("S_R"), _restore_session("S_R")])
        result = legacy_coverage.census(path=self.path)
        self.assertEqual(_states(result, legacy_coverage.EPISODIC_SESSION)["S_R"], legacy_coverage.PRESENT)

    def test_cross_session_cluster_gist_is_bucketed_and_its_sentinel_is_never_a_subject(self):
        # A cluster gist none of whose sources has a line: bucketed, counted as unresolved, and the `tag:`
        # sentinel itself never appears as a session subject.
        self._write([legacy_shapes.gist(records.TAG_SESSION_PREFIX + "topic", "cluster", ["missing"], "bc")])
        result = legacy_coverage.census(path=self.path)
        self.assertEqual(result.scanned["gist_cross_session_clusters"], 1)
        self.assertEqual(result.scanned["gists_sessions_unresolved"], 1)
        self.assertEqual(_states(result, legacy_coverage.GIST_SESSION), {})

    def test_a_gists_sessions_are_resolved_through_its_source_ids(self):
        # Round 3, DH-3: a cross-session cluster gist folded episodic records from two sessions. Its
        # contributing sessions are resolved THROUGH source_ids to the episodic records it folded, and each
        # is classified over that session's conversation records: S_A still has a turn (present); S_B's
        # conversation is gone (absent). The sentinel is not a subject; the gist itself is one row per session.
        e_a = legacy_shapes.episodic("S_A", "user", "a", "ba")
        e_b = legacy_shapes.episodic("S_B", "user", "b", "bb")
        cluster = legacy_shapes.gist(records.TAG_SESSION_PREFIX + "topic", "cluster", [e_a[_ID], e_b[_ID]], "bc")
        self._write([_turn("S_A"), e_a, e_b, cluster])
        result = legacy_coverage.census(path=self.path)
        rows = [r for r in result.rows
                if r["subject_kind"] == legacy_coverage.GIST_SESSION and r["referenced_by"] == cluster[_ID]]
        self.assertEqual({r["subject_id"]: r["state"] for r in rows},
                         {"S_A": legacy_coverage.PRESENT, "S_B": legacy_coverage.ABSENT})
        self.assertEqual(result.scanned["gists_sessions_unresolved"], 0)
        self.assertEqual(result.counts[legacy_coverage.GIST_SESSION][legacy_coverage.ABSENT], 1)

    def test_a_single_session_gist_names_its_session_once_even_when_its_sources_agree(self):
        # DH-3: the gist's own real session and the session its sources resolve to are the same session —
        # one row, not two, so the counts are not inflated.
        e = legacy_shapes.episodic("S_ONE", "user", "x", "b1")
        g = legacy_shapes.gist("S_ONE", "g", [e[_ID]], "b1")
        self._write([_turn("S_ONE"), e, g])
        result = legacy_coverage.census(path=self.path)
        rows = [r for r in result.rows
                if r["subject_kind"] == legacy_coverage.GIST_SESSION and r["referenced_by"] == g[_ID]]
        self.assertEqual([(r["subject_id"], r["state"]) for r in rows], [("S_ONE", legacy_coverage.PRESENT)])

    def test_a_source_from_a_wholly_withheld_session_is_excluded_but_present(self):
        # Round 3, DH-4: the operator withheld all of S_W. Its episodic record is unsuperseded and still has a
        # line, but it is hidden from recall exactly as a record withhold hides one — so a gist that cites it
        # sees the source as excluded-but-present, never plainly present. The session row says the same.
        e_w = legacy_shapes.episodic("S_W", "user", "w", "bw")
        g = legacy_shapes.gist("S_W", "g", [e_w[_ID]], "bw")
        self._write([_turn("S_W"), e_w, g, _withhold_session("S_W")])
        result = legacy_coverage.census(path=self.path)
        self.assertEqual(_states(result, legacy_coverage.GIST_SOURCE)[e_w[_ID]],
                         legacy_coverage.EXCLUDED_BUT_PRESENT)
        self.assertEqual(_states(result, legacy_coverage.GIST_SESSION)["S_W"],
                         legacy_coverage.EXCLUDED_BUT_PRESENT)
        # ...and a restore puts the source back to present.
        self._write([_restore_session("S_W")])
        result = legacy_coverage.census(path=self.path)
        self.assertEqual(_states(result, legacy_coverage.GIST_SOURCE)[e_w[_ID]], legacy_coverage.PRESENT)

    def test_read_health_marks_indeterminate_and_surfaces_counts(self):
        self._write([legacy_shapes.episodic("S", "user", "s", "b")])
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write("this is not json\n")            # a malformed complete line
            fh.write("torn fragment with no newline")  # a torn trailing line
        result = legacy_coverage.census(path=self.path)
        self.assertTrue(result.indeterminate)
        self.assertGreaterEqual(result.read_health["malformed"], 1)
        self.assertTrue(result.read_health["torn_trailing"])
        self.assertTrue(any("malformed" in r for r in result.indeterminate_reasons))

    def test_ledger_change_during_pass_marks_indeterminate(self):
        self._write([legacy_shapes.episodic("S", "user", "s", "b")])
        fp_a = (0, 0, "aaaa")
        fp_b = (0, 1, "bbbb")   # a different index_epoch/digest: the ledger moved under the pass
        with mock.patch.object(legacy_coverage, "_fingerprint", side_effect=[fp_a, fp_b]):
            result = legacy_coverage.census(path=self.path)
        self.assertTrue(result.indeterminate)
        self.assertTrue(any("the ledger changed while this census was reading it" in r
                            for r in result.indeterminate_reasons))

    def test_artifact_rows_carry_no_record_text(self):
        self._write([_turn("S", text="SECRET CONVERSATION TEXT"),
                     legacy_shapes.episodic("S", "user", "SECRET SUMMARY TEXT", "b"),
                     legacy_shapes.gist("S2", "SECRET GIST TEXT", ["missing"], "b2")])
        result = legacy_coverage.census(path=self.path)
        allowed = {"subject_kind", "subject_id", "referenced_by", "state"}
        for row in result.rows:
            self.assertEqual(set(row.keys()), allowed, row)
        blob = legacy_coverage.render(result) + repr(result.as_dict())
        for secret in ("SECRET CONVERSATION TEXT", "SECRET SUMMARY TEXT", "SECRET GIST TEXT"):
            self.assertNotIn(secret, blob)

    def test_denominator_is_declared_unknown(self):
        self._write([legacy_shapes.episodic("S", "user", "s", "b")])
        result = legacy_coverage.census(path=self.path)
        self.assertTrue(result.denominator_unknown)
        self.assertIn("UNKNOWABLE", legacy_coverage.render(result))

    def test_empty_ledger_is_clean_and_determinate(self):
        result = legacy_coverage.census(path=self.path)
        self.assertFalse(result.indeterminate)
        self.assertEqual(result.rows, [])
        self.assertEqual(result.scanned, {"episodics": 0, "gists": 0, "gist_cross_session_clusters": 0,
                                          "gists_sessions_unresolved": 0})


if __name__ == "__main__":
    unittest.main()
