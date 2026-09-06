"""Regression fixture for the full-suite launcher (selftest.py).

Run: uv run --directory .engine --frozen -- python tools/selftest.py

Every case drives the launcher against a tiny SYNTHETIC suite written into a temp directory, never the
real `tools/` suite — so the fixture is fast and can never recurse when the real discover collects it.
The load-bearing assertions are the false-green ones (an import/collection error and a killed child
must each exit NON-ZERO, verdict = child exit status verbatim) and the no-hang one (a test that leaves a
background process running must not stall the launcher's teardown), plus the interrupt one: SIGINT to the
launcher tears the suite down promptly — and that test PROVES its premise (a child that can be interrupted)
through a beacon the synthetic suite writes, rather than assuming it from however the enclosing run happened
to be started (#1188).
"""
from __future__ import annotations

import io
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # importable standalone, whatever loaded first

import selftest

_SELFTEST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "selftest.py")


def _write_suite(bodies: dict) -> str:
    """Materialise a synthetic suite (filename -> source) in a fresh temp dir; return the dir."""
    tmp = tempfile.mkdtemp(prefix="selftest-fixture-")
    for name, body in bodies.items():
        with open(os.path.join(tmp, name), "w") as fh:
            fh.write(textwrap.dedent(body))
    return tmp


def _stop_launcher(proc) -> None:
    """Cleanup for a launcher a test may have left running: ask it to tear its child down (it forwards
    SIGTERM to the child's group), then kill it if it does not."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


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
        promptly (well under the test's own runtime) with a non-zero status, never hang.

        The premise — a child that CAN be interrupted — is proved, not assumed (#1188). A POSIX shell starts
        a `cmd &` job with SIGINT ignored, and that disposition inherits through exec; the launcher used to
        spawn its child before installing its own forwarding handlers, so a backgrounded validation run
        handed the ignore straight to the suite and this test read as a launcher hang. The launcher now
        installs its handlers first (a caught handler resets to default across exec; an ignore is
        preserved), and the synthetic suite writes a beacon recording the disposition it actually observed.
        The beacon doubles as the readiness signal: the test waits for it (bounded) instead of sleeping a
        guessed second, and a missing beacon or an 'ignored' one is a NAMED failure — never a fall-through to
        the exit-code check, which any fast failure would satisfy.
        """
        tmp = _write_suite({"test_synth.py": textwrap.dedent("""
            import os, signal, time, unittest
            class T(unittest.TestCase):
                def test_long(self):
                    seen = ("default" if signal.getsignal(signal.SIGINT) is signal.default_int_handler
                            else "ignored")
                    beacon = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sigint-disposition.txt")
                    with open(beacon + ".tmp", "w") as fh:
                        fh.write(seen)
                    os.replace(beacon + ".tmp", beacon)   # atomic: the reader never sees a half-written beacon
                    time.sleep(30)
                    self.assertTrue(True)
        """)})
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        beacon = os.path.join(tmp, "sigint-disposition.txt")
        # Construct the ADVERSE premise here rather than inheriting it from however this run was started:
        # start the launcher with SIGINT already ignored — exactly what a shell hands a `cmd &` job — so
        # the beacon can read 'default' only if the launcher installed its own handler before spawning the
        # child. That makes the test bite in a foreground run and on CI, not only when backgrounded.
        inherited = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            proc = subprocess.Popen(
                [sys.executable, _SELFTEST, "--start-dir", tmp, "--cwd", tmp,
                 "--heartbeat-interval", "0.1", "--log-path", os.path.join(tmp, "run.log")],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
        finally:
            signal.signal(signal.SIGINT, inherited)
        self.addCleanup(proc.stdout.close)
        self.addCleanup(_stop_launcher, proc)

        # Readiness: wait (bounded) for the beacon the synthetic suite writes on entering its test body.
        deadline = time.monotonic() + 10.0
        while not os.path.exists(beacon) and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if not os.path.exists(beacon):
            self.fail("the synthetic suite never wrote its SIGINT-disposition beacon (launcher "
                      f"{'exited with ' + str(proc.returncode) if proc.poll() is not None else 'still running'}), "
                      "so the child never reached the test body and nothing about SIGINT forwarding was "
                      "exercised — this is not a teardown verdict")
        with open(beacon, encoding="utf-8") as fh:
            seen = fh.read().strip()
        self.assertEqual(seen, "default",
                         "the launcher's child inherited SIGINT ignored, so a forwarded interrupt could never "
                         "reach it: the launcher must install its forwarding handlers BEFORE spawning the child "
                         "(a caught handler resets to default across exec; an ignore is inherited)")

        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            self.fail("launcher hung after SIGINT instead of tearing down")
        self.assertNotEqual(proc.returncode, 0)

    def test_a_signal_before_the_child_exists_behaves_as_the_default_disposition(self):
        """The forwarding handlers are installed before the spawn, so there is a window in which a signal
        arrives with no child to forward to. In that window the handler must do what the default disposition
        would have done — SIGINT interrupts, SIGTERM terminates with the conventional 128+signal status —
        never swallow the signal, and never leave a launcher that nothing can stop."""
        with self.assertRaises(KeyboardInterrupt):
            selftest._forward_signal(None, signal.SIGINT)
        with self.assertRaises(SystemExit) as caught:
            selftest._forward_signal(None, signal.SIGTERM)
        self.assertEqual(caught.exception.code, 128 + int(signal.SIGTERM))

    def test_a_signal_after_the_spawn_is_forwarded_to_the_whole_child_group(self):
        """Once the child exists the same handler forwards to its PROCESS GROUP (the child runs in its own
        session), so demo grandchildren are torn down with it; a vanished group is not an error."""
        sent = []

        class _Child:
            pid = 4242

        with mock.patch.object(selftest.os, "getpgid", return_value=4242), \
                mock.patch.object(selftest.os, "killpg", side_effect=lambda group, sig: sent.append((group, sig))):
            selftest._forward_signal(_Child(), signal.SIGTERM)
        self.assertEqual(sent, [(4242, signal.SIGTERM)])
        with mock.patch.object(selftest.os, "getpgid", side_effect=ProcessLookupError):
            selftest._forward_signal(_Child(), signal.SIGINT)   # already gone: swallowed, not raised

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
# Focused runs and the run record. Every pre-existing CASE above this line is unchanged: the whole file
# diffs against the Build base with zero deletions. Two import lines near the top were added, so the
# region is not literally byte-identical and this comment no longer claims it is. Preservation is
# verified by that diff, never by counting `def test_` — a count also matches the synthetic suite bodies
# these fixtures embed as strings, which is how a figure in this Build's own plan came out wrong.
# --------------------------------------------------------------------------------------------------


_SELECTION_SCHEMA = "selftest-selection.v1"


def _selection(modules, classification="focused", code=None, project_paths=()):
    """A selection manifest the launcher can be handed directly, without a git repository to derive
    one from — which is why `--selection-path` exists as a hidden flag."""
    return {
        "schema_version": _SELECTION_SCHEMA,
        "classification": classification,
        "changed_from": "fixture-base",
        "changed_paths": list(project_paths),
        "full_reason": None if classification in ("focused", "project-only")
                       else {"code": code or "path-not-classifiable", "detail": "fixture"},
        "exempt_paths": [],
        "project_paths": list(project_paths),
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

    def test_a_selected_module_that_fails_to_import_reports_the_real_error(self):
        """The realistic mid-edit case, and the one the first fixture pair missed between them.

        One case covered an unimportable module that was NOT selected; another covered a selected module
        that did not exist. Neither covered the ordinary one — you broke the import in the very file you
        are editing — which was misdiagnosed as a module "this tree does not produce", with the actual
        ImportError never shown. Two reviewers hit it independently."""
        proc, record = self._run(
            {"test_one.py": _CLEAN, "test_broken.py": _BAD_IMPORT},
            _selection(["test_one", "test_broken"]))
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(record["verdict"], "failed",
                         "a broken selected module exists; it must not be called absent")
        self.assertIn("test_broken", [p["module"] for p in record["problems"]])
        self.assertIn("a_module_that_is_definitely_not_installed", proc.stdout + proc.stderr,
                      "the real import error must reach the reader")

    def test_a_focused_selection_naming_no_module_at_all_is_refused(self):
        """An empty filtered suite reports as successful, so a focused run naming nothing would be a
        clean green having executed nothing. The selector cannot emit this; the runner can be handed it."""
        proc, record = self._run({"test_one.py": _CLEAN}, _selection([]))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(record["verdict"], "selection-unmatched")
        self.assertEqual(record["executed"]["case_count"], 0)

    def test_a_focused_run_says_so_in_its_closing_banner(self):
        """The opening announcement is hundreds of lines up the buffer after a long run."""
        proc, _ = self._run({"test_one.py": _CLEAN, "test_two.py": _ALSO_CLEAN},
                            _selection(["test_one"]))
        tail = proc.stdout[proc.stdout.rfind("Self-tests"):]
        self.assertIn("Focused run", tail)
        self.assertIn("NOT a full-inventory result", tail)

    def test_a_project_only_run_keeps_its_name_in_the_record_and_the_banner(self):
        """A deployed project's own change: the guard alone runs, and neither the record nor the banner
        may present that as an ordinary focused narrowing or as a full result."""
        proc, record = self._run(
            {"test_one.py": _CLEAN, "test_two.py": _ALSO_CLEAN},
            _selection(["test_one"], classification="project-only", project_paths=["src/app.py"]))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(record["scope"], "project-only")
        self.assertEqual(record["selection"]["classification"], "project-only")
        self.assertEqual([e["module"] for e in record["modules"]], ["test_one"])
        tail = proc.stdout[proc.stdout.rfind("Self-tests"):]
        self.assertIn("Project-only run", tail)
        self.assertIn("inventory did not run", tail)
        self.assertIn("no product validation", tail)
        self.assertIn("NOT a full-inventory result", tail)

    def test_a_crashed_focused_run_does_not_claim_it_was_a_full_one(self):
        """`scope` is the field the schema calls its load-bearing honesty field; a crashed focused run
        used to report `full`, meaning the complete inventory had run."""
        _, record = self._run({"test_suicide.py": """
            import os, signal, unittest
            class T(unittest.TestCase):
                def test_dies(self):
                    os.kill(os.getpid(), signal.SIGKILL)
        """}, _selection(["test_suicide"]))
        self.assertEqual(record["scope"], "focused")

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
                    # The obligation asks that the embedded copy's digest MATCH the standalone
                    # serialization, not merely that one is present — a truthiness check would pass on
                    # any string at all, which is the gap a reviewer named.
                    import selftest_select
                    self.assertEqual(record["selection_digest"],
                                     selftest_select.digest(record["selection"]),
                                     "the embedded selection's digest must match its own canonical bytes")

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


class RecordHonesty(unittest.TestCase):

    def _run(self, bodies, selection=None, **kw):
        return FocusedRuns._run(self, bodies, selection, **kw)

    def test_the_nested_sentinel_is_recorded_true_on_a_crashed_run(self):
        """The record is written by the PARENT, which sets the sentinel for the child it spawns —
        so reading the parent's own environment reported False on every ordinary run. An unrequested
        regression that rode in on an unrelated fix, contradicting the schema's own statement."""
        _, record = self._run({"test_suicide.py": """
            import os, signal, unittest
            class T(unittest.TestCase):
                def test_dies(self):
                    os.kill(os.getpid(), signal.SIGKILL)
        """})
        self.assertTrue(record["nested_sentinel"])

    def test_a_record_is_written_with_owner_only_permissions(self):
        """The run log's own docstring already argued this posture; the new files must match it.

        The first version of this test asserted only that the file existed, which passed identically
        against the commit before the permission fix — a test named for a property it never checked."""
        tmp = _write_suite({"test_one.py": _CLEAN})
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        record_path = os.path.join(tmp, "record.json")
        subprocess.run([sys.executable, _SELFTEST, "--start-dir", tmp, "--cwd", tmp,
                        "--heartbeat-interval", "0.05", "--log-path", os.path.join(tmp, "run.log"),
                        "--run-record-path", record_path], capture_output=True, text=True, timeout=60)
        self.assertTrue(os.path.exists(record_path))
        self.assertEqual(stat.S_IMODE(os.stat(record_path).st_mode), 0o600,
                         "the record must be owner-only, like the run log beside it")

    def test_a_run_whose_bookkeeping_breaks_says_so_without_changing_its_verdict(self):
        """The one state `record_incomplete` exists to describe, actually driven.

        Two reviewers noticed the field could only ever be observed as false, so nothing showed what it
        looks like when it means something. Driven here at the result object directly rather than
        through a synthetic suite: the guard sits inside `_StructuredResult`'s own hooks, and reaching
        it end-to-end depends on which of unittest's internals happens to touch a case first — which
        would make the test about unittest rather than about the guard. A case whose printed form
        raises is exactly what the guard catches: the entry is dropped, the flag is set, and — the
        point of the whole thing — the base class's own verdict is untouched."""
        class Hostile(unittest.TestCase):
            def __str__(self):
                raise RuntimeError("this case refuses to describe itself")

            def runTest(self):
                pass

        case = Hostile()
        result = selftest._StructuredResult(io.StringIO(), True, 1, progress_write=None)
        result.addFailure(case, (AssertionError, AssertionError("a real failure"), None))

        self.assertTrue(result._record_broke,
                        "a bookkeeping failure must be recorded, not swallowed")
        self.assertEqual(result.problems, [], "the entry it could not build is dropped")
        self.assertFalse(result.wasSuccessful(),
                         "the VERDICT comes from the base class and is untouched by the bookkeeping")

    def test_an_ordinary_run_reports_its_bookkeeping_as_complete(self):
        """The other half, so the field above cannot pass by being true for everything."""
        _, record = self._run({"test_one.py": _CLEAN})
        self.assertFalse(record["record_incomplete"])

    def test_the_atomic_write_refuses_a_symlink_planted_at_its_temporary_name(self):
        """A predictable temporary name opened with a plain write follows symlinks — an arbitrary
        local file overwrite as the running user. The module had already solved this for its log."""
        tmp = tempfile.mkdtemp(prefix="selftest-symlink-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        victim = os.path.join(tmp, "victim.txt")
        with open(victim, "w") as fh:
            fh.write("untouched")
        target = os.path.join(tmp, "record.json")
        os.symlink(victim, f"{target}.{os.getpid()}.partial")
        wrote = selftest._atomic_write_json(target, {"hello": "world"})
        with open(victim) as fh:
            self.assertEqual(fh.read(), "untouched", "the planted symlink must not be followed")
        self.assertFalse(wrote, "the write must fail rather than clobber through a symlink")


class EveryWriterMatchesItsSchema(unittest.TestCase):
    """The test that would have caught the same mistake twice.

    A required field was added to each schema and wired into ONE writer out of three. Both times the
    writers left behind were the ones for bad days — the crashed run, the parent-side exits, the
    selector that could not run — so the artifact produced for the outcome the plan calls "the one it
    is most needed for" was the one that failed its own contract. The existing schema test only ever
    validated a clean pass and a focused pass, which is exactly how it got through. This drives every
    writer there is."""

    def _record_schema(self):
        import validate as _validate
        with open(os.path.join(_validate.ENGINE_DIR, "schemas",
                               "selftest-run-record.v1.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def test_the_parent_side_exit_record_validates(self):
        import jsonschema
        tmp = tempfile.mkdtemp(prefix="selftest-schema-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        record_path = os.path.join(tmp, "record.json")
        subprocess.run([sys.executable, _SELFTEST, "--start-dir", ".", "--cwd", ".",
                        "--log-path", os.path.join("no_such_dir_xyz", "run.log"),
                        "--run-record-path", record_path],
                       capture_output=True, text=True, timeout=30.0)
        with open(record_path) as fh:
            record = json.load(fh)
        self.assertEqual(record["verdict"], "log-unavailable")
        jsonschema.validate(record, self._record_schema())

    def test_the_crashed_run_record_validates(self):
        import jsonschema
        proc, record = FocusedRuns._run(self, {"test_suicide.py": """
            import os, signal, unittest
            class T(unittest.TestCase):
                def test_dies(self):
                    os.kill(os.getpid(), signal.SIGKILL)
        """})
        self.assertNotEqual(proc.returncode, 0)
        jsonschema.validate(record, self._record_schema())

    def test_the_selector_unavailable_fallback_manifest_validates(self):
        """The manifest built by hand when the selector cannot run — the artifact whose whole purpose
        is that a broken selector runs everything rather than fewer tests."""
        import jsonschema
        import validate as _validate
        with open(os.path.join(_validate.ENGINE_DIR, "schemas",
                               "selftest-selection.v1.json"), encoding="utf-8") as fh:
            selection_schema = json.load(fh)
        original = selftest._compute_selection.__globals__.get("__builtins__")
        manifest = selftest._compute_selection("no-such-ref-anywhere-at-all")
        self.assertEqual(manifest["classification"], "full")
        jsonschema.validate(manifest, selection_schema)

    def test_every_record_writer_declares_the_same_field_set(self):
        """Mechanical, so a field added to the schema cannot reach one writer and miss the others."""
        import inspect
        source = inspect.getsource(selftest)
        required = set(self._record_schema()["required"])
        for field in required:
            self.assertGreaterEqual(
                source.count(f'"{field}"'), 2,
                f"{field} is required by the schema but appears in too few writers to be on all of them")


class TreeBinding(unittest.TestCase):
    """The PR #1059 residual, discharged: the record names the committed tree it ran over.

    Both states are driven — the synthetic-fixture path where no repository exists and the binding is
    honestly null, and a real repository where the record's tree must equal what git itself says and
    the dirty flag must notice drift, including the new-file-inside-a-new-directory shape that a
    default porcelain read collapses."""

    def _run_in(self, suite_dir, out_dir):
        record_path = os.path.join(out_dir, "record.json")
        proc = subprocess.run(
            [sys.executable, _SELFTEST, "--start-dir", suite_dir, "--cwd", suite_dir,
             "--heartbeat-interval", "0.05", "--stall-threshold", "0.1",
             "--log-path", os.path.join(out_dir, "run.log"),
             "--run-record-path", record_path],
            capture_output=True, text=True, timeout=60.0)
        with open(record_path) as fh:
            return proc, json.load(fh)

    def test_a_run_outside_any_repository_binds_nothing_rather_than_guessing(self):
        """A fixture run in a bare temp directory has no tree to attest; null is the honest value, and
        the dirtiness of an unresolvable tree is not a fact the record may invent."""
        _, record = FocusedRuns._run(self, {"test_one.py": _CLEAN})
        self.assertIsNone(record["tree"])
        self.assertIsNone(record["worktree_dirty"])

    def test_a_run_inside_a_repository_records_the_committed_tree_and_sees_drift(self):
        suite_dir = _write_suite({"test_one.py": _CLEAN})
        self.addCleanup(shutil.rmtree, suite_dir, ignore_errors=True)
        out_dir = tempfile.mkdtemp(prefix="selftest-tree-out-")
        self.addCleanup(shutil.rmtree, out_dir, ignore_errors=True)
        # The run itself writes bytecode caches into the suite directory; the engine repo ignores
        # them, so the fixture repo must too or the clean run reads as self-dirtied.
        with open(os.path.join(suite_dir, ".gitignore"), "w") as fh:
            fh.write("__pycache__/\n")
        git = ["git", "-C", suite_dir, "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(git + ["init", "-q"], check=True, timeout=30)
        subprocess.run(git + ["add", "."], check=True, timeout=30)
        subprocess.run(git + ["commit", "-q", "-m", "seed"], check=True, timeout=30)
        expected = subprocess.run(git + ["rev-parse", "HEAD^{tree}"], check=True, timeout=30,
                                  capture_output=True, text=True).stdout.strip()

        proc, record = self._run_in(suite_dir, out_dir)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(record["tree"], expected,
                         "the record must attest the tree the suite actually ran over")
        self.assertIs(record["worktree_dirty"], False)

        # Drift in the shape the porcelain default collapses: a new file inside a new directory.
        os.makedirs(os.path.join(suite_dir, "newdir"))
        with open(os.path.join(suite_dir, "newdir", "stray.txt"), "w") as fh:
            fh.write("drift\n")
        out_dir2 = tempfile.mkdtemp(prefix="selftest-tree-out2-")
        self.addCleanup(shutil.rmtree, out_dir2, ignore_errors=True)
        _, dirty_record = self._run_in(suite_dir, out_dir2)
        self.assertEqual(dirty_record["tree"], expected,
                         "the committed tree is unchanged; only the working tree drifted")
        self.assertIs(dirty_record["worktree_dirty"], True,
                      "an untracked file inside an untracked directory must count as drift")


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
