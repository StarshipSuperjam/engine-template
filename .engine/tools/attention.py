#!/usr/bin/env python3
"""Slice 12 — the attention tool: substrate adapters + CLI over the pure ranking core (attention_rank.py).

The core ranks a candidate-set it is handed. THIS module assembles that set from the substrates that exist
today and degrades over the ones that do not, then exposes the operator's no-Claude-Desktop CLI. The reads:
  - state  (.engine/state/state.json via validate.load_json): the standing-situation pointers become an
    `orientation` candidate; the offline integration-debt count — carried with its as-of marker — stands in
    for the live register (attention/README.md:73-76) as a single `blocking_debt` candidate when non-zero.
    The `register` pointer is deliberately ignored: boot/telemetry own the live read, not attention.
  - knowledge (knowledge_query.neighbors): each neighbour of the focus becomes a `structural_neighbors`
    candidate. The focus is the "work in hand" — given explicitly (the CLI `--focus`) or DERIVED from the
    in-flight work record (`derive_focus`, the orientation default boot uses). A focus may be a single entity
    id or a SET; each member is walked at depth 1, so proximity is uniform today (the query returns no hop-depth).
  - git/GitHub (the in-flight work-record reader, work_record): open PRs + the working branch become
    `in_flight` candidates, and the files that work touches drive the knowledge focus above (#37). telemetry
    (the live debt register, slice 18) does NOT exist yet, so it is always in a result's `degraded_inputs`.

The adapter NARRATES nothing in the result (boot surfaces degradation loudly, at its slice); the CLI prints
a degrade note to stderr only, for the operator's own demo. `as_of` is the recorded reference time: the live
`rank` takes it from the cursor's integration-debt as-of, falling back to the wall clock — and ONLY the
adapter ever reads the clock, marking such a run `as_of_is_wallclock` so a consumer never mistakes it for a
refreshed-debt timestamp. The pure core never reads the clock.
"""
from __future__ import annotations
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402
import attention_rank    # noqa: E402
from attention_rank import rank, SUBSTRATES, CATEGORIES, PRECEDENCE_KEYS, TRIM_KEYS  # noqa: E402

try:
    import knowledge_query   # noqa: E402
except Exception:            # pragma: no cover - knowledge import should not fail, but assembly degrades if it does
    knowledge_query = None

try:
    import work_record       # noqa: E402  (the in-flight git/GitHub work-record reader, a pure leaf -> no cycle)
except Exception:            # pragma: no cover - a stdlib-only leaf should import, but assembly degrades if not
    work_record = None

POLICY_PATH = os.path.join(validate.ENGINE_DIR, "policies", "attention.md")
STATE_PATH = os.path.join(validate.ENGINE_DIR, "state", "state.json")

# How many distinct entities the work-in-hand focus may span — the build-spec-leaf cap (D-052/D-113), decided
# with the maintainer (a SET of all touched components, capped). It bounds the FOCUS, not the neighbour count.
FOCUS_CAP = 5


# ---- policy + substrate reads ---------------------------------------------------------------

def load_policy_values(policy_path: str = POLICY_PATH, override: dict | None = None) -> dict:
    """The attention policy's machine-read tuning values (the flat snake_case->number map), read straight
    from its YAML frontmatter by the validation foundation's reader — never parsed out of the prose body.

    When an operator policy-override is supplied, this returns the EFFECTIVE values: the shipped default with
    the override merged per-key at read time (D-167), via the core merge `validate.effective_policy_values`.
    ATTENTION owns which of its keys are structural — the partition precedence + trim order, never
    override-eligible, so "blocking-debt-first holds by construction" — and CORE owns the merge. The override
    is operator config supplied as DATA; this slice never reads an override FILE: its path/format is the
    policy-tuning authoring slice's leaf (slice 26), which loads the file and passes it here, and wires the
    live stale-key rule that consumes the merge's findings. With no override (the live path today), the
    shipped default is returned unchanged — the new `override` is a trailing keyword arg, so the existing
    positional callers are bit-for-bit unaffected."""
    default = validate.frontmatter(policy_path).get("values", {})
    if override is None:
        return default
    effective, _findings = validate.effective_policy_values(
        default, override, structural_keys=set(PRECEDENCE_KEYS) | set(TRIM_KEYS), tier="soft",
        message="An operator policy-override tunes values only, never the structural ordering.")
    return effective


def derive_focus(*, run=None, gh=None, cap: int = FOCUS_CAP) -> list:
    """The knowledge focus for the orientation-time focused read: the distinct graph entities that OWN the
    files the in-flight work touches (#37 — "which entities neighbor the work in hand", attention/README:69).

    Maps each changed path (`work_record.changed_paths`) to its entity via an EXACT source-path lookup over a
    one-shot `knowledge_query.find()` index — never a SQLite GLOB match, so a path with shell metacharacters
    (`*?[]`) can't silently mis-resolve. Skips a path that owns no entity (a non-surface file: the root
    README, CLAUDE.md, the derived graph.json, ...) and EXCLUDES a changed file's own `test_`/`demo_` entity
    (the focus is the thing under test, not its test). Distinct, stable order, capped at `cap`.

    Fail-open: returns [] on no in-flight work, or any read failure (work_record/knowledge absent, or `find`
    raising KnowledgeUnavailable) — boot still ranks the rest. `gh` is accepted for the deferred PR-files
    layer but unused today; the floor is local git. `run` defaults LAZILY to work_record's git runner (never
    as a default-arg expression, which would crash at import if the guarded work_record import degraded)."""
    if work_record is None or knowledge_query is None:
        return []
    runner = run or work_record._run_git
    try:
        paths = work_record.changed_paths(run=runner)
        if not paths:
            return []
        # One find() -> an EXACT path->id index (no SQLite GLOB, so a metacharacter path can't mis-resolve).
        # The catalog guarantees one entity per source_path (a file owns exactly one surface entity), so the
        # dict build has no real clobber; if that invariant ever broke, find()'s id-order makes it deterministic.
        by_path = {e["source_path"]: e["id"] for e in knowledge_query.find()
                   if e.get("source_path") and e.get("id")}
        focus: list = []
        for p in paths:
            eid = by_path.get(p)
            if eid is None:
                continue                                       # a non-surface file owns no entity -> skip
            if eid.split(":", 1)[-1].startswith(("test_", "demo_")):
                continue                                       # focus the thing under test, not its test/demo
            if eid not in focus:
                focus.append(eid)
            if len(focus) >= cap:
                break
        return focus
    except Exception:
        return []  # the work-in-hand focus could not be derived -> degrade (no focused read this session)


def assemble_candidates(policy_values: dict, *, state_path: str = STATE_PATH,
                        focus: "str | list[str] | None" = None,
                        edge_filter=None, depth: int = 1, gh=None):
    """Assemble the candidate-set from the substrates present today, reporting which were available and the
    cursor's as-of marker. Returns (candidates, available_inputs:set, cursor_as_of:str|None). Narrates nothing.

    `gh` is the GitHub reader for the in-flight work-record read (None -> the local-git floor only; boot
    passes a real reader, the CLI passes None). Like state/knowledge, the work record is read HERE — the only
    boot-loaded input is the operator override (config, not a substrate)."""
    candidates: list = []
    available: set = set()
    cursor_as_of = None

    try:
        state = validate.load_json(state_path)
        available.add("state")
        situation = state.get("standing_situation") or {}
        if situation.get("milestone") or situation.get("phase"):
            candidates.append({"id": "state:standing-situation", "category": "orientation",
                               "recency": None, "source": "state"})
        debt = state.get("integration_debt") or {}
        cursor_as_of = debt.get("as_of")
        if (debt.get("open_count") or 0) > 0:
            # The offline cached count stands in for the live register while telemetry is absent. Severity
            # is unknown offline, so the floor is surfaced AS blocking (severity = the policy bar) rather
            # than hidden — the safe degraded posture; telemetry refines it per-item when it lands.
            candidates.append({"id": "state:integration-debt", "category": "blocking_debt",
                               "severity": policy_values.get("debt_blocking_threshold", 0),
                               "recency": cursor_as_of, "source": "state"})
    except Exception:
        pass  # state absent or malformed -> degrade over it (it stays out of available_inputs)

    if focus and knowledge_query is not None:
        try:
            # The cold-start adjacency walk is PINNED to the four structural edges (the attention policy's
            # `## Scope` budget-neutrality invariant, D-203): a new edge kind (e.g. supersedes) is pull-only
            # and never bulks up orientation. Pass the walk set explicitly rather than leaning on the
            # neighbors() default, so the pin lives at attention's own call site.
            walk_edges = edge_filter if edge_filter is not None else list(knowledge_query.WALK_EDGE_KINDS)
            # The walk is BIDIRECTIONAL (forward + reverse, `direction="both"`) over that same pinned edge
            # set (D-224). Forward-only starves a leaf: a non-check, ungoverned surface has no outgoing
            # structural edge but `provided_by` -> its module, so it collapses to just its module. Reverse
            # (`direction:in`) surfaces the connective tissue that already exists in the graph — a policy's
            # governed surfaces, a module's dependents/surfaces, any surface's targeting checks. Reverse is a
            # query-time direction over the SAME forward edges (D-203 gate 3), NOT a new edge type, so it is
            # budget-neutral: reverse candidates compete for the same fixed structural_neighbors slice, never
            # grow it. A genuinely bare leaf (ungoverned AND untargeted, e.g. a tool) still resolves to only
            # its module; relevance-ordering a dense neighbourhood is deferred (engine-planning Q38/Q39, D-224).
            # The focus is the work in hand — a single entity id or a SET (the changed work usually spans
            # several entities, #37). Walk each member, then DEDUPE neighbours and EXCLUDE any neighbour that
            # is itself a focus member (co-changed entities are not each other's "structural neighbours").
            # FOCUS_CAP bounds the focus set; the structural_neighbors PARTITION is bounded downstream by the
            # policy budget/trim via rank() — this cap does not (and need not) bound the candidate count.
            focus_ids = [focus] if isinstance(focus, str) else list(focus)
            focus_set = set(focus_ids)
            seen: set = set()
            for fid in focus_ids:
                for n in knowledge_query.neighbors(fid, edge_filter=walk_edges, depth=depth, direction="both"):
                    nid = n["id"]
                    if nid in focus_set or nid in seen:
                        continue
                    seen.add(nid)
                    candidates.append({"id": nid, "category": "structural_neighbors",
                                       "proximity": 1.0, "recency": None, "source": "knowledge"})
            available.add("knowledge")  # the substrate WAS consulted (an empty neighbourhood is still a read)
        except Exception:
            pass  # knowledge unavailable -> degrade over it

    if work_record is not None:
        try:
            # The native git/GitHub work record's in-flight half (open PRs + the working branch) -> in_flight
            # candidates. read_in_flight RAISES only when git cannot be consulted at all (then git stays in
            # degraded_inputs); an empty list means git IS available with no in-flight work. The recency the
            # reader emits is already trailing-Z-normalised (or None), so the ranking math never sees a bad ts.
            for r in work_record.read_in_flight(gh=gh):
                candidates.append({"id": r["id"], "category": "in_flight",
                                   "recency": r.get("recency"), "source": "git"})
            available.add("git")
        except Exception:
            pass  # the in-flight work record could not be consulted -> degrade over git

    return candidates, available, cursor_as_of


# ---- live ranking (the single assembler shared by the CLI and boot) -------------------------

def _now_z() -> str:
    """The wall-clock reference moment, trailing-Z UTC. The ONLY clock read in attention, and only on the
    live path when the cursor carries no as-of; the result is marked as_of_is_wallclock."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rank_live(*, policy_path: str = POLICY_PATH, override: dict | None = None,
              focus: "str | list[str] | None" = None,
              depth: int = 1, budget_total: int | None = None, as_of: str | None = None,
              apply_precedence: bool = True, gh=None) -> dict:
    """The live ranking path over the substrates present today, returning the attention-result.v1 dict
    (whose own `degraded_inputs` records the absent substrates). This is the ONE assembler the CLI (`rank`)
    and boot's SessionStart pack both call, so boot CONSUMES the partition it is handed — in the locked
    precedence order — and never re-ranks (boot/README relay-not-detect; the result contract is attention's,
    not boot's to re-derive). `as_of` defaults to the cursor's integration-debt as-of, falling back to the
    wall clock (the run then marked `as_of_is_wallclock`) — the only clock read; the pure core stays
    clock-free. `budget_total` (boot owns it) sizes the per-category split when supplied. `override` is the
    attention slice of the operator policy-override (D-167) the LOADING layer (boot) reads and passes as DATA;
    it is merged per-key into the effective values via the core merge (`load_policy_values`), keeping the
    static-input determinism — attention never reads the override FILE itself. `gh` is the GitHub reader for
    the in-flight work-record read (boot builds + passes it; the CLI leaves it None -> the local-git floor)."""
    policy_values = load_policy_values(policy_path, override)
    candidates, available, cursor_as_of = assemble_candidates(policy_values, focus=focus, depth=depth, gh=gh)
    resolved_as_of = as_of or cursor_as_of
    as_of_is_wallclock = False
    if resolved_as_of is None:
        resolved_as_of, as_of_is_wallclock = _now_z(), True
    return rank(candidates, policy_values, resolved_as_of, available,
                budget_total=budget_total, apply_precedence=apply_precedence,
                as_of_is_wallclock=as_of_is_wallclock)


# ---- CLI ------------------------------------------------------------------------------------


def _emit(result: dict) -> None:
    # allow_nan=False: the core coerces non-finite signals away, so a NaN can never reach here on a valid
    # run — but if one ever did it must fail LOUD rather than emit an invalid bare-NaN token (halt-on-malformed).
    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True, allow_nan=False))


def _note_degrade(degraded: list) -> None:
    if degraded:
        print(f"(attention ranked over the available substrates; absent and degraded over: "
              f"{', '.join(degraded)} — boot surfaces this loudly at its slice)", file=sys.stderr)


def _cmd_rank(rest: list) -> int:
    """Live rank over the substrates present today (the operator CLI over the shared `rank_live`). With no
    explicit `--focus`, the focus is AUTO-DERIVED from the in-flight work record (the orientation default, so
    the CLI shows the same focused read boot does); `--no-focus` opts out (a bare, focus-free rank)."""
    if "--no-focus" in rest:
        focus = None
    else:
        explicit = _flag(rest, "--focus", None)
        focus = explicit if explicit is not None else (derive_focus() or None)
    budget = _flag(rest, "--budget", None)
    result = rank_live(
        policy_path=_flag(rest, "--policy", POLICY_PATH),
        focus=focus,
        depth=int(_flag(rest, "--depth", "1")),
        budget_total=int(budget) if budget is not None else None,
        as_of=_flag(rest, "--as-of", None),
        apply_precedence="--no-precedence" not in rest)
    _note_degrade(result["degraded_inputs"])
    _emit(result)
    return 0


def _cmd_rank_fixture(rest: list) -> int:
    """Rank a CONSTRUCTED candidate-set from a fixture file — the demo/test path, no live substrates. The
    fixture is {"candidates": [...], "available_inputs": [...]}. --as-of is REQUIRED so the core stays clock-free.
    """
    if not rest:
        print("usage: attention.py rank-fixture FIXTURE.json --as-of <UTC-Z> [--policy P] [--budget N] "
              "[--no-precedence]", file=sys.stderr)
        return 2
    fixture = validate.load_json(rest[0])
    as_of = _flag(rest, "--as-of", None)
    if not as_of:
        print("rank-fixture requires --as-of <UTC-Z> (the core never reads the clock)", file=sys.stderr)
        return 2
    policy_values = load_policy_values(_flag(rest, "--policy", POLICY_PATH))
    budget = _flag(rest, "--budget", None)
    result = rank(fixture.get("candidates", []), policy_values, as_of,
                  set(fixture.get("available_inputs", [])),
                  budget_total=int(budget) if budget is not None else None,
                  apply_precedence="--no-precedence" not in rest)
    _emit(result)
    return 0


# A built-in constructed fixture for the scripted demo: one blocking-debt item with a deliberately LOW
# weight (old, severity just at the bar) and one in-flight feature with a deliberately HIGH weight (current,
# close). With precedence the debt leads structurally; without it the feature out-weighs and floats up.
DEMO_AS_OF = "2026-06-04T00:00:00Z"
DEMO_CANDIDATES = [
    {"id": "debt:payment-overdue", "category": "blocking_debt", "severity": 2,
     "recency": "2026-04-01T00:00:00Z", "source": "telemetry"},
    {"id": "feature:shiny-rewrite", "category": "in_flight", "proximity": 1.0,
     "recency": "2026-06-04T00:00:00Z", "source": "git"},
]


def _lead_id(result: dict) -> str:
    """The id of the single highest-ranked candidate across the whole partition (flattened in array order)."""
    for entry in result["partition"]:
        if entry["members"]:
            return entry["members"][0]["id"]
    return "(none)"


def _cmd_demo(rest: list) -> int:
    """Scripted fail->pass the operator can read without JSON, and VARY with one flag. Ranks the built-in
    fixture WITH precedence, with precedence REMOVED, then RESTORED, printing the lead each time. The flag
    `--feature-proximity N` raises the feature's ranking weight, so the operator can confirm by hand that
    blocking debt STILL leads with precedence however high the feature's weight goes (the guarantee is
    structural). No JSON authoring needed — one flag does it."""
    policy_values = load_policy_values(_flag(rest, "--policy", POLICY_PATH))
    feature_proximity = float(_flag(rest, "--feature-proximity", "1.0"))
    candidates = [dict(DEMO_CANDIDATES[0]), {**DEMO_CANDIDATES[1], "proximity": feature_proximity}]
    avail = {"state", "knowledge"}  # the demo fixture stands in for telemetry/git content; mark them degraded
    with_p = rank(candidates, policy_values, DEMO_AS_OF, avail, apply_precedence=True)
    without_p = rank(candidates, policy_values, DEMO_AS_OF, avail, apply_precedence=False)
    restored = rank(candidates, policy_values, DEMO_AS_OF, avail, apply_precedence=True)
    print("Attention ranking — structural-precedence demonstration")
    print("  Candidates: a blocking-debt item ('debt:payment-overdue', old, lower weight) and an in-flight")
    print(f"              feature ('feature:shiny-rewrite', current and close; proximity={feature_proximity}).")
    print(f"  WITH precedence (the engine's normal behaviour): leads with -> {_lead_id(with_p)}")
    print("      The blocking debt leads even though the feature has the higher ranking weight, because the")
    print("      order of importance is structural, not a weight anything can out-tune.")
    print(f"  PRECEDENCE REMOVED (diagnostic): leads with -> {_lead_id(without_p)}")
    print("      With the structural order taken away, the higher-weighted feature floats to the top —")
    print("      proving the lead above was held by structure, not by the weights.")
    print(f"  PRECEDENCE RESTORED: leads with -> {_lead_id(restored)}")
    print("  Try it yourself (no file editing needed): re-run with the feature's weight cranked far higher —")
    print("      uv run --directory .engine -- python tools/attention.py demo --feature-proximity 1000")
    print("  WITH precedence the blocking debt STILL leads. The guarantee is structural, not out-tunable.")
    # the self-check asserts only the structural invariant (debt leads WITH precedence, and again once restored);
    # whether the feature floats up when precedence is removed depends on the weight the operator chose.
    ok = _lead_id(with_p) == "debt:payment-overdue" and _lead_id(restored) == "debt:payment-overdue"
    if not ok:
        print("DEMO UNEXPECTED: the structural guarantee did not hold on the built-in fixture.", file=sys.stderr)
        return 1
    return 0


def main(argv: list) -> int:
    if not argv:
        print("usage: attention.py {rank [--focus ID] [--no-focus] [--as-of T] [--budget N] [--no-precedence] "
              "[--policy P] | rank-fixture FIXTURE.json --as-of T [...] | demo}", file=sys.stderr)
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "rank":
        return _cmd_rank(rest)
    if cmd == "rank-fixture":
        return _cmd_rank_fixture(rest)
    if cmd == "demo":
        return _cmd_demo(rest)
    print(f"unknown command {cmd!r}", file=sys.stderr)
    return 2


def _flag(argv: list, flag: str, default):
    return argv[argv.index(flag) + 1] if flag in argv and argv.index(flag) + 1 < len(argv) else default


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
