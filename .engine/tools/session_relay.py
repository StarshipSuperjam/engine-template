"""session_relay: validate and deterministically render the session-relay.v1 envelope.

WHAT THIS IS. Boot's session-start `additionalContext` used to be free-form prose assembled ad hoc.
It is being replaced by a typed, schema-validated envelope (`session-relay.v1` /
`.engine/schemas/session-relay.v1.json`) rendered into a compact, DETERMINISTIC prose block: same
envelope in, byte-identical string out. This module is the foundational node — it defines
`validate()` and `render()` only. It does NOT wire itself into boot.py, does NOT read/write a
session-binding.v1 evidence file, and does NOT populate `authority_contract` from modes.py; those
are later nodes' work. See `.engine/schemas/session-relay.v1.json` for the seven-section push-warrant
taxonomy this module renders, and `.engine/schemas/session-binding.v1.json` for the verified-binding
evidence shape `task_binding.binding` mirrors.

THE 2,000-CHAR CONSTRAINT. The platform truncates injected session-start context to a 2,000-character
preview. A prior measurement (the size-spike node) found that today's PROSE alarm rendering overflows
that budget once several alarms fire together. This module's fix is a COMPACT alarm encoding — a short
stable `code` plus minimal structured `data`, never a sentence — plus a FIXED SECTION ORDER that puts
`grounding_receipt` first and `action_forcing_alarms` immediately after it, so the two sections a
truncated preview must never lose always land inside the first 2,000 characters. See
`test_render_worst_case_receipt_and_alarms_fit_2000_chars` for the measured worst case.

INJECTION SAFETY. Every machine-derived / untrusted string reaching this envelope (worktree paths,
branch names, pull-request titles, pin text, slugs, memory excerpts) is confined to a DATA field and
rendered as INERT text. `_inert()` is the one chokepoint: it strips every control character —
including `\\n` and `\\r` — from a data value before it is interpolated. Because this module's own
section headers, list bullets, and pointer handles are ONLY ever emitted by the fixed template at the
start of a physical line immediately after a literal `\\n` this module itself writes, and a sanitized
data value can never contain a `\\n`, a data value can never cause a new physical line to begin — so it
can never forge a new header, bullet, or handle. `_inert()` also defangs the literal marker substrings
the template uses (`## `, `\\n- `, and Markdown code fences) as defense in depth, even though the
newline-stripping guarantee alone already makes them inert mid-line text.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SCHEMA_PATH = os.path.join(ROOT, ".engine", "schemas", "session-relay.v1.json")
BINDING_SCHEMA_PATH = os.path.join(ROOT, ".engine", "schemas", "session-binding.v1.json")


class RelayValidationError(Exception):
    """The envelope does not satisfy session-relay.v1. Carries every problem, not just the first."""


def _load_schema(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _schema() -> dict:
    return _load_schema(SCHEMA_PATH)


def _binding_schema() -> dict:
    return _load_schema(BINDING_SCHEMA_PATH)


def validate(envelope: dict) -> None:
    """Validate `envelope` against session-relay.v1. Raises RelayValidationError naming every problem
    (path + message), sorted for stable output, mirroring build_coordinator_contract.validate_claim."""
    from jsonschema import Draft202012Validator

    schema = _schema()
    errors = sorted(Draft202012Validator(schema).iter_errors(envelope), key=lambda e: list(e.path))
    if errors:
        lines = []
        for e in errors:
            where = "/".join(str(p) for p in e.path) or "(root)"
            lines.append(f"  - {where}: {e.message}")
        raise RelayValidationError(
            f"the envelope does not satisfy session-relay.v1 ({len(errors)} problem(s)):\n"
            + "\n".join(lines)
        )


def validate_binding(binding: dict) -> None:
    """Validate a session-binding.v1 evidence record on its own (independent of any envelope)."""
    from jsonschema import Draft202012Validator

    schema = _binding_schema()
    errors = sorted(Draft202012Validator(schema).iter_errors(binding), key=lambda e: list(e.path))
    if errors:
        lines = []
        for e in errors:
            where = "/".join(str(p) for p in e.path) or "(root)"
            lines.append(f"  - {where}: {e.message}")
        raise RelayValidationError(
            f"the binding does not satisfy session-binding.v1 ({len(errors)} problem(s)):\n"
            + "\n".join(lines)
        )


# ---- injection safety: the one chokepoint every data value passes through -----------------------

# Markers the render template uses to delimit structure. Defanged as defense in depth: the
# newline-stripping below already makes these inert mid-line text (they can only signal structure at
# the start of a physical line, which a sanitized value can never begin because it can never contain
# the preceding "\n"), but we neutralize the literal substrings too rather than rely on that alone.
_FENCE_MARKERS = ("```", "## ", "- ", "-> ")
_ZWSP = "​"


def _inert(value) -> str:
    """Render any data value as flat, single-line, structure-free text. This is the ONLY function that
    may place an untrusted/machine-derived value into rendered output. It:
      1. Coerces to text (None -> "", non-str -> str()).
      2. Strips every Unicode control/format character (category Cc/Cf), which removes \\n, \\r, \\t,
         and lookalikes such as zero-width joiners smuggled in to defeat a naive newline check.
      3. Collapses runs of whitespace to a single space and trims the ends.
      4. Defangs the template's own fence/marker substrings so they cannot even masquerade as
         structure mid-line.
    """
    text = "" if value is None else str(value)
    # Replace (never merely delete) control/format characters, so "a\nb" becomes "a b" rather than
    # "ab" — deletion could accidentally weld two distinct tokens together.
    text = "".join(" " if unicodedata.category(ch) in ("Cc", "Cf") else ch for ch in text)
    text = re.sub(r"\s+", " ", text).strip()
    for marker in _FENCE_MARKERS:
        if marker in text:
            broken = _ZWSP.join(marker)
            text = text.replace(marker, broken)
    return text


# ---- fixed section order -------------------------------------------------------------------------
# grounding_receipt FIRST, action_forcing_alarms SECOND — together they must fit the first 2,000
# characters of the render. Everything else follows in this same fixed order every time.
_SECTION_ORDER = (
    "grounding_receipt",
    "action_forcing_alarms",
    "identity",
    "authority_contract",
    "task_binding",
    "standing_directives",
    "pointers",
)


def _render_grounding_receipt(section: dict) -> str:
    helpers = section["helpers"]
    return (
        "## GROUNDING\n"
        f"markers={section['present_marker_count']} "
        f"memory={helpers['memory']['state']} "
        f"knowledge_graph={helpers['knowledge_graph']['state']}"
    )


def _render_alarm(alarm: dict) -> str:
    # The must-relay line itself — boot's own governance-relay text (terse or full per the
    # anti-habituation ledger), carried verbatim in `text`. Inert-guarded like every other data value
    # so an interpolated worktree/branch/PR name inside it can never forge relay structure. The `code`
    # is envelope structure (audit + collapse identity), surfaced in the header line below, not here.
    return f"- {_inert(alarm['text'])}"


def _render_alarms(alarms: list) -> str:
    # The header lists EVERY alarm's code up front, so the grounding_receipt + this one compact line
    # (which lead the render) tell a truncated 2,000-char preview WHICH alarms fired even when the full
    # relay texts below are cut. The texts follow in order; the whole block renders inside the cap in
    # the ordinary (uncapped) case.
    codes = ", ".join(_inert(a["code"]) for a in alarms)
    lines = [f"## ALARMS ({len(alarms)}): {codes}" if alarms else "## ALARMS (0)"]
    for alarm in alarms:
        lines.append(_render_alarm(alarm))
    return "\n".join(lines)


def _render_identity(section: dict) -> str:
    label = _inert(section.get("label", "-")) or "-"
    return f"## IDENTITY\ndeployment={section['deployment']} label={label}"


def _render_authority_contract(section: dict) -> str:
    blocked = ",".join(sorted(section["blocked"])) or "none"
    exceptions = section["provider_exceptions"]
    exc_text = "none" if not exceptions else ";".join(
        f"{_inert(e['provider'])}:{_inert(e['note'])}" for e in exceptions
    )
    return (
        "## AUTHORITY\n"
        f"stance={section['stance']} default={section['action_default']} "
        f"blocked=[{blocked}] exceptions={exc_text}"
    )


def _render_task_binding(section: dict) -> str:
    if section["state"] == "none":
        return "## TASK_BINDING\nstate=none"
    binding = section["binding"]
    snapshot_rev = _inert(binding["coordinator_snapshot"]["revision"])
    pr_state = binding["pr_contract"]["state"]
    pr_ref = _inert(binding["pr_contract"].get("pr_ref", "-")) or "-"
    return (
        "## TASK_BINDING\n"
        f"state=verified worktree={_inert(binding['worktree'])} "
        f"plan_ref={_inert(binding['plan_ref'])} snapshot_rev={snapshot_rev} "
        f"pr_state={pr_state} pr_ref={pr_ref}"
    )


def _render_standing_directives(section: dict) -> str:
    pins = section["pins_index"]
    pins_summary = _inert(pins.get("summary", "")) if pins.get("summary") else ""
    pins_line = f"pins={pins['count']}" + (f" ({pins_summary})" if pins_summary else "")
    lines = [
        "## STANDING_DIRECTIVES",
        f"{pins_line} posture={section['execution_posture']}",
    ]
    for routing_line in section["routing_lines"]:
        lines.append(f"- {routing_line}")
    where = section["where_we_left_off"]
    lines.append(f"{where['label']}: {_inert(where['pointer'])}")
    return "\n".join(lines)


def _render_pointers(section: list) -> str:
    lines = [f"## POINTERS ({len(section)})"]
    for p in section:
        ref = _inert(p.get("ref", "")) if p.get("ref") else ""
        lines.append(f"-> {p['kind']}" + (f": {ref}" if ref else ""))
    return "\n".join(lines)


_RENDERERS = {
    "grounding_receipt": _render_grounding_receipt,
    "action_forcing_alarms": _render_alarms,
    "identity": _render_identity,
    "authority_contract": _render_authority_contract,
    "task_binding": _render_task_binding,
    "standing_directives": _render_standing_directives,
    "pointers": _render_pointers,
}


def render(envelope: dict) -> str:
    """Render a validated envelope into the deterministic prose block. Does NOT validate — call
    `validate(envelope)` first (or `render(envelope, validate_first=True)` below is intentionally not
    offered: callers choose when to pay for validation, e.g. once at boot, not on every render).
    Same envelope in -> byte-identical string out: no dict-ordering nondeterminism (every collection
    this module iterates is either schema-ordered, e.g. arrays, or explicitly sorted, e.g. dict keys
    and blocked-action sets), and no timestamp-of-now is ever generated here."""
    sections = []
    for name in _SECTION_ORDER:
        sections.append(_RENDERERS[name](envelope[name]))
    return "\n".join(sections)
