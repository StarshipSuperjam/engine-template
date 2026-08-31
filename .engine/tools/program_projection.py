#!/usr/bin/env python3
"""program_projection — the read-only views of the program shelf, beside plan_program's records.

This is to plan_program what plan_projection is to plan_store: the module that RENDERS, and never
writes a record. It composes two surfaces from the program library's own derivations (child_view,
derived_status, obligation_report, superseded_children) — it invents no status of its own:

- `render_portfolio` — the qualitative picture of every OPEN program at once: what each is for in one
  bounded plain-language line, how far along it is as facts (what is in flight, what has settled on the
  chain, what it still owes, when it last moved), and a bounded tail of what recently closed. Never a
  percentage, never a recommendation, and unknown is never rendered as done. A record that will not
  read is shown as needing attention rather than blanking the shelf.
- `render_program_md` / `project_program` / `project_all` (added by the PROGRAM.md work) — the
  per-program generated file and the library-wide sweep.

Every function here is a pure read of the record: it returns text, and writing is the caller's job.
"""
from __future__ import annotations

import re

import build_coordinator_core as core
import moment
import plan_program
import plan_store

PROGRAM_MD = "PROGRAM.md"
# The trust signal is VISIBLE body text, not an HTML comment — a comment is stripped by every common
# markdown renderer, so an operator browsing PROGRAM.md at rest the normal way would see no generation
# moment at all. This mirrors the sibling PLAN.md, which states its own metadata in plain visible text.
# The generated-at moment and the staleness window are disclosed here rather than promised away: the
# file is a projection of live child-plan data, so a child plan revised, sealed, retired or completed
# OUTSIDE any program verb moves what it should say with no program-record change to trigger a refresh.
_GENERATED_LINE_PREFIX = "> **Generated "


def _generated_blockquote(at: str) -> str:
    return (f"> **Generated {at}.** Regenerated from the program record and its children by every "
            "program verb that touches this program. It can go stale between those moments — a child "
            "plan changed outside a program verb is not reflected until the next program verb here, or "
            "a `program reproject` sweep. Edits here are overwritten.")

PORTFOLIO_NOTE = ("<!-- generated from the program records; a read-only view — nothing here selects, "
                  "starts, or advances work -->")

# The working states — a child still being built, not yet landed and not a dead branch. Everything a
# plan can derive to (plan_store.derived_status) that is neither `complete` nor a dead-branch closure
# nor an unreadable/missing row is in flight.
_LANDED = "complete"
_ATTENTION_STATES = ("missing", "unreadable")
_HEADLINE_CAP = 180


# The clause boundaries a truncated headline prefers to end on, so the visible text stops on a complete
# thought rather than an orphaned lead-in. Punctuation breaks first, then the coordinating phrases that
# introduce a subordinate clause — cutting BEFORE any of these keeps the clause and drops the rest.
_CLAUSE_MARKERS = (", ", "; ", " — ", " – ", ": ")
_CLAUSE_PHRASES = (" so that ", " rather than ", " because ", " while ", " and ", " but ", " so ", " to ")
# Don't cut so early that the headline says almost nothing: a clause boundary only wins if it lands past
# this fraction of the cap. Below it, a plain word-boundary cut carries more of the goal.
_CLAUSE_FLOOR_RATIO = 0.55


def _last_clause_boundary(window: str) -> int | None:
    floor = int(len(window) * _CLAUSE_FLOOR_RATIO)
    best = None
    for marker in _CLAUSE_MARKERS + _CLAUSE_PHRASES:
        idx = window.rfind(marker)
        if idx >= floor and (best is None or idx > best):
            best = idx
    return best


def goal_headline(objective: str, *, cap: int = _HEADLINE_CAP) -> str:
    """The objective's opening sentence, hard-capped at `cap` and cut on a complete thought.

    The live shelf holds objectives that are a single ~490-character sentence and 322-character opening
    sentences with an inline list, so "the first sentence" alone is not a bound — the cap is. A sentence
    at or under the cap is returned whole, its period kept. A longer one is cut at the last CLAUSE
    boundary within the cap (a comma, semicolon, dash, colon, or a coordinating phrase like "so that")
    so the visible text ends on a complete thought rather than an orphaned lead-in; only when there is
    no such boundary late enough does it fall back to a plain word break. Either way a single-character
    ellipsis marks the truncation.
    """
    text = " ".join(objective.split())          # collapse any internal whitespace, so the cap is on prose
    if not text:
        return "(no objective recorded)"
    match = re.search(r"\.(\s|$)", text)
    sentence = text[:match.start() + 1] if match else text
    if len(sentence) <= cap:
        return sentence
    window = sentence[:cap]
    clause = _last_clause_boundary(window)
    if clause is not None:
        cut = window[:clause]
    else:
        cut = window[:window.rfind(" ")] if " " in window else window
    return cut.rstrip().rstrip(".,;:—–") + "…"


def _strip_program_prefix(program_title: str, child_title: str) -> str:
    """Drop the run of leading WORDS a child title shares with its program's title.

    Child titles here redundantly re-prefix the program name — 'Program Manager — 4: its own address'
    under a program titled 'Program Manager'. Stripping the shared leading words declutters the
    in-flight line without inventing anything; leftover separators at the new start are trimmed too.
    """
    prog_words = program_title.split()
    child_words = child_title.split()
    shared = 0
    for a, b in zip(prog_words, child_words):
        if a != b:
            break
        shared += 1
    if not shared or shared == len(child_words):
        return child_title
    remainder = " ".join(child_words[shared:])
    return remainder.lstrip(" —-:–").strip() or child_title


def _obligation_phrase(obligation: dict) -> str:
    """An obligation as `ID — its short statement`, so the line says what is owed, not just a code.

    The whole point of the portfolio is qualitative, and a bare ID is the one place it fell back to a
    code the operator would have to run `program show` to decode. The statement is trimmed to a short
    length at a word boundary so a long one does not run the line away."""
    statement = " ".join((obligation.get("statement") or "").split())
    if not statement:
        return obligation["id"]
    if len(statement) > 60:
        statement = statement[:60].rsplit(" ", 1)[0].rstrip(".,;:") + "…"
    return f"{obligation['id']} — {statement}"


def _obligations_line(programs: plan_program.ProgramLibrary, record: dict) -> str:
    report = programs.obligation_report(record)
    known = report["obligations"]
    parts = []
    if known:
        # Name the first few with their statements; fold any remainder to a bare count so a program
        # carrying many obligations does not run the line off the page.
        shown = [_obligation_phrase(o) for o in known[:3]]
        if len(known) > 3:
            shown.append(f"(+{len(known) - 3} more)")
        parts.append("; ".join(shown))
    if report["unknown"]:
        # Unknown is never folded into a count of zero and never rendered as "none": a debt whose
        # position cannot be computed is still a debt the operator must see named as unknown.
        parts.append(f"unknown ({len(report['unknown'])} reason(s) — run `program show`)")
    return "; ".join(parts) if parts else "none outstanding"


def _settled_counts(programs: plan_program.ProgramLibrary, record: dict, view: list) -> str:
    superseded = plan_program.superseded_children(record)
    landed = supers = retired = abandoned = 0
    for child in view:
        pid = child["plan_id"]
        if pid in superseded:
            supers += 1
        elif child["status"] == _LANDED:
            landed += 1
        elif child["status"] == "retired":
            retired += 1
        elif child["status"] == "abandoned":
            abandoned += 1
    pieces = []
    for count, label in ((landed, "landed"), (supers, "superseded"),
                         (retired, "retired"), (abandoned, "abandoned")):
        if count:
            pieces.append(f"{count} {label}")
    return ", ".join(pieces)


def _in_flight_titles(programs: plan_program.ProgramLibrary, record: dict, view: list) -> list:
    superseded = plan_program.superseded_children(record)
    titles = []
    for child in view:
        if child["plan_id"] in superseded:
            continue
        if child["status"] in _ATTENTION_STATES or child["status"] == _LANDED \
                or child["status"] in plan_program.DEAD_BRANCH_STATES:
            continue
        titles.append(_strip_program_prefix(record["title"], child["title"]))
    return titles


def _lanes_line(record: dict) -> str | None:
    split = plan_program.lane_split(record)
    if not split:
        return None
    return "; ".join(f"{lane['name']} — {', '.join(lane['children'])}" for lane in split)


def _open_block(programs: plan_program.ProgramLibrary, record: dict) -> list:
    view = programs.child_view(record)
    status = programs.derived_status(record)
    recorded = "recorded" if programs.status_is_recorded(record) else "derived"
    out = [f"### {record['title']}",
           f"- **Goal**: {goal_headline(record['objective'])}",
           f"- **Program**: `{record['program_id']}` · status {status} ({recorded})",
           f"- **Last movement**: {plan_program.last_movement(record)}"]
    in_flight = _in_flight_titles(programs, record, view)
    if in_flight:
        # Semicolon-delimited, not comma: child titles are free text that regularly carry their own
        # internal commas, and a comma join makes three children read as five. The separator has to be
        # something the items do not themselves contain.
        out.append(f"- **In flight**: {'; '.join(in_flight)}")
    elif status == plan_program.ProgramLibrary.CHILDREN_COMPLETE:
        out.append("- **In flight**: nothing — every live child has landed, but no one has recorded "
                   "the PROGRAM complete; unwritten successors are unknown, not done")
    elif not view:
        out.append("- **In flight**: nothing yet — no child has been added")
    else:
        out.append("- **In flight**: nothing right now")
    settled = _settled_counts(programs, record, view)
    if settled:
        out.append(f"- **Settled on the chain**: {settled}")
    out.append(f"- **Obligations**: {_obligations_line(programs, record)}")
    lanes = _lanes_line(record)
    if lanes:
        out.append(f"- **Lanes**: {lanes}")
    return out


def _portfolio_entries(programs: plan_program.ProgramLibrary) -> tuple:
    """Read every program once, splitting the shelf into open, recently-closed, and unreadable — the
    unreadable kept as a fact rather than dropped, exactly as plan_projection keeps a damaged plan."""
    open_records, closed_records, unreadable = [], [], []
    for slug in programs.slugs():
        try:
            record = programs.read(slug)
        except Exception as exc:  # noqa: BLE001 — a record that will not read is a fact, not a crash
            unreadable.append((slug, str(exc)))
            continue
        if record.get("closure"):
            closed_records.append(record)
        else:
            open_records.append(record)
    return open_records, closed_records, unreadable


def render_portfolio(programs: plan_program.ProgramLibrary) -> str:
    """The qualitative portfolio of every open program, plus a bounded tail of the recently closed."""
    open_records, closed_records, unreadable = _portfolio_entries(programs)
    open_records.sort(key=lambda r: (r["title"], r["program_id"]))

    out = ["# Programs — portfolio", "", PORTFOLIO_NOTE, "",
           "A shelf, not a queue: every OPEN program below, what it is for, how far along it is, and "
           "what is in flight — as facts, never a percentage and never a recommendation. Unknown is "
           "unknown, not done.", "",
           f"## In flight ({len(open_records)})", ""]
    if open_records:
        blocks = [_open_block(programs, record) for record in open_records]
        out.append("\n\n".join("\n".join(block) for block in blocks))
    else:
        out.append("_No program is open._")

    if unreadable:
        out += ["", f"## Needs attention ({len(unreadable)})", "",
                "A program record that would not read. It is neither open nor closed here until it is "
                "repaired — shown so the damage is visible, not silently dropped.", ""]
        for slug, problem in unreadable:
            out.append(f"- **{slug}** — {problem}")

    if closed_records:
        closed_records.sort(key=lambda r: (r["closure"]["at"], r["program_id"]), reverse=True)
        shown = closed_records[:5]
        out += ["", f"## Recently closed ({len(shown)} of {len(closed_records)})", ""]
        for record in shown:
            closure = record["closure"]
            out.append(f"- `{record['program_id']}` {record['title']} — "
                       f"{closure['state']} {closure['at'][:10]}")
        remainder = len(closed_records) - len(shown)
        if remainder:
            out.append(f"- … and {remainder} more closed program(s) not shown")

    return "\n".join(out) + "\n"


# --- PROGRAM.md, the per-program generated file, and the library-wide sweep -----

def render_program_md(programs: plan_program.ProgramLibrary, record: dict, *, at: str | None = None) -> str:
    """The program as `program show` renders it, headed by the moment it reflects and its staleness
    window. A pure read: it renders text and writes nothing. `at` pins the generated-at for a test."""
    moment_at = at if at is not None else moment.utc_now()
    body = plan_program.render(programs, record)
    header = _generated_blockquote(moment_at) + "\n\n"
    return header + body if body.endswith("\n") else header + body + "\n"


def render_needs_attention_md(slug: str, problem: str, *, at: str | None = None) -> str:
    """The projection for a program whose record will not read: the sweep writes THIS rather than
    skipping the folder, so the damage is visible at rest instead of looking like the program vanished."""
    moment_at = at if at is not None else moment.utc_now()
    return (_generated_blockquote(moment_at) + "\n\n"
            f"# {slug}\n\n> **Needs attention.** This program's record did not read when the projection "
            f"was generated, so its state cannot be shown: {problem}\n")


def _program_md_path(programs: plan_program.ProgramLibrary, slug: str):
    return programs.program_dir(slug) / PROGRAM_MD


def _write_program_md(programs: plan_program.ProgramLibrary, slug: str, text: str) -> None:
    core.atomic_write(_program_md_path(programs, slug), text, durable=True, mode=plan_store.FILE_MODE)


def project_program(programs: plan_program.ProgramLibrary, slug: str, *, at: str | None = None):
    """Regenerate one program's PROGRAM.md from its record. Pure output — it never mutates a record.

    Raises if the record does not read: this is the per-verb refresh path, called right after a verb
    wrote the record, so the record is known good; a raise here is a real projection failure the caller
    is expected to degrade open on (warn, exit 0), never to swallow into the record's own success."""
    _write_program_md(programs, slug, render_program_md(programs, programs.read(slug), at=at))
    return _program_md_path(programs, slug)


def project_all(programs: plan_program.ProgramLibrary, *, at: str | None = None) -> list:
    """The library-wide sweep: regenerate every program's PROGRAM.md, and CONTINUE past a damaged one
    by writing it a needs-attention projection rather than letting the whole sweep die on it."""
    written = []
    for slug in programs.slugs():
        try:
            text = render_program_md(programs, programs.read(slug), at=at)
        except Exception as exc:  # noqa: BLE001 — a record that will not read gets a needs-attention file
            text = render_needs_attention_md(slug, str(exc), at=at)
        _write_program_md(programs, slug, text)
        written.append(slug)
    return written
