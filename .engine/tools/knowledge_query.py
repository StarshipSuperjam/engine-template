#!/usr/bin/env python3
"""Slice 11a — the knowledge-retrieval op-set, the conforming fallback floor.

Realizes the knowledge-retrieval interface (.engine/interfaces/knowledge-retrieval.json) over the
gitignored SQLite index (knowledge_index.py): the four operations every conforming implementation
must answer —

  get-entity(id)                          one entity + its declared edges, or null
  find(type?, path_glob?, owner?)         the entities matching a selector
  neighbors(id, edge_filter?, direction?, depth?)   adjacency (out / in / both; multi-hop), the
                                          REVERSE traversal the committed graph cannot give cheaply
  relate(id_a, id_b)                      the shortest edge path between two entities, or null

Traversal is pushed into the indexed store (recursive CTEs over the dst-indexed edge table), so it
scales to a real adopter's repo. Degrade-to-git-native: every op ensures the index first, rebuilding
it from the committed graph if it is missing or stale; if the committed graph itself is gone the op
reports knowledge unavailable in plain language, never a crash. The graph-query MCP server
(knowledge_mcp_server.py) is a thin transport over this library; the CLI here is the operator's
no-Claude-Desktop demo path.
"""
from __future__ import annotations
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate            # noqa: E402
import knowledge_gen       # noqa: E402
import knowledge_index     # noqa: E402
from knowledge_index import KnowledgeUnavailable  # noqa: E402

EDGE_KINDS = knowledge_index.EDGE_KINDS


# ---- pure query logic over an open connection (fixture-testable) ----------------------------

def _entity_row_to_dict(conn, row) -> dict:
    """Rebuild a knowledge.v1-shaped entity dict (with its outgoing predicates) from an entities row."""
    preds: dict = {}
    for er in conn.execute(
            "SELECT predicate, dst_id FROM edges WHERE src_id=? ORDER BY predicate, dst_id", (row["id"],)):
        preds.setdefault(er["predicate"], []).append(er["dst_id"])
    return {
        "id": row["id"], "type": row["type"], "name": row["name"], "slug": row["slug"],
        "source": {"path": row["source_path"], "fingerprint": row["fingerprint"]},
        "owner": row["owner"], "predicates": preds,
    }


def _get_entity(conn, entity_id: str):
    row = conn.execute(
        "SELECT id,type,name,slug,source_path,fingerprint,owner FROM entities WHERE id=?",
        (entity_id,)).fetchone()
    return _entity_row_to_dict(conn, row) if row else None


def _find(conn, type=None, path_glob=None, owner=None) -> list:
    where, params = [], []
    if type is not None:
        where.append("type=?"); params.append(type)
    if owner is not None:
        where.append("owner=?"); params.append(owner)
    if path_glob is not None:
        where.append("source_path GLOB ?"); params.append(path_glob)
    sql = "SELECT id,type,name,slug,source_path,owner FROM entities"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id"
    return [{"id": r["id"], "type": r["type"], "name": r["name"], "slug": r["slug"],
             "source_path": r["source_path"], "owner": r["owner"]}
            for r in conn.execute(sql, params)]


def _neighbors(conn, entity_id: str, edge_filter=None, direction="out", depth=1) -> list:
    if direction not in ("out", "in", "both"):
        raise ValueError(f"direction must be out/in/both, got {direction!r}")
    if not isinstance(depth, int) or depth < 1:
        raise ValueError(f"depth must be an integer >= 1, got {depth!r}")
    preds = tuple(edge_filter) if edge_filter else EDGE_KINDS
    bad = [p for p in preds if p not in EDGE_KINDS]
    if bad:
        raise ValueError(f"unknown edge kind(s) {bad}; valid: {list(EDGE_KINDS)}")
    ph = ",".join("?" for _ in preds)
    want_out = 1 if direction in ("out", "both") else 0
    want_in = 1 if direction in ("in", "both") else 0
    # dedges: each edge projected as a directed step honouring the direction + edge filter; walk: a
    # single recursive term following dedges from the root, bounded by depth, UNION-dedup as the cycle guard.
    sql = f"""
    WITH RECURSIVE
      dedges(a, b, pred, dir) AS (
        SELECT src_id, dst_id, predicate, 'out' FROM edges WHERE ?=1 AND predicate IN ({ph})
        UNION ALL
        SELECT dst_id, src_id, predicate, 'in'  FROM edges WHERE ?=1 AND predicate IN ({ph})
      ),
      walk(nid, pred, dir, d) AS (
        SELECT b, pred, dir, 1 FROM dedges WHERE a=?
        UNION
        SELECT de.b, de.pred, de.dir, w.d+1 FROM walk w JOIN dedges de ON de.a=w.nid WHERE w.d < ?
      )
    SELECT DISTINCT nid, pred, dir FROM walk WHERE nid<>? ORDER BY nid, pred, dir
    """
    params = [want_out, *preds, want_in, *preds, entity_id, depth, entity_id]
    return [{"id": r["nid"], "predicate": r["pred"], "direction": r["dir"]}
            for r in conn.execute(sql, params)]


def _relate(conn, id_a: str, id_b: str):
    """The shortest undirected edge path id_a..id_b as a list of ids (inclusive), or None. BFS via a
    recursive CTE over an undirected edge view; the path string is '>'-delimited and the cycle guard
    rejects revisiting a node (ids contain no '>')."""
    if id_a == id_b:
        return [id_a] if _get_entity(conn, id_a) else None
    max_depth = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    sql = """
    WITH RECURSIVE
      uedges(a, b) AS (
        SELECT src_id, dst_id FROM edges
        UNION
        SELECT dst_id, src_id FROM edges
      ),
      walk(node, path, d) AS (
        SELECT ?, ?, 0
        UNION
        SELECT u.b, w.path || '>' || u.b, w.d+1
          FROM walk w JOIN uedges u ON u.a = w.node
         WHERE w.d < ? AND instr(w.path || '>', u.b || '>') = 0
      )
    SELECT path FROM walk WHERE node=? ORDER BY d LIMIT 1
    """
    row = conn.execute(sql, (id_a, id_a, max_depth, id_b)).fetchone()
    return row["path"].split(">") if row else None


# ---- public ops: open a fresh index, query, close (returns data only) -----------------------

def _with_conn(fn, index_path, graph_path):
    conn, _rebuilt = knowledge_index.connect(index_path, graph_path)
    try:
        return fn(conn)
    finally:
        conn.close()


def get_entity(entity_id, *, index_path=None, graph_path=None):
    return _with_conn(lambda c: _get_entity(c, entity_id), index_path, graph_path)


def find(type=None, path_glob=None, owner=None, *, index_path=None, graph_path=None):
    return _with_conn(lambda c: _find(c, type, path_glob, owner), index_path, graph_path)


def neighbors(entity_id, edge_filter=None, direction="out", depth=1, *, index_path=None, graph_path=None):
    return _with_conn(lambda c: _neighbors(c, entity_id, edge_filter, direction, depth),
                      index_path, graph_path)


def relate(id_a, id_b, *, index_path=None, graph_path=None):
    return _with_conn(lambda c: _relate(c, id_a, id_b), index_path, graph_path)


# ---- CLI (the operator's no-Claude-Desktop demo path) ---------------------------------------

def _note_degrade(rebuilt: bool) -> None:
    if rebuilt:
        print(f"(the knowledge query index was absent or stale, so this answer was rebuilt from the "
              f"committed graph — {knowledge_gen._display(knowledge_gen.GRAPH_PATH)}, the git-native "
              f"source of truth)", file=sys.stderr)


def main(argv: list) -> int:
    if not argv:
        print("usage: knowledge_query.py {get-entity <id> | find [--type T] [--glob G] [--owner M] "
              "| neighbors <id> [--in|--out|--both] [--depth N] [--edge K] | relate <id-a> <id-b>}",
              file=sys.stderr)
        return 2
    cmd, rest = argv[0], argv[1:]
    try:
        conn, rebuilt = knowledge_index.connect()
    except KnowledgeUnavailable as exc:
        print(f"Knowledge is unavailable: {exc}. Restore or regenerate "
              f"{knowledge_gen._display(knowledge_gen.GRAPH_PATH)} "
              f"(`{knowledge_gen.REGEN_CMD}`), then try again.", file=sys.stderr)
        return 1
    try:
        _note_degrade(rebuilt)
        if cmd == "get-entity" and len(rest) == 1:
            result = _get_entity(conn, rest[0])
        elif cmd == "find":
            opts = _parse_opts(rest, {"--type": "type", "--glob": "path_glob", "--owner": "owner"})
            result = _find(conn, **opts)
        elif cmd == "neighbors" and rest:
            entity_id = rest[0]
            direction = "out"
            if "--in" in rest: direction = "in"
            if "--both" in rest: direction = "both"
            depth = int(_flag_value(rest, "--depth", "1"))
            edges = [v for v in _all_flag_values(rest, "--edge")] or None
            result = _neighbors(conn, entity_id, edge_filter=edges, direction=direction, depth=depth)
        elif cmd == "relate" and len(rest) == 2:
            result = _relate(conn, rest[0], rest[1])
        else:
            print(f"bad arguments for {cmd!r}", file=sys.stderr)
            return 2
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (ValueError, KeyError) as exc:
        print(f"bad query: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()


def _flag_value(argv, flag, default):
    return argv[argv.index(flag) + 1] if flag in argv and argv.index(flag) + 1 < len(argv) else default


def _all_flag_values(argv, flag):
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag and i + 1 < len(argv)]


def _parse_opts(argv, mapping):
    out = {}
    for flag, key in mapping.items():
        if flag in argv:
            out[key] = _flag_value(argv, flag, None)
    return out


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
