"""records.py — the shared record vocabulary for the memory ledger.

The `kind` strings and provenance keys that more than one memory tool must agree on, in ONE place so they
never drift and no import cycle can form. `consolidate` writes the episodic + marker records; `index` keeps
provenance keys out of the search body; `forget` derives logical retirement from the marker↔batch linkage and
appends the `reinforcement` access marker + scores demotion from it; `compact` folds those
markers into the carried current-state fields below and `score` reads them back; `rollup` writes the
gist + supersession markers and `forget` derives the raws' retirement from them; `index.search` ranks
recall best-first and attaches the per-result `SCORE_KEY`. Because all of them need these names and `consolidate`
already imports `index`, defining them here — a leaf that imports nothing from the `memory` package — lets `index`,
`forget`, `score`, `compact`, and `rollup` import them without
`consolidate`→`index`→`forget`→`consolidate` becoming a cycle.

stdlib-only; imports nothing from `memory`.
"""

import uuid

# Record kinds (the `kind` field). These are the shared kinds the reflection (3b) and forgetting (4a+) layers
# both reference.
AMBIENT_CAPTURE_KIND = "turn-delta"  # the role-less, Stop-appended verbatim capture record. Promoted here (it was
                                     # capture's own) so `forget`'s recall-membership filter can name it WITHOUT
                                     # importing `capture` at module load (cycle discipline); `capture.RECORD_KIND`
                                     # now aliases this so the string never drifts.
EPISODIC_KIND = "episodic"          # an AI-written episodic summary record
MARKER_KIND = "consolidated"        # the in-ledger "this session has been tidied" marker (survives backup)

# Recall membership (issue #332). Recall surfaces the curated layer — episodic records + gists — and
# excludes ambient `turn-delta` capture, which is fuel for consolidation and the abandoned-session sweep, never
# recall content. `forget._is_ambient_capture` keys on AMBIENT_CAPTURE_KIND above; the discriminator is the
# record's `kind`, re-derived on every recall read / index rebuild (no per-record marker, no carried bit — it
# survives compaction for free). It is a targeted exclusion of the ambient kind, not a curated-kind allowlist: a
# record carrying a `role` + `text` but no explicit kind is an episodic-shaped recall record and stays surfaced.

# Tags.
DEFAULT_EPISODIC_TAG = "episodic"
MARKER_TAG = "consolidated"

# Harness-injected pseudo-turns (issue #274, folding in #333). Claude Code injects non-conversational blocks as
# `user`-role transcript turns — a background-agent completion notice (`<task-notification>`) and the `/compact`
# continuation summary (`This session is being continued from a previous conversation…`). They reach the ledger
# as ambient `turn-delta` records and are already EXCLUDED FROM RECALL by kind (above), but the consolidation
# sweep reads the raw ledger, so without a filter the in-context AI would consolidate them as if the operator had
# said them. The fix is NOT a pre-ledger drop — #333 chose to keep them RESIDENT + recoverable (the durability
# law: an abandoned session loses the reflection, not the content). Instead capture TAGS them (`INJECTED_TAG`, on
# every chunk of an injected message, recognised before chunking so a multi-chunk continuation summary is fully
# tagged) and `consolidate` SKIPS a tagged/injected record as fuel. The prefix set is deliberately the two
# DISTINCTIVE, ground-truthed standalone sentinels: each is the WHOLE injected message (never fused with a real
# prompt, confirmed against the live ledger), so a start-anchored match cannot eat conversation. `<system-reminder>`
# is deliberately EXCLUDED — it fuses with a human prompt in the same turn, so dropping it would lose real content.
INJECTED_TAG = "injected"               # the tag capture stamps on every chunk of a harness-injected pseudo-turn
_INJECTED_PSEUDO_TURN_PREFIXES = (
    "<task-notification>",                                              # background-agent completion notice
    "This session is being continued from a previous conversation",    # the /compact continuation summary
)


def is_injected_pseudo_turn_text(text) -> bool:
    """True iff `text` BEGINS with a known harness-injected pseudo-turn marker. Start-anchored (the whole injected
    message IS the block), so a genuine turn that merely mentions a marker mid-sentence is never matched. Used at
    CAPTURE, on the whole message before chunking, so every chunk of an injected message is tagged uniformly."""
    return isinstance(text, str) and text.strip().startswith(_INJECTED_PSEUDO_TURN_PREFIXES)


def is_injected_record(record) -> bool:
    """True iff `record` is a harness-injected pseudo-turn the consolidation sweep should skip as fuel: tagged
    `INJECTED_TAG` at capture (the durable path — covers every chunk), OR — back-compat for records captured
    before tagging existed — its text begins with an injected marker. The record stays physically resident and
    recoverable in the ledger and is already recall-excluded by kind; this only keeps it out of consolidation."""
    if not isinstance(record, dict):
        return False
    tags = record.get("tags")
    if isinstance(tags, list) and INJECTED_TAG in tags:
        return True
    return is_injected_pseudo_turn_text(record.get("text"))

# Provenance keys — envelope fields that are NOT human content, so the derived index keeps them OUT of the
# search body (index._NON_BODY_KEYS).
BATCH_KEY = "batch"                 # one id per consolidation pass, stamped on every episodic of that pass AND
                                    # on the pass's marker. It lets `forget` derive, purely from the ledger,
                                    # which episodics a *completed* pass closed — and which are orphans from a
                                    # crashed pass (their batch carries no marker), to logically retire.

THROUGH_SEQ_KEY = "through_seq"     # on the `consolidated` marker (#446): the per-session HIGH-WATER-MARK — the
                                    # `seq` of the last genuine turn the pass EXAMINED (reusing capture's own
                                    # per-message seq, never a parallel counter). It turns the marker from a
                                    # binary done-flag into "swept through here", so a session tidied mid-run is
                                    # re-swept for only its later half. An INT (so, like seq/ts, it stays out of
                                    # the string-leaf search body by type — no _NON_BODY_KEYS entry needed).
                                    # OPTIONAL on read: a LEGACY marker written before #446 lacks it and is
                                    # projected into seq-space from its `ts`; always present on a marker written
                                    # now. Effective per-session watermark = the MAX across the session's markers.

# The stable, content-free record id. Minted at capture in each record factory — one per record, on
# every kind (turn-delta, episodic, marker). It is a durable NAME for a record: a uuid hex, so it reveals nothing
# about the gitignored content (content-free) and survives the index rebuild and the future compaction rewrite (it
# rides in the record JSON, not an ephemeral index offset). The derived index keeps it OUT of the search body
# (index._NON_BODY_KEYS): a uuid's hex fragments are real words, exactly the `session_id`/`batch` problem.
RECORD_ID_KEY = "id"

# The reinforcement (access) marker (scored demotion). An append-only ledger record minted each time
# a record is RECALLED: it names, by the reinforced record's stable id, that the record was used. `forget.score`
# folds these into a frecency × role-weight × recency score, demoting an old, unused record in tiers
# (hot → warm → cold → archived); `archived` is excluded from recall but stays resident + recoverable in the
# ledger. A reinforcement marker is pure derivation fuel — non-content provenance — so it carries no `text`/
# `session_id`; `index` keeps its `target` (a uuid hex, the `id`/`batch` problem) OUT of the search body
# (index._NON_BODY_KEYS), and `forget.live_records` drops the marker itself from recall. The live caller that
# appends it on recall is the search server; this change ships the kind + the appender + the demo only.
REINFORCEMENT_KIND = "reinforcement"   # the `kind` field of an access marker
TARGET_KEY = "target"                  # the reinforced record's RECORD_ID_KEY value (whom the access points at)
REINFORCEMENT_TAG = "reinforcement"    # the marker's tag (kept out of the search body like every tag)

# The carried current-state fields ledger compaction folds onto a recall record before it prunes that
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

# The gist roll-up vocabulary. Active forgetting's first move is a SECOND-order
# consolidation: an AI-judged maintenance pass rolls up OLD, low-frecency EPISODIC summaries of one session into a
# compact GIST and LOGICALLY RETIRES the raw episodes (excluded from recall, still resident + fully recoverable —
# Layer-1 never erases; physical erasure is Layer-2/4e, audit-gated). `rollup` writes, in strict order under the
# single-writer lock, the gist → a per-raw `superseded` marker → the closing `rolled-up` marker; `forget` derives
# the raws' retirement from a CLOSED-batch supersession; `compact` (extended) folds a closed-batch
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

# The operator-adjudicated-erasure marker (Layer-2 physical erasure). Its OWN evidence class (NOT a
# stretch of `operator-directed`): the one marker that authorises COMPACTION to physically REMOVE a recall record
# from the ledger — the single irreversible act in the memory system, reachable ONLY because the operator merged a
# single-purpose erasure pull request (the consent gate). It names the target by its stable, content-free
# RECORD_ID_KEY (reusing TARGET_KEY — already non-body) and carries MERGE_SHA_KEY, the merge identity that
# authorised it. Pure non-content provenance: no `text`/`session_id`; `index` keeps MERGE_SHA_KEY (and TARGET_KEY)
# OUT of the search body, and `forget.live_records` drops the marker from recall (forget._is_demoted). `compact`
# removes the TARGET but RETAINS the marker itself (the idempotency tombstone, so a re-compaction is a clean no-op).
# In this PR the marker is minted ONLY by hand — the test + the throwaway-cabinet demo (compact.enact_erasure,
# the SOLE minter); no automatic producer exists until the cross-session observer reads a merged erasure PR.
# The MERGE_SHA presence is a STRUCTURAL fail-safe floor, NOT consent verification — the real merged-not-closed /
# immutable-merge-tree binding is the observer's job; `compact`'s read-side validity check ignores a
# SHA-less marker so a hand-written or bypassed one can never erase.
ERASURE_KIND = "operator-adjudicated-erasure"   # the `kind` of the merge-gated physical-removal marker
MERGE_SHA_KEY = "merge_sha"                      # the merge commit SHA that authorised the erasure (provenance only)
ERASURE_TAG = "operator-adjudicated-erasure"     # the marker's tag (kept out of the search body like every tag)

# The per-result ranking field (the `search` interface). NOT a stored ledger field: `index.search`
# attaches it to a SHALLOW COPY of each returned record, carrying the record's lexical relevance so a caller
# can see the ordering basis. The usage signal (frecency) is the
# internal tiebreak, NOT this exposed number. Because `search` could re-project a scored copy, `index` keeps this
# key OUT of the search body too (index._NON_BODY_KEYS) — belt-and-suspenders, since scored copies are never indexed.
SCORE_KEY = "score"

# Cross-session roll-up cluster sentinels (#235). Roll-up's coarse "related" pre-filter was group-by-
# session; the richer signal relates COLD episodes ACROSS sessions — a shared-topic-tag cluster (`tag:<tag>`) or a
# lexical-similarity cluster (`sim:<id8>`). Such a gist has no single originating session, so it carries the
# CLUSTER KEY as its `session_id` — a non-empty string, so every store/veto invariant that assumes a session_id
# still holds. The gist's real-session provenance is NOT lost: it lives in SOURCE_IDS_KEY, from which
# `forget.earned_consolidated_raw` recovers each contributing real session to credit the erasure veto. A real work
# session id is a uuid hex, so it can never collide with these `<prefix>:` sentinels.
TAG_SESSION_PREFIX = "tag:"      # a gist rolling up a cross-session shared-topic-tag cluster
SIM_SESSION_PREFIX = "sim:"      # a gist rolling up a cross-session lexical-similarity cluster
_CROSS_SESSION_SENTINEL_PREFIXES = (TAG_SESSION_PREFIX, SIM_SESSION_PREFIX)


def is_cross_session_sentinel(session_id) -> bool:
    """True iff `session_id` is a roll-up CLUSTER key (a gist that folds notes from MORE than one real session),
    not a real work session. Its contributing real sessions are recoverable from the gist's SOURCE_IDS_KEY."""
    return isinstance(session_id, str) and session_id.startswith(_CROSS_SESSION_SENTINEL_PREFIXES)


def new_record_id() -> str:
    """Mint a fresh content-free record id (a uuid4 hex). Distinct per call; reveals nothing about content."""
    return uuid.uuid4().hex
