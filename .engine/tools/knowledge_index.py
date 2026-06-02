#!/usr/bin/env python3
"""Slice 11a — the gitignored SQLite query index derived from the committed knowledge graph.

The committed `.engine/knowledge/graph.json` (slice 10) is the source of truth and the offline
cold-start readout; it stores OUTGOING edges only. This module derives a gitignored SQLite index
under `.engine/knowledge/.cache/` that the graph-query op-set (knowledge_query.py) reads — adding the
reverse-edge traversal and the indexed selectors the committed file does not give cheaply at scale.

The index is a CACHE, never committed: delete it and it rebuilds from the committed graph on the next
query (degrade-to-git-native). It is regenerated on demand — rebuilt when missing or when the
committed graph's fingerprint moves. SQLite (stdlib `sqlite3`) is the end-state floor: it scales to a
real adopter's repo and serves neighbors(depth) / relate(path) / find(selector) via indexed SQL and
recursive CTEs, with no third-party dependency.

Degrade chain (knowledge/README.md:51): a fresh index answers; a missing/stale index is rebuilt from
the committed graph; a missing committed graph raises KnowledgeUnavailable (the query layer reports it
in plain language, never a crash).
"""
from __future__ import annotations
import hashlib
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402
import knowledge_gen     # noqa: E402

# The gitignored derivative dir alongside the committed graph. `.cache` is pruned from the engine
# file inventory (module_coherence.PRUNE_DIRS) so the index is never a false ownership orphan, and
# is gitignored via core's wires:gitignore fence — it is regenerable, never committed.
CACHE_DIR = os.path.join(knowledge_gen.KNOWLEDGE_DIR, ".cache")
INDEX_PATH = os.path.join(CACHE_DIR, "index.sqlite")

EDGE_KINDS = ("provided_by", "governed_by", "targets", "depends_on")

SCHEMA_SQL = """
CREATE TABLE entities (
  id          TEXT PRIMARY KEY,
  type        TEXT NOT NULL,
  name        TEXT NOT NULL,
  slug        TEXT NOT NULL,
  source_path TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  owner       TEXT NOT NULL
);
CREATE TABLE edges (
  src_id    TEXT NOT NULL,
  predicate TEXT NOT NULL,
  dst_id    TEXT NOT NULL
);
CREATE INDEX edges_src ON edges(src_id);
CREATE INDEX edges_dst ON edges(dst_id);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class KnowledgeUnavailable(Exception):
    """The committed knowledge graph is absent, so the index cannot be built — knowledge is
    unavailable. Raised (never crashed-through) so the query layer surfaces it in plain language."""


def graph_fingerprint(graph_path: str | None = None) -> str | None:
    """A sha256 of the committed graph.json raw bytes (prefixed 'sha256:'), or None if it is absent.
    The index's staleness key: the index records which graph it was built from."""
    graph_path = knowledge_gen.GRAPH_PATH if graph_path is None else graph_path
    if not os.path.isfile(graph_path):
        return None
    with open(graph_path, "rb") as fh:
        return "sha256:" + hashlib.sha256(fh.read()).hexdigest()


def _load_graph(graph_path: str) -> dict:
    text = knowledge_gen.read_committed(graph_path)
    if text is None:
        raise KnowledgeUnavailable(
            f"the committed knowledge graph ({knowledge_gen._display(graph_path)}) is missing")
    return json.loads(text)


def build_index(index_path: str | None = None, graph_path: str | None = None) -> str:
    """(Re)build the SQLite index from the committed graph; return the index path. Builds into a
    temp file then atomically replaces, so a reader never sees a half-built index. Raises
    KnowledgeUnavailable if the committed graph is gone."""
    index_path = INDEX_PATH if index_path is None else index_path
    graph_path = knowledge_gen.GRAPH_PATH if graph_path is None else graph_path
    graph = _load_graph(graph_path)
    fp = graph_fingerprint(graph_path)
    os.makedirs(os.path.dirname(index_path) or ".", exist_ok=True)
    # A process-unique temp name so two concurrent rebuilds of the same index (e.g. parallel test
    # runners sharing one worktree) cannot collide on the in-progress file; the final os.replace is
    # atomic, so the last writer wins with valid content.
    tmp = f"{index_path}.building.{os.getpid()}"
    if os.path.exists(tmp):
        os.remove(tmp)
    conn = sqlite3.connect(tmp)
    try:
        conn.executescript(SCHEMA_SQL)
        for e in graph.get("entities", []):
            src = e.get("source") or {}
            conn.execute(
                "INSERT INTO entities(id,type,name,slug,source_path,fingerprint,owner) "
                "VALUES (?,?,?,?,?,?,?)",
                (e["id"], e["type"], e["name"], e["slug"],
                 src.get("path", ""), src.get("fingerprint", ""), e["owner"]))
            for pred, dsts in (e.get("predicates") or {}).items():
                for dst in dsts:
                    conn.execute("INSERT INTO edges(src_id,predicate,dst_id) VALUES (?,?,?)",
                                 (e["id"], pred, dst))
        conn.execute("INSERT INTO meta(key,value) VALUES ('graph_fingerprint', ?)", (fp,))
        conn.commit()
    finally:
        conn.close()
    os.replace(tmp, index_path)
    return index_path


def is_fresh(index_path: str | None = None, graph_path: str | None = None) -> bool:
    """True iff the index exists and was built from the current committed graph (fingerprints match)."""
    index_path = INDEX_PATH if index_path is None else index_path
    graph_path = knowledge_gen.GRAPH_PATH if graph_path is None else graph_path
    if not os.path.isfile(index_path):
        return False
    try:
        conn = sqlite3.connect(index_path)
        try:
            row = conn.execute("SELECT value FROM meta WHERE key='graph_fingerprint'").fetchone()
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return False                                   # a corrupt/partial index is not fresh
    return bool(row) and row[0] == graph_fingerprint(graph_path)


def ensure_index(index_path: str | None = None, graph_path: str | None = None):
    """Return (index_path, rebuilt) — a fresh index, rebuilding from the committed graph if missing
    or stale (the git-native degrade step). `rebuilt` is True when this call had to rebuild, so the
    query layer can surface that it answered from the committed graph. Raises KnowledgeUnavailable if
    the committed graph is gone."""
    index_path = INDEX_PATH if index_path is None else index_path
    graph_path = knowledge_gen.GRAPH_PATH if graph_path is None else graph_path
    if is_fresh(index_path, graph_path):
        return index_path, False
    return build_index(index_path, graph_path), True


def connect(index_path: str | None = None, graph_path: str | None = None):
    """A read connection to a fresh index (ensuring it first). Returns (sqlite3.Connection, rebuilt).
    The caller closes the connection."""
    path, rebuilt = ensure_index(index_path, graph_path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn, rebuilt


# ---- CLI ------------------------------------------------------------------------------------

def main(argv: list) -> int:
    cmd = argv[0] if argv else "status"
    try:
        if cmd == "build":
            path = build_index()
            print(f"Built the knowledge index ({knowledge_gen._display(path)}) from the committed graph.")
            return 0
        if cmd == "status":
            fresh = is_fresh()
            exists = os.path.isfile(INDEX_PATH)
            where = knowledge_gen._display(INDEX_PATH)
            if fresh:
                print(f"The knowledge index ({where}) is present and fresh.")
            elif exists:
                print(f"The knowledge index ({where}) is stale — it will rebuild from the committed "
                      f"graph on the next query.")
            else:
                print(f"The knowledge index ({where}) is absent — it will be built from the committed "
                      f"graph on the next query.")
            return 0
        print(f"usage: knowledge_index.py {{build|status}}\nunknown command {cmd!r}", file=sys.stderr)
        return 2
    except KnowledgeUnavailable as exc:
        print(f"Knowledge is unavailable: {exc}. Restore or regenerate "
              f"{knowledge_gen._display(knowledge_gen.GRAPH_PATH)} "
              f"(`{knowledge_gen.REGEN_CMD}`).", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
