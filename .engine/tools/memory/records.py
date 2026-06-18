"""records.py — the shared record vocabulary for the memory ledger (memory-substrate-sqlite-fts5, slice 4d-ii).

The `kind` strings and provenance keys that more than one memory tool must agree on, in ONE place so they
never drift and no import cycle can form. `consolidate` writes the episodic + marker records; `index` keeps
provenance keys out of the search body; `forget` derives logical retirement from the marker↔batch linkage and
(slice 4c) appends the `reinforcement` access marker + scores demotion from it; `compact` (slice 4d-i) folds those
markers into the carried current-state fields below and `score` reads them back; `rollup` (slice 4d-ii) writes the
gist + supersession markers and `forget` derives the raws' retirement from them. Because all of them need these
names and `consolidate` already imports `index`, defining them here — a leaf that imports nothing from the
`memory` package — lets `index`, `forget`, `score`, `compact`, and `rollup` import them without
`consolidate`→`index`→`forget`→`consolidate` becoming a cycle.

stdlib-only; imports nothing from `memory`.
"""

import uuid

# Record kinds (the `kind` field). `turn-delta` is capture's own (it stays in capture.py); these are the
# shared kinds the reflection (3b) and forgetting (4a+) layers both reference.
EPISODIC_KIND = "episodic"          # an AI-written episodic summary record
MARKER_KIND = "consolidated"        # the in-ledger "this session has been tidied" marker (survives backup)

# Tags.
DEFAULT_EPISODIC_TAG = "episodic"
MARKER_TAG = "consolidated"

# Provenance keys — envelope fields that are NOT human content, so the derived index keeps them OUT of the
# search body (index._NON_BODY_KEYS).
BATCH_KEY = "batch"                 # one id per consolidation pass, stamped on every episodic of that pass AND
                                    # on the pass's marker. It lets `forget` derive, purely from the ledger,
                                    # which episodics a *completed* pass closed — and which are orphans from a
                                    # crashed pass (their batch carries no marker), to logically retire.

# The stable, content-free record id (slice 4b). Minted at capture in each record factory — one per record, on
# every kind (turn-delta, episodic, marker). It is a durable NAME for a record: a uuid hex, so it reveals nothing
# about the gitignored content (content-free) and survives the index rebuild and the future compaction rewrite (it
# rides in the record JSON, not an ephemeral index offset). The derived index keeps it OUT of the search body
# (index._NON_BODY_KEYS): a uuid's hex fragments are real words, exactly the `session_id`/`batch` problem.
RECORD_ID_KEY = "id"

# The reinforcement (access) marker (slice 4c — scored demotion). An append-only ledger record minted each time
# a record is RECALLED: it names, by the reinforced record's stable id, that the record was used. `forget.score`
# folds these into a frecency × role-weight × recency score, demoting an old, unused record in tiers
# (hot → warm → cold → archived); `archived` is excluded from recall but stays resident + recoverable in the
# ledger. A reinforcement marker is pure derivation fuel — non-content provenance — so it carries no `text`/
# `session_id`; `index` keeps its `target` (a uuid hex, the `id`/`batch` problem) OUT of the search body
# (index._NON_BODY_KEYS), and `forget.live_records` drops the marker itself from recall. The live caller that
# appends it on recall is slice 5 (the search server); 4c ships the kind + the appender + the demo only.
REINFORCEMENT_KIND = "reinforcement"   # the `kind` field of an access marker
TARGET_KEY = "target"                  # the reinforced record's RECORD_ID_KEY value (whom the access points at)
REINFORCEMENT_TAG = "reinforcement"    # the marker's tag (kept out of the search body like every tag)

# The carried current-state fields ledger compaction (slice 4d) folds onto a recall record before it prunes that
# record's reinforcement markers. They make a compacted record's demotion score durable WITHOUT keeping the
# folded-away markers: `score` reproduces the pre-compaction score from `FRECENCY_SNAPSHOT_KEY` (the frecency
# value at compaction time) decayed forward from `SNAPSHOT_TS_KEY`, with `LAST_ACCESS_TS_KEY` flooring recency.
# This is legal precisely because frecency is a RECURRENCE on the carried snapshot (score.frecency). `TIER_KEY`
# carries the snapshot-time tier as a legibility field ONLY — the authoritative tier is still RECOMPUTED on read
# from the snapshot (it ages as time passes), so a future reader must never trust the carried `tier` as current.
# `index` keeps `TIER_KEY` (a string: "hot"/"cold"/"archived") OUT of the search body (index._NON_BODY_KEYS); the
# numeric snapshot fields are excluded from the body by type already.
FRECENCY_SNAPSHOT_KEY = "frecency_snapshot"   # float: score.frecency value at compaction time t0
SNAPSHOT_TS_KEY = "snapshot_ts"               # int: t0, the compaction time the snapshot was stamped at
LAST_ACCESS_TS_KEY = "last_access_ts"         # int: max(birth, *accesses) at t0, the recency floor
TIER_KEY = "tier"                             # str: the snapshot-time tier (legibility; recomputed on read)

# The gist roll-up vocabulary (slice 4d-ii). Active forgetting's first move (memory/README) is a SECOND-order
# consolidation: an AI-judged maintenance pass rolls up OLD, low-frecency EPISODIC summaries of one session into a
# compact GIST and LOGICALLY RETIRES the raw episodes (excluded from recall, still resident + fully recoverable —
# Layer-1 never erases; physical erasure is Layer-2/4e, audit-gated). `rollup` writes, in strict order under the
# single-writer lock, the gist → a per-raw `superseded` marker → the closing `rolled-up` marker; `forget` derives
# the raws' retirement from a CLOSED-batch supersession; `compact` (slice 4d-i, extended) folds a closed-batch
# supersession into the carried `SUPERSEDED_BY_KEY` field below and prunes the marker. The gist↔raw link is thus
# carried in the ledger (the marker, then the folded field) and survives the rewrite.
GIST_KIND = "gist"                  # an AI-written gist consolidating several old episodes of one session
GIST_TAG = "gist"                   # surfaces alongside DEFAULT_EPISODIC_TAG so a gist rides episodic recall
ROLLUP_KIND = "rolled-up"           # the closing marker of a roll-up pass — the ONLY kind that CLOSES its batch,
                                    # DISTINCT from MARKER_KIND so a roll-up never spuriously marks a session
                                    # 3b-consolidated (the two closure namespaces never mix — forget._closed_batches
                                    # reads MARKER_KIND, forget._closed_rollup_batches reads ROLLUP_KIND)
SUPERSEDED_KIND = "superseded"      # a per-raw marker: this raw episode's content now lives in a gist. It points at
                                    # the raw by TARGET_KEY (reused — already non-body) and names the gist by
                                    # SUPERSEDED_BY_KEY, and carries the pass's BATCH_KEY. INERT until its batch is
                                    # closed (a `rolled-up` marker landed): only then does it hide its raw, so a
                                    # crash before the closing marker never hides a raw whose gist's pass didn't finish.
SOURCE_IDS_KEY = "source_ids"       # on the gist: the RECORD_ID_KEY values of the raw episodes it consolidates — the
                                    # forward half of the gist↔raw link (a list of uuid hex; kept OUT of the search
                                    # body, index._NON_BODY_KEYS, like every uuid-hex field)
# The carried current-state field compaction folds a CLOSED-batch supersession into, before it prunes the marker:
# the raw episode carries the gist id it was superseded by, so `forget.live_records` still retires it after the
# marker is gone. Minted ONLY across a closed gate, so its mere presence proves the gist pass completed — trusted
# unconditionally. A uuid hex, so `index` keeps it OUT of the search body (index._NON_BODY_KEYS).
SUPERSEDED_BY_KEY = "superseded_by"


def new_record_id() -> str:
    """Mint a fresh content-free record id (a uuid4 hex). Distinct per call; reveals nothing about content."""
    return uuid.uuid4().hex
