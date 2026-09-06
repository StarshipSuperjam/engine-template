#!/usr/bin/env python3
"""The launch contract: the exact commands a host runs must start the memory server on a fresh clone.

This suite exists because of a specific failure. In PR #1153 the memory MCP server was launched through an
activation-gated dispatcher, on a machine where activation could never succeed — so on every clone, in every
session, the server did not start at all, and with it went memory reads, automatic capture, and the ability to
enter Build. Every unit test passed. Nothing anywhere ran the commands a host actually runs.

So that is what this does. It reads the SHIPPED launcher configuration — `.mcp.json` and `.codex/config.toml`,
not a copy or a paraphrase — resolves the argv the way each host resolves it, runs it, and speaks the protocol
to the process that comes back.

**The red witness.** Every claim here is one that fails at commit be922f46 (PR #1153's merge). To see it:

    git worktree add /tmp/witness be922f46
    cd /tmp/witness && uv run --directory .engine -- python tools/test_launch_contract.py

`test_the_memory_server_starts_and_answers_on_a_clone_with_no_activation` fails there with the server exiting
before the handshake, because `dispatch_attended` raised on the absent activation. That is the regression this
locks, and the check below records the method rather than leaving it as a claim in a pull-request body.
"""
from __future__ import annotations

import ast
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT = Path(__file__).resolve().parents[2]
MCP_JSON = ROOT / ".mcp.json"
CODEX_CONFIG = ROOT / ".codex/config.toml"
SERVER_NAME = "engine-memory"

#: The host contracts this suite pins, with their provenance. A host expands variables and resolves relative
#: paths its own way, and each of these was read off the shipped configuration rather than assumed:
#:
#:  * Claude Code (.mcp.json) expands `${CLAUDE_PROJECT_DIR:-.}` and runs from the project directory.
#:  * Codex (.codex/config.toml) performs NO variable expansion and resolves relative paths against the
#:    directory it was started in — which is why the Codex arm below is deliberately run from elsewhere.
CLAUDE_PROJECT_DIR_TOKEN = "${CLAUDE_PROJECT_DIR:-.}"
READ_TOOLS = ("health", "search", "recall-window", "recall-by-meaning", "list-pins", "list-withheld")
WRITE_TOOLS = ("pin", "withhold", "restore")
READ_ARGUMENTS = {
    "health": {},
    "search": {"query": "engine", "limit": 1},
    "recall-window": {"session_id": "tag:none"},
    "recall-by-meaning": {"query": "engine", "limit": 1},
    "list-pins": {},
    "list-withheld": {},
}


def _claude_argv(root: Path = ROOT) -> list:
    """The shipped Claude argv, with the project-dir token expanded to `root`.

    The configuration is always read from the REAL checkout (that is the shipped file this suite pins);
    only the expansion target varies, so a test can launch the same contract against a pristine clone.
    """
    config = json.loads(MCP_JSON.read_text(encoding="utf-8"))
    server = config["mcpServers"][SERVER_NAME]
    return [server["command"]] + [a.replace(CLAUDE_PROJECT_DIR_TOKEN, str(root)) for a in server["args"]]


def _codex_block() -> dict:
    """The Codex launcher block, parsed out of the shipped TOML without a TOML dependency."""
    text = CODEX_CONFIG.read_text(encoding="utf-8")
    marker = f"[mcp_servers.{SERVER_NAME}]"
    if marker not in text:
        raise AssertionError(f"{CODEX_CONFIG} no longer declares {marker}")
    body = text.split(marker, 1)[1]
    block = {}
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("[") or line.startswith("# END"):
            break
        if "=" not in line or line.startswith("#"):
            continue
        key, _, raw = line.partition("=")
        block[key.strip()] = ast.literal_eval(raw.strip())
    return block


def _hermetic_environment(extra_path: str | None = None) -> dict:
    """The environment a host hands a launcher, minus anything that could reach the network.

    `uv` needs its cache and project environment, so those are inherited explicitly and named here rather
    than being smuggled in by copying os.environ wholesale. Everything that could authenticate or reach out
    — the GitHub CLI's token, proxies — is removed, and a shim directory is prepended to PATH so a network
    tool that DID run leaves evidence instead of quietly succeeding.
    """
    keep = ("HOME", "PATH", "LANG", "LC_ALL", "TMPDIR", "UV_CACHE_DIR", "UV_PROJECT_ENVIRONMENT",
            "XDG_CACHE_HOME", "SYSTEMROOT")
    environment = {key: os.environ[key] for key in keep if key in os.environ}
    environment["ENGINE_LAUNCH_CONTRACT_TEST"] = "1"
    if extra_path:
        environment["PATH"] = extra_path + os.pathsep + environment.get("PATH", "")
    return environment


def _egress_shim(directory: str) -> str:
    """A PATH directory whose `gh`, `curl` and `ssh` record any invocation and fail."""
    shim = os.path.join(directory, "shim")
    os.makedirs(shim, exist_ok=True)
    witness = os.path.join(directory, "egress.log")
    for name in ("gh", "curl", "ssh", "wget"):
        path = os.path.join(shim, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(f'#!/bin/sh\necho "{name} $*" >> {witness}\nexit 97\n')
        os.chmod(path, 0o755)
    return shim


class _StdioClient:
    """A minimal MCP stdio client that keeps stdin OPEN between calls.

    Closing stdin cancels an in-flight call, which makes a slow tool look like a dropped one — a mistake that
    cost real debugging time while building this, and the reason this helper exists rather than a one-shot
    `communicate`.
    """

    def __init__(self, argv, cwd, env):
        self.process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        self._lines = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.process.stdout:
            self._lines.put(line)

    def call(self, message, *, wait=True, timeout=120):
        """Send one request and wait for its reply — or for the process to die, whichever comes first.

        Waiting only on the reply would turn "the server never started" into a full-length timeout, which is
        exactly the failure this suite is built to catch and therefore the one it must report FAST and
        legibly. A dead process is an immediate, named failure.
        """
        try:
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            raise AssertionError(f"the launcher process was gone before {message.get('method')}: {exc}")
        if not wait:
            return None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                raw = self._lines.get(timeout=0.25)
            except queue.Empty:
                if self.process.poll() is not None:
                    stderr = ""
                    try:
                        stderr = (self.process.stderr.read() or "")[-800:]
                    except Exception:  # noqa: BLE001
                        pass
                    raise AssertionError(
                        f"the launcher exited with code {self.process.returncode} before answering "
                        f"{message.get('method')!r}. stderr tail: {stderr}")
                continue
            try:
                value = json.loads(raw)
            except ValueError:
                continue
            if value.get("id") == message.get("id"):
                return value
        raise AssertionError(f"no reply to {message.get('method')!r} within {timeout}s")

    def handshake(self):
        reply = self.call({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "launch-contract", "version": "0"}}})
        self.call({"jsonrpc": "2.0", "method": "notifications/initialized"}, wait=False)
        return reply

    def tools(self):
        return [t["name"] for t in self.call(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})["result"]["tools"]]

    def invoke(self, index, name, arguments):
        return self.call({"jsonrpc": "2.0", "id": 100 + index, "method": "tools/call",
                          "params": {"name": name, "arguments": arguments}})

    def close(self):
        try:
            self.process.stdin.close()
            code = self.process.wait(timeout=60)
        except Exception:  # noqa: BLE001
            self.process.kill()
            code = -1
        for stream in (self.process.stdout, self.process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:  # noqa: BLE001
                pass
        return code


class LaunchContractTests(unittest.TestCase):
    """The exact shipped commands, run for real — against a clone with no activation, BY CONSTRUCTION.

    The first two tests' premise is a fresh clone where accepted-hook activation has never converged.
    Launching against ROOT only *assumes* that premise: the activation record lives in the git common
    directory, shared by the real checkout and every worktree, so on a machine where qualification has
    converged (the engine's own home repository, once the mandated pointer split was excused) the write
    verbs EXECUTE instead of refusing and the refusal test fails for a reason that has nothing to do
    with the shipped contract. So the premise is constructed instead of assumed: a pristine local clone
    has its own git directory and therefore no activation record — exactly the state every fresh clone,
    including CI's, is in. The clone holds ROOT's committed HEAD; uncommitted edits are deliberately
    absent, the same trade every commit-addressed harness makes.
    """

    @classmethod
    def setUpClass(cls):
        cls._clone_home = tempfile.TemporaryDirectory(prefix="launch-contract-clone-",
                                                      ignore_cleanup_errors=True)
        clone = Path(cls._clone_home.name) / "clone"
        head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                              check=True, capture_output=True, text=True).stdout.strip()
        # --no-checkout + an explicit detach pins the clone to ROOT's exact HEAD even when ROOT is a
        # linked worktree, whose default clone branch would otherwise be the source repository's choice.
        subprocess.run(["git", "clone", "--quiet", "--no-checkout", str(ROOT), str(clone)],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(clone), "checkout", "--quiet", "--detach", head],
                       check=True, capture_output=True)
        cls.clone_root = clone

    @classmethod
    def tearDownClass(cls):
        cls._clone_home.cleanup()

    @classmethod
    def _clone_environment(cls, tmp: str) -> dict:
        """The hermetic environment, plus the REAL checkout's project venv when one exists.

        `uv run --frozen` inside the clone would otherwise materialize a second venv from the same
        lockfile — identical bytes, minutes of linking. Pointing UV_PROJECT_ENVIRONMENT at the real
        venv is a deliberate fixture choice, not smuggling: the clone sits at ROOT's committed HEAD,
        so the lockfile the venv satisfies is the same one the clone carries. UV_NO_SYNC keeps that
        reuse READ-ONLY — `--frozen` stops lockfile updates, not environment syncs, so without it a
        run here could sync the operator's live venv toward the committed lockfile while their
        working tree carries uncommitted dependency changes. The borrowed venv is used exactly as it
        stands or not at all.
        """
        environment = _hermetic_environment(_egress_shim(tmp))
        venv = ROOT / ".engine" / ".venv"
        if venv.is_dir():
            environment["UV_PROJECT_ENVIRONMENT"] = str(venv)
            environment["UV_NO_SYNC"] = "1"
        return environment

    def test_the_memory_server_starts_and_answers_on_a_clone_with_no_activation(self):
        """The regression itself. RED at be922f46 (see the module docstring for the method)."""
        with tempfile.TemporaryDirectory() as tmp:
            environment = self._clone_environment(tmp)
            client = _StdioClient(_claude_argv(self.clone_root), cwd=str(self.clone_root), env=environment)
            try:
                reply = client.handshake()
                self.assertEqual(reply["result"]["serverInfo"]["name"], SERVER_NAME)
                published = client.tools()
                for name in READ_TOOLS + WRITE_TOOLS:
                    self.assertIn(name, published)
                for index, tool in enumerate(READ_TOOLS):
                    with self.subTest(tool=tool):
                        result = client.invoke(index, tool, READ_ARGUMENTS[tool])["result"]
                        self.assertFalse(result.get("isError"), f"{tool} did not answer: {result}")
            finally:
                client.close()
            self.assertFalse(os.path.exists(os.path.join(tmp, "egress.log")),
                             "the launch path reached the network")

    def test_the_three_write_verbs_refuse_in_plain_words_and_leave_the_server_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _StdioClient(_claude_argv(self.clone_root), cwd=str(self.clone_root),
                                  env=self._clone_environment(tmp))
            try:
                client.handshake()
                arguments = {"pin": {"text": "launch-contract probe"},
                             "withhold": {"record_id": "probe", "reason": "probe"},
                             "restore": {"record_id": "probe"}}
                for index, tool in enumerate(WRITE_TOOLS, start=50):
                    with self.subTest(tool=tool):
                        result = client.invoke(index, tool, arguments[tool])["result"]
                        self.assertTrue(result.get("isError"), f"{tool} did not refuse")
                        text = " ".join(c.get("text", "") for c in result.get("content", []))
                        self.assertIn("qualif", text)
                        self.assertNotIn("Traceback", text)
                # Still alive after three refusals: a refusal is an answer, not a crash.
                self.assertFalse(client.invoke(60, "health", {})["result"].get("isError"))
            finally:
                client.close()

    def test_the_codex_arm_launches_from_a_different_working_directory(self):
        """Codex resolves its relative `--directory .engine` against where it was started, so running this
        from elsewhere is the whole point: it is how a wrong-cwd contract shows up as a failure here rather
        than on an operator's machine."""
        block = _codex_block()
        self.assertEqual(block["command"], "uv")
        self.assertIn("--directory", block["args"])
        with tempfile.TemporaryDirectory() as tmp:
            argv = [block["command"]] + [
                str(ROOT / a[2:]) if a.startswith("./") else a for a in block["args"]]
            # Make the relative --directory absolute exactly as a Codex launched IN the project would resolve
            # it, then run from somewhere else entirely to prove nothing else in the argv is cwd-dependent.
            argv = [str(ROOT / a) if a == ".engine" else a for a in argv]
            client = _StdioClient(argv, cwd=tmp, env=_hermetic_environment(_egress_shim(tmp)))
            try:
                reply = client.handshake()
                self.assertEqual(reply["result"]["serverInfo"]["name"], SERVER_NAME)
                self.assertFalse(client.invoke(1, "health", {})["result"].get("isError"))
            finally:
                client.close()


class HostContractDriftTests(unittest.TestCase):
    """The shipped configuration must keep the SHAPE each host actually understands."""

    def test_the_claude_config_uses_the_expansion_claude_code_performs(self):
        server = json.loads(MCP_JSON.read_text(encoding="utf-8"))["mcpServers"][SERVER_NAME]
        self.assertEqual(server["command"], "uv")
        directory = server["args"][server["args"].index("--directory") + 1]
        self.assertTrue(directory.startswith(CLAUDE_PROJECT_DIR_TOKEN),
                        f"Claude Code expands {CLAUDE_PROJECT_DIR_TOKEN}; this form would not resolve")

    def test_the_codex_config_uses_no_expansion_because_codex_performs_none(self):
        block = _codex_block()
        for argument in block["args"]:
            self.assertNotIn("${", argument,
                             "Codex does not expand variables; a ${...} here reaches uv literally")

    def test_an_unmodelled_config_form_is_caught(self):
        """The drift test itself: a config shape neither host resolves must fail here, not at a session start
        on someone's machine."""
        server = json.loads(MCP_JSON.read_text(encoding="utf-8"))["mcpServers"][SERVER_NAME]
        broken = dict(server)
        broken["args"] = ["run", "--directory", "$CLAUDE_PROJECT_DIR/.engine"] + server["args"][3:]
        directory = broken["args"][broken["args"].index("--directory") + 1]
        self.assertFalse(directory.startswith(CLAUDE_PROJECT_DIR_TOKEN))   # the assertion above would fail
        block_form = "args = [\"run\", \"--directory\", \"${PWD}/.engine\"]"
        self.assertIn("${", block_form)                                    # the Codex assertion would fail

    def test_both_hosts_launch_the_same_server_module(self):
        claude = _claude_argv()
        codex = _codex_block()["args"]
        for argv in (claude, codex):
            self.assertIn("tools/accepted_hook_dispatch.py", argv)
            self.assertIn(".engine/tools/memory/mcp_server.py", argv)
            self.assertIn("attended-memory-mcp", argv)


class TestModuleIntegrityTests(unittest.TestCase):
    """A test suite must not report green while quietly skipping cases when run the ordinary developer way.

    This guards the runner-last convention. A `unittest.main()` sitting mid-file is invisible under the
    engine's canonical `unittest discover` run — discovery imports the module (so the `__main__` guard is
    false) and `loadTestsFromModule` collects every TestCase regardless of position; `test_the_..._is_
    collected` below proves exactly that against the real activation suite. The failure mode it prevents is
    narrower and real: a developer running a file DIRECTLY (`python tools/test_x.py`) executes
    `unittest.main()` at the mid-file line, before the classes beneath it are even defined, and gets a
    confident green that silently omitted them. Runner-last makes the direct run and the discovered run
    cover the same cases.

    Not the #1153 story. PR #1153's accepted-hook activation suite did sit below its runner, but it was
    collected and DID run under discovery (verified by reconstructing that tree) — #1153 shipped broken
    because those tests never exercised the fresh-clone path, an inadequacy, not a dark suite. An earlier
    draft of this guard misattributed #1153 to non-collection; that claim was wrong and has been removed.

    Corrective note on #1165. The below-runner classes in test_module_manager.py and test_modules.py that
    #1165 flagged were likewise already COLLECTED and run under the canonical `unittest discover` — they were
    never a coverage gap, only a direct-run-hygiene defect (`python tools/<file>.py` skipped them). Both
    runners have now been moved to end-of-file, so this guard admits no exceptions and KNOWN_DEAD_TAILS is
    empty.
    """

    #: Modules with a known dead tail allowed as a direct-run-hygiene exception. The list must only ever
    #: shrink, and it is now EMPTY: test_module_manager.py and test_modules.py had their runners moved to
    #: end-of-file, so every test module's direct run and discovered run now cover the same cases.
    KNOWN_DEAD_TAILS: set = set()

    def _dead_classes(self, path: Path):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        runner_line = None
        for node in tree.body:
            if isinstance(node, ast.If) and "unittest" in ast.dump(node) and "main" in ast.dump(node):
                runner_line = node.lineno
        if runner_line is None:
            return []
        return [node.name for node in tree.body
                if isinstance(node, ast.ClassDef) and node.lineno > runner_line
                and any(getattr(base, "attr", "") == "TestCase" for base in node.bases)]

    def test_no_test_module_defines_a_testcase_after_its_runner(self):
        offenders = {}
        for path in sorted((ROOT / ".engine/tools").rglob("test_*.py")):
            dead = self._dead_classes(path)
            if dead and path.name not in self.KNOWN_DEAD_TAILS:
                offenders[str(path.relative_to(ROOT))] = dead
        self.assertEqual(offenders, {},
                         "these TestCase classes sit below their runner and are silently skipped when the "
                         "file is run directly (python tools/<file>.py); move the runner to end-of-file")

    def test_the_known_dead_tails_are_still_the_only_exceptions(self):
        """If one is fixed, this fails and the allowance must be removed with it — so the list cannot rot."""
        still_dead = {path.name for path in sorted((ROOT / ".engine/tools").rglob("test_*.py"))
                      if self._dead_classes(path)}
        fixed = sorted(self.KNOWN_DEAD_TAILS - still_dead)
        self.assertEqual(fixed, [], f"these known dead tails were fixed; remove them from KNOWN_DEAD_TAILS: "
                                    f"{', '.join(fixed)}")

    #: Test modules that import a sibling tool WITHOUT putting their own directory on sys.path first, so
    #: they import only when some module loaded earlier happened to set the path — the standalone dotted
    #: run (`python -m unittest tools.test_x` from .engine) fails with ModuleNotFoundError (#1010). The
    #: canonical `discover -s tools` run puts the directory on the path itself, so it can never catch a
    #: regression here; this guard is what does. The list must only ever SHRINK: fix a module by adding
    #: `sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))` ahead of its sibling imports and
    #: remove it here — the companion test fails if it is fixed and the allowance is left behind.
    #: Only that bare module-level statement, placed AHEAD of the sibling imports, counts: a call inside a
    #: function, below the imports, or wrapped in a conditional is deliberately not recognised.
    KNOWN_PATH_BLIND: frozenset = frozenset({
        "test_boot_alarm_ledger.py",
        "test_build_coordinator_contract.py",
        "test_build_state_store.py",
        "test_checkout_auto_update.py",
        "test_checkout_health.py",
        "test_ci_assurance.py",
        "test_first_run_health.py",
        "test_integration_queue_backend.py",
        "test_issue_gate.py",
        "test_license_health.py",
        "test_license_seeds.py",
        "test_mechanic_build.py",
        "test_modes.py",
        "test_plan_contract.py",
        "test_plan_dogfood.py",
        "test_plan_lifecycle.py",
        "test_plan_program.py",
        "test_plan_projection.py",
        "test_plan_store.py",
        "test_pr_reconcile.py",
        "test_program_manager.py",
        "test_program_projection.py",
        "test_project_manager.py",
        "test_release_cut.py",
        "test_release_impact.py",
        "test_release_impact_check.py",
        "test_release_terminal.py",
        "test_repair_divergence.py",
        "test_self_review_setup.py",
        "test_selftest_select.py",
        "test_session_relay.py",
        "test_uv_workspace_cache.py",
    })

    @staticmethod
    def _sibling_tools() -> set:
        return {path.stem for path in (ROOT / ".engine/tools").glob("*.py")}

    def _path_blind_imports(self, path: Path, tools: set) -> list:
        """The sibling tools `path` imports bare (top-level `import x` / `from x import ...` where x is a
        module in .engine/tools) BEFORE any module-level `sys.path.insert(...)` / `sys.path.append(...)`.
        Only a module-level call that precedes the import counts: a call inside a function, or below the
        imports it was meant to enable, does nothing for the import that already failed."""
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

        def _sets_path(node) -> bool:
            call = node.value if isinstance(node, ast.Expr) else None
            return (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and call.func.attr in ("insert", "append")
                    and isinstance(call.func.value, ast.Attribute) and call.func.value.attr == "path"
                    and isinstance(call.func.value.value, ast.Name) and call.func.value.value.id == "sys")

        bare = []
        for node in tree.body:
            if _sets_path(node):
                break
            if isinstance(node, ast.Import):
                bare += [alias.name for alias in node.names if alias.name in tools]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module in tools:
                bare.append(node.module)
        return bare

    def test_every_test_module_that_imports_a_sibling_tool_sets_its_own_path(self):
        tools = self._sibling_tools()
        offenders = {}
        for path in sorted((ROOT / ".engine/tools").rglob("test_*.py")):
            bare = self._path_blind_imports(path, tools)
            if bare and path.name not in self.KNOWN_PATH_BLIND:
                offenders[str(path.relative_to(ROOT))] = bare
        self.assertEqual(offenders, {},
                         "these test modules import a sibling tool without putting their own directory on "
                         "sys.path first, so they cannot be run standalone (python -m unittest tools.<module>); "
                         "add sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))) ahead of the imports")

    def test_the_known_path_blind_modules_are_still_the_only_exceptions(self):
        """If one is fixed, this fails and the allowance must be removed with it — so the list cannot rot."""
        tools = self._sibling_tools()
        still_blind = {path.name for path in sorted((ROOT / ".engine/tools").rglob("test_*.py"))
                       if self._path_blind_imports(path, tools)}
        fixed = sorted(self.KNOWN_PATH_BLIND - still_blind)
        self.assertEqual(fixed, [], f"these path-blind test modules were fixed; remove them from "
                                    f"KNOWN_PATH_BLIND: {', '.join(fixed)}")

    def test_the_activation_suite_this_guard_exists_for_is_collected(self):
        source = (ROOT / ".engine/tools/test_hooks.py").read_text(encoding="utf-8")
        self.assertIn("class TestAcceptedAutomaticHookDispatch", source)
        self.assertIn("class TestAmbientActivationLifecycle", source)
        self.assertEqual(self._dead_classes(ROOT / ".engine/tools/test_hooks.py"), [])

    def test_discovery_collects_a_testcase_defined_below_the_runner(self):
        """The load-bearing fact behind this whole guard: `unittest discover` — the engine's canonical run —
        collects a TestCase even when it sits BELOW `unittest.main()`, so runner position never darkens a
        test in CI. It is only a direct `python file.py` run that skips it. If this ever fails, the guard's
        premise (that runner-last is a direct-run-hygiene matter, not a CI-coverage one) is wrong and the
        rationale must be revisited."""
        probe = (
            "import unittest\n"
            "class Above(unittest.TestCase):\n"
            "    def test_above(self): pass\n"
            'if __name__ == "__main__":\n'
            "    unittest.main()\n"
            "class Below(unittest.TestCase):\n"
            "    def test_below(self): pass\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "test_probe.py").write_text(probe, encoding="utf-8")
            suite = unittest.TestLoader().discover(start_dir=tmp, pattern="test_probe.py")

            def leaves(s):
                for item in s:
                    if isinstance(item, unittest.TestSuite):
                        yield from leaves(item)
                    else:
                        yield item.id()

            collected = set(leaves(suite))
        self.assertIn("test_probe.Below.test_below", collected,
                      "discovery no longer collects below-runner classes — this guard's rationale is stale")
        self.assertIn("test_probe.Above.test_above", collected)


class RedWitnessMethodTests(unittest.TestCase):
    """The red witness is recorded as an executable method, not a sentence in a pull request."""

    WITNESS_COMMIT = "be922f46"

    def test_the_method_is_documented_with_the_exact_commit_and_command(self):
        doc = sys.modules[__name__].__doc__ or ""
        self.assertIn(self.WITNESS_COMMIT, doc)
        self.assertIn("git worktree add", doc)
        self.assertIn("test_launch_contract.py", doc)

    def test_the_witness_commit_lacks_the_degrade_this_suite_locks(self):
        """Proof the witness is real: at be922f46 the attended dispatcher had no degraded path at all, so the
        server could not start without an activation. Read from git rather than asserted."""
        if not shutil.which("git"):
            self.skipTest("git is unavailable")
        result = subprocess.run(
            ["git", "-C", str(ROOT), "show", f"{self.WITNESS_COMMIT}:.engine/tools/accepted_hook_dispatch.py"],
            capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            self.skipTest("the witness commit is not present in this clone")
        self.assertNotIn("_dispatch_attended_degraded", result.stdout)
        self.assertIn("activation = load_activation(root)", result.stdout)
        # And the manifest key that made every activation refuse there:
        self.assertIn('value.get("engine_version")', result.stdout)


class TestCaptureHermeticity(unittest.TestCase):
    """Repo-wide invariant: a test module that DRIVES the real capture path must redirect its status
    marker and failure history off the production cache files, or a green self-test run silently writes
    a false "capture is failing" signal onto the very files the engine reads for its own memory-capture
    health (StarshipSuperjam/engine-template#1193 — the test suite had latched exactly that). Hermeticity
    is a property of the tests, guarded here — never a runtime "am I under test" guess inside capture, the
    rejected alternative. The one blessed seam is `memory.capture.redirect_health_paths`; any module that
    calls a capture entry point must reference it. The guard only PARSES modules; it never runs them, so
    checking it can never itself write the production files."""

    #: Capture entry points whose call writes the status marker and/or the failure history.
    CAPTURE_DRIVERS = {"capture_turn_delta", "_trigger_ambient_capture"}
    #: The blessed redirect seam a capture-driving module must use to stay hermetic.
    REDIRECT_SEAM = "redirect_health_paths"

    @classmethod
    def _identifiers(cls, tree: ast.AST) -> set:
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.Name):
                names.add(node.id)
        return names

    @classmethod
    def _drives_capture(cls, tree: ast.AST) -> bool:
        # A CALL to a driver, not a mere reference — so `hasattr(memory, "capture_turn_delta")` and
        # `callable(memory.capture_turn_delta)` (which never write anything) are correctly ignored.
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                if name in cls.CAPTURE_DRIVERS:
                    return True
        return False

    @classmethod
    def _offenders(cls, test_files) -> list:
        offenders = []
        for path in test_files:
            tree = ast.parse(Path(path).read_text(encoding="utf-8"), filename=str(path))
            if cls._drives_capture(tree) and cls.REDIRECT_SEAM not in cls._identifiers(tree):
                offenders.append(Path(path).name)
        return sorted(offenders)

    def test_no_capture_driving_test_writes_the_production_marker(self):
        tools = ROOT / ".engine" / "tools"
        offenders = self._offenders(sorted(tools.rglob("test_*.py")))
        self.assertEqual(
            offenders, [],
            "these test modules call a capture entry point without redirecting its health paths, so a "
            f"self-test run writes the production capture cache files: {offenders}. Redirect both in the "
            "module's setUp with memory.capture.redirect_health_paths / restore_health_paths.")

    def test_the_guard_flags_an_unredirected_driver_and_passes_a_redirected_one(self):
        # Negative control: a synthetic module that drives capture without the seam MUST be flagged;
        # a sibling that uses the seam must pass. Both are only parsed, never run — no production write.
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "test_synthetic_unredirected.py"
            bad.write_text(
                "from memory import capture\n"
                "def test_it():\n"
                "    capture.capture_turn_delta({'session_id': 'x'})\n",
                encoding="utf-8")
            self.assertEqual(self._offenders([bad]), ["test_synthetic_unredirected.py"])
            good = Path(tmp) / "test_synthetic_redirected.py"
            good.write_text(
                "from memory import capture\n"
                "def test_it():\n"
                "    saved = capture.redirect_health_paths('/tmp/x')\n"
                "    capture.capture_turn_delta({'session_id': 'x'})\n"
                "    capture.restore_health_paths(saved)\n",
                encoding="utf-8")
            self.assertEqual(self._offenders([good]), [])


class TestGuardHelpersSingleHome(unittest.TestCase):
    """Repo-wide invariant: the self-test suite's guard helpers — the construction predicate, the installed-
    modules helper and the needs-modules skip wrapper — have ONE home, `selftest_support.py`, and no test
    module carries its own copy (StarshipSuperjam/engine-template#940). Before the consolidation the copies
    had drifted under NEW names (`_needs_product_design`, `_needs_design_review`) and one had the helper
    inlined into a wrapper's body, so a guard keyed on the original names would have caught none of them.
    This one is keyed on the two shapes that actually drifted — a helper function (not a test case, which
    may legitimately read a manifest's content and skip when it is absent) that both calls `skipTest` and
    reaches `discover_manifests`, and a module-level binding that calls a home-repo probe — plus the old
    and new names as a belt. The predicate shape is flagged whether or not it also mentions a nested/
    projection marker: a re-copied shape predicate is a copy either way, marker or no marker. It is a
    tripwire for those shapes, not a proof of uniqueness: a copy written in a third shape would pass it.
    The guard only PARSES modules; it never runs them. `selftest_support.py` sits outside the `test_*.py`
    pattern on purpose, so it is not in the population this guard scans (and must not be renamed into it:
    see its docstring)."""

    #: Names the consolidation removed, and the single home's public spellings — no test module may DEFINE
    #: its own under either spelling (an assignment or a def at module level; importing them is the point).
    BANNED_NAMES = frozenset({
        "_CONSTRUCTION", "CONSTRUCTION",
        "_installed_module_ids", "installed_module_ids",
        "_needs_modules", "needs_modules",
        "_needs_product_design", "_needs_design_review",
    })
    #: The probes a re-copied construction predicate calls — flagged with or without a marker mention.
    HOME_PROBES = frozenset({"is_home_repo", "_in_home_repo"})
    @staticmethod
    def _called_names(node: ast.AST) -> set:
        names = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                fn = sub.func
                names.add(fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None))
        return names

    @classmethod
    def _copies_in(cls, tree: ast.AST) -> list:
        """Every guard-helper copy in one parsed module, as plain-language reasons."""
        found = []
        for node in tree.body:  # module level only for the name and predicate shapes
            targets = []
            if isinstance(node, ast.Assign):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                targets = [node.target.id]
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                targets = [node.name]
            for name in targets:
                if name in cls.BANNED_NAMES:
                    found.append(f"binds `{name}` at module level")
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                if cls._called_names(node.value) & cls.HOME_PROBES:
                    found.append("binds a module-level predicate that calls a home-repo probe")
        for node in ast.walk(tree):  # the inlined-wrapper shape, at any depth: a HELPER, not a test case
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("test_"):
                called = cls._called_names(node)
                if "skipTest" in called and "discover_manifests" in called:
                    found.append(f"`{node.name}` both calls skipTest and reaches discover_manifests")
        return found

    @classmethod
    def _offenders(cls, paths) -> list:
        out = []
        for path in paths:
            tree = ast.parse(Path(path).read_text(encoding="utf-8"), filename=str(path))
            for reason in cls._copies_in(tree):
                out.append(f"{Path(path).name}: {reason}")
        return out

    def test_no_test_module_carries_its_own_guard_helper(self):
        paths = sorted((ROOT / ".engine/tools").rglob("test_*.py"))
        self.assertEqual(self._offenders(paths), [],
                         "these test modules carry their own copy of a guard helper; import it from "
                         "selftest_support.py (CONSTRUCTION, installed_module_ids, needs_modules) instead")

    def test_the_guard_names_each_shape_that_drifted(self):
        """The tripwire's own proof: each shape the consolidation removed is caught and named, and a module
        that uses the single home is not."""
        shapes = {
            "old_name": "_CONSTRUCTION = True\n",
            "new_name": "def _needs_design_review(case):\n    return None\n",
            "public_name": "installed_module_ids = None\n",
            "predicate": ("import os, repo_identity, validate\n"
                          "_GATE = repo_identity.is_home_repo(validate.ROOT) and not os.environ.get('ENGINE_NESTED_SELFTEST')\n"),
            "predicate_variant": "import os, release_gate as rg\n_GATE = rg._ccc._in_home_repo() and not os.environ.get(rg._NESTED_ENV)\n",
            "predicate_no_marker": "import repo_identity, validate\n_GATE = repo_identity.is_home_repo(validate.ROOT)\n",
            "inlined_wrapper": ("import module_coherence\n"
                                "def _needs_thing(case):\n"
                                "    ids = {m.get('id') for _p, m in module_coherence.discover_manifests()}\n"
                                "    if 'thing' not in ids:\n"
                                "        case.skipTest('thing is absent')\n"),
        }
        with tempfile.TemporaryDirectory() as tmp:
            for label, body in shapes.items():
                path = Path(tmp) / f"test_{label}.py"
                path.write_text(body, encoding="utf-8")
                self.assertEqual(len(self._offenders([path])), 1, f"{label} was not caught exactly once")
            good = Path(tmp) / "test_good.py"
            good.write_text("import selftest_support\n"
                            "@unittest.skipUnless(selftest_support.CONSTRUCTION, 'home only')\n"
                            "class T(unittest.TestCase):\n"
                            "    def test_x(self):\n"
                            "        selftest_support.needs_modules(self, 'qa-review')\n", encoding="utf-8")
            self.assertEqual(self._offenders([good]), [])

    def test_the_nested_run_marker_has_one_value_across_its_homes(self):
        """The marker's name is held by value in three places on purpose (the launcher, the release gate,
        and the support module: a test module's import graph stays light, so the support module does not
        import the launcher just to read one string). This case and test_selftest_support.py's name pins are
        the two deliberate places that import both to compare the values, and are what keep the three from
        drifting apart."""
        import release_gate
        import selftest
        import selftest_support
        self.assertEqual(selftest_support.NESTED_ENV, selftest._NESTED_ENV)
        self.assertEqual(selftest_support.NESTED_ENV, release_gate._NESTED_ENV)


if __name__ == "__main__":
    unittest.main()
