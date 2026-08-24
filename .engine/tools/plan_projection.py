#!/usr/bin/env python3
"""Readable projections of the plan library: PLAN.md per plan, INDEX.md and index.json over all of them.

These files are VIEWS. Nothing here is an authority and nothing here is a source: delete every
generated file in the library and this module rebuilds all of them from the immutable revisions
alone. That property is what makes it safe for the projections to be rich — a generated file that
were also a source would be a second place the truth lives, and the two would drift.

Determinism is the load-bearing property. Regenerating from the same revision must produce the same
bytes, or an operator cannot tell "the plan changed" from "the renderer changed", and a diff of the
library stops meaning anything. So: no timestamps of the moment, no dictionary iteration that depends
on insertion order, no locale-sensitive formatting. Every value rendered comes from the revision or
the record.

The audience is an operator opening a folder cold, months later, with no session context. That is why
PLAN.md leads with what the plan is FOR and the reasoning behind it rather than with its graph, and
why the scheduling consequences are spelled out in prose beneath the diagram instead of being left
for the reader to derive from a table of edges.
"""
from __future__ import annotations

import json
from pathlib import Path

import build_coordinator_core as core
import plan_contract
import plan_store

PlanProjectionError = plan_store.PlanStoreError

INDEX_JSON = "index.json"
INDEX_MD = "INDEX.md"
PLAN_MD = "PLAN.md"

_GENERATED_NOTE = "<!-- generated from this plan's immutable revisions; edits here are overwritten -->"


def critical_path(items: list) -> dict:
    """Longest remaining successor chain per node, counting the node itself.

    This is what makes the DAG legible as a SCHEDULE rather than a picture: the node with the longest
    chain behind it is the one whose slipping moves the end date, and that is the thing an operator
    actually wants to know from a graph.
    """
    successors = {item["id"]: [] for item in items}
    for item in items:
        for dependency in item.get("depends_on", []):
            successors[dependency].append(item["id"])
    memo: dict = {}

    def depth(node: str) -> int:
        if node not in memo:
            memo[node] = 1 + max((depth(s) for s in successors[node]), default=0)
        return memo[node]

    return {item["id"]: depth(item["id"]) for item in items}


def _quote(text: str) -> str:
    """Render prose as a Markdown blockquote, preserving its own line breaks. Multiline raw intent is
    the common case and losing its shape would misrepresent what the operator actually wrote."""
    return "\n".join("> " + line if line else ">" for line in text.split("\n"))


def render_plan(document: dict, record: dict) -> str:
    """PLAN.md for one plan. Pure: same inputs, same bytes, always."""
    payload = document["build_plan"]
    items = payload["work_items"]
    chains = critical_path(items)
    out: list = []
    add = out.append

    add(f"# {document['title']}")
    add("")
    add(_GENERATED_NOTE)
    add("")
    add(f"- **Plan**: `{document['plan_id']}` · revision {document['revision']}")
    add(f"- **Status**: {plan_store.derived_status(record, head_blockers=plan_contract.seal_blockers(document))}"
        " — derived from evidence, never stored")
    add(f"- **Last revised**: {document['revised_at']}")
    add(f"- **Plan digest**: `{record['current']['plan_digest']}`")
    add(f"- **Build payload digest**: `{record['current']['build_plan_digest']}`")
    add(f"- **Profile**: {payload['profile']} · **Execution**: {payload['parallelism']['mode']}, "
        f"at most {payload['parallelism']['max_concurrency']} node(s) at once")
    add("")

    add("## Intent")
    add("")
    add("**As the operator put it:**")
    add("")
    add(_quote(document["intent"]["raw"]))
    add("")
    add(f"**As interpreted:** {document['intent']['interpretation']}")
    add("")

    add("## Objective")
    add("")
    add(payload["objective"])
    add("")

    deliberation = document["deliberation"]
    add("## Deliberation")
    add("")
    add("### The problem")
    add("")
    add(deliberation["problem_frame"])
    add("")
    add("### The strongest case against doing this")
    add("")
    add(deliberation["case_against"])
    add("")
    if deliberation["alternatives"]:
        add("### Alternatives considered")
        add("")
        for alternative in deliberation["alternatives"]:
            add(f"- **{alternative['option']}** — _{alternative['disposition']}_: {alternative['reason']}")
        add("")
    if deliberation["failure_modes"]:
        add("### How this could fail")
        add("")
        for mode in deliberation["failure_modes"]:
            add(f"- {mode}")
        add("")
    add("### Open decisions")
    add("")
    if deliberation["unresolved_decisions"]:
        for question in deliberation["unresolved_decisions"]:
            add(f"- {question}")
        add("")
        add("_The plan cannot be sealed while any of these is unanswered._")
    else:
        add("_None outstanding._")
    add("")

    if document.get("operator_decisions"):
        add("## Decisions the operator made")
        add("")
        for decision in document["operator_decisions"]:
            add(f"- {decision['decision']} _({decision['recorded']})_")
        add("")

    if document.get("intake"):
        add("## Where this plan came from")
        add("")
        add(document["intake"]["provenance"])
        add("")
        for predecessor in document["intake"].get("predecessors", []):
            add(f"- {predecessor}")
        if document["intake"].get("predecessors"):
            add("")

    add("## What success requires")
    add("")
    for number, obligation in enumerate(payload["success_obligations"], 1):
        add(f"{number}. **{obligation['outcome']}**")
        add(f"   - _Verified by:_ {obligation['verification']}")
    add("")

    if payload.get("evidence"):
        add("## Evidence this rests on")
        add("")
        for item in payload["evidence"]:
            add(f"- _({item['kind']})_ {item['claim']}")
            add(f"  - Basis: {item['basis']}")
        add("")

    if payload.get("assumptions"):
        add("## Assumptions")
        add("")
        for assumption in payload["assumptions"]:
            add(f"- **[{assumption['status']}]** {assumption['claim']}")
        add("")

    add("## Scope")
    add("")
    add("**In scope:**")
    add("")
    for entry in payload.get("scope_boundary", []):
        add(f"- {entry}")
    add("")
    if payload.get("non_goals"):
        add("**Explicitly not in scope:**")
        add("")
        for entry in payload["non_goals"]:
            add(f"- {entry}")
        add("")

    if payload.get("risks"):
        add("## Risks")
        add("")
        for risk in payload["risks"]:
            add(f"- {risk}")
        add("")

    if payload.get("review_strategy"):
        add("## Review strategy")
        add("")
        add(payload["review_strategy"])
        add("")

    add("## Specification posture")
    add("")
    add(f"**{payload['spec']['posture']}** — {payload['spec']['selection_basis']}")
    add("")
    add(payload["spec"]["disclosure"])
    add("")

    add("## Execution graph")
    add("")
    add("```mermaid")
    add("graph TD")
    for item in items:
        add(f'  {_node(item["id"])}["{item["id"]}"]')
    for item in items:
        for dependency in item.get("depends_on", []):
            add(f"  {_node(dependency)} --> {_node(item['id'])}")
    add("```")
    add("")
    add("| Node | Depends on | Critical path | Exclusive resources |")
    add("|---|---|---:|---|")
    for item in items:
        dependencies = ", ".join(f"`{d}`" for d in item.get("depends_on", [])) or "—"
        resources = ", ".join(f"`{r}`" for r in item.get("exclusive_resources", [])) or "—"
        add(f"| `{item['id']}` | {dependencies} | {chains[item['id']]} | {resources} |")
    add("")
    add(_scheduling_prose(items, chains, payload["parallelism"]))
    add("")

    add("## The work, node by node")
    add("")
    for item in items:
        add(f"### `{item['id']}`")
        add("")
        add(item["description"])
        add("")
        add(f"- **Depends on**: {', '.join(f'`{d}`' for d in item.get('depends_on', [])) or '—'}")
        add(f"- **Executor**: {item.get('executor_class', '—')}")
        add(f"- **Paths**: {', '.join(f'`{p}`' for p in item['paths'])}")
        resources = ", ".join(f"`{r}`" for r in item.get("exclusive_resources", [])) or "—"
        add(f"- **Exclusive resources**: {resources}")
        contract = item.get("output_contract")
        if contract:
            add(f"- **Deliverable**: {contract['deliverable']}")
            add(f"- **Artifact kinds**: {', '.join(contract['artifact_kinds'])}")
            add(f"- **Required evidence**: {', '.join(contract['required_evidence'])}")
        add("- **Verification**:")
        for check in item["verification"]:
            add(f"  - {check}")
        add("")

    add("## Revision history")
    add("")
    add("| Revision | Revised | Note |")
    add("|---:|---|---|")
    for entry in record["ledger"]:
        note = entry.get("note", "—")
        if "redacted" in entry:
            note = f"_body redacted {entry['redacted']['at']} — {entry['redacted']['reason']}_"
        add(f"| {entry['revision']} | {entry['revised_at']} | {note} |")
    add("")

    return "\n".join(out).rstrip() + "\n"


def _node(identifier: str) -> str:
    """A mermaid-safe node name. Mermaid treats a hyphen as syntax in a bare id, so ids that read
    naturally to a human need escaping to survive the round trip into a diagram."""
    return "n_" + "".join(character if character.isalnum() else "_" for character in identifier)


def _scheduling_prose(items: list, chains: dict, parallelism: dict) -> str:
    """The table above says what depends on what. This says what that MEANS for how the work runs —
    the part an operator would otherwise have to derive by eye."""
    longest = max(chains.values())
    entry_points = sorted(item["id"] for item in items if chains[item["id"]] == longest)
    roots = sorted(item["id"] for item in items if not item.get("depends_on"))
    mode, limit = parallelism["mode"], parallelism["max_concurrency"]
    lines = [
        f"The longest chain runs {longest} node(s) deep, entering at "
        + ", ".join(f"`{entry}`" for entry in entry_points)
        + f". {len(roots)} node(s) can start immediately: " + ", ".join(f"`{root}`" for root in roots) + "."
    ]
    if mode == "serial" or limit == 1:
        lines.append(
            "Execution is serial, so exactly one node runs at a time and the declared exclusive "
            "resources are never contended — the graph here constrains ORDER, not concurrency.")
    else:
        lines.append(
            f"Execution is conditional at up to {limit} nodes at once, so two nodes may run together "
            "only when the graph allows it and their declared exclusive resources do not overlap.")
    return " ".join(lines)


# --- writing the projections -------------------------------------------------

def project_plan(library: plan_store.PlanLibrary, slug: str) -> Path:
    """Regenerate one plan's PLAN.md. Reads the head THROUGH the store, so a plan whose head does not
    match its recorded digest refuses here too rather than being rendered as though it were sound."""
    record = library.read_record(slug)
    document = library.head(slug)
    path = library.plan_dir(slug) / PLAN_MD
    _write(library, path, render_plan(document, record))
    return path


def render_index(entries: list) -> str:
    out = [
        "# Plan library",
        "",
        "<!-- generated from the plans' own records; edits here are overwritten -->",
        "",
        "A shelf, not a queue. Nothing here is automatically current, and no command picks one of these "
        "for you: select a plan by its full id, an unambiguous id prefix, or its folder name.",
        "",
        "| Plan | Status | Rev | Last activity | Id |",
        "|---|---|---:|---|---|",
    ]
    for entry in entries:
        title = entry["title"].replace("|", "\\|")
        link = f"[{title}]({entry['slug']}/{PLAN_MD})" if entry["readable"] else title
        out.append(f"| {link} | {entry['status']} | {entry['revision']} | {entry['last_activity']} "
                   f"| `{entry['plan_id']}` |")
    if not entries:
        out.append("| _no plans yet_ | — | — | — | — |")
    unreadable = [entry for entry in entries if not entry["readable"]]
    if unreadable:
        out += ["", "### Needs attention", ""]
        for entry in unreadable:
            out.append(f"- **{entry['slug']}** — {entry['problem']}")
    return "\n".join(out) + "\n"


def project_library(library: plan_store.PlanLibrary, *, force: bool = False) -> list:
    """Regenerate every projection in the library and return the index entries.

    A plan whose head is damaged still gets an INDEX row, marked, rather than being dropped: an
    operator scanning the shelf must be able to see that a plan is in trouble. Silently omitting it
    would make the damage look like the plan never existed.
    """
    entries = []
    for slug in library.slugs():
        try:
            record = library.read_record(slug)
        except plan_store.PlanStoreError as exc:
            entries.append({"slug": slug, "plan_id": "unknown", "title": slug, "status": "unreadable",
                            "revision": 0, "last_activity": "—", "readable": False, "problem": str(exc)})
            continue
        entry = {
            "slug": slug,
            "plan_id": record["plan_id"],
            "title": record["title"],
            "revision": record["current"]["revision"],
            "last_activity": record["ledger"][-1]["revised_at"],
            "readable": True,
            "problem": "",
        }
        try:
            document = library.head(slug)
            entry["status"] = plan_store.derived_status(
                record, head_blockers=plan_contract.seal_blockers(document))
            # Render ALWAYS; skip only the WRITE, and only when the bytes are provably identical.
            #
            # An earlier version skipped rendering a plan whose `closure` was set, reasoning that a
            # closed plan's inputs are frozen. That was wrong on the one transition it most had to
            # handle: `close` sets the closure and then projects, so the skip fired on the very first
            # render after closing and PLAN.md kept its pre-closure status forever, disagreeing with
            # the index that sat beside it. The lesson is the one the disposition of the original
            # performance finding already stated and then failed to honour — any staleness heuristic
            # over these files risks the single property they exist to have. Comparing bytes cannot
            # go stale: it saves the I/O and the mtime churn, and concedes the CPU.
            target = library.plan_dir(slug) / PLAN_MD
            rendered = render_plan(document, record)
            if force or not target.exists() or target.read_text(encoding="utf-8") != rendered:
                _write(library, target, rendered)
        except plan_store.PlanStoreError as exc:
            entry["status"] = plan_store.derived_status(record)
            entry["readable"] = False
            entry["problem"] = str(exc)
        entries.append(entry)

    entries.sort(key=lambda item: item["slug"])
    index = {"schema_version": "plan-index.v1",
             "plans": [{k: entry[k] for k in
                        ("plan_id", "slug", "title", "status", "revision", "last_activity")}
                       for entry in entries]}
    _write(library, library.root / INDEX_JSON,
           json.dumps(index, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    _write(library, library.root / INDEX_MD, render_index(entries))
    return entries


def _write(library: plan_store.PlanLibrary, path: Path, text: str) -> None:
    # Projections are rebuildable, so they do not need the durability barrier the revisions get. They
    # DO need the same privacy: a projection restates the plan in full, so a world-readable PLAN.md
    # beside a 0600 revision would leak exactly what the permissions were for. The library is passed
    # in so directory tightening is bounded by its root and never reaches a parent that is not ours.
    plan_store.ensure_dir(path.parent, within=library.root)
    core.atomic_write(path, text, mode=plan_store.FILE_MODE)
