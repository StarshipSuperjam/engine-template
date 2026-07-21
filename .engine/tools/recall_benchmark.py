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
  (below) is the private, uncommitted external-validity correlate; a real run of it is a NON-DEFERRABLE
  precondition on the eventual curation-removal.

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

- **Deterministic + reproducible.** Scoring uses `force_scan=True` — the machine-independent pure-Python
  ranking path (the FTS5 fast path can differ in top-k membership across environments); membership@k sidesteps
  the frecency tiebreak. Corpus timestamps are stamped RELATIVE to run time (records "born" minutes ago, well
  under the shortest role-weighted archival boundary), so no record drifts out of recall between runs and the
  baseline reproduces exactly.

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
  uv run --directory .engine --frozen -- python tools/recall_benchmark.py run --real-local   # private, read-only
"""

import argparse
import hashlib
import json
import os
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
                           "--real-local external-validity check (a real-world correlate; nothing committed)",
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

def synthetic_producer(ledger_file, index_file):
    """Old-path producer over a throwaway synthetic cabinet: the side-effect-free `index.search`, forced onto
    the machine-independent scan path so the baseline reproduces across environments."""
    def _run(question_text):
        return index.search(question_text, force_scan=True,
                            ledger_file=ledger_file, index_file=index_file).records
    return _run


def real_local_producer():
    """Old-path producer over the maintainer's REAL local ledger — read-only (`index.search` never writes),
    for the private `--real-local` external-validity check. Its output is printed, never committed."""
    def _run(question_text):
        return index.search(question_text, force_scan=True).records
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
    `cabinet_dir`. Relative stamping keeps every record recent, so none drifts across the archival boundary
    between runs. Returns (ledger_path, index_path)."""
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
    return seal, problems


# --- Runners -------------------------------------------------------------------------------------------

def run_synthetic(now=None):
    """Materialize the committed synthetic corpus, run the old-path producer, score, and summarize. Returns
    (summary, rows). Raises if the cabinet is broken (a positive question that should retrieve gets nothing)."""
    now = int(time.time()) if now is None else now
    corpus = load_corpus()
    questions = load_questions()
    with tempfile.TemporaryDirectory(prefix="recall-benchmark-") as cabinet:
        lpath, ipath = materialize(corpus, cabinet, now)
        rows = evaluate(corpus, questions, synthetic_producer(lpath, ipath))
    # Sanity gate (so a broken cabinet can't inflate the nothing-relevant class into false confidence):
    # at least one known-answer question whose answer is curated (old-path-reachable) must actually retrieve.
    reachable = [r for r in rows if r["content_type"] != "nothing-relevant" and r["answer_locus"] == "curated"]
    if reachable and not any(r["returned"] > 0 for r in reachable):
        raise SystemExit("recall_benchmark: cabinet appears broken — no curated question retrieved anything")
    return summarize(rows), rows


def _print_report(summary, rows, *, real_local=False):
    where = "your REAL local memory (private; not committed)" if real_local else "the synthetic corpus"
    print("Memory recall benchmark (G2) — old retrieval path, scored against %s\n" % where)
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
    if not real_local:
        shows = discrimination_gap_shows(summary)
        print("  Discrimination gap visible (old path fails the hard classes): %s" % ("YES" if shows else "NO"))


def cmd_run(real_local=False):
    if real_local:
        corpus = load_corpus()
        questions = load_questions()
        rows = evaluate(corpus, questions, real_local_producer())
        summary = summarize(rows)
        _print_report(summary, rows, real_local=True)
        print("\n  (This private, read-only check touched only your local memory and wrote nothing. Running it "
              "is the real-world correlate required before the eventual curation-removal.)")
        return 0
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
    id_to_session = {r[_ID]: r.get(_SESSION) for r in corpus}
    with tempfile.TemporaryDirectory(prefix="recall-benchmark-demo-") as cabinet:
        lpath, ipath = materialize(corpus, cabinet, now)
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
                     help="run against your REAL local memory instead (read-only; prints only; commits nothing)")
    sub.add_parser("demo", help="falsifiable self-check of the scorer")
    sub.add_parser("reseal", help="author-time: recompute the baseline and (re)write seal.json")
    args = parser.parse_args(argv)
    if args.cmd == "demo":
        return _demo()
    if args.cmd == "reseal":
        return cmd_reseal()
    if args.cmd == "run":
        return cmd_run(real_local=args.real_local)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
