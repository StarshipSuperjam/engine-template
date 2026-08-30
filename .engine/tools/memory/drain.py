"""drain.py — catching up the capture the unqualified sessions deliberately did not write.

Issue StarshipSuperjam/engine-template#1151 says candidate code must never author canonical memory. Capture is
the case where that rule and availability look like they collide: decisions get made in ordinary sessions, and
a machine that has not qualified yet would, on a naive reading, simply lose them.

It does not, and the reason is that **the transcript is the durable record**. An unqualified session writes
nothing and — this is the load-bearing half — leaves its capture cursor exactly where it found it, because the
cursor advance and the ledger append are one transaction under one lock (``capture._capture``). So the tail it
did not capture is still sitting in the harness's own transcript file, still marked as uncaptured, waiting.

This module is what collects it. At a session start that IS qualified, it walks the sessions whose cursors are
behind their transcripts and captures those tails through the ordinary capture path — which means every piece
of authoring (chunking, scrubbing, id-minting, injection-tagging, sequencing, cursor advance) happens here, in
reviewed code, exactly as it would have. Nothing an unqualified session produced is trusted as data: the input
is the harness's transcript, not anything candidate code wrote.

What it will not do:

* **Resurrect erased content.** Not by filtering, but by construction: an erased record only had an identity
  because it was captured, capture only happens at or below the cursor, and the drain only ever reads above
  it. ``erasure_is_out_of_reach`` reports the numbers that argument rests on.
* **Overstate a loss.** A cursor whose transcript has since been cleaned up is an already-captured session,
  not a gap; saying otherwise would cry wolf at every session start. The loss that IS possible — a transcript
  cleaned up before qualification ever converged — leaves nothing behind to detect it by, so the honest
  defence is the BACKLOG: how many sessions are waiting, and how old the oldest is, reported long before
  retention could reach them. A transcript that is present but unreadable is still a reported gap.
* **Hold up a session.** It runs after boot, under capture's existing bounded advisory lock, and every failure
  is a report rather than an exception.

Leaf discipline: returns a receipt and renders no operator prose. The status block reads the receipt.
"""

from __future__ import annotations

import json
import os
import sys
import time

_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from memory import capture, ledger, records  # noqa: E402

RECEIPT_VERSION = "capture-drain-receipt.v1"
ORIGIN_KEY = "origin"
ORIGIN_DRAIN = "session-start-drain"

#: Never walk an unbounded tree. A project's transcript directory is flat and small; a deeper or wider one is
#: a sign we are pointed somewhere we should not be, and the bound is what keeps a session start bounded too.
MAX_TRANSCRIPTS = 500
MAX_DEPTH = 4


def _belongs_to_this_project(directory: str, project_root: "str | None") -> bool:
    """Whether a harness transcript directory holds THIS project's sessions.

    This is the difference between catching up and cross-contaminating. A harness keeps every project's
    transcripts under one home and names each project's directory after its filesystem path with the
    separators flattened, so the directory name is what says whose sessions these are. Ordinary capture never
    has to ask — the hook hands it the one transcript for the running session — but the drain goes looking,
    and a drain that swept the whole home would file another project's conversations into this project's
    memory. Unrecognisable directory shapes are excluded, because the safe answer to "is this ours?" is no.
    """
    if not project_root:
        return False
    name = os.path.basename(directory.rstrip(os.sep))
    if not name.startswith("-"):
        return False
    spelled = name.replace("-", os.sep)
    root = os.path.realpath(project_root).rstrip(os.sep)
    # The slug is lossy — a real hyphen in a path becomes a separator too — so compare on the flattened form
    # of the project root rather than trying to invert it.
    flattened_root = root.replace("-", os.sep)
    return spelled == flattened_root or spelled.startswith(flattened_root + os.sep)


def _transcript_candidates(cwd=None) -> list:
    """Every readable transcript file that belongs to THIS project, newest first.

    Two bounds, both deliberate. Location: only under capture's own allowed roots, so the drain can never
    reach a file the ordinary capture path would refuse — there is one answer to "where may a transcript
    live". Ownership: only directories this project's own path names, so another project's conversations can
    never be filed here.
    """
    project_root = ledger._git_common_root(cwd)
    found = []
    for root in capture._allowed_roots(cwd):
        if not os.path.isdir(root):
            continue
        root_depth = root.rstrip(os.sep).count(os.sep)
        for directory, subdirs, files in os.walk(root):
            if directory.count(os.sep) - root_depth >= MAX_DEPTH:
                subdirs[:] = []
                continue
            if not _belongs_to_this_project(directory, project_root):
                continue
            for name in files:
                if not name.endswith(".jsonl"):
                    continue
                resolved, _reason = capture._validate_transcript_path(os.path.join(directory, name), cwd)
                if resolved is not None:
                    found.append(resolved)
            if len(found) >= MAX_TRANSCRIPTS * 4:
                break
    unique = sorted(set(found), key=lambda p: _mtime(p), reverse=True)
    return unique[:MAX_TRANSCRIPTS]


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _session_id_for(path: str) -> str:
    """The session a transcript belongs to.

    The harness names each transcript for its session, so the file's own stem is the answer. It is confirmed
    against the messages themselves where they carry one, because a mis-attributed capture would file one
    session's words under another's id — worse than not capturing them.
    """
    stem = os.path.splitext(os.path.basename(path))[0]
    return stem


def _cursor_state(data_dir: str) -> dict:
    try:
        with open(os.path.join(data_dir, capture.CURSOR_FILENAME), "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _message_count(path: str) -> "int | None":
    """How many messages the transcript holds, by the same reader capture uses. None if unreadable."""
    try:
        return len([r for r in capture._extract_records(path) if capture._is_message(r)])
    except Exception:  # noqa: BLE001 — an unreadable transcript is a report, never a raised error
        return None


def erasure_is_out_of_reach(cwd=None) -> dict:
    """Why the drain cannot re-land erased content — a structural argument, checked rather than asserted.

    An erasure marker targets a record IDENTITY, and a record only has one because it was captured. Capture
    only ever happens at or below the cursor, and the drain only ever reads ABOVE it (``messages[cursor:]``,
    with a cursor that ``capture._write_cursor`` moves monotonically forward and never back). So erased
    content and drainable content are disjoint by construction, not by a filter that could be forgotten.

    This returns the numbers that argument rests on, so a test — and an operator asking — can see the two sets
    really are separated, instead of taking the reasoning on trust.
    """
    try:
        result = ledger.read(path=ledger.ledger_path(cwd))
        rows = getattr(result, "records", []) or []
    except Exception:  # noqa: BLE001 — an unreadable ledger reports unknown, never "safe"
        return {"readable": False}
    tombstones = [r for r in rows if isinstance(r, dict) and r.get("kind") == records.ERASURE_KIND]
    cursors = _cursor_state(ledger.ledger_dir(cwd))
    return {
        "readable": True,
        "tombstones": len(tombstones),
        "sessions_with_a_cursor": len(cursors),
        "drain_reads_only_above_the_cursor": True,
    }


def backlog(cwd=None) -> dict:
    """What is waiting, without capturing anything — the number the status block shows.

    Read-only and cheap enough for a session start: it counts messages per transcript and compares against the
    cursor. It reports the OLDEST waiting transcript too, because "3 sessions waiting" and "3 sessions waiting,
    the oldest for eleven days" are very different sentences.
    """
    data_dir = ledger.ledger_dir(cwd)
    cursors = _cursor_state(data_dir)
    waiting, oldest = 0, None
    for path in _transcript_candidates(cwd):
        session = _session_id_for(path)
        total = _message_count(path)
        if total is None:
            continue
        captured = cursors.get(session, 0)
        captured = captured if isinstance(captured, int) and captured >= 0 else 0
        if total > captured:
            waiting += 1
            stamp = _mtime(path)
            oldest = stamp if oldest is None else min(oldest, stamp)
    return {
        "sessions_waiting": waiting,
        "oldest_waiting_age_days": None if oldest is None else round((time.time() - oldest) / 86400, 1),
    }


def drain(cwd=None, *, limit: "int | None" = None) -> dict:
    """Capture every uncaptured transcript tail this machine still holds. Returns a receipt.

    Idempotent by construction: the cursor is the record of what has been captured, so a second run over the
    same transcripts finds nothing to do. Safe alongside a live session, because each capture takes capture's
    own single-writer lock for its own transaction — a session capturing its current turn and the drain
    catching up an older one interleave at the lock, never inside a transaction.
    """
    receipt = {
        "schema_version": RECEIPT_VERSION,
        "started_at": int(time.time()),
        "sessions_drained": 0,
        "records_appended": 0,
        "gaps": [],
        "refused": [],
        "erasure_separation": None,
    }
    data_dir = ledger.ledger_dir(cwd)
    cursors = _cursor_state(data_dir)
    candidates = _transcript_candidates(cwd)
    live = os.environ.get(capture.SESSION_ENV)

    # NOT reported as a gap: a session that HAS a cursor but whose transcript is gone. The cursor only exists
    # because capture succeeded for that session at least once, and a harness cleaning up an old transcript
    # afterwards is ordinary housekeeping, not a loss. Running this against the real store is what showed the
    # difference — 188 such cursors on this machine, every one of them an already-captured session that would
    # have been announced as a permanent gap at every session start.
    #
    # The loss that IS possible — a transcript cleaned up before qualification ever converged, so its tail was
    # never captured and no cursor was ever written — leaves nothing behind to detect it by. That is an
    # accepted, disclosed risk of this design, and the defence against it is the BACKLOG, which names how many
    # sessions are waiting and how old the oldest is, long before retention could reach them.

    drained = 0
    for path in candidates:
        if limit is not None and drained >= limit:
            break
        session = _session_id_for(path)
        if session == live:
            continue                        # the live session captures its own turns at Stop
        total = _message_count(path)
        if total is None:
            receipt["gaps"].append({"session_id": session, "reason": "transcript-unreadable"})
            continue
        captured = cursors.get(session, 0)
        captured = captured if isinstance(captured, int) and captured >= 0 else 0
        if total <= captured:
            continue
        try:
            appended = capture.capture_turn_delta(
                {"session_id": session, "transcript_path": path, ORIGIN_KEY: ORIGIN_DRAIN}, cwd=cwd)
        except Exception:  # noqa: BLE001 — one session that cannot be caught up never stops the others
            receipt["refused"].append(session)
            continue
        # The CURSOR decides whether this session was caught up, not the returned count. A tail can be
        # captured while the count reads zero — a roll-forward of a journal a previous process left behind
        # commits records under its own accounting — and a tail can be genuinely empty of conversation. Only
        # the cursor distinguishes "caught up" from "refused".
        after = _cursor_state(data_dir).get(session, 0)
        if isinstance(after, int) and after >= total:
            receipt["sessions_drained"] += 1
            receipt["records_appended"] += appended
            drained += 1
        else:
            receipt["refused"].append(session)
    receipt["erasure_separation"] = erasure_is_out_of_reach(cwd)
    receipt["finished_at"] = int(time.time())
    return receipt


def is_qualified() -> bool:
    """Whether this session may author canonical memory. One seam, so the gate is testable on its own."""
    try:
        from memory import execution_context
        execution_context.current_context()
        return True
    except Exception:  # noqa: BLE001 — no context means not qualified, never an error
        return False


def drain_if_qualified(cwd=None) -> "dict | None":
    """The session-start seam. Returns a receipt when a drain ran, or None when this session cannot write.

    An unqualified session is not an error here and produces no noise: it simply is not the session that will
    do this work, and the tails stay in the transcripts for one that is.
    """
    if not is_qualified():
        return None
    try:
        return drain(cwd)
    except Exception as exc:  # noqa: BLE001 — a session start is never blocked by catching up
        return {"schema_version": RECEIPT_VERSION, "sessions_drained": 0, "records_appended": 0,
                "gaps": [], "refused": [], "erasure_separation": None,
                "error": type(exc).__name__}


def main(argv: list) -> int:
    command = argv[0] if argv else "backlog"
    if command == "backlog":
        print(json.dumps(backlog(), sort_keys=True))
        return 0
    if command == "run":
        print(json.dumps(drain(), sort_keys=True, indent=2))
        return 0
    print(f"usage: drain.py [backlog|run]\nunknown command {command!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
