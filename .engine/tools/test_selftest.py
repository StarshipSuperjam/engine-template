"""Regression fixture for the full-suite launcher (selftest.py).

Run: uv run --directory .engine --frozen -- python tools/selftest.py

Every case drives the launcher against a tiny SYNTHETIC suite written into a temp directory, never the
real `tools/` suite — so the fixture is fast and can never recurse when the real discover collects it.
The load-bearing assertions are the false-green ones (an import/collection error and a killed child
must each exit NON-ZERO, verdict = child exit status verbatim) and the no-hang one (a test that leaves a
background process running must not stall the launcher's teardown).
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

import selftest

_SELFTEST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "selftest.py")


def _write_suite(bodies: dict) -> str:
    """Materialise a synthetic suite (filename -> source) in a fresh temp dir; return the dir."""
    tmp = tempfile.mkdtemp(prefix="selftest-fixture-")
    for name, body in bodies.items():
        with open(os.path.join(tmp, name), "w") as fh:
            fh.write(textwrap.dedent(body))
    return tmp


class _LauncherCase(unittest.TestCase):
    """Base: a helper that runs the launcher against a synthetic suite and always cleans the temp dir."""

    def _run_launcher(self, bodies, *, interval="0.02", stall="0.05", timeout=30.0):
        if isinstance(bodies, str):
            bodies = {"test_synth.py": bodies}
        tmp = _write_suite(bodies)
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return subprocess.run(
            [sys.executable, _SELFTEST,
             "--start-dir", tmp, "--cwd", tmp,
             "--heartbeat-interval", interval, "--stall-threshold", stall,
             "--log-path", os.path.join(tmp, "run.log")],
            capture_output=True, text=True, timeout=timeout,
        )


class SelftestLauncher(_LauncherCase):

    def test_passing_suite_exits_zero_and_hides_leaked_stdout(self):
        """A green run exits 0, and -b buffering keeps a test's own print out of the summary."""
        r = self._run_launcher("""
            import unittest
            class T(unittest.TestCase):
                def test_ok(self):
                    print("SHOULD_BE_BUFFERED_AWAY")
                    self.assertTrue(True)
        """)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("PASSED", r.stdout)
        self.assertNotIn("SHOULD_BE_BUFFERED_AWAY", r.stdout)

    def test_failing_test_exits_nonzero_with_visible_traceback(self):
        r = self._run_launcher("""
            import unittest
            class T(unittest.TestCase):
                def test_bad(self):
                    self.assertEqual(2, 3, "synthetic failure")
        """)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("synthetic failure", r.stdout)
        self.assertIn("FAILED", r.stdout)

    def test_many_failures_list_is_complete_with_an_omission_notice(self):
        """Every failing test's id appears (the complete list), and when tracebacks are capped the
        output says so explicitly — so a broad regression never silently truncates."""
        methods = "\n".join(
            f"    def test_fail_{i}(self):\n        self.assertTrue(False, 'fail marker {i}')"
            for i in range(20)
        )
        r = self._run_launcher("import unittest\nclass T(unittest.TestCase):\n" + methods)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Failing tests (20):", r.stdout)          # complete count
        for i in range(20):
            self.assertIn(f"test_fail_{i}", r.stdout)           # every id listed
        self.assertIn("more failing test(s)", r.stdout)         # explicit omission notice (cap < 20)

    def test_import_error_is_a_nonzero_exit_not_a_false_green(self):
        r = self._run_launcher("""
            import a_module_that_does_not_exist_xyz
            import unittest
            class T(unittest.TestCase):
                def test_never(self):
                    pass
        """)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("PASSED", r.stdout)

    def test_killed_child_is_a_nonzero_exit_not_a_false_green(self):
        """The child exits mid-run with no unittest summary at all; the verdict must still be failure,
        with the child's own exit code propagated verbatim."""
        r = self._run_launcher("""
            import unittest, os
            class T(unittest.TestCase):
                def test_kills_process(self):
                    os._exit(7)
        """)
        self.assertEqual(r.returncode, 7)
        self.assertNotIn("PASSED", r.stdout)

    def test_background_grandchild_does_not_hang_or_stall_teardown(self):
        """A test that leaves a subprocess running (inheriting the output pipe) must NOT stall the
        launcher. Run at a REALISTIC heartbeat interval (5s): the child finishes instantly, so the
        launcher must too — exit detection must not be gated by the heartbeat cadence. This catches
        both the original infinite hang and the interval-scaled stall regression."""
        start = time.monotonic()
        r = self._run_launcher("""
            import unittest, subprocess, sys
            class T(unittest.TestCase):
                def test_leaves_background_process(self):
                    # No stdout= capture: the grandchild inherits the pipe and outlives this test.
                    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
                    self.assertTrue(True)
        """, interval="5", stall="5", timeout=20.0)
        elapsed = time.monotonic() - start
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # Must finish well under one heartbeat interval — proves exit is polled promptly, not gated.
        self.assertLess(elapsed, 4.0, "teardown was gated by the heartbeat interval (stall regression)")

    def test_stalled_test_reports_time_since_last_and_a_slow_or_stalled_flag(self):
        """A test past the stall threshold produces a live heartbeat carrying the required
        'since last completion' field and a slow-or-stalled flag — a slow run is legible, not a hang."""
        r = self._run_launcher("""
            import unittest, time
            class T(unittest.TestCase):
                def test_quick(self):
                    self.assertTrue(True)
                def test_slow(self):
                    time.sleep(0.5)
                    self.assertTrue(True)
        """, interval="0.05", stall="0.1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("since last completion", r.stdout)
        self.assertIn("slow or possibly stalled", r.stdout)

    def test_stdin_reading_test_gets_eof_and_does_not_hang(self):
        """The launcher forces the child's stdin to end-of-input, so a stdin-reading demo completes
        instead of blocking. The subprocess timeout is the hang guard."""
        r = self._run_launcher("""
            import unittest, sys
            class T(unittest.TestCase):
                def test_reads_stdin(self):
                    self.assertEqual(sys.stdin.read(), "")
        """, timeout=20.0)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("PASSED", r.stdout)

    def test_sigint_tears_down_promptly_without_hanging(self):
        """SIGINT to the launcher is forwarded to the child's process group; the launcher must exit
        promptly (well under the test's own runtime) with a non-zero status, never hang."""
        tmp = _write_suite({"test_synth.py": textwrap.dedent("""
            import unittest, time
            class T(unittest.TestCase):
                def test_long(self):
                    time.sleep(30)
                    self.assertTrue(True)
        """)})
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        proc = subprocess.Popen(
            [sys.executable, _SELFTEST, "--start-dir", tmp, "--cwd", tmp,
             "--heartbeat-interval", "0.1", "--log-path", os.path.join(tmp, "run.log")],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        self.addCleanup(proc.stdout.close)
        time.sleep(1.0)  # let it get into the run
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            self.fail("launcher hung after SIGINT instead of tearing down")
        self.assertNotEqual(proc.returncode, 0)

    def test_log_path_is_announced_so_a_session_can_read_full_output(self):
        r = self._run_launcher("""
            import unittest
            class T(unittest.TestCase):
                def test_ok(self):
                    self.assertTrue(True)
        """)
        self.assertIn("Full output:", r.stdout)

    def test_concurrent_runs_do_not_cross_contaminate(self):
        """Two launchers running at once — this repo's normal multi-session model — each report their
        OWN result. Both use the DEFAULT log path (no --log-path), so this exercises the unique
        per-run log + in-memory printout: no shared-file clobber, no cross-shown failures."""
        import concurrent.futures

        def run(marker, should_fail):
            tmp = tempfile.mkdtemp(prefix="selftest-conc-")
            self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
            assertion = f"self.assertTrue(False, {marker!r})" if should_fail else "self.assertTrue(True)"
            with open(os.path.join(tmp, "test_synth.py"), "w") as fh:
                fh.write("import unittest\nclass T(unittest.TestCase):\n"
                         f"    def test_x(self):\n        {assertion}\n")
            return subprocess.run(
                [sys.executable, _SELFTEST, "--start-dir", tmp, "--cwd", tmp,
                 "--heartbeat-interval", "0.05"],  # NB: no --log-path → default unique per-run log
                capture_output=True, text=True, timeout=30)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            f_fail = ex.submit(run, "MARKER_FAIL_A", True)
            f_pass = ex.submit(run, "MARKER_PASS_B", False)
            r_fail, r_pass = f_fail.result(timeout=40), f_pass.result(timeout=40)

        # Clean the two default logs the runs minted (path is announced in their output).
        for r in (r_fail, r_pass):
            for line in r.stdout.splitlines():
                if line.startswith("Full output: "):
                    try:
                        os.remove(line[len("Full output: "):].strip())
                    except OSError:
                        pass

        self.assertNotEqual(r_fail.returncode, 0)
        self.assertIn("MARKER_FAIL_A", r_fail.stdout)
        self.assertEqual(r_pass.returncode, 0, r_pass.stdout + r_pass.stderr)
        self.assertIn("PASSED", r_pass.stdout)
        self.assertNotIn("MARKER_FAIL_A", r_pass.stdout)  # the passing run never shows the other's failure

    def _run_default_log(self, body, timeout=30.0):
        """Run against the DEFAULT log path (no --log-path) and recover the path the launcher announced,
        so a test can check the keep-on-green / keep-on-failure log-retention lifecycle."""
        tmp = _write_suite({"test_synth.py": textwrap.dedent(body)})
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        r = subprocess.run(
            [sys.executable, _SELFTEST, "--start-dir", tmp, "--cwd", tmp, "--heartbeat-interval", "0.05"],
            capture_output=True, text=True, timeout=timeout)
        log_path = None
        marker = "Running the self-test suite (log: "
        for line in r.stdout.splitlines():
            if line.startswith(marker) and line.endswith(")"):
                log_path = line[len(marker):-1]
                break
        if log_path:
            self.addCleanup(lambda p=log_path: os.path.exists(p) and os.remove(p))
        return r, log_path

    def test_default_log_kept_on_green_and_on_failure(self):
        """The log is kept whether the run passes or fails, and its path is announced both ways — so a
        session can always read its own run and never mistakes a vanished log for a failure."""
        r_ok, log_ok = self._run_default_log("""
            import unittest
            class T(unittest.TestCase):
                def test_ok(self):
                    self.assertTrue(True)
        """)
        self.assertEqual(r_ok.returncode, 0)
        self.assertIsNotNone(log_ok)
        self.assertTrue(os.path.exists(log_ok), "a clean run must keep its log")
        self.assertIn("Full output:", r_ok.stdout)

        r_bad, log_bad = self._run_default_log("""
            import unittest
            class T(unittest.TestCase):
                def test_bad(self):
                    self.assertTrue(False, "keep me")
        """)
        self.assertNotEqual(r_bad.returncode, 0)
        self.assertIsNotNone(log_bad)
        self.assertTrue(os.path.exists(log_bad), "a failing run must keep its log")
        self.assertIn("Full output:", r_bad.stdout)

    def test_sweep_removes_stale_logs_and_keeps_fresh(self):
        """The startup sweep deletes THIS user's run logs older than a day and leaves fresh ones."""
        sys.path.insert(0, os.path.dirname(_SELFTEST))
        import selftest  # the module under test

        tmp = tempfile.gettempdir()
        prefix = selftest._user_log_prefix()
        stale = os.path.join(tmp, f"{prefix}SWEEPTEST-stale.log")
        fresh = os.path.join(tmp, f"{prefix}SWEEPTEST-fresh.log")
        for path in (stale, fresh):
            with open(path, "w") as fh:
                fh.write("x")
            self.addCleanup(lambda p=path: os.path.exists(p) and os.remove(p))
        old = time.time() - selftest._LOG_MAX_AGE_S - 3600
        os.utime(stale, (old, old))

        selftest._sweep_stale_logs()

        self.assertFalse(os.path.exists(stale), "a stale run log should be swept")
        self.assertTrue(os.path.exists(fresh), "a fresh run log should survive")

    def test_unwritable_log_path_fails_cleanly(self):
        """A bad --log-path yields a clean one-line message and a non-zero exit, not a raw traceback."""
        r = subprocess.run(
            [sys.executable, _SELFTEST, "--start-dir", ".", "--cwd", ".",
             "--log-path", os.path.join("no_such_dir_xyz", "run.log")],
            capture_output=True, text=True, timeout=20.0,
        )
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("cannot open the run log", r.stdout + r.stderr)
        self.assertNotIn("Traceback (most recent call last)", r.stdout + r.stderr)


# --------------------------------------------------------------------------------------------------
# Focused runs and the run record. Everything above this line is the launcher's ORIGINAL fixture and is
# byte-identical to its state before this change — preservation is verified by diffing that region, not
# by counting `def test_`, which also matches the synthetic suite bodies these fixtures embed as strings.
# --------------------------------------------------------------------------------------------------


_SELECTION_SCHEMA = "selftest-selection.v1"


def _selection(modules, classification="focused", code=None):
    """A selection manifest the launcher can be handed directly, without a git repository to derive
    one from — which is why `--selection-path` exists as a hidden flag."""
    return {
        "schema_version": _SELECTION_SCHEMA,
        "classification": classification,
        "changed_from": "fixture-base",
        "changed_paths": [],
        "full_reason": None if classification == "focused"
                       else {"code": code or "path-not-classifiable", "detail": "fixture"},
        "selected": [{"module": m, "path": f".engine/tools/{m}.py",
                      "reason": {"code": "changed-test-module", "detail": "fixture"}}
                     for m in modules],
    }


_CLEAN = """
    import unittest
    class T(unittest.TestCase):
        def test_ok(self): pass
"""
_ALSO_CLEAN = """
    import unittest
    class T(unittest.TestCase):
        def test_fine(self): pass
"""
_BAD_IMPORT = "import a_module_that_is_definitely_not_installed\n"
_SETUP_FAILS = """
    import unittest
    def setUpModule():
        raise RuntimeError("module setup exploded")
    class T(unittest.TestCase):
        def test_never_runs(self): pass
"""


class FocusedRuns(unittest.TestCase):
    """The launcher driven with a selection, against synthetic suites."""

    def _run(self, bodies, selection=None, *, record=True, timeout=60.0):
        tmp = _write_suite(bodies)
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        cmd = [sys.executable, _SELFTEST, "--start-dir", tmp, "--cwd", tmp,
               "--heartbeat-interval", "0.05", "--stall-threshold", "0.1",
               "--log-path", os.path.join(tmp, "run.log")]
        record_path = os.path.join(tmp, "record.json")
        if record:
            cmd += ["--run-record-path", record_path]
        if selection is not None:
            sel_path = os.path.join(tmp, "selection.json")
            with open(sel_path, "w") as fh:
                json.dump(selection, fh)
            cmd += ["--selection-path", sel_path]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        loaded = None
        if record and os.path.exists(record_path):
            with open(record_path) as fh:
                loaded = json.load(fh)
        return proc, loaded

    def test_a_focused_run_executes_only_the_selected_modules(self):
        proc, record = self._run(
            {"test_one.py": _CLEAN, "test_two.py": _ALSO_CLEAN},
            _selection(["test_one"]))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(record["scope"], "focused")
        self.assertEqual(record["executed"]["case_count"], 1)
        self.assertEqual([e["module"] for e in record["modules"]], ["test_one"])

    def test_a_focused_run_still_reports_the_complete_inventory(self):
        """Half of what makes a focused record unusable as merge evidence: what was NOT run is visible
        by subtraction, because the inventory is taken from the canonical full discovery."""
        _, record = self._run(
            {"test_one.py": _CLEAN, "test_two.py": _ALSO_CLEAN, "test_three.py": _CLEAN},
            _selection(["test_one"]))
        self.assertEqual(record["inventory"]["module_count"], 3)
        self.assertEqual(record["executed"]["case_count"], 1)

    def test_a_module_that_cannot_be_imported_is_never_filtered_out(self):
        """The load-bearing false-green case. A module that fails to import is presented by unittest as
        a synthetic case attributed to the LOADER, not to the module that broke — so a filter keyed on
        module name alone discards it, the filtered suite runs clean, and the child exits 0. Here the
        broken module is deliberately NOT selected; the run must still go red."""
        proc, record = self._run(
            {"test_one.py": _CLEAN, "test_broken.py": _BAD_IMPORT},
            _selection(["test_one"]))
        self.assertNotEqual(proc.returncode, 0,
                            "an unimportable module must fail the run even when it was not selected")
        self.assertEqual(record["verdict"], "failed")
        self.assertIn("test_broken", [p["module"] for p in record["problems"]])

    def test_a_selection_naming_modules_this_tree_does_not_produce_is_refused(self):
        """The other false-green case. An empty filtered suite is reported by unittest as SUCCESSFUL,
        so a selection that matches nothing would otherwise be a clean green having run nothing."""
        proc, record = self._run({"test_one.py": _CLEAN}, _selection(["test_absent"]))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(record["verdict"], "selection-unmatched")
        self.assertIn("test_absent", record["detail"])
        self.assertIn("does not produce", proc.stdout + proc.stderr)

    def test_a_full_classification_runs_everything_and_says_so(self):
        proc, record = self._run(
            {"test_one.py": _CLEAN, "test_two.py": _ALSO_CLEAN},
            _selection([], classification="full", code="path-not-classifiable"))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(record["scope"], "full")
        self.assertEqual(record["executed"]["case_count"], 2)
        self.assertIn("COMPLETE inventory", proc.stdout + proc.stderr + "")


class RunRecord(unittest.TestCase):

    def _run(self, bodies, selection=None, **kw):
        return FocusedRuns._run(self, bodies, selection, **kw)

    def test_a_record_is_written_on_a_pass_and_on_a_failure(self):
        _, passed = self._run({"test_one.py": _CLEAN})
        self.assertEqual(passed["verdict"], "passed")
        self.assertEqual(passed["exit_status"], 0)
        _, failed = self._run({"test_bad.py": """
            import unittest
            class T(unittest.TestCase):
                def test_no(self): self.fail("nope")
        """})
        self.assertEqual(failed["verdict"], "failed")
        self.assertEqual(failed["exit_status"], 1)

    def test_a_module_level_setup_failure_is_recorded(self):
        """A `setUpModule` failure is reported straight to the result's error hook and NEVER passes
        through a start or stop event, so a failure list derived from the progress stream would be
        silently empty while the exit status said FAILED."""
        proc, record = self._run({"test_setupmod.py": _SETUP_FAILS})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("test_setupmod", [p["module"] for p in record["problems"]])

    def test_the_recorded_verdict_agrees_with_the_exit_status(self):
        for bodies, expected in ((({"test_one.py": _CLEAN}), 0),
                                 (({"test_broken.py": _BAD_IMPORT}), 1)):
            proc, record = self._run(bodies)
            with self.subTest(expected=expected):
                self.assertEqual(proc.returncode, expected)
                self.assertEqual(record["exit_status"], proc.returncode)

    def test_the_recorded_log_digest_matches_the_log_actually_written(self):
        """The digest is taken from the parent's in-memory capture, which is byte-identical to the file
        by construction — so it cannot depend on a buffered flush completing during teardown."""
        tmp = _write_suite({"test_one.py": _CLEAN})
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        log_path = os.path.join(tmp, "run.log")
        record_path = os.path.join(tmp, "record.json")
        subprocess.run(
            [sys.executable, _SELFTEST, "--start-dir", tmp, "--cwd", tmp,
             "--heartbeat-interval", "0.05", "--log-path", log_path,
             "--run-record-path", record_path],
            capture_output=True, text=True, timeout=60.0)
        with open(record_path) as fh:
            record = json.load(fh)
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            on_disk = fh.read()
        self.assertEqual(record["log"]["sha256"], selftest._sha256_text(on_disk))

    def test_a_killed_child_still_leaves_a_record(self):
        """The run record must survive the outcome it is most needed for."""
        proc, record = self._run({"test_suicide.py": """
            import os, signal, unittest
            class T(unittest.TestCase):
                def test_dies(self):
                    os.kill(os.getpid(), signal.SIGKILL)
        """})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIsNotNone(record, "a killed child must still leave a record")
        # The parent propagates the child's raw returncode, which Python reports as a NEGATIVE
        # number for a signal death; the status the shell sees is that value modulo 256.
        self.assertIn(record["exit_status"], (proc.returncode, proc.returncode - 256))
        self.assertIn(record["verdict"], ("crashed", "failed"))

    def test_the_parent_side_log_failure_exit_still_leaves_a_record(self):
        """One of the three exits the child never reaches."""
        tmp = tempfile.mkdtemp(prefix="selftest-record-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        record_path = os.path.join(tmp, "record.json")
        proc = subprocess.run(
            [sys.executable, _SELFTEST, "--start-dir", ".", "--cwd", ".",
             "--log-path", os.path.join("no_such_dir_xyz", "run.log"),
             "--run-record-path", record_path],
            capture_output=True, text=True, timeout=30.0)
        self.assertNotEqual(proc.returncode, 0)
        with open(record_path) as fh:
            record = json.load(fh)
        self.assertEqual(record["verdict"], "log-unavailable")

    def test_the_record_names_one_validation_command_and_declares_its_scope(self):
        """A record that did not say what it covered would read as 'this tree is validated'."""
        _, record = self._run({"test_one.py": _CLEAN})
        self.assertEqual(record["attests"], "engine-selftest")
        self.assertIn(record["scope"], ("full", "focused"))
        self.assertTrue(record["nested_sentinel"],
                        "a launcher run always sets the nested sentinel, which is why some tests skip")

    def test_the_record_validates_against_its_own_schema(self):
        import jsonschema
        import validate as _validate
        schemas_dir = os.path.join(_validate.ENGINE_DIR, "schemas")
        with open(os.path.join(schemas_dir, "selftest-run-record.v1.json")) as fh:
            record_schema = json.load(fh)
        with open(os.path.join(schemas_dir, "selftest-selection.v1.json")) as fh:
            selection_schema = json.load(fh)
        validator = jsonschema.Draft202012Validator(record_schema)
        for bodies, selection in (({"test_one.py": _CLEAN}, None),
                                  ({"test_one.py": _CLEAN, "test_two.py": _ALSO_CLEAN},
                                   _selection(["test_one"]))):
            _, record = self._run(bodies, selection)
            with self.subTest(selection=bool(selection)):
                validator.validate(record)
                if record["selection"] is not None:
                    # The record schema stays self-contained — this is the only place in the engine's
                    # schema corpus where one document embeds another, and a cross-file reference would
                    # oblige every consumer to carry a registry. The SELECTION schema is checked here as
                    # the authority for the nested object, and `selection_digest` carries its identity.
                    jsonschema.validate(record["selection"], selection_schema)
                    self.assertTrue(record["selection_digest"],
                                    "an embedded selection must carry its own digest")

    def test_the_scope_field_is_required_by_the_schema(self):
        """Required and non-defaultable, so a record cannot be silent about what it covered."""
        import jsonschema
        import validate as _validate
        with open(os.path.join(_validate.ENGINE_DIR, "schemas",
                               "selftest-run-record.v1.json")) as fh:
            record_schema = json.load(fh)
        _, record = self._run({"test_one.py": _CLEAN})
        record.pop("scope")
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(record, record_schema)


class NewFlagsAreDiscoverable(unittest.TestCase):

    def test_both_operator_facing_flags_appear_in_the_help(self):
        """Every pre-existing flag is hidden and the docstring says they are fixture-only; a capability
        nobody can find is not delivered."""
        proc = subprocess.run([sys.executable, _SELFTEST, "--help"],
                              capture_output=True, text=True, timeout=30.0)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--changed-from", proc.stdout)
        self.assertIn("--run-record-path", proc.stdout)
        self.assertNotIn("--selection-path", proc.stdout,
                         "the hand-off path is an internal seam, not an operator flag")


if __name__ == "__main__":
    unittest.main()
