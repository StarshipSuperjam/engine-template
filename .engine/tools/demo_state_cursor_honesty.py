#!/usr/bin/env python3
"""Behavioral demo for the state-cursor cold-start honesty slice (issue #398). It drives the REAL boot +
instantiator surfaces (only git origin / GitHub are stubbed) across the three findings:

  (U15a) VALIDATE ON READ: read_state accepts a schema-valid cursor and REFUSES a schema_version-1 cursor
      whose inner shape is broken — never rendering a confident "all clear" over a malformed cursor.
  (U15b) DURABLE FINDING: on the REAL SessionStart path (use_ledger=True) a refused cursor spools ONE benign
      "boot/refused-cursor" finding the #412 drain later promotes; the read-only status/debug path
      (use_ledger=False) spools nothing.
  (U16) FIRST-RUN GENESIS: _seed_state resets a generated repo's traveled construction cursor to genesis and
      PRESERVES a cursor that already names the repo's own origin.

Nothing is faked but git origin. Every case asserts; the self-check at the end is the falsification (a
regression in the refusal, the emit gating, or the reset returns 1).

Run:  uv run --directory .engine --frozen -- python tools/demo_state_cursor_honesty.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import boot                       # noqa: E402  (the real surface under test)
import instantiator as inst       # noqa: E402  (the real surface under test)

_GOOD = {"schema_version": 1, "standing_situation": {"milestone": None, "phase": None},
         "integration_debt": {"open_count": 0, "as_of": None, "register": None}}


def _write(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(obj))


def _lines(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return fh.read().splitlines()


def _load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def demo() -> int:
    print("state-cursor cold-start honesty (#398) — driving the REAL boot + instantiator surfaces\n")
    ok = True

    # (U15a) validate on read: valid accepted, a version-1-but-malformed cursor refused
    with tempfile.TemporaryDirectory() as td:
        good = os.path.join(td, "good.json"); _write(good, _GOOD)
        bad = os.path.join(td, "bad.json"); _write(bad, {"schema_version": 1})
        with mock.patch.object(boot, "STATE_PATH", good):
            _s, refused_good = boot.read_state()
        with mock.patch.object(boot, "STATE_PATH", bad):
            _s, refused_bad = boot.read_state()
        good = (not refused_good) and refused_bad
        print(f"  (U15a) validate on read     -> {'valid accepted, malformed refused' if good else 'REGRESSION'} "
              f"(valid_refused={refused_good}, malformed_refused={refused_bad})")
        ok = ok and good

    # (U15b) durable finding, gated to the real SessionStart path only
    with tempfile.TemporaryDirectory() as td:
        spool = os.path.join(td, "findings-inbox.ndjson")
        with mock.patch.object(boot, "repo_slug", return_value=None), \
                mock.patch.object(boot, "gh_token", return_value=None), \
                mock.patch.object(boot.attention, "derive_focus", return_value=([], 0)), \
                mock.patch.object(boot.boot_slice, "read", return_value=None), \
                mock.patch.object(boot, "recently_shipped", return_value=[]), \
                mock.patch.object(boot.telemetry, "INBOX_SPOOL_PATH", spool), \
                mock.patch.dict(os.environ, {boot.boot_alarm_ledger.ENV_DIR: td}), \
                mock.patch.object(boot, "read_state", return_value=(None, True)):
            boot.assemble_pack(use_ledger=True)     # real SessionStart -> emits once
            after_real = len(_lines(spool))
            boot.assemble_pack(use_ledger=False)    # read-only status / debug view -> must not emit
            after_readonly = len(_lines(spool))
        sid = json.loads(_lines(spool)[0])["source_id"] if after_real else None
        good = after_real == 1 and after_readonly == 1 and sid == "boot/refused-cursor"
        print(f"  (U15b) durable finding      -> {'one benign finding, gated to SessionStart' if good else 'REGRESSION'} "
              f"(after_real={after_real}, after_readonly={after_readonly}, sid={sid})")
        ok = ok and good

    # (U16) first-run genesis reset: foreign cursor reset, own-origin cursor preserved
    with tempfile.TemporaryDirectory() as td, inst._redirect_root(td), \
            mock.patch.object(inst.boot, "repo_slug", return_value="acme/proj"):
        state_dir = os.path.join(td, ".engine", "state"); os.makedirs(state_dir)
        state_file = os.path.join(state_dir, "state.json")
        foreign = {"schema_version": 1, "standing_situation": {"milestone": "wp", "phase": "workshop #449"},
                   "integration_debt": {"open_count": 31, "as_of": None,
                                        "register": "https://github.com/StarshipSuperjam/engine-template/issues"}}
        _write(state_file, foreign)
        out_reset = inst._seed_state(lambda t: None, inst.load_copy())
        reset_cursor = _load(state_file)
        own = dict(foreign, integration_debt=dict(foreign["integration_debt"],
                                                  register="https://github.com/acme/proj/issues"))
        _write(state_file, own)
        out_preserve = inst._seed_state(lambda t: None, inst.load_copy())
        preserved = _load(state_file)
    good = (out_reset == "reseeded" and reset_cursor == inst._GENESIS_CURSOR
            and out_preserve == "present" and preserved["integration_debt"]["open_count"] == 31)
    print(f"  (U16) first-run genesis     -> {'foreign reset, own-origin preserved' if good else 'REGRESSION'} "
          f"(reset={out_reset}, preserve={out_preserve})")
    ok = ok and good

    print("\nself-check:", "PASS" if ok else "FAIL — the state-cursor honesty slice regressed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(demo())
