"""Unit tests for clawmem_export.py — the terminal-gated, point-in-time ClawMem export.

The load-bearing halves are the ABSENCE guarantees (withheld records, withheld sessions, withheld pins,
pending erasures, injected and scaffolding content) and the CONSENT controls (terminal gate, destination guard,
fail-closed scrub). Most of what is here proves that content the operator took out of recall — or that the
harness inserted — never reaches the export, and that the verb refuses everywhere it must. The completeness
half proves the converse: every eligible message appears exactly once, counted against the ledger.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import clawmem_export, export, forget, ledger, recall, records, scrub  # noqa: E402


def _sid() -> str:
    """A fresh, valid (hex) conversation session id."""
    return records.new_record_id()


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._prev = os.environ.get(ledger.ENV_DIR)
        os.environ[ledger.ENV_DIR] = self._tmp.name
        self.out = tempfile.TemporaryDirectory()   # a place to write exports, outside any git tree
        self._seq = 0

    def tearDown(self):
        if self._prev is None:
            os.environ.pop(ledger.ENV_DIR, None)
        else:
            os.environ[ledger.ENV_DIR] = self._prev
        self._tmp.cleanup()
        self.out.cleanup()

    # --- fixture builders -------------------------------------------------------------------------------
    def _append(self, record):
        ledger.append(record)

    def _turn(self, sid, seq, speaker, text, ts=1000, tags=None):
        record = {"v": 1, "kind": records.AMBIENT_CAPTURE_KIND, records.RECORD_ID_KEY: records.new_record_id(),
                  "session_id": sid, "seq": seq, "speaker": speaker, "ts": ts, "text": text}
        if tags is not None:
            record["tags"] = tags
        self._append(record)
        return record

    def _episodic(self, sid, text, ts=1000, kind=None):
        record = {"v": 1, "kind": kind or records.EPISODIC_KIND, records.RECORD_ID_KEY: records.new_record_id(),
                  "session_id": sid, "ts": ts, "role": "assistant", "text": text,
                  "tags": [records.DEFAULT_EPISODIC_TAG]}
        self._append(record)
        return record

    def _pin(self, text, source_session=None, ts=1000):
        record = {"v": 1, "kind": records.PIN_KIND, records.RECORD_ID_KEY: records.new_record_id(),
                  "ts": ts, "text": text, records.PIN_VIA_KEY: records.PIN_VIA_CLI}
        if source_session:
            record[records.PIN_SOURCE_SESSION_KEY] = source_session
        self._append(record)
        return record

    def _withhold_record(self, rid, ts=2000):
        self._append({"kind": records.WITHHOLD_KIND, records.RECORD_ID_KEY: records.new_record_id(),
                      records.TARGET_KEY: rid, "ts": ts})

    def _withhold_session(self, sid, ts=2000):
        self._append({"kind": records.WITHHOLD_KIND, records.RECORD_ID_KEY: records.new_record_id(),
                      records.TARGET_SESSION_KEY: sid, "ts": ts})

    def _erasure_marker(self, rid, merge_sha="deadbeef", ts=2000):
        self._append({"kind": records.ERASURE_KIND, records.RECORD_ID_KEY: records.new_record_id(),
                      records.TARGET_KEY: rid, records.MERGE_SHA_KEY: merge_sha, "ts": ts})

    # --- helpers ---------------------------------------------------------------------------------------
    def _export(self, name="export", now=1_700_000_000):
        dest = os.path.join(self.out.name, name)
        manifest = clawmem_export.export_all(dest, now=now)
        return dest, manifest

    def _conversation_lines(self, dest, sid):
        path = os.path.join(dest, "conversations", f"{sid}.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def _all_conversation_text(self, dest):
        root = os.path.join(dest, "conversations")
        blob = []
        for name in os.listdir(root) if os.path.isdir(root) else []:
            with open(os.path.join(root, name), encoding="utf-8") as fh:
                blob.append(fh.read())
        return "\n".join(blob)


class CompletenessTests(_Base):
    def _expected_messages(self, src):
        """An INDEPENDENT ledger-side count of eligible MESSAGES (distinct session+seq), so a join that dropped
        or duplicated a message is caught against the ledger, not against the exporter's own bookkeeping."""
        withheld_ids, withheld_sessions = forget.withheld_targets(src)
        injected = forget._injected_message_keys(src)
        seen = set()
        for record in ledger.iter_records(path=src):
            if record.get("kind") != records.AMBIENT_CAPTURE_KIND or not recall.is_genuine_turn(record):
                continue
            sid, seq = record.get("session_id"), record.get("seq")
            if isinstance(sid, str) and isinstance(seq, int) and not isinstance(seq, bool) \
                    and (sid, seq) in injected:
                continue
            if isinstance(sid, str) and sid in withheld_sessions:
                continue
            if forget.is_withheld(record, withheld_ids, withheld_sessions):
                continue
            if not (isinstance(sid, str) and re.match(r"\A[0-9a-f]{16,64}\Z", sid)):
                continue
            if record.get("speaker") not in ("user", "assistant"):
                continue
            if not (isinstance(record.get("ts"), int) and not isinstance(record.get("ts"), bool)):
                continue
            seen.add((sid, seq))
        return len(seen)

    def test_manifest_message_count_equals_the_ledger_side_count(self):
        a, b = _sid(), _sid()
        for i in range(4):
            self._turn(a, i, "user" if i % 2 == 0 else "assistant", f"turn {i}", ts=100 + i)
        for i in range(3):
            self._turn(b, i, "user" if i % 2 == 0 else "assistant", f"other {i}", ts=200 + i)
        # noise that must NOT be counted as a message
        self._turn(a, 99, "user", "<task-notification>done</task-notification>", tags=[records.INJECTED_TAG])
        self._episodic(a, "a summary")
        dest, manifest = self._export()
        expected = self._expected_messages(ledger.ledger_path())
        self.assertEqual(manifest["counts"]["messages"], expected)
        self.assertEqual(sum(c["messages"] for c in manifest["conversations"].values()), expected)

    def test_census_by_kind_counts_every_record(self):
        s = _sid()
        self._turn(s, 0, "user", "hello")
        self._turn(s, 1, "assistant", "hi")
        self._episodic(s, "summary one")
        self._episodic(s, "gist one", kind=records.GIST_KIND)
        self._append({"kind": records.REINFORCEMENT_KIND, records.RECORD_ID_KEY: records.new_record_id(),
                      records.TARGET_KEY: "x", "ts": 1})
        _, manifest = self._export()
        census = manifest["census_by_kind"]
        self.assertEqual(census[records.AMBIENT_CAPTURE_KIND], 2)
        self.assertEqual(census[records.EPISODIC_KIND], 1)
        self.assertEqual(census[records.GIST_KIND], 1)
        self.assertEqual(census[records.REINFORCEMENT_KIND], 1)

    def test_an_oversized_session_exports_whole_with_no_cap(self):
        # export.py caps at MAX_RECORDS (5000); this verb deliberately does not.
        s = _sid()
        big = export.MAX_RECORDS + 5
        lines = [json.dumps({"v": 1, "kind": records.AMBIENT_CAPTURE_KIND,
                             records.RECORD_ID_KEY: records.new_record_id(), "session_id": s, "seq": i,
                             "speaker": "user" if i % 2 == 0 else "assistant", "ts": 1000 + i,
                             "text": f"m{i}"})
                 for i in range(big)]
        with open(ledger.ledger_path(), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        dest, manifest = self._export()
        self.assertEqual(manifest["conversations"][s]["messages"], big)
        self.assertEqual(len(self._conversation_lines(dest, s)), big)


class WithholdTests(_Base):
    def test_a_withheld_record_is_absent_and_accounted(self):
        s = _sid()
        keep = self._turn(s, 0, "user", "keep this one")
        gone = self._turn(s, 1, "assistant", "SECRETWORD hide this")
        self._withhold_record(gone[records.RECORD_ID_KEY])
        dest, manifest = self._export()
        self.assertNotIn("SECRETWORD", self._all_conversation_text(dest))
        self.assertIn(gone[records.RECORD_ID_KEY], manifest["omission_account"]["withheld_records"])
        lines = self._conversation_lines(dest, s)
        self.assertEqual([l["message"]["content"] for l in lines], ["keep this one"])

    def test_a_withheld_session_is_absent_entirely(self):
        keep, hide = _sid(), _sid()
        self._turn(keep, 0, "user", "visible")
        self._turn(hide, 0, "user", "WITHHELDSESSION content")
        self._turn(hide, 1, "assistant", "more WITHHELDSESSION")
        self._withhold_session(hide)
        dest, manifest = self._export()
        self.assertNotIn("WITHHELDSESSION", self._all_conversation_text(dest))
        self.assertFalse(os.path.exists(os.path.join(dest, "conversations", f"{hide}.jsonl")))
        self.assertIn(hide, manifest["omission_account"]["withheld_sessions"])
        self.assertGreaterEqual(manifest["omission_account"]["withheld_session_messages_skipped"], 2)

    def test_a_live_pin_is_exported_but_a_removed_pin_is_not_the_resurrection_path(self):
        s = _sid()
        self._turn(s, 0, "user", "conversation")
        live = self._pin("keep this pinned")
        removed = self._pin("REMOVEDPIN should not resurface")
        self._withhold_record(removed[records.RECORD_ID_KEY])   # pins.remove withholds — a removed pin IS withheld
        dest, _ = self._export()
        with open(os.path.join(dest, "meta", "pins.jsonl"), encoding="utf-8") as fh:
            pins_out = [json.loads(line) for line in fh if line.strip()]
        ids = {p["id"] for p in pins_out}
        self.assertIn(live[records.RECORD_ID_KEY], ids)
        self.assertNotIn(removed[records.RECORD_ID_KEY], ids)
        # and its text is nowhere in the export at all
        for root, _dirs, files in os.walk(dest):
            for name in files:
                with open(os.path.join(root, name), encoding="utf-8") as fh:
                    self.assertNotIn("REMOVEDPIN", fh.read())

    def test_a_pin_is_never_emitted_into_a_conversation_file(self):
        s = _sid()
        self._turn(s, 0, "user", "conversation")
        self._pin("PINTEXT remember", source_session=s)
        dest, _ = self._export()
        self.assertNotIn("PINTEXT", self._all_conversation_text(dest))


class InjectedAndScaffoldingTests(_Base):
    def test_a_tagged_injected_pseudo_turn_is_dropped(self):
        s = _sid()
        self._turn(s, 0, "user", "real question")
        self._turn(s, 1, "user", "<task-notification>agent done</task-notification>",
                   tags=[records.INJECTED_TAG])
        dest, manifest = self._export()
        self.assertNotIn("task-notification", self._all_conversation_text(dest))
        self.assertGreaterEqual(manifest["omission_account"]["injected_filtered_messages"], 1)

    def test_an_untagged_legacy_injected_message_split_across_chunks_is_dropped_whole(self):
        # The head chunk is recognised by its start-anchored text; the TAIL chunk (same seq) matches neither the
        # tag nor the text — it must travel with the head via the message-wise predicate.
        s = _sid()
        self._turn(s, 0, "user", "genuine")
        head = "This session is being continued from a previous conversation. Summary:"
        tail = "All user messages: the operator asked THINGONE and THINGTWO"
        self._turn(s, 5, "user", head)        # same seq -> one message
        self._turn(s, 5, "user", tail)
        dest, manifest = self._export()
        blob = self._all_conversation_text(dest)
        self.assertNotIn("THINGONE", blob)
        self.assertNotIn("continued from a previous conversation", blob)
        self.assertGreaterEqual(manifest["omission_account"]["injected_filtered_messages"], 2)

    def test_a_fused_harness_span_is_substituted_out(self):
        s = _sid()
        self._turn(s, 0, "user",
                   "please do X <system-reminder>SECRETREMINDER internal note</system-reminder> thanks")
        dest, manifest = self._export()
        content = self._conversation_lines(dest, s)[0]["message"]["content"]
        self.assertNotIn("SECRETREMINDER", content)
        self.assertIn(records.HARNESS_SPAN_MARKER, content)
        self.assertIn("please do X", content)
        self.assertIn("thanks", content)
        self.assertGreaterEqual(manifest["omission_account"]["scaffolding_marked_turns"], 1)

    def test_a_scaffold_opener_turn_is_kept_as_recall_presents_the_transcript(self):
        # recall's transcript readers KEEP scaffold-opener turns (they are only non-quotable as session handles);
        # the export mirrors that, so a faithful transcript is not silently thinned.
        s = _sid()
        self._turn(s, 0, "user", "Base directory for this skill: /path/to/skill")
        self._turn(s, 1, "assistant", "ok")
        dest, _ = self._export()
        contents = [l["message"]["content"] for l in self._conversation_lines(dest, s)]
        self.assertIn("Base directory for this skill: /path/to/skill", contents)


class ScrubTests(_Base):
    def test_a_secret_straddling_a_chunk_boundary_is_masked(self):
        # Two chunks share one seq+speaker: neither half is a full credential, but the rejoined message is.
        s = _sid()
        self._turn(s, 0, "user", "here is my key sk-ant-")
        self._turn(s, 0, "user", "abcdef0123456789abcdef0123456789")
        dest, manifest = self._export()
        content = self._conversation_lines(dest, s)[0]["message"]["content"]
        self.assertIn("[redacted:anthropic-key]", content)
        self.assertNotIn("sk-ant-abcdef0123456789", content)
        self.assertGreaterEqual(manifest["omission_account"]["scrub_altered_messages"], 1)

    def test_a_scrub_fault_aborts_the_export_and_leaves_nothing_behind(self):
        s = _sid()
        self._turn(s, 0, "user", "would-be unmasked content")
        dest = os.path.join(self.out.name, "aborted")
        with mock.patch("memory.scrub.scrub_text", side_effect=RuntimeError("boom")):
            with self.assertRaises(clawmem_export.ExportRefused):
                clawmem_export.export_all(dest, now=1)
        self.assertFalse(os.path.exists(dest))   # torn down; no half-masked artifact survives


class GateAndDestinationTests(_Base):
    def test_export_refuses_a_destination_inside_a_git_tree(self):
        root = tempfile.mkdtemp(dir=self.out.name)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
        s = _sid()
        self._turn(s, 0, "user", "hi")
        with self.assertRaises((clawmem_export.ExportRefused, export.ExportRefused)):
            clawmem_export.export_all(os.path.join(root, "leaked"), now=1)
        self.assertFalse(os.path.exists(os.path.join(root, "leaked")))

    def test_export_refuses_a_nonempty_destination(self):
        s = _sid()
        self._turn(s, 0, "user", "hi")
        dest = os.path.join(self.out.name, "used")
        os.makedirs(dest)
        with open(os.path.join(dest, "stale.txt"), "w", encoding="utf-8") as fh:
            fh.write("old")
        with self.assertRaises(clawmem_export.ExportRefused):
            clawmem_export.export_all(dest, now=1)

    def test_main_refuses_without_a_terminal_and_writes_nothing(self):
        s = _sid()
        self._turn(s, 0, "user", "hi")
        dest = os.path.join(self.out.name, "no-tty")
        with mock.patch.object(clawmem_export, "_on_a_terminal", return_value=False):
            rc = clawmem_export.main([dest])
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(dest))

    def test_main_writes_when_attended(self):
        s = _sid()
        self._turn(s, 0, "user", "hi there")
        dest = os.path.join(self.out.name, "attended")
        with mock.patch.object(clawmem_export, "_on_a_terminal", return_value=True):
            rc = clawmem_export.main([dest])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(os.path.join(dest, "meta", "manifest.json")))

    def test_on_a_terminal_reads_both_streams(self):
        with mock.patch("sys.stdin.isatty", return_value=True), \
                mock.patch("sys.stdout.isatty", return_value=True):
            self.assertTrue(clawmem_export._on_a_terminal())
        with mock.patch("sys.stdin.isatty", return_value=False), \
                mock.patch("sys.stdout.isatty", return_value=True):
            self.assertFalse(clawmem_export._on_a_terminal())


class ErasureTests(_Base):
    def test_a_pending_erasure_refuses_the_whole_export(self):
        s = _sid()
        doomed = self._turn(s, 0, "user", "content ordered erased")
        self._erasure_marker(doomed[records.RECORD_ID_KEY])
        from memory import compact
        self.assertGreater(compact.pending_erasures(ledger.ledger_path()), 0)
        with self.assertRaises(clawmem_export.ExportRefused):
            clawmem_export.export_all(os.path.join(self.out.name, "blocked"), now=1)


class LegacyHygieneTests(_Base):
    def test_a_malformed_session_id_is_skipped_and_counted(self):
        good = _sid()
        self._turn(good, 0, "user", "kept")
        self._turn("not/a/hex/../id", 0, "user", "MALFORMEDSID content")
        dest, manifest = self._export()
        self.assertNotIn("MALFORMEDSID", self._all_conversation_text(dest))
        self.assertGreaterEqual(manifest["omission_account"]["legacy_skipped"], 1)
        # nothing escaped the conversations/ directory as a stray path
        self.assertEqual(set(os.listdir(os.path.join(dest, "conversations"))), {f"{good}.jsonl"})

    def test_missing_speaker_and_missing_ts_records_are_skipped(self):
        good = _sid()
        self._turn(good, 0, "user", "kept")
        self._append({"v": 1, "kind": records.AMBIENT_CAPTURE_KIND,
                      records.RECORD_ID_KEY: records.new_record_id(), "session_id": _sid(), "seq": 0,
                      "ts": 1000, "text": "MISSINGSPEAKER"})                      # no speaker
        self._append({"v": 1, "kind": records.AMBIENT_CAPTURE_KIND,
                      records.RECORD_ID_KEY: records.new_record_id(), "session_id": _sid(), "seq": 0,
                      "speaker": "user", "text": "MISSINGTS"})                    # no ts
        dest, manifest = self._export()
        blob = self._all_conversation_text(dest)
        self.assertNotIn("MISSINGSPEAKER", blob)
        self.assertNotIn("MISSINGTS", blob)
        self.assertGreaterEqual(manifest["omission_account"]["legacy_skipped"], 2)


class JoinAndFormatTests(_Base):
    def test_join_matches_recall_join_on_shared_fields(self):
        s = _sid()
        turns = [
            self._turn(s, 0, "user", "chunk A part 1 ", ts=10),
            self._turn(s, 0, "user", "part 2", ts=10),           # same seq+speaker -> one message
            self._turn(s, 1, "assistant", "answer", ts=11),
            self._turn(s, 2, "user", "second question", ts=12),
        ]
        mine = clawmem_export._join_messages(turns)
        ref = recall._join_chunks(turns)
        self.assertEqual(len(mine), len(ref))
        for m, r in zip(mine, ref):
            self.assertEqual((m["seq"], m["speaker"], m["text"], m["chunks"]),
                             (r["seq"], r["speaker"], r["text"], r["chunks"]))
        # and mine carries ts, which recall's join does not
        self.assertEqual(mine[0]["ts"], 10)
        self.assertNotIn("ts", ref[0])

    def test_the_clawmem_line_format_contract(self):
        # Pins the accepted line schema and the ClawMem commit it was verified against.
        self.assertEqual(clawmem_export.CLAWMEM_COMMIT, "ba09cb8")
        s = _sid()
        self._turn(s, 0, "user", "hello world", ts=1_700_000_000)
        dest, _ = self._export()
        line = self._conversation_lines(dest, s)[0]
        self.assertEqual(set(line.keys()), {"type", "timestamp", "message"})
        self.assertEqual(set(line["message"].keys()), {"content"})
        self.assertIn(line["type"], ("user", "assistant"))
        self.assertRegex(line["timestamp"], r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
        self.assertEqual(line["message"]["content"], "hello world")


class ManifestAndDeterminismTests(_Base):
    def _fixture(self):
        s1, s2 = _sid(), _sid()
        self._turn(s1, 0, "user", "alpha")
        self._turn(s1, 1, "assistant", "beta")
        self._turn(s2, 0, "user", "gamma")
        self._episodic(s1, "a curated note")
        self._pin("a live pin", source_session=s1)

    def test_manifest_digests_every_emitted_file(self):
        self._fixture()
        dest, manifest = self._export()
        for rel, digest in manifest["files"].items():
            self.assertTrue(os.path.exists(os.path.join(dest, rel)), rel)
            self.assertEqual(digest, clawmem_export._digest_file(os.path.join(dest, rel)))
        # the manifest accounts for conversations, curated and pins, and never digests itself
        self.assertIn("curated/notes.md", manifest["files"])
        self.assertIn("meta/pins.jsonl", manifest["files"])
        self.assertNotIn("meta/manifest.json", manifest["files"])

    def test_content_digests_are_deterministic_across_runs(self):
        self._fixture()
        _, first = self._export(name="run-a", now=111)
        _, second = self._export(name="run-b", now=222)     # different `now`, identical content
        self.assertEqual(first["files"], second["files"])

    def test_manifest_carries_the_point_in_time_caveats_and_import_path(self):
        self._fixture()
        _, manifest = self._export()
        self.assertIn("not a backup", manifest["point_in_time_caveats"])
        self.assertIn("conversations/ only", manifest["clawmem"]["import_path"])


if __name__ == "__main__":
    unittest.main()
