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
    from . import forget, pins, records, refusals
    from . import execution_context
except ImportError:  # direct CLI / accepted runpy entry (`sys.argv[0]` is this file)
    from memory import forget, pins, records, refusals  # noqa: F401
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
    mid-write still leaves the parent something to reason about. A refusal the inner write raises is caught
    and returned as `{"refused": <plain sentence>}` — never re-raised — so the real cross-process child and
    the in-process seam classify identically."""
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
            _emit_line(emit, "committed", {"record": _safe_record(record)})
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

def dispatch(request: dict, *, run=None) -> dict:
    """Run the write once through `run` (default: the real accepted-child launch) and return the child's
    operator-facing response, or raise. A `{"refused": ...}` outcome becomes a `DispatchRefused` (forwarded
    verbatim by the server boundary); a fault becomes a `DispatchFaulted` (masked). The runner is called
    exactly once — a memory write is never retried, since a retry cannot tell a lost confirmation from a lost
    write and would risk doubling it."""
    runner = run if run is not None else _spawn_accepted_child
    outcome = runner(request)
    if not isinstance(outcome, dict):
        raise DispatchFaulted("write dispatch produced no readable outcome")
    if "refused" in outcome:
        raise DispatchRefused(str(outcome["refused"]))
    if "faulted" in outcome:
        raise DispatchFaulted("write dispatch did not complete")
    return outcome


def _spawn_accepted_child(request: dict) -> dict:
    """Launch one accepted child from the exact accepted tree on disk NOW, hand it the request on stdin, and
    read its outcome from stdout. The child is `write_dispatch.py` re-entered under `attended-write-dispatch`
    through the working-tree `accepted_hook_dispatch.py`, which re-verifies and re-materializes the accepted
    tree before running a byte of it.

    This cannot succeed from a tree whose accepted materialization does not yet contain this file — a
    pre-merge worktree — where it surfaces the launcher's own refusal. That is expected: write authority
    follows the merge, and the cross-process launch begins working once this change is accepted."""
    root = os.environ.get("ENGINE_PROJECT_ROOT") or os.getcwd()
    launcher = os.path.join(root, ".engine", "tools", "accepted_hook_dispatch.py")
    argv = [sys.executable, "-I", "-S", launcher, "attended",
            "--root", root, "--script", ".engine/tools/memory/write_dispatch.py",
            "--operation", OPERATION, "--"]
    payload = json.dumps(request, sort_keys=True, separators=(",", ":"))
    try:
        proc = subprocess.run(argv, input=payload, capture_output=True, text=True, timeout=_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return {"faulted": True}
    return _read_outcome(proc.stdout, proc.returncode)


#: The parent's patience for one dispatched write. Comfortably above the SR5 five-second ceiling the child's
#: own write path is measured against, so this trips only on a genuinely stuck child, not a slow-but-healthy
#: one.
_TIMEOUT_SECONDS = 30


def _read_outcome(stdout: str, returncode: int) -> dict:
    """Fold the child's line stream into one outcome. A `response` line is authoritative. Failing that, a
    `committed`/`already_pinned` line means the write DID land — reconstruct a success rather than ever
    claim nothing was saved. Only with no evidence of a commit is it a fault."""
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
    for event in reversed(events):
        if event["event"] == "response":
            payload = {k: v for k, v in event.items() if k != "event"}
            return payload.get("response", payload) if "response" in payload else payload
    committed = next((e for e in reversed(events)
                      if e["event"] in ("committed", "already_pinned")), None)
    if committed is not None:
        record = committed.get("record") or {}
        return {"id": record.get(records.RECORD_ID_KEY), "text": record.get("text"),
                records.PIN_VIA_KEY: record.get(records.PIN_VIA_KEY),
                "unconfirmed": "The write was saved, but its confirmation did not come back cleanly."}
    return {"faulted": True}


# --------------------------------------------------------------------------------------------------------- #
# Child entry: read one request from stdin, run it, print the line stream, exit.
# --------------------------------------------------------------------------------------------------------- #

def main(argv: "list | None" = None) -> int:
    raw = sys.stdin.read()
    try:
        request = json.loads(raw) if raw.strip() else {}
    except ValueError:
        request = {}
    lines = []
    response = run_child(request, emit=lines.append)
    for line in lines:
        print(line)
    print(json.dumps({"event": "response", "response": response},
                     sort_keys=True, separators=(",", ":")))
    return 0


_mutation_authority.install_module_guards(globals())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
