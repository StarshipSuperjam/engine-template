"""clawmem_export.py — a terminal-gated, point-in-time export of the memory ledger in ClawMem-ingestible form.

WHAT THIS IS FOR. The memory substrate is being replaced by ClawMem as an internal dependency (program
prg_13dc60836f68). Before any of that is decided, this repo's existing history has to be portable: a
machine-readable projection of the captured conversation that ClawMem's claude-code importer accepts, plus the
curated summaries over it and a manifest that accounts for everything left out. That projection is valuable
standing alone — a readable, greppable copy of the history — and it is the corpus the migration trial (X2)
measures ClawMem against.

PORTABILITY, NOT A BACKUP. The export is LOSSY BY DESIGN and has NO restore path: it drops bookkeeping markers,
rejoins chunked messages, masks secret shapes, and honours the operator's withholds. The memory vault
(`backup_vault.py`) remains the only backup. Every guarantee here is POINT-IN-TIME — true as of the moment the
file is written. A later withhold, a merged erasure, or a new masking rule does not reach an export already on
disk, nor any ClawMem store built from it. The manifest says so, and the migration-trial doc (X2) makes the
operator delete every residue copy when the trial is done.

THE TERMINAL GATE IS THE CONSENT CONTROL, and it is a built control rather than a line of prose. The export is
the operator's private conversation in cleartext leaving the ledger's governance, and this repository's actor
model is that an AI session runs commands — so a blanket `uv run` grant would make "operator-attended"
unenforceable by convention. Following `erase.py`, the verb REFUSES without a real terminal on both stdin and
stdout, and that check comes FIRST, before the store is read at all (an automated caller must not learn which
sessions or records exist one refusal at a time). It also refuses while any operator-ordered erasure is still
pending, and refuses an in-repo destination (`export.assert_safe_destination`).

FIDELITY. The export mirrors what RECALL surfaces, not the raw bytes: harness-injected pseudo-turns are dropped
(judged MESSAGE-wise, so a legacy continuation summary's tail chunks travel with its head), fused harness spans
are substituted out (`records.mark_harness_spans`, exactly as the window reader shows them), the operator's
withholds are honoured (a withheld record, a withheld session, and a removed pin — which IS a withheld record —
are all absent), and the rejoined message is scrubbed FAIL-CLOSED. There is no per-session cap: `export.py`'s
5000-record bound is deliberately inverted here — a whole session exports whole — and the terminal gate is the
compensating control for reading the store wholesale.

stdlib + the memory package only; no outbound calls.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import moment  # noqa: E402 — the one sanctioned time-idiom seam (RFC3339 formatting)
from memory import compact, export, forget, ledger, pins, recall, records, scrub  # noqa: E402

# The ClawMem claude-code line-format contract, verified hands-on in the Phase-0 sandbox against ClawMem's
# `src/normalize.ts` at this commit. The trial (X2) records the ACTUAL commit + resolved lockfile it runs
# against, so a drift from this is attributable; an import failure at scale is an exporter defect, never a
# retrieval verdict.
CLAWMEM_COMMIT = "ba09cb8"                       # v0.37.0
CLAWMEM_LINE_SCHEMA = {                          # one JSON object per line, in conversations/<session>.jsonl
    "type": "user | assistant",
    "timestamp": "RFC3339 (UTC, whole-second Z form)",
    "message": {"content": "the rejoined, scrubbed message text"},
}

MANIFEST_SCHEMA = "clawmem-export-manifest.v1"

# A conversation session id is a uuid4 hex. It is validated to this shape BEFORE it is ever used as a filename,
# so a malformed or hostile identifier can neither escape the conversations/ directory nor collide with a
# roll-up cluster sentinel (`tag:...`, which is not a conversation and carries no transcript).
_SESSION_ID_RE = re.compile(r"\A[0-9a-f]{16,64}\Z")

_CURATED_KINDS = (records.EPISODIC_KIND, records.GIST_KIND)
# Every kind that is machinery rather than content: recall never returns one, and the export drops and counts
# each. `consolidated` is included — it is a structural marker, not a recall note, and the curated layer this
# export carries is the episodic/gist content above it.
_BOOKKEEPING_KINDS = (
    records.MARKER_KIND, records.ROLLUP_KIND, records.REINFORCEMENT_KIND, records.SUPERSEDED_KIND,
    records.WITHHOLD_KIND, records.RESTORE_KIND, records.ERASURE_KIND,
)

_POINT_IN_TIME = (
    "This is a point-in-time portability export, not a backup: it is lossy by design and has no restore path — "
    "the memory vault remains the only backup. Every guarantee here holds as of the moment this was written. A "
    "later withhold, a merged erasure, or a new masking rule does not reach this export or any store built from "
    "it. Long messages were captured in pieces and are rejoined here. Secret-shaped text was masked on the way "
    "out, but by policy names, email addresses and phone numbers are left intact — read every file as private "
    "material."
)

_EXPECTED_RUNTIME = (
    "Runtime: expect roughly one to three minutes for a full store (hundreds of sessions, tens of thousands of "
    "records). It makes a few sequential passes over the ledger and writes one JSONL file per session; there is "
    "no per-session cap, so a very large session exports whole."
)

_NO_TERMINAL = (
    "this export writes your private conversation history in cleartext, so it runs only when you run it yourself "
    "at a real terminal. Its stdin and stdout are not a terminal here, so nothing was read and nothing was "
    "written. Run it from your own shell."
)


class ExportRefused(ValueError):
    """The export did not happen, with the plain-language reason. Raised, never returned, so a caller can never
    report an export that did not run or a file that was never written."""


class _ScrubFault(RuntimeError):
    """The scrubber raised while masking a rejoined message. `scrub.scrub_text` is fail-SOFT by contract, so this
    only fires when the scrubber itself is broken — and when it does, the export ABORTS rather than emit one
    unmasked message. Caught in `export_all`, which cleans up and re-raises as `ExportRefused`."""


def _on_a_terminal() -> bool:
    """True iff both stdin and stdout are real terminals. The consent gate; kept tiny so a test can drive it by
    replacing `sys.stdin.isatty` / `sys.stdout.isatty`."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _rfc3339(ts: int) -> str:
    """A whole-second UTC RFC3339 stamp (`...Z`), via the one sanctioned time seam. Capture stamps whole
    seconds, so `moment.to_z` renders no sub-second part."""
    return moment.to_z(ts)


def _digest_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _digest_file(path: str) -> str:
    with open(path, "rb") as fh:
        return _digest_bytes(fh.read())


def _scrub_fail_closed(text: str) -> str:
    """Scrub one REJOINED message, failing CLOSED.

    The message is scrubbed AFTER its chunks are rejoined, so a secret straddling a chunk boundary — invisible to
    a per-chunk scrub — is caught. `scrub.scrub_text` swallows its own faults and returns the input unchanged
    (fail-soft, right for capture); that is wrong for an export, where returning unmasked text on a fault is a
    silent leak. So the call is wrapped and a scrubber that RAISES aborts the export via `_ScrubFault`."""
    try:
        return scrub.scrub_text(text)
    except Exception as exc:  # noqa: BLE001 — a faulting scrubber must stop the export, never emit unmasked text
        raise _ScrubFault(str(exc)) from exc


def _join_messages(turns: list) -> list:
    """Rejoin the chunks of each captured message into one readable turn, CARRYING `ts` — the one field recall's
    own join drops (it renders for display, which needs no timestamp; the ClawMem line needs one).

    Otherwise this mirrors `recall._join_chunks` exactly: chunks of one message share a (`seq`, `speaker`) and
    concatenate in append order, each chunk's fused harness block substituted out first with
    `records.mark_harness_spans` — the same presentation the window reader gives. A record with no usable ordinal
    never merges (its identity is unknown, and guessing splices unrelated messages together).

    Returns dicts `{seq, speaker, ts, text, chunks, scaffolded}` — `scaffolded` records whether a harness span
    was substituted anywhere in the message, for the manifest's omission account. Owning the join rather than
    reusing recall's private helper is a divergence risk a reference-comparison test pins closed: it runs this
    and `recall._join_chunks` on the same fixture and asserts the shared fields agree."""
    joined: list = []
    for record in turns:
        seq = recall._seq_of(record)
        speaker = record.get("speaker") if isinstance(record.get("speaker"), str) else "unknown"
        raw = record.get("text") if isinstance(record.get("text"), str) else ""
        text = records.mark_harness_spans(raw)
        scaffolded = text != raw
        ts = record.get("ts")
        previous = joined[-1] if joined else None
        if (previous is not None and seq is not None and previous["seq"] == seq
                and previous["speaker"] == speaker):
            previous["text"] += text
            previous["chunks"] += 1
            previous["scaffolded"] = previous["scaffolded"] or scaffolded
            continue
        joined.append({"seq": seq, "speaker": speaker, "ts": ts, "text": text, "chunks": 1,
                       "scaffolded": scaffolded})
    return joined


def _classify(src: str):
    """One content pass over the ledger, with the withhold set and the injected-message set each resolved ONCE
    up front. Returns `(by_session, curated, live_pins, census, omission)`.

    The bucketing traversal is the single pass the plan names; the derivations it leans on
    (`withheld_targets`, `_injected_message_keys`, `pins.list_pins`) are `forget`'s own live-record machinery,
    each a cheap sequential read done once — the same shape `recall.session_cards` uses. Every record the export
    does NOT carry is COUNTED here by the reason it was dropped, which is what the manifest's omission account is
    built from."""
    withheld_ids, withheld_sessions = forget.withheld_targets(src)
    injected_keys = forget._injected_message_keys(src)

    by_session: dict = {}
    curated: list = []
    census: dict = {}
    omission = {
        "withheld_sessions": set(),
        "withheld_session_messages": 0,
        "withheld_records": set(),
        "injected_filtered_messages": 0,
        "legacy_skipped": 0,
        "bookkeeping_dropped": {},
    }

    def _note_withheld_record(record):
        rid = record.get(records.RECORD_ID_KEY)
        if isinstance(rid, str) and rid:
            omission["withheld_records"].add(rid)

    for record in ledger.iter_records(path=src):
        if not isinstance(record, dict):
            continue
        kind = record.get("kind")
        census[kind] = census.get(kind, 0) + 1
        if kind == records.AMBIENT_CAPTURE_KIND:
            sid = record.get("session_id")
            seq = record.get("seq")
            # Injectedness, record-wise then MESSAGE-wise: `is_genuine_turn` catches a tagged pseudo-turn and a
            # legacy head chunk; the message-key set catches the tail chunks of an untagged legacy summary that
            # travel with a head neither arm recognises on its own.
            if not recall.is_genuine_turn(record):
                omission["injected_filtered_messages"] += 1
                continue
            if (isinstance(sid, str) and isinstance(seq, int) and not isinstance(seq, bool)
                    and (sid, seq) in injected_keys):
                omission["injected_filtered_messages"] += 1
                continue
            if isinstance(sid, str) and sid in withheld_sessions:
                omission["withheld_sessions"].add(sid)
                omission["withheld_session_messages"] += 1
                continue
            if forget.is_withheld(record, withheld_ids, withheld_sessions):
                _note_withheld_record(record)
                continue
            # Legacy hygiene: a record with no hex session id (unusable, and unsafe as a filename), no
            # user/assistant speaker, or no integer ts cannot be rendered as a ClawMem line — skipped and counted.
            if not (isinstance(sid, str) and _SESSION_ID_RE.match(sid)):
                omission["legacy_skipped"] += 1
                continue
            if record.get("speaker") not in ("user", "assistant"):
                omission["legacy_skipped"] += 1
                continue
            if not (isinstance(record.get("ts"), int) and not isinstance(record.get("ts"), bool)):
                omission["legacy_skipped"] += 1
                continue
            by_session.setdefault(sid, []).append(record)
        elif kind in _CURATED_KINDS:
            if forget.is_withheld(record, withheld_ids, withheld_sessions):
                _note_withheld_record(record)
                continue
            curated.append(record)
        elif kind == records.PIN_KIND:
            # Pins are emitted ONLY through `pins.list_pins()` (below), which reads live pins — so a removed
            # (withheld) pin is absent exactly as it is from recall. Counted in the census, never emitted here.
            continue
        elif kind in _BOOKKEEPING_KINDS:
            omission["bookkeeping_dropped"][kind] = omission["bookkeeping_dropped"].get(kind, 0) + 1
        else:
            omission["legacy_skipped"] += 1

    live_pins = pins.list_pins(path=src)
    return by_session, curated, live_pins, census, omission


def export_all(dest: str, *, path: "str | None" = None, now: "int | None" = None) -> dict:
    """Write the whole export under `dest` and return the manifest dict.

    Refuses an in-repo destination and a non-empty one, refuses while any erasure is pending, then makes one
    content pass and renders conversations/, curated/ and meta/. Does NOT check the terminal — that gate is the
    verb's, enforced in `main`; this core is the reusable, directly-testable half. A scrub fault anywhere aborts
    the whole export and removes the partial directory: nothing half-masked is ever left behind."""
    export.assert_safe_destination(dest)
    if os.path.isdir(dest) and os.listdir(dest):
        raise ExportRefused(
            f"{dest} already has files in it. Choose a fresh, empty directory, so a stale export — a session "
            "since withheld, say — cannot survive beside this one and quietly falsify the manifest.")
    if os.path.exists(dest) and not os.path.isdir(dest):
        raise ExportRefused(f"{dest} exists and is not a directory.")

    src = ledger.ledger_path() if path is None else path

    pending = compact.pending_erasures(src)
    if pending:
        raise ExportRefused(
            f"{pending} operator-ordered erasure(s) have not been carried out yet, and their target records are "
            "still resident in the ledger. Exporting now would copy out content you asked to have erased. "
            "Nothing was written — let compaction enact the erasure first, then export.")

    by_session, curated, live_pins, census, omission = _classify(src)

    created_dest = not os.path.exists(dest)
    try:
        return _render(dest, by_session, curated, live_pins, census, omission, src=src, now=now)
    except _ScrubFault as exc:
        # Fail CLOSED: tear down whatever was written so no half-masked artifact survives, then refuse loudly.
        # This teardown is a registered export-artifact mutation surface (see memory/mutation_contract.py).
        if created_dest:
            shutil.rmtree(dest, ignore_errors=True)
        else:
            for sub in ("conversations", "curated", "meta"):
                shutil.rmtree(os.path.join(dest, sub), ignore_errors=True)
        raise ExportRefused(
            f"the scrubber faulted while masking a message ({exc}); the export was aborted and the partial "
            "output removed rather than risk writing unmasked text.") from exc


def _render(dest, by_session, curated, live_pins, census, omission, *, src, now) -> dict:
    conversations_dir = os.path.join(dest, "conversations")
    curated_dir = os.path.join(dest, "curated")
    meta_dir = os.path.join(dest, "meta")
    for directory in (dest, conversations_dir, curated_dir, meta_dir):
        os.makedirs(directory, exist_ok=True)

    scrub_altered = 0
    scaffolding_marked = 0
    earliest = latest = None
    conversations_manifest: dict = {}
    file_digests: dict = {}

    # conversations/ — one JSONL per session, deterministic session order, stable seq order within.
    for sid in sorted(by_session):
        joined = _join_messages(sorted(by_session[sid], key=recall._sort_key))
        lines = []
        for message in joined:
            if message["scaffolded"]:
                scaffolding_marked += 1
            pre = message["text"]
            scrubbed = _scrub_fail_closed(pre)
            if scrubbed != pre:
                scrub_altered += 1
            ts = message["ts"]
            earliest = ts if earliest is None else min(earliest, ts)
            latest = ts if latest is None else max(latest, ts)
            lines.append(json.dumps(
                {"type": message["speaker"], "timestamp": _rfc3339(ts), "message": {"content": scrubbed}},
                ensure_ascii=False, sort_keys=True))
        rel = f"conversations/{sid}.jsonl"
        full = os.path.join(dest, rel)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(("\n".join(lines) + "\n") if lines else "")
        file_digests[rel] = _digest_file(full)
        conversations_manifest[sid] = {"messages": len(joined), "file": rel}

    # curated/ — episodic and gist notes as one readable markdown file, withhold-honoured and scrubbed.
    curated_rel = "curated/notes.md"
    curated_sorted = sorted(
        curated,
        key=lambda r: (str(r.get("session_id") or ""),
                       r.get("ts") if isinstance(r.get("ts"), int) and not isinstance(r.get("ts"), bool) else 0,
                       str(r.get(records.RECORD_ID_KEY) or "")))
    sections = ["# Curated notes — episodic summaries and gists", "", f"_{_POINT_IN_TIME}_", "",
                f"{len(curated_sorted)} note(s). These are the disposable curated layer over the conversation, "
                "not imported into ClawMem; the conversations/ transcripts are the canonical record.", ""]
    for note in curated_sorted:
        pre = note.get("text") if isinstance(note.get("text"), str) else ""
        scrubbed = _scrub_fail_closed(pre)
        if scrubbed != pre:
            scrub_altered += 1
        when = note.get("ts")
        stamp = _rfc3339(when) if isinstance(when, int) and not isinstance(when, bool) else "unknown date"
        sections.append(f"## {note.get('kind') or 'note'} · {note.get('session_id') or '—'} · {stamp}")
        sections.append("")
        sections.append(scrubbed)
        sections.append("")
    curated_full = os.path.join(dest, curated_rel)
    with open(curated_full, "w", encoding="utf-8") as fh:
        fh.write("\n".join(sections) + "\n")
    file_digests[curated_rel] = _digest_file(curated_full)

    # meta/pins.jsonl — the live pins, read only through pins.list_pins(), one human-readable record per line.
    pins_rel = "meta/pins.jsonl"
    pin_lines = []
    for pin in live_pins:
        pre = pin.get("text") if isinstance(pin.get("text"), str) else ""
        scrubbed = _scrub_fail_closed(pre)
        if scrubbed != pre:
            scrub_altered += 1
        pin_lines.append(json.dumps({
            "id": pin.get(records.RECORD_ID_KEY),
            "text": scrubbed,
            "pinned_via": pin.get(records.PIN_VIA_KEY),
            "source_session": pin.get(records.PIN_SOURCE_SESSION_KEY),
            "ts": pin.get("ts"),
        }, ensure_ascii=False, sort_keys=True))
    pins_full = os.path.join(dest, pins_rel)
    with open(pins_full, "w", encoding="utf-8") as fh:
        fh.write(("\n".join(pin_lines) + "\n") if pin_lines else "")
    file_digests[pins_rel] = _digest_file(pins_full)

    try:
        generation = ledger.generation(for_path=src)
    except Exception:  # noqa: BLE001 — a missing sidecar is not a reason to fail an export
        generation = None

    manifest = {
        "schema": MANIFEST_SCHEMA,
        "generated_at": _rfc3339(now) if now is not None else moment.utc_now(),
        "point_in_time_caveats": _POINT_IN_TIME,
        "clawmem": {
            "commit": CLAWMEM_COMMIT,
            "line_schema": CLAWMEM_LINE_SCHEMA,
            "import_path": ("conversations/ only. curated/ is the human-readable curated layer and is NOT "
                            "imported into ClawMem; meta/ is provenance."),
        },
        "source_ledger_generation": generation,
        "time_range": {
            "earliest": _rfc3339(earliest) if earliest is not None else None,
            "latest": _rfc3339(latest) if latest is not None else None,
        },
        "census_by_kind": census,
        "counts": {
            "sessions": len(conversations_manifest),
            "messages": sum(entry["messages"] for entry in conversations_manifest.values()),
            "curated_notes": len(curated_sorted),
            "pins": len(live_pins),
        },
        "conversations": conversations_manifest,
        "curated": {"file": curated_rel, "count": len(curated_sorted)},
        "pins": {"file": pins_rel, "count": len(live_pins)},
        "omission_account": {
            "withheld_sessions": sorted(omission["withheld_sessions"]),
            "withheld_session_messages_skipped": omission["withheld_session_messages"],
            "withheld_records": sorted(omission["withheld_records"]),
            "injected_filtered_messages": omission["injected_filtered_messages"],
            "scaffolding_marked_turns": scaffolding_marked,
            "scrub_altered_messages": scrub_altered,
            "legacy_skipped": omission["legacy_skipped"],
            "bookkeeping_kinds_dropped": omission["bookkeeping_dropped"],
        },
        "files": file_digests,
    }
    manifest_full = os.path.join(dest, "meta", "manifest.json")
    with open(manifest_full, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    return manifest


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(
        prog="clawmem_export.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=("Export the memory ledger as a point-in-time, ClawMem-ingestible portability artifact.\n\n"
                     + _POINT_IN_TIME + "\n\n" + _EXPECTED_RUNTIME))
    parser.add_argument("dest", help="a fresh directory to write into, OUTSIDE any git project (or a path the "
                                     "project already ignores)")
    args = parser.parse_args(argv)

    # THE TERMINAL GATE COMES FIRST, before the store is touched at all: an automated caller must not be able to
    # learn which sessions or records exist one refusal message at a time (the erase.py lesson).
    if not _on_a_terminal():
        print(f"Not exported: {_NO_TERMINAL}")
        return 1

    try:
        manifest = export_all(args.dest)
    except (ExportRefused, export.ExportRefused) as exc:
        print(f"Not exported: {exc}")
        return 1

    counts = manifest["counts"]
    print(f"Exported to {args.dest}")
    print(f"  {counts['sessions']} session(s), {counts['messages']} message(s), "
          f"{counts['curated_notes']} curated note(s), {counts['pins']} pin(s).")
    omission = manifest["omission_account"]
    print(f"  omitted: {len(omission['withheld_sessions'])} withheld session(s), "
          f"{len(omission['withheld_records'])} withheld record(s), "
          f"{omission['injected_filtered_messages']} injected message(s), "
          f"{omission['legacy_skipped']} legacy record(s). See meta/manifest.json for the full account.")
    return 0


# Install the mutation-authority guards on this module's registered writers (`_render`, `export_all`), the
# same way export.py does. Export-artifact writes are allowed unqualified (degraded -> allow), so the wrapping
# is transparent at runtime; it exists so the write surfaces carry their registry ids for the coverage check.
try:
    from . import mutation_authority as _mutation_authority
except ImportError:  # direct CLI
    from memory import mutation_authority as _mutation_authority
_mutation_authority.install_module_guards(globals())


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
