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


def _claude_argv() -> list:
    config = json.loads(MCP_JSON.read_text(encoding="utf-8"))
    server = config["mcpServers"][SERVER_NAME]
    return [server["command"]] + [a.replace(CLAUDE_PROJECT_DIR_TOKEN, str(ROOT)) for a in server["args"]]


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
    """The exact shipped commands, run for real."""

    def test_the_memory_server_starts_and_answers_on_a_clone_with_no_activation(self):
        """The regression itself. RED at be922f46 (see the module docstring for the method)."""
        with tempfile.TemporaryDirectory() as tmp:
            environment = _hermetic_environment(_egress_shim(tmp))
            client = _StdioClient(_claude_argv(), cwd=str(ROOT), env=environment)
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
            client = _StdioClient(_claude_argv(), cwd=str(ROOT), env=_hermetic_environment(_egress_shim(tmp)))
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
    """

    #: Modules with a known dead tail that predates this work and is out of this Build's declared scope.
    #: Their below-runner classes still run under discovery; this is a direct-run-hygiene allowance, not a
    #: coverage gap. The list must only ever shrink.
    KNOWN_DEAD_TAILS = {"test_module_manager.py", "test_modules.py"}

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
        self.assertEqual(still_dead & self.KNOWN_DEAD_TAILS, self.KNOWN_DEAD_TAILS,
                         "a known dead tail was fixed; remove it from KNOWN_DEAD_TAILS")

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


if __name__ == "__main__":
    unittest.main()
