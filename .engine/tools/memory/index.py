"""index.py — the engine's derived memory lookup: the throwaway SQLite/FTS5 accelerator + the plain-scan floor.

The ledger (`ledger.py`) is the ONE source of truth. This module builds a FAST lookup over it — a
SQLite FTS5 full-text index — and, beneath that, a SLOW backup lookup: a plain scan straight through the
ledger, for when a machine's SQLite was built without the FTS5 module. The promise is the locked law: recall
always answers — *availability holds, latency does not*. When FTS5 is absent the answer still comes back,
just slower.

This index is DERIVED and THROWAWAY. It is rebuilt from the ledger and is never the only copy; deleting it
loses nothing (`rebuild()` reconstructs it), and backup is still "copy the ledger", never this file.

Leaf discipline: this module DETECTS the FTS5-absent / slow-path condition and RETURNS it to the caller; it
never renders operator-facing prose (boot does that). It writes no telemetry and logs no findings.

This module builds the index machinery + the two retrieval paths, record-shape-agnostic and UNRANKED (`query`). Ranked, filtered recall is `search` (BM25 best-first reinforced by usage; role/tag filters), implementing the
`search.json` contract and exposed by the engine-memory MCP server (`mcp_server.py`). `query` stays UNRANKED for the
rebuild/scan callers. The boot/attention per-prompt scent over this index is `scent_lookup`: a fast,
OR-match, relevance-ONLY top-k lookup (no usage pass, fast-path only) the boot-owned `scent.py` UserPromptSubmit hook
calls to surface attributed pointers. The closed record shape + role vocabulary come from the reflection step.

Both retrieval paths split text into words with ONE tokenizer (`_tokenize`, modeled on SQLite's FTS5
`unicode61`): the fast lookup stores the tokens it produces, and the slow scan matches the same way. That one
shared word-splitter — not FTS5's own, which folds some scripts differently — is what makes the slow backup
return the same set of records the fast lookup does, not a degraded different answer.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field

# Make the package parent (.engine/tools) importable so `from memory import ledger` resolves even when this
# file is run directly as a script (the demo). When imported as `memory.index`, the parent is already on
# sys.path, so this is a guarded no-op. (Not FS/DB work — close.py never imports this module, only the
# side-effect-free package `__init__`, so the leaf-safety invariant is untouched.)
_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from memory import forget, ledger, records, score  # noqa: E402

INDEX_FILENAME = "index.sqlite3"
_FTS_PROBE_TABLE = "engine_fts5_probe"
# Top-level record fields kept OUT of the searchable text. `tags` honors the locked typing law (tags are a
# secondary filter, never in the FTS body, so tag drift never poisons term statistics). The capture-record
# ENVELOPE metadata is excluded for the same reason: `session_id` (a per-session UUID — its hex fragments are
# real words: dead/beef/cafe/face…), `kind` ("turn-delta"), and `speaker` ("user"/"assistant") are provenance,
# not content, and indexing them makes `query("user")`/`query("delta")` match every record. Only the human
# `text` (and any other non-metadata string leaf) is searchable. The closed role vocabulary the reflection
# step adds is a structured filter, so `role` joins this set: searching a label like "decision" must
# never drag in every record that carries it (the same pollution the capture-record provenance fields would
# cause). Episodic provenance (`consolidated_ts`, `source_seqs`) is non-string and stays out by type. The
# per-pass `batch` id the forgetting step adds is a uuid — its hex fragments are real words, exactly the
# `session_id` problem — so it joins this set too. The per-record `id` is also a uuid hex (its only
# purpose is to NAME a record, never to be searched), so it joins for the same reason. The reinforcement
# marker's `target` is a uuid hex too — it points at the reinforced record's `id` — so it joins as
# well (the marker is dropped from recall by `forget.live_records` before indexing, but this keeps it out of
# the body even if it were reached). The carried `tier` (a compaction carry) is a STRING
# ("hot"/"cold"/"archived"), so it MUST join too, else those words would match every compacted record; its
# sibling carried fields (frecency_snapshot/snapshot_ts/last_access_ts) are numeric and stay out of the body by
# type already (the projection indexes only string leaves). The gist roll-up adds two more uuid-hex
# fields: a raw episode's `superseded_by` (the gist id a compaction folded onto it) and a gist's `source_ids`
# (the list of raw ids it consolidates) — both are uuid hex, exactly the `id`/`batch` problem, so both join too.
_TAGS_KEY = "tags"
_NON_BODY_KEYS = frozenset(
    {"tags", "session_id", "kind", "speaker", "role",
     records.BATCH_KEY, records.RECORD_ID_KEY, records.TARGET_KEY, records.TIER_KEY,
     records.SUPERSEDED_BY_KEY, records.SOURCE_IDS_KEY, records.SCORE_KEY, records.MERGE_SHA_KEY}
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
    """The records matching a query. `query` returns them in ledger order (UNRANKED); `search` returns
    them ranked best-first, each a shallow copy carrying `records.SCORE_KEY` (the lexical relevance). `degraded` is
    True when the answer came from the slow backup scan (FTS5 absent, the fast lookup not yet built, or scan forced)."""

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
    that are not content). Otherwise shape-agnostic; the reflection step finalizes the projection against
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
    # `search` ranks over). `entries_fts` is a standalone FTS5 index keyed by the same ordinal, fed the
    # PRE-FOLDED token stream from `_tokenize`. `remove_diacritics 0` tells FTS5 to do no diacritic folding of
    # its own — `_tokenize` already did it — so the indexed tokens are exactly what the scan path matches
    # against and the two paths agree across scripts. No porter stemming — `search` ranks the un-stemmed
    # tokens (bm25 over this body); stemming stays a future ranking concern.
    conn.execute("CREATE TABLE entries (ord INTEGER PRIMARY KEY, record_json TEXT NOT NULL)")
    conn.execute("CREATE VIRTUAL TABLE entries_fts USING fts5(body, tokenize='unicode61 remove_diacritics 0')")
    # `meta` carries the ledger GENERATION this index was built against. `query` trusts the fast
    # lookup only when this matches the ledger's current generation — so a compaction that swapped the ledger
    # out from under a stale index is detected and the query falls back to the always-correct scan, never a
    # stale fast answer (a full index rebuild gated on a monotonic ledger-generation stamp).
    conn.execute("CREATE TABLE meta (rowid INTEGER PRIMARY KEY, generation INTEGER NOT NULL)")


def _index_generation(conn: sqlite3.Connection) -> int:
    """The ledger generation the fast index was built against (its `meta` row), or -1 if absent/unreadable — a
    value that never equals a real ledger generation (>= 0), so an unstamped/old index is treated as stale and
    the query falls back to the slow scan."""
    try:
        row = conn.execute("SELECT generation FROM meta WHERE rowid = 1").fetchone()
    except sqlite3.Error:
        return -1
    if not row:
        return -1
    val = row[0]
    return val if isinstance(val, int) and not isinstance(val, bool) and val >= 0 else -1


def rebuild(*, ledger_file: "str | None" = None, index_file: "str | None" = None) -> RebuildReport:
    """Rebuild the fast lookup from the ledger (the one source of truth).

    Throwaway-safe: builds a fresh index in a uniquely-named temp file IN THE TARGET DIRECTORY, closes it, then
    atomically `os.replace`s it into place — so a crash mid-rebuild leaves the previous index intact and a
    reader never sees a half-built one. Streams the ledger via `forget.live_records` (logically-retired
    duplicates excluded from recall; malformed/torn lines dropped), so one bad line never costs the rest and a
    crash-duplicated summary is indexed once. If this machine has no FTS5, there is no fast lookup to build and
    this returns a no-op report (recall uses the slow scan). Stamps the ledger generation it built against
    so `query` can detect a compaction-staled index and fall back to the scan.
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
            # Stamp the ledger generation this index was built against — `query`'s fast path is
            # trusted only while it matches `ledger.generation`. Resolved from the SAME ledger file being read
            # (its sidecar sibling), never the default dir, so an explicit `ledger_file=` build stamps its own
            # store's generation.
            conn.execute("INSERT INTO meta (rowid, generation) VALUES (1, ?)",
                         (ledger.generation(for_path=src),))
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
    apply the SAME tokenizer, so they return the same set of records. UNRANKED (ledger order);
    ranking is a later concern.
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
                # Trust the fast lookup only while its stamped generation matches the ledger's current one
                # A mismatch means a compaction swapped the ledger out from under this index — treat
                # it like a missing index and fall through to the always-correct scan over the CURRENT ledger,
                # never a stale fast answer. The stamp is read from the same `conn`; the ledger generation from
                # the queried ledger file's own sidecar.
                if _index_generation(conn) == ledger.generation(for_path=src):
                    rows = conn.execute(sql, params).fetchall()
                    records = [json.loads(row[0]) for row in rows]
                    return QueryResult(records=records, degraded=False)
            finally:
                conn.close()
        except sqlite3.Error:
            # The fast lookup file is present but unreadable/corrupt (a truncated copy, a disk error, a stale
            # pre-atomic file). Availability law: fall through to the slow backup rather than take recall down.
            # A malformed MATCH cannot land here (the tokens are always valid), so this only ever catches a
            # broken index, never a bad query.
            pass
    return QueryResult(records=_scan(tokens, src, limit), degraded=True)


# --- Ranked recall: the `search` interface ------------------------------------------------------
# `query` (above) answers UNRANKED — it is the rebuild/scan workhorse and must stay order-stable for its callers
# and tests. `search` is the ranked, filtered recall the `search.json` contract names: best-first by lexical
# relevance, reinforced by usage, with optional role/tag filters. It is SIDE-EFFECT-FREE — it never reinforces and
# never writes the ledger; the live reinforcement-on-recall caller is the engine-memory MCP server (mcp_server.py),
# at the recall boundary, never here (rebuild/_scan/the demos all call read-only).

# Build-spec leaf `search-rank`: the decimal places bm25/relevance is rounded to before the usage tiebreak. It
# groups NEAR-equal matches into one relevance bucket so usage can reorder them (the "reinforced by usage" move),
# while a clearly-stronger match lands in its own better bucket and is NEVER overtaken by usage ("BM25 leads").
# Coarser (fewer places) lets usage reorder more; finer makes lexical relevance stricter — the demo validates both
# directions. The ordering is LEXICOGRAPHIC, deliberately NOT a multiplicative blend: a multiplicative
# rel * (1 + k*usage) gives usage ZERO leverage where bm25 ties at ~0 (a common query term) and UNBOUNDED leverage
# where bm25 separates (frecency is unwindowed) — inverting "BM25 leads".
_REL_DECIMALS = 1


def _validate_roles(roles):
    """The role filter as a set, or None for no filter. An unknown role (outside the closed vocabulary —
    `score.ROLE_WEIGHTS` keys, which a test pins == `consolidate.ROLE_VOCABULARY` == `search.json`'s roles) raises
    ValueError: a caller asking for a misspelled role is told, never silently handed all-roles results. The
    engine-memory server surfaces the raise as a tool error; the server survives."""
    if roles is None:
        return None
    valid = set(score.ROLE_WEIGHTS)
    unknown = set(roles) - valid
    if unknown:
        raise ValueError(f"unknown role(s): {sorted(unknown)}; valid roles are {sorted(valid)}")
    return set(roles)


def _passes_filters(record, roles, tags) -> bool:
    """The structured POST-FETCH filters (role/tags are non-body — never FTS MATCH terms). `roles`: the record's
    `role` must be in the set. `tags`: any-match — the record shares at least one of the requested tags. Both apply
    identically on the fast and slow paths, so the degraded path returns the same FILTERED set."""
    if roles is not None:
        if not isinstance(record, dict) or record.get("role") not in roles:
            return False
    if tags is not None:
        have = record.get("tags") if isinstance(record, dict) else None
        if not isinstance(have, (list, tuple)) or not (set(have) & tags):
            return False
    return True


def _usage_of(record, access_index, now: int) -> float:
    """The usage signal for the tiebreak: `score.score` (frecency × role-weight × recency) over the record's
    accesses, collected once into `access_index`. A record with no id / no accesses still scores from birth —
    never zero, so it is only deprioritized, never dropped (ranking, not retention)."""
    rid = record.get(records.RECORD_ID_KEY) if isinstance(record, dict) else None
    access_ts = access_index.get(rid, ()) if isinstance(rid, str) and rid else ()
    return score.score(record, access_ts, now)


def _rank_slice_score(candidates: list, limit: "int | None") -> list:
    """Order the candidates best-first, slice to `limit`, and attach `records.SCORE_KEY` (the lexical relevance) to
    a SHALLOW COPY of each kept record (never mutate the live record — the score must not leak back into the
    ledger/index). Each candidate is `(rel, usage, ord, record)` with `rel` the positive lexical relevance
    (higher = better). Sort key: bucketed relevance DESC (via `-rel` rounded, ASC), then usage DESC, then ledger
    `ord` ASC (a stable, deterministic final tiebreak)."""
    candidates.sort(key=lambda c: (round(-c[0], _REL_DECIMALS), -c[1], c[2]))
    if limit is not None:
        candidates = candidates[:limit]
    out = []
    for rel, _usage, _ord, record in candidates:
        scored = dict(record) if isinstance(record, dict) else record
        if isinstance(scored, dict):
            scored[records.SCORE_KEY] = rel
        out.append(scored)
    return out


def _ranked(tokens, src, dst, *, roles, tags, limit, force_scan, now):
    """The shared ranked retrieval. Fast path: bm25 over the FTS5 index (when present + generation-current); slow
    path: a full scan computing a damped term-frequency relevance. BOTH rank the FULL matched set, THEN slice —
    never an early ledger-order truncation, so the fast and slow paths return the same SET (the availability law;
    exact ORDER may differ on the degraded path). Returns a QueryResult."""
    access_index = forget._access_index(src)
    # Fast path — trust the FTS5 index only while its stamped generation matches the ledger's current one.
    if (not force_scan) and fts5_available() and os.path.exists(dst):
        match = " ".join('"' + token + '"' for token in tokens)
        sql = (
            "SELECT e.ord, e.record_json, bm25(entries_fts) AS relevance "
            "FROM entries_fts JOIN entries e ON e.ord = entries_fts.rowid "
            "WHERE entries_fts MATCH ? ORDER BY relevance"
        )
        try:
            conn = sqlite3.connect(dst)
            try:
                if _index_generation(conn) == ledger.generation(for_path=src):
                    rows = conn.execute(sql, [match]).fetchall()
                    candidates = []
                    for ordinal, record_json, relevance in rows:
                        record = json.loads(record_json)
                        if not _passes_filters(record, roles, tags):
                            continue
                        # bm25 is more-negative for a better match; flip to a positive relevance (higher = better).
                        rel = -float(relevance)
                        candidates.append((rel, _usage_of(record, access_index, now), ordinal, record))
                    return QueryResult(records=_rank_slice_score(candidates, limit), degraded=False)
            finally:
                conn.close()
        except sqlite3.Error:
            # Broken/corrupt index: fall through to the always-correct scan (availability law). A malformed MATCH
            # cannot land here (the tokens are always valid), so this only catches a broken index, never a bad query.
            pass
    # Slow path — rank the FULL matched set (no early limit break, so the SET matches the fast path).
    want = set(tokens)
    candidates = []
    for ordinal, record in enumerate(forget.live_records(path=src, now=now)):
        body_tokens = _tokenize(_record_text(record))
        if not (want <= set(body_tokens)):
            continue
        if not _passes_filters(record, roles, tags):
            continue
        tf = sum(1 for t in body_tokens if t in want)   # total query-term occurrences in the body
        candidates.append((math.log1p(tf), _usage_of(record, access_index, now), ordinal, record))
    return QueryResult(records=_rank_slice_score(candidates, limit), degraded=True)


# --- The cold-start recent-decisions pull (#394) -------------------------------------------------------
# The roles that ARE the decision record — what "recent decisions" means when boot pulls recall into the cold
# -start pack. A recorded build-spec leaf: the decision itself plus the reasoning/pushback behind
# it. A lesson, dead-end, preference, intent or observation is recall, but it is not a DECISION, so it does not
# compete for this partition's slice. Members of the closed role vocabulary (score.ROLE_WEIGHTS).
_DECISION_ROLES = ("decision", "rationale/pushback")
# How many the reader scans back for; how many SURFACE is the attention policy's budget_recent_decisions slice.
_RECENT_DECISIONS_LIMIT = 20


def recent_decisions(*, limit: int = _RECENT_DECISIONS_LIMIT, roles=_DECISION_ROLES,
                     ledger_file: "str | None" = None) -> list[dict]:
    """The most recently RECORDED decisions, newest first — the memory half of attention's recent-decisions
    partition (that partition draws from recently merged pull requests plus what the memory-recall boot assembles
    into the pack; at cold start boot pulls memory recall when their servers are up).

    NON-QUERY by construction, and that is the point: a cold start has no prompt yet to match against, so this
    is RECENCY-ordered, not lexical. Lexical recall against the operator's words is the per-prompt scent's job
    (`scent_lookup`); this answers the different question boot asks at orientation — "what was decided lately?".

    Reads the CURATED layer through `forget.live_records`, the one shared read path (so the ambient `turn-delta`
    verbatim is excluded here exactly as it is from search), and filters to the decision-bearing
    roles. SIDE-EFFECT-FREE: never reinforces, never writes the ledger — boot is read-only, and merely
    orienting must not silently re-rank what recall surfaces later.

    Deterministic: ordered by the record's own recorded `ts` (newest first), ties broken by record id, so the
    same ledger always yields the same list — the ranking downstream stays reproducible. Records with no usable
    `ts` sort last rather than crash the sort. Degrades to [] on any read fault: recall is orientation context,
    and boot surfaces an unreadable store separately (its own memory-offline notice), never from here."""
    def _order(record):
        # A TOTAL key: a `ts` that is not a real number (absent, a string, a bool, NaN) sorts into the
        # unusable bucket and carries a fixed 0, so tuples of mixed records only ever compare like with like.
        # Falling back to the raw value instead would compare a str against an int the moment one record's ts
        # was a string and another's was absent — raising, from a function whose whole contract is that a
        # damaged record costs its own place in the order and nothing else.
        ts = record.get("ts")
        usable = isinstance(ts, (int, float)) and not isinstance(ts, bool) and math.isfinite(ts)
        return usable, (ts if usable else 0), str(record.get(records.RECORD_ID_KEY) or "")

    wanted = set(roles)
    try:
        out = [r for r in forget.live_records(ledger_file) if r.get("role") in wanted]
        out.sort(key=_order, reverse=True)   # inside the guard: the contract is [] on ANY read fault
    except Exception:  # noqa: BLE001 — an unreadable/degraded store costs the digest, never the pack
        return []
    return out[:limit]


def search(
    query_text: str,
    *,
    roles: "list | None" = None,
    tags: "list | None" = None,
    limit: "int | None" = None,
    force_scan: bool = False,
    ledger_file: "str | None" = None,
    index_file: "str | None" = None,
) -> QueryResult:
    """Ranked, filtered recall — the `search` interface (search.json). Every query word must appear (implicit AND),
    and the matches come back BEST-FIRST: by lexical relevance (bm25 on the fast path, a damped term-frequency
    proxy on the slow backup), with usage (frecency × role-weight × recency) breaking near-ties but never
    overriding a clearly-stronger match. Optional `roles` (the closed vocabulary; an unknown role raises
    ValueError) and `tags` (any-match) narrow. Each result is a shallow copy carrying `records.SCORE_KEY` (the
    lexical relevance). `degraded` is True when answered by the slow backup scan.

    SIDE-EFFECT-FREE: never reinforces, never writes the ledger — the live reinforcement-on-recall caller is the
    engine-memory MCP server, at the recall boundary, not here."""
    src = ledger.ledger_path() if ledger_file is None else ledger_file
    dst = index_path() if index_file is None else index_file
    roles_set = _validate_roles(roles)
    tags_set = set(tags) if tags is not None else None
    tokens = _tokenize(query_text)
    if not tokens:
        return QueryResult(records=[], degraded=False)
    now = int(time.time())
    return _ranked(tokens, src, dst, roles=roles_set, tags=tags_set, limit=limit, force_scan=force_scan, now=now)


# --- The per-prompt scent lookup: `scent_lookup` -------------------------------------------
# The scent is a HOT PATH (the boot-owned `scent.py` UserPromptSubmit hook fires it every prompt) under a
# single-digit-ms budget (single-digit ms; no embeddings, no LLM, no graph walk). So `search` is
# the WRONG primitive for it on two counts, both measured: (1) `search`/`_ranked` does an unconditional
# O(ledger) `forget._access_index` + `live_records` usage pass (~46 ms on a ~1800-record ledger) — the
# "reinforced by usage" tiebreak the scent's threshold gate does not even read; (2) `search` is implicit-AND, so
# a raw multi-word prompt requires ONE record holding EVERY prompt word and matches almost nothing. `scent_lookup`
# is the scent-shaped primitive: OR-match the prompt's tokens, rank by bm25 ALONE (relevance-only — NO usage
# pass, so no O(ledger) cost: the FTS5 index is already rebuilt from `live_records`, so a direct query returns
# only live records), bounded top-k, and FAST-PATH ONLY — it never runs the slow scan (that both blows the
# latency budget and uses a different score scale, log1p(tf) vs bm25, so the threshold gate would diverge).
# `available=False` is reserved for the design's ONE degraded-latency condition — FTS5 absent on this machine
# (memory detects, boot renders the scent's slower-mode disclosure); a merely missing/stale/broken
# index is "no fast recall right now" -> silent (records=[], available=True), never a misleading slower-mode notice.

# How many distinct prompt tokens feed the OR-match, and the default bm25 top-k. Bounded so a long prompt cannot
# build an unbounded MATCH and a common term cannot return the whole ledger before the bm25 ORDER BY ... LIMIT
# trims to the strongest. (Common terms score ~0 via bm25 IDF, so they are kept only as weak OR alternates and
# fall below any sane salience threshold downstream — no stopword list is needed for correctness.)
_SCENT_MAX_QUERY_TERMS = 32
_SCENT_DEFAULT_TOPK = 20


@dataclass
class ScentResult:
    """The per-prompt scent's lookup outcome. `records` are bm25-ranked best-first shallow copies, each carrying
    `records.SCORE_KEY` (the positive lexical relevance) — the input to the scent's salience threshold gate.
    `available` is False ONLY when this machine has no FTS5 (the design's degraded-latency condition: the scent
    surfaces a slower-mode disclosure and stays silent on pointers, never running a per-prompt slow scan). A
    missing/stale/broken index, or a prompt with no usable terms, returns `available=True` with empty `records`
    (silent — no fast recall this prompt), never a degraded notice."""

    records: list = field(default_factory=list)
    available: bool = True


def _salient_terms(query_text: str) -> list:
    """The de-duplicated prompt tokens that feed the OR-match, length>=2 and capped at `_SCENT_MAX_QUERY_TERMS`
    (a bounded MATCH). Uses the shared `_tokenize` so the scent folds words exactly as the index stored them."""
    terms: list = []
    for token in _tokenize(query_text):
        if len(token) >= 2 and token not in terms:
            terms.append(token)
            if len(terms) >= _SCENT_MAX_QUERY_TERMS:
                break
    return terms


def scent_lookup(
    query_text: str,
    *,
    limit: "int | None" = None,
    ledger_file: "str | None" = None,
    index_file: "str | None" = None,
) -> ScentResult:
    """The per-prompt scent's fast lexical lookup over the FTS5 index — OR-match, bm25-ranked, RELEVANCE-ONLY.

    Returns up to `limit` (default `_SCENT_DEFAULT_TOPK`) records whose words best match the prompt's, best-first by
    bm25, each a shallow copy carrying `records.SCORE_KEY` (the positive relevance — higher = better). It does NOT
    compute usage/frecency (the scent's salience gate reads relevance alone), so it pays NO O(ledger) pass; and it is
    FAST-PATH ONLY — on FTS5-absent / missing / stale / broken index it returns empty rather than running the slow
    scan, protecting the single-digit-ms budget. SIDE-EFFECT-FREE: never reinforces, never writes the ledger (the
    push does not count as usage; reinforcement stays at the model-initiated MCP `search` pull). `available` is False
    only for FTS5-absent — the design's one slower-mode disclosure condition."""
    # Resolve the shared memory dir ONCE when both paths default: `ledger_path`/`index_path` each call
    # `ledger_dir` -> a worktree-aware `git rev-parse` subprocess (~tens of ms), so resolving per-path would
    # pay it TWICE every prompt. One resolution keeps the per-prompt path cost to a single git call; the FTS5
    # query itself is sub-millisecond. (This git step is constant in memory size — it does not grow with the
    # ledger, unlike the O(ledger) usage pass `search` pays — and every engine hook that touches memory, e.g.
    # close's capture relay, already pays it.)
    if ledger_file is None or index_file is None:
        mem_dir = ledger.ledger_dir()
        src = ledger_file or os.path.join(mem_dir, ledger.LEDGER_FILENAME)
        dst = index_file or os.path.join(mem_dir, INDEX_FILENAME)
    else:
        src, dst = ledger_file, index_file
    terms = _salient_terms(query_text)
    topk = _SCENT_DEFAULT_TOPK if limit is None else max(1, int(limit))
    if not terms:
        return ScentResult(records=[], available=True)        # nothing to look up -> silent, not degraded
    if not fts5_available():
        return ScentResult(records=[], available=False)       # the ONE degraded-latency condition (slower-mode)
    if not os.path.exists(dst):
        return ScentResult(records=[], available=True)        # no index built yet -> silent (never a slow scan)
    match = " OR ".join('"' + token + '"' for token in terms)
    sql = (
        "SELECT e.record_json, bm25(entries_fts) AS relevance "
        "FROM entries_fts JOIN entries e ON e.ord = entries_fts.rowid "
        "WHERE entries_fts MATCH ? ORDER BY relevance LIMIT ?"
    )
    try:
        conn = sqlite3.connect(dst)
        try:
            # Trust the fast lookup only while its stamped generation matches the ledger's current one (a
            # compaction that swapped the ledger out leaves a stale index). Stale -> silent, NOT the slow scan.
            if _index_generation(conn) != ledger.generation(for_path=src):
                return ScentResult(records=[], available=True)
            rows = conn.execute(sql, [match, topk]).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        # Broken/corrupt index -> silent this prompt (no slow scan on the hot path). A malformed MATCH cannot
        # land here: the tokens are letters/numbers only and each is double-quoted, so this only catches a broken
        # index, never a bad query.
        return ScentResult(records=[], available=True)
    out = []
    for record_json, relevance in rows:
        record = json.loads(record_json)
        scored = dict(record) if isinstance(record, dict) else record
        if isinstance(scored, dict):
            # bm25 is more-negative for a better match; flip to a positive relevance (higher = better), the same
            # convention `search` exposes via SCORE_KEY, so the scent's salience gate reads one scale.
            scored[records.SCORE_KEY] = -float(relevance)
        out.append(scored)
    return ScentResult(records=out, available=True)


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
    print("  the fast lookup; you will not see that here because this practice cabinet is tiny. In real use, when")
    print("  the fast recall is unavailable the engine tells you — its automatic per-prompt memory hints pause and")
    print("  say so once — so a slower answer is never a mystery. A missing fast-search feature is a non-event,")
    print("  never a failure.")
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
