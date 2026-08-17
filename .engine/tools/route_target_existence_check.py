#!/usr/bin/env python3
"""Route target existence — the custom/script entry for engine/check/route-target-existence (ADR 0336).

A route that names a workflow it cannot reach is a dead end: the model recognizes the request, then points at
an operation, tool, or subordinate skill that is not there. This check reads every engine skill's structured
`engine-targets` and enforces the routing contract ADR 0336 sets:

  PRESENCE — every MODEL-REACHABLE route (invocation in codex_gen._MODEL_REACHABLE = {model-auto, model-only})
     names at least one engine-target. A route that recognizes intent but points nowhere is a silent gap.
  ACTIVE TARGETS RESOLVE — a target marked `availability: active` must exist on disk: an operation/tool at its
     repo-relative `ref`, or a subordinate skill at `.claude/skills/<ref>/SKILL.md`. A broken active target is
     a route into nothing.
  WELL-FORMED — each target names a known `kind` (operation/tool/skill), a non-blank `ref`, and a recognized
     `availability`; a `module-conditional` target also names its `owner` module.

A `module-conditional` or `home-only` target is EXPLICITLY allowed to be absent (a module the deployment
declined, or a home-only asset retired on install) — the routing-map derivation tolerates the same, so this
check never demands their existence, only that they are well-formed. Reads local committed files only. Emits
finding.v1 JSON on stdout, exit 0 on a successful evaluation; a crash exits non-zero (fail-closed).
"""
from __future__ import annotations
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402
import skill_discovery  # noqa: E402  (the shared skill-discovery helper — one glob + slug-identity path)
import codex_gen  # noqa: E402  (the single home of _MODEL_REACHABLE — reachability lives there, not here)

_KINDS = ("operation", "tool", "skill")
_AVAILABILITY = ("active", "module-conditional", "home-only")


def _target_exists(base: str, kind: str, ref: str) -> bool:
    """Whether an `active` target resolves on disk. operation/tool are repo-relative paths; a skill target is a
    slug resolving to its SKILL.md."""
    if kind in ("operation", "tool"):
        return os.path.exists(os.path.join(base, ref))
    if kind == "skill":
        return os.path.exists(os.path.join(base, ".claude", "skills", ref, "SKILL.md"))
    return False


def findings(tier: str, root: str | None = None) -> list:
    base = root or validate.ROOT
    out = []
    # strict=True is the GUARD posture: an unparseable skill RAISES here (crash → hard finding via the runner)
    # rather than silently vanishing from the scan.
    for rec in skill_discovery.records("claude", root=base, strict=True):
        slug = rec["slug"]
        fm = rec["frontmatter"]
        loc = {"file": os.path.relpath(rec["path"], base), "line": None}
        targets = fm.get("engine-targets")
        # Platform-truth reachability: an omitted invocation is model-auto = reachable, so a route that skips
        # the invocation line the schema invites is still required to name its targets, never silently exempt.
        reachable = codex_gen.is_platform_reachable(fm.get("invocation"))

        if not targets:
            if reachable:
                out.append(validate.finding(tier,
                           f"The route '{slug}' is model-reachable but names no engine-targets, so it "
                           f"recognizes a request and then points nowhere. Give it an `engine-targets` entry "
                           f"naming the operation, tool, or subordinate skill it routes into.", loc))
            continue
        if not isinstance(targets, list):
            out.append(validate.finding(tier,
                       f"The route '{slug}' has a malformed `engine-targets` (it must be a list of targets).", loc))
            continue

        for t in targets:
            if not isinstance(t, dict):
                out.append(validate.finding(tier, f"The route '{slug}' has a malformed target (each must be a "
                           f"mapping of kind/ref/availability).", loc))
                continue
            kind, ref, avail = t.get("kind"), t.get("ref"), t.get("availability")
            if kind not in _KINDS:
                out.append(validate.finding(tier, f"The route '{slug}' names a target of unknown kind "
                           f"'{kind}' (expected one of {', '.join(_KINDS)}).", loc))
                continue
            if not isinstance(ref, str) or not ref.strip():
                out.append(validate.finding(tier, f"The route '{slug}' has a {kind} target with no `ref`.", loc))
                continue
            if avail not in _AVAILABILITY:
                out.append(validate.finding(tier, f"The route '{slug}' target '{ref}' has an unrecognized "
                           f"availability '{avail}' (expected one of {', '.join(_AVAILABILITY)}).", loc))
                continue
            if avail == "module-conditional" and not (t.get("owner") or "").strip():
                out.append(validate.finding(tier, f"The route '{slug}' target '{ref}' is module-conditional but "
                           f"names no `owner` module.", loc))
                continue
            if avail == "active" and not _target_exists(base, kind, ref):
                out.append(validate.finding(tier,
                           f"The route '{slug}' names the active {kind} target '{ref}', but it does not exist. "
                           f"Fix the reference, or mark the target module-conditional/home-only if it is "
                           f"legitimately absent here.", loc))
    return out


def main(argv: list) -> int:
    tier = os.environ.get("ENGINE_RULE_TIER", "hard")
    # ROUTE_TARGET_FIXTURE_ROOT (unset in production) points the scan at a seeded fixture repo root so the
    # negative-fixture meta-check witnesses the guard biting a real broken target.
    fixture = validate.env_override_path("ROUTE_TARGET_FIXTURE_ROOT")
    print(json.dumps(findings(tier, root=fixture)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
