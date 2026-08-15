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
agent_coherence_findings): ZERO personas shipped with the grammar, so the leg had nothing to
fire on; the review/audit personas now ship, so the guard has real subjects and runs every CI —
arming its role/model-tier/lens legs live for the first time alongside the new permissions rule.

GIT-SAFETY LEG (StarshipSuperjam/engine-template#947): a persona the platform would let run `Bash` can execute
commands, and a review agent that runs commands has twice mutated a shared checkout's real git state
(a `git stash` clobber; a `git worktree add` + remote repoint that rewrote a shared origin). So a
second leg requires every Bash-keeping persona's body to carry the git-safety recipe — work only in a
throwaway copy you make yourself, never `git worktree add` from an existing checkout, never repoint a
remote, never stash/reset a checkout you did not create — so the recipe ships IN the pack rather than
living in session memory. The design-review lenses and the audit persona are Bash-locked in their own
frontmatter, so they are exempt.

HONEST LIMIT: the write-tool leg enforces the Edit/Write/NotebookEdit floor and the git-safety leg
enforces that the recipe is PRESENT; neither can police what a shell actually does at runtime. Runtime
confinement of a Bash command to a throwaway copy is the orchestration worktree's + the protected-branch
merge gate's job, not a static invariant these legs can see.

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

# Git-safety recipe tokens (StarshipSuperjam/engine-template#947). A persona the platform would let run Bash must
# carry the git-safety recipe in its BODY, so a fresh session following the shipped pack cannot
# innocently re-create the two real incidents (a `git stash` clobber; a `git worktree add` + remote
# repoint that rewrote a shared origin). These two substrings anchor that recipe: the sanctioned copy
# primitive, and the prohibition that caused the second incident. Their presence is what the git-safety
# leg requires; the merge gate and the reviewer judge that the surrounding prose is real.
_GIT_SAFETY_TOKENS = ("clone_engine", "git worktree add")
_GIT_SAFETY_MESSAGE = (
    "Persona '{name}' keeps the Bash shell but its body is missing the git-safety recipe a "
    "shell-capable review persona must carry (missing: {missing}). A review agent that runs commands "
    "must work only in a throwaway copy it makes itself; add the recipe to .claude/agents/{name}.md — "
    "clone the tracked engine files into a fresh throwaway directory with engine_fixture.clone_engine() and "
    "run only there, never `git worktree add` from an existing checkout (a worktree shares its .git/config, "
    "so a remote change inside it repoints the real one), and never stash/checkout/switch/reset or "
    "change a remote in a checkout you did not create.")


def _keeps_bash(fm: dict) -> bool:
    """True when the platform would let this persona run the Bash shell: an explicit `tools` allowlist
    that includes Bash, a `disallowedTools` denylist (a list) that omits it, or neither (the inherit-all
    default). Mirrors the write-tool leg's conservative list-form reading — a string-valued
    tools/disallowedTools is treated as not-a-list, so it neither allows nor blocks and the fall-through
    requires the recipe. This errs toward requiring the git-safety recipe, never toward exempting a
    shell-capable persona from it."""
    allow = fm.get("tools")
    if isinstance(allow, list):
        return "Bash" in allow
    deny = fm.get("disallowedTools")
    if isinstance(deny, list):
        return "Bash" not in deny
    return True


def git_safety_findings(tier: str, root: str | None = None, agents_dir: str | None = None) -> list:
    """One finding per shell-capable persona whose body omits the git-safety recipe
    (StarshipSuperjam/engine-template#947). Unlike the pure frontmatter leg in validate, this reads the persona
    FILE — frontmatter to decide Bash access, body to check the recipe — so it lives here in the
    consumer. A Bash-locked persona (the design-review lenses, the audit persona) is exempt. Honours the
    same ENGINE_AGENT_FIXTURE_DIR seam so the negative-fixture meta-check can witness it biting."""
    if agents_dir:
        paths = sorted(glob.glob(os.path.join(agents_dir, "*.md")))
    else:
        base = root or validate.ROOT
        paths = sorted(glob.glob(os.path.join(base, _AGENT_GLOB)))
    findings = []
    for path in paths:
        fm = dict(validate.frontmatter(path))
        name = fm.get("name") or os.path.splitext(os.path.basename(path))[0]
        if not _keeps_bash(fm):
            continue
        body = validate._body_without_frontmatter(validate.read(path))
        missing = [tok for tok in _GIT_SAFETY_TOKENS if tok not in body]
        if missing:
            findings.append(validate.finding(
                tier, _GIT_SAFETY_MESSAGE.format(name=name, missing=", ".join(missing))))
    return findings


def engine_agents(root: str | None = None, agents_dir: str | None = None) -> list:
    """Parse the present personas' frontmatter. Inject the filename stem as `name` when the
    frontmatter omits it, so a finding names the persona file the operator would actually open.

    `agents_dir` is the negative-fixture meta-check's seam (StarshipSuperjam/engine-template#286): glob `*.md` directly under that
    directory instead of a real `.claude/agents` tree — so a committed negative fixture is NOT discovered
    by Claude Code's own agent loader (which scans `.claude/agents/**`) and shipped into every adopter as a
    phantom persona. The coherence logic over the parsed frontmatter is identical either way."""
    agents = []
    if agents_dir:
        paths = sorted(glob.glob(os.path.join(agents_dir, "*.md")))
    else:
        base = root or validate.ROOT
        paths = sorted(glob.glob(os.path.join(base, _AGENT_GLOB)))
    for path in paths:
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
    print("Your engine's review/audit personas, and the safety check that makes sure the read-only "
          "ones carry the lock that blocks the file-writing tools:\n")
    if not present:
        print("  (no personas are installed yet)")
        return 0
    for a in present:
        if a.get("permissions") != "read-only":
            continue
        deny = a.get("disallowedTools")
        locked = isinstance(deny, list) and all(t in deny for t in ("Edit", "Write", "NotebookEdit"))
        no_bash = locked and "Bash" in deny
        if not locked:
            note = "read-only but NOT locked — it would inherit the file-writing tools"
        elif no_bash:
            note = "read-only — carries the lock on the file-writing tools, and can't run commands"
        else:
            note = "read-only — carries the lock on the file-writing tools (keeps Bash to run checks)"
        print(f"  {str(a.get('name')):34} {note}")

    clean = validate.agent_coherence_findings(present, tier, _MESSAGE)
    if clean:
        print("\nThe safety check found a problem with the personas as installed (see engine-ci).")
    else:
        print("\nThe safety check: all clear — every read-only persona carries the lock that blocks the "
              "file-writing tools. (This check confirms the lock is DECLARED; that the platform then "
              "honors it is confirmed separately, in a fresh session — see the PR's review steps.)")

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
    print("\nThat is the safety net: a read-only reviewer can't quietly drop the lock that blocks the "
          "file-writing tools — the check catches it before it could be merged.")

    # Git-safety leg (StarshipSuperjam/engine-template#947): every Bash-keeping persona must carry the recipe in its body.
    import tempfile
    live_gs = git_safety_findings(tier)
    bash_keepers = [str(a.get("name")) for a in present if _keeps_bash(a)]
    print(f"\nThe shell-capable review personas — {', '.join(bash_keepers) or '(none)'} — can run "
          f"commands, so each must carry the git-safety recipe in its own text (work only in a throwaway "
          f"copy you make yourself; never worktree-add from, or repoint a remote on, a checkout you did "
          f"not create):")
    if live_gs:
        print("  -> the git-safety check is RED (see engine-ci): a shell-capable persona is missing the recipe.")
    else:
        print("  -> the git-safety check: all clear — every shell-capable persona carries the recipe.")
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "shell-persona-without-recipe.md"), "w") as fh:
            fh.write("---\nname: shell-persona-without-recipe\nrole: pre-submission-review\n"
                     "lens: spec-conformance\nmodel-tier: judgment\npermissions: read-only\n"
                     "disallowedTools: [Edit, Write, NotebookEdit]\n---\n\nA reviewer that keeps the shell "
                     "but never states the git-safety recipe.\n")
        gs = git_safety_findings(tier, agents_dir=tmp)
    print("\nNow suppose a new reviewer kept the shell but never stated that recipe (written to a "
          "throwaway folder here — your files are untouched):")
    if gs:
        print("  -> the git-safety check turns RED: nothing in the persona tells it to stay in a throwaway "
              "copy, so it could innocently run a command against your real checkout. The build is blocked "
              "until the recipe is added.")

    print("\nThe honest limit: these checks confirm two things are DECLARED — the lock on the file-writing "
          "tools (Edit/Write/NotebookEdit), and the git-safety recipe in each shell-capable persona's text. "
          "They do NOT police what a shell actually does at runtime, nor writes through any write-capable "
          "MCP tools the session exposes; confining those to a throwaway copy is the build's worktree "
          "isolation, and your merge gate is the guarantee that nothing a reviewer touches reaches your "
          "main branch.")
    if not found:
        print("\nDEMO UNEXPECTED: the guard did not flag the removed tool lock.", file=sys.stderr)
        return 1
    if not gs:
        print("\nDEMO UNEXPECTED: the git-safety leg did not flag the recipe-less shell persona.", file=sys.stderr)
        return 1
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    tier = os.environ.get("ENGINE_RULE_TIER", "hard")
    # ENGINE_AGENT_FIXTURE_DIR (unset in production) lets the negative-fixture meta-check point the persona
    # scan at a seeded non-.claude fixture dir, so the coherence gate is witnessed biting a real bad input
    # (StarshipSuperjam/engine-template#286) without the fixture being loaded as a real persona by Claude Code's own loader.
    fixture_dir = validate.env_override_path("ENGINE_AGENT_FIXTURE_DIR")
    agents = engine_agents(agents_dir=fixture_dir)
    findings = validate.agent_coherence_findings(agents, tier, _MESSAGE)
    findings += git_safety_findings(tier, agents_dir=fixture_dir)
    return emit(findings)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
