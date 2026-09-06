"""test_mcp_server.py — the engine-memory MCP server, headless (memory substrate).

Also locks the --help guard: --help / -h print usage naming the accepted-hook launch chain and exit 0 before
server.run(); `demo` is the only verb; any other argument exits 2 without serving (#807).

Run: uv run --directory .engine --frozen -- python tools/selftest.py

Exercises the server in-process (no Claude Desktop, no subprocess): the single `search` tool delegates to the
ranked library and returns `{"results": [...]}`, writing nothing at all — a read is a read. Beside it are the
operator's own controls, which DO write, and the two that do not appear here at all: permanent erasure and the
secret re-scrub are declared in the control contract and deliberately not served, because each is a
command-line verb a person runs at a terminal. Isolation is a throwaway
ENGINE_MEMORY_DIR cabinet, so the server's default-path library calls resolve to the test's temp store.
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory import (capture, execution_context, forget, index, ledger, mutation_authority,  # noqa: E402
                    mutation_contract, pins, records)
import memory.mcp_server as srv  # noqa: E402
import mcp_test_support as mts  # noqa: E402
from mcp.server import MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

_ID = records.RECORD_ID_KEY


def _marker_count():
    return sum(1 for r in ledger.iter_records()
               if isinstance(r, dict) and r.get("kind") == records.REINFORCEMENT_KIND)


class _ServerBase(unittest.IsolatedAsyncioTestCase):
    """Each test runs against a throwaway ENGINE_MEMORY_DIR cabinet; the server's default-path calls land there."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="engine-memory-mcp-")
        self._prev = os.environ.get(ledger.ENV_DIR)
        os.environ[ledger.ENV_DIR] = self.tmp
        self.now = int(time.time())
        self._authority = mutation_authority.test_scope("attended")
        self._authority.__enter__()

    def tearDown(self):
        self._authority.__exit__(None, None, None)
        if self._prev is None:
            os.environ.pop(ledger.ENV_DIR, None)
        else:
            os.environ[ledger.ENV_DIR] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add(self, text, *, role="observation", tags=(), with_id=True):
        record = {"ts": self.now, "role": role, "tags": list(tags), "text": text}
        if with_id:
            record[_ID] = records.new_record_id()
        ledger.append(record)
        index.rebuild()
        return record.get(_ID)

    async def _call(self, name, args):
        """Every test reaches the server through the SDK's in-memory client (mcp_test_support) — the real
        protocol path a caller sees — never through the server object's internals."""
        return await mts.call_tool_json(srv.server, name, args)


class ToolWiringTests(_ServerBase):
    async def test_health_is_content_free_and_fixed_identity(self):
        with mock.patch.object(index, "search", side_effect=AssertionError("health read memory")), \
             mock.patch.object(ledger, "iter_records", side_effect=AssertionError("health read ledger")):
            data = await self._call("health", {})
        # Fixed identity plus the stranding log's readiness — three content-free facts (armed, tier, loaded
        # code version), never a path or a record; the exact key set is pinned so nothing else can creep in.
        self.assertEqual({key: data[key] for key in ("status", "server")},
                         {"status": "ok", "server": "engine-memory"})
        self.assertEqual(set(data), {"status", "server", "diagnostics"})
        self.assertEqual(set(data["diagnostics"]), {"armed", "qualification", "code_version"})
        self.assertIsInstance(data["diagnostics"]["armed"], bool)
        self.assertIn(data["diagnostics"]["qualification"], {"attended", "degraded", "none"})
        self.assertIsNone(data["diagnostics"]["code_version"])   # in-process from a live checkout, not a tree
        # And the answer conforms to the health operation's DECLARED output schema — the interface file says
        # `additionalProperties: false`, so the diagnostics block has to be declared there, not just returned.
        import validate
        schema_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "interfaces", "search.json")
        with open(schema_path, encoding="utf-8") as fh:
            out_schema = next(op["output_schema"] for op in json.load(fh)["operations"] if op["name"] == "health")
        checker = validate.Draft202012Validator(out_schema)
        self.assertEqual(list(checker.iter_errors(data)), [])
        tree = {**data, "diagnostics": {**data["diagnostics"], "code_version": "a" * 40 + "-" + "b" * 40}}
        self.assertEqual(list(checker.iter_errors(tree)), [])                  # an accepted-tree launch conforms
        self.assertTrue(list(checker.iter_errors({**data, "surprise": 1})))    # unknown keys still rejected
        # `diagnostics` is declared but OPTIONAL: every live helper's health keeps the same fixed, content-free
        # required signature (`status`, `server` — pinned across helpers by test_interface), and this server
        # adds its readiness block on top of it.
        self.assertEqual(list(checker.iter_errors({"status": "ok", "server": "engine-memory"})), [])

    @unittest.skipUnless(srv._semantic_installed(), "the optional semantic module is not installed here")
    async def test_the_meaning_operations_answer_matches_its_declared_schema(self):
        # The contract declares `additionalProperties: false`, so a key the server sends and the interface
        # does not name is a contract breach. Nothing validated this operation's shape, which is why one
        # survived until a cold review found it by hand.
        import json as _json
        import jsonschema

        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        with open(os.path.join(root, ".engine", "interfaces", "search.json"), encoding="utf-8") as fh:
            declared = _json.load(fh)
        schema = next(op["output_schema"] for op in declared["operations"]
                      if op["name"] == "recall-by-meaning")
        self.add("We ruled out a cron job and hooked the calendar instead.", role="decision")
        for query in ("did we consider running it on a timer",       # a hit
                      "zzzqqx nothing here matches this at all"):    # and an empty answer
            with self.subTest(query=query):
                out = await self._call("recall-by-meaning", {"query": query})
                jsonschema.validate(out, schema)

    async def test_tools_list_is_exactly_the_declared_operations(self):
        # The server answers its declared operation sets and nothing else — an undeclared tool would be a
        # private detail no other conforming implementation would offer, breaking a caller that relied on it.
        #
        # DERIVED FROM THE CONTRACTS, not from a literal: the operations are read out of the declarations
        # themselves, so this fails if a tool is added without declaring it. TWO declarations are in play
        # because the writes are a separate contract — `search.json` describes recall as never changing what
        # is stored, so the operator's controls could not be declared beside it without making that false.
        #
        # SERVED IS A SUBSET OF DECLARED, not an equality, and the difference is deliberate. Two operations —
        # permanent erasure and the secret re-scrub — are declared BECAUSE a reader of the contract must know
        # the capability exists and where it lives, and are NOT served BECAUSE serving them would defeat what
        # makes them safe: each is a command-line verb that a person runs at a terminal, and a callable tool
        # would be exactly the model-reachable path they are built to refuse. Their descriptions say so. The
        # property that actually matters is the one below: the server offers nothing it has not declared.
        #
        # TWO SHAPES ARE REAL, so both are covered rather than whichever this checkout happens to be:
        # `recall-by-meaning` is registered only where the optional semantic module is installed, and a
        # deployment without it offers the rest alone.
        here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(srv.__file__))))
        declared, unserved = set(), set()
        for slug in ("search", "memory-control"):
            with open(os.path.join(here, "interfaces", f"{slug}.json"), encoding="utf-8") as fh:
                for op in json.load(fh)["operations"]:
                    declared.add(op["name"])
                    if "NOT SERVED AS A TOOL" in op.get("description", ""):
                        unserved.add(op["name"])
        expected = declared - unserved
        if not srv._semantic_installed():
            expected -= {"recall-by-meaning"}
        tools = await mts.list_tool_objects(srv.server)
        names = {t.name for t in tools}
        self.assertEqual(names, expected)
        # The descriptions are load-bearing here — they are how a caller learns that permanent erasure is
        # deliberately NOT served — so a regression that drops them must not hide behind a name-set match.
        for t in tools:
            self.assertTrue((t.description or "").strip(), f"tool {t.name!r} lost its description")
        self.assertTrue(unserved, "no operation is declared as unserved — this assertion has stopped biting")
        self.assertFalse(names & unserved,
                         "an operation declared NOT SERVED is being served — the terminal gate is bypassed")

    @unittest.skipUnless(srv._semantic_installed(), "the optional semantic module is not installed here")
    async def test_the_meaning_operation_returns_the_passage_and_no_closeness_figure(self):
        # Measured, nearness does not track relevance: an irrelevant question outscored a correct reworded
        # match. A figure beside a result is read as confidence whatever the description says, so the
        # transport relays the matched passage and the ordering and nothing that looks like a score.
        self.add("We ruled out a cron job and hooked the calendar instead.", role="decision")
        data = await self._call("recall-by-meaning",
                                {"query": "did we consider running it on a timer"})
        self.assertTrue(data["results"], "expected the reworded question to reach the record")
        for entry in data["results"]:
            self.assertNotIn("score", entry)
            self.assertTrue(entry.get("passage"))

    async def test_an_uninstalled_module_reads_as_absent_even_though_its_folder_remains(self):
        # The honest-absence law, tested against the way it actually breaks. Removing a module deletes its
        # files and leaves the directory, and Python resolves an empty directory as a namespace package — so
        # a plain "can I find this package?" probe answered YES for an uninstalled module, registered the
        # tool, and crashed on the first call. A namespace package has no `origin`; a real module file does.
        import importlib.util

        real = importlib.util.find_spec

        class _NamespaceLike:
            """What importlib hands back for a directory with no module file in it: a spec with no origin."""

            origin = None

        def emptied(name, *args, **kwargs):
            # A STAND-IN, never the real spec: importlib caches specs, so mutating one would leave
            # `origin = None` set for the rest of the process and quietly fail every later check. Discovered
            # by the full suite — this test passed alone and broke two others when run with them.
            if name == "memory.semantic.store":
                return _NamespaceLike()
            return real(name, *args, **kwargs)

        importlib.util.find_spec = emptied
        try:
            self.assertFalse(srv._semantic_installed(),
                             "an emptied module directory must not read as installed")
        finally:
            importlib.util.find_spec = real
        # Deliberately NOT asserting the module is present afterwards: this test file is owned by the always-
        # present memory module, so it also runs on a deployment where the operator declined the semantic
        # add-on. Asserting its presence would fail in exactly the configuration the decline path exists for.

    async def test_recall_window_reads_a_sessions_conversation_back(self):
        # The read side of the transcript-first substrate: raw turns are excluded from every ranked path, so
        # this is the only way the exact wording comes back.
        for seq, (speaker, text) in enumerate([("user", "shall we cache the roster"),
                                               ("assistant", "yes, with a short expiry")]):
            ledger.append({"v": 1, "kind": records.AMBIENT_CAPTURE_KIND, _ID: records.new_record_id(),
                           "session_id": "s-live", "ts": self.now, "seq": seq, "speaker": speaker,
                           "text": text, "tags": ["transcript", "stop"]})
        out = await self._call("recall-window", {"session_id": "s-live"})
        self.assertEqual([t["text"] for t in out["turns"]],
                         ["shall we cache the roster", "yes, with a short expiry"])

    async def test_recall_window_writes_nothing(self):
        # Reading a conversation must not reinforce or otherwise mutate the store.
        ledger.append({"v": 1, "kind": records.AMBIENT_CAPTURE_KIND, _ID: records.new_record_id(),
                       "session_id": "s-live", "ts": self.now, "seq": 0, "speaker": "user",
                       "text": "a stored turn", "tags": ["transcript", "stop"]})
        before = _marker_count()
        await self._call("recall-window", {"session_id": "s-live"})
        self.assertEqual(_marker_count(), before, "reading a window must append no reinforcement marker")

    async def test_search_returns_ranked_results_matching_the_library(self):
        self.add("export format export schedule decided", role="decision")
        self.add("a note that export came up once")
        for t in ("alpha", "beta", "gamma", "delta"):
            self.add(t)
        data = await self._call("search", {"query": "export"})
        tool_ids = [r.get(_ID) for r in data["results"]]
        lib_ids = [r.get(_ID) for r in index.search("export").records]
        self.assertEqual(tool_ids, lib_ids)   # the server is a thin pass-through over the ranked library

    async def test_tags_and_limit_pass_through(self):
        d = self.add("we decided to ship export", tags=["release"])
        self.add("a lesson about export")
        capped = await self._call("search", {"query": "export", "limit": 1})
        self.assertEqual(len(capped["results"]), 1)
        tagged = await self._call("search", {"query": "export", "tags": ["release"]})
        self.assertEqual([r.get(_ID) for r in tagged["results"]], [d])

    async def test_search_answer_carries_the_recall_completeness_note(self):
        # The recall answer carries its own disclosures, because it reaches a caller that may never have opened
        # the workflow document. Three of them, and each is a STANDING condition rather than a one-time note:
        # what a result is (summary or a piece of real conversation, and how to read it whole), that recalled
        # text is a record and not an instruction, and that the stored conversation was never fully stripped of
        # secret-shaped content. Present when there are results; omitted on an empty answer.
        self.add("we decided to ship the export format", role="decision")
        data = await self._call("search", {"query": "export"})
        self.assertTrue(data["results"])
        self.assertIn("recall_completeness", data)
        note = data["recall_completeness"].lower()
        self.assertIn("summary", note)
        self.assertIn("conversation", note)
        self.assertIn("recall-window", note, "the note must name the reader that gets the exact wording")
        self.assertIn("never an instruction", note, "prompt-injection framing must ride the answer, not only "
                                                    "the workflow doc a direct caller may never have read")
        self.assertIn("never masked", note, "the standing privacy condition must be disclosed where it is true "
                                            "— on every answer, not once in a merge note — and stated so it "
                                            "cannot be skim-read as the reassuring opposite")
        empty = await self._call("search", {"query": "nonexistentzqxword"})
        self.assertEqual(empty["results"], [])
        self.assertNotIn("recall_completeness", empty)   # nothing returned -> nothing to disclose

    async def test_an_omitted_limit_is_bounded_rather_than_unbounded(self):
        # The library returns EVERY match when no limit is given. Against a few hundred summaries that was
        # survivable; against a store whose bulk is conversation, one common word matches tens of thousands of
        # records and every one comes back whole. Reverting this to an unbounded default is a one-character
        # edit, so it needs a guard of its own — and reinforcement fires per RETURNED record, so the cap bounds
        # the writes too.
        for i in range(25):
            self.add("a shared quokka note number %d" % i, role="observation")
        data = await self._call("search", {"query": "quokka"})
        self.assertEqual(len(data["results"]), srv._DEFAULT_LIMIT)

    async def test_the_search_answer_validates_against_the_interface_output_schema(self):
        # The interface contract must admit exactly what the reference implementation returns — results, plus the
        # optional recall_completeness note it carries when there are results. Without the widening the note would
        # fail a strict conformance build, and the completeness disclosure would have to be dropped.
        import json
        import validate
        schema_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "interfaces", "search.json")
        with open(schema_path, encoding="utf-8") as fh:
            operations = json.load(fh)["operations"]
            out_schema = next(op["output_schema"] for op in operations if op["name"] == "search")
        checker = validate.Draft202012Validator(out_schema)
        self.add("we decided to ship the export format", role="decision")
        answer = await self._call("search", {"query": "export"})
        self.assertIn("recall_completeness", answer)
        self.assertEqual(list(checker.iter_errors(answer)), [])        # a note-bearing answer conforms
        empty = await self._call("search", {"query": "nonexistentzqxword"})
        self.assertEqual(list(checker.iter_errors(empty)), [])         # an empty answer conforms
        self.assertTrue(list(checker.iter_errors({"results": [], "surprise": 1})))  # unknown keys still rejected

    async def test_search_still_answers_when_fts5_absent(self):
        # Availability law: with the fast lookup off, the server still returns recall (via the slow scan).
        self.add("export decision", role="decision")
        original = index.fts5_available
        index.fts5_available = lambda *a, **k: False
        try:
            data = await self._call("search", {"query": "export"})
            self.assertTrue(len(data["results"]) >= 1)
        finally:
            index.fts5_available = original


class ControlToolTests(_ServerBase):
    """The three tools that WRITE, exercised through the server rather than through the library beneath it —
    the transport is where a caller actually meets them, and where a wrong shape would surface."""

    async def test_pinning_stores_the_text_and_makes_it_findable(self):
        out = await self._call("pin", {"text": "Always ask before filing an issue."})
        self.assertTrue(out["id"])
        self.assertEqual(out[records.PIN_VIA_KEY], records.PIN_VIA_ASSISTANT)
        found = await self._call("search", {"query": "filing"})
        self.assertEqual(len(found["results"]), 1)

    async def test_a_pin_is_scrubbed_at_the_transport_too(self):
        # The tool is the path a model actually uses, so the scrub has to hold here and not only in the library
        # — this is the call that would carry a credential a session had just been shown.
        out = await self._call("pin", {"text": "token sk-ant-api03-" + "A" * 32})
        self.assertNotIn("sk-ant-api03", out["text"])

    async def test_pinning_returns_the_total_and_warns_only_when_the_list_grows_long(self):
        # Warn, never refuse (engine-template#950): every pin is saved in full; once the list is long the handler
        # adds a plain prune nudge, keyed off the LIVE count (not boot's render dial — no cross-layer import).
        from memory import pins as _pins
        for i in range(_pins.PIN_PRUNE_HINT_AT - 1):
            out = await self._call("pin", {"text": f"standing preference number {i}"})
            self.assertEqual(out["total"], i + 1)
            self.assertNotIn("note", out)                       # below the hint threshold: no nudge
        out = await self._call("pin", {"text": "one more standing preference"})
        self.assertEqual(out["total"], _pins.PIN_PRUNE_HINT_AT)  # nothing refused — all saved
        self.assertIn("note", out)                              # at the threshold: a prune nudge appears
        self.assertIn("prune", out["note"].lower())

    async def test_withhold_and_restore_round_trip_through_the_server(self):
        rid = self.add("a decision that was withdrawn", role="decision")
        self.assertEqual(len((await self._call("search", {"query": "withdrawn"}))["results"]), 1)
        said = await self._call("withhold", {"record_id": rid})
        self.assertIn("still saved", said["withheld"])          # never reads as erasure
        self.assertEqual((await self._call("search", {"query": "withdrawn"}))["results"], [])
        report = await self._call("list-withheld", {})
        self.assertEqual(report["notes"][0]["id"], rid)
        self.assertEqual(set(report["notes"][0]), {"id", "kind", "withheld_at"})
        self.assertNotIn("withdrawn", json.dumps(report).casefold())
        legacy_query = await self._call("list-withheld", {"query": "withdrawn"})
        self.assertEqual(legacy_query, report,
                         "an ignored legacy argument must not become a withheld-content oracle")
        back = await self._call("restore", {"record_id": rid})
        self.assertIn("back in recall", back["restored"])
        self.assertEqual(len((await self._call("search", {"query": "withdrawn"}))["results"]), 1)

    async def test_withholding_names_exactly_one_target(self):
        # Both, or neither, is refused rather than guessed at: a record id and a session id are both uuid hex,
        # so a wrong guess here withholds something the operator never named. Over the protocol a refusal
        # arrives as an ERROR RESULT, not a raised exception — asserting through the expect-error helper is
        # what keeps this test biting (a plain call_tool_json here would itself raise, proving nothing).
        for args in ({}, {"record_id": "r", "session_id": "s"}):
            text = await mts.call_tool_expect_error(srv.server, "withhold", args)
            self.assertTrue(text, "the refusal must say why, not fail silently")

    async def test_no_control_tool_removes_a_record_from_the_ledger(self):
        # The whole safety story of these tools is that they append. If one ever deletes, the reversibility
        # every description promises becomes false and the erasure wall stops being the only way out.
        rid = self.add("something to take out of recall")
        before = sum(1 for _ in ledger.iter_records())
        await self._call("withhold", {"record_id": rid})
        await self._call("pin", {"text": "a standing note"})
        after = list(ledger.iter_records())
        self.assertGreater(len(after), before)
        self.assertIn(rid, {r.get(_ID) for r in after})

    async def test_search_can_be_scoped_to_one_conversation(self):
        for sid in ("s-A", "s-B"):
            for i in range(3):
                ledger.append({"v": 1, "kind": records.AMBIENT_CAPTURE_KIND, _ID: records.new_record_id(),
                               "session_id": sid, "seq": i, "speaker": "user", "ts": self.now + i,
                               "text": f"{sid} talking about wombats"})
        index.rebuild()
        whole = await self._call("search", {"query": "wombats", "limit": 50})
        scoped = await self._call("search", {"query": "wombats", "session": "s-B", "limit": 50})
        self.assertEqual(len(whole["results"]), 6)
        self.assertEqual({r["session_id"] for r in scoped["results"]}, {"s-B"})


class StdioLaunchTest(unittest.IsolatedAsyncioTestCase):
    async def test_server_launches_over_stdio_and_answers_health(self):
        # The launch seam .mcp.json actually uses — the one place a dead server fails SILENTLY in a real
        # deployment (it just never appears in the model's tool list). The in-memory tests above never run
        # `server.run()`, never resolve the frozen environment, and never complete a handshake; this one
        # runs the documented argv as a real subprocess and asserts the handshake and the health answer.
        # HEALTH-ONLY: the stdio child gets an allowlisted env, so the ENGINE_MEMORY_DIR test override
        # cannot reach it — a richer call here would hit the operator's REAL store (see stdio_health).
        engine_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        data = await mts.stdio_health(engine_dir, "tools/memory/mcp_server.py")
        # Fixed identity plus the stranding log's readiness — three content-free facts (armed, tier, loaded
        # code version), never a path or a record; the exact key set is pinned so nothing else can creep in.
        self.assertEqual({key: data[key] for key in ("status", "server")},
                         {"status": "ok", "server": "engine-memory"})
        self.assertEqual(set(data), {"status", "server", "diagnostics"})
        self.assertEqual(set(data["diagnostics"]), {"armed", "qualification", "code_version"})
        self.assertIsInstance(data["diagnostics"]["armed"], bool)
        self.assertIn(data["diagnostics"]["qualification"], {"attended", "degraded", "none"})
        self.assertIsNone(data["diagnostics"]["code_version"])   # launched from this checkout, not a tree


class DemoTests(unittest.TestCase):
    def test_help_prints_usage_names_the_launch_chain_and_never_serves(self):
        # The #807 guard: --help / -h exit 0 with usage on stdout naming the accepted-hook launch chain, and
        # server.run (the outermost seam) is never entered; a flag anywhere in argv counts.
        for argv in (["--help"], ["-h"], ["demo", "--help"]):
            calls, out, err = [], io.StringIO(), io.StringIO()
            with mock.patch.object(srv.server, "run", side_effect=lambda *a, **k: calls.append("run")), \
                 contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(srv.main(argv), 0, argv)
            self.assertIn("usage: mcp_server.py", out.getvalue())
            self.assertIn("accepted_hook_dispatch.py", out.getvalue())
            self.assertIn("attended-memory-mcp", out.getvalue())
            self.assertEqual(calls, [], argv)

    def test_an_unknown_flag_or_bare_word_is_rejected_without_serving(self):
        for argv, stray in ((["--bogus"], "--bogus"), (["help"], "help"), (["serve"], "serve"), (["demo", "extra"], "extra"),
                            (["demo", "demo"], "demo")):
            calls, out, err = [], io.StringIO(), io.StringIO()
            with mock.patch.object(srv.server, "run", side_effect=lambda *a, **k: calls.append("run")), \
                 mock.patch.object(srv, "_demo", side_effect=lambda *a, **k: calls.append("demo")), \
                 contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(srv.main(argv), 2, argv)
            self.assertIn("usage: mcp_server.py", err.getvalue())
            self.assertIn(f"unknown argument {stray!r}", err.getvalue())     # names the wrong word, never `demo`
            self.assertEqual(out.getvalue(), "")
            self.assertEqual(calls, [], argv)

    def test_no_arguments_and_none_both_serve(self):
        for argv in ([], None):
            calls = []
            with mock.patch.object(srv.server, "run", side_effect=lambda *a, **k: calls.append("run")):
                self.assertEqual(srv.main(argv), 0)
            self.assertEqual(calls, ["run"], argv)

    def test_the_exit_status_reaches_the_process(self):
        # A bounded wait, since this machine has no `timeout` command: on the unfixed tool the --help run
        # would block serving stdio and the wait would expire.
        import subprocess
        script = os.path.abspath(srv.__file__)
        helped = subprocess.run([sys.executable, script, "--help"], capture_output=True, text=True, timeout=10,
                                stdin=subprocess.DEVNULL)
        self.assertEqual(helped.returncode, 0, helped.stderr)
        self.assertIn("attended-memory-mcp", helped.stdout)
        rejected = subprocess.run([sys.executable, script, "--bogus"], capture_output=True, text=True, timeout=10,
                                  stdin=subprocess.DEVNULL)
        self.assertEqual(rejected.returncode, 2, rejected.stdout)
        self.assertIn("usage: mcp_server.py", rejected.stderr)
        twice = subprocess.run([sys.executable, script, "demo", "demo"], capture_output=True, text=True, timeout=10,
                               stdin=subprocess.DEVNULL)
        self.assertEqual(twice.returncode, 2, twice.stderr)      # never a traceback (repair round 2)
        self.assertNotIn("Traceback", twice.stderr)

    def test_demo_body_exits_zero(self):
        # The operator demo exercises the REAL rank + filter + reinforce on its own throwaway cabinet; a real
        # regression flips a `!!!` and returns non-zero. (It manages its own ENGINE_MEMORY_DIR.)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(srv._demo(), 0)



class UnqualifiedTierTests(unittest.TestCase):
    """Every tool this server publishes lands in exactly one tier, and the refusing ones say something an
    operator can act on. StarshipSuperjam/engine-template#1153 refused all of them; the failure that mattered
    was not the refusal but that nothing could still be READ."""

    #: Each published tool mapped to the registry entry its call actually authorizes. A tool absent from this
    #: map fails the completeness test below — the point is that a new tool cannot be added without deciding,
    #: in the open, what an unqualified session may do with it.
    TOOL_ENTRIES = {
        "health": "read-memory-health",
        "search": "attended-keyword-mcp-search",
        "recall-window": "read-recall-window",
        "recall-by-meaning": "attended-semantic-mcp-search",
        "list-pins": "read-pins",
        "list-withheld": "read-withheld",
        "pin": "attended-pin-add",
        "withhold": "attended-withhold",
        "restore": "attended-restore-withheld",
    }
    READS = ("health", "search", "recall-window", "recall-by-meaning", "list-pins", "list-withheld")
    WRITES = ("pin", "withhold", "restore")

    def _published(self):
        tools = getattr(srv.server, "_tool_manager", None)
        names = list(tools._tools) if tools is not None else []
        return sorted(names)

    def test_every_published_tool_is_classified_and_none_is_unassigned(self):
        published = set(self._published())
        self.assertTrue(published, "the server published no tools to classify")
        self.assertEqual(published - set(self.TOOL_ENTRIES), set(),
                         "a published tool has no declared tier")
        self.assertEqual(set(self.TOOL_ENTRIES) - published, set(),
                         "the tier map names a tool the server no longer publishes")
        self.assertEqual(sorted(self.READS + self.WRITES), sorted(self.TOOL_ENTRIES))

    def test_every_read_tool_answers_without_qualification(self):
        for tool in self.READS:
            entry = mutation_contract.entry_by_id(self.TOOL_ENTRIES[tool])
            with self.subTest(tool):
                self.assertEqual(mutation_contract.degraded_disposition(entry), "allow")

    def test_the_three_attended_verbs_refuse_and_say_what_makes_it_stick(self):
        for tool in self.WRITES:
            entry = mutation_contract.entry_by_id(self.TOOL_ENTRIES[tool])
            with self.subTest(tool):
                self.assertEqual(mutation_contract.degraded_disposition(entry), "refuse")
                reply = mutation_contract.degraded_refusal(entry)
                self.assertIn("session start", reply)
                self.assertNotIn("execution context", reply)
                self.assertNotIn("registry", reply)
                self.assertLess(len(reply), 500)

    def test_the_withhold_refusal_names_the_erase_chain_consequence(self):
        """It must say the note is still findable, that the erase request did NOT register, and — the part
        the repair review caught it getting wrong — that erasing is the operator's own terminal step, not
        something a later session can complete for them by being asked again."""
        reply = mutation_contract.degraded_refusal(mutation_contract.entry_by_id("attended-withhold"))
        self.assertIn("still there", reply)
        self.assertIn("eras", reply)
        self.assertIn("nothing was registered", reply)
        self.assertIn("terminal", reply)


class RefusalTranslationTests(unittest.IsolatedAsyncioTestCase):
    """The three plain-word refusals this server raises must reach the client whole.

    Under mcp 2.1.1 the tool boundary forwards only a `ToolError`'s message to the client — every other
    exception is flattened to a bare "Error executing tool <name>". The `_tool` registration helper
    translates each refusal to `ToolError(str(exc))`, so its designed sentence survives under 2.1.1 as
    it always did under 2.0.0 (whose boundary returned `str(exc)` for any exception). These tests reach
    the shipped helper directly, so they hold under whichever mcp version is installed.
    """

    # A distinctive sentence per translated type. The point is not the wording but that the WHOLE
    # sentence crosses; a "crash" case that is NOT one of the translated types proves the translation is
    # selective and leaves a genuine fault to be masked as the unexpected crash it is.
    SENTENCES = {
        "mutation": "MUTATION-REFUSAL: this session is not qualified to write memory yet, so nothing was written.",
        "pin": "PIN-REFUSAL: that pin is over the length cap and was refused rather than silently shortened.",
        "control": "CONTROL-REFUSAL: nothing was registered to forget, and erasing stays your own terminal step.",
        "crash": "CRASH: an internal detail that must never be dressed up as a refusal.",
    }

    def _probe(self):
        """Register a probe tool through the REAL `srv._tool` helper on a throwaway server, so the
        shipped translation path is exercised without publishing an extra tool on the module server.
        Returns the throwaway server and the wrapper the helper produced (for direct, version-independent
        assertions)."""
        fresh = MCPServer("refusal-probe")
        with mock.patch.object(srv, "server", fresh):
            @srv._tool(name="probe", description="Raises the named refusal (or a crash), for the translation test.")
            def probe(which: str) -> dict:
                if which == "mutation":
                    raise mutation_authority.MutationAuthorityError(self.SENTENCES["mutation"])
                if which == "pin":
                    raise pins.PinRefused(self.SENTENCES["pin"])
                if which == "control":
                    raise forget.ControlNotRecorded(self.SENTENCES["control"])
                if which == "crash":
                    raise RuntimeError(self.SENTENCES["crash"])
                return {"ok": which}
        return fresh, probe

    async def test_each_refusal_sentence_arrives_whole_over_the_protocol(self):
        fresh, _ = self._probe()
        for which in ("mutation", "pin", "control"):
            with self.subTest(which):
                # call_tool_expect_error asserts is_error is true and hands back the text content. The
                # ONE cross-version invariant is that the whole sentence arrives: under 2.0.0 the
                # boundary returns str(exc) (a bare exception's text also crossed, prefixed with
                # "Error executing tool <name>:"), while under 2.1.1 only a ToolError's message crosses
                # and every other exception is flattened to that prefix WITHOUT the sentence. So the
                # sentence's presence is exactly what the translation buys under 2.1.1; selectivity is
                # asserted separately below, version-independently.
                text = await mts.call_tool_expect_error(fresh, "probe", {"which": which})
                self.assertIn(self.SENTENCES[which], text)  # the WHOLE sentence, not a truncation
                self.assertNotIn("Traceback", text)         # a refusal, never a crash dump

    def test_translation_is_selective_only_named_refusals_become_toolerror(self):
        """A version-independent check on the wrapper itself: exactly the three named refusals become a
        `ToolError` carrying the sentence intact, and a genuine fault propagates unchanged so the
        boundary can mask it rather than the helper laundering it into a refusal."""
        _, probe = self._probe()
        for which in ("mutation", "pin", "control"):
            with self.subTest(translated=which):
                with self.assertRaises(ToolError) as caught:
                    probe(which)
                self.assertEqual(str(caught.exception), self.SENTENCES[which])
        with self.subTest(not_translated="crash"):
            with self.assertRaises(RuntimeError) as caught:
                probe("crash")
            self.assertNotIsInstance(caught.exception, ToolError)

    def test_the_guard_marker_survives_the_translation_wrapper(self):
        """search and recall-by-meaning carry a `@_mutation_authority.guard` beneath the `_tool` wrapper.
        `functools.wraps` must copy its `__engine_registry_id__` onto the wrapper so
        `install_module_guards` still recognises the writer when it rebinds the module globals, and the
        guard-coverage enumeration still sees it."""
        ids = mutation_authority.guarded_registry_ids(vars(srv))
        self.assertIn("attended-keyword-mcp-search", ids)
        self.assertEqual(getattr(srv.search, "__engine_registry_id__", None),
                         "attended-keyword-mcp-search")
        if srv._semantic_installed():
            self.assertIn("attended-semantic-mcp-search", ids)
            self.assertEqual(getattr(srv.recall_by_meaning, "__engine_registry_id__", None),
                             "attended-semantic-mcp-search")


class ReadCaveatWiringTests(_ServerBase):
    """All four read tools attach the stale-context caveat when the probe reports one and omit it when the probe
    is silent — driven through the real in-memory client path with `_memory_read_caveat` stubbed, so this pins
    the wiring independently of how staleness is detected."""

    def _seed_conversation(self):
        ledger.append({"v": 1, "kind": records.AMBIENT_CAPTURE_KIND, _ID: records.new_record_id(),
                       "session_id": "s1", "seq": 1, "speaker": "user", "ts": self.now,
                       "text": "a searchable line about widgets"})
        index.rebuild()

    async def test_each_read_tool_attaches_the_caveat_when_the_probe_reports_one(self):
        self._seed_conversation()
        sentinel = "STALE-CONTEXT-SENTINEL"
        calls = [("search", {"query": "widgets"}),
                 ("recall-window", {"session_id": "s1"}),
                 ("list-pins", {})]
        if srv._semantic_installed():
            calls.append(("recall-by-meaning", {"query": "widgets"}))
        with mock.patch.object(srv, "_memory_read_caveat", return_value=sentinel):
            for name, args in calls:
                with self.subTest(name):
                    out = await self._call(name, args)
                    self.assertEqual(out.get("memory_caveat"), sentinel)

    async def test_no_read_tool_attaches_a_caveat_when_the_probe_is_silent(self):
        self._seed_conversation()
        with mock.patch.object(srv, "_memory_read_caveat", return_value=None):
            for name, args in (("search", {"query": "widgets"}),
                               ("recall-window", {"session_id": "s1"}),
                               ("list-pins", {})):
                with self.subTest(name):
                    out = await self._call(name, args)
                    self.assertNotIn("memory_caveat", out)


class OperatorMovedCommitReadTests(unittest.TestCase):
    """The headline recovery scenario. A commit lands under a running memory server, so its bound context is
    stale. Every read tool still answers — carrying one plain restart caveat — while a mutating verb refuses in
    plain words that name no path or commit; clearing the drift (the restart re-accepting the commit) removes
    the caveat. Runs against a REAL installed context, so it exercises the production authority path."""

    def setUp(self):
        from memory import test_mutation_authority as tma

        self.fixture = tma._QualifiedFixture(mcp=True)
        self.fixture.install()
        self.activation = os.path.join(
            self.fixture.common, "engine", "accepted-hooks", "activation.json")
        self._original = Path(self.activation).read_text(encoding="utf-8")

    def tearDown(self):
        self.fixture.cleanup()

    def _set_commit(self, commit):
        record = json.loads(Path(self.activation).read_text(encoding="utf-8"))
        record["commit"] = commit
        Path(self.activation).write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")

    def test_reads_answer_with_a_restart_caveat_while_a_write_refuses_and_a_restart_clears_it(self):
        # Healthy: the reads carry no caveat.
        self.assertNotIn("memory_caveat", srv.list_pins())
        self.assertNotIn("memory_caveat", srv.search("anything"))

        # A commit lands under the running server: the binding is now stale.
        self._set_commit("c" * 40)

        pins = srv.list_pins()
        self.assertIn("memory_caveat", pins)
        self.assertIn("restart", pins["memory_caveat"])
        self.assertEqual(pins["pins"], [])  # the read still answers, it is not an error

        found = srv.search("anything")
        self.assertIn("results", found)
        self.assertIn("restart", found["memory_caveat"])

        # A mutating verb refuses in plain words — no path, commit or fingerprint reaches the caller.
        with self.assertRaises(ToolError) as caught:
            srv.pin("must not be written while stale")
        message = str(caught.exception)
        self.assertIn("restart", message)
        self.assertNotIn("c" * 40, message)
        self.assertNotIn(self.fixture.base, message)
        self.assertNotIn("fingerprint", message)

        # The restart re-accepts the current commit: the caveat is gone and writes would resume.
        Path(self.activation).write_text(self._original, encoding="utf-8")
        execution_context._CURRENT_CONTEXT = self.fixture.context
        self.assertNotIn("memory_caveat", srv.list_pins())

    def test_unstattable_root_reads_answer_and_write_refuses_cleanly(self):
        # The residual crash #1199 missed: when revalidate_context's identity reads (_path_identity ->
        # os.stat on the project root / Git common directory) fail under drift, the raw OSError used to
        # escape untyped and crash every read and write. revalidate_context now types any OSError from the
        # matched body as ArtifactUnreadable (a ContextError), so it routes through the SAME degrade/refuse
        # path as any stale binding — reads answer with the restart caveat, and a write refuses cleanly with
        # the store-on-disk refusal and no path leaked.
        from unittest import mock

        # Seed a real record so the degraded read is shown to return actual content via the ledger-scan
        # fallback, not merely an empty-but-caveated shell.
        ledger.append({"v": 1, "kind": records.AMBIENT_CAPTURE_KIND, _ID: records.new_record_id(),
                       "session_id": "s-unstat", "ts": int(time.time()), "seq": 0, "speaker": "user",
                       "text": "a line recalled while the root is unreadable", "tags": ["transcript", "stop"]})

        with mock.patch.object(execution_context, "_path_identity", side_effect=OSError(13, "denied")):
            pins = srv.list_pins()
            self.assertIn("memory_caveat", pins)
            self.assertIn("restart", pins["memory_caveat"])
            self.assertEqual(pins["pins"], [])  # the read answers, it is not an error

            found = srv.search("anything")
            self.assertIn("results", found)
            self.assertIn("restart", found["memory_caveat"])

            # Real content still comes back through the degraded recall path (the ledger scan), proving the
            # read genuinely answers rather than silently returning nothing under the escape.
            window = srv.recall_window("s-unstat")
            self.assertIn("restart", window["memory_caveat"])
            self.assertTrue(any("unreadable" in str(turn) for turn in window.get("turns", [])),
                            "degraded recall must return the seeded turn, not an empty answer")

            with self.assertRaises(ToolError) as caught:
                srv.pin("must not be written while the root is unreadable")
            message = str(caught.exception)
            self.assertIn("writing is held", message)  # refused cleanly — nothing was changed
            self.assertIn("fresh session", message)  # names how the operator recovers
            self.assertNotIn(self.fixture.base, message)  # content-free: no path leaks to the caller

    def test_recall_window_and_recall_by_meaning_carry_the_caveat_while_stale(self):
        # Obligation 4 end-to-end for the other two read tools. The headline test above proves list-pins and
        # search; this proves recall-window and recall-by-meaning against the SAME real moved-commit staleness
        # (not a stubbed probe), so all four read tools' real staleness-to-caveat path is covered. recall-by-
        # meaning exists only where the semantic add-on is installed, so it is exercised conditionally.
        semantic = srv._semantic_installed()
        ledger.append({"v": 1, "kind": records.AMBIENT_CAPTURE_KIND, _ID: records.new_record_id(),
                       "session_id": "s-live", "ts": int(time.time()), "seq": 0, "speaker": "user",
                       "text": "a line to recall", "tags": ["transcript", "stop"]})

        # Healthy: the reads carry no caveat.
        self.assertNotIn("memory_caveat", srv.recall_window("s-live"))
        if semantic:
            self.assertNotIn("memory_caveat", srv.recall_by_meaning("anything"))

        # A commit lands under the running server: the binding is now stale.
        self._set_commit("c" * 40)

        window = srv.recall_window("s-live")
        self.assertIn("memory_caveat", window)
        self.assertIn("restart", window["memory_caveat"])
        self.assertIn("turns", window)  # the read still answers; it is not an error

        if semantic:
            meaning = srv.recall_by_meaning("anything")
            self.assertIn("memory_caveat", meaning)
            self.assertIn("restart", meaning["memory_caveat"])
            self.assertIn("results", meaning)


from memory import stranding_log as _stranding_log  # noqa: E402 — the in-server diagnostic under test below

_SECRET = "hunter2-SECRET-TOKEN-9f8e7d"


def _fault_carrying_the_secret():
    """Raise with the secret in every place a careless trace could read it from: the message, the args, the
    cause's message, a note, a local variable, and this very source line."""
    local_copy = _SECRET
    try:
        raise ValueError("cause " + _SECRET)
    except ValueError as inner:
        outer = RuntimeError("outer " + local_copy)
        outer.add_note("note " + _SECRET)
        raise outer from inner


def _probe_server():
    """A throwaway RECORDING server with one tool registered through the REAL `srv._tool` helper, so the
    seam wiring is exercised without publishing an extra tool on the module server."""
    fresh = srv._RecordingServer("stranding-probe")
    with mock.patch.object(srv, "server", fresh):
        @srv._tool(name="probe", description="Raises a refusal or a crash, for the stranding-log wiring test.")
        def probe(which: str) -> dict:
            if which == "refusal":
                raise mutation_authority.MutationAuthorityError("REFUSAL: a designed sentence")
            if which == "crash":
                raise RuntimeError("CRASH: " + _SECRET)
            if which == "unconvertible":
                return {"ok": _Unconvertible()}   # the tool returned; conversion for the wire is what fails
            return {"ok": which}
    return fresh, probe


class _Unconvertible:
    """A return value no serializer can render: every textual fallback raises."""

    def __repr__(self):
        raise ValueError("cannot be rendered " + _SECRET)

    __str__ = __repr__


class StrandingLogContentSafetyTests(unittest.TestCase):
    """What the stranding log writes — and, the whole point, what it never can."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="engine-stranding-")
        self.path = os.path.join(self.temp.name, "stranding-log.ndjson")

    def tearDown(self):
        self.temp.cleanup()

    def _fault(self):
        try:
            _fault_carrying_the_secret()
        except RuntimeError as exc:
            return exc
        raise AssertionError("the probe did not raise")

    def _text(self):
        return Path(self.path).read_text(encoding="utf-8") if os.path.exists(self.path) else ""

    def _records(self):
        return [json.loads(line) for line in self._text().splitlines() if line.strip()]

    def _record(self, exc=None, **kwargs):
        return _stranding_log.record_stranding(_stranding_log.Event.TOOL_FAULT, exc, **kwargs)

    def test_a_secret_in_message_args_chain_note_local_or_source_line_never_reaches_the_record(self):
        self.assertTrue(self._record(self._fault(), tool="search", path=self.path))
        text = self._text()
        self.assertNotIn(_SECRET, text)
        self.assertNotIn("hunter2", text)
        (record,) = self._records()
        facts = record["exception"]
        self.assertEqual(facts["type"], "builtins.RuntimeError")
        self.assertEqual(facts["chain"], ["builtins.ValueError"])
        self.assertTrue(facts["frames"])
        for basename, lineno, function in facts["frames"]:
            self.assertEqual(basename, "test_mcp_server.py")   # a basename, never a path
            self.assertIsInstance(lineno, int)
            self.assertNotIn("/", function)
        self.assertIn("_fault_carrying_the_secret", [frame[2] for frame in facts["frames"]])
        self.assertEqual(set(facts), {"type", "chain", "frames"})  # no message, no line, no locals
        self.assertEqual(record["tool"], "search")

    def test_a_secret_in_an_allowlisted_or_presence_env_value_never_reaches_the_record_or_the_export(self):
        with mock.patch.dict(os.environ, {"PYTHONNOUSERSITE": _SECRET, "ENGINE_QUALIFICATION_DEGRADED": _SECRET}):
            self.assertTrue(self._record(self._fault(), path=self.path))
            exported = _stranding_log.export_sanitized(path=self.path)
        self.assertNotIn(_SECRET, self._text())
        self.assertNotIn(_SECRET, json.dumps(exported))
        (record,) = self._records()
        self.assertEqual(record["env"], {"PYTHONNOUSERSITE": False})       # a boolean, never the value
        self.assertIs(record["env_present"]["ENGINE_QUALIFICATION_DEGRADED"], True)
        self.assertEqual(record["qualification"], "degraded")

    def test_a_dynamic_exception_name_is_replaced_unless_every_segment_is_an_identifier(self):
        weird = type("Evil/" + _SECRET, (RuntimeError,), {})
        long = type("L" * 500, (RuntimeError,), {})
        dashed = type("Boom", (RuntimeError,), {"__module__": "sk-ant-api03-" + _SECRET + ".leak"})
        for kind in (weird, long, dashed):
            try:
                raise kind("x")
            except RuntimeError as exc:
                self.assertTrue(self._record(exc, path=self.path))
        for record in self._records():
            self.assertEqual(record["exception"]["type"], "<unnamed>")
        self.assertNotIn(_SECRET, self._text())
        self.assertNotIn("sk-ant", self._text())

    def test_a_free_text_event_is_refused(self):
        self.assertFalse(_stranding_log.record_stranding("tool-fault", self._fault(), path=self.path))
        self.assertFalse(os.path.exists(self.path))

    def test_an_unwritable_or_redirected_sink_yields_false_without_raising_or_looping(self):
        locked = os.path.join(self.temp.name, "locked")
        os.makedirs(locked)
        os.chmod(locked, 0o500)
        try:
            for _ in range(3):  # repeated failure: three honest misses, no retry loop
                self.assertFalse(self._record(self._fault(), path=os.path.join(locked, "log.ndjson")))
        finally:
            os.chmod(locked, 0o700)
        real = os.path.join(self.temp.name, "elsewhere.ndjson")
        link = os.path.join(self.temp.name, "link.ndjson")
        os.symlink(real, link)
        self.assertFalse(self._record(self._fault(), path=link))     # a symlink planted at the sink
        self.assertFalse(os.path.exists(real))
        realdir = os.path.join(self.temp.name, "realdir")
        os.makedirs(realdir)
        linkdir = os.path.join(self.temp.name, "linkdir")
        os.symlink(realdir, linkdir)
        self.assertFalse(self._record(self._fault(), path=os.path.join(linkdir, "log.ndjson")))
        self.assertEqual(os.listdir(realdir), [])
        # A FIFO planted at the lock path or the sink path would block a naive open forever; it is refused
        # before anything is opened (a hang here would fail the whole run, which is the assertion).
        fifo_sink = os.path.join(self.temp.name, "fifo.ndjson")
        os.mkfifo(fifo_sink + ".lock")
        self.assertFalse(self._record(self._fault(), path=fifo_sink))
        os.mkfifo(fifo_sink)
        os.unlink(fifo_sink + ".lock")
        self.assertFalse(self._record(self._fault(), path=fifo_sink))

    def test_a_busy_lock_drops_the_record_honestly_and_a_free_one_records(self):
        import fcntl
        holder = os.open(self.path + ".lock", os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(holder, fcntl.LOCK_EX)
            self.assertFalse(self._record(self._fault(), path=self.path))
        finally:
            os.close(holder)
        self.assertTrue(self._record(self._fault(), path=self.path))
        self.assertEqual(len(self._records()), 1)

    def test_a_fault_while_building_the_record_yields_false(self):
        with mock.patch.object(_stranding_log, "_record", side_effect=RuntimeError("formatting exploded")):
            self.assertFalse(self._record(self._fault(), path=self.path))
        self.assertFalse(os.path.exists(self.path))

    def test_the_sink_rotates_once_at_the_cap_and_drops_the_older_rotation(self):
        Path(self.path).write_text("x" * _stranding_log._ROTATE_BYTES, encoding="utf-8")
        Path(self.path + ".1").write_text("older rotation\n", encoding="utf-8")
        self.assertTrue(self._record(self._fault(), path=self.path))
        self.assertEqual(len(self._records()), 1)      # a fresh sink holding only the new record
        self.assertEqual(Path(self.path + ".1").read_text(encoding="utf-8"),
                         "x" * _stranding_log._ROTATE_BYTES)   # the older rotation is gone, not kept
        self.assertTrue(self._record(self._fault(), path=self.path))
        self.assertEqual(len(self._records()), 2)      # no rotation below the cap

    def test_reading_and_exporting_cover_the_rotated_file_first_and_then_the_live_one(self):
        # The one record the log exists to keep can be the OLDER one after a rotation; an export that read
        # only the live sink would silently omit it. Both files are read, oldest first.
        older = {"schema_version": "stranding-log.v1", "ts": 1.0, "event": "tool-fault", "tool": "recall-by-meaning"}
        newer = {"schema_version": "stranding-log.v1", "ts": 2.0, "event": "read-degraded"}
        Path(self.path + ".1").write_text(json.dumps(older) + "\nnot json\n", encoding="utf-8")
        Path(self.path).write_text(json.dumps(newer) + "\n", encoding="utf-8")
        self.assertEqual([r["event"] for r in _stranding_log.read_records(path=self.path)],
                         ["tool-fault", "read-degraded"])
        self.assertEqual([r["event"] for r in _stranding_log.export_sanitized(path=self.path)],
                         ["tool-fault", "read-degraded"])
        os.unlink(self.path + ".1")
        self.assertEqual([r["event"] for r in _stranding_log.read_records(path=self.path)], ["read-degraded"])

    def test_a_short_write_is_completed_or_cut_back_and_never_fuses_two_records(self):
        real_write = os.write
        # write(2) storing fewer bytes than asked is legal; the writer keeps going until the line is whole.
        with mock.patch.object(_stranding_log.os, "write", side_effect=lambda fd, data: real_write(fd, data[:7])):
            self.assertTrue(self._record(self._fault(), tool="search", path=self.path))
        (first,) = self._records()
        self.assertEqual(first["tool"], "search")
        # A short write that then fails outright: the partial line is cut back, the miss is honest (False),
        # and the NEXT good record parses on its own line instead of fusing with the fragment.
        calls = []

        def short_then_fail(fd, data):
            calls.append(len(data))
            if len(calls) == 1:
                return real_write(fd, data[:7])
            raise OSError(28, "No space left on device")

        with mock.patch.object(_stranding_log.os, "write", side_effect=short_then_fail):
            self.assertFalse(self._record(self._fault(), tool="pin", path=self.path))
        self.assertEqual(len(calls), 2)
        self.assertTrue(self._record(self._fault(), tool="search", path=self.path))
        records = self._records()
        self.assertEqual([r["tool"] for r in records], ["search", "search"])   # the fragment is gone
        self.assertEqual(self._text().count("\n"), 2)

    def test_readiness_is_truthful_and_the_real_sink_is_gitignored(self):
        live = _stranding_log.readiness(check_ignore=True)
        self.assertEqual(set(live), {"schema_version", "armed", "reason", "registered", "guard_installed",
                                     "sink_dir_writable", "harness_gated", "sink_present", "sink_ignored",
                                     "rotated", "qualification", "code_version"})
        self.assertTrue(live["registered"])
        self.assertTrue(live["guard_installed"])
        self.assertIs(live["sink_ignored"], True)       # git check-ignore confirms the production path
        self.assertIsNone(live["code_version"])          # this module was loaded from a checkout, not a tree
        self.assertIsInstance(live["rotated"], bool)
        # Under the test harness the writer records nothing without a named file, and readiness says so
        # rather than reporting armed: the bit is truthful, not optimistic — and it says why.
        self.assertTrue(live["harness_gated"])
        self.assertFalse(live["armed"])
        self.assertIn("harness", live["reason"])
        self.assertIsNone(_stranding_log.readiness()["sink_ignored"])   # a health probe never forks git
        with mock.patch.object(_stranding_log, "_test_path_allowed", return_value=False):
            ready = _stranding_log.readiness()
            self.assertTrue(ready["armed"])                               # the production shape
            self.assertIsNone(ready["reason"])
            locked = os.path.join(self.temp.name, "locked")
            os.makedirs(locked)
            os.chmod(locked, 0o500)
            try:
                with mock.patch.object(_stranding_log, "sink_path",
                                       return_value=os.path.join(locked, "log.ndjson")):
                    ready = _stranding_log.readiness()
                    self.assertFalse(ready["armed"])
                    self.assertIn("not writable", ready["reason"])
            finally:
                os.chmod(locked, 0o700)
            # A receipt (`check_ignore`) may not call the instrument armed at a destination git cannot
            # confirm ignored — a redirected project root outside any work tree is exactly that case.
            with mock.patch.object(_stranding_log, "_is_ignored", return_value=None):
                ready = _stranding_log.readiness(check_ignore=True)
                self.assertFalse(ready["armed"])
                self.assertIn("could not confirm", ready["reason"])
            with mock.patch.object(_stranding_log, "_is_ignored", return_value=False):
                self.assertIn("NOT ignored", _stranding_log.readiness(check_ignore=True)["reason"])
            with mock.patch.object(_stranding_log, "_is_ignored", return_value=True):
                self.assertTrue(_stranding_log.readiness(check_ignore=True)["armed"])

    def test_the_frame_budget_keeps_the_engine_entry_point_over_the_sdk_plumbing_above_it(self):
        # Every tool call travels the same SDK frames before the engine; a trace that spent its outer slots
        # on them would be identical in every record and lose the tool entry point from the middle. The SDK
        # frames are placed where this deployment REALLY keeps them — under the project's own
        # `.engine/.venv/…` — so a marker that merely looked for `.engine` in the path would fail this test.
        site = os.path.join(_stranding_log._ROOT, ".engine", ".venv", "lib", "python3.12", "site-packages")
        sdk_outer = {}
        exec(compile("def o1(fn):\n    return o2(fn)\n\ndef o2(fn):\n    return fn()\n",
                     os.path.join(site, "mcp", "server", "mcpserver", "tools", "base.py"), "exec"), sdk_outer)
        sdk_inner = {}
        exec(compile("def a(fn):\n    return b(fn)\n\ndef b(fn):\n    return c(fn)\n\n"
                     "def c(fn):\n    return d(fn)\n\ndef d(fn):\n    return fn()\n",
                     os.path.join(site, "anyio", "to_thread.py"), "exec"), sdk_inner)

        def raiser():
            raise RuntimeError("deep " + _SECRET)

        def engine_tool():
            return sdk_inner["a"](raiser)

        try:
            sdk_outer["o1"](engine_tool)
        except RuntimeError as exc:
            facts = _stranding_log._exception_facts(exc)
        frames = facts["frames"]
        # Nine frames were walked (this test, o1, o2, engine_tool, a, b, c, d, raiser); six survive: the two
        # outermost ENGINE frames — this test and the tool entry point, NOT the SDK's o1/o2 above the entry
        # point — and the innermost four.
        self.assertEqual(len(frames), _stranding_log._OUTER_FRAMES + _stranding_log._INNER_FRAMES)
        # (This test's own 83-character name is over the identifier cap, so it is recorded as `<unnamed>` —
        # the cap at work, on the frame the budget rightly kept.)
        self.assertEqual([frame[2] for frame in frames[:2]], ["<unnamed>", "engine_tool"])
        self.assertEqual({frame[0] for frame in frames[:2]}, {"test_mcp_server.py"})
        self.assertNotIn("base.py", [frame[0] for frame in frames])   # the SDK plumbing above it, dropped
        self.assertEqual([frame[2] for frame in frames[2:]], ["b", "c", "d", "raiser"])   # where it broke
        self.assertNotIn(_SECRET, json.dumps(facts))

    def test_export_drops_unlisted_fields_redacts_paths_and_is_bound_to_the_cache_directory(self):
        home = os.path.expanduser("~")
        raw = {"schema_version": "stranding-log.v1", "ts": 1.0, "event": "baseline", "tool": "search",
               "observed_error": {"generic": False, "text": None, "length": 40},
               "servers": [{"pid": 1, "launcher": "accepted-tree", "code_version": "a-b"}],
               "activation": {"repository": "owner/repo", "commit": None, "tree": None,
                              "engine_release": None, "epoch": None},
               "raw": {"servers": [{"raw_argv": f"{home}/private --tree /x"}]},
               "note": f"not an exported field {home}"}
        Path(self.path).write_text(json.dumps(raw) + "\n", encoding="utf-8")
        (out,) = _stranding_log.export_sanitized(path=self.path)
        self.assertNotIn("raw", out)
        self.assertNotIn("note", out)
        self.assertEqual(out["activation"]["repository"], "owner/repo")   # a slug is identity, not a path
        self.assertEqual(out["servers"][0]["code_version"], "a-b")
        self.assertNotIn(home, json.dumps(out))
        self.assertEqual(_stranding_log._redact(f"{home}/x"), "<redacted-path>")
        self.assertEqual(_stranding_log._redact("/etc/passwd"), "<redacted-path>")
        self.assertEqual(_stranding_log._redact("a/b/c"), "<redacted-path>")
        self.assertEqual(_stranding_log._redact(r"C:\Users\x"), "<redacted-path>")
        self.assertEqual(_stranding_log._redact("read-degraded"), "read-degraded")
        # A destination anywhere but under the engine's own cache directory is refused before any write —
        # a tracked file, a temp file, and above all the store.
        for elsewhere in (os.path.join(_stranding_log._ROOT, ".gitignore"),
                          os.path.join(self.temp.name, "export.ndjson"),
                          os.path.join(_stranding_log._project_root(), ".engine", "memory", "ledger.ndjson")):
            with self.subTest(destination=elsewhere), self.assertRaises(ValueError):
                _stranding_log.export_sanitized(elsewhere, path=self.path)
            self.assertFalse(os.path.exists(elsewhere + ".tmp"))

    def test_the_baseline_records_the_visible_failure_classified_and_writes_nothing_durable(self):
        memory_dir = os.path.join(self.temp.name, "memory")
        os.makedirs(memory_dir)
        Path(os.path.join(memory_dir, "index.sqlite3")).write_bytes(b"0" * 10)
        home = os.path.expanduser("~")
        tree = "a" * 40 + "-" + "b" * 40
        servers = [{"pid": 4242, "launcher": "accepted-tree", "code_version": tree}]
        activation = {"repository": "o/r", "commit": "c" * 40, "tree": "d" * 40, "engine_release": "1.2.3",
                      "epoch": 7}
        with mock.patch.dict(os.environ, {"ENGINE_MEMORY_DIR": memory_dir}), \
             mock.patch.object(_stranding_log, "_live_servers", return_value=servers), \
             mock.patch.object(_stranding_log, "_activation_on_disk", return_value=activation):
            generic = _stranding_log.capture_baseline("Error executing tool search", tool="search",
                                                      path=self.path)
            pasted = _stranding_log.capture_baseline("bearer=" + _SECRET + " " + home, tool="Bad Name",
                                                     path=self.path)
        self.assertIsNotNone(generic)
        self.assertEqual(generic["event"], "baseline")
        # The generic boundary string is content-free and kept verbatim; anything else is summarised by
        # its length only — the pasted text never enters the sink at all.
        self.assertEqual(generic["observed_error"], {"generic": True, "text": "Error executing tool search",
                                                     "length": len("Error executing tool search")})
        self.assertEqual(pasted["observed_error"]["generic"], False)
        self.assertIsNone(pasted["observed_error"]["text"])
        self.assertNotIn(_SECRET, self._text())
        self.assertNotIn(home, self._text())
        self.assertEqual(generic["servers"], servers)
        self.assertEqual(generic["activation_on_disk"], activation)
        self.assertNotIn("raw", generic)
        self.assertTrue(generic["lifecycle"]["files"]["index.sqlite3"]["present"])
        self.assertFalse(generic["lifecycle"]["files"]["vectors.sqlite3"]["present"])
        self.assertIn("nothing is written to the durable ledger", generic["cache_effects"])
        self.assertEqual(pasted["tool"], "<unnamed>")                    # normalised, not trusted
        self.assertEqual(sorted(os.listdir(memory_dir)), ["index.sqlite3"])  # nothing durable was touched

    def test_the_server_sweep_keeps_only_this_engines_servers_under_this_account_and_never_the_argv(self):
        home = os.path.expanduser("~")
        mine, other = str(os.getuid()), str(os.getuid() + 1)
        tree = "e" * 40 + "-" + "f" * 40
        ours = os.path.realpath(os.path.join(self.temp.name, "proj", ".git"))
        elsewhere = os.path.realpath(os.path.join(self.temp.name, "other", ".git"))
        os.makedirs(os.path.join(ours, "engine", "accepted-hooks", "trees", tree))
        os.makedirs(os.path.join(elsewhere, "engine", "accepted-hooks", "trees", tree))
        listing = "\n".join([
            # a foreign process that merely mentions the server on its command line, with a secret
            f"111 {mine} python -c sleep mcp_server.py --api-key={_SECRET} --tree /x/TOKEN-{_SECRET}",
            # this engine's real attended server from an accepted tree under ANOTHER checkout's git directory
            f"222 {mine} python -I {home}/.git/x/accepted_hook_dispatch.py _run-accepted "
            f"--tree {elsewhere}/engine/accepted-hooks/trees/{tree} "
            f"--script .engine/tools/memory/mcp_server.py -- attended-memory-mcp",
            # the same launch shape under ANOTHER account
            f"333 {other} python {home}/.git/x/accepted_hook_dispatch.py _run-accepted --tree {home}/t/{tree}",
            # a degraded (live-checkout) launch of the dispatcher for the memory server, argv naming no root
            f"444 {mine} python {home}/tools/accepted_hook_dispatch.py attended --script .engine/tools/memory/mcp_server.py",
            # a --tree that is not a materialized accepted tree name (but IS a path outside this project)
            f"555 {mine} python {home}/tools/accepted_hook_dispatch.py --tree /x/not-a-tree -- attended-memory-mcp",
            # THIS project's attended server: its tree sits under this project's git directory
            f"666 {mine} python -I {ours}/engine/accepted-hooks/trees/{tree}/.engine/tools/accepted_hook_dispatch.py "
            f"_run-accepted --tree {ours}/engine/accepted-hooks/trees/{tree} -- attended-memory-mcp",
            # the `uv run` wrapper that spawns a server is not a second server
            f"777 {mine} uv run --directory .engine --frozen -- python tools/accepted_hook_dispatch.py attended "
            f"--script .engine/tools/memory/mcp_server.py --operation attended-memory-mcp --",
            # a live-checkout launch running a checkout's own interpreter: that checkout is asked for its git dir
            f"888 {mine} {home}/dev/proj/.engine/.venv/bin/python3 -I {home}/dev/proj/.engine/tools/accepted_hook_dispatch.py "
            f"attended --script .engine/tools/memory/mcp_server.py",
        ])
        done = mock.Mock(stdout=listing, returncode=0)
        asked = []

        def common_dir(root=None):
            asked.append(root)
            return ours if root is None or root == f"{home}/dev/proj" else None

        with mock.patch.object(_stranding_log.subprocess, "run", return_value=done), \
             mock.patch.object(_stranding_log, "_git_common_dir", side_effect=common_dir):
            found = _stranding_log._live_servers()
        self.assertEqual(found, [
            {"pid": 222, "launcher": "accepted-tree", "code_version": tree, "same_repository": False},
            {"pid": 444, "launcher": "live-checkout", "code_version": None, "same_repository": None},
            {"pid": 555, "launcher": "live-checkout", "code_version": None, "same_repository": False},
            {"pid": 666, "launcher": "accepted-tree", "code_version": tree, "same_repository": True},
            {"pid": 888, "launcher": "live-checkout", "code_version": None, "same_repository": True},
        ])
        self.assertEqual(asked, [None, f"{home}/dev/proj"])    # one question per distinct checkout root
        self.assertNotIn(_SECRET, json.dumps(found))
        self.assertNotIn(home, json.dumps(found))
        self.assertNotIn(self.temp.name, json.dumps(found))


class StrandingLogServerWiringTests(unittest.IsolatedAsyncioTestCase):
    """Where the server calls the log: its `call_tool` seam on an unexpected fault — a crash in the tool OR
    a fault converting its result for the wire — never on a refusal; and the read caveat on a typed
    staleness. The tool's own outcome never depends on the recording."""

    async def test_the_seam_records_a_crash_with_the_original_exception_and_reraises_unchanged(self):
        fresh, _ = _probe_server()
        with mock.patch.object(srv._stranding_log, "record_stranding", return_value=True) as recorded:
            with self.assertRaises(srv.UnexpectedToolError) as caught:
                await fresh.call_tool("probe", {"which": "crash"})
        recorded.assert_called_once()
        self.assertIs(recorded.call_args.args[0], _stranding_log.Event.TOOL_FAULT)
        original = recorded.call_args.args[1]
        self.assertIsInstance(original, RuntimeError)                 # the ORIGINAL, not the SDK wrapper
        self.assertIs(original, caught.exception.__cause__)
        self.assertEqual(recorded.call_args.kwargs, {"tool": "probe"})
        self.assertNotIn(_SECRET, str(caught.exception))             # the client-facing flattening holds

    async def test_a_fault_converting_the_result_for_the_wire_is_recorded_too(self):
        fresh, _ = _probe_server()
        with mock.patch.object(srv._stranding_log, "record_stranding", return_value=True) as recorded:
            with self.assertRaises(srv.UnexpectedToolError):
                await fresh.call_tool("probe", {"which": "unconvertible"})
        recorded.assert_called_once()
        self.assertNotIsInstance(recorded.call_args.args[1], RuntimeError)  # a conversion fault, seen anyway

    async def test_a_translated_refusal_is_not_a_stranding(self):
        fresh, _ = _probe_server()
        with mock.patch.object(srv._stranding_log, "record_stranding", return_value=True) as recorded:
            with self.assertRaises(ToolError) as caught:
                await fresh.call_tool("probe", {"which": "refusal"})
        self.assertNotIsInstance(caught.exception, srv.UnexpectedToolError)
        self.assertIn("REFUSAL: a designed sentence", str(caught.exception))
        recorded.assert_not_called()

    async def test_a_recording_fault_never_changes_the_tools_outcome(self):
        fresh, _ = _probe_server()
        with mock.patch.object(srv._stranding_log, "record_stranding", side_effect=RuntimeError("log exploded")):
            with self.assertRaises(srv.UnexpectedToolError) as caught:
                await fresh.call_tool("probe", {"which": "crash"})
        self.assertIsInstance(caught.exception.__cause__, RuntimeError)
        self.assertTrue(str(caught.exception.__cause__).startswith("CRASH: "))

    async def test_the_module_server_is_the_recording_kind(self):
        self.assertIsInstance(srv.server, srv._RecordingServer)

    def test_the_read_caveat_records_the_typed_staleness_and_not_a_refreshable_drift(self):
        context = object()
        srv._READ_DEGRADED_NOTED.clear()
        self.addCleanup(srv._READ_DEGRADED_NOTED.clear)
        with mock.patch.object(execution_context, "current_context", return_value=context), \
             mock.patch.object(execution_context, "revalidate_context",
                               side_effect=execution_context.ActivationStale("moved")), \
             mock.patch.object(srv._stranding_log, "record_stranding", return_value=True) as recorded:
            self.assertEqual(srv._memory_read_caveat(), srv._READ_CAVEAT)
            # A stale session reads many times; the trace is written ONCE per staleness type per process,
            # so the routine caveat can never rotate the rare crash record out of the bounded sink.
            self.assertEqual(srv._memory_read_caveat(), srv._READ_CAVEAT)
            self.assertEqual(srv._memory_read_caveat(), srv._READ_CAVEAT)
        recorded.assert_called_once()
        self.assertIs(recorded.call_args.args[0], _stranding_log.Event.READ_DEGRADED)
        self.assertIsInstance(recorded.call_args.args[1], execution_context.ActivationStale)
        # A miss is not counted: the next read tries once more.
        srv._READ_DEGRADED_NOTED.clear()
        with mock.patch.object(execution_context, "current_context", return_value=context), \
             mock.patch.object(execution_context, "revalidate_context",
                               side_effect=execution_context.ActivationStale("moved")), \
             mock.patch.object(srv._stranding_log, "record_stranding", return_value=False) as missed:
            self.assertEqual(srv._memory_read_caveat(), srv._READ_CAVEAT)
            self.assertEqual(srv._memory_read_caveat(), srv._READ_CAVEAT)
        self.assertEqual(missed.call_count, 2)
        with mock.patch.object(execution_context, "current_context", return_value=context), \
             mock.patch.object(execution_context, "revalidate_context",
                               side_effect=execution_context.ExpectedStateStale("healed")), \
             mock.patch.object(srv._stranding_log, "record_stranding", return_value=True) as recorded:
            self.assertIsNone(srv._memory_read_caveat())
        recorded.assert_not_called()


if __name__ == "__main__":
    unittest.main()
