"""test_backup_vault.py — memory's backup vault, EXPORT path (slice 6a).

The REAL backup logic runs fully offline behind the module's own injected `_FakeVault` transport (the
erasure_proposer/_FakeGH precedent) — only GitHub is faked. Each test redirects a throwaway ledger cabinet
(ENGINE_MEMORY_DIR) AND a throwaway repo root (validate.ROOT) so neither the real ledger nor the real
committed pointer is ever touched.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # .engine/tools
import validate  # noqa: E402
from memory import backup_vault as bv  # noqa: E402
from memory import ledger  # noqa: E402


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
        bv._project_slug = lambda: "test-org/test-project"   # hermetic project slug

    def tearDown(self):
        validate.ROOT = self._old_root
        if self._old_engine is not None:
            validate.ENGINE_DIR = self._old_engine
        os.environ.pop("ENGINE_MEMORY_DIR", None)
        bv._project_slug = self._old_slug
        self._root.cleanup()
        self._cab.cleanup()


class ManifestTests(_Base):
    def test_manifest_has_exactly_the_four_locked_keys(self):
        ledger.append({"kind": "turn-delta", "text": "x"})
        m = bv.build_manifest(ledger_path=ledger.ledger_path())
        self.assertEqual(set(m), {"ledger-version", "ledger-generation", "timestamp", "engine-version"})
        self.assertEqual(m["ledger-version"], ledger.LEDGER_FORMAT_VERSION)
        self.assertEqual(m["engine-version"], "1.2.3")

    def test_manifest_generation_is_populated_live_and_tracks_bump(self):
        self.assertEqual(bv.build_manifest(ledger_path=ledger.ledger_path())["ledger-generation"], 0)
        ledger.bump_generation()
        self.assertEqual(bv.build_manifest(ledger_path=ledger.ledger_path())["ledger-generation"], 1)


class SetupTests(_Base):
    def test_consent_yes_creates_private_repo_and_writes_pointer(self):
        fake = bv._FakeVault()
        res = bv.setup(transport=fake.transport, consent="y")
        self.assertTrue(res["ok"])
        self.assertTrue(res.get("created"))
        self.assertEqual(len(fake.created), 1)
        self.assertFalse(fake.deleted)
        p = bv.read_pointer()
        self.assertIsNotNone(p)
        self.assertTrue(bv._setup_done())
        self.assertEqual(p["repo"], res["repo"])
        self.assertEqual(p["namespace"], "test-project")
        for k in ("owner", "repo", "branch", "namespace", "created_at"):
            self.assertTrue(p[k])

    def test_consent_no_creates_nothing(self):
        fake = bv._FakeVault()
        res = bv.setup(transport=fake.transport, consent="n")
        self.assertTrue(res.get("declined"))
        self.assertEqual(fake.created, [])
        self.assertIsNone(bv.read_pointer())
        self.assertFalse(bv._setup_done())

    def test_missing_repo_scope_discloses_and_creates_nothing(self):
        fake = bv._FakeVault(no_scope=True)
        res = bv.setup(transport=fake.transport, consent="y")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "no-scope")
        self.assertIn("gh auth refresh -s repo", res["message"])
        self.assertIsNone(bv.read_pointer())
        self.assertEqual(fake.created, [])

    def test_wrongly_public_create_is_deleted_and_disclosed(self):
        fake = bv._FakeVault(private=False)   # the create succeeds, but the verify GET reads it as public
        res = bv.setup(transport=fake.transport, consent="y")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "not-private")
        self.assertTrue(fake.deleted)                                  # the wrongly-public repo was removed
        self.assertIsNone(bv.read_pointer())
        for banned in ("http", "git", "status", "404", "403"):
            self.assertNotIn(banned, res["message"].lower())            # never a git/HTTP error


class PushTests(_Base):
    def test_ledger_pushed_via_git_data_not_contents(self):
        ledger.append({"kind": "turn-delta", "text": "hello"})
        fake = bv._FakeVault()
        bv.setup(transport=fake.transport, consent="y")              # setup pushes the first copy
        self.assertFalse(fake.pushed_ledger_via_contents)            # the ledger NEVER goes via the 1MB Contents API
        self.assertTrue(fake.blobs)                                  # it went via Git Data blobs

    def test_throttle_gates_on_last_success(self):
        self.assertTrue(bv._should_push(10_000))                    # no state -> push now
        bv._record_state(now=100_000, success=True, privacy_ok=True)
        self.assertFalse(bv._should_push(100_000 + 3600))           # 1h after a success -> skip
        self.assertTrue(bv._should_push(100_000 + 25 * 3600))       # >24h after success -> push
        self.assertTrue(bv._should_push(50_000))                    # a FUTURE last-success -> push now (never stuck off)
        bv._record_state(now=100_000 + 7200, success=False, privacy_ok=True)
        self.assertFalse(bv._should_push(100_000 + 3 * 3600))       # a FAILED push did not advance the throttle key

    def test_push_now_requires_setup(self):
        res = bv.push_now(transport=bv._FakeVault().transport)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "not-configured")


class SessionStartTests(_Base):
    def test_silent_no_op_and_no_network_before_setup(self):
        calls = {"n": 0}
        orig = bv.push_now
        bv.push_now = lambda **k: calls.__setitem__("n", calls["n"] + 1) or {"ok": True}
        try:
            decision = bv._session_start_handler({})
        finally:
            bv.push_now = orig
        self.assertEqual(decision, {"action": "proceed"})
        self.assertEqual(calls["n"], 0)                              # never reached the push (no pointer)

    def test_pushes_after_setup_then_silent_within_cooldown(self):
        fake = bv._FakeVault()
        bv.setup(transport=fake.transport, consent="y")             # records a success at ~now
        orig = bv._gh
        bv._gh = lambda transport=None: bv._Boundary(fake.transport)
        try:
            within = bv._session_start_handler({})                  # within cooldown -> no push
            self.assertEqual(within, {"action": "proceed"})
            ref_before = dict(fake.refs)
            elapsed = bv._session_start_handler({}, now=int(time.time()) + 100 * 3600)
            self.assertEqual(elapsed, {"action": "proceed"})        # a clean success injects nothing
            self.assertNotEqual(fake.refs, ref_before)              # a real push DID advance the vault branch
        finally:
            bv._gh = orig

    def test_privacy_flip_surfaced_once_then_silent(self):
        fake = bv._FakeVault()
        bv.setup(transport=fake.transport, consent="y")
        fake.private = False
        orig = bv._gh
        bv._gh = lambda transport=None: bv._Boundary(fake.transport)
        try:
            first = bv._session_start_handler({}, now=int(time.time()) + 100 * 3600)
            self.assertEqual(first["action"], "inject")
            self.assertIn("PUBLIC", first["context"])
            self.assertIn("Private", first["context"])
            second = bv._session_start_handler({}, now=int(time.time()) + 200 * 3600)
            self.assertEqual(second, {"action": "proceed"})         # already reported -> silent (no nag)
        finally:
            bv._gh = orig

    def test_push_failure_discloses_floor4_and_never_raises(self):
        fake = bv._FakeVault()
        bv.setup(transport=fake.transport, consent="y")
        fake.private = True
        fake.fail_blob = True                                       # the repo is reachable + private, the upload fails
        orig = bv._gh
        bv._gh = lambda transport=None: bv._Boundary(fake.transport)
        try:
            decision = bv._session_start_handler({}, now=int(time.time()) + 100 * 3600)
        finally:
            bv._gh = orig
        self.assertEqual(decision["action"], "inject")
        ctx = decision["context"]
        self.assertIn("back up memory now", ctx)
        for banned in ("http", "git", "422", "status", "traceback", "exception"):
            self.assertNotIn(banned, ctx.lower())


class PointerTests(_Base):
    def test_partial_pointer_reads_as_unconfigured(self):
        path = bv._pointer_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"schema_version": 1, "owner": "o", "branch": "main", "namespace": "n"}, fh)  # no repo
        self.assertIsNone(bv.read_pointer())
        self.assertFalse(bv._setup_done())

    def test_placeholder_reads_as_unconfigured(self):
        path = bv._pointer_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"schema_version": 1, "configured": False}, fh)
        self.assertIsNone(bv.read_pointer())

    def test_pointer_is_content_free(self):
        ledger.append({"kind": "turn-delta", "text": "RUMBLEDETHUMPS a secret private note"})
        fake = bv._FakeVault()
        bv.setup(transport=fake.transport, consent="y")
        blob = json.dumps(bv.read_pointer()).lower()
        self.assertNotIn("rumbledethumps", blob)
        self.assertNotIn("secret", blob)


class LeakGuardTests(unittest.TestCase):
    def test_commit_message_and_readme_carry_no_ledger_content(self):
        word = "rumbledethumps"
        self.assertNotIn(word, bv._COMMIT_MESSAGE.lower())
        self.assertNotIn(word, bv._readme_text("test-project").lower())


class DemoGuardTests(unittest.TestCase):
    def test_safe_demo_delete_only_targets_disposable_names(self):
        self.assertTrue(bv._safe_demo_delete("test-project-memvault-demo-abcd1234", "test-project"))
        self.assertFalse(bv._safe_demo_delete("test-project", "test-project"))                       # the project repo
        self.assertFalse(bv._safe_demo_delete("test-project-engine-memory-backup", "test-project"))  # the real vault
        self.assertFalse(bv._safe_demo_delete("some-random-repo", "test-project"))                   # no marker


class DemoSelfCheckTests(unittest.TestCase):
    def test_offline_demo_self_check_passes(self):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            rc = bv._demo()
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
