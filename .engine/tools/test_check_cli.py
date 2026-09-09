"""Bounded executable help regressions for the L3-7 finding-check roster.

This deliberately names the one approved roster; it is a regression test for this
change, not a repository-wide CLI-policy check.  Each subprocess runs from a
fresh copy and writes no bytecode.  The trap startup module makes an accidental
action visible: help must not read its environment, open a write handle, start a
child process, or connect to the network.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.dirname(HERE)
ROOT = os.path.dirname(ENGINE)

# validate plus the fixed 14 emitter callers.  Keep the roster local to this
# migration test: later general enforcement belongs to C-4, not this PR.
ROSTER = (
    "validate.py",
    "agent_coherence_check.py",
    "block_coherence_check.py",
    "conduct_shape_check.py",
    "conduct_weakening_check.py",
    "interface_coherence_check.py",
    "knowledge_vocabulary_check.py",
    "operator_guarded_paths_check.py",
    "operator_local_references_check.py",
    "policy_override_check.py",
    "protection_guard.py",
    "release_integrity_check.py",
    "self_map_check.py",
    "skill_coherence_check.py",
    "weakening_guard.py",
)

_TRAPS = r'''
import builtins
import io
import os
import pathlib
import socket
import subprocess
import sys

sys.stderr.write("HELP-TRAP: startup\n")

def _fail(kind):
    def trapped(*args, **kwargs):
        sys.stderr.write("HELP-TRAP: " + kind + "\n")
        raise AssertionError("HELP-TRAP: " + kind)
    return trapped

os.environ.get = _fail("environment")
socket.create_connection = _fail("network")
socket.socket.connect = _fail("network")
subprocess.Popen = _fail("subprocess")
subprocess.run = _fail("subprocess")
os.system = _fail("subprocess")
_open = builtins.open
def guarded_open(file, mode="r", *args, **kwargs):
    if any(flag in mode for flag in ("w", "a", "x", "+")):
        sys.stderr.write("HELP-TRAP: write\n")
        raise AssertionError("HELP-TRAP: write")
    return _open(file, mode, *args, **kwargs)
builtins.open = guarded_open
io.open = guarded_open
pathlib.Path.open = lambda self, mode="r", *args, **kwargs: guarded_open(self, mode, *args, **kwargs)
_os_open = os.open
def guarded_os_open(path, flags, *args, **kwargs):
    if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND):
        sys.stderr.write("HELP-TRAP: write\n")
        raise AssertionError("HELP-TRAP: write")
    return _os_open(path, flags, *args, **kwargs)
os.open = guarded_os_open

def audit(event, args):
    if event in {"os.remove", "os.rename", "os.mkdir", "os.rmdir", "os.unlink"}:
        sys.stderr.write("HELP-TRAP: write\n")
        raise AssertionError("HELP-TRAP: write")
sys.addaudithook(audit)

def profile(frame, event, arg):
    if event == "call" and frame.f_code.co_name in {"_main", "run", "run_check", "run_files", "_demo", "_demo_kinds"}:
        sys.stderr.write("HELP-TRAP: action\n")
        raise AssertionError("HELP-TRAP: action")
sys.setprofile(profile)
'''


class TestCheckCliHelp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = os.path.join(cls.tmp.name, "repo")
        shutil.copytree(ROOT, cls.root, ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__"))
        cls.engine = os.path.join(cls.root, ".engine")
        cls.trap_dir = os.path.join(cls.tmp.name, "traps")
        os.mkdir(cls.trap_dir)
        with open(os.path.join(cls.trap_dir, "sitecustomize.py"), "w", encoding="utf-8") as fh:
            fh.write(_TRAPS)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _run(self, tool, argv, *, trapped=False, dummy_credentials=False):
        env = {"PYTHONDONTWRITEBYTECODE": "1"}
        if trapped:
            env["PYTHONPATH"] = self.trap_dir
        if dummy_credentials:
            env.update({"GITHUB_REPOSITORY": "owner/repo", "GITHUB_TOKEN": "dummy-token",
                        "GITHUB_EVENT_PATH": os.path.join(self.tmp.name, "missing-event.json")})
        return subprocess.run([sys.executable, "-B", os.path.join("tools", tool), *argv], cwd=self.engine,
                              env=env, capture_output=True, text=True)

    def _assert_help(self, tool, argv, **kwargs):
        proc = self._run(tool, argv, **kwargs)
        self.assertEqual(proc.returncode, 0,
                         f"{tool} {argv!r} must exit help cleanly. stdout={proc.stdout!r} stderr={proc.stderr!r}")
        self.assertIn("Usage:", proc.stdout)
        self.assertIn("--help", proc.stdout)
        self.assertNotRegex(proc.stdout, r"(?m)^\[")  # help must not emit a finding array
        markers = [line for line in proc.stderr.splitlines() if line.startswith("HELP-TRAP:")]
        if kwargs.get("trapped"):
            self.assertEqual(markers, ["HELP-TRAP: startup"], proc.stderr)
        else:
            self.assertEqual(markers, [], proc.stderr)

    def test_every_entrypoint_handles_both_flags_in_mixed_positions(self):
        positions = (
            ("--help",), ("-h",),
            ("--help", "invalid"), ("invalid", "--help"),
            ("-h", "invalid"), ("invalid", "-h"),
            ("--help", "demo"), ("demo", "--help"),
            ("-h", "demo"), ("demo", "-h"),
        )
        for tool in ROSTER:
            for argv in positions:
                with self.subTest(tool=tool, argv=argv):
                    self._assert_help(tool, argv)

    def test_help_never_runs_an_action_with_absent_or_dummy_credentials(self):
        for tool in ROSTER:
            with self.subTest(tool=tool, credentials="absent"):
                self._assert_help(tool, ("--help",), trapped=True)
            with self.subTest(tool=tool, credentials="dummy"):
                self._assert_help(tool, ("-h",), trapped=True, dummy_credentials=True)

    def test_fixed_roster_uses_the_one_shared_emitter_alias(self):
        for tool in ROSTER[1:]:  # validate.py owns emit; the other 14 consume it.
            with self.subTest(tool=tool):
                with open(os.path.join(self.engine, "tools", tool), encoding="utf-8") as fh:
                    source = fh.read()
                self.assertIn("emit = validate.emit", source)
                self.assertNotIn("def emit(", source)

    def test_trap_is_not_a_mocked_green_control(self):
        # Each control catches its own trap, prints Usage, and exits zero.  The
        # subprocess harness must still reject the recorded marker; this proves
        # a caught action cannot turn the negative control into a false green.
        controls = {
            "environment": "import os\ntry: os.environ.get('X')\nexcept AssertionError: pass",
            "network": "import socket\ntry: socket.create_connection(('127.0.0.1', 1))\nexcept AssertionError: pass",
            "subprocess": "import subprocess\ntry: subprocess.Popen(['false'])\nexcept AssertionError: pass",
            "write": "import pathlib\ntry: pathlib.Path('mutated').open('w')\nexcept AssertionError: pass",
            "action": "def _main(): pass\ntry: _main()\nexcept AssertionError: pass",
        }
        for kind, action in controls.items():
            tool = "negative_" + kind + ".py"
            with open(os.path.join(self.engine, "tools", tool), "w", encoding="utf-8") as fh:
                fh.write(action + "\nprint('Usage: intentionally caught trap --help')\n")
            with self.subTest(kind=kind), self.assertRaisesRegex(AssertionError, "HELP-TRAP: " + kind):
                self._assert_help(tool, ("--help",), trapped=True)

    def test_actual_roster_dispatch_is_caught_even_if_it_returns_usage_zero(self):
        tool = "conduct_weakening_check.py"
        path = os.path.join(self.engine, "tools", tool)
        with open(path, encoding="utf-8") as fh:
            original = fh.read()
        sabotaged = original.replace(
            "return validate.cli_main(argv, usage=USAGE, run=_main)",
            "try:\n        _main(argv)\n    except AssertionError:\n        pass\n    print(USAGE)\n    return 0",
            1,
        )
        self.assertNotEqual(sabotaged, original, "negative control must modify the real wrapper")
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(sabotaged)
            with self.assertRaisesRegex(AssertionError, "HELP-TRAP: action"):
                self._assert_help(tool, ("--help",), trapped=True)
        finally:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(original)

    def test_shared_helper_and_guards_start_without_site_packages(self):
        for tool, argv in (("validate.py", ("--help",)), ("protection_guard.py", ("-h",)),
                           ("weakening_guard.py", ("--help",))):
            with self.subTest(tool=tool):
                proc = subprocess.run([sys.executable, "-B", "-S", os.path.join("tools", tool), *argv],
                                      cwd=self.engine, env={"PYTHONDONTWRITEBYTECODE": "1"},
                                      capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("Usage:", proc.stdout)


if __name__ == "__main__":
    unittest.main()
