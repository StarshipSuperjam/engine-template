#!/usr/bin/env python3
"""Decision-record form inspector — the read-only `custom/script` entry for engine/check/product-adr-form
(the product-design module's presence check over the product's own decision records under `docs/adr/`).

What it does: when the engine has written decision records for a project — plain files under `docs/adr/`, one
per significant choice, each recording what was decided and what was ruled out — it confirms each record still
carries its `## What we ruled out` section, present and with something in it. That section is the anti-churn
value of a record: it names the alternatives that were weighed and turned down, so a later session does not
re-open ground already settled.

Only the engine's OWN records are checked. A decision record the engine authored carries a `status:` line in a
leading `---` frontmatter block (the starting shape under `.engine/modules/product-design/scaffold/adr.md`
writes one); a record kept in some other style — the common public convention keeps its status as a `## Status`
section, not frontmatter — carries no such block and is left untouched. This is the engine/product wall: the
engine validates the FORM of the records it wrote as a contributor, and never annexes a project's own doc tree
by imposing its shape on files it did not author.

Presence, not judgement: the check confirms the section is there with content — never whether the reasons given
are sound. That stays the operator's call and the review lenses'; genuineness is posture, presence is the gate.

Disclosed-no-op: when the engine has written no records yet — no `docs/adr/` tree, or one with no
engine-authored records in it — it says so plainly (one soft note, never a silent pass), and starts checking on
its own once the engine writes a record.

Honest floor — the engine/product wall and the read-only firewall: it inspects the product's own `docs/adr/`
tree ONLY, never the engine's own `.engine/` files; it reads file contents to check structure but never writes.

Operator-communication law: the engine-side framework vocabulary for these records never surfaces to the
operator — every finding says "decision record" and "what was ruled out" in plain words, never a raw token.

Shared grammar: this reuses the product_design package's one home for reading a doc's frontmatter and headings
(`spec_form._read` / `_frontmatter_status` / `_h2_headings` / `_section_body`) rather than a second copy, so the
parsers can never drift apart across the module's checks.

Contract: invoked by the validator with NO arguments, it prints a finding.v1 JSON array to stdout and exits 0.
A separate `demo` subcommand runs a falsifiable self-check.
"""
from __future__ import annotations
import json
import os
import re
import sys

# Make the sibling `.engine/tools/` modules importable whether imported as `product_design.adr_form`
# or run directly as the wired check script (the spec_form / migration_discipline idiom).
_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

import validate  # noqa: E402 — ROOT (test-redirectable) + the finding.v1 helper
from product_design import spec_form  # noqa: E402 — the package's shared doc-reading grammar

# The committed decision-record tree, relative to the repository root.
_ADR_DIR = os.path.join("docs", "adr")
# A record is one file per decision, numbered in the project's own sequence: NNNN-<slug>.md (0001, 0002, …).
# A stray non-record file under docs/adr/ (a README or index) is not forced to carry the section.
_RECORD_NAME_RE = re.compile(r"^\d{4}-.+\.md$")
# The one checked section. Plain wording — the operator never sees framework vocabulary for these records.
_RULED_OUT_HEADING = "What we ruled out"

_BOUND_TAIL = (
    "This check only reads your files — it never changes them — and it checks that the section is present with "
    "something in it, not whether the reasons you gave are the right ones (that stays your call and the review "
    "lenses')."
)

_NO_OP_MESSAGE = (
    "Decision-record checking isn't active here yet — this looks for the project's own decision records under "
    "`docs/adr/` (the ones the engine wrote, each recording what was decided and what was ruled out) and didn't "
    "find any. That's a normal, expected state before any decision has been recorded, not an error: it starts "
    "checking a record's structure on its own once the engine writes one under `docs/adr/`. " + _BOUND_TAIL
)


def _adr_root(root: str) -> str:
    return os.path.join(root, _ADR_DIR)


def _record_rels(root: str) -> list:
    """Repo-root-relative paths of every numbered decision-record file (NNNN-*.md) under `docs/adr/`, sorted.
    Symlinks are not followed (the safe default); dot-directories are skipped; a non-record file (README/index)
    is excluded by the naming pattern so it is never forced to carry the checked section."""
    adr_root = _adr_root(root)
    out = []
    for dirpath, dirnames, filenames in os.walk(adr_root):  # followlinks=False (default)
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if _RECORD_NAME_RE.match(name):
                out.append(os.path.relpath(os.path.join(dirpath, name), root))
    return sorted(out)


def _section_is_nonempty(body: "str | None") -> bool:
    """True when a section body carries real content — anything left after HTML guidance comments and blank
    lines are dropped. Mirrors the core presence-kind's non-empty rule: a heading followed by nothing (or only
    stripped guidance) does not count as recording what was ruled out."""
    if body is None:
        return False
    without_comments = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    return any(line.strip() for line in without_comments.splitlines())


def _missing_section_message(rel: str) -> str:
    return (
        f"The decision record `{rel}` doesn't record what was ruled out. Every decision record needs a "
        f"`## {_RULED_OUT_HEADING}` section naming the alternatives that were weighed and turned down — the part "
        f"a later session reads before re-opening the choice, so settled ground isn't re-argued. To clear this, "
        f"add that section to `{rel}` with the options you ruled out and why each one lost. " + _BOUND_TAIL
    )


def _empty_section_message(rel: str) -> str:
    return (
        f"The decision record `{rel}` has a `## {_RULED_OUT_HEADING}` section but nothing in it. The section is "
        f"there to name the alternatives that were weighed and turned down, so a later session can see what was "
        f"already settled. To clear this, fill it in with the options you ruled out and why each one lost. "
        + _BOUND_TAIL
    )


def findings(tier: str, root: "str | None" = None) -> list:
    """The decision-record findings for `root` (defaults to `validate.ROOT`), as a list of finding.v1 dicts.

    Empty list = a genuine clean pass (every engine-authored record carries a filled `## What we ruled out`
    section). A single `soft` no-op finding when the engine has written no records yet — said plainly, never a
    silent pass. Otherwise one finding per record that is missing the section or has left it empty, each at
    `tier` severity (`hard`) so a record that drops its ruled-out alternatives blocks the merge; the no-op is
    always `soft` so a project with no records is never blocked. Only records carrying a frontmatter `status:`
    block — the ones the engine authored — are checked; a record kept in another style is left untouched (the
    engine/product wall)."""
    root = root or validate.ROOT
    adr_root = _adr_root(root)
    record_rels = _record_rels(root) if os.path.isdir(adr_root) else []

    # Only the engine's own records (a frontmatter `status:` block marks them) are this check's business.
    engine_rels = [rel for rel in record_rels
                   if spec_form._frontmatter_status(spec_form._read(root, rel)) is not None]

    # Disclosed no-op: no engine-authored records yet — whether the tree is absent, empty, or holds only
    # records kept in another style. Always soft, never silent.
    if not engine_rels:
        return [validate.disclosed_noop(_NO_OP_MESSAGE, None)]

    out = []
    want = _RULED_OUT_HEADING.lower()
    for rel in engine_rels:
        text = spec_form._read(root, rel)
        if want not in spec_form._h2_headings(text):
            out.append(validate.finding(tier, _missing_section_message(rel), {"file": rel, "line": None}))
        elif not _section_is_nonempty(spec_form._section_body(text, _RULED_OUT_HEADING)):
            out.append(validate.finding(tier, _empty_section_message(rel), {"file": rel, "line": None}))
    return out


def emit_findings() -> int:
    """The no-argument path the validator invokes: print the finding.v1 array and return 0. Violations carry
    the rule's declared tier (the validator passes it as ENGINE_RULE_TIER, defaulting hard); the no-op is
    always soft (set inside findings())."""
    # ENGINE_ADR_ROOT (unset in production) lets the negative-fixture meta-check point the record scan at a
    # seeded docs/adr tree, so the presence gate is witnessed biting a real bad input.
    print(json.dumps(findings(os.environ.get("ENGINE_RULE_TIER", "hard"),
                              validate.env_override_path("ENGINE_ADR_ROOT"))))
    return 0


def demo() -> int:
    """Prove the inspector: passes a well-formed engine record; says the no-op plainly (never silently) when no
    records exist and when only a foreign-style record is present; flags — at hard severity — an engine record
    missing its `## What we ruled out` section and one that left it empty; leaves a foreign record (no
    frontmatter) untouched even when it lacks the section (the engine/product wall); never shows a raw framework
    token in a finding; and never treats a record under `.engine/` as the product's own. RETURNS NON-ZERO if any
    invariant is broken (the falsification can fail). Mutation-free: every case runs against a throwaway temp
    root, so the real working tree is never touched."""
    import shutil
    import tempfile

    ok = True

    def _mkroot(files: dict) -> str:
        d = tempfile.mkdtemp(prefix="engine-adr-demo-")
        for rel, body in files.items():
            path = os.path.join(d, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
        return d

    good = ("---\nstatus: accepted\n---\n\n# Pick a datastore\n\n## The decision\n\nUse Postgres.\n\n"
            "## Why\n\nRelational fit.\n\n## What we ruled out\n\n- **A document store.** Weak joins.\n")
    no_section = ("---\nstatus: accepted\n---\n\n# Pick a datastore\n\n## The decision\n\nUse Postgres.\n\n"
                  "## Why\n\nRelational fit.\n")
    empty_section = ("---\nstatus: accepted\n---\n\n# Pick a datastore\n\n## The decision\n\nUse Postgres.\n\n"
                     "## What we ruled out\n\n<!-- nothing yet -->\n")
    foreign = ("# 1. Record architecture decisions\n\n## Status\n\nAccepted\n\n## Context\n\nWe need ADRs.\n\n"
               "## Decision\n\nUse ADRs.\n")

    cases = []

    d1 = _mkroot({os.path.join(_ADR_DIR, "0001-datastore.md"): good})
    cases.append(d1)
    fs = findings("hard", root=d1)
    if fs != []:
        ok = False
        print(f"DEMO FAIL: a well-formed engine record should pass cleanly, got {fs}", file=sys.stderr)

    d2 = _mkroot({os.path.join("docs", "readme.md"): "no adr here\n"})
    cases.append(d2)
    fs = findings("hard", root=d2)
    if not (len(fs) == 1 and fs[0].get("not_applicable")):
        ok = False
        print(f"DEMO FAIL: no records should be a disclosed no-op, got {fs}", file=sys.stderr)

    d3 = _mkroot({os.path.join(_ADR_DIR, "0001-decision.md"): foreign})
    cases.append(d3)
    fs = findings("hard", root=d3)
    if not (len(fs) == 1 and fs[0].get("not_applicable")):
        ok = False
        print(f"DEMO FAIL: a foreign-style record (no frontmatter) must be left untouched — a disclosed "
              f"no-op, not a finding, got {fs}", file=sys.stderr)

    d4 = _mkroot({os.path.join(_ADR_DIR, "0001-datastore.md"): no_section})
    cases.append(d4)
    fs = findings("hard", root=d4)
    if not (len(fs) == 1 and fs[0]["severity"] == "hard"
            and _RULED_OUT_HEADING in fs[0]["message"]):
        ok = False
        print(f"DEMO FAIL: an engine record missing the section must fire one hard finding, got {fs}",
              file=sys.stderr)

    d5 = _mkroot({os.path.join(_ADR_DIR, "0001-datastore.md"): empty_section})
    cases.append(d5)
    fs = findings("hard", root=d5)
    if not (len(fs) == 1 and fs[0]["severity"] == "hard"):
        ok = False
        print(f"DEMO FAIL: an engine record with an empty section must fire one hard finding, got {fs}",
              file=sys.stderr)

    # The wall: a record under .engine/ is never the product's own — a product root with only .engine/docs/adr
    # holds no product records, so the scan (rooted at <root>/docs/adr) sees none: a disclosed no-op.
    d6 = _mkroot({os.path.join(".engine", "docs", "adr", "0001-x.md"): no_section})
    cases.append(d6)
    fs = findings("hard", root=d6)
    if not (len(fs) == 1 and fs[0].get("not_applicable")):
        ok = False
        print(f"DEMO FAIL: a record under .engine/ must be walled out (no-op, never a finding), got {fs}",
              file=sys.stderr)

    # No raw framework token ever leaks into a finding's PROSE. Code spans (`docs/adr/…`, `## What we ruled
    # out`) are stripped first: the `docs/adr/` path and the plain heading are legitimate, not the acronym
    # surfacing as operator vocabulary.
    for d in (d4, d5):
        for f in findings("hard", root=d):
            prose = re.sub(r"`[^`]*`", "", f["message"]).lower()
            if "anti-choice" in prose or re.search(r"\badr\b", prose):
                ok = False
                print(f"DEMO FAIL: a finding leaked a raw framework token: {f['message']}", file=sys.stderr)

    for d in cases:
        shutil.rmtree(d, ignore_errors=True)

    if ok:
        print("adr_form demo: all invariants held (clean pass, disclosed no-op, foreign record untouched, "
              "missing + empty section bite at hard, .engine/ walled out, no raw token leak).")
    return 0 if ok else 1


def main(argv: list) -> int:
    if len(argv) > 1 and argv[1] == "demo":
        return demo()
    return emit_findings()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
