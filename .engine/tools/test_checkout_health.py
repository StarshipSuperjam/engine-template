#!/usr/bin/env python3
"""Tests for checkout_health — the stranded-checkout detector (issue #80, slice B).

Lock the behaviours a non-engineer cannot read code to verify: a healthy folder reads CLEAR, a folder
stuck off its branch or missing the engine's files reads STRANDED (with the right reason), and a folder the
detector cannot resolve degrades QUIETLY to None (never a false alarm, never a crash). Fixtures are throwaway
git repos (the 27d collision-check pattern) so the detection is proven offline and deterministically.
"""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
import tempfile
import unittest

import checkout_health


def _git(root: str, *args: str) -> None:
    subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, check=False)


def _repo(tmp: str, name: str, *, detach: bool = False, drop: tuple = ()) -> str:
    """A throwaway git checkout: engine files present, one commit. `detach` leaves HEAD detached; `drop`
    removes the named engine paths from the working tree (a missing-files strand)."""
    root = os.path.join(tmp, name)
    os.makedirs(os.path.join(root, ".claude"))
    os.makedirs(os.path.join(root, ".engine"))
    with open(os.path.join(root, ".claude", "settings.json"), "w") as fh:
        fh.write("{}")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=e@x", "-c", "user.name=n", "commit", "-q", "-m", "seed", "--allow-empty")
    if detach:
        sha = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"],
                             capture_output=True, text=True).stdout.strip()
        _git(root, "checkout", "-q", "--detach", sha)
    for rel in drop:
        p = os.path.join(root, rel)
        os.rmdir(p) if os.path.isdir(p) else os.remove(p)
    return root


class TestDetectStrand(unittest.TestCase):
    def test_healthy_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(checkout_health.detect_strand(cwd=_repo(tmp, "ok")))

    def test_detached_head_is_stranded(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = checkout_health.detect_strand(cwd=_repo(tmp, "det", detach=True))
            self.assertIsNotNone(r)
            self.assertIn("detached", r["states"])
            self.assertNotIn("missing-files", r["states"])   # files still present, only HEAD detached

    def test_missing_settings_is_stranded(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = checkout_health.detect_strand(
                cwd=_repo(tmp, "nos", drop=(os.path.join(".claude", "settings.json"),)))
            self.assertEqual(r["states"], ["missing-files"])

    def test_missing_engine_dir_is_stranded(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = checkout_health.detect_strand(cwd=_repo(tmp, "noe", drop=(".engine",)))
            self.assertEqual(r["states"], ["missing-files"])

    def test_detached_and_missing_reports_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = checkout_health.detect_strand(cwd=_repo(tmp, "both", detach=True, drop=(".engine",)))
            self.assertEqual(set(r["states"]), {"detached", "missing-files"})

    def test_main_path_is_the_resolved_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(tmp, "p", detach=True)
            self.assertTrue(os.path.samefile(checkout_health.detect_strand(cwd=root)["main"], root))

    def test_behind_origin_is_not_alarmed(self):
        # Ordinary "behind" is the NORMAL state under the worktree-and-PR model: a healthy branch that is
        # simply behind its (here absent) upstream must read CLEAR — only detached / missing-files strand.
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(checkout_health.detect_strand(cwd=_repo(tmp, "behind")))

    def test_non_git_dir_degrades_quietly_to_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            # no resolvable main checkout -> quiet None (fail-soft), never a crash or a false alarm
            self.assertIsNone(checkout_health.detect_strand(cwd=tmp))

    def test_bare_repo_is_not_a_strand(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "b.git")
            subprocess.run(["git", "init", "--bare", "-q", root], capture_output=True, text=True, check=False)
            # a bare repo has no working checkout -> not an operator checkout -> None, never "missing-files"
            self.assertIsNone(checkout_health.detect_strand(cwd=root))


class TestDemo(unittest.TestCase):
    def test_demo_runs(self):
        # the operator-runnable demo classifies fixtures + prints the warm strand line; rc 0, never raises.
        with contextlib.redirect_stdout(io.StringIO()):   # keep the suite output clean
            self.assertEqual(checkout_health.main(["demo"]), 0)


def _commit(root: str, msg: str) -> None:
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=e@x", "-c", "user.name=n", "commit", "-q", "-m", msg)


def _head(root: str) -> str:
    return subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], capture_output=True, text=True).stdout


class TestUnstrand(unittest.TestCase):
    """The un-stranding fix: lossless-or-it-does-not-run. The load-bearing proof for a folder-mutating change —
    every at-risk artifact must survive (on the rescue branch / untouched), and an unresolvable case refuses."""

    def _show(self, root, ref, path):
        return checkout_health._run(["git", "-C", root, "show", f"{ref}:{path}"])

    def test_healthy_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(checkout_health.unstrand(cwd=_repo(tmp, "ok"), apply=True)["status"], "healthy")

    def test_detached_lossless_reattaches_with_no_rescue(self):
        # detached at the branch tip (on-branch commit), clean tree -> lossless re-attach, no rescue branch
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(tmp, "det", detach=True)
            r = checkout_health.unstrand(cwd=root, apply=True)
            self.assertEqual(r["status"], "fixed")
            self.assertIsNone(r["rescue"])
            self.assertIsNone(checkout_health.detect_strand(cwd=root))   # healthy: back on its branch

    def test_offbranch_committed_work_survives_on_the_rescue_branch(self):
        # the scary case: COMMITTED work on a detached HEAD, reachable from no branch
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(tmp, "atrisk", detach=True)
            with open(os.path.join(root, "note.txt"), "w") as fh:
                fh.write("KEEP ME")
            _commit(root, "off-branch work")
            r = checkout_health.unstrand(cwd=root, apply=True)
            self.assertEqual(r["status"], "fixed")
            self.assertIsNotNone(r["rescue"])
            self.assertIsNone(checkout_health.detect_strand(cwd=root))     # healthy now
            self.assertEqual(self._show(root, r["rescue"], "note.txt"), "KEEP ME")  # the work SURVIVED

    def test_uncommitted_work_on_a_detached_head_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(tmp, "dirty", detach=True)
            with open(os.path.join(root, "wip.txt"), "w") as fh:   # untracked WIP, never committed
                fh.write("WIP CONTENT")
            r = checkout_health.unstrand(cwd=root, apply=True)
            self.assertEqual(r["status"], "fixed")
            self.assertIsNotNone(r["rescue"])
            self.assertEqual(self._show(root, r["rescue"], "wip.txt"), "WIP CONTENT")  # WIP saved, not lost

    def test_a_stash_is_left_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(tmp, "stash", detach=True)
            with open(os.path.join(root, "x.txt"), "w") as fh:
                fh.write("v1")
            _commit(root, "x")                              # an off-branch commit (so HEAD has x.txt tracked)
            with open(os.path.join(root, "x.txt"), "w") as fh:
                fh.write("v2")
            _git(root, "stash")                             # stash the v2 change
            before = checkout_health._run(["git", "-C", root, "stash", "list"])
            self.assertTrue(before and before.strip())      # there IS a stash
            checkout_health.unstrand(cwd=root, apply=True)
            self.assertEqual(checkout_health._run(["git", "-C", root, "stash", "list"]), before)  # untouched

    def test_missing_engine_files_are_rematerialized(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(tmp, "miss", drop=(os.path.join(".claude", "settings.json"),))
            r = checkout_health.unstrand(cwd=root, apply=True)
            self.assertEqual(r["status"], "fixed")
            self.assertTrue(os.path.exists(os.path.join(root, ".claude", "settings.json")))
            self.assertIsNone(checkout_health.detect_strand(cwd=root))

    def test_rematerialize_is_per_path_never_tracked_does_not_block_others(self):
        # HEAD has .engine but NOT .claude/settings.json; both absent from the tree. Restoring must handle each
        # path independently — the never-tracked .claude/settings.json must not abort restoring .engine.
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "partial")
            os.makedirs(os.path.join(root, ".engine"))
            with open(os.path.join(root, ".engine", "marker"), "w") as fh:
                fh.write("e")
            _git(root, "init", "-q")
            _commit(root, "engine only")                    # HEAD has .engine/marker, no .claude/settings.json
            import shutil
            shutil.rmtree(os.path.join(root, ".engine"))     # now .engine is missing too
            r = checkout_health.unstrand(cwd=root, apply=True)
            self.assertEqual(r["status"], "fixed")
            self.assertTrue(os.path.exists(os.path.join(root, ".engine", "marker")))  # .engine restored anyway

    def test_unresolvable_branch_refuses_without_mutating(self):
        # detached, no origin/HEAD, two branches and neither main nor master -> can't resolve -> REFUSE
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(tmp, "ambi")
            _git(root, "branch", "-M", "feature-a")          # rename current branch
            _git(root, "branch", "feature-b")                # a second branch
            _git(root, "checkout", "-q", "--detach", "HEAD")
            before = _head(root)
            r = checkout_health.unstrand(cwd=root, apply=True)
            self.assertEqual(r["status"], "needs-manual")
            self.assertEqual(r["reason"], "no-default-branch")
            self.assertEqual(_head(root), before)            # NO mutation — still where it was
            self.assertIn("detached", checkout_health.detect_strand(cwd=root)["states"])

    def test_dry_run_mutates_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(tmp, "dry", detach=True)
            before = _head(root)
            r = checkout_health.unstrand(cwd=root, apply=False)
            self.assertFalse(r["applied"])
            self.assertEqual(r["status"], "fixable")
            self.assertEqual(_head(root), before)            # unchanged
            self.assertIsNotNone(checkout_health.detect_strand(cwd=root))  # still stranded

    def test_a_rescue_that_cannot_save_refuses_and_keeps_the_work(self):
        # defense-in-depth: if the rescue commit can't capture the dirty work, the fix REFUSES (needs-manual)
        # and never moves HEAD onward — the work stays intact on disk rather than being put at any risk.
        import unittest.mock as mock
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(tmp, "norescue", detach=True)
            with open(os.path.join(root, "wip.txt"), "w") as fh:
                fh.write("KEEP")
            real_ok = checkout_health._ok

            def fake_ok(cmd, cwd=None):
                return True if "commit" in cmd else real_ok(cmd, cwd=cwd)   # the commit reports success but no-ops

            with mock.patch.object(checkout_health, "_ok", side_effect=fake_ok):
                r = checkout_health.unstrand(cwd=root, apply=True)
            self.assertEqual(r["status"], "needs-manual")
            self.assertEqual(r["reason"], "rescue-failed")
            with open(os.path.join(root, "wip.txt")) as fh:
                self.assertEqual(fh.read(), "KEEP")        # the work is intact on disk, nothing lost

    def test_fix_source_names_no_destructive_git_tokens(self):
        # defense-in-depth: the fix must never reach for a force/destructive git operation. Scan for the
        # QUOTED command tokens (so a backtick mention in a docstring is not a false positive).
        with open(checkout_health.__file__, encoding="utf-8") as fh:
            src = fh.read()
        for token in ('"reset"', '"clean"', '"-f"', '"--force"', '"--ff-only"', '"--hard"',
                      '"drop"', '"clear"', '"push"'):
            self.assertNotIn(token, src, f"the un-stranding fix must never use the git token {token}")


if __name__ == "__main__":
    unittest.main()
