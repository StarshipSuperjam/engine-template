"""Unit tests for forget.py — Layer-1 logical retirement of crash-duplicate consolidations (slice 4a).

The retirement is REVERSIBLE and recall-only: an orphaned crash-pass episodic is excluded from recall but
stays resident in the ledger, fully recoverable. These tests exercise the real filter (`live_records`), the
real recall paths (fast index + slow scan) through it, the read-only `duplicates` inspector, and the
build-conformance invariant that this Layer-1 module reaches NO physical-erasure path.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import capture, consolidate, forget, index, ledger, records  # noqa: E402


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

    def test_a_batchless_episodic_is_always_live(self):
        # a pre-4a episodic with no batch field — nothing to resolve, so never retired (back-compat)
        ledger.append({"v": 1, "kind": records.EPISODIC_KIND, "session_id": "S", "text": "old note", "tags": []})
        self.assertEqual(len(self._live_episodics()), 1)

    def test_an_empty_string_batch_is_treated_as_batchless_and_stays_live(self):
        # Defensive: the real write path mints a uuid, never "" — but a hand-edited / corrupt record with an
        # empty batch must be treated as batchless (always live), never mistaken for an unmarked orphan.
        ledger.append({"v": 1, "kind": records.EPISODIC_KIND, "session_id": "S", "text": "edge note",
                       "tags": [], records.BATCH_KEY: ""})
        self.assertEqual(len(self._live_episodics()), 1)

    def test_non_episodic_records_pass_through(self):
        ledger.append(capture._make_record("S", 0, "user", "a turn note"))   # turn-delta, no batch
        self._marker("S", "batch-x")                                          # a lone marker
        kinds = sorted({r.get("kind") for r in forget.live_records()})
        self.assertIn(capture.RECORD_KIND, kinds)
        self.assertIn(records.MARKER_KIND, kinds)

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
        # Layer-1 logical retirement NEVER erases (memory/README two-layer law): physical removal is reachable
        # only through Layer 2's merge-gated path. forget.py must carry no ledger-delete / erase call — a
        # build-conformance invariant pinned by source scan.
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "forget.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        for token in ("os.remove", "os.unlink", "os.truncate", "truncate(", "rmtree", "os.replace"):
            self.assertNotIn(token, src, f"forget.py must not reach physical erasure: found {token!r}")

    def test_no_layer1_file_mints_an_erasure_marker(self):
        # Layer-2 physical erasure (slice 4e) is reachable ONLY through compact's gated removal, which fires only on
        # an `operator-adjudicated-erasure` marker. Exactly TWO Layer-2 files may touch the minter call:
        # `compact.py` (the chokepoint OWNER — it DEFINES `enact_erasure` and performs the removal) and, since slice
        # 4e-ii, `erasure_observer.py` (the SANCTIONED cross-session ENACTOR — it calls the minter, but ONLY with a
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


if __name__ == "__main__":
    unittest.main()
