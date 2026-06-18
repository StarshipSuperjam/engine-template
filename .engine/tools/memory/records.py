"""records.py — the shared record vocabulary for the memory ledger (memory-substrate-sqlite-fts5, slice 4c).

The `kind` strings and provenance keys that more than one memory tool must agree on, in ONE place so they
never drift and no import cycle can form. `consolidate` writes the episodic + marker records; `index` keeps
provenance keys out of the search body; `forget` derives logical retirement from the marker↔batch linkage and
(slice 4c) appends the `reinforcement` access marker + scores demotion from it. Because all three need these
names and `consolidate` already imports `index`, defining them here — a leaf that imports nothing from the
`memory` package — lets `index`, `forget`, and `score` import them without
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


def new_record_id() -> str:
    """Mint a fresh content-free record id (a uuid4 hex). Distinct per call; reveals nothing about content."""
    return uuid.uuid4().hex
