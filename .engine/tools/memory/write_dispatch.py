"""write_dispatch.py — the one seam every memory write crosses: the server hands a write to a freshly
launched accepted child, and the decisive work happens THERE, not on the server.

WHY A DISPATCH AT ALL. Write authority follows the *merge*. The server the operator is talking to was
launched from whatever tree was accepted when the session began; the code that may write must be the code
on the project's current commit. So instead of writing in-process, the server describes the write as a
`request` and asks for a child launched from the exact accepted tree on disk right now. The child mints the
record, takes the ledger lock, and commits — under the current commit's rules — and reports back. Nothing
canonical is written by the server itself (that is the invariant node 0 establishes and a test pins).

THE CHILD IS AUTHORITATIVE, THE PARENT ONLY RELAYS. `run_child` is the child-side body: it pre-mints one
record id, prints a `begin` line carrying that id BEFORE it takes the write lock, does the write with the
decisive duplicate check inside the lock, prints a `committed` line after the append lands, and returns the
full operator-facing response. `dispatch` is the parent-side relay: it runs the child exactly once — never a
retry, because a retried write is a doubled write — and classifies the single outcome. A refusal the child
wrote for the operator crosses back as its plain sentence (an `EngineRefusal`, so the server boundary
forwards it verbatim); a write that committed but whose confirmation did not make it back is NEVER reported
as "nothing saved", because it may well have saved.

HOW A LOST CONFIRMATION IS RESOLVED, NOT GUESSED. The parent runs the child under a timeout, and on a stuck
or client-cancelled child it terminates and REAPS the child before it reads anything — so a lingering child
can never keep committing behind an already-reported outcome. Only once the child is dead does the parent
fold its line stream into one of five outcomes (`_classify_outcome`): committed, refused, faulted,
unconfirmed (the child could still be writing — never reported as nothing-saved), or not-attempted (the
child never launched). A `begin` line with no confirmation is resolved by a read-only read-back of the
pre-minted id against the ledger on disk: found means the write landed; absent-with-a-dead-child means a
genuine fault; absent-while-alive stays unconfirmed. Faults and not-attempted launches are recorded to the
stranding log for forensics.

THE SEAM IS INJECTABLE. `dispatch(request, run=...)` takes the runner, defaulting to the real cross-process
launcher. Tests pass `run=run_child` to exercise the whole child body in-process without a subprocess. The
real launcher cannot be exercised from a pre-merge worktree — the running accepted tree does not yet contain
this file — so node 0 measures the seam in-process and the cross-process launch begins working once this
change is on an accepted commit."""

import json
import os
import subprocess
import sys

try:  # package import (the accepted server process, and the tests)
    from . import forget, ledger, pins, records, refusals, stranding_log
    from . import execution_context
except ImportError:  # direct CLI / accepted runpy entry (`sys.argv[0]` is this file)
    from memory import forget, ledger, pins, records, refusals, stranding_log  # noqa: F401
    from memory import execution_context  # noqa: F401

try:
    from . import mutation_authority as _mutation_authority
except ImportError:
    from memory import mutation_authority as _mutation_authority


#: The registered operation the accepted child launches under. Its writer is `memory.write_dispatch.main`;
#: the contract grants that writer the three canonical write operations through TRANSITIVE_BOUNDARIES, so a
#: child running under this operation may reach `attended-pin-add` / `attended-withhold` /
#: `attended-restore-withheld` and nothing else.
OPERATION = "attended-write-dispatch"

#: The request verbs a child understands. One canonical write each; the verb alone disambiguates the shared
#: `session_id` field (pin's source session vs. a withhold/restore target session).
_VERBS = ("pin", "withhold", "restore")


class DispatchRefused(refusals.EngineRefusal):
    """The child refused the write and wrote the operator a plain reason. It derives from `EngineRefusal`, so
    the memory server's tool boundary forwards its sentence verbatim — the same path a refusal took when the
    write still happened in-process."""


class DispatchFaulted(RuntimeError):
    """The child neither committed nor refused in a way we could read — a genuine fault, not a refusal. It is
    deliberately NOT an `EngineRefusal`: it stays the masked crash it is, so a broken dispatch is disclosed as
    an unexpected fault rather than dressed up as a polished sentence."""


# --------------------------------------------------------------------------------------------------------- #
# Child side: the authoritative write.
# --------------------------------------------------------------------------------------------------------- #

def _emit_line(sink, event: str, payload: dict) -> None:
    if sink is not None:
        sink(json.dumps({"event": event, **payload}, sort_keys=True, separators=(",", ":")))


def run_child(request: dict, *, emit=None) -> dict:
    """Perform ONE write, authoritatively, and return the operator-facing response. Called in the accepted
    child (real launch) or in-process by tests through `dispatch(run=run_child)`.

    `emit`, when given, is a line sink: `run_child` writes a `begin` line (with the pre-minted record id)
    before the write lock and a `committed`/`already_pinned` line after the append, so a child that dies
    mid-write still leaves the parent something to reason about. The `committed` line also carries the
    committed byte length, so a lost-confirmation reply can state what landed. A refusal the inner write
    raises is caught and returned as `{"refused": <plain sentence>}` — never re-raised — so the real
    cross-process child and the in-process seam classify identically."""
    verb = request.get("verb")
    if verb not in _VERBS:
        # Not an operator-facing refusal: a malformed request is our bug, so it faults rather than refuses.
        raise DispatchFaulted(f"write dispatch received an unknown verb {verb!r}")
    accepted_id = records.new_record_id()

    def on_event(kind: str, payload: dict) -> None:
        record = payload.get("record")
        if kind == "begin":
            _emit_line(emit, "begin", {"id": payload.get(records.RECORD_ID_KEY)})
        elif kind == "committed":
            line = {"record": _safe_record(record)}
            if payload.get("bytes") is not None:
                line["bytes"] = payload.get("bytes")
            _emit_line(emit, "committed", line)
        elif kind == "already_pinned":
            _emit_line(emit, "already_pinned", {"record": _safe_record(record)})

    try:
        if verb == "pin":
            record = pins.add(
                request.get("text") or "",
                session_id=request.get("session_id"),
                via=records.PIN_VIA_ASSISTANT,
                accepted_id=accepted_id, emit=on_event, dedup=True,
            )
            live = pins.list_pins()
            response = {"id": record[records.RECORD_ID_KEY], "text": record["text"],
                        records.PIN_VIA_KEY: record[records.PIN_VIA_KEY], "total": len(live)}
            if len(live) >= pins.PIN_PRUNE_HINT_AT:
                response["note"] = (
                    f"Saved. You now have {len(live)} pinned notes. The session-start briefing shows the "
                    "newest as one-line titles and folds the older ones behind a loud disclosed count — "
                    "they stay safe and readable with list-pins. A list this long is worth a prune when "
                    "it's convenient; tell me which to drop.")
        elif verb == "withhold":
            forget.withhold(record_id=request.get("record_id"), session_id=request.get("session_id"),
                            accepted_id=accepted_id, emit=on_event)
            what = "that conversation" if request.get("session_id") else "that note"
            response = {"withheld": f"{what} is out of recall now. It is still saved — say the word and it "
                                    "comes back."}
        else:  # restore
            forget.restore(record_id=request.get("record_id"), session_id=request.get("session_id"),
                           accepted_id=accepted_id, emit=on_event)
            what = "that conversation" if request.get("session_id") else "that note"
            response = {"restored": f"{what} is back in recall."}
    except refusals.EngineRefusal as exc:
        return {"refused": str(exc)}
    return response


def _safe_record(record) -> dict:
    """The subset of a stored record safe to print on the forensic `committed` line: identity and the
    already-scrubbed stored text, never anything the scrubber has not already passed."""
    if not isinstance(record, dict):
        return {}
    return {k: record.get(k) for k in (records.RECORD_ID_KEY, "text", records.PIN_VIA_KEY, "kind")
            if record.get(k) is not None}


# --------------------------------------------------------------------------------------------------------- #
# Parent side: relay, classify once, never retry.
# --------------------------------------------------------------------------------------------------------- #

#: The parent's patience for one dispatched write. Comfortably above the SR5 five-second ceiling the child's
#: own write path is measured against, so this trips only on a genuinely stuck child, not a slow-but-healthy
#: one.
_TIMEOUT_SECONDS = 30

#: The grace given a killed child to reap after `kill()`, so `_spawn_accepted_child` never blocks forever on
#: a child that will not die and never leaves a zombie behind.
_REAP_SECONDS = 5

#: A write we KNOW landed (a committed receipt, or a positive read-back) whose full confirmation line did not
#: make it back: the reply is rebuilt from the receipt, honestly flagged, and nothing is retried.
_UNCONFIRMED_NOTE = (
    "This was saved — the write's own receipt confirms it — but the child's final confirmation did not come "
    "back cleanly, so this reply was rebuilt from that receipt. Nothing was retried.")

#: A write that began but has no commit evidence yet while the child could still be running: never reported
#: as nothing-saved, because it may still be completing.
_STILL_UNCONFIRMED_NOTE = (
    "This may still be completing and was not confirmed. Nothing was retried. Give it a moment, then check "
    "with a search before saving it again.")


def dispatch(request: dict, *, run=None) -> dict:
    """Run the write once through `run` (default: the real accepted-child launch) and return the child's
    operator-facing response, or raise. The runner is called exactly once — a memory write is never retried,
    since a retry cannot tell a lost confirmation from a lost write and would risk doubling it.

    The runner may return either the new classified envelope (`{"outcome": ...}`, from the cross-process
    launcher) or, for the in-process test seam `run=run_child`, the operator response directly (or
    `{"refused": ...}`). Both are normalized here: a refusal becomes a `DispatchRefused` (forwarded verbatim
    by the server boundary); a fault or a never-launched child becomes a `DispatchFaulted` (masked); a
    committed or unconfirmed write returns its operator-facing response."""
    runner = run if run is not None else _spawn_accepted_child
    outcome = runner(request)
    if not isinstance(outcome, dict):
        raise DispatchFaulted("write dispatch produced no readable outcome")
    kind = outcome.get("outcome")
    if kind is None:
        # Legacy in-process seam: `run_child` returns the operator response directly, or {"refused": ...}.
        if "refused" in outcome:
            raise DispatchRefused(str(outcome["refused"]))
        return outcome
    if kind == "refused":
        raise DispatchRefused(str(outcome.get("sentence", "")))
    if kind == "faulted":
        raise DispatchFaulted("write dispatch did not complete")
    if kind == "not_attempted":
        raise DispatchFaulted("write dispatch could not start")
    if kind in ("committed", "unconfirmed"):
        return outcome.get("response", {})
    raise DispatchFaulted(f"write dispatch produced an unknown outcome {kind!r}")


def _spawn_accepted_child(request: dict) -> dict:
    """Launch one accepted child from the exact accepted tree on disk NOW, hand it the request on stdin, and
    fold its outcome from stdout. The child is `write_dispatch.py` re-entered under `attended-write-dispatch`
    through the working-tree `accepted_hook_dispatch.py`, which re-verifies and re-materializes the accepted
    tree before running a byte of it.

    A stuck or client-cancelled child is terminated and reaped BEFORE its outcome is read, so no lingering
    child can keep committing behind a reported result. A child that never launches is `not_attempted`
    (distinct from a child that ran and faulted) and is recorded to the stranding log; a launched child that
    leaves no evidence of a commit is `faulted` and likewise recorded.

    This cannot succeed from a tree whose accepted materialization does not yet contain this file — a
    pre-merge worktree — where it surfaces the launcher's own refusal. That is expected: write authority
    follows the merge, and the cross-process launch begins working once this change is accepted."""
    root = os.environ.get("ENGINE_PROJECT_ROOT") or os.getcwd()
    launcher = os.path.join(root, ".engine", "tools", "accepted_hook_dispatch.py")
    argv = [sys.executable, "-I", "-S", launcher, "attended",
            "--root", root, "--script", ".engine/tools/memory/write_dispatch.py",
            "--operation", OPERATION, "--"]
    payload = json.dumps(request, sort_keys=True, separators=(",", ":"))
    verb = request.get("verb")
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except (OSError, ValueError, subprocess.SubprocessError):
        # The child never launched: not-attempted, a distinct class from a child that ran and faulted.
        stranding_log.record_dispatch_outcome(
            stranding_log.DispatchOutcome.NOT_ATTEMPTED, stranding_log.EXIT_NOT_LAUNCHED)
        return {"outcome": "not_attempted"}
    try:
        stdout, _ = proc.communicate(input=payload, timeout=_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        # Stuck or client-cancelled: kill and REAP before reading, so the child cannot keep committing behind
        # a reported outcome, and salvage whatever it managed to print before the kill.
        proc.kill()
        try:
            stdout, _ = proc.communicate(timeout=_REAP_SECONDS)
        except subprocess.TimeoutExpired:
            stdout = ""
    returncode = proc.returncode if proc.returncode is not None else stranding_log.EXIT_NOT_LAUNCHED
    # The child is dead and reaped by now, so a read-back is authoritative: child_alive is False.
    outcome = _classify_outcome(
        stdout, returncode=returncode, verb=verb, request=request,
        read_back=_ledger_read_back, child_alive=False)
    if outcome.get("outcome") == "faulted":
        stranding_log.record_dispatch_outcome(stranding_log.DispatchOutcome.FAULTED, returncode)
    return outcome


def _parse_events(stdout: str) -> list:
    """The child's stdout as an ordered list of well-formed event objects. Non-JSON lines and any line that
    is not a dict carrying a string `event` are dropped — so a corrupt or partial line can never be mistaken
    for a receipt."""
    events = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("event"), str):
            events.append(parsed)
    return events


def _classify_outcome(stdout, *, returncode, verb, request, read_back, child_alive) -> dict:
    """Fold one child's line stream into exactly one outcome, keyed on what the child proved and whether it
    could still be writing. Pure: every liveness and read-back fact arrives as an argument, so the same
    function classifies a real launch and a test's synthetic stream.

      committed   — a response line, OR a well-formed committed/already_pinned receipt, OR a positive
                    read-back of the pre-minted id: the write LANDED.
      refused     — a response line carrying the child's plain refusal sentence.
      unconfirmed — a begin line, no commit evidence, read-back absent, but the child is still alive: it may
                    yet commit, so this is NEVER reported as nothing-saved.
      faulted     — a begin line with the child confirmed dead, no commit evidence and read-back absent; or
                    no begin line at all (the child never reached the write body).

    (`not_attempted` is the launcher's call — only it knows the child never started — and never reaches here.)
    """
    events = _parse_events(stdout)

    # 1. The authoritative response line the child prints last: a committed response (any verb) or a refusal.
    for event in reversed(events):
        if event.get("event") == "response":
            payload = event.get("response")
            payload = payload if isinstance(payload, dict) else {}
            if "refused" in payload:
                return {"outcome": "refused", "sentence": str(payload["refused"])}
            return {"outcome": "committed", "response": payload}

    # 2. A WELL-FORMED committed/already_pinned receipt (a dict record carrying an id). A malformed receipt is
    #    not trusted here: it falls through to the read-back, so a corrupt or fabricated line can never by
    #    itself mint a success — only real disk state can.
    committed = next(
        (e for e in reversed(events)
         if e.get("event") in ("committed", "already_pinned")
         and isinstance(e.get("record"), dict)
         and e["record"].get(records.RECORD_ID_KEY) is not None),
        None)
    if committed is not None:
        return {"outcome": "committed",
                "response": _committed_response(verb, request, committed.get("record"))}

    # 3. A begin line: the child reached the write body. Decide on disk first, then on liveness.
    begin = next((e for e in reversed(events) if e.get("event") == "begin"), None)
    if begin is not None:
        record_id = begin.get("id")
        found = read_back(record_id) if (read_back is not None and record_id) else None
        if found is not None:
            # Physically on disk: the write committed; only its confirmation was lost.
            return {"outcome": "committed", "response": _committed_response(verb, request, found)}
        if child_alive:
            # The child could still be mid-commit — hold it open, never call it nothing-saved.
            return {"outcome": "unconfirmed",
                    "response": _still_unconfirmed_response(verb, request, record_id)}
        # Confirmed dead, nothing on disk, no receipt: a genuine fault.
        return {"outcome": "faulted", "returncode": returncode}

    # 4. No begin line at all: the child never reached the write body.
    return {"outcome": "faulted", "returncode": returncode}


def _target_phrase(request: dict) -> str:
    return "that conversation" if request.get("session_id") else "that note"


def _committed_response(verb: str, request: dict, record) -> dict:
    """Rebuild the operator-facing response for a write we KNOW committed (a committed receipt or a positive
    read-back) when the child's authoritative response line did not make it back. Shaped per verb, so a lost
    confirmation reads like the verb that actually ran rather than defaulting to a pin, and always carries the
    honest `_UNCONFIRMED_NOTE`."""
    record = record if isinstance(record, dict) else {}
    if verb == "withhold":
        return {"withheld": f"{_target_phrase(request)} is out of recall now. It is still saved — say the "
                            "word and it comes back.", "unconfirmed": _UNCONFIRMED_NOTE}
    if verb == "restore":
        return {"restored": f"{_target_phrase(request)} is back in recall.", "unconfirmed": _UNCONFIRMED_NOTE}
    # pin (and any unknown verb, which run_child would already have faulted): return what identity we have.
    response = {"unconfirmed": _UNCONFIRMED_NOTE}
    if record.get(records.RECORD_ID_KEY) is not None:
        response["id"] = record.get(records.RECORD_ID_KEY)
    if record.get("text") is not None:
        response["text"] = record.get("text")
    if record.get(records.PIN_VIA_KEY) is not None:
        response[records.PIN_VIA_KEY] = record.get(records.PIN_VIA_KEY)
    return response


def _still_unconfirmed_response(verb: str, request: dict, record_id) -> dict:
    """The reply for a write that began but has no commit evidence yet while the child could still be running:
    honest that it may still be completing, and never a claim that nothing was saved."""
    response = {"unconfirmed": _STILL_UNCONFIRMED_NOTE}
    if verb == "pin" and record_id is not None:
        response["id"] = record_id
    return response


def _ledger_read_back(record_id):
    """Read-only confirmation: return the stored record/marker carrying `record_id`, or None if absent. Reads
    the RAW ledger (`ledger.read().records`, not `iter_records`) so a withhold/restore marker — which the
    witness layer would fold away — is still seen, since that marker may be exactly the write being confirmed.
    Never writes; any read failure is treated as absent, so a broken read can only withhold a success, never
    invent one."""
    if not record_id:
        return None
    try:
        result = ledger.read()
    except Exception:
        return None
    for rec in getattr(result, "records", None) or []:
        if isinstance(rec, dict) and rec.get(records.RECORD_ID_KEY) == record_id:
            return rec
    return None


# --------------------------------------------------------------------------------------------------------- #
# Child entry: read one request from stdin, run it, stream the line stream, exit.
# --------------------------------------------------------------------------------------------------------- #

def main(argv: "list | None" = None) -> int:
    raw = sys.stdin.read()
    try:
        request = json.loads(raw) if raw.strip() else {}
    except ValueError:
        request = {}

    def _stream(line: str) -> None:
        # Stream each forensic line the instant it is produced — the begin line rides out BEFORE the child
        # takes the ledger lock, so a parent watching stdout learns the pre-minted id even if the child dies
        # mid-write. flush so the bytes leave this process immediately, not buffered until exit. A parent that
        # has already gone away (a closed pipe) must not turn a completing write into a crash.
        try:
            print(line, flush=True)
        except (BrokenPipeError, OSError):
            pass

    response = run_child(request, emit=_stream)
    try:
        print(json.dumps({"event": "response", "response": response},
                         sort_keys=True, separators=(",", ":")), flush=True)
    except (BrokenPipeError, OSError):
        pass
    return 0


_mutation_authority.install_module_guards(globals())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
