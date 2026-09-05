#!/usr/bin/env python3
"""Stamp each engine persona with the model + reasoning effort that realize its capability tier, from the one
committed bindings file (.engine/policies/model-bindings.json).

Personas stay capability-shaped — they declare a `model-tier` (judgment / mechanical), never a model name.
This tool is the single place that tier is turned into a concrete model + effort, so retuning the whole fleet
for a new model landscape is one edit to model-bindings.json followed by `agent_bindings.py render`. `check`
reports drift — a persona whose stamped model/effort no longer matches the binding, or an override that names
no installed persona — and backs the coherence unit test (test_agent_bindings.py). It edits only the two
frontmatter lines it owns; the persona's prose and every other frontmatter field are untouched.

REVIEWER ROLES CARRY MODEL BUT NOT EFFORT. Review depth is the lens roster on each side of the seal plus each
lens's model pin; a reviewer's reasoning effort is not part of the review contract, so a reviewer persona
carries no `effort:` line, `render` stamps only `model:` for the reviewer roles, and `check` flags a stray
effort line as drift (model drift is still caught). A reviewer runs at whatever effort the harness that spawns
it is configured for, which is the operator's own setting on either runtime. Workers and the audit persona
carry BOTH stamps: their effort is an execution binding, not a review promise. For the same reason a
per-persona override may pin the MODEL alone, and the three reviewer overrides do.
"""
from __future__ import annotations
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import repo_identity  # noqa: E402  (dependency-light home-repo signal; scopes the dangling-override leg)

_AGENTS_REL = os.path.join(".claude", "agents")
_BINDINGS_REL = os.path.join(".engine", "policies", "model-bindings.json")
_FM_KEY = re.compile(r"^([A-Za-z0-9_-]+):\s*(.*)$")
_OWNED = re.compile(r"^(model|effort):")   # the two lines this tool owns (not 'model-tier', which starts model-)
# The reviewer roles that carry NO effort stamp: a reviewer's effort is not part of the review contract (see
# the module docstring). Keyed by role INCLUSION, never "everything but worker" — the audit persona (role:
# audit) keeps its effort stamp, so an exclusion of workers alone would wrongly un-pin it.
EFFORT_UNPINNED_ROLES = frozenset({"plan-review", "pre-submission-review"})


def _stamps_effort(fm: dict) -> bool:
    """True when this persona's effort is stamped into frontmatter (workers, audit); False for the reviewer
    roles, which carry none. `codex_gen` reuses EFFORT_UNPINNED_ROLES so the Codex twin omits
    `model_reasoning_effort` for exactly the same roles."""
    return fm.get("role") not in EFFORT_UNPINNED_ROLES


def _root(root: str | None = None) -> str:
    return root or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_bindings(root: str | None = None) -> dict:
    with open(os.path.join(_root(root), _BINDINGS_REL), encoding="utf-8") as fh:
        return json.load(fh)


def resolve(name: str, model_tier: str, bindings: dict) -> dict:
    """The {model, effort} for a persona: its override if one exists, else its tier default.

    An override may pin the MODEL alone. The three reviewer overrides do, because a reviewer persona
    carries no effort frontmatter by design — effort is not part of the review contract — so an effort
    on such an override would have nothing to ride on. An override without an effort falls back to the TIER's effort rather than to None: None means
    "deliberately un-pinned" to `_stamp`, and inferring that from a silent field would un-pin a worker
    whose author only meant to retune its model."""
    override = (bindings.get("overrides") or {}).get(name)
    tier = (bindings.get("tiers") or {}).get(model_tier)
    if override and "effort" in override:
        return {"model": override["model"], "effort": override["effort"]}
    if not tier:
        if override:
            raise KeyError(f"override for {name!r} pins no effort and its tier {model_tier!r} has no binding")
        raise KeyError(f"no binding for capability tier {model_tier!r}")
    if override:
        return {"model": override["model"], "effort": tier["effort"]}
    return {"model": tier["model"], "effort": tier["effort"]}


def _agent_files(root: str) -> list[str]:
    d = os.path.join(root, _AGENTS_REL)
    return sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".md"))


def _frontmatter(text: str):
    """(lines, end_index, {key: value}) for the leading YAML frontmatter, or None if absent."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = lines.index("---", 1)
    except ValueError:
        return None
    fm = {}
    for line in lines[1:end]:
        m = _FM_KEY.match(line)
        if m:
            fm[m.group(1)] = m.group(2).strip()
    return lines, end, fm


def _stamp(text: str, model: str, effort: str | None) -> str:
    """Return `text` with `model:` (and `effort:` when `effort` is not None) set immediately after the
    `model-tier:`/`implementation-class:` line, preserving every other line. Removes any existing model/effort
    lines first, so the render is idempotent. `effort is None` stamps no effort line (the reviewer roles)."""
    parsed = _frontmatter(text)
    if not parsed:
        raise ValueError("persona has no frontmatter")
    lines, end, _ = parsed
    body = [line for line in lines[1:end] if not _OWNED.match(line)]
    out = []
    for line in body:
        out.append(line)
        # Reviewers anchor the stamp after model-tier; workers (no model-tier) after implementation-class.
        if line.startswith("model-tier:") or line.startswith("implementation-class:"):
            out.append(f"model: {model}")
            if effort is not None:
                out.append(f"effort: {effort}")
    rebuilt = "\n".join(["---", *out, "---", *lines[end + 1:]])
    return rebuilt + "\n" if text.endswith("\n") else rebuilt


def _binding_for(fm: dict, bindings: dict) -> dict:
    """The {model, effort} for one persona: a review/audit persona resolves through its model-tier;
    a worker resolves through its implementation-class -> the Claude side of implementation_classes,
    so both axes are single-sourced from the one bindings file."""
    if fm.get("role") == "worker":
        cls = fm.get("implementation-class")
        provider = (bindings.get("implementation_classes", {}).get(cls) or {}).get("claude")
        if not provider:
            raise KeyError(f"no implementation_classes.{cls}.claude binding for worker persona")
        return {"model": provider["model"], "effort": provider["effort"]}
    return resolve(fm["name"], fm.get("model-tier"), bindings)


def render(root: str | None = None) -> list[str]:
    """Stamp every persona from the bindings. Returns the basenames actually changed."""
    root = _root(root)
    bindings = load_bindings(root)
    changed = []
    for path in _agent_files(root):
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        parsed = _frontmatter(text)
        if not parsed:
            continue
        _, _, fm = parsed
        binding = _binding_for(fm, bindings)
        effort = binding["effort"] if _stamps_effort(fm) else None
        new = _stamp(text, binding["model"], effort)
        if new != text:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(new)
            changed.append(os.path.basename(path))
    return changed


def check(root: str | None = None) -> list[str]:
    """Drift between the personas and the bindings: a persona whose stamped model/effort differs from its
    binding, or an override that names no installed persona. Empty list means in sync."""
    root = _root(root)
    bindings = load_bindings(root)
    problems, names = [], set()
    for path in _agent_files(root):
        with open(path, encoding="utf-8") as fh:
            parsed = _frontmatter(fh.read())
        if not parsed:
            continue
        _, _, fm = parsed
        names.add(fm["name"])
        binding = _binding_for(fm, bindings)
        if _stamps_effort(fm):
            want = {"model": binding["model"], "effort": binding["effort"]}
            got = {"model": fm.get("model"), "effort": fm.get("effort")}
        else:
            # A reviewer role: model is stamped (anchor) and the correct stamped state carries NO effort line,
            # because effort is not part of the review contract. Flag a wrong model, and flag a stray effort
            # line as drift.
            want = {"model": binding["model"], "effort": None}
            got = {"model": fm.get("model"), "effort": fm.get("effort")}
        if got != want:
            problems.append(f"{fm['name']}: stamped {got} != binding {want} — run agent_bindings.py render")
    # A dangling override — one naming no installed persona — is a source-authoring concern: in the engine's
    # OWN repo every persona ships, so an override matching none is a genuine typo or a stale entry worth
    # flagging. In a DEPLOYED repo the operator may DECLINE an optional review pack, which removes its personas
    # while the core-owned bindings file still carries their (now dormant) overrides — a benign state, not
    # drift. So run this leg ONLY when the checkout is CONFIDENTLY the home repo: a readable git origin that
    # matches the recorded home. Deliberately fail toward NOT-home when the origin is unreadable — the safe
    # direction here, since running the leg in a possibly-deployed repo (e.g. an arrival before its remote is
    # set) would re-red the very StarshipSuperjam/engine-template#646 symptom this closes. The unconditional drift leg above still runs
    # everywhere, so a persona silently downgraded to a weaker model is still caught in any deployment.
    own = repo_identity.origin_slug(root)
    try:
        home = repo_identity.home_repository(root)
    except Exception:  # noqa: BLE001 — a malformed manifest cannot confirm home; skip the leg (never red a deployment)
        home = None
    if own is not None and home is not None and repo_identity.slug_eq(own, home):
        for override in (bindings.get("overrides") or {}):
            if override not in names:
                problems.append(f"override '{override}' names no installed persona")
    return problems


def main(argv: list | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if argv and argv[0] == "render":
        changed = render()
        print("stamped: " + (", ".join(changed) if changed else "no changes"))
        return 0
    if argv and argv[0] == "check":
        problems = check()
        if problems:
            for p in problems:
                print("DRIFT: " + p, file=sys.stderr)
            return 1
        print("persona model/effort bindings are in sync")
        return 0
    print("usage: agent_bindings.py render | check")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
