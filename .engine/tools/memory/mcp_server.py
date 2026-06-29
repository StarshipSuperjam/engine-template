#!/usr/bin/env python3
"""Slice 5 — the engine-memory MCP server: the conforming fallback for memory recall (search.json).

A thin MCP transport over the ranked recall library (`memory.index.search`): the single declared operation
`search`, delegating to the library that ranks (lexical relevance, reinforced by usage) and filters (role/tag).
On every hit it fires the live reinforcement that records the access (`forget.record_access`), so recall is
self-reinforcing — the move slices 4c/4d reserved for "the search server" (records.py / forget.py). Registered
definition-only in the root .mcp.json AND the memory manifest's `wires` (handle 'engine-memory', the search.json
fallback); the operator's one-time approval of the tool is the operator's own (never engine-written), so until they
approve it the tool is simply switched off — recall never half-runs.

Built on the official MCP SDK (the `mcp` package) so protocol conformance — the handshake, framing, and future
protocol-version changes — is maintained upstream rather than hand-written; a richer semantic-recall implementation
overrides this lexical floor by presence at the same engine-prefixed server name (degrade-to-git-native: if this
server is unavailable the boot says so in plain language and the session is never blocked — boot's render, not this
module's).

Run (normally launched by the platform via .mcp.json over stdio):
  uv run --directory .engine --frozen -- python tools/memory/mcp_server.py
Operator demo (a throwaway practice cabinet; never the real store):
  uv run --directory .engine --frozen -- python tools/memory/mcp_server.py demo
"""
from __future__ import annotations
import os
import sys

# Make the package parent (.engine/tools) importable so `from memory import …` resolves both when launched as a
# script via .mcp.json (`python tools/memory/mcp_server.py`) and when imported as `memory.mcp_server` in a test.
_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from memory import forget, index, ledger, records  # noqa: E402

from mcp.server.fastmcp import FastMCP  # noqa: E402

SERVER_NAME = "engine-memory"

server = FastMCP(SERVER_NAME)


def _reinforce_on_recall(results) -> None:
    """Record one access per RETURNED (post-slice) record — the live reinforcement the ranking reads back as
    usage. Fires only for what the caller actually saw, never the wider candidate set. Fail-soft: a reinforcement
    fault NEVER converts a successful recall into an error (the contract is *recall always answers*);
    `forget.record_access` is already a clean no-op on lock contention (it reinforces again on the next hit) and
    on a blank id, and compaction (slice 4d-i) folds these markers into the carried frecency snapshot, so the
    marker population stays bounded."""
    for record in results:
        try:
            rid = record.get(records.RECORD_ID_KEY) if isinstance(record, dict) else None
            forget.record_access(rid)
        except Exception:  # noqa: BLE001 — best-effort bookkeeping; one fault never costs the response or the rest
            pass


def _recall(query: str, *, roles=None, tags=None, limit=None):
    """The recall + live-reinforcement the `search` tool performs, as a plain function shared by the tool and the
    operator demo so BOTH exercise the real path: rank/filter via the side-effect-free library, then record one
    access per returned record. Returns the library `QueryResult` (an unknown role raises ValueError from the
    library — the tool lets the SDK serialize that as a tool error)."""
    result = index.search(query, roles=roles, tags=tags, limit=limit)
    _reinforce_on_recall(result.records)
    return result


# Operator-facing recall-completeness note (§7 floor; D-273/D-274, issue #332). Recall surfaces only the curated
# layer — episodic summaries and gists; the raw, word-for-word turn-notes behind them are kept and fully
# recoverable, never deleted by this exclusion. Carried in the recall answer itself (alongside the results) so the
# assistant relays it to the operator (the operator-communication law) and can offer the verbatim. The wording is
# a build-spec leaf.
_RECALL_COMPLETENESS_NOTE = (
    "These are curated summaries. The original word-for-word notes behind them are still kept and recoverable — "
    "offer to pull the exact wording if the operator wants it."
)


@server.tool(
    name="search",
    description=(
        "Recall the memory records most relevant to a query, ranked best-first (lexical relevance, with how often "
        "a memory has been used breaking near-ties — a clearly stronger match is never shoved aside by a much-used "
        "weaker one). Optional `roles` narrows to record kinds (decision, rationale/pushback, lesson, dead-end, "
        "preference, intent, observation); optional `tags` narrows to records carrying any given tag (entity refs "
        "like 'eADR-0007' or free topic tags — compose the link to knowledge yourself by tag-filtering an entity "
        "id); optional `limit` caps results. Returns narrative recall only, never structural fact (knowledge's "
        "job). Each result carries the substrate's own fields (role, narrative, tags, provenance, score). Using a "
        "memory reinforces it, so what you rely on stays easy to recall."
    ),
)
def search(query: str, roles: list[str] | None = None,
           tags: list[str] | None = None, limit: int | None = None) -> dict:
    out = _recall(query, roles=roles, tags=tags, limit=limit).records
    result: dict = {"results": out}
    if out:
        result["recall_completeness"] = _RECALL_COMPLETENESS_NOTE
    return result


# --- Operator demonstration -------------------------------------------------------------------------------
# An operator-runnable walkthrough on a throwaway PRACTICE filing cabinet (a temp folder via ENGINE_MEMORY_DIR),
# never the real store. It exercises the REAL ranked search + the REAL live reinforcement above. Plain words
# only — "the filing cabinet" (the one real copy), "looking it up", "how often you've used it". Run it and vary
# the memories/question/usage near the top:
#     uv run --directory .engine --frozen -- python tools/memory/mcp_server.py demo

_ID = records.RECORD_ID_KEY


def _demo_body() -> bool:
    import time

    now = int(time.time())
    ok = True

    def add(text: str, *, role: str = "observation", tags=()) -> str:
        rid = records.new_record_id()
        ledger.append({_ID: rid, "ts": now, "role": role, "tags": list(tags), "text": text})
        return rid

    def rebuild() -> None:
        index.rebuild()

    # A handful of memories. "export" is RARE (only two mention it), so looking it up clearly separates the strong
    # match from the weak one; "almanac" is shared by two near-identical notes, so usage decides between them.
    strong = add("we decided the export format, the export schedule, and the export owner", role="decision", tags=["release"])
    weak = add("a passing note that export came up once in standup", role="observation")
    for t in ("keep the onboarding copy short and friendly", "the nightly job rebuilds the cache",
              "prefer dark mode across the whole interface", "the planning meeting moved to friday",
              "we settled on snake_case for the config names", "retries are capped at three attempts"):
        add(t)
    almanac_a = add("the field almanac lists the frost dates")
    almanac_b = add("the field almanac lists the frost dates")
    rebuild()

    print("=" * 80)
    print("PART 1 — the engine looks it up itself, and the most relevant memory comes back first")
    print("=" * 80)
    top = _recall("export").records
    ok1 = bool(top) and top[0].get(_ID) == strong
    print('  you asked: "export"')
    for r in top:
        print("    found:", r["text"])
    print("  =>", "the most relevant memory came back first." if ok1 else "!!! the wrong memory was first")
    # ...and a much-used weaker match must NOT shove the stronger one aside.
    for _ in range(30):
        forget.record_access(weak)
    rebuild()
    top2 = _recall("export").records
    ok1b = bool(top2) and top2[0].get(_ID) == strong
    print("  even after the weaker note was used 30 times, the best answer still leads:",
          "yes" if ok1b else "NO")
    print("  =>", "a much-used weaker memory did not push the best answer down." if ok1b else "!!! the weaker note jumped the queue")
    ok = ok and ok1 and ok1b

    print("\n" + "=" * 80)
    print("PART 2 — using a memory makes it easier to find again, and the others are still there")
    print("=" * 80)
    before = [r.get(_ID) for r in _recall("almanac").records]   # both come back; this reinforces both equally
    for _ in range(8):
        forget.record_access(almanac_b)                          # then use ONE of them repeatedly
    rebuild()
    after = _recall("almanac").records
    after_ids = [r.get(_ID) for r in after]
    climbed = bool(after_ids) and after_ids[0] == almanac_b
    both_present = {almanac_a, almanac_b} <= set(after_ids)
    print("  before, looking up \"almanac\" brings back:", len(before), "memories")
    print("  after using one of them repeatedly, looking it up again:")
    for r in after:
        print("    found:", r["text"], "  <- the one you kept using" if r.get(_ID) == almanac_b else "")
    print("  =>", "the one you used rose to the top — and the other is still right there, just lower."
          if (climbed and both_present) else "!!! the climb or the retention failed")
    ok = ok and climbed and both_present

    print("\n" + "=" * 80)
    print("PART 3 — you can narrow the search to one kind of memory, or one topic")
    print("=" * 80)
    all_export = _recall("export").records
    decisions = _recall("export", roles=["decision"]).records
    tagged = _recall("export", tags=["release"]).records
    ok3 = len(all_export) >= 2 and 1 <= len(decisions) < len(all_export) and 1 <= len(tagged) < len(all_export)
    print('  looking up "export":')
    print("    all memories that mention it:", len(all_export))
    print('    just the decisions:', len(decisions))
    print('    just the ones tagged "release":', len(tagged))
    print("  =>", "the filters narrowed the answer." if ok3 else "!!! a filter did not narrow the answer")
    ok = ok and ok3

    print("\n" + "=" * 80)
    print("PART 4 — the private \"when you used it\" notes never show up when you search")
    print("=" * 80)
    raw_total = sum(1 for _ in ledger.iter_records())
    leaked = any(r.get("kind") == records.REINFORCEMENT_KIND
                 for r in _recall("almanac").records + _recall("export").records)
    print("  the cabinet now holds", raw_total, "lines (real memories + the private usage notes from all that looking-up),")
    print("  yet a search still returns only real memories — none of the private notes.")
    print("  =>", "none of the private usage notes showed up as a search result." if not leaked else "!!! a private note leaked into search")
    ok = ok and not leaked

    print("\n" + "-" * 80)
    print("What you just saw ran on a PRACTICE filing cabinet we filled for this demo, then threw away.")
    print("On your REAL data: the engine can now look things up in its own memory ITSELF — but only after you")
    print("approve the new memory-search tool once (a one-time approval, like the knowledge tool; until then it")
    print("stays switched off). This is the engine PULLING an answer when it needs one — the separate step where")
    print("it AUTOMATICALLY surfaces a relevant memory on every prompt is a later step. Nothing here deletes")
    print("anything: using a memory only changes its ranking, never removes the others, and permanent erasure")
    print("stays a separate step you approve yourself.")
    print("\nVary it yourself: edit the memories / question / how-many-times-used near the top and run it again.")
    return ok


def _demo() -> int:
    import shutil
    import tempfile

    if not index.fts5_available():
        print("This computer's fast-search feature is unavailable, so this demo would only show the slow backup.")
        print("Recall still works on the slow backup; the ranking comparison is clearest with the fast lookup.")
    tmp = tempfile.mkdtemp(prefix="engine-memory-demo-")
    prev = os.environ.get("ENGINE_MEMORY_DIR")
    os.environ["ENGINE_MEMORY_DIR"] = tmp
    try:
        ok = _demo_body()
    finally:
        if prev is None:
            os.environ.pop("ENGINE_MEMORY_DIR", None)
        else:
            os.environ["ENGINE_MEMORY_DIR"] = prev
        shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok else 1


def main(argv) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    server.run()  # stdio transport by default
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
