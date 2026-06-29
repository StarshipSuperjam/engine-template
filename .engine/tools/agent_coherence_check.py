#!/usr/bin/env python3
"""Persona-set coherence guard — the custom/script entry for engine/check/agent-coherence.

Runs as a `custom/script` check in the CI suite: it discovers the present personas
(`.claude/agents/*.md`), parses each one's frontmatter, and runs the pure agent coherence leg
(`validate.agent_coherence_findings`) over them. That leg owns four cross-field rules — a `role`
outside the closed set, a `model-tier` outside {judgment, mechanical}, a `lens` on a lensless role,
and (the load-bearing one here) a `permissions: read-only` persona that does not actually BLOCK the
authoritative-write tools (Edit/Write/NotebookEdit) via `disallowedTools` or a write-excluding
`tools` allowlist. The last rule turns the design's "permissions maps to the platform's tool
restrictions" from a declared-only label into a standing mechanical guard: a future read-only
persona authored with no tool lock (the inherit-all trap) reds engine-ci instead of silently
shipping a reviewer that can edit the work it reviews.

This is the live consumer the agent grammar's coherence leg was built for (validate.py
agent_coherence_findings): ZERO personas shipped with the grammar (D-066), so the leg had nothing to
fire on; the review/audit personas now ship, so the guard has real subjects and runs every CI —
arming its role/model-tier/lens legs live for the first time alongside the new permissions rule.

HONEST LIMIT: the guard enforces the write-tool floor; it deliberately does NOT police `Bash`, which
the execution roles (pre-submission-review, audit) legitimately keep to run the suite in a scratch
worktree. Bash-via-shell confinement is the orchestration worktree's + the protected-branch merge
gate's job, not a static frontmatter invariant this leg can see.

Reads local committed files only — no network, no token — so it runs unchanged in the head-checkout
engine-ci context. Emits finding.v1 JSON on stdout and returns 0 on a successful evaluation: an empty
array when every persona is coherent, one finding per problem (each carrying the plain-language fix).
An internal crash returns non-zero, which the custom/script kind turns into a hard fail-closed
finding. `demo` prints an operator-runnable fail-then-pass narration of the guard.
"""
from __future__ import annotations
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402

_AGENT_GLOB = ".claude/agents/*.md"
_MESSAGE = ("A reviewer or audit persona declared read-only must be one the platform cannot let edit "
            "the work it reviews. Correct the persona's frontmatter in .claude/agents/<name>.md so a "
            "read-only persona blocks the write tools — add Edit, Write, NotebookEdit to its "
            "disallowedTools (the design-review lenses also block Bash, since they never run code).")


def engine_agents(root: str | None = None) -> list:
    """Parse the present personas' frontmatter. Inject the filename stem as `name` when the
    frontmatter omits it, so a finding names the persona file the operator would actually open."""
    base = root or validate.ROOT
    agents = []
    for path in sorted(glob.glob(os.path.join(base, _AGENT_GLOB))):
        fm = dict(validate.frontmatter(path))
        fm.setdefault("name", os.path.splitext(os.path.basename(path))[0])
        agents.append(fm)
    return agents


def emit(findings: list) -> int:
    """Write the finding.v1 array to stdout and return 0 — a successful evaluation, whatever it found."""
    print(json.dumps(findings))
    return 0


def _demo() -> int:
    """An operator-runnable fail-then-pass demonstration over the REAL guard and the REAL personas.
    Nothing on disk changes — the "broken" variant is built in memory. It shows the engine's read-only
    review/audit personas really do block the write tools, and that the guard catches it if that lock
    is ever removed."""
    tier = "hard"
    present = engine_agents()
    print("Your engine's review/audit personas, and the safety check that keeps the read-only ones "
          "unable to edit the work they review:\n")
    if not present:
        print("  (no personas are installed yet)")
        return 0
    for a in present:
        if a.get("permissions") != "read-only":
            continue
        deny = a.get("disallowedTools")
        locked = isinstance(deny, list) and all(t in deny for t in ("Edit", "Write", "NotebookEdit"))
        note = "read-only — the platform blocks it from editing/writing files" if locked \
            else "read-only but NOT locked — it could still edit files"
        print(f"  {str(a.get('name')):34} {note}")

    clean = validate.agent_coherence_findings(present, tier, _MESSAGE)
    if clean:
        print("\nThe safety check found a problem with the personas as installed (see engine-ci).")
    else:
        print("\nThe safety check: all clear — every read-only persona really is one the platform "
              "stops from editing the work it reviews.")

    target = next((a for a in present
                   if a.get("permissions") == "read-only" and isinstance(a.get("disallowedTools"), list)), None)
    if target is None:
        print("\n(no locked read-only persona installed yet to demonstrate the guard on)")
        return 0
    broken = {k: v for k, v in target.items() if k not in ("disallowedTools", "tools")}
    found = validate.agent_coherence_findings([broken], tier, _MESSAGE)
    name = target.get("name")
    print(f"\nNow suppose someone removed the tool lock from {name} (shown here in memory only — your "
          f"files are untouched):")
    if found:
        print(f"  -> the safety check turns RED: {name} would inherit every tool, including the ones "
              f"that edit and write files, while still calling itself read-only. The build is blocked "
              f"until the lock is put back.")
    print("\nThat is the safety net: a read-only reviewer can't quietly become one that edits the work "
          "it reviews — the check catches it before it could be merged.")
    print("\nThe honest limit: this guarantees the write tools (Edit/Write/NotebookEdit) are blocked. "
          "The qa and audit personas still keep Bash to run checks; confining what Bash does to a "
          "throwaway copy is the build's worktree isolation and your merge gate, not this check.")
    if not found:
        print("\nDEMO UNEXPECTED: the guard did not flag the removed tool lock.", file=sys.stderr)
        return 1
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    tier = os.environ.get("ENGINE_RULE_TIER", "hard")
    return emit(validate.agent_coherence_findings(engine_agents(), tier, _MESSAGE))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
