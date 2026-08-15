"""Tests for the post-review checkout-integrity guard (StarshipSuperjam/engine-template#947). Builds throwaway git
repos, snapshots them, applies the exact mutations the two incidents caused, and asserts `verify`
flags each one and stays silent on an untouched checkout. Offline — the temp repos use a local
file:// origin, never the network."""
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import review_integrity as ri  # noqa: E402


def _git(args, cwd):
    subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, check=True)


def _new_repo(path, origin="https://github.com/acme/real.git"):
    os.makedirs(path, exist_ok=True)
    _git(["init", "-q"], path)
    _git(["remote", "add", "origin", origin], path)
    _git(["config", "user.email", "t@example.com"], path)
    _git(["config", "user.name", "t"], path)
    with open(os.path.join(path, "f.txt"), "w") as fh:
        fh.write("one\n")
    _git(["add", "f.txt"], path)
    _git(["commit", "-q", "-m", "one"], path)


class TestSnapshot(unittest.TestCase):
    def test_captures_the_mutation_sensitive_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            main = os.path.join(tmp, "c")
            _new_repo(main)
            snap = ri.snapshot(main)
            self.assertEqual(snap["origin"], "https://github.com/acme/real.git")
            self.assertEqual(snap["head"], _rev(main))
            self.assertEqual(snap["stash_count"], 0)
            self.assertEqual([e[0] for e in snap["worktrees"]], [os.path.realpath(main)])

    def test_non_repo_reads_as_none_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            snap = ri.snapshot(tmp)  # a bare dir, no git
            self.assertIsNone(snap["origin"])
            self.assertIsNone(snap["head"])
            self.assertIsNone(snap["worktrees"])


class TestVerify(unittest.TestCase):
    def test_untouched_checkout_not_mutated(self):
        with tempfile.TemporaryDirectory() as tmp:
            main = os.path.join(tmp, "c")
            _new_repo(main)
            before = ri.snapshot(main)
            result = ri.verify(main, before)
            self.assertFalse(result["mutated"])
            self.assertEqual(result["changes"], [])

    def test_origin_repoint_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            main = os.path.join(tmp, "c")
            _new_repo(main)
            before = ri.snapshot(main)
            _git(["remote", "set-url", "origin", "https://github.com/attacker/fake.git"], main)
            result = ri.verify(main, before)
            self.assertTrue(result["mutated"])
            self.assertTrue(any("origin remote URL changed" in c for c in result["changes"]))

    def test_stray_worktree_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            main = os.path.join(tmp, "c")
            _new_repo(main)
            before = ri.snapshot(main)
            _git(["worktree", "add", "-q", os.path.join(tmp, "stray"), "-b", "stray"], main)
            result = ri.verify(main, before)
            self.assertTrue(result["mutated"])
            self.assertTrue(any("new worktree registration" in c for c in result["changes"]))

    def test_stash_growth_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            main = os.path.join(tmp, "c")
            _new_repo(main)
            before = ri.snapshot(main)
            with open(os.path.join(main, "f.txt"), "w") as fh:
                fh.write("two\n")
            _git(["stash", "-q"], main)
            result = ri.verify(main, before)
            self.assertTrue(result["mutated"])
            self.assertTrue(any("stash stack changed" in c for c in result["changes"]))

    def test_head_move_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            main = os.path.join(tmp, "c")
            _new_repo(main)
            before = ri.snapshot(main)
            with open(os.path.join(main, "g.txt"), "w") as fh:
                fh.write("two\n")
            _git(["add", "g.txt"], main)
            _git(["commit", "-q", "-m", "two"], main)
            result = ri.verify(main, before)
            self.assertTrue(result["mutated"])
            self.assertTrue(any("HEAD moved" in c for c in result["changes"]))

    def test_becoming_unreadable_fails_closed(self):
        # a real 'before' snapshot verified against a now-unreadable checkout reports mutation,
        # never a false all-clear.
        with tempfile.TemporaryDirectory() as tmp:
            main = os.path.join(tmp, "c")
            _new_repo(main)
            before = ri.snapshot(main)
            after = {"checkout": main, "origin": None, "branch": None, "head": None,
                     "stash_count": None, "worktrees": None}
            self.assertNotEqual(ri.compare(before, after), [])


class TestCli(unittest.TestCase):
    def test_demo_runs(self):
        import io
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ri.main(["demo"]), 0)

    def test_verify_exit_code_on_mutation(self):
        import io
        import json
        import contextlib
        with tempfile.TemporaryDirectory() as tmp:
            main = os.path.join(tmp, "c")
            _new_repo(main)
            snap_path = os.path.join(tmp, "before.json")
            with open(snap_path, "w") as fh:
                json.dump(ri.snapshot(main), fh)
            # unchanged -> 0
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(ri.main(["verify", main, snap_path]), 0)
            # mutate -> exit 3
            _git(["remote", "set-url", "origin", "https://x/y.git"], main)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(ri.main(["verify", main, snap_path]), 3)


def _rev(path):
    return subprocess.run(["git", "-C", path, "rev-parse", "HEAD"], capture_output=True,
                          text=True, check=True).stdout.strip()


if __name__ == "__main__":
    unittest.main()
