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

import plan_program

PORTFOLIO_NOTE = ("<!-- generated from the program records; a read-only view — nothing here selects, "
                  "starts, or advances work -->")

# The working states — a child still being built, not yet landed and not a dead branch. Everything a
# plan can derive to (plan_store.derived_status) that is neither `complete` nor a dead-branch closure
# nor an unreadable/missing row is in flight.
_LANDED = "complete"
_ATTENTION_STATES = ("missing", "unreadable")
_HEADLINE_CAP = 180


def goal_headline(objective: str, *, cap: int = _HEADLINE_CAP) -> str:
    """The objective's opening sentence, hard-capped and cut at a word boundary with an ellipsis.

    The live shelf holds objectives that are a single ~490-character sentence and 322-character opening
    sentences with an inline list, so "the first sentence" alone is not a bound — the cap is. A sentence
    at or under the cap is returned whole, its period kept; a longer one is cut at the last word break
    within the cap and given a single-character ellipsis so the truncation is visible.
    """
    text = " ".join(objective.split())          # collapse any internal whitespace, so the cap is on prose
    if not text:
        return "(no objective recorded)"
    match = re.search(r"\.(\s|$)", text)
    sentence = text[:match.start() + 1] if match else text
    if len(sentence) <= cap:
        return sentence
    cut = sentence[:cap]
    if " " in cut:
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip().rstrip(".,;:") + "…"


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


def _obligations_line(programs: plan_program.ProgramLibrary, record: dict) -> str:
    report = programs.obligation_report(record)
    known = [o["id"] for o in report["obligations"]]
    parts = []
    if known:
        parts.append(", ".join(known))
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
        out.append(f"- **In flight**: {', '.join(in_flight)}")
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
