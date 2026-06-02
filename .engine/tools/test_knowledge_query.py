#!/usr/bin/env python3
"""Self-tests for slice 11a — the knowledge-retrieval op-set (knowledge_query.py), the SQLite index
(knowledge_index.py), and the graph-query MCP server (knowledge_mcp_server.py).

Run: uv run --directory .engine -- python -m unittest discover -s tools -p 'test_*.py'

These lock the load-bearing teeth over a controlled FIXTURE graph (so assertions are exact and
independent of the evolving real graph): get-entity returns the entity + edges (or None); find selects
by type/glob/owner; neighbors traverses out / in (the REVERSE edges the committed graph cannot give) /
both, honours an edge filter and multi-hop depth; relate finds the shortest undirected path (or null).
Then degrade-to-git-native: a missing index is rebuilt from the committed graph; a stale index is
rebuilt; a missing committed graph raises KnowledgeUnavailable (never a crash). Finally the MCP server,
headless (no Claude Desktop): tools/list is exactly the four declared ops, and tools/call delegates to
the op-set.
"""
from __future__ import annotations
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import knowledge_index as ki      # noqa: E402
import knowledge_query as kq      # noqa: E402

D116_OPS = {"get-entity", "find", "neighbors", "relate"}


def _entity(eid, etype, owner, src, preds):
    return {"id": eid, "type": etype, "name": src, "slug": eid.split(":", 1)[1],
            "source": {"path": src, "fingerprint": "sha256:" + "0" * 64},
            "owner": owner, "predicates": preds}


def _fixture_graph() -> dict:
    """A small controlled graph: checks governed by schemas + targeting state, all provided by core,
    a 2-hop chain (check:c1 -> state:x -> schema:s2), and an isolated doc:orphan."""
    return {"schema_version": 1, "entities": [
        _entity("module:core", "module", "core", ".engine/modules/core/manifest.json", {}),
        _entity("schema:s1", "schema", "core", ".engine/schemas/s1.json",
                {"provided_by": ["module:core"]}),
        _entity("schema:s2", "schema", "core", ".engine/schemas/s2.json",
                {"provided_by": ["module:core"]}),
        _entity("state:x", "state", "core", ".engine/state/x.json",
                {"provided_by": ["module:core"], "governed_by": ["schema:s2"]}),
        _entity("check:c1", "check", "core", ".engine/check/c1.json",
                {"provided_by": ["module:core"], "governed_by": ["schema:s1"], "targets": ["state:x"]}),
        _entity("check:c2", "check", "core", ".engine/check/c2.json",
                {"provided_by": ["module:core"], "governed_by": ["schema:s1"]}),
        _entity("doc:orphan", "doc", "core", ".engine/docs/orphan.md", {}),
    ]}


class TestQueryOps(unittest.TestCase):
    """The pure op logic over a fixture index built into a temp dir."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.graph_path = os.path.join(self._tmp.name, "graph.json")
        self.index_path = os.path.join(self._tmp.name, "index.sqlite")
        with open(self.graph_path, "w", encoding="utf-8") as fh:
            json.dump(_fixture_graph(), fh)
        ki.build_index(self.index_path, self.graph_path)
        self.conn = __import__("sqlite3").connect(self.index_path)
        self.conn.row_factory = __import__("sqlite3").Row

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def _ids(self, rows):
        return sorted(r["id"] for r in rows)

    def test_get_entity_returns_entity_with_edges(self):
        e = kq._get_entity(self.conn, "check:c1")
        self.assertEqual(e["id"], "check:c1")
        self.assertEqual(e["predicates"]["governed_by"], ["schema:s1"])
        self.assertEqual(e["predicates"]["targets"], ["state:x"])
        self.assertEqual(e["predicates"]["provided_by"], ["module:core"])

    def test_get_entity_unknown_is_none(self):
        self.assertIsNone(kq._get_entity(self.conn, "check:does-not-exist"))

    def test_find_by_type(self):
        self.assertEqual(self._ids(kq._find(self.conn, type="check")), ["check:c1", "check:c2"])

    def test_find_by_glob(self):
        self.assertEqual(self._ids(kq._find(self.conn, path_glob=".engine/check/*")),
                         ["check:c1", "check:c2"])

    def test_find_empty_selector_matches_all(self):
        self.assertEqual(len(kq._find(self.conn)), 7)

    def test_neighbors_out(self):
        got = {n["id"] for n in kq._neighbors(self.conn, "check:c1", direction="out")}
        self.assertEqual(got, {"schema:s1", "state:x", "module:core"})

    def test_neighbors_in_is_reverse_traversal(self):
        # who is governed_by schema:s1 — the checks point AT it (the reverse edge the index exists for)
        got = {n["id"] for n in kq._neighbors(self.conn, "schema:s1", direction="in")}
        self.assertEqual(got, {"check:c1", "check:c2"})
        for n in kq._neighbors(self.conn, "schema:s1", direction="in"):
            self.assertEqual(n["direction"], "in")
            self.assertEqual(n["predicate"], "governed_by")

    def test_neighbors_edge_filter(self):
        got = {n["id"] for n in kq._neighbors(self.conn, "check:c1", edge_filter=["governed_by"],
                                              direction="out")}
        self.assertEqual(got, {"schema:s1"})

    def test_neighbors_depth_is_transitive(self):
        d1 = {n["id"] for n in kq._neighbors(self.conn, "check:c1", direction="out", depth=1)}
        d2 = {n["id"] for n in kq._neighbors(self.conn, "check:c1", direction="out", depth=2)}
        self.assertNotIn("schema:s2", d1)
        self.assertIn("schema:s2", d2)          # reached via check:c1 -> state:x -> schema:s2

    def test_neighbors_rejects_bad_args(self):
        with self.assertRaises(ValueError):
            kq._neighbors(self.conn, "check:c1", direction="sideways")
        with self.assertRaises(ValueError):
            kq._neighbors(self.conn, "check:c1", depth=0)
        with self.assertRaises(ValueError):
            kq._neighbors(self.conn, "check:c1", edge_filter=["not_a_real_edge"])

    def test_relate_direct(self):
        self.assertEqual(kq._relate(self.conn, "check:c1", "schema:s1"), ["check:c1", "schema:s1"])

    def test_relate_multi_hop(self):
        path = kq._relate(self.conn, "check:c1", "check:c2")
        self.assertIsNotNone(path)
        self.assertEqual(path[0], "check:c1")
        self.assertEqual(path[-1], "check:c2")
        self.assertEqual(len(path), 3)          # c1 - (schema:s1 | module:core) - c2

    def test_relate_unconnected_is_none(self):
        self.assertIsNone(kq._relate(self.conn, "check:c1", "doc:orphan"))

    def test_relate_same_node(self):
        self.assertEqual(kq._relate(self.conn, "check:c1", "check:c1"), ["check:c1"])


class TestDegradeToGitNative(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.graph_path = os.path.join(self._tmp.name, "graph.json")
        self.index_path = os.path.join(self._tmp.name, "index.sqlite")

    def tearDown(self):
        self._tmp.cleanup()

    def _write_graph(self, graph):
        with open(self.graph_path, "w", encoding="utf-8") as fh:
            json.dump(graph, fh)

    def test_missing_index_is_rebuilt_from_committed_graph(self):
        self._write_graph(_fixture_graph())
        self.assertFalse(os.path.exists(self.index_path))
        path, rebuilt = ki.ensure_index(self.index_path, self.graph_path)
        self.assertTrue(rebuilt)
        self.assertTrue(os.path.isfile(path))
        # the answer is correct off the rebuilt index, and a second ensure is a no-op
        e = kq.get_entity("check:c1", index_path=self.index_path, graph_path=self.graph_path)
        self.assertEqual(e["id"], "check:c1")
        _p, rebuilt2 = ki.ensure_index(self.index_path, self.graph_path)
        self.assertFalse(rebuilt2)

    def test_stale_index_is_rebuilt(self):
        self._write_graph(_fixture_graph())
        ki.build_index(self.index_path, self.graph_path)
        self.assertTrue(ki.is_fresh(self.index_path, self.graph_path))
        # change the committed graph (drop an entity) -> the index is now stale -> rebuilt
        smaller = _fixture_graph()
        smaller["entities"] = [e for e in smaller["entities"] if e["id"] != "doc:orphan"]
        self._write_graph(smaller)
        self.assertFalse(ki.is_fresh(self.index_path, self.graph_path))
        _p, rebuilt = ki.ensure_index(self.index_path, self.graph_path)
        self.assertTrue(rebuilt)

    def test_missing_committed_graph_raises_unavailable(self):
        # neither index nor committed graph present -> knowledge is unavailable, reported (not crashed)
        with self.assertRaises(ki.KnowledgeUnavailable):
            ki.ensure_index(self.index_path, self.graph_path)


class TestMcpServer(unittest.IsolatedAsyncioTestCase):
    """The graph-query MCP server, headless (in-process) — no Claude Desktop, no subprocess. The
    server's tools delegate to the op-set over the LIVE committed graph."""

    @staticmethod
    def _tool_result_json(res):
        content = res[0] if isinstance(res, tuple) else res
        return json.loads(content[0].text)

    async def test_tools_list_is_exactly_the_op_set(self):
        import knowledge_mcp_server as srv
        names = {t.name for t in await srv.server.list_tools()}
        self.assertEqual(names, D116_OPS)

    async def test_call_tool_get_entity_delegates(self):
        import knowledge_mcp_server as srv
        data = self._tool_result_json(await srv.server.call_tool("get-entity", {"id": "module:core"}))
        self.assertEqual(data["entity"]["id"], "module:core")

    async def test_call_tool_neighbors_matches_the_library(self):
        import knowledge_mcp_server as srv
        data = self._tool_result_json(
            await srv.server.call_tool("neighbors", {"id": "schema:check.v1", "direction": "in"}))
        expected = kq.neighbors("schema:check.v1", direction="in")
        self.assertEqual({n["id"] for n in data["neighbors"]}, {n["id"] for n in expected})
        self.assertTrue(len(data["neighbors"]) >= 1)


if __name__ == "__main__":
    unittest.main()
