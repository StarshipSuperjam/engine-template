"""test_restore_vault.py — memory's backup vault, the RESTORE path (slice 6b).

The REAL restore logic runs fully offline behind the in-module `_FakeVault` (the backup_vault precedent) — only
GitHub is faked. Each test redirects a throwaway ledger cabinet (ENGINE_MEMORY_DIR) AND a throwaway repo root
(validate.ROOT) so neither the real ledger nor the real committed pointer is ever touched. The round-trip pushes
through `backup_vault` and reads back through `restore_vault`, so the two halves are proven against each other.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # .engine/tools
import validate  # noqa: E402
from memory import backup_vault as bv  # noqa: E402
from memory import index  # noqa: E402
from memory import ledger  # noqa: E402
from memory import restore_vault as rv  # noqa: E402


def _rb(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


class _Base(unittest.TestCase):
    def setUp(self):
        self._root = tempfile.TemporaryDirectory()
        self._cab = tempfile.TemporaryDirectory()
        self._old_root = validate.ROOT
        self._old_engine = getattr(validate, "ENGINE_DIR", None)
        validate.ROOT = self._root.name
        validate.ENGINE_DIR = os.path.join(self._root.name, ".engine")
        os.makedirs(validate.ENGINE_DIR, exist_ok=True)
        with open(os.path.join(validate.ENGINE_DIR, "engine.json"), "w", encoding="utf-8") as fh:
            json.dump({"engine_release": "1.2.3"}, fh)
        os.environ["ENGINE_MEMORY_DIR"] = self._cab.name
        self._old_slug = bv._project_slug
        bv._project_slug = lambda: "test-org/test-project"          # hermetic project slug

    def tearDown(self):
        validate.ROOT = self._old_root
        if self._old_engine is not None:
            validate.ENGINE_DIR = self._old_engine
        os.environ.pop("ENGINE_MEMORY_DIR", None)
        bv._project_slug = self._old_slug
        self._root.cleanup()
        self._cab.cleanup()

    def _seed_and_backup(self, notes):
        """Plant `notes` into the throwaway ledger and back them up into a fresh _FakeVault. Returns the fake."""
        fake = bv._FakeVault()
        for n in notes:
            bv._demo_plant(n)
        res = bv.setup(transport=fake.transport, consent="y")
        self.assertTrue(res.get("ok"))
        return fake

    def _wipe_local(self):
        rv._quiet_remove(ledger.ledger_path())
        rv._quiet_remove(ledger.meta_path())


class RoundTripTests(_Base):
    def test_round_trip_restores_identical_and_searchable(self):
        fake = self._seed_and_backup(["banner ships in the spring release", "never deploy on a friday ZQXWORD"])
        original = _rb(ledger.ledger_path())
        self.assertEqual(len(index.query("zqxword").records), 1)     # searchable before
        self._wipe_local()
        self.assertTrue(rv._local_structurally_empty())
        res = rv.restore_now(transport=fake.transport, consent="y", github=None)
        self.assertTrue(res["ok"])
        self.assertTrue(res["restored"])
        self.assertEqual(_rb(ledger.ledger_path()), original)   # byte-identical
        self.assertEqual(len(index.query("zqxword").records), 1)     # searchable again via the rebuilt index

    def test_a_large_ledger_round_trips_via_git_data(self):
        notes = [f"note number {i} " + "x" * 200 for i in range(60)]  # well past the Contents API's ~1MB-or-bust path
        fake = self._seed_and_backup(notes)
        original = _rb(ledger.ledger_path())
        self.assertGreater(len(original), 12_000)
        self._wipe_local()
        res = rv.restore_now(transport=fake.transport, consent="y", github=None)
        self.assertTrue(res["ok"])
        self.assertEqual(_rb(ledger.ledger_path()), original)


class IntegrityTests(_Base):
    def test_fetch_blob_rejects_a_sha_mismatch_but_accepts_the_real_id(self):
        def bad(method, path, body=None):
            return 200, {"sha": "deadbeef", "content": base64.b64encode(b"hello").decode("ascii"),
                         "encoding": "base64"}
        self.assertIsNone(rv._fetch_blob(bv._Boundary(bad), "o", "r", "deadbeef"))   # bytes don't hash to the id
        real = bv._git_blob_sha1(b"hello")

        def good(method, path, body=None):
            return 200, {"sha": real, "content": base64.b64encode(b"hello").decode("ascii"),
                         "encoding": "base64", "size": 5}
        self.assertEqual(rv._fetch_blob(bv._Boundary(good), "o", "r", real), b"hello")

    def test_a_torn_backup_is_rejected_and_nothing_is_swapped(self):
        bv._demo_plant("keep me safe")                              # a good local ledger that must NOT be clobbered
        before = _rb(ledger.ledger_path())
        torn = b'{"kind":"a","text":"ok"}\n{"kind":"b","text":"tor'   # final line lacks a newline => torn
        orig = rv.fetch_snapshot
        rv.fetch_snapshot = lambda **k: {"ok": True, "error": None, "ledger_bytes": torn,
                                         "manifest": {"ledger-version": 1, "ledger-generation": 0,
                                                      "timestamp": "t", "engine-version": "x"},
                                         "owner": "o", "repo": "r", "namespace": "n"}
        try:
            res = rv.restore_now(consent="y", override=True, github=None)   # override past the resurrection guard
        finally:
            rv.fetch_snapshot = orig
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "corrupt")
        self.assertEqual(_rb(ledger.ledger_path()), before)  # the good local ledger survived

    def test_format_version_mismatch_is_declined_in_plain_words(self):
        orig = rv.fetch_snapshot
        rv.fetch_snapshot = lambda **k: {"ok": True, "error": None, "ledger_bytes": b'{"kind":"x"}\n',
                                         "manifest": {"ledger-version": 2, "ledger-generation": 0,
                                                      "timestamp": "t", "engine-version": "x"},
                                         "owner": "o", "repo": "r", "namespace": "n"}
        try:
            res = rv.restore_now(consent="y", github=None)
        finally:
            rv.fetch_snapshot = orig
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "version-mismatch")
        for banned in ("http", "git", "ledger", "index", "blob"):
            self.assertNotIn(banned, res["message"].lower())


class ResurrectionTests(_Base):
    def test_an_older_backup_is_surfaced_and_declined_but_fresh_proceeds(self):
        fake = self._seed_and_backup(["alpha note", "beta note"])  # local non-empty; backup generation is 0
        ledger.set_generation(10)                                  # pretend this machine compacted/erased since
        before = _rb(ledger.ledger_path())
        calls = []
        orig = rv.surface_resurrection
        rv.surface_resurrection = lambda *a, **k: calls.append((a, k))
        try:
            res = rv.restore_now(transport=fake.transport, consent="y")
        finally:
            rv.surface_resurrection = orig
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "resurrection")
        self.assertEqual(_rb(ledger.ledger_path()), before)  # untouched
        self.assertTrue(calls)                                              # surfaced through the open-findings path
        # A fresh/empty local is the normal recovery case — it proceeds.
        self._wipe_local()
        res2 = rv.restore_now(transport=fake.transport, consent="y", github=None)
        self.assertTrue(res2["ok"])

    def test_a_non_empty_ledger_with_an_unknown_generation_is_treated_as_resurrection(self):
        fake = self._seed_and_backup(["only note"])
        rv._quiet_remove(ledger.meta_path())                       # wipe the sidecar -> generation UNKNOWN
        self.assertFalse(rv._generation_known())
        res = rv.restore_now(transport=fake.transport, consent="y", github=None)
        self.assertEqual(res["error"], "resurrection")             # can't prove it's not a resurrection -> decline

    def test_an_explicit_override_restores_an_older_backup(self):
        fake = self._seed_and_backup(["alpha"])
        ledger.set_generation(10)
        res = rv.restore_now(transport=fake.transport, consent="y", override=True, github=None)
        self.assertTrue(res["ok"])
        self.assertTrue(res["restored"])


class OfferAndConsentTests(_Base):
    def test_offer_only_when_a_backup_is_configured_and_local_is_empty(self):
        self.assertIsNone(rv.detect_restore_offer())               # unconfigured + empty -> no offer
        fake = self._seed_and_backup(["a note"])
        self.assertIsNone(rv.detect_restore_offer())               # configured but populated -> no offer
        rv._quiet_remove(ledger.ledger_path())
        self.assertTrue(rv.detect_restore_offer())                 # configured + empty -> offer
        _ = fake

    def test_consent_no_leaves_local_unchanged(self):
        fake = self._seed_and_backup(["note one"])
        self._wipe_local()
        res = rv.restore_now(transport=fake.transport, consent="n", github=None)
        self.assertTrue(res.get("declined"))
        self.assertTrue(rv._local_structurally_empty())            # nothing was restored

    def test_consent_prompt_names_the_overwrite_flatly(self):
        prompt = rv._restore_consent_prompt(42, 40)
        self.assertIn("42", prompt)
        self.assertIn("40", prompt)
        self.assertIn("will be gone", prompt)


class Floor4Tests(_Base):
    def test_not_configured_is_a_plain_message_not_a_raise(self):
        res = rv.restore_now(transport=bv._FakeVault().transport, consent="y", github=None)
        self.assertEqual(res["error"], "not-configured")
        self.assertFalse(res["restored"])

    def test_unreachable_fetch_is_floor4_and_never_raises(self):
        self._seed_and_backup(["x"])                               # configures the pointer
        res = rv.restore_now(transport=lambda *a, **k: (None, None), consent="y", github=None)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "unreachable")
        for banned in ("http", "traceback", "exception", "ledger", "index"):
            self.assertNotIn(banned, res["message"].lower())


class ResurrectionMessageLeakTests(unittest.TestCase):
    def test_the_resurrection_finding_carries_no_internals(self):
        finding = rv._resurrection_finding()
        text = (json.dumps(finding)).lower()
        for banned in ("generation", "ledger", "index", "sha", "http", "namespace"):
            self.assertNotIn(banned, text)


class DemoSelfCheckTests(_Base):
    def test_offline_demo_self_check_passes(self):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = rv._demo()
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
