#!/usr/bin/env python3
"""Provider parity — the custom/script entry for engine/check/codex-provider-parity (eADR-0035).

Every capability the engine exposes on one AI runtime must have its counterpart on the other, in
BOTH directions, and the only sanctioned differences are the committed entries of the
provider-exception ledger (.engine/policies/provider-exceptions.json — itself schema-checked and
inside the guardrail floor). Five legs, each with a written equivalence rule:

  HOOKS   — an engine hook is (event, script + args, matcher) with the script extracted from the
            command by each runtime's own grammar (the Claude launcher form vs the Codex shim form);
            the sets must match both ways, modulo ledger entries of kind `hook` (matched by the
            script+args in the entry id).
  MCP     — an engine server is (name, server script — the command's last argument); .mcp.json's
            engine servers and .codex/config.toml's engine tables must match both ways, modulo
            ledger entries of kind `mcp`.
  SKILLS  — an engine typed command is its verb (the engine-prefixed directory name); the
            .claude/skills and .agents/skills rosters must match both ways, modulo ledger entries
            of kind `skill`.
  AGENTS  — a review persona is its slug (.claude/agents/<slug>.md vs .codex/agents/<slug>.toml);
            the rosters must match both ways, modulo ledger entries of kind `agent`.
  FLOORS  — each runtime instruction floor exists (CLAUDE.md/AGENTS.md, and the deployed pair in
            the construction repo), and NO committed AGENTS.override.md exists anywhere (it would
            silently mask the floor).

A ledger entry that matches no real asymmetry is surfaced softly (a stale exception is clutter that
hides real ones, not a merge-stopper). Reads local committed files only. Emits finding.v1 JSON on
stdout, exit 0 on a successful evaluation; a crash exits non-zero (fail-closed).
"""
from __future__ import annotations
import glob
import json
import os
import re
import sys
import tomllib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402

LEDGER_REL = os.path.join(".engine", "policies", "provider-exceptions.json")

# The script+args token of each runtime's hook command. Claude: the first .engine/tools/ path token
# that is not the launcher, plus the bare args tail. Codex: the shim's quoted script argument plus
# the bare args tail. Single-homed here; the hook-command drift tests pin the command forms.
_CLAUDE_SCRIPT_RE = re.compile(r'"\$\{CLAUDE_PROJECT_DIR\}/(\.engine/tools/(?!hook-runner\.sh)[^"]+)"(.*)$')
_CODEX_SCRIPT_RE = re.compile(r'sh "\.engine/tools/codex-hook-runner\.sh" "([^"]+)"(.*)$')


def _hook_identity(command: str):
    for pattern in (_CLAUDE_SCRIPT_RE, _CODEX_SCRIPT_RE):
        m = pattern.search(command)
        if m:
            return (m.group(1) + m.group(2)).strip()
    return None


def _engine_hooks(data: dict) -> set:
    out = set()
    for event, groups in (data.get("hooks") or {}).items():
        for group in (groups or []):
            matcher = group.get("matcher") or None
            for h in (group.get("hooks") or []):
                command = (h or {}).get("command", "")
                if ".engine/" in command:
                    script = _hook_identity(command)
                    if script:
                        out.add((event, matcher, script))
    return out


def _load_json(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _ledger(root: str):
    try:
        entries = _load_json(os.path.join(root, LEDGER_REL)).get("exceptions", [])
        return [e for e in entries if isinstance(e, dict)]
    except (OSError, ValueError):
        return []


def _excepted(ledger: list, kind: str, identity: str) -> bool:
    return any(e.get("kind") == kind and e.get("id") == identity for e in ledger)


def findings(tier: str, root: str | None = None) -> list:
    base = root or validate.ROOT
    ledger = _ledger(base)
    out, used = [], set()

    def miss(kind: str, identity: str, side: str, detail: str):
        if _excepted(ledger, kind, identity):
            used.add((kind, identity))
            return
        out.append(validate.finding(tier,
                   f"Runtime parity broke: {detail} — present for {side} with no counterpart and no "
                   f"committed exception. Add the counterpart, or record the difference in "
                   f"{LEDGER_REL} with a reason (eADR-0035).", None))

    # HOOKS
    try:
        claude_hooks = _engine_hooks(_load_json(os.path.join(base, ".claude", "settings.json")))
    except (OSError, ValueError):
        claude_hooks = set()
    try:
        codex_hooks = _engine_hooks(_load_json(os.path.join(base, ".codex", "hooks.json")))
    except (OSError, ValueError):
        codex_hooks = set()
    for event, matcher, script in sorted(claude_hooks - codex_hooks):
        miss("hook", script, "Claude Code",
             f"the engine step '{script}' on {event}" + (f" ({matcher})" if matcher else ""))
    for event, matcher, script in sorted(codex_hooks - claude_hooks):
        miss("hook", script, "Codex",
             f"the engine step '{script}' on {event}" + (f" ({matcher})" if matcher else ""))

    # MCP
    def server_script(definition: dict) -> str:
        args = definition.get("args") or []
        return str(args[-1]) if args else str(definition.get("command", ""))
    try:
        claude_mcp = {name: server_script(d) for name, d in
                      (_load_json(os.path.join(base, ".mcp.json")).get("mcpServers") or {}).items()
                      if name.startswith("engine-")}
    except (OSError, ValueError):
        claude_mcp = {}
    try:
        with open(os.path.join(base, ".codex", "config.toml"), "rb") as fh:
            codex_mcp = {name: server_script(d) for name, d in
                         (tomllib.load(fh).get("mcp_servers") or {}).items()
                         if name.startswith("engine-")}
    except (OSError, ValueError):
        codex_mcp = {}
    for name in sorted(set(claude_mcp) - set(codex_mcp)):
        miss("mcp", name, "Claude Code", f"the engine helper server '{name}'")
    for name in sorted(set(codex_mcp) - set(claude_mcp)):
        miss("mcp", name, "Codex", f"the engine helper server '{name}'")
    for name in sorted(set(claude_mcp) & set(codex_mcp)):
        if claude_mcp[name] != codex_mcp[name] and not _excepted(ledger, "mcp", name):
            out.append(validate.finding(tier, f"Runtime parity broke: the engine helper server "
                       f"'{name}' launches a different program on each runtime ({claude_mcp[name]} "
                       f"vs {codex_mcp[name]}). The two runtimes must reach the same helper.", None))

    # SKILLS
    claude_skills = {os.path.basename(os.path.dirname(p)) for p in
                     glob.glob(os.path.join(base, ".claude", "skills", "engine-*", "SKILL.md"))}
    codex_skills = {os.path.basename(os.path.dirname(p)) for p in
                    glob.glob(os.path.join(base, ".agents", "skills", "engine-*", "SKILL.md"))}
    for verb in sorted(claude_skills - codex_skills):
        miss("skill", verb, "Claude Code", f"the typed command '{verb}'")
    for verb in sorted(codex_skills - claude_skills):
        miss("skill", verb, "Codex", f"the typed command '{verb}'")

    # AGENTS
    claude_agents = {os.path.splitext(os.path.basename(p))[0] for p in
                     glob.glob(os.path.join(base, ".claude", "agents", "*.md"))}
    codex_agents = {os.path.splitext(os.path.basename(p))[0] for p in
                    glob.glob(os.path.join(base, ".codex", "agents", "*.toml"))}
    for slug in sorted(claude_agents - codex_agents):
        miss("agent", slug, "Claude Code", f"the review persona '{slug}'")
    for slug in sorted(codex_agents - claude_agents):
        miss("agent", slug, "Codex", f"the review persona '{slug}'")

    # FLOORS
    pairs = [("CLAUDE.md", "AGENTS.md")]
    if os.path.isfile(os.path.join(base, "CLAUDE.deployed.md")) \
            or os.path.isfile(os.path.join(base, "AGENTS.deployed.md")):
        pairs.append(("CLAUDE.deployed.md", "AGENTS.deployed.md"))
    for claude_floor, agents_floor in pairs:
        for present, absent in ((claude_floor, agents_floor), (agents_floor, claude_floor)):
            if os.path.isfile(os.path.join(base, present)) \
                    and not os.path.isfile(os.path.join(base, absent)):
                miss("floor", absent, "one runtime only",
                     f"the instruction floor pair ({present} exists, {absent} is missing)")
    for override in glob.glob(os.path.join(base, "**", "AGENTS.override.md"), recursive=True):
        out.append(validate.finding(tier, f"'{os.path.relpath(override, base)}' would silently MASK "
                   f"the engine's AGENTS.md floor for every session under its directory — Codex "
                   f"reads the override INSTEAD of the floor. Remove it (fold anything needed into "
                   f"the floor or the project's own AGENTS.md content).", validate.loc(override)))

    # Stale exceptions: a `missing` entry that excused nothing is soft clutter that hides real ones.
    # A `weaker` entry documents a strength difference no roster comparison can see (an instruction
    # standing in for enforcement, a platform-inherent gap), so it is never read as stale here.
    for e in ledger:
        key = (e.get("kind"), e.get("id"))
        if key not in used and e.get("missing_or_weaker") == "missing":
            out.append(validate.finding("soft", f"The provider-exception entry {key[0]}:'{key[1]}' "
                       f"matched no real difference this run — if the capability now exists on both "
                       f"runtimes, remove the stale entry from {LEDGER_REL}.", None))
    return out


def main(argv: list) -> int:
    tier = os.environ.get("ENGINE_RULE_TIER", "hard")
    # ENGINE_PARITY_FIXTURE_ROOT (unset in production) points every leg at a seeded fixture repo
    # root so the negative-fixture meta-check witnesses the guard biting a real bad input.
    fixture = validate.env_override_path("ENGINE_PARITY_FIXTURE_ROOT")
    print(json.dumps(findings(tier, root=fixture)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
