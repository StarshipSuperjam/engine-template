"""index.py — the engine's derived memory lookup: the throwaway SQLite/FTS5 accelerator + the plain-scan floor.

(memory-substrate-sqlite-fts5, build slice 2.)

The ledger (slice 1, `ledger.py`) is the ONE source of truth. This module builds a FAST lookup over it — a
SQLite FTS5 full-text index — and, beneath that, a SLOW backup lookup: a plain scan straight through the
ledger, for when a machine's SQLite was built without the FTS5 module. The promise is the locked law: recall
always answers — *availability holds, latency does not*. When FTS5 is absent the answer still comes back,
just slower.

This index is DERIVED and THROWAWAY. It is rebuilt from the ledger and is never the only copy; deleting it
loses nothing (`rebuild()` reconstructs it), and backup is still "copy the ledger", never this file.

Leaf discipline: this module DETECTS the FTS5-absent / slow-path condition and RETURNS it to the caller; it
never renders operator-facing prose (boot does that, principle §16). It writes no telemetry and logs no findings.

Slice-2 scope: the index machinery + the two retrieval paths only, record-shape-agnostic and UNRANKED. The
public `search` interface contract, BM25 ranking, role/tag filters, the engine-memory MCP server, and the
boot/attention scent are slice 5; the closed record shape + role vocabulary are slice 3.

Both retrieval paths split text into words with ONE tokenizer (`_tokenize`, modeled on SQLite's FTS5
`unicode61`): the fast lookup stores the tokens it produces, and the slow scan matches the same way. That one
shared word-splitter — not FTS5's own, which folds some scripts differently — is what makes the slow backup
return the same set of records the fast lookup does, not a degraded different answer.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unicodedata
from dataclasses import dataclass, field

# Make the package parent (.engine/tools) importable so `from memory import ledger` resolves even when this
# file is run directly as a script (the demo). When imported as `memory.index`, the parent is already on
# sys.path, so this is a guarded no-op. (Not FS/DB work — close.py never imports this module, only the
# side-effect-free package `__init__`, so the leaf-safety invariant is untouched.)
_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from memory import forget, ledger, records  # noqa: E402

INDEX_FILENAME = "index.sqlite3"
_FTS_PROBE_TABLE = "engine_fts5_probe"
# Top-level record fields kept OUT of the searchable text. `tags` honors the locked typing law (tags are a
# secondary filter, never in the FTS body, so tag drift never poisons term statistics). The capture-record
# ENVELOPE metadata is excluded for the same reason: `session_id` (a per-session UUID — its hex fragments are
# real words: dead/beef/cafe/face…), `kind` ("turn-delta"), and `speaker` ("user"/"assistant") are provenance,
# not content, and indexing them makes `query("user")`/`query("delta")` match every record. Only the human
# `text` (and any other non-metadata string leaf) is searchable. The closed role vocabulary the reflection
# slice (3b) adds is a structured filter, so `role` joins this set: searching a label like "decision" must
# never drag in every record that carries it (the same pollution the capture-record provenance fields would
# cause). Episodic provenance (`consolidated_ts`, `source_seqs`) is non-string and stays out by type. The
# per-pass `batch` id the forgetting slice (4a) adds is a uuid — its hex fragments are real words, exactly the
# `session_id` problem — so it joins this set too. The per-record `id` (slice 4b) is also a uuid hex (its only
# purpose is to NAME a record, never to be searched), so it joins for the same reason.
_TAGS_KEY = "tags"
_NON_BODY_KEYS = frozenset(
    {"tags", "session_id", "kind", "speaker", "role", records.BATCH_KEY, records.RECORD_ID_KEY}
)


@dataclass
class RebuildReport:
    """The outcome of rebuilding the fast lookup. `fts5` is False when this machine has no FTS5, in which case
    no index is built (recall uses the slow scan). `with_text` counts how many indexed records had any
    searchable text — a record with no string content is indexed but unsearchable, surfaced so the fast and
    slow paths cannot silently diverge. Leaf law: returned, never logged."""

    indexed: int = 0
    with_text: int = 0
    fts5: bool = True
    path: str = ""


@dataclass
class QueryResult:
    """The records matching a query, in ledger order (UNRANKED — ranking is slice 5). `degraded` is True when
    the answer came from the slow backup scan (FTS5 absent, the fast lookup not yet built, or scan forced)."""

    records: list = field(default_factory=list)
    degraded: bool = False


def _tokenize(text: str) -> list:
    """Tokenize text the way SQLite's FTS5 `unicode61` tokenizer does, so the slow backup lookup finds the SAME
    records the fast lookup does.

    This is the SINGLE folding authority for both lookup paths: the fast lookup stores the tokens this produces
    (and FTS5 is configured to add no diacritic folding of its own — see `_build_schema`), and the slow scan
    tokenizes the same way. So the two paths agree across scripts whose folding FTS5 handles differently from
    Python (Cyrillic, Greek, accented Latin) — not just Latin text.

    The rule, matching FTS5 `unicode61`: split on every codepoint that is not a letter or number (so
    `snake_case_config` is three tokens), fold case with `str.lower()` (NOT `casefold()` — casefold turns ß
    into ss, which unicode61 does not), and strip diacritics via NFD (canonical, NOT NFKD compatibility, so
    `Ⅳ` stays `ⅳ` rather than becoming `iv`) + dropping combining marks (so `café` matches `cafe`).

    Residual: FTS5 still applies its OWN case-fold to the stored tokens, which differs from `str.lower()` in a
    few exotic corners (e.g. Greek final sigma `ς` vs `σ`). Such a record is still recalled by the scan, so the
    locked law's guarantee — availability — holds; only the fast path's match set differs there, never a wrong
    result.
    """
    folded = unicodedata.normalize("NFD", text.lower())
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    tokens = []
    current = []
    for ch in folded:
        if unicodedata.category(ch)[0] in ("L", "N"):
            current.append(ch)
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return tokens


def _record_text(record) -> str:
    """The searchable text for one record — the projection BOTH lookup paths use (so they agree).

    Gathers the record's string leaf values and joins them, EXCLUDING the top-level envelope-metadata keys
    in `_NON_BODY_KEYS` (the locked tags-not-in-the-FTS-body law, plus the capture-record provenance fields
    that are not content). Otherwise shape-agnostic; the reflection slice finalizes the projection against
    the full record shape.
    """
    parts: list = []

    def walk(value) -> None:
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, (list, tuple)):
            for v in value:
                walk(v)

    if isinstance(record, dict):
        for key, value in record.items():
            if key in _NON_BODY_KEYS:
                continue
            walk(value)
    else:
        walk(record)
    return " ".join(parts)


def fts5_available(conn: "sqlite3.Connection | None" = None) -> bool:
    """True if this machine's SQLite has the FTS5 full-text module compiled in.

    The locked law: when FTS5 is absent the fast lookup is unavailable and recall falls back to the slow scan
    — availability holds, latency does not. This DETECTS and RETURNS that condition; boot renders the
    operator-facing disclosure. Absence is decided ONLY here, by probing — never by catching a query-time
    error, because a malformed MATCH raises the same `sqlite3.OperationalError` and must not be mislabeled
    "FTS5 absent".
    """
    own = conn is None
    if own:
        conn = sqlite3.connect(":memory:")
    try:
        conn.execute(f"CREATE VIRTUAL TABLE temp.{_FTS_PROBE_TABLE} USING fts5(x)")
        conn.execute(f"DROP TABLE temp.{_FTS_PROBE_TABLE}")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        if own:
            conn.close()


def index_path(cwd: "str | None" = None) -> str:
    """The derived-index file: a sibling of the ledger, in the gitignored `.engine/memory/` data dir."""
    return os.path.join(ledger.ledger_dir(cwd), INDEX_FILENAME)


def _build_schema(conn: sqlite3.Connection) -> None:
    # `entries` holds the full record per ledger ordinal (so a hit hydrates the exact record — the provenance
    # slice 5 ranks over). `entries_fts` is a standalone FTS5 index keyed by the same ordinal, fed the
    # PRE-FOLDED token stream from `_tokenize`. `remove_diacritics 0` tells FTS5 to do no diacritic folding of
    # its own — `_tokenize` already did it — so the indexed tokens are exactly what the scan path matches
    # against and the two paths agree across scripts. No porter stemming: it is a slice-5 ranking concern.
    conn.execute("CREATE TABLE entries (ord INTEGER PRIMARY KEY, record_json TEXT NOT NULL)")
    conn.execute("CREATE VIRTUAL TABLE entries_fts USING fts5(body, tokenize='unicode61 remove_diacritics 0')")


def rebuild(*, ledger_file: "str | None" = None, index_file: "str | None" = None) -> RebuildReport:
    """Rebuild the fast lookup from the ledger (the one source of truth).

    Throwaway-safe: builds a fresh index in a uniquely-named temp file IN THE TARGET DIRECTORY, closes it, then
    atomically `os.replace`s it into place — so a crash mid-rebuild leaves the previous index intact and a
    reader never sees a half-built one. Streams the ledger via `forget.live_records` (logically-retired
    duplicates excluded from recall; malformed/torn lines dropped), so one bad line never costs the rest and a
    crash-duplicated summary is indexed once. If this machine has no FTS5, there is no fast lookup to build and
    this returns a no-op report (recall uses the slow scan).
    """
    src = ledger.ledger_path() if ledger_file is None else ledger_file
    dst = index_path() if index_file is None else index_file
    if not fts5_available():
        return RebuildReport(fts5=False, path=dst)
    parent = os.path.dirname(dst) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".index-build-", suffix=".sqlite3")
    os.close(fd)
    report = RebuildReport(path=dst)
    try:
        # Default rollback journal (NOT WAL): a clean close leaves no -wal/-shm sidecars, so the whole index is
        # the single file we atomically replace into place.
        conn = sqlite3.connect(tmp)
        try:
            _build_schema(conn)
            ordinal = 0
            # `live_records` excludes logically-retired duplicates (a crashed pass's orphans) — the SAME shared
            # filter the slow `_scan` uses, so the fast and slow lookups retire identically (parity).
            for record in forget.live_records(path=src):
                tokens = _tokenize(_record_text(record))
                conn.execute(
                    "INSERT INTO entries (ord, record_json) VALUES (?, ?)",
                    (ordinal, json.dumps(record, ensure_ascii=False, separators=(",", ":"))),
                )
                # Store the PRE-FOLDED token stream (space-joined), not the raw text, so the fast lookup
                # indexes exactly the tokens the scan matches against — see `_tokenize` / `_build_schema`.
                conn.execute("INSERT INTO entries_fts (rowid, body) VALUES (?, ?)", (ordinal, " ".join(tokens)))
                report.indexed += 1
                if tokens:
                    report.with_text += 1
                ordinal += 1
            conn.commit()
        finally:
            conn.close()
        os.replace(tmp, dst)
    except BaseException:
        # Leave any prior index untouched; discard the half-built temp.
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return report


def _scan(query_tokens: list, src: str, limit: "int | None") -> list:
    """The slow backup lookup: read the ledger straight through, tokenize each record the same way, and keep
    the records whose text contains EVERY query token. Always available (no FTS5 needed). This is the single
    fallback path that both a genuine FTS5-absent machine and `force_scan=True` flow through, so exercising one
    is real evidence for the other. Reads through `live_records`, the same retirement filter the fast `rebuild`
    bakes in, so the slow backup returns the deduped set the fast lookup does (parity)."""
    want = set(query_tokens)
    out = []
    for record in forget.live_records(path=src):
        have = set(_tokenize(_record_text(record)))
        if want <= have:
            out.append(record)
            if limit is not None and len(out) >= limit:
                break
    return out


def query(
    text: str,
    *,
    limit: "int | None" = None,
    force_scan: bool = False,
    ledger_file: "str | None" = None,
    index_file: "str | None" = None,
) -> QueryResult:
    """Recall the records matching `text` — every query word must appear (implicit AND). Uses the fast lookup
    when this machine has FTS5 and the index exists; otherwise the slow backup scan over the ledger. Both paths
    apply the SAME tokenizer, so they return the same set of records. UNRANKED in slice 2 (ledger order);
    ranking is slice 5.
    """
    src = ledger.ledger_path() if ledger_file is None else ledger_file
    dst = index_path() if index_file is None else index_file
    tokens = _tokenize(text)
    if not tokens:
        return QueryResult(records=[], degraded=False)
    if (not force_scan) and fts5_available() and os.path.exists(dst):
        # Fast path. Per-token double-quoting neutralizes any FTS5 MATCH syntax — the tokens are letters/numbers
        # only (the tokenizer already stripped operators), so this is belt-and-suspenders.
        match = " ".join('"' + token + '"' for token in tokens)
        sql = (
            "SELECT e.record_json FROM entries_fts "
            "JOIN entries e ON e.ord = entries_fts.rowid "
            "WHERE entries_fts MATCH ? ORDER BY e.ord"
        )
        params: list = [match]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        try:
            conn = sqlite3.connect(dst)
            try:
                rows = conn.execute(sql, params).fetchall()
                records = [json.loads(row[0]) for row in rows]
            finally:
                conn.close()
            return QueryResult(records=records, degraded=False)
        except sqlite3.Error:
            # The fast lookup file is present but unreadable/corrupt (a truncated copy, a disk error, a stale
            # pre-atomic file). Availability law: fall through to the slow backup rather than take recall down.
            # A malformed MATCH cannot land here (the tokens are always valid), so this only ever catches a
            # broken index, never a bad query.
            pass
    return QueryResult(records=_scan(tokens, src, limit), degraded=True)


# --- Operator demonstration -------------------------------------------------------------------------------
# An operator-runnable walkthrough on a throwaway PRACTICE filing cabinet (a temp folder), never the real
# store. It exercises the REAL rebuild/query above. Run it and vary the questions/memories near the top:
#     uv run --directory .engine --frozen -- python tools/memory/index.py demo
# Plain words only — three nouns throughout: "the filing cabinet" (the one real copy), "the fast lookup",
# and "the slow backup lookup".

# Vary these and re-run — every memory that mentions your question's words should still come back both ways.
_DEMO_MEMORIES = [
    {"body": "We chose the snake_case_config naming for all the project settings."},
    {"body": "The cafe meeting on Tuesday decided to ship the export feature on Friday."},
    {"body": "We rejected the cron approach because it could not see the user's calendar."},
    {"body": "Preference: keep the onboarding copy short and friendly, no jargon."},
]
_DEMO_QUESTIONS = ["config", "cafe", "calendar"]


def _demo_same_both_ways(cabinet: str, index_file: str) -> bool:
    print("=" * 80)
    print("PART 1 — does the slow backup lookup find the SAME memories as the fast lookup?")
    print("=" * 80)
    for memory in _DEMO_MEMORIES:
        ledger.append(memory, path=cabinet)
    rebuild(ledger_file=cabinet, index_file=index_file)
    all_same = True
    for question in _DEMO_QUESTIONS:
        fast = query(question, ledger_file=cabinet, index_file=index_file)
        slow = query(question, force_scan=True, ledger_file=cabinet, index_file=index_file)
        fast_bodies = sorted(r["body"] for r in fast.records)
        slow_bodies = sorted(r["body"] for r in slow.records)
        same = fast_bodies == slow_bodies and len(fast_bodies) >= 1
        all_same = all_same and same
        print(f'\n  question: "{question}"')
        for body in fast_bodies:
            print(f"    found: {body}")
        print(f"    fast lookup and slow backup agree: {'yes' if same else 'NO'}")
    print('\n  Note: "config" only matches "snake_case_config" because the backup splits words the same careful')
    print("  way the fast lookup does — a naive backup would miss it. That is the faithfulness this proves.")
    print(f"  => {'Both ways found the same memories for every question.' if all_same else '!!! a mismatch'}")
    return all_same


def _demo_still_answered_when_fast_off(cabinet: str, index_file: str) -> bool:
    print("\n" + "=" * 80)
    print("PART 2 — turn the fast lookup OFF (as if this computer lacked the fast-search feature)")
    print("=" * 80)
    result = query("config", force_scan=True, ledger_file=cabinet, index_file=index_file)
    answered = len(result.records) >= 1
    print(f'\n  question: "config", with the fast lookup turned off')
    for record in result.records:
        print(f"    still found: {record['body']}")
    print("\n  Nothing is broken — the question is still answered. On a large memory this backup is slower than")
    print("  the fast lookup; you will not see that here because this practice cabinet is tiny. In real use the")
    print("  engine will tell you at startup when it is running on the slow backup, so a slow answer is never a")
    print("  mystery (that startup notice is a later step). A missing fast-search feature is a non-event, never")
    print("  a failure.")
    print(f"  => {'Answered without the fast lookup.' if answered else '!!! not answered'}")
    return answered


def _demo_throwaway_nothing_lost(cabinet: str, index_file: str) -> bool:
    print("\n" + "=" * 80)
    print("PART 3 — DELETE the entire fast lookup. Is anything lost?")
    print("=" * 80)
    ledger.append({"body": "DO NOT LOSE THIS — the decision we must never forget."}, path=cabinet)
    rebuild(ledger_file=cabinet, index_file=index_file)
    before = query("forget", ledger_file=cabinet, index_file=index_file)
    os.remove(index_file)  # blow away the whole fast lookup
    after_gone = query("forget", ledger_file=cabinet, index_file=index_file)
    rebuild(ledger_file=cabinet, index_file=index_file)  # rebuilt from the one real copy
    after_rebuilt = query("forget", ledger_file=cabinet, index_file=index_file)

    def bodies(result):
        return [r["body"] for r in result.records]

    survived = bodies(before) == bodies(after_gone) == bodies(after_rebuilt) and len(bodies(before)) == 1
    print(f"\n  before deleting the fast lookup: {bodies(before)}")
    print(f"  after deleting the fast lookup:  {bodies(after_gone)}   (answered by the slow backup)")
    print(f"  after rebuilding the fast lookup: {bodies(after_rebuilt)}")
    print("\n  The fast lookup is disposable: deleting it lost nothing, and the engine rebuilt it from the")
    print("  filing cabinet — the one real copy. Backing up memory only ever means copying the cabinet.")
    print(f"  => {'Nothing was lost.' if survived else '!!! something was lost'}")
    return survived


def _demo_one_bad_entry(cabinet: str, index_file: str) -> bool:
    print("\n" + "=" * 80)
    print("PART 4 — one corrupted entry in the cabinet. Do the memories around it survive?")
    print("=" * 80)
    ledger.append({"body": "the lesson we keep about retries"}, path=cabinet)
    with open(cabinet, "a", encoding="utf-8") as fh:  # a corrupted, unreadable line
        fh.write("@@@ corrupted junk that is not a real memory @@@\n")
    ledger.append({"body": "the lesson we keep about timeouts"}, path=cabinet)
    rebuild(ledger_file=cabinet, index_file=index_file)
    fast = [r["body"] for r in query("lesson", ledger_file=cabinet, index_file=index_file).records]
    slow = [r["body"] for r in query("lesson", force_scan=True, ledger_file=cabinet, index_file=index_file).records]
    ok = "the lesson we keep about retries" in fast and "the lesson we keep about timeouts" in fast and fast == slow
    print(f"\n  memories found around the corrupted entry: {sorted(fast)}")
    print("  The corrupted entry was skipped by both lookups; the good memories on either side came back.")
    print(f"  => {'Both good memories survived.' if ok else '!!! a good memory was lost'}")
    return ok


def _demo() -> int:
    import shutil

    if not fts5_available():
        print("This computer's search feature is unavailable, so this demo would only show the slow backup.")
        print("That is itself fine (recall still works), but the side-by-side comparison needs the fast lookup.")
        return 0
    tmp = tempfile.mkdtemp(prefix="engine-memory-demo-")
    try:
        cabinet = os.path.join(tmp, "ledger.ndjson")
        index_file = os.path.join(tmp, "index.sqlite3")
        results = [
            _demo_same_both_ways(cabinet, index_file),
            _demo_still_answered_when_fast_off(cabinet, index_file),
            _demo_throwaway_nothing_lost(cabinet, index_file),
            _demo_one_bad_entry(cabinet, index_file),
        ]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "-" * 80)
    print("Reminder: what you just saw is the engine looking things up in a PRACTICE cabinet we filled for this")
    print("demo. In real use the cabinet is still EMPTY — nothing has filed anything into it yet — and the")
    print("engine still does NOT remember across sessions. The piece that files your real work into the cabinet,")
    print("and the piece that looks things up while you work, are later steps. Today only proves: once things")
    print("ARE filed, you can always look them up — fast when the search feature is present, slower but never")
    print("broken when it is not, and nothing is lost if the fast lookup is thrown away.")
    print("\nVary it yourself: edit the questions and memories near the top of this file and run it again.")
    return 0 if all(results) else 1


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    print("usage: index.py demo")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
