"""Tests for `release_gate.py` — the cut-time deployment gate.

FAST + INJECTED by design. The real gate (a full self-test suite inside a projected deployment, and real
practice upgrades from released baselines) takes many minutes and runs at CUT TIME, never in this per-PR
suite — moving that cost out of every pull request is the whole point of the slice. So these exercise the
gate's ORCHESTRATION and its fail-CLOSED contract with the heavy arms stubbed; the genuine operate/upgrade
proofs are the cut-time gate run and the first-run-retired `demo_664_release_gate.py`.

Every case is guarded `@skipUnless(_CONSTRUCTION)` — the home repo AND not a nested run — so that when Arm A's
in-projection suite re-collects this file (it ships to deployed repos), these cases skip rather than recurse
into the gate or fail against a tag-less projected tree.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate                              # noqa: E402
import release_gate as rg                    # noqa: E402

_CONSTRUCTION = rg._ccc._in_home_repo() and not os.environ.get(rg._NESTED_ENV)
_SKIP = "runs where a release is cut (the home repo, not a nested projection run)"


def _proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


@unittest.skipUnless(_CONSTRUCTION, _SKIP)
class TestBaselineSelection(unittest.TestCase):
    """`_upgrade_baselines` picks the released version tags at or above the clean-upgrade floor."""

    def test_at_or_above_floor_only(self):
        baselines = rg._upgrade_baselines()
        self.assertIn("v0.3.2", baselines)                 # the current floor
        self.assertIn("v0.4.0", baselines)                 # the latest release
        for below in ("v0.1.0", "v0.2.0", "v0.3.0", "v0.3.1"):
            self.assertNotIn(below, baselines, f"{below} is below the floor and must be excluded")
        self.assertEqual(baselines, sorted(set(baselines), key=lambda t: validate._ver_tuple(t[1:])))

    def test_v_prefix_stripped_in_floor_compare(self):
        # a bare-vs-v-prefixed mismatch would silently drop every baseline; prove the floor compare works
        self.assertTrue(all(t.startswith("v") for t in rg._upgrade_baselines()))


@unittest.skipUnless(_CONSTRUCTION, _SKIP)
class TestIsolationGuard(unittest.TestCase):
    """The belt-and-suspenders half of the ROOT-isolation guarantee refuses a non-throwaway target."""

    def test_refuses_home_root(self):
        with self.assertRaises(rg.GateError):
            rg._assert_isolated(validate.ROOT)

    def test_refuses_path_outside_tempdir(self):
        with self.assertRaises(rg.GateError):
            rg._assert_isolated(os.path.dirname(validate.ROOT))

    def test_allows_throwaway_tempdir(self):
        with tempfile.TemporaryDirectory() as d:
            rg._assert_isolated(d)                          # must not raise


@unittest.skipUnless(_CONSTRUCTION, _SKIP)
class TestFailClosed(unittest.TestCase):
    """A gate that cannot run — a setup GateError or ANY unexpected error — BLOCKS the cut (exit nonzero),
    never waves it through."""

    def _main_json(self):
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = rg.main(["--json"])
        return code, json.loads(buf.getvalue())

    def test_setup_error_blocks(self):
        with mock.patch.object(rg, "run_gate", side_effect=rg.GateError("projection could not be built")):
            code, out = self._main_json()
        self.assertEqual(code, 1)
        self.assertFalse(out["passed"])
        self.assertIn("projection could not be built", out["reason"])

    def test_unexpected_error_blocks(self):
        with mock.patch.object(rg, "run_gate", side_effect=ValueError("kaboom")):
            code, out = self._main_json()
        self.assertEqual(code, 1)
        self.assertFalse(out["passed"])
        self.assertIn("unexpected error", out["reason"])

    def test_red_result_blocks(self):
        with mock.patch.object(rg, "run_gate", return_value={"ran": True, "passed": False}):
            code, _ = self._main_json()
        self.assertEqual(code, 1)

    def test_green_result_passes(self):
        with mock.patch.object(rg, "run_gate", return_value={"ran": True, "passed": True}):
            code, _ = self._main_json()
        self.assertEqual(code, 0)


@unittest.skipUnless(_CONSTRUCTION, _SKIP)
class TestInertWhenDeployed(unittest.TestCase):
    """On a non-home checkout the gate is inert (ran=False) and passes — a deployed repo runs the suite in its
    own engine-ci. The workflow, not the tool, decides an engine cut must actually have run."""

    def test_not_home_repo_is_inert_pass(self):
        with mock.patch.object(rg._ccc, "_in_home_repo", return_value=False):
            result = rg.run_gate()
        self.assertFalse(result["ran"])
        self.assertTrue(result["passed"])


@unittest.skipUnless(_CONSTRUCTION, _SKIP)
class TestHomeTreeGuard(unittest.TestCase):
    """If the gate ever leaves a change in the home working tree the cut is about to commit, it BLOCKS."""

    def test_home_tree_mutation_blocks(self):
        with mock.patch.object(rg._ccc, "_in_home_repo", return_value=True), \
             mock.patch.object(rg, "_archive_candidate", return_value="/tmp/nowhere"), \
             mock.patch.object(rg, "_arm_operates", return_value={"passed": True, "failures": []}), \
             mock.patch.object(rg, "_arm_upgrades", return_value={"passed": True, "failures": []}), \
             mock.patch.object(rg, "_worktree_digest", side_effect=["BEFORE", "AFTER"]):
            result = rg.run_gate()
        self.assertTrue(result["ran"])
        self.assertFalse(result["passed"])
        self.assertTrue(result.get("home_tree_mutated"))


@unittest.skipUnless(_CONSTRUCTION, _SKIP)
class TestUpgradeArmReporting(unittest.TestCase):
    """`_upgrade_from` reads the practice-upgrade result and blocks on refusal, non-application, a hard gate
    finding, a driver crash, OR a missing practice-path note (a silent network fetch of a real release)."""

    def _drive(self, result_obj=None, rc=0, stdout=None, stderr=""):
        out = stdout if stdout is not None else ("GATE_RESULT:" + json.dumps(result_obj))
        with mock.patch.object(rg, "_archive_baseline", return_value="/tmp/proj"), \
             mock.patch.object(rg, "_project_to_deployed", return_value=[]), \
             mock.patch.object(rg, "_assert_isolated", return_value=None), \
             mock.patch.object(rg, "_run", return_value=_proc(rc, out, stderr)):
            return rg._upgrade_from("v9.9.9", "/tmp/candidate")

    def _clean(self, **over):
        base = {"refused": False, "applied": True, "findings": [],
                "notes": ["(practice run — the pull request was not opened)"]}
        base.update(over)
        return base

    def test_clean_upgrade_passes(self):
        self.assertTrue(self._drive(self._clean())["passed"])

    def test_hard_finding_blocks(self):
        res = self._drive(self._clean(findings=[{"severity": "hard", "id": "engine/check/knowledge-coverage"}]))
        self.assertFalse(res["passed"])
        self.assertIn("blocking", res["detail"])

    def test_refusal_blocks(self):
        self.assertFalse(self._drive(self._clean(refused=True, reason="unreachable"))["passed"])

    def test_not_applied_blocks(self):
        self.assertFalse(self._drive(self._clean(applied=False))["passed"])

    def test_missing_practice_note_blocks(self):
        # no "practice run" note => the upgrade may have fetched a real release instead of the candidate
        self.assertFalse(self._drive(self._clean(notes=[]))["passed"])

    def test_driver_crash_blocks(self):
        self.assertFalse(self._drive(rc=1, stdout="", stderr="boom")["passed"])

    def test_no_gate_result_marker_blocks(self):
        self.assertFalse(self._drive(stdout="garbage with no marker")["passed"])


@unittest.skipUnless(_CONSTRUCTION, _SKIP)
class TestNoBaselinesFailsClosed(unittest.TestCase):
    """A checkout with no in-range baseline (shallow / tag-less) BLOCKS rather than reporting a vacuous pass."""

    def test_empty_baselines_blocks(self):
        with mock.patch.object(rg, "_run", return_value=_proc(0, "", "")):   # `git tag` lists nothing
            with self.assertRaises(rg.GateError):
                rg._arm_upgrades("/tmp/candidate")


@unittest.skipUnless(_CONSTRUCTION, _SKIP)
class TestRenderCopy(unittest.TestCase):
    """The operator-facing copy is plain language — never a check id or an internal arm token."""

    def test_inert_copy(self):
        self.assertIn("inert", rg._render({"ran": False, "passed": True}).lower())

    def test_pass_copy(self):
        self.assertIn("passed", rg._render({"ran": True, "passed": True}).lower())

    def test_blocked_copy_is_plain(self):
        text = rg._render({"ran": True, "passed": False,
                           "operates": {"passed": False}, "upgrades": {"passed": True}})
        self.assertIn("would not work", text.lower())
        self.assertIn("nothing was changed", text.lower())
        self.assertIn("operate", text.lower())
        for jargon in ("engine/check", "knowledge-coverage", "Arm A", "Arm B", "_reconcile", "DanglingImport"):
            self.assertNotIn(jargon, text)


if __name__ == "__main__":
    unittest.main()
