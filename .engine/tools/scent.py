#!/usr/bin/env python3
"""scent.py — the per-prompt attention scent (boot/orientation; memory-substrate-sqlite-fts5 slice 5, PR 2).

This is the second of the two inert `core` seams the memory build lights up — the one whose end-to-end
fail-then-pass demo completes M1 (the close ambient-capture relay is its twin). It is "metacognition as a
push" (boot/README): on every prompt (`UserPromptSubmit`) a FAST lexical lookup over memory's FTS5 index
injects **attributed pointers, not content** — "memory may hold earlier notes whose words match this; query
and verify before asserting" — when the prompt STRONGLY matches stored memory, and is **silent** otherwise
(often, on sparse memory — that is correct: it is a nudge, not a guarantee).

OWNERSHIP — the close-relay twin (NOT memory-owned). `UserPromptSubmit` is a single-owner `boot/orientation`
event (hooks.py EVENT_INVENTORY `("boot",)`; the locked hooks owner table), so this is a boot/core-owned tool,
wired in core's manifest, that LAZILY reaches memory's index — exactly as `close.py` (a single-owner `Stop`
seam) lazily relays to `memory.capture_turn_delta`. Memory SUPPLIES the lookup (`index.scent_lookup`); it does
not own the behavior, so the inventory stays `("boot",)`. On a repo without the memory module the lazy import
fails and the seam is inert (silent), never a fault — the close-relay degrade-clean precedent.

THE LAWS (boot/README:212-232), all load-bearing here and pinned by tests:
  - HOT PATH. Fires every prompt, so it imports only `hooks` (+ `validate`) and lazily `memory.index`, never
    boot's heavy stack. The lookup is `index.scent_lookup` — OR-match, relevance-ONLY, FAST-PATH ONLY (it never
    runs the slow scan): the lexical work is sub-millisecond and does not grow with memory size.
  - ATTRIBUTED POINTERS, NOT CONTENT. The injected block names a matched record's ROLE + TAGS + a count, NEVER
    its `text` body, and carries "verify before asserting" — a pointer the AI verifies, never recall woven into
    prose. (Pinned: a body-leak guard + a verify-clause assert.)
  - SILENT ON NO STRONG MATCH. A match surfaces only when its lexical salience clears attention's policy bar
    (`scent_strong_match_threshold`); below it, nothing is injected.
  - DEDUP. Because Claude Code's `additionalContext` persists in history, a pointer surfaced once this session
    is not re-injected — tracked by RECORD ID in an OS-temp, session-keyed store (the modes/close pattern). A
    NEW topic still surfaces (keyed on id, not on the prompt).
  - DOES NOT REINFORCE. `scent_lookup` is side-effect-free; a pushed pointer the AI has not acted on is not
    "usage". Reinforcement stays at the model-initiated PULL (the engine-memory MCP `search` tool).
  - DEGRADE / FAIL-OPEN. FTS5 absent on this machine → a one-time slower-mode disclosure, then silent (never a
    per-prompt slow scan). Any crash injects nothing (the hooks harness fail-opens), never stalling the turn.

CLI:  python tools/scent.py            # hook mode: run the UserPromptSubmit handler over stdin (what the
                                       #   wired hook invokes; injects additionalContext, fail-open)
      python tools/scent.py demo       # an operator-runnable fail-then-pass demonstration (throwaway cabinet)
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hooks     # noqa: E402  (run_hook + inject/proceed: the fail-open harness this rides)
import validate  # noqa: E402  (frontmatter + ENGINE_DIR + effective_policy_values: the policy read + the D-167 merge)
import operator_overrides  # noqa: E402  (the D-167 override slice; a thin stdlib+validate JSON reader — hot-path-safe)


# ---- tuning leaves (recorded build-spec leaves; see the slice-5 PR-2 plan) ------------------
_THRESHOLD_KEY = "scent_strong_match_threshold"   # attention owns the value; the scent only READS it
_DEFAULT_THRESHOLD = 0.5                            # fallback if the policy can't be read (degrade-safe)
_CANDIDATES = 20                                    # how many bm25-ranked matches scent_lookup hands back
_SURFACE_MAX = 3                                    # at most this many pointers per prompt ("selective, minimal")
_TAGS_SHOWN = 4                                     # at most this many tags named per pointer

# The per-session "already surfaced" store — OS-temp, session-keyed, the modes stance / close findings pattern
# (close.py:72-95): non-committed, never read across sessions, no repo footprint, no catalog entry. The prefix
# differs from `engine-stance-` / `engine-findings-` (no collision).
_SIGNAL_PREFIX = "engine-scent-"
_DEGRADED_MARKER = "\x00degraded"   # a reserved sentinel in the surfaced-set (never a real record id), so the
                                    # slower-mode disclosure is shown at most ONCE per session.

# AI-FACING text (this reaches the model via additionalContext, never the operator's screen). The verify clause
# is the locked "attributed and unverified" trust seam — keep it.
_VERIFY_CLAUSE = "a word-match, not confirmed recall — query memory and verify before asserting"
_DEGRADED_DISCLOSURE = (
    "Memory's fast recall is unavailable on this machine, so the automatic per-prompt memory hints are paused "
    "this session. You can still query memory directly (the memory search tool) when you need it."
)
# The §7 recall-completeness disclosure (D-273/D-274, issue #332). Recall surfaces only the curated layer — these
# pointers are to curated summaries; the raw, word-for-word notes behind them are kept and fully recoverable
# (never deleted by that exclusion). Shown ONCE per session alongside the first pointers (mirroring the degraded
# disclosure's once-per-session sentinel), so the operator can be offered the verbatim. AI-facing and relayed; the
# wording is a build-spec leaf.
_COMPLETENESS_MARKER = "\x00recall-completeness"   # a reserved sentinel in the surfaced-set (never a real id)
_COMPLETENESS_DISCLOSURE = (
    "These point to curated summaries of earlier sessions; the raw, word-for-word notes behind them are kept and "
    "recoverable — offer to pull the exact wording if the operator wants it."
)


# ---- the strong-match threshold (read from attention's policy; degrade-safe) ----------------

def _threshold() -> float:
    """The salience bar a lexical match clears to surface, read from attention's policy (attention OWNS the
    value; the scent reads it) THROUGH the D-167 operator-override merge — so a reviewed and merged
    `/engine-tune` of `scent_strong_match_threshold` actually reaches the per-prompt scent. Merged inline via
    `operator_overrides.slice_for` + the core `validate.effective_policy_values` (the same merge
    `attention.load_policy_values` performs) rather than by importing `attention` — that would pull
    `attention_rank`→`knowledge_query` onto the hot path, while `operator_overrides` is a thin stdlib+validate
    JSON read. An unreadable/malformed policy or a missing key degrades to `_DEFAULT_THRESHOLD` — never a
    per-prompt crash and never a silently-disabled scent."""
    try:
        policy = os.path.join(validate.ENGINE_DIR, "policies", "attention.md")
        default = validate.frontmatter(policy).get("values") or {}
        # The D-167 read-time merge: the operator's tuned value wins over the shipped default, so a reviewed and
        # merged `/engine-tune` of the scent threshold actually reaches this hot path (it never did before).
        # `scent_strong_match_threshold` is override-ELIGIBLE (a threshold, not a structural precedence/trim law),
        # so `structural_keys=set()` merges it cleanly; `slice_for` already returns {} on any override-file fault.
        effective, _findings = validate.effective_policy_values(
            default, operator_overrides.slice_for("attention"), structural_keys=set(), tier="soft",
            message="An operator policy-override tunes the scent threshold, never the structural ordering.")
        return float(effective.get(_THRESHOLD_KEY, _DEFAULT_THRESHOLD))
    except Exception:  # noqa: BLE001 — the scent must survive any policy fault, not disable itself or crash
        return _DEFAULT_THRESHOLD


def _salience(score) -> float:
    """Map a record's raw lexical relevance (bm25, unbounded ≥ 0) into a 0..1 band with the bounded transform
    `s/(1+s)`, so it is comparable to the policy's [0,1] threshold. UNCALIBRATED — bm25 is corpus-dependent and
    the shipped product starts with an empty corpus; the tests prove the MECHANISM (a distinctive match clears
    the bar, a common/weak one does not), not a calibrated value."""
    try:
        s = float(score)
    except (TypeError, ValueError):
        return 0.0
    return s / (1.0 + s) if s > 0 else 0.0


# ---- the per-session "already surfaced" store (OS-temp; the close.py pattern) ----------------

def _sanitize(session_id) -> str:
    """A filename-safe, length-bounded slug of the platform session id. An empty/garbled id yields "" →
    `_signal_path` returns None → no dedup state (the same pointer may re-surface — benign), never a crash."""
    if not session_id or not isinstance(session_id, str):
        return ""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)[:200]


def _signal_path(session_id) -> "str | None":
    slug = _sanitize(session_id)
    return os.path.join(tempfile.gettempdir(), f"{_SIGNAL_PREFIX}{slug}") if slug else None


def _surfaced(session_id) -> set:
    """The set of record-ids (plus the degraded sentinel) already surfaced this session. Absent / unreadable /
    malformed → empty (degrade-safe: at worst a pointer re-surfaces, never a crash or a held turn)."""
    path = _signal_path(session_id)
    if not path:
        return set()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001 — absent / unreadable / malformed → nothing surfaced yet
        return set()
    ids = data.get("surfaced") if isinstance(data, dict) else None
    return {i for i in ids if isinstance(i, str)} if isinstance(ids, list) else set()


def _mark_surfaced(session_id, ids) -> bool:
    """Add ids to this session's surfaced-set. Non-atomic read-modify-write (the close.py pattern): two prompts
    in flight could lose-update, at worst re-injecting ONE pointer — never a crash. A failed write degrades to
    "nothing recorded" (the pointer may re-surface), never raising."""
    path = _signal_path(session_id)
    if not path:
        return False
    current = _surfaced(session_id)
    current.update(i for i in ids if isinstance(i, str))
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"surfaced": sorted(current)}, fh)
        return True
    except Exception:  # noqa: BLE001 — a failed write means the pointer may re-surface, never a crash
        return False


def _clear(session_id) -> None:
    """Wipe a session's surfaced-set (used by the demo for a clean run). Idempotent; never raises."""
    path = _signal_path(session_id)
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


# ---- rendering: attributed pointers, NEVER the record body ----------------------------------

def _pointer_line(record) -> str:
    """One pointer for one matched record: its ROLE + a few TAGS (entity references — pointers, not content),
    NEVER its `text` body. Tags are the locked "secondary filter" surface (search.json: a caller composes entity
    links by tag-filtering), so naming them points without quoting."""
    role = record.get("role") if isinstance(record, dict) else None
    role = role if isinstance(role, str) and role else "note"
    raw = record.get("tags") if isinstance(record, dict) else None
    tags = [t for t in (raw or []) if isinstance(t, str) and t][:_TAGS_SHOWN]
    return f"- a recorded {role} tagged: {', '.join(tags)}" if tags else f"- a recorded {role}"


def _render(records) -> str:
    """The attributed pointer block injected as additionalContext (AI-facing). Names role/tags/count only —
    body-free — and leads with the verify clause (the locked trust seam)."""
    n = len(records)
    head = (f"Memory may already hold {n} earlier {'note' if n == 1 else 'notes'} whose words match your prompt "
            f"({_VERIFY_CLAUSE}):")
    return head + "\n" + "\n".join(_pointer_line(r) for r in records)


# ---- the UserPromptSubmit handler -----------------------------------------------------------

def handler(payload: dict) -> dict:
    """The per-prompt scent. Read the prompt; lazily reach memory's fast lookup; surface the STRONG, not-yet-seen
    matches as attributed pointers, or stay silent. Rides `hooks.run_hook` (fail-open: any crash injects nothing).

      no prompt / no memory module        -> proceed (silent; the seam is inert without memory)
      FTS5 absent (lookup unavailable)     -> a ONE-TIME slower-mode disclosure, then silent (never a slow scan)
      no match clears the salience bar     -> proceed (silent on no strong match)
      all strong matches already surfaced  -> proceed (dedup; persists-in-history hygiene)
      else                                 -> inject ≤ _SURFACE_MAX attributed pointers
    """
    payload = payload if isinstance(payload, dict) else {}
    prompt = payload.get("prompt")
    session_id = payload.get("session_id")
    if not isinstance(prompt, str) or not prompt.strip():
        return hooks.proceed()
    try:
        from memory import index, records  # noqa: E402 — lazy: absent on a memory-less repo -> inert seam
    except Exception:  # noqa: BLE001 — no memory module is the INERT-SEAM state, never a fault (no finding)
        return hooks.proceed()

    result = index.scent_lookup(prompt, limit=_CANDIDATES)
    if not result.available:
        # FTS5 absent — memory DETECTS, the scent RENDERS the slower-mode disclosure (once), then stays silent;
        # it never runs the slow scan on this hot path (that would blow the latency budget).
        if _DEGRADED_MARKER in _surfaced(session_id):
            return hooks.proceed()
        _mark_surfaced(session_id, [_DEGRADED_MARKER])
        return hooks.inject(_DEGRADED_DISCLOSURE)

    threshold = _threshold()
    strong = [r for r in result.records if _salience(_score_of(r, records)) >= threshold]
    if not strong:
        return hooks.proceed()

    fresh = _undeduped(strong, session_id, records.RECORD_ID_KEY)
    if not fresh:
        return hooks.proceed()
    block = _render(fresh)
    # §7: alongside the FIRST pointers of the session, disclose once that these are curated summaries whose raw
    # verbatim is kept and recoverable (the surfaced-set dedup keeps it to one showing, like the degraded notice).
    if _COMPLETENESS_MARKER not in _surfaced(session_id):
        _mark_surfaced(session_id, [_COMPLETENESS_MARKER])
        block = block + "\n\n" + _COMPLETENESS_DISCLOSURE
    return hooks.inject(block)


def _score_of(record, records_mod) -> float:
    return record.get(records_mod.SCORE_KEY, 0.0) if isinstance(record, dict) else 0.0


def _undeduped(strong, session_id, id_key) -> list:
    """The strong matches not already surfaced this session, capped at `_SURFACE_MAX`, marking the new ids.
    Dedup is keyed on RECORD ID (not the prompt), so a topic pivot still surfaces fresh records. A match with no
    id is surfaced (a real match) but cannot be tracked — a rare, benign re-surface."""
    seen = _surfaced(session_id)
    fresh: list = []
    new_ids: list = []
    for record in strong:
        rid = record.get(id_key) if isinstance(record, dict) else None
        has_id = isinstance(rid, str) and bool(rid)
        if has_id and rid in seen:
            continue
        fresh.append(record)
        if has_id:
            new_ids.append(rid)
        if len(fresh) >= _SURFACE_MAX:
            break
    if new_ids:
        _mark_surfaced(session_id, new_ids)
    return fresh


# ---- the operator-runnable demo (a throwaway PRACTICE cabinet; never the real store) --------
# Run it and vary the prompt / memories / threshold near the top:
#     uv run --directory .engine --frozen -- python tools/scent.py demo
# It exercises the REAL handler + real index.scent_lookup on a temp cabinet (via ENGINE_MEMORY_DIR), so a real
# regression flips a `!!!` and returns non-zero. Plain words only.

# Vary these and re-run. The strong prompt shares a DISTINCTIVE word ("calendar") with a stored memory; the
# near-miss prompt shares only ordinary words, so it stays silent.
_DEMO_MEMORIES = [
    {"role": "decision", "tags": ["scheduling", "calendar-sync"],
     "text": "We decided to build the calendar sync against the user's own calendar, never a blind cron job."},
    {"role": "lesson", "tags": ["onboarding"],
     "text": "Keep the onboarding copy short; people skip long introductions."},
    {"role": "preference", "tags": ["naming"],
     "text": "Prefer snake_case for every configuration key."},
    {"role": "observation", "tags": ["release"],
     "text": "The Friday release shipped without the export feature."},
]
_DEMO_STRONG_PROMPT = "how should we handle the calendar sync?"
_DEMO_NEAR_MISS_PROMPT = "what is the weather like today?"


def _inject_text(decision) -> "str | None":
    """The text a handler decision would inject, or None when it stays silent."""
    if isinstance(decision, dict) and decision.get("action") == "inject":
        return decision.get("context", "")
    return None


def _demo() -> int:
    import shutil
    from memory import index, ledger, records  # demo-only (not the hot path), so a top-of-function import is fine

    if not index.fts5_available():
        print("This computer's fast search feature is unavailable, so the scent would only show its paused")
        print("disclosure. That is itself correct behaviour, but this demo needs the fast lookup present.")
        return 0

    tmp = tempfile.mkdtemp(prefix="engine-scent-demo-")
    prev = os.environ.get(ledger.ENV_DIR)
    os.environ[ledger.ENV_DIR] = tmp          # the REAL handler's default-path lookup now lands in this cabinet
    results: list = []
    try:
        cabinet = ledger.ledger_path()
        ipath = index.index_path()
        now = int(__import__("time").time())

        def run(prompt, session_id):
            return _inject_text(handler({"prompt": prompt, "session_id": session_id}))

        # PART 1 — the inert → live crossover (the M1 seam)
        print("=" * 80)
        print("PART 1 — the seam lights up: SILENT on empty memory, a pointer once memory is filled")
        print("=" * 80)
        _clear("p1")
        before = run(_DEMO_STRONG_PROMPT, "p1")
        for m in _DEMO_MEMORIES:
            ledger.append({**m, "ts": now, records.RECORD_ID_KEY: records.new_record_id()}, path=cabinet)
        index.rebuild(ledger_file=cabinet, index_file=ipath)
        _clear("p1")
        after = run(_DEMO_STRONG_PROMPT, "p1")
        ok1 = before is None and after is not None
        print(f'\n  prompt: "{_DEMO_STRONG_PROMPT}"')
        print(f"  with EMPTY memory  -> {'(silent)' if before is None else 'INJECTED'}")
        print(f"  with memory FILLED -> {'INJECTED a pointer' if after is not None else '(silent)'}")
        if after:
            print("  what it injected:")
            for line in after.splitlines():
                print(f"      {line}")
        print(f"  => {'Silent until memory was filled, then it surfaced a pointer.' if ok1 else '!!! the seam did not light up'}")
        results.append(ok1)

        # PART 2 — fires on a strong match, silent on a near-miss
        print("\n" + "=" * 80)
        print("PART 2 — fires when your words STRONGLY match a memory; silent otherwise")
        print("=" * 80)
        _clear("p2a"); _clear("p2b")
        strong = run(_DEMO_STRONG_PROMPT, "p2a")
        miss = run(_DEMO_NEAR_MISS_PROMPT, "p2b")
        ok2 = strong is not None and miss is None
        print(f'\n  strong  "{_DEMO_STRONG_PROMPT}"  -> {"a pointer" if strong else "(silent)"}')
        print(f'  near-miss "{_DEMO_NEAR_MISS_PROMPT}" -> {"a pointer" if miss else "(silent)"}')
        print("\n  It is a NUDGE, not a guarantee: it speaks only on a strong word-match and is silent the rest of")
        print("  the time (often, on sparse memory — that is correct). Vary the PROMPT above and re-run to watch")
        print("  the line move — that is the proof it is not staged.")
        print(f"  => {'Strong match spoke; near-miss stayed silent.' if ok2 else '!!! firing was wrong'}")
        results.append(ok2)

        # PART 3 — attributed POINTERS, never the memory's words
        print("\n" + "=" * 80)
        print("PART 3 — it points (kind + topic), it never quotes the stored memory, and it says 'verify'")
        print("=" * 80)
        _clear("p3")
        text = run(_DEMO_STRONG_PROMPT, "p3") or ""
        bodies = [m["text"] for m in _DEMO_MEMORIES]
        leaked = [b for b in bodies if b in text]
        has_verify = "verify before asserting" in text
        ok3 = bool(text) and not leaked and has_verify
        print(f"\n  the injected pointer:\n      " + "\n      ".join(text.splitlines()))
        print(f"\n  quotes a stored memory's words? {'NO' if not leaked else 'YES — ' + str(leaked)}")
        print(f"  tells the assistant to verify first? {'yes' if has_verify else 'NO'}")
        print(f"  => {'Pointed without quoting, and said verify.' if ok3 else '!!! it leaked content or dropped the verify clause'}")
        results.append(ok3)

        # PART 4 — surfaced once per session, not on every prompt
        print("\n" + "=" * 80)
        print("PART 4 — the same pointer is shown ONCE per session, never repeated every prompt")
        print("=" * 80)
        _clear("p4")
        first = run(_DEMO_STRONG_PROMPT, "p4")
        second = run(_DEMO_STRONG_PROMPT, "p4")
        ok4 = first is not None and second is None
        print(f"\n  first time the topic comes up  -> {'a pointer' if first else '(silent)'}")
        print(f"  second time, same session      -> {'a pointer' if second else '(silent)'}")
        print(f"  => {'Surfaced once, then quiet.' if ok4 else '!!! it repeated or never surfaced'}")
        results.append(ok4)

        # PART 5 — fast search unavailable: a one-time paused notice, never a stall
        print("\n" + "=" * 80)
        print("PART 5 — if fast search is unavailable, it says so ONCE and pauses, never stalls")
        print("=" * 80)
        _clear("p5")
        orig = index.fts5_available
        index.fts5_available = lambda *a, **k: False
        try:
            d1 = run(_DEMO_STRONG_PROMPT, "p5")
            d2 = run(_DEMO_STRONG_PROMPT, "p5")
        finally:
            index.fts5_available = orig
        ok5 = d1 is not None and "paused" in d1 and d2 is None
        print(f"\n  first prompt  -> {'a one-time notice' if d1 else '(silent)'}")
        print(f"  later prompt  -> {'(silent)' if d2 is None else 'REPEATED the notice'}")
        print(f"  => {'Disclosed once, then quiet — no stall.' if ok5 else '!!! the paused notice misbehaved'}")
        results.append(ok5)

        # PART 6 — surfacing does not promote a memory (no reinforcement)
        print("\n" + "=" * 80)
        print("PART 6 — surfacing a pointer does NOT bump the memory's standing (only USING it does)")
        print("=" * 80)
        _clear("p6")
        def reinforcements():
            return sum(1 for r in ledger.iter_records(path=cabinet)
                       if isinstance(r, dict) and r.get("kind") == records.REINFORCEMENT_KIND)
        b = reinforcements()
        run(_DEMO_STRONG_PROMPT, "p6")
        a = reinforcements()
        ok6 = (a - b) == 0
        print(f"\n  'used' marks before the scent: {b}")
        print(f"  'used' marks after the scent:  {a}")
        print(f"  => {'The push added no usage marks — surfacing is not using.' if ok6 else '!!! the scent reinforced'}")
        results.append(ok6)
    finally:
        for s in ("p1", "p2a", "p2b", "p3", "p4", "p5", "p6"):
            _clear(s)
        if prev is None:
            os.environ.pop(ledger.ENV_DIR, None)
        else:
            os.environ[ledger.ENV_DIR] = prev
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "-" * 80)
    print("What you just saw ran on a PRACTICE cabinet we filled for this demo. After you merge this, the engine")
    print("will quietly check memory on every prompt and, WHEN your words strongly match something stored, add a")
    print("short pointer (at most a few, the kinds + topics, NEVER the stored text itself) for the assistant to go")
    print("VERIFY — automatically, with no separate approval (it is a background hook). When nothing strongly")
    print("matches, it stays silent — which, on fresh or sparse memory, is most of the time, and is correct. It")
    print("is a nudge, not a guarantee; it deletes nothing; the assistant always verifies before relying on it.")
    print("\nVary it: edit the prompt, the memories, or attention's threshold and run again.")
    return 0 if all(results) else 1


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    if not argv or argv[0] == "hook":
        # Hook mode: what the wired UserPromptSubmit hook invokes. run_hook reads the event JSON from stdin,
        # runs the handler, translates inject -> structured stdout (additionalContext), fail-open on any error.
        return hooks.run_hook("UserPromptSubmit", handler)
    print("usage: scent.py [hook | demo]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
