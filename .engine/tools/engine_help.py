#!/usr/bin/env python3
"""`/engine-help` listing tool (core slice 26b) — the degradation-proof command index.

Backs the `/engine-help` operator command: a plain-language listing of the engine's own typed
commands so a non-engineer asking "what can I do here?" always gets an answer. It derives the listing
from committed files only — never an MCP substrate — so an outage cannot blank it (the §14 discovery
axis; degrade-to-git-native). Two parts:

- Installed commands — the engine's OWN, engine-prefixed, operator-typed verbs present on disk
  (`.claude/skills/engine-*/SKILL.md` and the legacy `.claude/commands/engine-*.md`), each shown as the
  command the operator types plus its one-line description. Scoped to the engine's commands (the
  engine/operator wall, the same scope the self-election guard governs); the operator's own un-prefixed
  product commands, and the full command set, are the platform's bare `/` menu to show, not this one's.
- Available-if-installed commands — optional commands the operator could add, RELAYED from the committed
  module catalog the install step maintains (a §16 relay: the catalog's owner is provisioning; this tool
  only reads it). No catalog exists yet (it is owed to the first-run instantiator slice), so this part is
  an empty relay today — present but with nothing to list.

Design fidelity notes (for a maintainer reading the source, not the operator):
- The verb shown is the TYPED name — the skill DIRECTORY (or the legacy command FILENAME), i.e. the
  string the operator actually types. The locked design says "the skill `name` (fallback: its directory)";
  on the platform the typed identity IS the directory and frontmatter `name` is only a display label
  (skill.v1 schema), so the typed name is the platform-correct source. For an engine command the two
  coincide.
- Each per-file frontmatter parse is wrapped: `validate.frontmatter` RAISES on malformed YAML (its
  halt-on-malformed posture), so a single broken command file must not crash the whole listing — the
  always-answers guarantee. This is the DELIBERATE OPPOSITE of the self-election guard
  (skill_coherence_check.engine_skills), which lets the raise propagate to fail closed: a detection guard
  must never silently pass, an operator listing must never go blank. Same discovery, opposite posture by
  design.
- The ~typed-name + engine-glob logic is intentionally a small local copy, not an import of the check
  tool's private helper (a verb tool must not reach up into a guard tool) nor an addition to the seed
  validator. When a third skill-globbing tool appears, extract a shared skill-discovery helper that
  exposes the raw per-file parse and lets each caller choose its posture.
"""
from __future__ import annotations
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402

# The engine's own commands carry the engine- prefix (the engine/operator wall); this is the same scope
# the self-election guard governs. The legacy commands directory lives under .claude/commands/.
_ENGINE_VERB_GLOBS = (".claude/skills/engine-*/SKILL.md", ".claude/commands/engine-*.md")

_HEADER = "Commands you can type:"
_AVAILABLE_HEADER = "You can also add these by installing more parts of your Engine:"
_EMPTY_AVAILABLE_LINE = "More commands become available as you add optional parts to your Engine."
_POINTER = "New to the Engine? Ask me to open the getting-started guide — it walks you through the basics."


def _typed_name(path: str) -> str:
    """The command the operator types for a verb file: the skill DIRECTORY name (a SKILL.md), or the
    legacy command FILENAME (a .claude/commands/<name>.md). Mirrors the self-election guard's helper."""
    parent = os.path.basename(os.path.dirname(path))
    if parent and parent != "commands":
        return parent
    return os.path.splitext(os.path.basename(path))[0]


def installed_verbs(root: str | None = None) -> list:
    """The engine's installed operator-typed commands as a list of {name, description}, sorted by the
    typed name. Globs only the engine-prefixed command files and keeps only the commands the operator
    actually types (the operator-typed invocation axis). Each file's frontmatter parse is guarded: a
    malformed command file is skipped rather than allowed to crash the whole listing (degrade, never
    blank — the always-answers guarantee)."""
    base = root or validate.ROOT
    verbs = []
    for pattern in _ENGINE_VERB_GLOBS:
        for path in sorted(glob.glob(os.path.join(base, pattern))):
            try:
                fm = validate.frontmatter(path)
            except Exception:
                # A broken command file must not blank the list — skip it, keep answering.
                continue
            if fm.get("invocation") != "operator-typed":
                continue
            verbs.append({"name": _typed_name(path), "description": str(fm.get("description") or "")})
    return sorted(verbs, key=lambda v: v["name"])


def available_verbs(catalog_path: str | None = None) -> list:
    """The optional, not-yet-installed commands, RELAYED from the committed catalog the install step
    maintains — or an empty list when there is no catalog yet (a missing or absent catalog narrows the
    listing, never breaks it). Returns each entry as {name, description}, sorted by name. This tool only
    relays the catalog; it never decides the optional set itself.

    The default is no catalog (empty relay): the catalog and its exact home are owned by the first-run
    instantiator slice. The read-path below is a non-binding suggestion that slice is free to overrule;
    `.engine/provisioning/module-catalog.json` is one candidate. The shape relayed here (a JSON array of
    {name, description}) is likewise provisional and finalized with the catalog's owner."""
    if not catalog_path or not os.path.isfile(catalog_path):
        return []
    try:
        with open(catalog_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out = [{"name": str(e.get("name", "")), "description": str(e.get("description", ""))}
           for e in data if isinstance(e, dict) and e.get("name")]
    return sorted(out, key=lambda v: v["name"])


def _verb_line(verb: dict) -> str:
    desc = verb.get("description") or ""
    name = verb.get("name", "")
    return f"  /{name} — {desc}" if desc else f"  /{name}"


def render(installed: list, available: list) -> str:
    """The plain-language listing the operator sees. Installed commands first (alphabetical), then the
    optional ones (alphabetical) or a single plain line when there are none — never a bare empty heading
    — and a closing pointer to the getting-started guide. A pure function of its inputs: no clock, no
    network, no MCP."""
    lines = [_HEADER, ""]
    lines.extend(_verb_line(v) for v in installed)
    lines.append("")
    if available:
        lines.append(_AVAILABLE_HEADER)
        lines.append("")
        lines.extend(_verb_line(v) for v in available)
    else:
        lines.append(_EMPTY_AVAILABLE_LINE)
    lines.append("")
    lines.append(_POINTER)
    return "\n".join(lines)


def _demo() -> int:
    """An operator-runnable demonstration that the listing always answers. It prints the real listing,
    then re-runs the REAL listing logic over a throwaway temporary copy of the commands that has no
    optional-commands catalog and one deliberately broken command file — showing the listing still
    renders (the broken command skipped, the rest intact, nothing crashing). Real files are untouched."""
    import shutil
    import tempfile

    print("Your Engine's commands, the way /engine-help lists them:\n")
    print(render(installed_verbs(), available_verbs()))
    print("\n" + "-" * 70 + "\n")
    print("The same listing when a command file is broken — to show the help always answers.\n"
          "This copies your commands into a throwaway temporary folder (your real files are NOT\n"
          "touched), then plants one deliberately broken command file in the copy:\n")
    with tempfile.TemporaryDirectory() as tmp:
        dst_skills = os.path.join(tmp, ".claude", "skills")
        os.makedirs(dst_skills, exist_ok=True)
        src_skills = os.path.join(validate.ROOT, ".claude", "skills")
        for entry in sorted(glob.glob(os.path.join(src_skills, "engine-*"))):
            if os.path.isdir(entry):
                shutil.copytree(entry, os.path.join(dst_skills, os.path.basename(entry)))
        broken_dir = os.path.join(dst_skills, "engine-broken")
        os.makedirs(broken_dir, exist_ok=True)
        with open(os.path.join(broken_dir, "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write("---\ndescription: [this line is broken\ninvocation: operator-typed\n---\n\n"
                     "## Steps\n\n1. Go.\n")
        print(render(installed_verbs(root=tmp), available_verbs(None)))
    print("\nThe broken command was skipped, the rest are still listed, and nothing crashed — so\n"
          "\"what can I do here?\" always gets an answer, even during an outage or with a damaged file.")
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    print(render(installed_verbs(), available_verbs()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
