"""pins.py — durable operator intent, saved the moment they ask for it.

WHAT A PIN IS FOR. Most of what a project decides has a better home than memory: a merged pull request, a
decision record, the code itself. A pin is for the residue — a standing preference, a way of working, a "never
do that again" — that has no canonical artifact to live in and would otherwise survive only as a sentence in a
conversation nobody thinks to search for. It lives in the same substrate as a distinct record type rather than a
sixth store, and that is exactly what this is: an ordinary ledger record that ordinary recall surfaces.

WHAT A PIN IS NOT FOR (StarshipSuperjam/engine-template#650, StarshipSuperjam/engine-template#766). An assistant's own material is different in kind and does not belong
here, however durable it feels: an operating note (a navigation map, a tool quirk, a build lesson) goes in the
assistant's own harness memory notebook, and a project conclusion gets stated plainly in the session, where
capture makes it recallable. Pinning either forces it into every future boot briefing — the operator's
context, spent forever on something they never asked to be remembered.

WHY IT IS DELIBERATE AND SMALL. Pins are the one thing here nothing ages out and nothing summarises away, and
the cold-start briefing carries them into every session. That is the whole point, and it is also why minting
one is an explicit act rather than something inferred: a store that pins generously stops being a small set of
standing intentions and becomes another stream to wade through, at a cost paid on every session start forever.
So there is a verb, the operator says the word, and `remove` is a first-class verb rather than an afterthought.

WHAT THE PROVENANCE FIELD DOES AND DOES NOT CLAIM. A pin records the route it arrived by
(`records.PIN_VIA_KEY`) and nothing stronger. When a model calls the write tool it is transcribing what the
operator asked for, and its context may also hold a page it recalled, a file it read, or tool output — text
shaped like an instruction that nobody typed. Nothing downstream can tell those apart. So every reader presents
a pin as something saved when the operator asked, never as a verified quotation, and this module's job is to
make sure the field needed to say that honestly is always present.

SCRUBBED ON THE WAY IN. A pin does not travel through capture, so capture's secret scrub never sees it. A
credential pasted into a session and then pinned would be stored unscrubbed and read into every future
briefing — worse than the exposure the scrub exists to prevent. `add` runs the same scrubber, and says so.

REMOVING A PIN IS WITHHOLDING IT. There is no second retirement path: `remove` calls `forget.withhold`, so a
removed pin stops being surfaced, stays in the ledger, and comes back with `forget.restore` like anything else
the operator withheld. One mechanism, one mental model, and the append-only law untouched.
"""

import argparse
import base64
import binascii
import os
import sys
import time
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import forget, ledger, records, refusals, scrub  # noqa: E402

# What one pin may carry. A pin is a standing intention, not a document: the briefing reads every live pin at
# every session start, so an unbounded one would quietly spend a growing share of the pack forever. The cap is
# generous enough for a real preference stated in full sentences and refuses rather than truncating, because a
# silently halved instruction is worse than one that was not saved.
MAX_PIN_CHARS = 1000

# How many live pins is "a lot" — the point at which the pin handler gently suggests pruning at creation time,
# set to arrive AS the session-start briefing begins folding older pins behind a disclosed count (boot's index
# cap is 8, so the 9th pin is the first one folded). Aligning the two means the operator meets the gentle
# create-time nudge no later than the louder every-session fold, not after it. This is memory's OWN hygiene
# sense tracking that fold point, NOT a mirror-import of boot's render budget (boot owns and enforces the actual
# index cap, StarshipSuperjam/engine-template#950) — keeping it local avoids a memory->boot import. A nudge only: creation NEVER
# refuses on count — a pin the operator asked for is always saved (over-long TEXT is the one refusal, and even
# that saves nothing halfway).
PIN_PRUNE_HINT_AT = 9


class PinRefused(refusals.EngineRefusal, ValueError):
    """A pin could not be saved, with the plain-language reason. Raised rather than returned: the operator
    asked for something to be remembered, and a verb that quietly declined would leave them believing it was.
    The sentence names no path and splices no underlying exception; `raw_detail` keeps the latter for a log."""

    def __init__(self, message: str, *, raw_detail: "str | None" = None):
        super().__init__(message)
        self.raw_detail = raw_detail


class PinUnconfirmed(PinRefused):
    """The save step failed after the record's bytes may already have landed, and the ledger could not be read
    back to tell: neither "saved" nor "nothing was saved" is honest, so this says so. A PinRefused, so every
    existing handler still catches it; the command line prints it as "Not confirmed", never "Not saved"."""


#: The sentence for `PinUnconfirmed`: it names what is not known and what to do, and never says nothing was saved.
UNCONFIRMED_SENTENCE = ("the pin's save step did not complete and memory could not be read back to confirm "
                        "whether it landed, so this is not confirmed either way. Check with a search before "
                        "saving it again. " + refusals.ESCALATION)


#: The note that rides with a pin whose bytes landed but whose flush step then failed (R9 DH-1): the save is
#: real and readable, and the operator is told so, but never as a clean success — the failed step is named.
UNFLUSHED_NOTE = ("Saved, but not cleanly: the pin is on disk and readable, but the save step after its bytes "
                  "landed (the ledger flush) reported an I/O error, so it may not survive a crash until the next "
                  "write completes cleanly. Nothing was retried. " + refusals.ESCALATION)


def _fault_collector():
    """A line sink for the command line: records whether the write landed despite a fault, so the success
    line can carry `UNFLUSHED_NOTE` instead of reading as a clean save (R9 DH-1)."""
    faults = []

    def collect(kind: str, payload: dict) -> None:
        if kind == "committed" and payload.get("fault") is not None:
            faults.append(payload["fault"])
    return collect, faults


def _landed_despite(record, target: str):
    """After an exception inside the append step: True when the record is readable in the ledger (the bytes
    landed before the fault — an I/O error in the flush is the observed case), False when the ledger was
    searched and holds no such record, None when the ledger could not be read. Called under the single-writer
    lock, before the catch-all decides its sentence (round 7)."""
    if record is None:
        return False
    try:
        return ledger.find_raw_record(record[records.RECORD_ID_KEY], path=target,
                                      id_key=records.RECORD_ID_KEY) is not None
    except ledger.LedgerUnreadable:
        return None


def _cli_refusal_line(exc: "PinRefused") -> str:
    """The command line's one-line report of a refusal: "Not saved" only when nothing was saved, "Not
    confirmed" when that is not known."""
    return f"{'Not confirmed' if isinstance(exc, PinUnconfirmed) else 'Not saved'}: {exc}"


#: The one explicit identity an omitted session_id collapses to, so a dispatched pin with no session and a
#: second call that also carries none are recognised as the same lane by the under-lock duplicate check.
_NO_SESSION_IDENTITY = "\x00no-session"


def _normalized_pin_text(text: str) -> str:
    """The decisive key the duplicate check compares on: NFC-folded, outer whitespace stripped, inner runs
    collapsed to one space. Two requests that differ only in those land as the same pin, never two."""
    return " ".join(unicodedata.normalize("NFC", text).split())


def _session_identity(session_id: "str | None") -> str:
    return session_id if (isinstance(session_id, str) and session_id) else _NO_SESSION_IDENTITY


def _find_duplicate_pin(cleaned: str, session_identity: str, *, path: str):
    """The live pin already carrying this exact normalized text on this exact session lane, or None. Called
    only under the write lock, so what it reads is the committed state the append would extend."""
    key = _normalized_pin_text(cleaned)
    for record in list_pins(path=path):
        if (_normalized_pin_text(record.get("text") or "") == key
                and _session_identity(record.get(records.PIN_SOURCE_SESSION_KEY)) == session_identity):
            return record
    return None


def _emit_confirmation(emit, event: str, payload: dict) -> None:
    """Emit a post-commit forensic line as BEST EFFORT: the write is already durable, so a failure here
    (a broken pipe to a parent that closed, or any callback error) is swallowed rather than allowed to
    masquerade as a lost write. The parent's read-back of the pre-minted id is the backstop that still
    resolves the outcome to `committed` when this line never arrives."""
    if emit is None:
        return
    try:
        emit(event, payload)
    except Exception:
        pass


def add(text: str, *, session_id: "str | None" = None, via: str = records.PIN_VIA_ASSISTANT,
        path: "str | None" = None, now: "int | None" = None, accepted_id: "str | None" = None,
        emit=None, dedup: bool = False) -> dict:
    """Save one pin and return the record as written. Raises PinRefused on empty or over-long text.

    A pin is standing OPERATOR intent — call this when the operator asked for something to be remembered,
    never for the assistant's own notes or conclusions (WHAT A PIN IS NOT FOR, module docstring: those have
    their own homes, and every pin is read into the operator's boot briefing forever).

    The text is scrubbed before it is stored (module docstring), so what lands in the ledger is what every
    later reader sees — there is no unscrubbed copy anywhere. `session_id` records where it was asked for, so
    the conversation around the request stays reachable with the window reader; a pin minted outside a session
    simply carries none. `via` records the route, never an authority claim.

    Appends under the single-writer lock and bumps the ledger's INDEX EPOCH (membership changed: a record the
    index has not seen), exactly as the withhold verbs do and for the same reason: without it the fast index
    stays stamped current and the pin the operator just saved is missing from the next search, answered as
    though the index were authoritative. It does not bump the ledger GENERATION — that counter means content
    was rewritten or removed, which an append never does — and the two have different recovery meanings."""
    if not isinstance(text, str) or not text.strip():
        raise PinRefused("there was nothing to save — a pin needs some words.")
    cleaned = scrub.scrub_text(text.strip())
    if len(cleaned) > MAX_PIN_CHARS:
        raise PinRefused(
            f"that is longer than a pin holds ({len(cleaned)} characters against a limit of {MAX_PIN_CHARS}). "
            "Nothing was saved. Shorten it to the standing instruction itself, or let it live in the "
            "conversation, which stays searchable either way."
        )
    if via not in (records.PIN_VIA_ASSISTANT, records.PIN_VIA_CLI):
        via = records.PIN_VIA_ASSISTANT
    from memory import capture  # lazy: keep capture off the module-load path (cycle discipline)
    target = path if path is not None else ledger.ledger_path()
    data_dir = os.path.dirname(target) or "."
    os.makedirs(data_dir, exist_ok=True)
    record_id = accepted_id if (isinstance(accepted_id, str) and accepted_id) else records.new_record_id()
    session_identity = _session_identity(session_id)
    if emit is not None:
        # Forensic: the pre-minted id crosses to the parent BEFORE the lock, so a child that dies mid-write
        # leaves the parent a record id to reason about rather than a silent gap.
        emit("begin", {records.RECORD_ID_KEY: record_id})
    lock_fd = capture._acquire_lock(os.path.join(data_dir, capture.LOCK_FILENAME))
    if lock_fd is None:
        # `None` is not proof of contention: the same value comes back when the store cannot be opened at all.
        # "Try again in a moment" over a permissions problem or a full disk is advice that can never work.
        writable = os.access(data_dir, os.W_OK)
        raise PinRefused(
            "another memory write is in progress, so nothing was saved. Try again in a moment."
            if writable else
            "memory could not be written to (the memory folder is not writable), so nothing was saved. This will "
            "not clear on its own — check the folder's permissions and that its disk is mounted and has room."
        )
    duplicate_record = None
    committed_record = None
    committed_bytes = None
    attempted = None
    landed_despite_fault = None
    try:
        if dedup:
            duplicate = _find_duplicate_pin(cleaned, session_identity, path=target)
            if duplicate is not None:
                duplicate_record = duplicate
        if duplicate_record is None:
            record = {
                "v": capture.RECORD_VERSION,
                "kind": records.PIN_KIND,
                records.RECORD_ID_KEY: record_id,
                "text": cleaned,
                "ts": int(time.time()) if now is None else now,
                "tags": [records.PIN_TAG],
                records.PIN_VIA_KEY: via,
            }
            if isinstance(session_id, str) and session_id:
                record[records.PIN_SOURCE_SESSION_KEY] = session_id
            ledger.bump_index_epoch(for_path=target)
            attempted = record
            appended = ledger.append(record, path=path)
            committed_record = record
            committed_bytes = appended.length
    except PinRefused:
        raise
    except Exception as exc:
        # The append may have LANDED before this was raised: `ledger.append` flushes after its write loop, so
        # an I/O error in the flush leaves a readable record on disk. Reconcile against the ledger — still
        # under the lock — before the sentence is chosen (round 7): readable -> the pin is saved and is
        # returned as such (its byte length is unknown); unreadable -> unconfirmed, never "nothing was
        # saved"; searched and absent -> nothing was saved.
        landed = _landed_despite(attempted, target)
        if landed is True:
            # Landed, but not cleanly: the fault is carried on the committed line (R9 DH-1) so every route
            # discloses it, never swallowed into a plain "Pinned".
            committed_record = attempted
            landed_despite_fault = exc
        elif landed is None:
            raise PinUnconfirmed(UNCONFIRMED_SENTENCE, raw_detail=str(exc)) from exc
        else:
            raise PinRefused("the pin could not be saved — an internal memory-write step did not complete, so "
                             "nothing was saved. " + refusals.ESCALATION, raw_detail=str(exc)) from exc
    finally:
        capture._release_lock(lock_fd)
    # The append (if any) has LANDED and the lock is released. The forensic confirmation line is best-effort
    # telemetry for the dispatch parent — its failure (e.g. a BrokenPipeError writing to a parent that already
    # closed the pipe) must NEVER be reported as a lost write, so it is emitted OUTSIDE the catch-all above,
    # whose sentence says "nothing was saved". The record is durable and is returned regardless.
    if duplicate_record is not None:
        _emit_confirmation(emit, "already_pinned", {"record": duplicate_record})
        return duplicate_record
    receipt = {"record": committed_record, "bytes": committed_bytes}
    if landed_despite_fault is not None:
        receipt["fault"] = str(landed_despite_fault)
    _emit_confirmation(emit, "committed", receipt)
    return committed_record


def list_pins(*, path: "str | None" = None, limit: "int | None" = None) -> list:
    """Every live pin, newest first. Reads through `live_records`, so a pin the operator removed is absent
    exactly as it is absent from recall — one definition of "live", never a second one that could disagree.

    The operator is promised they can ask what is saved and have it read back, so the default returns all of
    them; `limit` is for the callers that must stay bounded, like the briefing."""
    src = ledger.ledger_path() if path is None else path
    out = [r for r in forget.live_records(path=src)
           if isinstance(r, dict) and r.get("kind") == records.PIN_KIND]
    out.sort(key=lambda r: (isinstance(r.get("ts"), int) and not isinstance(r.get("ts"), bool),
                            r.get("ts") if isinstance(r.get("ts"), int) else 0,
                            r.get(records.RECORD_ID_KEY) or ""), reverse=True)
    return out[:limit] if isinstance(limit, int) and limit >= 0 else out


def remove(record_id: str, *, path: "str | None" = None, emit=None) -> dict:
    """Stop surfacing one pin. Withholds it (module docstring) — nothing is deleted and `forget.restore` on the
    same id brings it back. `emit` is the same line sink `forget.withhold` takes."""
    return forget.withhold(record_id=record_id, path=path, emit=emit)


def _print_list(path: "str | None" = None) -> int:
    live = list_pins(path=path)
    if not live:
        print("Nothing is pinned. Say \"remember this\" with what you want kept and it will be saved here.")
        return 0
    print(f"{len(live)} pinned:" if len(live) != 1 else "1 pinned:")
    for record in live:
        when = record.get("ts")
        stamp = time.strftime("%Y-%m-%d", time.localtime(when)) if isinstance(when, int) else "unknown date"
        print(f"  [{record.get(records.RECORD_ID_KEY)}] {stamp}  {record.get('text')}")
    return 0


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(
        prog="pins.py",
        description="Save and read back durable operator intent — what the OPERATOR asked to be "
                    "remembered. An assistant's own note does not belong here: an operating note goes in "
                    "its harness memory notebook, a project conclusion gets stated plainly in the session, "
                    "where recall can find it.")
    sub = parser.add_subparsers(dest="cmd")
    add_cmd = sub.add_parser("add", help="save a pin (something the operator asked to be remembered)")
    add_cmd.add_argument("text", help="the standing instruction to keep, in the operator's own terms")
    add_cmd.add_argument("--session", default=None, help="the session it was asked for in")
    encoded_cmd = sub.add_parser(
        "add-base64", help="save one UTF-8 pin carried as canonical URL-safe Base64 (shell-safe transport)")
    encoded_cmd.add_argument("text_base64", help="canonical URL-safe Base64 of the exact standing instruction")
    encoded_cmd.add_argument("--session", default=None, help="the session it was asked for in")
    sub.add_parser("list", help="read back every live pin")
    rm = sub.add_parser("remove", help="stop surfacing one pin (reversible)")
    rm.add_argument("record_id", help="the pin's id, as shown by `list`")
    args = parser.parse_args(argv)
    if args.cmd == "add":
        collect, faults = _fault_collector()
        try:
            record = add(args.text, session_id=args.session, via=records.PIN_VIA_CLI, emit=collect)
        except PinRefused as exc:
            print(_cli_refusal_line(exc))
            return 1
        print(f"Pinned [{record[records.RECORD_ID_KEY]}]." + (f" {UNFLUSHED_NOTE}" if faults else ""))
        return 0
    if args.cmd == "add-base64":
        try:
            encoded = args.text_base64.encode("ascii")
            raw = base64.b64decode(encoded, altchars=b"-_", validate=True)
            if base64.urlsafe_b64encode(raw) != encoded:
                raise ValueError("non-canonical encoding")
            text = raw.decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError, binascii.Error, ValueError):
            print("Not saved: the pin transport must be canonical URL-safe Base64 of UTF-8 text.")
            return 1
        collect, faults = _fault_collector()
        try:
            record = add(text, session_id=args.session, via=records.PIN_VIA_CLI, emit=collect)
        except PinRefused as exc:
            print(_cli_refusal_line(exc))
            return 1
        print(f"Pinned [{record[records.RECORD_ID_KEY]}]." + (f" {UNFLUSHED_NOTE}" if faults else ""))
        return 0
    if args.cmd == "remove":
        collect, faults = _fault_collector()
        try:
            remove(args.record_id, emit=collect)
        except forget.ControlNotRecorded as exc:
            # The shared verb speaks of "a single note, or a whole session" because it serves both; this
            # command takes a pin id and nothing else, so offering a session here names a choice the operator
            # was never given.
            reason = str(exc).replace("name exactly one thing to act on — a single note, or a whole session.",
                                      "no pin identifier was given.")
            reason = reason.replace("there is no note in memory with that identifier",
                                    "there is no pin with that identifier")
            # "Not removed" only when nothing was changed; an unconfirmed outcome says so (R9 DH-2).
            print(f"{'Not confirmed' if isinstance(exc, forget.ControlUnconfirmed) else 'Not removed'}: {reason}")
            return 1
        print("Removed from recall. It is still saved — ask to restore it any time."
              + (f" {forget.UNFLUSHED_NOTE}" if faults else ""))
        return 0
    if args.cmd == "list":
        return _print_list()
    parser.print_help()
    return 2


try:
    from . import mutation_authority as _mutation_authority
except ImportError:  # direct CLI
    from memory import mutation_authority as _mutation_authority
_mutation_authority.install_module_guards(globals())


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
