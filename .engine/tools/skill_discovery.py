#!/usr/bin/env python3
"""Shared skill-discovery helper — the single home for finding the engine's own skills on disk.

Several tools need to enumerate the engine-prefixed skills (`.claude/skills/engine-*/SKILL.md` and their
committed Codex twins under `.agents/skills/engine-*/`), and each one used to re-glob the tree and re-derive
the typed-name-is-the-directory rule with its own private copy. `engine_help.py`'s docstring foresaw the
moment a third such globber would appear and called for exactly this extraction: a helper that "exposes the
raw per-file parse and lets each caller choose its posture." This is that helper.

What it centralizes — DISCOVERY only:
- the skill trees (which directories hold the Claude and Codex skills) and the engine-prefix glob;
- the IDENTITY rule — a skill's typed name is its DIRECTORY slug (a `SKILL.md` is always named `SKILL.md`,
  so the parent directory is the command the operator types); the legacy `.claude/commands/<name>.md` flat
  form falls back to the filename stem;
- the per-file frontmatter parse, through the one shared reader (`validate.frontmatter`), with a `strict`
  toggle for the malformed-file posture.

What it deliberately does NOT centralize — SEMANTICS. It never decides what an omitted `invocation` defaults
to, never filters to operator-invocable vs model-only, never maps a skill to a Codex policy. Those choices
diverge by design between callers — a degrade-never-blank operator listing reads an omitted invocation as
model-auto and skips a broken file, while the Codex policy render fails CLOSED on an omitted invocation and a
detection guard lets a malformed file raise — and each must stay visibly local to the caller that owns it.
Centralizing the raw parse but not its interpretation is what keeps those postures from silently converging.

The `strict` posture:
- `strict=False` (the listing posture): a malformed skill is dropped from the enumeration rather than
  allowed to crash it (degrade, never blank — `engine_help`'s always-answers guarantee).
- `strict=True` (the guard posture): `validate.frontmatter`'s raise on malformed YAML propagates, so a
  detection guard fails closed rather than silently passing over an unparseable skill.

Reads committed files only — no network, no token — so it runs unchanged in the head-checkout engine-ci
context, the same as every caller that used to inline this.
"""
from __future__ import annotations
import glob
import os

import validate

# The two runtime skill trees. The Claude side is canonical; the Codex side is a committed render of it
# (codex_gen.py). Both hold one directory per skill, each containing a `SKILL.md`.
SKILL_TREES = {
    "claude": os.path.join(".claude", "skills"),
    "codex": os.path.join(".agents", "skills"),
}
# The legacy flat command form, Claude-only. The directory is empty today (every command is a SKILL.md now),
# but the two operator-facing enumerators still include it so a legacy command file, if one ever reappeared,
# is neither lost from the listing nor from the self-election guard.
_LEGACY_COMMANDS_GLOB = os.path.join(".claude", "commands", "engine-*.md")

# The engine governs only its OWN skills (the engine- prefix — the engine/operator wall). The operator authors
# their own un-prefixed product skills in the same `.claude/skills/` directory, and the engine never touches
# those.
_ENGINE_GLOB = "engine-*"


def slug(path: str) -> str:
    """The command the operator types for a skill file: the skill DIRECTORY name for a `SKILL.md`, or the
    filename stem for a legacy `.claude/commands/<name>.md`. This is the skill's identity on the platform —
    the frontmatter `name` is only an optional display label (skill.v1)."""
    parent = os.path.basename(os.path.dirname(path))
    if parent and parent != "commands":
        return parent
    return os.path.splitext(os.path.basename(path))[0]


def skill_files(provider: str = "claude", root: str | None = None, skills_dir: str | None = None) -> list:
    """The engine-prefixed `SKILL.md` paths under one provider's skills tree, sorted. `skills_dir` overrides
    the tree with a literal directory (the negative-fixture meta-check's seam — glob `engine-*/SKILL.md`
    directly under it, so a seeded fixture is never discovered by the platform's own skill loader)."""
    if skills_dir is not None:
        base = skills_dir
    else:
        base = os.path.join(root or validate.ROOT, SKILL_TREES[provider])
    return sorted(glob.glob(os.path.join(base, _ENGINE_GLOB, "SKILL.md")))


def skill_dirs(provider: str = "claude", root: str | None = None) -> list:
    """The engine-prefixed skill DIRECTORIES under one provider's tree (each one containing a `SKILL.md`),
    sorted. The directory is the skill's identity — the render and parity callers work from it."""
    return [os.path.dirname(p) for p in skill_files(provider, root)]


def command_files(root: str | None = None) -> list:
    """The legacy engine-prefixed flat command files (`.claude/commands/engine-*.md`), sorted. Empty today;
    kept so the operator listing and the self-election guard never silently drop a legacy command."""
    return sorted(glob.glob(os.path.join(root or validate.ROOT, _LEGACY_COMMANDS_GLOB)))


def parse(path: str, strict: bool = False) -> dict | None:
    """One skill file's raw frontmatter, through the shared reader. With `strict=False` a malformed file
    yields `None` (the caller drops it — the listing posture); with `strict=True` the malformed-YAML raise
    from `validate.frontmatter` propagates (the guard posture, fail closed). A frontmatter-less file yields
    an empty dict either way (the governing schema's `required` catches it downstream), never `None` — `None`
    means specifically 'could not be parsed'."""
    if strict:
        return validate.frontmatter(path)
    try:
        return validate.frontmatter(path)
    except Exception:  # noqa: BLE001 — a broken skill is dropped, not allowed to blank the enumeration
        return None


def records(
    provider: str = "claude",
    root: str | None = None,
    skills_dir: str | None = None,
    strict: bool = False,
    include_commands: bool = False,
) -> list:
    """The provider's engine skills as `{provider, path, slug, frontmatter}` records, sorted by path. Each
    record exposes the RAW parsed frontmatter — the caller reads whatever fields it needs with its own
    defaults (this helper decides no semantics). Under `strict=False` a skill whose frontmatter cannot be
    parsed is omitted; under `strict=True` the parse raises. `include_commands` also enumerates the legacy
    Claude flat commands (ignored for the Codex provider, which has no commands tree). `skills_dir` is the
    fixture seam (Claude only)."""
    paths = list(skill_files(provider, root, skills_dir))
    if include_commands and provider == "claude" and skills_dir is None:
        paths = sorted(paths + command_files(root))
    out = []
    for path in paths:
        fm = parse(path, strict=strict)
        if fm is None:
            continue
        out.append({"provider": provider, "path": path, "slug": slug(path), "frontmatter": fm})
    return out
