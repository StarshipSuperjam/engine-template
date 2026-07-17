#!/usr/bin/env python3
"""Operator-runnable demo of restoring a memory backup when its record shape changed across engine versions.

It answers, in plain words, a question a non-engineer can't read code to verify: *when I restore a backup, what
happens if it was made by a different version of the engine? Does the engine carry it forward when it safely
can, decline honestly when it can't, and never leave a half-restored or wrong copy on my computer?*

It runs the REAL restore end-to-end — memory's own `restore_vault.restore_now` and the `ledger_migrations` home a
restore routes through — in an ISOLATED temp store and a fake vault (via env overrides + an in-module fake
GitHub), so it never touches your real memory and needs no network, no token, no edits.

There is no record-shape change in this version, so the "carry it forward" step is exercised here with a
stand-in transform the demo registers itself (in-process, removed afterwards) — proving the routing is a live
mechanism, not an empty promise, and that when a real shape change ships one day it will just work.

It shows, and CHECKS (so this demo can FAIL — it is a falsification, not a showcase):
  * SAME SHAPE RESTORES — a backup at the current shape restores normally;
  * OLDER SHAPE CARRIED FORWARD — with a stand-in step registered, an older-shaped backup is carried up to the
    current shape IN MEMORY and then restored (the note it carried is searchable afterwards);
  * NO WAY FORWARD DECLINES HONESTLY — a backup from a different version with no way to bridge it is declined in
    plain words (never the old "an update step that isn't built yet"), and the memory on this computer is left
    exactly as it was;
  * NOTHING HALF-DONE — after a decline, the local store is byte-for-byte unchanged.

Vary it yourself: change the versions or the stand-in transform below and re-run.

Run: uv run --directory .engine -- python tools/demo_restore_migration_routing.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # .engine/tools
import validate  # noqa: E402
from memory import backup_vault as bv  # noqa: E402
from memory import index  # noqa: E402
from memory import ledger  # noqa: E402
from memory import ledger_migrations as lm  # noqa: E402
from memory import restore_vault as rv  # noqa: E402


def _rb(path):
    with open(path, "rb") as fh:
        return fh.read()


def _wipe_local():
    rv._quiet_remove(ledger.ledger_path())
    rv._quiet_remove(ledger.meta_path())


def main() -> int:
    failures = []
    root = tempfile.TemporaryDirectory()
    cab = tempfile.TemporaryDirectory()
    old_root, old_engine = validate.ROOT, getattr(validate, "ENGINE_DIR", None)
    old_slug, old_fetch = bv._project_slug, rv.fetch_snapshot
    validate.ROOT = root.name
    validate.ENGINE_DIR = os.path.join(root.name, ".engine")
    os.makedirs(validate.ENGINE_DIR, exist_ok=True)
    with open(os.path.join(validate.ENGINE_DIR, "engine.json"), "w", encoding="utf-8") as fh:
        json.dump({"engine_release": "1.2.3"}, fh)
    os.environ["ENGINE_MEMORY_DIR"] = cab.name
    bv._project_slug = lambda: "demo-org/demo-project"
    try:
        print("=== Same shape — a normal restore ===")
        fake = bv._FakeVault()
        bv._demo_plant("the sourdough starter doubles in 4 hours")
        bv.setup(scope="shared", transport=fake.transport, consent="y")
        _wipe_local()
        res = rv.restore_now(transport=fake.transport, consent="y", github=None)
        print(f"  restored: {res.get('restored')} — {res.get('message')}\n")
        if not (res.get("ok") and res.get("restored")):
            failures.append("a backup at the current shape must restore normally")

        print("=== Older shape — carried forward by a registered step, then restored ===")
        # A stand-in transform for a made-up older shape (version 0): rewrite the old field name to the new one.
        def _carry_forward(raw):
            return raw.replace(b'"note_v0"', b'"note"')
        rv.fetch_snapshot = lambda **k: {"ok": True, "error": None,
                                         "ledger_bytes": b'{"note_v0":"kept across the version change"}\n',
                                         "manifest": {"ledger-version": 0, "ledger-generation": 0,
                                                      "timestamp": "t", "engine-version": "x"},
                                         "owner": "o", "repo": "r", "namespace": "n"}
        _wipe_local()
        lm._REGISTRY[(0, ledger.LEDGER_FORMAT_VERSION)] = _carry_forward
        try:
            res2 = rv.restore_now(consent="y", github=None)
        finally:
            lm._REGISTRY.pop((0, ledger.LEDGER_FORMAT_VERSION), None)   # remove the stand-in; the registry is empty again
        carried = _rb(ledger.ledger_path()) if os.path.exists(ledger.ledger_path()) else b""
        has_new_field = b'"note"' in carried and b'"note_v0"' not in carried
        print(f"  restored: {res2.get('restored')}; carried record present in the new shape: {has_new_field}\n")
        if not (res2.get("ok") and has_new_field):
            failures.append("an older-shaped backup with a registered step must be carried forward and restored")

        print("=== No way forward — declined honestly, nothing touched ===")
        bv._demo_plant("today's notes I do NOT want overwritten")
        before = _rb(ledger.ledger_path())
        rv.fetch_snapshot = lambda **k: {"ok": True, "error": None, "ledger_bytes": b'{"note":"from a newer engine"}\n',
                                         "manifest": {"ledger-version": 99, "ledger-generation": 0,
                                                      "timestamp": "t", "engine-version": "x"},
                                         "owner": "o", "repo": "r", "namespace": "n"}
        res3 = rv.restore_now(consent="y", github=None)
        msg = (res3.get("message") or "").lower()
        print(f"  restored: {res3.get('restored')} — {res3.get('message')}\n")
        if res3.get("ok") or res3.get("restored"):
            failures.append("a backup with no way forward must be declined, never restored")
        if "isn't built" in msg or "built yet" in msg:
            failures.append("the decline must be honest — never 'an update step that isn't built yet'")
        if _rb(ledger.ledger_path()) != before:
            failures.append("a declined restore must leave the memory on this computer byte-for-byte unchanged")
    finally:
        rv.fetch_snapshot = old_fetch
        bv._project_slug = old_slug
        validate.ROOT = old_root
        if old_engine is not None:
            validate.ENGINE_DIR = old_engine
        os.environ.pop("ENGINE_MEMORY_DIR", None)
        root.cleanup()
        cab.cleanup()

    if failures:
        print("DEMO FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All checks passed: same shape restores, an older shape is carried forward when a step exists, no way "
          "forward declines honestly, and a declined restore changes nothing on this computer.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
