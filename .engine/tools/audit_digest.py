#!/usr/bin/env python3
"""The audit digest (audit-library slice 2) — the engine's committed, plain-language self-attestation
of its own operational health, and the two rules that protect it.

The engine's periodic self-review (the audit) produces a short, human-readable file —
`.engine/audits/audit-digest.md` — that says, in plain words, what it looked at, what it found, and
what it recommends. It is committed so a non-engineer can open it in the repo and read it (the self-map
precedent), and it carries its run-date. This module ships the deterministic machinery around that file;
the scheduled run that actually WRITES one lands later (the audit persona writes the prose; this tool
seals it). Until then no digest exists, and the two rules below handle that honestly.

Unlike the self-map or the knowledge graph, the digest is NOT source-deterministic — it is run-dated,
judgment-bearing prose, so it cannot be regenerated-and-byte-compared. So "fingerprint-gated so it cannot
silently drift" (the audits design) is realized as a SELF-SEAL: the file carries a check-value computed
over its own run-date + body, and the gate recomputes that value and compares. A hand-edit that changes
the file after the audit wrote it — without re-running the audit to re-seal — no longer matches, and the
gate goes red. This catches a *silent* hand-edit, exactly as the self-map gate catches a silent edit of a
generated file; a deliberate re-seal (re-running the audit) passes, which is the intended way to change it.

SEAL INVARIANT (load-bearing): the seal hashes the PARSED `generated` scalar (read through
validate.frontmatter, which normalizes a YAML date to a stable ISO string) plus the RAW body bytes (the
text after the closing `---` fence, read with newline='' so a CRLF/LF change is caught, not masked). It
never hashes the frontmatter's serialized text, so re-quoting or re-ordering the header cannot affect
validity — only a change to the run-date or the body can. The `fingerprint` field is never part of its own
input.

Library + CLI (mirrors self_map.py — plain language first):

  uv run --directory .engine -- python tools/audit_digest.py seal <file> [YYYY-MM-DD] [--body-file P]  # stamp + seal
  uv run --directory .engine -- python tools/audit_digest.py check [<file>]            # is the seal intact?
  uv run --directory .engine -- python tools/audit_digest.py staleness [<file>]        # how fresh is it?

The two CI/audit-prep rules are thin custom/script entries over check()/staleness():
audit_digest_fingerprint_check.py (the CI seal gate) and audit_digest_staleness_check.py (the report-only
freshness signal). finding.v1 + the frontmatter reader are reused from validate.py via the sibling-import
precedent.
"""
from __future__ import annotations
import datetime
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402


# The committed digest's home: a file under .engine/audits/ (already a registered infra dir, beside the
# concern-list), audits-system-owned data, not a catalogued surface. It does NOT exist until the scheduled
# run first writes one — both rules below treat its absence as a first-class, honest state.
AUDIT_DIGEST_PATH = os.path.join(validate.ENGINE_DIR, "audits", "audit-digest.md")

# How long the engine may go without a self-review before the freshness signal warns. A plain bound, not a
# tuned parameter; re-tunable by editing this one line. Pinned by a unit test so it cannot drift silently.
STALENESS_DAYS = 30

# The action the freshness signal names when the self-review has stopped — the design's pinned re-arm verb.
REARM_HINT = "enable it with `gh workflow enable …` or push a commit to re-arm it"


# ---- small shared helpers --------------------------------------------------------------------

def _display(path: str) -> str:
    """A path for human messages: repo-relative inside the repo, else absolute — never a `../..` chain
    (matters for the demo's throwaway copy outside the repo). Mirrors self_map._display."""
    rel = os.path.relpath(path, validate.ROOT)
    return rel.replace(os.sep, "/") if not rel.startswith("..") else os.path.abspath(path)


def _loc_opt(path: str):
    """A finding.v1 location (repo-relative) — or None when the path is outside the repo. Mirrors
    self_map._loc_opt / wiring._loc_opt."""
    rel = os.path.relpath(path, validate.ROOT)
    return None if rel.startswith("..") else {"file": rel.replace(os.sep, "/"), "line": None}


def _iso(value) -> str:
    """The run-date as a stable ISO `YYYY-MM-DD` string, whether it arrives as a date (the seal verb's
    default) or an already-parsed string (frontmatter, normalized by validate._json_model). Both sides of
    the seal read the date this way, so the seal is independent of how the header was serialized."""
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()[:10]
    return str(value)[:10]


# ---- the seal (pure; fixture-testable) -------------------------------------------------------

def compute_seal(generated: str, body: str) -> str:
    """The self-seal: sha256 over the run-date + the raw body. Never includes the frontmatter's
    serialized text or the fingerprint field — only a change to the date or the body moves it."""
    digest = hashlib.sha256((generated + "\n" + body).encode("utf-8")).hexdigest()
    return "sha256:" + digest


def split(path: str):
    """Return (frontmatter_dict, body_text) for a digest file. The dict is read through the engine's own
    frontmatter reader (validate.frontmatter — raises loudly on malformed YAML, {} on no front-matter).
    The body is the exact bytes after the closing `---` fence, read with newline='' so a CRLF/LF change
    is not masked. A file with no front-matter has the whole text as its body."""
    with open(path, encoding="utf-8", newline="") as fh:
        raw = fh.read()
    fm = validate.frontmatter(path)
    parts = raw.split("---", 2)  # maxsplit=2: a `---` rule inside the body stays in the body
    body = parts[2] if (raw.startswith("---") and len(parts) == 3) else raw
    return fm, body


# ---- the two gates as pure functions (no IO beyond the read) ---------------------------------

def check(path: str | None = None) -> dict:
    """The fingerprint gate. `note` when no digest exists yet (nothing to verify) or the seal is intact;
    `hard` when the file is present but malformed, or its contents no longer match the recorded seal (a
    silent hand-edit). `path` defaults to the live digest, resolved at call time so a test may redirect."""
    path = AUDIT_DIGEST_PATH if path is None else path
    name, where = _display(path), _loc_opt(path)
    if not os.path.isfile(path):
        return validate.finding("note", f"No self-review file exists yet ({name}); nothing to verify.", where)
    try:
        fm, body = split(path)
    except Exception:
        return validate.finding(
            "hard",
            f"The engine's self-review file ({name}) is present but unreadable (its header is malformed). "
            f"Re-run the audit so it rewrites the file.",
            where)
    generated, fingerprint = fm.get("generated"), fm.get("fingerprint")
    if not generated or not fingerprint:
        return validate.finding(
            "hard",
            f"The engine's self-review file ({name}) is missing its run-date or its check-value, so it "
            f"cannot be verified. Re-run the audit so it rewrites the file.",
            where)
    if compute_seal(_iso(generated), body) != fingerprint:
        return validate.finding(
            "hard",
            f"The engine's self-review file ({name}) has been changed since the audit wrote it — its "
            f"contents no longer match the value the audit recorded. Re-run the audit to refresh the "
            f"file, or revert the hand-edit.",
            where)
    return validate.finding(
        "note", f"The engine's self-review file ({name}) matches the record the audit wrote.", where)


def staleness(path: str | None = None, now: datetime.date | None = None) -> dict:
    """The freshness signal. `note` when the digest is current; a `soft` (advisory, never blocking)
    finding when the engine has not self-reviewed in more than STALENESS_DAYS days, OR when no self-review
    has run yet — so a self-review that quietly stopped (an expired token, a disabled schedule, a setup
    never finished) is surfaced on the operator's return rather than missed. `now` defaults to today
    inside the function (the thin script calls it arg-less; tests inject a fixed date)."""
    path = AUDIT_DIGEST_PATH if path is None else path
    now = datetime.date.today() if now is None else now
    name, where = _display(path), _loc_opt(path)
    if not os.path.isfile(path):
        return validate.finding(
            "soft",
            f"The engine's self-review hasn't run yet — set up the scheduled self-review so the engine "
            f"can start checking its own health. Once it runs, this notice clears.",
            where)
    try:
        fm, _body = split(path)
        run_date = datetime.date.fromisoformat(_iso(fm.get("generated")))
    except Exception:
        return validate.finding(
            "soft",
            f"The engine's self-review file ({name}) is present but its run-date can't be read — re-run "
            f"the audit so it rewrites the file.",
            where)
    age = (now - run_date).days
    if age > STALENESS_DAYS:
        return validate.finding(
            "soft",
            f"The engine hasn't reviewed its own health in {age} days. Re-arm the scheduled self-review — "
            f"{REARM_HINT} — and it will refresh on the next run.",
            where)
    return validate.finding(
        "note",
        f"The engine's self-review is current (last run {run_date.isoformat()}, within the last "
        f"{STALENESS_DAYS} days).",
        where)


# ---- the seal writer (IO) --------------------------------------------------------------------

def _render(generated: str, fingerprint: str, body: str) -> str:
    """The committed digest text: a fixed-order header (run-date + check-value the gate recomputes) then
    the body verbatim. Built as a plain string, not via a YAML dumper, so the serialization is stable and
    the body the seal covers round-trips exactly through split()."""
    return (f"---\nschema_version: 1\ngenerated: {generated}\nfingerprint: {fingerprint}\n---{body}")


def seal(path: str, generated=None, body: str | None = None) -> dict:
    """Stamp the run-date and write the self-seal over the file's body. With `body` given (the scheduled
    run's path: the persona's prose), the body is normalized to one blank line after the header; with
    `body=None` (re-sealing an existing file), the current body is preserved exactly. The write's exact
    bytes are what check() later verifies. `generated` defaults to today; a test injects a fixed date."""
    generated = _iso(generated if generated is not None else datetime.date.today())
    if body is None:
        _fm, body = split(path)              # re-seal: reuse the existing body slice verbatim
    else:
        body = "\n\n" + body.lstrip("\n")    # fresh: one blank line between the header and the prose
    fingerprint = compute_seal(generated, body)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(_render(generated, fingerprint, body))
    return validate.finding(
        "note", f"Sealed the self-review file ({_display(path)}), dated {generated}.", _loc_opt(path))


# ---- CLI ------------------------------------------------------------------------------------

def _take_body_file(argv: list):
    """Pull an optional `--body-file PATH` pair out of argv (wherever it sits) and return
    (argv_without_it, body_text|None). The scheduled run passes the captured persona prose this way; the
    pair is removed BEFORE the positional file/date are read, so `--body-file` can never be mis-parsed as
    the file path (argv[1]) or the run-date (argv[2])."""
    out, body, i = [], None, 0
    while i < len(argv):
        if argv[i] == "--body-file":
            if i + 1 >= len(argv):
                raise ValueError("--body-file needs a file path")
            with open(argv[i + 1], encoding="utf-8", newline="") as fh:
                body = fh.read()
            i += 2
            continue
        out.append(argv[i])
        i += 1
    return out, body


def main(argv: list) -> int:
    cmd = argv[0] if argv else "check"
    try:
        if cmd == "seal":
            rest, body = _take_body_file(argv)   # strip --body-file PATH before any positional read
            if len(rest) < 2:
                print("usage: audit_digest.py seal <file> [YYYY-MM-DD] [--body-file PATH]", file=sys.stderr)
                return 2
            gen = rest[2] if len(rest) > 2 else None
            print(validate.fmt(seal(rest[1], generated=gen, body=body)))
            return 0
        if cmd == "check":
            print(validate.fmt(check(argv[1] if len(argv) > 1 else None)))
            return 0
        if cmd == "staleness":
            print(validate.fmt(staleness(argv[1] if len(argv) > 1 else None)))
            return 0
    except Exception as exc:  # a tool error is loud, never a silent pass
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"unknown command '{cmd}' (expected: seal, check, staleness)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
