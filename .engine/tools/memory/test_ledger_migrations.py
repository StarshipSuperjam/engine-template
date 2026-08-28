"""test_ledger_migrations.py — the home a restore routes through to carry an older-shaped backup forward.

The registry is empty in this version (no record-shape change has shipped), so every real resolve returns None
and the restore declines honestly. These tests pin that refuse-by-default safety AND prove the routing is a live
mechanism, not a stub: a fixture-registered step (injected in-process, never a public API) is found, ordered into
a chain, and applied. The injection is `mock.patch.dict` on the private registry — auto-restored, so nothing
leaks between tests or into production.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # .engine/tools
from memory import ledger_migrations as lm  # noqa: E402


class ResolveRefusesByDefaultTests(unittest.TestCase):
    def test_empty_registry_bridges_nothing(self):
        # The shipped state: no step exists, so a differing version can't be carried forward.
        self.assertIsNone(lm.resolve_ledger_migration(0, 1))
        self.assertIsNone(lm.resolve_ledger_migration(1, 2))

    def test_a_newer_backup_has_no_downgrade_path(self):
        with mock.patch.dict(lm._REGISTRY, {(0, 1): lambda b: b}, clear=False):
            # steps only go forward; a version-2 backup into a version-1 engine can't be walked down.
            self.assertIsNone(lm.resolve_ledger_migration(2, 1))

    def test_a_malformed_version_declines_and_never_raises(self):
        for bad in (None, "two", {"x": 1}, [1], True, 1.5):
            self.assertIsNone(lm.resolve_ledger_migration(bad, 1))

    def test_the_registry_is_empty_between_tests(self):
        # isolation: a prior test's patch must not leak.
        self.assertEqual(lm._REGISTRY, {})


class ResolveRoutesAndAppliesTests(unittest.TestCase):
    def test_a_registered_single_step_is_found_and_applied(self):
        def _to_v1(b):
            return b.replace(b'"kind":"old"', b'"kind":"new"')
        with mock.patch.dict(lm._REGISTRY, {(0, 1): _to_v1}, clear=False):
            chain = lm.resolve_ledger_migration(0, 1)
            self.assertEqual(chain, [_to_v1])
            out = lm.apply_ledger_migrations(b'{"kind":"old"}\n', chain)
            self.assertEqual(out, b'{"kind":"new"}\n')

    def test_a_multi_step_path_is_ordered_and_chained(self):
        def _a(b):
            return b + b"a"
        def _b(b):
            return b + b"b"
        with mock.patch.dict(lm._REGISTRY, {(0, 1): _a, (1, 2): _b}, clear=False):
            chain = lm.resolve_ledger_migration(0, 2)
            self.assertEqual(chain, [_a, _b])
            self.assertEqual(lm.apply_ledger_migrations(b"x", chain), b"xab")

    def test_apply_is_all_or_nothing_on_a_bad_transform(self):
        # a transform that returns something other than bytes raises, so the restore caller lands nothing.
        with self.assertRaises(TypeError):
            lm.apply_ledger_migrations(b"x", [lambda b: "not-bytes"])

    def test_apply_over_an_empty_chain_is_the_bytes_unchanged(self):
        self.assertEqual(lm.apply_ledger_migrations(b"same", []), b"same")


class PrimaryEvidenceDryRunTests(unittest.TestCase):
    def test_every_legacy_source_has_one_disposition_and_curation_is_dropped(self):
        source = [
            {"kind": "turn-delta", "id": "turn", "text": "Codex said this", "session_id": "claude-id",
             "seq": 0, "speaker": "user"},
            {"kind": "episodic", "id": "summary", "text": "a recalled summary"},
            {"kind": "pin", "id": "pin", "text": "the current choice", "pinned_via": "assistant"},
            {"kind": "consolidated", "id": "marker"},
        ]
        report = lm.classify_legacy_records(source)
        self.assertEqual(report["source_count"], 4)
        self.assertEqual(report["retained_count"], 2)
        self.assertEqual(report["transformed_count"], 0)
        self.assertEqual(report["dropped_count"], 2)
        self.assertEqual(report["unresolved_count"], 0)
        self.assertFalse(report["mutation_blocked"])
        self.assertEqual([item["disposition"] for item in report["items"]],
                         ["retain", "drop", "retain", "drop"])
        turn = report["items"][0]["result"]
        self.assertEqual(turn["provider"], "unknown")  # no content/id inference
        self.assertEqual(turn["authority"], "recalled-evidence")
        self.assertEqual(turn["text"], "Codex said this")
        pin = report["items"][2]["result"]
        self.assertEqual(pin["authority"], "operator-intent")

    def test_chunked_task_result_is_one_terminal_result_and_wrapper_only_is_dropped(self):
        source = [
            {"kind": "turn-delta", "id": "a", "session_id": "S", "seq": 7, "speaker": "user",
             "tags": ["injected"], "text": "<task-notification>\n<task-id>T</task-id>\n<status>failed</status>\n<result>first"},
            {"kind": "turn-delta", "id": "b", "session_id": "S", "seq": 7, "speaker": "user",
             "tags": ["injected"], "text": "second</result>\n</task-notification>"},
            {"kind": "turn-delta", "id": "c", "session_id": "S", "seq": 8, "speaker": "user",
             "tags": ["injected"], "text": "<task-notification><task-id>U</task-id><status>completed</status></task-notification>"},
        ]
        report = lm.classify_legacy_records(source)
        self.assertEqual(report["unresolved_count"], 0)
        self.assertEqual([item["disposition"] for item in report["items"]],
                         ["transform", "transform", "drop"])
        result = report["items"][0]["result"]
        self.assertEqual(result["event"], "agent-result-failure-text")
        self.assertEqual(result["terminal"], "failure")
        self.assertEqual(result["item_id"], "T")
        self.assertEqual(result["text"], "first\nsecond")
        self.assertEqual(report["items"][1]["result_id"], result["id"])

    def test_compaction_group_drops_and_unknown_injected_fragment_refuses(self):
        source = [
            {"kind": "turn-delta", "id": "a", "session_id": "S", "seq": 4, "speaker": "user",
             "tags": ["injected"], "text": "This session is being continued from a previous conversation..."},
            {"kind": "turn-delta", "id": "b", "session_id": "S", "seq": 4, "speaker": "user",
             "tags": ["injected"], "text": "continuation chunk"},
            {"kind": "turn-delta", "id": "c", "session_id": "S", "seq": 5, "speaker": "user",
             "tags": ["injected"], "text": "orphan injected chunk"},
        ]
        report = lm.classify_legacy_records(source)
        self.assertEqual([item["disposition"] for item in report["items"]],
                         ["drop", "drop", "unresolved"])
        self.assertTrue(report["mutation_blocked"])

    def test_unknown_kind_is_inspectable_and_blocks_mutation(self):
        report = lm.classify_legacy_records([{"kind": "future-unrecognised", "text": "keep me"}])
        self.assertEqual(report["unresolved_count"], 1)
        self.assertEqual(report["unresolved_source_indexes"], [0])
        self.assertTrue(report["mutation_blocked"])
        self.assertEqual(report["items"][0]["reason"], "unknown-legacy-kind")

    def test_dry_run_is_read_only_and_reports_source_and_result_digests(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ledger.ndjson")
            raw = b'{"kind":"turn-delta","id":"t","speaker":"user","text":"hello"}\n'
            with open(path, "wb") as fh:
                fh.write(raw)
            with open(path, "rb") as fh:
                before = fh.read()
            report = lm.dry_run_legacy_ledger(path=path)
            with open(path, "rb") as fh:
                self.assertEqual(fh.read(), before)
        self.assertEqual(report["source_count"], 1)
        self.assertEqual(len(report["source_digest"]), 64)
        self.assertEqual(len(report["source_records_digest"]), 64)
        self.assertEqual(len(report["result_digest"]), 64)
        self.assertEqual(report["unresolved_count"], 0)


if __name__ == "__main__":
    unittest.main()
