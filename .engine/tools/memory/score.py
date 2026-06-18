"""score.py — the demotion scorer for active forgetting (memory-substrate-sqlite-fts5, slice 4c).

Active forgetting (memory/README) demotes a record in tiers — **hot → warm → cold → archived** — by
**frecency × role-weight × recency**, where `archived` is an index-exclusion *state* (excluded from recall, but
the record stays resident + fully recoverable in the one ledger — demotion never deletes). This module is the
pure scoring law; `forget` collects a record's access timestamps from the ledger and applies it in
`live_records` (dropping only `archived`). Tier is **derived on read** in 4c (nothing is persisted); compaction
(slice 4d) will later fold the access markers into a carried frecency snapshot — legal precisely because the
frecency function here is a **recurrence on the carried snapshot** (see `frecency`).

Design constraints (so 4d's fold stays legal and tests stay deterministic):
  * **Pure functions.** Every entry takes its inputs explicitly — a record's `access_ts` list is passed in,
    already collected; this module never reads the ledger and never looks at *other* records (no population /
    percentile statistics, which could not survive compaction folding the history away).
  * **No window.** Frecency is a decay sum over *all* of a record's reinforcements, never "the last K" — a
    windowed score is not a recurrence on a snapshot and is out of bounds (README).
  * **`now` injectable.** Defaults to wall-clock only when `None`, so a test passes an explicit `now` and
    back-dates `ts` to age a record deterministically (no sleeping).

The concrete constants below (half-life, role weights, tier thresholds) are the build-spec-leaf "forgetting
scores" the design defers to this pass (README "Build-spec leaves"); the *shape* — a birth-seeded product of
recurrence-form decays, four tiers, archived-only-excludes — is fixed by the spec.

stdlib-only; imports nothing from `memory` (a pure leaf — it cannot form an import cycle).
"""

from __future__ import annotations

import time

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
    KEEPING: born-now -> hot -> stays in recall, never silently aged into archival)."""
    if isinstance(value, bool):
        return fallback
    if isinstance(value, (int, float)):
        return int(value)
    return fallback


def frecency(birth_ts: int, access_ts, now: int) -> float:
    """Frequency-and-recency of *reinforcement*: the decayed sum over a record's birth (an implicit first
    reinforcement — without it a never-accessed record would score 0 and archive immediately) plus every
    access. Rewards accumulated, repeated recall.

    Recurrence on the carried snapshot (what slice-4d compaction relies on): exponential decay is separable,
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


def score(record, access_ts, now: "int | None" = None) -> float:
    """The demotion score frecency x role-weight x recency for `record`, given the timestamps of the accesses
    that name it (already collected from the ledger by `forget._access_index`). `now` defaults to wall-clock."""
    now = int(time.time()) if now is None else now
    birth = _coerce_ts(record.get("ts") if isinstance(record, dict) else None, now)
    return frecency(birth, access_ts, now) * role_weight(record) * recency(birth, access_ts, now)


def tier(record, access_ts, now: "int | None" = None) -> str:
    """The freshness tier of `record`: HOT / WARM / COLD / ARCHIVED. In slice 4c only ARCHIVED changes recall
    (it is excluded from `live_records`); the finer hot/warm/cold deprioritization rides slice-5 ranking."""
    now = int(time.time()) if now is None else now
    s = score(record, access_ts, now)
    if s >= HOT_THRESHOLD:
        return HOT
    if s >= WARM_THRESHOLD:
        return WARM
    if s >= COLD_THRESHOLD:
        return COLD
    return ARCHIVED
