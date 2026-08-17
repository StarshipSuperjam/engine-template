#!/usr/bin/env python3
"""Shipped local-reference floor (StarshipSuperjam/engine-template#943) — the custom/script entry for
engine/check/shipped-local-references, and the shared scan the release cut reuses.

WHAT IT PROTECTS. engine-template is distributed by "Use this template", which copies the file tree as one
commit; Issues, pull requests and history do not travel. So a bare reference that means something only in the
repository that wrote it — a decision-record id, a spec section, a ticket prefix — names a record a reader of
a generated repository cannot reach. A surface that travels must name the CAPABILITY it means, never a
reference that resolves only where it was written. The sibling floor
(`shipped_issue_references_check.py`, StarshipSuperjam/engine-template#640) closes the bare GitHub `#N` species; this closes the
deployment-DECLARED species (the engine's own decision-record shorthand `D-###`, and whatever a deployment
declares of its own).

DECLARATION-DRIVEN, so it is safe everywhere by CONSTRUCTION — not by a home-repo gate. The vocabulary is read
from `.engine/operator-local-references.json` (StarshipSuperjam/engine-template#639), which is per-deployment operator config that
ships ABSENT and never travels. A repository that has declared nothing compiles an EMPTY vocabulary, so the
scan matches nothing and this floor no-ops — the normal steady state for every deployment. engine-template
declares `D-###`, so the floor is live here; a deployment that declares its own shorthand gets the same
protection for it. This is why the floor needs no `is_home_repo` gate (the sibling `#N` floor needs one only
because a bare `#N` is a legitimate reference in a deployed repo): here the declaration itself scopes it, the
same way the StarshipSuperjam/engine-template#639 contribution scan (`local_references` via `external_contribution/submit.py`) is
declaration-driven rather than home-gated.

WHAT IT SCANS — reuses the sibling floor's ONE definition of the shipped surface and its prose extraction, so
the two floors never drift: `.engine/**` minus the first-run retire set minus the excluded paths, plus the
foundation files outside `.engine/`, EXCLUDING `test_*`/`demo_*` (synthetic scenario tokens), and only the
PROSE of each file (comments + docstrings for `.py`, whole text for `.md`/`.yml`/…, prose-key values for
`.json`) — never a string literal, which holds fixture data and behaviour-bearing messages. The declared
vocabulary is matched by `local_references.scan`, the same matcher the contribution scan uses.

HONEST BOUNDS. A literal token match narrows risk, it never proves absence: a split token or a Unicode-hyphen
form passes, and paraphrase passes trivially. It catches only what is DECLARED and shaped as an id-prefix /
phrase / section-ref; a source-owned `ADR-####` or an undeclared vocabulary is unpoliced here. Blocking with
no per-token exemption is deliberate — the remedy is always available (name the capability), which is the
rule itself. The review at merge stays the real wall.

FAIL DIRECTIONS. An unreadable/unusable/empty/absent DECLARATION yields no vocabulary and this floor stays
silent — the fail-CLOSED teeth for a malformed declaration are the shape gate
(`operator_local_references_check.py`), which blocks it from reaching the base branch. A single shipped file
that cannot be read or parsed is SKIPPED, never crashing the whole scan (mirroring the sibling floor). The one
fail-CLOSED here is an unreadable first-run retire census: without it the shipped surface cannot be
enumerated, so the floor cannot confirm nothing leaks and emits a hard fault.

Runs as a hard CI custom/script check: finding.v1 JSON on stdout, return 0 on a successful evaluation (empty
array = nothing ships). `ENGINE_LOCAL_REFERENCES_PATH` (unset in production) seeds a declaration and
`ENGINE_REF_SCAN_ROOT` (unset in production) seeds a mini shipped-tree, so the negative-fixture meta-check can
witness the floor biting a real bad input.
"""
from __future__ import annotations
import ast
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402  (finding.v1, ROOT, env_override_path)
import local_references  # noqa: E402  (declared vocabulary + matcher — StarshipSuperjam/engine-template#639)
import shipped_issue_references_check as shipped_surface  # noqa: E402  (shipped-surface enum + prose — StarshipSuperjam/engine-template#640)

_RETIRE_MANIFEST_REL = os.path.join(".engine", "provisioning", "first-run-assets.json")


def _retire_fault_message() -> str:
    return (
        f"The engine can't read the list of files removed when a project is first set up "
        f"(`{_RETIRE_MANIFEST_REL}`). Without it this floor can't tell which files ship, so it can't confirm "
        f"that no bare local reference travels into a generated repository, and it can't pass. Restore that "
        f"file from the project's history — it is permanent data — then re-run this check.")


def hits(root: str, vocabulary: list) -> list | None:
    """Every declared-reference occurrence in the PROSE of the shipped surface, as `local_references.scan`
    records them (`[{where, line, kind, token, declared}]`). Returns None when the shipped surface cannot be
    enumerated (the first-run retire census is unreadable) so the caller can fail closed. An individual
    unreadable or unparseable file is SKIPPED, never failing the whole scan."""
    retire = shipped_surface.retire_set(root)
    if retire is None:
        return None
    retire_files, retire_dirs = retire
    lines = []
    for rel in shipped_surface.scan_targets(root, retire_files, retire_dirs):
        try:
            with open(os.path.join(root, rel), encoding="utf-8") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError):
            continue  # an unreadable committed file is an anomaly other checks surface; never crash this scan
        if rel.endswith(".py"):
            try:
                ast.parse(text)
            except SyntaxError:
                continue  # an unparseable .py is caught by other checks; don't crash this one
            frags = shipped_surface.py_prose_fragments(text)
        else:
            frags = shipped_surface.text_fragments(text, is_json=rel.endswith(".json"))
        for lineno, fragment in frags:
            lines.append((rel, lineno, fragment))
    return local_references.scan(vocabulary, lines=lines)


def _message(hit: dict) -> str:
    where = hit.get("where") or "(unknown file)"
    line = hit.get("line")
    loc = f"`{where}`" + (f" line {line}" if line else "")
    return (
        f"{loc} carries a bare local reference (`{hit.get('token')}`) in a file that ships into every "
        f"generated repository, where it names a record that repository cannot reach — a reader meets a bare "
        f"identifier with nowhere to go. Name the CAPABILITY it means instead of the bare reference, or move "
        f"it to a form that travels: an engine `eADR-####` record, or a fully-qualified `owner/repo#N`. The "
        f"references treated as local are declared in `.engine/operator-local-references.json`; if this file "
        f"does not actually ship into a generated repository, it belongs in the first-run retirement set "
        f"(`.engine/provisioning/first-run-assets.json`).")


def check(root: str | None = None) -> list:
    """Every shipped bare local reference as a list of findings at the rule's tier (hard in CI). Empty when
    the deployment has declared no vocabulary (the steady state) — nothing compiled, nothing scanned. Fails
    CLOSED (a hard fault) only when the shipped surface cannot be enumerated."""
    root = root or validate.ROOT
    decl_override = validate.env_override_path("ENGINE_LOCAL_REFERENCES_PATH")
    vocabulary, _state = local_references.load_vocabulary(decl_override if decl_override else None)
    if not vocabulary:
        return []  # ABSENT/EMPTY/UNUSABLE/UNREADABLE -> nothing to match; the shape gate is the teeth
    tier = os.environ.get("ENGINE_RULE_TIER", "hard")
    found = hits(root, vocabulary)
    if found is None:
        return [validate.finding(tier, _retire_fault_message(),
                                 {"file": _RETIRE_MANIFEST_REL, "line": None})]
    return [validate.finding(tier, _message(h), {"file": h.get("where"), "line": h.get("line")})
            for h in found]


def main() -> int:
    # ENGINE_REF_SCAN_ROOT (unset in production) points the scan at a seeded mini shipped-tree; the declaration
    # override above seeds the vocabulary — together they let the negative-fixture meta-check witness the floor
    # biting a real bad input. No home-repo gate, so the fixture bites wherever the meta-check runs it.
    print(json.dumps(check(validate.env_override_path("ENGINE_REF_SCAN_ROOT"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
