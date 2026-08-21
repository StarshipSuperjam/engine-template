#!/usr/bin/env python3
"""Focused tests for bounded, default-on session-start checkout catch-up."""
from __future__ import annotations

import json
import os
import subprocess
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

    def test_dangling_symlink_fails_closed_instead_of_becoming_the_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "operator-checkout.json")
            os.symlink(os.path.join(tmp, "no-such-preference"), path)
            result = cau.load_preference(path=path)
        self.assertEqual((result["state"], result["reason"]), ("invalid", "not-a-regular-file"))

    def test_preference_replaced_between_lstat_and_open_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "operator-checkout.json")
            replacement = os.path.join(tmp, "replacement.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"automatic_catch_up": False}, fh)
            with open(replacement, "w", encoding="utf-8") as fh:
                json.dump({"automatic_catch_up": True}, fh)
            real_open = cau.os.open
            def swapped(open_path, flags):
                os.replace(replacement, path)
                return real_open(open_path, flags)
            with mock.patch.object(cau.os, "open", side_effect=swapped):
                result = cau.load_preference(path=path)
        self.assertEqual((result["state"], result["reason"]), ("invalid", "changed-during-read"))

    def test_invalid_preference_reason_has_a_plain_recovery_explanation(self):
        self.assertEqual(cau.preference_problem("invalid-json"), "the file is not valid JSON")
        self.assertIn("automatic_catch_up", cau.preference_problem("unexpected-shape"))
        self.assertNotIn("not-a-boolean", cau.preference_problem("not-a-boolean"))
        self.assertIn("regular file", cau.preference_problem("not-a-regular-file"))

    def test_atomic_writer_preserves_previous_file_when_replace_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".engine", "operator-checkout.json")
            os.makedirs(os.path.dirname(path))
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"automatic_catch_up": False}, fh)
            with mock.patch.object(cau.os, "replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    cau._atomic_write(path, True)
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh), {"automatic_catch_up": False})

    def test_choice_is_staged_atomically_only_in_the_review_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "live", ".engine", "operator-checkout.json")
            review = os.path.join(tmp, "review")
            os.makedirs(os.path.join(review, ".engine"))
            seen = {}
            def opener(**kwargs):
                seen.update(kwargs)
                with open(os.path.join(kwargs["cwd"], ".engine", "operator-checkout.json"), encoding="utf-8") as fh:
                    seen["preference"] = json.load(fh)
                return {"number": 1, "html_url": "https://example.invalid/pr/1"}
            with mock.patch.object(cau, "_staging_worktree", return_value=(review, None)), \
                 mock.patch.object(cau, "_remove_staging_worktree", return_value=(True, None)) as remove:
                result = cau.set_preference(False, path=path, opener=opener)
            self.assertTrue(result["ok"])
            self.assertEqual(seen["paths"], [".engine/operator-checkout.json"])
            self.assertEqual(seen["cwd"], review)
            self.assertEqual(seen["preference"], {"automatic_catch_up": False})
            self.assertFalse(os.path.exists(path), "the live checkout cannot read an unmerged proposal")
            remove.assert_called_once_with(os.path.join(tmp, "live"), review)

    def test_failed_preference_pr_leaves_no_active_live_choice(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "live", ".engine", "operator-checkout.json")
            review = os.path.join(tmp, "review")
            os.makedirs(os.path.join(review, ".engine"))
            with mock.patch.object(cau, "_staging_worktree", return_value=(review, None)), \
                 mock.patch.object(cau, "_remove_staging_worktree", return_value=(True, None)):
                result = cau.set_preference(False, path=path,
                                            opener=mock.Mock(side_effect=RuntimeError("network down")))
            self.assertFalse(result["ok"])
            self.assertFalse(os.path.exists(path))

    def test_failed_preference_pr_force_removes_the_actual_dirty_staging_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            live = os.path.join(tmp, "live")
            def git(*args):
                return subprocess.run(["git", "-C", live, *args], capture_output=True, text=True, check=True)
            subprocess.run(["git", "init", "-q", "--initial-branch=main", live], check=True)
            git("config", "user.email", "test@example.invalid")
            git("config", "user.name", "Preference cleanup test")
            os.makedirs(os.path.join(live, ".engine"))
            with open(os.path.join(live, ".engine", "marker"), "w", encoding="utf-8") as fh:
                fh.write("fixture\n")
            git("add", ".engine")
            git("commit", "-qm", "initial")
            result = cau.set_preference(False, path=os.path.join(live, cau.CONFIG_REL),
                                        opener=mock.Mock(side_effect=RuntimeError("network down")))
            self.assertFalse(result["ok"])
            self.assertFalse(os.path.exists(os.path.join(live, cau.CONFIG_REL)))
            registered = git("worktree", "list", "--porcelain").stdout
            self.assertNotIn("engine-checkout-preference-", registered)

    def test_real_review_worktree_keeps_the_live_checkout_and_preference_unchanged_until_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            live = os.path.join(tmp, "live")
            def git(*args):
                return subprocess.run(["git", "-C", live, *args], capture_output=True, text=True, check=True)
            subprocess.run(["git", "init", "-q", "--initial-branch=main", live], check=True)
            git("config", "user.email", "test@example.invalid")
            git("config", "user.name", "Preference test")
            os.makedirs(os.path.join(live, ".engine"))
            with open(os.path.join(live, ".engine", "marker"), "w", encoding="utf-8") as fh:
                fh.write("fixture\n")
            git("add", ".engine")
            git("commit", "-qm", "initial")
            seen = {}
            def opener(**kwargs):
                seen["cwd"] = kwargs["cwd"]
                with open(os.path.join(kwargs["cwd"], ".engine", "operator-checkout.json"), encoding="utf-8") as fh:
                    seen["value"] = json.load(fh)
                return {"number": 1}
            result = cau.set_preference(False, path=os.path.join(live, cau.CONFIG_REL), opener=opener)
            self.assertTrue(result["ok"])
            self.assertEqual(seen["value"], {"automatic_catch_up": False})
            self.assertNotEqual(seen["cwd"], live)
            self.assertFalse(os.path.exists(os.path.join(live, cau.CONFIG_REL)))
            self.assertEqual(git("branch", "--show-current").stdout.strip(), "main")


class TestAssessedTargetPreference(unittest.TestCase):
    def test_committed_symlink_fails_closed_without_dereferencing_it(self):
        with mock.patch.object(cau.checkout_health, "_run",
                               return_value="120000 blob deadbeef\t.engine/operator-checkout.json\0"):
            result = cau._target_preference(SNAPSHOT)
        self.assertEqual((result["state"], result["reason"]), ("invalid", "not-a-regular-file"))


class TestAutomaticController(unittest.TestCase):
    def _enabled(self):
        return {"state": "enabled", "source": "default", "path": "/operator/.engine/operator-checkout.json"}

    def test_opt_out_keeps_a_fresh_snapshot_for_drift_detection_without_mutating(self):
        current = {**SNAPSHOT, "state": "current", "behind_commits": 0}
        with mock.patch.object(cau, "load_preference", return_value={"state": "disabled"}), \
             mock.patch.object(cau.checkout_health, "checkout_snapshot", return_value=current) as snapshot, \
             mock.patch.object(cau.checkout_health, "_advance_clean_default_snapshot") as advance:
            self.assertEqual(cau.automatic_catch_up()["status"], "disabled")
        snapshot.assert_called_once_with(None)
        advance.assert_not_called()

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
             mock.patch.object(cau, "_target_preference", return_value=self._enabled()), \
             mock.patch.object(cau.checkout_health, "_advance_clean_default_snapshot", return_value=fixed) as advance:
            result = cau.automatic_catch_up()
        advance.assert_called_once_with(SNAPSHOT, protect_head=True)
        self.assertEqual((result["status"], result["snapshot"]["state"], result["snapshot"]["head_oid"]),
                         ("updated", "current", SNAPSHOT["target_oid"]))

    def test_concurrent_winner_normalises_to_current(self):
        current = {**SNAPSHOT, "state": "current", "behind_commits": 0}
        with mock.patch.object(cau, "load_preference", return_value=self._enabled()), \
             mock.patch.object(cau.checkout_health, "checkout_snapshot", side_effect=[SNAPSHOT, current]), \
             mock.patch.object(cau.checkout_health, "_succeeds", return_value=True), \
             mock.patch.object(cau.checkout_health, "_is_lossless", return_value=(True, [])), \
             mock.patch.object(cau, "_target_preference", return_value=self._enabled()), \
             mock.patch.object(cau.checkout_health, "_advance_clean_default_snapshot",
                               return_value={"status": "blocked", "reason": "checkout-changed", "applied": False}):
            result = cau.automatic_catch_up()
        self.assertEqual((result["status"], result["peer_updated"]), ("current", True))

    def test_peer_wait_polls_only_local_locks_then_refreshes_once(self):
        current = {**SNAPSHOT, "state": "current", "behind_commits": 0}
        loser = {"status": "blocked", "reason": "checkout-changed", "snapshot": SNAPSHOT}
        with mock.patch.object(cau.checkout_health, "_git_lock_is_present", side_effect=[True, True, False, False]), \
             mock.patch.object(cau.checkout_health, "checkout_snapshot", return_value=current) as snapshot, \
             mock.patch.object(cau.checkout_health, "_is_lossless", return_value=(True, [])), \
             mock.patch.object(cau.time, "sleep") as sleep:
            result = cau._normalise_peer_winner("/operator", loser)
        self.assertEqual((result["status"], result["peer_updated"]), ("current", True))
        self.assertEqual(snapshot.call_count, 1)
        self.assertEqual(sleep.call_count, 2)

    def test_unavailable_target_never_advances(self):
        unavailable = {"state": "unavailable", "reason": "refresh-timeout", "fresh": False}
        with mock.patch.object(cau, "load_preference", return_value=self._enabled()), \
             mock.patch.object(cau.checkout_health, "checkout_snapshot", return_value=unavailable), \
             mock.patch.object(cau.checkout_health, "_advance_clean_default_snapshot") as advance:
            result = cau.automatic_catch_up()
        self.assertEqual(result["status"], "unavailable")
        advance.assert_not_called()

    def test_assessed_target_opt_out_never_enters_the_mutation_seam(self):
        target_opt_out = {"state": "disabled", "source": "target-configured", "path": "target:config"}
        with mock.patch.object(cau, "load_preference", return_value=self._enabled()), \
             mock.patch.object(cau.checkout_health, "checkout_snapshot", return_value=SNAPSHOT), \
             mock.patch.object(cau.checkout_health, "_succeeds", return_value=True), \
             mock.patch.object(cau.checkout_health, "_is_lossless", return_value=(True, [])), \
             mock.patch.object(cau, "_target_preference", return_value=target_opt_out), \
             mock.patch.object(cau.checkout_health, "_advance_clean_default_snapshot") as advance:
            result = cau.automatic_catch_up()
        self.assertEqual((result["status"], result["preference"]["source"]), ("disabled", "target-configured"))
        advance.assert_not_called()

    def test_reviewed_target_reenable_supersedes_a_stale_local_opt_out(self):
        fixed = {"status": "fixed", "branch": "main", "before": SNAPSHOT["head_oid"],
                 "after": SNAPSHOT["target_oid"], "target_oid": SNAPSHOT["target_oid"], "applied": True}
        local_opt_out = {"state": "disabled", "source": "configured", "path": "local:config"}
        remote_reenable = {"state": "enabled", "source": "target-configured", "path": "target:config"}
        with mock.patch.object(cau, "load_preference", return_value=local_opt_out), \
             mock.patch.object(cau.checkout_health, "checkout_snapshot", return_value=SNAPSHOT), \
             mock.patch.object(cau, "_target_preference", return_value=remote_reenable), \
             mock.patch.object(cau.checkout_health, "_succeeds", return_value=True), \
             mock.patch.object(cau.checkout_health, "_is_lossless", return_value=(True, [])), \
             mock.patch.object(cau.checkout_health, "_advance_clean_default_snapshot", return_value=fixed) as advance:
            result = cau.automatic_catch_up()
        self.assertEqual(result["status"], "updated")
        advance.assert_called_once_with(SNAPSHOT, protect_head=True)

    def test_malformed_assessed_target_never_enters_the_mutation_seam(self):
        malformed = {"state": "invalid", "reason": "invalid-json", "path": "target:config"}
        with mock.patch.object(cau, "load_preference", return_value=self._enabled()), \
             mock.patch.object(cau.checkout_health, "checkout_snapshot", return_value=SNAPSHOT), \
             mock.patch.object(cau.checkout_health, "_succeeds", return_value=True), \
             mock.patch.object(cau.checkout_health, "_is_lossless", return_value=(True, [])), \
             mock.patch.object(cau, "_target_preference", return_value=malformed), \
             mock.patch.object(cau.checkout_health, "_advance_clean_default_snapshot") as advance:
            result = cau.automatic_catch_up()
        self.assertEqual((result["status"], result["preference"]["reason"]), ("invalid-config", "invalid-json"))
        advance.assert_not_called()

    def test_target_moved_or_late_clash_is_never_reported_as_updated(self):
        for reason in ("target-changed", "postcondition-failed"):
            with self.subTest(reason=reason), \
                 mock.patch.object(cau, "load_preference", return_value=self._enabled()), \
                 mock.patch.object(cau.checkout_health, "checkout_snapshot", return_value=SNAPSHOT), \
                 mock.patch.object(cau.checkout_health, "_succeeds", return_value=True), \
                 mock.patch.object(cau.checkout_health, "_is_lossless", return_value=(True, [])), \
                 mock.patch.object(cau, "_target_preference", return_value=self._enabled()), \
                 mock.patch.object(cau.checkout_health, "_advance_clean_default_snapshot",
                                   return_value={"status": "blocked", "reason": reason, "applied": False}):
                result = cau.automatic_catch_up()
            self.assertEqual((result["status"], result["reason"]), ("blocked", reason))

    def test_real_remote_opt_out_is_honoured_before_it_is_materialized(self):
        with tempfile.TemporaryDirectory() as tmp:
            origin = os.path.join(tmp, "origin.git")
            author = os.path.join(tmp, "author")
            operator = os.path.join(tmp, "operator")
            def git(cwd, *args):
                return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, check=True)
            subprocess.run(["git", "init", "-q", "--bare", "--initial-branch=main", origin], check=True)
            subprocess.run(["git", "init", "-q", "--initial-branch=main", author], check=True)
            git(author, "config", "user.email", "test@example.invalid")
            git(author, "config", "user.name", "Target preference test")
            os.makedirs(os.path.join(author, ".claude"))
            os.makedirs(os.path.join(author, ".engine"))
            with open(os.path.join(author, ".claude", "settings.json"), "w", encoding="utf-8") as fh:
                fh.write("{}\n")
            with open(os.path.join(author, ".engine", "marker"), "w", encoding="utf-8") as fh:
                fh.write("fixture\n")
            with open(os.path.join(author, "shared.txt"), "w", encoding="utf-8") as fh:
                fh.write("before\n")
            git(author, "add", ".")
            git(author, "commit", "-qm", "initial")
            git(author, "remote", "add", "origin", origin)
            git(author, "push", "-qu", "origin", "main")
            subprocess.run(["git", "clone", "-q", origin, operator], check=True)
            with open(os.path.join(author, ".engine", "operator-checkout.json"), "w", encoding="utf-8") as fh:
                json.dump({"automatic_catch_up": False}, fh)
                fh.write("\n")
            git(author, "add", ".engine/operator-checkout.json")
            git(author, "commit", "-qm", "disable automatic catch-up")
            git(author, "push", "-q", "origin", "main")
            before = git(operator, "rev-parse", "HEAD").stdout.strip()
            result = cau.automatic_catch_up(cwd=operator)
            after = git(operator, "rev-parse", "HEAD").stdout.strip()
            self.assertEqual((result["status"], result["preference"]["source"]), ("disabled", "target-configured"))
            self.assertEqual(after, before)
            self.assertFalse(os.path.exists(os.path.join(operator, ".engine", "operator-checkout.json")))

    def test_throwaway_repository_demonstration_proves_exact_clean_fast_forward(self):
        self.assertEqual(cau._demo(), 0)


if __name__ == "__main__":
    unittest.main()
