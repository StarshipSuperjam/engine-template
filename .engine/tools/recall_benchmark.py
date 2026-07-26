"""recall_benchmark.py — the G2 memory-retrieval benchmark (construction-only; retires at first run).

This is the labeled instrument that measures memory RECALL quality and — at the far end of the memory
overhaul — the evidence that authorizes the irreversible removal of the curation lifecycle (eADR-0038 names
"a labeled benchmark" as the gate; #387 gate class G2 fixes its bar). It is maintainer-layer CONSTRUCTION
tooling, not a deployed check: it runs during the build and RETIRES at first run (a generated repo ships with
an empty ledger — nothing to benchmark).

Shape (settled with the maintainer + the thorough plan gate):

- **Fully synthetic corpus.** The corpus (`_fixtures/recall-benchmark/corpus.ndjson`) is invented from whole
  cloth — nothing from any real conversation touches this PUBLIC repo. That buys strong INTERNAL validity
  (planted, transparent ground truth a cold reviewer and the maintainer can read end to end) at the cost of
  weaker EXTERNAL validity (invented conversations are tidier than real ones). The `--real-local` mode
  (below) is the private, uncommitted, UNSCORED external-validity correlate — you ask real questions and
  judge the old path's real-memory results yourself; a real run of it is a NON-DEFERRABLE precondition on the
  eventual curation-removal.

- **A frozen PURE scorer, an injected producer.** The grading logic (`score_question`) is a pure function of
  a producer's ranked output — it never changes across slices. The RETRIEVAL PRODUCER is injected: the
  old-path producer here wraps the side-effect-free `index.search`; the future transcript-first path plugs in
  the same way WITHOUT editing the frozen scorer or the sealed corpus.

- **Path-agnostic scoring by SOURCE SESSION, with record-level for exact-wording.** The old path surfaces
  only the curated `episodic`/`gist` layer; the future new path surfaces raw `turn-delta` windows — the one
  unit both trace to is the source `session_id`. A hit@k credits a result whose traced session is expected
  (a cross-session gist is resolved through its `source_ids` back to real sessions — else its sentinel
  `session_id` would score a real hit as a miss and understate the old baseline). The `exact-wording` class
  scores at RECORD level (the property is verbatim recovery, not "some record from the right session"). The
  `nothing-relevant` class succeeds on PURE top-k emptiness — no salience threshold (that would be the
  post-hoc dial the freeze forbids).

- **Deterministic + reproducible.** Scoring uses `force_scan=True` — the pure-Python ranking path, which
  needs no FTS5 module and so reproduces on any machine; membership@k sidesteps the frecency tiebreak. Corpus
  timestamps are stamped RELATIVE to run time, which no longer guards against anything ageing out of recall
  (nothing does) but still keeps the frecency tiebreak identical between runs, so the baseline reproduces
  exactly.

- **A tamper-evident freeze.** `seal.json` pins a `sha256` over the corpus + questions, plus the numeric pass
  bar and the recorded old-path baseline; `verify_seal` (and a test) fail loudly if any sealed byte changes.
  This makes "frozen before the new path exists" enforced, not honor-system, and pins the bar so it cannot be
  quietly moved at deletion time.

Honest bound: the mechanical scorer measures top-k retrieval of a planted source; usefulness and the binding
"new beats old" judgment are HUMAN-judged over-and-above this number, at the curation-removal gate. This
harness computes the OLD-path baseline (the number the new path must beat) and proves it DISCRIMINATES — the
old lexical path visibly fails the paraphrase / raw-only / zero-lexical-overlap classes; an instrument the old
path already passes could not justify the deletion it gates.

Run:
  uv run --directory .engine --frozen -- python tools/recall_benchmark.py run     # synthetic baseline
  uv run --directory .engine --frozen -- python tools/recall_benchmark.py demo    # falsifiable self-check
  uv run --directory .engine --frozen -- python tools/recall_benchmark.py run --real-local --ask "…"  # private probe
"""

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # .engine/tools on path (for `from memory import`)
from memory import index, ledger, records  # noqa: E402

# --- Fixture locations (the committed, frozen artifact) ------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))                     # .../.engine/tools
_ENGINE = os.path.dirname(_HERE)                                       # .../.engine
_FIXTURES = os.path.join(_ENGINE, "_fixtures", "recall-benchmark")
CORPUS_PATH = os.path.join(_FIXTURES, "corpus.ndjson")
QUESTIONS_PATH = os.path.join(_FIXTURES, "questions.json")
SEAL_PATH = os.path.join(_FIXTURES, "seal.json")

K = 5                                          # recall@k — the top-five bar (#387 G2)
_ID = records.RECORD_ID_KEY
_SESSION = "session_id"
_AGE = "age_seconds"                           # corpus template field: stamped to ts = now - age at materialize

# The pinned pass bar (frozen into seal.json — eADR-0034's "written pass bar", unmovable once sealed).
# What "the old path" MEANS, sealed alongside the number it produced. The seal hashes the corpus, the
# questions, the bar and the baseline — it hashes no code, so without this the definition of the baseline could
# drift while the number stayed reassuringly at 0.49. That matters because the curation-removal slice strips
# supersession and reinforcement ranking from the same shared reader, with every incentive to keep the number
# steady. Changing this string is a deliberate re-seal, which is what the seal's own note already
# claims is true of the baseline.
FROZEN_OLD_PATH = ("the captured conversation (`turn-delta`) is ABSENT FROM THE SEARCHED CORPUS — the old-path "
                   "cabinet is materialized without those records, so they are missing from the ranking's own "
                   "corpus statistics and not merely filtered out of its results; forced scan path; no limit. "
                   "The committed corpus carries no unclosed batch and no supersession marker, so the "
                   "crash-orphan and roll-up exclusions `live_records` also applies are structurally inert on "
                   "it (a test pins that), and no archival age-out exists in the engine to apply.")

BAR = {
    "recall_at_k": K,
    "top5_threshold": 0.90,      # correct source in top-5 for >=90% of known-answer questions
    "conjunction": "the new path must BEAT the old on this set AND clear top5 >= 0.90 (human-judged, at the "
                   "curation-removal gate)",
    "nothing_relevant_rule": "success = pure top-k emptiness (no salience threshold)",
    "new_vs_old_rule": "the SAME mechanical session/record membership scorer is applied to each path's emitted "
                       "top-k; human usefulness is a SEPARATE overlay, never folded into this number",
    "discrimination": "the old-path baseline MUST be meaningfully sub-0.90 on the paraphrased / raw-only / "
                      "zero-lexical-overlap classes, or the instrument cannot justify the deletion it gates",
    "slice6_precondition": "before the irreversible curation-removal, the maintainer must have run the private "
                           "--real-local probe on real questions and judged real-memory recall FOR HIMSELF — an "
                           "UNSCORED, human-judged real-world correlate (there is no ground-truth label for the "
                           "real ledger); nothing is committed. Wiring this precondition to the deletion gate is "
                           "a REQUIREMENT on the removal slice, not enforced by this slice.",
}

CONTENT_TYPES = ("plain", "exact-wording", "superseded", "nothing-relevant", "lesson-recall")
VOCAB = ("original", "paraphrased")
# The classes whose answer the OLD lexical path is expected to struggle on — the discrimination gap must show here.
_HARD_LOCI = ("raw-only",)


# --- The frozen PURE scorer (never changes across slices) ----------------------------------------------

def trace_sessions(record, id_to_session):
    """The real source session(s) a returned record traces to. A normal record → its own `session_id`; a
    CROSS-SESSION gist carries a sentinel `session_id` (`tag:`/`sim:`) that is not a real session, so it is
    resolved through its `source_ids` back to the real sessions of the episodes it rolled up. `id_to_session`
    maps corpus record-id → session_id. Returns a set (possibly empty)."""
    sid = record.get(_SESSION)
    if records.is_cross_session_sentinel(sid):
        srcs = record.get(records.SOURCE_IDS_KEY) or []
        return {id_to_session[s] for s in srcs if s in id_to_session}
    return {sid} if sid else set()


def score_question(ranked, question, id_to_session, k=K):
    """Did the producer surface the correct source in the top-k? PURE — the whole grading law, frozen.

    `ranked` is the producer's best-first list of returned record dicts. Rules by class:
    - `nothing-relevant`: success = the top-k is EMPTY (pure emptiness — no threshold).
    - `exact-wording`: hit iff an expected RECORD id appears in the top-k (verbatim recovery, not session).
    - all others: hit iff a top-k result TRACES to an expected session, or is an expected record id.
    """
    topk = ranked[:k]
    ctype = question.get("content_type")
    if ctype == "nothing-relevant":
        return len(topk) == 0
    expected_sessions = set(question.get("expected_sessions") or ())
    expected_records = set(question.get("expected_record_ids") or ())
    for rec in topk:
        rid = rec.get(_ID)
        if rid in expected_records:
            return True
        if ctype != "exact-wording" and (trace_sessions(rec, id_to_session) & expected_sessions):
            return True
    return False


# --- The injected producers ----------------------------------------------------------------------------

def curated_only(corpus):
    """The corpus as the OLD path saw it — with the captured conversation removed ENTIRELY, not filtered out of
    the results afterwards.

    An earlier version of this did post-filter the ranked list, and argued the two were equivalent because the
    scan path's relevance was a per-record `log1p(tf)` that read no corpus statistics. That argument is gone:
    the scan now computes the same bm25 the FTS5 index does, and bm25 weighs every document against the corpus
    it sits in (document count, average length, per-term document frequency). Removing records from the RESULTS
    of a ranking computed over a corpus that still contained them is no longer the same thing as ranking a
    corpus that never had them. Building the cabinet without them is exact under any ranking law, which is the
    point — the definition of "the old path" must not need re-arguing every time the ranking changes."""
    return [r for r in corpus if r.get("kind") != records.AMBIENT_CAPTURE_KIND]


def synthetic_producer(ledger_file, index_file):
    """Old-path producer over a throwaway synthetic cabinet built by `curated_only`: the side-effect-free
    `index.search`, forced onto the machine-independent scan path so the baseline reproduces across
    environments. No filtering of its own — the cabinet it reads IS the old path's searched set."""
    def _run(question_text):
        return index.search(question_text, force_scan=True,
                            ledger_file=ledger_file, index_file=index_file).records
    return _run


def raw_visible_producer(ledger_file, index_file):
    """The NEW path's retrieval half over the same synthetic cabinet: identical call, identical frozen scorer,
    no old-path exclusion — so the only difference from `synthetic_producer` is whether the conversation itself
    is reachable. That isolation is the whole point; it is what makes the paired counts attributable."""
    def _run(question_text):
        return index.search(question_text, force_scan=True,
                            ledger_file=ledger_file, index_file=index_file).records
    return _run


def real_local_producer(*, raw_visible=False):
    """Producer over the maintainer's REAL local ledger — read-only (`index.search` never writes), for the
    private `--real-local` external-validity probe. Its output is printed, never committed.

    Defaults to the old-path arm so the probe keeps measuring what it was defined to measure; pass
    `raw_visible=True` for the other arm. Without that default this would have become a new-path measurement
    while still labelled the old-path probe — and running it is a stated precondition on the curation-removal
    gate.

    Here the old-path arm is a post-filter on the results, NOT the exact reconstruction the sealed synthetic
    baseline uses (`curated_only`): rebuilding a ~29 MB store without its conversation for a private read is
    not worth the cost, and this probe produces no scored number to protect — it is an UNSCORED, human-judged
    reading of what the maintainer's own memory returns. Stated so the difference is never mistaken for the
    sealed definition."""
    def _run(question_text):
        found = index.search(question_text, force_scan=True).records
        return found if raw_visible else [r for r in found
                                          if r.get("kind") != records.AMBIENT_CAPTURE_KIND]
    return _run


# --- The leak guard ------------------------------------------------------------------------------------

def _assert_not_live_store(*paths):
    """Fail loud if a benchmark cabinet path would resolve to the real memory store. The synthetic path must
    NEVER touch the live ledger/index (`index.search` defaults to `ledger.ledger_path()` when its path arg is
    omitted — a single missing argument would read the real ~private store). `--real-local` is the only
    sanctioned reader of the live store, and it goes through `real_local_producer`, never through here."""
    live = {os.path.realpath(ledger.ledger_path()), os.path.realpath(index.index_path())}
    for p in paths:
        if os.path.realpath(p) in live:
            raise SystemExit("recall_benchmark: refusing to operate a synthetic cabinet on the LIVE memory store")


# --- Corpus loading + materialization ------------------------------------------------------------------

def load_corpus(path=CORPUS_PATH):
    """Read the synthetic corpus template (NDJSON; each record carries `age_seconds`, not an absolute `ts`)."""
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def load_questions(path=QUESTIONS_PATH):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def materialize(corpus, cabinet_dir, now):
    """Stamp every record `ts = now - age_seconds` and write a throwaway ledger + rebuilt index in
    `cabinet_dir`. Relative stamping keeps every record the same age on every run, so the frecency tiebreak
    lands identically. The index is rebuilt so the cabinet is a complete stand-in store, but the canonical scoring
    path uses `force_scan=True` (machine-independent), so the FTS5 index is not the path the baseline depends
    on. Returns (ledger_path, index_path)."""
    lpath = os.path.join(cabinet_dir, "ledger.ndjson")
    ipath = os.path.join(cabinet_dir, "index.sqlite3")
    _assert_not_live_store(lpath, ipath)
    for rec in corpus:
        materialized = dict(rec)
        age = int(materialized.pop(_AGE, 0))
        materialized["ts"] = now - age
        ledger.append(materialized, path=lpath)
    index.rebuild(ledger_file=lpath, index_file=ipath)
    return lpath, ipath


# --- Evaluation + reporting ----------------------------------------------------------------------------

def evaluate(corpus, questions, producer, k=K):
    """Run every question through the producer and score it. Returns per-question rows."""
    id_to_session = {r[_ID]: r.get(_SESSION) for r in corpus}
    rows = []
    for q in questions:
        ranked = producer(q["question"])
        rows.append({
            "qid": q["qid"],
            "vocab": q.get("vocab"),
            "content_type": q.get("content_type"),
            "answer_locus": q.get("answer_locus"),
            "returned": len(ranked),
            "hit": bool(score_question(ranked, q, id_to_session, k)),
        })
    return rows


def _rate(rows):
    return (sum(1 for r in rows if r["hit"]), len(rows))


def summarize(rows):
    """Aggregate + per-axis raw counts. Percentages are reported only for the overall known-answer set; small
    per-class n is reported as a raw count (e.g. "6/7"), never a headline percentage it cannot support."""
    known = [r for r in rows if r["content_type"] != "nothing-relevant"]
    nothing = [r for r in rows if r["content_type"] == "nothing-relevant"]
    by_vocab = {v: _rate([r for r in known if r["vocab"] == v]) for v in VOCAB}
    by_ctype = {c: _rate([r for r in rows if r["content_type"] == c]) for c in CONTENT_TYPES}
    hard = _rate([r for r in known if r.get("answer_locus") in _HARD_LOCI or r["vocab"] == "paraphrased"])
    hit_known, n_known = _rate(known)
    return {
        "overall_known": {"hits": hit_known, "n": n_known,
                          "recall_at_k": round(hit_known / n_known, 3) if n_known else None},
        "nothing_relevant": {"correct": _rate(nothing)[0], "n": len(nothing)},
        "by_vocab": by_vocab,
        "by_content_type": by_ctype,
        "hard_classes": {"hits": hard[0], "n": hard[1],
                         "recall_at_k": round(hard[0] / hard[1], 3) if hard[1] else None},
    }


def discrimination_gap_shows(summary):
    """The instrument must DISCRIMINATE: the old lexical path must visibly fail the hard (paraphrased /
    raw-only) classes. True iff the hard-class recall is meaningfully below the 0.90 bar."""
    hard = summary["hard_classes"]
    return hard["n"] > 0 and hard["recall_at_k"] is not None and hard["recall_at_k"] < BAR["top5_threshold"]


# --- The seal (tamper-evident freeze) ------------------------------------------------------------------

def _sha256_file(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def compute_seal(baseline_summary):
    return {
        "corpus_sha256": _sha256_file(CORPUS_PATH),
        "questions_sha256": _sha256_file(QUESTIONS_PATH),
        "bar": BAR,
        "frozen_old_path": FROZEN_OLD_PATH,
        "old_path_baseline": baseline_summary,
        "note": ("Frozen before the transcript-first path exists. The sha256s + the pinned bar + the recorded "
                 "old-path baseline are the anti-gaming lock: a change to the corpus, the questions, the bar, "
                 "or the baseline is a DELIBERATE re-seal (run `reseal`), never a silent edit. A test "
                 "(test_recall_benchmark) fails if a sealed byte changes without a re-seal."),
    }


def verify_seal():
    """Return (seal, problems). `problems` is empty when the committed corpus + questions still match the seal."""
    if not os.path.exists(SEAL_PATH):
        return None, ["seal.json is missing — the frozen set is unsealed"]
    with open(SEAL_PATH, encoding="utf-8") as fh:
        seal = json.load(fh)
    problems = []
    if _sha256_file(CORPUS_PATH) != seal.get("corpus_sha256"):
        problems.append("corpus.ndjson changed since the seal — the frozen set was edited without a re-seal")
    if _sha256_file(QUESTIONS_PATH) != seal.get("questions_sha256"):
        problems.append("questions.json changed since the seal — the frozen set was edited without a re-seal")
    if seal.get("bar") != BAR:
        problems.append("the pinned pass bar in code no longer matches the sealed bar — it was moved without a "
                        "re-seal (the baseline reproduction check separately guards the recorded baseline)")
    if seal.get("frozen_old_path") != FROZEN_OLD_PATH:
        problems.append("the frozen old-path definition in code no longer matches the sealed one — what counts "
                        "as 'the old path' was redefined without a re-seal")
    return seal, problems


# --- Runners -------------------------------------------------------------------------------------------

# --- The query-decomposition measurement (the read-time workflow's first step, scored) ------------------
# The recall workflow's load-bearing move is REPHRASING: memory's search is a strict implicit-AND keyword
# floor, so a whole natural-language question matches nothing and a question worded unlike the original
# conversation matches nothing either. The workflow gives that rephrasing to the session's model, which no
# fixed harness can score. What CAN be scored is the mechanical FLOOR of it: split the question into short
# search phrases and search each, then pool. No vocabulary knowledge of any kind — no synonyms, no thesaurus,
# no authored artifact — run through the SAME frozen scorer and the SAME producer slot as the old path, so the
# difference is attributable to query strategy alone.
#
# WHY NO SYNONYM MAP. An earlier version of this measurement carried a committed synonym map. A control run —
# the identical producer with the map emptied — scored BETTER without it (8/22 against 3/22), because synonym
# variants crowded out the later phrases under the fan-out cap; and raised to an unbounded fan-out the same map
# recovered 22/22 of deliberately zero-overlap rewordings, which no general English map could do. Both facts
# said the map was fitted to this corpus rather than general, so it was deleted rather than defended. What
# remains needs no fairness argument: decomposition cannot be tuned toward answers it has no vocabulary for.
#
# WHAT THIS NUMBER IS. A genuine floor for the rephrasing step — the value of merely splitting the question,
# with zero understanding. A model rephrasing in context has vocabulary this does not and should beat it.

_EXPANSION_LIMIT = 10        # per-phrase cap — the operation doc's rule (search is unbounded by default)
_MAX_PHRASES = 8             # bound the fan-out so the stand-in stays a search strategy, not a store dump

# Ordinary English function words. Generic by construction — no project or corpus vocabulary appears here, so
# there is nothing in this list that could aim the measurement at a planted answer.
STOPWORDS = frozenset("""
a about after against all an and any are as at be been before being between both but by can did do does
doing done down during each few for from further had has have having he her here hers him his how i if in
into is it its itself just me more most my no nor not now of off on once only or other our ours out over own
same she should so some such than that the their theirs them then there these they this those through to too
under until up very was we were what when where which while who whom why will with you your yours
""".split())


def expand_query(question_text, stopwords=None):
    """A question -> a handful of SHORT search phrases. Purely mechanical and question-only: drop stopwords,
    then pair adjacent content words (search demands every word of a phrase in one record, so a whole sentence
    matches nothing), then single-word anchors as the broadest fallback. Deterministic and order-stable, so
    the measurement reproduces exactly."""
    stop = STOPWORDS if stopwords is None else stopwords
    words = [w for w in re.findall(r"[a-z0-9]+", (question_text or "").lower()) if w and w not in stop]
    phrases, seen = [], set()

    def _add(parts):
        phrase = " ".join(parts)
        if phrase and phrase not in seen:
            seen.add(phrase)
            phrases.append(phrase)

    for a, b in zip(words, words[1:]):          # adjacent content-word pairs, in question order
        _add([a, b])
    for w in words:                             # single anchors last (broadest, so lowest priority)
        _add([w])
    return phrases[:_MAX_PHRASES]


def expanded_producer(ledger_file, index_file):
    """The decomposition stand-in in the injected-producer slot: search EACH phrase under a per-phrase limit,
    then union the hits preserving first-seen order (the pooled set is what the workflow judges).
    Side-effect-free — the same `index.search` the old path uses, forced onto the machine-independent scan."""
    def _run(question_text):
        pooled, seen = [], set()
        for phrase in expand_query(question_text):
            # The per-phrase cap is passed into the search itself, which is safe here BECAUSE the cabinet this
            # reads was built by `curated_only`: there is no conversation in it to displace a curated hit
            # inside the top-10. An earlier version searched unlimited and post-filtered for exactly that
            # reason; removing the records from the corpus instead removes the hazard rather than working
            # around it.
            found = index.search(phrase, force_scan=True, limit=_EXPANSION_LIMIT,
                                 ledger_file=ledger_file, index_file=index_file).records
            for record in found:
                rid = record.get(_ID)
                if rid in seen:
                    continue
                seen.add(rid)
                pooled.append(record)
        return pooled
    return _run


def run_expanded():
    """The decomposition stand-in over the same committed corpus, scored by the same frozen scorer."""
    now = int(time.time())
    corpus = load_corpus()
    questions = load_questions()
    with tempfile.TemporaryDirectory(prefix="recall-benchmark-exp-") as cabinet:
        lpath, ipath = materialize(curated_only(corpus), cabinet, now)
        rows = evaluate(corpus, questions, expanded_producer(lpath, ipath))
    return summarize(rows), rows


def run_synthetic():
    """Materialize the committed synthetic corpus, run the old-path producer, score, and summarize. Returns
    (summary, rows). Raises if the cabinet is broken (a positive question that should retrieve gets nothing).

    Timestamps are stamped relative to the real current time (see `materialize`) — deliberately NOT injectable,
    because `index.search` reads the real wall clock internally for its frecency tiebreak, so a past `now` here
    would rank against one clock what was stamped against another."""
    now = int(time.time())
    corpus = load_corpus()
    questions = load_questions()
    with tempfile.TemporaryDirectory(prefix="recall-benchmark-") as cabinet:
        lpath, ipath = materialize(curated_only(corpus), cabinet, now)
        rows = evaluate(corpus, questions, synthetic_producer(lpath, ipath))
    # Sanity gate (so a broken cabinet can't inflate the nothing-relevant class into false confidence):
    # at least one known-answer question whose answer is curated (old-path-reachable) must actually retrieve.
    reachable = [r for r in rows if r["content_type"] != "nothing-relevant" and r["answer_locus"] == "curated"]
    if reachable and not any(r["returned"] > 0 for r in reachable):
        raise SystemExit("recall_benchmark: cabinet appears broken — no curated question retrieved anything")
    return summarize(rows), rows


def _print_report(summary, rows):
    print("Memory recall benchmark (G2) — old retrieval path, scored against the synthetic corpus\n")
    ok = summary["overall_known"]
    print("  Overall (known-answer questions): %d/%d correct source in top-%d  (recall@%d = %s)"
          % (ok["hits"], ok["n"], K, K, ok["recall_at_k"]))
    nr = summary["nothing_relevant"]
    print("  'Nothing relevant' handled correctly: %d/%d" % (nr["correct"], nr["n"]))
    print("\n  By vocabulary (the axis the overhaul targets):")
    for v in VOCAB:
        hits, n = summary["by_vocab"][v]
        print("    %-12s %d/%d" % (v, hits, n))
    print("\n  By question type:")
    for c in CONTENT_TYPES:
        hits, n = summary["by_content_type"][c]
        print("    %-16s %d/%d" % (c, hits, n))
    hard = summary["hard_classes"]
    print("\n  Hard classes (paraphrased / raw-only): %d/%d  (recall@%d = %s)"
          % (hard["hits"], hard["n"], K, hard["recall_at_k"]))
    shows = discrimination_gap_shows(summary)
    print("  Discrimination gap visible (old path fails the hard classes): %s" % ("YES" if shows else "NO"))
    print("\n  (This old-path baseline is a FLOOR: every paraphrase is worded to zero lexical overlap, so the "
          "old lexical search misses them by construction. Beating it mechanically is therefore easy — the real "
          "bar is the absolute >=90% top-5 AND the human-judged usefulness pass at the curation-removal gate.)")


def cmd_run():
    seal, problems = verify_seal()
    summary, rows = run_synthetic()
    _print_report(summary, rows)
    if problems:
        print("\n  ! FROZEN-SET INTEGRITY: " + "; ".join(problems))
        return 1
    if seal is not None:
        sealed = seal.get("old_path_baseline", {}).get("overall_known", {}).get("recall_at_k")
        live = summary["overall_known"]["recall_at_k"]
        print("\n  Sealed baseline recall@%d = %s; this run = %s  (%s)"
              % (K, sealed, live, "reproduced" if sealed == live else "DIVERGED — investigate"))
    return 0


def run_raw_visible():
    """The same committed corpus and the same frozen scorer, with the conversation reachable."""
    now = int(time.time())
    corpus = load_corpus()
    questions = load_questions()
    with tempfile.TemporaryDirectory(prefix="recall-benchmark-raw-") as cabinet:
        lpath, ipath = materialize(corpus, cabinet, now)
        rows = evaluate(corpus, questions, raw_visible_producer(lpath, ipath))
    return summarize(rows), rows


def cmd_raw():
    """Score the frozen old path against the same path with the conversation reachable — the paired counts for
    making raw searchable, attributable because only reachability differs.

    Reports the instrument's limits ALONGSIDE the numbers, because here they are the more important half. The
    sealed corpus holds three `turn-delta` records in two raw-only sessions, and all four raw-only questions are
    original-vocabulary lookups on invented tokens that appear in no other record — so a clean 4/4 demonstrates
    that indexing works, NOT that recall improved on the failure this overhaul exists to fix. There is not one
    paraphrased raw-only question in the set. And the `nothing-relevant` questions share no token with the three
    added records, so 6/6 here is not evidence of precision: it is a check that cannot fail at this scale.
    The real reading is the private `--real-local` probe against the maintainer's own store."""
    old, old_rows = run_synthetic()
    new, new_rows = run_raw_visible()

    # The raw-only tally is computed HERE from the rows, deliberately not added to `summarize` — that dict is
    # what `compute_seal` records as the baseline, and a reporting convenience must not reshape a sealed value.
    def _line(label, s, rows):
        raw = _rate([r for r in rows if r.get("answer_locus") == "raw-only"])
        return ("  %-26s overall %-6s   raw-only %d/%d   paraphrased %s/%s   nothing-relevant %s/%s"
                % (label, s["overall_known"]["recall_at_k"], raw[0], raw[1],
                   s["by_vocab"]["paraphrased"][0], s["by_vocab"]["paraphrased"][1],
                   s["nothing_relevant"]["correct"], s["nothing_relevant"]["n"]))

    print("Memory recall benchmark (G2) — what does making the conversation searchable buy?\n")
    print(_line("conversation excluded", old, old_rows))
    print(_line("conversation searchable", new, new_rows))
    print("\n  READ THE LIMITS BEFORE THE NUMBERS. The frozen corpus holds three conversation records across")
    print("  two sessions, and all four raw-only questions are original-vocabulary lookups on invented tokens")
    print("  found nowhere else — so a clean result there says indexing works, not that recall got better at")
    print("  the paraphrased question this whole overhaul exists to fix. The set contains no paraphrased")
    print("  raw-only question at all. And the 'should find nothing' questions share no word with the three")
    print("  added records, so that column cannot move at this scale and is not evidence of precision.")
    print("\n  A drop in any other column IS meaningful — it would be conversation crowding the summaries out,")
    print("  which is the documented failure the earlier exclusion was put in to fix.")
    old_n, new_n = old["overall_known"]["recall_at_k"], new["overall_known"]["recall_at_k"]
    print("\n  Sealed old-path baseline %s (unmoved by this measurement); conversation searchable %s."
          % (old_n, new_n))
    return 0


def cmd_expanded():
    """Score the old path and the expansion stand-in side by side, so the gain is attributable to rephrasing
    alone (same corpus, same frozen scorer, same producer slot — only the query strategy differs)."""
    old, _ = run_synthetic()
    new, _ = run_expanded()

    def _line(label, s):
        return ("  %-26s overall %-6s   paraphrased %s/%s   hard classes %s/%s   nothing-relevant %s/%s"
                % (label, s["overall_known"]["recall_at_k"],
                   s["by_vocab"]["paraphrased"][0], s["by_vocab"]["paraphrased"][1],
                   s["hard_classes"]["hits"], s["hard_classes"]["n"],
                   s["nothing_relevant"]["correct"], s["nothing_relevant"]["n"]))

    para_new, para_n = new["by_vocab"]["paraphrased"]
    para_old = old["by_vocab"]["paraphrased"][0]

    print("Memory recall benchmark (G2) — does splitting the question into short searches help?\n")
    print(_line("one whole-question query", old))
    print(_line("split into short phrases", new))
    print("\n  Every number above is 'how often the right source landed in the top 5'.")
    print("  The headline: of %d questions deliberately reworded to share NO words with what was actually"
          % para_n)
    print("  said, one whole-question search found %d. Splitting the same question into short phrases and"
          % para_old)
    print("  searching each finds %d — and the questions that should correctly find NOTHING are unchanged,"
          % para_new)
    print("  so the gain is retrieval rather than a wider net catching noise.")
    print("\n  This is a genuine FLOOR for the rephrasing step: it is pure mechanism — splitting on word")
    print("  boundaries, with no synonyms, no thesaurus and no vocabulary of any kind, so nothing about it")
    print("  can be aimed at the planted answers. The real workflow gives this step to the session's model,")
    print("  which understands the question and should do better. What it does NOT measure is whether the")
    print("  results are judged well once found — that is the human-judged half, at the removal gate.")
    if new["nothing_relevant"]["correct"] < old["nothing_relevant"]["correct"]:
        print("\n  ! Searching more ways cost accuracy on questions that SHOULD find nothing — a real regression.")
        return 1
    return 0


def cmd_reseal():
    """Author-time only: recompute the old-path baseline over the committed corpus and (re)write seal.json.
    Deliberate by construction — this is how the freeze is (re)established, and the change shows in the diff."""
    summary, _rows = run_synthetic()
    seal = compute_seal(summary)
    with open(SEAL_PATH, "w", encoding="utf-8") as fh:
        json.dump(seal, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print("Re-sealed. old-path baseline recall@%d = %s; corpus %s… questions %s…"
          % (K, summary["overall_known"]["recall_at_k"], seal["corpus_sha256"][:12], seal["questions_sha256"][:12]))
    if not discrimination_gap_shows(summary):
        print("  ! WARNING: the discrimination gap does NOT show — the old path passes the hard classes; the "
              "instrument would not justify the deletion it gates. Harden the paraphrase / raw-only classes.")
    return 0


def cmd_real_local(asks, *, raw_visible=False):
    """The private, read-only, HUMAN-JUDGED external-validity probe. There is NO ground-truth label for the
    real ledger, so this is deliberately UNSCORED: for each real question you pass, it prints the records the
    OLD retrieval path surfaces from YOUR real local memory, for YOU to judge whether the right memory came
    back. It reads only (`index.search` never writes) and commits nothing. A real run of this — you satisfying
    yourself that recall works on your own messy questions — is the real-world correlate reserved for the
    eventual curation-removal gate (the synthetic baseline measures the mechanics; this measures reality)."""
    if not asks:
        print("Pass one or more REAL questions to probe your own memory (unscored — you judge the results):\n"
              "  ... recall_benchmark.py run --real-local --ask \"when did we decide to keep the erasure wall\"\n"
              "  ... --ask \"what did we learn about the secret scrubber\"")
        return 0
    producer = real_local_producer(raw_visible=raw_visible)
    print("Probing the %s against your REAL local memory (read-only; nothing written).\n"
          % ("path WITH the conversation searchable" if raw_visible else "FROZEN OLD retrieval path"))
    for question in asks:
        ranked = producer(question)
        print("Q: %s" % question)
        if not ranked:
            print("   (the old path surfaced nothing)\n")
            continue
        for rec in ranked[:K]:
            kind = rec.get("role") or rec.get("kind") or "?"
            text = " ".join((rec.get("text") or "").split())
            snippet = text[:160] + ("…" if len(text) > 160 else "")
            print("   • [%s] %s" % (kind, snippet))
        print("")
    print("Judge for yourself whether the right memory came back. This unscored, private read is the real-world "
          "correlate reserved for the curation-removal gate — nothing here was scored or committed.")
    return 0


# --- The falsifiable demo (must be able to FAIL) -------------------------------------------------------

def _demo() -> int:
    """A self-check over the REAL scorer + REAL index.search on a tiny planted cabinet. It is BUILT TO FAIL:
    each assertion below catches a specific scorer defect, and a wrong scorer makes the demo exit non-zero.
    Covers: a curated hit scores a hit; a raw-only answer the old path can't reach scores a miss; a
    'nothing relevant' question scores correct on emptiness; a cross-session gist hit is credited through its
    source_ids; and a deliberately-WRONG label scores a miss."""
    now = int(time.time())
    ok = True

    def check(label, cond):
        nonlocal ok
        print("  [%s] %s" % ("PASS" if cond else "FAIL", label))
        ok = ok and cond

    corpus = [
        {_ID: "d-ep", _SESSION: "d-s1", _AGE: 60, "role": "decision", "tags": ["episodic"],
         "text": "the widget cache eviction interval was set to ninety seconds"},
        {_ID: "d-raw", _SESSION: "d-s2", _AGE: 60, "kind": records.AMBIENT_CAPTURE_KIND, "tags": [],
         "text": "the raw turn only mentions a peculiar zamboni heuristic never summarized"},
        {_ID: "d-src", _SESSION: "d-s3", _AGE: 90, "role": "lesson", "tags": ["episodic"],
         "text": "a lesson about flumox retries living in a rolled-up session"},
        {_ID: "d-gist", _SESSION: "tag:flumox", _AGE: 30, "kind": records.GIST_KIND, "tags": ["gist"],
         "text": "gist rolling up the flumox retries lesson across sessions",
         records.SOURCE_IDS_KEY: ["d-src"]},
    ]
    questions = [
        {"qid": "q-hit", "content_type": "plain", "vocab": "original", "answer_locus": "curated",
         "question": "widget cache eviction interval", "expected_sessions": ["d-s1"]},
        {"qid": "q-raw", "content_type": "plain", "vocab": "original", "answer_locus": "raw-only",
         "question": "peculiar zamboni heuristic", "expected_sessions": ["d-s2"]},
        {"qid": "q-none", "content_type": "nothing-relevant", "vocab": "original", "answer_locus": "none",
         "question": "quarterly budget for the marketing offsite", "expected_sessions": []},
        {"qid": "q-gist", "content_type": "plain", "vocab": "original", "answer_locus": "curated",
         "question": "flumox retries", "expected_sessions": ["d-s3"]},
    ]
    id_to_session = {r[_ID]: r.get(_SESSION) for r in corpus}   # the FULL corpus: the scorer must still trace ids
    with tempfile.TemporaryDirectory(prefix="recall-benchmark-demo-") as cabinet:
        # The old path's cabinet is built WITHOUT the conversation (`curated_only`), which is what makes the
        # raw-only question below a genuine miss rather than one arranged by a post-filter.
        lpath, ipath = materialize(curated_only(corpus), cabinet, now)
        producer = synthetic_producer(lpath, ipath)
        results = {q["qid"]: producer(q["question"]) for q in questions}

    def scored(qid):
        q = next(q for q in questions if q["qid"] == qid)
        return score_question(results[qid], q, id_to_session)

    check("a curated answer is found (hit)", scored("q-hit") is True)
    check("a raw-only answer the old path can't reach is a miss", scored("q-raw") is False)
    check("a 'nothing relevant' question is correct on emptiness", scored("q-none") is True)
    check("a cross-session gist hit is credited via its source_ids", scored("q-gist") is True)

    # A deliberately-WRONG label must NOT score a hit — proves the scorer isn't rubber-stamping.
    wrong = {"qid": "q-wrong", "content_type": "plain", "vocab": "original", "answer_locus": "curated",
             "question": "widget cache eviction interval", "expected_sessions": ["d-s2"]}  # wrong session
    check("a wrong label scores a miss (no rubber-stamp)",
          score_question(results["q-hit"], wrong, id_to_session) is False)

    # The leak guard must refuse the live store.
    guard_fired = False
    try:
        _assert_not_live_store(ledger.ledger_path())
    except SystemExit:
        guard_fired = True
    check("the leak guard refuses the live memory store", guard_fired)

    print("\nDemo %s." % ("passed" if ok else "FAILED"))
    return 0 if ok else 1


# --- CLI -----------------------------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description="G2 memory-recall benchmark (construction-only).")
    sub = parser.add_subparsers(dest="cmd")
    run = sub.add_parser("run", help="score the old retrieval path against the synthetic set")
    run.add_argument("--real-local", action="store_true",
                     help="instead, probe your REAL local memory (read-only, UNSCORED, prints only, commits "
                          "nothing) — pass real questions with --ask")
    run.add_argument("--ask", action="append", default=[], metavar="QUESTION",
                     help="a real question for --real-local (repeatable)")
    run.add_argument("--expanded", action="store_true",
                     help="also score the query-expansion stand-in and show the gain over the sealed baseline")
    run.add_argument("--raw", action="store_true",
                     help="instead, score the frozen old path against the same path with the conversation "
                          "searchable (reports the instrument's limits alongside the counts)")
    run.add_argument("--raw-visible", action="store_true",
                     help="with --real-local: probe with the conversation searchable rather than the frozen "
                          "old path, for the paired real-store reading")
    sub.add_parser("demo", help="falsifiable self-check of the scorer")
    sub.add_parser("reseal", help="author-time: recompute the baseline and (re)write seal.json")
    args = parser.parse_args(argv)
    if args.cmd == "demo":
        return _demo()
    if args.cmd == "reseal":
        return cmd_reseal()
    if args.cmd == "run":
        if args.real_local:
            return cmd_real_local(args.ask, raw_visible=args.raw_visible)
        if args.raw:
            return cmd_raw()
        return cmd_expanded() if args.expanded else cmd_run()
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
