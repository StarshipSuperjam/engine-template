"""Unit tests for rescrub.py — running already-stored conversation back through the secret masking.

Two things carry the risk and both are tested here: that a turn captured WHILE the rewrite runs is not lost
with the old file, and that a rewrite which cannot verify itself never lands at all.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import capture, ledger, records, rescrub  # noqa: E402

_SECRET = "ghp_" + "a1b2c3d4e5" * 4                      # a github token shape the masker recognises


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

    def _turn(self, text, *, seq=0, session="s-1"):
        rid = records.new_record_id()
        ledger.append({"v": 1, "kind": records.AMBIENT_CAPTURE_KIND, records.RECORD_ID_KEY: rid,
                       "session_id": session, "seq": seq, "speaker": "user", "ts": 1785000000 + seq,
                       "text": text})
        return rid

    def _texts(self):
        return [r.get("text") for r in ledger.iter_records()]


class CleaningTests(_Base):
    def test_a_secret_stored_before_masking_existed_is_masked_now(self):
        self._turn(f"the token is {_SECRET} do not share it")
        self._turn("an ordinary turn with nothing sensitive in it", seq=1)
        report = rescrub.run(snapshot=False)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["changed"], 1)
        joined = " ".join(self._texts())
        self.assertNotIn(_SECRET, joined)
        self.assertIn("[redacted:github-token]", joined)
        self.assertIn("an ordinary turn", joined)          # everything else is untouched

    def test_running_it_twice_changes_nothing_the_second_time(self):
        # The masker is idempotent, which is what makes "re-scrub everything" safe — there is no boundary to
        # find and no way to know which records were already clean.
        self._turn(f"the token is {_SECRET}")
        self.assertEqual(rescrub.run(snapshot=False)["changed"], 1)
        self.assertEqual(rescrub.run(snapshot=False)["changed"], 0)

    def test_nothing_but_the_text_is_altered(self):
        rid = self._turn(f"leaking {_SECRET}")
        before = [r for r in ledger.iter_records()][0]
        rescrub.run(snapshot=False)
        after = [r for r in ledger.iter_records()][0]
        self.assertEqual(after[records.RECORD_ID_KEY], rid)
        for key in ("v", "kind", "session_id", "seq", "speaker", "ts"):
            self.assertEqual(after[key], before[key], f"{key} changed")

    def test_an_empty_store_says_so_rather_than_reporting_a_clean(self):
        self.assertEqual(rescrub.run(snapshot=False)["status"], "empty")

    def test_plan_reports_without_changing_anything(self):
        self._turn(f"the token is {_SECRET}")
        report = rescrub.plan()
        self.assertEqual((report["records"], report["would_change"]), (1, 1))
        self.assertIn(_SECRET, " ".join(self._texts()))     # still there — plan only reads


class DurabilityTests(_Base):
    def test_a_turn_captured_during_the_rewrite_is_not_lost(self):
        # THE HAZARD THIS VERB INTRODUCES. `replace_ledger` swaps the whole file by rename, so any turn
        # appended between the read and the swap goes with the old one. The lock is what stops that — this
        # asserts a concurrent writer genuinely cannot interleave, rather than trusting the ordering.
        self._turn(f"the token is {_SECRET}")
        landed = []

        def concurrent_append():
            lock = capture._acquire_lock(os.path.join(self._tmp.name, capture.LOCK_FILENAME))
            landed.append(lock)                           # None means it was correctly kept out
            if lock is not None:
                capture._release_lock(lock)

        real_read = ledger.read

        def read_then_race(*a, **k):
            out = real_read(*a, **k)
            t = threading.Thread(target=concurrent_append)
            t.start()
            t.join()
            return out

        with mock.patch.object(ledger, "read", side_effect=read_then_race):
            rescrub.run(snapshot=False)
        # The reader is reached twice — once for the ledger, once for the round-trip check — so the racer runs
        # twice. Every attempt must have been kept out.
        self.assertTrue(landed, "the race never ran, so this proved nothing")
        self.assertEqual(set(landed), {None}, "a concurrent writer got the lock during the rewrite")

    def test_a_failed_verification_leaves_the_original_untouched(self):
        self._turn(f"the token is {_SECRET}")
        before = self._texts()
        with mock.patch.object(rescrub, "_digest_of", side_effect=["expected", "something-else"]):
            with self.assertRaises(rescrub.RescrubRefused):
                rescrub.run(snapshot=False)
        self.assertEqual(self._texts(), before)            # the secret is still there — and so is everything else
        leftovers = [n for n in os.listdir(self._tmp.name) if n.startswith(rescrub._TEMP_PREFIX)]
        self.assertEqual(leftovers, [], "a temp file was left behind")

    def test_it_refuses_when_no_backup_is_configured(self):
        # The engine does not rewrite stored data it cannot first copy somewhere else.
        self._turn(f"the token is {_SECRET}")
        with mock.patch("memory.backup_vault.migration_backup_available", return_value=False):
            with self.assertRaises(rescrub.RescrubRefused) as caught:
                rescrub.run()
        self.assertIn("no backup is set up", str(caught.exception))
        self.assertIn(_SECRET, " ".join(self._texts()))

    def test_it_moves_the_index_epoch_and_not_the_content_generation(self):
        # `generation` means "content was rewritten or REMOVED", and the restore guard reads it that way: moving
        # it would make every backup taken before this refuse, telling the operator notes had been deliberately
        # removed. Nothing is removed here.
        self._turn(f"the token is {_SECRET}")
        gen, epoch = ledger.generation(), ledger.index_epoch()
        rescrub.run(snapshot=False)
        self.assertEqual(ledger.generation(), gen)
        self.assertGreater(ledger.index_epoch(), epoch)


if __name__ == "__main__":
    unittest.main()
