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
have gone are dropped, deletions first.

WHAT ERASURE GUARANTEES HERE, EXACTLY. An erased record can never be returned by this operation, from the
moment its text leaves the ledger — the answer is assembled only from records read live in that same pass,
so a stale row cannot reach a caller even before it is deleted. What is NOT instant is the row itself: the
vectors of an erased record stay in this local file until the next meaning-based question, which is when the
reconcile runs. What lingers is a lossy numeric derivative, not the wording — this store holds no text at
all — it lives only in the gitignored memory directory, and it is unreachable by every read path. Said
plainly rather than claimed away: the erasure paths rebuild the keyword index in the same pass, and they do
not know this file exists.

WHY BRUTE FORCE. Similarity here is one matrix multiply against a few tens of thousands of rows — a handful
of milliseconds, scored in blocks so peak memory stays flat however large the store grows. A vector index
would add a compiled SQLite extension, which has no source fallback on several platforms this engine
supports, to save time that is not being spent.
"""

import os
import re
import sqlite3
import sys

# Make the package parent (.engine/tools) importable so `from memory import …` resolves when this file is run
# directly as a script (the demo). Imported as `memory.semantic.store`, the parent is already on sys.path, so
# this is a guarded no-op. Three levels up rather than two: this package is nested inside `memory`.
_PARENT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from memory import forget, index, ledger  # noqa: E402
from memory.semantic import embed  # noqa: E402

STORE_FILENAME = "vectors.sqlite3"

# Bumped whenever what the store holds, or how a vector is derived, changes — so an older store is rebuilt
# rather than silently mixed with vectors made a different way.
SCHEMA_VERSION = 2

# The PROJECTION the stored rows were split and digested under: `index._record_text` (what a record's
# searchable text is) and `passages` below (how it is split: PASSAGE_CHARS, MAX_PASSAGES). Bump it when either
# changes. A qualified reconcile then discards and re-embeds; a READ-ONLY reader (`search_read_only`) refuses
# a store stamped with another projection, because a stored ordinal would address a different sentence and
# the quoted passage is this tool's whole offer. A per-row text digest is checked as well (the row-level form
# of the same guard), so a store stamped current but rebuilt by other code still cannot quote the wrong line.
PROJECTION_VERSION = 1

# How many records appended since the store's receipt a READ-ONLY answer embeds in memory for one question
# before it stops and says so (completeness incomplete, the not-caught-up note). Measured at about 0.2 ms per
# record on the shipped table, so the bound is under half a second of work, and a session that may not write
# the store is never asked to wait longer than that for a meaning answer.
READ_ONLY_TAIL_LIMIT = 2000

DEFAULT_LIMIT = 10

# Passage length, in characters. Short enough that one passage is about one thing, long enough to carry a
# whole thought — sentence boundaries are preferred and this is the cap when a sentence runs long.
PASSAGE_CHARS = 220

# The most passages taken from any one record, so a single enormous record cannot dominate the store.
MAX_PASSAGES = 24

# Rows scored per block. Keeps peak memory flat: the stored rows are one byte per dimension and only a
# block is widened to float at a time.
SCORE_BLOCK = 8192

# The floor below which a passage is not offered at all. It cuts obvious noise; it does NOT decide relevance,
# and no number could.
#
# THE SCORE RANKS, IT DOES NOT VERDICT — measured, not assumed. On real captured history a question whose
# answer was genuinely present scored 0.478, while a deliberately irrelevant question about a broken coffee
# machine still peaked at 0.402 by sharing the word "broken". Meanwhile a true zero-overlap paraphrase —
# "did we consider running it on a timer" against "we ruled out a cron job and hooked the calendar" — scored
# only 0.244, because sharing no vocabulary is exactly the case this exists for and exactly the case that
# scores low. The relevant and irrelevant distributions therefore OVERLAP: any threshold high enough to
# exclude the coffee machine also excludes the cron job.
#
# So the floor is set low, where plainly unrelated text sits (sourdough bread against an engine ledger scores
# 0.077), and the judgement is left where it can actually be made: the caller receives the passage that
# matched, and reads it. That is not a shortcut — it is the same division of labour the rest of recall uses,
# where meaning is supplied by the reading model rather than by the retrieval process.
#
# The figure is computed here, used to order results and to apply this floor, and deliberately NOT relayed to
# the caller by the transport above this module. It ranks within one answer; it does not compare across
# questions, and a number printed beside a result is read as confidence no matter what the words around it
# say. The ordering and the passage carry everything a caller can actually act on.
MIN_SIMILARITY = 0.15

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
        # A single sentence can exceed the cap on its own — minified code, a pasted table, prose with no
        # terminator at all. Splitting it is what keeps the cap real: an un-split run averages into the same
        # blurred middle that passages exist to avoid, and it would silently be the whole record.
        while len(piece) > PASSAGE_CHARS:
            if current:
                out.append(current)
                current = ""
            out.append(piece[:PASSAGE_CHARS])
            piece = piece[PASSAGE_CHARS:]
        if not piece:
            continue
        if current and len(current) + 1 + len(piece) > PASSAGE_CHARS:
            out.append(current)
            current = piece
        else:
            current = f"{current} {piece}".strip()
    if current:
        out.append(current)
    return out[:MAX_PASSAGES]


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


_CREATE_PASSAGES = ("CREATE TABLE IF NOT EXISTS passages ("
                    "  record_id TEXT NOT NULL,"
                    "  ordinal INTEGER NOT NULL,"
                    "  vec BLOB NOT NULL,"
                    "  scale REAL NOT NULL,"
                    # A digest of the text these vectors were made from. A record can be rewritten in place
                    # keeping its id — a ledger migration does exactly that — and without this the old vectors
                    # survive, so the record answers for wording it no longer contains and the passage recovered
                    # for the caller comes back EMPTY. That would strip the one piece of evidence this whole
                    # design asks the caller to judge on. The read-only search checks it per row too.
                    "  text_digest TEXT NOT NULL,"
                    "  PRIMARY KEY (record_id, ordinal))")
_CREATE_META = ("CREATE TABLE IF NOT EXISTS meta ("
                "  rowid INTEGER PRIMARY KEY CHECK (rowid = 1),"
                "  schema_version INTEGER NOT NULL,"
                "  table_fingerprint TEXT NOT NULL)")
# THE RECEIPT, and the projection stamp, widened onto `meta` in place (ALTER TABLE ADD COLUMN) so a store built
# before they existed keeps every embedding it holds. `covered_*` name the ledger identity the live set was
# derived under when the store was last reconciled — its content generation, its index epoch, and the byte
# length of the file the live set was read from — captured at DERIVATION (`_live_snapshot`), never at commit,
# because a sibling can append while embedding runs. A reader that may not reconcile trusts the rows for
# records whose line sits below `covered_length` and embeds the rest itself; a receipt whose generation or
# epoch differ from the ledger's means the rows cannot be trusted by position at all.
_META_COLUMNS = (("covered_generation", "INTEGER"), ("covered_epoch", "INTEGER"),
                 ("covered_length", "INTEGER"), ("projection_version", "INTEGER"))


def _open(path: str) -> sqlite3.Connection:
    """Connect, and nothing else: no table is created, no version compared, no row dropped. The half of the
    old `_connect` that writes nothing, kept separate so a read-only reader has a door that is not a writer."""
    return sqlite3.connect(path)


def _open_read_only(path: str) -> sqlite3.Connection:
    """A connection SQLite itself refuses to write through (`mode=ro`): an INSERT, an UPDATE or a DROP raises
    inside the library, so nothing on the read-only path can migrate, reconcile or empty the store — the
    #1151 hazard closed by construction rather than by discipline. Raises when the file is absent; the
    read-only search maps that, it never opens a store into existence."""
    from pathlib import Path

    return sqlite3.connect(Path(os.path.abspath(path)).as_uri() + "?mode=ro", uri=True)


def _migrate(conn) -> sqlite3.Connection:
    """Bring an open store - or, given a path, a store that does not exist yet - to this code's shape. The ONE
    writer of DDL in this module, registered as
    `semantic-store-migrate` (it was `semantic-store-connect` while `_connect` did both jobs), and deliberately
    outside the degraded-allowed set: a session that may not write is refused here, before anything is dropped.

    Creates what is missing, widens `meta` in place for the receipt and projection columns, and discards every
    stored vector when the word table, the schema or the projection that made them changed. DROP, never
    DELETE, for the passages: a version bump can change the table's SHAPE, and `CREATE TABLE IF NOT EXISTS` is
    a no-op against a table that already exists — so emptying the rows would leave an OLD-shaped table stamped
    as current, and the next query would fail on a missing column, for good, with no path back. Dropping it
    forces the create to run for real. The receipt goes with the rows it vouched for."""
    opened = None
    if not isinstance(conn, sqlite3.Connection):
        opened = conn = _open(conn)        # creating the file is the store's first write: it happens here
    try:
        conn.execute(_CREATE_PASSAGES)
        conn.execute(_CREATE_META)
        present = {row[1] for row in conn.execute("PRAGMA table_info(meta)").fetchall()}
        for name, kind in _META_COLUMNS:
            if name not in present:
                conn.execute(f"ALTER TABLE meta ADD COLUMN {name} {kind}")
        row = conn.execute("SELECT schema_version, table_fingerprint, projection_version FROM meta "
                           "WHERE rowid = 1").fetchone()
        current = (SCHEMA_VERSION, _table_fingerprint())
        if row is None:
            conn.execute("INSERT INTO meta (rowid, schema_version, table_fingerprint, projection_version) "
                         "VALUES (1, ?, ?, ?)", (*current, PROJECTION_VERSION))
        elif tuple(row[:2]) != current or (row[2] is not None and row[2] != PROJECTION_VERSION):
            # A different table, a different way of deriving a vector, or a different way of splitting a
            # record: every stored row is unusable.
            conn.execute("DROP TABLE IF EXISTS passages")
            conn.execute(_CREATE_PASSAGES)
            conn.execute("UPDATE meta SET schema_version = ?, table_fingerprint = ?, projection_version = ?, "
                         "covered_generation = NULL, covered_epoch = NULL, covered_length = NULL WHERE rowid = 1",
                         (*current, PROJECTION_VERSION))
        elif row[2] is None:
            # A store from before the projection was stamped. Its rows were split the one way there has ever
            # been, so it is stamped current without touching them — the whole point of widening in place.
            conn.execute("UPDATE meta SET projection_version = ? WHERE rowid = 1", (PROJECTION_VERSION,))
        conn.commit()
    except BaseException:
        if opened is not None:
            opened.close()
        raise
    return conn


def _connect(path: str) -> sqlite3.Connection:
    """The writer's door: open, then migrate. A session that may not write is refused at `_migrate`, and the
    connection it would have received is closed rather than leaked. A store that does not exist yet is
    CREATED by the writer, not by the open: `sqlite3.connect` on a missing path leaves an empty file behind,
    so the path goes to `_migrate` and the file comes into being only once the writer is admitted. A session
    that may not write leaves no store behind, not even an empty one (the reproduction harness asserts it)."""
    if not os.path.exists(path):
        return _migrate(path)
    conn = _open(path)
    try:
        _migrate(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def _stored_fingerprint(conn: sqlite3.Connection):
    """`(schema_version, table_fingerprint, projection_version)` as stamped, or None when the store has no
    readable `meta` — a file that was never migrated, or is not a store at all."""
    try:
        row = conn.execute("SELECT schema_version, table_fingerprint, projection_version FROM meta "
                           "WHERE rowid = 1").fetchone()
    except sqlite3.Error:
        return None
    return tuple(row) if row else None


def _stored_receipt(conn: sqlite3.Connection):
    """`(covered_generation, covered_epoch, covered_length)`, or None when any part is unset."""
    try:
        row = conn.execute("SELECT covered_generation, covered_epoch, covered_length FROM meta "
                           "WHERE rowid = 1").fetchone()
    except sqlite3.Error:
        return None
    if not row or any(v is None for v in row):
        return None
    return tuple(int(v) for v in row)


# The last derivation, keyed by everything that can invalidate it. `search` is the only caller and this
# module is loaded inside the long-lived recall server, so a session that asks several meaning-based questions
# in a row re-derives once instead of once per question.
#
# WHAT IT COSTS, stated because a comment further down in `search` warns against exactly this shape: it says
# materialising the row set whole "would make peak memory linear in store size". The distinction that makes
# this a different trade is PEAK versus RESIDENT. `_live_text` is derived on every query regardless and held
# for that query's duration, so the peak is unchanged; what changes is that the derivation now survives
# BETWEEN queries in the long-lived recall server — roughly one ledger's worth, about 30 MB on a 30 MB store,
# never released. That is a real cost and it is the one being bought: 334 ms to 118 ms on a repeat question.
#
# It is also NOT the durable byte-cursor the design review named. A cursor kept in the vector store would
# survive a restart; this does not, so the first query in each process still pays full price, and `_reconcile`
# still re-checks its passages per query. What is fixed is the repeat cost inside one session, which is where
# a recall workflow actually asks several questions in a row.
#
# WHY THESE FOUR KEYS ARE SUFFICIENT, which is the whole safety argument. A record can only enter the live set
# by being APPENDED (the file grows, so size changes) and can only leave it by being withheld (which bumps the
# index epoch), by an erasure compaction, or by the operator's re-scrub (both of which rewrite the file, and
# the first bumps the generation). Size plus mtime catches every append including a same-size rewrite; the two
# counters catch every removal. Miss any of them and this would serve a withheld record from cache, so the
# cache is deliberately dropped on ANY doubt rather than refreshed cleverly.
_LIVE_CACHE: dict = {}


def _live_key(path: "str | None") -> tuple:
    """The identity of the ledger's current content, or a never-matching key when it cannot be read."""
    from memory import ledger as _ledger
    target = path or _ledger.ledger_path()
    try:
        stat = os.stat(target)
        return (target, stat.st_size, stat.st_mtime_ns,
                _ledger.generation(for_path=target), _ledger.index_epoch(for_path=target))
    except Exception:  # noqa: BLE001 — unreadable: never reuse a cache against a ledger we cannot identify
        return (target, None, None, None, None)


def _live_snapshot(path: "str | None" = None) -> tuple:
    """`(live, key)`: {record_id: (record, searchable_text, ledger_position)} for everything recall is allowed
    to surface, and the ledger identity (`_live_key`) it was derived under.

    The key is what a reconcile's receipt names, and it is captured HERE, at derivation — never at commit
    time, because a sibling session can append while embedding runs, and a receipt naming the newer file
    would claim rows for records the reconcile never saw. The position lets a read-only reader tell which
    records the receipt covers (their line sits below the covered byte length) from those appended since.
    Cached exactly as `_live_text` documents.
    """
    key = _live_key(path)
    if key[1] is not None and _LIVE_CACHE.get("key") == key:
        return _LIVE_CACHE["value"], key
    out = {}
    for position, length, raw, record in forget.live_records(path, with_positions=True):
        if not isinstance(record, dict):
            continue
        rid = record.get("id")
        if not rid:
            continue                          # a legacy record with no id cannot be tracked or re-found
        text = index._record_text(record)
        if text.strip():
            out[rid] = (record, text, position)
    if key[1] is not None:
        _LIVE_CACHE.clear()
        _LIVE_CACHE.update({"key": key, "value": out})
    return out, key


def _live_text(path: "str | None" = None) -> dict:
    """{record_id: (record, searchable_text, ledger_position)} for everything recall is allowed to surface.

    The text is `index._record_text` — the same projection the keyword path uses, so both paths see one
    record the same way, and a harness-inserted block is excluded from meaning-based reach exactly as it is
    excluded from keyword reach. A record whose projection is empty is skipped outright: it has no meaning
    to match, and an all-zero vector would otherwise be a row that matches everything equally.

    CACHED on the ledger's identity (see `_live_key`), because this is a full pass over the whole store and it
    ran on EVERY meaning-based question — measured at 215 ms of a 334 ms query on a 30 MB store, with a further
    48 ms re-hashing every record behind it. A miss re-derives from scratch; there is no partial update, so a
    stale cache cannot survive any change that could remove a record from recall.
    """
    return _live_snapshot(path)[0]


def _digest(text: str) -> str:
    """A short fingerprint of a record's searchable text, so a rewrite in place is noticed."""
    import hashlib

    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:32]


def _quantize(vectors):
    """Unit vectors as int8 rows plus a per-row scale — a quarter of the size, no measurable ranking cost."""
    import numpy

    scales = numpy.abs(vectors).max(axis=1)
    scales[scales == 0] = 1.0
    rows = numpy.round(vectors / scales[:, None] * 127).astype(numpy.int8)
    return rows, scales


def _reconcile(conn: sqlite3.Connection, live: dict, derived_under: "tuple | None" = None) -> dict:
    """Bring the open store in line with `live`: drop departed records, embed newly-arrived ones.

    The one place reconciliation happens, so the answer a question gets and the answer a maintenance run
    reports can never be computed from differently-reconciled stores. Deletions are applied and committed
    before any embedding, so a record that left is gone even if embedding then fails — that ordering is a
    stated guarantee and it stands.

    `derived_under` is the `_live_key` the live set was derived under (`_live_snapshot`). It is written as the
    store's RECEIPT in the same commit as the insertions, so a store either carries a receipt for a live set it
    fully holds, or the previous receipt: an embedding failure leaves the receipt where it was. The receipt is
    what lets a session that may not write answer from this store (`search_read_only`).
    """
    stored = {}
    for rid, digest in conn.execute("SELECT DISTINCT record_id, text_digest FROM passages"):
        stored[rid] = digest
    gone = set(stored) - live.keys()
    # A record whose text changed under the same id is as stale as one that left: its rows are dropped and
    # rebuilt rather than kept, so no vector ever outlives the wording it was made from.
    changed = {rid for rid in live if rid in stored and stored[rid] != _digest(live[rid][1])}
    stale = gone | changed
    if stale:
        conn.executemany("DELETE FROM passages WHERE record_id = ?", [(rid,) for rid in stale])
        conn.commit()
    fresh = [rid for rid in live if rid not in stored or rid in changed]
    if fresh:
        texts, owners, ordinals, digests = [], [], [], []
        for rid in fresh:
            digest = _digest(live[rid][1])
            for ordinal, passage in enumerate(passages(live[rid][1])):
                texts.append(passage)
                owners.append(rid)
                ordinals.append(ordinal)
                digests.append(digest)
        if texts:
            rows, scales = _quantize(embed.embed_many(texts))
            conn.executemany(
                "INSERT OR REPLACE INTO passages (record_id, ordinal, vec, scale, text_digest) "
                "VALUES (?, ?, ?, ?, ?)",
                [(owners[i], ordinals[i], rows[i].tobytes(), float(scales[i]), digests[i])
                 for i in range(len(texts))])
    if derived_under is not None and derived_under[1] is not None:
        conn.execute("UPDATE meta SET covered_generation = ?, covered_epoch = ?, covered_length = ?, "
                     "projection_version = ? WHERE rowid = 1",
                     (int(derived_under[3]), int(derived_under[4]), int(derived_under[1]), PROJECTION_VERSION))
    conn.commit()
    return {"embedded": len(fresh), "dropped": len(gone), "total": len(live)}


def sync(*, ledger_file: "str | None" = None, store_file: "str | None" = None) -> dict:
    """Reconcile the store against the ledger: embed what is new, drop what is gone.

    Returns plain counts. Raises `embed.TableUnavailable` when the word table cannot be trusted — the
    caller turns that into a stated reason rather than an empty answer.
    """
    live, derived_under = _live_snapshot(ledger_file)
    conn = _connect(store_path(store_file))
    try:
        return _reconcile(conn, live, derived_under)
    finally:
        conn.close()


def _best_by_record(cursor, live: dict, question, *, digests: "dict | None" = None, dropped: "set | None" = None):
    """(best, scanned): the closest passage of each live record, and how many passages were compared.

    Streams the cursor in blocks so peak memory is bounded by SCORE_BLOCK rather than by the store's size,
    and considers only rows still present in the live read — the second erasure guarantee, applied before a
    departed record's vector is ever scored.

    With `digests` (record id -> the digest THIS code computes for the record's text) a row whose stored
    `text_digest` (the cursor's fifth column) differs is skipped and its record id added to `dropped`: the
    row was embedded from a different projection or a different wording, so its ordinal cannot be trusted to
    name a sentence this code would quote. The read-only search uses this; a reconciled search does not need
    it, because reconcile already re-embedded every row whose digest moved.
    """
    import numpy

    best: dict = {}
    scanned = 0
    width = question.shape[0]
    while True:
        rows = cursor.fetchmany(SCORE_BLOCK)
        if not rows:
            break                       # only an exhausted cursor returns nothing
        block = []
        for row in rows:
            if row[0] not in live:
                continue
            if digests is not None and row[4] != digests.get(row[0]):
                if dropped is not None:
                    dropped.add(row[0])
                continue
            block.append(row)
        if not block:
            continue                    # a block that was entirely departed records; more may follow
        matrix = numpy.frombuffer(b"".join(row[1] for row in block), dtype=numpy.int8)
        matrix = matrix.reshape(len(block), width).astype(numpy.float32)
        scales = numpy.fromiter((row[2] for row in block), dtype=numpy.float32, count=len(block))
        # The stored row is a unit vector scaled into int8; restoring the scale restores the cosine.
        scores = (matrix @ question) * (scales / 127.0)
        for offset, score in enumerate(scores):
            rid, ordinal = block[offset][0], block[offset][3]
            if score > best.get(rid, (-2.0, 0))[0]:
                best[rid] = (float(score), int(ordinal))
        scanned += len(block)
    return best, scanned


def _unavailable(exc) -> dict:
    """The no-trustworthy-answer result, saying WHICH kind of unavailable it is.

    Two very different things reach it, and collapsing them was a real objection: a qualification refusal is
    ordinary and resolves itself, while a corrupt store or a dead embedding backend is a fault someone has to
    know about. Reporting the second as "not qualified yet" would send the operator to wait for something
    that is never going to fix it. The exception CLASS name is carried, never its message, which can embed
    paths or record text.
    """
    # Keyed on the refusal's own words, NOT on the exception class. `MutationAuthorityError` is the authority
    # layer's generic type: it also carries "advisory locking is unavailable", "the lock is not a regular
    # file", cardinality overruns and source-binding failures. Classifying those as "not qualified yet" would
    # tell the operator to wait for a session start that is never going to fix it — the exact outcome this
    # function exists to prevent, and the review caught it doing so. `degraded_refusal` is the one place that
    # phrase is minted, so matching it identifies a qualification refusal and nothing else.
    refused = "qualified to write memory" in str(exc)
    return {"records": [], "scores": [], "passages": [], "searched": 0, "embedded": 0,
            "unavailable": "not-qualified" if refused else "store-fault",
            "fault_class": None if refused else type(exc).__name__}


def search(query: str, *, limit: int = DEFAULT_LIMIT, ledger_file: "str | None" = None,
           store_file: "str | None" = None) -> dict:
    """The records closest in meaning to `query`, best first, each with how close it was.

    Reconciles first, so the answer covers everything currently in the ledger and nothing that has left it.
    Every returned record comes from the live read of the ledger performed in that same pass, and a record
    scores as well as its best passage.

    Carries `unavailable`: True when this session could not open or reconcile the store and therefore has no
    trustworthy answer to give — which is NOT the same as having searched and found nothing, and a caller
    must never report it as such.
    """
    live, derived_under = _live_snapshot(ledger_file)
    try:
        conn = _connect(store_path(store_file))
    except Exception as exc:  # noqa: BLE001 — an unqualified session may not open-or-migrate the store
        return _unavailable(exc)
    try:
        try:
            reconciled = _reconcile(conn, live, derived_under)
        except Exception as exc:  # noqa: BLE001 — without the repair, this store's answer cannot be trusted
            # An unqualified session may not write embeddings: the passage store holds record text, so
            # rewriting it is a way to put invented content in front of recall without touching the ledger.
            #
            # An earlier attempt answered anyway from whatever was already embedded, and the repair review
            # showed two things wrong with that. A record REWRITTEN under the same id is still in `live`, so
            # it is not filtered out — it scores on its stale vector, and the evidence passage is then read
            # from the NEW text at the OLD offset: a hit for a question the record has nothing to do with,
            # carrying a quotation that had nothing to do with the match, when the passage BEING the evidence
            # is this tool's whole offer. And the `degraded` flag meant to carry that caveat was dropped by
            # both return paths, so the caller could not tell — and told the operator their project memory
            # was empty when it was not.
            #
            # So an unreconciled store reports itself UNAVAILABLE instead of answering. Keyword recall is
            # unaffected and still covers everything; the next qualified session reconciles and this returns.
            return _unavailable(exc)
        # Streamed in blocks, never fetchall(): the row set grows with the store, and materialising it whole
        # would make peak memory linear in store size — the same unbounded read the keyword path already fixed.
        best, scanned = _best_by_record(
            conn.execute("SELECT record_id, vec, scale, ordinal FROM passages"), live, embed.embed(query))
    finally:
        conn.close()

    # Every key the populated return carries must be present in the empty one too. A caller unpacks this dict
    # positionally, and a deployed repo starts with an empty ledger — so the shape a fresh project sees FIRST
    # is the empty one, and an omitted key is a crash on the first question ever asked. `_ranked` keeps them
    # the same shape by construction.
    return {**_ranked(best, live, limit), "searched": scanned, "embedded": reconciled["embedded"],
            "unavailable": False}


def _ranked(best: dict, live: dict, limit) -> dict:
    """The answer's records, scores and passages from the per-record bests, best first, above the floor."""
    records, scores_out, matched = [], [], []
    for rid, (score, ordinal) in sorted(best.items(), key=lambda pair: -pair[1][0])[:max(int(limit), 1)]:
        if score < MIN_SIMILARITY:
            break                              # sorted best-first, so everything after this is further away
        # The passage that actually matched — recomputed from the same text, never stored twice. Without it
        # a caller sees a record's opening and judges relevance on words that had nothing to do with the hit.
        found = passages(live[rid][1])
        if ordinal >= len(found):
            continue        # no recoverable passage means no evidence, and evidence is the whole offer
        records.append(live[rid][0])
        scores_out.append(round(score, 4))
        matched.append(found[ordinal])
    return {"records": records, "scores": scores_out, "passages": matched}


def _score_in_memory(live: dict, ids, question) -> tuple:
    """(best, scanned) for `ids`, embedded for this one question and never stored — the tail a read-only
    answer covers itself. The same passages, the same unit vectors and the same cosine as the stored rows,
    so a record found this way ranks against a stored one on equal terms."""
    texts, owners, ordinals = [], [], []
    for rid in ids:
        for ordinal, passage in enumerate(passages(live[rid][1])):
            texts.append(passage)
            owners.append(rid)
            ordinals.append(ordinal)
    best: dict = {}
    if not texts:
        return best, 0
    scores = embed.embed_many(texts) @ question
    for i, score in enumerate(scores):
        rid = owners[i]
        if float(score) > best.get(rid, (-2.0, 0))[0]:
            best[rid] = (float(score), ordinals[i])
    return best, len(texts)


def _read_only_unavailable(kind: str, fault_class: "str | None" = None) -> dict:
    return {"records": [], "scores": [], "passages": [], "searched": 0, "embedded": 0,
            "unavailable": kind, "fault_class": fault_class, "complete": False, "tail": 0, "dropped": 0}


def search_read_only(query: str, *, limit: int = DEFAULT_LIMIT, ledger_file: "str | None" = None,
                     store_file: "str | None" = None) -> dict:
    """Meaning-based recall for a session that MAY NOT WRITE the store: a moved activation, no context
    installed, a session not yet qualified. Registered as the read entry `read-semantic-store`.

    TOTAL: every way this can fail is an answer, never an exception — the path that carries the program's
    restoration claim must not reappear as a tool fault. It opens the store read-only (SQLite refuses every
    write through that connection), runs no migration and no reconcile, and answers from three sources that
    are each safe on their own terms:

      * the RECEIPT-COVERED rows — trusted only when the store's schema, word table and projection are this
        code's own and the receipt names the ledger's current generation and index epoch, and then only for
        records whose line sits below the covered byte length, and only where the row's stored text digest
        equals the digest this code computes (a row that fails that is DROPPED — never scored, never quoted —
        and its record joins the tail below, so it is searched from its live text instead);
      * the TAIL — every live record the receipt does not cover, embedded in memory for this one question,
        up to READ_ONLY_TAIL_LIMIT; beyond it the tail is left unsearched and the answer says so;
      * this code's own LIVE READ of the ledger, which every returned record and quoted passage comes from.

    A file that is not a readable store (absent, never migrated, or corrupt) is simply a store with nothing
    to trust: the live set is embedded here when it fits the bound, and otherwise declined - nothing is ever
    quoted from such a file, and nothing is ever written to it.

    `unavailable` is False for an answer, `newer-code` when the store was rebuilt by another schema, table
    or projection (a restart clears it), `not-reconciled` when the rows cannot be trusted by position (no
    store, no receipt, a receipt naming another generation or epoch) AND the live set is too large to embed
    here, and `store-fault` for anything else, with the exception's class name and never its text. `complete`
    is True when nothing was left unsearched; `tail` and `dropped` are the counts.
    """
    try:
        live, key = _live_snapshot(ledger_file)
        question = embed.embed(query)
        path = store_path(store_file)
        best: dict = {}
        scanned = 0
        dropped: set = set()
        covered_ids: set = set()
        trusted = False
        if os.path.exists(path):
            conn = _open_read_only(path)
            try:
                stamped = _stored_fingerprint(conn)
                if stamped is not None:
                    if (tuple(stamped[:2]) != (SCHEMA_VERSION, _table_fingerprint())
                            or (stamped[2] is not None and stamped[2] != PROJECTION_VERSION)):
                        return _read_only_unavailable("newer-code")
                    receipt = _stored_receipt(conn)
                    if (receipt is not None and stamped[2] == PROJECTION_VERSION
                            and key[1] is not None and receipt[0] == key[3] and receipt[1] == key[4]):
                        covered = {rid: v for rid, v in live.items() if v[2] < receipt[2]}
                        expected = {rid: _digest(v[1]) for rid, v in covered.items()}
                        best, scanned = _best_by_record(
                            conn.execute("SELECT record_id, vec, scale, ordinal, text_digest FROM passages"),
                            covered, question, digests=expected, dropped=dropped)
                        covered_ids = set(covered) - dropped
                        trusted = True
            finally:
                conn.close()
        tail = [rid for rid in live if rid not in covered_ids]
        complete = True
        if len(tail) > READ_ONLY_TAIL_LIMIT:
            if not trusted:
                return _read_only_unavailable("not-reconciled")
            complete, tail = False, []       # answer from the covered rows and say the rest was not searched
        if tail:
            fresh_best, fresh_scanned = _score_in_memory(live, tail, question)
            best.update(fresh_best)
            scanned += fresh_scanned
        return {**_ranked(best, live, limit), "searched": scanned, "embedded": 0, "unavailable": False,
                "fault_class": None, "complete": complete, "tail": len(tail), "dropped": len(dropped)}
    except Exception as exc:  # noqa: BLE001 — total by contract: a fault is an answer, never a tool crash
        return _read_only_unavailable("store-fault", type(exc).__name__)


# --- Operator demonstration -------------------------------------------------------------------------------
# A runnable proof, on a throwaway practice store in a temp folder — never the real one:
#     uv run --directory .engine --frozen -- python tools/memory/semantic/store.py demo
#
# It plants a note whose wording deliberately shares nothing with the question that should find it, then
# checks BOTH halves. Either half failing exits non-zero: if keyword search finds the note, the case was not
# genuinely word-free and proves nothing; if meaning-based recall misses it, the capability does not work.

_DEMO_NOTE = "We ruled out a cron job and hooked the calendar instead, so nothing polls on a schedule."
_DEMO_QUESTION = "did we consider having it run automatically on a timer"
_DEMO_UNRELATED = "sourdough bread proofs better with rye flour and a longer cold rest"


def _demo() -> int:
    import shutil
    import tempfile
    import time

    from memory import index as _index
    from memory import records as _records
    from memory import recall as _recall

    scratch = tempfile.mkdtemp(prefix="engine-semantic-demo-")
    ledger_file = os.path.join(scratch, "ledger.ndjson")
    index_file = os.path.join(scratch, "index.sqlite3")
    store_file = os.path.join(scratch, "vectors.sqlite3")
    # The same refusal the window reader carries: this writes and prints, so it must never touch the real store.
    _recall.assert_not_live_store(scratch, ledger_file)

    previous = os.environ.get("ENGINE_MEMORY_DIR")
    os.environ["ENGINE_MEMORY_DIR"] = scratch
    try:
        print("Meaning-based recall — finding a note by what it means, not the words you use")
        print("=" * 78)
        print()
        for text in (_DEMO_NOTE, _DEMO_UNRELATED):
            ledger.append({_records.RECORD_ID_KEY: _records.new_record_id(), "ts": int(time.time()),
                           "role": "decision", "tags": [], "text": text}, path=ledger_file)
        _index.rebuild(ledger_file=ledger_file, index_file=index_file)
        print(f'Saved into a practice store:\n  "{_DEMO_NOTE}"\n  "{_DEMO_UNRELATED}"')
        print(f'\nThe question asked later:\n  "{_DEMO_QUESTION}"')
        print("\nIt shares no words with the note — no 'cron', no 'calendar', no 'schedule'.")
        print()

        by_word = _index.search(_DEMO_QUESTION, ledger_file=ledger_file, index_file=index_file)
        word_missed = not by_word.records
        print(f"  looking it up by the WORDS in the question .......... "
              f"{'found nothing' if word_missed else 'FOUND IT — the question was not word-free'}")

        found = search(_DEMO_QUESTION, ledger_file=ledger_file, store_file=store_file)
        hit = bool(found["records"]) and "cron job" in (found["records"][0].get("text") or "")
        print(f"  looking it up by MEANING ............................ "
              f"{'found the note' if hit else 'MISSED IT'}")
        if hit:
            print(f'      it returned the sentence that matched: "{found["passages"][0][:70]}…"')

        noise = search("what is the best way to renew a passport",
                       ledger_file=ledger_file, store_file=store_file)
        kept_out = all("cron job" not in (r.get("text") or "") for r in noise["records"])
        print(f"  an unrelated question does not return the note ...... {'correct' if kept_out else 'WRONG'}")
        print()
        print("  One honest limit, worth seeing before you rely on this. Only two notes are saved here, so")
        print("  the unrelated question above had almost nothing to reach for. On a real store of thousands,")
        print("  an unrelated question often DOES come back with something — the nearest thing present, which")
        print("  is not the same as an answer. That is why every result carries the sentence that matched:")
        print("  reading it is how you tell a real hit from the nearest miss, and there is no score that")
        print("  would tell you instead.")

        ok = word_missed and hit and kept_out
        print()
        if ok:
            print("What this changes for you: until now, asking about something recorded in different words")
            print("found nothing at all — the lookup matched words, so a question phrased your way missed a")
            print("note phrased another way. It can now also search by meaning, and it shows you the sentence")
            print("that matched, so you can see for yourself why it was offered. It shows the sentence rather")
            print("than a confidence figure on purpose: how near two pieces of text are does not reliably say")
            print("whether one answers a question about the other, and a number would suggest otherwise.")
            print("Word-based lookup is unchanged and still the right tool for an exact phrase.")
        else:
            print("Meaning-based recall is NOT working as described above.")
        return 0 if ok else 1
    finally:
        if previous is None:
            os.environ.pop("ENGINE_MEMORY_DIR", None)
        else:
            os.environ["ENGINE_MEMORY_DIR"] = previous
        shutil.rmtree(scratch, ignore_errors=True)


def main(argv: list) -> int:
    cmd = argv[0] if argv else "demo"
    if cmd == "demo":
        return _demo()
    print("usage: store.py demo")
    return 2


try:
    from .. import mutation_authority as _mutation_authority
except ImportError:  # direct CLI
    from memory import mutation_authority as _mutation_authority
_mutation_authority.install_module_guards(globals())


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
