"""Tests for execution_env_policy — allowlist environment construction (only named keys, no ambient leak),
process-group launch, and verified tree-kill (leader and child-tree reaping)."""
from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import execution_env_policy as ep  # noqa: E402


class TestAllowlistEnvironment(unittest.TestCase):
    def test_only_named_keys_present(self):
        src = {"KEEP": "1", "DROP": "2", "ALSO": "3"}
        env = ep.allowlist_environment(["KEEP", "ALSO"], source=src)
        self.assertEqual(env, {"KEEP": "1", "ALSO": "3"})

    def test_child_env_equals_allowlist_intersection(self):
        # the witness the spike asserts: the child environment IS exactly the allowlisted keys present
        src = {"A": "1", "B": "2"}
        allow = ["A", "B", "C"]  # C absent from source
        env = ep.allowlist_environment(allow, source=src)
        self.assertEqual(set(env), {"A", "B"})

    def test_absent_key_not_invented(self):
        self.assertEqual(ep.allowlist_environment(["MISSING"], source={"OTHER": "x"}), {})

    def test_ambient_secret_not_leaked(self):
        os.environ["EP_TEST_SECRET_XYZ"] = "leak"
        try:
            env = ep.allowlist_environment(["PATH"])  # default source is os.environ; only PATH kept
            self.assertNotIn("EP_TEST_SECRET_XYZ", env)
        finally:
            del os.environ["EP_TEST_SECRET_XYZ"]


class TestLaunchAndReap(unittest.TestCase):
    def test_single_process_group_reaped_and_verified(self):
        proc = ep.launch(["/bin/sh", "-c", "sleep 30"], env=ep.allowlist_environment(["PATH"]))
        try:
            witness = ep.terminate_tree(proc, grace_seconds=1.0)
            self.assertIsNotNone(witness["pgid"])  # launched in its own process group
            self.assertTrue(witness["leader_exited"])
            self.assertTrue(witness["group_reaped"])
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_child_tree_is_reaped(self):
        # a child that spawns a grandchild; both must be gone after terminate_tree
        proc = ep.launch(["/bin/sh", "-c", "sleep 30 & sleep 30"], env=ep.allowlist_environment(["PATH"]))
        pgid = None
        try:
            witness = ep.terminate_tree(proc, grace_seconds=1.0)
            pgid = witness["pgid"]
            self.assertTrue(witness["leader_exited"])
            # the group may hold a briefly-reparented grandchild; give the OS a moment, then confirm gone
            gone = witness["group_reaped"]
            for _ in range(50):
                if gone:
                    break
                time.sleep(0.05)
                gone = ep._group_gone(pgid)
            self.assertTrue(gone, "process group was not fully reaped")
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_terminate_tree_tolerates_eperm_from_reused_pgid(self):
        # When a group is already gone and its pgid has been reused by a process this session does not own,
        # killpg raises PermissionError. terminate_tree must swallow that (it must never signal a stranger's
        # process) and still return a witness rather than crashing the teardown.
        proc = ep.launch(["/bin/sh", "-c", "sleep 30"], env=ep.allowlist_environment(["PATH"]))
        real_killpg = os.killpg

        def fake_killpg(pgid, sig):
            raise PermissionError("Operation not permitted")

        os.killpg = fake_killpg
        try:
            witness = ep.terminate_tree(proc, grace_seconds=0.2)
            self.assertEqual(witness["pid"], proc.pid)  # returned a witness instead of raising EPERM
        finally:
            os.killpg = real_killpg
            if proc.poll() is None:
                proc.kill()
                proc.wait()


if __name__ == "__main__":
    unittest.main()
