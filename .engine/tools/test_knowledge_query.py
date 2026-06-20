#!/usr/bin/env python3
"""Self-tests for slice 11a — the knowledge-retrieval op-set (knowledge_query.py), the SQLite index
(knowledge_index.py), and the graph-query MCP server (knowledge_mcp_server.py).

Run: uv run --directory .engine -- python -m unittest discover -s tools -p 'test_*.py'

These lock the load-bearing teeth over a controlled FIXTURE graph (so assertions are exact and
independent of the evolving real graph): get-entity returns the entity + edges (or None); find selects
by type/glob/owner; neighbors traverses out / in (the REVERSE edges the committed graph cannot give) /
both, honours an edge filter and multi-hop depth; relate finds the shortest undirected path (or null).
Then the four-rung degrade cascade (knowledge/README.md:51): a missing index rebuilds from the committed
graph; a stale index rebuilds; an ABSENT committed graph rebuilds from a live walk of the surfaces and
still answers; only if that live walk also fails is KnowledgeUnavailable raised (never a crash). Finally
the MCP server, headless (no Claude Desktop): tools/list is exactly the four declared ops, and tools/call
delegates to the op-set.
"""
from __future__ import annotations
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import knowledge_index as ki      # noqa: E402
import knowledge_query as kq      # noqa: E402
import knowledge_gen as kg        # noqa: E402

D116_OPS = {"get-entity", "find", "neighbors", "relate"}


def _entity(eid, etype, owner, src, preds):
    return {"id": eid, "type": etype, "name": src, "slug": eid.split(":", 1)[1],
            "source": {"path": src, "fingerprint": "sha256:" + "0" * 64},
            "owner": owner, "predicates": preds}


def _fixture_graph() -> dict:
    """A small controlled graph: checks governed by schemas + targeting an interface, all provided by core,
    a 2-hop chain (check:c1 -> interface:x -> schema:s2), and an isolated doc:orphan."""
    return {"schema_version": 1, "entities": [
        _entity("module:core", "module", "core", ".engine/modules/core/manifest.json", {}),
        _entity("schema:s1", "schema", "core", ".engine/schemas/s1.json",
                {"provided_by": ["module:core"]}),
        _entity("schema:s2", "schema", "core", ".engine/schemas/s2.json",
                {"provided_by": ["module:core"]}),
        _entity("interface:x", "interface", "core", ".engine/interfaces/x.json",
                {"provided_by": ["module:core"], "governed_by": ["schema:s2"]}),
        _entity("check:c1", "check", "core", ".engine/check/c1.json",
                {"provided_by": ["module:core"], "governed_by": ["schema:s1"], "targets": ["interface:x"]}),
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
        self.assertEqual(e["predicates"]["targets"], ["interface:x"])
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
        self.assertEqual(got, {"schema:s1", "interface:x", "module:core"})

    def test_neighbors_in_is_reverse_traversal(self):
        # who is governed_by schema:s1 — the checks point AT it (the reverse edge the index exists for)
        got = {n["id"] for n in kq._neighbors(self.conn, "schema:s1", direction="in")}
        self.assertEqual(got, {"check:c1", "check:c2"})
        for n in kq._neighbors(self.conn, "schema:s1", direction="in"):
            self.assertEqual(n["direction"], "in")
            self.assertEqual(n["predicate"], "governed_by")

    def test_neighbors_both_unions_forward_and_reverse(self):
        # The cold-start orientation walk (D-224): `direction="both"` is the union of out and in, deduped.
        # schema:s1 is forward-poor (out -> only its module) but reverse-rich (the checks it governs point AT
        # it) — exactly the connective tissue a forward-only walk starves. both() must surface both halves.
        out = {n["id"] for n in kq._neighbors(self.conn, "schema:s1", direction="out")}
        inn = {n["id"] for n in kq._neighbors(self.conn, "schema:s1", direction="in")}
        both = {n["id"] for n in kq._neighbors(self.conn, "schema:s1", direction="both")}
        self.assertEqual(out, {"module:core"})                       # forward-only collapses to the module
        self.assertEqual(both, out | inn)                            # both is the deduped union
        self.assertEqual(both, {"module:core", "check:c1", "check:c2"})

    def test_neighbors_edge_filter(self):
        got = {n["id"] for n in kq._neighbors(self.conn, "check:c1", edge_filter=["governed_by"],
                                              direction="out")}
        self.assertEqual(got, {"schema:s1"})

    def test_neighbors_depth_is_transitive(self):
        d1 = {n["id"] for n in kq._neighbors(self.conn, "check:c1", direction="out", depth=1)}
        d2 = {n["id"] for n in kq._neighbors(self.conn, "check:c1", direction="out", depth=2)}
        self.assertNotIn("schema:s2", d1)
        self.assertIn("schema:s2", d2)          # reached via check:c1 -> interface:x -> schema:s2

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
    """The four-rung degrade cascade (knowledge/README.md:51): a fresh index answers; a missing/stale
    index rebuilds from the committed graph (rung 2); an ABSENT committed graph rebuilds from a LIVE WALK
    of the surfaces (rung 3); only if that live walk also fails is knowledge unavailable (rung 4)."""

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
        path, source = ki.ensure_index(self.index_path, self.graph_path)
        self.assertEqual(source, "committed")           # rung 2
        self.assertTrue(os.path.isfile(path))
        # the answer is correct off the rebuilt index, and a second ensure is a no-op
        e = kq.get_entity("check:c1", index_path=self.index_path, graph_path=self.graph_path)
        self.assertEqual(e["id"], "check:c1")
        _p, source2 = ki.ensure_index(self.index_path, self.graph_path)
        self.assertIsNone(source2)                       # already fresh -> no rebuild

    def test_stale_index_is_rebuilt(self):
        self._write_graph(_fixture_graph())
        ki.build_index(self.index_path, self.graph_path)
        self.assertTrue(ki.is_fresh(self.index_path, self.graph_path))
        # change the committed graph (drop an entity) -> the index is now stale -> rebuilt
        smaller = _fixture_graph()
        smaller["entities"] = [e for e in smaller["entities"] if e["id"] != "doc:orphan"]
        self._write_graph(smaller)
        self.assertFalse(ki.is_fresh(self.index_path, self.graph_path))
        _p, source = ki.ensure_index(self.index_path, self.graph_path)
        self.assertEqual(source, "committed")            # rebuilt from the (changed) committed graph

    def test_missing_committed_graph_falls_back_to_live_walk(self):
        # rung 3: no committed graph at the temp path, but the real surfaces ARE present -> the index is
        # rebuilt from a LIVE WALK (knowledge_gen.canonical_graph()) and still answers (loudly degraded).
        self.assertFalse(os.path.exists(self.graph_path))
        path, source = ki.ensure_index(self.index_path, self.graph_path)
        self.assertEqual(source, "live")
        self.assertTrue(os.path.isfile(path))
        # module:core is always derived from the core manifest, so a real live walk must surface it
        e = kq.get_entity("module:core", index_path=self.index_path, graph_path=self.graph_path)
        self.assertIsNotNone(e)
        self.assertEqual(e["id"], "module:core")
        # while the committed graph stays absent, every ensure re-walks (never wrongly deemed fresh)
        _p, source2 = ki.ensure_index(self.index_path, self.graph_path)
        self.assertEqual(source2, "live")

    def test_live_walk_failure_reports_unavailable(self):
        # rung 4: committed graph absent AND the live walk also fails -> KnowledgeUnavailable (reported,
        # not crashed). Fake only the boundary (the live walk); the real cascade logic runs.
        def _boom():
            raise RuntimeError("simulated live-walk failure")
        with mock.patch.object(kg, "canonical_graph", _boom):
            with self.assertRaises(ki.KnowledgeUnavailable) as cm:
                ki.ensure_index(self.index_path, self.graph_path)
        # Pin that it failed AT the live-walk rung (3->4), not earlier: the message names the live walk
        # and the chained cause is the simulated failure. This makes the test revert-proof on its own —
        # the old 3-rung code raised on absence with neither signal, so these asserts would fail on it.
        self.assertIn("live walk", str(cm.exception))
        self.assertIsInstance(cm.exception.__cause__, RuntimeError)
        self.assertIn("simulated live-walk failure", str(cm.exception.__cause__))


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


class TestEnrichedEntities(unittest.TestCase):
    """D-203 pull-path enrichment: the declared attributes ride through get-entity/find via the JSON
    attributes column; supersedes is a deliberate PULL (neighbors edge_filter) but stays OFF the
    cold-start default walk; build_index allowlists edge kinds."""

    def _build(self, graph):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        gpath = os.path.join(tmp.name, "graph.json")
        ipath = os.path.join(tmp.name, "index.sqlite")
        with open(gpath, "w", encoding="utf-8") as fh:
            json.dump(graph, fh)
        ki.build_index(ipath, gpath)
        conn = __import__("sqlite3").connect(ipath)
        conn.row_factory = __import__("sqlite3").Row
        self.addCleanup(conn.close)
        return conn

    def _enriched_graph(self):
        c = _entity("check:c1", "check", "core", ".engine/check/c1.json", {"provided_by": ["module:core"]})
        c.update({"status": "active", "tier": "hard", "kind": "shape", "suites": ["CI"]})
        p = _entity("policy:p1", "policy", "core", ".engine/policies/p1.md", {"provided_by": ["module:core"]})
        p["title"] = "Attention"
        a = _entity("contract:eADR-0002", "contract", "core", "x/a.md", {"supersedes": ["contract:eADR-0001"]})
        b = _entity("contract:eADR-0001", "contract", "core", "x/b.md", {})
        m = _entity("module:core", "module", "core", ".engine/modules/core/manifest.json", {})
        return {"schema_version": 1, "entities": [c, p, a, b, m]}

    def test_get_entity_carries_declared_attributes_and_keeps_edges(self):
        conn = self._build(self._enriched_graph())
        e = kq._get_entity(conn, "check:c1")
        self.assertEqual((e.get("status"), e.get("tier"), e.get("kind"), e.get("suites")),
                         ("active", "hard", "shape", ["CI"]))
        self.assertEqual(e["predicates"]["provided_by"], ["module:core"])      # edges still present
        self.assertEqual(kq._get_entity(conn, "policy:p1").get("title"), "Attention")

    def test_find_carries_attributes_but_selects_core_scalar_only(self):
        conn = self._build(self._enriched_graph())
        rows = {r["id"]: r for r in kq._find(conn, type="check")}
        self.assertEqual(rows["check:c1"].get("tier"), "hard")
        # NO attribute selector -> no find(attribute) canon back-door (D-203)
        import inspect
        self.assertEqual(set(inspect.signature(kq._find).parameters) - {"conn"},
                         {"type", "path_glob", "owner"})

    def test_supersedes_is_pull_queryable_but_off_the_cold_start_default(self):
        conn = self._build(self._enriched_graph())
        pulled = {n["id"] for n in kq._neighbors(conn, "contract:eADR-0002", edge_filter=["supersedes"])}
        self.assertEqual(pulled, {"contract:eADR-0001"})            # deliberate pull
        default = {n["id"] for n in kq._neighbors(conn, "contract:eADR-0002")}
        self.assertEqual(default, set())                            # cold-start default never follows it

    def test_edge_sets_are_split(self):
        self.assertNotIn("supersedes", kq.WALK_EDGE_KINDS)
        self.assertIn("supersedes", kq.EDGE_KINDS)

    def test_build_index_allowlists_edge_kinds(self):
        c = _entity("check:c1", "check", "core", ".engine/check/c1.json",
                    {"provided_by": ["module:core"], "bogus_edge": ["module:core"]})
        conn = self._build({"schema_version": 1, "entities": [
            c, _entity("module:core", "module", "core", "m.json", {})]})
        preds = {row[0] for row in conn.execute("SELECT DISTINCT predicate FROM edges")}
        self.assertNotIn("bogus_edge", preds)
        self.assertIn("provided_by", preds)

    def test_old_shape_index_is_rebuilt(self):
        # an index built before the attributes column / version sentinel must be deemed stale and rebuilt
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        gpath = os.path.join(tmp.name, "graph.json")
        ipath = os.path.join(tmp.name, "index.sqlite")
        with open(gpath, "w", encoding="utf-8") as fh:
            json.dump(self._enriched_graph(), fh)
        ki.build_index(ipath, gpath)
        # simulate an OLD index: wipe the version sentinel
        conn = __import__("sqlite3").connect(ipath)
        conn.execute("DELETE FROM meta WHERE key='index_schema_version'"); conn.commit(); conn.close()
        self.assertFalse(ki.is_fresh(ipath, gpath))                 # version leg forces a rebuild
        ki.ensure_index(ipath, gpath)
        self.assertTrue(ki.is_fresh(ipath, gpath))


if __name__ == "__main__":
    unittest.main()
