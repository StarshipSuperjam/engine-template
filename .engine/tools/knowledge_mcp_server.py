#!/usr/bin/env python3
"""Slice 11a — the graph-query MCP server: core's conforming fallback for knowledge-retrieval.

This is the named fallback the knowledge-retrieval interface declares
(.engine/interfaces/knowledge-retrieval.json, handle 'engine-knowledge-graph'). It is a thin MCP
transport over the knowledge_query op-set: it exposes the four declared operations
(get-entity / find / neighbors / relate) as MCP tools, each delegating to knowledge_query, which reads
the committed knowledge graph through the gitignored SQLite index, rebuilds that index from the
committed graph when it is missing, and falls back to a live walk of the surfaces when the committed
graph is absent (degrade-to-git-native; knowledge/README.md:51).

Built on the official MCP SDK (the `mcp` package) so protocol conformance — the handshake, capability
negotiation, framing, and future protocol-version changes — is maintained upstream rather than
hand-written; a richer knowledge-retrieval implementation overrides this floor by presence at the same
engine-prefixed server name. The server is registered, definition-only, in the root .mcp.json; the
operator's one-time approval is the operator's own (never engine-written).

Run (normally launched by the platform via .mcp.json over stdio):
  uv run --directory .engine --frozen -- python tools/knowledge_mcp_server.py
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import knowledge_query as kq  # noqa: E402

from mcp.server.fastmcp import FastMCP  # noqa: E402

SERVER_NAME = "engine-knowledge-graph"

server = FastMCP(SERVER_NAME)


@server.tool(name="get-entity", description="Fetch one entity by id, with its declared outgoing "
             "edges; returns {entity} or {entity: null} if no such entity exists.")
def get_entity(id: str) -> dict:
    return {"entity": kq.get_entity(id)}


@server.tool(name="find", description="List the entities matching a selector (surface type, "
             "source-path glob, and/or owning module); an empty selector matches every entity.")
def find(type: str | None = None, path_glob: str | None = None, owner: str | None = None) -> dict:
    return {"entities": kq.find(type=type, path_glob=path_glob, owner=owner)}


@server.tool(name="neighbors", description="The entities adjacent to one entity by edge traversal — "
             "direction 'out' (declared edges), 'in' (reverse), or 'both'; optional edge_filter and "
             "depth (default 1) for transitive traversal.")
def neighbors(id: str, edge_filter: list[str] | None = None, direction: str = "out",
              depth: int = 1) -> dict:
    return {"neighbors": kq.neighbors(id, edge_filter=edge_filter, direction=direction, depth=depth)}


@server.tool(name="relate", description="The shortest edge path between two entities (edges followed "
             "in either direction) as an ordered id list, or null if they are not connected.")
def relate(id_a: str, id_b: str) -> dict:
    return {"path": kq.relate(id_a, id_b)}


def main() -> None:
    server.run()  # stdio transport by default


if __name__ == "__main__":
    main()
