#!/usr/bin/env python3
"""Focused tests for bounded, default-on session-start checkout catch-up."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

import checkout_auto_update as cau


SNAPSHOT = {
    "state": "behind", "main": "/operator", "branch": "main", "current": "main", "on_default": True,
    "origin": "https://github.com/acme/project.git", "upstream": "refs/remotes/origin/main",
    "head_oid": "a" * 40, "default_oid": "a" * 40, "target_oid": "b" * 40,
    "behind_commits": 2, "missing_merges": 1, "fresh": True,
}


class TestPreference(unittest.TestCase):
    def test_missing_file_defaults_to_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = cau.load_preference(path=os.path.join(tmp, "operator-checkout.json"))
        self.assertEqual((result["state"], result["source"]), ("enabled", "default"))

    def test_false_is_the_only_opt_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "operator-checkout.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"automatic_catch_up": False}, fh)
            self.assertEqual(cau.load_preference(path=path)["state"], "disabled")

    def test_malformed_and_unknown_shapes_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "operator-checkout.json")
            for content, reason in (("{ broken", "invalid-json"),
                                    (json.dumps({}), "unexpected-shape"),
                                    (json.dumps({"automatic_catch_up": 0}), "not-a-boolean"),
                                    (json.dumps({"automatic_catch_up": True, "extra": True}), "unexpected-shape")):
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(content)
                result = cau.load_preference(path=path)
                self.assertEqual((result["state"], result["reason"]), ("invalid", reason))

    def test_unreadable_path_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = cau.load_preference(path=tmp)
        self.assertEqual((result["state"], result["reason"]), ("invalid", "unreadable"))

    def test_atomic_writer_preserves_previous_file_when_replace_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".engine", "operator-checkout.json")
            os.makedirs(os.path.dirname(path))
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"automatic_catch_up": False}, fh)
            with mock.patch.object(cau.os, "replace", side_effect=OSError("disk full")):
                result = cau.set_preference(True, path=path, open_pr=False)
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh), {"automatic_catch_up": False})
            self.assertFalse(result["ok"])

    def test_choice_is_saved_atomically_and_handed_to_a_reviewed_pr(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".engine", "operator-checkout.json")
            seen = {}
            def opener(**kwargs):
                seen.update(kwargs)
                return {"number": 1, "html_url": "https://example.invalid/pr/1"}
            result = cau.set_preference(False, path=path, opener=opener)
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh), {"automatic_catch_up": False})
            self.assertTrue(result["ok"])
            self.assertEqual(seen["paths"], [".engine/operator-checkout.json"])
            self.assertEqual(seen["cwd"], tmp)


class TestAutomaticController(unittest.TestCase):
    def _enabled(self):
        return {"state": "enabled", "source": "default", "path": "/operator/.engine/operator-checkout.json"}

    def test_opt_out_short_circuits_before_the_automatic_snapshot(self):
        with mock.patch.object(cau, "load_preference", return_value={"state": "disabled"}), \
             mock.patch.object(cau.checkout_health, "checkout_snapshot") as snapshot:
            self.assertEqual(cau.automatic_catch_up()["status"], "disabled")
        snapshot.assert_not_called()

    def test_invalid_preference_short_circuits_before_the_automatic_snapshot(self):
        with mock.patch.object(cau, "load_preference", return_value={"state": "invalid", "reason": "invalid-json"}), \
             mock.patch.object(cau.checkout_health, "checkout_snapshot") as snapshot:
            self.assertEqual(cau.automatic_catch_up()["status"], "invalid-config")
        snapshot.assert_not_called()

    def test_current_checkout_is_silent_current_and_unchanged(self):
        current = {**SNAPSHOT, "state": "current", "behind_commits": 0}
        with mock.patch.object(cau, "load_preference", return_value=self._enabled()), \
             mock.patch.object(cau.checkout_health, "checkout_snapshot", return_value=current), \
             mock.patch.object(cau.checkout_health, "_advance_clean_default_snapshot") as advance:
            result = cau.automatic_catch_up()
        self.assertEqual(result["status"], "current")
        advance.assert_not_called()

    def test_dirty_subsumed_work_never_enters_manual_rescue(self):
        with mock.patch.object(cau, "load_preference", return_value=self._enabled()), \
             mock.patch.object(cau.checkout_health, "checkout_snapshot", return_value=SNAPSHOT), \
             mock.patch.object(cau.checkout_health, "_succeeds", return_value=True), \
             mock.patch.object(cau.checkout_health, "_is_lossless", return_value=(False, ["uncommitted"])), \
             mock.patch.object(cau.checkout_health, "_dirty_subsumed", return_value=True) as subsumed, \
             mock.patch.object(cau.checkout_health, "_advance_clean_default_snapshot") as advance:
            result = cau.automatic_catch_up()
        self.assertEqual((result["status"], result["reason"]), ("blocked", "local-work"))
        subsumed.assert_not_called()
        advance.assert_not_called()

    def test_automatic_mode_never_calls_manual_catchup_or_branch_return(self):
        off_main = {**SNAPSHOT, "on_default": False, "current": "topic"}
        with mock.patch.object(cau, "load_preference", return_value=self._enabled()), \
             mock.patch.object(cau.checkout_health, "checkout_snapshot", return_value=off_main), \
             mock.patch.object(cau.checkout_health, "catch_up") as catch_up, \
             mock.patch.object(cau.checkout_health, "return_to_default") as return_main:
            result = cau.automatic_catch_up()
        self.assertEqual((result["status"], result["reason"]), ("blocked", "off-main"))
        catch_up.assert_not_called()
        return_main.assert_not_called()

    def test_off_main_diverged_stashed_and_paused_states_do_not_advance(self):
        cases = [
            ({**SNAPSHOT, "on_default": False, "current": "topic"}, None, "off-main"),
            (SNAPSHOT, False, "diverged"),
            (SNAPSHOT, True, "local-work"),
        ]
        for snapshot, ancestor, reason in cases:
            with self.subTest(reason=reason), \
                 mock.patch.object(cau, "load_preference", return_value=self._enabled()), \
                 mock.patch.object(cau.checkout_health, "checkout_snapshot", return_value=snapshot), \
                 mock.patch.object(cau.checkout_health, "_succeeds", return_value=ancestor) as succeeds, \
                 mock.patch.object(cau.checkout_health, "_is_lossless", return_value=(False, ["stash", "op-in-progress"])), \
                 mock.patch.object(cau.checkout_health, "_advance_clean_default_snapshot") as advance:
                result = cau.automatic_catch_up()
            self.assertEqual((result["status"], result["reason"]), ("blocked", reason))
            advance.assert_not_called()
            if reason == "off-main":
                succeeds.assert_not_called()

    def test_success_reuses_the_exact_assessed_target_for_boot_current_state(self):
        fixed = {"status": "fixed", "branch": "main", "before": SNAPSHOT["head_oid"],
                 "after": SNAPSHOT["target_oid"], "target_oid": SNAPSHOT["target_oid"], "applied": True}
        with mock.patch.object(cau, "load_preference", return_value=self._enabled()), \
             mock.patch.object(cau.checkout_health, "checkout_snapshot", return_value=SNAPSHOT), \
             mock.patch.object(cau.checkout_health, "_succeeds", return_value=True), \
             mock.patch.object(cau.checkout_health, "_is_lossless", return_value=(True, [])), \
             mock.patch.object(cau.checkout_health, "_advance_clean_default_snapshot", return_value=fixed) as advance:
            result = cau.automatic_catch_up()
        advance.assert_called_once_with(SNAPSHOT)
        self.assertEqual((result["status"], result["snapshot"]["state"], result["snapshot"]["head_oid"]),
                         ("updated", "current", SNAPSHOT["target_oid"]))

    def test_concurrent_winner_normalises_to_current(self):
        current = {**SNAPSHOT, "state": "current", "behind_commits": 0}
        with mock.patch.object(cau, "load_preference", return_value=self._enabled()), \
             mock.patch.object(cau.checkout_health, "checkout_snapshot", side_effect=[SNAPSHOT, current]), \
             mock.patch.object(cau.checkout_health, "_succeeds", return_value=True), \
             mock.patch.object(cau.checkout_health, "_is_lossless", return_value=(True, [])), \
             mock.patch.object(cau.checkout_health, "_advance_clean_default_snapshot",
                               return_value={"status": "blocked", "reason": "checkout-changed", "applied": False}):
            result = cau.automatic_catch_up()
        self.assertEqual((result["status"], result["peer_updated"]), ("current", True))

    def test_unavailable_target_never_advances(self):
        unavailable = {"state": "unavailable", "reason": "refresh-timeout", "fresh": False}
        with mock.patch.object(cau, "load_preference", return_value=self._enabled()), \
             mock.patch.object(cau.checkout_health, "checkout_snapshot", return_value=unavailable), \
             mock.patch.object(cau.checkout_health, "_advance_clean_default_snapshot") as advance:
            result = cau.automatic_catch_up()
        self.assertEqual(result["status"], "unavailable")
        advance.assert_not_called()

    def test_target_moved_or_late_clash_is_never_reported_as_updated(self):
        for reason in ("target-changed", "postcondition-failed"):
            with self.subTest(reason=reason), \
                 mock.patch.object(cau, "load_preference", return_value=self._enabled()), \
                 mock.patch.object(cau.checkout_health, "checkout_snapshot", return_value=SNAPSHOT), \
                 mock.patch.object(cau.checkout_health, "_succeeds", return_value=True), \
                 mock.patch.object(cau.checkout_health, "_is_lossless", return_value=(True, [])), \
                 mock.patch.object(cau.checkout_health, "_advance_clean_default_snapshot",
                                   return_value={"status": "blocked", "reason": reason, "applied": False}):
                result = cau.automatic_catch_up()
            self.assertEqual((result["status"], result["reason"]), ("blocked", reason))

    def test_throwaway_repository_demonstration_proves_exact_clean_fast_forward(self):
        self.assertEqual(cau._demo(), 0)


if __name__ == "__main__":
    unittest.main()
