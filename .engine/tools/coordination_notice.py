#!/usr/bin/env python3
"""coordination_notice — assemble, fingerprint, render, and parse ONE advisory cross-session coordination
notice (StarshipSuperjam/engine-template#939, eADR-0043).

WHAT A NOTICE IS. A typed, closed-vocabulary signal one active worker session leaves for another on the
GitHub work item they share. It is advisory: a receiver acts on it ONLY by re-verifying canonical state, and
it can never carry authority (eADR-0043 law 3). This module owns the notice's *shape* — the closed kind/event
vocabulary, the by-construction render, the condition fingerprint, the machine-marked comment block, and the
skip-malformed parser. It makes no network call and reaches no GitHub surface (coordination_board owns the
one comment transport); the confinement check depends on that separation.

WHY NO FREE-PROSE SLOT (eADR-0043 law 2). A notice is ultimately read by a *model* (a peer session, and the
operator via boot's relay). So there is no author-supplied text field: every human-readable line is FIXED
copy generated here from the closed enums. The one attacker-influenceable surface is the repo-native
identifier fields (a branch name, a changed path) — a session can create a branch or file whose *name*
carries markup. Those are render-constrained through the single render-safety boundary
(`render_safety.safe_ident`) at assembly time, so by the time an identifier is stored it can no longer break
a code span or a fenced block. That narrows the surface; it does not close it (a receiver still re-verifies).

DETERMINISM. `render` takes injectable `now` and `id_source` seams (eADR-0032): production reads the wall
clock / a random id at the call boundary, tests inject fixed values so the rendered block, poke line, and
fingerprint are byte-deterministic.

INTEGRITY, NOT AUTHENTICITY. The block carries a sha256 over its own canonical JSON (tamper/corruption
detection + a stable id) and a condition fingerprint (dedupe). Neither is a signature: any collaborator with
repository write can post a well-formed notice with a correct digest. That is acceptable because a notice
carries no authority and the receiver re-verifies canonical state (eADR-0043 residual 3); the parser SKIPS a
block whose digest or schema does not hold, so a corrupted or malformed block is inert, never a crash.

CLI:
  uv run --directory .engine -- python tools/coordination_notice.py demo
  uv run --directory .engine -- python tools/coordination_notice.py vocabulary
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import render_safety  # noqa: E402  (the one render-safety boundary for identifier fields)

SCHEMA_VERSION = "coordination-notice.v1"
_SCHEMA_REL = os.path.join(os.path.dirname(__file__), "..", "schemas", "coordination-notice.v1.json")

# ---- the closed vocabulary (the single source; the schema's enums mirror this, drift-test pinned) ---------

# Each kind's permitted events. Additive-only: a new kind or event is one deliberate edit here AND in the
# schema, and the drift test refuses a divergence. A reader that meets an unknown kind/event skips the notice.
EVENTS_BY_KIND = {
    "integration-notice": ("admitted", "next-in-queue", "blocked", "released", "merged-ahead"),
    "revalidation-notice": ("base-advanced", "plan-changed", "spec-changed", "checks-invalidated"),
    "overlap-warning": ("domains-intersect", "domains-cleared"),
    "dependency-update": ("merged", "closed", "reopened", "head-moved", "plan-revised"),
    "handoff": ("ready-for-review", "slot-released", "node-abandoned", "work-abandoned"),
    "bounded-status": ("work-declared", "work-completed"),
}
KINDS = tuple(EVENTS_BY_KIND.keys())

VERIFY_ACTIONS = (
    "recheck-queue", "recheck-base", "recheck-plan", "recheck-overlap", "recheck-pr-state", "none")

# The action -> required `observed` evidence pins. Enforced in render (the schema cannot express the coupling
# under additionalProperties:false); the drift test pins that every action appears here so a new action cannot
# be added without deciding its required evidence.
_ACTION_REQUIRES = {
    "recheck-queue": (),
    "recheck-base": ("base_sha",),
    "recheck-plan": ("plan_digest",),
    "recheck-overlap": (),
    "recheck-pr-state": ("head_sha",),
    "none": (),
}

# ---- fixed operator-facing copy (enums + counts only; NEVER a branch/path string) -------------------------

_OPERATOR_LINE = {
    ("integration-notice", "admitted"): "An integration slot opened for this pull request.",
    ("integration-notice", "next-in-queue"): "This pull request is next in the integration queue.",
    ("integration-notice", "blocked"): "The integration slot was released — this pull request is blocked.",
    ("integration-notice", "released"): "The integration slot was released.",
    ("integration-notice", "merged-ahead"): "Work merged ahead of this pull request in the integration queue.",
    ("revalidation-notice", "base-advanced"): "The main branch advanced under this pull request.",
    ("revalidation-notice", "plan-changed"): "The build plan changed.",
    ("revalidation-notice", "spec-changed"): "The settled specification changed.",
    ("revalidation-notice", "checks-invalidated"):
        "The checks on this pull request may no longer reflect the current base.",
    ("overlap-warning", "domains-intersect"):
        "Another active pull request touches an overlapping set of files.",
    ("overlap-warning", "domains-cleared"): "A previously overlapping pull request no longer overlaps.",
    ("dependency-update", "merged"): "A pull request this work depends on was merged.",
    ("dependency-update", "closed"): "A pull request this work depends on was closed.",
    ("dependency-update", "reopened"): "A pull request this work depends on was reopened.",
    ("dependency-update", "head-moved"): "The head of a pull request this work depends on moved.",
    ("dependency-update", "plan-revised"): "The build plan of related work was revised.",
    ("handoff", "ready-for-review"): "A prerequisite pull request is ready for review.",
    ("handoff", "slot-released"): "An integration slot was released.",
    ("handoff", "node-abandoned"): "A work node was abandoned.",
    ("handoff", "work-abandoned"): "A unit of work was abandoned.",
    ("bounded-status", "work-declared"): "A peer session declared the work it is starting.",
    ("bounded-status", "work-completed"): "A peer session reported work complete.",
}

_VERIFY_PHRASE = {
    "recheck-queue": "Re-check the integration queue before acting.",
    "recheck-base": "Re-check this branch against the current main before acting.",
    "recheck-plan": "Re-read the durable build plan before acting.",
    "recheck-overlap": "Re-compute the file overlap before acting.",
    "recheck-pr-state": "Re-check the pull request's current state before acting.",
    "none": "No action is required.",
}

# ---- machine markers (marker-safe: only hex/enum values ever enter them) ----------------------------------

_MARKER_KIND = "engine-coordination-notice:v1"
_BLOCK_OPEN = "<!-- " + _MARKER_KIND + " id:{id} fp:{fp} sha256:{digest} -->"
_BLOCK_CLOSE = "<!-- /" + _MARKER_KIND + " -->"
_BLOCK_RE = re.compile(
    r"<!--\s*engine-coordination-notice:v1\s+id:([0-9a-f]{32})\s+fp:([0-9a-f]{64})\s+"
    r"sha256:([0-9a-f]{64})\s*-->\s*```json\s*(.*?)\s*```\s*<!--\s*/engine-coordination-notice:v1\s*-->",
    re.DOTALL)


class NoticeError(ValueError):
    """A notice could not be assembled to the contract (an unknown kind/event/action, a missing required
    reference, or a missing action-required evidence pin). Raised at the by-construction render boundary."""


def _canonical(obj) -> str:
    """The one canonical JSON form used for both the integrity digest and the condition fingerprint —
    sorted keys, no insignificant whitespace, so the same content always digests identically."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _safe_work_ref(ref, *, field: str) -> dict:
    """A work_ref with its branch render-constrained and its numbers validated. At least one of pr/issue/branch
    must be present (a reference to nothing is a bug, not a benign empty)."""
    if not isinstance(ref, dict):
        raise NoticeError(f"{field} must be an object with at least one of pr/issue/branch")
    out: dict = {}
    for key in ("pr", "issue"):
        if ref.get(key) is not None:
            out[key] = int(ref[key])
    if ref.get("branch") is not None:
        out["branch"] = render_safety.safe_ident(str(ref["branch"]), replacement="_")
    if not out:
        raise NoticeError(f"{field} must name at least one of pr/issue/branch")
    return out


def _safe_subject(subject) -> dict:
    if not isinstance(subject, dict):
        raise NoticeError("subject must be an object")
    out: dict = {}
    for key in ("pr", "issue"):
        if subject.get(key) is not None:
            out[key] = int(subject[key])
    if subject.get("branch") is not None:
        out["branch"] = render_safety.safe_ident(str(subject["branch"]), replacement="_")
    if subject.get("paths") is not None:
        paths = list(subject["paths"])[:20]  # cap enforced here and by the schema
        out["paths"] = [render_safety.safe_ident(str(p), replacement="_") for p in paths if str(p)]
    if not out:
        raise NoticeError("subject must name at least one of pr/issue/branch/paths")
    return out


def render(*, kind: str, event: str, emitter_work_ref: dict, audience: dict, subject: dict,
           verify_action: str, observed: "dict | None" = None,
           now: "str | None" = None, id_source=None) -> dict:
    """Assemble one coordination notice by construction. Keyword-only; an omitted required argument raises
    TypeError at the call boundary. Validates the closed vocabularies, render-constrains every identifier,
    enforces the action's required evidence pins, and stamps a random id + a UTC timestamp through the
    injectable seams. Returns the notice dict (also schema-valid — validated as a belt below)."""
    if kind not in EVENTS_BY_KIND:
        raise NoticeError(f"unknown kind {kind!r}; one of {', '.join(KINDS)}")
    if event not in EVENTS_BY_KIND[kind]:
        raise NoticeError(f"event {event!r} does not belong to kind {kind!r}")
    if verify_action not in VERIFY_ACTIONS:
        raise NoticeError(f"unknown verify action {verify_action!r}")
    observed = dict(observed or {})
    for required in _ACTION_REQUIRES[verify_action]:
        if not observed.get(required):
            raise NoticeError(f"action {verify_action!r} requires observed.{required}")

    if not isinstance(audience, dict) or not any(audience.get(k) for k in ("pr", "issue")):
        raise NoticeError("audience must name at least one of pr/issue")
    audience_out = {k: int(audience[k]) for k in ("pr", "issue") if audience.get(k) is not None}

    verify_out: dict = {"action": verify_action}
    if observed:
        verify_out["observed"] = observed

    notice_id = (id_source() if id_source is not None else secrets.token_hex(16))
    if now is None:
        import moment  # lazy: wall-clock read lives at the call boundary (eADR-0032)
        now = moment.utc_now()

    notice = {
        "schema_version": SCHEMA_VERSION,
        "notice_id": notice_id,
        "kind": kind,
        "event": event,
        "emitter": {"work_ref": _safe_work_ref(emitter_work_ref, field="emitter.work_ref")},
        "audience": audience_out,
        "subject": _safe_subject(subject),
        "verify": verify_out,
        "emitted_at": now,
    }
    validate_notice(notice)  # belt: by-construction should already satisfy the schema
    return notice


def fingerprint(notice: dict) -> str:
    """The condition fingerprint — sha256 over the STRUCTURED condition (kind, event, emitter work_ref,
    subject, observed evidence), never over the rendered prose and never over notice_id/emitted_at. Two
    notices that mean the same thing fingerprint identically, so dedupe collapses them (eADR-0043 law 4)."""
    cond = {
        "kind": notice["kind"],
        "event": notice["event"],
        "work_ref": notice["emitter"]["work_ref"],
        "subject": notice["subject"],
        "observed": notice.get("verify", {}).get("observed", {}),
    }
    return hashlib.sha256(_canonical(cond).encode("utf-8")).hexdigest()


def validate_notice(notice: dict) -> dict:
    """Validate `notice` against coordination-notice.v1 and return it unchanged; raises NoticeError naming the
    first violation. jsonschema and the schema file are loaded lazily so importing this module stays light."""
    from jsonschema import Draft202012Validator  # lazy: tool-runtime dep
    with open(_SCHEMA_REL, "r", encoding="utf-8") as fh:
        schema = json.load(fh)
    errors = sorted(Draft202012Validator(schema).iter_errors(notice), key=lambda e: list(e.path))
    if errors:
        first = errors[0]
        where = "/".join(str(p) for p in first.path) or "(root)"
        raise NoticeError(f"notice does not match {SCHEMA_VERSION} at {where}: {first.message}")
    return notice


def render_operator_line(notice: dict) -> str:
    """The one plain-language line a person reads — built ONLY from the closed kind/event copy, the verify
    phrase, and an integer count. It never interpolates a branch or path string (those live in the fenced
    JSON below, render-constrained); so this line cannot carry a crafted identifier into the operator's view."""
    base = _OPERATOR_LINE[(notice["kind"], notice["event"])]
    line = f"{base} {_VERIFY_PHRASE[notice['verify']['action']]}"
    paths = notice.get("subject", {}).get("paths")
    if paths:
        line += f" ({len(paths)} file(s) named in the notice below.)"
    return line


def render_poke_line(notice: dict, repo: str) -> str:
    """The fixed one-line live poke — a pointer, never the payload. Carries only repo-native values (repo
    slug, audience number, kind enum, hex id). Pinned by unit test so live text stays a pointer to the board."""
    number = notice["audience"].get("pr") or notice["audience"].get("issue")
    ref = "PR" if notice["audience"].get("pr") else "issue"
    return (f"engine-coordination: {notice['kind']} notice on {repo} ({ref} #{number}, {notice['notice_id']})"
            f" — read your coordination notices and re-verify canonical state before acting.")


def render_block(notice: dict) -> str:
    """The machine-marked block stored in the coordination comment: the fixed operator line, then the notice
    as canonical fenced JSON, wrapped in an open marker carrying the notice id, condition fingerprint, and a
    sha256 over the JSON. Because every identifier was render-constrained at assembly, the JSON contains no
    backtick, so it cannot break the fence."""
    doc = _canonical(notice)
    digest = hashlib.sha256(doc.encode("utf-8")).hexdigest()
    fp = fingerprint(notice)
    open_marker = _BLOCK_OPEN.format(id=notice["notice_id"], fp=fp, digest=digest)
    return f"{render_operator_line(notice)}\n\n{open_marker}\n```json\n{doc}\n```\n{_BLOCK_CLOSE}"


def parse_blocks(body: str) -> list:
    """Recover every well-formed, digest-true, schema-valid notice from a comment body, newest-last in
    document order. A block whose recomputed sha256 does not match its marker, whose JSON does not parse,
    whose fingerprint does not match, or which fails the schema is SKIPPED (never raised) — a corrupted or
    forged-malformed block is inert, so a hostile edit degrades to a dropped notice, not a crash. Returns a
    list of notice dicts."""
    out = []
    for m in _BLOCK_RE.finditer(body or ""):
        marker_id, marker_fp, marker_digest, doc = m.group(1), m.group(2), m.group(3), m.group(4).strip()
        if hashlib.sha256(doc.encode("utf-8")).hexdigest() != marker_digest:
            continue  # integrity: the JSON was altered after the digest was stamped
        try:
            notice = json.loads(doc)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(notice, dict) or notice.get("notice_id") != marker_id:
            continue
        try:
            validate_notice(notice)
        except NoticeError:
            continue
        if fingerprint(notice) != marker_fp:
            continue
        out.append(notice)
    return out


# ---- CLI + self-checking demo -----------------------------------------------------------------------------

def _demo() -> int:
    print("COORDINATION-NOTICE DEMO — assemble, fingerprint, render, parse; and the refusals.\n")
    seq = iter(["a" * 32])
    notice = render(
        kind="integration-notice", event="admitted",
        emitter_work_ref={"pr": 12, "branch": "claude/939-x"},
        audience={"pr": 12}, subject={"pr": 12, "branch": "claude/939-x"},
        verify_action="recheck-queue",
        now="2026-08-18T00:00:00Z", id_source=lambda: next(seq))
    block = render_block(notice)
    print(block)
    print("\n--- the block round-trips through the parser ---")
    parsed = parse_blocks(block)
    round_trips = len(parsed) == 1 and parsed[0]["notice_id"] == notice["notice_id"]
    print(f"Round-trips: {round_trips}")

    print("\n--- a tampered JSON body is skipped (integrity) ---")
    tampered = block.replace('"admitted"', '"blocked"')
    skipped_tamper = parse_blocks(tampered) == []
    print(f"Tampered block skipped: {skipped_tamper}")

    print("\n--- a crafted branch name cannot break the fenced block (render-safety) ---")
    crafted = render(
        kind="integration-notice", event="admitted", emitter_work_ref={"pr": 1},
        audience={"pr": 1}, subject={"pr": 1, "branch": "```evil`)[x](http://y)"},
        verify_action="none", now="2026-08-18T00:00:00Z", id_source=lambda: "b" * 32)
    crafted_safe = "```" not in crafted["subject"]["branch"] and len(parse_blocks(render_block(crafted))) == 1
    print(f"Crafted branch neutralised and still parses: {crafted_safe}")

    refused = 0
    print("\n--- an event outside its kind is refused ---")
    try:
        render(kind="integration-notice", event="work-declared", emitter_work_ref={"pr": 1},
               audience={"pr": 1}, subject={"pr": 1}, verify_action="none")
    except NoticeError as exc:
        refused += 1
        print(f"Refused — {exc}")
    print("\n--- an action missing its required evidence pin is refused ---")
    try:
        render(kind="revalidation-notice", event="base-advanced", emitter_work_ref={"pr": 1},
               audience={"pr": 1}, subject={"pr": 1}, verify_action="recheck-base")
    except NoticeError as exc:
        refused += 1
        print(f"Refused — {exc}")
    print("\n--- a reference to nothing is refused ---")
    try:
        render(kind="handoff", event="slot-released", emitter_work_ref={},
               audience={"pr": 1}, subject={"pr": 1}, verify_action="none")
    except NoticeError as exc:
        refused += 1
        print(f"Refused — {exc}")

    ok = round_trips and skipped_tamper and crafted_safe and refused == 3
    if not ok:
        print(f"\nDEMO UNEXPECTED: round_trips={round_trips} tamper={skipped_tamper} "
              f"crafted={crafted_safe} refused={refused}/3", file=sys.stderr)
        return 1
    return 0


def _vocabulary() -> int:
    print(f"{SCHEMA_VERSION} — closed coordination vocabulary\n")
    for kind in KINDS:
        print(f"  {kind}: {', '.join(EVENTS_BY_KIND[kind])}")
    print(f"\n  verify actions: {', '.join(VERIFY_ACTIONS)}")
    return 0


def main(argv: list) -> int:
    verb = argv[0] if argv else None
    if verb == "demo":
        return _demo()
    if verb == "vocabulary":
        return _vocabulary()
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
