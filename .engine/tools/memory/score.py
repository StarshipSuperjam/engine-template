"""score.py — the demotion scorer for active forgetting (memory substrate).

Active forgetting demotes a record in tiers — **hot → warm → cold → archived** — by
**frecency × role-weight × recency**, where `archived` is an index-exclusion *state* (excluded from recall, but
the record stays resident + fully recoverable in the one ledger — demotion never deletes). This module is the
pure scoring law; `forget` collects a record's access timestamps from the ledger and applies it in
`live_records` (dropping only `archived`). Tier is **derived on read** (nothing recall-affecting is persisted).

Scoring first worked from a record's birth + its live reinforcement markers. A later pass adds **compaction**, which
folds those markers into a carried **frecency snapshot** on the record (records.FRECENCY_SNAPSHOT_KEY etc.) and
prunes them. So this module is now **snapshot-aware**: when a record carries a valid snapshot, `score` resumes
the recurrence from it (decaying the snapshot forward + folding any post-snapshot accesses); otherwise it takes
the original birth-seeded path. Both yield the IDENTICAL score for the same record-state — legal precisely
because frecency is a **recurrence on the carried snapshot** (see `frecency`): `decay(now − t0) ·
frecency_snapshot(t0) + Σ decay(now − a)`. `compact` mints the snapshot via `mint_snapshot` here.

Design constraints (so 4d's fold stays legal and tests stay deterministic):
  * **Pure functions, no file I/O.** Every entry takes its inputs explicitly — a record's `access_ts` list is
    passed in, already collected; this module never reads the ledger/index and never looks at *other* records
    (no population / percentile statistics, which could not survive compaction folding the history away).
  * **No window.** Frecency is a decay sum over *all* of a record's reinforcements, never "the last K" — a
    windowed score is not a recurrence on a snapshot and is out of bounds.
  * **`now` injectable.** Defaults to wall-clock only when `None`, so a test passes an explicit `now` and
    back-dates `ts` to age a record deterministically (no sleeping).

The concrete constants below (half-life, role weights, tier thresholds) are the build-spec-leaf "forgetting
scores" the design defers to this pass; the *shape* — a birth-seeded product of
recurrence-form decays, four tiers, archived-only-excludes — is fixed by the spec.

stdlib-only except `records` (the field-name vocabulary leaf, which imports nothing from `memory`) — so this is
still a leaf that cannot form an import cycle. No file I/O.
"""

from __future__ import annotations

import math
import time

from memory import records

# --- Build-spec leaves (the "forgetting scores") ----------------------------------------------------------

# Decay half-life: a record's birth/access contribution halves every 14 days. A fortnight keeps a normally
# worked record hot across a sprint, yet lets a wholly untouched note cross all four tiers within ~a month.
HALF_LIFE_SECONDS = 1_209_600  # 14 * 24 * 60 * 60

# Per-type prior (NOT a per-record protection — it scales the whole product uniformly for every record of a
# role and cannot pin one specific record; recoverability, not ranking, is the real guarantee). The decisions /
# rationale / lessons that are the durable spine of project memory weigh more; an explored-and-rejected
# dead-end decays fastest. Keys MUST equal consolidate.ROLE_VOCABULARY (a test pins this without importing
# consolidate — that would cycle); a role-less record (a turn-delta, a pre-3b record) gets the default.
ROLE_WEIGHTS = {
    "decision": 1.30,
    "rationale/pushback": 1.20,
    "lesson": 1.20,
    "intent": 1.10,
    "preference": 1.10,
    "observation": 0.90,
    "dead-end": 0.70,
}
DEFAULT_ROLE_WEIGHT = 1.00

# Tier names + the score thresholds on the product. A fresh record scores 1.0 * role * 1.0 >= 0.70 -> hot; a
# never-reinforced default-role record (score = decay(age)**2) ages hot (<= ~7 d) -> warm (~7-16 d) ->
# cold (~16-30 d) -> archived (> ~30 d). Only `archived` is excluded from recall in 4c.
HOT = "hot"
WARM = "warm"
COLD = "cold"
ARCHIVED = "archived"
TIERS = (HOT, WARM, COLD, ARCHIVED)
HOT_THRESHOLD = 0.50
WARM_THRESHOLD = 0.20
COLD_THRESHOLD = 0.05


# --- The scoring law (pure) -------------------------------------------------------------------------------

def _decay(delta: float) -> float:
    """0.5 ** (max(0, delta) / HALF_LIFE_SECONDS): a contribution from `delta` seconds ago. A future-dated or
    clock-skewed event (delta < 0) is clamped to delta 0 -> 1.0, so it reads as fresh and never exceeds 1.0."""
    d = delta if delta > 0 else 0.0
    return 0.5 ** (d / HALF_LIFE_SECONDS)


def _coerce_ts(value, fallback: int) -> int:
    """A usable timestamp from a record field, or `fallback`. bool is excluded (it subclasses int); a
    missing/non-numeric value falls back — for a record's birth that means 'treat as now' (fail-safe toward
    KEEPING: born-now -> hot -> stays in recall, never silently aged into archival). A NON-FINITE float
    (NaN/inf — only reachable on an out-of-band corrupted ledger line, since the engine never writes one) also
    falls back rather than crashing `int(value)`, so one bad line never costs recall of the records around it
    (the ledger's line-resilience law, upheld here for every timestamp field, incl. the carried 4d ones)."""
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return int(value)
    return fallback


def frecency(birth_ts: int, access_ts, now: int) -> float:
    """Frequency-and-recency of *reinforcement*: the decayed sum over a record's birth (an implicit first
    reinforcement — without it a never-accessed record would score 0 and archive immediately) plus every
    access. Rewards accumulated, repeated recall.

    Recurrence on the carried snapshot (what compaction relies on): exponential decay is separable,
    `decay(now - e) = decay(now - t) * decay(t - e)`, so for any split time `t`
        frecency(now) = decay(now - t) * frecency_snapshot(t) + sum(decay(now - a) for a after t)
    i.e. 4d can carry one scalar `frecency_snapshot` stamped at `t`, discard the folded-away markers, and
    recover the identical value. A windowed or population-relative score could not be folded this way."""
    total = _decay(now - birth_ts)
    for a in access_ts:
        total += _decay(now - _coerce_ts(a, now))
    return total


def recency(birth_ts: int, access_ts, now: int) -> float:
    """Freshness floor: the decay of the single most-recent touch (birth or any access). Distinct from
    frecency — one recent access spikes this to ~1.0 and pulls a long-dormant record back up sharply, whereas
    frecency rewards the accumulated history. Both are in the spec's literal `frecency x role-weight x recency`
    and neither subsumes the other."""
    last = birth_ts
    for a in access_ts:
        a = _coerce_ts(a, now)
        if a > last:
            last = a
    return _decay(now - last)


def role_weight(record) -> float:
    """The per-type prior for a record's `role` (DEFAULT_ROLE_WEIGHT for a role-less record)."""
    role = record.get("role") if isinstance(record, dict) else None
    return ROLE_WEIGHTS.get(role, DEFAULT_ROLE_WEIGHT)


def _valid_snapshot(snap, snap_ts) -> bool:
    """True iff a record carries a usable carried frecency snapshot: a FINITE, non-negative number
    `snap` and a FINITE numeric `snap_ts`. A missing/malformed snapshot is NOT treated as `now` (that would
    inflate a record to just-compacted freshness — a visibility resurrection); instead `_effective` falls back
    to the deterministic birth-seeded path. The finiteness of `snap_ts` is load-bearing: `_effective` does
    `int(snap_ts)`, which would raise on NaN/inf — a corrupt line must degrade, never crash recall. bool is
    excluded (it subclasses int).

    Note (a deliberate, bounded acceptance): a FINITE but absurdly large `snap` (only reachable via an
    out-of-band corrupted ledger line — compaction mints a snapshot bounded by ~1 + a record's access count) is
    accepted and scores the record HIGH, i.e. KEPT VISIBLE. That is the fail-safe direction for memory (a record
    wrongly shown is recoverable; a record wrongly hidden is the dangerous one), and there is no principled
    magnitude bound distinguishing corruption from a legitimately frequently-recalled record — so this guards
    against the crash, not against high-but-finite values."""
    if isinstance(snap, bool) or isinstance(snap_ts, bool):
        return False
    if not isinstance(snap, (int, float)) or not isinstance(snap_ts, (int, float)):
        return False
    return math.isfinite(snap) and snap >= 0.0 and math.isfinite(snap_ts)


def _effective(record, access_ts, now: int):
    """The (frecency, last_event_ts) pair for `record` at `now`, the single basis both `score` and the
    compaction minter (`mint_snapshot`) use. If the record carries a valid snapshot (compacted), resume the
    recurrence from it — `decay(now − snapshot_ts) · frecency_snapshot + Σ decay(now − a)` over the
    post-snapshot accesses, with `last_access_ts` (floored to `snapshot_ts` if malformed, never `now`) carried
    as the recency base. Otherwise the original 4c birth-seeded path. The two agree for the same record-state."""
    is_dict = isinstance(record, dict)
    snap = record.get(records.FRECENCY_SNAPSHOT_KEY) if is_dict else None
    snap_ts = record.get(records.SNAPSHOT_TS_KEY) if is_dict else None
    if _valid_snapshot(snap, snap_ts):
        base_ts = int(snap_ts)
        total = _decay(now - base_ts) * float(snap)
        last = _coerce_ts(record.get(records.LAST_ACCESS_TS_KEY) if is_dict else None, base_ts)
        for a in access_ts:
            ac = _coerce_ts(a, now)
            total += _decay(now - ac)
            if ac > last:
                last = ac
        return total, last
    birth = _coerce_ts(record.get("ts") if is_dict else None, now)
    total = frecency(birth, access_ts, now)
    last = birth
    for a in access_ts:
        ac = _coerce_ts(a, now)
        if ac > last:
            last = ac
    return total, last


def score(record, access_ts, now: "int | None" = None) -> float:
    """The demotion score frecency x role-weight x recency for `record`, given the timestamps of the accesses
    that name it (already collected from the ledger by `forget._access_index`). Snapshot-aware: a
    compacted record resumes the recurrence from its carried snapshot, an un-compacted one scores from birth —
    the same value either way. `now` defaults to wall-clock."""
    now = int(time.time()) if now is None else now
    total, last = _effective(record, access_ts, now)
    return total * role_weight(record) * _decay(now - last)


def mint_snapshot(record, access_ts, now: "int | None" = None):
    """Mint the carried fields ledger compaction stamps on `record` at compaction time `now` (= t0):
    returns `(frecency_snapshot, last_access_ts)` from the record's CURRENT state (its birth or prior snapshot)
    plus the accesses being folded away. By the recurrence property a later `score` over the compacted record
    (carrying these, zero post-snapshot accesses) reproduces the pre-compaction score exactly; re-compacting a
    record that already carries a snapshot folds the prior snapshot + new accesses into a fresh one. Pure — it
    only reads `record` + the passed `access_ts`, never the ledger."""
    now = int(time.time()) if now is None else now
    return _effective(record, access_ts, now)


def tier(record, access_ts, now: "int | None" = None) -> str:
    """The freshness tier of `record`: HOT / WARM / COLD / ARCHIVED. Only ARCHIVED changes recall
    (it is excluded from `live_records`); the finer hot/warm/cold deprioritization rides the search ranking."""
    now = int(time.time()) if now is None else now
    s = score(record, access_ts, now)
    if s >= HOT_THRESHOLD:
        return HOT
    if s >= WARM_THRESHOLD:
        return WARM
    if s >= COLD_THRESHOLD:
        return COLD
    return ARCHIVED
