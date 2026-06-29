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
import json
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


def _gcommit(root: str, date: str, *args: str) -> None:
    """A git command with a FIXED author+committer date (YYYY-MM-DD) and identity — so merge-commit dates,
    and therefore the velocity span, are deterministic (never the wall clock)."""
    env = dict(os.environ, GIT_AUTHOR_DATE=f"{date}T12:00:00", GIT_COMMITTER_DATE=f"{date}T12:00:00")
    subprocess.run(["git", "-C", root, "-c", "user.email=e@x", "-c", "user.name=n", *args],
                   capture_output=True, text=True, check=False, env=env)


def _origin_and_work(tmp: str, *, merge_dates: list, touch_shared_on_last: bool = False) -> tuple:
    """A local 'origin' (default branch `main`, engine files + a tracked `shared.txt`) and a `work` clone of
    it. origin is then advanced by one MERGE commit per date in `merge_dates` (each merges a fresh side branch
    — a 'merged PR'); `work` stays at the seed, behind by len(merge_dates) merges. With touch_shared_on_last,
    the final PR also edits `shared.txt`, so a work-side edit to `shared.txt` will CLASH on fast-forward.
    Returns (work, origin). Dates ('YYYY-MM-DD') drive the deterministic velocity span. The behind detector
    fetches from this local origin — hermetic, no network."""
    origin = os.path.join(tmp, "origin")
    os.makedirs(os.path.join(origin, ".claude"))
    os.makedirs(os.path.join(origin, ".engine"))
    with open(os.path.join(origin, ".claude", "settings.json"), "w") as fh:
        fh.write("{}")
    with open(os.path.join(origin, ".engine", "marker"), "w") as fh:
        fh.write("e")           # a tracked file so .engine survives the clone (git does not track empty dirs)
    with open(os.path.join(origin, "shared.txt"), "w") as fh:
        fh.write("base\n")
    _git(origin, "init", "-q", "-b", "main")
    base = merge_dates[0] if merge_dates else "2026-06-01"
    _gcommit(origin, base, "add", "-A")
    _gcommit(origin, base, "commit", "-q", "-m", "seed")
    work = os.path.join(tmp, "work")
    subprocess.run(["git", "clone", "-q", origin, work], capture_output=True, text=True, check=False)
    for i, date in enumerate(merge_dates, start=1):
        _git(origin, "checkout", "-q", "-b", f"pr{i}", "main")
        with open(os.path.join(origin, f"f{i}.txt"), "w") as fh:
            fh.write(f"pr{i}\n")
        if touch_shared_on_last and i == len(merge_dates):
            with open(os.path.join(origin, "shared.txt"), "w") as fh:
                fh.write(f"origin change in pr{i}\n")
        _gcommit(origin, date, "add", "-A")
        _gcommit(origin, date, "commit", "-q", "-m", f"work {i}")
        _git(origin, "checkout", "-q", "main")
        _gcommit(origin, date, "merge", "--no-ff", "-q", "-m", f"Merge pull request #{i}", f"pr{i}")
    return work, origin


class TestBehindOrigin(unittest.TestCase):
    """The ONLINE behind-origin tail (#335): fires only for an on-default-branch, clean-fast-forwardable
    checkout that is missing MORE merged work than the project's own pace makes normal — and never mutates."""

    def test_fires_when_missing_exceeds_velocity_bar(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 4 merges over 4 distinct days -> span 3 -> per_day ~1.33 -> threshold 1; missing 4 > 1 -> FIRES
            work, _ = _origin_and_work(tmp, merge_dates=["2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05"])
            r = checkout_health.detect_behind_origin(cwd=work, do_fetch=True)
            self.assertIsNotNone(r)
            self.assertEqual(r["state"], "behind")
            self.assertEqual(r["missing"], 4)
            self.assertEqual(r["branch"], "main")
            self.assertEqual(r["latest"], "2026-06-05")     # newest missing merge's date, for the felt line

    def test_quiet_when_below_velocity_bar(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 4 merges ALL the same day -> span 1 -> threshold 4; missing 4 NOT > 4 -> quiet (normal drift)
            work, _ = _origin_and_work(tmp, merge_dates=["2026-06-02"] * 4)
            self.assertIsNone(checkout_health.detect_behind_origin(cwd=work, do_fetch=True))

    def test_none_on_a_feature_branch(self):
        # 'behind origin/main' is the NORMAL state on a working feature branch -> not this signal
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_work(tmp, merge_dates=["2026-06-02", "2026-06-04", "2026-06-06"])
            _git(work, "checkout", "-q", "-b", "my-feature")
            self.assertIsNone(checkout_health.detect_behind_origin(cwd=work, do_fetch=True))

    def test_none_when_detached(self):
        # a detached HEAD is the strand detector's territory, not this tail
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_work(tmp, merge_dates=["2026-06-02", "2026-06-04", "2026-06-06"])
            _git(work, "checkout", "-q", "--detach", "HEAD")
            self.assertIsNone(checkout_health.detect_behind_origin(cwd=work, do_fetch=True))

    def test_none_when_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_work(tmp, merge_dates=[])   # origin never advanced -> current
            self.assertIsNone(checkout_health.detect_behind_origin(cwd=work, do_fetch=True))

    def test_none_when_diverged(self):
        # a local commit on work's main makes HEAD no longer an ancestor of origin/main -> not a clean behind
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_work(tmp, merge_dates=["2026-06-02", "2026-06-04", "2026-06-06"])
            with open(os.path.join(work, "local.txt"), "w") as fh:
                fh.write("local\n")
            _commit(work, "local divergent work")
            self.assertIsNone(checkout_health.detect_behind_origin(cwd=work, do_fetch=True))

    def test_fetch_leaves_working_tree_and_head_unchanged(self):
        # the online fetch touches ONLY the remote-tracking ref — never HEAD or the working tree (read-only)
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_work(tmp, merge_dates=["2026-06-02", "2026-06-04"])
            head_before = _head(work)
            with open(os.path.join(work, "shared.txt")) as fh:
                shared_before = fh.read()
            checkout_health.detect_behind_origin(cwd=work, do_fetch=True)   # performs the fetch
            self.assertEqual(_head(work), head_before)
            with open(os.path.join(work, "shared.txt")) as fh:
                self.assertEqual(fh.read(), shared_before)


class TestCatchUp(unittest.TestCase):
    """The fast-forward correction: LOSSLESS by construction (`git merge --ff-only`). Brings a clean behind
    checkout current, keeps unrelated uncommitted edits, REFUSES (no mutation, no loss) a clash or divergence."""

    def test_up_to_date_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_work(tmp, merge_dates=[])
            self.assertEqual(checkout_health.catch_up(cwd=work, apply=True, do_fetch=True)["status"], "healthy")

    def test_clean_fast_forward_brings_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_work(tmp, merge_dates=["2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05"])
            r = checkout_health.catch_up(cwd=work, apply=True, do_fetch=True)
            self.assertEqual(r["status"], "fixed")
            self.assertEqual(r["brought_in"], 4)
            self.assertIsNone(checkout_health.detect_behind_origin(cwd=work, do_fetch=True))   # current now

    def test_dry_run_mutates_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_work(tmp, merge_dates=["2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05"])
            before = _head(work)
            r = checkout_health.catch_up(cwd=work, apply=False, do_fetch=True)
            self.assertEqual(r["status"], "behind")
            self.assertFalse(r["applied"])
            self.assertEqual(_head(work), before)

    def test_unrelated_uncommitted_edit_survives_the_fast_forward(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_work(tmp, merge_dates=["2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05"])
            with open(os.path.join(work, "shared.txt"), "w") as fh:   # origin's PRs do NOT touch shared.txt
                fh.write("my local edit\n")
            r = checkout_health.catch_up(cwd=work, apply=True, do_fetch=True)
            self.assertEqual(r["status"], "fixed")
            with open(os.path.join(work, "shared.txt")) as fh:
                self.assertEqual(fh.read(), "my local edit\n")        # kept across the ff
            self.assertTrue(os.path.exists(os.path.join(work, "f4.txt")))   # incoming merged work present

    def test_clashing_uncommitted_edit_blocks_with_no_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            # origin's LAST PR edits shared.txt; work also edits shared.txt -> ff would clobber -> git refuses
            work, _ = _origin_and_work(tmp, merge_dates=["2026-06-02", "2026-06-04", "2026-06-06"],
                                       touch_shared_on_last=True)
            with open(os.path.join(work, "shared.txt"), "w") as fh:
                fh.write("MY UNSAVED EDIT\n")
            before = _head(work)
            r = checkout_health.catch_up(cwd=work, apply=True, do_fetch=True)
            self.assertEqual(r["status"], "blocked")
            self.assertFalse(r["applied"])
            self.assertEqual(_head(work), before)                     # no mutation
            with open(os.path.join(work, "shared.txt")) as fh:
                self.assertEqual(fh.read(), "MY UNSAVED EDIT\n")       # the unsaved edit intact -> nothing lost

    def test_diverged_is_refused_never_force_merged(self):
        # the behavioural guard that REPLACES the --ff-only source-scan: a diverged checkout is never advanced
        # or force-merged — detect_behind_origin gates it out, so catch_up makes no mutation.
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_work(tmp, merge_dates=["2026-06-02", "2026-06-04", "2026-06-06"])
            with open(os.path.join(work, "local.txt"), "w") as fh:
                fh.write("local\n")
            _commit(work, "divergent local work")
            before = _head(work)
            r = checkout_health.catch_up(cwd=work, apply=True, do_fetch=True)
            self.assertEqual(r["status"], "healthy")                  # diverged -> not behind -> nothing to do
            self.assertEqual(_head(work), before)                     # HEAD never moved (no ff, no merge)
            merges = checkout_health._run(["git", "-C", work, "rev-list", "--merges", "--count", "HEAD"])
            self.assertEqual((merges or "").strip(), "0")             # no merge commit was ever created


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
        # QUOTED command tokens (so a backtick mention in a docstring is not a false positive). `--ff-only`
        # is DELIBERATELY absent from this set (#335): it is git's own refuse-if-not-a-fast-forward guard —
        # the one sanctioned non-additive verb, used only by catch_up. The behavioural guard that it can never
        # force a diverged/clashing checkout is TestCatchUp (test_diverged_is_refused_never_force_merged,
        # test_clashing_uncommitted_edit_blocks_with_no_loss) — that, not this source-scan, protects the verb.
        with open(checkout_health.__file__, encoding="utf-8") as fh:
            src = fh.read()
        for token in ('"reset"', '"clean"', '"-f"', '"--force"', '"--hard"',
                      '"drop"', '"clear"', '"push"'):
            self.assertNotIn(token, src, f"the un-stranding fix must never use the git token {token}")


class TestPersistedDefaultBranch(unittest.TestCase):
    """#342 Slice 1: `_default_branch` reads the persisted manifest name FIRST, but only when it is a real
    local branch (a stale/wrong name must never redirect the detached-HEAD re-attach mutation); else it falls
    back to the live origin/HEAD → main/master → sole-branch resolution."""

    def _repo(self, root, *, branch, persisted):
        os.makedirs(os.path.join(root, ".engine"))
        _git(root, "init", "-q", "-b", branch)
        with open(os.path.join(root, "f.txt"), "w") as fh:
            fh.write("x")
        _git(root, "add", "-A")
        _git(root, "-c", "user.email=e@x", "-c", "user.name=n", "commit", "-q", "-m", "seed")
        manifest = {"engine_release": "0.0.0-dev", "packages": {"core": "0.0.0-dev"}, "identity": "solo"}
        if persisted is not None:
            manifest["default_branch"] = persisted
        with open(os.path.join(root, ".engine", "engine.json"), "w") as fh:
            json.dump(manifest, fh)

    def test_persisted_name_wins_over_the_fallback_guess(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "r")
            self._repo(root, branch="main", persisted="trunk")
            _git(root, "branch", "trunk")           # 'trunk' is a real local branch, distinct from main
            # the live fallback would pick 'main'; the validated persisted name wins
            self.assertEqual(checkout_health._default_branch(root), "trunk")

    def test_falls_back_when_persisted_name_is_not_a_local_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "r")
            self._repo(root, branch="main", persisted="renamed-away")   # stale: no such local branch
            self.assertEqual(checkout_health._default_branch(root), "main")   # ignored -> live fallback

    def test_falls_back_when_no_persisted_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "r")
            self._repo(root, branch="main", persisted=None)             # construction / pre-persistence repo
            self.assertEqual(checkout_health._default_branch(root), "main")


if __name__ == "__main__":
    unittest.main()
