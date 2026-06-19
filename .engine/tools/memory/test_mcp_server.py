"""test_mcp_server.py — the engine-memory MCP server, headless (memory-substrate-sqlite-fts5, slice 5).

Run via the engine's CI command:
    uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py'

Exercises the server in-process (no Claude Desktop, no subprocess): the single `search` tool delegates to the
ranked library and returns `{"results": [...]}`, and — the move slice 5 adds — fires the live reinforcement
(forget.record_access) once per RETURNED record, so recall is self-reinforcing. The reinforcement is fail-soft
(a fault never converts a successful recall into an error), lock-safe (it never writes lock-free), and skips a
record with no id. An unknown role surfaces as a tool error, not a crash. Isolation is a throwaway
ENGINE_MEMORY_DIR cabinet, so the server's default-path library calls resolve to the test's temp store.
"""

import contextlib
import io
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory import capture, forget, index, ledger, records  # noqa: E402
import memory.mcp_server as srv  # noqa: E402

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

    def tearDown(self):
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

    @staticmethod
    def _result_json(res):
        import json
        content = res[0] if isinstance(res, tuple) else res
        return json.loads(content[0].text)


class ToolWiringTests(_ServerBase):
    async def test_tools_list_is_exactly_search(self):
        names = {t.name for t in await srv.server.list_tools()}
        self.assertEqual(names, {"search"})

    async def test_search_returns_ranked_results_matching_the_library(self):
        self.add("export format export schedule decided", role="decision")
        self.add("a note that export came up once")
        for t in ("alpha", "beta", "gamma", "delta"):
            self.add(t)
        data = self._result_json(await srv.server.call_tool("search", {"query": "export"}))
        tool_ids = [r.get(_ID) for r in data["results"]]
        lib_ids = [r.get(_ID) for r in index.search("export").records]
        self.assertEqual(tool_ids, lib_ids)   # the server is a thin pass-through over the ranked library

    async def test_roles_tags_limit_pass_through(self):
        d = self.add("we decided to ship export", role="decision", tags=["release"])
        self.add("a lesson about export", role="lesson")
        data = self._result_json(
            await srv.server.call_tool("search", {"query": "export", "roles": ["decision"], "limit": 5}))
        self.assertEqual([r.get(_ID) for r in data["results"]], [d])
        tagged = self._result_json(
            await srv.server.call_tool("search", {"query": "export", "tags": ["release"]}))
        self.assertEqual([r.get(_ID) for r in tagged["results"]], [d])

    async def test_unknown_role_surfaces_as_a_tool_error(self):
        self.add("a decision about export", role="decision")
        with self.assertRaises(Exception) as cm:
            await srv.server.call_tool("search", {"query": "export", "roles": ["banana"]})
        self.assertIn("banana", str(cm.exception))

    async def test_search_still_answers_when_fts5_absent(self):
        # Availability law: with the fast lookup off, the server still returns recall (via the slow scan).
        self.add("export decision", role="decision")
        original = index.fts5_available
        index.fts5_available = lambda *a, **k: False
        try:
            data = self._result_json(await srv.server.call_tool("search", {"query": "export"}))
            self.assertTrue(len(data["results"]) >= 1)
        finally:
            index.fts5_available = original


class ReinforcementOnRecallTests(_ServerBase):
    async def test_reinforces_one_marker_per_returned_result(self):
        self.add("export one export two", role="decision")
        self.add("export three")
        before = _marker_count()
        data = self._result_json(await srv.server.call_tool("search", {"query": "export"}))
        after = _marker_count()
        self.assertEqual(after - before, len(data["results"]))   # one access marker per RETURNED record

    async def test_reinforces_only_the_post_slice_set(self):
        # Three matches but limit=1 -> exactly ONE marker (the returned record), not three (the candidates).
        for _ in range(3):
            self.add("export mention here")
        before = _marker_count()
        data = self._result_json(await srv.server.call_tool("search", {"query": "export", "limit": 1}))
        self.assertEqual(len(data["results"]), 1)
        self.assertEqual(_marker_count() - before, 1)

    async def test_reinforcement_is_fail_soft(self):
        # A reinforcement fault must never convert a successful recall into an error.
        self.add("export decision", role="decision")
        with mock.patch.object(forget, "record_access", side_effect=RuntimeError("boom")):
            data = self._result_json(await srv.server.call_tool("search", {"query": "export"}))
        self.assertTrue(len(data["results"]) >= 1)   # the response is the contract

    async def test_a_result_lacking_an_id_is_skipped(self):
        self.add("export without an id", with_id=False)
        before = _marker_count()
        data = self._result_json(await srv.server.call_tool("search", {"query": "export"}))
        self.assertTrue(len(data["results"]) >= 1)
        self.assertEqual(_marker_count() - before, 0)   # no id -> record_access no-op, no marker, no error

    async def test_reinforcement_is_lock_safe_no_lock_free_write(self):
        # While the single-writer lock is held, recall still answers but appends ZERO markers (never lock-free).
        self.add("export decision", role="decision")
        lock_path = os.path.join(ledger.ledger_dir(), capture.LOCK_FILENAME)
        held = capture._acquire_lock(lock_path)
        self.assertIsNotNone(held)
        try:
            before = _marker_count()
            data = self._result_json(await srv.server.call_tool("search", {"query": "export"}))
            self.assertTrue(len(data["results"]) >= 1)
            self.assertEqual(_marker_count() - before, 0)
        finally:
            capture._release_lock(held)


class DemoTests(unittest.TestCase):
    def test_demo_body_exits_zero(self):
        # The operator demo exercises the REAL rank + filter + reinforce on its own throwaway cabinet; a real
        # regression flips a `!!!` and returns non-zero. (It manages its own ENGINE_MEMORY_DIR.)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(srv._demo(), 0)


if __name__ == "__main__":
    unittest.main()
