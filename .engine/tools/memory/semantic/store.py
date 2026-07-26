"""store.py — the vector store behind meaning-based recall, and the search over it.

WHY THIS IS ITS OWN FILE. The vectors live in `vectors.sqlite3`, beside the ledger and the keyword index but
separate from both. The keyword index belongs to the always-present memory substrate and its rebuild is
strict: any fault there propagates into consolidation, compaction and restore, which is why an optional
capability must not be able to reach it. Keeping the vectors apart means this module can be absent, stale,
or broken without the keyword floor noticing — and means installing it needs no rebuild of anything else.

WHY PASSAGES AND NOT WHOLE RECORDS. A vector for a passage is the average of its word vectors, so averaging
a long record pulls every distinct thing it says toward one blurred middle. Measured on real captured
conversation, whole-record vectors ranked plainly unrelated records above the right ones. Records are
therefore split into short passages and each is embedded on its own; a record scores as well as its best
passage. This is why the store holds more rows than the ledger has records.

WHAT IT IS. A throwaway derivative, exactly like the keyword index: every vector is reproducible from the
ledger and the committed word table, deleting the file loses nothing, and it is never the only copy of
anything.

HOW IT STAYS CURRENT. There is no background job and no capture-time work. The store reconciles itself at
the moment a question is asked: records that have appeared since last time are embedded, and records that
have gone are dropped. That ordering is what makes erasure safe — a record the operator removed cannot
linger as a vector and be found by meaning after its text is gone. Two independent guarantees, because one
is not enough for a deletion the operator asked for: the reconcile deletes the vectors, and the answer is
assembled only from records read live from the ledger in the same pass.

WHY BRUTE FORCE. Similarity here is one matrix multiply against a few tens of thousands of rows — a handful
of milliseconds, scored in blocks so peak memory stays flat however large the store grows. A vector index
would add a compiled SQLite extension, which has no source fallback on several platforms this engine
supports, to save time that is not being spent.
"""

import os
import re
import sqlite3

from memory import forget, index, ledger
from memory.semantic import embed

STORE_FILENAME = "vectors.sqlite3"

# Bumped whenever what the store holds, or how a vector is derived, changes — so an older store is rebuilt
# rather than silently mixed with vectors made a different way.
SCHEMA_VERSION = 1

DEFAULT_LIMIT = 10

# Passage length, in characters. Short enough that one passage is about one thing, long enough to carry a
# whole thought — sentence boundaries are preferred and this is the cap when a sentence runs long.
PASSAGE_CHARS = 220

# The most passages taken from any one record, so a single enormous record cannot dominate the store.
MAX_PASSAGES = 24

# Rows scored per block. Keeps peak memory flat: the stored rows are one byte per dimension and only a
# block is widened to float at a time.
SCORE_BLOCK = 8192

# Below this closeness a passage is not a match, only the nearest thing present. Unlike the keyword path —
# which returns nothing when a word is absent — every question has a nearest neighbour, so without a floor
# "have we hit this before?" would always answer yes. Measured against this engine's own history: questions
# with a real answer score above 0.42, an unrelated question peaks around 0.36. Set below the true answers
# rather than above the noise, because the caller reads the passages and the score, and a near-miss it can
# dismiss costs less than an answer it never sees.
MIN_SIMILARITY = 0.40

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def store_path(path: "str | None" = None) -> str:
    """Where the vectors live — beside the ledger, in the gitignored memory directory."""
    if path is not None:
        return path
    return os.path.join(ledger.ledger_dir(), STORE_FILENAME)


def passages(text: str) -> list:
    """`text` split into short passages on sentence and paragraph boundaries.

    Sentences are packed together up to the length cap rather than emitted one at a time, so a passage
    carries enough context to mean something. A record with no sentence punctuation still yields its
    opening — never an empty list, which would make the record unreachable.
    """
    out, current = [], ""
    for piece in _SENTENCE_SPLIT.split(text):
        piece = piece.strip()
        if not piece:
            continue
        if current and len(current) + 1 + len(piece) > PASSAGE_CHARS:
            out.append(current)
            current = piece
        else:
            current = f"{current} {piece}".strip()
    if current:
        out.append(current)
    return out[:MAX_PASSAGES] or ([text[:PASSAGE_CHARS]] if text.strip() else [])


def _table_fingerprint() -> str:
    """Identifies the word table the stored vectors were made with.

    Vectors from two different tables are not comparable, and comparing them degrades ranking silently
    rather than raising. Recording which table produced the store lets a change discard it wholesale.
    """
    import json

    with open(embed.CHECKSUMS_FILE, encoding="utf-8") as fh:
        recorded = json.load(fh)
    name = os.path.basename(embed.TABLE_FILE)
    return ((recorded.get("files") or {}).get(name) or {}).get("sha256", "")


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS passages ("
                 "  record_id TEXT NOT NULL,"
                 "  ordinal INTEGER NOT NULL,"
                 "  vec BLOB NOT NULL,"
                 "  scale REAL NOT NULL,"
                 "  PRIMARY KEY (record_id, ordinal))")
    conn.execute("CREATE TABLE IF NOT EXISTS meta ("
                 "  rowid INTEGER PRIMARY KEY CHECK (rowid = 1),"
                 "  schema_version INTEGER NOT NULL,"
                 "  table_fingerprint TEXT NOT NULL)")
    row = conn.execute("SELECT schema_version, table_fingerprint FROM meta WHERE rowid = 1").fetchone()
    current = (SCHEMA_VERSION, _table_fingerprint())
    if row is None:
        conn.execute("INSERT INTO meta (rowid, schema_version, table_fingerprint) VALUES (1, ?, ?)", current)
        conn.commit()
    elif tuple(row) != current:
        # A different table, or a different way of deriving a vector: every stored row is unusable.
        conn.execute("DELETE FROM passages")
        conn.execute("UPDATE meta SET schema_version = ?, table_fingerprint = ? WHERE rowid = 1", current)
        conn.commit()
    return conn


def _live_text(path: "str | None" = None) -> dict:
    """{record_id: (record, searchable_text)} for everything recall is allowed to surface.

    The text is `index._record_text` — the same projection the keyword path uses, so both paths see one
    record the same way, and a harness-inserted block is excluded from meaning-based reach exactly as it is
    excluded from keyword reach. A record whose projection is empty is skipped outright: it has no meaning
    to match, and an all-zero vector would otherwise be a row that matches everything equally.
    """
    out = {}
    for record in forget.live_records(path):
        if not isinstance(record, dict):
            continue
        rid = record.get("id")
        if not rid:
            continue                          # a legacy record with no id cannot be tracked or re-found
        text = index._record_text(record)
        if text.strip():
            out[rid] = (record, text)
    return out


def _quantize(vectors):
    """Unit vectors as int8 rows plus a per-row scale — a quarter of the size, no measurable ranking cost."""
    import numpy

    scales = numpy.abs(vectors).max(axis=1)
    scales[scales == 0] = 1.0
    rows = numpy.round(vectors / scales[:, None] * 127).astype(numpy.int8)
    return rows, scales


def _reconcile(conn: sqlite3.Connection, live: dict) -> dict:
    """Bring the open store in line with `live`: drop departed records, embed newly-arrived ones.

    The one place reconciliation happens, so the answer a question gets and the answer a maintenance run
    reports can never be computed from differently-reconciled stores. Deletions are applied and committed
    before any embedding, so a record that left is gone even if embedding then fails.
    """
    stored = {row[0] for row in conn.execute("SELECT DISTINCT record_id FROM passages")}
    gone = stored - live.keys()
    if gone:
        conn.executemany("DELETE FROM passages WHERE record_id = ?", [(rid,) for rid in gone])
        conn.commit()
    fresh = [rid for rid in live if rid not in stored]
    if fresh:
        texts, owners, ordinals = [], [], []
        for rid in fresh:
            for ordinal, passage in enumerate(passages(live[rid][1])):
                texts.append(passage)
                owners.append(rid)
                ordinals.append(ordinal)
        if texts:
            rows, scales = _quantize(embed.embed_many(texts))
            conn.executemany(
                "INSERT OR REPLACE INTO passages (record_id, ordinal, vec, scale) VALUES (?, ?, ?, ?)",
                [(owners[i], ordinals[i], rows[i].tobytes(), float(scales[i])) for i in range(len(texts))])
    conn.commit()
    return {"embedded": len(fresh), "dropped": len(gone), "total": len(live)}


def sync(*, ledger_file: "str | None" = None, store_file: "str | None" = None) -> dict:
    """Reconcile the store against the ledger: embed what is new, drop what is gone.

    Returns plain counts. Raises `embed.TableUnavailable` when the word table cannot be trusted — the
    caller turns that into a stated reason rather than an empty answer.
    """
    live = _live_text(ledger_file)
    conn = _connect(store_path(store_file))
    try:
        return _reconcile(conn, live)
    finally:
        conn.close()


def coverage(*, ledger_file: "str | None" = None, store_file: "str | None" = None) -> dict:
    """How much of the store meaning-based recall can actually see.

    This is what lets an empty answer say which kind of empty it is: nothing stored to search, or searched
    and genuinely nothing close.
    """
    path = store_path(store_file)
    records = len(_live_text(ledger_file))
    if not os.path.exists(path):
        return {"records_embedded": 0, "records": records, "passages": 0}
    conn = _connect(path)
    try:
        held = conn.execute("SELECT COUNT(*), COUNT(DISTINCT record_id) FROM passages").fetchone()
    finally:
        conn.close()
    return {"records_embedded": int(held[1]), "records": records, "passages": int(held[0])}


def search(query: str, *, limit: int = DEFAULT_LIMIT, ledger_file: "str | None" = None,
           store_file: "str | None" = None) -> dict:
    """The records closest in meaning to `query`, best first, each with how close it was.

    Reconciles first, so the answer covers everything currently in the ledger and nothing that has left it.
    Every returned record comes from the live read of the ledger performed in that same pass, and a record
    scores as well as its best passage.
    """
    import numpy

    live = _live_text(ledger_file)
    conn = _connect(store_path(store_file))
    try:
        reconciled = _reconcile(conn, live)
        rows = conn.execute("SELECT record_id, vec, scale, ordinal FROM passages").fetchall()
    finally:
        conn.close()

    # Only rows still present in the live read are considered — the second erasure guarantee.
    usable = [row for row in rows if row[0] in live]
    if not usable:
        return {"records": [], "scores": [], "searched": 0, "embedded": reconciled["embedded"]}

    question = embed.embed(query)
    best: dict = {}
    for start in range(0, len(usable), SCORE_BLOCK):
        block = usable[start:start + SCORE_BLOCK]
        matrix = numpy.frombuffer(b"".join(row[1] for row in block), dtype=numpy.int8)
        matrix = matrix.reshape(len(block), question.shape[0]).astype(numpy.float32)
        scales = numpy.fromiter((row[2] for row in block), dtype=numpy.float32, count=len(block))
        # The stored row is a unit vector scaled into int8; restoring the scale restores the cosine.
        scores = (matrix @ question) * (scales / 127.0)
        for offset, score in enumerate(scores):
            rid, ordinal = block[offset][0], block[offset][3]
            if score > best.get(rid, (-2.0, 0))[0]:
                best[rid] = (float(score), int(ordinal))

    ranked = sorted(best.items(), key=lambda pair: -pair[1][0])[:max(int(limit), 1)]
    records, scores_out, matched = [], [], []
    for rid, (score, ordinal) in ranked:
        if score < MIN_SIMILARITY:
            break                              # sorted best-first, so everything after this is further away
        records.append(live[rid][0])
        scores_out.append(round(score, 4))
        # The passage that actually matched — recomputed from the same text, never stored twice. Without it
        # a caller sees a record's opening and judges relevance on words that had nothing to do with the hit.
        found = passages(live[rid][1])
        matched.append(found[ordinal] if ordinal < len(found) else "")
    return {"records": records, "scores": scores_out, "passages": matched,
            "searched": len(usable), "embedded": reconciled["embedded"]}
