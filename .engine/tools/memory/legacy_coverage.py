"""Legacy summary/gist coverage census — a READ-ONLY audit (StarshipSuperjam/engine-template#1053).

It answers one question honestly: for the episodic summaries and gists already sitting in the raw
ledger, are the things they point at — the session a summary belongs to, the raw episodes a gist folded —
still retrievable from the ledger, or gone? It reads the RAW ledger through `ledger.read()` and nothing
else: NEVER the search index, NEVER the recall generator, NEVER a read-only index view (the search index served read-only) — every one of
those DROPS withheld and superseded records, which are exactly the records this census must SEE so it can
label them "retained, not gone" rather than mistake them for lost. It never reads or reports record TEXT:
only ids, kinds, states, and counts.

Three distinct states, per subject (a referenced session, or a source-record id):
  present              — a line for it is physically in the ledger and is not excluded
  absent               — no line for it exists
  excluded-but-present — a line exists but is withheld or superseded: retained, not gone

Honesty rails, because a census that quietly guesses is worse than none:
  * read-health. `ledger.read()` reports the malformed and torn lines that `ledger.iter_records` would
    silently skip and never witness. Any such line makes the whole run INDETERMINATE — a lost `superseded`
    or `withheld` marker could mislabel any subject — and the counts are surfaced, never hidden.
  * consistency fingerprint. `(generation, index_epoch, sha256 of the ledger bytes)` is taken at the START
    and again at the END of the pass; any difference marks the run INDETERMINATE. The byte digest catches
    even a same-length replacement that a length-or-offset check cannot.
  * unknown denominator. The original turn count is UNKNOWABLE, so coverage as a fraction of all turns is
    explicitly UNKNOWN. This census reports only what the summaries/gists reference and whether it
    survives — never an "X% covered".

The withheld/superseded derivation below is re-expressed over the already-read record list on purpose. The
canonical live-recall counterparts live in `forget.py` (`withheld_targets`, `_superseded_by_map`), but they
each open the ledger through `iter_records`, which cannot carry read-health and would be a second,
possibly-inconsistent read. Deriving over the one `ledger.read()` snapshot keeps the census honest and its
fingerprint meaningful; the positional "last marker in ledger order wins" rule is preserved verbatim.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field

# Make the package parent (.engine/tools) importable so `from memory import ledger` resolves even when this
# file is run directly as a script to print the live numbers. Imported as `memory.legacy_coverage` (under the
# test suite), the parent is already on sys.path, so this is a guarded no-op. Module-level imports stay limited
# to the cycle-free set `ledger` + `records`.
_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from memory import ledger, records  # noqa: E402

PRESENT = "present"
ABSENT = "absent"
EXCLUDED_BUT_PRESENT = "excluded-but-present"
_STATES = (PRESENT, ABSENT, EXCLUDED_BUT_PRESENT)

EPISODIC_SESSION = "episodic-session"      # an episodic summary's referenced session
GIST_SESSION = "gist-session"              # a gist's referenced session (real sessions only, not cluster sentinels)
GIST_SOURCE = "gist-source-record"         # one raw episode id a gist folded (its SOURCE_IDS_KEY entries)
_SUBJECT_KINDS = (EPISODIC_SESSION, GIST_SESSION, GIST_SOURCE)

# A referenced session is "present" only when its underlying CONVERSATION still survives — not when the
# summary/gist line itself (which of course carries the session_id) is in the ledger. So session-presence is
# measured over conversation-bearing records, excluding the derived summaries/gists and the pure bookkeeping
# markers. This is what makes ABSENT reachable and meaningful: a session whose turns were compacted away
# leaves its gist behind but no conversation, and that is exactly the loss StarshipSuperjam/engine-template#1053 asks about.
_DERIVED_OR_BOOKKEEPING_KINDS = frozenset((
    records.EPISODIC_KIND, records.GIST_KIND, records.MARKER_KIND, records.ROLLUP_KIND,
    records.SUPERSEDED_KIND, records.WITHHOLD_KIND, records.RESTORE_KIND, records.REINFORCEMENT_KIND,
    records.PIN_KIND, records.ERASURE_KIND,
))


def _is_conversation(record) -> bool:
    """True iff `record` is conversation the summaries were made FROM, not a derived summary/gist or a
    bookkeeping marker. Only these count toward whether a referenced session still survives."""
    return isinstance(record, dict) and record.get("kind") not in _DERIVED_OR_BOOKKEEPING_KINDS


@dataclass
class CensusResult:
    scanned: dict = field(default_factory=dict)
    counts: dict = field(default_factory=dict)
    rows: list = field(default_factory=list)
    read_health: dict = field(default_factory=dict)
    indeterminate: bool = False
    indeterminate_reasons: list = field(default_factory=list)
    fingerprint_start: list = field(default_factory=list)
    fingerprint_end: list = field(default_factory=list)
    denominator_unknown: bool = True

    def as_dict(self) -> dict:
        return {
            "scanned": self.scanned,
            "counts": self.counts,
            "rows": self.rows,
            "read_health": self.read_health,
            "indeterminate": self.indeterminate,
            "indeterminate_reasons": self.indeterminate_reasons,
            "fingerprint_start": self.fingerprint_start,
            "fingerprint_end": self.fingerprint_end,
            "denominator_unknown": self.denominator_unknown,
        }


def _ledger_digest(path) -> str:
    target = path or ledger.ledger_path()
    try:
        with open(target, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except FileNotFoundError:
        return hashlib.sha256(b"").hexdigest()


def _fingerprint(path):
    """(generation, index_epoch, sha256-of-bytes) — the triple whose change means the ledger moved under us."""
    return (ledger.generation(for_path=path), ledger.index_epoch(for_path=path), _ledger_digest(path))


def _closed_rollup_batches(recs) -> set:
    return {r.get(records.BATCH_KEY) for r in recs
            if isinstance(r, dict) and r.get("kind") == records.ROLLUP_KIND
            and isinstance(r.get(records.BATCH_KEY), str) and r.get(records.BATCH_KEY)}


def _superseded_ids(recs, closed_rollup) -> set:
    """Raw episode ids a COMPLETED roll-up retired — a closed-batch `superseded` marker names them, OR a
    compaction folded the supersession into the raw's own carried `superseded_by` field. A marker in an
    un-closed (crashed) batch is INERT and contributes nothing: the crash-safety that a raw is hidden only
    once its gist's pass finished."""
    out = set()
    for r in recs:
        if not isinstance(r, dict):
            continue
        if r.get("kind") == records.SUPERSEDED_KIND:
            batch = r.get(records.BATCH_KEY)
            raw = r.get(records.TARGET_KEY)
            if isinstance(batch, str) and batch in closed_rollup and isinstance(raw, str) and raw:
                out.add(raw)
        sb = r.get(records.SUPERSEDED_BY_KEY)
        rid = r.get(records.RECORD_ID_KEY)
        if isinstance(sb, str) and sb and isinstance(rid, str) and rid:
            out.add(rid)
    return out


def _withheld_targets(recs):
    """`({record_id, ...}, {session_id, ...})` currently withheld. Positional pass, LAST MARKER WINS — the
    same rule `forget.withheld_targets` uses, and for the same reason: capture stamps whole seconds, so a
    withhold-then-restore can tie on `ts` and only ledger order resolves the operator's latest intent."""
    ids, sessions = set(), set()
    for r in recs:
        if not isinstance(r, dict):
            continue
        kind = r.get("kind")
        if kind not in (records.WITHHOLD_KIND, records.RESTORE_KIND):
            continue
        hiding = kind == records.WITHHOLD_KIND
        rid = r.get(records.TARGET_KEY)
        sid = r.get(records.TARGET_SESSION_KEY)
        if isinstance(rid, str) and rid:
            ids.add(rid) if hiding else ids.discard(rid)
        elif isinstance(sid, str) and sid:
            sessions.add(sid) if hiding else sessions.discard(sid)
    return ids, sessions


def _state_for_id(subject_id, present_ids, excluded_ids) -> str:
    if subject_id not in present_ids:
        return ABSENT
    if subject_id in excluded_ids:
        return EXCLUDED_BUT_PRESENT
    return PRESENT


def _state_for_session(sid, by_session, excluded_ids, withheld_sessions) -> str:
    line_ids = by_session.get(sid)
    if not line_ids:
        return ABSENT
    if sid in withheld_sessions:
        return EXCLUDED_BUT_PRESENT
    if any(lid not in excluded_ids for lid in line_ids):
        return PRESENT
    return EXCLUDED_BUT_PRESENT


def census(path=None) -> CensusResult:
    """Run the census over one `ledger.read()` snapshot, fingerprinted start and end."""
    fp_start = _fingerprint(path)
    read = ledger.read(path=path)
    recs = read.records

    present_ids = {r.get(records.RECORD_ID_KEY) for r in recs
                   if isinstance(r, dict) and isinstance(r.get(records.RECORD_ID_KEY), str)
                   and r.get(records.RECORD_ID_KEY)}

    by_session: dict = {}   # session_id -> conversation record ids (derived summaries/gists excluded)
    for r in recs:
        if not _is_conversation(r):
            continue
        sid = r.get("session_id")
        rid = r.get(records.RECORD_ID_KEY)
        if isinstance(sid, str) and sid and isinstance(rid, str) and rid:
            by_session.setdefault(sid, []).append(rid)

    closed_rollup = _closed_rollup_batches(recs)
    excluded_ids = _superseded_ids(recs, closed_rollup)
    withheld_ids, withheld_sessions = _withheld_targets(recs)
    excluded_ids |= withheld_ids

    counts = {k: {s: 0 for s in _STATES} for k in _SUBJECT_KINDS}
    rows: list = []
    scanned = {"episodics": 0, "gists": 0, "gist_cross_session_clusters": 0}

    for r in recs:
        if not isinstance(r, dict):
            continue
        kind = r.get("kind")
        rid = r.get(records.RECORD_ID_KEY)
        if kind == records.EPISODIC_KIND:
            scanned["episodics"] += 1
            sid = r.get("session_id")
            if isinstance(sid, str) and sid:
                st = _state_for_session(sid, by_session, excluded_ids, withheld_sessions)
                counts[EPISODIC_SESSION][st] += 1
                rows.append({"subject_kind": EPISODIC_SESSION, "subject_id": sid,
                             "referenced_by": rid, "state": st})
        elif kind == records.GIST_KIND:
            scanned["gists"] += 1
            sid = r.get("session_id")
            if isinstance(sid, str) and sid and records.is_cross_session_sentinel(sid):
                # a `tag:` cluster gist has no single real session; its real provenance is its source_ids,
                # classified below, so a session-line lookup on the sentinel would be meaningless.
                scanned["gist_cross_session_clusters"] += 1
            elif isinstance(sid, str) and sid:
                st = _state_for_session(sid, by_session, excluded_ids, withheld_sessions)
                counts[GIST_SESSION][st] += 1
                rows.append({"subject_kind": GIST_SESSION, "subject_id": sid,
                             "referenced_by": rid, "state": st})
            for s in (r.get(records.SOURCE_IDS_KEY) or []):
                if not isinstance(s, str) or not s:
                    continue
                st = _state_for_id(s, present_ids, excluded_ids)
                counts[GIST_SOURCE][st] += 1
                rows.append({"subject_kind": GIST_SOURCE, "subject_id": s,
                             "referenced_by": rid, "state": st})

    fp_end = _fingerprint(path)

    reasons: list = []
    if read.malformed:
        reasons.append(f"{read.malformed} malformed ledger line(s) the reader had to skip")
    if read.torn_trailing:
        reasons.append("a torn trailing ledger line (a crash mid-append)")
    if fp_start != fp_end:
        reasons.append("the ledger changed while this census was reading it, so these counts are not one "
                       "consistent snapshot")

    rows.sort(key=lambda x: (x["subject_kind"], x.get("referenced_by") or "", x["subject_id"], x["state"]))
    return CensusResult(
        scanned=scanned, counts=counts, rows=rows,
        read_health={"malformed": read.malformed, "torn_trailing": read.torn_trailing},
        indeterminate=bool(reasons), indeterminate_reasons=reasons,
        fingerprint_start=list(fp_start), fingerprint_end=list(fp_end),
    )


def render(result: CensusResult) -> str:
    """The AGGREGATE report — the only thing quoted in a pull request body. No record text, no per-id rows."""
    lines = ["Legacy summary/gist coverage census (StarshipSuperjam/engine-template#1053) — raw ledger, read-only."]
    if result.indeterminate:
        lines.append("RESULT: INDETERMINATE — " + "; ".join(result.indeterminate_reasons))
    else:
        lines.append("RESULT: determinate (read-health clean; ledger stable across the pass).")
    lines.append("read-health: malformed={malformed} torn_trailing={torn_trailing}".format(**result.read_health))
    lines.append("scanned: {episodics} episodic summaries, {gists} gists "
                 "({gist_cross_session_clusters} cross-session cluster gists)".format(**result.scanned))
    lines.append("what the counts mean: present = the referenced record is still in memory and recallable; "
                 "excluded-but-present = its text is still on the ledger but withheld from recall; "
                 "absent = the reference points at a record no longer on the ledger.")
    for kind, label in ((EPISODIC_SESSION, "episodic referenced-session"),
                        (GIST_SESSION, "gist referenced-session   "),
                        (GIST_SOURCE, "gist source-record        ")):
        c = result.counts[kind]
        lines.append(f"{label}: present={c[PRESENT]}  excluded-but-present={c[EXCLUDED_BUT_PRESENT]}  absent={c[ABSENT]}")
    lines.append("denominator: the original turn count is UNKNOWABLE, so coverage as a fraction of all turns is "
                 "explicitly UNKNOWN. These counts describe only what the summaries/gists reference and whether it survives.")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Read-only legacy summary/gist coverage census over the raw ledger.")
    ap.add_argument("--path", default=None, help="ledger path (default: the live ledger)")
    ap.add_argument("--json", action="store_true",
                    help="print the full result as JSON — including the identifier-level artifact rows "
                         "(ids/kinds/states only, never record text) — instead of the aggregate report; "
                         "redirect stdout to a file to capture the artifact. This tool is read-only and never "
                         "writes a file itself.")
    args = ap.parse_args(argv)
    result = census(path=args.path)
    print(json.dumps(result.as_dict(), indent=2, sort_keys=True) if args.json else render(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
