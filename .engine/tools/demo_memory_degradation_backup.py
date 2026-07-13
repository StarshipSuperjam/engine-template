#!/usr/bin/env python3
"""Behavioral demo for #397 — the memory-degradation notice (U09) + the backup-consent choice (U10). It drives the
REAL boot / ledger_health / backup_vault surfaces; only GitHub is stubbed (backup_vault's own `_FakeVault`), and the
ledger cabinet + repo root are throwaway temp dirs, so the real ledger and committed pointer are never touched.

  (U09) OFFLINE NOTICE: `ledger_health.detect_recall_offline` is True for a present-but-unreadable ledger and False
      for a healthy or empty one; `boot.render_dashboard` renders the plain "I couldn't open your saved memory …
      restore it from your backup" line for that signal and stays silent otherwise. An unreadable ledger and the
      "N unreadable lines" rot are mutually exclusive (the render precedence rests on that).
  (U10) BACKUP CONSENT: the `disclosure` verb names the destination + its must-stay-private requirement with NO
      side effect; a flagged `setup` emits that disclosure and creates the chosen PRIVATE repo ONLY on --consent y,
      and creates nothing on --consent n.

Every case asserts; the self-check at the end is the falsification (a regression in the detector, the render, or the
consent gate returns 1).

Run:  uv run --directory .engine --frozen -- python tools/demo_memory_degradation_backup.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import boot                                   # noqa: E402 — the real renderer under test
import validate                               # noqa: E402
from memory import backup_vault as bv         # noqa: E402
from memory import ledger, ledger_health      # noqa: E402

# A complete, valid signals dict for the pure renderer (mirrors test_boot._SIGNALS; the demo retires at first-run).
_BASE_SIGNALS = {
    "state": {"schema_version": 1, "standing_situation": {}, "integration_debt": {}},
    "refused": False, "gate": "on", "reason": None, "finding_count": 0, "register": "",
    "finding_fingerprint": None, "debt_count": 0, "debt_as_of": None, "att_lines": [],
    "att_degraded": [], "shipped": [], "stance": "Exploring", "strand": None,
    "behind_origin": None, "off_main": None, "pr_conflict": None, "restore_offer": None,
    "migration_revert": None, "audit_stale": None, "live_standing": None, "neighborhood": None,
    "map_rebuilt": False, "map_corrupt": False, "ledger_malformed": None, "migration_stalled": False,
    "recall_offline": False,
}


def _dash(**over) -> str:
    return boot.render_dashboard(dict(_BASE_SIGNALS, **over))


def _run(argv) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        bv.main(argv)
    return buf.getvalue()


def _u09() -> bool:
    with tempfile.TemporaryDirectory() as cab:
        os.environ["ENGINE_MEMORY_DIR"] = cab
        try:
            lp = ledger.ledger_path()
            os.makedirs(os.path.dirname(lp), exist_ok=True)
            missing = ledger_health.detect_recall_offline()             # no ledger yet -> "no memories yet", not offline
            with open(lp, "w", encoding="utf-8") as fh:
                fh.write('{"kind":"episodic","text":"hi"}\n')
            healthy = ledger_health.detect_recall_offline()             # readable -> not offline
            os.remove(lp)
            os.mkdir(lp)                                                # present but unopenable -> offline
            offline = ledger_health.detect_recall_offline()
            malformed_on_unopenable = ledger_health.detect_ledger_malformed()   # None -> the two never co-fire
            os.rmdir(lp)
        finally:
            os.environ.pop("ENGINE_MEMORY_DIR", None)
    detect_ok = (missing is False and healthy is False and offline is True and malformed_on_unopenable is None)

    off_line = "I couldn't open your saved memory"
    rendered = off_line in _dash(recall_offline=True) and "ask me to restore" in _dash(recall_offline=True)
    silent = off_line not in _dash(recall_offline=False)
    exclusive = (off_line in _dash(recall_offline=True, ledger_malformed=None)
                 and off_line not in _dash(recall_offline=False, ledger_malformed=2)
                 and "unreadable line" in _dash(recall_offline=False, ledger_malformed=2))
    good = detect_ok and rendered and silent and exclusive
    print(f"  (U09) offline detect + render -> {'ok' if good else 'REGRESSION'} "
          f"(missing={missing}, healthy={healthy}, offline={offline}, rendered={rendered}, silent={silent}, "
          f"exclusive={exclusive})")
    return good


def _u10() -> bool:
    root = tempfile.TemporaryDirectory()
    cab = tempfile.TemporaryDirectory()
    old_root, old_engine, old_slug, old_gh = validate.ROOT, getattr(validate, "ENGINE_DIR", None), bv._project_slug, bv._gh
    try:
        validate.ROOT = root.name
        validate.ENGINE_DIR = os.path.join(root.name, ".engine")
        os.makedirs(validate.ENGINE_DIR, exist_ok=True)
        with open(os.path.join(validate.ENGINE_DIR, "engine.json"), "w", encoding="utf-8") as fh:
            json.dump({"engine_release": "1.2.3"}, fh)
        os.environ["ENGINE_MEMORY_DIR"] = cab.name
        bv._project_slug = lambda: "demo-org/demo-project"

        choice = _run(["disclosure"])
        consent = _run(["disclosure", "--scope", "shared"])
        disclosure_ok = ("SHARED BACKUP" in choice and "SEPARATE BACKUP" in choice
                         and "engine-memory-vault" in consent
                         and "Nothing leaves your computer until you say yes" in consent
                         and "private" in consent.lower())

        fake_no = bv._FakeVault()
        bv._gh = lambda transport=None: bv._Boundary(fake_no.transport)
        out_no = _run(["setup", "--scope", "shared", "--consent", "n"])
        declined_ok = ("Nothing leaves your computer until you say yes" in out_no
                       and fake_no.created == [] and bv.read_pointer() is None)

        fake_yes = bv._FakeVault()
        bv._gh = lambda transport=None: bv._Boundary(fake_yes.transport)
        out_yes = _run(["setup", "--scope", "per-project", "--consent", "y"])
        pointer = bv.read_pointer()
        created_ok = ("Nothing leaves your computer until you say yes" in out_yes
                      and len(fake_yes.created) == 1 and pointer is not None
                      and pointer["repo"] != "engine-memory-vault")           # per-project, not the shared vault
    finally:
        validate.ROOT = old_root
        if old_engine is not None:
            validate.ENGINE_DIR = old_engine
        os.environ.pop("ENGINE_MEMORY_DIR", None)
        bv._project_slug = old_slug
        bv._gh = old_gh
        root.cleanup()
        cab.cleanup()

    good = disclosure_ok and declined_ok and created_ok
    print(f"  (U10) disclosure + consent    -> {'ok' if good else 'REGRESSION'} "
          f"(disclosure={disclosure_ok}, declined_no_create={declined_ok}, yes_creates={created_ok})")
    return good


def demo() -> int:
    print("memory degradation notice (#397 U09) + backup consent (#397 U10) — driving the REAL surfaces\n")
    ok = _u09()
    ok = _u10() and ok
    print("\nself-check:", "PASS" if ok else "FAIL — the memory-degradation / backup-consent slice regressed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(demo())
