#!/usr/bin/env python3
"""Tests for boot_alarm_ledger — the D-269 standing-alarm presentation ledger.

These lock the behaviours a non-engineer cannot read code to verify: an UNCHANGED standing alarm collapses
(decide -> "collapse") only after a true full relay; a NEW/CHANGED one renders full with the prior value
exposed for worsening labels; a VANISHED alarm is dropped so a recurrence relays full again; the ledger is
FAIL-TOWARD-FULL on a missing/corrupt/unwritable store; shown-in-full is stamped only on a true full relay
(no suppression-by-drift); the path resolves to a stable per-instance root (not an ephemeral worktree); and
the module imports nothing from boot (a one-way boot -> ledger dependency, D-269 sweep-isolation)."""
from __future__ import annotations

import ast
import json
import os
import tempfile
import unittest
from unittest import mock

import boot_alarm_ledger as bal


def _decide(alarms, path):
    return bal.decide(alarms, path=path)


class TestCollapseDecision(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "standing-alarms.json")

    def test_first_ever_session_renders_full_then_seeds(self):
        # No ledger yet -> full (neutral), ok False; and the ledger is seeded so the NEXT session can collapse.
        out = _decide([{"key": "findings", "value": 20}], self.path)
        self.assertFalse(out["ok"])  # missing ledger -> neutral full, never a misleading "still"/"worse"
        self.assertEqual(out["results"]["findings"]["outcome"], "full")
        self.assertTrue(os.path.isfile(self.path))

    def test_unchanged_condition_collapses_after_a_full_relay(self):
        _decide([{"key": "findings", "value": 20}], self.path)             # full (seed)
        out = _decide([{"key": "findings", "value": 20}], self.path)       # same -> collapse
        self.assertTrue(out["ok"])
        self.assertEqual(out["results"]["findings"]["outcome"], "collapse")

    def test_changed_condition_relays_full_with_prior(self):
        _decide([{"key": "findings", "value": 20}], self.path)
        _decide([{"key": "findings", "value": 20}], self.path)             # collapse
        out = _decide([{"key": "findings", "value": 25}], self.path)       # changed -> full, prior exposed
        self.assertEqual(out["results"]["findings"]["outcome"], "full")
        self.assertEqual(out["results"]["findings"]["prior"], 20)          # so the renderer can say "got worse"

    def test_vanished_alarm_is_dropped_so_a_recurrence_relays_full(self):
        _decide([{"key": "findings", "value": 20}], self.path)            # full (seed)
        _decide([], self.path)                                            # the alarm is gone -> dropped
        out = _decide([{"key": "findings", "value": 20}], self.path)      # recurs -> full again (NOT collapse)
        self.assertEqual(out["results"]["findings"]["outcome"], "full")
        self.assertIsNone(out["results"]["findings"]["prior"])            # no stale baseline survived

    def test_shown_in_full_is_only_stamped_on_a_true_full_relay(self):
        # The suppression-by-drift guard: drive full -> collapse -> collapse; the baseline stays the full value,
        # so a later genuine change still relays full (a terse render never becomes the collapse baseline).
        _decide([{"key": "gate", "value": ["off", "a"]}], self.path)     # full (seed)
        c1 = _decide([{"key": "gate", "value": ["off", "a"]}], self.path)  # collapse
        c2 = _decide([{"key": "gate", "value": ["off", "a"]}], self.path)  # collapse again
        self.assertEqual(c1["results"]["gate"]["outcome"], "collapse")
        self.assertEqual(c2["results"]["gate"]["outcome"], "collapse")
        changed = _decide([{"key": "gate", "value": ["off", "b"]}], self.path)
        self.assertEqual(changed["results"]["gate"]["outcome"], "full")
        self.assertEqual(changed["results"]["gate"]["prior"], ["off", "a"])

    def test_two_independent_alarms_collapse_independently(self):
        _decide([{"key": "gate", "value": ["off", "a"]}, {"key": "findings", "value": 3}], self.path)
        out = _decide([{"key": "gate", "value": ["off", "a"]}, {"key": "findings", "value": 4}], self.path)
        self.assertEqual(out["results"]["gate"]["outcome"], "collapse")   # gate unchanged
        self.assertEqual(out["results"]["findings"]["outcome"], "full")   # findings changed
        # and the gate baseline persisted across a session that wrote a findings change
        again = _decide([{"key": "gate", "value": ["off", "a"]}, {"key": "findings", "value": 4}], self.path)
        self.assertEqual(again["results"]["gate"]["outcome"], "collapse")
        self.assertEqual(again["results"]["findings"]["outcome"], "collapse")


class TestFailTowardFull(unittest.TestCase):
    def test_corrupt_ledger_fails_to_full(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "standing-alarms.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("}{ not json")
        out = _decide([{"key": "findings", "value": 20}], path)
        self.assertFalse(out["ok"])
        self.assertEqual(out["results"]["findings"]["outcome"], "full")
        # and it self-heals: the garbage is overwritten with a valid seed, so next session can collapse
        nxt = _decide([{"key": "findings", "value": 20}], path)
        self.assertEqual(nxt["results"]["findings"]["outcome"], "collapse")

    def test_non_dict_ledger_fails_to_full(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "standing-alarms.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(["not", "a", "dict"], fh)
        out = _decide([{"key": "findings", "value": 20}], path)
        self.assertFalse(out["ok"])
        self.assertEqual(out["results"]["findings"]["outcome"], "full")

    def test_unwritable_location_fails_to_full_and_never_raises(self):
        # A path whose parent is a FILE (not a dir): makedirs fails -> fail-toward-full, no exception.
        d = tempfile.mkdtemp()
        blocker = os.path.join(d, "afile")
        with open(blocker, "w", encoding="utf-8") as fh:
            fh.write("x")
        path = os.path.join(blocker, "standing-alarms.json")  # parent is a file
        out = _decide([{"key": "findings", "value": 20}], path)
        self.assertFalse(out["ok"])
        self.assertEqual(out["results"]["findings"]["outcome"], "full")

    def test_malformed_alarm_entry_fails_to_full_and_never_raises(self):
        # A malformed alarm (missing "key") must degrade to fail-toward-full, never raise into the hook.
        out = bal.decide([{"value": 5}], path=os.path.join(tempfile.mkdtemp(), "l.json"))
        self.assertFalse(out["ok"])
        self.assertEqual(out["results"], {})  # empty -> the renderer defaults every alarm to full

    def test_no_eligible_alarms_yields_no_results_and_never_crashes(self):
        out = _decide([], os.path.join(tempfile.mkdtemp(), "l.json"))
        self.assertEqual(out["results"], {})

    def test_empty_alarm_set_drops_a_prior_entry(self):
        # The vanish path at the decide level: a populated ledger, then a session with no eligible alarms,
        # clears the ledger so a later recurrence relays full (no stale baseline survives).
        d = tempfile.mkdtemp()
        path = os.path.join(d, "l.json")
        _decide([{"key": "findings", "value": 5}], path)   # seed
        _decide([], path)                                  # all alarms gone -> drop
        out = _decide([{"key": "findings", "value": 5}], path)
        self.assertEqual(out["results"]["findings"]["outcome"], "full")


class TestPathResolution(unittest.TestCase):
    def test_env_override_wins(self):
        d = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {bal.ENV_DIR: d}):
            self.assertEqual(bal.ledger_dir(), os.path.abspath(d))
            self.assertEqual(bal.ledger_path(), os.path.join(os.path.abspath(d), bal.LEDGER_FILENAME))

    def test_explicit_path_arg_wins_over_everything(self):
        self.assertEqual(bal.ledger_path(path="/tmp/x.json"), "/tmp/x.json")

    def test_default_dir_is_the_boot_cache_under_a_clone_root(self):
        # No env override: resolves under <root>/.engine/boot/.cache (the gitignored, .cache-pruned home).
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(bal.ENV_DIR, None)
            d = bal.ledger_dir()
        self.assertTrue(d.endswith(os.path.join(".engine", "boot", ".cache")), d)


class TestSweepIsolation(unittest.TestCase):
    def test_module_does_not_import_boot_or_memory(self):
        # D-269: the ledger shares NO code path with memory's sweep; the dependency is one-way boot -> ledger.
        with open(bal.__file__, encoding="utf-8") as fh:
            src = fh.read()
        tree = ast.parse(src)
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
        for forbidden in ("boot", "memory"):
            self.assertNotIn(forbidden, imported,
                             f"boot_alarm_ledger must not import {forbidden} (one-way dependency / sweep isolation)")
            self.assertFalse(any(m.startswith(forbidden + ".") for m in imported),
                             f"boot_alarm_ledger must not import a {forbidden} submodule")


if __name__ == "__main__":
    unittest.main()
