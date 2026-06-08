#!/usr/bin/env python3
"""Skill-set self-election guard (core slice 26a) — the custom/script entry for engine/check/skill-coherence.

Runs as a `custom/script` check in the CI suite: it discovers the present ENGINE skills
(`.claude/skills/engine-*/SKILL.md` and the legacy `.claude/commands/engine-*.md`), parses each SKILL.md
frontmatter, and runs the pure skill coherence leg (`validate.skill_coherence_findings`) over them — the
self-election leak-guard that keeps an operator-typed verb (a command only the operator types) from being
one the model could still invoke itself. It is scoped to the engine's OWN, engine-prefixed skills: the
operator authors their own un-prefixed product skills in the same `.claude/skills/` directory, and the
engine never governs those (skills/README §location-and-collision).

This is the live consumer the skill grammar's coherence leg was built for (validate.py
skill_coherence_findings): ZERO skills shipped with the grammar (Slice SG), so the leg had nothing to fire
on; slice 26 ships the first operator-typed verbs, so the guard now has real subjects and runs every CI.
It strengthens the self-election safety property from an authoring-time fixture test into a standing
mechanical guard — a future edit that drops the operator-only flag on the Build verb turns engine-ci red
instead of silently regressing it.

Reads local committed files only — no network, no token — so it runs unchanged in the head-checkout
engine-ci context. Emits finding.v1 JSON on stdout and returns 0 on a successful evaluation: an empty
array when every engine skill's declared invocation agrees with its real platform flags, one finding per
disagreement (each carrying the plain-language fix). An internal crash returns non-zero, which the
custom/script kind turns into a hard fail-closed finding. `demo` prints an operator-runnable fail-then-pass
narration of the guard.
"""
from __future__ import annotations
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402

# The engine governs only its OWN skills (the engine- prefix); operator product skills stay un-governed.
_ENGINE_SKILL_GLOBS = (".claude/skills/engine-*/SKILL.md", ".claude/commands/engine-*.md")
_MESSAGE = ("An engine command that only the operator should type must be one the assistant cannot start "
            "on its own. Set the command back to operator-only in its .claude/skills/<name>/SKILL.md so "
            "the assistant cannot invoke it itself.")


def _typed_name(path: str) -> str:
    """The command the operator types for a skill file: the skill DIRECTORY name (a SKILL.md), or the
    legacy command FILENAME (a .claude/commands/<name>.md)."""
    parent = os.path.basename(os.path.dirname(path))
    if parent and parent != "commands":
        return parent
    return os.path.splitext(os.path.basename(path))[0]


def engine_skills(root: str | None = None) -> list:
    """Parse the present engine-prefixed skills' frontmatter. Inject the typed name as `name` when the
    frontmatter omits it, so a finding names the command the operator would actually see."""
    base = root or validate.ROOT
    skills = []
    for pattern in _ENGINE_SKILL_GLOBS:
        for path in sorted(glob.glob(os.path.join(base, pattern))):
            fm = dict(validate.frontmatter(path))
            fm.setdefault("name", _typed_name(path))
            skills.append(fm)
    return skills


def emit(findings: list) -> int:
    """Write the finding.v1 array to stdout and return 0 — a successful evaluation, whatever it found."""
    print(json.dumps(findings))
    return 0


def _demo() -> int:
    """An operator-runnable fail-then-pass demonstration over the REAL guard and the REAL engine commands.
    Nothing on disk changes — the "broken" variant is built in memory. It shows the engine's commands are
    operator-only and that the guard catches it if that protection is ever removed."""
    tier = "hard"
    present = engine_skills()
    print("Your engine's commands, and the safety check that keeps the operator-only ones safe:\n")
    if not present:
        print("  (no engine commands are installed yet)")
        return 0
    for s in present:
        op_only = s.get("disable-model-invocation") is True
        note = "operator-only — you type it; the assistant can't start it" if op_only \
            else "the assistant may also start it"
        print(f"  /{str(s.get('name')):22} {note}")

    clean = validate.skill_coherence_findings(present, tier, _MESSAGE)
    if clean:
        print("\nThe safety check found a problem with the commands as installed (see engine-ci).")
    else:
        print("\nThe safety check: all clear — every operator-only command really is one the assistant "
              "cannot start by itself.")

    target = next((s for s in present if s.get("disable-model-invocation") is True), None)
    if target is None:
        print("\n(no operator-only command installed yet to demonstrate the guard on)")
        return 0
    broken = {k: v for k, v in target.items() if k != "disable-model-invocation"}
    found = validate.skill_coherence_findings([broken], tier, _MESSAGE)
    name = target.get("name")
    print(f"\nNow suppose someone removed the operator-only protection from /{name} (shown here in memory "
          f"only — your files are untouched):")
    if found:
        print(f"  -> the safety check turns RED: /{name} would become a command the assistant could start "
              f"on its own, when only you should. The build is blocked until the protection is put back.")
    print("\nThat is the safety net: an operator-only command can't quietly become one the assistant can "
          "start itself — the check catches it before it could be merged.")
    print("\nThe honest limit: this guarantees the assistant can't AUTO-START this command. It does not "
          "make it impossible for the assistant to begin building another way — that stays visible and "
          "deliberate, and nothing reaches your main branch without your approval either way.")
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    tier = os.environ.get("ENGINE_RULE_TIER", "hard")
    return emit(validate.skill_coherence_findings(engine_skills(), tier, _MESSAGE))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
